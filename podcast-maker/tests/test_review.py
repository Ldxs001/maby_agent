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

"""前期回顾：由程序拼、不进模型，位置在片头之后。

这一组盯的是**降级**。回顾引用的是上一期，而「上一期」有一堆取不到的情形：
第 1 期、本期不在地图上、上一期没留下段主旨。取不到就整句不粘——留一句
「上期我们聊了……」后面空着，比少说一句难看。

两路数据各有各的坑，两路都盯：

- **期主旨**在地图行（`project_store.map_episodes`）里，**不是** `item["episodes"]`
  ——后者是「已出片的期登记」，只有期号与标题，照它取会永远取不到 gist，
  表现成「回顾从不出现」；
- **段主旨**在上一期的**旁挂规划档**（`layout.plan_file`）里，不是正文那份 json
  （正文是扁平句子数组，出片/时长/字幕都按数组读，不能加字段）。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import layout, pipeline as PL, project_store as P  # noqa: E402
from podcast_maker.config_manager import ConfigManager                # noqa: E402


def _map_row(no, title, gist, chars=100):
    return {"no": no, "title": title, "gist": gist, "points": [],
            "refs": [], "chars": chars}


class TestReviewRows(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm-review-")
        self.p = P.create(self.base, "甲档", "mapped")
        P.save_map(self.base, self.p["id"], "", [
            _map_row("1", "第一期标题", "第一期主旨"),
            _map_row("2", "第二期标题", "第二期主旨"),
        ])
        self.item = P.find(self.base, self.p["id"])
        self.cfg = ConfigManager().data()
        self.cfg["intro_outro.review"] = True

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _sidecar(self, no, topics):
        """写上一期的旁挂规划档（段主旨住在这里）。"""
        root = layout.project_dir(self.base, self.p["id"])
        os.makedirs(layout.script_dir(root), exist_ok=True)
        PL.write_json(layout.plan_file(root, no),
                      {"episode_no": no, "title": "",
                       "segments": [{"no": i + 1, "topic": t, "sections": [i + 1],
                                     "quota": 100}
                                    for i, t in enumerate(topics)]})

    def test_switch_off_means_nothing_is_glued(self):
        """默认关：开关没开时，一次取值都不发生。"""
        self.cfg["intro_outro.review"] = False
        self._sidecar("1", ["甲段主旨", "乙段主旨"])
        self.assertEqual(PL.review_rows(self.base, self.item, "2", self.cfg), [])

    def test_first_episode_has_no_previous_one(self):
        """第 1 期没有上一期——整句不粘，不留半句。"""
        self._sidecar("1", ["甲段主旨"])
        self.assertEqual(PL.review_rows(self.base, self.item, "1", self.cfg), [])

    def test_an_episode_off_the_map_gets_nothing(self):
        """本期不在地图上（脚本页手填期号）时判不出「上一期」是谁，不猜。"""
        self._sidecar("1", ["甲段主旨"])
        self.assertEqual(PL.review_rows(self.base, self.item, "9", self.cfg), [])

    def test_a_full_previous_episode_gets_quoted(self):
        """期主旨与段主旨一起引用进来，「等」由模板写死。"""
        self._sidecar("1", ["甲段主旨", "乙段主旨"])
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"],
                         "上期《第一期标题》聊的是第一期主旨——"
                         "讲了甲段主旨、乙段主旨等。")
        self.assertEqual(rows[0]["speaker"], "B")
        self.assertEqual(rows[0]["emotion"], "承接")

    def test_missing_topics_drop_the_whole_line(self):
        """上一期没留下旁挂（没跑过分段路、或本功能上线前写的期）→ 整句不粘。

        只引期主旨也能凑一句，但那样「讲了…」后面就得空着，或者写成半句——
        两种都比不说这一句难看。
        """
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertEqual(rows, [])

    def test_at_most_three_topics_and_always_with_deng(self):
        """一期三五段全念出来是流水账：只引前三段；「等」一条时也照留。

        条数一变句子形状就变，听感上像两套模板——所以「等」写死在模板里，
        不交给程序按条数裁。
        """
        self._sidecar("1", ["一", "二", "三", "四", "五"])
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertIn("讲了一、二、三等。", rows[0]["text"])
        self.assertNotIn("四", rows[0]["text"])
        self._sidecar("1", ["唯一一条"])
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertIn("讲了唯一一条等。", rows[0]["text"])

    def test_the_gist_comes_from_the_map_not_the_register(self):
        """期主旨取自地图行。

        出片登记行（`record_episode` 写的）只有期号与标题、**没有主旨**。回顾
        若照登记取，会永远缺「上一期主旨」而整句不粘——功能看着没坏，其实一次
        都不会出现。
        """
        P.record_episode(self.base, self.p["id"], "第一期标题", "1")
        self.item = P.find(self.base, self.p["id"])
        self._sidecar("1", ["甲段主旨"])
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertEqual(len(rows), 1, "主旨取自地图行，不取自出片登记")
        self.assertIn("第一期主旨", rows[0]["text"])

    def test_branch_episode_counts_as_the_previous_one(self):
        """支期（2a）按播出顺序排在正期 2 之后：第 3 期的上一期是 2a。"""
        P.save_map(self.base, self.p["id"], "", [
            _map_row("1", "一", "一主旨"),
            _map_row("2", "二", "二主旨"),
            _map_row("2a", "二支", "二支主旨"),
            _map_row("3", "三", "三主旨"),
        ])
        self.item = P.find(self.base, self.p["id"])
        self._sidecar("2a", ["支段主旨"])
        rows = PL.review_rows(self.base, self.item, "3", self.cfg)
        self.assertEqual(len(rows), 1)
        self.assertIn("二支主旨", rows[0]["text"])

    def test_episode_order_is_broadcast_order(self):
        """期号排序按播出顺序，不按字符串：字符串比会把 10 排到 2 前面。"""
        o = PL._episode_order
        self.assertLess(o("2"), o("2a"))
        self.assertLess(o("2a"), o("3"))
        self.assertLess(o("9"), o("10"))

    def test_no_project_means_nothing(self):
        """不挂项目的生成（单集）没有地图，也就没有回顾。"""
        self._sidecar("1", ["甲段主旨"])
        self.assertEqual(PL.review_rows(self.base, None, "2", self.cfg), [])


class TestWritePlanFile(unittest.TestCase):
    """写的那一头。

    读侧测过不等于写侧通：落盘这一节断了，症状同样是「回顾从不出现」，而读侧
    的降级逻辑会把断点掩盖成「这一期本来就没有上一期」——两头一起测才看得出
    是哪一节断了。写侧与读侧同一个模块（`pipeline`），口径对得上才谈得上成对。
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm-plan-")
        self.p = P.create(self.base, "甲档", "mapped")
        P.save_map(self.base, self.p["id"], "", [
            _map_row("1", "第一期标题", "第一期主旨"),
            _map_row("2", "第二期标题", "第二期主旨"),
        ])
        self.item = P.find(self.base, self.p["id"])
        self.root = layout.project_dir(self.base, self.p["id"])
        self.cfg = ConfigManager().data()
        self.cfg["intro_outro.review"] = True

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_segmented_payload_writes_the_sidecar(self):
        """段清单在场就落一份旁挂，内容原样——段主旨是回顾唯一要读的字段。"""
        segs = [{"no": 1, "topic": "甲段主旨", "sections": [1], "quota": 100}]
        wrote = PL.write_plan_file(self.root, "1",
                                   {"title": "第一期标题", "segments": segs})
        self.assertTrue(wrote)
        saved = PL.read_json(layout.plan_file(self.root, "1"), {})
        self.assertEqual(saved["segments"], segs)
        self.assertEqual(saved["episode_no"], "1")

    def test_whole_text_payload_writes_nothing(self):
        """整篇路没有段主旨——不写空壳，免得把「没有」伪装成「有但为空」。"""
        for gen in ({}, {"segments": []}, {"segments": None}, None):
            with self.subTest(gen=gen):
                self.assertFalse(PL.write_plan_file(self.root, "1", gen))
        self.assertFalse(os.path.exists(layout.plan_file(self.root, "1")))

    def test_write_then_read_round_trip(self):
        """写一份第 1 期的旁挂，第 2 期的回顾就得把它念出来。"""
        wrote = PL.write_plan_file(self.root, "1", {
            "title": "第一期标题",
            "segments": [{"no": 1, "topic": "甲段主旨", "sections": [1], "quota": 100},
                         {"no": 2, "topic": "乙段主旨", "sections": [2], "quota": 100}]})
        self.assertTrue(wrote, "这一份就是回顾唯一的取料口")
        rows = PL.review_rows(self.base, self.item, "2", self.cfg)
        self.assertEqual(len(rows), 1)
        self.assertIn("讲了甲段主旨、乙段主旨等。", rows[0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
