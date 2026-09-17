#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""脚本引擎：前置模板 + LLM 填空 + 门禁 + 六检 + 回灌重试 + 锁文件。

分工（确定性特权）：
    LLM 只填两个空 —— 台词文本、情绪标签（从受控词表选）
    其余全部由代码完成 —— 结构、编号、字数统计、时长估算、格式归一、验收

两条数字纪律（09e）：
    prompt 里的数字是**生成引导量**（LLM 可操作：字数、句数）
    GATE_SPEC 里的数字是**验收判据**（代码执行，LLM 无权判断自己是否达标）
    时长、偏差、断词率等 LLM 无法自测的量，一律不出现在 prompt 中。
"""

import copy
import json
import os
import re
from datetime import datetime

from . import audio_engine, duration_model, paradigms, probe
from .config_manager import (BANNED_RULES, DISCOURSE_VOCAB, EMOTION_TAGS,
                             GATE_BY_KEY, INTRO_OUTRO, PRESET_SPEC, STYLE_DIMS)

DEFAULT_EMOTION = "平静"


def material_capacity(cfg):
    """一期素材的**输入容量**（字）：成稿目标 × 偏移量 1.25 × 压缩档。

    口径（全链唯一的算法源）：**1 = 成稿目标**（每期时长 × 标准语速，不含
    偏移——它对应人设定的时长换算出的真实字数）；**x = 1 × 1.25 × 档位**；
    下限 = 1 × 1.25 × 1.2（素材至少多出偏移后脚本两成，写作才有「压缩」
    可言）。偏移量 1.25 只进算式、不改变"1"。与排图体检（planner 的
    per_ep_max）同一把尺：排图放进去的一期素材最多就这个量，写作端用同一
    口径收，两边不会互相打架。
    """
    return int(probe.capacity(cfg) * probe.RATIO_HEADROOM)

#: 「不写心情」那一档 `emotion` 的合法集合：语篇功能标签 + 平静。
#: 平静要放进来——它是归并非法值时的落点，也是这个项目的默认底色。不许它，
#: 归一化把越界值改成「平静」之后又会被门禁判越界，那一句就卡死了、改不动。
NONE_LEVEL_EMOTIONS = DISCOURSE_VOCAB + [DEFAULT_EMOTION]

# 风格维度落到可验证形态：prompt 给引导，代码给判据
#
# 只有「情绪密度」这一维按档位分两套话术，其余四维不分档：体裁、提问频率、
# 比喻密度、互动词强度跟卡的天花板没有关系，牵连它们等于把一张卡的作用放大到
# 它管不着的地方。
STYLE_GUIDE = {
    "emotion_density": {
        # 卡说这批不写心情：这一维说的是**功能标签**怎么分布。
        # 从前这里不分档，于是同一份提示词里一边躺着「情绪标签适度起伏，「平静」
        # 类约占一半」，一边躺着卡上的「这批不写心情……不填任何心情词」——同一个
        # 字段上一个说必须填、一个说禁止填，模型只能二选一，档位等于作废。
        "none": {
            "low": "标签以「平静」「解释」「总结」为主",
            "mid": "标签适度起伏，「平静」类约占一半",
            "high": "标签勤换，「平静」类不超过四成",
        },
        # 卡允许略情绪：这一维才说情绪起伏多大。
        "light": {
            "low": "情绪标签以「平静」「解释」「总结」为主",
            "mid": "情绪标签适度起伏，「平静」类约占一半",
            "high": "情绪标签至少六成为非平静类",
        },
    },
    "question_rate": {
        "low": "全篇问句不超过两处",
        "mid": "每四到五句有一处问句",
        "high": "每两句到三句有一处问句",
    },
    "metaphor_density": {
        "low": "不使用比喻",
        "mid": "全篇两到三处比喻",
        "high": "每三到四句有一处比喻或类比",
    },
    "interaction": {
        "restrained": "不使用寒暄类互动词",
        "natural": "少量使用「你看」「你想」这类衔接",
        "warm": "较多使用「对吧」「你想想」「有意思」这类互动",
    },
    "genre": {
        "argument": "以论点推进为主线：提出判断、给出依据、收束结论",
        "story": "以具体事例推进：从场景或经历进入，再落到判断",
        "science": "以机制解释为主线：从现象到原理，逐层拆解",
        "debate": "以观点交锋为主线：A 提出主张，B 质疑或补充",
        "review": "以复盘为主线：先给结果，再回溯过程与取舍",
    },
}

#: 情绪密度这一维在 none 档下的名字。维度名跟着话术一起换：措辞已经是「标签怎么
#: 分布」，表头还写「情绪密度」，模型会把「情绪」两个字自己读回去，等于没换。
#: 界面那一栏仍叫「情绪密度」——那是风格维度的名字，人选的也是它，不动。
EMOTION_DENSITY_LABEL = {"none": "标签分布", "light": "情绪密度"}


def emotion_vocab(level):
    """这一档 `emotion` 的合法集合。

    三处（写脚本提示词、定点修补提示词、两处约束解码 schema）都从这里取，
    不各写一份 `if level == "none"`——分开写，加档位那天必然漏一处。
    """
    lv = level if level in paradigms.EMOTION_LEVELS else paradigms.DEFAULT_EMOTION_LEVEL
    return NONE_LEVEL_EMOTIONS if lv == "none" else EMOTION_TAGS


def style_dim_row(dim, val, level):
    """风格倾向取一行「- 维度名：要求」。只有情绪密度按档位换话术与维度名。"""
    if dim == "emotion_density":
        guide = STYLE_GUIDE[dim].get(level, {}).get(val, "")
        label = EMOTION_DENSITY_LABEL.get(level, STYLE_DIMS[dim]["label"])
    else:
        guide = STYLE_GUIDE.get(dim, {}).get(val, "")
        label = STYLE_DIMS.get(dim, {}).get("label", dim)
    return "- %s：%s" % (label, guide)


# LLM 输出的结构 schema（用于约束解码；后端不支持时降级为提示词约束）
#
# 顶层是对象而不是数组：标题必须与正文同一次产出。让用户合成时再填标题，
# 等于把人塞回流程中间——脚本都写完了，标题早就该定了，那一栏是「改」不是「填」。
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "planned_episodes": {"type": "integer"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "speaker": {"type": "string", "enum": ["A", "B"]},
                    "text": {"type": "string"},
                    "emotion": {"type": "string", "enum": EMOTION_TAGS},
                },
                "required": ["speaker", "text", "emotion"],
            },
        },
    },
    "required": ["title", "lines"],
}


def script_schema(level=None):
    """脚本输出的结构 schema，`emotion` 的枚举按档位收窄。

    档位是硬天花板，就不该只靠两件软的事撑着——提示词里劝、门禁事后抓。schema
    是约束解码的入手处：none 档把心情词从枚举里去掉，模型**当场就填不出来**，
    不必等它填完再罚一轮定点修补。后端不支持约束解码时这层会降级（提示词约束
    那一路仍是劝），但能约束的后端上，这是唯一能让模型连「想填」的机会都没有的
    地方。
    """
    schema = copy.deepcopy(SCRIPT_SCHEMA)
    schema["properties"]["lines"]["items"]["properties"]["emotion"]["enum"] = list(
        emotion_vocab(level))
    return schema


# 标题长度上限（字）。标题要能塞进封面最大字号还不折行，太长就只好缩字号，
# 于是每期封面的主视觉大小都不一样。限长是排版约束，不是文风偏好。
TITLE_MAX = 16


# 提示词三查自查表（09e）：构建的提示词自身须过此关
PROMPT_AUDIT = [
    "每条指令能填进「范围 / 位置 / 判据」三格",
    "不给 LLM 它无法自测的量（秒数、百分比、断词率）",
    "不给 LLM 需要它自行判断对错的验收标准",
    "删除只有语气没有结构的句子",
]


# ------------------------------------------------------------------ 可朗读
# 台词是唯一进 TTS 的字段，判据盯「TTS 拿到什么字符会坏」，与素材来源无关。
# emoji 与图形符号按 Unicode 码位块判：docx 从网页复制会带零宽字符，txt 里
# 贴什么都有，✅ ❌ 在任何格式里都是同一个字符；网址与命令参数在任何来源都
# 可能出现。反引号、星号、竖线是 Markdown 痕迹（把一份 md 存成 txt 就会出现），
# 对别的来源永不命中，留作无害保险。中文破折号「——」是 em dash（U+2014），
# 范围号「3-5」是单个 ASCII 连字符，都不是命令参数的两个连字符，不会误伤；
# 英文单词念得出来，不在判据内。
READABLE_RULE = ("台词中不可出现无法朗读的内容：emoji（如 ✅ ❌）、图标、表格、"
                 "网址（如 https://…）、命令及参数（如 --mode）。")
UNREADABLE_SHAPES = [
    ("emoji", "emoji 或图标字符",
     re.compile("[\u2190-\u21ff\u2300-\u27bf\u2b00-\u2bff"
                "\U0001f000-\U0001faff]")),
    ("invisible", "不可见字符",
     re.compile("[\u00a0\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")),
    ("url", "网址",
     re.compile(r"https?://\S+|www\.[^\s，。；、！？]+")),
    ("cli_flag", "命令参数",
     re.compile(r"(?<![\w-])--[\w][\w-]*")),
    ("md_trace", "排版符号", re.compile("[`*|]")),
]


def unreadable_hits(text):
    """扫一句台词里的无法朗读形状，命中返回 [{kind, label, sample}]。

    只判形状（字符码位与固定写法），不判语感——「念着像念说明书」归语义检
    或人耳；形状是确定性的，代码判一次就是一次，跑多少遍都一样。
    """
    hits = []
    for kind, label, rx in UNREADABLE_SHAPES:
        m = rx.search(text or "")
        if m:
            hits.append({"kind": kind, "label": label, "sample": m.group(0)})
    return hits


class ScriptError(RuntimeError):
    """脚本生成失败或被门禁阻断。"""


# ------------------------------------------------------------------ 提示词
def format_banned_rules():
    """渲染措辞禁忌：分组 + 为什么 + 换成什么。

    只给一份禁用名单是不够的——模型知道要绕开哪些词，却不知道绕去哪里，
    只好换成一个意思相同、照样把话说满的说法，等于把同一个毛病换了层壳。
    落点与理由一起给出，这条约束才是可执行的；这也是「前置封住」的意义：
    等写完再拿词表去卡，模型没有先验，命中与否全看运气。
    """
    rows = []
    for r in BANNED_RULES:
        row = "   - %s：不用%s" % (
            r["label"], "、".join("「%s」" % w for w in r["words"]))
        if r.get("substitute"):
            row += "；换成%s" % r["substitute"]
        if r.get("why"):
            row += "。（%s）" % r["why"]
        rows.append(row)
    return "\n".join(rows)


def resolve_paradigm(project, cfg):
    """写脚本这一步用哪张范式卡。

    顺序取排图/凝缩同一口径的**前两档**：项目已定的 > 全局默认 > `auto`。
    没有第三档「探查推断」——那一步在排图时已经定过，脚本这一步只消费结论。
    两处各判一次，同一份素材会拿到两张不同的卡：排图按 A 切，写脚本按 B 说。
    """
    return paradigms.get((project or {}).get("paradigm")
                         or (cfg or {}).get("script.paradigm") or "auto")


def build_system_prompt(cfg, preset_key, target_chars, line_count, project=None,
                        paradigm=None):
    """拼写脚本的系统提示词。

    `paradigm` 是范式卡（`resolve_paradigm()` 的产物）：它决定两人的站位与分工
    （`cast`）、情绪基调（`emotion`）、同一人连续句数上限（`max_run`）。不传
    就在这里现取一张——取的口径只有 `resolve_paradigm()` 一处。
    """
    name_a = cfg.get("tts.name_a", "A")
    name_b = cfg.get("tts.name_b", "B")
    preset = PRESET_SPEC.get(preset_key) or PRESET_SPEC["argument"]
    io = intro_outro_spec(cfg)
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    banned_block = format_banned_rules()
    card = paradigm or resolve_paradigm(project, cfg)
    max_run = paradigms.max_run_of(card)
    card_block = paradigms.script_block(card)

    # 情绪档位决定 `emotion` 字段能填什么。枚举只在这里列一次——档位与它同源，
    # 卡上的「情绪到哪为止」也是从同一个值生成的，不许写侧与说侧各认一套。
    level = paradigms.emotion_level_of(card)
    emo_rule, emo_allowed = _emotion_rule(level)

    # 项目的期数计划：人定了就以人为准。没定也不是「让模型建议一个」——
    # 总期数由期数地图的合并与切分决定（见 `planner.plan_map`），脚本这一步
    # 编出来的数只会混进产物里冒充计划，而且模型每次给的都不一样。
    project = project or {}
    plan = project.get("planned_episodes")
    no = str(project.get("episode_no") or "").strip()
    if plan:
        plan_hint = ("本项目已定共 %d 期，本期是第 %s 期。"
                     "原样填 %d，不要改动。" % (int(plan), no or "?", int(plan)))
    else:
        plan_hint = ("本项目尚未排出期数地图，总期数未定：原样填 0，"
                     "不要自己给一个数。")

    # 地图条目：本期讲什么。有地图的项目里这是「本期内容」的唯一来源，
    # 脚本页不必再手填一遍要点——它已经由排地图那一步定下了。
    # 主旨（gist）与取材单元也一并给出：稿子为什么被划成这一期，在排图那一步
    # 已经定过，写的时候不必让模型自己再从素材里猜一遍主题。
    episode_block = _episode_block(project)

    # 风格倾向五行按档位取：只有情绪密度那一行动（见 STYLE_GUIDE 的注释）。
    style_rows = [style_dim_row(dim, preset.get(dim), level)
                  for dim in ("genre", "emotion_density", "question_rate",
                              "metaphor_density", "interaction")]

    # 本期计划与范式卡连着给：都属「已经定下的事」，中间不夹别的段落。
    middle = "\n\n".join(x for x in (episode_block.strip(), card_block) if x)
    if middle:
        middle += "\n\n"

    # 站位写在卡上就从卡上取；卡上没写（自定义卡留空）才退回内置的分工口径。
    hosts = _hosts_text(card, cfg)

    return """你负责把素材改写成两位主持人的对话脚本。

【固定结构】（不可改动；首末两句最终由程序按模板补齐）
全篇共 %d 句左右。
第 1 句固定为 —— A：%s
最后 1 句固定为 —— B：%s
其余为正文。**每一句都要自己判断是谁在说**——由这句话的内容决定，不是由它
排在第几句决定。
（这两句照抄即可，不要改写、不要加书名号以外的装饰。）

【输出格式】（硬性契约，逐条满足）
只输出一个 JSON 对象。不要任何解释文字，不要代码块标记，不要在对象前后写任何内容。
对象固定三个字段，顺序如下：
{"title": "本期标题", "planned_episodes": 0, "lines": [{"speaker": "A", "text": "台词正文，只写要念出来的话", "emotion": "平静"}]}
- title：本期标题，%d 字以内，概括本期主旨。不要书名号、引号、期号，不要写成完整句子
- planned_episodes：%s
- lines：正文数组，元素固定三个字段。**每个字段只装它自己的东西，互不串场**：
  - speaker：只装一个大写字母 "A" 或 "B"。人名、称呼、其他字母一律不进。
  - text：只装**会被逐字合成语音念出来的台词正文**。text 里的每个字都会被
    念出来——所以下面这些一律不进 text：标签词（「解释」「追问」「强调」
    「比喻」这类，它们属于 emotion 字段）、说话人标记（「A:」「B:」）、任何
    形式的标签、注释或说明。
    正确：{"speaker": "A", "text": "AI接管体力劳动之后，人该往哪走？", "emotion": "追问"}
    错误：{"speaker": "A", "text": "追问AI接管体力劳动之后，人该往哪走？", "emotion": "追问"}
    （「追问」混进了 text 开头——这就是错误示范，标签只能待在 emotion 字段里）
  - emotion：只装标签词，从下面的词表选一个，规则：%s
