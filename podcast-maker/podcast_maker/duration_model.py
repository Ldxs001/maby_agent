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

"""时长模型 —— 字数 ↔ 时长 ↔ 语速 的唯一换算入口。

依据《我思故我写》13.3：任何模块都不得自行写换算系数，全部经由本模块。
原项目把"每秒 4 个汉字"写进提示词，实测系统性偏高 18–27%；
本模块把系数从提示词里拿出来，放进可观测、可校准、可回填的模型。

两级模型：
    L1 单参数（默认）      t = eff / (k · speed)
    L2 三参数最小二乘       t = (a·汉字 + b·标点 + c) / speed

标准语速（STANDARD_K）：脚本阶段唯一的一把尺子，与音色无关——稿子的时长目标
不该随「这期换了哪个音色」变。音色之间快慢的差异，由校准实测换算成一个比例
（`ratio_of`）挂在配置页的音色旁边，看得见、可追溯。

校准闭环：合成得到实测时长后，逐句配对拟合，EMA 平滑写回，
按 (引擎, 音色, 语速) 分组存储，不同音色互不污染。
"""

import json
import os
import re
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALIB_PATH = os.path.join(ROOT, "calibration.json")

# ---------------------------------------------------------------- 标准语速
# 脚本侧唯一的一把尺子：估时只认它，跟用哪个音色无关。
#
# 锚点（外部标准 + 本机实测，都核过）：
#   国家语委《普通话水平测试实施纲要》正常语速 240 音节/分钟（区间 150–300）
#   朗读/播音口径 250–270 字/分钟；日常交流 150–300 音节/分钟
#   本机 391 句实测（TTS 产物）：8541 汉字 / 2005.0 秒 = 256 汉字/分钟
# 取 256 字/分钟 —— 落在朗读口径中位，与实测一致；离国家标准的 +7%。
#
# 汉字与「有效字」的换算：同一批实测里 8541 汉字配 428 标点、23 西文词，
# 有效字 = 汉字 + 0.5×标点 + 1.5×西文词 = 8789.5，即 1 个汉字折 1.029 个有效字。
# 所以 256÷60×1.029 = 4.39 有效字/秒。改这一个数，全项目的脚本估时同步变。
STANDARD_CPM = 256.0        # 汉字/分钟
EFF_PER_HANZI = 1.029       # 有效字 ÷ 汉字（同一个批实测的口径折算）
STANDARD_K = round(STANDARD_CPM / 60.0 * EFF_PER_HANZI, 2)   # ≈4.39 有效字/秒

# 旧名保留：校准表没有该音色样本时的兜底值，与标准语速同源——全局只有一把尺子。
DEFAULT_K = STANDARD_K

# L2 启用门槛：样本达到该数量后才拟合三参数
L2_MIN_SAMPLES = 20

# EMA 平滑系数
EMA_ALPHA = 0.3

_HANZI_RE = re.compile(r"[\u4e00-\u9fff]")
_PUNCT_RE = re.compile(r"[，。！？；：、（）《》【】“”‘’…—·,.!?;:()\[\]\"']")
_LATIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.]*")


# --------------------------------------------------------------------- 计数
def count_hanzi(text):
    return len(_HANZI_RE.findall(text or ""))


def count_punct(text):
    return len(_PUNCT_RE.findall(text or ""))


def count_latin_tokens(text):
    return len(_LATIN_RE.findall(text or ""))


def effective_chars(text):
    """有效字数：汉字 1.0 + 中文标点 0.5 + 西文词 1.5。

    标点计停顿——这是原模型完全忽略的一维。
    """
    return (count_hanzi(text) + 0.5 * count_punct(text)
            + 1.5 * count_latin_tokens(text))


# --------------------------------------------------------------------- 校准
def _atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def group_key(engine, voice, speed):
    return "%s|%s|%.2f" % (engine or "edge", voice or "-", float(speed or 1.0))


