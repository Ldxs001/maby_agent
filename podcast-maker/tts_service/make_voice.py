#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色音色档案 —— 把一段参考音频钉进项目目录。

为什么要有这个脚本
------------------
Base 变体的音色不在模型里，在一段参考音频里。这段音频是整期播客的**音色源头**：
它的质量决定后面每一句；它的内容决定种子，从而决定每一句的波形。所以它不能是
一个飘在临时目录里的文件，必须作为**项目资产**落盘、可追溯、可复制。

档案长这样（目录名由主程序 layout.DIR_VOICE 传入，默认「音色」）：

    <项目目录>/音色/A/ref.wav        参考音频
    <项目目录>/音色/A/ref.txt        转录文本（ICL 模式必须，一行）
    <项目目录>/音色/A/profile.json   溯源信息，含 ref_md5

三件事，各有分工
----------------
  **生成**：用 CustomVoice 变体把内置音色「录」成一段音频。它是个一次性进程 ——
      CustomVoice 与 Base 各占约 3.4GB 显存，8GB 的卡上放不下，所以不常驻。
  **继承**：把另一个项目的音色目录整个复制过来。跨项目音色完全一致靠的是这一步，
      而不是「重新生成一遍再祈祷参数对上」。
  **查账**：打印现有档案的内容指纹与音高，用来判断两个项目的音色是不是同一份。

参考文案是固定的
----------------
`REF_TEXTS` 两条**混合三句型文案**（陈述 + 疑问 + 惊叹各一句）是内置常量，
**不随项目内容变化**。原因有二：音色档案只负责音色，拿当期台词去当参考会让
「换个项目音色就变」；而句型混排是刻意为之——Base 克隆会整条继承 ref 的
韵律先验，混合 ref 让输出全局更生动（实验：疑问句句尾抬高约 +2.7 半音）。
念全靠门禁兜底：语速 + 语音段数不合格自动重念，防止「念两句就停」的残废
音频混进来（那会让每句输出复读缺的那句）。要跨项目一致，源头必须与项目无关。
需要别的文案时用 `--text` 显式指定，那种情况下音色基准会跟着换，自己心里有数。

生成的确定性边界
----------------
同一台机器上，**同一设备、同一后端**下，同一套音色名 + 同一句参考文案生成出的
音频逐字节相同 —— 实测同一目录 --force 重建三次，sha256 全同。重念机制不破这个
边界：尝试序列的种子是固定的（base + 0/7/14/21），哪一次定稿也由确定性波形决定，
所以同输入仍收敛到同一条定稿。换项目不会改变
这个结果，所以「新项目自动录一份」与「从老项目拷一份」在 GPU 上等价。

前提不能省。`pick_device()` 在空闲显存不足时会**静默**退 CPU，而 CPU 路径的数值
结果与 GPU 路径不是同一条（同一套输入，波形不同、F0 也不同）。所以录档案之前
要先确认没有别的模型占着显存；profile.json 里记了 `device`，两份档案音色对不上
时先看这一栏。真要跨项目对齐又要绕过这一切，用 `--from` 继承：复制文件，
不依赖任何数值巧合。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import serve  # noqa: E402

# 固定参考文案。**混合三句型 + 全符号**（2026-10-08 定稿）：陈述 + 疑问 +
# 惊叹各一句的骨架不变（2026-09 实验结论：Base 克隆整条继承 ref 的韵律先验
# ——混合 ref 让输出的疑问句句尾抬高约 +2.7 半音（超 1 半音可辨阈），全局
# 韵律更生动；而给 Base 传 instruct 判死，两轮干净对照一个反向一个无效，
# 官方能力表没骗人），在此之上补齐 ref 里缺失的韵律料：省略号（句中拖停
# 先验）与引语转述（引号包住完整疑问句，转述起手式语调）。每种符号各留
# 一个干净实例——问号句尾上扬的先验不许被省略号顶掉。
# 代价是全局效应：陈述句的句尾起伏也会变大——这是听感验收过可接受的。
# 仍必须与项目内容无关，否则音色基准会跟着项目漂。
REF_TEXTS = {
    "A": "我一直觉得这本书写得很有意思。有人问我：“这书真的是AI写的吗？”我翻到最后一页……这答案太让人吃惊了！",
    "B": "我们先把问题理清楚，一步一步来。总有人问：“这样讲大家能听明白吗？”可没想到……答案竟这么简单！",
}

# 念全校验的门禁（实验三轮踩坑换来的）：CustomVoice 一次念三句**有时念两句
# 就自己停**——ref 音频缺一句而 ref.txt 写三句时，Base 会把没被念出的那句
# 当成待合成文本补说，每条输出都复读一遍。拦法：语速（含标点字/有声秒）
# 必须落在人声区间、且语音段数 ≥3（三句话至少三段）。不合格自动重念。
RATE_MIN, RATE_MAX, MIN_SEGMENTS, MAX_ATTEMPTS = 3.5, 7.0, 3, 4

# 与 config_manager.PARAM_SPEC 里 tts.qwen3tts_voice_a / _b 的**出厂默认值**一致
# （library:瑟琳-利落 / library:老傅-收束 —— 随仓声库条目，A 案 / B 案各一，
# 与 adopt_official 的角色把关同向）。
# 主程序会把项目里配好的音色来源通过 --voice-a / --voice-b 传进来覆盖它，
# 这里只兜「直接跑命令行」那一种入口。
#
# 必须与出厂值对齐，不能随手挑两条顺耳的：两边不一致时，同一台机器上
# 「命令行直接跑」与「主程序自动跑」会落到两条不同音频上，而档案里看不出
# 是哪个入口来的，排查起来毫无线索。有测试逐条比对这两处。
# 默认音色来自随仓声库（resources/voices），且**必须各认各的案**（A 角认 A 案、
# B 角认 B 案，与 adopt_official 的把关一致）：A 角瑟琳-利落（Serena A 案）、
# B 角老傅-收束（Uncle_Fu B 案）—— 沿 2026-10 前默认的 Serena/Uncle_Fu 家族。
DEFAULT_VOICES = {"A": "library:瑟琳-利落", "B": "library:老傅-收束"}

ROLES = ("A", "B")
# VoiceDesign 造嗓的**草稿目录**：造嗓先落这里，试听满意后再「保存」改名成
# 正式名字（改目录名 + 改 profile 里的 role 字段）。放进清单扫描的排除名单，
# 它不是具名音色，永远不该出现在下拉或 --list 里。
# 双案草稿（2026-10-08 起）：一次生成 A / B 两案，目录 = 基准名 + 案后缀；
# DRAFT_ROLE 保留作基准名与旧版单草稿清理用。
DRAFT_ROLE = "_draft"
DRAFT_ROLES = ("_draftA", "_draftB")
WAV_NAME = "ref.wav"
TXT_NAME = "ref.txt"
PROFILE_NAME = "profile.json"
SCHEMA = 1


