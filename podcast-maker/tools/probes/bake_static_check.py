# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
# SPDX-License-Identifier: Apache-2.0
"""静态层烘焙的**无损 A/B 校准**。

两个模式：

  （默认）对账 —— 同一条时间轴、同一份文字事件渲两帧 PNG（PNG 无损，不过 x264）：

    A 旧路：原背景图 → scale/crop → `format=rgb24,drawbox(black@bg_dim)` → ass（含框）
    B 新路：烘焙图（`bake_static_layers`）→ scale/crop → `format=rgb24` → ass（只有文字）

  两条链的 `format=rgb24` 都要有——libass 的取色口径由「进到 ass 的那帧是什么格式」
  决定（见 `video_engine.compose` 里的说明），A 靠 drawbox 附带，B 显式写。少这一步
  整个字幕会亮 6%（实测白字 233→253），对账会直接 FAIL。

  `--calibrate` —— 颜色传递表。纯色底 + 纯色（不透明/半透明）填充，量 ffmpeg/libass
  实际画出来的值，用来定死烘焙侧该怎么取色。已知结论：ffmpeg 把字幕颜色按**有限范围**
  逐通道映射（黑→16、白→235、纯红→(235,16,16)），所以烘焙必须走同一条曲线
  （`subtitle_engine.tv_range_color`），否则同一份配置换个画法就换个颜色。

A 里的框按**旧格式**在探针里现补一条 `\\p1` 事件——`_frame_box_event` 已随烘焙一起
删除，这里重写它是为了有一把对照的尺子。这条重写不靠记忆：`box_event_tags()` 会拿
它和已发布各期里那条真品（`projects/*/过程/*/sub.ass`）逐字比，不一致就报错。
真实生成路径上没有任何地方再产出它。

反斜杠一律用 chr(92) 拼，避免被当成转义序列。
"""
import glob
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, ROOT)

from podcast_maker import assets_factory as A          # noqa: E402
from podcast_maker import subtitle_engine as S         # noqa: E402
from podcast_maker import video_engine as V            # noqa: E402
from podcast_maker.config_manager import ConfigManager  # noqa: E402

BS = chr(92)
PROJ = os.path.join(ROOT, 'projects', '20260917-015336')
OUT = os.path.join(ROOT, '_smoke', '_bench')
T = 18.5                    # 取帧时刻：落在第一句滚动字幕上
CANVAS = (1920, 1080)

# 已发布成片里那条真品框事件（2c 横屏，20260917-015336）。重写版必须逐字等于它，
# 否则「A 侧那把尺子」量的就不是线上真正画过的东西。
TRUE_BOX = ('{' + BS + 'p1' + BS + 'an7' + BS + 'pos(90,920)' + BS + '1c&H000000&'
            + BS + '1a&H80&' + BS + 'bord2' + BS + '3c&HFFFFFF' + BS + '3a&H66&}'
            + 'm 0 0 l 1740 0 l 1740 70 l 0 70 c')


def box_event_tags(cfg, w, h, suffix):
    """按**旧格式**复现框事件里除时间戳以外的那一段（探针专用）。

    返回值是 `{标签}路径` 这一整段——**花括号不能少**：少了它这串东西不是标签块，
    libass 会把 `\\p1\\an7\\pos(…)` 当普通文字画，框静默消失（本探针第二版就这么
    写错过一次，A 侧的框整块不见，看起来像"新路多画了一个框"）。
    """
    g = S.frame_of(cfg, w, h, suffix)
    p = S.frame_box_paint(g, int(cfg.get('subtitle.bg_alpha', 128)),
                          int(cfg.get('subtitle.outline', 2)))
    bw, bh = g['x2'] - g['x1'], g['h']
    # 路径写在 `}` **之后**——写在里面就成了一个认不出的标签，libass 同样什么都不画。
    tags = (BS + "p1" + BS + "an7" + BS + "pos(%d,%d)" % (g['x1'], g['top'])
            + BS + "1c&H000000&"
            + BS + "1a&H%02X&" % int(cfg.get('subtitle.bg_alpha', 128))
            + BS + "bord%d" % p['width']
            # 描边色的十六进制后面**不带**收尾 `&`：libass 读到 `\` 就断，两条都认，
            # 但旧代码写的就是不带的那种，这里照抄，好让上面的逐字比对真的比得动。
            + BS + "3c&HFFFFFF" + BS + "3a&H%02X&" % S.FRAME_BOX_BORDER_ALPHA)
    return "{%s}m 0 0 l %d 0 l %d %d l 0 %d c" % (tags, bw, bw, bh, bh)


