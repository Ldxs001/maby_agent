# silprespec-orchestrator — 前置规范编排器

> **版本：v0.1.0**
> 基于"我思故我写"方法论的多 agent 协同头部规划器。
> 根据用户任务+工具集，从 14 种穷举的原子化组合里选最合适的，
> PY 确定性组合，LLM 填空执行，输出给工具（智能体）走内部流程。

---

## 核心思想

- **LLM 只填空不决策**：决策空间穷举（14 种组合 + 编排模式穷举），LLM 在穷举域内选
- **前置规范 > 后置验证**：步骤间用 14 种组合做前置封堵
- **保留聪明剥夺自由度**：LLM 能判断场景但只能在穷举域内选
- **槽位是减法不是加法**：中间步骤前置规范受 output_limit 约束，防产出过长污染下游
- **递归自举**：编排器可以注册自己为工具，前置规范的前置规范

---

## 架构

```
用户输入 → Orchestrator(分类+选编排模式) → ProgressMap(进度地图)
  → 对每个子任务：
      Mapper(选组合+设参) → Composer(PY组合) → Executor(LLM填空+调智能体)
      → Adapter(步骤间适配，不能直通则 loop 回 Mapper 选适配组合)
  → 汇总输出
```

### 6 核心模块

| 模块 | 职责 |
|------|------|
| `orchestrator.py` | 分类用户输入 → 选编排模式 → 生成进度地图 → 分解子任务 → 依次执行 |
| `mapper.py` | 看 ToolSpec.input_requirements + 子任务 → 从 14 种组合选最合适 + 设 output_limit |
| `composer.py` | 组合名称 → recipe（PY 确定性查表）→ exec_recipe 执行 |
| `executor.py` | LLM 在 recipe 里填空 → 调智能体 API → 返回输出 |
| `adapter.py` | 步骤间格式适配，不能直通则 loop 回 Mapper 选适配组合 |
| `progress_map.py` | 进度地图 + 输入分类（穷举类别，LLM 填空） |

### 14 种穷举组合

| # | 名称 | 描述 | 场景 |
|---|------|------|------|
| 1 | pure_guide | 纯软引导 | 开放生成/续写/摘要 |
| 2 | diverge_correct | 发散纠偏 | 创意生成/文案/扩写 |
| 3 | deterministic_pin | 确定性封死 | 格式固定/编号重排 |
| 4 | detect_report | 检出上报 | 数值核查/事实核查 |
| 5 | enum_select | 可枚举选择 | 分类/标注/情绪判断 |
| 6 | condense_enum | 凝练+枚举过滤 | 标签凝练/主题提取 |
| 7 | slot_extract | 槽位提取 | 信息提取/结构化 |
| 8 | required_min | 必填最小化 | 表单填写/必填校验 |
| 9 | diverge_detect | 纠偏+检出 | 创意+核查/文案+合规 |
| 10 | diverge_condense | 纠偏+凝练 | 创意+标签/文案+主题 |
| 11 | detect_condense | 检出+凝练 | 核查+标签/数值+主题 |
| 12 | range_bound_gen | 范围约束生成 | 数值校验/范围检查 |
| 13 | exact_match_gen | 精确匹配生成 | 精确提取/严格匹配 |
| 14 | enum_filter_fabricate | 枚举+过滤编造 | 分类+防编造 |

---

## 标准化工具接口 ToolSpec

子智能体只声明"我要什么"（input_requirements）+ 产出什么（output_schema）
+ 内部保留什么（internal_prespec）。不声明"怎么凝缩"——编排器负责。

```json
{
  "name": "rag-assistant",
  "url": "http://localhost:8767",
  "input_requirements": ["query", "kb?"],
  "output_schema": ["answer", "docs", "summary", "sources"],
  "internal_prespec": ["路由", "重排序", "NLI验证"]
}
```

---

## 使用

```bash
# 启动 Web UI（默认端口 8789）
python main.py --web

# 单次任务
python main.py --query "分析这份报告并生成摘要"

# 列出 14 种组合
python main.py --list-combos

# 列出已注册工具
python main.py --list-tools

# 检测 LLM 连接
python main.py --check
```

---

## 配置

配置文件：`config.json`

| 段 | 说明 |
|----|------|
| `llm` | LLM 后端与参数 |
| `orchestrator` | 编排参数（max_steps/max_retry/output_limit） |
| `tools` | 工具注册（ToolSpec） |
| `combos` | 组合默认 output_limit |

---

## 依赖

- Python 3.11+（标准库为主）
- LLM 后端：LM Studio / Ollama / OpenAI 兼容 API

---

## License

MIT