# --------------------------------------------------------------------- 小工具
# 响度基准：入库即归一 —— 凡是要存成本地可选音色的 ref（官方预录 take、
# VoiceDesign 造的嗓子），落盘前统一到同一个响度，试听听到的就是归一后的
# 文件，认领保持纯复制，链路上不再有第二处响度逻辑。基准取广播标准的
# RMS -23 dBFS，峰值限幅 -1.5 dBFS 防削波（实测定稿 take 间极差约 12 dB，
# Base 克隆会继承 ref 响度 —— 不归一，A/B 成片一个像吼一个像蚊子）。
LOUDNESS_TARGET_DBFS = -23.0
LOUDNESS_PEAK_CEIL_DBFS = -1.5


def _wav_pcm16_to_array(wav_bytes: bytes):
    """16-bit 单声道 PCM WAV 字节 → (float 波形 [-1,1), 采样率)。"""
    import wave
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        nch, sw, sr, n = (w.getnchannels(), w.getsampwidth(),
                          w.getframerate(), w.getnframes())
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError("只处理 16-bit PCM WAV，收到 sampwidth=%d" % sw)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if nch > 1:
        a = a[::nch]                     # 参考音频一律单声道，多声道兜底取左
    return a, sr


def rms_dbfs(x) -> float:
    """波形的全曲 RMS，dBFS。全静音返回 -inf。"""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if not a.size:
        return float("-inf")
    rms = float(np.sqrt(np.mean(a * a)))
    return 20.0 * float(np.log10(rms)) if rms > 0 else float("-inf")


def peak_dbfs(x) -> float:
    """波形的峰值，dBFS。全静音返回 -inf。"""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if not a.size:
        return float("-inf")
    p = float(np.max(np.abs(a)))
    return 20.0 * float(np.log10(p)) if p > 0 else float("-inf")


def normalize_wav_bytes(wav_bytes: bytes, target: float = LOUDNESS_TARGET_DBFS,
                        peak_ceil: float = LOUDNESS_PEAK_CEIL_DBFS
                        ) -> tuple[bytes, float, float]:
    """把 16-bit 单声道 PCM WAV 归一到目标 RMS，返回 (新字节, 增益dB, 原RMS)。

    gain = min(target - rms, peak_ceil - peak)：先对齐响度，峰值超限就收手
    —— 高峰值低响度的素材宁可响度差一点也不许削波。已在目标 ±0.2 dB 内的
    不动（gain=0），避免无意义地改字节。
    """
    import math
    import struct as _struct
    a, sr = _wav_pcm16_to_array(wav_bytes)
    rms = rms_dbfs(a)
    if rms == float("-inf"):
        raise ValueError("全静音的音频没有归一的意义。")
    gain = target - rms
    peak = peak_dbfs(a)
    if peak != float("-inf"):
        gain = min(gain, peak_ceil - peak)
    if abs(gain) < 0.2:
        return wav_bytes, 0.0, rms
    g = 10.0 ** (gain / 20.0)
    out = np.clip(a * g, -1.0, 1.0)
    pcm = (out * 32767.0).astype(np.int16)
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), round(gain, 2), rms


def f0_med(x, sr, fmin=60.0, fmax=400.0, frame=1024, hop=512) -> float:
    """自相关法取 F0 中位数 —— 只为记个数，方便日后对账音色有没有漂。"""
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if not a.size:
        return float("nan")
    a = a - a.mean()
    peak = float(np.max(np.abs(a)))
    if peak <= 1e-9:
        return float("nan")
    a = a / peak
    lo, hi = max(1, int(sr / fmax)), min(frame - 1, int(sr / fmin))
    vals = []
    for s in range(0, len(a) - frame, hop):
        f = a[s:s + frame]
        if float(np.sqrt(np.mean(f ** 2))) < 0.02:
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
        vals.append(sr / k)
    return float(np.median(vals)) if vals else float("nan")


def role_dir(root: str, role: str) -> str:
    return os.path.join(root, role)


