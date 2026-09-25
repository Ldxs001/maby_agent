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

**画面上的两档只差一个轴**（`subtitle.preset`）：
    lyric    纵轴：折行进框（wrap_balanced），窗口若干句、换句时整块上滚
    single   横轴：不折行，一句一行在框里滚过框口（装不下才滚，见 _strip_events）
两档共用同一个框（左右与底边取自边距、高 = 行数 × 行距）与同一套像素宽度口径
（text_px_width），Style 也只有一套：per-line 盒关掉，框自己画一条。从前的单行 /
双行是另一套模型（per-line 填充块 + Style 静态定位），同一个下拉里塞两套模型。

断行四级算法（原项目逐字符硬切，实测 393 处折行中 42 处断在词中间）：
    1. token 保护      英文词、数字、连字符词不被切开
    2. 标点优先        优先在句读处断开
    3. 禁则校验        中文避头尾：禁则字不作行首、不作行末
    4. 二分回退        无合法断点时取最近的合法位置，实在无解才硬切并记录

四级算法是**逐点判定**的，跟「折几行」无关——只有歌词那一档折行，所以也只有
它折出断点：wrap_balanced 先定行数再均分，行数由每行容量自己算出来，外面不设
上限。横滚档一个断点都没有，断词率天然为 0（见 count_word_breaks 的 axis 形参）。

