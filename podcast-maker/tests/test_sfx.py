# -*- coding: utf-8 -*-
"""sfx_engine 的钉子与行为测试。

资源库完整性（manifest ↔ 文件 ↔ 哈希）与决策校验是纯本地断言；执行层走
真 ffmpeg，量小（秒级），量的是「时长实测回填」这个最容易漂的口径。
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from podcast_maker import audio_engine, sfx_engine  # noqa: E402


def _fake_sentence(tmp, seconds=3.0):
    path = os.path.join(tmp, "_fake_sentence.wav")
    audio_engine._run([audio_engine.ffmpeg_bin(), "-y", "-f", "lavfi",
                       "-i", "sine=frequency=440:duration=%.2f" % seconds,
                       "-ar", "44100", "-ac", "1", path])
    return path


class TestManifestLibrary(unittest.TestCase):
    """随仓台账即 closed 清单的源，台账烂了决策层就瞎——先锁台账。"""

    def setUp(self):
        self.man = sfx_engine.load_manifest()

    def test_manifest_exists_and_schema(self):
        self.assertIsNotNone(self.man)
        self.assertEqual(self.man["schema"], 1)
        self.assertIn("entries", self.man)
        self.assertGreater(len(self.man["entries"]), 0)

    def test_every_entry_has_file_and_hash_matches(self):
        for e in self.man["entries"]:
            p = os.path.join(sfx_engine.SFX_DIR, e["file"])
            self.assertTrue(os.path.isfile(p), msg=e["name"])
            with open(p, "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(sha, e["sha256"], msg=e["name"])
            self.assertGreater(e["duration"], 0, msg=e["name"])
            self.assertTrue(e["scene"], msg=e["name"])

    def test_every_source_is_cc0_with_trace(self):
        for e in self.man["entries"]:
            src = e["source"]
            self.assertEqual(src["license"], "CC0-1.0", msg=e["name"])
            self.assertTrue(src["id"], msg=e["name"])
            self.assertTrue(src["url"], msg=e["name"])

    def test_catalog_is_closed_list(self):
        cat = sfx_engine.catalog(self.man)
        self.assertEqual(len(cat), len(self.man["entries"]))
        for c in cat:
            self.assertIn(c["type"], ("event", "amb"))
            self.assertIn(sfx_engine._entry(self.man, c["name"])["type"],
                          ("event", "amb"))

    def test_seasoning_names_exist_in_library(self):
        """配额名单里的名字必须真实在库——名单写歪等于配额形同虚设。"""
        for name in sfx_engine.SEASONING:
            self.assertIsNotNone(sfx_engine._entry(self.man, name), msg=name)


class TestDecisionValidate(unittest.TestCase):
    """LLM 输出不可信：库外/越界/类型错位/超配额，四条防线各自要真拦。"""

    def setUp(self):
        self.man = sfx_engine.load_manifest()
        self.script = [{"text": "阿彪拔出了刀", "speaker": "A"},
                       {"text": "雨下了一夜", "speaker": "B"}]
        self.logs = []
        self.plan = sfx_engine._validate(
            {"lines": [
                {"i": 0, "sfx": "金属-碰撞", "pos": "before"},
                {"i": 1, "sfx": "雨-中", "pos": "bed"},
            ]}, self.script, self.man, self.logs.append)
        self.drop = lambda data: sfx_engine._validate(
            data, self.script, self.man, lambda m: None)

    def test_valid_entries_pass(self):
        self.assertEqual([e["sfx"] for e in self.plan["entries"]],
                         ["金属-碰撞", "雨-中"])

    def test_off_library_name_dropped(self):
        out = self.drop({"lines": [{"i": 0, "sfx": "龙吟", "pos": "before"}]})
        self.assertEqual(out["entries"], [])

    def test_out_of_range_index_dropped(self):
        out = self.drop({"lines": [{"i": 9, "sfx": "金属-碰撞", "pos": "before"},
                                   {"i": -1, "sfx": "门-关", "pos": "after"}]})
        self.assertEqual(out["entries"], [])

    def test_type_position_mismatch_dropped(self):
        out = self.drop({"lines": [{"i": 0, "sfx": "雨-中", "pos": "before"},
                                   {"i": 0, "sfx": "金属-碰撞", "pos": "bed"}]})
        self.assertEqual(out["entries"], [])

    def test_one_entry_per_sentence(self):
        out = self.drop({"lines": [{"i": 0, "sfx": "金属-碰撞", "pos": "before"},
                                   {"i": 0, "sfx": "门-关", "pos": "after"}]})
        self.assertEqual(len(out["entries"]), 1)

    def test_seasoning_cap(self):
        sfx_engine._validate(
            {"lines": [{"i": 0, "sfx": "雷-单声", "pos": "before"},
                       {"i": 1, "sfx": "雷-单声", "pos": "after"}]},
            self.script, self.man, lambda m: None)
        # 第二次超配额被丢：用三条句子试满配额边界
        script3 = self.script + [{"text": "又打雷了", "speaker": "A"}]
        out = sfx_engine._validate(
            {"lines": [{"i": 0, "sfx": "雷-单声", "pos": "before"},
                       {"i": 1, "sfx": "雷-单声", "pos": "after"},
                       {"i": 2, "sfx": "雷-单声", "pos": "before"}]},
            script3, self.man, lambda m: None)
        self.assertEqual(len(out["entries"]), sfx_engine.SEASONING_CAP)

    def test_off_library_drop_is_logged(self):
        logs = []
        sfx_engine._validate({"lines": [{"i": 0, "sfx": "龙吟", "pos": "before"}]},
                             self.script, self.man, logs.append)
        self.assertTrue(any("龙吟" in m for m in logs))


class TestPlanIO(unittest.TestCase):
    def test_plan_path_is_in_work(self):
        self.assertEqual(os.path.basename(sfx_engine.plan_path("某工作目录")),
                         "sfx_plan.json")


class TestApply(unittest.TestCase):
    """执行层走真 ffmpeg：量的是时长实测回填这个口径。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sfx_apply_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sent = _fake_sentence(self.tmp)
        self.dur = audio_engine.probe_duration_safe(self.sent)
        self.cfg = {"audio.sample_rate": 44100, "audio.channels": 1,
                    "sfx.event_gain_db": -6.0, "sfx.bed_gain_db": -8.0}

    def test_noop_on_empty_plan(self):
        files, durs, added = sfx_engine.apply(
            [self.sent], [self.dur], self.tmp, {"entries": []}, self.cfg)
        self.assertEqual(files, [self.sent])
        self.assertEqual(durs, [self.dur])
        self.assertEqual(added, 0.0)

    def test_before_after_baked_and_duration_refilled(self):
        plan = {"entries": [{"i": 0, "sfx": "金属-碰撞", "pos": "before"},
                            {"i": 0, "sfx": "门-关", "pos": "after"}]}
        files, durs, added = sfx_engine.apply(
            [self.sent], [self.dur], self.tmp, plan, self.cfg, log=lambda m: None)
        self.assertTrue(files[0].endswith("0000_after.wav"))
        # 实测回填：新时长必须大于原句长，且与 probe 一致（预算不漂的根）
        self.assertGreater(durs[0], self.dur + 2.0)
        self.assertAlmostEqual(durs[0],
                               audio_engine.probe_duration_safe(files[0]),
                               places=2)
        self.assertGreater(added, 2.0)

    def test_bed_keeps_duration(self):
        plan = {"entries": [{"i": 0, "sfx": "雨-中", "pos": "bed"}]}
        files, durs, _added = sfx_engine.apply(
            [self.sent], [self.dur], self.tmp, plan, self.cfg, log=lambda m: None)
        self.assertTrue(files[0].endswith("0000_bed.wav"))
        self.assertAlmostEqual(durs[0], self.dur, places=1)

    def test_missing_file_warn_skips(self):
        plan = {"entries": [{"i": 0, "sfx": "金属-碰撞", "pos": "before"}]}
        e = sfx_engine._entry(sfx_engine.load_manifest(), "金属-碰撞")
        os.rename(os.path.join(sfx_engine.SFX_DIR, e["file"]),
                  os.path.join(sfx_engine.SFX_DIR, e["file"] + ".bak"))
        self.addCleanup(os.rename,
                        os.path.join(sfx_engine.SFX_DIR, e["file"] + ".bak"),
                        os.path.join(sfx_engine.SFX_DIR, e["file"]))
        logs = []
        files, _d, _a = sfx_engine.apply([self.sent], [self.dur], self.tmp,
                                         plan, self.cfg, log=logs.append)
        self.assertEqual(files, [self.sent])   # 原样返回 = 跳过不停产
        self.assertTrue(any("缺失" in m for m in logs))


