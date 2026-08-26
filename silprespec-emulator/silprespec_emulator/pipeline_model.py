"""前置规范效果实验台 — 数据模型

用户从 8 种前置规范方式中选一种或多种，配置后对输入执行，观测：
  - 前置规范后填入了什么（真实填空内容）
  - 重试次数、是否撑满 max_retry、撑满失败次数
  - 命中/留空分布
  - 并行 N 次的重现性

8 种方式都是前置规范（作用在生成通道/填空出口），后置验证（任务完成后全量验证）不在本系统。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict

# 8 种前置规范方式
WAYS = [
    ("gate", "门禁·穷举词组（减法）", "08c论断三/四：未命中留空不block，显式未指定"),
    ("guide", "软引导·引导提示词", "08a§4：软引导不强制"),
    ("condense", "凝练+代码固定枚举拼接", "08c场景二：LLM凝练锚定禁泛化+代码枚举组合"),
    ("slot", "槽位限定+查多余编造", "08c场景一：填槽位+在填空出口查编造（前置）"),
    ("diverge", "发散+确定性纠偏", "08c场景三：放开生成+代码/RAG拉回"),
    ("deterministic", "确定性后处理（完全封死）", "08a§4.3：代码钉死，LLM零参与"),
    ("detect_report", "检出即上报", "08a§7：内容轴封不死，检出+标记+上报人工"),
    ("required_min", "required最小化", "08c§4.3：哪些槽位必填/可留空"),
]

# 8 种方式的默认任务提示词（系统提示词，泛化；用户可在 UI 编辑覆盖）
TASK_PROMPTS = {
    "gate": "按照以下要求，将用户输入分类。",
    "guide": "按照引导提示词，对用户输入给出填空结果。",
    "condense": "把用户输入浓缩为短词或短语。",
    "slot": "从用户输入中提取信息，填入指定槽位。",
    "diverge": "基于用户输入发散生成一段内容。",
    "deterministic": "生成一段内容，后续将被代码后处理钉死。",
    "detect_report": "生成一段可能含特定内容（如数值）的文本。",
    "required_min": "从用户输入中提取信息，必填字段必须有内容，可留空字段填未指定。",
}


WAY_HELPS = {
    "gate": """【门禁·穷举词组（减法）】
名词：门禁=分类关卡，把一个维度限定为有限候选词（如情绪=积极/消极/中性），LLM 从中选一个或填"未指定"。穷举词组=候选词是预定义的有限集。减法=未命中不 block，留空即可。
多道门禁 AND，每道内候选词 OR 命中。LLM 每道填一个词或"未指定"，不 block。
编造（填了不在候选词里的）才标记失败并重试。

表单：
- 门禁行：每行一道门禁（维度名 + 候选词逗号分隔）；点"+ 门禁"增加、"删"删除
- 允许未指定：勾选=允许填"未指定"（减法），不勾=必须命中候选词
- 多道门禁 AND，每道内候选词 OR 命中

观测：filled（每道填了什么）、hit/unspecified/fabricated 计数

检测什么：LLM 在穷举词组中能否命中，编造了什么词
适用场景：维度可穷举的有限分类（情绪/时态/语态/类别等）
输入类型：自然语言短文本（一句话/评论/短语），需归入有限候选词
缺陷：维度不可穷举时无法用；词组太少逼 LLM 硬造（PhantomFill）""",

    "guide": """【软引导·引导提示词】
名词：软引导=只给提示词引导方向，不限定候选集，不硬约束。LLM 自由填空但受引导影响。
给 LLM 引导提示词，让它填空。无硬约束，LLM 正常响应即成功。

表单：
- 引导提示词：文本框，给 LLM 的引导方向（如"围绕主题展开，保持语义一致"）

观测：filled.output（LLM 输出内容）

检测什么：LLM 在软引导下填了什么内容
适用场景：无法穷举的开放维度，只需引导方向
输入类型：自然语言文本（开放内容，如摘要/续写/改写）
缺陷：无硬约束，LLM 可能偏离引导""",

    "condense": """【凝练+代码固定枚举拼接】
