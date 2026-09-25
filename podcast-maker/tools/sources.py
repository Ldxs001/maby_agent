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

# ---------------------------------------------------------------- ffmpeg 运行时
# 合成、烧字幕、拼视频要用 **ffmpeg + ffprobe 两件**（见 podcast_maker/bins.py）。
# **不内置、不随仓分发**：仓里只有下载器，二进制落在 .gitignore 排除的 `bin/`，
# 与 tts_service/models/ 的权重同一待遇 —— 用户自己下，我们不分发。
#
# 主源是阿里 npmmirror 镜像的 KarinJS/FFmpeg-Builds（BtbN 系）静态构建。选它的
# 理由全部实测过：**8.4 MB/s（134 MB / 17 秒）**；`tar.xz` 解出来 3 个 exe、
# **0 个 dll**；`configure` 里 `--enable-libass --enable-fontconfig` 都在；
# 拿项目自己的 ass 烧中文字幕实测通过（中文字形真出得来，不只是「列在 -filters 里」）。
#
# 版本为什么是 8.1.3 而不是更高的：
#   * 官方最新是 9.0.2（2026-09-18），但**国内源上没有 9.x** —— 清华只有 MSYS2
#     的 mingw 仓库（连带 234 个依赖，不可用），腾讯云 / 中科大 404，华为云那条
#     是门户页不是镜像；
#   * 8.1.3 是 **8.1 分支的最新补丁**（2026-09-21 发布），不是被淘汰的老版本，
#     对项目要用的滤镜与编码器**一个不缺**；
#   * 走 GitHub 拿 9.0.2 实测 18.8~25.7 KB/s（134 MB ≈ 1.8 小时），比国内源慢 400 倍。
FFMPEG_VERSION = "8.1.3"
FFMPEG_DIR = "ffmpeg-builds/v%s" % FFMPEG_VERSION
FFMPEG_BASE = "https://registry.npmmirror.com/-/binary/" + FFMPEG_DIR + "/"
# 主用 GPL 变体（与项目现有构建同档）；LGPL 变体留作备选（许可证更宽松）。
FFMPEG_ARCHIVE = "ffmpeg-%s-win32-x64-gpl.tar.xz" % FFMPEG_VERSION
FFMPEG_ARCHIVE_URL = FFMPEG_BASE + FFMPEG_ARCHIVE
FFMPEG_ARCHIVE_LGPL = "ffmpeg-%s-win32-x64-lgpl.tar.xz" % FFMPEG_VERSION
FFMPEG_ARCHIVE_URL_LGPL = FFMPEG_BASE + FFMPEG_ARCHIVE_LGPL
# 源站自带校验值：一行 `sha256  filename`
FFMPEG_SHA256_URL = FFMPEG_BASE + "ffmpeg-%s.sha256.txt" % FFMPEG_VERSION
# 官方下载页（「你也可以自己装」那条路指引用户去的地方）
FFMPEG_OFFICIAL_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
# 解压后从归档里取这两件，落到项目 bin/
FFMPEG_PAYLOAD = ("ffmpeg", "ffprobe")
