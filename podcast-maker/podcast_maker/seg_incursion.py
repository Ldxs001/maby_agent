# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""段级异常混入判据 —— 独立一维，与四维分 / 暗淡 / 身份 / 形态 / 嵌入并列。

它解决的那件事
-------------
老六维的 F0 是**整句中位**：0.31s 的局部混入被 90% 的正常语音抹平。实测把
一个确定性阳性句子拿到主仓同一把尺下量，四维分只有 0.74（线 4.25）——直接
放行。这一维把句子按**谱变点**切成段，逐段估基频，看有没有哪一段脱离了该
角色自己的段级常态，所以位置无关（句首 / 句中 / 句尾一样判）。

三条不能省的规矩
---------------
1. **基线按角色分别定**，决不许一套扫全场。A 是女高音、B 是男声，拿 B 的
   标准去扫 A，A 会被系统性全报（实测全期 1954 段报 312 段，绝大多数是 A）——
   这正是上一版"大面积过报"的机制。
2. **基频用段内平均谱 + 谐波和（SHS）求**，不用逐帧自相关取最大峰：自相关
   对男声普遍锁到第 2 谐波（实测 405-455Hz 的读数对应真实值 208-216Hz），
   假读数比真混入（344Hz）还高。拿它当尺子，确定性阳性必漏。
3. **分段在傅里叶域**：STFT 谱通量 + 倒谱通量找变点，不按固定时长硬切。

它不是什么（写在这里，免得后来的人误会）
-------------------------------------
第 8 维（本模块原有的段级基频偏离）测的是**段级基频相对该角色常态的偏离**，
不是"这段像不像女声"。它是异常检测，不是音色识别。它分不出"换人"和"同人
喊高"——这是已知边界，由第 9 维兜底（见下）。

第 9 维：音色否决（只压不增）
-------------
第 8 维命中的段，再过一遍**音色否决**：倒谱谱包络比对（声道形状）+ 声门
三件套 CPP / HNR / H1*–H2*（嗓音源）。机制实测（2026-10-08，B 池 83 句 +
A 池 88 句）：

- 真混入（self_i012_o1 / syn_head 两个独立件）给出同一套签名：声门三件套
  同向大偏（cpp↑ hnr↑ h1h2 由正转负）——量到的确实是"嗓子换了"。
- 同人喊高（0127_B 等）：抬音高不改变声门开商，三件贴池均值 ⇒ 包络与声门
  两把尺都正常 ⇒ 压掉。
- 金属污染：包络巨偏、声门/cpp 反而低（宽带噪声周期差）——另一条路，不压。

级联（每道都在定标数据上单独验过零误伤，见 `_ab_user_scheme6.log` 逐维
扫描与 `_ab_landing_verify.log` 端到端复验）：
  压掉 ⟺ d_cep < 绝对线(1.0)（包络形状贴池常态，根本不像混入）
       或 h1h2_z ≥ 0（真混入的 H1*–H2* 稳定为负）
否决只能往下压、不能新增报警；量不出包络/声门的段（极端短段）按"压不成"
处理——该报照报。残余（0113_B/0003_B 类，与真阳性在特征空间孪生）如实照报，
属人工耳听档。

报警线不再用固定 3.2：那是在"每句最高段"这个统计量上用逐段 σ 定线，口径
错——N≈10 段取 max 天然到 2–3σ，B 池实测 53% 句子超线。改为**按池自校准**：
线 = 该角色"每句最高段 z"分布的 90 分位（`calibrate`），文件太少建不起分布
时才退回固定线。