%s

%s【当前任务：生成要求】（逐条满足；本期的验收标准）
1. 每句台词在 %d 到 %d 字之间。
2. 全篇台词合计约 %d 字。
3. 说话人由**内容**定，不是由位置定：一句完整的话（一次回答、一段说明、
   一个例子）由同一个人说完，**不许拆给两个人**；分工只说明各自的主场，
   不要求每句对调。同一人最多连着说 %d 句。
4. 台词里的每个论断都要能对应到素材内容，不添加素材之外的事实、数据或来源。
5. 措辞禁忌（全篇写完后再逐句回查一遍，命中就当场改掉）。每组给了替代说法，
   照着换；不要换成一个意思相同、照样把话说满的说法，那只是把同一个毛病换了层壳。
%s
6. %s

【风格倾向】（全篇通用背景，与本期具体内容无关；与「生成要求」冲突时以生成要求为准）
%s

【两位主持人】（既定阵容，不由你指定）
%s
""" % (
        line_count,
        io["intro_first"].format(program=program_name_of(cfg)),
        io["outro_last"].format(program=program_name_of(cfg)),
        TITLE_MAX,
        plan_hint,
        emo_rule,
        "、".join(emo_allowed),
        middle,
        min_c, max_c, int(target_chars),
        max_run,
        banned_block,
        READABLE_RULE,
        "\n".join(style_rows),
        hosts,
    )


def build_user_prompt(material, cfg, extra, previous_report=None,
                      previous_lines=None):
    """拼用户提示词。回灌重写时把上一版正文一并带上。

    带正文不是为了好看：反馈点到「第 58 句命中」，模型却看不到第 58 句原本写了
    什么，就只能凭印象通篇重写——重写出来的新句又会带进新的命中，上一轮改掉的
    地方白改，几轮全耗在打地鼠上。给了原稿，才是「只改这一句」。
    """
    parts = []
    if extra:
        parts.append("【补充说明】（用户指定，优先采用）\n%s" % extra)
    parts.append("【素材】（改写依据；台词里的每个论断都要能对应到这里，"
                 "不添加素材之外的事实、数据或来源）\n%s" % material)
    if previous_report:
        parts.append(
            "【上一版正文】（这一版没过校验，下面是它的全文，供你对照）\n%s\n\n"
            "【要改的地方】（当前任务：按这里逐条改）\n%s\n\n"
            "这是**定点修补**，不是重写：「第 N 句」指的就是上面正文里的第 N 行，"
            "只改这几行，没提到的行逐字照抄。\n"
            "输出仍是完整的 JSON 对象——全篇都在，不能只给改动的那几句。"
            % (previous_lines or "（未提供）", previous_report))
    parts.append("现在输出 JSON 对象。")
    return "\n\n".join(parts)


def estimate_line_count(cfg, target_chars):
    """由目标字数估算句数（平均句长取字数区间中值）。"""
    lo = int(cfg.get("gate.min_chars", 8))
    hi = int(cfg.get("gate.max_chars", 40))
    avg = max(6.0, (lo + hi) / 2.0 * 0.75)
    return max(8, int(round(target_chars / avg)))


# ------------------------------------------------------------------ 解析
def parse_script(raw):
    """从 LLM 原始输出里解析脚本。解析失败抛 ScriptError。

    返回 {"title": str, "planned_episodes": int|None, "lines": [...]}。
    顶层是对象是现在的约定；模型偶尔仍会只吐一个数组（降级提示词约束时），
    这种情况按「无标题」接住——格式可以宽容，内容一律过门禁。
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    arr = text.find("[")
    if start != -1 and (arr == -1 or start < arr):
        end = text.rfind("}")
        if end <= start:
            raise ScriptError("模型输出中没有找到完整 JSON 对象。")
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ScriptError("模型输出的 JSON 无法解析：%s" % e)
        if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
            raise ScriptError("模型输出的对象里没有 lines 数组。")
        if not all(isinstance(x, dict) for x in data["lines"]):
            # 非对象条目一律在这里拦。放过去的话后处理会把它们全丢掉，
            # 症状变成「脚本是空的」，离真正的错处隔了整整一个阶段。
            raise ScriptError("lines 里有不是对象的条目。")
        title = str(data.get("title") or "").strip()
        plan = data.get("planned_episodes")
        try:
            plan = int(plan) if plan not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            plan = None
        return {"title": clean_title(title), "planned_episodes": plan,
                "lines": data["lines"]}

    s2, e2 = text.find("["), text.rfind("]")
    if s2 == -1 or e2 == -1 or e2 <= s2:
        raise ScriptError("模型输出中没有找到 JSON。")
    try:
        data = json.loads(text[s2:e2 + 1])
    except json.JSONDecodeError as e:
        raise ScriptError("模型输出的 JSON 无法解析：%s" % e)
    if not isinstance(data, list) or not data:
        raise ScriptError("模型输出的不是非空数组。")
    if not all(isinstance(x, dict) for x in data):
        raise ScriptError("数组里有不是对象的条目。")
    return {"title": "", "planned_episodes": None, "lines": data}


def clean_title(raw):
    """标题收束：去包裹符号、去期号前缀、限长。格式轴的事由代码办。"""
    t = re.sub(r"\s+", "", str(raw or ""))
    t = t.strip("《》〈〉「」『』\"'“”‘’【】 []")
    t = re.sub(r"^第\s*\d+\s*期[·:：\-—\s]*", "", t)
    return t[:24]


def title_from_lines(lines):
    """没有标题时的兜底：取正文里最像主旨的一句，截断成短标题。

    不用第一个非片头句——片头是写死的问候，当标题毫无信息量。
    """
    for item in (lines or [])[2:6]:
        text = str(item.get("text", "")).strip("。！？!?，,、；;：: ")
        if 6 <= len(text) <= 24:
            return text
    for item in (lines or []):
        text = str(item.get("text", "")).strip("。！？!?，,、；;：: ")
        if len(text) >= 6:
            return text[:24]
    return ""


def _strip_label_prefix(text, emotion):
    """把模型拼进 text 开头的 emotion 标签剥掉。

    分段写作一上线，模型开始把标签整批拼进正文开头（「解释AI正在…」「追问
    面对…」），TTS 原样念出来——标签是元数据不是台词。只剥**本句自己的**
    标签：别的标签开头可能是正常内容（emotion=平静 的「比喻是人类最好的
    思维拐杖」），碰了就是篡改。剥完为空按空句丢弃规则走。
    """
    if emotion and text.startswith(emotion):
        return text[len(emotion):].lstrip("：:，,、·-—")
    return text


def normalize_script(data, cfg):
    """格式确定性后处理（08b：格式轴硬编码收束）。

    - 字段缺失即补位（speaker 缺省承接上一句、emotion 归并到词表、text 去空白）
    - text 以本句 emotion 标签开头的剥掉标签（模型把标签拼进正文，TTS 会念）
    - 空 text 的条目直接丢弃
    - estimated_seconds 由时长模型按标准语速计算，丢弃 LLM 给的任何数字
    """
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker", "")).strip().upper()
        if speaker not in ("A", "B"):
            # 漏标不等于「轮到另一个」：位置不是身份。缺省按**承接上一句**补
            # （首句没有上一句，才落 A）。从前按 `i % 2` 补，等于把「谁在说」
            # 定义成「第几句」——模型漏一个字母，一个人的半句话就被判给了对方。
            speaker = out[-1]["speaker"] if out else "A"
        emotion = str(item.get("emotion", "")).strip()
        if emotion not in EMOTION_TAGS:
            emotion = DEFAULT_EMOTION
        text = str(item.get("text", "")).strip()
        text = re.sub(r"\s+", "", text)
        text = _strip_label_prefix(text, emotion)
        if not text:
            continue
        out.append({
            "speaker": speaker,
            "text": text,
            "emotion": emotion,
            "estimated_seconds": 0,
        })
    for item in out:
        item["estimated_seconds"] = int(round(
            duration_model.estimate_line(item["text"], item["speaker"], cfg)
        ))
    return out


def enforce_max_run(script, max_run):
    """把同一人的连续句数压到上限之内，返回被改的句号。

    上限由范式卡给（对话录允许被访者连说四句，一问一答的论述类两句就够）。
    这是格式轴的后处理，不是内容决策——内容仍由 LLM 写；正因为它不改内容，
    上限必须**宽到不需要它动刀**：靠它把稿子掰成交替，掰出来的就是两个人
    在念同一段话，听感上「一个人的话被切成两个人」正是这么来的。
    """
    max_run = max(1, int(max_run or 1))
    flips = []
    run = 1
    for i in range(1, len(script)):
        if script[i]["speaker"] == script[i - 1]["speaker"]:
            run += 1
            if run > max_run:
                script[i]["speaker"] = "B" if script[i - 1]["speaker"] == "A" else "A"
                flips.append(i + 1)
                run = 1
        else:
            run = 1
    return flips



# ------------------------------------------------------------------ 门禁
# 片头/片尾的判定特征。要求逐字复述模板会把模型的正常改写判成失败，
# 那是把提示词当成了判据。判据是结构特征：问候语（或收束语）加节目名。
INTRO_MARKS = ("欢迎", "大家好", "你好", "各位好", "这里是")
OUTRO_MARKS = ("关注", "订阅", "下期", "再见", "收听", "下期见")


#: 判重的最短长度。短句（「对吧」「是的」）重复本身就是口语形态，把它也算
#: 重复，会把正常的一问一答删成残句。只有成段的长句重复才是「拿旧段落凑字数」
#: 的形态——实测那次事故里，重复的整句最短 24 字。
DEDUPE_MIN_CHARS = 12
#: 归一化只留字：空白、标点、下划线一律去掉，汉字与字母数字保留。
#: 用 `\W` 而不是列一张标点表——标点表永远列不全，而「只比字」这个口径
#: 一句话就说清了。
_DEDUPE_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


def _dedupe_key(text):
    """判重用的骨架：去掉空白与标点，只比字。

    标点与语气差异不该让同一句话逃过判重——实测的重复形态是「把写过的整段
    再背一遍」，背的时候标点未必逐字一致。反过来，只差一两个虚词的句子仍
    算两句：那是改写，该由内容检判，程序不越权删。
    """
    return _DEDUPE_STRIP.sub("", str(text or ""))


def dedupe_script(script, min_chars=DEDUPE_MIN_CHARS):
    """程序硬去重：同一句长句在整篇里出现多次时，只留第一次。

    这件事本该由提示词与规划防住，但不该只靠模型自觉：实测整篇生成 302 句里
    有 129 句（43%）是把自己写过的整段又背了一遍。这类重复不是「改写」而是
    「复播」——留着只会让成片多出一段时间在说同一件事，删掉没有任何信息损失。

    程序删得掉，且不花一次模型调用，所以放在门禁之前：让时长、句数这些门禁
    对着**真实的稿子**判，而不是对着注水后的数字判——对注水稿判「达标」，正是
    这次事故的样子。

    认定口径只有一条：归一化后完全相同的长句。措辞相近的不同句子不删（那是
    改写），短句不删（口语本来的样子）。返回 (新脚本, 删掉的句数)。
    """
    seen, out, dropped = set(), [], 0
    for item in (script or []):
        key = _dedupe_key(item.get("text"))
        if len(key) >= min_chars:
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
        out.append(item)
    return out, dropped


def intro_outro_spec(cfg):
    """按配置取片头尾档位。档位必须真的生效，不能只在表里存在。"""
    key = cfg.get("intro_outro.preset", "standard")
    return INTRO_OUTRO.get(key) or INTRO_OUTRO["standard"]


def program_name_of(cfg):
    """节目名的唯一取值口径。

    空值一律回落到「播客」。落不回的话片头尾判据会因为一个空字符串恒为假，
    症状看起来像模型没照做，其实是我们自己算出来的——这种失败最难查，
    因为提示词里那一行也是空的，看上去毫无异常。
    """
    return (cfg.get("project.program_name") or "").strip() or "播客"


def pin_intro_outro(script, cfg):
    """把片头首句与片尾末句按模板写死，返回改写的句数。

    片头尾是节目的固定标识，各期必须逐字一致。交给模型复述就必然措辞漂移，
    再拿门禁去验等于抽奖：写对了是运气，写错了白烧一轮重试。格式轴的事由
    代码办，模型只写正文——省下的注意力正好用在内容上。

    说话人同属固定结构（第 1 句 A、末句 B），一并写死：它从前是靠「严格交替」
    顺带保证的，交替判据放宽之后，只写文本就会漏掉这一半。
    """
    if not script:
        return 0
    io = intro_outro_spec(cfg)
    prog = program_name_of(cfg)
    first = io["intro_first"].format(program=prog)
    last = io["outro_last"].format(program=prog)
    # 写死的句子也得守单句字数上限，否则我们自己造出一个模型无法修的
    # 门禁失败，然后白重试到超限。这里直接说清是节目名的问题。
    max_c = int(cfg.get("gate.max_chars", 40))
    over = [(n, len(s)) for n, s in (("片头", first), ("片尾", last)) if len(s) > max_c]
    if over:
        raise ScriptError(
            "固定%s句 %d 字，超过单句上限 %d 字，多半是节目名太长（当前「%s」）。"
            "请把节目名改短，或把单句上限调大。"
            % ("、".join(n for n, _ in over), over[0][1], max_c, prog))
    n = 0
    # 说话人也一并写死。模板说的是「第 1 句由 A 念、末句由 B 念」，这层意思从前
    # 只写在提示词里，靠「严格交替」顺带保证；交替判据一放宽，模型就可能让 B 念
    # 开场白——片头尾就不再是各期一致的固定标识了。
    if script[0].get("text") != first or script[0].get("speaker") != "A":
        script[0]["text"] = first
        script[0]["speaker"] = "A"
        n += 1
    # 只有一句时首尾是同一句，写两遍会把片头句覆盖成片尾句
    if len(script) > 1 and (script[-1].get("text") != last
                            or script[-1].get("speaker") != "B"):
        script[-1]["text"] = last
        script[-1]["speaker"] = "B"
        n += 1
    return n


def intro_outro_ok(script, cfg):
    """首句是否像片头、末句是否像片尾。返回 (first_ok, last_ok)。

    定点写死之后这条门禁基本恒真，保留它是为了防结构性走样：脚本被外部
    改写、句数太少导致首尾重合、模板被改坏，都在这里现形。
    """
    prog = program_name_of(cfg)
    first = script[0]["text"] if script else ""
    last = script[-1]["text"] if script else ""
    first_ok = bool(prog and prog in first and any(m in first for m in INTRO_MARKS))
    last_ok = bool(prog and prog in last and any(m in last for m in OUTRO_MARKS))
    return first_ok, last_ok


