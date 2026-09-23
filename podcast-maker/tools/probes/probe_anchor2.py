# -*- coding: utf-8 -*-
"""补测：固定种子 × 关子码本 —— 两条路叠加。

用户的两条路（固定种子 / 关子码本）已各自单测，这里补一个缺口：叠加。
两句实验同批（12 句 B/Serena/解释），口径同 _probe_precise（pyin + LPC 复根）。

条件：
  C1  固定 12345  + 关子码本
  C2  固定 podcast-maker 串 + 关子码本

产物：_smoke/_anchor_probe2/{anchor2.json,*.wav}
"""
import hashlib
import io
import json
import os
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tts_service"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"))

import serve  # noqa: E402
import probe_predictor_temp as PT  # noqa: E402
from probe_precise import measure  # noqa: E402

SCRIPT = os.path.join(ROOT, "projects/20260915-103249/脚本/1.json")
OUT = os.path.join(ROOT, "_smoke", "_anchor_probe2")
VOICE = "Serena"
N_PICKS = 12
CONDS = ("combo_12345_off", "combo_rand_off")


def save_wav(samples, sr, path):
    a = np.asarray(samples, dtype=np.float64)
    if a.ndim > 1:
        a = a.mean(axis=1)
    a = np.clip(a, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes((a * 32767.0).astype("<i2").tobytes())


def set_sub(backend, do_sample: bool):
    pg = PT.build_predictor(backend, 0.9, do_sample=do_sample)
    PT.install_predictor(backend, pg)


def set_main(do_sample: bool):
    serve.SAMPLE_KWARGS["do_sample"] = do_sample


def disp(rs):
    s = {}
    for k in ("f0_med", "f1", "f2", "cent", "dur"):
        v = np.array([r[k] for r in rs], dtype=float)
        s[k + "_sd"] = float(v.std(ddof=1)) if v.size > 1 else 0.0
        s[k + "_span"] = float(v.max() - v.min()) if v.size else 0.0
    return s


def main():
    os.makedirs(OUT, exist_ok=True)
    script = json.load(io.open(SCRIPT, encoding="utf-8"))
    hits = [(i, s["text"]) for i, s in enumerate(script)
            if s.get("speaker") == "B" and s.get("emotion") == "解释"]
    step = max(1, len(hits) // N_PICKS)
    picks = hits[::step][:N_PICKS]

    seed_rand = int.from_bytes(hashlib.sha256(b"podcast-maker").digest()[:4], "big")
    SEEDS = {"combo_12345_off": 12345, "combo_rand_off": seed_rand}

    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    backend = eng.ensure()
    print("  设备=%s  后端=%s" % (getattr(backend, "device", "?"),
                                  type(backend).__name__))

    set_main(True)
    set_sub(backend, False)          # 关子码本
    print("  子码本采样：已关（贪心）")

    rows = []
    for cond in CONDS:
        sd = SEEDS[cond]
        print("\n【%s】种子固定 = %d" % (cond, sd))
        for idx, text in picks:
            samples, sr = eng.synth_one(text, VOICE, instruct=None,
                                        language="Chinese", seed=sd)
            fn = "%s_%04d.wav" % (cond, idx)
            save_wav(samples, sr, os.path.join(OUT, fn))
            m = measure(os.path.join(OUT, fn))
            rows.append({"cond": cond, "idx": idx, "seed": sd, "file": fn,
                         "chars": len(text), "f0_med": m["f0_med"],
                         "f1": m["f1"], "f2": m["f2"],
                         "cent": m["cent"], "dur": m["dur"]})
            print("   %04d %2d字 F0 %6.1f | F1 %7.1f | F2 %7.1f | %.2fs"
                  % (idx, len(text), m["f0_med"], m["f1"], m["f2"], m["dur"]))

    print("\n" + "=" * 70)
    print("叠加条件离散度（对照：现状 F0标30.2/F0极差97.7/F1标110.4/F2标236.7/时长1.05）")
    print("=" * 70)
    summary = {}
    for cond in CONDS:
        rs = [r for r in rows if r["cond"] == cond]
        s = disp(rs)
        summary[cond] = s
        print("  %-20s F0标 %5.1f  F0极差 %5.1f  F1标 %6.1f  F2标 %6.1f  时长 %5.2f"
              % (cond, s["f0_med_sd"], s["f0_med_span"], s["f1_sd"],
                 s["f2_sd"], s["dur_sd"]))

    out = {"picks": [i for i, _ in picks], "rows": rows, "summary": summary}
    with io.open(os.path.join(OUT, "anchor2.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT, "anchor2.json"))


if __name__ == "__main__":
    main()
