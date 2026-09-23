#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针四：本轮两处改动在真模型上的行为验证。

一、内容检上约束解码（CHECK6_SCHEMA）
   它是七条提示词里唯一要求复杂结构的一条，也是唯一从前不带契约的一条。
   真调一次 check6_llm，看：回包能不能过、落点给不给得出、约束解码有没有被降级。

二、提示词分块改语汇（## / ==== → 【】+ 括注）
   块标记只是标记，但改的是模型读提示词的方式。真跑一次定点修补链，
   看落点还准不准。

用法：python tools/probes/probe_blocks_prod.py [次数]
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

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 1
PROJ = os.path.join(ROOT, "projects", "20260912-235659")
P = os.path.join(PROJ, "脚本", "1.json")
MAT = os.path.join(PROJ, "素材", "s1.md")

SHORT = [12, 40, 68, 95, 130]
BANNED = 77
SEMANTIC = 150


def _usage(meta):
    u = meta.get("usage") or {}
    return "prompt=%s completion=%s reasoning=%s" % (
        u.get("prompt_tokens"), u.get("completion_tokens"),
        (u.get("completion_tokens_details") or {}).get("reasoning_tokens"))


def main():
    cm = ConfigManager()
    cfg = cm.data()
    script = json.load(io.open(P, encoding="utf-8"))
    material = io.open(MAT, encoding="utf-8").read()

    llm = LLMClient(backend=cfg.get("llm.backend"), base_url=cm.resolve_base_url(),
                    api_key=cfg.get("llm.api_key"), model=cfg.get("llm.model"),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)))

    print("=" * 78)
    print("【一】内容检（带 CHECK6_SCHEMA）")
    print("=" * 78)
    print("契约：required=%s" % SE.check6_schema()["required"])
    row = script[SEMANTIC - 1]["text"]
    report = SE.check6_llm(script, cfg, llm, material)
    for key, (state, detail, issues) in report.items():
        print("  %-14s %s" % (key, state))
        print("      detail：%s" % detail)
        for it in issues:
            print("      落点 line=%d | quote=%r | problem=%s"
                  % (it["line"], it["quote"][:20], it["problem"][:50]))
    if not any(issues for _s, _d, issues in report.values()):
        print("  （两项都没报问题清单）")
    print("  第 150 句原文：%s" % row[:40])

    print()
    print("=" * 78)
    print("【二】定点修补链（分块改语汇之后）")
    print("=" * 78)
    for ln in SHORT:
        script[ln - 1]["text"] = script[ln - 1]["text"][:5]
    script[BANNED - 1]["text"] = "所有" + script[BANNED - 1]["text"]

    card = SE.resolve_paradigm(None, cfg)
    rep = SE.gate_generate(script, cfg, card)
    rep["items"].append({
        "key": "check_semantic", "label": "语义检（LLM）", "level": "fail",
        "judge": "llm", "ok": False, "detail": "第 150 句：素材里没有这个数据",
        "issues": [{"line": SEMANTIC, "quote": script[SEMANTIC - 1]["text"][:10],
                    "problem": "这条数据在素材里找不到出处"}]})
    rep = SE._summarize(rep["items"], cfg)

    targets, refull, manual = SE.patch_targets(rep, len(script))
    print("全篇 %d 句；可定点 %d 句 %s；只能重出 %d 项；交人工 %d 项"
          % (len(script), len(targets), sorted(targets), len(refull), len(manual)))
    feedback = SE._patch_feedback(targets)
    need_mat = SE._needs_material(rep)
    user = SE.build_patch_prompt(script, targets, feedback,
                                 material=material if need_mat else "")
    print("提示词 %d 字（带素材：%s）" % (len(user), need_mat))
    print("块标记：%s" % " | ".join(
        b for b in ("【素材】", "【要改的地方】", "【相关段落】") if b in user))

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
        print("   usage：%s%s" % (_usage(meta),
                                 "（约束解码已降级）" if meta.get("degraded") else ""))
        print("   返回 %d 字符" % len(raw))
        try:
            edits = SE.parse_patch(raw)
            new = SE.apply_patch(script, edits, targets)
        except SE.ScriptError as e:
            print("   补丁没法落地：%s" % e)
            continue
        done = len({e["index"] for e in edits})
        missed = sorted(set(targets) - {e["index"] for e in edits})
        print("   落地：改了 %d 句（点名 %d 句，%s）"
              % (done, len(targets), "全覆盖" if not missed else "漏 %s" % missed))
        print("   行数：%d → %d" % (len(script), len(new)))
        SE.enforce_max_run(new, SE.paradigms.max_run_of(card))
        # 片头尾不再进循环：自 v0.18.0 起由 `glue_intro_outro` 在**落盘那一刻**粘上，
        # 门禁/补丁看的一直是正文。这里照生产路径走，所以不粘。
        new = SE.normalize_script(new, cfg)
        after = SE.gate_generate(new, cfg, card)
        bad = [i for i in after["items"]
               if not i["ok"] and not i.get("soft") and not i.get("advisory")]
        print("   回门禁：%s%s" % ("形式项全过" if not bad else "仍未过",
                                 "（%s）" % "、".join(
                                     "%s %s" % (i["label"], i.get("detail", ""))
                                     for i in bad) if bad else ""))
        if not missed and not bad:
            ok_n += 1

    print("\n" + "=" * 78)
    print("定点修补：%d/%d 次「全覆盖 + 回门禁全过」" % (ok_n, RUNS))


if __name__ == "__main__":
    main()
