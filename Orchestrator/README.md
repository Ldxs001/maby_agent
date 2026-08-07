# Orchestrator — 链驱动技能编排器

> **版本：v2.8.1**
> 基于本地 LLM 的 Python 编排器。人工编排技能链（Pipeline），LLM 只做前处理与输出整理，中间由 subprocess 确定性执行技能脚本。
> **Orchestrator 不是聊天工具。**

---

## 目录

1. [它是什么](#一它是什么)
2. [核心架构](#二核心架构)
3. [环境要求与依赖](#三环境要求与依赖)
4. [搭建步骤](#四搭建步骤)
5. [使用流程](#五使用流程)
6. [Pipeline 编排](#六pipeline-编排)
7. [工具与数据](#七工具与数据)
8. [配置](#八配置)
9. [命令行](#九命令行)
10. [常见问题](#十常见问题)

---

## 一、它是什么

Orchestrator 是一个 **链驱动编排器**，核心思想：

- **人工编排**：用户在 Pipeline Tab 从 32 个可编排技能中挑选，排成技能链（支持串行/并行/循环嵌套），配好参数，保存
- **LLM 只做两头**：
  - **前处理**：需求分析（理解任务与链的对应关系）+ 用工具处理上传数据（查库/读表/识别图片）
  - **输出整理**：把链执行结果按用户提示词整理为最终交付
- **中间死链**：Pipeline 执行是 subprocess 确定性运行技能脚本，不经过 LLM，结果真实可复现

**与聊天工具的本质区别**：无 Pipeline 时系统拒绝执行（提示先编排链）；LLM 不闲聊、不干预链执行。

---

## 二、核心架构

```
用户：编排链 → 选择链 + 任务描述 + 上传数据
  ↓
[前处理: LLM + 工具] 需求分析 / 处理数据 → 最小输入
  ↓
[Pipeline 死执行: subprocess] 确定性运行技能脚本
  ↓
[输出整理: LLM] 按用户提示词整理 → 最终交付
```

```
main.py ─── 入口（--web / --batch / --jsonl）
  │
  └── orchestrator/
      ├── web_ui.py           ← Web UI（Pipeline 编排 + 链驱动执行）
      ├── agent_loop.py       ← 编排器系统提示词（LLM 角色定义）
      ├── agent_config.py     ← 统一配置
      ├── llm_client.py       ← LLM 客户端（urllib，零依赖）
      ├── chain_engine.py     ← 流水线执行引擎（subprocess 真执行）
      ├── chain_model.py      ← 流水线数据模型
      ├── skill_scanner.py    ← 技能扫描（只显示有入口脚本的技能）
      ├── tool_base.py        ← 工具抽象基类
      ├── tools/              ← 16 个工具
      ├── chains/             ← 已保存 Pipeline
      └── input/              ← 上传文件落盘目录
```

**关键设计**：Pipeline 节点 = 可执行原子（技能脚本 CLI）。链执行时每个节点 subprocess 运行真实脚本，参数来自节点配置，前步输出作为下一步输入（stdin/文件），形成真实数据流管道。

---

## 三、环境要求与依赖

- **Python 3.11+**（标准库为主，零外部框架依赖）
- **LLM 后端**（四选一）：
  - LM Studio（默认，http://localhost:1234/v1）
  - Ollama（http://localhost:11434）
  - OpenAI 兼容 API（custom 模式）
  - 直接加载 GGUF（--direct，需 llama-cpp-python）
- **可选依赖**：
  - `pandas` / `openpyxl`：read_table 工具读取 xlsx
  - `Pillow`：image_info 工具解析图片尺寸
  - `sentence-transformers`：RAG 类技能

依赖清单见 `requirements.txt`。

---

## 四、搭建步骤

### 方式一：Windows 一键启动

```bash
setup.bat
```

自动完成：检测 Python → 安装依赖 → 启动 Web UI（固定端口 8788）→ 打开浏览器。

### 方式二：手动启动

```bash
# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 启动 Web UI
python main.py --web

# 3. 浏览器访问
# http://localhost:8788
```

### 首次使用配置

1. 打开配置 Tab，确认 LLM 后端与模型
2. 点击"测试连接"验证 LLM 可用
3. 配置页可设置用户提示词（仅影响前处理与输出格式）

---

## 五、使用流程

```
1. Pipeline Tab   人工编排技能链并保存
2. 对话 Tab       选择 Pipeline + 输入任务描述 + 可选上传文件
3. 前端处理       LLM 需求分析 + 工具处理上传数据
4. 链执行         subprocess 跑每个技能脚本
5. 输出整理       LLM 按提示词整理最终交付
```

**详细步骤**：

1. **编排**：Pipeline Tab 左侧是 32 个可编排技能，双击添加到画布；支持 seq（串行）/ par（并行组）/ loop（循环组）三种模式，节点可配参数（command/args/--key value）；编排完成后保存
2. **下达任务**：对话 Tab 顶部选择 Pipeline，输入任务描述（纯文本），可上传数据文件（csv/xlsx/db/图片/视频等）
3. **执行**：发送后系统自动完成 前处理 → 死执行 → 输出整理，界面分轮展示（需求分析 / skill-sub 优化（可选）/ 执行结果 / 最终输出）

---

## 六、Pipeline 编排

### 节点类型

| 模式 | 含义 | 说明 |
|------|------|------|
| `seq` | 串行 | 单个技能脚本，执行结果传给下一步 |
| `par` | 并行组 | 多个子节点同时执行（真并发） |
| `loop` | 循环组 | 子节点序列重复 N 遍，每轮结果回传下一轮 |

### 嵌套

loop 与 par 是两个正交维度，可任意嵌套：`loop 包 par`（每轮内并行）、`par 包 loop`（分支各自循环）。

### 节点参数

节点可配置参数，执行时转为 CLI 参数传给技能脚本：

| 参数键 | 用途 | 示例 |
|--------|------|------|
| `command` | CLI 子命令 | `calculate` |
| `args` | 位置参数列表 | `["2026"]` |
| 其他 key | `--key value` | `year=2026` → `--year 2026` |

### 技能过滤

技能列表只显示**有可执行脚本入口**的技能（32 个）。纯提示词技能（无脚本）自动过滤，不参与编排。

---

## 七、工具与数据

### 16 个工具（前处理阶段 LLM 可调用）

| 类别 | 工具 |
|------|------|
| 文件 | read_file / write_file / list_directory / copy_file / move_file / delete_file / append_file / make_dir / find_files |
| 网络 | web_fetch / web_search / python_execute |
| 数据 | db_query / read_table / image_info |
| 技能 | load_skill |

### 数据访问设计（防 token 爆炸）

- `db_query`：对 SQLite 执行 SQL 查询（仅 SELECT，写操作拦截），返回结果集而非整库
- `read_table`：csv/xlsx 摘要（列名 + 前 N 行），不整读大表
- `image_info`：图片元数据（格式/尺寸/大小），不加载像素进上下文

### 文件上传通道

- 对话框 = 纯文本任务指令
- 数据文件（csv/xlsx/db/图片/视频）经"上传文件"按钮 → `/api/upload` → 落盘 `input/` 目录
- 50MB 限制、文件名安全净化、重名自动加序号
- 上传文件的路径注入任务描述，前处理/链执行可引用

---

## 八、配置

配置文件：`data/config/settings.json`

| 段 | 字段 | 说明 |
|----|------|------|
| `llm` | backend / model / base_url / api_key / timeout / max_tokens | LLM 后端与参数 |
| `agent` | max_steps / max_retries / verbose | 执行参数 |
| `search` | backend / api_key / presets | 搜索配置 |
| `prompt` | user | 用户提示词（仅前处理+输出格式） |

**用户提示词**：仅作用于**需求分析（前处理）**和**输出格式**（如"用中文分析任务""最终输出为 Markdown 报告""表格优先"）。**不干预 Pipeline 执行**——链执行是确定性脚本运行。

---

## 九、命令行

| 命令 | 说明 |
|------|------|
| `python main.py --web` | 启动 Web UI（默认端口 8788） |
| `python main.py --web --port 8788` | 指定端口 |
| `python main.py --batch in.json out.json` | 批处理执行 Pipeline |
| `python main.py --jsonl < in.jsonl` | JSONL 管道执行 |
| `python main.py --backend ollama` | 切换 Ollama 后端 |
| `python main.py --check` | 仅检测 LLM 连接 |
| `python main.py --list-models` | 罗列本地模型 |

---

## 十、常见问题

**Q: 发送消息提示"请先编排技能链"？**
编排器不是聊天工具。需先在 Pipeline Tab 编排并保存技能链，再在对话 Tab 选择 Pipeline 执行。

**Q: 技能列表为什么只有 32 个？**
只显示有可执行脚本入口的技能。纯提示词技能无法参与链执行，自动过滤。

**Q: 上传的文件在哪？**
落盘在 `Orchestrator/input/` 目录，执行时通过路径引用。

**Q: 链执行结果准吗？**
准。Pipeline 执行是 subprocess 直接运行技能脚本，输出是脚本的真实结果，不是 LLM 生成。

---

## License

MIT（见 LICENSE）
