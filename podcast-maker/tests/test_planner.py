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

"""期数地图：模型分组，代码把位置。

四条纪律，每条都对应一类会一路潜行到产物上的错：

1. 期号、落点、每期体量由代码编。落点是「素材 + 标题 + 行号」——同名标题也
   定得到具体那一条；让模型抄标题，抄错一条这一期就取不到料，而地图看着正常。
2. 分组的合法性由代码校验。模型漏掉的单元会被补成独立一期：漏掉的不是
   「少讲一点」，是那几节从此不在任何一期里。
3. 人定期数时以人为准。模型每次给的数都不一样，拿它当计划，进度永远对不上账。
4. 单元凝缩拿不到就停下。凝缩结果是分组的依据，没有它排出来的图是按字数硬切
   的——那种图看着完整，才是最难查的假成功。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import paradigms as _PG                               # noqa: E402
from podcast_maker import planner as PLN                                 # noqa: E402
from podcast_maker import probe                                          # noqa: E402
from podcast_maker import project_store as P                             # noqa: E402
from podcast_maker import source_store as S                              # noqa: E402
from podcast_maker.planner import PlanError                              # noqa: E402

BOOK = """# 第一章 起点

这一章讲事情的由来。

# 第二章 转折

这一章讲分岔路口。

# 第三章 收束

这一章讲如何收尾。
"""

BOOK2 = """# 其一 另一个视角

换一个角度看同一件事。

# 其二 补充材料

