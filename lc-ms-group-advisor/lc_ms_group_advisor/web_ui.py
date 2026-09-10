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

"""Web UI — LC-MS 分组顾问

三个页面（Tab），共用一个四轴引擎（T_cycle ≤ W_peak/N_min）：
  1. 分组预测 — 极性出峰顺序预测 + 分组 + 每组质谱核算
  2. 时间参数 — 四台设备时间参数体系（体现 massspec_time_emulator）
  3. 通量核算 — 离子对漏检/混峰核算 + 反推（体现 massspec_flux_calculator）
深色主题参考 silprespec-emulator / structured-writer。
"""
import json
import os
import threading
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from .config_manager import ConfigManager, BACKEND_DEFAULTS, default_param, param_spec
from .llm_client import LLMClient, LLMClientError
from .separator import run_separation, COLUMN_TYPES
from .ms_engine import evaluate_all, DEVICES
from . import extractor
from .mass_resolution import (
    check_list as res_check_list,
    judge as res_judge,
    resolve_k,
    resolve_fhsw_da,
    k_to_valley,
    preset_payload as res_preset_payload,
)

config_mgr = ConfigManager()


def _make_llm() -> LLMClient:
    backend = config_mgr.get("llm.backend", "lm-studio")
    base_url = config_mgr.get("llm.base_url", "")
    if not base_url:
        base_url = BACKEND_DEFAULTS.get(backend, "")
    return LLMClient(backend=backend, base_url=base_url,
                     model=config_mgr.get("llm.model", ""),
                     api_key=config_mgr.get("llm.api_key", "not-needed"),
                     temperature=config_mgr.get("llm.temperature", 0.3))


def _extract_compounds(text: str) -> dict:
    """锚点指针抽取：LLM 只给「原文引用 + 槽位」，值由 Python 从原文取（见 extractor）。

    返回 {"ok", "mode", "compounds", "failures", "missing", "warnings"}。
    ok=False 表示未通过闸门（引用未命中 / 缺化学式），此时 compounds 是半成品——
    调用方必须把缺口清单原样透出，不得静默使用。
    """
    return extractor.extract(text, _make_llm(), max_retries=1)


def _extract_error(result):
    """把 ok=False 的原因压成一句可读文案。"""
    parts = []
    if result.get("failures"):
        parts.append(f"{len(result['failures'])} 处引用未命中")
    if result.get("missing", {}).get("formula"):
        parts.append(f"{len(result['missing']['formula'])} 个化合物缺化学式")
    return "、".join(parts) or "抽取未通过校验"


def _res_params(p):
    """从请求参数抽出质量分辨率键（缺省 = 设备标称峰宽 + 10% 谷判据）。

    缺省值一律取 default_param()，不就地写字面量——字面量会造出配置推动链之外的
    第二个源头，值一旦分叉，界面上看不见也改不了。
    """
    return {
        "fhsw_mode": p.get("fhsw_mode", default_param("fhsw_mode")),
        "fhsw_da": p.get("fhsw_da"),
        "fhsw_ppm": p.get("fhsw_ppm"),
        "valley_preset": p.get("valley_preset", default_param("valley_preset")),
        "k_custom": p.get("k_custom"),
    }


def _res_summary(dev, p):
    """给界面回显的判据摘要（k / 谷值 / 峰宽来源），让判定可复核。"""
    params = _res_params(p)
    k = resolve_k(params)
    mode = params["fhsw_mode"]
    if mode == "constant":
        val = params.get("fhsw_da")
        unit = "Da（恒定）"
    elif mode == "proportional":
        val = params.get("fhsw_ppm")
        unit = "ppm（随 m/z 等比）"
    else:
        if dev.get("res_ppm"):
            val, unit = dev["res_ppm"], "ppm（设备标称，随 m/z 等比）"
        else:
            val, unit = dev.get("res_da"), "Da（设备标称，恒定）"
    return {
        "k": round(k, 4),
        "valley_pct": round(k_to_valley(k), 2),
        "valley_preset": params["valley_preset"],
        "fhsw_mode": mode,
        "fhsw_value": val,
        "fhsw_unit": unit,
    }


def _make_mass_conflict_fn(dev, p):
    """分组用的质量冲突判定：两母离子不可分辨时返回冲突 dict，否则 None。"""
    params = _res_params(p)

    def conflict(a_prec, b_prec):
        if not a_prec or not b_prec:
            return None
        a, b = float(a_prec), float(b_prec)
        j = res_judge(abs(a - b), (a + b) / 2.0, dev, params)
        return j if j["overlap"] else None

    return conflict


def _do_separate(compounds, params):
    """分组预测：分离 + 分组 + 质谱核算。

    参数兜底统一走 default_param()（配置推动链的唯一源头），不就地写字面量。
    """
    def param(key):
        v = params.get(key)
        return default_param(key) if v is None else v

    instrument = param("instrument")
    dev = DEVICES.get(instrument, DEVICES[default_param("instrument")])
    sep = run_separation(
        compounds,
        column_type=param("column_type"),
        group_mode=param("group_mode"),
        threshold_pct=float(param("threshold_pct")),
        max_span_pct=float(param("max_span_pct")),
        peak_width_s=float(param("peak_width_s")),
        n_min=int(param("n_min")),
        dwell_ms=float(param("dwell_ms")),
        delay_q1_ms=float(param("delay_q1_ms")),
        delay_q3_ms=float(param("delay_q3_ms")),
        overhead_ms=float(param("overhead_ms")),
        channels_per_compound=float(param("channels_per_compound")),
        conflict_fn=_make_mass_conflict_fn(dev, params),
    )
    ms = evaluate_all(
        sep["groups"],
        instrument=instrument,
        peak_width_s=float(param("peak_width_s")),
        dwell_ms=float(param("dwell_ms")),
        delay_q1_ms=float(param("delay_q1_ms")),
        delay_q3_ms=float(param("delay_q3_ms")),
        overhead_ms=float(param("overhead_ms")),
        channels_per_compound=float(param("channels_per_compound")),
        res_params=_res_params(params),
    )
    return {"separation": sep, "ms_eval": ms, "res": _res_summary(dev, params)}


# ======================================================================
# Tab 2 — 时间参数（四台设备四轴计算，体现 massspec_time_emulator）
# ======================================================================

# Tab 2 可选设备 = 质谱引擎的设备表，label 直接从 DEVICES 派生，
# 避免两处各写一份设备名（历史漂移点）。
TIME_DEVICES = {k: {"label": v["label"]} for k, v in DEVICES.items()}


def _time_sim_qqq(p):
    N1 = int(p.get("n_precursors", 20))
    N3 = float(p.get("n_products", 2))
    dwell = float(p.get("dwell_ms", 20))
    delay_q1 = float(p.get("delay_q1_ms", 2))
    delay_q3 = float(p.get("delay_q3_ms", 2))
    overhead = float(p.get("overhead_ms", 2))
    n_ch = N1 * N3                        # 总通道数（实采离子对数）
    n_q3_sw = N1 * (N3 - 1)               # 母离子内：仅 Q3 动，按母离子分别加总
    n_q1_sw = N1 - 1                      # 母离子间：Q1 与 Q3 同时动
    delay_switch = max(delay_q1, delay_q3)  # Q1/Q3 并行稳定，取较慢者
    t_cycle = (n_ch * dwell
               + n_q3_sw * delay_q3
               + n_q1_sw * delay_switch
               + overhead)
    duty = (n_ch * dwell) / t_cycle * 100 if t_cycle > 0 else 0
    formula = (f"T_cycle = {N1}×{N3:g}×{dwell} + {n_q3_sw:g}×{delay_q3}(Q3切) "
               f"+ {n_q1_sw}×{delay_switch}(Q1/Q3并行切) + {overhead} = {t_cycle:.1f} ms")
    segments = []
    shown = min(N1, 10)
    # N₃ 允许小数（多化合物通道数不同时填等效平均值）：计算走浮点，渲染段数只能取整——
    # 段宽按比例缩放，使「每化合物块总宽 = 该块真实耗时」，Σsegments 恒等于 T_cycle
    n3_draw = max(1, int(round(N3)))
    per_compound_ms = N3 * dwell + max(N3 - 1, 0) * delay_q3
    draw_dwell_ms = (per_compound_ms - (n3_draw - 1) * delay_q3) / n3_draw
    for ci in range(shown):
        for j in range(n3_draw):
            segments.append({"label": f"化合物{ci+1} 子{j+1}", "ms": draw_dwell_ms, "kind": "dwell"})
            if j < n3_draw - 1:
                segments.append({"label": "Q3切换", "ms": delay_q3, "kind": "gap_q3"})
        if ci < shown - 1:
            segments.append({"label": "Q1+Q3切", "ms": delay_switch, "kind": "gap_q1"})
    # 省略段按“剩余化合物的真实耗时”聚合计宽，使甘特图各段宽度恒等于耗时占比、合计占满 100%
    if N1 > shown:
        r = N1 - shown
        omit_ms = r * (per_compound_ms + delay_switch)
        segments.append({"label": f"省略 {r} 个化合物（同类重复块，合计 {omit_ms:.0f}ms）",
                         "ms": omit_ms, "kind": "omit", "n_compounds": r})
    segments.append({"label": "开销", "ms": overhead, "kind": "switch"})
    help_ = [
        {"name": "dwell 驻留时间", "what": "Q1 选母离子、Q3 同时选子离子，停在一个离子对上采集信号的时长",
         "effect": "调大→单点信号强、灵敏度高；但 cycle 同步变长→点数变少"},
        {"name": "Q3切换延迟 delay_q3", "what": "同一化合物内，Q3 从一个子离子切到另一个并稳定的时间（Q1 保持不动）",
         "effect": "不采集信号，纯开销；次数 = N₁×(N₃−1)——每个母离子内 N₃ 个通道之间有 N₃−1 次，按母离子分别加总"},
        {"name": "Q1切换延迟 delay_q1", "what": "Q1 从一个母离子切到另一个并稳定的时间；此时子离子也换，Q1/Q3 同时动作",
         "effect": "Q1/Q3 并行稳定，边界单次开销 = max(delay_q1, delay_q3)，不是两者相加（相加为保守高估）；次数 = N₁−1"},
        {"name": "母离子数 N₁", "what": "一个 cycle 里采集的不同母离子（化合物）总数",
         "effect": "N₁ 越大→总通道数与边界切换次数同步增大→cycle 越长"},
        {"name": "子离子通道数 N₃", "what": "每个母离子（化合物）对应的子离子通道数，即该化合物的离子对数",
         "effect": "总通道数 = N₁×N₃；N₃ 调大→总通道数按 N₁ 倍放大→dwell 段变长→点数减少"},
        {"name": "总通道数 N₁×N₃", "what": "一个 cycle 内实际采集的离子对（dwell 段）总数",
         "effect": "构成 cycle 的主体耗时；两类切换开销叠加在其上"},
    ]
    return t_cycle, duty, formula, segments, help_


def _time_sim_qtof(p):
    f_push = float(p.get("f_push_khz", 20))
    nsum = int(p.get("nsum", 1000))
    spectra_hz = f_push * 1000.0 / nsum if nsum > 0 else 0
    t_cycle = 1000.0 / spectra_hz if spectra_hz > 0 else 0
    duty = 100.0
    formula = (f"谱图率 = {f_push}kHz/{nsum} = {spectra_hz:.1f} Hz；"
               f"T_cycle = 1000/{spectra_hz:.1f} = {t_cycle:.1f} ms")
    segments = [{"label": f"推斥叠加×{nsum}（连续并行采集）", "ms": t_cycle, "kind": "dwell"}]
    help_ = [
        {"name": "推斥频率 f_push", "what": "TOF 每秒推斥离子进入飞行管的次数（10-50 kHz）",
         "effect": "单次推斥=一张原始 m/z 谱；决定原始采样密度"},
        {"name": "叠加次数 nsum", "what": "多少次推斥叠加成一张输出质谱图",
         "effect": "调大→单张谱信噪比好，但输出谱图率（=f_push/nsum）下降→点数变少"},
        {"name": "并行性", "what": "TOF 全质量范围同时采集，无序列扫描",
         "effect": "化合物数量不影响 cycle——这是与 QqQ 的本质区别"},
    ]
    return t_cycle, duty, formula, segments, help_


def _time_sim_it(p):
    dm = float(p.get("scan_range_da", 500))
    scan_rate = float(p.get("scan_rate_das", 20000))
    fill = float(p.get("fill_ms", 10))
    t_scan = dm / scan_rate * 1000.0
    t_cycle = t_scan + fill
    duty = fill / t_cycle * 100 if t_cycle > 0 else 0
    formula = (f"T_scan = {dm}Da/{scan_rate}Da/s = {t_scan:.1f} ms；"
               f"T_cycle = {t_scan:.1f}+{fill} = {t_cycle:.1f} ms")
    segments = [
        {"label": "离子填充 fill", "ms": fill, "kind": "dwell"},
        {"label": f"扫描 {dm}Da", "ms": t_scan, "kind": "gap"},
    ]
    help_ = [
        {"name": "扫描速率", "what": "离子阱逐出离子的速度（10,000-66,000 Da/s）",
         "effect": "调快→扫描段短→cycle 短→点数多；但分辨率下降"},
        {"name": "fill time", "what": "阱内积累离子的时间（AGC 自动调节 0.01ms~数百ms）",
         "effect": "信号主要在此积累；浓度高 AGC 自动缩短 fill 防空间电荷"},
        {"name": "扫描范围 Δm/z", "what": "一张谱覆盖的质量区间",
         "effect": "范围越宽扫描越慢→cycle 越长→点数越少"},
    ]
    return t_cycle, duty, formula, segments, help_


