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

连续句数上限（`run`）**不属于卡**：它跟着对话形式走（一问一答里 A 只递话、
B 主讲，主讲＋捧哏里 B 成段铺开，同一个数管两个人等于逼其中一个人抢话），
卡只给一个默认形式（`form`）。取值收口在 `resolve_form()`（用哪种形式）与
`run_caps()`（那种形式下两人各几句）——写脚本的提示词与它的门禁必须取同一处，
否则会出现提示词说可以连说四句、门禁按两句判。
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import paradigms as PG, script_engine as S             # noqa: E402
from podcast_maker.config_manager import (ConfigManager, MODE_SPEC,      # noqa: E402
                                          PARAM_SPEC)


class TestPromptBlockStages(unittest.TestCase):

    def test_plan_stage_keeps_the_four_organization_items(self):
        block = PG.prompt_block("methodology")
        for title in ("切分依据", "整合依据", "重点判据", "推进方式"):
            self.assertIn(title, block)
        self.assertNotIn("情绪基调", block)
        self.assertNotIn("连着说的上限", block)

    def test_script_stage_carries_only_the_run_caps(self):
        """写侧只剩连句上限一条：站位归 `_hosts_text`，不在这里重复一份。"""
        card = PG.get("interview")
        block = PG.prompt_block("interview", stage="script")
        cap_a, cap_b = PG.run_caps(PG.resolve_form(card, ""))
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % (cap_a, cap_b), block)
        self.assertNotIn(card["cast"], block,
                         "站位在这里再贴一份，选中形式也顶不掉卡上那份")
        self.assertNotIn("切分依据", block)
        # 卡上那行「情绪基调」不许回来，语篇标签的**释义表**也不许在这里
        # 再贴一份——两者都会和词表层形成两套说法。
        # （"emotion" 这个键只作为形状示范里三个输出字段之一出现，
        # 它不是第二份标签表。）
        self.assertNotIn("情绪基调", block)
        self.assertNotIn(S.DISCOURSE_HELP["比喻"], block)

    def test_no_card_leaks_the_script_items_into_plan(self):
        for key in PG.PARADIGMS:
            self.assertNotIn("连着说的上限", PG.prompt_block(key))

    def test_override_reaches_both_stages(self):
        self.assertIn("换个说法", PG.prompt_block("essay", {"focus": "换个说法"}))
        # 写侧的上限不在卡上：改卡上的键没用，它跟着**形式**走。
        picked = PG.prompt_block("essay", stage="script", chosen_form="anchor")
        self.assertIn("A 最多连着说 1 句、B 最多连着说 10 句", picked)
        self.assertNotIn("A 最多连着说 5 句", picked,
                         "上限跟的是形式；这里出现 5 说明它还在按卡上的默认形式算")

    def test_unknown_key_falls_back_to_auto_without_raising(self):
        self.assertIn("自适应", PG.prompt_block("没有这张卡", stage="script"))


class TestEveryCardIsComplete(unittest.TestCase):
    """「怎么说」那三项每张卡都要有：缺一个就退回内置口径或让模型猜数。"""

    def test_every_card_has_cast(self):
        for key, card in PG.PARADIGMS.items():
            self.assertTrue(str(card.get("cast") or "").strip(),
                            "%s 缺 cast" % key)
            self.assertNotIn("emotion", card,
                             "%s 还带着已删除的 emotion 键" % key)

    def test_every_card_declares_a_valid_default_form(self):
        """每张卡都要有一个认得出的默认形式——上限就是从它推出来的。"""
        for key, card in PG.PARADIGMS.items():
            self.assertIn(card.get("form"), PG.DIALOGUE_FORMS,
                          "%s 的默认形式缺失或不认识" % key)
            self.assertNotIn("max_run", card,
                             "%s 还带着已搬走的 max_run——上限现在归对话形式" % key)


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

    def test_prompt_block_no_longer_carries_the_level_rule(self):
        # emotion 字段已删，「情绪到哪为止」那段口径从写侧提示词里撤掉；
        # 档位（emotion_level_of）只剩合成侧的 degree 在读。
        for key in ("paper", "narrative"):
            self.assertNotIn("情绪到哪为止",
                             PG.prompt_block(key, stage="script"))


