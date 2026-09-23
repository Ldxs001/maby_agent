#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Base 变体音色一致性实测 —— 两种模式 + CustomVoice 基线。

背景：CustomVoice 的音色靠一个离散 token（config 里的 spk_id）给，每句现演，
句间音色会漂。Base 变体改用 speaker_encoder 从参考音频抽 2048 维向量，理论上
锁得更死。本脚本量这个「锁得住多少」。

同一批 12 句文本，三种条件：

  custom     CustomVoice + 配置里的音色名（现状基线）
  base_xvec  Base + xvec_only=True（只喂 2048 维向量）
  base_icl   Base + xvec_only=False（参考音频整段进上下文）

指标：F0 中位数（音高，说话人最显著特征）、谱质心（音色明暗）、时长/字速。
主判据：句间 F0 中位数的极差与标准差 —— 越小越像同一个人。

模型分开跑，避免显存打架：
  python _probe_base_clone.py --stage custom
  python _probe_base_clone.py --stage base
  python _probe_base_clone.py --stage report
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tts_service"))

import serve  # noqa: E402  产品真模块，不改

OUT = os.path.join(ROOT, "_smoke", "_clone_probe")
REFGEN = os.path.join(ROOT, "_smoke", "_refgen")
MODELS = os.path.join(ROOT, "tts_service", "models")
MODEL_CUSTOM = os.path.join(MODELS, "Qwen3-TTS-12Hz-1.7B-CustomVoice")
MODEL_BASE = os.path.join(MODELS, "Qwen3-TTS-12Hz-1.7B-Base")

VOICE = {"A": "Vivian", "B": "Serena"}
ASR = {"A": "zh-CN-XiaoxiaoNeural", "B": "zh-CN-YunyangNeural"}

SEED = 12345          # 各条件统一，差异只来自「文本 + 音色机制」
N_LINES = 12

TEXTS = [
    "今天的天气比昨天凉了一些，出门记得加件外套。",
    "这条路径的选择并不复杂，关键是先看清成本。",
    "数据摆在这里，接下来要做的是把它讲清楚。",
    "我们先把基础事实对齐，再讨论后面的方案。",
    "从长期来看，这件事的影响会慢慢显现出来。",
    "关于这个问题，我的看法和你有些不太一样。",
    "会议纪要已经整理完毕，稍后会发到各位邮箱。",
    "这个方案的核心风险在于交付周期太短。",
    "我们需要在两周之内完成第一轮的验证工作。",
    "把复杂的问题拆开看，往往会清楚很多。",
    "这份报告的重点不在结论，而在推理过程。",
    "无论选择哪条路，都要先想清楚代价是什么。",
]


# ---------------------------------------------------------------- 声学指标

def f0_series(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512):
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    x = x - x.mean()
    p = np.max(np.abs(x))
    if p <= 1e-9:
        return np.array([])
    x = x / p
    lo, hi = max(1, int(sr / fmax)), min(frame - 1, int(sr / fmin))
    vals = []
    for s in range(0, len(x) - frame, hop):
        f = x[s:s + frame]
        if np.sqrt(np.mean(f ** 2)) < 0.02:
            continue
        ac = np.correlate(f, f, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if not len(seg):
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] / ac[0] < 0.3:
            continue
        vals.append(sr / k)
    return np.array(vals)


def metrics(x, sr, text):
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    dur = len(x) / float(sr)
    f0 = f0_series(x, sr)
    n = 1 << 12
    if len(x) >= n:
        seg = x[:n] * np.hanning(n)
        mag = np.abs(np.fft.rfft(seg))
        fr = np.fft.rfftfreq(n, 1.0 / sr)
        ssum = float(np.sum(mag))
        cen = float(np.sum(fr * mag) / ssum / (sr / 2.0)) if ssum > 0 else float("nan")
    else:
        cen = float("nan")
    return {
        "dur": round(dur, 3),
        "cps": round(len(text) / dur, 2) if dur > 0 else float("nan"),
        "f0_med": round(float(np.median(f0)), 1) if len(f0) else float("nan"),
        "f0_std": round(float(np.std(f0)), 1) if len(f0) else float("nan"),
        "centroid": round(cen, 4) if cen == cen else float("nan"),
    }


