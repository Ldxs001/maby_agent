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

"""素材摄入：粘贴文本 / 上传文件 / 书稿锚点切章。

支持 **md / txt / docx**，不支持 PDF。PDF 是出版格式而不是记录格式：它的
章节结构是排版时投影出来的，要拿回来只能反推，反推就是猜；正文顺序也是排版
顺序而非阅读顺序（双栏、页边注、图注、脚注都按坐标给出）。要排播客，给源文件。

原 `pdftotext` 那条路对中文 PDF 会抽出空串（xpdf 缺 CJK 语言包，报
`Unknown character collection 'Adobe-GB1'`），却报成「可能是扫描件，需要 OCR」
——把人往错误方向引。删掉功能，比留一条会误导人的假路径好。

## 三路结构证据

三种格式能提供的证据强度不同，抽取时**投影成同一套表示**，下游只认一种：

| 路 | 证据 | 来源 | 强度 |
|----|------|------|------|
| A | `#{1,6}` 标记 | md 原生；docx 的 Heading 在此投影 | 强 |
| B | `marks` 样式表 | docx 的 `pStyle="Heading N"`、整段加粗的短行 | 强 / 弱 |
| C | 正文里的编号 | 第X章、1.1、一、、a)、§1、Chapter 1 … | 强 / 中 / 弱 |

**C 路是纯文件（txt）唯一的依靠**，也是 md / docx 的交叉验证。三路各自独立
判定，冲突不在这里抹平——由 `probe` 把差异摆出来给人看。

`marks` 是抽取层交给探查层的证据表。样式信息在抽取时丢掉就再也找不回来，
所以它当场记下；`_relocate()` 按顺序把标题重新对到最终文本的行上，这样
剥离行内标记、压缩空行都不会让它错位。
"""

import os
import re
import zipfile

SUPPORTED_TEXT = (".md", ".markdown", ".txt")
SUPPORTED_DOCX = (".docx",)


class IngestError(RuntimeError):
    """摄入失败。绝不静默返回空内容。"""


# 标题行：`#{1,6}` + 空格 + 标题文字。剥正文标记与列锚点共用同一份判据——
# 两处各写一套正则，迟早有一处先漂，而漂的那个会把锚点吃掉。
_TITLE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# 代码围栏。围栏里的 `#` 是代码注释，不是章节标题；不排掉它，代码片段里
# 一行注释就会被认成一级标题，把它所属的那一章从中间切走。
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


# --------------------------------------------------------------------- 文本
def from_text(text):
    t = (text or "").replace("\r\n", "\n").strip()
    if not t:
        raise IngestError("素材内容为空。")
    return t


def _strip_markdown(text):
    out = []
    for line in text.split("\n"):
        # 标题行原样保留。`#` 是结构锚点，不是装饰：按标题切章全靠它，
        # 剥掉之后 list_anchors() 再也找不到锚点——上传 md 书稿后锚点下拉
        # 只剩「全文」，根因就在这里。剥正文标记不得连带剥掉结构。
        if _TITLE_RE.match(line):
            out.append(line.rstrip())
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", line)
        line = re.sub(r"`(.+?)`", r"\1", line)
        line = re.sub(r"!\[.*?\]\(.*?\)", "", line)
        line = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", line)
        out.append(line)
    # 逐行进出，行数不变——marks 记的行号因此可以对齐。空行压缩放到
    # 各格式自己的收尾里做，做完再 _relocate()。
    return "\n".join(out).strip()


# --------------------------------------------------------------------- 文件
_XML_ENT = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
            ("&apos;", "'"), ("&amp;", "&"))


def _unescape(s):
    for a, b in _XML_ENT:
        s = s.replace(a, b)
    return s


_STYLE_RE = re.compile(r'<w:pStyle[^>]*w:val="([^"]+)"')