class TestResolveForm(unittest.TestCase):
    """「本期用哪种形式」与「那种形式下两人各几句」的取值。

    三级顺序：人在配置里选的 > 卡上的默认 > 兜底。**不让「没选」落成空值**——
    上限总得有一个数，写成空值再让各处各兜一次底，就会重演 `max_run` 分散在
    卡上时的毛病（提示词按一种形式写、门禁按另一种判）。
    """

    def test_chosen_beats_the_card(self):
        self.assertEqual(PG.resolve_form({"form": "qa"}, "anchor"), "anchor")

    def test_card_default_applies_when_nobody_chose(self):
        self.assertEqual(PG.resolve_form({"form": "drill"}, ""), "drill")

    def test_junk_falls_back_to_the_default(self):
        self.assertEqual(PG.resolve_form({}, ""), PG.DEFAULT_FORM)
        self.assertEqual(PG.resolve_form(None, None), PG.DEFAULT_FORM)
        self.assertEqual(PG.resolve_form({"form": "没这张"}, ""), PG.DEFAULT_FORM)
        self.assertEqual(PG.resolve_form({"form": "qa"}, "认不出"), "qa")

    def test_every_form_has_two_int_caps(self):
        for key in PG.DIALOGUE_FORMS:
            cap_a, cap_b = PG.run_caps(key)
            self.assertIsInstance(cap_a, int, key)
            self.assertIsInstance(cap_b, int, key)
            self.assertGreaterEqual(min(cap_a, cap_b), 1, key)
        # 表里缺数按 1 算：不是宽松兜底，是让缺数当场显形
        self.assertEqual(PG.run_caps("没这张"), (1, 1))

    def test_no_form_leaves_a_side_without_a_cap(self):
        """哪怕是捧哏那种宽松形式，主讲那一条也得有上限——没有上限就成了单口。"""
        for key, f in PG.DIALOGUE_FORMS.items():
            self.assertLessEqual(PG.run_caps(key)[1], 20,
                                 "%s 的 B 上限放得太开，模型会摁着一个人说" % key)


class TestRegisterDefaults(unittest.TestCase):

    def test_a_custom_card_gets_a_default_form_and_no_stale_run(self):
        key = "_test_custom_card"
        try:
            card = PG.register(key, {"label": "测试卡", "max_run": 4})
            self.assertEqual(card["form"], PG.DEFAULT_FORM)
            self.assertEqual(card["cast"], "")
            self.assertNotIn("max_run", card,
                             "旧卡上的 max_run 要丢掉，不然会让人以为改它能改到上限")
            self.assertIn("连着说的上限", PG.prompt_block(key, stage="script"))
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


class TestFocusNote(unittest.TestCase):
    """人写的侧重（项目级「重点方向」）与卡上的重点判据**同时**进排图。

    两条判据：

    - **空串是「人没写过」**：那时输出必须与没有这条路时逐字一致。老项目
      重排一次图，口径不许因为多了一条通道就悄悄变——那种漂移没人会去查。
    - **写了才拼两样**：人写的那句，以及把它挂到分组粒度上的那句话。卡上的
      重点判据只管「抓什么、什么不播」，一句话里没有「合并的松紧」；不点明，
      模型会把人写的方向当成一条泛泛的内容取向，分组粒度一动也不动。
    """

    def test_absent_note_leaves_the_block_untouched(self):
        base = PG.prompt_block("methodology")
        for empty in ("", None, "   "):
            self.assertEqual(PG.prompt_block("methodology", extra_focus=empty),
                             base, repr(empty))

    def test_note_is_appended_beside_the_card_focus(self):
        block = PG.prompt_block("methodology", extra_focus="多解析方法论，少讲技术细节")
        self.assertIn("**重点判据**", block)
        self.assertIn("**本档侧重**", block)
        self.assertIn("多解析方法论，少讲技术细节", block)
        # 人的侧重排在卡上那一条之后：先给文体的通用判据，再给人给的方向。
        self.assertLess(block.index("**重点判据**"), block.index("**本档侧重**"))

    def test_note_carries_the_granularity_rule(self):
        block = PG.prompt_block("methodology", extra_focus="多解析方法论")
        self.assertIn("少合几节、多给期数", block)
        self.assertIn("合得更粗", block)
        # 粒度只许在上下限之内调：下限优先那条约束在范式层就得说明白，
        # 不能指望模型自己去 `_map_prompt` 的期数块里翻。
        self.assertIn("上下限之内", block)

    def test_note_never_reaches_the_script_stage(self):
        block = PG.prompt_block("methodology", extra_focus="多解析方法论",
                                stage="script")
        self.assertNotIn("本档侧重", block)
        self.assertNotIn("多解析方法论", block)


