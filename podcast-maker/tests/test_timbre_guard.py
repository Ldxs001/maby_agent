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

"""音色体检：三判据定标回归 + 修复循环行为钉子。

判定模型不是拍脑袋，是人工试听定出来的，三条判据各管一族病：
① 四维严苛分（谱心 1.0 + 音高 0.8 + 响度 0.4 + 存在感 0.5 单边，4.25）：
   2c 六句定标 + 2da 0054 补存在感维；② 暗淡子分（F0 下偏 + 谱心变暗 +
   0.6×基音抖动，4.9）：ep3 58 句 a2 候选——F0 向下还不稳 = 病态嗓；
③ 身份下限（F0 ≤ -2.4σ，或 F0 ≤ -2.0σ 且低频占比 ≥ +1.5σ）：大美是单薄
   女高音，音高掉了 + 低频鼓起来 = 滑向中音、不是本人；掉了但依然单薄 =
   嗓子累，放行。15 个耳标样本（9 坏 6 好）三规则合验 15/15。

修复循环用假测量离线跑：真合成已经在 _smoke/probe_seed_retry.py 用真服务端
验过，这里钉的是循环自身的纪律——三判据全过即停、保底（复用同角色最近一条
未点名句的种子，温度不动，无条件落盘）只在全败后出场、
全败保留最优一条并标记人工审、修复后时长与 actual_seconds 必须回填。
"""

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from collections import deque
from unittest import mock

from podcast_maker import tts_engine

#: 2c 期大美（B 角）基线：129 句实测（F0 中位 / 谱质心 / ffmpeg RMS dB /
#: 存在感频带占比——量自修复前音频，四句修复句用 44k a0 存档）
BASE = {"f0_mu": 263.7, "f0_sd": 19.85,
        "cent_mu": 1882.79, "cent_sd": 130.81,
        "rms_mu": -27.21, "rms_sd": 0.948,
        "pres_mu": 0.02209, "pres_sd": 0.010614}

#: 七句定标刻度：(F0, 谱心, 响度, 存在感, 期望判定)。0.02 分的边际也是刻度的
#: 一部分——0101（4.32）与 0169（4.20）骑在阈值两侧，正是人耳划的那条线。
#: 2c 六句的存在感全在基线下侧或均值附近，第 4 维不参与其判定（权重项为 0），
#: 定标结论原样保持；点名 2da 0054 的刻度单列在 PresenceBandTest。
CALIBRATION = [
    ("0157", 237.1, 1304.4, -28.77, 0.007812, False),   # 非常明显（谱心 z=-4.4）
    ("0093", 210.0, 1654.9, -24.29, 0.032810, False),   # 非常明显（F0 全场最低）
    ("0135", 216.2, 1532.5, -27.38, 0.018010, False),   # 能听出来
    ("0101", 222.7, 1620.6, -28.77, 0.011532, False),   # 不大对劲（探针曾漏报）
    ("0169", 222.7, 1562.9, -27.00, 0.004392, True),    # 可接受
    ("0233", 226.2, 1698.6, -29.46, 0.014155, True),    # 可接受（响度主导偏移）
]

THRESHOLD = 4.25
DULL_THRESHOLD = 4.9
ID_DEEP, ID_FULL, ID_S300 = 2.4, 2.0, 1.5


def _score(f0, cent, rms, pres=None):
    return tts_engine._timbre_score(
        {"f0": f0, "cent": cent, "rms": rms, "pres": pres}, BASE)


class CalibrationTest(unittest.TestCase):
    """2c 六句定标回归：尺子动了这里先红。"""

    def test_six_ear_verified_lines(self):
        for name, f0, cent, rms, pres, expect_pass in CALIBRATION:
            s = _score(f0, cent, rms, pres)
            self.assertEqual(
                s < THRESHOLD, expect_pass,
                "%s 得分 %.2f，期望%s（阈值 %.2f）"
                % (name, s, "过线" if expect_pass else "报警", THRESHOLD))

    def test_boundary_rides_between_0101_and_0169(self):
        s101 = _score(222.7, 1620.6, -28.77, 0.011532)
        s169 = _score(222.7, 1562.9, -27.00, 0.004392)
        self.assertGreater(s101, THRESHOLD)
        self.assertLess(s169, THRESHOLD)
        self.assertLess(s101 - THRESHOLD, 0.2)   # 阈值就该贴着这条线
        self.assertLess(THRESHOLD - s169, 0.2)


