"""前置规范效果实验台 — 数据模型

5 种前置规范方式（按逻辑分类，软引导为第一位基础原子）：
  1. pure_guide      纯软引导（只任务提示词，可加输出约束校验）
  2. value_bound     值域限定（gate/slot/required_min/condense 合并，bound_type 区分）
  3. diverge_correct 发散纠偏（高温度发散+代码确定性纠偏，语义偏离拉回）
  4. deterministic_pin 确定性封死（代码钉死可枚举，A 形态）
  5. detect_report   检出上报（不可枚举检出+上报，不阻塞，B 形态）
  + custom           自定义组合（自由组合原子，受互斥规则约束）

组合规则：值域限定(A) 与 发散纠偏(B) 互斥（收敛 vs 放开），其余任意组合。
所有方式都建立在软引导（task_prompt）基础之上——task_prompt 是第一位基础原子。

观测：填入内容、重试次数、撑满失败、重现性 + 验证指标（量化每种后置是否真的生效）。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict

# 5 种前置规范方式（custom 不是预置方式，是 UI 特殊入口：自由组合原子 + 保存为模板）
WAYS = [
    ("pure_guide", "纯软引导", "只任务提示词，LLM 自由填空，可加输出约束校验"),
    ("value_bound", "值域限定", "gate/slot/required_min/condense 合并，bound_type 区分值域类型"),
    ("diverge_correct", "发散纠偏", "高温度发散+代码确定性纠偏，语义偏离拉回（非格式校验）"),
    ("deterministic_pin", "确定性封死", "代码钉死可枚举，LLM 零参与，A 形态错误无通道"),
    ("detect_report", "检出上报", "不可枚举检出+上报，不阻塞生成通道，B 形态"),
]

# 5 种方式的默认任务提示词（系统提示词=软引导，第一位基础原子；用户可在 UI 编辑覆盖）
TASK_PROMPTS = {
    "pure_guide": "按照任务要求，对用户输入给出你的填空结果。",
    "value_bound": "按照要求，从用户输入中提取/分类/凝练信息。",
    "diverge_correct": "基于用户输入发散生成一段内容。",
    "deterministic_pin": "生成一段内容，后续将被代码后处理钉死。",
    "detect_report": "生成一段可能含特定内容（如数值）的文本。",
    "custom": "按照任务要求，对用户输入给出你的填空结果。",
}

# 值域限定的 4 种子类型
BOUND_TYPES = [
    ("enum_select", "可枚举选择", "从有限候选词中选一个或「未指定」（原 gate）"),
    ("slot_extract", "槽位提取", "从文本提取信息填槽位，查多余编造（原 slot）"),
    ("required_min", "必填最小化", "required 槽必填，可留空槽填未指定（原 required_min）"),
    ("condense_enum", "凝练+枚举过滤", "凝练为短词+枚举校验禁泛化（原 condense）"),
]


WAY_HELPS = {
    "pure_guide": """【纯软引导】（软引导 = 第一位基础原子）
名词：软引导=只给任务提示词引导方向，不限定候选集，不硬约束。LLM 自由填空但受引导影响。所有 5 种方式都建立在软引导之上——这是最基础的一种：只有软引导，可加输出约束校验。

■ 字段填写（每个空怎么填）：
【任务提示词 task_prompt】所有方式共有的第一位基础原子。
  · 填什么：自然语言句子，给 LLM 的任务方向（作为 system_prompt 传给 LLM）
  · 可填值：任意文本，如"按照要求，对用户输入给出你的填空结果。"
  · 留空：用默认"按照任务要求，对用户输入给出你的填空结果。"
【引导提示词 guide_prompt】给 LLM 的额外引导方向。
  · 可填值：任意文本，如"围绕主题展开，保持语义一致"
  · 留空：LLM 只受 task_prompt 引导，无额外引导
【必含关键词 required_keywords】输出必须包含的关键词，逗号分隔。
  · 可填值：关键词逗号分隔，如"软件,人工智能"
  · 留空：不校验关键词
  · 效果：输出缺任一关键词→重试
【禁词 forbidden_keywords】输出不得包含的词，逗号分隔。
  · 可填值：禁词逗号分隔，如"垃圾,错误"
  · 留空：不校验禁词
  · 效果：输出含任一禁词→重试
