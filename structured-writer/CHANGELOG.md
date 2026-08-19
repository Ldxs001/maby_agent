# Structured Writer 更新日志

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号遵循语义版本控制（`structured_writer/__init__.py` 唯一源）。

## [3.1.6] - 2026-08-19
### 修复（子结构规划失败静默跳章——前置章缺失无感知）
- **根因**：novel_writer 主循环规划子结构异常时 `continue` 直接跳过该章进入下一章——用户调整模型期间规划调用失败 → 章回 pending 但无任何提示，后续章照常规划/写作，出现"L03 没规划、L04 照写"的跳章缺章（全文三检守卫靠 status 拦，但用户侧缺明确反馈）
- **修复**：规划失败改为 `break` 停止整个生成流程 + 置 `planning_failed: True` 标记（前端可据此提示）+ 明确状态文本"XXX 子结构规划失败，已停止后续章节（原因: …）。请修复后重试「开始生成」"——不再静默跳过后续章；重试时该章（pending）会被优先重新规划
- **验证**：模拟 L03 规划失败 → 修复前 `L03 失败→continue→L04 续写→L05 规划`；修复后 `L03 失败→BREAK 停止`（planning_failed=True），下次生成 L03 优先重新规划 ✅

## [3.1.5] - 2026-08-19
### 修复（aux_knowledge 持久化回显 + sub_order 真实回显——3.1.4 同类问题收尾）
- **① 辅助知识（aux_knowledge）持久化**：生成请求 handler 补写回 `sub.aux_knowledge`（此前 aux 只进请求体不落盘 → 前端弹窗回显读 `ss.aux_knowledge`（web_ui:5717）永远空；同 rag 型"状态不持久"）——写回后弹窗重开自然回显
- **② 子结构顺序（sub_order）持久化 + 真实回显**：
  - 持久化：`sub_orders` 写回 `sub.order`（此前只用于排序不落盘 → 显式选择无处回显）
  - 回显：前端 subOpts selected 按 `ss.order`（显式选择优先）→ 无 order 时数组第 1 位显示"自动"——不再按 id 后缀猜（调整顺序后 UI 与实际错位）
  - 验证：显式 s1 → s1 选中 ✅；无 order 数组第 1 → 自动 ✅；无 order 非第 1 → 无选中（浏览器默认自动）✅；显式 s2 → s2 选中 ✅
- bump 3.1.5（不 git-sync，用户指示）

## [3.1.4] - 2026-08-19
### 修复（小说线模型配置统一 config.json + RAG 勾选 UI 回显）
- **① 小说线串行调度读 session.config 孤儿快照（用户"换 26B 后仍先加载 27B"）**：
  - 根因：novel_writer:396（novel_checks）/411（串行 key）读 `state_mgr._state.config`——session.config 是 `init_session(config_mgr.get_all())` 的一次性快照，**永不更新、永不写回、只被 novel_writer 读**（孤儿快照）——换模型后旧 session 恢复不刷新 → 串行调度按旧模型 key 加载（27B 幽灵），然后 llm_client（config_mgr 新模型）再加载 → "先旧后新"
  - 修复：novel_writer 两处改读 `ConfigManager().get_all()`（config.json 最新，与 llm_client 同源）——换模型后串行调度直接用新模型，旧模型不再出现
  - 排查确认：config_mgr 内存为唯一运行时权威（/api/config 实测 26B）；后端保存链路正常（POST 26B → 落盘 26B）；36152 为 PyManager 垫片（父子进程非双实例）
- **② RAG 勾选 UI 显示（用户"大纲 RAG 勾选消失但写作生效——状态没持久、UI没统一"）**：
  - 根因：rag 勾选只进请求体（collectOutlineData → rag_options），**不写回 outline**（对比 titles/key_sections/checked 都有写回）→ session 的 rag 字段永远构造默认 `{enabled: False}`；前端 5532 渲染 checkbox **无 checked 回显** → 重渲染后勾选消失
  - 修复：①生成请求 handler 补 rag 写回 outline（与 titles 同款）；②前端 checkbox 加 `${s.rag?.enabled ? 'checked' : ''}` 回显 + KB 下拉按 `s.rag.kb` 初始化显示/选中
  - 验证：勾选过 → checked + KB 显示 + kb 选中 ✅；未勾选 → 不 checked + KB 隐藏 ✅；旧 outline 无 rag 字段 → 安全 ✅
- bump 3.1.4（不 git-sync，用户指示）

## [3.1.3] - 2026-08-19
### 新增（外部 API 补齐小说线——双线并存"一家完整"）
- **背景**：external_api（8777，仿 rag-assistant 8767 模式）只覆盖通用线（/api/write）——小说线（novel）无对外接口。双线并存架构，API 需一家完整
- **新增 10 个 /api/novel/* 端点**（全部复用 novel_bridge / novel_writer / novel_repair_engine 已有封装，**novel 侧零改动**——最小侵入）：
  - 规划域：`POST /api/novel/init`（init_novel_project）、`POST /api/novel/plan`（plan_novel_outline → outline+state_path+setting）、`POST /api/novel/plan_chapter`（plan_chapter_subs）、`POST /api/novel/replan`（replan_novel_chapter/sub）
  - 写作域：`POST /api/novel/write`（generate_novel_article 整本写作，同步阻塞，outline 或 topic 双模式）、`POST /api/novel/write_sub`（write_novel_sub）
  - 质检域：`POST /api/novel/check`（finalize_novel_chapter 六检）、`POST /api/novel/check_full`（finalize_novel_full 三检）、`POST /api/novel/repair`（novel_repair_engine.run + checked_subs/repair_types）
  - 状态/导出域：`GET /api/novel/projects`、`GET /api/novel/status`（novel_project_state）、`GET /api/novel/extract`（实体/关系/pledges/时间线）、`GET /api/novel/export`（现场拼整本 md → md/latex/pdf，复用 _convert）
- **错误语义**：项目不存在 → 404；参数缺失/非法 → 400；未知路径 → 404；处理异常 → 500（统一异常包装）
- **验证**：8777 冒烟——projects/status/extract/export-md 全通（躯体协议 64 实体/56 关系/10 pledges/51377 字整本导出）；404/400/未知路径语义正确；通用线 /api/write 等零改动
- bump 3.1.3（不 git-sync，用户指示）

## [3.1.2] - 2026-08-19
### 修复（整本拼合缺章——用户"五章全写完过了三检，但输出只有 1/2/3/5 缺第四章"）
- **现象**：《躯体协议》5 章全写完（txt 全齐）、全文三检通过，但输出 `chapters/` 只有 L01/L02/L03/L05.md，**缺 L04.md**——整本拼合/预览缺第四章
- **根因**：章级 md 由 novel_writer 写作循环内生成（751 行）——L04 章检 HARD 后用户"全部跳过"（skip 接口只标章 status=done + hint，**不生成章级 md**）→ L04 done 但 md 从未写 → 输出缺章（skip 标 done 与 md 生成解耦）
- **修复**：
  1. `_handle_repair_skip` 标 done 后，**自动补齐章级 md**（`_ensure_chapter_md`：从项目 chapters/<章>/*.txt 拼，无【别名】/末行编号残留，章标题 + 各子结构）
  2. 三处整本拼合/预览/texpdf 接口兜底：**md 缺失但项目 txt 存在的章 → 现场拼**（覆盖 skip/中断/其他未生成 md 场景）
- **验证**：实机补 L04.md（10825 字符，`## 强制收容` + 5 子结构标题全对，无别名/编号残留）；输出目录 5 章齐 ✅
- bump 3.1.2（不 git-sync，用户指示）

## [3.1.1] - 2026-08-19
### 修复（"全部跳过总是不标记通过"——用户"两章点全部跳过多少次了还是不通过，守卫一直拦 n2/n4"）
- **现象**：L02/L04 反复点"全部跳过"，全文三检守卫仍拦 `['n2','n4']`（章 status 一直不是 done）
- **根因**：skip 接口只标记 hint（_repaired=True + 清 issues/full_items），**章的 status=done 由生成线程（novel_writer）在修复循环 break 后标**——生成线程已退出（修复轮次用完回 pending / 线程结束）后，再点多少次"全部跳过"都**无人标 done** → 章永久 pending → 守卫一直拦
- **修复**：`_handle_repair_skip` 补"跳过=通过"完整语义——除标记 hint 外，**直接把对应章（_novel.chapter == chapter）的 section status 置 done**（生成线程在跑时幂等，已退出时兜底生效）
- **验证**：模拟 L02 pending + HARD hint → skip 新逻辑 → L02 status=done → 守卫放行 ✅
- bump 3.1.1（不 git-sync，用户指示）

## [3.1.0] - 2026-08-19
### 发布（beta 线收敛转正式版——3.1.0b1~b22 全量沉淀）
- **发布说明**：3.1.0 正式版，beta 线（b1~b22）结束。本节为 beta 线核心变更摘要，逐条细节见对应 b 版本条目。
- **架构主线**：
  - llama.cpp 直挂后端整体废弃 → LM Studio 统一管理（llama.cpp 写作 8 t/s vs LM Studio 20+ t/s，两代推理内核代差）；模型配置按后端分槽、切后端自动恢复
  - 判定模型共享（`_SHARED_LLAMA`）弱引用化——35B 写作/规划常驻共享，判定模型（8B/7B）测完即卸；修复引擎独立实例 + apply 防重入竞态
  - 串行调度序列定稿：卸载 → 加载 → 执行 → 卸载；统一管理装载/卸载调度点 + ending 收尾判定 8B
- **判定体系**：
  - 推理审核注入 `writing_style.custom_rules`（[文风规则] 块）——题材感知判定，消除"设定内合法表达被通用标准误判"导致的修复空转（b21）
  - 推理审核采样参数回归判定温区 0.2（0.6 创造模式 → 0.2 判定模式）——判定任务稳定化，HARD 稳定归零（b22）
- **流程语义**：
  - 跳过 = 通过统一语义；章检"全部跳过"弹窗修复；修复状态会话隔离；修复静默失败 + 无过程反馈补齐
  - 三检修复项静默丢弃、全文三检守卫顺序、pledge flag 提取统一 8B 后端、3B 遗留加载等修复
- **验证**：L04 端到端推理审核 0 HARD 稳定（两次 0+0 / 0+1 SOFT）；8B/7B 就位后全链路实机验证通过
- bump 3.1.0（不 git-sync）

## [3.1.0b22] - 2026-08-19
### 修复（推理审核采样参数回归判定温区——0.6 创造模式→0.2 判定模式）
- **现象**：推理审核两次独立运行结果波动（1 HARD+1 SOFT / 0+0）——判定任务输出不稳定
- **根因**：`novel_reasoning_check.py` 推理审核用 `temperature=0.6, top_p=0.95`——创造模式采样用于判定任务，波动被放大；全套判定任务（4维 0.2 / fidelity 0.2 / pledge 0.2 / 实体提取 0.2）唯独推理审核是 0.6
- **修复**：推理审核采样参数 0.6→**0.2**、top_p 0.95→0.9（与全系统判定任务惯例一致；非 0.0 贪心，保留轻微多样性避免边界判定固化）
- **决策确认**：温度选 0.2（非 0.1）——与全系统判定任务一致且避免贪心焊死边界；pledge 继承 writer 配置路径（`_create_writer_client` 构造时 `wm.get("temperature", 0.7)`）实际被调用处 `temperature=0.2` 硬覆盖，生效值 0.2，无继承问题；HARD/SOFT 均弹窗由人工决定是否修复，无需改动 UI 行为
- **验证**：降温度后端到端跑 L04 推理审核两次：0+0 / 0+1（SOFT）——HARD 稳定归零（b21 已消除机器腔误判，b22 使结论稳定）；SOFT 波动（S05 人物行为一致性）属边界判定正常现象且不阻断
- bump 3.1.0b22（不 git-sync）

## [3.1.0b21] - 2026-08-19
### 修复（推理审核未注入文风规则——设定内合法表达被误判为问题，修复空转）
- **现象**：科幻题材（叙述者 = 植入式 LLM）L04 反复报 S03/S04/S05 推理审核问题；修复后验证通过但重检仍报
- **根因**：`writing_style.custom_rules`（明确"允许代码/协议块、系统警告标记作为修辞工具"）**未注入推理审核 prompt**——审核模型（R1）按通用小说标准判定，把设定内合法的机器腔（概率链/CPU 负载/系统日志）判为"对话用词超出角色设定"；且修复 prompt（novel_repair_engine 372 行）"禁止任何 meta 表述"与 custom_rules 冲突，修复器无所适从 → 修复无效 → 重检仍报 → 循环
- **修复**：
  - A. `novel_reasoning_check._build_prompt` 注入 `writing_style.custom_rules` 为 [文风规则] 块，加判定约束（先读文风规则再判各维度；规则允许的表述不属于违规）——审核器知晓题材边界
  - B. `novel_repair_engine._build_guided_prompt` 372 行"禁止任何 meta 表述"改为"除文风规则允许的表述外禁止"——修复器与审核器读同源规则，消除冲突
- **明确不改**：4 维检查（novel_4dim_check）不注入——只做章内连贯性（时间/情绪/话题/角色），无文风维度，机器腔不影响衔接判定
- **验证**：注入后端到端跑 L04 推理审核：0 HARD + 0 SOFT（改前反复报 S03/S04/S05）；两次独立运行存在模型判定波动（1 HARD+1 SOFT / 0+0），但机器腔类误判已消除
- bump 3.1.0b21（不 git-sync）

## [3.1.0b20] - 2026-08-18
### 修复（章检"全部跳过"弹两次——点击跳过后仍再次弹出，需二次跳过才停止）
- **现象**：章检 HARD 弹窗 → 点"全部跳过" → 又弹一次 → 再点才不弹
- **根因**：内存/磁盘不同步——skip 接口用独立 StateManager 实例写**磁盘**（_repaired=True + issues 清空 + skipped），但 novel_writer 的 state_mgr 是**内存对象**，内存 _repair_hints 仍是章检旧态（HARD + _repaired=False）；skip 后 novel_writer 轮询读到磁盘 _repaired → break → 标章 done 时 `state_mgr.save()` **用内存旧态覆盖磁盘**（HARD 复活 + _repaired=False）→ 前端 repair_pending 又非空又弹 → 再 skip（此时章已 done 不再覆盖）才停
- **修复**：novel_writer 修复循环轮询到 `_repaired=True` 后，**先把磁盘 hint 同步回 state_mgr 内存**（`_hints_mem[chapter_id] = _hint`），后续 save() 保留 skip/apply 状态
- **验证**：模拟——修复前 save 覆盖回 `_repaired=False + issues=[HARD]` → 又弹；修复后磁盘保留 `_repaired=True + issues=[]` → 不再弹 ✅
- bump 3.1.0b20（不 git-sync）

## [3.1.0b19] - 2026-08-18
### 修复（推理审核"无法评估"误报——无对话段落被标 HARD"无法评估"）
- **现象**：UI 章级问题显示 `HARD: 推理审核 - 对话匹配度: 暂无对话内容，无法评估`——R1 明确表示"无法评估"（该段无对话）却标 HARD 强制人工处理，与"符合设定"类误报同源
- **根因**：R1 输出 result="HARD"（标准值）+ detail="暂无对话内容，无法评估"——解析直接按 HARD 收集；"无法评估"语义（≠问题）无兜底
- **修复**：解析循环加**无法评估语义跳过**（无论 result 是 HARD/SOFT，detail/result 含 无法评估/无法判断/无法确认/无法核验/无法验证/暂无法/暂无对话/无对话内容/没有对话/对话内容不足/暂无内容/无内容可/不适用 → continue 不收集）
- **验证**：案例"HARD 暂无对话内容，无法评估"→ 跳过 ✅；"该段无对话，无法评估匹配度"→ 跳过 ✅；真问题"事件无前文铺垫，转折突兀"→ 保留 HARD ✅；PASS → 跳过 ✅
- bump 3.1.0b19（不 git-sync）

## [3.1.0b18] - 2026-08-18
### 修复（3B 遗留加载不做功——3B 在 8B 前被加载且不参与生成）
- **现象**：unified 勾选下提取一直走 8B（每段后 `已卸载判定模型: qwen3-8b`），但服务启动后第一次提取时 3B（transformers）也被加载（`Loading weights 434/434` + `抽取模型已加载 (Qwen2.5-3B, CPU)`），加载后从不使用、常驻内存
- **根因**：b9 改 `_extract_llm` 接入 `_llm_generate` 统一后端时，**只删了 `model, tokenizer = loaded`，残留了开头的 `loaded = _load_extract_model()` 前置检查**——每次提取先加载 3B（_EXTRACT_PIPE 单例常驻），然后 `_llm_generate` 走 8B 干活 → 3B 加载后不做功
- **修复**：删除 `_extract_llm` 开头的 `loaded = _load_extract_model(); if loaded is None: return None`（模型加载统一归 `_llm_generate`）；behavior/timeline 已确认无残留（仅 import，不加载）
- **验证**：实机复现（unified=True 真实提取）——修复前打印 3B 加载（Loading weights），修复后**无 3B 加载**，仅 8B 提取（总结新增关系）✅；临时文件已清理
- bump 3.1.0b18（不 git-sync）

## [3.1.0b17] - 2026-08-18
### 修复（UI 文案残留"统一管理 + 3B"——统一管理路径下文案仍提示 3B）
- **背景**：b9-b11 后统一管理（8B/7B LM Studio）下判定+提取全走 8B/7B（章内提取/pledge flag/4dim/reasoning/fidelity/ending），**3B 只在未勾选统一管理（transformers 3B/1.5B）时使用**——但 UI 文案仍残留"LM Studio 方案 + 3B"
- **修复**：①安装缺失模型说明「LM Studio 方案（统一勾选）：8B+7B GGUF 约9.4GB + 3B」→「…判定+提取全走 8B/7B，无需 3B」；②模型状态 JS：统一管理分支删除「3B(抽取) 就绪/缺失」显示 → 改为「提取: 8B（统一管理下无需 3B）」；未勾选分支（transformers）的 R1/3B 状态保留（该模式仍需 3B）
- **验证**：残留 "+ 3B"（统一方案）False；JS 无 "3B(抽取)"；含 "提取: 8B" ✅
- bump 3.1.0b17（不 git-sync）

## [3.1.0b16] - 2026-08-18
### 修复（修复提示词缺写作分类上下文——修复 prompt 未携带写作时的分类约束）
- **背景**："内部时钟/帧率/磨砂玻璃"是小说正常叙事（第一人称 AI 视角），问题句是"概率链显示…符合 INTJ 原型理性规划特征"（分析腔）。写作时 NOVEL_SYSTEM_PROMPT 有分类约束（视角强制最高优先级/show don't tell/对话符合身份），novel_context_loader 输出 ★★★ 分级上下文（角色/人格/情绪/命题框）——但**修复 prompt 从未复用**
- **根因**：_build_guided_prompt / _build_rewrite_prompt 只有 [子结构规划]（title/summary/emotions/word_count）+ R1 detail 问题描述——**无视角强制、无角色人格、无 show don't tell、无命题框** → 35B 重构无文风锚点自由发挥 → 分析腔
- **修复**：rewrite_segment 加载该段写作上下文（novel_context_loader._load_context_captured，与写作同源）注入两个修复 prompt 的 [写作上下文] 段（★★★ 分级约束/角色/情绪/视角强制/命题框）；rewrite_segment 加 state_path 参数（reng.run 传入）；guided 与整段重构均遵守原文分类
- **验证**：真实项目 L05/S02 上下文加载 6385 字符（含 ★★★ 分级说明），guided prompt 含 [写作上下文] ✅
- bump 3.1.0b16（不 git-sync）

## [3.1.0b15] - 2026-08-18
### 修复（修复提示词引导出"分析腔"正文——修复结果出现"符合 INTJ 原型理性规划特征"式分析表述）
- **现象**：勾选修复后 35B 重构输出"概率链显示，张维医生的行为模式符合 INTJ 原型中的理性规划特征…内部时钟与外部世界物理时间存在微小不同步…帧率丢失"——分析/评论/旁白腔，不是小说叙事
- **根因**：修复 prompt（guided 引导式局部改写 + 整段重构）把 R1 审核的 detail（问题描述）直接喂给 35B——detail 若含 PASS 语义（如"符合设定"，b14 前的误报）或分析性语言，35B 无从定位问题句 → 为了改而改 → 凭空插入分析词、把 meta 语言写进正文
- **修复**：`_build_guided_prompt` + `_build_rewrite_prompt` 双加硬约束——①文风硬约束：禁止任何分析/评论/旁白式 meta 表述（"概率链显示""符合某原型特征""内部时钟与物理时间不同步"等），一律动作/对话/场景呈现，叙述者视角不得跳成"分析报告"口吻；②PASS 语义保护：问题描述本身是"符合设定/一致/正常"等 PASS 语义或无法定位具体问题句 → 原样保留正文，不要为了改而改
- **非串行逻辑确认未动**：sync_after_rewrite 从 reng.run 挪到 web_ui with 外，非串行时 with 不卸载 35B（驻留），sync 时机与原行为一致；report["t1"]["results"] 结构保留（web_ui 唯一读点）
- bump 3.1.0b15（不 git-sync）

## [3.1.0b14] - 2026-08-18
### 修复（串行明确步骤：重构后同步 8B 提取挪出 35B 窗口 + 推理审核 PASS 语义误报 SOFT）
- **① 重构后同步（sync_after_rewrite）在 35B 窗口内跑 8B 提取（修复后 35B 未卸载即加载 8B——应先卸载再加载，串行步骤明确）**
  - 原：reng.run 内重构段后立即 `sync_after_rewrite`（8B 实体提取）——但此时修复线程 35B 仍驻留（lms_session 窗口内）→ 8B 被 CPU offload
  - 修：`sync_after_rewrite` 从 reng.run 内移除（report["t1"] 去 syncs），挪到 web_ui 修复线程 `lms_session(35B)` **退出后**执行——串行明确步骤：35B 卸载 → 8B 提取真 GPU
- **② 推理审核"符合设定"误报 SOFT（S02 符合设定却作为 SOFT 显示并强制修复）**
  - 原：模型 result 字段用自然语言（如"符合设定"）时，解析走 else 兜底 → 一律 SOFT → 通过项被收集并强制修复
  - 修：else 分支先判 PASS 语义词（符合/一致/正常/无问题/通过/合理/恰当/到位/没有矛盾/无异常/无误）→ PASS 不收集；问题词（不符合/不一致/矛盾/突兀/异常/缺失/不足/欠妥/生硬/断裂）→ SOFT 保守收集；均无 → SOFT
  - 验证：`result='符合设定'` → PASS 不收集 ✅；标准 PASS → 不收集 ✅；真问题"情绪转变略显突兀" → SOFT ✅
- bump 3.1.0b14（不 git-sync）

## [3.1.0b13] - 2026-08-18
### 修复（三检修复项被静默丢弃——勾选修复后 35B 装载未生成即卸载，宣称完成）
- **现象**：勾选三检修复项（ending 等）→ 点修复 → 35B 装载 → **零生成** → 卸载 → "修复完成"。正文根本没改
- **根因**：`_handle_repair_apply` 的 `structured` 只从**章检 issues**（hint.issues）构建；三检修复项（`hint.full_items`，fidelity/pledge/ending）**从未并入** → reng.run 的 seg_map 从 structured 构建 → 三检项永远进不了 seg_map → 无 T1 重构 → 35B 白装 → 重检 `_recheck_full_items` 重跑 verify_ending 碰运气通过（正文未改）→ full_items 清空标记完成
- **修复**：`_handle_repair_apply` 把 `full_items` 并入 structured（`file=sub.txt` + problem，severity=HARD）——三检项进入 seg_map → 35B 真重构对应子结构（repair_type=ending/fidelity/pledge 类型化修复目标）；重构后重检逻辑不变（通过才移除）
- **验证**：模拟三检阶段（章检 issues 空 + full_items 含 ending/S05）→ 合并后 structured 非空 → seg_map 命中 `{"S05.txt": ["结尾收束验证未通过"]}` → 35B 将真重构 ✅（修复前 seg_map 为空 = 零生成）
- bump 3.1.0b13（不 git-sync）

## [3.1.0b12] - 2026-08-18
### 修复（全文三检守卫顺序错误——章内六检并修复通过之后才做全文检查，顺序不对）
- **现象**：L05 章内 6 检在全文检查**之后**才执行——流程反了（应为：章内六检并修复通过 → 无规划任务 → 全文检查）
- **根因**：b7 修复守卫时只按"文件真相"（_chapter_files_complete）放行——L05 正文文件已落盘（S01-S05 非空）但**章检（6检）未通过/未跑**，守卫照样放行全文检查
- **修复**：守卫改为双条件——`session outline 后端状态全部 done（章检通过才标 done，state_mgr 维护的真相，非前端请求体副本）∧ 所有章文件真实落盘`。既防前端过期副本误判（b7 动机保留），又防"文件齐但章检未过"提前触发（本次根因）
- **验证**（真实项目）：全 done+文件全齐 → 触发 ✅；L05 文件齐但章检未过 → 不触发 ✅；全 planning（前端过期）→ 不触发 ✅
- bump 3.1.0b12（不 git-sync）

## [3.1.0b11] - 2026-08-18
### 修复（pledge flag 提取遗漏接入统一 8B 后端——flag 提取未随统一管理切到 8B）
- **现象**：b9 只改了三个小说提取器（entity/behavior/timeline），pledge 的 `extract_pledges` 仍走独立的 `_load_3b`/`_gen_3b`（transformers 3B ChatML 直挂）——统一管理下全文三检的承诺 flag 提取仍是 3B，漏网
- **修复**：extract_pledges 生成处改调 `_llm_generate`（统一后端：unified 勾选 → 8B LM Studio 句柄缓存；不勾 → 3B transformers）；前置检查改为"未勾统一管理时 3B 必须可用"（勾选时 8B 主路径，失败内部回退 3B）；删除死代码 `_gen_3b`（唯一调用点已替换）
- **验证**：临时开 unified → 8B 正确提取 flag（"潜入实验室查明真相"林渊 / "找到芯片"苏婉，含 sub 定位）→ 数组解析成功 → 8B 释放（lms unload）✅；config 已还原
- **至此统一管理下全部提取/判定点已无 3B 遗漏**：章内提取（entity/behavior/timeline）+ pledge flag 提取 + 4dim 判定 + reasoning 审核 + fidelity + ending 全走 LM Studio（串行时严格卸载加载）
- bump 3.1.0b11（不 git-sync）

## [3.1.0b10] - 2026-08-18
### 修复（独占串行依赖统一管理——未开启统一管理时走 3B/1.5B，独占串行不生效）
- **现象**：exclusive_serial 此前是独立开关——统一管理不勾（判定 transformers 3B/1.5B，无 8B/7B GPU 模型）时串行开关仍驱动 35B 卸载加载，属无效调度（35B 卸了纯浪费，下章又 load 15s）
- **修复**：串行生效条件 = `exclusive_serial ∧ unified_management`（四象限：unified=True+serial=True → 串行；unified=True+serial=False → 并行常驻；unified=False → 串行无效，35B 由 web_ui 包管常驻）
- **接入点**：web_ui 规划/写作/修复三处 `_serial_*` 改 `serial ∧ unified`（不勾时写作改回 lms_session 包整本常驻）；novel_writer 章级调度 `_serial = serial ∧ unified`（提取切换 `_switch` 同值）；pledge `_serial_pl` 同改
- **UI**：独占串行 checkbox 跟随统一管理——统一管理不勾 → 串行禁用（disabled）+ 强制取消勾选；save/初始化双处联动；后端判定 release 无需改（unified 不勾走 transformers 分支，release 为 del+gc 无害）
- 验证：四象限生效矩阵全过（unified×serial → 生效布尔）
- bump 3.1.0b10（不 git-sync）

## [3.1.0b9] - 2026-08-18
### 新增（提取 3B → 8B，流程不变——仅换后端，仍每子结构写完后立即做）
- **背景**：b8 曾误将提取设想为"挪到章级窗口"；纠正——提取仍是每子结构写完后立即做，只是后端从 transformers 3B 换成 LM Studio 8B；串行时在 S01 写完卸载 35B → 加载 8B 提取 → 卸载 8B → 加载 35B 继续写 S02
- **提取器统一生成后端（novel_entity_extractor._llm_generate）**：统一管理勾选（unified_management）→ LM Studio 8B（make_lms_handle 4dim key + lms_generate，GPU/部分 GPU；8B 句柄模块级缓存）；未勾选 → transformers Qwen2.5-3B（CPU，现状，ChatML 逻辑保留）。三个提取器（entity/behavior/timeline）生成处全部改调 `_llm_generate`，prompt 构造/纠错重试/正则兜底逻辑不变
- **串行切换（novel_writer._write_sub_inline）**：serial_switch（=独占串行 ∧ 统一管理勾选）时——提取前卸载写作模型（35B）→ 三个提取器（8B）→ 完成后 `_release_extract_lms()` 释放 8B → 下一段写段前 ensure_loaded 35B 重载（写段循环已有）。**流程完全不变：仍一子结构写完后一次提取**
- **并行模式**：提取直接调 8B（模型常驻，LM Studio 自行调度 GPU/部分 GPU），与 3B 时代调用方式一致，只是后端不同——无需卸载加载
- **UI**：取消独占串行时显示提醒"⚠️ 关闭独占串行：建议将 max concurrency 设置为 ≥2（多模型常驻可并行推理）"（仅此时提醒，串行模式一次一模型无并发；toast 不可用时用内联 span）
- 验证：临时开 unified_management + exclusive_serial → 8B 正确提取实体（林渊/苏婉 character + 推开门/启动防火墙关系）→ JSON 解析成功 → _release_extract_lms 正常卸载；config.json 已还原
- bump 3.1.0b9（不 git-sync）

## [3.1.0b8] - 2026-08-18
### 新增（独占串行两套卸载加载机制——35B 常驻致 8B/7B 被 CPU offload 的根因修复）
- **背景**：实测 35B 权重 22GB 只放 7.6GB 进 GPU（部分 offload，空闲即占满）；35B 驻留时 8B/7B 加载显存仅 +326MiB/+279MiB → 被 LM Studio 降级全量 CPU（8B ~6 t/s、7B ~9 t/s，GPU 利用率 0-17%）——"8B 很好/7B 一闪而过"的真因
- **exclusive_serial 开关（novel_checks，默认 True）**：两套机制
  - 开（独占串行，默认）：一次只驻留一个模型——写章 35B → 章检前卸载 → 8B 判定（真 GPU）→ 卸载 → 7B → 卸载 → 下一章再加载。规划/写作同模型也严格两步（规划线程退出即卸，写作首段幂等重载）
  - 关（并行）：只加载不卸载（模型常驻），适合显存充足硬件；UI 提醒"关闭本功能请先确认硬件足够"
- **lms_ensure_loaded 幂等加载**（model_backend）：先 lms_ps 探测再 load——LM Studio 重复 load 同模型会生成 `:2` 副本双倍占显存（实测 qwen3-8b:2），串行调度必须防
- **lms_session 加 unload_on_exit**：True=退出即卸（串行）；False=退出驻留（并行）；make_lms_handle 也走幂等加载
- **调度接入点**：web_ui 规划/写作/修复三处 lms_session 按开关传 unload_on_exit（串行开时写作不包整本——novel_writer 内部章级调度）；novel_writer 循环写段前 ensure_loaded 35B、章检前 unload 35B、循环尾卸载兜底（异常由 ttl=120 兜底）；判定模块（4dim/reasoning/fidelity/ending）release 按开关（关时驻留不卸）；pledge 包裹按开关
- **UI**：统一管理旁新增「独占串行（一次一模型，用完即卸）」checkbox，title 含硬件提醒；旧配置无该字段 → 默认勾选
- 验证：幂等加载（二次调用 already-loaded、无 :2 副本）、串行态退出即卸、并行态退出驻留、默认 True 全通过
- bump 3.1.0b8（不 git-sync）

## [3.1.0b7] - 2026-08-18
### 修复（全文三检守卫误判"未写完"——outline 过期副本与磁盘真相脱钩）
- **现象**：正文 5 章全部写完（L01-L05 段文件全齐、session n1-n5 全 done），真实写作流程却打 `[全文三检] 全书未写完（仍有章未 done: ['n5']）` 跳过三检
- **根因**：novel_writer.py 全文三检守卫读"调用方传入 outline 参数"的 section status；web_ui 生成请求的 outline 直接来自前端请求体（web_ui.py 还会把请求体 outline 覆盖回写 session）——前端缓存过期副本时 status 与磁盘真相脱钩 → 误判
- **修复**：守卫改为 `_chapter_files_complete(state_path, s, ndata)` 文件为真相源（与循环内续写恢复逻辑同构），outline status 不再参与判定；日志文案改"仍有章文件不全"
- **验证**：实测把 session outline 5 章 status 全部故意置为 planning（模拟前端过期）→ 旧守卫跳过，新守卫判定文件全齐 → 正确触发三检
- bump 3.1.0b7（不 git-sync）

## [3.1.0b6] - 2026-08-18
### 新增（统一管理装载/卸载调度点 + ending 收尾判定 8B——调度序列定稿）
- **调度基础设施（model_backend）**：新增 `lms_session(model_key, ctx, ttl=120)` 上下文管理器（进入 lms load --gpu max --ttl，退出 lms unload）+ `model_key_from_cfg(model_cfg)` 配置取 key；`make_lms_handle` 默认 ttl=120 兜底自动卸
- **web_ui 三处 LLM 调用点包裹 lms_session**：规划（planner_model）、写作（writer_model）、修复（writer_model）——每次请求装载对应模型、用完即卸，35B 不再 TTL 1h 常驻
- **判定模型卸载**：check_4dim（novel_4dim_check）/ check_reasoning（novel_reasoning_check）主体 try/finally `release()`（lms unload 识别句柄卸载）；fidelity_check 返回前 release。8B/7B 判定用完即卸，杜绝 35B+8B+7B 叠抢 8GB 显存
- **pledge 全文承诺判定**：writer client 调用包裹 lms_session（writer_model）
- **ending 收尾判定 8B（novel_fidelity）**：`_classify_ending_llm` 双后端——judge_backend==lmstudio → `make_lms_handle(8b)` LM Studio GPU；否则回退 Qwen2.5-3B（CPU）；source 文案同步更新
- **实机生效**：真实写作流程日志 `[lms-sched] 卸载: qwen/qwen3.5-35b-a3b` 确认调度点在生产链路运行（写完即卸）
- bump 3.1.0b6（不 git-sync）

## [3.1.0b5] - 2026-08-18
### 验证确认（8B/7B 就位后全链路实机验证——确认 7B 跑推理审核、8B 关思考）
- **8B/7B 就位**：现有 GGUF（data/models/gguf 旧 llama.cpp 遗留）移入 LM Studio 模型库 → 自动识别裸名 key（qwen3-8b / deepseek-r1-distill-qwen-7b），无需 lms import
- **7B 推理审核实机验证**：novel_reasoning_check 走 LM Studio 后端，加载 deepseek-r1-distill-qwen-7b → 审核"太阳落下但钟显八点"段落 → 正确输出 `{"ok": false, "issues": ["矛盾的时间信息"]}` → JSON 解析成功 → lms unload。非空跑/非回退/非跳过
- **8B 关思考实机验证**：Qwen3-8B 带 `/no_think` → content 直接输出（`'2'`）；不带 → 思考链进 reasoning_content（LM Studio 分离字段）→ 判定 JSON 永不被思考链污染；`_judge` 注入逻辑不变
- **结论**：b4 后无新增代码改动，纯验证闭环；bump 3.1.0b5（不 git-sync）

## [3.1.0b4] - 2026-08-18
### 修复（lms ls 裸名 key 过滤 bug——本地移入的 8B/7B 识别不了）
- **现象**：8B/7B GGUF 移入 LM Studio 模型库后 lms ls 正常显示（qwen3-8b / deepseek-r1-distill-qwen-7b），但 judge_model_keys 解析为空
- **根因**：_lms_list_keys 过滤条件 `if "/" not in key: continue` 只收 user/repo 形态 key（HF 下载的 qwen/qwen3.5-35b-a3b）；**本地移入的 GGUF 是裸名 key**（无斜杠）→ 全被过滤
- **修复**：去掉 "/" 要求，改为跳过段头/表头/统计行 + 首列非数字的通用过滤
- **模型就位**：现有 8B/7B（旧 llama.cpp 遗留 data/models/gguf/）移动进模型库 `~/.lmstudio/models/Qwen/Qwen3-8B-GGUF/` 与 `unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF/`，LM Studio 自动识别（无需 import）
- 验证：judge_model_keys → {4dim: qwen3-8b, r1: deepseek-r1-distill-qwen-7b}，ensure_judge_models ok=True；**8B 实机链路**（lms load 5GB 全 GPU → HTTP 生成 → lms unload）通过；bump 3.1.0b4（不 git-sync）

## [3.1.0b3] - 2026-08-18
### 简化（判定窗口 UI 移除 + ollama 场景禁用统一管理——判定窗口无必要、ollama 侧无实现）
- **两点**：①判定窗口输入框是否还需要 ②写作后端是 ollama 时统一管理不该显示/应禁用
- **判定窗口判断**：输入框可以删，但窗口参数本身不能丢——LM Studio 默认窗口不保证够 R1（prompt+思考链+JSON ≈13K），lms load 必须显式 `-c`。实现：删前端「判定窗口」输入框与说明，窗口固定 16384（4维/R1 的 make_lms_handle 传 `cfg.judge_n_ctx or 16384`；后端 judge_n_ctx 解析保留兼容旧配置）
- **ollama 禁用统一管理**：_novel_status_data 新增 model_backends（planner/writer 的 backend）；前端 unifiedEl.disabled = !(LM Studio 存在 && planner/writer 都是 lmstudio)，禁用时 title 提示"写作/规划后端为 Ollama，统一管理不适用"
- 验证：py_compile 全绿；novel-chk-nctx 无残留；bump 3.1.0b3（不 git-sync）

## [3.1.0b2] - 2026-08-18
### 清理（setup.bat 去掉 py 3.11 优先——bat 不再装 llama/CUDA，无需限制 py 版本）
- **确认**：llama-cpp-python/CUDA/Python 版本限定全部移除后，setup.bat 的 `py -3.11 优先` 是唯一残留的历史偏好（llama-cpp-python wheel 区间遗留），不再有任何意义
- **实现**：setup.bat 删除 `py -3.11` 探测段，直接用系统默认 python（`set PYCMD=python`）；注释更新（仅需 transformers/torch，任意版本）
- 验证：bat 语法检查；3B/1.5B 与 LM Studio 链路不受影响（env_check 用 sys.executable 自检）；bump 3.1.0b2（不 git-sync）

## [3.1.0b1] - 2026-08-17
### 重构（llama.cpp 直挂后端整体废弃 → LM Studio 统一管理——llama.cpp 写作 8 t/s vs LM Studio 20+ t/s）
- **现象**：llama.cpp 后端（llama-cpp-python 0.3.34 本地直挂）35B 写作仅 8 t/s，LM Studio 同模型 20+ t/s，差距巨大
- **根因确认**：llama-cpp-python 官方 PyPI 最高 0.3.35（2025 年初内核，已停更）——旧内核对 Qwen3.5/3.6-35B-A3B（新 MoE）无专家并行/CUDA MoE kernel 优化，9 层 partial offload 后 55 层（含全部专家）在 CPU 串行 → 8 t/s 是物理极限；LM Studio 用最新 llama.cpp master → 20+ t/s。上下文窗口非主因（KV 预分配，窗口只影响内存/prefill 不影响 decode t/s）
- **架构决策（定稿）**：llama.cpp 直挂整体废弃；写作/规划 35B + 判定 8B/7B 统一走 LM Studio（lms load → GPU → HTTP localhost:1234）；统一管理勾选 → 8B/7B 走 LM Studio GPU，不勾 → transformers 3B+1.5B；实体/行为提取永远 transformers Qwen2.5-3B CPU
- **实现**：
  - 新增 `novel/lmstudio_probe.py`——LM Studio 环境探查（lms.exe 定位、settings.json downloadsFolder 模型根目录、server 可达性）+ lms load/unload/ps/import/server start 封装 + GGUF 下载目标映射（hf_hub_download local_dir=模型库根 → 自动落 `<publisher>/<repo>/<file>`，与 LM Studio 目录结构一致）
  - `model_backend.py`：judge_backend 从 `A∧B→llama.cpp` 改为 `B→lmstudio`（A=Python 3.10~3.11 条件消失）；删 load_llama/_estimate_gpu_layers/_gguf_*/弱引用共享；新增 judge_model_keys（lms ls 解析 8B/7B key）、lms_generate（HTTP 生成）、make_lms_handle（可调用句柄，带 _lms_model_key 标记）、ensure_judge_models（缺失检测）；release 识别 lms 句柄 → lms unload（测完即卸）
  - `llm_client.py`：删除全部 llama.cpp 分支（_llama_instance/_llama_generate/_estimate_gpu_layers/window_info 等），保留纯 HTTP 客户端
  - `web_ui.py`：后端下拉移除 llama.cpp 选项；统一管理勾选条件改为 LM Studio 存在；「安装缺失模型」GGUF 下载目标改为 LM Studio 模型库（缺失自动下载 + lms import 注册，不依赖本机已有）；状态注入改 lmstudio probe
  - `model_env_check.py`：check_llamacpp → check_lmstudio；`setup.bat`：删除 llama-cpp-python 安装段（不再限定 Python 3.10~3.11）
  - `config.json`：planner/writer 移除 llama.cpp profile，顶层对齐 lmstudio（qwen/qwen3.6-35b-a3b）