断词率必须为 0，否则产物门禁判 FAIL。它按**实际折行的那一套切点**统计，否则
算的是另一种折法的账。
"""

import re

from .config_manager import (KINSOKU_HEAD, KINSOKU_TAIL, MODE_SPEC,
                             PARAM_SPEC)

BREAK_PUNCT = "，。！？；：、）】》”…—"
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.]*")

# 逗号类标点断行后不宜留在行首
_TAIL_ONLY = set("，。！？；：、）》】”…—·")

# 屏幕上真实的字宽（× 字号）。实测标定：拿 libass 渲同一段文本的四个字号、
# 量墨迹宽度，四点极差 ≤ 0.0007。**不是 1.0 倍**——按 1.0 估会把字宽算多三成，
# 横滚的终点就跑过框（预览也跟着错）。
#   汉字       0.751   54 字样本，字号 12 / 16 / 20 / 24
#   全角标点   0.820   60 字样本，同上
#   半角       0.438   96 字样本，同上
# 前端预览按同一组常量算（web_ui.estSubW），预览与成片不许各算各的。
CHAR_W_HAN = 0.751
CHAR_W_FULL = 0.820
CHAR_W_HALF = 0.438


def _char_px(ch, size):
    """一个字符占多宽（像素）。分类只看码位，不测字体——换字体就得重标一次。"""
    o = ord(ch)
    if 0x2000 <= o <= 0x206F or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
        return size * CHAR_W_FULL        # 全角标点、连接号、省略号、弯引号
    if o >= 0x2E80:
        return size * CHAR_W_HAN
    return size * CHAR_W_HALF


def text_px_width(text, size):
    """一行文字在画面上占多宽（像素）。横滚的起点、终点、位移全靠它。"""
    return sum(_char_px(ch, size) for ch in (text or ""))


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


def _nearest_legal_break(text, pos, want, lo, hi):
    """在 [lo, hi] 里取离 want 最近的合法切点（绝对索引，pos 为起点）。

    向两侧交替外扩：先试目标位置，再试「多切一个字」与「少切一个字」。一边倒的
    搜索会把行越推越偏——只会往小里找，第一行就永远贴不满；只会往大里找，末行
    就永远是个尾巴。

    **找不到返回 None**，不返回一个「大概齐」的位置。窗口里没有合法切点，说明
    窗口给错了，该由调用方扩窗或加行；在这一层悄悄兜底硬切，等于把「切在词中间」
    藏进一个看不出问题的返回值里——2c / 2d 的两期就是死在这个 `return hi` 上。
    """
    for d in range(0, hi - lo + 1):
        cands = (want,) if d == 0 else (want + d, want - d)
        for c in cands:
            if lo <= c <= hi and _legal_break(text, pos + c):
                return c
    return None


def wrap_balanced(text, max_chars):
    """均衡折行：行数由每行容量自己算，各行长度尽量相等。返回行列表。

    先定行数 k = ceil(字数 ÷ 每行容量)，再把剩余字数按剩余行数均分
    （40 字一句在 33 字/行下得 21+19；贪心填满会得 33+7，头重脚轻）。这是
    歌词档的折法——两档里只有它折行。

    **行数不设上限**——这一档要的就是「料多长就排多少行」，上限由窗口的行预算
    管（`_frame_window`），不在这层。所以签名里没有 max_lines：写一个用不上的
    参数，等于给下一个读代码的人留一句谎话。

    切点仍走四级判定（token 保护 → 标点优先 → 避头尾 → 回退）：均分只换了
    「从哪儿开始找」，找出来的位置照样合法。每行都 ≤ max_chars——这是硬保证，
    末行也一样（歌词框里一行超出画面宽会顶到框边，比多一行难看）。

    **合法是硬约束，均衡只是偏好，行数是下界。** 三者冲突时的次序不能反：
    被 `_in_latin_token` 护住的 token 横跨整个均衡窗口时，窗口里一个合法切点都
    没有——从前那版在这里兜底硬切（`…semantic-` / `split`、`…RAG Assista` / `nt`）。
    现在的做法是把窗口放开到整行：宁可这一行短一点、后面多占一行，也不把词切开。
    只有整行只装得下一个超长 token 时才硬切——那时确实无点可切。
    """
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]
    out, pos, n = [], 0, len(text)
    while pos < n:
        rem = n - pos
        if rem <= max_chars:
            out.append(text[pos:])
            break
        left = -(-rem // max_chars)                    # 含本行，装下余料至少要几行
        want = min(max_chars, int(round(rem / float(left))))
        # 本行至少切这么多，剩下 left-1 行才装得下；至多切这么多，剩下每行至少一字
        lo = max(1, rem - (left - 1) * max_chars)
        cut = _nearest_legal_break(text, pos, want, lo, max_chars)
        if cut is None:
            # 均衡窗口里没有合法切点：放开到整行找最近的合法点，行数由「下界」
            # 变「下界 + 1」——多一行是版式问题，切在词中间是读错意思。
            cut = _nearest_legal_break(text, pos, want, 1, max_chars)
        if cut is None:
            cut = max_chars                             # 整行就是一个超长 token
        out.append(text[pos:pos + cut])
        pos += cut
    return [ln for ln in out if ln] or [text]


def count_word_breaks(text, max_chars, axis="y"):
    """统计一处折行里违反禁则的断点数（断词率分子）。

    axis="y"（歌词档）按 wrap_balanced 的切点判——统计必须跟实际折行走同一套
    位置，否则报的是「另一种折法」的账：歌词档每行都短，只按长度看永远合格。

    axis="x"（单行滚动档）恒为 0：这一档不折行，一句一行整句滚过框口，一个断点
    都不存在。从前这里靠「max_lines <= 1 就跳过」把单行档漏出统计，那是个巧合
    ——横滚档真正的原因是**没有折行这回事**，不是行数上限等于 1。
    """
    if axis == "x":
        return 0
    return _count_balanced_breaks(text, max_chars)


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


def wrap_stats(lines, max_chars, axis="y"):
    """整篇断行统计：总折行数、违规数、断词率。

    横滚档（axis="x"）不折行：一句一行滚过去，没有任何断点，也就没有断词率。
    在这一档报「折行 N 处」是假账——那些句子一行都没折。
    """
    if axis == "x":
        return {"wraps": 0, "violations": 0, "rate": 0.0, "samples": []}
    total = 0
    bad = 0
    samples = []
    for ln in lines or []:
        text = (ln.get("text") if isinstance(ln, dict) else ln) or ""
        if len(text) <= max_chars:
            continue
        total += 1
        b = count_word_breaks(text, max_chars, axis)
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


def fmt_second_time(seconds):
    """整秒 TXT 用的 `mm:ss`——LRC 的 `mm:ss.xx` 四舍五入到秒，别处同口径。

    **不写成 `round(seconds)`**：Python 的 `round` 是银行家舍入，`round(2.5)` 得 2，
    而 `[00:02.50]` 按「四舍五入」必须进到第 3 秒。走整数半加：先量化到**与
    `fmt_lrc_time` 同一颗百分秒**，再 `+50 // 100` —— 两条路落在同一根数轴上，
    不会一条进位一条不进（`tests/test_subtitle_txt.py` 锁这条同源律）。

    分钟位与 `fmt_lrc_time` 一样补零两位、允许超过 59（一小时以上照常两位）。
    """
    seconds = max(0.0, float(seconds))
    total = (int(round(seconds * 100)) + 50) // 100
    return "%02d:%02d" % (total // 60, total % 60)


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


def _build_lyrics(script, cfg, timings, fmt, bracket=True):
    """歌词类产物的共用主体——LRC、整秒 TXT、洁版 TXT 只差时间戳怎么写。

    同一条歌词不该有三条生成路径：它们取同一份 script、同一份 timings、同一个
    说话人判据（`speaker_name_shown`）、同一套「一条一行」清理（句内换行换成空格，
    否则一条歌词被劈成两条、只剩第一条带时间戳）。只把**时间戳怎么写**当参数传
    进来——`fmt` 管精度，`bracket` 管要不要方括号（只有洁版不要）。「同一份源」
    因此是代码级的事实，而不是三处各写一遍、等着哪天漂开。
    """
    show_name = speaker_name_shown(cfg)
    out = []
    for i, item in enumerate(script):
        t = timings[i] if i < len(timings) else {"start": 0.0, "end": 0.0}
        # 一条一行：句内的换行会把一条歌词劈成两条，只剩第一条带着时间戳
        one_line = dict(item)
        one_line["text"] = (str(item.get("text", ""))
                            .replace("\r", " ").replace("\n", " "))
        stamp = fmt(t["start"])
        body = _caption_text(cfg, one_line, show_name)
        out.append("[%s]%s" % (stamp, body) if bracket
                   else "%s%s" % (stamp, body))
    return "\n".join(out)


def build_lrc(script, cfg, timings):
    """生成 LRC（音频平台歌词位认的那一份）。timings 为逐句 [{"start","end"}]。

    与 SRT 同一份时间轴，只取**起始时间**：LRC 没有结束时间，一条从自己的
    时刻显示到下一条为止。所以这里用**原始整句**、不套 ASS 的断行——LRC 一行
    就是一条，照画面宽度断开只会得到「半句配一个时间戳」。

    说话人：判据与 ASS 同源（`speaker_name_shown`），因为 LRC 没有样式可承载
    A/B 双色，名字是唯一能区分谁在说的办法。
    """
    return _build_lyrics(script, cfg, timings, fmt_lrc_time)


def build_txt(script, cfg, timings):
    """生成整秒 TXT：`[mm:ss]` 一行一条，与 LRC 逐字同源、只差时间戳精度。

    LRC 的百分秒是给播放器的（逐句精确对齐）；对**读它的人与机器**它是噪声——
    行对照、检索、外部工具要的是「第几秒开始」。这份就是那个视角的产物：同一份
    源、同一次生成，只把时间戳四舍五入到秒。

    时间戳口径与 LRC 逐条一致（见 `fmt_second_time`）：把 LRC 那份交给离线取整
    工具，结果应与本函数输出逐字节相同（`tests/test_subtitle_txt.py` 锁这一条）。
    """
    return _build_lyrics(script, cfg, timings, fmt_second_time)


def build_clean_txt(script, cfg, timings):
    """生成洁版 TXT：整秒 TXT 摘掉时间戳的方括号，其余逐字相同。

    这一份是给**读它的人**的：`[00:03]` 的方括号在逐行阅读、复制粘贴、喂给外部
    工具时都是噪声。摘掉之后时间戳仍是定宽的一列、与文本之间不留分隔
    （`00:03小美：…`），列照旧对得齐，又少两个字符。

    与 `build_txt` 同源、同一次生成、同一个 `fmt_second_time`——**不是**读磁盘上
    那份 `.txt` 再拿掉括号：读文件会把「txt 必须先存在」变成隐含依赖，目录里躺着
    上一次留下的旧文件时还会静默产出错内容。节奏也得一样：取的是**起始时间**、
    一行一条、说话人判据同源（`tests/test_subtitle_txt.py` 锁这条同源律）。
    """
    return _build_lyrics(script, cfg, timings, fmt_second_time, bracket=False)


def build_ass(script, cfg, timings, width, height, suffix=""):
    """直接生成 ASS，不再经 ffmpeg 转换（避免样式被改写）。

    **两档共用一套定位方式**：一个固定宽高的框（`frame_geometry`），文字全走
    `\\pos` / `\\move` + `\\clip`。**MarginV 是静态的，要滚动就只能走定位标签**；
    而 `\\move`/`\\pos` 定位下 libass 不再理会 Style 的左右边距，所以框内文字的
    安全宽度由几何保证——歌词档靠折行（每行 ≤ 每行容量），横滚档靠裁切。

    两档的差别只有**轴**（见 MODE_SPEC 的档位表）：歌词档框内纵向排布、换句时
    整块上滚；单行滚动档一句一行、装不下就在框里横向滚过。Style 也只有一套：
    per-line 盒关掉（BackColour 全透明、BorderStyle=1），框由烘焙自己画一条
    （见 `frame_box_paint` 与 `assets_factory.bake_static_layers`）——它不再是一条
    ASS 事件，所以这里也不产出框事件。

    返回 (ass 文本, 每行容量, 轴)。后两位给统计用：歌词档的断词率要按它自己那套
    切点算，横滚档不折行（见 wrap_stats）。
    """
    _preset, opt = preset_of(cfg)
    axis = str(opt.get("axis", "y"))
    show_name = speaker_name_shown(cfg)
    highlight = bool(cfg.get("subtitle.highlight", False))
    hl_color = _ass_color(cfg.get("subtitle.highlight_color"), "&H8AD9FF")

    font = cfg.get("subtitle.font_family") or "Sans"
    window = frame_window_of(cfg)
    g = frame_of(cfg, width, height, suffix)
    size, mlr = g["size"], g["mlr"]
    outline = int(cfg.get("subtitle.outline", 2))
    col_a = _ass_color(cfg.get("subtitle.color_a"), "&HFFFFFF")
    col_b = _ass_color(cfg.get("subtitle.color_b"), "&HFFFFFF")

    max_chars = max(8, (g["x2"] - g["x1"]) // max(1, size))

    # 高亮要有「当前句」这一色；歌词档另建两个上下文色（本色压暗），横滚档屏幕上
    # 只有当前句、没有上下文可言，不建那两个。
    styles = [("Default", col_a), ("SpeakerA", col_a), ("SpeakerB", col_b)]
    if highlight:
        styles.append(("FrameCur", hl_color))
        if axis == "y":
            styles += [("FrameCtxA", _dim_color(col_a)),
                       ("FrameCtxB", _dim_color(col_b))]

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        # **折行只由我们决定，libass 不许插手**：2 = 不做自动折行，只在 `\N` 处断。
        # 0（smart wrap）会让 libass 把宽于可用宽的文字自己折成两行，实测后果就是
        # 「单行滚动档出现双行」：整句连说话人名 2458 px 宽、框内可用宽只有 1740 px，
        # libass 折成两行，两行都落在 70 px 高的框里，于是屏幕上真的并排两行小字。
        # `\clip` 只裁像素、拦不住折行，所以这不是裁切能解决的问题。
        # 本引擎的文字全是自己拼的、断点全由 `_frame_window` 的 `\N` 给出，关掉自动
        # 折行不会有任何正面损失；反过来说，让渲染器保留「替我们折行」的权力，就是
        # 把同一个决定交给两处去下。
        "WrapStyle: 2",
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
    # Style 只有一套：框是**烘焙时独立画出来的一个矩形**（`frame_box_paint` →
    # `assets_factory.bake_static_layers`），所以
    # per-line 盒必须关掉——BackColour 全透明、BorderStyle=1（不做自动盒）、
    # Outline 即框描边宽（subtitle.outline，两档共用），免得叠出一层多余的盒。
    # MarginV 是静态定位的遗留：两档的纵向位置都由定位标签自己算。
    for name, color in styles:
        header.append(
            "Style: %s,%s,%d,%s,%s,&H00000000,&HFF000000,0,0,0,0,100,100,0,0,1,%d,0,2,%d,%d,%d,1"
            % (name, font, size, color, color, outline, mlr, mlr, g["mv"])
        )
    header += ["", "[Events]",
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    opts = {"show_name": show_name, "highlight": highlight,
            "mode": cfg.get("speaker_indicator.mode", "style")}
    if axis == "x":
        events = _strip_events(script, cfg, timings, g, opts)
    else:
        events = _frame_events(script, cfg, timings, g, max_chars, window,
                               g["rows"], opts)
    return "\n".join(header + events) + "\n", max_chars, axis


# ------------------------------------------------------------- 框（两档共用）
FRAME_LINE_PITCH = 1.35         # 行距 = 字号 × 本值（折行之间、句间空行、框高同用）
FRAME_BOX_FILL = (0x00, 0x00, 0x00)     # 框底：纯黑，靠不透明度调深浅
FRAME_BOX_BORDER_COLOR = (0xFF, 0xFF, 0xFF)   # 描边：淡白，只留一道边
FRAME_BOX_BORDER_ALPHA = 0x66   # ASS 口径的透明度；约 60% 不透明


def preset_of(cfg):
    """版式档：返回 (档位名, 档位定义)。

    认不出的值一律按默认档走——老项目清单里还存着已删除的 `dual`，界面上那个
    下拉在新配置里也不会给出它。默认值只有一处出处（PARAM_SPEC），这里不写
    第二份名单。
    """
    opts = MODE_SPEC["subtitle.preset"]["options"]
    name = str(cfg.get("subtitle.preset") or "")
    if name not in opts:
        name = PARAM_SPEC["subtitle.preset"]["default"]
    return name, opts[name]


def frame_geometry(cfg, width, height, suffix, frame_rows):
    """字幕框的几何（**唯一出处**）：左右收 margin_lr、底距画面底 mv、
    高 = 行数 × 行距。两档共用这一个矩形，差别只在文字往哪个方向走。

    前端预览照同一组数画（`web_ui.paintSubPreview`），烘焙也照同一组数画进
    背景图（`assets_factory.bake_static_layers`）——三处不许各算各的。
    """
    if suffix == "_v":
        size = int(cfg.get("subtitle.font_size_vertical", 40))
        mv = int(cfg.get("subtitle.margin_v_vertical", 220))
    else:
        size = int(cfg.get("subtitle.font_size", 52))
        mv = int(cfg.get("subtitle.margin_v", 90))
    mlr = int(cfg.get("subtitle.margin_lr", 90))
    pitch = max(1, int(round(size * FRAME_LINE_PITCH)))
    x1, x2 = mlr, width - mlr
    bottom = height - mv
    rows = max(1, int(frame_rows))
    h = rows * pitch
    return {"size": size, "mv": mv, "mlr": mlr, "pitch": pitch, "rows": rows,
            "x1": x1, "x2": x2, "bottom": bottom, "top": bottom - h, "h": h,
            "anchor": bottom - h / 2.0, "cx": width // 2}


def frame_rows_of(cfg):
    """框排几行。档位表里写死的优先——单行滚动按定义就是 1 句 1 行，给它一个
    可调的「框排几行」等于摆一个死旋钮；没写的取配置。两档共用一套旋钮，
    切档位时值跟着档位走。"""
    _name, opt = preset_of(cfg)
    return max(1, int(opt.get("frame_rows", cfg.get("subtitle.frame_rows", 8))))


def frame_window_of(cfg):
    """框里最多同时显示几句。出处同上：档位写死的优先，再退到配置。"""
    _name, opt = preset_of(cfg)
    return max(1, int(opt.get("window", cfg.get("subtitle.window", 3))))


def frame_of(cfg, width, height, suffix=""):
    """当前档位在这张画幅下的框几何。ASS 与烘焙都从这里取，不许各自算。"""
    return frame_geometry(cfg, width, height, suffix, frame_rows_of(cfg))


def tv_range_color(color):
    """8 bit **全范围**颜色 → ffmpeg 真画到画面上的那个**有限范围**（studio swing）值。

    ffmpeg 的 `ass` 滤镜把字幕颜色当视频色处理，逐通道按 `Y = 16 + 219·c/255`
    映射：黑写成 16、白写成 235、纯红写成 (235,16,16) 而不是 (255,0,0)。这是这条
    链上既成的事实，不是我们的选择——实测过 7 组颜色（黑／白／纯红／纯绿／纯蓝／
    中灰／青）与 4 档不透明度，**逐个吻合**（`tools/probes/bake_static_check.py`
    的 `--calibrate`）。

    烘焙走 Pillow、直接写 8 bit RGB，不做这一步映射就会因为「换谁来画」而换一个
    颜色：框底比现在深 8、描边比现在亮 20（灰度 128 底上实测 64/204 对 72/192）。
    只动颜色，alpha 口径不变。
    """
    return tuple(min(255, max(0, int(round(16.0 + 219.0 * v / 255.0))))
                 for v in color)


def frame_box_paint(g, alpha, outline):
    """框怎么画（**唯一出处**）：一个矩形 + 填充不透明度 + 描边宽 + 两个颜色。

    从前这是 ASS 里的一条 `\\p1` 绘图事件，现在改由 Pillow 一次画进背景图
    （见 `assets_factory.bake_static_layers`）——画的时机变了，参数与矩形没有
    变，所以把参数抽在这里，两边都从这里取。

    两个颜色出的是 **ffmpeg 有限范围**口径（见 `tv_range_color`）：画到画面上的
    黑是 16、白是 235，不是 0/255。直接拿 0/255 去画，成片会比现在偏深偏亮。

    ASS 的 alpha 是**透明度**（0 不透明、255 全透），Pillow 要的是不透明度，
    在这里换算一次；换个画布就换个口径写两份，迟早对不上。

    描边是**只往外扩整宽**，不是骑在轮廓上：把旧框单独渲在纯灰底上量过——
    框矩形 x=[90,1830] y=[920,990]、`\\bord2` 时，描边落在 x=88/89 与
    x=1830/1831、y=918/919 与 y=990/991，内部 [90,1830)×[920,990) 是纯填充
    （灰 128 → 填充 64、描边 203，与「黑 50% 覆盖」「白 60% 覆盖」逐个吻合）。
    照着「骑在轮廓上、各出一半」去实现，四边会各差一个像素。
    """
    a = max(0, min(255, int(alpha)))
    bw = max(0, int(outline))
    return {
        "rect": (g["x1"], g["top"], g["x2"], g["bottom"]),
        "fill": tv_range_color(FRAME_BOX_FILL),
        "fill_alpha": int(round(255 * (1.0 - a / 255.0))),
        "width": bw,
        "out": bw,
        "border": tv_range_color(FRAME_BOX_BORDER_COLOR),
        "border_alpha": int(round(255 * (1.0 - FRAME_BOX_BORDER_ALPHA / 255.0))),
    }


def _caption_text(cfg, item, show_name):
    """一句字幕的最终文本：需要时把说话人名缀在句首（唯一出处）。"""
    text = item.get("text", "")
    if show_name:
        who = cfg.get("tts.name_a" if item.get("speaker") == "A" else "tts.name_b", "")
        if who:
            text = "%s：%s" % (who, text)
    return text


def _frame_window(n, line_counts, current, window, max_rows):
    """歌词档：当前句为 current 时，框里显示哪几句。返回升序的句序号列表。

    代价按「行」算：**每句 = 1 个空行 + 它自己折的行数**（框高就是这么数的）。
    当前句必留——它自己就超上限时也留，宁可顶出去，也不能把人在读的那句藏起来。
    然后先纳入紧邻的下一句、再纳入紧邻的上一句；某一侧放不下就停在那侧，不跳过
    去捡更远的句子——那样框里会出现一个说不清的空洞。
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


