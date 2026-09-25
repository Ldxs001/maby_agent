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

"""AIGC 合规标识（唯一入口）：元数据隐式标识 + 产物打标。

依据《人工智能生成合成内容标识办法》（国信办通字〔2025〕2号，2025-09-01
施行）与配套强制性国家标准 GB 45438-2025《网络安全技术 人工智能生成合成
内容标识方法》：

- **隐式标识**（本办法第五条，无条件）：在文件元数据中写入字段名为
  ``AIGC`` 的 JSON，含七要素——生成合成标签、内容制作者、内容编号、
  传播者两项与两个预留字段（必填范围见 ``REQUIRED_FIELDS``）。mp3/aac
  走 ffmpeg 的 ``-metadata``（落 ID3 TXXX）；mp4 走
  ``use_metadata_tags``（mov 封装默认丢未知键，实测已验证）；PNG 落 tEXt
  块、JPEG 落 COM 段（纯标准库读写，不重编码）。
- **显式标识**：音频起始位置的语音声明由编排层合成、``audio_engine``
  在片头拼接（声明时长并入片头偏移，字幕时间轴随之整体后移）；封面与
  背景的"AI 生成"水印在 ``assets_factory`` 绘制，与本模块互不替代。

总开关 ``aigc.labeling``（默认开）。开启后 ``aigc.content_producer``
（内容制作者）是法定必填项：留空时 ``aigc_json`` 报错停产——制作者
身份跟的是运行工具的人，开源后每个用户填自己的，代码里不得预设任何
人的名字。内容编号 ProduceID 一律取**去扩展名的文件名**——产物文件名
已含期号，天然唯一，不需要调用方再传一份编号。
"""

import json
import os
import struct
import subprocess
import zlib

from . import bins

FIELD = "AIGC"
DISCLOSURE_TEXT = "本节目人声由人工智能合成。"
MEDIA_EXTS = (".mp3", ".aac", ".m4a", ".mp4", ".mov", ".mkv")
IMAGE_EXTS = (".png", ".jpg", ".jpeg")

# GB 45438-2025 附录 E 的七要素里，法律必填的只有三个：生成合成标签
# （Label）、内容制作者（ContentProducer）、内容编号（ProduceID）。
# Label 与 ProduceID 由程序产出，ContentProducer 是唯一要人填的——配置页
# 上以「必填」标记，留空则管线拒绝出片；两个传播者要素在首次写入时按
# 注 1 与制作者/制作编号镜像，两个预留字段可选。
REQUIRED_FIELDS = ("Label", "ContentProducer", "ProduceID")

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class LabelError(RuntimeError):
    """标识写入失败。绝不静默跳过——合规动作失败要让整条流水线停下来。"""


def aigc_json(cfg, content_id=""):
    """按强标七要素组装元数据 JSON（ASCII 安全，任何容器都能原样存放）。

    Label 取值按附录 E 只能是 1/2/3——本工具的产物确为 AI 生成，恒取 1。
    ContentProducer 是唯一需要人填的必填项：留空直接报错，绝不代填——
    代填任何名字（原作者的、工具的）都是伪造归属，比不填更糟。
    """
    producer = str(cfg.get("aigc.content_producer") or "").strip()
    if not producer:
        raise LabelError(
            "AIGC 标识已开启，但内容制作者（aigc.content_producer）没有填。"
            "这是《人工智能生成合成内容标识办法》的法定必填项：请到配置页"
            "「AIGC 标识」分区填入你自己的名称或编码——不是模型名，也不是"
            "工具名。")
    payload = {
        "Label": "1",
        "ContentProducer": producer,
        "ProduceID": str(content_id or ""),
        "ReservedCode1": "",
        "ContentPropagator": producer,
        "PropagateID": str(content_id or ""),
        "ReservedCode2": "",
    }
    return json.dumps({FIELD: payload}, separators=(",", ":"))


def labeled(cfg):
    """总开关。关了就什么都不写、什么也不拼，调用方据此跳过。"""
    return bool(cfg.get("aigc.labeling", True))


# ------------------------------------------------------------------ 图片（纯标准库）
def _png_chunks(data):
    """把 PNG 字节流拆成 (类型, 载荷) 列表（不含文件签名）。"""
    pos = 8
    out = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        out.append((ctype, payload))
        pos += 12 + length            # 长度 + 类型 + 载荷 + CRC
    return out