- 验证：全项目 py_compile 全绿；lmstudio_probe 探查全通（lms_exe/models_dir/server_ok）；judge_backend 路由（勾选→lmstudio/不勾→transformers）；缺失场景（库无 8B/7B → 明确提示下载 + 回退 transformers 3B 实测加载 OK）；完整链路实测（make_lms_handle 1B → lms load → HTTP 生成"我是AI助手。" → release lms unload）；/api/config 与 /api/novel/status 集成验证通过；bump 3.1.0b1（不 git-sync）

## [3.0.0b41] - 2026-08-17
### 优化（模型列表缓存——F5 不触发加载/切后端模型加载慢）
- **实测**：切后端/刷新时模型下拉框加载慢或失败（LM Studio API 实测 2.07s）；F5 时 LM Studio 未就绪 → 获取失败
- **验证结论**：恢复逻辑本身正确（完整列表模拟：lmstudio→qwen/qwen3.5-35b-a3b 恢复 OK；llama.cpp→Qwen3.5-35B-A3B GGUF 恢复 OK；两模型都在各自列表里）
- **实现**：LLMClient.list_models 加 30 秒 TTL 缓存（key=backend|base_url）——LM Studio /v1/models 2 秒 → 第二次秒回；llama.cpp rglob 扫描也缓存
- 验证：单测首次 2.00s 调 API、第二次 0.000s 命中缓存、API 只调 1 次；py_compile 全绿；bump 3.0.0b41

## [3.0.0b40] - 2026-08-17
### 修复（模型存空根因——选中即应有值，切换后端后丢失）
- **现象**：选了模型（LM Studio/llama.cpp），切后端再切回来模型不恢复——config 里 model 存成了空
- **根因（存空机制）**：onBackendChange 填好 model → 调 refreshModels（异步重建下拉框为"加载中"，value 变空）→ 立即调 autoSaveModelConfig（600ms 后读 DOM 表单）——模型列表未加载完时下拉框 value='' → 保存空 model → 覆盖刚填的 → config 槽 model 变空 → 下次切回没得恢复
- **修复**：内存变量 `_modelValues = {planner, writer}` 记录当前模型值——autoSave 的 model 读内存值（不读正在重建的 DOM）；四个同步点：loadConfig 恢复、onBackendChange 填值、refreshModels 恢复成功、手动选模型（change）
- 验证：py_compile 全绿 + node --check JS 语法通过；bump 3.0.0b40

## [3.0.0b39] - 2026-08-17
### 修复（模型不跟着恢复——切换后端/重启后已选模型丢失，需重新选择）
- **现象**：切换后端/重启后，模型下拉框不恢复已选模型，每次重新选择
- **根因**：b38 清掉 config 顶层残留后，loadConfig 调 `refreshModels('planner', pm.model)` 传的是顶层 `pm.model`（现在 undefined）——正确值在 `profiles[backend].model`（pmProf.model）；savedValue=undefined → refreshModels 恢复逻辑匹配不到 → 下拉框不恢复
- **修复**：loadConfig 传参改为 `pmProf.model` / `wmProf.model`（当前后端槽的 model）
- 验证：模拟链路——顶层 None vs 槽内 35B GGUF，修复后传槽值恢复 ✓；py_compile 全绿；bump 3.0.0b39

## [3.0.0b38] - 2026-08-17
### 修复（b37 分槽数据污染——切换后配置错乱，规划/写作逻辑不一致）
- **切换乱象**：切 LM Studio 再切回 llama.cpp 配置不跟着变、模型不跟着、规划写作不一致
- **根因确认（config.json 污染）**：①writer_model.profiles[llama.cpp] 被写成 base_url=http://localhost:1234（LM Studio API 地址）+ model 空——onBackendChange 加载失败时表单残留上一个后端值，autoSave 把错值存进新槽 ②loadConfig 槽缺失回退 `|| pm` 读到顶层残留旧字段（base_url=C:\...\model）③顶层旧字段残留干扰
- **修复**：①loadConfig 槽缺失 → 用后端默认值（绝不读顶层残留；profiles 存在时顶层作废）②onBackendChange fetch 失败 → 也填当前后端默认（防残留值保存污染）③清理 config.json 污染（writer 两个槽恢复正确值，清顶层残留）
- **清理后配置**：planner/writer × lmstudio/llama.cpp 四槽全正确（lmstudio→1234+qwen；llama.cpp→本地路径+35B GGUF）
- 验证：_model_profile 读取四槽正确；py_compile 全绿；bump 3.0.0b38

## [3.0.0b37] - 2026-08-17
### 新增（模型配置按后端分槽——切回 lmstudio 后配置未恢复，落盘配置应随后端切换）
- **需求**：每个后端（LM Studio/llama.cpp/Ollama）一套独立配置，切后端自动恢复对应落盘配置——LM Studio→http://localhost:1234、llama.cpp→本地 GGUF 路径、切回 LM Studio→自动恢复 1234
- **根因**：planner_model/writer_model 是扁平结构 {backend, base_url, ...}——切后端不改 base_url（onBackendChange 只改 placeholder/title），切回 LM Studio 时 base_url 还是上次 llama.cpp 的本地路径
- **实现（profiles 分槽）**：`{backend, profiles: {lmstudio: {...}, ollama: {...}, llama.cpp: {...}}}`——①后端读取辅助 `_model_profile()`（model_backend 单一实现，web_ui._create_writer_client/_create_planner_client/repair engine 共用；旧格式无 profiles 兼容）②前端 autoSaveModelConfig 保存时先 GET 合并（只更新当前后端槽，保留其他后端槽）③onBackendChange 切后端时异步加载该后端槽填表单（无槽则填默认 LM Studio 1234/Ollama 11434，防残留）④loadConfig 按 backend 取槽
- 验证：_model_profile 7 场景单测全过（各后端槽切换/旧格式兼容/无槽空/空配置）；py_compile 全绿；bump 3.0.0b37

## [3.0.0b36] - 2026-08-17
### 优化（offload_kqv=False——同样预分配 KV，LM Studio 不慢的根因）
- **质疑链**：①n_ctx 大为什么慢（attention 只按实际序列算，不按 n_ctx）②同样预分配 KV，LM Studio 为什么不慢
- **真根因**：llama-cpp-python 默认 `offload_kqv=True`——KV cache 预分配在显存（GPU 层 KV 占显存 2.3GB@262144）→ 8GB 显存被 KV 挤占 → 权重只能 offload 10 层；**LM Studio 的 KV 放共享内存**（截图共享 GPU 内存 31.6GB = 同款策略效果）→ 显存几乎全给权重
- **实现**：llm_client.py + model_backend.py 两处 Llama(...) 加 `offload_kqv=False`（KV 全放 CPU 内存）+ `_estimate_gpu_layers` 加 offload_kqv 参数（False 时 KV 不占显存，只按权重算）
- **实测（真实 GGUF + mock 显存 8188）**：offload_kqv=True → 10 层 GPU；False → 14 层 GPU（CPU 30→26 层）——**14 层 = 8GB 显存权重物理上限**，预期提速 10-15%（9.4 → ~11 t/s）
- **天花板定论**：8GB 显存物理放不下 35B——26 层 CPU 是硬限制，offload_kqv 只是把 KV 让给权重；质变需换 14B 模型或更大显存
- 验证：py_compile 全绿；真实 GGUF 层数对比 10→14；bump 3.0.0b36

## [3.0.0b35] - 2026-08-17
### 优化（t/s 实时显示——长时生成需实时观测，而非等生成完成才见）
- **需求**：生成过程中实时看到速度（b34 只在生成完成后打印，长任务等于看不到）
- **实现**：_llama_generate 改流式（create_chat_completion stream=True）——逐 token 迭代累计 content/token 数，每 2 秒 `
` 原地刷新 `[llama] 模型名 生成中: N tokens, X t/s`；完成打印最终统计；返回结构与 stream=False 一致（content+finish_reason，截断续写逻辑无感知）
- **不损失速度**：stream=True 内部仍是逐 token eval，仅多 Python 侧迭代（35B CPU 5-15 t/s 下开销可忽略）
- 验证：mock 流式 20 tokens/1.0s → 20.0 t/s 实时+完成双打印；chat/chat_detailed 兼容；py_compile 全绿；bump 3.0.0b35

