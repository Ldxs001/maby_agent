# -*- coding: utf-8 -*-
"""退化率：同一句话换 20 个种子，各温度下有几句会跑到 token 上限。

为什么必须查这个：服务端的种子由 (文本, 音色) 派生，所以「某一句炸」是确定的
——重跑、重启都一样。一批 400 句里只要命中几个，就会混进几段两分半的噪声，而
用户只会听到「有一句不对劲」。要回答两件事：

  1. 温度越低，退化概率是不是越高（分布越尖，进循环越出不来）；
  2. 若确实如此，说明「压温度」与「防退化」是一对矛盾，必须另外加保护。

失控判据：时长 > 25 秒（文本 39 字，正常约 9 秒）。
"""
import hashlib
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
INSTRUCT = "用好奇的语气说"
TEMPS = (0.9, 0.4, 0.2)
N_SEED = 20
BAD_SECONDS = 25.0


def seed_from(tag):
    """按服务端的算法派生种子，样本才和线上同分布。"""
    h = hashlib.sha256(("%s\x00%s" % (SPEAKER, TEXT + tag)).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def main():
    from faster_qwen3_tts import FasterQwen3TTS

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("加载模型 … device=%s" % dev, flush=True)
    m = FasterQwen3TTS.from_pretrained(MODEL, device=dev)
    print("就绪\n", flush=True)

    def gen(temp, seed):
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        wavs, sr = m.generate_custom_voice(
            text=TEXT, speaker=SPEAKER, language="Chinese",
            instruct=INSTRUCT, temperature=temp, top_k=50, top_p=1.0,
            do_sample=True, repetition_penalty=1.05)
        a = wavs[0] if isinstance(wavs, (list, tuple)) else wavs
        if hasattr(a, "cpu"):
            a = a.cpu().numpy()
        return len(np.asarray(a).reshape(-1)) / float(sr)

    print("预热…\n", flush=True)
    gen(0.4, 7)

    print("文本 %d 字 · 指令 %r · 每温度 %d 个种子" % (len(TEXT), INSTRUCT, N_SEED),
          flush=True)
    print("%-7s %-9s %-9s %-9s %s" % ("温度", "失控句数", "中位时长", "最长",
                                      "失控种子（前 4 个）"), flush=True)
    for temp in TEMPS:
        durs, bad = [], []
        for k in range(N_SEED):
            d = gen(temp, seed_from("·%d" % k))
            durs.append(d)
            if d > BAD_SECONDS:
                bad.append(seed_from("·%d" % k))
        print("%-7.1f %-9s %-9.2f %-9.2f %s" % (
            temp, "%d/%d" % (len(bad), N_SEED), float(np.median(durs)),
            max(durs), bad[:4]), flush=True)
    print("\n完成", flush=True)


if __name__ == "__main__":
    main()
