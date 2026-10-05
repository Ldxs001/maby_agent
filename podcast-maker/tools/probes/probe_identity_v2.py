# -*- coding: utf-8 -*-
"""说话人同一性归因 v2（重写）：不用合成「指纹距离」，改用可解释的物理量。

v1 教训：合成出来的「音色指纹距离」自检不过关——librosa 的 pitch_shift 会把
共振峰一起搬走，位移 0.98，等于尺子把「音高变了」也算成「音色变了」。

本版改为逐项报告可直接解释的量：
    F0 中位 / P10 / P90     音高与音域
    F1 / F2 中位            共振峰 → 声道长度 → 说话人身份的核心
    谱质心中位               明亮度
    时长 / 有效语音 / 语速    节奏

产物：_smoke/_speaker_probe/identity_v2.json
"""
import io
import json
import os

import numpy as np
import librosa
import scipy.signal as sps

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIO = os.path.join(ROOT, "projects", "20260915-103249", "过程", "1", "audio")
VAR = os.path.join(ROOT, "_smoke", "_variance_probe")
OUT_DIR = os.path.join(ROOT, "_smoke", "_speaker_probe")

SR = 22050
LPC_ORDER = int(SR / 1000) + 4          # 26
F0_MIN, F0_MAX = 70.0, 500.0
FORMANT_MIN = 200.0                     # 低于此不算共振峰（排除基频残留）


def formants(y, sr=SR, order=LPC_ORDER, n_keep=3):
    """LPC 谱包络找前 n_keep 个共振峰。只取有声帧。"""
    win = int(0.025 * sr)
    hop = int(0.010 * sr)
    yp = np.append(y[0], y[1:] - 0.97 * y[:-1])      # 预加重
    hamming = np.hamming(win)
    peaks_all = []
    for s in range(0, len(yp) - win, hop):
        fr = yp[s:s + win]
        if np.sqrt(np.mean(fr ** 2)) < 0.02:
            continue
        fr = fr * hamming
        try:
            a = librosa.lpc(fr, order=order)
        except Exception:
            continue
        w, h = sps.freqz([1.0], a, worN=1024)
        mag = np.abs(h)
        freqs = w / np.pi * (sr / 2.0)
        # 找峰
        pk, _ = sps.find_peaks(mag, height=mag.max() * 0.12)
        pk = [p for p in pk if freqs[p] > FORMANT_MIN]
        if len(pk) < n_keep:
            continue
        peaks_all.append(freqs[pk[:n_keep]])
    if len(peaks_all) < 5:
        return [0.0] * n_keep
    P = np.array(peaks_all)
    return [float(np.median(P[:, i])) for i in range(n_keep)]


def f0_stats(y, sr=SR):
    f = librosa.yin(y, fmin=F0_MIN, fmax=F0_MAX, sr=sr, frame_length=2048)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    n = min(len(f), len(rms))
    thr = max(rms.max() * 0.08, 1e-4)
    v = f[:n][(rms > thr)[:n]]
    v = v[(v > F0_MIN) & (v < F0_MAX)]
    # 去八度错误：把低于中位一半的值视为 yin 八度误判
    if v.size > 10:
        med = np.median(v)
        v2 = v[(v > med * 0.55)]
        if v2.size > v.size * 0.6:
            v = v2
    if v.size < 5:
        return {"f0_med": 0.0, "f0_p10": 0.0, "f0_p90": 0.0, "voiced_frames": 0}
    return {"f0_med": float(np.median(v)),
            "f0_p10": float(np.percentile(v, 10)),
            "f0_p90": float(np.percentile(v, 90)),
            "voiced_frames": int(v.size)}


def analyze(path):
    y, _ = librosa.load(path, sr=SR, mono=True)
    if y.size == 0:
        return None
    f0 = f0_stats(y)
    f123 = formants(y)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=256)[0]
    voiced_sec = float((rms > max(rms.max() * 0.06, 1e-4)).sum()) * 256.0 / SR
    cent = librosa.feature.spectral_centroid(y=y, sr=SR)[0]
    return {
        "f0_med": f0["f0_med"], "f0_p10": f0["f0_p10"], "f0_p90": f0["f0_p90"],
        "f1": f123[0], "f2": f123[1], "f3": f123[2],
        "cent": float(np.median(cent)),
        "dur": float(librosa.get_duration(y=y, sr=SR)),
        "voiced_sec": voiced_sec,
    }