## [3.0.0b34] - 2026-08-17
### 新增（llama.cpp 推理速度观测——无推理速度查看入口，生成速率不显示）
- **需求**：llama-cpp-python 无 UI 显示 t/s——无法判断 b33 参数是否生效/推理是否正常
- **第一次实现缺陷（实测不符）**：读 `create_chat_completion` 返回的 usage 字段——**0.3.34 源码实测无 usage 字段**（mock 测试编造了 usage 才通过）→ 真实环境 `r.get('usage')` 为 None → 异常被静默吞 → 什么都不打印（生成速率未显示）
- **修正**：改用 `llm.tokenize(输出文本, add_bos=False)` 自数 completion_tokens（prompt 侧无 apply_chat_template 拿不到准确值，去掉）；打印 `[llama] 模型名: 生成 M tokens, 用时 Xs, Y t/s`
- **教训**：mock 测试不能编造 API 返回——必须先查真实返回结构再写代码
- 验证：无 usage 字段场景 tokenize 自数打印正常（11 tokens/0.5s → 22 t/s）；两条调用链正常；py_compile 全绿；bump 3.0.0b34

## [3.0.0b33] - 2026-08-17
### 优化（llama.cpp 推理参数——LM Studio 同硬件更快，截图反证窗口/offload 归因错误）
- **反证**：LM Studio 截图——上下文 96000（不是小窗口）、GPU 卸载设 40（同 8GB 显存）、Unified KV Cache 开、Context Checkpoints 32、评估批处理 768/512、CPU 线程 10——"快"不是 offload 层数差
- **查证**：llama-cpp-python 0.3.35 只有版本号 bump（chore）；主分支 llama_cpp.py 源码无任何 checkpoint 绑定（LlamaContextCheckpoint/checkpoint/ckpt 全无）——Context Checkpoints 在 Python 绑定中不存在，**非版本问题，设计上未实现**（依赖升级不现实）
- **根因**：代码 Llama 实例化只传 flash_attn/type_k/type_v——n_threads（CPU 层多核并行）、n_batch（prefill 批量）、use_mlock（20GB 权重防换页）全用默认 → 35B 31/40 层在 CPU 上没吃满多核
- **实现**：①LLMClient._llama_n_threads()——wmic 物理核数探测（AMD 7845HX 12C24T → 12）②llm_client.py + model_backend.py 两处 Llama(...) 加 n_threads=12/n_threads_batch=12/n_batch=1024/n_ubatch=512/use_mlock=True
- 验证：物理核数探测=12；mock Llama 捕获参数全生效；py_compile 全绿；bump 3.0.0b33
- **遗留**：Context Checkpoints 做不了（llama-cpp-python 无此 API）——已划掉不画饼

## [3.0.0b32] - 2026-08-17
### 修复（全文三检触发守卫——全文写完才触发，if 规划 else 全文三检）
- **语义**：全文三检只在最后一章章检通过修复完、后续不再有任何规划时触发——if 规划 else 全文三检
- **根因确认**：generate_novel_article 主循环结束后【无条件】执行 finalize_novel_full——全书未写完（有章 pending/章检没过）也跑全文三检 → verify_ending 兜底把 ending 收束问题挂到 L01 → 弹窗显示"L01 全文质检（三检修复项）"——章检项与三检项并存，违背"同一章不会同时有"的设计
- **修法**：全文三检前加守卫 `all(section.status == 'done')`——任一章未 done（pending/planning/in_progress/confirmed）= 还有规划/写作 → 跳过全文三检（打印原因）；全部 done（最后一章章检通过修复完）才触发；fn=None 时质检报告段跳过
- **触发点定论**：全文三检的唯一点位 = 全书所有章 done 之后（后续不再有规划）
- 验证：守卫逻辑 6 场景单测全过（全书done触发/L01 pending跳过/部分done跳过/planning跳过/confirmed跳过）；py_compile 全绿；bump 3.0.0b32

## [3.0.0b31] - 2026-08-17
### 修复（跳过=通过：跳过后不再重检重置——跳过语义与上一章检测覆盖冲突）
- **流程**：规划章内子结构→写作→检测→通过→规划下一章子结构——跳过=通过，不该再弹
- **根因确认（b29 也没覆盖）**：跳过（_repaired=True）后，novel_writer 646-665 **立即重检 L01**——重检仍有 HARD（跳过≠修复，正文没改问题当然还在）→ 663-665 显式 `_repaired=False` 重置 → save_repair_hint → 轮询又弹。b29 的 setdefault 保留只对"result 没带 _repaired"生效，665 是显式 False，挡不住
- **修复**：修复循环轮询到 `_repaired=True` 后，检查 `_repair_result.skipped`（skip 已写）——**跳过 → 不再重检、不重置，直接通过进入下一章**（_fc_final 置通过）；修复完成（非跳过）→ 照旧重检
- 验证：py_compile 全绿；bump 3.0.0b31

## [3.0.0b30] - 2026-08-17
### 清理（删除冗余跳过标识——跳过/未勾选均等同于通过，无需独立标识）
- **语义定稿**：跳过 / 未勾选 = 通过——统一一个"通过"语义，不要单独的跳过标识
- **确认**：`_skipped_all` 只有 `_handle_repair_skip` 一处写入，**没有任何读取**——纯冗余；skip 已设 `_repaired=True` + 清 issues/full_items（这就是"通过"语义）
- **清理**：删除 `_skipped_all`（skip 统一走 `_repaired=True` + 清 issues/full_items）
- **保留（有实际用途）**：`_skipped_subs`（未勾选段记录——重检时过滤未勾选段问题）；`_repaired`（弹窗核心判定）
- **语义统一**：跳过 = `_repaired=True`（通过）；未勾选 = 从 issues 移除（通过）——弹窗只认 `_repaired` / issues 空
- 验证：py_compile 全绿；`_skipped_all` 全局清零；bump 3.0.0b30

## [3.0.0b29] - 2026-08-17
### 修复（跳过修复后又被弹窗——跳过未记录 True/ok，规划后重弹修复）
- **流程确认**：跳过所有修复 → 自动规划下一子结构 → 规划完又弹修复窗口 → 被迫点修复 → 重加载模型 → 链路错乱
- **根因确认**：`StateManager.save_repair_hint` 用 `hints[chapter_id] = result` **整个覆盖**——章检（novel_writer 628 的 `_checks_result`）**不含 `_repaired`** → 覆盖时把"已跳过"的 `_repaired=True` 冲掉 → `get_progress` 的 `repair_pending` 判定（`if not hint.get("_repaired")`）→ **又弹**
- **修复**：`save_repair_hint` 覆盖时**保留已有 `_repaired`**（`merged.setdefault("_repaired", old...)`）——result 显式带 `_repaired` 时以其为准（重检通过置 True / 重检仍 HARD 重置 False，656/665 行为不变）；无旧值默认 False（正常弹窗）
- 验证：3 场景单测全过（跳过后章检保留 True 不再弹 / 显式 False 生效重置 / 无旧值默认 False）；py_compile 全绿；bump 3.0.0b29

## [3.0.0b28] - 2026-08-17
### 修复（修复复用共享实例——跳过修复→规划→弹修复→点修复 内存不足）
- **流程确认**：跳过修复 → 规划下一子结构（35B 加载进共享表）→ 规划完弹修复窗口 → 点修复 → **b24 的 share=False 让修复独立加载第二份 35B** → 内存两份 35B → 窗口兜底 8192 → 修复失败（内存不足）
- **b24 修错方向**：当时"共享之后崩溃"的僵尸根因是 `_release_repair_client` 的**强制 close**（close 后共享表弱引用仍指向已释放底层 ctx 的实例 → 复用 segfault）——**正确解法是去掉 close，不是让修复独立**
- **修复**：`_create_repair_client` 恢复 share=True（复用共享——规划/写作/修复同一模型只加载一次，b20 核心诉求）；`_release_repair_client` **去掉 close**（只释放 client 引用——有其他持有者则复用，全部回收则弱引用自动卸载，僵尸无来源）
- **完整闭环（验证）**：规划(加载) → 弹修复 → 点修复(复用) → 修复完成(不 close) → 继续规划/写作(复用) → 全部结束(自动卸载)——**全程一份 35B**
- 验证：5 场景单测全过（规划→修复→写作只加载1次 / 同实例 / 修复释放不影响 / 全部回收自动卸载 / 无僵尸重新加载）；py_compile 全绿；bump 3.0.0b28

## [3.0.0b27] - 2026-08-17
### 修复（修复静默失败 + 无过程反馈——仅提示未执行修复）
- **现象**：日志只有"加载成功（n_ctx=8192）→ 修复完成卸载"，没看到修复结果；GPU 跑着但不知道在干嘛；对比 LM Studio 无此问题
- **根因确认（静默失败）**：n_ctx=8192 是**内存不足的动态窗口兜底值**（内存可容纳 ≤0 → 8192）；修复 max_tokens 继承 writer 的 81920 **远超 8192 窗口** → llama_cpp 报 "Requested tokens exceed context window" → rewrite 失败 → 只显示"加载→卸载"，**没有修复结果可见**（UI 也不显示失败原因）
- **修复（三层）**：
  1. **超窗降级**：LLMClient 加 `_llama_max_tokens()`——llama.cpp 分支 max_tokens 自动 min(n_ctx−1024, 最小 256)——n_ctx=8192 时输出上限降到 7168，修复能跑而不是直接报错
  2. **过程日志**：rewrite_segment 打印"正在重构 S02（窗口 N）..."——可观测 GPU 状态；加载时 n_ctx<16384 打警告"内存不足窗口仅 N"
  3. **UI 失败原因**：pollRepairStatus 显示 failed 段的原因（problems）——"失败 N 段（文件: 原因）"红色显示
- 验证：超窗降级 3 场景单测过（8192→7168 / 262144 保持 / 未知保持）；三文件 py_compile 全绿；bump 3.0.0b27

## [3.0.0b26] - 2026-08-17
### 修复（修复状态会话隔离——A 会话点修复，B 会话也显示/执行）
- **现象**：在会话 A 点击修复，B 会话的修复面板也显示修复状态/进行中——"好几个会话一起跑"的错觉 + 多次加载模型
- **根因确认**：`_repair_state` 是**模块级全局单例**——所有 session 的修复状态/防重入共用一份；前端 `pollRepairStatus` 调 `/api/novel/repair/status` 不带 session_id → 返回全局状态 → **任何 session 的页面都显示同一修复**；防重入也全局互锁（A 修复时 B 被拒/或状态串）
- **说明**：run() 本身只修请求的 session_id（不会真的修 B）——界面显示"B 也修"是全局状态误导；两条 [llama] 是"A 修复 + 之前 B 的操作"各自加载
- **修复（会话隔离）**：`_repair_state` → `_repair_states: dict`（key=session_id）；apply 防重入/置位按 session；`_run` 完成按 session 写；status 接口读 query `session_id` 返回对应 state；前端 `pollRepairStatus` fetch 带 `?session_id=`（currentSessionId）
- 验证：py_compile 全绿；`_repair_state` 全局引用清零；bump 3.0.0b26

## [3.0.0b25] - 2026-08-17
### 修复（apply 防重入竞态——单击触发双加载）
- **现象**：点一次"开始修复"却出现两条 `[llama] 加载成功`（n_ctx 251447 / 250240 不同 = 两个时刻）——run() 内只有一处 client 调用（单例复用），两条 = 两个 `_run` 线程
- **根因（竞态窗口）**：b22 防重入的"检查 `running`"与"置位 `running=True`"是两个**分离的锁块**——两次 apply（前端重复提交/双击）在"检查通过 → 置位"之间并发时**双双放行** → 两个 `_run` 线程 → 两条 35B 加载（且并发调用 Llama 实例 = 崩溃风险）
- **修复**：检查与置位合并到**同一锁块**（原子化）——第二次 apply 必被拒（无论何时到达）
- 验证：py_compile 全绿；bump 3.0.0b25

## [3.0.0b24] - 2026-08-17
### 修复（修复引擎独立实例——崩溃出现在共享机制引入之后）
- **两条线索**：①崩溃与共享机制强相关（共享引入后出现）②curl 单次测试不触发复用、连续操作才触发
- **根因确认（僵尸实例复用）**：`_release_repair_client` 显式 `llm.close()` 释放了 35B 底层 ctx，但**共享表 `_SHARED_LLAMA` 的弱引用还在**——实例对象未死（底层已死）。下次任何操作（修复/写作）`_llama_instance` 命中该僵尸实例 → 调用已释放的 ctx → **segfault → 无输出崩溃**。共享表是**跨 session 全局**的——我测试 close 的僵尸实例，任何 session 都可能命中
- **为什么我测试不崩**：curl 单次 apply → close → 测试结束，没再触发复用；连续操作（修完再修/修完写作）→ 命中僵尸 → 崩
- **修复**：`LLMClient` 加 `share` 参数（默认 True）——`share=False` 时**不查共享表、不写共享表**；修复引擎 `_create_repair_client` 传 `share=False`（独立加载/close，僵尸无来源）；共享仍服务于规划/写作（b20 定的范围）；`_release_repair_client` 简化（独立实例 close 安全，不再需要判空逻辑）
- 验证：share=False 单测 7 项全过（修复独立加载不复用 / 不进共享表 / close 只关自己的 / 写作复用正常实例 / 连续多次修复无僵尸）；py_compile 全绿；bump 3.0.0b24

## [3.0.0b23] - 2026-08-17
### 回滚（b21 修复窗口限制——回滚崩溃后改动的显存/动态层数相关修改）
- **指令**：崩溃后改的"显存/动态层数"相关改动改回来——崩溃真因是并发（b22 防重入已修），窗口限制是错误方向
- **判断正确**：当前环境 b20 日志 `n_gpu_layers=9` → 9 层 + 262144 窗口显存 = 4.5GB 权重 + ~1.5GB KV ≈ 6GB < 8GB，**根本不崩显存**——b21 窗口限制（n_ctx=16384）对当前环境是多余的
- **回滚**：`_create_repair_client()` 恢复继承 writer 配置（max_tokens 原值、n_ctx 自动推导），删 b21 的 max_tokens cap + n_ctx 固定
- **保留**：b22 防重入（`running` 标志，并发崩溃真因修复）、b21 `_release_repair_client` 显式 close（防假卸载）
- 验证：py_compile 全绿；bump 3.0.0b23；服务器重启

## [3.0.0b22] - 2026-08-17
### 修复（修复 apply 防重入——根因在代码级并发，非内存/KV）
- **定位**：根因不在内存/KV 而在代码级——同一 b21 代码 curl 单次 apply 执行不崩、浏览器 UI 执行崩溃 → 差异在请求并发
- **根因确认（代码级）**：`_handle_repair_apply` 的 `_repair_lock` 只保护 `_repair_state` 写入（1733），**run() 后台线程执行完全在锁外**——重复 apply（前端重复提交/手动再点"开始修复"）会启动**多个 `_run` 线程并发跑 `run()`**，而修复 client 是模块级单例 `_REPAIR_CLIENT`（同一 Llama 实例）→ **llama_cpp 线程不安全 → 并发调用同一实例 → segfault → 无输出崩溃**（无 WER、无 traceback、cmd 直接关闭——与观察到的现象完全吻合）
- **为什么我 curl 不崩**：单次 apply → 单线程 → 无并发
- **修复**：`_handle_repair_apply` 开头加防重入——`with _repair_lock` 检查 `_repair_state["done"]`，修复进行中（done=False）→ 拒绝新 apply（返回"已有修复任务进行中"）
- 验证：py_compile 全绿；bump 3.0.0b22；服务器重启生效

## [3.0.0b21] - 2026-08-17
### 修复（修复引擎 35B 不释放 + OOM 崩溃——实测抓包）
- **现象链**：①日志"加载成功→复用→卸载"三连但修复秒级完成，S02 没被改②"卸载"打印但 GPU 还在跑③cmd 直接崩溃退出
- **确认**：
  1. **假卸载**：`_release_repair_client()` 只置 None + gc——llama_cpp Llama 实例的 `__del__` 依赖 `close()`，引用循环/异常路径下 close 不执行 → 35B 永不释放（实测 python 进程 22.4GB 内存 + 6.5GB 显存残留，直到崩溃后进程退出才释放）
  2. **OOM 崩溃**：修复加载 35B 时 n_ctx 推导到模型原生 262144 → 生成时 KV cache 膨胀 10-30GB（q8_0 ~80KB/token）+ 权重 20GB + 3B 常驻 6GB → 66GB 机器被击穿 → python 被系统杀（b11 同款病根：KV cache 是内存爆放大器）
- **修复**：
  1. `_release_repair_client()`：释放引用后**查 LLMClient 弱引用共享表判空**——无其他任务持有 → **显式 `llm.close()`**（不依赖 `__del__`）；有其他任务持有 → 只释放本 client 引用不 close（防杀掉共享实例）；日志区分"真卸载"/"仍被持有"/"无实例"
  2. `_create_repair_client()`：**修复专用窗口限制**——max_tokens cap 到 8192（重构单段 ~1500 字，8K 输出上限绰绰有余，原样继承 writer 81920 会超 n_ctx 报错）、n_ctx 固定 16384（KV q8_0 仅 ~0.7GB，不再膨胀 10-30GB）
- 验证：window_info 显式 n_ctx=16384 生效（read_space=8192）；Llama.close 方法确认存在；py_compile 全绿；服务器重启（日志落盘，便于后续排查）

## [3.0.0b20] - 2026-08-17
### 修复（35B 也不常驻——强引用常驻是错误方向，只需解决同模型不二次加载）
- **纠正**：整章完成后应卸载规划/写作模型 → 加载 8B 做 4维 → 卸载 → 加载 7B 做 R1 → 卸载 → 修复时再加载写作模型；写作/规划模型不常驻，只需解决同模型不二次加载
- **确认**：我 b17-b19 保留的 LLMClient 强引用（35B 常驻）是错的——8GB 显存被 35B 占着，8B/7B 判定模型无法正确用上 GPU；35B 常驻还叠加判定模型内存压力
- **执行链**：批量/章级入口（web_ui 2278 等）函数内创建 writer+planner client 全程复用，函数结束回收——正是"章内常驻、章后卸载"的天然边界
- **修复**：
  1. `LLMClient._SHARED_LLAMA` 改 **weakref.WeakValueDictionary**——章内规划/写作交替持 client 句柄 → 同一 35B 复用不二次加载；任务结束 client 回收 → 35B 自动卸载（显存让给 8B/7B）
  2. 修复引擎 `_release_repair_client()`——run() 的 finally 显式释放模块级 `_REPAIR_CLIENT`（修复循环内多段复用 35B，修复完成即卸载，防单例常驻）
- **三种共享语义定论**：全部弱引用——LLMClient（35B 任务内复用/任务后卸载）、model_backend（判定模型测完即卸）、修复引擎（循环内复用/循环后显式释放）
- 验证：LLMClient 弱引用单测 5 项全过（规划+写作同模型复用 / client 回收自动卸载 / 下一任务重新加载 / 修复循环内复用 / 修复结束卸载）；_release_repair_client 释放+幂等验证过；四文件 py_compile 全绿；服务器已重启

## [3.0.0b19] - 2026-08-17
### 修复（b18 判定模型共享改弱引用——测完卸载是否有影响）
- **问题**：判定模型测完是否需彻底卸载再加载下一个（8B→7B→规划/写作）；共享缓存对判定模型看似用不上——若仅为冗余无影响，但需确认不引发其他问题
- **确认**：**不是冗余，是真问题**——b18 的共享表是**强引用**，8B/7B 加载后常驻进程无法卸载，与"测完彻底卸载"直接冲突（8B~5GB+7B~4GB 堆积，等 35B 20GB+KV 30GB 加载时逼近 63GB 上限，之前实测爆过）
- **执行链确认**：`finalize_chapter` 每章调用 `check_4dim`(8B) → `check_reasoning`(7B) 交替，章内串行"测完即卸"——共享缓存对判定模型确实"用不上"（每次都是新加载），判断正确
- **修复**：
  1. `model_backend._SHARED_LLAMA` 改 **weakref.WeakValueDictionary**——有强引用才共享（35B 写作/规划 client 持句柄 → 常驻复用），无强引用自动卸载（判定模型局部句柄消失 → 弱引用失效 → 真释放）
  2. 删 `novel_4dim_check._4DIM_HANDLE` global 缓存（b18 加的强引用元凶）——回到"每章加载即卸"
  3. 删 `novel_reasoning_check._LLM/_TOKENIZER` global 缓存（b11 遗留，7B 从不卸载）——同样"测完即卸"
- **保留**：LLMClient._SHARED_LLAMA（35B）强引用——写作/规划常驻共享是设计目标，未要求卸载 35B
- 验证：弱引用单测 7 项全过（测完即卸表内自动移除 / 下次重新加载 / 持句柄复用 / 双句柄释放表空 / 释放后 get None）；残留引用检查干净；三文件 py_compile 全绿；服务器已重启

## [3.0.0b18] - 2026-08-17
### 新增（判定模型共享——判定模型场景更适用）
- **论点**：审核判定模型更需要共享——章内审核多次调用反复加载；且判定模型只有固定 8B/7B/3B 三个角色、分阶段执行（4维→fidelity→R1）、judge_n_ctx 统一 → **永远不会有"各开各"的情况，纯共享场景**
- **现状确认**：4维 `_load_model` 无缓存 → 每章六检都重新加载 8B（38s+，一本书 N 章 = N 次）；R1 有模块级缓存（`_LLM` global）、fidelity/提取 3B 有缓存（`_EXTRACT_PIPE` global）——8B 是唯一漏网
- **实现**：
  1. `model_backend.load_llama` 加进程级共享表 `_SHARED_LLAMA: {规范化path → (Llama实例, n_ctx)}` + threading.Lock——同 path 复用（打印 `复用共享实例`），不同 path 独立；n_ctx 需求更大（判定场景被 clamp 到 16384 后不可能发生）→ 独立加载不覆盖首实例
  2. `novel_4dim_check._load_model` 加模块级缓存 `_4DIM_HANDLE`（与 R1 同款模式）——消除每章重复 judge_backend/路径解析/日志噪音
- **关键印证**：load_llama 判定模型 n_ctx 硬 clamp `min(n_ctx, 16384)` → 任何 n_ctx 需求（65536/4096）都归一 16384 → 共享表永远命中——**"判定模型永不各开各"的论点是代码语义层面的证明，不是巧合**
- 验证：单测 11 项全过（同 path 只实例化 1 次 / 不同 path 独立 / 第 3 次复用 / 65536 需求 clamp 后复用 / 4096 复用 / 连续 5 次调用仍 1 实例 / 4维 第二次调用走缓存不重载）；py_compile 全绿；服务器已重启

## [3.0.0b17] - 2026-08-17
### 新增（llama.cpp 实例进程级共享）
- **现象**：`[llama] 加载成功` 日志出现两次，n_gpu_layers=9 和 1——同一 35B 被加载两份
- **根因**：`planner_model` 与 `writer_model` 都配了同一 GGUF 且都是 llama.cpp → `_create_planner_client` / `_create_writer_client` 两个独立 LLMClient 各缓存各的实例（`self._llama` 实例级）→ 20GB 模型驻留两份（修复引擎 `_REPAIR_CLIENT` 也读 writer 配置，跑 llama 时第三份）；n_gpu_layers 不同是因为 `_estimate_gpu_layers` 按当前可用显存实时估算，第二次加载时第一个实例已占显存 → 只剩 1 层
- **铁律**：相同模型可以只加载一次；**不同模型必须分开加载**
- **实现**：`LLMClient` 类级共享表 `_SHARED_LLAMA: {规范化模型路径 → (Llama 实例, n_ctx)}` + `threading.Lock` 双重保护；`_llama_instance` 先查共享表——命中且需求 n_ctx ≤ 共享实例 → 复用（打印 `[llama] 复用共享实例`）；需求更大（罕见，显式配置大窗口）→ 独立加载且不覆盖首实例；未命中 → 正常加载并写表。不同模型路径 → 天然独立实例
- 验证：单测 11 场景全过（同模型两 client 只实例化 1 次且同实例 / 不同模型分开 / 第三 client 复用 / 需求更小复用、更大独立且不覆盖首实例 / 后续大需求独立）；py_compile 全绿；服务器已重启

## [3.0.0b16] - 2026-08-17
### 修复（修复面板 HARD/SOFT 过滤的 3 个 bug——实测抓包）
- **现象 1**：勾选框点击后视觉状态不消失（取消勾选后仍显示打勾）
- **现象 2**：HARD/SOFT 像互斥开关——点 SOFT 后所有子结构下的问题全消失，但子结构本身不消失且全部打√；点 HARD 又"恢复最初"
- **根因**：
  1. `renderRepairBody` 重建 `panel.innerHTML` 时 HARD/SOFT 勾选框 `checked` 硬编码 true → 每次 onchange 重渲染后视觉永远恢复打勾（现象 1）
  2. `(pv.files || []).forEach(...)` 无条件初始化所有子结构 → 级别过滤后无问题的子结构仍显示且带 √，违反"完全隐藏"设计（现象 2 前半）
  3. 因根因 1 重建后 SOFT 勾选框回 checked，点 HARD 时 `wantSoft` 误读为 true → SOFT 行全显示，误以为"恢复最初"（现象 2 后半）
