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

"""画面引擎：背景 + 动画 + 说话人指示 + 字幕烧录 → 横屏 / 竖屏 mp4。

时长对齐（本模块的硬约束）：视频流时长显式等于音频时长，不用 -shortest 猜。
原做法用 -loop 1 无限静图源配 -shortest，音频结束后又排空了 76 帧，
实测音画相差 2.52 秒。这里改为显式 -t，并在产物校验里断言。

说话人指示用 ASS 绘图实现（不额外挂 ffmpeg overlay 链），
因为 libass 已经在渲染字幕，复用同一层零边际成本。
"""

import os
import shutil
import subprocess

from .config_manager import MODE_SPEC
from .subtitle_engine import _ass_color, fmt_ass_time


class VideoError(RuntimeError):
    """视频合成失败。"""


def ffmpeg_bin():
    exe = shutil.which("ffmpeg")
    if not exe:
        raise VideoError("找不到 ffmpeg，请先安装并加入 PATH。")
    return exe


def ffprobe_bin():
    exe = shutil.which("ffprobe")
    if not exe:
        raise VideoError("找不到 ffprobe。")
    return exe


def _run(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, cwd=cwd)
    if r.returncode != 0:
        raise VideoError("ffmpeg 失败：%s"
                         % (r.stderr or b"").decode("utf-8", "replace")[-800:])


def _ff_escape(p):
    """ffmpeg filtergraph 参数里的路径转义。

    filtergraph 是两级解析：先按 filter 参数分隔符解一层，filter 自己再解一层。
    因此盘符冒号必须转义两次；只转一次时，第一层把 ``\\:`` 还原成字面冒号，
    后半段路径就被当成新的参数名，报 “No option name near '/Windows/Fonts'”。
    反斜杠统一换成斜杠，避免二次转义层级混乱。
    """
    return str(p).replace("\\", "/").replace(":", "\\\\:")


# ------------------------------------------------------------------ 说话人指示
def _portrait_files(cfg):
    """返回实际存在的立绘文件 [(speaker, path)]。A 在前 B 在后。"""
    out = []
    for sp, key in (("A", "speaker_indicator.portrait_a"),
                    ("B", "speaker_indicator.portrait_b")):
        p = cfg.get(key) or ""
        if p and os.path.exists(p):
            out.append((sp, p))
    return out


def _portrait_enable(timings, speaker):
    """按句时间轴生成 enable 表达式：只在说话方开口的时段显示立绘。"""
    if not timings:
        return "'1'"
    spans = ["between(t,%.3f,%.3f)" % (t["start"], t["end"])
             for t in timings if t.get("speaker") == speaker]
    if not spans:
        return "'0'"
    # between 返回 0/1，相加非零即显示
    return "'%s'" % "+".join(spans)


