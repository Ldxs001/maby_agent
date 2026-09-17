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

"""音频编码参数测试：码率不做静默钳制，BGM 来源单一入口。"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import audio_engine as A  # noqa: E402


class TestBitrateCeiling(unittest.TestCase):
    def test_mpeg1_high_rates(self):
        for sr in (32000, 44100, 48000):
            self.assertEqual(A.bitrate_ceiling(sr, "mp3"), 320)

    def test_mpeg2_low_rates_capped_at_160(self):
        # 22050 Hz 属 MPEG-2 LSF，MP3 上限 160 kbps
        for sr in (16000, 22050, 24000):
            self.assertEqual(A.bitrate_ceiling(sr, "mp3"), 160)

    def test_lowest_rates_capped_at_64(self):
        for sr in (8000, 11025, 12000):
            self.assertEqual(A.bitrate_ceiling(sr, "mp3"), 64)

    def test_aac_has_its_own_table(self):
        self.assertEqual(A.bitrate_ceiling(22050, "aac"), 192)
        self.assertEqual(A.bitrate_ceiling(44100, "aac"), 320)

    def test_unknown_rate_falls_back_to_lowest(self):
        self.assertLessEqual(A.bitrate_ceiling(99999, "mp3"), 320)


class TestValidateParams(unittest.TestCase):
    def test_rejects_unreachable_bitrate(self):
        """原项目缺陷：22050 + 192k 会被编码器静默钳到 160k。"""
        warns, errs = A.validate_params(
            {"audio.sample_rate": 22050, "audio.bitrate_kbps": 192,
             "audio.codec": "mp3"})
        self.assertEqual(warns, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("160", errs[0])

    def test_accepts_legal_pair(self):
        warns, errs = A.validate_params(
            {"audio.sample_rate": 44100, "audio.bitrate_kbps": 192,
             "audio.codec": "mp3"})
        self.assertEqual((warns, errs), ([], []))

    def test_default_config_is_legal(self):
        from podcast_maker.config_manager import ConfigManager
        warns, errs = A.validate_params(ConfigManager().data())
        self.assertEqual(errs, [], "默认配置不应触发编码参数错误")


class TestResolveBgm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm-bgm-")
        self.bgm = os.path.join(self.tmp, "bgm.wav")
        with open(self.bgm, "wb") as f:
            f.write(b"RIFF")

    def test_none_mode_returns_empty(self):
        self.assertEqual(A.resolve_bgm_source({"bgm.mode": "none"}, self.tmp), "")

    def test_builtin_prefers_explicit_path(self):
        cfg = {"bgm.mode": "builtin", "_bgm_src": self.bgm}
        self.assertEqual(A.resolve_bgm_source(cfg, self.tmp), self.bgm)

    def test_builtin_falls_back_to_out_dir(self):
        self.assertEqual(A.resolve_bgm_source({"bgm.mode": "builtin"}, self.tmp),
                         self.bgm)

    def test_custom_uses_configured_path(self):
        cfg = {"bgm.mode": "custom", "bgm.custom_path": self.bgm}
        self.assertEqual(A.resolve_bgm_source(cfg, self.tmp), self.bgm)

    def test_missing_file_returns_empty_not_crash(self):
        cfg = {"bgm.mode": "custom", "bgm.custom_path": os.path.join(self.tmp, "no.wav")}
        self.assertEqual(A.resolve_bgm_source(cfg, self.tmp), "")

    def test_builtin_missing_returns_empty(self):
        empty = tempfile.mkdtemp(prefix="pm-empty-")
        self.assertEqual(A.resolve_bgm_source({"bgm.mode": "builtin"}, empty), "")


class TestEncodeGuard(unittest.TestCase):
    def test_raises_before_invoking_ffmpeg(self):
        """超限必须在编码前报错，不做静默钳制。"""
        with self.assertRaises(A.AudioError):
            A.to_encoded("不存在的文件.wav",
                         os.path.join(tempfile.mkdtemp(), "out.mp3"),
                         {"audio.codec": "mp3", "audio.bitrate_kbps": 320,
                          "audio.sample_rate": 22050})


class TestIntroOutroContract(unittest.TestCase):
    def test_returns_triple(self):
        """片头尾时长必须向上返回（字幕时间轴依赖它）。"""
        import inspect
        src = inspect.getsource(A.add_intro_outro)
        self.assertIn("return out_path, intro_sec, outro_sec", src)

    def test_process_exposes_offsets(self):
        import inspect
        src = inspect.getsource(A.process)
        for key in ("intro_seconds", "outro_seconds", "final_audio"):
            self.assertIn(key, src)


class TestBgmResources(unittest.TestCase):
    """内置 BGM = 包内真实器乐循环库（resources/bgm/），正弦合成已退役。"""

    def test_all_presets_have_resource(self):
        from podcast_maker import assets_factory as F
        from podcast_maker.config_manager import MODE_SPEC
        opts = MODE_SPEC["bgm.preset"]["options"]
        self.assertGreaterEqual(len(opts), 15, "内置档位应不少于 15 档")
        for preset in opts:
            path = F.bgm_resource(preset)
            self.assertTrue(os.path.exists(path), "缺资源：%s" % path)

    def test_unknown_preset_rejected(self):
        from podcast_maker import assets_factory as F
        with self.assertRaises(ValueError):
            F.bgm_resource("no_such_preset")

    def test_spec_has_no_synth_params(self):
        """旧正弦合成的参数不得残留在枚举表里。"""
        from podcast_maker.config_manager import MODE_SPEC
        for opt in MODE_SPEC["bgm.preset"]["options"].values():
            self.assertEqual(set(opt.keys()), {"label"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