# ---------------------------------------------------------------- 参考音频

def load_refs() -> dict:
    p = os.path.join(REFGEN, "ref.json")
    if not os.path.isfile(p):
        print("  缺参考音频清单：%s" % p)
        print("  先跑 _probe_refgen.py")
        raise SystemExit(2)
    with io.open(p, encoding="utf-8") as fh:
        return json.load(fh)


def pick_ref(refs: dict, spk: str, cid: str = "") -> dict:
    items = refs.get(spk, [])
    if not items:
        raise SystemExit("参考音频里没有说话人 %s" % spk)
    if cid:
        for it in items:
            if it["id"] == cid:
                return it
        raise SystemExit("参考音频里没有 %s" % cid)
    return items[0]


# ---------------------------------------------------------------- 三种条件

def run_custom(eng, lines, audio_dir) -> list:
    rows = []
    for spk in ("A", "B"):
        for i, text in enumerate(lines, 1):
            serve.set_seed(SEED)
            samples, sr = eng.synth_one(text, VOICE[spk], instruct=None,
                                        language="Chinese", seed=SEED)
            m = metrics(samples, sr, text)
            name = "custom_%s_%02d.wav" % (spk, i)
            with open(os.path.join(audio_dir, name), "wb") as fh:
                fh.write(serve.to_wav_bytes(samples, sr))
            m.update({"stage": "custom", "speaker": spk, "line": i, "text": text,
                      "wav": name})
            rows.append(m)
            print("  %s 第%2d句 %5.2fs  F0 %6.1f  质心 %.4f"
                  % (spk, i, m["dur"], m["f0_med"], m["centroid"]))
    return rows


def run_base(model, lines, mode: str, refs: dict, ref_ids: dict, audio_dir) -> list:
    xvec = (mode == "base_xvec")
    rows = []
    for spk in ("A", "B"):
        ref = pick_ref(refs, spk, ref_ids.get(spk, ""))
        ref_audio = os.path.join(REFGEN, ref["wav"])
        for i, text in enumerate(lines, 1):
            serve.set_seed(SEED)
            out = model.generate_voice_clone(
                text=text, language="Chinese",
                ref_audio=ref_audio, ref_text=ref["text"],
                xvec_only=xvec,
                max_new_tokens=serve.max_frames_for(text),
                **serve.SAMPLE_KWARGS)
            samples, sr = serve._split_out(out)
            m = metrics(samples, sr, text)
            name = "%s_%s_%02d.wav" % (mode, spk, i)
            with open(os.path.join(audio_dir, name), "wb") as fh:
                fh.write(serve.to_wav_bytes(samples, sr))
            m.update({"stage": mode, "speaker": spk, "line": i, "text": text,
                      "ref": ref["id"], "wav": name})
            rows.append(m)
            print("  %s 第%2d句 %5.2fs  F0 %6.1f  质心 %.4f"
                  % (spk, i, m["dur"], m["f0_med"], m["centroid"]))
    return rows


# ---------------------------------------------------------------- 汇总

def spread(rows: list, spk: str, key: str) -> dict:
    vals = [r[key] for r in rows
            if r["speaker"] == spk and r[key] == r[key]]
    if not vals:
        return {"n": 0}
    a = np.asarray(vals, dtype=float)
    med = float(np.median(a))
    return {
        "n": len(a),
        "min": round(float(a.min()), 1),
        "max": round(float(a.max()), 1),
        "range": round(float(a.max() - a.min()), 1),
        "std": round(float(a.std()), 1),
        "median": round(med, 1),
        "rel_range_pct": round(float(a.max() - a.min()) / med * 100.0, 1) if med else float("nan"),
    }


