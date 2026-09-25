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

"""中英文字距规范化（`tidy_text`）—— 汉字与西文之间补空格，别的都不动。

**它修的是什么**：脚本落盘那道收束从前跑的是 `re.sub(r"\\s+", "", text)`，把词与
词之间的边界一起删了。于是提示词里刚示范过的字距（`build_system_prompt` 的示例块
写着「含碳 1.2% 的铁」「stainless steel」），一到落盘就被抹平——**要求什么、抹掉
什么**。手改的那几句（`RAG Assistant 的…`）活得下来，只因为它们绕过了生成链路。
这一组钉五件事：

1. **规则表**：补哪三类（汉字↔拉丁字母、汉字↔阿拉伯数字、百分号→汉字）、
   **不补哪两类**（数字↔紧跟着的百分号、英文单词↔英文单词）。「不补」也是契约，
   反例一并钉住——不钉，后人「顺手补全」就又走回猜词那条路。
2. **只插不删**：去空白后逐字相同（字符守恒）；跑第二遍零变化（幂等）。
3. **接上落盘点**：`normalize_script` / `apply_replace` / `apply_insert` /
   `apply_trim` / `apply_patch`（并句与插入两条例外都过 `_drop_group`）/ `clean_title`
   六处都吃这一套——七处落盘只留一处口径。
4. **粘合那三句单独补**：片头、前期回顾、片尾是**从模板逐字拼**的，不经过正文那道
   收束（顺序见 `glue_intro_outro`：粘合在收束之后）。脏的不是模板，是喂进去的变量
   ——本期标题、上期标题、期主旨里常年带西文。所以字距在 `glue_intro_outro` 里补，
   位置在**补时长之前**。回顾句的唯一生产者是 `pipeline.review_rows`（重拼工具
   `tools/reglue_review.py` 走它、不走 glue），那里也补一次。
5. **提示词与代码同源**：示例块里那几条「正确」必须**原样过一遍落盘**，「错误」那条
   必须**被落盘改掉**——两边谁先漂了都会在这里响。

**不管英文分词**：`RAGAssistant` 该写 `RAG Assistant`，但一个字母串该切成几个词，
正则判不出来（`AI` 与 `Assistant` 是词，`NVIDIA` 是一个词），硬切就是猜。那一层归
提示词前置，这里只做保底：汉字与西文之间一处不漏。
"""

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import layout, pipeline as PL, project_store as P   # noqa: E402
from podcast_maker import script_engine as S                           # noqa: E402
from podcast_maker.config_manager import ConfigManager                 # noqa: E402


def _cfg(**over):
    data = ConfigManager().data()
    data.update({"project.program_name": "播客",
                 "project.audience": "关注方法论与认知边界",
                 "tts.name_a": "小思", "tts.name_b": "小笔"})
    data.update(over)
    return data


#: 提示词示例块里那几条「正确」的规范写法（从提示词里现取，见
#: `TestPromptAndCodeAgree`）。这里是**离线副本**，供规则表逐条对照。
SAMPLES = [
    ("汉字↔数字、数字↔百分号、百分号↔汉字、汉字↔西文、西文↔西文，五个边界在一句里",
     "含碳 1.2% 且含铬 10.5% 的铁属于 stainless steel，为什么这么说呢？"),
    ("汉字↔数字", "大明于 1368 年立朝。"),
    ("纯中文标点不动", "这是胜利的预言家在叫喊：——让暴风雨来得更猛烈些吧！"),
]


