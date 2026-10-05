# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
# SPDX-License-Identifier: Apache-2.0
"""横竖两版并行值不值：横屏一条、竖屏一条，串行跑 vs 并发跑。

这一条是从另一组实测推出来的：**单条渲染只吃掉 2.9 个核**（24 核里），全链的墙钟
由 libass 与编码器里的串行段决定，CPU 大把空着。既然一条只用 3 个核，那同时跑两条
横竖就该互不打扰、总时长约等于**一条**——省下一半；而分段并行（把同一条切成 N 段）
量下来反而更慢，因为它切开的正是那些串行段，还把每帧 CPU 成本抬到 6.6 倍。

判据：并发墙钟 ≤ 串行的 0.7 倍（1.43x 提速）才算划算；同时两条的核当量合计不能
顶满设备核数（顶满就说明是在抢核、那点收益是虚的）。

环境变量 `PAR_HV_SECONDS`（默认 180）改窗口长度。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, ROOT)

from podcast_maker import assets_factory as A      # noqa: E402
from podcast_maker import subtitle_engine as S     # noqa: E402
from podcast_maker import video_engine as V        # noqa: E402
from podcast_maker.config_manager import ConfigManager  # noqa: E402

PROJ = os.path.join(ROOT, 'projects', '20260917-015336')
OUT = os.path.join(tempfile.gettempdir(), 'pm_hv_bench')
SEC = float(os.environ.get('PAR_HV_SECONDS', '180'))
W, H = 1920, 1080
V.shutil.rmtree = lambda *a, **k: None


def prepare():
    os.makedirs(OUT, exist_ok=True)
    script = json.load(io.open(os.path.join(PROJ, '脚本', '2c.json'), encoding='utf-8'))
    cfg = dict(ConfigManager().data())
    cfg['subtitle.preset'] = 'single'
    cfg['animation.mode'] = 'static'
    cfg['video.fps'] = 30
    from podcast_maker import duration_model as D
    timings, t = [], 0.0
    pause = float(cfg.get('audio.pause_between_lines', 0.35))
    for it in script:
        d = float(it.get('estimated_seconds') or 0) or D.estimate_line(
            it.get('text', ''), it.get('speaker', 'A'), cfg)
        timings.append({'start': t, 'end': t + d, 'speaker': it.get('speaker', 'A')})
        t += d + pause

    font = A.resolve_font(cfg.get('subtitle.font_family', ''))
    font_dir = A.fonts_dir_of(font)
    sides = {}
    for suffix, (w, h) in (('', (W, H)), ('_v', (H, W))):
        tag = suffix or '_h'
        ass = os.path.join(OUT, 'sub%s.ass' % tag)
        io.open(ass, 'w', encoding='utf-8').write(
            S.build_ass(script, cfg, timings, w, h, suffix)[0])
        V.render_ass(ass, ass, script, timings, cfg, w, h, suffix)
        src = os.path.join(OUT, 'raw%s.png' % tag)
        if not os.path.exists(src):
            from PIL import Image
            Image.open(os.path.join(PROJ, '背景', '2c_bg.png')).convert('RGB') \
                 .resize((w, h)).save(src)
        bg = A.bake_static_layers(src, os.path.join(OUT, 'bg%s.png' % tag),
                                  cfg, w, h, suffix)
        sides[suffix] = (ass, bg, w, h)
    return (cfg, sides, os.path.join(PROJ, '音视频', '2c.mp3'), timings, font_dir)


def one(ctx, suffix, out):
    cfg, sides, audio, timings, font_dir = ctx
    ass, bg, w, h = sides[suffix]
    t0 = time.time()
    V._compose_single(audio, ass, os.path.join(OUT, out), cfg, bg, SEC, w, h,
                      suffix, font_dir=font_dir, timings=timings, log=lambda m: None)
    return time.time() - t0


def main():
    ctx = prepare()
    cfg = ctx[0]
    print('设备逻辑核 %d；窗口 %.0f 秒；横 1920×1080 / 竖 1080×1920；preset %s'
          % (os.cpu_count() or 1, SEC, cfg.get('video.encoder_preset')))
    print()

    th = one(ctx, '', 'hv_h.mp4')
    tv = one(ctx, '_v', 'hv_v.mp4')
    seq = th + tv
    print('   串行：横 %.1fs + 竖 %.1fs = %.1fs' % (th, tv, seq))

    import concurrent.futures as cf
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        fh = pool.submit(one, ctx, '', 'hv_h2.mp4')
        fv = pool.submit(one, ctx, '_v', 'hv_v2.mp4')
        ph, pv = fh.result(), fv.result()
    par = time.time() - t0
    print('   并发：横 %.1fs ∥ 竖 %.1fs → 墙钟 %.1fs' % (ph, pv, par))
    print()
    print('   提速 %.2fx（并发/串行 %.2f）' % (seq / par if par else 0, par / seq if seq else 0))
    ok = par <= seq * 0.7
    print('   [%s] 判据：并发墙钟 ≤ 串行的 70%%（2 条各吃约 3 核、24 核装得下）'
          % ('PASS' if ok else 'FAIL'))
    for f in ('hv_h2.mp4', 'hv_v2.mp4'):
        p = os.path.join(OUT, f)
        if os.path.exists(p):
            st = V.probe_streams(p)
            print('   %s：%s / %s 帧' % (f, st['video']['duration'],
                                        V.ffprobe_nb_frames(p)))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
