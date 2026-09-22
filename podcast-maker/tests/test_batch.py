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

"""批量跑期的规矩，以及批量合成取的是哪一份脚本。

批量的价值在「不用守着」，所以它出错的方式必须是可预期的：

1. **串行**。语音合成与视频渲染吃满机器，两个一起跑只会互相拖慢，还可能
   爆显存。这里钉住「一期跑完才开下一期」，顺序也不许打乱。
2. **单期失败跳过**。二期挂了不该把三到八期一起赔进去——那些期跟它没关系。
3. **连续同因失败到阈值就停**。失败若是系统性的（模型后端挂了、合成服务没
   起），跳过等于把同一个错误重复 N 遍：几小时白等，机器还一直占着。所以
   熔断按「同一个原因连续几期」计，不是按总失败数。
4. **中止是软的**。只置标记、当前这一步做完就停，不硬杀线程——硬杀会把正
   写着的文件截断，半截的清单比没有更坏，它看着像做完了。
5. **批量合成合成的是定稿那一份**。读盘上落盘的脚本，读不到就报错，绝不
   顺手现生成：现场生成等于把审过的稿换成模型新写的另一份。
"""

import contextlib
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

from podcast_maker import layout, pipeline, project_store, tts_engine, web_ui  # noqa: E402


def wait_job(jid, timeout=20):
    """等一个批任务收尾。批任务跑在后台线程上，不轮询就只能撞运气。"""
    end = time.time() + timeout
    while time.time() < end:
        j = pipeline.get_job(jid)
        if j and j.get("status") != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("批任务在 %.0fs 内没结束" % timeout)


class BatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm_batch_")
        self._root = web_ui.ROOT
        web_ui.ROOT = self.tmp
        self.base = os.path.join(self.tmp, "projects")
        os.makedirs(self.base, exist_ok=True)
        item = project_store.create(self.base, "批任务试验", "episodic")
        self.pid = item["id"]
        self.root = layout.project_dir(self.base, self.pid)

    def tearDown(self):
        web_ui.ROOT = self._root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put_script(self, no, lines=3):
        pipeline.write_json(layout.script_file(self.root, no),
                            [{"speaker": "A", "text": "第 %s 期第 %d 句" % (no, i)}
                             for i in range(1, lines + 1)])

    def put_scripts(self, nos):
        for no in nos:
            self.put_script(no)

    @contextlib.contextmanager
    def patched_render(self, fn):
        """替换渲染函数，顺手把本地语音服务的起停摘掉。

        批任务开工前会先拉起本地 TTS 服务（整批共用一次起停，省去逐期重复加载
        权重）。那一步跟这里要验的东西——循环的串行、跳过、熔断、软中止——没有
        关系，却要看这台机器上装没装那套几个 GB 的依赖：真起得来服务的机器上
        用例才过，等于把测试的成败挂在机器环境上。这里一并摘掉，只留循环本身。
        """
        with mock.patch.object(web_ui, "_render_sync", fn), \
                mock.patch.object(tts_engine, "acquire_service",
                                  lambda *a, **k: None), \
                mock.patch.object(tts_engine, "release_service",
                                  lambda *a, **k: None):
            yield


