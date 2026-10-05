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

"""字幕框「出现时刻」回归测试 —— 框与第一句字幕同时出现。

静态层预烘焙把字幕框一次画进了背景图，副作用是**框从第 0 帧就出现**；旧链上框是
一条 ASS 事件，起止跟着第一句字幕（2b 实测 2.72 s）。修法是把背景烘成两张（只压暗 /
压暗加框）、合成时按帧接起来。这一组钉四件事：

1. **帧号怎么算**（`box_onset_frames`）：第一句字幕的开口时刻向上取整到帧号；取不到、
   落在开头、落在片尾之外都是 0（退回单张）。浮点噪声不许把它推过界——1.0 秒是
   第 30 帧，不能算成第 31 帧。
2. **链上怎么接**：用 `trim` 的 `start_frame` / `end_frame` **帧精确**切，不用 `-t 秒数`
   （2.72 s × 30 fps = 81.6，`-t` 说不准算 82 还是 83 帧）；`format=rgb24` 仍在 `ass=`
   之前；不传 `box_split` 时链与从前逐字一致。
3. **缓推档的帧偏移**：两段各自 zoompan 时第二段必须带 `(on+N0)`，否则接缝处缩放会
   跳回起点。这是「按输出帧序号驱动」这一档的固有要求，不是补丁。
4. **输入顺序与音频下标**：无框图占 0 号位、有框图占 1 号位，音频因此从 `1:a` 挪到
   `2:a` —— 波形/频谱读的就是这个下标，写死会让它去第二张背景图上取音频。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image                                             # noqa: E402

from podcast_maker import assets_factory as AF                    # noqa: E402
from podcast_maker import subtitle_engine as SE                   # noqa: E402
from podcast_maker import video_engine as VE                      # noqa: E402
from podcast_maker.config_manager import ConfigManager            # noqa: E402


class TestBoxOnsetFrames(unittest.TestCase):
    """框该从第几帧起出现。"""

    def test_rounds_up_to_the_frame_the_subtitle_opens_on(self):
        # 2.72 s × 30 fps = 81.6 → 第 82 帧（2.7333 s）；第 81 帧（2.7000 s）在它之前
        self.assertEqual(VE.box_onset_frames([{"start": 2.72}], 30), 82)

    def test_an_exact_frame_stays_on_that_frame(self):
        """浮点噪声不许把整数帧推过界，否则框会早一帧出现。"""
        self.assertEqual(VE.box_onset_frames([{"start": 1.0}], 30), 30)
        self.assertEqual(VE.box_onset_frames([{"start": 2.7}], 30), 81)
        self.assertEqual(VE.box_onset_frames([{"start": 81.0 / 30}], 30), 81)

    def test_just_past_a_frame_goes_to_the_next(self):
        self.assertEqual(VE.box_onset_frames([{"start": 1.0 + 1e-4}], 30), 31)

    def test_nothing_to_postpone_is_zero(self):
        for timings in (None, [], [{}], [{"start": 0}], [{"start": 0.0}],
                        [{"start": -1}], [{"start": "x"}], ["不是字典"]):
            with self.subTest(timings=timings):
                self.assertEqual(VE.box_onset_frames(timings, 30), 0)

    def test_reads_the_first_event_only(self):
        self.assertEqual(VE.box_onset_frames(
            [{"start": 3.0}, {"start": 9.0}], 30), 90)


class TestDualBackgroundChain(unittest.TestCase):
    """两张背景图在链上怎么接。"""

    ASS = os.path.join(ROOT, "x.ass")

    def _chain(self, box_split=None, mode="static", audio_idx=1):
        cfg = {"animation.mode": mode, "video.fps": 30}
        return VE._graph(cfg, 1920, 1080, 30, 60.0, self.ASS, "", [], [],
                         box_split=box_split, audio_idx=audio_idx)

    def test_one_image_is_the_old_chain(self):
        joined = ";".join(self._chain())
        self.assertTrue(joined.startswith("[0:v]"))
        self.assertNotIn("trim=", joined)
        self.assertNotIn("concat=", joined)

    def test_two_images_cut_by_frame_number(self):
        joined = ";".join(self._chain(box_split=(0, 1, 82)))
        self.assertIn("[0:v]trim=end_frame=82", joined)
        self.assertIn("[1:v]trim=start_frame=82", joined)
        self.assertIn("concat=n=2:v=1:a=0[bg]", joined)

    def test_never_cuts_by_seconds(self):
        """`-t 2.733333` 到底算 82 还是 83 帧说不准，所以只许按帧号切。"""
        joined = ";".join(self._chain(box_split=(0, 1, 82)))
        self.assertNotRegex(joined, r"trim=start=\d")

    def test_the_split_happens_before_the_geometry(self):
        first = self._chain(box_split=(0, 1, 82))[0]
        self.assertLess(first.index("trim="), first.index("scale="))
        self.assertLess(first.index("trim="), first.index("crop="))

    def test_rgb24_still_enters_ass(self):
        last = self._chain(box_split=(0, 1, 82))[-1]
        self.assertIn("format=rgb24", last)
        self.assertLess(last.index("format=rgb24"), last.index("ass="))
        self.assertLess(last.index("ass="), last.index("format=yuv420p"))

    def test_kenburns_offsets_the_second_segment(self):
        """缓推的缩放曲线按输出帧序号算：第二段不带偏移，接缝处会跳回起点重推。"""
        joined = ";".join(self._chain(box_split=(0, 1, 82), mode="kenburns"))
        self.assertIn("/1800", joined)            # 60 s × 30 fps = 1800 帧，两段同一归一
        self.assertIn("(on+82)/1800", joined)
        self.assertIn("d=82", joined)             # 第一段只生成 82 帧
        self.assertIn("d=1718", joined)           # 1800 - 82
        self.assertNotIn("[bg]null", joined)      # 动画已并进两段，不该再有第二遍
        self.assertEqual(joined.count("zoompan="), 2)

    def test_waveform_reads_the_audio_where_it_actually_is(self):
        joined = ";".join(self._chain(box_split=(0, 1, 82), mode="waveform",
                                      audio_idx=2))
        self.assertIn("[2:a]showwaves", joined)
        self.assertNotIn("[1:a]showwaves", joined)


class TestBackgroundInputs(unittest.TestCase):
    """两张背景图的输入顺序与音频下标。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pmonset")
        self.plain = os.path.join(self.tmp, "plain.png")
        self.boxed = os.path.join(self.tmp, "boxed.png")
        for p in (self.plain, self.boxed):
            Image.new("RGB", (320, 180), (30, 30, 30)).save(p)
        self.audio = os.path.join(self.tmp, "a.wav")
        with open(self.audio, "wb") as f:
            f.write(b"RIFF")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _inputs(self, mode="static", bg_plain=None, box_frame=0, bg_path=None):
        cmd = []
        return (cmd,) + VE._bg_and_audio_inputs(
            cmd, self.boxed if bg_path is None else bg_path, self.audio, mode,
            60.0, 1920, 1080, 30, "0x0F1418",
            bg_plain=bg_plain, box_frame=box_frame)

    def test_single_image_keeps_the_audio_at_one(self):
        cmd, n, audio_idx, dual = self._inputs()
        self.assertEqual((n, audio_idx, dual), (2, 1, False))
        self.assertEqual(cmd.count("-i"), 2)

    def test_the_frameless_image_comes_first(self):
        cmd, n, audio_idx, dual = self._inputs(bg_plain=self.plain, box_frame=82)
        self.assertEqual((n, audio_idx, dual), (3, 2, True))
        self.assertLess(cmd.index(self.plain), cmd.index(self.boxed),
                        "0 号位必须是无框图：链上按输入下标认背景")
        self.assertLess(cmd.index(self.boxed), cmd.index(self.audio))

    def test_frame_zero_needs_no_second_image(self):
        for frame in (0, None):
            with self.subTest(box_frame=frame):
                _cmd, _n, audio_idx, dual = self._inputs(bg_plain=self.plain,
                                                         box_frame=frame)
                self.assertFalse(dual)
                self.assertEqual(audio_idx, 1)

    def test_a_frame_past_the_end_needs_no_second_image(self):
        # 60 s × 30 fps = 1800 帧；第 1800 帧已在片尾之外，没有可延后的区间
        for frame, want in ((1800, False), (1799, True), (1, True)):
            with self.subTest(box_frame=frame):
                _cmd, _n, _ai, dual = self._inputs(bg_plain=self.plain,
                                                   box_frame=frame)
                self.assertEqual(dual, want)

    def test_kenburns_takes_two_single_frames(self):
        """缓推档是单帧输入：这里加 `-loop 1 -t` 会让 zoompan 对每个输入帧各生成一遍。"""
        cmd, _n, _ai, dual = self._inputs(mode="kenburns", bg_plain=self.plain,
                                          box_frame=82)
        self.assertTrue(dual)
        self.assertNotIn("-loop", cmd)
        self.assertEqual(cmd.count("-i"), 3)

    def test_a_missing_plain_image_falls_back_to_one(self):
        _cmd, _n, audio_idx, dual = self._inputs(
            bg_plain=os.path.join(self.tmp, "nope.png"), box_frame=82)
        self.assertFalse(dual)
        self.assertEqual(audio_idx, 1)

    def test_colour_background_has_nothing_to_split(self):
        cmd = []
        n, audio_idx, dual = VE._bg_and_audio_inputs(
            cmd, None, self.audio, "static", 60.0, 1920, 1080, 30, "0x0F1418",
            bg_plain=self.plain, box_frame=82)
        self.assertEqual((n, audio_idx, dual), (2, 1, False))
        self.assertIn("lavfi", cmd)


