# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
# SPDX-License-Identifier: Apache-2.0
"""合成链的分级吞吐上限：输入解码 / 归一 / libass / 编码，逐级加，看哪一级是墙。

起因：单路 ffmpeg 能跑到 281 帧/秒（1080p），而把它切成 2 路（各 12 线程）吞吐不变、
切成 6 路（各 4 线程）反而更慢。也就是说**机器已经被一个进程吃满了**，并发的收益
上限就是 0。那就要问：这一个进程的时间花在哪一级？如果花在某一段"本来就不该这么贵"
的地方（比如每帧都重新解一遍 PNG），把它挪掉比叠并发划算得多。

每级都用 `-f null -` 收尾（第 5 级除外），所以量到的是**这一级的纯吞吐**：

  ① 输入侧        PNG 循环解码
  ② 输入侧（改法）单帧进 + `loop` 滤镜在内存里复制 —— 与①对比就是"每帧重解 PNG"的价钱
  ③ ①＋scale/crop/setsar 归一
  ④ ③＋format=rgb24 ＋ ass ＋ format=yuv420p
  ⑤ ④＋libx264 编码（就是现在出片的完整链）

环境变量 `PAR_CEIL_SECONDS`（默认 120）改窗口长度。
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
OUT = os.path.join(tempfile.gettempdir(), 'pm_ceiling')
SEC = float(os.environ.get('PAR_CEIL_SECONDS', '120'))
W, H, FPS = 1920, 1080, 30
FRAMES = int(SEC * FPS)


def build():
    os.makedirs(OUT, exist_ok=True)
    script = json.load(io.open(os.path.join(PROJ, '脚本', '2c.json'), encoding='utf-8'))
    cfg = dict(ConfigManager().data())
    cfg['subtitle.preset'] = 'single'
    cfg['animation.mode'] = 'static'
    cfg['video.fps'] = FPS
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
    return ass, bg, cfg


def timed(cmd):
    """跑一条命令，返回 (墙钟, CPU 秒, 核当量)。

    `-benchmark` 让 ffmpeg 把 utime/stime/rtime 打到 stderr：utime 是进程（含全部
    线程）的 CPU 秒。**核当量 = utime / rtime** 就是这条命令实际吃掉了几个核——
    这是「单路到底有没有把机器吃满」唯一说得清的量法。
    """
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, cwd=OUT)
    wall = time.time() - t0
    if r.returncode:
        raise RuntimeError((r.stderr or b'').decode('utf-8', 'replace')[-400:])
    err = (r.stderr or b'').decode('utf-8', 'replace')
    cpu = 0.0
    for line in err.splitlines():
        if line.startswith('bench:'):
            for kv in line.split()[1:]:
                k, _, v = kv.partition('=')
                if k in ('utime', 'stime'):
                    cpu += float(v.rstrip('s'))
    return wall, cpu, (cpu / wall if wall else 0.0)


def main():
    ass, bg, cfg = build()
    ass_arg = "ass=%s" % V._ff_escape(os.path.abspath(ass))
    ff = V.ffmpeg_bin()

    def null_cmd(fc, input_side, out_extra=()):
        c = [ff, '-y', '-hide_banner', '-benchmark', '-nostats', '-loglevel', 'info'] + input_side
        if fc:
            c += ['-filter_complex', fc, '-map', '[v]']
        else:
            c += ['-map', '0:v']
        c += ['-t', '%.3f' % SEC]
        c += list(out_extra)          # 输出选项必须在输出文件之前
        c += ['-f', 'null', '-']
        return c

    loop_input = ['-framerate', str(FPS), '-loop', '1', '-i', bg]
    single_input = ['-framerate', str(FPS), '-i', bg]
    repeat = 'loop=loop=-1:size=1:start=0,'
    norm = 'scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1' % (
        W, H, W, H)

    cases = [
        ('① 输入侧 · PNG 循环解码', null_cmd(None, loop_input)),
        ('② 输入侧 · 单帧进 + loop 滤镜',
         null_cmd('[0:v]%snull[v]' % repeat, single_input)),
        ('③ ①＋归一 scale/crop', null_cmd('[0:v]%s[v]' % norm, loop_input)),
        ('④ ③＋rgb24＋ass',
         null_cmd('[0:v]%s,format=rgb24,%s,format=yuv420p[v]' % (norm, ass_arg),
                  loop_input)),
        ('⑤ ④＋libx264 编码（现在的全链）',
         null_cmd('[0:v]%s,format=rgb24,%s,format=yuv420p[v]' % (norm, ass_arg),
                  loop_input,
                  out_extra=['-c:v', 'libx264', '-preset',
                             str(cfg.get('video.encoder_preset', 'medium')),
                             '-crf', str(int(cfg.get('video.crf', 20)))])),
        # 下面两条是「砍每帧搬运量」的两个候选，量的都是同一条全链，只去掉一环。
        ('⑥ 全链 · 去掉恒等 scale/crop（尺寸本已相等）',
         null_cmd('[0:v]format=rgb24,%s,format=yuv420p[v]' % ass_arg, loop_input,
                  out_extra=['-c:v', 'libx264', '-preset',
                             str(cfg.get('video.encoder_preset', 'medium')),
                             '-crf', str(int(cfg.get('video.crf', 20)))])),
        ('⑦ 全链 · 去掉 rgb24 往返（ass 直接吃 yuv420p）',
         null_cmd('[0:v]%s,%s,format=yuv420p[v]' % (norm, ass_arg), loop_input,
                  out_extra=['-c:v', 'libx264', '-preset',
                             str(cfg.get('video.encoder_preset', 'medium')),
                             '-crf', str(int(cfg.get('video.crf', 20)))])),
        # ⑧ 是「输入方式」这一环：`-loop 1` 每帧都要重新读/解一次 PNG，而
        # 「单帧进 + loop 滤镜」只解一次、之后在内存里复制同一张帧。
        ('⑧ 全链 · 输入改单帧进＋loop 滤镜',
         null_cmd('[0:v]%s%s,format=rgb24,%s,format=yuv420p[v]'
                  % (repeat, norm, ass_arg), single_input,
                  out_extra=['-c:v', 'libx264', '-preset',
                             str(cfg.get('video.encoder_preset', 'medium')),
                             '-crf', str(int(cfg.get('video.crf', 20)))])),
    ]

    print('窗口 %.0f 秒（%d 帧）%d×%d；preset %s / crf %s；设备 %d 逻辑核'
          % (SEC, FRAMES, W, H, cfg.get('video.encoder_preset'),
             cfg.get('video.crf'), os.cpu_count() or 1))
    print()
    print('   %-34s %8s %9s %9s' % ('', '墙钟', '帧/秒', '吃掉几个核'))
    prev = None
    for label, cmd in cases:
        t, cpu, cores = timed(cmd)
        fps = FRAMES / t
        delta = '' if prev is None else '   比上一级多花 %.1f%%' % ((t / prev - 1) * 100)
        print('   %-34s %7.1fs %8.1f %9.1f%s' % (label, t, fps, cores, delta))
        prev = t
    print()
    print('  「吃掉几个核」那一列是判据：')
    print('   · ⑤ 的核当量接近满机核数 → 单路已经把机器吃满，叠并发没有可捡的算力；')
    print('   · ⑤ 的核当量远低于核数 → 链上有单线程的墙，那才值得按段切进程。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
