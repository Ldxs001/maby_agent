"""md2tex — Markdown → LaTeX 转换器（生成完整 .tex 文档）

参考 latex-modular 技能的 compose.py 宏包顺序与模板结构。
只做确定性映射，不做语义理解。

用法:
    from .md2tex import md_to_tex
    tex = md_to_tex(md_text, title="文章标题")
"""

import os
import re
import struct
from typing import Optional

# 宏包加载顺序（参考 latex-modular scripts/compose.py PACKAGE_ORDER）
# 注意：geometry 不放在这里，避免与下方带选项的 geometry 重复加载触发 Option clash
PACKAGES = [
    "ctex", "fontspec", "xunicode",
    "fancyhdr", "lastpage",
    "xcolor", "graphicx", "eso-pic",
    "pgfplots", "tikz", "siunitx",
    "enumitem", "multicol", "float",
    "tabularx", "booktabs", "multirow",
    "pifont", "amssymb", "caption",
    "etoolbox", "newunicodechar",
    "pdflscape", "tocloft",
]

# 文本区宽高比（textwidth / textheight，纯比例，与纸张尺寸无关）。
# A4 16.0/24.7 ≈ 0.648；letterpaper 16.6/22.9 ≈ 0.725——分类与排版全部比例化，任意纸张一致。
_TEXT_RATIO = 16.0 / 24.7

# ── 图片分类阈值（纯比例设计参数） ──
_IMG_ROTATE_MIN_PX = 2600   # 像素宽超过此值视为超高清大图
_IMG_ROTATE_MIN_AR = 2.5    # 且宽高比 ≥ 此值 → 旋转 90° 竖放（原宽被页高限定）
_IMG_LARGE_H_RATIO = 0.75   # 按 0.92 页宽渲染高度占比超过此值 → 竖图高度约束
_IMG_SMALL_MAX_PX = 900     # 像素宽 ≤ 此值 且 非竖图 → 小图（两列并排）
_IMG_SMALL_MIN_AR = 0.6

_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "%": r"\%",
    "&": r"\&",
    "#": r"\#",
    "$": r"\$",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_text(s: str) -> str:
    """转义 LaTeX 特殊字符（先处理反斜杠避免二次转义）"""
    out = []
    for ch in s:
        out.append(_ESCAPE_MAP.get(ch, ch))
    return "".join(out)


def _md_table(lines: list) -> list:
    """Markdown 表格 → tabular 环境（吃掉分隔行和连续表行）"""
    tex_lines = []
    rows = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not re.match(r"^\s*\|.*\|\s*$", line):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if re.match(r"^[\s:|-]+$", line.strip().replace("|", "")):
            i += 1
            continue  # 分隔行（|---|）跳过
        rows.append(cells)
        i += 1
    if not rows:
        return [], i
    ncol = max(len(r) for r in rows)
    tex_lines.append("\\begin{table}[H]")
    tex_lines.append("\\centering")
    tex_lines.append("\\begin{tabular}{" + "l" * ncol + "}")
    for ri, row in enumerate(rows):
        row = row + [""] * (ncol - len(row))
        tex_lines.append("  " + " & ".join(_escape_text(c) for c in row) + r" \\")
        if ri == 0:
            tex_lines.append("  \\hline")
    tex_lines.append("\\end{tabular}")
    tex_lines.append("\\end{table}")
    tex_lines.append("")
    return tex_lines, i


# ── 图片分类与排版（读像素尺寸 → 四类布局） ──

