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

"""续跑时「续哪一期」怎么定下来。

产物按类归位之后，一期由「树根 + 期号」才定得下来，所以续跑必须知道期号。
没给的时候可以从锁文件反推，但**只有恰好一期卡在门禁上**才自动定：零期说明没有
可续的东西，多期则说明选择权在人手上。猜错的后果不是报错，是拿着另一期的产物
当这一期的接着做——这种错在产物上看不出来。
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main as cli                                                  # noqa: E402
from podcast_maker import layout, script_engine                     # noqa: E402


class TestBlockedEpisode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pm_resume_")
        self.root = os.path.join(self.tmp, "20260912-121051")
        layout.ensure_dirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _lock(self, no):
        work = layout.tmp_dir(self.root, no)
        os.makedirs(work, exist_ok=True)
        script_engine.write_lock(work, "render", "门禁未过", {}, 3)

    def test_one_lock_is_taken(self):
        self._lock("3")
        self.assertEqual(cli._blocked_episode(self.root, layout, script_engine), "3")

    def test_no_lock_asks_for_episode(self):
        self.assertEqual(cli._blocked_episode(self.root, layout, script_engine), "")

    def test_many_locks_ask_for_episode(self):
        self._lock("2")
        self._lock("3")
        self.assertEqual(cli._blocked_episode(self.root, layout, script_engine), "")

    def test_missing_tmp_dir_is_not_a_guess(self):
        shutil.rmtree(os.path.join(self.root, layout.DIR_TMP))
        self.assertEqual(cli._blocked_episode(self.root, layout, script_engine), "")


if __name__ == "__main__":
    unittest.main()
