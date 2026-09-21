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

"""内置 BGM 素材响度拉平 —— **事后校正**工具，不进管线。

与生成侧的分工
--------------
- **生成时归一**：`_smoke/_bgm_gen.py` 落盘即调 `bgm_loudness.normalize_file()`，
  新产出的素材一出来就是平的一档。
- **事后校正（本脚本）**：对**已存在的**素材目录就地拉平，带备份 + 闭环复测。
  用途是补历史素材，或校正从别处拿来的素材。
两者共用 `tools/bgm_loudness.py`，同一套测量与增益算法，不会跑出两个标准。

为什么需要
----------
`_smoke/_bgm_gen.py` 原先只在 `save_wav()` 里做**削峰保护**（`peak > 0.90`
才缩），不做响度归一。MusicGen 每一档生成出来的动态差异被原样保留，实测
15 档的 integrated LUFS 从 `chimes -15.18` 到 `horror -28.76`，**相差 13.6
LU**。后果：同一个 `bgm.volume` 值在不同档位上实际响度差 4 倍 —— 换一档
就从「听不清」跳成「盖住人声」。混音侧无论怎么调默认值都救不了这种不齐。

本脚本做什么
------------
按 EBU R128 integrated loudness 把素材拉到同一响度，**只施加线性增益**，
不做动态压缩、不做限幅、不改采样率/位深/声道数 —— 素材的音色与动态结构
一个采样都不动，只是整体音量被乘了一个常数。

测量走 ffmpeg 的 `loudnorm`（EBU R128 标准实现）；施加增益走纯 Python 的
PCM 整数运算，避免 ffmpeg 的重采样与抖动参与进来。

硬约束：真峰值
--------------
线性增益对 LUFS 是严格线性的，所以「增益到目标响度」在数学上总能做到；
唯一的物理约束是**真峰值不得越过 `--tp-ceil`**。最紧的一档是 `mech`，
它的 TP 已经是 **-0.40 dBTP**，只剩 0.6 dB 提升空间 —— 因此目标响度
不能高于 **-22.4 LUFS**，否则该档必然削波。默认取 **-23.0 LUFS**，
经测算 15 档全部可达且留有余量。

用法
----
    python tools/bgm_level.py                 # 只测量并打印对照表（不改文件）
    python tools/bgm_level.py --apply         # 备份后施加增益，并复测验证
    python tools/bgm_level.py --target-lufs -24 --apply

不传 `--apply` 时**一个文件都不会写**。
"""

import argparse
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bgm_loudness import (                                    # noqa: E402
    DEFAULT_TARGET_LUFS, DEFAULT_TP_CEIL,
    ffmpeg_bin, measure, normalize_file, plan_gain)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BGM_DIR = os.path.join(ROOT, "podcast_maker", "resources", "bgm")
BACKUP_ROOT = os.path.join(ROOT, "_smoke")


def plan_one(path, ffmpeg, target_lufs, tp_ceil):
    """算一档该加多少 dB。返回 dict。"""
    lufs, tp = measure(path, ffmpeg)
    name = os.path.basename(path)
    if lufs is None or tp is None:
        return {"name": name, "path": path, "error": "测量失败（loudnorm 未给出数值）"}

    gain, limited = plan_gain(lufs, tp, target_lufs, tp_ceil)
    return {"name": name, "path": path, "lufs": lufs, "tp": tp,
            "gain": gain, "limited": limited,
            "expect_lufs": lufs + gain, "expect_tp": tp + gain}


def cmd_measure(plans):
    print("%-10s %10s %12s %10s %12s %10s" %
          ("档位", "LUFS(I)", "TP(dBTP)", "需加dB", "加后LUFS", "加后TP"))
    print("-" * 72)
    for p in plans:
        if p.get("error"):
            print("%-10s  %s" % (p["name"], p["error"]))
            continue
        flag = "  ←峰值受限" if p["limited"] else ""
        print("%-10s %10.2f %12.2f %+10.2f %12.2f %10.2f%s" %
              (p["name"], p["lufs"], p["tp"], p["gain"],
               p["expect_lufs"], p["expect_tp"], flag))


def cmd_apply(plans, ffmpeg, target_lufs, tp_ceil):
    ok = [p for p in plans if not p.get("error")]
    if not ok:
        print("没有任何一档可处理。")
        return 1

    stamp = datetime.date.today().strftime("%Y%m%d")
    backup = os.path.join(BACKUP_ROOT, "_bgm_level_backup_%s" % stamp)
    os.makedirs(backup, exist_ok=True)
    print("备份目录：%s" % backup)

    failures = []
    for p in ok:
        src = p["path"]
        keep = os.path.join(backup, os.path.basename(src))
        if not os.path.exists(keep):
            shutil.copy2(src, keep)

        # 施加增益与复测都在 normalize_file 里，且保证「失败 = 文件未变」。
        res = normalize_file(src, target_lufs, tp_ceil, ffmpeg)
        if not res.get("ok"):
            failures.append("%s %s" % (p["name"], res["reason"]))
            continue
        print("  %-10s %+.2f dB → LUFS %7.2f (目标 %6.2f, Δ%+.2f)  TP %6.2f  %s%s" %
              (p["name"], res["gain_db"], res["after_lufs"], target_lufs,
               res["delta"], res["after_tp"], "OK",
               "  ←峰值受限" if res["limited"] else ""))

    print()
    if failures:
        print("失败项：")
        for f in failures:
            print("  - %s" % f)
        print("回退：把 %s 下的文件拷回 %s" % (backup, BGM_DIR))
        return 1
    print("全部通过：%d 档响度已拉平到 %.1f LUFS。" % (len(ok), target_lufs))
    print("回退：把 %s 下的文件拷回 %s" % (backup, BGM_DIR))
    return 0


def main():
    ap = argparse.ArgumentParser(description="内置 BGM 素材响度拉平")
    ap.add_argument("--dir", default=BGM_DIR, help="素材目录（默认内置 BGM 目录）")
    ap.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS,
                    help="目标 integrated loudness（默认 %g）" % DEFAULT_TARGET_LUFS)
    ap.add_argument("--tp-ceil", type=float, default=DEFAULT_TP_CEIL,
                    help="真峰值上限 dBTP（默认 %g）" % DEFAULT_TP_CEIL)
    ap.add_argument("--apply", action="store_true",
                    help="真正写入；不传则只测量打印")
    args = ap.parse_args()

    ffmpeg = ffmpeg_bin()
    files = sorted(f for f in os.listdir(args.dir) if f.endswith(".wav"))
    if not files:
        print("目录里没有 wav：%s" % args.dir)
        return 1

    print("样本 %d 档 | 目标 %.2f LUFS | 真峰值上限 %.2f dBTP | ffmpeg=%s"
          % (len(files), args.target_lufs, args.tp_ceil, ffmpeg))
    print()
    plans = [plan_one(os.path.join(args.dir, f), ffmpeg,
                      args.target_lufs, args.tp_ceil) for f in files]
    cmd_measure(plans)

    if not args.apply:
        print()
        print("（仅测量。加 --apply 才会写入。）")
        return 0

    print()
    return cmd_apply(plans, ffmpeg, args.target_lufs, args.tp_ceil)


if __name__ == "__main__":
    sys.exit(main())