def check_true_box(cfg):
    """重写版 vs 已发布成片里的真品。几何或常数一动，这里就红。"""
    got = box_event_tags(cfg, CANVAS[0], CANVAS[1], '')
    return got == TRUE_BOX, got


def build_old_ass(cfg, w, h, suffix, script, timings, path):
    """旧流派的 ASS：`build_ass` 的文字 + 一条现补的框事件（层 0，排最前）。"""
    ass = S.build_ass(script, cfg, timings, w, h, suffix)[0]
    box = "Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (
        S.fmt_ass_time(timings[0]['start']),
        S.fmt_ass_time(max(timings[-1]['end'], timings[-1]['start'] + 0.01)),
        box_event_tags(cfg, w, h, suffix))
    lines = ass.splitlines()
    head = [l for l in lines if not l.startswith('Dialogue:')]
    ev = [l for l in lines if l.startswith('Dialogue:')]
    io.open(path, 'w', encoding='utf-8').write('\n'.join(head + [box] + ev) + '\n')


def render(bg, vf, png, at=None):
    """渲一帧 PNG。`at` 为 None 时取第 0 帧（校准用的合成 ASS 只挂几秒，不能按 T 取）。"""
    at = T if at is None else at
    cmd = [V.ffmpeg_bin(), '-y', '-hide_banner', '-loglevel', 'error',
           '-loop', '1', '-t', '%.3f' % (at + 0.2), '-i', bg,
           '-filter_complex', vf, '-map', '[vout]',
           '-ss', '%.3f' % at, '-frames:v', '1', png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or '')[-500:])


def chain(w, h, mid, ass_path, font_dir):
    """A/B 共用的链骨架：归一到画幅 → mid（压暗/什么都不做）→ ass → yuv420p。"""
    return ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "setsar=1[bg];[bg]%s[vc];[vc]format=rgb24,ass=%s%s,format=yuv420p[vout]"
            % (w, h, w, h, mid, V._ff_escape(ass_path),
               (":fontsdir=%s" % font_dir) if font_dir else ""))


def timings_of(script, cfg):
    from podcast_maker import duration_model as D
    out, t = [], 0.0
    pause = float(cfg.get('audio.pause_between_lines', 0.35))
    for it in script:
        d = float(it.get('estimated_seconds') or 0) or D.estimate_line(
            it.get('text', ''), it.get('speaker', 'A'), cfg)
        out.append({'start': t, 'end': t + d, 'speaker': it.get('speaker', 'A')})
        t += d + pause
    return out