class TestBatchLoop(BatchBase):
    """批任务的循环规矩：串行、跳过、熔断、软中止。"""

    def test_progress_restarts_at_each_episode(self):
        """批任务里的进度是**本期**的，切到新一期必须归零。

        界面按 (第几期-1+本期进度)/总期数 折算整批进度，而进度口一律只增不减
        （见 pipeline._step）。不归零的话，第二期一开局就顶着上一期跑完时的高
        位——整期看着不动，反倒像卡住了。
        """
        self.put_scripts(["1", "2", "3"])
        seen = []

        def fake(body, job=None):
            seen.append((str(body.get("episode_no")), job.get("progress")))
            job["progress"] = 0.9                 # 模拟这一期跑到九成
            return {"ok": True, "episode_files": {}}

        with self.patched_render(fake):
            r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                                  "episodes": ["1", "2", "3"]})
            self.assertTrue(r.get("ok"), r)
            wait_job(r["task_id"])

        self.assertEqual([no for no, _ in seen], ["1", "2", "3"])
        self.assertEqual([p for _, p in seen], [0.0, 0.0, 0.0],
                         "每期开头都要把本期进度清零：%r" % (seen,))

    def test_serial_and_skip_failed(self):
        self.put_scripts(["1", "2", "3"])
        calls = []

        def fake(body, job=None):
            no = str(body.get("episode_no"))
            calls.append(no)
            if no == "2":
                raise RuntimeError("合成服务没起")
            return {"ok": True, "episode_files": {}}

        with self.patched_render(fake):
            r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                                  "episodes": ["1", "2", "3"]})
            self.assertTrue(r.get("ok"), r)
            self.assertEqual(r.get("total"), 3)
            j = wait_job(r["task_id"])

        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["done"], ["1", "3"])
        self.assertEqual([f["no"] for f in j["result"]["failed"]], ["2"])
        self.assertIn("合成服务没起", j["result"]["failed"][0]["error"])
        # 串行：调用顺序就是勾选顺序，没有并发插入。
        self.assertEqual(calls, ["1", "2", "3"])

    def test_same_reason_streak_fuses(self):
        self.put_scripts(["1", "2", "3", "4", "5"])
        calls = []

        def boom(body, job=None):
            calls.append(str(body.get("episode_no")))
            raise RuntimeError("模型后端挂了")

        with self.patched_render(boom):
            r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                                  "episodes": ["1", "2", "3", "4", "5"]})
            j = wait_job(r["task_id"])

        # 同一个原因连着栽跟头，跑到阈值就停，后面的期不再白跑。
        self.assertEqual(calls, ["1", "2", "3"])
        self.assertEqual(j["result"]["done"], [])
        self.assertEqual(len(j["result"]["failed"]), 3)
        self.assertEqual(j["result"]["requested"], 5)
        self.assertTrue(any("连续 3 期" in m for m in j["log"]), j["log"][-3:])

    def test_different_reason_resets_streak(self):
        """失败原因各不相同＝不是系统性问题，要跑到底。"""
        self.put_scripts(["1", "2", "3", "4", "5"])
        calls = []

        def flaky(body, job=None):
            no = str(body.get("episode_no"))
            calls.append(no)
            raise RuntimeError("第 %s 期自己出问题" % no)

        with self.patched_render(flaky):
            r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                                  "episodes": ["1", "2", "3", "4", "5"]})
            j = wait_job(r["task_id"])

        self.assertEqual(calls, ["1", "2", "3", "4", "5"])
        self.assertEqual(len(j["result"]["failed"]), 5)

    def test_stop_flag_breaks_after_current(self):
        self.put_scripts(["1", "2", "3"])
        calls = []

        def stopper(body, job=None):
            no = str(body.get("episode_no"))
            calls.append(no)
            job["stop"] = True          # 模拟界面点了「中止」
            return {"ok": True}

        with self.patched_render(stopper):
            r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                                  "episodes": ["1", "2", "3"]})
            j = wait_job(r["task_id"])

        # 当前这一步做完就停：第 1 期记进账，2、3 期不碰。
        self.assertEqual(calls, ["1"])
        self.assertEqual(j["result"]["done"], ["1"])
        self.assertTrue(any("已中止" in m for m in j["log"]), j["log"])

    def test_script_kind_runs_generator(self):
        calls = []

        def fake_gen(body, job=None):
            no = str(body.get("episode_no"))
            calls.append(no)
            return {"ok": True, "logs": ["第 %s 期：已生成" % no]}

        with mock.patch.object(web_ui, "_script_generate_work", fake_gen):
            r = web_ui.api_batch({"kind": "script", "project_id": self.pid,
                                  "episodes": ["1", "2"]})
            j = wait_job(r["task_id"])

        self.assertEqual(calls, ["1", "2"])
        self.assertEqual(j["result"]["kind"], "script")
        self.assertEqual(j["result"]["done"], ["1", "2"])

    def test_script_generator_failure_skips(self):
        calls = []

        def half(body, job=None):
            no = str(body.get("episode_no"))
            calls.append(no)
            if no == "1":
                return {"ok": False, "error": "模型没答上来"}
            return {"ok": True, "logs": []}

        with mock.patch.object(web_ui, "_script_generate_work", half):
            r = web_ui.api_batch({"kind": "script", "project_id": self.pid,
                                  "episodes": ["1", "2"]})
            j = wait_job(r["task_id"])

        self.assertEqual(calls, ["1", "2"])
        self.assertEqual(j["result"]["done"], ["2"])
        self.assertEqual([f["no"] for f in j["result"]["failed"]], ["1"])