def _frame_style_of(k, current, highlight, speaker, mode):
    """某一句用哪个样式。

    当前句：高亮开 → 高亮色；高亮关 → 该句说话人的本色。
    上下文：高亮开 → 本色压暗；高亮关 → 与当前句同色（读到哪只靠位置看）。
    """
    base = "Default" if mode == "none" else ("SpeakerA" if speaker == "A" else "SpeakerB")
    if k == current:
        return "FrameCur" if highlight else base
    if not highlight:
        return base
    return "FrameCtxA" if speaker == "A" else "FrameCtxB"


def _frame_head(x_from, y_from, x_to, y_to, t1_ms, t2_ms, fade_ms=0, clip=None):
    """一条字幕的定位头：不动就 ``pos``，要动就 ``move``；给了框四角就裁到框内。

    四坐标而不是「横向中心 + 纵向两点」：两档都会动，只是方向不同——歌词档纵向
    上滚（横向不动），横滚档横向滑过（纵向不动）。同一段 ``move`` 把两种动都
    表达得下，就不必为横滚另造一套头部。

    ``an5`` 是钉死的：定位标签的锚点由它决定，不写就跟着 Style 的 Alignment 走，
    而字幕 Style 沿用了底中对齐——那样横向居中和纵向锚点会各算一遍，锚点越靠上
    偏得越多。

    ``clip`` 用**画面绝对坐标**（不受位移标签影响），所以直接传框的四角即可：
    出了框的字被裁掉、不会飘到框外。

    t1_ms / t2_ms 是位移标签的起止时刻（相对本条 Dialogue 的毫秒数）。t1 > 0 就是
    「先静止一段再动」——横滚档的句首静止靠它（见 _strip_events）。
    """
    if (x_from, y_from) != (x_to, y_to) and t2_ms > t1_ms:
        head = r"{\an5\move(%d,%d,%d,%d,%d,%d)" % (x_from, y_from, x_to, y_to,
                                                   t1_ms, t2_ms)
    else:
        head = r"{\an5\pos(%d,%d)" % (x_to, y_to)
    if fade_ms > 0:
        head += r"\fad(0,%d)" % fade_ms
    if clip:
        head += r"\clip(%d,%d,%d,%d)" % clip
    return head + "}"


