#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 Base 变体生成说话人参考音频候选 —— 音色克隆的源头。

Base 变体没有内置音色表：音色来自 speaker_encoder（ECAPA-TDNN）从一段参考
波形里抽出的 2048 维向量。这段波形就是「音色的源头」，它的质量决定后面整期
播客所有句子的音色。挑选标准：

  - 单说话人、无背景音、无混响
  - 5-8 秒、语速平稳
  - 不带情绪指令（这里要的是音色，不是表演）

产出：
  _smoke/_refgen/<SPK>_c<n>.wav   候选波形
  _smoke/_refgen/ref.json         含转录文本（ICL 模式要 ref_text）
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

OUT = os.path.join(ROOT, "_smoke", "_refgen")
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")

VOICE = {"A": "Vivian", "B": "Serena"}

CANDIDATES: dict[str, list[str]] = {
    "A": [
        "今天的讨论到这里告一段落，我们下次接着聊这个话题。",
        "这条路径的选择并不复杂，关键是先看清成本再决定。",
        "数据摆在这里，接下来要做的是把它讲清楚。",
    ],
    "B": [
        "关于这个问题，我的看法可能和你有些不太一样。",
        "我们需要先把基础的事实对齐，再讨论后面的方案。",
        "从长期来看，这件事的影响会慢慢显现出来。",
    ],
}


def f0_med(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512) -> float:
    """自相关法取 F0 中位数 —— 只为快速看音高落在哪个区间。"""
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    x = x - x.mean()
    p = np.max(np.abs(x))
    if p <= 1e-9:
        return float("nan")
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
    return float(np.median(vals)) if vals else float("nan")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)

    print("=" * 78)
    print("加载 CustomVoice —— 只用来出参考音频，一次性动作")
    print("=" * 78)
    eng = serve.Engine(MODEL, "auto", lambda m: print("  " + m))
    t0 = time.time()
    eng.ensure()
    print("  后端 %s · 设备 %s · 加载 %.1fs"
          % (eng.backend, eng.device, time.time() - t0))

    refs: dict[str, list[dict]] = {}
    for spk in ("A", "B"):
        refs[spk] = []
        voice = VOICE[spk]
        for n, text in enumerate(CANDIDATES[spk], 1):
            seed = serve.seed_for(text, spk)
            t1 = time.time()
            samples, sr = eng.synth_one(text, voice, instruct=None,
                                        language="Chinese", seed=seed)
            wav = serve.to_wav_bytes(samples, sr)
            name = "%s_c%d.wav" % (spk, n)
            with open(os.path.join(OUT, name), "wb") as fh:
                fh.write(wav)
            audio = np.asarray(samples, dtype=np.float32).reshape(-1)
            dur = len(audio) / float(sr)
            rec = {
                "id": "%s_c%d" % (spk, n),
                "speaker": spk,
                "voice": voice,
                "text": text,
                "wav": name,
                "dur": round(dur, 3),
                "cps": round(len(text) / dur, 2) if dur else float("nan"),
                "f0_med": round(f0_med(audio, sr), 1),
                "sr": int(sr),
                "seed": seed,
                "bytes": len(wav),
                "secs": round(time.time() - t1, 2),
            }
            refs[spk].append(rec)
            print("  %-6s %-7s %5.2fs %5.2f 字/秒  F0中位 %6.1f  %s"
                  % (rec["id"], voice, dur, rec["cps"], rec["f0_med"], text))

    with io.open(os.path.join(OUT, "ref.json"), "w", encoding="utf-8") as fh:
        json.dump(refs, fh, ensure_ascii=False, indent=1)

    print()
    print("=" * 78)
    print("产出 %s" % OUT)
    for spk in ("A", "B"):
        print("  说话人 %s（%s）%d 条候选" % (spk, VOICE[spk], len(refs[spk])))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
