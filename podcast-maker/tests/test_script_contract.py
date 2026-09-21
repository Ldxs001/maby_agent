#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 [username-redacted]
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

"""脚本契约：标题随脚本产出，期数要么人定要么模型定。

第七类缺陷：产物里的标题不由脚本产出，于是「脚本页」拿到了稿子却没有名字，
「合成页」反过来还要人再输一次标题。同一个字段两处来源，谁是准的说不清，
人输的那份改错了也没人拦。所以顶层契约必须是「脚本自带标题」，合成页只做修改。
"""

import json
import math
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import paradigms as PG, script_engine as S            # noqa: E402
from podcast_maker.config_manager import EMOTION_TAGS, ConfigManager       # noqa: E402
from podcast_maker.script_engine import ScriptError                       # noqa: E402


def _lines(n=8):
    out = []
    for i in range(n):
        out.append({"speaker": "A" if i % 2 == 0 else "B",
                    "emotion": "承接", "text": "第 %d 句台词内容够长够取标题。" % i})
    return out


class TestSchemaContract(unittest.TestCase):
    def test_schema_is_an_object_with_title(self):
        self.assertEqual(S.SCRIPT_SCHEMA["type"], "object")
        props = S.SCRIPT_SCHEMA["properties"]
        self.assertIn("title", props)
        self.assertIn("planned_episodes", props)
        self.assertIn("lines", props)
        self.assertEqual(S.SCRIPT_SCHEMA["required"], ["title", "lines"])

    def test_title_is_bounded_for_the_cover(self):
        # 封面给标题留的字号是按不折行算的，超长就会撑破版式
        self.assertGreaterEqual(S.TITLE_MAX, 8)
        self.assertLessEqual(S.TITLE_MAX, 24)


class TestParseScript(unittest.TestCase):
    def test_object_form_carries_title_and_plan(self):
        raw = ('{"title": "链与两头", "planned_episodes": 12, '
               '"lines": [{"speaker":"A","emotion":"承接","text":"甲"}]}')
        got = S.parse_script(raw)
        self.assertEqual(got["title"], "链与两头")
        self.assertEqual(got["planned_episodes"], 12)
        self.assertEqual(len(got["lines"]), 1)

    def test_legacy_array_form_still_parses_without_title(self):
        raw = '[{"speaker":"A","emotion":"承接","text":"甲"},' \
              '{"speaker":"B","emotion":"激动","text":"乙"}]'
        got = S.parse_script(raw)
        self.assertEqual(got["title"], "")
        self.assertIsNone(got["planned_episodes"])
        self.assertEqual(len(got["lines"]), 2)

    def test_code_fence_is_tolerated(self):
        raw = '```json\n{"title":"甲","lines":[{"speaker":"A","text":"x"}]}\n```'
        self.assertEqual(S.parse_script(raw)["title"], "甲")

    def test_zero_plan_means_let_the_model_decide(self):
        raw = '{"title":"甲","planned_episodes":0,"lines":[{"speaker":"A","text":"x"}]}'
        self.assertIsNone(S.parse_script(raw)["planned_episodes"])

    def test_garbage_raises(self):
        for raw in ("", "完全不是 JSON", '{"lines": "不是数组"}', "[1,2]"):
            with self.subTest(raw=raw):
                with self.assertRaises(ScriptError):
                    S.parse_script(raw)


class TestCleanTitle(unittest.TestCase):
    def test_wrapping_marks_are_stripped(self):
        for raw, want in (("《链与两头》", "链与两头"),
                          ("「链与两头」", "链与两头"),
                          ("【链与两头】", "链与两头"),
                          ('"链与两头"', "链与两头")):
            with self.subTest(raw=raw):
                self.assertEqual(S.clean_title(raw), want)

    def test_episode_prefix_is_stripped(self):
        for raw in ("第 3 期 · 链与两头", "第3期：链与两头", "第 12 期-链与两头"):
            with self.subTest(raw=raw):
                self.assertEqual(S.clean_title(raw), "链与两头")

    def test_length_is_bounded(self):
        self.assertEqual(len(S.clean_title("标" * 80)), 24)

    def test_blank_is_blank(self):
        self.assertEqual(S.clean_title(None), "")
        self.assertEqual(S.clean_title("   "), "")


class TestTitleFallback(unittest.TestCase):
    def test_picks_from_the_body_not_the_greeting(self):
        # 前两句是片头问候，信息量为零，不能拿来当标题
        lines = [{"text": "你好，欢迎收听"},
                 {"text": "今天我们聊聊"},
                 {"text": "链条上的两头都松了"},
                 {"text": "另一句很长的台词内容"}]
        self.assertEqual(S.title_from_lines(lines), "链条上的两头都松了")

    def test_falls_back_to_first_usable_line(self):
        lines = [{"text": "你好"}, {"text": "短"}, {"text": "这句够长了可以用"}]
        self.assertEqual(S.title_from_lines(lines), "这句够长了可以用")

    def test_nothing_usable_returns_blank(self):
        self.assertEqual(S.title_from_lines([]), "")
        self.assertEqual(S.title_from_lines([{"text": "嗨"}]), "")


class TestPromptContract(unittest.TestCase):
    """提示词是写给模型的接口说明，它必须与解析器认的格式一致。"""

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}

    def test_prompt_asks_for_an_object_not_a_bare_array(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        self.assertIn("title", p)
        self.assertIn("lines", p)
        self.assertIn("只输出一个 JSON 对象", p)

    def test_prompt_tells_the_model_not_to_write_the_fixed_lines(self):
        """片头尾由程序在整期定稿那一刻粘上，提示词要**反过来**说：一句都不要写。

        这里从前断言的是「提示词里写着与代码相同的那两句，模型照着抄」。现在那
        两句根本不在稿子里，提示词再说「照抄」，模型就会写出一份问候语——粘合
        之后与程序那一句挨在一起，同一期出现两遍片头。
        """
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        self.assertIn("【片头片尾】", p)
        self.assertIn("不归你写", p)
        self.assertNotIn("欢迎收听《示例节目》", p)
        self.assertNotIn("第 1 句固定为", p)

    def test_prompt_falls_back_when_program_name_is_blank(self):
        p = S.build_system_prompt(dict(self.CFG, **{"project.program_name": ""}),
                                  "argument", 900, 24)
        self.assertIn("播客", p)

    def test_prompt_forbids_redeciding_a_set_plan(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                  project={"episode_no": "3",
                                           "planned_episodes": 10})
        self.assertIn("10", p)
        self.assertIn("3", p)

    def test_prompt_lets_the_model_propose_a_plan_when_unset(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                  project={"episode_no": "1",
                                           "planned_episodes": None})
        self.assertIn("planned_episodes", p)


class TestIntroOutroGluedAtSave(unittest.TestCase):
    """片头尾是固定标识，由程序在**整期定稿那一刻**粘上去，正文一句不改、一句不删。

    从前那一版是把片头句**替换**到正文第一句、片尾句**替换**到正文最后一句。
    于是每期的正文头尾各丢一句：第 2 句还在应答一句已经不存在的开场白（听感上
    就是「没错」开头），倒数第二句提的问题永远等不到回答。这一组把「只粘不改」
    焊死——它是这版改动里最容易被后人改回去的一条。
    """

    def cfg(self, **over):
        data = ConfigManager().data()
        data.update({"project.program_name": "播客",
                     "project.audience": "关注方法论与认知边界",
                     "tts.name_a": "小美", "tts.name_b": "大美"})
        data.update(over)
        return data

    def body(self, n=12):
        return [{"speaker": "A" if i % 2 == 0 else "B",
                 "emotion": "平静",
                 "text": "第 %d 句正文内容示例文字。" % i} for i in range(n)]

    def test_head_and_tail_are_added_and_the_body_survives(self):
        script, cfg = self.body(), self.cfg()
        out = S.glue_intro_outro(script, cfg, title="零依赖拆解与渐进")
        self.assertEqual(len(out), len(script) + 3)
        # 粘合会跑一遍 normalize（给新粘上的句子补 estimated_seconds），正文也
        # 一并走一遍——它是幂等的，已经 normalize 过的正文原样回来。所以这里拿
        # normalize 之后的正文去比：比的是「句子还在、还在原位」，不是空白符。
        self.assertEqual([l["text"] for l in out[2:-1]],
                         [l["text"] for l in S.normalize_script(script, cfg)],
                         "正文一句不改、一句不删，整体往后挪两位")
        self.assertEqual(out[0]["text"],
                         "欢迎收听《播客》，面向关注方法论与认知边界的听众。")
        self.assertEqual(out[1]["text"],
                         "本期讲述零依赖拆解与渐进，播讲人小美、大美。")
        self.assertEqual(out[-1]["text"], "这里是《播客》，欢迎关注。")

    def test_the_original_is_left_untouched(self):
        """循环里那份稿子必须原样不动。

        下一轮的定点修补按**句号**改句子；要是它拿到的是粘合版，句号整体后移
        两位，报「第 12 句」改到的其实是正文第 10 句——改错句子，还不报错。
        """
        script = self.body()
        before = [dict(l) for l in script]
        S.glue_intro_outro(script, self.cfg(), title="甲")
        self.assertEqual(script, before)

    def test_speakers_alternate_so_the_seam_cannot_pile_up(self):
        """片头 A→B、片尾 B：接缝处最多两句同一个人，撞不破连续句上限。

        片头要是两句都归 A，正文第一句又是 A，接缝处就是三连——而 ab_run_limit
        是 fail 级，一道纯格式的坎会卡住整期（它现在能定点并句修，也该从源头避开）。
        """
        out = S.glue_intro_outro(self.body(), self.cfg(), title="甲")
        self.assertEqual([l["speaker"] for l in out[:3]], ["A", "B", "A"])
        self.assertEqual(out[-1]["speaker"], "B")

    def test_audience_clause_disappears_when_the_project_has_none(self):
        """没填受众 → 整段消失，不留「面向的听众」这种半句被 TTS 念出来。"""
        out = S.glue_intro_outro(self.body(), self.cfg(**{"project.audience": ""}),
                                 title="甲")
        self.assertEqual(out[0]["text"], "欢迎收听《播客》。")
        self.assertNotIn("面向", out[0]["text"])

    def test_missing_title_drops_that_line_instead_of_half_a_sentence(self):
        """标题为空就少粘一句。粘出「本期讲述，播讲人小美、大美」比少一句坏得多。"""
        out = S.glue_intro_outro(self.body(), self.cfg(), title="")
        self.assertEqual(len(out), len(self.body()) + 2, "少粘一句，不粘半句")
        self.assertFalse(any("本期讲述" in l["text"] for l in out))

    def test_names_clause_disappears_when_both_names_are_empty(self):
        cfg = self.cfg(**{"tts.name_a": "", "tts.name_b": ""})
        out = S.glue_intro_outro(self.body(), cfg, title="零依赖拆解与渐进")
        self.assertEqual(out[1]["text"], "本期讲述零依赖拆解与渐进。")

    def test_brief_preset_keeps_only_the_welcome(self):
        cfg = self.cfg(**{"intro_outro.preset": "brief"})
        out = S.glue_intro_outro(self.body(), cfg, title="零依赖拆解与渐进")
        self.assertEqual(len(out), len(self.body()) + 2)
        self.assertEqual(out[0]["text"],
                         "欢迎收听《播客》，面向关注方法论与认知边界的听众。")
        self.assertEqual(out[-1]["text"], "这里是《播客》，欢迎关注。")

    def test_glued_lines_carry_their_own_discourse_tags(self):
        """片头尾的语篇标签在模板里写死，且**都在词表内**，不靠 normalize 兜底。

        「开场 / 收束」已从词表除名（v0.34.1）：片头尾由程序粘，正文没有哪句
        该背这两个标签；模板改用中性档「承接」，保证粘出来的整份文件重判时
        不会吃到「词表外标签」。
        """
        out = S.glue_intro_outro(self.body(), self.cfg(), title="甲")
        self.assertEqual(out[0]["emotion"], "承接")
        self.assertEqual(out[1]["emotion"], "承接")
        self.assertEqual(out[-1]["emotion"], "承接")
        brief = S.glue_intro_outro(self.body(),
                                   self.cfg(**{"intro_outro.preset": "brief"}),
                                   title="甲")
        self.assertEqual(brief[0]["emotion"], "承接")
        self.assertEqual(brief[-1]["emotion"], "承接")

    def test_removed_tags_are_out_of_vocab_everywhere(self):
        """「开场 / 收束」全链路除名：词表、释义表、片头尾模板都不得再出现。"""
        from podcast_maker.config_manager import DISCOURSE_ORDER, INTRO_OUTRO
        self.assertNotIn("开场", DISCOURSE_ORDER)
        self.assertNotIn("收束", DISCOURSE_ORDER)
        self.assertNotIn("开场", S.DISCOURSE_HELP)
        self.assertNotIn("收束", S.DISCOURSE_HELP)
        blob = json.dumps(INTRO_OUTRO, ensure_ascii=False)
        self.assertNotIn("开场", blob)
        self.assertNotIn("收束", blob)

    def test_blank_program_name_falls_back_instead_of_rendering_empty(self):
        """节目名留空时若原样使用空串，片头会变成「欢迎收听《》」。"""
        cfg = self.cfg(**{"project.program_name": ""})
        self.assertEqual(S.program_name_of(cfg), "播客")
        out = S.glue_intro_outro(self.body(), cfg, title="甲")
        self.assertIn("《播客》", out[0]["text"])

    def test_glued_lines_get_their_own_seconds(self):
        """新粘上的句子也得有 estimated_seconds：字幕时间轴与总时长都读它。"""
        out = S.glue_intro_outro(self.body(), self.cfg(), title="甲")
        for line in out:
            self.assertGreater(line["estimated_seconds"], 0)

    def test_same_body_always_glues_the_same_way(self):
        """同样的正文与配置，粘出来逐字一样——各期一致靠的就是这条。"""
        cfg, script = self.cfg(), self.body()
        self.assertEqual(S.glue_intro_outro(script, cfg, title="甲"),
                         S.glue_intro_outro(self.body(), cfg, title="甲"))

    # ---- 前期回顾：与片头尾同类（程序拼、不进轮），位置在片头之后、正文之前 ----

    def test_review_sits_right_after_the_head(self):
        """回顾的位置：**片头之后、正文之前**。

        它是「上期讲到哪儿」的交代，得在正文开始前让听众听到；摆到正文后面就
        成尾声的一部分了。所以粘合顺序是 片头 → 回顾 → 正文 → 片尾。
        """
        rows = [{"speaker": "B", "emotion": "承接",
                 "text": "上期《甲》聊的是乙——讲了丙、丁、戊等。"}]
        out = S.glue_intro_outro(self.body(), self.cfg(), title="己", review=rows)
        self.assertEqual(len(out), len(self.body()) + 4, "片头 2 + 回顾 1 + 片尾 1")
        self.assertEqual(out[2]["text"], "上期《甲》聊的是乙——讲了丙、丁、戊等。")
        # 正文整体后挪三位，一句不改、一句不删
        self.assertEqual([l["text"] for l in out[3:-1]],
                         [l["text"] for l in S.normalize_script(self.body(),
                                                                self.cfg())],
                         "回顾只往中间加，正文零损失")

    def test_review_follows_a_brief_head_too(self):
        """精简档片头只有一句，回顾照样紧跟在它后面。"""
        cfg = self.cfg(**{"intro_outro.preset": "brief"})
        rows = [{"speaker": "B", "emotion": "承接", "text": "上期《甲》讲的是乙。"}]
        out = S.glue_intro_outro(self.body(), cfg, title="己", review=rows)
        self.assertEqual([l["text"] for l in out[:2]],
                         ["欢迎收听《播客》，面向关注方法论与认知边界的听众。",
                          "上期《甲》讲的是乙。"])

    def test_no_review_means_not_a_single_line_more(self):
        """没给回顾（开关关、或上一期取不到）时，输出与从前逐字一致。"""
        base = S.glue_intro_outro(self.body(), self.cfg(), title="甲")
        for nothing in (None, []):
            self.assertEqual(
                S.glue_intro_outro(self.body(), self.cfg(), title="甲",
                                   review=nothing), base, repr(nothing))

    def test_review_lines_get_their_own_seconds(self):
        """粘进来的回顾句也要有 estimated_seconds，否则字幕上它是 0 秒。"""
        rows = [{"speaker": "B", "emotion": "承接", "text": "上期《甲》讲的是乙。"}]
        out = S.glue_intro_outro(self.body(), self.cfg(), title="己", review=rows)
        self.assertGreater(out[2]["estimated_seconds"], 0)


class FakeLLM(object):
    """只回一句固定 payload 的假模型，用来验串联而不引入模型的不确定性。"""

    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        return self.payload, {"degraded": False}


class TestGenerateIntegration(unittest.TestCase):
    def setUp(self):
        # 只验「片头尾由代码写死」与「期数以项目为准」这两条串联。
        # 时长类门禁在别处已单独测过，留着它会把测试语料绑死在字数上。
        self.cfg = ConfigManager().data()
        self.cfg.update({"script.gate_strict": False,
                         "gate.min_deviation_seconds": 9999,
                         "project.program_name": "播客",
                         # 两位称呼写死在本文件里：片头第二句要用它们，跟着
                         # config.json 走的话，谁改一下全局称呼这里就红。
                         "tts.name_a": "小思", "tts.name_b": "小笔"})

    def payload(self, plan=7):
        lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平静",
                  "text": "第 %d 句正文内容示例文字。" % i} for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": plan,
                           "lines": lines}, ensure_ascii=False)

    def test_model_text_cannot_break_the_fixed_lines(self):
        """模型把首尾写成什么样，都不影响那两句固定结构——但它的正文必须留下。

        这一条盯的正是这次修的那个 bug：从前片头句是**替换**正文第一句的，于是
        第 2 句成了对着空气点头（「没错，这属于…」），倒数第二句的提问也永远等
        不到回答。现在片头尾只往两头加，正文一句不动。
        """
        bad = json.loads(self.payload())
        bad["lines"][0]["text"] = "模型写的开场内容，说的是一件具体的事。"
        bad["lines"][-1]["text"] = "模型写的收束内容，回答上面那个问题。"
        gen = S.generate("素材内容示例", self.cfg,
                         FakeLLM(json.dumps(bad, ensure_ascii=False)))
        texts = [l["text"] for l in gen["script"]]
        self.assertEqual(texts[0], "欢迎收听《播客》。")
        self.assertEqual(texts[1], "本期讲述链与两头，播讲人小思、小笔。")
        self.assertEqual(texts[2], "模型写的开场内容，说的是一件具体的事。",
                         "正文第一句不能被片头句顶掉")
        self.assertEqual(texts[-2], "模型写的收束内容，回答上面那个问题。",
                         "正文最后一句不能被片尾句顶掉")
        self.assertEqual(texts[-1], "这里是《播客》，欢迎关注。")
        self.assertTrue(gen["report"]["passed"], "门禁判的是正文，不该有片头尾失败项")
        self.assertNotIn("intro_outro", [i["key"] for i in gen["report"]["items"]])

    def test_project_plan_overrides_the_model_number(self):
        gen = S.generate("素材内容示例", self.cfg,
                         FakeLLM(self.payload(plan=7)),
                         project={"episode_no": "3", "planned_episodes": 10})
        self.assertEqual(gen["planned_episodes"], 10,
                         "人定的期数不该被模型的 7 覆盖")

    def test_model_suggestion_is_kept_when_no_plan_is_set(self):
        gen = S.generate("素材内容示例", self.cfg,
                         FakeLLM(self.payload(plan=7)),
                         project={"episode_no": "1", "planned_episodes": None})
        self.assertEqual(gen["planned_episodes"], 7)