- **修复**：勾选框 `checked` 用 `${wantHard ? 'checked' : ''}` 按当前状态回填；fileMap 改为只聚合 `pv.files` 内且有匹配问题的文件（无问题文件不生成条目 → 完全隐藏）；顺带修问题行聚合时未去掉 `S01.txt: ` 文件名前缀（显示重复）
- 验证：node 单测 11 场景全过（全 SOFT 只勾 HARD → 0 子结构显示占位；混合只勾 HARD → 无 HARD 的子结构隐藏；T0 末行行不参与聚合）；真实 preview 数据（22 issues）验证——只勾 HARD 仅显示 S02、其余 3 个全隐藏；py_compile 全绿；服务器已重启

## [3.0.0b15] - 2026-08-17
### 新增（修复面板 HARD/SOFT 级别过滤）
- **需求**：修复面板只有子结构勾选不够——需 HARD/SOFT 级别勾选。都勾 = 现状（所有检出作为子结构备选）；只勾一个 = 过滤掉未勾选级别的问题，**未勾选级别的问题子结构完全隐藏**（防错误勾选：全 SOFT 子结构在只修 HARD 时直接消失）
- **现状确认**：子结构勾选已生效（前端 rp-check → applyRepair checked_subs → 修复引擎 `seg_map = {k: v for k, v in seg_map.items() if k in checked_subs}` 只重构勾选文件）
- **实现**：`renderRepairPreview` 拆分出 `renderRepairBody()`（preview 数据缓存 `_repairPv`，级别过滤重渲染复用）；面板加 HARD/SOFT 两个 checkbox（默认勾，onchange 重渲染）；过滤规则 `[HARD]`/`[FAIL]` 归 HARD、`[SOFT]`/`[WARN]` 归 SOFT，未勾选级别整行滤掉 → 全隐藏该级别子结构；显示当前子结构数
- **坑**：前端正则初版用 `^\[(HARD|...)\]`（锚定行首），但 preview 实际格式是 `S01.txt: [HARD] problem`（severity 在文件名后）——非锚定 `\[(HARD|...)\]` 才对；单测用错格式（凭空加缩进）误报逻辑错，用真实格式验证全对
- 验证：级别过滤 5 场景单测全过（都勾 4 / 只HARD 2 / 只SOFT 2 / 都不勾 0 / 全SOFT子结构隐藏）；编译全绿；服务器已重启

## [3.0.0b14] - 2026-08-17
### 修复（n_gpu_layers=-1 触发 VMM 慢路径——加载 200s+ 的元凶）
- **实测**：实测 35B 加载 269s/211s（"加载 5 分钟"），追问根因
- **根因**：`n_gpu_layers=-1`（全 GPU）在放不下的模型（35B 19.7GB > 8GB 显存）上**不会 OOM 失败**——llama.cpp 0.3.34 用 VMM（虚拟内存管理）把权重硬映射进显存，放不下的溢出到系统内存 → 加载 200s+ 且溢出层实际仍 CPU 算。项目代码 `layers_try=[-1]` 第一个就试 -1 → 35B 必中慢路径
- **顺带发现**：估算层数=0 的场景（显存被 LM Studio 占满）→ 35B 纯 CPU 跑 → 这是"GPU 完全不占用"的真因（nvidia-smi 91% 是 LM Studio 自己在跑写作）
- **修复**：llm_client `_llama_instance` + model_backend `load_llama`——模型放得下全 GPU（估算层数 ≥ 总层数）才试 -1；放不下 → 跳过 -1 直接用估算层数；估算失败 → 纯 CPU
- 验证：35B 加载 211s → **38s**（n_gpu_layers=1，显存被 LM Studio 占满时估算准确）；双版本编译全绿；服务器已重启

## [3.0.0b13] - 2026-08-17
### 修复（GPU 层数估算计入 KV cache + KV 量化 flash_attn 路径）
- **背景**：观测到"CPU 满载、GPU 12% 出力不足"——partial offload 串行（GPU 层算完等 CPU 层）是架构固有；但 `_estimate_gpu_layers` 只留 1GB KV 预算，实际 n_ctx=262K 时 KV cache 占显存 6.8GB（实测 1B 全 GPU：n_ctx 1024→8192 显存差 516MB ≫ 理论 56MB——KV 在显存确认）→ 层数估算虚高
- **C 修复**：`_estimate_gpu_layers(model_path, n_ctx=0)` 两处（llm_client + model_backend）——KV cache 显存按层比例分摊计入（`n_ctx × KV每token / 总层数`）；n_ctx=0 兼容旧调用；调用点传 n_ctx
- **B 修复（关键发现：flash_attn 路径支持 KV 量化）**：默认 attention 路径 type_k=8（Q8_0）报 `SET_ROWS 无法在 CUDA buffer 运行`（0.3.34 CUDA 后端限制）；但 **`flash_attn=True` 绕开该限制，Q8_0 KV 量化完全可用**——实测 1B 全 GPU：fp16 KV +2141MB vs flash+Q8_0 +1729MB，**省 412MB**；部分 offload（12 层 GPU）+ flash + Q8_0 加载成功、推理正常
- **落地**：两处 Llama 加载（llm_client `_llama_instance` + model_backend `load_llama`）加 `flash_attn=True, type_k=8, type_v=8`；C 估算 KV 系数 0.5（q8_0 减半）。效果：35B n_ctx=262K → 7 层（fp16 虚高不计）→ **9 层（q8_0 减半后回升）**
- **结论修正**：KV 量化在 Python 直挂可行（flash_attn 路径）——35B 走 llama.cpp 直挂 GPU 层数从 13 层虚高修正为 9 层真实值（q8_0），比 fp16 的 7 层多放 2 层；写作用 LM Studio 仍是正解（GPU 满载 + MoE offload），llama.cpp 直挂服务 8B/7B 判定
- 验证：35B 估算（16K→14 层 / 262K→9 层）；1B 端到端（flash+Q8_0 加载+推理+省显存）；双版本编译全绿；服务器已重启

## [3.0.0b12] - 2026-08-17
### 新增（窗口 = 内存动态 + 判定模型 max_tokens 定稿）
- **规划/写作模型窗口（定稿规矩）**：`n_ctx = min(模型原生窗口, 内存可容纳窗口)`——内存可容纳 = (可用物理内存 × 0.8 − 模型权重) ÷ KV每token（安全系数 0.8 拍板）；读 = n_ctx − max_tokens。**用"可用内存"做基数**（物理总内存算 35B 会得出 384K > 原生 262K，min 后等于没限制——正是要防的"莽原生"）；内存紧张时窗口自动缩小（模拟 28GB 可用 → 35B n_ctx=35K 防爆）
- **KV 每 token 实算**：GGUF 元数据 `2 × 层数 × KV头 × head_dim × 2字节(fp16)`（35B → 80KB/token，与实测 256K≈35GB 吻合）；元数据一次性读入缓存（35B 19.7GB 文件首次 9.6s，缓存后 0s）
- **判定模型 max_tokens 定稿**：4维判定 512→**1024**；忠实度复核 256→**1024**（llama 后端也用 8B，3B 只做提取）；R1 推理审核 4096→**8192**（R1 思考链 1-3K + JSON 5 维，4096 偏紧）；judge_n_ctx 固定 16384 不动态
- **Qwen3-8B 关思考**：llama.cpp 后端（Qwen3 默认思考模式会吞输出 token）4维/fidelity 判定注入 `/no_think`——transformers 3B 无思考概念不注入
- **前端两级提示（只提示不强制）**：`/api/llm/window` 返回 n_ctx/读空间；max_tokens 超 n_ctx/2 → 黄"读写不平衡"；超 n_ctx−4096 → 红"超上限（llama.cpp 会报错/截断）"；配置面板 planner/writer 最大Token 改动实时提示，显示当前窗口
- 验证：35B 窗口计算（空闲 50.8GB 可用 → n_ctx=262K；模拟 28GB → 35K）、红/黄提示 API、双版本编译全绿

## [3.0.0b11] - 2026-08-17
### 新增（判定模型双后端：llama.cpp / transformers，配置驱动）
- **背景**：Qwen2.5-3B 判不了"叙事目的/起承转合"抽象标准（实测 22→8 条仍伪问题为主）；换 Qwen3-8B 但 Python 3.14 装不了 llama-cpp-python（无预编译 wheel），3.11 可装——催生双后端
- **路由矩阵（定稿）**：A = Python 3.10~3.11 存在（llama.cpp 选项出现的唯一条件）；B = 用户勾选"统一管理"（仅 A 真时可勾选，写作规划与审查判定统一走 llama.cpp）；**审核判定后端 = A ∧ B → llama.cpp（4维: Qwen3-8B Q4_K_M 5GB / R1: DeepSeek-R1-Distill-Qwen-7B Q4 4.4GB），否则 transformers（3B + 1.5B）**
- **model_backend.py（新）**：`detect_llamacpp()`（版本区间+import）、`judge_backend(cfg)`（A∧B 路由）、`list_gguf_models()`（目录扫描）、`load_llama()`（n_gpu_layers=-1 + OOM 降层回退 CPU）、`release()`（del+gc 按需释放——llama.cpp 无运行时自动卸载，显存错峰靠代码管理）、`generate()`（双后端统一生成接口：transformers 句柄=(model,tokenizer) chatml 解码；llama 句柄=Llama 原始补全）
- **4维/R1 改造**：`_load_model` 先判后端（llama → GGUF 路径；transformers → 现状 3B/1.5B）；`_judge`/`fidelity_judge`/R1 生成全走 `model_backend.generate`；R1 的 pipeline 仅 transformers 后端用，llama 后端直出（Qwen 系认识 chatml）
- **setup.bat**：py launcher 探测 3.11→3.10（通用，不特例任何环境）→ PYCMD + 幂等装 llama-cpp-python（aliyun 镜像，失败仅 WARN 回退方案 1）
- **model_env_check.py**：加 `check_llamacpp()` 探测（版本区间 + import），输出 llama.cpp available/unavailable
- **LLMClient**：backend="llama.cpp" 时 base_url 语义 = 本地 GGUF 路径（文件或目录），list_models 扫描目录 *.gguf，chat/chat_detailed llama_cpp 直挂生成，test_connection 验证路径+llama_cpp
- **web_ui**：规划/写作模型后端 select 加 llama.cpp 选项（A 不可用时禁用）；小说质检区加"统一管理（llama.cpp 判定 8B+7B）"勾选（仅 A 可用时可勾）+ 判定后端徽标 + GGUF 状态行；`/api/novel/status` 返回 llamacpp 可用性/judge_backend/GGUF 就绪；`/api/novel/checks` 保存 unified_management/gguf 路径；`/api/config` 注入 llamacpp.available
- **模型安装按后端分流**：llama.cpp 后端 → 下载 8B+7B GGUF（hf-mirror hf_hub_download 到 data/models/gguf/）+ 3B（实体抽取仍需 transformers）；transformers 后端 → 现状 1.5B+3B
- 验证：3.14（transformers）与 3.11（llama.cpp）双环境路由矩阵全对；全量编译双版本全绿
### 修复（setup.bat 闪退）
- **根因**：setup.bat 以 UTF-8 编码保存，cmd 用 GBK 代码页解析 → 中文/全角标点乱码字节被误解析成命令语法（`'indows' 不是内部或外部命令` + `此时不应有`）→ 启动即闪退
- **修法**：全文件改纯 ASCII（英文注释/echo 文案），任何编码解析都安全；同时消除 bat 经典预展开坑——括号块内 `%errorlevel%`/`%VAR%` 在解析时预展开（3.10 探测恒失败、pip 已装判断恒真），重构为 goto 标签结构 + `if errorlevel`（无 % 运行时判断）
- 验证：修复后 bat 完整跑通——Python 3.11 探测 → llama-cpp 检查 → env_check → main.py 启动（PID 19124 监听 8770）→ HTTP 200；`/api/novel/status` 返回 llamacpp.available=True（3.11 + 0.3.30）、judge_backend=transformers（未勾选统一，正确）
### 修复（前端模型下拉：跨后端配置污染 + 无引导提示）
- **问题**：后端从 lmstudio/ollama 切到 llama.cpp 后，模型下拉出现 "qwen3.5（已配置）"——来源是 config.json `planner_model.model`/`writer_model.model` 旧值（LM Studio 时代配置），refreshModels 的"恢复保存值"分支无条件塞回下拉并标"（已配置）"，误导用户以为扫描到了模型；实际 GGUF 目录为空，选了也加载不了
- **修法（配置隔离 + 引导）**：
  - refreshModels 只恢复「当前后端返回列表内」的值——跨后端一律不恢复，每个后端各管各的模型列表，杜绝互相污染（"（已配置）"无条件添加分支整个删除）
  - llama.cpp 扫描无 GGUF → 下拉显示"（未找到 GGUF — 请在地址栏填写模型目录或 .gguf 文件路径，再点刷新）"，地址栏 placeholder 给示例（C:\models 或 C:\models\xxx.gguf）；地址若是 http://（LM Studio/Ollama 残留 API）→ title 提示"llama.cpp 地址 = 本地模型路径（非 API URL）"
  - 新增 `onBackendChange(prefix)`：后端切换时设置地址栏 placeholder/title 引导 + 刷新模型列表
- 验证：`/api/llm/models?backend=llama.cpp&base_url=http://localhost:1234` → `[]`（空，前端显示引导提示不再恢复旧值）
### 新增（模型配置"选择即持久化"）
- **问题**：模型区（后端/地址/模型/参数）改动必须点"保存配置"按钮才写盘——切换 llama.cpp 配完未点保存，刷新即丢（config 里 planner/writer 仍是 lmstudio + qwen3.5），与 novel_checks 勾选自动保存不一致
- **修法**：新增 `autoSaveModelConfig()`（600ms 防抖）——后端 select change（并入 onBackendChange）、地址 change、模型 select change、超时/max_tokens/温度 change 全部自动 POST /api/config，选择即持久化
- 验证：POST planner/writer = llama.cpp + 本地路径 + GGUF 模型 → config.json 实写成功（backend/base_url/model 全持久化）
### 修复（GGUF 下载走 huggingface.co 官方超时 10060）
- **根因（两坑叠加）**：①`os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")`——环境变量已存在（哪怕是 huggingface.co）就不覆盖 → 走官方 → WinError 10060 连接超时；②huggingface_hub 的 `constants.ENDPOINT` 在库 import 时缓存（服务器进程内 transformers 已 import 过，缓存 huggingface.co），之后再改环境变量也无效
- **修法（三保险走镜像）**：`os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"`（强制覆盖非 setdefault）+ `huggingface_hub.constants.ENDPOINT` 直接改（破 import 缓存）+ `hf_hub_download(endpoint="https://hf-mirror.com")` 显式参数（旧版库无此参数 TypeError 兜底）
- 验证：hf-mirror HEAD 302（0.58s 可达）；三保险逻辑单测通过（env + constants 双覆盖成功）

## [3.0.0b10] - 2026-08-17
### 修复（章内检测 prompt 哲学：内容一致 → 叙事目的/起承转合）
- **问题**：检测"完全脱离大纲路线发展"，L01 22 条里 21 条是"前后波动"伪问题——4维/R1 的 prompt 用的是"内容一致"哲学（前后平滑/连贯/自然），但文章需要起伏转折，那才叫文章
- **只改 prompt 措辞，判定逻辑/结构/流程全不动**（4维 仍 4 布尔维度、R1 仍 5 维 PASS/HARD/SOFT、SOFT 仍进修复面板、回退链不变）
- **4维（novel_4dim_check.py PROMPT_TPL）**：
  - **上下文共享 + 判定提示词分隔（原始设计还原）**：删掉"每个维度必须独立判断、独立理由，不得影响或引用其他维度"（该句把共享上下文分割成各维各看各的）——改为"下方所有输入为共享上下文（各维度各自摘取的合并），判断任一维度时可参考任意部分（含其他维度摘取的内容）；判定提示词相互独立，但上下文全程共享"。摘取五块（前段尾300/后段头300/后段尾200/规划情绪/角色名字+代称全文上下文）**一字不动**，仅输入标签标注"共享上下文·XX维度摘取"明确各块来源
  - 时间衔接：`无跳变/无矛盾/无倒序` → "时间推进是否有叙事目的（闪回/蒙太奇/跳跃=文学手法允许；无叙事目的的时间断裂或前后事实矛盾=不通过）"
  - 话题过渡：`衔接是否自然连贯` → "话题转折是否服务剧情推进（起承转合/视角切换=正常叙事允许；与剧情无关的游离、丢失前文关键线索=不通过）"
  - 角色承接：删 `情绪不一致=不合理`，保留"自然退场/场景切换/情绪转变=合理；无交代的异常消失=不合理"
- **R1（novel_reasoning_check.py DIMENSIONS）**：
  - 因果合理性：`转折是否牵强` → "转折是否有叙事逻辑（大转折允许，但需有因果呼应）"
  - 情绪弧自然度：`是否突兀` → "情绪转变是否有铺垫与后文呼应（情绪大起大落是人物弧光允许；无铺垫无呼应的情绪跳跃=问题）"
- 人物行为一致性/对话匹配度/论证可靠性/情绪对照规划——本就合理，未动
- 预期：L01 22 条伪问题 → 1-3 条真问题（如 S02 对话匹配度 HARD）

## [3.0.0b9] - 2026-08-16
### 修复（手动/自动模式行为拆分）
- **b8 缺陷**：手动模式也被套进"自动重检循环"（有问题自动继续 apply + 轮次上限）——违背定义
- **手动模式（无上限，全凭人）**：允许中途点"全部跳过"；点"开始修复" → 修复 → 自动重检一次 → **有问题 → 界面不关，`renderRepairPreview` 刷新为本次问题与勾选**（再勾选/跳过/继续点）；无问题 → 关闭界面走下一章。**无重试上限**
- **自动模式（有上限）**：修复 → 重检 → 有问题继续自动全量修复 → 重检 → ... 直到通过或超 `repair_rounds`（默认 3）次
- **实现**：模块级 `_repairMode`（manual/auto）——自动分支（auto_repair 配置）设 auto，applyRepair/applyFullRepair 设 manual；`triggerRecheck` 按模式分支：manual → 刷新面板，auto → 循环 apply + 轮次判定
- 渲染抽取：showRepairPanel 的 preview 渲染抽成 `renderRepairPreview(chapter)`（首次弹出 + 手动重检刷新共用）

## [3.0.0b8] - 2026-08-16
### 修复（修复面板：删"关闭"文案 + 修复完自动重检循环）
- **删"关闭"按钮文案**：原成功消息"可点「关闭」后查看，或重新完结章节重检"——用户没有"关闭"按钮（点击外部不关闭），文案自相矛盾；改为"🔄 修复完成：重写 X 段。自动重检中 (N/M)..."
- **修复完自动重检**：`pollRepairStatus` 拿到 done → 触发 `triggerRecheck`：拉 preview → 有问题 & 未超轮次（默认 3 次，配置 `novel_checks.repair_rounds`）→ 继续 apply → 继续 poll；preview 无问题 → 关闭 modal + 提示"全通过"；超轮次仍有 → 保留 modal 让用户人工处理/全部跳过
- **统一手动/自动模式**：手动模式也走自动重检循环（修复完未自动重检的问题）；配置 auto_repair 仅决定"是否弹面板"（自动模式不弹），不影响自动重检循环
- **防误触发**：triggerRecheck 检查 modal 未关闭 且 `_repairPanelChapter === chapter`——中途点"全部跳过"后不会触发"通过"误判
- 轮次计数器模块级 `_autoRecheckRound`，新弹窗重置，超轮次/通过/失败时重置

## [3.0.0b7] - 2026-08-16
### 重构（R1 修复极简定案：删 quote/difflib，detail 描述即引导）
- **拍板**："R1 本来就有摘要（detail），多余机制全删，R1 信息当引导给大模型告诉它只修这里"
- **删除**：R1 prompt 的 `quote` 字段（输出要求/示例/纠错重试）、issue `source_text`、finalize 聚合打印 `|引:` 标记、web_ui source_text 提取与 quotes_map、`_fuzzy_locate`（difflib 模糊定位）、`_build_window_prompt`（窗口输入）——difflib 全项目零残留
- **R1 修复 = 引导式局部改写**：problem 含"推理审核" → `_build_guided_prompt`（完整正文 + R1 detail 问题描述，writer 自行定位问题句、只改那里、其余逐字保留）→ `_validate_guided`（三行保留 + 输出与原文确有差异）
- 校验只防"未改写空转"与"三行丢失"——不再用 diff/相似度（明确不需要）；writer 是配置的大模型，detail 描述足以定位，重检兜底全文润色风险
- 三检 pledge 的 source_text 路径（_full_repair → src_map，b3 前已有）不受影响，保留

## [3.0.0b6] - 2026-08-16
### 重构（R1 窗口修复 v3：窗口输入，杜绝 LLM 全文润色）
- **b5 缺陷**：给 writer 完整正文 + "只改问题句"指令 → 大模型易顺手全文润色，靠"变化块占比"校验拦截是"先放任再拦截"，且"相似度"字样引起向量模型误解（实际是 Python 标准库 difflib 字符比对，非 embedding）
- **窗口输入**：`_fuzzy_locate`（精确 find 优先 → difflib 滑动窗口模糊匹配，容忍 R1 摘录出入；句子边界对齐防拼接残句）→ **LLM 只见问题句 ±40 字窗口**（`_build_window_prompt`，输出仅窗口片段）→ 拼回原文件——窗口外天然不可能被改动，无需事后校验
- **多问题句**：逐个 quote 循环（定位→窗口改写→拼接，累积），未定位/未改写进 notes；全部失败返回失败
- **删除**：`_build_guided_prompt`（完整正文引导）、`_validate_local`（变化块占比）——均不再需要
- 说明：difflib = Python 标准库最长公共子序列比对，零模型零依赖，与向量模型/bge 完全无关（bge 已全项目清零）

## [3.0.0b5] - 2026-08-16
### 重构（R1 句子级修复：quote 从"精确钥匙"降级为"引导线索"）
- **根因**：R1（1.5B）的 quote 摘录不可靠——prompt 正文预览中段省略（每段只给前 15 行 + 后 8 行），问题句在省略区时 quote 是编的；1.5B 也无法保证"逐字复制"。b3/b4 的 `_locate_windows` 精确检索 + 多级匹配 + missed 报告本质上在跟不可靠输入搏斗
- **方案 C（拍板）**：R1 机制不变（sub 定位 + 问题描述 + quote 尽力摘录），quote 降级为**引导线索**——`rewrite_segment` 收到 quotes 直接发 `_build_guided_prompt`（完整正文 + 问题说明 + quote 线索"可能与正文有细微出入，以正文实际内容为准"），**由 writer 大模型在完整正文中自行定位问题句并局部改写**
- **删除多余机制**：`_locate_windows`（精确检索）、`_build_window_prompt`（窗口契约）、`_validate_window`（窗口外子序列校验）全删；quote 不再做 find 硬钥匙
- **校验换锚**：`_validate_local` = 三行保留 + **变化块占比 ≤25%**（difflib opcodes replace/delete 块长度/原文长度）——不依赖 quote 精确性，整段重写（97%）必拒、局部改（9%）通过；多问题句适度多改（15%）通过
- R1 prompt quote 措辞：去掉"必须逐字复制"（1.5B 做不到的约束会硬编）→ "尽力摘录即可，仅作修复引导"

## [3.0.0b4] - 2026-08-16
### 修复（R1 窗口修复的过度修复回潮 + 定位健壮性）
- **quote 定位失败不再静默整段重构**：b3 的 `if windows:` 之外，R1 问题句全部定位失败会掉进整段重构（过度修复回潮）→ 改为明确失败返回（列出未定位问题句，提示人工核对或跳过），绝不整段重写
- **多级匹配**：`_locate_windows` 三级定位（原样 → 剥引号 `“”"'` → 剥标点 `…。，！？；：、`）——R1（1.5B）摘录 quote 与正文的引号/标点出入可容错命中；返回 (windows, missed) 双值
- **部分定位失败**：修成功的问题句窗口照修，missed 附加 note（重检时 R1 仍报该句可再修）；missed 同时进窗口 prompt 的[未定位问题句]块，让 LLM 若能找到一并修正
- run 收尾：ok=True 且带 note 时附加到 result（此前 note 被吞）

