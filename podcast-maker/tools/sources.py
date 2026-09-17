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

"""下载源清单 —— 全仓唯一事实来源。

这一份是所有下载地址的源头：`tts_service/setup_env.py` 直接 import 它；
`requirements-torch.txt` / `requirements-tts.txt` 各带自己那一条（pip 的源只能
写在文件里）；根目录 `setup.bat` 把 `-i` 传给 pip。`tests/test_sources.py` 逐条
比对，任何一处与这里不一致就当场失败，避免几处走散。

每一条都是**实测可达**的，不是照抄别人的清单：
  * 清华 / 阿里云 / 腾讯云 / 中科大 的 PyPI 源均已验证返回 200。
  * PyTorch 的 CUDA 轮子只有上海交大与官方两处能拿到 `torch-2.9.1+cu126`
    的 cp311 win_amd64 轮子；清华、阿里云、北外、南大、CERNET 的
    `pytorch-wheels` 路径实测 404 或没有该轮子，因此**不写进来**。
"""

# ---------------------------------------------------------------- PyPI
# 主用清华：国内直连速度最稳，且是各家镜像里索引最全的一档。
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple/"
PYPI_MIRRORS = {
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple/",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple/",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple/",
    "ustc": "https://mirrors.ustc.edu.cn/pypi/simple/",
    "default": "https://pypi.org/simple/",
}

# ---------------------------------------------------------------- PyTorch
# 装 torch 时必须把这一条当**主 index**：`+cu126` 这种带本地版本号的轮子
# 只在这些 pytorch-wheels 源里，PyPI 上那个同名包是 CPU-only 构建，
# 装上去永远不会用 GPU。
PYTORCH_INDEX = "https://mirror.sjtu.edu.cn/pytorch-wheels/cu126/"
PYTORCH_INDEX_FALLBACK = "https://download.pytorch.org/whl/cu126"

# ---------------------------------------------------------------- 模型权重
# 由 tts_service/fetch_model.py 按顺序试；这里是清单，不是下载实现。
MODEL_SOURCES = (
    ("modelscope", "ModelScope 国内（首选）"),
    ("hf_mirror", "https://hf-mirror.com（HF 国内镜像）"),
    ("hf_official", "HF 官方源（不设 HF_ENDPOINT 即为官方）"),
)
