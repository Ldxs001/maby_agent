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

"""门禁规则测试。

这几条规则都曾因判据设计不当而误报：
片头尾按字面复述判定、总时长只按百分比判定、拿词汇重叠去判内容是否连贯。
测试把修正后的判据钉住。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import duration_model, script_engine as SE     # noqa: E402
from podcast_maker.config_manager import ConfigManager  # noqa: E402

# emotion 字段已随脚本侧整体删除；旧测试数据里仍带这个键（多出的键没人读）。
EMO = "平静"


def _script(chars_per_line, n, speaker_alt=True):
    return [{"speaker": "A" if (i % 2 == 0 or not speaker_alt) else "B",
             "text": "中" * chars_per_line, "emotion": EMO}
            for i in range(n)]


class TestIntroOutroIsNotGatedAnymore(unittest.TestCase):
    """片头尾撤出门禁了——它现在是程序在整期定稿那一刻逐字粘上去的。

    判据留在这里只有两种下场：恒真（模型没参与，没什么可验的），或者拿正文去
    验首末句、每次都报「未命中」，然后让模型去改一句它压根没写过的句子。
    所以这一组不是「改判据」，是**确认它真的不在了**：谁也拿不到那条门禁。
    """

    def test_no_gate_key_and_no_predicate(self):
        from podcast_maker.config_manager import GATE_BY_KEY
        self.assertNotIn("intro_outro", GATE_BY_KEY, "撤掉的门禁不该留在表里")
        self.assertFalse(hasattr(SE, "intro_outro_ok"),
                         "判据函数一并撤掉，免得有人再挂回门禁上")

    def test_report_has_no_intro_outro_item(self):
        """报告里也不该再有这一项——每一项都对应一个真的被执行过的判据。"""
        cfg = ConfigManager().data()
        cfg["project.program_name"] = "播客"
        rep = SE.gate_generate(_script(20, 6), cfg)
        self.assertNotIn("intro_outro", [i["key"] for i in rep["items"]])


class TestTotalDuration(unittest.TestCase):
    """容差 = max(目标 × 百分比, 绝对秒数)。短集只按百分比判会永远不过。

    两个阈值一律从配置读，不在这里抄一份。抄一份就会脱钩：阈值从 8% 调到 15% 之后，
    这里还拿 8% 算容差，于是用例红着而没人管——本类就长期是这个状态。用例要钉的是
    「比例项与绝对下限谁占主导」这条分界逻辑，不是某天那个具体的百分比数字。

    每条用例都加一步前提自校验（`assertGreater` / `assertLess`）：一旦阈值被调歪到
    让分界逻辑失效，用例会在前提那一步就报错并说明原因，而不是换个方式静默通过。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg["audio.pause_between_lines"] = 0.0

    def _tolerance(self):
        """实现用的那对阈值：(比例, 绝对秒数)。"""
        return (float(self.cfg["gate.max_deviation_pct"]) / 100.0,
                float(self.cfg["gate.min_deviation_seconds"]))

    def _allowed(self, target_seconds):
        """实现用的容差口径：比例项与绝对下限取大者。"""
        ratio, floor = self._tolerance()
        return max(target_seconds * ratio, floor)

    def _lines_for(self, target_minutes, factor=1.0):
        """按当前校准反推句数：凑到目标时长需要多少句 20 字台词（再乘 factor）。

        为什么不写死句数：句数与估算时长之间隔着语速，而语速是个会改的配置。
        写死句数等于让「用例成不成立」取决于当时那把尺子，那两个数字早晚会漂。
        这里现推：量两句脚本的时长差得到每句耗时，再解目标句数。
        """
        self.cfg["script.target_minutes"] = target_minutes
        t1 = duration_model.explain(_script(20, 1), self.cfg)["total_seconds"]
        t2 = duration_model.explain(_script(20, 11), self.cfg)["total_seconds"]
        per = (t2 - t1) / 10.0
        if per <= 0:
            per = 4.8
        const = t1 - per
        n = int(round((target_minutes * 60.0 - const) / per * factor))
        return max(1, n)

    def _gate(self, n_lines, target_minutes):
        return self._pick(n_lines, target_minutes)[0]

    def _pick(self, n_lines, target_minutes):
        """返回 (total_duration 门禁项, explain 结果)。"""
        self.cfg["script.target_minutes"] = target_minutes
        s = _script(20, n_lines)
        est = duration_model.explain(s, self.cfg)
        rep = SE.gate_generate(s, self.cfg)
        return next(i for i in rep["items"] if i["key"] == "total_duration"), est

    def test_on_target_passes(self):
        """句数按当前校准凑到目标时长，门禁放行。"""
        item, est = self._pick(self._lines_for(1.0), 1.0)
        gap = abs(est["target_seconds"] - est["total_seconds"])
        self.assertLess(gap, self._allowed(est["target_seconds"]),
                        "本用例要落在容差之内（gap=%.1f 秒）" % gap)
        self.assertTrue(item["ok"], item["detail"])

    def test_short_set_tolerance_comes_from_absolute_floor(self):
        """1 分钟目标下，比例项只有几秒，实际容差必须由绝对下限撑起。"""
        item, est = self._pick(self._lines_for(1.0), 1.0)
        ratio, floor = self._tolerance()
        proportional = est["target_seconds"] * ratio
        self.assertGreater(floor, proportional, "本用例应处于绝对下限占主导的区间")
        gap = abs(est["target_seconds"] - est["total_seconds"])
        self.assertEqual(item["ok"], gap <= self._allowed(est["target_seconds"]),
                         item["detail"])

    def test_short_set_beyond_absolute_tolerance_fails(self):
        """台词多写 60%，偏差必然越过容差 —— 与容差的具体数值无关，只看比例。"""
        item, est = self._pick(self._lines_for(1.0, factor=1.6), 1.0)
        gap = abs(est["target_seconds"] - est["total_seconds"])
        self.assertGreater(gap, self._allowed(est["target_seconds"]),
                           "本用例要落在容差之外（gap=%.1f 秒）" % gap)
        self.assertFalse(item["ok"], item["detail"])

    def test_long_set_tolerance_comes_from_percentage(self):
        """20 分钟目标下，比例项应压过绝对下限，成为实际容差。"""
        item, est = self._pick(self._lines_for(20.0), 20.0)
        ratio, floor = self._tolerance()
        proportional = est["target_seconds"] * ratio
        self.assertGreater(proportional, floor, "本用例应处于比例项占主导的区间")
        gap = abs(est["target_seconds"] - est["total_seconds"])
        self.assertLess(gap, proportional, "本用例要落在容差之内（gap=%.1f 秒）" % gap)
        self.assertTrue(item["ok"], item["detail"])

    def test_long_set_beyond_percentage_fails(self):
        """超出比例容差就不通过。

        句数不写死，按当前校准的 1.6 倍目标推 —— 偏差远超 15% 的容差，
        不会出现「差几秒就翻面」的脆边界。
        """
        item, est = self._pick(self._lines_for(20.0, factor=1.6), 20.0)
        gap = abs(est["target_seconds"] - est["total_seconds"])
        self.assertGreater(gap, self._allowed(est["target_seconds"]),
                           "本用例要落在容差之外（gap=%.1f 秒）" % gap)
        self.assertFalse(item["ok"], item["detail"])

    def test_feedback_is_actionable(self):
        item = self._gate(23, 1.0)
        self.assertIn("字", item["detail"])