名词：凝练=把长文本浓缩为短词/短语。代码枚举=用预定义允许词列表校验凝练结果。泛化=LLM 造了不在允许列表中的新词。
LLM 凝练为短词，代码枚举校验是否在允许集（禁泛化）。只保留在 enums 中的词或其子串，编造的丢弃。

表单：
- 凝练规则：文本框，给 LLM 的浓缩规则
- 枚举词：逗号分隔输入（如 环境治理,生态文明,绿色发展）；代码校验输出词必须是其中某词的子串或超串

观测：filled.condensed（通过校验的词）、filled.raw（原始输出）、fabricated_count

检测什么：LLM 凝练后是否泛化造新词
适用场景：把长文本浓缩为标准短语/标签/分类
输入类型：自然语言长文本（文章/段落/描述），输出需落在枚举词集
缺陷：enums 太少时 LLM 可能无法命中；子串匹配可能误判""",

    "slot": """【槽位限定+查多余编造】
名词：槽位=预定义的信息字段（如 who/what/why），LLM 从输入文本中提取信息填入。与门禁不同：门禁是从穷举词中"选"，槽位是从文本中"提取"，值是开放的。多余编造=LLM 输出了槽位定义以外的字段。
LLM 填指定槽位，代码检测是否有多余 key（编造）。有多余 key 则重试。

表单：
- 槽位行：每行一个槽位（槽位名 + 必填勾选）；点"+ 槽位"增加、"删"删除
- 注：slot 方式不强制检查必填，仅检查 LLM 是否编造了槽位以外的多余字段

观测：filled（各槽位填入）、extra_fabrication（多余 key 列表）

检测什么：LLM 是否编造了槽位以外的字段
适用场景：结构化抽取（who/what/why 等固定字段）
输入类型：自然语言文本（含可提取实体/事件的事实描述，如新闻/履历）
缺陷：槽位定义太死可能逼 LLM 硬填；不检查槽位内容质量""",

    "diverge": """【发散+确定性纠偏】
名词：发散=让 LLM 高温度自由生成，不限制方向。纠偏=生成后用代码（正则替换）把偏离的部分拉回。与门禁相反：门禁是生成前限制，纠偏是生成后修正。
LLM 高温度发散生成，代码按配置确定性纠偏（正则替换+空行归一化）。不设硬失败。

表单：
- 发散提示词：文本框，给 LLM 的发散方向
- 替换规则行：每行一条（正则 pattern + 替换文本）；点"+ 替换规则"增加、"删"删除；代码按顺序执行
- 空行归一化：勾选=多余空行压成双换行

观测：filled.raw（纠偏前）、filled.corrected（纠偏后）、changed（是否改过）

检测什么：LLM 发散生成后纠偏改了什么
适用场景：需要创意发散但有些维度要纠偏
输入类型：自然语言主题/提示词（创意生成，如故事/文案/扩写）
缺陷：纠偏规则有限（正则替换），复杂语义错误无法纠""",

    "deterministic": """【确定性后处理（完全封死）】
名词：钉死=用代码强制固定某些维度（如编号重排、格式归一化），LLM 零参与，百分百准确。封死=这些维度不再需要验证。与纠偏类似但更彻底：纠偏是部分修正，钉死是完全固定。
LLM 生成，代码按配置钉死（正则替换+编号重排+空行归一化）。代码部分百分百准确不需测试，测的是 LLM 原始输出——钉死前后差异反映 LLM 不可控程度。

表单：
- 替换规则行：每行一条（正则 pattern + 替换文本）；点"+ 替换规则"增加、"删"删除；代码按顺序执行
- 编号重排：勾选=来源编号重排为 1,2,3...
- 空行归一化：勾选=多余空行压成双换行

观测：filled.raw（钉死前）、filled.pinned（钉死后）、changed（是否改过）

检测什么：LLM 原始输出中需要钉死的内容（编号/格式），钉死前后差异
适用场景：某些维度可代码封死（编号/格式/空行），减少需验证维度
输入类型：自然语言文本（含来源编号/引用标记/多空行等格式噪声，需钉死格式）
缺陷：只能封死格式轴（编号/空行），内容轴无法封死""",

    "detect_report": r"""【检出即上报】