def _docx_style(para):
    """段落样式 → (层级, 证据种类)。

    `Heading N` 是作者显式声明的结构，比正文里的编号可靠得多。中文 Word
    另有一套「标题 1」样式名，一并认。
    """
    m = _STYLE_RE.search(para)
    if not m:
        return 0, ""
    v = m.group(1).strip()
    mm = re.match(r"^(?:Heading|heading|Header|标题)[\s\-_]*([1-9])$", v)
    if mm:
        return min(6, int(mm.group(1))), "style"
    return 0, ""


def _is_bold(run):
    m = re.search(r"<w:b\b[^>]*>", run)
    if not m:
        return False
    # `<w:b w:val="0"/>` 与 `w:val="false"` 是「明确不加粗」。
    return not re.search(r'w:val="(?:0|false|off)"', m.group(0))


def _docx_bold_title(para, text):
    """整段加粗 + 短 + 不以句末标点收尾 → 作者拿加粗当标题用了。

    很多人不套 Heading 样式，直接加粗一行当中标题。这是**弱证据**：正文里
    偶尔也会整句加粗，所以只标「疑似」，由探查层交给人的那份表去确认。
    """
    if not text or len(text) > 30:
        return False
    if text[-1] in "。！？；：.!?;:":
        return False
    runs = re.findall(r"<w:r>.*?</w:r>", para, re.S)
    if not runs:
        return False
    flags = [_is_bold(r) for r in runs if "<w:t" in r]
    return bool(flags) and all(flags)


def _read_docx(path):
    """解压 docx，按段落抽出文本与样式证据。返回 (text, marks)。

    只取 `<w:t>` 文本节点会把 `pStyle` 丢掉，docx 就退化成一个更大的 txt。
    所以逐段读：有 Heading 样式就投影成 `#`，整段加粗的短行记进 marks。
    """
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as e:
        raise IngestError("docx 解析失败：%s" % e)

    paras = re.findall(r"<w:p\b.*?</w:p>|<w:p\b[^>]*/>", xml, re.S)
    lines, marks = [], []
    for para in paras:
        text = _unescape("".join(
            re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S))).strip()
        level, evidence = _docx_style(para)
        if level and text:
            lines.append("#" * level + " " + text)
            marks.append({"line": len(lines) - 1, "title": text,
                          "level": level, "evidence": "style",
                          "confidence": 0.95})
            continue
        lines.append(text)
        if text and _docx_bold_title(para, text):
            marks.append({"line": len(lines) - 1, "title": text,
                          "level": 2, "evidence": "bold", "confidence": 0.5})
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise IngestError("docx 中没有抽取到文本。")
    return text, marks


def _bare(line):
    """剥掉 markdown 标记后的行内容，用于让 marks 对上最终文本。"""
    m = _TITLE_RE.match(line)
    return m.group(2).strip() if m else line.strip()


def _relocate(text, marks):
    """把 marks 里的标题按顺序重新对到 text 的行上。

    marks 是在抽取途中记的，那时还没有剥离行内标记、压缩空行，行号会漂。
    标题**顺序**不会变，所以按顺序往后找即可——用行号硬对，压缩一次空行
    就整体错位，而错位后的表现是「标题认得出来、却切不出去」。
    """
    if not marks:
        return []
    lines = text.split("\n")
    out, cur = [], 0
    for mk in marks:
        title = (mk.get("title") or "").strip()
        if not title:
            continue
        for i in range(cur, len(lines)):
            if _bare(lines[i]) == title:
                out.append(dict(mk, line=i))
                cur = i + 1
                break
    return out


def _unsupported(ext):
    if ext == ".pdf":
        return ("不支持 PDF。PDF 是出版格式而非记录格式：其中的章节结构是排版时"
                "投影出来的，反推只能靠猜，正文顺序也是排版顺序而非阅读顺序。"
                "请提供源文件（md / txt / docx）。")
    return "不支持的文件类型：%s（支持 md / txt / docx）" % (ext or "无扩展名")


