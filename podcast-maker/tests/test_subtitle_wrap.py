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

"""字幕版式测试：断词率必须为 0，两档共用同一个框、只差一个轴。

两档：`lyric` 纵轴（折行进框、换句上滚）、`single` 单行滚动（不折行、装不下就
滚过框口）。它们共用 `frame_geometry` 的矩形、同一套 Style、同一套像素宽度口径
（`CHAR_W_*`）——所以这一组里凡是写「两档一致」的地方，都是拿两侧的输出直接比。
"""

import hashlib
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import subtitle_engine as S  # noqa: E402

# 覆盖三类风险：纯中文、含西文 token、含标点密集
CASES = [
    ("这个问题的答案其实很简单，就是不该停的地方不能停，否则读者会读错意思。", 16),
    ("我们把 GPT-4 和 Claude-3.5 放在一起比较它们的表现。", 18),
    ("确定性的部分用代码解决，不确定性的部分才交给模型填空。", 14),
    ("空着的地方要敢承认填不了，而不是编一个看起来对的答案。", 12),
    ("一条链加两头：链是主体，模型只做两端。", 10),
    ("模型擅长的是另一头：理解人的意图，把意图翻译成结构。", 20),
]

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\.\-_/]*[A-Za-z0-9]|[A-Za-z0-9]")

MOVE_RE = re.compile(r"\\move\((-?\d+),(-?\d+),(-?\d+),(-?\d+),(\d+),(\d+)\)")
POS_RE = re.compile(r"\\pos\((-?\d+),(-?\d+)\)")


def dialogues(ass):
    """把 ASS 的 Dialogue 拆成字段（两档共用这一份解析）。

    `box` 标记这条是不是垫底的整块框（`\\p1` 绘图事件）——它不参与「文字行数 /
    图层号」那几条断言，必须能一眼摘出来。横滚档的 `\\move` 终点坐标**可能是负的**
    （文字中心滑到画面左边外），所以坐标一律按有符号解析。
    """
    out = []
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        f = line.split(",", 9)
        raw = f[9]
        head, text = (raw.split("}", 1) + [""])[:2] if raw.startswith("{") else ("", raw)
        head = head + "}" if head else ""
        rec = {"layer": f[0].split()[1], "start": f[1], "end": f[2],
               "style": f[3], "head": head, "text": text,
               "box": "\\p1" in head}
        mv = MOVE_RE.search(head)
        ps = POS_RE.search(head)
        if mv:
            rec.update(x_from=int(mv.group(1)), y_from=int(mv.group(2)),
                       x_to=int(mv.group(3)), y_to=int(mv.group(4)),
                       t1=int(mv.group(5)), t2=int(mv.group(6)))
        elif ps:
            # 静止的一句（装得下、\pos 居中）：没有滚动期，两个时间都记 0，
            # 这样「这条动没动」就只看 t1 < t2。
            rec.update(x_from=int(ps.group(1)), y_from=int(ps.group(2)),
                       x_to=int(ps.group(1)), y_to=int(ps.group(2)),
                       t1=0, t2=0)
        out.append(rec)
    return out


def boxes(ass):
    """垫底的整块框（`\\p1` 绘图事件）。"""
    return [d for d in dialogues(ass) if d["box"]]


def captions(ass):
    """文字事件（排除垫底的框）。"""
    return [d for d in dialogues(ass) if not d["box"]]


def seconds(stamp):
    """ASS 时间戳 "0:00:27.10" → 秒。"""
    h, m, s = stamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