def _png_chunk(ctype, payload):
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def tag_png_bytes(data, text):
    """在 IHDR 之后插入 tEXt 块。JSON 已 ASCII 安全，tEXt 的 Latin-1 约束不碍事。"""
    if not data.startswith(_PNG_SIG):
        raise LabelError("不是 PNG 文件，拒绝打标。")
    out = [_PNG_SIG]
    for ctype, payload in _png_chunks(data):
        out.append(_png_chunk(ctype, payload))
        if ctype == b"IHDR":
            out.append(_png_chunk(b"tEXt", FIELD.encode("ascii") + b"\x00"
                                  + text.encode("ascii")))
    return b"".join(out)


def read_png_texts(data):
    """读出 PNG 里全部 tEXt（测试与核验用）。返回 {keyword: text}。"""
    out = {}
    if not data.startswith(_PNG_SIG):
        return out
    for ctype, payload in _png_chunks(data):
        if ctype == b"tEXt":
            k, _, v = payload.partition(b"\x00")
            out[k.decode("latin-1")] = v.decode("latin-1")
    return out


def tag_jpeg_bytes(data, text):
    """在 SOI 之后插入 COM 段。载荷长度超 65533 字节才需要多段，标识 JSON 远够不着。"""
    if data[:2] != b"\xff\xd8":
        raise LabelError("不是 JPEG 文件，拒绝打标。")
    payload = text.encode("ascii")
    seg = b"\xff\xfe" + struct.pack(">H", len(payload) + 2) + payload
    return data[:2] + seg + data[2:]


# ------------------------------------------------------------------ 音视频（ffmpeg）
def tag_media(src, dst, cfg, content_id="", faststart=False):
    """ffmpeg 重封装写入 AIGC 元数据（流直拷，不重编码）。

    mp4/mov/m4a 的 mov 封装默认**静默丢弃**未知元数据键（实测 ffprobe 读不
    回来），必须开 ``use_metadata_tags`` 才会把自定义键写进 ilst——实测开与
    不开的差别就是元数据在不在。faststart 请求只是叠加，不是替代。
    """
    exe = bins.locate("ffmpeg")
    if not exe:
        raise LabelError(bins.missing_message("ffmpeg"))
    ext = os.path.splitext(dst)[1].lower()
    tmp = dst + ".aigc.tmp"
    cmd = [exe, "-y", "-i", src, "-c", "copy", "-map", "0",
           "-metadata", "%s=%s" % (FIELD, aigc_json(cfg, content_id))]
    if ext in (".mp4", ".mov", ".m4a"):
        flags = "+use_metadata_tags" + ("+faststart" if faststart else "")
        cmd += ["-movflags", flags]
    elif faststart:
        cmd += ["-movflags", "+faststart"]
    # 临时文件后缀是 .tmp，ffmpeg 推不出封装格式，必须显式给 -f。
    fmt = {".mp3": "mp3", ".aac": "aac", ".m4a": "mp4",
           ".mp4": "mp4", ".mov": "mov", ".mkv": "matroska"}.get(ext)
    if fmt:
        cmd += ["-f", fmt]
    cmd += [tmp]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise LabelError("AIGC 元数据写入失败：%s"
                         % (r.stderr or b"").decode("utf-8", "replace")[-500:])
    os.replace(tmp, dst)
    return dst


def tag_image(path, cfg, content_id=""):
    """图片打标（就地）：PNG 走 tEXt，JPEG 走 COM。

    幂等：已有同值 AIGC 标识的图直接跳过——续跑会反复路过同一批封面，
    重复插入 tEXt/COM 会把文件越撑越脏。
    """
    with open(path, "rb") as f:
        data = f.read()
    text = aigc_json(cfg, content_id or os.path.splitext(os.path.basename(path))[0])
    if data.startswith(_PNG_SIG):
        if read_png_texts(data).get(FIELD) == text:
            return path
        new = tag_png_bytes(data, text)
    elif data[:2] == b"\xff\xd8":
        if b"\xff\xfe" in data[:4096] and text.encode("ascii") in data[:4096]:
            return path
        new = tag_jpeg_bytes(data, text)
    else:
        raise LabelError("不支持的图片格式：%s" % path)
    tmp = path + ".aigc.tmp"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, path)
    return path


def tag_file(path, cfg, content_id="", faststart=False):
    """产物打标统一入口（就地）：音视频走 ffmpeg，图片走块插入。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return tag_image(path, cfg, content_id)
    if ext in MEDIA_EXTS:
        return tag_media(path, path, cfg,
                         content_id or os.path.splitext(os.path.basename(path))[0],
                         faststart=faststart)
    raise LabelError("不支持打标的文件类型：%s" % path)
