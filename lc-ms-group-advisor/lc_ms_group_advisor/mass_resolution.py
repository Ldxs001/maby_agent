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

"""质量分辨率判据（纯标准库，零依赖）。

核心区分（本模块存在的理由）：

  质量准确度 (measurement accuracy, ppm)  ≠  质量分辨率 (resolution, Δm/FWHM)
  仪器能"称准"不代表能"分开"。两个质量峰间距 Δm 一旦小于峰宽 FWHM，
  合成谱上就是一个包，无论仪器读数精确到小数点后几位。

数学推导（两个等高等宽高斯峰，中心相差 Δm，峰宽 σ）：

  单峰   f(x) = exp( -(x-Δm/2)² / (2σ²) )
  合成谱在 x=0 处的谷值  Σf(0) = 2·exp( -Δm²/(8σ²) )
  单峰峰高 1，两峰叠加后的峰顶 1 + exp( -Δm²/(2σ²) )
  峰宽   FWHM  = 2σ·√(2·ln2) = 2.354820·σ

谷值定义（本模块统一采用「相对合成峰的峰顶」，即人眼看到的凹陷深度）：

  v(Δm) = 2·exp(-t²/8) / (1 + exp(-t²/2))        t = Δm / σ

反解出判据系数：

  k = Δm / FWHM = t / 2.354820

  谷值标准         v      k（精确解）   k（闭式近似 √(8ln(2/v))/2.3548）
  50% 谷          0.50      1.41219              1.41421
  10% 谷（经典）   0.10      2.07892              2.07892
  1%  谷          0.01      2.76475              2.76475

闭式近似 v = 2·exp(-Δm²/(8σ²)) 忽略了合成峰顶比单峰高出的那一小截（Δm 大时
可忽略），在 10% / 1% 锚点上与精确解相差 <0.001%，在 50% 锚点上差 0.14%。
本模块统一用精确解（二分法），保证 valley_to_k 与 k_to_valley 互为严格反函数，
且判据锚点上"选 50% 谷就一定读到 50%"。

质谱文献的「10% 谷」分辨率定义即 k ≈ 2.08。
注意 Δm/FWHM = 1 时谷值高达 94%——凹陷只有峰高的 6%，肉眼完全看不出是两个峰，
所以 k 绝不能取 1（旧实现取 k=1，宽松一倍以上，系统性漏报混峰）。

峰宽 FHSW/FWHM 随 m/z 的缩放律由仪器类型决定，不能一刀切：
  · 单位分辨率四极杆（QqQ / QTRAP / 离子阱）：FWHM ≈ 0.7 Da 恒定，不随 m/z 变
  · 高分辨 TOF / Orbitrap：FWHM ∝ m/z（等价于恒定 ppm）
因此 fhsw 模式必须可选（auto / constant / proportional），默认跟随设备标称值；
用户填实测值时必须能覆盖标称值。
"""
import math

# FWHM = FWHM_FACTOR × σ
FWHM_FACTOR = 2.0 * math.sqrt(2.0 * math.log(2.0))  # ≈ 2.354820

# 谷值判据预设（key 会被前端 /api/res_presets 取用）
VALLEY_PRESETS = {
    "v10": {"label": "10% 谷（质谱经典）", "valley_pct": 10.0},
    "v50": {"label": "50% 谷", "valley_pct": 50.0},
    "v1": {"label": "1% 谷（严格）", "valley_pct": 1.0},
    "custom": {"label": "自定义 k", "valley_pct": None},
}
DEFAULT_VALLEY = "v10"

# 峰宽来源模式
FHSW_MODES = {
    "auto": {"label": "跟随设备标称"},
    "constant": {"label": "恒定峰宽 (Da)"},
    "proportional": {"label": "按 m/z 等比 (ppm)"},
}
DEFAULT_FHSW_MODE = "auto"
DEFAULT_VALLEY_PCT = 10.0