名词：检出=用正则扫描 LLM 输出中的特定内容（如数值）。上报=检出项不在合法值集（allowed_values）中则标记人工复审，不阻塞生成。与封死相反：封不死的内容轴只能检出后交给人工。
LLM 生成，代码按配置检出内容（正则），对照 allowed_values 标记未命中的为上报。不阻塞。

表单：
- 检出正则：输入框，匹配需要审核的内容（如 \d+(?:\.\d+)?(%|亿|万|元|人次)）
- 合法值：逗号分隔输入（如 100%,3.5亿）；配了则只标记不在此列表的检出项，不配则标记所有检出项
- 上报标签：输入框，上报标记文本（如"建议人工复审"）

观测：filled.raw（原始输出）、filled.flagged（检出列表，含 value/pos/report/unmatched）、flagged_count/unmatched_count

检测什么：LLM 输出中检出项（如数值）是否在合法值集
适用场景：内容轴封不死（数值/事实），需人工审核
输入类型：自然语言文本（含数值/事实陈述，如报告/新闻/统计描述）
缺陷：正则检出有限；allowed_values 需人工维护""",

    "required_min": """【required 最小化】
名词：required=必填字段。最小化=只设最少必填字段，其余可留空（填"未指定"）。与 slot 互补：slot 查"多"（编造多余字段），required_min 查"少"（必填字段缺失）。
只设最少 required 槽必填，可留空槽填"未指定"。required 槽没填或填"未指定"则重试。

表单：
- 槽位行：每行一个槽位（槽位名 + 必填勾选）；点"+ 槽位"增加、"删"删除
- 必填勾选：勾=必须有内容，不勾=可留空（填"未指定"）

观测：filled（各槽位填入）、required_count/optional_count/left_empty

检测什么：required 槽是否都填了，可留空槽填了什么
适用场景：部分字段必填、部分可留空的场景
输入类型：自然语言文本（含可提取字段的事实描述，部分字段可能缺失）
    缺陷：只检查有无内容，不检查内容质量""",

    "custom": r"""【自定义模板·自由组合原子】
名词：自定义模板=从 10 个原子中自由选组合，配出自己的前置规范方式。recipe（原子配方）决定执行哪些原子，config（配置 JSON）提供这些原子需要的参数。

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
- none：不校验，直接通过
- hit：命中分布，统计每道门禁命中/未指定/编造
- fabricated：编造统计，统计造了不在允许集的词
- extra_keys：多余字段，列出 LLM 编造的槽位以外的 key
- left_empty：留空统计，统计必填/可留空/实际留空数
- flagged：检出统计，统计检出项数和未命中白名单数
- changed：改过标记，对比后处理前后是否改过

recipe 表单和 config JSON 的配合：
- recipe 选了什么原子 → config 里就要填对应字段
- 生成原子决定 LLM 怎么填：text→自由文本（需 guide_prompt 或 diverge_prompt 或 condense_rule）；select→从候选词选（需 gates）；slot→填槽位（需 slots）
- 后处理原子决定代码怎么加工：deterministic→正则替换+编号重排+空行归一化（需 regex_replaces/renumber_source/normalize_blanklines）；enum_filter→枚举过滤（需 enums）；detect_report→检出上报（需 detect_pattern/allowed_values/report_label）
- 校验原子决定怎么判合规：in_set→候选词命中（需 gates）；no_extra→查多余字段（需 slots）；required_full→必填检查（需 slots）；in_range→区间容差（需 allowed_values）；eq_exact→精确相等（需 allowed_values）；none→不校验
- 同一字段可被多个原子共用（如 gates 同时给 gen_select 和 validate in_set 用）

config JSON 全部可用字段：
- guide_prompt：字符串，引导提示词（gen_text 用）
- diverge_prompt：字符串，发散提示词（gen_text 用，与 guide_prompt 二选一）
- condense_rule：字符串，凝练规则提示词（gen_text 用）
- gates：数组，每项 {name, words, logic}，门禁维度+候选词+逻辑（gen_select / validate in_set 用）
- slots：数组，每项 {name, required}，槽位名+是否必填（gen_slot / validate no_extra / validate required_full 用）
- enums：字符串数组，允许的候选词列表（apply_enum_filter 用）
- regex_replaces：数组，每项 {pattern, replace}，正则替换规则（apply_deterministic 用）
- renumber_source：布尔，来源编号重排为 1,2,3...（apply_deterministic 用）
- normalize_blanklines：布尔，多余空行归一化为双换行（apply_deterministic 用）
- detect_pattern：字符串，检出正则（apply_detect_report 用）
- allowed_values：字符串数组，合法值列表（apply_detect_report / validate in_range / validate eq_exact 用）
- report_label：字符串，上报标记文本（apply_detect_report 用）

