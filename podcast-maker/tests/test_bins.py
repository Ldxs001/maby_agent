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

"""运行环境定位（``podcast_maker/bins.py``）的验收。

这里钉住的是**四件事**：

1. **查找顺序是契约** —— ① PATH → ② 项目 bin/。这条顺序同时决定「实际跑哪个」
   与「界面报哪个」，两边必须一致；顺序一翻，就会出现「界面说就绪、跑的是另一个」。
2. **探测不撒谎** —— ``state()`` 的 ``ok`` 必须等于两件工具的都找到，
   缺件时那句 message 要指向配置页（用户得知道去哪儿补）。
3. **安装 fail-closed** —— 校验不符就抛错、不留半个 exe 在 bin/ 里。
   宁可装不上，也不许把来路不明的二进制放进去。
4. **四份定位实现已收口** —— 源码钉子：四个模块不许再出现裸
   ``shutil.which("ffmpeg")``（含 aigc_label 那处没封函数的）。
"""

import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from podcast_maker import bins  # noqa: E402

PKG = os.path.dirname(os.path.abspath(bins.__file__))
MODULES = ("audio_engine.py", "tts_engine.py", "video_engine.py",
           "aigc_label.py")


class TestLocateOrder(unittest.TestCase):
    """① PATH → ② 项目 bin/ → ③ None。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._bin = bins.BIN_DIR
        bins.BIN_DIR = self.tmp

    def tearDown(self):
        bins.BIN_DIR = self._bin
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _put(self, name):
        p = os.path.join(self.tmp, name + bins.EXE_SUFFIX)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def test_path_wins_over_bin(self):
        """PATH 里有就用 PATH 的 —— 用户自己装的那份先认。"""
        self._put("ffmpeg")
        with mock.patch.object(bins.shutil, "which",
                               side_effect=lambda n: "C:/fake/" + n):
            self.assertEqual(bins.locate("ffmpeg"), "C:/fake/ffmpeg")

    def test_bin_dir_when_path_misses(self):
        """PATH 里没有才退到项目 bin/。"""
        binned = self._put("ffmpeg")
        with mock.patch.object(bins.shutil, "which", return_value=None):
            self.assertEqual(bins.locate("ffmpeg"), binned)

    def test_none_when_neither(self):
        with mock.patch.object(bins.shutil, "which", return_value=None):
            self.assertIsNone(bins.locate("ffmpeg"))

    def test_bin_dir_lookup_needs_a_real_file(self):
        """bin/ 里没有那个文件时不许瞎认。"""
        with mock.patch.object(bins.shutil, "which", return_value=None):
            self.assertIsNone(bins.locate("ffprobe"))

    def test_source_of(self):
        binned = self._put("ffmpeg")
        self.assertEqual(bins.source_of(binned), "bin")
        self.assertEqual(bins.source_of("C:/other/ffmpeg.exe"), "PATH")
        self.assertEqual(bins.source_of(None), "")


class TestState(unittest.TestCase):

    def test_shape_and_ok_agrees_with_tools(self):
        st = bins.state()
        self.assertEqual(sorted(st["tools"]), sorted(bins.TOOLS))
        for name in bins.TOOLS:
            for k in ("found", "path", "source", "version"):
                self.assertIn(k, st["tools"][name])
        self.assertEqual(st["ok"],
                         all(st["tools"][n]["found"] for n in bins.TOOLS))

    def test_missing_message_points_at_the_config_card(self):
        """缺件那句必须指向配置页那块 —— 用户得知道去哪儿点。"""
        with mock.patch.object(bins, "locate", return_value=None):
            st = bins.state()
        self.assertFalse(st["ok"])
        self.assertIn("运行环境", st["message"])
        for name in bins.TOOLS:
            self.assertIn(name, st["message"])

    def test_version_is_the_first_line(self):
        """版本报的是 ``-version`` 首行，不是空串（界面要拿它给用户看）。"""
        st = bins.state()
        if not st["ok"]:
            self.skipTest("本机没有 ffmpeg，跳过版本断言")
        self.assertTrue(st["tools"]["ffmpeg"]["version"].startswith("ffmpeg version"))

    def test_missing_message_helper(self):
        self.assertIn("ffprobe", bins.missing_message("ffprobe"))


class TestInstallHint(unittest.TestCase):
    """「你也可以自己装」 —— 不能写得像唯一出路。"""

    def test_covers_page_probe_and_self_install(self):
        txt = bins.install_hint()
        self.assertIn("gyan.dev", txt)      # 官方下载页
        self.assertIn("ffprobe", txt)       # 别只拷 ffmpeg.exe
        self.assertIn("自己装", txt)


class TestSourcesContract(unittest.TestCase):
    """下载地址只写在 tools/sources.py 一处，这里钉住 bins 与它的一致性。"""

    def test_urls_are_built_from_the_table(self):
        s = bins._sources()
        self.assertIn(s.FFMPEG_VERSION, s.FFMPEG_ARCHIVE)
        self.assertTrue(s.FFMPEG_ARCHIVE_URL.endswith(s.FFMPEG_ARCHIVE))
        self.assertTrue(s.FFMPEG_ARCHIVE_URL.startswith(s.FFMPEG_BASE))
        self.assertIn(s.FFMPEG_VERSION, s.FFMPEG_SHA256_URL)
        self.assertEqual(set(s.FFMPEG_PAYLOAD), {"ffmpeg", "ffprobe"})

    def test_bins_does_not_hardcode_a_url(self):
        """bins.py 自己不许写死下载域名 —— 地址的唯一出处是源表。

        不能直接扫 ``http://``：Apache 许可证头里就有一个（第一次就是这么
        误报的）。要钉的是「下载地址从哪儿来」，不是「文件里出现过 http」。
        """
        s = bins._sources()
        with open(os.path.join(PKG, "bins.py"), encoding="utf-8") as f:
            txt = f.read()
        self.assertNotIn("npmmirror", txt)
        self.assertNotIn("gyan.dev", txt)
        self.assertNotIn(s.FFMPEG_BASE, txt)

    def test_archive_layout_tail(self):
        """归档里是 ``*/bin/ffmpeg.exe`` —— 取末尾两段，不写死版本前缀。"""
        want = "bin/ffmpeg" + bins.EXE_SUFFIX
        member = "ffmpeg-8.1.3-full_build/bin/ffmpeg" + bins.EXE_SUFFIX
        self.assertEqual("/".join(member.split("/")[-2:]), want)


class TestExtractMember(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archive(self, member_name, body=b"hi"):
        arc = os.path.join(self.tmp, "a.tar.xz")
        with tarfile.open(arc, "w:xz") as tf:
            info = tarfile.TarInfo(member_name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        return arc

    def test_extracts_by_tail_not_by_prefix(self):
        """前缀带版本号也不影响 —— 所以换 ffmpeg 版本时这里不用改。"""
        name = "ffmpeg" + bins.EXE_SUFFIX
        arc = self._archive("ffmpeg-8.1.3-full_build/bin/" + name)
        dst = os.path.join(self.tmp, "out" + bins.EXE_SUFFIX)
        bins._extract_member(arc, "ffmpeg", dst)
        self.assertTrue(os.path.isfile(dst))
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"hi")

    def test_missing_member_raises(self):
        arc = self._archive("ffmpeg-8.1.3-full_build/bin/ffplay" + bins.EXE_SUFFIX)
        with self.assertRaises(bins.BinError):
            bins._extract_member(arc, "ffmpeg",
                                 os.path.join(self.tmp, "out" + bins.EXE_SUFFIX))


class TestInstallFailClosed(unittest.TestCase):
    """校验不符 → 抛错，且 bin/ 里不许留下半个 exe。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._bin = bins.BIN_DIR
        bins.BIN_DIR = self.tmp

    def tearDown(self):
        bins.BIN_DIR = self._bin
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exes(self):
        return [n for n in os.listdir(self.tmp)
                if n.lower().endswith(bins.EXE_SUFFIX or ".exe")]

    def test_sha_mismatch_raises_and_leaves_nothing(self):
        with mock.patch.object(bins, "_download"), \
             mock.patch.object(bins, "_fetch_sha256", return_value="00" * 32), \
             mock.patch.object(bins, "_sha256", return_value="11" * 32):
            with self.assertRaises(bins.BinError) as cm:
                bins.install(lambda m: None)
        self.assertIn("校验不符", str(cm.exception))
        self.assertEqual(self._exes(), [])

    def test_stop_is_honoured(self):
        with mock.patch.object(bins, "_download",
                               side_effect=bins.BinError("已中止，临时文件已丢弃。")):
            with self.assertRaises(bins.BinError):
                bins.install(lambda m: None, should_stop=lambda: True)
        self.assertEqual(self._exes(), [])

    def test_progress_never_goes_backwards(self):
        """进度回调拿到的数只增不减 —— 界面那条永远不会倒退。"""
        seen = []
        with mock.patch.object(bins, "_download"), \
             mock.patch.object(bins, "_fetch_sha256", return_value="00" * 32), \
             mock.patch.object(bins, "_sha256", return_value="11" * 32):
            try:
                bins.install(lambda m: None, on_step=seen.append)
            except bins.BinError:
                pass
        for a, b in zip(seen, seen[1:]):
            self.assertLessEqual(a, b)