class TestTimeFormat(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual(S.fmt_srt_time(3725.5), "01:02:05,500")
        self.assertEqual(S.fmt_srt_time(0), "00:00:00,000")

    def test_ass_time(self):
        self.assertEqual(S.fmt_ass_time(3725.5), "1:02:05.50")
        self.assertEqual(S.fmt_ass_time(0), "0:00:00.00")

    def test_negative_clamped(self):
        self.assertEqual(S.fmt_srt_time(-1.0), "00:00:00,000")


class TestTimeline(unittest.TestCase):
    def test_offset_shifts_everything(self):
        script = [{"speaker": "A", "text": "一"}, {"speaker": "A", "text": "二"}]
        t = S.probe_timings(script, [2.0, 3.0], 0.5, offset=4.0)
        self.assertEqual(t[0]["start"], 4.0)
        self.assertEqual(t[0]["end"], 6.0)
        self.assertEqual(t[1]["start"], 6.5)
        self.assertEqual(t[1]["end"], 9.5)

    def test_rescale_within_tolerance_is_noop(self):
        t = S.probe_timings([{"text": "一"}], [10.0], 0.0)
        k, changed = S.rescale_timings(t, 10.0, 10.2)
        self.assertFalse(changed)
        self.assertEqual(k, 1.0)
        self.assertEqual(t[0]["end"], 10.0)

    def test_rescale_stretches_to_measured(self):
        script = [{"text": "一"}, {"text": "二"}]
        t = S.probe_timings(script, [10.0, 10.0], 0.0)
        k, changed = S.rescale_timings(t, 20.0, 22.0)
        self.assertTrue(changed)
        self.assertAlmostEqual(k, 1.1, places=4)
        self.assertAlmostEqual(t[-1]["end"], 22.0, places=3)

    def test_rescale_guards_bad_input(self):
        t = S.probe_timings([{"text": "一"}], [10.0], 0.0)
        self.assertEqual(S.rescale_timings(t, 0.0, 10.0), (1.0, False))
        self.assertEqual(S.rescale_timings([], 10.0, 10.0), (1.0, False))


class TestLrc(unittest.TestCase):
    """LRC 与 SRT 同一份时间轴：只取起始时间、两位百分秒、一行一条。

    音频平台（喜马拉雅一类）的字幕位走歌词渲染，只认 LRC——这份产物就是给
    它的。所以下面盯的都是「平台认不认」的硬规格，不是好看不好看。
    """

    SCRIPT = [{"text": "第一句。", "speaker": "A"},
              {"text": "第二句。", "speaker": "B"}]
    TIMINGS = [{"start": 0.0, "end": 3.0}, {"start": 62.72, "end": 70.0}]

    def test_time_is_hundredths_not_milliseconds(self):
        """秒后两位百分秒。写成三位毫秒（SRT 那种）平台不认。"""
        self.assertEqual(S.fmt_lrc_time(62.72), "01:02.72")
        self.assertEqual(S.fmt_lrc_time(0), "00:00.00")

    def test_time_carries_instead_of_truncating(self):
        """59.999 必须先进位成 01:00.00，不能拆成 00:59.99。"""
        self.assertEqual(S.fmt_lrc_time(59.999), "01:00.00")

    def test_minutes_may_exceed_59(self):
        """一小时以上照常两位分钟，不做进时——LRC 没有小时位。"""
        self.assertEqual(S.fmt_lrc_time(3600), "60:00.00")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(S.fmt_lrc_time(-3), "00:00.00")

    def test_one_line_per_sentence_with_start_only(self):
        out = S.build_lrc(self.SCRIPT, {}, self.TIMINGS)
        self.assertEqual(out.split("\n"),
                         ["[00:00.00]第一句。", "[01:02.72]第二句。"])
        self.assertNotIn("-->", out)

    def test_keeps_full_sentence_instead_of_wrapping(self):
        """LRC 一行就是一条：照画面宽度断开会切出「半句配一个时间戳」。"""
        long_text = "这句话比画面字幕一行放得下的字数长得多，但歌词位不需要断行。" * 3
        out = S.build_lrc([{"text": long_text}], {}, [{"start": 1.0, "end": 2.0}])
        self.assertEqual(len(out.split("\n")), 1)
        self.assertTrue(out.endswith(long_text))

    def test_text_newline_does_not_split_entry(self):
        """句内换行会把一条劈成两条，第二条连时间戳都没有。"""
        out = S.build_lrc([{"text": "上行\n下行"}], {}, [{"start": 0.0, "end": 1.0}])
        self.assertEqual(out, "[00:00.00]上行 下行")

    def test_missing_timings_do_not_drop_lines(self):
        """句数与时间轴长度不必相等，缺的按时长 0 处理，一行都不能少。"""
        out = S.build_lrc(self.SCRIPT, {}, [{"start": 1.0, "end": 2.0}])
        self.assertEqual(len(out.split("\n")), 2)
        self.assertTrue(out.split("\n")[1].startswith("[00:00.00]"))

    def test_name_switch_is_one_switch(self):
        """名字只有一条判据：角色名称开关。版式与提示档都不管它。"""
        base = {"tts.name_a": "小美", "tts.name_b": "大美"}
        self.assertEqual(S.build_lrc(self.SCRIPT, base, self.TIMINGS).split("\n")[0],
                         "[00:00.00]第一句。")

        on = dict(base, **{"speaker_indicator.name_shown": True})
        self.assertEqual(S.build_lrc(self.SCRIPT, on, self.TIMINGS).split("\n"),
                         ["[00:00.00]小美：第一句。", "[01:02.72]大美：第二句。"])

        # 换版式、换提示档都不该动摇它——从前这两处各自兼职决定名字，
        # 于是选「侧栏色块」「自备立绘」反而没了名字。
        for preset in ("single", "lyric"):
            with self.subTest(preset=preset):
                self.assertTrue(S.speaker_name_shown(dict(on, **{"subtitle.preset": preset})))
        for mode in ("none", "style", "block", "portrait"):
            with self.subTest(mode=mode):
                cfg = dict(on, **{"speaker_indicator.mode": mode})
                self.assertTrue(S.speaker_name_shown(cfg))
                self.assertEqual(
                    S.build_lrc(self.SCRIPT, cfg, self.TIMINGS).split("\n")[0],
                    "[00:00.00]小美：第一句。")

        off = dict(base, **{"speaker_indicator.name_shown": False,
                            "speaker_indicator.mode": "style"})
        self.assertFalse(S.speaker_name_shown(off))
        self.assertEqual(S.build_lrc(self.SCRIPT, off, self.TIMINGS).split("\n")[0],
                         "[00:00.00]第一句。")

    def test_ass_and_lrc_read_one_judgement(self):
        """两处各写一遍判据就迟早各说各话——这条钉住它只有一份。"""
        self.assertTrue(S.speaker_name_shown({"speaker_indicator.name_shown": True}))
        self.assertFalse(S.speaker_name_shown({"speaker_indicator.name_shown": False}))
        # 老配置里还没有这个键（升级前落盘的项目）按旧判据回退：升一次级不该
        # 让名字突然变样。这几条回退判据留着，等老项目都重存过一遍再说。
        self.assertTrue(S.speaker_name_shown({"subtitle.preset": "dual_named"}))
        self.assertTrue(S.speaker_name_shown({"speaker_indicator.mode": "style"}))
        self.assertFalse(S.speaker_name_shown({}))
        self.assertFalse(S.speaker_name_shown({"subtitle.preset": "single"}))


class TestBalancedWrap(unittest.TestCase):
    """歌词档的折行：行数自己算、每行都不超容量。

    先定行数再均分（40 字一句在 33 字/行下得到 21+19；贪心填满会得到 33+7）。
    """

    LONG = "既然产物种类繁多，统一归口预设文件夹是为了让后续自动化清理与备份能精准定位目标。"
    TEXTS = [LONG, LONG * 2, "中" * 96, "我们把 GPT-4 和 Claude-3.5 放在一起比较它们的表现，"
                               "看看谁在长句里的断点更稳一点。"]

    def test_line_count_is_a_lower_bound_not_a_target(self):
        """行数是**下界**：容量算出来的最少行数一定要够，但允许更多一行。

        「合法」是硬约束，「均衡」只是偏好，行数服从前两者。窗口里找不到合法
        断点时宁可多占一行（见 test_token_covering_the_window_never_gets_cut），
        所以这里判 ≥——判 = 会把「不许切词」这条硬约束判死。
        """
        for text in self.TEXTS:
            for mc in (12, 22, 33):
                with self.subTest(text=text[:8], mc=mc):
                    rows = S.wrap_balanced(text, mc)
                    self.assertGreaterEqual(len(rows), -(-len(text) // mc))

    def test_count_is_exactly_the_lower_bound_when_every_cut_is_legal(self):
        """切点处处合法时不留余量：纯中文不触发任何禁则，行数就是下界。"""
        for n in (24, 96, 101):
            text = "中" * n
            with self.subTest(n=n):
                self.assertEqual(len(S.wrap_balanced(text, 12)), -(-n // 12))

    # 两处实测样本：2c / 2d 两期就是死在这上面。这类退化只在「被保护的西文词
    # 恰好横跨整个均衡窗口」时出现，随手编的短句复现不了，所以把现场的句子搬进来。
    TOKEN_COVER_CASES = [
        ("上期《零依赖拆解与渐进加载的决策引擎》聊的是以 semantic-split 为核心，"
         "阐释 Pipeline A/B/C 递进匹配、懒加载门禁与 0.6 阈值复用机制，"
         "实现低开销任务分解。", 33),
        ("我们把 GPT-4 与 RAG Assistant 放在一起比较，"
         "看看谁是靠 semantic-split 做任务分解的。", 20),
    ]

    def test_token_covering_the_window_never_gets_cut(self):
        """受保护的词横跨整个均衡窗口时，宁可加一行，也不切在词中间。

        从前的 `_nearest_legal_break` 在窗口里找不到合法点就 `return hi` 兜底
        硬切，切出 `…聊的是以 semantic-` / `split`、`…靠 semantic-sp` / `lit`——
        一个一百多行的报告里只有这一处违规，整期却被判不过。
        """
        for text, mc in self.TOKEN_COVER_CASES:
            rows = S.wrap_balanced(text, mc)
            at = 0
            for row in rows[:-1]:
                at += len(row)
                with self.subTest(mc=mc, at=at):
                    self.assertFalse(S._in_latin_token(text, at),
                                     "切在西文词中间：%r" % (rows,))
            with self.subTest(mc=mc):
                self.assertEqual(S.count_word_breaks(text, mc, "y"), 0)
                self.assertEqual("".join(rows), text, "切行不许丢字")
                for row in rows:
                    self.assertLessEqual(len(row), mc, "行超容量：%r" % row)

    def test_every_line_fits_the_capacity(self):
        """含末行：歌词框里一行超出框宽会顶到框边，比多一行难看。"""
        for text in self.TEXTS:
            for mc in (12, 22, 33):
                for row in S.wrap_balanced(text, mc):
                    self.assertTrue(row)
                    self.assertLessEqual(len(row), mc, "行超容量：%r" % row)

    def test_nothing_is_dropped(self):
        for text in self.TEXTS:
            for mc in (12, 22, 33):
                self.assertEqual("".join(S.wrap_balanced(text, mc)), text)

    def test_no_violations(self):
        """切点仍走四级判定：均分只换了从哪儿开始找，找出来的位置照样合法。"""
        for text in self.TEXTS:
            for mc in (12, 22, 33):
                with self.subTest(mc=mc):
                    self.assertEqual(S.count_word_breaks(text, mc, "y"), 0)

    def test_short_text_untouched(self):
        self.assertEqual(S.wrap_balanced("短句。", 20), ["短句。"])
        self.assertEqual(S.wrap_balanced("", 20), [""])

    def test_stats_follow_the_balanced_cuts(self):
        """统计必须跟实际折行走同一套切点，否则报的是另一种折法的账。"""
        st = S.wrap_stats([{"text": self.LONG}], 33, "y")
        self.assertEqual((st["wraps"], st["violations"], st["rate"]), (1, 0, 0.0))

    def test_the_roll_rail_has_no_wrapping_at_all(self):
        """横滚档不折行：一句一行滚过去，一个断点都不存在。

        在这一档报「折行 N 处」是假账——那些句子一行都没折。从前这里靠
        「行数上限 = 1 就跳过」漏出统计，那是个巧合。
        """
        st = S.wrap_stats([{"text": self.LONG}, {"text": self.LONG * 2}], 33, "x")
        self.assertEqual(st, {"wraps": 0, "violations": 0, "rate": 0.0, "samples": []})
        self.assertEqual(S.count_word_breaks(self.LONG, 33, "x"), 0)


class TestFrameGeometry(unittest.TestCase):
    """框的几何只有一处出处（`frame_geometry`），两档共用。

    这些数是预览与成片的共同分母：预览照同一次算式画（web_ui.paintSubPreview），
    差一点就是「所见非所得」。
    """

    BASE = {"subtitle.font_size": 52, "subtitle.font_size_vertical": 40,
            "subtitle.margin_lr": 90, "subtitle.margin_v": 90,
            "subtitle.margin_v_vertical": 220}

    def test_landscape_numbers(self):
        g = S.frame_geometry(self.BASE, 1920, 1080, "", 8)
        self.assertEqual((g["x1"], g["x2"]), (90, 1830))
        self.assertEqual(g["pitch"], round(52 * 1.35))       # 70
        self.assertEqual(g["bottom"], 990)
        self.assertEqual(g["h"], 8 * 70)
        self.assertEqual(g["top"], 990 - 560)
        self.assertEqual(g["anchor"], 990 - 280)
        self.assertEqual(g["cx"], 960)

    def test_vertical_uses_its_own_margin_and_size(self):
        g = S.frame_geometry(self.BASE, 1080, 1920, "_v", 8)
        self.assertEqual((g["x1"], g["x2"]), (90, 990))
        self.assertEqual(g["pitch"], round(40 * 1.35))       # 54
        self.assertEqual(g["bottom"], 1700)
        self.assertEqual(g["h"], 8 * 54)

    def test_height_is_rows_times_pitch(self):
        for rows in (1, 3, 8, 24):
            with self.subTest(rows=rows):
                g = S.frame_geometry(self.BASE, 1920, 1080, "", rows)
                self.assertEqual(g["h"], rows * g["pitch"])

    def test_pixel_width_is_calibrated_not_the_font_size(self):
        """字宽是实渲标定的系数，不是「一个汉字管一个字号」。
        
        按 1.0 倍估会把字宽算多三成，横滚的终点就跑过框（预览也跟着错）。
        """
        self.assertEqual((S.CHAR_W_HAN, S.CHAR_W_FULL, S.CHAR_W_HALF),
                         (0.751, 0.820, 0.438))
        self.assertAlmostEqual(S.text_px_width("中", 100), 75.1, places=4)
        self.assertAlmostEqual(S.text_px_width("，", 100), 82.0, places=4)
        self.assertAlmostEqual(S.text_px_width("a", 100), 43.8, places=4)
        self.assertAlmostEqual(S.text_px_width("中a，", 100), 75.1 + 43.8 + 82.0, places=4)

    def test_the_width_rule_matches_a_real_render(self):
        """验收口径：算式算出的宽**不得小于**实渲量出的宽，偏差 ≤ 2%。

        实渲那个数是标定阶段用 libass 量的：111 汉字 + 9 全角标点 + 5 半角、
        字号 52，墨迹宽 4790px（系数本身由同一段文本在 12/16/20/24 四个字号上
        四点定出，这是拿 52 号做的交叉验证）。算式给 4832.4px，偏宽 0.9%。

        方向是安全的：终点按「文字宽」反推，算宽了只会让文字早一点点停住
        （框里仍留得住结尾），算窄了才会跑过框、把结尾裁掉。
        """
        sample = "中" * 111 + "，" * 9 + "a" * 5
        self.assertEqual(len(sample), 125, "样本被改动了，这一条就失去意义")
        calc = S.text_px_width(sample, 52)
        measured = 4790.0
        self.assertGreaterEqual(calc, measured,
                                "算式比实渲窄：横滚终点会跑过框，结尾被裁")
        self.assertLessEqual(abs(calc - measured) / measured, 0.02,
                             "算式与实渲差过 2%%，字宽系数该重标了")

    def test_unrecognised_preset_falls_back_to_the_default(self):
        """老项目清单里还存着已删除的 `dual`：认不出就按默认档走，不报错。"""
        for 老值 in ("dual", "dual_named", "", None):
            with self.subTest(preset=老值):
                name, opt = S.preset_of({"subtitle.preset": 老值})
                self.assertEqual(name, "lyric")
                self.assertEqual(opt["axis"], "y")


class TestColourWritings(unittest.TestCase):
    """配色有两种写法，点位各定一种：`subtitle.color_a` 收 ASS 的 `&HAABBGGRR`，
    `subtitle.highlight_color` 收 CSS 的 `#RRGGBB`。

    后端两种都认（`_ass_color`），前端预览要把 ASS **倒回** CSS 才画得出来——倒错
    方向就是 BGR 当 RGB 用（红蓝互换），不过滤则是非法值被浏览器丢掉、字变黑。
    所以这一条契约两边一起钉：这边钉后端收两种写法，预览那一边钉转换在。
    """

    FONT = "HarmonyOS Sans SC"

    def _ass(self, **over):
        cfg = {"subtitle.preset": "single", "speaker_indicator.name_shown": False,
               "speaker_indicator.mode": "style", "subtitle.font_family": self.FONT}
        cfg.update(over)
        return S.build_ass([{"speaker": "A", "text": "一句。"}], cfg,
                           [{"start": 0.0, "end": 2.0}], 1920, 1080, "")[0]

    def test_the_speaker_colour_takes_either_writing(self):
        for value in ("&H8AD9FF", "#FFD98A"):          # 同一个颜色的两种写法
            with self.subTest(value=value):
                ass = self._ass(**{"subtitle.color_a": value})
                self.assertIn("Style: SpeakerA,%s,52,&H8AD9FF" % self.FONT, ass,
                              "A 角字幕色没认出来（%s）" % value)

    def test_the_highlight_colour_takes_either_writing(self):
        for value in ("#FFD98A", "&H8AD9FF"):
            with self.subTest(value=value):
                ass = self._ass(**{"subtitle.highlight": True,
                                   "subtitle.highlight_color": value})
                self.assertIn("Style: FrameCur,%s,52,&H8AD9FF" % self.FONT, ass,
                              "高亮配色没认出来（%s）" % value)


class TestStripPreset(unittest.TestCase):
    """单行滚动档：一句一行、不折字，装不下就在框里滚过框口。"""

    LONG = ("上期《骨架叙事切分与介质边界的确定性约束》聊的是聚焦成书的结构性排版，"
            "揭示注册列表驱动的四部叙事弧线、页面物理边界（断页/字体/缩放）与"
            "元数据口径的刚性同步机制。")
    SCRIPT = [
        {"speaker": "A", "text": "第一句很短。"},
        {"speaker": "B", "text": LONG},
        {"speaker": "A", "text": "第三句。"},
    ]
    TIMINGS = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 29.1},
               {"start": 29.1, "end": 31.0}]
    BASE = {"subtitle.preset": "single", "speaker_indicator.name_shown": False,
            "speaker_indicator.mode": "style", "subtitle.font_family": "HarmonyOS Sans SC"}

    def _ass(self, script=None, timings=None, suffix="", size=(1920, 1080), **over):
        cfg = dict(self.BASE, **over)
        text, mc, axis = S.build_ass(script or self.SCRIPT, cfg,
                                     timings or self.TIMINGS,
                                     size[0], size[1], suffix)
        return text, mc, axis

    def test_preset_reports_the_horizontal_axis(self):
        _, _, axis = self._ass()
        self.assertEqual(axis, "x", "单行档没走到横滚那一支")

    def test_one_dialogue_per_sentence_and_no_wrapping(self):
        """不折行：每句一条 Dialogue，正文里一个 \\N 都没有。

        从前的单行档把整句交给渲染器自动折行——中文没有词间空格，libass 把整句
        当成一个超长 token（实测它根本不折），超出画幅的那截被直接裁掉。
        """
        ass, _, _ = self._ass()
        rows = captions(ass)
        self.assertEqual(len(rows), len(self.SCRIPT))
        for d in rows:
            self.assertNotIn("\\N", d["text"])

    def test_nothing_is_dropped_from_the_text(self):
        """整句原样进画面：一个字不少（这正是横滚要保住的东西）。"""
        ass, _, _ = self._ass()
        for d, item in zip(captions(ass), self.SCRIPT):
            self.assertEqual(d["text"], item["text"])

    def test_the_box_is_one_construct_for_both_presets(self):
        """两档的框是同一个构造：左右、底边、填充、描边完全相同。

        只有「高」随各自的行数（单行滚动 1 行、歌词 `frame_rows` 行）——它由
        `frame_geometry` 同一行算式算出来。框已改由烘焙画进背景图，所以这里断言的是
        **画框那份参数**（`frame_box_paint`），不再是 ASS 里的一条事件。
        """
        cfg = dict(self.BASE, **{"subtitle.bg_alpha": 128, "subtitle.outline": 2})
        px = S.frame_box_paint(S.frame_of(cfg, 1920, 1080, ""), 128, 2)
        cfg_y = dict(cfg, **{"subtitle.preset": "lyric"})
        gy = S.frame_of(cfg_y, 1920, 1080, "")
        py = S.frame_box_paint(gy, 128, 2)
        gx = S.frame_of(cfg, 1920, 1080, "")
        self.assertEqual(px["rect"][:2], (gx["x1"], gx["top"]),
                         "框左上角 ≠ frame_geometry")
        self.assertEqual(py["rect"][:2], (gy["x1"], gy["top"]))
        # 左右与底边同源：同一个 margin_lr / margin_v
        self.assertEqual(px["rect"][0], py["rect"][0])
        self.assertEqual(px["rect"][2], py["rect"][2])
        self.assertEqual(px["rect"][3], py["rect"][3])
        # 填充、描边、描边宽都是同一套参数（描边只往外扩整宽）
        for key in ("fill", "border", "width", "out"):
            with self.subTest(key=key):
                self.assertEqual(px[key], py[key])
        self.assertEqual(px["out"], px["width"])
        # 两档的框宽相同、高不同（同一条 `frame_geometry` 算式，只有行数不一样）
        self.assertEqual(px["rect"][2] - px["rect"][0], py["rect"][2] - py["rect"][0])
        self.assertLess(px["rect"][3] - px["rect"][1], py["rect"][3] - py["rect"][1])
        self.assertEqual(px["rect"][3] - px["rect"][1], gx["h"])
        self.assertEqual(py["rect"][3] - py["rect"][1], gy["h"])
        # 两档的 ASS 里都不该再有框事件——框只有烘焙一处画
        for preset in ("single", "lyric"):
            with self.subTest(preset=preset):
                ass = self._ass(**{"subtitle.preset": preset})[0]
                self.assertEqual(boxes(ass), [], "ASS 里还有框事件：%s" % preset)

    def test_short_sentence_stays_put_in_the_middle(self):
        """装得下就静止居中：\\pos 落在框中心，不滚。"""
        ass, _, _ = self._ass()
        g = S.frame_geometry(self.BASE, 1920, 1080, "", 1)
        cy = int(round(g["anchor"]))
        first = captions(ass)[0]
        self.assertEqual((first["x_from"], first["y_to"]), (g["cx"], cy))
        self.assertNotIn("\\move(", first["head"])

    def test_long_sentence_rolls_between_the_two_edges(self):
        """装不下就滚：起点左缘贴框左、终点右缘贴框右，位移 = 文字宽 − 框宽。

        三段式里的「句首静止」由 t1 > 0 表达——t1 = 0 就是「一上来就动」，
        观众还没读到开头字就滑走了。
        """
        ass, _, _ = self._ass()
        g = S.frame_geometry(self.BASE, 1920, 1080, "", 1)
        cy = int(round(g["anchor"]))
        d = captions(ass)[1]
        w_t = S.text_px_width(self.LONG, g["size"])
        self.assertGreater(w_t, g["x2"] - g["x1"], "这条样本本该溢出，测试没验到东西")
        self.assertEqual((d["x_from"], d["y_to"], d["x_to"]),
                         (int(round(g["x1"] + w_t / 2.0)), cy,
                          int(round(g["x2"] - w_t / 2.0))))
        self.assertGreater(d["t1"], 0, "句首静止期为 0：开头一个字都留不住")
        self.assertLessEqual(d["t2"], int(round(27.1 * 1000)) + 1)
        self.assertEqual(d["t2"], int(round((29.1 - 2.0) * 1000)))

    #: 终点允许晚于句末的余量。ASS 时间戳只有 10ms 一格（`fmt_ass_time` 是
    #: `%05.2f`），而 `\move` 的终点是 `round(span × 1000)` 毫秒，事件起止又各自
    #: 落格一次——两端各最多差 5ms，合起来不到一格半，取 20ms 封顶。
    #:
    #: 所以「终点 ≤ 句末」这条不变量只能断言到 20ms 以内：差额是时间戳取整的
    #: 零头，**不是滚不完**。画面上的表现是最后十几毫秒的位移被掐掉，不足一个
    #: 像素，肉眼不可见。拿 `end <= span` 去卡会误报（真渲实测差 3ms）。
    TAIL_TOLERANCE = 0.020

    def test_the_roll_always_finishes_before_the_sentence_ends(self):
        """滚得完——写进测试的硬断言，不靠眼看。

        位移 = 文字宽 − 框宽 恒小于文字总宽；速度 = 文字宽 ÷ 句时长，于是
        滚动时长 = 句时长 × 位移 ÷ 文字宽 **恒小于句时长**。这条在真时间轴上
        逐句验一遍（长短句、两种画幅都覆盖）。

        静止的那几句（装得下、`\\pos` 居中）本来就没有滚动期，跳过——不动的
        话 t1 == t2 == 0，拿「t2 ≤ 时长」去套它没有意义。句时长不足 0.3 秒的
        也跳过：那会顶到 `_strip_events` 的时长下限（防零除的兜底），推不出
        「滚得完」的结论。

        终点不精确等于句末，只保证落在句末的**一格时间戳**之内——理由见
        `TAIL_TOLERANCE`。
        """
        for size, suffix in (((1920, 1080), ""), ((1080, 1920), "_v")):
            ass, _, _ = self._ass(size=size, suffix=suffix)
            moving = [d for d in captions(ass) if "\\move(" in d["head"]]
            self.assertTrue(moving, "这份样本一句都没滚起来，测试没验到东西")
            for d in moving:
                span = seconds(d["end"]) - seconds(d["start"])
                if span < 0.3:
                    continue
                with self.subTest(size=size, start=d["start"]):
                    self.assertLessEqual(d["t2"] / 1000.0, span + self.TAIL_TOLERANCE,
                                         "终点冲出去了：句长 %.2fs，终点 %.2fs"
                                         % (span, d["t2"] / 1000.0))
                    self.assertGreater(d["t1"], 0)
                    self.assertLess(d["t1"], d["t2"], "这句根本没动")

    def test_the_width_counts_the_name_prefix(self):
        """说话人名算进文字宽——它是画面上的字，不算就滚不到位。"""
        cfg = dict(self.BASE, **{"speaker_indicator.name_shown": True,
                                 "tts.name_a": "小美", "tts.name_b": "大美"})
        ass = S.build_ass(self.SCRIPT, cfg, self.TIMINGS, 1920, 1080, "")[0]
        d = captions(ass)[1]
        g = S.frame_geometry(cfg, 1920, 1080, "", 1)
        w_t = S.text_px_width("大美：" + self.LONG, g["size"])
        self.assertEqual(d["x_from"], int(round(g["x1"] + w_t / 2.0)))

    def test_every_sentence_is_clipped_to_the_box(self):
        """每条都裁到框内：\\clip 的矩形与框四点一致。"""
        ass, _, _ = self._ass()
        g = S.frame_geometry(self.BASE, 1920, 1080, "", 1)
        want = "\\clip(%d,%d,%d,%d)" % (g["x1"], g["top"], g["x2"], g["bottom"])
        for d in captions(ass):
            with self.subTest(start=d["start"]):
                self.assertIn(want, d["head"])

    def test_speaker_colours_still_apply(self):
        """说话人分色在这一档照旧：样式名字跟歌词档同一套。"""
        ass, _, _ = self._ass()
        self.assertEqual([d["style"] for d in captions(ass)],
                         ["SpeakerA", "SpeakerB", "SpeakerA"])

    def test_highlight_turns_the_only_sentence_into_the_highlight_colour(self):
        """屏幕上只有当前句，高亮就是整句换色（不建上下文样式）。"""
        ass, _, _ = self._ass(**{"subtitle.highlight": True,
                                 "subtitle.highlight_color": "#FFD98A"})
        self.assertEqual({d["style"] for d in captions(ass)}, {"FrameCur"})
        self.assertIn("Style: FrameCur,%s,52,&H8AD9FF"
                      % self.BASE["subtitle.font_family"], ass)
        self.assertNotIn("FrameCtx", ass)


class TestLyricPreset(unittest.TestCase):
    """歌词版式（纵轴）：框内行预算、自动折行、上滚、高亮。"""

    SCRIPT = [
        {"speaker": "A", "text": "第一句很短。"},
        {"speaker": "B", "text": "第二句长一些，这里故意凑到超过一行容量好让它折成两行看看窗口怎么排。"},
        {"speaker": "A", "text": "第三句。"},
    ]
    TIMINGS = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 6.0}, {"start": 6.0, "end": 8.0}]
    BASE = {"subtitle.preset": "lyric", "speaker_indicator.name_shown": False,
            "speaker_indicator.mode": "style", "subtitle.font_family": "HarmonyOS Sans SC"}

    def _ass(self, script=None, timings=None, suffix="", size=(1920, 1080), **over):
        cfg = dict(self.BASE, **over)
        text, mc, axis = S.build_ass(script or self.SCRIPT, cfg,
                                     timings or self.TIMINGS,
                                     size[0], size[1], suffix)
        return text, mc, axis

    def _rows(self, ass):
        return dialogues(ass)

    def _by_interval(self, ass):
        """按区间分组（区间起点就是句子的起点）。"""
        groups = {}
        for d in self._rows(ass):
            groups.setdefault(d["start"], []).append(d)
        return [groups[k] for k in sorted(groups)]

    def _boxes(self, ass):
        return boxes(ass)

    def _lyrics(self, ass):
        return captions(ass)

    def _geom(self, cfg=None, size=(1920, 1080), sfx=""):
        """框几何：直接问引擎要（唯一出处），测试不再自己算一遍。"""
        cfg = cfg if cfg is not None else self.BASE
        rows = max(2, int(cfg.get("subtitle.frame_rows", 8)))
        return S.frame_geometry(cfg, size[0], size[1], sfx, rows)

    def test_preset_reports_the_vertical_axis(self):
        _, mc, axis = self._ass()
        self.assertEqual(axis, "y")
        self.assertEqual(mc, (1920 - 90 * 2) // 52)        # 每行容量仍由边距与字号算

    def test_capacity_is_ours_not_the_renderers(self):
        """自动折行是我们算的：每一段都 ≤ 每行容量。

        实测过 libass 不自动折行（不写 \\N 时长句直接顶到边），所以「交给渲染器
        自动换行」这条路在画面烧录里不存在——折行只能自己来。
        """
        ass, mc, _ = self._ass()
        for d in self._lyrics(ass):
            for seg in d["text"].split("\\N"):
                self.assertLessEqual(len(seg), mc, "行超容量：%r" % seg)

    def test_row_budget_counts_blank_rows(self):
        """框高按行计（含空行）：每句的代价 = 1 个空行 + 它自己折的行数。

        只数文字（图层 1）——垫底的框（图层 0）不占文字行预算。
        """
        for cap in (5, 8, 12):
            ass, _, _ = self._ass(**{"subtitle.frame_rows": cap})
            for j, evs in enumerate(self._by_interval(ass)):
                used = sum(len(d["text"].split("\\N")) + 1
                           for d in evs if d["layer"] == "1")
                with self.subTest(cap=cap, interval=j):
                    self.assertLessEqual(used, cap)

    def test_window_cap_limits_visible_sentences(self):
        ass, _, _ = self._ass(**{"subtitle.window": 2, "subtitle.frame_rows": 12})
        for j, evs in enumerate(self._by_interval(ass)):
            n = len([d for d in evs if d["layer"] == "1"])
            with self.subTest(interval=j):
                self.assertLessEqual(n, 2)

    def test_shift_is_one_rigid_block(self):
        """整块带子上移：同一区间里各句的位移量相同，横向中心不动。"""
        ass, _, _ = self._ass()
        self.assertIn("\\move(", ass, "一处都不滚，「上滚」是空话")
        for j, evs in enumerate(self._by_interval(ass)):
            deltas = set()
            for d in evs:
                if d["layer"] != "1":
                    continue
                self.assertEqual(d["x_from"], d["x_to"], "横向中心被移动了")
                deltas.add(d["y_to"] - d["y_from"])
            with self.subTest(interval=j):
                self.assertLessEqual(len(deltas), 1, "同区间内各句位移不一致：%s" % deltas)

    def test_scroll_happens_at_the_sentence_boundary(self):
        """滚在换点那一刻开始：\\move 的 t1 = 0，历时就是配置里的上滚时长。"""
        ass, _, _ = self._ass(**{"subtitle.scroll_ms": 600})
        moved = [d for d in self._rows(ass) if d["layer"] == "1" and "\\move(" in d["head"]]
        self.assertTrue(moved)
        for d in moved:
            self.assertEqual((d["t1"], d["t2"]), (0, 600))
        ass0, _, _ = self._ass(**{"subtitle.scroll_ms": 0})
        self.assertNotIn("\\move(", ass0)          # 0 即不滚，直接跳

    def test_squeezed_sentence_slides_out_and_fades(self):
        """被框高挤出去的那句在换点处上滚淡出，不是硬消失。"""
        ass, _, _ = self._ass(**{"subtitle.frame_rows": 5})
        exits = [d for d in self._rows(ass) if d["layer"] == "2"]
        self.assertTrue(exits, "行预算挤压没触发，这条没验到东西")
        for d in exits:
            self.assertIn("\\fad(0,", d["head"])
            self.assertIn("\\move(", d["head"])

    def test_highlight_marks_the_sentence_in_the_box_centre(self):
        """当前句只有一个、且落在框的垂直中心；上下文用压暗的说话人本色。"""
        ass, _, _ = self._ass(**{"subtitle.highlight": True,
                                 "subtitle.highlight_color": "#FFD98A"})
        anchor = int(round(self._geom()["anchor"]))
        per = self._by_interval(ass)
        for j, evs in enumerate(per[:len(self.SCRIPT)]):
            cur = [d for d in evs if d["style"] == "FrameCur"]
            with self.subTest(interval=j):
                self.assertEqual(len(cur), 1, "当前句该有且只该有一句是高亮色")
                self.assertEqual(cur[0]["y_to"], anchor)           # 落在框垂直中心
                self.assertIn(self.SCRIPT[j]["text"][:4], cur[0]["text"])
            for d in evs:
                if d["layer"] == "1" and d["style"] != "FrameCur":
                    self.assertIn(d["style"], ("FrameCtxA", "FrameCtxB"))
        # 高亮色真的写进了样式表（#FFD98A → ASS 的 &H8AD9FF）
        self.assertIn("Style: FrameCur,%s,52,&H8AD9FF" % self.BASE["subtitle.font_family"], ass)

    def test_highlight_off_keeps_speaker_colours(self):
        """高亮关掉时三句同底色（读到哪里只靠位置看），不建高亮样式。"""
        ass, _, _ = self._ass(**{"subtitle.highlight": False})
        styles = {d["style"] for d in self._rows(ass) if d["layer"] == "1"}
        self.assertEqual(styles, {"SpeakerA", "SpeakerB"})
        self.assertNotIn("Style: FrameCur", ass)
        self.assertNotIn("FrameCtx", ass)

    def test_highlight_off_ignores_a_configured_colour(self):
        """配色只在开关打开时才读——关着还染色的活，是开关没接上。"""
        ass, _, _ = self._ass(**{"subtitle.highlight": False,
                                 "subtitle.highlight_color": "#FF0000"})
        self.assertNotIn("Style: FrameCur", ass)

    def test_long_sentence_gets_as_many_lines_as_needed(self):
        """料多长就排多少行：96 字一句在三行容量下必须排满三行，不是两行。"""
        long_text = "既然产物种类繁多，统一归口预设文件夹是为了让后续自动化清理与备份能精准定位目标。" * 3
        cfg = {"subtitle.preset": "lyric", "subtitle.window": 1,
               "subtitle.frame_rows": 24, "speaker_indicator.name_shown": False}
        ass, mc, _ = S.build_ass([{"speaker": "A", "text": long_text}], cfg,
                                 [{"start": 0.0, "end": 5.0}], 1920, 1080, "")
        body = [d for d in self._rows(ass) if d["layer"] == "1"][0]["text"]
        rows = body.split("\\N")
        self.assertEqual(len(rows), -(-len(long_text) // mc))

    def test_box_geometry_uses_each_frames_own_margin(self):
        """框几何：底 = 画幅高 − 边距，高 = 行数 × 行距，左右由 margin_lr 收。

        竖屏取 margin_v_vertical、横屏取 margin_v。两种画幅靠各自的边距各得其所。
        几何只有一处出处（`frame_geometry`），烘焙与 ASS 都从这里取——所以这里比的是
        「画框那份参数」与引擎给的那组数，不再去 ASS 里找框事件。
        """
        for (w, h), sfx, size, mvkey, mv in (
                ((1920, 1080), "", 52, "subtitle.margin_v", 90),
                ((1080, 1920), "_v", 40, "subtitle.margin_v_vertical", 220)):
            cfg = dict(self.BASE, **{"subtitle.window": 1, mvkey: mv,
                                     "subtitle.bg_alpha": 128, "subtitle.outline": 2})
            g = self._geom(cfg, (w, h), sfx)
            p = S.frame_box_paint(g, 128, 2)
            with self.subTest(size=(w, h)):
                self.assertEqual(p["rect"][0], g["x1"], "框左边 ≠ margin_lr")
                self.assertEqual(p["rect"][2], g["x2"])
                self.assertEqual(p["rect"][1], g["top"], "框顶 ≠ 高 − 边距 − 框高")
                self.assertEqual(p["rect"][3], g["bottom"])
                self.assertEqual(g["h"], 8 * max(1, int(round(size * S.FRAME_LINE_PITCH))))
                self.assertEqual(g["bottom"], h - mv, "底边没按这一画幅自己的边距收")

    def test_box_fill_and_border_come_from_the_config(self):
        """框填充 = bg_alpha；描边宽 = subtitle.outline（两档共用同一项）。

        从前歌词档不读 subtitle.outline（那一项只给单双行的填充块当内边距），
        描边写死 2px——两档各有一套框的样子，正是这次合并要消掉的东西。框改由烘焙
        画进背景图之后，这一项仍然只值一处：`frame_box_paint` 的参数。
        """
        g = self._geom()
        p = S.frame_box_paint(g, 64, 7)
        self.assertEqual(p["width"], 7, "描边宽没跟 subtitle.outline 走")
        self.assertEqual(p["out"], 7, "描边只往外扩整宽")
        # ASS 口径的 64 透明度 → Pillow 的不透明度 191（255 × (1 − 64/255)）
        self.assertEqual(p["fill_alpha"], 191)
        # 描边的不透明度是常量（FRAME_BOX_BORDER_ALPHA = 0x66）
        self.assertEqual(p["border_alpha"], 153)

    def test_every_line_is_clipped_to_the_box(self):
        """每条歌词都裁到框内：\\clip 的矩形与框四点一致（上滚时不许飘出框）。"""
        ass, _, _ = self._ass()
        g = self._geom()
        want = r"\clip(%d,%d,%d,%d)" % (g["x1"], g["top"], g["x2"], g["bottom"])
        lines = self._lyrics(ass)
        self.assertTrue(lines)
        for d in lines:
            self.assertIn(want, d["head"])

    def test_layers_are_text_only_the_box_is_not_an_ass_layer(self):
        """框不再是 ASS 里的一层。

        从前框是 layer 0 的 `\\p1` 绘图事件、文字在 1/2 层。现在框由烘焙画在背景图上，
        ASS 里只剩文字层——留着框事件就成了「两处都能画框」，迟早叠成两层。
        """
        ass, _, _ = self._ass(**{"subtitle.frame_rows": 5})
        self.assertEqual(boxes(ass), [], "ASS 里还有框事件")
        self.assertEqual({d["layer"] for d in dialogues(ass)}, {"1", "2"},
                         "文字层号仍是 1（常驻）/ 2（淡出）")

    def test_both_presets_share_one_style_set(self):
        """两档的 Style 逐字相同：per-line 盒关掉（BackColour 全透明、BorderStyle=1）、
        描边宽同取 subtitle.outline。从前的单双行是另一套（BorderStyle=3 + per-line
        填充块），同一个下拉里塞两套框的样子。"""
        want = ",&HFF000000,0,0,0,0,100,100,0,0,1,%d,0,2," % 6
        for preset in ("lyric", "single"):
            cfg = {"subtitle.preset": preset, "speaker_indicator.mode": "style",
                   "speaker_indicator.name_shown": False, "subtitle.outline": 6,
                   "subtitle.font_family": "HarmonyOS Sans SC"}
            txt = S.build_ass(self.SCRIPT, cfg, self.TIMINGS, 1920, 1080, "")[0]
            with self.subTest(preset=preset):
                self.assertIn(want, txt.split("[Events]")[0])

    def test_lrc_and_srt_ignore_the_window(self):
        """框只管画面字幕：SRT 与 LRC 一行一条，不看窗口与框高。"""
        srt = S.build_srt(self.SCRIPT, self.BASE, self.TIMINGS)
        self.assertEqual([ln for ln in srt.split("\n") if " --> " in ln][1],
                         "00:00:02,000 --> 00:00:06,000")
        self.assertIn(self.SCRIPT[1]["text"], srt)
        lrc = S.build_lrc(self.SCRIPT, self.BASE, self.TIMINGS)
        self.assertEqual(len(lrc.split("\n")), 3)


class TestPreviewSharesTheGeometry(unittest.TestCase):
    """预览与成片必须同源。

    配置页那两格预览是拿 JS 重画一遍的（`web_ui.paintSubPreview`）：框的几何、字宽、
    横滚的静止占比都得跟成片一样。界面上的 JS 读不到 Python 常量，只能靠对账——
    系数差一成，预览里的滚动终点就跟成片差一截，而界面看着一切正常（人按预览调参数，
    成片却是另一个样子）。这一组按源码扫，逮住就拦。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "podcast_maker", "web_ui.py"),
                  encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_pixel_width_coefficients_are_the_same_numbers(self):
        m = re.search(r"const CHAR_W=\{han:([\d.]+),full:([\d.]+),half:([\d.]+)\}",
                      self.src)
        self.assertIsNotNone(m, "预览的字宽系数表没了，正则已失效")
        self.assertEqual([float(x) for x in m.groups()],
                         [S.CHAR_W_HAN, S.CHAR_W_FULL, S.CHAR_W_HALF],
                         "预览与后端的字宽系数对不上，横滚终点会各算各的")

    def test_line_pitch_matches_the_backend(self):
        self.assertIn("Math.round(size*%s)" % S.FRAME_LINE_PITCH, self.src,
                      "预览的行距不是后端那一档系数（%s）" % S.FRAME_LINE_PITCH)

    def test_the_roll_rhythm_matches_the_backend_formula(self):
        """静止占比 = 框宽 ÷ 文字宽；两端分别贴框左 / 框右——与 `_strip_events` 同式。"""
        for want in ("bw/tw", "x1+tw/2", "x2-tw/2", "animateTransform"):
            with self.subTest(fragment=want):
                self.assertIn(want, self.src, "预览的横滚少了这一式：%s" % want)

    def test_preview_converts_the_ass_colours_to_css(self):
        """配色两种写法，取哪一路看点位：`subtitle.color_a` 是 ASS 的 `&HAABBGGRR`，
        `highlight_color` 是 CSS 的 `#RRGGBB`。

        前者直接喂给 SVG 的 fill 是非法值——浏览器丢掉它、回落到黑色，深色框里的
        「非当前句」就成了看不见的字（`&HFFFFFF` 倒过来还是白，所以白字下看不出问题）。
        预览必须过一道 `cssColor`。
        """
        self.assertIn("cssColor(", self.src, "预览没做配色转换")
        self.assertIn("h.slice(6,8)+h.slice(4,6)+h.slice(2,4)", self.src,
                      "ASS→CSS 的字节序没倒过来（BGR → RGB）")
        self.assertIn("const txtColor=cssColor(cfgVal('subtitle.color_a')", self.src)
        self.assertIn("const hlColor=cssColor(cfgVal('subtitle.highlight_color')", self.src)

    def test_the_box_is_drawn_from_the_same_four_numbers(self):
        """框的四点与后端 `frame_geometry` 同源：x1=mlr、x2=W−mlr、底=H−mv、高=行数×行距。"""
        for want in ("const fh=rows*pitch", "x1=mlr", "x2=W-mlr", "y2=H-mv",
                     "(axis==='x')?1:"):
            with self.subTest(fragment=want):
                self.assertIn(want, self.src, "预览的框几何与后端脱钩了：%s" % want)


class TestWrapStyle(unittest.TestCase):
    """折行只由引擎自己决定，libass 不许插手。

    `WrapStyle: 0`（smart wrap）允许 libass 把宽于可用宽的文字自己折成两行。单行
    滚动档就是这么长出双行的：整句连说话人名 2458 px 宽、框内可用宽只有 1740 px，
    libass 折成两行，两行都落在 70 px 高的框里，于是屏幕上真的并排两行小字。
    `\\clip` 只裁像素、拦不住折行——这不是裁切能解决的问题，只能把折行的决定收回
    来：写成 2（只在 `\\N` 处断）。本引擎的文字全是自己拼的、断点全由 `_frame_window`
    的 `\\N` 给出，所以关掉自动折行不会少任何一处换行。
    """

    SCRIPT = [{"speaker": "A", "text": "第一句。"},
              {"speaker": "B", "text": "既然产物种类繁多，统一归口预设文件夹。"}]
    TIMINGS = [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 6.0}]

    def _ass(self, preset, w=1920, h=1080, suffix=""):
        cfg = {"subtitle.preset": preset,
               "subtitle.font_family": "HarmonyOS Sans SC"}
        return S.build_ass(self.SCRIPT, cfg, self.TIMINGS, w, h, suffix)[0]

    def test_wrap_is_off_in_both_presets_and_both_orientations(self):
        for preset in ("single", "lyric"):
            for w, h, suffix in ((1920, 1080, ""), (1080, 1920, "_v")):
                with self.subTest(preset=preset, suffix=suffix or "h"):
                    head = self._ass(preset, w, h, suffix).split("[Events]")[0]
                    self.assertIn("WrapStyle: 2", head)

    def test_nobody_hands_the_decision_back_to_libass(self):
        """`\\q` 是逐事件的折行覆盖。写一条就等于把决定又还回去一半。"""
        for preset in ("single", "lyric"):
            with self.subTest(preset=preset):
                self.assertNotIn(chr(92) + "q", self._ass(preset))

    def test_ass_no_longer_carries_the_box_event(self):
        """框改由烘焙画进背景图（`assets_factory.bake_static_layers`），ASS 里不该
        再有 `\\p1` 绘图事件——留着就是「两处都能画框」，迟早叠成两层。"""
        for preset in ("single", "lyric"):
            with self.subTest(preset=preset):
                self.assertEqual(boxes(self._ass(preset)), [],
                                 "ASS 里还有框事件：%s" % preset)


# 单行滚动档的冻结值。来源**不是**「照现在的代码跑一遍」——那会连 bug 一起冻上；
# 是改造后逐条核对过横滚算式（位移 = 文字宽 − 框宽、句首静止 = 框宽 ÷ 速度）之后
# 取下来的。所以这里红了只有两种可能：横滚真的被动过，或名字开关串了路。
#
# 2026-09-24 重取过一次（0.42.0 的「字幕版式两档」与「静态层预烘焙」）：ASS 全文确实
# 变了，但**只变了这两处**——`WrapStyle: 0 → 2`（关掉 libass 的自动折行，单行滚动
# 不再长出双行）与删掉框事件（框改由 `assets_factory.bake_static_layers` 画进背景图）。
# 这不是「跑一遍取个新值」：拿已发布那一期真用过的 ASS（253 句、1920×1080）逐字节
# 比对过——**253 条文字事件完全一致**，头部只有 `WrapStyle` 一行不同，框事件旧 1 条新 0 条。
FROZEN_FIXTURE = {
    "script": [
        {"speaker": "A", "text": "第一句。"},
        {"speaker": "B", "text": "既然产物种类繁多，统一归口预设文件夹是为了让后续自动化清理与备份能精准定位目标。"},
        {"speaker": "A", "text": "我们把 GPT-4 和 Claude-3.5 放在一起比较它们的表现，看看谁的断行更稳。"},
        {"speaker": "B", "text": "短句。"},
    ],
    "timings": [{"start": 0.0, "end": 2.0}, {"start": 2.0, "end": 6.0},
                {"start": 6.0, "end": 10.0}, {"start": 10.0, "end": 11.5}],
}
FROZEN_ASS = {
    "single|none|False|h": "c39ce614428a0e25",
    "single|none|False|_v": "2ecb2687c9e9d15f",
    "single|style|True|h": "e081ad3b2c6ce0fb",
    "single|style|True|_v": "f355fd89ad925d4b",
}
# 横滚的算式样本：（句时长秒, 期望的 move 参数 x_from/x_to/t1/t2）。文字与画幅
# 固定为下面这条 81 字的句子与横屏 1920×1080——数字背后是
# 「文字宽 3155.8px、框宽 1740px、位移 1415.8px、句首静止 14.94 秒」。
ROLL_TEXT = ("上期《骨架叙事切分与介质边界的确定性约束》聊的是聚焦成书的结构性排版，"
             "揭示注册列表驱动的四部叙事弧线、页面物理边界（断页/字体/缩放）与"
             "元数据口径的刚性同步机制。")
FROZEN_ROLL = {
    27.1: (1668, 252, 14942, 27100),
}


class TestStripFrozen(unittest.TestCase):
    """横滚档必须逐字不变。

    这一档是新写的，风险是往后有人「顺手调一个数」——位移或句首静止一偏，观众
    就是「读不完」或「开头没读到」。下面钉住 ASS 全文的 sha256 与 \\move 的四个数。
    """

    def test_ass_text_is_byte_identical(self):
        for key, want in sorted(FROZEN_ASS.items()):
            preset, mode, named, sfx = key.split("|")
            cfg = {"subtitle.preset": preset, "speaker_indicator.mode": mode,
                   "speaker_indicator.name_shown": named == "True",
                   "tts.name_a": "小美", "tts.name_b": "大美",
                   "subtitle.font_family": "HarmonyOS Sans SC"}
            w, h = (1080, 1920) if sfx == "_v" else (1920, 1080)
            text = S.build_ass(FROZEN_FIXTURE["script"], cfg, FROZEN_FIXTURE["timings"],
                               w, h, "" if sfx == "h" else "_v")[0]
            got = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            with self.subTest(case=key):
                self.assertEqual(got, want, "横滚这一路被改动了：%s" % key)

    def test_roll_arithmetic_is_frozen(self):
        cfg = {"subtitle.preset": "single", "speaker_indicator.name_shown": False,
               "subtitle.font_family": "HarmonyOS Sans SC"}
        for span, want in FROZEN_ROLL.items():
            ass = S.build_ass([{"speaker": "B", "text": ROLL_TEXT}], cfg,
                              [{"start": 0.0, "end": span}], 1920, 1080, "")[0]
            d = captions(ass)[0]
            with self.subTest(span=span):
                self.assertEqual((d["x_from"], d["x_to"], d["t1"], d["t2"]), want)

    def test_stats_shape_is_frozen(self):
        st = S.wrap_stats(FROZEN_FIXTURE["script"], 33, "y")
        self.assertEqual(st, {"wraps": 2, "violations": 0, "rate": 0.0, "samples": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
