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

"""精确质量计算与离子加合物解析（纯标准库，零依赖、零 LLM）。

职责边界
--------
本模块是「化学式 ↔ 母离子 m/z」双向换算的**唯一入口**：

  正向 ion_mz()      : 化学式 + 加合物 + 电荷数  ->  理论 m/z
  反向 resolve_ion() : (化学式, m/z)             ->  加合物判定（三档）

反向为什么可判定
----------------
m/z = (M + Δ) / z  （Δ = 加合物增质量，z = 电荷数）

单凭一个 m/z 解不出 (Δ, z) —— 这是欠定问题。但候选集是**离散且有限**的
（常见加合物 20 项 × z ∈ {1,2,3}）。把每个候选代入算理论 m/z，与实测比对：

  唯一命中  ->  判定成立，且化学式与 m/z 互相验证
  多个命中  ->  列出候选交使用者确认，**不自动挑**
  零命中    ->  化学式或 m/z 至少有一个错（最有价值的信号）

候选间的质量间隔决定了唯一性：同极性内 Δ 的最小间隔 = 22.989218 - 18.033823
= 4.955 Da（[M+NH4]+ 与 [M+Na]+）；跨极性最近对 = [M+NH4]+ 与 [M+F]-，间隔
0.965 Da。因此**声明电离极性后，标称分辨下判定唯一**（唯一性扫描见测试）。

术语区分
--------
ion_mode   : 电离极性（ESI+ / ESI-），**方法表的显式参数**
polarity   : 本项目 polarity.py 里指色谱极性（疏水性强弱），与本模块无关

加合物是方法开发者的隐含选择，使用者无需知道；电离极性是方法本身的明示
参数。二者不可混为一谈。
"""
import re

# ---------------------------------------------------------------------------
# 元素质量表：单同位素质量（最丰同位素，用于高分辨精确质量）
#              平均原子质量（IUPAC 常规原子量，用于平均分子量）
# 单位 Da。质子质量与电子质量单列。
# ---------------------------------------------------------------------------
PROTON = 1.007276466
ELECTRON = 0.000548580

# element: (monoisotopic, average)
ELEMENTS = {
    "H": (1.0078250319, 1.00794),
    "D": (2.0141017779, 2.014101778),
    "Li": (7.01600455, 6.94),
    "B": (11.0093055, 10.81),
    "C": (12.0000000, 12.0107),
    "N": (14.0030740052, 14.0067),
    "O": (15.9949146221, 15.9994),
    "F": (18.99840320, 18.9984032),
    "Na": (22.98976928, 22.98976928),
    "Mg": (23.9850419, 24.3050),
    "Al": (26.98153863, 26.9815386),
    "Si": (27.9769265327, 28.0855),
    "P": (30.97376151, 30.973762),
    "S": (31.97207069, 32.065),
    "Cl": (34.96885271, 35.453),
    "K": (38.9637069, 39.0983),
    "Ca": (39.9625912, 40.078),
    "Mn": (54.9380496, 54.938049),
    "Fe": (55.9349375, 55.845),
    "Co": (58.9332002, 58.933200),
    "Ni": (57.9353479, 58.6934),
    "Cu": (62.9295975, 63.546),
    "Zn": (63.9291422, 65.38),
    "As": (74.9215964, 74.92160),
    "Se": (79.9165218, 78.96),
    "Br": (78.9183376, 79.904),
    "Mo": (97.9054078, 95.94),
    "Ag": (106.9050916, 107.8682),
    "Sn": (119.9021966, 118.710),
    "I": (126.9044680, 126.90447),
    "Ba": (137.9052472, 137.327),
    "W": (183.9509326, 183.84),
    "Pb": (207.9766525, 207.2),
}

