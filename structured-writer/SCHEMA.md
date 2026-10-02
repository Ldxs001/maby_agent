# Structured Writer · 结构化写作智能体 — 设计方案

> 更新日期：2026-08-18（对齐 v3.1.0b5）
> 状态：实施中（双线架构：通用写作线 + 小说模式线）

## 一、项目定位

一个带交互式规划界面的结构化写作助手，**双线架构**：

- **通用写作线**：模板驱动的大纲规划 + 串行写作。用户提供主题/提示词 → LLM 生成大纲 → 用户交互调整（排序/勾选/字数/RAG/辅助知识）→ 逐节串行写作 → 输出 `.md`。
- **小说模式线**（v2.0.0b0 起）：场景配置 → 章数组 → 因果链 → 逐章子结构规划（用户确认门控）→ 写作 → 章检（4维/格式/逻辑/推理 R1）→ 修复弹窗 → 全文三检（忠实度/承诺/收束）。

- 通过 HTTP 调用 rag-assistant:8767 可选地获取 RAG 资料（通用线）
- 不依赖 RAG 亦可独立运行

## 二、文件结构

```
structured-writer/
├── main.py                          # 入口：启动 Web 服务器 + 对外写作 API
├── setup.bat                        # Windows 一键启动（双击）
├── config.json                      # 默认配置
├── requirements.txt                 # 依赖（transformers/torch，无 llama-cpp-python）
├── README.md                        # 使用说明
├── CHANGELOG.md                     # 版本更新日志
├── SCHEMA.md                        # ⬅ 本文件，方案文档
├── blueprint.json                   # PyPI 发布蓝图
│
├── structured_writer/               # ★ 智能体核心包（PyPI 包名 structured-writer-ldxs）
│   ├── __init__.py                  # 版本号唯一源（当前 3.1.0b5）
│   ├── web_ui.py                    # HTTP 服务器 + 内联 HTML/CSS/JS（~7000 行）
│   ├── config_manager.py            # 配置读写 + 模板分离存储 + 旧格式迁移
│   ├── planner.py                   # 大纲规划器（通用线）
│   ├── writer.py                    # 串行写作器（通用线，两级 RAG + 续写 + 引用后处理）
│   ├── rag_client.py                # RAG 客户端（调 rag-assistant :8767）
│   ├── llm_client.py                # LLM 客户端（纯 HTTP：LM Studio / Ollama）
│   ├── state_manager.py             # 会话状态管理 + 修复提示（_repair_hints）
│   ├── citation_validator.py        # 引用验证（扫描+报告）
│   ├── external_api.py              # 对外写作 API（/api/write，8777 独立端口）
│   ├── aux_parser.py / md2tex.py    # 辅助解析 / md→tex+pdf 编译
│   ├── novel/                       # ★ 小说模式子包
│   │   ├── novel_bridge.py          # 场景配置→章数组→因果链→outline→项目初始化→plan-chapter
│   │   ├── novel_writer.py          # 小说写作引擎（逐章 plan-chapter + 写作 + 章检门控）
│   │   ├── novel_workflow_engine.py # 章检/全文三检编排（子进程 finalize-chapter/finalize-novel）
│   │   ├── novel_repair_engine.py   # 修复引擎（T0 自动修/T1 重构、轮次、三检当场重检）
│   │   ├── novel_4dim_check.py      # 章检 4 维判定（时间/情绪/话题/角色，8B）
│   │   ├── novel_reasoning_check.py # 推理审核 R1（7B）
│   │   ├── novel_fidelity.py        # 大纲忠实度检查（全文三检）
│   │   ├── novel_pledge_check.py    # 全文承诺检查（全文三检）
│   │   ├── novel_logic_check.py     # 逻辑检查（4维 回退链）
│   │   ├── novel_entity_extractor.py / novel_behavior_extractor.py / novel_timeline*.py  # 三提取器（3B）
│   │   ├── novel_state_manager.py   # novel_state.json 读写
│   │   ├── novel_atomic_writer.py   # 原子写入 + 末行标记
│   │   ├── novel_character_registry.py / novel_continuity.py / novel_style_check.py 等
│   │   ├── model_backend.py         # 判定模型后端路由（LM Studio / transformers）
│   │   ├── lmstudio_probe.py        # LM Studio 环境探查 + lms 生命周期（load/unload/ps/import）
│   │   └── model_env_check.py       # 环境探测（transformers/torch 缺失自动安装）
│   └── plugins/                     # 数据源插件系统（base + manager + builtin/db_source）
│
└── data/                            # 运行时数据（不出库）
    ├── sessions/{id}.json           # 会话状态
    ├── outputs/{name}_{ts}/         # 生成结果（md + 图片集）
    ├── templates/user_templates.json # 用户自定义模板
    ├── examples/examples.json       # 快速范例
    ├── novel/projects/{id}/         # 小说项目（data/novel_state.json + chapters/<章>/*.txt）
    └── models/                      # transformers 模型（Qwen2.5-3B / R1-1.5B 等）
```

