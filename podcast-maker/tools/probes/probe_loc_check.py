# -*- coding: utf-8 -*-
"""实测：结构读出来之后，每条单元的位置与章节路径落位是否正确。

三件事一起验：
  1. 每条都有起止行（`end`）与所属章节链（`path`）；
  2. 结构自算的位置与**取料**实际切走的范围一致（两处口径不能漂）；
  3. 父级链指向的外层标题确实存在于本文。

只用真实素材、只读不写，不调模型。
"""
import random
import sys

sys.path.insert(0, ".")
from podcast_maker import ingest, probe                          # noqa: E402

SRC = "projects/_sources/20260912-121051/s1.md"

text = open(SRC, encoding="utf-8").read()
ir = probe.scan(text)
segs = ir["segments"]

print("素材：%s（%.0f KB）" % (SRC, len(text.encode("utf-8")) / 1024.0))
print("段落数：%d ｜ 兜底判定的切分单位：H%s" % (len(segs), ir["unit_level"]))
print("各层条数（含附录与空壳）：%s" % dict(sorted(ir["level_counts"].items())))
print("各层**在播且切得出正文**的条数：%s" % probe._unit_cost(segs))
print()

print("前 8 条的落位：")
print("  层级  位置           字数    所属 / 标题")
for s in segs[:8]:
    print("  H%-3s %-14s %6d   %s「%s」"
          % (s["level"], probe.loc_text(s), int(s["chars"]),
             (s.get("path") + " › ") if s.get("path") else "（顶层）",
             (s["title"] or "")[:44]))
print()

# ---- 与取料口径对账 ----
random.seed(7)
sample = random.sample(segs, min(10, len(segs)))
bad = []
for s in sample:
    body, meta = ingest.slice_by_anchor(text, s["title"], [], line=s.get("line"))
    ok = (int(meta["line_start"]) == int(s["line"]) + 1
          and int(meta["line_end"]) == int(s["end"]))
    if not ok:
        bad.append(s["title"])
    print("  %-32s 结构 %-14s ｜ 取料 第 %d–%d 行  %s"
          % ((s["title"] or "")[:32], probe.loc_text(s),
             meta["line_start"], meta["line_end"], "✓" if ok else "✗ 不一致"))
print()
print("位置对账：%s（抽样 %d 条）"
      % ("全部一致" if not bad else "%d 处不一致：%s" % (len(bad), "、".join(bad[:5])),
         len(sample)))
print()

# ---- 父级链 ----
h1 = {s["title"] for s in segs if int(s["level"]) == 1}
h2 = [s for s in segs if int(s["level"]) == 2 and s.get("path")]
orphan = [s["title"] for s in h2 if s["path"].split(" › ")[0] not in h1]
print("H2 带所属的：%d 条 ｜ 所属顶层不在 H1 标题里的：%d 条"
      % (len(h2), len(orphan)))
if orphan:
    print("  例：%s" % "、".join(orphan[:3]))
depth = 0
for s in segs:
    p = s.get("path") or ""
    if p:
        depth = max(depth, len(p.split(" › ")))
print("章节路径最深层数：%d" % depth)
print()

# ---- classify 的提示词：文体判据是否真的进去了 ----
from podcast_maker import paradigms                            # noqa: E402
from podcast_maker.config_manager import ConfigManager          # noqa: E402


class _Spy:
    def __init__(self):
        self.prompt = ""

    def chat(self, messages, **kw):
        self.prompt = messages[1]["content"]
        return ('{"kind":"methodology","unit_level":1,'
                '"reason":"按文体判据判"}'), {}


spy = _Spy()
probe.classify(ir, spy, ConfigManager(), paradigms.PARADIGMS)
need = ["凝缩单位", "切分依据", "整合依据", "重点判据", "推进方式", "层级期望"]
miss = [k for k in need if k not in spy.prompt]
print("文体判据进提示词：%s" % ("六项齐全" if not miss else "缺 " + "、".join(miss)))
print("带上了「选细的代价」：%s" % ("真正要凝缩的条数" in spy.prompt))
print("没有命令期数：%s" % ("正好排出" not in spy.prompt))
print("结构按层级列出：%s" % ("H1（共" in spy.prompt and "H2（共" in spy.prompt))
print()
print("---- classify 提示词（前 1200 字）----")
print(spy.prompt[:1200])
