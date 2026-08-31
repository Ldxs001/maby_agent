# silprespec-orchestrator Protocol

> 版本: 0.1.0 | 作者: wUwproject | 许可证: Apache 2.0
> 更新: 2026-08-31

---

## 1. 概述

silprespec-orchestrator 是多 agent 协同头部规划器，基于"我思故我写"方法论。
根据用户任务 + 工具集，从 14 种穷举的原子化前置规范组合里选最合适的，
PY 确定性组合，LLM 填空执行，输出交付给子智能体走各自内部流程。

### 技术栈

- Python 3.11+ 标准库（零第三方依赖）
- HTTP 服务器: `http.server`（内置）
- LLM 通信: `urllib`（OpenAI 兼容 API）
- 前端: 纯 HTML/CSS/JS（无构建步骤）

---

## 2. CLI 参数

```
python main.py [OPTIONS]
```

| 参数 | 说明 |
|------|------|
| `--web` | 启动 Web UI（默认端口 8789） |
| `--port PORT` | Web UI 端口 |
| `--query TEXT` | 单次任务执行 |
| `--check` | 仅测试 LLM 连接 |
| `--list-combos` | 列出 14 种穷举组合 |
| `--list-tools` | 列出已注册工具 |
| `--backend {lm-studio,ollama,custom}` | LLM 后端 |
| `--base-url URL` | API 地址 |
| `--model NAME` | 模型名称 |
| `--verbose` | 打印详细过程 |

---

## 3. HTTP API

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | Web UI 主页面 |
| `/static/*` | GET | 静态资源（CSS/JS） |
| `/api/combos` | GET | 列出 14 种穷举组合 |
| `/api/tools` | GET | 列出已注册工具（含完整 ToolSpec） |
| `/api/config` | GET | 获取配置 |
| `/api/config` | POST | 保存配置到文件 + 更新内存 |
| `/api/llm/models` | GET | 列出 LLM 可用模型（?backend=&base_url=） |
| `/api/llm/test` | GET | 测试 LLM 连接（?backend=&base_url=&api_key=） |
| `/api/run` | POST | 执行编排 |

### POST /api/run

**请求:**
```json
{
  "message": "用户任务",
  "tools": ["rag-assistant", "structured-writer"],
  "verbose": false
}
```

**响应:**
```json
{
  "success": true,
  "result": "编排结果文本"
}
```

### POST /api/config

**请求:**
```json
{
  "llm": { "backend": "...", "model": "...", "max_tokens": 4096 },
  "orchestrator": { "max_steps": 20, "max_retry": 3 }
}
```

**响应:**
```json
{ "success": true }
```

合并到现有配置 → 落盘 config.json → 更新内存。

---

## 4. 14 种穷举组合

| # | 名称 | 描述 |
|---|------|------|
| 1 | pure_guide | 纯软引导 |
| 2 | diverge_correct | 发散纠偏 |
| 3 | deterministic_pin | 确定性封死 |
| 4 | detect_report | 检出上报 |
| 5 | enum_select | 可枚举选择 |
| 6 | condense_enum | 凝练+枚举过滤 |
| 7 | slot_extract | 槽位提取 |
| 8 | required_min | 必填最小化 |
| 9 | diverge_detect | 纠偏+检出 |
| 10 | diverge_condense | 纠偏+凝练 |
| 11 | detect_condense | 检出+凝练 |
| 12 | range_bound_gen | 范围约束生成 |
| 13 | exact_match_gen | 精确匹配生成 |
| 14 | enum_filter_fabricate | 枚举+过滤编造 |

---

## 5. ToolSpec 标准化接口

子智能体声明完整接口契约：

```python
ToolSpec(
    name="rag-assistant",
    url="http://localhost:8767",
    endpoint="/api/kb/query",
    description="RAG 知识库问答智能体",
    input_fields=[
        FieldSpec(name="query", type="string", required=True, description="用户问题"),
        FieldSpec(name="kb", type="string", required=False, description="知识库名"),
        FieldSpec(name="top_k", type="int", required=False, default=5, description="检索数量"),
    ],
    output_fields=[
        FieldSpec(name="answer", type="string", required=True, description="生成的回答"),
        FieldSpec(name="docs", type="array", required=True, description="检索到的文档片段"),
    ],
    examples=[ExampleSpec(title="基础问答", input={"query": "..."}, explanation="...")],
    internal_prespec=["路由", "向量检索", "重排序", "NLI验证"],
    capabilities=["多知识库问答", "自动路由"],
    limitations=["需要预建知识库"],
)
```

**直通判定**：`can_accept(available_keys)` — 上一步输出的 key 包含下一步工具所有必填字段 → 直通。

---

## 6. 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 参数错误或执行失败 |
