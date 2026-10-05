# -*- coding: utf-8 -*-
"""情绪指令（instruct）对照探针 · 判断情感词要不要加、加到什么程度。

背景
----
Qwen3-TTS 的 instruct 是自由自然语言，官方**没有**情绪枚举、也没有强度刻度
（模型卡只有 get_supported_speakers / get_supported_languages 两个枚举接口）。
脚本侧那套「平静/好奇/感慨…」标签是我们自己造的，跟模型之间没有契约。
现在 build_instruct 把它翻成「用{X}的语气说」，不加任何程度修饰。

那就得问清楚两件事：
  1. 「用感慨的语气说」这类指令，比不给指令，真的改变了什么吗？
  2. 加上程度词（轻/中/重）会不会更有效，还是只是噪音？

对照设计（唯一变量是 instruct）
------------------------------
服务端种子 = sha256(speaker + text)[:4]，**与 instruct 无关**。所以同一句文本、
同一音色下，五个档位共用同一个种子 —— 组间差别只能来自 instruct 本身，干净。

    none   不给任何情绪指令
    bare   「用感慨的语气说」        ← 现状口径
    light  「用略带感慨的语气说」
    mid    「用明显感慨的语气说」
    heavy  「用非常感慨的语气说」

指标（全部客观可算，不靠耳朵听）
    dur       时长（秒）
    rate      语速（字/秒）
    rms       整体响度（dBFS）
    rms_std   帧能量标准差 —— 越大越有起伏
    f0_mean   基频均值（Hz）
    f0_std    基频标准差（Hz）—— 情绪波动的主要声学代理
    centroid  谱质心均值（Hz）—— 音色亮度

用法
----
    python tools/probes/probe_emotion_instruct.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import wave

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "_emo_probe")
HOST = "http://127.0.0.1:9880"
SPEAKER = "Serena"

#: 五档指令。顺序即表格列序。
LEVELS = [
    ("none", None),
    ("bare", "用感慨的语气说"),
    ("light", "用略带感慨的语气说"),
    ("mid", "用明显感慨的语气说"),
    ("heavy", "用非常感慨的语气说"),
]

#: 四句文本：从「情绪中性」到「文本自身就带情绪」，看标签的作用是否被文本淹没。
TEXTS = [
    ("中性", "这件事在当时并没有引起太多人的注意。"),
    ("感慨", "我们花了整整三年，才把这个问题想明白。"),
    ("疑问", "如果当初换一条路，结果会不会完全不同？"),
    ("肯定", "说到底，工具只是工具，真正要紧的是用它的人。"),
]


# --------------------------------------------------------------------------- #
# 音频指标
# --------------------------------------------------------------------------- #

def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def rms_stats(a, sr, frame_s=0.02):
    """整体响度（dBFS）+ 帧能量标准差。"""
    frame = max(1, int(frame_s * sr))
    usable = len(a) - len(a) % frame
    if usable <= 0:
        return -120.0, 0.0
    blocks = a[:usable].reshape(-1, frame)
    vals = np.sqrt((blocks ** 2).mean(axis=1))
    total = float(np.sqrt((a ** 2).mean()))
    return (20.0 * float(np.log10(total + 1e-9)), float(vals.std()))


def estimate_f0(a, sr, fmin=60.0, fmax=400.0, frame_s=0.04, hop_s=0.02):
    """自相关估基频，只统计有声帧。返回 Hz 数组。"""
    frame, hop = int(frame_s * sr), int(hop_s * sr)
    lo, hi = int(sr / fmax), int(sr / fmin)
    out = []
    for i in range(0, max(1, len(a) - frame), hop):
        seg = a[i:i + frame]
        if seg.size < frame:
            break
        if float(np.sqrt((seg ** 2).mean())) < 0.01:
            continue
        seg = seg - seg.mean()
        n = seg.size
        spec = np.fft.rfft(seg, 2 * n)
        ac = np.fft.irfft(spec * np.conj(spec))[:n]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg_ac = ac[lo:min(hi, ac.size)]
        if seg_ac.size == 0:
            continue
        k = int(np.argmax(seg_ac)) + lo
        if k <= 0 or ac[k] < 0.3:      # 周期性不足 → 判为无音高
            continue
        out.append(sr / float(k))
    return np.asarray(out, dtype=np.float32)


def spectral_centroid(a, sr, frame=1024, hop=512):
    """谱质心均值（Hz）—— 音色亮度的粗代理。"""
    win = np.hanning(frame)
    freqs = np.fft.rfftfreq(frame, 1.0 / sr)
    vals = []
    for i in range(0, max(1, len(a) - frame), hop):
        seg = a[i:i + frame] * win
        if float(np.sqrt((seg ** 2).mean())) < 0.01:
            continue
        mag = np.abs(np.fft.rfft(seg))
        s = float(mag.sum())
        if s <= 0:
            continue
        vals.append(float((freqs * mag).sum() / s))
    return float(np.mean(vals)) if vals else 0.0


def measure(path, n_chars):
    a, sr = read_wav(path)
    dur = len(a) / float(sr)
    dbfs, rms_std = rms_stats(a, sr)
    f0 = estimate_f0(a, sr)
    return {
        "dur": round(dur, 3),
        "rate": round(n_chars / dur, 3) if dur > 0 else 0.0,
        "rms": round(dbfs, 2),
        "rms_std": round(rms_std, 5),
        "f0_mean": round(float(f0.mean()), 1) if f0.size else 0.0,
        "f0_std": round(float(f0.std()), 1) if f0.size else 0.0,
        "f0_n": int(f0.size),
        "centroid": round(spectral_centroid(a, sr), 1),
    }


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #

def wait_health(timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(HOST + "/health", timeout=10) as r:
                st = json.loads(r.read().decode("utf-8"))
            if st.get("loaded"):
                return st
            print("  …等待模型加载（%ds）" % int(time.time() - t0), flush=True)
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(10)
    raise SystemExit("服务未就绪，超时 %ds" % timeout)


def synth(text, instruct, out_path):
    body = {"text": text, "voice": SPEAKER, "speed": 1.0}
    if instruct:
        body["instruct"] = instruct
    req = urllib.request.Request(
        HOST + "/tts", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as resp:
        raw = resp.read()
    with open(out_path, "wb") as f:
        f.write(raw)
    return round(time.time() - t0, 2), hashlib.md5(raw).hexdigest()[:10]


def main():
    os.makedirs(OUT, exist_ok=True)
    print("等待服务就绪 …", flush=True)
    st = wait_health()
    print("服务就绪：%s / %s\n" % (st.get("device"), st.get("model")))

    rows = []
    total = len(TEXTS) * len(LEVELS)
    n = 0
    for tag, text in TEXTS:
        for level, instruct in LEVELS:
            n += 1
            path = os.path.join(OUT, "%s_%s.wav" % (tag, level))
            if not os.path.exists(path):
                try:
                    secs, md5 = synth(text, instruct, path)
                except urllib.error.HTTPError as e:
                    print("[%d/%d] %s/%s 失败：%s"
                          % (n, total, tag, level, e.read()[:200]))
                    continue
            else:
                secs, md5 = 0.0, "-"
            m = measure(path, len(text))
            m.update({"text_tag": tag, "level": level, "instruct": instruct or "",
                      "gen_secs": secs, "md5": md5})
            rows.append(m)
            print("[%2d/%d] %-4s %-6s %5.2fs · 响度 %6.2f · F0 %5.1f±%4.1f · 质心 %6.0f"
                  % (n, total, tag, level, m["dur"], m["rms"],
                     m["f0_mean"], m["f0_std"], m["centroid"]), flush=True)

    # 确定性校验：同输入同种子应当逐字节一致（种子与 instruct 无关）
    tag, text = TEXTS[0]
    p = os.path.join(OUT, "%s_%s.wav" % (tag, LEVELS[1][0]))
    p2 = os.path.join(OUT, "%s_%s_again.wav" % (tag, LEVELS[1][0]))
    _s, md5b = synth(text, LEVELS[1][1], p2)
    same = md5b == next(r["md5"] for r in rows
                        if r["text_tag"] == tag and r["level"] == LEVELS[1][0])
    print("\n确定性校验（同句同档重跑）：%s" % ("逐字节一致" if same else "不一致"))

    res = {"speaker": SPEAKER, "levels": [lv for lv, _ in LEVELS],
           "deterministic": same, "rows": rows}
    with io.open(os.path.join(HERE, "_emo_probe.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    # ---- 汇总表：每句各档位相对 none 的增量 -------------------------------- #
    print("\n" + "=" * 96)
    print("各句 × 各档位（F0 标准差 / 响度 / 语速 / 时长）")
    print("=" * 96)
    print("%-4s %-6s %6s %7s %8s %8s %7s" %
          ("文本", "档位", "时长", "语速", "F0std", "响度", "质心"))
    for tag, _t in TEXTS:
        base = next((r for r in rows if r["text_tag"] == tag and r["level"] == "none"), None)
        for level, _i in LEVELS:
            r = next((x for x in rows if x["text_tag"] == tag and x["level"] == level), None)
            if not r:
                continue
            mark = ""
            if base:
                d = r["f0_std"] - base["f0_std"]
                mark = " (%+.1f)" % d
            print("%-4s %-6s %6.2f %7.2f %7.1f%-7s %6.2f %7.0f" %
                  (tag, level, r["dur"], r["rate"], r["f0_std"], mark,
                   r["rms"], r["centroid"]))
        print("-" * 96)

    print("\n结果写入 _smoke/_emo_probe.json，音频在 _smoke/_emo_probe/")


if __name__ == "__main__":
    main()
