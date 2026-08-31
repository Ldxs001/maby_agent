"""silprespec-orchestrator — 前置规范编排器

基于"我思故我写"方法论的多 agent 协同头部规划器。
根据用户任务+工具集，从 14 种穷举的原子化组合里选最合适的，
PY 确定性组合，LLM 填空执行，输出给工具（智能体）走内部流程。

核心流程：
  用户输入 → Orchestrator(分类+选编排模式) → ProgressMap(进度地图)
    → 对每个子任务：
        Mapper(选组合+设参) → Composer(PY 组合) → Executor(LLM 填空+调智能体)
        → Adapter(步骤间适配，不能直通则 loop 回 Mapper 选适配组合)
    → 汇总输出

14 种穷举组合（当前原子库下穷举完，不存在第 15 种）：
  1-8 已实现：pure_guide / diverge_correct / deterministic_pin / detect_report
             / enum_select / condense_enum / slot_extract / required_min
  9-14 新增：纠偏+检出 / 纠偏+凝练 / 检出+凝练 / 范围约束生成
            / 精确匹配生成 / 枚举选择+过滤编造
"""

from .llm_client import LLMClient, LLMClientError
from .combo_registry import COMBOS, ComboSpec, get_combo, list_combos
from .tool_registry import ToolSpec, FieldSpec, ExampleSpec, TOOL_REGISTRY, register_tool
from .progress_map import ProgressMap, classify_input
from .orchestrator import Orchestrator
from .mapper import Mapper
from .composer import Composer
from .executor import Executor
from .adapter import Adapter

__version__ = "0.1.0"
__all__ = [
    "LLMClient", "LLMClientError",
    "COMBOS", "ComboSpec", "get_combo", "list_combos",
    "ToolSpec", "FieldSpec", "ExampleSpec", "TOOL_REGISTRY", "register_tool",
    "ProgressMap", "classify_input",
    "Orchestrator", "Mapper", "Composer", "Executor", "Adapter",
]