def read_profile(root: str, role: str) -> dict | None:
    """读一份档案的记录。文件不在、或 rec.wav 不在，都算没有。"""
    d = role_dir(root, role)
    prof = os.path.join(d, PROFILE_NAME)
    wav = os.path.join(d, WAV_NAME)
    if not (os.path.isfile(prof) and os.path.isfile(wav)):
        return None
    try:
        with io.open(prof, encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    rec["role"] = role
    rec["dir"] = d
    rec["wav"] = wav
    return rec


def ref_paths(root: str, role: str) -> tuple[str, str] | None:
    """返回 (参考音频路径, 转录文本)。档案不全时返回 None。"""
    rec = read_profile(root, role)
    if not rec:
        return None
    txt = os.path.join(role_dir(root, role), TXT_NAME)
    if not os.path.isfile(txt):
        return None
    with io.open(txt, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return None
    return rec["wav"], text


def digest_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def voiced_stats(audio, sr: float, frame_s=0.025, hop_s=0.010,
                 gap_s: float = 0.3) -> tuple[float, int]:
    """有声时长（秒）与语音段数 —— 念全校验的两把尺。

    能量法：峰值 6% 以下的帧算静音；间隔小于 gap_s 的有声块合并为同一段。
    段数是关键：三句话至少要出现三段，只数总时长拦不住「念两句就停」。
    """
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    fl = max(1, int(sr * frame_s))
    hop = max(1, int(sr * hop_s))
    nfr = max(0, (len(x) - fl) // hop + 1)
    if nfr == 0:
        return 0.0, 0
    e = np.array([np.sqrt(np.mean(x[i * hop:i * hop + fl] ** 2))
                  for i in range(nfr)])
    voiced = e > max(e.max() * 0.06, 1e-6)
    step = hop / float(sr)
    segs, start = [], None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        if not v and start is not None:
            if (i - start) * step > 0.15:
                segs.append((start, i))
            start = None
    if start is not None:
        segs.append((start, nfr))
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < gap_s / step:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return float(voiced.sum() * step), len(merged)


# ------------------------------------------------------------- ref_base 账本
# ref_base / icl 全部带版本后缀（ref_base_<sign>.wav / icl_<sign>.wav），
# sign = 生成那一刻 ref.wav 内容指纹的前 8 位 —— 文件名即「克隆自哪个原生
# ref」的映射。账本 base_refs.json 记 sign → 原生 ref 指纹 ↔ 文件组，
# active 指当前生效的 sign。换嗓老版本原样不动；同一原生 ref 永远只克隆
# 一次（重做 = Base 克隆重掷骰子 = 音色变化），命中账本直接复用。
# 三个名字常量与 podcast_maker/layout.py 同名，tests/test_tts_engine.py 锁。
BASE_REFS_NAME = "base_refs.json"
BASEREF_STEM = "ref_base"
ICL_STEM = "icl"


def base_refs_path(root: str, role: str) -> str:
    """角色级账本路径：音色/<角色>/base_refs.json。"""
    return os.path.join(role_dir(root, role), BASE_REFS_NAME)


def read_base_refs(root: str, role: str) -> dict | None:
    """读账本。文件不在或解析失败都算没有（返回 None）。"""
    p = base_refs_path(root, role)
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as fh:
            led = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    return led if isinstance(led, dict) and isinstance(
        led.get("entries"), dict) else None


def write_base_refs(root: str, role: str, ledger: dict) -> None:
    """原子写账本。"""
    os.makedirs(role_dir(root, role), exist_ok=True)
    p = base_refs_path(root, role)
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(ledger, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def base_ref_entry_for(root: str, role: str, log=print) -> dict | None:
    """当前 ref.wav 对应的账本条目；命中时顺手把 active 切到它。

    sign 按当前 ref.wav 的字节重算，不信任账本里的旧指纹 —— ref 换了就是
    换了，账本必须跟着现实走。没命中返回 None（调用方决定是否生成）。
    """
    wav = os.path.join(role_dir(root, role), WAV_NAME)
    if not os.path.isfile(wav):
        return None
    sign = digest_of(wav)[:8]
    led = read_base_refs(root, role)
    if not led:
        return None
    entry = led["entries"].get(sign)
    if not entry:
        return None
    if led.get("active") != sign:
        led["active"] = sign
        write_base_refs(root, role, led)
        log("  %s 角 ref_base 切回账本版本 %s（原生 ref %s）。"
            % (role, sign, entry.get("ref_sha16", "?")))
    return entry


def _stash_base_ref_artifacts(role_d: str) -> str | None:
    """换嗓覆盖前，把旧嗓的 ref_base_* / icl_* / 账本搬进临时目录。

    旧版本的 ref_base 是账本资产（换回旧嗓时直接复用，不重新克隆）——
    rmtree 一把梭会把它们连带删掉。搬出去，新档案拷完再原样搬回来，
    账本条目一个不少。
    """
    if not os.path.isdir(role_d):
        return None
    keep = [n for n in os.listdir(role_d)
            if n == BASE_REFS_NAME or n.startswith((BASEREF_STEM + "_",
                                                    ICL_STEM + "_"))]
    if not keep:
        return None
    import tempfile
    tmp = tempfile.mkdtemp(prefix="_base_ref_stash_")
    for n in keep:
        os.replace(os.path.join(role_d, n), os.path.join(tmp, n))
    return tmp


def _restore_base_ref_artifacts(role_d: str, tmp: str | None) -> None:
    """把暂存的旧版本 ref_base 件搬回角色目录。"""
    if not tmp:
        return
    for n in os.listdir(tmp):
        os.replace(os.path.join(tmp, n), os.path.join(role_d, n))
    os.rmdir(tmp)


def ensure_base_ref(project_dir: str, role: str, voice_dir_name: str = "音色",
                    force: bool = False, device: str = "auto", log=print,
                    sample_rate=None) -> dict:
    """保证当前 ref.wav 有自己版本的 ref_base：账本命中直接用，永不重做。

    命中（该原生 ref 克隆过）→ active 切过去、文件原样用，零模型调用。
    未命中 → 先把账本 active 清空（旧指向随换嗓失效），再加载 Base 模型
    生成一次并记账。生成失败会抛错，此时账本无 active 指向 —— 读者
    （tts_engine._ref_pair）fail-closed 报「先补 ref_base」，绝不静默拿旧
    嗓上场。--force 只在 make_base_ref 那侧有意义（同 sign 重掷骰子）。
    """
    root = os.path.join(project_dir, voice_dir_name)
    if not ref_paths(root, role):
        raise RuntimeError("%s 角还没有音色档案，先认领再补 ref_base。" % role)
    entry = base_ref_entry_for(root, role, log)
    if entry and not force:
        missing = [k for k in ("ref_base", "icl_wav", "icl_txt")
                   if not os.path.isfile(os.path.join(role_dir(root, role),
                                                      entry.get(k, "")))]
        if not missing:
            sign = digest_of(os.path.join(role_dir(root, role),
                                          WAV_NAME))[:8]
            log("  %s 角 ref_base 命中账本（sign %s），直接复用不重做。"
                % (role, sign))
            return {"role": role, "hit": True, "sign": sign, "entry": entry}
        log("  %s 角账本条目缺 %s，按未生成补做。" % (role, "、".join(missing)))
    wav = os.path.join(role_dir(root, role), WAV_NAME)
    sign = digest_of(wav)[:8]
    led = read_base_refs(root, role) or {"schema": 1, "active": None,
                                         "entries": {}}
    if led.get("active") != sign:
        led["active"] = None          # 旧指向随换嗓失效，生成成功才落新指向
        write_base_refs(root, role, led)
    import make_base_ref as mbr   # 拖 torch 的导入链，只在真要生成时进来
    out = mbr.build(project_dir, roles=(role,), voice_dir_name=voice_dir_name,
                    force=bool(force), device=device, log=log,
                    sample_rate=sample_rate)
    return {"role": role, "hit": False, "sign": sign, "build": out}


# --------------------------------------------------------------------- 生成
def write_profile(role_root: str, role: str, wav_bytes: bytes, text: str,
                  rec: dict) -> dict:
    """落盘一份档案：波形 + 转录 + 记录。三件一起写，缺一件就等于没有。"""
    os.makedirs(role_root, exist_ok=True)
    wav_path = os.path.join(role_root, WAV_NAME)
    tmp = wav_path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(wav_bytes)
    os.replace(tmp, wav_path)                     # 原子替换：不留下半个文件

    with io.open(os.path.join(role_root, TXT_NAME), "w",
                 encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")

    rec = dict(rec)
    rec["schema"] = SCHEMA
    rec["role"] = role
    rec["text"] = text
    # 内容指纹，与 serve.voice_key_for() 同一种算法（文件内容的 sha256 取前 16 位）。
    # 它有两个用处：跨项目对账「是不是同一份音色」，以及充当种子输入。
    rec["ref_md5"] = hashlib.sha256(wav_bytes).hexdigest()[:16]
    rec.pop("dir", None)
    rec.pop("wav", None)
    prof = os.path.join(role_root, PROFILE_NAME)
    tmp = prof + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, prof)
    return rec


def parse_voice_source(spec: str) -> tuple:
    """音色来源语法有三种取值：

    ``library:名字`` —— 随仓声库（resources/voices）里的人耳定稿音色，
    **现在的首选**；``official:名字#takeN`` —— 旧预录池按 take 认领（认领
    已改走声库，这语法留作旧配置的可读兼容，take 会与声库溯源对账）；
    ``named:名字`` —— 项目里 VoiceDesign 造的/认领落地的具名档案。裸的
    内置音色名（如 Serena）**不再是合法来源**：CustomVoice 每项目现录那条路
    已退役，这里 fail-closed 当场拒绝，不静默退回旧路径。
    返回 (kind, name, take)；library / named 的 take 恒为 None。
    """
    spec = str(spec or "").strip()
    kind, sep, rest = spec.partition(":")
    if sep and kind in ("official", "named", "library") and rest.strip():
        name, _, take = rest.partition("#")
        name = name.strip()
        if not name:
            raise RuntimeError("音色来源「%s」缺名字。" % spec)
        if kind == "official":
            take = take.strip()
            return kind, name, (int(take) if take else None)
        return kind, name, None
    raise RuntimeError(
        "「%s」不是合法的音色来源。音色只能来自随仓声库（library:名字）、"
        "旧预录引用（official:名字#takeN，已退役仅兼容读取）或具名档案"
        "（named:名字）—— CustomVoice 的内置音色名已退役，"
        "不再为单个项目现录参考音频。" % (spec or "（空）"))


def build(project_dir: str, roles=ROLES, voices=None, voice_dir_name: str = "音色",
          force: bool = False, log=print) -> dict:
    """为项目的指定角色备齐音色档案。已存在且非 force 时跳过。

    「备齐」不是录制，是**复制**：来源只有两种已经落地的音频 —— 官方预录
    定稿 take 与本项目具名档案（VoiceDesign 造的）。选了哪条，哪条的字节
    就原样落进角色槽位，逐字节相同；全程不加载任何模型、不掷一次骰子。
    这取代了 CustomVoice 每项目现录那条路（句间像换人是引擎级病，实证过）。
    """
    root = os.path.join(project_dir, voice_dir_name)
    voices = dict(DEFAULT_VOICES, **(voices or {}))
    out: dict = {"voice_dir": root, "roles": {}, "generated": [], "skipped": []}

    todo = [r for r in roles if force or not ref_paths(root, r)]
    for r in roles:
        if r not in todo:
            out["skipped"].append(r)
    if not todo:
        log("音色档案已齐，不需要重新生成。")
        out["roles"] = {r: (read_profile(root, r) or {}) for r in roles}
        return out

    for role in todo:
        kind, name, take = parse_voice_source(voices.get(role, ""))
        log("%s 角 ← %s（复制文件，逐字节相同）" % (role, voices.get(role, "")))
        if kind in ("official", "library"):
            # 两种来源都落声库认领：library: 是正路，official: 旧语法里存的
            # 名字/take 会与声库溯源对账（对不上当场报错，不静默改名）。
            rec = adopt_official(project_dir, name, role, take,
                                 voice_dir_name, True, log)
        else:
            rec = adopt(project_dir, name, role, voice_dir_name, True, log)
        out["roles"][role] = rec
        out["generated"].append(role)
    for r in out["skipped"]:
        out["roles"][r] = read_profile(root, r) or {}
    return out


# --------------------------------------------------------------------- 继承
def inherit(project_dir: str, src_project_dir: str, roles=ROLES,
            voice_dir_name: str = "音色", force: bool = False,
            log=print) -> dict:
    """把另一个项目的音色档案整个搬过来。

    跨项目「音色完全一样」只有这一条路走得通：复制文件，不是重新生成。
    重新生成能不能得到同一条，取决于参考文案、音色名、调用写法三样都对上 ——
    那是一串可以被改动的巧合，而文件的内容指纹不是。
    """
    dst_root = os.path.join(project_dir, voice_dir_name)
    src_root = os.path.join(src_project_dir, voice_dir_name)
    out: dict = {"voice_dir": dst_root, "roles": {}, "copied": [], "skipped": []}
    if not os.path.isdir(src_root):
        raise RuntimeError("源项目没有音色目录：%s" % src_root)

    for role in roles:
        src = ref_paths(src_root, role)
        if not src:
            out["skipped"].append(role)
            log("  %s 角：源项目里没有完整档案，跳过" % role)
            continue
        if not force and ref_paths(dst_root, role):
            out["skipped"].append(role)
            log("  %s 角：本项目已有档案，跳过（要覆盖加 --force）" % role)
            continue

        d = role_dir(dst_root, role)
        if os.path.isdir(d):
            shutil.rmtree(d)
        shutil.copytree(role_dir(src_root, role), d)

        rec = read_profile(dst_root, role) or {}
        rec["source"] = "inherited"
        rec["inherited_from"] = os.path.basename(
            os.path.normpath(src_project_dir))
        rec["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
        for key in ("dir", "wav"):
            rec.pop(key, None)
        with io.open(os.path.join(d, PROFILE_NAME), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        out["roles"][role] = rec
        out["copied"].append(role)
        log("  %s 角：已继承（md5 %s，与源项目逐字节相同）"
            % (role, rec.get("ref_md5", "?")))
        out["roles"][role]["base_ref"] = ensure_base_ref(
            project_dir, role, voice_dir_name, log=log)
    return out


# --------------------------------------------------------------------- 名字
def sanitize_role_name(name: str) -> str:
    """把用户起的音色名清洗成安全的目录名。

    具名音色（VoiceDesign 造的）与 A / B 角共用一个 音色/ 目录，名字就是目录名，
    所以这里挡三类东西：路径分隔符（目录逃逸）、A / B 保留名（覆盖角色槽位）、
    空名与超长名。清洗后为空等于没给名字，直接报错不猜。
    """
    n = (name or "").strip()
    for ch in '\\/:*?"<>|':
        n = n.replace(ch, "")
    n = n.strip().strip(".")
    if not n:
        raise RuntimeError("音色名不能为空。")
    # 大小写一起挡：Windows 文件系统不分大小写，目录「a」会跟 A 角槽位相撞。
    if n.upper() in ROLES:
        raise RuntimeError("「%s」是 A / B 角保留名，具名音色换一个名字。" % n)
    if len(n) > 24:
        raise RuntimeError("音色名太长（最多 24 个字符）：%s" % n)
    return n


# --------------------------------------------------------------------- 设计
def design(project_dir: str, name: str, description: str, text: str = "",
           voice_dir_name: str = "音色", device: str = "auto",
           force: bool = False, seed_offset: int = 0, log=print,
           cases=("A", "B")) -> dict:
    """用 VoiceDesign 按描述造音色，**双案生成**：A 案念 A 案定稿文案、
    B 案念 B 案定稿文案，模型只加载一次，两案各落各的档案。

    与 build() 同一套门禁（念全校验 + 自动重念）、同一种档案格式；差别只在
    音色来源：build 从内置音色表挑一个，design 按描述**凭空造一个不存在的
    嗓子**。产物落在 <项目>/音色/<基准名><案>/（如 老陈说书A / 老陈说书B），
    profile 记 text_role —— 认领时 A 角只认 A 案、B 角只认 B 案，与声库
    同一条把关。之后用 adopt() 复制进角色槽位，Base 克隆照常走。

    为什么要双案：A / B 角永远念不同的稿子，ref 的韵律先验整条继承——
    只造一案的话，另一角拿到的 ref 念的是别案的文案，节奏先验错位。

    种子按 (该案文案, 档案名, 描述) 派生：同一套输入在同设备同后端下复现
    同一条，与 build() 的确定性边界同源；两案文案不同，种子天然岔开。
    ``seed_offset`` 在这个基准上平移 —— 界面上「重新生成」就是同一套描述
    再要一条不一样的，没有它同输入会复现同一条波形，重生成成了空操作。
    ``text`` 非空时**两案都念这份自定义文案**（此时双案 = 同文案的两条
    采样，A / B 案节奏先验合一 —— 这是使用者显式覆盖，档案里记得清清楚楚）。
    """
    root = os.path.join(project_dir, voice_dir_name)
    base = sanitize_role_name(name)
    desc = (description or "").strip()
    if not desc:
        raise RuntimeError(
            "VoiceDesign 需要一段音色描述（例如「低沉醇厚的成熟男声，像说书人」），"
            "空描述造不出音色。")
    custom_text = (text or "").strip()
    cases = tuple(c for c in cases if c in ROLES) or ROLES

    todo = []
    for case in cases:
        dir_name = base + case
        if not force and ref_paths(root, dir_name):
            log("音色「%s」已有档案，跳过（要重建加 --force）" % dir_name)
            continue
        todo.append((case, dir_name,
                     custom_text or REF_TEXTS[case]))
    if not todo:
        return {"voice_dir": root, "base": base, "generated": False,
                "cases": {c: {"skipped": True} for c in cases}}

    resolved = serve.resolve_model(serve.VOICE_DESIGN_MODEL)
    kind = serve.model_kind(resolved)
    if kind != "voice_design":
        raise RuntimeError(
            "造音色要用 VoiceDesign 变体，当前解析到 %s（%s）。"
            "先跑 setup_env.py / fetch_model.py 把它下下来。" % (resolved, kind))

    log("加载 %s —— 只用来造音色，跑完就退" % os.path.basename(resolved))
    eng = serve.Engine(resolved, device, log)
    t0 = time.time()
    eng.ensure()
    log(f"  后端 {eng.backend} · 设备 {eng.device} · 加载 {time.time() - t0:.1f}s")
    if str(eng.device or "").startswith("cpu"):
        log("  **注意**：本次跑在 CPU 上（%s）。造出的音色与 GPU 路径不是同一条"
            "波形，且慢十几倍。" % (eng.device_note or "空闲显存不足"))
        log("  **注意**：要换设备重造，先腾空显存再加 --force 重跑。")

    out = {}
    for case, dir_name, case_text in todo:
        seed0 = serve.seed_for(case_text, dir_name + "\x00" + desc) \
            + int(seed_offset)
        attempts = 0
        while True:
            attempts += 1
            s = seed0 + (attempts - 1) * 7
            samples, sr = eng.synth_one(case_text, "", instruct=desc,
                                        language="Chinese", seed=s)
            audio = np.asarray(samples, dtype=np.float32).reshape(-1)
            vd, nseg = voiced_stats(audio, sr)
            rate = len(case_text) / vd if vd > 0.1 else float("inf")
            ok = (RATE_MIN <= rate <= RATE_MAX and nseg >= MIN_SEGMENTS)
            log("  「%s」（%s案） 念全校验 第%d次：有声%.2fs 语速%.1f字/s 段%d → %s"
                % (dir_name, case, attempts, vd, rate, nseg,
                   "PASS" if ok else "FAIL"))
            if ok:
                break
            if attempts >= MAX_ATTEMPTS:
                raise RuntimeError(
                    "音色「%s」（%s案）连试 %d 次都没念全（语速 %.1f字/s、%d 段）。"
                    "请重跑一次；若反复出现，改描述或检查模型。"
                    % (dir_name, case, attempts, rate, nseg))

        wav_bytes = serve.to_wav_bytes(samples, sr)
        # 入库即归一：造出来的嗓子在落盘前统一响度，试听/认领/成片全用这一份。
        wav_bytes, loud_gain, loud_rms = normalize_wav_bytes(wav_bytes)
        dur = len(audio) / float(sr)
        rec = write_profile(role_dir(root, dir_name), dir_name, wav_bytes,
                            case_text, {
            "source": "voicedesign",
            "builtin_voice": "",
            "design_description": desc,
            "base_name": base,
            "text_role": case,
            "model": resolved,
            "device": str(eng.device or ""),
            "backend": eng.backend,
            "seed": s,
            "sample_rate": int(sr),
            "ref_seconds": round(dur, 3),
            "voiced_seconds": round(vd, 3),
            "speech_rate": round(rate, 2),
            "attempts": attempts,
            "f0_med": round(f0_med(audio, sr), 1),
            "loudness_gain_db": loud_gain,
            "loudness_rms_dbfs": round(loud_rms + loud_gain, 2),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "origin_project": os.path.basename(os.path.normpath(project_dir)),
            "inherited_from": "",
        })
        log("  「%s」（%s案） · %.2fs · F0 %s · md5 %s"
            % (dir_name, case, dur, rec["f0_med"], rec["ref_md5"]))
        out[case] = {"dir": dir_name, "generated": True, "profile": rec}
    for case in cases:
        out.setdefault(case, {"skipped": True})
    return {"voice_dir": root, "base": base, "generated": True, "cases": out}


# --------------------------------------------------------------------- 认领
def adopt(project_dir: str, name: str, role: str,
          voice_dir_name: str = "音色", force: bool = False,
          log=print) -> dict:
    """把一个具名音色档案复制进 A / B 角槽位 —— 复制文件，波形逐字节相同。

    与 inherit() 同一条哲学：跨槽位「音色完全一样」靠复制文件，不靠重新生成
    碰运气。Base 克隆读的是 A / B 槽位的那份 ref，所以「让造出来的音色上场」
    就是把它拷进槽位这一步，运行链零改动。
    """
    role = (role or "").strip().upper()
    if role not in ROLES:
        raise RuntimeError("认领目标只能是 A 角或 B 角。")
    src_name = sanitize_role_name(name)
    if src_name == role:
        raise RuntimeError("「%s」本身就是 %s 角。" % (src_name, role))
    root = os.path.join(project_dir, voice_dir_name)
    if not ref_paths(root, src_name):
        raise RuntimeError(
            "音色「%s」还没有完整档案（ref.wav + ref.txt），先造一份。" % src_name)
    # 分案把关与 adopt_official 同一把尺：A 角只认 A 案、B 角只认 B 案。
    # 档案没标 text_role（旧版造嗓）视为通用，不作限制。ref.txt 与音频逐字
    # 对应虽然天然成立，但 A 案嗓子认给 B 角，B 角拿到的节奏先验是 A 案
    # 文案的 —— 「A / B 永远念不同的东西」在认领这一步锁死。
    src_tr = (read_profile(root, src_name) or {}).get("text_role")
    if src_tr in ROLES and src_tr != role:
        raise RuntimeError(
            "「%s」是%s案嗓子（念%s案定稿文案），%s 角只能认%s案档案。"
            % (src_name, src_tr, src_tr, role, role))
    if not force and ref_paths(root, role):
        raise RuntimeError(
            "%s 角已有档案；要覆盖请加 --force（界面按钮自带二次确认）。" % role)

    dst = role_dir(root, role)
    stash = _stash_base_ref_artifacts(dst)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(role_dir(root, src_name), dst)
    _restore_base_ref_artifacts(dst, stash)

    rec = read_profile(root, role) or {}
    rec["adopted_from"] = src_name
    rec["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    for key in ("dir", "wav"):
        rec.pop(key, None)
    with io.open(os.path.join(dst, PROFILE_NAME), "w",
                 encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    log("  %s 角 ← 「%s」（md5 %s，逐字节相同）"
        % (role, src_name, rec.get("ref_md5", "?")))
    base = ensure_base_ref(project_dir, role, voice_dir_name, log=log)
    return {"role": role, "adopted_from": src_name, "profile": rec,
            "base_ref": base}


# ------------------------------------------------------------- 官方预设音色
OFFICIAL_DIR = os.path.join(HERE, "official_voices")
# 随仓声库：人耳挑选结论落库处（curate_voices.py 从车间素材池抽出的定稿）。
# 认领的**唯一**源在这里 —— 车间（official_voices）只服务重录与试听，不再
# 直接上场：声库每条音色天然带着自己的 text_role / 逐 take 文案 / 溯源。
LIBRARY_DIR = os.path.join(os.path.dirname(HERE), "podcast_maker",
                           "resources", "voices")


def library_voices() -> list:
    """随仓声库清单：一条人耳定稿就是一个音色，带 text_role 与全套溯源。

    只读账本不做裁判 —— 「A 角能不能认这条」由 adopt_official 按 text_role
    把关；清单本身供下拉、面板与校验共用，纯 JSON 读取，不碰模型。
    """
    out = []
    if not os.path.isdir(LIBRARY_DIR):
        return out
    for entry in sorted(os.listdir(LIBRARY_DIR)):
        prof_path = os.path.join(LIBRARY_DIR, entry, PROFILE_NAME)
        if not os.path.isfile(prof_path):
            continue
        try:
            prof = json.load(io.open(prof_path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if prof.get("kind") != "voice_library":
            continue
        out.append({
            "name": prof.get("name", entry),
            "text_role": prof.get("text_role"),
            "family": prof.get("family", ""),
            "flavor": prof.get("flavor", ""),
            "text": prof.get("text", ""),
            "builtin_voice": prof.get("builtin_voice", ""),
            "official_take": prof.get("official_take"),
            "language": prof.get("language_tag", ""),
            "desc": prof.get("desc", ""),
            "f0_med": prof.get("f0_med"),
            "seconds": prof.get("seconds"),
            "voiced_seconds": prof.get("voiced_seconds"),
            "speech_rate": prof.get("speech_rate"),
            "sha16": prof.get("sha16"),
            "ref_md5": prof.get("ref_md5"),
        })
    return out


def official_presets() -> list:
    """官方预设音色清单（record_official_presets.py 预录、人耳定稿的基准档案）。

    每项带 chosen（人耳过关的 take 列表）与全部 takes 元数据。这里只读账本
    不做裁判 —— 「哪条能上场」由 adopt_official 按 chosen 把关。
    """
    out = []
    if not os.path.isdir(OFFICIAL_DIR):
        return out
    for entry in sorted(os.listdir(OFFICIAL_DIR)):
        prof_path = os.path.join(OFFICIAL_DIR, entry, PROFILE_NAME)
        if not os.path.isfile(prof_path):
            continue
        try:
            prof = json.load(io.open(prof_path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if prof.get("kind") != "official_preset":
            continue
        chosen = prof.get("chosen") or []
        if isinstance(chosen, int):
            chosen = [chosen]
        out.append({
            "name": prof.get("builtin_voice", entry),
            "language": prof.get("language_tag", ""),
            "desc": prof.get("desc", ""),
            "text": prof.get("text", ""),
            "chosen": chosen,
            "takes": prof.get("takes", []),
        })
    return out


def adopt_official(project_dir: str, voice: str, role: str, take=None,
                   voice_dir_name: str = "音色", force: bool = False,
                   log=print) -> dict:
    """声库音色直接复制进 A / B 槽位 —— 复制文件，波形逐字节相同。

    源是随仓声库（resources/voices，curate_voices.py 落库的人耳定稿）：每条
    音色自带 text_role，把关只有一个硬条件 —— **A 角只认 A 案、B 角只认 B 案**
    （ref.txt 与音频逐字对应是 ICL 的命门，认领错文案等于给 Base 塞一份对不上
    的参考）。fail-closed：角色不符、音色不存在、take 对不上溯源，一律拒绝；
    「选哪条」在试听页已经做完，认领链不再掷骰子。

    ``take`` 参数是旧 official:时代（预录池按 take 认领）的兼容入参：给了就
    与声库溯源的 official_take 对账，对不上当场报错 —— 静默忽略等于掩盖错引。
    """
    role = (role or "").strip().upper()
    if role not in ROLES:
        raise RuntimeError("认领目标只能是 A 角或 B 角。")
    lib = {v["name"]: v for v in library_voices()}
    prof = lib.get((voice or "").strip())
    if not prof:
        raise RuntimeError("声库里没有「%s」。可选：%s。旧 official:引用"
                           "（按 take 从预录池认领）已退役，请从音色下拉重新选"
                           "（library:…）。"
                           % (voice, "、".join(sorted(lib)) or "无"))
    if prof.get("text_role") != role:
        raise RuntimeError("「%s」是%s案音色（A 角认 A 案、B 角认 B 案），"
                           "不能上场给 %s 角。" % (voice, prof.get("text_role"), role))
    if take is not None and int(take) != int(prof.get("official_take") or -1):
        raise RuntimeError("「%s」在声库里对应的是 take%d，不是 take%s —— 溯源"
                           "对不上，拒绝上场。" % (voice, prof.get("official_take"),
                                                 take))
    wav_src = os.path.join(LIBRARY_DIR, prof["name"], "ref.wav")
    if not os.path.isfile(wav_src):
        raise RuntimeError("声库「%s」的音频文件缺失，重跑 curate_voices.py 补。"
                           % prof["name"])
    root = os.path.join(project_dir, voice_dir_name)
    if not force and ref_paths(root, role):
        raise RuntimeError(
            "%s 角已有档案；要覆盖请加 --force（界面按钮自带二次确认）。" % role)

    with open(wav_src, "rb") as fh:
        wav_bytes = fh.read()
    # ref.txt 就是声库里那份逐 take 文案 —— 与音频同落库、同认领，永远对齐。
    rec = write_profile(role_dir(root, role), role, wav_bytes, prof["text"], {
        "source": "voice_library",
        "adopted_from": prof["name"],
        "builtin_voice": prof.get("builtin_voice"),
        "official_take": prof.get("official_take"),
        "seed": prof.get("seed"),
        "sha16": prof.get("sha16"),
        "f0_med": prof.get("f0_med"),
        "ref_seconds": prof.get("seconds"),
        # 预录时实测的语速与有声时长 —— 生成那天就量好的，认领照抄，
        # 不让槽位档案在这两栏上开着空窗，逼人回头查预录账本。
        "voiced_seconds": prof.get("voiced_seconds"),
        "speech_rate": prof.get("speech_rate"),
    })
    log("  %s 角 ← 声库「%s」（%s案 · md5 %s，逐字节相同）"
        % (role, prof["name"], prof.get("text_role"), rec.get("ref_md5", "?")))
    base = ensure_base_ref(project_dir, role, voice_dir_name, log=log)
    return {"role": role, "voice": prof["name"],
            "take": prof.get("official_take"), "profile": rec,
            "base_ref": base}


# --------------------------------------------------------------------- 查账
def normalize_official_archive(official_dir: str = None, log=print) -> int:
    """存量回填：把官方预录库里的 take 统一归一到目标响度，一次性的迁移。

    改的是字节，账本必须跟着走：每条 take 重算 sha16，记下增益与归一后
    RMS；chosen（人耳定稿结论）不动 —— 响度缩放不改音色与韵律，耳朵在
    原声上做的取舍依然成立。已经在目标 ±0.2 dB 内的 take 原样跳过。
    落在 make_voice 而不是 record_official_presets：它操作的是官方档案资产
    （OFFICIAL_DIR 的账与音频），且这样系统 python 不用拖 serve/torch 就能
    测它。
    """
    root = official_dir or OFFICIAL_DIR
    changed = skipped = 0
    for name in sorted(os.listdir(root)):
        vdir = os.path.join(root, name)
        prof_path = os.path.join(vdir, PROFILE_NAME)
        if not os.path.isdir(vdir) or not os.path.isfile(prof_path):
            continue
        with io.open(prof_path, encoding="utf-8") as fh:
            prof = json.load(fh)
        dirty = False
        for t in prof.get("takes", []):
            wav_path = os.path.join(vdir, "take%d.wav" % t.get("take", 0))
            if not os.path.isfile(wav_path):
                log("  %s take%s 缺音频，跳过" % (name, t.get("take", "?")))
                continue
            with open(wav_path, "rb") as fh:
                wav_bytes = fh.read()
            new_bytes, gain, rms = normalize_wav_bytes(wav_bytes)
            if new_bytes is wav_bytes:
                skipped += 1
                t["loudness_gain_db"] = 0.0
                t["loudness_rms_dbfs"] = round(rms, 2)
                dirty = True
                log("  %s take%s 已在目标响度，不动" % (name, t.get("take")))
                continue
            with open(wav_path, "wb") as fh:
                fh.write(new_bytes)
            t["sha16"] = digest_of(wav_path)[:16]     # 字节变了，指纹重算
            t["loudness_gain_db"] = gain
            t["loudness_rms_dbfs"] = round(rms + gain, 2)
            changed += 1
            log("  %s take%s 归一 %+0.1f dB → %.2f dBFS"
                % (name, t.get("take"), gain, rms + gain))
            dirty = True
        if dirty:
            tmp = prof_path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(prof, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, prof_path)
    log("归一回填完成：改 %d 条、已在目标 %d 条。" % (changed, skipped))
    return 0


def named_roles(root: str) -> list:
    """音色目录里 A / B 之外的具名档案（VoiceDesign 造的那些）。

    判定与 ref_paths 同一把尺：波形 + 转录两件齐才算有档案，半份不算。
    """
    if not os.path.isdir(root):
        return []
    out = []
    for entry in sorted(os.listdir(root)):
        if (entry in ROLES or entry == DRAFT_ROLE or entry in DRAFT_ROLES
                or entry.startswith(".")):
            continue
        if os.path.isdir(os.path.join(root, entry)) and ref_paths(root, entry):
            out.append(entry)
    return out


def status(project_dir: str, voice_dir_name: str = "音色") -> dict:
    """现有档案的实况。指纹按**当前文件内容**重算，不信任记录里的旧值 ——
    记录可以没跟着文件一起更新，那正是要查的东西。"""
    root = os.path.join(project_dir, voice_dir_name)
    out = {"voice_dir": root, "exists": os.path.isdir(root), "roles": {}}
    roles = list(ROLES) + named_roles(root)
    for role in roles:
        p = ref_paths(root, role)
        if not p:
            out["roles"][role] = {"ok": False}
            continue
        wav, text = p
        rec = read_profile(root, role) or {}
        st = os.stat(wav)
        sha = digest_of(wav)
        out["roles"][role] = {
            "ok": True,
            "wav": wav,
            "seconds": round(st.st_size / (24000 * 2.0), 2),
            "bytes": st.st_size,
            "content_sha": sha[:16],
            "recorded_md5": rec.get("ref_md5", ""),
            "matches_record": sha[:16] == rec.get("ref_md5", ""),
            "builtin_voice": rec.get("builtin_voice", ""),
            # 录这份档案时跑在哪条路上。同一套音色名 + 同一句参考文案，GPU 与
            # CPU 出的是两条不同的波形，所以这个字段是「两份档案音色不一致」
            # 时唯一的排查线索，不能省。
            "device": rec.get("device", ""),
            "backend": rec.get("backend", ""),
            "source": rec.get("source", ""),
            "design_description": rec.get("design_description", ""),
            "inherited_from": rec.get("inherited_from", ""),
            "adopted_from": rec.get("adopted_from", ""),
            "voiced_seconds": rec.get("voiced_seconds", ""),
            "speech_rate": rec.get("speech_rate", ""),
            "attempts": rec.get("attempts", ""),
            "text": text,
            "created": rec.get("created", ""),
        }
    return out


def _print_status(st: dict) -> None:
    print("音色目录 %s" % st["voice_dir"])
    for role, r in st["roles"].items():
        if not r["ok"]:
            print("  %s 角：没有档案" % role)
            continue
        print("  %s 角：%.2fs · %s · 来源 %s%s%s"
              % (role, r["seconds"],
                 r["builtin_voice"] or r.get("design_description") or "?",
                 r["source"] or "?",
                 ("（继承自 %s）" % r["inherited_from"]
                  if r["inherited_from"] else ""),
                 ("（认领自 %s）" % r["adopted_from"]
                  if r.get("adopted_from") else "")))
        print("       指纹 %s %s"
              % (r["content_sha"],
                 "与记录一致" if r["matches_record"] else "**与记录不符**"))
        print("       录制设备 %s" % (r["device"] or "（旧档案未记）"))
        print("       文案 %s" % r["text"])
        if r.get("attempts"):
            print("       念全 %s次尝试 · 有声 %ss · 语速 %s字/s"
                  % (r["attempts"], r["voiced_seconds"], r["speech_rate"]))


# --------------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(
        description="角色音色档案：生成 / 继承 / 查账",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python make_voice.py --project-dir projects/20260916-100000\n"
               "  python make_voice.py --project-dir projects/新 --from "
               "projects/旧\n"
               "  python make_voice.py --project-dir projects/某 --list\n")
    ap.add_argument("--project-dir", default="",
                    help="项目根目录（--officials 查清单时不需要）")
    ap.add_argument("--voice-dir", default="音色",
                    help="音色子目录名（主程序按 layout.DIR_VOICE 传入）")
    ap.add_argument("--role", default="both", choices=("A", "B", "both"))
    ap.add_argument("--voice-a", default=DEFAULT_VOICES["A"],
                    help="A 角音色来源：official:名字#takeN 或 named:名字")
    ap.add_argument("--voice-b", default=DEFAULT_VOICES["B"],
                    help="B 角音色来源：official:名字#takeN 或 named:名字")
    ap.add_argument("--from", dest="src", default="",
                    help="从另一个项目目录继承档案（跨项目音色一致走这条）")
    ap.add_argument("--design-name", default="",
                    help="VoiceDesign 造音色：档案名（也是音色/ 下的目录名）")
    ap.add_argument("--design-desc", default="",
                    help="VoiceDesign 造音色：音色描述（自然语言，必填）")
    ap.add_argument("--design-text", default="",
                    help="VoiceDesign 造音色：参考文案（留空 = 内置三句混合）")
    ap.add_argument("--design-seed-offset", type=int, default=0,
                    help="VoiceDesign 造音色：种子偏移（重新生成要另一条时用）")
    ap.add_argument("--design-case", default="all",
                    choices=("A", "B", "all"),
                    help="VoiceDesign 造音色：造哪几案（默认 A/B 双案，"
                         "模型加载一次；补案时单选）")
    ap.add_argument("--adopt-from", default="",
                    help="认领具名音色：把 音色/<名字> 复制进角色槽位")
    ap.add_argument("--adopt-role", default="", choices=("A", "B", ""),
                    help="认领目标角色（配合 --adopt-from）")
    ap.add_argument("--officials", action="store_true",
                    help="只列官方预设音色清单（JSON），不需要 --project-dir")
    ap.add_argument("--official-adopt", default="",
                    help="认领官方预设：把预录定稿 take 复制进角色槽位")
    ap.add_argument("--official-take", type=int, default=None,
                    help="官方预设的 take 序号（缺省 = 定稿清单第一条；"
                         "只允许 --official-adopt 指定的音色已定稿的 take）")
    ap.add_argument("--device", default="auto",
                    choices=("auto", "cuda", "cpu"),
                    help="VoiceDesign 造音色用；复制档案不跑模型")
    ap.add_argument("--force", action="store_true", help="已有档案时也重建")
    ap.add_argument("--list", action="store_true", help="只打印现状，不生成")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印结果")
    args = ap.parse_args()

    if args.officials:
        print(json.dumps({"officials": official_presets()},
                         ensure_ascii=False, indent=1))
        return 0

    if not args.project_dir:
        raise SystemExit("--project-dir 必填（除非只用 --officials 查清单）。")
    roles = ROLES if args.role == "both" else (args.role,)
    project = os.path.abspath(args.project_dir)
    if not os.path.isdir(project):
        raise SystemExit("项目目录不存在：%s" % project)

    def _log(msg):
        """json 模式下进度走 stderr、结果留在 stdout。

        调用方（主程序）要解析 stdout 那段 JSON，所以它必须是干净的一份 ——
        进度行混进去就解析不了。分开写，两边各取各的。
        """
        if args.json:
            print(msg, file=sys.stderr, flush=True)
        else:
            print(msg, flush=True)

    try:
        if args.list:
            st = status(project, args.voice_dir)
            if args.json:
                print(json.dumps(st, ensure_ascii=False, indent=1))
            else:
                _print_status(st)
            return 0

        if args.src:
            res = inherit(project, os.path.abspath(args.src), roles,
                          args.voice_dir, args.force, _log)
        elif args.official_adopt:
            if not args.adopt_role:
                raise SystemExit("--official-adopt 需要配 --adopt-role A|B")
            res = adopt_official(project, args.official_adopt, args.adopt_role,
                                 args.official_take, args.voice_dir,
                                 args.force, _log)
        elif args.adopt_from:
            if not args.adopt_role:
                raise SystemExit("--adopt-from 需要配 --adopt-role A|B")
            res = adopt(project, args.adopt_from, args.adopt_role,
                        args.voice_dir, args.force, _log)
        elif args.design_name:
            dcases = ROLES if args.design_case == "all" else (args.design_case,)
            res = design(project, args.design_name, args.design_desc,
                         args.design_text, args.voice_dir, args.device,
                         args.force, args.design_seed_offset, _log,
                         cases=dcases)
        else:
            res = build(project, roles,
                        {"A": args.voice_a, "B": args.voice_b},
                        args.voice_dir, args.force, _log)
        st = status(project, args.voice_dir)
        if args.json:
            print(json.dumps({"result": res, "status": st},
                             ensure_ascii=False, indent=1))
        else:
            print()
            _print_status(st)
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print("[失败] %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