def gate_generate(script, cfg, paradigm=None):
    """生成阶段门禁（GATE_SPEC.stage == generate）。

    `paradigm` 是范式卡（`resolve_paradigm()` 的产物），连续句数上限取自它。
    与写脚本的提示词取同一个数——提示词说可以连说三句、门禁按两句卡，
    稿子会陷在「改了还是不过」的死循环里。

    不接 calib：脚本阶段的时长一律按标准语速估，与音色无关。
    """
    items = []
    lo = int(cfg.get("gate.min_chars", 8))
    hi = int(cfg.get("gate.max_chars", 40))
    max_sec = float(cfg.get("gate.max_seconds_per_line", 15))

    def add(key, ok, detail="", **extra):
        spec = GATE_BY_KEY.get(key, {"label": key, "level": "warn"})
        item = {"key": key, "label": spec["label"], "level": spec["level"],
                "judge": spec["judge"], "ok": bool(ok), "detail": detail}
        if spec.get("soft"):
            item["soft"] = True
        # 结构化明细随条目一起走：报告里给人看的是 detail 那一行，
        # 回灌给模型的是结构化字段（哪一句、哪个词、换成什么）。
        # 两者都由同一次判定产出，不各算一遍——各算一遍迟早会对不上。
        item.update(extra)
        items.append(item)

    add("json_valid", isinstance(script, list) and len(script) > 0,
        "共 %d 句" % len(script))

    missing = [i + 1 for i, s in enumerate(script)
               if not s.get("text") or not s.get("speaker") or not s.get("emotion")]
    add("fields_complete", not missing,
        "缺字段句子：%s" % missing if missing else "全部齐备")

    # 同一人连说两句不是错（一段完整的回答本来就该由一个人说完），超上限才是。
    # 判据从「必须交替」换成「连续句数」：前者会把一个人的一段话掰给两个人，
    # 那正是这条门禁原先在逼着模型做的事。
    card = paradigm or resolve_paradigm(None, cfg)
    max_run = paradigms.max_run_of(card)
    over_run = []
    run = 1
    for i in range(1, len(script)):
        if script[i]["speaker"] == script[i - 1]["speaker"]:
            run += 1
            if run > max_run:
                over_run.append(i + 1)
        else:
            run = 1
    add("ab_run_limit", not over_run,
        ("超限句：%s（同一人连续上限 %d 句）" % (over_run, max_run)) if over_run
        else "无超过 %d 句的连续同一人" % max_run,
        # 明细列全，不跟着人话一起截断：人话是给人扫一眼的，它是给定点修补用的。
        # 只报前几个的话，改完这几处、剩下的还在，下一轮还是不过。
        lines=over_run)

    too_short = [{"line": i + 1, "chars": len(s["text"])}
                 for i, s in enumerate(script) if len(s["text"]) < lo]
    too_long = [{"line": i + 1, "chars": len(s["text"])}
                for i, s in enumerate(script) if len(s["text"]) > hi]
    segs = []
    if too_long:
        segs.append("过长 %s" % "、".join("第 %d 句（%d 字）" % (h["line"], h["chars"])
                                        for h in too_long))
    if too_short:
        segs.append("过短 %s" % "、".join("第 %d 句（%d 字）" % (h["line"], h["chars"])
                                        for h in too_short))
    # 明细一律列全，不截断：这一行既要给人看现场，也是回灌反馈的底稿。
    # 只报前几处的话，模型把这几处改完、剩下的还在，下一轮还是不过。
    add("line_length", not (too_short or too_long),
        "；".join(segs) if segs else "全部落在 %d–%d 字" % (lo, hi),
        too_short=too_short, too_long=too_long, lo=lo, hi=hi)

    over = [i + 1 for i, s in enumerate(script)
            if duration_model.estimate_line(s["text"], s["speaker"], cfg) > max_sec]
    add("line_duration", not over,
        "超时句：%s" % over[:6] if over else "全部 ≤ %.1f 秒" % max_sec,
        lines=over)

    bad_emo = [i + 1 for i, s in enumerate(script)
               if s.get("emotion") not in EMOTION_TAGS]
    add("emotion_vocab", not bad_emo,
        "越界句：%s" % bad_emo[:6] if bad_emo else "全部取自词表",
        lines=bad_emo)

    # 档位是硬约束，与词表那条分开判：卡上说这批不写心情，写了就是跑偏。
    # 两者要改的地方也不同——词表那条是"填了个不存在的标签"，这一条是
    # "标签合法，但这档不让写"。**先过词表再过档位**：词表外的值归一化之后
    # 会落到「平静」，本来就落在 none 档的合法集合里，不会两条同时报。
    over_emo = []
    if paradigms.emotion_level_of(card) == "none":
        over_emo = [i + 1 for i, s in enumerate(script)
                    if s.get("emotion") in EMOTION_TAGS
                    and s.get("emotion") not in NONE_LEVEL_EMOTIONS]
    add("emotion_level", not over_emo,
        ("档位越界句：%s（这批不写心情，`emotion` 只填语篇标签或「平静」）"
         % over_emo[:6]) if over_emo else "档位与标签一致",
        lines=over_emo)

    hits = []
    for i, s in enumerate(script):
        for r in BANNED_RULES:
            for w in r["words"]:
                if w in s["text"]:
                    hits.append({"line": i + 1, "word": w, "rule": r["key"],
                                 "label": r["label"],
                                 "substitute": r.get("substitute", "")})
    add("banned_words", not hits,
        ("命中 %d 处：%s" % (len(hits),
                          "、".join("第 %d 句「%s」" % (h["line"], h["word"])
                                    for h in hits))) if hits else "未命中禁用词",
        hits=hits)

    # 可朗读：台词是唯一进 TTS 的字段，emoji、网址、命令参数这类形状念出来
    # 必然出洋相。判据按字符形状定（见 UNREADABLE_SHAPES，与提示词禁令
    # READABLE_RULE 同一条形状清单），命中给句号、给形状样例，走定点修补。
    bad_read = []
    for i, s in enumerate(script):
        for h in unreadable_hits(s["text"]):
            bad_read.append({"line": i + 1, **h})
    add("readable_text", not bad_read,
        ("命中 %d 处：%s" % (len(bad_read),
                          "、".join("第 %d 句「%s」（%s）"
                                    % (h["line"], h["sample"], h["label"])
                                    for h in bad_read))) if bad_read
        else "未发现无法朗读的形状",
        hits=bad_read)

    io = intro_outro_spec(cfg)
    first_ok, last_ok = intro_outro_ok(script, cfg)
    add("intro_outro", bool(first_ok and last_ok),
        ("首句%s、末句%s" % ("命中" if first_ok else "未命中",
                          "命中" if last_ok else "未命中")))

    # 总时长按「软门禁」办：不达标照常写进报告，但不阻断放行、也不回灌重写。
    # 它是估算值不是实测值——真正的时长要等合成出来才作数；而估时口径是**标准语速**，
    # 与本期用哪个音色无关（各音色相对标准的快慢见配置页那个比例）。
    # 拿一个估不准的秒数把稿子打回重写，等于让模型围着一个它既测不出、
    # 也控不住的数字反复改，最后把内容改坏。
    # 判定取「比例阈值」与「绝对容差」的较大者：短集句数少，
    # 每句几字之差就超过百分比，只按比例判会让短集永远过不了。
    est = duration_model.explain(script, cfg)
    thr_pct = float(cfg.get("gate.max_deviation_pct", 15))
    thr_sec = float(cfg.get("gate.min_deviation_seconds", 20))
    allowed = max(est["target_seconds"] * thr_pct / 100.0, thr_sec)
    # 符号统一成「预估减目标」：正数偏长、负数偏短，与后面那句「需删减/需增加」
    # 同一方向。原先秒数取「目标减预估」、百分比取「预估减目标」，同一行里两个
    # 方向相反，模型得自己猜哪个才算数。
    diff = est["total_seconds"] - est["target_seconds"]
    ok = abs(diff) <= allowed
    detail = ("预估 %.1f 秒 vs 目标 %.1f 秒，%s %.1f 秒 / %+.1f%%（容差 ±%.1f 秒）"
              % (est["total_seconds"], est["target_seconds"],
                 "超出" if diff > 0 else "不足", abs(diff),
                 est["deviation_pct"], allowed))
    if not ok:
        # 反馈要可操作：只给偏差百分比，模型不知道该增删多少内容。
        # 换算系数取本稿口径的有效语速（explain 已按标准语速算好，含语速倍率），
        # 不用另一个常量顶替——同一个数出自同一处，改了尺子两处一起变。
        k = est.get("speech_cps") or duration_model.STANDARD_K
        detail += "；需%s约 %d 字" % ("删减" if diff > 0 else "增加",
                                    int(abs(diff) * k))
    add("total_duration", ok, detail)

    return _summarize(items, cfg)


# ------------------------------------------------------------------ 内容检
# 两项：语义检（有没有编造）、承诺链检（开头开的口子收没收）。
# 两者的判断依据都在语义层，词汇匹配做不了，只能交给模型。
# 判定三态：pass / fail / pending。pending 是「模型没给出结论」，
# 与 fail（判为不通过）是两件事，绝不能混。
CHECK6_DIMS = [("semantic", "语义检"), ("promise", "承诺链检")]

#: 分批核对时相邻两批的重叠字数。一句台词的依据可能正好骑在批次边界上，
#: 前后两批各看到半句——重叠一段，让这种句子至少在一批里是完整的。
CHECK6_BATCH_OVERLAP = 1000

#: 分批上限。超限**不静默降级**：宁可在报告里写「没核完、交人工」，
#: 也不能把「没核的那部分」记成「核过且没问题」——那是把没检查说成通过。
CHECK6_MAX_BATCHES = 8

#: 内容检用户提示词里固定文案的字符数（说明、块名、批次标注）。算输入预算
#: 时把它当常量扣掉：它每次都真算也不难，但它是常量，量一次写死足够。
CHECK6_FIXED_CHARS = 400

#: 写作与修补两条路提示词里固定文案的字符数（块名、契约说明、固定口令）。
#: 同上：算预算时要扣，但不必每次真量。
WRITE_FIXED_CHARS = 900
PATCH_FIXED_CHARS = 500


def split_material(text, size, overlap=CHECK6_BATCH_OVERLAP):
    """按预算把素材切成若干批，相邻两批尾首重叠。

    `size` 是每批能喂的最大字符数（素材容量扣掉提示词后的余量，见
    `material_capacity`）。它比 overlap
    还小时，重叠会把步子挤成 0，切分原地打转——所以那里把重叠压到批量的
    四分之一：边界保护该有，但不能把批次本身挤没。
    """
    text = text or ""
    size = int(size or 0)
    if size <= 0 or len(text) <= size:
        return [text] if text else []
    ov = max(0, min(int(overlap or 0), size // 4))
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        if i + size >= len(text):
            break
        i += size - ov
    return out


def _evidence_block(evidence):
    """本期判据包：本期讲什么（主旨与要点）+ 每一节压出来的逻辑骨架。

    这两样都是排图那一步定下的、写脚本时也已经喂过的东西。内容检判「方向」
    时用它比用原文更稳：原文字面上没有「本期主旨」这句话，主旨是从原文得出
    的结论——拿结论去核结论，比拿几万字原文去比对更直接，也不占预算。
    """
    if not evidence:
        return ""
    parts = []
    gist = str(evidence.get("gist") or "").strip()
    points = [str(p).strip() for p in (evidence.get("points") or [])
              if str(p).strip()]
    if gist or points:
        rows = []
        if gist:
            rows.append("- 主旨：%s" % gist)
        rows += ["- %s" % p for p in points]
        parts.append("【本期计划】（本期讲什么；判「方向」用）\n" + "\n".join(rows))
    rows = []
    for i, s in enumerate(evidence.get("sections") or []):
        if not isinstance(s, dict):
            continue
        g = str(s.get("gist") or "").strip()
        if not g:
            continue
        anchor = str(s.get("anchor") or "").strip()
        rows.append("- 第 %d 节%s：%s"
                    % (i + 1, ("（%s）" % anchor) if anchor else "", g))
    if rows:
        parts.append("【各节凝缩】（每一节的逻辑骨架；判「方向」用）\n"
                     + "\n".join(rows))
    return "\n\n".join(parts)


def merge_batch_checks(results, dims, n_batches, labels):
    """把各批的核对结论并成一份。

    规则只有一句：**同一句要在每一批里都被报出来，才算真问题**。

    某句的依据只落在某一批的素材里，那一批自然找不到它——但依据真的在，
    只是分在另一段。反过来，「每一批都找不到」才等价于「全文里找不到」，
    这正是分批核对能得出与全文核对同一结论的原因。单批时这条规则退化成
    「报了就留下」，与从前一样。

    没指出句号（line=0）的那些没法投票：本来就交人工复核，一律保留。
    """
    out = {}
    for key in dims:
        label = labels[key]
        ok_states = [r[key] for r in results if r[key][0] != "pending"]
        if not ok_states:
            un = [r[key][1] for r in results if r[key][0] == "pending"]
            out[key] = ("pending",
                        "%s未判定（%s），交人工复核" % (label, un[0] if un else "无结论"),
                        [])
            continue
        votes = {}
        for state, _detail, issues in ok_states:
            for it in issues:
                votes.setdefault(it["line"], []).append(it)
        keep, dropped = [], 0
        for line, items in votes.items():
            if line and len(items) >= len(ok_states):
                keep.append(items[0])
            elif not line:
                keep.append(items[0])
            else:
                dropped += 1
        detail = _issues_detail(keep)
        if dropped:
            # 撤销这件事必须留痕：不写出来，「这一轮报的问题比上一轮少」就
            # 变成了一个没人知道来由的悬案。
            detail += "（%d 批比对，%d 处仅单批报出、已撤销）" % (len(ok_states), dropped)
        if len(ok_states) < n_batches:
            detail += "（另有 %d 批未判定，未计入）" % (n_batches - len(ok_states))
        keep.sort(key=lambda it: it["line"] or 0)
        out[key] = ("fail" if keep else "pass", detail, keep)
    return out


def fit_material(llm, material, cfg, other_chars=0, evidence=None, log=None):
    """把素材裁到这次调用放得下的长度，超了就明说。返回 (素材文本, 备注)。

    备注非空表示这份素材被裁过、或被换成了凝缩，要一路带到日志里：静默少喂会
    让模型在缺依据的情况下硬写，而产物上看不出少了什么。

    降级次序：
    1. 原文放得下 → 用原文（常态，一期两万字放得下，这条覆盖绝大多数情况）；
    2. 放不下但有凝缩 → 用凝缩（每节的逻辑骨架）。**这是有损的**，所以必须
       在返回的备注与素材头部把话说出来；
    3. 连凝缩都没有（逐期即兴没排过图）→ 按预算截原文，同样明说。

    素材长度按字符算：token 与字符的折算比由客户端从后端回传的用量里反标
    （见 `llm_client.tokens_per_char`），不是拍出来的常数。
    """
    material = material or ""
    log = log or (lambda m: None)
    capacity = material_capacity(cfg)
    if not hasattr(llm, "quota_note"):
        # 没有预算能力的后端（测试里的假模型、以及将来接的非 OpenAI 客户端）：
        # 照原文全喂。裁不了不等于要裁，硬塞一个常数只会把素材白砍掉。
        return material, ""
    budget = capacity - int(other_chars or 0)
    if budget > 0 and len(material) <= budget:
        return material, ""
    ev = _evidence_block(evidence)
    if budget <= 0:
        # 容量扣掉提示词后连一段素材都放不进去——判不了就明说。静默把整段
        # 塞进去会把后端顶爆，报出来的却是后端一句看不懂的错。有凝缩就先用
        # 凝缩，日志里留着这条，稿子为什么缺依据才有据可查。
        note = ("本期素材容量（输出上限 × 压缩档）扣掉提示词后放不下素材，"
                "请检查 script.target_minutes 与 script.compress_ratio%s。"
                % ("，已改用凝缩" if ev else ""))
        log(note)
        return ev, note
    if ev:
        note = ("本期素材 %d 字，超出容量 %d 字（输出上限 × 压缩档），"
                "已改用凝缩（每节逻辑骨架）作依据——细节可能缺失。"
                % (len(material), budget))
        log(note)
        return ev, note
    head = ("本份素材为节选：全 %d 字，超出这次调用的输入预算 %d 字，此处只给了"
            "前 %d 字。**没看到的段落不等于不存在**——某句在本份里找不到依据时，"
            "不要就此判它编造。\n\n" % (len(material), budget, max(0, budget)))
    note = ("本期素材 %d 字，超出这次调用的输入预算 %d 字，已按预算截断喂入"
            "（依据可能缺失）。" % (len(material), budget))
    log(note)
    return head + material[:max(0, budget)], note


def _unjudged(reason, dims=None):
    """没被判定的维度。

    不阻断放行（模型没有结论，不等于内容有问题），但必须在报告里留痕：
    静默消失会让「全部通过」变成假象。落进 pending，前端标「待复核」。
    """
    return {key: ("pending", "%s未判定（%s），交人工复核" % (label, reason), [])
            for key, label in CHECK6_DIMS if dims is None or key in dims}


def gate_check6(script, cfg, llm=None, material="", dims=None, evidence=None):
    """内容检。

    两项由模型判，一次调用判完（见 check6_llm）。与生成阶段的形式门禁互补：
    那边管 JSON 合法、字段完整、句长、A/B 交替、禁用词这类可枚举的形，代码判；
    这边管内容本身，模型判。两侧各自有做得了和做不了的事，谁也别替谁下判断。

    三态落到门禁条目上：
    - 通过   → ok=True
    - 不通过 → ok=False，参与放行（语义检 fail 级、承诺链 warn 级）
    - 未判定 → ok=False 且标 advisory，只落 pending 交人工，不参与放行

    `dims` 限定这次要判哪几项。全判是默认；定点修补之后只需要重判**被改到的那
    一项**（改的是措辞就重判语义检，动的是承诺句才重判承诺链检），此时把它传进来，
    少判的那一项由调用方保留上一轮结论——不必为了一项把另一项也白喂一遍上下文。

    总时长偏差不在此处：它属于可回灌修正的量，已在生成阶段把关。
    """
    dims = tuple(dims) if dims else tuple(k for k, _ in CHECK6_DIMS)
    results = (_unjudged("未提供 LLM", dims) if llm is None
               else check6_llm(script, cfg, llm, material, dims,
                               evidence=evidence))
    items = []
    for key, _label in CHECK6_DIMS:
        if key not in results:
            continue
        state, detail, issues = results[key]
        spec = GATE_BY_KEY["check_" + key]
        item = {"key": "check_" + key, "label": spec["label"],
                "level": spec["level"], "judge": spec["judge"],
                "ok": state == "pass", "detail": detail}
        # 问题清单原样带在条目上：定点修补要的就是它里面的句号。
        # 压成一行人话再往外传，等于把「哪一句」这条信息在出口处丢掉，
        # 后面再想定点只能靠猜。
        if issues:
            item["issues"] = issues
        if state == "pending":
            # 标 advisory 使它落 pending 而不阻断——模型没判，不该按「有问题」办。
            # 但绝不写成 ok=True：那是把「没检查」说成「检查通过」。
            item["advisory"] = True
        items.append(item)

    report = _summarize(items, cfg)
    report["note"] = "内容检由模型承担；未判定的项落「待复核」，不计入放行。"
    return report


#: 内容检单项的输出契约。七条提示词里**只有这一条要求复杂结构**（第几句 /
#: 原话 / 什么问题三件套），从前也只有它把输出形状全押在提示词的一段叮嘱上。
#: 而定点修补完全依赖它给出句号——劝不住的代价是那一轮定不了点、只能交人工。
#: 所以照样上约束解码：能定点的前提不是「求模型给落点」，是**结构上必须给**。
_CHECK6_ITEM = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},
                    "quote": {"type": "string"},
                    "problem": {"type": "string"},
                },
                "required": ["line", "problem"],
            },
        },
    },
    "required": ["pass", "issues"],
}


