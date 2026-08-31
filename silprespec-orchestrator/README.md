# silprespec-orchestrator — 多 agent 协同头部规划器

> **版本：v0.1.0** | 作者：wUwproject | 许可证：Apache 2.0
> 基于"我思故我写"方法论的多 agent 协同头部规划器。
> 根据用户任务 + 工具集，从 14 种穷举的原子化前置规范组合里选最合适的，
> PY 确定性组合，LLM 填空执行，输出交付给子智能体走各自内部流程。
> **前置规范是核心手段，不是全部** — 编排器还做任务分类、子任务分解、
> 编排模式选择、步骤间适配、进度地图贯穿、HTTP 调智能体、汇总输出。

---

## 核心思想

- **LLM 只填空不决策**：决策空间穷举（14 种组合 + 6 类输入分类 + 3 种编排模式），LLM 在穷举域内填空选，PY 查表验证
- **前置规范 > 后置验证**：每个子任务执行前先选前置规范组合（Recipe: 生成→后处理→校验→观测），约束前置告知 + 生成后 PY 校验
- **保留聪明剥夺自由度**：LLM 负责填空（聪明），PY 负责约束（剥夺自由度），两者正交
- **槽位是减法不是加法**：槽位定义"只准填这些"，多余 key 是编造，不是"多给了信息"
- **递归自举**：编排器自身的前置规范也来自 14 种组合（Adapter 适配时 loop 回 Mapper 选组合）
- **进度地图贯穿全局**：每步 LLM 都看到完整用户初始输入 + 输入分类 + 全局进度，不盲目执行

---

## 架构

```
用户输入 + 工具集
  → Orchestrator(分类→选编排模式→分解子任务→进度地图)
    → 对每个子任务：
        Mapper(选组合+设参) → Composer(PY组合+LLM填空) → Executor(调智能体API)
        → Adapter(步骤间适配，能直通则直传，不能则 loop 回 Mapper)
    → 汇总输出
```

### 6 核心模块

| 模块 | 职责 |
|------|------|
| `orchestrator.py` | 分类用户输入 → 选编排模式 → 分解子任务 → 生成进度地图 → 依次执行 → 汇总 |
| `progress_map.py` | 进度地图 + 输入分类（穷举 6 类别）+ 编排模式选择（serial/parallel） |
| `mapper.py` | 看 ToolSpec + 子任务 → 从 14 种组合选最合适（LLM 填空选编号，PY 查表验证）+ 设 output_limit |
| `composer.py` | 组合 → Recipe（PY 确定性查表）→ exec_recipe 执行（生成→后处理→校验→观测） |
| `executor.py` | LLM 填空生成工具输入 JSON → HTTP POST 调智能体 API → 返回输出 |
| `adapter.py` | 步骤间格式适配，can_accept 直通否则 loop 回 Mapper 选适配组合 |

### 三层 loop

| loop | 位置 | 语义 |
|------|------|------|
| **retry loop** | exec_recipe 内部 | 校验不通过则重新 LLM 生成（最多 max_retry 次） |
| **Adapter loop** | 步骤间适配 | 上步输出不能直通下步 → loop 回 Mapper 选适配组合 |
| **串行 loop** | _run_serial | 子任务依次执行，每步输出经 Adapter 传下一步 |

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

当前原子库下穷举完，不存在第 15 种。

---

## 标准化工具接口 ToolSpec

子智能体声明完整接口契约：输入字段（类型/必填/默认/示例/选项）、输出字段、引导示例、能力边界、内部前置规范链。编排器据此匹配前置规范 + 判定直通。

```python
ToolSpec(
    name="rag-assistant",
    url="http://localhost:8767",
    endpoint="/api/kb/query",
    description="RAG 知识库问答智能体：路由→检索→重排序→NLI验证→生成",
    input_fields=[
        FieldSpec("query", "string", required=True, description="用户问题", example="茅台酒的制作工艺"),
        FieldSpec("kb", "string", required=False, description="知识库名（留空自动路由）"),
        FieldSpec("top_k", "int", required=False, default=5, description="检索数量"),
        # ...
    ],
    output_fields=[
        FieldSpec("answer", "string", required=True, description="生成的回答"),
        FieldSpec("docs", "array", required=True, description="检索到的文档片段"),
        # ...
    ],
    examples=[
        ExampleSpec("基础问答", input={"query": "茅台酒的制作工艺"},
                    explanation="留空 kb 自动路由到白酒知识库"),
    ],
    internal_prespec=["路由(查询→最佳KB)", "向量检索", "重排序", "NLI验证", "提示词模板"],
    capabilities=["多知识库问答", "自动路由", "重排序优化", "NLI事实校验", "来源追溯"],
    limitations=["需要预建知识库", "不支持跨库join"],
)
```

**直通判定**：上一步输出的 key ⊇ 下一步工具的所有必填字段 → 零 LLM 直传；否则 Adapter loop 回 Mapper 选适配组合。

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

## Web UI（端口 8789）

四 Tab：编排 / 组合 / 工具 / 配置

| Tab | 功能 |
|-----|------|
| 编排 | 任务输入 + 工具勾选 + 执行编排 + 结果展示 |
| 组合 | 14 种穷举组合表格 + 点击查看 Recipe |
| 工具 | 三智能体完整接口契约（字段/示例/能力/局限） |
| 配置 | LLM 后端/模型/测试连接/编排参数/保存配置 |

---

## 配置

配置文件：`config.json`

| 段 | 说明 |
|----|------|
| `llm` | LLM 后端与参数（backend/base_url/model/timeout/max_tokens/temperature） |
| `orchestrator` | 编排参数（max_steps/max_retry/verbose/output_limit） |
| `combos` | 组合默认 output_limit |

工具由 `tool_registry.py` 的 `_init_default_tools()` 硬编码注册（含完整 FieldSpec/ExampleSpec），不从 config.json 读取。

---

## 依赖

- Python 3.11+（纯标准库，零第三方依赖）
- LLM 后端：LM Studio / Ollama / OpenAI 兼容 API

---

## License

Apache 2.0 © wUwproject（见 LICENSE）
