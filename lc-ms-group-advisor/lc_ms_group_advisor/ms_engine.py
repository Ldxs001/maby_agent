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

"""质谱引擎：每组 cycle 核算 + 点数 + 达标判定（复用已有四轴逻辑）。

设备参数表与 massspec_flux_calculator/devices.py 一致。
res_da / res_ppm 只是**标称默认值**，会被用户在界面上输入的实测峰宽覆盖，
不构成判据上限（见 mass_resolution.resolve_fhsw_da）。

标称与界面标注：仅 `qqq` 为已验证的正式支持设备，其余三台（it / qtrap / qtof）
为实验性支持，label 统一带「（实验性）」后缀，随 label 传播到所有展示面
（下拉框、核算结果 device、时间参数页 device_label、通量核算）。
判定逻辑（cycle / 点数 / 判据）本身不区分正式与实验性，不使用 label 做任何分支。
"""
DEVICES = {
    "qqq": {
        "label": "三重四极杆 QqQ",
        "dwell_min_ms": 2.0,
        "res_da": 0.7,
        "n_min": 15,
        "res_note": "单位质量分辨率 ≈ 0.7 Da（FWHM，恒定，不随 m/z 变）",
    },
    "it": {
        "label": "（线性）离子阱（实验性）",
        "dwell_min_ms": 10.0,
        "res_da": 0.7,
        "n_min": 15,
        "res_note": "低分辨阱，常规扫描 ~0.7 Da（FWHM，恒定）",
    },
    "qtrap": {
        "label": "Q离子阱 QTRAP（实验性）",
        "dwell_min_ms": 2.0,
        "res_da": 0.7,
        "n_min": 15,
        "res_note": "四极杆段单位分辨率 ~0.7 Da（FWHM，恒定）",
    },
    "qtof": {
        "label": "四极杆-飞行时间 Q-TOF（实验性）",
        "dwell_min_ms": 0.0,
        "res_ppm": 20.0,
        "n_min": 15,
        "res_note": "高分辨：标称 FWHM ≈ 20 ppm × m/z（随 m/z 等比；实测常宽 2–5 倍，请填实测值）",
    },
}


def _group_mass_check(group, device, res_params):
    """组内母离子最近邻的质量可分辨判据。无母离子数据时返回 None。"""
    from .mass_resolution import check_list
    precs = [m.get("precursor") for m in (group.get("members") or [])]
    return check_list(precs, device, res_params)


def evaluate_group(group, device, peak_width_s, dwell_ms,
                   delay_q1_ms=2.0, delay_q3_ms=2.0, overhead_ms=2.0,
                   channels_per_compound=2, res_params=None):
    """对单个分组做质谱核算。

    MRM cycle 物理模型（N₃ = 每个母离子对应的子离子通道数）：
      N₁ = 化合物数（母离子数），总通道数 = N₁ × N₃
      cycle = N₁×N₃×dwell
            + N₁×(N₃-1)×delay_q3             （同化合物内子离子间切 Q3，按母离子分别加总）
            + (N₁-1)×max(delay_q1,delay_q3)  （跨化合物 Q1 与 Q3 同时动作、并行稳定）
            + overhead

    res_params: 质量分辨率参数（谷值判据 / 峰宽来源）。传入时附带组内母离子
                最近邻的 Δm / FWHM / k / 谷值判定，用于验证分组是否已满足
                质量可分辨约束（输出键 "mass"，无母离子数据时为 None）。

    返回:
      {
        "n_compounds", "n_transitions", "cycle_ms", "n_points",
        "ok_points", "ok_dwell", "verdict", "suggestion", "mass"
      }
    """
    n_compounds = group["n_compounds"]
    n_transitions = n_compounds * channels_per_compound
    n_q3_switch = n_compounds * (channels_per_compound - 1)
    n_q1_switch = n_compounds - 1
    delay_switch_ms = max(delay_q1_ms, delay_q3_ms)
    cycle_ms = (n_transitions * dwell_ms
                + n_q3_switch * delay_q3_ms
                + n_q1_switch * delay_switch_ms
                + overhead_ms)
    n_points = (peak_width_s * 1000.0) / cycle_ms if cycle_ms > 0 else float("inf")

    ok_points = n_points >= device["n_min"]
    ok_dwell = dwell_ms >= device["dwell_min_ms"]

    if ok_points and ok_dwell:
        verdict = "pass"
        suggestion = ""
    elif not ok_dwell:
        verdict = "fail_dwell"
        suggestion = f"dwell {dwell_ms}ms < 设备下限 {device['dwell_min_ms']}ms，该设备做不到"
    else:
        verdict = "fail_points"
        cycle_max_ms = peak_width_s * 1000.0 / device["n_min"]
        per_compound = channels_per_compound * dwell_ms + (channels_per_compound - 1) * delay_q3_ms
        max_c = max(int((cycle_max_ms - overhead_ms + delay_switch_ms)
                        / (per_compound + delay_switch_ms)), 1)
        suggestion = f"点数 {n_points:.1f} < {device['n_min']}，建议每组不超过 {max_c} 个化合物"

    return {
        "n_compounds": n_compounds,
        "n_transitions": n_transitions,
        "cycle_ms": round(cycle_ms, 1),
        "n_points": round(n_points, 1),
        "ok_points": ok_points,
        "ok_dwell": ok_dwell,
        "verdict": verdict,
        "suggestion": suggestion,
        "mass": _group_mass_check(group, device, res_params) if res_params is not None else None,
    }


