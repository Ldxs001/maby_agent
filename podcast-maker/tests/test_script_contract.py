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

"""脚本契约：标题随脚本产出，期数要么人定要么模型定。

第七类缺陷：产物里的标题不由脚本产出，于是「脚本页」拿到了稿子却没有名字，
「合成页」反过来还要人再输一次标题。同一个字段两处来源，谁是准的说不清，
人输的那份改错了也没人拦。所以顶层契约必须是「脚本自带标题」，合成页只做修改。
"""

import json
import os
import sys
import unittest
from unittest import mock

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
                    "emotion": "平稳", "text": "第 %d 句台词内容够长够取标题" % i})
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
               '"lines": [{"speaker":"A","emotion":"平稳","text":"甲"}]}')
        got = S.parse_script(raw)
        self.assertEqual(got["title"], "链与两头")
        self.assertEqual(got["planned_episodes"], 12)
        self.assertEqual(len(got["lines"]), 1)

    def test_legacy_array_form_still_parses_without_title(self):
        raw = '[{"speaker":"A","emotion":"平稳","text":"甲"},' \
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

    def test_prompt_states_the_same_fixed_lines_the_code_will_write(self):
        # 提示词里写的固定句必须与代码写死的那句一模一样，否则模型会围着
        # 一个别的开场白写正文，衔接不上
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        self.assertIn("欢迎收听《示例节目》", p)
        self.assertIn("这里是《示例节目》", p)

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


