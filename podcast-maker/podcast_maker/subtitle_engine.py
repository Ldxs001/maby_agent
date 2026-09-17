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

"""字幕引擎：断行（唯一入口）+ SRT / ASS 生成 + 断词率统计。

断行四级算法（原项目逐字符硬切，实测 393 处折行中 42 处断在词中间）：
    1. token 保护      英文词、数字、连字符词不被切开
    2. 标点优先        优先在句读处断开
    3. 禁则校验        中文避头尾：禁则字不作行首、不作行末
    4. 二分回退        无合法断点时取最近的合法位置，实在无解才硬切并记录

断词率必须为 0，否则产物门禁判 FAIL。
"""

import re

from .config_manager import KINSOKU_HEAD, KINSOKU_TAIL, MODE_SPEC

BREAK_PUNCT = "，。！？；：、）】》”…—"
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.]*")

# 逗号类标点断行后不宜留在行首
_TAIL_ONLY = set("，。！？；：、）》】”…—·")


def _in_latin_token(text, i):
    """切点 i 是否落在西文 token 内部（如 Claude-3.5 中间）。

    用区间判定而不是逐字符判定——`.` 和 `-` 也属于 token 内部字符。
    """
    for m in _LATIN_TOKEN_RE.finditer(text):
        if m.start() < i < m.end():
            return True
    return False


def _legal_break(text, i):
    """切点 i 是否合法。

    优先级最高的一条：**标点之后断行永远合法**——标点是天然断点，
    下一字是不是虚词都不该否掉这个位置。这条不加，逗号后的最佳断点
    会因为「就是……」的「就」被判非法而丢失。
    """
    if i <= 0 or i >= len(text):
        return False
    if _in_latin_token(text, i):
        return False
    if text[i - 1] in BREAK_PUNCT:
        return True
    if text[i] in KINSOKU_HEAD or text[i] in _TAIL_ONLY:
        return False
    if text[i - 1] in KINSOKU_TAIL:
        return False
    return True


def find_break(text, max_chars):
    """在 text 前 max_chars 个字符内找最佳切点。返回切点索引。

    优先级：标点后 + 合法 > 合法 > 硬切
    """
    if len(text) <= max_chars:
        return len(text)

    best_punct = -1
    best_legal = -1
    floor = max(1, int(max_chars * 0.45))
    for i in range(max_chars, floor - 1, -1):
        if not _legal_break(text, i):
            continue
        if best_legal < 0:
            best_legal = i
        if text[i - 1] in BREAK_PUNCT:
            best_punct = i
            break

    if best_punct > 0:
        return best_punct
    if best_legal > 0:
        return best_legal
    return max_chars


def wrap_text(text, max_chars, max_lines=2):
    """把一句台词折成若干行。返回行列表。

    行数上限内尽可能均衡；最后一行允许超出 max_chars（不丢字）。
    """
    text = (text or "").strip()
    if not text:
        return [""]
    if max_lines <= 1 or len(text) <= max_chars:
        return [text]

    lines = []
    rest = text
    guard = 0
    while rest and len(lines) < max_lines - 1 and guard < 8:
        guard += 1
        if len(rest) <= max_chars:
            break
        cut = find_break(rest, max_chars)
        if cut <= 0:
            break
        lines.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        lines.append(rest)
    return lines or [text]


def count_word_breaks(text, max_chars, max_lines=2):
    """统计一处折行里违反禁则的断点数（断词率分子）。"""
    bad = 0
    if len(text) <= max_chars or max_lines <= 1:
        return 0
    rest = text
    guard = 0
    while rest and guard < 8:
        guard += 1
        if len(rest) <= max_chars:
            break
        cut = find_break(rest, max_chars)
        if cut <= 0 or cut >= len(rest):
            break
        if not _legal_break(rest, cut):
            bad += 1
        rest = rest[cut:]
        if len(rest) <= max_chars:
            break
    return bad


def wrap_stats(lines, max_chars, max_lines=2):
    """整篇断行统计：总折行数、违规数、断词率。"""
    total = 0
    bad = 0
    samples = []
    for ln in lines or []:
        text = (ln.get("text") if isinstance(ln, dict) else ln) or ""
        if len(text) <= max_chars or max_lines <= 1:
            continue
        total += 1
        b = count_word_breaks(text, max_chars, max_lines)
        if b:
            bad += b
            if len(samples) < 6:
                samples.append(text[:30])
    return {"wraps": total, "violations": bad,
            "rate": round(bad / total, 4) if total else 0.0,
            "samples": samples}


# ------------------------------------------------------------------ 时间格式
def fmt_srt_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        ms = 999
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def fmt_ass_time(seconds):
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def _ass_color(hex_or_ass, default="&HFFFFFF"):
    """接受 #RRGGBB 或 &HAABBGGRR，统一返回 ASS 的 &HBBGGRR。"""
    v = (hex_or_ass or "").strip()
    if not v:
        return default
    if v.startswith("&H"):
        core = v[2:]
        if len(core) == 8:      # AABBGGRR 去掉 alpha
            core = core[2:]
        return "&H" + core.upper()
    if v.startswith("#") and len(v) == 7:
        return "&H%s%s%s" % (v[5:7].upper(), v[3:5].upper(), v[1:3].upper())
    return default


