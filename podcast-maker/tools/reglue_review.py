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

"""前期回顾重拼 —— 把已落盘脚本里的「回顾」段按**当前模板**重拼一遍。

**为什么需要它**：回顾是「脚本定稿那一刻」由程序逐字粘上去的（`script_engine.glue_intro_outro`），
**出片不回头重拼**。所以改了模板、重新合成，字一个都不会变——盘上已有的脚本一律是
老样子，除非专门重拼一次。点名重写时漏掉的期就是这么留下来的。

**走的是现成代码路径**：`pipeline.review_rows` 出句（含引用值各自剃尾）、
`script_engine._fill_line_seconds` 补时长（与定稿那一刻同一套口径）。不在这里另写一份
拼装规则——两份规则迟早对不上。

**只动「回顾」段**：片头、承接、正文、收束一句不碰，改后逐字节回读对账。改前落备份到
`projects/<id>/过程备份/脚本/<期>.<时间戳>.json`。

用法：
    python tools/reglue_review.py --project 20260917-015336 --episodes 2,2a,2b,3 --dry-run
    python tools/reglue_review.py --project 20260917-015336 --episodes 2 --yes
"""

import argparse
import datetime
import io
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import layout, pipeline, project_store, script_engine  # noqa: E402
from podcast_maker.config_manager import INTRO_TAG, REVIEW_TAG            # noqa: E402

BACKUP_DIR = "过程备份"


def load_script(path):
    with io.open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit("%s 不是句子数组，拒绝对它动手。" % path)
    return data


def review_span(script):
    """「回顾」段的下标区间 `[i, j)`。没有返回 `None`；不连续就报错（不猜）。"""
    idx = [k for k, line in enumerate(script)
           if str((line or {}).get("emotion") or "") == REVIEW_TAG]
    if not idx:
        return None
    if idx != list(range(idx[0], idx[0] + len(idx))):
        raise SystemExit("回顾句不连续，下标 %s；这种形状没料到，不动手。" % idx)
    return idx[0], idx[-1] + 1


def preview(lines, limit=6, width=110):
    out = []
    for line in lines[:limit]:
        text = str(line.get("text") or "")
        if len(text) > width:
            text = text[:width] + "…"
        out.append("      %s  %s" % (line.get("speaker"), text))
    if len(lines) > limit:
        out.append("      …（共 %d 句）" % len(lines))
    return "\n".join(out)


def reglue(base, item, no, cfg, dry, say):
    root = layout.project_dir(base, item.get("id") or "")
    path = layout.script_file(root, no)
    if not os.path.exists(path):
        say("  [%s] 跳过：脚本不存在" % no)
        return None
    old = load_script(path)
    rows = [dict(r) for r in pipeline.review_rows(
        base, item, no, {"intro_outro.review": True}, log=lambda m: None)]
    if not rows:
        say("  [%s] 跳过：按当前规则这一期没有回顾可拼（`review_rows` 返回空）" % no)
        return None

    span = review_span(old)
    if span:
        i, j = span
    else:
        # 没有回顾段时只往「片头第一句之后」插（与 `glue_intro_outro` 同一处位置）。
        # 首句不是片头就不敢插了——那说明这份脚本不是「片头 + 正文」的形状。
        if str((old[0] or {}).get("emotion") or "") != INTRO_TAG:
            raise SystemExit("[%s] 首句不是片头（emotion=%r），没有可锚定的插入位置；"
                             "不动手。" % (no, (old[0] or {}).get("emotion")))
        i, j = 1, 1

    new = old[:i] + rows + old[j:]
    new = script_engine._fill_line_seconds(new, cfg)

    say("  [%s] 回顾段：第 %d 句起，旧 %d 句 → 新 %d 句（脚本 %d 句 → %d 句）"
        % (no, i + 1, j - i, len(rows), len(old), len(new)))
    say("    旧：\n%s" % preview([dict(x) for x in old[i:j]]))
    say("    新：\n%s" % preview(rows))

    # 对账：除回顾段外必须逐字节相同（`_fill_line_seconds` 只给缺时长的句补，其余不动）
    head_ok = old[:i] == new[:i]
    tail_ok = old[j:] == new[i + len(rows):]
    if not (head_ok and tail_ok):
        raise SystemExit("[%s] 对账失败：回顾段以外的句子被改动了"
                         "（前段一致=%s ／ 后段一致=%s），已放弃写入。"
                         % (no, head_ok, tail_ok))
    say("    对账：回顾段以外 %d 句逐字节相同 ✓" % (len(old) - (j - i)))

    if dry:
        say("    （dry-run，未写入）")
        return new

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(root, BACKUP_DIR, "脚本")
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, "%s.%s.json" % (layout.safe_no(no), stamp))
    shutil.copy2(path, bpath)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 回读复校：写完再读一遍，与内存里的那份比
    back = load_script(path)
    if back != new:
        raise SystemExit("[%s] 写后回读与内存不一致，备份在 %s" % (no, bpath))
    say("    已写入 %s（备份 %s）" % (os.path.relpath(path, ROOT),
                                     os.path.relpath(bpath, ROOT)))
    return new


def main():
    ap = argparse.ArgumentParser(description="按当前模板重拼已落盘脚本的前期回顾。")
    ap.add_argument("--project", default="", help="项目 id；留空则要求盘上只有一个项目")
    ap.add_argument("--episodes", required=True, help="期号，逗号分隔，例如 2,2a,2b,3")
    ap.add_argument("--base", default=os.path.join(ROOT, "projects"), help="项目根目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写盘")
    args = ap.parse_args()

    say = print
    data = project_store._read(args.base)
    items = data.get("projects") or []
    if args.project:
        hit = [p for p in items if str(p.get("id")) == args.project]
    else:
        hit = items if len(items) == 1 else []
    if not hit:
        raise SystemExit("找不到项目 %r（盘上共 %d 个）。" % (args.project, len(items)))
    item = hit[0]

    from podcast_maker.config_manager import ConfigManager
    cfg = ConfigManager(ROOT)
    try:
        cfg.load()
    except Exception as e:                       # noqa: BLE001 —— 配置读不到也要能跑
        say("配置读取失败，按默认值继续：%s" % e)
    cfg = dict(project_store.apply_to_config(dict(cfg.data()), item))

    eps = [e.strip() for e in args.episodes.split(",") if e.strip()]
    say("项目 %s（%s）／ 期号 %s ／ %s"
        % (item.get("id"), item.get("name") or "", "、".join(eps),
           "试跑" if args.dry_run else "写入"))
    changed = 0
    for no in eps:
        if reglue(args.base, item, no, cfg, args.dry_run, say):
            changed += 1
    say("\n共 %d 期%s。" % (changed, "待写入" if args.dry_run else "已重拼"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
