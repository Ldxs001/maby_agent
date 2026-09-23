# -*- coding: utf-8 -*-
"""贪心档完整对照：把 talker 与 predictor 两处采样分开关，看各关掉哪一处起了作用。

缘起
----
上一轮只测了 predictor 一处，且贪心档只跑了 1 句 2 种子。听感反馈「贪心最好」，
但样本不足，且恰好没覆盖长句 —— 而长句正是贪心最容易退化的地方（argmax 容易
落进重复循环，一路念到帧数上限）。本脚本补全 3 句 × 4 种子 × 四档配置。

四档
----
    ref     主路采样 0.4 + 子码本采样 0.9   生产现状
    g_pred  主路采样 0.4 + 子码本贪心       听感所指的「贪心」
    g_talk  主路贪心     + 子码本采样 0.9   只关主路，隔离它的作用
    g_all   主路贪心     + 子码本贪心       全关，最确定的形态

两条硬判据
----------
  A. g_talk 档换种子，输出是否完全相同？
     主路已关随机、只剩子码本那一处采样。若跨种子输出完全相同，就证明
     CUDA 图内的采样与 torch 随机种子无关（图 replay 会复位图内 RNG）——
     即上一轮那条推论可以直接实证。
  B. 长句是否退化？
     统计主路 token 的连续重复段与 3-gram 最大重数，配合帧数上限与语速。

跑法：cd podcast_maker && tts_service/.venv/Scripts/python.exe tools/probes/probe_greedy_voice.py
产物：_smoke/_greedy_probe/*.wav + probe.json + listen.html
"""
from __future__ import annotations

import base64
import collections
import gc
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np
import torch

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import probe_predictor_temp as PT  # noqa: E402  复用指标与建图逻辑，保证口径一致

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

OUTDIR = os.path.join(HERE, "_greedy_probe")
SPEAKER = PT.SPEAKER
TEXTS = PT.TEXTS
SEEDS = PT.SEEDS

# (代号, 中文标签, 主路温度, 主路是否采样, 子码本温度, 子码本是否采样)
ARMS = [
    ("ref",    "现状：两处都采样", 0.4, True,  0.9, True),
    ("g_pred", "关子码本（听感所指）", 0.4, True,  0.9, False),
    ("g_talk", "只关主路",          0.4, False, 0.9, True),
    ("g_all",  "两处都关（全贪心）", 0.4, False, 0.9, False),
]

TALK_TOKENS: list[int] = []   # 主路（talker）每一步采出的第一个码本 token


def install_talk_hook() -> None:
    """拦主路的采样调用。

    子码本在 CUDA 图里，不经过 Python 的 sample_logits；所以这个钩子只看得见
    主路 —— 正好用来单独统计主路的重复模式。
    """
    import faster_qwen3_tts.generate as FG

    orig = FG.sample_logits

    def hooked(logits, **kw):
        out = orig(logits, **kw)
        try:
            TALK_TOKENS.append(int(out.reshape(-1)[0].item()))
        except Exception:  # noqa: BLE001
            pass
        return out

    FG.sample_logits = hooked


def repeat_stats(tokens: list[int]) -> dict:
    """主路 token 的重复模式：卡住会让这两项同时飙升。"""
    n = len(tokens)
    if n < 3:
        return {"steps": n, "adj_same": 0, "max_run": 0, "top3gram": 0}
    adj = sum(1 for i in range(1, n) if tokens[i] == tokens[i - 1])
    run = best = 1
    for i in range(1, n):
        run = run + 1 if tokens[i] == tokens[i - 1] else 1
        best = max(best, run)
    grams = collections.Counter(tuple(tokens[i:i + 3]) for i in range(n - 2))
    return {"steps": n, "adj_same": adj, "max_run": best,
            "top3gram": max(grams.values())}


def md5_of(a) -> str:
    return hashlib.md5(np.asarray(a, dtype=np.float32).tobytes()).hexdigest()[:12]


