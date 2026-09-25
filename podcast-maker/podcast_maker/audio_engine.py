#!/usr/bin/env python3
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

"""音频引擎：编码参数校验 + 拼接 + 降噪 + 响度归一 + 背景音乐（含人声闪避）。

编码参数的物理约束校验是本模块的唯一入口。原项目配置 192 kbps、采样率 22050 Hz，
被编码器静默钳制到 160 kbps——本模块把这种组合拦在启动之前，报错而不改数。
"""

import os
import shutil
import subprocess

from . import bins
from .config_manager import bitrate_ceiling


class AudioError(RuntimeError):
    """音频处理失败。绝不静默降级。"""


CODEC_MAP = {"mp3": "libmp3lame", "aac": "aac"}


# ------------------------------------------------------------------ 前置校验
def validate_params(cfg):
    """编码参数可行性校验（唯一入口）。返回 (warns, errors)。"""
    warns, errs = [], []
    sr = int(cfg.get("audio.sample_rate", 44100))
    br = int(cfg.get("audio.bitrate_kbps", 192))
    codec = cfg.get("audio.codec", "mp3")
    ceil = bitrate_ceiling(sr, codec)
    if br > ceil:
        errs.append(
            "%s 在 %d Hz 下的码率上限为 %d kbps，当前配置 %d kbps 不可达；"
            "编码器会静默钳制。请调整采样率或码率。" % (codec.upper(), sr, ceil, br)
        )
    return warns, errs


def ffmpeg_bin():
    exe = bins.locate("ffmpeg")
    if not exe:
        raise AudioError(bins.missing_message("ffmpeg"))
    return exe


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise AudioError("ffmpeg 失败：%s"
                         % (r.stderr or b"").decode("utf-8", "replace")[-500:])
    return r


# ------------------------------------------------------------------ 探测
def probe_audio(path):
    exe = bins.locate("ffprobe")
    if not exe:
        raise AudioError(bins.missing_message("ffprobe"))
    r = subprocess.run(
        [exe, "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels,bit_rate:format=duration,bit_rate",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True)
    info = {}
    for line in (r.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        info.setdefault(k.strip(), v.strip())
    out = {"codec": info.get("codec_name", ""),
           "sample_rate": int(info.get("sample_rate") or 0),
           "channels": int(info.get("channels") or 0),
           "duration": float(info.get("duration") or 0)}
    br = info.get("bit_rate") or info.get("bit_rate")
    try:
        out["bit_rate_kbps"] = int(int(br) / 1000) if br else 0
    except ValueError:
        out["bit_rate_kbps"] = 0
    # 容器级 bit_rate 优先（流级在 VBR 下可能缺失）
    if not out["bit_rate_kbps"]:
        r2 = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=bit_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True)
        try:
            out["bit_rate_kbps"] = int(int((r2.stdout or "").strip()) / 1000)
        except ValueError:
            pass
    return out


# ------------------------------------------------------------------ 处理
def concat(parts, out_path, pause, sample_rate, channels):
    """按顺序拼接，并在片段之间插入静音停顿。"""
    seq = []
    silence = None
    if pause and pause > 0 and len(parts) > 1:
        silence = os.path.join(os.path.dirname(out_path), "_silence.wav")
        _run([ffmpeg_bin(), "-y", "-f", "lavfi",
              "-i", "anullsrc=r=%d:cl=%s" % (sample_rate, "mono" if channels == 1 else "stereo"),
              "-t", "%.3f" % float(pause), silence])
    for i, p in enumerate(parts):
        seq.append(p)
        if silence and i < len(parts) - 1:
            seq.append(silence)

    list_file = os.path.join(os.path.dirname(out_path), "_concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in seq:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/"))
    try:
        _run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0",
              "-i", list_file, "-ar", str(sample_rate),
              "-ac", str(channels), out_path])
    finally:
        for p in (list_file, silence):
            if p and os.path.exists(p):
                os.remove(p)
    return out_path


