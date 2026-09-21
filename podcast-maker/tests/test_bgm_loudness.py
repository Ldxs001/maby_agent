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

"""BGM 素材响度归一的回归守卫。

守的是什么
----------
素材生成器原先只做**削峰保护**（`peak > 0.90` 才缩），不做响度归一，导致
15 档内置素材 integrated LUFS 从 `chimes -15.18` 到 `horror -28.76`
**相差 13.6 LU**：同一个 `bgm.volume` 在不同档位上响度差 4 倍，换一档就
从「听不清」跳成「盖住人声」。混音侧怎么调默认值都救不了。

这类缺陷**不会自己报错**——素材照样生成、出片照样成功，只有人耳在某个
档位上听出不对才可能被发现。所以必须把它变成自动化判据：

1. 内置 15 档必须落在同一响度上（这是本文件最重要的一条）；
2. 归一必须真的把不同电平的素材拉到一处；
3. 目标在真峰值上限内不可达时必须**拒写**，且文件一个字节都不能变
   （「悄悄削一点凑数」正是这套机制存在的理由）；
4. 实现只能有一份 —— 生成侧与校正侧必须共用同一个模块，否则两边
   迟早跑出两个标准。
"""

import array
import math
import os
import sys
import tempfile
import unittest
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bgm_loudness as L                                        # noqa: E402

BGM_DIR = os.path.join(ROOT, "podcast_maker", "resources", "bgm")

# 内置素材响度的最大容差（LU）。素材之间只要差超过这个数，同一个
# bgm.volume 就会在不同档位上听出明显大小差。
PRESET_SPREAD_TOL_LU = 1.0

SR = 32000


def have_ffmpeg():
    try:
        return bool(L.ffmpeg_bin())
    except Exception:
        return False


def write_tone(path, amp, seconds=2.0, sr=SR):
    """生成一段单声道 16bit 正弦，指定幅度。"""
    n = int(sr * seconds)
    pcm = array.array("h", (int(round(amp * 32767 * math.sin(
        2 * math.pi * 220 * i / float(sr)))) for i in range(n)))
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


def write_square(path, amp, seconds=2.0, sr=SR):
    """方波：峰均比≈1（峰值顶死），响度提不上去，用来构造不可达目标。"""
    n = int(sr * seconds)
    pcm = array.array("h", (int(round(amp * 32767)) for _ in range(n)))
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


@unittest.skipUnless(have_ffmpeg(), "需要 ffmpeg 测量响度")
class TestBuiltinPresetsAreLevelMatched(unittest.TestCase):
    """内置 15 档必须响度一致 —— 本改动存在的唯一理由。"""

    def test_all_presets_within_tolerance(self):
        files = sorted(f for f in os.listdir(BGM_DIR) if f.endswith(".wav"))
        self.assertTrue(files, "内置 BGM 目录里一个 wav 都没有：%s" % BGM_DIR)

        ffmpeg = L.ffmpeg_bin()
        measured = {}
        for name in files:
            lufs, _tp = L.measure(os.path.join(BGM_DIR, name), ffmpeg)
            self.assertIsNotNone(lufs, "%s 测不出响度，素材可能损坏" % name)
            measured[name] = lufs

        lo = min(measured.values())
        hi = max(measured.values())
        detail = "、".join("%s %.2f" % (k, v)
                          for k, v in sorted(measured.items(),
                                             key=lambda kv: kv[1]))
        self.assertLessEqual(
            hi - lo, PRESET_SPREAD_TOL_LU,
            "内置 BGM 素材响度不齐：极差 %.2f LU（上限 %.1f）—— 同一个 "
            "bgm.volume 在不同档位上会听出大小差。\n  %s\n"
            "修法：重新生成素材后跑 `python tools/bgm_level.py --apply`，"
            "生成侧已默认归一，出现这条说明素材来路不对。"
            % (hi - lo, PRESET_SPREAD_TOL_LU, detail))


