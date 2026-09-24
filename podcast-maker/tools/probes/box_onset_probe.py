# -*- coding: utf-8 -*-
"""字幕框「出现时刻」的代价探针（临时 scratch，主人拍板后再决定归位）。

背景：静态层烘焙把框画进了背景图，于是框从**第 0 帧**就在；旧链上框是与第一条文字
事件（2.72s）同时出现的。要还原旧行为，得让「有框/无框」按时间切换。本探针把几种
切法放在同一条链、同一份素材上量墙钟，挑最便宜的那个：

  now    现状 —— 单输入（有框图）+ 全链
  concat 双输入 concat 滤镜（无框图 N0 帧 + 有框图其余）
  cfirst concat 放在 scale/crop **之前**（只留一份几何）
  cropov 双输入，只把**框那块**从有框图裁出来，overlay + enable 按时开关
  rawbox 单输入无框图（下限参照：完全不画框）

素材：真实 2b（1080p / 30fps / medium / crf20）。N0 由第一条文字事件换算。
"""
import math
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.join(ROOT, '_smoke')          # 产物锚点（见 tools/probes/README.md）
sys.path.insert(0, ROOT)

from podcast_maker import subtitle_engine as S        # noqa: E402
from podcast_maker import video_engine as V           # noqa: E402

PROJ = os.path.join(ROOT, 'projects', '20260917-015336')
RAW = os.path.join(PROJ, '背景', '2b_bg.png')          # 无框（assets 原图）
FLAT = os.path.join(PROJ, '过程', '2b', 'bg_h.png')    # 有框（烘焙产物）
ASS = os.path.join(PROJ, '过程', '2b', 'sub.ass')
WAV = os.path.join(PROJ, '过程', '2b', 'voice_bgm.wav')
OUT = os.path.join(HERE, '_bench')

W, H, FPS = 1920, 1080, 30
DUR = 120.0
T0 = 2.72
N0 = int(round(T0 * FPS))
T0F = N0 / float(FPS)

GEOM = "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1" % (
    W, H, W, H)
ASS_ARG = "ass=%s" % V._ff_escape(os.path.abspath(ASS))


def box_rect(cfg=None):
    """框外扩后的矩形（含描边）。烘焙用的是 `frame_box_paint` 的 out。"""
    from podcast_maker.config_manager import ConfigManager
    cfg = cfg or ConfigManager(ROOT)
    try:
        cfg.load()
    except Exception:
        pass
    g = S.frame_of(cfg, W, H, '')
    p = S.frame_box_paint(g, int(cfg.get('subtitle.bg_alpha', 128)),
                          int(cfg.get('subtitle.outline', 2)))
    bx, by = g['x1'] - p['out'], g['top'] - p['out']
    bw, bh = (g['x2'] - g['x1']) + 2 * p['out'], g['h'] + 2 * p['out']
    return bx, by, bw, bh


def tail(label):
    return "[%s]null[vbase];[vbase]format=rgb24,%s,format=yuv420p[vout]" % (
        label, ASS_ARG)


def base_args(out, audio_idx):
    return ([V.ffmpeg_bin(), '-y'],
            ['-map', '[vout]', '-map', '%d:a' % audio_idx,
             '-t', '%.6f' % DUR, '-r', str(FPS),
             '-c:v', 'libx264', '-preset', 'medium',
             '-crf', '20', '-c:a', 'aac', '-b:a', '192k',
             os.path.basename(out)])


def build(mode, out):
    """返回 (输入参数, 滤镜图, 音频输入下标)。"""
    loop = lambda p, t: ['-framerate', str(FPS), '-loop', '1', '-t', '%.6f' % t,
                         '-i', p]
    if mode == 'now':                       # 单输入：有框图
        return (loop(FLAT, DUR), "[0:v]%s[bg];%s" % (GEOM, tail('bg')), 1)
    if mode == 'rawbox':                    # 单输入：无框图（下限参照）
        return (loop(RAW, DUR), "[0:v]%s[bg];%s" % (GEOM, tail('bg')), 1)
    if mode == 'concat':                    # 双输入 concat（各自先过几何）
        g = ("[0:v]%s,setpts=PTS-STARTPTS[ra];[1:v]%s,setpts=PTS-STARTPTS[fb];"
             "[ra][fb]concat=n=2:v=1:a=0[bg];%s" % (GEOM, GEOM, tail('bg')))
        return (loop(RAW, T0F) + loop(FLAT, DUR - T0F), g, 2)
    if mode == 'cfirst':                    # 双输入 concat 放在几何之前
        g = ("[0:v]setpts=PTS-STARTPTS[ra];[1:v]setpts=PTS-STARTPTS[fb];"
             "[ra][fb]concat=n=2:v=1:a=0[cc];[cc]%s[bg];%s" % (GEOM, tail('bg')))
        return (loop(RAW, T0F) + loop(FLAT, DUR - T0F), g, 2)
    if mode == 'cropov':                    # 双输入，只叠框那一块 + enable
        bx, by, bw, bh = box_rect()
        g = ("[0:v]%s[base];[1:v]crop=%d:%d:%d:%d[box];"
             "[base][box]overlay=x=%d:y=%d:enable='gte(t,%.2f)'[bg];%s"
             % (GEOM, bw, bh, bx, by, bx, by, T0F, tail('bg')))
        return (loop(RAW, DUR) + loop(FLAT, DUR), g, 2)
    raise SystemExit('未知 mode: %s' % mode)