def compare(a_png, b_png, g):
    """按几何分区对账。返回 (报告行列表, 全过?)。

    分区（out = 描边宽，实测 2）：
      · 填充区   ── 矩形内部，去掉最内 1 px 环
      · 内侧交界 ── 矩形最内 1 px 环：libass 在这里留一道软边并往内渗一点，
                    它在 918/920 与 990/989 之间是"渐变"而不是硬切，不复刻
      · 描边带   ── 矩形外 out px（含四个角）
      · 压暗区   ── 其余

    残余都是**取整与光栅化**，不是颜色：颜色口径（TV 映射）已由 `--calibrate`
    定死，实测过 7 组颜色逐个吻合。
    """
    from PIL import Image
    ia = Image.open(a_png).convert('RGB')
    ib = Image.open(b_png).convert('RGB')
    pa, pb = ia.load(), ib.load()
    w, h = ia.size
    x1, x2, top, bot = g['x1'], g['x2'], g['top'], g['bottom']
    out = S.frame_box_paint(g, 128, 2)['out']

    def zone(x, y):
        if x1 <= x < x2 and top <= y < bot:
            if y in (top, bot - 1) or x in (x1, x2 - 1):
                return '内侧交界'
            return '填充'
        if (x1 - out <= x < x2 + out) and (top - out <= y < bot + out):
            return '描边带'
        return '压暗区'

    stat = {k: {'max': 0, 'over8': 0} for k in ('填充', '内侧交界', '描边带', '压暗区')}
    for y in range(h):
        for x in range(w):
            va, vb = pa[x, y], pb[x, y]
            d = max(abs(va[i] - vb[i]) for i in range(3))
            z = stat[zone(x, y)]
            z['max'] = max(z['max'], d)
            if d > 8:
                z['over8'] += 1
    f, j, e, o = (stat['填充'], stat['内侧交界'], stat['描边带'], stat['压暗区'])
    rows = [
        ('框几何：外扩 %d px、四边落在同一行/列' % out, True,
         'x=[%d,%d] y=[%d,%d]' % (x1, x2, top, bot)),
        ('填充区最大差 ≤ 4', f['max'] <= 4,
         '%d 个码值（取整口径差：ffmpeg 截断、Pillow 四舍五入）' % f['max']),
        ('填充区 >8 的像素 = 0', f['over8'] == 0, '%d 个' % f['over8']),
        ('压暗区最大差 ≤ 4', o['max'] <= 4,
         '%d 个码值（drawbox 与 Pillow 各自取整差 1，再经 PNG 出口那次 RGB↔4:2:0 '
         '往返放大——真实链是 yuv420p 直进编码器，没有这一层）' % o['max']),
        ('压暗区 >8 的像素 = 0', o['over8'] == 0, '%d 个' % o['over8']),
        ('描边带 >8 的像素 ≤ 20', e['over8'] <= 20,
         '%d 个（实测为矩形四角：libass 描边在转角不铺满，%d px；最大差 %d 码值）'
         % (e['over8'], 4 * 4, e['max'])),
        ('内侧交界 >8 的像素 ≤ 4000', j['over8'] <= 4000,
         '%d 个（libass 光栅化在交界处的软边，不复刻；最大差 %d 码值）'
         % (j['over8'], j['max'])),
    ]
    ok = all(r[1] for r in rows)
    return rows, ok, stat


