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

"""色谱分离模拟 + 分组策略（纯标准库，零依赖）。

链路：
  化合物列表(名称+分子式) → 排序键 → 排序 → 归一化位置 → 分组

柱子类型决定排序键与方向（见 COLUMN_TYPES）：
  反相 C18/C8/C4 ：键 = 极性指数，极性大先出（降序 → 位置小）
  HILIC / 正相   ：键 = 极性指数，极性小先出（升序 → 位置小）
  SEC（GPC/GFC） ：键 = 分子量，  大分子先出（降序 → 位置小）

C4/C8/C18 是同一保留机制（疏水分配），只是烷基链长短不同、疏水性强弱递变，
排序方向一致，因此合并为同一个模式；只有机制不同的柱型才单列。

分组策略：
  threshold — 按百分比阈值分组（相邻位置差 < 阈值分同组）
  capacity  — 按质谱能力反推每组最大化合物数，贪心填充

两条硬约束（两种策略都生效）：
  1. 出峰位置：约束的**几何对象随策略而异**，上限由调用方传入。
       threshold —— 相邻位置差 >= threshold_pct 即开新组。
                    位置差在此**就是分组定义本身**，1~20 是它的正常量纲。
       capacity  —— 组内首尾跨度 > max_span_pct 即开新组。
                    跨度是**整体量**（一组总共铺多宽），相邻差是**局部量**（逐对间隙）。
                    局部量容得下「两端各站一个、中间空着」的组；整体量才是
                    「出峰隔太远不该塞进同一采集窗口」的本意。实测同一批数据：
                    相邻差上限 50 给出 1 组（首尾跨满 100%），跨度上限 50 给出 2 组。
     位置经批次内 min-max 归一化，全批跨度恒为 100%，故跨度上限天然落在 (0, 100]，
     取 100 等于不干预。它若低于临界值（见 capacity_span_critical，即该批次实际分组
     中各组的最大跨度）就会抢在容量之前拆组，此时分组由出峰位置而非质谱能力决定——
     该临界值随 run_separation 一并回显。
     **三个上限都是 run_separation 的形参，不是函数签名里的隐式默认值**——
     隐式默认值会让参数脱离配置推动链（PARAM_SPEC → DEFAULT_CONFIG → config.json → 请求），
     在界面上既看不见也改不了，只在拆组时以「阈值 20%」的形式泄漏给使用者。
     split_reasons 的 gap / span 记录带 op 字段，如实标注本次比较用的是 >= 还是 >，
     界面据此渲染，不靠猜。
  2. 质量可分辨：同组内任意两个母离子的 Δm 必须 ≥ k×FWHM，否则同一采集窗口内
     质量峰糊在一起——这类冲突必须拆组，不能只靠事后报警。
     conflict_fn(a_prec, b_prec) 返回冲突 dict（不可共组）或 None（可共组）。
     返回的冲突记录会带 pos_gap（位置差%），供使用者判断该冲突能否靠时间调度解决：
     位置差接近 0 的两条离子对是共洗脱的，拆组也无济于事，属不可解冲突。

每个分组边界都会带一条 split_reasons 记录（mass / gap / capacity），供界面回显
「这个组是为什么开的」——不做无声拆组。

配置自检（_self_check）不静默通过：分子式缺失、排序键超出适用域、母离子多电荷
三类问题一律产出显式记录。
"""
from .polarity import polarity_index, parse_formula, describe_polarity
from .mass_calc import monoisotopic_mass, FormulaError

# 极性指数的适用域上限（Da）。超过此值后极性指数对出峰顺序失去分辨力——
# 高分子量的保留由序列/二级结构/离子对试剂决定，不由元素计数决定。
PI_DOMAIN_MAX_DA = 1500.0

# SEC 分离区的下沿（Da）。小分子全部进入孔内，保留体积趋同，几乎没有分离度。
SEC_DOMAIN_MIN_DA = 500.0

# 多电荷判据余量（Da）：单电荷的 m/z 至少是 M+1，低于 M-2 只能是 z>1。
MULTICHARGE_MARGIN_DA = 2.0