【长度上限 max_length】输出字符数上限，数字。
  · 可填值：数字，如 300
  · 填 0：不校验长度
  · 效果：输出超长→重试
【格式正则 format_regex】输出必须匹配的正则。
  · 可填值：正则表达式，如"^.{10,100}$"（10-100字符）
  · 留空：不校验格式
  · 效果：输出不匹配→重试

■ 观测：filled.output（LLM 输出内容）
■ 验证指标：达标比例（满足输出约束的 attempt 比例）、重复性
■ 检测什么：LLM 在软引导下填了什么，是否满足输出约束
■ 适用场景：无法穷举的开放维度，只需引导方向，但要校验某些约束
■ 输入类型：自然语言文本（开放内容，如摘要/续写/改写）
■ 缺陷：无硬约束时 LLM 可能偏离引导；约束太严等于硬约束

■ 示例（照填即可）：
  输入框：人工智能正在改变软件开发的方式，从代码生成到测试自动化，都在发生深刻变化。
  任务提示词：按照要求，对用户输入续写一段文字。
  引导提示词：围绕主题展开，保持语义一致
  必含关键词：软件
  禁词：（留空）
  长度上限：300
  格式正则：（留空）
  预期：LLM 续写一段含"软件"且≤300字的文本；若缺关键词或超长→重试""",

    "value_bound": """【值域限定】（gate/slot/required_min/condense 合并，bound_type 区分值域类型）
名词：值域限定=把 LLM 输出限定在某个值域内。四种都是"从一句话分类/提取/凝练，值域精度不同"——逻辑上同一类，配置区分。

■ 字段填写（每个空怎么填）：
【值域类型 bound_type】下拉选，决定值域限定方式。选后显示对应子表单。
  · enum_select：可枚举选择——值域=有限候选词集，LLM 从中选一个或"未指定"
  · slot_extract：槽位提取——值域=预定义槽位，LLM 从文本提取填入，查多余编造
  · required_min：必填最小化——值域=槽位，required 槽必填，可留空槽填"未指定"
  · condense_enum：凝练+枚举过滤——值域=枚举词集，LLM 凝练为短词，代码校验在允许集

—— enum_select 子表单 ——
【门禁行】每行一道门禁（多道 AND，每道内候选词 OR）。
  · 维度名：如"情绪"
  · 候选词：逗号分隔，如"积极,消极,中性"
  · 效果：LLM 从候选词中选一个或"未指定"；编造（填了不在候选词里的）→重试
【允许未指定】勾选。
  · 勾：LLM 可填"未指定"（减法，不 block）
  · 不勾：必须命中候选词，未命中→重试

—— slot_extract 子表单 ——
【槽位行】每行一个槽位。
  · 槽位名：如"who"
  · 必填勾选：slot_extract 不强制必填，此勾选仅标记（不查必填）
  · 效果：LLM 填槽位输出 JSON，代码查多余 key（编造槽位以外字段）→有多余 key 重试

—— required_min 子表单 ——
【槽位行】每行一个槽位。
  · 槽位名：如"entity"
  · 必填勾选：勾=必填（必须有内容），不勾=可留空（填"未指定"）
  · 效果：required 槽缺失→重试；可留空槽填"未指定"通过

—— condense_enum 子表单 ——
【凝练规则】给 LLM 的浓缩规则。
  · 可填值：如"浓缩为短词，禁止泛化造新词"
【枚举词】允许的候选词，逗号分隔。
  · 可填值：如"环境治理,生态文明,绿色发展"
  · 效果：LLM 凝练为短词，代码校验是否在枚举集（子串匹配）；造新词→标记编造→重试

■ 观测：filled（填入内容）、hit/unspecified/fabricated 或 extra_fabrication 或 left_empty 或 fabricated_count
■ 验证指标：值域命中率（hit/total）、编造检出率（fabricated/total）、重试回值域率、重复性
■ 检测什么：LLM 输出是否落在值域内，编造了什么
■ 适用场景：维度可穷举的有限分类 / 结构化抽取 / 必填最小化 / 凝练标签
■ 输入类型：自然语言文本（一句话/评论/新闻/履历/长文章，按 bound_type 而定）
■ 缺陷：值域太死可能逼 LLM 硬填/硬造；不检查内容质量

