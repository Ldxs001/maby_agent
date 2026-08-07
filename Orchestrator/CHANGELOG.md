# Orchestrator 更新日志

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号遵循语义版本控制（`orchestrator/__init__.py` 唯一源）。

---

## [2.8.1] - 2026-08-07

### 文档补齐（与 rag-assistant 对齐）
- **新增 README.md**（239 行，8.6KB）：它是什么/核心架构/环境要求/搭建步骤/使用流程/Pipeline 编排/工具与数据/配置/命令行/常见问题——对齐 RAG 的完整手册结构
- **重写 llms.txt**：从过时的 v1.1.0（ReAct 循环/--query/tkinter GUI/rag_tool 等已删内容）更新为 v2.8.1（链驱动架构/16 工具/上传通道/32 技能）
- 版本三处同步：`__init__.py` / README.md / llms.txt → **2.8.1**

### 文档对比结论（vs rag-assistant）
- 必要缺口 README.md 已补齐
- EXTERNAL_API.md 不适用（Orchestrator 无外部 API 模块，PROTOCOL.md 已覆盖 HTTP API）
- blueprint_rag.json 为可选（用 test_batch 代替）

---

## [2.8.0] - 2026-08-07

### 架构清理：彻底移除普通对话（ReAct 聊天），编排器定位纯化
- **Web UI**：无 Pipeline 时不再回退 `agent.run` 闲聊，改为提示"请先在 Pipeline Tab 编排技能链并保存"；`/api/chat` GET 返回空（无对话历史，非聊天工具）
- **CLI**：删除 `interactive()` 交互模式与 `--query` 单次问答；无参数启动改为提示使用方式（--web / --batch / --jsonl）
- **批处理/JSONL**：从 `agent.run("执行技能")` 改为 chain_engine 真执行（`_run_skill_node` subprocess 跑脚本）
- **agent_loop.py 重写**：删除 Agent 类 / ToolRegistry / ReAct 循环 / REACT_SYSTEM_PROMPT，替换为 **ORCHESTRATOR_SYSTEM_PROMPT**（编排器本体提示词：明确 LLM 只做前处理 + 输出整理，不干预 Pipeline 执行，非聊天机器人）
- **__init__.py**：移除 Agent / ToolRegistry / ConversationMemory / WorkingMemory 导出

### 用户提示词文案更新（消除误导）
- UI 文案改为："仅作用于前处理与输出格式：如「用中文分析任务」「最终输出为 Markdown 报告」「表格优先」。不干预 Pipeline 执行。"
- 补充说明：Pipeline 执行是确定性脚本运行，不受提示词影响

### 实测
- 无 pipeline 发消息 → 拒绝并提示编排链 ✅
- 系统提示词 → 编排器本体（1037 字符，含前处理/输出整理，无 JSON 动作协议）✅
- 全量 py_compile PASS

---

## [2.7.0] - 2026-08-07

### 核心架构：LLM 只做两头（前处理 + 输出整理），中间死链不碰
- **需求分析注入用户提示词**：`_round_analysis` prompt 追加 user_prompt（用户对理解任务/处理数据的偏好）
- **需求分析注入工具清单**：提示 LLM 可用 db_query / read_table / image_info / read_file 等 16 个工具，处理上传数据时先调工具取必要信息（不假设内容、不整读大文件）
- **新增输出整理环节**：链执行后 `_finalize_output` 按 user_prompt 整理最终交付（格式/样式）；明确"不保证完全按提示词执行"——若 Pipeline 输出已是最终形态（如已生成文件），如实呈现不重复加工
- 链流程：需求分析(带工具) → skill-sub 优化 → 执行 → **最终输出**

### 新增：3 个数据访问工具（data_tool.py，避免 token 爆炸）
- `db_query`：SQLite SQL 查询（仅 SELECT，写操作拦截），返回结果集而非整库
- `read_table`：csv/xlsx 摘要读取（列名 + 前 N 行），不整读大表
- `image_info`：图片元数据（格式/尺寸/大小），不加载像素进上下文
- 注册到 main.py / __init__.py

### 实测
- db_query：SELECT 返回结果集 ✅；DROP TABLE 被拦截 ✅
- read_table：csv 列名+预览 ✅
- image_info：PNG 格式/大小 ✅

