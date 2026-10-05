#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色音色档案 —— 把一段参考音频钉进项目目录。

为什么要有这个脚本
------------------
Base 变体的音色不在模型里，在一段参考音频里。这段音频是整期播客的**音色源头**：
它的质量决定后面每一句；它的内容决定种子，从而决定每一句的波形。所以它不能是
一个飘在临时目录里的文件，必须作为**项目资产**落盘、可追溯、可复制。

档案长这样（目录名由主程序 layout.DIR_VOICE 传入，默认「音色」）：

    <项目目录>/音色/A/ref.wav        参考音频
    <项目目录>/音色/A/ref.txt        转录文本（ICL 模式必须，一行）
    <项目目录>/音色/A/profile.json   溯源信息，含 ref_md5

三件事，各有分工
----------------
  **生成**：用 CustomVoice 变体把内置音色「录」成一段音频。它是个一次性进程 ——
      CustomVoice 与 Base 各占约 3.4GB 显存，8GB 的卡上放不下，所以不常驻。
  **继承**：把另一个项目的音色目录整个复制过来。跨项目音色完全一致靠的是这一步，
      而不是「重新生成一遍再祈祷参数对上」。
  **查账**：打印现有档案的内容指纹与音高，用来判断两个项目的音色是不是同一份。

参考文案是固定的
----------------
`REF_TEXTS` 两条**混合三句型文案**（陈述 + 疑问 + 惊叹各一句）是内置常量，
**不随项目内容变化**。原因有二：音色档案只负责音色，拿当期台词去当参考会让
「换个项目音色就变」；而句型混排是刻意为之——Base 克隆会整条继承 ref 的
韵律先验，混合 ref 让输出全局更生动（实验：疑问句句尾抬高约 +2.7 半音）。
念全靠门禁兜底：语速 + 语音段数不合格自动重念，防止「念两句就停」的残废
音频混进来（那会让每句输出复读缺的那句）。要跨项目一致，源头必须与项目无关。
需要别的文案时用 `--text` 显式指定，那种情况下音色基准会跟着换，自己心里有数。

生成的确定性边界
----------------
同一台机器上，**同一设备、同一后端**下，同一套音色名 + 同一句参考文案生成出的
音频逐字节相同 —— 实测同一目录 --force 重建三次，sha256 全同。重念机制不破这个
边界：尝试序列的种子是固定的（base + 0/7/14/21），哪一次定稿也由确定性波形决定，
所以同输入仍收敛到同一条定稿。换项目不会改变
这个结果，所以「新项目自动录一份」与「从老项目拷一份」在 GPU 上等价。

前提不能省。`pick_device()` 在空闲显存不足时会**静默**退 CPU，而 CPU 路径的数值
结果与 GPU 路径不是同一条（同一套输入，波形不同、F0 也不同）。所以录档案之前
要先确认没有别的模型占着显存；profile.json 里记了 `device`，两份档案音色对不上
时先看这一栏。真要跨项目对齐又要绕过这一切，用 `--from` 继承：复制文件，
不依赖任何数值巧合。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import serve  # noqa: E402

# 固定参考文案。**混合三句型**：陈述 + 疑问 + 惊叹各一句（2026-09 实验结论：
# Base 克隆整条继承 ref 的韵律先验——混合 ref 让输出的疑问句句尾抬高约
# +2.7 半音（超 1 半音可辨阈），全局韵律更生动；而给 Base 传 instruct 判死，
# 两轮干净对照一个反向一个无效，官方能力表没骗人）。代价是全局效应：陈述句
# 的句尾起伏也会变大——这是听感验收过可接受的。
# 仍必须与项目内容无关，否则音色基准会跟着项目漂。
REF_TEXTS = {
    "A": "这本书写得很有意思。这书真的是AI写的吗？这个结果太让人吃惊了！",
    "B": "我们先把问题理清楚。这样讲大家能听明白吗？没想到答案这么简单！",
}

# 念全校验的门禁（实验三轮踩坑换来的）：CustomVoice 一次念三句**有时念两句
# 就自己停**——ref 音频缺一句而 ref.txt 写三句时，Base 会把没被念出的那句
# 当成待合成文本补说，每条输出都复读一遍。拦法：语速（含标点字/有声秒）
# 必须落在人声区间、且语音段数 ≥3（三句话至少三段）。不合格自动重念。
RATE_MIN, RATE_MAX, MIN_SEGMENTS, MAX_ATTEMPTS = 3.5, 7.0, 3, 4

