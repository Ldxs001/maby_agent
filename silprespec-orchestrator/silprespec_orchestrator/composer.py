"""Composer — PY 确定性组合器

从 combo_registry 查到 ComboSpec（含 Recipe），调用 atoms.exec_recipe 执行。
PY 确定性查表，无 LLM 决策（LLM 只在 exec_recipe 内部填空）。
"""
from __future__ import annotations
from .atoms import exec_recipe, Recipe
from .pipeline_model import WayConfig
from .combo_registry import get_combo, ComboSpec


class Composer:
    def __init__(self, llm):
        self.llm = llm

    def compose(self, combo_id, user_input: str, config: dict,
                task_prompt: str = "") -> dict:
        combo = get_combo(combo_id)
        if not combo:
            return {"success": False, "error": f"未知组合: {combo_id}"}

        wc = WayConfig(way=combo.way_id, config=config, task_prompt=task_prompt)
        if combo.way_id == "custom":
            wc.recipe = combo.recipe.to_dict()

        chat = self._make_chat()
        result = exec_recipe(combo.way_id, wc, user_input, chat)
        return result.to_dict()

    def compose_with_combo(self, combo: ComboSpec, user_input: str, config: dict,
                           task_prompt: str = "") -> dict:
        wc = WayConfig(way=combo.way_id, config=config, task_prompt=task_prompt)
        if combo.way_id == "custom":
            wc.recipe = combo.recipe.to_dict()

        chat = self._make_chat()
        result = exec_recipe(combo.way_id, wc, user_input, chat)
        return result.to_dict()

    def _make_chat(self):
        def chat(prompt, max_tokens=None, temperature=0.5, system_prompt=None):
            msgs = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append({"role": "user", "content": prompt})
            try:
                return self.llm.chat(msgs, max_tokens=max_tokens, temperature=temperature)
            except Exception as e:
                return f"[异常]{e}"
        return chat