class ScriptedLLM(object):
    """按调用顺序分路返回的假模型。

    三条支路靠 system 提示词区分：审校是内容检，定点修补是改句，其余是整篇写作。
    分开数调用次数，才能验「内容检跑了几轮」「定点修补开没开」而不被写作调用混进来。
    """

    def __init__(self, script_payload, content_replies, patch_replies=None):
        self.script_payload = script_payload
        self.content_replies = list(content_replies)
        self.patch_replies = list(patch_replies or [])
        self.content_calls = 0
        self.patch_calls = 0
        self.users = []          # 整篇写作的调用
        self.patch_users = []    # 定点修补的调用
        self.content_users = []  # 内容检的调用
        self.schemas = []        # 每次调用带的输出契约（None = 没带）

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        system = messages[0].get("content") or ""
        if "审校" in system:
            self.content_calls += 1
            self.content_users.append(messages[-1].get("content") or "")
            self.schemas.append(json_schema)
            if self.content_replies:
                return self.content_replies.pop(0), {}
            return ('{"semantic": {"pass": true, "issues": []}, '
                    '"promise": {"pass": true, "issues": []}}'), {}
        if "定点修补" in system:
            self.patch_calls += 1
            self.patch_users.append(messages[-1].get("content") or "")
            self.schemas.append(json_schema)
            if self.patch_replies:
                return self.patch_replies.pop(0), {}
            return '{"edits": []}', {}
        self.users.append(messages[-1].get("content") or "")
        self.schemas.append(json_schema)
        return self.script_payload, {"degraded": False}


#: 带落点的语义检结论：第 3 句编了数据。旧形态那种「第3句编了数据」是一句人话、
#: 没有句号字段，按新契约属于「定不了点」，所以这里必须给结构化写法。
SEM_FAIL_AT_3 = ('{"semantic": {"pass": false, "issues": ['
                 '{"line": 3, "quote": "第 2 句", "problem": "编了素材里没有的数字"}]},'
                 ' "promise": {"pass": true, "issues": []}}')
SEM_PASS = ('{"semantic": {"pass": true, "issues": []},'
            ' "promise": {"pass": true, "issues": []}}')


class TestStopBetweenRounds(unittest.TestCase):
    """中止只在轮次之间生效，且要带着已经写好的那一版收工。

    脚本挪到后台任务之后，"中止"这个按钮才真的有人接——从前它是同步接口，
    页面上的中止按下去没人听。这里盯两件事：它真的少开了一轮，且停之前那一版
    完好无损地交了回来。正在跑的那次调用掐不断（本地模型一个请求就是一次不
    可分割的生成），但"掐不断"不等于要装作没收到。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg.update({"script.gate_strict": False,
                         "gate.min_deviation_seconds": 9999,
                         "project.program_name": "播客",
                         "script.max_llm_rounds": 3})

    def _payload(self):
        lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平静",
                  "text": "第 %d 句正文内容示例文字。" % i} for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": 7,
                           "lines": lines}, ensure_ascii=False)

    def test_stop_keeps_the_round_already_written(self):
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8)
        logs = []
        gen = S.generate("素材内容示例", self.cfg, llm, log=logs.append,
                         should_stop=lambda: True)
        self.assertEqual(len(llm.users), 1, "叫停之后不该再开新一轮")
        self.assertEqual(llm.patch_calls, 0, "叫停之后也不该再开一轮定点修补")
        # 12 句正文 + 片头两句 + 片尾一句。片头尾照粘：它们不是「门禁通过」的
        # 奖励，是每一期都该有的固定结构。
        self.assertEqual(len(gen["script"]), 15, "叫停时要交回上一轮的完整稿子")
        self.assertEqual(len(gen["script"][2:-1]), 12)
        self.assertTrue(any("收到中止" in m for m in logs), logs)

    def test_without_stop_it_uses_the_whole_round_budget(self):
        """对照组：没人叫停就把轮次预算用完，免得"少开一轮"变成一直只跑一轮。

        预算用完不等于整篇重出四遍——只有第 1 轮整篇写，之后每轮都只是定点修补。
        """
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容。"}]}'] * 8)
        S.generate("素材内容示例", self.cfg, llm)
        self.assertEqual(len(llm.users) + llm.patch_calls, 4,
                         "上限 3 轮重试 → 一共开 4 轮")
        self.assertEqual(len(llm.users), 1, "只有第 1 轮是整篇写")


class TestContentCheckInGenerateLoop(unittest.TestCase):
    """内容检要真的挂在「生成 → 检 → 定点修」这条线上。

    挂在产物之后、或者只 advisory 不参与判定，看着都有个检查，却一次也拦不住。
    所以这里盯的是两件事：它拦得下、且带着检出的落点去定点修。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg.update({"script.gate_strict": False,
                         "gate.min_deviation_seconds": 9999,
                         "project.program_name": "播客",
                         "script.max_llm_rounds": 3})

    def _payload(self, tag=""):
        lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平静",
                  "text": "%s第 %d 句正文内容示例文字。" % (tag, i)}
                 for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": 7,
                           "lines": lines}, ensure_ascii=False)

    def test_failed_semantic_is_fixed_pointwise(self):
        """内容检给了句号 → 只改那几句，不再整篇重出。"""
        llm = ScriptedLLM(
            self._payload(), [SEM_FAIL_AT_3, SEM_PASS],
            patch_replies=['{"edits": [{"index": 3, "text": '
                           '"这一句改成了不含数字的说法示例。"}]}'])
        gen = S.generate("素材内容示例", self.cfg, llm)
        self.assertEqual(len(llm.users), 1, "第 1 轮整篇写，之后不该再整篇重出")
        self.assertEqual(llm.patch_calls, 1, "第 2 轮应走定点修补")
        self.assertIn("第 3 句", llm.patch_users[0], "修补提示词要带上被点名的那一句")
        self.assertNotIn("第 11 句", llm.patch_users[0],
                         "只给被点名句与它的邻句，不该把全篇递过去")
        self.assertEqual(llm.content_calls, 2, "第 1 轮全判，第 2 轮只重判没过的那一项")
        self.assertTrue(gen["report"]["passed"])
        self.assertTrue(gen["report"]["content_checked"])

    def test_promise_verdict_is_carried_when_only_semantic_is_rejudged(self):
        """只重判一项时，另一项的结论要跟着走，报告里不能凭空少一行。"""
        llm = ScriptedLLM(
            self._payload(), [SEM_FAIL_AT_3, SEM_PASS],
            patch_replies=['{"edits": [{"index": 3, "text": '
                           '"这一句改成了不含数字的说法示例。"}]}'])
        gen = S.generate("素材内容示例", self.cfg, llm)
        keys = [i["key"] for i in gen["report"]["items"]]
        self.assertIn("check_promise", keys, "承诺链检没重判，但结论必须留在报告里")

    def test_located_problem_still_unfixed_after_rounds_exhausted(self):
        """一直编造就一直是 fail：重试耗尽后报告不许「通过」。"""
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容。"}]}'] * 8)
        gen = S.generate("素材内容示例", self.cfg, llm)
        self.assertFalse(gen["report"]["passed"], "语义检一直不过，报告不能是 passed")
        self.assertTrue(any(i["key"] == "check_semantic" and not i["ok"]
                            for i in gen["report"]["items"]))

    def test_unlocated_problem_is_not_rewritten_but_handed_over(self):
        """内容检指不出是哪一句 → 不动稿子，交人工，不整篇重写。

        让模型为一句它自己都说不清的问题重写两百句，只是重新摇一次骰子，
        还会把已经好的句子一起改坏。
        """
        llm = ScriptedLLM(self._payload(),
                          ['{"semantic": {"pass": false, "issues": ["整篇偏题"]},'
                           ' "promise": {"pass": true, "issues": []}}'] * 4)
        logs = []
        gen = S.generate("素材内容示例", self.cfg, llm, log=logs.append)
        self.assertEqual(len(llm.users), 1, "定不了点就不该整篇重出")
        self.assertEqual(llm.patch_calls, 0, "定不了点也不该开定点修补")
        self.assertFalse(gen["report"]["passed"])
        self.assertTrue(any("指不出是哪一句" in m for m in logs), logs)

    def test_unjudged_does_not_block(self):
        """模型没给结论 = 未判定，不该按「有问题」把脚本打回。"""
        llm = ScriptedLLM(self._payload(), ["{}"] * 4)
        gen = S.generate("素材内容示例", self.cfg, llm)
        self.assertTrue(gen["report"]["passed"])
        self.assertEqual(llm.content_calls, 1, "未判定不该触发重写")


class TestParadigmDrivesTheScript(unittest.TestCase):
    """范式卡贯穿到写脚本这一步：两人站位、情绪基调、连续句数上限。

    从前 `script_engine` 一次都没 import 范式——排图按方法论切，写脚本那一段
    仍是「A 一律提问、B 一律解释、严格交替」，同一张卡管不到它。
    """

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}

    def test_card_gives_cast_and_the_forms_run_caps(self):
        card = PG.get("methodology")
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn(card["cast"], p, "没选形式时卡上那段站位照旧出现")
        cap_a, cap_b = PG.run_caps(PG.resolve_form(card, ""))
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % (cap_a, cap_b), p)

    def test_prompt_no_longer_demands_strict_alternation(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        self.assertNotIn("严格交替", p)
        self.assertIn("不许拆给两个人", p)

    def test_run_limit_in_the_prompt_is_the_one_the_gate_uses(self):
        """提示词里的上限与门禁判的是同一处取的值——两处各算一遍迟早对不上。"""
        card = PG.get("interview")
        cap_a, cap_b = PG.run_caps(PG.resolve_form(card, ""))
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % (cap_a, cap_b), p)
        rep = S.gate_generate([{"speaker": "A", "emotion": "解释", "text": "中" * 20},
                               {"speaker": "A", "emotion": "解释", "text": "中" * 20},
                               {"speaker": "A", "emotion": "解释", "text": "中" * 20}],
                              self.CFG, card)
        item = next(i for i in rep["items"] if i["key"] == "ab_run_limit")
        self.assertFalse(item["ok"], "A 连说三句已超过 A 的上限（%d 句）" % cap_a)

    def test_card_without_cast_falls_back_to_the_builtin_hosts(self):
        card = dict(PG.get("auto"), cast="")
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn("负责抛出问题", p)
        self.assertIn("小思", p)

    def test_project_paradigm_wins_over_the_global_default(self):
        self.assertEqual(
            S.resolve_paradigm({"paradigm": "narrative"},
                               {"script.paradigm": "methodology"})["label"],
            PG.get("narrative")["label"])

    def test_global_default_used_when_the_project_says_nothing(self):
        self.assertEqual(
            S.resolve_paradigm({}, {"script.paradigm": "tutorial"})["label"],
            PG.get("tutorial")["label"])

    def test_unknown_card_falls_back_to_auto(self):
        self.assertEqual(
            S.resolve_paradigm({"paradigm": "没有这张卡"}, {})["label"],
            PG.get("auto")["label"])


class TestStyleDims(unittest.TestCase):
    """风格维度与范式卡：体裁、提问频率、比喻密度、互动词四维照卡走。

    （原有第五维「情绪密度」已随 emotion 字段一起删掉，它没有判据对象了。）
    """

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}

    def test_other_four_dims_ignore_the_level(self):
        """只有情绪密度吃档位：体裁、提问、比喻、互动四维两档同值。"""
        p_none = S.build_system_prompt(self.CFG, "science", 900, 24,
                                       paradigm=PG.get("paper"))
        p_light = S.build_system_prompt(self.CFG, "science", 900, 24,
                                        paradigm=PG.get("narrative"))
        for dim in ("- 体裁：", "- 提问频率：", "- 比喻密度：", "- 互动词强度："):
            self.assertEqual([r for r in p_none.splitlines() if r.startswith(dim)],
                             [r for r in p_light.splitlines() if r.startswith(dim)],
                             "%s 不该跟着档位变" % dim)


class TestDiscourseTagRestored(unittest.TestCase):
    """emotion 字段 v0.27.0 起恢复为**语篇标签**（承接/追问/解释/…）：
    生成侧逐句必填、按风格倾向收窄枚举（2b 期实证：逐句枚举是问句率的
    硬信号，提示词软行接不住）；合成侧硬隔离（tts_engine 不读不传）。
    """

    def test_script_schema_carries_emotion(self):
        item = S.script_schema()["properties"]["lines"]["items"]
        self.assertIn("emotion", item["properties"])
        self.assertEqual(item["required"], ["speaker", "emotion", "text"])
        self.assertIn("承接", item["properties"]["emotion"]["enum"])
        self.assertIn("追问", item["properties"]["emotion"]["enum"])

    def test_patch_and_segment_schemas_carry_emotion(self):
        self.assertIn("emotion",
                      S.patch_schema()["properties"]["edits"]["items"]["properties"])
        self.assertIn(
            "emotion",
            S.segment_schema(2000)["properties"]["lines"]["items"]["properties"])
        # 插入的新句 emotion 必填；修补/压紧的 edit emotion 可选。
        self.assertIn("emotion",
                      S.insert_schema(628)["properties"]["inserts"]["items"]["required"])
        self.assertNotIn(
            "emotion",
            S.trim_schema(7)["properties"]["edits"]["items"]["required"])

    def test_templates_are_not_mutated(self):
        """模块级模板是下一次的底稿：调用不许往里写东西。"""
        S.script_schema(); S.patch_schema()
        S.segment_schema(2000); S.insert_schema(628); S.trim_schema(7)
        item = S.SCRIPT_SCHEMA["properties"]["lines"]["items"]
        self.assertEqual(item["properties"]["emotion"]["enum"],
                         list(S.DISCOURSE_ORDER))
        self.assertIn("emotion",
                      S.PATCH_SCHEMA["properties"]["edits"]["items"]["properties"])

    def test_the_discourse_table_has_exactly_one_source(self):
        """语篇词表**只有一份**：提示词、schema 枚举、界面下拉、门禁读的都是它。

        从前有两处收窄——提问频率低去「追问」、情绪密度低去「铺垫/过渡」——两处
        的净效果是同一个：提示词列的词与门禁手里的表对不上。写稿时合法的标签，判
        的时候成了「词表外标签」；而重判那条路只读全局配置，于是同一份稿子一会儿
        过一会儿不过。两个分量连同它们各自的那一刀一起拆掉了。
        """
        from podcast_maker.config_manager import DISCOURSE_ORDER
        self.assertEqual(S.vocab_words(), list(DISCOURSE_ORDER))
        block = S._vocab_block()
        for w in DISCOURSE_ORDER:
            self.assertIn("- %s：" % w, block)
        # 「比喻」也在，且不因为默认档是「少用」就被摘掉——那是把少用当禁用
        for w in ("比喻", "铺垫", "过渡", "追问"):
            self.assertIn(w, S.vocab_words())

    def test_no_style_preset_carries_the_two_dropped_dims(self):
        """风格倾向只剩调子三维，词表与节奏都不归它管。"""
        from podcast_maker.config_manager import PRESET_SPEC, STYLE_DIMS
        self.assertEqual(sorted(STYLE_DIMS), ["genre", "interaction", "metaphor_density"])
        for name, p in PRESET_SPEC.items():
            self.assertNotIn("question_rate", p, name)
            self.assertNotIn("emotion_density", p, name)
        self.assertNotIn("question_rate", S.STYLE_GUIDE)
        for name, p in PRESET_SPEC.items():
            rows = S._style_block(p)
            self.assertNotIn("提问", rows, name)
            self.assertEqual(len(rows.splitlines()), 3, name)

    def test_schema_enum_is_that_same_table(self):
        """schema 枚举就是那一份词表：段落、插入、修补三个 schema 都对齐。"""
        want = S.vocab_words()
        self.assertEqual(
            S.script_schema()["properties"]["lines"]["items"]
            ["properties"]["emotion"]["enum"], want)
        self.assertEqual(
            S.segment_schema(2000)["properties"]["lines"]["items"]
            ["properties"]["emotion"]["enum"], want)
        self.assertEqual(
            S.insert_schema(600)["properties"]["inserts"]["items"]
            ["properties"]["emotion"]["enum"], want)

    def test_the_gate_judges_by_the_same_table(self):
        """门禁判的就是那十个词：论证型项目里的「铺垫」不再被打回。

        这一条正是人报上来的那个错：提示词里给了「铺垫」、门禁说它词表外。
        """
        script = [{"speaker": "A", "emotion": "铺垫", "text": "先埋一笔在前头。"},
                  {"speaker": "B", "emotion": "过渡", "text": "换个话题继续讲下去。"}]
        rep = S.gate_generate(script, {"script.style_preset": "argument"})
        item = [x for x in rep["items"] if x["key"] == "emotion_vocab"][0]
        self.assertTrue(item["ok"], item["detail"])
        self.assertEqual(item["vocab"], S.vocab_words())

    def test_patch_system_carries_emotion_rule(self):
        p = S.patch_system()
        self.assertIn(S.format_banned_rules(), p, "措辞禁忌那一段还在")
        self.assertIn("emotion", p, "修补提示词要有语篇标签的口径")


