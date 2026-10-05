# -*- coding: utf-8 -*-
"""多维度重测：已存波形，零额外合成。

为什么换指标：上一轮只用「末尾 30% 的线性斜率」和「最后 3 帧中位 − 全句中位」
两个量。但中文疑问句的两个音高线索都不是线性斜率：

  * yes/no 问句（「…吗？」）末字是**轻声**，F0 天然偏低，末 3 帧中位必然为负 ——
    拿它当「有没有上扬」的判据，等于用错了尺。
  * 汉语疑问真正的感知线索是**音域扩大 + 整体抬高**：句末音节的 H 更高、L 更低，
    范围拉开；同时整句基频上移。

所以这一轮补五个量：

  med          全句 F0 中位（半音）—— 整体音高
  rng          全句 F0 的 p90−p10 范围（半音）—— 音域宽窄
  tail_peak    末尾 20% 的 F0 最高点 − 全句中位 —— 句末抬高幅度
  tail_rng     末尾 20% 的 F0 范围（半音）—— 句末音域
  end10_vs_25  最后 10% 的中位 − 倒数 10%~25% 的中位 —— 末段有没有翘头
"""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import io
import json
import os
import statistics as st

import numpy as np

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")

_spec = importlib.util.spec_from_file_location(
    "_probe_intonation", os.path.join(HERE, "_probe_intonation.py"))
PI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PI)


def rich(path):
    """一句的多维音高画像。"""
    x, sr = PI.read_wav(path)
    ts, fs = PI.f0_track(x, sr)
    if fs.size < 8:
        return None
    s = PI.semitone(fs)                     # 半音，相对 200 Hz
    med = float(np.median(s))
    p10, p90 = float(np.percentile(s, 10)), float(np.percentile(s, 90))
    n_tail = max(3, int(round(s.size * 0.20)))
    tail = s[-n_tail:]
    n10 = max(2, int(round(s.size * 0.10)))
    end10 = float(np.median(s[-n10:]))
    prev = s[-int(round(s.size * 0.25)):-n10] if s.size >= 8 else s[:0]
    prev_med = float(np.median(prev)) if prev.size else med
    return {
        "frames": int(s.size),
        "seconds": float(ts[-1] - ts[0]) if ts.size else 0.0,
        "f0_med_hz": float(np.median(fs)),
        "med": med,
        "rng": p90 - p10,
        "tail_peak": float(np.max(tail)) - med,
        "tail_rng": float(np.percentile(tail, 90) - np.percentile(tail, 10)),
        "end10_vs_25": end10 - prev_med,
        "slope": PI.intonation(x, sr)["slope"] if PI.intonation(x, sr) else 0.0,
        "delta": PI.intonation(x, sr)["delta"] if PI.intonation(x, sr) else 0.0,
    }


KEYS = ("med", "rng", "tail_peak", "tail_rng", "end10_vs_25")
LABEL = {"med": "整体音高", "rng": "音域", "tail_peak": "句末抬高",
         "tail_rng": "句末音域", "end10_vs_25": "末段翘头",
         "slope": "旧:斜率", "delta": "旧:落差"}


def summarize(rows, keys=KEYS):
    out = {}
    for k in keys:
        vals = [r[k] for r in rows if r]
        out[k] = float(st.median(vals)) if vals else 0.0
    out["n"] = len([r for r in rows if r])
    return out


def show(title, groups):
    print("=" * 78)
    print(title)
    print("=" * 78)
    hdr = "%-22s %3s" % ("分组", "n")
    for k in KEYS:
        hdr += " %10s" % LABEL[k]
    print(hdr)
    print("-" * len(hdr))
    for name, rows in groups:
        s = summarize(rows)
        line = "%-22s %3d" % (name, s["n"])
        for k in KEYS:
            line += " %+10.2f" % s[k]
        print(line)
    print()


def load_ab():
    """A/B 主实验的已存波形，按条件分组。"""
    base = os.path.join(HERE, "_ab_out")
    groups = []
    for d in sorted(glob.glob(os.path.join(base, "*"))):
        if not os.path.isdir(d):
            continue
        rows = [rich(p) for p in sorted(glob.glob(os.path.join(d, "*.wav")))]
        groups.append((os.path.basename(d), rows))
    return groups


def load_field():
    """现场 302 句，按句尾标点分组 —— 这是「模型无指令时的天然行为」基准。"""
    audio = os.path.join(PI.PROJ, "过程", "1", "audio")
    script = json.load(io.open(PI.SCRIPT, encoding="utf-8"))
    lines = script if isinstance(script, list) else (script.get("lines") or [])
    q, stm = [], []
    for i, item in enumerate(lines):
        p = os.path.join(audio, "%04d_%s.wav" % (i, item.get("speaker", "?")))
        if not os.path.isfile(p):
            continue
        r = rich(p)
        if not r:
            continue
        t = (item.get("text") or "").rstrip()
        (q if t.endswith("？") else stm).append(r)
    return [("现场 疑问句", q), ("现场 陈述句", stm)]


if __name__ == "__main__":
    print()
    print("注：所有量都以半音为单位，相对 200 Hz。")
    print()
    show("一、模型无指令时的天然行为（现场 302 句真实产物）", load_field())
    show("二、A/B 各条件的句尾画像（同一批 8 句疑问文本）", load_ab())

    out = {"field": {n: summarize(r) for n, r in load_field()},
           "ab": {n: summarize(r) for n, r in load_ab()}}
    with io.open(os.path.join(HERE, "_intonation_rich.json"), "w",
                 encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("写入 _smoke/_intonation_rich.json")