class TestDialogueForm(unittest.TestCase):
    """对话形式：默认跟卡走，选了就顶掉卡上那段站位说明。

    站位（谁懂谁不懂）与形式（话怎么交错、两人各能连说几句）是两件事。从前
    形式写死在卡上的 `cast` 里，于是不论什么素材出来都是同一套问答节奏。单拎成
    一维之后，卡上只留一个默认形式：不选＝按卡上那个形式走、卡上的站位照旧；
    选了就把站位顶掉（形式的说明里已经写了谁干什么活）。

    默认态有一处**可感知的变化**（v0.31.0）：上限的展示从卡上那一个数变成
    「A …句、B …句」两条，数值也跟着形式走（方法论卡原来是「两人都 2 句」，
    现在是 qa 的 A2/B4）。这是把「一个数管两个人」拆成按主场各给一条的代价。
    """

    def _cfg(self, **over):
        """干净基线：**不读用户的 config.json**——他在界面上改过哪一项（对话
        形式正是他最容易改的一项），不该让测试替他红一遍。"""
        cfg = {"tts.name_a": "小美", "tts.name_b": "大美",
               "script.dialogue_form": "", "script.style_preset": "argument"}
        cfg.update(over)
        return cfg

    def test_empty_form_leaves_the_block_untouched(self):
        """空值、空白、未知键都返回空串：不选＝没这一维。"""
        for nothing in ("", None, "   ", "不存在的形式"):
            self.assertEqual(PG.form_block(nothing), "", repr(nothing))

    def test_every_form_carries_a_label_and_a_shape(self):
        """每条都要有标签与「话怎么接」的说明。

        只有标签没有形状等于给模型一个风格名——它照样按示例的问答形状写，
        形式就成了一句空话。
        """
        self.assertGreaterEqual(len(PG.DIALOGUE_FORMS), 2)
        for key, f in PG.DIALOGUE_FORMS.items():
            self.assertTrue(str(f.get("label") or "").strip(), key)
            self.assertGreaterEqual(len(str(f.get("desc") or "").strip()), 20, key)

    def test_the_option_table_is_shared_with_the_ui(self):
        """界面下拉与提示词用同一批标签：标签只写一处。

        下拉说明后面挂的连句上限也一并核：门禁按这两个数判超限、点出整段交模型
        并句，界面上却看不到，等于「为什么门禁说这段超了」找不到出处。
        """
        opts = MODE_SPEC["script.dialogue_form"]["options"]
        self.assertIn("", opts, "默认项「跟随素材类型」要在下拉里看得见，不是留空")
        for key, f in PG.DIALOGUE_FORMS.items():
            self.assertEqual(opts[key]["label"], f["label"], key)
            cap_a, cap_b = PG.run_caps(key)
            self.assertIn("A %d 句、B %d 句" % (cap_a, cap_b), opts[key]["desc"],
                          "%s 的下拉说明里要看得见它给的上限" % key)

    def test_the_default_config_is_the_empty_choice(self):
        """配置默认值必须是空——默认态是「按卡走」，不是替所有人选一种形式。

        读的是**点位的默认值表**，不是用户的 config.json：他改过这一项是他
        自己的选择，不该让这条测试变红。
        """
        self.assertEqual(PARAM_SPEC["script.dialogue_form"]["default"], "")

    def test_untouched_behaviour_is_byte_for_byte_the_old_one(self):
        """不选形式时，站位文本与从前逐字一致（卡上写了 cast 就照它）。"""
        cfg = self._cfg()
        card = {"cast": "A 讲方法，B 挑毛病。"}
        self.assertEqual(S._hosts_text(card, cfg),
                         "A 称呼「小美」，B 称呼「大美」。两人的站位与分工：\n"
                         "A 讲方法，B 挑毛病。")
        self.assertEqual(S._hosts_text({"cast": ""}, cfg, ""),
                         S._hosts_text({"cast": ""}, cfg))

    def test_chosen_form_replaces_the_cards_cast(self):
        """选了形式就**顶掉**卡上那段站位说明——是覆盖，不是并列两个来源。"""
        cfg, card = self._cfg(), {"cast": "甲甲甲站位"}
        picked = S._hosts_text(card, cfg, "debate")
        self.assertNotIn("甲甲甲站位", picked, "形式覆盖卡上的站位说明")
        self.assertIn("观点对辩", picked)
        self.assertIn("小美", picked, "播讲人称呼两种情况下都保留")
        self.assertIn("甲甲甲站位", S._hosts_text(card, cfg),
                      "不选形式时卡上那段照旧生效")

    def test_the_form_reaches_both_script_paths(self):
        """整篇路与分段路的提示词里都要有这句形式。"""
        cfg = self._cfg(**{"script.dialogue_form": "anchor"})
        card = PG.get("methodology")
        whole = S.build_system_prompt(cfg, "argument", 6000, 200, paradigm=card)
        self.assertIn("主讲＋捧哏", whole)
        self.assertIn("主讲＋捧哏", S.insert_system(card, cfg),
                      "插入那一轮也得知道现在是什么形式，不然补出来的句子是另一种节奏")

    def test_no_form_means_no_override_line(self):
        """不选形式时，提示词里不许有「覆盖」那一段——角色块与「以形式为准」。

        但**节奏与上限照样要给**：没人选时用的是卡上的默认形式，那也是形式，
        模型得知道这一期是怎么接的、上限几个数。缺了它，模型只能自己拿默认
        节奏（一问一答）来填——上限写着 B 十句，稿子照样一句一换。
        """
        cfg = self._cfg()
        auto = PG.get("auto")
        key = PG.resolve_form(auto, "")
        self.assertTrue(PG.rhythm_text(key), "每种形式都得有节奏那一句")
        for name, text in (
                ("插入", S.insert_system(auto, cfg)),
                ("整篇", S.build_system_prompt(cfg, "argument", 6000, 200,
                                               paradigm=auto))):
            self.assertNotIn("以形式为准", text, "%s：没人选，不许出现覆盖声明" % name)
            self.assertNotIn("- A 是", text, "%s：没人选，不许出现形式的角色块" % name)
            self.assertIn(PG.rhythm_text(key), text, "%s：节奏那一句要在" % name)

    def test_the_cards_cast_never_survives_a_chosen_form(self):
        """三处提示词都要把卡上那段站位顶掉——只顶一处等于没顶。

        卡上的站位从前还会被拼块另贴一份进【文体依据】，于是选中形式之后
        提示词里同时有两套分工说法，而且是卡上那份**永远顶不掉**。
        """
        cfg = self._cfg(**{"script.dialogue_form": "anchor"})
        card = PG.get("methodology")
        texts = {
            "整篇": S.build_system_prompt(cfg, "argument", 6000, 200, paradigm=card),
            "分段": S._segment_system_prompt(cfg, card, "argument", 1, 3, 900,
                                              "主旨", True),
            "插入": S.insert_system(card, cfg),
        }
        for name, text in texts.items():
            self.assertNotIn("A 是提问方", text, "%s：卡上站位没被形式顶掉" % name)
            self.assertIn("主讲＋捧哏", text, name)

    def test_the_run_caps_follow_the_form_in_every_prompt(self):
        """节奏与上限在三处都与形式同源；「没人选」时取的是卡上的默认形式。

        只给上限就是把红线当形状：上限里推不出「该连着说几句」，模型会拿自己
        的默认节奏来填。所以三处除了上限，还得有那句「两人怎么接」。
        """
        card = PG.get("methodology")
        cfg = self._cfg(**{"script.dialogue_form": "anchor"})
        cap_a, cap_b = PG.run_caps("anchor")
        rhythm = PG.rhythm_text("anchor")
        whole = S.build_system_prompt(cfg, "argument", 6000, 200, paradigm=card)
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % (cap_a, cap_b), whole)
        self.assertIn(rhythm, whole)
        seg = S._segment_system_prompt(cfg, card, "argument", 1, 3, 900, "主旨", True)
        self.assertIn("A 最多 %d 句、B 最多 %d 句" % (cap_a, cap_b), seg)
        self.assertIn(rhythm, seg)
        ins = S.insert_system(card, cfg)
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % (cap_a, cap_b), ins)
        self.assertIn(rhythm, ins)
        # 没人选时按卡上的默认形式算，不是没有上限、也不是没有节奏
        key = PG.resolve_form(card, "")
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % PG.run_caps(key),
                      S.insert_system(card, self._cfg()))
        self.assertIn(PG.rhythm_text(key), S.insert_system(card, self._cfg()))

    def test_only_qa_pins_the_speaking_order(self):
        """六种形式里只有一问一答把顺序写死，其余一律描述式。

        把某一种固定序列（A B A B、A A A B B B）焊进提示词，等于把所有素材的
        节奏压成同一副样子；而「不选形式的 A 就是提问者」这种口径，会让任何
        形式写出来都是一问一答——形式名换了，稿子没换。
        """
        for key, f in PG.DIALOGUE_FORMS.items():
            rhythm = PG.rhythm_text(key)
            self.assertTrue(rhythm, key)
            for bad in ("A B A B", "A A A", "B B B", "AAABBB"):
                if key == "qa":
                    continue
                self.assertNotIn(bad, rhythm,
                                 "%s 的节奏写成了固定序列" % key)
        self.assertIn("A B A B", PG.rhythm_text("qa"))
        for key, f in PG.DIALOGUE_FORMS.items():
            roles = f.get("roles") or {}
            self.assertEqual(sorted(roles), ["A", "B"], "%s 缺 A/B 角色" % key)
            for who in ("A", "B"):
                self.assertTrue(str(roles[who]).strip(), "%s 的 %s 角色是空的" % (key, who))

    def test_every_form_gives_a_concrete_magnitude(self):
        """每种形式的节奏都要带一个具体量级，「连着说几句」这种不行。

        实测（同一素材同一张卡，只换形式提示词）：写「连着说**几句**」，模型
        出 40 句、开头 `B A B A B A`、B 最长连说 2——一句一换，描述被当背景
        略过；写「连着说**三到六句**」，开头 `B B B A·B B B A`、连说≥3 有三段。
        **量级是那个开关，位置不是**（把它挪进编号验收段反而更散）。

        所以断言：每种形式的节奏里必须出现一个数（汉字或阿拉伯数字）。
        去掉数就等于退回「描述当背景」，形式上写了捧哏、稿子还是一问一答。
        """
        for key in PG.DIALOGUE_FORMS:
            rhythm = PG.rhythm_text(key)
            self.assertRegex(
                rhythm, r"[0-9一二两三四五六七八九十]",
                "%s 的节奏没有具体量级，模型会当背景略过" % key)

    def test_the_roles_land_in_the_hosts_section_only(self):
        """角色进【两位主持人】，节奏进【文体依据】——各回各处。

        从前两样都由一条说明兼着、整段挂在「这两位是谁」那一节里，模型把它当
        人物介绍读，读不出「该怎么写」。
        """
        cfg = self._cfg(**{"script.dialogue_form": "anchor"})
        card = PG.get("methodology")
        text = S.build_system_prompt(cfg, "argument", 6000, 200, paradigm=card)
        hosts = text.split("【两位主持人】", 1)[1]
        style = text.split("【文体依据】", 1)[1].split("\n【", 1)[0]
        self.assertIn("A 是捧哏", hosts)
        self.assertNotIn("B 连着说三到六句", hosts, "节奏不许留在人物介绍那一节")
        self.assertIn("成段推进（三到六句）", style)