观测：取决于 recipe 选的观测原子（hit/fabricated/extra_keys/left_empty/flagged/changed）

检测什么：由 recipe 的校验原子和观测原子决定
适用场景：8 种预置方式都不满足时，自由组合原子
输入类型：取决于 recipe 的生成原子
缺陷：config 字段必须与 recipe 原子匹配，填了没用的字段被忽略，漏了需要的字段原子报错""",
}

# 空坐标形态由 validate 原子承载（in_set=点对面 / in_range=面对面 / eq_exact=点对点 / none=不校验）


@dataclass
class WayConfig:
    """一种前置规范方式的配置"""
    way: str = "gate"             # 方式 id
    enabled: bool = True
    config: dict = field(default_factory=dict)  # 该方式专属配置（见 default_config）
    max_retry: int = 3
    task_prompt: str = ""         # 任务提示词（系统提示词）；空则用 TASK_PROMPTS[way]
    recipe: dict = field(default_factory=dict)  # 自定义原子配方；空则用 WAY_RECIPES[way]
    template_id: str = ""         # 自定义模板库 id（仅 UI 标记；执行用 way=custom + recipe）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "WayConfig":
        return WayConfig(
            way=d.get("way", "gate"),
            enabled=d.get("enabled", True),
            config=d.get("config", {}),
            max_retry=d.get("max_retry", 3),
            task_prompt=d.get("task_prompt", ""),
            recipe=d.get("recipe", {}),
            template_id=d.get("template_id", ""),
        )


def default_config(way: str) -> dict:
    """每种方式的默认配置 schema（供 UI 渲染）"""
    if way == "gate":
        return {"gates": [{"name": "情绪", "words": ["积极", "消极", "中性"], "logic": "or"}],
                "allow_unspecified": True}
    if way == "guide":
        return {"guide_prompt": "围绕主题展开，保持语义一致"}
    if way == "condense":
        return {"condense_rule": "浓缩为短词，禁止泛化造新词",
                "enums": ["环境治理", "生态文明", "绿色发展"]}
    if way == "slot":
        return {"slots": [{"name": "who", "required": True}, {"name": "what", "required": True},
                          {"name": "why", "required": False}]}
    if way == "diverge":
        return {"diverge_prompt": "自由发散生成",
                "regex_replaces": [{"pattern": r"【引用自来源\d+】", "replace": ""}],
                "normalize_blanklines": True}
    if way == "deterministic":
        return {"regex_replaces": [{"pattern": r"【引用自来源\d+】", "replace": ""}],
                "renumber_source": True, "normalize_blanklines": True}
    if way == "detect_report":
        return {"detect_pattern": r"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)",
                "allowed_values": [], "report_label": "建议人工复审"}
    if way == "required_min":
        return {"slots": [{"name": "entity", "required": True},
                          {"name": "attr", "required": True},
                          {"name": "rel", "required": False}]}
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
            description="选一种或多种前置规范方式，观测填入内容/重试/撑满失败",
            ways=[
                WayConfig(way="gate", config=default_config("gate"), max_retry=3),
                WayConfig(way="guide", config=default_config("guide"), max_retry=3),
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
    filled: dict = field(default_factory=dict)    # 实际填入内容（门禁填了什么词、凝练成什么...）
    retry_count: int = 0
    exhausted: bool = False           # 是否撑满 max_retry 仍失败
    attempts: list = field(default_factory=list)  # 每次尝试的填入内容（看重试过程）
    extra: dict = field(default_factory=dict)     # 各方式专属观测（命中分布/编造项/纠偏前后...）
    error: str = ""

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
    distinct_fills: list = field(default_factory=list)  # 出现过的不同 filled（去重）
    consistency: float = 0.0          # 最高频 filled 出现次数 / 总次数

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
