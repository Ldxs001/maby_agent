"""环境音（SFX）：资源库读取 → LLM 决策 → 合成后混入。

分层（与 BGM 链互不干涉）：

  资源层  resources/sfx/ 随仓（<具名>.mp3 + manifest.json 台账）。库内响度
          已统一：事件类峰值 -3 dBFS（瞬态单发，RMS 无意义）、氛围类 RMS
          -26 dBFS（垫底稳态）。运行时只做整体增益，不再归一。
  决策层  脚本定稿后、合成前跑一次：LLM 拿「句子清单 + closed 清单」逐句判
          （无 | 声音名 | 位置），库外名字/非法位置 fail-closed 丢弃。
          结果落 过程/<期>/sfx_plan.json——可审计、可改可重跑、期级留痕。
  执行层  合成完拿到逐句 wav 后、拼接前跑：句前/句后把短音烘焙进该句 wav
          头尾（不经过 concat 的统一停顿，节奏可控、时长增量可实测回填）；
          垫底 amix 进句体（时长不变）。audio_engine.process 一行不动——
          拼接/降噪/响度/BGM 链对 SFX 是无感的。

装饰性资产的降级纪律：开关关 = 全链零痕迹零连接；库缺/模型失败/单条缺文件
= WARN 跳过，不停产（与「缺 BGM 停产」的硬依赖不同性质）。
"""

import hashlib
import json
import os
import subprocess
import time

from . import audio_engine, config_manager
from .llm_client import LLMClient, LLMError, extract_json

# 资源库随包（与 BGM 同级）：resources/sfx/<具名>.mp3 + manifest.json
SFX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "resources", "sfx")

POSITIONS = ("before", "after", "bed")
# 重口味的「 seasoning 」：每期顶多用一两次，决策层超出配额的直接丢——
# 不设限 LLM 会把心跳塞进半本剧本。
SEASONING = ("心跳-单组", "尖叫-女", "雷-单声", "警报-蜂鸣")
SEASONING_CAP = 2

EVENT_GAP_BEFORE = 0.18   # 句前：声音在前，声音结束到开口留口气
EVENT_GAP_AFTER = 0.25    # 句后：说完留口气，声音收尾


class SfxError(RuntimeError):
    pass


# ------------------------------------------------------------------ 资源层
def load_manifest():
    """读随仓台账。库缺失返回 None（调用方 WARN 降级，不停产）。"""
    p = os.path.join(SFX_DIR, "manifest.json")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def catalog(manifest):
    """closed 清单：LLM 只能从这里选。名字 + 类型 + 场景描述 + 时长。"""
    return [{"name": e["name"], "type": e["type"], "scene": e["scene"],
             "duration": e["duration"]} for e in manifest["entries"]]


def _entry(manifest, name):
    for e in manifest["entries"]:
        if e["name"] == name:
            return e
    return None


# ------------------------------------------------------------------ 决策层
def plan_path(work):
    return os.path.join(work, "sfx_plan.json")


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "sfx": {"type": "string"},
                    "pos": {"type": "string",
                            "enum": ["before", "after", "bed"]},
                },
                "required": ["i", "sfx", "pos"],
            },
        },
    },
    "required": ["lines"],
}