class Calibration:
    """校准系数存储。所有系数的读写都经过这里。"""

    def __init__(self, path=None):
        self.path = path or CALIB_PATH
        self.groups = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.groups = data.get("groups", {}) or {}
            except (json.JSONDecodeError, OSError):
                self.groups = {}
        return self.groups

    def save(self):
        _atomic_write_json(self.path, {"groups": self.groups,
                                       "updated": datetime.now().isoformat(timespec="seconds")})

    def get(self, engine, voice, speed):
        return self.groups.get(group_key(engine, voice, speed))

    def stats(self, engine, voice, speed):
        """返回该组的 (k, a, b, c, samples, mode)。

        本组没有校准时，借同引擎同语速下已有组的实测系数，而不是直接落回全局
        默认值。理由：同一个引擎的音色之间语速相近，而默认值与实测值差得不小
        （本机实测 4.01、默认 5.00，差 25%）。拿默认值去估，两个主持人就等于用
        两把尺子量同一份稿子——A 角按实测算、B 角按默认算，同一稿的总时长能估出
        14% 的差，而这条差最后是记在门禁账上的。
        跨引擎不借：云端与本地模型的语速是两码事。
        借来的组会带 borrowed 字段（值为原音色名），报告与界面据此能看出这不是
        本组实测——不能让「借来的数」冒充「量过的数」。
        """
        g = self.get(engine, voice, speed)
        if not g:
            return self._borrow(engine, speed) or {
                "k": DEFAULT_K, "a": None, "b": None, "c": None,
                "samples": 0, "mode": "L1", "fitted": False}
        return self._normalize(g)

    def _normalize(self, g, borrowed_from=None):
        out = {"k": float(g.get("k", DEFAULT_K)), "samples": int(g.get("n", 0)),
               "mode": g.get("mode", "L1"), "fitted": True,
               "a": g.get("a"), "b": g.get("b"), "c": g.get("c")}
        if out["mode"] == "L2" and None in (out["a"], out["b"], out["c"]):
            out["mode"] = "L1"
        if borrowed_from is not None:
            out["borrowed"] = borrowed_from
        return out

    def _borrow(self, engine, speed):
        """在同引擎、同语速下借一个已标定的组。找不到返回 None。"""
        if not engine:
            return None
        want = "%.2f" % float(speed or 1.0)
        for key, g in sorted(self.groups.items()):
            parts = key.split("|")
            if len(parts) == 3 and parts[0] == engine and parts[2] == want:
                return self._normalize(g, borrowed_from=parts[1])
        return None

    def summary(self):
        """供界面显性展示：每一组当前的语速实测值。"""
        rows = []
        for key, g in sorted(self.groups.items()):
            parts = key.split("|")
            rows.append({
                "engine": parts[0] if len(parts) > 0 else "",
                "voice": parts[1] if len(parts) > 1 else "",
                "speed": float(parts[2]) if len(parts) > 2 else 1.0,
                "k": round(float(g.get("k", DEFAULT_K)), 3),
                "samples": int(g.get("n", 0)),
                "mode": g.get("mode", "L1"),
                "updated": g.get("updated", ""),
            })
        return rows


# ----------------------------------------------------------------- 标准尺子
def standard_stats():
    """标准语速的 stats，形状与 `Calibration.stats()` 一致，估算函数可以直接取。

    脚本阶段一律用它：目标字数反推、逐句估时、总时长门禁，全项目同一把尺子。
    音色之间的快慢差异不在估算里体现——那是「配置 → 声音」里每个音色的比例。
    """
    return {"k": STANDARD_K, "a": None, "b": None, "c": None,
            "samples": 0, "mode": "L1", "fitted": False, "standard": True}


def standard_rate():
    """标准语速的两个单位，供界面显性展示。"""
    return {"k": STANDARD_K, "cpm": round(STANDARD_CPM), "per_hanzi": EFF_PER_HANZI}