COLUMN_TYPES = {
    "c18": {
        "label": "反相 C18/C8/C4", "sort": "desc", "key": "pi",
        "key_label": "极性指数",
        "note": "极性大先出，极性小后出（C4/C8/C18 同为疏水分配，排序方向一致）",
    },
    "hilic": {
        "label": "HILIC", "sort": "asc", "key": "pi",
        "key_label": "极性指数",
        "note": "极性小先出，极性大后出（保留机制复杂，排序仅供参考）",
    },
    "normal": {
        "label": "正相（硅胶/氨基）", "sort": "asc", "key": "pi",
        "key_label": "极性指数",
        "note": "极性小先出，极性大后出",
    },
    "sec": {
        "label": "体积排阻 SEC（GPC/GFC）", "sort": "desc", "key": "mass",
        "key_label": "分子量 (Da)",
        "note": "按流体力学体积排序：大分子进不了孔先出、小分子后出。"
                "保留由分子尺寸决定，与极性无关——极性指数不参与本模式排序",
    },
}


def _normalize(values, direction):
    """归一化到 0-100。direction='desc' 表示值大的位置小（先出）。"""
    if not values:
        return []
    vmin, vmax = min(values), max(values)
    span = vmax - vmin
    result = []
    for v in values:
        if span == 0:
            pos = 50.0
        elif direction == "desc":
            pos = (vmax - v) / span * 100.0
        else:
            pos = (v - vmin) / span * 100.0
        result.append(round(pos, 2))
    return result


def _formula_mass(formula):
    """由分子式算单同位素质量（Da）。分子式缺失或非法时返回 None。

    返回 None 表示「无法计算」，不补 0——补 0 会让该化合物静默排到序列一端，
    界面上看不出异常。None 会被 _self_check 转成显式告警。
    """
    if not formula:
        return None
    try:
        return monoisotopic_mass(formula)
    except (FormulaError, ValueError, KeyError):
        return None


def describe_size(mass):
    """分子量的通俗描述（SEC 排序键对应的口径）。"""
    if mass is None:
        return "分子量未知（无法排序）"
    if mass < SEC_DOMAIN_MIN_DA:
        return "小分子（SEC 下全进孔，分离度低）"
    if mass < PI_DOMAIN_MAX_DA:
        return "中等分子量"
    if mass < 5000.0:
        return "大分子"
    return "生物大分子"


def separate(compounds, column_type="c18"):
    """色谱分离模拟。

    compounds: [{"name": "...", "formula": "C6H12O6"}, ...]
    column_type: "c18" / "hilic" / "normal" / "sec"

    母离子 / 子离子 m/z 若存在会被原样带下去，供质量可分辨判据与通量核算使用。

    返回 (items, col)。items 按出峰顺序排列，每个化合物带：
      idx            化合物在输入列表中的原始序号（稳定标识——名称可能重复，
                     排序后位置也会变，只有它唯一）
      position_pct   归一化出峰位置（0 = 最先出，100 = 最后出）
      sort_key       本模式使用的排序键名（"pi" / "mass"）
      sort_value     排序键取值
      sort_value_valid  排序键是否由有效分子式算出
      pi / mass      两个键值都保留（供界面同时展示与自检）
      polarity_desc  与当前排序键口径一致的通俗描述
    """
    col = COLUMN_TYPES.get(column_type, COLUMN_TYPES["c18"])
    direction = col["sort"]
    key = col.get("key", "pi")

    items = []
    for idx, c in enumerate(compounds):
        name = c.get("name", "")
        formula = c.get("formula", "")
        mass = _formula_mass(formula)
        pi = polarity_index(formula) if formula else 0.0
        it = {
            "idx": idx,
            "name": name,
            "formula": formula,
            "pi": pi,
            "mass": mass,
            "pi_valid": bool(formula),
        }
        for k in ("precursor", "product_quant", "product_qual"):
            if c.get(k) is not None:
                it[k] = c[k]
        items.append(it)

    # 排序键取值。分子式缺失/非法时用 0.0 保证排序不崩，但该行会被标注为
    # 占位值并由 _self_check 产出告警——不做静默处理。
    def _rank(it):
        v = it["pi"] if key == "pi" else it["mass"]
        return 0.0 if v is None else float(v)

    ranks = [_rank(it) for it in items]
    positions = _normalize(ranks, direction)
    for i, it in enumerate(items):
        it["position_pct"] = positions[i]
        it["sort_key"] = key
        it["sort_value"] = ranks[i]
        it["sort_value_valid"] = bool(it["pi_valid"]) if key == "pi" else it["mass"] is not None
        it["polarity_desc"] = (describe_polarity(it["pi"]) if key == "pi"
                               else describe_size(it["mass"]))

    items.sort(key=lambda x: x["position_pct"])
    return items, col