def run(mode, out, n=3, quiet=False):
    ins, graph, aidx = build(mode, out)
    head, tailargs = base_args(out, aidx)
    cmd = head + ins + ['-i', WAV, '-filter_complex', graph] + tailargs
    best, fails = None, 0
    for _ in range(n):
        t = time.time()
        r = subprocess.run(cmd, cwd=OUT, capture_output=True, text=True)
        d = time.time() - t
        if r.returncode != 0:
            fails += 1
            if not quiet:
                print('  %-7s FAILED：%s' % (mode, (r.stderr or '')[-400:]))
            continue
        best = d if best is None else min(best, d)
    return best


def psnr(a, b):
    from PIL import Image
    ia, ib = Image.open(a).convert('RGB'), Image.open(b).convert('RGB')
    if ia.size != ib.size:
        return None
    pa, pb = ia.tobytes(), ib.tobytes()
    mse = sum((x - y) ** 2 for x, y in zip(pa, pb)) / float(len(pa))
    return float('inf') if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)


def frame(path, at, out):
    subprocess.run([V.ffmpeg_bin(), '-y', '-ss', '%.3f' % at, '-i', path,
                    '-frames:v', '1', out], capture_output=True)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    for p in (RAW, FLAT, ASS, WAV):
        if not os.path.exists(p):
            print('缺素材：%s' % p)
            return 1
    print('素材 2b  dur=%.1fs  T0=%.2fs → %d 帧  框矩形 %s'
          % (DUR, T0, N0, box_rect()))

    modes = ['now', 'concat']
    rounds = 4
    res = {}
    print('\n[计时] 交错跑 %d 轮（这台机器上顺序跑会有温升/负载漂移，交错才比得动）'
          % rounds)
    for _ in range(rounds):
        for m in modes:
            d = run(m, os.path.join(OUT, 'onset_%s.mp4' % m), n=1, quiet=True)
            if d:
                res.setdefault(m, []).append(d)
    for m in modes:
        v = sorted(res.get(m) or [])
        if v:
            print('  %-7s 最小 %6.2fs ／ 中位 %6.2fs ／ 全部 %s'
                  % (m, v[0], v[len(v) // 2], ' '.join('%.2f' % x for x in v)))
    base, cand = (res.get('now') or [None])[0], (res.get('concat') or [None])[0]
    if base and cand:
        print('  → 候选相对现状：最小 %.3fx ／ 中位 %.3fx'
              % (min(res['now']) / min(res['concat']),
                 sorted(res['now'])[len(res['now']) // 2]
                 / sorted(res['concat'])[len(res['concat']) // 2]))

    print('\n[对账] 候选在 T0 前应"无框"、T0 后应与现状一致')
    now_mp4 = os.path.join(OUT, 'onset_now.mp4')
    raw_mp4 = os.path.join(OUT, 'onset_rawbox.mp4')
    now1 = frame(now_mp4, 1.0, os.path.join(OUT, 'x_now_1.png'))
    now4 = frame(now_mp4, 4.0, os.path.join(OUT, 'x_now_4.png'))
    raw1 = frame(raw_mp4, 1.0, os.path.join(OUT, 'x_raw_1.png'))
    for m in ('concat', 'cfirst'):
        p = os.path.join(OUT, 'onset_%s.mp4' % m)
        if not os.path.exists(p):
            continue
        f1 = psnr(frame(p, 1.0, os.path.join(OUT, 'x_%s_1.png' % m)), raw1)
        f2 = psnr(frame(p, 4.0, os.path.join(OUT, 'x_%s_4.png' % m)), now4)
        print('  %-7s t=1.0s vs 无框 PSNR=%s ／ t=4.0s vs 现状 PSNR=%s'
              % (m, fmt(f1), fmt(f2)))
    print('  现状本身 t=1.0s vs 无框 PSNR=%s（这就是"框的可见代价"）'
          % fmt(psnr(now1, raw1)))
    print('\n[结构] 帧数 %s' % {m: V.ffprobe_nb_frames(os.path.join(OUT, 'onset_%s.mp4' % m))
                              for m in modes
                              if os.path.exists(os.path.join(OUT, 'onset_%s.mp4' % m))})
    return 0


def fmt(v):
    return '—' if v is None else ('inf' if v == float('inf') else '%.2f' % v)


if __name__ == '__main__':
    sys.exit(main())
