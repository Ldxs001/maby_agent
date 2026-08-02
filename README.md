<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 wUwproject
-->

# Maby Agent — 智能体仓库

> wUwproject 开发的领域智能体集合。Apache License 2.0。

## 仓库定位

本仓库独立托管 wUwproject 的智能体（Agent）项目。2026-08-02 自 `workbuddy-skills` 仓库的 `agent/` 目录独立拆分而来。

**历史提交说明：** 本仓库为全新初始化，不带历史提交。所有历史记录保留于永久存档仓库：

- Gitee: https://gitee.com/wUwproject/workbuddy-skills （`agent/` 目录）
- GitHub: https://github.com/Ldxs001/workbuddy-skills （`agent/` 目录）

## 智能体列表

| 智能体 | 说明 |
|--------|------|
| **Orchestrator** | 链驱动 Pipeline 编排引擎（skill-sub 优化 + seq/par/loop 真执行） |
| **rag-assistant** | 本地知识库智能体（组合式检索 + SM3 去重 + 多库路由 + 自修正循环） |
| **structured-writer** | 结构化写作智能体（模板驱动大纲规划 + 串行写作 + 两级 RAG + 引用自动格式化） |

## 目录结构

```
maby_agent/
├── LICENSE                  # Apache License 2.0
├── README.md
├── Orchestrator/            # 链驱动编排引擎
├── rag-assistant/           # 本地知识库智能体
└── structured-writer/       # 结构化写作智能体
```

## 使用方式

各智能体目录内自带 README / PROTOCOL / SCHEMA 说明文档，按其自身说明运行。

## 维护约定

- 本仓库由 wUwproject 维护，Gitee / GitHub 双平台同步
- 智能体版本号从各自 `__init__.py` 的 `__version__` 读取
- 更新流程：修改 → 测试 → 双端推送
