"""14 种穷举组合声明

当前原子库下穷举完，不存在第 15 种。每个组合 = Recipe（PY 确定性查表）+ output_limit + 场景描述。

PY 范式：每个组合骨架是 LLM 生成（受约束）→ PY 后处理 → PY 校验 → PY 观测。
PY 部分确定性，只有生成那步是概率的。

组合分类：
  1-4  基础方式：纯引导 / 发散纠偏 / 确定性封死 / 检出上报
  5-8  值域限定：枚举选择 / 凝练过滤 / 槽位提取 / 必填最小化
  9-11 复合后处理：纠偏+检出 / 纠偏+凝练 / 检出+凝练
  12-14 精确校验生成：范围约束 / 精确匹配 / 枚举+过滤编造
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .atoms import Recipe


@dataclass
class ComboSpec:
    id: int
    name: str
    desc: str
    way_id: str
    recipe: Recipe
    output_limit: dict = field(default_factory=dict)
    py_pattern: str = ""
    scene_tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "desc": self.desc,
            "way_id": self.way_id, "recipe": self.recipe.to_dict(),
            "output_limit": self.output_limit, "py_pattern": self.py_pattern,
            "scene_tags": self.scene_tags,
        }


_SOFT = {"max_length": 500}
_DIVERGE = {"max_length": 800}
_SLOT = {"max_fields": 5}


COMBOS: list[ComboSpec] = [
    ComboSpec(1, "pure_guide", "纯软引导：只任务提示词，LLM 自由填空",
              "pure_guide", Recipe("text", "", [], "guide", True, []),
              _SOFT, "LLM生成→PY校验(约束)", ["开放生成", "续写", "摘要"]),

    ComboSpec(2, "diverge_correct", "发散纠偏：高温度发散+代码确定性纠偏",
              "diverge_correct", Recipe("text", "", ["deterministic"], "diverge", True, ["changed"]),
              _DIVERGE, "LLM生成→PY后处理(正则)→PY校验(纠偏目标)", ["创意生成", "文案", "扩写"]),

    ComboSpec(3, "deterministic_pin", "确定性封死：代码钉死格式，LLM 零参与",
              "deterministic_pin", Recipe("text", "", ["deterministic"], "deterministic", False, ["changed"]),
              _SOFT, "LLM生成→PY后处理(钉死)→PY校验(封死目标)", ["格式固定", "编号重排", "空行归一"]),

    ComboSpec(4, "detect_report", "检出上报：不可枚举检出+上报，不阻塞",
              "detect_report", Recipe("text", "", ["detect_report"], "detect_report", False, ["flagged"]),
              _SOFT, "LLM生成→PY后处理(检出)→PY校验(有检出即成功)", ["数值核查", "事实核查", "人工复审"]),

    ComboSpec(5, "enum_select", "可枚举选择：从有限候选词中选一个或未指定",
              "value_bound", Recipe("select", "", [], "in_set", True, ["hit"]),
              _SOFT, "LLM生成(选词)→PY校验(集合内)", ["分类", "标注", "情绪判断"]),

    ComboSpec(6, "condense_enum", "凝练+枚举过滤：凝练为短词+校验在允许集",
              "value_bound", Recipe("text", "", ["enum_filter"], "no_extra", True, ["fabricated"]),
              _SOFT, "LLM生成(凝练)→PY后处理(过滤)→PY校验(无编造)", ["标签凝练", "主题提取", "关键词"]),

    ComboSpec(7, "slot_extract", "槽位提取：从文本提取填槽位，查多余编造",
              "value_bound", Recipe("slot", "extra_check", ["json_parse"], "no_extra", True, ["extra_keys"]),
              _SLOT, "LLM生成(填槽)→PY后处理(解析)→PY校验(无多余key)", ["信息提取", "结构化", "实体识别"]),

    ComboSpec(8, "required_min", "必填最小化：required 槽必填，可留空槽填未指定",
              "value_bound", Recipe("slot", "required_min", ["json_parse"], "required_full", True, ["left_empty"]),
              _SLOT, "LLM生成(填槽)→PY后处理(解析)→PY校验(必填齐全)", ["表单填写", "最小信息", "必填校验"]),

    ComboSpec(9, "diverge_detect", "纠偏+检出：发散纠偏后检出异常上报",
              "custom", Recipe("text", "", ["deterministic", "detect_report"], "detect_report", True, ["changed", "flagged"]),
              _DIVERGE, "LLM生成→PY后处理(纠偏+检出)→PY校验(有检出)", ["创意+核查", "文案+合规", "扩写+数值"]),

    ComboSpec(10, "diverge_condense", "纠偏+凝练：发散纠偏后凝练为标签",
              "custom", Recipe("text", "", ["deterministic", "enum_filter"], "no_extra", True, ["changed", "fabricated"]),
              _DIVERGE, "LLM生成→PY后处理(纠偏+过滤)→PY校验(无编造)", ["创意+标签", "文案+主题", "扩写+关键词"]),

    ComboSpec(11, "detect_condense", "检出+凝练：检出异常后凝练为标签",
              "custom", Recipe("text", "", ["detect_report", "enum_filter"], "no_extra", True, ["flagged", "fabricated"]),
              _SOFT, "LLM生成→PY后处理(检出+过滤)→PY校验(无编造)", ["核查+标签", "数值+主题", "事实+关键词"]),

    ComboSpec(12, "range_bound_gen", "范围约束生成：数值必须在区间内",
              "custom", Recipe("slot", "extra_check", ["json_parse"], "in_range", True, []),
              _SLOT, "LLM生成(填槽)→PY后处理(解析)→PY校验(区间内)", ["数值校验", "范围检查", "量化提取"]),

    ComboSpec(13, "exact_match_gen", "精确匹配生成：值必须等于指定值",
              "custom", Recipe("slot", "extra_check", ["json_parse"], "eq_exact", True, []),
              _SLOT, "LLM生成(填槽)→PY后处理(解析)→PY校验(精确相等)", ["精确提取", "固定值", "严格匹配"]),

    ComboSpec(14, "enum_filter_fabricate", "枚举选择+过滤编造：选词后过滤编造",
              "custom", Recipe("select", "", ["enum_filter"], "no_extra", True, ["hit", "fabricated"]),
              _SOFT, "LLM生成(选词)→PY后处理(过滤)→PY校验(无编造)", ["分类+防编造", "标注+过滤", "严格分类"]),
]


_COMBO_BY_ID: dict[int, ComboSpec] = {c.id: c for c in COMBOS}
_COMBO_BY_NAME: dict[str, ComboSpec] = {c.name: c for c in COMBOS}


def get_combo(combo_id) -> ComboSpec | None:
    if isinstance(combo_id, int):
        return _COMBO_BY_ID.get(combo_id)
    return _COMBO_BY_NAME.get(combo_id)


def list_combos() -> list[ComboSpec]:
    return list(COMBOS)


def combos_by_tag(tag: str) -> list[ComboSpec]:
    return [c for c in COMBOS if tag in c.scene_tags]