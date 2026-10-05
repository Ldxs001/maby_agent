#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Base + ICL 端到端实测 —— 起真服务、跑真权重、出真音频。

与 tests/ 下的单元测试分工不同：那些用假服务验契约；这里验的是「真能出声、而且
每次出的声是同一条」。走的是产品真路径：

    tts_engine.acquire_service  →  起 serve.py（Base）
    tts_engine.synthesize       →  自动备音色档案（make_voice.py）→ 逐句合成
    tts_engine.synth_line       →  同句复跑，核波形是否逐字节相同

产出全部落在 `_smoke/_e2e_base/`，跑一次删一次，不碰任何真项目。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from podcast_maker import config_manager, layout, tts_engine  # noqa: E402

SCRATCH = os.path.join(HERE, "_e2e_base")
EPISODE = "1"

SCRIPT = [
    {"speaker": "A", "text": "我们今天聊的是本地语音合成的音色一致性。", "emotion": ""},
    {"speaker": "B", "text": "对，关键在于参考音频是否固定下来。", "emotion": ""},
    {"speaker": "A", "text": "固定之后，每一句的波形是不是可复现的？", "emotion": ""},
    {"speaker": "B", "text": "只要参数一致，就能逐字节复现。", "emotion": ""},
]

REPORT = {"steps": [], "verdict": {}}


