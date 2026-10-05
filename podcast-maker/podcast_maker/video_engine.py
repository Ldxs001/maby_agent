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

import math
import os
import shutil
import subprocess

from . import bins
from .config_manager import MODE_SPEC
from .subtitle_engine import _ass_color, fmt_ass_time


class VideoError(RuntimeError):
    """视频合成失败。"""


def ffmpeg_bin():
    exe = bins.locate("ffmpeg")
    if not exe:
        raise VideoError(bins.missing_message("ffmpeg"))
    return exe


def ffprobe_bin():
    exe = bins.locate("ffprobe")
    if not exe:
        raise VideoError(bins.missing_message("ffprobe"))
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


def _animation_filter(mode, width, height, fps, duration, accent, zoom_max=1.04,
                      audio_idx=1):
    """返回 (filter_complex 片段列表, 额外输入需求)。

    `audio_idx` 是音频输入的下标：波形与频谱直接吃音频流，而这个下标不总是 1——
    背景若由两张图接成（见 `_graph` 的 `box_split`），音频就被挤到 2。写死 1 会
    让波形去第二张背景图上取音频，报「流类型不匹配」。
    """
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
            "[%d:a]showwaves=s=%dx%d:mode=cline:colors=0x%s:rate=%d,"
            "format=yuva420p,colorchannelmixer=aa=0.30[wav]" % (
                int(audio_idx), width, int(height * 0.22), accent, fps),
            "[bg][wav]overlay=x=0:y=H-h-40:format=auto,format=yuv420p[vbase]",
        ], [])
    if mode == "spectrum":
        return ([
            "[%d:a]showspectrum=s=%dx%d:mode=combined:color=intensity:scale=log,"
            "format=yuva420p,colorchannelmixer=aa=0.28[spc]" % (int(audio_idx), width, int(height * 0.30)),
            "[bg][spc]overlay=x=0:y=H-h-40:format=auto,format=yuv420p[vbase]",
        ], [])
    return ([], [])


# ------------------------------------------------------------------ 分段并行
CORES_PER_FFMPEG = 4    # 一个 libx264 进程喂到 ~4 个逻辑核就基本吃满
WORKERS_MAX = 1         # ← 实测结论：这条链不该分段（见 render_workers 的说明）
SEG_SNAP = 2.0          # 切点就近吸附到句间空隙的最大距离（秒）