■ 示例（照填即可）：
  [enum_select]
    输入框：今天阳光真好，心情特别棒，感觉一切都很顺利！
    任务提示词：按照以下要求，将用户输入分类。
    值域类型：可枚举选择
    门禁行：维度名=情绪，候选词=积极,消极,中性
    允许未指定：勾
    预期：filled={情绪:积极}，hit=1/fabricated=0
  [slot_extract]
    输入框：张三于2024年3月在北京大学发表了关于大模型训练优化的演讲。
    值域类型：槽位提取
    槽位行：who(必填)、what(必填)、why(不勾)
    预期：filled={who:张三,what:演讲,why:...}；若 LLM 编造槽位外 key→重试
  [required_min]
    输入框：茅台酒的价格是多少？
    值域类型：必填最小化
    槽位行：entity(必填勾)、attr(必填勾)、rel(不勾)
    预期：filled={entity:茅台酒,attr:价格,rel:未指定}
  [condense_enum]
    输入框：近年来我国在环境治理方面取得显著成效，生态文明建设深入推进...
    值域类型：凝练+枚举过滤
    凝练规则：浓缩为短词，禁止泛化造新词
    枚举词：环境治理,生态文明,绿色发展
    预期：filled.condensed=[环境治理,生态文明]；造新词→fabricated 标记→重试""",

    "diverge_correct": """【发散纠偏】（08c 场景三：放开+收紧配对=误差抵消，语义偏离拉回，非格式校验）
名词：发散=让 LLM 高温度自由生成（放开，必然漂移）。纠偏=生成后用代码确定性修正可纠的漂移（收紧，把偏离拉回）。放开+收紧配对=误差抵消。与值域限定相反：值域限定是生成前限定，diverge_correct 是生成后纠偏。纠偏是语义偏离范围的拉回，不是格式校验（格式校验由 deterministic_pin 做）。

■ 字段填写（每个空怎么填）：
【发散提示词 diverge_prompt】给 LLM 的发散方向（锚点在提示词里）。
  · 可填值：任意文本，如"基于此主题发散生成一段科普"
  · 留空：用默认"自由发散生成"
  · 效果：高温度(0.9)生成，必然漂移
【替换规则行】每行一条正则替换，代码按顺序执行。
  · 正则 pattern：如"【引用标记】"或"\\n{3,}"
  · 替换文本：如""（删除）或"\\n\\n"（替换成双换行）
  · 无行（删光）：不执行正则替换
  · 效果：代码按顺序对 LLM 输出执行 re.sub(pattern, replace, text)
【空行归一化 normalize_blanklines】勾选。
  · 勾：多余空行（≥3个连续换行）压成双换行
  · 不勾：不处理空行
  · 效果：LLM 高温度常产生多余空行，勾选后泛化纠偏
【纠偏目标 correction_target】校验纠偏后 corrected 是否达标。全留空=只观测纠偏 changed，不校验。
  · 格式正则：纠偏后 corrected 必须匹配的正则，如"^.{10,500}$"
    - 留空：不校验格式
  · 必含模式：纠偏后必须包含的正则，如"生物|海洋|深海"
    - 留空：不校验必含
  · 禁含模式：纠偏后不得包含的正则，如"广告|链接|http"
    - 留空：不校验禁含
  · 效果：非空=校验纠偏后是否达标，不达标→重试；全留空=只观测 changed

■ 观测：filled.raw（纠偏前）、filled.corrected（纠偏后）、changed（收紧是否生效）
■ 验证指标：changed 比例 + 纠偏编辑距离(Levenshtein raw→corrected) + 纠偏有效性(raw不达标且corrected达标) + 达标比例 + 重复性
■ 检测什么：LLM 发散漂移了什么，代码纠偏改了什么（changed），纠偏后是否达标
■ 适用场景：需要创意发散但有些维度可代码纠偏（格式/标记/空行）
■ 输入类型：自然语言主题/提示词（创意生成，如故事/文案/扩写）
■ 缺陷：纯正则纠偏能力有限（格式/标记轴），复杂语义漂移需 RAG/人类重修

