"""Orchestrator — 编排器主控

根据用户任务+工具集，从 14 种穷举组合里选最合适的，
PY 确定性组合，LLM 填空执行，输出给工具（智能体）走内部流程。

流程：
  用户输入 → 分类(穷举) → 选编排模式(穷举) → 生成进度地图
    → 分解子任务(LLM填空)
    → 对每个子任务：
        Mapper(选组合+设参) → Composer(PY组合+LLM填空) → Executor(调智能体)
        → Adapter(步骤间适配，不能直通则 loop 回 Mapper)
    → 汇总输出
"""
from __future__ import annotations
import json
from typing import Callable

from .llm_client import LLMClient
from .progress_map import ProgressMap, classify_input, select_orchestration_mode
from .mapper import Mapper
from .composer import Composer
from .executor import Executor
from .adapter import Adapter
from .tool_registry import get_tool, list_tools, load_tools_from_config


class Orchestrator:
    def __init__(self, llm: LLMClient, config: dict, verbose: bool = False):
        self.llm = llm
        self.config = config
        self.verbose = verbose
        self.mapper = Mapper(llm, verbose)
        self.composer = Composer(llm)
        self.executor = Executor(llm, verbose)
        self.adapter = Adapter(llm, verbose)
        load_tools_from_config(config)

    def run(self, user_input: str, tool_names: list | None = None) -> str:
        chat = self._make_chat()

        category, category_desc = classify_input(user_input, chat)
        if self.verbose:
            print(f"[Orchestrator] 输入分类：{category}（{category_desc}）")

        tools = self._select_tools(tool_names)
        subtasks = self._decompose(user_input, category, tools, chat)
        if self.verbose:
            print(f"[Orchestrator] 分解为 {len(subtasks)} 个子任务")

        mode = select_orchestration_mode(category, len(subtasks))
        if self.verbose:
            print(f"[Orchestrator] 编排模式：{mode}")

        pm = ProgressMap(user_input, category, category_desc, mode)
        for st in subtasks:
            pm.add_step(st["name"], tool_name=st.get("tool", ""))

        if mode == "parallel":
            results = self._run_parallel(subtasks, pm)
        else:
            results = self._run_serial(subtasks, pm)

        return self._summarize(user_input, pm, results)

    def _make_chat(self) -> Callable:
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

    def _select_tools(self, tool_names: list | None) -> list:
        if tool_names:
            return [get_tool(n) for n in tool_names if get_tool(n)]
        return list_tools()

    def _decompose(self, user_input: str, category: str, tools: list,
                   chat: Callable) -> list:
        if len(tools) == 0:
            return [{"name": "直接处理", "desc": user_input, "tool": ""}]

        tool_list = "\n".join(
            f"  - {t.name}: {t.description}（需要{t.input_requirements}）"
            for t in tools
        )
        prompt = f"""把以下任务分解为子任务，每个子任务指定一个工具执行。

可用工具：
{tool_list}

任务：{user_input[:800]}

输出 JSON 数组，每个元素：{{"name":"步骤名","desc":"子任务描述","tool":"工具名"}}。只输出 JSON。"""
        try:
            out = chat(prompt, max_tokens=500, temperature=0.3)
            start = out.find("[")
            end = out.rfind("]")
            if start >= 0 and end > start:
                subtasks = json.loads(out[start:end + 1])
                if isinstance(subtasks, list) and subtasks:
                    return subtasks
        except Exception:
            pass
        return [{"name": "执行任务", "desc": user_input, "tool": tools[0].name}]

    def _run_serial(self, subtasks: list, pm: ProgressMap) -> list:
        results = []
        prev_output = {}
        for i, st in enumerate(subtasks):
            step = pm.steps[i]
            tool_name = st.get("tool", "")
            tool = get_tool(tool_name) if tool_name else None

            if tool and prev_output:
                adapted = self.adapter.adapt(prev_output, tool, pm)
                step.input_data = adapted
            else:
                step.input_data = {"query": st["desc"]}

            if tool:
                combo, config = self.mapper.map(tool, st["desc"], pm)
                step.combo_id = combo.id
                comp_result = self.composer.compose(combo.id, st["desc"], config)
                exec_input = comp_result.get("filled", step.input_data)
                if isinstance(exec_input, dict) and "output" in exec_input:
                    exec_input = {"query": exec_input["output"]}
                exec_result = self.executor.execute(tool_name, exec_input, pm)
            else:
                exec_result = {"result": st["desc"]}

            if exec_result.get("success", True):
                pm.mark_done(step.step_id, exec_result)
            else:
                pm.mark_error(step.step_id, exec_result.get("error", "未知错误"))
            results.append(exec_result)
            prev_output = exec_result
        return results

    def _run_parallel(self, subtasks: list, pm: ProgressMap) -> list:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = [None] * len(subtasks)

        def _run_one(idx, st):
            step = pm.steps[idx]
            tool_name = st.get("tool", "")
            tool = get_tool(tool_name) if tool_name else None
            step.input_data = {"query": st["desc"]}
            if tool:
                combo, config = self.mapper.map(tool, st["desc"], pm)
                step.combo_id = combo.id
                comp_result = self.composer.compose(combo.id, st["desc"], config)
                exec_input = comp_result.get("filled", step.input_data)
                if isinstance(exec_input, dict) and "output" in exec_input:
                    exec_input = {"query": exec_input["output"]}
                return self.executor.execute(tool_name, exec_input, pm)
            return {"result": st["desc"]}

        with ThreadPoolExecutor(max_workers=min(4, len(subtasks))) as pool:
            futures = {pool.submit(_run_one, i, st): i for i, st in enumerate(subtasks)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    res = fut.result()
                    results[idx] = res
                    pm.mark_done(idx, res)
                except Exception as e:
                    results[idx] = {"error": str(e)}
                    pm.mark_error(idx, str(e))
        return results

    def _summarize(self, user_input: str, pm: ProgressMap, results: list) -> str:
        lines = [f"=== 编排结果 ===",
                 f"用户任务：{user_input[:200]}",
                 f"输入分类：{pm.category}（{pm.category_desc}）",
                 f"编排模式：{pm.orchestration_mode}",
                 f"完成步骤：{len(pm.completed)}/{len(pm.steps)}",
                 ""]
        for i, res in enumerate(results):
            step = pm.steps[i]
            mark = "●" if step.status == "done" else "✗"
            combo_tag = f"[组合{step.combo_id}]" if step.combo_id else ""
            tool_tag = f"[{step.tool_name}]" if step.tool_name else ""
            lines.append(f"  {mark} [{i}] {step.name} {combo_tag}{tool_tag}")
            if res:
                summary = json.dumps(res, ensure_ascii=False)[:300]
                lines.append(f"      → {summary}")
            if step.error:
                lines.append(f"      ✗ {step.error}")
        return "\n".join(lines)