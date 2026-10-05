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

"""LRC 时间戳取整 —— 把 `[mm:ss.xx]` 的百分秒**四舍五入**进整秒，得 `[mm:ss]`。

**规则**（严格四舍五入，不是 Python `round()` 的银行家舍入）：

    小数 >= .50 → 进一秒（`[00:02.50]` → `[00:03]`）
    小数 <  .50 → 舍掉  （`[00:20.41]` → `[00:20]`）

按「总秒数」进位，所以秒溢出会正常带动分钟：`[00:59.72]` → `[01:00]`。
分钟位补零到两位、允许超过 59（与 `subtitle_engine.fmt_lrc_time` 同一口径）。

**只改时间戳**：行文本、说话人前缀、`\\r\\n` 换行、有无 BOM、末尾有无换行，一律逐字节保留。
非时间戳的方括号（如 `[ti:]`、`[ar:]` 元数据标签）、已经不带小数的戳，原样不动。

**产品侧有同源实现**：出片时 `subtitle_engine.build_txt` 会与 SRT／LRC 一起直接
生成整秒 TXT（`fmt_second_time`，同一颗百分秒量化 + 整数半加）。本工具是**独立的
离线实现**，管两件事：存量 LRC 的批量换算、以及给那边当交叉验证的参照——两边对
同一串时间戳必须给出同一个答案（`tests/test_subtitle_txt.py` 锁这条同源律）。

**为什么不顺手把分钟也去掉**：LRC 播放器认的是 `mm:ss`，去掉分钟会退化成裸秒数，
且一行内多个戳（`[t1][t2]歌词`）需要每个都转。规则只做「取整」这一件事。

用法：
    python tools/lrc_round_seconds.py projects/20260917-015336/字幕/1.lrc          # 只看报告
    python tools/lrc_round_seconds.py projects/20260917-015336/字幕 --all          # 整个目录
    python tools/lrc_round_seconds.py <文件或目录> --write                         # 就地改写（先备份）
    python tools/lrc_round_seconds.py <文件或目录> -o _smoke/_bench/lrc_round      # 另存一份
    python tools/lrc_round_seconds.py projects/20260917-015336/字幕 \\
        -o projects/20260917-015336/字幕 --ext txt                                 # 每期多出一份 `<期号>.txt`
"""

import argparse
import glob
import os
import re
import shutil
import sys

TS_RE = re.compile(r"\[(\d{1,4}):(\d{1,2})\.(\d+)\]")