## [3.0.0b3] - 2026-08-16
### 新增（R1 句子级窗口修复 + 逻辑检查子结构定位 + 修复顺序）
- **R1 推理审核句子级定位**：prompt 输出加 `quote` 字段（问题句原文逐字摘录，整章性填 null；输出要求+示例+纠错重试 prompt 同步）→ issue 带 `source_text` → finalize_chapter 聚合打印附 `|引:xxx` 标记走 stdout 链路 → `_parse_hint_issues` 提取回 `source_text`（问题描述剥离干净）
- **修复引擎窗口修复模式**（`repair_type="reason"` 场景）：`rewrite_segment` 加 `quotes` 参数，正文中定位问题句 ±40 字窗口 → 窗口修复契约 prompt（只改问题句、窗口外逐字保留）→ `_validate_window` 强校验（三行保留 + 窗口外正文子序列顺序逐字保留 + 相似度 ≥60%）；quote 定位失败自动回退整段重构保底；多问题句同段一次修复（多窗口并列）
- **逻辑检查子结构定位**：`generate_report` 结构化从 detail 提取 `S\d+\.txt`（`_check_*` 内部本已带 fname 前缀）→ issue 落子结构级可勾选整段重构；无定位（如 timeline 全局倒序）落章级仅查看
- **修复顺序环节优先级**：`files_hit` 从 `sorted()` 字典序改为环节排序——4维 问题=0 最前、格式/逻辑=1、推理审核=2 最后（聚合打印把全部 HARD 放 SOFT 前导致 R1 HARD 反而在前，必须显式按环节排）——弹窗勾选天然"4维 先修、R1 后修"，符合分阶段修复
- apply 聚合 `quotes_map`（{file: [问题句...]}）传入修复引擎；三检 pledge 的 source_text 路径（_full_repair → src_map）互不干扰

## [3.0.0b2] - 2026-08-16
### 修复（4维 SOFT 问题丢失根因链）
- **bridge 过滤漏 `[SOFT]`**：`novel_bridge` 章检/三检 issues 提取只收 `[HARD]`/`[FAIL]` 行 → 所有 SOFT 问题（4维 SOFT/R1 SOFT/格式 SOFT/逻辑 SOFT）从未写入 `hint.issues`，弹窗靠 preview 从 `output` 补 4维，output 截断（stdout[-3000:]）即丢（L01 案例）→ 过滤加 `[SOFT]`
- **4维 issue 缺子结构定位**：`check_4dim` 的 issue `file` 是 `"S01 → S02"`（无 .txt）→ 聚合打印后 `_parse_hint_issues` 匹配不到 `S\d+\.txt` 落章级不可修复 → 改为 `file=前段 S0X.txt`（与 output 补 4维 路径一致，position 保留"后段开头"定位语义）
- **problem 剥离正则**：`\[\w+\]` 匹配不了 `[S01.txt]`（`.` 不在 `\w`）→ 问题描述残留 `.txt]` 前缀 → 改 `\[[^\]]*\]` 兼容
- **HARD 拦截语义收紧（issues 含 SOFT 后的连带）**：novel_writer 章检 `_has_hard`、重检通过判定、三轮回 pending 判定全部改为"存在 `[HARD]`/`[FAIL]` 行"——SOFT 残留不拦截、不无限循环；state_manager `repair_pending` 只由 HARD/FAIL 行或三检 full_items 触发（全 SOFT 章不弹窗）；repair/apply 收尾 `_repaired` 置位改 HARD 判定（重检只剩 SOFT 不死等）
- 效果：有 HARD 的章，弹窗修复面板现在完整显示 4维 SOFT 问题（可勾选整段重构），不再依赖 output 截断后残留

## [3.0.0b1] - 2026-08-16
### 修复
- **逻辑检查正确归位**：`logic_check` 从"无条件跑"纳入 4维 回退链——4维 3B 判定成功（`_4dim_ok`）时跳过；3B 不可用/异常时与规则连通性（`check_continuity`）共同作为规则回退（4维 的规则版替代），异常仍报 HARD 阻断
- **删除跨章承诺检查**（历史遗留，全文三检承诺已完整替代）：`novel_continuity.cross_chapter` 函数、`finalize_chapter` 调用块与 `chapters_dir` 死变量、CLI `cross-chapter` 分支、`cross_chapter_check` 写入全部移除；`_extract_keywords` 保留（`fidelity_check` L1 词面复用）；质检 UI 4维 悬浮提示同步为"回退规则连通性+逻辑检查"
- 命名不做改动（novel_style_check.py / style_check / style_check_notes 保持现状，避免链式引用风险）

## [3.0.0b0] - 2026-08-16
### 架构重构（major：门禁体系废除 + 模型架构 3→2 模型）
- **门禁体系废除**：`require_gate` 为死代码（无任何调用方）、gate_state.json 只写不读；三检放行改为"修复弹窗处理状态"驱动（`_full_repair` 全部处理完即放行），`_eval_full_gate` 删除
- **章内/全文统一存储**：三检修复项从 novel_state `_full_repair` 迁入 session `_repair_hints[chapter].full_items`（与章检 issues 同层）——repair_pending 一套判定、apply/skip 一套逻辑；修复 finalize-novel 写 novel_state vs get_progress 读 session 的存储断链（novel_writer 同步桥接）
- **模型架构定型**：向量模型 bge 全项目移除（章检语义检查 + 实体提取器归并相似度档），系统模型 = R1（推理审核）+ 3B（4维判定/实体提取/忠实度复核/结尾收束）
- **章检重构**：连通性/语义规则版 → 3B 一次判 4 维（时间衔接/情绪匹配/话题过渡/角色承接，逐维独立判定+独立 reason）+ 角色注册表（name+aliases）全文检索上下文注入；4维不可用回退规则连通性；语义检查（bge）整链路删除

### 新增
- **全文三检全 LLM 化**：①大纲忠实度（L1 词面全量筛 → 覆盖率<0.6 可疑段 3B 复核"正文是否支持 summary"，支持提升 PASS/不支持给理由）②全文承诺（3B 按正文提取 flag（sub+source_text 规则定位）→ writer 配置驱动推理判已兑现/未兑现/悬停+依据 → 关键词回退；替代旧 cross_chapter 章尾/章头关键词检查）③结尾收束（末章末段 → 3B 判封闭/开放/悬停三型 + 特征词回退）
- **三检修复弹窗复用**：fidelity/pledge/ending 类型化重构（`novel_repair_engine` 加 repair_type/source_text → [修复目标] 指令变体）；pledge 减法 = LLM 重构移除悬置承诺+平滑衔接
- **三检当场重检**：勾选修复 = 承担成本——重构成功后当场重跑对应检查（fidelity 重跑 fidelity_check / pledge 重跑 extract+check / ending 重跑 verify_ending），通过才移除修复项，全清空才标记通过（无"下次 finalize-novel"）
- **修复弹窗显式裁决**：全部跳过（`_repaired=True` 不再弹）替代"关闭"；勾选=要修、未勾选=接受问题立即标记通过
- **包环境探测安装**：`novel/model_env_check.py`——setup.bat 启动阶段探测 transformers/torch（可选 accelerate），缺失自动 pip install（阿里云镜像），防"UI 下载了模型但缺运行包跑不了"
- **质检 UI 重构**：章内检测/全文检测分组 + 点位标注 + 慢速提示；全文三检单独打钩（full_fidelity/full_pledge/full_ending）；格式校验独立开关（format，修复挂错 semantic 字段的空开关）

### 修复
- 修复引擎选错项目（glob 取第一个项目）→ 从 session outline `_novel.state_path` 定位当前项目
- 修复引擎 chapter_dir 少章级子目录（chapters/ 根 vs chapters/<章>/）→ "文件不存在"全失败
- 修复失败无条件置 `_repaired=True`（面板永不重弹）→ 有失败保持可重试
- 章检输出格式与修复解析不匹配（语义检查缩进行/段间标记无 .txt）→ 兼容解析 + 问题清单（只列有问题子结构 + 带问题）+ T0/T1 划分（T0 自动修不进勾选）
- `repair/preview` + `repair/status` 路由挂错 POST 表（前端 GET → 404 → 修复面板被隐藏）
- 修复面板按钮不可见 → 改模态框（居中 + 滚动区外物理固定按钮 + 点遮罩不关闭）
- 章级重规划 title 污染（`S05《xxx》`）→ s_key 明确沿用 + prompt 去诱导（目标子结构编号仅供定位、禁止写入 title）
- 重规划在途三按钮（开始生成/重新规划/保存范例并生成）未禁用 → `_markActionButtonsBusy` 统一禁用（文案不变）
- 4维判定 prompt 中略标记误导 3B（把省略当自然承接）→ 移除，改独立【后段结尾节选】字段
- setup.bat 闪退（UTF-8 中文注释被 GBK cmd 乱码解析 + LF 行尾）→ 全 ASCII + CRLF 重写
- 修复模型文案写死"35b" → "写作模型"（配置驱动）

### 移除
- `novel_semantic_check.py`（bge 语义检查，死代码）整文件删除
- 实体提取器 bge 归并（`_load_bge`/`_cosine`/`MERGE_SIM_THRESHOLD`/第三档相似度）——归并降级精确名+子串
- `_eval_full_gate`、gate_state.json 的 fidelity/ending_verify 写入、`NOVEL_SKIP_SEMANTIC`、质检 UI "语义bge" 状态/安装项

## [2.4.0b0] - 2026-08-16
### 新增（重规划全链路 UI 反馈与竞态防护，2.3.29b0 之后）
- **重规划在途反馈**：子结构/章级/整篇重规划进行中——行内"重规划中..."、章卡片/底部三按钮（开始生成/重新规划/保存范例并生成）视觉禁用（文案不变）、防重复发起
- **后端竞态防护三层**：重规划登记 `_replan_inflight`（try/finally 清理）；确认/生成接口检查活 in-flight → 409 拒绝（1800s 僵尸自动清理）；前端 JS 守卫双保险
- **刷新后 in-flight 恢复**：`/api/session/load` 返回活 in-flight；新增轻量 `GET /api/novel/replan_status`；前端恢复禁用态 + 2s 轮询，重规划完成自动恢复按钮
- **确认面板子结构排序**：复用通用线 `sub_orders` 链路（s1/s2 下拉）→ 确认时重排 outline + 同步 novel_state `sub_structures` dict 顺序（s_key 不变、顺序变；save_state 白名单加 `novel-confirm`）
- **规划/写作上下文升级**：三层分区（目的★★★/背景★★/参考★）；原始需求全量注入（规划/写作/章级重规划，移除 500 字截断）；人物档案完整性格；后续章大纲预告；前文章节概述；实体/行为/时间线参考层
- **三提取器统一**（实体/行为/时间线）：write-sub 逐段提取，LLM（Qwen2.5-3B）优先 + 正则兜底（模型缺失不丢数据）；时间线从空壳接活（day 累计解析：第一天/次日/三天后）
- **写作阶段三层分级**：6 块（实体/人格/行为/收尾/钩子/关键人物）从 main 块移入函数（Web 场景首次拿到）；补时间线/原始需求注入
### 修复
- **章级重规划语义修正**：章卡片重规划改为重做单章 title+overview（`replan_novel_chapter`），不再误调子结构规划（两级分离）
- **重规划 title 污染**：LLM 输出 `S05《xxx》` 编号前缀——prompt 明确 s_key 沿用/禁止编号入 title（与初规划对齐引导，不搞后端清洗）
- **修复引擎 preview/status 路由挂错表**：挂在 POST 表但前端 GET 调 → 404 → 面板被隐藏（按钮不出现）；已移入 GET 表
- **修复引擎选错项目**：`_repair_engine_for` glob 取第一个项目（最老），改为从 session `_novel.state_path` 定位当前项目（否则"文件不存在"全失败）
- **修复面板文案**：写死的"35b 整段重构"改为"写作模型"（实际模型 = 配置 writer_model）
- 版本 2.3.29b0 → 2.4.0b0

## [2.3.29b0] - 2026-08-15
### 修复（HARD 拦截判定用 ok 字段导致永不拦截，2.3.28b0 之后）
- **现象**：L03 有 HARD（推理审核-对话匹配度），但没弹修复面板直接规划 L04
- **根因**：novel_writer 章检拦截用 `fc.get("ok")` 判定——但 `finalize_novel_chapter` 的 ok 是**子进程退出码==0**（workflow_engine 有 HARD 只写 fixes 正常 return，退出码 0）→ **ok 恒 True，不可信**！L02/L03 的 hint 都是 ok=True 但 issues 有 HARD。state_manager 的 repair_pending 已改用 issues 判定，但 novel_writer 主循环拦截漏了（同一 bug 修漏一处）→ 拦截永不触发 → HARD 不拦截直接推进下一章
- **修复**：novel_writer 全部改用 `fc.get("issues")` 非空判定 HARD（拦截/重检通过/3轮判定），ok 字段弃用
- **存量**：session L03 done → pending（待修复）、L04 in_progress → pending（等 L03 完成后规划）；repair_pending 验证返回 L03 ✅
- 版本 2.3.28b0 → 2.3.29b0
### 修复（段级续写重复重写，2.3.27b0 之后）
- **现象**：L03/S01《逻辑重构与试探》已写好（21:02 落盘），续写又从 S01 重写一遍
- **根因**：段级续写跳过逻辑只认 `sub.status == "done"`（novel_writer 493 行）——与章级 2.3.26b0 修的同一个 bug：session 状态滞后（写段线程更新 session 前被中断/重启，n3_1 停留在 in_progress）→ 文件在但 session 说没写完 → 重写。2.3.26b0 只修了章级，漏了段级
- **修复**：段级改为文件真相源——**无论 session 状态**，`_read_sub_content` 有内容即视为已写：跳过 + 同步 session 为 done；文件缺失/空才重写
- **存量**：session L03/S01 in_progress → done（wc=2008 回填）；误写线程已 /api/stop（S01 原稿 21:02 完好）
- 验证：L03/S01 文件在 → 跳过 ✅；S02 无文件 → 重写 ✅
- 版本 2.3.27b0 → 2.3.28b0
### 修复（CLI 写入路径改真原子写，2.3.26b0 之后）
- **背景**：CLI 路径 `validate_and_write_body`（novel_atomic_writer）是两段式写入——`open(fp,"w")` 写正文 + `open(fp,"a")` 追加末行标记。中断在两步之间 → 文件缺末行标记 → 章检报"末行缺失" HARD
- **修复**：改为真原子写——组装完整内容（标题/空行/正文/别名/末行一次成型）→ 写 tmp → fsync → `os.replace`，与 `_write_sub_inline` 完全一致；中断只留 .tmp 残留，目标文件要么旧完整版要么新完整版
- **CLI 保留**（存在直接调用场景），逻辑与 web 写段统一
- 验证：CLI 落盘结构 标题/正文/别名/末行 一次成型 ✅；中断残留 tmp 不影响目标文件 ✅
- 版本 2.3.26b0 → 2.3.27b0
### 修复（续写重复重写已写章，2.3.25b0 之后）
- **现象**：重启后点开始生成，从 L02 重新开始写（本地 L02 四段早已落盘）
- **根因**：续写恢复逻辑只在 `section.status == "done"` 时才检查文件齐全（novel_writer 378 行）——session 状态不是 done 的章（in_progress/pending，旧 server 写段完成只更新 novel_state 不同步 session 的残留），文件再多也不看直接重写。2.3.14b0 修过"写段只更新 novel_state 不同步 session"，当时只同步了存量数据，代码层 TODO 未做 → 复发
- **修复**：
  1. **文件为真相源**：续写恢复改为"文件齐全 → 无论 session 状态如何都跳过 + 同步 session 为 done"（不再依赖 session 状态判定是否重写）
  2. 文件不齐时仍按原逻辑降级（done 空章回 pending / 文件不全降级段级处理）
- **存量修复**：session 20260815_170214 的 L02 in_progress → done（子结构 done + 字数按 novel_state 回填）；L02 磁盘原稿备份 L02.bak_2039（防覆盖）；已调用 /api/stop 停止误写线程（S01 原稿 19:03 完好）
- 验证：续写判断模拟 L01/L02 文件齐全 → 跳过 ✅，L03（confirmed 未写）→ 正常进入 ✅
- 版本 2.3.25b0 → 2.3.26b0
### 修复（实体清洗/注册死循环，2.3.24b0 之后）
- **现象**：每轮 extract 打印"清洗历史碎片: 6 → 5"+"角色注册: 新增 1"，无限循环
- **根因链**：规划 LLM 在 L02/S02 规划文本写了 `【新角色：无】` 占位标记 → "新角色自动登记"正则（`{1,8}` 抓任意字符）把 `'无'` 注册进 characters → extract 每轮：清洗端删 `'无'`（len<2 规则）+ 注册端从 characters 加回 → 死循环
- **修复**：
  1. 自动登记 `_detect_new_chars_in_plan`：占位符精确枚举挡截（`无/未知/待定/None/暂无/未定/未命名`，集合整串相等，不误伤"无风/无面人"）
  2. 清洗 `_sanitize_legacy`：删掉 `len<2` 伪语义规则（单字可能是合法名"渊"，长度不是语义判断）；只保留标点残留（FRAGMENT_RE）确定性清洗；**character 类型永不删**（权威源）
  3. 注册 `_ensure_characters_registered`：占位符精确挡截（与规划端同源 PLACEHOLDER_NAMES 双端一致）
  4. 存量数据修复：characters 4→3（删 '无'），entity_tracker 7→6，关系无损；备份 novel_state.json.bak_20260815_2036
- 验证：清洗不误删单字角色/保护 character/清标点残留 ✅；注册挡 '无' 放 '无风' ✅；2 轮模拟稳定 3 实体无循环 ✅
### 修复（repair_pending 取最近章，2.3.23b0 之后）
- **根因**：`get_progress` 的 repair_pending 用 `break` 只取第一个未修复章——L01 的旧 HARD 永远占位，L02 的新 HARD 永远轮不到 → 前端弹的永远是 L01，L02 写完不弹
- **修复**：遍历全部章不 break，最后命中的 = 最近 finalize 的待修复章（写一章弹一章语义）
- 验证：真实 session 170214 的 repair_pending 从 L01 → L02（2 条 HARD 正确返回）✅

## [2.3.23b0] - 2026-08-15
### 修复（六检 HARD 章级触发 + 主循环拦截，2.3.22b0 之后）
- **根因**：修复面板弹窗挂在 session 级 done（全书完成才弹），单章有 HARD 不弹；写段循环先标 done 后跑 finalize，HARD 不阻断主流程 → 直接推进下一章（《计算无法抵达处》L01 有 HARD 但直接规划 L02）
- **弹窗改章级触发**：`get_progress` 新增 `repair_pending`（hint.issues 非空且未标记 `_repaired` 即返回）→ 前端轮询发现即弹修复面板，不依赖 phase/status_text；`_repairPanelChapter` 防重（同章不重复弹）
- **判定修正**：以 `hint.issues` 非空为准（ok 字段被旧数据污染不可信）
- **主循环拦截**：novel_writer finalize 有 HARD → 先检后标 done（finalize 是章级裁判）；章级 done 撤回，轮询 hint._repaired 等修复引擎（≤3 轮），修复后全六检重检，通过才标 done；3 轮仍 HARD → 章回 pending 交人工（正文保留）
- **apply 完成标记** `_repaired=True` → repair_pending 消失，不重复弹
- 验证：真实 session 170214 的 L01 HARD → repair_pending 正确返回；标记 _repaired 后消失 ✅
### 修复（实体抽取格式漂移，2.3.21b0 之后）
- **`_extract_llm` 增强**（照搬 R1 审核已验证的套路）：
  1. prompt 加 **few-shot 完整示例**（治本：模型一次输出正确格式）
  2. 解析失败 → **打回纠正重试最多 3 次**（原始输出+错误说明回喂）
  3. WARN 时**打印原始输出前 200 字**（可诊断）
- **`_extract_json` 解析容错增强**：
  - `NaN/Infinity/-Infinity` → `null`（模型偶发输出非标准数字）
  - **截断修复**：从末尾找最大合法 JSON 前缀 + 补全闭合括号（模型被 max_new_tokens 截断时不再丢本轮抽取）
- 实测：真实长正文（1688 字）——第 1 次格式漂移 → **第 2 次纠正后解析成功**（entities=7 relations=5），重试兜底生效；8 个解析用例全过
- 说明：角色注册（"新增 4 个 character"）是纯代码链路，与 LLM 抽取失败无关——两条独立链路

## [2.3.21b0] - 2026-08-15
### 新增（修复引擎 P4：自动模式 + 重构后同步）
- **自动修复模式**：
  - 配置面板新增 `auto_repair` 开关 + `repair_rounds` 轮次（小说质检区，默认关/3 轮）
  - 前端轮询章 done 后：auto_repair=on → 不弹面板，全选自动重构（后台线程 + 轮询）
  - 手动/自动模式：配置面板=全局默认，修复面板=单次覆盖
- **重构后同步**（关键：重构改正文 → 实体/时间线必须跟随）：
  - `extract()` 加 `force_status=True`：状态覆盖式刷新（原仅增量填缺，重构后 alive→injured 更新）
  - `sync_after_rewrite`：重构落盘后重跑 extract（force），实体状态以新正文为准
  - 挂进 `run`：每段重构成功后自动 sync
  - 双份备份（正文+state）保证回滚一致
- 实测：临时副本，艾琳 alive → unconscious 覆盖成功（force 生效）

## [2.3.20b0] - 2026-08-15
### 新增（修复引擎 P3：UI 修复面板）
- **章检结果落盘**：novel_writer 章检后 `save_repair_hint(chapter_id, result)` 存 session（含 HARD/FAIL 行 + 完整 stdout 供前端解析 T0/T1）
- **StateManager**：`save_repair_hint` / `get_repair_hints`
- **后端 4 API**：
  - `/api/novel/repair/preview` — 章检结果 → T0（自动修）/ T1（按文件聚合）清单
  - `/api/novel/repair/apply` — {session, chapter, checked_subs, mode} → 后台线程 T0+T1 重构
  - `/api/novel/repair/rollback` — 回滚指定轮（正文+state）
  - `/api/novel/repair/status` — 修复进度轮询
- **前端修复面板**（`novel-repair-panel`）：章 done 后自动弹出 → 列问题 + 子结构勾选（勾掉=跳过）→ 开始修复 → 4s 轮询状态 → 显示重写/失败段数
- 修复模型配置驱动（config writer_model 全参数继承，零新增配置）

## [2.3.19b0] - 2026-08-15
### 新增（修复引擎 P2：T1 整段重构）
- **`rewrite_segment`**：35b 整段重构单子结构（novel_repair_engine.py）：
  - 契约 prompt：保留首行标题/末行编号/别名行 + 字数 ±15% + 衔接上下文（上段尾/下段头 100 字，已净化仅正文）+ 子结构规划 + 问题清单
  - `_validate_rewrite`：输出校验 4 项（标题/末行/别名/字数），不合格拒绝落盘保留原稿
  - `_create_repair_client`：**配置驱动**——读 config `writer_model`，timeout/max_tokens 全继承，零新增配置
- **`run` 引擎**：T0 自动修 + T1 按段聚合重构（checked_subs 勾选过滤）
- `_prev_tail`/`_next_head`：衔接上下文净化（去掉标题/别名/末行，只留正文）
- 实测：35b 真实重构 S01（10 分钟），契约三行全保留、字数 1264（原 1209 +4.5%）、内容质量提升

## [2.3.18b0] - 2026-08-15
### 新增（修复引擎 P1：T0 自动修复）
- **新增 `novel_repair_engine.py`**（六检问题修复引擎，v0.3 设计落地第一步）：
  - `apply_t0`：纯代码修复格式问题——末行编号缺失补全（旧格式 S01 兼容跳过）、禁用模式行删除（元文本/指令残留）
  - `backup_segment` / `rollback_round`：**双份备份**（正文 + novel_state 快照），按轮回滚（正文与实体状态永远一致）
  - `run` 引擎骨架（P1 只做 T0，T1 整段重构待 P2）
- 修复模型配置驱动（读 config writer_model，参数全继承），不写死
- 设计文档：`docs/repair_engine_design.md`（v0.3 定稿：T0/T1/T2 分级、整段重构契约、同步步骤、双份回滚、3 轮上限）

## [2.3.17b0] - 2026-08-15
### 修复（2.3.16b0 之后）
- **推理审核格式漂移根因修复**（novel_reasoning_check.py）：
  - 根因：prompt 只有一行格式说明、无示例 → R1-1.5B 输出 result 字段填中文/填反（"通过"/"合理"），后端 3 个兜底穷举填错姿势（376/378/382 行）把正面判断猜成 SOFT → 每次审核产生 5 个"通过型 SOFT"噪音
  - 治本：prompt 增加 **few-shot 完整输出示例**（5 维 JSON 数组，含 PASS/SOFT/HARD 三态）→ 模型一次输出正确格式（实测 5/5 漂移 → 一次通过）
  - 治标改治本：解析失败不再直接标记"审核失败"（审核结果不可见）——**打回纠正重试最多 3 次**（把原始输出+错误说明喂回要求重新输出），3 次仍失败才标记「推理审核失败」SOFT（人工可见）
  - 解析逻辑抽为 `_parse_reasoning_results()` 独立函数