依赖
----
只用 numpy（主仓 requirements 里没有它，与 `_timbre_features` 同样按可选
依赖处理：缺了这维如实缺席，其余判据照常）。重采样、STFT、MFCC 全部用
numpy 自己实现，不引入 librosa/scipy —— 主仓的依赖表只有 edge-tts 与 Pillow。
"""
from __future__ import annotations

import os
import wave

# ---- 分段口径（与定标探针同：24kHz / 帧 42.7ms / 跳 10ms）----
# 这是**内部分析率**，不是出片率：输入无论什么率都先重采样到这里再分析，
# 判决阈值就是按这个口径定标的。与分析率无关的是出片链路，它一律走
# audio.sample_rate（统一推动点），两者别混。
SR_SEG = 24000
N_FFT = 1024
HOP = 240
GAP_ABSORB = int(0.12 * SR_SEG / HOP)
SILENCE_DB = 25.0
CP_WIN = 15
CP_MAD_K = 1.5
CP_MIN_GAP = int(0.20 * SR_SEG / HOP)
MIN_PIECE = int(0.20 * SR_SEG / HOP)
MAX_TOTAL = 12

# ---- 段级基频口径 ----
SR_F0 = 16000
NFFT_F0 = 8192
F0_LO, F0_HI = 70.0, 500.0
F0_FLOOR = 12.0          # sigma 下限：段级标准差不能被"几乎没散开"的样本压到 0
SEG_FRAME = int(0.05 * SR_F0)
SEG_HOP = int(0.025 * SR_F0)

# ---- 判据门 ----
DUR_GATE = 0.20          # 段长门（秒）：更短的段不参与判决（SHS 在短段上不稳）
# 段级偏离线（**退路值**）。正常路径由 `calibrate` 按池自校准：该角色"每句
# 最高段 z"分布的 q 分位（默认 90）——固定线在 max 统计量上口径错（N≈10 段
# 取 max 天然到 2-3σ，B 池实测 53% 句超线）。池太小（< MIN_CAL_FILES 句）
# 建不起分布时才退回这条固定线。
Z_LINE = 3.2

# ---- 第 9 维（音色否决）口径：与第 8 维同一段平均谱 / 同一条波形 ----
Q_CUT = int(0.002 * SR_F0)      # 32 样本：lifter 截止，须 < 1/500Hz = 2ms，
                                # 否则谐波梳（1/F0 ≥ 2ms）漏进包络
SD_FLOOR_DB = 0.6               # 包络系数 σ 下限：不许"几乎没散开"的池把 d_cep 抬飞
G_WIN = int(0.040 * SR_F0)      # 声门帧长 40ms（CPP/HNR 的定义窗口）
G_HOP = int(0.010 * SR_F0)      # 帧步 10ms
G_NFFT = 2048                   # 声门帧倒谱用（帧长 640 → 补零到 2048）
RMS_REL = 0.06                  # 有声帧门：相对全文件时域帧 RMS 95 分位。
                                # **参照量必须用时域帧 RMS**——STFT 幅度谱的
                                # 跨 bin RMS 是频谱量纲，比时域大 ~20 倍，
                                # 拿它当参照会把门抬到"最响帧的 120%"，
                                # 轻段全帧错杀、声门值假性缺失（2026-10-08 诊断）
ACF_MIN = 0.35                  # 周期门：归一化自相关低于它 = 无周期性帧
Q_LO = int(SR_F0 / 500.0)       # 倒谱峰搜索下界 2.0ms（500Hz）
Q_HI = int(SR_F0 / 70.0)        # 倒谱峰搜索上界 14.3ms（70Hz）
MIN_CAL_FILES = 8               # 池自校准最少句数：更少的池分位无意义
DCP_ABS = 1.0                   # d_cep 绝对压制线（实测零误伤线）

_CACHE: dict = {}

try:                          # 主仓 requirements 里没有 numpy，缺了就如实缺席
    import numpy as np        # noqa: F401
except ImportError:           # pragma: no cover - 环境相关
    np = None


def _ensure_np():
    if np is None:
        raise ImportError("段级异常混入判据需要 numpy —— 未安装时该维缺席，"
                          "其余判据照常工作。")


# 包络采样频点（对数铺 120Hz-5kHz，13 点）：跨过前三个共振峰的典型落位。
ENV_FREQS = np.geomspace(120.0, 5000.0, 13) if np is not None else None


def available() -> bool:
    """这维现在能不能跑。跑不了就如实缺席，不假装查过。"""
    return np is not None


# ----------------------------------------------------------------- 读与重采样
def _read_mono(path):
    """读 16bit PCM 波形成 float64 单声道。档案与成品都是它。"""
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        x = x[::ch]
    return x, int(sr)


def _resample(x, sr_from: int, sr_to: int):
    """FFT 重采样：只保留目标奈奎斯特以内的分量再做变长逆变换。

    这就是低通 + 重采样一起做了 —— 直接抽点（`x[::step]`）会把高频折叠
    回来污染低音区，而这一维量的正是低音区，折叠进来就是假读数。
    """
    if int(sr_from) == int(sr_to):
        return np.asarray(x, dtype=np.float64)
    n_out = int(round(len(x) * sr_to / float(sr_from)))
    if n_out < 1:
        return np.zeros(0, dtype=np.float64)
    X = np.fft.rfft(np.asarray(x, dtype=np.float64))
    n_keep = n_out // 2 + 1
    m = min(len(X), n_keep)
    Y = np.zeros(n_keep, dtype=complex)
    Y[:m] = X[:m]
    y = np.fft.irfft(Y, n_out)
    return y * (n_out / float(len(x)))


def _load_at(path, sr_target: int):
    x, sr = _read_mono(path)
    if sr != sr_target:
        x = _resample(x, sr, sr_target)
    return np.ascontiguousarray(x, dtype=np.float64)


# ----------------------------------------------------------------- 分段特征
def _mel_filterbank(sr: int, n_fft: int, n_mels: int = 40):
    fmax = sr / 2.0

    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    mels = np.linspace(hz2mel(0.0), hz2mel(fmax), n_mels + 2)
    hz = mel2hz(mels)
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    nb = n_fft // 2 + 1
    bins = np.clip(bins, 0, nb - 1)
    fb = np.zeros((n_mels, nb))
    for i in range(n_mels):
        l, c, r = int(bins[i]), int(bins[i + 1]), int(bins[i + 2])
        # 低频端相邻 bin 会撞到一起，退化成零宽三角 —— 撑开一格，别让它除零。
        if c <= l:
            c = min(l + 1, nb - 1)
        if r <= c:
            r = min(c + 1, nb)
        if c > l:
            fb[i, l:c] = (np.arange(l, c) - l) / float(c - l)
        if r > c:
            fb[i, c:r] = (r - np.arange(c, r)) / float(r - c)
    return fb


def _dct2(x, n_out: int = 13):
    """DCT-II（正交归一）。取代 librosa 的 mfcc，只取前 n_out 个系数。"""
    n_mels, frames = x.shape
    k = np.arange(n_out)[:, None]
    n = np.arange(n_mels)[None, :]
    basis = np.cos(np.pi * k * (2 * n + 1) / (2.0 * n_mels)) * np.sqrt(2.0 / n_mels)
    basis[0] = basis[0] * np.sqrt(0.5)
    return basis @ x


def _stft_mag(x, n_fft: int = N_FFT, hop: int = HOP):
    if len(x) < n_fft:
        return None
    n = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx] * np.hanning(n_fft)
    return np.abs(np.fft.rfft(frames, n_fft)).T


def _seg_feats(x, sr: int = SR_SEG):
    """分段用的三样：谱（求平均谱用）、每帧能量 dB（找有声块）、
    谱通量与倒谱通量（找变点）。"""
    S = _stft_mag(x)
    if S is None or S.shape[1] < 8:
        return None
    rms = np.sqrt((S * S).mean(axis=0))
    db = 20.0 * np.log10(rms + 1e-9)
    dm = np.diff(S, axis=1)
    flux = np.concatenate([[0.0], np.maximum(dm, 0.0).sum(axis=0)])
    mel = _mel_filterbank(sr, N_FFT) @ (S * S)
    M = _dct2(np.log(mel + 1e-10), 13)
    cf = np.concatenate([[0.0], np.linalg.norm(np.diff(M, axis=1), axis=0)])
    return {"db": db, "flux": flux, "cf": cf, "M": M}


def _blocks(fe):
    """有声块：低于「最响 − 25dB」的帧算静音，短空洞吸收掉。只作变点搜索范围。"""
    sp = fe["db"] > (float(np.max(fe["db"])) - SILENCE_DB)
    idx = np.where(sp)[0]
    if idx.size == 0:
        return []
    out, s, p = [], int(idx[0]), int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - p - 1 > GAP_ABSORB:
            out.append((s, p + 1))
            s = i
        p = i
    out.append((s, p + 1))
    return [(a, b) for a, b in out if b - a >= 6]


def _change_points(score, lo: int, hi: int):
    """局部极大 + 中位/MAD 门限。门限从这一段自己的分布来，不写死绝对值。"""
    if hi - lo < 3 * CP_WIN:
        return []
    seg = score[lo:hi]
    med = float(np.median(seg))
    mad = float(np.median(np.abs(seg - med)))
    thr = med + CP_MAD_K * 1.4826 * mad
    cps = []
    for i in range(lo + CP_WIN, hi - CP_WIN):
        w = score[i - CP_WIN:i + CP_WIN + 1]
        if score[i] >= w.max() and score[i] > thr:
            if not cps or i - cps[-1] >= CP_MIN_GAP:
                cps.append(i)
    return cps


def _merge_short(pieces, M, floor: int):
    """把过短的段并进谱形最像的邻居 —— 段太短时 SHS 估不出基频。"""
    changed = True
    while changed and len(pieces) > 1:
        changed = False
        for k, (a, b) in enumerate(pieces):
            if b - a >= floor:
                continue
            mv = M[:, a:b].mean(axis=1)
            cand = []
            if k > 0:
                pa, pb = pieces[k - 1]
                cand.append((float(np.linalg.norm(mv - M[:, pa:pb].mean(axis=1))), k - 1, k))
            if k + 1 < len(pieces):
                na, nb = pieces[k + 1]
                cand.append((float(np.linalg.norm(mv - M[:, na:nb].mean(axis=1))), k, k + 1))
            if not cand:
                continue
            cand.sort()
            _, i1, i2 = cand[0]
            a2, b2 = pieces[i1], pieces[i2]
            pieces[i1:i2 + 1] = [(a2[0], b2[1])]
            changed = True
            break
    return pieces


def _full_cover(pieces, n: int):
    """段边界延到句首/句尾。中间空洞**不并**：那些是低于音高门的真静音，
    异常混入一定带能量，不会掉到门以下；但句首/句尾的低能量段必须进来，
    否则句首的混入整段不参与判决（探针里踩过这个洞）。"""
    if not pieces:
        return [(0, n)]
    pcs = list(pieces)
    pcs[0] = (0, pcs[0][1])
    pcs[-1] = (pcs[-1][0], n)
    return pcs


def _segment(fe):
    n = len(fe["db"])
    st = (fe["flux"] / (float(np.median(fe["flux"])) + 1e-9)
          + fe["cf"] / (float(np.median(fe["cf"])) + 1e-9))
    pieces = []
    for lo, hi in _blocks(fe):
        edges = [lo] + _change_points(st, lo, hi) + [hi]
        pieces += [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    pieces = _merge_short(pieces, fe["M"], MIN_PIECE)
    pieces = _full_cover(pieces, n)
    while len(pieces) > MAX_TOTAL:
        k = min(range(len(pieces)), key=lambda i: pieces[i][1] - pieces[i][0])
        a, b = pieces[k]
        if k == 0:
            pieces[0:2] = [(a, pieces[1][1])]
        else:
            pieces[k - 1:k + 1] = [(pieces[k - 1][0], b)]
    return _full_cover(pieces, n)


# ----------------------------------------------------------------- 段级基频
def _seg_spectrum(x, t0: float, t1: float, frame: int, hop: int):
    """段内平均幅度谱（对齐段中心落在 [t0,t1] 的帧）。

    为什么要平均：逐帧谱里倍频错帧和噪声帧各占一部分，平均把偶发错帧稀释掉，
    留下的是这一段**稳定存在**的谐波结构 —— 这才是判官该看的。
    """
    if len(x) < frame:
        return None, 0
    n = 1 + (len(x) - frame) // hop
    acc, cnt = None, 0
    for i in range(n):
        s = i * hop
        th = (s + frame / 2.0) / SR_F0
        if th < t0 or th > t1:
            continue
        w = x[s:s + frame]
        S = np.abs(np.fft.rfft((w - w.mean()) * np.hanning(frame), NFFT_F0))
        acc = S.copy() if acc is None else acc + S
        cnt += 1
    if acc is None or cnt == 0:
        return None, 0
    return acc / cnt, cnt


def _shs_f0(S, freqs):
    """谐波和（SHS）求真基频：让**整条谐波串**同时成立的 f0 才算数。

    这是这一维能成立的关键。逐帧自相关取最大峰会锁到第 2 谐波（男声普遍
    如此），读出来的假值比真混入还高；SHS 要求 1..6 次谐波在谱上都有峰，
    倍频假设会因为奇次谐波缺席而掉分。
    """
    if S is None:
        return None
    df = float(freqs[1] - freqs[0])
    best_score, best_f = 0.0, None
    for f in np.arange(F0_LO, F0_HI + 0.5, 0.5):
        tot = 0.0
        for hh in range(1, 7):
            fc = f * hh
            if fc >= 3800.0:
                break
            j = int(round(fc / df))
            if 1 <= j < len(S) - 1:
                tot += float(S[j - 1:j + 2].max())
        if tot > best_score:
            best_score, best_f = tot, float(f)
    return best_f


# ----------------------------------------------------------------- 第 9 维特征
# 全部从 `_seg_spectrum` 的段平均谱与 x16 波形上算，与第 8 维同源同口径，
# 不多做一次分段。实现与定标探针 _ab_timbre9.py 逐式一致（2026-10-08 定标）。
def _envelope_db(Savg):
    """低 quefrency liftering 后的 log 谱包络（dB）。

    log|S| = log|E| + log|H|：对 log 谱再做第二次变换（倒谱），
    低 quefrency = 声道传输函数 = 音色；高 quefrency = 基频周期 = 音高。
    保留 <2ms 的 quefrency ⇒ 谐波梳（1/F0 ≥ 2ms）整条被切掉 ⇒ 对 F0 免疫。
    """
    logS = np.log10(Savg + 1e-12)
    c = np.fft.irfft(logS, NFFT_F0)
    c2 = np.zeros_like(c)
    c2[:Q_CUT + 1] = c[:Q_CUT + 1]
    c2[NFFT_F0 - Q_CUT:] = c[NFFT_F0 - Q_CUT:]
    return np.fft.rfft(c2).real * 20.0


def _env_feats(e):
    """(形状, 含倾斜)。形状 = 逐点减自身均值（去掉增益与倾斜，只留共振峰
    形状）；含倾斜 = 逐点减全带均值（去掉增益，留着谱倾斜）。"""
    fr = np.fft.rfftfreq(NFFT_F0, 1.0 / SR_F0)
    idx = np.clip(np.searchsorted(fr, ENV_FREQS), 0, len(e) - 1)
    v = e[idx]
    return v - v.mean(), v - float(e.mean())


def _env_at(e, f):
    fr = np.fft.rfftfreq(NFFT_F0, 1.0 / SR_F0)
    return float(np.interp(f, fr, e))


def _hnr_of(w):
    """帧 HNR：归一化自相关峰高的 10log10(r/(1-r))（Praat / de Krom 口径）。"""
    x = w - w.mean()
    n = len(x)
    N2 = 1
    while N2 < 2 * n:
        N2 *= 2
    X = np.fft.rfft(x, N2)
    r = np.fft.irfft(np.abs(X) ** 2, N2)[:n]
    if r[0] <= 1e-18:
        return None
    r = r / r[0]
    lo, hi = Q_LO, min(Q_HI, n - 1)
    if hi <= lo:
        return None
    rmax = float(r[lo:hi].max())
    if rmax < ACF_MIN:
        return None
    rmax = min(rmax, 0.999999)
    return 10.0 * np.log10(rmax / (1.0 - rmax))


def _cpp_of(c):
    """帧 CPP：倒谱峰相对该区间线性回归线的高度（Hillenbrand 1994）。"""
    seg = c[Q_LO:Q_HI + 1]
    k = int(np.argmax(seg))
    x = np.arange(len(seg), dtype=np.float64)
    a, b = np.polyfit(x, seg, 1)
    return float(seg[k] - (a * k + b))


def _rms_ref_of(x):
    """全文件时域帧 RMS 的 95 分位（SEG_FRAME/SEG_HOP 帧格）——声门能量门
    的参照量。**不许用 STFT 幅度谱的跨 bin RMS**：那是频谱量纲（见常量注）。"""
    nfr = 1 + (len(x) - SEG_FRAME) // SEG_HOP
    if nfr < 1:
        return None
    fidx = np.arange(SEG_FRAME)[None, :] + SEG_HOP * np.arange(nfr)[:, None]
    return float(np.percentile(np.sqrt((x[fidx] ** 2).mean(axis=1)), 95))


def _glottal(x, t0, t1, rms_ref):
    """段内逐帧 CPP / HNR 的中位。能量门 + 周期门双门都过不了 ⇒ (None,None,0)。"""
    a, b = int(t0 * SR_F0), min(int(t1 * SR_F0), len(x))
    cpps, hnrs = [], []
    i = a
    while i + G_WIN <= b:
        w = x[i:i + G_WIN]
        i += G_HOP
        if float(np.sqrt(np.mean(w ** 2))) < RMS_REL * rms_ref:
            continue
        h = _hnr_of(w)
        if h is None:
            continue
        mag = np.abs(np.fft.rfft((w - w.mean()) * np.hanning(G_WIN), G_NFFT))
        c = np.fft.irfft(np.log(mag + 1e-12), G_NFFT)
        cpps.append(_cpp_of(c))
        hnrs.append(h)
    if not cpps:
        return None, None, 0
    return float(np.median(cpps)), float(np.median(hnrs)), len(cpps)


def _h1h2(Savg, e, f0):
    """H1*–H2*：第 1、2 谐波幅度差再减倒谱包络在两点的落差——把声道影响
    去掉，剩下的就是声门源谱倾斜（Holmberg / Shue 系口径）。真混入为负。"""
    if f0 is None or 2 * f0 > 0.45 * SR_F0:
        return None
    fr = np.fft.rfftfreq(NFFT_F0, 1.0 / SR_F0)
    df = float(fr[1] - fr[0])
    w = max(2, int(round(0.03 * f0 / df)))

    def pk(fc):
        j = int(round(fc / df))
        lo, hi = max(0, j - w), min(len(Savg), j + w + 1)
        if hi <= lo:
            return None
        return float(Savg[lo:hi].max())

    a1, a2 = pk(f0), pk(2 * f0)
    if not a1 or not a2:
        return None
    obs = 20.0 * np.log10(a1) - 20.0 * np.log10(a2)
    return obs - (_env_at(e, f0) - _env_at(e, 2 * f0))


def _timbre_dims(x16, row, Savg, rms_ref):
    """往段行上挂第 9 维特征：包络形状（去增益去倾斜的 13 点）+ 声门三件套。
    量不出就如实缺（env=None / 三件 None），判定侧按"压不成"处理。"""
    try:
        e = _envelope_db(Savg)
        shape, _ = _env_feats(e)
        cpp, hnr, _ = _glottal(x16, row["t0"], row["t1"], rms_ref)
        row["env"] = [float(v) for v in shape]
        row["cpp"] = cpp
        row["hnr"] = hnr
        row["h1h2"] = _h1h2(Savg, e, row["f0"])
    except Exception:                      # noqa: BLE001 - 第 9 维缺席不拖垮第 8 维
        row["env"] = None
        row["cpp"] = row["hnr"] = row["h1h2"] = None


def measure(path: str, dur_gate: float = DUR_GATE):
    """切段 + 逐段求真基频，返回 [{"t0","t1","dur","f0","n"}]。

    结果按 (路径, 文件大小, mtime, 门) 缓存 —— 体检会逐句调它两次
    （先量基线、再判句），文件在两次之间不会变，缓存是安全的。
    """
    _ensure_np()
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (os.path.abspath(path), st.st_size, round(st.st_mtime, 3), dur_gate)
    if key in _CACHE:
        return _CACHE[key]

    x24 = _load_at(path, SR_SEG)
    x16 = _load_at(path, SR_F0)
    fe = _seg_feats(x24, SR_SEG)
    if fe is None:
        _CACHE[key] = []
        return []
    freqs = np.fft.rfftfreq(NFFT_F0, 1.0 / SR_F0)
    rms_ref = _rms_ref_of(x16)
    rows = []
    for a, b in _segment(fe):
        t0, t1 = a * HOP / float(SR_SEG), b * HOP / float(SR_SEG)
        S, cnt = _seg_spectrum(x16, t0, t1, SEG_FRAME, SEG_HOP)
        if S is None or cnt < 3:
            continue
        f0 = _shs_f0(S, freqs)
        if f0 is None:
            continue
        row = {"t0": round(t0, 2), "t1": round(t1, 2),
               "dur": round(t1 - t0, 2), "f0": f0, "n": cnt}
        if rms_ref is not None:
            _timbre_dims(x16, row, S, rms_ref)
        rows.append(row)
    _CACHE[key] = rows
    return rows


def _robust(arr):
    """中位 + 1.4826×MAD（NaN 忽略），σ 带 1e-9 下限。"""
    m = float(np.nanmedian(arr))
    s = max(float(1.4826 * np.nanmedian(np.abs(arr - m))), 1e-9)
    return m, s


def baseline(seg_lists):
    """角色段级基线：mu = 全部段 f0 的中位，sigma = 1.4826×MAD（带下限）。

    输入是**该角色所有句子**的段列表 —— 不是参考音频自己那一条。参考音频
    只有 8 秒、十几段，段级标准差在两版之间能差 50% 以上（实测过），
    拿它当分母，线的含义随素材抖，判出来的 z 不可比。

    第 9 维的池分布也在这里一并收集：包络 13 系数逐系数 robust μ/σ
    （σ 带 SD_FLOOR_DB 下限），声门三件套各一对 robust μ/σ。段行没带
    第 9 维特征（旧缓存 / numpy 缺失期）就如实少收，不凑数。
    """
    _ensure_np()
    flat = [r for segs in seg_lists for r in segs]
    v = np.array([r["f0"] for r in flat], dtype=float)
    if v.size == 0:
        return None
    mu = float(np.median(v))
    sd = max(float(1.4826 * np.median(np.abs(v - mu))), F0_FLOOR)
    base = {"mu": mu, "sd": sd, "n": int(v.size)}

    env_rows = [r for r in flat if r.get("env") is not None]
    if env_rows:
        env = np.array([r["env"] for r in env_rows], dtype=float)
        base["env_mu"] = np.median(env, axis=0)
        base["env_sd"] = np.maximum(
            1.4826 * np.median(np.abs(env - np.median(env, axis=0)), axis=0),
            SD_FLOOR_DB)
    gl_arr = {}
    for nm in ("cpp", "hnr", "h1h2"):
        arr = np.array([r.get(nm) if r.get(nm) is not None else np.nan
                        for r in flat], dtype=float)
        gl_arr[nm] = arr
        if np.isfinite(arr).any():
            base.setdefault("gl", {})[nm] = _robust(arr)
    if base.get("gl"):
        base["gl_arr"] = gl_arr
    return base


def score(segs, base, dur_gate: float = DUR_GATE):
    """这一句里最脱线的那一段，返回 {"z","t0","t1","dur","f0",...} 或 None。"""
    if base is None or not segs:
        return None
    cand = [r for r in segs if r["dur"] >= dur_gate]
    if not cand:
        return None
    b = max(cand, key=lambda r: r["f0"])
    out = dict(b)
    out["z"] = (b["f0"] - base["mu"]) / (base["sd"] or 1.0)
    return out


# ----------------------------------------------------------------- 第 9 维判定
def d_cep(row, base):
    """倒谱谱包络距离：形状 13 系数逐系数按池 σ 归一后的 RMS。
    基线没包络分布（池太老 / 特征缺席）时如实返回 None。"""
    env_mu = base.get("env_mu")
    if env_mu is None or row.get("env") is None:
        return None
    v = np.asarray(row["env"], dtype=float)
    return float(np.sqrt(np.mean(((v - env_mu) / base["env_sd"]) ** 2)))


def d_glot(row, base):
    """声门距离：CPP / HNR / H1*–H2* 三件按池 σ 归一后的 RMS。
    三件全缺（帧门全灭 / 基线没分布）时返回 None——判定侧按"压不成"处理。"""
    gl = base.get("gl")
    if not gl:
        return None
    zs = []
    for nm in ("cpp", "hnr", "h1h2"):
        val = row.get(nm)
        if val is None or nm not in gl:
            continue
        m, sd = gl[nm]
        zs.append((val - m) / (sd or 1.0))
    if not zs:
        return None
    return float(np.sqrt(np.mean(np.square(zs))))


def h1h2_z(row, base):
    """H1*–H2* 的池 z 分数。真混入稳定为负（声门源谱变陡），用力说话常 ≥0。"""
    gl = base.get("gl") or {}
    val, m_sd = row.get("h1h2"), gl.get("h1h2")
    if val is None or m_sd is None:
        return None
    return (val - m_sd[0]) / (m_sd[1] or 1.0)


def calibrate(per_file_segs, base, q: float = 90.0,
              dur_gate: float = DUR_GATE):
    """报警线**按池自校准**，不写死绝对值。

    报警线 = 该角色「每句最高段 z」分布的 q 分位。旧固定线 3.2 在这个统计
    量上口径错：N≈10 段取 max 天然到 2-3σ，与信号无关，B 池实测 53% 句超线。

    返回 {"line","n_files","n_alert"}；句数不足 MIN_CAL_FILES（分位无意义）
    时返回 None，调用方退回固定线 + 关否决。

    **曾经有第三道 p80/p70 幅度组合否决**（d_cep 与 d_glot 同时落在报警句
    分布常态区就压），端到端复验时砍掉：报警线提到 90 分位后报警句只剩
    9 句，p80/p70 在这个样本量上框出的"常态区"宽到把真混入 self_i012_o1
    （d_cep 2.25 / d_glot 3.36 vs 区 2.44/3.53）也圈了进去。样本太薄的
    分位线不可靠——"第 9 维只能往下压"还有镜像纪律：**也不能压到真阳性
    头上**，宁可少压。
    """
    _ensure_np()
    if base is None or not per_file_segs:
        return None
    zs = []
    for segs in per_file_segs:
        s = score(segs, base, dur_gate)
        if s is None:
            continue
        zs.append(s["z"])
    if len(zs) < MIN_CAL_FILES:
        return None
    line = float(np.percentile(zs, q))
    return {"line": line, "n_files": len(zs),
            "n_alert": int(sum(1 for z in zs if z >= line))}


def veto_hit(row, base, veto):
    """第 9 维否决：返回 True = 第 8 维的命中被压掉（同人抬音高，非混入）。

    两道级联，每道都在定标数据上单独验过零误伤（`_ab_landing_verify.log`
    端到端复验 + `_ab_user_scheme6.log` 逐维扫描）：
    ① d_cep < cep_abs——倒谱谱包络形状贴池常态的段根本不像混入；
    ② h1h2_z ≥ 0——真混入的 H1*–H2* 稳定为负（声门源谱变陡），同嗓子抬
       音高不改变声门开商。
    量不出的量按"压不成"处理：第 9 维只能往下压，绝不因为缺席就放行报
    不出的病。
    """
    dc = d_cep(row, base)
    if dc is not None and dc < veto["cep_abs"]:
        return True
    hz = h1h2_z(row, base)
    return hz is not None and hz >= 0.0


def flag(segs, base, line: float = Z_LINE, dur_gate: float = DUR_GATE,
         veto=None):
    """超线就返回一句人话（写进命中原因），否则 None。

    只报**上偏**：段级基频偏低是音色自然差异（低沉、气声），不是混入。
    `veto`（`calibrate` 的产物 + "cep_abs"）给出时，命中的段先过第 9 维
    否决——被压掉返回 None，理由是"同一嗓子抬音高，非混入"，不写进命中
    原因（压掉的不是病，不值得留痕）。
    """
    s = score(segs, base, dur_gate)
    if s is None or s["z"] < line:
        return None
    if veto is not None and veto_hit(s, base, veto):
        return None
    return "混入%.2fσ(%.2fs@%.0fHz)" % (s["z"], s["dur"], s["f0"])
