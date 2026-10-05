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

"""配套规划文件（<正文同名>.outline.json）的生成。

正文 md 只带标题层级，章/子结构的**主旨与字数**留在会话/项目状态里——
下游（如 podcast-maker）拿到 md 排图时只能再烧几十次模型调用重新凝缩。
导出 md 的同时把这份规划顺手写成一个同名的 .outline.json：正文与规划
一起走，下游读文件就能拿到结构化的主旨，零猜测、零重算。

通用线与小说线共用这一个提取器：两线的字段名不同（summary/overview、
sub_sections/sub_structures），在这里统一成一种输出结构，下游只认一份。
"""

import json
from pathlib import Path

#: 文件里写明出身与版本：下游只认自己认识的 kind+version，不猜。
KIND = "structured-writer-outline"
VERSION = 1


def _clean_text(value):
    return str(value or "").strip()


def _norm_section(sec):
    """一章统一成 {title, gist, chars, subs}。空标题的跳过。"""
    title = _clean_text(sec.get("title"))
    if not title:
        return None
    gist = _clean_text(sec.get("summary") or sec.get("overview"))
    chars = int(sec.get("actual_word_count") or sec.get("word_count") or 0)
    subs = []
    for sub in (sec.get("sub_sections") or sec.get("sub_structures") or []):
        if not isinstance(sub, dict):
            continue
        # 通用线勾掉的节不会进正文，规划里也不带给下游。
        if sub.get("_checked") is False:
            continue
        sub_title = _clean_text(sub.get("title"))
        if not sub_title:
            continue
        subs.append({
            "title": sub_title,
            "gist": _clean_text(sub.get("summary") or sub.get("overview")),
            "chars": int(sub.get("actual_word_count")
                         or sub.get("word_count") or 0),
        })
    return {"title": title, "gist": gist, "chars": chars, "subs": subs}


def build_payload(outline):
    """两种来源统一成一份输出结构。

    - 通用线会话：``{"sections": [...]}``
    - 小说项目状态：``{"chapters": [...]}``（章主旨叫 overview）
    """
    if not isinstance(outline, dict):
        return None
    raw = (outline.get("sections") or outline.get("chapters") or [])
    sections = []
    for sec in raw:
        if not isinstance(sec, dict):
            continue
        # 通用线整章被勾掉的同样不进正文，不带给下游。
        if sec.get("_checked") is False:
            continue
        norm = _norm_section(sec)
        if norm:
            sections.append(norm)
    if not sections:
        return None
    return {"kind": KIND, "version": VERSION, "sections": sections}


def write_sidecar(md_path, outline):
    """在正文 md 旁边写同名 .outline.json。写不出内容返回 None，不写空文件。

    规划提取失败**不拦导出**——正文是主产物，规划文件是捎带的赠品；下游
    读不到它只会走自己的报错路径，不会拿到一份坏 json。
    """
    try:
        payload = build_payload(outline)
    except Exception:                                        # noqa: BLE001
        return None
    if not payload:
        return None
    path = Path(md_path).with_suffix(".outline.json")
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError:
        return None
    return str(path)