---

## [2.6.0] - 2026-08-07

### 新增：文件上传通道（对话框保持纯文本，数据走上传）
- **后端 `/api/upload`**：base64 JSON 接收 → 存 `input/` 目录 → 返回绝对路径
  - 文件名安全净化（路径穿越 `..\..\evil.py` → `evil.py`）
  - 大小限制 50MB、重名自动加序号（sales.csv → sales_1.csv）
- **前端**：对话工具栏新增"上传文件"按钮（多文件），上传后以附件 chips 显示，可移除
- **附件注入**：chat 请求携带 `attachments`，后端把文件路径列表注入任务描述（`[已附加文件]`），需求分析/执行可引用
- 输入定位：对话框 = 纯文本任务指令；csv/xlsx/db/图片/视频等数据 = 上传通道 → `input/` → 路径引用

### 实测
- 上传 sales.csv → `input/sales.csv`（46 字节）✅
- 重名上传 → `sales_1.csv` ✅
- `..\..\evil.py` → 净化 `evil.py` ✅

---

## [2.5.0] - 2026-08-07

### 修复
- **配置持久化真凶**：`main.py` `make_llm` 用硬编码 `qwen/qwen3.6-35b-a3b` 覆盖 settings.json 保存的模型 → 改为优先用已持久化配置（`saved_model`/`saved_base`），CLI 参数可覆盖
- **系统提示词真实化**：配置页 `system_prompt_raw` 从硬编码占位文本改为返回真实 `REACT_SYSTEM_PROMPT`（741 字符）
- **会话持久化**：`/api/chat` GET 返回 agent 内存真实历史（`get_recent(10)`），前端初始化时拉取并渲染 → 刷新页面聊天记录不再丢失

### 技能过滤放宽（误杀修复）
- 入口匹配新增"排除纯配置脚本"规则：`settings.py`/`config.py`/`__init__.py`/`_paths.py` 等不算入口；其余任意 .py/.sh/.bat 视为可执行入口
- 可编排技能：9 → **32 个**（everything-search-breadmemory、latex-modular、memory-pet、local-rag-builder、arxiv-watcher 等回归）

### 实测
- 技能 API：32 个
- system_prompt_raw：真实 REACT（含 final_answer），741 字符
- 会话：发 1 条消息 → GET 返回 2 条历史
- 配置：保存 model → settings.json 落盘确认

---

## [2.4.0] - 2026-08-07

### 修复
- **loop 真循环**：web_ui `_execute_tree` 的循环组改为"每轮子执行 → 提取结果 → 回传下一轮输入"（此前输出不回传，循环 = 重复 N 次相同独立执行）。与 chain_engine 行为对齐
- **技能过滤升级**：从"scripts/ 有任意 .py 即显示"改为"能匹配到可执行主脚本入口才显示"（用与执行引擎相同的入口匹配逻辑）→ 33 → 9 个真正可编排技能；带辅助脚本无入口的（如仅 settings.py）不再显示

### 实测
- loop ×2 循环组：两轮真执行，每轮独立 subprocess，234ms
- 过滤后 9 个技能均能匹配主脚本入口（workday-calendar → workday_calendar.py 等）

---

## [2.3.0] - 2026-08-07

### 核心改造：链执行从"LLM 伪执行"变为"subprocess 真执行"
- **web_ui 接入真执行**：`_execute_single_skill` / `_execute_chain_step` 不再把 SKILL.md 喂给 LLM 编输出，改为调用 chain_engine 的脚本发现 + subprocess 运行
- **脚本匹配修复**：`_get_main_script` 支持下划线/连字符归一化（`workday-calendar` ↔ `workday_calendar.py`），并新增 cli.py 标准入口与模糊兜底 → 可执行技能从 3/66 提升到 33/66
- **参数传递**：节点 params 转 CLI 参数（`command`/`args`/`--key value`），前步输出作为 stdin 传入 → 链的"能输入/能输出"闭环

### 新增：6 个通用文件衔接工具（file_ops_tool.py）
- `copy_file` / `move_file` / `delete_file` / `append_file` / `make_dir` / `find_files`
- 全部带路径安全校验：空路径拦截、系统关键目录拦截（桌面/文档/下载/.workbuddy/盘符根）、目标已存在拦截
- 注册到 main.py 与 __init__.py