def denoise(in_path, out_path):
    _run([ffmpeg_bin(), "-y", "-i", in_path, "-af", "afftdn=nr=12", out_path])
    return out_path


def loudnorm(in_path, out_path, target_lufs, sample_rate, channels):
    _run([ffmpeg_bin(), "-y", "-i", in_path,
          "-af", "loudnorm=I=%.1f:TP=-1.5:LRA=11" % float(target_lufs),
          "-ar", str(sample_rate), "-ac", str(channels), out_path])
    return out_path


def add_intro_outro(body, out_path, cfg, sample_rate, channels):
    """拼接声明 / 片头 / 正文 / 片尾。返回 (路径, 片头秒数, 片尾秒数)。

    AI 语音声明（``_ai_decl_src``，编排层合成好放进来）固定排在最前——
    《标识办法》要求显式语音提示落在音频的起始、末尾或中间适当位置，取
    起始：听众在形成「真人在说话」的印象之前就该被告知。声明时长**并入
    片头秒数**一起返回，字幕时间轴拿这个偏移整体后移即可，无需单独知道
    声明的存在——偏移口径分家正是字幕错位的成因，这里从源头堵死。

    片头尾时长必须向上返回：字幕时间轴依赖它对齐。
    把时长藏在这一层里，等于让字幕整条错位，且外部无从察觉。
    """
    decl = cfg.get("_ai_decl_src") or ""
    decl = decl if decl and os.path.exists(decl) else ""
    decl_sec = probe_duration_safe(decl) if decl else 0.0
    intro = cfg.get("audio.intro_path") or ""
    outro = cfg.get("audio.outro_path") or ""
    intro = intro if intro and os.path.exists(intro) else ""
    outro = outro if outro and os.path.exists(outro) else ""
    intro_sec = probe_duration_safe(intro) if intro else 0.0
    outro_sec = probe_duration_safe(outro) if outro else 0.0

    parts = (([decl] if decl else []) + ([intro] if intro else [])
             + [body] + ([outro] if outro else []))
    if len(parts) == 1:
        shutil.copy2(body, out_path)
    else:
        concat(parts, out_path, 0.0, sample_rate, channels)
    if decl_sec:
        intro_sec = decl_sec + intro_sec
    return out_path, intro_sec, outro_sec