def _frame_events(script, cfg, timings, g, max_chars, window, max_rows, opts):
    """歌词档（纵轴）的事件行：每句在它「在场」的每个区间出一条 Dialogue。

    纵向位置全部自己算。位置只跟句子自身有关（全局行尺），当前句挪一格时所有
    句子的位移量天然相同——整块带子刚性上移，不必逐句去算差值，这也是「跳换 +
    缓慢从下往上滚」能用一条位移标签表达出来的原因。

    纵向基准是一块**固定宽高的框**（见 frame_geometry）：框底距画面底 mv、
    框高 = 行预算 × 行距，当前句落在框的垂直中心。

    区间按**句首到下一句句首**取（不是句首到句尾），句间停顿里框不闪断；
    最后一句止于自己的结束时间。
    """
    n = len(script)
    if n == 0 or not timings:
        return []
    window = max(1, int(window))
    max_rows = max(2, int(max_rows))
    scroll_ms = max(0, int(cfg.get("subtitle.scroll_ms", 600)))
    mode, highlight = opts["mode"], opts["highlight"]
    pitch, anchor, cx = g["pitch"], g["anchor"], g["cx"]
    clip = (g["x1"], g["top"], g["x2"], g["bottom"])

    texts, line_counts = [], []
    for item in script:
        rows = wrap_balanced(_caption_text(cfg, item, opts["show_name"]), max_chars)
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
    vis = [_frame_window(n, line_counts, j, window, max_rows) for j in range(n)]

    # 框不在 ASS 里：它是静态的，已由 assets_factory.bake_static_layers 一次画进
    # 背景图。这里只出文字事件，图层号从 1 起（0 留给将来的垫底层）。
    events = []
    for j in range(n):
        t0 = starts[j]
        t1 = starts[j + 1] if j + 1 < n else max(ends[j], t0 + 0.01)
        for k in vis[j]:
            y_from = y_at(k, j - 1) if j > 0 else y_at(k, j)
            y_to = y_at(k, j)
            body = (_frame_head(cx, y_from, cx, y_to, 0, scroll_ms, clip=clip)
                    + "\\N".join(texts[k]))
            style = _frame_style_of(k, j, highlight, script[k].get("speaker", "A"), mode)
            events.append("Dialogue: 1,%s,%s,%s,,0,0,0,,%s"
                          % (fmt_ass_time(t0), fmt_ass_time(t1), style, body))
        # 被框高挤出去的那句：让它跟着整块上移并淡掉（硬消失会像画面抖了一下）。
        # 单独一条短事件，图层号最高（2）——正在淡出，压在常驻句之上。
        if j > 0 and scroll_ms > 0:
            for k in [x for x in vis[j - 1] if x not in vis[j]]:
                body = (_frame_head(cx, y_at(k, j - 1), cx, y_at(k, j), 0, scroll_ms,
                                    fade_ms=scroll_ms, clip=clip)
                        + "\\N".join(texts[k]))
                style = _frame_style_of(k, j - 1, highlight, script[k].get("speaker", "A"), mode)
                events.append("Dialogue: 2,%s,%s,%s,,0,0,0,,%s"
                              % (fmt_ass_time(t0), fmt_ass_time(t0 + scroll_ms / 1000.0),
                                 style, body))
    return events


