# -*- coding: utf-8 -*-
"""定位：加语气指令后输出时长炸到 token 上限，是温度造成的，还是指令本身？

线索：
  · 无语气时一切正常（8 秒），带「用好奇的语气说」变成 160 秒 —— 撞上 token 上限；
  · 官方 README 的示例句式就是「用特别愤怒的语气说」，格式没错；
  · 官方 generation_config 的默认温度是 0.9，我们把生产温度压到了 0.2。

假设：极低温度让分布太尖，一旦落进重复循环就出不来（repetition_penalty=1.05 太弱）。
这里把「温度 × 指令」拉成矩阵，每格两个种子，看哪一格开始失控。
"""
import os
import sys

import numpy as np
import torch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")
SPEAKER = "Vivian"
TEXT = ("今天我们来聊一个有点意思的话题：为什么有些决定，"
        "明明想清楚了，事后还是会后悔？")
TEMPS = (0.9, 0.5, 0.4, 0.2)
INSTRUCTS = (None, "用好奇的语气说", "用平静的语气说")


def main():
    from faster_qwen3_tts import FasterQwen3TTS

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("加载模型 … device=%s" % dev, flush=True)
    m = FasterQwen3TTS.from_pretrained(MODEL, device=dev)
    print("就绪\n", flush=True)

    def gen(temp, instruct, seed):
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        kwargs = {"text": TEXT, "speaker": SPEAKER, "language": "Chinese",
                  "temperature": temp, "top_k": 50, "top_p": 1.0,
                  "do_sample": True, "repetition_penalty": 1.05}
        if instruct:
            kwargs["instruct"] = instruct
        wavs, sr = m.generate_custom_voice(**kwargs)
        a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
        if hasattr(a, "cpu"):
            a = a.cpu().numpy()
        a = np.asarray(a).reshape(-1)
        return len(a) / float(sr)

    print("预热…\n", flush=True)
    gen(0.4, None, 7)

    print("文本 %d 字（正常时长约 8 秒）" % len(TEXT), flush=True)
    print("%-7s %-20s %s" % ("温度", "指令", "两次时长（秒）"), flush=True)
    for temp in TEMPS:
        for ins in INSTRUCTS:
            durs = [gen(temp, ins, 1000 + k) for k in (1, 2)]
            flag = "  ← 失控" if any(d > 30 for d in durs) else ""
            print("%-7.1f %-20s %s%s" % (
                temp, ins or "(无)", "  ".join("%8.2f" % d for d in durs), flag),
                flush=True)
    print("\n完成", flush=True)


if __name__ == "__main__":
    main()