class TestSpeakerIdentitySurvivesGeneration(unittest.TestCase):
    """生成串联：模型给 A A B B A，出来的稿子不许被掰回交替。

    AABBA 是「主持人开场 + 主持人提问 + 回答两句 + 下一个提问」——真实对话
    本来长这样。硬切成 ABABA 就会出现「一个人的一段话被两个人说」，
    音色、字幕署名、画面名牌跟着一起换。
    """

    def setUp(self):
        # 时长类门禁在别处单独测过，这里只验「说话人不被掰回去」这一条串联。
        self.cfg = ConfigManager().data()
        self.cfg.update({"script.gate_strict": False,
                         "gate.min_deviation_seconds": 9999,
                         "project.program_name": "播客",
                         # 对话形式是界面上可改的一项，测试自己定死：用户在界面上
                         # 选了哪种，不该让这条串联测试替他红一遍。
                         "script.dialogue_form": ""})

    def _payload(self, speakers):
        lines = [{"speaker": s, "emotion": "平静",
                  "text": "第 %d 句正文内容示例文字。" % i}
                 for i, s in enumerate(speakers)]
        return json.dumps({"title": "链与两头", "planned_episodes": 0,
                           "lines": lines}, ensure_ascii=False)

    def _gen(self, speakers, key="methodology"):
        return S.generate("素材内容示例", self.cfg,
                          FakeLLM(self._payload(speakers)),
                          project={"paradigm": key})

    def test_aabba_is_not_flipped_back(self):
        # 开场与提问同属 A（同一人连说两句）、中间两句回答同属 B。
        # 取正文那一段来看：首尾各粘了固定结构，不参与连续句判定。
        speakers = ["A", "A", "B", "B", "A", "B"]
        gen = self._gen(speakers)
        self.assertEqual([s["speaker"] for s in gen["script"][2:-1]], speakers,
                         "同一人连说两句不该被翻面")
        self.assertEqual([s["speaker"] for s in gen["script"][:2]], ["A", "B"],
                         "片头固定 A→B：接缝处最多两句同人")
        self.assertNotIn("flips", gen["report"])

    def test_over_the_limit_is_named_and_the_draft_is_left_alone(self):
        """超限只点名、不动稿：程序不再替模型翻说话人。

        从前这里跑的是后处理（`enforce_max_run`，v0.34.0 取下）：A 的一句问话被翻给
        B，就成了「B 自己问自己」。现在门禁把整段点出来，交模型并句。
        """
        script = [{"speaker": "A", "emotion": "平静",
                   "text": "第 %d 句正文内容示例文字。" % i} for i in range(1, 8)]
        rep = S.gate_generate(script, self.cfg, PG.get("methodology"))
        item = next(i for i in rep["items"] if i["key"] == "ab_run_limit")
        self.assertFalse(item["ok"], item["detail"])
        self.assertEqual(item["runs"][0]["lines"], list(range(1, 8)),
                         "整段都要点出来，并句才有得并")
        self.assertEqual([s["speaker"] for s in script], ["A"] * 7, "门禁不改稿")

    def test_the_same_script_is_judged_by_the_same_form(self):
        """形式换了，判据跟着换——被访者连说四句在追问深挖下合法，闲聊漫谈下就超了。"""
        script = [{"speaker": s, "emotion": "平静", "text": "中" * 20}
                  for s in ["A", "B", "B", "B", "B", "A"]]
        ok = S.gate_generate(script, self.cfg, PG.get("interview"))
        self.assertTrue(next(i for i in ok["items"]
                             if i["key"] == "ab_run_limit")["ok"],
                        "追问深挖里 B 连说四句是常态（上限 5）")
        self.cfg["script.dialogue_form"] = "chat"      # A/B 各 3 句
        bad = S.gate_generate(script, self.cfg, PG.get("interview"))
        item = next(i for i in bad["items"] if i["key"] == "ab_run_limit")
        self.assertFalse(item["ok"], item["detail"])
        self.assertEqual(item["runs"][0]["lines"], [2, 3, 4, 5],
                         "整段点出来（第 2–5 句都是 B），供并句用")


class TestPatchContract(unittest.TestCase):
    """定点补丁的契约：只换句子，不改行数，不碰没点名的句。

    这条路的价值全在这三条上。行数一变，门禁报的「第 N 句」就跟人对不上了；
    顺手改没点名的句子，等于把没毛病的地方重新摇一次骰子——那正是从前整篇
    重出最贵的地方。所以它们都是硬约束，越界就报错，不静默丢弃。
    """

    def test_schema_has_no_way_to_add_lines_or_to_swap_speakers(self):
        """「不许凭空增删、不许换人」不靠提示词劝，靠结构里写不出来。

        能减行数的只有 `absorb`（并句），且只给「连着说超限」用；说话人不在 schema
        里——换人是拿格式代替语义（A 的一句问话翻给 B，就成了 B 自己问自己）。
        """
        item = S.PATCH_SCHEMA["properties"]["edits"]["items"]["properties"]
        self.assertEqual(set(item), {"index", "emotion", "absorb", "text"})
        self.assertNotIn("speaker", item, "说话人不归模型管；超限靠并句，不靠换人")

    def test_apply_patch_replaces_in_place(self):
        script = S.normalize_script(_lines(6), _cfg())
        out = S.apply_patch(script, [{"index": 3, "text": "换过之后的一句新台词文本"}], {3})
        self.assertEqual(len(out), len(script), "行数一个字都不能变")
        self.assertEqual(out[2]["text"], "换过之后的一句新台词文本")
        self.assertEqual(out[1]["text"], script[1]["text"], "没点名的句子照抄")
        self.assertEqual([s["speaker"] for s in out], [s["speaker"] for s in script])

    def test_apply_patch_ignores_the_speaker_the_model_echoes(self):
        script = S.normalize_script(_lines(4), _cfg())
        now = script[1]["speaker"]
        out = S.apply_patch(script, [{"index": 2, "speaker": "B" if now == "A" else "A",
                                      "text": "换过之后的一句新台词文本"}], {2})
        self.assertEqual(out[1]["speaker"], now, "说话人不采纳模型给的")

    def test_same_line_twice_is_a_split_in_disguise(self):
        script = S.normalize_script(_lines(6), _cfg())
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 3, "text": "改过一次的台词文本"},
                                   {"index": 3, "text": "又改一次的台词文本"}], {3})
        self.assertIn("两次", str(ctx.exception))

    def test_line_out_of_range_raises(self):
        script = S.normalize_script(_lines(4), _cfg())
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 9, "text": "改过一次的台词文本"}], {9})
        self.assertIn("越界", str(ctx.exception))

    def test_touching_an_unflagged_line_raises(self):
        script = S.normalize_script(_lines(6), _cfg())
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 5, "text": "改过一次的台词文本"}], {3})
        self.assertIn("没被点名", str(ctx.exception))

    def test_blank_text_raises(self):
        script = S.normalize_script(_lines(4), _cfg())
        with self.assertRaises(ScriptError):
            S.apply_patch(script, [{"index": 2, "text": "   "}], {2})

    def _same_speaker(self, n=3, who="A"):
        return S.normalize_script(
            [{"speaker": who, "emotion": "承接",
              "text": "第 %d 句台词内容够长够取标题。" % i} for i in range(1, n + 1)],
            _cfg())

    def test_absorb_merges_the_run_into_one_line(self):
        """并句是减行数的唯一表达：一句替掉紧随其后的几句，说话人一个字不动。"""
        script = self._same_speaker(3)
        merged = ("本地推理最大的好处是数据不出门，合同病历这类东西"
                  "发出去就等于复制到别人的机房里。")
        out = S.apply_patch(script,
                            [{"index": 1, "text": merged,
                              "absorb": 2}], {1, 2, 3})
        self.assertEqual(len(out), 1, "三句并成一句")
        self.assertEqual(out[0]["text"], merged)
        self.assertEqual(out[0]["speaker"], "A", "并句不该换人")

    def test_absorb_cannot_swallow_a_line_nobody_flagged(self):
        script = self._same_speaker(3)
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 1, "text": "并好之后的一整句台词文本",
                                    "absorb": 1}], {1})
        self.assertIn("没点名", str(ctx.exception))

    def test_absorb_cannot_merge_across_speakers(self):
        """跨人并就是把一个人的话塞进另一个人嘴里。"""
        script = S.normalize_script(
            [{"speaker": "A", "emotion": "承接", "text": "第 1 句台词内容够长够取标题。"},
             {"speaker": "B", "emotion": "承接", "text": "第 2 句台词内容够长够取标题。"}],
            _cfg())
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 1, "text": "并好之后的一整句台词文本",
                                    "absorb": 1}], {1, 2})
        self.assertIn("同一个人", str(ctx.exception))

    def test_absorb_cannot_eat_the_content(self):
        """并句只许删掉合并处的重复衔接，不许借并句把内容吃掉。"""
        script = self._same_speaker(2)
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 1, "text": "太短", "absorb": 1}], {1, 2})
        self.assertIn("意思一个都不许少", str(ctx.exception))

    def test_parse_patch_rejects_empty_and_garbage(self):
        for bad in ('{"edits": []}', "这次我直接给你重写一遍：好，那我们开始。", "{}"):
            with self.assertRaises(ScriptError, msg=bad):
                S.parse_patch(bad)

    def test_parse_patch_accepts_a_code_fence(self):
        raw = '```json\n{"edits": [{"index": 2, "text": "改过之后的一句台词"}]}\n```'
        self.assertEqual(len(S.parse_patch(raw)), 1)


class TestPatchTargets(unittest.TestCase):
    """哪些问题能定点、哪些只能重出、哪些该交人工——分成三类，各有各的去处。"""

    def _form_report(self):
        script = S.normalize_script(_lines(6), _cfg())
        return script, S.gate_generate(script, _cfg(),
                                       S.resolve_paradigm(None, _cfg()))

    def test_short_and_long_lines_come_back_with_line_numbers(self):
        cfg = _cfg()
        script = S.normalize_script(_lines(6), cfg)
        script[2]["text"] = "短"
        script[4]["text"] = "长" * 44
        report = S.gate_generate(script, cfg, S.resolve_paradigm(None, cfg))
        targets, refull, manual = S.patch_targets(report, len(script))
        self.assertEqual(sorted(targets), [3, 5])
        self.assertFalse(refull and manual)
        self.assertTrue(any("不许拆句" in w for w in targets[5]))

    def test_banned_word_comes_back_with_the_substitute(self):
        cfg = _cfg()
        script = S.normalize_script(_lines(6), cfg)
        script[1]["text"] = "所有" + script[1]["text"]
        report = S.gate_generate(script, cfg, S.resolve_paradigm(None, cfg))
        targets, _refull, _manual = S.patch_targets(report, len(script))
        self.assertEqual(sorted(targets), [2])
        self.assertTrue(any("所有" in w for w in targets[2]))

    def test_located_content_issue_is_patchable(self):
        report = {"items": [{"key": "check_semantic", "label": "语义检（LLM）",
                             "ok": False, "detail": "第 7 句：编造",
                             "issues": [{"line": 7, "quote": "原话", "problem": "编了数据"}]}]}
        targets, refull, manual = S.patch_targets(report, 20)
        self.assertEqual(sorted(targets), [7])
        self.assertFalse(refull or manual)
        # 方向要给全：只贴检查结论的话，模型会当成报告而不是指令。
        self.assertTrue(any("去掉素材里没有" in w for w in targets[7]),
                        targets[7])

    def test_unlocated_content_issue_goes_to_a_human(self):
        report = {"items": [{"key": "check_semantic", "label": "语义检（LLM）",
                             "ok": False, "detail": "未指出句号：整篇偏题",
                             "issues": [{"line": 0, "quote": "", "problem": "整篇偏题"}]}]}
        targets, refull, manual = S.patch_targets(report, 20)
        self.assertFalse(targets, "没有落点就不该交给模型")
        self.assertFalse(refull, "也不该整篇重出")
        self.assertEqual(len(manual), 1, "该交人工")

    def test_broken_structure_can_only_be_regenerated(self):
        report = {"items": [{"key": "json_valid", "label": "JSON 合法",
                             "ok": False, "detail": "共 0 句"}]}
        targets, refull, manual = S.patch_targets(report, 0)
        self.assertFalse(targets)
        self.assertEqual(len(refull), 1)
        self.assertFalse(manual)

    def test_soft_and_advisory_items_are_left_alone(self):
        report = {"items": [
            {"key": "total_duration", "label": "总时长偏差", "ok": False, "soft": True},
            {"key": "check_promise", "label": "承诺链检（LLM）", "ok": False,
             "advisory": True}]}
        targets, refull, manual = S.patch_targets(report, 20)
        self.assertFalse(targets or refull or manual)


class TestRecheckScope(unittest.TestCase):
    """贵的检查（走模型）按需跑，便宜的（走代码）每轮全跑。"""

    def _report(self, sem_ok, promise_ok):
        return {"items": [
            {"key": "check_semantic", "ok": sem_ok},
            {"key": "check_promise", "ok": promise_ok}]}

    def test_never_checked_means_check_everything(self):
        self.assertEqual(S.dims_to_recheck(None), {"semantic", "promise"})
        self.assertEqual(S.dims_to_recheck({"items": []}), {"semantic", "promise"})

    def test_failed_dim_is_rejudged_others_are_carried(self):
        self.assertEqual(S.dims_to_recheck(self._report(False, True)), {"semantic"})
        self.assertEqual(S.dims_to_recheck(self._report(True, True)), set())
        self.assertEqual(S.dims_to_recheck(self._report(False, False)),
                         {"semantic", "promise"})

    def test_merge_keeps_the_dim_that_was_not_rejudged(self):
        report = {"items": [{"key": "json_valid", "ok": True}]}
        carry = self._report(False, True)
        content = {"items": [{"key": "check_semantic", "ok": True}]}
        merged = S.merge_check(report, content, _cfg(), carry=carry)
        keys = [i["key"] for i in merged["items"]]
        self.assertIn("check_promise", keys, "没重判的那一项要跟过来")
        self.assertTrue(merged["content_checked"])


class TestCheck6OutputShape(unittest.TestCase):
    """内容检的输出必须带落点——这是定点修补的前置条件。"""

    def test_prompt_demands_line_quote_and_problem(self):
        for token in ("line", "quote", "problem"):
            self.assertIn(token, S.CHECK6_SYSTEM)
        self.assertIn("填 0", S.CHECK6_SYSTEM,
                      "指不出哪一句时要明说填 0，不许拿别的句子顶替")

    def test_structured_issues_survive(self):
        payload = ('{"semantic": {"pass": false, "issues": ['
                   '{"line": 4, "quote": "原话片段", "problem": "编了数据"}]},'
                   ' "promise": {"pass": true, "issues": []}}')
        out = S.check6_llm(_lines(8), _cfg(), _LLM(payload), "素材")
        state, detail, issues = out["semantic"]
        self.assertEqual(state, "fail")
        self.assertEqual(issues, [{"line": 4, "quote": "原话片段",
                                   "problem": "编了数据"}])
        self.assertIn("第 4 句", detail)

    def test_out_of_range_line_becomes_zero_not_a_guess(self):
        """句号越界一律归 0：拿假句号去定点，改的是没毛病的那句。"""
        payload = ('{"semantic": {"pass": false, "issues": ['
                   '{"line": 99, "quote": "", "problem": "说不清"}]},'
                   ' "promise": {"pass": true, "issues": []}}')
        out = S.check6_llm(_lines(4), _cfg(), _LLM(payload), "素材")
        self.assertEqual(out["semantic"][2][0]["line"], 0)

    def test_legacy_plain_string_issue_is_unlocatable(self):
        payload = ('{"semantic": {"pass": false, "issues": ["第3句编了数据"]},'
                   ' "promise": {"pass": true, "issues": []}}')
        out = S.check6_llm(_lines(4), _cfg(), _LLM(payload), "素材")
        self.assertEqual(out["semantic"][2][0]["line"], 0)
        self.assertIn("第3句编了数据", out["semantic"][2][0]["problem"])

    def test_dims_limits_what_is_judged(self):
        payload = '{"semantic": {"pass": true, "issues": []}}'
        out = S.check6_llm(_lines(4), _cfg(), _LLM(payload), "素材",
                           dims=("semantic",))
        self.assertEqual(sorted(out), ["semantic"])


class TestPatchPromptShape(unittest.TestCase):
    def test_window_only_lists_nearby_lines(self):
        script = S.normalize_script(_lines(40), _cfg())
        user = S.build_patch_prompt(script, {20: ["过长"]}, "第 20 句：太长", window=2)
        self.assertIn("18. [", user, "要改的那一句往前数两句在内")
        self.assertIn("22. [", user, "往后数两句也在内")
        self.assertNotIn("30. [", user, "远处的句子不该递给模型")

    def test_material_only_rides_along_when_needed(self):
        script = S.normalize_script(_lines(10), _cfg())
        without = S.build_patch_prompt(script, {5: ["过长"]}, "第 5 句：太长")
        with_mat = S.build_patch_prompt(script, {5: ["编造"]}, "第 5 句：编造",
                                        material="素材正文")
        self.assertNotIn("素材正文", without)
        self.assertIn("素材正文", with_mat)

    def test_needs_material_reads_the_report(self):
        self.assertTrue(S._needs_material(
            {"items": [{"key": "check_semantic", "ok": False}]}))
        self.assertFalse(S._needs_material(
            {"items": [{"key": "banned_words", "ok": False}]}))
        self.assertFalse(S._needs_material(
            {"items": [{"key": "total_duration", "ok": False, "soft": True}]}))

    def test_system_prompt_forbids_adding_and_removing(self):
        self.assertIn("行数一个字都不能变", S.patch_system())
        self.assertIn("不要输出 speaker", S.patch_system())

    def test_system_prompt_carries_the_banned_words(self):
        """定点修补也要知道措辞禁忌。

        实测里模型把一句补长，自己写进了「毫无」，回门禁当场被拦——修改这一端
        此前只说「改这几句」，没说「改出来的也得守词表」。词表不另写一份，直接
        用写作那一侧的同一个渲染结果：两处各存一份，走岔了只有出事那天才知道。
        """
        self.assertIn(S.format_banned_rules(), S.patch_system())


class TestDraftSink(unittest.TestCase):
    """每轮写完就落盘：后面哪一轮卡住或被中止，手上都还有一份完整稿子。"""

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg.update({"script.gate_strict": False,
                         "gate.min_deviation_seconds": 9999,
                         "project.program_name": "播客",
                         "script.max_llm_rounds": 2})

    def _payload(self):
        lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平静",
                  "text": "第 %d 句正文内容示例文字。" % i} for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": 7,
                           "lines": lines}, ensure_ascii=False)

    def test_sink_is_called_after_every_round(self):
        """每轮写完都有一份落盘：中途卡住或被中止，手上还有稿子。

        落的是**过程稿**（裸正文）。最后一轮的裸落由定稿那一次顶替——定稿同样
        往同一个位置写，同一份文件写两遍是白写（见 `_finish`）。所以落盘次数
        等于轮数，不是轮数加一。
        """
        seen = []
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容。"}]}'] * 8)
        S.generate("素材内容示例", self.cfg, llm, draft_sink=seen.append)
        self.assertEqual(len(seen), len(llm.users) + llm.patch_calls,
                         "每开一轮就该落一次盘")
        self.assertTrue(all("script" in g and "report" in g for g in seen))
        self.assertEqual([g["attempt"] for g in seen], [1, 2, 3])

    def test_glue_runs_once_per_episode(self):
        """整期只粘一次：几轮改下来，片头尾只在定稿那一刻挂上去。

        片头尾不进轮——不参与生成、不过门禁、不被定点修补。整期没定稿之前，
        每一轮落的盘都是「写到这儿的正文」：给它粘上片头尾，形态像成品、其实
        还是半成品；轮次一多，日志上就跟着冒一串「片头尾已粘上」，读的人分不清
        是粘重了还是落了几次盘。粘合只发生在定稿（见 `_finish`）。
        """
        seen, logs = [], []
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容。"}]}'] * 8)
        S.generate("素材内容示例", self.cfg, llm,
                   draft_sink=seen.append, log=logs.append)
        glued = [m for m in logs if "片头尾已粘上" in m]
        self.assertGreater(len(seen), 1, "这一段确实跑了好几轮")
        self.assertEqual(len(glued), 1, "粘合全篇只该有一次：定稿那一刻")

    def test_intermediate_drafts_carry_no_fixed_lines(self):
        """中间轮落的是裸正文：不带片头，也不带片尾。

        中间稿挂上片尾，读的人会以为后半段已经写完了；挂上片头，还会让下一轮
        定点修补的句号整体后移两位。片头尾只在定稿那一份里（见 `_finish`）。
        """
        seen = []
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容。"}]}'] * 8)
        gen = S.generate("素材内容示例", self.cfg, llm, draft_sink=seen.append)
        for i, g in enumerate(seen[:-1], 1):
            texts = [l["text"] for l in g["script"]]
            with self.subTest(draft=i):
                self.assertFalse(any("欢迎收听" in t for t in texts),
                                 "第 %d 轮的过程稿不该带片头" % i)
                self.assertFalse(any("欢迎关注" in t for t in texts),
                                 "第 %d 轮的过程稿不该带片尾" % i)
        final = [l["text"] for l in seen[-1]["script"]]
        self.assertIn("欢迎收听", final[0], "定稿那一份带片头")
        self.assertIn("欢迎关注", final[-1], "定稿那一份带片尾")
        self.assertEqual([l["text"] for l in gen["script"]], final,
                         "返回的就是落盘的那份定稿")

    def test_the_returned_script_is_the_sunk_one(self):
        """返回的就是刚落盘的那一份（同一个对象），不是又粘一遍的复制品。

        定稿只粘一次、只落一次，落盘与返回共用同一个 dict——不然同一份正文被
        粘两遍、写两遍，日志上也看不出哪一行对应哪一次写盘。
        """
        seen = []
        llm = ScriptedLLM(self._payload(), [SEM_PASS] * 2)
        gen = S.generate("素材内容示例", self.cfg, llm, draft_sink=seen.append)
        self.assertIs(gen, seen[-1])
        self.assertEqual([l["text"] for l in gen["script"]],
                         [l["text"] for l in seen[-1]["script"]])

    def test_sink_absent_is_fine(self):
        llm = ScriptedLLM(self._payload(), [SEM_PASS] * 2)
        gen = S.generate("素材内容示例", self.cfg, llm)
        self.assertTrue(gen["report"]["passed"])

    def test_pinned_intro_seconds_are_refreshed(self):
        """片头尾写死之后，单句时长字段要跟着刷新。

        整篇轮里 normalize 跑在 pin 之前，pin 一改文本，那个字段就旧了；定点轮是
        「改完定型再 normalize」，字段是新的。两条路顺序不齐，同一期稿子落盘的首末
        句秒数会因走哪条路而不同。这里盯首句——它的文本一定是被模板改过的。
        """
        llm = ScriptedLLM(self._payload(), [SEM_PASS] * 2)
        seen = []
        S.generate("素材内容示例", self.cfg, llm, draft_sink=seen.append)
        first = seen[0]["script"][0]
        self.assertEqual(
            first["estimated_seconds"],
            int(round(S.duration_model.estimate_line(
                first["text"], first["speaker"], self.cfg))),
            "首句时长要按写死之后的文本算")


