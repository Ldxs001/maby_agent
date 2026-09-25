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

"""启动抬头只许有一份。

`setup.bat` 起来以后，控制台原先是这么两段挨着出现的：`main.py` 印一份
**不带版本号**的，紧接着 `web_ui.run_server()` 又印一份**带版本号**的，
同一个抬头隔一行连着出现两次，白占四行屏幕（中间其实什么都没打印）。

判据两条：抬头文案**被打印**的地方全仓只有一处；印它的那一行**带 `VERSION`**。
第二条不是洁癖 —— 写死版本号的抬头改版本时必漏，v0.6.0 那次就是这么错的
（日志先跑到 `## v0.6.0`，程序报的还是 0.5.0）。

判据第一条为什么不数「文案出现过几次」：测试与探针都要拿这句文案当标尺
（`TAGLINE = "…"`、对账断言），**出现在常量或文档里不算数**。要钉的是
「谁把它打到屏幕上」，所以只认落在 `print` / `log` / `sys.stderr.write` 调用里的。
这条是实测逼出来的：第一版按「出现过」判，`tools/probes/startup_banner_probe.py`
一进仓就把它判红了 —— 探针只是引用标尺，没打印任何东西。
"""

import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 抬头里那句产品说明。改文案时这里跟着改，不然钉不住。
TAGLINE = "播客制作智能体 · 脚本 → 声音 → 字幕 → 画面 → 产物"

#: 算「打到屏幕上」的那几种调用。
_PRINT_CALL = re.compile(r"^\s*(?:print|log|sys\.std(?:err|out)\.write)\s*\(")

#: 不算第一方源码的目录：探针归档、第三方环境、权重。
_SKIP_DIRS = {".git", "__pycache__", "_smoke", ".venv", "models", "node_modules",
              "build", "dist", ".pytest_cache", ".mypy_cache"}


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _printed_taglines(rel):
    """这个文件里把抬头文案**打出去**的行（引用标尺不算）。"""
    return [ln for ln in _read(rel).splitlines()
            if TAGLINE in ln and _PRINT_CALL.match(ln)]


def _python_sources():
    """仓内第一方 Python 源（相对路径，排序后返回）。"""
    out = []
    for root, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in sorted(files):
            if fn.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, fn), _ROOT))
    return sorted(out)


class TestStartupBannerIsWrittenOnce(unittest.TestCase):
    def test_the_tagline_is_printed_exactly_once(self):
        """抬头文案**打出去**的次数全仓只有 1 次 —— 两次就是用户屏幕上看见两次。"""
        hits = []
        for rel in _python_sources():
            n = len(_printed_taglines(rel))
            if n:
                hits.append("%s ×%d" % (rel.replace(os.sep, "/"), n))
        self.assertEqual(hits, ["podcast_maker/web_ui.py ×1"],
                         "抬头该只在 web_ui.py 里打一次，实际：%s" % (hits or "一处都没有"))

    def test_main_does_not_print_a_second_banner(self):
        """入口不许自己再画一条分隔线抬头 —— 那份不带版本号，还紧挨着一份。"""
        self.assertNotIn('"=" * 62', _read("main.py"))

    def test_the_launcher_bat_does_not_print_the_product_banner(self):
        """`setup.bat` 的抬头是启动器自己的（英文一行 + 四步进度），不重复产品抬头。"""
        for rel in ("setup.bat", "_run.bat"):
            self.assertNotIn(TAGLINE, _read(rel))

    def test_the_banner_carries_the_live_version(self):
        """印抬头那一行必须用 `VERSION`，不许把版本号写死在文案里。"""
        lines = [ln for ln in _read(os.path.join("podcast_maker", "web_ui.py")).splitlines()
                 if "Podcast Maker" in ln]
        self.assertEqual(len(lines), 1, "抬头那行该正好一条，实际 %d 条" % len(lines))
        self.assertIn("VERSION", lines[0])
        self.assertIsNone(re.search(r"\d+\.\d+", lines[0]),
                          "抬头里不许出现版本号字面量：%s" % lines[0].strip())

    def test_version_literals_live_in_exactly_two_files(self):
        """版本号字面量只有 `config_manager` 与 `__init__` 两处（三端齐步靠它）。

        怕的是有人图省事在抬头里再抄一个版本号 —— 那份从此再也跟不上 bump。
        """
        defined = []
        for rel in _python_sources():
            if re.search(r'^\s*(__version__|VERSION)\s*=\s*"', _read(rel), re.M):
                defined.append(rel.replace(os.sep, "/"))
        self.assertEqual(defined, ["podcast_maker/__init__.py",
                                   "podcast_maker/config_manager.py"])


if __name__ == "__main__":
    unittest.main()
