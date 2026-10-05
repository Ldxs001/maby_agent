# -*- coding: utf-8 -*-
"""方差分解：把「去掉语气指令」的效应，和「模型自身的采样方差」分开。

用户的质疑是硬的：都已经 none 了，为什么有的句子突然拉长、有的突然压低？
到底是「去指令」这个动作造成的，还是同一条件下本来就这么飘？

实验设计（配对，排除混淆）：
    对每一句，取 4 个种子 × 2 个指令状态
        seed ∈ {s0, s0+1, s0+2, s0+3}   （s0 = 产品真派生 seed_for(text, speaker)）
        instruct ∈ {None, "用平静的语气说"}
    于是可以分离两种方差：
        条件主效应 = 同种子下 (None) 与 (有指令) 的差   ← 「去指令」的因果效应
        种子主效应 = 同条件下 跨 4 个种子的离散度        ← 模型自身的采样方差

用的全是产品真代码：tts_service.serve 的 Engine / synth_one / seed_for。
"""
import io
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tts_service"))

import serve  # noqa: E402  （产品真模块，不改）

SCRIPT = os.path.join(ROOT, "projects/20260915-103249/脚本/1.json")
OUT = os.path.join(ROOT, "_smoke", "_variance_probe")
INSTRUCT_ON = "用平静的语气说"          # 旧逻辑在「平静」上拼出来的原句
N_SEEDS = 4
N_LINES = 4