def _prompt(script, cat):
    lines = "\n".join(
        "%d | %s | %s" % (i, s.get("speaker", "A"),
                          (s.get("text") or "").strip())
        for i, s in enumerate(script))
    lib = "\n".join(
        "- %s（%s，%.1f 秒）：%s" % (c["name"],
                                    "事件音/句前后点缀" if c["type"] == "event"
                                    else "氛围音/垫底",
                                    c["duration"], c["scene"])
        for c in cat)
    return (
        "你在为播讲节目安排环境音（音效）。下面是逐句脚本和可选声音的封闭清单。\n"
        "规则：\n"
        "1. 只能从清单里选，名字一字不差；绝大多数句子不需要环境音，宁缺勿滥。\n"
        "2. 事件音（短，1~6 秒）只能放句前(before)或句后(after)；氛围音只能"
        "垫底(bed)。铺垫一整段场景（雨夜/战场/集市连续多句）时，在该段第一句"
        "标 bed 即可，不要每句都标。\n"
        "3. 心跳-单组、尖叫-女、雷-单声、警报-蜂鸣 是重口味点缀，整期最多各用 "
        "2 次，能不用就不用。\n"
        "4. 只在文本明确描写或强烈暗示时才配（如「拔出刀」配金属碰撞、「摔门」"
        "配门-关）；转述、回忆、比喻不加。\n"
        "5. 输出 JSON：{\"lines\":[{\"i\":句子序号,\"sfx\":声音名,\"pos\":"
        "\"before|after|bed\"}]}，没有合适的句子就输出空数组。\n\n"
        "【可选声音清单】\n%s\n\n【逐句脚本】\n%s" % (lib, lines))


def plan_sfx(script, work, cfg, log=None):
    """决策层唯一入口。返回 plan dict 或 None（关闭/空库/失败都降级）。"""
    log = log or (lambda m: None)
    if not cfg.get("sfx.enabled", False):
        return None
    manifest = load_manifest()
    if not manifest or not manifest.get("entries"):
        log("环境音已开启但资源库缺失/为空，本期不加环境音")
        return None

    cat = catalog(manifest)
    prompt = _prompt(script, cat)
    llm = LLMClient(backend=cfg.get("llm.backend", "lm-studio"),
                    base_url=config_manager.resolve_base_url(cfg),
                    api_key=cfg.get("llm.api_key", ""),
                    model=cfg.get("llm.model", ""),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)))
    try:
        text, _meta = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            # 输出预算走统一推动点：思考段与答案段共用 max_tokens，写死小值
            # 会被思考型模型的推理段吃光（v2.7.0 真机：reasoning=4096 吃满预算）。
            max_tokens=int(cfg.get("llm.max_tokens", 8192)),
            json_schema=PLAN_SCHEMA)
        data = extract_json(text)
    except (LLMError, ValueError) as e:
        log("环境音决策失败（%s），本期不加环境音" % e)
        return None

    plan = _validate(data, script, manifest, log)
    with open(plan_path(work), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"schema": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "manifest_sha256": _manifest_sha(manifest),
                   "entries": plan["entries"]}, fh, ensure_ascii=False, indent=1)
    return plan


def _manifest_sha(manifest):
    blob = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _validate(data, script, manifest, log):
    """LLM 输出 → 合法计划。库外名字/越界/类型错位一律丢弃（fail-closed）。"""
    entries, seen_i = [], set()
    seasoning_used = {}
    for item in (data or {}).get("lines") or []:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        name = str(item.get("sfx") or "").strip()
        pos = str(item.get("pos") or "").strip()
        if not (0 <= i < len(script)):
            continue
        if i in seen_i:            # 一句最多一条，多报的丢
            continue
        e = _entry(manifest, name)
        if e is None:
            log("环境音：句 %d 提到「%s」不在资源库，已丢弃" % (i, name))
            continue
        if pos not in POSITIONS or (pos == "bed") != (e["type"] == "amb"):
            log("环境音：句 %d 的「%s」位置 %s 与类型不符，已丢弃" % (i, name, pos))
            continue
        if name in SEASONING:
            if seasoning_used.get(name, 0) >= SEASONING_CAP:
                log("环境音：「%s」超出每期 %d 次配额，已丢弃" % (name, SEASONING_CAP))
                continue
            seasoning_used[name] = seasoning_used.get(name, 0) + 1
        seen_i.add(i)
        entries.append({"i": i, "sfx": name, "pos": pos})
    return {"entries": entries}


def read_plan(work):
    p = plan_path(work)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ 执行层
def _run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise SfxError("命令失败: %s\n%s" % (" ".join(cmd[:6]) + " ...",
                                             p.stderr.decode("utf-8", "replace")[-400:]))