def ratio_of(calib, engine, voice, speed=1.0):
    """音色语速 ÷ 标准语速。没有该音色的实测样本时返回 None。

    只认该音色自己量出来的 k：借别人的数顶替，等于把「量过的」伪装成「没量过的」
    反过来——这个比例的全部价值就在于它是这一个音色的现场值。
    比例小于 1 = 比标准慢（成品会比脚本估时长），大于 1 = 比标准快。
    """
    if calib is None:
        return None
    g = calib.get(engine, voice, speed)
    if not g:
        return None
    try:
        k = float(g.get("k") or 0)
    except (TypeError, ValueError):
        return None
    return round(k / STANDARD_K, 3) if k > 0 else None


# --------------------------------------------------------------------- 估算
def estimate_seconds(text, speed=1.0, stats=None):
    """单句估时（秒）。stats 来自 Calibration.stats()。"""
    stats = stats or {"k": DEFAULT_K, "mode": "L1", "fitted": False}
    h = count_hanzi(text)
    p = count_punct(text)
    lt = count_latin_tokens(text)
    speed = float(speed or 1.0)
    if speed <= 0:
        speed = 1.0

    if stats.get("mode") == "L2" and None not in (stats.get("a"), stats.get("b"), stats.get("c")):
        t = (stats["a"] * h + stats["b"] * p + stats["c"] * lt)
    else:
        eff = h + 0.5 * p + 1.5 * lt
        k = float(stats.get("k") or DEFAULT_K)
        t = eff / k if k > 0 else 0.0
    return t / speed


def speaker_ctx(cfg, speaker):
    """取某说话人对应的 (engine, voice, speed)。

    这里的 `voice` 只是**校准表的键**，不等于「这一期的实际音色」。本地引擎走
    Base 变体时实际音色来自项目里那份参考音频，而不是这个字符串。之所以不按
    参考音频的内容指纹换键：时长模型吃的是语速与停顿的统计分布，换一段参考音频
    对它的影响远小于音色差异本身，而换键会让全部历史校准数据作废 —— 得不偿失。
    """
    is_a = (speaker or "A").upper() == "A"
    engine = cfg.get("tts.engine", "edge")
    if engine == "qwen3tts":        # 本地引擎的音色另存一套键（见 tts_engine.LOCAL_ENGINES）
        voice = cfg.get("tts.qwen3tts_voice_a" if is_a else "tts.qwen3tts_voice_b", "")
    else:
        voice = cfg.get("tts.voice_a" if is_a else "tts.voice_b", "")
    speed = cfg.get("tts.speed_a" if is_a else "tts.speed_b", 1.0)
    return engine, voice, speed


def estimate_line(text, speaker, cfg, calib=None):
    """单句估时。默认按**标准语速**估；传 calib 才改用该说话人音色的实测系数。

    脚本阶段一律不传 calib：稿子的时长目标不该随「这期换了哪个音色」变。
    """
    engine, voice, speed = speaker_ctx(cfg, speaker)
    st = standard_stats() if calib is None else calib.stats(engine, voice, speed)
    return estimate_seconds(text, speed, st)


def estimate_total(script, cfg, calib=None, include_pauses=True):
    """全篇估时（秒）。默认标准语速，口径同 `estimate_line`。"""
    total = 0.0
    for item in script or []:
        total += estimate_line(item.get("text", ""), item.get("speaker", "A"), cfg, calib)
    if include_pauses and script:
        total += float(cfg.get("audio.pause_between_lines", 0.35)) * max(0, len(script) - 1)
    return total


def chars_for_target(target_seconds, speed=1.0, stats=None):
    """由目标时长反推目标有效字数。"""
    stats = stats or {"k": DEFAULT_K, "mode": "L1"}
    speed = float(speed or 1.0)
    if stats.get("mode") == "L2" and stats.get("a"):
        return max(0.0, (target_seconds * speed - (stats.get("c") or 0.0)) / stats["a"])
    k = float(stats.get("k") or DEFAULT_K)
    return max(0.0, target_seconds * speed * k)


