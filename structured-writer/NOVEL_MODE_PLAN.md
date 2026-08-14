# 小说模式（NOVEL MODE）功能新增方案

> 版本：2.3 | 状态：**已实施（structured-writer v2.3.0b0，含消息持久化+字数差异化+备份全局化+叙事视角强指令+轮询实时刷新，待端到端验证后发正式版）** | 关联项目：structured-writer
> 目标：不改变通用写作流程，新增独立「小说线」，继承 novel-weaver（已停更）15 项小说能力资产

---

## 〇、设计总纲（三条铁律）

1. **模板是开关，代码是能力** — 选择「小说」模板只是触发小说线路由，全部能力在 novel/ 子包代码里
2. **模型权力在用户** — 模板不绑定任何模型决策；模型选择、安装、开关全在配置面板，无模型自动降级
3. **审核必须独立** — 推理审核用小模型（R1-1.5B）独立于写作模型，禁止"35B 自审自己"（自我偏好偏差）；语义检查用向量模型（bge，确定性计算无幻觉）

---

## 一、总体架构（四层）

```
选择模板
   ↓
[L3 路由层] 模板含 novel.mode? ──否──→ 通用线（原流程一字不动）
   ↓是
[L1 模板层]  小说模板：meta + content(kind=setting/chapters) + style + logic
   ↓
[L2 增强层]  novel/ 子包：场景配置 → 章数组 → 因果链 → 增强写作 → 六检三检
   ↓
[L4 模型层]  三轨：LM Studio 35B(规划/写作) + bge(语义) + R1-1.5B(推理审核)
```

| 层 | 内容 | 落点 | 工作量 |
|---|---|---|---|
| L1 模板层 | 内置「小说」模板 | `config_manager.py` DEFAULT_TEMPLATES + schema 放行 | 纯数据 + 几行 |
| L2 增强层 | 场景配置/增强写作/六检三检 | 新增 `structured_writer/novel/` 子包 | 主体工作 |
| L3 路由层 | novel.mode 检测 → 走小说分支 | planner/writer/web_ui 加分支 | 每处 3-5 行 |
| L4 模型层 | bge/R1 加载 + 降级 | novel/ 子包内 | 移植即用 |

---

## 二、L1 模板层：小说模板设计

### 2.1 模板 JSON（进 DEFAULT_TEMPLATES）

```json
{
  "name": "小说",
  "novel": {"mode": true},
  "meta": [
    {"name": "标题", "show_label": false, "desc": "小说标题", "source": "auto"},
    {"name": "题材", "show_label": true, "desc": "科幻/武侠/悬疑/都市/奇幻/历史等，必须填写", "source": "user"},
    {"name": "篇幅", "show_label": true, "desc": "短篇/中篇/长篇——决定每子结构字数目标（短篇1000-1500、中篇1500-2000、长篇2000-4000）", "source": "user"},
    {"name": "叙事视角", "show_label": true, "desc": "第一人称/第三人称有限/第三人称全知/第二人称，留空由AI按题材推断", "source": "auto"},
    {"name": "署名", "show_label": true, "desc": "作者署名", "source": "user"}
  ],
  "content": [
    {"name": "世界观设定", "show_label": false, "type": "section", "kind": "setting",
     "desc": "【设定节点·不输出正文】生成时代背景、核心地点、风土人情、核心冲突，写入小说状态"},
    {"name": "人物表", "show_label": false, "type": "leaf", "kind": "setting",
     "desc": "【设定节点·不输出正文】登记主要角色：姓名/身份/人格(MBTI+荣格原型)/动机/别名，写入小说状态"},
    {"name": "正文", "show_label": false, "type": "section", "kind": "chapters",
     "desc": "【多章节点·由AI展开】按场景配置生成 L01-L15 章结构，每章含概述；末章自动为结局章(is_ending)。章内子结构含情绪基调与写作命题(writing_prompt≥50字)"}
  ],
  "style": "文风六字段模板：叙事视角=meta.叙事视角；时态=过去式为主；句式=长短句交错；词汇=文学化；描写=中等密度(环境描写每段≤2句)。创作铁律：1)show don't tell，情绪通过人物行为/生理反应表达，禁止纯抒情段落；2)对话必须符合角色身份与人格；3)禁止元文本引用；4)禁止第三人称插入叙述(除非对白转述)；5)允许代码/协议块、系统警告标记、可量化体征数据作为修辞工具",
  "logic": "先确立世界观设定与人物表，再按因果链推进正文各章；结局章在全文主体完成后收束，尾声最后"
}
```