class TestDecisionBudget(unittest.TestCase):
    """输出预算必须走统一推动点 llm.max_tokens。

    思考段与答案段共用 max_tokens：写死小值=思考型模型的推理段吃光预算、
    答案段空转（v2.7.0 真机事故：reasoning_tokens=4096 吃满写死的 4096）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = self._tmp.name

    def _plan_with(self, cfg):
        seen = {}

        class _FakeLLM:
            def __init__(self, *a, **k):
                pass

            def chat(self, messages, **kw):
                seen.update(kw)
                return '{"lines":[]}', {}

        orig = sfx_engine.LLMClient
        sfx_engine.LLMClient = _FakeLLM
        try:
            plan = sfx_engine.plan_sfx(
                [{"speaker": "A", "text": "测试一句"}], self.work, cfg)
        finally:
            sfx_engine.LLMClient = orig
        return plan, seen

    def test_uses_unified_llm_max_tokens(self):
        _plan, seen = self._plan_with({"sfx.enabled": True,
                                       "llm.max_tokens": 47872})
        self.assertEqual(seen.get("max_tokens"), 47872)

    def test_fallback_matches_client_default(self):
        """键缺失时回落 8192，与 LLMClient.chat / planner 的缺省同口径。"""
        _plan, seen = self._plan_with({"sfx.enabled": True})
        self.assertEqual(seen.get("max_tokens"), 8192)


if __name__ == "__main__":
    unittest.main()
