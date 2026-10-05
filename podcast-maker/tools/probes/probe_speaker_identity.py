# -*- coding: utf-8 -*-
"""说话人同一性归因：把「同句换种子」「同句换指令」「同音色不同句」三组
   放在同一把尺子上量，回答「这俩到底是不是一个人」。

三组对照（全部 B = Serena）：
    G1 同文本 · 同种子基准 · 换 4 个种子   ← 模型自身的采样方差（下界）
    G2 同文本 · 同种子 · 加/不加语气指令   ← 去指令的因果效应
    G3 同音色 · 不同文本（0003 vs 0019）   ← 真实生产里遇到的差异
    参考分布：本期 151 个 B 句两两距离

距离口径与 _probe_speaker_consistency.py 完全一致（对齐 151 句分布）。
产物：_smoke/_speaker_probe/identity.json
"""
import io
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"))

from probe_speaker_consistency import analyze, AUDIO  # noqa: E402

VAR = os.path.join(ROOT, "_smoke", "_variance_probe")
OUT_DIR = os.path.join(ROOT, "_smoke", "_speaker_probe")

FOCUS = {"0003_B.wav": "Serena", "0019_B.wav": "Serena"}


def mfcc_vec(rec):
    return np.array(rec["mfcc"], dtype=float)


def cos_dist(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return float("nan")
    return float(1.0 - (a @ b) / (na * nb))


def main():
    recs = {}

    # ---- 本期全部 B 句（含焦点两句）----
    script = json.load(io.open(os.path.join(ROOT, "projects/20260915-103249/脚本/1.json"),
                               encoding="utf-8"))
    files = sorted(f for f in os.listdir(AUDIO) if f.endswith("_B.wav"))
    print("分析本期 %d 个 B 句 ..." % len(files))
    for fn in files:
        idx = int(fn[:4])
        r = analyze(os.path.join(AUDIO, fn))
        r["file"] = fn
        r["text"] = script[idx].get("text", "") if idx < len(script) else ""
        r["emotion"] = script[idx].get("emotion", "") if idx < len(script) else ""
        recs[fn] = r

    # ---- 上轮对照组 ----
    print("分析对照组 %d 个 ..." % len([f for f in os.listdir(VAR) if f.endswith(".wav")]))
    for fn in sorted(f for f in os.listdir(VAR) if f.endswith(".wav")):
        r = analyze(os.path.join(VAR, fn))
        r["file"] = fn
        recs["VAR/" + fn] = r

    def get(k):
        return recs[k]

    # ================= 距离矩阵（全体 B）=================
    keys_b = ["%04d_B.wav" % i for i in range(302) if "%04d_B.wav" % i in recs]
    Mb = np.array([mfcc_vec(recs[k]) for k in keys_b])
    Mn = Mb / (np.linalg.norm(Mb, axis=1, keepdims=True) + 1e-9)
    Cb = 1.0 - Mn @ Mn.T
    np.fill_diagonal(Cb, 0.0)
    iu = np.triu_indices(len(keys_b), k=1)
    pop = Cb[iu]

    pos = {k: i for i, k in enumerate(keys_b)}
    g3 = float(Cb[pos["0003_B.wav"], pos["0019_B.wav"]])

    # ================= G1 同句换种子 =================
    seeds = ["B_seed0.wav", "B_seed1.wav", "B_seed2.wav", "B_seed3.wav"]
    g1 = []
    for i in range(len(seeds)):
        for j in range(i + 1, len(seeds)):
            d = cos_dist(mfcc_vec(recs["VAR/" + seeds[i]]),
                         mfcc_vec(recs["VAR/" + seeds[j]]))
            g1.append({"a": seeds[i], "b": seeds[j], "dist": d})
    g1d = np.array([x["dist"] for x in g1])

    # ================= G2 同句同种子 · 换指令 =================
    g2 = []
    for tag, voice in (("A1", "Vivian/A"), ("A2", "Vivian/A"),
                       ("A3", "Serena/B"), ("A4", "Serena/B")):
        on = recs["VAR/%s_on.wav" % tag]
        off = recs["VAR/%s_off.wav" % tag]
        d = cos_dist(mfcc_vec(on), mfcc_vec(off))
        g2.append({
            "tag": tag, "voice": voice, "dist": d,
            "f0_on": on["f0_med"], "f0_off": off["f0_med"],
            "f0_delta_pct": (off["f0_med"] - on["f0_med"]) / on["f0_med"] * 100.0,
            "cent_on": on["cent"], "cent_off": off["cent"],
            "dur_on": on["dur"], "dur_off": off["dur"],
        })
    g2d = np.array([x["dist"] for x in g2])

    # ================= F0 口径 =================
    def f0_of(k):
        return recs[k]["f0_med"]

    f0_g1 = [f0_of("VAR/" + s) for s in seeds]
    f0_g3 = [f0_of("0003_B.wav"), f0_of("0019_B.wav")]
    allb_f0 = np.array([recs[k]["f0_med"] for k in keys_b])

    result = {
        "voice_map": {"A": "Vivian", "B": "Serena"},
        "population": {
            "n": len(keys_b),
            "pair_dist": {"med": float(np.median(pop)),
                          "p10": float(np.percentile(pop, 10)),
                          "p90": float(np.percentile(pop, 90)),
                          "max": float(pop.max())},
            "f0": {"min": float(allb_f0.min()), "med": float(np.median(allb_f0)),
                   "max": float(allb_f0.max())},
        },
        "G1_seed_only": {
            "note": "同一句文本、同音色 Serena、只换种子",
            "pairs": g1,
            "dist_med": float(np.median(g1d)),
            "dist_max": float(g1d.max()),
            "f0": f0_g1,
            "f0_spread": float(max(f0_g1) - min(f0_g1)),
            "f0_spread_pct": float((max(f0_g1) - min(f0_g1)) / np.mean(f0_g1) * 100),
        },
        "G2_instruct_only": {
            "note": "同一句文本、同种子，加/不加「用平静的语气说」",
            "pairs": g2,
            "dist_med": float(np.median(g2d)),
            "dist_max": float(g2d.max()),
        },
        "G3_cross_line": {
            "note": "同音色 Serena、不同文本（0003 vs 0019）",
            "dist": g3,
            "percentile_in_pop": float((pop < g3).mean() * 100),
            "f0": f0_g3,
            "f0_delta_pct": (f0_g3[1] - f0_g3[0]) / f0_g3[0] * 100,
        },
        "detail": {
            "0003": {k: recs["0003_B.wav"][k] for k in
                     ("f0_med", "f0_p10", "f0_p90", "f0_range", "cent",
                      "roll85", "dur", "voiced_sec", "text", "emotion")},
            "0019": {k: recs["0019_B.wav"][k] for k in
                     ("f0_med", "f0_p10", "f0_p90", "f0_range", "cent",
                      "roll85", "dur", "voiced_sec", "text", "emotion")},
            "seed_samples": [{"file": s, "f0_med": f0_of("VAR/" + s),
                              "cent": recs["VAR/" + s]["cent"],
                              "dur": recs["VAR/" + s]["dur"],
                              "voiced_sec": recs["VAR/" + s]["voiced_sec"]}
                             for s in seeds],
        },
    }
    with io.open(os.path.join(OUT_DIR, "identity.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # ================= 输出 =================
    P = result["population"]
    print("\n" + "=" * 70)
    print("参照：本期 151 个 B 句（全是 Serena）两两音色指纹距离")
    print("=" * 70)
    print("  中位 %.4f | P10 %.4f | P90 %.4f | 最大 %.4f" %
          (P["pair_dist"]["med"], P["pair_dist"]["p10"],
           P["pair_dist"]["p90"], P["pair_dist"]["max"]))
    print("  F0 分布 %.1f ~ %.1f Hz（中位 %.1f）" %
          (P["f0"]["min"], P["f0"]["med"], P["f0"]["max"]))

    print("\n" + "=" * 70)
    print("G1  同一句文本 · 只换种子（4 个种子 → 6 对）")
    print("=" * 70)
    for x in g1:
        print("    %-14s ↔ %-14s  %.4f" % (x["a"], x["b"], x["dist"]))
    print("  → 距离中位 %.4f / 最大 %.4f" % (result["G1_seed_only"]["dist_med"],
                                              result["G1_seed_only"]["dist_max"]))
    print("  → 4 个种子 F0: %s" % " ".join("%.1f" % v for v in f0_g1))
    print("  → 同句 F0 极差 %.1f Hz（%.1f%%）" %
          (result["G1_seed_only"]["f0_spread"], result["G1_seed_only"]["f0_spread_pct"]))

    print("\n" + "=" * 70)
    print("G2  同一句 · 同种子 · 加/不加语气指令")
    print("=" * 70)
    for x in g2:
        print("    %s (%s)  距离 %.4f | F0 %.1f→%.1f (%+.1f%%) | 时长 %.2f→%.2f" %
              (x["tag"], x["voice"], x["dist"], x["f0_on"], x["f0_off"],
               x["f0_delta_pct"], x["dur_on"], x["dur_off"]))
    print("  → 距离中位 %.4f / 最大 %.4f" % (result["G2_instruct_only"]["dist_med"],
                                              result["G2_instruct_only"]["dist_max"]))

    print("\n" + "=" * 70)
    print("G3  同音色 · 不同文本（你这俩文件）")
    print("=" * 70)
    print("    0003_B ↔ 0019_B  距离 %.4f  （超过全体 %.1f%% 的配对）" %
          (g3, result["G3_cross_line"]["percentile_in_pop"]))
    print("    F0 %.1f → %.1f Hz  (%+.1f%%)" %
          (f0_g3[0], f0_g3[1], result["G3_cross_line"]["f0_delta_pct"]))

    print("\n" + "=" * 70)
    print("关键比值：G3 的距离 / G1 的距离  = %.2f 倍" %
          (g3 / result["G1_seed_only"]["dist_med"]))
    print("=" * 70)
    print("\n产物: %s" % os.path.join(OUT_DIR, "identity.json"))


if __name__ == "__main__":
    main()