def from_file(path):
    """读取上传文件。返回 (text, meta)；meta 里带 marks（结构证据表）。"""
    if not path or not os.path.exists(path):
        raise IngestError("文件不存在：%s" % path)
    ext = os.path.splitext(path)[1].lower()
    marks = []
    if ext in SUPPORTED_TEXT:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        text = _strip_markdown(raw) if ext != ".txt" else raw
    elif ext in SUPPORTED_DOCX:
        text, marks = _read_docx(path)
    else:
        raise IngestError(_unsupported(ext))
    text = text.strip()
    if not text:
        raise IngestError(
            "文件内容为空：%s。若是扫描件或截图，请先转成可复制的文本。"
            % os.path.basename(path))
    marks = _relocate(text, marks)
    return text, {"source": os.path.basename(path), "ext": ext,
                  "chars": len(text), "marks": marks}


# ----------------------------------------------------------------- 编号体系
_CN_DIGITS = "零〇一二三四五六七八九十百千两"
# 单位词表。长词在前——正则的或取第一个匹配，`部` 写在 `部分` 前面会把
# 「第一部分」切成「部」+「分」。
_UNIT_WORDS = ("部分", "单元", "章", "节", "篇", "部", "卷", "讲", "课", "回", "集")

# 深度基准：把不同体系挪到同一把尺子上，再由 probe 归一到连续层级。
_DEPTH = {"篇": 1, "部": 1, "卷": 1, "部分": 2, "章": 2,
          "单元": 3, "节": 3, "讲": 3, "课": 3, "回": 3, "集": 3}

_CHAPTER_RE = re.compile(
    r"^第\s*([0-9%s]{1,6})\s*(%s)\s*(.*)$"
    % (_CN_DIGITS, "|".join(_UNIT_WORDS)))
_MULTI_NUM_RE = re.compile(r"^(\d{1,4}(?:\.\d{1,4}){1,5})[.．]?\s+?(\S.*)$")
_ARABIC_ITEM_RE = re.compile(r"^[（(]?\s*(\d{1,3})\s*[）).．、:：]\s*(\S.*)$")
_CN_ITEM_RE = re.compile(r"^[（(]?\s*([%s]{1,4})\s*[）).．、:：]\s*(\S.*)$"
                         % _CN_DIGITS)
_ALPHA_ITEM_RE = re.compile(r"^[（(]?\s*([A-Za-z])\s*[）).．、]\s*(\S.*)$")
_ROMAN_ITEM_RE = re.compile(r"^([IVXLCDMivxlcdm]{1,7})\s*[.．、)）]\s*(\S.*)$")
_SECTION_RE = re.compile(r"^§+\s*(\d{1,4}(?:\.\d{1,4})*)\s*(.*)$")
_EN_HEAD_RE = re.compile(
    r"^(Chapter|Part|Section|Lesson|Lecture|Unit|Appendix)\s+"
    r"([0-9]{1,3}|[IVXLCivxlc]{1,6}|[A-Z])\b\s*[:.．、]?\s*(.*)$", re.I)
# 分支字母编号：`08a`、`9b`。这是「同系列」的载体——同一数字前缀带着不同
# 字母，就是同一篇的续篇。书里怎么写，播客的 3a/3b 就怎么排。
_BRANCH_RE = re.compile(r"^(\d{1,4})\s*([a-z])\s+(\S.*)$")
# 篇号：`第 II 部 · 08a｜…`。中点是分隔符，竖线是系列名与篇名的分界。
_ISSUE_RE = re.compile(r"[·・]\s*(\d{1,4})\s*([a-z])?\s*[｜|]")

# 无编号但语义就是标题的行。要求**整行**等于关键词，正文里出现不算。
_KEYWORD_TITLES = (
    "序言", "前言", "序", "自序", "代序", "导言", "引言", "绪论", "绪言", "导读",
    "阅读指南", "凡例", "题记", "结语", "结论", "余论", "尾声", "后记", "跋",
    "致谢", "参考文献", "参考书目", "引用文献", "索引", "术语表", "词汇表",
    "版权页", "内容简介", "内容提要", "作者简介", "译者简介", "出版说明",
    "编后记", "译后记", "大事记", "年表", "缩略语表", "图表目录",
)
_KEYWORD_RE = re.compile(
    r"^(%s)\s*([A-Fa-f]|[0-9]{1,2}|[%s]{1,3})?\s*[:：]?\s*$"
    % ("|".join(_KEYWORD_TITLES), _CN_DIGITS))


