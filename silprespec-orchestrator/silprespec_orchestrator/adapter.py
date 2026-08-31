"""Adapter — 步骤间适配

检查上一步输出能否直通下一步。不能则 loop 回 Mapper 选适配组合，
用 Composer 执行适配，返回适配后的输入。受 output_limit 约束。
"""
from __future__ import annotations
import json
from .tool_registry import get_tool, ToolSpec
from .mapper import Mapper
from .composer import Composer


class Adapter:
    def __init__(self, llm, verbose: bool = False):
        self.llm = llm
        self.verbose = verbose
        self.mapper = Mapper(llm, verbose)
        self.composer = Composer(llm)

    def adapt(self, prev_output: dict, next_tool: ToolSpec,
              progress_map=None) -> dict:
        available_keys = list(prev_output.keys())
        if next_tool.can_accept(available_keys):
            if self.verbose:
                print(f"  [Adapter] 直通 {next_tool.name}")
            return prev_output

        if self.verbose:
            print(f"  [Adapter] 需适配：{available_keys} → {next_tool.input_requirements}")

        adapted = self._adapt_via_combo(prev_output, next_tool, progress_map)
        return adapted

    def _adapt_via_combo(self, prev_output: dict, next_tool: ToolSpec,
                         progress_map) -> dict:
        from .combo_registry import get_combo

        prev_text = json.dumps(prev_output, ensure_ascii=False)[:1000]
        adapt_subtask = (f"把上一步输出适配为 {next_tool.name} 所需的输入格式。"
                         f"\n上一步输出：{prev_text}"
                         f"\n目标工具需要：{next_tool.input_requirements}")

        combo, config = self.mapper.map(next_tool, adapt_subtask, progress_map)

        result = self.composer.compose(combo.id, prev_text, config,
                                        task_prompt=f"适配为{next_tool.name}的输入")

        if result.get("success"):
            filled = result.get("filled", {})
            if isinstance(filled, dict):
                if "output" in filled:
                    return {"input": filled["output"]}
                return filled
            return {"input": str(filled)}

        return {"adapted_from": prev_output, "target": next_tool.name,
                "adapt_error": result.get("error", "适配失败")}