- 实测：L02 一次通过，输出 1 HARD（对话匹配度，真问题）+ 1 SOFT（人物行为一致性，模型有意判定），无格式噪音

## [2.3.16b0] - 2026-08-15
### 数据修复（《模拟失败报告》实体表清洗）
- **历史正则碎片清除**：entity_tracker 从 425 实体（85.6% 为 `'但我没有'`/`'噪音逐渐平息'` 等句法碎片）清洗至 121 个纯名词性实体，上下文注入体积从 ~27KB 降至 ~7.4KB（-72%），续写 prompt 不再被垃圾实体污染
- 清洗策略（三级）：
  1. 白名单保底：character + 关系两端永不删
  2. Qwen2.5-3B 语义判定（421 个分批，保留 218）
  3. 规则兜底：动词结尾/方位结尾/助词/代词/判断句黑名单（218 → 149 → 121）
- 关系完整性：19 条关系全部保留、引用零悬空（id 重映射）
- 备份：`novel_state.json.bak_20260815_1503`

## [2.3.15b0] - 2026-08-15
### 修复（2.3.14b0 之后）
- **章级确认面板不再主动弹出**：原 loadSession 加载 writing 会话时无条件 `startProgressPolling`，而 `get_progress` 的 `awaiting_confirm` 是静态推断（只要有 planning 章就返回，不管生成线程是否在跑）→ 加载即弹确认面板，语义混乱。改：
  1. 后端 `/api/progress` 加 `running` 字段（该 session 是否有活跃生成线程）；`running=false` 时 `awaiting_confirm` 置 None（静态残留不展示）
  2. 前端 loadSession writing 分支：先查一次 progress，`running=true`（断线重连）才启动轮询；否则不轮询不弹面板
  3. 确认面板回归其本质：**「开始生成」流程的子确认步骤**——用户点「开始生成」→ 启动线程 → 线程跑进 planning 章 → 轮询弹出确认面板 → 确认后写本章；退出再进 → 不弹，点「开始生成」自然续写

## [2.3.14b0] - 2026-08-15
### 修复（2.3.13b0 之后）
- **续写链路状态混乱根治**（session 与磁盘/novel_state 三源不同步 + 章检末行格式漏网）：
  1. `novel_style_check.py` 末行校验兼容旧格式——原只认 `{chapter}S\d+`（"L02S01"），2.3.10b0 只改了写入端和读取端、漏改校验端 → 旧文件（末行 "S01"）章检永远报 4 个 HARD → 章级永不标 done → 续写永远重写已完成章节。改 `^(?:{chapter}S\d+|S\d+)$` 与读取端一致
  2. 存量数据修复：L01/L02 共 8 个 S*.txt 末行批量补全为 `L{chapter}S{key}`（幂等，已有 L 前缀跳过）
  3. session 003918 同步：L02 章+4 子结构 in_progress/pending → done，字数按磁盘实际回填（1204/1791/2619/996），续写直接进 L03
- 根因链条：写段完成只更新 novel_state 不同步 session → 续写以 session 为唯一判断源 → 无脑重写磁盘已完成的章节；旧进程残留重写（12:47）把 session 刷回 in_progress 加重矛盾

## [2.3.13b0] - 2026-08-15
### 修复（2.3.12b0 之后）
- **实体抽取窗口回放**：`EXTRACT_MAX_TOKENS` 1024 → 2048。实测 7016 字长文本（4 段拼接）生成仅 472 token（1024 够用），但 1024 在长篇多角色（20+ 实体、30+ 关系）场景有截断风险；max_new_tokens 是上限非消耗，模型写完 JSON 自动 eos（实测 213/472 token），调大零成本

## [2.3.12b0] - 2026-08-15
### 修复（2.3.11b0 之后）
- **模型就绪检测假阳性（UI 误报）**：`qwen25` 就绪检测原只查 `snapshots` 目录存在（`_has`），目录一创建即显示绿色"就绪"——下载中断/进行中时权重分片缺失仍误报，导致"模型输出无法解析"（实际是权重没下完、模型根本没工作）。新增 `_model_ready`：验证 `model.safetensors.index.json` 引用的**所有权重分片存在且非空**才算就绪；bge/r1 的 HF 缓存回退检测同步规范化

## [2.3.11b0] - 2026-08-15
### 修复（2.3.10b0 之后）
- **实体抽取阻塞写段**：extract 同步跑在写段主线程，Qwen2.5-3B CPU 生成 4096 tokens 需 1-3 分钟/段 → "写一段停一段"。`EXTRACT_MAX_TOKENS` 4096 → 1024（实体+关系 JSON 足够，3B CPU 单次抽取降至 10-30 秒）；修复 pipeline 传参（去掉 pad_token_id 减少 generation_config 混传警告，警告无害不阻塞）

## [2.3.10b0] - 2026-08-15
### 修复（2.3.9b0 之后）
- **子结构末行格式统一**：`_write_sub_inline` 末行原写 `s_key`（"S01"），而 `_read_sub_content` 与 `novel_style_check` 期望 `{chapter}{s_key}`（"L02S01"）——写/读/检三方不一致 → 每章完结必报 4 个 HARD（末行编号），fixes 文件持续生成；且旧文件（"S01"）续写时正文残留末行标记。修复：写入改为 `{chapter_id}{s_key}`，读取兼容新旧两种格式（`L\d+S\d+$|S\d+$`）

## [2.3.9b0] - 2026-08-15
### 修复（2.3.8b0 之后）
- **安装模型 500 修复**：`_handle_novel_install` 对模块级 `_install_state` 赋值缺 `global` 声明 → `UnboundLocalError` → 后端 500 → 前端显示「安装失败」。加 `global _install_state`，安装线程正常启动

## [2.3.8b0] - 2026-08-15
### 修复（2.3.7b0 之后）
- **模型安装改自动下载（去弹窗）**：点击「安装缺失模型」→ 后端后台线程执行 hf-mirror 下载（bge/R1/Qwen2.5-3B 缺失自动装），不再 `alert()` 弹命令手动安装；前端内联显示安装进度 + 3s 轮询，模型就绪状态自动变色（铁律：不弹消息框）

## [2.3.7b0] - 2026-08-15
### 修复（2.3.6b0 之后）
- **实体关系提取改 LLM 驱动**（novel_entity_extractor 重写，替代纯正则版）：
  - 抽取：Qwen2.5-3B-Instruct（transformers 本地，非思考，CPU，`max_new_tokens=4096` 不抠窗口）——语义抽取实体/关系/状态，杜绝正则碎片
  - 归并：bge-small-zh 嵌入相似度 >0.85 合并（防"792-Alpha"/"编号 792-Alpha"重复建档）；缺 bge 降级名字/子串匹配
  - 注册：characters（含别名）强制注册为 character 实体，人物不再缺失
  - 清洗：历史正则碎片（标点残留/超短名）幂等过滤
  - 模型走 transformers 本地（data/models 优先 → HF 缓存回退），不走 LM Studio；失败非阻断
- **前端模型检测/安装**：`_novel_status_data` 加 qwen25 检测；`_handle_novel_install` 加 Qwen2.5-3B 下载命令（hf-mirror 一条命令）；配置面板显示「实体抽取Qwen2.5-3B 就绪/缺失」

## [2.3.6b0] - 2026-08-15
### 修复（2.3.5b0 之后）
- **推理审核窗口扩大 + 失败兜底**：`max_new_tokens` 1024 → 4096（R1-Distill 思考+审核 JSON 不再被截断）；解析失败不再静默通过——返回 SOFT issue「推理审核失败：模型输出无法解析」，六检聚合可见（不阻断但人工必须知晓审核未完成），杜绝"审核失效假装通过"

## [2.3.5b0] - 2026-08-15
### 修复（2.3.4b0 之后）
- **版本单一来源**：pyproject.toml 静态 `version` 改为 `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "structured_writer.__version__"}`——bump 只改 `__init__.py` 一处，构建自动读取，杜绝"这里改了那里没改"。新增 `scripts/check_version.py` 校验（`__init__` vs CHANGELOG 最新条目 vs pyproject 动态配置），发布前必须 exit 0

## [2.3.4b0] - 2026-08-15
### 修复（2.3.3b0 之后）
- **删除输出文章去掉 confirm 弹窗**：deleteOutput 目录文章删除时，在内联二级确认（✕→确认?/取消）之外还套了浏览器 `confirm()` 弹窗 = 双重确认。移除弹窗，只保留内联二级确认（铁律：不弹消息框）

## [2.3.3b0] - 2026-08-15
### 修复（2.3.2b0 之后）
- **setup.bat 全局杀进程**：原实现只杀 8770 端口（`netstat findstr ":8770 "`），命令行直启的其他端口（如 8798）永不清理 → 双 server 并发读写同一 data/（session/projects），生成线程在 LLM 调用前被数据竞争搅死（LM Studio 无模型加载 + session `_status_text` 停在旧值）。改为 PowerShell 按命令行特征杀**所有** `main.py` 实例（任意端口），杜绝残留

## [2.3.2b0] - 2026-08-15
### 修复（2.3.1b0 之后）
- **小说父级目录跨会话统一**：novel_writer 输出目录改为按项目关联复用——首次写章新建 `outputs/<标题>_<时间戳>/` 并写入 `.project` 标记（记录 state_path）；后续跨天/跨会话续写扫描 outputs/ 按 `.project` 匹配同一项目并复用，不再无条件新建带时间戳目录（原实现每次调用都新建 → 跨天续写分裂出多个同名父级，树状列表一个题目下挂不全章节）。异项目（state_path 不同）严格隔离。新增 `_resolve_novel_out_dir(state_path, title)` 模块函数，`os.path.normcase` 归一化比较防 Windows 大小写/分隔符差异

## [2.3.1b0] - 2026-08-15
### 修复（2.3.0b0 之后）
- **子进程编码两侧统一**：`_run_script`（plan-chapter/replan）与 `novel_atomic_writer` register-alias 子进程 env 加 `PYTHONIOENCODING="utf-8"`——Windows 下子进程默认 GBK 输出中文，主进程 `encoding="utf-8"` 读取时 `UnicodeDecodeError`（`_readerthread` 0xa8 字节）导致子结构规划失败；测试环境手动设 env 掩盖了生产问题，现已统一

## [2.3.0b0] - 2026-08-15
### 新增（会话消息持久化 / 字数差异化 / 备份全局化 / UI 实时性，2.2.0b0 之后迭代）
- **会话消息持久化**：session 加 `messages` 字段 + `append_message`（上限 200）；`/api/plan` 规划**前**创建 session 并存入用户要求（规划跑数分钟期间切走/切回要求都在，规划失败会话保留可重试）；`/api/session/load` 返回 messages；前端 loadSession 重建用户消息（config 分支中性提示）——"切走切回什么都没有"根治
- **通用线消息持久化**：`/api/chat` 复用/创建 session 存 user 消息（load-or-init 保留历史累积）；响应新增 `session_id` 字段（原结构保留）；前端 sendMessage 据此更新 currentSessionId；`/api/plan` 存 topic 去重（chat→生成大纲按钮→plan 链路防重复）；会话列表标题 outline 空时取消息首条截断
- **子结构字数差异化**：规划 prompt 加 `word_count` 字段（lo-hi 区间内按内容重要度浮动，同章各段不许相同——重点段取中上/普通段中下/过渡段可短）；注册保存 LLM 值；显示优先 LLM 值（不再统一取档位 max 导致全 1500/2000/4000）
- **项目备份兜底全局化 + 备份含正文**：`load_state` 统一兜底（文件缺失 → 自动从备份恢复 → 失败才抛明确中文错误）——plan_chapter_subs/replan/context_loader 等所有读点不再裸抛 FileNotFoundError；`_backup_state` 整目录同步（state + chapters 正文），`_restore_from_backup` 完整恢复两者
- **前端加载自动恢复**：`/api/session/load` 小说线自动尝试恢复备份（ok/restored/missing）；前端 done/error/writing 三分支按恢复结果判定——恢复成功 → 非只读（「开始生成」续写入口）+ 恢复提示；无备份 → 只读 + 明确提示删会话重开（不再"明明能救却显示只读"）
- **叙事视角强指令**：context_loader 文风约束段加【视角强制】（第一人称=以「我」叙述禁他/她/角色名作主语，第三人称全知/有限各自明确）；NOVEL_SYSTEM_PROMPT 声明"【视角强制】是最高优先级写作规则"——标签变可执行规则
- **轮询实时刷新大纲卡片**：progress 轮询防抖（进度戳变化）→ 拉最新 outline 重渲染大纲卡片（两线共用）——切会话再切回，生成线程的章/子结构进度实时可见（"状态丢失、结果不体现"根治）
- **重规划刷新分场景**：章级重规划（大纲卡片操作）→ 刷新大纲卡片 + 小说线重置确认面板；段级重规划（确认面板操作）→ 只刷确认面板，大纲卡片不动
- **确认面板即时反馈**：确认成功立即收起面板 + 对话栏提示 + `_ncConfirming` 防重复点击（不再等 1.5s 轮询才动）

### 修复
- 切走切回会话"什么都没有"（无消息历史 + 规划期 session 不存在）→ 消息持久化 + 规划前落库
- 子结构字数全相同（显示层取档位 max）→ LLM 定字数全链路
- 备份只接 novel_writer 开头（L02-L05 规划裸抛 FileNotFoundError）→ load_state 统一兜底
- 备份缺正文（只存 state）→ 整目录备份含 chapters
- done 会话永远只读死胡同（不试恢复）→ 加载自动恢复 + 按结果判定可续写
- 叙事视角标签无执行规则（LLM 仍写第三人称）→ 【视角强制】强指令
- 进度轮询空实现（注释"重新加载session"但没做）→ 防抖重渲染
- 确认后无即时反馈/可重复点击 → 收面板 + 防重复
- confirmReplan 小说线一刀切不刷大纲卡片（章级该刷）→ 按操作入口分场景
- 通用线 chat 会话列表标题为空 → 消息首条截断

## [2.2.0b0] - 2026-08-14
### 新增（小说线健壮性与模板一致性，2.1.0b0 之后迭代）
- **小说标题提炼**：章大纲规划时 LLM 同步输出短标题（≤12字）；`build_outline` 按 auto 语义落地——用户填了用用户的，没填用 LLM 提炼，兜底截断原文（不再把完整需求当标题）
- **小说线辅助知识**：前端确认面板每段「+辅助」按钮（与通用线同入口）→ `aux_knowledge` 传入 novel_writer → `_build_sub_aux` 组装（使用指令+文字/表格注入，图片跳过）→ 段 prompt 注入【辅助知识】；文案与通用线统一，RAG 独立为【RAG 参考资料】
- **项目状态自动备份与恢复**：`save_state` 每次成功同步备份到 `data/novel/backups/`（projects 兄弟级，防误删连带）；novel_writer 检测 state 丢失 → `restore_novel_state` 自动从备份恢复（角色/设定/实体/命题框不丢），无备份才 fail-closed
- **meta 三模式语义统一**（模板 source 唯一权威，零字段特判）：user=用户填直接抄入 LLM 不经手（通用线代码级固化）；auto=已填抄入未填 LLM 生成；llm=一律 LLM 生成。新增 `META_TO_WRITING_STYLE` 配置映射表 + `_apply_meta_to_writing_style` 通用函数（叙事视角→narrative_voice 声明式映射）；标题/叙事视角删字段特判
- **写作上下文真正注入**：`load_context` 是 novel-weaver CLI 脚本（全程 print 无 return）→ 新增 `_load_context_captured`（redirect_stdout 捕获）→ 角色/人格/情绪/命题框/叙事视角首次真正进入段写作 prompt
- **叙事视角生效**：场景配置 JSON 加 `writing_style` 字段（prompt 严格沿用 user 给定视角）；用户填的叙事视角按 source 语义覆盖 LLM 返回（第一人称不再被写成第三人称）
- **规划 max_tokens 全走配置**：novel_bridge 4 处 `_llm_json` 拍死值（8192/16384）移除，统一 `llm_client.max_tokens`（用户配置）
- **文件为真相源**：续写恢复以文件存在且非空为准——段级 done 但文件缺失/空 → 置回 pending 重写（不静默丢正文）；章级 `_chapter_files_complete` 校验子结构文件齐全，缺/0字节 → 降级段级处理；落盘后回读校验（防 0 字节假 done）

### 修复
- planner.py topic 硬塞标题（`user_meta["标题"]=topic` 旧兜底）→ 删除，小说线标题走 auto 语义
- 空章闪跳：done 章未规划子结构（空章）→ 回 pending 重新规划，不允许空转标 done；段循环前空子结构拦截；章末尾 `_chapter_any_sub_written` 校验（勾选段至少一个落盘才标 done）
- 写作循环空正文卡死 → 空内容重试（最多 3 次降级放弃），反馈"只输出正文不输出思考"
- LLM 挂起无限等（urllib timeout Windows 不可靠）→ daemon 线程 + join(用户配置 timeout) 兜底
- write-sub 失败不检查 → 落盘失败不标 done 置 pending，且改为进程内落盘 `_write_sub_inline`（写子进程在 Web 线程不可靠，0 字节根源）+ `_read_sub_content`/`_read_chapter_md` 路径修正（parent → parent.parent）
- `novel_context_loader` CLI 串行阻断（HOOK-BLOCK + sys.exit）→ 删除，Web 串行由 novel_writer 状态机保证；段落盘失败 → 停止整章
- 会话切换子结构残留（旧轮询弹回面板）→ loadSession/newSession 停轮询 + 响应会话守卫 + stopProgressPolling 清面板
- error 会话死胡同（只读渲染无按钮）→ 小说线 error 会话非只读渲染（开始生成=重试/续写入口）
- 输入框高度拉伸错位 → textarea resize:vertical（右上角无空间 → 顶部拖拽条向上拉，双击复位）
- 题材必填前端两入口 + 后端双保险（缺失 400）；篇幅不强制（默认中篇）

## [2.1.0b0] - 2026-08-14
### 新增（小说线交互与一致性强化，2.0.0b0 之后迭代）
- **两阶段规划 + 章级门控**：规划只出章（2 次 LLM，快）→ 写作时逐章「规划子结构 → 下方确认面板 → 确认 → 写本章」；章状态机 `pending → planning → confirmed → in_progress → done`；确认状态持久化在 session（重启/刷新不丢）
- **章级确认面板**（固定生成控制区，不随对话滚动）：每段可**勾选跳过 / 改字数 / 标 ⭐重点 / 看概述 / 段级重规划**；`POST /api/novel/confirm` 应用调整 + 章字数汇总；`_ncConfirmId` 防轮询重复重建丢用户输入
- **段级重规划**：`replan_novel_sub`（保留 s_key/word_count/status/word_count_target，重做 title/summary/tone/emotions/writing_prompt≥50）+ `POST /api/novel/replan_sub`（session 反查 → 更新 state+outline，章保持 planning）
- **篇幅字数档位注入规划**：plan-chapter / replan prompt 注入【字数目标】每子结构 lo-hi 字（短篇 1000-1500 / 中篇 1500-2000 / 长篇 2000-4000），LLM 规划即知档位；写作 prompt 注入确认后字数目标
- **续写恢复**：done 章从 novel 项目文件恢复正文跳过；章中途中断续写时已写段（status=done）从文件恢复**不重写**；confirmed 章续写不重新规划子结构（`==pending` 才规划）
- **前文状态摘要注入**：plan-chapter 注入已规划章节的子结构摘要 + 角色表（受控规模一行一条，防漂移不线性膨胀）

### 修复
- 章大纲无概述 → 章行渲染 📖 概述（buildOutlineHTML 章级补 summary 行）
- 章字数 800 vs 子结构 1500 矛盾 → 章字数=篇幅估算（中间值×默认3段），同步子结构后=各段汇总
- 子结构字段缺失 → `_sync_subs_from_state` 补全 15 字段对齐通用线（is_key/rag/subtitle/type/show_label/_tmpl_key/_logical_order）
- 确认面板藏在大纲卡片 + 文案"请在右侧确认"误导 → 面板移出大纲卡片到固定生成控制区（stop-bar 下方）
- 指纹跨进程非确定（`_fingerprint` 遍历 set 受 PYTHONHASHSEED 影响）→ 全部 `sorted()`，跨进程指纹确定
- `_llm_json` 一次失败即抛 → 对齐通用线 3 次重试 + 思考污染检测反馈（**不禁思考**，模型行为不动）
- 新角色 HOOK-BLOCK 阻断死路（novel-weaver 面向 LLM 对话回路，Web 后台无回路）→ 声明即自动登记不阻断，prompt 收紧只声明有名字角色
- write-sub 内部隐式 finalize-chapter 加载 bge/R1（3.7G）卡线程 → `NOVEL_SKIP_AUTOFINALIZE`，章检统一由 novel_writer 受配置开关控制
- WinError 183（Windows `rename` 目标已存在不覆盖）→ `os.replace` / `Path.replace`
- 规划阶段二次写（init 建空骨架 + _seed_characters 覆盖）→ `init_novel_project` 带 characters 一次建好，死代码删除
- plan-chapter 指纹误拦 → 合法核心字段写入入口（plan-chapter / replan-novel-sub）跳过校验并刷新指纹
- f-string 花括号未转义（Invalid format specifier）→ 去 `f` 前缀

## [2.0.0b0] - 2026-08-14
### 新增（小说模式 NOVEL MODE，独立一条线，按规划 P1-P4 四阶段落地）
> 实施为一次性完成（P1-P4 连续实施），版本号直接落在 2.0.0b0；以下按方案规划的阶段路线（1.10→2.0）组织演进脉络，中间规划版本未独立发布，不伪造版本历史。

#### P1 模板层 + 路由层（对应规划 1.10.0）
- **内置「小说」模板**：meta（标题 auto / 题材 user / 篇幅 user / 叙事视角 auto / 署名 user）+ content 三节点——世界观设定 `kind=setting`、人物表 `kind=setting`（设定节点，不输出正文，存状态）、正文 `kind=chapters`（多章锚点，由 AI 自由展开 L01-L15）；style 文风六字段 + 创作铁律（show don't tell、禁元文本、禁纯抒情、对话符合人格）；logic 创作认知顺序
- **schema 扩展**：`_normalize_template` allowed 集合放行 `novel`/`kind`；旧模板零影响，其他模板路径零改动（L3 路由天然隔离）
- **L3 路由**：`planner.plan_outline` / `writer.generate_article` 检测 `novel.mode` 自动切小说分支；通用线一字不动

#### P2 增强层（对应规划 1.11.0）
- **`structured_writer/novel/` 子包**（移植自 novel-weaver 已停更技能，改造路径）：
  - `novel_bridge.py`：场景配置（人物/时代/地点/冲突，含 MBTI+荣格原型）→ 章数组（短3-6/中8-10/长11-15）→ 因果链验证（概述≥12字符+因果动词，失败反馈重生成）→ 组装标准 outline（章=section，`_novel.chapter` 身份）→ 项目初始化（`data/novel/projects/`）→ plan-chapter 子结构规划（S01-S05，tone/emotions/writing_prompt≥50 硬校验，末章 is_ending）
  - `novel_writer.py`：小说版写作引擎（复用串行骨架+续写机制+停止控制），上下文注入换血——角色表/人格/实体关系网/时间线/情绪基调/上章行为轨迹/写作命题框；逐章自动 plan-chapter；每段写入 novel 项目（原子写入+别名拦截+实体提取+字数三档校验）
- **驱动字段保护**：小说模板「题材」「篇幅」在编辑器锁定（× 灰色不可点，UI 层）+ `validateNovelTemplate` 保存校验（改名/绕过 UI 仍拦截，保存层兜底）；另存为副本继承 novel.mode/kind（collectTemplateData 补 kind + novel 顶层标记）

#### P3 模型层（对应规划 1.12.0）
- **`data/models/`**（与 outputs/sessions/templates 平级）：bge-small-zh-v1.5（184MB）+ DeepSeek-R1-Distill-Qwen-1.5B（3.7GB），保持 HF 目录结构
- **检查器移植**：`novel_semantic_check.py`（bge 向量语义检查）/ `novel_reasoning_check.py`（R1 独立推理审核）——`_load_model` 目录参数改 `data/models/` 直查（查找顺序 data/models → novel-weaver 缓存 → HF 默认），强制 CPU（`CUDA_VISIBLE_DEVICES=-1`）不与 LM Studio 抢显存，无模型自动降级
- **配置面板「小说质检」区**：模型目录/检测/安装指引 + 四个开关（章检规则4检/语义bge/推理R1/全文三检），控制权全在用户；`GET /api/novel/status`、`POST /api/novel/install`、`POST /api/novel/checks` 端点；开关经 `NOVEL_SKIP_SEMANTIC`/`NOVEL_SKIP_REASON` 环境变量控制 finalize-chapter 第5/6步

