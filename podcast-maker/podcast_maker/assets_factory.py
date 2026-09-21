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

"""资源工厂：背景图、封面三尺寸、背景音乐 —— 全部由代码生成，不调用 LLM。

档位一律取自 MODE_SPEC（有限枚举）。字体先探测、再使用；探测不到即报错，
不静默回退到渲染不出中文的默认字体。
"""

import math
import os
import sys

from . import aigc_label
from .config_manager import MODE_SPEC

WATERMARK_TEXT = "AI 生成"

# 水印的几何与判据集中一处：绘制与检测共用同一组常量，
# 禁止在两边各写一份阈值（分散的点位会漂移）。
WATERMARK_REGION = (0.60, 0.12)        # 右上角检测区：x0 = 0.60w，y1 = 0.12h
WATERMARK_MARGIN = (36, 20)            # 距右边缘、距上边缘（像素）
WATERMARK_LIGHT = (240, 244, 252)      # 深色底 → 浅色字
WATERMARK_DARK = (24, 30, 46)          # 浅色底 → 深色字
WATERMARK_ALPHA = 235
WATERMARK_SPLIT_LUMA = 140             # 背景亮度分界，决定取浅字还是深字
WATERMARK_MIN_CONTRAST = 40            # 灰度差阈值
WATERMARK_MIN_PIXELS = 200             # 达标像素下限

# 字体白名单。条目在这里登记，**能不能选用只看本机装没装**：
# 装了 → 可选；没装 → 界面上照样列出，但暗显、不可选。
#
# 同一族名可挂多条候选文件名：开源字体各渠道发的文件名并不统一，任一条命中即收录
# （族名只有一个，去重后每族只出现一次）。
# license 只写给界面看，不参与任何判定——能不能商用得由使用者自行核对官方声明。
FONT_CANDIDATES = [
    # ---- 中文 · 正文与标题 ----
    {"family": "Microsoft YaHei", "file": "msyh.ttc", "label": "微软雅黑",
     "os": "nt", "license": "系统自带"},
    {"family": "Microsoft YaHei", "file": "msyhbd.ttc", "label": "微软雅黑",
     "os": "nt", "license": "系统自带"},
    {"family": "SimHei", "file": "simhei.ttf", "label": "黑体",
     "os": "nt", "license": "系统自带"},
    {"family": "SimSun", "file": "simsun.ttc", "label": "宋体",
     "os": "nt", "license": "系统自带"},
    {"family": "KaiTi", "file": "simkai.ttf", "label": "楷体",
     "os": "nt", "license": "系统自带"},
    {"family": "FangSong", "file": "simfang.ttf", "label": "仿宋",
     "os": "nt", "license": "系统自带"},
    {"family": "DengXian", "file": "Deng.ttf", "label": "等线",
     "os": "nt", "license": "系统自带"},
    {"family": "PingFang SC", "file": "PingFang.ttc", "label": "苹方",
     "os": "posix", "license": "系统自带"},
    # 开源/免费商用：这一批是跨机器的稳定选择，也是唯一允许随包分发的
    {"family": "Source Han Sans SC", "file": "SourceHanSansSC-Regular.otf",
     "label": "思源黑体", "os": "any", "license": "OFL 1.1"},
    {"family": "Source Han Serif SC", "file": "SourceHanSerifSC-Regular.otf",
     "label": "思源宋体", "os": "any", "license": "OFL 1.1"},
    {"family": "Noto Sans SC", "file": "NotoSansSC-Regular.otf",
     "label": "Noto Sans SC", "os": "any", "license": "OFL 1.1"},
    {"family": "Noto Sans SC", "file": "NotoSansSC-VF.ttf",
     "label": "Noto Sans SC", "os": "any", "license": "OFL 1.1"},
    {"family": "Noto Serif SC", "file": "NotoSerifSC-Regular.otf",
     "label": "Noto Serif SC", "os": "any", "license": "OFL 1.1"},
    {"family": "Noto Serif SC", "file": "NotoSerifSC-VF.ttf",
     "label": "Noto Serif SC", "os": "any", "license": "OFL 1.1"},
    {"family": "Noto Sans CJK SC", "file": "NotoSansCJK-Regular.ttc",
     "label": "Noto Sans CJK SC", "os": "any", "license": "OFL 1.1"},
    {"family": "HarmonyOS Sans SC", "file": "HarmonyOS_Sans_SC_Regular.ttf",
     "label": "鸿蒙黑体", "os": "any", "license": "华为免费商用"},
    {"family": "Alibaba PuHuiTi", "file": "AlibabaPuHuiTi-3-55-Regular.ttf",
     "label": "阿里巴巴普惠体", "os": "any", "license": "阿里免费商用"},
    {"family": "Alibaba PuHuiTi", "file": "Alibaba-PuHuiTi-Regular.ttf",
     "label": "阿里巴巴普惠体", "os": "any", "license": "阿里免费商用"},
    {"family": "LXGW WenKai", "file": "LXGWWenKai-Regular.ttf",
     "label": "霞鹜文楷", "os": "any", "license": "OFL 1.1"},
    {"family": "LXGW Neo XiHei", "file": "LXGWNeoXiHei.ttf",
     "label": "霞鹜新晰黑", "os": "any", "license": "OFL 1.1"},
    {"family": "Smiley Sans", "file": "SmileySans-Oblique.ttf",
     "label": "得意黑", "os": "any", "license": "OFL 1.1"},
    {"family": "Sarasa Gothic SC", "file": "SarasaGothicSC-Regular.ttf",
     "label": "更纱黑体", "os": "any", "license": "OFL 1.1"},
    {"family": "Glow Sans SC", "file": "GlowSansSC-Normal-Regular.otf",
     "label": "未来荧黑", "os": "any", "license": "OFL 1.1"},
    {"family": "Zhuque Fangsong", "file": "ZhuqueFangsong-Regular.ttf",
     "label": "朱雀仿宋", "os": "any", "license": "OFL 1.1"},
    {"family": "MiSans", "file": "MiSans-Regular.ttf",
     "label": "MiSans", "os": "any", "license": "小米免费商用"},
    {"family": "OPPO Sans", "file": "OPPOSans-Regular.ttf",
     "label": "OPPO Sans", "os": "any", "license": "OPPO 免费商用"},
    {"family": "ZCOOL KuaiLe", "file": "ZCOOLKuaiLe-Regular.ttf",
     "label": "站酷快乐体", "os": "any", "license": "站酷免费商用"},
    {"family": "ZCOOL GaoDuanHei", "file": "ZCOOLGaoDuanHei-Regular.ttf",
     "label": "站酷高端黑", "os": "any", "license": "站酷免费商用"},
    {"family": "WenQuanYi Zen Hei", "file": "wqy-zenhei.ttc",
     "label": "文泉驿正黑", "os": "any", "license": "GPLv2 字体例外"},
    # ---- 中文 · 等宽（代码、参数、表格用）----
    {"family": "LXGW WenKai Mono", "file": "LXGWWenKaiMono-Regular.ttf",
     "label": "霞鹜文楷等宽", "os": "any", "license": "OFL 1.1"},
    {"family": "Sarasa Mono SC", "file": "SarasaMonoSC-Regular.ttf",
     "label": "更纱等宽", "os": "any", "license": "OFL 1.1"},
    {"family": "Noto Sans Mono CJK SC", "file": "NotoSansMonoCJKsc-Regular.otf",
     "label": "Noto 等宽黑体", "os": "any", "license": "OFL 1.1"},
    # ---- 西文 · 正文与标题（不含中文字形，中文文案选了会缺字）----
    {"family": "Inter", "file": "Inter-Regular.otf",
     "label": "Inter", "os": "any", "license": "OFL 1.1"},
    {"family": "Source Sans 3", "file": "SourceSans3-Regular.otf",
     "label": "Source Sans 3", "os": "any", "license": "OFL 1.1"},
    {"family": "Source Serif 4", "file": "SourceSerif4-Regular.otf",
     "label": "Source Serif 4", "os": "any", "license": "OFL 1.1"},
    {"family": "IBM Plex Sans", "file": "IBMPlexSans-Regular.ttf",
     "label": "IBM Plex Sans", "os": "any", "license": "OFL 1.1"},
    {"family": "Fira Sans", "file": "FiraSans-Regular.ttf",
     "label": "Fira Sans", "os": "any", "license": "OFL 1.1"},
    {"family": "Roboto", "file": "Roboto-Regular.ttf",
     "label": "Roboto", "os": "any", "license": "Apache 2.0"},
    {"family": "Open Sans", "file": "OpenSans-Regular.ttf",
     "label": "Open Sans", "os": "any", "license": "OFL 1.1"},
    {"family": "Lato", "file": "Lato-Regular.ttf",
     "label": "Lato", "os": "any", "license": "OFL 1.1"},
    {"family": "Montserrat", "file": "Montserrat-Regular.ttf",
     "label": "Montserrat", "os": "any", "license": "OFL 1.1"},
    {"family": "DejaVu Sans", "file": "DejaVuSans.ttf",
     "label": "DejaVu Sans", "os": "any", "license": "免费（Bitstream Vera 派生）"},
    # ---- 西文 · 等宽 ----
    {"family": "JetBrains Mono", "file": "JetBrainsMono-Regular.ttf",
     "label": "JetBrains Mono", "os": "any", "license": "OFL 1.1"},
    {"family": "Fira Code", "file": "FiraCode-Regular.ttf",
     "label": "Fira Code", "os": "any", "license": "OFL 1.1"},
    {"family": "Cascadia Code", "file": "CascadiaCode.ttf",
     "label": "Cascadia Code", "os": "any", "license": "OFL 1.1"},
    {"family": "Source Code Pro", "file": "SourceCodePro-Regular.otf",
     "label": "Source Code Pro", "os": "any", "license": "OFL 1.1"},
    {"family": "IBM Plex Mono", "file": "IBMPlexMono-Regular.ttf",
     "label": "IBM Plex Mono", "os": "any", "license": "OFL 1.1"},
    {"family": "Roboto Mono", "file": "RobotoMono-Regular.ttf",
     "label": "Roboto Mono", "os": "any", "license": "Apache 2.0"},
    {"family": "Space Mono", "file": "SpaceMono-Regular.ttf",
     "label": "Space Mono", "os": "any", "license": "OFL 1.1"},
]


