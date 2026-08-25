# 更新日志 / CHANGELOG

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