def say(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    REPORT["steps"].append(line)


def vram():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return out
    except Exception:  # noqa: BLE001
        return "n/a"


def md5(path):
    """波形比对用的短指纹。注意：档案里的 `ref_md5` 字段名骗人，它存的是
    sha256 前 16 位（见 make_voice.write_profile），对账时别拿 md5 去比。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha16(path):
    """与 make_voice.write_profile 里 ref_md5 同一种算法（sha256 取前 16 位）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main() -> int:
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH)
    layout.ensure_dirs(SCRATCH)
    out_dir = layout.tmp_dir(SCRATCH, EPISODE)
    os.makedirs(out_dir, exist_ok=True)

    say("显存开工前 %s" % vram())

    cfg = config_manager.ConfigManager()          # 读项目 config.json
    say("引擎=%s A音色=%s B音色=%s"
        % (cfg.get("tts.engine"), cfg.get("tts.qwen3tts_voice_a"),
           cfg.get("tts.qwen3tts_voice_b")))

    # ----------------------------------------------- 对照项目（必须最先建）
    # 顺序有讲究：参考音频的生成走 CustomVoice（另占约 3.4GB 显存），而
    # pick_device() 在空闲显存不足时会**静默**退 CPU —— 退 CPU 得到的是另一条
    # 波形。所以两份对照档案都必须在任何模型驻留之前建好，否则比的是
    # 「GPU 路径 vs CPU 路径」，不是「项目 A vs 项目 B」。
    other = os.path.join(HERE, "_e2e_base_sibling")
    if os.path.isdir(other):
        shutil.rmtree(other)
    layout.ensure_dirs(other)
    py = tts_engine.venv_python()
    say("先建对照项目的音色档案（此时显存空闲 %s）" % vram())
    r = subprocess.run([py, os.path.join(ROOT, "tts_service", "make_voice.py"),
                        "--project-dir", other, "--json", "--role", "both"],
                       capture_output=True, cwd=os.path.join(ROOT, "tts_service"))
    out = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0 or '"error"' in out:
        say("对照项目建档案失败：%s"
            % ((r.stderr or b"").decode("utf-8", "replace")[-300:] or out[-300:]))
    sib_dev = {}
    for role in ("A", "B"):
        pf = layout.voice_profile_file(other, role)
        if os.path.isfile(pf):
            meta = json.load(open(pf, encoding="utf-8"))
            sib_dev[role] = "%s / %s" % (meta.get("device"), meta.get("backend"))
    say("对照项目档案录制设备 %s" % sib_dev)

    # ---------------------------------------------------------------- 起服务
    t0 = time.time()
    tts_engine.acquire_service(cfg, say)
    kind = ""
    for _ in range(60):
        kind = tts_engine.service_kind(cfg)
        if kind:
            break
        time.sleep(1)
    say("服务变体 model_kind=%r（%.1fs）" % (kind, time.time() - t0))
    REPORT["verdict"]["model_kind"] = kind

    voices = tts_engine._service_voices(cfg)
    ref_based = bool(voices) and all(v.get("ref_based") for v in voices)
    say("/speakers 返回 %d 条，ref_based=%s，首条=%s"
        % (len(voices), ref_based, voices[0]["name"] if voices else "-"))
    REPORT["verdict"]["speakers_ref_based"] = ref_based

    # ------------------------------------------------- 合成（自动备音色档案）
    t0 = time.time()
    res = tts_engine.synthesize(SCRIPT, out_dir, cfg, say,
                               emotion_level="none", voice_root=SCRATCH)
    say("整期合成完成：%d 句 / %.1fs" % (len(res["files"]), time.time() - t0))

    profiles = tts_engine.voice_profiles(SCRATCH)
    say("音色档案角色=%s" % sorted(profiles))
    REPORT["verdict"]["profiles"] = sorted(profiles)

    # 档案必须落在项目根的「音色/」，不能落到过程目录
    in_project = os.path.isdir(layout.voice_dir(SCRATCH))
    in_tmp = os.path.isdir(os.path.join(out_dir, layout.DIR_VOICE))
    say("档案位置 项目根/音色=%s  过程内/音色=%s" % (in_project, in_tmp))
    REPORT["verdict"]["profile_in_project_root"] = in_project and not in_tmp

    # profile.json 的 ref_md5（其实是 sha256 前 16 位）必须与实际文件对上
    sha_ok = {}
    for role in ("A", "B"):
        pf = layout.voice_profile_file(SCRATCH, role)
        wf = layout.voice_ref_file(SCRATCH, role)
        if not (os.path.isfile(pf) and os.path.isfile(wf)):
            sha_ok[role] = False
            continue
        meta = json.load(open(pf, encoding="utf-8"))
        actual = sha16(wf)
        recorded = str(meta.get("ref_md5") or "")
        sha_ok[role] = actual == recorded
        say("%s 角 profile.ref_md5=%s 实测 sha256[:16]=%s 设备=%s 一致=%s"
            % (role, recorded, actual, meta.get("device"), sha_ok[role]))
    REPORT["verdict"]["profile_sha_match"] = sha_ok

    # ------------------------------------------------- 音色跨项目可复现
    # 同设备、同后端、同内置音色名、同参考文案 → 两份档案应当逐字节相同。
    same = {}
    for role in ("A", "B"):
        a = layout.voice_ref_file(SCRATCH, role)
        b = layout.voice_ref_file(other, role)
        if os.path.isfile(a) and os.path.isfile(b):
            same[role] = (sha16(a) == sha16(b))
            say("%s 角 跨项目 sha 本项目=%s 对照=%s 相同=%s"
                % (role, sha16(a), sha16(b), same[role]))
        else:
            same[role] = False
    REPORT["verdict"]["cross_project_same_voice"] = same

    # ------------------------------------------------------- 逐句复现核对
    first = res["files"][0]
    ref_a = profiles.get("A")
    again = []
    for k in range(2):
        p = os.path.join(out_dir, "audio", "rep_%d.wav" % k)
        data = tts_engine.synth_line(SCRIPT[0]["text"], cfg.get("tts.qwen3tts_voice_a"),
                                     1.0, cfg, emotion="", degree="none", ref=ref_a)
        with open(p, "wb") as f:
            f.write(data)
        again.append(p)
    repo = [md5(p) for p in again]
    say("同句复跑两次 md5：%s  相同=%s" % (repo, repo[0] == repo[1]))
    REPORT["verdict"]["same_line_reproducible"] = repo[0] == repo[1]

    # 与逐期合成出来的第一句比 —— 含 atempo/重采样后的成品字节
    say("逐期首页 md5=%s（与复跑%s）"
        % (md5(first), "一致" if md5(first) == repo[0] else "不同（正常：复跑走的是同一后处理链，若不同需查）"))
    REPORT["verdict"]["episode_first_line_md5"] = md5(first)

    # 换角色必须换声 —— 拿 B 的档案合成同一句话，波形必须不同
    ref_b = profiles.get("B")
    if ref_b:
        pb = os.path.join(out_dir, "audio", "roleb.wav")
        data = tts_engine.synth_line(SCRIPT[0]["text"], cfg.get("tts.qwen3tts_voice_b"),
                                     1.0, cfg, emotion="", degree="none", ref=ref_b)
        with open(pb, "wb") as f:
            f.write(data)
        diff = md5(pb) != repo[0]
        say("换 B 角档案后 md5=%s 与 A 角不同=%s" % (md5(pb), diff))
        REPORT["verdict"]["role_switch_changes_audio"] = diff

    say("显存收工前 %s" % vram())
    tts_engine.release_service(say)
    time.sleep(3)
    say("显存释放后 %s" % vram())

    with open(os.path.join(HERE, "_e2e_base_report.json"), "w", encoding="utf-8") as f:
        json.dump(REPORT, f, ensure_ascii=False, indent=2)
    print("\n=== VERDICT ===")
    print(json.dumps(REPORT["verdict"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