# ---------------------------------------------------------------- 指标
def f0_series(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512):
    x = np.asarray(x, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
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
    x = np.asarray(x, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    dur = len(x) / float(sr)
    f0 = f0_series(x, sr)
    # 谱质心（归一化到 Nyquist）
    n = 1 << 12
    if len(x) >= n:
        seg = x[:n] * np.hanning(n)
        mag = np.abs(np.fft.rfft(seg))
        fr = np.fft.rfftfreq(n, 1.0 / sr)
        cen = float(np.sum(fr * mag) / np.sum(mag) / (sr / 2.0)) if np.sum(mag) > 0 else float("nan")
    else:
        cen = float("nan")
    return {
        "dur": round(dur, 3),
        "cps": round(len(text) / dur, 2) if dur > 0 else float("nan"),   # 字/秒
        "f0_med": round(float(np.median(f0)), 1) if len(f0) else float("nan"),
        "f0_std": round(float(np.std(f0)), 1) if len(f0) else float("nan"),
        "f0_p10": round(float(np.percentile(f0, 10)), 1) if len(f0) else float("nan"),
        "f0_p90": round(float(np.percentile(f0, 90)), 1) if len(f0) else float("nan"),
        "centroid": round(cen, 4) if cen == cen else float("nan"),
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    with io.open(SCRIPT, encoding="utf-8") as f:
        script = json.load(f)

    # 取「平静」句：两个说话人各 2 句，长度中等
    real = [s for s in script if s.get("emotion") == "平静"]
    picks = []
    for spk in ("A", "B"):
        got = [s for s in real if s.get("speaker") == spk]
        got = sorted(got, key=lambda s: len(s.get("text", "")))
        if got:
            picks.append(got[len(got) // 2])
            picks.append(got[len(got) // 3])
    picks = picks[:N_LINES]

    from podcast_maker.config_manager import ConfigManager
    cfg = ConfigManager()
    model_id = serve.MODEL_ID if hasattr(serve, "MODEL_ID") else None
    if model_id is None:
        model_id = os.path.join(ROOT, "tts_service", "models",
                                "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    va = cfg.get("tts.qwen3tts_voice_a", "Vivian")
    vb = cfg.get("tts.qwen3tts_voice_b", "Serena")

    print("=" * 78)
    print("加载引擎（产品真类 serve.Engine）")
    print("=" * 78)
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    t0 = time.time()
    eng.ensure()
    print("  后端 %s · 设备 %s · 耗时 %.1fs" % (eng.backend, eng.device, time.time() - t0))

    records = []
    for idx, s in enumerate(picks):
        text = s.get("text", "")
        speaker = s.get("speaker", "A")
        voice = va if speaker == "A" else vb
        s0 = serve.seed_for(text, speaker)
        print()
        print("=" * 78)
        print("第 %d 句 · speaker=%s · voice=%s · %d 字" % (idx + 1, speaker, voice, len(text)))
        print("  %s" % text)
        print("  派生种子 s0 = %d" % s0)
        print("=" * 78)
        for k in range(N_SEEDS):
            sd = s0 + k
            for cond, ins in (("off", None), ("on", INSTRUCT_ON)):
                samples, sr = eng.synth_one(text, voice, instruct=ins,
                                            language="Chinese", seed=sd)
                m = metrics(samples, sr, text)
                m.update({"line": idx + 1, "speaker": speaker, "voice": voice,
                          "text": text, "seed": sd, "seed_offset": k,
                          "cond": cond, "instruct": ins, "n_chars": len(text)})
                records.append(m)
                print("  种子+%d  %-3s  时长 %5.2fs  字/秒 %5.2f  F0中位 %6.1f  "
                      "F0离散 %5.1f  谱质心 %.4f"
                      % (k, cond, m["dur"], m["cps"], m["f0_med"], m["f0_std"], m["centroid"]))

    with io.open(os.path.join(ROOT, "_smoke", "_variance_probe.json"), "w",
                 encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)

    # ------------------------------------------------------------ 方差分解
    print()
    print("=" * 78)
    print("方差分解")
    print("=" * 78)

    cond_diffs = {"dur_pct": [], "cps_pct": [], "f0_pct": [], "f0std_pct": []}
    seed_spread = {"dur_pct": [], "cps_pct": [], "f0_pct": [], "f0std_pct": []}

    for idx in range(1, N_LINES + 1):
        sub = [r for r in records if r["line"] == idx]
        for k in range(N_SEEDS):
            off = [r for r in sub if r["seed_offset"] == k and r["cond"] == "off"]
            on = [r for r in sub if r["seed_offset"] == k and r["cond"] == "on"]
            if off and on:
                a, b = off[0], on[0]                       # a=无指令, b=有指令
                for key, out in (("dur", "dur_pct"), ("cps", "cps_pct"),
                                 ("f0_med", "f0_pct"), ("f0_std", "f0std_pct")):
                    if a[key] and b[key] and b[key] == b[key] and a[key] == a[key]:
                        cond_diffs[out].append(abs(a[key] - b[key]) / abs(b[key]) * 100.0)
        for cond in ("off", "on"):
            grp = [r for r in sub if r["cond"] == cond]
            if len(grp) > 1:
                for key, out in (("dur", "dur_pct"), ("f0_med", "f0_pct"),
                                 ("f0_std", "f0std_pct")):
                    vals = [r[key] for r in grp if r[key] == r[key]]
                    if len(vals) > 1 and np.mean(vals):
                        seed_spread[out].append(
                            (max(vals) - min(vals)) / abs(np.mean(vals)) * 100.0)

    def stat(v):
        return "中位 %5.1f%%  最大 %6.1f%%  n=%d" % (
            float(np.median(v)) if v else float("nan"),
            float(np.max(v)) if v else float("nan"), len(v)) if v else "—"

    print("  【条件效应】同一种子下，有指令 vs 无指令 的差异（去指令的因果效应）")
    for k, label in (("dur_pct", "时长"), ("cps_pct", "语速"),
                     ("f0_pct", "F0中位"), ("f0std_pct", "F0离散")):
        print("    %-8s %s" % (label, stat(cond_diffs[k])))
    print()
    print("  【种子效应】同一条件下，换 4 个种子的离散度（模型自身采样方差）")
    for k, label in (("dur_pct", "时长"), ("f0_pct", "F0中位"), ("f0std_pct", "F0离散")):
        print("    %-8s %s" % (label, stat(seed_spread[k])))

    summary = {
        "cond_diff": {k: (float(np.median(v)) if v else None, float(np.max(v)) if v else None)
                      for k, v in cond_diffs.items()},
        "seed_spread": {k: (float(np.median(v)) if v else None, float(np.max(v)) if v else None)
                        for k, v in seed_spread.items()},
    }
    print()
    print("机器可读:", json.dumps(summary, ensure_ascii=False))
    return records


if __name__ == "__main__":
    main()
