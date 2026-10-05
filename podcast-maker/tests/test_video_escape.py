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

"""滤镜路径转义测试。

filtergraph 是两级解析，盘符冒号必须转义两次。
这个坑只转一次时表面看是转义问题，实际会让后半段路径被当成新的参数名，
报错信息也指不到根因，因此用测试把它钉住。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import video_engine as V  # noqa: E402


class TestEscapeLevel(unittest.TestCase):
    def test_colon_double_escaped(self):
        self.assertEqual(V._ff_escape("C:\\Windows\\Fonts"), "C\\\\:/Windows/Fonts")

    def test_backslash_becomes_slash(self):
        self.assertNotIn("\\", V._ff_escape("D:\\a\\b").replace("\\\\", ""))

    def test_posix_path_has_no_colon(self):
        self.assertEqual(V._ff_escape("/usr/share/fonts"), "/usr/share/fonts")

    def test_drive_letter_only_colon_escaped(self):
        out = V._ff_escape("C:/Windows/Fonts")
        self.assertEqual(out.count("\\\\:"), 1)
        self.assertEqual(out, "C\\\\:/Windows/Fonts")


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class TestEscapeActuallyWorks(unittest.TestCase):
    """把转义结果真的喂给 ffmpeg，确认能被解析。"""

    def setUp(self):
        from podcast_maker.audio_engine import ffmpeg_bin
        self.ffmpeg = ffmpeg_bin()
        self.tmp = tempfile.mkdtemp(prefix="pm-esc-")

    def _probe(self, fontsdir_arg):
        fc = ("[0:v]scale=320:180,setsar=1[bg];"
              "[bg]ass=sub.ass%s,format=yuv420p[vout]" % fontsdir_arg)
        cmd = [self.ffmpeg, "-y", "-f", "lavfi", "-t", "0.5",
               "-i", "color=c=black:s=320x180:r=10",
               "-f", "lavfi", "-t", "0.5", "-i", "anullsrc",
               "-filter_complex", fc, "-map", "[vout]", "-map", "1:a",
               "-t", "0.5", "-r", "10", "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "35", "-c:a", "aac", "out.mp4"]
        with open(os.path.join(self.tmp, "sub.ass"), "w", encoding="utf-8") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n"
                    "PlayResX: 320\nPlayResY: 180\n\n"
                    "[V4+ Styles]\n"
                    "Format: Name, Fontname, Fontsize\nStyle: D,Arial,12\n\n"
                    "[Events]\nFormat: Layer, Start, End, Style, Text\n")
        return subprocess.run(cmd, cwd=self.tmp, capture_output=True)

    def test_single_escape_fails(self):
        """只转一层必须失败——证明这个坑真实存在。"""
        fd = os.path.join(self.tmp, "fonts").replace("\\", "/")
        os.makedirs(fd, exist_ok=True)
        r = self._probe(":fontsdir=" + fd.replace(":", "\\:"))
        self.assertNotEqual(r.returncode, 0,
                            "单层转义竟然通过了，说明前提已变，需重新审视实现")

    def test_double_escape_passes(self):
        fd = os.path.join(self.tmp, "fonts").replace("\\", "/")
        os.makedirs(fd, exist_ok=True)
        r = self._probe(":fontsdir=" + V._ff_escape(fd))
        self.assertEqual(r.returncode, 0, (r.stderr or b"").decode("utf-8", "replace")[-500:])


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class TestAssInForeignDir(unittest.TestCase):
    """字幕与成片不在同一目录时必须照常合成。

    pipeline 把 ASS 写在过程目录（work），成片落在音视频目录，而 ffmpeg 的 cwd
    设成成片目录。旧实现只把 basename 交出去，libass 就在成片目录里找字幕，报
    "ass_read_file(sub.ass): fopen failed"。上面那个类把 sub.ass 写在 cwd 里，
    恰好复刻了产生 bug 的假设，所以一直是绿的。
    """

    def setUp(self):
        from podcast_maker.audio_engine import ffmpeg_bin
        self.ffmpeg = ffmpeg_bin()
        self.tmp = tempfile.mkdtemp(prefix="pm-assdir-")
        self.work = os.path.join(self.tmp, "过程", "1")   # 非 ASCII 路径顺带覆盖
        self.out_dir = os.path.join(self.tmp, "音视频")
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        self.ass = os.path.join(self.work, "sub.ass")
        with open(self.ass, "w", encoding="utf-8") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n"
                    "PlayResX: 320\nPlayResY: 180\n\n"
                    "[V4+ Styles]\n"
                    "Format: Name, Fontname, Fontsize\nStyle: D,Arial,12\n\n"
                    "[Events]\nFormat: Layer, Start, End, Style, Text\n"
                    "Dialogue: 0,0:00:00.00,0:00:00.50,D,,字幕\n")

        self.audio = os.path.join(self.tmp, "voice.wav")
        subprocess.run([self.ffmpeg, "-y", "-f", "lavfi", "-t", "0.5",
                        "-i", "anullsrc=r=16000:cl=mono", self.audio],
                       capture_output=True, check=True)

    def _cfg(self):
        return {"video.fps": 10, "video.width": 320, "video.height": 180,
                "video.crf": 40, "video.encoder_preset": "ultrafast",
                "video.bg_dim": 0.0, "animation.mode": "none",
                "bg_color": "0x0F1418"}

    def test_composes_when_ass_lives_elsewhere(self):
        out = os.path.join(self.out_dir, "1.mp4")
        V.compose(self.audio, self.ass, out, self._cfg(), None, 0.5, 320, 180)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 0)

    def test_relative_ass_name_would_have_failed(self):
        """反向钉子：把 ASS 挪走后仍按旧写法喂 ffmpeg，必须失败。

        证明这个用例真的在覆盖那条错误路径，而不是碰巧通过。
        """
        fc = ("[0:v]scale=320:180,setsar=1[bg];"
              "[bg]ass=sub.ass,format=yuv420p[vout]")
        cmd = [self.ffmpeg, "-y", "-f", "lavfi", "-t", "0.5",
               "-i", "color=c=black:s=320x180:r=10",
               "-filter_complex", fc, "-map", "[vout]",
               "-t", "0.5", "-c:v", "libx264", "out.mp4"]
        r = subprocess.run(cmd, cwd=self.out_dir, capture_output=True)
        self.assertNotEqual(r.returncode, 0,
                            "成片目录里没有 sub.ass，相对路径竟然通过了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