def check6_schema(dims=None):
    """内容检的输出契约。

    `dims` 限定只判某几项时，`required` 跟着缩到那几项——约束解码按 schema
    剪裁输出，schema 里还留着这一轮不判的键，模型就得给它压根没判过的项下结论。
    """
    dims = tuple(dims) if dims else tuple(k for k, _ in CHECK6_DIMS)
    props = {k: _CHECK6_ITEM for k, _ in CHECK6_DIMS if k in dims}
    return {"type": "object", "properties": props, "required": list(props)}


CHECK6_SYSTEM = """你是播客脚本的审校者，回答只输出 JSON，不要任何其它文字。

输出格式（两个键都要给出，缺一不可）：
{"semantic": {"pass": true, "issues": []},
 "promise": {"pass": true, "issues": []}}

issues 里每一项都必须是一个对象，带三个键：
{"line": 12, "quote": "那一句里的原话片段", "problem": "一句话说清哪里不对"}

- line：出问题的是第几句（从 1 数起）。**找不出具体哪一句时填 0**——不许拿
  第一句或最接近的一句顶替，填了假句号，改稿的人会去改一句没毛病的台词，
  真正的问题原封不动，下一轮又原样报一遍。
- quote：那一句里的一小段原话，让人不翻全篇就能对上号。
- problem：这一处到底哪里不对，别写成结论式的空话。

判断标准（两块依据各管一项，不要串用）：
- semantic（语义检）：每条台词的内容是否能在【素材节选】里找到依据。出现素材之外的
  事实、数据、来源即判不通过。素材可能是分节的，某句在本份里找不到依据就照实报出
  来并带上句号——这一份找不到不等于全篇没有，是否成立由程序合并，你不必替它留余地。
  注意：首句与末句是按固定结构写死的片头语与片尾语（问候、节目名、收束），素材里
  没有它们属正常，不受本条约束；判断范围是这两句之外的正文。
- promise（承诺链检）：片头提出的疑问、设问或承诺，在后文有没有真的回应。判据是
  【本期计划】与【各节凝缩】——本期该讲的有没有讲到、开的口子有没有收。只管
  「开的口子收没收」，不管用词是否重复：对话体前后用词不同是正常的，
  判断依据是那件事有没有被回答，不是字面有没有重现。若判为没回应，line 填
  **提出那个承诺的句子**。

没有任何问题时 issues 是空数组。"""


def _norm_issues(node, total):
    """把模型给的问题清单归一化成带落点的结构。

    `line` 取不出、或不在 1..total 之内，一律归 0——「指不出哪一句」就老实说
    指不出。拿假句号顶替比不填坏得多：定点修补会照着它去改一句没毛病的台词，
    真正的问题原封不动，下一轮再报一遍，轮次全耗在原地打转。
    """
    out = []
    for it in (node.get("issues") or []):
        if isinstance(it, dict):
            try:
                line = int(it.get("line"))
            except (TypeError, ValueError):
                line = 0
            if not 1 <= line <= total:
                line = 0
            quote = str(it.get("quote") or "").strip()
            problem = str(it.get("problem") or it.get("text") or "").strip()
        else:
            # 旧形态（一句人话）：没有句号，就是定不了点。兼容它不为好看，
            # 是为了后端降级、模型不守格式时不要整项变成「解析失败」。
            line, quote, problem = 0, "", str(it).strip()
        if not problem and not quote:
            continue
        out.append({"line": line, "quote": quote, "problem": problem})
    return out


def _issues_detail(issues):
    """给人看的那一行。句号缺失时明说「未指出句号」，不假装知道是哪一句。"""
    if not issues:
        return "通过"
    parts = []
    for it in issues[:4]:
        where = "第 %d 句" % it["line"] if it["line"] else "未指出句号"
        parts.append("%s：%s" % (where, it["problem"] or it["quote"]))
    return "；".join(parts)


def _parse_check6(text, dims, labels, total):
    """解析一批的核对结论。缺键按「未判定」记，绝不默认通过。

    缺键 = 模型没判这一项。默认通过会让「模型偷懒」在报告上变成绿灯，
    比不检更坏——那是把「没检查」说成「检查通过」。
    """
    def pending(reason):
        return {k: ("pending",
                    "%s未判定（%s），交人工复核" % (labels[k], reason), [])
                for k in dims}

    text = (text or "").strip()
    s = text.find("{"); e = text.rfind("}")
    if s == -1 or e == -1:
        return pending("输出无法解析")
    try:
        data = json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return pending("输出无法解析")
    if not isinstance(data, dict):
        return pending("输出不是 JSON 对象")
    out = {}
    for key in dims:
        node = data.get(key)
        if not isinstance(node, dict) or "pass" not in node:
            out[key] = ("pending",
                        "%s未判定（模型未返回该项结论），交人工复核" % labels[key], [])
            continue
        issues = _norm_issues(node, total)
        out[key] = ("pass" if bool(node.get("pass")) else "fail",
                    _issues_detail(issues), issues)
    return out


def check6_llm(script, cfg, llm, material, dims=None, evidence=None):
    """语义检与承诺链检。

    素材放得下就一次判完；放不下**分批核**——每批带一段素材和整篇脚本，最后
    按「每批都报同一句才算真问题」合并（见 `merge_batch_checks`）。

    分批不是图省事，是为了**不砍素材**：从前把素材截到前 6000 字，落在后面的
    依据一律被记成「素材里没有」，把本来正确的句子判成编造。语义检的误报会把
    对的句子改掉，而且一声不响。

    两项合并成一次调用不是图省事：都要读整篇脚本，分两次就是把同一段上下文
    喂两遍，白花一次调用，还多出一次两次结论不一致的机会。

    `dims` 限定本次判哪几项。定点修补之后只重判被改到的那一项，另一项由调用方
    保留上一轮结论，不必为它再喂一遍上下文。

    `evidence` 是本期判据包（主旨 + 各节凝缩），供「方向」类判断用；事实判断
    仍看素材原文。两样都要给：原文字面上没有「本期主旨」这句话，主旨是从原文
    得到的结论——拿结论核方向比拿几万字原文更直接。

    返回 {key: (state, detail, issues)}，state ∈ {"pass", "fail", "pending"}，
    issues 是 [{line, quote, problem}]，line=0 表示模型没指出是哪句。
    """
    dims = tuple(dims) if dims else tuple(k for k, _ in CHECK6_DIMS)
    labels = dict(CHECK6_DIMS)
    system = CHECK6_SYSTEM
    if set(dims) != set(labels):
        system += ("\n\n本次**只判**：%s。输出对象里只给这几个键，"
                   "其余键不要出现。" % "、".join(labels[k] for k in dims))

    lines = "\n".join("%d. [%s] %s" % (i + 1, s["speaker"], s["text"])
                      for i, s in enumerate(script))
    ev = _evidence_block(evidence)
    # 每一批都必须带上的字：系统提示词、判据包、待审脚本与固定说明。素材只能
    # 占剩下的——先把这些量出来再算，得到的才是这次调用真实的余量，而不是
    # 一个拍出来的常数。
    other = len(system) + len(lines) + len(ev) + CHECK6_FIXED_CHARS
    size = material_capacity(cfg) - other
    if size <= 0:
        # 模板本身就撑满了容量，连一段素材都放不进去——判不了就明说。
        # 静默把整段塞进去会把后端顶爆，报出来的却是后端一句看不懂的错。
        return _unjudged(
            "素材容量（输出上限 × 压缩档）扣掉提示词后放不下素材，未核",
            dims)
    # 审校与探查、写作同一个口径取全局预算：思考段与答案段共用这份
    # max_tokens。原先在这一处固定给 2000，答案段一个字也轮不上。
    max_tokens = int(cfg.get("llm.max_tokens", 8192))
    batches = split_material(material, size) or [""]
    if len(batches) > CHECK6_MAX_BATCHES:
        return _unjudged("素材 %d 字要分 %d 批核（上限 %d 批），本次未核"
                         % (len(material or ""), len(batches), CHECK6_MAX_BATCHES),
                         dims)

    results = []
    for i, batch in enumerate(batches):
        head = ""
        if len(batches) > 1:
            # 明确交代这一批只覆盖一部分，并要求它照实报：合并端要的正是
            # 「每一批各自有没有找到依据」，替别的批省事反而会把票投歪。
            head = ("【本批素材】第 %d/%d 批（全篇 %d 字，本批 %d 字，批间有重叠）\n"
                    "本批只覆盖素材的一部分：某句在本批里找不到依据，照实报出来并"
                    "标出句号即可，不必替它在别的批里找——这一步由程序合并。\n\n"
                    % (i + 1, len(batches), len(material or ""), len(batch)))
        user = ("%s%s【素材节选】（事实判据：台词里的事实、数据、来源都要能在"
                "这里找到）\n%s\n\n"
                "【待审脚本】（当前任务：逐句核下面这一份）\n%s"
                % ((ev + "\n\n") if ev else "", head, batch, lines))
        try:
            raw, _meta = llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=0.1,
                # 审校与探查、写作同一个口径取全局预算：思考段与答案段共用
                # 这份 max_tokens，会思考的后端也要给够。原先在这一处固定给
                # 2000，答案段一个字也轮不上，两项检查会一同落空。
                max_tokens=max_tokens,
                json_schema=check6_schema(dims),
            )
        except Exception as e:                                   # noqa: BLE001
            results.append(_unjudged(str(e), dims))
            continue
        results.append(_parse_check6(raw, dims, labels, len(script)))

    if len(results) == 1:
        return results[0]
    return merge_batch_checks(results, dims, len(results), labels)


def _summarize(items, cfg):
    """汇总门禁结果。

    advisory 项是「模型没给出结论」的那些，落 pending 交人工复核、不参与放行：
    模型没判不等于内容有问题，按有问题办会误杀正常内容。而**判定为不通过**的项
    （不带 advisory）照常参与放行——语义检 fail 级、承诺链 warn 级。
    """
    strict = bool(cfg.get("script.gate_strict", True))
    hard = [i for i in items if not i.get("advisory")]
    pending = [i for i in items if i.get("advisory") and not i["ok"]]
    # soft 项是第三类：不是「没判」，而是「判了、但没资格拦人」。总时长偏差就属
    # 这一类——它的输入是估算值，估算系数带着校准误差，要等合成出来才知道真章。
    # 所以不达标只记账，不参与放行，严格模式下也不拦。
    soft = [i for i in hard if not i["ok"] and i.get("soft")]
    blocking = [i for i in hard if not i["ok"] and not i.get("soft")]
    fails = [i for i in blocking if i["level"] == "fail"]
    warns = [i for i in blocking if i["level"] == "warn"]
    passed = not fails and not (strict and warns)
    return {"items": items, "fails": fails, "warns": warns, "pending": pending,
            "soft": soft, "passed": passed, "strict": strict,
            # 记下判定的时刻。合成前那份提醒读的就是它，人要看着这个时间
            # 判断记录有多旧——要不要理它，是这个时间说了算。
            "time": datetime.now().isoformat(timespec="seconds")}


# ------------------------------------------------------------------ 门禁结论
def gate_report_path(project_dir):
    """脚本阶段门禁结论的落点（过程目录下）。"""
    return os.path.join(project_dir, "gate.generate.json")


def write_gate_report(project_dir, report):
    """把这一轮的门禁结论落盘。

    合成端读它，只为在出片前给人提个醒。它是**记录**，不是闸门——稿子和问题
    一起落盘之后，脚本阶段就结束了，改稿还是直接出片是人自己的事。
    """
    path = gate_report_path(project_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def read_gate_report(project_dir):
    """读脚本阶段留下的门禁结论。

    读不到、或文件坏了，一律返回 None：读不出来就不提醒，不拿一份坏文件
    去骚扰人。也不追踪此后的人工修改——记录产生于脚本生成那一刻，
    改过没有以手上的稿子为准，框里会把这一点说明白。
    """
    path = gate_report_path(project_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ------------------------------------------------------------------ 锁文件
def lock_path(project_dir):
    return os.path.join(project_dir, "blocked.lock.json")


def write_lock(project_dir, stage, reason, context, attempts):
    """门禁连续不过 → 写锁文件硬中断（08a）。

    只剩产物阶段在用：脚本阶段不再写锁——那边不过只是「有问题的稿子」，
    稿子和结论都落了盘，人愿意带着问题出片是他的选择，没有拦的道理。
    """
    payload = {
        "blocked_at": stage,
        "reason": reason,
        "context": context,
        "attempts": attempts,
        "time": datetime.now().isoformat(timespec="seconds"),
        "resume": "修复后以 --continue 指定本目录续跑",
    }
    path = lock_path(project_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def read_lock(project_dir):
    path = lock_path(project_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def clear_lock(project_dir):
    path = lock_path(project_dir)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# ------------------------------------------------------------------ 定点修补
# 稿子改一句，不该把两百句重打一遍。
#
# 回灌从「整篇重出」改成「只回改动句」，省下的不只是 token：整篇重出会把没毛病的
# 两百句重新摇一次骰子，改好的地方又带进新毛病，轮次全耗在打地鼠上（这一点
# _feedback_banned 的注释里早就写明了，只是提示词劝不住格式）。
#
# 能不能定点只看一件事：**问题带不带句号**。带，就能精确落到那一行；不带，就只能
# 交人工。所以内容检的输出格式必须先带落点（见 CHECK6_SYSTEM），这不是顺手加个
# 字段，是定点修补的前置条件。

#: 定点补丁的输出契约。结构里**没有「新增」和「删除」这两种表达**——只有
#: `index`（改哪一句）与 `text` / `emotion`（改成什么）。「不许增删」不靠提示词劝，
#: 靠这里根本写不出来。说话人也不在此列：它归代码管（enforce_max_run 压连续句、
#: pin_intro_outro 写死首末句），模型能改它等于给格式轴开了一道后门。
PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "text": {"type": "string"},
                    "emotion": {"type": "string", "enum": EMOTION_TAGS},
                },
                "required": ["index", "text"],
            },
        },
    },
    "required": ["edits"],
}