#: 2da 期 B 角四维基线：实测（30/102/166 用 attempt0 重构、0054 用坏句原盘，
#: 与 4 维验证回放同集合；_smoke/probe_presence_consts.py 量取）
BASE2DA = {"f0_mu": 259.083369, "f0_sd": 25.004402,
           "cent_mu": 1855.170858, "cent_sd": 205.623577,
           "rms_mu": -27.041119, "rms_sd": 1.240858,
           "pres_mu": 0.022269, "pres_sd": 0.008681}


def _score2da(f0, cent, rms, pres=None):
    return tts_engine._timbre_score(
        {"f0": f0, "cent": cent, "rms": rms, "pres": pres}, BASE2DA)


class PresenceBandTest(unittest.TestCase):
    """第 4 维钉子：存在感频带抓频谱形状重分布，2da 0054 是活体刻度。"""

    def test_presence_band_catches_shape_redistribution(self):
        """0054 原句：三维 3.45 放行、四维 5.67 点名——第 4 维是唯一判据。"""
        s3 = _score2da(230.77, 1583.01, -30.84, None)      # 不带存在感项
        s4 = _score2da(230.77, 1583.01, -30.84, 0.060724)  # 0054 a0 实测
        self.assertLess(s3, THRESHOLD)
        self.assertGreaterEqual(s4, THRESHOLD)

    def test_repaired_line_passes(self):
        """0054 seed+1 重出（a1 实测）：存在感回带内，四维 1.27 过线。"""
        s = _score2da(266.67, 1720.98, -28.20, 0.022101)
        self.assertLess(s, THRESHOLD)

    def test_presence_term_is_one_sided(self):
        """占比偏低（变闷）不加权——只有刺耳方向点名，与谱心变亮不出戏同理。"""
        f0m, cm, rm = BASE2DA["f0_mu"], BASE2DA["cent_mu"], BASE2DA["rms_mu"]
        s_low = _score2da(f0m, cm, rm, 0.0)          # 远低于均值
        s_mid = _score2da(f0m, cm, rm, BASE2DA["pres_mu"])  # 恰在均值
        self.assertAlmostEqual(s_low, 0.0, places=9)
        self.assertAlmostEqual(s_mid, 0.0, places=9)


#: 暗淡子分与身份下限的活体刻度（ep3 期 B 角，2026-09-30 复听定标）。
#: 用合成基线/测量值复刻实测 z 分——数值本身来自产线口径的测量，见
#: _smoke/probe_identity_features.py 与 ear_review/identity_z.json。
DULL_BASE = {"f0_mu": 264.0, "f0_sd": 20.0,
             "cent_mu": 1880.0, "cent_sd": 130.0,
             "jit_mu": 7.0, "jit_sd": 1.0,
             "s300_mu": 0.30, "s300_sd": 0.10,
             "rms_mu": -27.0, "rms_sd": 1.0,
             "pres_mu": 0.022, "pres_sd": 0.0106}
#: a2：F0 下偏 1.70σ + 谱心暗 1.38σ + 抖动 3.91σ → 暗淡 5.42，坏
A2 = {"f0": 264.0 - 1.70 * 20.0, "cent": 1880.0 - 1.38 * 130.0,
      "jit": 7.0 + 3.91 * 1.0, "s300": 0.30, "rms": -27.0, "pres": 0.022}
#: ep3-0169（主人判「最好」）：暗淡 3.63，放行
EP3_0169 = {"f0": 264.0 - 0.94 * 20.0, "cent": 1880.0 - 2.17 * 130.0,
            "jit": 7.0 + 0.85 * 1.0, "s300": 0.30, "rms": -27.0, "pres": 0.022}


class DullScoreTest(unittest.TestCase):
    """暗淡子分钉子：抓发抖病态嗓，ep3 0169 不冤。"""

    def test_a2_trembling_voice_is_caught(self):
        s = tts_engine._dull_score(A2, DULL_BASE)
        self.assertGreaterEqual(s, DULL_THRESHOLD)
        self.assertAlmostEqual(s, 5.42, delta=0.02)

    def test_best_line_passes(self):
        s = tts_engine._dull_score(EP3_0169, DULL_BASE)
        self.assertLess(s, DULL_THRESHOLD)