class TestFormExample(unittest.TestCase):
    """每种对话形式的**形状示范**：用一段实例回答「谁在说、连着说几句」。

    为什么要有（实测，不是推理）：节奏句写准了、上限写全了，模型照样一句一换。
    描述回答「应当怎样」，示范回答「长什么样」——它得先看见一段。

    示范是**一个可能的形状**，不是必须照排的固定序列：只有 qa 把顺序写死这条
    规矩仍归 `rhythm_text`（见 `test_only_qa_pins_the_speaking_order`）。
    """

    def _cfg(self, **over):
        cfg = {"tts.name_a": "小美", "tts.name_b": "大美",
               "script.dialogue_form": "", "script.style_preset": "argument"}
        cfg.update(over)
        return cfg

    def _runs(self, rows):
        """把 speaker 序列压成 [(谁, 连着几句)]。"""
        out, cur, n = [], None, 0
        for r in rows:
            if r["speaker"] == cur:
                n += 1
            else:
                if cur:
                    out.append((cur, n))
                cur, n = r["speaker"], 1
        out.append((cur, n))
        return out

    def test_every_form_has_an_example(self):
        for key, f in PG.DIALOGUE_FORMS.items():
            rows = f.get("example") or []
            self.assertGreaterEqual(len(rows), 4, "%s 的示范太短，看不出节奏" % key)
            for r in rows:
                self.assertEqual(sorted(r), ["emotion", "speaker", "text"], key)
                self.assertIn(r["speaker"], ("A", "B"), key)

    def test_the_example_never_breaks_its_own_caps(self):
        """示范自己超限，等于当着模型的面破规矩——它照着抄就超。"""
        for key in PG.DIALOGUE_FORMS:
            cap_a, cap_b = PG.run_caps(key)
            for who, n in self._runs(PG.DIALOGUE_FORMS[key]["example"]):
                cap = cap_a if who == "A" else cap_b
                self.assertLessEqual(
                    n, cap, "%s 的示范里 %s 连说 %d 句，超上限 %d 句"
                    % (key, who, n, cap))

    def test_the_example_lines_would_pass_the_gates(self):
        """示范里每一句自己得合规：句长区间、句尾标点、语篇词表。

        不合规的示范是最坏的一种——它同时教了形状和写法，而写法那一半会被
        `line_length` / `emotion_vocab` 打回，模型还以为是形状错了。
        """
        from podcast_maker.config_manager import DISCOURSE_ORDER
        for key, f in PG.DIALOGUE_FORMS.items():
            for r in f["example"]:
                t = r["text"]
                self.assertGreaterEqual(len(t), 8, "%s：%s" % (key, t))
                self.assertLessEqual(len(t), 40, "%s：%s" % (key, t))
                self.assertIn(t[-1], "。？！", "%s：%s" % (key, t))
                self.assertIn(r["emotion"], list(DISCOURSE_ORDER),
                              "%s：标签「%s」不在本篇词表" % (key, r["emotion"]))

    def test_the_example_shows_the_shape_that_form_is_about(self):
        """示范要能被认出是它：qa 一句一换，捧哏 A 每次只一句、B 成段。"""
        qa = [r["speaker"] for r in PG.DIALOGUE_FORMS["qa"]["example"]]
        self.assertEqual(qa, ["A", "B", "A", "B"], qa)
        runs = self._runs(PG.DIALOGUE_FORMS["anchor"]["example"])
        self.assertTrue(all(n == 1 for w, n in runs if w == "A"), runs)
        self.assertTrue(any(n >= 3 for w, n in runs if w == "B"), runs)
        for key in ("alternate", "debate"):
            runs = self._runs(PG.DIALOGUE_FORMS[key]["example"])
            self.assertFalse(all(n == 1 for w, n in runs),
                             "%s 的示范是一句一换，看不出成段：%s" % (key, runs))
            for who in ("A", "B"):
                self.assertTrue(any(x == who and n >= 3 for x, n in runs),
                                "%s 的示范里 %s 没有成段讲一段：%s"
                                % (key, who, runs))

    def test_the_example_reaches_all_three_prompts(self):
        """整篇 / 分段 / 插入三处引同一份，且都写明不许照抄。"""
        card = {"label": "测试素材", "form": "anchor"}
        cfg = self._cfg(**{"script.dialogue_form": "anchor"})
        texts = {
            "整篇": S.build_system_prompt(cfg, "argument", 6000, 200,
                                         paradigm=card),
            "分段": S._segment_system_prompt(cfg, card, "argument", 1, 3, 900,
                                            "主旨", True),
            "插入": S.insert_system(card, cfg),
        }
        sample = PG.DIALOGUE_FORMS["anchor"]["example"][0]["text"]
        for name, text in texts.items():
            self.assertIn("长什么样", text, name)
            self.assertIn(sample, text, name)
            self.assertIn("不许照抄", text, name)

    def test_an_unknown_form_gives_an_empty_block(self):
        """空串是默认态：没选形式、或形式没写示范，调用点按「有就带上」处理。"""
        self.assertEqual(PG.example_block(""), "")
        self.assertEqual(PG.example_block(None), "")
        self.assertEqual(PG.example_block("不存在的形式"), "")
        for key in PG.DIALOGUE_FORMS:
            self.assertTrue(PG.example_block(key), key)