class TestContentCheckOwnership(unittest.TestCase):
    """内容检两项（语义 / 承诺链）都归模型判，代码侧一条不碰。

    判断依据都在语义层：话有没有编造、开头开的那个口子收没收，词汇匹配都做不了——
    「前后用词不同」恰恰是正常对话的样子，拿字面去比只会把正常的判成毛病。
    """

    def test_dims_are_semantic_and_promise(self):
        self.assertEqual([k for k, _ in SE.CHECK6_DIMS], ["semantic", "promise"])

    def test_code_side_never_judges_content(self):
        cfg = ConfigManager().data()
        cfg["audio.pause_between_lines"] = 0.0
        rep = SE.gate_check6(_script(20, 4), cfg, llm=None, material="")
        for it in rep["items"]:
            self.assertTrue(it.get("advisory"),
                            "内容项不该由代码下判断：%s" % it["key"])

    def test_no_llm_still_records_both_dims(self):
        """两项不能因缺 LLM 而从报告里消失，否则「全部通过」是假象。"""
        cfg = ConfigManager().data()
        cfg["audio.pause_between_lines"] = 0.0
        rep = SE.gate_check6(_script(20, 4), cfg, llm=None, material="")
        dims = [i["key"] for i in rep["items"] if i.get("advisory")]
        self.assertEqual(dims, ["check_" + k for k, _ in SE.CHECK6_DIMS])
        self.assertTrue(all(not i["ok"] for i in rep["items"] if i.get("advisory")))
        self.assertEqual(len(rep["pending"]), 2)
        self.assertTrue(rep["passed"], "未判定不等于不通过，不应阻断")

    def test_prompt_covers_both_dims(self):
        for key, _ in SE.CHECK6_DIMS:
            self.assertIn(key, SE.CHECK6_SYSTEM)

    def test_prompt_states_wording_rule(self):
        """提示词要说明对话体用词不同是正常的，否则模型会朝字面重复去凑。"""
        self.assertIn("用词", SE.CHECK6_SYSTEM)
        self.assertIn("回应", SE.CHECK6_SYSTEM)

    def test_prompt_does_not_hole_out_the_fixed_lines(self):
        """从前这里留着「首句与末句不受本条约束」那道口子。

        口子必须堵上：片头尾现在要到整期定稿那一刻才粘，内容检面对的**就是正文本身**。
        留着那句话，模型会去找一个不存在的豁免对象，甚至顺着它想象出两句固定
        语来「核对」，把不存在的句子报成问题。
        """
        self.assertNotIn("首句与末句", SE.CHECK6_SYSTEM)
        self.assertIn("不在这份稿子里", SE.CHECK6_SYSTEM,
                      "得说清为什么不必留口子，否则下一个人还会加回去")


