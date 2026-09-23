# -*- coding: utf-8 -*-
"""只读探针：看脚本里 emotion 标签是怎么在句间流动的。

要回答的问题：成品里「一句一个样、突兀」的现象，有多少是脚本本身
逐句贴了不同情绪标签造成的，有多少只能归给采样随机。

不改任何东西，只统计。
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = [
    os.path.join(ROOT, "projects", "20260912-235659", "脚本", "1.json"),
    os.path.join(ROOT, "projects", "20260912-235659", "脚本", "2.json"),
]


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if isinstance(obj, dict):
        for key in ("lines", "script", "items", "segments"):
            if isinstance(obj.get(key), list):
                return obj[key]
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return obj if isinstance(obj, list) else []


def run(path):
    lines = load(path)
    if not lines:
        print("  (读不出句子列表) 顶层类型=%s" % type(lines).__name__)
        return
    print("  句子数 %d" % len(lines))
    keys = sorted({k for it in lines if isinstance(it, dict) for k in it})
    print("  字段：%s" % ", ".join(keys))

    spk = Counter(it.get("speaker", "?") for it in lines)
    emo = Counter((it.get("emotion") or "(空)") for it in lines)
    print("  说话人分布：%s" % dict(spk))
    print("  情绪分布（%d 种）：%s" % (len(emo), dict(emo.most_common())))

    # 相邻句情绪是否跳变
    seq = [(it.get("speaker", "?"), (it.get("emotion") or "")) for it in lines]
    switch = sum(1 for a, b in zip(seq, seq[1:]) if a[1] != b[1])
    ratio = switch / max(1, len(seq) - 1)
    print("  相邻句情绪变化：%d/%d = %.0f%%" % (switch, len(seq) - 1, ratio * 100))

    # 同一个说话人内部，情绪是否也在跳
    same_switch = same_total = 0
    for a, b in zip(seq, seq[1:]):
        if a[0] == b[0]:
            same_total += 1
            if a[1] != b[1]:
                same_switch += 1
    print("  同一角色连续两句之间情绪变化：%d/%d = %.0f%%"
          % (same_switch, same_total, same_switch / max(1, same_total) * 100))

    # 情绪持续段（同角色 + 同情绪 连成一段）
    runs = []
    cur = None
    for s, e in seq:
        if cur and cur[0] == s and cur[1] == e:
            cur[2] += 1
        else:
            cur = [s, e, 1]
            runs.append(cur)
    lens = [r[2] for r in runs]
    print("  情绪段数 %d，段长中位 %.1f，最长 %d"
          % (len(runs), sorted(lens)[len(lens) // 2] if lens else 0, max(lens or [0])))

    # 字数（对应发音时长）
    chars = [len(it.get("text", "")) for it in lines]
    chars = [c for c in chars if c]
    if chars:
        chars.sort()
        print("  句长：中位 %d 字，最短 %d，最长 %d"
              % (chars[len(chars) // 2], chars[0], chars[-1]))
    if "actual_seconds" in keys:
        secs = [it["actual_seconds"] for it in lines if it.get("actual_seconds")]
        if secs:
            ss = sorted(secs)
            per = sorted(it["actual_seconds"] / max(1, len(it.get("text", "")))
                         for it in lines if it.get("actual_seconds") and it.get("text"))
            print("  实测时长：中位 %.2fs，最短 %.2fs，最长 %.2fs"
                  % (ss[len(ss) // 2], ss[0], ss[-1]))
            if per:
                print("  每秒字数（语速代理）：中位 %.2f，最慢 %.2f，最快 %.2f"
                      % (per[len(per) // 2], per[0], per[-1]))


def dump_head(path, n=36):
    lines = load(path)
    print("  前 %d 句（角色 / 情绪 / 文本开头）：" % n)
    for i, it in enumerate(lines[:n]):
        print("    %3d  %s  %-6s  %s"
              % (i, it.get("speaker", "?"), it.get("emotion") or "-",
                 (it.get("text", "") or "")[:24]))


for p in SCRIPTS:
    print("== %s" % os.path.basename(p))
    if os.path.exists(p):
        run(p)
        dump_head(p)
    else:
        print("  (不存在)")
    print()
