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

"""Podcast Maker —— 播客制作智能体。

入口模块导出的公共符号：
    ConfigManager / PARAM_SPEC / MODE_SPEC / GATE_SPEC / PRESET_SPEC
"""

# 版本字面量必须写在这里（不能写成 __version__ = VERSION）：
# 发布工具按 __version__ = "x.y.z" 字面量读版本号（PyPI setup.py 解析同理）。
# 与 config_manager.VERSION 同步修改。
__version__ = "0.12.2"

from .config_manager import (ConfigManager, GATE_SPEC, MODE_SPEC, PARAM_SPEC,
                             PRESET_SPEC, VERSION)

assert VERSION == __version__, f"config_manager.VERSION({VERSION}) 与 __init__.__version__({__version__}) 不同步"
__all__ = ["ConfigManager", "PARAM_SPEC", "MODE_SPEC", "GATE_SPEC",
           "PRESET_SPEC", "VERSION"]