class TestMissingKeyIsUnjudged(unittest.TestCase):
    """模型没返回某个键 = 那一项没判，绝不能默认成通过。

    缺键默认通过，等于把「模型偷懒」记成「检查通过」：报告上的绿灯比不检更坏，
    因为它让人以为已经检过了。
    """

    class _LLM:
        def __init__(self, payload):
            self.payload = payload

        def chat(self, messages, **kw):
            return self.payload, {}

    def _run(self, payload):
        return SE.check6_llm(_script(8, 4), ConfigManager().data(),
                             self._LLM(payload), "素材")

    def test_missing_key_is_pending_not_pass(self):
        out = self._run('{"semantic": {"pass": true, "issues": []}}')
        self.assertEqual(out["semantic"][0], "pass")
        self.assertEqual(out["promise"][0], "pending", "缺键必须判为未判定")
        self.assertIn("未判定", out["promise"][1])

    def test_node_without_pass_flag_is_pending(self):
        out = self._run('{"semantic": {"issues": ["x"]}, "promise": {"pass": true}}')
        self.assertEqual(out["semantic"][0], "pending")

    def test_judged_failure_carries_issues(self):
        out = self._run('{"semantic": {"pass": false, "issues": ["第2句编了数据"]},'
                        ' "promise": {"pass": true, "issues": []}}')
        self.assertEqual(out["semantic"][0], "fail")
        self.assertIn("第2句", out["semantic"][1])

    def test_broken_json_marks_both_pending(self):
        out = self._run("这不是 JSON")
        self.assertEqual([out[k][0] for k, _ in SE.CHECK6_DIMS],
                         ["pending", "pending"])


class TestReviewTokenBudget(unittest.TestCase):
    """审校调用按全局预算取，不写死一个小值。

    推理型模型把思考过程算进 max_tokens。预算写死 2000 时，思考吃完这 2000，
    答案部分一个字也轮不上，两项检查一同落空——报告上只剩两行「未判定」，
    起因被掩盖成一个看起来像模型不听话的现象。
    """

    class _LLM:
        def __init__(self):
            self.seen = None

        def chat(self, messages, temperature=0.8, max_tokens=8192, **kw):
            self.seen = max_tokens
            return ('{"semantic": {"pass": true, "issues": []},'
                    ' "promise": {"pass": true, "issues": []}}'), {}

    def _run_with(self, budget):
        cfg = ConfigManager().data()
        cfg["llm.max_tokens"] = budget
        llm = self._LLM()
        SE.check6_llm(_script(8, 4), cfg, llm, "素材")
        return llm.seen

    def test_budget_comes_from_config(self):
        self.assertEqual(self._run_with(6000), 6000)

    def test_budget_is_not_hardcoded_small(self):
        self.assertGreater(self._run_with(48640), 2000,
                           "审校预算不能写死成小值，否则思考过程会把答案吃光")


