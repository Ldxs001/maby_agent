# 更新日志 / CHANGELOG

## 0.5.0b1 — 架构重构：8 方式 → 5 方式（按逻辑分类，软引导为第一位基础原子）

### 背景
用户指出原 8 种方式是从工程实例硬凑的，不是从前置规范逻辑分类来的：gate（关键词精确匹配）和 slot（槽位范围）逻辑上都是从一句话分类/提取，带正则的关键词也是槽位；diverge 的纠偏是语义偏离拉回不是格式校验；软引导（任务提示词）是第一位原子所有方式建立在它之上不是平行的一种。要求重新规划成 5 种，加验证方案量化每种后置是否真的生效。

### 改动
- **5 种方式（按逻辑分类）**：
  1. `pure_guide` 纯软引导（只 task_prompt，可加输出约束校验）
  2. `value_bound` 值域限定（gate/slot/required_min/condense 合并，`bound_type` 区分：enum_select/slot_extract/required_min/condense_enum）
  3. `diverge_correct` 发散纠偏（高温度发散+代码确定性纠偏，语义偏离拉回，非格式校验）
  4. `deterministic_pin` 确定性封死（代码钉死可枚举，A 形态错误无通道）
  5. `detect_report` 检出上报（不可枚举检出+上报，不阻塞，B 形态）
  + `custom` 自定义组合（A 与 B 互斥，其余任意组合）
- **软引导提升为第一位基础原子**：task_prompt 从方式附属→所有方式必有的基础配置（WAY_HELPS 说明"软引导=第一位基础原子"）
- **value_bound 合并**：gate/slot/required_min/condense 四种合并成一种，`bound_type` 下拉区分值域类型（可枚举选择/槽位提取/必填最小化/凝练+枚举过滤）。`recipe_for(way_id, cfg)` 根据 bound_type 动态返回子 recipe
- **验证指标落地**（`calc_metrics`，跨 run 聚合，量化每种后置是否真的生效）：
  - pure_guide：达标比例 + 重复性
  - value_bound：值域命中率 + 编造检出率 + 重试回值域率 + 重复性
  - diverge_correct：changed 比例 + **纠偏编辑距离**（Levenshtein raw→corrected）+ **纠偏有效性**（raw不达标且corrected达标比例）+ 达标比例 + 重复性
  - deterministic_pin：changed 比例 + 达标率 + **多次 100% 完全一致**（代码零采样）+ 重复性
  - detect_report：检出率 + 上报率 + 重复性
- **Levenshtein 编辑距离**：`levenshtein(a,b)` 量化纠偏改了多少字符
- **diverge_correct 示例**：correction_target 设非空（required_pattern=生物|海洋|深海|发光），展示纠偏约束效果
- **UI**：方式下拉用新 5 种；value_bound 表单 bound_type 下拉+子表单动态切换（change 事件）；e2e 结果展示加验证指标区块
- **WAY_HELPS 重写**：5 种方式说明 + 组合规则（A 与 B 互斥）+ 验证指标说明
- 验证：py_compile 全通过；import 测试通过；recipe_for 按 bound_type 正确返回子 recipe；levenshtein(kitten,sitting)=3

## 0.4.0b3 — diverge/deterministic/detect_report 泛化（去照搬工程实例，回归泛化理论）

### 背景
diverge 照搬 novel-weaver 引用标记实例（假设 LLM 造【引用自来源】+删标记），但 LLM 无理由产生该标记，纠偏空转；deterministic demo 照搬 Structured Writer 引用编号实例（输入塞标记但 LLM 生成新内容不复制）；detect_report 的"全 unmatched 判失败"违反 B 形态"上报器不阻塞生成通道"。三者都把工程实例当实现照搬，而非泛化理论。用户澄清：前置规范内部的校验（correction_target/pin_target）属于前置规范，不是任务完成后的全量后置验证，保留不违反理论。

