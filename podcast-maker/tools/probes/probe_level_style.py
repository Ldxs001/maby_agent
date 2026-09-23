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

"""档位 × 风格倾向的同源验证（纯本地，不调模型）。

盯四件事：
  1. 密度行按档位换话术，且 none 档表头不再是「情绪密度」；
  2. 一份提示词里不会同时出现两套话术（旧毛病：一边禁止填、一边要求填）；
  3. 卡块收尾句也跟着档位换（none 档不再说「情绪标签」）；
  4. 约束解码的枚举按档位收窄，两条产出 emotion 的路（整篇 / 定点）一致。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import paradigms as PG, script_engine as SE      # noqa: E402

CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
       "tts.name_b": "小笔", "project.program_name": "示例节目"}

MOOD = ("感慨", "恍然", "好奇", "疑惑", "肯定", "轻松")


def density_row(text):
    for ln in text.splitlines():
        if ln.startswith("- 标签分布：") or ln.startswith("- 情绪密度："):
            return ln
    return "（没有密度行）"


def main():
    print("=" * 78)
    print("一、密度行按档位换话术（同一张卡 × 三个密度档）")
    print("=" * 78)
    for key in ("paper", "narrative"):                      # none / light
        card = PG.get(key)
        lv = PG.emotion_level_of(card)
        print("\n【%s】档位 = %s" % (card.get("label"), lv))
        for preset in ("argument", "science", "story"):      # 低 / 中 / 高
            p = SE.build_system_prompt(CFG, preset, 900, 24, paradigm=card)
            print("  %-8s %s" % (preset, density_row(p)))

    print()
    print("=" * 78)
    print("二、两套话术不共存 + 卡块收尾句（旧毛病现场）")
    print("=" * 78)
    heads = ("- 标签分布：", "- 情绪密度：")
    for key, expect in (("paper", "标签分布"), ("narrative", "情绪密度")):
        card = PG.get(key)
        lv = PG.emotion_level_of(card)
        hit = []
        for preset in ("argument", "science", "story"):
            p = SE.build_system_prompt(CFG, preset, 900, 24, paradigm=card)
            rows = [ln for ln in p.splitlines() if ln.startswith(heads)]
            other = ("情绪密度：" if expect == "标签分布" else "标签分布：")
            hit.append((preset, len(rows), "情绪标签" in p, other in p))
        print("\n【%s】档位 = %s；期望表头 = %s" % (card.get("label"), lv, expect))
        for preset, n, has_mood_tag, has_other in hit:
            print("  %-8s 密度行 %d 行 | 含「情绪标签」：%s | 混进另一档表头：%s"
                  % (preset, n, "是" if has_mood_tag else "否",
                     "是" if has_other else "否"))

    print()
    print("=" * 78)
    print("三、约束解码枚举按档位收窄")
    print("=" * 78)
    for lv in ("none", "light", None, "LIGHT"):
        s_enum = SE.script_schema(lv)["properties"]["lines"]["items"][
            "properties"]["emotion"]["enum"]
        p_enum = SE.patch_schema(lv)["properties"]["edits"]["items"][
            "properties"]["emotion"]["enum"]
        mood = [w for w in MOOD if w in s_enum]
        print("  档位=%-6s 整篇 %2d 个标签（心情词 %d 个：%s） | 定点 %2d 个 | 一致：%s"
              % (str(lv), len(s_enum), len(mood), "".join(mood) or "无",
                 len(p_enum), s_enum == p_enum))

    print()
    print("=" * 78)
    print("四、模板未被收窄污染（模块级常量仍是全量）")
    print("=" * 78)
    SE.script_schema("none")
    SE.patch_schema("none")
    t1 = SE.SCRIPT_SCHEMA["properties"]["lines"]["items"]["properties"]["emotion"]["enum"]
    t2 = SE.PATCH_SCHEMA["properties"]["edits"]["items"]["properties"]["emotion"]["enum"]
    print("  SCRIPT_SCHEMA 枚举 %d 个 | PATCH_SCHEMA 枚举 %d 个（应都是 16）"
          % (len(t1), len(t2)))

    print()
    print("=" * 78)
    print("五、定点修补提示词的 emotion 可填范围")
    print("=" * 78)
    for lv in ("none", "light"):
        ps = SE.patch_system(lv)
        line = [ln for ln in ps.splitlines() if ln.startswith("- emotion")]
        print("  档位=%-6s %s" % (lv, line[0] if line else "（没有这一行）"))

    print()
    print("=" * 78)
    print("六、档位越界的句子能不能落到定点修补")
    print("=" * 78)
    report = {"items": [{"key": "emotion_level", "ok": False, "lines": [3, 7]}]}
    targets, refull, manual = SE.patch_targets(report, 10, "none")
    print("  targets=%s  refull=%d  manual=%d" % (sorted(targets), len(refull), len(manual)))
    print("  改法：%s" % (targets.get(3) or ["（没落上）"])[0])


if __name__ == "__main__":
    main()
