# -*- coding: utf-8 -*-
"""句尾语调量化：疑问句到底有没有升调。

读现场那一期（projects/20260915-103249）的逐句音频，按脚本里的标点分组，
量每句的**句尾 F0 走向**。指标两个：

  tail_slope  句尾最后 30% 有声帧的 F0 线性斜率（半音/秒）。升调为正。
  tail_delta  最后 3 帧的中位 F0 减去全句中位 F0（半音）。升调为正。

一个句子不带情绪指令、也不带句型提示时，模型只能靠标点和参考音频的先验出调。
用真产物量一下，看这个先验够不够。
"""
from __future__ import annotations

import glob
import io
import json
import os
import wave

import numpy as np

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
PROJ = os.path.join(ROOT, "projects", "20260915-103249")
SCRIPT = os.path.join(PROJ, "脚本", "1.json")
AUDIO = os.path.join(PROJ, "过程", "1", "audio")


def read_wav(path):
    w = wave.open(path)
    sr = w.getframerate()
    n = w.getnframes()
    raw = w.readframes(n)
    w.close()
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return a, sr


def f0_track(x, sr, fmin=60.0, fmax=400.0, frame=2048, hop=441, floor=0.02):
    """逐帧自相关取 F0。返回 (时间轴, F0 序列)。"""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if a.size < frame:
        return np.array([]), np.array([])
    a = a - a.mean()
    pk = float(np.max(np.abs(a)))
    if pk <= 1e-9:
        return np.array([]), np.array([])
    a = a / pk
    lo, hi = max(1, int(sr / fmax)), min(frame - 1, int(sr / fmin))
    ts, fs = [], []
    for s in range(0, len(a) - frame, hop):
        f = a[s:s + frame]
        if float(np.sqrt(np.mean(f ** 2))) < floor:
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
        ts.append((s + frame / 2.0) / sr)
        fs.append(sr / k)
    return np.asarray(ts), np.asarray(fs)


def semitone(f, ref=200.0):
    return 12.0 * np.log2(np.maximum(np.asarray(f, dtype=np.float64), 1e-6) / ref)


def intonation(x, sr):
    """一句的句尾语调指标。"""
    ts, fs = f0_track(x, sr)
    if fs.size < 8:
        return None
    # 首尾各掐掉一点：句首起音不稳，句尾可能带静音尾巴造成的伪帧
    st = semitone(fs)
    med = float(np.median(st))
    n_tail = max(3, int(round(fs.size * 0.30)))
    tail_t, tail_s = ts[-n_tail:], st[-n_tail:]
    if tail_t.size < 3 or (tail_t[-1] - tail_t[0]) <= 0.05:
        slope = 0.0
    else:
        slope = float(np.polyfit(tail_t - tail_t[0], tail_s, 1)[0])
    last3 = float(np.median(st[-3:]))
    return {
        "frames": int(fs.size),
        "f0_med": float(np.median(fs)),
        "slope": slope,
        "delta": last3 - med,
        "seconds": float(ts[-1] - ts[0]) if ts.size else 0.0,
    }


def group_stats(rows):
    if not rows:
        return {}
    sl = np.asarray([r["slope"] for r in rows], dtype=np.float64)
    dl = np.asarray([r["delta"] for r in rows], dtype=np.float64)
    return {
        "n": len(rows),
        "slope_med": float(np.median(sl)),
        "slope_mean": float(np.mean(sl)),
        "delta_med": float(np.median(dl)),
        "delta_mean": float(np.mean(dl)),
        "rise_ratio": float(np.mean(dl > 0.5)),   # 明显上扬（>0.5 半音）占比
        "fall_ratio": float(np.mean(dl < -0.5)),
    }