class IdentityRuleTest(unittest.TestCase):
    """身份下限钉子：音高掉了 + 低频鼓起来 = 别人；掉了但单薄 = 她累了。"""

    def _flag(self, f0, s300):
        row = {"f0": f0, "s300": s300, "cent": DULL_BASE["cent_mu"],
               "rms": -27.0, "pres": 0.022, "jit": 7.0}
        return tts_engine._identity_flag(row, DULL_BASE,
                                         ID_DEEP, ID_FULL, ID_S300)

    def test_deep_f0_drop_is_another_person(self):
        """ep3-009（a1 现成片）：F0 -2.75σ，掉破深降线。"""
        self.assertTrue(self._flag(DULL_BASE["f0_mu"] - 2.75 * 20.0, 0.30))

    def test_low_pitch_with_full_low_band_is_another_person(self):
        """2c-0101（04 号）：同样 -2.26σ，低频 +2.27σ → 滑向中音。"""
        self.assertTrue(self._flag(DULL_BASE["f0_mu"] - 2.26 * 20.0,
                                   0.30 + 2.27 * 0.10))

    def test_low_pitch_but_thin_is_still_her(self):
        """2c-0169（05 号）：F0 -2.26σ 但低频 +0.09σ → 嗓子累，放行。"""
        self.assertFalse(self._flag(DULL_BASE["f0_mu"] - 2.26 * 20.0,
                                    0.30 + 0.09 * 0.10))

    def test_full_low_band_alone_is_fine(self):
        """ep3-0169（15 号）：低频 +2.12σ 但 F0 -0.95σ → 放行。"""
        self.assertFalse(self._flag(DULL_BASE["f0_mu"] - 0.95 * 20.0,
                                    0.30 + 2.12 * 0.10))


class CaptureTest(unittest.TestCase):
    """三判据合验：检测与候选验收共用同一把尺。"""

    def _capture(self, row):
        row = dict(row, score=tts_engine._timbre_score(row, DULL_BASE),
                   dull=tts_engine._dull_score(row, DULL_BASE))
        return tts_engine._timbre_capture(row, DULL_BASE, THRESHOLD,
                                          DULL_THRESHOLD, ID_DEEP,
                                          ID_FULL, ID_S300)

    def test_clean_row_passes_all_three(self):
        good = {"f0": 264.0, "cent": 1880.0, "rms": -27.0, "pres": 0.022,
                "s300": 0.30, "jit": 7.0}
        self.assertEqual(self._capture(good), [])

    def test_trembling_row_caught_by_dull(self):
        self.assertTrue(self._capture(A2))

    def test_deep_drop_caught_by_identity(self):
        row = {"f0": DULL_BASE["f0_mu"] - 2.75 * 20.0, "cent": 1880.0,
               "rms": -27.0, "pres": 0.022, "s300": 0.30, "jit": 7.0}
        why = self._capture(row)
        self.assertTrue(any(w.startswith("身份") for w in why))


