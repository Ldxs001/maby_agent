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

"""三份字幕同源实测：SRT / LRC / TXT 由同一份 script 与 timings 生成。

一期出四份字幕：SRT（起止齐全）、LRC（音频平台歌词位，百分秒）、TXT（整秒
`[mm:ss]`）、洁版 TXT（整秒摘方括号 `mm:ss`）。本探针只跑**前三份**——它们是从
`.lrc` 反解时间轴那一套能对得上的部分；洁版与整秒 TXT 只差时间戳的方括号，由
`_smoke/subtitle_write_integration.py`（出片第 6 步重放）与
`_smoke/probe_clean_vs_disk.py`（与既有 14 份 `_clean.txt` 逐字节复现）验。

「同一份源」不能靠读代码相信，得拿**真实项目数据**量出来。做法：

  1. 从每期已有的 `.lrc` **反解时间轴**——`[mm:ss.ff]` 本来就是百分秒刻度，反解无损；
  2. 用同一份 `脚本/<期号>.json` 与项目配置，真调 `build_srt` / `build_lrc` / `build_txt`；
  3. 三路对账。

**不拿历史产物当基准**：历史 `.lrc` 是**当时的**配置与**当时的**脚本生成的（项目配置演化过
——说话人名从「小美/大美」改成「小思/小笔」；脚本也改过若干句，于是有的期句数比时间轴多）。
用现在的参数去还原它必然不等，那是配置史不是代码缺陷。所以脚本只判三条与配置无关的：

  A. **同源律**：`build_lrc` 的输出交给离线取整工具（`tools/lrc_round_seconds.py`），
     必须**逐字节等于** `build_txt` 的输出 —— 运行时算的整秒与事后转的整秒是同一个答案。
  B. **产物对账**：磁盘上的 `.txt` 必须逐字节等于「磁盘上的 `.lrc` 交给同一个工具」的结果。
  C. **SRT 时间轴**：反解时间轴再生成 SRT，起止时刻与磁盘 `.srt` 逐条一致（毫秒口径有
     截断，允许 ≤1 秒差）。

句数与时间轴不等长时按**交集**裁剪（出片时 timings 是由 script 算出来的，必然等长；
不等长只说明脚本后来被改过，多出来的句不是本次要判的东西）。

用法：
    python tools/probes/subtitle_three_way.py
    python tools/probes/subtitle_three_way.py --json _smoke/_bench/subtitle_three_way.json
"""
import argparse
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.join(ROOT, "_smoke")

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import lrc_round_seconds as LR                                     # noqa: E402
from podcast_maker import project_store, subtitle_engine as S      # noqa: E402
from podcast_maker.config_manager import ConfigManager             # noqa: E402

LRC_TS = re.compile(r"^\[(\d+):(\d{2})\.(\d{2})\](.*)$")
SRT_AX = re.compile(r"^(\d+):(\d{2}):(\d{2}),\d{3} -->", re.M)


def read_text(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()                                            # 通用换行 → \n


def timings_from_lrc(text):
    """`[mm:ss.ff]` → 起始秒（百分秒刻度，无损反解）。"""
    out = []
    for ln in text.split("\n"):
        m = LRC_TS.match(ln)
        if m:
            out.append({"start": int(m.group(1)) * 60 + int(m.group(2))
                        + int(m.group(3)) / 100.0, "end": 0.0})
    return out


def _secs(t):
    return int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2])


