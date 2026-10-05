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

"""运行环境定位：ffmpeg / ffprobe 的**唯一**查找、探测与安装实现。

**为什么必须收成一处**：这个智能体是要分发出去的 —— 用户机器上大概率
**一个 ffmpeg 都没有**，「探测 → 一键装」是绝大多数用户唯一会走的路。
而定位逻辑从前有**四份、各写一遍**：

    audio_engine.py:53/70    ffmpeg_bin() / shutil.which("ffprobe")
    tts_engine.py:66/73      ffmpeg_bin() / ffprobe_bin()
    video_engine.py:40/47    ffmpeg_bin() / ffprobe_bin()
    aigc_label.py:158        裸 shutil.which("ffmpeg")   ← 连函数都没封

配置页再加一份「探测」就是第五份；哪天要改口径（比如加兜底搜索路径），
改一处漏三处。与 `tools/sources.py`（全仓唯一源表）、
`tts_service/setup_env.py`（唯一搭建实现）是同一条既有哲学。

**查找顺序（全仓唯一口径）**：

    ① PATH 里的          —— 用户自己装的，先尊重用户已有的环境
    ② 项目 bin/ 里的      —— 我们自己下到项目里的那一份
    ③ 都没有 → None

`locate()` 决定**实际跑哪个**，`state()` 决定**界面报哪个**，**两者必须共用
这一顺序** —— 否则会出现「界面说已就绪、跑起来用的是另一个」。

**返回绝对路径、整个传给 subprocess** ⇒ 不依赖 PATH 解析 ⇒ 用户点完一键安装，
解到 `bin/` 后**当场可用、不用重启**。这是它相对「装到系统 PATH」的根本好处。

**版权口径**：二进制**不内置、不随仓分发、不进 release assets**，落在
`.gitignore` 排除的 `bin/`，与 `tts_service/models/` 的权重同一待遇。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(ROOT, "bin")

EXE_SUFFIX = ".exe" if os.name == "nt" else ""
# 项目要用两件：ffmpeg 干重封装/编码/烧字幕，ffprobe 读音频时长与视频帧数。
# 只装 ffmpeg 不成体系 —— 时长对齐、帧数校验都会失败。
TOOLS = ("ffmpeg", "ffprobe")

USER_AGENT = "podcast-maker/ffmpeg-fetch"


class BinError(RuntimeError):
    """运行环境缺失或安装失败。绝不静默降级。"""


# ------------------------------------------------------------------ 定位
def locate(name):
    """找一个可执行文件，返回**绝对路径**；找不到返回 None。

    顺序是契约：**① PATH → ② 项目 bin/**。改这里就等于改全仓口径。
    """
    exe = shutil.which(name)
    if exe:
        return exe
    return _in_bin_dir(name)


def _in_bin_dir(name):
    p = os.path.join(BIN_DIR, name + EXE_SUFFIX)
    return p if os.path.isfile(p) else None


def source_of(exe):
    """这个路径是从哪来的：``"bin"`` 还是 ``"PATH"``。界面要显示出来。"""
    if not exe:
        return ""
    bd = os.path.normcase(os.path.abspath(BIN_DIR)) + os.sep
    if os.path.normcase(os.path.abspath(exe)).startswith(bd):
        return "bin"
    return "PATH"


def missing_message(name):
    """各模块定位失败时统一用这一句。指向配置页那条现成的路。"""
    return ("找不到 %s。请在配置页的「运行环境」里点一键安装，"
            "或照那块里的说明自己装。" % name)


# ------------------------------------------------------------------ 探测
def probe_version(exe, timeout=15):
    """跑一次 ``-version`` 取首行。取不到返回空串 —— 探测失败不算致命。"""
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    out = (r.stdout or b"").decode("utf-8", "replace")
    first = out.strip().splitlines()
    return first[0].strip() if first else ""


def state():
    """两件工具的现状 + 给界面用的一句话。

    ``{"ok": bool, "message": str, "tools": {name: {...}}}``，
    每个工具报 ``found`` / ``path`` / ``source``（``bin`` 或 ``PATH``）/ ``version``。
    """
    out = {"ok": False, "message": "", "tools": {}}
    missing = []
    for name in TOOLS:
        exe = locate(name)
        out["tools"][name] = {
            "found": bool(exe),
            "path": exe or "",
            "source": source_of(exe) if exe else "",
            "version": probe_version(exe) if exe else "",
        }
        if not exe:
            missing.append(name)

    if missing:
        s = _sources()
        out["message"] = (
            "缺少 **%s**。在配置页的「运行环境」里点「一键安装」：从国内镜像下载 "
            "ffmpeg %s 并解压到本项目 bin/（约 134 MB，十几秒），"
            "**装完当场可用、不用重启**。%s"
            % ("、".join(missing), s.FFMPEG_VERSION, install_hint()))
        return out

    parts = []
    for name in TOOLS:
        it = out["tools"][name]
        where = "项目 bin/" if it["source"] == "bin" else "PATH"
        parts.append("%s ← %s（%s）" % (name, it["path"], where))
    out["ok"] = True
    out["message"] = "；".join(parts)
    return out


def install_hint():
    """「你也可以自己装」那段指引 —— 唯一出处。"""
    s = _sources()
    return ("也可以自己装：从 %s 取 full build（或系统包管理器里任一变体），"
            "把解出来的 bin 目录整体放进本项目 bin/，或加进 PATH，回来点「重新探测」。"
            "**必须含 ffprobe** —— 拼接与时长探测要用，别只拷 ffmpeg.exe。"
            % s.FFMPEG_OFFICIAL_PAGE)


def _sources():
    """取源表。全仓唯一写死下载地址的地方在 tools/sources.py。"""
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import sources  # noqa: PLC0415
    except ImportError:
        raise BinError("找不到 tools/sources.py —— 下载源表不在，无法安装。")
    return sources


# ------------------------------------------------------------------ 安装
def install(log, should_stop=None, on_step=None):
    """下载 → 校验 sha256 → 解压 → 落到项目 bin/。返回 ``state()``。

    全程 **fail-closed**：下载不完整、校验不符、归档里没有目标文件，一律
    抛错并丢弃临时件，**绝不把来路不明的二进制放进 bin/**。

    ``on_step(frac)`` 报进度（0~1）。**进度语义归这里** —— 只有本模块知道
    自己走到哪一步；界面只负责把它画出来，不必去猜日志文案（猜文案的写法
    一改通知就断）。
    """
    s = _sources()
    os.makedirs(BIN_DIR, exist_ok=True)
    stop = should_stop or (lambda: False)
    step = on_step or (lambda frac: None)

    with tempfile.TemporaryDirectory(prefix="ffmpeg-install-") as tmp:
        arc = os.path.join(tmp, s.FFMPEG_ARCHIVE)

        step(0.02)
        log("从国内镜像下载 ffmpeg %s（约 134 MB）…" % s.FFMPEG_VERSION)
        _download(s.FFMPEG_ARCHIVE_URL, arc, log, stop,
                  on_step=lambda pct: step(0.05 + 0.65 * pct / 100.0))

        step(0.75)
        log("校验 sha256…")
        want = _fetch_sha256(s.FFMPEG_SHA256_URL, s.FFMPEG_ARCHIVE)
        got = _sha256(arc)
        if got.lower() != want.lower():
            raise BinError("sha256 校验不符，已丢弃：期望 %s，实际 %s" % (want, got))
        log("校验通过：%s…" % got[:16])

        step(0.85)
        log("解压并安装到 %s …" % BIN_DIR)
        names = list(s.FFMPEG_PAYLOAD)
        for i, name in enumerate(names):
            _extract_member(arc, name, os.path.join(BIN_DIR, name + EXE_SUFFIX))
            log("  %s%s 已就位" % (name, EXE_SUFFIX))
            step(0.85 + 0.14 * (i + 1) / len(names))

    st = state()
    if not st["ok"]:
        raise BinError("安装跑完了但探测仍然不通过，看上面的日志。")
    log("完成：装的是项目 bin/ 里的这一份，当场可用、不用重启。")
    return st


def _download(url, dst, log, stop, on_step=None):
    tick = on_step or (lambda pct: None)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, mark = 0, -1
        while True:
            if stop():
                raise BinError("已中止，临时文件已丢弃。")
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                pct = int(got * 100 / total)
                if pct >= mark + 5:
                    mark = pct
                    tick(pct)
                    log("  已下载 %d%%（%.1f / %.1f MB）"
                        % (pct, got / 1048576.0, total / 1048576.0))
        if total and got != total:
            raise BinError("下载不完整：期望 %d 字节，实际 %d 字节" % (total, got))


def _fetch_sha256(url, filename):
    """取源站的 sha256 清单，挑出目标文件那一行。

    清单是标准 `sha256sum` 格式（`<hash>  <filename>`），也可能带 `*` 前缀。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0]
    raise BinError("源站的 sha256 清单里没有 %s 这一行。" % filename)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _extract_member(arc, name, dst):
    """从 tar.xz 里取出 ``*/bin/<name>.exe`` 复制到 dst。

    只认末尾两段路径，**不写死前缀版本号** —— 换 ffmpeg 版本时这里不用改。
    """
    want = "bin/" + name + EXE_SUFFIX
    with tarfile.open(arc, "r:xz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            tail = "/".join(m.name.replace("\\", "/").split("/")[-2:])
            if tail != want:
                continue
            src = tf.extractfile(m)
            if src is None:
                break
            with src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
            return dst
    raise BinError("归档里找不到 %s。" % want)
