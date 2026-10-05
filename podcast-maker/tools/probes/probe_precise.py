# -*- coding: utf-8 -*-
"""精测：对关键样本重算 F0 与共振峰，用更可靠的口径。

- F0 改用 librosa.pyin（概率式，自带 voiced_flag，比 yin 稳）
- 共振峰改用 LPC 复根法（标准做法），并加频率/带宽约束

覆盖：原始两句 + 同句换种子 12 条 + 种子策略 18 条
产物：_smoke/_speaker_probe/precise.json
"""
import io
import json
import os
import sys

import numpy as np
import librosa

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIO = os.path.join(ROOT, "projects/20260915-103249/过程/1/audio")
SEED_DIR = os.path.join(ROOT, "_smoke", "_seed_probe")
OUT_DIR = os.path.join(ROOT, "_smoke", "_speaker_probe")

SR = 22050
LPC_ORDER = int(SR / 1000) + 4


def precise_f0(y, sr=SR):
    f0, vflag, vprob = librosa.pyin(
        y, fmin=70.0, fmax=450.0, sr=sr, frame_length=2048,
        fill_na=None)
    m = (~np.isnan(f0)) & vflag
    v = f0[m]
    if v.size < 5:
        return {"f0_med": 0.0, "f0_p10": 0.0, "f0_p90": 0.0, "n": 0}
    return {"f0_med": float(np.median(v)),
            "f0_p10": float(np.percentile(v, 10)),
            "f0_p90": float(np.percentile(v, 90)),
            "n": int(v.size)}


def lpc_roots_formants(y, sr=SR, order=LPC_ORDER):
    """LPC 复根法求共振峰中位。标准做法：取虚部>=0 的根，转频率+带宽后筛。"""
    win = int(0.025 * sr)
    hop = int(0.010 * sr)
    yp = np.append(y[0], y[1:] - 0.97 * y[:-1])
    hamming = np.hamming(win)
    F = []
    for s in range(0, len(yp) - win, hop):
        fr = yp[s:s + win]
        if np.sqrt(np.mean(fr ** 2)) < 0.03:
            continue
        try:
            a = librosa.lpc(fr * hamming, order=order)
        except Exception:
            continue
        if not np.all(np.isfinite(a)):
            continue
        r = np.roots(a)
        r = r[np.imag(r) >= 0]
        if r.size == 0:
            continue
        ang = np.arctan2(np.imag(r), np.real(r))
        freq = ang * sr / (2.0 * np.pi)
        bw = -0.5 * (sr / (2.0 * np.pi)) * np.log(np.abs(r) + 1e-12)
        keep = (freq > 90) & (freq < sr / 2.0 - 200) & (bw < 500)
        freq = np.sort(freq[keep])
        if freq.size < 3:
            continue
        # F1 在第一共振峰典型区间取；避免选到基频残留
        cand = freq[(freq > 250) & (freq < 1300)]
        if cand.size == 0:
            continue
        f1 = cand[0]
        rest = freq[freq > f1 + 200]
        if rest.size < 2:
            continue
        F.append([f1, rest[0], rest[1]])
    if len(F) < 5:
        return {"f1": 0.0, "f2": 0.0, "f3": 0.0, "frames": 0}
    M = np.array(F)
    return {"f1": float(np.median(M[:, 0])), "f2": float(np.median(M[:, 1])),
            "f3": float(np.median(M[:, 2])), "frames": int(len(F))}


def measure(path):
    y, _ = librosa.load(path, sr=SR, mono=True)
    out = {}
    out.update(precise_f0(y))
    out.update(lpc_roots_formants(y))
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=256)[0]
    voiced = rms > max(rms.max() * 0.06, 1e-4)
    out["voiced_sec"] = float(voiced.sum()) * 256.0 / SR
    out["dur"] = float(librosa.get_duration(y=y, sr=SR))
    cent = librosa.feature.spectral_centroid(y=y, sr=SR)[0]
    out["cent"] = float(np.median(cent))
    return out