def _fake_cfg(**over):
    cfg = {"tts.engine": "qwen3tts", "tts.timbre_guard": True,
           "tts.timbre_threshold": THRESHOLD, "tts.timbre_seed_retries": 3,
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
        # 六维元组：F0 / 谱心 / 响度 / 存在感 / 低频占比 / 抖动。
        # bad 三维+抖动全偏：任一判据都能抓；good 全带内。
        self.bad = (210.0, 1304.4, -28.8, 0.022, 0.030, 11.0)
        self.good = (264.0, 1883.0, -27.2, 0.022, 0.020, 7.0)
        self.files = []
        self.script = []
        for i in range(self.n_a + self.n_b):
            spk = "A" if i < self.n_a else "B"
            p = os.path.join(self.tmp, "%04d_%s.wav" % (i, spk))
            self.files.append(p)
            self.script.append({"speaker": spk, "text": "第%d句" % i})
        self.refs = {"A": {"wav": os.path.join(self.tmp, "a.wav"), "text": "x"},
                     "B": {"wav": os.path.join(self.tmp, "b.wav"), "text": "x"}}
        # 保底复用种子要读参考音频算内容指纹（与服务端 voice_key_for 同口径），
        # 所以这里的档案 wav 必须真实存在——内容无所谓，字节固定即可。
        for r in self.refs.values():
            with open(r["wav"], "wb") as f:
                f.write(b"ref-bytes")
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

    def test_fallback_reuses_nearest_clean_seed(self):
        # 三次换种子都仍报警，第 4 招保底：复用同角色最近一条未点名句的种子
        #（温度同样不动，只换抽样点），这一招量出好样本即转正
        mid = (210.0, 1500.0, -27.2, 0.022, 0.020, 7.0)   # 谱心+音高双偏：报警
        rep, calls = self._run({5: [self.bad, mid, mid, mid, self.good]})
        self.assertIn(5, rep["changed"])
        self.assertEqual([c["seed_offset"] for c in calls[:3]], [1, 2, 3])
        vk = tts_engine._voice_fingerprint(self.refs["A"])
        exp = ((tts_engine._line_seed(vk, "第4句")
                - tts_engine._line_seed(vk, "第5句")) % (1 << 32))
        self.assertEqual(calls[3]["seed_offset"], exp)      # 第 4 次复用句 4 种子
        self.assertTrue(all(c["temperature"] is None for c in calls))
        r0 = rep["repairs"][0]
        self.assertEqual(r0["attempt"], exp)                # 报告里按偏移记
        self.assertEqual(r0["via"], "reuse")
        self.assertEqual(r0["fallback"]["ref_index"], 4)    # 离 5 最近且未点名

    def test_fallback_lands_even_if_failing_and_marks_review(self):
        # 三次换种子 + 保底全部仍报警：保底不管过不过都落盘，标记人工审
        rep, calls = self._run({7: [self.bad] * 4})
        self.assertEqual(len(calls), 4)                     # 3 次重试 + 1 次保底
        self.assertEqual(rep["repairs"], [])
        self.assertEqual(len(rep["exhausted"]), 1)
        rec = rep["exhausted"][0]
        self.assertFalse(rec["fixed"])
        self.assertEqual(rec["via"], "fallback")
        self.assertEqual(rec["fallback"]["ref_index"], 6)   # 离 7 最近的未点名句
        with open(self.files[7], "rb") as f:
            self.assertEqual(f.read(), b"wav")              # 保底那条已在盘上

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

    def test_service_has_embed_slot(self):
        """嵌入模型槽：wavlm 槽位 + /embed 端点 + 懒加载，与 TTS 同一套架构。"""
        src = self._src(os.path.join("tts_service", "serve.py"))
        self.assertIn("SV_MODEL_ID", src)
        self.assertIn("microsoft/wavlm-base-plus-sv", src)
        self.assertIn('"/embed"', src)
        self.assertIn("WavLMForXVector", src)
        self.assertIn("class SVEngine", src)

    def test_config_keys_exist(self):
        src = self._src(os.path.join("podcast_maker", "config_manager.py"))
        for key in ("tts.timbre_guard", "tts.timbre_threshold",
                    "tts.timbre_seed_retries",
                    "tts.timbre_dull_threshold",
                    "tts.timbre_identity_f0_deep",
                    "tts.timbre_identity_f0_full",
                    "tts.timbre_identity_s300",
                    "tts.timbre_cent_dark", "tts.timbre_s300_band",
                    "tts.timbre_sv_guard", "tts.timbre_sv_sim",
                    "tts.timbre_sv_min_seconds"):
            self.assertIn('"%s"' % key, src)
        self.assertNotIn("timbre_fallback_temp", src)   # 保底温度已废，改复用种子

    def test_ui_group_carries_new_knobs(self):
        """体检滑杆组带新旋钮：否则界面上没人调得了线。"""
        src = self._src(os.path.join("podcast_maker", "web_ui.py"))
        for key in ("tts.timbre_cent_dark", "tts.timbre_s300_band",
                    "tts.timbre_sv_guard", "tts.timbre_sv_sim",
                    "tts.timbre_sv_min_seconds"):
            self.assertIn("'%s'" % key, src)

    def test_sv_model_in_user_setup_chain(self):
        """wavlm 必须在用户的完整安装链里：下载清单、安装表、验收三处都不能缺。

        缺任何一处，用户机器上就没有这份权重 —— 嵌入维永远缺席，而配置里
        该维默认是开着的。手工拷贝不算搭建。
        """
        fetch = self._src(os.path.join("tts_service", "fetch_model.py"))
        self.assertIn('"microsoft/wavlm-base-plus-sv"', fetch)
        setup = self._src(os.path.join("tts_service", "setup_env.py"))
        self.assertIn('"microsoft/wavlm-base-plus-sv"', setup)
        check = self._src(os.path.join("tts_service", "check.py"))
        self.assertIn("post_embed", check)
        self.assertIn("/embed", check)


# ------------------------------------------------------------------ ②期增补
# 男声 22 句耳标考卷（期1：20 违和 + 2 像基准）+ 9 期回放定出的增补判据。
# 考卷结论：四族句级探针里唯一抓住严重音色离群的是嵌入（尾部分离），
# 频谱形态抓「缺亮音 / 低频异常」两个方向，音高 d 对男声定不出线
# （#019 d=4.8 违和 vs #057 d=3.4 像，同区间反判决）→ 降为记录项。

class CentTruncationTest(unittest.TestCase):
    """谱心项单向截断：变亮不出戏，不给好方向发抵扣券。

    旧实现亮漂以负分与音高偏离相消——男声 0001_B 谱心 +7.3st 亮漂把
    0.8×音高项抵剩 0.52 分，病句过线。截断后四维只升不降（ Female 定标句
    全部谱心偏暗，CalibrationTest 回归不受影响——尺子只朝一个方向动）。
    """

    def test_bright_cent_contributes_zero(self):
        s = tts_engine._timbre_score(
            {"f0": BASE["f0_mu"], "cent": BASE["cent_mu"] + 3 * BASE["cent_sd"],
             "rms": BASE["rms_mu"], "pres": BASE["pres_mu"]}, BASE)
        self.assertAlmostEqual(s, 0.0, places=9)

    def test_dark_cent_unchanged(self):
        """变暗方向照旧计权——截断只删抵扣，不动定标。"""
        z = 2.0
        s = tts_engine._timbre_score(
            {"f0": BASE["f0_mu"], "cent": BASE["cent_mu"] - z * BASE["cent_sd"],
             "rms": BASE["rms_mu"], "pres": BASE["pres_mu"]}, BASE)
        self.assertAlmostEqual(s, z, places=9)


class SpecformRuleTest(unittest.TestCase):
    """频谱形态钉子：缺亮音单向、低频双向。基线用 DULL_BASE。"""

    def _flags(self, cent=None, s300=None):
        row = {"f0": 264.0, "cent": cent, "rms": -27.0, "pres": 0.022,
               "s300": s300, "jit": 7.0}
        return tts_engine._specform_flags(row, DULL_BASE)

    def test_dark_cent_flagged(self):
        why = self._flags(cent=DULL_BASE["cent_mu"] - 2.0 * DULL_BASE["cent_sd"],
                          s300=DULL_BASE["s300_mu"])
        self.assertTrue(any(w.startswith("缺亮音") for w in why))

    def test_bright_cent_not_flagged(self):
        """变亮不出戏——缺亮音只计变暗方向。"""
        why = self._flags(cent=DULL_BASE["cent_mu"] + 4.0 * DULL_BASE["cent_sd"],
                          s300=DULL_BASE["s300_mu"])
        self.assertEqual(why, [])

    def test_low_band_high_flagged(self):
        """低频鼓包 = 模型加了基准没有的低频男声（期1 全批 59 句零负值的
        批次现象，句级仍要抓极端）。"""
        why = self._flags(cent=DULL_BASE["cent_mu"],
                          s300=DULL_BASE["s300_mu"] + 2.0 * DULL_BASE["s300_sd"])
        self.assertTrue(any(w.startswith("低频+") for w in why))

    def test_low_band_low_flagged(self):
        """低频塌陷 = 低音缺失，双向的另一头。"""
        why = self._flags(cent=DULL_BASE["cent_mu"],
                          s300=DULL_BASE["s300_mu"] - 2.0 * DULL_BASE["s300_sd"])
        self.assertTrue(any(w.startswith("低频-") for w in why))

    def test_mid_row_clean(self):
        why = self._flags(cent=DULL_BASE["cent_mu"], s300=DULL_BASE["s300_mu"])
        self.assertEqual(why, [])


class SvGateTest(unittest.TestCase):
    """嵌入维裁决钉子：时长门 + 阈值 + 不可用缺席。"""

    def test_unavailable_absent_not_flagged(self):
        """嵌入不可用（sv=None）→ 不参与，既不误报也不假装查过。"""
        self.assertIsNone(tts_engine._sv_flag(None, 10.0))

    def test_below_line_flagged(self):
        self.assertTrue(tts_engine._sv_flag(0.85, 10.0))

    def test_above_line_passes(self):
        self.assertFalse(tts_engine._sv_flag(0.95, 10.0))

    def test_short_line_skipped(self):
        """时长门：短句 sim 系统性偏低（r=0.49），<4s 不参与嵌入点名。"""
        self.assertIsNone(tts_engine._sv_flag(0.85, 3.9))

    def test_short_line_still_caught_by_pure_code(self):
        """时长门不是免检金牌：短句仍走缺亮音/低频双向等纯代码判据
        （期1 #067 实测被缺亮音 -2.9σ 与音高双抓，门零漏损）。"""
        row = {"cent": DULL_BASE["cent_mu"] - 3.0 * DULL_BASE["cent_sd"],
               "s300": DULL_BASE["s300_mu"], "sv": 0.60, "seconds": 2.0}
        why = tts_engine._timbre_capture(row, DULL_BASE, THRESHOLD,
                                         DULL_THRESHOLD, ID_DEEP,
                                         ID_FULL, ID_S300)
        self.assertTrue(any(w.startswith("缺亮音") for w in why))
        self.assertFalse(any(w.startswith("嵌入") for w in why))


class CosineTest(unittest.TestCase):

    def test_identical_orthogonal(self):
        self.assertAlmostEqual(tts_engine._cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(tts_engine._cosine([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_unnormalized_input_ok(self):
        """不依赖服务端归一：客户端自己除模长。"""
        self.assertAlmostEqual(
            tts_engine._cosine([3.0, 0.0], [7.0, 0.0]), 1.0)


class EmaOffsetTest(unittest.TestCase):
    """每期音高基线 o 的三道闸：中位冷启动 / 更新门 / 硬钳位。"""

    REF = 135.6

    @staticmethod
    def _rows(*semis):
        return [{"f0": 135.6 * (2.0 ** (s / 12.0))} for s in semis]

    def test_cold_start_median_robust_to_one_outlier(self):
        """冷启动 10 句里混 1 句 +7st：中位数纹丝不动，更新门再拒收后续。"""
        rows = self._rows(*([1.0] * 4 + [7.2] + [1.0] * 10))
        o, warn = tts_engine._ema_offset(rows, self.REF)
        self.assertFalse(warn)
        self.assertLess(abs(o - 1.0), 0.2)

    def test_update_gate_rejects_far_rows(self):
        """+7st 的句不许把 o 拽走：离 o 超 1.5st 一律拒收。"""
        rows = self._rows(*([1.0] * 12 + [7.2] * 5))
        o, warn = tts_engine._ema_offset(rows, self.REF)
        self.assertLess(abs(o - 1.0), 0.3)
        self.assertFalse(warn)

    def test_clamp_caps_and_warns(self):
        """整期基线 +5st：爆钳位 → 封顶 + 告警（报警不吸收）。"""
        rows = self._rows(*([5.0] * 15))
        o, warn = tts_engine._ema_offset(rows, self.REF)
        self.assertTrue(warn)
        self.assertAlmostEqual(o, 2.5)

    def test_few_rows_median_fallback(self):
        """合格句不足 warm：现有样本中位兜底，同样受钳位管辖。"""
        o, warn = tts_engine._ema_offset(self._rows(1.0, 1.1, 0.9), self.REF)
        self.assertAlmostEqual(o, 1.0, delta=0.01)
        self.assertFalse(warn)


class _FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class EmbedClientTest(unittest.TestCase):
    """嵌入客户端钉子：解析形状 + 不可用降级（不静默）。"""

    CFG = {"tts.qwen3tts_host": "127.0.0.1", "tts.qwen3tts_port": 9880}

    def test_parses_embeddings(self):
        payload = {"ok": True, "embeddings": {"a.wav": [1.0, 0.0],
                                              "b.wav": [0.0, 1.0]}}
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(payload)):
            m = tts_engine._service_embed(["a.wav", "b.wav"], self.CFG,
                                          log=lambda s: None)
        self.assertEqual(m, {"a.wav": [1.0, 0.0], "b.wav": [0.0, 1.0]})

    def test_unreachable_returns_none(self):
        """服务没起：返回 None（调用方标「嵌入缺席」），绝不抛异常炸体检。"""
        def boom(req, timeout=None):
            raise urllib.error.URLError("connection refused")
        logs = []
        with mock.patch("urllib.request.urlopen", boom):
            m = tts_engine._service_embed(["a.wav"], self.CFG, log=logs.append)
        self.assertIsNone(m)
        self.assertTrue(any("嵌入维不可用" in s for s in logs))  # 不静默

    def test_error_payload_returns_none(self):
        payload = {"ok": False, "error": "模型加载失败"}
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout=None: _FakeResp(payload)):
            m = tts_engine._service_embed(["a.wav"], self.CFG,
                                          log=lambda s: None)
        self.assertIsNone(m)


class EmbedFlaggedRepairTest(unittest.TestCase):
    """端到端：嵌入维点名 → 候选必须同样过嵌入才转正。

    离线假嵌入：参考向量 u=(1,0)，句 5 向量 v=(0.5,0.5)（cos≈0.71 < 0.90）
    → 仅嵌入维点名；候选量回 u → 首试转正。嵌入不可用的降级路径由
    RepairLoopTest 全组顺带覆盖（真 _service_embed 连不上 → 全体缺席）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="timbre_sv_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bad = (210.0, 1304.4, -28.8, 0.022, 0.030, 11.0)
        self.good = (264.0, 1883.0, -27.2, 0.022, 0.020, 7.0)
        self.files, self.script = [], []
        for i in range(24):
            spk = "A" if i < 12 else "B"
            self.files.append(os.path.join(self.tmp, "%04d_%s.wav" % (i, spk)))
            self.script.append({"speaker": spk, "text": "第%d句" % i})
        self.refs = {"A": {"wav": os.path.join(self.tmp, "a.wav"), "text": "x"},
                     "B": {"wav": os.path.join(self.tmp, "b.wav"), "text": "x"}}
        for r in self.refs.values():
            with open(r["wav"], "wb") as f:
                f.write(b"ref-bytes")
        self.cfg = _fake_cfg()
        self.report = os.path.join(self.tmp, "timbre_repair.json")
        self.u, self.v = [1.0, 0.0], [0.5, 0.5]
        self.embed_calls = []

    def _fake_embed(self, paths, cfg, log=None):
        self.embed_calls.append(list(paths))
        if self.embed_calls_n() == 1:           # 批量：参考 + 全部句子
            out = {p: self.v if p.endswith("0005_A.wav") else self.u
                   for p in paths}
        else:                                    # 候选单嵌：量回正常
            out = {p: self.u for p in paths}
        return out

    def embed_calls_n(self):
        return len(self.embed_calls)

    def test_sv_only_flag_repaired_and_recorded(self):
        with mock.patch.object(tts_engine, "_timbre_features",
                               _fake_measurement_factory(
                                   {5: [self.good, self.good]})(
                                   self.bad, self.good)), \
             mock.patch.object(tts_engine, "synth_line", lambda *a, **k: b"wav"), \
             mock.patch.object(tts_engine, "probe_duration", lambda p: 4.5), \
             mock.patch.object(tts_engine, "_service_embed", self._fake_embed):
            rep = tts_engine.timbre_repair(
                self.files, self.script, self.cfg, log=lambda m: None,
                emotion_level="none", voice_root=self.tmp, refs=self.refs,
                report_path=self.report)
        self.assertEqual(len(rep["repairs"]), 1)
        self.assertEqual(rep["repairs"][0]["index"], 5)
        with open(self.report, encoding="utf-8") as f:
            report = json.load(f)
        self.assertTrue(report["sv"]["available"])
        row5 = next(r for r in report["rows"] if r["index"] == 5)
        self.assertLess(row5["sv"], 0.90)
        self.assertTrue(any(w.startswith("嵌入") for w in row5["why"]))
        # 两次调用：批量（参考+全部句）与候选单嵌
        self.assertEqual(len(self.embed_calls), 2)


if __name__ == "__main__":
    unittest.main()
