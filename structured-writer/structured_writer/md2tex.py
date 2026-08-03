"""md2tex — Markdown → LaTeX 转换器（生成完整 .tex 文档）

参考 latex-modular 技能的 compose.py 宏包顺序与模板结构。
只做确定性映射，不做语义理解。

用法:
    from .md2tex import md_to_tex
    tex = md_to_tex(md_text, title="文章标题")
"""

import re

# 宏包加载顺序（参考 latex-modular scripts/compose.py PACKAGE_ORDER）
# 注意：geometry 不放在这里，避免与下方带选项的 geometry 重复加载触发 Option clash
PACKAGES = [
    "ctex", "fontspec", "xunicode",
    "fancyhdr", "lastpage",
    "xcolor", "graphicx", "eso-pic",
    "pgfplots", "tikz", "siunitx",
    "enumitem", "multicol", "float",
    "tabularx", "booktabs", "multirow",
    "pifont", "amssymb",
    "etoolbox", "newunicodechar",
    "pdflscape", "tocloft",
]

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


def _md_to_tex_lines(lines: list) -> list:
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

    # 图片 ![](path) → includegraphics（路径不转义）
    s = re.sub(r"!\[[^\]]*\]\(([^)]+)\)",
               lambda m: _stash(rf"\includegraphics[width=0.8\textwidth]{{{m.group(1)}}}"), s)

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


def md_to_tex(md_text: str, title: str = "", author: str = "") -> str:
    """Markdown 文本 → 完整 LaTeX 文档（lualatex 兼容，中文 ctex）"""
    lines = md_text.splitlines()

    # 首行 # 标题若存在则作为文档标题
    if lines and re.match(r"^#\s+\S", lines[0].strip()):
        m = re.match(r"^#\s+(.*)$", lines[0].strip())
        title = title or m.group(1).strip()
        lines = lines[1:]

    body = _md_to_tex_lines(lines)

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