class TestBatchGates(BatchBase):
    """接口层的门禁：绕过界面直调也得拦住，且不留下半个任务。"""

    def test_render_without_script_refused(self):
        self.put_scripts(["1"])
        r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                              "episodes": ["1", "9"]})
        self.assertFalse(r.get("ok"))
        self.assertIn("9", r.get("error") or "")
        self.assertNotIn("task_id", r)

    def test_no_episodes_refused(self):
        r = web_ui.api_batch({"kind": "render", "project_id": self.pid,
                              "episodes": []})
        self.assertFalse(r.get("ok"))
        self.assertNotIn("task_id", r)

    def test_unknown_project_refused(self):
        r = web_ui.api_batch({"kind": "render", "project_id": "没这个项目",
                              "episodes": ["1"]})
        self.assertFalse(r.get("ok"))
        self.assertNotIn("task_id", r)

    def test_script_kind_allows_missing_script(self):
        """生成脚本是「把没脚本的期补上」，不能反过来要求先有脚本。"""
        with mock.patch.object(web_ui, "_script_generate_work",
                               lambda body, job=None: {"ok": True, "logs": []}):
            r = web_ui.api_batch({"kind": "script", "project_id": self.pid,
                                  "episodes": ["1"]})
            j = wait_job(r["task_id"])
        self.assertTrue(r.get("ok"))
        self.assertEqual(j["result"]["done"], ["1"])


class TestRenderSyncUsesStoredScript(BatchBase):
    """批量合成读的是落盘定稿那一份，不是现场新生成的一份。"""

    def test_reads_script_from_disk(self):
        self.put_script("2", lines=5)
        want = pipeline.read_json(layout.script_file(self.root, "2"), None)
        seen = {}

        def fake_run(cfg, calib, material, title, **kw):
            seen.update(kw)
            return {"ok": True}

        with mock.patch.object(web_ui.audio_engine, "validate_params",
                               lambda cfg: ({}, [])), \
                mock.patch.object(web_ui.pipeline, "run_episode", fake_run), \
                mock.patch.object(web_ui, "make_llm",
                                  lambda cfg: self.fail("不该去建 LLM：脚本已在盘上")):
            web_ui._render_sync({"project_id": self.pid, "episode_no": "2"})

        self.assertEqual(seen.get("script"), want)
        self.assertEqual(seen.get("episode_no"), "2")
        self.assertIsNone(seen.get("llm"), "有定稿脚本时不该带 LLM 进场")

    def test_missing_script_names_the_episode(self):
        with mock.patch.object(web_ui.audio_engine, "validate_params",
                               lambda cfg: ({}, [])):
            with self.assertRaises(RuntimeError) as ctx:
                web_ui._render_sync({"project_id": self.pid, "episode_no": "7"})
        self.assertIn("7", str(ctx.exception))
        self.assertIn("脚本", str(ctx.exception))


