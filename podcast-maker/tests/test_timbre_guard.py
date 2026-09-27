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

"""音色体检：判定分定标回归 + 修复循环行为钉子。

判定分模型不是拍脑袋，是 2c 期六句人工试听定出来的（2026-09-27）：四句耳朵
判「有问题」的必须全报警，两句「可接受」的必须放过——这六句就是这把尺子的
刻度，模型或阈值一动，这里先红。

修复循环用假测量离线跑：真合成已经在 _smoke/probe_seed_retry.py 用真服务端
验过（四问题句 × seed+1/+2/+3，15/16 过线、四句全部首试即过），这里钉的是
循环自身的纪律——首次过线即停、保底温度只在全败后出场、全败保留最优一条
并标记人工审、修复后时长与 actual_seconds 必须回填。
"""

import json
import os
import shutil
import tempfile
import unittest
from collections import deque
from unittest import mock

from podcast_maker import tts_engine

#: 2c 期大美（B 角）基线：129 句实测（F0 中位 / 谱质心 / ffmpeg RMS dB）
BASE = {"f0_mu": 263.7, "f0_sd": 19.85,
        "cent_mu": 1882.79, "cent_sd": 130.81,
        "rms_mu": -27.21, "rms_sd": 0.948}

#: 六句定标刻度：(F0, 谱心, 响度, 期望判定)。0.02 分的边际也是刻度的一部分
#: ——0101（4.32）与 0169（4.20）骑在阈值两侧，正是人耳划的那条线。
CALIBRATION = [
    ("0157", 237.1, 1304.4, -28.77, False),   # 非常明显（谱心 z=-4.4）
    ("0093", 210.0, 1654.9, -24.29, False),   # 非常明显（F0 全场最低）
    ("0135", 216.2, 1532.5, -27.38, False),   # 能听出来
    ("0101", 222.7, 1620.6, -28.77, False),   # 不大对劲（探针曾漏报）
    ("0169", 222.7, 1562.9, -27.00, True),    # 可接受
    ("0233", 226.2, 1698.6, -29.46, True),    # 可接受（响度主导偏移）
]

THRESHOLD = 4.25


def _score(f0, cent, rms):
    return tts_engine._timbre_score(
        {"f0": f0, "cent": cent, "rms": rms}, BASE)


class CalibrationTest(unittest.TestCase):
    """六句定标回归：尺子动了这里先红。"""

    def test_six_ear_verified_lines(self):
        for name, f0, cent, rms, expect_pass in CALIBRATION:
            s = _score(f0, cent, rms)
            self.assertEqual(
                s < THRESHOLD, expect_pass,
                "%s 得分 %.2f，期望%s（阈值 %.2f）"
                % (name, s, "过线" if expect_pass else "报警", THRESHOLD))

    def test_boundary_rides_between_0101_and_0169(self):
        s101 = _score(222.7, 1620.6, -28.77)
        s169 = _score(222.7, 1562.9, -27.00)
        self.assertGreater(s101, THRESHOLD)
        self.assertLess(s169, THRESHOLD)
        self.assertLess(s101 - THRESHOLD, 0.2)   # 阈值就该贴着这条线
        self.assertLess(THRESHOLD - s169, 0.2)


def _fake_cfg(**over):
    cfg = {"tts.engine": "qwen3tts", "tts.timbre_guard": True,
           "tts.timbre_threshold": THRESHOLD, "tts.timbre_seed_retries": 3,
           "tts.timbre_fallback_temp": 0.3,
           "tts.qwen3tts_voice_a": "Vivian", "tts.qwen3tts_voice_b": "Serena",
           "tts.speed_a": 1.0, "tts.speed_b": 1.0}
    cfg.update(over)
    return cfg


def _fake_measurement_factory(sequences):
    """按句号给测量序列：主测量第 0 次，之后每次重试往后排一个，末项无限重复。

    `sequences` 缺某句号就永远量出 GOOD（正常句）。这样「三次换种子都坏、
    保底温度才好」这类时序场景才能被钉住。
    """
    counters = {}

    def factory(bad, good):
        def fake_features(path):
            idx = int(os.path.basename(path).split("_")[0])
            seq = sequences.get(idx, [good])
            n = counters.get(idx, 0)
            counters[idx] = n + 1
            return seq[min(n, len(seq) - 1)]
        return fake_features

    return factory