### 改动
- **diverge（08c 场景三 泛化）**：放开+收紧配对=误差抵消。validate_diverge 加空响应判失败；correction_target 保留（前置规范内部校验纠偏达标，留空=只观测 changed）；retry=True（前置规范内部重试）。default_config 去掉默认删【引用标记】规则（regex_replaces=[]），用 normalize_blanklines 泛化收紧。WAY_HELPS 泛化重写（不绑死引用标记，说明用户针对自己场景配纠偏规则）
- **deterministic（08a §7 A 形态 泛化）**：生成时封死可枚举值域。validate_deterministic 加空响应判失败；pin_target 保留（前置规范内部校验钉死达标，留空=纯 A 钉死观测 changed）。default_config 去掉默认删【引用标记】+renumber_source=False（不照搬引用编号）。DEMO_INPUTS 去掉【引用自来源】标记改成主题。WAY_HELPS 泛化重写（不绑死引用编号，说明用户有编号场景才开 renumber）
- **detect_report（08a §7 B 形态 泛化）**：上报器不阻塞生成通道。validate_detect_report 去掉"全 unmatched 判失败"——有检出=success（哪怕全 unmatched 也是"全部需上报"+人工兜底，不阻塞）；只判空响应/无检出失败（检出器无效）。WAY_HELPS 明确"上报器不是验证器，不宣称没问题，只上报"
- **ATOM_GLOSS/ATOM_AXES**：diverge note 改"放开+收紧·误差抵消"，detect_report 说明改"有检出=success不阻塞"
- 验证：mock chat 跑通——detect_report 全 unmatched 现在 success；diverge/deterministic 空响应失败、泛化默认空规则+normalize；DEMO_INPUTS 无【引用自来源】标记；import 无警告

## 0.4.0b2 — custom 暴露新校验原子 + 一键演示约束示例 + WAY_HELPS 填写示例

### 改动
- **custom 下拉补新校验**：validate 下拉原只有 none/in_set/no_extra/required_full/in_range/eq_exact，补进 guide/diverge/detect_report/deterministic 四个新校验原子，custom 用户可自由组合
- **ATOM_GLOSS/ATOM_AXES**：加 guide（软引导·输出约束）/diverge（纠偏目标校验）说明，更新 deterministic/detect_report 说明兼顾后处理+校验
- **一键演示约束示例**：原 run_e2e_demo 用 default_config，新约束字段全空（向后兼容）导致一键看不到约束效果。新增 demo_config(way_id) 给 guide（必含"软件"+限长300）/diverge（禁含【）/deterministic（格式含来源1）/detect_report（合法值=55.8万亿元,42.8%,10.9亿人）设非空示例，run_e2e_demo 改用 demo_config
- **WAY_HELPS 填写示例**：8 种方式各加"示例"段（示例输入+配置+预期），降低填写门槛，尤其新约束（output_constraints/correction_target/pin_target）用户知道填什么
- 验证：import 无警告（-W error），demo_config 输出正确，8 方式示例齐全

## 0.4.0b1 — 四种 validate=none 方式补可配置约束（guide/diverge/deterministic/detect_report）

### 背景
guide/diverge/detect_report/deterministic 四种原 validate=none，没有可配置约束，什么都通过，用户无法设置门禁/验证来测试场景。按理论（08a §7 三态谱系 / 08b 面对面弱约束 / 08c 场景三 novel-weaver）给这四种加可配置的门禁/验证约束。

### 改动
- **guide（08b 软引导）**：加 `output_constraints`（required_keywords/forbidden_keywords/max_length/format_regex），校验续写是否满足约束，不满足重试。约束全空=纯软引导不校验（向后兼容）
- **diverge（08c 场景三 发散+纠偏）**：加 `correction_target`（format_regex/required_pattern/forbidden_pattern），校验纠偏后 corrected 是否达标，不达标重试。目标全空=不校验纠偏（向后兼容）。retry 改 True（纠偏不达标可重试）
- **deterministic（08a §7 A 形态 封死）**：加 `pin_target`（exact_value/format_regex），校验钉死后 corrected 是否满足封死目标。目标全空=只钉死不比对（向后兼容）
- **detect_report（08a §7 B 形态 上报）**：修 `detect_and_report`——allowed 为空时所有检出项 unmatched=True（原 `bool(allowed) and ...` 导致 allowed 空时永不 unmatched）；加 `validate_detect_report`——空响应/无检出/全部需上报判失败，有命中且有合法判成功（上报不阻塞）
- **WAY_RECIPES**：guide→validate=guide/retry=True，diverge→validate=diverge/retry=True，deterministic→validate=deterministic，detect_report→validate=detect_report
- **VALIDATORS**：新增 validate_guide/validate_diverge/validate_deterministic/validate_detect_report
- **UI**：renderConfigForm + collectConfig 给 guide/diverge/deterministic 加约束配置项（detect_report 已有 allowed_values，校验逻辑改即可）
- **default_config + WAY_HELPS**：更新说明，明确可配约束及留空=不校验
- 验证：mock chat 跑 18 用例全通过——四种带约束能判失败/成功，约束留空向后兼容

## 0.3.2b4 — 一键演示改实验级并行（与正常运行一致）