# 与 config_manager.PARAM_SPEC 里 tts.qwen3tts_voice_a / _b 的**出厂默认值**一致
# （Serena / Uncle_Fu）。主程序会把项目里配好的音色名通过 --voice-a / --voice-b
# 传进来覆盖它，这里只兜「直接跑命令行」那一种入口。
#
# 必须与出厂值对齐，不能随手挑两个顺耳的：两边不一致时，同一台机器上
# 「命令行直接跑」与「主程序自动跑」会录出两套不同音色，而档案里只看得出内置
# 音色名、看不出是哪个入口来的，排查起来毫无线索。有测试逐条比对这两处。
DEFAULT_VOICES = {"A": "Serena", "B": "Uncle_Fu"}

ROLES = ("A", "B")
WAV_NAME = "ref.wav"
TXT_NAME = "ref.txt"
PROFILE_NAME = "profile.json"
SCHEMA = 1


# --------------------------------------------------------------------- 小工具
def f0_med(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512) -> float:
    """自相关法取 F0 中位数 —— 只为记个数，方便日后对账音色有没有漂。"""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if not a.size:
        return float("nan")
    a = a - a.mean()
    peak = float(np.max(np.abs(a)))
    if peak <= 1e-9:
        return float("nan")
    a = a / peak
    lo, hi = max(1, int(sr / fmax)), min(frame - 1, int(sr / fmin))
    vals = []
    for s in range(0, len(a) - frame, hop):
        f = a[s:s + frame]
        if float(np.sqrt(np.mean(f ** 2))) < 0.02:
            continue
        ac = np.correlate(f, f, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if not len(seg):
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] / ac[0] < 0.3:
            continue
        vals.append(sr / k)
    return float(np.median(vals)) if vals else float("nan")


def role_dir(root: str, role: str) -> str:
    return os.path.join(root, role)