#### P4 检查体系（对应规划 2.0.0）
- **章检六检**：连通性/跨章承诺链/风格校验/逻辑检查（纯规则，进程内毫秒级）+ bge 语义 + R1 推理（子进程隔离，崩溃不影响主服务，内存可回收）
- **全文三检**：fidelity 大纲忠实度 + 结尾收束三型（封闭/开放/悬停）验证 + 完结；质检报告附加到输出 md
- 章检 HARD 问题写入状态（不阻塞主流程，报告展示），继承 novel-weaver 降级哲学：无模型/未装依赖全部自动跳过

#### 修复与行为说明
- meta「显」语义：通用线未动（false=有值显示裸值行）；小说线专属渲染（`novel_writer._render_meta_block` false=彻底隐藏，题材/篇幅/视角不进文章）——两线解耦，其他模板输出零影响
- UI 说明文案更新：meta 区「显」区分通用/小说两线语义；content 区「显」补充"无标题行+空内容整节跳过"

## [1.9.0b0] - 2026-08-11
### 新增（插件系统 + 大表蓝皮书取数）
- **插件系统**（仿 RAG 形态）：`structured_writer/plugins/` 模块——`base.py`（`BasePlugin.execute(inputs) → {type, name, content}`，输出契约限定辅助知识三形态 table/text/image）、`manager.py`（扫描内置 `builtin/` + 用户 `data/plugins/` 目录、读 `plugin.json`、注册与调用）。新插件 = 一个目录（plugin.json + 实现类），消费方只按 `input_fields` 收参数、按 `output_types` 接数据，不接触写作管道
- **预置插件「数据库数据源」（db_source）**：对接 SQLite（标准库只读 mode=ro）/ CSV / MySQL（pymysql）/ PostgreSQL（psycopg2，驱动缺失自动提示安装）。**所有设置都在插件内**（连接信息配一次固化），消费时只选插件名 → 自动取数挂到子结构「+」→ 取什么数据由使用指令决定，插件不预设 SQL。只读安全：SQLite 只读模式、MySQL/PG 仅 SELECT、表名白名单校验（只允许库内实际存在的表）、凭证仅会话内存不落盘
- **大表蓝皮书取数**（`aux_parser.py` 重写 `select_table` + 新增 `build_blueprint`）：修复大表只给前 50 行标识的漏数据缺陷。按规模分档：小表 ≤100 行 py 全量 0 次 LLM；中表蓝皮书+全量行标识 1 次 LLM 选列行；大表 2 次 LLM——粗筛（py 统计生成蓝皮书 → LLM 选维度和分段）→ 精取（段内全量行标识 → LLM 选列行）→ py 在全量数据精确切。**2016 年在第 10 万行也取得到**
- **蓝皮书统计（天然算法，无领域预设）**：每列唯一值分析（2~500 天然维度）→ 日期列自动聚合到年/月层级 → 随机列尝试文本前缀聚合（分隔符/字符截断）→ 数值列等频分箱。候选维度最多 5 个，category > date > prefix > number 排序
- **前端**：子结构「+」辅助知识模态框新增「数据源插件」区——选插件 → 动态参数表单 → 执行取数 → 结果预览 → 一键挂载（复用 `addAuxFile`，与手动上传完全同管道）；`GET /api/plugins` + `POST /api/plugin/run` 两个端点
- **侵入面**：writer.py / state_manager / 范例系统 / planner **零改动**；`select_table` 签名不变，写作注入调用点原样

## [1.8.0b0] - 2026-08-11
### 新增（快速范例适配）
- **范例大纲适配**（快速范例栏新增「适配新主题」勾选框）：勾选后加载范例时，LLM 只重写**内容项**——章节标题、章节写作要点(summary)、子结构标题、子结构写作要点，按新主题适配；**结构字段物理不变**（RAG 开关/知识库、辅助知识挂载、每节字数、子结构数量、重点标记、勾选状态、`_tmpl_key` 血缘、show_label、逻辑顺序）——LLM 输出格式只含 `{id: {title, summary}}` 文本映射，结构守恒从「约束」变成「物理保证」
- **`planner.adapt_outline()`**：收集节点清单 → LLM 输出文本映射 → 校验已知 id、只取 title/summary 写回；LLM 未返回的节点保留原文；3 次格式失败抛 ValueError（适配失败提示用户，不影响原大纲）
- **`POST /api/example/use` 新增 `adapt` 参数**（True 时先适配再重置状态返回）；前端 `useExample()` 传勾选状态，加载中提示「正在按新主题适配大纲」
- 适配语义：LLM 判断「哪些节需要适配」（prompt 允许与主题无关的通用节如"研究方法"保留原标题），文章标题由新主题覆盖（不参与适配）
### 修复（UI 优化）
- UI 布局与样式优化（页签圆角样式、配置面板居中、文本域样式、响应头 Content-Length 与禁缓存等）
### 变更
- 版本格式启用 `x.y.zbn` beta 标记（1.8.0b0）

---

## [1.7.0] - 2026-08-11
### 新增（快速范例 + 两级局部重规划 + 标题可编辑）
- **章节/子结构标题可编辑**（评审界面）：章节标题、子结构标题改为输入框直接改名；改标题不断模板绑定（见下方血缘标记）
- **`_tmpl_key` 血缘标记**（`planner.py`）：大纲每章烙下其模板 content 源字段名；`writer.py` desc 注入、`logical_order`、`citation_check` 匹配全部改按 `_tmpl_key` 查表——「参考文献」改名「文献引用」后，GB/T 7714 权威写作要求、引用格式化、逻辑顺序原样继承，标题只负责展示
- **快速范例**（`data/examples/examples.json`）：
  - 「保存范例并生成」按钮（评审界面）：前置保存当前大纲为范例 → 开始写作 → **生成完成后自动回填文章全文**（前端传 output_file，后端读文件，非截断预览）
  - 快速调用（对话发送区第二行）：范例下拉 + 新主题 + 「用范例写作（跳过规划）」——加载范例大纲，跳过 LLM 全局规划，直接进评审界面；新主题覆盖文章标题，章节结构/要点/字数保留范例原样
  - 新端点：`GET /api/examples`、`POST /api/example/save`、`POST /api/example/update_article`、`POST /api/example/use`
  - 调用时重置所有节点写作状态（pending），避免范例大纲携带上次 done 状态
- **两级局部重规划**（`planner.replan_section()` + `POST /api/replan_section`）：
  - 章节级（section-card 头部「↻重规划」）：章节内全部子结构按新交互重做，数量可变，章身份（排序/逻辑顺序/模板绑定/重点/RAG）保留
  - 子结构级（sub-card 行尾「↻」）：只重建目标子结构，其余节点不动
  - 身份属性（`_tmpl_key`/`show_label`/`is_key`/`rag`/`_logical_order`）显式继承，展示属性（title/summary/word_count）取 LLM 结果；`word_count` 自动 = 新子结构之和；状态重置 pending
  - 与整篇重规划（原有「重新规划」按钮）共存，互不影响；局部重规划后原大纲卡片原地刷新，用户已做的勾选/排序/字数调整保留
### 变更
- `_handle_generate` 新增 `titles` 参数（章节/子结构改名应用）
- `config_manager.py` 新增范例存储层（原子写入、更新时间倒序列表、文章回填、删除）
- README「对外写作 API」条目更新为 v1.7.0

---

## [1.6.0] - 2026-08-07
### 变更
- **1.6.0b0 转正 1.6.0**：对外写作 API 验证通过
- PyPI 元数据新增 project_urls（GitHub / Gitee / Documentation 三链接），页面 Project Links 区显示双平台仓库链接

---

## [1.6.0b0] - 2026-08-06（beta 验证版：对外写作 API；验证通过后转正 1.6.0）
### 新增（对外写作 API，仿 rag-assistant 8767 模式）
- **新模块 `external_api.py`**：`--api-port`（默认 8777）独立端口启动，与 Web UI(8770) 完全隔离；`http.server` + 统一 `{"success": bool}` 响应风格
- **`POST /api/write`（同步写作，核心）**：
  - `prompt`（必填）+ `template` 三形态：模板名 / 内联模板 JSON（结构校验）/ `template_desc` 描述生成（走 SCHEMA 规矩）
  - `instructions` / `title` / `meta`（填 source=user 字段）/ `word_count` / `context_review_length` / `fact_check`
  - `images`：base64 数组（≤20 张、单张 ≤20MB、type 与扩展名一致校验），`target` 可选（子结构标题模糊匹配），**缺省落点 = 第一个 section 节末尾**
  - `rag`：`{enabled, kb（空=自动路由，sub 级查询沿用同一 kb）, cold_start}`——探测 8767 → 冷启动拉起 rag-assistant 子进程（≤90s）→ 失败降级纯 LLM 写作（**不报错**），返回 `rag.status = off|online|cold_started|degraded`
  - `format`：`md` / `latex`（含四类图片排版）/ `pdf`（xelatex 编译，base64 返回）
- **`GET /api/health` / `GET /api/capabilities` / `GET /api/rag/status`**（仿 8767 风格）
- **模板生成逻辑抽取**：`GEN_TEMPLATE_SYSTEM_PROMPT` + 3 次重试容错解析 + `_normalize_template` 从 `web_ui._handle_gen_template` 迁移至 `planner.generate_template()`（行为逐字节一致），web_ui 调用处替换——模板规矩单一来源
### 变更
- `main.py` 新增 `--api-port` 参数（默认不启动，不影响现有启动路径）
- 写作管道（planner/writer/md2tex/rag_client/config_manager）零改动，全部复用

---

## [1.5.0] - 2026-08-06（由 1.5.0b1 迭代转正：四类图片排版 + 非浮动独立块 + 文件名特殊字符保护 + 辅助知识面板文案；b1 未发布废弃）
### 新增（图片分类排版）
- **图片按像素尺寸自动四类排版**（`md2tex.py`，读 PNG/JPEG/GIF 头，标准库 struct 零依赖）：
  - **小图**（像素宽 ≤900 且非竖图）：两列并排，每张 0.48 页宽（minipage + \hfill）；奇数张时最后一张落下一行左列左对齐，**无居中孤张**
  - **中图**：0.8 页宽左右居中，高度按各自宽高比等比
  - **竖大图**（按 0.92 页宽渲染高度占比 >0.75）：`0.92\textwidth × 0.85\textheight` 双约束 + keepaspectratio，任何宽高比不越界
  - **旋转大图**（像素宽 >2600 且宽高比 ≥2.5 的超宽全景）：`angle=90` 旋转竖放 + `width=0.92\textheight`，原宽被页高限定、等比不变形
- **全部阈值比例化**（\textwidth/\textheight 相对值），排版与纸张尺寸（A4/letter 等）无关
- `md_to_tex` 新增 `image_base_dir` 参数（读尺寸需定位文件）；`web_ui.py` 生成 tex 时传入输出目录
- 尺寸读取失败/未识别格式 → 降级中图 0.8 页宽；`image_base_dir` 为空 → 全量 0.8（向后兼容旧行为）
### 修复
- **图片文件名含 LaTeX 特殊字符编译失败**：文件名含 `&`/`%`/`#`/`_` 等（如从 URL 转存的 `u=...&fm=...` 图片名）时 graphicx 文件名解析阶段报 `Missing endcsname` → `\detokenize{...}` 保护后编译通过
### 变更
- 旧版所有图片统一 `width=0.8\textwidth` 改为四类自动排版；行内图片（正文嵌图）固定 0.5 页宽
- **图片块改为非浮动独立块**：`figure[H]`（强制本页，页尾放不下报 `Float too large` 编译错误）→ `\par` + `center`/`minipage` 独立块——紧跟文本流末尾下一行、居中/两列排列，空间不足时整块自动换到下一页，**不与文字混排、不强制本页**；图注用 `\captionof`（新增 caption 宏包），表格 `[H]` 保持不变
- **辅助知识面板文案明确图片定位**：指令框 label/placeholder 改为「作用于文字/表格资料；图片自动插图至本子结构末尾，不受指令控制」，文件列表中图片项加「（自动插图至末尾）」后缀——图片位置由 py 确定性控制（本就 LLM 零参与），UI 不再暗示可指令图片位置，消除文图脱节误导
- README「tex/pdf 生成」条目同步 v1.5.0 功能说明

---

## [1.4.0b3] - 2026-08-03（b1/b2 已废弃：b1 classifier 误标、b2 文件名被 PyPI 删除复用受限；b3 修正为 Beta + long_description 粘合更新日志）
### 新增（tex/pdf 生成）
- **预览模态框「生成 tex+pdf」按钮**：一键将文章 md 转换为 .tex 并编译出 .pdf，产物与 md/图片同目录；生成成功在预览框内显示 tex/pdf 完整路径（无弹窗，状态由按钮文字 + 信息区承载）
- **LaTeX 环境自包含**：点击后自动检测可用引擎（xelatex 优先、lualatex 回退，PATH + MiKTeX 常见路径），未安装则 `winget install MiKTeX` 自动安装；装完设置 `[MPM]AutoInstall=1`（宏包自动静默安装，无弹窗）；MiKTeX bin 自动注入当前进程 PATH
- **复用 latex-modular 技能脚本**：编译走 `~/.workbuddy/skills/latex-modular/scripts/validate.py --engine <引擎> --fix`（错误自动修复）；宏包顺序/文档头组装参考其 compose.py
- 新模块 `md2tex.py`：Markdown → LaTeX 确定性映射（#→section、表格→tabular、图片→includegraphics、粗体/斜体/列表/引用/代码块、特殊字符转义），中文经 ctex + Windows 自带字体
- 技能脚本缺失时回退直接 `<引擎> -interaction=nonstopmode -halt-on-error` 编译
### 修复
- **Windows 编码崩溃（2 类）**：① `subprocess.run(text=True)` 默认 GBK 解码 UTF-8 输出 → `UnicodeDecodeError`，全部补 `encoding="utf-8", errors="replace"`（web_ui 2 处 + validate.py 3 处）；② validate.py 子进程内部 print/open 默认 GBK 编码 Unicode 字符（×/✓）→ `UnicodeEncodeError`，调用时强制 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`
- **lualatex + ctex 不兼容**：报 `this package currently works only with XeTeX` → 改用 **xelatex**（ctex 官方推荐引擎，兼容性最好；lualatex 保留为回退）
- **geometry Option clash**：md2tex.py 的 PACKAGES 列表与文档头各加载一次 geometry → 从 PACKAGES 移除，保留文档头带边距选项那一次
- **找不到 lualatex**：MiKTeX 装在用户目录（AppData）不在系统 PATH，且 winget 装完不更新当前进程 → 模块加载时检测 3 个 MiKTeX bin 候选路径 prepend 到 `os.environ["PATH"]`
### 变更
- UI 静默化：生成结果不再 `alert()` 弹窗，改为按钮文字状态 + 预览模态框 `#texpdf-info` 信息区（成功显示 tex/pdf 路径，失败显示错误摘要）

---

## [1.3.0] - 2026-08-03
### 新增（tex/pdf 生成）
- **预览模态框「生成 tex+pdf」按钮**：一键将文章 md 转换为 .tex 并编译出 .pdf，产物与 md/图片同目录
- **LaTeX 环境自包含**：点击后自动检测 lualatex（PATH + MiKTeX 常见路径），未安装则 `winget install MiKTeX` 自动安装，全程无人工介入
- **复用 latex-modular 技能脚本**：编译走 `~/.workbuddy/skills/latex-modular/scripts/validate.py --engine lualatex --fix`（错误自动修复）；宏包顺序/文档头组装参考其 compose.py
- 新模块 `md2tex.py`：Markdown → LaTeX 确定性映射（#→section、表格→tabular、图片→includegraphics、粗体/斜体/列表/引用/代码块、特殊字符转义），中文经 ctex + Windows 自带字体
- 技能脚本缺失时回退直接 `lualatex -interaction=nonstopmode` 编译

---

## [1.2.0] - 2026-08-03
### 新增（辅助资料系统 + 目录化输出）
- **辅助资料按类型特化三条管线**（子结构"+"按钮）：
  - **图片**（.png/.jpg/.jpeg/.gif）：py 确定性插图——生成时复制到输出目录、子结构正文末尾自动追加 `![](图名)` 相对路径引用，LLM 零参与，无写错风险
  - **文字**（.txt/.md）：原样注入【辅助知识】，注入前截断 8000 字符防撑爆上下文
  - **表格**（.csv/.db）：标准库解析（csv/sqlite3，零第三方依赖）——**表头定位鲁棒**（启发式：多单元格+文本列名+列数与数据行一致，自动丢弃大标题/说明行；失败则 LLM 看原始行定位，兼容英文表头/合并单元格/双行表头）——小表（≤100 行）全量 JSON 注入；大表由 LLM 直接看着列标题/行标题选列选行（理解归 LLM、执行归 py，无正则穷举，中英列名均可），失败回退前 50 行
- **命令框语义升级**：模态框输入框从"填资料内容"改为"填使用指令"（如"必须真实采用以下资料进行分析"），placeholder 按已选类型提示
- **防造数据数值校验**：注入表格时收集数据源数字集合，生成后扫描正文带单位数值，未在数据源找到的（非年份/编号）追加到"建议人工复审"清单
- **输出目录化**：每篇文章一个文件夹 `data/outputs/<标题>_<时间戳>/`（含 md + 图片集，无图也建目录），md 用相对路径引用图片
- **上传接口 `/api/aux_upload`**：base64 JSON 通道，扩展名白名单 + 20MB 限制，存会话临时目录（不入 session JSON）
### 变更
- `aux_parser.py`（新）：csv/sqlite 解析、select_table（小表全量/大表 LLM 选列行）、JSON 转换、文字截断、数字提取与正文校验
- `web_ui.py`：模态框文件类型放开 + 命令框；输出三接口（list/read/delete）适配目录文章——扫文件夹内 md、读目录内 md、删整个文件夹（含图片集）；**新增 resolve 路径穿越防护**（修复旧代码 `OUTPUTS_DIR / name` 可越界的隐患）；前端列表图片徽标 + 目录删除确认提示
- `writer.py`：辅助资料按类型分流注入、图片 py 插图、目录化落盘、数值校验接入事实自检清单
- 旧平铺 .md 输出与历史文件：三接口照常兼容管理

## [1.1.0] - 2026-07-31（正式版，自 1.1.0b15 累积）
### 行为变化（用户可见）
1. **参考文献列表只包含正文真实引用的来源**：以前按 RAG 全集 1-5 全列（未引用的也列），现在只列正文实际用到的，悬空条目消失
2. **正文引用编号按真实引用顺序、保持连贯**：以前 LLM 只用来源 1/2/4 时正文是 [1][2][4] 缺号，现在按首次出现顺序编号 [1][2][3]，同一来源多处引用同号
3. **LLM 输出的标签不再被清理**：以前"摘要：[1]"会被自动清成"[1]"，现在原样保留——标签去留完全由模板"显"开关和 LLM 输出决定
4. **进度条不再提前满格**：writing 阶段封顶 95%，收尾（参考文献格式化/保存）有状态文本提示，phase 完成才 100%
### 修复
- **引用编号不连贯 + 参考文献悬空条目**：原后处理按"来源全集顺序"编号（1=keys[0]...），LLM 只用来源 1/2/4 时正文出现 [1][2][4] 缺号，参考文献列表却 1-5 全列（3、5 未被引用）。重构为**按正文真实引用顺序重编号**：扫描占位符 → 首次出现顺序去重 → 旧→新映射 → 全局替换正文 + 仅对真实引用构建列表（悬空条目消失）
- **清理正则误删中文句子**：原 `[\u4e00-\u9fff]{2,}[\s：:]*\[(\d+)\]` 在无冒号时也会匹配，把"正文第一段[1]"误删成"[1]"——该正则存在理由不成立（LLM 输出被约束为"引用自来源N"，替换后即为干净的 [N]），且与"显"（show_label）标签语义冲突，**直接移除**，标签去留交给"显"和 LLM 输出
- **desc 字数与大纲字数冲突**：模板 desc 如"约200-300字"在规划器解析（→word_count）后仍以原文注入【当前章节要求】，若用户在大纲中把字数改为 50，LLM 会同时看到 50 和 200-300。新增 `_strip_word_desc`：注入 desc 前清洗独立字数短语（"约200-300字"/"300字左右"），字数由「字数要求」行唯一确定；语义嵌入型（"每个小标题不少于50字"）不删
### 变更
- `citation_validator.py`：列举归一化（"引用自来源1、来源2、来源4"合并写法补全标记）、正则回调替换（免疫来源 ≥10 的误伤）、不存在来源标记直接删除、无真实引用时参考文献节整体删除
- `planner.py`：新增 `_strip_word_desc`（与 `_parse_word_count` 匹配形态一致）
- `writer.py`：desc 注入前调用 `_strip_word_desc`；收尾阶段补状态文本（"正在格式化参考文献…"/"正在保存文章…"）
- **进度条封顶 95%**：进度只统计逐节写作单元，收尾阶段（参考文献 LLM 格式化/保存）无进度单元，writing 阶段封顶 95%，phase=done 才满格——修复"进度条提前满格但 LLM 仍在工作"的误导

## [1.1.0b15] - 2026-07-31
### 修复
- **关键词节输出一整套写作（desc 指令丢失）**：模板 content 项的 desc（如"仅输出3-5个关键词，不要段落"）在规划→写作之间丢失——写作引擎只注入大纲的 word_count + summary，从不读模板 desc；而规划器给关键词这类 leaf 节兜底 800 字，写作提示变成"约800字"诱导长文。修复两处：
  1. **desc 确定性注入【当前章节要求】**：写作时按节名匹配模板 content，将 desc 作为"本节要求"注入写作提示，不依赖规划器转述
  2. **leaf 字数按 desc 解析、拒绝 800 兜底**：新增字数解析函数，leaf 节 desc 无数字→0（字数不限，由 desc 指令约束）、有数字（如"200-300字"）→取中值；section 节保持现状（desc 无数字→800 或保留规划器值）
### 变更
- `planner.py` 提取 `_parse_word_count`，规范化补全与已存在节共用同一字数解析逻辑（leaf 强制按 desc，section 不动）
- `writer.py` 写作提示新增"本节要求"区块（模板 desc），与"写作要点"（规划器 summary）分离，互不污染

## [1.1.0b14] - 2026-07-31
### 移除
- **指纹保护机制**：删除 `IMMUTABLE_FIELDS`、指纹计算、校验方法及会话中的指纹字段。该机制与评审阶段的交互式大纲编辑（字数/重点/勾选/排序）冲突——评审正是要改这些字段，但改后从不更新指纹，机制形同虚设且从未在关键路径生效
- **引用检查系统（validate + format_report）**：删除引用验证器及其 9 个辅助函数（格式/一致性/来源三项检查）。引用在生成时由 Python 后处理确定性完成，检查属引用机制缺失时的过渡补救，全项目无任何调用点（死代码）。保留 `post_process`（引用后处理）
### 变更
- `citation_validator.py` 收敛为纯引用后处理模块
- `state_manager.py` 移除 `hashlib` 依赖，docstring 更新

## [1.1.0b13] - 2026-07-31
### 修复
- **写作引擎必现 NameError（content_fields 未定义）**：`generate_article` 在 style 不含"引用/参考文献"关键词时（通用公文、日常写作、新闻报道、技术报告等模板），引用规则检测引用了未定义的变量，直接抛异常导致生成失败。修复：内容树字段定义提前到函数开头。此前只有学术论文/论文综述（style 含"引用"）能侥幸绕过短路
- **leaf 节（叶子节）RAG 结果被丢弃**：叶子节启用 RAG 后，节级检索的上下文在构建写作提示时硬编码不注入，白查一次。修复：叶子节注入节级 RAG 上下文
- **批量自动撰写与前文回顾/引用处理不一致**：批量模式调用写作引擎时未传前文回顾字数配置（长文 token 爆炸风险）且未构建引用验证配置（引用后处理整体失效）。修复：批量模式与单篇模式对齐，传入前文回顾长度与引用配置
- **规划器大纲输出截断无续接**：规划器单次调用 LLM，输出被 max_tokens 截断时直接判失败重试；且 max_tokens 下限硬编码 4096 且注释声称已删除（注释与代码不符）。修复：给规划器补上截断续接机制（与写作引擎一致，最多 4 轮），下限调低至 2048 并明确语义——2048 保证推理模型完成推理并输出大纲主体，截断部分由续接补全；低于 2048 推理吃光 token 输出为空，续接无法挽救
### 变更
- 规划器 `plan_outline` 改用带 finish_reason 检测的调用方式，支持截断续接

## [1.1.0b12] - 2026-07-31
### 修复
- **leaf 节写作报错 Missing section_title**：删除了 `_build_context_section_prompt()` 的 `section_title` 参数，已补回
- **LLM 输出引用自来源 N 带空格不匹配**：后处理新增 `引用自来源 N` 和 `引自来源 N` 空格变种替换
- **max_tokens 硬编码覆盖用户配置**：`citation_validator.py` 的 LLM 格式化调用 `max_tokens=4096` 改为 `None`，走用户上下文窗口
- **logic 模板残留旧引用指令**：学术论文/论文综述 logic 字段中的"引用来源标注为目标文件名"已删除
- **IMRaD 缩写清理**：学术论文 logic 字段 IMRaD → "按顺序撰写"
### 改进
- **删除按钮二次确认**：输出列表删除不再使用浏览器 confirm 弹窗，改为行内确认/取消
- **上下文行为保持一致**：恢复 `_logical_order==2` 传全部上下文的原始设计

