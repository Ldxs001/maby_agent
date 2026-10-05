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

"""分段并行的三条硬约束：核数怎么分、切点怎么切、滤镜链的口径。

这里不跑 ffmpeg——真渲对账在 `tools/probes/parallel_render_check.py`（两条路都用
crf 0 编，逐帧 MSE 必须全 0）。单测守的是容易被「顺手调一个数」改坏、又不会当场
报错的那几条：

1. **并行度默认 1，机制不许超订**。真素材实测切段只会更慢（整期 1 路 156.6 秒 /
   6 路 177.9 秒），所以默认上限写成 1；但「二核机器不许硬开六路」这条机制仍在
   （`cap` 参数），每路的编码线程数也必须把核**分掉**——不分的 6 路就是 6×24 个
   线程挤 24 个核。
2. **切点帧对齐且不重不漏**。段边界错半帧，接起来就是跳一帧或停一帧，画面上
   很难一眼看出来，但会在字幕滚动里露出来。
3. **`format=rgb24` 必须紧贴在 `ass` 之前**。libass 的取色口径由「进到它里面那帧是
   什么格式」决定：同一份 ASS 同一像素，rgb24 进白字出 233，yuv420p 进出 253——
   差 16～20 个码值，字幕亮度肉眼可辨。这一条没有任何别的地方会报错。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import video_engine as VE  # noqa: E402


class TestRenderWorkers(unittest.TestCase):
    """默认并行度恒为 1——实测这条链切段只会更慢（理由见 `render_workers`）。

    核数自适应的**机制**留着（`cap` 参数），因为它回答的是「二核机器不许硬开六路」
    那类问题：`cap=6` 时二核算出来仍是 1。但**默认上限是 1**，标的是实测结果，不是
    拍的数：真素材 22.7 分钟一期，1 路 156.6 秒 / 6 路 177.9 秒（0.88x）。
    """

    def test_default_is_serial_on_every_machine(self):
        for cpu in (1, 2, 4, 8, 24, 128):
            with self.subTest(cpu=cpu):
                self.assertEqual(VE.render_workers(cpu), 1)
        self.assertEqual(VE.WORKERS_MAX, 1)

    def test_mechanism_never_oversubscribes_low_core_machines(self):
        """机制本身是对的：核不够就不开那么多路（cap 只是把它放开后的样子）。"""
        for cpu in (1, 2, 3):
            with self.subTest(cpu=cpu):
                self.assertEqual(VE.render_workers(cpu, cap=6), 1)
        self.assertEqual(VE.render_workers(8, cap=6), 2)
        self.assertEqual(VE.render_workers(24, cap=6), 6)
        self.assertEqual(VE.render_workers(128, cap=6), 6)

    def test_never_zero_and_never_negative(self):
        for cpu in (0, -1, 1, 2, 24):
            with self.subTest(cpu=cpu):
                self.assertGreaterEqual(VE.render_workers(cpu), 1)


class TestEncoderThreads(unittest.TestCase):
    """核是**分**出去的，不是**乘**出来的。"""

    def test_budget_is_divided_by_worker_count(self):
        self.assertEqual(VE.encoder_threads(1, cpu=24), 24)
        self.assertEqual(VE.encoder_threads(2, cpu=24), 12)
        self.assertEqual(VE.encoder_threads(4, cpu=24), 6)
        self.assertEqual(VE.encoder_threads(6, cpu=24), 4)
        # 6 路 × 4 线程 = 24 核，恰好一机；不许出现 6 路各按 24 核开线程
        self.assertLessEqual(VE.encoder_threads(VE.WORKERS_MAX, cpu=24)
                             * VE.WORKERS_MAX, 24)

    def test_never_zero(self):
        for cpu in (1, 2, 3, 24):
            for workers in (1, 2, 6, 100):
                with self.subTest(cpu=cpu, workers=workers):
                    self.assertGreaterEqual(VE.encoder_threads(workers, cpu=cpu), 1)


class TestSegmentBlocker(unittest.TestCase):
    """链上只依赖「静态背景 + 绝对时间」才切得动。"""

    def test_static_without_portrait_can_be_cut(self):
        self.assertEqual(VE.segment_blocker({"animation.mode": "static"}), "")

    def test_frame_index_driven_animations_are_blocked(self):
        for mode in ("kenburns", "waveform", "spectrum"):
            with self.subTest(mode=mode):
                self.assertNotEqual(VE.segment_blocker({"animation.mode": mode}), "",
                                    "%s 按帧序号/音频流算，切段会各段从头" % mode)

    def test_portraits_block_it_too(self):
        import tempfile
        from PIL import Image
        d = tempfile.mkdtemp(prefix="pmpar")
        p = os.path.join(d, "portrait_a.png")
        Image.new("RGB", (10, 10), (0, 0, 0)).save(p)
        cfg = {"animation.mode": "static", "speaker_indicator.portrait_a": p}
        self.assertNotEqual(VE.segment_blocker(cfg), "",
                            "配了立绘就别切：overlay 还牵着额外输入")

    def test_missing_portrait_file_is_not_a_portrait(self):
        cfg = {"animation.mode": "static",
               "speaker_indicator.portrait_a": os.path.join(ROOT, "没有这张.png")}
        self.assertEqual(VE.segment_blocker(cfg), "")

    def test_unknown_mode_is_treated_as_animation(self):
        """认不出的档按动画处理（宁可串行，也别切出各段从头）。"""
        self.assertNotEqual(VE.segment_blocker({"animation.mode": "what"}), "")


class TestCutPoints(unittest.TestCase):
    """切点：帧对齐、首尾覆盖整段、段间无重叠无缝隙、帧数守恒。"""

    FPS = 30
    DUR = 25.0

    @staticmethod
    def _timings():
        # 每句 2 秒、句间停 0.5 秒 → 空隙落在 2.0–2.5、4.5–5.0、7.0–7.5 …
        t, out = 0.0, []
        for _ in range(10):
            out.append({"start": t, "end": t + 2.0})
            t += 2.5
        return out

    def test_contiguous_and_frame_aligned(self):
        total = int(round(self.DUR * self.FPS))
        for workers in (1, 2, 3, 4, 6, 9):
            spans = VE.cut_points(self.DUR, self.FPS, workers, self._timings())
            with self.subTest(workers=workers):
                self.assertEqual(spans[0][0], 0.0)
                self.assertAlmostEqual(spans[-1][1], self.DUR, places=9)
                self.assertEqual(sum(s[2] for s in spans), total)
                for t0, t1, n in spans:
                    self.assertAlmostEqual(t0 * self.FPS, round(t0 * self.FPS),
                                           places=6, msg="段首不在整帧上：%r" % t0)
                    self.assertAlmostEqual(t1 * self.FPS, round(t1 * self.FPS),
                                           places=6, msg="段尾不在整帧上：%r" % t1)
                    self.assertEqual(round((t1 - t0) * self.FPS), n)
                for a, b in zip(spans, spans[1:]):
                    self.assertEqual(a[1], b[0], "段与段接不上（重叠或缝隙）")

    def test_cuts_prefer_the_gaps_between_sentences(self):
        spans = VE.cut_points(self.DUR, self.FPS, 4, self._timings())
        gaps = tuple(x + 0.1 for x in (2.0, 4.5, 7.0, 9.5, 12.0, 14.5, 17.0, 19.5, 22.0))
        for t0, _t1, _n in spans[1:]:
            self.assertTrue(any(abs(t0 - f) <= 0.5 / 1 for f in gaps),
                            "切点 %.3f 落在了句子中间" % t0)

    def test_hard_cuts_when_no_gap_is_reachable(self):
        """句间没有空隙也得切得动：不回退、不报错（宁可切在句子中间）。"""
        solid = [{"start": 0.0, "end": self.DUR}]
        spans = VE.cut_points(self.DUR, self.FPS, 4, solid)
        self.assertEqual(len(spans), 4)
        self.assertEqual(sum(s[2] for s in spans),
                         int(round(self.DUR * self.FPS)))

    def test_too_short_to_cut_stays_in_one_piece(self):
        """每段至少 1 秒，不然切了也只是多付进程开销。"""
        for workers in (1, 6, 24):
            with self.subTest(workers=workers):
                self.assertEqual(len(VE.cut_points(3.0, 30, workers, [])), 1)

    def test_missing_timings_is_not_a_crash(self):
        self.assertEqual(len(VE.cut_points(self.DUR, self.FPS, 4, None)), 4)


class TestGraphFormat(unittest.TestCase):
    """滤镜链的口径：进 ass 的必须是 rgb24；分段的 trim 在归一之前、归零在 ass 之后。"""

    def _chain(self, pre="", tail=""):
        cfg = {"animation.mode": "static", "video.fps": 30}
        return VE._graph(cfg, 1920, 1080, 30, 60.0, os.path.join(ROOT, "x.ass"),
                         "", [], [], pre, tail)

    def test_rgb24_is_what_enters_ass(self):
        last = self._chain()[-1]
        self.assertIn("format=rgb24", last)
        self.assertLess(last.index("format=rgb24"), last.index("ass="),
                        "先转 rgb24 再进 ass，顺序不能反")
        self.assertLess(last.index("ass="), last.index("format=yuv420p"),
                        "ass 之后才回到 yuv420p")

    def test_segment_cut_happens_before_the_background_is_normalised(self):
        chain = self._chain(pre="trim=start=1.000000:end=2.000000,")
        self.assertIn("trim=start=1.000000:end=2.000000", chain[0])
        self.assertLess(chain[0].index("trim="), chain[0].index("scale="),
                        "trim 必须挂在 scale 之前，否则每段都要白算一遍缩放")

    def test_tail_zeroes_the_timeline_after_ass(self):
        last = self._chain(tail=",setpts=PTS-STARTPTS")[-1]
        self.assertLess(last.index("ass="), last.index("setpts=PTS-STARTPTS"),
                        "setpts 必须在 ass 之后：ass 要拿绝对时刻，编码器要 0 基")

    def test_the_dim_is_not_on_the_chain_any_more(self):
        """压暗是静态层，已烘进背景图。链上再有 drawbox 就是同一件事做两遍。"""
        joined = ";".join(self._chain())
        self.assertNotIn("drawbox", joined)
        self.assertFalse(hasattr(VE, "_dim_filter"),
                         "_dim_filter 已随烘焙删除，留着会让人以为链上还能压暗")


if __name__ == "__main__":
    unittest.main(verbosity=2)
