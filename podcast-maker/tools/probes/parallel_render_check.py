# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
# SPDX-License-Identifier: Apache-2.0
"""分段并行的对齐、噪声与计时。

三件事各用各的尺子——前一轮把它们混成一把尺子，得出的结论是错的：

  · **对齐**（决定性、0/1）。两条路都改成 `crf 0` 无损编：无损解码回来的帧必须
    逐帧等于编码器吃进去的那一帧，**与 GOP 结构无关**。于是「无损串行」与「无损
    分段」的解码结果若逐帧 MSE 全 0，就证明切点 / `trim` / `setpts` 没让任何一帧
    错位。这项没有主观阈值，要么全 0 要么不是。
  · **画质**（对真值，不对另一条有损片）。把无损片当真值，各自的有损片与真值比
    PSNR：谁离本该画出的画面更远，谁画质就差。**不去要求两条有损片逐像素相等**——
    两条路各自独立编码，各自做码率控制与前瞻，彼此不同是必然的；有意义的是
    「分段有没有让画质掉下来」+「有没有过视觉无损线」。另出一条『串行 + 同数量
    IDR』的对照当参照系。
  · **计时**。全长素材（真实一期 22.7 分钟）上量 1 路 vs N 路墙钟。40 秒的短片
    量不出真收益——6 路的进程启动/收尾开销占比过高。

环境变量：`PAR_SECONDS` 改计时段长度（默认取音频全长）。
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
# 基准产物落系统临时目录：这里要出好几条全长片子（每条几十 MB），不该往工程目录里
# 堆。放临时目录也就没有"跑完清理"这一步——省得反复触发宿主的大批量删除确认。
OUT = os.path.join(tempfile.gettempdir(), 'pm_par_bench')
ALIGN_SEC = 40                     # 对齐/画质窗口：40 秒里跨 5 个段边界
W, H = 1920, 1080

# 探针不做段目录清理（`_compose_segmented` 成功后会 rmtree）。一次基准要跑 A/B/C 三组
# 分段，三组连着删就会撞上宿主"每轮大批量删除需确认"的闸；而这一步只影响垃圾清理、
# 不影响片子本身。生产里一期只清两次（横竖各一），远不到闸门，照常。
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

    ass = os.path.join(OUT, 'sub.ass')
    io.open(ass, 'w', encoding='utf-8').write(
        S.build_ass(script, cfg, timings, W, H, '')[0])
    V.render_ass(ass, ass, script, timings, cfg, W, H, '')
    bg = A.bake_static_layers(os.path.join(PROJ, '背景', '2c_bg.png'),
                              os.path.join(OUT, 'bg.png'), cfg, W, H, '')
    font = A.resolve_font(cfg.get('subtitle.font_family', ''))
    return (cfg, ass, bg, os.path.join(PROJ, '音视频', '2c.mp3'), timings,
            A.fonts_dir_of(font))


def run(ctx, out, workers, dur, crf=None, preset=None, x264_extra=None):
    """按指定口径出一条片，返回墙钟秒数。"""
    cfg, ass, bg, audio, timings, font_dir = ctx
    c = dict(cfg)
    if crf is not None:
        c['video.crf'] = crf
    if preset is not None:
        c['video.encoder_preset'] = preset
    orig = V._x264_args
    if x264_extra:
        V._x264_args = lambda cc: orig(cc) + list(x264_extra)
    p = os.path.join(OUT, out)
    t0 = time.time()
    try:
        if workers <= 1:
            V._compose_single(audio, ass, p, c, bg, dur, W, H, '',
                              font_dir=font_dir, timings=timings, log=lambda m: None)
        else:
            V._compose_segmented(audio, ass, p, c, bg, dur, W, H, '',
                                 font_dir=font_dir, timings=timings,
                                 log=lambda m: None, workers=workers)
    finally:
        V._x264_args = orig
    return time.time() - t0, p


def psnr_frames(a, b, tag):
    """整片逐帧 PSNR。返回 [(帧序, mse_avg, psnr_avg)]。

    stats_file 走相对名 + cwd 落在基准目录，免得给 ffmpeg 的滤镜参数转义盘符冒号。
    """
    log = '%s_psnr.log' % tag
    lp = os.path.join(OUT, log)
    if os.path.exists(lp):
        os.remove(lp)
    cmd = [V.ffmpeg_bin(), '-y', '-hide_banner', '-loglevel', 'error',
           '-i', os.path.basename(a), '-i', os.path.basename(b),
           '-filter_complex', '[0:v][1:v]psnr=stats_file=%s[v]' % log,
           '-map', '[v]', '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=OUT)
    if r.returncode:
        raise RuntimeError(r.stderr[-400:])
    rows = []
    for line in io.open(lp, encoding='utf-8'):
        d = {}
        for kv in line.split():
            if ':' in kv:
                k, _, v = kv.partition(':')
                d[k] = v
        if 'n' not in d:
            continue
        rows.append((int(d['n']), float(d['mse_avg']),
                     float('inf') if d['psnr_avg'] == 'inf' else float(d['psnr_avg'])))
    return rows


def spread(rows):
    """逐帧 PSNR 的分布：最差一帧、最差 1% 处、中位。inf 记成一个大数方便排序。"""
    vals = sorted(1e9 if p == float('inf') else p for _n, _m, p in rows)
    if not vals:
        return (0.0, 0.0, 0.0)
    return (vals[0], vals[max(0, int(len(vals) * 0.01) - 1)], vals[len(vals) // 2])


def grab(mp4, t, png):
    r = subprocess.run([V.ffmpeg_bin(), '-y', '-hide_banner', '-loglevel', 'error',
                        '-ss', '%.4f' % t, '-i', mp4, '-frames:v', '1', png],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-300:])
    return png


def main():
    ctx = prepare()
    cfg, ass, _bg, audio, timings, _fd = ctx
    workers = V.render_workers()
    fps = int(cfg.get('video.fps', 30))
    print('设备逻辑核 %d → 并行度 %d（上限 %d，每路按 %d 核折算）'
          % (os.cpu_count() or 1, workers, V.WORKERS_MAX, V.CORES_PER_FFMPEG))
    if V.segment_blocker(cfg):
        print('当前配置切不动：%s' % V.segment_blocker(cfg))
    print('对齐窗口 %.0f 秒 / %d×%d；计时段取音频全长' % (ALIGN_SEC, W, H))
    print('切点：%s' % ['%.2f–%.2f(%d帧)' % s for s in
                     V.cut_points(ALIGN_SEC, fps, workers, timings)])
    print()

    # ---------------------------------------------------------------- A 对齐
    print('A. 对齐（两条路都 crf 0 无损，逐帧 MSE 必须全 0）')
    _t, a_ser = run(ctx, 'A_serial.mp4', 1, ALIGN_SEC, crf=0, preset='ultrafast')
    _t, a_seg = run(ctx, 'A_seg.mp4', workers, ALIGN_SEC, crf=0, preset='ultrafast')
    na, nb = V.ffprobe_nb_frames(a_ser), V.ffprobe_nb_frames(a_seg)
    rows = psnr_frames(a_ser, a_seg, 'A')
    bad = [r for r in rows if r[1] > 0.0]
    print('   串行 %d 帧 / 分段 %d 帧 / 比对 %d 帧' % (na, nb, len(rows)))
    if na != nb:
        print('   [FAIL] 帧数不一致')
    elif bad:
        print('   [FAIL] 有 %d 帧对不上（错位）。前几帧：%s'
              % (len(bad), ['#%d mse=%.3f' % (n, m) for n, m, _p in bad[:5]]))
    else:
        print('   [PASS] 全部 %d 帧逐帧 MSE = 0：没有任何一帧错位' % len(rows))
    align_ok = (na == nb and not bad)

    # ---------------------------------------------------------------- B 画质
    print()
    print('B. 画质（crf %s / preset %s：各有损片对**无损真值**，看谁更接近本该画出的画面）'
          % (cfg.get('video.crf'), cfg.get('video.encoder_preset')))
    seg_frames = V.cut_points(ALIGN_SEC, fps, workers, timings)
    gop = int(round(ALIGN_SEC * fps / float(max(1, len(seg_frames)))))
    _t, b_ser = run(ctx, 'B_serial.mp4', 1, ALIGN_SEC)
    _t, b_seg = run(ctx, 'B_seg.mp4', workers, ALIGN_SEC)
    _t, b_ctl = run(ctx, 'B_ctrl.mp4', 1, ALIGN_SEC,
                    x264_extra=['-g', str(gop), '-keyint_min', str(gop),
                                '-sc_threshold', '0'])
    print('   参照：对照 = 串行 + keyint=%d（只改 GOP 结构，IDR 个数与 %d 段相同）'
          % (gop, len(seg_frames)))
    r_ss = psnr_frames(b_ser, a_ser, 'Bss')      # 串行有损 vs 真值
    r_gs = psnr_frames(b_seg, a_ser, 'Bgs')      # 分段有损 vs 真值
    r_cs = psnr_frames(b_ctl, a_ser, 'Bcs')      # 对照有损 vs 真值
    r_sg = psnr_frames(b_ser, b_seg, 'Bsg')      # 两条路互相差多少（仅信息）
    print('   %-24s %-10s %-10s %s' % ('（对无损真值）', '最差帧', '最差1%', '中位'))
    for label, rows in (('串行 1 路', r_ss), ('分段 %d 路' % workers, r_gs),
                        ('对照(只改GOP)', r_cs)):
        sl, s1, sm = spread(rows)
        print('   %-24s %-10s %-10s %s' % (label, *[('%.2f' % v) if v < 1e9 else 'inf'
                                                    for v in (sl, s1, sm)]))
    print('   %-24s %-10s %-10s %s' % ('（分段 vs 串行，互联差）',
                                       *[('%.2f' % v) if v < 1e9 else 'inf'
                                         for v in spread(r_sg)]))
    ss, gs = spread(r_ss), spread(r_gs)
    # 判据两条，都写明白，不用拍脑袋的阈值：
    #   ①绝对：分段对真值的最差帧 ≥ 45 dB——8 bit 编码「视觉无损」的通行线。
    #   ②相对：分段的代价 ≤ 2 dB，且 ≤ **串行自身逐帧波动**的 1/4。串行自己的最差帧
    #     与中位就差了 5 dB 以上，尺子是编码器的天然波动，不是我随便定的数。
    nat = ss[2] - ss[0]
    cost = ss[0] - gs[0]
    ok_abs = gs[0] >= 45.0
    ok_rel = cost <= 2.0 and cost <= nat / 4.0
    noise_ok = ok_abs and ok_rel
    print('   串行自身逐帧波动（中位−最差）%.2f dB；分段代价（最差帧之差）%.2f dB'
          % (nat, cost))
    print('   [%s] ①分段 %.2f dB ≥ 45 dB 视觉无损线；②代价 %.2f dB ≤ 2 dB 且 ≤ 波动/4（%.2f dB）'
          % ('PASS' if noise_ok else 'FAIL', gs[0], cost, nat / 4.0))

    # 定位：两条路互相差得最狠那几帧，差在哪一块像素
    from PIL import Image, ImageChops
    worst = sorted(r_sg, key=lambda r: r[2])[:3]
    print('   两条路互相差得最狠的帧（字幕框在 y≈920–990）：')
    for n, _m, p in worst:
        t = (n - 1) / float(fps)
        pa = grab(b_ser, t, os.path.join(OUT, 'w_a.png'))
        pb = grab(b_seg, t, os.path.join(OUT, 'w_b.png'))
        diff = ImageChops.difference(Image.open(pa).convert('RGB'),
                                     Image.open(pb).convert('RGB'))
        chans = diff.split()
        mask = None
        for ch in chans:
            m = ch.point(lambda v: 255 if v > 8 else 0)
            mask = m if mask is None else ImageChops.lighter(mask, m)
        cnt = mask.histogram()[255]
        mx = max(ch.getextrema()[1] for ch in chans)
        print('      #%-5d t=%7.3fs  PSNR %6.2f  >8 的 %6d 个（%.3f%%）  区域 %s  峰值 %d'
              % (n, t, p, cnt, 100.0 * cnt / float(W * H), mask.getbbox(), mx))

    # ---------------------------------------------------------------- C 计时
    print()
    dur = float(os.environ.get('PAR_SECONDS') or 0) or _audio_len(audio)
    print('C. 计时（全长 %.0f 秒 = %.1f 分钟）' % (dur, dur / 60.0))
    t_ser, c_ser = run(ctx, 'C_serial.mp4', 1, dur)
    t_seg, c_seg = run(ctx, 'C_seg.mp4', workers, dur)
    nf_ser, nf_seg = V.ffprobe_nb_frames(c_ser), V.ffprobe_nb_frames(c_seg)
    st_ser, st_seg = V.probe_streams(c_ser), V.probe_streams(c_seg)
    print('   %-12s %-8s %-10s %-12s %s' % ('', '帧数', '墙钟', '视频时长', '音频时长'))
    print('   %-12s %-8s %-10s %-12s %s' % ('串行 1 路', nf_ser, '%.1fs' % t_ser,
                                            st_ser['video']['duration'],
                                            '%.3fs' % st_ser['audio']['duration']))
    print('   %-12s %-8s %-10s %-12s %s' % ('分段 %d 路' % workers, nf_seg,
                                            '%.1fs' % t_seg,
                                            st_seg['video']['duration'],
                                            '%.3fs' % st_seg['audio']['duration']))
    print('   提速 %.2fx（%.1f 帧/秒 → %.1f 帧/秒）'
          % (t_ser / t_seg if t_seg else 0,
             nf_ser / t_ser if t_ser else 0, nf_seg / t_seg if t_seg else 0))
    dur_ok = nf_ser == nf_seg
    print('   [%s] 帧数一致：%s' % ('PASS' if dur_ok else 'FAIL', nf_seg))

    ok = align_ok and noise_ok and dur_ok
    print()
    print('总判：%s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def _audio_len(p):
    r = subprocess.run([V.ffprobe_bin(), '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', p],
                       capture_output=True, text=True)
    return float((r.stdout or '0').strip() or 0)


if __name__ == '__main__':
    sys.exit(main())