def _self_check(items, col, key):
    """配置 ↔ 数据域一致性自检，返回告警列表（空 = 无问题）。

    自检的意义是把「静默地用错模型」变成「显式告诉使用者」：
      missing_formula 分子式缺失/非法 → 排序键是占位值，出峰位置不代表真实顺序
      out_of_domain   极性指数用于高分子量化合物 → 排序无分辨力（或柱子选错）
      sec_under_domain SEC 用于小分子 → 全进孔，几乎没有分离度
      multi_charge    母离子 m/z 低于中性质量 → z>1，单电荷阈值不适用
    """
    warn = []
    label = lambda it: it.get("name") or "(未命名)"

    # 1. 分子式缺失/非法
    bad = [label(it) for it in items if it.get("mass") is None]
    if bad:
        show = "、".join(bad[:5]) + ("…" if len(bad) > 5 else "")
        warn.append({
            "level": "warn",
            "code": "missing_formula",
            "message": f"{len(bad)} 个化合物缺分子式或分子式非法（{show}），"
                       f"无法计算分子量与极性指数，其出峰位置为占位值、不代表真实顺序。",
        })

    # 2. 排序键适用域
    if key == "pi":
        over = [it for it in items
                if it["mass"] is not None and it["mass"] > PI_DOMAIN_MAX_DA]
        if over:
            show = "、".join(f"{label(it)}(M {it['mass']:.0f})" for it in over[:5])
            show += "…" if len(over) > 5 else ""
            warn.append({
                "level": "warn",
                "code": "out_of_domain",
                "message": f"{len(over)} 个化合物分子量 > {PI_DOMAIN_MAX_DA:.0f} Da（{show}）。"
                           f"极性指数只由元素计数决定，对高分子量化合物没有分辨力"
                           f"（保留实际由序列/结构决定），排序结果不可用——"
                           f"请确认柱型是否选错，或改用实测保留时间排序。",
            })
    else:
        small = [it for it in items
                 if it["mass"] is not None and it["mass"] < SEC_DOMAIN_MIN_DA]
        if small:
            show = "、".join(label(it) for it in small[:5])
            show += "…" if len(small) > 5 else ""
            warn.append({
                "level": "warn",
                "code": "sec_under_domain",
                "message": f"{len(small)} 个化合物分子量 < {SEC_DOMAIN_MIN_DA:.0f} Da（{show}）。"
                           f"SEC 下小分子全部进入孔内、保留体积趋同，几乎没有分离度，"
                           f"分组结果没有实际意义。",
            })

    # 3. 多电荷
    multi = []
    for it in items:
        if it["mass"] is None or not it.get("precursor"):
            continue
        try:
            mz = float(it["precursor"])
        except (TypeError, ValueError):
            continue
        if mz < it["mass"] - MULTICHARGE_MARGIN_DA:
            multi.append(f"{label(it)}（m/z {mz:g} < M {it['mass']:.2f}）")
    if multi:
        show = "；".join(multi[:4]) + ("…" if len(multi) > 4 else "")
        warn.append({
            "level": "warn",
            "code": "multi_charge",
            "message": f"{len(multi)} 个化合物的母离子 m/z 低于其中性质量（{show}），"
                       f"说明电荷数 z > 1。质量可分辨判据按实测 m/z 之差计算，"
                       f"z>1 时 Δm = ΔM/z 被压缩，单电荷下的阈值不再适用。",
        })

    return warn


def _mass_conflicts(current, item, conflict_fn):
    """item 与 current 内任一成员的质量冲突列表（空列表 = 可共组）。"""
    if conflict_fn is None or not item.get("precursor"):
        return []
    out = []
    for m in current:
        if not m.get("precursor"):
            continue
        c = conflict_fn(m["precursor"], item["precursor"])
        if c:
            rec = dict(c)
            rec["a_name"] = m.get("name")
            rec["b_name"] = item.get("name")
            rec["pos_gap"] = round(abs(item["position_pct"] - m["position_pct"]), 2)
            out.append(rec)
    return out


def _split_record(prev, cur, reason, detail, group_no):
    """记录一次分组边界：为什么在这里开新组。"""
    return {
        "after_group": group_no,
        "after": prev.get("name"),
        "before": cur.get("name"),
        "reason": reason,
        "detail": detail,
    }