class TestContentCheckSchema(unittest.TestCase):
    """内容检的输出契约：落点三件套靠结构保证，不能靠劝。

    它是七条提示词里唯一要求复杂结构的一条（第几句 / 原话 / 什么问题），而定点
    修补完全依赖它给出句号——模型不给句号，那一轮就定不了点，只能交人工。这里
    盯三件事：契约里有落点字段、不判的项不进契约、调用时真的把契约递了出去。
    """

    def test_schema_carries_location_fields(self):
        node = S.check6_schema()
        self.assertEqual(sorted(node["required"]), ["promise", "semantic"])
        item = node["properties"]["semantic"]["properties"]["issues"]["items"]
        self.assertIn("line", item["required"], "句号必须是必填项")
        self.assertIn("problem", item["required"])

    def test_schema_narrows_with_dims(self):
        one = S.check6_schema(["promise"])
        self.assertEqual(one["required"], ["promise"])
        self.assertNotIn("semantic", one["properties"],
                         "不判的项不该出现在契约里：约束解码会逼模型给它下结论")

    def test_schema_is_passed_to_the_call(self):
        llm = _LLM(SEM_PASS)
        S.check6_llm([{"speaker": "A", "text": "第 1 句正文内容示例文字",
                       "emotion": "平静"}], _cfg(), llm, "素材")
        self.assertIsNotNone(llm.schemas[-1], "内容检必须带约束解码")


class TestPromptBlocks(unittest.TestCase):
    """提示词分块：每一块自报「这是什么、怎么用」。

    一条提示词里塞着好几种东西——素材、上一版正文、当前任务、输出契约。块名
    统一只是表面；要紧的是**模型一眼能分出哪块是依据、哪块是当前该产出的东西**。
    这里盯块名在场，防的是后来人图省事把它们抹平回去。
    """

    def setUp(self):
        self.cfg = _cfg()

    def test_script_system_prompt_blocks(self):
        txt = S.build_system_prompt(self.cfg, "argument", 3000, 200)
        for block in ("【片头片尾】", "【输出格式】", "【当前任务：生成要求】",
                      "【风格倾向】", "【两位主持人】"):
            self.assertIn(block, txt, block)

    def test_script_user_prompt_blocks(self):
        user = S.build_user_prompt("素材正文示例", self.cfg, None, None)
        self.assertIn("【素材】", user)

    def test_script_user_prompt_rewrite_blocks(self):
        user = S.build_user_prompt("素材正文示例", self.cfg,
                                   "第 12 句过长", "12. [A] 够长的例句文本")
        self.assertIn("【上一版正文】", user)
        self.assertIn("【要改的地方】", user)

    def test_check6_user_prompt_blocks(self):
        llm = _LLM(SEM_PASS)
        S.check6_llm([{"speaker": "A", "text": "第 1 句正文内容示例文字",
                       "emotion": "平静"}], self.cfg, llm, "素材正文示例")
        user = llm.users[-1]
        # 块名要说清这块是**哪一类判据**：事实判据看素材，方向判据看计划与凝缩。
        self.assertIn("【素材节选】", user)
        self.assertIn("【待审脚本】", user)

    def test_check6_carries_the_evidence_pack(self):
        """判据包要真的进提示词：光在参数表上有这个名，等于没接上。"""
        llm = _LLM(SEM_PASS)
        S.check6_llm([{"speaker": "A", "text": "第 1 句正文内容示例文字",
                       "emotion": "平静"}], self.cfg, llm, "素材正文示例",
                     evidence={"gist": "本期讲链与两头",
                               "points": ["先立链", "再收口"],
                               "sections": [{"anchor": "第一节", "gist": "讲了起点"}]})
        user = llm.users[-1]
        self.assertIn("【本期计划】", user)
        self.assertIn("【各节凝缩】", user)
        self.assertIn("本期讲链与两头", user)

    def test_patch_user_prompt_blocks(self):
        script = S.normalize_script(_lines(20), self.cfg)
        user = S.build_patch_prompt(
            script, {12: ["过长"]}, S._patch_feedback({12: ["过长"]}),
            material="素材正文示例")
        for block in ("【素材】", "【要改的地方】", "【相关段落】"):
            self.assertIn(block, user, block)


class _LLM(object):
    """只回一句固定 payload 的假模型（内容检用）；顺带记下递过来的契约。"""

    def __init__(self, payload):
        self.payload = payload
        self.users = []
        self.schemas = []

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        self.users.append(messages[-1].get("content") or "")
        self.schemas.append(json_schema)
        return self.payload, {}


def _cfg(**over):
    cfg = ConfigManager().data()
    cfg.update({"script.gate_strict": False,
                "gate.min_deviation_seconds": 9999,
                "project.program_name": "播客"})
    cfg.update(over)
    return cfg


class _BudgetScriptedLLM(ScriptedLLM):
    """带输入预算的假模型，用来把「分批核对」这条路逼出来。

    `budget_chars` 固定返回 `size`，不按模板算：这里要盯的是**切分与合并的
    规则**，预算公式另有自己的用例（`test_llm_client.TestInputBudget`）。
    真客户端的 `budget_chars` 会自己扣 `other_chars`，这里由用例直接把最终
    每批字数钉死，免得用例去复刻那段算术。
    """

    def __init__(self, content_replies, size):
        super().__init__("{}", content_replies)
        self.size = size

    def budget_chars(self, max_tokens, other_chars=0):
        return int(self.size)


def _sem_at(*lines):
    """一段语义检结论：点名这几句。"""
    items = ", ".join('{"line": %d, "quote": "q", "problem": "p"}' % ln
                      for ln in lines)
    return ('{"semantic": {"pass": %s, "issues": [%s]},'
            ' "promise": {"pass": true, "issues": []}}'
            % ("false" if lines else "true", items))


class TestBatchCheck(unittest.TestCase):
    """内容检分批：素材放不下时不能靠砍，要靠分几批各核一遍再合并。

    盯三件事：切分按预算发生、每批都带整篇脚本、**问题只有在每一批里都被
    报出来才算数**。最后一条是分批能成立的全部理由——某句的依据落在另一批
    的素材里，这一批当然找不到它，撤销它才是对的。
    """

    def setUp(self):
        self.cfg = _cfg()

    def test_material_is_split_by_budget(self):
        """每批该装多少，问的是**这一次调用的输入额度**，不是画地图那把尺。

        从前这里算的是「素材容量（成稿目标 × 档位 × 1.25，朗读字数）− 字符数
        占位」——两把尺相减，减出来的值没有意义。现在这一批能装多少由
        `budget_chars_for_check` 折 token 给出（画地图的数字一个都不进这道判断）。
        """
        llm = _BudgetScriptedLLM([SEM_PASS] * 8, size=2000)
        script = S.normalize_script(_lines(12), self.cfg)
        res = S.check6_llm(script, self.cfg, llm, "素" * 5000)
        # 5000 字按 2000 一批切：批间重叠取 min(1000, 2000/4)=500，步子 1500，
        # 于是切出 0/1500/3000 三段，共 3 批。
        self.assertEqual(llm.content_calls, 3)
        self.assertIn("第 2/3 批", llm.content_users[1])
        self.assertIn("1. [A]", llm.content_users[1], "每批都要带整篇脚本")
        self.assertEqual(res["semantic"][0], "pass")

    def test_issue_survives_only_when_every_batch_reports_it(self):
        llm = _BudgetScriptedLLM([_sem_at(3, 7), _sem_at(3), _sem_at(3)],
                                 size=2000)
        script = S.normalize_script(_lines(12), self.cfg)
        res = S.check6_llm(script, self.cfg, llm, "素" * 5000)
        state, detail, issues = res["semantic"]
        self.assertEqual([it["line"] for it in issues], [3],
                         "第 7 句只有一批报出，说明依据在别批里，应撤销")
        self.assertEqual(state, "fail")
        self.assertIn("已撤销", detail, "撤销要留痕，否则问题变少的来由不明")

    def test_no_budget_at_all_is_reported_not_swallowed(self):
        """额度连模板都装不下时说「未核」，不能把整段素材硬塞进去。"""
        llm = _BudgetScriptedLLM([SEM_PASS] * 4, size=0)
        script = S.normalize_script(_lines(12), self.cfg)
        res = S.check6_llm(script, self.cfg, llm, "素" * 5000)
        self.assertEqual(llm.content_calls, 0, "额度为 0 时不该开调用")
        self.assertEqual(res["semantic"][0], "pending")
        self.assertIn("输入倍率", res["semantic"][1],
                      "话要说清是哪个旋钮不够，别报成「素材容量」")


class _SegmentedLLM(object):
    """分段生成路径的假模型：规划 / 分段 / 补字插入 / 压字删减 / 定点修补 / 内容检。

    分段开启时**不该**再走整篇生成——script_calls 就是盯这件事的。

    「补字」与「压字」两条路没有现成 payload 时现场造：
      - 少了 → 按提示词里的「少了 N 字」造插入项，每句顶到句长上限，插在本段末尾；
      - 多了 → 按「多了 N 字」把最长的几句压到句长下限。
    这样测试不必预先算准会插几句、压哪几句（那是配额算术的事，另有测试盯着），
    只管这两条路走不走得通。

    定点修补那一路归门禁阶段用（改已有句子，行数不变），与上面两条路不是一回事。
    """

    def __init__(self, plan_payloads, segment_payloads, content_replies=None,
                 script_payloads=None, patch_payloads=None, insert_payloads=None,
                 trim_payloads=None, logic_payloads=None):
        self.plan_payloads = list(plan_payloads)
        self.segment_payloads = list(segment_payloads)
        self.content_replies = list(content_replies or [])
        self.script_payloads = list(script_payloads or [])
        self.patch_payloads = list(patch_payloads or [])
        self.insert_payloads = list(insert_payloads or [])
        self.trim_payloads = list(trim_payloads or [])
        # 逻辑拆分的回答。不给就自动回「一节一段」——等价于不合并，段数只由节数
        # 定，绝大多数测试想要的正是这个稳定形态。要验合并的场景显式给。
        self.logic_payloads = list(logic_payloads or [])
        self.plan_calls = 0
        self.segment_calls = 0
        self.script_calls = 0
        self.content_calls = 0
        self.patch_calls = 0
        self.insert_calls = 0
        self.trim_calls = 0
        self.logic_calls = 0
        self.segment_users = []
        self.patch_users = []
        self.insert_users = []
        self.trim_users = []
        self.logic_users = []
        self.budgets = []

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        self.budgets.append(int(max_tokens))
        system = messages[0].get("content") or ""
        if "分段者" in system:
            self.logic_calls += 1
            user = messages[-1].get("content") or ""
            self.logic_users.append(user)
            if self.logic_payloads:
                return self.logic_payloads.pop(0), {}
            return self._auto_logic(user), {}
        if "规划者" in system:
            self.plan_calls += 1
            return self.plan_payloads.pop(0), {}
        if "分段写作" in system:
            self.segment_calls += 1
            self.segment_users.append(messages[-1].get("content") or "")
            if self.segment_payloads:
                return self.segment_payloads.pop(0), {}
            raise AssertionError("分段调用超出脚本给出的段数")
        if "补写者" in system:
            self.insert_calls += 1
            user = messages[-1].get("content") or ""
            self.insert_users.append(user)
            if self.insert_payloads:
                return self.insert_payloads.pop(0), {}
            return self._auto_insert(user), {}
        if "压缩者" in system:
            self.trim_calls += 1
            user = messages[-1].get("content") or ""
            self.trim_users.append(user)
            if self.trim_payloads:
                return self.trim_payloads.pop(0), {}
            return self._auto_trim(user), {}
        if "定点修补" in system:
            self.patch_calls += 1
            user = messages[-1].get("content") or ""
            self.patch_users.append(user)
            if self.patch_payloads:
                return self.patch_payloads.pop(0), {}
            return self._auto_patch(user), {}
        if "审校" in system:
            self.content_calls += 1
            if self.content_replies:
                return self.content_replies.pop(0), {}
            return ('{"semantic": {"pass": true, "issues": []}, '
                    '"promise": {"pass": true, "issues": []}}'), {}
        self.script_calls += 1
        if self.script_payloads:
            return self.script_payloads.pop(0), {}
        raise AssertionError("整篇生成分支没有脚本可回")

    @staticmethod
    def _auto_logic(user):
        """默认分组：**一节一段**（等价于不合并）。

        段数只由节数定，不随额度浮动——绝大多数测试要的就是这个稳定形态。
        要验「这几节合成一段」的场景，显式给 `logic_payloads`。
        """
        ns = [int(k) for k in re.findall(r"^第 (\d+) 节（", user, re.M)]
        return json.dumps({"groups": [[k] for k in ns]}, ensure_ascii=False)

    @staticmethod
    def _auto_insert(user):
        """按「还差 N 字」造一份插入：每句顶到句长上限，插在本段末尾。"""
        m = re.search(r"还差 (\d+) 字", user)
        tail = re.search(r"插在最后填 (\d+)", user)
        if not m or not tail:
            raise AssertionError("补字提示词里没有缺口字数或末尾句号")
        need, last = int(m.group(1)), int(tail.group(1))
        rows, total, k = [], 0, 0
        while total < need:
            rows.append({"after": last, "speaker": "A" if k % 2 == 0 else "B",
                         "text": "补" * 39 + "。", "emotion": "承接"})
            total += 40
            k += 1
        return json.dumps({"inserts": rows}, ensure_ascii=False)

    @staticmethod
    def _auto_trim(user):
        """按「多了 N 字」把最长的几句压到句长下限。"""
        m = re.search(r"多了 (\d+) 字", user)
        if not m:
            raise AssertionError("压字提示词里没有超出字数")
        need = int(m.group(1))
        rows = re.findall(r"^(\d+)\. \[([AB])(?:·[^\]]*)?\] (.+)$", user, re.M)
        if not rows:
            raise AssertionError("压字提示词里没有正文")
        edits, left = [], need
        for idx, _sp, text in sorted(rows, key=lambda r: -len(r[2])):
            if left <= 0:
                break
            cut = min(len(text) - 8, left)
            if cut <= 0:
                continue
            # 压紧不许压掉句尾标点：终止标点保留在尾上，从正文里扣字。
            tail = text[-1] if text[-1] in "。！？…" else ""
            body = text[:len(text) - cut - len(tail)]
            edits.append({"index": int(idx), "text": body + tail})
            left -= cut
        if not edits:
            raise AssertionError("压字提示词里没有可压的句子")
        return json.dumps({"edits": edits}, ensure_ascii=False)

    @staticmethod
    def _auto_patch(user):
        """按提示词里点名的句子造一份合法补丁（只替换，不增删行）。

        门禁那条路给的措辞是「第 N 句：怎么改」，方向各异（过长 / 过短 / 禁用词 /
        朗读性 / 情绪标签 / 内容不实），所以正则只认句号、不认方向：假模型不去猜
        该改成什么样，统一给一句合规长度的文本，跑得通就说明这条路走得通。
        """
        idxs = sorted({int(n) for n in re.findall(r"第 (\d+) 句：", user)})
        if not idxs:
            raise AssertionError("定点修补的提示词里没点名任何句子")
        return json.dumps({"edits": [{"index": n, "text": "乙" * 19 + "。"} for n in idxs]},
                          ensure_ascii=False)


def _evidence():
    return {"gist": "总主旨", "points": ["要点一"],
            "sections": [
                {"source": "s1", "anchor": "甲节", "gist": "甲节主旨",
                 "points": ["甲点一", "甲点二"], "chars": 1000},
                {"source": "s2", "anchor": "乙节", "gist": "乙节主旨",
                 "points": ["乙点一"], "chars": 700},
            ]}


def _plan_payload(title="测试标题", n=2):
    """规划轮的假回答：只有「标题 + 每段一句话题目」，段边界一个字都不给。

    段边界归逻辑拆分（分组）与装箱（按容量切），规划轮碰不到；所以条数必须与
    最终段数一致——少了多了都要打回（schema 里 minItems==maxItems 钉死）。
    造几段就写几条题目，`n` 要跟装箱结果对上。
    """
    return json.dumps({"title": title,
                       "topics": ["第 %d 段讲什么" % (i + 1) for i in range(n)]},
                      ensure_ascii=False)


def _seg_payload(n):
    lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "承接",
              "text": "甲" * 30 + "。"} for i in range(n)]
    return json.dumps({"lines": lines}, ensure_ascii=False)


def _whole_script_payload(n=6):
    lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "承接",
              "text": "这一句台词正好二十个字左右的内容。"} for i in range(n)]
    return json.dumps({"title": "整篇标题", "planned_episodes": 0,
                       "lines": lines}, ensure_ascii=False)


