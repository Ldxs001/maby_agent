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

"""范式卡的两档提示词与上限取值。

排图问「怎么切、怎么并」，写脚本问「怎么说」——同一张卡的两个面。混在一处
必然打架：模型会拿「同一人可以连说三句」去凑分期，或者拿「一条链不许拆开」
去写台词。所以 `prompt_block()` 分 plan / script 两档，各拼各的字段。

连续句数上限（`max_run`）另有一层要求：写脚本的提示词与它的门禁必须取同一个
数，所以取值只有 `max_run_of()` 一处。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import paradigms as PG                                  # noqa: E402


class TestPromptBlockStages(unittest.TestCase):

    def test_plan_stage_keeps_the_four_organization_items(self):
        block = PG.prompt_block("methodology")
        for title in ("切分依据", "整合依据", "重点判据", "推进方式"):
            self.assertIn(title, block)
        self.assertNotIn("情绪基调", block)
        self.assertNotIn("同一人连续句数上限", block)

    def test_script_stage_carries_the_three_speaking_items(self):
        card = PG.get("interview")
        block = PG.prompt_block("interview", stage="script")
        self.assertIn(card["cast"], block)
        self.assertIn(card["emotion"], block)
        self.assertIn("同一人连续句数上限**：%d 句" % card["max_run"], block)
        self.assertNotIn("切分依据", block)

    def test_no_card_leaks_the_script_items_into_plan(self):
        for key in PG.PARADIGMS:
            self.assertNotIn("同一人连续句数上限", PG.prompt_block(key))

    def test_override_reaches_both_stages(self):
        self.assertIn("换个说法", PG.prompt_block("essay", {"focus": "换个说法"}))
        self.assertIn("换个站位",
                      PG.prompt_block("essay", {"cast": "换个站位"}, stage="script"))

    def test_unknown_key_falls_back_to_auto_without_raising(self):
        self.assertIn("自适应", PG.prompt_block("没有这张卡", stage="script"))


class TestEveryCardIsComplete(unittest.TestCase):
    """「怎么说」那三项每张卡都要有：缺一个就退回内置口径或让模型猜数。"""

    def test_every_card_has_cast_and_emotion(self):
        for key, card in PG.PARADIGMS.items():
            self.assertTrue(str(card.get("cast") or "").strip(),
                            "%s 缺 cast" % key)
            self.assertTrue(str(card.get("emotion") or "").strip(),
                            "%s 缺 emotion" % key)

    def test_every_card_has_an_integer_run_limit(self):
        for key, card in PG.PARADIGMS.items():
            self.assertIsInstance(card.get("max_run"), int,
                                  "%s 的 max_run 不是整数" % key)
            self.assertGreaterEqual(card["max_run"], 2,
                                    "%s 的上限是 1，等于强制交替" % key)


class TestEmotionLevel(unittest.TestCase):
    """情绪档位：写脚本按它决定能不能填心情词，合成按它决定拼不拼措辞。

    两头取的是同一个值，所以**每张卡都必须写出来**——缺了它就一路退到
    `none`，写脚本不填心情、合成不贴语气，整期悄悄少了这一层。
    """

    #: 心情词。none 档的基调里点名它们，模型会照着基调填，与门禁两头打架。
    MOOD_WORDS = ("好奇", "疑惑", "恍然", "肯定", "感慨", "轻松")

    def test_every_card_declares_a_valid_level(self):
        for key, card in PG.PARADIGMS.items():
            self.assertIn(card.get("emotion_level"), PG.EMOTION_LEVELS,
                          "%s 卡的档位缺失或不合法" % key)

    def test_none_cards_never_name_a_mood_word(self):
        """档位说不写心情，基调就不该点名心情词。「平静」不在此列：它是默认
        底色、也是归并越界值的落点，不算心情。"""
        for key in PG.PARADIGMS:
            card = PG.get(key)
            if PG.emotion_level_of(card) != "none":
                continue
            tone = str(card.get("emotion") or "")
            for w in self.MOOD_WORDS:
                self.assertNotIn(w, tone,
                                 "%s 卡是 none 档，基调里却写了「%s」" % (key, w))

    def test_junk_falls_back_to_none(self):
        self.assertEqual(PG.emotion_level_of({}), "none")
        self.assertEqual(PG.emotion_level_of(None), "none")
        self.assertEqual(PG.emotion_level_of({"emotion_level": "LIGHT"}), "none")
        self.assertEqual(PG.emotion_level_of({"emotion_level": "light"}), "light")

    def test_prompt_block_carries_the_level_rule(self):
        # 规则要说在提示词里，模型才知道这一档能填什么——只写"略"两个字没用。
        paper = PG.prompt_block("paper", stage="script")
        self.assertIn("情绪到哪为止", paper)
        self.assertIn("不写心情", paper)
        story = PG.prompt_block("narrative", stage="script")
        self.assertIn("略带", story)


class TestMaxRunOf(unittest.TestCase):

    def test_reads_the_value_on_the_card(self):
        self.assertEqual(PG.max_run_of({"max_run": 4}), 4)

    def test_missing_value_falls_back_to_the_default(self):
        self.assertEqual(PG.max_run_of({}), PG.DEFAULT_MAX_RUN)
        self.assertEqual(PG.max_run_of(None), PG.DEFAULT_MAX_RUN)

    def test_junk_is_not_fatal(self):
        self.assertEqual(PG.max_run_of({"max_run": "不知道"}), PG.DEFAULT_MAX_RUN)
        self.assertEqual(PG.max_run_of({"max_run": 0}), PG.DEFAULT_MAX_RUN)

    def test_never_below_one(self):
        self.assertEqual(PG.max_run_of({"max_run": -3}), 1)


class TestRegisterDefaults(unittest.TestCase):

    def test_a_custom_card_gets_the_run_limit_and_no_fake_tone(self):
        key = "_test_custom_card"
        try:
            card = PG.register(key, {"label": "测试卡"})
            self.assertEqual(card["max_run"], PG.DEFAULT_MAX_RUN)
            self.assertEqual(card["cast"], "")
            self.assertEqual(card["emotion"], "")
            # 上限照样写出来（门禁按它判），站位与基调留空——宁缺勿造
            block = PG.prompt_block(key, stage="script")
            self.assertIn("同一人连续句数上限", block)
            self.assertNotIn("两人站位", block)
            self.assertNotIn("情绪基调", block)
        finally:
            PG.PARADIGMS.pop(key, None)


class TestChosenOf(unittest.TestCase):
    """「用哪张卡」的定序只有一处：项目 > 全局默认 > 空串（交给探查推断）。

    空串与 `"auto"` 不是一回事：空串是没表态，推断那一层才接手；`"auto"` 是
    明确选了自适应那张卡。混成一个值，推断就再也进不来。
    """

    def test_project_beats_the_global_default(self):
        self.assertEqual(
            PG.chosen_of({"paradigm": "paper"}, {"script.paradigm": "essay"}),
            "paper")

    def test_global_default_applies_when_the_project_says_nothing(self):
        self.assertEqual(
            PG.chosen_of({"paradigm": ""}, {"script.paradigm": "essay"}), "essay")

    def test_an_explicit_auto_on_the_project_is_a_choice(self):
        self.assertEqual(
            PG.chosen_of({"paradigm": "auto"}, {"script.paradigm": "essay"}),
            "auto")

    def test_global_auto_means_no_statement(self):
        self.assertEqual(PG.chosen_of({}, {"script.paradigm": "auto"}), "",
                         "全局写 auto 等于没表态，要留给探查推断")

    def test_nothing_anywhere_returns_empty(self):
        self.assertEqual(PG.chosen_of(None, None), "")


class TestAppendixLists(unittest.TestCase):
    """附属页两栏：每张卡都要有，且两栏不许出现同一条。"""

    def test_every_card_has_both_columns(self):
        for key, card in PG.PARADIGMS.items():
            pr = card.get("probe") or {}
            with self.subTest(card=key):
                self.assertIn("misc_drop", pr, "缺「不播」那一栏")
                self.assertIn("misc_keep", pr, "缺「要播」那一栏")
                self.assertNotIn("misc", pr, "旧的单栏写法该清掉")

    def test_no_card_contradicts_itself(self):
        for key, card in PG.PARADIGMS.items():
            pr = card.get("probe") or {}
            both = set(pr.get("misc_drop") or []) & set(pr.get("misc_keep") or [])
            with self.subTest(card=key):
                self.assertFalse(both, "同一条既在不播又在要播：%s" % "、".join(both))

    def test_auto_leaves_both_columns_empty(self):
        pr = PG.PARADIGMS["auto"]["probe"]
        self.assertEqual(pr["misc_drop"], [])
        self.assertEqual(pr["misc_keep"], [],
                         "推断不出类型时一切落通用兜底，不拿猜的卡改判定")

    def test_register_rejects_a_contradiction(self):
        with self.assertRaises(ValueError):
            PG.register("_bad_card",
                        {"label": "x", "probe": {"misc_drop": ["致谢"],
                                                 "misc_keep": ["致谢"]}})
        self.assertNotIn("_bad_card", PG.PARADIGMS, "报错后不该留下半张卡")

    def test_register_drops_the_old_single_column(self):
        key = "_test_old_misc"
        try:
            card = PG.register(key, {"label": "x", "probe": {"misc": ["致谢"]}})
            self.assertNotIn("misc", card["probe"])
            self.assertEqual(card["probe"]["misc_drop"], [])
            self.assertEqual(card["probe"]["misc_keep"], [])
        finally:
            PG.PARADIGMS.pop(key, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