def group_by_threshold(items, threshold_pct=5.0, conflict_fn=None):
    """模式A：按百分比阈值分组。相邻位置差 < 阈值分同组（质量冲突强制拆组）。

    返回 (groups, conflicts, splits)。
    """
    if not items:
        return [], [], []
    groups = []
    conflicts = []
    splits = []
    current = [items[0]]
    for i in range(1, len(items)):
        prev, cur = items[i - 1], items[i]
        gap = cur["position_pct"] - prev["position_pct"]
        hit = _mass_conflicts(current, cur, conflict_fn)
        if hit:
            conflicts.extend(hit)
            splits.append(_split_record(
                prev, cur, "mass",
                {"dm": hit[0].get("dm"), "need_da": hit[0].get("need_da"),
                 "a": hit[0].get("a_name"), "b": hit[0].get("b_name")},
                len(groups) + 1))
            groups.append(current)
            current = [cur]
        elif gap >= threshold_pct:
            splits.append(_split_record(
                prev, cur, "gap",
                {"gap": round(gap, 2), "threshold": threshold_pct, "op": ">="},
                len(groups) + 1))
            groups.append(current)
            current = [cur]
        else:
            current.append(cur)
    groups.append(current)
    return _format_groups(groups), conflicts, splits


def group_by_capacity(items, max_compounds, max_span_pct=100.0, conflict_fn=None):
    """模式B：按质谱能力反推每组最大化合物数，贪心填充（质量冲突强制拆组）。

    max_compounds: 每组最多容纳几个化合物（由质谱 cycle 反推）
    max_span_pct:  组内跨度上限(%)——一组化合物首尾铺开的宽度超过此值则强制开新组。
                   位置经批次内 min-max 归一化，全批跨度恒为 100%，故取值天然落在
                   (0, 100]，100 即不干预。由 run_separation 从参数表透传，取值来自
                   配置推动链；签名默认值仅供本模块自测直接调用，应用路径不会走到。

    判据用跨度而非相邻差：相邻差是局部量，逐对比较，容得下「两端各站一个、
    中间空着」的组；跨度是整体量，直接约束这个采集窗口铺多宽。实测同一批数据
    （位置 [0, 22.8, 72.2, 73.3, 74.2, 83.5, 88.0, 100.0]）：相邻差上限 50 给出
    1 组（所有 gap ≤ 49.45，合格），跨度上限 50 给出 2 组（第一组跨到 72.2 已超限）。

    返回 (groups, conflicts, splits)。
    """
    if not items:
        return [], [], []
    groups = []
    conflicts = []
    splits = []
    current = [items[0]]
    for i in range(1, len(items)):
        prev, cur = items[i - 1], items[i]
        head = current[0]
        span = cur["position_pct"] - head["position_pct"]
        hit = _mass_conflicts(current, cur, conflict_fn)
        # 拆分优先级：质量冲突 > 容量上限 > 组内跨度
        if hit:
            conflicts.extend(hit)
            splits.append(_split_record(
                prev, cur, "mass",
                {"dm": hit[0].get("dm"), "need_da": hit[0].get("need_da"),
                 "a": hit[0].get("a_name"), "b": hit[0].get("b_name")},
                len(groups) + 1))
            groups.append(current)
            current = [cur]
        elif len(current) >= max_compounds:
            splits.append(_split_record(
                prev, cur, "capacity", {"max_compounds": max_compounds},
                len(groups) + 1))
            groups.append(current)
            current = [cur]
        elif span > max_span_pct:
            splits.append(_split_record(
                prev, cur, "span",
                {"span": round(span, 2), "threshold": max_span_pct, "op": ">",
                 "head": head.get("name"), "tail": prev.get("name")},
                len(groups) + 1))
            groups.append(current)
            current = [cur]
        else:
            current.append(cur)
    groups.append(current)
    return _format_groups(groups), conflicts, splits


def _format_groups(groups):
    """格式化分组结果。"""
    result = []
    for i, grp in enumerate(groups):
        positions = [c["position_pct"] for c in grp]
        result.append({
            "group_id": i + 1,
            "range": [min(positions), max(positions)],
            "compounds": [c["name"] for c in grp],
            "n_compounds": len(grp),
            "members": grp,
        })
    return result


