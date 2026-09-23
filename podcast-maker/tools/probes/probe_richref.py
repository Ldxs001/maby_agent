# -*- coding: utf-8 -*-
"""富语调参考音频实验（用户提案）：CustomVoice 合一条含陈述/疑问/惊叹的 ref，
看 Base+ICL 克隆这条 ref 时，疑问句句尾会不会跟着上扬。

与上一轮"换 ref"实验的本质区别：上次 ref 是 Base 自产的疑问句（本身不扬），
先验里没有上扬样本；这次 ref 由 CustomVoice（官方支持 instruct）合成，先验里
第一次真正含有"疑问=上扬"模式。

三阶段：
  1  CustomVoice 合两条混合语调 ref（instruct 版 / 无指令版）+ 存盘 + 自检指标
  2  卸载 CustomVoice，加载 Base，用两条 ref 分别 ICL 合成 8 句疑问 + 1 句陈述
  3  离线量五维指标，对照旧基线（ref_statement__none）

运行：tts_service/.venv/Scripts/python.exe tools/probes/probe_richref.py [1|2|3|all]
产物：_smoke/_richref/ 与 _smoke/_ab_out/richref_instruct|richref_plain/
"""
from __future__ import annotations

import gc
import io
import json
import os
import sys
import wave

import numpy as np

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
TTS = os.path.join(ROOT, "tts_service")
MODELS = os.path.join(TTS, "models")
OUT_RICH = os.path.join(HERE, "_richref")
OUT_A = os.path.join(HERE, "_ab_out", "richref_instruct")
OUT_P = os.path.join(HERE, "_ab_out", "richref_plain")

TEMPERATURE = 0.4
SAMPLE_KWARGS = {"temperature": TEMPERATURE, "top_k": 50, "top_p": 1.0,
                 "do_sample": True, "repetition_penalty": 1.05}
MAX_FRAMES, FRAME_FLOOR, FRAMES_PER_CHAR = 2048, 120, 8
SEED = 20260916

MIXED_TEXT = "这本书写得很有意思。这书真的是AI写的吗？这个结果太让人吃惊了！"
INSTRUCT = ("请用自然对话的语气朗读：陈述句平稳；疑问句句尾语调明显上扬；"
            "感叹句情绪饱满、力度加强。")
ST_TEXT = "这条路径的选择并不复杂，关键是先看清成本再决定。"


def log(msg):
    print(msg, flush=True)


def max_frames(text):
    return int(min(MAX_FRAMES, max(FRAME_FLOOR, len(text) * FRAMES_PER_CHAR)))


