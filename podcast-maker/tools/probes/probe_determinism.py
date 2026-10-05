#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考音频的跨项目可复现性实证。

三个问题，逐一用波形说话：

  Q1 同文本 + 同音色 + 同种子，重复生成 → 是否逐字节相同？
  Q2 换「项目」（换个无关前缀 / 换目录 / 换服务实例）→ 是否影响结果？
  Q3 文本变了（换一句参考文案）→ 种子变 → 输出差多少？

产出：
  _smoke/_determinism/det.json   全部记录
  _smoke/_determinism/*.wav      每条波形，供人耳复核
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tts_service"))

import serve  # noqa: E402

OUT = os.path.join(ROOT, "_smoke", "_determinism")
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
VOICE = "Vivian"

# 同一句参考文案（跨项目复用的那种）
TEXT_MAIN = "这条路径的选择并不复杂，关键是先看清成本再决定。"
# 换一句文案 —— 模拟「每个项目自己写参考文本」
TEXT_ALT = "我们需要先把基础的事实对齐，再讨论后面的方案。"


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def f0_med(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512) -> float:
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


def spectral_centroid(x, sr) -> float:
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    n = 1024
    hop = 512
    win = np.hanning(n)
    acc, wsum = 0.0, 0.0
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    for s in range(0, max(1, len(x) - n), hop):
        seg = x[s:s + n]
        if len(seg) < n:
            break
        if np.sqrt(np.mean(seg ** 2)) < 0.01:
            continue
        mag = np.abs(np.fft.rfft(seg * win))
        acc += float(np.sum(freqs * mag))
        wsum += float(np.sum(mag))
    return acc / wsum if wsum else float("nan")


def gen(eng, text: str, speaker_tag: str, seed: int) -> tuple:
    serve.set_seed(seed)
    samples, sr = eng.synth_one(text, VOICE, instruct=None,
                                language="Chinese", seed=seed)
    wav = serve.to_wav_bytes(samples, sr)
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    return wav, audio, sr


def main() -> int:
    os.makedirs(OUT, exist_ok=True)

    print("=" * 78)
    print("加载 CustomVoice（作为「音色源头」的产出方）")
    print("=" * 78)
    eng = serve.Engine(MODEL, "auto", lambda m: print("  " + m))
    t0 = time.time()
    eng.ensure()
    print("  后端 %s · 设备 %s · 加载 %.1fs"
          % (eng.backend, eng.device, time.time() - t0))
    print()

    rows = []

    def run(tag: str, text: str, spk_tag: str, note: str) -> dict:
        seed = serve.seed_for(text, spk_tag)
        t1 = time.time()
        wav, audio, sr = gen(eng, text, spk_tag, seed)
        name = "%s.wav" % tag
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(wav)
        rec = {
            "tag": tag,
            "text": text,
            "spk_tag": spk_tag,
            "seed": seed,
            "md5": md5(wav),
            "bytes": len(wav),
            "dur": round(len(audio) / float(sr), 4),
            "f0_med": round(f0_med(audio, sr), 2),
            "centroid": round(spectral_centroid(audio, sr), 1),
            "secs": round(time.time() - t1, 2),
            "note": note,
            "_audio": audio,
        }
        rows.append(rec)
        print("  %-14s seed %-11d %7d B  md5 %s  %5.2fs  F0 %6.1f  %s"
              % (tag, seed, len(wav), rec["md5"][:12], rec["dur"],
                 rec["f0_med"], note))
        return rec

    print("-" * 78)
    print("Q1  同文本 + 同音色 + 同种子，重复生成三次（跨进程内三次调用）")
    print("-" * 78)
    for i in (1, 2, 3):
        run("q1_run%d" % i, TEXT_MAIN, "A", "第 %d 次生成" % i)

    print()
    print("-" * 78)
    print("Q2  换「项目身份」：同音色、同文本，但派生种子的 tag 换成产品音色名")
    print("     （模拟另一个项目用不同写法调用 → 种子不同 → 结果不同）")
    print("-" * 78)
    run("q2_vivian_tag", TEXT_MAIN, "Vivian", "spk_tag 用 Vivian")

    print()
    print("-" * 78)
    print("Q3  换参考文案：同音色，文本换成另一句")
    print("-" * 78)
    run("q3_alt_text", TEXT_ALT, "A", "换成另一句文案")

    # ---- 逐样本差异 ----
    print()
    print("=" * 78)
    print("逐样本比对（以 q1_run1 为基准）")
    print("=" * 78)
    base = rows[0]["_audio"]
    print("  %-14s %-10s %14s %14s %s"
          % ("对比项", "长度差", "最大绝对差", "均方根差", "判定"))
    for r in rows[1:]:
        a = r["_audio"]
        n = min(len(base), len(a))
        d = base[:n] - a[:n]
        mad = float(np.max(np.abs(d))) if n else float("nan")
        rms = float(np.sqrt(np.mean(d ** 2))) if n else float("nan")
        same = "逐字节相同" if r["md5"] == rows[0]["md5"] else (
            "波形完全相同" if mad == 0.0 else "有差异")
        r["max_abs_diff"] = mad
        r["rms_diff"] = rms
        print("  %-14s %+10d %14.3e %14.3e  %s"
              % (r["tag"], len(a) - len(base), mad, rms, same))

    clean = []
    for r in rows:
        c = dict(r)
        c.pop("_audio", None)
        clean.append(c)
    with io.open(os.path.join(OUT, "det.json"), "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=1)

    print()
    print("产出 %s" % OUT)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