## 三、后端架构（v3.1.0b1 定稿：LM Studio 统一管理）

> **历史**：v3.1.0b1 起 **llama.cpp 直挂后端整体废弃**（llama-cpp-python 0.3.34 旧内核无新 MoE 优化，35B 写作仅 8 t/s vs LM Studio 20+ t/s）。判定模型也不再走 llama.cpp，统一 LM Studio 管理。

| 角色 | 模型 | 后端 | 生命周期 |
|------|------|------|---------|
| 写作/规划（35B） | qwen/qwen3.6-35b-a3b | **LM Studio**（lms load → GPU → HTTP localhost:1234） | 任务内复用，任务结束自动卸载 |
| 判定 4维（8B） | qwen3-8b | LM Studio（统一管理勾选时）/ transformers 3B（不勾） | 章检用，测完即卸（lms unload） |
| 判定 R1（7B） | deepseek-r1-distill-qwen-7b | 同上 | 同上 |
| 实体/行为/时间线提取（3B） | Qwen2.5-3B | **永远 transformers（CPU）** | 常驻复用 |
| 通用线写作 | 用户配置（LM Studio/Ollama） | HTTP | 会话内 |

- **统一管理勾选**：planner/writer 后端都是 LM Studio 时可用；勾选 → 判定走 LM Studio GPU 8B/7B；不勾 → transformers 3B/1.5B
- **ollama 场景**：写作后端为 Ollama 时统一管理禁用（判定模型仍是 LM Studio/transformers）
- **模型管理**：`lmstudio_probe.py` 封装 lms.exe（load/unload/ps/import/server）；8B/7B GGUF 位于 LM Studio 模型库（`~/.lmstudio/models/`），缺失自动下载 + lms import
- **窗口**：判定模型窗口固定 16384（R1 思考链+JSON ≈13K）；写作/规划窗口由 LM Studio 管理

## 四、核心数据流

### 通用写作线

```
用户选择模板 + 填写 meta + 发送主题
    ↓
planner.plan_outline() → LLM 按模板生成大纲 JSON（节/子结构）
    ↓
Web UI 渲染交互式大纲（勾选/排序/字数/重点/RAG/辅助知识/局部重规划）
    ↓
用户确认 → writer.generate_article() → 逐节逐子结构串行写作
    ├─ 两级 RAG 查询（节级 + 子结构级，all_rag_headers 全局共享）
    ├─ LLM 写正文（续写机制 + 【事实待核查】标记）
    └─ state_manager 更新进度
    ↓
引用后处理（citation_check）→ 事实自检汇总 → 输出 .md
```

### 小说模式线

```
场景配置（人物/时代/地点/冲突）→ 章数组（L01-L15）→ 因果链验证
    ↓
逐章循环（novel_writer）:
    ├─ 章内子结构规划（plan_chapter_subs，S01-S05，用户确认门控）
    ├─ 逐段写作（上下文注入：角色表/人格/实体关系/时间线/情绪/上章轨迹）
    ├─ 章检（finalize_chapter 子进程）: 4维(8B) + 格式 + 逻辑 + 推理R1
    │   ├─ HARD/FAIL → 修复弹窗（T0 自动修 / T1 写作模型重构 / 跳过=通过）
    │   └─ 通过 → 标章 done → 下一章
    ↓
全书所有章 done → 全文三检（finalize_novel）: fidelity 忠实度 + pledge 承诺 + ending 收束
    ↓
三检问题 → 修复弹窗（勾选修复当场重检 / 全部跳过）→ 处理完放行
```

