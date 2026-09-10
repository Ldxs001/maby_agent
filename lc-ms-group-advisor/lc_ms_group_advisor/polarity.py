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

"""分子式解析 + 极性指数计算（纯标准库，零依赖）。

极性指数用于反相 C18 出峰顺序预测：
  PI 越大 → 极性越大 → 反相中越先出峰
  PI 越小 → 疏水性越大 → 反相中越后出峰

公式：
  hetero = O*2 + N*3 + S*1.5 + P*2 + F*1 + Cl*1 + Br*0.8 + I*0.5
  DBE    = C - H/2 + N/2 + 1  （不饱和度，反映芳香/环状）
  backbone = C + H*0.1
  PI = hetero / (backbone + max(DBE,0)*0.3 + 0.1)

局限：分子式不含结构信息，同分异构体 PI 相同但实际极性可能不同。
对"大概分组"场景够用，个别排错实测修正。
"""
import re

_ELEM_RE = re.compile(r"([A-Z][a-z]?)(\d*)")

_HETERO_WEIGHTS = {
    "O": 2.0, "N": 3.0, "S": 1.5, "P": 2.0,
    "F": 1.0, "Cl": 1.0, "Br": 0.8, "I": 0.5,
}


def parse_formula(formula: str) -> dict:
    """解析分子式，返回各原子计数字典。

    支持 C H O N S P F Cl Br I 等标准元素。
    例: "C6H12O6" -> {"C":6,"H":12,"O":6}
        "C2H5Cl" -> {"C":2,"H":5,"Cl":1}
    """
    formula = formula.strip()
    if not formula:
        return {}
    atoms = {}
    for elem, count_str in _ELEM_RE.findall(formula):
        if not elem or elem[0].islower():
            continue
        n = int(count_str) if count_str else 1
        atoms[elem] = atoms.get(elem, 0) + n
    return atoms


def polarity_index(formula: str) -> float:
    """从分子式计算极性指数。PI 越大极性越大。"""
    atoms = parse_formula(formula)
    C = atoms.get("C", 0)
    H = atoms.get("H", 0)
    N = atoms.get("N", 0)

    hetero = sum(atoms.get(e, 0) * w for e, w in _HETERO_WEIGHTS.items())
    dbe = C - H / 2.0 + N / 2.0 + 1
    backbone = C + H * 0.1
    pi = hetero / (backbone + max(dbe, 0) * 0.3 + 0.1)
    return round(pi, 4)


def describe_polarity(pi: float) -> str:
    """极性指数的通俗描述。"""
    if pi >= 2.0:
        return "强极性（先出峰）"
    elif pi >= 1.0:
        return "中等极性"
    elif pi >= 0.5:
        return "弱极性"
    elif pi > 0.0:
        return "弱疏水性（后出峰）"
    else:
        return "非极性/纯烃（最先出或死体积出峰）"


if __name__ == "__main__":
    tests = [
        ("H2O", "水"),
        ("C2H6O", "乙醇"),
        ("C3H6O", "丙酮"),
        ("C6H12O6", "葡萄糖"),
        ("C6H6", "苯"),
        ("C6H14", "正己烷"),
        ("C2H5Cl", "氯乙烷"),
        ("C10H7Br", "溴萘"),
        ("C7H5N3O6", "TNT"),
        ("C8H10N4O2", "咖啡因"),
    ]
    print(f"{'分子式':<12} {'名称':<8} {'PI':>8}  {'描述'}")
    print("-" * 60)
    for f, name in tests:
        pi = polarity_index(f)
        print(f"{f:<12} {name:<8} {pi:>8.4f}  {describe_polarity(pi)}")