补上前面漏掉的一环。
"""


REF1 = [{"source": "s1", "anchor": "第一章 起点"}]   # 对得上 BOOK 的锚点


class FakeLLM(object):
    """只回一段预设文本的模型替身。调用参数一并留下，供断言提示词。

    替身要分辨三类调用，否则会答非所问：
    探查的分类（schema 是 `PROBE_SCHEMA`）、单元凝缩（`probe.CONDENSE_SCHEMA`）、
    分组与插入（各自的排图 schema）。共用一个替身而不分开，凝缩调用会拿到一份
    `{"episodes":[...]}`——看着像模型不会凝缩，实际是替身在乱答。
    """

    def __init__(self, reply, meta=None, kind=None, unit_level=None,
                 gist="本节主旨", failed_condense=False):
        self.reply = reply
        self.meta = meta or {}
        self.kind = kind
        self.unit_level = unit_level
        self.gist = gist
        self.failed_condense = failed_condense
        self.calls = []

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        import json
        self.calls.append({"messages": messages, "schema": json_schema,
                           "max_tokens": max_tokens, "temperature": temperature})
        if json_schema is probe.PROBE_SCHEMA:
            body = {"kind": self.kind or "auto", "reason": "替身判定"}
            if self.unit_level is not None:
                body["unit_level"] = self.unit_level
            return json.dumps(body, ensure_ascii=False), {"model": "fake-probe"}
        if json_schema is probe.CONDENSE_SCHEMA:
            if self.failed_condense:
                return "我不想凝缩", {"model": "fake-condense"}
            return json.dumps({"gist": self.gist, "points": ["重点甲", "重点乙"],
                               "concepts": ["概念甲"]},
                              ensure_ascii=False), {"model": "fake-condense"}
        return self.reply, dict(self.meta)

    @property
    def prompt(self):
        return "\n".join(m["content"] for m in self.calls[-1]["messages"])

    def _is_map(self, call):
        return call["schema"] not in (probe.PROBE_SCHEMA, probe.CONDENSE_SCHEMA)

    def map_calls(self):
        """排图/插入那一次调用（排除探查分类与单元凝缩）。"""
        for c in reversed(self.calls):
            if self._is_map(c):
                return c
        raise AssertionError("没有发生排图调用，只有探查/凝缩调用。")

    def map_calls_all(self):
        """全部排图/插入调用（排除探查分类与单元凝缩）。"""
        return [c for c in self.calls if self._is_map(c)]

    def condense_calls(self):
        return [c for c in self.calls if c["schema"] is probe.CONDENSE_SCHEMA]


class SeqLLM(FakeLLM):
    """按次序回不同文本的替身——给「第一次没拿到落点，第二次拿到」这类判据用。

    真实模型偶发不照清单里的标题抄，重试是常态；替身只回一段固定文本就测不到
    这件事。探查的分类与单元凝缩不消耗序号：否则每加一个单元，序号整体错一位。
    """

    def __init__(self, replies, kind=None):
        FakeLLM.__init__(self, "", kind=kind)
        self.replies = list(replies)
        self.used = 0

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        import json
        self.calls.append({"messages": messages, "schema": json_schema,
                           "max_tokens": max_tokens, "temperature": temperature})
        if json_schema is probe.PROBE_SCHEMA:
            return json.dumps({"kind": self.kind or "auto", "reason": "替身判定"},
                              ensure_ascii=False), {"model": "fake-probe"}
        if json_schema is probe.CONDENSE_SCHEMA:
            return json.dumps({"gist": "本节主旨", "points": [], "concepts": []},
                              ensure_ascii=False), {"model": "fake-condense"}
        i = min(self.used, len(self.replies) - 1)
        self.used += 1
        return self.replies[i], {"model": "fake"}


def groups_json(pairs):
    """把 (单元序号, 标题) 列表拼成模型该输出的 JSON 文本。"""
    import json
    return json.dumps({"episodes": [
        {"units": u, "title": t, "gist": "本期主旨", "points": ["要点一", "要点二"]}
        for u, t in pairs]}, ensure_ascii=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm_plan_")
        self.item = P.create(self.base, "书", "mapped")
        self.pid = self.item["id"]
        S.add_source(self.base, self.pid, "书.md", BOOK)
        self.cfg = {"script.map_max_episodes": 60,
                    "llm.temperature": 0.8, "llm.max_tokens": 4096}

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)


class TestJsonObject(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(PLN._json_object('{"a":1}', "x"), {"a": 1})

    def test_fenced(self):
        self.assertEqual(PLN._json_object('```json\n{"a":1}\n```', "x"), {"a": 1})

    def test_surrounded_by_prose(self):
        raw = '好的，这是计划：\n{"a":1}\n以上。'
        self.assertEqual(PLN._json_object(raw, "x"), {"a": 1})

    def test_garbage_raises(self):
        with self.assertRaises(PlanError):
            PLN._json_object("模型今天不想干活", "排地图")

    def test_malformed_raises(self):
        with self.assertRaises(PlanError):
            PLN._json_object('{"a":1,}', "排地图")


class TestPlanMap(Base):
    """排地图：模型分组，代码把位置。

    这套判据与从前那套的分别不在写法，在**谁持有决定权**。从前期数与落点都在
    模型手里：期数问模型（同一份素材两次排出 60 期和 5 期，两个都「合乎提示词」），
    落点也让模型抄（抄错一条这一期就取不到料）。现在期数由 `probe.capacity()`
    从目标时长与标准语速算出来，落点由分组结果直接取用，模型只决定哪些单元
    合成一期、以及这一期叫什么。
    """

    def plan(self, cfg=None, item=None, llm=None, log=None):
        # 不再需要「隔离的口径表」：容量由标准语速算出，与机器上的
        # calibration.json 无关，同一个断言在哪台机器上都是同一个数。
        return PLN.plan_map(self.base, self.pid, item or self.item,
                            cfg or self.cfg, llm, log=log)

    def test_episodes_numbered_by_code(self):
        llm = FakeLLM(groups_json([([1], "起点"), ([2], "转折"), ([3], "收束")]))
        res = self.plan(llm=llm)
        self.assertEqual([r["no"] for r in res["episodes"]], ["1", "2", "3"])
        self.assertEqual(res["episodes"][0]["title"], "起点")

    def test_schema_is_passed_for_constrained_decoding(self):
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        self.plan(llm=llm)
        self.assertEqual(llm.map_calls()["schema"], PLN.MAP_SCHEMA)

    def test_focus_note_enters_the_grouping_brief(self):
        """人写的侧重必须**走到排图提示词里**才算接线。

        光在 `paradigms` 里拼出来没用：这一路要经 `plan_map` 从项目上取
        `focus_note` 再交给 `prompt_block`，中间断一节，人写了什么都不会到
        模型手上，而界面看着一切正常。
        """
        item = dict(self.item, focus_note="多解析方法论，少讲技术细节与实现")
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        self.plan(item=item, llm=llm)
        prompt = "\n".join(m["content"] for m in llm.map_calls()["messages"])
        self.assertIn("**本档侧重**", prompt)
        self.assertIn("多解析方法论，少讲技术细节与实现", prompt)

    def test_no_focus_note_leaves_the_map_prompt_as_before(self):
        # 人没写＝老口径：提示词里不该冒出一段空的侧重，也不该多出一句
        # 粒度规则——那等于给所有老项目换了一套分组依据。
        item = dict(self.item)
        item.pop("focus_note", None)
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        self.plan(item=item, llm=llm)
        prompt = "\n".join(m["content"] for m in llm.map_calls()["messages"])
        self.assertNotIn("本档侧重", prompt)
        self.assertNotIn("少合几节", prompt)

    def test_probe_classification_is_a_separate_call(self):
        # 探查的分类与排图是两次调用，schema 与温度都不同。分类固定低温度，
        # 否则同一份素材两次探查给出两套结果，后面所有争论都无从对质。
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        self.plan(llm=llm)
        kinds = [c for c in llm.calls if c["schema"] is probe.PROBE_SCHEMA]
        self.assertTrue(kinds, "探查的分类调用没有发生")
        self.assertLess(kinds[0]["temperature"], 0.5)

    def test_condensation_is_one_call_per_unit(self):
        """凝缩**逐个单元**调，不是把全书押在一次输出上。

        一次调用要凝缩几十万字，模型只能按比例各切一刀，重点必然漂；逐个调用
        每次只看一节，上下文与输出都不随全书体量增长。
        """
        S.add_source(self.base, self.pid, "第二本.md",
                     "# 其一 另一册\n\n换一本看。\n")
        llm = FakeLLM(groups_json([([1, 2, 3, 4], "甲")]))
        self.plan(llm=llm)
        self.assertEqual(len(llm.condense_calls()), 4)
        for c in llm.condense_calls():
            self.assertLess(c["temperature"], 0.5)

    def test_map_records_which_model_and_version_condensed_it(self):
        # 凝缩这一步的来历要落在 IR 上。换过模型或换过提示词之后回头看，得能
        # 一眼看出这张图是不是同一道工序排出来的。写了字段不接线，等于没写。
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        self.plan(llm=llm)
        ir = probe.load(self.base, self.pid, "s1")
        self.assertEqual(ir["condense_version"], probe.CONDENSE_VERSION)
        self.assertTrue(ir["condense_model"])

    def test_condensation_failure_stops_instead_of_faking(self):
        # 凝缩结果是分组的依据。拿不到它还要往下走，排出来的图是按字数硬切的——
        # 那种图看着完整，才是最难查的假成功。
        llm = FakeLLM(groups_json([([1], "甲")]), failed_condense=True)
        with self.assertRaises(probe.ProbeError) as ctx:
            self.plan(llm=llm)
        self.assertIn("凝缩失败", str(ctx.exception))

    def test_unit_gist_is_condensed_not_the_whole_text(self):
        # 喂给模型的是凝缩结果，不是原文：几十万字的原文进不了上下文。
        tail = "尾部这一句只该留在正文里"
        S.add_source(self.base, self.pid, "长稿.md",
                     "# 长章\n\n" + ("正文内容。" * 80) + tail)
        llm = FakeLLM(groups_json([([1, 2, 3, 4], "甲")]))
        self.plan(llm=llm)
        self.assertIn("第一章 起点", llm.prompt)
        self.assertIn("本节主旨", llm.prompt)
        self.assertNotIn(tail, llm.prompt)

    def test_episode_count_comes_from_grouping_not_division(self):
        # 期数是**合并与切分的结果**，不是除法算出来再命令模型照排。素材体量
        # 只在两处露面：撞上限红线时说事、排完各期之间比一比。拿总字数除一期
        # 容量当期数用，等于假设「原文一个字换脚本一个字、照单全念」。
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        res = self.plan(llm=llm)
        # 容量与实现同源：都由标准语速算出，不随机器上的校准变。
        self.assertEqual(res["capacity"], probe.capacity(self.cfg))
        self.assertIn("由你决定", llm.prompt)
        self.assertNotIn("请正好排出", llm.prompt)
        self.assertEqual(len(res["episodes"]), 1)

    def test_unit_brief_carries_position_and_path(self):
        # 合并与切分的依据是逻辑与章节上下。清单里必须看得见每一节压在原文的
        # 哪一块、属于哪一篇——只有标题与字数的扁平表，模型只能靠字面猜。
        seg = {"title": "第一章 起点", "level": 2, "line": 9, "end": 48,
               "path": "第一部 总论", "chars": 400, "gist": "起点主旨",
               "series": "", "misc": ""}
        brief = PLN._units_text([("s1", seg)])
        self.assertIn("第一部 总论 › H2「第一章 起点」", brief)
        self.assertIn("第 10–48 行", brief)
        self.assertIn("400 字", brief)

    def test_same_material_gives_same_count_twice(self):
        # 确定性：同一份素材两次排出同样的期数，与模型这一次的心情无关
        a = self.plan(llm=FakeLLM(groups_json([([1, 2, 3], "甲")])))
        b = self.plan(llm=FakeLLM(groups_json([([1, 2, 3], "甲")])))
        self.assertEqual(len(a["episodes"]), len(b["episodes"]))
        self.assertEqual(a["planned"], b["planned"])

    def test_human_episode_count_wins(self):
        item = dict(self.item, planned_episodes=3)
        # 目标时长压到 1.2 秒（约 5 字/期，下限 6 字）：BOOK 那 26 个字才撑得起
        # 3 期。不压的话产能预检会先把这个场景拦在门外——那是
        # TestCapacityPreflight 的事，不是这里要测的。
        cfg = dict(self.cfg, **{"script.target_minutes": 0.02})
        llm = FakeLLM(groups_json([([1], "一"), ([2], "二"), ([3], "三")]))
        res = PLN.plan_map(self.base, self.pid, item, cfg, llm)
        self.assertEqual(res["planned"], 3)
        self.assertEqual(len(res["episodes"]), 3)
        # 人定的是**命令**：照这个数排。没定才由模型按文体分组定。
        self.assertIn("项目定的是 **3 期**", llm.prompt)

    def test_human_count_over_cap_falls_back_to_cap(self):
        item = dict(self.item, planned_episodes=5)
        cfg = dict(self.cfg, **{"script.map_max_episodes": 3,
                                "script.target_minutes": 0.02})
        res = PLN.plan_map(self.base, self.pid, item, cfg,
                           FakeLLM(groups_json([([1, 2, 3], "甲")])))
        self.assertEqual(res["planned"], 3)
        self.assertTrue(any("超出上限" in w for w in res["warnings"]),
                        res["warnings"])

    def test_model_overcount_lands_with_warning_not_wasted(self):
        # 排一次要几分钟。模型多排一期就把整张图废掉，那几分钟白等——
        # 落库并把差异说清楚，由人决定重排还是接受。
        item = dict(self.item, planned_episodes=2)
        cfg = dict(self.cfg, **{"script.target_minutes": 0.02})
        llm = FakeLLM(groups_json([([1], "一"), ([2], "二"), ([3], "三")]))
        res = PLN.plan_map(self.base, self.pid, item, cfg, llm)
        self.assertEqual(len(res["episodes"]), 3)
        self.assertTrue(any("与项目定的 2 期不符" in w for w in res["warnings"]),
                        res["warnings"])

    def test_cap_is_a_red_line_after_grouping(self):
        # 上限是**排完之后的红线**，不是排之前的除法。内容按逻辑分出超过上限
        # 的期数时报错要求合并——原先先 min 截断再命令模型照排，等于让它把
        # 超出来的内容无声塞进别的期里，每期都超载。
        S.add_source(self.base, self.pid, "厚书.md",
                     "\n\n".join("# 第%d章 标题\n\n" % i + ("正文。" * 400)
                                 for i in range(1, 41)))
        cfg = dict(self.cfg, **{"script.map_max_episodes": 2,
                                "script.target_minutes": 25.0})
        with self.assertRaises(PlanError) as ctx:
            PLN.plan_map(self.base, self.pid, self.item, cfg,
                         FakeLLM(groups_json([([1], "甲")])))
        self.assertIn("超过上限", str(ctx.exception))

    def test_paradigm_block_is_woven_into_prompt(self):
        # 组织依据来自范式，不再是一句对谁都能说的「按素材推进顺序排期」
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]), kind="methodology")
        res = self.plan(llm=llm)
        self.assertEqual(res["kind"], "methodology")
        self.assertIn("切分依据", llm.prompt)
        self.assertIn("整合依据", llm.prompt)
        self.assertIn("重点判据", llm.prompt)
        self.assertIn("推进方式", llm.prompt)
        # 凝缩依据取自范式的**凝缩专用**那一项（condense），不是排图用的
        # 「重点判据」——两件事判据方向相反（凝缩要「不丢」，排图要「选」），
        # 混用会让凝缩先按「挑重点」办事。
        cond = llm.condense_calls()[0]["messages"][1]["content"]
        card = _PG.PARADIGMS["methodology"]
        self.assertIn(card["condense"][:12], cond)
        self.assertNotIn(card["focus"][:12], cond)
        # 反过来，凝缩规则也不该混进排图提示词
        self.assertNotIn(card["condense"][:12], llm.prompt)

    def test_no_sources_raises(self):
        item2 = P.create(self.base, "空", "mapped")
        with self.assertRaises(probe.ProbeError):
            PLN.plan_map(self.base, item2["id"], item2, self.cfg, FakeLLM("{}"))

    def test_empty_episodes_raises(self):
        with self.assertRaises(PlanError):
            self.plan(llm=FakeLLM('{"episodes":[]}'))

    def test_brief_compresses_instead_of_dropping_sections(self):
        long_book = "\n\n".join(
            "# 第%d章 标题\n\n" % i + ("正文。" * 400) for i in range(1, 41))
        S.add_source(self.base, self.pid, "厚书.md", long_book)
        llm = FakeLLM(groups_json([([i], "第%d期" % i) for i in range(1, 41)]))
        self.plan(llm=llm)
        # 40 章一章都不能少：砍章节会让排出的期数与素材脱节
        for i in range(1, 41):
            self.assertIn("第%d章 标题" % i, llm.prompt)
        # 提示词有界：40 章的清单加凝缩也该压在几万字内，不随原文体量爆炸
        self.assertLess(len(llm.prompt), 72000)


class TestGrouping(Base):
    """分组的归一：模型只给序号，代码把它变成一个不重不漏的划分。

    这一层是从前没有的。从前落点由模型抄、期数由模型数，两者都可能缺一块，
    而缺的那一块没有任何人回头对账——地图上少了一节，只有把 645 条标题与
    期数逐条比过才看得出来。
    """

    def clean(self, rows, n):
        warns = []
        groups, dropped = PLN._clean_groups(rows, n, warns)
        return groups, dropped, warns

    def test_units_reduce_to_a_partition(self):
        groups, dropped, _w = self.clean(
            [{"units": [1, 2], "title": "一"}, {"units": [3], "title": "二"}], 3)
        self.assertEqual([g["units"] for g in groups], [[1, 2], [3]])
        self.assertEqual(dropped, [])

    def test_missing_unit_becomes_its_own_episode(self):
        # 漏掉的单元不是「少讲一点」，是那几节从此不在任何一期里
        groups, _d, warns = self.clean([{"units": [1], "title": "一"}], 3)
        self.assertEqual([g["units"] for g in groups], [[1], [2], [3]])
        self.assertTrue(any("没有被分进任何一期" in w for w in warns), warns)

    def test_out_of_range_index_is_dropped(self):
        groups, dropped, warns = self.clean(
            [{"units": [1, 9], "title": "一"}, {"units": [2], "title": "二"}], 2)
        self.assertEqual([g["units"] for g in groups], [[1], [2]])
        self.assertTrue(any("越界序号" in d for d in dropped), dropped)
        self.assertTrue(any("没有通过归一" in w for w in warns), warns)

    def test_duplicate_unit_lands_once(self):
        groups, _d, _w = self.clean(
            [{"units": [1, 2], "title": "一"}, {"units": [2, 3], "title": "二"}], 3)
        self.assertEqual([g["units"] for g in groups], [[1, 2], [3]])

    def test_groups_are_reordered_into_document_order(self):
        groups, _d, warns = self.clean(
            [{"units": [3], "title": "三"}, {"units": [1, 2], "title": "一二"}], 3)
        self.assertEqual([g["units"] for g in groups], [[1, 2], [3]])
        self.assertEqual(warns, [])

    def test_interleaved_groups_are_reported(self):
        groups, _d, warns = self.clean(
            [{"units": [1, 3], "title": "一三"}, {"units": [2], "title": "二"}], 3)
        self.assertTrue(any("打了结" in w for w in warns), warns)

    def test_empty_rows_produce_nothing(self):
        groups, _d, _w = self.clean([], 3)
        self.assertEqual(groups, [])


class TestEpisodeRefs(Base):
    """落点由分组直接取用，不经过模型。"""

    def plan(self, llm):
        return PLN.plan_map(self.base, self.pid, self.item, self.cfg, llm)

    def test_refs_come_from_the_units(self):
        llm = FakeLLM(groups_json([([1, 2], "一二"), ([3], "三")]))
        res = self.plan(llm=llm)
        self.assertEqual(res["episodes"][0]["refs"],
                         [{"source": "s1", "anchor": "第一章 起点", "line": 0},
                          {"source": "s1", "anchor": "第二章 转折", "line": 4}])
        self.assertEqual(res["episodes"][1]["refs"],
                         [{"source": "s1", "anchor": "第三章 收束", "line": 8}])

    def test_prompt_never_asks_the_model_for_refs(self):
        # 让模型抄标题是把「取料能不能取到」押在它肯不肯逐字照抄上。
        # 现在落点由序号映射，提示词里根本不该出现落点这回事。
        llm = FakeLLM(groups_json([([1, 2, 3], "甲")]))
        self.plan(llm=llm)
        self.assertIn("units", llm.prompt)
        self.assertNotIn("anchor", llm.prompt)
        self.assertNotIn("refs", llm.prompt)

    def test_episode_chars_are_summed_by_code(self):
        llm = FakeLLM(groups_json([([1, 2], "一二"), ([3], "三")]))
        res = self.plan(llm=llm)
        ir = probe.scan(BOOK)
        want = ir["segments"][0]["chars"] + ir["segments"][1]["chars"]
        self.assertEqual(res["episodes"][0]["chars"], want)

    def test_episode_without_gist_or_points_is_filled_and_reported(self):
        # 没给要点就退回各单元自己的凝缩主旨：要点一栏空着，后面写脚本时
        # 这一期就只剩标题可依。
        import json
        reply = json.dumps({"episodes": [
            {"units": [1, 2, 3], "title": "甲", "gist": "", "points": []}]},
            ensure_ascii=False)
        res = self.plan(llm=FakeLLM(reply))
        self.assertTrue(res["episodes"][0]["points"])
        self.assertTrue(any("没给要点" in w for w in res["warnings"]),
                        res["warnings"])

    def test_series_split_is_reported(self):
        text = "".join("# 08%s 第%s节\n\n%s\n\n" % (c, c, "正文内容。" * 30)
                       for c in ("a", "b"))
        S.add_source(self.base, self.pid, "系列.md", text)
        # 两个同系列单元被分到两期
        llm = FakeLLM(groups_json([([1, 2, 3], "甲"), ([4], "乙"), ([5], "丙")]))
        res = self.plan(llm=llm)
        self.assertTrue(any("同系列被拆" in w for w in res["warnings"]),
                        res["warnings"])


class TestUnitList(unittest.TestCase):
    """单元清单是排图那一次调用能看到的全部依据，判据是「一条不砍」。

    凝缩做的是保逻辑的压缩，那些主干条目正是判断两节该合还是该拆的依据。
    清单里砍掉第七条，模型就永远看不到它——而地图看上去完全正常。
    """

    def units(self, points=9, concepts=9):
        ir = probe.scan(BOOK)
        units = [("s1", s) for s in probe.pick_units(ir, 100000)]
        units[0][1]["gist"] = "主线"
        units[0][1]["points"] = ["主干%d" % i for i in range(1, points + 1)]
        units[0][1]["concepts"] = ["概念%d" % i for i in range(1, concepts + 1)]
        return units

    def test_every_point_reaches_the_prompt(self):
        text = PLN._units_text(self.units())
        for i in range(1, 10):
            self.assertIn("主干%d" % i, text)

    def test_every_concept_reaches_the_prompt(self):
        text = PLN._units_text(self.units())
        for i in range(1, 10):
            self.assertIn("概念%d" % i, text)

    def test_points_are_one_per_line(self):
        # 一条一行。挤成一行时模型读到的边界会糊掉，而条目的先后顺序是它
        # 判断论证链走向的依据。
        text = PLN._units_text(self.units(points=3, concepts=0))
        self.assertIn("   · 主干1", text)
        self.assertIn("   · 主干3", text)


class TestAssignBranchNos(Base):
    def _map(self, nos):
        P.set_map(self.base, self.pid, [
            {"no": n, "title": "第%s期" % n, "points": [], "refs": []} for n in nos])
        return P.find(self.base, self.pid)

    def test_starts_at_a(self):
        item = self._map(["1", "2", "3"])
        self.assertEqual(PLN.assign_branch_nos(item, "2", 2), ["2a", "2b"])

    def test_skips_taken_letters(self):
        item = self._map(["1", "2", "2a", "2b", "3"])
        # 同一个锚点插过一批之后，再插一批续用后面的字母
        self.assertEqual(PLN.assign_branch_nos(item, "2", 2), ["2c", "2d"])

    def test_gap_is_refilled(self):
        item = self._map(["1", "2", "2b", "3"])
        self.assertEqual(PLN.assign_branch_nos(item, "2", 2), ["2a", "2c"])

    def test_carries_past_z(self):
        nos = ["1", "2"] + ["2" + chr(97 + i) for i in range(26)]
        item = self._map(nos)
        self.assertEqual(PLN.assign_branch_nos(item, "2", 2), ["2aa", "2ab"])

    def test_zero_count(self):
        item = self._map(["1"])
        self.assertEqual(PLN.assign_branch_nos(item, "1", 0), [])


class TestPlanInsert(Base):
    """插入 = 换个容器再排一次：新凝缩、序号分组、落点代码填、体检照跑。"""

    def setUp(self):
        super(TestPlanInsert, self).setUp()
        P.set_map(self.base, self.pid, [
            {"no": "1", "title": "起点", "gist": "由来的主线", "points": ["由来"],
             "refs": [{"source": "s1", "anchor": "第一章 起点"}]},
            {"no": "2", "title": "转折", "gist": "分岔的主线", "points": ["分岔"],
             "refs": [{"source": "s1", "anchor": "第二章 转折"}]},
            {"no": "3", "title": "收束", "gist": "收尾的主线", "points": ["收尾"],
             "refs": [{"source": "s1", "anchor": "第三章 收束"}]}])
        S.add_source(self.base, self.pid, "佐证.md", BOOK2)
        self.item = P.find(self.base, self.pid)

    def insert_reply(self, anchor="2", titles=("另一个视角",)):
        import json
        # BOOK2 探查出两个单元，替身的分组要覆盖全部序号：漏掉的会被归一
        # 补成独立一期——那是代码的真实行为，不该混进替身的预期里
        spans = [[1, 2]] if len(titles) == 1 else [[i + 1] for i in range(len(titles))]
        eps = [{"units": span, "title": t, "gist": "本期主旨", "points": ["补充"]}
               for span, t in zip(spans, titles)]
        return json.dumps({"anchor_no": anchor, "episodes": eps},
                          ensure_ascii=False)

    def test_branch_numbers(self):
        llm = FakeLLM(self.insert_reply(titles=("另一个视角", "补充材料")))
        sug = PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        self.assertEqual(sug["anchor_no"], "2")
        self.assertEqual([e["no"] for e in sug["episodes"]], ["2a", "2b"])

    def test_focus_note_enters_the_insert_brief(self):
        """插入也是重分组，人写的侧重照样要进依据。

        只在「首次排图」认、插入时不认，等于同一档节目补一次料就换一套分组
        口径——而两次产出的地图要拼在一起用，口径不一致当场看不出来。
        """
        item = dict(self.item, focus_note="多解析方法论，少讲技术细节")
        llm = FakeLLM(self.insert_reply())
        PLN.plan_insert(self.base, self.pid, item, self.cfg, llm)
        prompt = "\n".join(m["content"] for m in llm.map_calls()["messages"])
        self.assertIn("**本档侧重**", prompt)
        self.assertIn("多解析方法论，少讲技术细节", prompt)

    def test_second_insert_continues_letters(self):
        P.insert_branches(self.base, self.pid, "2", [
            {"no": "2a", "title": "另一个视角", "points": [],
             "refs": [{"source": "s2", "anchor": "其一 另一个视角"}]}])
        # 第二批插入前先入库新料：缺省口径只处理还没排进地图的素材
        S.add_source(self.base, self.pid, "补料.md", BOOK2)
        item = P.find(self.base, self.pid)
        llm = FakeLLM(self.insert_reply(titles=("补充材料",)))
        sug = PLN.plan_insert(self.base, self.pid, item, self.cfg, llm)
        # 不重号也不重置：同一锚点下第二批接着字母往后排
        self.assertEqual([e["no"] for e in sug["episodes"]], ["2b"])

    def test_plan_brief_feeds_existing_episodes(self):
        llm = FakeLLM(self.insert_reply())
        PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        self.assertIn("第 1 期「起点」", llm.prompt)
        self.assertIn("第 3 期「收束」", llm.prompt)
        # 主旨也在场：模型判断新料与哪期相关、避哪些重，靠的就是它
        self.assertIn("主旨：由来的主线", llm.prompt)

    def test_source_filter_narrows_the_units(self):
        llm = FakeLLM(self.insert_reply())
        PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm,
                        source_ids=["s2"])
        # 只要新素材时，单元清单里是新素材的凝缩；原稿的章节不该再进清单
        self.assertIn("其一 另一个视角", llm.prompt)
        self.assertNotIn("第三章 收束", llm.prompt)

    def test_default_selection_skips_mapped_sources(self):
        # 不勾素材时默认只处理还没排进地图的：原稿已排过，再插一遍等于重插
        llm = FakeLLM(self.insert_reply())
        sug = PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        self.assertIn("其一 另一个视角", llm.prompt)
        self.assertNotIn("第一章 起点", llm.prompt)

    def test_insert_condenses_new_units(self):
        llm = FakeLLM(self.insert_reply())
        sug = PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        # 新素材逐节凝缩——插入与排图吃同一种料，不再有「只看开头几百字」的薄简报
        self.assertTrue(llm.condense_calls())
        self.assertTrue(sug["episodes"][0]["gist"], sug["episodes"])

    def test_insert_reuses_condense_cache(self):
        PLN.plan_insert(self.base, self.pid, self.item, self.cfg,
                        FakeLLM(self.insert_reply()))
        # 第二次插入同一素材：凝缩已随探查落盘，一次凝缩调用都不该再烧
        llm2 = FakeLLM(self.insert_reply())
        PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm2)
        self.assertEqual(llm2.condense_calls(), [])
        self.assertIn("主线：", llm2.prompt)

    def test_insert_refs_are_computed_from_units(self):
        llm = FakeLLM(self.insert_reply())
        sug = PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        # 落点由代码从单元序号映射：模型只填 units，不抄标题，也就抄不错
        refs = sug["episodes"][0]["refs"]
        self.assertEqual(refs[0]["source"], "s2")
        self.assertEqual(refs[0]["anchor"], "其一 另一个视角")

    def test_no_map_raises(self):
        item = P.create(self.base, "空", "mapped")
        S.add_source(self.base, item["id"], "甲", BOOK2)
        item = P.find(self.base, item["id"])
        with self.assertRaises(PlanError):
            PLN.plan_insert(self.base, item["id"], item, self.cfg,
                            FakeLLM(self.insert_reply()))

    def test_unknown_anchor_raises(self):
        llm = FakeLLM(self.insert_reply(anchor="99"))
        with self.assertRaises(PlanError):
            PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)

    def test_insert_schema_has_room_for_the_anchor(self):
        # 插入必须用带 anchor_no 的 schema。拿排图的 schema 去约束它，约束解码
        # 会把插入点一并剪掉，表现为「模型给的插入点是空的」——看着像模型不听话。
        llm = FakeLLM(self.insert_reply())
        PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        schema = llm.calls[-1]["schema"]
        self.assertEqual(schema, PLN.INSERT_SCHEMA)
        self.assertIn("anchor_no", schema.get("required") or [])
        # 条目与排图同款：模型照样只填 units，落点由代码映射
        self.assertIn("units", schema["properties"]["episodes"]["items"]["properties"])
        self.assertNotEqual(schema, PLN.MAP_SCHEMA)

    def test_empty_anchor_raises_with_its_own_message(self):
        import json
        llm = FakeLLM(json.dumps({"anchor_no": "", "episodes": [
            {"units": [1], "title": "甲", "gist": "主", "points": []}]}))
        with self.assertRaises(PlanError) as ctx:
            PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        self.assertIn("没有给出插入点", str(ctx.exception))

    def test_insert_retries_when_groups_empty(self):
        import json
        empty = json.dumps({"anchor_no": "2", "episodes": []},
                           ensure_ascii=False)
        llm = SeqLLM([empty, self.insert_reply()])
        sug = PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        self.assertEqual(sug["anchor_no"], "2")
        self.assertEqual([e["no"] for e in sug["episodes"]], ["2a"])

    def test_insert_groups_empty_twice_fails(self):
        import json
        empty = json.dumps({"anchor_no": "2", "episodes": []},
                           ensure_ascii=False)
        with self.assertRaises(PlanError):
            PLN.plan_insert(self.base, self.pid, self.item, self.cfg,
                            SeqLLM([empty]))

    def test_insert_does_not_touch_existing_map(self):
        before = [e["no"] for e in P.map_episodes(P.find(self.base, self.pid))]
        llm = FakeLLM(self.insert_reply(titles=("另一个视角",)))
        sug = PLN.plan_insert(self.base, self.pid, self.item, self.cfg, llm)
        P.insert_branches(self.base, self.pid, sug["anchor_no"], sug["episodes"])
        after = [e["no"] for e in P.map_episodes(P.find(self.base, self.pid))]
        self.assertEqual(after, ["1", "2", "2a", "3"])
        self.assertEqual(before, ["1", "2", "3"])


class TestUnitListContract(Base):
    """清单里的标题要在素材里切得回、过得准入。这一层两头都要锁。

    清单（排图与插入共用 `_units_text`）的标题与取料认的锚点分属两层，
    各自测都过。两层对不上时，分组落库了、取料却切不出东西，中间没人拦。
    """

    def _units(self):
        ir = probe.scan(BOOK, [], probe.HEAD_CHARS)
        return PLN.units_of({"s1": ir}, 100000)

    def _text(self):
        return PLN._units_text(self._units())

    def test_unit_list_wraps_titles_in_quotes(self):
        text = self._text()
        self.assertIn("「第一章 起点」", text)
        # 层级标签与标题之间不能再无分隔地贴在一起
        self.assertNotIn("H1 第一章 起点", text)

    def test_unit_list_states_the_format_before_the_list(self):
        # 规则必须前置：写在清单后面，模型读到清单时已经决定了怎么抄
        text = self._text()
        self.assertTrue(text.startswith(PLN.UNIT_LEGEND))
        self.assertLess(text.index(PLN.UNIT_LEGEND), text.index("第一章 起点"))

    def test_titles_in_unit_list_can_be_sliced_back(self):
        """清单里「」内的标题，拿去切素材必须切得出正文。

        探查切的标题与取料认的锚点分属两层，各自测都过。两层对不上时，
        分组落库了、取料却切不出东西，中间没人拦。
        """
        import re
        from podcast_maker import ingest
        text = self._text()
        titles = re.findall(r"H\d+「(.+?)」", text)
        self.assertTrue(titles)
        for t in titles:
            body, _meta = ingest.slice_by_anchor(BOOK, t, [])
            self.assertTrue(body.strip(), "「%s」切不出正文" % t)

    def test_unit_list_titles_all_pass_admission(self):
        ir = probe.scan(BOOK, [], probe.HEAD_CHARS)
        units = PLN.units_of({"s1": ir}, 100000)
        ok, _bad = probe.admissible(ir)
        ok_titles = {s["title"] for s in ok}
        for _sid, s in units:
            self.assertIn(s["title"], ok_titles)

    def test_prompt_asks_for_unit_numbers_not_titles(self):
        """排图提示词里不该再让模型抄标题。

        从前示例写「第1章 起点」——既不是素材里的标题，又长得像，模型照着
        它的形态把「第一章」改写成「第1章」，落点全丢。现在落点由序号映射，
        提示词里连 anchor 这个字段都不该出现。
        """
        prompt = PLN._map_prompt("（简报）", "（范式）", 2, 6000, 6000, 5)
        self.assertNotIn("第1章 起点", prompt)
        self.assertNotIn("anchor", prompt)
        self.assertIn("units", prompt)

    def test_insert_prompt_shows_no_fake_anchor(self):
        prompt = PLN._insert_prompt("（已有计划）", "（单元清单）", "（范式）")
        self.assertNotIn("第1章 起点", prompt)
        # 插入与排图同口径：模型只填序号；定锚职责在场（anchor_no 由它给）
        self.assertIn("只填序号", prompt)
        self.assertIn("anchor_no", prompt)


class TestRatioCheck(unittest.TestCase):
    """压比体检：两条红线之间都合格，红线外的反馈自带期号、字数与方向。

    口径（使用者定的公式）：**1 = 成稿目标**（每期时长 × 标准语速，不含
    偏移），分母就是它。偏移量 1.25 只进算式、不改变"1"：

    - 上限 = 档位 × 1.25（素材容量 x = 1 × 1.25 × 档位）；
    - 下限 = 1.2 × 1.25 = 1.5（素材至少 1 × 1.25 × 1.2；25 分钟一期
      ≈ 9877 字）。实测模型与素材近乎 1:1 消耗（4034 ÷ 4132 ≈ 0.98），
      素材刚好等于脚本时它写不满，出路是把写过的段落再背一遍，所以下限
      留两成余量——余量作用在偏移后的脚本上。
    """

    def test_over_upper_limit_flagged_with_numbers(self):
        issues = PLN._ratio_issues([{"no": "1", "chars": 98765}], 6024, 5)
        self.assertEqual(len(issues), 1)
        self.assertIn("第 1 期", issues[0])
        self.assertIn("超过上限 6.25", issues[0], "上限 = 档位 × 偏移 1.25")
        self.assertIn("1:5 档 × 偏移 1.25", issues[0])
        self.assertIn("拆成几期", issues[0])

    def test_under_floor_flagged_as_fabrication_risk(self):
        issues = PLN._ratio_issues([{"no": "2", "chars": 2100}], 6024, 5)
        self.assertEqual(len(issues), 1)
        self.assertIn("低于下限 1.50", issues[0], "下限 = 1.2 × 偏移 1.25")
        self.assertIn("素材至少 9036 字", issues[0], "6024 × 1.25 × 1.2 = 9036")
        self.assertIn("捏造", issues[0])

    def test_between_red_lines_passes(self):
        # 名义 1:5 的期（对成稿目标压 5 倍）不该报
        self.assertEqual(PLN._ratio_issues(
            [{"no": "1", "chars": 6024 * 5}], 6024, 5), [])
        # 刚好压满下限：6024 × 1.25 × 1.2 = 9036，恰好达线不算破
        self.assertEqual(PLN._ratio_issues(
            [{"no": "1", "chars": 9036}], 6024, 5), [])
        # 掉到线下 → 报
        self.assertTrue(PLN._ratio_issues(
            [{"no": "1", "chars": 9035}], 6024, 5))

    def test_headroom_boundary_is_exact(self):
        # 恰好压在 档位×1.25 上算达标：10 档上限 12.5，压 12.5 不报
        self.assertEqual(PLN._ratio_issues(
            [{"no": "1", "chars": int(6024 * 12.5)}], 6024, 10), [])
        # 再多一个字就超
        self.assertTrue(PLN._ratio_issues(
            [{"no": "1", "chars": int(6024 * 12.5) + 1}], 6024, 10))


# 素材体量按 target_chars(0.55 分钟) = 145 字、上限 906（×1.25）、下限 174（×1.2）
# 设计，每个场景的区间判断都留出两位数百分比的余量，不跟语速常数的舍入赌博：
LONG_BODY = "正文内容。" * 180          # ≈810 有效字：单独成期压比约 5.6，合格
MID_BODY = "正文内容。" * 90           # ≈405 有效字
TINY_BODY = "小结。" * 16              # ≈40 有效字：单独成期破下限
MIXED_BOOK = ("# 第一章 长章\n\n%s\n\n# 第二章 中章\n\n%s\n\n"
              "# 第三章 收尾\n\n%s\n" % (LONG_BODY, MID_BODY, TINY_BODY))


class TestMapRatioLoop(Base):
    """压比体检的整体重排回环：凝缩全程复用，反馈带数字，不收敛就收口。

    收口分两极：**超上限报错停下**（压不动的期写不出来，落库等于让后面每期都
    照错的图出片），**低于下限落警告放行**（料不够写出来是短、不是错，源头另有
    产能预检与下限两道闸门兜着）。

    素材统一换成本类的 MIXED_BOOK：Base 的 BOOK 全文只有 26 有效字，任何
    分组都破下限，会跟体检的语义搅在一起。三个单元的体量（810/405/40）按
    target_chars(0.55 分钟)=145 的两条红线设计——一期全装 8.66 超上限、
    [1] 单独成期 5.59 合格、[2,3] 合并 3.07 合格、[3] 单独成期 0.28 破下限，
    每个场景都踩得准。
    """

    def setUp(self):
        Base.setUp(self)
        for s in S.list_sources(self.base, self.pid):
            S.remove_source(self.base, self.pid, s["id"])
        S.add_source(self.base, self.pid, "书L.md", MIXED_BOOK)
        self.ratio_cfg = dict(self.cfg, **{"script.target_minutes": 0.55,
                                           "script.compress_ratio": 5})

    def test_oversize_first_draft_triggers_one_retry_then_passes(self):
        # 第一版把全部素材挤进一期（压比超上限），反馈后第二版拆成两期
        first = groups_json([([1, 2, 3], "甲")])
        second = groups_json([([1], "长章"), ([2, 3], "中章带收尾")])
        llm = SeqLLM([first, second])
        res = PLN.plan_map(self.base, self.pid, self.item, self.ratio_cfg,
                           llm, log=lambda m: None)
        maps = llm.map_calls_all()
        self.assertEqual(len(maps), 2, "体检不过应恰好重排一次")
        fb = maps[1]["messages"][1]["content"]
        self.assertIn("【上一版的问题】", fb)
        self.assertIn("超过上限", fb)
        self.assertNotIn("按这个数排", fb, "重排时期数必须放开")
        self.assertEqual([r["title"] for r in res["episodes"]],
                         ["长章", "中章带收尾"])
        self.assertFalse(any("请人工核对" in w for w in res["warnings"]))

    def test_under_floor_first_draft_triggers_one_retry_then_passes(self):
        # 第一版把最小的单元单独成期（压比破下限=没话硬写），重排后并期
        first = groups_json([([1], "甲"), ([2], "乙"), ([3], "丙")])
        second = groups_json([([1], "甲"), ([2, 3], "乙丙")])
        llm = SeqLLM([first, second])
        res = PLN.plan_map(self.base, self.pid, self.item, self.ratio_cfg,
                           llm, log=lambda m: None)
        self.assertEqual(len(llm.map_calls_all()), 2)
        self.assertIn("低于下限", llm.map_calls()["messages"][1]["content"])
        self.assertEqual(len(res["episodes"]), 2)

    def test_never_converging_stops_with_error_instead_of_landing(self):
        # 三版都超上限：重排 MAP_RETRIES 次后**报错停下**——超上限的期写不出来，
        # 落库等于让后面每一期都照这张错的图出片。既不空转，也不放过。
        llm = SeqLLM([groups_json([([1, 2, 3], "甲")])])
        with self.assertRaises(PlanError) as ctx:
            PLN.plan_map(self.base, self.pid, self.item, self.ratio_cfg,
                         llm, log=lambda m: None)
        self.assertEqual(len(llm.map_calls_all()), PLN.MAP_RETRIES + 1)
        self.assertIn("超过压缩上限", str(ctx.exception))

    def test_total_too_small_skips_retry_and_warns(self):
        # 素材连一期下限都不够：重排注定徒劳，直接落人说得清的警告
        for s in S.list_sources(self.base, self.pid):
            S.remove_source(self.base, self.pid, s["id"])
        S.add_source(self.base, self.pid, "书.md", BOOK)
        llm = SeqLLM([groups_json([([1], "甲"), ([2], "乙"), ([3], "丙")])])
        res = PLN.plan_map(self.base, self.pid, self.item, self.ratio_cfg,
                           llm, log=lambda m: None)
        self.assertEqual(len(llm.map_calls_all()), 1, "总量不足不该烧重排")
        self.assertTrue(any("不足一期的素材下限" in w for w in res["warnings"]),
                        res["warnings"])

    def test_healthy_ratio_never_retries(self):
        # 压比落在两线之间（405/145 ≈ 2.8）：一遍过，一次分组调用
        for s in S.list_sources(self.base, self.pid):
            S.remove_source(self.base, self.pid, s["id"])
        S.add_source(self.base, self.pid, "单章.md",
                     "# 单章\n\n%s\n" % MID_BODY)
        llm = SeqLLM([groups_json([([1], "甲")])])
        res = PLN.plan_map(self.base, self.pid, self.item, self.ratio_cfg,
                           llm, log=lambda m: None)
        self.assertEqual(len(llm.map_calls_all()), 1)
        self.assertFalse(any("压比" in w for w in res["warnings"]),
                         res["warnings"])


class TestCapacityPreflight(Base):
    """产能预检：素材撑不起计划的期数，排图前就拦，不烧凝缩。"""

    def setUp(self):
        Base.setUp(self)
        for s in S.list_sources(self.base, self.pid):
            S.remove_source(self.base, self.pid, s["id"])
        S.add_source(self.base, self.pid, "书L.md", MIXED_BOOK)

    def test_cannot_sustain_wanted_episodes_raises(self):
        # 默认 12 分钟/期（目标约 3160 字）要排 3 期，素材却只有一千余字
        item = dict(self.item, planned_episodes=3)
        llm = FakeLLM(groups_json([([1], "甲")]))
        with self.assertRaises(PlanError) as ctx:
            PLN.plan_map(self.base, self.pid, item, self.cfg, llm,
                         log=lambda m: None)
        self.assertIn("撑不起", str(ctx.exception))
        self.assertEqual(llm.condense_calls(), [], "预检拦下后不该再烧凝缩")

    def test_too_few_episodes_for_the_body_warns_but_proceeds(self):
        # 内容多、期数少：平均压比必超上限，警告但不拦——重排会增期数
        item = dict(self.item, planned_episodes=1)
        llm = SeqLLM([groups_json([([1], "甲"), ([2, 3], "乙")])])
        res = PLN.plan_map(self.base, self.pid, item, self.ratio_cfg(), llm,
                           log=lambda m: None)
        self.assertTrue(any("至少需要" in w for w in res["warnings"]),
                        res["warnings"])
        self.assertEqual(len(res["episodes"]), 2)

    def ratio_cfg(self):
        return dict(self.cfg, **{"script.target_minutes": 0.55,
                                 "script.compress_ratio": 5})


class TestPromptBlocks(unittest.TestCase):
    """提示词分块：每一块自报「这是什么、怎么用」。

    一条提示词里塞着好几种东西——稿件结构、可选类型、当前任务、输出契约。块名
    统一只是表面；要紧的是**模型一眼能分出哪块是依据、哪块是当前该产出的东西**。
    这里盯块名在场，防的是后来人图省事把它们抹平。
    """

    def test_probe_prompt_blocks(self):
        llm = FakeLLM(groups_json([]), kind="methodology", unit_level=1)
        probe.classify(
            {"segments": [{"level": 1, "ok": True, "title": "甲篇"},
                          {"level": 1, "ok": True, "title": "乙篇"},
                          {"level": 2, "ok": True, "title": "甲篇的一节"}],
             "level_counts": {"1": 2, "2": 1}},
            llm, {"llm.max_tokens": 4096}, _PG.PARADIGMS)
        for block in ("【稿件结构】", "【各层按文体真正要凝缩的条数】",
                      "【可选素材类型与各自的文体判据】",
                      "【当前任务：判断三件事】"):
            self.assertIn(block, llm.prompt, block)

    def test_condense_prompt_blocks(self):
        txt = probe._condense_prompt("标题甲", "文体依据正文", "原文正文")
        for block in ("【凝缩依据】", "【要做到什么程度】", "【输出】",
                      "【本节原文】"):
            self.assertIn(block, txt, block)

    def test_map_prompt_blocks(self):
        txt = PLN._map_prompt("清单正文", "文体块", 0, 3000, 3000, 5)
        for block in ("【结构单元清单】", "【期数】", "【当前任务：分组】",
                      "【每期给四项】", "【输出】"):
            self.assertIn(block, txt, block)

    def test_insert_prompt_blocks(self):
        txt = PLN._insert_prompt("已有计划正文", "单元清单正文", "文体块")
        for block in ("【已有计划】", "【新素材的凝缩单元清单】",
                      "【当前任务：定插入点并在锚点下分组建期】", "【输出】"):
            self.assertIn(block, txt, block)


if __name__ == "__main__":
    unittest.main()