def _reject_body(rest):
    """正文体否决：这是在叙述，不是在起标题。

    标题与正文在标点上分得开：标题几乎不用句号，也很少在短句里连用逗号。
    第一版只看了收尾标点，于是以编号开头的正文段落被当成了标题——
    例如「08a 说"…"，但它没有回答一个关键问题：…补上这个切分…」，
    以「08a」开头、末尾是冒号，两道防线全过。
    """
    rest = (rest or "").strip()
    if not rest:
        return False
    if "。" in rest:                        # 句号：这是句子
        return True
    if rest[-1] in "！？；!?;" and len(rest) > 15:
        return True
    if len(rest) > 40 and (rest.count("，") >= 2 or rest.count("、") >= 3):
        return True
    return False


def _by_numbering(line):
    """从正文里认出编号式标题（C 路）。返回候选或 None。

    覆盖实际排版中出现过的编号体系：

        第X章 / 第X节 / 第X篇 / 第X部 / 第X卷 / 第X讲 / 第X回 / 第X单元
        1.1  1.1.1                        多级数字
        1. / 1) / (1) / 1、               阿拉伯单层
        一、 / （一） / 一.                中文单层
        a) / A. / （b）                     字母
        IV. / Ⅳ、                           罗马
        §1.2                              节号
        08a / 9b                          分支字母
        Chapter 1 / Part 2 / Appendix A   英文
        序言 / 附录 / 参考文献 …            无编号关键词

    防线：围栏、表格行、引用块、分隔线、列表项一律排除；`第X节` 后紧跟
    「课」不认（那是「第一节课」）；单层编号标为弱证据，等连续成组再升级。
    """
    s = (line or "").strip()
    if not s or len(s) > 90:
        return None
    if s[0] in "|>" or s.startswith("---") or s.startswith("==="):
        return None
    if re.match(r"^[-*+•]\s", s):        # 项目符号列表
        return None

    def out(level, strength, title=None):
        conf = {"strong": 0.9, "medium": 0.7, "weak": 0.45}[strength]
        return {"title": title or s, "level": level, "evidence": "numbering",
                "confidence": conf, "strength": strength}

    m = _CHAPTER_RE.match(s)
    if m:
        unit = m.group(2)
        rest = m.group(3).strip()
        # 「第一节课」不是章标题，是正文里的时间说法。
        if unit == "节" and rest.startswith("课"):
            return None
        if _reject_body(rest):
            return None
        return out(_DEPTH.get(unit, 2), "strong")

    m = _MULTI_NUM_RE.match(s)
    if m:
        if _reject_body(m.group(2)):
            return None
        return out(min(9, m.group(1).count(".") + 1), "strong")

    m = _KEYWORD_RE.match(s)
    if m:
        return out(1, "strong")

    m = _SECTION_RE.match(s)
    if m:
        if _reject_body(m.group(2)):
            return None
        return out(2 + m.group(1).count("."), "medium")

    m = _EN_HEAD_RE.match(s)
    if m:
        if _reject_body(m.group(3)):
            return None
        lv = 1 if m.group(1).lower() in ("part", "appendix") else 2
        return out(lv, "medium")

    m = _BRANCH_RE.match(s)
    if m:
        # 分支编号后的标题很短（`08a 探测的边界`）。整行一长就不是标题，
        # 是拿「08a」开头的正文段落——这正是「08a 说…」那类误识别的来路。
        if len(s) > 40 or _reject_body(m.group(3)):
            return None
        return out(2, "medium")

    m = _CN_ITEM_RE.match(s)
    if m:
        if _reject_body(m.group(2)):
            return None
        return out(3, "weak")

    m = _ARABIC_ITEM_RE.match(s)
    if m:
        if _reject_body(m.group(2)):
            return None
        return out(3, "weak")

    m = _ALPHA_ITEM_RE.match(s)
    if m:
        if _reject_body(m.group(2)):
            return None
        return out(3, "weak")

    m = _ROMAN_ITEM_RE.match(s)
    if m:
        if _reject_body(m.group(2)):
            return None
        return out(3, "weak")

    return None


