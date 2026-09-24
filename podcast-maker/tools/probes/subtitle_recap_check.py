#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拿真实项目的回顾三条，按真实估时验算横滚滚不滚得完。

回顾三条是**整切**的（上期《…》。/ 聊的是…。/ 讲了…等。），第三条常常上百字，
所以在单行滚动档里会横滚——这个探针就是回答「念完这句之前能不能滚完」：

- 回顾三条由 `pipeline.review_rows` 现算（不是抄一份样本），估时由
  `duration_model.estimate_seconds` 现算，时间轴按「句 + 0.45 秒停顿」推；
- 两档各跑一遍，逐句打印「句首静止 / 滚动时长 / 终点 vs 句长」；
- 装得下（文字宽 ≤ 框宽）的句子不滚，静止居中。

`\\move` 的终点与句末的差**只可能是时间戳取整残差**（ASS 是 10ms 一格），
所以判定用 `TAIL_TOLERANCE` 而不是 `end <= span`——后者会误报（实测差 3ms）。

跑法：python tools/probes/subtitle_recap_check.py
      python tools/probes/subtitle_recap_check.py --project 20260917-015336 --episode 2d
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from podcast_maker import duration_model as D                     # noqa: E402
from podcast_maker import pipeline as PL                          # noqa: E402
from podcast_maker import project_store as P                      # noqa: E402
from podcast_maker import subtitle_engine as S                    # noqa: E402
from podcast_maker.config_manager import ConfigManager            # noqa: E402

MV = re.compile(r"\\move\((-?\d+),(-?\d+),(-?\d+),(-?\d+),(\d+),(\d+)\)")

#: 与 `tests/test_subtitle_wrap.py` 的 `TAIL_TOLERANCE`、探针 `subtitle_roll_check.py`
#: 同一口径：10ms 时间戳落格 + `\move` 终点取整，20ms 封顶。
TAIL_TOLERANCE = 0.020


def secs(stamp):
    h, m, s = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def main():
    ap = argparse.ArgumentParser(description="回顾三条的横滚验算（真实项目数据）")
    ap.add_argument("--base", default="projects", help="项目根目录（默认 projects）")
    ap.add_argument("--project", default="20260917-015336", help="项目 id")
    ap.add_argument("--episode", default="2d", help="要出的期号（回顾取的是它上一期）")
    args = ap.parse_args()

    item = P.find(args.base, args.project)
    if not item:
        raise SystemExit("项目里没找到 %s" % args.project)
    cfg = ConfigManager().data()
    cfg["intro_outro.review"] = True
    rows = PL.review_rows(args.base, item, args.episode, cfg)
    print("回顾条数：%d" % len(rows))
    timings, t = [], 0.0
    for r in rows:
        span = D.estimate_seconds(r["text"])
        timings.append({"start": t, "end": t + span})
        t += span + 0.45
        print("  %2d 字 %6.1f 秒   %s" % (len(r["text"]), span, r["text"][:46]))
    if cfg.get("speaker_indicator.name_shown"):
        print("注：下面每条字幕的字数含「说话人名：」前缀——它是画面上的字，要算进文字宽。")

    for preset in ("single", "lyric"):
        cfg["subtitle.preset"] = preset
        ass = S.build_ass(rows, cfg, timings, 1920, 1080, "")[0]
        g = S.frame_geometry(cfg, 1920, 1080, "", 1 if preset == "single" else 8)
        print("\n[%s] 框 x=[%d,%d] 宽=%d 高=%d（左缘 %d / 右缘 %d）"
              % (preset, g["x1"], g["x2"], g["x2"] - g["x1"], g["h"],
                 g["x1"], g["x2"]))
        for ln in ass.splitlines():
            if not ln.startswith("Dialogue:"):
                continue
            body = ln.split(",", 9)[9]
            if "\\p1" in body:
                continue
            span = secs(ln.split(",")[2]) - secs(ln.split(",")[1])
            raw = re.sub(r"^\{[^}]*\}", "", body)
            rows_n = raw.count("\\N") + 1          # 先数行，再剥 `\N`——顺序反了就永远是 1 行
            text = raw.replace("\\N", "")
            if preset != "single":
                # 歌词档的 `\move` 是「换句/换行整块上滚」（每次 600ms），不是横滚，
                # 「滚得完」这条判据不适用它——那一档靠窗口与折行保证不丢字。
                print("  %3d 字 / %d 行，句长 %.1f 秒" % (len(text), rows_n, span))
                continue
            mv = MV.search(body)
            if not mv:
                print("  %3d 字：装得下，静止居中（句长 %.1f 秒）" % (len(text), span))
                continue
            hold, end = int(mv.group(5)) / 1000.0, int(mv.group(6)) / 1000.0
            residual_ms = int(mv.group(6)) - int(round(span * 1000))
            ok = "滚得完" if end <= span + TAIL_TOLERANCE else "**滚不完**"
            print("  %3d 字：句首静止 %5.1f / 滚动 %5.1f / 终点 %5.1f ≤ 句长 %5.1f → %s"
                  % (len(text), hold, end - hold, end, span, ok))
            print("        原始：start=%s end=%s  t1=%sms t2=%sms  差=%+d ms（时间戳取整残差）"
                  % (ln.split(",")[1], ln.split(",")[2], mv.group(5), mv.group(6),
                     residual_ms))
            tw = S.text_px_width(text, g["size"])
            print("        文字宽 %.0f px、位移 %.0f px、首停占比 %.3f（框宽/文字宽）"
                  % (tw, tw - (g["x2"] - g["x1"]), (g["x2"] - g["x1"]) / tw))


if __name__ == "__main__":
    main()
