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

"""BGM 素材响度归一的共用实现 —— 测量 + 施加线性增益。

单一实现，两个调用方
--------------------
- `tools/bgm_level.py`：**事后校正**已有素材目录（就地覆盖 + 备份 + 复测）
- `tools/probes/bgm_gen.py`：**生成时归一**，新产出的素材一落盘就是平的一档

为什么必须有这个模块
--------------------
素材生成器的 `save_wav()` 原先只做**削峰保护**（`peak > 0.90` 才缩），不做
响度归一 —— 每个档位各削各的，轻的原样留着、重的被压下来。实测 15 档
integrated LUFS 从 `chimes -15.18` 到 `horror -28.76`，**相差 13.6 LU**。
后果是同一个 `bgm.volume` 在不同档位上实际响度差 4 倍：换一档就从「听不清」
跳成「盖住人声」，混音侧怎么调默认值都救不了。

设计约束
--------
1. **只施加线性增益**：不做动态压缩、不做限幅、不改采样率/位深/声道数。
   素材的音色与动态结构一个采样都不动，只是整体音量乘一个常数。
2. **只走真峰值约束，不夹不削**：增益按真峰值上限算，若算出的增益会导致
   任何样本溢出，**直接判失败**而不是顺手 clip 一下。「悄悄削掉一点」正是
   本模块存在的理由，绝不允许自己犯同样的错。
3. **写完必复测**：不复测等于没做。偏差超过 `VERIFY_TOL_LU` 即判失败。
4. **测量走 ffmpeg loudnorm**（EBU R128 标准实现）；施加增益走纯 Python
   PCM 整数运算，避免 ffmpeg 的重采样与抖动参与进来。
"""

import array
import os
import re
import shutil
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_TARGET_LUFS = -23.0
DEFAULT_TP_CEIL = -1.0

# 复测允许的偏差（LU）。超过即认为归一没做对。
VERIFY_TOL_LU = 0.5

# 素材是 s16le 单声道 PCM。别的形态一律拒绝，免得静默改了素材规格。
PCM_WIDTH = 2
INT16_MIN, INT16_MAX = -32768, 32767


def ffmpeg_bin():
    """优先用项目里那套定位逻辑，拿不到就退回 PATH。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from podcast_maker.audio_engine import ffmpeg_bin as _fb
        return _fb()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def measure(path, ffmpeg):
    """返回 (integrated_lufs, true_peak_dbtp)。测不出返回 (None, None)。"""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path,
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")

    def grab(key):
        m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, proc.stderr)
        if not m:
            return None
        raw = m.group(1).strip()
        if raw in ("-inf", "inf", "nan", ""):
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    return grab("input_i"), grab("input_tp")


def read_pcm(path):
    """读 s16le 单声道 WAV，返回 (array('h'), sample_rate)。规格不符即报错。"""
    with wave.open(path, "rb") as wf:
        nch, width, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if nch != 1 or width != PCM_WIDTH:
        raise ValueError("%s 不是单声道 16bit PCM（channels=%d width=%d），"
                         "拒绝改动" % (os.path.basename(path), nch, width))
    samples = array.array("h")
    samples.frombytes(raw)
    return samples, rate


def write_pcm(path, samples, rate):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(PCM_WIDTH)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def apply_gain_db(samples, gain_db):
    """对 PCM 施加线性增益，返回 (新样本, 削波数)。

    削波数应当恒为 0 —— 调用方已按真峰值约束算过增益，出现非零说明约束
    没算对，必须当失败报出来，不能悄悄夹一下了事。
    """
    g = 10.0 ** (gain_db / 20.0)
    out = array.array("h", [0]) * len(samples)
    clipped = 0
    for i, v in enumerate(samples):
        x = int(round(v * g))
        if x > INT16_MAX:
            x = INT16_MAX
            clipped += 1
        elif x < INT16_MIN:
            x = INT16_MIN
            clipped += 1
        out[i] = x
    return out, clipped


def plan_gain(lufs, tp, target_lufs, tp_ceil):
    """按目标响度与真峰值上限算增益。返回 (gain_db, limited)。

    `limited=True` 表示峰值先撞线：只能提到真峰值允许的位置，响度到不了
    目标。调用方应当把这种情况报出来，而不是偷偷降目标。
    """
    gain = target_lufs - lufs
    if tp + gain > tp_ceil:
        return tp_ceil - tp, True
    return gain, False


def normalize_file(path, target_lufs=DEFAULT_TARGET_LUFS,
                   tp_ceil=DEFAULT_TP_CEIL, ffmpeg=None):
    """把单个 WAV 就地拉到目标响度。

    返回 dict：`ok` / `gain_db` / `lufs` / `tp` / `after_lufs` / `after_tp` /
    `limited` / `reason`。

    **文件只在 `ok=True` 时被改动**：任何一步出问题都不写盘；万一写完复测
    不过，会把内存里留着的原样本写回去再返回失败。调用方不需要自己兜底。
    """
    ffmpeg = ffmpeg or ffmpeg_bin()
    name = os.path.basename(path)

    lufs, tp = measure(path, ffmpeg)
    if lufs is None or tp is None:
        return {"ok": False, "name": name, "reason": "测量失败（loudnorm 未给出数值）"}

    gain, limited = plan_gain(lufs, tp, target_lufs, tp_ceil)

    # 可达性前置判定：峰值先撞线时，响度能到哪儿是算得出来的。到不了目标
    # 就别写 —— 「先改一版再报失败」等于留下一个响度不合标的素材，而调用方
    # 可能只看返回码就把备份删了。
    reachable = lufs + gain
    if reachable < target_lufs - VERIFY_TOL_LU:
        return {"ok": False, "name": name, "lufs": lufs, "tp": tp,
                "gain_db": gain, "limited": True, "reason":
                "目标 %.2f LUFS 在真峰值上限 %.2f dBTP 内不可达：该素材峰值为 "
                "%.2f dBTP，最多只能到 %.2f LUFS（差 %.2f LU）。"
                "要么放宽 --tp-ceil，要么降低 --target-lufs。"
                % (target_lufs, tp_ceil, tp, reachable,
                   target_lufs - reachable)}

    try:
        samples, rate = read_pcm(path)
    except ValueError as exc:
        return {"ok": False, "name": name, "reason": str(exc)}

    new, clipped = apply_gain_db(samples, gain)
    if clipped:
        # 真峰值约束算错了才会走到这里。宁可失败也不削。
        return {"ok": False, "name": name,
                "reason": "出现 %d 个削波样本，未写入（真峰值约束失效）" % clipped}
    write_pcm(path, new, rate)

    # 闭环复测：不测就等于没做。不过就把原样本写回去，保证「失败 = 文件未变」。
    after_lufs, after_tp = measure(path, ffmpeg)
    delta = None if after_lufs is None else after_lufs - target_lufs
    if after_lufs is None or abs(delta) > VERIFY_TOL_LU:
        write_pcm(path, samples, rate)
        return {"ok": False, "name": name, "lufs": lufs, "tp": tp,
                "gain_db": gain, "after_lufs": after_lufs, "after_tp": after_tp,
                "reason": ("写入后复测失败" if after_lufs is None else
                           "复测 LUFS 偏离目标 %+.2f LU（容差 %.1f），已还原原文件"
                           % (delta, VERIFY_TOL_LU))}

    return {"ok": True, "name": name, "lufs": lufs, "tp": tp,
            "gain_db": gain, "after_lufs": after_lufs, "after_tp": after_tp,
            "limited": limited, "delta": delta}
