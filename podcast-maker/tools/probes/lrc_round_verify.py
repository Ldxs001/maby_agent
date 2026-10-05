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

"""LRC 时间戳取整的**验收** —— 实现交叉复算 ＋ 存量产物对账。

`tools/lrc_round_seconds.py` 把它写的每个戳送两遍：一遍走 `round_stamp`（整数半加），
一遍走独立实现（`Decimal` + `ROUND_HALF_UP`）。两条路必须逐戳同 —— 单看一种实现，
写错方向（银行家舍入、先分后秒取整）也看不出来。

四类断言：

    A 交叉复算   与独立实现逐戳一致（含 `[00:02.50]` 这类 half-up 边界）
    B 字符守恒   去掉时间戳后新旧文本逐字符相同（一个字没动）
    C 结构不变   `\\r\\n` 数、BOM、末尾换行、方括号总数不变
    D 幂等       对已取整的文本再跑一遍不产生变化

用法：
    python tools/probes/lrc_round_verify.py                       # 扫全项目 .lrc（A–D）
    python tools/probes/lrc_round_verify.py --txt                 # 另加：.lrc ↔ .txt 产物对账
    python tools/probes/lrc_round_verify.py --file <某个.lrc>
"""

import argparse
import glob
import os
import re
import sys
from decimal import Decimal, ROUND_HALF_UP

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

import lrc_round_seconds as L  # noqa: E402

OLD = re.compile(r"\[(\d{1,4}):(\d{1,2})\.(\d+)\]")
NEW = re.compile(r"\[(\d{1,4}):(\d{1,2})\]")


def ref_round(mm, ss, frac):
    """独立实现：Decimal half-up（与整数版互不依赖，专门用来对账）。"""
    val = Decimal(int(mm) * 60 + int(ss)) + Decimal(int(frac)) / (10 ** len(frac))
    return int(val.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def check_impl(path):
    """A–D 四类断言。返回 (四个布尔, 报告字典, 新旧文本)。

    BOM 不在这里查：`convert_text` 只吃纯文本，BOM 的保留由落盘那一侧负责，
    归 `check_product` 对 `.lrc ↔ .txt` 逐份比。
    """
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    new, rep = L.convert_text(text)

    olds = OLD.findall(text)
    news = NEW.findall(new)
    a_ok = len(olds) == len(news) == rep["total"] and all(
        "%02d:%02d" % (int(nm), int(ns))
        == "%02d:%02d" % (ref_round(m, s, f) // 60, ref_round(m, s, f) % 60)
        for (m, s, f), (nm, ns) in zip(olds, news))
    b_ok = OLD.sub("", text) == NEW.sub("", new)
    c_ok = (text.count("\r\n") == new.count("\r\n")
            and text.endswith("\n") == new.endswith("\n")
            and text.count("[") == new.count("[")
            and text.count("]") == new.count("]"))
    d_ok = L.convert_text(new)[0] == new
    return (a_ok, b_ok, c_ok, d_ok), rep, (text, new, bom)


def check_product(lrc_path, txt_path):
    """存量 `.txt` 产物对账：戳数、守恒、格式、换行、BOM 五项。"""
    a = open(lrc_path, "rb").read()
    b = open(txt_path, "rb").read()
    ta, tb = a.decode("utf-8"), b.decode("utf-8")
    return {
        "戳数": len(OLD.findall(ta)) == len(NEW.findall(tb)),
        "守恒": OLD.sub("", ta) == NEW.sub("", tb),
        "格式": len(re.findall(r"\[", tb)) == len(NEW.findall(tb)),
        "换行": (a.count(b"\r\n") == b.count(b"\r\n")
                 and a.endswith(b"\n") == b.endswith(b"\n")),
        "BOM": a.startswith(b"\xef\xbb\xbf") == b.startswith(b"\xef\xbb\xbf"),
    }


def main():
    ap = argparse.ArgumentParser(description="LRC 取整验收")
    ap.add_argument("--file", help="只查某个 .lrc")
    ap.add_argument("--txt", action="store_true", help="另做 .lrc ↔ .txt 产物对账")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 条的 before→after")
    args = ap.parse_args()

    files = ([args.file] if args.file
             else sorted(glob.glob(os.path.join(ROOT, "projects", "*", "字幕", "*.lrc"))))
    if not files:
        print("没找到 .lrc")
        return 1

    bad = 0
    tot = 0
    print("%-6s %-44s %-18s %s" % ("结果", "文件", "行/戳", "A复算 B守恒 C结构 D幂等"))
    for p in files:
        flags, rep, (old, new, bom) = check_impl(p)
        ok = all(flags)
        tot += rep["total"]
        if not ok:
            bad += 1
        print("%-6s %-44s %-9s %s"
              % ("PASS" if ok else "FAIL", os.path.relpath(p, ROOT),
                 "%d/%d" % (rep["lines"], rep["total"]),
                 "  ".join("%s=%s" % (n, v) for n, v in zip("ABCD", flags))))
        if args.show or not ok:
            for lineno, chs, _o, _n in rep["details"][:args.show or 3]:
                print("        L%-4d %s" % (lineno, "  ".join("%s→%s" % (a, b) for a, b, _ in chs)))

    print()
    print("实现验收：文件 %d ｜ 时间戳 %d ｜ 失败 %d" % (len(files), tot, bad))

    if args.txt:
        tb = 0
        pairs = 0
        print()
        print("%-6s %-44s %s" % ("结果", "产物", "戳数 守恒 格式 换行 BOM"))
        for p in files:
            txt = p[:-4] + ".txt"
            if not os.path.exists(txt):
                continue
            pairs += 1
            chk = check_product(p, txt)
            ok = all(chk.values())
            if not ok:
                tb += 1
            print("%-6s %-44s %s" % ("PASS" if ok else "FAIL",
                                     os.path.relpath(txt, ROOT),
                                     " ".join("%s=%s" % (k, v) for k, v in chk.items())))
        print()
        print("产物对账：%d 对 ｜ 失败 %d" % (pairs, tb))
        bad += tb

    print()
    print("RESULT: %s" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