class TestNoBareWhich(unittest.TestCase):
    """源码钉子：定位已收口，四个模块不许再出现裸 shutil.which。"""

    def _read(self, fn):
        with open(os.path.join(PKG, fn), encoding="utf-8") as f:
            return f.read()

    def test_no_bare_which_for_ffmpeg_or_ffprobe(self):
        bad = []
        for fn in sorted(os.listdir(PKG)):
            if not fn.endswith(".py") or fn == "bins.py":
                continue
            for i, line in enumerate(self._read(fn).splitlines(), 1):
                if ('shutil.which("ffmpeg")' in line
                        or 'shutil.which("ffprobe")' in line):
                    bad.append("%s:%d" % (fn, i))
        self.assertEqual(bad, [], "这些地方还留着裸 shutil.which：%s" % bad)

    def test_four_modules_route_through_bins(self):
        for fn in MODULES:
            txt = self._read(fn)
            self.assertIn("bins.locate(", txt, "%s 没走 bins.locate" % fn)

    def test_module_errors_still_use_their_own_type(self):
        """各模块仍抛自己的错误类型 —— 调用方按类型接的，不能换成 BinError。"""
        pairs = (("audio_engine.py", "AudioError"),
                 ("tts_engine.py", "TTSError"),
                 ("video_engine.py", "VideoError"),
                 ("aigc_label.py", "LabelError"))
        for fn, err in pairs:
            self.assertIn("%s(bins.missing_message(" % err, self._read(fn),
                          "%s 没把缺件错误包成 %s" % (fn, err))


if __name__ == "__main__":
    unittest.main()