def evaluate_all(groups, instrument="qqq", peak_width_s=6.0, dwell_ms=20.0,
                 delay_q1_ms=2.0, delay_q3_ms=2.0, overhead_ms=2.0,
                 channels_per_compound=2, res_params=None):
    """对所有分组做质谱核算。res_params 见 evaluate_group。"""
    dev = DEVICES.get(instrument, DEVICES["qqq"])
    results = []
    for g in groups:
        ev = evaluate_group(g, dev, peak_width_s, dwell_ms,
                            delay_q1_ms, delay_q3_ms, overhead_ms,
                            channels_per_compound, res_params)
        results.append({"group": g, "eval": ev, "device": dev["label"]})

    n_total = sum(g["n_compounds"] for g in groups)
    n_pass = sum(1 for r in results if r["eval"]["verdict"] == "pass")

    without_n1 = n_total
    without_n3 = n_total * channels_per_compound
    without_q3_sw = without_n1 * (channels_per_compound - 1)
    without_q1_sw = without_n1 - 1
    without_cycle = (without_n3 * dwell_ms
                     + without_q3_sw * delay_q3_ms
                     + without_q1_sw * max(delay_q1_ms, delay_q3_ms)
                     + overhead_ms)
    without_points = (peak_width_s * 1000.0) / without_cycle

    return {
        "device": dev["label"],
        "instrument": instrument,
        "groups": results,
        "summary": {
            "n_total_compounds": n_total,
            "n_groups": len(groups),
            "n_pass": n_pass,
            "n_fail": len(groups) - n_pass,
            "without_grouping": {
                "n_transitions": without_n3,
                "cycle_ms": round(without_cycle, 1),
                "n_points": round(without_points, 1),
                "would_fail": without_points < dev["n_min"],
            },
        },
    }


if __name__ == "__main__":
    from .separator import run_separation

    compounds = [
        {"name": f"化合物{i}", "formula": f"C{i}H{2*i}O{i//3+1}"}
        for i in range(1, 21)
    ]
    sep = run_separation(compounds, "c18", "capacity",
                         peak_width_s=6.0, dwell_ms=20.0)
    ms = evaluate_all(sep["groups"], "qqq", 6.0, 20.0)

    print(f"设备: {ms['device']}")
    print(f"化合物总数: {ms['summary']['n_total_compounds']}")
    print(f"分组数: {ms['summary']['n_groups']}  通过: {ms['summary']['n_pass']}  不通过: {ms['summary']['n_fail']}")
    print()
    for r in ms["groups"]:
        ev = r["eval"]
        mark = "✓" if ev["verdict"] == "pass" else "✗"
    print(f"  {mark} 组{r['group']['group_id']} ({ev['n_compounds']}个, {ev['n_transitions']}通道) "
          f"cycle={ev['cycle_ms']}ms 点数={ev['n_points']} {ev['suggestion']}")
    print()
    wg = ms["summary"]["without_grouping"]
    print(f"不分组: {wg['n_transitions']}通道 cycle={wg['cycle_ms']}ms 点数={wg['n_points']} "
          f"{'→ 漏检!' if wg['would_fail'] else '→ 达标'}")