■ 示例（照填即可）：
  输入框：深海中的发光生物
  任务提示词：基于用户输入发散生成一段内容。
  发散提示词：基于此主题发散生成一段科普，介绍深海发光生物的特点
  替换规则行：（无行，删光）
  空行归一化：勾
  纠偏·格式正则：（留空）
  纠偏·必含模式：生物|海洋|深海|发光
  纠偏·禁含模式：（留空）
  预期：LLM 高温度发散一段（可能含多余空行），代码压空行（changed=true），纠偏后校验含"生物/海洋/深海/发光"之一→达标；若不含→重试""",

    "deterministic_pin": """【确定性封死】（08a §7 A 形态：生成时封死可枚举值域，错误无通道）
名词：钉死=用代码强制固定某些维度（格式/路径/编号），LLM 零参与，百分百准确。封死=这些维度错误无生成通道。与纠偏类似但更彻底：纠偏是部分修正，钉死是完全固定。代码部分百分百准确不需测试，测的是 LLM 原始输出——钉死前后差异反映 LLM 不可控程度。

■ 字段填写（每个空怎么填）：
【替换规则行】每行一条正则替换，代码按顺序执行。
  · 正则 pattern：如"【引用自来源\\d+】"或"\\*\\*"
  · 替换文本：如""（删除）或""（替换成空）
  · 无行（删光）：不执行正则替换
  · 效果：代码按顺序对 LLM 输出执行 re.sub(pattern, replace, text)
【编号重排 renumber_source】勾选。
  · 勾：把"来源1""来源3""来源2"重排为"来源1""来源2""来源3"
  · 不勾：不重排
  · 效果：用户有引用编号场景才开
【空行归一化 normalize_blanklines】勾选。
  · 勾：多余空行（≥3个连续换行）压成双换行
  · 不勾：不处理
  · 效果：LLM 常产生多余空行，勾选后泛化钉死
【封死目标 pin_target】校验钉死后 corrected 是否达标。全留空=纯 A 钉死，观测 changed，不校验。
  · 精确值：钉死后 corrected 必须精确等于此字符串
    - 可填值：任意文本，如"固定输出内容"
    - 留空：不校验精确值
  · 格式正则：钉死后必须匹配的正则，如"^.{10,200}$"
    - 留空：不校验格式
  · 效果：非空=校验钉死后是否达标；全留空=纯 A 钉死观测 changed

■ 观测：filled.raw（钉死前）、filled.pinned（钉死后）、changed（是否改过）
■ 验证指标：changed 比例 + 达标率 + 多次 100% 完全一致（代码零采样，最硬的验证）+ 重复性
■ 检测什么：LLM 原始输出中需要钉死的格式，钉死前后差异（changed），钉死后是否达标
■ 适用场景：某些维度可代码封死（格式/空行/编号），减少需验证维度
■ 输入类型：自然语言文本（含格式噪声，需钉死格式）
■ 缺陷：只能封死格式轴（编号/空行/符号），内容轴无法封死

■ 示例（照填即可）：
  输入框：大模型技术发展迅速，多模态能力不断提升，应用场景持续扩展。
  任务提示词：生成一段内容，后续将被代码后处理钉死。
  替换规则行：（无行，删光）
  编号重排：不勾
  空行归一化：勾
  封死·精确值：（留空）
  封死·格式正则：（留空）
  预期：LLM 生成一段简述（可能含多余空行），代码压空行钉死格式（changed=true）；观测 raw vs pinned；多次运行 pinned 100% 一致""",

    "detect_report": r"""【检出即上报】（08a §7 B 形态：上报器不阻塞生成通道）
名词：检出=用正则扫描 LLM 输出中的特定内容（如数值）。上报=检出项不在合法值集（allowed_values）中则标记人工复审。与封死相反：封不死的内容轴（不可枚举值域）只能检出后交给人工。上报器不是验证器——不宣称"没问题"，宣称"这些我没法确认请人工看"，监督责任显式转移给人类，不阻塞生成通道。

■ 字段填写（每个空怎么填）：
【检出正则 detect_pattern】匹配需要审核的内容的正则。
  · 可填值：正则表达式，如"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)"
  · 留空：用默认正则（匹配数值+单位）
  · 效果：正则扫描 LLM 输出，匹配的为检出项；无检出→失败（检出器无效）
【合法值 allowed_values】合法值列表，逗号分隔。
  · 可填值：合法值逗号分隔，如"55.8万亿元,42.8%,10.9亿人"
  · 留空：所有检出项都标记 unmatched（未配合法域，全部需上报）
  · 效果：不在此列表的检出项标记 unmatched=True（需上报）；在此列表的 unmatched=False