def read_profile(root: str, role: str) -> dict | None:
    """读一份档案的记录。文件不在、或 rec.wav 不在，都算没有。"""
    d = role_dir(root, role)
    prof = os.path.join(d, PROFILE_NAME)
    wav = os.path.join(d, WAV_NAME)
    if not (os.path.isfile(prof) and os.path.isfile(wav)):
        return None
    try:
        with io.open(prof, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    rec["role"] = role
    rec["dir"] = d
    rec["wav"] = wav
    return rec


def ref_paths(root: str, role: str) -> tuple[str, str] | None:
    """返回 (参考音频路径, 转录文本)。档案不全时返回 None。"""
    rec = read_profile(root, role)
    if not rec:
        return None
    txt = os.path.join(role_dir(root, role), TXT_NAME)
    if not os.path.isfile(txt):
        return None
    with io.open(txt, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return None
    return rec["wav"], text


def digest_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def voiced_stats(audio, sr: float, frame_s=0.025, hop_s=0.010,
                 gap_s: float = 0.3) -> tuple[float, int]:
    """有声时长（秒）与语音段数 —— 念全校验的两把尺。

    能量法：峰值 6% 以下的帧算静音；间隔小于 gap_s 的有声块合并为同一段。
    段数是关键：三句话至少要出现三段，只数总时长拦不住「念两句就停」。
    """
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    fl = max(1, int(sr * frame_s))
    hop = max(1, int(sr * hop_s))
    nfr = max(0, (len(x) - fl) // hop + 1)
    if nfr == 0:
        return 0.0, 0
    e = np.array([np.sqrt(np.mean(x[i * hop:i * hop + fl] ** 2))
                  for i in range(nfr)])
    voiced = e > max(e.max() * 0.06, 1e-6)
    step = hop / float(sr)
    segs, start = [], None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        if not v and start is not None:
            if (i - start) * step > 0.15:
                segs.append((start, i))
            start = None
    if start is not None:
        segs.append((start, nfr))
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < gap_s / step:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return float(voiced.sum() * step), len(merged)


# --------------------------------------------------------------------- 生成
def write_profile(role_root: str, role: str, wav_bytes: bytes, text: str,
                  rec: dict) -> dict:
    """落盘一份档案：波形 + 转录 + 记录。三件一起写，缺一件就等于没有。"""
    os.makedirs(role_root, exist_ok=True)
    wav_path = os.path.join(role_root, WAV_NAME)
    tmp = wav_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(wav_bytes)
    os.replace(tmp, wav_path)                     # 原子替换：不留下半个文件

    with io.open(os.path.join(role_root, TXT_NAME), "w",
                 encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")

    rec = dict(rec)
    rec["schema"] = SCHEMA
    rec["role"] = role
    rec["text"] = text
    # 内容指纹，与 serve.voice_key_for() 同一种算法（文件内容的 sha256 取前 16 位）。
    # 它有两个用处：跨项目对账「是不是同一份音色」，以及充当种子输入。
    rec["ref_md5"] = hashlib.sha256(wav_bytes).hexdigest()[:16]
    rec.pop("dir", None)
    rec.pop("wav", None)
    prof = os.path.join(role_root, PROFILE_NAME)
    tmp = prof + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, prof)
    return rec


def build(project_dir: str, roles=ROLES, voices=None, voice_dir_name: str = "音色",
          model: str = serve.CUSTOM_VOICE_MODEL, device: str = "auto",
          texts=None, force: bool = False, log=print) -> dict:
    """为项目的指定角色生成音色档案。已存在且非 force 时跳过。"""
    root = os.path.join(project_dir, voice_dir_name)
    voices = dict(DEFAULT_VOICES, **(voices or {}))
    texts = dict(REF_TEXTS, **(texts or {}))
    out: dict = {"voice_dir": root, "roles": {}, "generated": [], "skipped": []}

    todo = [r for r in roles if force or not ref_paths(root, r)]
    for r in roles:
        if r not in todo:
            out["skipped"].append(r)
    if not todo:
        log("音色档案已齐，不需要重新生成。")
        out["roles"] = {r: (read_profile(root, r) or {}) for r in roles}
        return out

    resolved = serve.resolve_model(model)
    kind = serve.model_kind(resolved)
    if kind != "custom_voice":
        raise RuntimeError(
            "生成参考音频要用内置音色变体，当前指到的是 %s（%s）。"
            % (resolved, kind))

    log(f"加载 {os.path.basename(resolved)} —— 只用来出参考音频，跑完就退")
    eng = serve.Engine(resolved, device, log)
    t0 = time.time()
    eng.ensure()
    log(f"  后端 {eng.backend} · 设备 {eng.device} · 加载 {time.time() - t0:.1f}s")

    # 跑在 CPU 上要吼一声，不能只当普通日志。这条路的数值结果与 GPU 路径不是
    # 同一条 —— 同一套音色名、同一句参考文案，CPU 录出来的波形与 GPU 录出来的
    # 不同（实测：A 角 sha256[:16] 从 713bab2d382ab0fd 变成另一条，F0 也从
    # 228.6Hz 偏走）。而 pick_device() 的退让是**静默**的：显存被别的模型占着
    # （例如上一期已把服务拉起来），它就悄悄退 CPU，慢十几倍，音色也不再与
    # 别的项目对齐。这跟「显存不够就快点跑完」是两回事，所以必须说出来。
    if str(eng.device or "").startswith("cpu"):
        log("  **注意**：本次跑在 CPU 上（%s）。录出的参考音频与 GPU 路径不是"
            "同一条波形，且慢十几倍。" % (eng.device_note or "空闲显存不足"))
        log("  **注意**：要跨项目对齐音色，先把显存腾空再录，"
            "或直接用 --from 继承上一份档案（复制文件，逐字节相同）。")

    for role in todo:
        voice = voices.get(role, "")
        text = texts.get(role, "")
        if not voice:
            raise RuntimeError("角色 %s 没有指定音色名。" % role)
        seed = serve.seed_for(text, voice)
        # 念全校验 + 自动重念：混合文案比旧的两句陈述长，CustomVoice 偶尔念
        # 两句就停（实验里 4.19s = 前两句的量）。不合格的波形**不落盘**，
        # 换个种子重念，直到念全或次数用尽——次数用尽宁可报错，也不能让
        # 残废 ref 混进项目当音色源头（那会让每句输出复读缺的那句）。
        attempts = 0
        while True:
            attempts += 1
            s = seed + (attempts - 1) * 7
            serve.set_seed(s)
            samples, sr = eng.synth_one(text, voice, instruct=None,
                                        language="Chinese", seed=s)
            audio = np.asarray(samples, dtype=np.float32).reshape(-1)
            vd, nseg = voiced_stats(audio, sr)
            rate = len(text) / vd if vd > 0.1 else float("inf")
            ok = (RATE_MIN <= rate <= RATE_MAX
                  and nseg >= MIN_SEGMENTS)
            log("  %s 角 念全校验 第%d次：有声%.2fs 语速%.1f字/s 段%d → %s"
                % (role, attempts, vd, rate, nseg,
                   "PASS" if ok else "FAIL"))
            if ok:
                break
            if attempts >= MAX_ATTEMPTS:
                raise RuntimeError(
                    "角色 %s 的参考音频连试 %d 次都没念全（语速 %.1f字/s、"
                    "%d 段）。请重跑一次；若反复出现，换文案或检查模型。"
                    % (role, attempts, rate, nseg))
        wav_bytes = serve.to_wav_bytes(samples, sr)
        dur = len(audio) / float(sr)
        d = role_dir(root, role)
        rec = write_profile(d, role, wav_bytes, text, {
            "source": "customvoice",
            "builtin_voice": voice,
            "model": model,
            "device": str(eng.device or ""),
            "backend": eng.backend,
            "seed": s,
            "sample_rate": int(sr),
            "ref_seconds": round(dur, 3),
            "voiced_seconds": round(vd, 3),
            "speech_rate": round(rate, 2),
            "attempts": attempts,
            "f0_med": round(f0_med(audio, sr), 1),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "origin_project": os.path.basename(os.path.normpath(project_dir)),
            "inherited_from": "",
        })
        out["roles"][role] = rec
        out["generated"].append(role)
        log("  %s 角 · %s · %.2fs · F0 %s · md5 %s"
            % (role, voice, dur, rec["f0_med"], rec["ref_md5"]))
    for r in out["skipped"]:
        out["roles"][r] = read_profile(root, r) or {}
    return out


# --------------------------------------------------------------------- 继承
def inherit(project_dir: str, src_project_dir: str, roles=ROLES,
            voice_dir_name: str = "音色", force: bool = False,
            log=print) -> dict:
    """把另一个项目的音色档案整个搬过来。

    跨项目「音色完全一样」只有这一条路走得通：复制文件，不是重新生成。
    重新生成能不能得到同一条，取决于参考文案、音色名、调用写法三样都对上 ——
    那是一串可以被改动的巧合，而文件的内容指纹不是。
    """
    dst_root = os.path.join(project_dir, voice_dir_name)
    src_root = os.path.join(src_project_dir, voice_dir_name)
    out: dict = {"voice_dir": dst_root, "roles": {}, "copied": [], "skipped": []}
    if not os.path.isdir(src_root):
        raise RuntimeError("源项目没有音色目录：%s" % src_root)

    for role in roles:
        src = ref_paths(src_root, role)
        if not src:
            out["skipped"].append(role)
            log("  %s 角：源项目里没有完整档案，跳过" % role)
            continue
        if not force and ref_paths(dst_root, role):
            out["skipped"].append(role)
            log("  %s 角：本项目已有档案，跳过（要覆盖加 --force）" % role)
            continue

        d = role_dir(dst_root, role)
        if os.path.isdir(d):
            shutil.rmtree(d)
        shutil.copytree(role_dir(src_root, role), d)

        rec = read_profile(dst_root, role) or {}
        rec["source"] = "inherited"
        rec["inherited_from"] = os.path.basename(
            os.path.normpath(src_project_dir))
        rec["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        for key in ("dir", "wav"):
            rec.pop(key, None)
        with io.open(os.path.join(d, PROFILE_NAME), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        out["roles"][role] = rec
        out["copied"].append(role)
        log("  %s 角：已继承（md5 %s，与源项目逐字节相同）"
            % (role, rec.get("ref_md5", "?")))
    return out


# --------------------------------------------------------------------- 查账
def status(project_dir: str, voice_dir_name: str = "音色") -> dict:
    """现有档案的实况。指纹按**当前文件内容**重算，不信任记录里的旧值 ——
    记录可以没跟着文件一起更新，那正是要查的东西。"""
    root = os.path.join(project_dir, voice_dir_name)
    out = {"voice_dir": root, "exists": os.path.isdir(root), "roles": {}}
    for role in ROLES:
        p = ref_paths(root, role)
        if not p:
            out["roles"][role] = {"ok": False}
            continue
        wav, text = p
        rec = read_profile(root, role) or {}
        st = os.stat(wav)
        sha = digest_of(wav)
        out["roles"][role] = {
            "ok": True,
            "wav": wav,
            "seconds": round(st.st_size / (24000 * 2.0), 2),
            "bytes": st.st_size,
            "content_sha": sha[:16],
            "recorded_md5": rec.get("ref_md5", ""),
            "matches_record": sha[:16] == rec.get("ref_md5", ""),
            "builtin_voice": rec.get("builtin_voice", ""),
            # 录这份档案时跑在哪条路上。同一套音色名 + 同一句参考文案，GPU 与
            # CPU 出的是两条不同的波形，所以这个字段是「两份档案音色不一致」
            # 时唯一的排查线索，不能省。
            "device": rec.get("device", ""),
            "backend": rec.get("backend", ""),
            "source": rec.get("source", ""),
            "inherited_from": rec.get("inherited_from", ""),
            "voiced_seconds": rec.get("voiced_seconds", ""),
            "speech_rate": rec.get("speech_rate", ""),
            "attempts": rec.get("attempts", ""),
            "text": text,
            "created": rec.get("created", ""),
        }
    return out


def _print_status(st: dict) -> None:
    print("音色目录 %s" % st["voice_dir"])
    for role in ROLES:
        r = st["roles"][role]
        if not r["ok"]:
            print("  %s 角：没有档案" % role)
            continue
        print("  %s 角：%.2fs · %s · 来源 %s%s"
              % (role, r["seconds"], r["builtin_voice"] or "?",
                 r["source"] or "?",
                 ("（继承自 %s）" % r["inherited_from"]
                  if r["inherited_from"] else "")))
        print("       指纹 %s %s"
              % (r["content_sha"],
                 "与记录一致" if r["matches_record"] else "**与记录不符**"))
        print("       录制设备 %s" % (r["device"] or "（旧档案未记）"))
        print("       文案 %s" % r["text"])
        if r.get("attempts"):
            print("       念全 %s次尝试 · 有声 %ss · 语速 %s字/s"
                  % (r["attempts"], r["voiced_seconds"], r["speech_rate"]))


# --------------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(
        description="角色音色档案：生成 / 继承 / 查账",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python make_voice.py --project-dir projects/20260916-100000\n"
               "  python make_voice.py --project-dir projects/新 --from "
               "projects/旧\n"
               "  python make_voice.py --project-dir projects/某 --list\n")
    ap.add_argument("--project-dir", required=True, help="项目根目录")
    ap.add_argument("--voice-dir", default="音色",
                    help="音色子目录名（主程序按 layout.DIR_VOICE 传入）")
    ap.add_argument("--role", default="both", choices=("A", "B", "both"))
    ap.add_argument("--voice-a", default=DEFAULT_VOICES["A"])
    ap.add_argument("--voice-b", default=DEFAULT_VOICES["B"])
    ap.add_argument("--text-a", default=REF_TEXTS["A"],
                    help="A 角参考文案；默认是内置固定句，改它等于换音色基准")
    ap.add_argument("--text-b", default=REF_TEXTS["B"])
    ap.add_argument("--from", dest="src", default="",
                    help="从另一个项目目录继承档案（跨项目音色一致走这条）")
    ap.add_argument("--model", default=serve.CUSTOM_VOICE_MODEL)
    ap.add_argument("--device", default="auto",
                    choices=("auto", "cuda", "cpu"))
    ap.add_argument("--force", action="store_true", help="已有档案时也重建")
    ap.add_argument("--list", action="store_true", help="只打印现状，不生成")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印结果")
    args = ap.parse_args()

    roles = ROLES if args.role == "both" else (args.role,)
    project = os.path.abspath(args.project_dir)
    if not os.path.isdir(project):
        raise SystemExit("项目目录不存在：%s" % project)

    def _log(msg):
        """json 模式下进度走 stderr、结果留在 stdout。

        调用方（主程序）要解析 stdout 那段 JSON，所以它必须是干净的一份 ——
        进度行混进去就解析不了。分开写，两边各取各的。
        """
        if args.json:
            print(msg, file=sys.stderr, flush=True)
        else:
            print(msg, flush=True)

    try:
        if args.list:
            st = status(project, args.voice_dir)
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=1))
            else:
                _print_status(st)
            return 0

        if args.src:
            res = inherit(project, os.path.abspath(args.src), roles,
                          args.voice_dir, args.force, _log)
        else:
            res = build(project, roles,
                        {"A": args.voice_a, "B": args.voice_b},
                        args.voice_dir, args.model, args.device,
                        {"A": args.text_a, "B": args.text_b},
                        args.force, _log)
        st = status(project, args.voice_dir)
        if args.json:
            print(json.dumps({"result": res, "status": st},
                             ensure_ascii=False, indent=1))
        else:
            print()
            _print_status(st)
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print("[失败] %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
