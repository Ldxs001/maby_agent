# -*- coding: utf-8 -*-
"""分析修正后的 12 条样本（产品真实种子口径），输出结论数据。"""
import io
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"))

from probe_precise import measure  # noqa: E402

D = os.path.join(ROOT, "_smoke", "_seed_probe2")
OUT = os.path.join(ROOT, "_smoke", "_speaker_probe")
METRICS = [("f0_med", "F0中位Hz"), ("f0_p10", "F0低端"), ("f0_p90", "F0高端"),
           ("f1", "F1共振峰"), ("f2", "F2共振峰"), ("cent", "谱质心"),
           ("dur", "时长s")]


def main():
    rows = json.load(io.open(os.path.join(D, "seeds2.json"), encoding="utf-8"))
    for r in rows:
        m = measure(os.path.join(D, r["file"]))
        r.update(m)

    groups = {}
    for r in rows:
        groups.setdefault(r["tag"], []).append(r)
    for g in groups.values():
        g.sort(key=lambda x: x["k"])

    print("=" * 86)
    print("产品真实口径：同一句话 · 只换种子（音色 Serena，instruct=None，温度 0.4）")
    print("=" * 86)
    for tag in sorted(groups):
        g = groups[tag]
        print("\n  【%s_B】%d 字" % (tag, len(g[0]["text"])))
        print("  %-4s %10s %9s %9s %9s %9s %8s" %
              ("变体", "F0中位", "F1", "F2", "谱质心", "时长", "字数"))
        for r in g:
            mark = "  ← 就是产物" if r["k"] == 0 else ""
            print("  +%-3d %10.1f %9.1f %9.1f %9.1f %9.2f %8d%s" %
                  (r["k"], r["f0_med"], r["f1"], r["f2"], r["cent"], r["dur"],
                   len(r["text"]), mark))

    print("\n" + "=" * 86)
    print("每组内部波动（6 个种子）")
    print("=" * 86)
    print("  %-10s %-10s %14s %14s %10s" % ("组", "指标", "范围", "极差", "相对幅度"))
    span = {}
    for tag in sorted(groups):
        g = groups[tag]
        span[tag] = {}
        for k, label in METRICS:
            v = np.array([r[k] for r in g], dtype=float)
            s = float(v.max() - v.min())
            mean = float(v.mean())
            span[tag][k] = {"min": float(v.min()), "max": float(v.max()),
                            "span": s, "pct": s / mean * 100 if mean else 0}
            print("  %-10s %-10s %6.1f~%-6.1f %14.1f %13.1f%%" %
                  (tag, label, v.min(), v.max(), s, s / mean * 100 if mean else 0))

    print("\n" + "=" * 86)
    print("产物本身（+0）两句之差   vs   各自换种子的极差")
    print("=" * 86)
    a0 = groups["0003"][0]
    b0 = groups["0019"][0]
    print("  %-10s %14s %16s %16s %12s" %
          ("指标", "两句之差", "0003自己波动", "0019自己波动", "比值(对0003)"))
    verdict = {}
    for k, label in METRICS:
        between = abs(b0[k] - a0[k])
        sa = span["0003"][k]["span"]
        sb = span["0019"][k]["span"]
        ratio = between / sa if sa else float("inf")
        verdict[k] = {"between": float(between), "span_0003": float(sa),
                      "span_0019": float(sb), "ratio": float(ratio),
                      "a0": float(a0[k]), "b0": float(b0[k])}
        print("  %-10s %14.1f %16.1f %16.1f %11.2fx" % (label, between, sa, sb, ratio))
    print("\n  比值 <1 = 「同一句话自己换种子」的差异比「这两句之间」还大")

    out = {"rows": rows, "span": span, "verdict": verdict,
           "prod_a": {k: a0[k] for k in ("f0_med", "f1", "f2", "cent", "dur", "text")},
           "prod_b": {k: b0[k] for k in ("f0_med", "f1", "f2", "cent", "dur", "text")}}
    with io.open(os.path.join(OUT, "final.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT, "final.json"))


if __name__ == "__main__":
    main()
