# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
# SPDX-License-Identifier: Apache-2.0
"""出片耗时到底花在哪：编码线程 / libass 字幕 / 并发路数，三个变量分开量。

起因：分段并行在 22.7 分钟真素材上是 **0.88x（反而更慢）**。单路能跑到 260 帧/秒，
说明瓶颈不在"核不够"。这个探针把三个候选逐个隔离：

  ① `串行 · 不给 -threads`（老行为，ffmpeg auto）——基线。
  ② `串行 · -threads 1`——编码器单线程。②与①同速，就说明墙不在编码器。
  ③ `串行 · 空字幕`（ASS 只有头、没有一条 Dialogue）——①与③之差就是 libass 的价钱。
     `ass` 滤镜是**单线程**的，单路渲到底时整条链的吞吐由它一个人定。
  ④ `N 路 · 每路 threads=核数/N`——把核按路数分掉再并发。这是唯一公平的并发量法：
     不分核的 6 路是 6×24 个线程挤 24 个核，量出来的不是并发的上限。

环境变量 `PAR_SCAN_SECONDS`（默认 300）改窗口长度。
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
OUT = os.path.join(tempfile.gettempdir(), 'pm_scale_scan')
SEC = float(os.environ.get('PAR_SCAN_SECONDS', '300'))
W, H = 1920, 1080
CORES = os.cpu_count() or 1

V.shutil.rmtree = lambda *a, **k: None     # 同 parallel_render_check：别撞宿主的大批量删除闸


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

    ass = os.path.join(OUT, 'sub.ass')
    io.open(ass, 'w', encoding='utf-8').write(
        S.build_ass(script, cfg, timings, W, H, '')[0])
    V.render_ass(ass, ass, script, timings, cfg, W, H, '')
    # 空字幕：只留头，一条 Dialogue 都不放
    heads = io.open(ass, encoding='utf-8').read().splitlines()
    bare = os.path.join(OUT, 'bare.ass')
    io.open(bare, 'w', encoding='utf-8').write(
        '\n'.join(l for l in heads if not l.startswith('Dialogue:')) + '\n')
    bg = A.bake_static_layers(os.path.join(PROJ, '背景', '2c_bg.png'),
                              os.path.join(OUT, 'bg.png'), cfg, W, H, '')
    font = A.resolve_font(cfg.get('subtitle.font_family', ''))
    return (cfg, ass, bare, bg, os.path.join(PROJ, '音视频', '2c.mp3'), timings,
            A.fonts_dir_of(font))


def stage_breakdown(ctx, workers):
    """把 `_compose_segmented` 里每一条 ffmpeg 的真实耗时逐个打出来。

    不重写生产的命令拼装（重写就等于量了一把会飘的尺子），而是给 `V._run` 套一个计时
    壳——`_run_parallel` 也是在调用时从模块里取 `_run`，所以壳会照样套上。于是能分清：
    「某一段特别慢」还是「并发本身在互相咬」，以及拼接/合流各占多少。
    """
    cfg, ass, _bare, bg, audio, timings, font_dir = ctx
    rec = []
    orig = V._run

    def timed(cmd, cwd=None):
        """真跑这条命令，顺带把 `-benchmark` 的 utime/stime 拿回来。

        核当量 = (utime+stime)/墙钟 —— 这一条 ffmpeg 实际吃掉几个核。用它分清
        「进程在抢核」和「进程在等」：前者核当量顶满，后者墙钟涨而核当量不动。
        """
        c = [cmd[0], '-benchmark', '-nostats', '-loglevel', 'info'] + list(cmd[1:])
        t0 = time.time()
        r = subprocess.run(c, capture_output=True, cwd=cwd)
        wall = time.time() - t0
        if r.returncode:
            raise RuntimeError((r.stderr or b'').decode('utf-8', 'replace')[-400:])
        cpu = 0.0
        for line in (r.stderr or b'').decode('utf-8', 'replace').splitlines():
            if line.startswith('bench:'):
                for kv in line.split()[1:]:
                    k, _, v = kv.partition('=')
                    if k in ('utime', 'stime'):
                        cpu += float(v.rstrip('s'))
        rec.append((wall, cmd, cpu))
        return None

    V._run = timed
    out = os.path.join(OUT, 'stages.mp4')
    if os.environ.get('PAR_SEQ'):
        # 把并发换成顺序：每段单独跑（机器上只有它一个）。与自己并发时的耗时对比，
        # 就能分清「这一段本身贵」还是「六个一起跑互相咬」。
        real = V._run_parallel
        V._run_parallel = lambda jobs, cwd, w: [V._run(cmd, cwd) for _s, cmd in jobs]
    t0 = time.time()
    try:
        V._compose_segmented(audio, ass, out, cfg, bg, SEC, W, H, '',
                             font_dir=font_dir, timings=timings,
                             log=lambda m: None, workers=workers)
    finally:
        V._run = orig
        if os.environ.get('PAR_SEQ'):
            V._run_parallel = real
    wall = time.time() - t0

    print('分段 %d 路（%s）的每一条 ffmpeg（总墙钟 %.1fs）：'
          % (workers, '顺序跑' if os.environ.get('PAR_SEQ') else '并发跑', wall))
    spans = V.cut_points(SEC, 30, workers, timings)
    for t, cmd, _cwd in rec:
        kind = '段' if '_seg' in ' '.join(cmd) and 'concat' not in cmd else '其他'
        name = [a for a in cmd if a.endswith('.mp4') or a.endswith('.txt')]
        seg_i = ''
        for i, sp in enumerate(spans):
            if name and name[-1].startswith('seg%02d' % i):
                seg_i = '（窗 %.0f–%.0f，输入要生成 %.0f 帧）' % (sp[0], sp[1],
                                                              sp[1] * 30)
        print('   %5.1fs  %s  %s %s'
              % (t, kind, os.path.basename(name[-1]) if name else '?', seg_i))
    seg = [t for t, c, _ in rec if any(a.endswith('.mp4') and 'seg' in a for a in c)]
    other = [t for t, c, _ in rec if not any(a.endswith('.mp4') and 'seg' in a for a in c)]
    cores = [(t, cpu / t if t else 0.0) for t, _c, cpu in rec]
    print('   分段合计核当量 %.1f 个（单条最大 %.1f 个；设备 %d 核）'
          % (sum(c for t, c in cores[:len(seg)]), max([c for t, c in cores[:len(seg)]] or [0]),
             os.cpu_count() or 1))
    ref = stage_ref(ctx)
    print('   段合计 %.1fs（若完全并发，墙钟应接近 %.1fs）；并行后总墙钟 %.1fs'
          % (sum(seg), max(seg) if seg else 0, wall))
    print('   拼接/合流等 %.1fs，占墙钟 %.0f%%'
          % (sum(other), 100.0 * sum(other) / wall if wall else 0))
    print('   同窗口串行基线 %.1fs（%.1f 帧/秒）—— 分段 %.1f 帧/秒，%s'
          % (ref, SEC * 30 / ref, SEC * 30 / wall, '更快' if wall < ref else '更慢'))
    return 0


def stage_ref(ctx):
    cfg, ass, _bare, bg, audio, timings, font_dir = ctx
    orig = V.encoder_threads
    V.encoder_threads = lambda w, cpu=None: 0
    t0 = time.time()
    try:
        V._compose_single(audio, ass, os.path.join(OUT, 'ref.mp4'), cfg, bg, SEC,
                          W, H, '', font_dir=font_dir, timings=timings,
                          log=lambda m: None)
    finally:
        V.encoder_threads = orig
    return time.time() - t0


def run(ctx, out, workers, threads, filt_threads=None, ass_path=None):
    """跑一种口径。`threads=None` → 不给 `-threads`（ffmpeg auto，老行为）。

    `filt_threads` 是 `-filter_complex_threads`：**这一项我第一轮漏了**。只卡编码器
    线程是不够的——滤镜图默认也按整机核数开线程，6 路就是 6×24 个滤镜线程挤 24 个核。
    """
    cfg, ass, bare, bg, audio, timings, font_dir = ctx
    orig_threads, orig_args = V.encoder_threads, V._x264_args
    V.encoder_threads = lambda w, cpu=None: threads or 0
    if filt_threads:
        V._x264_args = (lambda c, t=None:
                        orig_args(c, t) + ['-filter_complex_threads', str(filt_threads)])
    p = os.path.join(OUT, out)
    t0 = time.time()
    try:
        if workers <= 1:
            V._compose_single(audio, ass_path or ass, p, cfg, bg, SEC, W, H, '',
                              font_dir=font_dir, timings=timings, log=lambda m: None)
        else:
            V._compose_segmented(audio, ass_path or ass, p, cfg, bg, SEC, W, H, '',
                                 font_dir=font_dir, timings=timings,
                                 log=lambda m: None, workers=workers)
    finally:
        V.encoder_threads, V._x264_args = orig_threads, orig_args
    return time.time() - t0


def main():
    ctx = prepare()
    cfg, ass, bare, _bg, audio, timings, _fd = ctx
    fps = int(cfg.get('video.fps', 30))
    frames = int(SEC * fps)
    if os.environ.get('PAR_STAGES'):
        print('设备逻辑核 %d；窗口 %.0f 秒（%d 帧）1920×1080'
              % (CORES, SEC, frames))
        return stage_breakdown(ctx, V.render_workers())
    print('设备逻辑核 %d；窗口 %.0f 秒（%d 帧）1920×1080；preset %s / crf %s'
          % (CORES, SEC, frames, cfg.get('video.encoder_preset'), cfg.get('video.crf')))
    print()

    rows = []
    cases = [
        ('串行 · 不给 -threads（基线）', 1, None, None, None),
        ('串行 · 空字幕（价出 libass）', 1, None, None, bare),
        ('2 路 · 编码 12 + 滤镜 2', 2, 12, 2, None),
        ('4 路 · 编码 6 + 滤镜 1', 4, 6, 1, None),
        ('6 路 · 编码 4 + 滤镜 1', 6, 4, 1, None),
        ('6 路 · 编码 3 + 滤镜 1', 6, 3, 1, None),
        ('12 路 · 编码 2 + 滤镜 1', 12, 2, 1, None),
    ]

    base = None
    for label, workers, threads, ft, use_ass in cases:
        t = run(ctx, 'r_%s.mp4' % label.replace(' ', '').replace('·', ''), workers,
                threads, ft, use_ass)
        if base is None:
            base = t
        rows.append((label, t, frames / t, t / base))
        print('   %-30s %7.1fs   %6.1f 帧/秒   %.2fx' % (label, t, frames / t, t / base))
    print()
    d = {r[0]: r[1] for r in rows}
    b = '串行 · 不给 -threads（基线）'
    print('读法（相对基线的耗时比，<1 才是更快）：')
    print('  编码器是不是墙：%s（-threads 1 那笔上一轮量过：慢 3.98x）'
          % ('是' if d['6 路 · 编码 4 + 滤镜 1'] > d[b] else '否'))
    print('  libass 占了多少：拿掉全部 Dialogue 后耗时剩 %.0f%%'
          % (d['串行 · 空字幕（价出 libass）'] / d[b] * 100))
    for label, t, fps, rel in rows[2:]:
        print('  %-28s %s 基线（%.2fx）'
              % (label, '快于' if t < d[b] else '慢于', rel))
    best = min(rows, key=lambda r: r[1])
    print('  最快的一项：%s（%.1fs，%.2fx）' % (best[0], best[1], best[3]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