class TestTidyTextRules(unittest.TestCase):
    """规则表逐条：补的三类、不补的两类，各钉正反例。"""

    def test_han_and_latin_get_a_space(self):
        for raw, want in (("AI写的", "AI 写的"),
                          ("RAGAssistant的准确率", "RAGAssistant 的准确率"),
                          ("这套skill-standardization的设计", "这套 skill-standardization 的设计")):
            with self.subTest(raw=raw):
                self.assertEqual(S.tidy_text(raw), want)

    def test_han_and_digits_get_a_space(self):
        for raw, want in (("2026年", "2026 年"),
                          ("第L5数据声明", "第 L5 数据声明"),
                          ("讲了3点", "讲了 3 点")):
            with self.subTest(raw=raw):
                self.assertEqual(S.tidy_text(raw), want)

    def test_percent_followed_by_han_gets_a_space(self):
        self.assertEqual(S.tidy_text("占比15%规则验证通过。"),
                         "占比 15% 规则验证通过。")

    def test_digit_stays_glued_to_its_percent(self):
        """`12%` 是一个整体，拆开是错的——数字与紧跟着的百分号之间**不补**。"""
        for raw in ("12%", "36.5%", "100%", "占比 15%"):
            with self.subTest(raw=raw):
                self.assertEqual(S.tidy_text(raw), raw)

    def test_english_word_boundaries_are_left_alone(self):
        """管不到的那一类：一个字母串该切成几个词，正则判不出来（讲在 `tidy_text` 里）。

        钉它是为了**防「顺手补全」**：加一条 `(?<=[a-z])(?=[A-Z])` 之类的规则就能
        「修好」它，代价是对 `NVIDIA`、`iOS`、`LaTeX` 一起下手。
        """
        self.assertEqual(S.tidy_text("RAGAssistant"), "RAGAssistant")
        self.assertEqual(S.tidy_text("stainlesssteel"), "stainlesssteel")
        self.assertEqual(S.tidy_text("用iOS开发的"), "用 iOS 开发的")

    def test_chinese_punctuation_next_to_latin_is_not_spaced(self):
        """中文标点贴着西文不加空格：`，AI`、`（RAG）` 保持——标点不是词。"""
        for raw in ("，AI 与 RAG）", "「RAG」", "（AI）"):
            with self.subTest(raw=raw):
                self.assertEqual(S.tidy_text(raw), raw)

    def test_whitespace_runs_collapse_to_one_halfwidth_space(self):
        for raw, want in (("上期讲了  skill-standardization   的事。",
                           "上期讲了 skill-standardization 的事。"),
                          ("甲\u3000\u3000乙", "甲 乙"),
                          ("第一句\n第二句", "第一句 第二句"),
                          ("  两端空白  ", "两端空白")):
            with self.subTest(raw=raw):
                self.assertEqual(S.tidy_text(raw), want)

    def test_blank_is_blank(self):
        self.assertEqual(S.tidy_text(None), "")
        self.assertEqual(S.tidy_text(""), "")
        self.assertEqual(S.tidy_text("   "), "")
        self.assertEqual(S.tidy_text("\n\t "), "")

    def test_the_prompt_samples_pass_through_untouched(self):
        """提示词示范的写法，过一个落盘**逐字不变**——这就是这次要修的那件事。"""
        for why, sample in SAMPLES:
            with self.subTest(why=why):
                self.assertEqual(S.tidy_text(sample), sample)

    def test_only_inserts_never_deletes(self):
        """字符守恒：去掉全部空白后逐字相同。插进去的空格以外一个字都不许动。"""
        raws = [s for _, s in SAMPLES] + [
            "占比15%规则验证通过。", "RAGAssistant的准确率超过九成。",
            "上期讲了  skill-standardization   的事。", "第L5数据声明",
            "，AI 与 RAG）", "第一句\n第二句", "12%", "RAGAssistant"]
        for raw in raws:
            with self.subTest(raw=raw):
                self.assertEqual(re.sub(r"\s+", "", S.tidy_text(raw)),
                                 re.sub(r"\s+", "", raw))

    def test_idempotent(self):
        raws = [s for _, s in SAMPLES] + [
            "占比15%规则验证通过。", "RAGAssistant的准确率超过九成。",
            "，AI 与 RAG）", "上期讲了  skill-standardization   的事。"]
        for raw in raws:
            with self.subTest(raw=raw):
                once = S.tidy_text(raw)
                self.assertEqual(S.tidy_text(once), once)