### 2.2 三个关键机制

| 机制 | 说明 |
|---|---|
| `novel.mode` | 顶层标记 = L3 路由开关。其他模板无此字段 → 通用线零回归 |
| `kind=setting` | 设定节点：不输出正文，novel_planner 生成后存 session，写作时注入上下文 |
| `kind=chapters` | 展开节点：novel_planner 忽略模板骨架，让 LLM 自由生成 L01-L15 章数组（数量可变） |

### 2.3 schema 最小扩展（`config_manager.py _normalize_template`）

1. `allowed` 集合加 `"novel"`、`"kind"` 两个键
2. `type` 校验保持 `leaf/section`（kind 独立于 type，不冲突）

---

## 三、L2 增强层：规划机制（8 步）

| 步 | 动作 | 输入 → 输出 | 说明 |
|---|---|---|---|
| 1 | 生成场景配置 | 题材+篇幅+视角 → 设定 JSON（时代/地点/风土人情/核心冲突/初步人物） | 存 `novel.setting`，不输出正文 |
| 2 | 生成一级大纲 | 场景配置 → 章数组（L01-L15，title+overview，末章 is_ending） | 章数自由 |
| 3 | 因果链验证 | 章数组 → 通过/不通过（逐链节 L01→L02→…因果递进） | 不通过反馈重生成，最多 3 次 |
| 4 | 用户确认 | 设定+章结构 → 确认/修正 | 未确认不进下一步 |
| 5 | 组装 outline | 章数组 → 标准 outline（sections=章，带 novel 身份字段） | 复用现有评审/进度/状态机制 |
| 6 | 每章 plan-chapter | 章+overview → 子结构数组（S01-S05：title/summary/tone/emotions/writing_prompt≥50字） | 缺 writing_prompt 硬阻断 |
| 7 | 子结构因果链验证 | 子结构数组 → 通过/不通过 | 同步骤 3 机制 |
| 8 | 进入写作 | outline 完整 → 串行写作 | 串行阻断由现有引擎天然保证 |

> 关键：步骤 2 不走 `plan_outline` 的"content 树字段必须全部输出"映射，novel_planner 直接让 LLM 自由生成章数组再组装，解决"模板定死骨架 vs 小说章数自由"的冲突。

---

## 四、L2 增强层：大纲修改机制

### 4.1 现有评审能力原样复用

勾选/排序/字数/重点/改名/章节级↻/子结构级↻/整篇重规划/辅助知识「+」——全部保留，语义见各操作。

### 4.2 三处必须扩展

**扩展1：身份继承铁律加 4 字段**（`replan_section` 小说分支）

| 新增继承字段 | 含义 |
|---|---|
| `tone` | 情绪基调（重规划后不变） |
| `emotions` | 混合情绪（重规划后不变） |
| `writing_prompt` | 写作命题（重规划后新子结构必须带 ≥50 字，缺则阻断） |
| `is_ending` | 末章标记（重规划后不能丢，否则收束验证失效） |

**扩展2：修改后因果链重验**
排序/重规划/增删章后，重新跑跨章承诺链（上章尾↔下章头续接），不通过给 SOFT 提示。接入 `_handle_replan_section` 小说分支。

**扩展3：整篇重规划两级**

| 选项 | 行为 |
|---|---|
| A：仅重排章结构 | 保留场景配置和人物表，重新生成章数组+因果链验证 |
| B：设定+章全重来 | 重走步骤 1-4 |

---

## 五、L2 增强层：写作引擎增强（novel_writer）

| 增强点 | 实现 |
|---|---|
| 上下文注入换血 | 角色表+人格(MBTI/原型)+实体关系网+时间线+情绪基调+上章行为轨迹+写作命题框（替代"前文回顾+参考资料"） |
| 新角色自动登记 | 写作中发现未登记角色 → 自动 add-char（novel-weaver 原机制） |
| 别名系统 | 子结构末尾【别名】行 → 注册 characters[].aliases（原子写入拦截） |
| 串行阻断 | 现有引擎逐子结构循环天然保证，无需新增 |
| 字数标准 | 按 meta.篇幅 三档：短篇1000-1500 / 中篇1500-2000 / 长篇2000-4000，规划-写作-检查三阶段同源 |

---

## 六、L2 增强层：检查体系（六检三检）

### 6.1 触发点

