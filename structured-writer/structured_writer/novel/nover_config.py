LENGTH_TARGETS = {
    "short": (1000, 1500),
    "medium": (1500, 2000),
    "long": (2000, 4000),
}

LENGTH_LABELS = {
    "short": "短篇(3-6章)",
    "medium": "中篇(8-10章)",
    "long": "长篇(11-20章)",
}

LENGTH_CHAPTERS = {
    "short": (3, 6),
    "medium": (8, 10),
    "long": (11, 20),
}

KEY_UPSCALE = 1.5

JUDGE_TEMPERATURE = 0.2

JUDGE_MAX_TOKENS = 1024

REPAIR_WORD_TOLERANCE = 0.15

DEFAULT_NOVEL_STYLE = (
    "文风六字段模板：叙事视角=meta.叙事视角；时态=过去式为主；句式=长短句交错；"
    "词汇=文学化；描写=中等密度(环境描写每段≤2句)。"
    "创作铁律：1)show don't tell，情绪通过人物行为/生理反应表达，禁止纯抒情段落；"
    "2)对话必须符合角色身份与人格；3)禁止元文本引用；"
    "4)禁止第三人称插入叙述(除非对白转述)。"
)