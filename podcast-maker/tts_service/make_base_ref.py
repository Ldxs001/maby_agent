# -*- coding: utf-8 -*-
"""给角色生成 ref_base，并用它拼出 ICL 参考音频。

为什么要有 ref_base
------------------
ICL 模式把参考音频连同它的转录文本一起放进上下文 —— 相当于跟模型说
「你听一遍这个人怎么说话，再照这个念」。只给一条 ref 时，模型是从**单条
样例**里推「这个人的标准状态」：那条样例里如果恰好有个词音高飙上去，
模型会把它当成风格学走。补一条锚就是这件事的解法。

ref_base = 拿同一段文本、同一份 ref 当参考，用 Base 克隆模型把内容**重生成
一遍**得到的「该角色最标准的一次读法」。

采样率：不在这里定，也不由人定
----------------------------
唯一的出处在**统一推动点 `audio.sample_rate`**（主程序 config.json →
config_manager.PARAM_SPEC 的默认值）。本地语音服务只会吐 24000（模型固有），
所以这里把它重采样到统一口径再落盘 —— 出片链路上所有音频同源同率，不再出现
「参考 24k / 成品 44.1k 两把尺子」那种事后对不上的事。

产物（落在 <项目>/音色/<角色>/，**不动 ref.wav**）
  ref_base_<sign>.wav   候选里挑「平均谱形最贴 ref」的那条（谱形=音色，与音高无关）
  icl_<sign>.wav        ref_base_<sign>.wav + 0.15s 静音 + ref_base_<sign>.wav —— 一条嗓子，两段
  icl_<sign>.txt        两段转录连写（ICL 要求转录和音频内容对齐，缺了会串内容）
  ref_base_<sign>.json  溯源记录（采样率、种子、候选、谱相似度、设备、后端）
  base_refs.json        版本账本：sign ↔ 原生 ref 指纹 ↔ 文件组，active 指当前生效

sign = 生成那一刻 ref.wav 内容指纹前 8 位 —— 同一原生 ref 永远只克隆一次，
账本命中直接复用（重做 = Base 克隆重掷骰子，音色会变）；换嗓老版本原样
不动，换回去时账本命中、引用切回去即可。

主程序读哪一对由 `tts_engine.voice_profiles()` 定：账本 active 指向的 icl
两件齐就用 icl，缺件 fail-closed 报错（不静默退回 ref —— 那等于换嗓）；
没有账本的项目（从没做过 ref_base），行为与既往一致直接用 ref。

用法
----
  python make_base_ref.py --project-dir <项目目录> [--role A|B|both] [--force] [--json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import wave
from math import gcd

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # 仓库根 —— 主程序包 podcast_maker 在这里
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import make_voice as mv      # noqa: E402
import serve                 # noqa: E402

ROLES = ("A", "B")
# 版本化命名：ref_base_<sign>.wav / icl_<sign>.wav / icl_<sign>.txt，
# sign = 生成那一刻 ref.wav 内容指纹前 8 位。三个常量与
# podcast_maker/layout.py 同名，tests/test_tts_engine.py 锁。
BASEREF_STEM = "ref_base"
ICL_STEM = "icl"
BASE_REFS_NAME = "base_refs.json"


def baseref_name(sign: str) -> str:
    return "%s_%s.wav" % (BASEREF_STEM, sign)


def icl_wav_name(sign: str) -> str:
    return "%s_%s.wav" % (ICL_STEM, sign)


def icl_txt_name(sign: str) -> str:
    return "%s_%s.txt" % (ICL_STEM, sign)


def record_name(sign: str) -> str:
    return "%s_%s.json" % (BASEREF_STEM, sign)

# 两段参考音频之间的静音。定这个值时算的是「模型能不能把两条当成两段」：
# 太短会连成一段（模型读成同一个人的连续两次发声，锚的作用消失），太长
# 白白占上下文。0.15s 是时长，与采样率无关 —— 帧数按目标率现算。
GAP_SECONDS = 0.15
CANDS = 3


# ----------------------------------------------------------------- 采样率
def resolve_sample_rate(override=None) -> int:
    """取统一推动点的采样率：`audio.sample_rate`。

    优先级照主程序那一套：命令行的 --sample-rate > config.json > 配置表默认值。
    这里是**唯一**的采样率出处 —— 不写死 24000/44100/48000 里的任何一个。
    读不到就报错退出，不猜一个数顶着（猜错了要等出片对不上才会发现）。
    """
    if override is not None:
        sr = int(override)
        if sr <= 0:
            raise RuntimeError("--sample-rate 必须是正整数，收到 %r" % (override,))
        return sr
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from podcast_maker import config_manager as cm
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "读不到统一配置（%s）；采样率必须来自 audio.sample_rate，"
            "不在这里猜一个。可用 --sample-rate 显式覆盖。" % e)
    try:
        mgr = cm.ConfigManager()
        mgr.load()
        sr = mgr.get("audio.sample_rate")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("统一配置读取失败（%s）；采样率出处缺失，不猜。" % e)
    if not sr:
        raise RuntimeError(
            "统一配置里没有 audio.sample_rate（%s）；采样率出处缺失，不猜。"
            % getattr(mgr, "path", "?"))
    return int(sr)


# ----------------------------------------------------------------- 波形读写
def _read_wav(path: str):
    """读成 (float32 单声道, 采样率)。只认 16bit PCM —— 档案一律是它。"""
    with wave.open(path, "rb") as w:
        sr, ch, sw, n = (w.getframerate(), w.getnchannels(),
                         w.getsampwidth(), w.getnframes())
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError("参考音频不是 16bit PCM：%s（%d 字节/样本）" % (path, sw))
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(a, dtype=np.float32), int(sr)


def _write_wav(path: str, a, sr: int) -> None:
    """原子写：先写 .tmp 再 replace，不留半个文件。"""
    pcm = np.clip(np.asarray(a, dtype=np.float32).reshape(-1), -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)


def _resample(a, sr_from: int, sr_to: int):
    """把波形重采样到目标采样率（带抗混叠）。同率直接返回。"""
    if int(sr_from) == int(sr_to):
        return np.asarray(a, dtype=np.float32)
    from scipy.signal import resample_poly

    g = gcd(int(sr_from), int(sr_to))
    out = resample_poly(np.asarray(a, dtype=np.float32),
                        int(sr_to) // g, int(sr_from) // g)
    return np.ascontiguousarray(out, dtype=np.float32)


# ----------------------------------------------------------------- 候选挑选
def _log_spectrum(x, sr: int, n_fft: int = 2048,
                  fmin: float = 70.0, fmax: float = 7000.0):
    """整段平均幅度谱的对数压缩，只留语音带。

    为什么用它挑候选、而不是用 F0：候选之间要比的是「像不像同一个人」，
    那是**谱形**（音色）的事，不是音高的事。自相关法测 F0 会锁到第 2 谐波
    上翻倍（实测过），拿一把会翻倍的尺子挑锚，挑出来的可能不是最像的那条。
    平均谱把帧间抖动稀释掉，稳定且与音高无关。
    """
    a = np.asarray(x, dtype=np.float64).reshape(-1)
    if a.size < n_fft:
        a = np.pad(a, (0, n_fft - a.size))
    a = a - a.mean()
    win = np.hanning(n_fft)
    hop = n_fft // 2
    acc = None
    for s in range(0, len(a) - n_fft + 1, hop):
        S = np.abs(np.fft.rfft(a[s:s + n_fft] * win, n_fft))
        acc = S if acc is None else acc + S
    if acc is None:
        return None
    f = np.fft.rfftfreq(n_fft, 1.0 / sr)
    band = (f >= fmin) & (f <= fmax)
    v = acc[band]
    if not v.size:
        return None
    v = v / (float(v.max()) or 1.0)
    return np.log1p(v * 100.0)


def _spec_cos(x, sr_x: int, ref, sr_ref: int) -> float:
    """平均谱余弦相似度。1.0 = 谱形完全一致。"""
    sx = _log_spectrum(x, sr_x)
    sr_ = _log_spectrum(ref, sr_ref)
    if sx is None or sr_ is None or sx.shape != sr_.shape:
        return float("nan")
    d = float(np.linalg.norm(sx) * np.linalg.norm(sr_))
    if d <= 1e-12:
        return float("nan")
    return float(np.dot(sx, sr_) / d)


# ----------------------------------------------------------------- 生成本体
def build_role(eng, root: str, role: str, force: bool, log,
               sample_rate: int) -> dict:
    """为一个角色生成 ref_base_<sign> 三件并记账；命中账本直接复用。

    sign = 当前 ref.wav 内容指纹前 8 位 —— 它就是「这次克隆自哪个原生 ref」
    的版本号。账本里已有该 sign 且三件齐 → 直接复用（**同一原生 ref 永远只
    克隆一次**，重做等于 Base 克隆重掷骰子，音色会变）；--force 是人主动要
    求的重掷，同 sign 覆盖重做。
    """
    rd = mv.role_dir(root, role)
    ref_wav = os.path.join(rd, mv.WAV_NAME)
    ref_txt = os.path.join(rd, mv.TXT_NAME)
    if not (os.path.isfile(ref_wav) and os.path.isfile(ref_txt)):
        raise RuntimeError("%s 角还没有音色档案（%s / %s 不齐）"
                           % (role, mv.WAV_NAME, mv.TXT_NAME))

    sign = mv.digest_of(ref_wav)[:8]
    base_path = os.path.join(rd, baseref_name(sign))
    icl_wav = os.path.join(rd, icl_wav_name(sign))
    icl_txt = os.path.join(rd, icl_txt_name(sign))

    led = mv.read_base_refs(root, role) or {"schema": 1, "active": None,
                                            "entries": {}}
    entry = led["entries"].get(sign)
    if entry and not force and all(os.path.isfile(p) for p in
                                   (base_path, icl_wav, icl_txt)):
        if led.get("active") != sign:
            led["active"] = sign
            mv.write_base_refs(root, role, led)
        log("%s 角 ref_base 命中账本（sign %s，原生 ref %s），直接复用不重做。"
            % (role, sign, entry.get("ref_sha16", "?")))
        return {"role": role, "skipped": True, "hit": True, "sign": sign,
                "base_ref": base_path, "icl_wav": icl_wav, "icl_txt": icl_txt}

    with io.open(ref_txt, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        raise RuntimeError("%s 角的 %s 是空的 —— ICL 缺转录会把内容串进结果。"
                           % (role, mv.TXT_NAME))

    ref, sr_ref = _read_wav(ref_wav)
    ref_t = _resample(ref, sr_ref, sample_rate)   # 仅用于跟候选比谱形，不动档案
    log("%s 角 ref：%.2fs / %dHz → 统一口径 %dHz · %d 字 · sign %s"
        % (role, len(ref) / sr_ref, sr_ref, sample_rate, len(text), sign))

    base_seed = serve.seed_for(text, "baseref:" + role)
    cands, arrs = [], []
    for k in range(CANDS):
        seed = base_seed + k * 100003
        t1 = time.time()
        wav, sr = eng._gen_clone(text=text, ref_audio=ref_wav, ref_text=text,
                                 instruct=None, language="Chinese", seed=seed)
        # 服务固定吐 24000（模型固有的），统一到推动点口径再落盘 —— 出片链路
        # 上所有音频同源同率，不再出现「参考一套率、成品另一套率」那种两把尺子
        arr = _resample(np.asarray(wav, dtype=np.float32).reshape(-1),
                        int(sr), sample_rate)
        arrs.append(arr)
        sc = _spec_cos(arr, sample_rate, ref_t, sample_rate)
        cands.append({"index": k, "seed": seed, "spec_cos": sc,
                      "seconds": round(len(arr) / sample_rate, 3),
                      "gen_seconds": round(time.time() - t1, 1)})
        log("  候选 %d/%d：谱形相似 %.4f · %.2fs · 生成 %.1fs"
            % (k + 1, CANDS, sc, len(arr) / sample_rate, time.time() - t1))

    ok = [c for c in cands if c["spec_cos"] == c["spec_cos"]]
    best = max(ok, key=lambda c: c["spec_cos"]) if ok else cands[0]
    log("  选中候选 %d（谱形相似 %.4f）" % (best["index"], best["spec_cos"]))

    # 选中的那条直接从内存取回 —— 重跑一次虽然同种子本该同波形，但那要靠
    # 「同设备同后端同版本」三个条件同时成立，多跑一次就多一个变量。
    arr = arrs[int(best["index"])]
    _write_wav(base_path, arr, sample_rate)

    # ICL = 同一条嗓子的两段，隔 0.15s。两半同源、同率、同一个后端 —— 不会再出现
    # 「一段是录的、一段是生的」这种拼法（那等于给模型看两个人的声音）。
    gap = np.zeros(int(round(GAP_SECONDS * sample_rate)), dtype=np.float32)
    icl = np.concatenate([arr, gap, arr]).astype(np.float32)
    _write_wav(icl_wav, icl, sample_rate)
    with io.open(icl_txt, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n" + text + "\n")

    rec = {"schema": 2, "role": role, "sign": sign,
           "ref_sha16": mv.digest_of(ref_wav)[:16],
           "source_ref": os.path.basename(ref_wav),
           "gap_seconds": GAP_SECONDS, "sample_rate": int(sample_rate),
           "source_sample_rate": int(sr_ref),
           "ref_seconds": round(len(ref) / sr_ref, 3),
           "base_seconds": round(len(arr) / sample_rate, 3),
           "icl_seconds": round(len(icl) / sample_rate, 3),
           "icl_layout": "ref_base + %.2fs + ref_base" % GAP_SECONDS,
           "cands": cands, "picked": best,
           "device": str(eng.device or ""), "backend": eng.backend,
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = os.path.join(rd, record_name(sign))
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)

    # 记账 + 切 active：文件三件落齐才写指向，读者 fail-closed 不会踩空。
    led["entries"][sign] = {
        "ref_sha16": rec["ref_sha16"], "created": rec["created"],
        "ref_base": os.path.basename(base_path),
        "icl_wav": os.path.basename(icl_wav),
        "icl_txt": os.path.basename(icl_txt),
        "record": os.path.basename(p),
        "rerolled": bool(force),
    }
    led["active"] = sign
    mv.write_base_refs(root, role, led)

    log("  → %s（%.2fs @%dHz） · %s（%.2fs，含 %.2fs 静音）"
        % (os.path.basename(base_path), len(arr) / sample_rate, sample_rate,
           os.path.basename(icl_wav), len(icl) / sample_rate, GAP_SECONDS))
    return {"role": role, "skipped": False, "sign": sign,
            "base_ref": base_path, "icl_wav": icl_wav, "icl_txt": icl_txt,
            "record": rec}


# ----------------------------------------------------------------- 入口
def _needs_build(root: str, role: str, force: bool) -> bool:
    """该角色是否要进生成队列：账本无命中（或 --force）才需要。"""
    rd = mv.role_dir(root, role)
    ref_wav = os.path.join(rd, mv.WAV_NAME)
    if not (os.path.isfile(ref_wav) and os.path.isfile(os.path.join(rd, mv.TXT_NAME))):
        return False
    if force:
        return True
    entry = mv.base_ref_entry_for(root, role, log=lambda m: None)
    if not entry:
        return True
    sign = mv.digest_of(ref_wav)[:8]
    return not all(os.path.isfile(os.path.join(rd, f))
                   for f in (baseref_name(sign), icl_wav_name(sign),
                             icl_txt_name(sign)))


def build(project_dir: str, roles=ROLES, voice_dir_name: str = "音色",
          force: bool = False, device: str = "auto", log=print,
          sample_rate=None) -> dict:
    """为项目里的角色备齐当前 ref 版本的 ref_base/icl。采样率默认取自统一推动点。"""
    sr_t = resolve_sample_rate(sample_rate)
    root = os.path.join(project_dir, voice_dir_name)
    for r in roles:
        if not mv.ref_paths(root, r):
            raise RuntimeError("%s 角还没有音色档案，先认领再补 ref_base。" % r)
    out: dict = {"voice_dir": root, "sample_rate": sr_t, "roles": {}}
    todo = [r for r in roles if _needs_build(root, r, force)]
    for r in roles:
        if r not in todo:
            sign = mv.digest_of(os.path.join(mv.role_dir(root, r),
                                             mv.WAV_NAME))[:8]
            out["roles"][r] = {"role": r, "skipped": True, "hit": True,
                               "sign": sign}
    if not todo:
        log("ref_base/icl 都已就绪（账本命中，不重做）。")
        return out

    log("统一采样率 %dHz（来自 audio.sample_rate）" % sr_t)
    resolved = serve.resolve_model(serve.DEFAULT_MODEL)
    if serve.model_kind(resolved) != "base":
        raise RuntimeError("ref_base 要用 Base 变体生成，当前解析到 %s（%s）。"
                           % (resolved, serve.model_kind(resolved)))
    log("加载 %s —— 与出片同一份 Base，跑完就退" % os.path.basename(resolved))
    eng = serve.Engine(resolved, device, log)
    t0 = time.time()
    eng.ensure()
    log("  后端 %s · 设备 %s · 加载 %.1fs" % (eng.backend, eng.device, time.time() - t0))

    for r in todo:
        out["roles"][r] = build_role(eng, root, r, force, log, sr_t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成 ref_base.wav 与 icl.wav（角色参考音频的 ICL 前缀）。")
    ap.add_argument("--project-dir", required=True, help="项目目录")
    ap.add_argument("--voice-dir", default="音色", help="音色子目录名")
    ap.add_argument("--role", default="both", choices=("A", "B", "both"),
                    help="只做某个角色，默认两个都做")
    ap.add_argument("--force", action="store_true", help="已有也重做")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--sample-rate", type=int, default=None,
                    help="覆盖统一采样率（默认取 audio.sample_rate，通常不必给）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    roles = ROLES if args.role == "both" else (args.role,)
    log = (lambda m: None) if args.json else (lambda m: print(m, flush=True))
    try:
        out = build(args.project_dir, roles, args.voice_dir,
                    args.force, args.device, log,
                    sample_rate=args.sample_rate)
    except Exception as e:  # noqa: BLE001
        if args.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            print("[失败] %s" % e, flush=True)
        return 1
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print("完成。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
