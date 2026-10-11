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

"""标准标尺试听 / 标定按钮 / BGM 混合试听 / structured-writer 开关归项目 的钉子。

钉四件事：
1. 标准标尺＝音频＋实测语速成对落盘，A / B 各一条（各念自己的 ref.txt），
   合成一次后只读；
2. A/B 试听换算 = 滑杆倍率 × 该音色实测语速 ÷ **该角色自己那条尺子**的句子
   口径语速，没有实测就停下；
3. 标定按钮：拿 ref_base 当参考、念角色自己的 ref.txt 逐句现测进校准表
   （与成片校准同一套机制），两种引擎都得量；
4. structured-writer 开关归项目字段（画地图逐次决定），全局配置里没有它。
"""

import base64
import json
import os
import struct
import sys
import tempfile
import unittest
import wave
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import (audio_engine, duration_model, tts_engine,  # noqa: E402
                           web_ui)
from podcast_maker.project_store import EDITABLE, PROJECT_CONFIG_MAP  # noqa: E402


def _write_wav(path, seconds=1.0, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * __import__("math").sin(i * 0.05)))
            for i in range(int(rate * seconds)))
        w.writeframes(frames)


def _write_seg_wav(path, segs, rate=8000):
    """拼一段单声道 wav：segs = [(秒, 是否出声), ...]——造带已知停顿的样本。"""
    import math
    buf = bytearray()
    for sec, loud in segs:
        for i in range(int(rate * sec)):
            buf += struct.pack("<h", int(12000 * math.sin(i * 0.05)) if loud else 0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(buf))


class TestAtempoFactors(unittest.TestCase):
    def test_within_single_segment(self):
        self.assertEqual(audio_engine._atempo_factors(1.5), [1.5])

    def test_fast_split(self):
        f = audio_engine._atempo_factors(4.0)
        self.assertEqual(f, [2.0, 2.0])
        r = 1.0
        for x in f:
            r *= x
        self.assertAlmostEqual(r, 4.0)

    def test_slow_split(self):
        f = audio_engine._atempo_factors(0.25)
        self.assertEqual(f, [0.5, 0.5])
        r = 1.0
        for x in f:
            r *= x
        self.assertAlmostEqual(r, 0.25)

    def test_rejects_nonpositive(self):
        with self.assertRaises(audio_engine.AudioError):
            audio_engine.speed_shift("x", "y", 0)


class TestSpeedShiftReal(unittest.TestCase):
    def test_halves_duration_at_2x(self):
        """真跑 ffmpeg：2.0 倍速后时长减半。ffmpeg 缺席则跳过。"""
        try:
            audio_engine.ffmpeg_bin()
        except audio_engine.AudioError:
            self.skipTest("ffmpeg 不可用")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.wav")
            out = os.path.join(td, "out.wav")
            _write_wav(src, seconds=2.0)
            audio_engine.speed_shift(src, out, 2.0)
            with wave.open(out) as w:
                dur = w.getnframes() / w.getframerate()
            self.assertAlmostEqual(dur, 1.0, delta=0.05)