class RepairLoopTest(unittest.TestCase):
    """修复循环纪律：离线假测量，钉循环不看真服务端。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="timbre_guard_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.n_a = self.n_b = 12          # 每角色 ≥10 句才建基线
        self.bad = (210.0, 1304.4, -28.8)   # 三维都偏：必报警
        self.good = (264.0, 1883.0, -27.2)  # 全带内
        self.files = []
        self.script = []
        for i in range(self.n_a + self.n_b):
            spk = "A" if i < self.n_a else "B"
            p = os.path.join(self.tmp, "%04d_%s.wav" % (i, spk))
            self.files.append(p)
            self.script.append({"speaker": spk, "text": "第%d句" % i})
        self.refs = {"A": {"wav": "a.wav", "text": "x"},
                     "B": {"wav": "b.wav", "text": "x"}}
        self.cfg = _fake_cfg()
        self.report = os.path.join(self.tmp, "timbre_repair.json")

    def _run(self, sequences):
        calls = []

        def fake_synth(text, voice, speed, cfg, degree=None, ref=None,
                       seed_offset=0, temperature=None):
            calls.append({"seed_offset": seed_offset,
                          "temperature": temperature, "voice": voice})
            return b"wav"

        with mock.patch.object(tts_engine, "_timbre_features",
                               _fake_measurement_factory(sequences)(self.bad,
                                                                    self.good)), \
             mock.patch.object(tts_engine, "synth_line", fake_synth), \
             mock.patch.object(tts_engine, "probe_duration",
                               lambda p: 4.5):
            rep = tts_engine.timbre_repair(
                self.files, self.script, self.cfg, log=lambda m: None,
                emotion_level="none", voice_root=self.tmp, refs=self.refs,
                report_path=self.report)
        return rep, calls

    def test_first_pass_fix_stops_early(self):
        rep, calls = self._run({5: [self.bad, self.good]})
        self.assertIn(5, rep["changed"])
        self.assertEqual(len(rep["repairs"]), 1)
        self.assertEqual(rep["repairs"][0]["attempt"], 1)   # 首试即过
        self.assertEqual(rep["exhausted"], [])
        self.assertEqual(len(calls), 1)                     # 过线就停，不多抽
        self.assertEqual(calls[0]["seed_offset"], 1)
        self.assertIsNone(calls[0]["temperature"])          # 温度没动
        self.assertEqual(self.script[5]["actual_seconds"], 4.5)

    def test_fallback_temp_only_after_seed_retries_exhausted(self):
        # 三次换种子都仍报警（中等坏），保底温度那一次才量出好样本
        mid = (210.0, 1500.0, -27.2)   # 谱心+音高仍双偏，分数 ~5.1：报警
        rep, calls = self._run({5: [self.bad, mid, mid, mid, self.good]})
        self.assertIn(5, rep["changed"])
        self.assertEqual(len(calls), 4)
        self.assertEqual([c["seed_offset"] for c in calls], [1, 2, 3, 1])
        self.assertIsNone(calls[0]["temperature"])
        self.assertIsNone(calls[2]["temperature"])
        self.assertAlmostEqual(calls[3]["temperature"], 0.3)  # 第 4 次才保底
        self.assertEqual(rep["repairs"][0]["attempt"], 1)     # 报告里按偏移记

    def test_exhausted_marks_manual_review_and_keeps_best(self):
        # 怎么重出都坏：保留最优一条（分数与原句同为坏档），标记人工审
        rep, calls = self._run({7: [self.bad] * 5})
        self.assertEqual(rep["repairs"], [])
        self.assertEqual(len(rep["exhausted"]), 1)
        self.assertFalse(rep["exhausted"][0]["fixed"])
        self.assertEqual(len(calls), 4)
        with open(self.report, encoding="utf-8") as f:
            report = json.load(f)
        self.assertEqual(len(report["exhausted"]), 1)
        self.assertEqual(report["exhausted"][0]["index"], 7)

    def test_only_flagged_line_touched(self):
        rep, _ = self._run({5: [self.bad, self.good]})
        self.assertEqual(sorted(rep["changed"]), [5])
        for i, p in enumerate(self.files):
            if i != 5:
                self.assertFalse(os.path.exists(p + ".tmp"))

    def test_report_written_with_baseline(self):
        self._run({5: [self.bad, self.good]})
        with open(self.report, encoding="utf-8") as f:
            report = json.load(f)
        self.assertIn("B", report["baselines"])
        self.assertEqual(report["seed_retries"], 3)
        self.assertEqual(len(report["rows"]), self.n_a + self.n_b)

    def test_small_speaker_skipped(self):
        # 每角色句数不足基线下限：不建基线、不点名、不动任何文件
        cfg = _fake_cfg()
        with mock.patch.object(tts_engine, "_timbre_features",
                               _fake_measurement_factory(
                                   {0: [self.bad, self.good]})(self.bad,
                                                               self.good)), \
             mock.patch.object(tts_engine, "probe_duration", lambda p: 4.5):
            rep = tts_engine.timbre_repair(
                self.files[:6], self.script[:6], cfg, log=lambda m: None,
                emotion_level="none", voice_root=self.tmp, refs=self.refs,
                report_path=self.report)
        self.assertIsNone(rep)
        self.assertFalse(os.path.exists(self.report))


class WiringTest(unittest.TestCase):
    """接线钉子：两个分支都必须过体检，服务端两个旋钮必须存在。"""

    def _src(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()

    def test_repair_wiring_follows_full_rebuild(self):
        """体检挂点随「合成段全量」收口：synthesize 尾部调用，pipeline 不再有复用分支。

        合成段改成纯函数全量重建后，复用分支（连同它在 pipeline 里那次体检
        调用）一并删除；体检的唯一挂点是 synthesize() 尾部——全量合成后必跑，
        不存在「复用路径不体检」的第二种质量。
        """
        src = self._src(os.path.join("podcast_maker", "tts_engine.py"))
        self.assertIn("rep = timbre_repair(files, script, cfg, log=log",
                      src)                       # synthesize 尾部唯一挂点
        psrc = self._src(os.path.join("podcast_maker", "pipeline.py"))
        self.assertNotIn("timbre_repair", psrc)  # pipeline 不再各自调用
        self.assertNotIn("复用已有音频", psrc)   # 文件数判据分支已删

    def test_service_supports_seed_offset_and_temperature(self):
        src = self._src(os.path.join("tts_service", "serve.py"))
        self.assertIn("seed_offset", src)
        self.assertIn("temperature", src)

    def test_config_keys_exist(self):
        src = self._src(os.path.join("podcast_maker", "config_manager.py"))
        for key in ("tts.timbre_guard", "tts.timbre_threshold",
                    "tts.timbre_seed_retries", "tts.timbre_fallback_temp"):
            self.assertIn('"%s"' % key, src)


if __name__ == "__main__":
    unittest.main()
