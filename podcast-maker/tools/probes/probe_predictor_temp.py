# -*- coding: utf-8 -*-
"""探测：PredictorGraph（15 个子码本）的采样温度能不能在运行时改，改了有没有用。

背景
----
Qwen3-TTS 有两处采样：talker（第 1 码本）与 code predictor（另外 15 个子码本）。
前者走 generate_custom_voice(temperature=...) 参数，服务端能传；
后者走 PredictorGraph，温度在 from_pretrained() 构造图时写死 0.9，
并被录进 CUDA 图 —— 服务端传的 0.4 到不了这一路。

本脚本**不改库源码**，只做一件事：重建一个带目标温度的 PredictorGraph，
capture 之后挂回 backend.predictor_graph（fast_generate 每步都从 self 取，
所以替换即刻生效）。据此回答四个问题：

  1. 能不能改 —— 重建后输出是否真的与 0.9 不同
  2. 改了有没有用 —— 跨种子的音高/音色漂移是否收窄
  3. 代价 —— 退化（跑满帧数上限）、时长异常是否上升
  4. 图内采样是不是确定性的 —— 同输入重复跑是否同输出
     （这条决定「跨种子差异」到底该归给谁）

跑法：cd podcast_maker && tts_service/.venv/Scripts/python.exe tools/probes/probe_predictor_temp.py
产物：_smoke/_pred_temp_probe/*.wav + probe.json + 控制台表格
"""
from __future__ import annotations

import gc
import hashlib
import io
import json
import os
import sys
import time
import wave

import numpy as np
import torch

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 指标口径直接复用既有探针，不另写一套（避免口径漂移）
from probe_emotion_instruct import estimate_f0, spectral_centroid  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
OUTDIR = os.path.join(HERE, "_pred_temp_probe")

SPEAKER = "Vivian"

# 三句播客口语，长度落在门禁区间（8–40 字）内
TEXTS = [
    ("S1", "今天我们聊一个有点意思的话题。"),
    ("S2", "他把杯子放下，沉默了很久，才开口说了一句。"),
    ("S3", "问题是，我们真的需要这么多选择吗？"),
]

# predictor 档位：现状 0.9 → 中间档 → 目标 0.4 → 贪心（极限对照）
PRED_TEMPS = [0.9, 0.6, 0.4]
GREEDY_KEY = "greedy"
SEEDS = [1007, 2007, 3007, 4007]

# talker 一路保持服务端现值不动（serve.py: TEMPERATURE = 0.4）
TALKER_KW = {"temperature": 0.4, "top_k": 50, "top_p": 1.0,
             "do_sample": True, "repetition_penalty": 1.05}

# 与 serve.py 一致：生成帧数按字数封顶
FRAMES_PER_CHAR = 8
FRAME_FLOOR = 120
MAX_FRAMES = 2048

RUNLOG: list[list[int]] = []   # 每次 predictor_graph.run 的输出 token


def max_frames_for(text: str) -> int:
    return int(min(MAX_FRAMES, max(FRAME_FLOOR, len(text) * FRAMES_PER_CHAR)))