class TestStandardRuler(unittest.TestCase):
    """标尺＝音频＋实测语速成对落盘；合成一次后只读；残档重新生成。"""

    def _ruler(self, td, synth_calls, probe_sec=5.0):
        def synth(text):
            synth_calls.append(text)
            return b"fake-wav"
        with mock.patch("podcast_maker.audio_engine.probe_duration_safe",
                        return_value=probe_sec):
            return tts_engine.standard_ruler(td, synth)

    def test_synth_once_then_read_cache(self):
        """只合成一次：第二次调用直接读盘，synth 不再被调。"""
        calls = []
        with tempfile.TemporaryDirectory() as td:
            r1 = self._ruler(td, calls)
            r2 = self._ruler(td, calls)
            self.assertEqual(len(calls), 1)
            self.assertEqual(r1["path"], r2["path"])
            self.assertEqual(r1["k"], r2["k"])
            # 标尺文本固定：合成参数里就是那三句、原速由闭包定（不在本层）
            self.assertEqual(calls[0], tts_engine.STANDARD_RULER_TEXT)
            self.assertEqual(calls[0],
                             "".join(tts_engine.CALIBRATION_LINES))

    def test_k_is_measured_chars_over_seconds(self):
        """落盘的 k = 有效字 ÷ 实测秒——尺子的刻度是量出来的，不是填的。"""
        calls = []
        with tempfile.TemporaryDirectory() as td:
            r = self._ruler(td, calls, probe_sec=5.0)
            eff = duration_model.effective_chars(tts_engine.STANDARD_RULER_TEXT)
            self.assertAlmostEqual(r["k"], round(eff / 5.0, 4))
            with open(os.path.join(td, tts_engine.RULER_JSON),
                      encoding="utf-8") as f:
                meta = json.load(f)
            self.assertAlmostEqual(meta["k"], r["k"])

    def test_wav_without_json_regenerates(self):
        """只有音频没有配套 json（旧版 Edge 残档）＝没有标尺，重新生成。"""
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, tts_engine.RULER_WAV), "wb") as f:
                f.write(b"old-edge-ruler")
            r = self._ruler(td, calls)
            self.assertEqual(len(calls), 1)
            self.assertEqual(r["k"], round(
                duration_model.effective_chars(
                    tts_engine.STANDARD_RULER_TEXT) / 5.0, 4))

    def test_broken_json_regenerates(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, tts_engine.RULER_WAV), "wb") as f:
                f.write(b"x")
            with open(os.path.join(td, tts_engine.RULER_JSON), "w",
                      encoding="utf-8") as f:
                f.write("{not json")
            self._ruler(td, calls)
            self.assertEqual(len(calls), 1)

    def test_synth_failure_leaves_no_ruler(self):
        """合成失败照实报错，不写半个标尺、不返回假音频。"""
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(tts_engine.TTSError):
                tts_engine.standard_ruler(td, lambda t: (_ for _ in ()).throw(
                    tts_engine.TTSError("服务不在")))
            self.assertFalse(os.path.exists(os.path.join(td, tts_engine.RULER_WAV)))
            self.assertFalse(os.path.exists(os.path.join(td, tts_engine.RULER_JSON)))

    def test_read_ruler_never_builds(self):
        """只读取用：没有标尺就是 None，不合成、不写盘。"""
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(tts_engine.read_ruler(td))
            self.assertFalse(os.path.exists(os.path.join(td, tts_engine.RULER_WAV)))

    def test_read_ruler_returns_cached(self):
        """标尺在盘上且 k>0：只读取回它，synth 一次都不再调。"""
        calls = []
        with tempfile.TemporaryDirectory() as td:
            r = self._ruler(td, calls, probe_sec=5.0)
            self.assertEqual(len(calls), 1)
            again = tts_engine.read_ruler(td)
            self.assertIsNotNone(again)
            self.assertEqual(again["k"], r["k"])
            self.assertEqual(len(calls), 1)   # 只读没有触发新合成

    def test_records_sentence_k_on_build(self):
        """构建时把句子口径 k_sentence 一并落盘，整段口径的 k 同时保留。"""
        calls = []
        eff = duration_model.effective_chars(tts_engine.STANDARD_RULER_TEXT)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(tts_engine, "ruler_sentence_k",
                                   return_value=5.5):
                r = self._ruler(td, calls)
            self.assertAlmostEqual(r["k"], round(eff / 5.0, 4))
            self.assertAlmostEqual(r["k_sentence"], 5.5)
            with open(os.path.join(td, tts_engine.RULER_JSON),
                      encoding="utf-8") as f:
                meta = json.load(f)
            self.assertAlmostEqual(meta["k_sentence"], 5.5)

    def test_read_ruler_derives_sentence_k_for_legacy_json(self):
        """旧尺子没有 k_sentence：只读取回时从音频现量一次，不合成、不写盘。"""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, tts_engine.RULER_WAV), "wb") as f:
                f.write(b"legacy-wav")
            with open(os.path.join(td, tts_engine.RULER_JSON), "w",
                      encoding="utf-8") as f:
                json.dump({"k": 4.0, "seconds": 5.0}, f)
            with mock.patch.object(tts_engine, "ruler_sentence_k",
                                   return_value=4.7):
                r = tts_engine.read_ruler(td)
            self.assertAlmostEqual(r["k"], 4.0)
            self.assertAlmostEqual(r["k_sentence"], 4.7)

    def test_ruler_sentence_k_subtracts_sentence_gaps(self):
        """真跑 ffmpeg：句子口径 k = 有效字 ÷ (整段 − 句间停顿)。"""
        try:
            audio_engine.ffmpeg_bin()
        except audio_engine.AudioError:
            self.skipTest("ffmpeg 不可用")
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "seg.wav")
            _write_seg_wav(wav, [(1.0, True), (0.2, False),
                                 (1.0, True), (0.2, False), (1.0, True)])
            T = audio_engine.probe_duration_safe(wav)
            ks = tts_engine.ruler_sentence_k(wav)
            self.assertIsNotNone(ks)
            eff = duration_model.effective_chars(tts_engine.STANDARD_RULER_TEXT)
            self.assertAlmostEqual(ks, eff / (T - 0.4), delta=0.5)

    def test_ruler_sentence_k_none_when_no_gaps(self):
        """量不出句间停顿就返回 None（调用方退回整段口径），不抛异常。"""
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "tone.wav")
            _write_wav(wav, 1.0)
            self.assertIsNone(tts_engine.ruler_sentence_k(wav))

    def test_role_rulers_are_isolated(self):
        """A / B 两条尺子各占自己的文件名：写一条不盖另一条，只读也不串台。"""
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("podcast_maker.audio_engine.probe_duration_safe",
                            return_value=5.0):
                ra = tts_engine.standard_ruler(
                    td, lambda t: calls.append(t) or b"a",
                    text="甲一。甲二。甲三。", role="A")
                rb = tts_engine.standard_ruler(
                    td, lambda t: calls.append(t) or b"b",
                    text="乙一。乙二。乙三。", role="B")
            self.assertNotEqual(ra["path"], rb["path"])
            self.assertTrue(os.path.exists(
                os.path.join(td, tts_engine.RULER_WAV)))
            self.assertTrue(os.path.exists(
                os.path.join(td, tts_engine.RULER_WAV_B)))
            self.assertEqual(tts_engine.read_ruler(td, "A")["path"], ra["path"])
            self.assertEqual(tts_engine.read_ruler(td, "B")["path"], rb["path"])
            # 文本随角色落到各自 json —— 分母口径才对得上各自的 k_role
            with open(os.path.join(td, tts_engine.RULER_JSON),
                      encoding="utf-8") as f:
                self.assertEqual(json.load(f)["text"], "甲一。甲二。甲三。")
            with open(os.path.join(td, tts_engine.RULER_JSON_B),
                      encoding="utf-8") as f:
                self.assertEqual(json.load(f)["text"], "乙一。乙二。乙三。")

    def test_sentence_boundary_uses_given_text(self):
        """句边界由传入文本算：文本不同，边界位置不同（不能写死一份）。"""
        f1 = tts_engine._sentence_boundary_fracs("甲甲甲甲。乙乙乙乙。")
        f2 = tts_engine._sentence_boundary_fracs("甲甲。乙乙乙乙乙乙。")
        self.assertEqual(len(f1), 1)
        self.assertAlmostEqual(f1[0], 0.5, places=3)
        self.assertAlmostEqual(f2[0], 2.5 / 9.0, places=3)
        # 单句没有句间停顿可扣：返回空，调用方据此处为量不出
        self.assertEqual(tts_engine._sentence_boundary_fracs("只有一句。"), [])

    def test_allow_build_false_refuses_when_missing(self):
        """只读入口（试听）缺标尺照实报错，绝不就地起服务合成。"""
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(tts_engine.TTSError):
                tts_engine.standard_ruler(td, lambda t: calls.append(t) or b"x",
                                          allow_build=False)
            self.assertEqual(calls, [])
            self.assertFalse(os.path.exists(os.path.join(td, tts_engine.RULER_WAV)))