class TestSegmentQuotas(unittest.TestCase):
    """字数配额：账本单位只有字——句数只是形态引导，永不进核账。"""

    def test_schemas_carry_hard_caps(self):
        """maxItems 是失控刹车：约束解码实测能把数组无限写下去（单次 6300+
        token 不停）。段数、单段句数都必须有语法层封顶。

        规划轮那一头的数组长度不是"刹车"而是**钉死**：段数由装箱给，模型既不能
        增也不能删，所以 minItems == maxItems == 段数。
        """
        plan = S._segment_plan_schema(4)
        self.assertEqual(plan["properties"]["topics"]["maxItems"], 4)
        self.assertEqual(plan["properties"]["topics"]["minItems"], 4)
        self.assertNotIn("segments", plan["properties"],
                         "段边界归装箱：规划轮不再输出 sections")
        seg = S.segment_schema(2000)
        # 刹车按本段配额现算（不再写死形态上限）：配额 × (1+容差) ÷ 最短句
        self.assertEqual(seg["properties"]["lines"]["maxItems"],
                         S._lines_hard_cap({}, 2000))

    def test_line_guides_are_derived_from_chars(self):
        """句数一律从字数推，不写死：软句数按期望句长算，刹车按配额算。"""
        cfg = {"gate.min_chars": 8, "gate.max_chars": 40}
        self.assertEqual(S._expected_line_chars(cfg), 24)
        # 配额 2315：软句数 96——不是拿句长上限当除数算出来的 57
        self.assertEqual(S._soft_line_guide(cfg, 2315), 96)
        # 刹车取「合法输出的句数上界」：2315 × 1.15 ÷ 8 = 333
        self.assertEqual(S._lines_hard_cap(cfg, 2315), 333)
        # 提示与刹车不许打架：软句数恒在刹车之内
        for quota in (200, 1471, 2315, 5000):
            self.assertLess(S._soft_line_guide(cfg, quota),
                            S._lines_hard_cap(cfg, quota))

    def test_segment_prompt_gives_no_line_range(self):
        """提示词只给一个软句数、且明说以字数为准。

        给区间就会出事：曾把「57~40 句」原样写给模型（下限大于上限），模型贴着
        下限写完就收手，字数恒定少三成半。
        """
        p = S._segment_system_prompt(ConfigManager().data(), PG.get("paper"),
                                     "argument", 1, 4, 2315, "测试主题",
                                     is_first=True)
        self.assertIn("约 96 句", p)
        self.assertIn("一切以字数为准", p)
        self.assertNotIn("57~40", p)

    def test_insert_prompt_carries_only_chars(self):
        """补字数报「还差 N 字」+ 验收区间，一个句数都不报；落点按全篇句号给全。"""
        seg = [{"speaker": "A", "emotion": "平静", "text": "字" * 10}
               for _ in range(3)]
        p = S.build_insert_prompt(seg, 628, 1839, 57, material="素材正文")
        self.assertIn("还差 628 字", p)
        self.assertIn("验收区间 1564~2114 字", p)
        self.assertNotIn("少了", p)
        self.assertIn("【素材】", p)
        self.assertIn("一个字都不许改", p)
        self.assertIn("填 57", p, "段首落点 = 本段第一句的前一句句号")
        self.assertIn("58~60", p, "本段句号按全篇口径给")
        self.assertIn("填 60", p, "段尾落点")
        self.assertNotRegex(p, r"约 \d+ 句", "补字提示词里不许出现句数引导")

    def test_insert_prompt_carries_segment_context(self):
        """插入轮的上下文与写脚本同源：原文、段主旨、凝缩都要在。

        2d 期插乱的根因之一是插入模型只拿到凝缩摘要——原文细节进不来，
        它只能拿二手材料编句子。原文与段主旨缺一个，这条钉子就红。
        """
        seg = [{"speaker": "A", "emotion": "追问", "text": "字" * 10}
               for _ in range(2)]
        p = S.build_insert_prompt(seg, 100, 500, 5, material="凝缩素材正文",
                                  raw_material="这一段对应的原始正文内容。",
                                  topic="本段讲透一个机制")
        self.assertIn("【本段原文】", p, "对应部分的原文必须给")
        self.assertIn("这一段对应的原始正文内容。", p)
        self.assertIn("【本段主旨】本段讲透一个机制", p)
        self.assertIn("【素材】", p, "凝缩素材照旧给")
        self.assertIn("[A·追问]", p, "正文行带语篇标签，接缝可见")
        # 不给就不得出现假块：空着比造假锚好。
        p2 = S.build_insert_prompt(seg, 100, 500, 5, material="素材")
        self.assertNotIn("【本段原文】", p2)
        self.assertNotIn("【本段主旨】", p2)

    def test_insert_system_carries_the_same_writing_rules(self):
        """插入的写作纪律与写脚本**同源**：站位、连句上限、词表、句长都取自同一处。

        从前这几样在插入那一轮是另写一份的硬编码（8–40 字、六个标签、一句没有
        数字的「别连着说太多」）：写作按收窄后的枚举写、插入按旧的那几个写，
        插入句一落进正文就被 `emotion_vocab` / `line_length` 打回，那一轮白写。
        插入**特有**的铁律（只许新增、不重复）另有一套，见下面几条。
        """
        card = {"cast": "A 是讲述者；B 是追问者。", "form": "alternate"}
        cfg = {"gate.min_chars": 8, "gate.max_chars": 30,
               "script.style_preset": "argument"}
        s = S.insert_system(card, cfg)
        self.assertIn("A 是讲述者；B 是追问者。", s, "站位与写脚本同源")
        cap_a, cap_b = PG.run_caps("alternate")
        self.assertIn("A 最多连着说 %d 句、B 最多连着说 %d 句" % (cap_a, cap_b), s)
        self.assertIn("8–30 字", s, "句长跟着配置走，不写死 8–40")
        self.assertIn("【风格倾向】", s)
        self.assertIn("只许新增", s, "插入特有的铁律不许被统一掉")
        # 不带卡的老调用：不塞卡上的站位进去（内置分工照旧兜底）。
        self.assertNotIn("A 是讲述者", S.insert_system())

    def test_insert_system_forbids_restating(self):
        """补字提示词必须从「内容」上禁止重复，而不是从「字数」上开退路。

        v0.20.0 之前这里只有一句「插进去要接得上」——那只管衔接，不管内容，而
        同义重复恰恰接得上。实测出的形态是同一件事被问两遍（202/203 句），程序
        侧 `dedupe_script` 只删「归一化后一字不差」的句子，措辞不同的拦不住。
        """
        s = S.insert_system()
        self.assertIn("换个说法再说一遍", s, "同义改写必须被点名禁止")
        self.assertIn("带进正文里还没有", s, "每句要带新信息点")
        self.assertIn("各处彼此也不许重复", s, "多处插入之间也要排重")
        self.assertIn("复读不算补字", s)
        # 「宁可少补一点」是逃避出口：一位模型读成「可以少补」，就会滑向
        # 「一句都不补」——字数目标必须仍然是硬的，出口只许往「讲细」走。
        self.assertNotIn("宁可少补", s)
        self.assertIn("讲得更细", s, "出路是写细，不是少写")

    def test_insert_system_shows_what_counts_as_repetition(self):
        """「不许重复」必须配正反例，不能只给一个名词。

        「意思一样就算重复」是句无判据的话——模型拿它对照自己刚写的句子，永远
        判自己合格。要让判据可执行，就得把**算重复**与**不算重复**各摆几条在
        它面前：换个词问同一件事、换个词答同一件事、前一句没答就替它下结论；
        反过来，背景相同但问的是另一件事、补了新细节、补了另一条分支，都不算。
        """
        s = S.insert_system()
        self.assertIn("【这些算重复】", s)
        self.assertIn("【这些不算重复】", s)
        self.assertIn("换个词问同一件事", s)
        self.assertIn("换个词答同一件事", s)
        self.assertIn("前一句还没答", s, "先问再替它下结论是最隐蔽的一种")
        self.assertIn("背景一样，问的是另一件事", s, "反例要给全，不然模型不敢补")
        self.assertIn("补上了新的细节", s)
        self.assertIn("把另一条路补上", s)
        self.assertIn("一句话判据", s, "例子之后要能收成一句可用的判断")
        self.assertIn("换个说法不改变这个判断", s)
        # 「一个点可以拆成问答两句」是同一件事写两遍的开脱口子：模型把它扩张成
        # 「两问 + 一答」凑字数。收紧成一组。
        self.assertIn("一组一问一答", s)
        self.assertNotIn("拆成问答两句", s)

    def test_insert_system_keeps_the_seam_rule(self):
        """「接得上前面那句，也接得住后面那句」这两条不许被挤掉。

        补的句子要落进一段**正在进行**的对话里，前后都得接住——这是补字这件事的
        根本。加内容质控（禁同义复读）时容易只顾着排重，把衔接那条当成旧话删掉，
        而「一句接得上、意思却是重复」恰恰是同义复读的原形：两条各管一头，缺一条
        另一条就漏。
        """
        s = S.insert_system()
        self.assertIn("连贯的对话", s)
        self.assertIn("接得上前面那句", s)
        self.assertIn("接得住后面那句", s)
        self.assertIn("不是注解", s, "补的是话，不是书面插入语")
        self.assertIn("此外还需说明", s, "这类书面插入语被点名禁掉")

    def test_insert_prompt_lists_a_working_order(self):
        """排重要做成一个显式步骤，不能只靠「别重复」这句话自觉。

        「先列已讲要点 → 再从素材挑没讲到的 → 最后写句 → 写完全对一遍」——把
        它写成步骤，模型才有个顺序可循；只给一条禁令，它得自己发明流程。
        """
        seg = [{"speaker": "A", "emotion": "平静", "text": "字" * 10}]
        p = S.build_insert_prompt(seg, 628, 1839, 57, material="素材正文")
        self.assertIn("【怎么补】", p)
        self.assertIn("已经讲过的话", p, "第 1 步：先列出已讲要点")
        self.assertIn("正文还没讲到", p, "第 2 步：从素材挑没讲过的点")
        self.assertIn("划掉重写", p, "第 3 步收尾：写完自己回对一遍")
        self.assertLess(p.index("【怎么补】"), p.index("现在输出 JSON"),
                        "工作顺序要排在「现在输出 JSON」之前")

    def test_trim_prompt_carries_no_material(self):
        """压字数只说「多 N 字」：哪句该压、哪句该删由模型判断，不预先点名。"""
        seg = [{"speaker": "A", "emotion": "承接", "text": "字" * 30}]
        p = S.build_trim_prompt(seg, 261, 1839, 0)
        self.assertIn("多了 261 字", p)
        self.assertIn("1. [A·承接]", p, "正文按全篇句号列出，带语篇标签")
        self.assertNotIn("【素材】", p, "压字数不带素材")
        self.assertNotRegex(p, r"约 \d+ 句", "压字提示词里不许出现句数引导")
        self.assertIn("不许新增句子", S.trim_system())

    def test_insert_lands_between_any_two_lines(self):
        """插入可以落在任意两句之间，也可以落在段首 / 段尾；已有句子一个字不动。"""
        cfg = {"gate.min_chars": 8, "gate.max_chars": 40}
        seg = [{"speaker": "A", "emotion": "承接", "text": "原句一" * 5},
               {"speaker": "B", "emotion": "承接", "text": "原句二" * 5}]
        mid = S.apply_insert(seg, [{"after": 6, "speaker": "B", "emotion": "追问",
                                    "text": "插在中间" * 5}], 5, cfg)
        self.assertEqual([l["text"] for l in mid],
                         [seg[0]["text"], "插在中间" * 5, seg[1]["text"]])
        head = S.apply_insert(seg, [{"after": 5, "speaker": "B", "emotion": "承接",
                                     "text": "插在最前" * 5}], 5, cfg)
        self.assertEqual(head[0]["text"], "插在最前" * 5)
        tail = S.apply_insert(seg, [{"after": 7, "speaker": "B", "emotion": "承接",
                                     "text": "插在最后" * 5}], 5, cfg)
        self.assertEqual(tail[-1]["text"], "插在最后" * 5)
        self.assertEqual(len(tail), 3, "只增不减")

    def test_insert_rejects_out_of_range_and_bad_speaker(self):
        """落点越界、说话人非法、句长出门禁一律报错，不静默丢弃。"""
        cfg = {"gate.min_chars": 8, "gate.max_chars": 40}
        seg = [{"speaker": "A", "emotion": "", "text": "字" * 20}]
        with self.assertRaises(S.ScriptError):
            S.apply_insert(seg, [{"after": 99, "speaker": "A",
                                  "text": "字" * 20}], 5, cfg)
        with self.assertRaises(S.ScriptError):
            S.apply_insert(seg, [{"after": 5, "speaker": "C",
                                  "text": "字" * 20}], 5, cfg)
        with self.assertRaises(S.ScriptError):
            S.apply_insert(seg, [{"after": 5, "speaker": "A", "text": "字"}], 5, cfg)
        with self.assertRaises(S.ScriptError):
            S.apply_insert(seg, [{"after": 5, "speaker": "A", "emotion": "好奇",
                                  "text": "字" * 20}], 5, cfg)

    def test_insert_and_trim_contracts_are_mirror_images(self):
        """补字契约里没有 index（改不了已有句子）；压字契约里没有 after（加不了新句）。"""
        ins = json.dumps(S.SEGMENT_INSERT_SCHEMA, ensure_ascii=False)
        self.assertIn("after", ins)
        self.assertNotIn("index", ins)
        trim = json.dumps(S.SEGMENT_TRIM_SCHEMA, ensure_ascii=False)
        self.assertIn("drops", trim)
        self.assertNotIn("after", trim)

    def test_insert_and_trim_caps_scale_with_chars(self):
        """两条路的 schema 刹车都按字数 / 句数现算，不写死。"""
        cfg = {"gate.min_chars": 8}
        self.assertEqual(S._insert_hard_cap(cfg, 628), 91)   # 628×1.15÷8 上取整
        self.assertEqual(S._insert_hard_cap(cfg, 8), 2)
        ins = S.insert_schema(628, cfg)
        self.assertEqual(ins["properties"]["inserts"]["maxItems"], 91)
        trim = S.trim_schema(7)
        self.assertEqual(trim["properties"]["edits"]["maxItems"], 7)
        self.assertEqual(trim["properties"]["drops"]["maxItems"], 7)

    def test_trim_rejects_conflict_and_wipeout(self):
        """同一句不许既改又删；也不许把整段删光——段没了账就无从对起。"""
        cfg = {"gate.min_chars": 8, "gate.max_chars": 40}
        seg = [{"speaker": "A", "emotion": "", "text": "字" * 30},
               {"speaker": "B", "emotion": "", "text": "字" * 30}]
        with self.assertRaises(S.ScriptError):
            S.apply_trim(seg, [{"index": 1, "text": "字" * 20}], [1], 0, cfg)
        with self.assertRaises(S.ScriptError):
            S.apply_trim(seg, [], [1, 2], 0, cfg)
        # 正常压字：改一句、删一句，行数只减不增
        out = S.apply_trim(seg, [{"index": 2, "text": "字" * 20}], [1], 0, cfg)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "字" * 20)

    def test_auto_patch_reads_gate_wording(self):
        """假模型要认门禁那条路的措辞（「第 N 句：过长 52 字…」）。

        段内不再走定点修补之后，正则若还锚在段内旧措辞（「偏短 / 偏长」）上，一旦
        门禁真的点名，假模型会误报「没点名任何句子」——测试自己先失效。
        """
        user = ("**只改下面这几句。** 没列出的句子一个字都不许动\n"
                "    第 3 句：过长 52 字（上限 40 字）：精简措辞压进 40 字以内\n"
                "    第 8 句：措辞禁忌命中「显然」。换成更稳妥的说法\n")
        edits = json.loads(_SegmentedLLM._auto_patch(user))["edits"]
        self.assertEqual([e["index"] for e in edits], [3, 8])

    def test_section_quotas_sum_to_target_and_proportional(self):
        """配额先摊到**节**：q_i = target × w_i / Σw，余数有归属、合计精确。

        权重 w 是「这一节的原文有效字」（`chars`）：甲 1000、乙 700。

        先摊到节、再装箱，与从前按段摊是同一套算术——段权重本就是段内各节权重
        之和，摊派线性、加法可交换。区别只在顺序：现在装箱前就全算完，不迭代。
        """
        secs = _evidence()["sections"]
        qs = S._section_quotas(secs, 1000)
        self.assertEqual(sum(qs), 1000, "配额合计必须精确等于目标，余数有归属")
        self.assertEqual(qs, [588, 412], "原文有效字 1000:700 → 588 / 412")

    def test_quota_weight_is_material_words_not_gist(self):
        """配额权重是**原文有效字**，不是摘要自己的字数，也不是原始字符数。

        三把尺量三件事，各有各的用途：
        - `chars`（原文有效字）——**产能**，配额用它：成稿目标出自
          `chars_for_target`（单位=有效字/秒 × 秒），地图压比也拿有效字比；
        - `loc.chars`（原始字符数）——**装箱折 token 的重量**，含排版符号与西文
          数字，它们念不成本；
        - gist + points（摘要自己写得多长）——与料无关。

        两节刻意**反向**，让三把尺给出三种答案：有效字甲 1000 / 乙 600 →
        625 / 375；原始字符数甲 1400 / 乙 1224 → 533 / 467；摘要字数甲极短、乙极长。
        只有跟有效字走才对得上地图的压比。
        """
        secs = [
            {"anchor": "甲节", "source": "s1", "gist": "短", "points": [],
             "chars": 1000, "loc": {"chars": 1400}},
            {"anchor": "乙节", "source": "s2", "gist": "长" * 300,
             "points": ["要点" * 100], "chars": 600, "loc": {"chars": 1224}},
        ]
        qs = S._section_quotas(secs, 1000)
        self.assertEqual(qs, [625, 375], "料厚的多分、料薄的少分，与摘要长短无关")
        self.assertNotEqual(qs, [533, 467], "原始字符数不是产能，不许当权重")

    def test_missing_material_words_is_reported(self):
        """没有原文有效字就算不出配额：**报错**，不退回别的尺。

        退回去等于把摊歪的配额重新摊一遍，而且从日志上看不出来用的是哪把尺。
        """
        secs = [{"anchor": "甲节", "source": "s1", "gist": "主旨", "points": []}]
        with self.assertRaises(S.ScriptError) as cm:
            S._section_quotas(secs, 1000)
        self.assertIn("有效字", str(cm.exception))

    def test_plan_validation_requires_one_topic_per_segment(self):
        """段数已由逻辑拆分与装箱定死：题目多了少了都打回，模型没有增删段的余地。"""
        topics, err = S._validate_plan({"topics": ["只给了一段"]}, 2)
        self.assertIsNone(topics)
        self.assertIn("段数是 2", err)

    def test_plan_validation_rejects_blank_topic(self):
        topics, err = S._validate_plan({"topics": ["甲", "  "]}, 2)
        self.assertIsNone(topics)
        self.assertIn("第 2 段", err)

    def test_plan_validation_accepts_one_topic_per_segment(self):
        topics, err = S._validate_plan({"topics": ["甲", "乙"]}, 2)
        self.assertIsNone(err)
        self.assertEqual(topics, ["甲", "乙"])

    def test_plan_validation_rejects_missing_topics(self):
        """老格式（segments + sections）不再被接受：段边界归逻辑拆分与装箱。"""
        topics, err = S._validate_plan(
            {"segments": [{"sections": [1], "topic": "甲"}]}, 1)
        self.assertIsNone(topics)
        self.assertIn("topics", err)


class _BudgetLLM(object):
    """带输入额度能力的假模型：装箱与裁剪都按 token 算，这里给一个确定的折算。

    折算比固定 1.0（一个字符一个 token），额度 = 最大输出 × 倍率，
    `material_tokens` 不做余量——测试要盯的是**装不装得下这条算术**，
    余量那套在 `llm_client` 自己的用例里。
    """

    def __init__(self, per_char=1.0, ratio=1.0):
        self.input_ratio = ratio
        self.per_char = per_char

    def input_budget_tokens(self, max_tokens):
        return int(int(max_tokens) * self.input_ratio)

    def material_tokens(self, chars):
        return int(round(max(0, int(chars)) * self.per_char))