def report() -> int:
    stages = {}
    for tag in ("custom", "base_xvec", "base_icl"):
        p = os.path.join(OUT, "%s.json" % tag)
        if os.path.isfile(p):
            with io.open(p, encoding="utf-8") as fh:
                stages[tag] = json.load(fh)

    if not stages:
        print("没有可汇总的结果")
        return 2

    keys = ("f0_med", "f0_std", "centroid", "cps")
    out = {}
    print("=" * 92)
    print("%-12s %-4s %-28s %s" % ("条件", "人", "指标", "极差 / 相对极差 / 标准差"))
    print("=" * 92)
    for tag, rows in stages.items():
        out[tag] = {}
        for spk in ("A", "B"):
            out[tag][spk] = {}
            for k in keys:
                s = spread(rows, spk, k)
                out[tag][spk][k] = s
                if k == "f0_med":
                    print("%-12s %-4s %-28s %7.1f / %5.1f%% / %6.1f"
                          % (tag, spk, "F0中位(Hz)", s["range"], s["rel_range_pct"], s["std"]))
                elif k == "centroid":
                    print("%-12s %-4s %-28s %7.4f / %5.1f%% / %6.4f"
                          % (tag, spk, "谱质心", s["range"], s["rel_range_pct"], s["std"]))
                elif k == "cps":
                    print("%-12s %-4s %-28s %7.2f / %5.1f%% / %6.2f"
                          % (tag, spk, "字/秒", s["range"], s["rel_range_pct"], s["std"]))
                else:
                    print("%-12s %-4s %-28s %7.1f / %5.1f%% / %6.1f"
                          % (tag, spk, "F0句内波动", s["range"], s["rel_range_pct"], s["std"]))
        print("-" * 92)

    with io.open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("产物 %s" % os.path.join(OUT, "summary.json"))
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="custom",
                    choices=["custom", "base", "report"])
    ap.add_argument("--mode", default="both",
                    choices=["both", "xvec", "icl"])
    ap.add_argument("--lines", type=int, default=N_LINES)
    ap.add_argument("--ref-a", default="A_c2", help="说话人 A 用的参考音频 id")
    ap.add_argument("--ref-b", default="B_c2", help="说话人 B 用的参考音频 id")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    audio = os.path.join(OUT, "audio")
    os.makedirs(audio, exist_ok=True)
    lines = TEXTS[:args.lines]

    if args.stage == "report":
        return report()

    if args.stage == "custom":
        print("=" * 78)
        print("基线 · CustomVoice（音色靠 spk_id token）")
        print("=" * 78)
        eng = serve.Engine(MODEL_CUSTOM, "auto", lambda m: print("  " + m))
        t0 = time.time()
        eng.ensure()
        print("  后端 %s · %s · %.1fs" % (eng.backend, eng.device, time.time() - t0))
        rows = run_custom(eng, lines, audio)
        with io.open(os.path.join(OUT, "custom.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print("产物 %s" % os.path.join(OUT, "custom.json"))
        return 0

    print("=" * 78)
    print("Base 变体（音色靠参考音频抽出的 2048 维向量）")
    print("=" * 78)
    from faster_qwen3_tts import FasterQwen3TTS  # type: ignore

    t0 = time.time()
    model = FasterQwen3TTS.from_pretrained(MODEL_BASE, device="cuda")
    print("  加载 %.1fs" % (time.time() - t0))

    refs = load_refs()
    ref_ids = {"A": args.ref_a, "B": args.ref_b}
    print("  参考音频 A=%s  B=%s" % (args.ref_a, args.ref_b))
    modes = ["base_xvec", "base_icl"] if args.mode == "both" else (
        ["base_xvec"] if args.mode == "xvec" else ["base_icl"])

    for mode in modes:
        print()
        print("-" * 78)
        print("条件 %s（xvec_only=%s）" % (mode, mode == "base_xvec"))
        print("-" * 78)
        t1 = time.time()
        rows = run_base(model, lines, mode, refs, ref_ids, audio)
        with io.open(os.path.join(OUT, "%s.json" % mode), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print("  耗时 %.1fs · 产物 %s" % (time.time() - t1, os.path.join(OUT, "%s.json" % mode)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