def round_stamp(mm, ss, frac):
    """`mm:ss.frac` → 总秒数（四舍五入）。

    整数算法，不碰浮点：把「分秒 + 小数」统一折算成分母 `10**len(frac)` 的分数，
    加半个分母再整除 —— 分母是 10 的正整数次幂，必为偶数，所以这就是 half-up。
    """
    den = 10 ** len(frac)
    whole = (int(mm) * 60 + int(ss)) * den + int(frac)
    return (whole + den // 2) // den


def fmt_sec(total):
    """总秒 → `mm:ss`（分钟补零两位，允许超过 59）。"""
    return "%02d:%02d" % (total // 60, total % 60)


def convert_text(text):
    """逐行替换时间戳。返回 (新文本, 报告字典)。"""
    rep = {"total": 0, "carry": 0, "drop": 0, "minute_carry": 0,
           "lines": 0, "changed_lines": 0, "details": [], "stamps": [], "dup": []}
    out = []
    for lineno, line in enumerate(text.split("\n"), 1):
        rep["lines"] += 1
        if "." not in line or "[" not in line:
            out.append(line)
            continue
        row_changes = []

        def _sub(m):
            mm, ss, frac = m.group(1), m.group(2), m.group(3)
            sec = round_stamp(mm, ss, frac)
            rep["total"] += 1
            old = "[%s:%s.%s]" % (mm, ss, frac)
            if int(frac) * 2 >= 10 ** len(frac):
                rep["carry"] += 1
            else:
                rep["drop"] += 1
            if sec // 60 != int(mm):
                rep["minute_carry"] += 1
            new = "[%s]" % fmt_sec(sec)
            row_changes.append((old, new, sec))
            rep["stamps"].append((lineno, sec, old))
            return new

        converted = TS_RE.sub(_sub, line)
        if row_changes:
            rep["changed_lines"] += 1
            rep["details"].append((lineno, row_changes, line, converted))
        out.append(converted)

    # 取整后相邻戳撞车 / 倒序（同一秒两条歌词，播放器会闪跳）
    for i in range(1, len(rep["stamps"])):
        p_lineno, p_sec, p_old = rep["stamps"][i - 1]
        lineno, sec, old = rep["stamps"][i]
        if sec <= p_sec:
            rep["dup"].append((p_lineno, p_old, p_sec, lineno, old, sec))
    return "\n".join(out), rep


def read_text(path):
    """二进制读 → 记录 BOM／编码，返回 (text, bom)。非 UTF-8 原样回退。"""
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    return raw.decode("utf-8"), bom


def write_text(path, text, bom):
    raw = text.encode("utf-8")
    open(path, "wb").write((b"\xef\xbb\xbf" if bom else b"") + raw)


def collect(targets, all_flag):
    files = []
    for t in targets:
        if os.path.isdir(t):
            files += sorted(glob.glob(os.path.join(t, "**", "*.lrc"), recursive=True))
        elif os.path.isfile(t):
            files.append(t)
        else:
            pat = glob.glob(t)
            files += sorted(pat) if pat else []
    files = list(dict.fromkeys(files))
    if all_flag and not files:
        print("目标里没找到 .lrc")
    return files


def main():
    ap = argparse.ArgumentParser(description="LRC 时间戳 `[mm:ss.xx]` → `[mm:ss]`（四舍五入）")
    ap.add_argument("targets", nargs="+", help="文件、目录或通配符")
    ap.add_argument("--all", action="store_true", help="目录递归（默认已递归，保留兼容）")
    ap.add_argument("--write", action="store_true", help="就地改写（先落 .bak 备份）")
    ap.add_argument("-o", "--outdir", help="输出目录（与 --write 互斥；不改原文件）")
    ap.add_argument("--ext", help="输出扩展名（默认沿用原名，如 --ext txt 得 `1.txt`；需配 -o）")
    ap.add_argument("--show", type=int, default=12, help="报告里展示的逐行样例条数")
    ap.add_argument("--json", help="把逐条明细写成 JSON")
    args = ap.parse_args()

    if args.write and args.outdir:
        print("--write 与 -o 互斥，二选一")
        return 2
    if args.ext and not args.outdir:
        print("--ext 需配 -o 指定输出目录")
        return 2

    files = collect(args.targets, args.all)
    if not files:
        print("没找到可处理的 .lrc")
        return 1

    grand = {"total": 0, "carry": 0, "drop": 0, "minute_carry": 0, "dup": 0}
    all_items = []
    for path in files:
        try:
            text, bom = read_text(path)
        except UnicodeDecodeError as e:
            print("跳过（非 UTF-8）：%s —— %s" % (path, e))
            continue
        new_text, rep = convert_text(text)
        changed = new_text != text
        grand["total"] += rep["total"]
        grand["carry"] += rep["carry"]
        grand["drop"] += rep["drop"]
        grand["minute_carry"] += rep["minute_carry"]
        grand["dup"] += len(rep["dup"])

        print("=" * 96)
        print("%s" % path)
        print("  行 %d ｜ 时间戳 %d ｜ 进位 %d ｜ 舍去 %d ｜ 秒溢出带动分钟 %d ｜ 文本%s"
              % (rep["lines"], rep["total"], rep["carry"], rep["drop"],
                 rep["minute_carry"], "有变" if changed else "无变"))
        for lineno, chs, old_line, new_line in rep["details"][:args.show]:
            pairs = "  ".join("%s→%s" % (o, n) for o, n, _ in chs)
            print("    L%-4d %s" % (lineno, pairs))
        if len(rep["details"]) > args.show:
            print("    …… 其余 %d 行略" % (len(rep["details"]) - args.show))
        if rep["dup"]:
            print("  ⚠ 取整后相邻戳撞车／倒序 %d 处：" % len(rep["dup"]))
            for p_lineno, p_old, p_sec, lineno, old, sec in rep["dup"][:10]:
                print("     L%d %s(%ds) 与 L%d %s(%ds)"
                      % (p_lineno, p_old, p_sec, lineno, old, sec))
        all_items.append({"file": path, "report": {
            k: v for k, v in rep.items() if k not in ("details",)},
            "details": [{"line": ln, "pairs": [(o, n) for o, n, _ in chs]}
                        for ln, chs, _, _ in rep["details"]]})

        if args.outdir:
            os.makedirs(args.outdir, exist_ok=True)
            name = os.path.basename(path)
            if args.ext:
                name = os.path.splitext(name)[0] + "." + args.ext.lstrip(".")
            dst = os.path.join(args.outdir, name)
            write_text(dst, new_text, bom)
            print("  → 写出 %s" % dst)
        elif args.write and changed:
            shutil.copy2(path, path + ".bak")
            write_text(path, new_text, bom)
            print("  → 已就地改写（备份 %s.bak）" % os.path.basename(path))

    print("=" * 96)
    print("合计：%d 个文件 ｜ 时间戳 %d ｜ 进位 %d ｜ 舍去 %d ｜ 带动分钟 %d ｜ 撞车 %d"
          % (len(files), grand["total"], grand["carry"], grand["drop"],
             grand["minute_carry"], grand["dup"]))
    if not args.write and not args.outdir:
        print("（本次**未写盘**，只看报告；要落盘加 --write 或 -o 目录）")
    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=1)
        print("明细 → %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