### 编排对象过滤
- Pipeline 技能列表只展示可执行技能（scripts/ 有 .py/.sh/.bat），纯提示词技能不参与编排 → 66 → 33

### 实测验证
- workday-calendar `calculate 2026` → 真实 JSON（261 工作日，latency 123ms）
- 双技能链 workday-calendar → color-toolkit-turn → 两段真实脚本输出（254ms）
- 6 个文件工具功能 + 安全拦截全部 PASS

---

## [2.2.0] - 2026-08-07

### 变更（端口）
- **默认端口 8766 → 8788**：8765-8767 被 rag-assistant 占用（主界面/配置页/外部API 三端口），8770 被 structured-writer 占用，8766 与 RAG 配置页正面冲突 → 迁移到 8788（87xx 段内空闲，实测无冲突）
- **setup.bat 重写**：`--port auto` 随机端口 → 固定 8788；kill 逻辑与端口对应；等待逻辑从"等待 server.port 文件"改为"轮询端口监听"；根治 `server.port` 内容损坏（4347343473 系 auto 端口 + 多进程并发写坏）

### 清理
- **归档 tkinter 界面**：`orchestrator/gui_agent.py`（935 行，tkinter 桌面 GUI）为死代码（main.py 零引用）→ `git mv` 至 `archive/`，保留历史
- **文档同步**：llms.txt 移除 gui_agent 条目、requirements.txt 移除 tkinter 注释

---

## [2.1.0] - 2026-08-07

### 修复（致命缺陷）
- **链驱动对话崩溃**：`web_ui.py` `_handle_chat_post` 引用未定义变量 `full_output`/`chain_info` + HTTP 双响应 → 改为单次响应，先保存优化链再发送
- **批处理/JSONL 假执行**：`main.py` `--batch`/`--jsonl` 传入 `agent=None` 导致只打印步骤不执行 → 改为创建 LLM+Agent 真实执行，LLM 不可用时降级为步骤规划
- **配置加载失效**：`main.py` 加载不存在的 `agent_config.json` → 统一加载 `data/config/settings.json`（此前用户保存的配置启动后全部丢失）
- **配置保存不生效**：`_recreate_llm()` 只重建 Handler 的 LLM 不更新 agent → 同步 `agent.llm`；`start_web_ui` 与 agent 共用同一 LLM 实例

### 修复（工程规范）
- **单线程服务器**：`socketserver.TCPServer` → `ThreadingTCPServer` + `daemon_threads`，chat 阻塞不再卡死全站
- **外部 CDN**：`cdn.jsdelivr.net` marked.min.js → 本地 `static/marked.min.js`（内联资源，断网可用）
- **emoji 清理**：前后端输出 ✅🔧🏁⚠️🔍 → 文本标记（[OK]/[FIX]/[MS]/[WARN]/[S]）
- **静态 MIME 修复**：`mime[""]` 键覆盖写法 → 规范扩展名映射

### 修复（技能加载）
- **技能扫描断裂**：`skill_scanner` 默认路径只查项目内 `skills/`（不存在）→ 两级回退：项目内 `skills/` → `~/.workbuddy/skills`
- **main.py 技能目录**：`CFG_SKILL_DIRS` 增加 `~/.workbuddy/skills` 回退，Pipeline Tab 技能列表不再为空

### 变更
- **默认端口**：8765 → 8766（与 rag-assistant 错开，避免互杀进程）

---

## [1.1.0] - 2026-07-10

### 重构
- **项目迁移**：从 `D:\Code~\PythonProject\local_agent\` 迁移到 `C:\Users\sm001\WorkBuddy\Orchestrator\`
- **统一结构**：所有核心代码移入 `orchestrator/` 子包，`run_agent.py` → `main.py`
- **配置整理**：`settings.json` → `data/config/settings.json`，`working_memory.json` → `data/memory/working_memory.json`
- **文档补齐**：新增 `llms.txt`、`CHANGELOG.md`、`LICENSE`、`requirements.txt`
- **名称统一**：所有 import 从 `local_agent.xxx` → `orchestrator.xxx`
