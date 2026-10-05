# -*- coding: utf-8 -*-
"""固定种子 vs 关子码本 —— 两个方案的实测对照。

问题：
  1. 能否「用全部 A 的文本算一个 A 的专属种子」，整期乃至整项目固定？B 同理。
  2. 关掉子码本采样（子码本贪心）能带来多少句间一致性改善？两者可叠加吗？

三块，一次模型加载跑完：
  P0  插桩：CUDA 图内的子码本采样到底吃不吃 torch.manual_seed
  P1  固定种子幅面：12 句 × 6 种种子策略（含用户方案）+ 分半验证
  P2  关子码本：现状 / 关子码 / 关子码 + 固定种子

产物：_smoke/_anchor_probe/anchor.json 与 *.wav
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
OUT = os.path.join(ROOT, "_smoke", "_anchor_probe")
VOICE = "Serena"
N_PICKS = 12
SEED_STRATS = ("derived", "agg", "voice", "zero", "fixed12345", "rand")
STRAT_LABEL = {
    "derived": "现状（逐句派生）",
    "agg": "整期语料算一个（用户方案）",
    "voice": "音色名算一个（跨期固定）",
    "zero": "固定 0",
    "fixed12345": "固定 12345",
    "rand": "固定 podcast-maker 串",
}


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
    """重建并挂上子码本图。温度保持库默认 0.9，只动 do_sample。"""
    pg = PT.build_predictor(backend, 0.9, do_sample=do_sample)
    PT.install_predictor(backend, pg)


def set_main(do_sample: bool):
    serve.SAMPLE_KWARGS["do_sample"] = do_sample


def seed_from(tag, text, seed_agg, seed_voice, seed_rand):
    if tag == "derived":
        return serve.seed_for(text, VOICE)
    return {"agg": seed_agg, "voice": seed_voice, "zero": 0,
            "fixed12345": 12345, "rand": seed_rand}[tag]


def disp(rs):
    """一组样本的离散度。越小 = 句间越统一。"""
    s = {}
    for k in ("f0_med", "f1", "f2", "cent", "dur"):
        v = np.array([r[k] for r in rs], dtype=float)
        s[k + "_sd"] = float(v.std(ddof=1)) if v.size > 1 else 0.0
        s[k + "_span"] = float(v.max() - v.min()) if v.size else 0.0
    return s


def main():
    os.makedirs(OUT, exist_ok=True)
    script = json.load(io.open(SCRIPT, encoding="utf-8"))

    # ---------- 选句：B 音色 + 情绪=解释，均匀取 12 ----------
    hits = [(i, s["text"]) for i, s in enumerate(script)
            if s.get("speaker") == "B" and s.get("emotion") == "解释"]
    step = max(1, len(hits) // N_PICKS)
    picks = hits[::step][:N_PICKS]
    all_text = "".join(t for _, t in picks)

    seed_agg = int.from_bytes(hashlib.sha256(all_text.encode("utf-8")).digest()[:4], "big")
    seed_voice = int.from_bytes(hashlib.sha256(VOICE.encode("utf-8")).digest()[:4], "big")
    seed_rand = int.from_bytes(hashlib.sha256(b"podcast-maker").digest()[:4], "big")

    print("选中 %d 句（B / %s / 情绪=解释）" % (len(picks), VOICE))
    print("  整期语料种子 agg   = %d" % seed_agg)
    print("  音色名种子   voice = %d" % seed_voice)
    print("  固定串种子   rand  = %d" % seed_rand)
    for i, t in picks:
        print("    %04d %2d字 %s" % (i, len(t), t[:38]))

    # ---------- 加载引擎 ----------
    from podcast_maker.config_manager import ConfigManager
    vb = ConfigManager().get("tts.qwen3tts_voice_b", "Serena")
    print("\n配置里 B 音色 = %r" % vb)

    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    backend = eng.ensure()
    print("  设备=%s  后端=%s  主路采样参数=%s"
          % (getattr(backend, "device", "?"), type(backend).__name__,
             dict(serve.SAMPLE_KWARGS)))

    rows = []

    # ================= P0 插桩 =================
    print("\n" + "=" * 78)
    print("P0 · 插桩：CUDA 图内的子码本采样吃不吃 torch.manual_seed")
    print("=" * 78)
    set_main(True)
    set_sub(backend, True)
    t0 = picks[0][1]
    snaps, waves = [], []
    for sd in (1, 2):
        PT.RUNLOG.clear()
        s, sr = eng.synth_one(t0, VOICE, instruct=None, language="Chinese", seed=sd)
        snaps.append([list(map(int, r)) for r in PT.RUNLOG])
        waves.append(hashlib.sha256(
            np.asarray(s, dtype=np.float64).tobytes()).hexdigest()[:16])
        print("  seed=%-4d  子码本图跑 %4d 帧  波形 sha16=%s  前2帧=%s"
              % (sd, len(snaps[-1]), waves[-1], snaps[-1][:2]))
    n = min(len(snaps[0]), len(snaps[1]))
    same_frames = sum(1 for i in range(n) if snaps[0][i] == snaps[1][i])
    sub_blind = (n > 0 and same_frames == n)
    print("  两种子共同帧 %d，子码本 token 逐帧相同 %d 帧" % (n, same_frames))
    print("  → 子码本图内采样 %s（随机源不来自 torch.manual_seed）"
          % ("【不吃种子】" if sub_blind else "【吃种子】"))
    print("  → 真波形是否随种子变：%s" % ("是" if waves[0] != waves[1] else "否"))
    p0 = {"frames": n, "same_frames": same_frames, "sub_blind": sub_blind,
          "wave_sha": waves, "wave_differs": waves[0] != waves[1]}

    # ================= P1 固定种子幅面 =================
    print("\n" + "=" * 78)
    print("P1 · 固定种子幅面：12 句 × 6 种种子策略")
    print("=" * 78)
    set_main(True)
    set_sub(backend, True)
    for tag in SEED_STRATS:
        print("\n  【%s】%s" % (tag, STRAT_LABEL[tag]))
        for idx, text in picks:
            sd = seed_from(tag, text, seed_agg, seed_voice, seed_rand)
            samples, sr = eng.synth_one(text, VOICE, instruct=None,
                                        language="Chinese", seed=sd)
            fn = "P1_%s_%04d.wav" % (tag, idx)
            save_wav(samples, sr, os.path.join(OUT, fn))
            m = measure(os.path.join(OUT, fn))
            rows.append({"blk": "P1", "cond": tag, "idx": idx, "seed": sd,
                         "file": fn, "chars": len(text),
                         "f0_med": m["f0_med"], "f1": m["f1"], "f2": m["f2"],
                         "cent": m["cent"], "dur": m["dur"]})
            print("     %04d %3d种子 F0 %6.1f | F1 %7.1f | F2 %7.1f | %5.0f | %.2fs"
                  % (idx, sd, m["f0_med"], m["f1"], m["f2"], m["cent"], m["dur"]))

    # ================= P2 关子码本 =================
    print("\n" + "=" * 78)
    print("P2 · 关子码本（子码本贪心）")
    print("=" * 78)
    set_main(True)
    set_sub(backend, False)
    for tag in ("off_derived", "off_agg"):
        print("\n  【%s】" % tag)
        for idx, text in picks:
            sd = seed_from("derived" if tag == "off_derived" else "agg",
                           text, seed_agg, seed_voice, seed_rand)
            samples, sr = eng.synth_one(text, VOICE, instruct=None,
                                        language="Chinese", seed=sd)
            fn = "P2_%s_%04d.wav" % (tag, idx)
            save_wav(samples, sr, os.path.join(OUT, fn))
            m = measure(os.path.join(OUT, fn))
            rows.append({"blk": "P2", "cond": tag, "idx": idx, "seed": sd,
                         "file": fn, "chars": len(text),
                         "f0_med": m["f0_med"], "f1": m["f1"], "f2": m["f2"],
                         "cent": m["cent"], "dur": m["dur"]})
            print("     %04d %3d种子 F0 %6.1f | F1 %7.1f | F2 %7.1f | %5.0f | %.2fs"
                  % (idx, sd, m["f0_med"], m["f1"], m["f2"], m["cent"], m["dur"]))

    # ================= 汇总 =================
    print("\n" + "=" * 78)
    print("汇总 · 句间离散度（越小 = 整期越像同一个人在说）")
    print("=" * 78)
    print("  %-28s %8s %8s %8s %8s %8s %8s"
          % ("条件", "F0标", "F0极差", "F1标", "F2标", "质心标", "时长效"))
    summary = {}
    for tag in SEED_STRATS:
        rs = [r for r in rows if r["cond"] == tag]
        s = disp(rs)
        summary["P1_" + tag] = s
        print("  %-28s %8.1f %8.1f %8.1f %8.1f %8.1f %8.2f"
              % ("P1 " + STRAT_LABEL[tag][:12], s["f0_med_sd"], s["f0_med_span"],
                 s["f1_sd"], s["f2_sd"], s["cent_sd"], s["dur_sd"]))
    for tag in ("off_derived", "off_agg"):
        rs = [r for r in rows if r["cond"] == tag]
        s = disp(rs)
        summary["P2_" + tag] = s
        print("  %-28s %8.1f %8.1f %8.1f %8.1f %8.1f %8.2f"
              % ("P2 " + tag, s["f0_med_sd"], s["f0_med_span"],
                 s["f1_sd"], s["f2_sd"], s["cent_sd"], s["dur_sd"]))

    print("\n" + "=" * 78)
    print("改善倍数（现状 ÷ 该方案，>1 = 更统一；现状 = P1 derived）")
    print("=" * 78)
    base = summary["P1_derived"]
    improve = {}
    for name, s in summary.items():
        if name == "P1_derived":
            continue
        rec = {}
        for k, lab in (("f0_med_sd", "F0标"), ("f0_med_span", "F0极差"),
                       ("f1_sd", "F1"), ("f2_sd", "F2"),
                       ("cent_sd", "质心"), ("dur_sd", "时长")):
            rec[lab] = (base[k] / s[k]) if s[k] > 0 else float("inf")
        improve[name] = rec
        print("  %-28s %s" % (name, "  ".join("%s %.2fx" % (k, v)
                                              for k, v in rec.items())))

    print("\n" + "=" * 78)
    print("分半验证 · 12 句切前 6 / 后 6，看「最优种子策略」是否跨半一致")
    print("=" * 78)
    half = {}
    for tag in SEED_STRATS:
        rs = [r for r in rows if r["cond"] == tag]
        h1, h2 = rs[:6], rs[6:]
        a, b = disp(h1), disp(h2)
        half[tag] = {"first6_span": a["f0_med_span"], "last6_span": b["f0_med_span"],
                     "first6_f1sd": a["f1_sd"], "last6_f1sd": b["f1_sd"]}
        print("  %-14s 前6句 F0极差 %6.1f / F1标 %7.1f   |   后6句 F0极差 %6.1f / F1标 %7.1f"
              % (tag, a["f0_med_span"], a["f1_sd"], b["f0_med_span"], b["f1_sd"]))
    rank1 = sorted(SEED_STRATS, key=lambda t: half[t]["first6_span"])
    rank2 = sorted(SEED_STRATS, key=lambda t: half[t]["last6_span"])
    print("\n  按 F0 极差排名：")
    print("    前 6 句最优 → %s" % rank1[0])
    print("    后 6 句最优 → %s" % rank2[0])
    print("    两半一致：%s" % ("是" if rank1[0] == rank2[0] else "否 —— 换一批句子结论就翻转"))

    out = {"picks": [i for i, _ in picks], "seeds": {"agg": seed_agg,
           "voice": seed_voice, "rand": seed_rand}, "p0": p0, "rows": rows,
           "summary": summary, "improve": improve, "half": half,
           "rank_first6": rank1, "rank_last6": rank2}
    with io.open(os.path.join(OUT, "anchor.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % os.path.join(OUT, "anchor.json"))


if __name__ == "__main__":
    main()