class TestRunCapsHaveOneSource(unittest.TestCase):
    """上限只有一个来源：`resolve_form()` 定形式、`run_caps()` 给两条数。

    分散在两处各算一遍是有前车之鉴的——上限从前长在卡上，提示词与门禁各取
    一次，还跟对话形式打架（形式说 B 成段讲、卡说最多两句）。下面几条钉住
    它不会又长回来。
    """

    def _src(self, name):
        with open(os.path.join(ROOT, "podcast_maker", name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_old_reader_is_gone(self):
        for name in ("script_engine.py", "paradigms.py"):
            self.assertNotIn("max_run_of", self._src(name),
                             "%s 里还有上限的旧取值口" % name)

    def test_the_cap_is_read_through_one_helper(self):
        src = self._src("script_engine.py")
        self.assertIn("def _run_caps(card, cfg):", src)
        # 直接取上限的动作只许落在 _run_caps 里：它之前（模块顶部）与之后
        # （各处调用点）都不许再直接碰 `paradigms.run_caps`。别处一律调
        # `_run_caps`——分头各取一次，就会出现提示词按一种形式写、门禁按
        # 另一种判的事，稿子陷在「改了还是不过」。
        head, rest = src.split("def _run_caps(card, cfg):", 1)
        self.assertNotIn("paradigms.run_caps(", head, "上限的取法只许有一处")
        tail = rest.split("# ------------------------------------------------------------------ 门禁", 1)[1]
        self.assertNotIn("paradigms.run_caps(", tail, "上限的取法只许有一处")

    def test_registered_cards_drop_the_stale_key(self):
        key = "_test_stale_run_card"
        try:
            card = PG.register(key, {"label": "旧卡", "max_run": 5})
            self.assertNotIn("max_run", card)
        finally:
            PG.PARADIGMS.pop(key, None)

    def test_insert_and_writing_share_the_same_blocks(self):
        """插入那一轮的词表、句长、风格行都引写作那几块，不再各写一份。

        各写一份的代价是硬的：写作按一种枚举写、插入按另一份写，插入句一落进
        正文就被 `emotion_vocab` 门禁打回，那一轮白写。
        """
        src = self._src("script_engine.py")
        # 定义一处 + 三处调用（整篇 / 分段 / 插入）
        self.assertEqual(src.count("_vocab_block()"), 4)
        self.assertEqual(src.count("_style_block(preset)"), 4)
        # 节奏与上限也只有一个取法口：各写一份就会出现插入按另一种节奏补句
        calls = re.findall(r"[^`]paradigms\.run_caps\(", src)
        self.assertEqual(len(calls), 1,
                         "上限只许在 _run_caps 里直接取（注释里提到不算），别处一律调它")
        # 插入那张模板里只许留按钮：句长与标签释义都从写作那几块取。
        # 写死过的地方是有的（正文提示词、压紧提示词各一处），所以要看模板本身，
        # 不是看整份文件。
        tmpl = src.split('_INSERT_SYSTEM_TMPL = """', 1)[1].split('"""', 1)[0]
        self.assertNotIn("8–40", tmpl, "插入的句长不许再写死")
        self.assertNotIn("8–30", tmpl, "插入的句长不许再写死")
        self.assertIn("%d–%d 字", tmpl, "插入的句长按钮记配置里那两个数")
        self.assertNotIn("承接=顺着往下讲", tmpl, "插入的标签释义不许再列一份")

    def test_no_template_hardcodes_the_line_range(self):
        """句长区间在每个模板里都是按钮，不是写死的 8–40。

        写死的代价是硬的：人把上限调到 30，提示词还按 40 说，模型写到 35 字，
        `line_length` 门禁按 30 判——一次调用白烧，下一轮还是同一段。插入、
        定点修补、压紧三处都翻过这个车。
        """
        src = self._src("script_engine.py")
        for name in ("_PATCH_SYSTEM_TMPL", "_TRIM_SYSTEM_TMPL",
                     "_INSERT_SYSTEM_TMPL"):
            tmpl = src.split(name + ' = """', 1)[1].split('"""', 1)[0]
            self.assertNotIn("8–40", tmpl, "%s 的句长不许写死" % name)
            self.assertIn("%d–%d 字", tmpl, "%s 的句长该按配置填" % name)
        # 修补与压紧两处确实把配置递了进去（不递就等于退回写死的默认值）
        self.assertIn("patch_system(cfg)", src)
        self.assertIn("trim_system(cfg)", src)

    def test_the_gate_says_where_the_caps_come_from(self):
        from podcast_maker.config_manager import GATE_BY_KEY
        judge = GATE_BY_KEY["ab_run_limit"]["judge"]
        self.assertIn("对话形式", judge)
        self.assertNotIn("max_run", judge)


if __name__ == "__main__":
    unittest.main(verbosity=2)
