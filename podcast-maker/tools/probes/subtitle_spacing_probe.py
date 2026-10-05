#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""字距规范化对**字幕断行**的影响 —— 只读探针，不写任何项目文件。

字距一改，字幕是最可能被连带的一层：折行按字数算，多一个空格就可能把切点挪一格。
这个探针把盘上全部脚本逐句过一遍：

  原文 → `subtitle_engine._caption_text`（缀说话人名）→ `wrap_balanced`
  字距 → 同上

比三样：**行数**（框高预算按行算）、**断词数**（`count_word_breaks`：一个西文 token
被拦腰切断算一处）、**每行是否超容量**（折行的硬保证）。行数变了但没断词＝排版动了，
可以接受；多出断词＝这次改动把字幕弄坏了，必须回头。

口径与出片一致：`max_chars` 按 `frame_of` 的几何现算（`build_ass` 里就是这一行），
轴取当前档位（横滚档不折行，本来就不受字距影响）。

用法：
    python tools/probes/subtitle_spacing_probe.py --project 20260917-015336
    python tools/probes/subtitle_spacing_probe.py            # 全部项目
"""

import argparse
import glob
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import script_engine, subtitle_engine    # noqa: E402
from podcast_maker.config_manager import ConfigManager      # noqa: E402

OUT = os.path.join(ROOT, "_smoke", "_bench", "subtitle_spacing.json")


def geometry(cfg):
    """`(每行容量, 轴)` —— 与 `build_ass` 同一行公式，不重新定义。"""
    _preset, opt = subtitle_engine.preset_of(cfg)
    axis = str(opt.get("axis", "y"))
    g = subtitle_engine.frame_of(cfg, int(cfg.get("video.width", 1920)),
                                int(cfg.get("video.height", 1080)), "")
    max_chars = max(8, (g["x2"] - g["x1"]) // max(1, g["size"]))
    return max_chars, axis


def main():
    ap = argparse.ArgumentParser(description="字距对字幕断行的影响（只读）")
    ap.add_argument("--project", default="", help="项目 id；留空＝全部项目")
    ap.add_argument("--base", default=os.path.join(ROOT, "projects"))
    args = ap.parse_args()

    cfg = dict(ConfigManager().data())
    try:
        ConfigManager(ROOT).load()
        cfg = dict(ConfigManager(ROOT).data())
    except Exception as e:                                  # noqa: BLE001
        print("配置读取失败，按默认值继续：%s" % e)
    max_chars, axis = geometry(cfg)
    show_name = subtitle_engine.speaker_name_shown(cfg)
    print("每行容量 %d 字 ／ 轴 %s ／ 缀说话人名 %s" % (max_chars, axis, show_name))

    data_files = sorted(glob.glob(os.path.join(
        args.base, args.project, "脚本", "*.json") if args.project
        else os.path.join(args.base, "*", "脚本", "*.json")))
    data_files = [p for p in data_files
                  if not os.path.basename(p).endswith((".form.json", ".plan.json"))]

    rows, changed, stat_old, stat_new = [], 0, {}, {}
    wrap_rows_old = wrap_rows_new = 0
    for p in data_files:
        b = os.path.basename(os.path.dirname(os.path.dirname(p))) + "/" + \
            os.path.basename(p)
        try:
            with io.open(p, encoding="utf-8") as f:
                script = json.load(f)
        except Exception as e:                              # noqa: BLE001
            print("  跳过 %s：%s" % (p, e))
            continue
        if not isinstance(script, list):
            continue
        for i, ln in enumerate(script):
            if not isinstance(ln, dict):
                continue
            text = str(ln.get("text") or "")
            if not text:
                continue
            new = script_engine.tidy_text(text)
            if new == text:
                continue
            changed += 1
            if axis != "y":
                continue
            cap_old = subtitle_engine._caption_text(cfg, ln, show_name)
            cap_new = subtitle_engine._caption_text(cfg, dict(ln, text=new),
                                                    show_name)
            r_old = subtitle_engine.wrap_balanced(cap_old, max_chars)
            r_new = subtitle_engine.wrap_balanced(cap_new, max_chars)
            w_old = subtitle_engine.count_word_breaks(cap_old, max_chars, axis)
            w_new = subtitle_engine.count_word_breaks(cap_new, max_chars, axis)
            over_new = [r for r in r_new if len(r) > max_chars]
            wrap_rows_old += len(r_old)
            wrap_rows_new += len(r_new)
            stat_old[b] = stat_old.get(b, 0) + w_old
            stat_new[b] = stat_new.get(b, 0) + w_new
            rows.append({
                "file": os.path.relpath(p, args.base), "index": i,
                "old": text, "new": new,
                "rows_old": len(r_old), "rows_new": len(r_new),
                "break_old": w_old, "break_new": w_new,
                "over_new": over_new,
                "wrap_old": r_old, "wrap_new": r_new,
            })

    print()
    print("=== 受影响句 %d 条（原文就已经合规格的不计）===" % changed)
    if axis != "y":
        print("横滚档不折行：字距只改屏幕上滚过去的字，不产生断点。")
    else:
        same = [r for r in rows if r["rows_old"] == r["rows_new"]]
        grew = [r for r in rows if r["rows_new"] > r["rows_old"]]
        shrank = [r for r in rows if r["rows_new"] < r["rows_old"]]
        print("折行行数：合计 %d -> %d；逐句比较 不变 %d ／ 多一行 %d ／ 少一行 %d"
              % (wrap_rows_old, wrap_rows_new, len(same), len(grew), len(shrank)))
        print("断词数：合计 %d -> %d（应只降不升）"
              % (sum(stat_old.values()), sum(stat_new.values())))
        print("超过每行容量的行：%d 行（折行的硬保证，应为 0）"
              % sum(len(r["over_new"]) for r in rows))

        for tag, group in (("多了一行", grew), ("少了一行", shrank)):
            if not group:
                continue
            print()
            print("--- %s（%d 句，抽样 6）---" % (tag, len(group)))
            for r in group[:6]:
                print("  [%s#%d] %d->%d 行"
                      % (r["file"], r["index"], r["rows_old"], r["rows_new"]))
                print("     旧 %s" % r["old"][:76])
                print("     新 %s" % r["new"][:76])

        worse = [r for r in rows if r["break_new"] > r["break_old"]]
        print()
        print("断词变多的句：%d 条" % len(worse))
        for r in worse[:6]:
            print("  [%s#%d] %d -> %d 处断词" % (r["file"], r["index"],
                                                 r["break_old"], r["break_new"]))
            print("     旧 %s" % r["old"][:76])
            print("     新 %s" % r["new"][:76])

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump({"max_chars": max_chars, "axis": axis, "changed": changed,
                   "rows_old": wrap_rows_old, "rows_new": wrap_rows_new,
                   "break_old": sum(stat_old.values()),
                   "break_new": sum(stat_new.values()), "items": rows},
                  f, ensure_ascii=False, indent=1)
        f.write("\n")
    print()
    print("明细写入 %s" % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