def mix_bgm(voice_path, out_path, bgm_path, cfg):
    """叠加背景音乐。人声闪避开启时，人声起则音乐自动压低。"""
    volume = float(cfg.get("bgm.volume", 0.50))
    fade = float(cfg.get("bgm.fade_seconds", 3.0))
    duck = bool(cfg.get("bgm.ducking", True))
    thr = float(cfg.get("bgm.duck_threshold", 0.25))
    ratio = float(cfg.get("bgm.duck_ratio", 2.0))
    sr = int(cfg.get("audio.sample_rate", 44100))
    ch = int(cfg.get("audio.channels", 1))

    fade_in = "afade=t=in:st=0:d=%.2f" % fade if fade > 0 else "anull"
    chain = [
        "[1:a]volume=%.3f,%s[bg]" % (volume, fade_in),
    ]
    if duck:
        chain.append("[0:a]asplit=2[v1][v2]")
        chain.append("[bg][v2]sidechaincompress=threshold=%.4f:ratio=%.1f:"
                     "attack=20:release=400:makeup=1[duck]" % (thr, ratio))
        chain.append("[v1][duck]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        chain.append("[0:a][bg]amix=inputs=2:duration=first:normalize=0[aout]")

    _run([ffmpeg_bin(), "-y", "-i", voice_path,
          "-stream_loop", "-1", "-i", bgm_path,
          "-filter_complex", ";".join(chain),
          "-map", "[aout]", "-ar", str(sr), "-ac", str(ch),
          out_path])
    return out_path


def to_encoded(wav_path, out_path, cfg):
    """转成目标编码格式。码率经可行性校验，不做静默钳制。"""
    codec = cfg.get("audio.codec", "mp3")
    br = int(cfg.get("audio.bitrate_kbps", 192))
    sr = int(cfg.get("audio.sample_rate", 44100))
    ceil = bitrate_ceiling(sr, codec)
    if br > ceil:
        raise AudioError("码率 %d kbps 在 %d Hz 下不可达（上限 %d kbps）。" % (br, sr, ceil))
    _run([ffmpeg_bin(), "-y", "-i", wav_path,
          "-codec:a", CODEC_MAP.get(codec, "libmp3lame"),
          "-b:a", "%dk" % br, "-ar", str(sr), out_path])
    return out_path


def resolve_bgm_source(cfg, out_dir):
    """BGM 来源解析的唯一入口。none 或不可用返回空串。

    builtin：编排层把合成好的 bgm.wav 绝对路径写入 cfg["_bgm_src"]；
             未写时回落到 out_dir/bgm.wav（同目录约定）
    custom ：用户指定的外部音频文件
    """
    mode = cfg.get("bgm.mode", "builtin")
    if mode == "none":
        return ""
    if mode == "custom":
        p = cfg.get("bgm.custom_path") or ""
    else:
        p = cfg.get("_bgm_src") or os.path.join(out_dir, "bgm.wav")
    return p if p and os.path.exists(p) else ""


def process(audio_files, out_dir, cfg, pause=0.35, log=None):
    """完整音频后处理链：拼接 → 降噪 → 片头尾 → 响度 → BGM → 编码。

    BGM 混音只在这里做一次；编排层不得重复混音。
    """
    log = log or (lambda m: None)
    sr = int(cfg.get("audio.sample_rate", 44100))
    ch = int(cfg.get("audio.channels", 1))

    cur = os.path.join(out_dir, "_merged.wav")
    concat(audio_files, cur, pause, sr, ch)
    log("音频拼接完成")

    if cfg.get("audio.denoise", True):
        nxt = os.path.join(out_dir, "_denoised.wav")
        denoise(cur, nxt)
        cur = nxt
        log("降噪完成")

    nxt = os.path.join(out_dir, "_with_io.wav")
    cur, intro_sec, outro_sec = add_intro_outro(cur, nxt, cfg, sr, ch)
    if intro_sec or outro_sec:
        decl_sec = probe_duration_safe(cfg.get("_ai_decl_src") or "")
        log("片头 %.2f 秒 / 片尾 %.2f 秒已拼接%s"
            % (intro_sec, outro_sec,
               "（含 AI 声明 %.2f 秒）" % decl_sec if decl_sec > 0 else ""))

    final = os.path.join(out_dir, "voice.wav")
    loudnorm(cur, final, cfg.get("audio.loudnorm_target", -14), sr, ch)
    log("响度归一完成（%.1f LUFS）" % float(cfg.get("audio.loudnorm_target", -14)))

    mixed = None
    bgm = resolve_bgm_source(cfg, out_dir)
    if bgm:
        mixed = os.path.join(out_dir, "voice_bgm.wav")
        mix_bgm(final, mixed, bgm, cfg)
        log("背景音乐已叠加（%s，来源 %s）"
            % ("人声闪避" if cfg.get("bgm.ducking") else "恒定音量", os.path.basename(bgm)))
    elif cfg.get("bgm.mode", "builtin") != "none":
        log("背景音乐来源不可用，已跳过（%s）" % cfg.get("bgm.mode"))

    for tmp in os.listdir(out_dir):
        if tmp.startswith("_") and tmp.endswith(".wav"):
            try:
                os.remove(os.path.join(out_dir, tmp))
            except OSError:
                pass

    return {"final_audio": mixed or final, "voice_audio": final,
            "duration_seconds": probe_duration_safe(mixed or final),
            "intro_seconds": round(intro_sec, 3),
            "outro_seconds": round(outro_sec, 3),
            "declaration_seconds": round(
                probe_duration_safe(cfg.get("_ai_decl_src") or ""), 3)}


def probe_duration_safe(path):
    try:
        return round(probe_audio(path).get("duration", 0.0), 3)
    except AudioError:
        return 0.0