【上报标签 report_label】上报标记文本。
  · 可填值：任意文本，如"建议人工复审"
  · 留空：用默认"建议人工复审"
  · 效果：检出项的 report 字段填此标签

■ 观测：filled.raw（原始输出）、filled.flagged（检出列表，每项含 value/pos/report/unmatched）、flagged_count/unmatched_count
■ 验证指标：检出率（flagged 非空比例）+ 上报率（unmatched/flagged 比例）+ 重复性
■ 检测什么：LLM 输出中检出项（如数值）是否在合法值集；检出器是否有效（空响应/无检出判失败）
■ 适用场景：内容轴封不死（数值/事实），需人工审核
■ 输入类型：自然语言文本（含数值/事实陈述，如报告/新闻/统计描述）
■ 缺陷：正则检出有限；allowed_values 需人工维护

■ 示例（照填即可）：
  输入框：2024年我国数字经济规模达到55.8万亿元，占GDP比重42.8%，网民规模10.9亿人。
  任务提示词：生成一段可能含特定内容（如数值）的文本。
  检出正则：\d+(?:\.\d+)?(%|亿|万|元|人次)
  合法值：55.8万亿元,42.8%,10.9亿人
  上报标签：建议人工复审
  预期：检出 55.8万/42.8%/10.9亿（正则匹配），对照合法值标记 unmatched；有检出=success（上报器工作，不阻塞）；若 LLM 造数（如 99%）→unmatched 标记上报，仍 success（人工兜底）""",

    "custom": r"""【自定义组合·自由组合原子】
名词：自定义组合=从原子中自由选组合，配出自己的前置规范方式。recipe（原子配方）决定执行哪些原子，config（配置 JSON）提供这些原子需要的参数。

■ 组合规则（重要）：
- 值域限定(A) 与 发散纠偏(B) 互斥（收敛 vs 放开，不能同时做）
- 其余任意组合（不同轴或互补）：A+C / A+D / B+C / B+D / C+D / A+C+D / B+C+D
- 软引导（task_prompt）是第一位基础原子，所有方式必有

■ 字段填写（每个空怎么填）：
【任务提示词 task_prompt】所有方式共有的第一位基础原子。
  · 可填值：任意文本，如"按照要求，对用户输入给出你的填空结果。"
  · 留空：用默认
【配方 recipe】原子下拉多选，决定执行哪些原子。
  · 生成原子 generate（三选一）：
    - text：文本生成，LLM 自由填空输出一段文本（受 guide_prompt/diverge_prompt/condense_rule 引导）
    - select：穷举选择，LLM 从候选词表中每道选一个词或"未指定"（需 gates）
    - slot：槽位填空，LLM 从输入提取信息填入预定义槽位，输出 JSON（需 slots）
  · 生成参数 generate_arg（slot 时选）：
    - extra_check：查多余编造（槽位以外字段）
    - required_min：查必填缺失
  · 后处理原子 postprocess（多选，按顺序执行）：
    - deterministic：正则替换+编号重排+空行归一化，LLM 零参与（需 regex_replaces/renumber_source/normalize_blanklines）
    - enum_filter：枚举过滤，只留在允许词列表中的词，标记编造（需 enums）
    - detect_report：检出即上报，正则扫描+白名单对照+标记人工复审（需 detect_pattern/allowed_values/report_label）
    - json_parse：JSON 解析，把 LLM 输出解析为槽位 dict，找多余 key（需 slots）
  · 校验原子 validate（选一）：
    - none：不校验，直接通过
    - in_set：集合成员校验，每个维度值必须在候选词表或"未指定"（点对面，需 gates）
    - no_extra：无多余校验，查编造词或多余字段（需 slots 或 enums）
    - required_full：必填齐全校验，所有 required 槽位必须有内容（需 slots）
    - in_range：区间容差校验，数值必须在区间内（面对面，需 range_checks）
    - eq_exact：精确相等校验，值必须等于指定值（点对点，需 exact_checks）
    - guide：软引导输出约束校验（需 output_constraints）
    - diverge：发散纠偏目标校验（需 correction_target）
    - deterministic：确定性封死目标校验（需 pin_target）
    - detect_report：检出上报校验（空响应/无检出判失败，有检出=success）
  · 重试 retry：勾=校验驱动重试，不勾=单次
  · 观测原子 observe（多选）：
    - hit：命中分布（命中/未指定/编造）
    - fabricated：编造统计（造了不在允许集的词数）
    - extra_keys：多余字段（LLM 编造的槽位以外 key）
    - left_empty：留空统计（必填/可留空/实际留空数）
    - flagged：检出统计（检出项数和未命中白名单数）
    - changed：改过标记（后处理前后是否改过）