```
generate_article（小说分支）
├── 章循环：每写完一个 section（一章）
│   └── 章检：规则4检【进程内同步，毫秒级】
│       连通性 → 承诺链 → 风格校验 → 逻辑检查
└── 全文写完 → 小说质检阶段
    ├── 语义检查 bge【子进程】
    ├── 推理审核 R1-1.5B【子进程】
    ├── 大纲忠实度 fidelity【LLM/关键词】
    └── 结尾收束验证【三型判定】
    产出：质检报告（HARD/SOFT 清单）→ reports/ + 结果页
```

### 6.2 降级链（继承 novel-weaver 哲学）

1. 无 bge/R1 模型 → 自动跳过 5/6 步，规则 4 检照跑
2. 子进程超时/崩溃 → 捕获标记跳过，不阻塞完结
3. 未装 transformers/torch → UI 显示安装命令，不阻断
4. 强制 CPU（`CUDA_VISIBLE_DEVICES=-1`）→ 不与 LM Studio 抢显存

---

## 七、L4 模型层：三轨架构与用户控制权

### 7.1 三轨分工

| 轨 | 模型 | 干什么 | 为什么必须独立 |
|---|---|---|---|
| 轨A | LM Studio 35B（已有） | 场景配置/大纲/写作 | 主流程，唯一必须在线 |
| 轨B | bge-small-zh（33MB） | 语义检查（向量相似度） | 确定性计算无幻觉 |
| 轨C | R1-Distill-1.5B（~1GB） | 推理审核（因果/人格/情绪弧/对话/论证） | 独立于写作模型，防自审偏差 |

### 7.2 配置面板新增「小说质检」区（仿现有 RAG 区）

```
★ 小说质检
├── 模型目录    [路径框]     默认 structured-writer/models/
├── 检测模型    [按钮]       → 状态灯：语义✔ / 推理✔ / 均无
├── 安装模型    [按钮]       一键装（阿里云PyPI + hf-mirror）
├── ☑ 章检（规则4检）         默认开
├── ☑ 语义检查（bge）        默认开，无模型时灰色
├── ☑ 推理审核（R1）         默认开，无模型时灰色
└── ☑ 全文三检               默认开
```

### 7.3 模型查找顺序（已装过不重下；模型存放于 data/models，与 outputs/sessions/templates 平级）

```
structured-writer/data/models/              ← 首选（bge + R1 已复制，保持 HF 目录结构）
  → ~/.workbuddy/skills/.standardization/novel-weaver/models/（novel-weaver 缓存复用）
  → ~/.cache/huggingface/hub/               ← HF 默认缓存
  → 全无：跳过 + 显示安装命令
```

### 7.4 调用方式

| 检查 | 方式 | 理由 |
|---|---|---|
| 规则 4 检 | 进程内直接调 | 无模型依赖，毫秒级 |
| bge 语义 | 子进程（复用原 CLI 脚本） | 隔离崩溃、内存可回收 |
| R1 推理 | 子进程（复用原 CLI 脚本） | transformers 不进主进程，防拖垮 Web 服务 |

### 7.5 控制权清单（每个开关关掉会怎样）

| 关掉 | 后果 | 小说照写？ |
|---|---|---|
| 推理审核 | 少因果/人格/情绪弧审核 | ✅ |
| 语义检查 | 少语义对齐检测 | ✅ |
| 章检 | 少章完整性校验 | ✅ |
| 全文三检 | 无忠实度/收束报告 | ✅ |
| 全部检查+不装模型 | 退化成带场景配置和角色注入的普通写作 | ✅ |

---

## 八、前端改动（web_ui.py）

| 改动点 | 内容 |
|---|---|
| 配置面板 | 新增「小说质检」区块（模型目录/检测/安装/四个开关） |
| 模板下拉 | 内置「小说」模板（自动出现在列表） |
| 设定确认屏 | 场景配置 + 章结构确认界面（小说线规划后出现，现有规划结果页扩展） |
| 评审界面 | 章/子结构行新增：情绪基调(tone)、写作命题(writing_prompt)可编辑；人物表/设定可折叠区 |
| 进度状态 | 章检状态文本（"章检通过 L03"）、质检阶段进度 |
| 结果页 | 质检报告展示（HARD/SOFT 清单 + 修复入口） |

---

## 九、数据模型（session JSON novel 命名空间）

```
session.json
└── novel:
    ├── setting           场景配置（时代/地点/风土人情/冲突）
    ├── characters[]      角色表（name/role/mbti/archetype/traits/aliases）
    ├── timeline[]        时间线
    ├── entity_tracker    {entities[], relations[]} 五类实体关系网
    ├── behavior_summary  跨章行为轨迹
    ├── gates             门禁状态（outline_causality/sub_causality/...）
    └── checks            章检/质检报告 {chapter: {passed, issues[]}}
```

