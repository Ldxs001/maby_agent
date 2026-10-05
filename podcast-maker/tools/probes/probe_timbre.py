# -*- coding: utf-8 -*-
"""音色指纹体检：用 MFCC 统计量找「这一句不像本人」的句子。

F0 只能看音高，口音差异落在频谱包络上，所以这里用 MFCC 均值+标准差当指纹，
再按说话人分簇算马氏距离，把离群句排出来。
"""
import os
import sys
import wave
import json
import numpy as np

AD, SCRIPT = sys.argv[1], sys.argv[2]
TOPN = int(sys.argv[3]) if len(sys.argv) > 3 else 15
NFFT, NMEL, NCEP = 512, 26, 13


def melbank(sr):
    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    lo, hi = hz2mel(80), hz2mel(min(7600, sr / 2 - 100))
    pts = mel2hz(np.linspace(lo, hi, NMEL + 2))
    bins = np.floor((NFFT + 1) * pts / sr).astype(int)
    fb = np.zeros((NMEL, NFFT // 2 + 1))
    for m in range(1, NMEL + 1):
        a, b, c = bins[m - 1], bins[m], bins[m + 1]
        for k in range(a, b):
            if b > a:
                fb[m - 1, k] = (k - a) / (b - a)
        for k in range(b, c):
            if c > b:
                fb[m - 1, k] = (c - k) / (c - b)
    return fb


def dct2(x):
    n = x.shape[0]
    k = np.arange(n)[:, None]
    return np.cos(np.pi * k * (2 * np.arange(n)[None, :] + 1) / (2 * n))


def read_wav(p):
    with wave.open(p, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    return a / 32768.0, sr


def mfcc_stat(a, sr, fb, D):
    win, hop = int(sr * 0.025), int(sr * 0.01)
    rows = []
    for i in range(0, max(1, a.size - win), hop):
        fr = a[i:i + win]
        if fr.size < win:
            break
        fr = fr - fr.mean()
        if np.sqrt((fr ** 2).mean()) < 0.01:      # 只统计有声帧
            continue
        sp = np.abs(np.fft.rfft(fr * np.hanning(win), n=NFFT)) ** 2
        e = fb @ sp
        e[e <= 0] = 1e-10
        c = D @ np.log(e)
        rows.append(c[:NCEP])
    if len(rows) < 5:
        return None
    M = np.array(rows)
    return np.concatenate([M.mean(axis=0), M.std(axis=0)])


def main():
    lines = json.load(open(SCRIPT, encoding="utf-8"))
    if isinstance(lines, dict):
        lines = lines.get("lines", [])
    files = sorted(f for f in os.listdir(AD) if f.endswith(".wav"))
    _, sr0 = read_wav(os.path.join(AD, files[0]))
    fb, D = melbank(sr0), dct2(np.zeros(NMEL))
    feats, meta = [], []
    for i, f in enumerate(files):
        a, sr = read_wav(os.path.join(AD, f))
        v = mfcc_stat(a, sr, fb, D)
        if v is None:
            continue
        feats.append(v)
        meta.append((i, f, f.rsplit("_", 1)[-1].replace(".wav", ""),
                     lines[i].get("text", "") if i < len(lines) else "",
                     lines[i].get("emotion", "") if i < len(lines) else ""))
    X = np.array(feats)
    print("读入 %d 句，指纹维度 %d" % (X.shape[0], X.shape[1]))

    for who, label in (("A", "Vivian"), ("B", "Serena")):
        idx = [k for k, m in enumerate(meta) if m[2] == who]
        if len(idx) < 10:
            continue
        sub = X[idx]
        mu = sub.mean(axis=0)
        sd = sub.std(axis=0) + 1e-8
        Z = (sub - mu) / sd
        d = np.sqrt((Z ** 2).mean(axis=1))       # 归一化欧氏距离
        order = np.argsort(-d)
        print()
        print("== %s 角（%s）%d 句 · 指纹距簇心 ==" % (who, label, len(idx)))
        print("   全体：中位 %.2f  最大 %.2f" % (np.median(d), d.max()))
        for k in order[:TOPN]:
            i, f, w, t, e = meta[idx[k]]
            print("      #%03d %-12s 距离 %.2f [%s] %s" % (i, f, d[k], e, t[:34]))


if __name__ == "__main__":
    main()
