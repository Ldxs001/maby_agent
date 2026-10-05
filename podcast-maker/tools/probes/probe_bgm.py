# -*- coding: utf-8 -*-
"""钉死四档内置 BGM 的物理性质：是白噪音，还是确定性正弦叠加。

方法：
  1. 谱平坦度（spectral flatness）— 白噪音≈1，纯正弦→0。这是判"噪音/音调"的硬指标。
  2. 峰值频率表 + 音名换算 — 看是不是离散谐波（和弦），还是有连续宽带。
  3. 确定性检验 — 同档连生两次比对字节哈希；正弦公式无随机源。
  4. 低/高频带能量占比。

对照基线：程序生成的真白噪音（numpy 高斯白噪）。
"""
import hashlib
import io
import math
import os
import sys
import wave

sys.path.insert(0, os.path.abspath("."))
import numpy as np  # noqa: E402

from podcast_maker.assets_factory import make_bgm  # noqa: E402
from podcast_maker.config_manager import MODE_SPEC  # noqa: E402

OUT = os.path.join("_smoke", "_bgm_probe")
DUR = 5.0
SR = 22050

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_of(freq):
    """把频率换算成音名（A4=440）。"""
    if freq <= 0:
        return "-"
    n = 12.0 * math.log(freq / 440.0, 2.0) + 69.0
    midi = int(round(n))
    cents = (n - midi) * 100.0
    return "%s%d%+.0fc" % (NAMES[midi % 12], midi // 12 - 1, cents)


def read_wav(path):
    with wave.open(path, "rb") as wf:
        n, sr, ch, sw = (wf.getnframes(), wf.getframerate(),
                         wf.getnchannels(), wf.getsampwidth())
        raw = wf.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return a, sr, {"channels": ch, "sampwidth": sw, "frames": n}


def mid_segment(x, size):
    """取中段避 fade。"""
    off = max(0, (len(x) - size) // 2)
    seg = x[off:off + size]
    if len(seg) < size:
        seg = np.pad(seg, (0, size - len(seg)))
    return seg


def flatness(x, nfft=8192):
    seg = mid_segment(x, nfft) * np.hanning(nfft)
    mag = np.abs(np.fft.rfft(seg))[1:]
    mag = np.maximum(mag, 1e-12)
    return float(np.exp(np.mean(np.log(mag))) / np.mean(mag))


def peaks(x, sr, k=10, nfft=65536):
    seg = mid_segment(x, nfft) * np.hanning(nfft)
    mag = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    order = np.argsort(mag)[::-1]
    picked = []
    for i in order:
        if freqs[i] < 20:
            continue
        if all(abs(freqs[i] - freqs[j]) > 10 for j in picked):
            picked.append(i)
        if len(picked) >= k:
            break
    picked.sort(key=lambda i: freqs[i])
    top = mag[picked[0]] if picked else 1.0
    return [(float(freqs[i]), float(mag[i] / top)) for i in picked]


def band_ratio(x, sr, lo, hi):
    nfft = 32768
    seg = mid_segment(x, nfft) * np.hanning(nfft)
    mag = np.abs(np.fft.rfft(seg)) ** 2
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    tot = mag.sum() or 1e-12
    m = (freqs >= lo) & (freqs < hi)
    return float(mag[m].sum() / tot)


def main():
    os.makedirs(OUT, exist_ok=True)
    print("=" * 78)
    print("四档内置 BGM 物理性质实测")
    print("=" * 78)

    opts = MODE_SPEC["bgm.preset"]["options"]
    print("\n档位参数（config_manager.MODE_SPEC['bgm.preset']）：")
    for key, c in opts.items():
        print("  %-9s %-4s root=%-8.2f chord=%s" %
              (key, c["label"], c["root"], c["chord"]))

    print("\n" + "-" * 78)
    print("① 频谱性质（对照：真白噪音）")
    print("-" * 78)
    rng = np.random.default_rng(0)
    white = rng.standard_normal(SR * int(DUR)).astype(np.float64) * 0.2
    print("%-14s %-12s %-14s %-16s" % ("样本", "谱平坦度", "过零率", "峰均比(dB)"))
    wf0 = flatness(white)
    zcr_w = float(np.mean(np.abs(np.diff(np.sign(white)))) / 2.0)
    pa_w = 20 * math.log10(float(np.max(np.abs(white))) /
                           (float(np.sqrt(np.mean(white ** 2))) or 1e-12))
    print("%-14s %-12.4f %-14.4f %-16.1f" % ("白噪音(对照)", wf0, zcr_w, pa_w))

    rows = []
    for key, c in opts.items():
        path = os.path.join(OUT, "%s.wav" % key)
        info = make_bgm(key, path, duration=DUR, sample_rate=SR)
        a, sr, meta = read_wav(path)
        f = flatness(a)
        zcr = float(np.mean(np.abs(np.diff(np.sign(a)))) / 2.0)
        rms = float(np.sqrt(np.mean(a ** 2)))
        pk = float(np.max(np.abs(a)))
        pa = 20 * math.log10((pk or 1e-12) / (rms or 1e-12))
        low = band_ratio(a, sr, 60, 2000)
        high = band_ratio(a, sr, 6000, sr / 2)
        rows.append((key, c["label"], f, zcr, pa, rms, pk, low, high, path))
        print("%-14s %-12.4f %-14.4f %-16.1f" %
              ("%s(%s)" % (key, c["label"]), f, zcr, pa))

    print("\n" + "-" * 78)
    print("② 峰值频率 → 音名（前 10 峰，按频次排序，幅度归一）")
    print("-" * 78)
    for key, label, *_ in rows:
        a, sr, meta = read_wav(os.path.join(OUT, "%s.wav" % key))
        pk = peaks(a, sr, k=10)
        print("\n  【%s / %s】" % (key, label))
        for fr, rel in pk:
            print("     %8.2f Hz  %s  (相对幅度 %.3f)" % (fr, note_of(fr), rel))

    print("\n" + "-" * 78)
    print("③ 能量分布")
    print("-" * 78)
    print("%-16s %-18s %-18s" % ("样本", "60Hz-2kHz 占比", "6kHz-11kHz 占比"))
    print("%-16s %-18.1f%% %-18.1f%%" % ("白噪音(对照)",
                                          band_ratio(white, SR, 60, 2000) * 100,
                                          band_ratio(white, SR, 6000, SR / 2) * 100))
    for key, label, f, zcr, pa, rms, pk_, low, high, _p in rows:
        print("%-16s %-18.1f%% %-18.1f%%" % ("%s(%s)" % (key, label),
                                             low * 100, high * 100))

    print("\n" + "-" * 78)
    print("④ 确定性检验：同档连生两次，逐字节比对")
    print("-" * 78)
    for key, *_ in rows:
        h = []
        for i in range(2):
            p = os.path.join(OUT, "_det_%s_%d.wav" % (key, i))
            make_bgm(key, p, duration=DUR, sample_rate=SR)
            with open(p, "rb") as fh:
                h.append(hashlib.sha256(fh.read()).hexdigest()[:16])
        print("  %-9s %s  %s  → %s" %
              (key, h[0], h[1], "完全一致（无随机源）" if h[0] == h[1] else "不一致"))

    print("\n" + "-" * 78)
    print("⑤ WAV 容器头（看有无任何元数据/标识字段）")
    print("-" * 78)
    with open(os.path.join(OUT, "pensive.wav"), "rb") as fh:
        head = fh.read(64)
    print("  前 64 字节 repr：")
    print("  %r" % head)
    # 扫 chunk 列表
    with open(os.path.join(OUT, "pensive.wav"), "rb") as fh:
        data = fh.read()
    pos = 12
    chunks = []
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        try:
            sz = int.from_bytes(data[pos + 4:pos + 8], "little")
        except Exception:
            break
        chunks.append((cid.decode("latin-1"), sz))
        pos += 8 + sz + (sz & 1)
    print("  chunk 列表：%s" % chunks)
    print("  → 只有 fmt/data 即无任何元数据；有 LIST/INFO 才谈得上字段")

    print("\n产物目录：%s" % OUT)


if __name__ == "__main__":
    main()