def patch_schema(level=None):
    """定点补丁的输出 schema，`emotion` 的枚举按档位收窄。

    与 `script_schema` 同一个道理：改写这一端同样是「模型产出 emotion」的入口，
    只收窄写那一次、不修这一端，等于给档位留了一道后门——第一次生成填不出来，
    下一轮定点修补又能填进去了。
    """
    schema = copy.deepcopy(PATCH_SCHEMA)
    item = schema["properties"]["edits"]["items"]["properties"]
    item["emotion"]["enum"] = list(emotion_vocab(level))
    return schema


_PATCH_SYSTEM_TMPL = """你是播客脚本的定点修补者，回答只输出 JSON，不要任何其它文字。

输出格式：
{"edits": [{"index": 58, "text": "改好后的整句文本", "emotion": "平静"}]}

铁律：
- **只允许替换**被点名句子的内容。不许新增句子，不许删除句子，全篇行数一个字都不能变。
- 只把**需要改的句子**写进 edits。没被点名的句子一个字都不许动，也不许出现在 edits 里。
- text 必须是替换后的**完整整句**，不是片段；一句就是一句，不许并进别的句子，
  也不许把一句拆成两条 edit。**不许把 emotion 标签词写进 text**（开头、结尾都
  不行）——标签只写在 emotion 字段里。text 里的每个字都会被念出来，写
  「追问AI正在…」就是让 TTS 把「追问」两个字念出来，这是事故不是风格。
- 每句 8–40 字。过长的只精简措辞、不许拆句；过短的只就地补内容、不许并句。
  拆句会让后面每一句的句号都挪一位，改稿的人按句号找不到原来那一句。
- 说话人不在你的职责内，不要输出 speaker 这个键。
- 你改出来的句子**同样要过措辞禁忌**——不许拿一个同样把话说满的词顶上去，
  那只是把同一个毛病换了层壳，门禁照样拦：
%s
- emotion 可以不输出（那就是不改）；要输出只能取：%s"""


def patch_system(level=None):
    """定点修补的系统提示词。`emotion` 的可填范围随档位走。

    词表不在这里另存一份：档位只决定取哪一个集合（`emotion_vocab`），集合本身
    仍是 `config_manager` 那一处。两处各存一份，走岔了只有出事那天才知道。
    """
    return _PATCH_SYSTEM_TMPL % (format_banned_rules(),
                                 "、".join(emotion_vocab(level)))


def _patch_feedback(targets):
    """定点修补的「要改的地方」那一段。

    逐句给：第几句、错在哪、往哪个方向改。缺了方向，模型知道要绕开什么却不知道
    绕去哪里，只会换成一个意思相同、照样不合规的说法。
    """
    rows = ["**只改下面这几句。** 没列出的句子一个字都不许动，也不要回给我。"]
    for ln in sorted(targets):
        for why in targets[ln]:
            rows.append("    第 %d 句：%s" % (ln, why))
    return "\n".join(rows)


def build_patch_prompt(script, targets, feedback, material="", window=2,
                       evidence=None):
    """拼定点修补的用户提示词。

    只给被点名句与它前后各 `window` 句，**不给全篇**。这不是省字那么简单：实测里
    把全篇 220 句一起递过去，模型三次里有两次直接不吐 JSON（被上下文带跑，改写起
    解释性文字）；只给窗口则三次全中。窗口取 2 也不是凭空定的——A/B 对话体的局部
    语境就是上下两句的事，再远的句子跟这一句的措辞没有关系。

    `material` 只在需要时才带：修「编造」这类内容问题，模型得知道素材支持什么；
    纯形式项（措辞、句长、单句时长）只跟这一句自己的字面有关，带上它就是白占上下文。

    素材给多长由调用方按输入预算定（见 `fit_material`），这里不再自己截：
    截在拼提示词这一层，等于把「少了多少」藏进这一层，调用方与日志都看不见它。
    """
    keep = set()
    for ln in sorted(targets):
        for k in range(max(1, ln - window), min(len(script), ln + window) + 1):
            keep.add(k)
    rows = ["%d. [%s] %s" % (k, script[k - 1]["speaker"], script[k - 1]["text"])
            for k in sorted(keep)]
    parts = []
    ev = _evidence_block(evidence)
    if ev:
        parts.append(ev)
    if material:
        parts.append("【素材】（判断依据：内容类的问题看这里；不要照抄它的句子）\n%s"
                     % material)
    parts.append("【要改的地方】（当前任务：按这里逐条改，只改点名的句）\n%s"
                 % feedback)
    parts.append("【相关段落】（被点名的句子，以及它们前后各 %d 句）\n%s"
                 % (window, "\n".join(rows)))
    parts.append("现在输出 JSON。")
    return "\n\n".join(parts)


def parse_patch(raw):
    """从模型输出里取出 edits 数组。取不出就抛 ScriptError。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        raise ScriptError("补丁输出里没有找到完整 JSON 对象。")
    try:
        data = json.loads(text[s:e + 1])
    except json.JSONDecodeError as exc:
        raise ScriptError("补丁输出的 JSON 无法解析：%s" % exc)
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise ScriptError("补丁输出里没有 edits 数组。")
    if not data["edits"]:
        raise ScriptError("补丁是空的：edits 数组里一条改动都没有。")
    return data["edits"]


def apply_patch(script, edits, targets):
    """把补丁落回原稿。只替换字段，**行数一个都不变**。

    行数不变是这条路的根基：门禁报的「第 N 句」在补丁前后指的是同一行。一旦允许
    增删，编号全漂，就得回头重编一遍——那正是这条路要避开的成本。

    **只许改被点名的句。** 越界一律报错，不静默丢弃：模型顺手改了别的句子，等于把
    没毛病的地方重新摇一次骰子，而这正是这条路要根除的东西；悄悄放行它，下一轮
    若因此冒出新毛病，要跨过整整一个流程才查得到根源。
    """
    target_set = set(int(x) for x in targets)
    out = [dict(s) for s in script]
    seen = set()
    for e in edits:
        if not isinstance(e, dict):
            raise ScriptError("补丁里有不是对象的条目：%r" % (e,))
        idx = e.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ScriptError("补丁里的句号不是整数：%r" % (idx,))
        if not 1 <= idx <= len(out):
            raise ScriptError("补丁句号越界：%d（全篇共 %d 句）" % (idx, len(out)))
        if idx in seen:
            raise ScriptError("第 %d 句在同一份补丁里出现两次——那是拆句或并句，"
                              "定点修补不许改行数。" % idx)
        if idx not in target_set:
            raise ScriptError("补丁改了没被点名的第 %d 句。定点修补只动被点名的句子，"
                              "其余一律照抄。" % idx)
        text = re.sub(r"\s+", "", str(e.get("text") or "").strip())
        emo = str(e.get("emotion") or "").strip()
        if emo:
            if emo not in EMOTION_TAGS:
                raise ScriptError("补丁给第 %d 句的情绪「%s」不在词表内。" % (idx, emo))
            out[idx - 1]["emotion"] = emo
        # 补丁端与生成端同一条纪律：标签是元数据不是台词，text 不得带标签前缀。
        text = _strip_label_prefix(text, out[idx - 1]["emotion"])
        if not text:
            raise ScriptError("补丁把第 %d 句改成了空文本。" % idx)
        seen.add(idx)
        out[idx - 1]["text"] = text
        # 说话人一律不采纳：它由 enforce_max_run 与 pin_intro_outro 写死。
        # 不是不信模型，是这条轴上不该有两个说话的人。
    return out


def _needs_material(report):
    """这一轮要修的问题里有没有内容类的。

    内容类（语义检、承诺链检）的修法要看素材，才知道什么能说什么不能说。
    """
    for it in report.get("items") or []:
        if it.get("ok") or it.get("soft") or it.get("advisory"):
            continue
        if (it.get("key") or "").startswith("check_"):
            return True
    return False


def patch_targets(report, total, level=None):
    """把门禁不通过项分成三类：能定点的、只能重出的、交人工的。

    能定点的定义窄得只有一条：**问题自带句号**。形式门禁全都自带（它本来就是逐句
    判的）；内容检要模型自己给，给了才算。

    返回 (targets, refull, manual)：
      targets — {句号: [问题说明]}，交给模型定点改
      refull  — 只能整篇重出的：结构坏了（JSON 不合法、字段残缺），补丁无从下手
      manual  — 定不了点的，交人工复核，**不动稿子**

    「定不了点」只有一种成因：内容检自己指不出是哪一句。这时候让模型重写整篇，
    等于把已经好的两百句一起摇一次骰子，还会把它自己都说不清的问题原样带回来。
    不动稿子、交人看，比假装修一遍诚实。
    """
    targets, refull, manual = {}, [], []
    for it in report.get("items") or []:
        if it.get("ok") or it.get("soft") or it.get("advisory"):
            continue
        key = it.get("key") or ""
        if key == "banned_words":
            for h in it.get("hits") or []:
                targets.setdefault(int(h["line"]), []).append(
                    "措辞禁忌命中「%s」（%s）。换成%s 这类留余地的说法"
                    % (h["word"], h.get("label", ""),
                       h.get("substitute") or "更稳妥的"))
        elif key == "readable_text":
            # 形状判据自带句号，与措辞禁忌同路：定点改，不通篇重写。
            for h in it.get("hits") or []:
                targets.setdefault(int(h["line"]), []).append(
                    "出现无法朗读的%s「%s」：用普通的话交代它的意思"
                    % (h.get("label", "符号"), h.get("sample", "")))
        elif key == "line_length":
            lo, hi = int(it.get("lo", 8)), int(it.get("hi", 40))
            for h in it.get("too_long") or []:
                targets.setdefault(int(h["line"]), []).append(
                    "过长 %d 字（上限 %d 字）：精简措辞压进 %d 字以内，**不许拆句**"
                    % (h["chars"], hi, hi))
            for h in it.get("too_short") or []:
                targets.setdefault(int(h["line"]), []).append(
                    "过短 %d 字（下限 %d 字）：就地把内容补到 %d 字以上，**不许并句**"
                    % (h["chars"], lo, lo))
        elif key == "line_duration":
            for ln in it.get("lines") or []:
                targets.setdefault(int(ln), []).append(
                    "这一句念出来超过单句时长上限：精简它的字数")
        elif key == "emotion_vocab":
            for ln in it.get("lines") or []:
                targets.setdefault(int(ln), []).append(
                    "情绪标签不在词表内：从 %s 里挑一个"
                    % "、".join(emotion_vocab(level)))
        elif key == "emotion_level":
            # 档位越界同样是逐句判的、自带句号，属于能定点的那一类。漏了这一条，
            # 它会掉进 else 的「交人工」——明明改一个标签就能过，却把整句摆回给人。
            # 这一条只在 none 档报（门禁本身按档位判），所以可改的落点是固定的。
            for ln in it.get("lines") or []:
                targets.setdefault(int(ln), []).append(
                    "这一档不写心情：把标签换成语篇功能标签或「平静」（%s）"
                    % "、".join(NONE_LEVEL_EMOTIONS))
        elif key in ("check_semantic", "check_promise"):
            located = False
            for iss in it.get("issues") or []:
                ln = int(iss.get("line") or 0)
                if 1 <= ln <= total:
                    located = True
                    # 方向必须给全，且措辞要和别的条目一样是「第 N 句：怎么改」。
                    # 实测里只把检查结论原样贴出来（「语义检：素材里没有这个数据」），
                    # 模型会把它当成一份报告而不是一条指令，连续两次都跳过这一句没改。
                    targets.setdefault(ln, []).append(
                        "%s：%s。改成素材支持的说法，去掉素材里没有的事实、数据或来源"
                        % (it.get("label", "内容检"),
                           iss.get("problem") or "内容在素材里找不到依据"))
            if not located:
                manual.append(it)
        elif key in ("json_valid", "fields_complete"):
            refull.append(it)
        else:
            # ab_run_limit 与 intro_outro 本该被定型阶段（enforce_max_run /
            # pin_intro_outro）处理掉，到这一步不会不过。真出现了说明稿子被
            # 外部改坏，不是模型能定点修的事，交人工。
            manual.append(it)
    return targets, refull, manual


def _find_item(report, key):
    for it in report.get("items") or []:
        if it.get("key") == key:
            return it
    return None


def dims_to_recheck(prev_report):
    """这一轮改完，内容检哪几项需要重判。

    只重判**上一轮没过、或压根没判过**的那几项。上一轮已经判过、且判通过的那一项，
    本轮改的又是措辞、句长这类形的事，没有理由把同一段上下文再喂一遍——真出了问题，
    形式门禁那一轮已经先把它拦下了。贵的东西（走模型）按需跑，便宜的东西（走代码）
    每轮全跑，这是这一段的成本纪律。
    """
    if not prev_report:
        return set(k for k, _ in CHECK6_DIMS)
    need = set()
    for key, _label in CHECK6_DIMS:
        item = _find_item(prev_report, "check_" + key)
        if item is None or not item.get("ok"):
            need.add(key)
    return need


def merge_check(report, content, cfg, carry=None):
    """把重判的那几项并回报告；没重判的那几项，沿用上一轮结论。

    `carry` 是上一轮的报告。两项只重判一项时，另一项若不同步带过来，报告里就会
    凭空少一行——「这一项检查通过」的记录丢了，合成前那份提醒会以为从没检过。
    """
    items = list(report["items"])
    have = {i.get("key") for i in items}
    for src in (carry or {}).get("items") or []:
        key = src.get("key") or ""
        if key.startswith("check_") and key not in have:
            items.append(src)
    refreshed = {c["key"] for c in content["items"]}
    items = [i for i in items if i.get("key") not in refreshed]
    items.extend(content["items"])
    merged = _summarize(items, cfg)
    if report.get("flips"):
        merged["flips"] = report["flips"]
    # 标记内容检跑过了：合成阶段据此决定要不要补检（复用旧脚本时没跑过）。
    merged["content_checked"] = any(
        (i.get("key") or "").startswith("check_") for i in items)
    return merged


# ---------------------------------------------------------------- 分段生成
#
# 整篇一次生成治不了字数漂移：模型写作时没有全局计数器，「约 6024 字 / 335 句」
# 只是软引导——期 1 实测写出 1156 句（+289%），期 3 又只写出六成（−38.6%），
# 双向都漂。分段生成把一次性大赌注换成小步闭环：
#
#   规划轮只做「凝缩分组」（模型**不报任何数字**——账本单位是字，0.75 句没法数）；
#   字数配额由程序按凝缩字数占比分（算术归代码，规划第一天起就精确）；
#   逐段生成后程序数该段实际字数，超差重摇该段，差额滚入下段配额——
#   误差逐段吸收，不再累积成整篇的倍数漂移。
#
# 账本口径只有字；句数只出现在提示词里当形态引导（配合单句 8~40 字门禁），
# 永不进核账。走不走这条路由**项目模式**定死，没有开关：成稿规划（mapped）
# 走分段，逐期即兴与单集走整篇。mapped 但各节凝缩不可得（还没排图、凝缩
# 版本对不上）时退回整篇一路，并落日志说明。

SEGMENT_MIN_SENTS = 15      # 段形态引导下限：太碎则接缝多、调用翻倍
SEGMENT_MAX_SENTS = 40      # 段形态引导上限：非推理模型可靠计数的边界
SEGMENT_TOL_FRAC = 0.15     # 段字数容差（比例）——与总时长门禁同一把尺
SEGMENT_TOL_MIN_CHARS = 60  # 段字数容差（绝对）：短段免碎核，与比例取大
SEGMENT_RETRIES = 2         # 单段超配额的重摇上限
PLAN_RETRIES = 2            # 规划打回上限，仍不过就按「一节一段」程序硬分
SEGMENT_MAX_COUNT = 30      # 段数上限：防规划轮把稿子切碎成渣
SEGMENT_FIXED_CHARS = 400   # 段提示词固定文案的字符数（算素材额度用，量级即可）


def _emotion_rule(level):
    """档位决定 `emotion` 能填什么：写侧与说侧共用的同一句口径。"""
    if level == "light":
        return ("从下面这个集合里选一个，不得自创。**底色以「这批素材该怎么"
                "写成对话」给的基调为准**；同一段连贯的话可以沿用同一个标签，"
                "不必逐句换："), emotion_vocab(level)
    return ("本档**不写心情**——从下面这个集合里选一个，不得自创。"
            "这些标签说的是这句话在做什么，不是说话人的心情："), emotion_vocab(level)


def _episode_block(project):
    """「本期计划」块：地图定下的标题/主旨/要点/取材 + 本期素材体量。有则给。

    体量这一项是**给模型的预算**，也是「该写多长」这件事唯一的实物依据。从前
    只告诉它要写多少、不告诉它手里有多少——素材撑不满目标字数时，它唯一的
    出路就是凑，而实测的凑法是把写过的整段再背一遍。把数字摆出来，「素材不够
    就照实写短」才是一句它能执行的话，而不是一句漂亮的禁令。
    """
    project = project or {}
    ep_title = str(project.get("title") or "").strip()
    ep_gist = str(project.get("gist") or "").strip()
    ep_points = [str(p).strip() for p in (project.get("points") or [])
                 if str(p).strip()]
    ep_sources = [str(x).strip() for x in (project.get("sources") or [])
                  if str(x).strip()]
    ep_chars = int(project.get("chars") or 0)
    if not (ep_title or ep_points or ep_gist or ep_chars):
        return ""
    block = ["## 本期计划", "本期是既定播出计划中的一期，下面这些已经定下，照它写："]
    if ep_title:
        block.append("- 本期标题：%s" % ep_title)
    if ep_gist:
        block.append("- 本期主旨：%s" % ep_gist)
    for p in ep_points:
        block.append("- %s" % p)
    if ep_sources:
        block.append("- 本期取材：%s" % "、".join(ep_sources))
        block.append("  素材已按这几节取全，不要超出这个范围去找内容。")
    if ep_chars:
        block.append("- **本期素材共约 %d 字**，这就是你手上的全部依据。" % ep_chars)
        block.append("  照这些素材写：写得满就写满，写不满就**按素材撑得起的内容"
                     "照实写短**。字数不达标是可以接受的；**把已经写过的句子或"
                     "段落再写一遍来凑数是不允许的**——重复的内容会被程序删除，"
                     "删完照样不达标。")
    return "\n".join(block) + "\n"


def _hosts_text(card, cfg):
    """主持人站位：卡上写了 cast 就用卡上的，否则退回内置分工口径。"""
    name_a = cfg.get("tts.name_a", "A")
    name_b = cfg.get("tts.name_b", "B")
    cast = str(card.get("cast") or "").strip()
    if cast:
        return ("A 称呼「%s」，B 称呼「%s」。两人的站位与分工：\n%s"
                % (name_a, name_b, cast))
    return ("- A（%s）：负责抛出问题、提出判断、追问\n"
            "- B（%s）：负责解释、补充、举例、收束" % (name_a, name_b))


def _seg_weight(sec):
    """一节凝缩的「分量」：主旨加要点的字数。配额按它占比分配。"""
    return max(1, len(str(sec.get("gist") or ""))
               + sum(len(str(p)) for p in (sec.get("points") or [])))


def _segment_plan_schema():
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "segments": {
                "type": "array",
                # maxItems 是失控刹车：约束解码下模型能把 "segments":[ 无限写
                # 下去（实测单次 6300+ token 不停），写到上限语法强制收尾。
                "maxItems": SEGMENT_MAX_COUNT,
                "items": {
                    "type": "object",
                    "properties": {
                        "sections": {"type": "array",
                                     "items": {"type": "integer", "minimum": 1}},
                        "topic": {"type": "string"},
                    },
                    "required": ["sections", "topic"],
                },
            },
        },
        "required": ["title", "segments"],
    }


def segment_schema(level=None):
    """单段输出的结构 schema：只有 lines，`emotion` 枚举按档位收窄。

    与 `script_schema` 同一个道理——分段这一路同样产出 `emotion`，不收窄
    等于给档位留后门：整篇填不出的心情词，从段里又填进来了。
    lines 的 maxItems 是失控刹车：约束解码下模型能把数组无限写下去
    （形态上限 40 句 × 2 倍余量封顶），写到上限语法强制收尾。
    """
    schema = {
        "type": "object",
        "properties": {"lines": copy.deepcopy(SCRIPT_SCHEMA["properties"]["lines"])},
        "required": ["lines"],
    }
    schema["properties"]["lines"]["maxItems"] = SEGMENT_MAX_SENTS * 2
    schema["properties"]["lines"]["items"]["properties"]["emotion"]["enum"] = list(
        emotion_vocab(level))
    return schema


def _segment_plan_system(target_chars):
    return """你是播客脚本的分段规划者，回答只输出 JSON，不要任何其它文字。

