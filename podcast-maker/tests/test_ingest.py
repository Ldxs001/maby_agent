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

"""素材摄入回归测试。

钉住一条曾经自相矛盾的路径：上传 md 时若先把 `#` 剥掉，再回头按 `#` 找
章节锚点，锚点必然一个不剩——「按标题切章」这个功能在自己的实现里被拆掉。
这里同时约束两侧：标题行必须原样保留，正文里的行内标记必须照剥。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from podcast_maker import ingest  # noqa: E402


BOOK = """# 第一章 起点

本章讲**起点**与`工具`，参考[某处](http://example.com)。

## 1.1 小节甲

正文甲。

## 1.2 小节乙

正文乙。

# 第二章 展开

本章展开论述。

## 2.1 小节丙

正文丙。
"""


class TestMarkdownStripping(unittest.TestCase):

    def test_title_lines_keep_their_hashes(self):
        """标题行的 `#` 是结构锚点，剥掉之后按标题切章就无锚可依。"""
        out = ingest._strip_markdown(BOOK)
        self.assertIn("# 第一章 起点", out)
        self.assertIn("## 1.1 小节甲", out)

    def test_inline_marks_are_still_stripped_from_body(self):
        """剥正文标记的本职不能因为保留标题而丢掉。"""
        out = ingest._strip_markdown(BOOK)
        self.assertNotIn("**", out)
        self.assertNotIn("`工具`", out)
        self.assertNotIn("](http://example.com)", out)
        self.assertIn("本章讲起点与工具", out)

    def test_anchors_survive_upload_path(self):
        """上传 md 走 from_file，锚点必须在剥离之后仍然存在。"""
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(BOOK)
            res = ingest.ingest(file_path=path)
        finally:
            os.remove(path)
        anchors = [a["title"] for a in ingest.list_anchors(res["text"])]
        self.assertEqual(anchors, ["第一章 起点", "1.1 小节甲", "1.2 小节乙",
                                   "第二章 展开", "2.1 小节丙"])

    def test_plain_text_upload_is_untouched(self):
        """txt 不按 markdown 处理，`#` 若本来就在正文里就照留。"""
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("纯文本一行\n# 这不是标题\n")
            res = ingest.ingest(file_path=path)
        finally:
            os.remove(path)
        self.assertIn("# 这不是标题", res["text"])


class TestAnchors(unittest.TestCase):

    def test_levels_are_recorded(self):
        anchors = ingest.list_anchors(BOOK)
        self.assertEqual([a["level"] for a in anchors], [1, 2, 2, 1, 2])

    def test_slice_stops_at_same_or_higher_level(self):
        """切一章应含其下级标题，到同级或更高级标题为止。"""
        body, meta = ingest.slice_by_anchor(BOOK, "第一章 起点")
        self.assertIn("1.1 小节甲", body)
        self.assertIn("1.2 小节乙", body)
        self.assertNotIn("第二章 展开", body)
        self.assertEqual(meta["anchor"], "第一章 起点")

    def test_slice_of_leaf_section(self):
        body, meta = ingest.slice_by_anchor(BOOK, "1.1 小节甲")
        self.assertIn("正文甲", body)
        self.assertNotIn("正文乙", body)

    def test_partial_match_is_accepted(self):
        body, _ = ingest.slice_by_anchor(BOOK, "第二章")
        self.assertIn("本章展开论述", body)

    def test_missing_anchor_names_candidates(self):
        """找不到就报错并列出候选，绝不静默返回空串。"""
        with self.assertRaises(ingest.IngestError) as cm:
            ingest.slice_by_anchor(BOOK, "不存在的章节")
        self.assertIn("第一章 起点", str(cm.exception))

    def test_no_headings_at_all(self):
        with self.assertRaises(ingest.IngestError) as cm:
            ingest.slice_by_anchor("没有任何标题的正文。", "随便")
        self.assertIn("找不到任何可作锚点的标题行", str(cm.exception))

    def test_blank_anchor_rejected(self):
        with self.assertRaises(ingest.IngestError):
            ingest.slice_by_anchor(BOOK, "   ")


class TestIngestEntry(unittest.TestCase):

    def test_empty_text_rejected(self):
        with self.assertRaises(ingest.IngestError):
            ingest.ingest(text="   \n  ")

    def test_anchor_applied_on_entry(self):
        res = ingest.ingest(text=BOOK, anchor="第二章 展开")
        self.assertIn("本章展开论述", res["text"])
        self.assertNotIn("起点", res["text"])
        self.assertEqual(res["meta"]["anchor"], "第二章 展开")

    def test_unsupported_extension_rejected(self):
        fd, path = tempfile.mkstemp(suffix=".xyz")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("x")
            with self.assertRaises(ingest.IngestError) as cm:
                ingest.ingest(file_path=path)
        finally:
            os.remove(path)
        self.assertIn("不支持的文件类型", str(cm.exception))

    def test_missing_file_rejected(self):
        with self.assertRaises(ingest.IngestError):
            ingest.ingest(file_path=os.path.join(tempfile.gettempdir(), "没有这个文件.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