# ------------------------------------------------------------------ 生成
def build_srt(script, cfg, timings):
    """生成 SRT。timings 为逐句 [{"start","end"}]。"""
    out = []
    for i, item in enumerate(script):
        t = timings[i] if i < len(timings) else {"start": 0.0, "end": 0.0}
        out.append(str(i + 1))
        out.append("%s --> %s" % (fmt_srt_time(t["start"]), fmt_srt_time(t["end"])))
        out.append(item.get("text", ""))
        out.append("")
    return "\n".join(out)


def build_ass(script, cfg, timings, width, height, suffix=""):
    """直接生成 ASS，不再经 ffmpeg 转换（避免样式被改写）。"""
    preset = cfg.get("subtitle.preset", "dual")
    max_lines = MODE_SPEC["subtitle.preset"]["options"].get(preset, {}).get("max_lines", 2)
    show_name = MODE_SPEC["subtitle.preset"]["options"].get(preset, {}).get("show_name", False)
    show_name = show_name or cfg.get("speaker_indicator.mode") == "style"

    font = cfg.get("subtitle.font_family") or "Sans"
    if suffix == "_v":
        size = int(cfg.get("subtitle.font_size_vertical", 40))
        mv = int(cfg.get("subtitle.margin_v_vertical", 220))
    else:
        size = int(cfg.get("subtitle.font_size", 52))
        mv = int(cfg.get("subtitle.margin_v", 90))
    mlr = int(cfg.get("subtitle.margin_lr", 90))
    outline = int(cfg.get("subtitle.outline", 6))
    alpha = int(cfg.get("subtitle.bg_alpha", 128))
    col_a = _ass_color(cfg.get("subtitle.color_a"), "&HFFFFFF")
    col_b = _ass_color(cfg.get("subtitle.color_b"), "&HFFFFFF")
    back = "&H%02X000000" % max(0, min(255, alpha))

    max_chars = max(8, (width - mlr * 2) // max(1, size))

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "PlayResX: %d" % width,
        "PlayResY: %d" % height,
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
    ]
    for name, color in (("Default", col_a), ("SpeakerA", col_a), ("SpeakerB", col_b)):
        header.append(
            "Style: %s,%s,%d,%s,%s,&H00000000,%s,0,0,0,0,100,100,0,0,3,%d,0,2,%d,%d,%d,1"
            % (name, font, size, color, color, back, outline, mlr, mlr, mv)
        )
    header += ["", "[Events]",
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    events = []
    for i, item in enumerate(script):
        if i >= len(timings):
            break
        t = timings[i]
        style = "SpeakerA" if item.get("speaker") == "A" else "SpeakerB"
        if cfg.get("speaker_indicator.mode") == "none":
            style = "Default"
        text = item.get("text", "")
        if show_name:
            who = cfg.get("tts.name_a" if item.get("speaker") == "A" else "tts.name_b", "")
            if who:
                text = "%s：%s" % (who, text)
        lines = wrap_text(text, max_chars, max_lines)
        body = "\\N".join(lines)
        events.append("Dialogue: 0,%s,%s,%s,,0,0,0,,%s"
                      % (fmt_ass_time(t["start"]), fmt_ass_time(t["end"]), style, body))

    return "\n".join(header + events) + "\n", max_chars, max_lines


def probe_timings(script, durations, pause, offset=0.0):
    """由逐句实测时长推出时间轴。offset 为片头占去的秒数。

    带上说话人：立绘与色块层要按句判断该显示谁。
    """
    timings = []
    cursor = float(offset)
    for i, item in enumerate(script):
        d = float(durations[i]) if i < len(durations) else 0.0
        timings.append({"start": cursor, "end": cursor + d,
                        "speaker": item.get("speaker", "A")})
        cursor += d + float(pause)
    return timings


def rescale_timings(timings, derived_end, measured_end):
    """按实测总时长线性校正时间轴。

    concat 逐段按音频帧对齐，累计偏差可达数百毫秒；用它做字幕会让
    越靠后的句子偏得越多。以实测值为准做线性拉伸即可消除累积偏差。
    """
    if not timings or derived_end <= 0 or measured_end <= 0:
        return 1.0, False
    skew = measured_end - derived_end
    if abs(skew) <= 0.25:
        return 1.0, False
    k = measured_end / derived_end
    for t in timings:
        t["start"] = round(t["start"] * k, 3)
        t["end"] = round(t["end"] * k, 3)
    return k, True


def parse_srt(path):
    """读取 SRT，返回 [{"start","end","text"}]（秒）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    blocks = re.split(r"\n\s*\n", raw.strip())
    out = []
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        out.append({"start": start, "end": end, "text": " ".join(lines[2:])})
    return out
