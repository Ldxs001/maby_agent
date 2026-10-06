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

"""标准标尺试听 / 标定按钮 / BGM 混合试听 / structured-writer 开关归项目 的钉子。

钉四件事：
1. 标准标尺＝音频＋实测语速成对落盘，全局唯一、合成一次后只读；
2. A/B 试听换算 = 滑杆倍率 × 标尺实测语速 ÷ 该音色实测语速，没有实测就停下；
3. 标定按钮：三句现测进校准表（与成片校准同一套机制），两种引擎都得量；
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

from podcast_maker import audio_engine, duration_model, tts_engine, web_ui  # noqa: E402
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

    def test_rate_is_speed_times_std_k_over_role_k(self):
        """换算 = 滑杆倍率 × 标尺实测语速 ÷ 音色实测语速——标尺自己的语速被消去。"""
        calib = mock.MagicMock()
        calib.get.return_value = {"k": 4.31}
        web_ui.CALIB = calib
        web_ui._get_std_ruler = lambda cfg, pid: {"path": self.std, "k": 4.26}
        with mock.patch.object(duration_model, "speaker_ctx",
                               return_value=("edge", "vA", 1.2)), \
             mock.patch.object(audio_engine, "speed_shift",
                               side_effect=lambda s, o, r: (_write_wav(o, 0.3), o)[1]) as sh:
            res = web_ui.api_preview_standard({"role": "A"})
        self.assertTrue(res["ok"], res.get("error"))
        expect = 1.2 * 4.26 / 4.31
        self.assertAlmostEqual(res["rate"], expect, places=3)
        self.assertAlmostEqual(sh.call_args[0][2], expect, places=6)
        self.assertTrue(res["audio_base64"])

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
        web_ui._get_std_ruler = lambda cfg, pid: {"path": self.std, "k": 4.0}
        with mock.patch.object(audio_engine, "mix_bgm",
                               side_effect=lambda v, o, b, c: (_write_wav(o, 0.4), o)[1]) as mb:
            res = web_ui.api_bgm_mix_preview({})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(mb.call_args[0][2], bgm)
        self.assertEqual(mb.call_args[0][0], self.std)
        self.assertTrue(res["audio_base64"])


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
