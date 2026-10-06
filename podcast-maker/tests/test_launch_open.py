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

"""浏览器必须由服务在端口绑定后打开。

缺陷：setup.bat 第 4 步先 `start "" http://...` 弹浏览器，再启动 main.py。
Python 导入模块要 1-2 秒，浏览器先开只会看到「连接不上」；等页面真到了，
页内开屏画面一闪而过——加载画面出现在错误的时间点，盖不住真正的等待。
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*rel):
    with open(os.path.join(_ROOT, *rel), encoding="utf-8") as f:
        return f.read()


class TestBrowserOpensAfterBind(unittest.TestCase):
    def test_setup_bat_does_not_pop_the_browser_first(self):
        bat = _read("setup.bat")
        self.assertNotIn("start \"\" http://", bat,
                         "启动脚本抢先弹浏览器＝用户先看「连接不上」")
        launch = bat[bat.index("main.py --port"):]
        self.assertIn("--open", launch[:launch.index("\n")],
                      "main.py 启动行必须带 --open，浏览器改由服务开")

    def test_main_parser_has_open_flag(self):
        sys.path.insert(0, _ROOT)
        import main
        args = main.build_parser().parse_args(["--open"])
        self.assertTrue(args.open)

    def test_run_server_opens_browser_after_bind(self):
        from podcast_maker import web_ui
        sig = inspect.signature(web_ui.run_server)
        self.assertIn("open_browser", sig.parameters)
        src = inspect.getsource(web_ui.run_server)
        # 顺序铁律：先绑定（ThreadingHTTPServer 构造＝bind+listen），后开浏览器。
        # 反过来就是浏览器又一次开在端口活着之前。
        self.assertLess(src.index("ThreadingHTTPServer("),
                        src.index("webbrowser.open"))

    def test_open_url_uses_loopback_even_when_host_is_wildcard(self):
        """--host 0.0.0.0 是监听地址，浏览器要访问的得是 127.0.0.1。"""
        from podcast_maker import web_ui
        src = inspect.getsource(web_ui.run_server)
        self.assertIn("http://127.0.0.1:%d/", src)


if __name__ == "__main__":
    unittest.main()