任务：把给出的各节凝缩要点**分组**，切成若干段。这一步只做「切与排」，不写台词。

铁律：
- 只能使用列出的凝缩要点，不得发明、引申任何新要点。
- 一段 = 逻辑完整的一组要点（一个论点、一个步骤、一个故事拍），段内要点按讲述顺序排。
- 每段预计写成 %d~%d 句对话。太碎的组并进相邻段，过大的组拆开。
- 编号列出的每一节都必须至少被一个段覆盖；每段标出它覆盖的节编号。
- segments 数组顺序 = 全篇讲述顺序。

输出格式：
{"title": "本期标题", "segments": [{"sections": [1, 2], "topic": "本段一句话主题"}]}
- title：%d 字以内，概括本期主旨。不要书名号、引号、期号，不要写成完整句子。
- topic：写给后面逐段写作的模型看的一句话指令：这段讲什么、从哪里起、到哪里止。
""" % (SEGMENT_MIN_SENTS, SEGMENT_MAX_SENTS, TITLE_MAX)


def _segment_plan_user(project, evidence, target_chars):
    lines = []
    block = _episode_block(project).strip()
    if block:
        lines += [block, ""]
    lines.append("【各节凝缩】（sections 里填这里的编号，原样用数字）")
    for i, s in enumerate((evidence.get("sections") or []), 1):
        lines.append("第 %d 节（来源 %s · %s，凝缩 %d 字）"
                     % (i, s.get("source") or "?", s.get("anchor") or "?",
                        _seg_weight(s)))
        if s.get("gist"):
            lines.append("  主旨：%s" % s["gist"])
        for p in s.get("points") or []:
            lines.append("  - %s" % p)
    lines.append("")
    lines.append("【目标】全篇正文合计约 %d 字。把上面的凝缩要点分组，输出 segments。"
                 % int(target_chars))
    return "\n".join(lines)


def _validate_plan(data, n_sections):
    """校验规划：段数、节编号越界、**每一节都被覆盖且只被覆盖一次**。"""

    segs = (data or {}).get("segments")
    if not isinstance(segs, list) or not segs or len(segs) > SEGMENT_MAX_COUNT:
        return None, "segments 缺失、为空或段数出格（上限 %d 段）" % SEGMENT_MAX_COUNT
    groups, covered, twice = [], set(), []
    for s in segs:
        if not isinstance(s, dict):
            return None, "段元素不是对象"
        idx = s.get("sections")
        topic = str(s.get("topic") or "").strip()
        if not isinstance(idx, list) or not idx or not topic:
            return None, "段缺 sections 或 topic"
        clean = []
        for v in idx:
            try:
                k = int(v)
            except (TypeError, ValueError):
                return None, "节编号不是整数：%r" % (v,)
            if not 1 <= k <= n_sections:
                return None, "节编号 %d 越界（共 %d 节）" % (k, n_sections)
            clean.append(k)
        # 同一节只许进一段。段内重复先归并——模型常把同一节在同一段里列两遍，
        # 那是笔误，归并即好；跨段重复直接打回：两段都会把它写一遍，成稿里
        # 同一份内容出现两次。那不是「覆盖到了」，是同一条重复。
        uniq = sorted(set(clean))
        twice += [k for k in uniq if k in covered]
        groups.append({"sections": uniq, "topic": topic})
        covered.update(uniq)
    if twice:
        return None, ("第 %s 节被分进了多个段：同一节只能进一段，"
                      "否则会被写两遍" % "、".join(str(k) for k in sorted(set(twice))))
    missed = [str(i + 1) for i in range(n_sections) if (i + 1) not in covered]
    if missed:
        return None, "第 %s 节没被任何段覆盖" % "、".join(missed)
    return groups, None


def _allocate_segment_quotas(groups, sections, target_chars):
    """字数配额按凝缩字数占比分到各段；余数补给最大的段，合计精确等于目标。"""
    weights = [max(1, sum(_seg_weight(sections[i - 1]) for i in g["sections"]))
               for g in groups]
    total_w = float(sum(weights)) or float(len(groups))
    quotas = [int(round(int(target_chars) * w / total_w)) for w in weights]
    quotas[quotas.index(max(quotas))] += int(target_chars) - sum(quotas)
    for g, q in zip(groups, quotas):
        g["quota"] = max(SEGMENT_TOL_MIN_CHARS, q)
    return groups


def _segment_system_prompt(cfg, card, level, preset_key, index, total, quota,
                           topic, is_first, feedback=None):
    preset = PRESET_SPEC.get(preset_key) or PRESET_SPEC["argument"]
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    max_run = paradigms.max_run_of(card)
    card_block = paradigms.script_block(card)
    emo_rule, emo_allowed = _emotion_rule(level)
    lo = max(SEGMENT_MIN_SENTS, int(quota / max(1, max_c)))
    hi = min(SEGMENT_MAX_SENTS, int(quota / max(1, min_c)) + 1)
    style_rows = [style_dim_row(dim, preset.get(dim), level)
                  for dim in ("genre", "emotion_density", "question_rate",
                              "metaphor_density", "interaction")]
    opening = ("本段是全篇的第一段：直接进入正题。"
               if is_first else
               "本段是正文中段：直接接住上文继续讲。")
    head_feedback = ("\n\n【上一版本段的问题】%s\n重写本段，把字数拉回配额。" % feedback
                     if feedback else "")
    return """你负责把素材改写成两位主持人的对话脚本。本篇采用**分段写作**：你只写其中一段。

【本段任务】（硬性契约）
- 这是全篇正文的第 %d 段，共 %d 段。全篇正文长度由各段配额合计构成；**本段配额约 %d 字**（大致 %d~%d 句，每句 %d~%d 字）。
- 本段主题：%s
- %s
- 只输出本段的句子：不要输出标题；不要问候（片头）或收尾（片尾）——它们由程序按模板补齐；不要「下面我们」「以上就是」这类过渡腔。
%s
【输出格式】（硬性契约，逐条满足）
只输出一个 JSON 对象，不要任何解释文字，不要代码块标记：
{"lines": [{"speaker": "A", "text": "台词正文，只写要念出来的话", "emotion": "平静"}]}
- lines：正文数组，元素固定三个字段。**每个字段只装它自己的东西，互不串场**：
  - speaker：只装一个大写字母 "A" 或 "B"。人名、称呼、其他字母一律不进。
  - text：只装**会被逐字合成语音念出来的台词正文**。text 里的每个字都会被
    念出来——所以下面这些一律不进 text：标签词（「解释」「追问」「强调」
    「比喻」这类，它们属于 emotion 字段）、说话人标记（「A:」「B:」）、任何
    形式的标签、注释或说明。
    正确：{"speaker": "A", "text": "AI接管体力劳动之后，人该往哪走？", "emotion": "追问"}
    错误：{"speaker": "A", "text": "追问AI接管体力劳动之后，人该往哪走？", "emotion": "追问"}
    （「追问」混进了 text 开头——这就是错误示范，标签只能待在 emotion 字段里）
  - emotion：只装标签词，从下面的词表选一个，规则：%s
%s
【生成要求】（逐条满足）
1. 每句台词在 %d 到 %d 字之间。
2. 说话人由**内容**定，不由位置定：一句完整的话由同一个人说完，不许拆给两个人；同一人最多连着说 %d 句。
3. 台词里的每个论断都要能对应到素材内容，不添加素材之外的事实、数据或来源。
4. 措辞禁忌（写完逐句回查一遍，命中就当场改掉）：%s
5. %s

【风格倾向】（全篇通用背景，与本期具体内容无关；与「本段任务」冲突时以本段任务为准）
%s

【两位主持人】（既定阵容，不由你指定）
%s
""" % (index, total, quota, lo, hi, min_c, max_c, topic, opening, head_feedback,
       emo_rule, "、".join(emo_allowed), min_c, max_c, max_run,
       format_banned_rules(), READABLE_RULE,
       "\n".join(style_rows), _hosts_text(card, cfg))


def _segment_user_prompt(project, evidence, group, quota, written_chars,
                         target_chars, fit, prev_text, extra, feedback):
    secs = evidence.get("sections") or []
    lines = []
    block = _episode_block(project).strip()
    if block:
        lines += [block, ""]
    lines.append("【本段覆盖的凝缩要点】（只写这些；写完即止，不得超出）")
    for i in group["sections"]:
        s = secs[i - 1]
        head = "第 %d 节（%s）" % (i, s.get("anchor") or "未命名")
        if s.get("gist"):
            lines.append("%s：%s" % (head, s["gist"]))
        else:
            lines.append(head)
        for p in s.get("points") or []:
            lines.append("  - %s" % p)
    lines.append("")
    lines.append("【进度】全篇正文目标约 %d 字，已写 %d 字，本段配额约 %d 字。"
                 % (int(target_chars), int(written_chars), int(quota)))
    lines.append("")
    lines.append("【本期素材】")
    lines.append(fit or "（素材未喂入，按凝缩要点写）")
    lines.append("")
    if prev_text:
        lines += ["【已写正文】（只供两件事：接住语气、不重复已写论断；禁止续写它）",
                  prev_text, ""]
    if extra:
        lines += ["【补充说明】%s" % extra, ""]
    if feedback:
        lines += ["【上一版的问题】%s" % feedback, ""]
    return "\n".join(lines)


def _extract_json_object(raw):
    """从模型原始输出里取出第一个 JSON 对象。失败抛 ScriptError。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start == -1:
        raise ScriptError("输出中没有找到 JSON 对象。")
    end = text.rfind("}")
    if end <= start:
        raise ScriptError("输出中没有找到完整 JSON 对象。")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ScriptError("输出的 JSON 无法解析：%s" % e)


