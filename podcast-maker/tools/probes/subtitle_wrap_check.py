# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
# SPDX-License-Identifier: Apache-2.0
"""单行滚动档「恒为一行」的真渲探针。

为什么需要它：单测能验算式、能验标签写法，**验不了渲染器自己会不会折行**。
「单行滚动出现双行」这个 bug 就是这么漏掉的——ASS 里一个字没写错（单条
`\\move`、`\\clip` 到框、没有 `\\N`），而 libass 因为 `WrapStyle` 允许自动
折行，把 2458 px 宽的整句在 1740 px 的可用宽里折成了两行，两行又都落在
70 px 高的框内，于是屏幕上并排两行小字。单测全绿，画面是错的。

判据：取每档文字最宽的几句，**逐句单独渲一帧**，量墨迹的纵向行区段数。
  · 单行滚动档（axis=x）→ 必须恒为 1 段；
  · 歌词档（axis=y）     → 必须等于我们自己折出来的行数（`\\N` 计数 + 1）。
单条渲染时把 `\\move` 换成 `\\pos`（终点位置），避开运动模糊——这里审的是
折行，不是运动。

用法：
    python tools/probes/subtitle_wrap_check.py [脚本.json] [--top=N]
默认取 `projects/20260917-015336/脚本/2c.json`。
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, ROOT)

from podcast_maker import assets_factory as A           # noqa: E402
from podcast_maker import duration_model as D           # noqa: E402
from podcast_maker import subtitle_engine as S          # noqa: E402
from podcast_maker import video_engine as V             # noqa: E402
from podcast_maker.config_manager import ConfigManager   # noqa: E402

BS = chr(92)
BENCH = os.path.join(ROOT, '_smoke', '_bench')
MOVE = re.compile(BS + BS + r"move\((-?\d+),(-?\d+),(-?\d+),(-?\d+),(\d+),(\d+)\)")
P1 = BS + 'p1'


def ink_bands(png):
    """墨迹的纵向行区段：逐行数「亮于阈值」的像素，取连续段（≥4 px 高）。"""
    from PIL import Image
    im = Image.open(png).convert('L')
    px = im.load()
    bands, cur = [], None
    for y in range(im.height):
        n = sum(1 for x in range(0, im.width, 2) if px[x, y] > 200)
        if n > 4:
            cur = [y, y] if cur is None else [cur[0], y]
        else:
            if cur and cur[1] - cur[0] >= 4:
                bands.append(tuple(cur))
            cur = None
    if cur and cur[1] - cur[0] >= 4:
        bands.append(tuple(cur))
    return bands


POS = re.compile(BS + BS + r"pos\((-?\d+),(-?\d+)\)")


def _lands_on(body, cx, cy):
    """这条事件的文字块是否最终落在 (cx, cy)——歌词档用来挑出「当前句」。"""
    m = MOVE.search(body)
    if m:
        return int(m.group(3)) == cx and int(m.group(4)) == cy
    m = POS.search(body)
    return bool(m) and int(m.group(1)) == cx and int(m.group(2)) == cy


def header_and_event(ass, event):
    """一份只含这一条事件的 ASS（头部原样），并把 `\\move` 换成终点 `\\pos`。"""
    head = [l for l in ass.splitlines() if not l.startswith('Dialogue:')]
    f = event.split(',', 9)
    body = MOVE.sub(lambda m: r"\pos(%s,%s)" % (m.group(3), m.group(4)), f[9])
    return '\n'.join(head + [','.join(f[:9] + [body])]) + '\n'


def render_frame(ass_path, t, png, w, h):
    font = A.resolve_font(ConfigManager().data().get('subtitle.font_family', ''))
    vf = "ass=%s:fontsdir=%s" % (V._ff_escape(os.path.abspath(ass_path)),
                                 V._ff_escape(os.path.abspath(A.fonts_dir_of(font))))
    cmd = [V.ffmpeg_bin(), '-y', '-hide_banner', '-loglevel', 'error',
           '-f', 'lavfi', '-t', '%.3f' % (t + 0.05),
           '-i', 'color=c=0x202020:s=%dx%d:r=30' % (w, h),
           '-vf', vf, '-ss', '%.3f' % t, '-frames:v', '1', png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or '')[-400:])


def secs(s):
    hh, mm, ss = s.strip().split(':')
    return int(hh) * 3600 + int(mm) * 60 + float(ss)


def timings_of(script, cfg):
    """按估时拼一条时间轴：探针只需形状真实，不必是某一期的真时间轴。"""
    t, out = 0.0, []
    pause = float(cfg.get('audio.pause_between_lines', 0.35))
    for it in script:
        d = float(it.get('estimated_seconds') or 0) or D.estimate_line(
            it.get('text', ''), it.get('speaker', 'A'), cfg)
        out.append({'start': t, 'end': t + d, 'speaker': it.get('speaker', 'A')})
        t += d + pause
    return out


def main():
    argv = sys.argv[1:]
    top = 3
    paths = []
    for a in argv:
        if a.startswith('--top'):
            top = int(a.split('=')[1]) if '=' in a else 3
        else:
            paths.append(a)
    script_path = paths[0] if paths else os.path.join(
        ROOT, 'projects', '20260917-015336', '脚本', '2c.json')
    script = json.load(io.open(script_path, encoding='utf-8'))
    cfg = ConfigManager().data()
    os.makedirs(BENCH, exist_ok=True)

    fails = 0
    for preset, suffix, w, h in (('single', '', 1920, 1080),
                                 ('single', '_v', 1080, 1920),
                                 ('lyric', '', 1920, 1080),
                                 ('lyric', '_v', 1080, 1920)):
        cfg['subtitle.preset'] = preset
        size = int(cfg.get('subtitle.font_size_vertical' if suffix == '_v'
                           else 'subtitle.font_size', 52))
        ass, max_chars, axis = S.build_ass(script, cfg, timings_of(script, cfg),
                                           w, h, suffix)
        if 'WrapStyle: 2' not in ass:
            print('FAIL 头部没有禁止自动折行：%s%s' % (preset, suffix))
            fails += 1
        ev = [l for l in ass.splitlines() if l.startswith('Dialogue:')
              and P1 not in l.split(',', 9)[9][:80]]
        g = S.frame_geometry(cfg, w, h, suffix,
                             int(S.preset_of(cfg)[1].get('frame_rows', 8)))
        if axis == 'y':
            # 歌词档每句会为「当前 + 上下文」各出一条事件。上下文那几条一是不在
            # 框心、二是被刻意压暗（亮度约 133，探针的墨迹阈值 200 根本量不到），
            # 拿它量行数只会量出噪声。当前句的纵向落点恰是 anchor——静止时写
            # `\pos(cx, anchor)`，跟着窗口上滚时写 `\move(cx, 起点, cx, anchor, …)`，
            # 就按「纵向终点 == 框心」这一条判据只留当前句。
            ev = [l for l in ev if _lands_on(l.split(',', 9)[9], g['cx'],
                                             int(round(g['anchor'])))]
        ranked = sorted(ev, key=lambda l: -S.text_px_width(
            re.sub(r'^\{[^}]*\}', '', l.split(',', 9)[9]).replace(BS + 'N', ''), size))
        tag = preset + (suffix or '_h')
        print('==== %s：%d×%d，轴 %s，每行容量 %s 字，框内可用宽 %d px，%d 条文字事件 ===='
              % (tag, w, h, axis, max_chars, g['x2'] - g['x1'], len(ev)))
        for line in ranked[:top]:
            f = line.split(',', 9)
            t = secs(f[1]) + 0.15 * (secs(f[2]) - secs(f[1]))
            plain = re.sub(r'^\{[^}]*\}', '', f[9])
            rows = plain.split(BS + 'N')
            expect = 1 if axis == 'x' else len(rows)
            tmp = os.path.join(BENCH, 'wrapchk.ass')
            io.open(tmp, 'w', encoding='utf-8').write(header_and_event(ass, line))
            png = os.path.join(BENCH, 'wrapchk_%s.png' % tag)
            render_frame(tmp, t, png, w, h)
            got = ink_bands(png)
            ok = len(got) == expect
            fails += 0 if ok else 1
            widest = max(rows, key=len)
            print('  %s 期望 %d 行 / 实测 %d 行（行区段 %s）'
                  % ('PASS' if ok else 'FAIL', expect, len(got), got))
            print('       最长一行 %d 字 → 算式宽 %.0f px / 可用宽 %d px'
                  % (len(widest), S.text_px_width(widest, size), g['x2'] - g['x1']))
            print('       %s' % plain.replace(BS + 'N', ' ⏎ ')[:76])
            print('       图：%s' % png)
    print()
    print('结论：%s' % ('全部 PASS' if not fails else '** %d 项 FAIL **' % fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
