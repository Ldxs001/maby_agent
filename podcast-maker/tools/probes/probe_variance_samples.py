# -*- coding: utf-8 -*-
"""保存可试听样本：真实种子下的新旧差异 + 跨种子的采样方差。

两组：
  A 组（真实重跑会遇到）：固定产品派生种子 s0，off（去指令）vs on（有指令）
  B 组（真实重跑不会遇到，但能说明模型有多飘）：固定 off，换 4 个种子

音频写到 _smoke/_variance_probe/，供生成试听页。
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
OUT = os.path.join(ROOT, "_smoke", "_variance_probe")
INSTRUCT_ON = "用平静的语气说"


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

    real = [s for s in script if s.get("emotion") == "平静"]
    picks = []
    for spk in ("A", "B"):
        got = sorted([s for s in real if s.get("speaker") == spk],
                     key=lambda s: len(s.get("text", "")))
        if got:
            picks.append(got[len(got) // 2])
            picks.append(got[len(got) // 3])
    picks = picks[:4]

    from podcast_maker.config_manager import ConfigManager
    cfg = ConfigManager()
    va = cfg.get("tts.qwen3tts_voice_a", "Vivian")
    vb = cfg.get("tts.qwen3tts_voice_b", "Serena")

    model_id = os.path.join(ROOT, "tts_service", "models",
                            "Qwen3-TTS-12Hz-1.7B-CustomVoice")
    eng = serve.Engine(model_id, "auto", lambda m: print("  " + m))
    print("加载引擎…")
    eng.ensure()

    rows = []
    for idx, s in enumerate(picks, 1):
        text = s.get("text", "")
        speaker = s.get("speaker", "A")
        voice = va if speaker == "A" else vb
        s0 = serve.seed_for(text, speaker)
        print("\n第 %d 句 · %s · %d 字\n  %s" % (idx, speaker, len(text), text))

        # --- A 组：真实种子，条件对照
        for cond, ins in (("off", None), ("on", INSTRUCT_ON)):
            samples, sr = eng.synth_one(text, voice, instruct=ins,
                                        language="Chinese", seed=s0)
            p = os.path.join(OUT, "A%d_%s.wav" % (idx, cond))
            save_wav(samples, sr, p)
            dur = len(samples) / float(sr)
            print("  A组 %-3s 时长 %5.2fs  %s" % (cond, dur, p))

        # --- B 组：固定 off，换种子（仅第 4 句做，作为采样方差展示）
        if idx == 4:
            for k in range(4):
                samples, sr = eng.synth_one(text, voice, instruct=None,
                                            language="Chinese", seed=s0 + k)
                p = os.path.join(OUT, "B_seed%d.wav" % k)
                save_wav(samples, sr, p)
                dur = len(samples) / float(sr)
                print("  B组 种子+%d 时长 %5.2fs  %s" % (k, dur, p))

        rows.append({"idx": idx, "speaker": speaker, "voice": voice,
                     "text": text, "seed": s0, "n_chars": len(text)})

    with io.open(os.path.join(ROOT, "_smoke", "_variance_samples.json"), "w",
                 encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("\n已保存样本到 %s" % OUT)


if __name__ == "__main__":
    main()