【配置 JSON config】提供选的原子需要的参数。
  · guide_prompt：字符串，引导提示词（gen_text 用）
  · diverge_prompt：字符串，发散提示词（gen_text 用）
  · condense_rule：字符串，凝练规则提示词（gen_text 用）
  · gates：数组，每项 {name, words, logic}，门禁维度+候选词+逻辑（gen_select / validate in_set 用）
  · slots：数组，每项 {name, required}，槽位名+是否必填（gen_slot / validate no_extra / validate required_full 用）
  · enums：字符串数组，允许的候选词列表（enum_filter 用）
  · regex_replaces：数组，每项 {pattern, replace}，正则替换规则（deterministic 用）
  · renumber_source：布尔，来源编号重排（deterministic 用）
  · normalize_blanklines：布尔，空行归一化（deterministic 用）
  · detect_pattern：字符串，检出正则（detect_report 用）
  · allowed_values：字符串数组，合法值列表（detect_report / validate in_range / validate eq_exact 用）
  · report_label：字符串，上报标记文本（detect_report 用）
  · output_constraints：对象，软引导输出约束（validate guide 用）
  · correction_target：对象，纠偏目标（validate diverge 用）
  · pin_target：对象，封死目标（validate deterministic 用）
  · range_checks：数组，区间容差校验（validate in_range 用）
  · exact_checks：数组，精确相等校验（validate eq_exact 用）

■ 验证指标：取决于选的原子（值域命中率/纠偏编辑距离/钉死确定性/检出率等）
■ 检测什么：由 recipe 的校验原子和观测原子决定
■ 适用场景：5 种预置方式都不满足时，自由组合原子
■ 输入类型：取决于 recipe 的生成原子
■ 缺陷：config 字段必须与 recipe 原子匹配，填了没用的字段被忽略，漏了需要的字段原子报错

