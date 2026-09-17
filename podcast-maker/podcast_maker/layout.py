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

"""项目目录布局：一个项目一棵树，各阶段产物各归其位。

立项即建目录，此后这个项目的**全部材料**都在它自己名下，不再散落在输出
目录的根上：

    projects/<项目>/
      素材/      原稿与结构探查结果
      地图/      期数地图（map.json）
      脚本/      各期脚本，按 `<期号>.json` 命名
      音色/      角色音色档案（A/B 各一份参考音频 + 转录 + 溯源记录）
      封面/      各期封面（`<期号>_16x9.png` 等三尺寸）
      背景/      各期背景图与背景音乐
      音视频/    各期成片与音频（`<期号>.mp4` / `<期号>_v.mp4` / `<期号>.mp3`）
      字幕/      各期字幕（`<期号>.srt`）
      图文/      各期公众号图文（`<期号>.md`）
      报告/      各期校验报告与清单（`<期号>.json`）
      过程/      本期中间文件（逐句语音、混音件、ass），按期分子目录

为什么要有这个模块：路径一旦散在各处，改一次目录结构就要满仓找 `os.path.join`，
漏一处就是产物写去别的地方、界面点开是空的。所以**项目内所有路径只在这里拼**。

期号直接当文件名前缀。分支期号（`3a`）是合法文件名，不做替换——替换会让
磁盘上的名字与地图上的期号对不上，而地图才是期号的唯一来源。
"""

import os
import re

# 各阶段的子目录名。改名只改这里。
DIR_MATERIAL = "素材"
DIR_MAP = "地图"
DIR_SCRIPT = "脚本"
# 角色音色档案：`音色/A/`、`音色/B/` 各含 ref.wav + ref.txt + profile.json。
# 它是整期音色的源头 —— 本地引擎走 Base 变体时，音色不来自模型而来自这段参考
# 音频，所以它必须随项目落盘（由 tts_service/make_voice.py 写入），不能是临时文件。
DIR_VOICE = "音色"
DIR_COVER = "封面"
DIR_BG = "背景"
DIR_AV = "音视频"
DIR_SUB = "字幕"
DIR_ARTICLE = "图文"
DIR_REPORT = "报告"
DIR_TMP = "过程"

SUBDIRS = (DIR_MATERIAL, DIR_MAP, DIR_SCRIPT, DIR_VOICE, DIR_COVER, DIR_BG,
           DIR_AV, DIR_SUB, DIR_ARTICLE, DIR_REPORT, DIR_TMP)

MAP_NAME = "map.json"
STORE_NAME = "_projects.json"          # 项目登记表，仍在输出目录根上


def safe_no(no):
    """期号转成能当文件名用的片段。

    期号来自地图，形态是 `1`、`3a` 这类。真要出现路径分隔符或空串，退成
    `unknown`——宁可文件名难看，也不能拼出一个跳出项目目录的路径。
    """
    s = str(no or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "-", s)
    return s or "unknown"


# ------------------------------------------------------------------ 项目根
def project_dir(base, pid):
    """项目目录。项目不存在时也返回路径（调用方自行决定要不要建）。"""
    return os.path.join(base, str(pid))


def ensure_dirs(root):
    """建齐一棵目录树下的全部子目录。"""
    for name in SUBDIRS:
        os.makedirs(os.path.join(root, name), exist_ok=True)
    return root


def ensure(base, pid):
    """建齐项目目录与全部子目录，返回项目目录路径。立项时调一次。"""
    return ensure_dirs(project_dir(base, pid))


def exists(base, pid):
    return os.path.isdir(project_dir(base, pid))


# ------------------------------------------------------------------ 各阶段
# 下面这些一律收「一棵树的根」——项目目录，或不归属项目的单集目录。
# 收根不收 (base, pid)，是因为单集也要用同一套布局：它只是没有登记而已，
# 产物该分门别类还是分门别类。
def material_dir(root):
    return os.path.join(root, DIR_MATERIAL)


def map_dir(root):
    return os.path.join(root, DIR_MAP)


def map_file(root):
    return os.path.join(map_dir(root), MAP_NAME)


def script_dir(root):
    return os.path.join(root, DIR_SCRIPT)


def script_file(root, no):
    return os.path.join(script_dir(root), "%s.json" % safe_no(no))


def cover_dir(root):
    return os.path.join(root, DIR_COVER)


def bg_dir(root):
    return os.path.join(root, DIR_BG)


def av_dir(root):
    return os.path.join(root, DIR_AV)


def sub_dir(root):
    return os.path.join(root, DIR_SUB)


def article_dir(root):
    return os.path.join(root, DIR_ARTICLE)


def report_dir(root):
    return os.path.join(root, DIR_REPORT)


# 音色档案的三个文件名。它们与 tts_service/make_voice.py 里的同名常量必须一致 ——
# 那边是独立环境，不引入本模块，所以只能各写一份，由 tests/test_layout.py 逐条
# 比对锁死（同 INSTRUCT_EMOTIONS 与 EMOTION_VOCAB 的处置办法）。
VOICE_WAV = "ref.wav"
VOICE_TXT = "ref.txt"
VOICE_PROFILE = "profile.json"


def voice_dir(root):
    """角色音色档案目录。"""
    return os.path.join(root, DIR_VOICE)


def voice_role_dir(root, role):
    """某个角色的音色档案目录：`音色/A`。"""
    return os.path.join(voice_dir(root), role)


def voice_ref_file(root, role):
    """角色的参考音频路径。本地引擎的音色源头就是这个文件。"""
    return os.path.join(voice_role_dir(root, role), VOICE_WAV)


def voice_text_file(root, role):
    """参考音频的转录文本。ICL 模式必须 —— 缺了它模型不知道示例在念什么。"""
    return os.path.join(voice_role_dir(root, role), VOICE_TXT)


def voice_profile_file(root, role):
    """档案的溯源记录（含内容指纹）。"""
    return os.path.join(voice_role_dir(root, role), VOICE_PROFILE)


def tmp_dir(root, no):
    """本期中间文件目录。过程件不跟成品混放，清理时删掉也不心疼。"""
    return os.path.join(root, DIR_TMP, safe_no(no))


def episode_files(root, no):
    """一期的成品路径表。键名是用途，值是绝对路径。"""
    n = safe_no(no)
    return {
        "script": script_file(root, no),
        "audio": os.path.join(av_dir(root), "%s.mp3" % n),
        "video": os.path.join(av_dir(root), "%s.mp4" % n),
        "video_vertical": os.path.join(av_dir(root), "%s_v.mp4" % n),
        "subtitle": os.path.join(sub_dir(root), "%s.srt" % n),
        "article": os.path.join(article_dir(root), "%s.md" % n),
        "report": os.path.join(report_dir(root), "%s.json" % n),
        "manifest": os.path.join(report_dir(root), "%s.manifest.json" % n),
        "cover": {k: os.path.join(cover_dir(root), "%s_%s.png" % (n, k))
                  for k in ("16x9", "3x4", "1x1")},
        "bg_h": os.path.join(bg_dir(root), "%s_bg.png" % n),
        "bg_v": os.path.join(bg_dir(root), "%s_bg_v.png" % n),
        "bgm": os.path.join(bg_dir(root), "%s_bgm.wav" % n),
    }