def _speaker_blocks(script, timings, cfg, width, height, suffix):
    """生成说话人指示的 ASS 事件行（左 A 右 B，当前说话方高亮）。"""
    mode = cfg.get("speaker_indicator.mode", "style")
    if mode in ("none", "style"):
        # style 档由字幕样式区隔，不需要额外交互层
        return []
    if mode == "portrait":
        if _portrait_files(cfg):
            # 立绘由 overlay 承担，这里不再画色块
            return []
        # 选了立绘档却没有图片可用时退到色块档：
        # 静默什么都不画，等于这个档位形同不存在。
        mode = "block"

    col_a = _ass_color(cfg.get("speaker_indicator.color_a"), "&HC9A45C")
    col_b = _ass_color(cfg.get("speaker_indicator.color_b"), "&H6FA8DC")
    name_a = cfg.get("tts.name_a", "A")
    name_b = cfg.get("tts.name_b", "B")
    if suffix == "_v":
        size = max(18, int(cfg.get("subtitle.font_size_vertical", 40) * 0.55))
        margin_v = int(cfg.get("subtitle.margin_v_vertical", 220)) + 54
        half = width * 0.24
    else:
        size = max(20, int(cfg.get("subtitle.font_size", 52) * 0.5))
        margin_v = int(cfg.get("subtitle.margin_v", 90)) + 62
        half = width * 0.16

    # 底框：胶囊形状用 ASS 矢量绘图实现（\p1）
    pad_x = int(size * 0.9)
    pad_y = int(size * 0.45)
    box_h = size + pad_y * 2
    box_w = int(max(len(name_a), len(name_b)) * size * 1.05) + pad_x * 2
    radius = int(box_h * 0.35)

    def capsule(cx, cy, w_, h_, r):
        x0, y0 = cx - w_ / 2, cy - h_ / 2
        x1, y1 = x0 + w_, y0 + h_
        return ("m %d %d l %d %d b %d %d %d %d %d %d l %d %d "
                "b %d %d %d %d %d %d l %d %d b %d %d %d %d %d %d l %d %d "
                "b %d %d %d %d %d %d" % (
                    int(x0 + r), int(y0), int(x1 - r), int(y0),
                    int(x1), int(y0), int(x1), int(y0), int(x1), int(y0 + r),
                    int(x1), int(y1 - r),
                    int(x1), int(y1), int(x1), int(y1), int(x1 - r), int(y1),
                    int(x0 + r), int(y1),
                    int(x0), int(y1), int(x0), int(y1), int(x0), int(y1 - r),
                    int(x0), int(y0 + r),
                    int(x0), int(y0), int(x0), int(y0), int(x0 + r), int(y0)))

    events = []
    for i, item in enumerate(script):
        if i >= len(timings):
            break
        t = timings[i]
        speaker = item.get("speaker", "A")
        cy = height - margin_v - box_h / 2
        for who, cx, col, name in (
                ("A", half, col_a, name_a), ("B", width - half, col_b, name_b)):
            active = (who == speaker)
            alpha = "00" if active else "B4"
            body = (r"{\an5\pos(%d,%d)\p1\c&H%s&\alpha&H%s%s}" % (int(cx), int(cy), col[2:], alpha, "&")
                    + capsule(int(cx), int(cy), box_w, box_h, radius) + r"{\p0}")
            events.append("Dialogue: 1,%s,%s,Default,,0,0,0,,%s"
                          % (fmt_ass_time(t["start"]), fmt_ass_time(t["end"]), body))

        # 名字文字层：仅当前说话方显示
        cx = half if speaker == "A" else width - half
        col = col_a if speaker == "A" else col_b
        name = name_a if speaker == "A" else name_b
        body = r"{\an5\pos(%d,%d)\c&H%s&\alpha&H00}" % (int(cx), int(cy), col[2:])
        events.append("Dialogue: 2,%s,%s,Default,,0,0,0,,%s%s"
                      % (fmt_ass_time(t["start"]), fmt_ass_time(t["end"]), body, name))
    return events


def render_ass(srt_events_path, out_path, script, timings, cfg, width, height, suffix=""):
    """在已有 ASS 文件上追加说话人指示层。返回最终 ASS 路径。"""
    with open(srt_events_path, "r", encoding="utf-8", errors="replace") as f:
        base = f.read()
    extra = _speaker_blocks(script, timings, cfg, width, height, suffix)
    if extra:
        base = base.rstrip("\n") + "\n" + "\n".join(extra) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(base)
    return out_path


# ------------------------------------------------------------------ 滤镜链
def bg_wants_single_frame(mode):
    """缓推档需要单帧输入。

    zoompan 的 d 是「每张输入帧生成多少输出帧」。若背景用 -loop 1 喂进去，
    输入帧数 = 时长×帧率，每个输入帧又各自生成 d 帧，实际只有第一个输入帧
    的产物落在时间轴内，其余全部被 -t 截掉——既是浪费，也让缩放曲线依赖
    输入帧率这种与画面无关的量。改成单帧输入，d 就是整段帧数。
    """
    return mode == "kenburns"