## [1.1.0b11] - 2026-07-30
### 重构
- **引用系统全面重构**：citation_check 改为纯后处理模式，不再校验/验证
- **引用后处理移至独立模块**：`citation_validator.py` 接管全部引用操作，`writer.py` 只调用一行
- **引用标记改为「来源N」**：LLM 使用 `引用自来源1/引自来源1` 格式替代原始文件名，消除 LLM 识别长文件名困惑
- **RAG 参考资料与来源编号打通**：`【文档: filename】` 自动替换为 `【来源N】`，LLM 直接对照
- **all_rag_headers 去重简化后处理**：利用 dict key 唯一性，去掉 `_seen`/`_order` 等去重逻辑
### 修复
- **regex 不匹配中文文件名**：引用来源改用编号，不再需要文件名 regex
- **LLM 缩写「引自来源1」也可匹配**：后处理同时替换 `引用自来源N` 和 `引自来源N`
- **摘要结论字数强制 0（自由撰写）**：plan 解析 desc 中字数描述
- **摘要结论 desc 加入"不使用引用标记"**：避免 LLM 在摘要/结论中写引用
- **关键词 leaf 节点字数默认 0**：无字数描述时自由发挥
- **UI 字数显示不再造假**：word_count=0 显示"自由"而非 800
- **引用节字数强制 0**：`citation_check=true` 的节 word_count=0
- **删除按钮改为二次确认**：不再使用浏览器 confirm 弹窗
### 新增
- **LLM 格式化参考文献**：后处理构建元信息列表后调 LLM 格式化为标准引文
- **planner 提示词强化字数规则**：明确"desc 中有字数要求以此为准"
- **LLM 引用指令提示词加强**：明确要求使用 `引用自来源N`，不要直接用编号格式
### 移除
- **删除引用验证子系统**：不再校验引用格式/一致性/来源

## [1.1.0b10] - 2026-07-30
### 修复
- **引用后处理 regex 不匹配中文文件名**：`[a-zA-Z0-9_.\-]+` → `\S+`，支持中文文件名的引用标记提取
- **关键词 leaf 节输出正文**：学术论文模板关键词 desc 改为"仅输出3-5个关键词（用逗号分隔），不要段落"
- **参考文献节元信息未格式化**：新增 LLM 后处理步骤，将原始元信息规范化为标准引文格式
### 重构
- **模板存储分离**：内置模板转为代码常量只读，用户模板存 `data/templates/user_templates.json`
- **内置模板只读 UI**：保存/删除按钮在内置模板时 disabled，修改须用"另存为"
- **默认模板更新**：学术论文 → IMRaD（方法/结果/讨论替代正文），论文综述 → 引言/分主题评述/研究空白
### 新增
- **内置模板引用校验默认开启**：学术论文和论文综述的参考文献字段默认 `citation_check=true, citation_format="[x]=1."`
- **对话界面三栏布局**：左侧会话管理、中间对话交互、右侧已完成文章列表
- **已完成文章列表**：`data/outputs/` 文件按修改时间倒序，点击查看、确认删除、30秒自动刷新
- **meta 输入框自动渲染**：不再依赖 setTimeout，配置加载完成后立即显示

---

## [1.1.0b9] - 2026-07-30
### 重构
- **引用系统整体重构**：引用规则从 style 移至参考文献 desc，style 仅保留纯风格
- **后处理系统**：Python 全权处理引用替换（扫描「引用自{文件名}」→去重→按首次出现编号→正文替换→参考文献重排），LLM 仅负责格式化
- **prompt 分层重构**：`_build_context_section_prompt` 输出结构拆分为【全文风格背景】【前文回顾】【当前章节要求】【引用来源】四个独立区域，LLM 能明确区分各块用途

### 新增
- **addContentRow**：引用列改为两个独立输入框（□=□），正文格式和条目格式分开配置
- **引用后处理**：`generate_article` 末尾执行，引用列打勾时触发
- **rag_client**：Content-Type 增加 `charset=utf-8`，默认超时 30→60 秒，添加 3 次重试

### 修复
- **引用自正则**：`\S+` 和 `\w+` 在 Python3 下默认匹配 Unicode（含中文），改为 `[a-zA-Z0-9_.\-]` 仅匹配 ASCII
- **config.json**：学术论文/论文综述/test 三个模板全部 content 项设 `citation_check=True` + `citation_format="[x]=1."`

### 模板变更
- 学术论文/论文综述/test：style 移除引用规则，参考文献 desc 内嵌映射规则和格式说明

## [1.1.0b8] - 2026-07-29
### 修复
- **style 引用规则3导致正文不标注引用**：删除了"仅在有 RAG 提供资料时进行引用标注"的条件规则，改为无条件"正文必须标注引用"。影响学术论文/论文综述/test三个模板
- **RAG external_api GBK 编码崩溃**：`_read_body()` 增加 GBK fallback，当请求体包含 GBK 编码的中文字符时不会崩溃（curl在中文Windows上的已知问题）

## [1.1.0b7] - 2026-07-29
### 新增
- **双格式引用映射**: `_parse_citation_mapping()` 解析 `[x]=1.` 格式，实现正文与参考文献条目的格式分离
- **格式兼容**: 支持 `x`/`n` 占位符和纯数字格式（`1.` / `[1]`）自动识别

### 变更
- **引用验证器** 格式检查/一致性检查/来源验证全部改用双格式：正文用 inline 格式（[x]），条目用 ref 格式（1.）
- **模板编辑器** 引用列默认值改为 `[x]=1.`，宽度加宽至 60px

## [1.1.0b6] - 2026-07-29
### 新增
- **保存按钮**：模板编辑器新增「保存」按钮，直接更新当前模板（区别于「另存为」创建副本），保存成功显示「已保存 ✓」
- **collectTemplateData 共享函数**：抽取模板数据收集逻辑，供「保存」和「另存为」共用

### 变更
- 底部提示更新：「修改表格后点保存直接更新当前模板，或另存为创建副本」

## [1.1.0b5] - 2026-07-29
### 新增
- **引用验证系统**（citation_validator.py）三项验证：
  - 格式检查：对勾选了引用检测的内容节，检查其内容区是否存在 `[x]` 格式引用编号
  - 一致性检查：全文引用编号是否连续、与参考文献条目编号是否一致
  - 来源验证：每条参考文献条目与 RAG 返回的文档元数据（headers）做子串匹配，匹配成功的标记为已确认，未匹配的列入人工复核区
- **提示词开关**：`_needs_metadata()` 从模板 style 自然语言检测"引用""参考文献""citation"等关键词，自动触发 RAG include_header=True
- **RAG 元数据累积**：writer.py 累积所有 RAG 查询的 headers 到 all_rag_headers，注入写作 prompt 的「文档元信息」区域，供 LLM 写作时准确引用
- **rag_client.py** `query()` 增加 `include_header` 参数
- **web_ui 大纲**：每个 content 项增加「引用」复选框 + 格式输入框（默认 `[x]`，可自定义）

### 变更
- **引用验证报告**追加到文章末尾，独立于事实自检区域

## [1.1.0b4] - 2026-07-29
### 修复
- **leaf 节 continue 跳过 parts_by_sid**：leaf 路径末尾的 `continue` 使得 `parts_by_sid[sid]` 赋值永远不执行，leaf 节（关键词/摘要/参考文献）有字数记录但内容不进 .md 文件 → 在 `continue` 前补充 parts_by_sid 写入
- **show_label if/else 嵌套导致 LLM 调用错位**：sec_show_label 的 if/else 把 `if s_type == "leaf":`（LLM 调用）包进了 else 分支，导致 show_label=true 的节完全不调 LLM → 将 LLM 调用移出 if/else
- **section["show_label"] 无 fallback**：从模板直读 `section["show_label"]`，LLM 输出的节不包含该字段 → planner _normalize_outline 传播 show_label 到所有 section
- **gen-template 验收逻辑过松**：`result.get("meta") is not None` 通过 `[]`（空数组非 None）→ 改为 `if result.get("meta") or result.get("content")`（truthiness 判断）
- **saveConfig/confirmSaveAs 表格索引错位**：meta 行 querySelectorAll 返回 3 个元素但代码读 inputs[3]；content 行预期 5 个元素实际 4 个（button 非 input/select）
- **planner JSON 示例硬编码真实姓名**：示例值改为通用占位符

### 新增
- **style_hint 注入**：`_build_context_section_prompt` 加 `style_hint` 参数，将模板 `style` 注入每节 prompt 作为"写作风格要求"
- **学术论文 引用规则**：style + 参考文献 desc 分开放（行为规则在 style，格式规则在 desc），含正文[1][2]标注、RAG 条件引用、引用一致性
- **_normalize_outline 兜底补缺**：对比 content_fields 所有 name 和现有 sections title，缺失的自动补入
- **is_key 自动标记恢复**：planner prompt 加 `is_key: true = 重点节，字数上浮50%`，JSON 示例每个 section 恢复 is_key 字段
- **_normalize_template 校验**：gen-template 后端校验，清理非法类型、补默认值、删多余字段
- **另存为模态框**：替换 `prompt()` 浏览器弹窗

### 变更
- **logical_order 语义修正**：0=先写（存模板），自动=不设（不参与逻辑排序）。UI 四选项一一对应：自动/先写(0)/其次(1)/最后(2)
- **context 传递策略**：leaf 节 `_logical_order=2` 传全文，其他节（含所有子结构）截取 `context_review_length` 字（默认 800，可调，0=不截断）
- **所有章节统一 `##` 级别**：去掉 `_first_leaf_rendered → #` 的 H1 污染
- **学术论文/论文综述 show_label**：摘要/引言/结论打勾显示标题，正文不打勾
- **关键词 desc**：改为"3-5个关键词，以分号分隔，不要成段描述"
- **默认 context_review_length**：800→8000→恢复为 800（子结构只用尾巴），leaf order=2 传全文
- **样式规则**：要求只在 RAG 开启时才引用，RAG 关闭时不标注
### 修复
- topic 注入 meta 导致 auto 标题被覆盖 → 彻底删除两处注入，LLM 自主生成标题
- meta 块 show_label=true 空值整行跳过 → 改为显示标签占位" > 名称："
- ConfigManager.update 对 templates 用合并而非替换 → 改为全量替换，删除后生效

### 新增
- plan_hints 模态框：重新规划时可输入章节/字数要求，留空按默认
- planner 层级规则 + 用户要求优先规则注入 prompt
- 8 个内置模板 logic 字段（写作顺序提示词）

### 变更
- 配置 tab 拆 meta[] + content[] + style + logic 四区，去掉"渲染为"列
### 架构变更
- **模板格式重大重构**：从平面五元组拆分为 meta[] + content[] + style + logic 四部分
  - 元数据（meta）：标识/管理信息，短数据（≤100字），source=user/auto/llm，固定 leaf
  - 内容树（content）：文章主体，长文本（≥200字），source 固定 llm，type=leaf/section
  - 逻辑提示词（logic）：控制 LLM 认知流程顺序，不改变文章最终排列
- **GEN_TEMPLATE_SYSTEM_PROMPT 重写**：明确定义元数据 vs 内容树的严格二分法

### 新增
- 8 个内置模板全部配置逻辑提示词
- 两个表格列描述 + 逻辑/风格提示词说明文字

### 修复
- renderMetaInputs 读旧格式 tmpl.structure → grid 消失
- deleteTemplate 删不掉（ConfigManager.update 浅合并问题）
- batch_auto template 未定义变量
- _handle_plan 未识别新格式 template
- 温度行因 min-width 溢出

### 变更
- 配置 tab 拆元数据 4列表 + 内容树 4列表 + 逻辑 textarea，去掉"渲染为"列
### 新增
- **五元组结构化模板系统**：模板从纯文本提示词升级为 `{name, show_label, desc, source, type}` 五元组结构，一份数据结构同时定义元数据（标题/作者/单位等）和内容树（引言/正文/结论/参考文献等），覆盖日常写作/学术论文/正式公文/新闻报道/技术报告全部类型
- **动态 Planner prompt 生成**：`plan_outline()` 根据五元组按 `source=user/llm/auto` 分类处理，user 字段不碰、llm 字段必生成、auto 字段用户可填留空 LLM 兜底
- **type:leaf 节支持**：无子结构的扁平节（标题/关键词/摘要/参考文献等），渲染跳过 `###` 标题，直接写内容在 `##` 下
- **meta 块输出**：文章全文前插入 `> 名称：值` 元数据块，按 `show_label` 控制前缀显隐
- **结构表格编辑器**：配置 tab 新增五列可编辑表格（名称/显示/字段意义/填写者/子结构类型）+ 纯展示"渲染"列（自动推导字段出现在聊天输入框还是大纲节）
- **字段意义模态框**：点击表格行中的"字段意义"预览文字弹出 modal textarea，支持长文本编辑，表格中显示截断预览
- **LLM 对话生成模板**：配置 tab "从对话生成" 按钮 → 弹窗输入描述 + 可选模板名称 → LLM 自动生成五元组结构 + 风格提示词 → 保存为自定义模板
- **动态 meta 输入框**：根据模板 `source=user/auto` 的字段，在聊天气泡下方按 4 列 grid 动态渲染输入框，值自动传给 Planner
- **模板搜索排序**：下拉框按拼音字母排序，"自定义"永远在最后
- **内置模板元数据字段**：学术论文/正式公文等新模板预置作者/单位/文号/关键词等字段
- **模板选择持久化**：切换模板时自动保存 `selected_template` 到 config.json，重启后恢复
- **ThreadingHTTPServer**：从单线程 `HTTPServer` 升级为多线程，LLM 请求不阻塞其他 API（归档/配置/进度）
- **删除会话双击确认**：归档会话的删除按钮，第一次单击变红显示"确认?"，2.5 秒内再点执行删除，替代 `confirm()` 浏览器弹窗
- **favicon 静默处理**：返回 `204 No Content`，消除控制台 404

### 变更
- `config.json` 模板格式重构：`templates` 从 `{"名": "字符串"}` 升级为 `{"名": {"structure": [五元组], "style": "字符串"}}`
- `planner.py` 接口变更：`plan_outline()` 新增 `template` 和 `user_meta` 参数，旧字符串调用兼容
- `writer.py` 接口变更：`generate_article()` 新增 `template` 参数（用于 meta 渲染），默认 `None` 兼容旧调用
- `web_ui.py` 路由表新增 `/api/gen-template` 和 `/favicon.ico`
- `config_manager.py` 新增旧格式自动迁移 + "自定义"模板硬保护

### 修复
- `const label` 重复声明导致 JS 加载失败 → 删掉重复行
- 从对话生成模板 `max_tokens=4096` 导致 JSON 截断 → 改为 `None`（走配置值）加 3 次重试 + 多级 JSON 容错解析
- `HTTPServer` 单线程阻塞 UI → 替换为 `ThreadingHTTPServer`
- `onTemplateChange()` 未持久化 `selected_template` → 切换时自动保存
- 模板下拉框排序混乱 → 拼音字母排序 + "自定义"永远最后
- 旧纯字符串模板格式迁移 → `config_manager.py` `load()` 自动检测+转换
### 新增
- **每子结构字数可编辑**：章节字数改为子结构字数之和（自动实时求和），子结构字数输入框直接可改；取消勾选的子结构不计入章节字数
- **进度条按过滤后子结构总数计算**：取消勾选的子结构不再计入进度分母
- **RAG 离线时复选框禁用**：8767 未上线时 RAG 复选框 disabled＋title 提示；上线后自动同步 KB 下拉框
- **子结构辅助知识模态框**：每个子结构 "+" 按钮 → 弹窗支持文本输入 + .txt/.md 文件上传（FileReader 前端读取）
- **RAG 与辅助知识 Prompt 分离注入**：`【RAG 参考资料】` 和 `【辅助知识】` 两段独立标注
- **前文回顾字数可配置**：配置页 "写作参数" 新增输入框，`context_review_length` 写入 config.json
- **配置项自动合并**：`config_manager.load()` 深层合并（嵌套 dict 中新 key 自动补上）；`update()` 支持写入新增键
- **LLM 模型自动检测**：`_build_payload` 中 model 为空时自动调 `list_models()` 取第一个已加载模型
- **批量自动撰写**：输入框写入多行（每行一个主题）→ 后端 `/api/batch_auto` 逐篇规划+RAG+生成 → 前端轮询批量进度
- **单篇自动撰写**：输入框旁 "自动撰写" 按钮 → 前端 chain `plan→generate`，全量自动 RAG
- **事实自检系统**：配置页 "事实自检" 开关 → 写作 prompt 末尾内嵌 `【事实待核查】` 标记 → LLM 在同一 response 中自检 → 解析标记收集 → 文章末尾编号列表汇总。**零额外 LLM 调用**
- **无问题时也输出自检段落**：即使所有子结构都返回"无"，文章末尾也输出 `## 建议人工复审` + `未发现需标记的问题`
- **会话归档/恢复/删除**：侧边栏每项 "🗂 归档" 按钮 → `data/archives/sessions/` 折叠区 → "↩ 恢复" + "✕ 删除"（`confirm()` 确认）
- **自动会话限额**：`max_sessions`（默认 20）→ 新建会话超出时自动归档最旧非当前会话
- **停止生成**：聊天区底部 "延时停止"（当前子结构写完停）+ "立即停止"（续写边界停）→ 保留已写内容输出 .md
- **规划器优先遵循用户指令**：约束前加 "优先遵循用户明确指定的结构要求"，`sections 数量` 改为 "如用户未指定"
- **规划/写作模型温度可配置**：配置页新增 "温度" 输入框（0-1，step=0.05），规划默认 0.6、写作默认 0.7，持久化到 config.json
- **LLM 客户端 temperature 参数**：`LLMClient.__init__` 加 `temperature`，`chat`/`chat_detailed`/`_build_payload` 默认值改为 `None`（走 `self.temperature`）
- **模型下拉框始终显示已保存的模型**：`refreshModels` 接受 `savedValue` 参数，配置模型不在 API 返回列表时追加 `xxx（已配置）` option
- **RAG 停止按钮**：配置页新增 "停止 RAG" 按钮 → 后端 `_handle_rag_stop` → `taskkill /F /T` 杀进程树 + `netstat` 查 8767 + 等端口释放 + auto-restart 检测
- **RAG 停止后不再显示"运行中"**：`_ragManuallyStopped` 标记阻止轮询跳回运行中状态，直到用户手动点击"冷启动 RAG"
- **RAG 状态轮询加速**：cache-buster 防缓存，间隔 3s→1.5s，启动后立即查一次

### 变更
- **自检从额外 LLM 调用改为内嵌标记**：删除 `FACT_CHECK_PROMPT` 和独立 `SELF_CHECK_SYSTEM_PROMPT`，改为在写作 prompt 末尾追加 `【事实待核查】` 标注要求，response 里直接解析
- **规划器 `max_tokens` 从配置读**：删除硬编码 4096，改用 `max(4096, llm_client.max_tokens)`
- **写作器/规划器 LLM 客户端统一工厂**：`_create_writer_client()` / `_create_planner_client()` 传 `temperature`
- **Planner/writer temperature 硬编码删除**：`planner.py` `temperature=0.6` → `None`；`writer.py` `temperature=0.7` → `None`（走客户端配置）
- **`status_text` 仅 writing 阶段返回**：`get_progress()` 非 writing 阶段返回空字符串，防止加载旧会话显示脏数据
- **状态文本生成时自动清空**：`_handle_generate` 入口调用 `set_status_text("")`
- **配置页提示文案更新**：改为 "推理模型建议不低于 4096（默认最低值），长文建议 8192 以上"

### 修复
- `planner.py` 硬编码 `max_tokens=4096` 导致推理模型 thinking 吃掉全部 token → JSON 输出为空
- `config_manager.py` `update()` 无法写入新增配置键 → `fact_check_enabled` 等不持久化
- `config_manager.py` `load()` 不合并 DEFAULT_CONFIG 缺失项 → 旧 config.json 没有新字段
- 自检 `max_tokens` 各值（2048/8192/512）导致推理模型 thinking 吃光 → 改为 `None`（走配置的 81920）
- 自检使用独立 system prompt → LLM 混淆角色 → 改为共享 `WRITER_SYSTEM_PROMPT`
- 自检额外 LLM 调用导致额外 token 消耗 → 改为内嵌标记法，零额外调用
- 加载旧会话时 `_status_text` 脏数据被轮询读出并显示
- 章节字数 input 可编辑但子结构字数不变 → 数据不一致
- 子结构取消勾选后章节字数不减 → 重算函数忽略未勾选
- 模型下拉框加载时显示"(请选择)"而非已保存模型 → `refreshModels` 接受 `savedValue` 回退
- RAG 冷启动后无法关闭 → 新增停止按钮 + 后端进程树 kill + 端口释放等待
- RAG 停止后轮询仍跳回"运行中" → `_ragManuallyStopped` 标记保护
- RAG 状态检测被浏览器缓存 → 加 `?_=Date.now()` cache-buster

---

## [0.9.0] - 2026-07-27
### 新增
- **事实自检系统上线**：配置页开关 → 每子结构写后自检 → 文章末尾汇总置信度分级列表
- **停止生成**：延时停止 / 立即停止，保留已写内容

### 变更
- 自检系统 Prompt 统一为 `WRITER_SYSTEM_PROMPT`

---

## [0.8.0] - 2026-07-27
### 新增
- **会话归档/恢复/删除**：侧边栏 UI + `/api/session/archive|restore|delete`
- **自动会话限额**：`max_sessions=20`，超出自动归档最旧非活跃会话
- **批量自动撰写**：后端 `/api/batch_auto` + 前端批量进度轮询
- **单篇自动撰写**：前端 chain 按钮，全量自动 RAG

### 变更
- 写作器/规划器 LLM 客户端工厂抽取，消除重复代码
- 配置项默认值系统：`load()` 合并 DEFAULT_CONFIG

---

## [0.7.0] - 2026-07-27
### 新增
- **子结构字数可编辑**：章节字数改为子结构实时的和
- **RAG 离线禁用** + **辅助知识模态框**（文本/文件上传）
- **前文回顾字数可配置**
- **RAG／辅助知识 Prompt 分离注入**

### 修复
- `_status_text` 脏数据跨会话显示 → 仅 writing 阶段返回
- 配置新增项不持久化 → `update()` 支持写新键
- `planner.py` `max_tokens=4096` 硬编码 → 从配置读 + 保底 4096
- LLM 模型名为空时调 `list_models()` 自动填充
- 自检 `max_tokens=2048` → 512（子结构级）降低推理 thinking 挤压

---

## [0.6.0] - 2026-07-27
### 新增
- **子结构字数输入框** + **章节字数自动求和**
- **RAG 复选框离线 disabled** + **在线同步 KB 下拉**
- **辅助知识模态框**（文本 + .txt/.md 上传）
- **前文回顾字数配置化**

---

## [0.5.0] - 2026-07-26
### 新增
- **会话归档/恢复/删除 UI**
- **自动清理旧会话**（max_sessions 限制）
- **大纲过滤同步进度**：取消勾选的子结构不计入进度分母

---

## [0.4.0] - 2026-07-26
### 新增
- **自动撰写入口**：发送区 "自动撰写" 按钮（单篇 chain / 批量提交）
- **全量自动 RAG**：8767 在线时所有子结构自动启用

---

## [0.3.0] - 2026-07-26
### 新增
- **日志系统**：串行写作状态持久化 `_status_text`
- **子结构写作要点显示**
- **蓝图文档**：`blueprint.json`

---

## [0.2.5b4] - 2026-07-26
### 修复
- PyPI long_description 缺失更新日志

## [0.2.5b3] - 2026-07-26
### 新增
- PyPI 发布准备：目录改名、LICENSE、README、blueprint.json
- GitHub Actions 检测支持

## [0.2.5b2] - 2026-07-26
### 新增
- 两级 RAG 查询、实时状态文本、子结构 summary 显示
- `state_manager.set_status_text()`

## [0.2.5b1] - 2026-07-26
### 新增
- RAG 知识库对接、冷启动、KB 下拉联动
- 提示词模板系统（5 套模板）
- 子结构系统、大纲勾选/取消、双级排序
- 续写机制（finish_reason=length 自动续写）
- LLM 客户端 `chat_detailed()`、max_tokens 从 config 传入

### 变更
- 端口 8770、LLMClient 存储 max_tokens

## [0.1.0] - 2026-07-26
### 新增
- 项目骨架、LLM 统一客户端、会话管理、大纲规划器、串行写作器
- 异步生成 + 进度轮询、会话恢复、setup.bat