class TestScriptJobEntry(BatchBase):
    """单期写脚本也走后台任务。

    它从前是同步接口：浏览器发一个请求，服务端线程一直挂着等模型把整期写完
    （本地模型十几分钟到半小时）。中途刷新、关页面、服务重启就是全白跑，
    而且一路上界面看不到任何进展——所有日志都要等返回值一起到。

    合成早就这么办了，脚本没道理例外；而且两处共用同一套 /api/task 轮询与
    中止，不另造一套。
    """

    def test_entry_returns_a_task_not_the_script(self):
        def work(body, job=None):
            if job is not None:
                pipeline.job_log(job, "第 1 轮：模型返回 12 字符")
            return {"ok": True, "script": [{"speaker": "A", "text": "稿"}]}

        with mock.patch.object(web_ui, "_script_generate_work", work):
            r = web_ui.api_script_generate({"project_id": self.pid,
                                            "episode_no": "1"})
            self.assertTrue(r.get("ok"))
            self.assertNotIn("script", r, "请求里不该等稿子：它要跑十几分钟")
            j = wait_job(r["task_id"])

        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"]["script"][0]["text"], "稿")
        self.assertTrue(any("第 1 轮" in m for m in j["log"]), j["log"])

    def test_stage_and_progress_follow_the_round(self):
        """阶段标签与进度从日志行里长出来，界面才知道现在跑到哪了。

        三段串行：出货 → 检查 → 门禁。后两段各跑各的轮，**检查段占 0~50%、
        门禁段占 50~100%**；日志行自带段名（「检查第 N 轮」「门禁第 N 轮」），
        界面按段折算——两段都喊「第 N 轮」的话，进度条会在门禁段开头往回跳一半。
        出货段只改阶段：它没有轮次表，跑多久也不该假装进度条在动。
        """
        job = pipeline.new_job("script", "试")
        web_ui._script_stage(job, "目标：1500 秒 / 约 6024 字")
        self.assertEqual(job["stage"], "排队", "不是轮次行就不该改阶段")
        self.assertEqual(job["progress"], 0.0)
        web_ui._script_stage(job, "生成第 1 次：调用模型…")
        self.assertEqual(job["stage"], "生成第 1 次：调用模型…",
                         "一轮要跑十几分钟，这期间界面不能停在上一句话上")
        self.assertEqual(job["progress"], 0.0, "出货还没跑完，进度不该动")
        web_ui._script_stage(job, "检查第 1 轮：模型返回正文 18466 有效字")
        first = job["progress"]
        self.assertGreater(first, 0.0)
        self.assertLessEqual(first, 0.5, "检查段最多走到一半")
        web_ui._script_stage(job, "检查通过（第 1 轮）")
        self.assertGreaterEqual(job["progress"], 0.5,
                                "检查段提前收工，进度也得交到它的终点，"
                                "门禁段才有 50% 这个起点")
        web_ui._script_stage(job, "门禁第 1 轮：模型返回正文 18466 有效字")
        self.assertGreater(job["progress"], first, "门禁段接着走 50~100%")
        self.assertGreaterEqual(job["progress"], 0.5)
        before = job["progress"]
        web_ui._script_stage(job, "门禁第 1 轮：定点修补 2 句…")
        self.assertEqual(job["progress"], before, "进度条只许前进，不许倒回")
        web_ui._script_stage(job, "门禁通过（第 2 轮）")
        self.assertGreater(job["progress"], before, "门禁过了就等于定稿，推到尾")
        self.assertLess(job["progress"], 1.0, "留一格给粘合与写盘")


    def test_batch_calls_the_work_function_not_the_entry(self):
        """批任务自己就是一个任务，里面再起一个，日志会分叉成两处。"""
        seen = []

        def work(body, job=None):
            seen.append(job is not None)
            return {"ok": True, "logs": []}

        with mock.patch.object(web_ui, "_script_generate_work", work):
            r = web_ui.api_batch({"kind": "script", "project_id": self.pid,
                                  "episodes": ["1", "2"]})
            j = wait_job(r["task_id"])

        self.assertEqual(seen, [True, True], "批任务要把自己的任务对象递进去")
        self.assertEqual(j["result"]["done"], ["1", "2"])

    def test_job_failure_keeps_the_reason(self):
        """干活函数抛异常时，任务要记下起因而不是变成「请求失败」。"""
        def boom(body, job=None):
            raise RuntimeError("后端连不上")

        with mock.patch.object(web_ui, "_script_generate_work", boom):
            r = web_ui.api_script_generate({"project_id": self.pid,
                                            "episode_no": "1"})
            j = wait_job(r["task_id"])

        self.assertEqual(j["status"], "failed")
        self.assertIn("后端连不上", j["error"])


if __name__ == "__main__":
    unittest.main()