# --------------------------------------------------------------------- 拟合
def _solve3(A, b):
    """3x3 线性方程组高斯消元（带部分选主元）。无解返回 None。"""
    m = [row[:] + [b[i]] for i, row in enumerate(A)]
    n = 3
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / pv
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_l1(pairs):
    """pairs: [{"hanzi","punct","latin","seconds","speed"}] → k"""
    num = den = 0.0
    for p in pairs:
        eff = (p["hanzi"] + 0.5 * p["punct"] + 1.5 * p.get("latin", 0))
        t_norm = p["seconds"] * float(p.get("speed") or 1.0)
        num += eff
        den += t_norm
    if den <= 0:
        return None
    k = num / den
    return k if k > 0 else None


def fit_l2(pairs):
    """三参数最小二乘：t_norm = a·hanzi + b·punct + c·latin"""
    sh = sp = sl = shh = spp = sll = shp = shl = spl = 0.0
    th = tp = tl = tt = 0.0
    for p in pairs:
        h, pp, lt = p["hanzi"], p["punct"], p.get("latin", 0)
        t = p["seconds"] * float(p.get("speed") or 1.0)
        sh += h; sp += pp; sl += lt
        shh += h * h; spp += pp * pp; sll += lt * lt
        shp += h * pp; shl += h * lt; spl += pp * lt
        th += h * t; tp += pp * t; tl += lt * t
        tt += t
    A = [[shh, shp, shl], [shp, spp, spl], [shl, spl, sll]]
    b = [th, tp, tl]
    sol = _solve3(A, b)
    if sol is None:
        return None
    a, bb, c = sol
    if not (0.02 <= a <= 1.0):
        return None
    return {"a": a, "b": bb, "c": c}


def calibrate(calib, engine, voice, speed, lines):
    """用实测结果更新校准系数。

    lines: [{"text":..., "seconds":...}]，seconds 为实测音频时长。
    返回更新后的 stats（供界面显性展示）。
    """
    pairs = []
    for ln in lines or []:
        sec = float(ln.get("seconds") or 0)
        txt = ln.get("text") or ""
        if sec <= 0 or not txt.strip():
            continue
        pairs.append({
            "hanzi": count_hanzi(txt), "punct": count_punct(txt),
            "latin": count_latin_tokens(txt),
            "seconds": sec, "speed": float(speed or 1.0),
        })
    if len(pairs) < 3:
        return calib.stats(engine, voice, speed)

    key = group_key(engine, voice, speed)
    old = calib.groups.get(key) or {}
    old_n = int(old.get("n", 0))

    k_new = fit_l1(pairs)
    if k_new is None:
        return calib.stats(engine, voice, speed)

    # EMA：新样本与历史系数平滑，避免单集异常把系数带偏
    k_old = float(old.get("k", DEFAULT_K))
    k = EMA_ALPHA * k_new + (1 - EMA_ALPHA) * k_old if old_n > 0 else k_new

    entry = {"k": round(k, 4), "n": old_n + len(pairs), "mode": "L1",
             "updated": datetime.now().isoformat(timespec="seconds")}

    sample_count = old_n + len(pairs)
    if sample_count >= L2_MIN_SAMPLES:
        # 累积历史样本与本次样本一起拟合（历史只保留聚合量，不存原始句）
        merged = _merge_history(old, pairs)
        l2 = fit_l2(merged) if merged else None
        if l2:
            entry.update({"mode": "L2", "a": round(l2["a"], 5),
                          "b": round(l2["b"], 5), "c": round(l2["c"], 5)})

    calib.groups[key] = entry
    calib.save()
    return calib.stats(engine, voice, speed)