def _valley_from_t(t):
    """t = Δm/σ → 谷值（相对合成峰顶）。t=0 时谷值 100%，t→∞ 时趋于 0。"""
    t2 = t * t
    return 2.0 * math.exp(-t2 / 8.0) / (1.0 + math.exp(-t2 / 2.0))


def _t_from_valley(v, iters=120):
    """谷值 v → t = Δm/σ。无闭式解，二分法（区间恒含解且函数单调递减）。"""
    lo, hi = 1e-12, 40.0
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if _valley_from_t(mid) > v:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def valley_to_k(valley_pct):
    """谷值标准(百分比) → 判据系数 k = Δm/FWHM。"""
    try:
        v = float(valley_pct) / 100.0
    except (TypeError, ValueError):
        v = DEFAULT_VALLEY_PCT / 100.0
    v = min(max(v, 1e-6), 0.999999)
    return _t_from_valley(v) / FWHM_FACTOR


def k_to_valley(k):
    """判据系数 k → 对应谷值(百分比)。与 valley_to_k 互为反函数。"""
    try:
        k = float(k)
    except (TypeError, ValueError):
        return 100.0
    if k <= 0:
        return 100.0
    return _valley_from_t(k * FWHM_FACTOR) * 100.0


def resolve_k(params):
    """从参数解析判据系数 k（含自定义覆盖）。"""
    params = params or {}
    if params.get("valley_preset") == "custom":
        try:
            k = float(params.get("k_custom"))
            if k > 0:
                return k
        except (TypeError, ValueError):
            pass
        return valley_to_k(DEFAULT_VALLEY_PCT)
    preset = VALLEY_PRESETS.get(params.get("valley_preset", DEFAULT_VALLEY))
    if preset and preset.get("valley_pct"):
        return valley_to_k(preset["valley_pct"])
    return valley_to_k(DEFAULT_VALLEY_PCT)


def nominal_fhsw_da(dev, mz):
    """设备标称峰宽(Da)：res_ppm → 随 m/z 等比；否则 res_da 恒定。"""
    dev = dev or {}
    if dev.get("res_ppm"):
        return float(mz) * float(dev["res_ppm"]) * 1e-6
    return float(dev.get("res_da", 0.7))


def resolve_fhsw_da(dev, mz, params):
    """解析实际使用的峰宽 FWHM(Da)。用户显式输入优先于设备标称值。

    params 键：
      fhsw_mode     auto | constant | proportional
      fhsw_da       恒定模式下的峰宽 (Da)
      fhsw_ppm      等比模式下的相对峰宽 (ppm)
    """
    params = params or {}
    mode = params.get("fhsw_mode", DEFAULT_FHSW_MODE)
    if mode == "constant":
        val = _positive_float(params.get("fhsw_da"))
        if val is not None:
            return val
    elif mode == "proportional":
        val = _positive_float(params.get("fhsw_ppm"))
        if val is not None:
            return float(mz) * val * 1e-6
    return nominal_fhsw_da(dev, mz)


