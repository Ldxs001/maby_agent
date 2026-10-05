# -*- coding: utf-8 -*-
"""三路推理路径速度基准 · CUDA 图 / 普通 GPU 前向 / 纯 CPU。

问题
----
faster-qwen3-tts 拿 CUDA 图换速度，代价是子码本那一路的采样参数被烧进图里、
无法从外部改（`generate_custom_voice` 签名里根本没有 subtalker_* 四个参数）。
官方 `qwen_tts` 包没有 CUDA 图，控制点是全的，但慢。

那到底慢多少？本脚本用**同一份权重、同一句文本、同一组采样参数**，把三条路各跑一遍。

三路
----
    cuda_graph   faster_qwen3_tts，device=cuda，backend=torch（CUDA 图捕获）   ← 现状
    cuda_eager   qwen_tts 官方包，device_map=cuda:0，不走图                  ← GPU 无图基线
    cpu          qwen_tts 官方包，device_map=cpu，float32                    ← 用户问的那条

计时口径
----
    load_secs   从调用 from_pretrained 到返回
    warm_secs   第一句（含图捕获 / warmup），单列不混入稳态
    steady_*    后续各句耗时；RTF = 生成耗时 / 音频时长，<1 即快于实时

用法
----
    python tools/probes/probe_cpu_vs_cuda.py --path cpu --texts s1
    python tools/probes/probe_cpu_vs_cuda.py --path cuda_graph --texts s1,s2,s3
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

import numpy as np
import torch

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
OUT = os.path.join(HERE, "_speed_probe")

SPEAKER = "Serena"

#: 采样参数：与产品侧（serve.py）保持一致，两侧同参才能比。
GEN = dict(temperature=0.4, top_k=50, top_p=1.0, do_sample=True, repetition_penalty=1.05)

TEXTS = {
    "s1": "我们先从原理讲起。",
    "s2": "这套流程的核心，是把随机性关到最后一步。",
    "s3": "它不是更聪明的模型，只是把约束写在了代码里。",
}


def max_tokens(text: str) -> int:
    """与 serve.py 同口径：按字数封顶，防退化。"""
    return int(min(2048, max(120, len(text) * 8)))


def audio_secs(wav: np.ndarray, sr: int) -> float:
    return len(wav) / float(sr)


def load(path: str):
    t0 = time.time()
    if path == "cuda_graph":
        from faster_qwen3_tts import FasterQwen3TTS
        eng = FasterQwen3TTS.from_pretrained(MODEL, device="cuda",
                                             dtype=torch.bfloat16)
    elif path == "cuda_eager":
        from qwen_tts import Qwen3TTSModel
        eng = Qwen3TTSModel.from_pretrained(MODEL, device_map="cuda:0",
                                            dtype=torch.bfloat16)
    elif path == "cpu":
        from qwen_tts import Qwen3TTSModel
        eng = Qwen3TTSModel.from_pretrained(MODEL, device_map="cpu",
                                            dtype=torch.float32)
    else:
        raise SystemExit("未知 path: %s" % path)
    return eng, round(time.time() - t0, 2)


def gen_once(eng, path, text, seed):
    torch.manual_seed(seed)
    kw = dict(GEN)
    kw["max_new_tokens"] = max_tokens(text)
    t0 = time.time()
    wavs, sr = eng.generate_custom_voice(
        text=text, speaker=SPEAKER, language="Chinese",
        instruct=None, **kw)
    dt = time.time() - t0
    wav = np.asarray(wavs[0], dtype=np.float32)
    return dt, wav, sr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True,
                    choices=["cuda_graph", "cuda_eager", "cpu"])
    ap.add_argument("--texts", default="s1", help="逗号分隔的文本键，如 s1,s2,s3")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每句重复生成次数；首次含图捕获/冷启动，后几次为稳态")
    ap.add_argument("--tag", default="", help="结果文件后缀，便于分次跑")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    keys = [k.strip() for k in args.texts.split(",") if k.strip()]
    torch.set_num_threads(torch.get_num_threads())

    print("=" * 78)
    print("路径 %s | 模型 %s" % (args.path, os.path.basename(MODEL)))
    print("torch %s | cuda %s | 线程 %d"
          % (torch.__version__, torch.cuda.is_available(), torch.get_num_threads()))
    print("=" * 78, flush=True)

    eng, load_secs = load(args.path)
    print("加载耗时 %.2fs" % load_secs, flush=True)

    rows = []
    for i, key in enumerate(keys):
        text = TEXTS[key]
        # 种子：与产品侧同源 —— sha256(speaker + text)[:4]，两侧同种
        h = __import__("hashlib").sha256(("%s\x00%s" % (SPEAKER, text)).encode()).digest()
        seed = int.from_bytes(h[:4], "big")
        n_tok = max_tokens(text)
        for rnd in range(1, args.repeat + 1):
            print("[%d/%d] %s 「%s」 max_new_tokens=%d seed=%d 第%d次 …"
                  % (i + 1, len(keys), key, text, n_tok, seed, rnd), flush=True)
            dt, wav, sr = gen_once(eng, args.path, text, seed)
            dur = audio_secs(wav, sr)
            rtf = dt / dur if dur else 0.0
            rows.append({"key": key, "round": rnd, "text": text,
                         "secs": round(dt, 2),
                         "audio_secs": round(dur, 3), "rtf": round(rtf, 3),
                         "max_new_tokens": n_tok, "seed": seed,
                         "samples": int(wav.size), "sr": int(sr)})
            print("      → %.2fs 生成 %.2fs 音频 · RTF %.2f · %d Hz"
                  % (dt, dur, rtf, sr), flush=True)

    res = {"path": args.path, "model": os.path.basename(MODEL),
           "torch": torch.__version__, "cuda": torch.cuda.is_available(),
           "threads": torch.get_num_threads(), "speaker": SPEAKER,
           "repeat": args.repeat,
           "gen_params": GEN, "load_secs": load_secs, "rows": rows}
    suffix = ("_" + args.tag) if args.tag else ""
    fp = os.path.join(OUT, "speed_%s%s.json" % (args.path, suffix))
    with io.open(fp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("\n结果写入 %s" % fp)

    if rows:
        first = [r for r in rows if r.get("round", 1) == 1]
        steady = [r for r in rows if r.get("round", 1) > 1]
        print("\n小结：加载 %.1fs" % load_secs)
        if first:
            print("  首次（含图捕获/冷启动）：%s → 平均 %.2fs/句"
                  % (["%.2f" % r["secs"] for r in first],
                     sum(r["secs"] for r in first) / len(first)))
        if steady:
            print("  稳态（第 2 次起）    ：%s → 平均 %.2fs/句 · 平均 RTF %.2f"
                  % (["%.2f" % r["secs"] for r in steady],
                     sum(r["secs"] for r in steady) / len(steady),
                     sum(r["rtf"] for r in steady) / len(steady)))


if __name__ == "__main__":
    main()