def _seq_of(title):
    """从标题里取出用于连续性判断的序号；取不到返回 None。

    连续性是单层编号**升级为强证据**的唯一依据：孤零零一个 `1.` 更可能是
    正文里的列表项，`1. 2. 3.` 连着出现就是章节编号。升的是证据强度，
    不是凭空认定——判定依据仍然来自文本本身。
    """
    s = (title or "").strip()
    m = _ARABIC_ITEM_RE.match(s)
    if m:
        return int(m.group(1)), "arabic"
    m = _ALPHA_ITEM_RE.match(s)
    if m:
        return (ord(m.group(1).lower()) - 96, "alpha")
    m = _CN_ITEM_RE.match(s)
    if m:
        idx = "零一二三四五六七八九".find(m.group(1)[0])
        return (idx if idx >= 0 else None), "cn"
    return None, ""


def has_numbering_head(title):
    """标题是否带编号（`08a`、`1.2`、`第X章`、`第 II 部 · 08a｜`）。

    探查层用它做**离群检测**：同一层的标题若大多带编号，那条不带编号的
    多半不是标题——作者偶尔用一个 `#` 标注正文里的分类小节，它跟真章标题
    同层，靠层级和长度都分不出来，只有形态分得出来。
    """
    s = (title or "").strip()
    if not s:
        return False
    if (_CHAPTER_RE.match(s) or _MULTI_NUM_RE.match(s) or _BRANCH_RE.match(s)
            or _EN_HEAD_RE.match(s) or _ISSUE_RE.match(s)):
        return True
    if re.match(r"^[（(]?\s*(\d{1,3}|[%s]{1,4})\s*[）).．、:：]" % _CN_DIGITS, s):
        return True
    if re.match(r"^第\s*[IVXLCivxlc0-9%s]+\s*(部|篇|卷|部分)" % _CN_DIGITS, s):
        return True
    return False


def branch_key(title):
    """标题所属的分支族标识。取不到返回空串。

    `08a 探测的边界` 与 `第 II 部 · 08a｜…` 都返回 `8`——同一数字前缀带不同
    字母，就是同一篇的续篇。书里的 `08a/08b/08c` 与播客的 `3a/3b/3c`
    是同一套编号哲学，认得出来才谈得上「同系列合并排期」。
    """
    s = (title or "").strip()
    m = _BRANCH_RE.match(s)
    if m:
        return m.group(1).lstrip("0") or "0"
    m = _ISSUE_RE.search(s)
    if m and m.group(2):
        return m.group(1).lstrip("0") or "0"
    return ""


def _promote_sequences(anchors):
    """连续递增的单层编号由弱证据升为强证据。"""
    for a in anchors:
        if a.get("strength") != "weak":
            continue
        a["_seq"] = _seq_of(a["title"])
    for i in range(len(anchors) - 1):
        a, b = anchors[i], anchors[i + 1]
        if a.get("strength") != "weak" or b.get("strength") != "weak":
            continue
        (na, ka), (nb, kb) = a.get("_seq") or (None, ""), b.get("_seq") or (None, "")
        if na is not None and nb == na + 1 and ka == kb:
            for x in (a, b):
                x["strength"] = "strong"
                x["confidence"] = 0.8
    for a in anchors:
        a.pop("_seq", None)
    return anchors


