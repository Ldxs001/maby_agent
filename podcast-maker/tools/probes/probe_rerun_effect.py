# -*- coding: utf-8 -*-
"""重跑前哨：拿本期真实脚本的句子，走产品真实合成链路，新旧两态对照。

验证两件事：
  1. 真情绪句（「平静」）—— 旧=发「用平静的语气说」，新=不发 → 音频应当不同
  2. 语篇标签句（追问/解释…）—— 新旧都不发指令 → 参数完全一致
     （种子只吃 音色+文本，与 emotion 无关）→ 音频应当逐字节相同

不重写任何合成逻辑：直接调 podcast_maker.tts_engine.synth_line，
就是 synthesize() 内部逐句用的那个函数。
"""
import hashlib
import io
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from podcast_maker import tts_engine                      # noqa: E402
from podcast_maker.config_manager import ConfigManager    # noqa: E402

OUT = os.path.join(ROOT, "_smoke", "_rerun_probe")
SCRIPT = os.path.join(ROOT, "projects/20260915-103249/脚本/1.json")
LEVEL = "none"          # 项目范式卡 methodology 的档位，实测取到


def f0_median(samples, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512):
    """自相关法估基频，返回有声帧的 F0 中位数（Hz）。"""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    # 去直流 + 归一化
    x = x - x.mean()
    peak = np.max(np.abs(x))
    if peak <= 1e-9:
        return float("nan")
    x = x / peak

    lo = max(1, int(sr / fmax))
    hi = min(frame - 1, int(sr / fmin))
    vals = []
    for s in range(0, len(x) - frame, hop):
        f = x[s:s + frame]
        if np.sqrt(np.mean(f ** 2)) < 0.02:      # 静音帧跳过
            continue
        ac = np.correlate(f, f, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if not len(seg):
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] / ac[0] < 0.3:                   # 自相关峰太弱视为无基频
            continue
        vals.append(sr / k)
    if not vals:
        return float("nan")
    return float(np.median(vals))


def read_wav(path):
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch)
    return a, sr


def main():
    os.makedirs(OUT, exist_ok=True)
    cfg = ConfigManager()

    with io.open(SCRIPT, encoding="utf-8") as f:
        script = json.load(f)

    # 挑样本：2 句真情绪（平静）+ 2 句语篇标签，尽量覆盖两个音色
    real = [s for s in script if s.get("emotion") == "平静"]
    tag = [s for s in script if s.get("emotion") in
           ("追问", "解释", "比喻", "过渡", "强调")]
    picks = []
    for pool, label in ((real, "真情绪-平静"), (tag, "语篇标签")):
        seen = set()
        for s in pool:
            if s.get("speaker") in seen:
                continue
            seen.add(s.get("speaker"))
            picks.append((label, s))
            if len(seen) >= 2:
                break

    va = cfg.get("tts.qwen3tts_voice_a", "")
    vb = cfg.get("tts.qwen3tts_voice_b", "")

    print("=" * 72)
    print("采样句（来自 projects/20260915-103249/脚本/1.json）")
    print("=" * 72)
    for label, s in picks:
        print("  [%s] speaker=%s emotion=%s  %s"
              % (label, s.get("speaker"), s.get("emotion"), s.get("text")[:28]))

    # 本地引擎独占显存，先按产品行为把语言模型请下去
    if bool(cfg.get("tts.unload_llm_before_synth", True)):
        print("\n[准备] 按产品行为卸载驻留语言模型…")
        tts_engine.unload_llm_models(lambda m: print("   " + m))

    print()
    print("=" * 72)
    print("合成对照（旧 = 不传档位 / 新 = 档位 none）")
    print("=" * 72)

    rows = []
    for label, s in picks:
        speaker = s.get("speaker", "A")
        voice = va if speaker == "A" else vb
        text = s.get("text", "")
        emo = s.get("emotion", "")
        speed = float(cfg.get("tts.speed_a" if speaker == "A" else "tts.speed_b", 1.0))

        got = {}
        for tag_name, deg in (("old", None), ("new", LEVEL)):
            data = tts_engine.synth_line(text, voice, speed, cfg,
                                         emotion=emo, degree=deg)
            path = os.path.join(OUT, "%s_%s_%s.wav"
                                % (label.replace("-", "_"), tag_name, speaker))
            with open(path, "wb") as f:
                f.write(data)
            a, sr = read_wav(path)
            got[tag_name] = {
                "path": path,
                "bytes": len(data),
                "sha": hashlib.sha256(data).hexdigest()[:16],
                "dur": round(len(a) / float(sr), 3),
                "f0": f0_median(a, sr),
            }
            print("  %-10s %-4s %-3s" % (label, tag_name, speaker), end="")
            print("  时长 %5.2fs  字节 %7d  sha %s  F0 %6.1f Hz"
                  % (got[tag_name]["dur"], got[tag_name]["bytes"],
                     got[tag_name]["sha"], got[tag_name]["f0"]))

        same = got["old"]["sha"] == got["new"]["sha"]
        df0 = (got["new"]["f0"] - got["old"]["f0"])
        pct = (df0 / got["old"]["f0"] * 100.0) if got["old"]["f0"] else float("nan")
        rows.append({
            "label": label, "speaker": speaker, "emotion": emo,
            "text": text, "voice": voice,
            "same": same,
            "old": got["old"], "new": got["new"],
            "f0_delta_hz": round(df0, 1), "f0_delta_pct": round(pct, 1),
        })
        print("      → 新旧%s，F0 %+.1f Hz (%+.1f%%)"
              % ("逐字节相同" if same else "不同", df0, pct))
        print()

    with io.open(os.path.join(ROOT, "_smoke", "_rerun_probe.json"), "w",
                 encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)

    print("=" * 72)
    print("小结")
    print("=" * 72)
    for r in rows:
        print("  %-10s %-4s emotion=%-4s → %s"
              % (r["label"], r["speaker"], r["emotion"],
                 "无变化（本来就无指令）" if r["same"] else
                 "变化 F0 %+.1f%%" % r["f0_delta_pct"]))
    return rows


if __name__ == "__main__":
    main()
