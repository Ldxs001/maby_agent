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

"""AIGC 合规标识测试：元数据七要素、图片块插入、ffmpeg 实测落盘、片头声明偏移。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import aigc_label as L  # noqa: E402
from podcast_maker import audio_engine as A  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _pil():
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def _ffmpeg(*args):
    subprocess.run([shutil.which("ffmpeg"), "-y", "-v", "error"] + list(args),
                   check=True, capture_output=True)


def _format_tags(path):
    r = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags",
         "-of", "json", str(path)], check=True, capture_output=True, text=True)
    return json.loads(r.stdout).get("format", {}).get("tags", {})


class TestAigcJson(unittest.TestCase):
    def test_seven_elements(self):
        """强标七要素一个不少；必填三项齐、首写镜像与制作者一致。"""
        payload = json.loads(
            L.aigc_json({"aigc.content_producer": "abc123"}, "ep01-audio"))["AIGC"]
        for k in ("Label", "ContentProducer", "ProduceID", "ReservedCode1",
                  "ContentPropagator", "PropagateID", "ReservedCode2"):
            self.assertIn(k, payload)
        self.assertEqual(payload["Label"], "1")       # 附录 E：取值只能是 1/2/3
        self.assertEqual(payload["ProduceID"], "ep01-audio")
        # 注 1：首次写入时传播者要素与制作者一致、传播编号与制作编号一致
        self.assertEqual(payload["ContentPropagator"], "abc123")
        self.assertEqual(payload["PropagateID"], "ep01-audio")
        for k in L.REQUIRED_FIELDS:
            self.assertIn(k, payload)

    def test_producer_is_legally_required(self):
        """制作者留空直接报错——绝不代填，代填谁都是伪造归属。"""
        for cfg in ({}, {"aigc.content_producer": ""},
                    {"aigc.content_producer": "   "}):
            with self.subTest(cfg=cfg):
                with self.assertRaises(L.LabelError):
                    L.aigc_json(cfg, "ep01")
        payload = json.loads(L.aigc_json(
            {"aigc.content_producer": " 我的名字 "}, "x"))["AIGC"]
        self.assertEqual(payload["ContentProducer"], "我的名字")  # 去首尾空白

    def test_config_surface(self):
        """默认空、必填标记在总表上：界面徽标与运行时守卫同源。"""
        from podcast_maker.config_manager import PARAM_SPEC
        spec = PARAM_SPEC["aigc.content_producer"]
        self.assertEqual(spec["default"], "")
        self.assertTrue(spec.get("required"))
        self.assertNotIn("wUwproject", json.dumps(PARAM_SPEC["aigc.content_producer"],
                                                  ensure_ascii=False))

    def test_ascii_safe(self):
        """中文经 ensure_ascii 转义，任何容器的文本段都放得下。"""
        raw = L.aigc_json({"aigc.content_producer": "张三"}, "第1期")
        self.assertTrue(raw.isascii())
        self.assertEqual(json.loads(raw)["AIGC"]["ProduceID"], "第1期")

    def test_labeled_switch(self):
        self.assertTrue(L.labeled({}))
        self.assertTrue(L.labeled({"aigc.labeling": True}))
        self.assertFalse(L.labeled({"aigc.labeling": False}))


class TestImageTagging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _png(self, name):
        Image = _pil()
        if Image is None:
            self.skipTest("未安装 Pillow")
        p = os.path.join(self.tmp, name)
        Image.new("RGB", (8, 8), (10, 20, 30)).save(p)
        return p

    def test_png_text_roundtrip(self):
        p = self._png("a.png")
        L.tag_image(p, {"aigc.content_producer": "t1"})
        with open(p, "rb") as f:
            texts = L.read_png_texts(f.read())
        self.assertIn("AIGC", texts)
        payload = json.loads(texts["AIGC"])["AIGC"]
        self.assertEqual(payload["Label"], "1")
        self.assertEqual(payload["ContentProducer"], "t1")
        self.assertEqual(payload["ProduceID"], "a")

    def test_jpeg_com_roundtrip(self):
        Image = _pil()
        if Image is None:
            self.skipTest("未安装 Pillow")
        p = os.path.join(self.tmp, "b.jpg")
        Image.new("RGB", (8, 8)).save(p)
        L.tag_image(p, {"aigc.content_producer": "t1"})
        with open(p, "rb") as f:
            data = f.read()
        self.assertEqual(data[:2], b"\xff\xd8")
        # SOI 之后第一个段即 COM，载荷含 AIGC 字段名
        marker, ln = data[2:4], int.from_bytes(data[4:6], "big")
        self.assertEqual(marker, b"\xff\xfe")
        self.assertIn(b"AIGC", data[6:4 + ln])

    def test_tag_twice_is_idempotent(self):
        """续跑会反复路过同一张图：重复打标不得重复插块。"""
        cfg = {"aigc.content_producer": "t1"}
        p = self._png("d.png")
        L.tag_image(p, cfg)
        with open(p, "rb") as f:
            first = f.read()
        L.tag_image(p, cfg)
        with open(p, "rb") as f:
            second = f.read()
        self.assertEqual(first, second, "同值重复打标必须零改动")

    def test_wrong_magic_rejected(self):
        p = os.path.join(self.tmp, "c.png")
        with open(p, "wb") as f:
            f.write(b"not a png")
        with self.assertRaises(L.LabelError):
            L.tag_image(p, {"aigc.content_producer": "t1"})


@unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg/ffprobe")
class TestMediaTagging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mp3_tag_readable(self):
        mp3 = os.path.join(self.tmp, "ep01.mp3")
        _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.3", mp3)
        L.tag_file(mp3, {"aigc.content_producer": "t2"})
        self.assertFalse(os.path.exists(mp3 + ".aigc.tmp"))
        raw = _format_tags(mp3).get("AIGC")
        self.assertIsNotNone(raw, "mp3 必须能读回 AIGC 元数据")
        payload = json.loads(raw)["AIGC"]
        self.assertEqual(payload["ContentProducer"], "t2")
        self.assertEqual(payload["ProduceID"], "ep01")

    def test_mp4_tag_readable(self):
        """mov 封装默认丢未知键，use_metadata_tags 必须把 AIGC 留在文件里。"""
        mp4 = os.path.join(self.tmp, "ep01_h.mp4")
        _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.2",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", mp4)
        L.tag_file(mp4, {"aigc.content_producer": "t3"}, faststart=True)
        raw = _format_tags(mp4).get("AIGC")
        self.assertIsNotNone(raw, "mp4 必须能读回 AIGC 元数据")
        self.assertEqual(json.loads(raw)["AIGC"]["Label"], "1")


@unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg/ffprobe")
class TestDeclarationOffset(unittest.TestCase):
    """声明时长并入片头秒数——字幕时间轴只认一个偏移，分家即错位。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _silence(self, name, seconds):
        p = os.path.join(self.tmp, name)
        _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                "-t", "%.3f" % seconds, p)
        return p

    def test_decl_absorbed_into_intro(self):
        decl = self._silence("decl.wav", 1.0)
        intro = self._silence("intro.wav", 2.0)
        body = self._silence("body.wav", 5.0)
        out = os.path.join(self.tmp, "out.wav")
        _, intro_sec, outro_sec = A.add_intro_outro(
            body, out, {"_ai_decl_src": decl, "audio.intro_path": intro},
            44100, 1)
        self.assertAlmostEqual(intro_sec, 3.0, places=2,
                               msg="片头秒数必须等于声明+片头，字幕偏移只此一份")
        self.assertEqual(outro_sec, 0.0)
        total = A.probe_duration_safe(out)
        self.assertAlmostEqual(total, 8.0, places=2)

    def test_no_decl_no_intro_passthrough(self):
        body = self._silence("body.wav", 2.0)
        out = os.path.join(self.tmp, "out.wav")
        _, intro_sec, outro_sec = A.add_intro_outro(body, out, {}, 44100, 1)
        self.assertEqual(intro_sec, 0.0)
        self.assertEqual(outro_sec, 0.0)


if __name__ == "__main__":
    unittest.main()
