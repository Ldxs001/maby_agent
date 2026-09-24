#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单行滚动档的真帧复验（libass 实渲，不是推算）。

盯三件事：

1. **三帧不丢字**——句首（首停期内）/ 滚动中 / 收尾（滚动终点前 0.1 秒）各取一帧，
   逐帧看图 + 量墨迹包围盒，并与框的左右缘对账；
2. **字宽标定对账**——把同一句放进一张 4000px 宽的画布、不裁不滚，量出真实墨迹宽，
   与 `text_px_width` 的算式结果比（验收口径：算式**不得更窄**，偏差 ≤ 2%）；
3. **滚得完**——直接从 ASS 的 `\\move` 读四个数，验证滚动终点落在句末之内
   （容差口径见下面的 `TAIL_TOLERANCE`）。

跑法：python tools/probes/subtitle_roll_check.py
产物：`_smoke/roll_*.png`、`_smoke/roll.ass`、`_smoke/roll_measure.json`；
     逐帧的中间帧落在 `_smoke/_roll_frames/`（可整目录删）。
"""

import io
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.join(ROOT, "_smoke")
FRAMES = os.path.join(HERE, "_roll_frames")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import assets_factory as A                     # noqa: E402
from podcast_maker import subtitle_engine as S                    # noqa: E402
from podcast_maker.config_manager import ConfigManager            # noqa: E402
from podcast_maker.video_engine import _ff_escape                 # noqa: E402
from PIL import Image                                             # noqa: E402

#: 一句 79 字的长台词（与 `web_ui.SUB_PREV_LONG`、`tests/test_subtitle_wrap.py`
#: 的 `LONG` 同一句）——短句装不下看不出横滚，必须用溢出的样本。
LONG = ("上期《骨架叙事切分与介质边界的确定性约束》聊的是聚焦成书的结构性排版，"
        "揭示注册列表驱动的四部叙事弧线、页面物理边界（断页/字体/缩放）与"
        "元数据口径的刚性同步机制。")
SPAN = 27.1                     # 这句话的估时（秒），与冻结值一致
W, H = 1920, 1080
FPS = 10
BOX_BAND = (890, 1010)          # 框所在的横带（上沿留到 890，把描边与抗锯齿都包进来）

#: 「终点 ≤ 句末」这条只能断言到 20ms 以内：ASS 时间戳只有 10ms 一格
#: （`fmt_ass_time` 是 `%05.2f`），`\move` 的终点又是 `round(span × 1000)` 毫秒，
#: 事件起止各落格一次——两端各最多差 5ms。差额是取整残差，**不是滚不完**。
#: 拿 `end <= span` 直接卡会误报（实测差 3ms）。
TAIL_TOLERANCE = 0.020


def ff(args):
    p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if p.returncode != 0:
        print(p.stderr.decode("utf-8", "replace")[-1500:])
        raise SystemExit("ffmpeg 失败：%s" % args)
    return p


def build():
    cfg = ConfigManager().data()
    cfg.update({"subtitle.preset": "single",
                "speaker_indicator.name_shown": False,
                "subtitle.highlight": False})
    script = [{"speaker": "B", "text": LONG}]
    timings = [{"start": 0.0, "end": SPAN}]
    ass, mc, axis = S.build_ass(script, cfg, timings, W, H, "")
    assert axis == "x", "没走到横滚那一支"
    path = os.path.join(HERE, "roll.ass")
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(ass)
    row = [ln for ln in ass.splitlines()
           if ln.startswith("Dialogue:") and "\\p1" not in ln][0]
    mv = re.search(r"\\move\((-?\d+),(-?\d+),(-?\d+),(-?\d+),(\d+),(\d+)\)", row)
    assert mv, "文字事件里没有 \\move，没走三段式"
    g = S.frame_geometry(cfg, W, H, "", 1)
    return {"ass": path, "g": g, "max_chars": mc, "axis": axis,
            "text_px": S.text_px_width(LONG, g["size"]),
            "move": [int(x) for x in mv.groups()], "row": row, "font": "HarmonyOS Sans SC"}


def render_roll(info):
    fontsdir = A.fonts_dir_of(A.resolve_font(info["font"]))
    vf = "ass=%s:fontsdir=%s" % (_ff_escape(info["ass"]), _ff_escape(fontsdir))
    ff(["ffmpeg", "-y", "-f", "lavfi",
        "-i", "color=c=black:s=%dx%d:d=%d" % (W, H, int(SPAN) + 1),
        "-vf", vf, "-r", str(FPS), os.path.join(FRAMES, "f%04d.png")])
    return vf


def ink_bbox(png, x_lim=0):
    im = Image.open(png).convert("L")
    px = im.load()
    bw, bh = im.size
    left, right, top, bottom = bw, -1, bh, -1
    for y in range(BOX_BAND[0], BOX_BAND[1]):
        for x in range(x_lim, bw):
            if px[x, y] > 40:
                left = min(left, x)
                right = max(right, x)
                top = min(top, y)
                bottom = max(bottom, y)
    return {"left": left, "right": right, "top": top, "bottom": bottom}


def render_wide(info):
    """同一句、不裁不滚，放进 4000px 宽的画布量真实墨迹宽。"""
    g = info["g"]
    with io.open(info["ass"], encoding="utf-8") as fh:
        text = fh.read()
    row = re.sub(r"\{[^}]*\}",
                 lambda _m: "{\\an4\\pos(0,%d)}" % int(round(g["anchor"])),
                 info["row"], count=1)
    head = text.split("[Events]")[0].replace("PlayResX: %d" % W, "PlayResX: 4000")
    wide = head + "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, " \
                  "MarginR, MarginV, Effect, Text\n" + row + "\n"
    path = os.path.join(HERE, "roll_wide.ass")
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(wide)
    fontsdir = A.fonts_dir_of(A.resolve_font(info["font"]))
    vf = "ass=%s:fontsdir=%s" % (_ff_escape(path), _ff_escape(fontsdir))
    png = os.path.join(HERE, "roll_wide.png")
    ff(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=4000x%d" % H,
        "-vf", vf, "-frames:v", "1", png])
    return png, row


def main():
    for d in (HERE, FRAMES):
        if not os.path.isdir(d):
            os.makedirs(d)
    info = build()
    g = info["g"]
    m = info["move"]
    hold, roll_end = m[4] / 1000.0, m[5] / 1000.0
    print(json.dumps({"text_px": info["text_px"], "box_w": g["x2"] - g["x1"],
                      "move": m, "hold_s": hold, "roll_end_s": roll_end,
                      "sentence_end_s": SPAN}, ensure_ascii=False))
    print("filter:", render_roll(info))

    picks = {"first": 7.0, "mid": round((hold + roll_end) / 2, 2),
             "last": round(roll_end - 0.1, 2), "end": round(roll_end, 2)}
    got = {}
    for tag, t in picks.items():
        idx = int(round(t * FPS)) + 1
        src = os.path.join(FRAMES, "f%04d.png" % idx)
        dst = os.path.join(HERE, "roll_" + tag + ".png")
        if not os.path.exists(src):
            got[tag] = {"t": t, "missing": src}
            continue
        Image.open(src).save(dst)
        ink = ink_bbox(dst)
        got[tag] = {"t": t, "png": dst, "ink": ink,
                    "right_gap_to_box": g["x2"] - ink["right"],
                    "left_gap_to_box": ink["left"] - g["x1"]}

    wide_png, wide_row = render_wide(info)
    wide_ink = ink_bbox(wide_png)
    measured = wide_ink["right"] - wide_ink["left"] + 1
    report = {
        "roll": {"hold_s": hold, "roll_end_s": roll_end, "sentence_end_s": SPAN,
                 "tail_tolerance_s": TAIL_TOLERANCE,
                 "finishes_in_time": roll_end <= SPAN + TAIL_TOLERANCE,
                 "residual_ms": round((roll_end - SPAN) * 1000, 1),
                 "displacement_px": m[0] - m[2],
                 "text_px": info["text_px"],
                 "box_w": g["x2"] - g["x1"]},
        "frames": got,
        "calibration": {"measured_px": measured, "calc_px": info["text_px"],
                        "calc_minus_measured": info["text_px"] - measured,
                        "rel": (info["text_px"] - measured) / measured,
                        "calc_not_narrower": info["text_px"] >= measured,
                        "within_2pct": abs(info["text_px"] - measured) / measured <= 0.02},
        "wide_row": wide_row,
    }
    with io.open(os.path.join(HERE, "roll_measure.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