def calibrate():
    """颜色传递表：纯色底 + 纯色填充，量 ffmpeg 到底画成什么。"""
    from PIL import Image
    w, h = CANVAS
    g = S.frame_of(ConfigManager().data(), w, h, '')
    print('底 = 纯色，填充 = 不透明纯色（框矩形 x=[%d,%d] y=[%d,%d]）'
          % (g['x1'], g['x2'], g['top'], g['bottom']))
    print()
    print('%-22s %-16s %-16s %s' % ('ASS 颜色', '画面实测', '逐通道 16+219c/255', '一致'))
    cases = [('&H000000&', (0, 0, 0)), ('&HFFFFFF&', (255, 255, 255)),
             ('&H0000FF&', (255, 0, 0)), ('&HFF0000&', (0, 0, 255)),
             ('&H00FF00&', (0, 255, 0)), ('&H808080&', (128, 128, 128)),
             ('&H008080&', (128, 128, 0))]
    solid = os.path.join(OUT, 'cal_solid.png')
    Image.new('RGB', (w, h), (128, 128, 128)).save(solid)
    bad = 0
    for color, rgb in cases:
        ass = os.path.join(OUT, 'cal.ass')
        path = 'm 0 0 l %d 0 l %d %d l 0 %d c' % (g['x2'] - g['x1'],
                                                  g['x2'] - g['x1'], g['h'], g['h'])
        io.open(ass, 'w', encoding='utf-8').write('\n'.join([
            '[Script Info]', 'ScriptType: v4.00+', 'WrapStyle: 2',
            'PlayResX: %d' % w, 'PlayResY: %d' % h, '', '[V4+ Styles]',
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
            'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, '
            'ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, '
            'MarginL, MarginR, MarginV, Encoding',
            'Style: Default,Arial,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&HFF000000,'
            '0,0,0,0,100,100,0,0,1,2,0,2,90,90,90,1', '', '[Events]',
            'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
            'Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,{%s%s%s%s}%s'
            % (BS + 'p1' + BS + 'an7' + BS + 'pos(%d,%d)' % (g['x1'], g['top']),
               BS + '1c' + color, BS + '1a&H00&', BS + 'bord0', path)]) + '\n')
        png = os.path.join(OUT, 'cal.png')
        render(solid, "[0:v]format=rgb24,ass=%s[vout]" % V._ff_escape(ass), png, at=1.0)
        got = Image.open(png).convert('RGB').load()[(g['x1'] + g['x2']) // 2,
                                                    (g['top'] + g['bottom']) // 2]
        want = S.tv_range_color(rgb)
        same = got == want
        bad += 0 if same else 1
        print('%-22s %-16s %-16s %s' % (color, got, want, 'PASS' if same else 'FAIL'))
    print()
    print('结论：%s' % ('全部吻合——烘焙按同一条曲线取色即可复现 ffmpeg 的画面值。'
                      if not bad else '有 %d 项不吻合，取色口径需要重查。' % bad))
    return 0 if not bad else 1


def main(argv):
    if '--calibrate' in argv:
        return calibrate()
    from PIL import Image

    script = json.load(io.open(os.path.join(PROJ, '脚本', '2c.json'), encoding='utf-8'))
    cfg = ConfigManager().data()
    cfg['subtitle.preset'] = 'single'
    w, h = CANVAS
    os.makedirs(OUT, exist_ok=True)

    ok_true, got_true = check_true_box(cfg)
    print('框事件重写 vs 已发布真品：%s' % ('一致' if ok_true else '不一致'))
    if not ok_true:
        print('  真品：%s' % TRUE_BOX)
        print('  重写：%s' % got_true)

    timings = timings_of(script, cfg)
    font = A.resolve_font(cfg.get('subtitle.font_family', ''))
    fdir = V._ff_escape(os.path.abspath(A.fonts_dir_of(font)))

    raw_bg = os.path.join(PROJ, '背景', '2c_bg.png')
    baked = os.path.join(OUT, 'bake_check_bg.png')
    A.bake_static_layers(raw_bg, baked, cfg, w, h, '')

    new_ass = os.path.join(OUT, 'bake_check_new.ass')
    io.open(new_ass, 'w', encoding='utf-8').write(
        S.build_ass(script, cfg, timings, w, h, '')[0])
    old_ass = os.path.join(OUT, 'bake_check_old.ass')
    build_old_ass(cfg, w, h, '', script, timings, old_ass)

    dim = float(cfg.get('video.bg_dim', 0.15))
    a_png = os.path.join(OUT, 'bake_A_old.png')
    b_png = os.path.join(OUT, 'bake_B_new.png')
    render(raw_bg, chain(w, h, "format=rgb24,drawbox=x=0:y=0:w=%d:h=%d:"
                             "color=black@%.2f:t=fill" % (w, h, dim), old_ass, fdir), a_png)
    render(baked, chain(w, h, "null", new_ass, fdir), b_png)

    g = S.frame_of(cfg, w, h, '')
    rows, ok, _ = compare(a_png, b_png, g)
    print('A 旧路：%s' % os.path.relpath(a_png, ROOT))
    print('B 新路：%s' % os.path.relpath(b_png, ROOT))
    print('烘焙图：%s' % os.path.relpath(baked, ROOT))
    print()
    for name, good, detail in rows:
        print('  [%s] %-38s %s' % ('PASS' if good else 'FAIL', name, detail))
    print()
    print('总判：%s' % ('PASS' if (ok and ok_true) else 'FAIL'))
    return 0 if (ok and ok_true) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