class TestStandardPayload(unittest.TestCase):
    """配置载荷的「脚本估时基准」块：脚本尺子 与 各角色试听分母 一起下发。

    这一块挂在「配置 → 声音」的音色行上，紧挨「▶ 试听」按钮——所以必须能把
    「脚本估时用的那个数」和「试听换算的分母」分开报，界面才不用猜；A/B 各有
    自己的尺子（各念自己的 ref.txt），分母按角色分别下发，不能并成一个数。
    """

    def test_carries_sentence_k_per_role(self):
        def ruler(cache, role="A"):
            return {"path": "x.wav", "k": 4.3586,
                    "k_sentence": 4.7015 if role == "A" else 5.1057}
        with mock.patch.object(web_ui, "_previews_dir", return_value="X"), \
             mock.patch.object(tts_engine, "read_ruler", side_effect=ruler):
            s = web_ui.standard_payload()
        self.assertEqual(s["k"], duration_model.STANDARD_K)
        self.assertEqual(s["k"], 4.39)
        self.assertAlmostEqual(s["k_sentence"]["A"], 4.7, places=2)
        self.assertAlmostEqual(s["k_sentence"]["B"], 5.11, places=2)
        # 不是同一个东西，别混报
        self.assertNotEqual(s["k"], s["k_sentence"]["A"])

    def test_sentence_k_falls_back_to_whole_when_column_absent(self):
        """尺子只给整段口径（旧档兜底路径）：如实取它，不凭空造一个更好的数。"""
        with mock.patch.object(web_ui, "_previews_dir", return_value="X"), \
             mock.patch.object(tts_engine, "read_ruler",
                               return_value={"path": "x.wav", "k": 4.3586}):
            s = web_ui.standard_payload()
        self.assertAlmostEqual(s["k_sentence"]["A"], 4.36, places=2)
        self.assertAlmostEqual(s["k_sentence"]["B"], 4.36, places=2)

    def test_no_sentence_k_without_ruler(self):
        """尺子还没建：只下发 k，界面照实不报试听分母。"""
        with mock.patch.object(web_ui, "_previews_dir", return_value="X"), \
             mock.patch.object(tts_engine, "read_ruler", return_value=None):
            s = web_ui.standard_payload()
        self.assertEqual(s["k"], duration_model.STANDARD_K)
        self.assertNotIn("k_sentence", s)

    def test_ruler_read_failure_does_not_break_payload(self):
        """读尺子任何岔子都不能让配置页加载不出来——降级成只报 k。"""
        with mock.patch.object(web_ui, "_previews_dir",
                               side_effect=OSError("boom")):
            s = web_ui.standard_payload()
        self.assertEqual(s["k"], duration_model.STANDARD_K)
        self.assertNotIn("k_sentence", s)


