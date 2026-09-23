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

"""字幕断行测试：断词率必须为 0，且单行/双行两档的输出逐字不变。"""

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


class TestWrap(unittest.TestCase):
    def test_no_breaks_inside_words(self):
        """西文 token 不得被切断（Claude-3.5 内部含 - 与 .）。"""
        for text, width in CASES:
            chunks = S.wrap_text(text, width, 2)
            for chunk in chunks:
                for token in TOKEN_RE.findall(text):
                    for i in range(1, len(token)):
                        head, tail = token[:i], token[i:]
                        if any(c.endswith(head) for c in chunks) and \
                           any(c.startswith(tail) for c in chunks):
                            self.fail("token %r 在第 %d 位被切断：%r" % (token, i, chunks))

    def test_no_violations_across_cases(self):
        """整卷断词率必须为 0。"""
        for text, width in CASES:
            n = S.count_word_breaks(text, width, 2)
            self.assertEqual(n, 0, "宽度 %d 下 %r 出现 %d 处断词" % (width, text, n))

    def test_kinsoku_head_char_never_starts_line(self):
        """禁则字不得出现在行首。"""
        for text, width in CASES:
            for chunk in S.wrap_text(text, width, 2):
                if chunk:
                    self.assertNotIn(chunk[0], S.KINSOKU_HEAD,
                                     "%r 以禁则字 %r 起行" % (chunk, chunk[0]))

    def test_break_after_punctuation_wins(self):
        """标点之后是最高优先级断点。"""
        idx = S.find_break("这个问题的答案其实很简单，就是不该停的地方不能停。", 16)
        self.assertEqual(idx, 13)
        self.assertTrue(S.wrap_text("这个问题的答案其实很简单，就是不该停的地方不能停。",
                                    16, 2)[0].endswith("，"))

    def test_long_unbreakable_text_falls_back(self):
        """无任何合法断点时二分回退，且不抛异常、不丢字。"""
        text = "中" * 60
        chunks = S.wrap_text(text, 12, 2)
        self.assertTrue(chunks)
        self.assertEqual("".join(chunks), text)

    def test_short_text_untouched(self):
        self.assertEqual(S.wrap_text("短句。", 20, 2), ["短句。"])

    def test_wrap_stats_shape(self):
        script = [{"speaker": "A", "text": t} for t, _ in CASES]
        st = S.wrap_stats(script, 16, 2)
        for key in ("wraps", "violations", "rate"):
            self.assertIn(key, st)
        self.assertEqual(st["violations"], 0)


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
        for preset in ("single", "dual", "lyric"):
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

    「单行/双行」那一档的毛病是贪心填满——第一行撑满、余数甩末行，40 字一句在
    33 字/行下得到 33+7。均衡版先定行数再均分，得到 21+19。
    """

    LONG = "既然产物种类繁多，统一归口预设文件夹是为了让后续自动化清理与备份能精准定位目标。"
    TEXTS = [LONG, LONG * 2, "中" * 96, "我们把 GPT-4 和 Claude-3.5 放在一起比较它们的表现，"
                               "看看谁在长句里的断点更稳一点。"]

    def test_line_count_is_what_the_capacity_needs(self):
        for text in self.TEXTS:
            for mc in (12, 22, 33):
                with self.subTest(text=text[:8], mc=mc):
                    rows = S.wrap_balanced(text, mc)
                    self.assertEqual(len(rows), -(-len(text) // mc))

    def test_every_line_fits_the_capacity(self):
        """含末行——与 wrap_text「末行允许超出、不丢字」的取舍不同。

        歌词窗口里一行超出画面宽会直接顶到边上，比多一行难看。
        """
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
                    self.assertEqual(S.count_word_breaks(text, mc, 2, True), 0)

    def test_balances_where_greedy_leaves_a_stub(self):
        """同一句 40 字：贪心给 33+7（头重脚轻），均衡给 21+19。"""
        greedy = S.wrap_text(self.LONG, 33, 2)
        bal = S.wrap_balanced(self.LONG, 33)
        self.assertEqual(greedy, ["既然产物种类繁多，统一归口预设文件夹是为了让后续自动化清理与备份能",
                                  "精准定位目标。"])
        self.assertEqual(bal, ["既然产物种类繁多，统一归口预设文件夹是为了",
                               "让后续自动化清理与备份能精准定位目标。"])
        self.assertLess(max(len(x) for x in bal) - min(len(x) for x in bal),
                        max(len(x) for x in greedy) - min(len(x) for x in greedy))

    def test_short_text_untouched(self):
        self.assertEqual(S.wrap_balanced("短句。", 20), ["短句。"])
        self.assertEqual(S.wrap_balanced("", 20), [""])

    def test_stats_follow_the_balanced_cuts(self):
        """统计必须跟实际折行走同一套切点，否则报的是另一种折法的账。"""
        st = S.wrap_stats([{"text": self.LONG}], 33, 2, True)
        self.assertEqual((st["wraps"], st["violations"], st["rate"]), (1, 0, 0.0))
        # 歌词档不设上限，写死 max_lines=1 也不该让它漏统计（单行档才该跳过）
        self.assertEqual(S.wrap_stats([{"text": self.LONG}], 33, 1, True)["wraps"], 1)
        self.assertEqual(S.wrap_stats([{"text": self.LONG}], 33, 1)["wraps"], 0)


class TestLyricPreset(unittest.TestCase):
    """歌词版式：窗口行预算、自动折行、上滚、高亮。"""

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
        text, mc, ml, balanced = S.build_ass(script or self.SCRIPT, cfg,
                                             timings or self.TIMINGS,
                                             size[0], size[1], suffix)
        return text, mc, ml, balanced

    @staticmethod
    def _rows(ass):
        """把 Dialogue 拆成字段。head 里的 \\pos/\\move 是歌词档的位置来源。"""
        out = []
        for line in ass.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            f = line.split(",", 9)
            raw = f[9]
            head, text = (raw.split("}", 1) + [""])[:2] if raw.startswith("{") else ("", raw)
            head = head + "}" if head else ""
            rec = {"layer": f[0].split()[1], "start": f[1], "end": f[2],
                   "style": f[3], "head": head, "text": text}
            mv = re.search(r"\\move\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)", head)
            ps = re.search(r"\\pos\((\d+),(\d+)\)", head)
            if mv:
                rec.update(cx=int(mv.group(1)), y_from=int(mv.group(2)),
                           cx2=int(mv.group(3)), y_to=int(mv.group(4)),
                           t1=int(mv.group(5)), t2=int(mv.group(6)))
            elif ps:
                rec.update(cx=int(ps.group(1)), y_from=int(ps.group(2)),
                           cx2=int(ps.group(1)), y_to=int(ps.group(2)))
            out.append(rec)
        return out

    def _by_interval(self, ass):
        """按区间分组（区间起点就是句子的起点）。"""
        groups = {}
        for d in self._rows(ass):
            groups.setdefault(d["start"], []).append(d)
        return [groups[k] for k in sorted(groups)]

    def test_preset_reports_balanced(self):
        _, mc, _, balanced = self._ass()
        self.assertTrue(balanced, "歌词档没走到均衡折行那一支")
        self.assertEqual(mc, (1920 - 90 * 2) // 52)        # 每行容量仍由边距与字号算

    def test_capacity_is_ours_not_the_renderers(self):
        """自动折行是我们算的：每一段都 ≤ 每行容量。

        实测过 libass 不自动折行（不写 \\N 时长句直接顶到边），所以「交给渲染器
        自动换行」这条路在画面烧录里不存在——折行只能自己来。
        """
        ass, mc, _, _ = self._ass()
        for d in self._rows(ass):
            for seg in d["text"].split("\\N"):
                self.assertLessEqual(len(seg), mc, "行超容量：%r" % seg)

    def test_row_budget_counts_blank_rows(self):
        """上限按行计（含空行）：每句的代价 = 1 个空行 + 它自己折的行数。"""
        for cap in (5, 8, 12):
            ass, _, _, _ = self._ass(**{"subtitle.lyric_max_rows": cap})
            for j, evs in enumerate(self._by_interval(ass)):
                used = sum(len(d["text"].split("\\N")) + 1
                           for d in evs if d["layer"] == "0")
                with self.subTest(cap=cap, interval=j):
                    self.assertLessEqual(used, cap)

    def test_window_cap_limits_visible_sentences(self):
        ass, _, _, _ = self._ass(**{"subtitle.lyric_window": 2,
                                   "subtitle.lyric_max_rows": 12})
        for j, evs in enumerate(self._by_interval(ass)):
            n = len([d for d in evs if d["layer"] == "0"])
            with self.subTest(interval=j):
                self.assertLessEqual(n, 2)

    def test_shift_is_one_rigid_block(self):
        """整块带子上移：同一区间里各句的位移量相同，横向中心不动。"""
        ass, _, _, _ = self._ass()
        self.assertIn("\\move(", ass, "一处都不滚，「上滚」是空话")
        for j, evs in enumerate(self._by_interval(ass)):
            deltas = set()
            for d in evs:
                if d["layer"] != "0":
                    continue
                self.assertEqual(d["cx"], d["cx2"], "横向中心被移动了")
                deltas.add(d["y_to"] - d["y_from"])
            with self.subTest(interval=j):
                self.assertLessEqual(len(deltas), 1, "同区间内各句位移不一致：%s" % deltas)

    def test_scroll_happens_at_the_sentence_boundary(self):
        """滚在换点那一刻开始：\\move 的 t1 = 0，历时就是配置里的上滚时长。"""
        ass, _, _, _ = self._ass(**{"subtitle.lyric_scroll_ms": 600})
        moved = [d for d in self._rows(ass) if d["layer"] == "0" and "\\move(" in d["head"]]
        self.assertTrue(moved)
        for d in moved:
            self.assertEqual((d["t1"], d["t2"]), (0, 600))
        ass0, _, _, _ = self._ass(**{"subtitle.lyric_scroll_ms": 0})
        self.assertNotIn("\\move(", ass0)          # 0 即不滚，直接跳

    def test_squeezed_sentence_slides_out_and_fades(self):
        """被行预算挤出去的那句在换点处上滚淡出，不是硬消失。"""
        ass, _, _, _ = self._ass(**{"subtitle.lyric_max_rows": 5})
        exits = [d for d in self._rows(ass) if d["layer"] == "1"]
        self.assertTrue(exits, "行预算挤压没触发，这条没验到东西")
        for d in exits:
            self.assertIn("\\fad(0,", d["head"])
            self.assertIn("\\move(", d["head"])

    def test_highlight_marks_the_sentence_at_the_anchor(self):
        ass, _, _, _ = self._ass(**{"subtitle.highlight": True,
                                    "subtitle.highlight_color": "#FFD98A"})
        per = self._by_interval(ass)
        for j, evs in enumerate(per[:len(self.SCRIPT)]):
            cur = [d for d in evs if d["style"] == "LyricCur"]
            with self.subTest(interval=j):
                self.assertEqual(len(cur), 1, "当前句该有且只该有一句是高亮色")
                self.assertEqual(cur[0]["y_to"], 470)              # 落在锚点上
                self.assertIn(self.SCRIPT[j]["text"][:4], cur[0]["text"])
            for d in evs:
                if d["layer"] == "0" and d["style"] != "LyricCur":
                    self.assertIn(d["style"], ("LyricCtxA", "LyricCtxB"))
        # 高亮色真的写进了样式表（#FFD98A → ASS 的 &H8AD9FF）
        self.assertIn("Style: LyricCur,%s,52,&H8AD9FF" % self.BASE["subtitle.font_family"], ass)

    def test_highlight_off_keeps_speaker_colours(self):
        """高亮关掉时三句同底色（读到哪里只靠位置看），不建高亮样式。"""
        ass, _, _, _ = self._ass(**{"subtitle.highlight": False})
        styles = {d["style"] for d in self._rows(ass) if d["layer"] == "0"}
        self.assertEqual(styles, {"SpeakerA", "SpeakerB"})
        self.assertNotIn("Style: LyricCur", ass)
        self.assertNotIn("LyricCtx", ass)

    def test_highlight_off_ignores_a_configured_colour(self):
        """配色只在开关打开时才读——关着还染色的活，是开关没接上。"""
        ass, _, _, _ = self._ass(**{"subtitle.highlight": False,
                                    "subtitle.highlight_color": "#FF0000"})
        self.assertNotIn("Style: LyricCur", ass)

    def test_long_sentence_gets_as_many_lines_as_needed(self):
        """料多长就排多少行：96 字一句在三行容量下必须排满三行，不是两行。"""
        long_text = "既然产物种类繁多，统一归口预设文件夹是为了让后续自动化清理与备份能精准定位目标。" * 3
        cfg = {"subtitle.preset": "lyric", "subtitle.lyric_window": 1,
               "subtitle.lyric_max_rows": 24, "speaker_indicator.name_shown": False}
        ass, mc, _, _ = S.build_ass([{"speaker": "A", "text": long_text}], cfg,
                                    [{"start": 0.0, "end": 5.0}], 1920, 1080, "")
        body = [d for d in self._rows(ass) if d["layer"] == "0"][0]["text"]
        rows = body.split("\\N")
        self.assertEqual(len(rows), -(-len(long_text) // mc))

    def test_vertical_anchor_scales_with_the_frame(self):
        """锚点按 1080 高的横屏定，竖屏按画幅比例换算（同一套配置两种画幅一致）。"""
        cfg = dict(self.BASE, **{"subtitle.lyric_window": 1, "subtitle.lyric_anchor_y": 470})
        ass_v, _, _, _ = S.build_ass(self.SCRIPT, cfg, self.TIMINGS, 1080, 1920, "_v")
        ys = [d["y_to"] for d in self._rows(ass_v) if d["layer"] == "0"]
        self.assertIn(int(round(470 * 1920.0 / 1080.0)), ys)

    def test_lrc_and_srt_ignore_the_window(self):
        """窗口只管画面字幕：SRT 与 LRC 一行一条，不看行数与窗口。"""
        srt = S.build_srt(self.SCRIPT, self.BASE, self.TIMINGS)
        self.assertEqual([ln for ln in srt.split("\n") if " --> " in ln][1],
                         "00:00:02,000 --> 00:00:06,000")
        self.assertIn(self.SCRIPT[1]["text"], srt)
        lrc = S.build_lrc(self.SCRIPT, self.BASE, self.TIMINGS)
        self.assertEqual(len(lrc.split("\n")), 3)


# 单行/双行两档的冻结值。来源不是「照现在的代码跑一遍」——那会连 bug 一起冻上，
# 而是拿改造前的发布副本（v0.39.0）与改造后各跑一份 dump，逐字比对 diff 为 0 之后
# 取下来的。所以这里红了只有两种可能：单双行真的被动过，或名字开关串了路。
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
    "single|none|False|h": "86a145ae131da143",
    "single|none|False|_v": "c87b2958b8dfeb99",
    "single|style|True|h": "e29381badf818491",
    "single|style|True|_v": "44b8f4747c20c39d",
    "dual|none|False|h": "5e1a8210f90571e4",
    "dual|none|False|_v": "95413c0bee7e015f",
    "dual|style|True|h": "1ae6ca4eb54650b5",
    "dual|style|True|_v": "38db2003d0ce4732",
}
FROZEN_WRAP = [
    ("这个问题的答案其实很简单，就是不该停的地方不能停，否则读者会读错意思。", 16, 2,
     ["这个问题的答案其实很简单，", "就是不该停的地方不能停，否则读者会读错意思。"]),
    ("我们把 GPT-4 和 Claude-3.5 放在一起比较它们的表现。", 18, 2,
     ["我们把 GPT-4 和 ", "Claude-3.5 放在一起比较它们的表现。"]),
    ("一条链加两头：链是主体，模型只做两端。", 10, 2,
     ["一条链加两头：", "链是主体，模型只做两端。"]),
]


class TestSingleDualFrozen(unittest.TestCase):
    """单行/双行两档必须逐字不变。

    这次改造把「折几行」从一个函数换成了两个，风险是顺手改了旧路径（改一处、
    另一处跟着动是最典型的回归形状）。下面钉住的是改造前的**实际输出**：ASS 全文
    的 sha256、wrap_text 的逐行结果。它们的 16 位摘要够用——本测试只防误改，
    不防有人蓄意伪造。
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
                self.assertEqual(got, want, "单双行这一路被改动了：%s" % key)

    def test_wrap_text_rows_are_frozen(self):
        for text, w, ml, want in FROZEN_WRAP:
            with self.subTest(text=text[:10]):
                self.assertEqual(S.wrap_text(text, w, ml), want)

    def test_stats_shape_is_frozen(self):
        st = S.wrap_stats(FROZEN_FIXTURE["script"], 33, 2)
        self.assertEqual(st, {"wraps": 2, "violations": 0, "rate": 0.0, "samples": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
