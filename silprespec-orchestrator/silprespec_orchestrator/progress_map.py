"""进度地图 + 输入分类

进度地图：每步 LLM 都看到完整用户初始输入 + 输入分类 + 全局进度，
不盲目执行。

输入分类：穷举用户任务类型（LLM 填空分类，PY 查表映射）。
编排模式：serial / parallel / loop 穷举。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable


INPUT_CATEGORIES = [
    ("extract", "信息提取", "从文本提取结构化信息（实体/关系/属性）"),
    ("generate", "内容生成", "生成新内容（文案/文章/摘要）"),
    ("analyze", "分析推理", "分析/推理/比较/评估"),
    ("verify", "数据核查", "核查数值/事实/合规性"),
    ("transform", "格式转换", "转换格式/重构/翻译"),
    ("orchestrate", "多步编排", "需要多步骤协同的复杂任务"),
]

ORCHESTRATION_MODES = [
    ("serial", "串行", "步骤依次执行，前步输出传下一步"),
    ("parallel", "并行", "步骤同时执行，结果汇总"),
    ("loop", "循环", "步骤重复执行，每轮结果回传"),
]


@dataclass
class StepStatus:
    step_id: int
    name: str
    status: str = "pending"
    combo_id: int = 0
    tool_name: str = ""
    input_data: dict = field(default_factory=dict)
    output_data: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id, "name": self.name, "status": self.status,
            "combo_id": self.combo_id, "tool_name": self.tool_name,
            "input_data": self.input_data, "output_data": self.output_data,
            "error": self.error,
        }


@dataclass
class ProgressMap:
    user_input: str
    category: str = ""
    category_desc: str = ""
    orchestration_mode: str = "serial"
    steps: list = field(default_factory=list)
    current_step: int = 0
    completed: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user_input": self.user_input,
            "category": self.category,
            "category_desc": self.category_desc,
            "orchestration_mode": self.orchestration_mode,
            "steps": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.steps],
            "current_step": self.current_step,
            "completed": self.completed,
        }

    def add_step(self, name: str, combo_id: int = 0, tool_name: str = "") -> StepStatus:
        step = StepStatus(step_id=len(self.steps), name=name,
                          combo_id=combo_id, tool_name=tool_name)
        self.steps.append(step)
        return step

    def mark_done(self, step_id: int, output_data: dict):
        for s in self.steps:
            if s.step_id == step_id:
                s.status = "done"
                s.output_data = output_data
                self.completed.append(step_id)
                break
        self.current_step = step_id + 1

    def mark_error(self, step_id: int, error: str):
        for s in self.steps:
            if s.step_id == step_id:
                s.status = "error"
                s.error = error
                break

    def summary(self) -> str:
        lines = [f"用户输入：{self.user_input[:100]}",
                 f"输入分类：{self.category}（{self.category_desc}）",
                 f"编排模式：{self.orchestration_mode}",
                 f"进度：{len(self.completed)}/{len(self.steps)} 步完成"]
        for s in self.steps:
            mark = {"pending": "○", "done": "●", "error": "✗"}.get(s.status, "○")
            lines.append(f"  {mark} [{s.step_id}] {s.name}")
        return "\n".join(lines)


def classify_input(user_input: str, chat: Callable) -> tuple:
    """LLM 填空分类用户输入 → PY 查表映射到穷举类别。
    返回 (category_id, category_desc)。"""
    cats_text = "\n".join(f"  - {cid}: {cdesc}" for cid, cname, cdesc in INPUT_CATEGORIES)
    prompt = f"""判断以下用户任务属于哪个类别（只输出类别 id，不要解释）：

{cats_text}

用户任务：{user_input[:500]}

只输出类别 id（如 extract / generate / analyze / verify / transform / orchestrate）。"""
    try:
        out = chat(prompt, max_tokens=20, temperature=0.1)
        out = out.strip().lower()
    except Exception:
        out = "orchestrate"

    for cid, cname, cdesc in INPUT_CATEGORIES:
        if cid in out:
            return cid, cdesc
    return "orchestrate", "需要多步骤协同的复杂任务"


def select_orchestration_mode(category: str, num_steps: int) -> str:
    """根据输入分类和步骤数选编排模式（PY 确定性）。"""
    if num_steps <= 1:
        return "serial"
    if category in ("extract", "transform", "verify"):
        return "parallel"
    if category in ("generate", "analyze"):
        return "serial"
    return "serial"