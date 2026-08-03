# Maby Agent

> **用户智能体仓库** — 由 git-sync 自动同步维护。
> 最后更新：2026-08-03

本仓库托管 wUwproject 智能体项目，由 git-sync 自动同步维护。码云（Gitee）和 GitHub 双平台同步。

> 历史提交保留于永久存档仓库 workbuddy-skills（agent/ 目录）：Gitee https://gitee.com/wUwproject/workbuddy-skills | GitHub https://github.com/Ldxs001/workbuddy-skills

---

## 智能体列表

以下为仓库中实际存在的智能体项目：

| 智能体名 | 描述 |
|----------|------|
| `Orchestrator` | Orchestrator 是一个基于本地 LLM 的 Python 智能体系统，支持: |
| `rag-assistant` | 基于 LLM 的组合式语义检索与多库路由智能体。连接本地 LLM，对你的文档库做知识问答——自动识别查询意图、拆分组合检索、跨库路由、精排与语义验证，最终给出带来源的答案。 |
| `structured-writer` | 模板驱动的大纲规划 + 串行写作引擎。基于 LLM 的结构化长文写作系统，支持两级 RAG 增强、事实自检、引用自动格式化、交互式大纲控制。 |

---

## 目录结构

```
maby_agent/
├── Orchestrator/
├── rag-assistant/
└── structured-writer/
```

---

## 维护说明

- 本仓库由 **git-sync** 技能自动维护
- README.md 由 `update_readme.py` **从仓库实际文件全量生成**，不手动编辑
- 许可证：Apache License 2.0