class TestIntroOutroPinnedByCode(unittest.TestCase):
    """片头尾是固定标识，必须由代码写死，不能靠模型复述。

    交给模型的下场是措辞每期漂移，然后拿 fail 级门禁去验，验不过就重试——
    把一次纯格式问题烧成三倍时间和三倍 token，最后仍然可能整片卡死。
    """

    def cfg(self, **over):
        data = ConfigManager().data()
        data.update(over)
        return data

    def body(self, n=12):
        return [{"speaker": "A" if i % 2 == 0 else "B",
                 "emotion": "平静",
                 "text": "第 %d 句正文内容示例文字" % i} for i in range(n)]

    def test_template_is_written_verbatim(self):
        script = self.body()
        cfg = self.cfg(**{"project.program_name": "播客"})
        n = S.pin_intro_outro(script, cfg)
        self.assertEqual(n, 2)
        self.assertEqual(script[0]["text"],
                         "欢迎收听《播客》，面向关注方法论与认知边界的听众。")
        self.assertEqual(script[-1]["text"], "这里是《播客》，欢迎关注。")

    def test_second_pass_changes_nothing(self):
        cfg = self.cfg(**{"project.program_name": "播客"})
        script = self.body()
        S.pin_intro_outro(script, cfg)
        self.assertEqual(S.pin_intro_outro(script, cfg), 0, "写死必须是幂等的")

    def test_intro_outro_speakers_are_pinned_too(self):
        """首句 A、末句 B 也属固定结构：交替判据放宽后，只写文本会漏掉这一半。"""
        cfg = self.cfg(**{"project.program_name": "播客"})
        script = self.body()
        script[0]["speaker"] = "B"
        script[-1]["speaker"] = "A"
        S.pin_intro_outro(script, cfg)
        self.assertEqual(script[0]["speaker"], "A")
        self.assertEqual(script[-1]["speaker"], "B")

    def test_gate_passes_after_pinning(self):
        cfg = self.cfg(**{"project.program_name": "播客"})
        script = self.body()
        S.pin_intro_outro(script, cfg)
        self.assertEqual(S.intro_outro_ok(script, cfg), (True, True))

    def test_gate_still_catches_a_mangled_script(self):
        # 定点写死之后这条门禁不会天天响，但结构性走样仍要能发现
        cfg = self.cfg(**{"project.program_name": "播客"})
        script = self.body()
        self.assertEqual(S.intro_outro_ok(script, cfg), (False, False))

    def test_single_line_script_does_not_self_overwrite(self):
        script = [{"speaker": "A", "emotion": "平静", "text": "只有一句话的脚本"}]
        S.pin_intro_outro(script, self.cfg(**{"project.program_name": "播客"}))
        self.assertIn("欢迎收听", script[0]["text"])
        self.assertNotIn("欢迎关注", script[0]["text"])

    def test_blank_program_name_falls_back_instead_of_failing(self):
        # 节目名留空时若原样使用空串，片头尾判据恒为假，看起来像模型的错
        cfg = self.cfg(**{"project.program_name": ""})
        self.assertEqual(S.program_name_of(cfg), "播客")
        script = self.body()
        S.pin_intro_outro(script, cfg)
        self.assertEqual(S.intro_outro_ok(script, cfg), (True, True))

    def test_overlong_program_name_reports_itself(self):
        # 写死的句子自己超限时，报「节目名太长」而不是让它伪装成模型缺陷
        cfg = self.cfg(**{"project.program_name": "名" * 40})
        with self.assertRaises(ScriptError) as ctx:
            S.pin_intro_outro(self.body(), cfg)
        self.assertIn("节目名", str(ctx.exception))

    def test_preset_switches_the_template(self):
        cfg = self.cfg(**{"project.program_name": "播客",
                          "intro_outro.preset": "brief"})
        script = self.body()
        S.pin_intro_outro(script, cfg)
        self.assertEqual(script[0]["text"], "欢迎收听《播客》。")


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
                         "project.program_name": "播客"})

    def payload(self, plan=7):
        lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平静",
                  "text": "第 %d 句正文内容示例文字" % i} for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": plan,
                           "lines": lines}, ensure_ascii=False)

    def test_model_text_cannot_break_the_fixed_lines(self):
        # 模型把首尾写成什么样都不影响结果——这正是「写死」的意思
        bad = json.loads(self.payload())
        bad["lines"][0]["text"] = "模型自己编的开场白，完全没照模板"
        bad["lines"][-1]["text"] = "模型自己编的结束语，同样没照模板"
        gen = S.generate("素材内容示例", self.cfg,
                         FakeLLM(json.dumps(bad, ensure_ascii=False)))
        self.assertEqual(gen["script"][0]["text"],
                         "欢迎收听《播客》，面向关注方法论与认知边界的听众。")
        self.assertEqual(gen["script"][-1]["text"], "这里是《播客》，欢迎关注。")
        self.assertTrue(gen["report"]["passed"], "写死之后不该再有片头尾失败项")

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
                  "text": "第 %d 句正文内容示例文字" % i} for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": 7,
                           "lines": lines}, ensure_ascii=False)

    def test_stop_keeps_the_round_already_written(self):
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8)
        logs = []
        gen = S.generate("素材内容示例", self.cfg, llm, log=logs.append,
                         should_stop=lambda: True)
        self.assertEqual(len(llm.users), 1, "叫停之后不该再开新一轮")
        self.assertEqual(llm.patch_calls, 0, "叫停之后也不该再开一轮定点修补")
        self.assertEqual(len(gen["script"]), 12, "叫停时要交回上一轮的完整稿子")
        self.assertTrue(any("收到中止" in m for m in logs), logs)

    def test_without_stop_it_uses_the_whole_round_budget(self):
        """对照组：没人叫停就把轮次预算用完，免得"少开一轮"变成一直只跑一轮。

        预算用完不等于整篇重出四遍——只有第 1 轮整篇写，之后每轮都只是定点修补。
        """
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容"}]}'] * 8)
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
                  "text": "%s第 %d 句正文内容示例文字" % (tag, i)}
                 for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": 7,
                           "lines": lines}, ensure_ascii=False)

    def test_failed_semantic_is_fixed_pointwise(self):
        """内容检给了句号 → 只改那几句，不再整篇重出。"""
        llm = ScriptedLLM(
            self._payload(), [SEM_FAIL_AT_3, SEM_PASS],
            patch_replies=['{"edits": [{"index": 3, "text": '
                           '"这一句改成了不含数字的说法示例"}]}'])
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
                           '"这一句改成了不含数字的说法示例"}]}'])
        gen = S.generate("素材内容示例", self.cfg, llm)
        keys = [i["key"] for i in gen["report"]["items"]]
        self.assertIn("check_promise", keys, "承诺链检没重判，但结论必须留在报告里")

    def test_located_problem_still_unfixed_after_rounds_exhausted(self):
        """一直编造就一直是 fail：重试耗尽后报告不许「通过」。"""
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容"}]}'] * 8)
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

    def test_card_gives_cast_emotion_and_run_limit(self):
        card = PG.get("methodology")
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn(card["cast"], p)
        self.assertIn(card["emotion"], p)
        self.assertIn("同一人连续句数上限**：%d 句" % card["max_run"], p)

    def test_none_level_lists_only_discourse_tags(self):
        """不写心情的档位：枚举里不该出现心情词，否则模型照着枚举填，
        门禁又要一条条抓——两头打架，白跑几轮定点修补。"""
        card = PG.get("paper")
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn("不写心情", p)
        for w in ("感慨", "恍然", "好奇", "疑惑"):
            self.assertNotIn(w, p, "none 档提示词里出现了心情词「%s」" % w)

    def test_light_level_keeps_the_mood_words(self):
        card = PG.get("narrative")
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn("略带", p)
        self.assertIn("感慨", p)

    def test_prompt_no_longer_demands_strict_alternation(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        self.assertNotIn("严格交替", p)
        self.assertIn("不许拆给两个人", p)

    def test_run_limit_in_the_prompt_is_the_one_the_gate_uses(self):
        card = PG.get("interview")
        p = S.build_system_prompt(self.CFG, "argument", 900, 24, paradigm=card)
        self.assertIn("：%d 句" % card["max_run"], p)
        self.assertEqual(PG.max_run_of(card), card["max_run"])

    def test_card_without_cast_falls_back_to_the_builtin_hosts(self):
        card = dict(PG.get("auto"), cast="", emotion="")
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


class TestEmotionDensityFollowsTheLevel(unittest.TestCase):
    """情绪密度这一维按档位换话术：卡定天花板，密度定天花板里怎么分布。

    从前两处各说各的——卡上的档位说「这批不写心情……不填任何心情词」，风格倾向那一
    行不论档位都说「情绪标签适度起伏，「平静」类约占一半」。同一个字段上一边禁止、
    一边要求，模型只能二选一，档位等于作废。所以话术按档位分成两套，且不许在一份
    提示词里同时出现。
    """

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}

    @staticmethod
    def _density_rows(prompt):
        return [ln for ln in prompt.splitlines()
                if ln.startswith("- 标签分布：") or ln.startswith("- 情绪密度：")]

    def test_none_level_talks_about_tags_not_mood(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                  paradigm=PG.get("paper"))
        rows = self._density_rows(p)
        self.assertEqual(len(rows), 1, "none 档该有一行密度要求，且只有一行")
        self.assertTrue(rows[0].startswith("- 标签分布："),
                        "none 档的表头不能还写「情绪密度」：措辞已经换成功能标签，"
                        "表头留着「情绪」，模型会把它自己读回去")
        self.assertNotIn("情绪", rows[0],
                         "none 档的密度行不能再说「情绪」——与卡上的"
                         "「不填任何心情词」当场打架")

    def test_light_level_keeps_the_mood_wording(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                  paradigm=PG.get("narrative"))
        self.assertIn("- 情绪密度：情绪标签", p)

    def test_the_two_wording_sets_never_meet(self):
        for key, wrong in (("paper", "情绪标签"), ("narrative", "标签分布")):
            p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                      paradigm=PG.get(key))
            self.assertNotIn(wrong, p, "%s 档里混进了另一档的话术" % key)

    def test_every_preset_step_renders_a_density_row(self):
        """三个密度档（低/中/高）在两套话术下都得有话说，不能有空行。"""
        for key in ("paper", "narrative"):
            for preset in ("argument", "story", "science"):    # 低 / 高 / 中
                p = S.build_system_prompt(self.CFG, preset, 900, 24,
                                          paradigm=PG.get(key))
                rows = self._density_rows(p)
                self.assertEqual(len(rows), 1, "%s / %s" % (key, preset))
                self.assertTrue(rows[0].split("：", 1)[1].strip(),
                                "密度行是空的：%s / %s" % (key, preset))

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


