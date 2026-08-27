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

# 5 种前置规范方式 + custom
WAYS = [
    ("pure_guide", "纯软引导", "只任务提示词，LLM 自由填空，可加输出约束校验"),
    ("value_bound", "值域限定", "gate/slot/required_min/condense 合并，bound_type 区分值域类型"),
    ("diverge_correct", "发散纠偏", "高温度发散+代码确定性纠偏，语义偏离拉回（非格式校验）"),
    ("deterministic_pin", "确定性封死", "代码钉死可枚举，LLM 零参与，A 形态错误无通道"),
    ("detect_report", "检出上报", "不可枚举检出+上报，不阻塞生成通道，B 形态"),
    ("custom", "自定义组合", "自由组合原子，A 与 B 互斥，其余任意组合"),
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
名词：软引导=只给任务提示词（task_prompt）引导方向，不限定候选集，不硬约束。LLM 自由填空但受引导影响。所有 5 种方式都建立在软引导之上——这是最基础的一种：只有软引导，可加输出约束校验。
给 LLM 任务提示词，让它填空。可配输出约束校验续写是否满足，不满足重试。约束留空=纯软引导不校验。

表单：
- 任务提示词（task_prompt）：所有方式共有的第一位基础原子，给 LLM 的任务方向
- 引导提示词（guide_prompt）：文本框，给 LLM 的引导方向（如"围绕主题展开，保持语义一致"）
- 输出约束（可配，留空=纯软引导不校验）：
  · 必含关键词：逗号分隔，输出必须包含每个
  · 禁词：逗号分隔，输出不得包含任何
  · 长度上限：数字，输出字符数不得超过
  · 格式正则：输出必须匹配此正则

观测：filled.output（LLM 输出内容）

验证指标：达标比例（满足输出约束的 attempt 比例）、重复性（并行一致）
检测什么：LLM 在软引导下填了什么，是否满足输出约束
适用场景：无法穷举的开放维度，只需引导方向，但要校验某些约束是否满足
输入类型：自然语言文本（开放内容，如摘要/续写/改写）
缺陷：无硬约束时 LLM 可能偏离引导；约束太严等于硬约束

示例：
- 输入：人工智能正在改变软件开发的方式，从代码生成到测试自动化，都在发生深刻变化。
- 配置：引导提示词=围绕主题展开，保持语义一致；必含关键词=软件；长度上限=300
- 预期：LLM 续写一段含"软件"且≤300字的文本；若偏离或缺关键词→重试""",

    "value_bound": """【值域限定】（gate/slot/required_min/condense 合并，bound_type 区分值域类型）
名词：值域限定=把 LLM 输出限定在某个值域内。值域类型不同，限定方式不同：
  · enum_select（可枚举选择）：值域=有限候选词集，LLM 从中选一个或"未指定"。多道门禁 AND，每道内候选词 OR。编造（填了不在候选词里的）标记失败重试。
  · slot_extract（槽位提取）：值域=预定义槽位，LLM 从文本提取信息填入。多余 key（编造槽位以外字段）标记失败重试。
  · required_min（必填最小化）：值域=槽位，required 槽必填，可留空槽填"未指定"。required 槽缺失重试。
  · condense_enum（凝练+枚举过滤）：值域=枚举词集，LLM 凝练为短词，代码校验是否在允许集（禁泛化造新词）。
四种都是"从一句话分类/提取/凝练，值域精度不同"——逻辑上同一类，配置区分。

表单：
- 值域类型（bound_type）：下拉选 enum_select/slot_extract/required_min/condense_enum
- enum_select：门禁行（维度名+候选词逗号分隔）+允许未指定勾选
- slot_extract：槽位行（槽位名+必填勾选）— 只查多余编造，不强制必填
- required_min：槽位行（槽位名+必填勾选）— 查必填缺失
- condense_enum：凝练规则+枚举词（逗号分隔）

观测：filled（填入内容）、hit/unspecified/fabricated 或 extra_fabrication 或 left_empty 或 fabricated_count

验证指标：值域命中率（hit/total）、编造检出率（fabricated/total）、重试回值域率（重试后回到值域的比例）、重复性
检测什么：LLM 输出是否落在值域内，编造了什么
适用场景：维度可穷举的有限分类 / 结构化抽取 / 必填最小化 / 凝练标签
输入类型：自然语言文本（一句话/评论/新闻/履历/长文章，按 bound_type 而定）
缺陷：值域太死可能逼 LLM 硬填/硬造；不检查内容质量

示例：
- enum_select：输入=今天阳光真好心情棒 → 情绪维度候选词=积极,消极,中性 → filled={情绪:积极}
- slot_extract：输入=张三于2024年3月在北京大学演讲 → 槽位=who,what,why → filled={who:张三,what:演讲,...}
- required_min：输入=茅台酒的价格是多少 → 槽位=entity(必填),attr(必填),rel(可留空) → filled={entity:茅台酒,attr:价格,rel:未指定}
- condense_enum：输入=环境治理成效显著... → 枚举词=环境治理,生态文明,绿色发展 → filled.condensed=[环境治理,生态文明]""",

    "diverge_correct": """【发散纠偏】（08c 场景三：放开+收紧配对=误差抵消，语义偏离拉回，非格式校验）
名词：发散=让 LLM 高温度自由生成（放开，必然漂移）。纠偏=生成后用代码确定性修正可纠的漂移（收紧，把偏离拉回）。放开+收紧配对=误差抵消（"错着错着就对了"）。与值域限定相反：值域限定是生成前限定，diverge_correct 是生成后纠偏。纠偏是语义偏离范围的拉回，不是格式校验（格式校验由 deterministic_pin 做）。
LLM 高温度发散，代码按配置确定性纠偏（正则替换+空行归一化，用户配任意规则针对任意漂移）。纠偏目标校验是前置规范内部校验（非任务后全量验证），correction_target 非空=校验纠偏达标不达标重试，留空=只观测纠偏 changed。

表单：
- 发散提示词：文本框，给 LLM 的发散方向（锚点在提示词里）
- 替换规则行：每行一条（正则 pattern + 替换文本）；用户针对自己场景配；代码按顺序执行
- 空行归一化：勾选=多余空行压成双换行（LLM 高温度真实产生，泛化纠偏）
- 纠偏目标（correction_target，非空=校验纠偏达标，留空=只观测 changed）：
  · 格式正则：纠偏后 corrected 必须匹配
  · 必含模式：纠偏后必须包含的正则
  · 禁含模式：纠偏后不得包含的正则

观测：filled.raw（纠偏前）、filled.corrected（纠偏后）、changed（收紧是否生效）

验证指标：changed 比例 + 纠偏编辑距离（Levenshtein(raw,corrected)，量化改了多少字符）+ 纠偏有效性（raw不达标且corrected达标的比例，证明纠偏真的把不达标变达标）+ 达标比例 + 重复性
检测什么：LLM 发散漂移了什么，代码纠偏改了什么（changed），纠偏后是否达标
适用场景：需要创意发散但有些维度可代码纠偏（格式/标记/空行）；用户针对自己场景配纠偏规则
输入类型：自然语言主题/提示词（创意生成，如故事/文案/扩写）
缺陷：纯正则纠偏能力有限（格式/标记轴），复杂语义漂移需 RAG/人类重修（本简化版不含）

示例：
- 输入：深海中的发光生物
- 配置：发散提示词=基于此主题发散生成一段科普；空行归一化=勾；纠偏目标=格式正则非空（如要求含"生物"关键词）
- 预期：LLM 高温度发散一段（可能含多余空行/偏离），代码压空行（changed=true），纠偏后校验达标""",

    "deterministic_pin": """【确定性封死】（08a §7 A 形态：生成时封死可枚举值域，错误无通道）
名词：钉死=用代码强制固定某些维度（格式/路径/编号），LLM 零参与，百分百准确。封死=这些维度错误无生成通道，不再需要验证（C 形态后验证自然淘汰）。与纠偏类似但更彻底：纠偏是部分修正，钉死是完全固定。
LLM 生成，代码按配置钉死（正则替换+编号重排+空行归一化，用户配任意规则）。封死目标校验是前置规范内部校验，pin_target 非空=校验钉死达标，留空=纯 A 形态钉死（错误无通道，观测 changed）。代码部分百分百准确不需测试，测的是 LLM 原始输出——钉死前后差异反映 LLM 不可控程度。

表单：
- 替换规则行：每行一条（正则 pattern + 替换文本）；用户针对自己场景配；代码按顺序执行
- 编号重排：勾选=来源编号重排为 1,2,3...（用户有编号场景才开）
- 空行归一化：勾选=多余空行压成双换行（泛化，LLM 真实产生）
- 封死目标（pin_target，可配，留空=纯 A 钉死不比对）：
  · 精确值：钉死后 corrected 必须精确等于此字符串
  · 格式正则：钉死后必须匹配此正则

观测：filled.raw（钉死前）、filled.pinned（钉死后）、changed（是否改过）

验证指标：changed 比例 + 达标率 + 多次 100% 完全一致（代码零采样，最硬的验证）+ 重复性
检测什么：LLM 原始输出中需要钉死的格式，钉死前后差异（changed），钉死后是否达标
适用场景：某些维度可代码封死（格式/空行/编号），减少需验证维度；用户针对自己场景配钉死规则
输入类型：自然语言文本（含格式噪声，需钉死格式）
缺陷：只能封死格式轴（编号/空行/符号），内容轴无法封死

示例：
- 输入：大模型技术发展迅速，多模态能力不断提升，应用场景持续扩展。
- 配置：空行归一化=勾；编号重排=不勾；封死目标=留空
- 预期：LLM 生成一段简述（可能含多余空行），代码压空行钉死格式（changed=true）；观测 raw vs pinned""",

    "detect_report": r"""【检出即上报】（08a §7 B 形态：上报器不阻塞生成通道）
名词：检出=用正则扫描 LLM 输出中的特定内容（如数值）。上报=检出项不在合法值集（allowed_values）中则标记人工复审。与封死相反：封不死的内容轴（不可枚举值域）只能检出后交给人工。上报器不是验证器——不宣称"没问题"，宣称"这些我没法确认请人工看"，监督责任显式转移给人类，不阻塞生成通道。
LLM 生成，代码按配置检出内容（正则），对照 allowed_values 标记未命中的为上报。校验：空响应/无检出→失败（检出器无效）；有检出→成功（上报器工作了，哪怕全 unmatched 也是"全部需上报"+人工兜底，不阻塞=success）。

表单：
- 检出正则：输入框，匹配需要审核的内容（如 \d+(?:\.\d+)?(%|亿|万|元|人次)）
- 合法值：逗号分隔输入（如 100%,3.5亿）；配了则只标记不在此列表的检出项，不配则所有检出项都标记需上报
- 上报标签：输入框，上报标记文本（如"建议人工复审"）

观测：filled.raw（原始输出）、filled.flagged（检出列表，含 value/pos/report/unmatched）、flagged_count/unmatched_count

验证指标：检出率（flagged 非空比例）+ 上报率（unmatched/flagged 比例）+ 重复性
检测什么：LLM 输出中检出项（如数值）是否在合法值集；检出器是否有效（空响应/无检出判失败）
适用场景：内容轴封不死（数值/事实），需人工审核
输入类型：自然语言文本（含数值/事实陈述，如报告/新闻/统计描述）
缺陷：正则检出有限；allowed_values 需人工维护

示例：
- 输入：2024年我国数字经济规模达到55.8万亿元，占GDP比重42.8%，网民规模10.9亿人。
- 配置：检出正则=\d+(?:\.\d+)?(%|亿|万|元|人次)；合法值=55.8万亿元,42.8%,10.9亿人
- 预期：检出 55.8万亿元/42.8%/10.9亿人 全在合法值→success（上报器工作）；若 LLM 造数（如 99%）→unmatched 标记上报，仍 success（不阻塞，人工兜底）""",

    "custom": r"""【自定义组合·自由组合原子】
名词：自定义组合=从原子中自由选组合，配出自己的前置规范方式。recipe（原子配方）决定执行哪些原子，config（配置 JSON）提供这些原子需要的参数。

组合规则（重要）：
- 值域限定(A) 与 发散纠偏(B) 互斥（收敛 vs 放开，不能同时做）
- 其余任意组合（不同轴或互补）：A+C / A+D / B+C / B+D / C+D / A+C+D / B+C+D
- 软引导（task_prompt）是第一位基础原子，所有方式必有

原子名词：
- text：文本生成，LLM 自由填空输出一段文本（受 guide_prompt/diverge_prompt/condense_rule 引导）
- select：穷举选择，LLM 从预定义候选词表中每道选一个词或"未指定"
- slot：槽位填空，LLM 从输入中提取信息填入预定义槽位，输出 JSON
- deterministic：确定性后处理，代码执行正则替换+编号重排+空行归一化，LLM 零参与
- enum_filter：枚举过滤，只保留在允许词列表中的词或其子串，标记编造词
- detect_report：检出即上报，正则扫描输出中的特定内容，未命中白名单的标记人工复审
- json_parse：JSON 解析，把 LLM 输出解析为槽位 dict，找出多余 key
- in_set：集合成员校验，每个维度值必须在候选词表中或"未指定"（点对面）
- no_extra：无多余校验，检查是否编造了不在允许集的词或多余字段
- required_full：必填齐全校验，所有 required 槽位必须有内容
- in_range：区间容差校验，数值必须在指定区间内（面对面）
- eq_exact：精确相等校验，值必须精确等于指定值（点对点）
- guide/diverge/deterministic/detect_report：各方式专属校验
- none：不校验，直接通过
- hit/fabricated/extra_keys/left_empty/flagged/changed：观测原子

recipe 表单和 config JSON 的配合：
- recipe 选了什么原子 → config 里就要填对应字段
- 生成原子决定 LLM 怎么填：text→自由文本；select→从候选词选（需 gates）；slot→填槽位（需 slots）
- 后处理原子决定代码怎么加工：deterministic→正则替换+编号重排+空行归一化；enum_filter→枚举过滤；detect_report→检出上报；json_parse→JSON 解析
- 校验原子决定怎么判合规
- 同一字段可被多个原子共用

验证指标：取决于选的原子（值域命中率/纠偏编辑距离/钉死确定性/检出率等）
检测什么：由 recipe 的校验原子和观测原子决定
适用场景：5 种预置方式都不满足时，自由组合原子
输入类型：取决于 recipe 的生成原子
缺陷：config 字段必须与 recipe 原子匹配，填了没用的字段被忽略，漏了需要的字段原子报错""",
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