def _merge_history(old, pairs):
    """历史样本以聚合量存留：用近似重建的样本点凑够拟合规模。"""
    n_old = int(old.get("n", 0))
    if n_old <= 0:
        return pairs
    k_old = float(old.get("k", DEFAULT_K))
    # 用历史 k 反推一条"平均句"作为代表样本，权重按历史样本数
    a = old.get("a")
    if a and old.get("mode") == "L2":
        merged = []
        for _ in range(min(n_old, 200)):
            merged.append({"hanzi": 25.0, "punct": 2.0, "latin": 0.0,
                           "seconds": a * 25.0 + (old.get("b") or 0) * 2.0 + (old.get("c") or 0),
                           "speed": 1.0})
        return merged + pairs
    if k_old <= 0:
        return pairs
    merged = []
    for _ in range(min(n_old, 200)):
        merged.append({"hanzi": 25.0, "punct": 2.0, "latin": 0.0,
                       "seconds": 25.0 / k_old, "speed": 1.0})
    return merged + pairs


# --------------------------------------------------------------------- 展示
def explain(script, cfg, calib=None):
    """逐句明细，供界面显性展示（需求三的"字/时间"）。

    默认按标准语速估——脚本页看到的秒数与音色无关，全篇一把尺子。
    传 calib 才按各说话人音色的实测系数估（出片侧要拿音色算数时才用）。
    """
    rows = []
    total = 0.0
    for i, item in enumerate(script or []):
        text = item.get("text", "")
        sp = item.get("speaker", "A")
        engine, voice, speed = speaker_ctx(cfg, sp)
        st = standard_stats() if calib is None else calib.stats(engine, voice, speed)
        sec = estimate_seconds(text, speed, st)
        total += sec
        rows.append({
            "index": i + 1,
            "speaker": sp,
            "text": text,
            "chars": len(text),
            "hanzi": count_hanzi(text),
            "punct": count_punct(text),
            "effective": round(effective_chars(text), 2),
            "speed": speed,
            "k": round(float(st.get("k") or STANDARD_K), 3),
            "rate_source": "standard" if calib is None else "voice",
            "estimated_seconds": round(sec, 2),
            "actual_seconds": item.get("actual_seconds"),
            "emotion": item.get("emotion", ""),
        })
    pause = float(cfg.get("audio.pause_between_lines", 0.35))
    total_with_pause = total + pause * max(0, len(rows) - 1)
    target = float(cfg.get("script.target_minutes", 4.0)) * 60.0
    dev = (total_with_pause - target) / target * 100 if target > 0 else 0.0
    return {
        "rows": rows,
        "speech_seconds": round(total, 2),
        "total_seconds": round(total_with_pause, 2),
        "target_seconds": round(target, 2),
        "deviation_pct": round(dev, 2),
        "line_count": len(rows),
        "total_chars": sum(r["chars"] for r in rows),
        "total_effective": round(sum(r["effective"] for r in rows), 2),
        "speech_cps": round(sum(r["effective"] for r in rows) / total, 3) if total > 0 else 0.0,
        "rate_source": "standard" if calib is None else "voice",
        "standard_k": STANDARD_K,
    }


def target_hint(cfg, calib=None, speeds=(1.0,)):
    """事前反推：给定目标时长与若干语速，给出目标有效字数。默认标准语速。"""
    target = float(cfg.get("script.target_minutes", 4.0)) * 60.0
    engine = cfg.get("tts.engine", "edge")
    voice = cfg.get("tts.voice_a", "")
    out = []
    for s in speeds:
        st = standard_stats() if calib is None else calib.stats(engine, voice, s)
        eff = chars_for_target(target, s, st)
        out.append({"speed": s, "effective_chars": round(eff),
                    "k": round(float(st.get("k") or STANDARD_K), 3),
                    "mode": st.get("mode", "L1"),
                    "samples": st.get("samples", 0)})
    return {"target_seconds": round(target, 2),
            "target_minutes": round(target / 60.0, 2), "options": out,
            "standard": standard_rate()}