### 改动
- **一键演示并行模型修正**：原 e2e_demo 是"方式间串行 + 方式内串行重复 N 次"（纯串行，parallel 参数名不副实），改成与正常运行一致的**实验级并行**——parallel 个管道并发，每管道内方式串行（各方式用预设输入），收齐按方式聚合算重现性
  - 新增 _aggregate(way_specs, pipes)：按方式聚合各管道结果，跳过未完成管道(None)
  - run_e2e_demo 用 ThreadPoolExecutor 并发跑 N 管道，as_completed 收齐，每管道完成调 on_progress(done_pipes, total_pipes, 聚合快照)
  - on_progress 签名变更：res 从"单方式结果"改为"全部方式聚合列表"，web/main 适配（web 替换不累加，main 打印管道进度）
- **正常运行不动**：ExperimentRunner 已是实验级并行（N 管道各跑所有方式，方式间交错），用户认可效率高
- 验证：gate parallel=2 跑通，2 管道并发 45s（串行会 60-80s），runs=2 run_ids=[1,2] consistency=1.0

## 0.3.2b3 — 结果落盘 + 右侧历史边栏（保存/复看/删除/清空）

### 改动
- **结果自动落盘**：每次正常运行 / 一键演示完成后，结果写入 `data/results/{时间戳}_{类型}.json`（含 type/saved_at/summary/input/result），时间戳精确到微秒保证唯一递增
- **右侧历史边栏**（结果 tab，参考 structured-writer outputs-sidebar）：
  - 结果 tab 改 flex 布局：左结果展示区 + 右 240px 历史边栏（sticky）
  - 边栏列表每条：类型徽章（运行/演示）+ 摘要 + 时间 + 删除✕
  - 点条目 → fetch /api/results/read → 按 type 调 renderResult / renderE2E 重新展示（反复看）
  - 删除：二次确认（✕→确认?→取消），fetch /api/results/delete
  - 清空：顶部「清空」按钮，confirmModal 确认后 fetch /api/results/clear
- **后端 API**：GET /api/results（列表，按时间逆序）/ GET /api/results/read?id= / POST /api/results/delete / POST /api/results/clear
- **自动刷新**：运行/演示完成回调里调 loadHistory() 刷新边栏；启动时 loadHistory()
- 验证：辅助函数 _save_result/_list_results/_read_result/_delete_result 全通过（保存/逆序/读取/删除）

## 0.3.2b2 — 正常运行统一详细报告 + 一键演示并行数可设

### 改动
- **正常运行报告对齐 e2e 详细度**（用户要求统一）：
  - pipeline_model.WayResult 加 calls/total_tokens/elapsed_total 字段
  - simulator.ExperimentRunner 复用 e2e_demo._exec_with_trace + _make_chat：每次 attempt 记 retry_reason/raw/filled/fabricated/missing_required/flagged，每次 LLM 调用记 prompt/system_prompt/response/elapsed/prompt_tokens/response_tokens
  - web_ui renderResult 重写：每方式子块含 LLM 调用记录 + attempt 重试理由高亮 + token + 耗时 + 最终填入 + 观测，与 renderE2E 同款展示
- **一键演示并行数可设**（原来写死 3）：
  - 前端按钮旁加「每方式并行」输入框（id=e2e-parallel，默认 3）
  - 后端 /api/e2e_demo 读 body.parallel 传给 _e2e_task → run_e2e_demo(parallel=...)
- **WorkBuddy 同步路径收窄**：只同步 C:\Users\sm001\WorkBuddy\silprespec-emulator（maby_agent 那个是仓库用的，不再覆盖）
- 验证：gate 正常运行 parallel=2 跑通，WayResult 含 calls/total_tokens/elapsed_total + attempts 含 retry_reason，6 项信息全覆盖

## 0.3.2b1 — 端到端演示加并行重现性：每方式跑 N 次 + 重试理由 + token 估算

### 改动
- **e2e_demo.py 重写**：run_e2e_demo 加 parallel 参数（每方式跑 N 次），返回结构从扁平改为 {runs, reproducibility}
  - _exec_with_trace：记录每次 attempt 的完整 trace（valid/retry_reason/raw/filled/fabricated/missing_required/flagged）
  - _make_chat：包装 LLM 调用，记录 prompt_tokens/response_tokens（粗估 1.5 token/字）+ elapsed
  - 重现性：Counter 统计各次 filled，算 consistency（最多见填入占比）+ distinct_fills + fill_counts
  - 每方式汇总：success_all/total_tokens_all/elapsed_all + reproducibility
