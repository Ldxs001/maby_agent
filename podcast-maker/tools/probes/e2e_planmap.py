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

"""端到端实跑：结构读出 → 逐单元凝缩 → 分组出图 → 按落点取原文 → 交写作。

用真实 LLM（配置里的后端与模型），素材是本目录现造的短稿：有子节的章、
附录、同名标题各一处——这三样正是「字数口径」与「落点定位」出过问题的地方。

跑法：python tools/probes/e2e_planmap.py [--model 模型名]
"""

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import (config_manager, ingest, llm_client,           # noqa: E402
                           planner, probe, project_store as P,
                           script_engine, source_store as S)

BOOK = """# 第一篇 怎么看

这一篇讲的是看事情的方法。

## 第一章 链与两头
每个判断都有两头的约束，中间是链。链断了，两头就各说各话。
我们先把约束写下来，再谈怎么接。

## 第二章 位置比名字重要
名字会撞，位置不会。同一本书里两节都叫「小结」是常态，
按名字找只会找到靠前那一条。

# 第二篇 怎么做

这一篇讲落地。

## 第一章 先读结构
结构在稿子里，不在模型脑子里。能读出来的就不要问。

## 第二章 小结
这一节讲的是把每一步的产物落盘。

# 第三篇 怎么不出错

## 第一章 小结
这一节讲的是失败要响，不要静默。

# 附录 A

## 甲表
表格内容，不该被排进节目。
"""


def main():
    model = ""
    argv = sys.argv[1:]
    if argv and argv[0] == "--model" and len(argv) > 1:
        model = argv[1]

    cfg = config_manager.ConfigManager().data()
    if model:
        cfg["llm.model"] = model
    llm = llm_client.LLMClient(
        backend=cfg.get("llm.backend", "lm-studio"),
        base_url=cfg.get("llm.base_url", ""),
        api_key=cfg.get("llm.api_key", ""),
        model=cfg.get("llm.model", ""),
        timeout=int(cfg.get("llm.timeout", 1800)))
    ok, res = llm.test_connection()
    print("后端：%s / 模型 %s" % ("可用" if ok else "不可用", cfg.get("llm.model")))
    if not ok:
        print(res)
        return 1

    base = tempfile.mkdtemp(prefix="pm_e2e_")
    try:
        item = P.create(base, "端到端", "mapped")
        pid = item["id"]
        S.add_source(base, pid, "书.md", BOOK)
        t0 = time.time()
        res = planner.plan_map(base, pid, item, cfg, llm,
                               log=lambda m: print("  · " + m))
        print("排图耗时 %.1f 秒" % (time.time() - t0))

        ir = probe.load(base, pid, "s1")
        print("\n结构：%d 段 / 层级 %s / 切分单位 H%s / 有效正文 %d / 一期容量 %d"
              % (len(ir["segments"]), ir["level_counts"], ir["unit_level"],
                 ir["body_chars"], ir["capacity"]))
        units = probe.pick_units(ir, ir["capacity"])
        print("取料单元 %d 个：" % len(units))
        for s in units:
            print("  H%d「%s」%d 字 → %s"
                  % (s["level"], s["title"], s["chars"], s.get("gist", "")))

        print("\n地图 %d 期：" % len(res["episodes"]))
        for e in res["episodes"]:
            print("  第 %s 期「%s」%d 字" % (e["no"], e["title"], e["chars"]))
            print("      主旨：%s" % e["gist"])
            for p in e["points"]:
                print("      要点：%s" % p)
            print("      落点：%s"
                  % "、".join("%s@%s+%s" % (r["anchor"], r["source"], r["line"])
                             for r in e["refs"]))
        if res["warnings"]:
            print("\n告警：")
            for w in res["warnings"]:
                print("  ! " + w)

        # 落点取原文：必须切得出、且取到的是同名标题里正确的那一条
        first = res["episodes"][0]
        material, meta = S.compose(base, pid, first["refs"])
        print("\n第 1 期取料 %d 字（%d 节，未截断，truncated=%s）"
              % (meta["chars"], len(meta["refs"]), meta["truncated"]))
        print("取料开头：%s" % material[:120].replace("\n", " / "))
        dup = [r for r in first["refs"] if r["anchor"] == "第一章 小结"]
        if dup:
            body, _m = ingest.slice_by_anchor(BOOK, dup[0]["anchor"],
                                              line=dup[0]["line"])
            print("同名标题按行号取到的正文：%s" % body.split("\n")[-1])
        print("附录是否入图：%s"
              % ("是（错）" if any(r["anchor"].startswith("甲表")
                                for e in res["episodes"] for r in e["refs"])
                 else "否（对）"))

        # 交给写作：提示词里必须带上主旨与取材单元
        sys_prompt = script_engine.build_system_prompt(
            cfg, "science", 4000, 60,
            project={"episode_no": first["no"], "title": first["title"],
                     "gist": first["gist"], "points": first["points"],
                     "sources": [r["anchor"] for r in first["refs"]]})
        print("\n写作提示词（本期计划段）：")
        for line in sys_prompt.split("## 本期计划")[1].split("\n\n")[0].strip().split("\n"):
            print("  " + line)
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
