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

"""料源按范式分流：成稿规划走地图落点，逐期即兴用调用方给的。

判据只有 `pipeline.resolve_material` 一处，这里钉住三件事：

- 成稿规划下调用方递来的素材被**忽略**，而不是"有就覆盖"——地图是本期讲什么
  的唯一来源，让它能被手填顶掉就等于有两个真值来源；
- 缺地图、缺落点、期号不在图里，一律在取料前拒绝，且不产生半份产物；
- 逐期即兴与未定模式不受影响，料源仍由调用方给。
"""

import shutil
import tempfile
import unittest

from podcast_maker import pipeline, project_store, source_store

TEXT_A = "# 第一章\n这是第一章的正文，甲甲甲。\n# 第二章\n这是第二章的正文，乙乙乙。"
HANDWRITTEN = "这段是调用方递来的素材，不该出现在成稿规划的料源里。"


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm_mat_")
        self.logs = []
        self.log = self.logs.append

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def make_project(self, mode):
        return project_store.create(self.base, "试验项目", plan_mode=mode)["id"]

    def add_source(self, pid, name, text):
        return source_store.add_source(self.base, pid, name, text)["id"]

    def find(self, pid):
        item = project_store.find(self.base, pid)
        self.assertIsNotNone(item)
        return item

    def logs_text(self):
        return "\n".join(self.logs)


class TestMappedTakesPlan(Base):
    """成稿规划：料源是地图落点，手填不作数。"""

    def test_plan_wins_over_passed_material(self):
        pid = self.make_project("mapped")
        sid = self.add_source(pid, "s1", TEXT_A)
        project_store.set_map(self.base, pid, [
            {"no": "1", "title": "起点", "points": [],
             "refs": [{"source": sid, "anchor": ""}]}])
        text, row = pipeline.resolve_material(self.base, self.find(pid), "1",
                                              HANDWRITTEN, log=self.log)
        self.assertIn("甲甲甲", text)
        self.assertNotIn("调用方递来的素材", text)
        self.assertEqual(row.get("no"), "1")

    def test_ignored_material_is_logged(self):
        pid = self.make_project("mapped")
        sid = self.add_source(pid, "s1", TEXT_A)
        project_store.set_map(self.base, pid, [
            {"no": "1", "title": "起点", "points": [],
             "refs": [{"source": sid, "anchor": ""}]}])
        pipeline.resolve_material(self.base, self.find(pid), "1",
                                  HANDWRITTEN, log=self.log)
        self.assertIn("已忽略", self.logs_text())

    def test_no_material_no_ignore_log(self):
        pid = self.make_project("mapped")
        sid = self.add_source(pid, "s1", TEXT_A)
        project_store.set_map(self.base, pid, [
            {"no": "1", "title": "起点", "points": [],
             "refs": [{"source": sid, "anchor": ""}]}])
        pipeline.resolve_material(self.base, self.find(pid), "1", "", log=self.log)
        self.assertNotIn("已忽略", self.logs_text())

    def test_only_this_episode_anchor_is_taken(self):
        pid = self.make_project("mapped")
        sid = self.add_source(pid, "s1", TEXT_A)
        project_store.set_map(self.base, pid, [
            {"no": "1", "title": "第一节", "points": [],
             "refs": [{"source": sid, "anchor": "第一章"}]}])
        text, _ = pipeline.resolve_material(self.base, self.find(pid), "1", "",
                                            log=self.log)
        self.assertIn("甲甲甲", text)
        self.assertNotIn("乙乙乙", text)


class TestMappedRefuses(Base):
    """成稿规划的取料前提不成立时，拒绝发生在取料之前。"""

    def test_without_map_refuses(self):
        pid = self.make_project("mapped")
        with self.assertRaises(pipeline.PipelineError) as cm:
            pipeline.resolve_material(self.base, self.find(pid), "1", HANDWRITTEN,
                                      log=self.log)
        self.assertIn("排", str(cm.exception))

    def test_episode_without_refs_refuses(self):
        pid = self.make_project("mapped")
        project_store.set_map(self.base, pid, [
            {"no": "1", "title": "空期", "points": [], "refs": []}])
        with self.assertRaises(pipeline.PipelineError) as cm:
            pipeline.resolve_material(self.base, self.find(pid), "1", "",
                                      log=self.log)
        self.assertIn("落点", str(cm.exception))

    def test_episode_not_in_map_refuses(self):
        pid = self.make_project("mapped")
        sid = self.add_source(pid, "s1", TEXT_A)
        project_store.set_map(self.base, pid, [
            {"no": "1", "title": "起点", "points": [],
             "refs": [{"source": sid, "anchor": ""}]}])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.resolve_material(self.base, self.find(pid), "7", "",
                                      log=self.log)

    def test_refusal_does_not_consult_passed_material(self):
        """递了素材也照样拒——拒的是"没有地图"，不是"没有素材"。"""
        pid = self.make_project("mapped")
        with self.assertRaises(pipeline.PipelineError):
            pipeline.resolve_material(self.base, self.find(pid), "1", HANDWRITTEN,
                                      log=self.log)


class TestPassedMaterialWins(Base):
    """逐期即兴与未定模式：料源由调用方给，这一层不改口径。"""

    def test_episodic_uses_passed(self):
        pid = self.make_project("episodic")
        text, row = pipeline.resolve_material(self.base, self.find(pid), "1",
                                              HANDWRITTEN, log=self.log)
        self.assertEqual(text, HANDWRITTEN)
        self.assertIsNone(row)

    def test_episodic_without_map_does_not_raise(self):
        pid = self.make_project("episodic")
        text, _ = pipeline.resolve_material(self.base, self.find(pid), "1",
                                            HANDWRITTEN, log=self.log)
        self.assertEqual(text, HANDWRITTEN)

    def test_legacy_uses_passed(self):
        pid = self.make_project("episodic")
        item = self.find(pid)
        item.pop("plan_mode", None)          # 本功能上线前立的项目没有这个字段
        text, _ = pipeline.resolve_material(self.base, item, "1", HANDWRITTEN,
                                            log=self.log)
        self.assertEqual(text, HANDWRITTEN)

    def test_single_uses_passed(self):
        # 单集也是一个项目，但它的料源同样是当场给的：它不排图，也就没有落点
        # 可取。判据与逐期即兴共用一条（非 mapped 就算当场给），不另开分支。
        pid = self.make_project("single")
        text, row = pipeline.resolve_material(self.base, self.find(pid), "1",
                                              HANDWRITTEN, log=self.log)
        self.assertEqual(text, HANDWRITTEN)
        self.assertIsNone(row)

    def test_passed_material_is_not_logged_as_ignored(self):
        pid = self.make_project("episodic")
        pipeline.resolve_material(self.base, self.find(pid), "1", HANDWRITTEN,
                                  log=self.log)
        self.assertNotIn("已忽略", self.logs_text())


class TestNoProject(Base):
    """单集模式：没有项目，料源就是调用方给的，且没有落点。"""

    def test_none_project_passes_through(self):
        text, row = pipeline.resolve_material(self.base, None, "1", HANDWRITTEN,
                                              log=self.log)
        self.assertEqual(text, HANDWRITTEN)
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
