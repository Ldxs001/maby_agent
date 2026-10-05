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

"""项目页三件长活（探查 / 排图 / 插入建议）的后台化与互斥。

这三件事都是「模型慢慢算」的活，压在 HTTP 请求里等与脚本生成是同一种病：
浏览器挂着、服务线程占着、中途关页面全白跑，而且看不到中途进展。改走与
脚本/合成同一套 /api/task 轮询之后，这里的规矩要钉住：

1. **入口只起任务**。api_plan_post 的 map / insert_plan、api_source_post 的
   add / scan 立刻回 task_id，不等模型——结果装在 job["result"]，形状与
   同步时代一致。
2. **同项目长活互斥**。探查与排图写的是同一批 <sid>.probe.json（凝缩结果
   落在探查文件里），并行跑会互相踩；起任务前查一把，撞了拒绝并说清原因；
   别的项目不受牵连。
3. **进度是真的**。凝缩 i/N、探查 i/N 由下层函数上报，job 的 stage/progress
   跟着走——不画匀速假进度条。
4. **落库在任务里完成**。排图 job 内 set_map，任务失败时地图保持上一版不动。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import (  # noqa: E402
    planner, pipeline, probe, project_store, source_store, web_ui)


def wait_job(jid, timeout=20):
    """等一个后台任务收尾。任务跑在线程上，不轮询就只能撞运气。"""
    end = time.time() + timeout
    while time.time() < end:
        j = pipeline.get_job(jid)
        if j and j.get("status") != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("任务在 %.0fs 内没结束" % timeout)


class TasksBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm_tasks_")
        self._root = web_ui.ROOT
        web_ui.ROOT = self.tmp
        self.base = os.path.join(self.tmp, "projects")
        os.makedirs(self.base, exist_ok=True)
        item = project_store.create(self.base, "长活试验", "mapped")
        self.pid = item["id"]
        # 互斥查的是「运行中」的任务；清掉别的用例可能留下的，账从零算。
        pipeline.JOBS.clear()

    def tearDown(self):
        web_ui.ROOT = self._root
        shutil.rmtree(self.tmp, ignore_errors=True)
        pipeline.JOBS.clear()


class TestBusyGuard(TasksBase):
    def test_second_long_task_is_refused(self):
        job = pipeline.new_job("map", "排地图 · 占位", project=self.pid)
        r = web_ui.api_plan_post({"action": "map", "project_id": self.pid})
        self.assertFalse(r["ok"])
        self.assertIn("正在跑", r["error"])
        job["status"] = "done"

    def test_probe_and_map_block_each_other(self):
        job = pipeline.new_job("probe", "素材探查", project=self.pid)
        r = web_ui.api_plan_post({"action": "map", "project_id": self.pid})
        self.assertFalse(r["ok"], "探查与排图写同一批探查文件，必须互斥")
        r = web_ui.api_source_post({"action": "scan", "project_id": self.pid})
        self.assertFalse(r["ok"])
        job["status"] = "done"

    def test_other_projects_are_free(self):
        other = project_store.create(self.base, "别的项目", "mapped")
        pipeline.new_job("map", "排地图 · 别的项目", project=other["id"])
        with mock.patch.object(web_ui, "_map_work",
                               lambda body, job=None: {"ok": True}):
            r = web_ui.api_plan_post({"action": "map", "project_id": self.pid})
        self.assertTrue(r["ok"], "互斥只拦同一个项目")
        self.assertIn("task_id", r)


class TestMapTask(TasksBase):
    def test_entry_returns_task_not_the_map(self):
        def work(body, job=None):
            pipeline.job_log(job, "凝缩 1/1：第一章 → 主干")
            return {"ok": True, "warnings": [], "capacity": 60000,
                    "ratio": 5, "planned": 0, "kind": "book",
                    "kind_label": web_ui._kind_label("book"),
                    "project": dict(
                        project_store.find(web_ui._projects_base(), self.pid),
                        map={"episodes": [{"no": "1"}]})}
        with mock.patch.object(web_ui, "_map_work", work):
            r = web_ui.api_plan_post({"action": "map", "project_id": self.pid})
        self.assertTrue(r["ok"])
        self.assertNotIn("warnings", r, "入口不该等模型：结果在任务里")
        j = wait_job(r["task_id"])
        self.assertEqual(j["status"], "done")
        self.assertTrue(any("凝缩 1/1" in m for m in j["log"]), j["log"])
        self.assertEqual(j["result"]["ratio"], 5)

    def test_work_persists_map_and_reports_progress(self):
        res = {"episodes": [{"no": "1", "title": "一期", "gist": "主旨",
                             "points": ["要点"], "refs": []}],
               "warnings": ["w"], "capacity": 60000, "planned": 0,
               "kind": "book", "ratio": 5, "unit_level": 1}
        seen = {}

        def fake_plan_map(*a, **k):
            seen["progress"] = k.get("progress")
            if k.get("progress"):
                k["progress"]("凝缩 2/3", 0.5)
            return res

        with mock.patch.object(planner, "plan_map", fake_plan_map):
            job = pipeline.new_job("map", "试", project=self.pid)
            out = web_ui._map_work({"project_id": self.pid}, job)
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind_label"], web_ui._kind_label("book"))
        self.assertEqual(job["stage"], "凝缩 2/3",
                         "下层上报的进度要落到 job 上，界面才跟得上")
        self.assertAlmostEqual(job["progress"], 0.5)
        # 落库发生在 work 里：任务跑完，地图已经在项目上。
        item = project_store.find(self.base, self.pid)
        eps = project_store.map_episodes(item)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].get("title"), "一期")

    def test_insert_plan_entry_returns_task(self):
        with mock.patch.object(web_ui, "_insert_plan_work",
                               lambda body, job=None: {
                                   "ok": True, "warnings": [],
                                   "suggestion": {"anchor_no": "1",
                                                  "episodes": []}}):
            r = web_ui.api_plan_post({"action": "insert_plan",
                                      "project_id": self.pid,
                                      "source_ids": ["s1"]})
        self.assertTrue(r["ok"])
        j = wait_job(r["task_id"])
        self.assertEqual(j["result"]["suggestion"]["anchor_no"], "1")


class TestProbeTask(TasksBase):
    def _put_source(self, name="书稿", text="# 一\n\n正文内容。\n" * 8):
        return source_store.add_source(self.base, self.pid, name, text)

    def test_entry_returns_task_not_the_sources(self):
        with mock.patch.object(web_ui, "_probe_work",
                               lambda body, job=None: {"ok": True,
                                                       "sources": []}):
            r = web_ui.api_source_post({"action": "scan",
                                        "project_id": self.pid})
        self.assertTrue(r["ok"])
        self.assertNotIn("sources", r, "入口不该等模型：结果在任务里")
        j = wait_job(r["task_id"])
        self.assertEqual(j["status"], "done")

    def test_scan_work_runs_offline_and_reports_progress(self):
        self._put_source()
        with mock.patch.object(web_ui, "make_llm", lambda cfg: None):
            job = pipeline.new_job("probe", "试", project=self.pid)
            out = web_ui._probe_work({"project_id": self.pid,
                                      "action": "scan"}, job)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(len(out["sources"]), 1)
        self.assertTrue(job["stage"].startswith("探查 1/"),
                        "进度要落到 job 上：界面才知道探查跑到第几份")

    def test_add_work_ingests_and_probes_offline(self):
        seen = []
        with mock.patch.object(web_ui, "make_llm", lambda cfg: None):
            job = pipeline.new_job("probe", "试", project=self.pid)
            out = web_ui._probe_work(
                {"project_id": self.pid, "action": "add",
                 "name": "新书", "text": "# 一\n\n正文内容。\n" * 8}, job)
        self.assertTrue(out["ok"])
        self.assertEqual((out["source"] or {}).get("name"), "新书")
        self.assertGreaterEqual((out["probe"] or {}).get("segments", 0), 1,
                                "入库即探查：返回里要带这份素材的结构摘要")


class TestProgressPlumbing(TasksBase):
    def test_job_progress_clamps(self):
        job = pipeline.new_job("map", "试")
        p = web_ui._job_progress(job)
        p("凝缩 1/3", 0.1)
        self.assertEqual(job["stage"], "凝缩 1/3")
        p("超了", 2.0)
        self.assertEqual(job["progress"], 0.98, "进度不许越过收尾那一格")
        p("负的", -1)
        self.assertEqual(job["progress"], 0.0)

    def test_span_maps_local_to_global(self):
        out = []
        p = planner._span(lambda t, f: out.append((t, f)), 0.12, 0.80)
        p("凝缩 1/2", 0.5)
        self.assertAlmostEqual(out[0][1], 0.46)
        self.assertIsNone(planner._span(None, 0.1, 0.9),
                          "没接进度回调的调用方不该被强塞一份")

    def test_scan_project_reports_progress(self):
        self._put_source()
        self._put_source("第二份")
        seen = []
        pr = probe.scan_project(self.base, self.pid, web_ui.CFG.data(),
                                llm=None,
                                progress=lambda t, f: seen.append((t, f)))
        self.assertGreaterEqual(len(seen), 2, "每份素材开工前都要报一次")
        self.assertTrue(all(0.0 <= f <= 1.0 for _, f in seen))
        self.assertIn("sources", pr)

    def _put_source(self, name="书稿", text="# 一\n\n正文内容。\n" * 8):
        return source_store.add_source(self.base, self.pid, name, text)


if __name__ == "__main__":
    unittest.main()