class TestAdvisorySemantics(unittest.TestCase):
    """advisory 只留给「模型没判」：它落 pending 交人工，不参与放行。

    而**判为不通过**的项照常参与放行——这正是从「模型判的一律 advisory」
    改过来的地方：一律 advisory 等于内容检永远拦不住人。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg["script.gate_strict"] = True

    def _advisory(self, ok=False, level="fail"):
        return [{"key": "check_semantic", "label": "语义检（LLM）", "level": level,
                 "judge": "", "ok": ok, "detail": "", "advisory": True}]

    def _hard(self, key="check_semantic", level="fail", ok=False):
        return {"key": key, "label": key, "level": level, "judge": "",
                "ok": ok, "detail": ""}

    def test_pending_goes_to_pending_not_fails(self):
        rep = SE._summarize(self._advisory(), self.cfg)
        self.assertEqual(rep["fails"], [])
        self.assertEqual(len(rep["pending"]), 1)
        self.assertTrue(rep["passed"])

    def test_pending_does_not_block_in_strict_mode(self):
        rep = SE._summarize(self._advisory(level="warn"), self.cfg)
        self.assertTrue(rep["passed"], "严格模式也不应把模型的不确定当硬事实")

    def test_advisory_pass_is_silent(self):
        rep = SE._summarize(self._advisory(ok=True), self.cfg)
        self.assertEqual(rep["pending"], [])
        self.assertTrue(rep["passed"])

    def test_judged_failure_blocks(self):
        """判为不通过 ≠ 未判定。前者要拦住，否则内容检等于没接上。"""
        rep = SE._summarize([self._hard()], self.cfg)
        self.assertEqual(len(rep["fails"]), 1)
        self.assertEqual(rep["pending"], [])
        self.assertFalse(rep["passed"])

    def test_promise_warn_blocks_only_in_strict(self):
        self.cfg["script.gate_strict"] = False
        rep = SE._summarize([self._hard(key="check_promise", level="warn")], self.cfg)
        self.assertTrue(rep["passed"])
        self.cfg["script.gate_strict"] = True
        rep = SE._summarize([self._hard(key="check_promise", level="warn")], self.cfg)
        self.assertFalse(rep["passed"])


class TestGateRegistry(unittest.TestCase):
    def test_total_duration_is_generate_stage(self):
        from podcast_maker.config_manager import GATE_BY_KEY
        self.assertEqual(GATE_BY_KEY["total_duration"]["stage"], "generate")

    def test_content_check_marked_llm(self):
        from podcast_maker.config_manager import GATE_BY_KEY
        for k in ("check_semantic", "check_promise"):
            self.assertIn("LLM", GATE_BY_KEY[k]["label"])

    def test_content_check_runs_in_generate_stage(self):
        """内容检跑在脚本生成阶段，不是等产物出来之后——晚一步就改不动了。"""
        from podcast_maker.config_manager import GATE_BY_KEY
        for k in ("check_semantic", "check_promise"):
            self.assertEqual(GATE_BY_KEY[k]["stage"], "generate")

    def test_semantic_check_is_blocking(self):
        """语义检要拦得住人，否则「检出不通过就打回重写」无从谈起。"""
        from podcast_maker.config_manager import GATE_BY_KEY
        self.assertEqual(GATE_BY_KEY["check_semantic"]["level"], "fail")

    def test_removed_gates_are_gone(self):
        from podcast_maker.config_manager import GATE_BY_KEY
        for k in ("check_logic", "check_continuity", "check_reasoning"):
            self.assertNotIn(k, GATE_BY_KEY)

    def test_all_param_keys_referenced_in_code(self):
        """配置项必须都有实际读取点，不留僵尸项。"""
        import glob
        from podcast_maker.config_manager import PARAM_SPEC
        files = [f for f in glob.glob(os.path.join(ROOT, "podcast_maker", "*.py"))
                 if not f.endswith("config_manager.py")]
        files.append(os.path.join(ROOT, "main.py"))
        bodies = []
        for f in files:
            with open(f, encoding="utf-8") as fh:
                bodies.append(fh.read())
        zombie = [k for k in PARAM_SPEC if not any(k in b for b in bodies)]
        self.assertEqual(zombie, [], "存在无引用配置项：%s" % zombie)


class TestReportMatchesWhatWasAsked(unittest.TestCase):
    """产物校验只能要求「这一期被要求产出什么」。

    关掉视频开关之后仍去要视频，每一次出片都判不过；片子过不了就不占期号，
    项目进度永远停在原地，而磁盘上每份产物看着都好好的——没人会想到是门禁
    在要一个从没被要求的东西。
    """

    def setUp(self):
        import shutil
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="pm_rep_")
        self.cfg = ConfigManager().data()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _manifest(self, do_video=True, vertical=False, files=("audio", "article")):
        import json
        assets = {}
        for k in files:
            p = os.path.join(self.dir, "%s.bin" % k)
            with open(p, "wb") as fh:
                fh.write(b"x")
            assets[k] = p
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"do_video": do_video, "assets": assets}, fh)
        return assets

    def _items(self):
        from podcast_maker import pipeline
        # 报告按「本期成品路径表」取料：一期的东西不再堆在同一目录里，
        # 清单落在「报告」子目录下，所以要给出它的确切位置。
        ep = {"manifest": os.path.join(self.dir, "manifest.json")}
        return {i["key"]: i for i in pipeline.build_report(ep, self.cfg)["items"]}

    def test_no_video_asked_means_no_video_required(self):
        self._manifest(do_video=False)
        items = self._items()
        self.assertTrue(items["assets_complete"]["ok"],
                        items["assets_complete"]["detail"])
        self.assertNotIn("av_sync", items, "没要求视频就不该有音画同步这一项")

    def test_video_asked_and_missing_is_caught(self):
        self._manifest(do_video=True)
        items = self._items()
        self.assertFalse(items["assets_complete"]["ok"])
        self.assertIn("video", items["assets_complete"]["detail"])

    def test_vertical_only_required_when_the_switch_is_on(self):
        self.cfg["video.produce_vertical"] = False
        self._manifest(do_video=True, files=("audio", "article", "video"))
        items = self._items()
        self.assertTrue(items["assets_complete"]["ok"],
                        items["assets_complete"]["detail"])
        items = self._items()
        self.assertNotIn("video_vertical", items["assets_complete"]["detail"],
                         "关掉竖屏还去要竖屏，等于永远过不了")

    def test_vertical_required_when_the_switch_is_on(self):
        self.cfg["video.produce_vertical"] = True
        self._manifest(do_video=True, files=("audio", "article", "video"))
        self.assertIn("video_vertical", self._items()["assets_complete"]["detail"])

    def test_everything_asked_and_present_passes(self):
        self.cfg["video.produce_vertical"] = True
        self._manifest(do_video=True, vertical=True,
                       files=("audio", "article", "video", "video_vertical"))
        items = self._items()
        self.assertTrue(items["assets_complete"]["ok"],
                        items["assets_complete"]["detail"])

    def test_manifest_records_what_was_asked(self):
        src_path = os.path.join(ROOT, "podcast_maker", "pipeline.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"do_video": bool(do_video)', src,
                      "不写进 manifest，报告只能一律按全套去要")

    def test_report_carries_render_stage_items_only(self):
        """产物报告只装产物阶段的项。

        门禁表里每条都标了 stage（generate 十一条 / render 八条），但表只是声明
        ——真正说了算的是「谁去执行」。产物报告一旦把脚本阶段的结论也收进来，
        或者在这里顺手再判一遍脚本，脚本的账就会在合成阶段又审一次：稿子被拒过
        的项目，连出片的机会都没有，磁盘上一个产物都看不见，看着像是合成坏了。
        """
        from podcast_maker.config_manager import GATE_BY_KEY
        self._manifest(do_video=True)
        wrong = sorted(k for k in self._items()
                       if GATE_BY_KEY.get(k, {}).get("stage") != "render")
        self.assertFalse(wrong, "产物报告里混进了非产物阶段的项：%s" % wrong)

    def test_build_report_takes_no_script_gate_argument(self):
        """build_report 的签名里不该有脚本阶段的门禁参数。

        有参数就等于留了个入口——下一次有人「顺手」把脚本的结论传进来，代码
        照样跑得通，划界又白划了。签名本身就是那道闸。
        """
        import inspect
        from podcast_maker import pipeline
        params = list(inspect.signature(pipeline.build_report).parameters)
        self.assertEqual(params, ["ep", "cfg"], params)


class TestRenderStageDoesNotWriteScript(unittest.TestCase):
    """合成端不写稿、不判稿：脚本是脚本阶段的事。"""

    def setUp(self):
        import tempfile
        from podcast_maker import config_manager, duration_model
        self.cfg = config_manager.default_config()
        self.calib = duration_model.Calibration()
        self.tmp = tempfile.mkdtemp()

    def test_no_script_means_an_error_not_a_fresh_one(self):
        """取不到稿子就报错，不现场写一份。

        现场生成看着像是"帮忙"，实则两件事都做坏了：把审过的稿换成模型新写的
        另一份，而合成端根本没有判它的资格。
        """
        from podcast_maker import pipeline
        with self.assertRaises(pipeline.PipelineError) as cm:
            pipeline.run_episode(self.cfg, self.calib, "", "标题", episode_no="1",
                                 project_dir=self.tmp, script=None,
                                 reuse=True, llm=None)
        self.assertIn("脚本", str(cm.exception))

    def test_existing_script_is_never_re_gated(self):
        """手上有稿子就直接合成，不再跑一遍生成阶段门禁。

        现场就是这么卡的：脚本页生成的稿子带 6 处禁用词，点合成又被判一遍，
        合成一步没跑，音视频目录空着——人看到的是「合成坏了」，其实是脚本的账
        在合成阶段又收了一遍。
        """
        from unittest import mock
        from podcast_maker import pipeline, script_engine, tts_engine
        script = [{"speaker": "A", "text": "所有的话都说满了", "emotion": "平静"},
                  {"speaker": "B", "text": "这是第二句台词", "emotion": "平静"}]
        boom = AssertionError("合成端不该再跑生成阶段门禁")
        with mock.patch.object(script_engine, "gate_generate", side_effect=boom), \
             mock.patch.object(script_engine, "gate_check6", side_effect=boom), \
             mock.patch.object(pipeline.assets_factory, "build_all",
                               return_value={"bg_h": "", "bg_v": "",
                                             "bgm": None, "covers": {}}), \
             mock.patch.object(tts_engine, "synthesize",
                               side_effect=RuntimeError("停在语音合成")):
            with self.assertRaises(RuntimeError) as cm:
                pipeline.run_episode(self.cfg, self.calib, "", "标题",
                                     episode_no="1", project_dir=self.tmp,
                                     script=script, reuse=False, llm=None)
        self.assertIn("停在语音合成", str(cm.exception))

    def test_gate_report_roundtrip_and_broken_file(self):
        """脚本阶段落的结论读得回来；文件坏了当作没有——不拿坏文件去骚扰人。"""
        from podcast_maker import script_engine as SE
        self.assertIsNone(SE.read_gate_report(self.tmp),
                          "没有文件就该返回 None")
        path = SE.write_gate_report(self.tmp, {"passed": False,
                                               "items": [{"ok": False}]})
        self.assertTrue(os.path.exists(path))
        self.assertFalse(SE.read_gate_report(self.tmp)["passed"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 json")
        self.assertIsNone(SE.read_gate_report(self.tmp),
                          "读坏了要当没有，不是抛出去把人挡在门外")


class TestBannedFeedbackIsActionable(unittest.TestCase):
    """措辞命中的回灌要精确到句、给全量、带替换落点。

    三条缺一不可。只报前几处，模型把这几处改完、剩下的还在，下一轮照样不过；
    只报命中词不给替换说法，模型只会换个同样把话说满的别的词；不带上「只改这几句」，
    模型通篇重写，新写的句子又带进新的命中，轮次全耗在打地鼠上。
    回灌里还得附上上一版正文——不然模型看不到第 58 句原本写了什么，想定点改也无从下手。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg["project.program_name"] = "播客"

    def _banned_item(self, n_hits):
        s = _script(20, 20)
        for i in range(0, n_hits * 2, 2):
            s[i]["text"] = "所有" + "中" * 18
        rep = SE.gate_generate(s, self.cfg)
        return next(i for i in rep["items"] if i["key"] == "banned_words")

    def test_every_hit_is_listed_not_truncated(self):
        item = self._banned_item(8)
        self.assertEqual(len(item["hits"]), 8)
        self.assertIn("第 15 句", item["detail"])

    def test_feedback_names_the_line_the_word_and_the_substitute(self):
        fb = SE._build_feedback([self._banned_item(8)])
        self.assertIn("第 15 句「所有」", fb)
        self.assertIn("换成", fb, "不给落点，模型只会换个同样把话说满的词")
        self.assertIn("只改", fb, "不说清是定点修补，模型会通篇重写")

    def test_banned_rules_carry_a_substitute(self):
        from podcast_maker.config_manager import BANNED_RULES
        for r in BANNED_RULES:
            self.assertTrue(r.get("substitute"), r["key"])

    def test_rewrite_carries_the_previous_script(self):
        prev = "1. [A] 上一版第一句"
        user = SE.build_user_prompt("素材", self.cfg, "要改的地方", prev)
        self.assertIn(prev, user, "不给原稿，模型看不见第几句是什么，只能凭印象重写")

    def test_prompt_carries_the_substitute_too(self):
        """前置与回灌必须给同一套说法：前面说「换成往往」，后面不能只说「不许用所有」。"""
        s = SE.format_banned_rules()
        self.assertIn("所有", s)
        self.assertIn("往往", s)