# ---------------------------------------------------------------------------
# 加合物表：(标签, 增质量总和 Δ, 电荷数 |z|)
# m/z = (M + Δ) / |z|
#
# Δ 已按「离子」口径计入电子得失（即 Δ = 中性加合基团质量 ∓ 电子质量），
# 与 XCMS / adduct 计算器的通行取值一致，可直接与文献 m/z 比对。
# ---------------------------------------------------------------------------
ADDUCTS = {
    "positive": [
        ("[M+H]+",         1.007276, 1),
        ("[M+NH4]+",      18.033823, 1),
        ("[M+Na]+",       22.989218, 1),
        ("[M+CH3OH+H]+",  33.033489, 1),
        ("[M+K]+",        38.963158, 1),
        ("[M+ACN+H]+",    42.033823, 1),
        ("[M+2Na-H]+",    44.971160, 1),
        ("[M+ACN+Na]+",   63.993089, 1),
        ("[M+2K-H]+",     76.919040, 1),
        ("[M+H+Na]2+",    23.996494, 2),
        ("[M+2H]2+",       2.014552, 2),
        ("[M+2Na]2+",     45.978436, 2),
        ("[M+3H]3+",       3.021828, 3),
    ],
    "negative": [
        ("[M-H]-",        -1.007276, 1),
        ("[M+F]-",        18.998403, 1),
        ("[M+Cl]-",       34.969402, 1),
        ("[M+HCOO]-",     44.998201, 1),
        ("[M+CH3COO]-",   59.013851, 1),
        ("[M+TFA-H]-",   112.985587, 1),
        ("[M-2H]2-",      -2.014552, 2),
    ],
}

ION_MODES = ("positive", "negative")

# 设备默认容差：高分辨走 ppm，标称分辨走 Da
_DEVICE_TOL = {
    "qtof": ("ppm", 20.0),
    "qqq": ("da", 0.5),
    "it": ("da", 0.5),
    "qtrap": ("da", 0.5),
}

# ---------------------------------------------------------------------------
# 分子式解析（严格模式：不认识的元素直接报错，不做静默跳过）
# ---------------------------------------------------------------------------
_ELEM_TOKEN = re.compile(r"[A-Z][a-z]?")
_CHARGE_RE = re.compile(r"(\d*)\s*([+-])$")


class FormulaError(ValueError):
    """分子式非法。"""


def split_formula(formula: str):
    """拆分多组分分子式（水合物 / 盐 / 加合形式）。

    返回 (parts, charge_hint)：
      parts       : ["C6H12O6", "H2O"] 这样的组分列表
      charge_hint : 原文尾部带电荷标记（如 "2+"/"-"）时返回带符号整数，否则 None

    例: "C6H12O6·H2O" -> (["C6H12O6","H2O"], None)
        "C6H12O6.H2O" -> (["C6H12O6","H2O"], None)
        "C9H8O4"      -> (["C9H8O4"], None)
    """
    s = (formula or "").strip()
    if not s:
        raise FormulaError("分子式为空")
    charge_hint = None
    m = _CHARGE_RE.search(s)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        charge_hint = n if m.group(2) == "+" else -n
        s = s[: m.start()].strip()
    # 归一化各种分隔符：· * × . 以及全角·
    s = s.replace("·", "·")
    parts = [p.strip() for p in re.split(r"[·*×.]", s) if p.strip()]
    if not parts:
        raise FormulaError(f"分子式无法解析: {formula!r}")
    return parts, charge_hint


def _read_number(s, i):
    """读取 [i, ...) 处的下标数字。无数字时返回 1（下标省略）。"""
    j = i
    while j < len(s) and s[j].isdigit():
        j += 1
    if j == i:
        return 1, i
    return int(s[i:j]), j


def _parse_seq(s, i):
    """递归下降解析元素序列，遇 ) 或 ] 返回。返回 (atoms, 新位置)。"""
    atoms = {}
    while i < len(s):
        ch = s[i]
        if ch in ")]":
            return atoms, i
        if ch in "([":
            sub, i = _parse_seq(s, i + 1)
            if i >= len(s) or s[i] not in ")]":
                raise FormulaError(f"括号不配对: {s!r}")
            if not sub:
                raise FormulaError(f"空括号: {s!r}")
            i += 1
            mult, i = _read_number(s, i)
            # 倍数只作用于括号内部，不外溢到括号外的元素
            for k, v in sub.items():
                atoms[k] = atoms.get(k, 0) + v * mult
            continue
        m = _ELEM_TOKEN.match(s, i)
        if not m:
            raise FormulaError(f"分子式含非法字符 {s[i]!r}（原文 {s!r}）")
        elem = m.group(0)
        if elem not in ELEMENTS:
            raise FormulaError(f"未知元素符号: {elem}（分子式 {s!r}）")
        i = m.end()
        cnt, i = _read_number(s, i)
        if cnt < 1:
            raise FormulaError(f"下标必须为正整数: {s!r}")
        atoms[elem] = atoms.get(elem, 0) + cnt
    return atoms, i


