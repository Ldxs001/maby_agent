# -*- coding: utf-8 -*-
"""三方对照：现状 / 只关子码本 / 全贪心 —— 句间音色离散度。

回答的问题：
    固定种子救不了句间一致性（P1/P2/P3 实测：一个方案变好、一个变差）。
    那「把随机性彻底掐掉」能不能救？

三组，同 6 句（B 音色 Serena、情绪=解释，与 P1/P2/P3 同批）、同分析口径：
    Q1 现状      主路采样(0.4) + 子码本采样(0.9)，种子按 (音色,文本) 派生
    Q2 只关子码本  主路采样(0.4) + 子码本贪心
    Q3 全贪心     两处都贪心

另附复验：Q3 下同一句换 3 个种子，波形应逐字节相同（证明"随机性真的没了"）。

产物：_smoke/_policy2_probe/policy2.json + *.wav
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
import probe_predictor_temp as PT  # noqa: E402
from probe_identity_v2 import analyze  # noqa: E402

SCRIPT = os.path.join(ROOT, "projects/20260915-103249/脚本/1.json")
OUT = os.path.join(ROOT, "_smoke", "_policy2_probe")
PICKS = [3, 7, 11, 19, 23, 29]          # 全是 B / Serena / 情绪=解释
CONDS = ("Q1_现状_两处都采样", "Q2_只关子码本", "Q3_全贪心")


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


def set_sub(samples_graph_owner, do_sample: bool):
    """重建并挂上子码本图。温度保持库默认 0.9，只动 do_sample。"""
    pg = PT.build_predictor(samples_graph_owner, 0.9, do_sample=do_sample)
    PT.install_predictor(samples_graph_owner, pg)


def set_main(do_sample: bool):
    """改产品侧的采样开关（模块级常量，运行时覆盖）。"""
    serve.SAMPLE_KWARGS["do_sample"] = do_sample


def main():
    os.makedirs(OUT, exist_ok=True)
    script = json.load(io.open(SCRIPT, encoding="utf-8"))

    from podcast_maker.config_manager import ConfigManager
    vb = ConfigManager().get("tts.qwen3tts_voice_b", "Serena")
    print("B 音色 = %r" % vb)

    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    backend = eng.ensure()
    print("  设备=%s 后端=%s" % (getattr(backend, "device", "?"),
                                type(backend).__name__))

    all_rows = []
    for cond in CONDS:
        print("\n" + "=" * 78)
        print("【%s】" % cond)
        print("=" * 78)
        if cond == "Q1_现状_两处都采样":
            set_main(True)
            set_sub(backend, True)
        elif cond == "Q2_只关子码本":
            set_main(True)
            set_sub(backend, False)
        else:
            set_main(False)
            set_sub(backend, False)

        for idx in PICKS:
            text = script[idx]["text"]
            seed = serve.seed_for(text, vb)          # 产品口径：音色名，不是 "B"
            samples, sr = eng.synth_one(text, vb, instruct=None,
                                       language="Chinese", seed=seed)
            fn = "%s_%04d.wav" % (cond.split("_")[0], idx)
            save_wav(samples, sr, os.path.join(OUT, fn))
            a = analyze(os.path.join(OUT, fn))
            rec = {"cond": cond, "idx": idx, "seed": seed, "file": fn,
                   "chars": len(text), "text": text}
            rec.update({k: a[k] for k in ("f0_med", "f1", "f2", "cent", "dur")})
            all_rows.append(rec)
            print("   %04d %2d字 F0 %6.1f | F1 %7.1f | F2 %7.1f | 质心 %5.0f | %.2fs" %
                  (idx, len(text), a["f0_med"], a["f1"], a["f2"], a["cent"], a["dur"]))

    # ---------------- 复验：Q3 下换种子 ----------------
    print("\n" + "=" * 78)
    print("复验 · Q3(全贪心) 下同一句换 3 个种子，波形是否相同")
    print("=" * 78)
    import hashlib
    recheck = []
    text = script[19]["text"]
    for k, s in enumerate((111, 222, 333)):
        samples, sr = eng.synth_one(text, vb, instruct=None,
                                   language="Chinese", seed=s)
        fn = "recheck_0019_s%d.wav" % s
        save_wav(samples, sr, os.path.join(OUT, fn))
        h = hashlib.sha256(open(os.path.join(OUT, fn), "rb").read()).hexdigest()[:16]
        recheck.append({"seed": s, "sha16": h, "file": fn})
        print("   seed=%-6d sha16=%s" % (s, h))
    same = len({r["sha16"] for r in recheck}) == 1
    print("   → 完全一致: %s" % ("是" if same else "否"))

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    print("句间离散度（6 句各指标的标准差/极差；越小 = 音色越统一）")
    print("=" * 78)
    print("  %-18s %9s %9s %9s %9s %9s %9s" %
          ("条件", "F0标", "F1标", "F2标", "质心标", "F0极差", "时长效"))
    summary = {}
    for cond in CONDS:
        rs = [r for r in all_rows if r["cond"] == cond]
        s = {}
        for k in ("f0_med", "f1", "f2", "cent", "dur"):
            v = np.array([r[k] for r in rs], dtype=float)
            s[k + "_sd"] = float(v.std(ddof=1))
        s["f0_span"] = float(max(r["f0_med"] for r in rs) - min(r["f0_med"] for r in rs))
        summary[cond] = s
        print("  %-18s %9.1f %9.1f %9.1f %9.1f %9.1f %9.2f" %
              (cond, s["f0_med_sd"], s["f1_sd"], s["f2_sd"],
               s["cent_sd"], s["f0_span"], s["dur_sd"]))

    print("\n" + "=" * 78)
    print("相对现状的改善倍数（现状标准差 ÷ 该条件标准差，>1 = 更统一）")
    print("=" * 78)
    base = summary["Q1_现状_两处都采样"]
    improve = {}
    for cond in CONDS[1:]:
        s = summary[cond]
        parts, rec = [], {}
        for k, label in (("f0_med_sd", "F0"), ("f1_sd", "F1"),
                         ("f2_sd", "F2"), ("cent_sd", "质心"), ("dur_sd", "时长")):
            r = base[k] / s[k] if s[k] > 0 else float("inf")
            rec[label] = r
            parts.append("%s %.2fx" % (label, r))
        improve[cond] = rec
        print("  %-18s %s" % (cond, "  ".join(parts)))

    # ---------------- 用户实际听的那两句 ----------------
    print("\n" + "=" * 78)
    print("0003 与 0019 的差距（用户实际听到的那两句）")
    print("=" * 78)
    gap = {}
    for cond in CONDS:
        rs = {r["idx"]: r for r in all_rows if r["cond"] == cond}
        d_f0 = abs(rs[3]["f0_med"] - rs[19]["f0_med"])
        d_f1 = abs(rs[3]["f1"] - rs[19]["f1"])
        d_f2 = abs(rs[3]["f2"] - rs[19]["f2"])
        d_dur = abs(rs[3]["dur"] - rs[19]["dur"])
        gap[cond] = {"d_f0": d_f0, "d_f1": d_f1, "d_f2": d_f2, "d_dur": d_dur}
        print("  %-18s ΔF0 %6.1f Hz | ΔF1 %7.1f | ΔF2 %7.1f | Δ时长 %.2fs" %
              (cond, d_f0, d_f1, d_f2, d_dur))
    print("\n  逐句明细（三条件同句对照）：")
    print("  %-6s %6s | %-30s | %-30s | %-30s" %
          ("句号", "字数", "Q1 现状", "Q2 只关子码本", "Q3 全贪心"))
    for idx in PICKS:
        cells = []
        for cond in CONDS:
            r = [x for x in all_rows if x["cond"] == cond and x["idx"] == idx][0]
            cells.append("F0 %6.1f F1 %7.1f %5.2fs" % (r["f0_med"], r["f1"], r["dur"]))
        print("  %04d  %6d | %-30s | %-30s | %-30s" %
              (idx, len(script[idx]["text"]), cells[0], cells[1], cells[2]))

    out = {"rows": all_rows, "summary": summary, "improve": improve,
           "gap_3_19": gap, "recheck_q3": recheck, "recheck_same": same}
    with io.open(os.path.join(OUT, "policy2.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT, "policy2.json"))


if __name__ == "__main__":
    main()
