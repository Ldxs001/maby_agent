# -*- coding: utf-8 -*-
"""产物体检：391 句逐条量基频与音色，找离群句。

只做测量，不改任何东西。判据：
  - 同一说话人（A=Vivian / B=Serena）的 F0 应各自聚成两簇；
  - 若某句 F0 远低于本簇中心，或质心/带宽明显偏离，就是「那一句不像本人」。
"""
import os
import sys
import wave
import json
import numpy as np

AD = sys.argv[1]
SCRIPT = sys.argv[2]
TOPN = int(sys.argv[3]) if len(sys.argv) > 3 else 15


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
        if w.getnchannels() > 1:
            a = a.reshape(-1, w.getnchannels()).mean(axis=1)
    return a / 32768.0, sr


def f0_median(a, sr):
    step = max(1, sr // 8000)
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
        if c[0] > 0 and c[k] / c[0] > 0.35:
            vals.append(fs / k)
    return float(np.median(vals)) if vals else 0.0


def centroid(a, sr):
    step = max(1, sr // 16000)
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
    lines = json.load(open(SCRIPT, encoding="utf-8"))
    if isinstance(lines, dict):
        lines = lines.get("lines", [])
    files = sorted(f for f in os.listdir(AD) if f.endswith(".wav"))
    rows = []
    for i, f in enumerate(files):
        p = os.path.join(AD, f)
        try:
            a, sr = read_wav(p)
        except Exception as e:
            print("读不了 %s: %s" % (f, e))
            continue
        rows.append({
            "i": i, "file": f,
            "who": f.rsplit("_", 1)[-1].replace(".wav", ""),
            "f0": f0_median(a, sr),
            "cen": centroid(a, sr),
            "dur": len(a) / float(sr),
            "text": (lines[i].get("text", "") if i < len(lines) else ""),
            "emo": (lines[i].get("emotion", "") if i < len(lines) else ""),
        })

    print("读入 %d 个 wav" % len(rows))
    for who in ("A", "B"):
        sub = [r for r in rows if r["who"] == who and r["f0"] > 0]
        if not sub:
            continue
        fs = np.array([r["f0"] for r in sub])
        cs = np.array([r["cen"] for r in sub])
        med = float(np.median(fs))
        mad = float(np.median(np.abs(fs - med))) or 1.0
        print()
        print("== %s 角（%s）：%d 句 ==" % (
            who, "Vivian" if who == "A" else "Serena", len(sub)))
        print("   F0  中位 %.1f  极差 %.1f  标准差 %.1f  (%.1f–%.1f)" % (
            med, fs.max() - fs.min(), fs.std(), fs.min(), fs.max()))
        print("   质心 中位 %.0f  极差 %.0f" % (np.median(cs), cs.max() - cs.min()))
        print("   本簇离群（|F0-中位| > 3×MAD 且偏离 > 25Hz）:")
        out = [r for r in sub if abs(r["f0"] - med) > max(3 * mad, 25)]
        out.sort(key=lambda r: abs(r["f0"] - med), reverse=True)
        for r in out[:TOPN]:
            print("      #%03d %-12s F0=%6.1f (偏%+6.1f) 质心=%5.0f %3.1fs [%s] %s" % (
                r["i"], r["file"], r["f0"], r["f0"] - med, r["cen"], r["dur"],
                r["emo"], r["text"][:30]))
        if not out:
            print("      （无）")


if __name__ == "__main__":
    main()
