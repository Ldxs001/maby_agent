# -*- coding: utf-8 -*-
"""修正版：用产品真实种子口径（seed_for(text, 音色名)）测同文本换种子的波动。

上一版探针把 seed_for 的第二参数传成 "B"，而产品传的是音色名 "Serena"，
两者派生出完全不同的种子 → 基准版本对不上产物。本版修正。
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

import serve  # noqa: E402

SCRIPT = os.path.join(ROOT, "projects/20260915-103249/脚本/1.json")
AUDIO = os.path.join(ROOT, "projects/20260915-103249/过程/1/audio")
OUT = os.path.join(ROOT, "_smoke", "_seed_probe2")
N = 6
VOICE = "Serena"
PICKS = [("0003", 3), ("0019", 19)]


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
    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    eng.ensure()

    rows = []
    for tag, idx in PICKS:
        text = script[idx]["text"]
        s0 = serve.seed_for(text, VOICE)          # ← 产品口径
        print("\n" + "=" * 72)
        print("%s_B  产品种子 seed_for(text, '%s') = %d" % (tag, VOICE, s0))
        print("  「%s」" % text)
        print("=" * 72)
        for k in range(N):
            seed = s0 + k
            samples, sr = eng.synth_one(text, VOICE, instruct=None,
                                        language="Chinese", seed=seed)
            fn = "%s_s%d.wav" % (tag, k)
            save_wav(samples, sr, os.path.join(OUT, fn))
            print("    +%d  种子 %-11d  时长 %5.2fs" % (k, seed, len(samples) / float(sr)))
            rows.append({"tag": tag, "idx": idx, "k": k, "seed": seed,
                         "file": fn, "text": text,
                         "dur": len(samples) / float(sr)})

    # ---- k=0 是否等于产物 ----
    print("\n" + "=" * 72)
    print("k=0 与既有产物的关系（验证种子口径是否修对）")
    print("=" * 72)
    for tag, idx in PICKS:
        regen = os.path.join(OUT, "%s_s0.wav" % tag)
        prod = os.path.join(AUDIO, "%04d_B.wav" % idx)
        import subprocess
        out44 = os.path.join(OUT, "%s_s0_44k.wav" % tag)
        subprocess.run(["ffmpeg", "-y", "-i", regen, "-af",
                        "aresample=44100,aformat=channel_layouts=mono", out44],
                       capture_output=True)
        with wave.open(prod, "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
        with wave.open(out44, "rb") as w:
            b = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
        same = "n/a"
        if len(a) == len(b):
            same = "完全一致" if np.abs(a - b).max() == 0 else "不一致(最大差 %.0f)" % np.abs(a - b).max()
        print("  %s_B: 产物 %d 样本 / 复现 %d 样本 → %s" % (tag, len(a), len(b), same))

    with io.open(os.path.join(OUT, "seeds2.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("\n产物: %s" % OUT)


if __name__ == "__main__":
    main()
