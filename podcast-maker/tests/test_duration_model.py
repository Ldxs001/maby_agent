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

"""时长模型测试：字数 ↔ 时长的唯一换算入口。"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import duration_model as D  # noqa: E402


class TestCounters(unittest.TestCase):
    def test_hanzi_only(self):
        self.assertEqual(D.count_hanzi("中文测试"), 4)

    def test_latin_word_is_one_token(self):
        self.assertEqual(D.count_latin_tokens("中文abc123"), 1)

    def test_latin_token_keeps_hyphen_and_dot(self):
        # GPT-4 与 Claude-3.5 各自算一个词，不能被连字符、小数点切开
        self.assertEqual(D.count_latin_tokens("GPT-4 和 Claude-3.5"), 2)

    def test_punct(self):
        self.assertEqual(D.count_punct("你好，世界。"), 2)

    def test_effective_weighting(self):
        # 汉字 1.0 / 标点 0.5 / 西文词 1.5
        self.assertAlmostEqual(D.effective_chars("你好，世界。"), 5.0)
        self.assertAlmostEqual(D.effective_chars("hello world"), 3.0)
        self.assertAlmostEqual(D.effective_chars("中文abc123"), 3.5)


class TestEstimate(unittest.TestCase):
    def test_monotonic_in_length(self):
        self.assertLess(D.estimate_seconds("中" * 100),
                        D.estimate_seconds("中" * 200))

    def test_speed_is_inverse(self):
        slow = D.estimate_seconds("中" * 100, 1.0)
        fast = D.estimate_seconds("中" * 100, 1.25)
        self.assertAlmostEqual(slow / fast, 1.25, places=6)

    def test_chars_for_target_inverts_estimate(self):
        # 反推与估时互为逆运算。字数只能取整，所以判据是「落在一个字的时长之内」，
        # 不是死等 60.000 秒——语速不是整数时，取整那半个字就是全部误差来源，
        # 拿 places=3 去卡它，卡的是除不尽的余数，不是模型。
        want = 60.0
        n = D.chars_for_target(want, 1.0)
        self.assertAlmostEqual(n, want * D.STANDARD_K, delta=1.0)
        sec = D.estimate_seconds("中" * int(round(n)), 1.0)
        self.assertAlmostEqual(sec, want, delta=D.estimate_seconds("中", 1.0))

    def test_empty_text_is_zero(self):
        self.assertEqual(D.estimate_seconds(""), 0.0)


class TestFit(unittest.TestCase):
    def test_l1_recovers_k(self):
        # t = eff / k  =>  k = eff / t
        pairs = [{"hanzi": c, "punct": 0, "latin": 0, "seconds": c / 4.0}
                 for c in (5, 10, 20, 40)]
        self.assertAlmostEqual(D.fit_l1(pairs), 4.0, places=6)

    def test_l1_rejects_zero_duration(self):
        self.assertIsNone(D.fit_l1([{"hanzi": 10, "punct": 0, "seconds": 0.0}]))

    def test_l2_needs_variation(self):
        # 三维无变化时矩阵奇异，必须拒绝升级而不是给出假系数
        flat = [{"hanzi": 10, "punct": 0, "latin": 0, "seconds": 2.0}] * 5
        self.assertIsNone(D.fit_l2(flat))

    def test_l2_recovers_weights(self):
        pairs = [{"hanzi": h, "punct": p, "latin": lt,
                  "seconds": 0.2 * h + 0.1 * p + 0.3 * lt}
                 for h in (10, 20, 30, 40, 50)
                 for p in (0, 2, 4)
                 for lt in (0, 1, 2)]
        sol = D.fit_l2(pairs)
        self.assertIsNotNone(sol)
        self.assertAlmostEqual(sol["a"], 0.2, places=4)
        self.assertAlmostEqual(sol["b"], 0.1, places=4)
        self.assertAlmostEqual(sol["c"], 0.3, places=4)


class TestCalibration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm-calib-")
        self.calib = D.Calibration(path=os.path.join(self.tmp, "calib.json"))

    def _pairs(self, k=4.0):
        return [{"text": "中" * c, "seconds": c / k} for c in (5, 10, 20, 40)]

    def test_calibrate_records_group(self):
        st = D.calibrate(self.calib, "edge", "zh-CN-X", 1.0, self._pairs())
        self.assertEqual(st["mode"], "L1")
        self.assertAlmostEqual(st["k"], 4.0, places=3)
        entry = self.calib.get("edge", "zh-CN-X", 1.0)
        self.assertEqual(entry["n"], 4)

    def test_groups_are_isolated(self):
        D.calibrate(self.calib, "edge", "zh-CN-X", 1.0, self._pairs())
        # 另一音色不应继承同组系数
        self.assertIsNone(self.calib.get("edge", "zh-CN-Y", 1.0))
        self.assertIsNotNone(self.calib.get("edge", "zh-CN-X", 1.0))
        # 语速也是分组维度
        self.assertIsNone(self.calib.get("edge", "zh-CN-X", 1.25))

    def test_ema_smooths_outlier(self):
        D.calibrate(self.calib, "edge", "v", 1.0, self._pairs(k=4.0))
        st = D.calibrate(self.calib, "edge", "v", 1.0, self._pairs(k=100.0))
        # 单次异常不能把系数整根带偏：结果落在两者之间且靠近旧值
        self.assertGreater(st["k"], 4.0)
        self.assertLess(st["k"], 100.0)

    def test_too_few_samples_keeps_l1_default(self):
        st = D.calibrate(self.calib, "edge", "v", 1.0,
                         [{"text": "中" * 10, "seconds": 2.0}])
        self.assertEqual(st["mode"], "L1")
        self.assertIsNone(self.calib.get("edge", "v", 1.0))

    def test_save_and_load_roundtrip(self):
        D.calibrate(self.calib, "edge", "v", 1.0, self._pairs())
        again = D.Calibration(path=os.path.join(self.tmp, "calib.json"))
        self.assertIsNotNone(again.get("edge", "v", 1.0))
        self.assertAlmostEqual(again.get("edge", "v", 1.0)["k"], 4.0, places=3)


class TestStandardRate(unittest.TestCase):
    """标准语速：脚本阶段唯一的一把尺子。

    它必须与音色无关——换一期换了个音色，稿子的时长目标不该跟着变。
    各音色相对它的快慢由一个比例体现（`ratio_of`），与估算本身分开。
    """

    def setUp(self):
        self.cfg = {"tts.engine": "edge", "tts.voice_a": "va", "tts.speed_a": 1.0,
                    "tts.voice_b": "vb", "tts.speed_b": 1.0,
                    "script.target_minutes": 4.0, "audio.pause_between_lines": 0.35}

    def test_standard_k_is_the_fallback_and_the_script_ruler(self):
        # 一个常量两处用：估算的缺省值、校准缺样本时的兜底，同为标准语速。
        self.assertEqual(D.standard_stats()["k"], D.STANDARD_K)
        self.assertEqual(D.DEFAULT_K, D.STANDARD_K)

    def test_unit_conversion_matches_the_declared_units(self):
        # 汉字/分钟 × 折算 × 每分钟 → 有效字/秒。改了单位关系必须在这里露馅。
        self.assertAlmostEqual(
            D.STANDARD_K, D.STANDARD_CPM / 60.0 * D.EFF_PER_HANZI, places=2)

    def test_script_estimate_ignores_the_voice(self):
        """A 角换成一个慢得多的音色，脚本估时不动。"""
        script = [{"speaker": "A", "text": "中" * 60}]
        base = D.explain(script, self.cfg)["total_seconds"]
        self.cfg["tts.voice_a"] = "另一个音色"
        calib = D.Calibration(path=os.path.join(
            tempfile.mkdtemp(prefix="pm-std-"), "calib.json"))
        calib.groups["edge|另一个音色|1.00"] = {"k": 3.0, "n": 50, "mode": "L1"}
        self.assertEqual(D.explain(script, self.cfg)["total_seconds"], base)
        self.assertAlmostEqual(D.estimate_line("中" * 60, "A", self.cfg), base, places=2)
        # 明确要音色口径时才看那本校准账——它算出来的确实不一样。
        self.assertGreater(D.explain(script, self.cfg, calib)["total_seconds"], base)

    def test_rows_declare_which_ruler_was_used(self):
        est = D.explain([{"speaker": "A", "text": "中" * 30}], self.cfg)
        self.assertEqual(est["rate_source"], "standard")
        self.assertEqual(est["rows"][0]["rate_source"], "standard")
        self.assertEqual(est["rows"][0]["k"], D.STANDARD_K)

    def test_ratio_is_voice_speed_over_standard(self):
        calib = D.Calibration(path=os.path.join(
            tempfile.mkdtemp(prefix="pm-ratio-"), "calib.json"))
        # 没量过就是没量过：不借别的音色顶替，也不拿 1.00 冒充。
        self.assertIsNone(D.ratio_of(calib, "edge", "va", 1.0))
        calib.groups["edge|va|1.00"] = {"k": D.STANDARD_K * 0.9, "n": 20, "mode": "L1"}
        self.assertAlmostEqual(D.ratio_of(calib, "edge", "va", 1.0), 0.9, places=3)
        # 语速是音色属性、按语速分组的键一起带上，别组的数不能借来用。
        self.assertIsNone(D.ratio_of(calib, "edge", "va", 1.5))


class TestExplain(unittest.TestCase):
    def setUp(self):
        self.cfg = {"tts.engine": "edge", "tts.voice_a": "va", "tts.speed_a": 1.0,
                    "tts.voice_b": "vb", "tts.speed_b": 1.0,
                    "script.target_minutes": 1.0, "audio.pause_between_lines": 0.35}

    def test_rows_expose_chars_and_time(self):
        """需求三：字/时间必须显性可见。"""
        script = [{"speaker": "A", "text": "中" * 10},
                  {"speaker": "B", "text": "中" * 20}]
        est = D.explain(script, self.cfg)
        self.assertEqual(est["line_count"], 2)
        self.assertEqual(est["total_chars"], 30)
        for row in est["rows"]:
            for key in ("chars", "effective", "estimated_seconds", "speed", "text"):
                self.assertIn(key, row)
        self.assertGreater(est["rows"][1]["estimated_seconds"],
                           est["rows"][0]["estimated_seconds"])

    def test_total_includes_pauses(self):
        """总时长 = 逐句语音 + 句间停顿，rows 只承载语音部分。"""
        script = [{"speaker": "A", "text": "中" * 10},
                  {"speaker": "A", "text": "中" * 25}]
        est = D.explain(script, self.cfg)
        speech = sum(r["estimated_seconds"] for r in est["rows"])
        pause = self.cfg["audio.pause_between_lines"]
        self.assertAlmostEqual(est["total_seconds"],
                               speech + pause * (len(script) - 1), places=6)

    def test_deviation_against_target(self):
        # 目标 1 分钟、实际约 6 秒 → 偏差应为明显的负值
        script = [{"speaker": "A", "text": "中" * 30}]
        est = D.explain(script, self.cfg)
        self.assertEqual(est["target_seconds"], 60.0)
        self.assertLess(est["deviation_pct"], 0)


class TestBorrowedStats(unittest.TestCase):
    """未标定的音色借同引擎同语速下已标定组的系数，而不是落回全局默认值。

    默认值（5.0）与本机实测（4.01）差 25%。A 角标过、B 角没标过的时候，两个主持人
    就成了两把尺子量同一份稿子——同一稿的总时长能估出 14% 的差，而这条差最后是记在
    门禁账上的。跨引擎不借：云端与本地模型的语速是两码事。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm-borrow-")
        self.calib = D.Calibration(path=os.path.join(self.tmp, "calib.json"))
        self.calib.groups["edge|甲|1.00"] = {"k": 4.0, "n": 30, "mode": "L1"}

    def test_borrows_within_the_same_engine(self):
        st = self.calib.stats("edge", "乙", 1.0)
        self.assertAlmostEqual(st["k"], 4.0, places=3)
        self.assertEqual(st["borrowed"], "甲")

    def test_borrowed_stats_say_where_they_came_from(self):
        # 借来的数不能冒充量过的数：报告与界面据此才说得清这不是本组实测。
        self.assertIn("borrowed", self.calib.stats("edge", "乙", 1.0))
        self.assertNotIn("borrowed", self.calib.stats("edge", "甲", 1.0))

    def test_does_not_borrow_across_engines(self):
        st = self.calib.stats("qwen3tts", "Serena", 1.0)
        self.assertEqual(st["k"], D.DEFAULT_K)
        self.assertFalse(st["fitted"])

    def test_different_speed_is_not_borrowed(self):
        self.assertEqual(self.calib.stats("edge", "乙", 1.5)["k"], D.DEFAULT_K)

    def test_own_group_beats_borrowed(self):
        self.calib.groups["edge|乙|1.00"] = {"k": 6.0, "n": 10, "mode": "L1"}
        st = self.calib.stats("edge", "乙", 1.0)
        self.assertAlmostEqual(st["k"], 6.0, places=3)
        self.assertNotIn("borrowed", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
