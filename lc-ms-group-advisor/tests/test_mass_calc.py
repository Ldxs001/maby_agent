# -*- coding: utf-8 -*-
"""mass_calc 离线验证：正向质量、反向加合物判定、唯一性扫描、异常路径。"""
import os
import sys
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from lc_ms_group_advisor import mass_calc as mc

PASS, FAIL = [], []
def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))

# ---------------------------------------------------------------- 1 正向质量
print("=== 1. 正向：单同位素质量 vs 文献值 ===")
LIT = {  # (formula, name, 文献单同位素质量)
    "H2O":        ("水",       18.01056),
    "C6H12O6":    ("葡萄糖",   180.06339),
    "C9H8O4":     ("阿司匹林", 180.04226),
    "C8H10N4O2":  ("咖啡因",   194.08038),
    "C8H14ClN5":  ("莠去津",   215.09377),
    "C2H6O":      ("乙醇",      46.04187),
    "C7H5N3O6":   ("TNT",      227.01784),
}
for f, (nm, ref) in LIT.items():
    m = mc.monoisotopic_mass(f)
    ok = abs(m - ref) < 0.0005
    chk(f"{nm} {f} M={m:.5f} (文献 {ref})", ok, f"Δ={m-ref:+.5f}")
print()

print("=== 2. 正向：[M+H]+ / [M-H]- / [M+Na]+ ===")
for f, nm in [("C8H14ClN5", "莠去津"), ("C9H8O4", "阿司匹林")]:
    m = mc.monoisotopic_mass(f)
    print(f"  {nm:<6} M={m:.4f}  "
          f"[M+H]+={mc.ion_mz(f,'[M+H]+','positive'):.4f}  "
          f"[M+Na]+={mc.ion_mz(f,'[M+Na]+','positive'):.4f}  "
          f"[M-H]-={mc.ion_mz(f,'[M-H]-','negative'):.4f}")
print()

# ------------------------------------------------- 3 你的场景：方法表无保留时间
print("=== 3. 你的场景：方法表（名称 + 化学式 + 母离子 m/z，无保留时间）===")
# m/z 由各自 [M+H]+ 正向算出，模拟一张 ESI+ 方法表；RT 全缺
METHOD = [
    ("莠去津",     "C8H14ClN5"),
    ("阿司匹林",   "C9H8O4"),
    ("咖啡因",     "C8H10N4O2"),
    ("葡萄糖",     "C6H12O6"),
    ("TNT",        "C7H5N3O6"),
    ("多菌灵",     "C9H9N3O2"),
    ("敌敌畏",     "C4H7Cl2O4P"),
]
rows = []
for nm, f in METHOD:
    rows.append({"name": nm, "formula": f,
                 "precursor": round(mc.ion_mz(f, "[M+H]+", "positive"), 4)})  # 无 RT 字段
rep = mc.check_transition_list(rows, ion_mode="positive", device="qqq")
print(f"  批次结果 ok={rep['ok']}  统计={rep['summary']}")
print(f"  {'化合物':<8}{'化学式':<13}{'m/z':>10}  {'判定':<12}{'误差':>9}  状态")
for it in rep["items"]:
    print(f"  {it['label']:<8}{it['formula']:<13}{it['precursor']:>10.4f}  "
          f"{str(it.get('adduct')):<12}{it.get('err_ppm', ''):>9}  {it['severity']}")
chk("全部化合物 adduct 判定为 [M+H]+",
    all(it.get("adduct") == "[M+H]+" for it in rep["items"]))
chk("无保留时间字段仍全部通过", rep["ok"] and rep["summary"]["block"] == 0)
chk("加合物分布一致（无突变）", rep["summary"]["adduct_hist"] == {"[M+H]+": 7})
print()

# -------------------------------------------------------- 4 唯一性扫描（证据）
print("=== 4. 唯一性扫描：容忍 ±tol 时是否存在碰撞（同极性 / 跨极性）===")
def scan(mode_list, tol, lo=50.0, hi=1500.0, step=0.05, max_z=3):
    hits, M = [], lo
    while M <= hi:
        cands = []
        for md in mode_list:
            for label, delta, z in mc.ADDUCTS[md]:
                if z <= max_z:
                    cands.append(((M + delta) / z, md, label))
        cands.sort(key=lambda t: t[0])
        for i in range(len(cands) - 1):
            gap = cands[i + 1][0] - cands[i][0]
            if gap < 2 * tol:
                hits.append((round(M, 2), gap, cands[i][1:], cands[i + 1][1:]))
                break
        M += step
    return hits

tol = 0.5  # 标称分辨（qqq/it/qtrap）
one = scan(("positive",), tol)
print(f"  仅 ESI+（±{tol} Da，M∈[50,1500]）：碰撞点 {len(one)} 个"
      + (f"，例 {one[0][0]} Da" if one else " → 无歧义"))
chk("同极性内全区间唯一", len(one) == 0)

both = scan(("positive", "negative"), tol)
gap_cross = min(h[1] for h in both) if both else None
print(f"  跨极性（未声明 ESI+/ESI-）：碰撞区间 {len(both)} 处，"
      f"最小候选间隔 {gap_cross:.4f} Da → 歧义窗口宽 {2*tol-gap_cross:.4f} Da")