def main() -> None:
    from faster_qwen3_tts import FasterQwen3TTS

    os.makedirs(OUTDIR, exist_ok=True)
    print("加载模型 …", flush=True)
    backend = FasterQwen3TTS.from_pretrained(PT.MODEL, device="cuda")
    install_talk_hook()
    print("就绪\n", flush=True)

    def gen(text: str, seed: int, t_temp: float, t_sample: bool):
        TALK_TOKENS.clear()
        PT.RUNLOG.clear()
        PT.set_seed(seed)
        wavs, sr = backend.generate_custom_voice(
            text=text, speaker=SPEAKER, language="Chinese",
            max_new_tokens=PT.max_frames_for(text),
            temperature=t_temp, top_k=50, top_p=1.0,
            do_sample=t_sample, repetition_penalty=1.05)
        a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
        if hasattr(a, "cpu"):
            a = a.cpu().numpy()
        return np.asarray(a, dtype=np.float32).reshape(-1), sr

    # 首次生成：触发默认图与主路图的捕获，消掉首推理差异
    print("预热 …", flush=True)
    gen(TEXTS[0][1], 0, 0.4, True)

    rows: list[dict] = []
    arm_meta: dict[str, dict] = {}

    for arm, label, t_temp, t_sample, p_temp, p_sample in ARMS:
        print("── %s ｜ %s ──" % (arm, label), flush=True)
        t0 = time.time()
        pg = PT.build_predictor(backend, p_temp, p_sample)
        PT.install_predictor(backend, pg)
        arm_meta[arm] = {"label": label, "talker_temp": t_temp,
                         "talker_sample": t_sample, "pred_temp": p_temp,
                         "pred_sample": p_sample,
                         "graph_temperature": backend.predictor_graph.temperature,
                         "graph_do_sample": backend.predictor_graph.do_sample,
                         "setup_s": round(time.time() - t0, 2)}
        print("   图内 temperature=%s do_sample=%s · 用时 %.1fs"
              % (arm_meta[arm]["graph_temperature"],
                 arm_meta[arm]["graph_do_sample"], arm_meta[arm]["setup_s"]), flush=True)

        for tag, text in TEXTS:
            for seed in SEEDS:
                t1 = time.time()
                a, sr = gen(text, seed, t_temp, t_sample)
                el = time.time() - t1
                rs = repeat_stats(list(TALK_TOKENS))
                name = "%s_%s_s%d.wav" % (tag, arm, seed)
                PT.write_wav(os.path.join(OUTDIR, name), a, sr)
                m = PT.measure(a, sr, len(text))
                hit = rs["steps"] >= PT.max_frames_for(text)
                rec = {"tag": tag, "text": text, "arm": arm, "label": label, "seed": seed,
                       "wav": name, "gen_s": round(el, 2), "md5": md5_of(a),
                       "hit_cap": hit, **rs, **m}
                rows.append(rec)
                print("   %-24s F0 %6.1f±%-5.1f 质心 %6.0f 时长 %5.2fs 步数 %4d "
                      "相邻同 %2d 最长连 %d 三连 %d%s"
                      % (name, m["f0_mean"], m["f0_std"], m["centroid"], m["dur"],
                         rs["steps"], rs["adj_same"], rs["max_run"], rs["top3gram"],
                         "  顶上限" if hit else ""), flush=True)
        print(flush=True)

    # ---- 硬判据：跨种子的波形是否全同 ---- #
    print("=" * 100, flush=True)
    print("判据 A · 同一档内换种子，输出是否相同（相同说明剩下的随机性为零）", flush=True)
    for arm, label, _a, _b, _c, _d in ARMS:
        for tag, _t in TEXTS:
            ms = [r["md5"] for r in rows if r["arm"] == arm and r["tag"] == tag]
            same = len(set(ms)) == 1
            print("   %-8s %-4s 波形 %s（%s）" % (arm, tag, "全同" if same else "不同",
                                                " ".join(ms)), flush=True)
    print(flush=True)

    # ---- 汇总 ---- #
    print("按档汇总（3 句 × 4 种子 = 12 条）", flush=True)
    print("%-28s %-24s %-22s %-20s %s"
          % ("档位", "F0 中位（跨种子极差）", "F0 波动均值", "质心（跨种子极差）", "退化"), flush=True)
    summary = {}
    for arm, label, _a, _b, _c, _d in ARMS:
        sub = [r for r in rows if r["arm"] == arm]
        per_tag = []
        for tag, _t in TEXTS:
            v = [r for r in sub if r["tag"] == tag]
            per_tag.append(max(r["f0_mean"] for r in v) - min(r["f0_mean"] for r in v))
        cn = []
        for tag, _t in TEXTS:
            v = [r for r in sub if r["tag"] == tag]
            cn.append(max(r["centroid"] for r in v) - min(r["centroid"] for r in v))
        f0s = [r["f0_std"] for r in sub]
        summary[arm] = {
            "label": label,
            "f0_range_mean": round(float(np.mean(per_tag)), 1),
            "f0_range_max": round(max(per_tag), 1),
            "centroid_range_mean": round(float(np.mean(cn)), 0),
            "centroid_range_max": round(max(cn), 0),
            "f0_std_mean": round(float(np.mean(f0s)), 1),
            "dur_range": round(max(r["dur"] for r in sub) - min(r["dur"] for r in sub), 2),
            "max_run": max(r["max_run"] for r in sub),
            "adj_same_mean": round(float(np.mean([r["adj_same"] for r in sub])), 1),
            "cap_hits": sum(1 for r in sub if r["hit_cap"]), "n": len(sub),
        }
        s = summary[arm]
        print("%-28s %-24s %-22s %-20s 顶上限 %d/%d"
              % (label, "%.1f（最大 %.1f）" % (s["f0_range_mean"], s["f0_range_max"]),
                 "%.1f" % s["f0_std_mean"],
                 "%.0f（最大 %.0f）" % (s["centroid_range_mean"], s["centroid_range_max"]),
                 s["cap_hits"], s["n"]), flush=True)

    print("\n主路重复模式（越长越像卡住；adj_same 是相邻两步同 token 的次数）", flush=True)
    for arm, label, _a, _b, _c, _d in ARMS:
        s = summary[arm]
        print("   %-28s 最长连续 %d 步 · 相邻同 均 %.1f 次/句" % (label, s["max_run"], s["adj_same_mean"]), flush=True)

    out = {"speaker": SPEAKER, "arms": arm_meta, "seeds": SEEDS,
           "summary": summary, "rows": rows,
           "note": "子码本经重建 CUDA 图切换，未改动库源码"}
    jp = os.path.join(OUTDIR, "probe.json")
    with io.open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n完成 · %s" % jp, flush=True)


if __name__ == "__main__":
    main()
