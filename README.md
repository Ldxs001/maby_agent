# Maby Agent

> **用户智能体仓库** — 由 git-sync 自动同步维护。
> 最后更新：2026-09-22

本仓库托管 wUwproject 智能体项目，由 git-sync 自动同步维护。码云（Gitee）和 GitHub 双平台同步。

> 历史提交保留于永久存档仓库 workbuddy-skills（agent/ 目录）：Gitee https://gitee.com/wUwproject/workbuddy-skills | GitHub https://github.com/Ldxs001/workbuddy-skills

---

## 智能体列表

以下为仓库中实际存在的智能体项目：

| 智能体名 | 描述 |
|----------|------|
| `Orchestrator` | **版本：v2.8.2** 基于本地 LLM 的 Python 编排器。人工编排技能链（Pipeline），LLM 只做前处理与输出整理，中间由 subprocess 确定性执行技能脚本。 |
| `lc-ms-group-advisor` | 给定一批化合物（名称 + 化学式 + 母离子 m/z），预测其在液相色谱上的**出峰顺序**，据此给出 MRM 分组扫描建议——哪些化合物可以放进同一个采集窗口，哪些必须分时段，避免 cycle 过长导致点数不足而漏检。 |
| `podcast-maker` | 播客制作智能体。素材进，成品出：**脚本 → 声音 → 字幕 → 画面 → 产物校验**，一条链走完。 |
| `rag-assistant` | 基于 LLM 的组合式语义检索与多库路由智能体。连接本地 LLM，对你的文档库做知识问答——自动识别查询意图、拆分组合检索、跨库路由、精排与语义验证，最终给出带来源的答案。 |
| `silprespec-emulator` | 通用实验台：从 **5 种前置规范方式**中选择/组合，对输入**真实执行**（LLM 真填空），观测填入内容、重试次数、撑满失败、重现性 + **验证指标**（量化每种后置是否真的生效）。不替用户选方式，只管执行并产出可观测结果。 |
| `silprespec-orchestrator` | **版本：v0.1.2** | 作者：wUwproject | 许可证：Apache 2.0 基于"我思故我写"方法论的多 agent 协同头部规划器。 |
| `structured-writer` | 模板驱动的大纲规划 + 串行写作引擎。基于 LLM 的结构化长文写作系统，支持两级 RAG 增强、事实自检、引用自动格式化、交互式大纲控制、快速范例复用、两级局部重规划，以及**小说模式**（章级规划→写作→章检→修复→全文三检）。 |

---

## 目录结构

```
maby_agent/
├── Orchestrator/
├── lc-ms-group-advisor/
├── podcast-maker/
├── rag-assistant/
├── silprespec-emulator/
├── silprespec-orchestrator/
└── structured-writer/
```

---

## 维护说明

- 本仓库由 **git-sync** 技能自动维护
- README.md 由 `update_readme.py` **从仓库实际文件全量生成**，不手动编辑
- 许可证：Apache License 2.0
