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

"""素材库：整本书入库，再按期取用。

第九类缺陷：一次给一部几十万字的稿子，却只能一期期地手工摘抄。
素材库把稿子落盘成两级取用——排地图读章节清单，出片读落点原文。
两条纪律：索引坏了宁可报错也不当没入库（那等于把整本书当成从没给过），
截断必须记进 meta（静默截断会让模型在缺料下硬写，产物却看不出来）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import source_store as S                              # noqa: E402
from podcast_maker.source_store import SourceError                       # noqa: E402

BOOK = """# 第一章 起点

这一章讲事情的由来，交代人物与处境，篇幅不长。

正文若干句，够切成一个锚点。

## 第一节 名字的来历

名字不是随手取的，背后有一段账要算。

## 第二节 第一次出门

出门那天下了雨，伞是借的。

# 第二章 转折

这一章讲分岔路口，两个选择各自通向哪里。
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm_src_")
        self.pid = "20260101-000000-book"

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def add(self, name, text, note=""):
        return S.add_source(self.base, self.pid, name, text, note=note)


class TestStore(Base):
    def test_add_returns_row(self):
        row = self.add("书稿.md", BOOK)
        self.assertEqual(row["id"], "s1")
        self.assertEqual(row["name"], "书稿.md")
        self.assertEqual(row["chars"], len(BOOK.strip()))
        # 三处标题：第一章 / 第一节 / 第二节 / 第二章 —— 共 4 个锚点
        self.assertEqual(row["sections"], 4)

    def test_ids_do_not_collide(self):
        self.assertEqual(self.add("甲", BOOK)["id"], "s1")
        self.assertEqual(self.add("乙", BOOK)["id"], "s2")
        self.assertEqual([s["id"] for s in S.list_sources(self.base, self.pid)],
                         ["s1", "s2"])

    def test_empty_text_rejected(self):
        with self.assertRaises(SourceError):
            self.add("空", "   \n  ")

    def test_list_on_fresh_project_is_empty(self):
        self.assertEqual(S.list_sources(self.base, self.pid), [])

    def test_add_without_project_rejected(self):
        with self.assertRaises(SourceError):
            S.add_source(self.base, "", "甲", BOOK)

    def test_body_is_written_to_disk(self):
        self.add("书稿.md", BOOK)
        self.assertTrue(os.path.exists(
            os.path.join(S.source_dir(self.base, self.pid), "s1.md")))
        self.assertEqual(S.read_source(self.base, self.pid, "s1"), BOOK.strip())

    def test_missing_source_raises(self):
        with self.assertRaises(SourceError):
            S.read_source(self.base, self.pid, "s9")

    def test_broken_index_is_not_silently_rebuilt(self):
        self.add("书稿.md", BOOK)
        with open(S.index_path(self.base, self.pid), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        # 索引坏了必须报错：当成"没入库"等于把整本书当成从没给过
        with self.assertRaises(SourceError):
            S.list_sources(self.base, self.pid)

    def test_wrong_index_shape_raises(self):
        self.add("书稿.md", BOOK)
        with open(S.index_path(self.base, self.pid), "w", encoding="utf-8") as f:
            json.dump({"sources": "nope"}, f)
        with self.assertRaises(SourceError):
            S.list_sources(self.base, self.pid)

    def test_find_source(self):
        self.add("书稿.md", BOOK)
        self.assertEqual(S.find_source(self.base, self.pid, "s1")["name"], "书稿.md")
        self.assertIsNone(S.find_source(self.base, self.pid, "s9"))

    def test_remove_clears_index_and_file(self):
        self.add("甲", BOOK)
        self.add("乙", BOOK)
        left = S.remove_source(self.base, self.pid, "s1")
        self.assertEqual([s["id"] for s in left], ["s2"])
        self.assertFalse(os.path.exists(
            os.path.join(S.source_dir(self.base, self.pid), "s1.md")))
        self.assertTrue(os.path.exists(
            os.path.join(S.source_dir(self.base, self.pid), "s2.md")))

    def test_remove_unknown_raises(self):
        self.add("甲", BOOK)
        with self.assertRaises(SourceError):
            S.remove_source(self.base, self.pid, "s9")

    def test_removed_id_is_reused(self):
        self.add("甲", BOOK)
        S.remove_source(self.base, self.pid, "s1")
        # 空出来的编号可以再用：编号只是标识，不留洞更利于人读
        self.assertEqual(self.add("乙", BOOK)["id"], "s1")


class TestSourceMeta(Base):
    def test_row_records_both_char_counts(self):
        # 原始字符数与有效字数是两个量：前者是文件大小，后者才是时长换算用的
        # 那个。从前只记前者，于是同一份素材在库里是 27 万、在写脚本那步是
        # 18 万，两个数对不上账。
        self.add("书稿.md", BOOK)
        row = S.find_source(self.base, self.pid, "s1")
        self.assertEqual(row["chars"], len(BOOK.strip()))
        self.assertLess(row["effective"], row["chars"])

    def test_sections_counted_by_anchors(self):
        self.add("书稿.md", BOOK)
        self.assertEqual(S.find_source(self.base, self.pid, "s1")["sections"], 4)

    def test_marks_are_persisted(self):
        # 样式证据只在读取文件那一刻存在。不落盘的话，第二次读素材就只剩纯
        # 文本，作者声明的标题层级白记一场。
        marks = [{"line": 0, "title": "第一章 起点", "level": 1,
                  "evidence": "style", "confidence": 0.95}]
        S.add_source(self.base, self.pid, "稿.docx", BOOK, marks=marks)
        self.assertEqual(S.marks_of(self.base, self.pid, "s1"), marks)

    def test_remove_clears_probe_result(self):
        from podcast_maker import probe
        self.add("书稿.md", BOOK)
        probe.save(self.base, self.pid, "s1", {"version": 1, "segments": []})
        self.assertTrue(os.path.exists(probe.probe_path(self.base, self.pid, "s1")))
        S.remove_source(self.base, self.pid, "s1")
        self.assertFalse(os.path.exists(probe.probe_path(self.base, self.pid, "s1")))


class TestCompose(Base):
    def test_whole_source(self):
        self.add("书稿.md", BOOK)
        text, meta = S.compose(self.base, self.pid, [{"source": "s1", "anchor": ""}])
        self.assertIn("第一章 起点", text)
        self.assertIn("第二章 转折", text)
        self.assertEqual(meta["chars"], len(text))
        self.assertFalse(meta["truncated"])
        self.assertEqual(meta["refs"][0]["anchor"], "")

    def test_anchor_slices_only_that_section(self):
        self.add("书稿.md", BOOK)
        text, meta = S.compose(
            self.base, self.pid,
            [{"source": "s1", "anchor": "第一节 名字的来历"}])
        self.assertIn("名字不是随手取的", text)
        self.assertNotIn("出门那天下了雨", text)
        self.assertNotIn("这一章讲事情的由来", text)
        self.assertEqual(meta["refs"][0]["anchor"], "第一节 名字的来历")

    def test_multiple_refs_are_joined_and_marked(self):
        self.add("书稿.md", BOOK)
        text, meta = S.compose(self.base, self.pid, [
            {"source": "s1", "anchor": "第一章 起点"},
            {"source": "s1", "anchor": "第二章 转折"}])
        self.assertIn("第一章 起点", text)
        self.assertIn("第二章 转折", text)
        self.assertEqual(len(meta["refs"]), 2)

    def test_truncation_is_recorded(self):
        self.add("书稿.md", BOOK)
        text, meta = S.compose(self.base, self.pid,
                               [{"source": "s1", "anchor": ""}], max_chars=30)
        self.assertEqual(len(text), 30)
        self.assertTrue(meta["truncated"])

    def test_no_truncation_flag_when_under_limit(self):
        self.add("书稿.md", BOOK)
        _text, meta = S.compose(self.base, self.pid,
                                [{"source": "s1", "anchor": ""}], max_chars=99999)
        self.assertFalse(meta["truncated"])

    def test_missing_source_raises(self):
        with self.assertRaises(SourceError):
            S.compose(self.base, self.pid, [{"source": "s9", "anchor": ""}])

    def test_unknown_anchor_raises(self):
        self.add("书稿.md", BOOK)
        # 落点在排图时核对过，到取料这一步再对不上就是硬错，不能悄悄给全文
        with self.assertRaises(Exception):
            S.compose(self.base, self.pid,
                      [{"source": "s1", "anchor": "第三章 不存在"}])

    def test_empty_refs_gives_empty_text(self):
        self.add("书稿.md", BOOK)
        text, meta = S.compose(self.base, self.pid, [])
        self.assertEqual(text, "")
        self.assertEqual(meta["refs"], [])

    def test_ref_without_source_is_skipped(self):
        self.add("书稿.md", BOOK)
        text, meta = S.compose(self.base, self.pid, [{"anchor": "第一章 起点"}])
        self.assertEqual(text, "")
        self.assertEqual(meta["refs"], [])


if __name__ == "__main__":
    unittest.main()
