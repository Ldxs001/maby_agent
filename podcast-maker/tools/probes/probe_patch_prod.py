#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针三：直接跑**生产代码**那条定点修补链，顺带把每次调用的 token 账摊开。

前两个探针用的是手搓的提示词与校验，验的是"这条路能不能走"。这一个不再手搓：
改用 script_engine 里的真函数（patch_targets / _patch_feedback / build_patch_prompt /
parse_patch / apply_patch），跑完再把稿子送回真门禁，看形式项是不是真的过了。

同时把每次调用的 usage 打印出来——prompt / completion / reasoning 三个数，
不用再靠"时间乘速度"倒推。

用法：python tools/probes/probe_patch_prod.py [次数]
"""
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import script_engine as SE                    # noqa: E402
from podcast_maker.llm_client import LLMClient                   # noqa: E402
from podcast_maker.config_manager import ConfigManager           # noqa: E402

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
P = os.path.join(ROOT, "projects", "20260912-235659", "脚本", "1.json")
MAT = os.path.join(ROOT, "projects", "20260912-235659", "素材", "s1.md")

SHORT = [12, 40, 68, 95, 130]
BANNED = 77
SEMANTIC = 150


def main():
    cm = ConfigManager()
    cfg = cm.data()
    script = json.load(io.open(P, encoding="utf-8"))
    for ln in SHORT:
        script[ln - 1]["text"] = script[ln - 1]["text"][:5]
    script[BANNED - 1]["text"] = "所有" + script[BANNED - 1]["text"]

    card = SE.resolve_paradigm(None, cfg)
    report = SE.gate_generate(script, cfg, card)
    # 内容检那一项按新契约手搓（真调一次内容检也行，但那要另花一次调用）。
    report["items"].append({
        "key": "check_semantic", "label": "语义检（LLM）", "level": "fail",
        "judge": "llm", "ok": False, "detail": "第 150 句：素材里没有这个数据",
        "issues": [{"line": SEMANTIC, "quote": script[SEMANTIC - 1]["text"][:10],
                    "problem": "这条数据在素材里找不到出处"}]})
    report = SE._summarize(report["items"], cfg)

    targets, refull, manual = SE.patch_targets(report, len(script))
    print("全篇 %d 句；可定点 %d 句 %s；只能重出 %d 项；交人工 %d 项"
          % (len(script), len(targets), sorted(targets), len(refull), len(manual)))
    feedback = SE._patch_feedback(targets)
    need_mat = SE._needs_material(report)
    user = SE.build_patch_prompt(script, targets, feedback,
                                 material=io.open(MAT, encoding="utf-8").read()
                                 if need_mat else "")
    print("提示词 %d 字（带素材：%s）" % (len(user), need_mat))
    print("=" * 78)

    llm = LLMClient(backend=cfg.get("llm.backend"), base_url=cm.resolve_base_url(),
                    api_key=cfg.get("llm.api_key"), model=cfg.get("llm.model"),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)))

    ok_n = 0
    for r in range(RUNS):
        print("\n[第 %d 次]" % (r + 1), flush=True)
        try:
            raw, meta = llm.chat(
                [{"role": "system", "content": SE.patch_system()},
                 {"role": "user", "content": user}],
                temperature=0.3, max_tokens=32768, json_schema=SE.patch_schema())
        except Exception as e:                                    # noqa: BLE001
            print("   调用失败：%s" % e)
            continue
        u = meta.get("usage") or {}
        print("   usage：prompt=%s completion=%s reasoning=%s total=%s"
              % (u.get("prompt_tokens"), u.get("completion_tokens"),
                 (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                 u.get("total_tokens")))
        print("   返回 %d 字符（答案部分）" % len(raw))
        try:
            edits = SE.parse_patch(raw)
            new = SE.apply_patch(script, edits, targets)
        except SE.ScriptError as e:
            print("   补丁没法落地：%s" % e)
            continue
        done = len({e["index"] for e in edits})
        print("   落地成功：改了 %d 句（点名 %d 句，%s）"
              % (done, len(targets), "全覆盖" if done == len(targets) else
                 "漏 %s" % sorted(set(targets) - {e["index"] for e in edits})))
        print("   行数：%d → %d" % (len(script), len(new)))
        # 生产路径里，补丁落地之后只有「压连续句 → 归一化」两步定型；片头尾自 v0.18.0
        # 起由 `glue_intro_outro` 在**落盘那一刻**粘上，不进循环、也不进门禁——
        # 从前这里多跑一步 `pin_intro_outro`（它当时是替换首末句），回门禁才会老报
        # 「首句/末句未命中」。
        SE.enforce_max_run(new, SE.paradigms.max_run_of(card))
        new = SE.normalize_script(new, cfg)
        after = SE.gate_generate(new, cfg, card)
        bad = [i for i in after["items"]
               if not i["ok"] and not i.get("soft") and not i.get("advisory")]
        print("   回门禁：%s%s" % ("形式项全过" if not bad else "仍未过",
                                 "（%s）" % "、".join(
                                     "%s %s" % (i["label"], i.get("detail", ""))
                                     for i in bad) if bad else ""))
        if done == len(targets) and not bad:
            ok_n += 1

    print("\n" + "=" * 78)
    print("生产路径：%d/%d 次「全覆盖 + 回门禁全过」" % (ok_n, RUNS))


if __name__ == "__main__":
    main()