chk("跨极性存在边际歧义（故需声明极性）", len(both) > 0)

# 解析验证：M≈22 / M≈44 是理论碰撞点
cross = []
for label, delta, z in mc.ADDUCTS["positive"]:
    if z != 2:
        continue
    for lb2, d2, z2 in mc.ADDUCTS["positive"]:
        if z2 != 1:
            continue
        M = delta - 2 * d2
        if 0 < M < 100:
            cross.append((round(M, 2), lb2, label))
print(f"  z=1 与 z=2 的理论碰撞质量：{cross}")
chk("理论碰撞点均 < 50 Da（远低于小分子范围）", all(c[0] < 50 for c in cross))
print()

# ------------------------------------------------------- 5 零命中 & 异常路径
print("=== 5. 零命中：把子离子 m/z 误填成母离子 ===")
r = mc.resolve_ion("C8H14ClN5", 174.055, ion_mode="positive", device="qqq")
print(f"  莠去津 M=215.0938，填 174.055 → {r['status']}")
print(f"    {r['message']}")
chk("误填子离子 → 零命中阻断", r["status"] == "no_match")
print()

print("=== 6. 异常路径 ===")
cases = [
    ("NaCl",            "含非有机元素"),
    ("C18",             "柱型号/碳链，非化合物"),
    ("约130",           "自然语言数字"),
    ("C6H12O6·H2O",     "水合物（多组分）"),
    ("Fe(C2H3O2)3",     "括号倍数"),
    ("2C6H12O6",        "系数在首（非法）"),
    ("C6h12o6",         "小写元素（非法）"),
]
for f, note in cases:
    ok, msg = mc.formula_valid(f)
    extra = ""
    if ok:
        extra = (f"M={mc.monoisotopic_mass(f):.4f}  "
                 f"原子={mc.format_formula(mc.parse_formula_strict(f))}")
    print(f"  {f:<14}{'OK  ' if ok else 'FAIL'}  {note:<16}{extra or msg}")
chk("C18 语法合法（语义排除归上游提示词层）", mc.formula_valid("C18")[0])
chk("水合物可解析且质量相加",
    abs(mc.monoisotopic_mass("C6H12O6·H2O")
        - (mc.monoisotopic_mass("C6H12O6") + mc.monoisotopic_mass("H2O"))) < 1e-9)
chk("括号倍数正确", mc.format_formula(mc.parse_formula_strict("Fe(C2H3O2)3")) == "C6H9FeO6")
chk("嵌套括号", mc.format_formula(mc.parse_formula_strict("K4[Fe(CN)6]")) == "C6FeK4N6")
# C18 这类伪化学式在「有 m/z」时会被质量互证拦下 —— 语义噪音的兜底在质量层
r18 = mc.resolve_ion("C18", 181.05, ion_mode="positive", device="qqq")
chk("C18 + 真实 m/z → 零命中拦截", r18["status"] == "no_match")
print()

# ------------------------------------------------------ 7 缺分子式 / 缺 m/z
print("=== 7. 缺项处理（fail-closed，不静默）===")
rep2 = mc.check_transition_list([
    {"name": "有式无 m/z", "formula": "C9H8O4"},
    {"name": "缺分子式",   "formula": "",        "precursor": 181.05},
    {"name": "错分子式",   "formula": "C18",     "precursor": 181.05},
], ion_mode="positive")
for it in rep2["items"]:
    print(f"  {it['label']:<12}{it['severity']:<6}{it['status']:<16}{it['message']}")
chk("缺分子式 → block", rep2["items"][1]["severity"] == "block")
chk("缺 m/z → warn（且不静默放行）", rep2["items"][0]["severity"] == "warn")
chk("批次 ok=False（有阻断项）", rep2["ok"] is False)
print()

# ------------------------------------------------------------------- 8 极低分辨
print("=== 8. 容差口径：qtof(ppm) vs qqq(Da) ===")
for dev, mzv in [("qtof", 216.10105 + 0.004), ("qtof", 216.10105 + 0.02),
                 ("qqq", 216.10), ("qqq", 216.6)]:
    r = mc.resolve_ion("C8H14ClN5", mzv, ion_mode="positive", device=dev)
    print(f"  {dev:<6} m/z={mzv:<10.4f} {r['status']:<14}{r.get('tol_kind')}="
          f"{r.get('tol')}  {(r['best'] or {}).get('adduct', '-')}"
          f"  err={((r['best'] or {}).get('err_ppm') or 0):+.1f} ppm")
chk("qtof 4 mDa 偏差通过", mc.resolve_ion("C8H14ClN5", 216.10505, device="qtof")["status"] == "ok_unique")
chk("qtof 20 mDa 偏差拒绝", mc.resolve_ion("C8H14ClN5", 216.12105, device="qtof")["status"] == "no_match")
chk("qqq 0.5 Da 内通过", mc.resolve_ion("C8H14ClN5", 216.6, device="qqq")["status"] == "ok_unique")
print()

print("=" * 62)
print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