def _time_sim_qtrap(p):
    N1 = int(p.get("n_precursors", 20))
    N3 = float(p.get("n_products", 2))
    dwell = float(p.get("dwell_ms", 20))
    delay_q1 = float(p.get("delay_q1_ms", 2))
    delay_q3 = float(p.get("delay_q3_ms", 2))
    dm = float(p.get("scan_range_da", 500))
    scan_rate = float(p.get("scan_rate_das", 20000))
    fill = float(p.get("fill_ms", 10))
    t_switch = float(p.get("t_switch_ms", 30))
    n_ch = N1 * N3                          # 总通道数（实采离子对数）
    n_q3_sw = N1 * (N3 - 1)                 # 母离子内：仅 Q3 动，按母离子分别加总
    n_q1_sw = N1 - 1                        # 母离子间：Q1 与 Q3 同时动
    delay_switch = max(delay_q1, delay_q3)  # Q1/Q3 并行稳定，取较慢者
    t_mrm = n_ch * dwell + n_q3_sw * delay_q3 + n_q1_sw * delay_switch
    t_scan = dm / scan_rate * 1000.0
    t_trap = t_scan + fill
    t_cycle = t_mrm + t_switch + t_trap
    duty = (n_ch * dwell + fill) / t_cycle * 100 if t_cycle > 0 else 0
    formula = (f"T_cycle = MRM段 {N1}×{N3:g}×{dwell}+{n_q3_sw:g}×{delay_q3}+{n_q1_sw}×{delay_switch}={t_mrm:.1f}"
               f" + 模式切换 {t_switch} + 阱段 {t_trap:.1f} = {t_cycle:.1f} ms")
    segments = []
    shown = min(N1, 6)
    # 同 QqQ：N₃ 可为小数，渲染段数取整 + 段宽按比例，保证块总宽等于真实耗时
    n3_draw = max(1, int(round(N3)))
    per_compound_ms = N3 * dwell + max(N3 - 1, 0) * delay_q3
    draw_dwell_ms = (per_compound_ms - (n3_draw - 1) * delay_q3) / n3_draw
    for ci in range(shown):
        for j in range(n3_draw):
            segments.append({"label": f"MRM {ci+1}-{j+1}", "ms": draw_dwell_ms, "kind": "dwell"})
            if j < n3_draw - 1:
                segments.append({"label": "Q3切", "ms": delay_q3, "kind": "gap_q3"})
        if ci < shown - 1:
            segments.append({"label": "Q1+Q3切", "ms": delay_switch, "kind": "gap_q1"})
    # 省略段按“剩余化合物的真实耗时”聚合计宽（此前 qtrap 直接丢弃，导致甘特图只占 6/N₁ 宽度）
    if N1 > shown:
        r = N1 - shown
        omit_ms = r * (per_compound_ms + delay_switch)
        segments.append({"label": f"省略 {r} 个化合物（同类重复块，合计 {omit_ms:.0f}ms）",
                         "ms": omit_ms, "kind": "omit", "n_compounds": r})
    segments.append({"label": "模式切换", "ms": t_switch, "kind": "switch"})
    segments.append({"label": "阱填充", "ms": fill, "kind": "dwell"})
    segments.append({"label": f"阱扫描 {dm}Da", "ms": t_scan, "kind": "gap"})
    help_ = [
        {"name": "Q3 双角色", "what": "Q3 既是四极杆（MRM）又是离子阱（增强扫描）",
         "effect": "两种模式不能并行，一个 cycle 内分段执行"},
        {"name": "模式切换时间", "what": "Q3 从 MRM 模式切到阱模式的往返开销（数十 ms）",
         "effect": "每次切换纯开销；切换越频繁 cycle 越长"},
        {"name": "MRM段+阱段", "what": "定量靠 MRM 段，谱图/定性靠阱扫描段",
         "effect": "两段共享同一个 cycle 预算——加阱段会吃掉 MRM 的点数"},
        {"name": "母离子数 N₁ / 子离子通道数 N₃", "what": "N₁ = 化合物数；N₃ = 每个母离子对应的子离子通道数",
         "effect": "MRM 段 = N₁×N₃×dwell + N₁×(N₃−1)×Q3切 + (N₁−1)×max(delay_q1, delay_q3)"},
    ]
    return t_cycle, duty, formula, segments, help_


def _do_time_sim(p):
    device = p.get("device", default_param("instrument"))
    W = float(p.get("peak_width_s") or default_param("peak_width_s"))
    n_min = int(p.get("n_min") or default_param("n_min"))
    fn = {"qqq": _time_sim_qqq, "qtof": _time_sim_qtof,
          "it": _time_sim_it, "qtrap": _time_sim_qtrap}.get(device, _time_sim_qqq)
    t_cycle, duty, formula, segments, help_ = fn(p)
    n_points = W * 1000.0 / t_cycle if t_cycle > 0 else float("inf")
    ok = n_points >= n_min
    total_shown = sum(s["ms"] for s in segments)
    omitted = max(t_cycle - total_shown, 0)
    return {
        "device": device,
        "device_label": TIME_DEVICES.get(device, TIME_DEVICES["qqq"])["label"],
        "axes": {
            "t_cycle_ms": round(t_cycle, 1),
            "n_points": round(n_points, 1),
            "duty": round(duty, 1),
            "ok": ok,
        },
        "formula": formula,
        "segments": segments,
        "omitted_ms": round(omitted, 1),
        "peak_width_s": W,
        "n_min": n_min,
        "help": help_,
    }


# ======================================================================
# Tab 3 — 通量核算（漏检/混峰 + 反推，体现 massspec_flux_calculator）
# ======================================================================