- **web_ui renderE2E 适配新结构**：每方式一个卡片，顶部汇总（并行/总耗时/总tokens/consistency），中部各次运行子块（run_id/success/撑满/重试/耗时/tokens + LLM调用 + attempt含重试理由 + filled/extra），底部重现性（不同填入列表）
- **main.py run_e2e_cli 适配新结构**：终端输出各次运行 + attempt 重试理由 + 重现性统计
- **验证**：gate parallel=2 端到端跑通，consistency=1.0（两次都填"积极"），6 项信息全覆盖（输入/提示词配置/耗时token/重试理由内容/撑满成功输出/并行重现性）

## 0.3.2b0 — 一键端到端演示：8 方式 × 预设输入 × 真实 LLM × 完整原始信息

### 改动
- **新增 e2e_demo.py 模块**：8 方式预设输入（DEMO_INPUTS）+ run_e2e_demo(llm, ways, on_progress)
  - 预设输入：每个方式配一个能体现该方式特性的输入（gate=情绪句/guide=技术段/condense=环境治理长文/slot=新闻/diverge=主题/deterministic=带来源编号/detect_report=含数值统计/required_min=查询问句）
  - 返回每个方式的完整原始信息：配置(recipe/config/task_prompt/max_retry/user_input) + 每次 LLM 调用(system/prompt/max_tokens/temperature/原始返回/耗时) + attempt 记录 + 最终结果(success/retry_count/exhausted/filled/观测extra/error)
  - 进度回调 on_progress(done, total, res)：每跑完一个方式通知，供 web 异步展示
- **web_ui 加一键端到端演示**：
  - 后端 POST /api/e2e_demo：启动后台线程跑 run_e2e_demo，复用 _run_tasks 进度机制
  - 后端 _e2e_task：创建带 timeout/max_tokens 的 LLMClient（避免默认 180s 超时），on_progress 实时更新 task result/progress
  - 前端运行 tab 加「一键端到端演示」按钮 + 进度条
  - 前端结果 tab 加「端到端演示结果」展示区（renderE2E）：每方式一个卡片，含配置/LLM调用记录/attempt/最终结果/观测
  - 轮询 /api/run/status 实时展示已完成方式的结果（边跑边出）
- **main.py 加 --e2e 命令行入口**：python main.py --e2e 跑 8 方式端到端，终端输出完整原始信息
- 用途：证明能跑通 + 预设输入省心 + 完整输入到输出端到端信息供有限实证 + 智能体内一键展示

## 0.3.1b0 — 阶段化 UI：按 5 阶段 + 轴标注 + 原子名词

### 改动
- **方式卡片按 5 阶段分组展示**（替代原来的 config + recipe 平铺）
  - ① 生成：LLM 怎么填（text/select/slot）+ 轴标注
  - ② 后处理：代码怎么加工（deterministic/enum_filter/detect_report/json_parse）+ 轴标注
  - ③ 校验：代码怎么判合规（in_set/no_extra/required_full/in_range/eq_exact/none）+ 轴标注
  - ④ 重试：不合格怎么办（retry bool）
  - ⑤ 观测：记录什么（hit/fabricated/extra_keys/left_empty/flagged/changed）
- **原子→轴映射**（ATOM_AXES 常量）：每个原子标注对应的轴（格式轴/集合轴/数值轴/内容轴）+ 理论形态（A 封死 / B 检出即上报）
  - 格式轴=可枚举→代码封死（A 形态）；集合轴=可枚举→代码校验（A 形态）；数值轴=不可枚举但可收窄→校验或检出上报；内容轴=不可枚举且不可收窄→无法前置封死
  - 对应理论 08a §7 三态谱系 + 09b §3.3 穷举边界
- **预置方式也显示阶段**（只读）：用户能看到 gate 背后跑了 select+in_set，diverge 背后跑了 text+deterministic
- **custom 方式阶段可编辑**：原子下拉/复选融入阶段区块，替代原独立 recipe 表单
- 后处理/观测改用 checkbox 替代 select-multiple，更直观
- 删除死代码 renderRecipeForm（已被 renderStages 替代）
- help 移至卡片顶部（说明在前，配置在后）
- 新增 CSS：.stages-area / .stage-row / .stage-label / .stage-body / .atom-sel / .atom-readonly / .config-header
- **原子名词解释**：
  - custom help 加"原子名词"表，解释全部 19 个原子（3 生成+4 后处理+6 校验+6 观测）各自干啥
  - UI 阶段区块每个原子加 title tooltip（悬停显示一句话名词解释）
  - ATOM_GLOSS JS 常量 + optT/ro 辅助函数给 option/span/label 加 title

### 验证
- py_compile 全模块通过（-W error 无警告）
- HTML 含 stages-area/renderStages/ATOM_AXES/ATOM_GLOSS/axisTag/stage-row/config-header/r_pp_/r_ob_；无残留 recipe-block/renderRecipeForm