def _sfx_part(src, dst, sr, ch, gain_db):
    """SFX 重采样到句子同口径 + 整体增益（库内已归一，这里只压一档）。"""
    _run([audio_engine.ffmpeg_bin(), "-y", "-i", src,
          "-af", "volume=%.1fdB" % float(gain_db),
          "-ar", str(sr), "-ac", str(ch), dst])
    return dst


def _bed_mix(sentence, src, dst, sr, ch, gain_db, log):
    """垫底：SFX 循环铺满整句，压低音量混进句体。句长不变。"""
    dur = audio_engine.probe_duration_safe(sentence)
    if dur <= 0:
        log("环境音：垫底跳过（句时长未知）")
        return None
    _run([audio_engine.ffmpeg_bin(), "-y",
          "-i", sentence, "-stream_loop", "-1", "-i", src,
          "-filter_complex",
          "[1:a]atrim=0:%.3f,asetpts=PTS-STARTPTS,volume=%.1fdB[s];"
          "[0:a][s]amix=inputs=2:duration=first:normalize=0" % (dur, float(gain_db)),
          "-ar", str(sr), "-ac", str(ch), dst])
    return dst


def apply(audio_files, durations, work, plan, cfg, log=None):
    """执行层唯一入口。返回 (新 audio_files, 新 durations, 增加秒数)。

    句前/句后把 SFX 烘焙进该句 wav（时长增量实测回填，预算不漂）；
    垫底 amix 进句体（时长不变）。单条失败 WARN 跳过，不拦整期。
    """
    log = log or (lambda m: None)
    entries = (plan or {}).get("entries") or []
    if not entries:
        return audio_files, durations, 0.0
    sr = int(cfg.get("audio.sample_rate", 44100))
    ch = int(cfg.get("audio.channels", 1))
    ev_gain = float(cfg.get("sfx.event_gain_db", -6.0))
    bed_gain = float(cfg.get("sfx.bed_gain_db", -8.0))
    manifest = load_manifest() or {}
    out_dir = os.path.join(work, "sfx")
    os.makedirs(out_dir, exist_ok=True)

    files = list(audio_files)
    durs = list(durations)
    added = 0.0
    for ent in entries:
        i, name, pos = ent["i"], ent["sfx"], ent["pos"]
        e = _entry(manifest, name)
        src = os.path.join(SFX_DIR, e["file"]) if e else ""
        if not src or not os.path.isfile(src):
            log("环境音：句 %d 的「%s」文件缺失，跳过" % (i, name))
            continue
        if i >= len(files):
            continue
        part = os.path.join(out_dir, "_p%04d_%s.wav" % (i, pos))
        try:
            if pos == "bed":
                dst = os.path.join(out_dir, "%04d_bed.wav" % i)
                if _bed_mix(files[i], src, dst, sr, ch, bed_gain, log):
                    files[i] = dst          # 句长不变
                    log("环境音：句 %d 垫底「%s」" % (i, name))
            else:
                gap = EVENT_GAP_BEFORE if pos == "before" else EVENT_GAP_AFTER
                dst = os.path.join(out_dir, "%04d_%s.wav" % (i, pos))
                if pos == "before":
                    audio_engine.concat([_sfx_part(src, part, sr, ch, ev_gain),
                                         files[i]], dst, gap, sr, ch)
                else:
                    audio_engine.concat([files[i],
                                         _sfx_part(src, part, sr, ch, ev_gain)],
                                        dst, gap, sr, ch)
                new_dur = audio_engine.probe_duration_safe(dst)
                if new_dur > 0:
                    delta = max(0.0, new_dur - float(durs[i] or 0.0))
                    added += delta
                    durs[i] = new_dur
                    files[i] = dst
                    log("环境音：句 %d %s「%s」（+%.2f 秒）"
                        % (i, "句前" if pos == "before" else "句后",
                           name, delta))
        except (SfxError, audio_engine.AudioError) as ex:
            log("环境音：句 %d「%s」处理失败（%s），跳过" % (i, name, ex))
    return files, durs, round(added, 2)
