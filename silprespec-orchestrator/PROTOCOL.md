# Silprespec Orchestrator Protocol

> 版本: 0.1.0
> 更新: 2026-08-31

---

## 1. 概述

silprespec-orchestrator 是前置规范编排器，基于"我思故我写"方法论的多 agent 协同头部规划器。

### 技术栈

- Python 3.11+ 标准库（无外部框架依赖）
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

### `GET /`
返回 Web UI 页面（HTML）。

### `POST /api/run`
执行编排。

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

### `GET /api/combos`
列出 14 种穷举组合。

### `GET /api/tools`
列出已注册工具。

### `GET /api/config`
获取配置。

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

```json
{
  "name": "rag-assistant",
  "url": "http://localhost:8767",
  "endpoint": "/api/query",
  "input_requirements": ["query", "kb?"],
  "output_schema": ["answer", "docs", "summary", "sources"],
  "internal_prespec": ["路由", "重排序", "NLI验证"],
  "description": "RAG 知识库问答智能体"
}
```

`?` 结尾的 input_requirements 为可选参数。

---

## 6. 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 参数错误或执行失败 |