class _StubCfg:
    """端点隔离：只供给端点实际读的键。"""

    def __init__(self, values):
        self._v = values

    def data(self):
        return dict(self._v)

    def get(self, key, default=None):
        return self._v.get(key, default)


class TestPreviewStandard(unittest.TestCase):
    def setUp(self):
        self._old_cfg, self._old_calib = web_ui.CFG, web_ui.CALIB
        self._old_pdir = web_ui._previews_dir
        self._old_ruler = web_ui._get_std_ruler
        self.td = tempfile.TemporaryDirectory()
        self.std = os.path.join(self.td.name, "standard.wav")
        _write_wav(self.std, 0.5)
        web_ui.CFG = _StubCfg({"tts.engine": "edge", "tts.voice_a": "vA",
                               "tts.voice_b": "vB", "tts.speed_a": 1.2})
        web_ui._previews_dir = lambda: self.td.name

    def tearDown(self):
        web_ui.CFG, web_ui.CALIB = self._old_cfg, self._old_calib
        web_ui._previews_dir = self._old_pdir
        web_ui._get_std_ruler = self._old_ruler
        self.td.cleanup()

    def test_rate_is_speed_times_role_k_over_ruler_k(self):
        """换算 = 滑杆倍率 × 音色实测语速 ÷ 标尺语速。

        方向判据：音色比标尺快（k_role > 分母）时，倍率必须**大于**滑杆值
        （音色越快播得越快）。这里 1.2 × 4.31 ÷ 4.26 = 1.214 > 1.2；把两个 k
        倒过来写会得到 1.186 < 1.2，与本用例直接冲突——方向写反这条必红。
        本用例的标尺只给整段口径的 k（没有 k_sentence），走的是兜底分母。
        """
        calib = mock.MagicMock()
        calib.get.return_value = {"k": 4.31}
        web_ui.CALIB = calib
        web_ui._get_std_ruler = lambda cfg, pid, allow_build=True, role="A": {
            "path": self.std, "k": 4.26}
        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vA", 1.2)), \
             mock.patch.object(audio_engine, "speed_shift",
                               side_effect=lambda s, o, r: (_write_wav(o, 0.3), o)[1]) as sh:
            res = web_ui.api_preview_standard({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        expect = 1.2 * 4.31 / 4.26
        self.assertAlmostEqual(res["rate"], expect, places=3)
        self.assertAlmostEqual(sh.call_args[0][2], expect, places=6)
        self.assertTrue(res["audio_base64"])

    def test_rate_uses_ruler_sentence_k_when_present(self):
        """分母取标尺的**句子口径** k_sentence，而不是整段口径的 k。

        口径必须对齐：分子 k_role 逐句量（不含句间停顿），分母也得是尺子扣掉
        句间停顿后的句子口径——用整段口径做分母，听到的词速会系统性偏快。
        """
        calib = mock.MagicMock()
        calib.get.return_value = {"k": 4.31}
        web_ui.CALIB = calib
        web_ui._get_std_ruler = lambda cfg, pid, allow_build=True, role="A": {
            "path": self.std, "k": 4.26, "k_sentence": 4.70}
        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vA", 1.2)), \
             mock.patch.object(audio_engine, "speed_shift",
                               side_effect=lambda s, o, r: (_write_wav(o, 0.3), o)[1]) as sh:
            res = web_ui.api_preview_standard({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        expect = 1.2 * 4.31 / 4.70
        self.assertAlmostEqual(res["rate"], expect, places=3)
        self.assertAlmostEqual(sh.call_args[0][2], expect, places=6)
        self.assertAlmostEqual(res["k_sentence"], 4.70, places=2)

    def test_preview_cache_key_carries_ruler_identity(self):
        """缓存键带「尺子 + 音色实测」两个身份：换尺子后 rate 撞上同一格也不复用旧音频。"""
        calib = mock.MagicMock()
        calib.get.return_value = {"k": 4.31}
        web_ui.CALIB = calib
        web_ui._get_std_ruler = lambda cfg, pid, allow_build=True, role="A": {
            "path": self.std, "k": 4.26, "k_sentence": 4.70}
        made = []

        def shift(s, o, r):
            made.append(os.path.basename(o))
            _write_wav(o, 0.3)
            return o

        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vA", 1.2)), \
             mock.patch.object(audio_engine, "speed_shift", side_effect=shift):
            res = web_ui.api_preview_standard({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(made)
        self.assertIn("k4.310", made[0])   # 音色实测 k_role
        self.assertIn("r4.700", made[0])   # 尺子的句子口径 k_sentence

    def test_fails_closed_without_measurement(self):
        """没有实测就停下报错，引导去点标定——拿默认值顶替等于让试听撒谎。"""
        calib = mock.MagicMock()
        calib.get.return_value = None
        web_ui.CALIB = calib
        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vB", 1.0)):
            res = web_ui.api_preview_standard({"role": "B"})
        self.assertFalse(res["ok"])
        self.assertIn("标定", res["error"])

    def test_rejects_bad_role(self):
        res = web_ui.api_preview_standard({"role": "C"})
        self.assertFalse(res["ok"])

    def test_missing_measurement_flags_need_calib(self):
        """缺实测：报错带 need_calib，前端据此就地把该角按钮转成「先标定」引导。"""
        calib = mock.MagicMock()
        calib.get.return_value = None
        web_ui.CALIB = calib
        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vB", 1.0)):
            res = web_ui.api_preview_standard({"role": "B"})
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("need_calib"), "B")

    def test_missing_ruler_refuses_without_building(self):
        """标尺缺失：立即报错并带 need_ruler，绝不调 speed_shift（不现合成、不变速）。"""
        calib = mock.MagicMock()
        calib.get.return_value = {"k": 4.0}
        web_ui.CALIB = calib

        def boom(cfg, pid, allow_build=True, role="A"):
            self.assertFalse(allow_build)     # 试听必须走只读口径
            raise tts_engine.TTSError("标准标尺还没生成——…")
        web_ui._get_std_ruler = boom
        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vA", 1.0)), \
             mock.patch.object(audio_engine, "speed_shift") as sh:
            res = web_ui.api_preview_standard({"role": "A"})
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("need_ruler"))
        sh.assert_not_called()


class TestRulerCarrierPinned(unittest.TestCase):
    """标尺载体钉死 Qwen3-TTS A 角：与当前引擎解耦，Edge 下不偷换载体。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._old_pdir = web_ui._previews_dir
        web_ui._previews_dir = lambda: self.td.name

    def tearDown(self):
        web_ui._previews_dir = self._old_pdir
        self.td.cleanup()

    def test_edge_engine_still_binds_qwen_voice(self):
        """当前引擎是 Edge，标尺合成照样绑 Qwen3-TTS 的 A 角音色。"""
        cfg = _StubCfg({"tts.engine": "edge", "tts.voice_a": "edgeVA",
                        "tts.qwen3tts_voice_a": "Vivian"})
        seen = {}

        def synth(text, voice, speed, c, ref=None, **kw):
            seen.update(voice=voice, speed=speed, ref=ref)
            return b"fake"

        with mock.patch.object(tts_engine, "voice_profiles",
                               return_value={"A": "ref.wav"}), \
             mock.patch.object(tts_engine, "acquire_service"), \
             mock.patch.object(tts_engine, "release_service"), \
             mock.patch.object(tts_engine, "synth_line", side_effect=synth):
            web_ui._std_ruler_synth(cfg, "proj")("text")
        self.assertEqual(seen["voice"], "Vivian")
        self.assertEqual(seen["speed"], 1.0)
        self.assertEqual(seen["ref"], "ref.wav")

    def test_missing_qwen_voice_refuses_even_with_edge_voice(self):
        """Qwen3-TTS A 角没选就拒绝——不许拿 Edge 音色顶替载体。"""
        cfg = _StubCfg({"tts.engine": "edge", "tts.voice_a": "edgeVA",
                        "tts.qwen3tts_voice_a": ""})
        with mock.patch.object(tts_engine, "voice_profiles",
                               return_value={"A": "ref.wav"}):
            with self.assertRaises(tts_engine.TTSError) as cm:
                web_ui._std_ruler_synth(cfg, "proj")("text")
        self.assertIn("Qwen3-TTS", str(cm.exception))

    def test_carrier_recorded_in_json(self):
        """标尺 json 必须带载体身份——尺子是谁的嗓子落盘可查。"""
        calls = []

        def synth(text):
            calls.append(text)
            return b"fake-wav"

        with mock.patch("podcast_maker.audio_engine.probe_duration_safe",
                        return_value=5.0):
            tts_engine.standard_ruler(
                self.td.name, synth,
                carrier={"engine": "qwen3tts", "voice": "Vivian"})
        with open(os.path.join(self.td.name, tts_engine.RULER_JSON),
                  encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["carrier"]["engine"], "qwen3tts")
        self.assertEqual(meta["carrier"]["voice"], "Vivian")
        self.assertEqual(len(calls), 1)

    def test_get_std_ruler_stamps_device(self):
        """尺子落盘带上生成它的设备/后端 —— 尺子也是「谁在哪儿生的」要说清。"""
        cfg = _StubCfg({"tts.engine": "qwen3tts",
                        "tts.qwen3tts_voice_a": "Vivian"})
        seen = {}

        def fake_ruler(cache_dir, synth_one, text=None, carrier=None,
                       allow_build=True, role="A"):
            seen.update(carrier or {})
            return {"path": "x", "k": 4.0}

        with mock.patch.object(tts_engine, "running_device",
                               return_value=("cuda", "faster-qwen3-tts")), \
             mock.patch.object(tts_engine, "standard_ruler",
                               side_effect=fake_ruler):
            web_ui._get_std_ruler(cfg, "proj", allow_build=True)
        self.assertEqual(seen.get("device"), "cuda")
        self.assertEqual(seen.get("backend"), "faster-qwen3-tts")
        self.assertEqual(seen.get("voice"), "Vivian")

    def test_get_std_ruler_skips_device_probe_on_readonly(self):
        """只读路径（试听）不问服务：别给一次试听白添一次探测。"""
        cfg = _StubCfg({"tts.engine": "qwen3tts",
                        "tts.qwen3tts_voice_a": "Vivian"})
        with mock.patch.object(tts_engine, "running_device") as rd, \
             mock.patch.object(tts_engine, "standard_ruler",
                               return_value={"path": "x", "k": 4.0}):
            web_ui._get_std_ruler(cfg, "proj", allow_build=False)
        rd.assert_not_called()


class TestVoiceToolPrep(unittest.TestCase):
    """跑音色工具（认领 / 录制 / 补 ref_base / 造嗓）前：先腾显存，再明确要 GPU。

    这几条链在**子进程**里判设备（`pick_device` 读整卡空闲），不像
    `synthesize()` 自带腾显存 —— 不先清掉 LM Studio 驻留就会静默退 CPU，产出的
    ref_base 与成片不在同一条波形上。所以规矩与 `synthesize()` 同款。
    """

    def test_unloads_then_pins_cuda(self):
        with mock.patch.object(tts_engine, "unload_llm_models") as ul:
            cmd = web_ui._prep_voice_tool(["py", "make_voice.py", "--json"])
        ul.assert_called_once()
        self.assertEqual(cmd[-2:], ["--device", "cuda"])

    def test_does_not_duplicate_device_flag(self):
        with mock.patch.object(tts_engine, "unload_llm_models"):
            cmd = web_ui._prep_voice_tool(["py", "x", "--device", "cpu"])
        self.assertEqual(cmd.count("--device"), 1)
        self.assertIn("cpu", cmd)

    def test_every_voice_tool_spawn_goes_through_prep(self):
        """结构级：四处 spawn 音色工具的地方必须全过 `_prep_voice_tool`。

        少一处就是一个静默退 CPU 的口子 —— 认领、录制、补 ref_base、造嗓，
        四条链都在加载本地模型。
        """
        import inspect
        src = inspect.getsource(web_ui)
        self.assertEqual(src.count("cmd = _prep_voice_tool(cmd"), 4)


class TestBgmMixPreview(unittest.TestCase):
    def setUp(self):
        self._old_cfg = web_ui.CFG
        self._old_pdir = web_ui._previews_dir
        self._old_ruler = web_ui._get_std_ruler
        self.td = tempfile.TemporaryDirectory()
        self.std = os.path.join(self.td.name, "standard.wav")
        _write_wav(self.std, 0.5)
        web_ui.CFG = _StubCfg({"bgm.mode": "builtin", "bgm.preset": "pensive",
                               "bgm.volume": 0.5, "bgm.fade_seconds": 0.0,
                               "bgm.ducking": False, "audio.sample_rate": 8000,
                               "audio.channels": 1})
        web_ui._previews_dir = lambda: self.td.name

    def tearDown(self):
        web_ui.CFG = self._old_cfg
        web_ui._previews_dir = self._old_pdir
        web_ui._get_std_ruler = self._old_ruler
        self.td.cleanup()

    def test_none_mode_refuses(self):
        web_ui.CFG = _StubCfg({"bgm.mode": "none"})
        res = web_ui.api_bgm_mix_preview({})
        self.assertFalse(res["ok"])

    def test_custom_missing_refuses(self):
        web_ui.CFG = _StubCfg({"bgm.mode": "custom", "bgm.custom_path": ""})
        res = web_ui.api_bgm_mix_preview({})
        self.assertFalse(res["ok"])

    def test_builtin_mixes_with_real_pipeline(self):
        """builtin 档：真资源文件 + 真 mix_bgm（本测不开 ducking，纯音量叠加）。"""
        try:
            bgm = __import__("podcast_maker.assets_factory", fromlist=["bgm_resource"]) \
                .bgm_resource("pensive")
        except (ValueError, RuntimeError):
            self.skipTest("内置 BGM 资源缺失")
        out = os.path.join(self.td.name, "bgm_mix.wav")
        web_ui._get_std_ruler = lambda cfg, pid, allow_build=True, role="A": {
            "path": self.std, "k": 4.0}
        with mock.patch.object(audio_engine, "mix_bgm",
                               side_effect=lambda v, o, b, c: (_write_wav(o, 0.4), o)[1]) as mb:
            res = web_ui.api_bgm_mix_preview({})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(mb.call_args[0][2], bgm)
        self.assertEqual(mb.call_args[0][0], self.std)
        self.assertTrue(res["audio_base64"])

    def test_missing_ruler_refuses_without_building(self):
        """混合试听同样只读标尺：缺了就报错带 need_ruler，不现合成。"""
        def boom(cfg, pid, allow_build=True, role="A"):
            self.assertFalse(allow_build)
            raise tts_engine.TTSError("标准标尺还没生成——…")
        web_ui._get_std_ruler = boom
        with mock.patch.object(audio_engine, "mix_bgm") as mb:
            res = web_ui.api_bgm_mix_preview({})
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("need_ruler"))
        mb.assert_not_called()


class TestVoiceCalibrate(unittest.TestCase):
    """标定按钮：三句现测进校准表，与成片校准同一套机制。"""

    def setUp(self):
        self._old_cfg, self._old_calib = web_ui.CFG, web_ui.CALIB
        self.td = tempfile.TemporaryDirectory()
        self.calib_path = os.path.join(self.td.name, "calib.json")
        web_ui.CALIB = duration_model.Calibration(path=self.calib_path)
        web_ui.CFG = _StubCfg({"tts.engine": "edge", "tts.voice_a": "vA",
                               "tts.voice_b": "vB", "tts.speed_a": 1.0})

    def tearDown(self):
        web_ui.CFG, web_ui.CALIB = self._old_cfg, self._old_calib
        self.td.cleanup()

    def _measure(self, seconds):
        """mock 逐句合成与测时长：记录 (voice, speed) 钉子，按句返回时长。"""
        calls = []

        def synth(text, voice, speed, cfg, ref=None, **kw):
            self.assertEqual(voice, "vA")
            self.assertEqual(speed, 1.0)   # 标定固定按 1.0 本色量
            calls.append(text)
            return b"fake"

        def probe(path):
            return seconds[len(calls) - 1]
        return synth, probe, calls

    def test_calibrates_three_lines_into_table(self):
        synth, probe, calls = self._measure([2.0, 3.0, 5.0])
        with mock.patch.object(tts_engine, "synth_line", side_effect=synth), \
             mock.patch.object(audio_engine, "probe_duration_safe",
                               side_effect=probe):
            res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(len(calls), len(tts_engine.CALIBRATION_LINES))
        # 三句样本必须进表，组键按本色 1.0——试听读取的就是这一组
        g = web_ui.CALIB.get("edge", "vA", 1.0)
        self.assertIsNotNone(g)
        self.assertEqual(g["n"], 3)
        self.assertGreater(g["k"], 0)
        self.assertAlmostEqual(res["ratio"],
                               duration_model.ratio_of(web_ui.CALIB, "edge", "vA", 1.0))

    def test_zero_duration_line_refuses(self):
        """某句读不出时长就整次作废，不落半个数。"""
        synth, probe, _ = self._measure([2.0, 0.0, 5.0])
        with mock.patch.object(tts_engine, "synth_line", side_effect=synth), \
             mock.patch.object(audio_engine, "probe_duration_safe",
                               side_effect=probe):
            res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertFalse(res["ok"])
        self.assertIsNone(web_ui.CALIB.get("edge", "vA", 1.0))

    def test_no_voice_selected_refuses(self):
        web_ui.CFG = _StubCfg({"tts.engine": "edge", "tts.voice_a": ""})
        res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertFalse(res["ok"])

    def test_rejects_bad_role(self):
        res = web_ui.api_voice_calibrate({"role": "C"})
        self.assertFalse(res["ok"])

    def test_local_engine_unloads_and_stamps_device(self):
        """本地引擎：标定先腾显存，并把服务自报的设备/后端随系数落表。

        标定与成片必须同后端 —— 标定在 CPU 上量、成片在 CUDA 上跑，两条波形
        不是同一条，标了等于白标。所以标定也走 `unload_llm_models`，并把真实
        设备记进校准表（「换档即失效」靠它）。
        """
        web_ui.CFG = _StubCfg({"tts.engine": "qwen3tts",
                               "tts.qwen3tts_voice_a": "library:薇薇-清亮",
                               "tts.qwen3tts_voice_b": "vB",
                               "tts.speed_a": 1.0})
        rb = os.path.join(self.td.name, "ref_base.wav")
        with open(rb, "wb") as f:
            f.write(b"base-bytes")
        ent = {"wav": rb, "text": "第一句。第二句。第三句。"}
        calls = []

        def synth(text, voice, speed, cfg, ref=None, **kw):
            calls.append(text)
            return b"fake"

        def probe(path):
            return [2.0, 3.0, 5.0][len(calls) - 1]

        with mock.patch.object(tts_engine, "voice_entity_ref",
                               return_value=ent), \
             mock.patch.object(tts_engine, "ensure_calib_ref",
                               return_value=rb), \
             mock.patch.object(tts_engine, "unload_llm_models") as ul, \
             mock.patch.object(tts_engine, "acquire_service"), \
             mock.patch.object(tts_engine, "release_service"), \
             mock.patch.object(tts_engine, "running_device",
                               return_value=("cuda", "faster-qwen3-tts")), \
             mock.patch.object(web_ui, "_get_std_ruler",
                               return_value={"path": "x", "k": 4.0}), \
             mock.patch.object(tts_engine, "synth_line", side_effect=synth), \
             mock.patch.object(audio_engine, "probe_duration_safe",
                               side_effect=probe):
            # 注意：**不传 project**——本体自带参考，标定不再问项目要档案
            res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(ul.called, "标定前必须腾显存")
        self.assertEqual(res["device"], "cuda")
        g = web_ui.CALIB.get("qwen3tts", "library:薇薇-清亮", 1.0)
        self.assertEqual(g["device"], "cuda")
        self.assertEqual(g["backend"], "faster-qwen3-tts")

    def test_measures_against_base_ref_and_its_own_text(self):
        """本地引擎：参考取本体自生成的 ref_base，文本取本体自己的转录逐句量。

        成片用的也是这份 ref_base 拼出的 icl；拿别的音频量出来的 k 与成片不是
        一条链。文本同理必须是本人的转录（不是 icl 那个两遍拼接件）。
        """
        web_ui.CFG = _StubCfg({"tts.engine": "qwen3tts",
                               "tts.qwen3tts_voice_a": "library:薇薇-清亮",
                               "tts.speed_a": 1.0})
        base = os.path.join(self.td.name, "ref_base_s.wav")
        with open(base, "wb") as f:
            f.write(b"base-bytes")
        ent = {"uri": "library:薇薇-清亮", "wav": "本体.wav",
               "text": "本是第一句。本是第二句。本是第三句。"}
        seen = []

        def synth(text, voice, speed, cfg, ref=None, **kw):
            seen.append({"text": text, "ref": ref})
            return b"fake"

        def probe(path):
            return [2.0, 3.0, 5.0][len(seen) - 1]

        with mock.patch.object(tts_engine, "voice_entity_ref",
                               return_value=ent) as ve, \
             mock.patch.object(tts_engine, "ensure_calib_ref",
                               return_value=base) as eb, \
             mock.patch.object(tts_engine, "unload_llm_models"), \
             mock.patch.object(tts_engine, "acquire_service"), \
             mock.patch.object(tts_engine, "release_service"), \
             mock.patch.object(tts_engine, "running_device",
                               return_value=("cuda", "faster-qwen3-tts")), \
             mock.patch.object(web_ui, "_get_std_ruler",
                               return_value={"path": "x", "k": 4.0}), \
             mock.patch.object(tts_engine, "synth_line", side_effect=synth), \
             mock.patch.object(audio_engine, "probe_duration_safe",
                               side_effect=probe):
            res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        ve.assert_called_once_with("library:薇薇-清亮", project_root=None)
        eargs = eb.call_args[0]
        self.assertEqual(eargs[0], ent)
        self.assertEqual(eargs[1], "A")
        self.assertEqual([s["text"] for s in seen],
                         ["本是第一句。", "本是第二句。", "本是第三句。"])
        # 参考是本体自生成的 ref_base，转录与它内容一致（本体转录，不是 icl 两遍）
        self.assertEqual(seen[0]["ref"]["wav"], base)
        self.assertEqual(seen[0]["ref"]["text"], ent["text"])

    def test_local_engine_unresolvable_entity_refuses(self):
        """本体解析不出（来源非法 / 文件不齐）就照实报错，不猜、不兜。"""
        web_ui.CFG = _StubCfg({"tts.engine": "qwen3tts",
                               "tts.qwen3tts_voice_a": "library:没有这个音色",
                               "tts.speed_a": 1.0})
        with mock.patch.object(tts_engine, "voice_entity_ref",
                               return_value=None):
            res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertFalse(res["ok"])
        self.assertIn("本体", res["error"])
        self.assertIsNone(web_ui.CALIB.get("qwen3tts",
                                           "library:没有这个音色", 1.0))

    def test_measures_into_the_unresolvable_named_needs_project(self):
        """named: 音色本体是项目资产：不给项目就解析不出，如实报错。"""
        web_ui.CFG = _StubCfg({"tts.engine": "qwen3tts",
                               "tts.qwen3tts_voice_a": "named:我的嗓",
                               "tts.speed_a": 1.0})
        with mock.patch.object(tts_engine, "voice_entity_ref",
                               return_value=None) as ve:
            res = web_ui.api_voice_calibrate({"role": "A"})
        self.assertFalse(res["ok"])
        ve.assert_called_once_with("named:我的嗓", project_root=None)


class TestSwOutlineBelongsToProject(unittest.TestCase):
    def test_mapping_and_editable(self):
        self.assertIn(("sw_outline", "script.sw_outline"), PROJECT_CONFIG_MAP)
        self.assertIn("sw_outline", EDITABLE)

    def test_gone_from_global_config(self):
        """全局配置里没有这个点位——开开关关是项目的事。"""
        from podcast_maker.config_manager import PARAM_SPEC
        self.assertNotIn("script.sw_outline", PARAM_SPEC)

    def test_planner_still_reads_the_channel(self):
        """planner 的读法不变：apply_to_config 把项目值送进 script.sw_outline。"""
        import inspect
        from podcast_maker import planner
        src = inspect.getsource(planner.plan_map)
        self.assertIn('cfg.get("script.sw_outline")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