def _secs(n):
    """造 n 节凝缩：配额权重（原文有效字 chars）依次递增，便于盯配额的摊法。

    `loc`（原始字符数）是装箱折 token 那一路用的，与配额无关——三把尺的区分由
    `test_quota_weight_is_material_words_not_gist` 专门盯。
    """
    return [{"source": "s%d" % (i + 1), "anchor": "第%d节" % (i + 1),
             "gist": "主旨" * (i + 1), "points": [], "chars": (i + 1) * 100,
             "loc": {"start": 1, "end": 9, "chars": (i + 1) * 130}}
            for i in range(n)]


class TestLogicSplit(unittest.TestCase):
    """逻辑拆分：按**内容**把节分组成段——段边界的第一道来源。

    它只回节序号：标题、题目、数字全由程序从节号映射出来。模型给的分组要过三道
    归一（越界/重复丢掉、组内与组间按序、漏掉的补成独立段）；两次都答不出合法
    分组就退回一节一段（不合并）——最保守的落法，不会错，只是调用次数多。
    """

    class _LLM(object):
        def __init__(self, replies):
            self.replies = list(replies)
            self.users = []
            self.systems = []
            self.schemas = []

        def chat(self, messages, temperature=0.8, max_tokens=8192,
                 json_schema=None, **kw):
            self.systems.append(messages[0].get("content") or "")
            self.users.append(messages[-1].get("content") or "")
            self.schemas.append(json_schema)
            return self.replies.pop(0), {}

    def _split(self, reply, n=4):
        llm = self._LLM([reply] if isinstance(reply, str) else reply)
        got = S.split_by_logic(_secs(n), llm,
                               {"llm.max_tokens": 8192}, log=lambda m: None)
        return got, llm

    def test_groups_come_back_as_section_groups(self):
        got, _ = self._split('{"groups": [[1, 2], [3], [4]]}')
        self.assertEqual(got, [[1, 2], [3], [4]])

    def test_out_of_order_groups_are_reordered(self):
        """重排要归一：讲述顺序由地图定死，后一节的料不许提到前面写。"""
        got, _ = self._split('{"groups": [[3, 4], [1, 2]]}')
        self.assertEqual(got, [[1, 2], [3, 4]])

    def test_missing_sections_become_their_own_segment(self):
        """漏掉的节补成独立一段。

        漏节不是「少讲一点」，是那一节**从此不在任何一段里**，而清单看上去完整
        ——取料只看各段的取材范围，不会回头对账有多少节没落上。
        """
        got, _ = self._split('{"groups": [[1, 3]]}')
        self.assertEqual(got, [[1, 3], [2], [4]])

    def test_out_of_range_and_duplicate_numbers_are_dropped(self):
        got, _ = self._split('{"groups": [[1, 1, 9], [2, 3, 4]]}')
        self.assertEqual(got, [[1], [2, 3, 4]])

    def test_illegal_replies_fall_back_to_one_section_each(self):
        """两次都拿不到合法分组：退回一节一段，不是崩掉，也不当它答对了。"""
        got, llm = self._split(['不是 JSON', '{"groups": "甲"}'], n=3)
        self.assertEqual(got, [[1], [2], [3]])
        self.assertEqual(len(llm.users), S.LOGIC_SPLIT_RETRIES + 1, "打回到上限")

    def test_feedback_names_what_was_wrong(self):
        """打回要把问题说清楚再问一次，不是原样重发。"""
        got, llm = self._split(['{"groups": []}', '{"groups": [[1, 2]]}'], n=2)
        self.assertEqual(got, [[1, 2]])
        self.assertIn("上一版分组的问题", llm.users[1])

    def test_prompt_states_the_reading_order_rule(self):
        """防重排是两头：提示词先说死，程序再兜底归一。"""
        _, llm = self._split('{"groups": [[1, 2, 3, 4]]}')
        self.assertIn("不许重排", llm.systems[0])
        self.assertIn("不许漏节", llm.systems[0])

    def test_prompt_feeds_gist_but_no_size_numbers(self):
        """清单只给「节号 + 标题 + 凝缩」，**一个体量数字都不给**。

        分组看的是语义。挂上字数不但没用，还会把模型往"按字数摊匀"上带——而铁律
        自己写着「归类看内容，不看体量」，schema 注释也写着"模型不产任何数字"，
        user 侧却先递了两个进去，自相矛盾。
        """
        _, llm = self._split('{"groups": [[1, 2, 3, 4]]}')
        user = llm.users[0]
        self.assertIn("第 3 节", user)
        self.assertIn("主旨主旨主旨", user, "每节的凝缩要进清单")
        self.assertNotRegex(user, r"\d+ 字", "清单里不许出现任何体量数字")
        self.assertNotIn("凝缩 ", user, "「凝缩 N 字」这一栏已撤")

    def test_no_model_call_when_there_is_nothing_to_group(self):
        llm = self._LLM([])
        self.assertEqual(S.split_by_logic(_secs(1), llm,
                                          {"llm.max_tokens": 8192}), [[1]])
        self.assertEqual(llm.users, [], "一节没有分组的余地，不该开调用")


class TestPlanPromptHasNoNumbers(unittest.TestCase):
    """规划轮只写「期标题 + 各段段主旨」，**一个体量数字都不给**。

    段边界是程序定的、配额是程序摊的，字数摆进这一轮只会把"这一段该讲什么"带成
    "这一段该多长"。同理「题目」这个叫法也换掉：它产出的是**段主旨**，与期主旨
    同层级，不是给段起个名字。
    """

    def _prompt(self):
        secs = _secs(2)
        groups = [{"quota": 1823, "sections": [1], "topic": "",
                   "pieces": [{"sec": 1}]},
                  {"quota": 4201, "sections": [2], "topic": "",
                   "pieces": [{"sec": 2}]}]
        return S._segment_plan_user({"title": "本期", "gist": "主旨", "chars": 900},
                                    secs, groups)

    def test_user_prompt_shows_no_size(self):
        """段清单里一个体量数字都不给。

        本期计划块的「本期素材共约 N 字」**不算**——那条是给写作模型的预算依据
        （素材撑不满目标字数时，它唯一能执行的出路是照实写短），不是给规划轮的。
        这里只查【段清单】这一段。
        """
        p = self._prompt()
        listing = p.split("【段清单】", 1)[1]
        self.assertNotRegex(listing, r"\d+ 字",
                            "段清单里不许出现配额或凝缩体量")
        self.assertNotIn("配额约", listing)
        self.assertIn("取材：第 1 节", listing, "取材范围仍要给——它是不越界的边界")

    def test_user_prompt_calls_it_segment_gist(self):
        p = self._prompt()
        self.assertIn("段主旨", p)
        self.assertNotIn("题目", p, "它产出的是段主旨，不是「题目」")

    def test_system_prompt_calls_it_segment_gist(self):
        p = S._segment_plan_system(2)
        self.assertIn("段主旨", p)
        self.assertNotIn("题目", p)
        self.assertNotIn("第 N 块", p, "块早已撤掉（切分单位是整节凝缩）")


class TestPacking(unittest.TestCase):
    """装箱：把一个**逻辑段**按输入额度切成写作段——切分单位是整节凝缩。

    这套算术管的是**容量**，不管语义：哪几节是一件事由逻辑拆分定（见
    `TestLogicSplit`），装箱只负责「一个装得下就装、装不下就切、单节超容就报错」。
    盯四件事：组内装得下就整组一段、装不下按整节切、两个逻辑段不许并进同一段、
    已写正文要从额度里扣掉；单节自身超容时报错，而不是无限往下切。
    """

    def _pack(self, sec_chars, groups=None, max_tokens=10000, target=1000,
              fixed=0, ratio=1.0, log=None):
        llm = _BudgetLLM(ratio=ratio)
        return S.pack_segments(_secs(len(sec_chars)), sec_chars, llm,
                               {"llm.max_tokens": max_tokens}, fixed_chars=fixed,
                               target_chars=target, log=log or (lambda m: None),
                               groups=groups)

    def test_a_logical_group_is_split_only_by_capacity(self):
        """一个逻辑段装不下时，**在它内部**按整节切成几段。

        [1,2,3] 是一件事（逻辑拆分的结论），但三节 3000/3000/5000 加起来
        11000 > 额度 10000：前两节合成一段、第三节单独一段。切完仍是**完整的
        节**——没有哪个节被从中间切开，取材块恒为「整节第 1/1 块」。
        """
        groups = self._pack([3000, 3000, 5000], groups=[[1, 2, 3]])
        self.assertEqual([g["sections"] for g in groups], [[1, 2], [3]])
        self.assertEqual(sum(g["quota"] for g in groups), 1000,
                         "段配额合计精确等于目标")
        for g in groups:
            for pc in g["pieces"]:
                self.assertEqual((pc["part"], pc["parts"]), (1, 1),
                                 "取材块恒为整节，不再有半节")

    def test_a_logical_group_that_fits_stays_one_segment(self):
        """装得下就整组一段：一组几节只由语义定，**不设节数上限**。

        从前有一段最多合 3 节的常数。那是给「一次写太长会漂移」打的补丁，可段长
        现在由额度管着——额度装得下就说明这一次写得完；再拿一个常数去卡它，等于
        把语义边界重新交回算术。
        """
        groups = self._pack([100, 100, 100, 100, 100], groups=[[1, 2, 3, 4, 5]])
        self.assertEqual([g["sections"] for g in groups], [[1, 2, 3, 4, 5]])

    def test_two_logical_groups_never_share_a_segment(self):
        """两个逻辑段之间是话题转折：它们的节**不许**被装进同一个写作段。

        只看容量的话，两组共 400 字远远装得下，必然并成一段——那等于让模型一口
        气写两个话题，段题目也只能写一个，逻辑拆分就白做了。
        """
        groups = self._pack([100, 100, 100, 100], groups=[[1, 2], [3, 4]])
        self.assertEqual([g["sections"] for g in groups], [[1, 2], [3, 4]])

    def test_bucket_capacity_shrinks_with_written_text(self):
        """桶容量逐段递减：写过的正文要一直带着，它涨一截，能装原文的地方就少一截。

        同一批料、同一个额度，只有「成稿目标」不同：目标小则前面各段写出来的
        字数少，第二个逻辑段的两节还能进同一个桶；目标大则已写正文吃掉一块额度，
        那两节只能拆成两段。
        """
        wide = self._pack([2000, 2000, 5000, 5000], groups=[[1, 2], [3, 4]],
                          max_tokens=12000, target=1000)
        self.assertEqual([g["sections"] for g in wide], [[1, 2], [3, 4]])
        tight = self._pack([2000, 2000, 5000, 5000], groups=[[1, 2], [3, 4]],
                           max_tokens=12000, target=8000)
        self.assertEqual([g["sections"] for g in tight], [[1, 2], [3], [4]],
                         "已写正文要从桶容量里扣掉")

    def test_over_cap_asks_for_more_budget(self):
        """单个节自己就超过额度：报错要求调倍率 / 拆期，不许往下切半个节。

        切分单位是整节凝缩——到这一节就切无可切。再往下切只能切半个节，半个节取
        不出原文、也算不出账；而无限往下拆本身也没有收益（几千字要调用十几次）。
        """
        with self.assertRaises(S.ScriptError) as cm:
            self._pack([100000], max_tokens=10000)
        self.assertIn("input_ratio", str(cm.exception))
        self.assertIn("切无可切", str(cm.exception))

    def test_no_headroom_for_fixed_overhead_is_reported(self):
        """额度连提示词都装不下：报错说清是哪个旋钮要调，不静默硬塞。"""
        with self.assertRaises(S.ScriptError) as cm:
            self._pack([1000], max_tokens=100, fixed=500)
        self.assertIn("放不下", str(cm.exception))

    def test_backend_without_budget_api_falls_back_to_one_section_each(self):
        """没有额度能力的后端（假模型 / 将来别的客户端）：不装箱，一节一段。

        不合并也不切块——最保守的落法，也是从前规划反复不过时的硬分兜底。
        """
        class _Dumb(object):
            pass
        groups = S.pack_segments(_secs(3), [10, 20, 30], _Dumb(),
                                 {"llm.max_tokens": 8192}, target_chars=1000)
        self.assertEqual([g["sections"] for g in groups], [[1], [2], [3]])
        self.assertEqual(sum(g["quota"] for g in groups), 1000)

    def test_backend_without_budget_api_still_keeps_the_logical_groups(self):
        """量不出重量，不等于分组作废：不装箱时**照逻辑分组落段**。

        「哪几节是一件事」是模型读内容得出的结论，跟「这次装不装得下」是两回事。
        从前这里一律退成一节一段，等于把刚分好的组扔掉——分组白做一次。
        """
        class _Dumb(object):
            pass
        groups = S.pack_segments(_secs(4), [10, 20, 30, 40], _Dumb(),
                                 {"llm.max_tokens": 8192}, target_chars=1000,
                                 groups=[[1, 2], [3, 4]])
        self.assertEqual([g["sections"] for g in groups], [[1, 2], [3, 4]])
        self.assertEqual(sum(g["quota"] for g in groups), 1000)

    def test_refit_batches_by_whole_sections(self):
        """段内兜底按**整节**分批喂完：合不来的节各自一批，一节都不丢。

        三节各 900 字、可用 1000 token（折算 1:1）：两节一批就 1800 超了，所以
        一节一批；总长度不变。
        """
        llm = _BudgetLLM()
        pieces = [{"sec": k, "part": 1, "parts": 1, "start": 0, "end": 900,
                   "chars": 900} for k in (1, 2, 3)]
        out = S._refit_pieces(pieces, llm, 1000)
        self.assertEqual([[p["sec"] for p in one] for one in out],
                         [[1], [2], [3]])
        self.assertEqual(sum(p["chars"] for one in out for p in one), 2700)

    def test_refit_keeps_small_sections_together(self):
        """装得下就合成一批：批不是「块」，是「一次喂进去的那几节」。"""
        llm = _BudgetLLM()
        pieces = [{"sec": k, "part": 1, "parts": 1, "start": 0, "end": 900,
                   "chars": 900} for k in (1, 2)]
        out = S._refit_pieces(pieces, llm, 3000)
        self.assertEqual([[p["sec"] for p in one] for one in out], [[1, 2]])

    def test_refit_never_cuts_a_section_in_half(self):
        """一节自己就装不下 → 报错，**不许把它切成半个节**。

        半个节取不出原文（`source_store.compose` 一次只认一个落点），也算不出账
        （配额按整节摊、内容只有半节）。这里从前是「按字符位置再切几块」，切到
        8 块还有退路；现在直接停在这一节上，要求调倍率或拆期。
        """
        llm = _BudgetLLM()
        with self.assertRaises(S.ScriptError) as cm:
            S._refit_pieces(
                [{"sec": 1, "part": 1, "parts": 1,
                  "start": 0, "end": 9000, "chars": 9000}], llm, 1000)
        self.assertIn("切无可切", str(cm.exception))
        self.assertIn("input_ratio", str(cm.exception))

    def test_fits_inside_budget_is_left_alone(self):
        llm = _BudgetLLM()
        one = [{"sec": 1, "part": 1, "parts": 1, "start": 0, "end": 900, "chars": 900}]
        self.assertEqual(S._refit_pieces(one, llm, 3000), [one])

    def test_refit_never_produces_a_half_section(self):
        """分批的结果里**不许出现半个节**：取材块恒为整节。

        半个节的坐标取不出原文——`source_store.compose` 一次只认一个落点，半截
        等于拿第一节的 `source/anchor/line` 去取它自己的一半，数字对不上、内容
        也缺一块。所以 `_refit_pieces` 只**重新分批**，不重新切节：进去的是整节，
        出来的还是那几个整节。
        """
        llm = _BudgetLLM()
        pieces = [{"sec": 1, "part": 1, "parts": 1, "start": 0, "end": 4000,
                   "chars": 4000},
                  {"sec": 2, "part": 1, "parts": 1, "start": 0, "end": 4000,
                   "chars": 4000}]
        out = S._refit_pieces(pieces, llm, 5000)
        self.assertEqual([[p["sec"] for p in one] for one in out], [[1], [2]])
        for one in out:
            for pc in one:
                self.assertEqual((pc["part"], pc["parts"]), (1, 1),
                                 "取材块恒为整节，不许有半节")
                self.assertEqual((pc["start"], pc["end"]), (0, 4000),
                                 "整节的区间就是原节本身，不许被重切")


class TestFitMaterialBudget(unittest.TestCase):
    """素材裁剪：按**输入额度**（token）判，两端同单位。

    从前这里比的是「朗读字数 − 字符数」，三个数三种语义。技术文档那类素材
    2.6 个字符才顶 1 个朗读字，一跨尺就把装得下的判成装不下。
    """

    def _cfg(self):
        return {"llm.max_tokens": 1000}

    def test_fits_when_material_within_budget(self):
        llm = _BudgetLLM(ratio=2.0)
        text, note = S.fit_material(llm, "素" * 1200, self._cfg(), other_chars=500)
        self.assertEqual(note, "")
        self.assertEqual(len(text), 1200)

    def test_evidence_is_used_when_material_over(self):
        llm = _BudgetLLM(ratio=2.0)
        text, note = S.fit_material(llm, "素" * 5000, self._cfg(),
                                    other_chars=500, evidence=_evidence())
        self.assertIn("凝缩", note)
        self.assertIn("甲节", text, "放不下时用凝缩顶替，且必须留痕")

    def test_truncates_by_token_budget_without_evidence(self):
        llm = _BudgetLLM(ratio=2.0)
        text, note = S.fit_material(llm, "素" * 5000, self._cfg(), other_chars=500)
        self.assertIn("节选", text)
        self.assertLess(len(text), 5000, "没有凝缩才截断，且按额度截")

    def test_backend_without_budget_api_keeps_material_intact(self):
        class _Dumb(object):
            pass
        text, note = S.fit_material(_Dumb(), "素" * 99999, {"llm.max_tokens": 10})
        self.assertEqual(note, "")
        self.assertEqual(len(text), 99999, "裁不了不等于要裁")

    def test_no_headroom_reports_budget_not_content_capacity(self):
        """额度被占位吃光：提示词要说清是「输入额度 = 最大输出 × 输入倍率」。"""
        llm = _BudgetLLM(ratio=1.0)
        text, note = S.fit_material(llm, "素" * 100, self._cfg(),
                                    other_chars=2000, evidence=_evidence())
        self.assertIn("输入倍率", note)
        self.assertNotIn("压缩档", note, "这里不该出现画地图那把尺的说法")


class _PackedLLM(_SegmentedLLM):
    """分段假模型 + 输入额度能力：用来跑「按节装箱」的整条路。"""

    def __init__(self, *args, **kw):
        per_char = kw.pop("per_char", 1.0)
        ratio = kw.pop("ratio", 1.0)
        super().__init__(*args, **kw)
        self.per_char = per_char
        self.input_ratio = ratio

    def input_budget_tokens(self, max_tokens):
        return int(int(max_tokens) * self.input_ratio)

    def material_tokens(self, chars):
        return int(round(max(0, int(chars)) * self.per_char))


