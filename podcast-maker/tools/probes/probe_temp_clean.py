# -*- coding: utf-8 -*-
"""温度定档（干净版）：固定文本，换种子，量「同一个人能抖多远」。

为什么要固定文本：上一版拿 8 个不同句子比，测出来的差异大半来自句子内容本身
（不同字、不同标点，韵律本来就不同），把温度的效应盖掉了。这里锁死一句话，
只换随机种子 —— 相当于「同一个人用不同的随机路径念同一句话」，抖动就只剩温度
与采样这两件事。

生产里的真实形态是：每句按 (文本, 音色) 派生种子。不同句子种子不同，所以
「跨句听起来像不像同一个人」取决于温度下采样分布有多宽，正是这里在量的。
"""
import os
import sys

import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
SPEAKER = "Vivian"
TEXT = ("今天我们来聊一个有点意思的话题：为什么有些决定，"
        "明明想清楚了，事后还是会后悔？")
TEMPS = (0.9, 0.5, 0.4, 0.3, 0.2)
N_SEED = 6


def f0_median(a, sr):
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    if a.size < sr // 20:
        return 0.0
    step = max(1, int(sr // 8000))
    a = a[::step]
    fs = sr / step
    win, hop = int(fs * 0.04), int(fs * 0.02)
    lo, hi = int(fs / 400), int(fs / 60)
    vals = []
    for i in range(0, max(1, a.size - win), hop):
        fr = a[i:i + win]
        if fr.size < win:
            break
        fr = fr - fr.mean()
        if np.sqrt((fr ** 2).mean()) < 0.01:
            continue
        c = np.correlate(fr, fr, "full")[win - 1:]
        seg = c[lo:hi]
        if not seg.size:
            continue
        k = int(np.argmax(seg)) + lo
        if c[0] > 0 and c[k] / c[0] > 0.3:
            vals.append(fs / k)
    return float(np.median(vals)) if vals else 0.0


def centroid(a, sr):
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    if a.size < 256:
        return 0.0
    step = max(1, int(sr // 16000))
    a = a[::step]
    fs = sr / step
    win = 512
    acc = []
    for i in range(0, max(1, a.size - win), win):
        fr = a[i:i + win]
        if fr.size < win:
            break
        sp = np.abs(np.fft.rfft(fr * np.hanning(win)))
        f = np.fft.rfftfreq(win, 1.0 / fs)
        s = sp.sum()
        if s > 1e-6:
            acc.append(float((f * sp).sum() / s))
    return float(np.median(acc)) if acc else 0.0


def spread(vals):
    v = [x for x in vals if x > 0]
    if len(v) < 2:
        return "n/a"
    return "中位 %6.1f  极差 %6.1f  标准差 %5.1f" % (
        float(np.median(v)), max(v) - min(v), float(np.std(v)))


def main():
    from faster_qwen3_tts import FasterQwen3TTS

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("加载模型 … device=%s" % dev, flush=True)
    m = FasterQwen3TTS.from_pretrained(MODEL, device=dev)
    print("就绪\n", flush=True)

    def gen(temp, seed):
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        wavs, sr = m.generate_custom_voice(
            text=TEXT, speaker=SPEAKER, language="Chinese",
            temperature=temp, top_k=50, top_p=1.0, do_sample=True,
            repetition_penalty=1.05)
        a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
        if hasattr(a, "cpu"):
            a = a.cpu().numpy()
        return np.asarray(a, dtype=np.float32).reshape(-1), sr

    print("预热一次（CUDA 图首次捕获，不作数）…\n", flush=True)
    gen(0.4, 1)

    print("固定文本 × %d 个种子 × 各温度（音色漂移只由温度与采样决定）" % N_SEED,
          flush=True)
    print("%-7s %-34s %s" % ("温度", "F0", "音色质心"), flush=True)
    for temp in TEMPS:
        f0s, cts = [], []
        for s in range(1, N_SEED + 1):
            a, sr = gen(temp, s * 1000 + 7)
            f0s.append(f0_median(a, sr))
            cts.append(centroid(a, sr))
        print("%-7.1f %-34s %s" % (temp, spread(f0s), spread(cts)), flush=True)
    print("\n完成", flush=True)


if __name__ == "__main__":
    main()