@unittest.skipUnless(have_ffmpeg(), "需要 ffmpeg 测量响度")
class TestNormalizeMovesSourcesTogether(unittest.TestCase):
    """归一必须真的把不同电平的素材拉到一处。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bgm_norm_")
        self.ffmpeg = L.ffmpeg_bin()

    def tearDown(self):
        for f in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, f))
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_two_very_different_sources_end_up_matched(self):
        target = -23.0
        results = []
        for name, amp in (("quiet.wav", 0.02), ("loud.wav", 0.90)):
            p = write_tone(os.path.join(self.tmp, name), amp)
            before, _ = L.measure(p, self.ffmpeg)
            res = L.normalize_file(p, target_lufs=target, tp_ceil=-1.0,
                                   ffmpeg=self.ffmpeg)
            self.assertTrue(res["ok"], "%s 归一失败：%s" % (name, res.get("reason")))
            results.append((before, res["after_lufs"]))

        # 归前极差应当很大（否则这条测试没在考验任何东西），归后必须收敛。
        spread_before = abs(results[0][0] - results[1][0])
        spread_after = abs(results[0][1] - results[1][1])
        self.assertGreater(spread_before, 10.0,
                           "两个合成素材归前只差 %.2f LU，测试没构造出对照"
                           % spread_before)
        self.assertLessEqual(spread_after, L.VERIFY_TOL_LU,
                             "归后仍差 %.2f LU，归一没起作用" % spread_after)

    def test_second_pass_is_a_no_op(self):
        p = write_tone(os.path.join(self.tmp, "twice.wav"), 0.3)
        first = L.normalize_file(p, target_lufs=-23.0, tp_ceil=-1.0,
                                 ffmpeg=self.ffmpeg)
        self.assertTrue(first["ok"], first.get("reason"))
        second = L.normalize_file(p, target_lufs=-23.0, tp_ceil=-1.0,
                                  ffmpeg=self.ffmpeg)
        self.assertTrue(second["ok"], second.get("reason"))
        self.assertLessEqual(abs(second["gain_db"]), L.VERIFY_TOL_LU,
                             "再跑一次还加了 %.2f dB —— 归一不收敛"
                             % second["gain_db"])

    def test_unreachable_target_is_refused_and_file_untouched(self):
        """峰值顶死的素材提不到目标响度时必须拒写，不能削一部分凑数。"""
        p = write_square(os.path.join(self.tmp, "square.wav"), 0.95)
        before = read_bytes(p)
        res = L.normalize_file(p, target_lufs=-6.0, tp_ceil=-1.0,
                               ffmpeg=self.ffmpeg)
        self.assertFalse(res["ok"], "不可达目标竟然判成功了：%s" % res)
        self.assertIn("不可达", res.get("reason", ""),
                      "失败原因没说清是峰值受限：%s" % res.get("reason"))
        self.assertEqual(before, read_bytes(p),
                         "判失败却改动了文件 —— 「失败 = 文件未变」必须成立")

    def test_rejects_non_mono_or_non_16bit(self):
        p = os.path.join(self.tmp, "stereo.wav")
        with wave.open(p, "w") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(b"\x00\x00" * 2000)
        before = read_bytes(p)
        res = L.normalize_file(p, ffmpeg=self.ffmpeg)
        self.assertFalse(res["ok"])
        self.assertEqual(before, read_bytes(p), "拒绝的素材不得被改动")


class TestSingleImplementation(unittest.TestCase):
    """生成侧与校正侧必须共用同一个模块，否则迟早跑出两个标准。"""

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_generator_normalizes_on_save(self):
        src = self._read("_smoke", "_bgm_gen.py")
        self.assertIn("normalize_file(", src,
                      "生成器落盘时没调归一 —— 新素材又会不齐")
        self.assertIn("import bgm_loudness", src,
                      "生成器没有共用 tools/bgm_loudness.py")

    def test_level_tool_does_not_reimplement_measurement(self):
        src = self._read("tools", "bgm_level.py")
        for fn in ("def measure(", "def apply_gain_db(", "def read_pcm("):
            self.assertNotIn(fn, src,
                             "校正工具里又写了一份 %s —— 两份实现迟早跑偏，"
                             "应当 import tools/bgm_loudness.py" % fn)

    def test_save_wav_failure_deletes_the_file(self):
        """归一失败必须删掉半成品：main() 会跳过已存在的文件。"""
        src = self._read("_smoke", "_bgm_gen.py")
        self.assertIn("os.remove(path)", src,
                      "归一失败没删半成品，会被 main() 永久跳过")


if __name__ == "__main__":
    unittest.main()
