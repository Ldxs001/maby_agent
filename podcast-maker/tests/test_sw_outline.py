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

"""structured-writer 配套规划（.outline.json）的接入。

开关打开＝规划是权威：主旨读文件、零模型调用；规划缺失、对不上、同名
歧义、主旨为空，一律报错停下——静默退回模型重猜等于假功能。开关关闭＝
行为面零变化。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import probe as P, source_store as SS          # noqa: E402

SIDE = {
    "kind": "structured-writer-outline", "version": 1,
    "sections": [
        {"title": "第一章 启程", "gist": "主角离乡南下，途中遇险。",
         "chars": 3000,
         "subs": [
             {"title": "雨夜出发", "gist": "主角冒雨离开村子，带走上路的盘缠。",
              "chars": 1500},
             {"title": "渡口遇险", "gist": "渡船遇风浪，主角救下同船的孩子。",
              "chars": 1400},
         ]},
        {"title": "第二章 落脚", "gist": "主角在县城安顿，寻到营生。",
         "chars": 2800,
         "subs": [
             {"title": "客栈安身", "gist": "主角住进客栈，结识掌柜。", "chars": 1300},
             {"title": "寻得营生", "gist": "主角在码头找了份脚力活。", "chars": 1400},
         ]},
    ],
}

MD = """# 测试书名

## 第一章 启程

### 雨夜出发

他背上包袱冒着雨出了村，泥路一直通向南边的渡口。雨点砸在斗笠上，声音密得像鼓。

### 渡口遇险

渡船行到江心起了风浪，他把船家孩子推上岸，自己呛了两口水才被人拉住。

## 第二章 落脚

### 客栈安身

县城的客栈一晚二十文，他数了数盘缠，要了最靠楼梯的那间。

### 寻得营生

码头上扛包一天三十文，管事看他腿脚利索，留了他。
"""


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = tempfile.mkdtemp(prefix="pm_sw_")
        self.pid = "p1"
        SS.add_source(self.base, self.pid, "测试书名.md", MD,
                      sidecar=json.dumps(SIDE, ensure_ascii=False))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def _irs(self):
        # 不带 llm：纯 L1 机械扫描就该出结构；SW 主旨不靠模型。
        return P.scan_project(self.base, self.pid, {},
                              llm=None, paradigms_map={})


class TestSidecarStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = tempfile.mkdtemp(prefix="pm_sw_")
        self.pid = "p1"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def test_add_source_stores_sidecar_and_marks_row(self):
        row = SS.add_source(self.base, self.pid, "x.md", "正文",
                            sidecar=json.dumps(SIDE, ensure_ascii=False))
        self.assertTrue(row["sw"])
        self.assertTrue(os.path.exists(
            SS.sidecar_path_of(self.base, self.pid, row["id"])))

    def test_add_source_without_sidecar_is_unchanged(self):
        row = SS.add_source(self.base, self.pid, "x.md", "正文")
        self.assertFalse(row["sw"])
        self.assertFalse(os.path.exists(
            SS.sidecar_path_of(self.base, self.pid, row["id"])))


class TestCondenseUnitsSw(_Base):
    def _units(self):
        irs = self._irs()
        units = []
        for sid, ir in irs["sources"].items():
            # llm=None 时 unit_level 没人判，钉成章级（与小说卡一致）：
            # 章 ~110 有效字装不下 50 的配额，单元下钻到子结构层。
            ir["unit_level"] = 2
            for s in P.pick_units(ir, 50):
                units.append((sid, s))
        return irs, units

    def test_fills_gist_with_zero_llm(self):
        irs, units = self._units()
        self.assertTrue(units)
        filled = P.condense_units_sw(self.base, self.pid, units, {},
                                     log=lambda m: None)
        self.assertEqual(filled, len(units))
        for sid, s in units:
            self.assertTrue(s.get("gist"), s["title"])
            self.assertEqual(s["condense_version"], P.SW_CONDENSE_VERSION)
            self.assertTrue(s.get("loc"), s["title"])
        # 子结构的主旨来自规划文件，不是模型：逐条对上规划原文。
        gists = {s["gist"]: s["title"] for _, s in units}
        self.assertIn("主角冒雨离开村子，带走上路的盘缠。", gists)

    def test_missing_sidecar_raises(self):
        SS.add_source(self.base, self.pid, "另一本.md", "# 另一本\n\n正文")
        irs = P.scan_project(self.base, self.pid, {}, llm=None,
                             paradigms_map={})
        units = [(sid, s) for sid, ir in irs["sources"].items()
                 for s in P.pick_units(ir, 20000)]
        with self.assertRaises(P.ProbeError) as cm:
            P.condense_units_sw(self.base, self.pid, units, {})
        self.assertIn("配套规划", str(cm.exception))

    def test_unmatched_title_raises(self):
        bad = json.loads(json.dumps(SIDE, ensure_ascii=False))
        bad["sections"][0]["subs"][0]["title"] = "规划里叫别的"
        sid = SS.list_sources(self.base, self.pid)[0]["id"]
        with open(SS.sidecar_path_of(self.base, self.pid, sid),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps(bad, ensure_ascii=False))
        irs, units = self._units()
        with self.assertRaises(P.ProbeError) as cm:
            P.condense_units_sw(self.base, self.pid, units, {})
        self.assertIn("对不上", str(cm.exception))

    def test_unknown_kind_raises(self):
        sid = SS.list_sources(self.base, self.pid)[0]["id"]
        bad = dict(SIDE, kind="别的格式")
        with open(SS.sidecar_path_of(self.base, self.pid, sid),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps(bad, ensure_ascii=False))
        irs, units = self._units()
        with self.assertRaises(P.ProbeError):
            P.condense_units_sw(self.base, self.pid, units, {})

    def test_empty_gist_raises(self):
        bad = json.loads(json.dumps(SIDE, ensure_ascii=False))
        bad["sections"][0]["subs"][0]["gist"] = ""
        sid = SS.list_sources(self.base, self.pid)[0]["id"]
        with open(SS.sidecar_path_of(self.base, self.pid, sid),
                  "w", encoding="utf-8") as f:
            f.write(json.dumps(bad, ensure_ascii=False))
        irs, units = self._units()
        with self.assertRaises(P.ProbeError) as cm:
            P.condense_units_sw(self.base, self.pid, units, {})
        self.assertIn("没有主旨", str(cm.exception))

    def test_stamp_condense_records_sw_source(self):
        irs, units = self._units()
        P.condense_units_sw(self.base, self.pid, units, {})
        for sid, ir in irs["sources"].items():
            stamped = P.stamp_condense(ir)
            self.assertEqual(stamped["condense_version"],
                             P.SW_CONDENSE_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