class TestEmotionLevelNarrowsTheEnum(unittest.TestCase):
    """档位是硬天花板，就要在约束解码那一层落地：让模型当场填不出来。

    只靠「提示词里劝 + 门禁事后抓」，填了再罚，等于拿一轮定点修补去买一件本来
    不该发生的事。两条产出 emotion 的路（整篇生成、定点修补）都得收窄——只收
    一条，等于给档位留了一道后门。
    """

    def test_script_schema_drops_mood_words_in_none_level(self):
        enum = S.script_schema("none")["properties"]["lines"]["items"][
            "properties"]["emotion"]["enum"]
        self.assertEqual(enum, list(S.NONE_LEVEL_EMOTIONS))
        for w in ("感慨", "恍然", "好奇", "疑惑", "肯定", "轻松"):
            self.assertNotIn(w, enum)

    def test_script_schema_keeps_mood_words_in_light_level(self):
        enum = S.script_schema("light")["properties"]["lines"]["items"][
            "properties"]["emotion"]["enum"]
        self.assertEqual(enum, list(EMOTION_TAGS))

    def test_patch_schema_narrows_like_the_script_schema(self):
        for level in ("none", "light"):
            item = S.patch_schema(level)["properties"]["edits"]["items"][
                "properties"]
            self.assertEqual(
                item["emotion"]["enum"],
                S.script_schema(level)["properties"]["lines"]["items"]
                ["properties"]["emotion"]["enum"],
                "两条产出 emotion 的路收窄得不一样")

    def test_schema_defaults_to_none_not_to_everything(self):
        """不写档位时按 none 走：默认只能更保守，不能把全量放行。"""
        enum = S.script_schema()["properties"]["lines"]["items"][
            "properties"]["emotion"]["enum"]
        self.assertEqual(enum, list(S.NONE_LEVEL_EMOTIONS))
        self.assertEqual(S.script_schema("LIGHT"), S.script_schema("none"),
                         "认不出的档位按 none 走，不许放行心情词")

    def test_templates_are_not_mutated(self):
        """收窄的是取出来的那一份，模块级模板是下一次的底稿，不许被改。"""
        S.script_schema("none")
        S.patch_schema("none")
        self.assertEqual(
            S.SCRIPT_SCHEMA["properties"]["lines"]["items"]["properties"]
            ["emotion"]["enum"], EMOTION_TAGS)
        self.assertEqual(
            S.PATCH_SCHEMA["properties"]["edits"]["items"]["properties"]
            ["emotion"]["enum"], EMOTION_TAGS)

    def test_patch_system_follows_the_level(self):
        self.assertNotIn("感慨", S.patch_system("none"))
        self.assertIn("感慨", S.patch_system("light"))
        self.assertIn(S.format_banned_rules(), S.patch_system("none"),
                      "换档位不该动措辞禁忌那一段")