class TestSectionFedSegments(unittest.TestCase):
    """每段只喂本段那几节的原文——不再把整期素材过一遍闸门。"""

    def _cfg(self, **over):
        cfg = ConfigManager().data()
        cfg.update({"script.gate_strict": False,
                    "gate.min_deviation_seconds": 9999,
                    "project.program_name": "播客",
                    # 钉的是「哪段喂哪节」，段数 = 节数是前提——碎段合并会改
                    # 段数，这里显式关掉；合并行为有专属用例。
                    "script.segment_min_sents": 0})
        cfg.update(over)
        return cfg

    def test_segment_topics_come_back_with_the_text(self):
        """段主旨跟着正文一起出函数——旁挂规划档全靠这第四项。

        从前 `_generate_segmented` 只回 (title, plan, script)：段主旨填进 groups
        之后就再没人看得见，函数一返回即弃。而「前期回顾」要引用的正是上一期讲了
        哪几块（见 `pipeline.review_rows`），丢了就只能读到空数组。
        """
        llm = _SegmentedLLM([_plan_payload(n=2)], [_seg_payload(8), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        title, plan, script, segs = S._generate_segmented(
            llm, cfg, "整期素材", _evidence(), card,
            "argument", None, 400, lambda m: None, None, None, sec_chars=[300, 300])
        self.assertEqual([s["no"] for s in segs], [1, 2])
        self.assertEqual([s["topic"] for s in segs],
                         ["第 1 段讲什么", "第 2 段讲什么"])
        self.assertEqual([s["sections"] for s in segs], [[1], [2]],
                         "覆盖了哪几节一并留下，回顾读不懂段主旨时还有据可查")
        self.assertTrue(all(s["quota"] > 0 for s in segs))

    def test_each_segment_takes_only_its_own_sections(self):
        """一节一段时，第二段的原文一个字都不许进第一段的提示词。

        这里用**没有额度能力**的假模型跑：逻辑拆分默认给「一节一段」，装箱又
        退化成原样落段，段数由节数定死。带额度能力时段数会随额度浮动（那是
        `TestPacking` 盯的事），验「哪段喂哪节」就不该跟那套算术绑在一起。
        """
        seen, texts = [], ["甲" * 300, "乙" * 300]

        def of(pieces):
            seen.append([(p["sec"], p["part"]) for p in pieces])
            return "\n".join(texts[p["sec"] - 1] for p in pieces)

        llm = _SegmentedLLM([_plan_payload(n=2)], [_seg_payload(8), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        S._generate_segmented(llm, cfg, "整期素材", _evidence(), card,
                              "argument", None, 400, lambda m: None, None, None,
                              sec_chars=[300, 300], material_of=of)
        self.assertEqual(seen, [[(1, 1)], [(2, 1)]], "每段只取本段的节")
        self.assertIn("甲甲", llm.segment_users[0])
        self.assertNotIn("乙乙", llm.segment_users[0],
                         "第二段的原文不该进第一段的提示词")
        self.assertIn("【本段取材范围】", llm.segment_users[0],
                      "范围由程序给死，模型不许自己划")

    def test_merged_segment_gets_every_section_it_covers(self):
        """一组几节合成一段时，这一段要拿到**组里每一节**的原文。

        「两节是一件事」是逻辑拆分的结论（这里显式给它 `[[1, 2]]`）：既然判成一件事，
        就该一口气写完，漏掉任何一节都等于把地图上定好的内容整段丢掉——比写歪
        严重得多，所以单盯一条。
        """
        seen, texts = [], ["甲" * 300, "乙" * 300]

        def of(pieces):
            seen.append([(p["sec"], p["part"]) for p in pieces])
            return "\n".join(texts[p["sec"] - 1] for p in pieces)

        # 默认配置的额度是几万 token，两节 300 字加起来离边界极远——必定并成一段。
        llm = _PackedLLM([_plan_payload(n=1)], [_seg_payload(14)],
                         logic_payloads=['{"groups": [[1, 2]]}'])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        S._generate_segmented(llm, cfg, "整期素材", _evidence(), card,
                              "argument", None, 400, lambda m: None, None, None,
                              sec_chars=[300, 300], material_of=of)
        self.assertEqual(seen, [[(1, 1), (2, 1)]], "并成一段就要两节的原文一起喂")
        self.assertEqual(llm.segment_calls, 1, "两节并成一段就是一次调用")
        self.assertIn("甲甲", llm.segment_users[0])
        self.assertIn("乙乙", llm.segment_users[0])

    def test_missing_material_source_falls_back_to_whole(self):
        """拿不到逐节原文时退回整期素材：不让人卡在一步跑不动。"""
        llm = _PackedLLM([_plan_payload(n=2)], [_seg_payload(8), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        S._generate_segmented(llm, cfg, "整期素材", _evidence(), card,
                              "argument", None, 400, lambda m: None, None, None)
        self.assertEqual(len(llm.segment_users), 2)
        for user in llm.segment_users:
            self.assertIn("整期素材", user)



class TestSegmentedGeneration(unittest.TestCase):
    """分段生成：规划打回与硬分兜底、段不足插句 / 超出压字、差额滚入下段、门禁照旧。"""

    def _cfg(self, **over):
        cfg = ConfigManager().data()
        cfg.update({"script.gate_strict": False,
                    "gate.min_deviation_seconds": 9999,
                    "project.program_name": "播客",
                    # 这批测试钉的是装箱/插压行为，段数是断言的一部分——
                    # 碎段合并会改段数，在这里显式关掉；合并行为有专属用例。
                    "script.segment_min_sents": 0})
        cfg.update(over)
        return cfg

    def test_overshoot_triggers_trim(self):
        """段超容差不重写整段：已写的话留着，压紧措辞或拿掉多余的话。

        重摇等于把整段扔掉、把同一个函数原样重跑一遍（实测两次输出只差 0.9%），
        这里盯的就是「不再重写」这件事：两段各只生成一次，超容差那段另外走一次
        压字数。
        """
        llm = _SegmentedLLM(
            [_plan_payload(n=2)],
            # 段 1 首版 10 句 ×31 字（含句尾句号）＝310 字（配额 235、容差
            # ±60，按有效字超 10）→ 压字轮把最长的几句压到句长下限，落回配额；
            # 段 2 配额按差额动态算，在容差内。句尾标点按 ×0.5 计有效字。
            [_seg_payload(10), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        logs = []
        title, plan, script, _segs = S._generate_segmented(
            llm, cfg, "素" * 100, _evidence(), card, "argument",
            {"planned_episodes": 5}, 400, logs.append, None, None)
        self.assertEqual(llm.segment_calls, 2, "两段各生成一次，超容差的那段不重写")
        self.assertEqual(llm.trim_calls, 1, "超容差走压紧删减")
        self.assertEqual(llm.insert_calls, 0, "没有缺口就不该插句")
        self.assertEqual(len(script), 15, "压字只改字数，一行都不增删")
        self.assertEqual(sum(len(l["text"]) for l in script), 395)
        self.assertLess(len(script[0]["text"]), 30, "被压的都是最长的那些句子")
        self.assertEqual(title, "测试标题")
        self.assertEqual(plan, 5, "总期数以项目为准，程序回填")
        self.assertTrue(any("压紧删减" in m for m in logs))

    def test_insert_feeds_material_and_trim_does_not(self):
        """少了要素材（新增的每一句都得有出处），多了不要（要减的话都在草稿里）。"""
        llm = _SegmentedLLM([_plan_payload(n=2)], [_seg_payload(3), _seg_payload(40)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        S._generate_segmented(llm, cfg, "素" * 400, _evidence(), card,
                              "argument", None, 900, lambda m: None, None, None)
        self.assertTrue(llm.insert_users, "段 1 严重不足，应走新增插入")
        self.assertTrue(llm.trim_users, "段 2 严重超出，应走压紧删减")
        for user in llm.insert_users:
            self.assertIn("【素材】", user, "插入必须带素材")
            self.assertIn("还差", user)
        for user in llm.trim_users:
            self.assertNotIn("【素材】", user, "压字数不带素材")
            self.assertIn("多了", user)

    def test_every_call_uses_config_budget(self):
        """预算一律按配置走：任何一次调用都不许自己夹一个更小的值。

        推理型模型的思考段与答案段共用同一份输出预算。规划轮曾把配置值夹到
        4096，思考段先把它吃光（实测 reasoning_tokens=4095），答案段一个字也轮
        不上——报出来的是「模型返回空答案」，看着像模型不听话，实际是预算被调用
        处改小了。这里用一个哨兵值跑完整条分段路，凡夹过预算的调用点都会被抓住。
        """
        sentinel = 48640
        llm = _SegmentedLLM([_plan_payload(n=2)], [_seg_payload(3), _seg_payload(40)])
        cfg = self._cfg(**{"llm.max_tokens": sentinel})
        card = S.resolve_paradigm(None, cfg)
        S._generate_segmented(llm, cfg, "素" * 400, _evidence(), card,
                              "argument", None, 900, lambda m: None, None, None)
        self.assertTrue(llm.plan_calls and llm.segment_calls,
                        "规划与分段都要走到，否则测不到全部调用点")
        self.assertTrue(llm.insert_calls and llm.trim_calls,
                        "补字与压字两条路都要走到，否则测不到全部调用点")
        self.assertEqual(sorted(set(llm.budgets)), [sentinel],
                         "有调用点没按配置预算走：%r" % (sorted(set(llm.budgets)),))

    def test_no_call_clamps_the_budget_in_source(self):
        """源码层面：不许对预算做夹取（`max_tokens=min(...)` 这类写法）。

        运行时哨兵只能覆盖跑到的路；这条扫全部调用点，新写的夹取当场就会被拦。
        预算只有一个来源——配置项 `llm.max_tokens`。
        """
        with open(S.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotRegex(src, r"max_tokens\s*=\s*(?:min|max)\(",
                            "有调用点对配置预算做了夹取，预算必须原样取配置值")

    def test_bad_plan_falls_back_to_gist_topics(self):
        """规划反复不过：题目按各段首节的凝缩主旨代填，**段边界与配额一个字不动**。

        段边界与配额是装箱算好的，不挂在模型自觉上——所以这一轮失败不会改变
        写几段、每段多少字，只是段题目少了一句人话。
        """
        bad = json.dumps({"title": "t", "topics": ["只给了一段"]},
                         ensure_ascii=False)
        # 兜底后两段配额 235/165、容差 ±60：240/150 字各一版入格，各一段调用。
        llm = _SegmentedLLM([bad] * (S.PLAN_RETRIES + 1),
                            [_seg_payload(8), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        logs = []
        title, plan, script, _segs = S._generate_segmented(
            llm, cfg, "素" * 100, _evidence(), card, "argument",
            None, 400, logs.append, None, None)
        self.assertEqual(llm.plan_calls, S.PLAN_RETRIES + 1, "规划打回到上限")
        self.assertTrue(any("凝缩主旨代填" in m for m in logs), "代填要留痕")
        self.assertEqual(llm.segment_calls, 2, "段数由装箱定，规划失败也不改")
        self.assertEqual(len(script), 13, "两段 8+5 句拼成整篇")

    def test_generate_routes_through_segments_for_mapped_project(self):
        # 路线判据是项目模式，不是配置开关：mapped（成稿规划）走分段。
        llm = _SegmentedLLM([_plan_payload("分段标题", n=2)],
                            [_seg_payload(3)] * 6,
                            content_replies=[SEM_PASS] * 3)
        res = S.generate("素" * 200, self._cfg(), llm, log=lambda m: None,
                         project={"title": "书名", "gist": "总主旨",
                                  "planned_episodes": 3},
                         evidence=_evidence(), segmented=True)
        self.assertEqual(llm.script_calls, 0, "成稿规划不该走整篇生成")
        self.assertEqual(llm.plan_calls, 1)
        self.assertGreaterEqual(llm.segment_calls, 2)
        self.assertGreaterEqual(llm.content_calls, 1, "全篇门禁与内容检照旧跑")
        self.assertEqual(res["title"], "书名", "地图已定的标题以人为准")
        self.assertTrue(res["script"])
        self.assertEqual(res["planned_episodes"], 3)

    def test_segment_drafts_are_not_glued(self):
        """段间落盘落的是**半期正文**，一句片头尾都不带。

        粘上就成了「半期 + 片头 + 片尾」：片尾已经挂在前半期的末尾了，内容却只写
        了一半——形态像一整期，其实是残缺。真被中止时交到手上的就是这样一份。
        片头尾只在整期拼完、走完门禁与定点修补之后粘一次（见
        `test_whole_episode_glues_the_fixed_lines_exactly_once`）。
        """
        seen = []
        llm = _SegmentedLLM([_plan_payload(n=2)], [_seg_payload(8), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        S._generate_segmented(llm, cfg, "素" * 100, _evidence(), card,
                              "argument", None, 400, lambda m: None, None,
                              seen.append)
        self.assertEqual(len(seen), 2, "每段写完各落一次盘")
        for i, g in enumerate(seen, 1):
            texts = [l["text"] for l in g["script"]]
            with self.subTest(seg=i):
                self.assertNotIn("欢迎收听", texts[0], "第 %d 段草稿不该带片头" % i)
                self.assertNotIn("欢迎关注", texts[-1], "第 %d 段草稿不该带片尾" % i)
                self.assertTrue(g["report"]["partial"], "半期草稿要标出来")

    def test_whole_episode_glues_the_fixed_lines_exactly_once(self):
        """整期拼完只粘一次：首 2 句片头、尾 1 句片尾，全篇没有第二套。

        从前段间落盘也粘，一期按段数粘好几遍。这里盯的是「只有一套」——
        按句数对得上不足以证明，正文凑巧提到同样的字也照样数得对。
        """
        llm = _SegmentedLLM([_plan_payload("分段标题", n=2)],
                            [_seg_payload(3)] * 6,
                            content_replies=[SEM_PASS] * 3)
        res = S.generate("素" * 200, self._cfg(), llm, log=lambda m: None,
                         project={"title": "书名", "gist": "总主旨",
                                  "planned_episodes": 3},
                         evidence=_evidence(), segmented=True)
        texts = [l["text"] for l in res["script"]]
        self.assertIn("欢迎收听", texts[0], "首句是片头第 1 句")
        self.assertIn("本期讲述", texts[1], "第 2 句是片头第 2 句")
        self.assertIn("欢迎关注", texts[-1], "末句是片尾")
        self.assertEqual(sum("欢迎收听" in t for t in texts), 1,
                         "片头只该有一套：段间不该另粘")
        self.assertEqual(sum("欢迎关注" in t for t in texts), 1,
                         "片尾只该有一套：段间不该另粘")
        self.assertEqual(len(texts), len(res["script"]))
        self.assertTrue(llm.segment_calls >= 2, "这一段确实分了两段以上")

    def test_generate_falls_back_when_no_sections(self):
        # mapped 但本期凝缩缺失（还没排图、版本对不上）：退回整篇照常出稿，
        # 不让人卡在「一步跑不动」上。
        llm = _SegmentedLLM([], [], script_payloads=[_whole_script_payload()])
        cfg = self._cfg()
        res = S.generate("素" * 200, cfg, llm, log=lambda m: None,
                         evidence={"gist": "", "points": [], "sections": []},
                         segmented=True)
        self.assertEqual(llm.plan_calls, 0, "没有凝缩就不该开规划")
        self.assertEqual(llm.segment_calls, 0)
        self.assertEqual(llm.script_calls, 1, "退回整篇一路照常出稿")
        self.assertTrue(res["script"])


class TestDedupe(unittest.TestCase):
    """程序硬去重：模型凑数时把写过的整段再背一遍，那不是改写，是复播。

    口径只有一条：归一化后完全相同的长句只留第一次。短句不删（重复本就是
    口语的样子），措辞不同不删（那是改写，该由内容检判，程序不越权删）。
    """

    def _lines(self, texts):
        return [{"speaker": "A", "text": t, "emotion": "平静"} for t in texts]

    def test_drops_repeated_long_lines_keeping_first(self):
        long_a = "人得守住最后一道门，判断的让渡与翻译的打通才是核心"
        long_b = "这一句是别的内容，与上面那句并不相同，用做对照组"
        out, dropped = S.dedupe_script(
            self._lines([long_a, long_b, long_a, long_a]))
        self.assertEqual(dropped, 2)
        self.assertEqual([l["text"] for l in out], [long_a, long_b])

    def test_short_lines_may_repeat(self):
        """「对吧」这类短句重复是口语形态，删掉会把正常问答删成残句。"""
        out, dropped = S.dedupe_script(
            self._lines(["对吧？", "接着说下一件事", "对吧？"]))
        self.assertEqual(dropped, 0)
        self.assertEqual(len(out), 3)

    def test_punctuation_difference_is_still_the_same_line(self):
        """背一段话时标点未必逐字一致：标点差异不该让它逃过判重。"""
        out, dropped = S.dedupe_script(self._lines([
            "人得守住最后一道门，判断的让渡与翻译的打通才是核心",
            "人得守住最后一道门。判断的让渡与翻译的打通才是核心"]))
        self.assertEqual(dropped, 1)
        self.assertEqual(len(out), 1)


class TestEpisodeBlockCarriesMaterialSize(unittest.TestCase):
    """本期计划块必须说出「本期素材有多少字」。

    从前只说「要写多少」、不说「手里有多少」——素材撑不满目标字数时，模型
    唯一的出路就是凑。把数字摆出来，「照实写短」才是一句它能执行的话，而
    不是一句漂亮的禁令。
    """

    def test_block_states_material_chars(self):
        block = S._episode_block({"title": "第一期", "chars": 4132})
        self.assertIn("4132 字", block)
        self.assertIn("照实写短", block)
        self.assertIn("重复的内容会被程序删除", block)

    def test_block_absent_when_nothing_known(self):
        self.assertEqual(S._episode_block({}), "")
        self.assertEqual(S._episode_block(None), "")


class TestFieldContractInPrompts(unittest.TestCase):
    """字段契约是前置规范，不是后置补救。

    v0.11.1 事故：分段 prompt 没说清「哪个字段装什么」，模型把 emotion
    标签词整批拼进 text 开头念了出来。emotion 字段删除后契约收窄成两个字段，
    但「每个字段只装它自己的东西 + 正反例」的契约块必须在两份 prompt
    （整篇 / 分段）里同时在场——缺一处，那条路径就退回裸奔。
    """

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}

    CONTRACT_MARKS = (
        "每个字段只装它自己的东西",
        "emotion：只填语篇标签",
        "text：只填对话内容",
        '正确：{"speaker": "A", "emotion": "追问"',
        '错误：{"speaker": "A", "emotion": "追问", "text": "小美疑惑地说：',
    )

    def test_main_prompt_carries_field_contract(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        for mark in self.CONTRACT_MARKS:
            self.assertIn(mark, p, "整篇 prompt 缺字段契约要素：%s" % mark)

    def test_segment_prompt_carries_field_contract(self):
        p = S._segment_system_prompt(self.CFG, PG.get("paper"),
                                     "argument", 1, 4, 900, "测试主题",
                                     is_first=True)
        for mark in self.CONTRACT_MARKS:
            self.assertIn(mark, p, "分段 prompt 缺字段契约要素：%s" % mark)

    def test_segment_prompt_forbids_restating_inside_the_segment(self):
        """段系统提示词必须写明「本段内同一件事只说一遍」，且带正反例。

        v0.23.0 复盘第 2 期段 2 复读 64 句：禁重复的条款全挂在 user 侧的
        「已写正文」栏与本期计划块上，两处都只覆盖**前文**；一处正文在一个
        段内把同一件事讲两遍，五条一条都管不到。判据与补字提示词同源——
        「并排放进听众耳朵里听一遍」，两处一个口径。
        """
        p = S._segment_system_prompt(self.CFG, PG.get("paper"),
                                     "argument", 2, 4, 900, "测试主题",
                                     is_first=False)
        self.assertIn("本段自己内部，同一件事只说一遍", p)
        self.assertIn("【这些算重复】", p, "只写名词不行，必须给反例")
        self.assertIn("【这些不算重复】", p, "只写名词不行，必须给正例")
        self.assertIn("并排放进听众耳朵里听一遍", p, "判据须与补字提示词同源")
        self.assertIn("段内复读不允许", p)
        self.assertNotIn("字数不达标可以接受", p,
                         "「不达标可接受」是被实证的 44% 缺口出口，不得回潮")
        self.assertNotIn("宁可这一段字数少一点", p)
        self.assertIn("字数够不够由程序核账", p,
                      "删掉出口后要指路：字数归程序管，模型只管不复读")

    def test_contract_avoids_mood_wording_in_none_level(self):
        """契约块措辞不得与 paper 档「不写心情」的纪律打架。"""
        p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                  paradigm=PG.get("paper"))
        self.assertNotIn("情绪标签", p)


class TestSegmentAccountingUnit(unittest.TestCase):
    """核账口径 = 有效字，与配额同一把尺（v0.13.0「账本口径只有字」的唯一定义）。

    配额出自 duration_model（有效字：汉字 1 / 标点 0.5 / 西文词 1.5），段核账
    从前拿 len() 数字符去减它——两种尺相减，标点越多缺口虚得越大。
    """

    def test_accounting_counts_effective_chars_not_raw_chars(self):
        text = "你好，世界。"          # len=6；有效字=汉字4×1+标点2×0.5=5
        self.assertAlmostEqual(
            S.duration_model.effective_chars(text), 5.0)
        self.assertNotEqual(len(text), 5)


class TestSegmentUsageAndWindow(unittest.TestCase):
    """段提示词的【本段用量】+【验收窗口】（v0.26.3）。

    背景：探针实测配 3444 有效字交 46.3%，模型踩着「字数不达标可以接受」
    这句合法出口在素材点讲完的地方收工。修法：删出口、给真实尺度感
    （素材÷配额，py 现算，两边同尺）、给程序真实收稿区间。
    """

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}
    EVIDENCE = {"sections": [{"anchor": "第一节", "gist": "概要",
                              "points": ["要点一"]}]}

    def _user(self, fit="字" * 100):
        return S._segment_user_prompt(
            {"project": {"name": "示例节目"}}, self.EVIDENCE,
            {"sections": [1]}, 3444, 0, 6585, fit, "", "")

    def test_usage_row_present_with_ratio_and_direction(self):
        """素材富余时给压比与方向句，方向句到「细节取用密度要够」为止。"""
        p = self._user(fit="字" * 3444 * 3)   # 3 倍富余
        self.assertIn("【本段用量】", p)
        self.assertIn("素材约为配额的 3.0 倍", p)
        self.assertIn("从素材选料铺满配额", p)
        self.assertIn("细节取用密度要够", p)
        self.assertNotIn("硬性要求", p,
                         "口号会被读成「可以硬凑」，诱发复读；压不压得满由核账兜底")

    def test_usage_row_points_to_expand_when_material_thin(self):
        """素材比目标少时方向翻转：靠展开撑满，不许编造。"""
        p = self._user(fit="字" * 1722)      # 约 0.5 倍
        self.assertIn("素材约为配额的 0.5 倍", p)
        self.assertIn("靠追问与展开", p)
        self.assertIn("不编造素材之外的事实", p)
        self.assertNotIn("从素材选料铺满配额", p)

    def test_usage_row_absent_without_material(self):
        """素材没喂就不给用量行——假锚比没锚更坏；验收窗口照给。"""
        p = self._user(fit="")
        self.assertNotIn("【本段用量】", p)
        self.assertIn("【验收窗口】", p)

    def test_window_matches_accounting_tolerance(self):
        """提示词报的窗口必须与程序核账同一把尺：±max(15%, 60)。"""
        p = self._user()
        tol = max(int(3444 * S.SEGMENT_TOL_FRAC), S.SEGMENT_TOL_MIN_CHARS)
        self.assertIn("【验收窗口】程序按 %d~%d 字收稿" % (3444 - tol, 3444 + tol), p)

    def test_insert_and_trim_prompts_carry_window(self):
        """补写/压紧轮也要给验收区间——模型得知道改到哪算过关。"""
        seg = [{"speaker": "A", "text": "字" * 30}]
        quota, need = 1839, 628
        tol = max(int(quota * S.SEGMENT_TOL_FRAC), S.SEGMENT_TOL_MIN_CHARS)
        low, high = quota - tol, quota + tol
        pi = S.build_insert_prompt(seg, need, quota, 57, material="素材正文")
        self.assertIn("验收区间 %d~%d 字" % (low, high), pi)
        self.assertIn("还差 %d 字" % need, pi)
        pt = S.build_trim_prompt(seg, 261, quota, 0)
        self.assertIn("验收区间 %d~%d 字" % (low, high), pt)
        self.assertIn("把字数减进验收区间", pt)

    def test_window_helper_single_source(self):
        """_segment_window 是容差的唯一出口：核账与提示词同源。"""
        low, high = S._segment_window(100)
        self.assertEqual((low, high), (40, 160))   # tol = max(15, 60) = 60


class TestMergeTinySegments(unittest.TestCase):
    """碎段合并（v0.26.4）：配额低于阈值的逻辑段并进配额较小的相邻段。

    阈值 = script.segment_min_sents × 期望句长，全部现算不写死；合并走在
    装箱之前，装箱按容量再切是兜底。
    """

    @staticmethod
    def _secs(chars):
        return [{"anchor": "第%d节" % (i + 1), "gist": "主旨", "points": [],
                 "chars": c} for i, c in enumerate(chars)]

    CFG = {}   # gate 缺省 8/40 → 期望句长 24；软句数缺省 15 → 阈值 360

    def test_merges_into_lighter_neighbor_then_chains(self):
        """小段并入较轻的邻居；合并后仍低于阈值就继续并（链式）。"""
        # 权重 600/600/30/30、目标 1000 → 配额约 476/476/24/24
        secs = self._secs([600, 600, 30, 30])
        out = S._merge_tiny_groups(secs, [[1], [2], [3], [4]], 1000, self.CFG)
        self.assertEqual(out, [[1], [2, 3, 4]],
                         "段3 先并更轻的段4，链式继续并进段2")

    def test_all_tiny_collapses_into_one(self):
        """全篇都碎就并成一段——全篇目标本来就是几千字量级，方向没错。"""
        secs = self._secs([100, 100, 100])
        out = S._merge_tiny_groups(secs, [[1], [2], [3]], 300, self.CFG)
        self.assertEqual(out, [[1, 2, 3]])

    def test_no_merge_when_all_above_threshold(self):
        """没有碎段就不动分组，一个节号都不许重排。"""
        secs = self._secs([400, 400])
        out = S._merge_tiny_groups(secs, [[1], [2]], 4000, self.CFG)
        self.assertEqual(out, [[1], [2]])

    def test_threshold_follows_config_not_hardcoded(self):
        """阈值随配置变：期望句长改大，原本达标的段也变碎段。"""
        secs = self._secs([500, 500])
        base = [[1], [2]]
        # 配额约 500/500：默认阈值 360 不并
        self.assertEqual(
            S._merge_tiny_groups(secs, base, 1000, self.CFG), base)
        # gate.max_chars 40→200 → 期望句长 104 → 阈值 1560 → 并
        self.assertEqual(
            S._merge_tiny_groups(secs, base, 1000,
                                 {"gate.max_chars": 200}), [[1, 2]])

    def test_soft_line_guide_reads_config_key(self):
        """软句数下限走 script.segment_min_sents，与合并阈值同源。"""
        cfg = {"script.segment_min_sents": 30}
        self.assertEqual(S._soft_line_guide(cfg, 100), 30)
        self.assertEqual(S._soft_line_guide({}, 100), 15, "缺配置回兜底默认")

    def test_config_default_is_15(self):
        self.assertEqual(
            ConfigManager().data()["script.segment_min_sents"], 15)

    def test_generation_merges_before_planning(self):
        """整链路：合并后段数变了，规划轮的题目条数必须跟着对上。"""
        llm = _SegmentedLLM([_plan_payload(n=1)], [_seg_payload(8)])
        cfg = ConfigManager().data()
        cfg.update({"script.gate_strict": False,
                    "gate.min_deviation_seconds": 9999,
                    "project.program_name": "播客"})
        card = S.resolve_paradigm(None, cfg)
        logs = []
        title, plan, script, _segs = S._generate_segmented(
            llm, cfg, "素" * 100, _evidence(), card, "argument",
            {"planned_episodes": 5}, 400, logs.append, None, None)
        self.assertEqual(llm.segment_calls, 1,
                         "两节都低于阈值 360，合并后只该有一次段调用")
        self.assertTrue(any("碎段合并" in m for m in logs),
                        "合并发生时必须留一行日志")
        self.assertTrue(script)



class TestShapeFlags(unittest.TestCase):
    """取材标记：形状扫描、展示拼接、素材账零污染。"""

    @classmethod
    def setUpClass(cls):
        cls.cfg = ConfigManager().data()

    def test_each_family_hits(self):
        """八家族各按形状命中；正文干净就不误报。"""
        cases = {
            "网址": "看 https://mirrors.aliyun.com/pypi 或 tuna.tsinghua.edu.cn，"
                    "联系 [email-redacted]",
            "公式": "损失 $L=\\frac{1}{n}$，取 α 小、β 大，∑ 求和即可。",
            "代码": "```py\ndef a():\n    return 1\n```\n再 import os 即可",
            "表格": "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |",
            "路径": "配置在 C:\\app\\cfg.yaml，也可能在 /usr/local/etc 下，"
                    "├── src 与 └── docs 是两个目录",
            "参考文献": "见[1]、doi.org/10.1/xyz、[2] 与 [3]。",
            "标记语言": "<div>甲</div><br><p>乙</p>",
            "日志行": "[10:42:08] 开始\n[10:47:40] 完成\n[10:51:43] 落盘",
        }
        for name, text in cases.items():
            self.assertIn(name, S._shape_flags(text, self.cfg),
                          "家族 %s 应命中" % name)
        clean = ("semantic-split 的管道只有十道门禁，任何一道不过整条管道直接停，"
                 "三条管线分工明确，缺了嵌入模型会自动退化成纯规则模式。")
        self.assertEqual(S._shape_flags(clean, self.cfg), [])

    def test_title_wordlist_flags_listy_section(self):
        """节标题命中词表 → 清单类；大小写不敏感。"""
        self.assertIn("清单", S._shape_flags(
            "去官网下载即可", self.cfg, anchor="02｜国内推荐（阿里云镜像）"))
        self.assertIn("清单", S._shape_flags("正文", self.cfg, anchor="LICENSE"))
        self.assertEqual(S._shape_flags("正文", self.cfg, anchor="架构总览"), [])

    def test_master_switch_off(self):
        """总开关关掉 → 一律空表，一切照旧。"""
        cfg = dict(self.cfg)
        cfg["script.shape_flags"] = False
        self.assertEqual(S._shape_flags(
            "https://a.com https://b.com https://c.com", cfg), [])

    def test_note_row_lists_only_hit_families(self):
        """标记行只写命中的家族，指导句逐条对应。"""
        row = S._sec_note_text(["清单", "网址"])
        self.assertIn("清单、网址", row)
        self.assertIn("不要逐条念", row)
        self.assertIn("不进台词", row)
        self.assertEqual(S._sec_note_text([]), "")

    def test_notes_inserted_after_sec_header_only(self):
        """标记插在命中节的标签行之后；未命中节一根不插。"""
        mat = ("【第 1 节《概览》】\n正文甲\n\n"
               "【第 2 节《镜像》】\n正文乙")
        noted = S._apply_sec_notes(mat, {2: ["清单"]})
        self.assertNotIn("【※取材注意】", noted.split("【第 2 节")[0])
        self.assertIn("【第 2 节《镜像》】\n【※取材注意】", noted)
        # 无命中：原串返回，一个字不动。
        self.assertIs(S._apply_sec_notes(mat, {}), mat)

    def test_notes_skipped_when_no_sec_headers(self):
        """老路素材（无节标签行）不插标记——宁可缺标记，不造假位置。"""
        mat = "整期素材，没有任何节标签。"
        self.assertIs(S._apply_sec_notes(mat, {1: ["网址"]}), mat)

    def test_prompt_shows_noted_but_ratio_eats_raw(self):
        """展示吃 noted 版；压比吃 fit 原版——标记行不进素材账。"""
        ev = {"sections": [{"anchor": "第一节", "gist": "概要",
                            "points": ["要点一"]}]}
        fit = "素材正文" * 500
        noted = fit + "\n【※取材注意】本节含：网址。"
        p = S._segment_user_prompt({"project": {"name": "x"}}, ev,
                                   {"sections": [1]}, 3444, 0, 6585,
                                   fit, "", "", pieces=[{"sec": 1}],
                                   noted_fit=noted)
        self.assertIn("【※取材注意】", p)                    # 展示带标记
        self.assertIn("素材约 %d 字" % int(round(
            S.duration_model.effective_chars(fit))), p)      # 压比按原版
        self.assertNotIn("素材约 %d 字" % int(round(
            S.duration_model.effective_chars(noted))), p)    # 不吃标记后的
        lines = [{"speaker": "A", "text": "甲" * 10}]
        self.assertIn("【※取材注意】",
                      S.build_insert_prompt(lines, 100, 600, 5,
                                            material=fit,
                                            noted_material=noted))
        self.assertNotIn("【※取材注意】",
                         S.build_insert_prompt(lines, 100, 600, 5,
                                               material=fit))

    def test_generate_passthrough_defaults_off(self):
        """generate / _generate_segmented 不传 sec_flags 时行为与从前一致。"""
        import inspect
        for fn in (S.generate, S._generate_segmented):
            sig = inspect.signature(fn)
            self.assertIn("sec_flags", sig.parameters)
            self.assertIsNone(sig.parameters["sec_flags"].default)


class TestEpisodeFormIsRecorded(unittest.TestCase):
    """对话形式跟着**期**走：生成时用的那一种记在这一期上，判稿读本期自己那份。

    从前只有一份全局配置：写第 3 期时选「捧哏」、写第 4 期时改成「追问深挖」，
    回头重判第 3 期读的是「追问深挖」——而连说上限挂在形式上头，同一份稿子被
    两把尺子量，一会儿过一会儿不过。更要紧的是，配置里那个值的语义是「下一次
    生成用哪种」，不是「这一期当初用哪种」，两件事共用一个字段本来就不该。
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="pm-form-")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "脚本"), exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _form_path(self, no):
        from podcast_maker import layout
        return layout.form_file(self.root, no)

    def test_the_sidecar_sits_next_to_the_script(self):
        """与正文同目录、同号、另一个后缀——正文那份 json 一个字段都不加。"""
        from podcast_maker import layout
        f = layout.form_file(self.root, "3")
        s = layout.script_file(self.root, "3")
        self.assertEqual(os.path.dirname(f), os.path.dirname(s))
        self.assertEqual(os.path.basename(f), "3.form.json")
        self.assertNotEqual(f, s)
        self.assertNotEqual(f, layout.plan_file(self.root, "3"))

    def test_what_was_written_is_what_is_read(self):
        from podcast_maker import layout, pipeline as PL
        self.assertTrue(PL.write_form_file(self.root, "3", "anchor"))
        self.assertEqual(PL.episode_form(self.root, "3"), "anchor")
        self.assertEqual(PL.read_json(layout.form_file(self.root, "3"), {})
                         .get("form"), "anchor")

    def test_an_empty_form_is_not_written_at_all(self):
        """空值不落盘：写一份空的，读的那头就分不清「记的就是空」和「没记过」。"""
        from podcast_maker import pipeline as PL
        for blank in ("", "   ", None):
            self.assertFalse(PL.write_form_file(self.root, "5", blank))
        self.assertFalse(os.path.exists(self._form_path("5")))

    def test_a_missing_or_broken_sidecar_reads_as_no_record(self):
        """没有 / 读坏 / 结构不对，一律返回空串——**这一层不许兜底**。"""
        from podcast_maker import pipeline as PL
        self.assertEqual(PL.episode_form(self.root, "7"), "")
        PL.write_json(self._form_path("7"), ["不是", "字典"])
        self.assertEqual(PL.episode_form(self.root, "7"), "")
        PL.write_json(self._form_path("7"), {"episode_no": "7"})
        self.assertEqual(PL.episode_form(self.root, "7"), "")
        with open(self._form_path("7"), "w", encoding="utf-8") as fh:
            fh.write("{ 坏掉的 json")
        self.assertEqual(PL.episode_form(self.root, "7"), "")

    def test_the_recorded_form_beats_the_current_config(self):
        """判稿这一路：本期有记录就用记录，配置只是「下一次生成用哪种」。"""
        from podcast_maker import pipeline as PL
        from podcast_maker.config_manager import ConfigManager
        cfg = ConfigManager().data()
        cfg["script.dialogue_form"] = "drill"          # 此刻配置里是「追问深挖」
        PL.write_form_file(self.root, "3", "anchor")   # 但第 3 期当初用的是「捧哏」
        recorded = PL.episode_form(self.root, "3")
        if recorded:
            cfg["script.dialogue_form"] = recorded
        self.assertEqual(cfg["script.dialogue_form"], "anchor")
        # 没有记录的期不被动：配置照旧
        self.assertEqual(PL.episode_form(self.root, "4"), "")

    def test_generation_writes_it_and_the_gate_reads_it(self):
        """两处接线。判稿那一路必须真把它盖上去，不能取了不用、也不能白写。"""
        import io as _io
        import podcast_maker.web_ui as W
        src = _io.open(W.__file__, encoding="utf-8").read()
        self.assertIn("pipeline.write_form_file(", src)      # 生成落盘时写
        self.assertIn("pipeline.episode_form(", src)         # 判稿时读
        self.assertIn('cfg["script.dialogue_form"] = recorded', src)
        # 前端得把期号报上去，否则服务端不知道读哪一期
        self.assertIn("episode_no:(LOADED_EP||picked('s')[0]||'')", src)

    def test_the_form_is_resolved_from_the_config_not_re_read(self):
        """写下来的是**解析后的结果**（配置选的 > 卡上默认），不是配置里的原字。"""
        from podcast_maker import paradigms as _PG
        from podcast_maker import script_engine as _S
        card = _PG.get("methodology")
        # 留空 → 用卡上的默认形式；选了 → 用选的。两者都写进旁挂档。
        self.assertEqual(_PG.resolve_form(card, ""), card.get("form") or "qa")
        self.assertEqual(_PG.resolve_form(card, "anchor"), "anchor")
        self.assertEqual(_S._form_key(card, {"script.dialogue_form": "chat"}), "chat")


if __name__ == "__main__":
    unittest.main()
