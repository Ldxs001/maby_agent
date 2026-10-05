#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Base 变体情绪指令效力实测 —— ICL 与 xvec 两种模式各试三种指令。

起因：库源码里有一条警告（model.py L510）：

    Base-model instruct with x-vector-only voice cloning is experimental.
    Upstream Qwen3-TTS itself does not follow instructions reliably in this
    mode. Prefer xvec_only=False (ICL mode) when using instruct.

即「只喂 2048 维向量」这条路上，情绪指令官方自己承认不可靠；要用指令得走
ICL（参考音频整段进上下文）。本脚本量这句话到底有多真。

矩阵：2 模式（icl / xvec）× 3 指令（无 / 轻松 / 感慨）× 4 句 = 24 次合成。

判据：同一模式下，三种指令的 F0 中位数与句内波动若没有系统性差异，说明指令
在那一档没被听进去。ICL 与 xvec 的时长差同时作为参考音频渗漏（bleed-through）
的代理指标 —— 参考音频整段进上下文时，多出来的上下文可能带出额外音节。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tts_service"))

import serve  # noqa: E402  产品真模块，不改

OUT = os.path.join(ROOT, "_smoke", "_instruct_probe")
AUDIO = os.path.join(OUT, "audio")
REFGEN = os.path.join(ROOT, "_smoke", "_refgen")
MODEL_BASE = os.path.join(ROOT, "tts_service", "models",
                          "Qwen3-TTS-12Hz-1.7B-Base")

SEED = 12345
REF = {"A": "A_c2", "B": "B_c2"}

LINES = [
    "今天的天气比昨天凉了一些，出门记得加件外套。",
    "这条路径的选择并不复杂，关键是先看清成本。",
    "数据摆在这里，接下来要做的是把它讲清楚。",
    "我们先把基础事实对齐，再讨论后面的方案。",
    "从长期来看，这件事的影响会慢慢显现出来。",
    "关于这个问题，我的看法和你有些不太一样。",
    "会议纪要已经整理完毕，稍后会发到各位邮箱。",
    "这个方案的核心风险在于交付周期太短。",
    "我们需要在两周之内完成第一轮的验证工作。",
    "把复杂的问题拆开看，往往会清楚很多。",
    "这份报告的重点不在结论，而在推理过程。",
    "无论选择哪条路，都要先想清楚代价是什么。",
]

# (标签, 情绪词) —— 情绪词取自产品的 INSTRUCT_EMOTIONS
CONDS = [("none", None), ("light", "轻松"), ("sigh", "感慨")]


def f0_stats(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512):
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    x = x - x.mean()
    p = np.max(np.abs(x))
    if p <= 1e-9:
        return float("nan"), float("nan")
    x = x / p
    lo, hi = max(1, int(sr / fmax)), min(frame - 1, int(sr / fmin))
    vals = []
    for s in range(0, len(x) - frame, hop):
        f = x[s:s + frame]
        if np.sqrt(np.mean(f ** 2)) < 0.02:
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
    if not vals:
        return float("nan"), float("nan")
    return float(np.median(vals)), float(np.std(vals))


def main() -> int:
    os.makedirs(AUDIO, exist_ok=True)

    with io.open(os.path.join(REFGEN, "ref.json"), encoding="utf-8") as fh:
        refs = json.load(fh)

    from faster_qwen3_tts import FasterQwen3TTS  # type: ignore

    print("=" * 78)
    print("加载 Base 变体")
    print("=" * 78)
    t0 = time.time()
    model = FasterQwen3TTS.from_pretrained(MODEL_BASE, device="cuda")
    print("  加载 %.1fs" % (time.time() - t0))

    rows = []
    for mode in ("icl", "xvec"):
        xvec = (mode == "xvec")
        for tag, emo in CONDS:
            ins = serve.build_instruct(emo, 1.0, None) if emo else None
            for spk in ("A",):
                ref = next(r for r in refs[spk] if r["id"] == REF[spk])
                ref_audio = os.path.join(REFGEN, ref["wav"])
                for i, text in enumerate(LINES, 1):
                    serve.set_seed(SEED)
                    out = model.generate_voice_clone(
                        text=text, language="Chinese",
                        ref_audio=ref_audio, ref_text=ref["text"],
                        xvec_only=xvec, instruct=ins,
                        max_new_tokens=serve.max_frames_for(text),
                        **serve.SAMPLE_KWARGS)
                    samples, sr = serve._split_out(out)
                    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
                    dur = len(audio) / float(sr)
                    med, std = f0_stats(audio, sr)
                    name = "%s_%s_%d.wav" % (mode, tag, i)
                    with open(os.path.join(AUDIO, name), "wb") as fh:
                        fh.write(serve.to_wav_bytes(samples, sr))
                    rec = {"mode": mode, "cond": tag, "instruct": ins,
                           "speaker": spk, "line": i, "text": text,
                           "wav": name, "dur": round(dur, 3),
                           "cps": round(len(text) / dur, 2) if dur else float("nan"),
                           "f0_med": round(med, 1), "f0_std": round(std, 1)}
                    rows.append(rec)
                    print("  %-5s %-6s 第%d句 %5.2fs %5.2f字/秒 F0 %6.1f 波动 %5.1f  %s"
                          % (mode, tag, i, dur, rec["cps"], med, std, ins or "-"))

    with io.open(os.path.join(OUT, "instruct.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)

    print()
    print("=" * 78)
    print("合议：同模式内三种指令的 F0 中位差（越大说明指令越被听进去）")
    print("=" * 78)
    for mode in ("icl", "xvec"):
        for tag in ("none", "light", "sigh"):
            v = [r["f0_med"] for r in rows
                 if r["mode"] == mode and r["cond"] == tag and r["f0_med"] == r["f0_med"]]
            if v:
                a = np.asarray(v, dtype=float)
                print("  %-5s %-6s  F0中位 均值 %6.1f  范围 %6.1f ~ %6.1f"
                      % (mode, tag, a.mean(), a.min(), a.max()))
        icl = {r["cond"]: r["dur"] for r in rows if r["mode"] == mode}
        print("  %-5s 时长合计 %.2fs" % (mode, sum(icl.values())))
        print("-" * 78)

    print("产物 %s" % os.path.join(OUT, "instruct.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