def _parse_part(part: str) -> dict:
    """解析单个组分，返回元素计数字典。未知元素 / 非法字符抛 FormulaError。

    支持嵌套括号与括号后的倍数，如 Fe(C2H3O2)3 -> C6H9FeO6。
    """
    if part[:1].isdigit():
        raise FormulaError(f"分子式不应以数字开头: {part!r}（疑似把系数当分子式）")
    atoms, i = _parse_seq(part, 0)
    if i != len(part):
        raise FormulaError(f"括号不配对: {part!r}")
    if not atoms:
        raise FormulaError(f"分子式不含任何元素: {part!r}")
    return atoms


def parse_formula_strict(formula: str) -> dict:
    """严格解析分子式，返回元素计数字典。非法输入抛 FormulaError。

    与 polarity.parse_formula 的区别：这里是**校验型**解析，未知元素、
    非法字符、错位数字一律报错；polarity 那版是**宽容型**，会静默跳过
    不认识的片段（那是"静默失败"的来源），因此本模块不复用它。

    例: "C6H12O6" -> {"C":6,"H":12,"O":6}
        "C6H12O6·H2O" -> {"C":6,"H":14,"O":7}
        "Fe(C2H3O2)3" -> {"Fe":1,"C":6,"H":9,"O":6}
    """
    parts, _charge = split_formula(formula)
    atoms = {}
    for p in parts:
        for k, v in _parse_part(p).items():
            atoms[k] = atoms.get(k, 0) + v
    return atoms


def formula_valid(formula: str):
    """校验分子式，返回 (ok, message)。不抛异常，供 UI / 批量检查使用。"""
    try:
        parse_formula_strict(formula)
    except FormulaError as e:
        return False, str(e)
    return True, ""


def format_formula(atoms: dict) -> str:
    """元素计数字典还原为分子式（C、H 在前，其余按字母序）。"""
    def key(e):
        return (0 if e == "C" else 1 if e == "H" else 2, e)
    out = []
    for e in sorted(atoms, key=key):
        n = atoms[e]
        out.append(e if n == 1 else f"{e}{n}")
    return "".join(out)


# ---------------------------------------------------------------------------
# 质量计算
# ---------------------------------------------------------------------------
def monoisotopic_mass(formula: str) -> float:
    """单同位素质量 M（中性分子，精确质量）。"""
    atoms = parse_formula_strict(formula)
    return sum(ELEMENTS[e][0] * n for e, n in atoms.items())


def average_mass(formula: str) -> float:
    """平均分子量（中性分子）。"""
    atoms = parse_formula_strict(formula)
    return sum(ELEMENTS[e][1] * n for e, n in atoms.items())


def ion_mz(formula: str, adduct: str, ion_mode: str) -> float:
    """正向：由化学式 + 加合物算理论 m/z。

    adduct 必须是 ADDUCTS 中的标签，如 "[M+H]+" / "[M-H]-" / "[M+2H]2+"。
    """
    if ion_mode not in ADDUCTS:
        raise ValueError(f"未知电离极性: {ion_mode!r}（可选 {ION_MODES}）")
    for label, delta, z in ADDUCTS[ion_mode]:
        if label == adduct:
            return (monoisotopic_mass(formula) + delta) / z
    raise ValueError(f"{ion_mode} 模式下无此加合物: {adduct!r}")


def list_adducts(ion_mode: str) -> list:
    """列出某极性下的全部候选加合物标签。"""
    return [a[0] for a in ADDUCTS.get(ion_mode, [])]


# ---------------------------------------------------------------------------
# 反向：加合物判定（三档）
# ---------------------------------------------------------------------------
def resolve_tolerance(device=None, tol_da=None, tol_ppm=None):
    """确定容差口径，返回 ("da"|"ppm", 值)。显式参数优先于设备默认。"""
    if tol_ppm is not None:
        return "ppm", float(tol_ppm)
    if tol_da is not None:
        return "da", float(tol_da)
    return _DEVICE_TOL.get((device or "").lower(), ("da", 0.5))


def _candidate(m_mono, mz, md, label, delta, z, kind):
    theo = (m_mono + delta) / z
    err_da = mz - theo
    err_ppm = err_da / theo * 1e6
    return {
        "ion_mode": md, "adduct": label, "z": z,
        "mz_theoretical": round(theo, 5),
        "err_da": round(err_da, 5),
        "err_ppm": round(err_ppm, 2),
        "abs_err": abs(err_da) if kind == "da" else abs(err_ppm),
    }