class TestLandingPoints(unittest.TestCase):
    """七处落盘收口到同一个函数：正文收束、定点修补、替换、插入、压字数、标题。"""

    def setUp(self):
        self.cfg = _cfg()

    def test_normalize_script_keeps_what_the_prompt_asked_for(self):
        """**这条是整次改动的靶心**：提示词要求的写法，必须活着到达脚本。

        从前 `normalize_script` 跑的是「删光空白」，这一句进来会被拧成
        `RAG Assistant的分解准确率超过九成证明能力达标。` —— 提示词白写。
        """
        want = "RAG Assistant 的分解准确率超过九成证明能力达标。"
        out = S.normalize_script([{"speaker": "A", "emotion": "承接", "text": want}],
                                 self.cfg)
        self.assertEqual(out[0]["text"], want)

    def test_normalize_script_tidies_glued_text(self):
        rows = [{"speaker": "A", "emotion": "承接",
                 "text": "含碳12%的铁属于钢，RAGAssistant的准确率超过九成。"}]
        out = S.normalize_script(rows, self.cfg)
        self.assertEqual(out[0]["text"],
                         "含碳 12% 的铁属于钢，RAGAssistant 的准确率超过九成。")

    def test_apply_replace_tidies(self):
        script = [{"speaker": "A", "emotion": "承接",
                   "text": "含碳12%的铁属于钢，为什么这么说呢？"},
                  {"speaker": "B", "emotion": "承接",
                   "text": "这句话的来处得从材料本身的定义讲起才说得清。"}]
        out, rejected = S.apply_replace(
            script, [{"index": 1, "text": "这套AI写的方案是不是已经真正落地了。"}],
            [1], cfg=self.cfg)
        self.assertEqual(rejected, [])
        self.assertEqual(out[0]["text"], "这套 AI 写的方案是不是已经真正落地了。")

    def test_apply_insert_tidies(self):
        out = S.apply_insert(
            [{"speaker": "A", "emotion": "承接", "text": "先讲这一句。"}],
            [{"after": 0, "speaker": "B", "emotion": "承接",
              "text": "补一句：占比15%的样本没通过。"}],
            0, cfg=self.cfg)
        self.assertEqual(out[0]["text"], "补一句：占比 15% 的样本没通过。")

    def test_apply_trim_tidies(self):
        out = S.apply_trim(
            [{"speaker": "A", "emotion": "承接", "text": "先讲这一句。"},
             {"speaker": "B", "emotion": "承接", "text": "再讲这一句。"}],
            [{"index": 1, "text": "改成讲AI写的这一段。"}], [], 0, cfg=self.cfg)
        self.assertEqual(out[0]["text"], "改成讲 AI 写的这一段。")

    def test_apply_patch_inserts_tidies(self):
        """插入那条例外走 `_drop_group`，与替换那条是同一处收口。"""
        script = [{"speaker": "A", "emotion": "承接", "text": "第一句。"}]
        out, rejected = S.apply_patch(
            script, [], [1],
            inserts=[{"after": 1, "speaker": "B", "emotion": "承接",
                      "text": "补一句RAGAssistant的事。"}], cfg=self.cfg)
        self.assertEqual(rejected, [])
        self.assertEqual(out[1]["text"], "补一句 RAGAssistant 的事。")

    def test_apply_patch_absorb_tidies(self):
        """并句那条例外也走 `_drop_group`：并出来的句子同样要过字距。"""
        script = [{"speaker": "A", "emotion": "承接",
                   "text": "含碳12%的铁属于钢为什么这么说呢"},
                  {"speaker": "A", "emotion": "承接",
                   "text": "RAGAssistant的分解准确率达标"},
                  {"speaker": "B", "emotion": "承接",
                   "text": "这句是别人的话，不该被并进去。"}]
        out, rejected = S.apply_patch(
            script,
            [{"index": 1, "absorb": 1,
              "text": "这套AI写的方案确实能用而且RAGAssistant的准确率达标了"}],
            [1, 2], cfg=self.cfg)
        self.assertEqual(rejected, [])
        self.assertEqual(out[0]["text"],
                         "这套 AI 写的方案确实能用而且 RAGAssistant 的准确率达标了")
        self.assertEqual(len(out), 2, "并掉紧随的一句：行数只减一行")

    def test_clean_title_tidies_and_still_trims_the_prefix(self):
        for raw, want in (("第 3 期 · RAGAssistant的架构重构", "RAGAssistant 的架构重构"),
                          ("《Orchestrator v2.8 观察》", "Orchestrator v2.8 观察"),
                          ("AI写的书", "AI 写的书")):
            with self.subTest(raw=raw):
                self.assertEqual(S.clean_title(raw), want)
        self.assertEqual(len(S.clean_title("标" * 80)), 24, "限长照旧")


