# -*- coding: utf-8 -*-
"""复现实验：固定音色、固定文本，反复合成，量它的抖动量。

问题：用户听到「每句都像不同的人在说话，还冒出一句四川口音」。
若说话人条件真的生效，同一 speaker 的多次采样应当只抖一点点；
若抖成大范围，说明音色没被钉住（采样温度过高 / 条件被稀释）。

用 faster_qwen3_tts 直接调，参数与服务端一致：
    temperature=0.9, top_k=50, top_p=1.0, do_sample=True, repetition_penalty=1.05
"""
import os
import sys
import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
TEXT = "大家好，欢迎收听本期节目，今天我们来聊一个有意思的话题。"


def f0_median(a, sr):
    """自相关法求中位基频（只看有声帧）。"""
    a = np.asarray(a, dtype=np.float64).reshape(-1)
    if a.size < sr // 20:
        return 0.0
    # 8k 降采样足够测基频
    step = max(1, int(sr // 8000))
    a = a[::step]
    fs = sr / step
    win = int(fs * 0.04)
    hop = int(fs * 0.02)
    lo, hi = int(fs / 400), int(fs / 60)   # 60–400 Hz
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


def main():
    from faster_qwen3_tts import FasterQwen3TTS
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("加载模型 … device=%s" % dev, flush=True)
    m = FasterQwen3TTS.from_pretrained(MODEL, device=dev)
    print("就绪\n", flush=True)

    def run(speaker, temp, n, tag):
        print("── %s · speaker=%s · temperature=%s · %d 次 ──" % (tag, speaker, temp, n), flush=True)
        rows = []
        for k in range(n):
            wavs, sr = m.generate_custom_voice(
                text=TEXT, speaker=speaker, language="Chinese",
                temperature=temp, top_k=50, top_p=1.0, do_sample=True,
                repetition_penalty=1.05)
            a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
            if hasattr(a, "cpu"):
                a = a.cpu().numpy()
            f = f0_median(a, sr)
            c = centroid(a, sr)
            d = len(np.asarray(a).reshape(-1)) / float(sr)
            rows.append((f, c, d))
            print("   第%d次  F0=%6.1fHz  质心=%6.1fHz  时长=%.2fs" % (k + 1, f, c, d), flush=True)
        fs = [r[0] for r in rows if r[0] > 0]
        if fs:
            print("   → F0 中位 %.1f Hz  极差 %.1f Hz  标准差 %.1f Hz" % (
                float(np.median(fs)), max(fs) - min(fs), float(np.std(fs))), flush=True)
        print(flush=True)

    run("Serena", 0.9, 6, "服务端现行参数")
    run("Serena", 0.2, 6, "降温对照")
    run("Vivian", 0.9, 3, "另一音色对照")
    print("完成", flush=True)


if __name__ == "__main__":
    main()
