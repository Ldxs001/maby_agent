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

"""排版与画面回归测试。

五条缺陷都在这里钉住：

1. 给主标题下方画框线时用了与字号无关的绝对偏移（ty+118），字号 108 的字
   实际高约 130，线正好横穿标题字。这类缺陷不能靠调数值解决——换个字号
   或换套字体又会穿，只能改成按实测包围盒推位置。
2. 缓推档用 min(zoom+常数, 上限) 表达缩放，62 秒的片子在第 9.5 秒就撞顶，
   之后 52 秒完全静止。缩放必须按整段帧数归一。
3. 文字块顶边若随文案行数浮动，同一档节目每集的主标题高度都不一样，
   横屏换竖屏还会再挪一次。块顶必须钉住。
4. 压暗若在带范围的 YUV 里叠黑，亮度与色度会被分开处理，等比压暗变成偏色。
   压暗与字幕框现在是静态层、由烘焙一次画进背景图，这一条连同「先压暗后画框」
   与「框几何只有一个出处」一起守着（见 `TestStaticLayerBake`）。
5. 声波间距按像素写死，小于相邻振幅之和，包络从第一帧就重叠。
   留白必须是结构条件（相邻中线间距 = 振幅之和 + 留白），不能是手感数值。
"""

import math
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw                                  # noqa: E402

from podcast_maker import assets_factory as AF                    # noqa: E402
from podcast_maker import subtitle_engine as SE                   # noqa: E402
from podcast_maker import video_engine as VE                      # noqa: E402
from podcast_maker.config_manager import PARAM_SPEC               # noqa: E402

LINES = {
    "brand": "COGITO · SCRIBO",
    "program": "我思故我写",
    "subtitle": "我思故我写 · 播客系列",
    "tagline": "能不能让代码干代码的活",
    "title": "链与两头",
    "episode_no": "3",
    "attribution": "wUwproject · CC BY-SA 4.0",
}

# 背景与封面共用同一组三色（accent / light / muted）。
COLORS = ((201, 164, 92), (232, 237, 248), (150, 165, 195))

CANVAS = [(1920, 1080), (1080, 1920), (1080, 1440), (1080, 1080)]


def _font_path():
    try:
        return AF.resolve_font()["path"]
    except AF.AssetError:
        return None


def _titles(seq):
    """序列里承载主标题的那一项。"""
    return next(it for it in seq if it.get("label") == "主标题")


