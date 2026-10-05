#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MusicGen 批量生成播客背景音乐（一次性资产生产，不进管线）。v2

相对 v1 的三处修正（针对用户反馈：开头大噪音 / 质量差 / 循环点能听出来）：
1. musicgen-small(300M) -> musicgen-medium(1.5B)，fp16 跑 8GB 卡；
2. 生成 40s 后先裁掉开头 3s——MusicGen 的攻击性 transient 集中在开头，
   v1 把它原样留进了循环体，且循环点正好压在开头，等于每 30s 炸一次；
3. crossfade 2s -> 4s，重叠区加长后衔接点能量曲线更平，听感无接缝。

输出 _bgm_pack/<preset>.wav（母带）+ <preset>.mp3（AIGC 打标副本，
由外部脚本处理，本脚本只出 wav）。

v3 修正（针对「换一档就听不清」）
--------------------------------
原先 `save_wav()` 只做**削峰保护**（`peak > 0.90` 才缩），不做响度归一 ——
每档各削各的，重的被压、轻的原样留着。实测 15 档 integrated LUFS 从
`chimes -15.18` 到 `horror -28.76`，相差 **13.6 LU**：同一个 `bgm.volume`
在不同档位上响度差 4 倍。现在落盘即调 `bgm_loudness.normalize_file()`
按 EBU R128 归一（默认 -23.0 LUFS / 真峰值上限 -1.0 dBTP），**生成出来的
素材天然就是平的一档**，不需要再手工跑 `tools/bgm_level.py` 事后校正。
归一失败会**删掉半成品并抛错**，绝不留下未归一的文件（`main()` 会跳过
已存在的文件，留下就等于永久跳过）。

    python tools/probes/bgm_gen.py                        # 用默认目标生成
    python tools/probes/bgm_gen.py --target-lufs -24      # 换目标响度