def _nt_font_dirs():
    """Windows 字体目录。

    `C:\\Windows\\Fonts` 是全局安装；Win10 起还支持「仅为当前用户安装」，
    装到 `%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`。只扫前者会漏掉一批
    用户自己装的字体，正是「装了却在列表里找不到」的成因之一。
    """
    dirs = [r"C:\Windows\Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    return dirs


FONT_DIRS = {
    "nt": _nt_font_dirs(),
    "posix": ["/usr/share/fonts", "/usr/local/share/fonts",
              "/System/Library/Fonts", os.path.expanduser("~/.fonts")],
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED_FONT_DIR = os.path.join(ROOT, "assets", "fonts")


class AssetError(RuntimeError):
    """资源生成失败。绝不静默产出次品。"""


def _platform_key():
    if sys.platform.startswith("win"):
        return "nt"
    return "posix"


# ------------------------------------------------------------------ 字体
def list_fonts():
    """探测**本机装了的**中文字体。返回 [{"family","path","label","source","license"}]。

    只返回装了的：这是 `resolve_font` 的取值域，也是「能不能真拿去渲染」的依据。
    「装了没装都要摆出来」是界面的事，走 `font_catalog()`，两者别混。
    """
    out = []
    seen = set()
    # 内置优先（跨机器字形稳定）
    if os.path.isdir(BUNDLED_FONT_DIR):
        for fn in sorted(os.listdir(BUNDLED_FONT_DIR)):
            if fn.lower().endswith((".ttf", ".otf", ".ttc")):
                fam = os.path.splitext(fn)[0]
                out.append({"family": fam, "path": os.path.join(BUNDLED_FONT_DIR, fn),
                            "label": "%s（内置）" % fam, "source": "bundled",
                            "license": "自备"})
                seen.add(fam)

    plat = _platform_key()
    dirs = FONT_DIRS.get(plat, [])
    for cand in FONT_CANDIDATES:
        if cand["os"] not in ("any", plat):
            continue
        if cand["family"] in seen:
            continue
        for d in dirs:
            p = os.path.join(d, cand["file"])
            if os.path.exists(p):
                out.append({"family": cand["family"], "path": p,
                            "label": "%s（系统）" % cand["label"], "source": "system",
                            "license": cand.get("license", "")})
                seen.add(cand["family"])
                break
    return out


def font_catalog():
    """白名单全量：装了没装都列，每项带 `installed` 与 `license`。

    与 `list_fonts()` 分工：那个答「现在能用什么」，这个答「界面上该摆什么」。
    没装的条目照样出现，只是界面上暗显不可选——「装了却找不着」和
    「没装所以不知道有这款」是两种不同的困惑，后者靠列出来解决。
    已装的排前面：下拉一点开，能用的先看见。
    """
    installed = {}
    for f in list_fonts():
        installed.setdefault(f["family"], f)

    out = [{"family": f["family"], "label": f["label"], "license": f["license"],
            "installed": True, "path": f["path"], "source": f["source"]}
           for f in installed.values()]

    plat = _platform_key()
    listed = set(installed)
    for cand in FONT_CANDIDATES:
        if cand["os"] not in ("any", plat):
            continue
        if cand["family"] in listed:
            continue
        listed.add(cand["family"])
        out.append({"family": cand["family"], "label": cand["label"],
                    "license": cand.get("license", ""), "installed": False,
                    "path": "", "source": ""})
    return out


def resolve_font(prefer=""):
    """按偏好取字体；找不到时报错并列出候选。"""
    fonts = list_fonts()
    if not fonts:
        raise AssetError(
            "未找到可用的中文字体。请把字体文件放入 assets/fonts/ 后重试。"
        )
    if prefer:
        for f in fonts:
            if f["family"] == prefer or os.path.basename(f["path"]) == prefer:
                return f
        raise AssetError(
            "指定字体「%s」不可用。当前可用：%s"
            % (prefer, "、".join(f["family"] for f in fonts))
        )
    return fonts[0]


def fonts_dir_of(font):
    """返回字体所在目录，供 ffmpeg 的 ass 滤镜 fontsdir 使用。"""
    return os.path.dirname(os.path.abspath(font["path"]))


# ------------------------------------------------------------------ 字形覆盖
_NOTDEF_PROBE = "\ue05f"   # 私用区码位，字体几乎必然没有，用来复现 .notdef


def glyph_ok(font, ch):
    """字体是否含该字形。

    FreeType 找不到字形时不报错，直接渲染 .notdef（豆腐块）。
    拿一个必然缺失的私用区码位当参照物，渲染结果逐字节相同即视为缺失。
    """
    if not ch or ch.isspace():
        return True
    try:
        return bytes(font.getmask(ch)) != bytes(font.getmask(_NOTDEF_PROBE))
    except Exception:
        return True


def assert_glyphs(font, text, where):
    """文本出现字体没有的字形时报错。

    缺字形会画出豆腐块，属于静默产出次品，必须拦下而不是让它进成片。
    换符号、换字体、或去掉字符，都比出一版带方块字的封面强。
    """
    missing = sorted({c for c in (text or "") if not glyph_ok(font, c)})
    if missing:
        try:
            family = font.getname()[0]
        except Exception:
            family = os.path.basename(getattr(font, "path", "")) or "当前字体"
        raise AssetError("字体「%s」缺少字形：%s（出现在%s）。请换字体或去掉这些字符。"
                         % (family, " ".join(missing), where))


# ------------------------------------------------------------------ 工具
def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise AssetError("未安装 Pillow（pip install Pillow），无法生成图片资源。")
    return Image, ImageDraw, ImageFont


def _vgradient(size, top, bottom):
    Image, ImageDraw, _ = _pil()
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    tr, tg, tb = _hex(top)
    br, bg, bb = _hex(bottom)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=(int(tr + (br - tr) * t), int(tg + (bg - tg) * t), int(tb + (bb - tb) * t)))
    return img


def _hex(s, default=(255, 255, 255)):
    s = (s or "").lstrip("#")
    if len(s) != 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def _font(path, size):
    _, _, ImageFont = _pil()
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(path, size, index=0)


def _metrics(draw, text, font, spacing=0):
    """实测字形度量。返回 (字形顶相对文字的偏移, 字形高, 字形宽)。

    位置一律由它推出来，不猜。字号一变，猜出来的偏移必然穿帮。
    """
    body = (" " * spacing).join(list(text)) if spacing else text
    try:
        l, t, r, b = draw.textbbox((0, 0), body, font=font)
    except Exception:
        l = t = 0
        r, b = len(body) * font.size, font.size
    return t, b - t, r - l


def _line_w(draw, text, font, spacing=0):
    """一行字的实宽。"""
    return _metrics(draw, text, font, spacing)[2]


# 不能落在行首的标点（避头点）。中文排版里，标点挂在行首是大忌——
# 折行折出个「：」开头，读起来像另起一句。
_HEAD_PUNCT = "，。、；：！？）】》」』…—·%’”"


def _wrap(draw, text, font, spacing, max_w):
    """按实宽折行，返回行列表。

    中文逐字累加到装不下为止；切点若落在 ASCII 单词中间，回退到词首再断——
    中文按字断没有代价，英文断在词中间就难看了。实在一行放不下一个词时，
    只能硬断，否则会无限循环。折完再收一道避头点：行首的标点挪回上一行尾，
    宁可那一行略宽一点，也不让标点孤零零挂在行首。
    """
    if not max_w or _line_w(draw, text, font, spacing) <= max_w:
        return [text]
    lines, cur = [], ""
    for ch in text:
        if cur and _line_w(draw, cur + ch, font, spacing) > max_w:
            cut = len(cur)
            if ch.isascii() and (ch.isalnum() or ch in "_.-"):
                i = cut
                while i > 0 and cur[i - 1].isascii() and \
                        (cur[i - 1].isalnum() or cur[i - 1] in "_.-"):
                    i -= 1
                cut = i if i > 0 else cut
            lines.append(cur[:cut])
            cur = cur[cut:] + ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    for i in range(1, len(lines)):
        while lines[i] and lines[i][0] in _HEAD_PUNCT:
            lines[i - 1] += lines[i][0]
            lines[i] = lines[i][1:]
    return [ln for ln in lines if ln]


# ------------------------------------------------------------------ 排版规格
# 纵向间距一律以「字号」为基准，换算成 em 倍数，禁止与字号无关的绝对像素。
# 曾用 ty+118 这类绝对偏移给主标题下方画框线，字号 108 的字实际高约 130，
# 线正好横穿标题字。这类缺陷调参调不好，只能改结构。
# 另有 0.23×字号 是字体自带的内部行距，落在元素自身包围盒里，不计入下列比例。
LAYOUT = {
    "block_top": 0.060,         # 文字块顶边（画布高比例），横竖屏同一条规则：
                                # 主标题在任何一集、任何画幅里都落在同一高度
    "wave_max": 0.82,           # 声波装饰上限（画布高比例），防长文案把它顶出画面
    "rule_span": 0.300,         # 顶部分隔线宽度（画布宽比例）
    "sub_rule_span": 0.250,     # 副标题上框线宽度（画布宽比例）
    "rule_thickness": 0.0018,   # 分隔线粗细（画布宽比例）
    "sub_rule_thickness": 0.0028,
    # 间距比例：乘以下一个元素的字号（分隔线按上一个元素的字号）
    "gap_rule_to_text": 0.20,   # 分隔线 → 其下方文字
    "gap_brand_to_title": 1.30, # 品牌字 → 主标题（大字号自带行距已够）
    "gap_text_to_rule": 0.45,   # 主标题 → 其下方框线
    "gap_rule_to_sub": 0.17,    # 框线 → 副标题（框线贴着副标题，成组）
    "gap_para": 0.85,           # 同级段落之间
    "gap_note": 1.15,           # 结尾标识与上一段
    "gap_wave": 6.00,           # 文字块底 → 声波装饰（按署名行字号）
    # 一行字最多占画布宽的多少：留出两边安全边距。字再长就折行——宁可折成两行，
    # 也不能让字顶出画布被裁掉。折到上限行数仍装不下，才轮到缩字号。
    "text_max_ratio": 0.86,
    "text_max_lines": 3,
}

# 声波装饰的三条带。每项为 (振幅权重, 周期权重, 不透明度)：
# 振幅权重决定带高，周期权重决定疏密，两者都不与画幅或文案长度挂钩。
# 周期权重特意取不成整数比（1 : 1.37 : 1.83），避免三条波长期同相叠在一起。
WAVE_BANDS = (
    (1.00, 1.00, 0.26),
    (0.70, 1.37, 0.18),
    (0.47, 1.83, 0.12),
)
WAVE_GAP = 0.45       # 相邻波带之间的留白，按单位振幅计；必须为正，否则包络相交
WAVE_CYCLES = 3.2     # 可见宽度内的周期数（按画布宽归一，换画幅疏密不变）
WAVE_MAX_AMP = 34.0   # 单位振幅上限（按 scale 缩放），避免短文案把波拉得过大


def wave_layout(size, wave_top, scale):
    """三条声波的几何：中线纵坐标、振幅、频率、相位与横跨区间。

    留白是硬约束，不是观感调参。两条相邻波的中线间距取
    `振幅之和 + WAVE_GAP·u`，因此包络之间的净空恒为 `WAVE_GAP·u`，
    与相位、频率无关——不论波形怎么走都不会相交。
    间距若按像素写死，文案一长、画幅一变就可能小于振幅之和，
    包络从第一帧就重叠，看上去就是几根线绞成一团。

    竖屏与横屏的短边相同则字号相同，波带高度同理按 scale 缩放，
    所以同一档节目换个画幅不会忽然变密或变疏。

    返回空表表示可用高度不够，此时不画声波。
    """
    w, h = size
    avail = h * LAYOUT["wave_max"] - wave_top
    if avail <= 4 * scale:
        return []
    rs = [r for r, _c, _a in WAVE_BANDS]
    gap = WAVE_GAP
    # 总高 = 2u·Σr + 2u·gap·(n-1)：先解出单位振幅 u，再按权重分配。
    u = avail / max(1e-6, 2.0 * (sum(rs) + gap * (len(rs) - 1)))
    u = min(u, WAVE_MAX_AMP * scale)
    block = 2.0 * u * (sum(rs) + gap * (len(rs) - 1))
    cursor = wave_top + max(0.0, (avail - block) / 2.0)
    vis_l, vis_r = w * 0.17, w * 0.83
    vis = max(1.0, vis_r - vis_l)
    out = []
    for i, (r, cycles, alpha) in enumerate(WAVE_BANDS):
        amp = u * r
        cursor += amp
        out.append({
            "cy": cursor,
            "amp": amp,
            "freq": 2.0 * math.pi * (WAVE_CYCLES * cycles) / vis,
            "phase": i * 1.13,
            "alpha": alpha,
            "x0": vis_l,
            "x1": vis_r,
        })
        cursor += amp + gap * u
    return out


def wave_points(band, step=2.0):
    """把一条波带采样成折线，供直绘与几何断言共用。

    采样步长与绘制共用同一入口，测试断言的就是实际画出来的那条线，
    不会出现「算的是 A、画的是 B」。
    """
    pts = []
    x = band["x0"]
    while x < band["x1"]:
        pts.append((x, band["cy"] + band["amp"] * math.sin(
            (x - band["x0"]) * band["freq"] + band["phase"])))
        x += step
    return pts


class _Flow:
    """纵向流式排版游标。

    游标 y 始终指向上一个元素的字形底边，下一个元素按「间距 + 实测包围盒」
    推进。干跑模式只推进不落笔，用于先量出整块高度再居中。

    每个元素落下的实测区间记进 marks，供门禁与单测检查是否互相压叠。
    排版正确性靠实测数据判定，不靠肉眼看渲染图。
    """

    def __init__(self, draw, cx, y, dry=False, max_w=None, max_lines=None):
        self.d = draw
        self.cx = cx
        self.y = float(y)
        self.dry = dry
        self.max_w = max_w          # 单行宽度上限；None 表示不折行
        self.max_lines = max_lines or LAYOUT["text_max_lines"]
        self.last = 0.0            # 上一个元素的字号
        self.marks = []

    def _advance(self, em, ratio):
        self.y += (em if em is not None else self.last) * ratio

    def _mark(self, kind, label, top, bottom):
        self.marks.append({"kind": kind, "label": label,
                           "top": float(top), "bottom": float(bottom)})

    def text(self, text, font, fill, em=None, gap=0.0, spacing=0, label=""):
        if not text:
            return self
        self._advance(em if em is not None else font.size, gap)
        lines, font = self._fit(text, font, spacing)
        top_y = self.y
        for ln in lines:
            top, h, w = _metrics(self.d, ln, font, spacing)
            if not self.dry:
                body = (" " * spacing).join(list(ln)) if spacing else ln
                self.d.text((self.cx - w / 2.0, self.y), body,
                            font=font, fill=fill)
            self.y += top + h
        self._mark("text", label or text, top_y, self.y)
        self.last = font.size
        return self

    def _fit(self, text, font, spacing):
        """定这一行字用几行画、要不要缩字号。返回 (行列表, 字体)。"""
        if not self.max_w:
            return [text], font
        lines = _wrap(self.d, text, font, spacing, self.max_w)
        if len(lines) <= self.max_lines:
            return lines, font
        # 折到上限行数仍装不下，才把字号收一收重折。收到六成还不行就用它的
        # 结果——宁可挤一点，也不能凭空吞掉字。
        path = getattr(font, "path", None)
        floor = max(1, int(font.size * 0.6))
        size = font.size
        while path and size > floor:
            size = max(floor, int(size * 0.92))
            font = _font(path, size)
            lines = _wrap(self.d, text, font, spacing, self.max_w)
            if len(lines) <= self.max_lines:
                break
        return lines, font

    def rule(self, span, color, thickness, em=None, gap=0.0, label="rule"):
        self._advance(em, gap)
        if not self.dry:
            self.d.line([(self.cx - span / 2.0, self.y),
                         (self.cx + span / 2.0, self.y)],
                        fill=color, width=thickness)
        self._mark("rule", label, self.y, self.y + thickness)
        self.last = em if em is not None else self.last
        self.y += thickness
        return self

    def pill(self, label, font, color, em=None, gap=0.0):
        if not label:
            # 与 text 同一条规矩：没有字就不落笔、也不推进游标。否则期数取不到
            # 的时候画面上会留一个空框，看着像印漏了字。
            return self
        self._advance(em, gap)
        top, h, w = _metrics(self.d, label, font)
        pad_x, pad_y = font.size * 0.85, font.size * 0.42
        pw, ph = w + pad_x * 2, (top + h) + pad_y * 2
        x0, y0 = self.cx - pw / 2.0, self.y
        if not self.dry:
            self.d.rounded_rectangle([x0, y0, x0 + pw, y0 + ph],
                                     radius=int(ph * 0.34), outline=color,
                                     width=max(1, int(font.size * 0.075)))
            self.d.text((self.cx - w / 2.0, y0 + (ph - h) / 2.0 - top),
                        label, font=font, fill=color)
        self._mark("pill", label, y0, y0 + ph)
        self.last = font.size
        self.y = y0 + ph
        return self

    def skip(self, ratio, em=None):
        self._advance(em, ratio)
        return self


def _draw_sequence(flow, seq):
    """按数据化序列落笔。背景与封面共用同一套执行器。"""
    for it in seq:
        kind = it["k"]
        em, gap = it.get("em"), it.get("gap", 0.0)
        if kind == "rule":
            flow.rule(it["span"], it["color"], it["w"], em=em, gap=gap,
                      label=it.get("label", "分隔线"))
        elif kind == "pill":
            flow.pill(it["text"], it["font"], it["color"], em=em, gap=gap)
        else:
            flow.text(it["text"], it["font"], it["color"], em=em, gap=gap,
                      spacing=it.get("spacing", 0), label=it.get("label", ""))
    return flow


# 画面上的字号一律按「角色」命名，不按位置。从前叫 f_big / f_mid，主标题换成
# 节目名之后没人说得出 mid 到底是哪一行。基准是短边 1080，实际字号 = 基准 × scale。
#
# 层级（两种画面同一条）：主标题 = 节目名 ＞ 副标题 ＞ 期标题 ＞ 期数 ＞ 标语 ＞ 版权行。
# 最大的一档给节目名而不是期标题——播客卖的是系列品牌，不是这一期讲什么。
# 期标题字数不定，长起来在最大档只能折行，一期一个样，摆上去就散。
#
# 每一档给两个基准：封面一列、背景一列。品牌行与版权行两列不同，因为两张画面的
# 骨架本来就不同——背景顶部对齐、下方留给声波，封面整块纵向居中，同一个数字摆在
# 这两种骨架里看上去不是同一个大小。
FONT_SIZES = {
    # 角色:        (封面基准, 背景基准)
    "brand":       (30, 34),      # 品牌行（配置·全局）
    "hero":        (104, 104),    # 主标题 = 节目名（项目）
    "second":      (44, 44),      # 副标题（项目，可留空）
    "note":        (22, 22),      # 标语（配置·全局，可留空）
    "episode":     (32, 32),      # 期标题（期）
    "pill":        (26, 26),      # 期数
    "small":       (20, 19),      # 版权行（配置·全局）
}
# 方图（1×1）左右只剩 1080，主标题照 104 排会顶到边，单独收一档。
COVER_SQUARE_OVERRIDES = {"hero": 88}


# ------------------------------------------------------------------ 画面元素清单
# 画面上要印的每一行，出处只有这一张表。从前封面与背景各写一份元素序列，同一个
# 元素在两处各描述一遍——改一处忘一处，两张画面就各印各的了。
#
# 每个字段的读法：
#   key    取值键，对上 lines_text 里的键名
#   label  这一行叫什么（报错与测试靠它认人）
#   who    这行字**跟谁走**：全局 = 所有项目共用一份配置；项目 = 每个节目一套；
#          期 = 每一期都不一样
#   source 「跟谁」的展开，写清值从哪来（代码取的是 lines_text，这一列给人看）
#   form   落笔形式：text 是普通的字，pill 是带圆角框的标识
#   font   字号档（对 FONT_SIZES 的键）
#   color  颜色档（对 accent / light / muted 三色）
#   cover  印不印在封面上
#   bg     印不印在背景上
#
# 封面跟项目、不跟期，所以期标题与期数不进封面；标语是整档节目的调性，两张画面都印。
FRAME_LINES = (
    # key           名称      跟谁    取值来源                    形式    字号档      颜色      封面   背景
    ("brand",       "品牌行", "全局", "project.brand",          "text", "brand",   "accent", True,  True),
    ("program",     "主标题", "项目", "project.program_name",   "text", "hero",    "accent", True,  True),
    ("subtitle",    "副标题", "项目", "project.subtitle",       "text", "second",  "light",  True,  True),
    ("tagline",     "标语",   "全局", "project.tagline",        "text", "note",    "light",  True,  True),
    ("title",       "期标题", "期",   "本期地图里的标题",         "text", "episode", "light",  False, True),
    ("episode_no",  "期数",   "期",   "项目进度（第 N 期）",      "pill", "pill",    "accent", False, True),
    ("attribution", "版权行", "全局", "project.attribution",    "text", "small",   "muted",  True,  True),
)


def _font_set(font_path, scale, role, overrides=None):
    """把角色→(封面基准, 背景基准) 的字号表，换成角色→字体对象。

    `role` 取 "cover" 或 "bg"，决定取两列里的哪一列。
    """
    col = 0 if role == "cover" else 1
    spec = {k: v[col] for k, v in FONT_SIZES.items()}
    spec.update(overrides or {})
    return {k: _font(font_path, max(1, int(v * scale))) for k, v in spec.items()}


# 顶部分隔线的粗细：两画幅基准不同——背景按画布宽、封面按品牌行字号。这不是
# 笔误，是两张画面各自的尺寸集合决定的（封面要出 1×1 方图，同一比例挪到方图上
# 会偏粗）。要合成一个基准是一句话的事，但那是改视觉，得先问。
RULE_WEIGHT = {
    "cover": lambda fonts, w: max(1, int(fonts["brand"].size * 0.06)),
    "bg": lambda fonts, w: max(1, int(w * LAYOUT["rule_thickness"])),
}


def frame_sequence(role, lines_text, fonts, colors, w, preset=""):
    """按画幅取要印的元素序列。`role` 取 "cover"（封面）或 "bg"（背景）。

    两张画面共用这一份构造器：印哪几行由 FRAME_LINES 的两列开关决定，于是
    「封面上有什么」不会再跟「背景上有什么」各说各话。

    先后与间距写在这里而不进表——间距属于排版，不属于元素身份。一律按「某个
    字号 × LAYOUT 里的比例」推进，基准取谁的档写在每一行上。

    封面跟项目、不跟期，所以期标题与期数不进封面；背景跟期，两样都印。
    """
    accent, light, muted = colors
    tone = {"accent": accent, "light": light, "muted": muted}
    L = LAYOUT
    flag = 7 if role == "cover" else 8      # FRAME_LINES 里封面/背景那两列
    rows = {row[0]: row for row in FRAME_LINES}

    def want(key):
        return bool(rows[key][flag])

    def line(key, gap, em_role=None, font_role=None, spacing=1):
        row = rows[key]
        text = str(lines_text.get(key) or "")
        if row[4] == "pill":
            # 期数落成「第 N 期」：外框与文字是一体的标识，包装写在这里，不让
            # 调用方自己拼好再交进来——否则「1」与「第 1 期」两种写法迟早各出现一次。
            text = "第 %s 期" % text if text else ""
        return {"k": "text" if row[4] == "text" else "pill",
                "text": text,
                "font": fonts[font_role or row[5]],
                "color": tone[row[6]], "spacing": spacing,
                "em": fonts[em_role].size if em_role else None,
                "gap": gap, "label": row[1]}

    # 极简档（只有封面有这个档）：不印品牌行与两条分隔线，其余照印。头两行因此
    # 成了首元素，上方不该再留间距，所以这两个间距在极简档下归零。
    bare = (role == "cover" and preset == "minimal")
    # 「标题主导」档（只有封面有）：副标题与期标题的档位对调。封面不印期标题之后，
    # 这一档剩下的效果就是副标题换一档字号。
    swap = (role == "cover" and preset == "episode")

    seq = []
    if not bare:
        seq.append({"k": "rule", "span": w * L["rule_span"], "color": accent,
                    "w": RULE_WEIGHT[role](fonts, w), "label": "顶部分隔线"})
    if want("brand") and not bare:
        seq.append(line("brand", L["gap_rule_to_text"], em_role="brand",
                        spacing=2))
    if want("program"):
        seq.append(line("program", 0.0 if bare else L["gap_brand_to_title"],
                        em_role="brand", spacing=2))
    if not bare:
        # 这条线是主标题的下沿，把品牌块与下面的本期信息分开。间距按主标题字号
        # 取一个固定比例，随后无论下一行多大都贴不上标题字。
        seq.append({"k": "rule", "span": w * L["sub_rule_span"], "color": accent,
                    "w": max(1, int(w * L["sub_rule_thickness"])),
                    "em": fonts["hero"].size, "gap": L["gap_text_to_rule"],
                    "label": "主标题下框线"})
    if want("subtitle"):
        f_role = "episode" if swap else None
        seq.append(line("subtitle", 0.0 if bare else L["gap_rule_to_sub"],
                        em_role=f_role or "second", font_role=f_role))
    if want("title"):
        seq.append(line("title", L["gap_para"], em_role="second"))
    if want("episode_no"):
        seq.append(line("episode_no", L["gap_note"], em_role="second"))
    if want("tagline"):
        seq.append(line("tagline", L["gap_para"], em_role="episode"))
    if want("attribution"):
        # 间距基准取「副标题」这一档，而不是上一个元素的字号：版权行上面可能是
        # 标语（小字）也可能空着（于是它头顶就是期标题）。基准跟着上面变的话，
        # 同一种排法会因为「有没有填标语」而给出两种间距。
        seq.append(line("attribution", L["gap_para"], em_role="second"))
    return seq


def background_layout(size, seq, wave_em, max_w=None):
    """量出背景的排版几何。返回 (文字块起点, 声波基线, 干跑游标)。

    绘制与测试共用这一个入口，避免两处各算一遍位置——位置算两遍必然对不上。
    干跑与实绘必须拿到同一个 max_w：一处折行、一处不折，量出来的块高就差了
    一整行，声波会贴到字上。

    文字块顶边钉在 block_top，横竖屏同一条规则；块内元素按实测包围盒往下推，
    声波跟着块底走。这样主标题在任何一集、任何一种画幅里都落在同一高度——换成
    竖屏不会被推下去一截；品牌行、标语、署名填不填，只改变块底与声波的位置，
    不会让主标题上下漂移。声波按画布高度钉死也不行：横屏贴得刚好、竖屏离出一大段。
    """
    w, h = size
    L = LAYOUT
    draw = _pil()[1].Draw(_pil()[0].new("RGB", (8, 8)))
    probe = _draw_sequence(_Flow(draw, w // 2, 0.0, dry=True, max_w=max_w), seq)
    start = h * L["block_top"]
    wave_y = min(start + probe.y + wave_em * L["gap_wave"], h * L["wave_max"])
    return start, wave_y, probe


def watermark_box(size):
    """水印所在区域（像素坐标）。绘制与检测共用，避免两处几何漂移。"""
    w, h = size
    return (int(w * WATERMARK_REGION[0]), 0, w, int(h * WATERMARK_REGION[1]))


def _bg_luma(img, box):
    """区域背景亮度估计：灰度中位数。

    水印文字只占区域内很小一部分，中位数不受其影响，因此可当作背景亮度。
    """
    g = img.convert("L").crop(box)
    data = list(g.getdata())
    data.sort()
    return data[len(data) // 2]


def _add_watermark(img, text=WATERMARK_TEXT):
    """右上角绘制 AI 生成标识。

    颜色按落点处背景亮度自适应：深底用浅字、浅底用深字。
    固定浅色字在浅色底上等于没画，固定深色字在深色底上同理。
    """
    Image, ImageDraw, ImageFont = _pil()
    rgb = img.convert("RGB")
    w, h = rgb.size
    try:
        f = ImageFont.truetype(resolve_font()["path"], max(18, w // 60))
    except AssetError:
        f = ImageFont.load_default()

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    try:
        bb = d.textbbox((0, 0), text, font=f)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
    except Exception:
        tw, th = len(text) * 20, 20

    x = max(0, w - tw - WATERMARK_MARGIN[0])
    y = WATERMARK_MARGIN[1]
    probe = (x, y, min(w, x + tw), min(h, y + th + 8))
    ref = _bg_luma(rgb, probe)
    color = WATERMARK_DARK if ref > WATERMARK_SPLIT_LUMA else WATERMARK_LIGHT

    d.text((x, y), text, font=f, fill=color + (WATERMARK_ALPHA,))
    return Image.alpha_composite(rgb.convert("RGBA"), overlay).convert("RGB")


def check_watermark(path, min_pixels=None):
    """水印门禁：区域内与背景亮度差达标的像素数。

    判据与 _add_watermark 同源（同一区域常量、同一对比度阈值），
    因此不会出现"画了却检不出"或"没画却检出"。
    """
    Image, _, _ = _pil()
    limit = WATERMARK_MIN_PIXELS if min_pixels is None else int(min_pixels)
    img = Image.open(path).convert("RGB")
    box = watermark_box(img.size)
    ref = _bg_luma(img, box)
    g = img.convert("L").crop(box)
    count = sum(1 for v in g.getdata() if abs(v - ref) >= WATERMARK_MIN_CONTRAST)
    return {"ok": count >= limit, "pixels": count, "threshold": limit, "bg_luma": ref}


# ------------------------------------------------------------------ 背景
def make_background(preset, size, lines_text, out_path, watermark=True,
                    font_family=""):
    """生成背景图（Pillow 直绘，不依赖浏览器渲染）。

    印哪几行一律取自 `lines_text`（见 FRAME_LINES）——这里不再另收 title /
    subtitle / attribution 一类的散参数：多一条入参就多一个「传了但没用上」
    或「两处传得不一样」的口子，而那两处单看都挑不出毛病。

    `font_family` 是画面字体（配置项 `frame.font_family`）。留空即自动挑一款。
    封面与背景拿同一款，画面上的字才是一套。
    """
    Image, ImageDraw, _ = _pil()
    opts = MODE_SPEC["background.preset"]["options"]
    cfg = opts.get(preset) or opts["ink"]
    accent = _hex(cfg["accent"])
    light = _hex(cfg["light"])
    muted = _hex(cfg["muted"])
    w, h = size
    # 字号按短边缩放：竖屏（1080×1920）与横屏（1920×1080）短边相同，
    # 字号也就相同。只按宽度缩放会把竖屏字号砍掉近一半。
    scale = min(w, h) / 1080.0

    img = _vgradient(size, cfg["bg_top"], cfg["bg_bottom"])
    d = ImageDraw.Draw(img)

    font_path = resolve_font(font_family)["path"]
    fonts = _font_set(font_path, scale, "bg")

    cx = w // 2
    seq = frame_sequence("bg", lines_text, fonts, (accent, light, muted), w)
    for it in seq:
        if it["k"] != "rule":
            assert_glyphs(it["font"], it["text"], "背景「%s」" % it["label"])

    # 文案行数由配置决定，可能只有标题一行，也可能品牌、标语、署名齐全。
    # 块顶钉在 block_top，行数变化只影响块底和跟着块底走的声波，主标题不动。
    max_w = w * LAYOUT["text_max_ratio"]
    start, wave_y, flow = background_layout(size, seq, fonts["small"].size,
                                            max_w=max_w)
    _draw_sequence(_Flow(d, cx, start, max_w=max_w), seq)
    # 声波装饰。三条波各占一条垂直带，带间留白大于相邻振幅之和，
    # 因此无论相位怎么走都不会相交。几何由 wave_layout 算出，测试直接断言它。
    #
    # 原先三条波振幅 22/17/12、间距写死 36/72：36 < 22+17，相邻包络本就重叠，
    # 频率又只差千分之三，相位周期性重合——看上去就是几根线绞成一团。
    # 频率还与画幅无关地写死（0.010 rad/px），换个画幅波形疏密又变一次。
    for band in wave_layout(size, wave_y, scale):
        col = tuple(int(accent[k] * band["alpha"]) for k in range(3))
        d.line(wave_points(band), fill=col, width=max(1, int(2 * scale)))

    if watermark:
        img = _add_watermark(img)
    img.save(out_path)
    return {"path": out_path, "size": [w, h], "preset": preset}


# ------------------------------------------------------------------ 封面
COVER_SIZES = {
    "16x9": (1920, 1080),
    "3x4": (1080, 1440),
    "1x1": (1080, 1080),
}


def make_covers(preset, lines_text, out_dir, prefix="", font_family=""):
    """生成三尺寸封面。返回 {尺寸键: 路径}。

    印哪几行取自 `lines_text`（见 FRAME_LINES），与背景同一份构造器：封面跟
    项目、不跟期，所以期标题与期数不进封面。

    `prefix` 是文件名前缀（期号）——各封面同处一个「封面」目录，不带前缀就会
    后一期盖掉前一期，而各期封面本来就该并存。

    整块纵向居中：先干跑量出块高，再从居中起点正式落笔。
    行距由字号推出，不再用固定像素——固定值在大字号下会把主副标题挤在一起。
    """
    Image, ImageDraw, _ = _pil()
    opts = MODE_SPEC["background.preset"]["options"]
    cur = opts.get(lines_text.get("bg_preset", "ink")) or opts["ink"]
    accent = _hex(cur["accent"])
    light = _hex(cur["light"])
    muted = _hex(cur["muted"])
    font_path = resolve_font(font_family)["path"]
    L = LAYOUT

    out = {}
    for key, (w, h) in COVER_SIZES.items():
        scale = min(w, h) / 1080.0
        override = COVER_SQUARE_OVERRIDES if key == "1x1" else None
        fonts = _font_set(font_path, scale, "cover", override)
        img = _vgradient((w, h), cur["bg_top"], cur["bg_bottom"])
        d = ImageDraw.Draw(img)
        cx = w // 2

        seq = frame_sequence("cover", lines_text, fonts,
                             (accent, light, muted), w, preset=preset)
        for it in seq:
            if it["k"] != "rule":
                assert_glyphs(it["font"], it["text"],
                              "封面 %s「%s」" % (key, it.get("label") or it["text"]))
        max_w = w * L["text_max_ratio"]
        probe = _draw_sequence(_Flow(d, cx, 0.0, dry=True, max_w=max_w), seq)
        _draw_sequence(_Flow(d, cx, max(0.0, (h - probe.y) / 2.0),
                             max_w=max_w), seq)

        img = _add_watermark(img)
        path = os.path.join(out_dir, "%scover_%s.png" % (prefix, key))
        img.save(path)
        out[key] = path
    return out


# ------------------------------------------------------------------ BGM
# 内置 BGM 是包内预生成的真实器乐循环（resources/bgm/<档位>.wav，
# 24.94s 无缝循环体，AI 本地生成、无第三方版权）。旧的正弦合成垫已退役。
BGM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "resources", "bgm")


def bgm_resource(preset):
    """内置档位 → 包内资源文件绝对路径。资源缺失=停产报错，绝不静默降级。"""
    opts = MODE_SPEC["bgm.preset"]["options"]
    if preset not in opts:
        raise ValueError("未知的背景音乐档位：%s（可选：%s）"
                         % (preset, "、".join(sorted(opts))))
    path = os.path.join(BGM_DIR, "%s.wav" % preset)
    if not os.path.exists(path):
        raise RuntimeError(
            "内置背景音乐资源缺失：%s（档位 %s）。资源目录 resources/bgm/ "
            "不完整，请恢复文件后重跑。" % (path, preset))
    return path


def build_all(cfg, dirs, lines_text, log=None, prefix=""):
    """生成本集所有代码侧资源：背景（横/竖）+ 封面三尺寸 + BGM。

    `dirs` 给出各类资源各去哪个目录：`{"bg": …背景…, "cover": …封面…}`。
    从前只有一个 `out_dir`，那是在「一集一个目录」的前提下；现在项目里各类
    产物各归其位，背景与封面不再同处一室，所以按类给目录。

    `prefix` 是文件名前缀（期号）。各期封面同处一个目录，不带前缀就会后一期
    盖掉前一期。预览调用不带前缀，文件名与从前一致。
    """
    log = log or (lambda m: None)
    bg_dir = dirs.get("bg") or dirs.get("cover") or ""
    cover_dir = dirs.get("cover") or bg_dir
    for d in (bg_dir, cover_dir):
        if d:
            os.makedirs(d, exist_ok=True)
    # 画面上印什么，一律以 lines_text 为准。它由调用方一次备齐（见 pipeline），
    # 这里不再各自翻 cfg 兜底——兜底链多一环，就多一个「配置里填了却不生效」
    # 的死框：那一环永远够不着，而界面上那个框长得跟真的一样。
    #
    # 印哪几行、从哪来、跟谁走，全在 FRAME_LINES 那一张表里；这里只管把材料
    # 递进去，不再替某一张画面单独挑参数。
    font_family = cfg.get("frame.font_family", "")

    bg_h = make_background(cfg.get("background.preset", "ink"),
                           (int(cfg.get("video.width", 1920)),
                            int(cfg.get("video.height", 1080))),
                           lines_text,
                           os.path.join(bg_dir, "%sbg.png" % prefix),
                           font_family=font_family)
    log("背景图（横屏）已生成")

    bg_v = make_background(cfg.get("background.preset", "ink"),
                           (int(cfg.get("video.height", 1080)),
                            int(cfg.get("video.width", 1920))),
                           lines_text,
                           os.path.join(bg_dir, "%sbg_v.png" % prefix),
                           font_family=font_family)
    log("背景图（竖屏）已生成")

    covers = make_covers(cfg.get("cover.preset", "book"), lines_text,
                         cover_dir, prefix=prefix, font_family=font_family)
    log("封面三尺寸已生成")

    # AIGC 元数据隐式标识（显式水印已在绘制时叠加，两者互不替代）。
    # 逐张打标，失败即停——合规动作不允许静默跳过某一张。
    if aigc_label.labeled(cfg):
        for p in [bg_h["path"], bg_v["path"]] + list(covers.values()):
            aigc_label.tag_image(p, cfg)
        log("AIGC 元数据已写入背景与封面")

    bgm_path = None
    if cfg.get("bgm.mode", "builtin") == "builtin":
        preset = cfg.get("bgm.preset", "pensive")
        bgm_path = bgm_resource(preset)
        log("背景音乐（%s）已就位" % MODE_SPEC["bgm.preset"]["options"]
            .get(preset, {}).get("label", preset))

    return {"bg_h": bg_h["path"], "bg_v": bg_v["path"],
            "covers": covers, "bgm": bgm_path}