class TestReadableGate(unittest.TestCase):
    """可朗读门禁：判据盯「TTS 拿到什么字符会坏」，与素材来源无关。

    三面边界各有测试钉着：该拦的形状（emoji、网址、命令参数、不可见字符、
    Markdown 痕迹）一条不漏；念得出来的内容（中文破折号、范围号、英文单词、
    数字）一个不误伤——中文破折号「——」是 em dash 不是 ASCII 连字符对，
    「3-5」是单个连字符，都不落在命令参数判据里。回灌给句号、给形状样例、
    说明只改这几句，与措辞禁忌同一条纪律。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg["project.program_name"] = "播客"

    def _item(self, texts):
        s = _script(20, len(texts))
        for i, t in enumerate(texts):
            s[i]["text"] = t
        rep = SE.gate_generate(s, self.cfg)
        return next(i for i in rep["items"] if i["key"] == "readable_text")

    def test_unreadable_shapes_are_caught(self):
        # 每种形状一个代表：命中，且人话里说得清是哪种
        for text, label in [("这招真管用 ✅", "emoji 或图标字符"),
                            ("详见 https://example.com/a 页面", "网址"),
                            ("加 --mode 参数再跑", "命令参数"),
                            ("复制来的\u200b文本", "不可见字符"),
                            ("原话是 `code` 这样", "排版符号")]:
            item = self._item(["中" * 20, text])
            self.assertFalse(item["ok"], text)
            self.assertIn(label, item["detail"], text)

    def test_readable_content_is_not_flagged(self):
        # 念得出来的都不拦——判据窄到只拦必坏的形状
        clean = ["回顾一下二〇二四年的进展——数据涨了三成",
                 "部署要三到五天，API 最多支持 40 个",
                 "他说走就走，一条短信都不留"]
        item = self._item(clean)
        self.assertTrue(item["ok"], item["detail"])

    def test_hit_feedback_names_line_shape_and_sample(self):
        item = self._item(["中" * 20, "这招真管用 ✅", "中" * 20])
        fb = SE._build_feedback([item])
        self.assertIn("第 2 句", fb)
        self.assertIn("✅", fb)
        self.assertIn("只改", fb, "不说清是定点修补，模型会通篇重写")

    def test_patch_target_points_at_the_line(self):
        s = _script(20, 3)
        s[1]["text"] = "加 --mode 参数再跑"
        rep = SE.gate_generate(s, self.cfg)
        targets, refull, manual = SE.patch_targets(rep, 3)
        self.assertIn(2, targets)
        self.assertFalse(refull, "形状判据自带句号，不该落进整篇重出")

    def test_prompt_bears_the_rule(self):
        """前置禁令进整篇提示词，与门禁判据同一条形状清单。"""
        sys_prompt = SE.build_system_prompt(self.cfg, None, 800, 20)
        self.assertIn("台词中不可出现无法朗读的内容", sys_prompt)
        self.assertIn("✅", sys_prompt)


class TestLineEndPunct(unittest.TestCase):
    """句尾标点门禁：有没有归 py，对不对归语义。

    判「末字符不是终止标点」一步盖住两种坏形：完全没标点（裸尾）、拿逗号顿号
    这类句中标点收尾。收尾引号/括号先剥掉——「……这样的话。」标点在引号里。
    问句收句号这类「符号用得对不对」py 不判，归提示词与回灌方向。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg["project.program_name"] = "播客"

    def _item(self, texts):
        s = _script(20, len(texts))
        for i, t in enumerate(texts):
            s[i]["text"] = t
        rep = SE.gate_generate(s, self.cfg)
        return next(i for i in rep["items"] if i["key"] == "line_end_punct")

    def test_tail_helper(self):
        # 命中：裸尾、句中标点收尾、收尾引号里没标点
        self.assertEqual(SE.end_punct_tail("先扫描再进模型"), "型")
        self.assertEqual(SE.end_punct_tail("先扫描，"), "，")
        self.assertEqual(SE.end_punct_tail("他问“为什么”"), "么")
        # 放行：三种终止标点、省略号、包裹符包住标点
        self.assertIsNone(SE.end_punct_tail("大明于1368年立朝。"))
        self.assertIsNone(SE.end_punct_tail("为什么这么说呢？"))
        self.assertIsNone(SE.end_punct_tail("让暴风雨来得更猛烈些吧！"))
        self.assertIsNone(SE.end_punct_tail("故事还没讲完……"))
        self.assertIsNone(SE.end_punct_tail("「他说完就走了。」"))
        self.assertIsNone(SE.end_punct_tail(""))

    def test_bad_endings_are_caught(self):
        item = self._item(["首要难题是如何拆解语言", "先扫描，再进模型，"])
        self.assertFalse(item["ok"])
        self.assertIn("第 1 句", item["detail"])
        self.assertIn("第 2 句", item["detail"])

    def test_punctuated_endings_pass(self):
        item = self._item(["大明于1368年立朝。",
                           "为什么这么说呢？",
                           "「他说完就走了。」",
                           "故事还没讲完……"])
        self.assertTrue(item["ok"], item["detail"])

    def test_semantic_type_is_not_judged(self):
        # 问句收句号是「符号用得对不对」，归语义，py 不拦
        item = self._item(["为什么这么说呢。"])
        self.assertTrue(item["ok"], item["detail"])

    def test_hit_feedback_names_line_and_direction(self):
        item = self._item(["中" * 20, "先扫描，再进模型，"])
        fb = SE._build_feedback([item])
        self.assertIn("第 2 句", fb)
        self.assertIn("只改", fb, "不说清是定点修补，模型会通篇重写")
        self.assertIn("疑问收？", fb, "方向不给足，模型一律句号应付")

    def test_patch_target_points_at_the_line(self):
        s = _script(20, 3)
        s[1]["text"] = "靠什么机制来运转呢"
        rep = SE.gate_generate(s, self.cfg)
        targets, refull, manual = SE.patch_targets(rep, 3)
        self.assertIn(2, targets)
        self.assertFalse(refull, "句尾标点自带句号，不该落进整篇重出")
        self.assertIn("语义", targets[2][0])

    def test_prompt_bears_the_rule(self):
        """契约与句尾标点条款进整篇与分段两份提示词，一处都不许漏。"""
        sys_prompt = SE.build_system_prompt(self.cfg, None, 800, 20)
        self.assertIn("每句必须以标点符号收尾", sys_prompt)
        self.assertIn("含碳12%的铁属于钢", sys_prompt)
        self.assertIn("承接", sys_prompt)
        card = SE.resolve_paradigm(None, self.cfg)
        seg_prompt = SE._segment_system_prompt(
            self.cfg, card, "science", 1, 3, 800, "段主旨", True)
        self.assertIn("每句必须以标点符号收尾", seg_prompt)
        self.assertIn("含碳12%的铁属于钢", seg_prompt)


