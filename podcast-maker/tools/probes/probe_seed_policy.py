# -*- coding: utf-8 -*-
"""方案验证：种子策略对「音色一致性」的影响。

现状 = 种子按 (音色, 文本) 派生 → 每句话各自抽一次签，抽到什么是什么。
候选 = 种子固定成常数     → 所有句子共用同一个随机起点。

对 6 句不同文本、同一音色 Serena、同 instruct(None)，各跑三种种子策略：
    P1 现状：seed = seed_for(text, speaker)
    P2 固定：seed = 12345
    P3 固定：seed = 0

比较「组内离散度」：越小说明这个音色的声音越统一。
同时看 F0 的分布，防止「统一」变成「棒读」（所有句子一个调）。

产物：_smoke/_seed_probe/policy.json + policy_*.wav
"""
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
from probe_identity_v2 import analyze  # noqa: E402

SCRIPT = os.path.join(ROOT, "projects/20260915-103249/脚本/1.json")
OUT = os.path.join(ROOT, "_smoke", "_seed_probe")
PICKS = [3, 7, 11, 19, 23, 29]          # 全是 B 音色、情绪=解释
POLICIES = [("P1_现状_按文本派生", None), ("P2_固定_12345", 12345),
            ("P3_固定_0", 0)]


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


def main():
    os.makedirs(OUT, exist_ok=True)
    script = json.load(io.open(SCRIPT, encoding="utf-8"))

    from podcast_maker.config_manager import ConfigManager
    vb = ConfigManager().get("tts.qwen3tts_voice_b", "Serena")

    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    eng.ensure()

    all_rows = []
    for pname, fixed in POLICIES:
        print("\n" + "=" * 74)
        print("【%s】" % pname)
        print("=" * 74)
        rows = []
        for idx in PICKS:
            text = script[idx]["text"]
            seed = serve.seed_for(text, "B") if fixed is None else fixed
            fn = "pol_%s_%04d.wav" % (pname.split("_")[0], idx)
            samples, sr = eng.synth_one(text, vb, instruct=None,
                                        language="Chinese", seed=seed)
            save_wav(samples, sr, os.path.join(OUT, fn))
            a = analyze(os.path.join(OUT, fn))
            rec = {"policy": pname, "idx": idx, "seed": seed, "file": fn,
                   "chars": len(text), "text": text}
            rec.update({k: a[k] for k in ("f0_med", "f1", "f2", "cent", "dur")})
            rows.append(rec)
            print("   %04d  %2d字 种子%-11d F0 %6.1f | F1 %6.1f | F2 %6.1f | 质心 %5.0f | %.2fs" %
                  (idx, len(text), seed, a["f0_med"], a["f1"], a["f2"], a["cent"], a["dur"]))
        all_rows.extend(rows)

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 74)
    print("组内离散度（6 句各指标的标准差；越小 = 音色越统一）")
    print("=" * 74)
    print("  %-18s %10s %10s %10s %10s %10s" %
          ("方案", "F0标准差", "F1标准差", "F2标准差", "质心标准差", "F0极差"))
    summary = {}
    for pname, _ in POLICIES:
        rs = [r for r in all_rows if r["policy"] == pname]
        s = {}
        for k in ("f0_med", "f1", "f2", "cent"):
            v = np.array([r[k] for r in rs])
            s[k] = float(v.std(ddof=1))
        s["f0_span"] = float(max(r["f0_med"] for r in rs) - min(r["f0_med"] for r in rs))
        s["f1_span"] = float(max(r["f1"] for r in rs) - min(r["f1"] for r in rs))
        summary[pname] = s
        print("  %-18s %10.1f %10.1f %10.1f %10.1f %10.1f" %
              (pname, s["f0_med"], s["f1"], s["f2"], s["cent"], s["f0_span"]))

    print("\n" + "=" * 74)
    print("相对现状的改善倍数（现状标准差 ÷ 该方案标准差，>1 即更统一）")
    print("=" * 74)
    base = summary[POLICIES[0][0]]
    for pname, _ in POLICIES[1:]:
        s = summary[pname]
        parts = []
        for k, label in (("f0_med", "F0"), ("f1", "F1"), ("f2", "F2"), ("cent", "质心")):
            r = base[k] / s[k] if s[k] > 0 else float("inf")
            parts.append("%s %.2fx" % (label, r))
        print("  %-18s %s" % (pname, "  ".join(parts)))

    out = {"rows": all_rows, "summary": summary}
    with io.open(os.path.join(OUT, "policy.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT, "policy.json"))


if __name__ == "__main__":
    main()