def max_compounds_per_group(peak_width_s, n_min, dwell_ms, delay_q1_ms, delay_q3_ms,
                            overhead_ms, channels_per_compound=2):
    """由质谱能力反推每组最多容纳几个化合物。

    cycle = N₁×N₃×dwell + N₁×(N₃-1)×delay_q3 + (N₁-1)×max(delay_q1,delay_q3) + overhead ≤ peak_width/n_min
    N₁ = 化合物数, N₃ = 每母离子对应的子离子通道数（总通道数 = N₁×N₃）
    同化合物内子离子间切 Q3（按母离子分别加总）；跨化合物时 Q1 与 Q3 同时动作、并行稳定，取较慢者。
    => N₁ × [channels×dwell + (channels-1)×delay_q3 + max(delay_q1,delay_q3)] + overhead - max(...) ≤ cycle_max
    => N₁ ≤ (cycle_max - overhead + max(delay_q1,delay_q3)) / [channels×dwell + (channels-1)×delay_q3 + max(delay_q1,delay_q3)]
    """
    cycle_max_ms = peak_width_s * 1000.0 / n_min
    delay_switch_ms = max(delay_q1_ms, delay_q3_ms)
    denom = (channels_per_compound * dwell_ms
             + (channels_per_compound - 1) * delay_q3_ms
             + delay_switch_ms)
    max_c = int((cycle_max_ms - overhead_ms + delay_switch_ms) / denom)
    return max(max_c, 1)


def capacity_span_critical(items, max_compounds, conflict_fn=None):
    """完全不触发跨度判据所需的最小跨度上限(%)。

    做法：以「无跨度约束」跑一次分组，取各组实际跨度的最大值。跨度上限不低于它，
    该判据一次也不会触发，分组完全由容量与质量判据决定。

    不用「均匀分布下 100×(c-1)/(N-1)」这类解析估计——真实峰位是聚集的，
    估计值偏乐观：实测 16 个化合物、容量上限 9，解析估计给 53.3%，但实际要
    68.5% 才完全不触发。估计值会让人以为设到 53.3 就安全，其实仍在抢戏。

    该值是**提示**而非硬约束：它随批次的实际分布变化，因此不适合当作界面上
    固定的 min——固定 min 会像早期那个「位置差上限从 1 开始」一样，看似有范围实则无意义。
    """
    if not items:
        return 0.0
    groups, _, _ = group_by_capacity(
        items, max_compounds, max_span_pct=float("inf"), conflict_fn=conflict_fn)
    if not groups:
        return 0.0
    return round(max(g["range"][1] - g["range"][0] for g in groups), 1)


def run_separation(compounds, column_type="c18", group_mode="capacity",
                   threshold_pct=5.0, max_span_pct=100.0, peak_width_s=6.0, n_min=15,
                   dwell_ms=20.0, delay_q1_ms=2.0, delay_q3_ms=2.0, overhead_ms=2.0,
                   channels_per_compound=2, conflict_fn=None):
    """完整分离 + 分组流程。

    threshold_pct: threshold 模式的相邻位置差阈值（gap >= 此值即开新组）
    max_span_pct:  capacity  模式的组内首尾跨度上限（span > 此值即开新组）

    conflict_fn: 可选，`(prec_a, prec_b) -> 冲突dict | None`。传入时启用
                 「质量可分辨」硬约束：同组内任意两母离子必须可分辨。

    返回:
      {
        "items": [...],        # 按出峰顺序排列
        "column": {...},       # 柱子信息（含 key / key_label）
        "groups": [...],       # 分组结果
        "group_mode": "...",
        "max_compounds_per_group": int,
        "max_span_pct": float,       # capacity 模式生效的跨度上限（threshold 模式为 None）
        "span_critical_pct": float,  # 完全不触发跨度判据所需的最小跨度上限
        "mass_conflicts": [...],     # 因质量不可分辨而强制拆组的记录
        "split_reasons": [...],      # 开新组原因（mass/span/capacity；threshold 模式为 mass/gap）
        "warnings": [...],           # 配置 ↔ 数据域一致性自检结果
      }
    """
    items, col = separate(compounds, column_type)
    key = col.get("key", "pi")
    warnings = _self_check(items, col, key)

    if group_mode == "threshold":
        groups, conflicts, splits = group_by_threshold(items, threshold_pct, conflict_fn)
        max_c = None
        span_cap = None
        span_critical = None
    else:
        max_c = max_compounds_per_group(
            peak_width_s, n_min, dwell_ms, delay_q1_ms, delay_q3_ms, overhead_ms,
            channels_per_compound
        )
        groups, conflicts, splits = group_by_capacity(
            items, max_c, max_span_pct=max_span_pct, conflict_fn=conflict_fn)
        span_cap = max_span_pct
        span_critical = capacity_span_critical(items, max_c, conflict_fn)

    return {
        "items": items,
        "column": {
            "type": column_type,
            "label": col["label"],
            "note": col["note"],
            "key": key,
            "key_label": col.get("key_label", "极性指数"),
        },
        "groups": groups,
        "group_mode": group_mode,
        "max_compounds_per_group": max_c,
        "threshold_pct": threshold_pct if group_mode == "threshold" else None,
        "max_span_pct": span_cap,
        "span_critical_pct": span_critical,
        "mass_conflicts": conflicts,
        "split_reasons": splits,
        "warnings": warnings,
    }