■ 示例（照填即可）——值域限定+确定性封死组合：
  配方 recipe：
    generate = text
    postprocess = [deterministic]
    validate = no_extra
    retry = 勾
    observe = [changed]
  配置 JSON config：
    {"condense_rule":"浓缩为短词","enums":["环境治理","生态文明"],
     "regex_replaces":[{"pattern":"\\*\\*","replace":""}],"normalize_blanklines":true}
  效果：LLM 凝练为短词→代码正则替换+压空行→校验无编造→观测 changed""",
}

# 空坐标形态由 validate 原子承载（in_set=点对面 / in_range=面对面 / eq_exact=点对点 / none=不校验）


@dataclass
class WayConfig:
    """一种前置规范方式的配置"""
    way: str = "pure_guide"   # 方式 id
    enabled: bool = True
    config: dict = field(default_factory=dict)  # 该方式专属配置（见 default_config）
    max_retry: int = 3
    task_prompt: str = ""         # 任务提示词（系统提示词=软引导，第一位基础原子）；空则用 TASK_PROMPTS[way]
    recipe: dict = field(default_factory=dict)  # 自定义原子配方；空则用 recipe_for(way)
    template_id: str = ""         # 自定义模板库 id（仅 UI 标记；执行用 way=custom + recipe）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "WayConfig":
        return WayConfig(
            way=d.get("way", "pure_guide"),
            enabled=d.get("enabled", True),
            config=d.get("config", {}),
            max_retry=d.get("max_retry", 3),
            task_prompt=d.get("task_prompt", ""),
            recipe=d.get("recipe", {}),
            template_id=d.get("template_id", ""),
        )


def default_config(way: str) -> dict:
    """每种方式的默认配置 schema（供 UI 渲染）"""
    if way == "pure_guide":
        return {"guide_prompt": "围绕主题展开，保持语义一致",
                "output_constraints": {"required_keywords": [], "forbidden_keywords": [],
                                        "max_length": 0, "format_regex": ""}}
    if way == "value_bound":
        return {"bound_type": "enum_select",
                "gates": [{"name": "情绪", "words": ["积极", "消极", "中性"], "logic": "or"}],
                "allow_unspecified": True,
                "slots": [{"name": "who", "required": True}, {"name": "what", "required": True},
                          {"name": "why", "required": False}],
                "condense_rule": "浓缩为短词，禁止泛化造新词",
                "enums": ["环境治理", "生态文明", "绿色发展"]}
    if way == "diverge_correct":
        return {"diverge_prompt": "自由发散生成",
                "regex_replaces": [],
                "normalize_blanklines": True,
                "correction_target": {"format_regex": "", "required_pattern": "", "forbidden_pattern": ""}}
    if way == "deterministic_pin":
        return {"regex_replaces": [],
                "renumber_source": False, "normalize_blanklines": True,
                "pin_target": {"exact_value": "", "format_regex": ""}}
    if way == "detect_report":
        return {"detect_pattern": r"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)",
                "allowed_values": [], "report_label": "建议人工复审"}
    return {}


@dataclass
class Experiment:
    """一次实验：用户选的一种或多种方式 + 并行数"""
    name: str = "前置规范效果实验"
    description: str = ""
    ways: list = field(default_factory=list)  # list[WayConfig]
    parallel: int = 5

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "ways": [w.to_dict() for w in self.ways], "parallel": self.parallel}

    @staticmethod
    def from_dict(d: dict) -> "Experiment":
        return Experiment(
            name=d.get("name", "前置规范效果实验"),
            description=d.get("description", ""),
            ways=[WayConfig.from_dict(w) for w in d.get("ways", [])],
            parallel=d.get("parallel", 5),
        )

    @staticmethod
    def default() -> "Experiment":
        return Experiment(
            name="前置规范效果实验·示例",
            description="选一种或多种前置规范方式，观测填入内容/重试/撑满失败/验证指标",
            ways=[
                WayConfig(way="pure_guide", config=default_config("pure_guide"), max_retry=3),
                WayConfig(way="value_bound", config=default_config("value_bound"), max_retry=3),
            ],
            parallel=5,
        )


# ======================================================================
# 执行结果
# ======================================================================
@dataclass
class WayResult:
    """一种方式在一次执行中的结果"""
    way: str = ""
    success: bool = False             # 最终是否填空合规
    filled: dict = field(default_factory=dict)    # 实际填入内容
    retry_count: int = 0
    exhausted: bool = False           # 是否撑满 max_retry 仍失败
    attempts: list = field(default_factory=list)  # 每次尝试的完整 trace
    extra: dict = field(default_factory=dict)     # 各方式专属观测
    calls: list = field(default_factory=list)     # 每次 LLM 调用记录
    total_tokens: int = 0
    elapsed_total: float = 0.0
    error: str = ""
    metrics: dict = field(default_factory=dict)   # 验证指标（量化每种后置是否真的生效）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
    """一次并行（一个个体）的结果"""
    run_id: int = 0
    way_results: list = field(default_factory=list)  # list[WayResult]

    def to_dict(self) -> dict:
        return {"run_id": self.run_id,
                "way_results": [w.to_dict() for w in self.way_results]}


@dataclass
class Reproducibility:
    """重现性：跨 run 各方式的填入一致率"""
    way: str = ""
    distinct_fills: list = field(default_factory=list)
    consistency: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def calc_reproducibility(results: list) -> list:
    """对每种方式计算跨 run 重现性"""
    if not results:
        return []
    def to_d(x):
        if isinstance(x, dict): return x
        if hasattr(x, 'to_dict'): return x.to_dict()
        return asdict(x)
    results = [to_d(r) for r in results]
    way_ids = []
    for w in results[0].get("way_results", []):
        w = to_d(w)
        way_ids.append(w.get("way", ""))
    out = []
    for wid in way_ids:
        fills = []
        for r in results:
            for w in r.get("way_results", []):
                w = to_d(w)
                if w.get("way", "") == wid:
                    fills.append(json_key(w.get("filled", {})))
        if not fills:
            continue
        from collections import Counter
        cnt = Counter(fills)
        most = cnt.most_common(1)[0][1]
        out.append(Reproducibility(
            way=wid,
            distinct_fills=[k for k, _ in cnt.most_common()],
            consistency=round(most / len(fills), 3),
        ).to_dict())
    return out


def json_key(d) -> str:
    import json
    try:
        return json.dumps(d, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(d)
