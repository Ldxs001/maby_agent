# Orchestrator — 平台编排型智能体

> 基于本地 LLM 的 Python 智能体系统 / Skill Pipeline Orchestrator。运行时动态加载本地技能，自动编排流水线，支持 ReAct 循环、多 LLM 后端与工具系统。
>
> 版本：2.0.0 | 作者：wUwproject | 许可证：Apache 2.0

---

## 目录

- [一、它是什么](#一它是什么)
- [二、环境要求与依赖](#二环境要求与依赖)
- [三、启动方式](#三启动方式)
- [四、核心能力](#四核心能力)
- [五、架构总览](#五架构总览)
- [六、配置](#六配置)
- [七、协议说明](#七协议说明)

---

## 一、它是什么

Orchestrator 是一个**平台编排型智能体**。它不内嵌任何技能代码，而是运行时扫描 `~/.workbuddy/skills/` 下的 SKILL.md，动态理解和加载技能，自动编排流水线。

核心设计理念：**技能是积木，编排是图纸，LLM 是执行器**。

---

## 二、环境要求与依赖

| 依赖 | 说明 | 获取方式 |
|------|------|---------|
| **Python** | 3.11+（标准库，无外部框架依赖） | python.org |
| **LLM 推理服务** | LM Studio / Ollama / OpenAI 兼容 API / 直接 GGUF 加载 | LM Studio / Ollama 官网 |
| **本地技能** | 可选，动态加载 `~/.workbuddy/skills/` 下的技能 | 随 WorkBuddy 环境安装 |

---

## 三、启动方式

### 一键安装启动

```bash
setup.bat    # 检测环境 → 装依赖 → 选择模式
```

### 手动启动

| 命令 | 说明 |
|------|------|
| `python main.py` | 默认 LM Studio 后端 |
| `python main.py --web` | Web UI（对话 + 配置 + Pipeline，默认端口 8765） |
| `python main.py --query "你的问题"` | 单次问答 |
| `python main.py --backend ollama` | Ollama 后端 |
| `python main.py --check` | 仅检测后端连接 |

---

## 四、核心能力

### 1. 多 LLM 后端

| 后端 | 启动方式 | 说明 |
|------|---------|------|
| **LM Studio** | 默认 | http://localhost:1234/v1 |
| **Ollama** | `--backend ollama` | 本地推理 |
| **Custom API** | `--backend custom --base-url URL --model NAME` | OpenAI 兼容 |
| **Direct GGUF** | `--direct` | 直接加载 GGUF，自动发现 `~/.lmstudio/models/`，GPU/CPU 分摊 |

### 2. 动态技能加载

- 扫描 `~/.workbuddy/skills/` 下的 SKILL.md
- 运行时通过 `load_skill` 工具加载任意技能，无需写适配代码
- 自动解析 frontmatter、参数 Schema、触发词

### 3. Skill Pipeline 编排

- 链式 / 并行节点、循环和条件分支
- GUI 可视化编排界面（tkinter）

### 4. 工具系统

| 工具 | 说明 |
|------|------|
| `skill_loader` | 动态技能加载器 |
| `file_tool` | 文件读/写/列出目录 |
| `web_tool` | 网页抓取 / 搜索 |
| `rag_tool` | 调用 local-rag-builder 技能检索 |

### 5. ReAct 智能体循环

思考 → 行动 → 观察 → 重复（最多 20 步）。

---

## 五、架构总览

```
main.py ─── 入口（CLI 交互 / 单次问答 / 检测）
  │
  └── orchestrator/           ← 智能体核心层
      ├── agent_loop.py       ← ReAct 决策循环（Think → Act → Observe）
      ├── agent_config.py     ← 代码配置（dataclass + property）
      ├── llm_client.py       ← LLM 客户端（urllib，零依赖）
      ├── memory.py           ← 两层记忆（会话 + 持久化工作记忆）
      ├── model_manager.py    ← 统一模型管理器（发现/加载/卸载/GPU仲裁）
      ├── direct_llm_client.py ← 直接加载 GGUF（GPU/CPU 分摊）
      ├── gui_agent.py        ← tkinter 流水线编排 GUI
      ├── chain_engine.py     ← 流水线执行引擎
      ├── chain_model.py      ← 流水线数据模型
      ├── skill_scanner.py    ← 技能目录扫描器
      ├── tool_base.py        ← 工具抽象基类
      │
      ├── tools/              ← 工具集合
      │   ├── skill_loader.py ← 动态技能加载器
      │   ├── file_tool.py    ← 文件操作工具
      │   ├── web_tool.py     ← 网络工具
      │   └── rag_tool.py     ← RAG 检索工具
      │
      ├── chains/             ← 预编排流水线定义
      └── working_memory.json ← 工作记忆持久化
```

---

## 六、配置

| 文件 | 作用 |
|------|------|
| `data/config/settings.json` | 运行时配置（技能路径、超时等） |
| `data/memory/working_memory.json` | 工作记忆持久化 |
| `main.py` 配置区 | 代码级默认配置（后端、模型、参数） |

---

## 七、协议说明

- 完整协议文档见 `PROTOCOL.md`（CLI 参数 / HTTP API / Pipeline 数据格式 / 配置 Schema / 批处理格式 / JSONL 管道格式 / 退出码）
- 许可证：Apache License 2.0
