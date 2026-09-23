#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：模型能否稳定输出「结构化定点补丁」。

要回答的只有一个问题——把回灌从「全篇重出」改成「只回改动句」，
模型撑不撑得住这个契约。测三件事：

  1. 格式合规：在约束解码（json_schema）下是否总能给出合法 JSON；
  2. 落点正确：edits 里的句号是否都落在门禁点名的那几句上（会不会顺手改别的）；
  3. 增删倾向：补丁结构本身没有增删字段，唯一变相增删的路径是把两句塞进
     一个 text（顶破句长上限）或拆成两个同号 edit。

两种提示词形态各跑 3 次：
  A 只给被点句 + 前后各 2 句窗口（省 token 的那条路）
  B 给全篇 + 问题清单（回到今天的老路，只换输出格式）

用法：python tools/probes/probe_patch.py [次数]
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import script_engine as SE                      # noqa: E402
from podcast_maker.llm_client import LLMClient                     # noqa: E402
from podcast_maker.config_manager import ConfigManager             # noqa: E402

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
SCRIPT_PATH = os.path.join(ROOT, "projects", "20260912-235659", "脚本", "1.json")

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "speaker": {"type": "string", "enum": ["A", "B"]},
                    "text": {"type": "string"},
                    "emotion": {"type": "string", "enum": SE.EMOTION_TAGS},
                },
                "required": ["index", "text"],
            },
        },
    },
    "required": ["edits"],
}

PATCH_SYSTEM = """你是播客脚本的定点修补者。只输出 JSON，不要任何其它文字。

输出格式：
{"edits": [{"index": 58, "speaker": "A", "text": "改好后的整句文本", "emotion": "平静"}]}

铁律：
- **只允许替换**被点名句子的内容。不允许新增句子，不允许删除句子，行数一个字都不能变。
- 只把**需要改的句子**写进 edits，没被点名的句子一个字都不许动，也不要出现在 edits 里。
- text 必须是**整句替换后的完整文本**，不是片段，也不要把两句并进一句。
- 每句 8–40 字。过长只能精简，不许拆句；过短只能就地补内容，不许并句。
- emotion 只能取：%s""" % "、".join(SE.EMOTION_TAGS)


def build_patch_user(script, window_lines, feedback, full):
    """window_lines：要改的句号集合。full=True 时附全篇，否则只给前后 2 句窗口。"""
    if full:
        body = "\n".join("%d. [%s] %s" % (i + 1, s["speaker"], s["text"])
                         for i, s in enumerate(script))
        ctx = "==== 全篇脚本（共 %d 句）====\n%s" % (len(script), body)
    else:
        keep = set()
        for ln in window_lines:
            for k in range(max(1, ln - 2), min(len(script), ln + 2) + 1):
                keep.add(k)
        body = "\n".join("%d. [%s] %s" % (k, script[k - 1]["speaker"], script[k - 1]["text"])
                         for k in sorted(keep))
        ctx = "==== 相关上下文（只列了要改的句子及其前后各 2 句）====\n%s" % body
    return "==== 要改的地方 ====\n%s\n\n%s\n\n现在输出 JSON。" % (feedback, ctx)


def probe_lines(problems):
    """从门禁问题里收集被点名的句号。"""
    lines = set()
    for p in problems:
        for h in (p.get("hits") or []):
            lines.add(h["line"])
        for h in (p.get("too_long") or []) + (p.get("too_short") or []):
            lines.add(h["line"])
    return lines


