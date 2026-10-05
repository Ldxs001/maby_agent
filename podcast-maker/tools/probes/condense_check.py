#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真跑一次凝缩，看新口径产出的骨架长什么样。

只调一次（一个单元），不改任何项目文件。判据：
- 有没有留下"说清了什么、反对什么"这类可判断的陈述；
- 有没有该有的细节区分（数字、条件、边界、反例），而不是一句"讲了 X 的重要性"；
- 格式对不对（示例有没有起作用）；
- 代价多大（耗时、推理 token）。
"""

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import (config_manager, ingest, paradigms, planner, probe,
                          source_store)
from podcast_maker.llm_client import LLMClient


def main():
    base, pid, sid = "projects", "20260911-200219", "s1"
    cfg = config_manager.ConfigManager().data()
    llm = LLMClient(backend=cfg.get("llm.backend"),
                    base_url=config_manager.resolve_base_url(cfg),
                    api_key=cfg.get("llm.api_key") or "",
                    model=cfg.get("llm.model"),
                    timeout=cfg.get("llm.timeout"))
    text = source_store.read_source(base, pid, sid)
    marks = source_store.marks_of(base, pid, sid)
    ir = probe.scan(text, marks, probe.HEAD_CHARS)
    ir["kind"] = "methodology"
    units = planner.units_of({sid: ir}, probe.capacity(cfg))
    print("取料单元 %d 个；原文 %d 字；模型 %s"
          % (len(units), len(text), cfg.get("llm.model")), flush=True)

    # 挑一个中位偏小的单元：够典型，又不至于等到天亮
    ordered = sorted(units, key=lambda u: int(u[1].get("chars") or 0))
    sid_u, unit = ordered[len(ordered) // 3]
    body, _m = ingest.slice_by_anchor(text, unit["title"], marks,
                                      line=unit.get("line"))
    print("这一节：%s（%d 字，切出 %d 字，行 %s）"
          % (unit["title"], int(unit.get("chars") or 0), len(body),
             unit.get("line")), flush=True)

    rule = paradigms.get(unit.get("kind") or "methodology")["condense"]
    t0 = time.time()
    raw, meta = llm.chat(
        [{"role": "system", "content": probe.CONDENSE_SYSTEM},
         {"role": "user", "content": probe._condense_prompt(
             unit["title"], rule, body)}],
        temperature=0.1,
        max_tokens=int(cfg.get("llm.max_tokens", 8192)),
        json_schema=probe.CONDENSE_SCHEMA)
    dt = time.time() - t0

    obj = probe._json_obj(raw, "凝缩")
    print("\n耗时 %.1f 秒 / 推理 token %s / 模型 %s"
          % (dt, (meta or {}).get("reasoning_tokens"), (meta or {}).get("model")),
          flush=True)
    print("\n原文长度 %d 字 → 凝缩 %d 字（压到 %.0f%%）"
          % (len(body),
             len(obj.get("gist", "")) + sum(len(p) for p in obj.get("points") or []),
             100.0 * (len(obj.get("gist", ""))
                      + sum(len(p) for p in obj.get("points") or []))
             / max(1, len(body))), flush=True)
    print("\n=== 凝缩结果 ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
