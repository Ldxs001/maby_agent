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

"""素材库：原始稿按项目入库落盘，供排地图与逐期取用。

上传的内容若只活在界面内存里，刷新即失，项目也就无从记住「这一期讲的是哪一段」。
所以素材要落盘：`<项目目录>/素材/` 下一份 `index.json` 记索引，每份素材一个
`<sid>.md`（已剥行内标记，保留标题结构）。目录位置由 `layout` 定，这里不自己拼。

**结构只有一处来源**：`probe.scan()`。从前这里还有一个 `outline()` 自己数
标题，与探查器各算一套——两套并存的那天起，「这本书有多少章」就有两个答案，
而且没人知道该信哪个。现在这里只负责存与取，结构判断全部交给探查器。

`marks`（docx 的样式证据表）随素材一起入库：它只在读取文件那一刻存在，
不落盘的话第二次读素材就只剩纯文本，作者声明的标题层级白记一场。
"""

import json
import os
import time

from . import duration_model, ingest, layout

INDEX = "index.json"
HEAD_CHARS = 280


class SourceError(RuntimeError):
    pass


# ------------------------------------------------------------------ 路径
def _dir(base, pid):
    if not pid:
        raise SourceError("未指定项目，素材无处安放。")
    return layout.material_dir(layout.project_dir(base, pid))


def source_dir(base, pid):
    """素材目录。对外公开，给探查结果（`<sid>.probe.json`）落盘用。"""
    return _dir(base, pid)


def index_path(base, pid):
    return os.path.join(_dir(base, pid), INDEX)


def _path_of(base, pid, sid):
    return os.path.join(_dir(base, pid), "%s.md" % sid)


# ------------------------------------------------------------------ 读写
def _read(base, pid):
    path = index_path(base, pid)
    if not os.path.exists(path):
        return {"version": 2, "sources": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # 索引坏了不能当没入库——那等于把整本书当成从没给过。
        raise SourceError("素材索引无法解析：%s（%s）" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise SourceError("素材索引结构不对：%s" % path)
    return data


def _write(base, pid, data):
    d = _dir(base, pid)
    os.makedirs(d, exist_ok=True)
    path = index_path(base, pid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _new_sid(items):
    taken = {s.get("id") for s in items}
    n = 1
    while ("s%d" % n) in taken:
        n += 1
    return "s%d" % n


# ------------------------------------------------------------------ 增删查
def add_source(base, pid, name, text, note="", marks=None):
    """素材入库。返回索引项。

    `chars` 记原始字符数、`effective` 记有效字数（汉字 1 + 标点 0.5 + 西文词
    1.5）。两个都留：前者是文件大小，后者才是时长换算用的量。从前只记前者，
    于是同一份素材在库里是 27 万、在写脚本那步是 18 万，对不上账。
    """
    text = (text or "").strip()
    if not text:
        raise SourceError("素材内容为空，未入库。")
    data = _read(base, pid)
    sid = _new_sid(data["sources"])
    os.makedirs(_dir(base, pid), exist_ok=True)
    with open(_path_of(base, pid, sid), "w", encoding="utf-8") as f:
        f.write(text)
    marks = list(marks or [])
    row = {
        "id": sid,
        "name": (name or sid).strip(),
        "chars": len(text),
        "effective": round(duration_model.effective_chars(text)),
        "sections": len(ingest.list_anchors(text, marks)),
        "note": note or "",
        "marks": marks,
        "added": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data["sources"].append(row)
    _write(base, pid, data)
    return row


def list_sources(base, pid):
    return _read(base, pid)["sources"]


def find_source(base, pid, sid):
    for s in _read(base, pid)["sources"]:
        if s.get("id") == sid:
            return s
    return None


def read_source(base, pid, sid):
    path = _path_of(base, pid, sid)
    if not os.path.exists(path):
        raise SourceError("素材不存在：%s" % sid)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def marks_of(base, pid, sid):
    s = find_source(base, pid, sid)
    return list((s or {}).get("marks") or [])


def remove_source(base, pid, sid):
    """移除素材。索引、正文与探查结果一并清掉，返回剩余索引项。"""
    from . import probe
    data = _read(base, pid)
    keep = [s for s in data["sources"] if s.get("id") != sid]
    if len(keep) == len(data["sources"]):
        raise SourceError("素材不存在：%s" % sid)
    data["sources"] = keep
    _write(base, pid, data)
    for path in (_path_of(base, pid, sid), probe.probe_path(base, pid, sid)):
        if os.path.exists(path):
            os.remove(path)
    return keep


# ------------------------------------------------------------------ 取用
def compose(base, pid, refs, max_chars=0):
    """按落点聚合素材原文。

    refs: [{"source": "s1", "anchor": "第1章 起点", "line": 12}]。anchor 为空表示
    整篇；`line` 是标题所在行号，同名标题靠它定位到具体那一条（缺省则按标题找
    第一条，与从前一致）。
    max_chars > 0 时超限截断，并把截断事实记进 meta——静默截断会让模型
    在缺料的情况下硬写，而产物看不出来少了什么。
    """
    parts, used = [], []
    for r in refs or []:
        sid = (r.get("source") or "").strip()
        anchor = (r.get("anchor") or "").strip()
        if not sid:
            continue
        body = read_source(base, pid, sid)
        if anchor:
            body, _meta = ingest.slice_by_anchor(body, anchor,
                                                 marks_of(base, pid, sid),
                                                 line=r.get("line"))
        parts.append("==== %s · %s ====\n%s" % (sid, anchor or "全文", body))
        used.append({"source": sid, "anchor": anchor, "line": r.get("line"),
                     "chars": round(duration_model.effective_chars(body))})
    text = "\n\n".join(parts).strip()
    truncated = False
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, {"refs": used, "chars": len(text), "truncated": truncated}
