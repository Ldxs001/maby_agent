# -*- coding: utf-8 -*-
"""分析同文本多种子样本：组内抖动 vs 组间差异，判断「是不是一个人」。

复用 _probe_identity_v2.py 的 analyze()（同口径），保证与 151 句分布可比。
产物：_smoke/_seed_probe/seed_analysis.json
"""
import io
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"))

from probe_identity_v2 import analyze  # noqa: E402

SEED_DIR = os.path.join(ROOT, "_smoke", "_seed_probe")
OUT = os.path.join(ROOT, "_smoke", "_speaker_probe")
METRICS = [("f0_med", "F0中位"), ("f1", "F1"), ("f2", "F2"),
           ("cent", "谱质心"), ("dur", "时长")]


def main():
    rows = json.load(io.open(os.path.join(SEED_DIR, "seeds.json"), encoding="utf-8"))
    for r in rows:
        a = analyze(os.path.join(SEED_DIR, r["file"]))
        r.update({k: a[k] for k in ("f0_med", "f0_p10", "f0_p90", "f1", "f2", "f3",
                                    "cent", "voiced_sec")})

    groups = {}
    for r in rows:
        groups.setdefault(r["tag"], []).append(r)
    for g in groups.values():
        g.sort(key=lambda x: x["k"])

    print("=" * 84)
    print("同一句话 · 只换种子（Serena，instruct=None）")
    print("=" * 84)
    for tag in sorted(groups):
        g = groups[tag]
        print("\n  【%s】%s" % (tag, g[0]["text"]))
        print("  %-4s %-12s %9s %8s %8s %9s %8s" %
              ("种子", "文件", "F0中位", "F1", "F2", "谱质心", "时长"))
        for r in g:
            print("  +%-3d %-12s %9.1f %8.1f %8.1f %9.1f %8.2f" %
                  (r["k"], r["file"], r["f0_med"], r["f1"], r["f2"], r["cent"], r["dur"]))

    print("\n" + "=" * 84)
    print("组内抖动（同一句话自己换种子）")
    print("=" * 84)
    print("  %-8s %18s %18s %10s" % ("指标", "A_0003 极差", "B_0019 极差", "比值"))
    within = {}
    for k, label in METRICS:
        va = [r[k] for r in groups["A_0003"]]
        vb = [r[k] for r in groups["B_0019"]]
        ra, rb = max(va) - min(va), max(vb) - min(vb)
        within[k] = {"A": ra, "B": rb}
        print("  %-8s %18.1f %18.1f %9.2fx" % (label, ra, rb, ra / rb if rb else float("inf")))

    print("\n" + "=" * 84)
    print("组间差异（同一句话的『基准版本』之间）")
    print("=" * 84)
    a0 = groups["A_0003"][0]
    b0 = groups["B_0019"][0]
    between = {}
    print("  %-8s %14s %14s %10s" % ("指标", "A_0003_s0", "B_0019_s0", "差"))
    for k, label in METRICS:
        d = abs(b0[k] - a0[k])
        between[k] = d
        print("  %-8s %14.1f %14.1f %10.1f" % (label, a0[k], b0[k], d))

    print("\n" + "=" * 84)
    print("关键判据：组间差 vs 组内极差（>1 才说明『换文本』比『换种子』影响更大）")
    print("=" * 84)
    print("  %-8s %12s %12s %12s %10s" % ("指标", "组间差", "A组内极差", "B组内极差", "组间/A"))
    cover = {}
    for k, label in METRICS:
        bd = between[k]
        wa = within[k]["A"]
        ratio = bd / wa if wa else float("inf")
        cover[k] = {"between": bd, "within_A": wa, "within_B": within[k]["B"], "ratio": ratio}
        print("  %-8s %12.1f %12.1f %12.1f %9.2fx" % (label, bd, wa, within[k]["B"], ratio))

    # 分布重叠判定：把两组样本混在一起看 F0 范围
    print("\n" + "=" * 84)
    print("分布重叠检查（12 条混排后的 F0）")
    print("=" * 84)
    allf0 = sorted([(r["f0_med"], r["tag"], r["k"]) for r in rows])
    for v, t, k in allf0:
        print("    %7.1f Hz   %s +%d" % (v, t, k))
    fa = [r["f0_med"] for r in groups["A_0003"]]
    fb = [r["f0_med"] for r in groups["B_0019"]]
    ov = max(0.0, min(max(fa), max(fb)) - max(min(fa), min(fb)))
    span = max(max(fa), max(fb)) - min(min(fa), min(fb))
    print("\n  A 组 F0 范围 %.1f ~ %.1f | B 组 %.1f ~ %.1f" %
          (min(fa), max(fa), min(fb), max(fb)))
    print("  重叠区间 %.1f Hz / 并集跨度 %.1f Hz = %.0f%%" %
          (ov, span, ov / span * 100 if span else 0))

    result = {
        "rows": rows,
        "within": within,
        "between": between,
        "cover": cover,
        "overlap": {"f0_A": fa, "f0_B": fb, "overlap_hz": ov,
                    "span_hz": span, "overlap_pct": ov / span * 100 if span else 0},
    }
    with io.open(os.path.join(OUT, "seed_analysis.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT, "seed_analysis.json"))


if __name__ == "__main__":
    main()