"""

import os
import sys
import wave
import array

import torch
from transformers import MusicgenForConditionalGeneration
from transformers import AutoProcessor

# 响度归一与 tools/bgm_level.py 共用同一套实现，避免出现两个标准。
_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "tools"))
try:
    import bgm_loudness
    _NORM_IMPORT_ERROR = None
except ImportError as _exc:                     # 缺工具模块时宁可炸
    bgm_loudness = None
    _NORM_IMPORT_ERROR = _exc

OUT = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"), "_bgm_pack")
SR = 32000             # musicgen 原生采样率
GEN_SEC = 32           # GPU 长生成（3200 token）在本机必崩（small fp32 /
TRIM_SEC = 3           # medium 半精度 ×多轮复现），1600 token 稳定区回退：
XFADE_SEC = 4          # 32s 原料 - 3s 裁头 - 4s crossfade = 25s 循环体
TOKENS_PER_SEC = 50    # musicgen 每 1 秒音频 ≈ 50 audio tokens

# 方向定「轻音乐」不是环境音：有乐器、有和弦进行、有旋律句，
# 背景化靠「soft / gentle / mellow」约束，不靠「drone / texture」——
# 那类词生成出来就是没有起伏的嗡鸣垫。
PRESETS = {
    "still":   "Gentle instrumental music, soft felt piano melody, warm "
               "string pad underneath, slow chord progression, peaceful and "
               "smooth, quiet dynamics, no drums, no vocals",
    "pensive": "Thoughtful soft piano piece, mellow warm harmonies, gentle "
               "melodic phrases with breathing space, intimate and calm, "
               "no drums, no vocals, background instrumental",
    "bright":  "Light cheerful acoustic instrumental, gentle plucked guitar "
               "melody, warm ukulele and soft strings, relaxed happy groove, "
               "smooth and easygoing, no heavy drums, no vocals",
    "deep":    "Emotional cinematic instrumental, low cello melody over warm "
               "strings, slow moving harmonies, film score mood, deep and "
               "reflective, gentle dynamics, no drums, no vocals",
    # ---- 扩展风格库（轻音乐基底 + 主题元素点缀）----
    "chimes":  "Delicate instrumental music, metallic glass chimes and "
               "jade-like bells over a soft piano bed, shimmering gentle "
               "textures, elegant and serene, no heavy drums, no vocals",
    "blades":  "Tense instrumental music with cold metallic resonance, "
               "subtle steel and sword-like shimmer accents over a dark "
               "string bed, sharp and disciplined, cinematic, no vocals",
    "summer":  "Warm gentle acoustic music with subtle summer cicada and "
               "cricket sounds woven in, nostalgic and relaxed, soft guitar "
               "and light percussion, peaceful evening mood, no vocals",
    "frost":   "Cold crystalline instrumental, icy bell-like piano notes, "
               "frozen glassy textures, delicate snow atmosphere, sparse "
               "and clean, quiet winter mood, no drums, no vocals",
    "nature":  "Peaceful acoustic music with soft forest stream and gentle "
               "bird sounds woven in, warm woodwinds and guitar, fresh and "
               "organic, morning atmosphere, no vocals",
    "warm":    "Cozy warm instrumental, soft piano and gentle acoustic "
               "guitar, heartfelt and tender, golden afternoon light mood, "
               "smooth and inviting, no drums, no vocals",
    "horror":  "Dark eerie horror soundscape, dissonant strings and low "
               "unsettling drones, tense suspenseful atmosphere, creepy "
               "film score, sparse and haunting, no vocals",
    "mech":    "Industrial mechanical instrumental, rhythmic metallic "
               "percussion and ticking gears, driving steady pulse, cold "
               "precise mood, cinematic machine atmosphere, no vocals",
    "tech":    "Sci-fi electronic instrumental, smooth synth pads with "
               "gentle digital pulses, futuristic clean atmosphere, sleek "
               "and intelligent mood, light groove, no vocals",
    "chaos":   "Chaotic tense instrumental, layered conflicting textures, "
               "unstable rhythms and dissonant intervals, anxious restless "
               "energy, still controlled cinematic, no vocals",
    "riot":    "Intense agitated instrumental, aggressive percussion and "
               "urgent driving strings, uprising energy, powerful and "
               "dramatic film score, heavy tension, no vocals",
}


def seamless_loop(samples, trim_sec, xfade_sec):
    """裁掉开头 transient，再首尾交叉淡化成无缝循环体。"""
    x = samples[trim_sec * SR:]
    n = x.shape[0]
    body = n - xfade_sec * SR
    out = x[:body].clone()
    for i in range(xfade_sec * SR):
        t = i / float(xfade_sec * SR)      # 0→1
        out[i] = x[body + i] * (1.0 - t) + x[i] * t
    return out


def save_wav(path, samples, target_lufs=None, tp_ceil=None):
    """写盘，然后按 EBU R128 归一响度。

    两步职责不同，不要混为一谈：

    1. **防溢出**：float → int16 之前必须把峰值压进 `[-1, 1)`，否则整数回绕
       会产生刺耳异响。这一步只是保护，**不是归一** —— 它把重的素材压下来、
       轻的原样留着，正是 15 档不齐 13.6 LU 的成因。
    2. **归一**：写盘后调 `bgm_loudness.normalize_file()`，按 integrated LUFS
       拉到目标响度，增益再受真峰值上限约束。失败即删文件并抛错。

    `target_lufs=None` 表示不做归一（只在显式传参时发生，正常路径不走到）。
    """
    x = samples.detach().cpu().numpy().squeeze()
    peak = max(1e-6, float(abs(x).max()))
    if peak > 0.90:
        x = x * (0.90 / peak)
    pcm = array.array("h", (x * 32767).astype("int16").tolist())
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())

    if target_lufs is None:
        return None
    if bgm_loudness is None:
        os.remove(path)
        raise RuntimeError(
            "响度归一模块不可用（%s）：%s —— 已删除未归一的半成品，"
            "拒绝产出不齐的素材。" % (os.path.join(_ROOT, "tools"), _NORM_IMPORT_ERROR))

    res = bgm_loudness.normalize_file(path, target_lufs=target_lufs,
                                      tp_ceil=tp_ceil)
    if not res.get("ok"):
        os.remove(path)          # main() 会跳过已存在文件，留下等于永久跳过
        raise RuntimeError("响度归一失败：%s —— %s"
                           % (os.path.basename(path), res.get("reason")))
    return res


def main():
    import argparse

    ap = argparse.ArgumentParser(description="MusicGen 批量生成播客背景音乐")
    ap.add_argument("--target-lufs", type=float,
                    default=getattr(bgm_loudness, "DEFAULT_TARGET_LUFS", -23.0),
                    help="归一目标 integrated loudness（默认 %g）"
                         % getattr(bgm_loudness, "DEFAULT_TARGET_LUFS", -23.0))
    ap.add_argument("--tp-ceil", type=float,
                    default=getattr(bgm_loudness, "DEFAULT_TP_CEIL", -1.0),
                    help="真峰值上限 dBTP（默认 %g）"
                         % getattr(bgm_loudness, "DEFAULT_TP_CEIL", -1.0))
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    print("响度归一：目标 %.2f LUFS / 真峰值上限 %.2f dBTP"
          % (args.target_lufs, args.tp_ceil), flush=True)
    # 用户拍板：medium 弃用（GPU 半精度长生成必崩 ×6 轮复现；fp32 装不下
    # 8GB 卡；device_map 混合 25min/档比纯 CPU 还慢），换回 small GPU fp32
    # 先出片听效果。v2 的后处理改进（裁头/crossfade/15 档 prompt）全保留。
    print("loading musicgen-small (cuda, fp32)...", flush=True)
    model = MusicgenForConditionalGeneration.from_pretrained(
        "facebook/musicgen-small", torch_dtype=torch.float32).to("cuda")
    model.eval()
    model.generation_config.bos_token_id = None
    processor = AutoProcessor.from_pretrained("facebook/musicgen-small")

    for name, prompt in PRESETS.items():
        dst = os.path.join(OUT, "%s.wav" % name)
        if os.path.exists(dst):
            print("skip", name, "(exists)", flush=True)
            continue
        print("generating", name, "...", flush=True)
        inputs = processor(text=[prompt], padding=True, return_tensors="pt")
        inputs = {k: v.to("cuda") for k, v in inputs.items()}
        audio = model.generate(
            **inputs,
            max_new_tokens=GEN_SEC * TOKENS_PER_SEC,
            do_sample=True,
            guidance_scale=3.0,
            top_k=250)
        loop = seamless_loop(audio[0, 0], TRIM_SEC, XFADE_SEC)
        res = save_wav(dst, loop, args.target_lufs, args.tp_ceil)
        print("saved %s  归一 %+.2f dB → %.2f LUFS (Δ%+.2f) / TP %.2f dBTP%s"
              % (dst, res["gain_db"], res["after_lufs"], res["delta"],
                 res["after_tp"], "  ←峰值受限" if res["limited"] else ""),
              flush=True)

        # 显存逐档清理
        del audio, loop, inputs
        torch.cuda.empty_cache()

    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