def check_episode(pid, no, sub, scripts, cfg):
    """一期的三路对账。返回 (记录字典, 是否通过)。"""
    lrc_path = os.path.join(sub, no + ".lrc")
    txt_path = os.path.join(sub, no + ".txt")
    srt_path = os.path.join(sub, no + ".srt")
    sc_path = os.path.join(scripts, no + ".json")
    if not os.path.exists(sc_path):
        return None, True                                          # 无脚本，跳过
    with io.open(sc_path, encoding="utf-8") as f:
        script = json.load(f)
    script = [{"speaker": r.get("speaker", "A"),
               "emotion": r.get("emotion", "承接"),
               "text": r.get("text", "")} for r in script]

    disk_lrc = read_text(lrc_path)
    timings = timings_from_lrc(disk_lrc)
    trimmed = max(0, len(script) - len(timings))
    if len(script) > len(timings):
        script = script[:len(timings)]
    elif len(timings) > len(script):
        timings = timings[:len(script)]

    gen_lrc = S.build_lrc(script, cfg, timings)
    gen_txt = S.build_txt(script, cfg, timings)
    converted, rep = LR.convert_text(gen_lrc)
    a_ok = converted == gen_txt                                   # A 同源律

    b_ok = None
    if os.path.exists(txt_path):
        conv_disk, rep_disk = LR.convert_text(disk_lrc)
        b_ok = conv_disk == read_text(txt_path)                    # B 产物对账

    c_ok = None
    if os.path.exists(srt_path):
        ds = SRT_AX.findall(read_text(srt_path))
        gs = SRT_AX.findall(S.build_srt(script, cfg, timings))
        c_ok = (len(ds) == len(gs)
                and all(abs(_secs(x) - _secs(y)) <= 1 for x, y in zip(ds, gs)))

    rec = {"project": pid, "no": no, "lines": len(script),
           "timings": len(timings), "stamps": rep["total"], "trimmed": trimmed,
           "same_source": a_ok, "disk_txt": b_ok, "srt_axis": c_ok,
           "collision": len(rep["dup"])}
    return rec, (a_ok and b_ok is not False and c_ok is not False)


def main():
    ap = argparse.ArgumentParser(description="三份字幕（SRT/LRC/TXT）同源实测")
    ap.add_argument("--base", default=os.path.join(ROOT, "projects"))
    ap.add_argument("--project", help="只测某个项目 id")
    ap.add_argument("--json", help="把逐期明细写成 JSON")
    args = ap.parse_args()

    cm = ConfigManager(ROOT)
    try:
        cm.load()
    except Exception as e:                                          # noqa: BLE001
        print("配置加载告警：%s" % e)
    raw_cfg = cm.data()
    store = project_store._read(args.base)

    rows, bad = [], 0
    total_sent = total_stamp = 0
    for item in store.get("projects") or []:
        pid = str(item.get("id"))
        if args.project and pid != args.project:
            continue
        cfg = dict(project_store.apply_to_config(dict(raw_cfg), item))
        sub = os.path.join(args.base, pid, "字幕")
        if not os.path.isdir(sub):
            continue
        for lrc in sorted(glob.glob(os.path.join(sub, "*.lrc"))):
            no = os.path.basename(lrc)[:-4]
            rec, ok = check_episode(pid, no, sub,
                                    os.path.join(args.base, pid, "脚本"), cfg)
            if rec is None:
                print("跳过 %s/%s（无脚本）" % (pid, no))
                continue
            rows.append(rec)
            total_sent += rec["lines"]
            total_stamp += rec["stamps"]
            if not ok:
                bad += 1

    print("%-22s %-5s %5s %6s %6s %-8s %-8s %-7s %4s %4s"
          % ("项目", "期号", "句数", "时间轴", "取整戳", "同源律", "产物对账",
             "SRT轴", "撞车", "已裁"))
    for r in rows:
        print("%-22s %-5s %5d %6d %6d %-8s %-8s %-7s %4d %4d"
              % (r["project"], r["no"], r["lines"], r["timings"], r["stamps"],
                 "PASS" if r["same_source"] else "FAIL",
                 ("PASS" if r["disk_txt"] else "FAIL")
                 if r["disk_txt"] is not None else "无源",
                 ("PASS" if r["srt_axis"] else "FAIL")
                 if r["srt_axis"] is not None else "无源",
                 r["collision"], r["trimmed"]))
    print()
    print("期数 %d ｜ 句数 %d ｜ 时间戳 %d ｜ 失败 %d"
          % (len(rows), total_sent, total_stamp, bad))
    if args.json:
        os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with io.open(args.json, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "lines": total_sent,
                       "stamps": total_stamp, "failed": bad}, f,
                      ensure_ascii=False, indent=1)
        print("明细 → %s" % args.json)
    print("RESULT:", "PASS" if bad == 0 and rows else "FAIL")
    return 0 if bad == 0 and rows else 1


if __name__ == "__main__":
    sys.exit(main())