def check(script, edits, target_lines):
    """收敛成一个判据：补丁能不能无损落回原稿，且只动了点名的句。"""
    got = []
    for e in edits:
        if not isinstance(e, dict):
            return False, "edits 里有非对象元素"
        idx = e.get("index")
        if not isinstance(idx, int):
            return False, "index 不是整数：%r" % (idx,)
        if not 1 <= idx <= len(script):
            return False, "index 越界：%d（全篇共 %d 句）" % (idx, len(script))
        got.append(idx)
    dup = [i for i in set(got) if got.count(i) > 1]
    if dup:
        return False, "同一句给了多个 edit（变相拆句/并句）：%s" % dup
    outside = sorted(i for i in got if i not in target_lines)
    if outside:
        return False, "动了没被点名的句：%s" % outside
    bad_len = [i for i in got
               if not 8 <= len([e for e in edits if e["index"] == i][0].get("text", "")) <= 40]
    if bad_len:
        return False, "改完句长仍越界：%s" % [
            (i, len([e for e in edits if e["index"] == i][0].get("text", ""))) for i in bad_len]
    return True, "只改 %d 句，落点全中" % len(got)


def main():
    cm = ConfigManager()
    cfg = cm.data()
    script = json.load(io.open(SCRIPT_PATH, encoding="utf-8"))
    n = len(script)

    # 注入三处真问题：一处禁用词、一处过长、一处过短。
    script[29]["text"] = "你一定" + script[29]["text"]
    script[49]["text"] = (script[49]["text"] + "，顺带把这句话拉长到超出单句上限的口径为止看看会怎样")[:46]
    script[69]["text"] = script[69]["text"][:5]

    report = SE.gate_generate(script, cfg, SE.resolve_paradigm(None, cfg))
    problems = [i for i in report["items"]
                if not i["ok"] and not i.get("soft") and not i.get("advisory")]
    target = probe_lines(problems)
    feedback = SE._build_feedback(problems)

    print("全篇 %d 句；门禁未过 %d 项；被点名的句：%s" % (n, len(problems), sorted(target)))
    print("-" * 78)
    print(feedback)
    print("-" * 78)

    llm = LLMClient(backend=cfg.get("llm.backend"), base_url=cm.resolve_base_url(),
                    api_key=cfg.get("llm.api_key"), model=cfg.get("llm.model"),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)))

    stats = {}
    for variant, full in (("A 窗口", False), ("B 全篇", True)):
        stats[variant] = {"ok": 0, "fail": []}
        for r in range(RUNS):
            user = build_patch_user(script, target, feedback, full)
            print("\n[%s 第 %d 次] 提示词 %d 字" % (variant, r + 1, len(user)), flush=True)
            try:
                raw, meta = llm.chat(
                    [{"role": "system", "content": PATCH_SYSTEM},
                     {"role": "user", "content": user}],
                    temperature=0.3, max_tokens=4096, json_schema=PATCH_SCHEMA)
            except Exception as e:
                print("   调用失败：%s" % e)
                stats[variant]["fail"].append("调用失败：%s" % e)
                continue
            s = raw.find("{"); e2 = raw.rfind("}")
            if s == -1 or e2 == -1:
                print("   格式不合规：未找到 JSON")
                stats[variant]["fail"].append("未找到 JSON")
                continue
            try:
                data = json.loads(raw[s:e2 + 1])
            except json.JSONDecodeError as ex:
                print("   格式不合规：%s" % ex)
                stats[variant]["fail"].append("JSON 非法：%s" % ex)
                continue
            edits = data.get("edits")
            if not isinstance(edits, list) or not edits:
                print("   edits 缺失或为空")
                stats[variant]["fail"].append("edits 缺失或为空")
                continue
            ok, why = check(script, edits, target)
            print("   %s  %s  edits=%d  返回 %d 字"
                  % ("PASS" if ok else "FAIL", why, len(edits), len(raw)))
            for e in edits[:8]:
                print("      #%s [%s] %s" % (e.get("index"), e.get("speaker", "-"),
                                             (e.get("text") or "")[:44]))
            if ok:
                stats[variant]["ok"] += 1
            else:
                stats[variant]["fail"].append(why)

    print("\n" + "=" * 78)
    for variant in stats:
        print("%s：%d/%d 通过" % (variant, stats[variant]["ok"], RUNS))
        for f in stats[variant]["fail"]:
            print("    未过原因：%s" % f)


if __name__ == "__main__":
    main()