def resolve_ion(formula, mz, ion_mode=None, device=None,
                tol_da=None, tol_ppm=None, max_z=3):
    """反向：(化学式, m/z) -> 加合物判定。

    参数
    ----
    ion_mode : "positive" / "negative" / None。None 表示未知，两侧都试，
               并报告跨极性歧义风险。
    device   : 设备型号（qqq/it/qtrap/qtof），用于取默认容差。
    max_z    : 电荷数搜索上限，默认 3（LC-MS 小分子实际几乎恒为 1）。

    返回 dict
    --------
    status      : ok_unique / ok_ambiguous / no_match / invalid_formula / missing
    best        : 命中项（按 |误差| 升序第一），无命中为 None
    candidates  : 全部命中项（已排序）
    cross_mode  : True 表示候选横跨两种极性（需声明极性才能定论）
    message     : 面向使用者的一句话
    """
    out = {
        "status": "missing", "best": None, "candidates": [],
        "cross_mode": False, "message": "",
        "formula": formula, "mz": mz, "ion_mode": ion_mode,
    }
    if not formula or not str(formula).strip():
        out["message"] = "缺分子式，无法计算中性质量"
        return out
    try:
        m_mono = monoisotopic_mass(formula)
        m_avg = average_mass(formula)
    except FormulaError as e:
        out["status"] = "invalid_formula"
        out["message"] = f"分子式不合法：{e}"
        return out

    if mz is None or mz == "":
        out["message"] = "缺母离子 m/z，质量约束不启用"
        out["m_mono"] = round(m_mono, 5)
        out["m_avg"] = round(m_avg, 4)
        return out
    try:
        mz = float(mz)
    except (TypeError, ValueError):
        out["status"] = "invalid_formula"
        out["message"] = f"母离子 m/z 不是数值: {mz!r}"
        return out

    out["m_mono"] = round(m_mono, 5)
    out["m_avg"] = round(m_avg, 4)

    kind, tol = resolve_tolerance(device, tol_da, tol_ppm)
    out["tol_kind"], out["tol"] = kind, tol

    modes = ION_MODES if ion_mode is None else (ion_mode,)
    cands = []
    for md in modes:
        for label, delta, z in ADDUCTS[md]:
            if z > max_z:
                continue
            c = _candidate(m_mono, mz, md, label, delta, z, kind)
            if c["abs_err"] <= tol:
                cands.append(c)
    cands.sort(key=lambda c: c["abs_err"])
    out["candidates"] = cands

    if not cands:
        out["status"] = "no_match"
        nearest = None
        for md in modes:
            for label, delta, z in ADDUCTS[md]:
                if z > max_z:
                    continue
                c = _candidate(m_mono, mz, md, label, delta, z, kind)
                if nearest is None or c["abs_err"] < nearest["abs_err"]:
                    nearest = c
        if nearest:
            out["nearest"] = nearest
            out["message"] = (
                f"零命中：±{tol:g} {kind} 内找不到任何加合物。最接近的是 "
                f"{nearest['ion_mode']} 的 {nearest['adduct']}"
                f"（理论 {nearest['mz_theoretical']:.4f}，偏差 "
                f"{nearest['err_da']:+.3f} Da / {nearest['err_ppm']:+.1f} ppm）。"
                f"分子式或 m/z 至少有一个不对——请核对是否把子离子 m/z 填成了母离子。"
            )
        else:
            out["message"] = f"零命中：±{tol:g} {kind} 内无候选加合物。"
        return out

    out["best"] = cands[0]
    out["cross_mode"] = len({c["ion_mode"] for c in cands}) > 1

    if len(cands) == 1:
        out["status"] = "ok_unique"
        b = cands[0]
        out["message"] = (
            f"判定成立：{b['adduct']}（{b['ion_mode']}，z={b['z']}）。"
            f"理论 {b['mz_theoretical']:.4f}，偏差 {b['err_da']:+.4f} Da / "
            f"{b['err_ppm']:+.1f} ppm。分子式与 m/z 互相验证通过。"
        )
    else:
        out["status"] = "ok_ambiguous"
        # 候选间隔是否小于 2×容差（真的分不开）
        near = (cands[0]["abs_err"] + cands[1]["abs_err"]) < 2 * tol
        out["tight"] = True
        names = "、".join(f"{c['adduct']}({c['ion_mode']})" for c in cands[:4])
        out["message"] = (
            f"候选不唯一，请人工确认：{names}"
            + ("（跨电离极性，声明 ESI+/ESI- 后可定论）" if out["cross_mode"] else "")
        )
    return out