def set_seed(seed: int) -> None:
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def write_wav(path: str, a, sr: int) -> None:
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    pcm = np.clip(a, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def measure(a, sr, n_chars: int) -> dict:
    """时长 / 语速 / 响度 / F0 / 质心。F0 是第一指标：f0_std 即句内音高波动。"""
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    dur = len(a) / float(sr)
    f0 = estimate_f0(a, sr)
    rms = float(np.sqrt((a ** 2).mean())) if a.size else 0.0
    return {
        "dur": round(dur, 3),
        "rate": round(n_chars / dur, 3) if dur > 0 else 0.0,
        "rms_db": round(20.0 * float(np.log10(rms + 1e-9)), 2),
        "f0_mean": round(float(f0.mean()), 1) if f0.size else 0.0,
        "f0_std": round(float(f0.std()), 1) if f0.size else 0.0,
        "centroid": round(spectral_centroid(a, sr), 1),
    }


def build_predictor(backend, temperature: float, do_sample: bool = True):
    """造一张新图：温度按需，其余与库里那处构造保持同值。"""
    from faster_qwen3_tts.predictor_graph import PredictorGraph

    talker = backend.model.model.talker
    cp = talker.code_predictor
    pred_config = cp.model.config
    hidden = backend.model.model.config.talker_config.hidden_size

    pg = PredictorGraph(
        cp, pred_config, hidden,
        device=backend.device, dtype=backend.dtype,
        do_sample=do_sample, top_k=50, top_p=1.0, temperature=temperature,
    )
    pg.capture(num_warmup=3)

    # 记下每一步的 token，用来验证「图内采样是否确定性」
    orig_run = pg.run

    def hooked(pred_input):
        out = orig_run(pred_input)
        RUNLOG.append(out.tolist())
        return out

    pg.run = hooked
    return pg


def install_predictor(backend, pg) -> None:
    """换上去。fast_generate 每步都读 self.predictor_graph，所以即刻生效。"""
    old = backend.predictor_graph
    backend.predictor_graph = pg
    if old is not None:
        try:
            del old
        except Exception:  # noqa: BLE001
            pass
    gc.collect()
    torch.cuda.empty_cache()


def digest_tokens(rows: list[list[int]]) -> str:
    flat = ";".join(",".join(str(t) for t in r) for r in rows)
    return hashlib.md5(flat.encode("utf-8")).hexdigest()[:12]


def main() -> None:
    from faster_qwen3_tts import FasterQwen3TTS

    if not torch.cuda.is_available():
        print("没有 CUDA，本探测的前提不成立（库硬判 cuda）")
        return

    free, total = torch.cuda.mem_get_info()
    print("显存空闲 %.0f MB / %.0f MB" % (free / 1048576, total / 1048576), flush=True)

    os.makedirs(OUTDIR, exist_ok=True)
    dev = "cuda"
    print("加载模型 … device=%s" % dev, flush=True)
    backend = FasterQwen3TTS.from_pretrained(MODEL, device=dev)
    print("就绪\n", flush=True)

    def gen(text: str, seed: int):
        set_seed(seed)
        wavs, sr = backend.generate_custom_voice(
            text=text, speaker=SPEAKER, language="Chinese",
            max_new_tokens=max_frames_for(text), **TALKER_KW)
        a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
        if hasattr(a, "cpu"):
            a = a.cpu().numpy()
        return np.asarray(a, dtype=np.float32).reshape(-1), sr

    # ---- 首次生成：触发库里那张默认图（0.9）的捕获，同时消掉首推理差异 --- #
    print("首次生成（触发 talker 图捕获，不作数）…", flush=True)
    RUNLOG.clear()
    gen(TEXTS[0][1], 0)
    base_graph = backend.predictor_graph
    print("库内默认图：temperature=%s  do_sample=%s  top_k=%s\n"
          % (base_graph.temperature, base_graph.do_sample, base_graph.top_k), flush=True)

    rows: list[dict] = []
    caps: dict[str, dict] = {}

    plan = [(("P%s" % t), t, True) for t in PRED_TEMPS] + [(GREEDY_KEY, 0.9, False)]
    for key, temp, do_sample in plan:
        print("── 换图：predictor temperature=%s  do_sample=%s ──" % (temp, do_sample), flush=True)
        t0 = time.time()
        pg = build_predictor(backend, temp, do_sample)
        install_predictor(backend, pg)
        caps[key] = {"temperature": temp, "do_sample": do_sample,
                     "capture_s": round(time.time() - t0, 2),
                     "graph_temperature": backend.predictor_graph.temperature}
        print("   已挂上 · 图内 temperature=%s · 用时 %.1fs"
              % (backend.predictor_graph.temperature, time.time() - t0), flush=True)

        texts = TEXTS if key != GREEDY_KEY else TEXTS[:1]
        seeds = SEEDS if key != GREEDY_KEY else SEEDS[:2]

        for tag, text in texts:
            for seed in seeds:
                RUNLOG.clear()
                t1 = time.time()
                a, sr = gen(text, seed)
                el = time.time() - t1
                steps = len(RUNLOG)
                name = "%s_%s_s%d.wav" % (tag, key, seed)
                path = os.path.join(OUTDIR, name)
                write_wav(path, a, sr)
                m = measure(a, sr, len(text))
                hit_cap = steps >= max_frames_for(text)
                rec = {
                    "tag": tag, "text": text, "temp_key": key, "pred_temp": temp,
                    "do_sample": do_sample, "seed": seed, "wav": name,
                    "gen_s": round(el, 2), "steps": steps,
                    "hit_cap": hit_cap, "pred_digest": digest_tokens(RUNLOG),
                    **m,
                }
                rows.append(rec)
                print("   %s seed=%-5d %5.2fs  F0 %6.1f±%-5.1f  质心 %6.1f  "
                      "时长 %5.2fs  步数 %4d%s"
                      % (name, seed, el, m["f0_mean"], m["f0_std"],
                         m["centroid"], m["dur"], steps,
                         "  顶到上限" if hit_cap else ""), flush=True)

        # ---- 确定性检验：同档同种子再跑一次 ---------------------------- #
        tag, text = TEXTS[0]
        seed = SEEDS[0]
        RUNLOG.clear()
        a1, sr = gen(text, seed)
        d1 = digest_tokens(RUNLOG)
        n1 = len(RUNLOG)
        RUNLOG.clear()
        a2, sr = gen(text, seed)
        d2 = digest_tokens(RUNLOG)
        n2 = len(RUNLOG)
        pcm1 = hashlib.md5(np.asarray(a1, dtype=np.float32).tobytes()).hexdigest()[:12]
        pcm2 = hashlib.md5(np.asarray(a2, dtype=np.float32).tobytes()).hexdigest()[:12]
        caps[key]["determinism"] = {
            "pred_tokens_first": d1, "pred_tokens_second": d2,
            "pred_tokens_same": d1 == d2, "steps": [n1, n2],
            "wav_md5": [pcm1, pcm2], "wav_same": pcm1 == pcm2,
        }
        print("   确定性：predictor token 两次 %s（%s / %s）· 波形两次 %s"
              % ("一致" if d1 == d2 else "不同", d1, d2,
                 "一致" if pcm1 == pcm2 else "不同"), flush=True)
        print(flush=True)

    # ---- 汇总 ------------------------------------------------------------- #
    print("=" * 96, flush=True)
    print("按档汇总（三句 × 四种子）", flush=True)
    print("%-8s %-26s %-26s %-22s" % ("档位", "F0 中位（跨种子）", "F0 波动 f0_std", "质心（跨种子）"), flush=True)
    summary = {}
    for key, _t, _d in plan:
        sub = [r for r in rows if r["temp_key"] == key]
        if not sub:
            continue
        f0m = [r["f0_mean"] for r in sub]
        f0s = [r["f0_std"] for r in sub]
        ct = [r["centroid"] for r in sub]
        dur = [r["dur"] for r in sub]

        def sp(v):
            return "中位 %6.1f 极差 %6.1f" % (float(np.median(v)), max(v) - min(v))

        summary[key] = {
            "f0_mean_median": round(float(np.median(f0m)), 1),
            "f0_mean_range": round(max(f0m) - min(f0m), 1),
            "f0_std_mean": round(float(np.mean(f0s)), 1),
            "centroid_median": round(float(np.median(ct)), 1),
            "centroid_range": round(max(ct) - min(ct), 1),
            "dur_range": round(max(dur) - min(dur), 3),
            "n": len(sub), "cap_hits": sum(1 for r in sub if r["hit_cap"]),
        }
        print("%-8s %-26s %-26s %-22s 顶上限 %d/%d"
              % (key, sp(f0m), "均值 %5.1f 极差 %5.1f" % (float(np.mean(f0s)), max(f0s) - min(f0s)),
                 sp(ct), summary[key]["cap_hits"], len(sub)), flush=True)

    # 同文本同种子、跨档位的直接对照（这才是「换温度改变了什么」）
    print("\n同句同种子、只换 predictor 温度的直接对照（时长 / F0 / 质心）", flush=True)
    print("%-5s %-6s %-14s %-18s %-14s %s" % ("句", "种子", "P0.9", "P0.6", "P0.4", "贪心"), flush=True)
    cross = []
    for tag, text in TEXTS:
        for seed in SEEDS:
            picked = {}
            for key, _t, _d in plan:
                for r in rows:
                    if r["tag"] == tag and r["seed"] == seed and r["temp_key"] == key:
                        picked[key] = r
            if "P0.9" not in picked:
                continue
            fmt = lambda k: ("%.2fs/%5.1f/%6.0f" % (picked[k]["dur"], picked[k]["f0_mean"],
                                                    picked[k]["centroid"])) if k in picked else "-"
            cross.append({"tag": tag, "seed": seed,
                          **{k: (picked[k]["wav"] if k in picked else None) for k in
                             ["P0.9", "P0.6", "P0.4", GREEDY_KEY]}})
            print("%-5s %-6d %-14s %-18s %-14s %s"
                  % (tag, seed, fmt("P0.9"), fmt("P0.6"), fmt("P0.4"), fmt(GREEDY_KEY)), flush=True)

    out = {
        "model": MODEL, "speaker": SPEAKER, "talker_kwargs": TALKER_KW,
        "pred_temps": PRED_TEMPS, "seeds": SEEDS,
        "caps": caps, "summary": summary, "rows": rows, "cross": cross,
        "note": "predictor 温度经重建 CUDA 图生效，未改动库源码",
    }
    jpath = os.path.join(OUTDIR, "probe.json")
    with io.open(jpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n完成 · %s" % jpath, flush=True)


if __name__ == "__main__":
    main()