class TestLevelOverflowIsPatchable(unittest.TestCase):
    """档位越界自带句号，属于能定点的那一类。

    漏了这一条，它会掉进「交人工」：明明改一个标签就能过，却把整句摆回给人。
    """

    def test_emotion_level_item_becomes_a_patch_target(self):
        report = {"items": [{"key": "emotion_level", "ok": False,
                             "lines": [3, 7]}]}
        targets, refull, manual = S.patch_targets(report, 10, "none")
        self.assertEqual(sorted(targets), [3, 7])
        self.assertEqual(refull, [])
        self.assertEqual(manual, [], "能定点的项不该掉进交人工")
        self.assertIn("平静", targets[3][0])


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
                         "project.program_name": "播客"})

    def _payload(self, speakers):
        lines = [{"speaker": s, "emotion": "平静",
                  "text": "第 %d 句正文内容示例文字" % i}
                 for i, s in enumerate(speakers)]
        return json.dumps({"title": "链与两头", "planned_episodes": 0,
                           "lines": lines}, ensure_ascii=False)

    def _gen(self, speakers, key="methodology"):
        return S.generate("素材内容示例", self.cfg,
                          FakeLLM(self._payload(speakers)),
                          project={"paradigm": key})

    def test_aabba_is_not_flipped_back(self):
        # 开场与提问同属 A（同一人连说两句）、中间两句回答同属 B，末句是写死的片尾
        speakers = ["A", "A", "B", "B", "A", "B"]
        gen = self._gen(speakers)
        self.assertEqual([s["speaker"] for s in gen["script"]], speakers,
                         "同一人连说两句不该被翻面")
        self.assertNotIn("flips", gen["report"])

    def test_over_the_limit_is_trimmed_and_recorded(self):
        gen = self._gen(["A"] * 7)
        # 上限 2 句：第 3、6 句超过上限，被压回交替并记下句号
        self.assertEqual(gen["report"].get("flips"), [3, 6])

    def test_the_same_script_is_judged_by_the_same_card(self):
        """卡换了，判据跟着换——对话录允许 4 句连说，方法论只给 2 句。"""
        gen = self._gen(["A", "B", "A", "A", "A", "A", "B"], key="interview")
        self.assertNotIn("flips", gen["report"], "四句连说在对话录里是合法的")