## 0.3.0b0 — 方式说明改成表单语言 + custom 方式 help

### 改动
- **WAY_HELPS 全部 8 种方式的说明从 JSON 描述改为表单描述**
  - 旧："示例：{json}\n\n字段：- xxx: 说明" → 新："表单：\n- 控件名：控件类型 + 说明"
  - gate：门禁行 + 允许未指定勾选
  - guide：引导提示词文本框
  - condense：凝练规则文本框 + 枚举词逗号输入
  - slot：槽位行动态增删 + 注（不强制检查必填）
  - diverge：发散提示词 + 替换规则行动态增删 + 空行归一化勾选
  - deterministic：替换规则行 + 编号重排勾选 + 空行归一化勾选
  - detect_report：检出正则输入框 + 合法值逗号 + 上报标签（raw string 避免 \d 转义警告）
  - required_min：槽位行 + 必填勾选说明
- 说明与 UI 表单控件一一对应，用户看说明即知怎么填
- **新增 custom 方式 help**（WAY_HELPS["custom"]）
  - 解释 recipe 表单和 config JSON 的配合关系（选了什么原子 → 需要填什么字段）
  - 列出 config JSON 全部 12 个可用字段及含义、哪个原子用
  - 后端 `/api/ways` 返回 `custom_help`；前端 `customHelp` 全局变量，custom 方式时显示此 help

### 验证
- py_compile 通过（-W error 无警告）；8 个预置方式 help 均以"表单："开头；custom help 含"config JSON 全部可用字段"参考

## 0.2.9b0 — 全站不暴露 JSON：recipe 表单 + 8 套 config 表单

### 改动
- **recipe 表单**（自定义模板）：原子配方 JSON textarea → 结构化表单，从有限原子集下拉/多选
  - 生成：下拉 text/select/slot；槽位参数：下拉 extra_check/required_min/无
  - 后处理：多选 deterministic/enum_filter/detect_report/json_parse
  - 校验：下拉 in_set/no_extra/required_full/in_range/eq_exact/none（标注点对面/面对面/点对点）
  - 重试：勾选；观测：多选 hit/fabricated/extra_keys/left_empty/flagged/changed
  - `renderRecipeForm`/`collectRecipe` 表单↔对象双向同步，不可能填出不存在的原子
- **8 套 config 表单**（预置方式）：配置 JSON textarea → 方式专属表单
  - gate：门禁行（维度名+候选词逗号+删）动态增删 + 允许未指定勾选
  - guide：引导提示词文本框
  - condense：凝练规则文本框 + 枚举词逗号输入
  - slot/required_min：槽位行（名+必填勾选）动态增删
  - diverge：发散提示词 + 替换规则行（pattern+replace）动态增删 + 空行归一化勾选
  - deterministic：替换规则行 + 编号重排勾选 + 空行归一化勾选
  - detect_report：检出正则 + 合法值逗号 + 上报标签
  - custom：仍用配置 JSON textarea（高级，schema 不固定）
  - `renderConfigForm`/`collectConfig` 按方式分支，字段名与 atoms.py 期望一致
- 动态行增删用事件委托（card click → add-gate/add-slot/add-replace/del-row）
- 方式切换时按 default_config/default_recipe 重新渲染对应表单
- `collectExp`/`saveAsTemplate` 改用 `collectConfig`/`collectRecipe` 从表单收集

### 验证
- py_compile 通过；HTML 含 renderRecipeForm/collectRecipe/renderConfigForm/collectConfig/config-area/r_generate/r_validate/r_postprocess/add-gate/add-slot/add-replace/del-row；旧 recipe textarea placeholder 和"原子配方JSON"label 已删
- 字段名核对：表单收集的 config 字段与 atoms.py（gen_text/gen_select/gen_slot/apply_deterministic/validate_*）期望全部一致

## 0.2.8b0 — 自动落盘，去掉两个保存按钮

### 改动
- **自动落盘**：去掉"保存后端配置"和最下方"保存"两个按钮，改完即存
  - LLM 后端字段（backend/base_url/model/timeout/max_tokens/temperature）change/blur → `saveLLMAuto()` → POST `/api/config` → 写 `config.json`
  - 实验字段（名称/说明/并行数/方式卡片任何字段）blur/change → `saveExpAuto()`（debounce 500ms）→ POST `/api/experiment` → 写 `experiment.json`
  - 方式卡片用事件委托：`#ways-list` 监听 `change`+`focusout`，增删卡片也触发保存
