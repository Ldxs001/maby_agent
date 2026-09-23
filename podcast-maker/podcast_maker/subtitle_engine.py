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

"""字幕引擎：断行（唯一入口）+ SRT / ASS / LRC 生成 + 断词率统计。

三种字幕同源：都吃同一份逐句时间轴，差别只在「交出去的东西给谁用」——
SRT 是通用成品（起止时间齐全），ASS 是烧进画面的中间件（带 A/B 双色与断行），
LRC 是音频平台的歌词位（只有起始时间、没有样式）。

断行四级算法（原项目逐字符硬切，实测 393 处折行中 42 处断在词中间）：
    1. token 保护      英文词、数字、连字符词不被切开
    2. 标点优先        优先在句读处断开
    3. 禁则校验        中文避头尾：禁则字不作行首、不作行末
    4. 二分回退        无合法断点时取最近的合法位置，实在无解才硬切并记录

四级算法是**逐点判定**的，跟「折几行」无关——版式这一层只决定「从哪儿开始
找切点」，本文件里两条路都复用它：
    wrap_text       贪心填满：第一行撑满，余数全甩末行（单行 / 双行档）
    wrap_balanced   先定行数再均分，行数由每行容量算出来（歌词档）

断词率必须为 0，否则产物门禁判 FAIL。歌词档必须用 wrap_balanced 那一套切点
统计（见 count_word_breaks 的 balanced 形参），否则算的是另一种折法的账。
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


def _nearest_legal_break(text, pos, want, lo, hi):
    """在 [lo, hi] 里取离 want 最近的合法切点（绝对索引，pos 为起点）。

    向两侧交替外扩：先试目标位置，再试「多切一个字」与「少切一个字」。一边倒的
    搜索会把行越推越偏——只会往小里找，第一行就永远贴不满；只会往大里找，末行
    就永远是个尾巴。
    """
    for d in range(0, hi - lo + 1):
        cands = (want,) if d == 0 else (want + d, want - d)
        for c in cands:
            if lo <= c <= hi and _legal_break(text, pos + c):
                return c
    return hi          # 窗口内没有一个合法位置，只能硬切


def wrap_balanced(text, max_chars):
    """均衡折行：行数由每行容量自己算，各行长度尽量相等。返回行列表。

    与 wrap_text 的差别只有一处「先定什么」：wrap_text 是贪心填满（第一行撑满、
    余数甩末行），40 字一句在 33 字/行下给 33+7；这里是先定行数
    k = ceil(字数 ÷ 每行容量)，再把剩余字数按剩余行数均分（40 字 → 21+19）。

    **行数不设上限**——这一档要的就是「料多长就排多少行」，上限由窗口的行预算
    管（`_lyric_window`），不在这层。所以签名里没有 max_lines：写一个用不上的
    参数，等于给下一个读代码的人留一句谎话。

    切点仍走四级判定（token 保护 → 标点优先 → 避头尾 → 回退）：均分只换了
    「从哪儿开始找」，找出来的位置照样合法。每行都 ≤ max_chars——这是硬保证，
    末行也一样（与 wrap_text「末行允许超出、不丢字」的取舍不同：歌词窗口里
    一行超出画面宽会直接顶到边上，比多一行难看）。
    """
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]
    need = -(-len(text) // max_chars)                 # 放得下所需的最少行数
    out, pos = [], 0
    for i in range(need):
        left = need - i                               # 含本行还剩几行
        rem = len(text) - pos
        if left == 1:
            out.append(text[pos:])
            break
        want = min(max_chars, int(round(rem / float(left))))
        # 本行至少切这么多，剩下 left-1 行才装得下；至多切这么多，剩下每行至少一字
        lo = max(1, rem - (left - 1) * max_chars)
        hi = min(max_chars, rem - (left - 1))
        cut = _nearest_legal_break(text, pos, want, lo, hi)
        out.append(text[pos:pos + cut])
        pos += cut
    return [ln for ln in out if ln] or [text]


def count_word_breaks(text, max_chars, max_lines=2, balanced=False):
    """统计一处折行里违反禁则的断点数（断词率分子）。

    balanced=True 时改按 wrap_balanced 的切点判——统计必须跟实际折行走同一套
    位置，否则报的是「另一种折法」的账：歌词档每行都短，只按长度看永远合格。
    """
    if balanced:
        return _count_balanced_breaks(text, max_chars)
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


def _count_balanced_breaks(text, max_chars):
    """歌词档的违规断点数：拿折行结果反推切点，逐个判合法性。"""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return 0
    rows = wrap_balanced(text, max_chars)
    bad = 0
    at = 0
    for row in rows[:-1]:
        at += len(row)
        if not _legal_break(text, at):
            bad += 1
    return bad


def wrap_stats(lines, max_chars, max_lines=2, balanced=False):
    """整篇断行统计：总折行数、违规数、断词率。"""
    total = 0
    bad = 0
    samples = []
    for ln in lines or []:
        text = (ln.get("text") if isinstance(ln, dict) else ln) or ""
        if len(text) <= max_chars:
            continue
        if max_lines <= 1 and not balanced:
            # 单行档程序不折（wrap_text 原样返回），没有断点可判；歌词档不设上限，
            # 不受这一条影响。
            continue
        total += 1
        b = count_word_breaks(text, max_chars, max_lines, balanced)
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


def fmt_lrc_time(seconds):
    """LRC 时间标签用的 `mm:ss.xx`——秒后是**两位百分秒**，不是三位毫秒。

    先换成百分秒总数再拆位：59.999 这类若先取分秒再算小数，会得到 00:59.99；
    按总百分秒取整进位才是 01:00.00。分钟位允许超过 59（一小时以上照常两位）。
    """
    seconds = max(0.0, float(seconds))
    total = int(round(seconds * 100))
    return "%02d:%02d.%02d" % (total // 6000, (total // 100) % 60, total % 100)


def speaker_name_shown(cfg):
    """说话人名要不要写进字幕文本。ASS 与 LRC 共用这一个判据。

    名字归「说话人提示」卡里的「角色名称」开关，**与版式无关、与提示方式无关**：
    从前它由版式（「双行带名」档）或提示档（「字幕色区分」）兼职决定，于是名字
    在四档之间飘——选侧栏色块或自备立绘反而没了名字。现在只有这一个入口。

    不给 LRC 另立一个开关：LRC 没有样式层承载 A/B 双色，名字是它唯一能区分
    「谁在说」的办法，比画面字幕更需要这一条，而不是需要另一条。

    老配置（升级前落盘、还没有这个键）按旧判据回退，免得升一次级名字突然变样。
    """
    v = cfg.get("speaker_indicator.name_shown", None)
    if v is not None:
        return bool(v)
    if cfg.get("subtitle.preset") == "dual_named":      # 已下线的旧档
        return True
    return cfg.get("speaker_indicator.mode") == "style"


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


def _dim_color(ass_color, alpha=0x8C):
    """把一个 &HBBGGRR 压暗：不换色相，只加不透明度（与画面混成暗色）。

    歌词窗口里「非当前句」用它。为什么不只用「另一个暗色」：本色可能是纯白，
    也可能是人自己配的金/蓝，写死一个暗色只对其中一种好看；加不透明度对任何
    本色都成立。
    """
    core = ass_color[2:] if ass_color.startswith("&H") else ass_color
    return "&H%02X%s" % (max(0, min(255, int(alpha))), core.upper())


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


def build_lrc(script, cfg, timings):
    """生成 LRC（音频平台歌词位认的那一份）。timings 为逐句 [{"start","end"}]。

    与 SRT 同一份时间轴，只取**起始时间**：LRC 没有结束时间，一条从自己的
    时刻显示到下一条为止。所以这里用**原始整句**、不套 ASS 的断行——LRC 一行
    就是一条，照画面宽度断开只会得到「半句配一个时间戳」。

    说话人：判据与 ASS 同源（`speaker_name_shown`），因为 LRC 没有样式可承载
    A/B 双色，名字是唯一能区分谁在说的办法。
    """
    show_name = speaker_name_shown(cfg)
    out = []
    for i, item in enumerate(script):
        t = timings[i] if i < len(timings) else {"start": 0.0, "end": 0.0}
        # 一条一行：句内的换行会把一条歌词劈成两条，只剩第一条带着时间戳
        text = str(item.get("text", "")).replace("\r", " ").replace("\n", " ")
        if show_name:
            who = cfg.get("tts.name_a" if item.get("speaker") == "A" else "tts.name_b", "")
            if who:
                text = "%s：%s" % (who, text)
        out.append("[%s]%s" % (fmt_lrc_time(t["start"]), text))
    return "\n".join(out)


def build_ass(script, cfg, timings, width, height, suffix=""):
    """直接生成 ASS，不再经 ffmpeg 转换（避免样式被改写）。

    **版式在这里分岔**，因为两路的定位方式根本不同：

      单行 / 双行   折行上限写死，一句一条 Dialogue，位置交给 Style 的
                    Alignment + MarginV（静态）——与改动前逐字一致
      歌词         行数自己算（wrap_balanced），窗口里每句各出一条 Dialogue，
                    纵向位置全部自己算 + `\\move` 滚动。**MarginV 是静态的，
                    要滚动就只能走 \\pos/\\move**；而 \\move/\\pos 定位下 libass
                    不再理会 Style 的左右边距，所以歌词档的左右安全宽度由折行
                    保证（每行 ≤ max_chars），不靠边距。

    返回 (ass 文本, 每行容量, 折行上限, 是否均衡折行)。末位给统计用：歌词档的
    断词率要按它自己那套切点算（见 wrap_stats）。
    """
    preset = cfg.get("subtitle.preset", "dual")
    opt = MODE_SPEC["subtitle.preset"]["options"].get(preset, {})
    max_lines = opt.get("max_lines", 2)
    balanced = bool(opt.get("balanced", False))
    show_name = speaker_name_shown(cfg)
    highlight = bool(cfg.get("subtitle.highlight", False))
    hl_color = _ass_color(cfg.get("subtitle.highlight_color"), "&H8AD9FF")

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

    # 歌词档高亮时另加三个样式：当前句一色、上下文按说话人本色压暗。
    # 高亮关掉时不建这三个——三句同色，靠位置区分「读到哪」。
    styles = [("Default", col_a), ("SpeakerA", col_a), ("SpeakerB", col_b)]
    if balanced and highlight:
        styles += [("LyricCur", hl_color),
                   ("LyricCtxA", _dim_color(col_a)),
                   ("LyricCtxB", _dim_color(col_b))]

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
    for name, color in styles:
        header.append(
            "Style: %s,%s,%d,%s,%s,&H00000000,%s,0,0,0,0,100,100,0,0,3,%d,0,2,%d,%d,%d,1"
            % (name, font, size, color, color, back, outline, mlr, mlr, mv)
        )
    header += ["", "[Events]",
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    if balanced:
        events = _lyric_events(script, cfg, timings, width, height, suffix,
                               max_chars, show_name, highlight)
        return "\n".join(header + events) + "\n", max_chars, max_lines, balanced

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
        if highlight:
            # 单行/双行同屏只有一句，「当前句」就是它自己——高亮＝整句换成高亮色，
            # 盖过说话人分色（行内 \\c 覆盖 Style 主色，不必另建样式）。
            body = "{\\c%s&}%s" % (hl_color, body)
        events.append("Dialogue: 0,%s,%s,%s,,0,0,0,,%s"
                      % (fmt_ass_time(t["start"]), fmt_ass_time(t["end"]), style, body))

    return "\n".join(header + events) + "\n", max_chars, max_lines, balanced


# ------------------------------------------------------------------ 歌词档
LYRIC_CONTEXT_ALPHA = 0x8C      # 非当前句的不透明度档（约 45% 可见）
LYRIC_LINE_PITCH = 1.35         # 行距 = 字号 × 本值（折行之间与句间空行同用）


def _lyric_window(n, line_counts, current, window, max_rows):
    """当前句为 current 时，窗口里显示哪几句。返回升序的句序号列表。

    代价按「行」算：**每句 = 1 个空行 + 它自己折的行数**（上限就是这么数的）。
    当前句必留——它自己就超上限时也留，宁可顶出去，也不能把人在读的那句藏起来。
    然后先纳入紧邻的下一句、再纳入紧邻的上一句；某一侧放不下就停在那侧，不跳过
    去捡更远的句子——那样窗口里会出现一个说不清的空洞。
    """
    cost = [c + 1 for c in line_counts]
    vis = [current]
    used = cost[current]
    below, above = current + 1, current - 1
    below_open = above_open = True
    while len(vis) < window and (below_open or above_open):
        if below_open:
            if below < n and used + cost[below] <= max_rows:
                vis.append(below)
                used += cost[below]
                below += 1
            else:
                below_open = False
        if above_open and len(vis) < window:
            if above >= 0 and used + cost[above] <= max_rows:
                vis.append(above)
                used += cost[above]
                above -= 1
            else:
                above_open = False
    return sorted(vis)


def _lyric_style_of(k, current, highlight, speaker, mode):
    """歌词档里某一句用哪个样式。

    当前句：高亮开 → 高亮色；高亮关 → 该句说话人的本色。
    上下文：高亮开 → 本色压暗；高亮关 → 与当前句同色（读到哪只靠位置看）。
    """
    base = "Default" if mode == "none" else ("SpeakerA" if speaker == "A" else "SpeakerB")
    if k == current:
        return "LyricCur" if highlight else base
    if not highlight:
        return base
    return "LyricCtxA" if speaker == "A" else "LyricCtxB"


def _lyric_head(cx, y_from, y_to, scroll_ms, fade_ms=0):
    """一条歌词的定位头：不动就 \\pos，要动就 \\move。

    \\an5 是钉死的：\\pos/\\move 的锚点由 \\an 决定，不写就跟着 Style 的
    Alignment 走，而歌词的 Style 沿用了底中对齐——那样横向居中和纵向锚点会各算
    一遍，锚点越靠上偏得越多。
    """
    if y_from != y_to and scroll_ms > 0:
        head = r"{\an5\move(%d,%d,%d,%d,0,%d)" % (cx, y_from, cx, y_to, scroll_ms)
    else:
        head = r"{\an5\pos(%d,%d)" % (cx, y_to)
    if fade_ms > 0:
        head += r"\fad(0,%d)" % fade_ms
    return head + "}"


def _lyric_events(script, cfg, timings, width, height, suffix,
                  max_chars, show_name, highlight):
    """歌词窗口的事件行：每句在它「在场」的每个区间出一条 Dialogue。

    纵向位置全部自己算。位置只跟句子自身有关（全局行尺），当前句挪一格时所有
    句子的位移量天然相同——整块带子刚性上移，不必逐句去算差值，这也是「跳换 +
    缓慢从下往上滚」能用一条 \\move 表达出来的原因。

    区间按**句首到下一句句首**取（不是句首到句尾），句间停顿里窗口不闪断；
    最后一句止于自己的结束时间。
    """
    n = len(script)
    if n == 0 or not timings:
        return []
    window = max(1, int(cfg.get("subtitle.lyric_window", 3)))
    max_rows = max(2, int(cfg.get("subtitle.lyric_max_rows", 8)))
    scroll_ms = max(0, int(cfg.get("subtitle.lyric_scroll_ms", 600)))
    mode = cfg.get("speaker_indicator.mode", "style")
    if suffix == "_v":
        size = int(cfg.get("subtitle.font_size_vertical", 40))
    else:
        size = int(cfg.get("subtitle.font_size", 52))
    pitch = max(1, int(round(size * LYRIC_LINE_PITCH)))
    anchor = int(cfg.get("subtitle.lyric_anchor_y", 470))
    if suffix == "_v":
        # 锚点按 1080 高的横屏定，竖屏按画幅比例换算——同一套配置两种画幅一致。
        anchor = int(round(anchor * float(height) / 1080.0))
    cx = width // 2

    texts, line_counts = [], []
    for item in script:
        text = item.get("text", "")
        if show_name:
            who = cfg.get("tts.name_a" if item.get("speaker") == "A" else "tts.name_b", "")
            if who:
                text = "%s：%s" % (who, text)
        rows = wrap_balanced(text, max_chars)
        texts.append(rows)
        line_counts.append(len(rows))

    base, row = [], 0.0
    for cnt in line_counts:
        base.append((row + (cnt - 1) / 2.0) * pitch)
        row += cnt + 1                                  # +1 = 句与句之间的空行

    def y_at(k, current):
        """current 为当前句时，第 k 句的纵向中心。"""
        return int(round(anchor + base[k] - base[current]))

    starts = [float(t.get("start", 0.0)) for t in timings[:n]]
    ends = [float(t.get("end", 0.0)) for t in timings[:n]]
    vis = [_lyric_window(n, line_counts, j, window, max_rows) for j in range(n)]

    events = []
    for j in range(n):
        t0 = starts[j]
        t1 = starts[j + 1] if j + 1 < n else max(ends[j], t0 + 0.01)
        for k in vis[j]:
            y_from = y_at(k, j - 1) if j > 0 else y_at(k, j)
            y_to = y_at(k, j)
            body = _lyric_head(cx, y_from, y_to, scroll_ms) + "\\N".join(texts[k])
            style = _lyric_style_of(k, j, highlight, script[k].get("speaker", "A"), mode)
            events.append("Dialogue: 0,%s,%s,%s,,0,0,0,,%s"
                          % (fmt_ass_time(t0), fmt_ass_time(t1), style, body))
        # 被行预算挤出去的那句：让它跟着整块上移并淡掉（硬消失会像画面抖了一下）。
        # 单独一条短事件，上下各差 1 个图层号，跟留下来的句子错开。
        if j > 0 and scroll_ms > 0:
            for k in [x for x in vis[j - 1] if x not in vis[j]]:
                body = (_lyric_head(cx, y_at(k, j - 1), y_at(k, j), scroll_ms,
                                    fade_ms=scroll_ms)
                        + "\\N".join(texts[k]))
                style = _lyric_style_of(k, j - 1, highlight, script[k].get("speaker", "A"), mode)
                events.append("Dialogue: 1,%s,%s,%s,,0,0,0,,%s"
                              % (fmt_ass_time(t0), fmt_ass_time(t0 + scroll_ms / 1000.0),
                                 style, body))
    return events


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