def _strip_events(script, cfg, timings, g, opts):
    """单行滚动档（横轴）的事件行：一句一行，装不下就在框里滚过框口。

    **不折行、不裁字。** 从前单行档把整句交给 Style 静态定位，靠渲染器自动折行
    ——中文没有词间空格，libass 把整句当成一个超长 token（实测它根本不折），于是
    超出画幅的那截被直接裁掉：104 字的一句，画面上只剩中间约 33 字。

    三段式（这是「滚得完」的保证）：
        ① 句首静止  文字左缘贴框左缘（观众先读到开头）
        ② 匀速左移  位移 = 文字宽 − 框宽（只把溢出那截滚进框）
        ③ 句尾静止  文字右缘贴框右缘（结尾留在框里）
    速度 = 文字宽 ÷ 句时长，与朗读同速（视听天然同步，不另设速度旋钮）。于是
        滚动时长 = 句时长 × (文字宽 − 框宽) ÷ 文字宽   ← 恒小于句时长
        句首静止 = 句时长 − 滚动时长 ≈ 框宽 ÷ 速度     ← 恒为正，且与句长无关
    位移恒小于文字总宽，所以**任何长度、任何语速都滚得完**，不需要兜底分支。
    装得下（文字宽 ≤ 框宽）就静止居中，不滚。

    每句独立重置：新句的位置回到「开头对齐框左」，不接上一句滚到的位置（不是
    一条长带子），所以不会出现「新句开场时框里还挂着上一句的尾巴」。
    """
    n = len(script)
    if n == 0 or not timings:
        return []
    mode, highlight = opts["mode"], opts["highlight"]
    size, cx, cy = g["size"], g["cx"], int(round(g["anchor"]))
    box_w = g["x2"] - g["x1"]
    clip = (g["x1"], g["top"], g["x2"], g["bottom"])

    starts = [float(t.get("start", 0.0)) for t in timings[:n]]
    ends = [float(t.get("end", 0.0)) for t in timings[:n]]
    # 框不在 ASS 里，已烘焙进背景图（理由同 _frame_events）。
    events = []
    for j in range(n):
        t0 = starts[j]
        t1 = starts[j + 1] if j + 1 < n else max(ends[j], t0 + 0.01)
        span = max(0.3, t1 - t0)                 # 时长下限防零除（异常输入兜底）
        text = _caption_text(cfg, script[j], opts["show_name"])
        w_t = text_px_width(text, size)
        if w_t <= box_w:
            head = _frame_head(cx, cy, cx, cy, 0, 0, clip=clip)
        else:
            hold = span * box_w / w_t            # 句首静止时长
            head = _frame_head(int(round(g["x1"] + w_t / 2.0)), cy,
                               int(round(g["x2"] - w_t / 2.0)), cy,
                               int(round(hold * 1000)), int(round(span * 1000)),
                               clip=clip)
        style = _frame_style_of(j, j, highlight, script[j].get("speaker", "A"), mode)
        events.append("Dialogue: 1,%s,%s,%s,,0,0,0,,%s"
                      % (fmt_ass_time(t0), fmt_ass_time(t1), style, head + text))
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
