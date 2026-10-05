# -*- coding: utf-8 -*-
"""决定性实验：同一句话自己换种子，会不会也「换一个人」。

用本轮用户质疑的两句真实文本，各自固定文本、固定音色(Serena)、固定指令(None)，
只改随机种子，各生成 6 条。

判定：
  - 若 A 文本的 6 条与 B 文本的 6 条在声学指标上大量重叠
    → 「这俩不像一个人」跟文本/情绪/指令都无关，是模型对该音色缺乏约束。
  - 若两组各自内部很紧、组间很远
    → 才是文本造成的。

产物：_smoke/_seed_probe/*.wav + seeds.json
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
OUT = os.path.join(ROOT, "_smoke", "_seed_probe")
N_SEEDS = 6
PICKS = [("A_0003", 3), ("B_0019", 19)]


def save_wav(samples, sr, path):
    a = np.asarray(samples, dtype=np.float64)
    if a.ndim > 1:
        a = a.mean(axis=1)
    a = np.clip(a, -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def main():
    os.makedirs(OUT, exist_ok=True)
    with io.open(SCRIPT, encoding="utf-8") as f:
        script = json.load(f)

    from podcast_maker.config_manager import ConfigManager
    cfg = ConfigManager()
    vb = cfg.get("tts.qwen3tts_voice_b", "Serena")

    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    eng.ensure()

    rows = []
    for tag, idx in PICKS:
        s = script[idx]
        text = s.get("text", "")
        speaker = s.get("speaker", "B")
        s0 = serve.seed_for(text, speaker)
        print("\n" + "=" * 70)
        print("%s  脚本第 %d 句 · %s · %d 字 · 情绪=%s" % (tag, idx, speaker, len(text), s.get("emotion")))
        print("  「%s」" % text)
        print("  基准种子 %d" % s0)
        print("=" * 70)
        for k in range(N_SEEDS):
            seed = s0 + k
            samples, sr = eng.synth_one(text, vb, instruct=None,
                                        language="Chinese", seed=seed)
            p = os.path.join(OUT, "%s_s%d.wav" % (tag, k))
            save_wav(samples, sr, p)
            dur = len(samples) / float(sr)
            print("    种子 %d (+%d)  时长 %5.2fs  → %s" % (seed, k, dur, os.path.basename(p)))
            rows.append({"tag": tag, "idx": idx, "k": k, "seed": seed,
                         "text": text, "voice": vb, "dur": dur,
                         "file": "%s_s%d.wav" % (tag, k)})

    with io.open(os.path.join(OUT, "seeds.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("\n已保存 %d 条样本到 %s" % (len(rows), OUT))


if __name__ == "__main__":
    main()