# ---------------------------------------------------------------------------
# 批量：方法表（化合物 + 化学式 + 母离子 m/z，无保留时间）一致性核查
# ---------------------------------------------------------------------------
def check_transition_list(rows, ion_mode=None, device=None):
    """对一批 (名称, 化学式, 母离子 m/z) 做质量自洽核查。

    rows : [{"name":..., "formula":..., "precursor":...}, ...]

    返回 {"ok": bool, "items": [...], "summary": {...}, "blocking": [...]}

    severity 语义
    -------------
    block : 阻断。缺/错分子式 → 分组预测不成立；零命中 → 数据自相矛盾。
    warn  : 降级。缺 m/z → 质量可分辨约束不启用（原实现是静默跳过）。
    ok    : 通过。

    本函数**不修改**输入，也不做任何补全（不猜分子式、不猜加合物）。
    """
    items, blocking = [], []
    for i, r in enumerate(rows or []):
        name = (r.get("name") or "").strip()
        formula = (r.get("formula") or "").strip()
        precursor = r.get("precursor")
        label = name or f"化合物{i + 1}"
        rec = {"index": i, "label": label, "formula": formula,
               "precursor": precursor, "adduct": None}

        # ① 分子式先行校验（它是分组排序的唯一输入，缺失即整链失效）
        if not formula:
            rec.update(severity="block",
                       status="missing_formula",
                       message="缺分子式：算不出极性指数，分组排序不成立")
            items.append(rec)
            blocking.append(label)
            continue
        ok_f, msg_f = formula_valid(formula)
        if not ok_f:
            rec.update(severity="block", status="invalid_formula",
                       message=f"分子式不合法：{msg_f}")
            items.append(rec)
            blocking.append(label)
            continue

        m_mono = round(monoisotopic_mass(formula), 5)
        rec["m_mono"] = m_mono

        # ② 无 m/z：降级，但必须显式说清约束未启用
        if precursor is None or precursor == "":
            rec.update(severity="warn", status="missing_mz",
                       m_avg=round(average_mass(formula), 4),
                       message="缺母离子 m/z：质量可分辨约束不启用（不静默放行）")
            items.append(rec)
            continue

        res = resolve_ion(formula, precursor, ion_mode=ion_mode, device=device)
        rec["status"] = res["status"]
        rec["message"] = res["message"]
        if res["status"] == "ok_unique":
            b = res["best"]
            rec.update(severity="ok", adduct=b["adduct"], ion_mode=b["ion_mode"],
                       z=b["z"], mz_theoretical=b["mz_theoretical"],
                       err_ppm=b["err_ppm"], err_da=b["err_da"])
        elif res["status"] == "ok_ambiguous":
            rec.update(severity="warn",
                       candidates=[f"{c['adduct']}({c['ion_mode']})"
                                   for c in res["candidates"]])
        else:
            rec["severity"] = "block"
            blocking.append(label)
        items.append(rec)

    n = len(items)
    n_ok = sum(1 for it in items if it["severity"] == "ok")
    n_warn = sum(1 for it in items if it["severity"] == "warn")
    n_block = sum(1 for it in items if it["severity"] == "block")
    summary = {
        "total": n, "ok": n_ok, "warn": n_warn, "block": n_block,
        # 加合物分布：同批方法表的加合物应一致，突变即为可疑项
        "adduct_hist": _hist([it.get("adduct") for it in items]),
        "ion_mode_hist": _hist([it.get("ion_mode") for it in items]),
    }
    return {"ok": n_block == 0, "items": items,
            "summary": summary, "blocking": blocking}


def _hist(vals):
    h = {}
    for v in vals:
        if v:
            h[v] = h.get(v, 0) + 1
    return h


def describe_ion(mz, formula, ion_mode=None, device=None):
    """面向 UI 的一行说明文本。"""
    r = resolve_ion(formula, mz, ion_mode=ion_mode, device=device)
    if r["status"] == "ok_unique":
        b = r["best"]
        return (f"{b['adduct']} · 理论 {b['mz_theoretical']:.4f} · "
                f"偏差 {b['err_ppm']:+.1f} ppm")
    return r["message"] or r["status"]


if __name__ == "__main__":
    print("=== 正向：常见化合物 [M+H]+ ===")
    for f, name in [("C6H12O6", "葡萄糖"), ("C9H8O4", "阿司匹林"),
                    ("C8H10N4O2", "咖啡因"), ("C7H5N3O6", "TNT"),
                    ("H2O", "水"), ("Fe(C2H3O2)3", "醋酸铁")]:
        m = monoisotopic_mass(f)
        print(f"  {name:<8} {f:<14} M={m:10.4f}  "
              f"[M+H]+={ion_mz(f, '[M+H]+', 'positive'):10.4f}  "
              f"avg={average_mass(f):9.3f}")