def _positive_float(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def judge(dm, mz, dev, params=None):
    """判定间距 dm(Da) 在 m/z 处是否可分辨。

    返回 dict：
      a / b       参与判定的两个相邻 m/z（仅由 check_list 填充）
      dm / mz / fhsw_da / k / need_da / valley_pct / overlap
      overlap=True 表示 Δm < k×FWHM，即质量混峰。

    比较用 1e-9 的相对容差：判据是「Δm ≥ k×FWHM」，恰好等于判据应当通过，
    而浮点表示会让 300.9-300.2 变成 0.6999999999999886，
    不加容差就会出现「Δm=0.7 < 判据 0.7」这种自相矛盾的判定。
    """
    k = resolve_k(params)
    fhsw = resolve_fhsw_da(dev, mz, params)
    need = k * fhsw
    sigma = fhsw / FWHM_FACTOR
    valley = _valley_from_t(float(dm) / sigma) * 100.0 if sigma > 0 else 100.0
    tol = 1e-9 * max(need, 1e-12)
    return {
        "dm": float(dm),
        "mz": float(mz),
        "fhsw_da": fhsw,
        "k": k,
        "need_da": need,
        "valley_pct": min(valley, 100.0),
        "overlap": float(dm) < need - tol,
    }


def check_list(values, dev, params=None):
    """对一组 m/z 取最近邻对并判定；不足 2 个有效值时返回 None。

    与 judge 一起构成唯一判据入口——所有调用点（分组 / 通量 / 核算）
    必须走这里，避免各写一套阈值。
    """
    vals = sorted(float(v) for v in (values or []) if v)
    if len(vals) < 2:
        return None
    best = None
    for a, b in zip(vals, vals[1:]):
        j = judge(b - a, (a + b) / 2.0, dev, params)
        j["a"] = a
        j["b"] = b
        if best is None or j["dm"] < best["dm"]:
            best = j
    return best


def preset_payload():
    """供前端下拉使用的静态选项（单一来源，避免 HTML 里再抄一遍）。"""
    return {
        "valley_presets": VALLEY_PRESETS,
        "fhsw_modes": FHSW_MODES,
        "defaults": {
            "valley_preset": DEFAULT_VALLEY,
            "fhsw_mode": DEFAULT_FHSW_MODE,
            "k": valley_to_k(DEFAULT_VALLEY_PCT),
            "valley_pct": DEFAULT_VALLEY_PCT,
        },
        "table": [
            {"valley_pct": v, "k": round(valley_to_k(v), 5)}
            for v in (50.0, 10.0, 1.0)
        ],
    }


if __name__ == "__main__":
    print("=== 判据系数（精确解 / 闭式近似）===")
    for v in (50.0, 10.0, 1.0):
        approx = math.sqrt(8.0 * math.log(2.0 / (v / 100.0))) / FWHM_FACTOR
        k = valley_to_k(v)
        print("  %5.1f%% 谷 → k = %.5f   近似 %.5f   偏差 %.3f%%   反算谷值 %.2f%%" % (
            v, k, approx, abs(k - approx) / approx * 100, k_to_valley(k)))

    print()
    print("=== k 取 1 会怎样（旧实现）===")
    print("  k=1.0 → Δm=FWHM 时谷值 %.1f%%（凹陷仅 %.1f%%，肉眼看不出两个峰）" % (
        k_to_valley(1.0), 100.0 - k_to_valley(1.0)))
    for kv in (1.0, 1.41, 2.08, 2.76):
        print("  k=%.2f → 谷值 %.1f%%" % (kv, k_to_valley(kv)))

    print()
    print("=== 设备标称峰宽与判据 ===")
    for tag, dev, mz in (("QqQ  300", {"res_da": 0.7}, 300.0),
                         ("QqQ 1000", {"res_da": 0.7}, 1000.0),
                         ("TOF  300", {"res_ppm": 20.0}, 300.0),
                         ("TOF 1000", {"res_ppm": 20.0}, 1000.0)):
        j = judge(0.05, mz, dev, {})
        print("  %-9s FWHM=%.4f Da  k=%.3f  需 Δm≥%.4f Da  Δm=0.050 → %s（谷值 %.1f%%）" % (
            tag, j["fhsw_da"], j["k"], j["need_da"],
            "混峰" if j["overlap"] else "分开", j["valley_pct"]))

    print()
    print("=== FHSW 覆盖：同一 TOF 标称 20ppm，实测宽 3 倍 ===")
    dev = {"res_ppm": 20.0}
    for fhsw in (0.006, 0.018):
        p = {"fhsw_mode": "constant", "fhsw_da": fhsw}
        j = judge(0.030, 300.0, dev, p)
        tag = "标称" if abs(fhsw - 0.006) < 1e-9 else "实测×3"
        print("  %-7s FWHM=%.4f Da → 需 Δm≥%.4f Da，Δm=0.030 → %s" % (
            tag, j["fhsw_da"], j["need_da"], "混峰" if j["overlap"] else "分开"))