def _animation_filter(mode, width, height, fps, duration, accent, zoom_max=1.04):
    """返回 (filter_complex 片段列表, 额外输入需求)。"""
    frames = max(1, int(round(duration * fps)))
    if mode == "kenburns":
        # 缩放必须按整段帧数归一，写成 min(zoom+常数, 上限) 会中途撞顶：
        # 62 秒 30fps 的片子在第 9.5 秒就到上限，之后 52 秒完全静止，
        # 视觉上是"先滚一下然后死住"。这里用输出帧序号驱动，全程匀速。
        return ([
            "[bg]zoompan=z='1+%.4f*on/%d':d=%d:s=%dx%d:fps=%d:"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'[vbase]" % (
                max(0.0, float(zoom_max) - 1.0), frames, frames,
                width, height, fps),
        ], [])
    if mode == "waveform":
        return ([
            "[1:a]showwaves=s=%dx%d:mode=cline:colors=0x%s:rate=%d,"
            "format=yuva420p,colorchannelmixer=aa=0.30[wav]" % (
                width, int(height * 0.22), accent, fps),
            "[bg][wav]overlay=x=0:y=H-h-40:format=auto,format=yuv420p[vbase]",
        ], [])
    if mode == "spectrum":
        return ([
            "[1:a]showspectrum=s=%dx%d:mode=combined:color=intensity:scale=log,"
            "format=yuva420p,colorchannelmixer=aa=0.28[spc]" % (width, int(height * 0.30)),
            "[bg][spc]overlay=x=0:y=H-h-40:format=auto,format=yuv420p[vbase]",
        ], [])
    return ([], [])


def _dim_filter(cur, width, height, amount):
    """整幅压暗。返回 (filter_complex 片段, 新标签)；amount 不为正时不压。

    必须在 RGB 域混合。滤镜链跑到这一步时像素已在带范围的 YUV 里，直接叠黑色
    会把亮度与色度分开处理：实测蓝色被压掉四成、红色几乎没动，成片比背景图
    和封面明显偏色。先转回 RGB 再叠，才是一次等比的压暗。
    """
    if amount <= 0:
        return None, cur
    return ("[%s]format=rgb24,drawbox=x=0:y=0:w=%d:h=%d:color=black@%.2f:t=fill[vdim]"
            % (cur, width, height, amount), "vdim")


