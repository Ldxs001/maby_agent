# -*- coding: utf-8 -*-
"""脚本自我重复的诊断：查一期稿子里有多少内容是重播的。

用法：
    python tools/probes/probe_script_repeat.py [项目目录] [期号]

不传参数就查 projects/20260915-103249 的第 1 期。

判据（三条，全部只读脚本文件，不碰生成链路）：

  1. 唯一文本数 / 总句数 —— 重复有多严重。
  2. 重复段的边界与间隔 —— 是「同一段循环」还是「零星撞车」。
  3. 素材承载量对账 —— 目标句数是程序按目标时长算的，去重后的句数才是素材
     真正撑得出来的内容。两者的差额就是模型被迫灌水的量。

第 3 条是这套诊断里最有用的：它能提前预判「这一期会不会重复」，不必等生成完。
"""
from __future__ import annotations

import collections
import io
import json
import os
import sys

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:          # 直接跑本文件时也能 import podcast_maker
    sys.path.insert(0, ROOT)


def load(path, default=None):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main(argv) -> int:
    proj = argv[1] if len(argv) > 1 else os.path.join(
        ROOT, "projects", "20260915-103249")
    no = argv[2] if len(argv) > 2 else "1"
    path = os.path.join(proj, "脚本", "%s.json" % no)
    script = load(path)
    if not isinstance(script, list) or not script:
        print("读不到脚本：%s" % path)
        return 1

    texts = [str(x.get("text", "")) for x in script]
    n = len(texts)
    cnt = collections.Counter(texts)
    uniq = len(cnt)
    print("脚本 %s" % path)
    print("总句数 %d / 唯一文本 %d / 重复占用 %d 句（%.0f%%）"
          % (n, uniq, n - uniq, 100.0 * (n - uniq) / n))

    dist = collections.Counter(cnt.values())
    print()
    print("重复次数分布：")
    for times in sorted(dist, reverse=True):
        if times > 1:
            print("  出现 %d 次的文本 %d 种，共占 %d 句"
                  % (times, dist[times], times * dist[times]))

    # 重复间隔：同一文本两次出现之间隔了多少句
    pos = collections.defaultdict(list)
    for i, t in enumerate(texts):
        pos[t].append(i)
    gaps = collections.Counter()
    for ps in pos.values():
        for a, b in zip(ps, ps[1:]):
            gaps[b - a] += 1
    print()
    print("重复间隔分布（前 5）：%s" % (gaps.most_common(5) or "无重复"))

    # 循环段：出现 3 次以上的那些文本，最早那一句就是循环起点。
    # 拿「重复的文本」定位而不是拿「逐句全等」扫 —— 循环里偶有一句标点不同
    # （采样抖动），逐句全等的判据会被这一句顶掉，起点就往后错两位。
    if gaps:
        period, hits = gaps.most_common(1)[0]
        print()
        print("主周期 = %d 句（命中 %d 次）" % (period, hits))
        strict = [t for t, c in cnt.items() if c >= 3]
        if strict:
            start = min(pos[t][0] for t in strict)
            reps = 1 + max(0, (n - start - 1) // period)
            print("找到循环段：第 %d 句起，长度 %d 句，重复 %d 遍"
                  % (start + 1, period, reps))
            print("  前 %d 句是唯一内容，之后开始重播" % start)
            diffs = []
            for k in range(period):
                vals = {texts[start + period * r + k]
                        for r in range(reps) if start + period * r + k < n}
                if len(vals) > 1:
                    diffs.append(k + 1)
            print("  循环段内不一致的句位（按段内序号）：%s"
                  % (diffs if diffs else "无，逐字相同"))
        else:
            print("没有出现 3 次以上的文本：是零星撞车，不是整段循环")

    # 素材承载量对账
    print()
    print("素材承载量对账：")
    total_chars = sum(len(t) for t in texts)
    seen, kept = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            kept.append(t)
    uniq_chars = sum(len(t) for t in kept)
    print("  落盘 %d 句 / %d 字" % (n, total_chars))
    print("  去重 %d 句 / %d 字  ← 素材真正撑出来的内容" % (len(kept), uniq_chars))
    print("  注水 %d 句 / %d 字（占 %.0f%%）"
          % (n - len(kept), total_chars - uniq_chars,
             100.0 * (total_chars - uniq_chars) / max(1, total_chars)))

    # 目标句数：按当时那份配置重算，看要求了多少句
    man = load(os.path.join(proj, "报告", "%s.manifest.json" % no)) or {}
    snap = dict(man.get("config_snapshot") or {})
    if snap:
        try:
            from podcast_maker import duration_model as dm, script_engine as S

            class C(dict):
                def get(self, k, d=None):
                    return dict.get(self, k, d)

            cfg = C(snap)
            secs = float(cfg.get("script.target_minutes", 4.0)) * 60
            speed = float(cfg.get("tts.speed_a", 1.0))
            stats = dm.standard_stats()
            chars = dm.chars_for_target(secs, speed, stats)
            lines = S.estimate_line_count(cfg, chars)
            pause = float(cfg.get("audio.pause_between_lines", 0.35))
            pb = pause * max(0, lines - 1)
            chars2 = dm.chars_for_target(max(10.0, secs - pb), speed, stats)
            lines2 = S.estimate_line_count(cfg, chars2)
            print()
            print("提示词当时要求的是：约 %d 句 / %d 字" % (lines2, chars2))
            short = lines2 - len(kept)
            print("  素材撑得住 %d 句，缺口 %d 句" % (len(kept), short))
            if short > 0:
                print("  → 模型只能靠「把刚才那段再说一遍」把句数凑上去")
        except Exception as exc:  # noqa: BLE001
            print("  （算目标句数失败：%s）" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