- **topbar 自动保存状态**：`● 已就绪 / 保存中… / 编辑中… / 已保存 HH:MM:SS`，全局可见
- 最下方 section 改为"配置改动自动保存"说明 + 重置按钮

### 验证
- py_compile 通过；HTML 含 `autosave-status`/`saveLLMAuto`/`saveExpAuto`/`focusout` 监听；旧 `btn-save-llm`/`btn-save`/`save-status` 已删

## 0.2.7b0 — 方式说明加"适用场景 + 输入类型"

### 改动
- 8 个方式的 `WAY_HELPS` 把"适用："扩充为"适用场景 + 输入类型"两行，明确告诉用户运行时应提供什么输入：
  - gate：自然语言短文本（一句话/评论），归入有限候选词
  - guide：自然语言文本（开放内容，摘要/续写/改写）
  - condense：自然语言长文本（文章/段落），输出落枚举词集
  - slot：自然语言文本（含实体/事件的事实描述，如新闻/履历）
  - diverge：自然语言主题/提示词（创意生成，故事/文案/扩写）
  - deterministic：自然语言文本（含来源编号/引用标记/多空行等格式噪声）
  - detect_report：自然语言文本（含数值/事实陈述，如报告/新闻/统计描述）
  - required_min：自然语言文本（含可提取字段的事实描述，部分字段可能缺失）

## 0.2.6b0 — 下拉框深色统一

### 改动
- 全局 `select{background:var(--bg-input);color:var(--text)}`：兜底所有未被 `.form-row select` 覆盖的 select（如方式卡片 `.wc-head` 内的 way 下拉），下拉框本身不再白底

## 0.2.5b0 — 删冗余 coord + validate 原子承载空坐标（in_range/eq_exact）

### 改动
- **删除 `coord` 字段**：点对点/点对面/面对面原本是冗余且有害的自由下拉（能乱配出门禁配面对面这种无意义组合），现已删除。空坐标形态改由 `validate` 原子承载：
  - `in_set` = 点对面（集合成员，门禁/凝练用）
  - `in_range`（新增）= 面对面（区间容差，数值 ∈ [lo,hi]）
  - `eq_exact`（新增）= 点对点（严格相等）
  - `none` = 不校验
- **新增 validate 原子**：
  - `validate_in_range`：读 `cfg.range_checks=[{field,lo,hi}]`，数值提取支持 `95%`/`3.5亿` 等，越界或非数值判失败
  - `validate_eq_exact`：读 `cfg.exact_checks=[{field,value}]`，字符串严格相等
  - `VALIDATORS` 注册 in_range/eq_exact；`_to_number` 辅助函数
- 清理：`pipeline_model.py` 删 `COORDS`/`WayConfig.coord`；`web_ui.py` 删 coord 下拉/coordsMeta/coords 返回；`atoms.py` 顶部注释更新

### 验证
- py_compile 通过；无遗留 coord/COORDS/coordsMeta
- `_to_number`：`95%`→95、`3.5亿`→3.5、`无数值`→None
- `in_range`：97%∈[95,100]→valid；80%→invalid offset=`80.0∉[95,100]`；非数值→invalid
- `eq_exact`：`1,2,3`==`1,2,3`→valid；`1,2,4`→invalid offset=`1,2,4≠1,2,3`

## 0.2.4b0 — 模态框统一 + 预置模板另存为 + 下拉深色 + 自定义无默认提示词

### 改动
- **模态框替代弹窗**：所有 `alert`/`prompt`/`confirm` 改为统一模态框（对齐 structured-writer 深色风格：overlay+box+header+body+footer），用于模板命名/删除确认/重置确认/输入校验提示
- **下拉深色选项**：`select option` 加 `background:var(--bg-input);color:var(--text)`，下拉列表不再白底
- **预置模板另存为**：预置模板本身只读不可改存；改后点"另存为模板"转成自定义模板（带上预置 `default_recipe`）
  - `/api/ways` 返回每个预置方式的 `default_recipe`（`WAY_RECIPES[way].to_dict()`）
  - "另存为模板"按钮对所有方式显示；"更新模板"按钮仅对已保存自定义模板显示
  - 另存为成功后卡片转 `way=custom` + `template_id`，可继续"更新模板"
- **自定义无默认任务提示词**：选"自定义模板（临时）"时任务提示词清空（不残留预置值）；保存的模板用模板自带 task_prompt

### 验证
- py_compile 通过；无遗留 alert/prompt/confirm
- `/api/ways` 返回 `default_recipe`（gate: generate=select/validate=in_set）
- 预置 gate 另存为自定义模板：POST ok，recipe 正确带入

## 0.2.3b0 — 自定义模板库 + 任务提示词默认常显