def compose(audio_path, ass_path, out_path, cfg, bg_path, duration,
            width, height, suffix="", font_dir=None, timings=None, log=None):
    """合成单个视频。时长显式对齐音频；立绘按句时间轴切换。"""
    log = log or (lambda m: None)
    fps = int(cfg.get("video.fps", 30))
    mode = cfg.get("animation.mode", "kenburns")
    preset = cfg.get("video.encoder_preset", "medium")
    crf = int(cfg.get("video.crf", 20))
    bg_dim = float(cfg.get("video.bg_dim", 0.15))
    bg_color = cfg.get("bg_color", "0x0F1418")
    accent = "C9A45C"

    out_dir = os.path.dirname(os.path.abspath(out_path))

    cmd = [ffmpeg_bin(), "-y"]
    inputs = 0
    if bg_path and os.path.exists(bg_path):
        if bg_wants_single_frame(mode):
            cmd += ["-i", bg_path]
        else:
            cmd += ["-loop", "1", "-t", "%.3f" % duration, "-i", bg_path]
    else:
        cmd += ["-f", "lavfi", "-t", "%.3f" % duration,
                "-i", "color=c=%s:s=%dx%d:r=%d" % (bg_color, width, height, fps)]
    inputs += 1
    cmd += ["-i", audio_path]
    inputs += 1

    # 立绘输入索引必须显式计数。用 len(cmd)//2 推算会随输入参数个数变化而失准，
    # 直接把滤镜图指向错误的输入。
    portrait_slots = []
    for sp, p in _portrait_files(cfg):
        cmd += ["-loop", "1", "-t", "%.3f" % duration, "-i", p]
        portrait_slots.append((sp, inputs))
        inputs += 1

    chain = []
    anim, _ = _animation_filter(mode, width, height, fps, duration, accent,
                                cfg.get("animation.zoom_max", 1.04))
    # 背景先归一到目标尺寸
    chain.append("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,"
                 "crop=%d:%d,setsar=1[bg]" % (width, height, width, height))

    overlay_inputs = []
    if anim:
        chain.extend(anim)
        base_label = "vbase"
    else:
        chain.append("[bg]null[vbase]")
        base_label = "vbase"

    cur = base_label
    for k, (sp, idx) in enumerate(portrait_slots):
        chain.append("[%d:v]scale=-1:ih*0.42[p%d]" % (idx, k))
        chain.append("[%s][p%d]overlay=x=%s:y=H-h-40:enable=%s[vp%d]"
                     % (cur, k, "40" if sp == "A" else "W-w-40",
                        _portrait_enable(timings, sp), k))
        cur = "vp%d" % k

    dim, cur = _dim_filter(cur, width, height, bg_dim)
    if dim:
        chain.append(dim)

    # ASS 必须给绝对路径。字幕由 pipeline 写在过程目录（work），成片落在音视频目录，
    # 两者不同目录；而 ffmpeg 的 cwd 为了产物文件名干净而设成成片目录。若只传
    # basename，libass 会在成片目录里找字幕，报 "ass_read_file(sub.ass): fopen failed"。
    # 绝对路径又要过 _ff_escape：盘符冒号在 filtergraph 里是两级解析，必须转义两次。
    ass_arg = "ass=%s" % _ff_escape(os.path.abspath(ass_path))
    if font_dir:
        ass_arg += ":fontsdir=%s" % _ff_escape(font_dir)
    chain.append("[%s]%s,format=yuv420p[vout]" % (cur, ass_arg))

    cmd += ["-filter_complex", ";".join(chain),
            "-map", "[vout]", "-map", "1:a",
            "-t", "%.3f" % duration,
            "-r", str(fps),
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            os.path.basename(out_path)]

    _run(cmd, cwd=out_dir)
    log("视频已合成：%s" % os.path.basename(out_path))
    return out_path


# ------------------------------------------------------------------ 校验
def probe_streams(path):
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries",
         "stream=index,codec_type,duration,width,height,r_frame_rate",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True)
    streams = []
    cur = {}
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k == "index":
            if cur:
                streams.append(cur)
            cur = {"index": v.strip()}
        else:
            cur[k] = v.strip()
    if cur:
        streams.append(cur)
    out = {"video": None, "audio": None}
    for s in streams:
        try:
            dur = float(s.get("duration") or 0)
        except ValueError:
            dur = 0.0
        if s.get("codec_type") == "video" and out["video"] is None:
            out["video"] = {"duration": dur, "width": int(s.get("width") or 0),
                            "height": int(s.get("height") or 0),
                            "fps": s.get("r_frame_rate", "")}
        elif s.get("codec_type") == "audio" and out["audio"] is None:
            out["audio"] = {"duration": dur}
    return out


def verify_av_sync(video_path, tolerance=0.05):
    """音画时长断言。视频流与音频流时长差超容差即 FAIL。"""
    try:
        st = probe_streams(video_path)
    except VideoError as e:
        return {"ok": False, "detail": str(e)}
    v = st.get("video") or {}
    a = st.get("audio") or {}
    vd, ad = float(v.get("duration") or 0), float(a.get("duration") or 0)
    diff = abs(vd - ad)
    return {"ok": diff <= tolerance,
            "video_duration": round(vd, 3), "audio_duration": round(ad, 3),
            "diff": round(diff, 3), "tolerance": tolerance,
            "detail": "视频 %.3fs / 音频 %.3fs / 差 %.3fs" % (vd, ad, diff)}