def main():
    targets = {}
    targets["原始_0003"] = os.path.join(AUDIO, "0003_B.wav")
    targets["原始_0019"] = os.path.join(AUDIO, "0019_B.wav")
    for k in range(6):
        targets["种子_0003_s%d" % k] = os.path.join(SEED_DIR, "A_0003_s%d.wav" % k)
        targets["种子_0019_s%d" % k] = os.path.join(SEED_DIR, "B_0019_s%d.wav" % k)
    pol = json.load(io.open(os.path.join(SEED_DIR, "policy.json"), encoding="utf-8"))
    for r in pol["rows"]:
        key = "策略_%s_%04d" % (r["policy"].split("_")[0], r["idx"])
        targets[key] = os.path.join(SEED_DIR, r["file"])

    print("精测 %d 个文件（pyin + LPC 复根法）...\n" % len(targets))
    res = {}
    for name, p in targets.items():
        if not os.path.exists(p):
            print("  跳过（不存在）: %s" % name)
            continue
        m = measure(p)
        m["file"] = os.path.basename(p)
        res[name] = m
        print("  %-18s F0 %6.1f (P10 %5.1f P90 %5.1f) | F1 %6.1f F2 %6.1f F3 %6.1f | 质心 %5.0f | %.2fs" %
              (name, m["f0_med"], m["f0_p10"], m["f0_p90"],
               m["f1"], m["f2"], m["f3"], m["cent"], m["dur"]))

    # ---------------- 组内 vs 组间 ----------------
    print("\n" + "=" * 80)
    print("一、同句换种子：组内离散")
    print("=" * 80)
    def grp(prefix):
        return [res[k] for k in res if k.startswith(prefix)]
    summary = {}
    for tag, prefix in (("0003", "种子_0003_s"), ("0019", "种子_0019_s")):
        g = grp(prefix)
        s = {}
        for key, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"),
                           ("cent", "谱质心"), ("dur", "时长")):
            v = np.array([x[key] for x in g], dtype=float)
            s[key] = {"min": float(v.min()), "max": float(v.max()),
                      "span": float(v.max() - v.min()),
                      "std": float(v.std(ddof=1)),
                      "mean": float(v.mean())}
        summary[tag] = s
        print("\n  【%s】6 个种子" % tag)
        for key, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"),
                           ("cent", "谱质心"), ("dur", "时长")):
            x = s[key]
            print("    %-8s %8.1f ~ %8.1f   极差 %7.1f  (%5.1f%%)" %
                  (label, x["min"], x["max"], x["span"],
                   x["span"] / x["mean"] * 100 if x["mean"] else 0))

    print("\n" + "=" * 80)
    print("二、原始两句之间（你质疑的那两个）")
    print("=" * 80)
    a, b = res["原始_0003"], res["原始_0019"]
    between = {}
    print("  %-8s %12s %12s %10s" % ("指标", "0003", "0019", "相对差"))
    for key, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"),
                       ("cent", "谱质心"), ("dur", "时长")):
        d = abs(b[key] - a[key])
        between[key] = {"abs": float(d),
                        "pct": float(d / a[key] * 100) if a[key] else 0}
        print("  %-8s %12.1f %12.1f %9.1f%%" % (label, a[key], b[key], between[key]["pct"]))

    print("\n" + "=" * 80)
    print("三、决定性对比：两句之差 vs 每句自己换种子")
    print("=" * 80)
    print("  %-8s %12s %14s %14s %10s" %
          ("指标", "两句之差", "0003组内极差", "0019组内极差", "比值(对0003)"))
    verdict = {}
    for key, label in (("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"),
                       ("cent", "谱质心"), ("dur", "时长")):
        bd = between[key]["abs"]
        sa = summary["0003"][key]["span"]
        sb = summary["0019"][key]["span"]
        ratio = bd / sa if sa else float("inf")
        verdict[key] = {"between": float(bd), "span0003": float(sa),
                        "span0019": float(sb), "ratio": float(ratio)}
        print("  %-8s %12.1f %14.1f %14.1f %9.2fx" % (label, bd, sa, sb, ratio))
    print("\n  判读：比值 <1 表示「同一句话自己换种子」造成的差异，比「这两句话之间」还大。")

    print("\n" + "=" * 80)
    print("四、固定种子能否统一音色（6 句不同文本）")
    print("=" * 80)
    print("  %-16s %10s %10s %10s %10s" % ("方案", "F0标准差", "F1标准差", "F2标准差", "质心标准差"))
    polsum = {}
    for pname in ("P1", "P2", "P3"):
        g = [res[k] for k in res if k.startswith("策略_" + pname)]
        so = {}
        for key in ("f0_med", "f1", "f2", "cent"):
            v = np.array([x[key] for x in g], dtype=float)
            so[key] = float(v.std(ddof=1))
        polsum[pname] = so
        print("  %-16s %10.1f %10.1f %10.1f %10.1f" %
              (pname, so["f0_med"], so["f1"], so["f2"], so["cent"]))
    base = polsum["P1"]
    for pname in ("P2", "P3"):
        parts = []
        for key, label in (("f0_med", "F0"), ("f1", "F1"), ("f2", "F2"), ("cent", "质心")):
            r = base[key] / polsum[pname][key] if polsum[pname][key] else float("inf")
            parts.append("%s %.2fx" % (label, r))
        print("  %-16s 相对现状改善：%s" % (pname, "  ".join(parts)))

    out = {"measures": res, "seed_span": summary, "between": between,
           "verdict": verdict, "policy_std": polsum}
    with io.open(os.path.join(OUT_DIR, "precise.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT_DIR, "precise.json"))


if __name__ == "__main__":
    main()
