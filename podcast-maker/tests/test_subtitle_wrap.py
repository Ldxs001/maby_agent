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

"""字幕断行测试：断词率必须为 0。"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
