# -*- coding: utf-8 -*-
"""说话人一致性实测：同一期、同一 speaker(B)、同一 emotion(解释) 的句子，
   音色/音高/语速到底稳不稳。

   纯 CPU 声学分析，不加载 TTS 模型、不占显存。
   产物：_smoke/_speaker_probe/consistency.json
"""
import io
import json
import os
import sys

import numpy as np
import librosa

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJ = os.path.join(ROOT, "projects", "20260915-103249")
AUDIO = os.path.join(PROJ, "过程", "1", "audio")
SCRIPT = os.path.join(PROJ, "脚本", "1.json")
OUT_DIR = os.path.join(ROOT, "_smoke", "_speaker_probe")

SR = 22050
FMIN, FMAX = 60.0, 500.0

FOCUS = ["0003_B.wav", "0019_B.wav"]


def load_script():
    with io.open(SCRIPT, encoding="utf-8") as f:
        return json.load(f)


def analyze(path):
    """返回单文件声学指标。"""
    y, _ = librosa.load(path, sr=SR, mono=True)
    if y.size == 0:
        return None

    # ---- 有效语音段（能量门限），用于排除首尾静音影响语速计算 ----
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=256)[0]
    thr = max(rms.max() * 0.06, 1e-4)
    voiced = rms > thr
    voiced_ratio = float(voiced.mean())

    # 有效语音时长（秒）
    hop_s = 256.0 / SR
    voiced_sec = float(voiced.sum()) * hop_s

    # ---- F0：yin 取基频，只保留有声音帧 ----
    f0 = librosa.yin(y, fmin=FMIN, fmax=FMAX, sr=SR, frame_length=2048)
    # 与能量帧对齐（长度可能差 1）
    n = min(len(f0), len(rms))
    f0v = f0[:n][voiced[:n]]
    f0v = f0v[(f0v > FMIN) & (f0v < FMAX)]

    if f0v.size < 10:
        f0_med = f0_iqr = f0_p10 = f0_p90 = 0.0
    else:
        f0_med = float(np.median(f0v))
        f0_p10, f0_p90 = (float(np.percentile(f0v, 10)),
                          float(np.percentile(f0v, 90)))
        f0_iqr = float(np.percentile(f0v, 75) - np.percentile(f0v, 25))

    # ---- 频谱质心 & 滚降：亮度 / 高频占比 ----
    cent = librosa.feature.spectral_centroid(y=y, sr=SR)[0]
    roll = librosa.feature.spectral_rolloff(y=y, sr=SR, roll_percent=0.85)[0]
    flat = librosa.feature.spectral_flatness(y=y)[0]

    # ---- MFCC（说话人指纹）：倒谱均值，做倒谱均值归一抑制内容影响 ----
    mfcc = librosa.feature.mfcc(y=y, sr=SR, n_mfcc=20, n_fft=2048, hop_length=512)
    mfcc_cmn = mfcc - mfcc.mean(axis=1, keepdims=True)
    # 音色指纹 = 各阶差分能量的均值向量（弱化文本、保留声道特征）
    mfcc_mean = mfcc_cmn.mean(axis=1)

    # ---- 时长 ----
    dur = float(librosa.get_duration(y=y, sr=SR))

    return {
        "dur": dur,
        "voiced_sec": voiced_sec,
        "voiced_ratio": voiced_ratio,
        "f0_med": f0_med,
        "f0_p10": f0_p10,
        "f0_p90": f0_p90,
        "f0_iqr": f0_iqr,
        "f0_range": f0_p90 - f0_p10,
        "cent": float(cent.mean()),
        "roll85": float(roll.mean()),
        "flat": float(flat.mean()),
        "mfcc": [float(x) for x in mfcc_mean],
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    script = load_script()

    files = sorted(f for f in os.listdir(AUDIO) if f.endswith("_B.wav"))
    print("扫描 B 音色文件：%d 个" % len(files))

    rows = []
    for i, fn in enumerate(files):
        idx = int(fn[:4])
        meta = script[idx] if idx < len(script) else {}
        rec = analyze(os.path.join(AUDIO, fn))
        if rec is None:
            continue
        rec["file"] = fn
        rec["idx"] = idx
        rec["text"] = meta.get("text", "")
        rec["emotion"] = meta.get("emotion", "")
        rec["chars"] = len(meta.get("text", ""))
        # 语速：字 / 秒（用有效语音时长，排除静音）
        rec["cps"] = rec["chars"] / rec["voiced_sec"] if rec["voiced_sec"] > 0.05 else 0.0
        rows.append(rec)
        if (i + 1) % 25 == 0:
            print("  ... %d/%d" % (i + 1, len(files)))

    # ---------------- 群体分布 ----------------
    def col(k):
        return np.array([r[k] for r in rows], dtype=float)

    summary = {}
    for k in ("dur", "voiced_sec", "f0_med", "f0_iqr", "f0_range",
              "cent", "roll85", "cps", "voiced_ratio"):
        v = col(k)
        v = v[np.isfinite(v)]
        summary[k] = {
            "min": float(v.min()), "p10": float(np.percentile(v, 10)),
            "p25": float(np.percentile(v, 25)), "med": float(np.median(v)),
            "p75": float(np.percentile(v, 75)), "p90": float(np.percentile(v, 90)),
            "max": float(v.max()), "std": float(v.std(ddof=1)),
        }

    # ---------------- 音色指纹距离矩阵 ----------------
    M = np.array([r["mfcc"] for r in rows], dtype=float)
    # 归一化后算余弦距离，抵消整体能量/增益差异
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    C = 1.0 - Mn @ Mn.T
    np.fill_diagonal(C, 0.0)
    iu = np.triu_indices(len(rows), k=1)
    pair_d = C[iu]

    def pair_index(fn):
        for i, r in enumerate(rows):
            if r["file"] == fn:
                return i
        return None

    focus = []
    for fn in FOCUS:
        i = pair_index(fn)
        if i is None:
            continue
        r = rows[i]
        others = np.delete(C[i], i)
        focus.append({
            "file": fn,
            "idx": r["idx"],
            "text": r["text"],
            "emotion": r["emotion"],
            "chars": r["chars"],
            "metrics": {k: r[k] for k in ("dur", "voiced_sec", "f0_med",
                                          "f0_p10", "f0_p90", "f0_iqr",
                                          "f0_range", "cent", "roll85",
                                          "cps", "voiced_ratio")},
            "dist_to_others": {
                "med": float(np.median(others)),
                "p10": float(np.percentile(others, 10)),
                "p90": float(np.percentile(others, 90)),
                "min": float(others.min()),
                "max": float(others.max()),
                "rank_pct": float((others < np.median(others)).mean()),
            },
        })

    # 两者互距
    i0, i1 = pair_index(FOCUS[0]), pair_index(FOCUS[1])
    focus_pair = None
    if i0 is not None and i1 is not None:
        d = float(C[i0, i1])
        pct = float((pair_d < d).mean() * 100.0)
        focus_pair = {"dist": d, "percentile": pct,
                      "median_all_pairs": float(np.median(pair_d)),
                      "p90_all_pairs": float(np.percentile(pair_d, 90)),
                      "max_all_pairs": float(pair_d.max())}

    # ---------------- 按 emotion 分组的 F0 ----------------
    by_emo = {}
    for e in sorted({r["emotion"] for r in rows}):
        sub = [r["f0_med"] for r in rows if r["emotion"] == e]
        by_emo[e] = {
            "n": len(sub),
            "f0_med": float(np.median(sub)),
            "f0_min": float(np.min(sub)),
            "f0_max": float(np.max(sub)),
            "spread": float(max(sub) - min(sub)),
        }

    out = {
        "n_files": len(rows),
        "summary": summary,
        "focus": focus,
        "focus_pair": focus_pair,
        "by_emotion": by_emo,
        "rows": [{k: v for k, v in r.items() if k != "mfcc"} for r in rows],
    }
    with io.open(os.path.join(OUT_DIR, "consistency.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # ---------------- 控制台摘要 ----------------
    print("\n" + "=" * 66)
    print("群体分布（151 个 B 句）")
    print("=" * 66)
    print("  %-12s %8s %8s %8s %8s %8s %8s %8s" %
          ("指标", "min", "p10", "中位", "p75", "p90", "max", "极差比"))
    label = {"f0_med": "F0中位Hz", "f0_range": "F0跨度Hz", "cent": "谱质心Hz",
             "cps": "语速字/秒", "dur": "总时长s", "voiced_sec": "有效语音s"}
    for k in ("f0_med", "f0_range", "cent", "cps", "dur", "voiced_sec"):
        s = summary[k]
        ratio = s["max"] / s["min"] if s["min"] > 0 else float("inf")
        print("  %-12s %8.1f %8.1f %8.1f %8.1f %8.1f %8.1f %7.2fx" %
              (label[k], s["min"], s["p10"], s["med"], s["p75"],
               s["p90"], s["max"], ratio))

    print("\n" + "=" * 66)
    print("焦点两句")
    print("=" * 66)
    for f in focus:
        m = f["metrics"]
        print("\n  %s  (脚本第 %d 句, 情绪=%s, %d 字)" % (f["file"], f["idx"], f["emotion"], f["chars"]))
        print("    %s" % f["text"])
        print("    F0 中位 %.1f Hz | 跨度 %.1f Hz (P10 %.1f → P90 %.1f)" %
              (m["f0_med"], m["f0_range"], m["f0_p10"], m["f0_p90"]))
        print("    谱质心 %.0f Hz | 有效语音 %.2fs / 总长 %.2fs | 语速 %.2f 字/秒" %
              (m["cent"], m["voiced_sec"], m["dur"], m["cps"]))

    if focus_pair:
        print("\n" + "=" * 66)
        print("这两句的『音色指纹距离』在全体中的位置")
        print("=" * 66)
        print("  0003_B ↔ 0019_B 距离 : %.4f" % focus_pair["dist"])
        print("  全体 151 句两两距离中位: %.4f" % focus_pair["median_all_pairs"])
        print("  全体两两距离 P90      : %.4f" % focus_pair["p90_all_pairs"])
        print("  全体两两距离最大       : %.4f" % focus_pair["max_all_pairs"])
        print("  这对距离超过全体 %.1f%% 的配对" % focus_pair["percentile"])

    print("\n" + "=" * 66)
    print("按情绪的 F0 波动（同一标签内部也不是定值）")
    print("=" * 66)
    for e, v in sorted(by_emo.items(), key=lambda x: -x[1]["spread"]):
        print("  %-6s n=%-4d 中位 %6.1f Hz  范围 %6.1f ~ %6.1f  跨度 %6.1f Hz" %
              (e, v["n"], v["f0_med"], v["f0_min"], v["f0_max"], v["spread"]))

    print("\n产物: %s" % os.path.join(OUT_DIR, "consistency.json"))


if __name__ == "__main__":
    main()