class TestDurationGateIsSoft(unittest.TestCase):
    """总时长偏差判的是估算值：只记账，不拦放行，也不回灌重写。

    它是估算值不是实测值——真章要等合成出来才算，口径本身也只是个通用语速。
    拿它把稿子打回重写，等于让模型围着一个它既测不出、也控不住的秒数反复改。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        self.cfg["project.program_name"] = "播客"
        self.cfg["gate.max_deviation_pct"] = 15.0

    def test_far_off_duration_is_recorded_but_not_blocking(self):
        rep = SE.gate_generate(_script(20, 10), self.cfg)
        dur = next(i for i in rep["items"] if i["key"] == "total_duration")
        self.assertFalse(dur["ok"])
        self.assertTrue(dur.get("soft"))
        self.assertIn("total_duration", [i["key"] for i in rep["soft"]])
        self.assertNotIn("total_duration", [i["key"] for i in rep["fails"]])

    def test_strict_mode_does_not_promote_it_to_a_blocker(self):
        self.cfg["script.gate_strict"] = True
        rep = SE.gate_generate(_script(20, 10), self.cfg)
        self.assertNotIn("total_duration", [i["key"] for i in rep["warns"]],
                         "严格模式拦的是 warn，soft 项不该被它顺手拦下")

    def test_tolerance_follows_the_configured_percent(self):
        """容差 = max(目标×百分比, 绝对下限)。四分钟、15% → ±36 秒。"""
        self.cfg["script.target_minutes"] = 4.0
        self.cfg["gate.min_deviation_seconds"] = 0.0
        rep = SE.gate_generate(_script(20, 60), self.cfg)
        dur = next(i for i in rep["items"] if i["key"] == "total_duration")
        self.assertIn("±36.0 秒", dur["detail"])


class TestSpeakerRunLimit(unittest.TestCase):
    """同一人连着说几句是允许的，超上限才是错。

    判据从「必须交替」换成「连续句数」：前者会把一个人的一段话掰给两个人
    （音色跟着换），那正是这条门禁原先在逼着模型做的事。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()
        # 对话形式是界面上可改的一项：测试自己定死「按卡走」，否则用户在界面上
        # 选过哪一项，这些用例就跟着变红——那是把他的配置当成了被测对象。
        self.cfg["script.dialogue_form"] = ""

    def _run(self, speakers):
        return [{"speaker": s, "emotion": EMO, "text": "中" * 20}
                for s in speakers]

    def _item(self, script, paradigm=None):
        rep = SE.gate_generate(script, self.cfg, paradigm)
        return next(i for i in rep["items"] if i["key"] == "ab_run_limit")

    def test_aabb_passes(self):
        """A A B B A —— 一段完整的回答由同一个人说完，不该被判错。"""
        item = self._item(self._run("AABBA"), {"form": "qa"})
        self.assertTrue(item["ok"], item["detail"])

    def test_run_over_the_limit_fails(self):
        item = self._item(self._run("AAA"), {"form": "qa"})
        self.assertFalse(item["ok"], item["detail"])
        self.assertIn("3", item["detail"])

    def test_limit_comes_from_the_form_not_the_card(self):
        """上限由**对话形式**给：同一份稿子（B 连说四句）在一问一答下合法，
        在闲聊漫谈下就超了——卡只决定默认用哪种形式。"""
        script = self._run("ABBBB")
        self.assertTrue(self._item(script, {"form": "qa"})["ok"])
        self.assertFalse(self._item(script, {"form": "chat"})["ok"])

    def test_each_side_is_judged_by_its_own_cap(self):
        """主讲＋捧哏：B 连说三句合法（上限 10），A 连说三句就超了（上限 1）。"""
        self.assertTrue(self._item(self._run("ABBB"), {"form": "anchor"})["ok"])
        self.assertFalse(self._item(self._run("AAAB"), {"form": "anchor"})["ok"])

    def test_gate_key_is_registered(self):
        from podcast_maker.config_manager import GATE_BY_KEY
        self.assertEqual(GATE_BY_KEY["ab_run_limit"]["stage"], "generate")
        self.assertEqual(GATE_BY_KEY["ab_run_limit"]["level"], "fail")

    def test_the_whole_run_is_named_with_its_cap(self):
        """点的是**整段**，不是超出的那几句：并句要把这一段并成一句。

        只点超出的句子，模型没法知道这几句该并到哪一句里去——整段加上限一起给，
        它才写得出一条合格的 absorb。
        """
        item = self._item(self._run("BAAAA"), {"form": "qa"})
        self.assertFalse(item["ok"], item["detail"])
        self.assertEqual(item["runs"], [{"speaker": "A", "lines": [2, 3, 4, 5],
                                         "cap": 2}])
        self.assertEqual(item["lines"], [2, 3, 4, 5])

    def test_the_gate_names_it_without_touching_the_draft(self):
        """门禁只判不改稿——翻说话人的那个后处理已在 v0.34.0 取下。"""
        script = self._run("AAAA")
        SE.gate_generate(script, self.cfg, {"form": "qa"})
        self.assertEqual([s["speaker"] for s in script], list("AAAA"))