class TestPatchContract(unittest.TestCase):
    """定点补丁的契约：只换句子，不改行数，不碰没点名的句。

    这条路的价值全在这三条上。行数一变，门禁报的「第 N 句」就跟人对不上了；
    顺手改没点名的句子，等于把没毛病的地方重新摇一次骰子——那正是从前整篇
    重出最贵的地方。所以它们都是硬约束，越界就报错，不静默丢弃。
    """

    def test_schema_has_no_way_to_add_or_remove_lines(self):
        """「不许增删」不靠提示词劝，靠结构里根本写不出来。"""
        item = S.PATCH_SCHEMA["properties"]["edits"]["items"]["properties"]
        self.assertEqual(set(item), {"index", "text", "emotion"})
        self.assertNotIn("speaker", item,
                         "说话人归代码管（enforce_max_run / pin_intro_outro），"
                         "不该给模型留这道后门")

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

    def test_emotion_out_of_vocab_raises(self):
        script = S.normalize_script(_lines(4), _cfg())
        with self.assertRaises(ScriptError) as ctx:
            S.apply_patch(script, [{"index": 2, "text": "改过一次的台词文本",
                                    "emotion": "狂喜"}], {2})
        self.assertIn("词表", str(ctx.exception))

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
                  "text": "第 %d 句正文内容示例文字" % i} for i in range(12)]
        return json.dumps({"title": "链与两头", "planned_episodes": 7,
                           "lines": lines}, ensure_ascii=False)

    def test_sink_is_called_after_every_round(self):
        seen = []
        llm = ScriptedLLM(self._payload(), [SEM_FAIL_AT_3] * 8,
                          patch_replies=['{"edits": [{"index": 3, "text": '
                                         '"改过之后的一句示例文本内容"}]}'] * 8)
        S.generate("素材内容示例", self.cfg, llm, draft_sink=seen.append)
        self.assertEqual(len(seen), len(llm.users) + llm.patch_calls,
                         "每开一轮就该落一次盘")
        self.assertTrue(all("script" in g and "report" in g for g in seen))
        self.assertEqual([g["attempt"] for g in seen], [1, 2, 3])

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
        for block in ("【固定结构】", "【输出格式】", "【当前任务：生成要求】",
                      "【风格倾向】", "【两位主持人】"):
            self.assertIn(block, txt, block)

    def test_script_user_prompt_blocks(self):
        user = S.build_user_prompt("素材正文示例", self.cfg, "补一句背景", None, None)
        self.assertIn("【补充说明】", user)
        self.assertIn("【素材】", user)

    def test_script_user_prompt_rewrite_blocks(self):
        user = S.build_user_prompt("素材正文示例", self.cfg, "",
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

    预算固定返回 `size`，不按模板算：这里要盯的是**切分与合并的规则**，
    预算公式另有自己的用例。
    """

    def __init__(self, content_replies, size):
        super().__init__("{}", content_replies)
        self.size = size


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

    def _patch_cap(self, script, size):
        """把 material_capacity 打桩成「扣除提示词后每批恰好 size 字」。

        预算已不问模型（material_capacity 由 cfg 推导），这里补回生产端要扣的
        other（系统提示词 + 待审脚本 + 判据包 + 固定说明），让用例锁的切分
        规则与从前同一条算术。预算公式另有自己的用例，不在这里重复。
        """
        lines = "\n".join("%d. [%s] %s" % (i + 1, s["speaker"], s["text"])
                          for i, s in enumerate(script))
        other = (len(S.CHECK6_SYSTEM) + len(lines)
                 + len(S._evidence_block(None)) + S.CHECK6_FIXED_CHARS)
        return mock.patch.object(S, "material_capacity",
                                 return_value=size + other)

    def test_material_is_split_by_budget(self):
        llm = _BudgetScriptedLLM([SEM_PASS] * 8, size=2000)
        script = S.normalize_script(_lines(12), self.cfg)
        with self._patch_cap(script, 2000):
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
        with self._patch_cap(script, 2000):
            res = S.check6_llm(script, self.cfg, llm, "素" * 5000)
        state, detail, issues = res["semantic"]
        self.assertEqual([it["line"] for it in issues], [3],
                         "第 7 句只有一批报出，说明依据在别批里，应撤销")
        self.assertEqual(state, "fail")
        self.assertIn("已撤销", detail, "撤销要留痕，否则问题变少的来由不明")

    def test_no_budget_at_all_is_reported_not_swallowed(self):
        """窗口连模板都装不下时说「未核」，不能把整段素材硬塞进去。"""
        llm = _BudgetScriptedLLM([SEM_PASS] * 4, size=0)
        script = S.normalize_script(_lines(12), self.cfg)
        with mock.patch.object(S, "material_capacity", return_value=0):
            res = S.check6_llm(script, self.cfg, llm, "素" * 5000)
        self.assertEqual(llm.content_calls, 0, "预算为 0 时不该开调用")
        self.assertEqual(res["semantic"][0], "pending")


class _SegmentedLLM(object):
    """分段生成路径的假模型：规划、分段、内容检三路按 system 关键词分。

    分段开启时**不该**再走整篇生成——script_calls 就是盯这件事的。
    """

    def __init__(self, plan_payloads, segment_payloads, content_replies=None,
                 script_payloads=None):
        self.plan_payloads = list(plan_payloads)
        self.segment_payloads = list(segment_payloads)
        self.content_replies = list(content_replies or [])
        self.script_payloads = list(script_payloads or [])
        self.plan_calls = 0
        self.segment_calls = 0
        self.script_calls = 0
        self.content_calls = 0
        self.segment_users = []

    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             **kw):
        system = messages[0].get("content") or ""
        if "分段规划者" in system:
            self.plan_calls += 1
            return self.plan_payloads.pop(0), {}
        if "分段写作" in system:
            self.segment_calls += 1
            self.segment_users.append(messages[-1].get("content") or "")
            if self.segment_payloads:
                return self.segment_payloads.pop(0), {}
            raise AssertionError("分段调用超出脚本给出的段数")
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


def _evidence():
    return {"gist": "总主旨", "points": ["要点一"],
            "sections": [
                {"source": "s1", "anchor": "甲节", "gist": "甲节主旨",
                 "points": ["甲点一", "甲点二"]},
                {"source": "s2", "anchor": "乙节", "gist": "乙节主旨",
                 "points": ["乙点一"]},
            ]}


def _plan_payload(title="测试标题"):
    return json.dumps({"title": title, "segments": [
        {"sections": [1], "topic": "先讲甲节"},
        {"sections": [2], "topic": "再讲乙节"},
    ]}, ensure_ascii=False)


def _seg_payload(n):
    lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平稳",
              "text": "甲" * 30} for i in range(n)]
    return json.dumps({"lines": lines}, ensure_ascii=False)


def _whole_script_payload(n=6):
    lines = [{"speaker": "A" if i % 2 == 0 else "B", "emotion": "平稳",
              "text": "这一句台词正好二十个字左右的内容"} for i in range(n)]
    return json.dumps({"title": "整篇标题", "planned_episodes": 0,
                       "lines": lines}, ensure_ascii=False)


class TestSegmentQuotas(unittest.TestCase):
    """字数配额：账本单位只有字——句数只是形态引导，永不进核账。"""

    def test_schemas_carry_hard_caps(self):
        """maxItems 是失控刹车：约束解码实测能把数组无限写下去（单次 6300+
        token 不停）。规划段数、单段句数都必须有语法层封顶。"""
        plan = S._segment_plan_schema()
        self.assertEqual(plan["properties"]["segments"]["maxItems"],
                         S.SEGMENT_MAX_COUNT)
        seg = S.segment_schema("none")
        self.assertEqual(seg["properties"]["lines"]["maxItems"],
                         S.SEGMENT_MAX_SENTS * 2)

    def test_alloc_sums_to_target_and_proportional(self):
        secs = _evidence()["sections"]
        groups = [{"sections": [1], "topic": "a"}, {"sections": [2], "topic": "b"}]
        out = S._allocate_segment_quotas(groups, secs, 1000)
        self.assertEqual(sum(g["quota"] for g in out), 1000,
                         "配额合计必须精确等于目标，余数有归属")
        # 权重 10:7 → 588/412
        self.assertEqual(out[0]["quota"], 588)
        self.assertEqual(out[1]["quota"], 412)

    def test_plan_validation_rejects_uncovered_section(self):
        groups, err = S._validate_plan(
            {"segments": [{"sections": [1], "topic": "t"}]}, 2)
        self.assertIsNone(groups)
        self.assertIn("第 2 节", err)

    def test_plan_validation_rejects_out_of_range(self):
        groups, err = S._validate_plan(
            {"segments": [{"sections": [3], "topic": "t"}]}, 2)
        self.assertIsNone(groups)
        self.assertIn("越界", err)

    def test_plan_validation_rejects_section_in_two_segments(self):
        """同一节分进两段会被写两遍：那不是「覆盖到了」，是同一条重复。"""
        groups, err = S._validate_plan(
            {"segments": [{"sections": [1, 2], "topic": "甲"},
                          {"sections": [2], "topic": "乙"}]}, 2)
        self.assertIsNone(groups)
        self.assertIn("多个段", err)

    def test_plan_validation_merges_repeat_inside_one_segment(self):
        """同一段里把一节列两遍是笔误：归并即好，不该因此打回重排。"""
        groups, err = S._validate_plan(
            {"segments": [{"sections": [1, 1, 2], "topic": "甲"}]}, 2)
        self.assertIsNone(err)
        self.assertEqual(groups[0]["sections"], [1, 2])


class TestSegmentedGeneration(unittest.TestCase):
    """分段生成：规划打回与硬分兜底、段超配额重摇、差额滚入下段、门禁照旧。"""

    def _cfg(self, **over):
        cfg = ConfigManager().data()
        cfg.update({"script.gate_strict": False,
                    "gate.min_deviation_seconds": 9999,
                    "project.program_name": "播客"})
        cfg.update(over)
        return cfg

    def test_overshoot_triggers_retake(self):
        llm = _SegmentedLLM(
            [_plan_payload()],
            # 段 1 首版 300 字（配额 235、容差 ±60，超）→ 重摇 210 字入格；
            # 段 2 配额按差额动态缩，150 字入格。
            [_seg_payload(10), _seg_payload(7), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        logs = []
        title, plan, script = S._generate_segmented(
            llm, cfg, "素" * 100, _evidence(), card, "none", "argument",
            {"planned_episodes": 5}, 400, logs.append, None, None)
        self.assertEqual(llm.segment_calls, 3, "超配额的那段重摇了一次")
        self.assertEqual(len(script), 12)
        self.assertEqual(sum(len(l["text"]) for l in script), 360)
        self.assertEqual(title, "测试标题")
        self.assertEqual(plan, 5, "总期数以项目为准，程序回填")
        self.assertTrue(any("重摇" in m for m in logs))

    def test_bad_plan_falls_back_to_one_section_per_segment(self):
        bad = json.dumps({"title": "t", "segments": [
            {"sections": [1], "topic": "只给了第一节"}]}, ensure_ascii=False)
        # 兜底后两段配额 235/165、容差 ±60：210/150 字各一版入格，各一段调用。
        llm = _SegmentedLLM([bad] * (S.PLAN_RETRIES + 1),
                            [_seg_payload(7), _seg_payload(5)])
        cfg = self._cfg()
        card = S.resolve_paradigm(None, cfg)
        logs = []
        title, plan, script = S._generate_segmented(
            llm, cfg, "素" * 100, _evidence(), card, "none", "argument",
            None, 400, logs.append, None, None)
        self.assertEqual(llm.plan_calls, S.PLAN_RETRIES + 1, "规划打回到上限")
        self.assertTrue(any("一节一段" in m for m in logs), "硬分兜底要留痕")
        self.assertEqual(len(script), 12, "两段 7+5 句拼成整篇")

    def test_generate_routes_through_segments_for_mapped_project(self):
        # 路线判据是项目模式，不是配置开关：mapped（成稿规划）走分段。
        llm = _SegmentedLLM([_plan_payload("分段标题")],
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
    标签词整批拼进 text 开头念了出来。契约块（含正反例）必须在两份
    prompt（整篇 / 分段）里同时在场——缺一处，那条路径就退回裸奔。
    """

    CFG = {"script.target_minutes": 4.0, "tts.name_a": "小思",
           "tts.name_b": "小笔", "project.program_name": "示例节目"}

    CONTRACT_MARKS = (
        "每个字段只装它自己的东西",
        "text：只装**会被逐字合成语音念出来的台词正文**",
        '正确：{"speaker": "A"',
        '错误：{"speaker": "A", "text": "追问AI接管体力劳动之后',
    )

    def test_main_prompt_carries_field_contract(self):
        p = S.build_system_prompt(self.CFG, "argument", 900, 24)
        for mark in self.CONTRACT_MARKS:
            self.assertIn(mark, p, "整篇 prompt 缺字段契约要素：%s" % mark)

    def test_segment_prompt_carries_field_contract(self):
        p = S._segment_system_prompt(self.CFG, PG.get("paper"), "none",
                                     "argument", 1, 4, 900, "测试主题",
                                     is_first=True)
        for mark in self.CONTRACT_MARKS:
            self.assertIn(mark, p, "分段 prompt 缺字段契约要素：%s" % mark)

    def test_contract_avoids_mood_wording_in_none_level(self):
        """契约块措辞不得与 paper 档「不写心情」的纪律打架。"""
        p = S.build_system_prompt(self.CFG, "argument", 900, 24,
                                  paradigm=PG.get("paper"))
        self.assertNotIn("情绪标签", p)


class TestStripLabelPrefix(unittest.TestCase):
    """剥前缀是零成本兜底，只剥本句自己的标签，别人的标签开头是正文。"""

    def test_strips_own_label_with_colon_variants(self):
        self.assertEqual(S._strip_label_prefix("追问：AI会怎样？", "追问"),
                         "AI会怎样？")
        self.assertEqual(S._strip_label_prefix("解释AI正在接管", "解释"),
                         "AI正在接管")
        self.assertEqual(S._strip_label_prefix("追问、AI正在接管", "追问"),
                         "AI正在接管")

    def test_does_not_touch_other_labels_or_plain_text(self):
        # 「比喻」开头但本句 emotion=平静：那是正文，剥了就是篡改
        self.assertEqual(S._strip_label_prefix("比喻是人类最好的思维拐杖", "平静"),
                         "比喻是人类最好的思维拐杖")
        # 标签词出现在句中不是前缀，不剥
        self.assertEqual(S._strip_label_prefix("这需要追问一句", "追问"),
                         "这需要追问一句")

    def test_empty_emotion_is_noop(self):
        self.assertEqual(S._strip_label_prefix("追问：开头", ""), "追问：开头")


if __name__ == "__main__":
    unittest.main()