class TestSegmentBlockerBoxFrame(unittest.TestCase):
    """分段并行与「框延后出现」不并存：分段切的是已经拼好的时间轴。"""

    def test_the_split_blocks_segmenting_even_on_static(self):
        why = VE.segment_blocker({"animation.mode": "static"}, 82)
        self.assertNotEqual(why, "")
        self.assertIn("错位", why)

    def test_no_split_is_still_segmentable(self):
        self.assertEqual(VE.segment_blocker({"animation.mode": "static"}, 0), "")


class TestBakeWithoutBox(unittest.TestCase):
    """`box=False` 只压暗、不画框 —— 两张背景图的前一张就是它。"""

    def setUp(self):
        self.cfg = dict(ConfigManager().data())
        self.cfg["video.bg_dim"] = 0.3
        self.cfg["subtitle.bg_alpha"] = 128
        self.cfg["subtitle.outline"] = 2
        self.tmp = tempfile.mkdtemp(prefix="pmnobox")
        self.src = os.path.join(self.tmp, "src.png")
        Image.new("RGB", (1920, 1080), (200, 200, 200)).save(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bake(self, name, box):
        return AF.bake_static_layers(self.src, os.path.join(self.tmp, name),
                                     self.cfg, 1920, 1080, "", box=box)

    def _centre(self):
        g = SE.frame_of(self.cfg, 1920, 1080, "")
        return ((g["x1"] + g["x2"]) // 2, (g["top"] + g["bottom"]) // 2)

    def test_no_box_leaves_the_subtitle_area_alone(self):
        img = Image.open(self._bake("plain.png", False)).convert("RGB")
        self.assertEqual(img.getpixel(self._centre()), img.getpixel((50, 50)),
                         "框没关掉：字幕区与别处不该有差别")

    def test_the_dim_still_runs_without_the_box(self):
        img = Image.open(self._bake("plain.png", False)).convert("RGB")
        for want, got in zip((200, 200, 200), img.getpixel((50, 50))):
            self.assertLessEqual(abs(got - round(want * 0.7)), 1,
                                 "压暗跟着 box 一起被关掉了")

    def test_the_two_images_differ_only_inside_the_box(self):
        plain = Image.open(self._bake("plain.png", False)).convert("RGB")
        boxed = Image.open(self._bake("boxed.png", True)).convert("RGB")
        self.assertEqual(plain.getpixel((50, 50)), boxed.getpixel((50, 50)))
        self.assertNotEqual(plain.getpixel(self._centre()),
                            boxed.getpixel(self._centre()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
