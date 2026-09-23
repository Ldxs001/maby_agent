# -*- coding: utf-8 -*-
"""证明：CUDA 图路径下，子码本采样参数收不到、改不动。

两条硬证据
----------
证据 A（静态）  两个包的 `generate_custom_voice` 签名对比
                —— 图包没有 subtalker_* 且没有 **kwargs，传了直接 TypeError。

证据 B（动态）  给 `PredictorGraph._full_loop` 插桩，打印图捕获那一刻
                **真正生效**的子码本采样参数。产品侧传 temperature=0.4，
                若图里打印 0.9，即证明两套参数各走各的、图那套改不动。

附带打印 TalkerGraph（主路图）的属性，确认它**不含**任何采样参数 ——
即主路的 temperature/top_k/top_p/do_sample/repetition_penalty 全部活在 Python 侧。

用法
----
    ./tts_service/.venv/Scripts/python.exe tools/probes/probe_subtalker_ignored.py
"""

from __future__ import annotations

import inspect
import os
import sys

import torch

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "tts_service", "models",
                     "Qwen3-TTS-12Hz-1.7B-CustomVoice")

TEXT = "我们先从原理讲起。"
SPEAKER = "Serena"
#: 产品侧 serve.py 当前实际传的值
PRODUCT_TEMPERATURE = 0.4

SEEN: set = set()


def main() -> int:
    print("=" * 78)
    print("证据 A · 两个包的 generate_custom_voice 签名")
    print("=" * 78)
    from faster_qwen3_tts import FasterQwen3TTS
    from qwen_tts import Qwen3TTSModel

    sig_graph = inspect.signature(FasterQwen3TTS.generate_custom_voice)
    sig_official = inspect.signature(Qwen3TTSModel.generate_custom_voice)
    print("  图包 faster_qwen3_tts :", sig_graph)
    print("  官方包 qwen_tts        :", sig_official)
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in sig_official.parameters.values())
    print("\n  图包接受 subtalker_temperature ?",
          "subtalker_temperature" in sig_graph.parameters)
    print("图包接受任意关键字(**)      ?",
          any(p.kind is inspect.Parameter.VAR_KEYWORD
              for p in sig_graph.parameters.values()))
    print("官方包接受任意关键字(**)    ?", accepts_kwargs)

    print()
    print("=" * 78)
    print("证据 B · 插桩：图里真正生效的子码本采样参数")
    print("=" * 78)

    import faster_qwen3_tts.predictor_graph as pg
    import faster_qwen3_tts.talker_graph as tg

    orig_loop = pg.PredictorGraph._full_loop

    def spy(self):
        key = (self.temperature, self.top_k, self.top_p, self.do_sample)
        if key not in SEEN:
            SEEN.add(key)
            print("  [子码本图] _full_loop 实际使用 → "
                  "temperature=%s  top_k=%s  top_p=%s  do_sample=%s"
                  % (key[0], key[1], key[2], key[3]), flush=True)
        return orig_loop(self)

    pg.PredictorGraph._full_loop = spy

    print("  正在加载模型（CUDA 图会被捕获）…", flush=True)
    eng = FasterQwen3TTS.from_pretrained(MODEL, device="cuda",
                                         dtype=torch.bfloat16)

    print("\n  --- 主路图 TalkerGraph 有没有采样参数 ---")
    tattrs = sorted(a for a in vars(eng.talker_graph)
                    if not a.startswith("_"))
    hits = [a for a in tattrs
            if any(w in a.lower() for w in
                   ("temp", "top_k", "top_p", "sample", "penalty"))]
    print("  属性:", tattrs)
    print("  含采样语义的属性:", hits if hits else "无 —— 图只做前向，采样在 Python 侧")

    print("\n  调用产品同款参数 temperature=%s 生成一句…" % PRODUCT_TEMPERATURE)
    torch.manual_seed(1234)
    wavs, sr = eng.generate_custom_voice(
        text=TEXT, speaker=SPEAKER, language="Chinese", instruct=None,
        max_new_tokens=120, temperature=PRODUCT_TEMPERATURE,
        top_k=50, top_p=1.0, do_sample=True, repetition_penalty=1.05)
    print("  → 生成完成，%d 采样点 @ %d Hz" % (len(wavs[0]), sr))

    print("\n  --- 图包里带 subtalker_temperature 调用 ---")
    try:
        eng.generate_custom_voice(text=TEXT, speaker=SPEAKER,
                                  language="Chinese", instruct=None,
                                  subtalker_temperature=0.1)
        print("  未报错（说明参数被接收）")
    except TypeError as exc:
        print("  TypeError →", exc)

    print()
    print("=" * 78)
    print("判定")
    print("=" * 78)
    for key in sorted(SEEN):
        verdict = ("图里跑的是 %.1f，与产品侧传入的 %.1f 无关"
                   % (key[0], PRODUCT_TEMPERATURE)
                   if abs(key[0] - PRODUCT_TEMPERATURE) > 1e-9
                   else "图里跑的就是产品侧传入的值")
        print("  子码本图生效参数 temperature=%.1f top_k=%d top_p=%.1f "
              "do_sample=%s → %s" % (key[0], key[1], key[2], key[3], verdict))
    print("  三条采样语义属性命中主路图: %s" % (hits if hits else "0 条"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