def _image_size(path: str) -> Optional[tuple]:
    """读取图片像素尺寸 (宽, 高)，仅支持 PNG/JPEG/GIF，标准库零依赖。

    无法识别/读取失败返回 None（调用方降级为中图 0.8 页宽，不参与并排）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    # PNG: 8 字节签名 + IHDR 块（宽/高为大端 uint32）
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", head[16:24])
        return w, h
    # GIF: 6 字节签名 + 逻辑屏幕描述符（宽/高为小端 uint16）
    if head[:4] == b"GIF8":
        w, h = struct.unpack("<HH", head[6:10])
        return w, h
    # JPEG: 逐段扫描 SOF marker（含尺寸的 13 个 marker）
    if head[:2] == b"\xff\xd8":
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in sof:
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + length
        return None
    return None


def _classify_image(pw: int, ph: int) -> str:
    """像素尺寸 → 四类：rotate_large / large / small / medium。

    - rotate_large: 超宽全景（像素宽大且宽高比极宽）→ 旋转 90° 竖放，原宽被页高限定
    - large: 竖图（按页宽渲染高度占比 > 0.75）→ 宽/高双约束防溢出
    - small: 像素小且非竖图 → 两列并排
    - medium: 其余常规图 → 0.8 页宽居中
    """
    r = pw / ph if ph else 1.0
    if pw > _IMG_ROTATE_MIN_PX and r >= _IMG_ROTATE_MIN_AR:
        return "rotate_large"
    if 0.92 * _TEXT_RATIO / r > _IMG_LARGE_H_RATIO:
        return "large"
    if pw <= _IMG_SMALL_MAX_PX and r >= _IMG_SMALL_MIN_AR:
        return "small"
    return "medium"


_INCLUDEGRAPHICS_OPTS = {
    "medium": "width=0.8\\textwidth",
    "large": "width=0.92\\textwidth, height=0.85\\textheight, keepaspectratio",
    "rotate_large": "width=0.92\\textheight, height=0.95\\textwidth, keepaspectratio, angle=90",
}


def _tex_path(path: str) -> str:
    """includegraphics 文件路径保护：含 LaTeX 特殊字符（& % # $ _ { } ~ ^）时
    用 \\detokenize 包住使其按普通字符处理，否则 graphicx 文件名解析阶段报错。"""
    if re.search(r"[&#%$_{}~^]", path):
        return r"\detokenize{" + path + "}"
    return path


def _single_figure(alt: str, path: str, cls: str) -> list:
    """单图独立块（非浮动，center 环境）：紧跟文本流末尾下一行、居中排列，
    页尾空间不足时整块自动换到下一页——不与文字混排、不强制本页、无 [H] 报错风险。"""
    opts = _INCLUDEGRAPHICS_OPTS.get(cls, "width=0.48\\textwidth")
    lines = ["\\begin{center}",
             f"\\includegraphics[{opts}]{{{_tex_path(path)}}}"]
    if alt:
        lines.append("\\captionof{figure}{" + _escape_text(alt) + "}")
    lines += ["\\end{center}", ""]
    return lines


def _small_figure(imgs: list) -> list:
    """小图两列独立块（非浮动）：按顺序填充 1行左→1行右→2行左→2行右…，
    奇数最后一张落在下一行左列（左对齐），无居中孤张；行间 \\par 分行，
    整体跟随文本流，页尾放不下自动整块换页。"""
    lines = []
    for idx, (_alt, path) in enumerate(imgs):
        if idx % 2 == 1:
            continue  # 右列已在成对时输出
        if idx + 1 < len(imgs):
            p2 = imgs[idx + 1][1]
            lines.append(
                "\\par\\noindent\n"
                "\\begin{minipage}[t]{0.48\\textwidth}\\centering\n"
                f"\\includegraphics[width=\\linewidth]{{{_tex_path(path)}}}\n"
                "\\end{minipage}%\n\\hfill\n"
                "\\begin{minipage}[t]{0.48\\textwidth}\\centering\n"
                f"\\includegraphics[width=\\linewidth]{{{_tex_path(p2)}}}\n"
                "\\end{minipage}"
            )
        else:
            lines.append(
                "\\par\\noindent\n"
                "\\begin{minipage}[t]{0.48\\textwidth}\\centering\n"
                f"\\includegraphics[width=\\linewidth]{{{_tex_path(path)}}}\n"
                "\\end{minipage}"
            )
    lines.append("")
    return lines


def _figure_tex(imgs: list, base_dir: str) -> list:
    """一组连续图片行 → LaTeX 独立图片块列表（非浮动）。

    先读尺寸分类：连续小图聚合为两列块；非小图各自独立块（保持原顺序）。
    尺寸读取失败 → medium（0.8 页宽，不参与并排）；base_dir 为空 → 全部 medium（向后兼容）。
    """
    out = []
    small_buf = []

    def flush():
        if small_buf:
            out.extend(_small_figure(small_buf))
            small_buf.clear()

    for alt, path in imgs:
        cls = "medium"
        if base_dir:
            size = _image_size(os.path.join(base_dir, path))
            if size:
                cls = _classify_image(size[0], size[1])
        if cls == "small":
            small_buf.append((alt, path))
        else:
            flush()
            out.extend(_single_figure(alt, path, cls))
    flush()
    return out


def _md_to_tex_lines(lines: list, base_dir: str = "") -> list:
    """逐行转换，返回 LaTeX 行列表"""
    tex = []
    in_code = False
    in_list = None  # None | itemize | enumerate
    in_quote = False
    i = 0
    n = len(lines)

    def close_list():
        nonlocal in_list
        if in_list:
            tex.append("\\end{" + in_list + "}")
            tex.append("")
            in_list = None

    def close_quote():
        nonlocal in_quote
        if in_quote:
            tex.append("\\end{quote}")
            tex.append("")
            in_quote = False

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 代码块（围栏）
        if stripped.startswith("```"):
            close_list(); close_quote()
            if in_code:
                tex.append("\\end{verbatim}")
                in_code = False
            else:
                tex.append("\\begin{verbatim}")
                in_code = True
            i += 1
            continue
        if in_code:
            tex.append(stripped)
            i += 1
            continue

        # 空行
        if not stripped:
            close_list(); close_quote()
            tex.append("")
            i += 1
            continue

        # 表格（连续 | 开头行）
        if stripped.startswith("|") and stripped.endswith("|"):
            close_list(); close_quote()
            tlines, consumed = _md_table(lines[i:])
            tex.extend(tlines)
            i += consumed
            continue

        # 图片（纯图片行，连续聚合后分类排版为独立块：小图两列并排 / 中图 0.8 / 大图双约束 / 超宽旋转）
        m = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if m:
            close_list(); close_quote()
            imgs = [(m.group(1), m.group(2))]
            while i + 1 < n:
                m2 = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", lines[i + 1].strip())
                if not m2:
                    break
                imgs.append((m2.group(1), m2.group(2)))
                i += 1
            tex.extend(_figure_tex(imgs, base_dir))
            tex.append("")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_list(); close_quote()
            level = len(m.group(1))
            title = _escape_text(m.group(2).strip())
            cmd = {1: "section*", 2: "section", 3: "subsection",
                   4: "subsubsection", 5: "paragraph", 6: "subparagraph"}[level]
            tex.append(f"\\{cmd}{{{title}}}")
            tex.append("")
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            close_list()
            if not in_quote:
                tex.append("\\begin{quote}")
                in_quote = True
            tex.append(_escape_text(stripped.lstrip(">").strip()))
            i += 1
            continue

        # 无序列表
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            close_quote()
            if in_list != "itemize":
                close_list()
                tex.append("\\begin{itemize}")
                in_list = "itemize"
            tex.append("  \\item " + _inline_md(m.group(1)))
            i += 1
            continue

        # 有序列表
        m = re.match(r"^\d+[.、)]\s+(.*)$", stripped)
        if m:
            close_quote()
            if in_list != "enumerate":
                close_list()
                tex.append("\\begin{enumerate}")
                in_list = "enumerate"
            tex.append("  \\item " + _inline_md(m.group(1)))
            i += 1
            continue

        # 分割线
        if re.match(r"^[-*_]{3,}\s*$", stripped):
            close_list(); close_quote()
            tex.append("\\hrule")
            tex.append("")
            i += 1
            continue

        # 普通段落
        close_list(); close_quote()
        tex.append(_inline_md(stripped))
        i += 1

    close_list(); close_quote()
    if in_code:
        tex.append("\\end{verbatim}")
    return tex


def _inline_md(s: str) -> str:
    """行内元素：图片、粗体、斜体、行内代码

    生成 LaTeX 命令后用占位符暂存，避免被最后的特殊字符转义误伤。
    """
    stash = {}

    def _stash(tex_fragment: str) -> str:
        key = f"\x01{len(stash)}\x01"
        stash[key] = tex_fragment
        return key

    # 图片 ![](path) → includegraphics（路径不转义，走 _tex_path 特殊字符保护；行内图固定
    # 0.5 页宽，完整分类排版由段落级图片行聚合处理）
    s = re.sub(r"!\[[^\]]*\]\(([^)]+)\)",
               lambda m: _stash(rf"\includegraphics[width=0.5\textwidth]{{{_tex_path(m.group(1))}}}"), s)

    # 行内代码 `x` → \texttt
    s = re.sub(r"`([^`]+)`",
               lambda m: _stash(r"\texttt{" + _escape_text(m.group(1)) + "}"), s)

    # 链接 [text](url) → text（保留文字）
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)",
               lambda m: _stash(_escape_text(m.group(1))), s)

    # 粗体 **x**
    s = re.sub(r"\*\*([^*]+)\*\*",
               lambda m: _stash(r"\textbf{" + _inline_md(m.group(1)) + "}"), s)

    # 斜体 *x* / _x_
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)",
               lambda m: _stash(r"\textit{" + _inline_md(m.group(1)) + "}"), s)
    s = re.sub(r"(?<!_)_([^_\n]+)_(?!_)",
               lambda m: _stash(r"\textit{" + _inline_md(m.group(1)) + "}"), s)

    # 剩余普通文本转义
    s = _escape_text(s)

    # 还原占位符
    for k, v in stash.items():
        s = s.replace(k, v)
    return s


def md_to_tex(md_text: str, title: str = "", author: str = "",
              image_base_dir: str = "") -> str:
    """Markdown 文本 → 完整 LaTeX 文档（xelatex 兼容，中文 ctex）

    image_base_dir: 图片所在目录（读像素尺寸做分类排版用）。为空时所有图片
    降级为 0.8 页宽（与旧版行为一致，向后兼容）。
    """
    lines = md_text.splitlines()

    # 首行 # 标题若存在则作为文档标题
    if lines and re.match(r"^#\s+\S", lines[0].strip()):
        m = re.match(r"^#\s+(.*)$", lines[0].strip())
        title = title or m.group(1).strip()
        lines = lines[1:]

    body = _md_to_tex_lines(lines, image_base_dir)

    head = [
        "\\documentclass[12pt]{article}",
    ]
    for pkg in PACKAGES:
        head.append(f"\\usepackage{{{pkg}}}")
    head += [
        "\\usepackage[top=2.5cm,bottom=2.5cm,left=2.5cm,right=2.5cm]{geometry}",
        "",
        "\\title{" + _escape_text(title) + "}",
        "\\author{" + _escape_text(author) + "}",
        "\\date{\\today}",
        "",
        "\\begin{document}",
        "\\maketitle",
        "",
    ]
    tail = [
        "\\end{document}",
    ]
    return "\n".join(head + [l for l in body if l != ""] + tail)