### 改动
- **自定义模板库**：自定义模板可**保存（命名）/另存为/删除，多个**，持久化到 `config.json` 的 `custom_templates`
  - `ConfigManager` 加 `get/save/delete_custom_templates` 方法
  - 后端 `GET /api/ways` 返回 `custom_templates`；`POST /api/custom_templates` 保存（有 id 更新、无 id 新建）；`DELETE /api/custom_templates?id=` 删除
  - 前端：方式下拉列出保存的模板（★ 前缀）；"存为模板"/"另存为"按钮（仅 custom 时显示）；配置 Tab 加"★ 自定义模板库"区（加载到新卡片/删除）
  - `WayConfig` 加 `template_id`（仅 UI 标记；执行仍用 `way=custom` + 内联 `recipe`，删除模板不影响已存实验）
  - 选保存模板时 recipe/task_prompt/config 从模板填入卡片；存为模板时卡片配方/提示词/配置写入模板
- **任务提示词默认常显**：textarea 永远显示默认值（`w.task_prompt || meta.default_task_prompt`），不再留空+placeholder fallback

### 验证
- ConfigManager CRUD：保存/更新（同名 id）/删除均通过
- HTTP 端点：`/api/ways` 含 `custom_templates`；POST 创建→count=1；DELETE→count=0

## 0.2.2b0 — UI 自定义模板（自由组合原子）

### 改动
- 方式卡片 way 下拉加"自定义模板"选项
- 选"自定义模板"时显示**原子配方JSON编辑器**：用户可自由组合生成/后处理/校验/重试/观测原子成自定义模板
- `WayConfig` 加 `recipe` 字段（自定义原子配方 dict，空则用 `WAY_RECIPES[way]`）
- `Recipe` 加 `from_dict`；`exec_recipe` 优先用 `wc.recipe` 构造自定义配方
- `_filled_for`/`_attempt_for` 支持 `way_id="custom"`：按 recipe 推断 filled/attempt 格式（不再按 way_id 分支）
- 切换方式时自动显示/隐藏配方编辑器

### 验证
- 自定义 gate 配方（select+in_set+retry+hit）→ filled={'情绪':'积极'} 命中
- 自定义 生成+钉死配方（text+deterministic+changed）→ filled={raw,corrected} 正确

## 0.2.1b0 — 任务提示词 + 连接修复 + max_tokens 配置推动

### 改动
- **任务提示词（系统提示词）**：每个预置模板配泛化任务提示词（`TASK_PROMPTS`），作为 system message 传给 LLM，让 LLM 知道要完成什么任务再填空
- `WayConfig` 加 `task_prompt` 字段；空则 fallback 到 `TASK_PROMPTS[way]`；用户可在 UI 编辑覆盖
- 切换方式时 UI 自动填入该方式默认任务提示词
- **连接修复**：`llm_client.py` 从 urllib 改用 `http.client`（基于 socket），绕过 Windows IE 代理把 localhost 也代理导致 `WinError 10051` 的问题
- **max_tokens 配置推动**：生成原子不再硬编码 max_tokens（32/64/256），从方式配置读，None 则用全局 config（4096）；修复思考模型 token 不够 content 空的问题
- 不禁用思考模型，原样测试（实验台要观测模型真实行为）

### 端到端验证
- gate 端到端通：输入"我今天很开心" → LLM 真填空选"积极"命中

## 0.2.0b0 — 原子化重构（执行层）

### 改动
- 新增 `atoms.py`：10 个前置规范原子（生成3 + 后处理4 + 校验1可配 + 控制流1 + 观测1可配）
- 8 种方式声明式表达为原子配方（`WAY_RECIPES`），执行逻辑不再按方式 id 分支
- `simulator.py` 的 8 个 `_way_xxx` 方法和 3 个辅助函数移入 `atoms.py`，`_exec_way` 改走 `exec_recipe`
- 执行层完全原子化（无 way_id 分支）；filled/extra 展示格式由兼容函数保持 UI 不变
- 行为不变：8 方式的 filled/extra/attempts 输出与 0.1.x 一致（冒烟测试验证）

### 原子清单
- 生成：text / select / slot
- 后处理：deterministic / enum_filter / detect_report / json_parse
- 校验：in_set / no_extra / required_full / none
- 控制流：retry 循环（exec_recipe 编排）
- 观测：hit / fabricated / extra_keys / left_empty / flagged / changed

### 下一步
- 开 UI 让用户自由组合原子成自定义模板（第二步）

## 0.1.6b0 — 并行数统一到运行Tab

