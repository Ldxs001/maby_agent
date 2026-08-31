"""Mapper — 选组合 + 设参

看 ToolSpec.input_requirements + 子任务，从 14 种组合选最合适 + 设参数 + output_limit。
LLM 填空选组合，PY 查表验证。
"""
from __future__ import annotations
import json
from .combo_registry import list_combos, get_combo, ComboSpec
from .tool_registry import ToolSpec


class Mapper:
    def __init__(self, llm, verbose: bool = False):
        self.llm = llm
        self.verbose = verbose

    def map(self, tool: ToolSpec, subtask: str, progress_map=None,
            output_limit_cfg: dict | None = None) -> tuple:
        combo = self._select_combo(tool, subtask, progress_map)
        config = self._build_config(combo, tool, subtask, output_limit_cfg)
        if self.verbose:
            print(f"  [Mapper] 子任务「{subtask[:50]}」→ 组合[{combo.id}]{combo.name}")
        return combo, config

    def _select_combo(self, tool: ToolSpec, subtask: str, progress_map) -> ComboSpec:
        combos = list_combos()
        combo_list = "\n".join(
            f"  [{c.id}] {c.name}: {c.desc} (场景: {', '.join(c.scene_tags)})"
            for c in combos
        )

        tool_info = f"工具需要：{tool.input_requirements}，产出：{tool.output_schema}"
        progress_info = ""
        if progress_map:
            progress_info = f"\n全局进度：{progress_map.summary()}"

        prompt = f"""为以下子任务选最合适的前置规范组合（只输出组合编号 1-14）。

可选组合：
{combo_list}

{tool_info}
{progress_info}

子任务：{subtask[:500]}

只输出一个数字（1-14）。"""
        msgs = [{"role": "user", "content": prompt}]
        try:
            out = self.llm.chat(msgs, max_tokens=10, temperature=0.1)
            out = out.strip()
            for ch in out:
                if ch.isdigit():
                    combo_id = int(ch)
                    combo = get_combo(combo_id)
                    if combo:
                        return combo
        except Exception:
            pass
        return combos[0]

    def _build_config(self, combo: ComboSpec, tool: ToolSpec, subtask: str,
                      output_limit_cfg: dict | None) -> dict:
        from .pipeline_model import default_config
        config = dict(default_config(combo.way_id))

        ol = combo.output_limit
        if output_limit_cfg:
            ol = {**ol, **output_limit_cfg}

        if "max_length" in ol:
            oc = config.get("output_constraints", {})
            oc["max_length"] = ol["max_length"]
            config["output_constraints"] = oc

        if "max_fields" in ol:
            slots = config.get("slots", [])
            if len(slots) > ol["max_fields"]:
                config["slots"] = slots[:ol["max_fields"]]

        return config