#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针二：把问题集压硬，只验窗口模式稳不稳。

第一轮已经给出方向（窗口 3/3、全篇 1/3），但样本太薄，且问题太"温和"——
只有三处、全是纯形式项。这一轮换成：

  - 五句过短（要就地扩写，最容易诱发"并句"这种变相增删）
  - 一处禁用词
  - 一个语义检问题（带落点 line + quote，需要素材才修得了）

只跑窗口模式，跑 5 次。全部通过才敢说"稳定"。

用法：python tools/probes/probe_patch_hard.py [次数]
"""
import io
import json
import os
import sys

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from probe_patch import (PATCH_SCHEMA, PATCH_SYSTEM,            # noqa: E402
                          build_patch_user, check)
from podcast_maker import script_engine as SE                    # noqa: E402
from podcast_maker.llm_client import LLMClient                   # noqa: E402
from podcast_maker.config_manager import ConfigManager           # noqa: E402

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
SCRIPT_PATH = os.path.join(ROOT, "projects", "20260912-235659", "脚本", "1.json")
MATERIAL_PATH = os.path.join(ROOT, "projects", "20260912-235659", "素材", "s1.md")

SHORT_LINES = [12, 40, 68, 95, 130]
BANNED_LINE = 77


def main():
    cm = ConfigManager()
    cfg = cm.data()
    script = json.load(io.open(SCRIPT_PATH, encoding="utf-8"))

    # 五句过短：只留前 5 个字，逼模型就地扩写。
    for ln in SHORT_LINES:
        script[ln - 1]["text"] = script[ln - 1]["text"][:5]
    # 一处禁用词。
    script[BANNED_LINE - 1]["text"] = "所有" + script[BANNED_LINE - 1]["text"]

    report = SE.gate_generate(script, cfg, SE.resolve_paradigm(None, cfg))
    problems = [i for i in report["items"]
                if not i["ok"] and not i.get("soft") and not i.get("advisory")]

    # 内容检那一项是按新契约手搓的：issues 带落点。
    problems.append({
        "key": "check_semantic", "label": "语义检（LLM）", "level": "fail",
        "judge": "llm", "ok": False,
        "detail": "第 150 句：素材里没有这个数据",
        "issues": [{"line": 150, "quote": script[149]["text"][:12],
                    "problem": "这条数据在素材里找不到出处"}],
    })

    target = set(SHORT_LINES) | {BANNED_LINE, 150}
    feedback = SE._build_feedback(problems)
    material = io.open(MATERIAL_PATH, encoding="utf-8").read()[:6000]

    print("被点名的句：%s" % sorted(target))
    print("-" * 78)
    print(feedback)
    print("-" * 78)

    llm = LLMClient(backend=cfg.get("llm.backend"), base_url=cm.resolve_base_url(),
                    api_key=cfg.get("llm.api_key"), model=cfg.get("llm.model"),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)))

    ok_n = 0
    fails = []
    for r in range(RUNS):
        user = build_patch_user(script, target, feedback, full=False)
        user = "==== 素材（供判断依据，不要照抄）====\n%s\n\n%s" % (material, user)
        print("\n[第 %d 次] 提示词 %d 字" % (r + 1, len(user)), flush=True)
        try:
            raw, meta = llm.chat(
                [{"role": "system", "content": PATCH_SYSTEM},
                 {"role": "user", "content": user}],
                temperature=0.3, max_tokens=4096, json_schema=PATCH_SCHEMA)
        except Exception as e:                                     # noqa: BLE001
            print("   调用失败：%s" % e)
            fails.append("调用失败：%s" % e)
            continue
        s = raw.find("{"); e2 = raw.rfind("}")
        if s == -1 or e2 == -1:
            print("   未找到 JSON（返回 %d 字，开头：%s）" % (len(raw), raw[:80].replace("\n", " ")))
            fails.append("未找到 JSON")
            continue
        try:
            data = json.loads(raw[s:e2 + 1])
        except json.JSONDecodeError as ex:
            print("   JSON 非法：%s" % ex)
            fails.append("JSON 非法")
            continue
        edits = data.get("edits")
        if not isinstance(edits, list) or not edits:
            print("   edits 缺失或为空")
            fails.append("edits 缺失或为空")
            continue
        good, why = check(script, edits, target)
        print("   %s  %s  edits=%d  返回 %d 字"
              % ("PASS" if good else "FAIL", why, len(edits), len(raw)))
        for e in edits:
            print("      #%s %s" % (e.get("index"), (e.get("text") or "")[:46]))
        if good:
            ok_n += 1
        else:
            fails.append(why)

    print("\n" + "=" * 78)
    print("窗口模式（硬问题集）：%d/%d 通过" % (ok_n, RUNS))
    for f in fails:
        print("    未过原因：%s" % f)


if __name__ == "__main__":
    main()