def main() -> int:
    script = json.load(io.open(SCRIPT, encoding="utf-8"))
    files = sorted(glob.glob(os.path.join(AUDIO, "*.wav")))
    print("脚本 %d 句 / 音频 %d 个" % (len(script), len(files)))

    # 先查一个反常：不同句子文件大小精确重复，是不是同一条波形
    import hashlib
    by_sha = {}
    for p in files:
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
        by_sha.setdefault(h, []).append(os.path.basename(p))
    dup = {k: v for k, v in by_sha.items() if len(v) > 1}
    print("唯一波形 %d / %d；重复组 %d 个" % (len(by_sha), len(files), len(dup)))
    for k, v in list(dup.items())[:6]:
        print("   同一波形 %s -> %s" % (k, v))

    rows_q, rows_s, rows_e = [], [], []
    detail = []
    for i, item in enumerate(script):
        name = "%04d_%s.wav" % (i, item.get("speaker", "?"))
        path = os.path.join(AUDIO, name)
        if not os.path.isfile(path):
            continue
        x, sr = read_wav(path)
        m = intonation(x, sr)
        if not m:
            continue
        text = str(item.get("text", ""))
        m.update({"i": i, "role": item.get("speaker"), "emotion": item.get("emotion"),
                  "text": text,
                  "pun": text[-1] if text else "",
                  "instruct": item.get("emotion") in (
                      "平静", "好奇", "疑惑", "恍然", "肯定", "感慨", "轻松")})
        detail.append(m)
        if text.endswith("？"):
            rows_q.append(m)
        elif text.endswith("。"):
            rows_s.append(m)
        elif text.endswith("！"):
            rows_e.append(m)

    print()
    for label, rows in (("疑问句 ？", rows_q), ("陈述句 。", rows_s), ("感叹句 ！", rows_e)):
        s = group_stats(rows)
        if not s:
            continue
        print("%-8s n=%3d  句尾斜率中位 %+6.2f 半音/秒  句尾落差中位 %+5.2f 半音  "
              "上扬占比 %.0f%%  下抑占比 %.0f%%"
              % (label, s["n"], s["slope_med"], s["delta_med"],
                 100 * s["rise_ratio"], 100 * s["fall_ratio"]))

    # 疑问句里，带指令与不带指令分开看
    print()
    for label, rows in (("疑问+无指令", [r for r in rows_q if not r["instruct"]]),
                        ("疑问+有指令", [r for r in rows_q if r["instruct"]])):
        s = group_stats(rows)
        if not s:
            continue
        print("%-10s n=%3d  斜率中位 %+6.2f  落差中位 %+5.2f  上扬占比 %.0f%%"
              % (label, s["n"], s["slope_med"], s["delta_med"], 100 * s["rise_ratio"]))

    # 陈述句对照
    s = group_stats(rows_s)
    if s:
        print("%-10s n=%3d  斜率中位 %+6.2f  落差中位 %+5.2f  上扬占比 %.0f%%"
              % ("陈述（对照）", s["n"], s["slope_med"], s["delta_med"], 100 * s["rise_ratio"]))

    # 最典型与最反面的疑问句各列几条
    rows_q.sort(key=lambda r: r["delta"])
    print()
    print("疑问句里句尾**最下抑**的五条（按理该升的）：")
    for r in rows_q[:5]:
        print("  [%d] %s 落差 %+5.2f 半音 斜率 %+6.2f  | %s"
              % (r["i"], r["emotion"], r["delta"], r["slope"], r["text"][:38]))
    print("疑问句里句尾**最上扬**的五条：")
    for r in rows_q[-5:]:
        print("  [%d] %s 落差 %+5.2f 半音 斜率 %+6.2f  | %s"
              % (r["i"], r["emotion"], r["delta"], r["slope"], r["text"][:38]))

    out = os.path.join(HERE, "_intonation.json")
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"question": group_stats(rows_q),
                   "statement": group_stats(rows_s),
                   "exclaim": group_stats(rows_e),
                   "detail": detail,
                   "duplicate_waves": {k: v for k, v in dup.items()}},
                  f, ensure_ascii=False, indent=1)
    print()
    print("明细 ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
