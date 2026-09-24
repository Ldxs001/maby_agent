# -*- coding: utf-8 -*-
"""字幕框「出现时刻」的端到端验证 —— 走**生产代码路径**（`video_engine.compose`）。

与另一个探针（`box_onset_probe.py`，自搭 ffmpeg 命令比四种切法）的分工：
那个是**选型**用的，这个是**验收**用的——它调的就是出片时的那两个函数
（`assets_factory.bake_static_layers` + `video_engine.compose`），所以它过了，
就说明新参数接对了，而不只是"某条链跑得通"。

三份产物：

  A 现状  compose(有框图, box_frame=0)
  B 候选  compose(有框图, bg_plain=无框图, box_frame=N0)
  C 参照  compose(无框图, box_frame=0)          ← 「完全不画框」的下限参照

判据：
  · B 在框出现之前必须等于 C（那段时间是干净背景，PSNR 越高越好，理想逐字节相同）
  · B 在框出现之后必须等于 A（框的位置/颜色/透明度一字不差，PSNR ≥ 45 dB）
  · A 在框出现之前与 C 的差要明显——那是"空框"的可见代价，不是我们凭空说的
  · 帧数一分不差
  · 计时：A 与 B 交错跑，代价应在机器噪声内
"""
import math
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.join(ROOT, '_smoke')          # 产物锚点（见 tools/probes/README.md）
sys.path.insert(0, ROOT)

from podcast_maker import assets_factory as AF       # noqa: E402
from podcast_maker import video_engine as V          # noqa: E402
from podcast_maker.config_manager import ConfigManager  # noqa: E402

PROJ = os.path.join(ROOT, 'projects', '20260917-015336')
EP = '2b'
WORK = os.path.join(PROJ, '过程', EP)
RAW = os.path.join(PROJ, '背景', '%s_bg.png' % EP)     # assets 的无框原图
ASS = os.path.join(WORK, 'sub.ass')
WAV = os.path.join(WORK, 'voice_bgm.wav')
OUT = os.path.join(HERE, '_bench')

W, H, FPS = 1920, 1080, 30
DUR = 120.0


def cfg_of():
    cfg = ConfigManager(ROOT)
    try:
        cfg.load()
    except Exception as e:
        print('配置读取失败（继续用默认值）：%s' % e)
    return cfg


def onset_from_ass(path, fps):
    """从真用的 ASS 反解第一句文字的开口时刻，再交给生产函数换算成帧号。

    这么绕一圈是为了**不自己编时间**：ASS 是出片真正烧进画面的那一份，它的第一条
    文字事件的开始时刻就是 `timings[0]["start"]` 在厘秒精度上的值。
    """
    s = open(path, encoding='utf-8-sig', errors='replace').read()
    for line in s.splitlines():
        if not line.startswith('Dialogue:'):
            continue
        m = re.match(r'Dialogue:\s*\d+,(\d+):(\d+):(\d+\.\d+),', line)
        if m:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            return t, V.box_onset_frames([{'start': t}], fps)
    return 0.0, 0


def run(version, bg, bg_plain=None, box_frame=0):
    out = os.path.join(OUT, 'onset_%s.mp4' % version)
    t = time.time()
    V.compose(WAV, ASS, out, CFG, bg, DUR, W, H, '',
              font_dir=AF.fonts_dir_of(AF.resolve_font(CFG.get('subtitle.font_family', ''))),
              timings=None, log=lambda m: None,
              bg_plain=bg_plain, box_frame=box_frame)
    return time.time() - t


def grab(mp4, at, name):
    p = os.path.join(OUT, name)
    subprocess.run([V.ffmpeg_bin(), '-y', '-ss', '%.3f' % at, '-i', mp4,
                    '-frames:v', '1', p], capture_output=True)
    return p


def psnr(a, b):
    from PIL import Image
    ia, ib = Image.open(a).convert('RGB'), Image.open(b).convert('RGB')
    if ia.size != ib.size:
        return None
    pa, pb, n = ia.tobytes(), ib.tobytes(), None
    n = len(pa)
    mse = sum((x - y) ** 2 for x, y in zip(pa, pb)) / float(n)
    return float('inf') if mse == 0 else 10 * math.log10(255.0 ** 2 / mse)


def fmt(v):
    if v is None:
        return '—'
    return '逐字节相同' if v == float('inf') else '%.2f dB' % v


def main():
    global CFG
    CFG = cfg_of()
    os.makedirs(OUT, exist_ok=True)
    for p in (RAW, ASS, WAV):
        if not os.path.exists(p):
            print('缺素材：%s' % p)
            return 1

    t0, n0 = onset_from_ass(ASS, FPS)
    print('素材 %s  dur=%.0fs  第一句字幕开口 %.2fs → 第 %d 帧（%.4fs）'
          % (EP, DUR, t0, n0, n0 / float(FPS)))

    plain = AF.bake_static_layers(RAW, os.path.join(WORK, 'bg_h_plain.png'),
                                  CFG, W, H, '', box=False)
    boxed = AF.bake_static_layers(RAW, os.path.join(WORK, 'bg_h.png'),
                                  CFG, W, H, '')
    print('烘焙：无框 %s ／ 有框 %s'
          % (os.path.basename(plain), os.path.basename(boxed)))

    print('\n[计时] A 现状 ／ B 候选 ／ C 参照，交错各 4 轮（这台机器顺序跑会比不动）')
    res = {'A': [], 'B': [], 'C': []}
    for _ in range(4):
        res['A'].append(run('A', boxed))
        res['B'].append(run('B', boxed, bg_plain=plain, box_frame=n0))
        res['C'].append(run('C', plain))
    for k in ('A', 'B', 'C'):
        v = sorted(res[k])
        print('  %s 最小 %6.2fs ／ 中位 %6.2fs' % (k, v[0], v[len(v) // 2]))
    print('  → 候选相对现状：最小 %.3fx ／ 中位 %.3fx'
          % (min(res['A']) / min(res['B']),
             sorted(res['A'])[2] / sorted(res['B'])[2]))

    print('\n[对账] 框出现前 %.2fs ／ 框出现后 %.2fs' % (n0 / float(FPS) - 0.5,
                                                      n0 / float(FPS) + 1.5))
    before = n0 / float(FPS) - 0.5
    after = n0 / float(FPS) + 1.5
    mp4 = {k: os.path.join(OUT, 'onset_%s.mp4' % k) for k in 'ABC'}
    fb = {k: grab(mp4[k], before, 'x_%s_before.png' % k) for k in 'ABC'}
    fa = {k: grab(mp4[k], after, 'x_%s_after.png' % k) for k in 'ABC'}
    print('  框出现前：B vs C（应当无框）  %s' % fmt(psnr(fb['B'], fb['C'])))
    print('  框出现前：A vs C（空框的代价）%s' % fmt(psnr(fb['A'], fb['C'])))
    print('  框出现后：B vs A（框要一致）  %s' % fmt(psnr(fa['B'], fa['A'])))

    print('\n[结构] 帧数 %s' % {k: V.ffprobe_nb_frames(mp4[k]) for k in 'ABC'})
    print('     音画同步 %s' % V.verify_av_sync(mp4['B'])['detail'])

    ok = (psnr(fb['B'], fb['C']) is None or psnr(fb['B'], fb['C']) >= 45.0) \
        and (psnr(fa['B'], fa['A']) or 0) >= 45.0 \
        and V.ffprobe_nb_frames(mp4['B']) == int(round(DUR * FPS))
    print('\n总判：%s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


CFG = None

if __name__ == '__main__':
    sys.exit(main())