class TestGluedTemplates(unittest.TestCase):
    """片头／回顾／片尾是从模板逐字拼的，不经过正文收束——字距得在这儿单独补一次。"""

    def setUp(self):
        self.cfg = _cfg()
        self.body = [{"speaker": "A", "emotion": "承接",
                      "text": "正文第一句内容足够长可以当例子。"},
                     {"speaker": "B", "emotion": "承接",
                      "text": "正文第二句内容也足够长可以当例子。"}]

    def test_intro_line_tidies_the_title_variable(self):
        """模板是干净的，脏的是喂进去的变量——本期标题里带西文。"""
        out = S.glue_intro_outro(self.body, self.cfg, title="RAGAssistant的架构重构")
        self.assertEqual(out[1]["text"],
                         "本期讲述 RAGAssistant 的架构重构，播讲人小思、小笔。")
        self.assertEqual(out[0]["text"], "欢迎收听《播客》，面向关注方法论与认知边界的听众。")

    def test_review_rows_passed_in_are_tidied_here_too(self):
        rows = [{"speaker": "B", "emotion": "回顾",
                 "text": "上期《RAGAssistant的架构》聊的是占比15%的样本。"}]
        out = S.glue_intro_outro(self.body, self.cfg, title="甲", review=rows)
        self.assertEqual(out[1]["text"],
                         "上期《RAGAssistant 的架构》聊的是占比 15% 的样本。")
        self.assertEqual(out[1]["emotion"], "回顾")

    def test_glue_does_not_touch_the_body(self):
        body = S.normalize_script(self.body, self.cfg)
        out = S.glue_intro_outro(body, self.cfg, title="甲")
        self.assertEqual([l["text"] for l in out[2:-1]], [l["text"] for l in body])


class TestReviewRowsSpacing(unittest.TestCase):
    """回顾句的唯一生产者是 `pipeline.review_rows`——重拼工具走它、不走 glue。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm-spacing-")
        self.p = P.create(self.base, "甲档", "mapped")
        P.save_map(self.base, self.p["id"], "", [
            {"no": "1", "title": "RAGAssistant的架构", "gist": "占比15%的样本才是关键",
             "points": [], "refs": [], "chars": 100},
            {"no": "2", "title": "第二期标题", "gist": "第二期主旨",
             "points": [], "refs": [], "chars": 100},
        ])
        self.item = P.find(self.base, self.p["id"])
        self.cfg = _cfg(**{"intro_outro.review": True})
        root = layout.project_dir(self.base, self.p["id"])
        os.makedirs(layout.script_dir(root), exist_ok=True)
        PL.write_json(layout.plan_file(root, "1"),
                      {"episode_no": "1", "title": "",
                       "segments": [{"no": 1, "topic": "AI写的接口", "sections": [1],
                                     "quota": 100}]})

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_review_quotes_are_tidied(self):
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertEqual([r["text"] for r in rows],
                         ["上期《RAGAssistant 的架构》。",
                          "聊的是占比 15% 的样本才是关键。",
                          "讲了 AI 写的接口等。"])


class TestPromptAndCodeAgree(unittest.TestCase):
    """提示词里那几条「正确」必须原样过落盘，「错误」那条必须被落盘改掉。

    这是**唯一一处把提示词与代码焊在一起**的地方：示例块里的写法改了、或者收束那边
    又改回「删光空白」，这里都会响。两边单独看都自洽，合起来才对。
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = _cfg()
        cls.prompt = S.build_system_prompt(cls.cfg, None, 800, 20)
        cls.seg_prompt = S._segment_system_prompt(
            cls.cfg, S.resolve_paradigm(None, cls.cfg), "argument", 1, 3, 800,
            "段主旨", True)

    def _rows(self, prompt, word):
        out = []
        for line in prompt.splitlines():
            line = line.strip()
            if not line.startswith(word + "："):
                continue
            try:
                out.append(json.loads(line[len(word) + 1:]))
            except ValueError:
                continue
        return out

    def test_correct_samples_survive_tidying(self):
        rows = self._rows(self.prompt, "正确")
        self.assertGreaterEqual(len(rows), 3, "示例块里的「正确」至少三条")
        for r in rows:
            with self.subTest(text=r["text"]):
                self.assertEqual(S.tidy_text(r["text"]), r["text"],
                                 "提示词说这么写是对的，落盘就得原样留下")

    def test_wrong_sample_is_actually_fixed_by_the_code(self):
        """反证：提示词那条「错误」写的就是**代码会改掉**的形态。

        两边若各说各话（提示词说「要加空格」、代码不认），这条会响。注意它只保证
        「被改过」——`stainlesssteel` 那种英文分词不归这一层，改不动。
        """
        rows = self._rows(self.prompt, "错误")
        self.assertTrue(rows, "示例块里得有一条「错误」")
        self.assertNotEqual(S.tidy_text(rows[0]["text"]), rows[0]["text"])

    def test_both_prompts_carry_the_same_samples(self):
        """整篇与分段两份提示词逐字同源（示例块是复制的一份，改动必须成对）。"""
        self.assertEqual(self._rows(self.prompt, "正确"),
                         self._rows(self.seg_prompt, "正确"))
        self.assertEqual(self._rows(self.prompt, "错误"),
                         self._rows(self.seg_prompt, "错误"))


if __name__ == "__main__":
    unittest.main()