def render_workers(cpu=None, cap=WORKERS_MAX):
    """分段并行度。**默认恒为 1 —— 实测这条链切段只会更慢。**

    墙不在 CPU：单路只用 2.8～3.1 个逻辑核（24 核机器），可每帧 1080p 要连过五六道
    全帧处理（输入 → 归一 → rgb24 → libass → yuv420p → x264），瓶颈是**进程间共享的
    数据搬运**。把同一条切成 N 段各起一个进程，只是把这份共享能力切成 N 份，合计吞吐
    不涨反跌，还多付 N 次进程启动、拼接与合流。三次独立实测（真素材 2c，横屏）：

      · 整期 22.7 分钟：1 路 **156.6 s** ／ 6 路 177.9 s        → 0.88x
      · 120 秒：1 路 **260 帧/秒** ／ 2 路 277 ／ 4 路 246 ／ 6 路 230
      · 横竖并发（180 秒）：串行 38.1 s ／ 并发 33.6 s           → 1.13x

    分段机制本身是**对的**——无损对账（`tools/probes/parallel_render_check.py`，两条路
    都 crf 0 编、逐帧比 MSE）证明切点、`trim`、`setpts` 没让任何一帧错位，MSE 全 0。
    只是不划算，所以默认不启用。

    `cap` 不是摆设：换一条链（例如将来出现滤镜重、编码轻的档位）先跑
    `tools/probes/render_scale_scan.py` 量一遍，再决定 `WORKERS_MAX` 该写几。
    """
    n = int(cpu if cpu is not None else (os.cpu_count() or 1))
    return max(1, min(int(cap), n // CORES_PER_FFMPEG))


def encoder_threads(workers, cpu=None):
    """一个 ffmpeg 进程分到多少编码线程：把设备逻辑核按并发路数**分掉**。

    这一条只在走分段那条路时才用得上，但它是**必需的**：ffmpeg 不写 `-threads` 就是
    auto——**每个进程都按整机核数开线程**，N 路并发等于 N×核数 个线程挤在核数 个核上。
    实测（分段并行开启时）：不分核的 6 路 177.9 秒，比单路 156.6 秒还慢。

    二核机器、一路并发都算得出 1，不会开出多余的线程。
    """
    n = int(cpu if cpu is not None else (os.cpu_count() or 1))
    return max(1, n // max(1, int(workers)))


def box_onset_frames(timings, fps):
    """字幕框该从第几帧起出现。0 表示「从第 0 帧起」。

    框与第一句字幕同时出现，所以取**第一条文字事件的开始时刻**换算成帧号、向上取整
    （2.72 秒 × 30 fps → 第 82 帧，即 2.7333 秒）。取不到时间轴、或第一句就落在开头
    （或更早）时返回 0 —— 那表示没有可延后的区间，退回「一张图从头用到尾」。

    时刻的来源是 `timings`，与字幕事件、说话人指示同一个出处：不在这里另算一份，
    否则「框比字早一帧/晚一帧」这类错位会从两处口径的差里长出来。
    """
    if not timings:
        return 0
    try:
        t0 = float((timings[0] or {}).get("start") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0
    if t0 <= 0:
        return 0
    # round 到微秒再取整，免得 81.00000000000001 被算成第 82 帧
    return max(0, int(math.ceil(round(t0 * int(fps), 6))))


def segment_blocker(cfg, box_frame=0):
    """能不能切段并行；不能就返回一句人话说明原因（空串表示能）。

    链上只依赖「静态背景 + 绝对时间」才切得动。几类东西会把它变成跨帧依赖：

    · **动画档**：`kenburns` 用 `zoompan` 的输出帧序号驱动缩放曲线，`waveform` /
      `spectrum` 直接吃音频流——两者都按"从第 0 帧起"算，切段后每段都会从头再来。
    · **立绘**：`overlay` 的 `enable=between(t,…)` 用的是绝对时间，切段本身没事，
      但它跟动画一样会引入额外输入，值不值得为它冒风险，不如一律不切。
    · **框要延后出现**（`box_frame > 0`）：背景由两张图按帧接成一条，而分段的
      `trim=start=秒:end=秒` 切的是**已经拼好的**时间轴——两张图一接，时间轴就重排了，
      段内网格与拼接网格必然错位。两者不并存，这里明确拦下而不是让它半对半错地跑。
    """
    if int(box_frame or 0) > 0:
        return "背景按帧分成两张接起来（字幕框延后出现），段内时间轴会与拼接网格错位"
    mode = str(cfg.get("animation.mode", "kenburns"))
    if mode != "static":
        return "动画档 %s 按帧序号/音频流算，切段会各段从头" % mode
    if _portrait_files(cfg):
        return "配了立绘，overlay 依赖绝对时间与额外输入"
    return ""


def cut_points(duration, fps, workers, timings):
    """把时长切成 workers 段，返回 [(t0, t1, 帧数), ...]。

    切点**帧对齐**：段边界落在整数帧上，接起来与"一次渲到底"逐帧相同。
    再**就近吸附到句间空隙**（半径 SEG_SNAP 秒）——切在句子中间其实无害（libass
    每帧都按绝对时间算，`\\move` 也不会跨段重算），但让段边界落在句间读起来更整齐，
    也免得某段开头正好压着半句话。吸不到就硬切，不回退、不报错。
    """
    total = int(round(float(duration) * int(fps)))
    floor_frames = int(fps)          # 每段至少 1 秒，否则不值得切
    if workers <= 1 or total < workers * floor_frames:
        return [(0.0, float(duration), total)]

    spans = [(float(t.get("start", 0.0)), float(t.get("end", 0.0)))
             for t in (timings or [])]

    def in_gap(t):
        return not any(s + 1e-3 < t < e - 1e-3 for s, e in spans)

    cuts = [0]
    reach = int(round(SEG_SNAP * fps))
    for i in range(1, workers):
        ideal = int(round(total * i / float(workers)))
        pick = ideal if in_gap(ideal / float(fps)) else None
        if pick is None:
            for d in range(1, reach + 1):
                for f in (ideal - d, ideal + d):
                    if cuts[-1] + 1 <= f <= total - 1 and in_gap(f / float(fps)):
                        pick = f
                        break
                if pick is not None:
                    break
        if pick is None:
            pick = ideal
        cuts.append(max(cuts[-1] + 1, min(total - 1, pick)))
    cuts.append(total)
    return [(cuts[i] / float(fps), cuts[i + 1] / float(fps),
             cuts[i + 1] - cuts[i]) for i in range(len(cuts) - 1)]


def _graph(cfg, width, height, fps, duration, ass_path, font_dir, timings,
           portrait_slots, pre="", tail="", box_split=None, audio_idx=1):
    """滤镜链。串行与分段**共用这一套**，免得两条路各写一份、日后各自跑偏。

    `pre` 挂在背景归一之前，`tail` 挂在最末。分段时 `pre="trim=start=…:end=…,"`：
    `trim` 只切时间窗、**不动时间戳**，所以 `ass` 里的字幕事件、`\\move` 的插值、
    `enable=between(t,…)` 拿到的仍然是整条时间轴上的绝对时刻——段与段各画各的，
    接起来与一次渲到底逐帧相同。`tail` 再用 `setpts=PTS-STARTPTS` 把段首归零，
    编码器要 0 基时间戳。

    `box_split` 是 `(无框图输入下标, 有框图输入下标, N0)` —— 给了它表示**背景由两张图
    接成**：前 N0 帧用「压暗但无框」那张，其余用「压暗 + 框」那张，于是字幕框与第一句
    字幕同时出现（N0 见 `box_onset_frames`）。`None` 是常态：一张图从头用到尾。

    切两段用 `trim` 的 `start_frame` / `end_frame`，**不用 `-t 秒数`**：`-t` 在帧率除
    不尽时落在「第 N 帧算不算在内」的浮点边界上（2.72 秒 × 30 fps = 81.6，到底 82 还是
    83 帧说不准），而 `trim` 按帧号数，数几是几。

    四档动画接在这条缝上的原因各不同：`static` 的 scale/crop 逐帧无状态，接在哪都等价；
    `kenburns` 的缩放曲线由输出帧序号 `on` 驱动，两段各自 zoompan 时第二段**必须带帧
    偏移 N0**，否则接缝处缩放会跳回起点；`waveform` / `spectrum` 是叠在背景上的独立
    一层，背景接成一条即可。
    """
    mode = cfg.get("animation.mode", "kenburns")
    accent = "C9A45C"
    frames = max(1, int(round(duration * fps)))
    norm = ("%sscale=%d:%d:force_original_aspect_ratio=increase,"
            "crop=%d:%d,setsar=1" % (pre, width, height, width, height))

    chain = []
    split_anim = False          # 动画是否已经并进背景拼接（kenburns 那一路）
    if box_split:
        p_idx, b_idx, n0 = (int(box_split[0]), int(box_split[1]),
                            int(box_split[2]))
        if mode == "kenburns":
            k = max(0.0, float(cfg.get("animation.zoom_max", 1.04)) - 1.0)
            zp = ("zoompan=z='1+%.4f*%s/%d':d=%d:s=%dx%d:fps=%d:"
                  "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
            # 缓推档的背景是**单帧输入**（见 bg_wants_single_frame），两张图各取第 0 帧，
            # 交给 zoompan 生成各自那一段。缩放曲线按输出帧序号算，所以第二段的表达式
            # 要带上 `(on+N0)`——不带这个偏移，接缝处缩放会跳回起点重推一遍。
            chain.append("[%d:v]trim=end_frame=1,setpts=PTS-STARTPTS,%s,%s[a]"
                         % (p_idx, norm,
                            zp % (k, "on", frames, n0, width, height, fps)))
            chain.append("[%d:v]trim=end_frame=1,setpts=PTS-STARTPTS,%s,%s[b]"
                         % (b_idx, norm,
                            zp % (k, "(on+%d)" % n0, frames,
                                  max(1, frames - n0), width, height, fps)))
            split_anim = True
        else:
            chain.append("[%d:v]trim=end_frame=%d,setpts=PTS-STARTPTS,%s[a]"
                         % (p_idx, n0, norm))
            chain.append("[%d:v]trim=start_frame=%d,setpts=PTS-STARTPTS,%s[b]"
                         % (b_idx, n0, norm))
        chain.append("[a][b]concat=n=2:v=1:a=0[bg]")
    else:
        chain.append("[0:v]%s[bg]" % norm)

    anim, _ = ([], []) if split_anim else _animation_filter(
        mode, width, height, fps, duration, accent,
        cfg.get("animation.zoom_max", 1.04), audio_idx)
    if anim:
        chain.extend(anim)
        base_label = "vbase"
    elif split_anim:
        # 动画（缓推的缩放曲线）已经并进上面那两段里了，这里不再插一道直通 null
        base_label = "bg"
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

    # 整幅压暗不在链上：它是静态层，已由 assets_factory.bake_static_layers 画进
    # 传进来的那张背景图里。链上留一份就等于同一件事做两遍，而且第二遍是逐帧的。

    # ASS 必须给绝对路径。字幕由 pipeline 写在过程目录（work），成片落在音视频目录，
    # 两者不同目录；而 ffmpeg 的 cwd 为了产物文件名干净而设成成片目录。若只传
    # basename，libass 会在成片目录里找字幕，报 "ass_read_file(sub.ass): fopen failed"。
    # 绝对路径又要过 _ff_escape：盘符冒号在 filtergraph 里是两级解析，必须转义两次。
    ass_arg = "ass=%s" % _ff_escape(os.path.abspath(ass_path))
    if font_dir:
        ass_arg += ":fontsdir=%s" % _ff_escape(font_dir)
    # `format=rgb24` 这一步不是摆设：**libass 的取色口径由「进到 ass 里的那一帧是什么
    # 格式」决定**，而这不是我们能从 ASS 里控制的。同一份 ASS、同一个像素实测：输入
    # rgb24 时白字出 233、黑描边出 16，输入 yuv420p 时同一处出 253 / 0——差 16～20 个
    # 码值，字幕的亮度肉眼可辨（已发布成片逐帧对过，走的是前者）。
    # 旧链因为压暗必须在 RGB 域做，链上自带 `format=rgb24`，所以字幕一直是前者；压暗
    # 挪去烘焙后这步会跟着消失，字幕会悄悄亮 6%。显式写出来，口径与已发布各期一致。
    # 不额外付代价：整帧 rgb24→yuv420p 的转换两种形态下都只做一次（要么在 ass 前由
    # scale 做，要么在 ass 后由 auto_scale 做）；ass 在 rgb24 上混合的，只是框内那
    # 一小块文字。
    chain.append("[%s]format=rgb24,%s,format=yuv420p%s[vout]" % (cur, ass_arg, tail))
    return chain


def _x264_args(cfg, threads=None):
    """编码参数。`threads` 由 `encoder_threads(workers)` 算出来，见那里的说明。"""
    args = ["-c:v", "libx264",
            "-preset", str(cfg.get("video.encoder_preset", "medium")),
            "-crf", str(int(cfg.get("video.crf", 20)))]
    if threads:
        args += ["-threads", str(int(threads))]
    return args


def _bg_and_audio_inputs(cmd, bg_path, audio_path, mode, duration, width, height,
                         fps, bg_color, bg_plain=None, box_frame=0):
    """背景输入 + 音频输入。返回 (输入个数, 音频输入下标, 是否双背景)。

    静图必须显式给 `-framerate fps`：不给的话 image2 默认 25，而输出是 30，
    中间会白插一层帧率转换——字幕的运动被量化到 25fps，分段渲染时那一层的相位
    还会随每段起点漂移。对齐之后输入网格就是输出网格，帧数可控、切点可预期。

    `bg_plain` 是「压暗但无框」那张图；`box_frame > 0` 时它**排在 `bg_path` 前面**占住
    0 号位（滤镜链按输入下标认背景，顺序反了就会把有框那张当第一段）。两张图都在音频
    之前，所以音频下标从 1 挪到 2 —— 必须返回给调用方，写死 `1:a` 的那一版会去第二张
    背景图上取音频。
    """
    total = max(1, int(round(float(duration) * int(fps))))
    dual = bool(bg_plain and os.path.exists(bg_plain)
                and 0 < int(box_frame or 0) < total)
    if bg_path and os.path.exists(bg_path):
        if bg_wants_single_frame(mode):
            # 缓推档是单帧输入（见 bg_wants_single_frame）：两张图各喂一帧，链上再由
            # zoompan 按各自那一段生成。这里不能加 `-loop 1 -t`——那会让 zoompan 的
            # `d` 对每个输入帧各生成一遍。
            if dual:
                cmd += ["-i", bg_plain, "-i", bg_path]
            else:
                cmd += ["-i", bg_path]
        elif dual:
            cmd += ["-framerate", str(fps), "-loop", "1",
                    "-t", "%.3f" % duration, "-i", bg_plain]
            cmd += ["-framerate", str(fps), "-loop", "1",
                    "-t", "%.3f" % duration, "-i", bg_path]
        else:
            cmd += ["-framerate", str(fps), "-loop", "1",
                    "-t", "%.3f" % duration, "-i", bg_path]
    else:
        dual = False
        cmd += ["-f", "lavfi", "-t", "%.3f" % duration,
                "-i", "color=c=%s:s=%dx%d:r=%d" % (bg_color, width, height, fps)]
    n = 2 if dual else 1
    audio_idx = n
    if audio_path:
        cmd += ["-i", audio_path]
        n += 1
    return n, audio_idx, dual


def ffprobe_nb_frames(path):
    """容器里记的视频帧数。拿不到返回 -1（不猜）。"""
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return int((r.stdout or "").strip())
    except ValueError:
        return -1


def compose(audio_path, ass_path, out_path, cfg, bg_path, duration,
            width, height, suffix="", font_dir=None, timings=None, log=None,
            bg_plain=None, box_frame=0):
    """合成单个视频。时长显式对齐音频；立绘按句时间轴切换。

    进到这里之前，背景应当已经过 `assets_factory.bake_static_layers`——整幅压暗
    与字幕框都是静态层，在那一步一次画进图里了，这里不再逐帧重算（见那里的说明）。

    `bg_plain` + `box_frame` 一起用时背景接成两条：前 `box_frame` 帧用无框图（`bg_plain`）、
    其余用有框图（`bg_path`），于是**字幕框与第一句字幕同时出现**。不传就退回一张图从
    头用到尾，与从前逐字节一致。帧号怎么算见 `box_onset_frames`。

    **默认一次渲到底**。分段并行那条路还在（`_compose_segmented`），但当前
    `render_workers()` 恒返回 1：真素材实测切段只会更慢，原因见那里的说明。
    要重新启用或换链再量，先把 `WORKERS_MAX` 改掉并跑 `tools/probes/` 里的基准。
    """
    log = log or (lambda m: None)
    workers = render_workers()
    why = segment_blocker(cfg, box_frame)
    if workers > 1 and not why:
        return _compose_segmented(audio_path, ass_path, out_path, cfg, bg_path,
                                  duration, width, height, suffix, font_dir,
                                  timings, log, workers)
    if workers > 1:
        log("并行渲染跳过：%s；本条一次渲到底" % why)
    return _compose_single(audio_path, ass_path, out_path, cfg, bg_path, duration,
                           width, height, suffix, font_dir, timings, log,
                           bg_plain=bg_plain, box_frame=box_frame)


def _compose_single(audio_path, ass_path, out_path, cfg, bg_path, duration,
                    width, height, suffix, font_dir, timings, log,
                    bg_plain=None, box_frame=0):
    """一次渲到底：一条 ffmpeg，音频与画面同一条命令。"""
    fps = int(cfg.get("video.fps", 30))
    mode = cfg.get("animation.mode", "kenburns")
    bg_color = cfg.get("bg_color", "0x0F1418")
    out_dir = os.path.dirname(os.path.abspath(out_path))

    cmd = [ffmpeg_bin(), "-y"]
    inputs, audio_idx, dual_bg = _bg_and_audio_inputs(
        cmd, bg_path, audio_path, mode, duration, width, height, fps, bg_color,
        bg_plain=bg_plain, box_frame=box_frame)
    # 双背景占掉 0 / 1 两个输入位（无框图在前、有框图在后），链上按帧号接起来。
    # 是否真走了双背景由上一步回话，这里不重算一遍条件——两处各算一次，迟早对不上。
    box_split = (0, 1, int(box_frame)) if dual_bg else None
    portrait_slots = []
    for sp, p in _portrait_files(cfg):
        cmd += ["-framerate", str(fps), "-loop", "1", "-t", "%.3f" % duration, "-i", p]
        portrait_slots.append((sp, inputs))
        inputs += 1

    chain = _graph(cfg, width, height, fps, duration, ass_path, font_dir,
                   timings, portrait_slots, box_split=box_split,
                   audio_idx=audio_idx)
    cmd += ["-filter_complex", ";".join(chain),
            "-map", "[vout]", "-map", "%d:a" % audio_idx,
            "-t", "%.3f" % duration,
            "-r", str(fps)]
    # 单路不写 `-threads`：让 ffmpeg/x264 自己按机器定，与叠并行之前逐字一致。
    cmd += _x264_args(cfg)
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            os.path.basename(out_path)]

    _run(cmd, cwd=out_dir)
    log("视频已合成：%s" % os.path.basename(out_path))
    return out_path


def _compose_segmented(audio_path, ass_path, out_path, cfg, bg_path, duration,
                       width, height, suffix, font_dir, timings, log, workers):
    """切段并行：每段一条 ffmpeg（只出画面），再拼接、最后与音频合流。

    三个决定：

    · **段里不带音频**：音频单独在最后一步编码，段边界就不会出现 AAC 帧边界
      与编码器 priming 造成的咔哒声，也不会因此跟画面错位。
    · **拼接用 `-c copy`**：段是同一套参数编出来的，接起来不重编、不再损失一代。
    · **合流后断言帧数**：段数×帧率与总帧数对不上就报错，不静默交一条短片。

    失败即抛出（不静默退回串行）：慢一点可以接受，悄悄换一条路走不行。段文件留在
    过程目录里供查；只有整条链成功才清掉。
    """
    fps = int(cfg.get("video.fps", 30))
    mode = cfg.get("animation.mode", "kenburns")
    bg_color = cfg.get("bg_color", "0x0F1418")
    out_dir = os.path.dirname(os.path.abspath(out_path))
    seg_dir = os.path.join(os.path.dirname(os.path.abspath(ass_path)),
                           "_seg%s" % (suffix or ""))
    os.makedirs(seg_dir, exist_ok=True)

    spans = cut_points(duration, fps, workers, timings)
    total_frames = sum(s[2] for s in spans)
    if len(spans) <= 1:
        return _compose_single(audio_path, ass_path, out_path, cfg, bg_path,
                               duration, width, height, suffix, font_dir,
                               timings, log)

    log("并行合成：%d 段 / %d 路并发（设备 %d 逻辑核）"
        % (len(spans), workers, os.cpu_count() or 1))

    def make_cmd(i, t0, t1, nframes):
        seg = os.path.join(seg_dir, "seg%02d.mp4" % i)
        cmd = [ffmpeg_bin(), "-y", "-framerate", str(fps), "-loop", "1",
               "-t", "%.6f" % t1, "-i", bg_path]
        chain = _graph(cfg, width, height, fps, t1 - t0, ass_path, font_dir,
                       timings, [],
                       pre="trim=start=%.6f:end=%.6f," % (t0, t1),
                       tail=",setpts=PTS-STARTPTS")
        cmd += ["-filter_complex", ";".join(chain),
                "-map", "[vout]", "-frames:v", str(nframes), "-r", str(fps)]
        cmd += _x264_args(cfg, encoder_threads(workers))
        cmd += ["-an", os.path.basename(seg)]
        return seg, cmd

    jobs = [make_cmd(i, t0, t1, nf) for i, (t0, t1, nf) in enumerate(spans)]
    _run_parallel(jobs, seg_dir, workers)

    segs = [j[0] for j in jobs]
    list_path = os.path.join(seg_dir, "_concat.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segs:
            f.write("file '%s'\n" % str(p).replace("\\", "/").replace("'", "'" + chr(92) + "'" + "'"))
    join_tmp = os.path.join(seg_dir, "_join.mp4")
    _run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", list_path,
          "-c", "copy", join_tmp], cwd=seg_dir)

    got = ffprobe_nb_frames(join_tmp)
    if got != total_frames:
        raise VideoError("分段拼接后帧数对不上：拼接得 %s 帧，应为 %d 帧（%d 段）。"
                         "段文件留在 %s 供查。" % (got, total_frames, len(segs), seg_dir))

    _run([ffmpeg_bin(), "-y", "-i", join_tmp, "-i", audio_path,
          "-map", "0:v", "-map", "1:a", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-t", "%.3f" % duration,
          "-movflags", "+faststart", os.path.basename(out_path)], cwd=out_dir)

    shutil.rmtree(seg_dir, ignore_errors=True)
    log("视频已合成：%s（%d 段并行）" % (os.path.basename(out_path), len(segs)))
    return out_path


def _run_parallel(jobs, cwd, workers):
    """并发跑多条 ffmpeg。任一条失败即整体失败，并把它自己的报错带出来。"""
    import concurrent.futures as cf
    errs = []
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futs = {pool.submit(_run, cmd, cwd): os.path.basename(seg)
                for seg, cmd in jobs}
        for fu in cf.as_completed(futs):
            try:
                fu.result()
            except Exception as e:            # noqa: BLE001 —— 原样攒起来再抛
                errs.append("%s：%s" % (futs[fu], e))
    if errs:
        raise VideoError("并行渲染有 %d 段失败：%s" % (len(errs), " | ".join(errs)))



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
