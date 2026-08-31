"""Executor — LLM 填空 + 调智能体

构造提示词（含进度地图 + 完整用户输入 + 输入分类），
LLM 填空生成工具输入，调用工具 API（HTTP），返回工具输出。
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from .tool_registry import get_tool, ToolSpec


class Executor:
    def __init__(self, llm, verbose: bool = False):
        self.llm = llm
        self.verbose = verbose

    def execute(self, tool_name: str, input_data: dict,
                progress_map=None) -> dict:
        tool = get_tool(tool_name)
        if not tool:
            return {"success": False, "error": f"工具未注册: {tool_name}"}

        prompt = self._build_prompt(tool, input_data, progress_map)
        msgs = [{"role": "user", "content": prompt}]
        try:
            llm_out = self.llm.chat(msgs, max_tokens=800, temperature=0.3)
        except Exception as e:
            return {"success": False, "error": f"LLM 调用失败: {e}"}

        tool_input = self._parse_tool_input(llm_out, tool, input_data)

        if self.verbose:
            print(f"  [Executor] 调用 {tool_name}，输入: {json.dumps(tool_input, ensure_ascii=False)[:200]}")

        result = self._call_tool(tool, tool_input)
        return result

    def _build_prompt(self, tool: ToolSpec, input_data: dict, progress_map) -> str:
        parts = []
        if progress_map:
            parts.append(f"=== 进度地图 ===\n{progress_map.summary()}")
        parts.append(f"=== 目标工具：{tool.name} ===")
        parts.append(f"工具说明：{tool.description}")
        parts.append(f"工具需要的输入：{tool.input_requirements}")
        parts.append(f"工具产出：{tool.output_schema}")
        parts.append(f"=== 当前步骤的输入数据 ===\n{json.dumps(input_data, ensure_ascii=False)[:1000]}")
        parts.append("\n请根据以上信息，生成调用该工具所需的输入参数（JSON 格式）。只输出 JSON。")
        return "\n\n".join(parts)

    def _parse_tool_input(self, llm_out: str, tool: ToolSpec, fallback: dict) -> dict:
        try:
            start = llm_out.find("{")
            end = llm_out.rfind("}")
            if start >= 0 and end > start:
                return json.loads(llm_out[start:end + 1])
        except Exception:
            pass
        return fallback

    def _call_tool(self, tool: ToolSpec, tool_input: dict) -> dict:
        url = tool.url + tool.endpoint
        payload = json.dumps(tool_input).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            return {"success": False, "error": f"HTTP {e.code}: {err[:300]}"}
        except urllib.error.URLError as e:
            return {"success": False, "error": f"连接失败: {e.reason}"}
        except Exception as e:
            return {"success": False, "error": f"调用异常: {e}"}