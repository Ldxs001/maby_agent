# -*- coding: utf-8 -*-
"""温度定档 + 种子可行性实验（只测，不改生产代码）。

两个待决问题：
  1. temperature 从 0.9 降到 0.4 / 0.2，「跨句音色一致性」各改善多少？
     用户听到的「每句像不同的人」就是跨句抖动，得拿不同句子量，不能只重复同一句。
  2. torch.manual_seed 能否把 (text, speaker) 的结果钉成逐样本一致？
     若能，重复性由种子负责，温度只需要负责「别抖」；若不能，只能靠降温。

参数与生产一致：top_k=50, top_p=1.0, do_sample=True, repetition_penalty=1.05。
"""
import os
import sys

import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
SPEAKER = "Vivian"

# 八句，长短与内容都不同 —— 测的是「同一角色念不同句子时像不像同一个人」
TEXTS = [
    "大家好，欢迎收听本期节目。",
    "今天我们来聊一个有点意思的话题：为什么有些决定，明明想清楚了，事后还是会后悔？",
    "先看一个例子。",
    "有位朋友跟我说，他花了三个月对比了十几款笔记软件，最后哪个都没用起来。",
    "这背后其实是同一个机制在起作用，我们把它叫做「决策消耗」。",
    "每一次选择都要占用一点注意力，选得越多，留给真正做事的就越少。",
    "所以有时候，把选项砍到三个以内，比再多研究两周都管用。",
    "好，今天就聊到这里，我们下期再见。",
]


def f0_median(a, sr):
    """自相关法求中位基频（只看有声帧）。"""
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


def stat(vals, label):
    v = [x for x in vals if x > 0]
    if not v:
        return "%s: 无有效值" % label
    return "%s 中位 %.1f 极差 %.1f 标准差 %.1f" % (
        label, float(np.median(v)), max(v) - min(v), float(np.std(v)))


def main():
    from faster_qwen3_tts import FasterQwen3TTS

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("加载模型 … device=%s" % dev, flush=True)
    m = FasterQwen3TTS.from_pretrained(MODEL, device=dev)
    print("就绪\n", flush=True)

    def gen(text, temp, seed=None):
        if seed is not None:
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        wavs, sr = m.generate_custom_voice(
            text=text, speaker=SPEAKER, language="Chinese",
            temperature=temp, top_k=50, top_p=1.0, do_sample=True,
            repetition_penalty=1.05)
        a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
        if hasattr(a, "cpu"):
            a = a.cpu().numpy()
        return np.asarray(a, dtype=np.float32).reshape(-1), sr

    print("预热一次（CUDA 图首次捕获，结果不作数）…", flush=True)
    gen(TEXTS[0], 0.4)
    print()

    # ── 一、种子能否钉住结果 ─────────────────────────────────────────────
    print("═══ 一、种子可行性（同句、temp=0.4、seed=20260914）═══", flush=True)
    outs = [gen(TEXTS[1], 0.4, seed=20260914) for _ in range(3)]
    base, sr = outs[0]
    lens = [len(o[0]) for o in outs]
    print("   三次样本数: %s" % lens, flush=True)
    if len(set(lens)) == 1:
        diffs = [float(np.max(np.abs(o[0] - base))) for o in outs[1:]]
        print("   与首次的逐样本最大差: %s" % ["%.6g" % d for d in diffs], flush=True)
        same = all(d < 1e-6 for d in diffs)
        print("   → 判定: %s" % ("逐样本完全一致，种子有效" if same
                                else "仍有差异，种子不能完全钉住（%.4f 量级）"
                                     % max(diffs)), flush=True)
    else:
        print("   → 判定: 长度都不同，种子无效", flush=True)
    print(flush=True)

    # ── 二、温度对「同句重复性」的影响 ───────────────────────────────────
    print("═══ 二、同句重复性（3 次，不设种子）═══", flush=True)
    for temp in (0.4, 0.2):
        f0s = [f0_median(*gen(TEXTS[1], temp)) for _ in range(3)]
        print("   temp=%.1f  %s" % (temp, stat(f0s, "F0")), flush=True)
    print(flush=True)

    # ── 三、温度对「跨句一致性」的影响（这才是用户听到的）───────────────
    print("═══ 三、跨句一致性（8 句不同文本，各跑一次）═══", flush=True)
    for temp in (0.9, 0.4, 0.2):
        f0s, cts = [], []
        for t in TEXTS:
            a, sr = gen(t, temp)
            f0s.append(f0_median(a, sr))
            cts.append(centroid(a, sr))
        print("   temp=%.1f  %s" % (temp, stat(f0s, "F0")), flush=True)
        print("             %s" % stat(cts, "质心"), flush=True)
    print("\n完成", flush=True)


if __name__ == "__main__":
    main()