class TestBackgroundLayout(unittest.TestCase):
    """位置必须由实测包围盒推出，元素之间不得压叠。"""

    @classmethod
    def setUpClass(cls):
        cls.path = _font_path()
        if not cls.path:
            raise unittest.SkipTest("未找到中文字体")
        cls.draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def _seq(self, size, hero_size=None, titles_only=False):
        w, h = size
        scale = min(w, h) / 1080.0
        fonts = AF._font_set(self.path, scale, "bg")
        if hero_size:
            fonts["hero"] = AF._font(self.path, hero_size)
        lines = ({"program": LINES["program"], "title": LINES["title"]}
                 if titles_only else LINES)
        return AF.frame_sequence("bg", lines, fonts, COLORS, w), fonts

    def _flow(self, size, hero_size=None, titles_only=False):
        seq, fonts = self._seq(size, hero_size, titles_only)
        start, _wave, _ = AF.background_layout(size, seq, fonts["small"].size)
        return AF._draw_sequence(
            AF._Flow(self.draw, size[0] // 2, start, dry=True), seq)

    def test_marks_recorded_for_every_element(self):
        flow = self._flow((1920, 1080))
        self.assertEqual(len(flow.marks), 9)
        self.assertTrue(all(m["bottom"] > m["top"] for m in flow.marks))

    def test_no_element_overlaps_the_previous_one(self):
        for size in CANVAS:
            with self.subTest(size=size):
                marks = self._flow(size).marks
                for prev, cur in zip(marks, marks[1:]):
                    self.assertGreaterEqual(
                        cur["top"], prev["bottom"] - 0.5,
                        "%s 与 %s 重叠" % (prev["label"], cur["label"]))

    def test_rule_sits_below_the_title(self):
        """就是这条线曾经横穿标题字。"""
        for size in CANVAS:
            with self.subTest(size=size):
                marks = self._flow(size).marks
                title = next(m for m in marks if m["label"] == "主标题")
                rule = next(m for m in marks if m["label"] == "主标题下框线")
                self.assertGreater(rule["top"], title["bottom"])

    def test_larger_title_never_collides(self):
        """字号变了位置必须跟着变。

        旧实现写死 ty+118，这个用例会失败：字号 200 的标题底边远超 118。
        """
        for size in CANVAS:
            for hero_size in (60, 108, 160, 240):
                with self.subTest(size=size, hero=hero_size):
                    marks = self._flow(size, hero_size=hero_size).marks
                    title = next(m for m in marks if m["label"] == "主标题")
                    rule = next(m for m in marks if m["label"] == "主标题下框线")
                    self.assertGreaterEqual(
                        rule["top"], title["bottom"] - 0.5,
                        "标题字号 %d 时框线压到了标题" % hero_size)

    def test_block_stays_inside_the_canvas(self):
        for size in CANVAS:
            with self.subTest(size=size):
                flow = self._flow(size)
                self.assertLess(flow.y, size[1] * 0.75,
                                "文字块占了画面四分之三以上，说明字号或间距失控")

    def test_block_top_is_fixed_regardless_of_line_count(self):
        """块顶钉在 block_top，行数不能把主标题推上推下。

        品牌行、标语、署名填不填由配置决定，主标题必须落在同一高度。若改成按
        可用区居中，同一档节目每集的主标题都不一样高，横竖屏还会各挪一次。
        """
        for size in CANVAS:
            with self.subTest(size=size):
                want = size[1] * AF.LAYOUT["block_top"]
                for titles_only in (False, True):
                    seq, fonts = self._seq(size, titles_only=titles_only)
                    start, _wave, flow = AF.background_layout(size, seq, fonts["small"].size)
                    self.assertAlmostEqual(start, want, delta=0.5,
                                           msg="文字块顶边没有钉在 block_top")
                    self.assertLess(start + flow.y, size[1] * 0.75,
                                    "文字块占了画面四分之三以上，说明字号或间距失控")

    def test_inner_offsets_do_not_depend_on_orientation(self):
        """块内各行只按字号排，与画幅无关。

        字号按短边缩放，横竖屏短边相同，块内相对位置就应当相同——否则换个画幅
        整块的行距都会变。分隔线粗细取自画布宽度，两种画幅下会差几像素，留一点容差。
        """
        offs = []
        for size in ((1920, 1080), (1080, 1920)):
            seq, fonts = self._seq(size)
            _start, _wave, flow = AF.background_layout(size, seq, fonts["small"].size)
            offs.append([m["top"] for m in flow.marks])
        for a, b in zip(*offs):
            self.assertAlmostEqual(a, b, delta=4.0,
                                   msg="同一份文案在横竖屏下的块内相对位置相差过大")

    def test_wave_stays_below_the_text_block(self):
        for size in CANVAS:
            for titles_only in (False, True):
                with self.subTest(size=size, titles_only=titles_only):
                    seq, fonts = self._seq(size, titles_only=titles_only)
                    start, wave, probe = AF.background_layout(size, seq, fonts["small"].size)
                    self.assertGreater(wave, start + probe.y,
                                       "声波压到了文字块")
                    self.assertLessEqual(wave, size[1] * AF.LAYOUT["wave_max"] + 1.0)

    def test_wave_keeps_a_similar_distance_in_both_orientations(self):
        """声波不能钉死在画布比例上：两种画幅下与文字的距离应当接近。"""
        gaps = []
        for size in ((1920, 1080), (1080, 1920)):
            seq, fonts = self._seq(size)
            start, wave, probe = AF.background_layout(size, seq, fonts["small"].size)
            gaps.append((wave - (start + probe.y)) / min(size))
        self.assertLess(abs(gaps[0] - gaps[1]), 0.2, "横竖屏声波与文字的距离相差过大")


class TestCoverLayout(unittest.TestCase):
    """封面整块居中，且元素之间不得压叠。"""

    @classmethod
    def setUpClass(cls):
        cls.path = _font_path()
        if not cls.path:
            raise unittest.SkipTest("未找到中文字体")
        cls.draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def _flow(self, size, preset, hero_size=None):
        w, h = size
        scale = min(w, h) / 1080.0
        over = (AF.COVER_SQUARE_OVERRIDES if size == AF.COVER_SIZES["1x1"]
                else None)
        fonts = AF._font_set(self.path, scale, "cover", over)
        if hero_size:
            fonts["hero"] = AF._font(self.path, hero_size)
        seq = AF.frame_sequence("cover", LINES, fonts, COLORS, w, preset=preset)
        # 折行上限要和实绘一致：干跑不折、实绘折，量出的块高差一整行，
        # 居中位置就错位了。
        max_w = w * AF.LAYOUT["text_max_ratio"]
        probe = AF._draw_sequence(
            AF._Flow(self.draw, w // 2, 0.0, dry=True, max_w=max_w), seq)
        return AF._draw_sequence(
            AF._Flow(self.draw, w // 2, max(0.0, (h - probe.y) / 2.0),
                     dry=True, max_w=max_w), seq), probe

    def test_no_element_overlaps_for_any_preset(self):
        for preset in ("book", "episode", "minimal"):
            for size in AF.COVER_SIZES.values():
                with self.subTest(preset=preset, size=size):
                    flow, _ = self._flow(size, preset)
                    for prev, cur in zip(flow.marks, flow.marks[1:]):
                        self.assertGreaterEqual(
                            cur["top"], prev["bottom"] - 0.5,
                            "%s 与 %s 重叠" % (prev["label"], cur["label"]))

    def test_block_is_vertically_centered(self):
        for preset in ("book", "episode", "minimal"):
            for size in AF.COVER_SIZES.values():
                with self.subTest(preset=preset, size=size):
                    flow, probe = self._flow(size, preset)
                    h = size[1]
                    start = max(0.0, (h - probe.y) / 2.0)
                    self.assertAlmostEqual(flow.y, start + probe.y, delta=1.0)
                    self.assertGreaterEqual(start, 0.0)
                    self.assertLessEqual(flow.y, h, "块超出了画布")

    def test_big_title_keeps_distance_from_next_line(self):
        """主副标题贴在一起就是封面曾经的样子。"""
        for size in AF.COVER_SIZES.values():
            with self.subTest(size=size):
                flow, _ = self._flow(size, "book")
                big = max(flow.marks, key=lambda m: m["bottom"] - m["top"])
                nxt = next(m for m in flow.marks if m["top"] >= big["bottom"] - 0.5)
                self.assertGreater(nxt["top"] - big["bottom"], 10.0,
                                   "大字之后没有留出间距")

    def test_large_title_still_does_not_collide(self):
        for size in AF.COVER_SIZES.values():
            with self.subTest(size=size):
                flow, _ = self._flow(size, "book", hero_size=200)
                for prev, cur in zip(flow.marks, flow.marks[1:]):
                    self.assertGreaterEqual(cur["top"], prev["bottom"] - 0.5)


class TestTextHierarchy(unittest.TestCase):
    """画面上的字谁大谁小，是规矩不是手感。

    最大的一档给节目名（主标题），不给期标题：播客卖的是系列品牌，不是这一期
    讲什么。期标题字数不定，长起来在最大档只能折行，一期一个样。这两条一旦
    被谁改回去，画面上只是「又变丑了」，没有任何一处会报错——只能钉在这里。
    """

    @classmethod
    def setUpClass(cls):
        cls.path = _font_path()
        if not cls.path:
            raise unittest.SkipTest("未找到中文字体")

    def test_hero_is_the_program_not_the_episode(self):
        """主标题位印的是节目名，期标题另起一行。"""
        fonts = AF._font_set(self.path, 1.0, "bg")
        seq = AF.frame_sequence("bg", LINES, fonts, COLORS, 1920)
        hero = next(it for it in seq if it.get("label") == "主标题")
        episode = next(it for it in seq if it.get("label") == "期标题")
        self.assertEqual(hero["text"], LINES["program"])
        self.assertEqual(episode["text"], LINES["title"])
        self.assertGreater(hero["font"].size, episode["font"].size,
                           "期标题不该大过主标题")

    def test_background_hero_outranks_every_other_line(self):
        fonts = AF._font_set(self.path, 1.0, "bg")
        seq = AF.frame_sequence("bg", LINES, fonts, COLORS, 1920)
        hero = next(it for it in seq if it.get("label") == "主标题")["font"].size
        for it in seq:
            if it["k"] != "text" or it.get("label") == "主标题":
                continue
            with self.subTest(label=it.get("label")):
                self.assertGreater(hero, it["font"].size,
                                   "「%s」没有小于主标题" % it.get("label"))

    def test_cover_hero_outranks_every_other_line(self):
        """主标题是封面上最大的一档，且封面不印任何跟期的行。

        封面跟项目、不跟期：期标题与期数都不该出现在封面上。这条被谁改回去，
        画面上只是「又变回按期一人一张」，没有任何一处会报错——只能钉在这里。
        """
        fonts = AF._font_set(self.path, 1.0, "cover")
        for preset in ("book", "episode", "minimal"):
            seq = AF.frame_sequence("cover", LINES, fonts, COLORS, 1920,
                                    preset=preset)
            labels = [it.get("label") for it in seq]
            hero = next(it for it in seq if it.get("label") == "主标题")
            with self.subTest(preset=preset):
                self.assertEqual(hero["text"], LINES["program"])
                for it in seq:
                    if it["k"] != "text" or it.get("label") == "主标题":
                        continue
                    self.assertGreater(hero["font"].size, it["font"].size,
                                       "「%s」没有小于主标题" % it.get("label"))
                self.assertNotIn("期标题", labels, "封面跟项目不跟期，不该印期标题")
                self.assertNotIn("期数", labels, "封面跟项目不跟期，不该印期数")

    def test_every_preset_has_its_own_layout(self):
        """三个档位必须真的排出三种样子。

        从前 book 与 episode 走的是同一条分支（函数里只判过 minimal），
        配置里那个下拉选了等于没选——这种空开关只能靠比对形状来拦。
        期标题退出封面之后，episode 档只剩副标题换一档字号，但仍与 book 不同。
        """
        fonts = AF._font_set(self.path, 1.0, "cover")
        shapes = {}
        for preset in ("book", "episode", "minimal"):
            seq = AF.frame_sequence("cover", LINES, fonts, COLORS, 1920,
                                    preset=preset)
            shapes[preset] = tuple(
                (it["k"], it.get("label"),
                 it["font"].size if it.get("font") else 0) for it in seq)
        self.assertEqual(len(set(shapes.values())), 3,
                         "档位之间排出了同一份结果：%s" % shapes)


class TestFrameLines(unittest.TestCase):
    """画面元素清单只有一张表，这里是它的钉子。

    哪一行印在哪张画面上、从哪来、跟谁走，全在 `assets_factory.FRAME_LINES`。
    从前封面与背景各写一份序列，同一个元素在两处各描述一遍——改一处忘一处，
    两张画面就各印各的了。这组用例钉住表本身，也钉住两张画面各自的取值。
    """

    def test_every_row_says_who_it_follows(self):
        for row in AF.FRAME_LINES:
            with self.subTest(key=row[0]):
                self.assertIn(row[2], ("全局", "项目", "期"),
                              "「跟谁」只能是这三样之一")

    def test_every_row_is_printed_somewhere(self):
        for row in AF.FRAME_LINES:
            with self.subTest(key=row[0]):
                self.assertTrue(row[7] or row[8],
                                "%s 两张画面都不印，那一行就是死的" % row[1])

    def test_cover_keeps_the_project_lines_only(self):
        """封面跟项目：跟期的两行（期标题、期数）不进封面。"""
        cover = [r for r in AF.FRAME_LINES if r[7]]
        self.assertNotIn("title", {r[0] for r in cover})
        self.assertNotIn("episode_no", {r[0] for r in cover})
        self.assertTrue(all(r[2] != "期" for r in cover),
                        "封面上出现了跟期的行")

    def test_background_takes_both_episode_lines(self):
        """背景跟期：期标题与期数都要印。"""
        bg = {r[0] for r in AF.FRAME_LINES if r[8]}
        self.assertIn("title", bg)
        self.assertIn("episode_no", bg)

    def test_font_and_color_roles_are_known(self):
        for row in AF.FRAME_LINES:
            with self.subTest(key=row[0]):
                self.assertIn(row[5], AF.FONT_SIZES, "字号档不在字号表里")
                self.assertIn(row[6], ("accent", "light", "muted"),
                              "颜色档不认识")

    def test_the_two_pictures_are_built_by_one_constructor(self):
        """两张画面必须出自同一个构造器：各写一份的话迟早各印各的。"""
        self.assertFalse(hasattr(AF, "_cover_sequence"))
        self.assertFalse(hasattr(AF, "_background_sequence"))


class TestLayoutSpec(unittest.TestCase):
    """排版器与规格表的关系。

    间距一旦允许就地写数值，就会再次散成各处各一套——线穿标题就是那么来的。
    这里把「间距只能取自 LAYOUT」变成可执行约束。
    """

    @classmethod
    def setUpClass(cls):
        cls.path = _font_path()
        if not cls.path:
            raise unittest.SkipTest("未找到中文字体")

    def _sequences(self):
        bg = AF.frame_sequence("bg", LINES,
                               AF._font_set(self.path, 1.0, "bg"), COLORS, 1920)
        cover_fonts = AF._font_set(self.path, 1.0, "cover")
        covers = [AF.frame_sequence("cover", LINES, cover_fonts, COLORS, 1920,
                                    preset=p)
                  for p in ("book", "episode", "minimal")]
        return [bg] + covers

    def test_every_gap_comes_from_the_spec_table(self):
        allowed = {v for k, v in AF.LAYOUT.items() if k.startswith("gap_")}
        allowed.add(0.0)
        for seq in self._sequences():
            for it in seq:
                with self.subTest(item=it.get("label", it["k"])):
                    self.assertIn(it.get("gap", 0.0), allowed,
                                  "间距没有取自 LAYOUT，属于就地写数值")

    def test_spec_keeps_only_ratios(self):
        """规格表里不该出现与字号无关的绝对像素。"""
        for key, val in AF.LAYOUT.items():
            if not key.startswith("gap_"):
                continue
            with self.subTest(key=key):
                self.assertLessEqual(val, 10.0, "%s 像是个绝对像素值" % key)

    def test_every_sequence_element_can_be_measured(self):
        """序列必须能落笔：每种类型该有的字段齐备。"""
        for seq in self._sequences():
            self.assertTrue(seq)
            for it in seq:
                with self.subTest(item=it.get("label", it["k"])):
                    self.assertIn(it["k"], ("text", "rule", "pill"))
                    self.assertIn("color", it)
                    if it["k"] == "rule":
                        self.assertIn("span", it)
                        self.assertIn("w", it)
                    else:
                        self.assertIn("font", it)
                        self.assertTrue(it["text"])


class TestGlyphCoverage(unittest.TestCase):
    """缺字形会画出豆腐块，属于静默产出次品，必须拦下。"""

    @classmethod
    def setUpClass(cls):
        cls.path = _font_path()
        if not cls.path:
            raise unittest.SkipTest("未找到中文字体")
        cls.font = AF._font(cls.path, 48)

    def test_notdef_is_detected(self):
        # 微软雅黑没有 ▸（U+25B8），缺字形必须判为不支持
        self.assertFalse(AF.glyph_ok(self.font, "▸"))

    def test_present_glyphs_pass(self):
        for ch in ("中", "A", "·", "—", "◆"):
            with self.subTest(ch=ch):
                self.assertTrue(AF.glyph_ok(self.font, ch))

    def test_assert_glyphs_raises_with_the_offending_char(self):
        with self.assertRaises(AF.AssetError) as cm:
            AF.assert_glyphs(self.font, "链与两头 🎙", "背景主标题")
        self.assertIn("🎙", str(cm.exception))
        self.assertIn("背景主标题", str(cm.exception))

    def test_assert_glyphs_passes_on_plain_text(self):
        AF.assert_glyphs(self.font, "链与两头", "背景主标题")

    def test_the_badge_line_is_gone(self):
        """副标签（▸ PODCAST）连同它的符号探测一并删掉了，别再回来。

        那一行只是把「这是播客」又说了一遍——收听的人不需要被告知自己在听播客。
        """
        self.assertFalse(hasattr(AF, "kind_line"))
        self.assertFalse(hasattr(AF, "KIND_MARKS"))


class TestAnimation(unittest.TestCase):
    """缩放必须全程匀速，且默认不动。"""

    def test_default_animation_is_static(self):
        self.assertEqual(PARAM_SPEC["animation.mode"]["default"], "static")

    def test_zoom_range_is_subtle(self):
        self.assertLessEqual(PARAM_SPEC["animation.zoom_max"]["default"], 1.06,
                             "缩放幅度过大会把背景构图带跑")

    def test_kenburns_zoom_is_normalised_by_frame_count(self):
        """写死上限会让运动集中在前几秒然后静止。"""
        for duration in (10.0, 62.0, 120.0):
            with self.subTest(duration=duration):
                frames = int(round(duration * 30))
                frag, _ = VE._animation_filter("kenburns", 1920, 1080, 30,
                                               duration, "C9A45C", 1.04)
                self.assertEqual(len(frag), 1)
                self.assertNotIn("min(zoom", frag[0],
                                 "缩放带上限会在中途撞顶，之后完全静止")
                self.assertIn("on/%d" % frames, frag[0],
                              "缩放没有按整段帧数归一")

    def test_kenburns_output_length_matches_duration(self):
        frag, _ = VE._animation_filter("kenburns", 1920, 1080, 30, 62.0,
                                       "C9A45C", 1.04)
        self.assertIn("d=1860", frag[0])

    def test_only_kenburns_wants_a_single_frame_input(self):
        self.assertTrue(VE.bg_wants_single_frame("kenburns"))
        for mode in ("static", "waveform", "spectrum", "none", ""):
            with self.subTest(mode=mode):
                self.assertFalse(VE.bg_wants_single_frame(mode))

    def test_static_produces_no_filter_fragment(self):
        frag, _ = VE._animation_filter("static", 1920, 1080, 30, 60.0, "C9A45C")
        self.assertEqual(frag, [])


class TestStaticLayerBake(unittest.TestCase):
    """整幅压暗与字幕框改成了**静态层**：一次画进背景图，合成时不再逐帧重算。

    这两层的算式只跟背景像素与一组常量有关、跟帧序号无关，逐帧重算等于把同一道
    算式抄一千八百遍（60 秒片段实测：压暗 9.2 秒、框 1.5～12.9 秒）。挪进烘焙之后
    原来守在这里的断言照旧有效，只是改守烘焙这一步：

    · **等比压暗**——压暗若在带范围的 YUV 里叠黑，亮度与色度被分开处理，蓝会被
      压掉四成、红几乎没动，成片比背景图与封面明显偏色。判据是「三个通道按同一
      比例走」，取样用 (40,90,200) 把通道差放大。
    · **先压暗、后画框**——框是半透明的，顺序反了框底会被压暗两遍。
    · **框的几何来自 `subtitle_engine.frame_of`**——ASS、界面预览、烘焙三处同一个
      出处；这里断言烘焙出来的框正好落在那个矩形的内外边界上。
    """

    def setUp(self):
        import podcast_maker.config_manager as CM
        self.cfg = dict(CM.ConfigManager().data())
        self.cfg["subtitle.bg_alpha"] = 0     # 默认只量压暗，不掺框
        self.cfg["subtitle.outline"] = 0
        self.tmp = tempfile.mkdtemp(prefix="pmbake")
        self.w, self.h = 1920, 1080

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bake(self, color, name="bg"):
        src = os.path.join(self.tmp, name + ".png")
        Image.new("RGB", (self.w, self.h), color).save(src)
        return AF.bake_static_layers(src, os.path.join(self.tmp, name + "_out.png"),
                                     self.cfg, self.w, self.h)

    def _box(self):
        return SE.frame_of(self.cfg, self.w, self.h, "")

    def test_dim_scales_all_channels_equally(self):
        self.cfg["video.bg_dim"] = 0.3
        got = Image.open(self._bake((40, 90, 200))).convert("RGB").getpixel((50, 50))
        for want, g in zip((40, 90, 200), got):
            self.assertLessEqual(abs(g - round(want * 0.7)), 1,
                                 "压暗不是等比：(40,90,200) → %s" % (got,))

    def test_no_dim_leaves_background_alone(self):
        self.cfg["video.bg_dim"] = 0.0
        got = Image.open(self._bake((40, 90, 200))).convert("RGB").getpixel((50, 50))
        self.assertEqual(got, (40, 90, 200))

    def test_box_sits_where_frame_of_says(self):
        self.cfg["video.bg_dim"] = 0.0
        self.cfg["subtitle.bg_alpha"] = 128
        self.cfg["subtitle.outline"] = 2
        img = Image.open(self._bake((255, 255, 255))).convert("RGB")
        g = self._box()
        cx = (g["x1"] + g["x2"]) // 2
        self.assertNotEqual(img.getpixel((cx, (g["top"] + g["bottom"]) // 2)),
                            (255, 255, 255), "框没画上去")
        self.assertEqual(img.getpixel((cx, g["top"] - 20)), (255, 255, 255),
                         "框画到了矩形外面")
        # 描边只往外扩整宽：矩形外第一个像素是描边，矩形内第一个像素是填充
        self.assertNotEqual(img.getpixel((g["x1"] - 1, g["top"] + 10)),
                            (255, 255, 255), "描边没有往外扩")
        self.assertNotEqual(img.getpixel((g["x1"], g["top"] + 10)),
                            img.getpixel((g["x1"] - 1, g["top"] + 10)),
                            "描边与填充应当是两种颜色")

    def test_dim_runs_before_the_box(self):
        self.cfg["video.bg_dim"] = 0.3
        self.cfg["subtitle.bg_alpha"] = 128
        self.cfg["subtitle.outline"] = 2
        img = Image.open(self._bake((0, 0, 0))).convert("RGB")
        g = self._box()
        got = img.getpixel(((g["x1"] + g["x2"]) // 2, (g["top"] + g["bottom"]) // 2))
        # 框底是有限范围的纯黑 16，按 128/255 不透明度盖在纯黑背景上 → 8。
        # 若顺序反了（先画框再压暗），这 8 会被再压一层变成 6。
        self.assertLessEqual(abs(got[0] - 8), 1, "框被压暗了两遍：%s" % (got,))

    def test_size_mismatch_raises(self):
        src = os.path.join(self.tmp, "small.png")
        Image.new("RGB", (100, 100), (0, 0, 0)).save(src)
        with self.assertRaises(AF.AssetError):
            AF.bake_static_layers(src, os.path.join(self.tmp, "o.png"), self.cfg,
                                  self.w, self.h)


class TestTvRangeColor(unittest.TestCase):
    """框底与描边的颜色走的是 ffmpeg 的**有限范围**口径，不是 0/255。

    ffmpeg 的 `ass` 滤镜逐通道按 `Y = 16 + 219·c/255` 映射字幕颜色。烘焙走 Pillow、
    直接写 8 bit RGB，不补这一步就会因为「换谁来画」而换一个颜色——框底比现在深 8、
    描边比现在亮 20。这组数是从实渲画面反推出来的（`bake_static_check.py` 的
    `--calibrate` 模式 7 组颜色 + 4 档不透明度逐个对过），钉在这里防止被"顺手改回去"。
    """

    def test_endpoints_and_primaries(self):
        # 黑→16、白→235；纯色只让对应通道进 235，另两个进 16
        for src, want in (((0, 0, 0), (16, 16, 16)),
                          ((255, 255, 255), (235, 235, 235)),
                          ((255, 0, 0), (235, 16, 16)),
                          ((0, 255, 0), (16, 235, 16)),
                          ((0, 0, 255), (16, 16, 235))):
            with self.subTest(src=src):
                self.assertEqual(SE.tv_range_color(src), want)

    def test_mid_gray_is_mapped_and_out_of_range_stays_in_8_bit(self):
        self.assertEqual(SE.tv_range_color((128, 128, 128)), (126, 126, 126))
        # 越界值不是合法颜色（真实调用只传 0/255 这类常量），但结果必须仍落在
        # 8 bit 里，不能外溢成一个能让 Pillow 报错或截断的值。
        for c in SE.tv_range_color((-5, 300, 0)):
            self.assertTrue(0 <= c <= 255, "越界输入的结果溢出了：%d" % c)

    def test_box_paint_uses_the_tv_range_colors(self):
        g = SE.frame_geometry({}, 1920, 1080, "", 1)
        p = SE.frame_box_paint(g, 128, 2)
        self.assertEqual(p["fill"], (16, 16, 16))
        self.assertEqual(p["border"], (235, 235, 235))
        self.assertEqual(p["rect"], (g["x1"], g["top"], g["x2"], g["bottom"]))
        self.assertEqual(p["out"], 2)


class TestWaveBandsDoNotIntersect(unittest.TestCase):
    """声波包络不相交是硬约束，不是观感调参。

    原先三条波的间距按像素写死（36 / 72），而振幅是 22 / 17 / 12：
    36 < 22+17，相邻两条的包络从第一帧就重叠；频率又只差千分之三，
    产生拍频、相位周期性重合，三条线看上去就绞成一团。
    修法是把留白变成结构条件——相邻中线间距取「振幅之和 + 留白」，
    于是净空恒为正，与相位、频率、画幅都无关。

    这一条必须由测试守着：留白被谁改小、振幅被谁调大，
    界面上只会表现为「波浪线又缠上了」，没有任何一处会报错。
    """

    SIZES = ((1920, 1080), (1080, 1920), (3840, 2160), (1280, 720),
             (1080, 1080), (2560, 1080))
    TOPS = (120.0, 260.0, 480.0, 760.0)

    def _all_layouts(self):
        for size in self.SIZES:
            for top in self.TOPS:
                bands = AF.wave_layout(size, top, min(size) / 1080.0)
                yield size, top, bands

    def test_layout_produces_three_bands_when_there_is_room(self):
        """有空间就要画满三条，不能悄悄少画。"""
        for size, top, bands in self._all_layouts():
            if bands:
                with self.subTest(size=size, top=top):
                    self.assertEqual(len(bands), len(AF.WAVE_BANDS))

    def test_clearance_is_positive(self):
        """相邻两条的包络净空必须为正，且恰好等于留白量。"""
        for size, top, bands in self._all_layouts():
            for a, b in zip(bands, bands[1:]):
                clear = (b["cy"] - b["amp"]) - (a["cy"] + a["amp"])
                with self.subTest(size=size, top=top):
                    self.assertGreater(clear, 0.0)
                    self.assertAlmostEqual(
                        clear, AF.WAVE_GAP * (bands[0]["amp"] / 1.00),
                        places=6,
                        msg="净空应恒等于 留白×单位振幅，与相位无关")

    def test_sampled_curves_never_cross(self):
        """按实际绘制点逐像素比对：任意 x 上都不出现上下关系反转。

        这是最强的一条：前一条断言算的是公式，这一条量的是真正落笔的折线。
        """
        for size, top, bands in self._all_layouts():
            for a, b in zip(bands, bands[1:]):
                ya = dict(AF.wave_points(a, step=1.0))
                yb = dict(AF.wave_points(b, step=1.0))
                for x in sorted(set(ya) & set(yb)):
                    with self.subTest(size=size, top=top, x=x):
                        self.assertGreater(yb[x] - ya[x], 0.0)

    def test_bands_stay_inside_the_canvas(self):
        """波带整体不得越出画布，也不得压到画布上沿以外。"""
        for size, top, bands in self._all_layouts():
            if not bands:
                continue
            w, h = size
            first, last = bands[0], bands[-1]
            with self.subTest(size=size, top=top):
                self.assertGreaterEqual(first["cy"] - first["amp"], 0.0)
                self.assertLessEqual(last["cy"] + last["amp"], h)
                self.assertGreaterEqual(first["x0"], 0.0)
                self.assertLessEqual(last["x1"], w)

    def test_period_weights_are_not_integer_ratios(self):
        """周期权重取不成整数比，避免三条波长期同相。

        同相不会让线相交（净空为正），但会让三条波在若干 x 上同时到顶，
        整片看起来是「一坨」而不是「三股」。
        """
        cycles = [c for _r, c, _a in AF.WAVE_BANDS]
        for i in range(len(cycles)):
            for j in range(i + 1, len(cycles)):
                ratio = cycles[j] / cycles[i]
                with self.subTest(ratio=ratio):
                    self.assertGreater(abs(ratio - round(ratio)), 0.05)

    def test_wave_max_amp_is_respected(self):
        """单位振幅不得超过上限，短文案也不能把波拉得过粗。"""
        for size, top, _bands in self._all_layouts():
            scale = min(size) / 1080.0
            bands = AF.wave_layout(size, top, scale)
            for band in bands:
                with self.subTest(size=size, top=top):
                    self.assertLessEqual(band["amp"], AF.WAVE_MAX_AMP * scale)

    def test_no_room_means_no_wave(self):
        """可用高度不够时不画波，而不是画出负高度或压到文字上。"""
        size = (1920, 1080)
        # 触底：可用高度 = 画布高×wave_max − wave_top，取到 4×scale 以下。
        top = 1080 * AF.LAYOUT["wave_max"] - 1.0
        self.assertEqual(AF.wave_layout(size, top, 1.0), [])


class TestFrameFont(unittest.TestCase):
    """画面字体是可选项，不是写死的。

    背景与封面从前一律取「本机第一款可用字体」——配置里没有这一项，人看得见
    画面上那行字，却选不了它用哪款。这一条钉住换字体真的换出不同的字来：
    只看配置项有没有被读到（字符串出现）是不够的，得看像素变没变。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pm_frame_font_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _shoot(self, family, tag):
        out = os.path.join(self.dir, tag)
        os.makedirs(out, exist_ok=True)
        r = AF.make_covers("book", LINES, out, prefix="1", font_family=family)
        with open(r["3x4"], "rb") as f:
            return f.read()

    def test_two_fonts_give_two_pictures(self):
        fonts = AF.list_fonts()
        if len(fonts) < 2:
            self.skipTest("本机只有一款可用字体，比不出差别")
        try:
            a = self._shoot(fonts[0]["family"], "a")
            b = self._shoot(fonts[1]["family"], "b")
        except AF.AssetError as e:
            self.skipTest("本机字体缺中文字形，比不出来：%s" % e)
        self.assertNotEqual(a, b, "换字体后封面一字未变——这一项没接上渲染")

    def test_blank_falls_back_to_the_same_font(self):
        # 留空即自动挑一款。它必须与「显式指定自动挑中的那款」产出同一张图，
        # 否则「自动」就悄悄成了另一套规矩。
        picked = AF.resolve_font()
        try:
            blank = self._shoot("", "blank")
            explicit = self._shoot(picked["family"], "explicit")
        except AF.AssetError as e:
            self.skipTest("本机字体缺中文字形，比不出来：%s" % e)
        self.assertEqual(blank, explicit)

    def test_unknown_font_is_refused(self):
        # 报错并列出候选，不静默换一款顶上去——悄悄换掉的话，人以为选上了。
        with self.assertRaises(AF.AssetError):
            self._shoot("根本没有这款字体", "bad")


if __name__ == "__main__":
    unittest.main(verbosity=2)