def _parse_segment_lines(raw):
    """解析单段输出 {"lines": [...]}。解析失败抛 ScriptError。"""
    data = _extract_json_object(raw)
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list) or not lines:
        raise ScriptError("本段输出缺 lines 数组。")
    out = []
    for l in lines:
        if not isinstance(l, dict):
            raise ScriptError("lines 元素不是对象。")
        sp = str(l.get("speaker") or "").strip().upper()
        tx = str(l.get("text") or "").strip()
        if sp not in ("A", "B") or not tx:
            raise ScriptError("lines 元素缺合法的 speaker 或 text。")
        out.append({"speaker": sp, "text": tx,
                    "emotion": str(l.get("emotion") or "").strip()})
    return out


def _segments_plan(llm, cfg, project, evidence, target_chars, log, should_stop):
    """规划轮：凝缩分组。返回 (title, groups)；反复不过则程序按「一节一段」硬分。"""
    secs = evidence.get("sections") or []
    system = _segment_plan_system(target_chars)
    user = _segment_plan_user(project, evidence, target_chars)
    title, fb = "", ""
    for attempt in range(PLAN_RETRIES + 1):
        if should_stop and should_stop():
            break
        u = user if not fb else (user + "\n\n【上一版规划的问题】%s\n请重新分组。" % fb)
        log("规划轮：第 %d 次尝试…" % (attempt + 1))
        raw, _meta = llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": u}],
            temperature=float(cfg.get("llm.temperature", 0.8)),
            # 规划是小输出：给 4096 封顶。maxItems 挡住结构失控，这里挡住
            # 「合法但啰嗦」——一份规划绝不需要 4 万 token 去写。
            max_tokens=min(int(cfg.get("llm.max_tokens", 8192)), 4096),
            json_schema=_segment_plan_schema())
        try:
            data = _extract_json_object(raw)
            if not isinstance(data, dict):
                raise ScriptError("输出不是 JSON 对象。")
        except ScriptError as e:
            fb = str(e)
            log("规划打回（第 %d 次）：%s" % (attempt + 1, fb))
            continue
        title = str(data.get("title") or "").strip()[:TITLE_MAX]
        groups, err = _validate_plan(data, len(secs))
        if groups:
            return title, groups
        fb = err or "规划不合法。"
        log("规划打回（第 %d 次）：%s" % (attempt + 1, fb))
    # 程序硬分兜底：一节一段，顺序不变。分配是算术，算术归代码。
    log("规划 %d 次仍未通过，按「一节一段」程序硬分（%d 段）" % (PLAN_RETRIES + 1, len(secs)))
    groups = [{"sections": [i + 1],
               "topic": (str(secs[i].get("gist") or "")[:40] or "本节内容")}
              for i in range(len(secs))]
    return title, groups


def _generate_segmented(llm, cfg, material, evidence, card, level, preset_key,
                        project, target_chars, log, should_stop, draft_sink,
                        extra=""):
    """分段生成整篇初稿：规划一轮 → 逐段生成 → 每段字数核账。

    返回 (title, plan, script_lines)；无法规划时返回 None（调用方退回整篇一路）。
    账本单位只有字：配额按凝缩字数占比程序分配；段写完程序数实际字数，
    超差重摇该段（取最接近配额的一版），差额按比例滚入后面各段的配额。
    """
    secs = evidence.get("sections") or []
    if not secs:
        return None
    log("分段生成：规划轮开始（%d 节，全篇目标约 %d 字）" % (len(secs), int(target_chars)))
    title, groups = _segments_plan(llm, cfg, project, evidence, target_chars,
                                   log, should_stop)
    groups = _allocate_segment_quotas(groups, secs, target_chars)
    log("规划完成：%d 段，配额合计 %d 字（%s）"
        % (len(groups), sum(g["quota"] for g in groups),
           "、".join("第%d段%d字" % (i + 1, g["quota"])
                     for i, g in enumerate(groups))))

    plan = int(project.get("planned_episodes") or 0) if project else 0
    script, written = [], 0
    planned = [g["quota"] for g in groups]
    for i, g in enumerate(groups, 1):
        if should_stop and should_stop():
            log("收到中止：第 %d 段不跑了，带着已写 %d 段收工" % (i, i - 1))
            break
        # 动态配额：前面段的差额按比例摊到本段，总账始终咬住目标。
        remaining_planned = sum(planned[i - 1:])
        quota = g["quota"]
        if i > 1 and remaining_planned > 0:
            quota = max(SEGMENT_TOL_MIN_CHARS,
                        int(round(g["quota"] * (int(target_chars) - written)
                                  / float(remaining_planned))))
        topic = g["topic"]
        attempt, best, best_gap, feedback = 0, None, None, None
        while True:
            if should_stop and should_stop():
                break
            system = _segment_system_prompt(cfg, card, level, preset_key, i,
                                            len(groups), quota, topic,
                                            is_first=(i == 1), feedback=feedback)
            prev_text = "\n".join("%s：%s" % (l["speaker"], l["text"])
                                  for l in script) if script else ""
            fit, _note = fit_material(
                llm, material, cfg,
                other_chars=len(system) + len(prev_text) + len(extra or "")
                            + SEGMENT_FIXED_CHARS,
                evidence=evidence, log=log)
            user = _segment_user_prompt(project, evidence, g, quota, written,
                                        target_chars, fit, prev_text, extra,
                                        feedback)
            log("段 %d/%d：调用模型…（配额 %d 字，主题：%s）"
                % (i, len(groups), quota, topic[:30]))
            raw, meta = llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=segment_schema(level))
            log("段 %d：模型返回 %d 字符%s"
                % (i, len(raw), "（约束解码已降级）" if meta.get("degraded") else ""))
            try:
                seg_lines = _parse_segment_lines(raw)
            except ScriptError as e:
                attempt += 1
                if attempt > SEGMENT_RETRIES:
                    raise ScriptError("第 %d 段连续 %d 次输出无法解析：%s"
                                      % (i, SEGMENT_RETRIES + 1, e))
                feedback = "本段输出没法解析（%s）。请只输出本段的 lines JSON。" % e
                log("段 %d：%s" % (i, feedback))
                continue
            chars = sum(len(l["text"]) for l in seg_lines)
            gap = chars - quota
            if best is None or abs(gap) < abs(best_gap):
                best, best_gap = seg_lines, gap
            tol = max(int(quota * SEGMENT_TOL_FRAC), SEGMENT_TOL_MIN_CHARS)
            if abs(gap) <= tol:
                break
            attempt += 1
            if attempt > SEGMENT_RETRIES:
                log("段 %d：重摇 %d 次仍差 %d 字，取最接近配额的一版继续"
                    % (i, SEGMENT_RETRIES, abs(best_gap)))
                break
            feedback = ("本段写了 %d 字，配额 %d 字，%s %d 字。"
                        % (chars, quota, "多了" if gap > 0 else "少了", abs(gap)))
            log("段 %d：%s重摇（第 %d 次）" % (i, feedback, attempt))
        seg_lines = best or []
        script.extend(seg_lines)
        seg_chars = sum(len(l["text"]) for l in seg_lines)
        written += seg_chars
        log("段 %d/%d 完成：%d 句 / %d 字（配额 %d；累计 %d/%d 字）"
            % (i, len(groups), len(seg_lines), seg_chars, quota,
               written, int(target_chars)))
        # 每段写完就落盘：十几段逐段生成，中途卡住、被中止，手上还有已写的部分。
        if draft_sink is not None:
            draft_sink({"script": list(script), "title": title,
                        "planned_episodes": plan,
                        "report": {"items": [], "fails": [], "warns": [],
                                   "pending": [], "soft": [], "passed": False,
                                   "partial": True}})
    if not script:
        return None
    return title, plan, script