## 五、子结构定义

### 通用线

```json
{
  "id": "s1",
  "title": "技术路线对比",
  "subtitle": "ASIC vs GPU vs FPGA",
  "summary": "对比三种主流AI芯片架构的优劣",
  "word_count": 1200,
  "is_key": true,
  "_checked": true,
  "_tmpl_key": "方法"   // 模板血缘（desc 权威要求不随改名丢失）
}
```

### 小说线（章级）

```json
{
  "id": "L01",
  "title": "深夜警报",
  "overview": "概述（≥12 字符 + 因果动词）",
  "is_key": false,
  "status": "pending",           // pending → planning → in_progress → done
  "sub_structures": {"S01": {...}, "S02": {...}, "S03": {...}, "S04": {...}, "S05": {...}}
}
```

- 小说子结构含 `tone` / `emotions` / `writing_prompt`（≥50 字符硬校验）/ `s_key`
- 末章子结构标记 `is_ending`

## 六、交互式 UI

### 通用线评审

每个节/子结构卡片：勾选（取消=完全跳过）、排序下拉、字数编辑、⭐ 重点、RAG 复选框+知识库、辅助知识「+」、章节/子结构级局部重规划、标题改名（`_tmpl_key` 血缘）。

### 小说线

- 章卡片：状态徽标、章级重规划（重做单章 title+overview）、进度
- 子结构确认面板：勾选（取消=跳过）、字数覆盖、重点标记
- **修复弹窗**（章检 HARD 时）：HARD/SOFT 级别过滤 + 子结构勾选 + T0/T1 分级 +「开始修复 / 全部跳过」+ 手动/自动模式 + 轮次提示

## 七、版本演进要点（2.0.0 后主线）

| 版本 | 里程碑 |
|------|--------|
| v2.0.0b0 | 小说模式引入（P1-P4：模板+路由 / novel 子包 / 模型层 / 检查体系） |
| v2.3.x | 续写文件真相源、实体清洗死循环、章检 HARD 拦截修复 |
| v2.4.0b0 | 重规划全链路 UI、三层分区上下文、三提取器统一（实体/行为/时间线） |
| v3.0.0b0 | 门禁体系废除、模型架构 3→2（去 bge）、全文三检全 LLM 化、三检修复弹窗复用 |
| v3.0.0b10 | 章检 prompt 哲学：内容一致 → 叙事目的/起承转合 |
| v3.0.0b17-28 | llama.cpp 实例共享/弱引用、修复引擎防重入/会话隔离/静默失败修复 |
| v3.0.0b31-32 | 跳过=通过、全文三检触发守卫（if 规划 else 全文三检） |
| v3.1.0b1 | **llama.cpp 直挂废弃 → LM Studio 统一管理**（写作/规划 35B + 判定 8B/7B） |
| v3.1.0b3-5 | 判定窗口固定 16384、ollama 禁用统一管理、8B/7B 移入模型库 + 实机验证 |

## 八、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| Web 框架 | Python http.server | 无依赖，与 rag-assistant 一致 |
| 前端 | 内联 HTML/CSS/JS | 无框架，单文件部署 |
| 写作/规划后端 | LM Studio（lms load → HTTP） | 最新 llama.cpp 内核，35B 20+ t/s |
| 判定后端 | LM Studio 8B/7B（勾选）/ transformers 3B/1.5B（不勾） | 配置驱动，GPU/CPU 双轨 |
| 提取后端 | 永远 transformers Qwen2.5-3B CPU | 常驻复用，不与推理抢显存 |
| 小说检测 | 章检（4维+格式+逻辑+R1）+ 全文三检（fidelity/pledge/ending） | 全文完结质量闭环 |
| 修复 | 弹窗裁决（勾选=重构 / 跳过=通过）+ 当场重检 | 用户语义：跳过=通过 |
| 状态保护 | 文件为真相源（章级/段级续写跳过判定） | 防 session 与磁盘分叉 |
