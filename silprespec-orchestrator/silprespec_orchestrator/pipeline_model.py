"""前置规范数据模型 — 方式/配置/结果

5 种前置规范方式（按逻辑分类）：
  1. pure_guide      纯软引导（只任务提示词，可加输出约束校验）
  2. value_bound     值域限定（gate/slot/required_min/condense 合并，bound_type 区分）
  3. diverge_correct 发散纠偏（高温度发散+代码确定性纠偏）
  4. deterministic_pin 确定性封死（代码钉死可枚举，A 形态）
  5. detect_report   检出上报（不可枚举检出+上报，B 形态）
  + custom           自定义组合

复用自 silprespec-emulator，为编排器提供原子库的数据基础。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json


WAYS = [
    ("pure_guide", "纯软引导", "只任务提示词，LLM 自由填空，可加输出约束校验"),
    ("value_bound", "值域限定", "gate/slot/required_min/condense 合并，bound_type 区分值域类型"),
    ("diverge_correct", "发散纠偏", "高温度发散+代码确定性纠偏，语义偏离拉回"),
    ("deterministic_pin", "确定性封死", "代码钉死可枚举，LLM 零参与，A 形态错误无通道"),
    ("detect_report", "检出上报", "不可枚举检出+上报，不阻塞生成通道，B 形态"),
]

TASK_PROMPTS = {
    "pure_guide": "按照任务要求，对用户输入给出你的填空结果。",
    "value_bound": "按照要求，从用户输入中提取/分类/凝练信息。",
    "diverge_correct": "基于用户输入发散生成一段内容。",
    "deterministic_pin": "生成一段内容，后续将被代码后处理钉死。",
    "detect_report": "生成一段可能含特定内容（如数值）的文本。",
    "custom": "按照任务要求，对用户输入给出你的填空结果。",
}

BOUND_TYPES = [
    ("enum_select", "可枚举选择", "从有限候选词中选一个或「未指定」"),
    ("slot_extract", "槽位提取", "从文本提取信息填槽位，查多余编造"),
    ("required_min", "必填最小化", "required 槽必填，可留空槽填未指定"),
    ("condense_enum", "凝练+枚举过滤", "凝练为短词+枚举校验禁泛化"),
]


@dataclass
class WayConfig:
    way: str = "pure_guide"
    enabled: bool = True
    config: dict = field(default_factory=dict)
    max_retry: int = 3
    task_prompt: str = ""
    recipe: dict = field(default_factory=dict)
    template_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "WayConfig":
        return WayConfig(
            way=d.get("way", "pure_guide"),
            enabled=d.get("enabled", True),
            config=d.get("config", {}),
            max_retry=d.get("max_retry", 3),
            task_prompt=d.get("task_prompt", ""),
            recipe=d.get("recipe", {}),
            template_id=d.get("template_id", ""),
        )


def default_config(way: str) -> dict:
    if way == "pure_guide":
        return {"guide_prompt": "围绕主题展开，保持语义一致",
                "output_constraints": {"required_keywords": [], "forbidden_keywords": [],
                                        "max_length": 0, "format_regex": ""}}
    if way == "value_bound":
        return {"bound_type": "enum_select",
                "gates": [{"name": "情绪", "words": ["积极", "消极", "中性"], "logic": "or"}],
                "allow_unspecified": True,
                "slots": [{"name": "who", "required": True}, {"name": "what", "required": True},
                          {"name": "why", "required": False}],
                "condense_rule": "浓缩为短词，禁止泛化造新词",
                "enums": ["环境治理", "生态文明", "绿色发展"]}
    if way == "diverge_correct":
        return {"diverge_prompt": "自由发散生成",
                "regex_replaces": [],
                "normalize_blanklines": True,
                "correction_target": {"format_regex": "", "required_pattern": "", "forbidden_pattern": ""}}
    if way == "deterministic_pin":
        return {"regex_replaces": [],
                "renumber_source": False, "normalize_blanklines": True,
                "pin_target": {"exact_value": "", "format_regex": ""}}
    if way == "detect_report":
        return {"detect_pattern": r"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)",
                "allowed_values": [], "report_label": "建议人工复审"}
    return {}


@dataclass
class WayResult:
    way: str = ""
    success: bool = False
    filled: dict = field(default_factory=dict)
    retry_count: int = 0
    exhausted: bool = False
    attempts: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    total_tokens: int = 0
    elapsed_total: float = 0.0
    error: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def json_key(d) -> str:
    try:
        return json.dumps(d, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(d)