class TestSpeakerFallback(unittest.TestCase):
    """漏标按「承接上一句」补，不按位置补。

    从前按 `i % 2` 补，等于把「谁在说」定义成「第几句」——模型漏一个字母，
    一个人的半句话就被判给了对方，音色随之换人。
    """

    def setUp(self):
        self.cfg = ConfigManager().data()

    def test_missing_speaker_inherits_the_previous_one(self):
        out = SE.normalize_script(
            [{"speaker": "B", "text": "第一句够长了", "emotion": EMO},
             {"text": "第二句也够长", "emotion": EMO},
             {"text": "第三句同样够长", "emotion": EMO}],
            self.cfg)
        self.assertEqual([x["speaker"] for x in out], ["B", "B", "B"])

    def test_missing_speaker_on_the_first_line_falls_to_a(self):
        out = SE.normalize_script(
            [{"text": "第一句够长了", "emotion": EMO}], self.cfg)
        self.assertEqual(out[0]["speaker"], "A")


class TestEveryExampleTextCarriesTerminalPunct(unittest.TestCase):
    """源码级钉子：script_engine 里所有提示词示例的 text 字面量必须带终止标点。

    v0.28.0 改五份提示词时漏了输出格式示例的占位文本（「台词正文，只写要
    念出来的话」裸尾两处），模型照形状抄出裸句。此钉子扫全部 "text": "…"
    字面量；唯一放行是「错误：」示例——它本来就演示句尾没标点。
    """

    def test_no_bare_example_text_in_source(self):
        import io as _io
        import re as _re
        src = _io.open(SE.__file__, encoding="utf-8").read()
        bad = []
        for m in _re.finditer(r'"text": "([^"]*)"', src):
            sample = m.group(1)
            if sample.endswith(("。", "！", "？", "…")):
                continue
            # 「错误：」示例故意裸尾，放行；其余一律不许
            head = src[max(0, m.start() - 40):m.start()]
            if "错误：" in head:
                continue
            bad.append(sample)
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