会话管理/归档/恢复/自动限额全部白嫖现有机制。

---

## 十、文件清单

### 新增文件

```
structured_writer/novel/__init__.py
structured_writer/novel/novel_planner.py     场景配置+章数组+因果链+组装 outline
structured_writer/novel/novel_context.py     上下文注入增强
structured_writer/novel/novel_writer.py      小说版 generate_article（复用串行骨架）
structured_writer/novel/novel_checks.py      章检(规则4检) + 质检编排(子进程)
structured_writer/novel/novel_state.py       novel 命名空间读写 + add-char + 门禁
structured_writer/novel/novel_semantic_check.py   移植自 novel-weaver（改 _load_model 目录参数）
structured_writer/novel/novel_reasoning_check.py  移植自 novel-weaver（同上）
```

### 修改文件（只加分支，不重写）

| 文件 | 改动 |
|---|---|
| `config_manager.py` | DEFAULT_TEMPLATES 加「小说」；_normalize_template allowed 集合加 novel/kind |
| `planner.py` | `plan_outline` 入口加 novel 分支 → novel_planner |
| `writer.py` | `generate_article` 加 novel 分支 → novel_writer；章循环尾/全文尾接 novel_checks |
| `web_ui.py` | 配置面板小说区、设定确认屏、评审界面字段、结果页质检报告 |
| `state_manager.py` | novel 命名空间透传（最小改动） |

---

## 十一、实施顺序（4 阶段，每阶段可验收）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P1：L1+L3** | 小说模板 + 路由 + schema 扩展 | 选「小说」模板能跑通现有流程（无增强），其他 8 模板零回归 |
| **P2：L2 上半** | 场景配置 + 章数组 + 因果链 + 增强上下文注入（角色/情绪/实体/时间线） | 小说线核心价值可用 |
| **P3：L4** | bge/R1 移植 + 配置面板小说区 + 降级链 | 有模型跑质检，无模型自动跳过 |
| **P4：L2 下半** | 六检三检编排 + 质检报告 + 修复入口 | 完整小说线闭环 |

---

## 十二、测试方案

| 类型 | 用例 |
|---|---|
| 回归 | 现有 8 内置模板 + 自定义模板全路径跑通（P1 门禁：零回归才进 P2） |
| 功能 | 小说线：场景配置→章生成→因果链→写作→章检→质检→收束验证全链路 |
| 降级 | 无 bge/R1 / 断网 / 子进程超时 / 未装 transformers → 全部自动跳过不报错 |
| 边界 | 篇幅三档字数标准 / 末章 is_ending 标记 / 重规划后身份继承 4 字段 / 别名注册 |
| 质量门禁 | 双零（0 ERROR 0 WARN）后 bump |

---

## 十三、风险与对策

| 风险 | 对策 |
|---|---|
| 轨B/轨C 装不上 | 阿里云 PyPI + hf-mirror 现成命令；装不上自动跳过 |
| web_ui 前端改动大（4660 行） | 只加区块/字段，复用现有组件，不重构 |
| 与通用线回归 | L3 路由"小说模板才走新代码"，其他路径零改动，天然隔离 |
| 35B 写作模型质量上限 | 属现状，六检只做偏离检测不做质量提升 |
| 场景配置确认屏打断流程 | 确认屏复用现有规划结果页交互，不新造范式 |

---

## 十四、版本规划

| 阶段 | 版本 | 说明 |
|---|---|---|
| P1 完成 | 1.10.0 | minor：新增小说模板+路由，无破坏 |
| P2 完成 | 1.11.0 | 场景配置+增强写作 |
| P3 完成 | 1.12.0 | 模型层+质检 |
| P4 完成 | 2.0.0 | 完整小说线闭环（major：架构新增模式） |

> 每次改动遵循既有铁律：修复→文档→bump（三端一致）→git-sync 双平台推送。

---

## 十五、验收标准（最终）

1. 通用线 8 模板行为与 v1.9.0b0 完全一致（零回归）
2. 小说线全流程：选模板 → 场景配置确认 → 章结构确认 → 评审 → 写作 → 章检 → 质检报告
3. 模型控制权：配置面板可检测/安装/开关 bge 与 R1，无模型时全部自动降级
4. 输出完整：正文 + 可选质检报告，目录化输出
5. 双零审计通过