def pct_of(v, val):
    v = np.asarray(v, dtype=float)
    return float((v < val).mean() * 100.0)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    script = json.load(io.open(os.path.join(ROOT, "projects/20260915-103249/脚本/1.json"),
                               encoding="utf-8"))

    files = sorted(f for f in os.listdir(AUDIO) if f.endswith("_B.wav"))
    print("分析本期 %d 个 B 句（全部 Serena）...\n" % len(files))
    recs = {}
    for i, fn in enumerate(files):
        r = analyze(os.path.join(AUDIO, fn))
        idx = int(fn[:4])
        r["file"] = fn
        r["chars"] = len(script[idx].get("text", ""))
        r["emotion"] = script[idx].get("emotion", "")
        r["text"] = script[idx].get("text", "")
        r["cps"] = r["chars"] / r["voiced_sec"] if r["voiced_sec"] > 0.05 else 0.0
        recs[fn] = r
        if (i + 1) % 40 == 0:
            print("  ... %d/%d" % (i + 1, len(files)))

    def col(k):
        return np.array([recs[f][k] for f in files], dtype=float)

    METRICS = [("f0_med", "F0中位Hz", 0), ("f0_p10", "F0低端Hz", 0),
               ("f0_p90", "F0高端Hz", 0), ("f1", "F1共振峰Hz", 0),
               ("f2", "F2共振峰Hz", 0), ("f3", "F3共振峰Hz", 0),
               ("cent", "谱质心Hz", 0), ("cps", "语速字每秒", 2),
               ("dur", "总时长s", 2)]

    print("\n" + "=" * 78)
    print("一、本期 151 个 B 句（同一音色 Serena）的全部指标分布")
    print("=" * 78)
    print("  %-12s %8s %8s %8s %8s %8s %8s %8s" %
          ("指标", "min", "P10", "中位", "P75", "P90", "max", "极差比"))
    summary = {}
    for k, label, nd in METRICS:
        v = col(k)
        v = v[v > 0]
        summary[k] = {"min": float(v.min()), "p10": float(np.percentile(v, 10)),
                      "med": float(np.median(v)), "p75": float(np.percentile(v, 75)),
                      "p90": float(np.percentile(v, 90)), "max": float(v.max())}
        s = summary[k]
        ratio = s["max"] / s["min"] if s["min"] > 0 else float("inf")
        print("  %-12s %8.*f %8.*f %8.*f %8.*f %8.*f %8.*f %7.2fx" %
              (label, nd, s["min"], nd, s["p10"], nd, s["med"], nd, s["p75"],
               nd, s["p90"], nd, s["max"], ratio))

    # ---- 焦点两句 ----
    print("\n" + "=" * 78)
    print("二、你这俩文件在分布里的位置")
    print("=" * 78)
    focus = {}
    for fn in ("0003_B.wav", "0019_B.wav"):
        r = recs[fn]
        focus[fn] = {k: r[k] for k in ("f0_med", "f0_p10", "f0_p90", "f1", "f2", "f3",
                                       "cent", "dur", "voiced_sec", "cps",
                                       "chars", "emotion", "text")}
        print("\n  %s  (%d 字, 情绪=%s)" % (fn, r["chars"], r["emotion"]))
        print("      %s" % r["text"])
        print("      F0 中位 %7.1f Hz   (全体 P%.0f)" % (r["f0_med"], pct_of(col("f0_med"), r["f0_med"])))
        print("      F1 共振峰 %7.1f Hz (全体 P%.0f)   F2 %7.1f Hz (全体 P%.0f)" %
              (r["f1"], pct_of(col("f1"), r["f1"]), r["f2"], pct_of(col("f2"), r["f2"])))
        print("      谱质心 %7.1f Hz  语速 %.2f 字/秒  有效语音 %.2fs" %
              (r["cent"], r["cps"], r["voiced_sec"]))

    a, b = recs["0003_B.wav"], recs["0019_B.wav"]
    print("\n  ---- 两句之差 ----")
    diff = {}
    for k, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"), ("cent", "谱质心"), ("cps", "语速")):
        d = (b[k] - a[k]) / a[k] * 100.0
        diff[k] = d
        print("      %-8s %8.1f → %8.1f   (%+.1f%%)" % (label, a[k], b[k], d))

    # ---- 对照组 ----
    print("\n" + "=" * 78)
    print("三、对照组：同句 Serena · 只换种子（模型自身的随机性）")
    print("=" * 78)
    seeds = ["B_seed0.wav", "B_seed1.wav", "B_seed2.wav", "B_seed3.wav"]
    g1 = {}
    for s in seeds:
        r = analyze(os.path.join(VAR, s))
        g1[s] = r
        print("      %-14s F0 %6.1f Hz | F1 %6.1f | F2 %6.1f | 谱质心 %6.0f | 时长 %.2fs" %
              (s, r["f0_med"], r["f1"], r["f2"], r["cent"], r["dur"]))

    def spread(d, k):
        v = [d[s][k] for s in seeds]
        return max(v) - min(v), (max(v) - min(v)) / np.mean(v) * 100.0

    print("\n      同句换种子的波动：")
    for k, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"), ("cent", "谱质心")):
        sp, sp_pct = spread(g1, k)
        print("        %-8s 极差 %8.1f  (%.1f%%)" % (label, sp, sp_pct))

    # ---- 关键对比 ----
    print("\n" + "=" * 78)
    print("四、关键对比：两句之间 vs 同句换种子")
    print("=" * 78)
    print("  %-10s %14s %14s %10s" % ("指标", "两句之差", "同句换种子极差", "倍数"))
    compare = {}
    for k, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"), ("cent", "谱质心")):
        between = abs(b[k] - a[k])
        within, _ = spread(g1, k)
        ratio = between / within if within > 0 else float("inf")
        compare[k] = {"between": float(between), "within": float(within), "ratio": float(ratio)}
        print("  %-10s %14.1f %14.1f %9.2fx" % (label, between, within, ratio))

    result = {
        "population": {"n": len(files), "summary": summary},
        "focus": focus,
        "focus_diff_pct": diff,
        "G1_seed_only": {s: g1[s] for s in seeds},
        "compare": compare,
        "rows": [{k: recs[f][k] for k in ("file", "f0_med", "f0_p10", "f0_p90",
                                          "f1", "f2", "f3", "cent", "dur",
                                          "voiced_sec", "cps", "chars", "emotion")}
                 for f in files],
    }
    with io.open(os.path.join(OUT_DIR, "identity_v2.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT_DIR, "identity_v2.json"))


if __name__ == "__main__":
    main()