def save_wav(path, x, sr):
    x = np.asarray(x, dtype=np.float64)
    peak = np.max(np.abs(x)) or 1.0
    if peak > 1.0:
        x = x / peak
    pcm = (x * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return len(x) / float(sr)


def load_faster(model_dir):
    from faster_qwen3_tts import FasterQwen3TTS
    return FasterQwen3TTS.from_pretrained(model_dir, device="cuda")


def unload(m):
    del m
    gc.collect()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass


def stage1():
    """CustomVoice 合两条混合语调 ref。"""
    os.makedirs(OUT_RICH, exist_ok=True)
    import torch
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    m = load_faster(os.path.join(MODELS, "Qwen3-TTS-12Hz-1.7B-CustomVoice"))
    kw = dict(language="Chinese", speaker="Vivian",
              max_new_tokens=max_frames(MIXED_TEXT), **SAMPLE_KWARGS)
    for tag, extra in (("instruct", {"instruct": INSTRUCT}), ("plain", {})):
        k = dict(kw, **extra)
        out = m.generate_custom_voice(text=MIXED_TEXT, **k)
        wavs = out[0] if isinstance(out, tuple) else out
        sr = out[1] if isinstance(out, tuple) else 24000
        p = os.path.join(OUT_RICH, "ref_mixed_%s.wav" % tag)
        dur = save_wav(p, wavs[0] if hasattr(wavs, "__len__") and
                       not hasattr(wavs, "dtype") else wavs, sr)
        io.open(os.path.join(OUT_RICH, "ref_mixed_%s.txt" % tag), "w",
                encoding="utf-8").write(MIXED_TEXT)
        log("  ref_mixed_%s.wav  %.2fs" % (tag, dur))
    unload(m)


def load_texts():
    """主实验同款 8 句疑问 + 1 句陈述对照。"""
    sc = json.load(io.open(os.path.join(
        ROOT, "projects", "20260915-103249", "脚本", "1.json"),
        encoding="utf-8"))
    lines = sc if isinstance(sc, list) else (sc.get("lines") or [])
    qs = [(l.get("text") or "").rstrip() for l in lines
          if (l.get("text") or "").rstrip().endswith("？")
          and 12 <= len((l.get("text") or "").rstrip()) <= 34]
    return qs[:8]


def stage2():
    """Base + 两条新 ref 分别 ICL 合成。"""
    import torch
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    texts = load_texts()
    tests = [("q%02d" % (i + 1), t) for i, t in enumerate(texts)]
    tests.append(("st", ST_TEXT))
    m = load_faster(os.path.join(MODELS, "Qwen3-TTS-12Hz-1.7B-Base"))
    for tag, outdir in (("instruct", OUT_A), ("plain", OUT_P)):
        os.makedirs(outdir, exist_ok=True)
        ref = os.path.join(OUT_RICH, "ref_mixed_%s.wav" % tag)
        rtxt = io.open(os.path.join(OUT_RICH, "ref_mixed_%s.txt" % tag),
                       encoding="utf-8").read()
        for name, t in tests:
            torch.manual_seed(SEED + name.__hash__() % 100000)
            torch.cuda.manual_seed_all(SEED)
            out = m.generate_voice_clone(
                text=t, language="Chinese", ref_audio=ref, ref_text=rtxt,
                xvec_only=False, max_new_tokens=max_frames(t), **SAMPLE_KWARGS)
            wavs = out[0] if isinstance(out, tuple) else out
            sr = out[1] if isinstance(out, tuple) else 24000
            save_wav(os.path.join(outdir, name + ".wav"),
                     wavs[0] if hasattr(wavs, "__len__") and
                     not hasattr(wavs, "dtype") else wavs, sr)
            log("  [%s] %s %.0f 字" % (tag, name, len(t)))
    unload(m)


def stage3():
    """离线量五维指标，对照旧基线。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rich", os.path.join(HERE, "_intonation_rich.py"))
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)

    import glob
    import statistics as st

    def summ(d):
        rows = [r for r in (R.rich(p) for p in
                            sorted(glob.glob(os.path.join(d, "*.wav")))) if r]
        if not rows:
            return None
        return {k: st.median([r[k] for r in rows])
                for k in ("med", "rng", "tail_peak", "tail_rng", "end10_vs_25")}

    base = summ(os.path.join(HERE, "_ab_out", "refswap",
                             "ref_statement__none"))
    log("=" * 88)
    log("富语调 ref 实验：8 句疑问句中位数（半音，相对 200Hz）")
    log("=" * 88)
    log("%-28s %3s %9s %9s %9s %9s"
        % ("条件", "n", "整体音高", "音域", "句末抬高", "句末音域"))
    log("%-28s %3d %+9.2f %+9.2f %+9.2f %+9.2f"
        % ("旧基线(陈述ref,无指令)", 8, base["med"], base["rng"],
           base["tail_peak"], base["tail_rng"]))
    for label, d in (("新ref·instruct版", OUT_A), ("新ref·plain版", OUT_P)):
        s = summ(d)
        if not s:
            log("%-28s  无数据" % label)
            continue
        log("%-28s %3d %+9.2f %+9.2f %+9.2f %+9.2f   增量: 整体%+.2f 句末%+.2f"
            % (label, 8, s["med"], s["rng"], s["tail_peak"], s["tail_rng"],
               s["med"] - base["med"], s["tail_peak"] - base["tail_peak"]))
    log("")
    log("判定阈：句末抬高或整体音高增量 ≥ 1 半音才算听得出。")
    log("ref 自检见 stage1 输出；波形在 _ab_out/richref_instruct|plain/。")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("1", "all"):
        log("=== 阶段1 CustomVoice 合混合语调 ref ===")
        stage1()
    if stage in ("2", "all"):
        log("=== 阶段2 Base ICL 用新 ref 合成测试句 ===")
        stage2()
    if stage in ("3", "all"):
        log("=== 阶段3 指标对比 ===")
        stage3()