# ------------------------------------------------------------------ 主流程
def generate(material, cfg, llm, preset_key=None, extra="",
             max_rounds=None, log=None, project=None, should_stop=None,
             draft_sink=None, evidence=None, segmented=False):
    """生成脚本并过门禁。返回 dict。

    第 1 轮整篇写；之后**能定点就定点**——只把门禁点名的句子交给模型改，全篇行数
    一个字不动。省的不只是 token：整篇重出会把没毛病的两百句重新摇一次骰子，
    改好的地方又带进新毛病，轮次全耗在打地鼠上。

    哪些问题能定点，取决于它带不带句号（见 `patch_targets`）。带句号的定点改；
    结构坏了（JSON 不合法、字段残缺）只能整篇重出；内容检自己指不出句号的，
    不重写也不硬猜，原样交人工。

    重试上限内不通过 → **把最后一版连同未通过的结论一并返回**，不抛错。脚本阶段
    到此为止：稿子和问题都落盘，人拿去改、或者直接出片，都是下一步的事。

    产出含 title —— 标题与正文同一次生成，不再让用户到合成页补。

    不接 calib：这一段的时长口径是标准语速，跟用哪个音色无关。

    `should_stop` 是放弃指令（后台任务被中止时置位）。只在**轮次之间**看它：
    正在跑的那一次调用掐不断——本地模型一个请求就是一次不可分割的生成——但
    掐不断不等于要装作没收到：一轮写完就带着已有稿子收工，比让人等它把剩下的
    几轮全跑完再丢弃强。

    `draft_sink` 每轮写完调一次，把当前这一版接出去落盘。生成一轮要十几分钟，
    等到全部跑完才第一次写盘，中途卡住或被中止就什么都没有——先落盘，后面
    无论出什么事，手上都有一份完整的稿子。

    `evidence` 是本期判据包（主旨 + 各节凝缩）：内容检判「方向」时用它，素材
    放不下时也用它顶替原文。素材本身按素材容量裁（见 `fit_material`）——容量
    由「输出上限 × 压缩档」推导（`material_capacity`），不再由写死的 6000 字
    决定，也不由最大输出推。

    `segmented` 是**路线判据，由调用方按项目模式给出**：成稿规划（mapped）为真，
    逐期即兴与单集为假。它不是给人选的偏好——两条路各自成立的前提不同：mapped
    有地图与各节凝缩，逐期配额、防重复、段级核账才有依据；episodic 与 single
    是当场给料、出完即止，没有凝缩可分，整篇一次写完就是这条路该有的样子。
    mapped 但本期凝缩缺失（项目还没排图、凝缩版本对不上）时仍退回整篇，不让
    人卡在一步跑不动。
    """
    preset_key = preset_key or cfg.get("script.style_preset", "argument")
    extra = extra if extra is not None else cfg.get("script.extra_requirement", "")
    max_rounds = int(max_rounds if max_rounds is not None else cfg.get("script.max_llm_rounds", 3))
    log = log or (lambda m: None)

    # 输入容量与窗口自查提醒，一期只报这一次。参照系是成稿脚本：容量 =
    # 素材容量 = 成稿目标 × 偏移 1.25 × 档位（下限 = 成稿 × 1.25 × 1.2），
    # llm.max_tokens 只是输出预算（思考段与答案共用）。要求多少 token 摆在
    # 日志里，后端窗口够不够一目了然，不用等爆出一句看不懂的后端报错再回头猜。
    if hasattr(llm, "quota_note"):
        log("本期素材容量 %d 字（= 成稿目标 %d 字 × 偏移 1.25 × 1:%d 档；"
            "下限 = 成稿 × 1.25 × 1.2 = %d 字）"
            % (material_capacity(cfg),
               probe.target_chars(cfg), probe.ratio_of(cfg),
               int(probe.offset_chars(cfg) * probe.MIN_RATIO)))
        log(llm.quota_note(int(cfg.get("llm.max_tokens", 8192)),
                           material_capacity(cfg)))

    target_seconds = float(cfg.get("script.target_minutes", 4.0)) * 60.0
    speed = float(cfg.get("tts.speed_a", 1.0))
    stats = duration_model.standard_stats()
    # 片头尾若用外部音频，其时长要从目标里扣掉。
    # 用脚本句子充当片头尾则不需要扣，固定扣减等于凭空削减目标，
    # 叠加模型偏保守的输出，会让总时长长期偏短。
    io_seconds = 0.0
    for key in ("audio.intro_path", "audio.outro_path"):
        p = cfg.get(key) or ""
        if p and os.path.exists(p):
            io_seconds += audio_engine.probe_duration_safe(p)
    if io_seconds > 0:
        log("片头尾外部音频共 %.1f 秒，已从目标时长扣除" % io_seconds)
    # 目标字数得按「纯语音」的秒数反推：句间停顿不是语音，要先扣掉。不扣的话，
    # 照着目标字数写满的稿子再加上停顿必然超出——334 句的停顿就有 117 秒，占目标
    # 的 7.8%，而容差里从没为它留过位置，等于让模型站不进格子。
    # 句数又要由字数估出来，所以先按全额估一遍拿到句数，再回头收一次字数。
    speech_seconds = max(10.0, target_seconds - io_seconds)
    target_chars = duration_model.chars_for_target(speech_seconds, speed, stats)
    line_count = estimate_line_count(cfg, target_chars)
    pause = float(cfg.get("audio.pause_between_lines", 0.35))
    pause_budget = pause * max(0, line_count - 1)
    if pause_budget > 1.0:
        target_chars = duration_model.chars_for_target(
            max(10.0, speech_seconds - pause_budget), speed, stats)
        line_count = estimate_line_count(cfg, target_chars)
        log("句间停顿按 %d 句计约 %.0f 秒，已从目标里扣掉（这才是留给语音的部分）"
            % (line_count, pause_budget))
    io = intro_outro_spec(cfg)
    # 范式卡只取一次，提示词与门禁共用：分头各取一次，同一份素材可能拿到两张卡
    # （提示词按方法论写、门禁按自适应判），稿子会陷在「改了还是不过」。
    card = resolve_paradigm(project, cfg)
    # 档位在这一层也取一次：两条路的约束解码 schema（整篇生成、定点修补）都按它
    # 收窄。取的口径仍是 `emotion_level_of`，与提示词、门禁是同一条。
    level = paradigms.emotion_level_of(card)

    # 路线由项目模式定死（见 `segmented` 的说明）：成稿规划走分段——先按凝缩
    # 分组规划，再逐段生成、逐段核字数配额，拼出整篇初稿；逐期即兴与单集走
    # 整篇。两条路之后都汇进同一套门禁与定点修补，判据只此一处。
    pre_draft = None
    if segmented and (evidence or {}).get("sections"):
        seg = _generate_segmented(llm, cfg, material, evidence, card, level,
                                  preset_key, project, target_chars, log,
                                  should_stop, draft_sink, extra=extra)
        if seg is not None:
            seg_title, seg_plan, seg_script = seg
            flips = enforce_max_run(seg_script, paradigms.max_run_of(card))
            pinned = pin_intro_outro(seg_script, cfg)
            if pinned:
                log("片头尾已按模板写死（改写 %d 句）" % pinned)
            # 写死片头尾改的是文本，normalize 重跑一遍刷新时长字段（幂等）。
            seg_script = normalize_script(seg_script, cfg)
            seg_title = seg_title or title_from_lines(seg_script)
            # 地图已定的事以人为准：标题与总期数不挂在模型自觉上。
            if project and project.get("title"):
                seg_title = project["title"]
            pre_draft = {"script": seg_script, "title": seg_title,
                         "planned_episodes": seg_plan, "flips": flips}
        else:
            log("本期各节凝缩不可得，退回整篇生成")
    elif segmented:
        # mapped 但本期凝缩读不出来：不让人卡在「一步跑不动」上，退回整篇
        # 照常出稿，缺的那段判据由 `evidence_pack` 的日志说明。
        log("本期未取到各节凝缩，退回整篇生成")

    system = build_system_prompt(cfg, preset_key, target_chars, line_count,
                                 project=project, paradigm=card)
    log("素材类型：%s；同一人连续上限 %d 句"
        % (card.get("label") or "自适应", paradigms.max_run_of(card)))
    log("目标：%.0f 秒 / 约 %.0f 字 / 约 %d 句（按标准语速 %.2f 有效字/秒 ≈ %d 汉字/分钟）"
        % (target_seconds, target_chars, line_count,
           duration_model.STANDARD_K, duration_model.STANDARD_CPM))

    feedback = None
    prev_lines = None   # 上一版正文（编号口径与门禁报的句号一致），整篇重出时对照用
    last = None
    content_seen = None   # 上一次内容检的结论，决定这一轮重判哪几项
    for attempt in range(max_rounds + 1):
        if attempt and should_stop and should_stop():
            # 已经有一版完整稿子在手，收工。写了一半的那次调用本来就掐不断，
            # 但「下一轮」是新的一次生成，没有再开一次的理由。
            log("收到中止：第 %d 轮不跑了，带着第 %d 轮的稿子收工" % (attempt + 1, attempt))
            break

        # 第 1 轮整篇写；之后能定点就定点，结构坏了（JSON 不合法、字段残缺）
        # 才回头整篇重出。定不了点的（内容检指不出句号）既不重写也不硬猜，交人工。
        patch = None
        if attempt and last and "script" in last:
            targets, refull, manual = patch_targets(last["report"],
                                                    len(last["script"]), level)
            if refull:
                log("第 %d 轮：稿子结构坏了（%s），只能整篇重出"
                    % (attempt + 1, "、".join(p["label"] for p in refull)))
            elif targets:
                patch = targets
            else:
                if manual:
                    log("第 %d 轮：剩下的问题指不出是哪一句（%s），不重写，交人工复核"
                        % (attempt + 1, "、".join(p["label"] for p in manual)))
                break

        changed = None
        if patch:
            log("第 %d 轮：定点修补 %d 句…" % (attempt + 1, len(patch)))
            fb = _patch_feedback(patch)
            # 修补的素材同样按这次调用的余量裁：提示词里还有系统提示、要改的
            # 条目、被点名句的前后文，它们都占地方。素材放不下就用凝缩顶
            # （见 fit_material），并在日志里说明。
            psystem = patch_system(level)
            fit, _note = fit_material(
                llm, material if _needs_material(last["report"]) else "", cfg,
                other_chars=len(psystem) + len(fb) + len(prev_lines or "")
                            + PATCH_FIXED_CHARS,
                evidence=evidence, log=log)
            raw, meta = llm.chat(
                [{"role": "system", "content": psystem},
                 {"role": "user", "content": build_patch_prompt(
                     last["script"], patch, fb, material=fit,
                     evidence=evidence)}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=patch_schema(level))
            log("第 %d 轮：模型返回 %d 字符%s"
                % (attempt + 1, len(raw),
                   "（约束解码已降级）" if meta.get("degraded") else ""))
            try:
                edits = parse_patch(raw)
                script = apply_patch(last["script"], edits, patch)
            except ScriptError as e:
                # 补丁没落地就整篇重出：这一轮的稿子一个字没动，退回上一版不算损失。
                # 不拿「半份补丁」凑合——那会让「改了几处、漏了哪几处」说不清。
                log("第 %d 轮：补丁没法落地（%s），改回整篇重出" % (attempt + 1, e))
                patch = None
            else:
                flips = enforce_max_run(script, paradigms.max_run_of(card))
                pinned = pin_intro_outro(script, cfg)
                if pinned:
                    log("片头尾已按模板写死（改写 %d 句）" % pinned)
                script = normalize_script(script, cfg)
                title = last["title"]
                plan = last["planned_episodes"]
                changed = len({e["index"] for e in edits})
                # 模型会漏改：实测里点名 7 句、它只回了 6 句，没回的那句原样留着。
                # 不把它当成失败——半份补丁也是净收益，漏掉的那句下一轮门禁会再
                # 报一次，接着补就是。但必须说出来，否则「为什么还是没过」会变成一个
                # 没人知道的悬案。
                if changed < len(patch):
                    log("第 %d 轮：只补到 %d 句，还差 %d 句没回（下一轮再补）"
                        % (attempt + 1, changed, len(patch) - changed))

        if not patch and pre_draft is not None:
            # 分段初稿已到手：这一轮只做门禁与后续修补，不再整篇生成。
            script = pre_draft["script"]
            title = pre_draft["title"]
            plan = pre_draft["planned_episodes"]
            flips = pre_draft.get("flips") or []
            # 公共尾巴记 raw_chars/degraded：分段路没有「原始整篇输出」，按 0 记。
            raw, meta = "", {}
            pre_draft = None
        elif not patch:
            # 整篇重出：第 1 轮走这里，之后是结构坏了或补丁落不了地。
            # 素材按这次调用的余量裁：系统提示、补充说明、上一版正文都占地方，
            # 先量再定能给素材多少字。放不下就改用凝缩，并把这件事写进日志——
            # 静默少喂会让模型在缺依据的情况下硬写，产物上看不出少了什么。
            fit, _note = fit_material(
                llm, material, cfg,
                other_chars=len(system) + len(extra or "") + len(prev_lines or "")
                            + WRITE_FIXED_CHARS,
                evidence=evidence, log=log)
            user = build_user_prompt(fit, cfg, extra, feedback, prev_lines)
            # 这一行在调用之前写。生成一轮要十几分钟，日志里若只有「模型返回」那一行，
            # 整轮期间界面上是上一次留下的字——看着像卡住了。
            log("第 %d 轮：调用模型…" % (attempt + 1))
            raw, meta = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=script_schema(level),
            )
            log("第 %d 轮：模型返回 %d 字符%s"
                % (attempt + 1, len(raw), "（约束解码已降级）" if meta.get("degraded") else ""))

            try:
                parsed = parse_script(raw)
            except ScriptError as e:
                last = {"error": str(e)}
                feedback = "输出不是合法 JSON 对象。请只输出 JSON 对象本身。"
                continue

            script = normalize_script(parsed["lines"], cfg)
            # 先压连续句、再写死片头尾。反过来的话，片尾那句是模板指定的 B——它若
            # 正好撞在「同一人连着说的第 N+1 句」上，会被翻成 A，片尾的固定标识就废了。
            flips = enforce_max_run(script, paradigms.max_run_of(card))
            pinned = pin_intro_outro(script, cfg)
            if pinned:
                log("片头尾已按模板写死（改写 %d 句）" % pinned)
            # 写死片头尾改的是文本，单句时长是按文本算出来的，得跟着刷新一次。
            # normalize 是幂等的：重跑一遍只更新 estimated_seconds，不动别的。
            # 定点那条路本来就是「改完定型再 normalize」，两条路在这里对齐——
            # 都以 normalize 收尾，落盘字段的口径不分叉。
            script = normalize_script(script, cfg)
            title = parsed["title"] or title_from_lines(script)
            # 地图已定期标题时以地图为准：本期讲什么在排地图那一步就定了。
            # 让模型每次重新命名，同一期会在计划与产物里挂上两个不同的名字。
            if project and project.get("title"):
                title = project["title"]
            # 项目已定的总期数以人为准。提示词里请模型原样回填，但它漏填或改口
            # 都不该改变结果——人定的事挂在模型的自觉上，进度迟早对不上账。
            plan = parsed["planned_episodes"]
            if project and project.get("planned_episodes"):
                plan = int(project["planned_episodes"])

        # 硬去重：模型凑数时会把写过的整段再背一遍（实测 302 句里 129 句是重复）。
        # 放在门禁与落盘之前、两条路共用——重复的内容不该进成片，也不该让时长
        # 门禁对着注水后的句数判「达标」。
        script, dropped = dedupe_script(script)
        if dropped:
            log("去重：删掉 %d 句与前文完全重复的内容（重复段不进成片）" % dropped)
            # 删句会让原本被重复段隔开的同一个人挨到一起，连续句上限重核一遍。
            flips = enforce_max_run(script, paradigms.max_run_of(card)) or flips

        # 记下这一版的正文。编号必须取这一版定型后的序号（含片头尾写死与连续句
        # 翻转的结果）——门禁报的「第 58 句」就是按这个序号数的，拿别的版本去
        # 对，模型会改错行。
        prev_lines = "\n".join("%d. [%s] %s" % (i + 1, s["speaker"], s["text"])
                               for i, s in enumerate(script))

        report = gate_generate(script, cfg, card)
        if flips:
            report["flips"] = flips
        if dropped:
            # 删得掉不等于没发生：凑过数这件事要留痕，人才知道这一期的素材其实
            # 撑不满目标时长——不然只是成片悄悄短了一截。记成 soft 项：稿子已经
            # 被程序改干净了，没有理由再拦人，也没有理由再烧一轮重写。
            report["items"].append({
                "key": "no_repeat", "label": "重复凑数", "level": "warn",
                "judge": "code", "ok": False, "soft": True,
                "detail": "有 %d 句与前文完全重复（同一段被复播），已由程序删除；"
                          "重复内容不会进成片，但这说明素材撑不满目标时长——"
                          "请补充素材或调低每期时长。" % dropped})
            report["soft"] = [i for i in report["items"]
                              if not i["ok"] and i.get("soft")]
        if not title:
            report["items"].append({
                "key": "title_present", "label": "本期标题", "level": "fail",
                "judge": "code", "ok": False,
                "detail": "模型未产出标题，正文也无法推出可用标题。"})
            report["fails"] = [i for i in report["items"]
                               if not i["ok"] and i["level"] == "fail"]
            report["passed"] = False

        # 内容检（语义检 / 承诺链检）接在这里，图的就是这一刻：脚本还没落盘、
        # 没进合成，打回重写只是重跑一次生成。等产物出来再检，片子已经渲染完，
        # 检出来也改不动——那才是「检了等于没检」。
        # 形式项全过了才跑：一份字数都不达标的稿子，先改形式，不值得为它花一次调用。
        # 重判范围只取「上一轮没过或没判过」的那几项——已经判通过的那一项，本轮改的
        # 又是措辞、句长这类形的事，没有理由把同一段上下文再喂一遍。没重判的那一项
        # 由 merge_check 从上一轮带过来，报告里不会凭空少一行。
        if report["passed"] and llm is not None:
            dims = (dims_to_recheck(content_seen) if content_seen
                    else set(k for k, _ in CHECK6_DIMS))
            if dims:
                content = gate_check6(script, cfg, llm=llm, material=material,
                                      dims=dims, evidence=evidence)
                report = merge_check(report, content, cfg, carry=content_seen)
            else:
                report = merge_check(report, {"items": []}, cfg, carry=content_seen)
            content_seen = report

        last = {"script": script, "title": title,
                "planned_episodes": plan,
                "report": report, "raw_chars": len(raw),
                "degraded": meta.get("degraded", False), "attempt": attempt + 1}
        if changed is not None:
            last["patched_lines"] = changed
        # 每轮写完就落盘：稿子在手，后面哪一轮卡住、被中止、进程崩了，都不至于
        # 从头再来。落的是「当前最新的那一版」，与最终返回的那版一致。
        if draft_sink is not None:
            draft_sink(last)

        if report["passed"]:
            log("门禁通过（第 %d 轮）" % (attempt + 1))
            return last

        # 回灌只给「够格拦人」的项。soft 项（总时长）不拦人，也就不该拿去让模型
        # 重写：它既测不出也控不住，围着它改只会把内容改坏。advisory 项是模型自己
        # 没给出结论的，把「交人工复核」这种话递给写作模型毫无意义。
        if report.get("soft"):
            log("门禁提示（不阻断）：%s"
                % "、".join("%s %s" % (p["label"], p.get("detail", ""))
                            for p in report["soft"]))
        problems = [i for i in report["items"]
                    if not i["ok"] and not i.get("soft") and not i.get("advisory")]
        if problems:
            log("门禁未过：%d 处（%s）"
                % (len(problems), "、".join(p["label"] for p in problems[:6])))
        feedback = _build_feedback(problems)

    if last and "script" in last:
        return last
    raise ScriptError("脚本生成失败：%s" % (last or {}).get("error", "未知原因"))


def _feedback_banned(p):
    """措辞命中的回灌：逐句给词、给替换落点，并说明只动这几句。

    两条都是必要的。只报「第 58 句命中了」而不给替换词，模型知道要绕开什么、
    不知道绕去哪里，只会换成意思相同、照样把话说满的另一个词。而若不明说
    「只改这几句」，模型会通篇重写——重写出来的新句子又会带进新的命中，
    上一轮改掉的那几处白改，轮次全耗在打地鼠上。
    """
    hits = p.get("hits") or []
    if not hits:
        return ""
    rows = ["- 措辞禁忌命中 %d 处。**只改下面这几句的用词，其余句子一律照抄**"
            "（A/B 对话体，换掉一句的说法不影响上下文怎么解释，不必重写别的）："
            % len(hits)]
    for h in hits:
        tip = "→ 换成%s 这类留余地的说法" % h["substitute"] if h.get("substitute") else ""
        rows.append("    第 %d 句「%s」（%s）%s"
                    % (h["line"], h["word"], h["label"], tip))
    return "\n".join(rows)


def _feedback_readable(p):
    """无法朗读形状的回灌：逐句给形状与样例，并说明只动这几句。

    与措辞禁忌同一条纪律：不明说「只改这几句」，模型会通篇重写，把上一轮
    改好的地方原样带进新的命中。指落点给动作，不给可照抄的说法——形状
    各句不同，说法只能从这一句自己的内容里长出来。
    """
    hits = p.get("hits") or []
    if not hits:
        return ""
    rows = ["- 无法朗读的内容 %d 处。**只改下面这几句，其余句子一律照抄**：" % len(hits)]
    for h in hits:
        rows.append("    第 %d 句出现%s「%s」：用普通的话交代它的意思"
                    % (h["line"], h.get("label", "符号"), h.get("sample", "")))
    return "\n".join(rows)


def _feedback_length(p):
    """句长越界的回灌：逐句给句号与字数，并给出改法。

    **不许拆句、不许并句**——两条都会改行数。行数一变，后面每一句的句号都挪一位，
    门禁报的「第 N 句」就跟人对不上了，定点修补那条路也走不成。过长只能精简，
    过短只能就地补内容：字数的问题在字数上解决，不动结构。

    从前这里写着「拆成两句（行数会多一行，后面各行顺延）」，那是整篇重出时代的
    写法——反正整篇都要重打，行数本来就不作数。
    """
    longs = p.get("too_long") or []
    shorts = p.get("too_short") or []
    if not (longs or shorts):
        return ""
    lo, hi = p.get("lo", 8), p.get("hi", 40)
    rows = ["- 句长越界 %d 处（要求 %d–%d 字）。**只改下面这几句**："
            % (len(longs) + len(shorts), lo, hi)]
    for h in longs:
        rows.append("    第 %d 句 %d 字（超出上限 %d 字）：精简掉可有可无的修饰，"
                    "压到 %d 字以内。**不许拆句**——拆一句会多出一行，"
                    "后面每一句的句号都跟着挪一位"
                    % (h["line"], h["chars"], h["chars"] - hi, hi))
    for h in shorts:
        rows.append("    第 %d 句 %d 字（差 %d 字）：就地把内容补到 %d 字以上，"
                    "**不许并到相邻句里去**（并句往往直接顶破单句上限）"
                    % (h["line"], h["chars"], lo - h["chars"], lo))
    return "\n".join(rows)


def _build_feedback(problems):
    """把门禁不通过项回灌给模型。

    只给可操作的信息，不给它自评判据。能定点修的（措辞、句长）给到句、给到
    替换落点；其余项照原样带过，只给事实不给结论。
    """
    rows = []
    for p in problems:
        key = p.get("key")
        if key == "banned_words":
            txt = _feedback_banned(p)
        elif key == "readable_text":
            txt = _feedback_readable(p)
        elif key == "line_length":
            txt = _feedback_length(p)
        else:
            txt = "- %s：%s" % (p["label"], p.get("detail", ""))
        if txt:
            rows.append(txt)
    return "\n".join(rows)