def _do_flux_check(compounds, p):
    def param(key):
        v = p.get(key)
        return default_param(key) if v is None else v

    dev = DEVICES.get(param("instrument"), DEVICES[default_param("instrument")])
    dwell = float(param("dwell_ms"))
    delay_q1 = float(param("delay_q1_ms"))
    delay_q3 = float(param("delay_q3_ms"))
    overhead = float(param("overhead_ms"))
    W = float(param("peak_width_s"))
    n_min = int(param("n_min"))

    N1 = 0
    N3 = 0
    n_q3_sw = 0
    n_quant = 0
    n_qual = 0
    for c in compounds:
        if not (c.get("precursor") and c.get("product_quant")):
            continue
        N1 += 1
        n_ch_i = 1
        N3 += 1
        n_quant += 1
        if c.get("product_qual"):
            N3 += 1
            n_ch_i += 1
            n_qual += 1
        extra = int(c.get("extra_products") or 0)
        if extra > 0:
            N3 += extra
            n_ch_i += extra
        n_q3_sw += n_ch_i - 1          # 每个母离子单独加总其 (子离子数-1)

    n_q1_sw = N1 - 1
    delay_switch = max(delay_q1, delay_q3)  # Q1/Q3 并行稳定，取较慢者
    t_cycle = N3 * dwell + n_q3_sw * delay_q3 + n_q1_sw * delay_switch + overhead
    n_points = W * 1000.0 / t_cycle if t_cycle > 0 else float("inf")
    ok_points = n_points >= n_min
    ok_dwell = dwell >= dev["dwell_min_ms"]
    res_p = _res_params(p)

    def examine(kind):
        if kind == "precursor":
            vals = [c["precursor"] for c in compounds if c.get("precursor")]
        else:
            vals = [c["product_quant"] for c in compounds if c.get("product_quant")]
            vals += [c["product_qual"] for c in compounds if c.get("product_qual")]
        j = res_check_list(vals, dev, res_p)
        if j is None:
            return None
        return {
            "m1": round(j["a"], 3), "m2": round(j["b"], 3),
            "dm": round(j["dm"], 4),
            "fhsw_da": round(j["fhsw_da"], 4),
            "k": round(j["k"], 4),
            "need_da": round(j["need_da"], 4),
            "valley_pct": round(j["valley_pct"], 2),
            "overlap": j["overlap"],
        }

    prox = {"precursor": examine("precursor"), "product": examine("product")}
    p1 = prox["precursor"]
    ok_mass = True if p1 is None else not p1["overlap"]

    w_min_s = n_min * t_cycle / 1000.0
    denom = (N3 * dwell / N1 + (N3 / N1 - 1) * delay_q3 + delay_switch) if N1 > 0 else 999999
    max_precursors = int((W * 1000.0 / n_min - overhead + delay_switch) / denom) if denom > 0 else 999999
    dwell_max = ((W * 1000.0 / n_min - n_q3_sw * delay_q3 - n_q1_sw * delay_switch - overhead) / N3) if N3 > 0 else 999999
    dwell_ok_range = (dwell >= dev["dwell_min_ms"]) and (dwell <= dwell_max)

    return {
        "device_label": dev["label"],
        "n_compounds": n_quant, "n_qual": n_qual, "N1": N1, "N3": N3,
        "axes": {"t_cycle_ms": round(t_cycle, 1), "n_points": round(n_points, 1)},
        "checks": [
            {"key": "points", "ok": ok_points,
             "msg": (f"点数 {n_points:.1f} ≥ {n_min}，达标" if ok_points else
                     f"点数 {n_points:.1f} < {n_min}，不达标——峰画不圆、有漏检风险")},
            {"key": "dwell", "ok": ok_dwell,
             "msg": (f"dwell {dwell}ms ≥ 设备下限 {dev['dwell_min_ms']}ms，仪器做得到" if ok_dwell else
                     f"dwell {dwell}ms < 设备下限 {dev['dwell_min_ms']}ms，该设备做不到")},
            {"key": "mass", "ok": ok_mass,
             "msg": ("母离子不足 2 个，无质量混峰风险" if p1 is None else
                     (f"母离子最近 {p1['m1']} vs {p1['m2']}：Δm={p1['dm']}Da ≥ 判据 {p1['need_da']}Da"
                      f"（= {p1['k']}×FWHM {p1['fhsw_da']}Da），分开"
                      if not p1["overlap"] else
                      f"母离子 {p1['m1']} vs {p1['m2']}：Δm={p1['dm']}Da < 判据 {p1['need_da']}Da"
                      f"（= {p1['k']}×FWHM {p1['fhsw_da']}Da），质量混峰、谷值仅 {p1['valley_pct']}%"))},
        ],
        "prox": prox,
        "res": _res_summary(dev, p),
        "backderive": {
            "w_min_s": round(w_min_s, 2),
            "max_precursors": max_precursors,
            "dwell_max_ms": round(dwell_max, 2),
            "dwell_min_ms": dev["dwell_min_ms"],
            "dwell_ok_range": dwell_ok_range,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/config":
            # 控件值域随配置一并下发，界面据此注入 min/max/step；
            # 范围只此一份，不在 HTML 里另写
            payload = config_mgr.get_all()
            payload["param_spec"] = param_spec()
            self._send(200, payload)
        elif self.path == "/api/columns":
            self._send(200, {k: {"label": v["label"], "note": v["note"],
                                 "key": v.get("key", "pi"),
                                 "key_label": v.get("key_label", "极性指数")}
                             for k, v in COLUMN_TYPES.items()})
        elif self.path == "/api/devices":
            self._send(200, {k: {"label": v["label"], "dwell_min_ms": v["dwell_min_ms"],
                                 "n_min": v["n_min"], "res_note": v["res_note"],
                                 "res_da": v.get("res_da"), "res_ppm": v.get("res_ppm")}
                             for k, v in DEVICES.items()})
        elif self.path == "/api/res_presets":
            self._send(200, res_preset_payload())
        elif self.path == "/api/backends":
            self._send(200, {"backends": ["lm-studio", "ollama", "custom"],
                             "current": config_mgr.get("llm.backend", "lm-studio"),
                             "base_url": config_mgr.get("llm.base_url", ""),
                             "model": config_mgr.get("llm.model", "")})
        elif self.path.startswith("/api/llm/models"):
            import urllib.parse
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            backend = (params.get("backend") or ["lm-studio"])[0]
            base_url = (params.get("base_url") or [""])[0]
            if not base_url:
                base_url = BACKEND_DEFAULTS.get(backend, "")
            try:
                client = LLMClient(backend=backend, base_url=base_url)
                models = client.list_models()
                self._send(200, {"success": True, "models": models})
            except Exception as e:
                self._send(200, {"success": False, "models": [], "error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/config":
            body = self._read_body()
            config_mgr.update(body)
            self._send(200, {"ok": True})
        elif self.path == "/api/backend":
            body = self._read_body()
            if "backend" in body: config_mgr.set("llm.backend", body["backend"])
            if "base_url" in body: config_mgr.set("llm.base_url", body["base_url"])
            if "model" in body: config_mgr.set("llm.model", body["model"])
            if "api_key" in body: config_mgr.set("llm.api_key", body["api_key"])
            self._send(200, {"ok": True})
        elif self.path == "/api/backend/test":
            body = self._read_body()
            backend = body.get("backend", config_mgr.get("llm.backend", "lm-studio"))
            base_url = body.get("base_url", "")
            if not base_url:
                base_url = BACKEND_DEFAULTS.get(backend, "")
            llm = LLMClient(backend=backend, base_url=base_url,
                            model=body.get("model", config_mgr.get("llm.model", "")),
                            api_key=body.get("api_key", config_mgr.get("llm.api_key", "not-needed")))
            ok, msg = llm.test_connection()
            self._send(200, {"ok": ok, "message": msg})
        elif self.path == "/api/extract":
            body = self._read_body()
            text = body.get("text", "").strip()
            if not text:
                self._send(200, {"ok": False, "error": "请输入化合物文本"})
                return
            try:
                result = _extract_compounds(text)
                self._send(200, {
                    "ok": result["ok"],
                    "mode": result["mode"],
                    "compounds": result["compounds"],
                    "failures": result["failures"],
                    "missing": result["missing"],
                    "warnings": result["warnings"],
                    "error": None if result["ok"] else _extract_error(result),
                })
            except LLMClientError as e:
                self._send(200, {"ok": False, "error": f"LLM 调用失败：{e}"})
            except extractor.ExtractionError as e:
                self._send(200, {"ok": False, "error": f"LLM 输出不符合协议：{e}"})
            except Exception as e:
                self._send(200, {"ok": False, "error": f"解析失败：{e}"})
        elif self.path == "/api/separate":
            body = self._read_body()
            compounds = body.get("compounds", [])
            params = body.get("params", {})
            if not compounds:
                self._send(200, {"ok": False, "error": "没有化合物"})
                return
            try:
                result = _do_separate(compounds, params)
                self._send(200, {"ok": True, **result})
            except Exception as e:
                self._send(200, {"ok": False, "error": f"{e}\n{traceback.format_exc()[:500]}"})
        elif self.path == "/api/time_sim":
            body = self._read_body()
            try:
                self._send(200, {"ok": True, **_do_time_sim(body)})
            except Exception as e:
                self._send(200, {"ok": False, "error": f"{e}\n{traceback.format_exc()[:500]}"})
        elif self.path == "/api/flux_check":
            body = self._read_body()
            compounds = body.get("compounds", [])
            if not compounds:
                self._send(200, {"ok": False, "error": "请先填写化合物离子对"})
                return
            try:
                self._send(200, {"ok": True, **_do_flux_check(compounds, body.get("params", {}))})
            except Exception as e:
                self._send(200, {"ok": False, "error": f"{e}\n{traceback.format_exc()[:500]}"})
        else:
            self._send(404, {"error": "not found"})


def run_server(host="0.0.0.0", port=8810, **kwargs):
    if kwargs.get("backend"):
        config_mgr.set("llm.backend", kwargs["backend"])
    if kwargs.get("base_url"):
        config_mgr.set("llm.base_url", kwargs["base_url"])
    if kwargs.get("model"):
        config_mgr.set("llm.model", kwargs["model"])
    if kwargs.get("api_key"):
        config_mgr.set("llm.api_key", kwargs["api_key"])
    if kwargs.get("pidfile"):
        try:
            with open(kwargs["pidfile"], "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"  LC-MS 分组顾问 → http://localhost:{port}")
    server.serve_forever()


# ======================================================================
# HTML
# ======================================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LC-MS 分组顾问</title>
<style>
:root{--bg:#1a1a2e;--bg-card:#16213e;--bg-panel:#0f3460;--bg-input:#1a1a3e;--text:#e0e0e0;--text-dim:#8899aa;--accent:#e94560;--accent2:#533483;--green:#00b894;--border:#2a2a4e;--radius:8px;--warn:#d68910}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}
.topbar{height:48px;background:linear-gradient(135deg,var(--accent2),var(--accent));display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
.topbar .logo{font-weight:700;font-size:16px}.topbar .tag{font-size:11px;opacity:.75}.topbar .spacer{flex:1}
.topbar button{padding:4px 12px;border:1px solid rgba(255,255,255,.4);border-radius:4px;background:transparent;color:#fff;cursor:pointer;font-size:12px}
.topbar .status{font-size:12px}.topbar .status.ok{color:#a8f0d4}.topbar .status.fail{color:#ffc9d2}
.tab-bar{display:flex;background:var(--bg-panel);border-bottom:1px solid var(--border);flex-shrink:0;padding:0 12px}
.tab-btn{padding:10px 22px;cursor:pointer;color:var(--text-dim);font-size:14px;border-radius:8px 8px 0 0;margin:6px 2px 0 0;border:1px solid transparent;border-bottom:none;transition:all .2s}
.tab-btn:hover{color:var(--text);background:rgba(255,255,255,.05)}
.tab-btn.active{color:var(--text);background:var(--bg-card);border-color:var(--border)}
.tab-content{display:none;flex:1;min-height:0;overflow-y:auto}
.tab-content.active{display:block}
.panel{max-width:1200px;margin:0 auto;padding:20px 24px 40px}
.section{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;margin-bottom:16px}
.section h3{font-size:14px;margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.section h4{font-size:13px;margin:12px 0 8px;color:var(--accent)}
.form-row{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.form-row label{width:120px;min-width:120px;flex:0 0 120px;font-size:13px;color:var(--text-dim);white-space:nowrap}
.form-row input,.form-row select{padding:6px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:13px;font-family:inherit}
.form-row input[type="text"],.form-row input[type="number"],.form-row select{flex:1;min-width:120px}
.form-row input[type="range"]{flex:2;min-width:120px}
.form-row input:focus,.form-row select:focus{outline:none;border-color:var(--accent)}
.slider-val{min-width:44px;text-align:right;font-size:13px;color:var(--text)}
textarea{width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;font-family:inherit;resize:vertical;box-sizing:border-box}
textarea:focus{outline:none;border-color:var(--accent)}
.btn{padding:6px 16px;border:none;border-radius:4px;cursor:pointer;font-size:13px}
.btn:hover{opacity:.85}.btn-primary{background:var(--accent);color:#fff}.btn-secondary{background:var(--bg-panel);color:var(--text);border:1px solid var(--border)}.btn-success{background:var(--green);color:#fff}.btn-sm{padding:4px 10px;font-size:12px}
.badge{display:inline-block;padding:1px 6px;border-radius:8px;font-size:11px}
.badge.ok{background:var(--green);color:#fff}.badge.fail{background:var(--accent);color:#fff}.badge.warn{background:var(--warn);color:#fff}.badge.dim{background:var(--bg-input);color:var(--text-dim)}
.params-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media(max-width:800px){.params-grid{grid-template-columns:1fr}}
.compound-table,.flux-table,.help-table{width:100%;border-collapse:collapse;font-size:12px}
.compound-table th,.flux-table th,.help-table th{color:var(--text-dim);text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);font-weight:600}
.compound-table td,.flux-table td,.help-table td{padding:5px 8px;border-bottom:1px solid var(--border)}
.compound-table tr:hover td{background:rgba(255,255,255,.03)}
.flux-table input{width:100%;padding:4px 6px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:12px}
.flux-table input:focus{outline:none;border-color:var(--accent)}
.peak-chart{width:100%;height:70px;background:var(--bg-input);border-radius:6px;margin-top:10px;position:relative;overflow:hidden}
.peak-bar{position:absolute;top:8px;bottom:20px;width:3px;border-radius:2px}
.peak-label{position:absolute;font-size:9px;white-space:nowrap;transform:translateX(-50%);bottom:2px;pointer-events:none}
.gantt{width:100%;height:44px;background:var(--bg-input);border-radius:6px;margin-top:10px;display:flex;border:1px solid var(--border)}
.gantt .seg{height:100%;min-width:0;position:relative}
.gantt .seg:first-child{border-radius:5px 0 0 5px}
.gantt .seg:last-child{border-radius:0 5px 5px 0}
.gantt .seg:only-child{border-radius:5px}
.gantt .seg.dwell{background:var(--green)}
.gantt .seg.gap_q3{background:#3a4a6e}
.gantt .seg.gap_q1{background:#533483}
.gantt .seg.gap{background:#3a4a6e}
.gantt .seg.switch{background:var(--warn)}
.gantt .seg.omit{background:repeating-linear-gradient(45deg,#242b40 0,#242b40 4px,#3a4a6e 4px,#3a4a6e 8px)}
.gantt .seg:hover{z-index:20}
.gantt .seg:hover::after{content:attr(data-tip);position:absolute;top:-26px;left:50%;transform:translateX(-50%);background:#0b0e14;border:1px solid var(--border);padding:2px 6px;border-radius:3px;font-size:10px;white-space:nowrap;z-index:20;pointer-events:none}
.gantt-note{font-size:10px;color:var(--text-dim);margin-top:4px}
.axes-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}
@media(max-width:800px){.axes-grid{grid-template-columns:repeat(2,1fr)}}
.axis-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:6px;padding:10px 12px;text-align:center}
.axis-card .a-label{font-size:11px;color:var(--text-dim)}
.axis-card .a-value{font-size:20px;font-weight:700;margin-top:4px}
.axis-card .a-unit{font-size:11px;color:var(--text-dim)}
.axis-card.ok .a-value{color:var(--green)}
.axis-card.fail .a-value{color:var(--accent)}
.formula-box{background:var(--bg-input);border-radius:6px;padding:8px 12px;margin-top:10px;font-size:12px;font-family:Consolas,monospace;color:var(--text)}
.group-block{background:var(--bg-panel);border:1px solid var(--border);border-radius:6px;padding:10px 12px;margin-bottom:8px}
.group-block .gb-head{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.group-block .gb-compounds{font-size:12px;color:var(--text-dim);line-height:1.6}
.kv{color:var(--text-dim);font-size:11px}.kv b{color:var(--text)}
.suggestion{color:var(--warn);font-size:11px;margin-top:4px}
.check-row{display:flex;align-items:flex-start;gap:8px;padding:8px 10px;border-radius:4px;margin-bottom:6px;font-size:13px}
.check-row.ok{background:rgba(0,184,148,.08);border-left:3px solid var(--green)}
.check-row.fail{background:rgba(233,69,96,.08);border-left:3px solid var(--accent)}
select option{background:var(--bg-input);color:var(--text)}
.llm-config{display:none}.llm-config.show{display:block}
.param-help{font-size:10px;color:var(--text-dim);margin:2px 0 8px 130px;line-height:1.5}
.sub-head{font-size:11px;color:var(--text-dim);margin:12px 0 8px;padding-top:9px;border-top:1px dashed var(--border);letter-spacing:.04em}
.status{font-size:12px}.status.ok{color:var(--green)}.status.fail{color:var(--accent)}
</style>
</head>
<body>
<div class="topbar">
  <span class="logo">⚡ LC-MS 分组顾问</span>
  <span class="tag">分组预测 · 时间参数 · 通量核算 —— 同一个四轴引擎</span>
  <span class="spacer"></span>
  <button id="btn-toggle-llm">LLM 设置</button>
  <span class="status" id="llm-status">● 未检测</span>
</div>

<div class="tab-bar">
  <div class="tab-btn active" data-tab="group">分组预测</div>
  <div class="tab-btn" data-tab="time">时间参数</div>
  <div class="tab-btn" data-tab="flux">通量核算</div>
</div>

<div class="tab-content active" id="tab-group">
<div class="panel">
  <div class="section llm-config" id="llm-config-section">
    <h3>🔧 LLM 后端配置</h3>
    <div class="form-row">
      <label>后端</label>
      <select id="llm-backend"><option value="lm-studio" selected>LM Studio</option><option value="ollama">Ollama</option><option value="custom">Custom</option></select>
    </div>
    <div class="form-row">
      <label>地址</label>
      <input type="text" id="llm-base-url" value="http://localhost:1234">
    </div>
    <div class="form-row">
      <label>模型</label>
      <select id="llm-model" style="flex:2"><option value="">(请选择)</option></select>
      <button class="btn btn-sm btn-secondary" id="btn-refresh-models">刷新</button>
    </div>
    <div class="form-row">
      <button class="btn btn-sm btn-primary" id="btn-test-conn">测试连接</button>
      <span id="conn-status" class="status"></span>
    </div>
  </div>

  <div class="section">
    <h3>📋 化合物输入 <span style="font-size:12px;color:var(--text-dim);font-weight:400">（名称+分子式，LLM 提取；分子式决定极性排序）</span></h3>
    <textarea id="compound-input" rows="5" placeholder="粘贴化合物列表，例如：&#10;葡萄糖 C6H12O6&#10;咖啡因 C8H10N4O2&#10;丙酮 C3H6O"></textarea>
    <div class="form-row" style="margin-top:10px">
      <button class="btn btn-primary" id="btn-extract">提取化合物</button>
      <input type="file" id="file-upload" accept=".txt,.csv,.md" style="flex:1;max-width:300px;font-size:12px;color:var(--text-dim)">
      <span id="extract-status" class="status"></span>
    </div>
    <div id="compound-list" style="margin-top:12px"></div>
    <div id="extract-report"></div>
  </div>

  <div class="params-grid">
    <div class="section">
      <h3>色谱参数</h3>
      <div class="form-row"><label>柱类型</label><select id="column-type"></select></div>
      <div class="form-row"><label>分组模式</label>
        <select id="group-mode"><option value="capacity" selected>质谱能力反推</option><option value="threshold">百分比阈值</option></select>
      </div>
      <!-- 滑杆的 min/max/step 一律不在此处写死：由 /api/config 下发的 param_spec 注入
           （见 applyParamSpec）。写死会另成一份值域源头——源头改了它不改，界面上也看不见。 -->
      <div class="form-row" id="threshold-row" style="display:none">
        <label>位置差阈值(%)</label><input type="range" id="threshold-pct"><span class="slider-val" id="threshold-val">5.0</span>
      </div>
      <div class="form-row" id="span-cap-row">
        <label>组内跨度上限(%)</label><input type="range" id="max-span-pct"><span class="slider-val" id="max-span-val">100.0</span>
      </div>
      <p class="param-help" id="group-help"></p>
      <p class="kv" id="column-note" style="margin-top:8px"></p>
    </div>
    <div class="section">
      <h3>质谱参数</h3>
      <div class="form-row"><label>设备</label><select id="instrument"></select></div>
      <div class="form-row"><label>dwell(ms)</label><input type="range" id="dwell-ms"><span class="slider-val" id="dwell-val"></span></div>
      <div class="form-row"><label>峰宽(s)</label><input type="range" id="peak-width"><span class="slider-val" id="peak-width-val"></span></div>
      <div class="form-row"><label>子离子通道/化合物</label><input type="number" id="transitions-per" style="width:60px;flex:none"></div>
      <div class="sub-head">质量分辨率（决定「两个母离子分不分得开」）</div>
      <div class="form-row"><label>质量峰宽</label><select id="grp-fhsw-mode" data-res="1"></select></div>
      <div class="form-row" id="grp-fhsw-da-row" style="display:none"><label>FWHM (Da)</label><input type="number" id="grp-fhsw-da" data-res="1" style="width:70px;flex:none"></div>
      <div class="form-row" id="grp-fhsw-ppm-row" style="display:none"><label>FWHM (ppm)</label><input type="number" id="grp-fhsw-ppm" data-res="1" style="width:70px;flex:none"></div>
      <div class="form-row"><label>谷值判据</label><select id="grp-valley" data-res="1"></select></div>
      <div class="form-row" id="grp-k-row" style="display:none"><label>k 系数</label><input type="number" id="grp-k-custom" data-res="1" style="width:70px;flex:none"></div>
      <p class="param-help">是什么：质量轴上的峰宽 FWHM（不是色谱时间峰宽）。两个母离子质量差 Δm 小于峰宽时，谱峰糊成一个包——<b>仪器能称准 ≠ 能分开</b>。｜ 影响：判据 Δm ≥ k×FWHM。10% 谷（质谱经典）k=2.08，50% 谷 k=1.41。<b>质量不可分辨的离子对会被强制拆到不同组</b>。</p>
      <p class="kv" id="device-note" style="margin-top:8px"></p>
    </div>
    <div class="section">
      <h3>高级参数</h3>
      <div class="form-row"><label>点数下限</label><input type="number" id="n-min" style="width:60px;flex:none"></div>
      <div class="form-row"><label>Q3切换延迟(ms)</label><input type="number" id="delay-ms" style="width:60px;flex:none"></div>
      <div class="form-row"><label>Q1切换延迟(ms)</label><input type="number" id="delay-q1-ms" style="width:60px;flex:none"></div>
      <div class="form-row"><label>额外开销(ms)</label><input type="number" id="overhead-ms" style="width:60px;flex:none"></div>
    </div>
  </div>

  <div class="section">
    <h3>📊 分组预测结果</h3>
    <div id="result-area"><p style="color:var(--text-dim);font-size:13px">请先输入化合物并提取。</p></div>
  </div>
</div>
</div>

<div class="tab-content" id="tab-time">
<div class="panel">
  <div class="section">
    <h3>⏱ 设备与参数 <span style="font-size:12px;color:var(--text-dim);font-weight:400">（四台设备各自的 T_cycle 拼法）</span></h3>
    <div class="form-row"><label>设备</label><select id="time-device"><option value="qqq">三重四极杆 QqQ</option><option value="qtof">四极杆-飞行时间 Q-TOF（实验性）</option><option value="it">（线性）离子阱（实验性）</option><option value="qtrap">Q离子阱 QTRAP（实验性）</option></select></div>
    <!-- 本页控件的 min/max/step 与缺省值同样一律不在此处写死：由 /api/config 下发的
         param_spec 注入（见 applyParamSpec / loadConfig）。 -->
    <div class="form-row"><label>色谱峰宽(s)</label><input type="range" id="time-peak-width"><span class="slider-val" id="time-peak-width-val"></span></div>
    <div class="form-row"><label>点数下限</label><input type="number" id="time-n-min" style="width:60px;flex:none"></div>
    <p class="param-help">峰宽、点数下限、dwell、Q1/Q3 切换延迟、额外开销、子离子通道数与「分组预测」页是<b>同一个物理量</b>，两侧控件绑定同一份配置、改动互相同步；其余参数只属于本页试算，不写入配置文件。</p>

    <div id="time-params">
      <div class="time-param-panel" data-dev="qqq">
        <div class="form-row"><label>母离子数 N₁</label><input type="range" id="qqq-n1"><span class="slider-val" id="qqq-n1-val"></span></div>
        <p class="param-help">是什么：一个 cycle 里采集的不同母离子（化合物）总数 ｜ 影响：N₁ 越大→总通道数(N₁×N₃)与边界切换次数(N₁-1)同步增大→cycle 越长</p>
        <div class="form-row"><label>子离子通道数 N₃</label><input type="range" id="qqq-n3"><span class="slider-val" id="qqq-n3-val"></span></div>
        <p class="param-help">是什么：<b>每个母离子（化合物）</b>对应的子离子通道数，即该化合物的离子对数（2=定量+定性，3+=加额外通道） ｜ 影响：总通道数 = N₁×N₃，N₃ 调大→总通道数按 N₁ 倍放大→dwell 段变长→点数减少</p>
        <p class="param-help">各化合物通道数不同时：填算术平均 N₃ =（n₁+n₂+…+n_k）÷ k（k = 母离子数）。该值在 cycle 总量上与逐化合物累加<b>完全等价</b>（dwell 与 delay_q3 在推导中约去），可填小数；拖滑杆后用方向键微调</p>
        <p class="param-help">此处的 N₃ 与「分组预测」页的「子离子通道/化合物」是同一个量，改动会同步到那里。</p>
        <div class="form-row"><label>dwell(ms)</label><input type="range" id="qqq-dwell"><span class="slider-val" id="qqq-dwell-val"></span></div>
        <p class="param-help">是什么：Q1 选母离子、Q3 同时选子离子，停在一个离子对上采信号的时长 ｜ 影响：调大→信号强灵敏度高，但 cycle 变长→点数少</p>
        <div class="form-row"><label>Q3切换延迟(ms)</label><input type="range" id="qqq-delay-q3"><span class="slider-val" id="qqq-delay-q3-val"></span></div>
        <p class="param-help">是什么：同一化合物内 Q3 从一个子离子切到另一个并稳定的时间（Q1 保持不动） ｜ 影响：纯开销，次数 = N₁×(N₃-1)——每个母离子内 N₃ 个通道之间有 N₃-1 次，按母离子分别加总</p>
        <div class="form-row"><label>Q1切换延迟(ms)</label><input type="range" id="qqq-delay-q1"><span class="slider-val" id="qqq-delay-q1-val"></span></div>
        <p class="param-help">是什么：Q1 换母离子的同时子离子也换，Q1/Q3 同时动作 ｜ 影响：两极并行稳定，边界单次开销 = max(delay_q1, delay_q3) 而非两者相加；次数 = N₁ - 1</p>
        <div class="form-row"><label>额外开销(ms)</label><input type="range" id="qqq-overhead"><span class="slider-val" id="qqq-overhead-val"></span></div>
        <p class="param-help">是什么：每个 cycle 的固定开销，含触发与传输，与化合物数、通道数无关 ｜ 影响：纯开销，直接加到 cycle 上；数目少时它占比可观</p>
      </div>
      <div class="time-param-panel" data-dev="qtof" style="display:none">
        <div class="form-row"><label>推斥频率(kHz)</label><input type="range" id="qtof-fpush"><span class="slider-val" id="qtof-fpush-val"></span></div>
        <p class="param-help">是什么：TOF 每秒推斥离子进飞行管的次数 ｜ 影响：单次推斥=一张原始谱，决定原始采样密度</p>
        <div class="form-row"><label>叠加次数 nsum</label><input type="range" id="qtof-nsum"><span class="slider-val" id="qtof-nsum-val"></span></div>
        <p class="param-help">是什么：多少次推斥叠加成一张输出谱 ｜ 影响：调大→信噪比好，但谱图率=f_push/nsum 下降→点数少</p>
        <p class="param-help">TOF 并行采集全质量范围——化合物数量不影响 cycle，与 QqQ 的本质区别。</p>
      </div>
      <div class="time-param-panel" data-dev="it" style="display:none">
        <div class="form-row"><label>扫描范围(Da)</label><input type="range" id="it-dm"><span class="slider-val" id="it-dm-val"></span></div>
        <p class="param-help">是什么：一张谱覆盖的质量区间 Δm/z ｜ 影响：范围越宽扫描越慢→cycle 长→点数少</p>
        <div class="form-row"><label>扫描速率(Da/s)</label><input type="range" id="it-rate"><span class="slider-val" id="it-rate-val"></span></div>
        <p class="param-help">是什么：阱逐出离子的速度 ｜ 影响：调快→扫描段短→点数多，但分辨率下降</p>
        <div class="form-row"><label>填充时间(ms)</label><input type="range" id="it-fill"><span class="slider-val" id="it-fill-val"></span></div>
        <p class="param-help">是什么：阱内积累离子的时长（AGC 自动调 0.01~数百ms） ｜ 影响：信号主要在此积累；浓度高时 AGC 自动缩短</p>
      </div>
      <div class="time-param-panel" data-dev="qtrap" style="display:none">
        <div class="form-row"><label>母离子数 N₁</label><input type="range" id="qtrap-n1"><span class="slider-val" id="qtrap-n1-val"></span></div>
        <div class="form-row"><label>子离子通道数 N₃</label><input type="range" id="qtrap-n3"><span class="slider-val" id="qtrap-n3-val"></span></div>
        <p class="param-help">是什么：每个母离子对应的子离子通道数（总通道数 = N₁×N₃） ｜ 影响：Q3 切次数 = N₁×(N₃-1)；跨母离子时 Q1/Q3 并行稳定，单次取 max(delay_q1, delay_q3)</p>
        <p class="param-help">各化合物通道数不同时：填算术平均 N₃ =（n₁+n₂+…+n_k）÷ k（k = 母离子数），与逐化合物累加在 cycle 总量上<b>完全等价</b>，可填小数</p>
        <div class="form-row"><label>dwell(ms)</label><input type="range" id="qtrap-dwell"><span class="slider-val" id="qtrap-dwell-val"></span></div>
        <div class="form-row"><label>Q3切换延迟(ms)</label><input type="range" id="qtrap-delay-q3"><span class="slider-val" id="qtrap-delay-q3-val"></span></div>
        <div class="form-row"><label>Q1切换延迟(ms)</label><input type="range" id="qtrap-delay-q1"><span class="slider-val" id="qtrap-delay-q1-val"></span></div>
        <p class="param-help">是什么：Q3 既是四极杆（MRM 段）又是阱（阱扫描段），两模式不能并行 ｜ 影响：MRM 段+阱段+模式切换共享一个 cycle 预算</p>
        <div class="form-row"><label>阱扫描范围(Da)</label><input type="range" id="qtrap-dm"><span class="slider-val" id="qtrap-dm-val"></span></div>
        <div class="form-row"><label>阱扫描速率(Da/s)</label><input type="range" id="qtrap-rate"><span class="slider-val" id="qtrap-rate-val"></span></div>
        <div class="form-row"><label>阱填充(ms)</label><input type="range" id="qtrap-fill"><span class="slider-val" id="qtrap-fill-val"></span></div>
        <div class="form-row"><label>模式切换(ms)</label><input type="range" id="qtrap-switch"><span class="slider-val" id="qtrap-switch-val"></span></div>
        <p class="param-help">是什么：Q3 从 MRM 切到阱模式的往返开销（数十 ms 实测量级） ｜ 影响：纯开销，切换越频繁 cycle 越长</p>
      </div>
    </div>
  </div>

  <div class="section">
    <h3>📊 四轴结果</h3>
    <div id="time-result"><p style="color:var(--text-dim);font-size:13px">加载中...</p></div>
  </div>
</div>
</div>

<div class="tab-content" id="tab-flux">
<div class="panel">
  <div class="section">
    <h3>🧪 离子对输入 <span style="font-size:12px;color:var(--text-dim);font-weight:400">（名称+母离子+定量/定性子离子，直接填质量）</span></h3>
    <table class="flux-table">
      <thead><tr><th style="width:30%">名称</th><th>母离子 m/z</th><th>定量子 m/z</th><th>定性子 m/z(可选)</th><th style="width:50px"></th></tr></thead>
      <tbody id="flux-tbody"></tbody>
    </table>
    <div class="form-row" style="margin-top:10px">
      <button class="btn btn-sm btn-secondary" id="btn-flux-add">+ 添加一行</button>
      <span class="kv" id="flux-hint"></span>
    </div>
  </div>

  <div class="params-grid">
    <div class="section">
      <h3>质谱参数</h3>
      <div class="form-row"><label>设备</label><select id="flux-instrument"></select></div>
      <div class="form-row"><label>dwell(ms)</label><input type="range" id="flux-dwell"><span class="slider-val" id="flux-dwell-val"></span></div>
      <div class="form-row"><label>峰宽(s)</label><input type="range" id="flux-peak-width"><span class="slider-val" id="flux-peak-width-val"></span></div>
      <div class="sub-head">质量分辨率（决定「两个母离子分不分得开」）</div>
      <div class="form-row"><label>质量峰宽</label><select id="flux-fhsw-mode" data-res="1"></select></div>
      <div class="form-row" id="flux-fhsw-da-row" style="display:none"><label>FWHM (Da)</label><input type="number" id="flux-fhsw-da" data-res="1" style="width:70px;flex:none"></div>
      <div class="form-row" id="flux-fhsw-ppm-row" style="display:none"><label>FWHM (ppm)</label><input type="number" id="flux-fhsw-ppm" data-res="1" style="width:70px;flex:none"></div>
      <div class="form-row"><label>谷值判据</label><select id="flux-valley" data-res="1"></select></div>
      <div class="form-row" id="flux-k-row" style="display:none"><label>k 系数</label><input type="number" id="flux-k-custom" data-res="1" style="width:70px;flex:none"></div>
      <p class="param-help">是什么：质量轴上的峰宽 FWHM。「跟随设备标称」用厂家指标；<b>实机快扫描/高浓度时峰宽常宽 2–5 倍，请改成实测值</b>。｜ 判据 Δm ≥ k×FWHM，k 由谷值标准决定（10% 谷 → k=2.08）。⚠「峰宽(s)」是色谱时间轴峰宽，与本项不是一回事。</p>
    </div>
    <div class="section">
      <h3>时间参数</h3>
      <div class="form-row"><label>点数下限</label><input type="number" id="flux-n-min" style="width:60px;flex:none"></div>
      <div class="form-row"><label>Q3切换延迟(ms)</label><input type="number" id="flux-delay" style="width:60px;flex:none"></div>
      <div class="form-row"><label>Q1切换延迟(ms)</label><input type="number" id="flux-delay-q1" style="width:60px;flex:none"></div>
      <div class="form-row"><label>额外开销(ms)</label><input type="number" id="flux-overhead" style="width:60px;flex:none"></div>
      <p class="param-help">这几个参数与「分组预测」「时间参数」两页共用同一份配置，改动会同步过去。</p>
    </div>
    <div class="section">
      <h3>说明</h3>
      <p class="kv" style="line-height:1.8">给一批离子对，核算在当前 dwell/峰宽下：<br>① 点数够不够（会不会漏检）<br>② dwell 做不做到（设备下限）<br>③ 质量混不混峰（判据 Δm ≥ k×FHSW）<br>并反推峰宽下限 / 最大母离子数 / dwell 可行区间。<br><br>注：N₁ = 母离子数（= 化合物数，每行一个），N₃ = 子离子通道总数 = Σ 每化合物的子离子个数（定量+定性+额外）。cycle = N₃×dwell + [Σ(nᵢ-1)]×Q3切换 + (N₁-1)×max(Q1切换, Q3切换) + 开销。同一化合物内子离子间切 Q3（Q1 不动，次数 = nᵢ-1，按母离子分别加总）；跨化合物时 Q1 与 Q3 同时动作、并行稳定，单次开销取两者较大者而非相加。<br><br><b>k 的来历</b>：两个等高等宽高斯峰，谷值 v 时 Δm = σ√(8·ln(2/v))，而 FWHM = 2σ√(2ln2)，故 k = √(8·ln(2/v)) / 2.3548。v=10%（质谱经典「10% 谷」）→ k=2.079；v=50% → 1.414；v=1% → 2.765。<b>Δm/FWHM = 1 时谷值仍有 94%，两个峰完全看不出分离</b>，所以 k 不能取 1。</p>
    </div>
  </div>

  <div class="section">
    <h3>📊 核算结果</h3>
    <div id="flux-result"><p style="color:var(--text-dim);font-size:13px">请先填写离子对。</p></div>
  </div>
</div>
</div>

<script>
'use strict';
var compounds = [];
var columns = {}, devices = {};

function api(url, opts) {
  return fetch(url, opts).then(function(r) { return r.json(); });
}
function apiPost(url, body) {
  return api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
}

// ---------- Tab 切换 ----------
function initTabs() {
  var btns = document.querySelectorAll('.tab-btn');
  btns.forEach(function(btn) {
    btn.addEventListener('click', function() {
      btns.forEach(function(b) { b.classList.remove('active'); });
      document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
      btn.classList.add('active');
      var el = document.getElementById('tab-' + btn.dataset.tab);
      if (el) el.classList.add('active');
      // 切页即重算：参数（dwell / 峰宽 / 切换延迟…）在三个页面间共享，在别页改过之后
      // 本页的旧结果已经不对，此处补算，避免展示过期数字。
      if (btn.dataset.tab === 'group') updateResult();
      else if (btn.dataset.tab === 'time') updateTimeSim();
      else if (btn.dataset.tab === 'flux') updateFlux();
    });
  });
}

// ---------- 配置 / LLM ----------
// 参数元数据（默认值 / 取值域），随 /api/config 下发。控件的 min/max/step
// 与缺省值都从这里取，不另写字面量——写了就成了第二份源头，源头改了它不改。
var SPEC = {};
// 控件 id → 参数键。这层是"结构与参数的对应关系"，不是值的重复。
//
// 同一个键可以对应多个控件：它们描述的是**同一个物理量**，只是入口在不同页面或
// 不同设备面板（例如「分组预测」页的 dwell 与时间参数页 QqQ/QTRAP 面板的 dwell
// 都是四极杆的驻留时间）。这类控件互相同步、共用一份配置，改动写回 config.json。
// 只属于时间参数页试算的键（N₁ / 推斥频率 / 扫描速率…）不在配置里，不落盘。
var CTRL_SPEC = {
  // —— 分组预测页 ——
  'threshold-pct': 'threshold_pct',
  'max-span-pct': 'max_span_pct',
  'dwell-ms': 'dwell_ms',
  'peak-width': 'peak_width_s',
  'transitions-per': 'channels_per_compound',
  'n-min': 'n_min',
  'delay-ms': 'delay_q3_ms',
  'delay-q1-ms': 'delay_q1_ms',
  'overhead-ms': 'overhead_ms',
  // —— 时间参数页：与分组页同义的物理量，绑定同一个键，两侧同步 ——
  'time-peak-width': 'peak_width_s',
  'time-n-min': 'n_min',
  'qqq-dwell': 'dwell_ms', 'qtrap-dwell': 'dwell_ms',
  'qqq-delay-q3': 'delay_q3_ms', 'qtrap-delay-q3': 'delay_q3_ms',
  'qqq-delay-q1': 'delay_q1_ms', 'qtrap-delay-q1': 'delay_q1_ms',
  'qqq-overhead': 'overhead_ms',
  'qqq-n3': 'channels_per_compound', 'qtrap-n3': 'channels_per_compound',
  // —— 时间参数页独占：单次试算输入，不写入配置文件 ——
  'qqq-n1': 'n_precursors', 'qtrap-n1': 'n_precursors',
  'qtof-fpush': 'f_push_khz',
  'qtof-nsum': 'nsum',
  'it-dm': 'scan_range_da', 'qtrap-dm': 'scan_range_da',
  'it-rate': 'scan_rate_das', 'qtrap-rate': 'scan_rate_das',
  'it-fill': 'fill_ms', 'qtrap-fill': 'fill_ms',
  'qtrap-switch': 't_switch_ms',
  // —— 通量核算页：同样是上述物理量的第三处入口，绑定同一批键 ——
  'flux-dwell': 'dwell_ms',
  'flux-peak-width': 'peak_width_s',
  'flux-n-min': 'n_min',
  'flux-delay': 'delay_q3_ms',
  'flux-delay-q1': 'delay_q1_ms',
  'flux-overhead': 'overhead_ms'
};
// 历史键名别名：旧配置文件里可能残留，读取时兼容（写入一律用新名）
var PARAM_ALIAS = {
  'channels_per_compound': 'transitions_per_compound',
  'delay_q3_ms': 'delay_ms'
};
// 只注入取值域、**不参与镜像同步**的控件。
//
// 质量分辨率那组（峰宽来源 / FWHM / k 系数）的值由 mirrorRes 在 grp ↔ flux 之间
// 同步，并带下拉联动与显隐控制；若同时挂进 CTRL_SPEC，两个机制会各写一次、
// 先后顺序还不确定。这里只补它们的 min/max/step——这三个数字输入的范围原本
// 也是在两处 HTML 各写一份。
var SPEC_INJECT_ONLY = {
  'grp-fhsw-da': 'fhsw_da',
  'grp-fhsw-ppm': 'fhsw_ppm',
  'grp-k-custom': 'k_custom',
  'flux-fhsw-da': 'fhsw_da',
  'flux-fhsw-ppm': 'fhsw_ppm',
  'flux-k-custom': 'k_custom'
};
function applyParamSpec(spec) {
  SPEC = spec || {};
  [CTRL_SPEC, SPEC_INJECT_ONLY].forEach(function(table) {
    Object.keys(table).forEach(function(cid) {
      var el = document.getElementById(cid);
      var s = SPEC[table[cid]];
      if (!el || !s) return;
      if (s.min !== null && s.min !== undefined) el.min = s.min;
      if (s.max !== null && s.max !== undefined) el.max = s.max;
      if (s.step !== null && s.step !== undefined) el.step = s.step;
    });
  });
}
// 取参数值：已存配置优先（含旧别名），缺失时回落 SPEC 默认值（而不是就地字面量）
function pval(p, key) {
  if (p[key] !== undefined && p[key] !== null) return p[key];
  var al = PARAM_ALIAS[key];
  if (al && p[al] !== undefined && p[al] !== null) return p[al];
  return (SPEC[key] && SPEC[key].default !== undefined) ? SPEC[key].default : '';
}
// 同一个参数键的所有控件：任一改动即同步其余，保证它们不出现分叉值。
// 持久化键写回 config.json；时间参数页独占键（不在 savedParams 里）不落盘。
function mirrorParam(cid) {
  var key = CTRL_SPEC[cid];
  var src = document.getElementById(cid);
  if (!key || !src) return;
  Object.keys(CTRL_SPEC).forEach(function(other) {
    if (other === cid || CTRL_SPEC[other] !== key) return;
    var el = document.getElementById(other);
    if (el && el.value !== src.value) el.value = src.value;
  });
  // 滑杆旁的数值文本跟着刷新（它读的是控件当前值，不是本轮改动的那一个）
  updateSliderLabels();
  updateTimeSliderLabels();
  if (Object.prototype.hasOwnProperty.call(savedParams, key)) {
    savedParams[key] = Number(src.value);
    saveParams();
  }
}

function loadConfig() {
  return api('/api/config').then(function(cfg) {
    var p = (cfg && cfg.params) || {};
    applyParamSpec(cfg && cfg.param_spec);
    savedParams = p;
    // 数值控件统一回填：配置里有该键就用配置值，没有（时间参数页独占键）则回落
    // SPEC 默认值。两页同键的控件在这里拿到的是同一个值，无需各自初始化。
    Object.keys(CTRL_SPEC).forEach(function(cid) {
      var el = document.getElementById(cid);
      if (el) el.value = pval(p, CTRL_SPEC[cid]);
    });
    // 下拉控件由各自的权威来源填充（option 是异步加载的，此处只记初值）
    document.getElementById('column-type').value = pval(p, 'column_type');
    document.getElementById('group-mode').value = pval(p, 'group_mode');
    var llm = (cfg && cfg.llm) || {};
    document.getElementById('llm-backend').value = llm.backend || 'lm-studio';
    document.getElementById('llm-base-url').value = llm.base_url || 'http://localhost:1234';
    updateGroupRows();
    updateSliderLabels();
    updateTimeSliderLabels();
    return loadBackends();
  });
}

function loadColumns() {
  return api('/api/columns').then(function(data) {
    columns = data || {};
    var sel = document.getElementById('column-type');
    sel.innerHTML = '';
    Object.keys(columns).forEach(function(k) {
      var opt = document.createElement('option');
      opt.value = k; opt.textContent = columns[k].label;
      sel.appendChild(opt);
    });
    var note = document.getElementById('column-note');
    var setNote = function() { var c = columns[sel.value]; note.textContent = c ? c.note : ''; };
    sel.addEventListener('change', function() { setNote(); updateResult(); });
    setNote();
  });
}

function loadDevices() {
  return api('/api/devices').then(function(data) {
    devices = data || {};
    var sel = document.getElementById('instrument');
    var sel2 = document.getElementById('flux-instrument');
    sel.innerHTML = ''; sel2.innerHTML = '';
    Object.keys(devices).forEach(function(k) {
      [sel, sel2].forEach(function(s) {
        var opt = document.createElement('option');
        opt.value = k; opt.textContent = devices[k].label;
        s.appendChild(opt);
      });
    });
    var note = document.getElementById('device-note');
    var setNote = function() { var d = devices[sel.value]; note.textContent = d ? d.res_note : ''; };
    sel.addEventListener('change', function() { setNote(); updateResult(); });
    sel2.addEventListener('change', updateFlux);
    setNote();
  });
}

function loadBackends() {
  return api('/api/backends').then(function(data) {
    if (data.base_url) document.getElementById('llm-base-url').value = data.base_url;
    if (data.model) {
      var sel = document.getElementById('llm-model');
      var opt = document.createElement('option');
      opt.value = data.model; opt.textContent = data.model;
      sel.appendChild(opt);
      sel.value = data.model;
    }
    refreshModels();
  });
}

function refreshModels() {
  var backend = document.getElementById('llm-backend').value;
  var base_url = document.getElementById('llm-base-url').value;
  return api('/api/llm/models?backend=' + backend + '&base_url=' + encodeURIComponent(base_url)).then(function(data) {
    var sel = document.getElementById('llm-model');
    var cur = sel.value;
    sel.innerHTML = '<option value="">(请选择)</option>';
    if (data.success && data.models) {
      data.models.forEach(function(m) {
        var opt = document.createElement('option');
        opt.value = m; opt.textContent = m;
        sel.appendChild(opt);
      });
      if (cur && data.models.indexOf(cur) >= 0) sel.value = cur;
    }
  });
}

function testConn() {
  var st = document.getElementById('conn-status');
  st.textContent = '检测中...';
  st.className = 'status';
  apiPost('/api/backend/test', {
    backend: document.getElementById('llm-backend').value,
    base_url: document.getElementById('llm-base-url').value,
    model: document.getElementById('llm-model').value
  }).then(function(r) {
    st.textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
    st.className = 'status ' + (r.ok ? 'ok' : 'fail');
    var top = document.getElementById('llm-status');
    top.textContent = r.ok ? '● 已连接' : '● 未连接';
    top.className = 'status ' + (r.ok ? 'ok' : 'fail');
  });
}

function saveBackend() {
  apiPost('/api/backend', {
    backend: document.getElementById('llm-backend').value,
    base_url: document.getElementById('llm-base-url').value,
    model: document.getElementById('llm-model').value
  });
}

// ---------- 质量分辨率控件（Tab1 / Tab3 共用同一套判据，改动双向同步） ----------
var resPresets = null;
var resSyncing = false;
var savedParams = {};
var RES_SUFFIX = ['fhsw-mode', 'fhsw-da', 'fhsw-ppm', 'valley', 'k-custom'];

function loadResPresets() {
  return api('/api/res_presets').then(function(data) {
    resPresets = data || null;
    if (!resPresets) return;
    ['grp', 'flux'].forEach(function(pre) {
      var sm = document.getElementById(pre + '-fhsw-mode');
      var sv = document.getElementById(pre + '-valley');
      sm.innerHTML = '';
      Object.keys(resPresets.fhsw_modes).forEach(function(k) {
        var o = document.createElement('option');
        o.value = k; o.textContent = resPresets.fhsw_modes[k].label;
        sm.appendChild(o);
      });
      sv.innerHTML = '';
      Object.keys(resPresets.valley_presets).forEach(function(k) {
        var o = document.createElement('option');
        o.value = k; o.textContent = resPresets.valley_presets[k].label;
        sv.appendChild(o);
      });
      sm.value = resPresets.defaults.fhsw_mode;
      sv.value = resPresets.defaults.valley_preset;
      refreshResRows(pre);
    });
  });
}

function refreshResRows(pre) {
  var sm = document.getElementById(pre + '-fhsw-mode');
  var sv = document.getElementById(pre + '-valley');
  if (!sm || !sv) return;
  document.getElementById(pre + '-fhsw-da-row').style.display = sm.value === 'constant' ? 'flex' : 'none';
  document.getElementById(pre + '-fhsw-ppm-row').style.display = sm.value === 'proportional' ? 'flex' : 'none';
  document.getElementById(pre + '-k-row').style.display = sv.value === 'custom' ? 'flex' : 'none';
}

function resValues(pre) {
  function val(id) { var el = document.getElementById(pre + '-' + id); return el ? el.value : ''; }
  function num(id) { var v = parseFloat(val(id)); return isNaN(v) ? null : v; }
  return {
    fhsw_mode: val('fhsw-mode') || 'auto',
    fhsw_da: num('fhsw-da'),
    fhsw_ppm: num('fhsw-ppm'),
    valley_preset: val('valley') || 'v10',
    k_custom: num('k-custom')
  };
}

function mirrorRes(srcPre) {
  var dstPre = srcPre === 'grp' ? 'flux' : 'grp';
  RES_SUFFIX.forEach(function(suf) {
    var a = document.getElementById(srcPre + '-' + suf);
    var b = document.getElementById(dstPre + '-' + suf);
    if (a && b) b.value = a.value;
  });
  refreshResRows(dstPre);
}

// 已存设置回填：必须在 loadResPresets 填充下拉之后执行，否则 option 还不存在、赋值丢失
function restoreResParams() {
  var p = savedParams || {};
  var pairs = [['fhsw-mode', p.fhsw_mode], ['fhsw-da', p.fhsw_da], ['fhsw-ppm', p.fhsw_ppm],
               ['valley', p.valley_preset], ['k-custom', p.k_custom]];
  ['grp', 'flux'].forEach(function(pre) {
    pairs.forEach(function(kv) {
      var el = document.getElementById(pre + '-' + kv[0]);
      if (el && kv[1] !== undefined && kv[1] !== null && kv[1] !== '') el.value = kv[1];
    });
    refreshResRows(pre);
  });
}

function onResChange(srcPre) {
  if (resSyncing) return;
  resSyncing = true;
  refreshResRows(srcPre);
  mirrorRes(srcPre);
  resSyncing = false;
  if (srcPre === 'grp') updateResult(); else updateFlux();
}

function bindResControls() {
  ['grp', 'flux'].forEach(function(pre) {
    RES_SUFFIX.forEach(function(suf) {
      var el = document.getElementById(pre + '-' + suf);
      if (!el) return;
      el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', function() { onResChange(pre); });
    });
  });
}

// ---------- Tab1 分组预测 ----------
function getParams() {
  var rv = resValues('grp');
  return {
    column_type: document.getElementById('column-type').value,
    group_mode: document.getElementById('group-mode').value,
    threshold_pct: parseFloat(document.getElementById('threshold-pct').value),
    max_span_pct: parseFloat(document.getElementById('max-span-pct').value),
    instrument: document.getElementById('instrument').value,
    dwell_ms: parseFloat(document.getElementById('dwell-ms').value),
    peak_width_s: parseFloat(document.getElementById('peak-width').value),
    channels_per_compound: parseFloat(document.getElementById('transitions-per').value),
    n_min: parseInt(document.getElementById('n-min').value, 10),
    delay_q3_ms: parseFloat(document.getElementById('delay-ms').value),
    delay_q1_ms: parseFloat(document.getElementById('delay-q1-ms').value),
    overhead_ms: parseFloat(document.getElementById('overhead-ms').value),
    fhsw_mode: rv.fhsw_mode,
    fhsw_da: rv.fhsw_da,
    fhsw_ppm: rv.fhsw_ppm,
    valley_preset: rv.valley_preset,
    k_custom: rv.k_custom
  };
}
function saveParams() { return apiPost('/api/config', {params: getParams()}); }

function updateGroupRows() {
  var mode = document.getElementById('group-mode').value;
  document.getElementById('threshold-row').style.display = mode === 'threshold' ? 'flex' : 'none';
  document.getElementById('span-cap-row').style.display = mode === 'capacity' ? 'flex' : 'none';
  document.getElementById('group-help').innerHTML = mode === 'threshold'
    ? '是什么：<b>分组定义本身</b>——相邻两化合物的位置差 <b>≥</b> 此值即开新组。｜ 影响：调小 → 组更碎、每组化合物更少；调大 → 组更大。'
    : '是什么：<b>兜底判据</b>——一组化合物首尾铺开的宽度 <b>&gt;</b> 此值即开新组（先判「每组容量上限」和「质量可分辨」，再看它）。'
      + '位置经批次内归一化，全批跨度恒为 100%，故上限不可能超过 100%，取 100 即不干预。'
      + '｜ 影响：调小 → 该判据会抢在容量前面生效，分组由出峰位置而非质谱能力决定；临界值见结果区回显。';
}
function updateSliderLabels() {
  document.getElementById('threshold-val').textContent = parseFloat(document.getElementById('threshold-pct').value).toFixed(1);
  document.getElementById('max-span-val').textContent = parseFloat(document.getElementById('max-span-pct').value).toFixed(1);
  document.getElementById('dwell-val').textContent = document.getElementById('dwell-ms').value;
  document.getElementById('peak-width-val').textContent = parseFloat(document.getElementById('peak-width').value).toFixed(1);
  document.getElementById('flux-dwell-val').textContent = document.getElementById('flux-dwell').value;
  document.getElementById('flux-peak-width-val').textContent = parseFloat(document.getElementById('flux-peak-width').value).toFixed(1);
  document.getElementById('time-peak-width-val').textContent = parseFloat(document.getElementById('time-peak-width').value).toFixed(1);
}

function extractCompounds() {
  var text = document.getElementById('compound-input').value.trim();
  if (!text) return;
  var status = document.getElementById('extract-status');
  status.textContent = '提取中...';
  status.className = 'status';
  apiPost('/api/extract', {text: text}).then(function(r) {
    renderExtractReport(r.warnings || []);
    var got = r.compounds || [];
    if (got.length) {
      compounds = got;
      renderCompoundList();
      updateResult();
      fillFluxFromCompounds();
    }
    if (r.ok) {
      status.textContent = '✓ 提取 ' + got.length + ' 个化合物（' + (r.mode || 'anchors') + ' 模式）';
      status.className = 'status ok';
    } else {
      status.textContent = '✗ ' + (r.error || '抽取未通过校验');
      status.className = 'status fail';
    }
  });
}

// 抽取校验报告：把闸门命中情况原样透出——引用未命中、缺化学式、自动编号
function renderExtractReport(warns) {
  var div = document.getElementById('extract-report');
  if (!div) return;
  if (!warns.length) { div.innerHTML = ''; return; }
  var html = '<h4 style="margin-top:12px">抽取校验（' + warns.length + ' 项）</h4>';
  warns.forEach(function(w) {
    var mark = (w.level === 'info') ? '·' : '⚠';
    var cls = (w.level === 'info') ? 'check-row' : 'check-row fail';
    html += '<div class="' + cls + '"><span>' + mark + '</span><span>' + w.message + '</span></div>';
  });
  div.innerHTML = html;
}

function renderCompoundList() {
  var div = document.getElementById('compound-list');
  if (!compounds.length) { div.innerHTML = ''; return; }
  var html = '<table class="compound-table"><thead><tr><th>#</th><th>名称</th><th>分子式</th><th></th></tr></thead><tbody>';
  compounds.forEach(function(c, i) {
    html += '<tr><td>' + (i + 1) + '</td><td>' + c.name + '</td><td>' + (c.formula || '-') +
      '</td><td><button class="btn btn-sm btn-secondary" data-del="' + i + '">删除</button></td></tr>';
  });
  html += '</tbody></table>';
  div.innerHTML = html;
  div.querySelectorAll('[data-del]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      compounds.splice(parseInt(btn.dataset.del, 10), 1);
      renderCompoundList();
      updateResult();
    });
  });
}

function updateResult() {
  saveParams();
  if (!compounds.length) return;
  apiPost('/api/separate', {compounds: compounds, params: getParams()}).then(function(r) {
    if (r.ok) renderResult(r);
    else document.getElementById('result-area').innerHTML = '<p style="color:var(--accent)">✗ ' + r.error + '</p>';
  });
}

function renderResult(r) {
  var sep = r.separation, ms = r.ms_eval;
  var items = sep.items, groups = sep.groups;
  var colInfo = sep.column || {};
  var keyLabel = colInfo.key_label || '极性指数';
  var isMass = (colInfo.key === 'mass');
  var fmtKey = function(v) { return (isMass ? Number(v).toFixed(2) : Number(v).toFixed(4)); };
  var colors = ['#e94560','#533483','#00b894','#d68910','#0984e3','#fd79a8','#6c5ce7','#fab1a0'];
  // 组映射按化合物在输入列表中的原始序号（idx）建立——名称可能重复，不能用名称做键
  var groupOf = {};
  groups.forEach(function(g) {
    (g.members || []).forEach(function(m) { groupOf[m.idx] = g.group_id; });
  });

  var html = '';
  var warns = sep.warnings || [];
  if (warns.length) {
    html += '<h4 style="color:var(--accent)">⚠ 配置自检（' + warns.length + ' 项）</h4>' +
      '<p class="kv">以下问题不阻塞计算，但会让结果失去物理意义——请先确认再采信分组。</p>';
    warns.forEach(function(w) {
      html += '<div class="check-row fail"><span>⚠</span><span>' + w.message + '</span></div>';
    });
  }
  html += '<h4>出峰顺序（0% = 最先出，100% = 最后出）</h4><div class="peak-chart">';
  items.forEach(function(it) {
    var x = Math.min(Math.max(it.position_pct, 0.5), 99.5);
    var g = groupOf[it.idx] || 1;
    var color = colors[(g - 1) % colors.length];
    html += '<div class="peak-bar" style="left:' + x + '%;background:' + color + '" title="' + it.name + ' ' + it.position_pct + '%"></div>';
    if (items.length <= 20) {
      html += '<div class="peak-label" style="left:' + x + '%;color:' + color + '">' + it.name + '</div>';
    }
  });
  html += '</div>';

  if (items.length <= 30) {
    html += '<table class="compound-table" style="margin-top:10px"><thead><tr><th>位置%</th><th>名称</th><th>分子式</th><th>' + keyLabel + '</th><th>描述</th><th>组</th></tr></thead><tbody>';
    items.forEach(function(it) {
      var g = groupOf[it.idx] || 1;
      var flag = it.sort_value_valid ? '' : ' <span class="badge fail">占位</span>';
      html += '<tr><td>' + it.position_pct.toFixed(1) + '</td><td>' + it.name + '</td><td>' + it.formula +
        '</td><td>' + fmtKey(it.sort_value) + flag + '</td><td style="color:var(--text-dim)">' + it.polarity_desc +
        '</td><td><span class="badge dim">' + g + '</span></td></tr>';
    });
    html += '</tbody></table>';
  }

  html += '<h4 style="margin-top:16px">分组详情</h4>';
  var isCap = sep.max_span_pct !== null && sep.max_span_pct !== undefined;
  if (sep.max_compounds_per_group) {
    html += '<p class="kv">质谱能力反推：每组最多 <b>' + sep.max_compounds_per_group + '</b> 个化合物' +
      '　｜　组内跨度上限 <b>' + sep.max_span_pct + '%</b>（超过即开新组）</p>';
    if (isCap && sep.span_critical_pct !== null && sep.max_span_pct < sep.span_critical_pct) {
      html += '<p class="kv" style="color:var(--warn)">跨度上限低于临界值 <b>' + sep.span_critical_pct +
        '%</b>：该判据会先于容量判据生效，分组结果由出峰位置而非质谱能力决定。</p>';
    }
  } else {
    html += '<p class="kv">阈值分组：相邻位置差 &lt; <b>' + sep.threshold_pct + '%</b> 分同组</p>';
  }
  var splits = sep.split_reasons || [];
  var splitReason = {mass: '质量不可分辨', span: '组内跨度过宽', gap: '位置差超限', capacity: '达每组容量上限'};
  ms.groups.forEach(function(grp) {
    var g = grp.group, ev = grp.eval;
    var sp = (g.group_id >= 2) ? splits[g.group_id - 2] : null;
    var splitInfo = '';
    if (sp) {
      var rdet = '';
      if (sp.reason === 'span') rdet = '组内跨度 ' + sp.detail.span + '% &gt; 上限 ' + sp.detail.threshold +
        '%（自「' + sp.detail.head + '」' +
        (sp.detail.tail && sp.detail.tail !== sp.detail.head ? '铺到「' + sp.detail.tail + '」' : '起') + '）';
      else if (sp.reason === 'gap') rdet = '位置差 ' + sp.detail.gap + '% ' +
        (sp.detail.op === '>=' ? '≥' : '&gt;') + ' 阈值 ' + sp.detail.threshold + '%';
      else if (sp.reason === 'capacity') rdet = '已达上限 ' + sp.detail.max_compounds + ' 个/组';
      else if (sp.reason === 'mass') rdet = sp.detail.a + ' vs ' + sp.detail.b +
        '：Δm=' + Number(sp.detail.dm).toFixed(4) + ' Da &lt; 判据 ' + Number(sp.detail.need_da).toFixed(4) + ' Da';
      splitInfo = '<span class="kv" style="color:var(--text-dim);flex-basis:100%">↑ 本组因「' +
        (splitReason[sp.reason] || sp.reason) + '」拆出（' + rdet + '）</span>';
    }
    var color = colors[(g.group_id - 1) % colors.length];
    var verdict = ev.verdict === 'pass' ? '<span class="badge ok">✓ 达标</span>' : '<span class="badge fail">✗ 不达标</span>';
    var massBadge = '';
    if (ev.mass) {
      massBadge = ev.mass.overlap
        ? '<span class="badge fail">✗ 质量混峰</span>'
        : '<span class="badge ok">✓ 质量可分辨</span>';
    }
    html += '<div class="group-block" style="border-left:3px solid ' + color + '"><div class="gb-head">' +
      '<span class="badge dim" style="background:' + color + ';color:#fff">组' + g.group_id + '</span>' +
      '<span class="kv">位置 <b>' + g.range[0].toFixed(1) + '-' + g.range[1].toFixed(1) + '%</b></span>' +
      '<span class="kv"><b>' + g.n_compounds + '</b> 个化合物</span>' +
      '<span class="kv"><b>' + ev.n_transitions + '</b> 子离子通道</span>' +
      '<span class="kv">cycle = <b>' + ev.cycle_ms + 'ms</b></span>' +
      '<span class="kv">点数 = <b>' + ev.n_points + '</b></span>' + verdict + massBadge + splitInfo + '</div>' +
      '<div class="gb-compounds">' + g.compounds.join('、') + '</div>' +
      (ev.mass ? '<div class="kv" style="margin-top:3px">组内最近母离子 ' + ev.mass.a + ' vs ' + ev.mass.b +
        '：Δm=' + ev.mass.dm.toFixed(4) + 'Da，判据 k×FWHM = ' + ev.mass.k.toFixed(3) + '×' +
        ev.mass.fhsw_da.toFixed(4) + ' = ' + ev.mass.need_da.toFixed(4) + 'Da，谷值 ' +
        ev.mass.valley_pct.toFixed(1) + '%</div>' : '') +
      (ev.suggestion ? '<div class="suggestion">⚠ ' + ev.suggestion + '</div>' : '') + '</div>';
  });

  var s = ms.summary;
  html += '<h4 style="margin-top:16px">汇总</h4>';
  html += '<p class="kv">设备：<b>' + ms.device + '</b> ｜ 化合物总数：<b>' + s.n_total_compounds + '</b> ｜ 分组数：<b>' + s.n_groups +
    '</b> ｜ 通过：<b style="color:var(--green)">' + s.n_pass + '</b> ｜ 不通过：<b style="color:var(--accent)">' + s.n_fail + '</b></p>';
  var wg = s.without_grouping;
  var wgVerdict = wg.would_fail ? '<span class="badge fail">漏检!</span>' : '<span class="badge ok">达标</span>';
  html += '<p class="kv" style="margin-top:6px">不分组（全时段扫）：' + wg.n_transitions + ' 对，cycle=' + wg.cycle_ms +
    'ms，点数=' + wg.n_points + ' ' + wgVerdict + '</p>';
  if (wg.would_fail && s.n_pass === s.n_groups) {
    html += '<p style="color:var(--green);font-size:13px;margin-top:8px">✓ 分组后从漏检变为全部达标——分组策略有效</p>';
  }

  var resSum = r.res;
  if (resSum) {
    html += '<p class="kv" style="margin-top:6px">质量判据：Δm ≥ <b>' + resSum.k + '</b> × FWHM（对应谷值 ' + resSum.valley_pct + '%）｜ 峰宽 ' + resSum.fhsw_value + ' ' + resSum.fhsw_unit + '</p>';
  }
  var mc = sep.mass_conflicts || [];
  if (mc.length) {
    html += '<h4 style="margin-top:16px">质量不可分辨 → 已强制拆组（' + mc.length + ' 对）</h4>';
    html += '<p class="kv">位置差 = 两条离子对出峰位置之差（%）。位置差接近 0% 的是共洗脱组分——即便拆到不同组，实际采集窗口仍然重叠，靠时间调度解决不了；位置差大一些的，分组能真正拉开。</p>';
    mc.forEach(function(c) {
      html += '<div class="check-row fail"><span>⚠</span><span>' + c.a_name + ' vs ' + c.b_name +
        '：Δm=' + c.dm.toFixed(4) + 'Da &lt; 判据 ' + c.need_da.toFixed(4) + 'Da（k=' + c.k.toFixed(3) +
        ' × FWHM ' + c.fhsw_da.toFixed(4) + 'Da），位置差 <b>' + c.pos_gap + '%</b></span></div>';
    });
  }
  document.getElementById('result-area').innerHTML = html;
}

// ---------- Tab2 时间参数 ----------
// 请求载荷：键名以质谱引擎的读法为准（n_products / overhead_ms 等），
// 控件值则来自 CTRL_SPEC 定义的同一套参数，不在此处另写默认值。
function currentTimeParams() {
  var dev = document.getElementById('time-device').value;
  function f(id) { return parseFloat(document.getElementById(id).value); }
  function i(id) { return parseInt(document.getElementById(id).value, 10); }
  var p = {
    device: dev,
    peak_width_s: f('time-peak-width'),
    n_min: i('time-n-min')
  };
  if (dev === 'qqq') {
    p.n_precursors = i('qqq-n1');
    p.n_products = f('qqq-n3');
    p.dwell_ms = f('qqq-dwell');
    p.delay_q3_ms = f('qqq-delay-q3');
    p.delay_q1_ms = f('qqq-delay-q1');
    p.overhead_ms = f('qqq-overhead');
  } else if (dev === 'qtof') {
    p.f_push_khz = f('qtof-fpush');
    p.nsum = i('qtof-nsum');
  } else if (dev === 'it') {
    p.scan_range_da = f('it-dm');
    p.scan_rate_das = f('it-rate');
    p.fill_ms = f('it-fill');
  } else if (dev === 'qtrap') {
    p.n_precursors = i('qtrap-n1');
    p.n_products = f('qtrap-n3');
    p.dwell_ms = f('qtrap-dwell');
    p.delay_q3_ms = f('qtrap-delay-q3');
    p.delay_q1_ms = f('qtrap-delay-q1');
    p.scan_range_da = f('qtrap-dm');
    p.scan_rate_das = f('qtrap-rate');
    p.fill_ms = f('qtrap-fill');
    p.t_switch_ms = f('qtrap-switch');
  }
  return p;
}

function updateTimeSliderLabels() {
  var ids = ['time-peak-width','qqq-n1','qqq-n3','qqq-dwell','qqq-delay-q3','qqq-delay-q1','qqq-overhead',
    'qtof-fpush','qtof-nsum','it-dm','it-rate','it-fill',
    'qtrap-n1','qtrap-n3','qtrap-dwell','qtrap-delay-q3','qtrap-delay-q1','qtrap-dm','qtrap-rate','qtrap-fill','qtrap-switch'];
  ids.forEach(function(id) {
    var el = document.getElementById(id);
    var val = document.getElementById(id + '-val');
    if (el && val) {
      var step = parseFloat(el.step) || 1;
      val.textContent = step < 1 ? parseFloat(el.value).toFixed(1) : el.value;
    }
  });
}

function updateTimeSim() {
  updateTimeSliderLabels();
  apiPost('/api/time_sim', currentTimeParams()).then(function(r) {
    if (!r.ok) {
      document.getElementById('time-result').innerHTML = '<p style="color:var(--accent)">✗ ' + r.error + '</p>';
      return;
    }
    renderTimeSim(r);
  });
}

function renderTimeSim(r) {
  var ax = r.axes;
  var html = '<div class="axes-grid">';
  html += '<div class="axis-card"><div class="a-label">T_cycle 采集周期</div><div class="a-value">' + ax.t_cycle_ms + '</div><div class="a-unit">ms</div></div>';
  html += '<div class="axis-card ' + (ax.ok ? 'ok' : 'fail') + '"><div class="a-label">N_points 数据点数</div><div class="a-value">' + ax.n_points + '</div><div class="a-unit">点 (下限' + r.n_min + ')</div></div>';
  html += '<div class="axis-card"><div class="a-label">T_acq 有效采集占比</div><div class="a-value">' + ax.duty + '</div><div class="a-unit">%</div></div>';
  html += '<div class="axis-card ' + (ax.ok ? 'ok' : 'fail') + '"><div class="a-label">达标判定</div><div class="a-value">' + (ax.ok ? '✓' : '✗') + '</div><div class="a-unit">' + (ax.ok ? '峰能画圆' : '漏检风险') + '</div></div>';
  html += '</div>';
  html += '<div class="formula-box">' + r.formula + '</div>';

  html += '<h4>一个 cycle 内的时序（甘特图）</h4><div class="gantt">';
  var hasOmit = 0;
  r.segments.forEach(function(seg) {
    if (seg.ms <= 0) return;
    if (seg.kind === 'omit') hasOmit = seg.n_compounds || 0;
    // flex-grow 按耗时分配：宽度 = 段耗时 / Σ段耗时，天然占满 100%，无百分比舍入残差
    html += '<div class="seg ' + seg.kind + '" style="flex:' + seg.ms + ' 1 0" data-tip="' + seg.label + ' ' + seg.ms.toFixed(1) + 'ms"></div>';
  });
  html += '</div>';
  if (hasOmit) html += '<p class="gantt-note">（斜纹段 = 图后省略的 ' + hasOmit + ' 个化合物，已按实际耗时聚合计宽）</p>';
  html += '<p class="gantt-note" style="margin-top:2px"><span style="color:var(--green)">■</span> 采信号（dwell）  <span style="color:#3a4a6e">■</span> Q3切换（同化合物内子离子间）  <span style="color:#533483">■</span> Q1切换（跨化合物母离子间）  <span style="color:#3a4a6e">▨</span> 省略化合物（聚合）  <span style="color:var(--warn)">■</span> 开销/模式切换</p>';

  html += '<h4>参数解释</h4><table class="help-table"><thead><tr><th style="width:22%">参数</th><th style="width:36%">是什么</th><th>影响</th></tr></thead><tbody>';
  r.help.forEach(function(h) {
    html += '<tr><td style="color:var(--accent)">' + h.name + '</td><td>' + h.what + '</td><td>' + h.effect + '</td></tr>';
  });
  html += '</tbody></table>';
  document.getElementById('time-result').innerHTML = html;
}

function initTimeTab() {
  var devSel = document.getElementById('time-device');
  devSel.addEventListener('change', function() {
    document.querySelectorAll('.time-param-panel').forEach(function(p) {
      p.style.display = p.dataset.dev === devSel.value ? 'block' : 'none';
    });
    updateTimeSim();
  });
  document.querySelectorAll('#tab-time input').forEach(function(el) {
    el.addEventListener('input', updateTimeSim);
  });
  updateTimeSim();
}

// ---------- Tab3 通量核算 ----------
function addFluxRow(c) {
  c = c || {};
  var tbody = document.getElementById('flux-tbody');
  var tr = document.createElement('tr');
  function v(x) { return (x === null || x === undefined) ? '' : x; }
  tr.innerHTML = '<td><input type="text" class="f-name" value="' + String(v(c.name)).replace(/"/g, '&quot;') + '"></td>' +
    '<td><input type="number" step="0.01" class="f-prec" value="' + v(c.precursor) + '"></td>' +
    '<td><input type="number" step="0.01" class="f-quant" value="' + v(c.product_quant) + '"></td>' +
    '<td><input type="number" step="0.01" class="f-qual" value="' + v(c.product_qual) + '"></td>' +
    '<td><button class="btn btn-sm btn-secondary f-del">×</button></td>';
  tbody.appendChild(tr);
  tr.querySelector('.f-del').addEventListener('click', function() { tr.remove(); updateFlux(); });
  tr.querySelectorAll('input').forEach(function(inp) { inp.addEventListener('input', updateFlux); });
}

function collectFluxCompounds() {
  var arr = [];
  document.querySelectorAll('#flux-tbody tr').forEach(function(tr) {
    var name = tr.querySelector('.f-name').value.trim();
    var prec = parseFloat(tr.querySelector('.f-prec').value);
    var quant = parseFloat(tr.querySelector('.f-quant').value);
    var qualRaw = tr.querySelector('.f-qual').value.trim();
    var qual = qualRaw === '' ? null : parseFloat(qualRaw);
    if (name && !isNaN(prec) && !isNaN(quant)) {
      arr.push({name: name, precursor: prec, product_quant: quant,
                product_qual: (qual !== null && !isNaN(qual)) ? qual : null});
    }
  });
  return arr;
}

function fillFluxFromCompounds() {
  var withMz = compounds.filter(function(c) { return c.precursor && c.product_quant; });
  if (!withMz.length) return;
  var tbody = document.getElementById('flux-tbody');
  var first = tbody.querySelector('.f-name');
  if (tbody.children.length === 0 || (first && first.value === '')) {
    tbody.innerHTML = '';
    withMz.forEach(function(c) { addFluxRow(c); });
    updateFlux();
  }
}

function getFluxParams() {
  var rv = resValues('flux');
  return {
    instrument: document.getElementById('flux-instrument').value,
    dwell_ms: parseFloat(document.getElementById('flux-dwell').value),
    peak_width_s: parseFloat(document.getElementById('flux-peak-width').value),
    n_min: parseInt(document.getElementById('flux-n-min').value, 10),
    delay_q3_ms: parseFloat(document.getElementById('flux-delay').value),
    delay_q1_ms: parseFloat(document.getElementById('flux-delay-q1').value),
    overhead_ms: parseFloat(document.getElementById('flux-overhead').value),
    fhsw_mode: rv.fhsw_mode,
    fhsw_da: rv.fhsw_da,
    fhsw_ppm: rv.fhsw_ppm,
    valley_preset: rv.valley_preset,
    k_custom: rv.k_custom
  };
}

function updateFlux() {
  var comps = collectFluxCompounds();
  var hint = document.getElementById('flux-hint');
  hint.textContent = comps.length ? ('有效离子对 ' + comps.length + ' 条') : '';
  var area = document.getElementById('flux-result');
  if (!comps.length) { area.innerHTML = '<p style="color:var(--text-dim);font-size:13px">请先填写离子对。</p>'; return; }
  apiPost('/api/flux_check', {compounds: comps, params: getFluxParams()}).then(function(r) {
    if (!r.ok) { area.innerHTML = '<p style="color:var(--accent)">✗ ' + r.error + '</p>'; return; }
    renderFlux(r);
  });
}

function renderFlux(r) {
  var allOk = r.checks.every(function(c) { return c.ok; });
  var html = '<div class="axes-grid">';
  html += '<div class="axis-card"><div class="a-label">母离子数 N₁</div><div class="a-value">' + r.N1 + '</div><div class="a-unit">个化合物</div></div>';
  html += '<div class="axis-card"><div class="a-label">子离子通道数 N₃</div><div class="a-value">' + r.N3 + '</div><div class="a-unit">通道（含定性' + r.n_qual + '）</div></div>';
  html += '<div class="axis-card"><div class="a-label">T_cycle</div><div class="a-value">' + r.axes.t_cycle_ms + '</div><div class="a-unit">ms</div></div>';
  html += '<div class="axis-card ' + (allOk ? 'ok' : 'fail') + '"><div class="a-label">三条评测</div><div class="a-value">' + (allOk ? '✓' : '✗') + '</div><div class="a-unit">' + (allOk ? '全部通过' : '存在不达标') + '</div></div>';
  html += '</div>';

  html += '<h4>三条评测</h4>';
  r.checks.forEach(function(c) {
    html += '<div class="check-row ' + (c.ok ? 'ok' : 'fail') + '"><span>' + (c.ok ? '✓' : '✗') + '</span><span>' + c.msg + '</span></div>';
  });
  if (r.prox.product) {
    var pp = r.prox.product;
    html += '<p class="kv" style="margin-top:6px">子离子最近对：' + pp.m1 + ' vs ' + pp.m2 + '，Δm=' + pp.dm + 'Da，判据 ' + pp.need_da + 'Da（k=' + pp.k + ' × FWHM ' + pp.fhsw_da + 'Da），谷值 ' + pp.valley_pct + '%' + (pp.overlap ? ' <span class="badge fail">混峰</span>' : ' <span class="badge ok">分开</span>') + '</p>';
  }

  var rs = r.res;
  if (rs) {
    html += '<h4>质量分辨率判据</h4><table class="help-table"><tbody>';
    html += '<tr><td style="color:var(--accent);width:30%">判据 k = Δm/FWHM</td><td><b>' + rs.k + '</b>　对应谷值 <b>' + rs.valley_pct + '%</b>（两峰之间凹到峰高的 ' + rs.valley_pct + '%）　参考：50% 谷 k=1.41，10% 谷 k=2.08，1% 谷 k=2.76</td></tr>';
    html += '<tr><td style="color:var(--accent)">峰宽 FWHM 来源</td><td>' + rs.fhsw_value + ' ' + rs.fhsw_unit + (rs.fhsw_mode === 'auto' ? '　<b>厂家标称值；实机快扫描/高浓度时可能宽 2–5 倍，建议填实测值</b>' : '') + '</td></tr>';
    html += '</tbody></table>';
  }

  var bd = r.backderive;
  html += '<h4>反推</h4><table class="help-table"><tbody>';
  html += '<tr><td style="color:var(--accent);width:30%">峰宽下限</td><td>要保点数下限，色谱峰宽至少 <b>' + bd.w_min_s + 's</b></td></tr>';
  html += '<tr><td style="color:var(--accent)">最大母离子数</td><td>当前 dwell/峰宽下最多塞 <b>' + bd.max_precursors + '</b> 个母离子（化合物）</td></tr>';
  html += '<tr><td style="color:var(--accent)">dwell 可行区间</td><td>[' + bd.dwell_min_ms + ', ' + bd.dwell_max_ms + '] ms；当前 dwell 在区间内？' + (bd.dwell_ok_range ? '<span class="badge ok">是</span>' : '<span class="badge fail">否——要么设备做不到，要么点数不够</span>') + '</td></tr>';
  html += '</tbody></table>';

  html += '<p class="kv" style="margin-top:10px">设备：' + r.device_label + '</p>';
  document.getElementById('flux-result').innerHTML = html;
}

function initFluxTab() {
  for (var i = 0; i < 3; i++) addFluxRow();
  document.getElementById('btn-flux-add').addEventListener('click', function() { addFluxRow(); });
  document.querySelectorAll('#tab-flux input:not([data-res]), #tab-flux select:not([data-res])').forEach(function(el) {
    el.addEventListener('input', updateFlux);
  });
}

// ---------- init ----------
function bindTopbar() {
  document.getElementById('btn-toggle-llm').addEventListener('click', function() {
    document.getElementById('llm-config-section').classList.toggle('show');
  });
  document.getElementById('btn-refresh-models').addEventListener('click', refreshModels);
  document.getElementById('btn-test-conn').addEventListener('click', testConn);
  document.getElementById('llm-backend').addEventListener('change', function() {
    var b = document.getElementById('llm-backend').value;
    var defaults = {'lm-studio': 'http://localhost:1234', 'ollama': 'http://localhost:11434', 'custom': ''};
    document.getElementById('llm-base-url').value = defaults[b] || '';
    refreshModels();
  });
  document.getElementById('llm-base-url').addEventListener('change', refreshModels);
  document.getElementById('llm-model').addEventListener('change', saveBackend);
}

function bindGroupTab() {
  document.getElementById('btn-extract').addEventListener('click', extractCompounds);
  document.getElementById('file-upload').addEventListener('change', function(e) {
    var f = e.target.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function(ev) { document.getElementById('compound-input').value = ev.target.result; };
    reader.readAsText(f);
  });
  document.getElementById('group-mode').addEventListener('change', function() { updateGroupRows(); updateResult(); });
  ['threshold-pct', 'max-span-pct', 'dwell-ms', 'peak-width'].forEach(function(id) {
    document.getElementById(id).addEventListener('input', function() { updateSliderLabels(); updateResult(); });
  });
  ['transitions-per', 'n-min', 'delay-ms', 'delay-q1-ms', 'overhead-ms'].forEach(function(id) {
    document.getElementById(id).addEventListener('input', updateResult);
  });
  ['flux-dwell', 'flux-peak-width'].forEach(function(id) {
    document.getElementById(id).addEventListener('input', updateSliderLabels);
  });
}

// 参数镜像绑定：给登记在 CTRL_SPEC 里的每个控件挂一个 input 监听，把值同步到
// 同一个键的其余控件并落盘。独立于各页原有的刷新监听，互不干扰。
function bindParamMirroring() {
  Object.keys(CTRL_SPEC).forEach(function(cid) {
    var el = document.getElementById(cid);
    if (!el) return;
    el.addEventListener('input', function() { mirrorParam(cid); });
  });
}

function init() {
  initTabs();
  bindTopbar();
  bindGroupTab();
  bindParamMirroring();
  bindResControls();
  loadConfig().then(function() {
    loadColumns();
    return loadResPresets();
  }).then(function() {
    restoreResParams();
    return loadDevices();
  }).then(function() {
    updateFlux();
  });
  initTimeTab();
  initFluxTab();
}
init();
</script>
</body>
</html>
"""