### 改动
- 去掉配置Tab的并行数输入（与运行Tab重复）
- 并行数唯一来源为运行Tab的"并行数"输入框
- 加载实验时把 experiment.parallel 同步到运行Tab的并行数

## 0.1.5b0 — 每种方式加名词解释

### 改动
- 8 种方式 help 开头加"名词："解释核心概念（门禁/槽位/凝练/发散/钉死/检出/required 等）
- slot 明确说明与门禁的区别：门禁是从穷举词中"选"，槽位是从文本中"提取"
- required_min 明确说明与 slot 的互补：slot 查"多"，required_min 查"少"

## 0.1.4b0 — 配置跟随方式切换 + 结果显示每次尝试 + help补充检测/缺陷

### 改动
- 切换方式时配置 JSON 自动填入该方式 default_config（不再保留旧方式配置）
- 结果 Tab：每次尝试展开显示"偏移方向"（如 情绪=积极 · 时态=现在），不再只有结论
- 每种方式 help 补充"检测什么/适用/缺陷"

## 0.1.3b0 — 方式说明+示例 + ⑤⑥⑦配置真正控制行为

### 新增
- `WAY_HELPS`：8 种方式各配说明+JSON示例+字段含义+观测指标
- UI 配置 Tab 每种方式卡片下方可折叠"📖 说明+示例"，切换方式自动更新
- `/api/ways` 返回 `help` 字段

### 修复
- ⑤ diverge：`correct_rule` 描述字段→`regex_replaces`+`normalize_blanklines`，代码按配置执行
- ⑥ deterministic：`post_rule` 描述字段→`regex_replaces`+`renumber_source`+`normalize_blanklines`，代码按配置执行
- ⑦ detect_report：`detect_rule` 描述字段→`detect_pattern`+`allowed_values`+`report_label`，代码按配置检出+对照数据源
- `_apply_deterministic`/`_detect_and_report` 替代旧的三写死函数

## 0.1.2b0 — LLM 配置移入配置 Tab

### 改动
- LLM 后端设置从 topbar 移到配置 Tab（对齐 structured-writer：配置 Tab 内设后端/地址/模型/超时/Token/温度）
- topbar 只保留 logo + tag
- 默认填好 API 地址（LM Studio → http://localhost:1234，Ollama → http://localhost:11434）
- 模型下拉 + "刷新"按钮：调用 `/api/llm/models` 抓取后端可用模型列表
- 模型固化：选中模型后"保存后端配置"持久化到 config.json
- 后端切换自动填默认 base_url

### 新增
- `/api/llm/models` 端点：按 backend + base_url 抓取模型列表

## 0.1.1b0 — 配置推动

### 新增
- `config_manager.py`：ConfigManager + DEFAULT_CONFIG + BACKEND_DEFAULTS，配置持久化到 `config.json`
- `/api/config` GET/POST：前端读写全部配置
- 配置推动：LLM 后端/base_url/model/api_key 全从 config 读取（CLI 参数 > config.json > DEFAULT_CONFIG）
- 前端 topbar 后端切换自动填默认 base_url
- 英文品牌名 `silprespec-emulator`（对齐 structured-writer 样式：logo `⚡ silprespec-emulator` + tag `前置规范效果模拟器`）

### 改动
- 删除 Handler 类变量（backend/base_url/model/api_key），改为 ConfigManager 推动
- `run_server` / `make_llm` 从 config 读，CLI 参数覆盖 config
- 端口 8790 → 8805

## 0.1.0b0 — 首次构建

### 新增
- 8 种前置规范方式（gate / guide / condense / slot / diverge / deterministic / detect_report / required_min），都是前置，作用在填空出口
- 真实执行引擎（LLM 真填空），观测：填入内容 / 重试次数 / 撑满失败 / 命中留空分布 / 重现性
- 并行 N 次重现性观测（命中一致率 + 填入一致率）
- Web UI（端口 8805）：三 Tab（配置=方式多选+各方式JSON配置 / 运行=输入+并行数 / 结果=填入内容+重试+撑满+重现性）
- 多后端 LLM 客户端（LM Studio / Ollama / Custom OpenAI 兼容）
- 批处理模式（--batch input.json output.json）

### 设计依据
- 08a 前置规范 > 后置验证；三态谱系；validate() 的死亡
- 08b 空坐标系；双判据交点（容错宽度 × 注意力窗口）
- 08c 四论断；三场景自由度光谱；减法操作集 ①-⑥
- 07 四原则（如无必要勿增实体 / 如无规矩勿增操作 / 如无能力勿增限制 / 如无资源勿增承诺）
- 09b 配置推动的穷举一致性；偏差一致 = 可校准