if __name__ == "__main__":
    test_compounds = [
        {"name": "葡萄糖", "formula": "C6H12O6"},
        {"name": "咖啡因", "formula": "C8H10N4O2"},
        {"name": "乙醇", "formula": "C2H6O"},
        {"name": "丙酮", "formula": "C3H6O"},
        {"name": "正己烷", "formula": "C6H14"},
        {"name": "苯", "formula": "C6H6"},
        {"name": "TNT", "formula": "C7H5N3O6"},
        {"name": "溴萘", "formula": "C10H7Br"},
        {"name": "水", "formula": "H2O"},
        {"name": "氯乙烷", "formula": "C2H5Cl"},
    ]

    print("=" * 70)
    print("反相 C18 分组（按质谱能力反推）")
    print("=" * 70)
    result = run_separation(test_compounds, column_type="c18",
                            group_mode="capacity",
                            peak_width_s=6.0, dwell_ms=20.0)
    print(f"柱子: {result['column']['label']}   排序键: {result['column']['key_label']}")
    print(f"每组最多: {result['max_compounds_per_group']} 个化合物")
    print()
    for it in result["items"]:
        print(f"  {it['position_pct']:>6.1f}%  {it['name']:<8} PI={it['pi']:.4f}  {it['polarity_desc']}")
    print()
    for g in result["groups"]:
        print(f"  组{g['group_id']} [{g['range'][0]:.1f}-{g['range'][1]:.1f}%] "
              f"({g['n_compounds']}个): {', '.join(g['compounds'])}")
    print()
    print("分组边界原因:")
    for s in result["split_reasons"]:
        print(f"  组{s['after_group']} 后 → {s['reason']}  {s['detail']}")
    print(f"自检告警: {result['warnings'] or '无'}")

    print()
    print("=" * 70)
    print("HILIC 分组（排序方向相反）")
    print("=" * 70)
    result2 = run_separation(test_compounds, column_type="hilic",
                             group_mode="threshold", threshold_pct=10.0)
    print(f"柱子: {result2['column']['label']}")
    print()
    for it in result2["items"]:
        print(f"  {it['position_pct']:>6.1f}%  {it['name']:<8} PI={it['pi']:.4f}")
    print()
    for g in result2["groups"]:
        print(f"  组{g['group_id']} [{g['range'][0]:.1f}-{g['range'][1]:.1f}%] "
              f"({g['n_compounds']}个): {', '.join(g['compounds'])}")

    print()
    print("=" * 70)
    print("SEC 分组（排序键 = 分子量，含超域自检）")
    print("=" * 70)
    sec_compounds = [
        {"name": "葡萄糖", "formula": "C6H12O6"},
        {"name": "环孢素A", "formula": "C62H111N11O12"},
        {"name": "万古霉素", "formula": "C66H75Cl2N9O24"},
        {"name": "胰岛素", "formula": "C257H383N65O77S6"},
    ]
    result3 = run_separation(sec_compounds, column_type="sec", group_mode="threshold",
                             threshold_pct=20.0)
    print(f"柱子: {result3['column']['label']}   排序键: {result3['column']['key_label']}")
    for it in result3["items"]:
        print(f"  {it['position_pct']:>6.1f}%  {it['name']:<10} M={it['mass']:>9.2f}  {it['polarity_desc']}")
    print()
    for g in result3["groups"]:
        print(f"  组{g['group_id']} ({g['n_compounds']}个): {', '.join(g['compounds'])}")
    print()
    print("自检告警:")
    for w in result3["warnings"]:
        print(f"  [{w['code']}] {w['message']}")