# --------------------------------------------------------------------- 锚点
def list_anchors(text, marks=None):
    """列出所有可作为锚点的标题行。

    合并三路证据，按行去重。返回项：`line` / `level` / `title` / `evidence`
    / `confidence` / `strength`。**不做跨路取舍**——同一行上标记与编号都在，
    取标记（更接近原作者声明）；不一致的地方留给 `probe` 摆给人看。
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    picked = {}
    fence = False
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            fence = not fence
            continue
        if fence:
            continue
        m = _TITLE_RE.match(line)
        if m:
            picked[i] = {"line": i, "level": len(m.group(1)),
                         "title": m.group(2).strip(), "evidence": "marker",
                         "confidence": 0.95, "strength": "strong"}
            continue
        cand = _by_numbering(line)
        if cand:
            cand["line"] = i
            picked[i] = cand

    # B 路：抽取层带来的样式证据。命中同一行时以它为准——`Heading 2` 是
    # 作者声明的层级，比正文里的编号更能反映他心里的结构。
    for mk in marks or []:
        i = int(mk.get("line", -1))
        if not (0 <= i < len(lines)):
            continue
        ev = mk.get("evidence") or "style"
        picked[i] = {"line": i, "title": mk.get("title") or _bare(lines[i]),
                     "level": int(mk.get("level") or 2), "evidence": ev,
                     "confidence": float(mk.get("confidence") or 0.9),
                     "strength": "strong" if ev == "style" else "weak"}

    out = [picked[k] for k in sorted(picked)]
    return _promote_sequences(out)


def slice_by_anchor(text, anchor, marks=None, line=None):
    """按标题锚点切出一章（含其下级标题直到同级或更高级标题为止）。

    `line` 给出标题所在行号时**按行号定位**：同一份稿子里同名标题并不罕见
    （各章都有一节「小结」），只按标题找只能取到靠前的那一条，而地图上写的
    是同一串字，看不出取的是谁。行号与标题对不上时退回按标题找——位置是
    索引，不是判据。

    匹配不到 → 抛 IngestError 并列出候选，绝不静默截断。
    """
    text = text.replace("\r\n", "\n")
    if not (anchor or "").strip():
        raise IngestError("未指定章节锚点。")
    anchor = anchor.strip()
    anchors = list_anchors(text, marks)
    if not anchors:
        raise IngestError(
            "素材中找不到任何可作锚点的标题行（Markdown 标记、Word 标题样式、"
            "或正文里的编号如「第1章」「1.1」「一、」）。请改用「全文」模式。"
        )

    hit = None
    if line is not None and str(line).strip() != "":
        try:
            want = int(line)
        except (TypeError, ValueError):
            want = -1
        for a in anchors:
            if a["line"] == want and a["title"] == anchor:
                hit = a
                break
    if hit is None:
        for a in anchors:
            if a["title"] == anchor:
                hit = a
                break
    if hit is None:
        for a in anchors:
            if anchor in a["title"]:
                hit = a
                break
    if hit is None:
        cand = "、".join(a["title"] for a in anchors[:12])
        raise IngestError(
            "锚点「%s」在素材中不存在。可用锚点前 12 项：%s" % (anchor, cand)
        )

    lines = text.split("\n")
    start = hit["line"]
    end = len(lines)
    for a in anchors:
        if a["line"] > start and a["level"] <= hit["level"]:
            end = a["line"]
            break

    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise IngestError("锚点「%s」切出的内容为空。" % anchor)
    return body, {
        "anchor": hit["title"],
        "level": hit["level"],
        "line_start": start + 1,
        "line_end": end,
        "chars": len(body),
        "anchor_count": len(anchors),
    }


# --------------------------------------------------------------------- 统一入口
def ingest(text=None, file_path=None, anchor=None):
    """统一摄入入口。三种来源择一：text / file_path。

    anchor 非空时对结果做锚点切章。返回 {"text", "meta", "marks"}——
    marks 提到顶层，调用方（素材入库）要把它一起存下来，否则第二次读素材
    时就只剩纯文本，docx 的样式证据白记一场。
    """
    marks = []
    if file_path:
        content, meta = from_file(file_path)
        marks = meta.get("marks") or []
    else:
        content = from_text(text)
        meta = {"source": "pasted", "chars": len(content)}

    if anchor:
        content, slice_meta = slice_by_anchor(content, anchor, marks)
        meta.update(slice_meta)

    return {"text": content, "meta": meta, "marks": marks}
