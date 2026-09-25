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

"""Web 接口回归测试。

钉住的缺陷：`/api/report` 只回校验报告，把它为这一期算好的产物路径表
（`layout.episode_files`）扔掉。前端的产物区（文件链接与成片试听播放器）
吃的就是这张表，而它只有「任务刚合成完」这一条填充路径——刷新页面、重启
服务之后，历史期的成片没有任何试听入口，产物区永远显示「尚无产物」。
报告接口必须把路径表一起吐出来，前端「报告」入口与「刚合成完」共用同一份
渲染，不另写第二份。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from podcast_maker import layout                                    # noqa: E402
from podcast_maker import web_ui                                    # noqa: E402


class _CfgStub:
    """只答 `project.output_dir` 这一问：回答默认值，测试不依赖本机配置。"""

    def get(self, key, default=None):
        return default

    def data(self):
        return {}


class TestApiReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="web_api_test_")
        self._old_root, self._old_cfg = web_ui.ROOT, web_ui.CFG
        web_ui.ROOT = self._tmp
        web_ui.CFG = _CfgStub()
        self.addCleanup(self._restore)

    def _restore(self):
        web_ui.ROOT, web_ui.CFG = self._old_root, self._old_cfg

    def _make_tree(self, root_name, report):
        """伪造一棵项目树，落一份现成的报告文件——读盘命中，不走重建。"""
        tree = os.path.join(self._tmp, "projects", root_name)
        os.makedirs(layout.report_dir(tree), exist_ok=True)
        path = os.path.join(layout.report_dir(tree), "%s.json" % "0001")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)
        return tree

    def test_report_carries_episode_files(self):
        """报告与产物路径表必须一起回——路径表是历史期试听的唯一入口。"""
        marker = {"passed": True, "fails": [], "warns": [], "marker": "on-disk"}
        tree = self._make_tree("proj1", marker)
        r = web_ui.api_report("proj1", "0001")
        self.assertTrue(r["ok"])
        # 报告读自磁盘，原样透传，而不是重建出来的另一份
        self.assertEqual(r["report"], marker)
        # 路径表与 layout.episode_files 同参同源，前端两个入口渲染一份
        self.assertEqual(r["episode_files"], layout.episode_files(tree, "0001"))

    def test_episode_files_point_at_this_episode(self):
        """音频/视频键必须指向这一期的成品——试听播放器的 src 从这里来。"""
        tree = self._make_tree("proj2", {"passed": True, "fails": [], "warns": []})
        ep = web_ui.api_report("proj2", "0001")["episode_files"]
        self.assertTrue(os.path.isabs(ep["audio"]))
        self.assertTrue(os.path.isabs(ep["video"]))
        self.assertEqual(os.path.basename(ep["audio"]), "0001.mp3")
        self.assertEqual(os.path.basename(ep["video"]), "0001.mp4")
        self.assertEqual(ep["audio"], os.path.join(layout.av_dir(tree), "0001.mp3"))
        # 歌词字幕与 SRT 同目录同前缀：喂音频平台的产物要在路径表里有位置
        self.assertEqual(ep["subtitle_lrc"], os.path.join(layout.sub_dir(tree), "0001.lrc"))
        # 整秒 TXT 是第三份字幕：同目录同前缀，产物区据此列出下载链接
        self.assertEqual(ep["subtitle_txt"], os.path.join(layout.sub_dir(tree), "0001.txt"))
        # 洁版 TXT 是第四份：同目录，名字在前缀后加 `_clean`
        self.assertEqual(ep["subtitle_clean_txt"],
                         os.path.join(layout.sub_dir(tree), "0001_clean.txt"))
        # 封面是三尺寸字典，键随表一起回
        self.assertEqual(sorted(ep["cover"].keys()), ["16x9", "1x1", "3x4"])


class TestPickFile(unittest.TestCase):
    """路径点位的「选择…」（`/api/pickfile`）。

    这一条是把本机文件对话框借给浏览器用：服务与浏览器同机，请求跑到
    ThreadingHTTPServer 的工作线程上，所以对话框交给子进程去开（Tk 只保证能在
    主线程里建 root）。测试不真弹框——弹框会挂住跑测试的人；这里只核「命令拼得对、
    入口守得住」。
    """

    def test_the_picker_script_compiles_and_asks_for_a_path(self):
        src = web_ui._PICK_SCRIPT
        compile(src, "<pick>", "exec")          # 语法错会在这里炸
        self.assertIn("askopenfilename", src)
        self.assertIn("filetypes", src)         # 不给过滤表，用户要在几千个文件里翻
        self.assertIn("stdout.write", src)      # 选中的路径得回得来

    def test_pick_kinds_are_usable_and_cover_what_the_config_asks_for(self):
        from podcast_maker.config_manager import PARAM_SPEC
        for name, kinds in web_ui.PICK_KINDS.items():
            self.assertTrue(kinds, "%s 没有过滤表" % name)
            for label, pats in kinds:
                self.assertTrue(label.strip())
                self.assertTrue(pats.split(), "%s 的 %s 没有扩展名" % (name, label))
        asked = {s.get("pick") for s in PARAM_SPEC.values() if s["type"] == "path"}
        self.assertTrue(asked <= set(web_ui.PICK_KINDS),
                        "配置里要的 pick 类型后端没有：%s"
                        % sorted(asked - set(web_ui.PICK_KINDS)))

    def test_the_route_is_registered(self):
        self.assertIn("/api/pickfile", web_ui.ROUTES_POST)

    def test_only_path_points_may_open_the_dialog(self):
        """别处调用一律拒绝——不然任意点位都能弹出一个本机对话框。"""
        r = web_ui.api_pickfile({"key": "script.target_minutes"})
        self.assertFalse(r["ok"])
        self.assertIn("不是路径", r["error"])
        r = web_ui.api_pickfile({"key": "no.such.key"})
        self.assertFalse(r["ok"])

    def test_cancel_is_not_an_error(self):
        """用户把对话框关掉＝什么都没发生，不该回一个错误让前端弹红字。"""
        orig = web_ui.pick_file
        try:
            web_ui.pick_file = lambda title, kind: {"ok": True, "path": "",
                                                    "cancelled": True}
            r = web_ui.api_pickfile({"key": "speaker_indicator.portrait_a"})
            self.assertTrue(r["ok"])
            self.assertTrue(r["cancelled"])
        finally:
            web_ui.pick_file = orig


if __name__ == "__main__":
    unittest.main()
