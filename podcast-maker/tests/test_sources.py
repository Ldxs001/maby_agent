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

"""下载源不许几处走散。

`tools/sources.py` 是全仓唯一写死下载地址的地方。`tts_service/setup_env.py` 直接
import 它；`requirements-*.txt` 各带自己那一条（pip 的源只能写在文件里）；根目录
`setup.bat` 把 `-i` 传给 pip。改了清华源换成阿里云，漏改任何一处都会装到一半才
发现连不上，所以这里逐条比对，并额外钉住「setup_env.py 不许自己再抄一份」。

另有一条钉的是「不许写不存在的源」：pytorch 的 CUDA 轮子只有上海交大与官方
两处拿得到，其余镜像（清华、阿里云、北外、南大、CERNET）的 pytorch-wheels
路径实测 404 或不含 win_amd64 的轮子。写进去看着很丰盛，实际是把用户送去一个
死地址。
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import sources  # noqa: E402


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestSourcesAgree(unittest.TestCase):
    def test_pypi_mirror_is_the_same_wherever_it_is_written(self):
        for rel in (("setup.bat",), ("tts_service", "requirements-tts.txt"),
                    ("requirements.txt",)):
            self.assertIn(sources.PYPI_MIRROR, _read(*rel),
                          "%s 里的 PyPI 源和 tools/sources.py 不一致" % "/".join(rel))

    def test_the_installer_reads_the_table_instead_of_copying_it(self):
        """安装脚本从源表读，不许自己再写地址 —— 写一份就是多一处会走散的地方。"""
        text = _read("tts_service", "setup_env.py")
        self.assertIn("import sources", text)
        for url in (sources.PYPI_MIRROR, sources.PYTORCH_INDEX,
                    sources.PYTORCH_INDEX_FALLBACK):
            self.assertNotIn(url, text)

    def test_pytorch_index_is_the_same_in_requirements_and_installer(self):
        self.assertIn(sources.PYTORCH_INDEX,
                      _read("tts_service", "requirements-torch.txt"))
        self.assertIn("PYTORCH_INDEX_FALLBACK",
                      _read("tts_service", "setup_env.py"),
                      "主源失败时要退到官方源，那一条也得从源表取")

    def test_model_sources_match_the_downloader(self):
        """三条源要一一对上。

        官方源那一条特意不设 HF_ENDPOINT —— 不设即官方，所以文件里不会出现
        huggingface.co 这个字面量；对不上就按函数名认。
        """
        text = _read("tts_service", "fetch_model.py")
        self.assertIn("def try_modelscope", text)
        self.assertIn("def try_hf_mirror", text)
        self.assertIn("def try_hf_official", text)
        self.assertIn("hf-mirror.com", text)
        self.assertIn('environ.pop("HF_ENDPOINT"', text,
                      "官方源靠「不设 HF_ENDPOINT」实现，这一句是它的开关")


class TestNoPhantomSources(unittest.TestCase):
    """写进去的每个源都得是真的。"""

    def test_no_mirror_is_listed_that_does_not_carry_the_wheel(self):
        """这几个 pytorch-wheels 路径实测没有 win_amd64 轮子，不许写。"""
        blob = "".join(_read(*rel) for rel in (
            ("tts_service", "setup_env.py"),
            ("tts_service", "requirements-torch.txt"),
            ("tools", "sources.py"),
        ))
        for host in ("mirrors.tuna.tsinghua.edu.cn/pytorch-wheels",
                     "mirrors.aliyun.com/pytorch-wheels",
                     "mirrors.bfsu.edu.cn", "mirror.nju.edu.cn",
                     "mirrors.cernet.edu.cn"):
            self.assertNotIn(host, blob,
                             "%s 实测拿不到 torch 的 CUDA 轮子，写进去是把用户送去死地址"
                             % host)

    def test_sources_table_is_documented_with_verification(self):
        text = _read("tools", "sources.py")
        self.assertIn("实测", text, "每条源旁边要写清它是怎么验过的")


if __name__ == "__main__":
    unittest.main()
