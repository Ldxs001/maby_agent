# -*- coding: utf-8 -*-
"""富语调 ref 第二轮：修 ref 残废问题 + 按句型路由 instruct 的矩阵实验。

第一轮事故（已定根因）：CustomVoice 带 instruct 合成时未正常收尾，
ref_mixed_instruct.wav = 5.4s 语音 + 15s 静音垃圾；该残废 ref 使 Base ICL
每句输出结尾复读 ref 最后一句（"这个结果太让人吃惊了"），且复读段在句尾，
导致第一轮"句末抬高 +7.44"量到的是复读句而非疑问句——PASS 作废。

本轮硬修：
  1  ref 合成收紧 token 预算（len*5）+ 能量法裁掉尾部静音 + 自检门禁
     （有声 3.5~8s 且总长 <9.5s，不过门禁直接 FAIL 退出，禁止进入阶段2）
  2  每条输出做复读自检：有声时长 > 预期(字数/4.5s)*1.35 即标记 ECHO 污染
  3  矩阵（全部用同一条修好的混合 ref）：
       q_none      8 句疑问，不传 instruct（对照组）
       q_instruct  同 8 句疑问，传疑问句 instruct
       ex_none     6 句惊叹（脚本里 0 句惊叹句，用通用文案），不传 instruct
       ex_instruct 同 6 句惊叹，传感叹句 instruct
       st_none     2 句陈述，不传 instruct（生产方案：陈述句永不带指令）

运行：tts_service/.venv/Scripts/python.exe tools/probes/probe_richref2.py [1|2|3|all]
产物：_smoke/_richref3/ 与 _smoke/_ab_out/richref3_*/
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
OUT_RICH = os.path.join(HERE, "_richref3")
OUT_BASE = os.path.join(HERE, "_ab_out")

TEMPERATURE = 0.4
SAMPLE_KWARGS = {"temperature": TEMPERATURE, "top_k": 50, "top_p": 1.0,
                 "do_sample": True, "repetition_penalty": 1.05}
MAX_FRAMES, FRAME_FLOOR, FRAMES_PER_CHAR = 2048, 120, 8
SEED = 20260916

# 第三轮根因修复：前两轮把三句一次给 CustomVoice，它有时念两句就停
# （第一轮停下后还灌了 15s 静音），ref_text 却写了三句——模型把没被念出
# 的句子当成待合成文本补说，造成全量复读。
# 修法 = 一次给三句（无指令，标点自带语调）+ 念完验一遍：
#   语速 4.0~6.5 字/s 且 >=3 个语音段；没验过自动重念，最多 4 次。
# 门禁不是限制念多长，是抓"没念全"，缺一句当场 FAIL，不让残废 ref 进阶段2。
MIXED_TEXT = "这本书写得很有意思。这书真的是AI写的吗？这个结果太让人吃惊了！"
Q_INSTRUCT = "请用自然对话的语气朗读，疑问句句尾语调明显上扬。"
EX_INSTRUCT = "请用自然对话的语气朗读，感叹句情绪饱满、力度加强。"

ST_TEXTS = [
    "这条路径的选择并不复杂，关键是先看清成本再决定。",
    "数据本身不会说谎，会骗人的只有解读数据的方式。",
]
EX_TEXTS = [
    "这个效率提升太惊人了，直接翻了一倍！",
    "没想到这次的误差能压到这么低，真漂亮！",
    "这个方案的巧思实在让人拍案叫绝！",
    "一年时间就从原型跑到量产，这速度太猛了！",
    "别忘了，这可是在零下四十度跑出来的成绩！",
    "这么复杂的问题，居然一句话就说透了！",
]


def log(msg):
    print(msg, flush=True)


def max_frames(text):
    return int(min(MAX_FRAMES, max(FRAME_FLOOR, len(text) * FRAMES_PER_CHAR)))


def load_wav(path):
    with wave.open(path) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()),
                          dtype="<i2").astype(np.float64) / 32768.0
    return x, sr


def voiced_mask(x, sr):
    fl, hop = int(sr * 0.025), int(sr * 0.010)
    nfr = max(1, (len(x) - fl) // hop)
    e = np.array([np.sqrt(np.mean(x[i * hop:i * hop + fl] ** 2))
                  for i in range(nfr)])
    return e > e.max() * 0.06, hop / sr


def trim_tail_silence(path, pad_s=0.15):
    """裁掉尾部静音/垃圾，原地重写。返回裁剪后时长。"""
    x, sr = load_wav(path)
    v, step = voiced_mask(x, sr)
    idx = np.where(v)[0]
    if len(idx) == 0:
        return 0.0
    end = min(len(x), int((idx[-1] * step + pad_s) * sr))
    start = max(0, int(max(0, (idx[0] * step - 0.1)) * sr))
    x2 = x[start:end]
    peak = np.max(np.abs(x2)) or 1.0
    if peak > 1.0:
        x2 = x2 / peak
    pcm = (x2 * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return len(x2) / float(sr)


def voiced_dur(path):
    x, sr = load_wav(path)
    v, step = voiced_mask(x, sr)
    return v.sum() * step, len(x) / float(sr)


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
    """三句一次给 CustomVoice（无指令），念完验一遍：语速 4.0~6.5 字/s
    且 >=3 个语音段；没念全自动重念，最多 4 次。不做任何人工长短限制。"""
    os.makedirs(OUT_RICH, exist_ok=True)
    import numpy as np
    import torch
    m = load_faster(os.path.join(MODELS,
                                 "Qwen3-TTS-12Hz-1.7B-CustomVoice"))
    ok_path = None
    try:
        for att in range(1, 5):
            torch.manual_seed(SEED + att * 7)
            torch.cuda.manual_seed_all(SEED)
            out = m.generate_custom_voice(
                text=MIXED_TEXT, language="Chinese", speaker="Vivian",
                max_new_tokens=len(MIXED_TEXT) * 6,  # 有多少念多少，上限只兜底
                **SAMPLE_KWARGS)
            wavs = out[0] if isinstance(out, tuple) else out
            sr = out[1] if isinstance(out, tuple) else 24000
            p = os.path.join(OUT_RICH, "ref_mixed.wav")
            save_wav(p, wavs[0] if hasattr(wavs, "__len__") and
                     not hasattr(wavs, "dtype") else wavs, sr)
            trim_tail_silence(p)
            vd, tot = voiced_dur(p)
            rate = len(MIXED_TEXT) / vd if vd > 0.1 else 99.0
            nseg = n_segments(p)
            gate = 4.0 <= rate <= 6.5 and nseg >= 3
            log("  尝试%d  总长%.2fs 有声%.2fs 语速%.1f字/s 语音段%d  门禁:%s"
                % (att, tot, vd, rate, nseg, "PASS" if gate else "FAIL"))
            if gate:
                ok_path = p
                break
    finally:
        unload(m)
    if not ok_path:
        log("GATE FAIL：4 次尝试都没念全三句，禁止进入阶段2。")
        sys.exit(1)
    io.open(os.path.join(OUT_RICH, "ref_mixed.txt"), "w",
            encoding="utf-8").write(MIXED_TEXT)
    log("  ref_mixed.wav 定稿（%d 次尝试）" % att)


def n_segments(path, gap_s=0.3):
    """语音段数（间隔 <0.3s 视为同段）——三句话至少要出现三段。"""
    x, sr = load_wav(path)
    v, step = voiced_mask(x, sr)
    segs, s = [], None
    for i, vv in enumerate(v):
        if vv and s is None:
            s = i
        if not vv and s is not None:
            if (i - s) * step > 0.15:
                segs.append((s, i))
            s = None
    if s is not None:
        segs.append((s, len(v)))
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < gap_s / step:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return len(merged)


def load_texts():
    """主实验同款 8 句疑问。"""
    sc = json.load(io.open(os.path.join(
        ROOT, "projects", "20260915-103249", "脚本", "1.json"),
        encoding="utf-8"))
    lines = sc if isinstance(sc, list) else (sc.get("lines") or [])
    qs = [(l.get("text") or "").rstrip() for l in lines
          if (l.get("text") or "").rstrip().endswith("？")
          and 12 <= len((l.get("text") or "").rstrip()) <= 34]
    return qs[:8]


def stage2():
    """Base + 修好的混合 ref，按句型路由 instruct 的矩阵。"""
    import torch
    ref = os.path.join(OUT_RICH, "ref_mixed.wav")
    rtxt = io.open(os.path.join(OUT_RICH, "ref_mixed.txt"),
                   encoding="utf-8").read()
    vd, tot = voiced_dur(ref)
    rate = len(rtxt) / vd if vd > 0.1 else 99.0
    if not (4.0 <= rate <= 6.5):
        log("GATE FAIL：ref 语速 %.1f 字/s 超出人声区间（有声%.2fs / %d字），"
            "先跑阶段1。" % (rate, vd, len(rtxt)))
        sys.exit(1)

    qs = load_texts()
    exs = EX_TEXTS
    sts = ST_TEXTS
    conds = []
    for i, t in enumerate(qs):
        conds.append(("q_none", "q%02d" % (i + 1), t, None))
        conds.append(("q_instruct", "q%02d" % (i + 1), t, Q_INSTRUCT))
    for i, t in enumerate(exs):
        conds.append(("ex_none", "ex%02d" % (i + 1), t, None))
        conds.append(("ex_instruct", "ex%02d" % (i + 1), t, EX_INSTRUCT))
    for i, t in enumerate(sts):
        conds.append(("st_none", "st%02d" % (i + 1), t, None))

    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    m = load_faster(os.path.join(MODELS, "Qwen3-TTS-12Hz-1.7B-Base"))
    for cond, name, t, ins in conds:
        outdir = os.path.join(OUT_BASE, "richref3_" + cond)
        os.makedirs(outdir, exist_ok=True)
        torch.manual_seed(SEED + (cond + name).__hash__() % 100000)
        torch.cuda.manual_seed_all(SEED)
        kw = dict(SAMPLE_KWARGS)
        if ins:
            kw["instruct"] = ins
        out = m.generate_voice_clone(
            text=t, language="Chinese", ref_audio=ref, ref_text=rtxt,
            xvec_only=False, max_new_tokens=max_frames(t), **kw)
        wavs = out[0] if isinstance(out, tuple) else out
        sr = out[1] if isinstance(out, tuple) else 24000
        p = os.path.join(outdir, name + ".wav")
        save_wav(p, wavs[0] if hasattr(wavs, "__len__") and
                 not hasattr(wavs, "dtype") else wavs, sr)
        vd, tot = voiced_dur(p)
        exp = len(t) / 4.5
        flag = "  <-- ECHO?" if vd > exp * 1.35 else ""
        log("  [%-11s] %s  有声%.2fs/预期%.1fs%s"
            % (cond, name, vd, exp, flag))
    unload(m)


def stage3():
    """五维指标 + 复读污染统计 + 判定。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_rich", os.path.join(HERE, "_intonation_rich.py"))
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)

    import glob
    import statistics as st

    def summ(d, endswith="?.wav"):
        rows, echo = [], 0
        for p in sorted(glob.glob(os.path.join(d, "*.wav"))):
            x, sr = load_wav(p)
            v, step = voiced_mask(x, sr)
            vd = v.sum() * step
            # 从文件名取不到字数，统一按目录对应文本集合判
            rows.append(R.rich(p))
        rows = [r for r in rows if r]
        if not rows:
            return None, 0
        return {k: st.median([r[k] for r in rows])
                for k in ("med", "rng", "tail_peak", "tail_rng",
                          "end10_vs_25")}, len(rows)

    def texts_of(cond):
        if cond.startswith("q"):
            return load_texts()
        if cond.startswith("ex"):
            return EX_TEXTS
        return ST_TEXTS

    def echo_count(cond):
        d = os.path.join(OUT_BASE, "richref3_" + cond)
        n = 0
        for p, t in zip(sorted(glob.glob(os.path.join(d, "*.wav"))),
                        texts_of(cond)):
            vd, tot = voiced_dur(p)
            if vd > (len(t) / 4.5) * 1.35:
                n += 1
        return n

    log("=" * 92)
    log("第二轮（修好 ref 后）：中位数（半音，相对 200Hz）")
    log("=" * 92)
    log("%-16s %3s %9s %9s %9s %9s %6s"
        % ("条件", "n", "整体音高", "音域", "句末抬高", "句末音域", "ECHO"))
    for cond in ("q_none", "q_instruct", "ex_none", "ex_instruct",
                 "st_none"):
        d = os.path.join(OUT_BASE, "richref3_" + cond)
        s, n = summ(d)
        if not s:
            log("%-16s  无数据" % cond)
            continue
        log("%-16s %3d %+9.2f %+9.2f %+9.2f %+9.2f %4d"
            % (cond, n, s["med"], s["rng"], s["tail_peak"],
               s["tail_rng"], echo_count(cond)))
    log("")
    log("判定阈：组内增量（instruct 相对 none，同句型）句末抬高或整体音高 "
        ">= 1 半音才算听得出。")
    log("注：任何条件若 ECHO>0，其句末指标视为污染，不作数。")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("1", "all"):
        log("=== 阶段1 重合混合 ref（收紧预算+裁尾+门禁） ===")
        stage1()
    if stage in ("2", "all"):
        log("=== 阶段2 Base ICL 句型路由矩阵 ===")
        stage2()
    if stage in ("3", "all"):
        log("=== 阶段3 指标对比 ===")
        stage3()
