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
import math
import os
import re
from datetime import datetime

from . import audio_engine, duration_model, paradigms, probe
from .config_manager import (BANNED_RULES, DISCOURSE_NEUTRAL, DISCOURSE_ORDER,
                             GATE_BY_KEY, INTRO_OUTRO, PRESET_SPEC, STYLE_DIMS)


def material_capacity(cfg):
    """一期素材的**内容额度**（字）：成稿目标 × 偏移量 1.25 × 压缩档。

    口径（全链唯一的算法源）：**1 = 成稿目标**（每期时长 × 标准语速，不含
    偏移——它对应人设定的时长换算出的真实字数）；**x = 1 × 1.25 × 档位**；
    下限 = 1 × 1.25 × 1.2（素材至少多出偏移后脚本两成，写作才有「压缩」
    可言）。偏移量 1.25 只进算式、不改变"1"。与排图体检（planner 的
    per_ep_max）同一把尺：排图放进去的一期素材最多就这个量。

    **它答的是「这一期该讲多少料」，不是「这次调用装不装得下」。** 后者由
    输入额度答（= 最大输出 × `llm.input_ratio`，见 `llm_client`），倍率由使用者
    自己定。两把尺单位都不同（这里是朗读字数，那里是 token），拿它去判输入
    装不装得下就是跨尺比较——技术文档那类素材 2.6 个字符才顶 1 个朗读字，
    一跨就判错。
    """
    return int(probe.capacity(cfg) * probe.RATIO_HEADROOM)

# 风格维度落到可验证形态：prompt 给引导，代码给判据
#
# 三维都进提示词，**都不碰语篇词表**。风格倾向只管调子：按什么路子推进、拿多少
# 比方、用不用互动词。谁说几句、多久问一次，都不归它管——「多久问一次」归对话
# 形式（见 paradigms.DIALOGUE_FORMS），语篇词表则只有一份常量（DISCOURSE_ORDER）。
STYLE_GUIDE = {
    "metaphor_density": {
        "low": "比喻最多一两处，别句句打比方",
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


def style_dim_row(dim, val):
    """风格倾向取一行「- 维度名：要求」。"""
    guide = STYLE_GUIDE.get(dim, {}).get(val, "")
    label = STYLE_DIMS.get(dim, {}).get("label", dim)
    return "- %s：%s" % (label, guide)


def _style_block(preset):
    """风格倾向三行：体裁、比喻密度、互动词。

    整篇、分段、插入三处共用这一份。各写各的后果不是文风分歧，是**同一份稿子
    里两套口径**：写作按「低、克制」写，插入却按别的档补句。
    """
    return "\n".join(style_dim_row(dim, preset.get(dim))
                     for dim in ("genre", "metaphor_density", "interaction"))


# ------------------------------------------------------------------ 语篇标签
#: 释义表：提示词里逐词给「词=干什么活」。顺序即 DISCOURSE_ORDER。
DISCOURSE_HELP = {
    "承接": "顺着上句往下讲，没有特别的修辞动作——大多数句子是它",
    "追问": "向对方抛出问题，把话头递过去——**这句必须是问句**",
    "解释": "把道理、机制、原因讲清楚",
    "强调": "加重语气，突出前面说过的重点",
    "比喻": "用类比或打比方来讲——**这句里要真有比喻**",
    "铺垫": "给后文要讲的东西先埋一笔",
    "过渡": "从一件事换到另一件事的桥",
    "总结": "把前面说过的收拢成结论",
}


#: 语篇词表：**只有一份**，就是 config_manager 的 DISCOURSE_ORDER（八个词）。
#:
#: 从前这里有个 `discourse_vocab_for(preset)`，按风格倾向再收窄一次（提问频率低
#: 去「追问」、情绪密度低去「铺垫/过渡」）。两次收窄的净效果都是同一件事：提示词
#: 里列十个词、门禁手里只有八个——写稿时合法的标签，判的时候成了「词表外标签」。
#: 一个词表分两处算，迟早给出两套口径，所以那个函数整个删掉了：需要在哪用，
#: 就到哪用 `DISCOURSE_ORDER`。
#:
#: 「开场 / 收束」也不在表里（v0.34.1 除名）：片头尾由程序在定稿那一刻粘上
#: （`glue_intro_outro`），正文没有哪一句该背这两个标签。


def vocab_words():
    """本篇可填的语篇标签（就是全集，顺序即 DISCOURSE_ORDER）。

    留这个函数是为了让「词表从哪来」在调用处一眼看得见：提示词、输出 schema、
    门禁三处都调它，谁也别自己列一份。
    """
    return list(DISCOURSE_ORDER)


def _vocab_block():
    """语篇词表与释义：每个词干什么活。

    整篇、分段、插入三处共用**同一份**（`vocab_words()` + `DISCOURSE_HELP`
    释义）。各处自己列一份的后果是写作按一份枚举写、插入却按另一份写——插入句
    一落进去就被 `emotion_vocab` 门禁打回，那一轮补的字全废。
    """
    return "\n".join("    - %s：%s" % (w, DISCOURSE_HELP[w])
                     for w in vocab_words())


def _line_item_schema(vocab):
    """单句 schema：speaker / emotion / text 三字段。

    emotion 的枚举就是本篇词表（`vocab_words()`）；必填——逐句必选正是它在生成侧
    的全部价值：约束解码下每句都要从枚举里挑一个语篇动作，「追问」这个动作本身
    就把句子逼成疑问句。
    """
    return {
        "type": "object",
        "properties": {
            "speaker": {"type": "string", "enum": ["A", "B"]},
            "emotion": {"type": "string", "enum": list(vocab)},
            "text": {"type": "string"},
        },
        "required": ["speaker", "emotion", "text"],
    }


def _edit_item_schema(vocab):
    """定点修补的 edit 条目：index+text 必填，emotion / absorb 可选。

    emotion 设为可选：edit 只在修被点名句子时出现，多数修补只动 text；
    但语篇标签错了（词表外、与句型不符）也要有得改——给了枚举，模型改得动。

    absorb 也设为可选，只给「同一人连着说超限」用：值 N 表示这一句替掉紧随其后的
    N 句（并句），行数因此少 N。**并句是减行数的唯一合法表达**——超限要落到上限
    以内，而补丁不许拆句、不许凭空加句，就只剩「几句并成一句」这条路。它没给
    模型开「随便增删」的口子：`apply_patch` 会核被并的每一句都在点名清单里、
    都与这一句同一个人、并出来的字没有被吃掉。
    """
    return {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "emotion": {"type": "string", "enum": list(vocab)},
            "absorb": {"type": "integer"},
            "text": {"type": "string"},
        },
        "required": ["index", "text"],
    }


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
            "items": _line_item_schema(DISCOURSE_ORDER),
        },
    },
    "required": ["title", "lines"],
}


def script_schema(vocab=None):
    """脚本输出的结构 schema：一句台词是 speaker / emotion / text 三个字段。

    `vocab` 是本篇可填的语篇标签（`vocab_words()` 的产物，就是全集）；不传也按
    全集开——**没有第二份更窄的表**：词表分两处算，就必然出现提示词列的与门禁
    判的对不上。这个参数留着只是为了测试能递一个假表进来验结构，生产路径一律走默认。
    """
    schema = copy.deepcopy(SCRIPT_SCHEMA)
    schema["properties"]["lines"]["items"] = _line_item_schema(vocab or DISCOURSE_ORDER)
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

    `paradigm` 是范式卡（`resolve_paradigm()` 的产物）：它给两人的站位（`cast`）
    与卡上的默认对话形式（`form`）。不传就在这里现取一张——取的口径只有
    `resolve_paradigm()` 一处。
    """
    name_a = cfg.get("tts.name_a", "A")
    name_b = cfg.get("tts.name_b", "B")
    preset = PRESET_SPEC.get(preset_key) or PRESET_SPEC["argument"]
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    banned_block = format_banned_rules()
    card = paradigm or resolve_paradigm(project, cfg)
    chosen_form = cfg.get("script.dialogue_form") or ""
    card_block = paradigms.run_block(card, chosen_form)

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

    # 风格倾向四行：体裁、提问频率、比喻密度、互动词。拼法收口在 `_style_block`。
    style_block = _style_block(preset)

    # 本期计划与范式卡连着给：都属「已经定下的事」，中间不夹别的段落。
    middle = "\n\n".join(x for x in (episode_block.strip(), card_block) if x)
    if middle:
        middle += "\n\n"

    # 站位写在卡上就从卡上取；卡上没写（自定义卡留空）才退回内置的分工口径。
    # 对话形式选了就顶掉卡上那段站位说明（没选＝按卡走）。卡上那份站位**不再**
    # 由 card_block 另贴一次——两处都贴，选中形式也顶不掉卡上那份，提示词里就
    # 会有两套分工说法同时在。
    hosts = _hosts_text(card, cfg, chosen_form)

    # 语篇词表只有一份：枚举里没有的词约束解码选不出来，提示词只负责
    # 把「每个词干什么活」讲清。拼法收口在 `_vocab_block`——插入那一轮用同一份。
    vocab_help = _vocab_block()

    return """你负责把素材改写成两位主持人的对话脚本。

【片头片尾】（不归你写，一句都不要写）
片头与片尾是节目的固定标识，由程序在整期定稿那一刻逐字粘上去——**你只写正文**。
不要写问候语（「欢迎收听」「大家好」「各位好」），不要报本期题目与播讲人，也不要
写收尾语（「这里是…，欢迎关注」「我们下期再见」）。它们不在你的输出里，写了就会
和程序粘上去的那一句挨在一起。
全篇正文共 %d 句左右。**第一句直接进入内容**——不要在开头复述节目名或打招呼。
**每一句都要自己判断是谁在说**——由这句话的内容决定，不是由它排在第几句决定。

【输出格式】（硬性契约，逐条满足）
只输出一个 JSON 对象。不要任何解释文字，不要代码块标记，不要在对象前后写任何内容。
对象固定三个字段，顺序如下：
{"title": "本期标题", "planned_episodes": 0, "lines": [{"speaker": "A", "emotion": "追问", "text": "台词正文，只写要念出来的话。"}]}
- title：本期标题，%d 字以内，概括本期主旨。不要书名号、引号、期号，不要写成完整句子
- planned_episodes：%s
- lines：正文数组，元素固定三个字段。**每个字段只装它自己的东西，互不串场**：
  - speaker：只写说话人，填一个大写字母 "A" 或 "B"。人名、称呼、其他字母一律不进。
  - emotion：只填语篇标签，标这句在对话里**干什么活**，每句必须从本篇词表中选一个
    （词表只有这一份，照抄下面列出的词，不在此列的一律不许编）：
%s
    正确：{"speaker": "A", "emotion": "追问", "text": "含碳12%%的铁属于钢，为什么这么说呢？"}
    正确：{"speaker": "A", "emotion": "承接", "text": "大明于1368年立朝。"}
    正确：{"speaker": "A", "emotion": "强调", "text": "这是胜利的预言家在叫喊：——让暴风雨来得更猛烈些吧！"}
    错误：{"speaker": "A", "emotion": "疑惑", "text": "疑惑含碳12%%的铁属于钢，为什么这么说呢"}
    （这一条错了三处：「疑惑」是心情不是语篇动作、不在本篇词表里；「疑惑」二字
    误进了 text 正文；句尾没有标点符号）
  - text：只填对话内容，即**会被逐字合成语音念出来的台词正文**。text 里的每个
    字都会被念出来——所以下面这些一律不进 text：语气/情绪描写（「小美疑惑地
    说」「他笑着说」）、说话人标记（「A:」「B:」）、任何形式的标签、注释或说明。
    错误：{"speaker": "A", "emotion": "追问", "text": "小美疑惑地说：为什么这么说呢？"}
    （「小美疑惑地说：」混进了 text——这是旁白腔，TTS 会把它原样念出来）
    每句必须以标点符号收尾。收什么符号由这句话的语义定：例如：疑问收「？」、感叹收
    「！」、陈述收「。」——照句子的语气挑，不许全篇拿「。」应付。

%s【当前任务：生成要求】（逐条满足；本期的验收标准）
1. 每句台词在 %d 到 %d 字之间。
2. 全篇台词合计约 %d 字。
3. 说话人由**内容**定，不是由位置定：一句完整的话（一次回答、一段说明、
   一个例子）由同一个人说完，**不许拆给两个人**；分工只说明各自的主场，
   不要求每句对调。**两人的连句上限按上面【文体依据】给的那两个数**——
   那是上限不是目标，谁都不许超过它。
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
        TITLE_MAX,
        plan_hint,
        vocab_help,
        middle,
        min_c, max_c, int(target_chars),
        banned_block,
        READABLE_RULE,
        style_block,
        hosts,
    )


def build_user_prompt(material, cfg, previous_report=None,
                      previous_lines=None):
    """拼用户提示词。回灌重写时把上一版正文一并带上。

    带正文不是为了好看：反馈点到「第 58 句命中」，模型却看不到第 58 句原本写了
    什么，就只能凭印象通篇重写——重写出来的新句又会带进新的命中，上一轮改掉的
    地方白改，几轮全耗在打地鼠上。给了原稿，才是「只改这一句」。
    """
    parts = []
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


def normalize_script(data, cfg):
    """格式确定性后处理（08b：格式轴硬编码收束）。

    - 字段缺失即补位（speaker 缺省承接上一句、text 去空白、emotion 不在词表
      落中性档「承接」）
    - 空 text 的条目直接丢弃
    - estimated_seconds 由时长模型按标准语速计算，丢弃 LLM 给的任何数字

    emotion 的收束分两层：这里按**全表**收（词表外的值一律落承接，旧稿、
    片头尾模板等没有标签的句子也在这里补齐）；语篇词表（本篇可填哪些词）
    由 emotion_vocab 门禁按 preset 判，不在这里越权。
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
        emotion = str(item.get("emotion") or "").strip()
        if emotion not in DISCOURSE_ORDER:
            emotion = DISCOURSE_NEUTRAL
        text = str(item.get("text", "")).strip()
        text = re.sub(r"\s+", "", text)
        if not text:
            continue
        out.append({
            "speaker": speaker,
            "emotion": emotion,
            "text": text,
            "estimated_seconds": 0,
        })
    for item in out:
        item["estimated_seconds"] = int(round(
            duration_model.estimate_line(item["text"], item["speaker"], cfg)
        ))
    return out


def _form_key(card, cfg):
    """本期用哪种对话形式（配置里选的 > 卡上的默认 `form` > 兜底）的键。

    取值只有这一处：上限（`_run_caps`）、节奏（`rhythm_text`）、站位说明里的
    角色（`_hosts_text`）都从它出发。各写一遍取法，就会出现提示词按一种形式写、
    门禁按另一种判的事。
    """
    chosen = (cfg or {}).get("script.dialogue_form") or ""
    return paradigms.resolve_form(card, chosen)


def _run_caps(card, cfg):
    """本期两人各自连着说的上限，返回 `(A, B)`。

    取值只有这一处：写脚本的提示词、它的门禁、门禁报给模型的上限全走它。形式由
    `_form_key()` 定，上限由 `paradigms.run_caps()` 给——两边各写一遍取法，就会
    出现提示词按一种形式写、门禁按另一种判的事（`max_run` 从前分散在卡上就是这个
    毛病）。
    """
    return paradigms.run_caps(_form_key(card, cfg))


# ------------------------------------------------------------------ 门禁


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


def _audience_clause(cfg):
    """片头里「面向…的听众」那一小段。项目没填受众时整段消失。

    整段消失而不是留个「面向的听众」：半句话比少一句话难看得多（TTS 会字正腔圆
    地把它念出来），而「没填」本来就是合法状态——单集与逐期即兴的项目没有地图，
    也就不产受众。受众口径见 `project_store`：排地图时模型给第一版，界面可改，
    人填过重排不覆盖。
    """
    aud = (cfg.get("project.audience") or "").strip()
    return "，面向%s的听众" % aud if aud else ""


def _names_clause(cfg):
    """「，播讲人甲乙」那一小段。两个称呼都空时整段消失，同上。"""
    names = [x for x in ((cfg.get("tts.name_a") or "").strip(),
                         (cfg.get("tts.name_b") or "").strip()) if x]
    return "，播讲人%s" % "、".join(names) if names else ""


def glue_intro_outro(script, cfg, title="", log=None, review=None):
    """把片头尾按模板粘在正文首尾，返回**新的**句子列表（原稿不动）。

    三件事必须一起说清，否则这个函数会被用错：

    1. **只粘，不改**。正文一句不动、一句不删。从前那版是拿片头句「替换」正文
       第一句、片尾句「替换」正文最后一句，于是每期的正文头尾各丢一句：第 2 句
       还在应答一句已经不存在的开场白（听感上就是「没错」开头），倒数第二句提的
       问题永远等不到回答。粘合只往两头加，正文零损失。
    2. **粘在门禁与修补之后**。生成过程里模型面对的只有正文，门禁、内容检、
       定点修补、字数核账看到的也只有正文。片头尾是固定结构，让格式门禁去掰它
       （比如「同一人连续句数」）只会把固定标识掰歪。所以粘合放在落盘与返回那
       一刻（见 `generate` 与 `_generate_segmented`）。
    3. **没有可失败的判断**。句子是从模板逐字拼出来的，模型没参与，也就没有
       「它照没照做」可验。缺料各退一步：受众缺 → 那一小段消失；期标题缺 →
       「本期讲述…」整句不粘。粘出半句话比少粘一句坏得多。

    `log` 只用来把「哪一句因为缺料没粘」说出来，不影响结果。

    `review` 是**前期回顾**，一组已经填好文本的句子（由 `pipeline.review_rows`
    读上一期的期主旨与段主旨组出来）。位置在**片头之后、正文之前**：它是「上期
    讲到哪儿」的交代，得在正文开始前让听众听到，摆到正文后面就成尾声了。
    这一层只管位置——开关开没开、上一期取不取得到，全是调用方的事，取不到就给
    空，这里当没有。回顾与片头尾同类：模型没参与，也就没有「照没照做」可验。
    """
    log = log or (lambda m: None)
    io = intro_outro_spec(cfg)
    title = str(title or "").strip()
    fill = {"program": program_name_of(cfg),
            "audience_clause": _audience_clause(cfg),
            "names_clause": _names_clause(cfg),
            "title": title}

    def _render(rows, where):
        out = []
        for tpl in rows or []:
            if "{title}" in tpl["text"] and not title:
                log("%s里「本期讲述…」那一句没粘：本期标题是空的" % where)
                continue
            out.append({"speaker": tpl["speaker"],
                        "emotion": tpl["emotion"],
                        "text": tpl["text"].format(**fill)})
        return out

    body = [dict(l) for l in (script or [])]
    head = _render(io.get("intro"), "片头")
    tail = _render(io.get("outro"), "片尾")
    # 前期回顾站在片头之后、正文之前。文本由调用方组好（`pipeline.review_rows`），
    # 这里不填值、不判开关、不做降级——粘合只负责位置。空列表＝这一期没有回顾。
    mid = [dict(r) for r in (review or [])]
    if mid:
        log("前期回顾已粘上：%d 句，位置在片头之后" % len(mid))
    log("片头尾已粘上：正文 %d 句，首 %d 句、尾 %d 句固定结构"
        % (len(body), len(head), len(tail)))
    # 补 estimated_seconds：片头尾与前期回顾都是新加进来的句子，时长模型还没量过
    # 它们。normalize 是幂等的（正文那一遍量过的值一模一样），顺手把整篇统一量一遍——
    # 字幕时间轴与总时长都读这个字段，缺了它那几句就成了 0 秒。
    return normalize_script(head + mid + body + tail, cfg)


def _glued_draft(payload, cfg, log=None, review=None):
    """把一份**整期终稿**包成「正文 + 粘好的片头尾」，**不改原稿**。

    循环里跑的自始至终是**正文**：定点修补按句号改句子、段落配额按字数核账、
    门禁按「同一人连续句数」判——这些看到的都只该是正文。而落盘与返回界面的
    是**成品句子表**，片头尾已经在里面了。所以这里复制一份出去，原 dict 一个字
    不动：下一轮的 `apply_patch(last["script"], ...)` 拿到的仍是正文的句号。

    **只许喂整期终稿，不许喂任何中间产物。** 调用点只有 `generate` 的 `_finish`
    一处（门禁通过、以及轮次用尽的兜底返回，两个出口都从它出去）。分段生成的
    **段间落盘**不在其中：那是「写了一半的正文」，粘上就变成「半期 + 片头 + 片尾」
    ——片尾挂在半篇的末尾，形态像一整期、其实是残缺。中间轮次的落盘同样不给片头尾
    （见 `_finish` 与 `generate` 循环内那处说明）。
    """
    out = dict(payload)
    out["script"] = glue_intro_outro(payload.get("script"), cfg,
                                     title=payload.get("title") or "",
                                     log=log, review=review)
    return out


#: 句尾标点门禁的判据集。**有没有归 py，对不对归语义**：问句收句号这类
# 「符号用得对不对」py 判不了，归提示词引导与回灌文案里带的方向。
END_PUNCT = "。！？….!?"
#: 判末字符前先剥掉的收尾包裹符：整句收在引号/括号上时（「……。」），
#: 标点其实在包裹符里，剥到看见终止标点为止。
END_PUNCT_CLOSERS = "」』\"'“”’）》〉】]"


def end_punct_tail(text):
    """句尾不是终止标点时返回惹事的那个尾字符，合规返回 None。

    判「末字符不是终止标点」一步盖住两种坏形：完全没标点（收在汉字/字母上）、
    拿逗号顿号这类句中标点收尾——不用分两种报法。
    """
    t = str(text or "").strip().rstrip(END_PUNCT_CLOSERS)
    if not t:
        return None
    return None if t[-1] in END_PUNCT else t[-1]


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
               if not s.get("text") or not s.get("speaker")
               or not s.get("emotion")]
    add("fields_complete", not missing,
        "缺字段句子：%s" % missing if missing else "全部齐备")

    # 语篇词表：emotion 只认 DISCOURSE_ORDER 里的八个词（`vocab_words()`）。
    # 枚举层已经拦了绝大多数越界，这里兜的是降级路（无约束解码）的漏网。
    vocab = vocab_words()
    off_vocab = [i + 1 for i, s in enumerate(script)
                 if s.get("emotion") not in vocab]
    add("emotion_vocab", not off_vocab,
        ("词表外标签：%s（本篇可用：%s）"
         % ("、".join("第 %d 句「%s」" % (n, script[n - 1].get("emotion"))
                      for n in off_vocab[:8]),
            "/".join(vocab))) if off_vocab else "全部在词表内",
        lines=off_vocab, vocab=vocab)

    # 同一人连说两句不是错（一段完整的回答本来就该由一个人说完），超上限才是。
    # 判据从「必须交替」换成「连续句数」：前者会把一个人的一段话掰给两个人，
    # 那正是这条门禁原先在逼着模型做的事。
    # 上限**按说话人各一条**：A 与 B 的主场不同（一问一答里 A 只递话），拿一个
    # 数管两个人，等于逼其中一个人抢话。取值与提示词同一处（`_run_caps`）。
    # 超限了**不替它翻**：把这一整段的句号和上限一起报出去，交模型自己并句。
    # 程序翻出来的只是「换个人念」——A 的一句问话落到 B 嘴里，就成了 B 自己问
    # 自己（那个后处理 `enforce_max_run` 已在 v0.34.0 取下）。
    card = paradigm or resolve_paradigm(None, cfg)
    cap_a, cap_b = _run_caps(card, cfg)
    caps = {"A": cap_a, "B": cap_b}
    runs, run_lines = [], []
    start = 0
    for i in range(1, len(script) + 1):
        if i < len(script) and script[i]["speaker"] == script[start]["speaker"]:
            continue
        who = script[start]["speaker"]
        cap = caps.get(who, 1)
        if i - start > cap:
            lines = list(range(start + 1, i + 1))
            runs.append({"speaker": who, "lines": lines, "cap": cap})
            run_lines.extend(lines)
        start = i
    add("ab_run_limit", not runs,
        ("超限段：%s（A 连着说的上限 %d 句、B %d 句）"
         % ("、".join("第 %d–%d 句 %s 连说 %d 句"
                      % (r["lines"][0], r["lines"][-1], r["speaker"],
                         len(r["lines"])) for r in runs),
            cap_a, cap_b)) if runs
        else "A 与 B 都没超过各自的上限（A %d 句、B %d 句）" % (cap_a, cap_b),
        # 明细列全，不跟着人话一起截断：人话是给人扫一眼的，它是给定点修补用的。
        # 只报前几个的话，改完这几处、剩下的还在，下一轮还是不过。
        # **整段都列进去**：并句要把这一段并成一句，只点超出的那几句没法并。
        lines=run_lines, runs=runs)

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

    # 句尾标点：标点会被 TTS 当作停顿与语调的依据，整句缺了它合成出来就跟
    # 下一句连成一片。判据见 end_punct_tail——只判「有没有」，不判「对不对」；
    # 补什么符号由模型按语义定（提示词契约与回灌文案都带方向）。
    bad_punct = []
    for i, s in enumerate(script):
        tail = end_punct_tail(s.get("text"))
        if tail is not None:
            bad_punct.append({"line": i + 1, "tail": tail,
                              "sample": str(s.get("text") or "")[-12:]})
    add("line_end_punct", not bad_punct,
        ("命中 %d 处：%s" % (len(bad_punct),
                          "、".join("第 %d 句以「%s」收尾" % (h["line"], h["tail"])
                                    for h in bad_punct))) if bad_punct
        else "全部以终止标点收尾",
        hits=bad_punct)

    # 这里本来有一条 intro_outro：判首句像不像片头、末句像不像片尾。撤掉了——
    # 片头尾现在由程序在**整期定稿那一刻**逐字粘上去（`glue_intro_outro`），门禁
    # 看到的稿子里根本没有它们，判它只会恒为假，然后把一句模型没写过的句子交给它
    # 去改。粘合是纯字面拼接，没有可失败的实验，也就不需要「验」。

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


def _chars_within(chars, left_tokens, tok_of):
    """按 token 额度折回能喂多少字符（二分：折算比不一定是常数）。"""
    if chars <= 0 or left_tokens <= 0:
        return 0
    lo, hi = 0, int(chars)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tok_of(mid) <= left_tokens:
            lo = mid
        else:
            hi = mid - 1
    return lo


def budget_chars_for_check(llm, cfg, other_chars=0):
    """内容检每批能装多少字符：**输入额度** − 每批必带的那些字。

    两端都折 token 再比（`llm_client.budget_chars` 做这件事）：从前是拿
    「朗读字数」的容量去减「字符数」的占位，两个单位的数相减，减出来的值没有
    意义。没有额度能力的后端退回旧口径，只为让假模型跑得动，不是可选路径。
    """
    if hasattr(llm, "budget_chars"):
        return llm.budget_chars(int(cfg.get("llm.max_tokens", 8192)),
                                other_chars=other_chars)
    return material_capacity(cfg) - int(other_chars or 0)


def fit_material(llm, material, cfg, other_chars=0, evidence=None, log=None):
    """把素材裁到**这一次调用**的输入额度内，超了就明说。返回 (素材文本, 备注)。

    口径只有一个：token。额度 = 最大输出 × `llm.input_ratio`
    （`llm_client.input_budget_tokens`），素材与占位都折成 token 再比——两端
    同单位。从前这里比的是「朗读字数（容量）− 字符数（占位）」，再拿字符数去
    跟它比大小：三个数三种语义，那个比较没有意义，技术文档那一期就是被它判成
    「超出容量」的。

    **这里不回答「这一期该讲多少料」**——那由画地图按压比定死（`probe.capacity`），
    与本次调用无关。这里只回答「这一次装不装得下」；装不下由装箱去切（见
    `pack_segments` / `_refit_pieces`），切到每块都装得下。

    降级次序（只在装箱没覆盖到的边角发生）：
    1. 原文放得下 → 用原文（常态）；
    2. 放不下但有凝缩 → 用凝缩（每节的逻辑骨架）。**这是有损的**，所以必须
       在返回的备注与素材头部把话说出来；
    3. 连凝缩都没有（逐期即兴没排过图）→ 按额度截原文，同样明说。
    """
    material = material or ""
    log = log or (lambda m: None)
    budget_of = getattr(llm, "input_budget_tokens", None)
    tok_of = getattr(llm, "material_tokens", None)
    if budget_of is None or tok_of is None:
        # 没有额度能力的后端（测试里的假模型、以及将来接的非 OpenAI 客户端）：
        # 照原文全喂。裁不了不等于要裁，硬塞一个常数只会把素材白砍掉。
        return material, ""
    out_tokens = int(cfg.get("llm.max_tokens", 8192))
    quota = budget_of(out_tokens)
    left = quota - tok_of(other_chars)
    if left > 0 and tok_of(len(material)) <= left:
        return material, ""
    ev = _evidence_block(evidence)
    if left <= 0:
        note = ("输入额度（= 最大输出 %d × 输入倍率 %.1f）扣掉提示词与已写正文后"
                "放不下素材，请调大 llm.input_ratio"
                "%s。" % (out_tokens, getattr(llm, "input_ratio", 0.0),
                          "，本次已改用凝缩" if ev else ""))
        log(note)
        return ev, note
    if ev:
        note = ("本次素材折合 %d token，超出这次调用的可用额度（额度 %d − "
                "提示词与已写正文 %d = %d token），已改用凝缩（每节逻辑骨架）"
                "作依据——细节可能缺失。"
                % (tok_of(len(material)), quota, tok_of(other_chars), left))
        log(note)
        return ev, note
    keep = _chars_within(len(material), left, tok_of)
    head = ("本份素材为节选：全 %d 字，超出这次调用的输入额度，此处只给了"
            "前 %d 字。**没看到的段落不等于不存在**——某句在本份里找不到依据时，"
            "不要就此判它编造。\n\n" % (len(material), keep))
    note = ("本次素材 %d 字超出这次调用的输入额度，已按额度截断喂入"
            "（依据可能缺失）。" % len(material))
    log(note)
    return head + material[:max(0, keep)], note


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
  呈给你的就是正文本身：片头片尾是固定结构，要到整期定稿那一刻才粘上去，不在这份稿子里，
  所以不必再为「问候语找不到依据」留口子——那一类句子这里根本不会出现。
- promise（承诺链检）：正文开头提出的疑问、设问或承诺，在后文有没有真的回应。判据是
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
    # 每批能装多少 = **这一次调用的输入额度**（最大输出 × 输入倍率）扣掉必带的
    # 那些字。这里不问「这一期该讲多少料」——那是画地图按压比定的事，用的是
    # 朗读字数一把尺；拿它来判「这批装不装得下」就是两把尺混用。
    size = budget_chars_for_check(llm, cfg, other)
    if size <= 0:
        # 模板本身就撑满了额度，连一段素材都放不进去——判不了就明说。
        # 静默把整段塞进去会把后端顶爆，报出来的却是后端一句看不懂的错。
        return _unjudged(
            "本次调用的输入额度（= 最大输出 × 输入倍率）扣掉提示词后放不下素材，"
            "未核", dims)
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

#: 定点补丁的输出契约。**减行数只有一种表达：并句。** 一条 edit 是 `index`
#: （改哪一句）+ `text`（改成什么），带上 `absorb`（并掉紧随其后的几句）时才
#: 允许少行——「同一人连着说超限」就靠它把这一段并进第一句。**拆句、凭空加句，
#: 结构里根本写不出来**；`absorb` 也只许并同一人紧挨着的句子、并完还不许吃掉
#: 内容，由 `apply_patch` 逐条核，核不过整份补丁作废。
#: 说话人不在其中：超限不许靠换人解决——把 A 的一句问话翻给 B，就成了 B 自己
#: 问自己（那个后处理 `enforce_max_run` 已在 v0.34.0 取下）。
PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": _edit_item_schema(DISCOURSE_ORDER),
        },
    },
    "required": ["edits"],
}


def patch_schema(vocab=None):
    """定点补丁的输出 schema：一条 edit 是 index / emotion(可选) / absorb(可选) / text。

    emotion 可选：多数修补只动 text；语篇标签错了（词表外、与句型不符）
    也要有得改。absorb 可选，只给「同一人连着说超限」用——并句是减行数的唯一
    合法表达。`vocab` 为本篇词表（`vocab_words()`），不传也按它开。
    """
    schema = copy.deepcopy(PATCH_SCHEMA)
    schema["properties"]["edits"]["items"] = _edit_item_schema(vocab or DISCOURSE_ORDER)
    return schema


_PATCH_SYSTEM_TMPL = """你是播客脚本的定点修补者，回答只输出 JSON，不要任何其它文字。

输出格式：
{"edits": [{"index": 58, "text": "改好后的整句文本。"}]}
要并句时多带一个 absorb：
{"edits": [{"index": 58, "text": "并好的一整句文本。", "absorb": 2}]}

铁律：
- **只允许替换**被点名句子的内容。不许新增句子，不许凭空删句；除问题清单点名
  「连着说超限」的地方（靠 absorb 并句，见下）以外，全篇行数一个字都不能变。
- 只把**需要改的句子**写进 edits。没被点名的句子一个字都不许动，也不许出现在 edits 里。
- text 必须是替换后的**完整整句**，不是片段；一句就是一句，不许把一句拆成两条
  edit（并句只许出现在被点名「连着说超限」的地方）。text 只装对话内容——语气/
  情绪描写（「小美疑惑地说」这类）、说话人标记、任何标签都不进 text。text 里的
  每个字都会被念出来。每句必须以标点符号收尾，符号由这句话的语义定：疑问收
  「？」、感叹收「！」、陈述收「。」——不许一律补句号应付。
- 被点名句子的语篇标签（emotion）若在问题清单里点名要改，在对应条目里带上
  emotion 键，从本篇词表中选一个；没点名就不带这个键。
- 每句 %d–%d 字。过长的只精简措辞、不许拆句；过短的只就地补内容、不许并句。
  拆句会让后面每一句的句号都挪一位，改稿的人按句号找不到原来那一句。
- 说话人不在你的职责内，不要输出 speaker 这个键。**超限也不许靠换人解决**——
  把 A 说的一句问话翻给 B，就成了 B 自己问自己，那是拿格式代替语义。
- 被点名「连着说超限」的：把那一段**并成上限以内**。写法是把并好的整段放进这一段
  的**第一句**，带上 absorb = 并掉的句数：
    {"index": 5, "text": "并好的一整句。", "absorb": 2}   ← 这一句替掉第 5、6、7 句
  只许并同一人**紧接着**的句子，不许跨过另一个人；并完这一段连着说的句数必须落在
  上限之内。并句只删掉合并处的重复衔接（「对。」这类接话），意思一个都不许少。
- 你改出来的句子**同样要过措辞禁忌**——不许拿一个同样把话说满的词顶上去，
  那只是把同一个毛病换了层壳，门禁照样拦：
%s"""


def patch_system(cfg=None):
    """定点修补的系统提示词。

    句长区间从配置读（`gate.min_chars` / `gate.max_chars`），不写死。写死过
    一次：提示词按 8–40 字改、门禁按人调的 30 字判，模型改完照样被打回，
    再改还是同一段——和插入那一处是同一种毛病（见 `insert_system`）。
    """
    lo = int((cfg or {}).get("gate.min_chars", 8))
    hi = int((cfg or {}).get("gate.max_chars", 40))
    return _PATCH_SYSTEM_TMPL % (lo, hi, format_banned_rules())


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
    """把补丁落回原稿。默认只换字段、行数不变；**并句是唯一的例外**。

    行数不变是这条路的根基：门禁报的「第 N 句」在补丁前后指的是同一行。唯一的例外
    是「同一人连着说超限」——它只能靠并句解决（为什么必须减行，见 `_edit_item_schema`
    的 `absorb`）。所以这里按**补丁前那一版的句号**逐行重建：被 `absorb` 并掉的句号
    整行不出现，`targets` 里的「第 N 句」全程指补丁前那一句，编号不会漂。

    **只许改被点名的句。** 越界一律报错，不静默丢弃：模型顺手改了别的句子，等于把
    没毛病的地方重新摇一次骰子，而这正是这条路要根除的东西；悄悄放行它，下一轮
    若因此冒出新毛病，要跨过整整一个流程才查得到根源。

    并句另有三条硬校验，缺一条就整份补丁作废、退回去整篇重出：被并的每一句都得在
    点名清单里（没点名的说明它自己没毛病）、都得与并进的那句**同一个人**说的
    （跨人并就是把一个人的话塞进另一个人嘴里）、并出来的字不许比原来几句加起来
    少太多（并句只许删掉合并处的重复衔接，不许借并句吃掉内容）。
    """
    total = len(script)
    target_set = set(int(x) for x in targets)
    heads, swallowed, texts = {}, {}, {}
    for e in edits:
        if not isinstance(e, dict):
            raise ScriptError("补丁里有不是对象的条目：%r" % (e,))
        idx = e.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ScriptError("补丁里的句号不是整数：%r" % (idx,))
        if not 1 <= idx <= total:
            raise ScriptError("补丁句号越界：%d（全篇共 %d 句）" % (idx, total))
        if idx in heads or idx in swallowed:
            raise ScriptError("第 %d 句在同一份补丁里出现两次——一条 edit 只管一句，"
                              "它既不能自己改一遍又被别人并走。" % idx)
        if idx not in target_set:
            raise ScriptError("补丁改了没被点名的第 %d 句。定点修补只动被点名的句子，"
                              "其余一律照抄。" % idx)
        text = re.sub(r"\s+", "", str(e.get("text") or "").strip())
        if not text:
            raise ScriptError("补丁把第 %d 句改成了空文本。" % idx)

        raw_absorb = e.get("absorb")
        absorb = 0 if raw_absorb is None else raw_absorb
        if isinstance(absorb, bool) or not isinstance(absorb, int) or absorb < 0:
            raise ScriptError("第 %d 句的 absorb 只许是正整数（并掉紧随其后的几句）："
                              "%r" % (idx, raw_absorb))
        tail = [idx + k for k in range(1, absorb + 1)]
        if tail and tail[-1] > total:
            raise ScriptError("第 %d 句要并掉 %d 句，已经并到篇外（全篇共 %d 句）。"
                              % (idx, absorb, total))
        for k in tail:
            if k in heads or k in swallowed:
                raise ScriptError("第 %d 句被同一份补丁并了两次。" % k)
            if k not in target_set:
                raise ScriptError("第 %d 句要并掉第 %d 句，可问题清单没点名第 %d 句。"
                                  "门禁没点它就说明它自己没毛病——并句只许并"
                                  "被点名的那一段。" % (idx, k, k))
            if script[k - 1]["speaker"] != script[idx - 1]["speaker"]:
                raise ScriptError(
                    "第 %d 句要把第 %d 句并进来，可这两句不是同一个人说的"
                    "（%s / %s）——并句只许并同一人紧接着的句子。"
                    % (idx, k, script[idx - 1]["speaker"],
                       script[k - 1]["speaker"]))
        if tail:
            was = sum(len(re.sub(r"\s+", "", script[x - 1]["text"] or ""))
                      for x in [idx] + tail)
            # 只拦「借并句吃掉内容」：并句本来就要删掉合并处的重复衔接（「对。」
            # 这类接话），所以留出余量；整句整句地丢会被这里拦下。
            if len(text) < was * 0.7:
                raise ScriptError(
                    "第 %d 句并出来的只有 %d 字，原来 %d 句加起来 %d 字——并句只许"
                    "删掉合并处的重复衔接，意思一个都不许少。"
                    % (idx, len(text), len(tail) + 1, was))
        heads[idx] = e
        texts[idx] = text
        for k in tail:
            swallowed[k] = idx

    out = []
    for i, s in enumerate(script, 1):
        if i in swallowed:
            continue
        row = dict(s)
        if i in heads:
            row["text"] = texts[i]
            # emotion 只在模型带了且合法时才落——没带就保留原标签。说话人则一律
            # 不采纳：超限不许靠换人解决（A 的一句问话翻给 B，就成了 B 自己问自己），
            # 片头尾那两句归 `glue_intro_outro`，都不在模型手里。
            if heads[i].get("emotion") in DISCOURSE_ORDER:
                row["emotion"] = heads[i]["emotion"]
        out.append(row)
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


def patch_targets(report, total):
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
        elif key == "line_end_punct":
            # 句尾标点自带句号，同走定点：有没有归 py，补什么符号归这句话的
            # 语义——方向在问题说明里给足，不给模型就一律句号应付。
            for h in it.get("hits") or []:
                targets.setdefault(int(h["line"]), []).append(
                    "句尾没有终止标点（以「%s」收尾）：按这句话的语义补一个合适的"
                    "句尾标点——疑问收？、感叹收！、陈述收。"
                    % h.get("tail", ""))
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
        elif key == "ab_run_limit":
            # 连说超限自带句号，走定点，修法是**并句**不是换人。程序从前在这里直接
            # 翻说话人（v0.34.0 取下的 `enforce_max_run`），翻出来的是「A 问、A 自己
            # 答」——拿格式代替语义。行数不许增、只许减，所以并句是这一段唯一的出路；
            # 方向必须给全：并到几句、怎么写、写在哪一句，否则模型只会把话重说一遍。
            for rn in it.get("runs") or []:
                who = rn.get("speaker")
                cap = int(rn.get("cap") or 1)
                lines = [int(x) for x in rn.get("lines") or []]
                if not lines:
                    continue
                tip = ("第 %d–%d 句都是 %s 说的，连着说了 %d 句，超过上限"
                       "（%s 最多连着说 %d 句）：把这 %d 句**并成 %d 句以内**。写法是把"
                       "并好的整段放进这一段的第一句、带上 absorb，absorb 是并掉的"
                       "句数（至少 %d）：{\"index\": %d, \"text\": \"并好的一整句。\", "
                       "\"absorb\": %d}。**说话人一个字都不许动**——换人是拿格式代替"
                       "语义；并句只删掉合并处的重复衔接，意思一个都不许少。"
                       % (lines[0], lines[-1], who, len(lines), who, cap,
                          len(lines), cap, len(lines) - cap, lines[0],
                          len(lines) - cap))
                for ln in lines:
                    targets.setdefault(ln, []).append(tip)
        elif key in ("json_valid", "fields_complete"):
            refull.append(it)
        else:
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
#   字数配额由程序按**各节凝缩对应的原文量**（loc.chars）占比分（算术归代码，
#   规划第一天起就精确）——不是按摘要自己的字数分，那个既不度量"料有多少"，
#   也不度量"该写多长"；
#   逐段生成后程序数该段实际字数，超差的段**不重写**，改为按字数增删（见下），
#   差额滚入下段配额——误差逐段吸收，不再累积成整篇的倍数漂移。
#
# 段内超容差的修法不是重摇，也**不是改已有句子**。重摇等于把整段扔掉、把同一个
# 确定性函数原样重跑一遍：实测两次输出 1538 / 1524 字（差 0.9%），约束一个零件
# 没变，只白烧二十来分钟。改成按字数增删后，**已写的句子一个字不动**：
#
#   少了 → **新增句子插进去**（`apply_insert`）。补多少字由程序算，插在哪几处、
#          插几句、每句多长，全由模型按素材和上下文定——可能插在两句之间，也可能
#          在本段末尾再接一拍。为什么不让它改长现有句子：单句有 8~40 字门禁，
#          「补长」的天花板 = 句数 ×（上限 − 现有句长），实测缺口 628 字时这个
#          天花板正好被顶满（46 句 × 13.7 字余量 = 629 字），于是每一句都被点名、
#          形式上补丁、效果上等于整段重打；而模型实际只补进 24%。插入没有天花板。
#   多了 → **压紧措辞或整句拿掉**（`apply_trim`）。不带素材：要减的话都在草稿里，
#          递素材进去只会让它把删掉的内容换个说法搬回来。
#
# 改已有句子是**门禁那条路**的事（问题带句号 → `patch_schema` 只许替换）：那是
# 稿子写完、门禁点名之后才该发生的事，跟这里「按字数补删」是两件事，别混。
#
# 账本口径只有字。句数只有两个去处，**都不进核账**：一是提示词里给模型一个软参考
# （`_soft_line_guide`），二是 schema 层的失控刹车（`_lines_hard_cap`）。两者都按
# 本段配额现算，不写死——写死的形态上限会跟「本段该写多少字」脱钩，配额一大就先
# 被刹车截断。走不走这条路由**项目模式**定死，没有开关：成稿规划（mapped）走分段，
# 逐期即兴与单集走整篇。mapped 但各节凝缩不可得（还没排图、凝缩版本对不上）时
# 退回整篇一路，并落日志说明。

SEGMENT_MIN_SENTS = 15      # 软句数下限的兜底默认：正式值走 script.segment_min_sents
SEGMENT_TOL_FRAC = 0.15     # 段字数容差（比例）——与总时长门禁同一把尺
SEGMENT_TOL_MIN_CHARS = 60  # 段字数容差（绝对）：短段免碎核，与比例取大
SEGMENT_RETRIES = 2         # 单段超容差的修正轮上限（少了插入、多了压删，都不是重写）
PLAN_RETRIES = 2            # 规划打回上限，仍不过就按「一节一段」程序硬分
SEGMENT_MAX_COUNT = 30      # 段数上限：防规划轮把稿子切碎成渣
SEGMENT_FIXED_CHARS = 400   # 段提示词固定文案的字符数（算素材额度用，量级即可）


def _segment_window(quota):
    """本段验收区间 (lower, upper)：配额 ± max(15%, 60 字)，与核账同一把尺。

    单一出口：核账判停（调用处 `tol`）与提示词里的【验收窗口】都从这里取数，
    两处永远一致——提示词给模型报的窗口若和程序实际收稿的窗口对不上，
    模型按假窗口收工就会被真窗口打回，白烧调用。
    """
    tol = max(int(quota * SEGMENT_TOL_FRAC), SEGMENT_TOL_MIN_CHARS)
    return int(quota) - tol, int(quota) + tol


def _call_telemetry(meta):
    """一次 LLM 调用的收工观测，折成一行日志。

    回答的问题只有一个：**这一轮是模型自己停的，还是被什么掐断的。**
    `finish_reason` 是判官——stop=自停，length=输出预算/上下文被截断；
    输入输出 token 数来自后端实测回传（不是本地估算）。窗口被钳制的典型
    长相：completion_tokens 每次都停在同一附近且 finish=length，或
    prompt_tokens 远小于提示词实际字数（输入被上下文窗口裁过）。
    """
    usage = meta.get("usage") or {}
    parts = ["finish=%s" % (meta.get("finish_reason") or "unknown"),
             "输入 %d token" % int(usage.get("prompt_tokens", 0) or 0),
             "输出 %d token" % int(usage.get("completion_tokens", 0) or 0),
             "思考 %d token" % int(meta.get("reasoning_tokens", 0) or 0)]
    if meta.get("usage_dropped"):
        parts.append("后端未下发 usage，token 数是空的")
    return "，".join(parts)


# ---------------------------------------------------------------- 取材标记
# 素材里有些内容是**给眼睛看的**：网址、代码、公式、表格、路径、清单……
# 念出来是灾难（"斜杠 用户名 斜杠"），逐条念清单是灾难（浪费配额）。
# 这里在**入口**给模型打标记提醒，出口的台词门禁（READABLE_RULE + 门禁检查）
# 继续兜底——两层咬合：标记防它写进去，门禁防漏网。标记只改**展示文本**
# （素材块前加一行提示），称重、节权重、压比全用原始素材——素材一个字没删，
# 压比从头到尾就一个数。形状正则是机制内置；标题词表与总开关进 config。

_SHAPE_PATTERNS = (
    # 家族名, 编译正则, 命中次数阈值（达到才算命中）
    ("网址", re.compile(r"https?://|\b[\w.-]+\.(?:com|cn|net|org|io|dev|app)\b"
                        r"|[\w.+-]+@[\w-]+\.[\w.]+", re.I), 2),
    ("公式", re.compile(r"\$[^$\n]+\$|\\(?:frac|sum|int|sqrt|alpha|beta|gamma)\b"
                        r"|[∑∫∏±√∞≈≠≤≥]|\b[α-ωΑ-Ω]\b"), 2),
    ("代码", re.compile(r"^\s*```|\b(?:def |import |from .+ import|return"
                        r" |function |const |var |class )", re.M), 3),
    ("表格", re.compile(r"^\s*\|.+\|\s*$|^\s*\|?[-: ]{3,}\|", re.M), 2),
    ("路径", re.compile(r"\b[A-Za-z]:\\|\B/(?:usr|home|etc|var|opt|tmp)/"
                        r"|[├└]──|^│", re.M), 2),
    ("参考文献", re.compile(r"\[\d+\]|doi\.org|@\b(?:article|inproceedings"
                           r"|book|phdthesis)\b", re.I), 3),
    ("标记语言", re.compile(r"</?(?:div|span|br|p|table|tr|td|img|a)\b"), 3),
    ("日志行", re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s"
                         r"|^\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}", re.M), 3),
)

# 家族 → 该怎么用这类内容的指导句（写进【※取材注意】行）。
_SHAPE_GUIDE = {
    "清单": "清单类内容不要逐条念，只取结论性信息（如「支持国内镜像加速」）",
    "网址": "网址、邮箱不进台词，用白话转述它指向什么",
    "公式": "公式不逐符号念，能白话讲清含义就转述，讲不清就跳过",
    "代码": "代码不逐行念，讲这段代码在做什么",
    "表格": "表格不逐格念，用一句话概括它在对比或说明什么",
    "路径": "文件路径、目录树不进台词，只说涉及的目录或文件是什么用途",
    "参考文献": "参考文献编号、DOI 不进台词",
    "标记语言": "排版标签残留不进台词",
    "日志行": "时间戳、日志行不进台词",
}

_SHAPE_TITLE_DEFAULT = "镜像,下载,安装,命令,参数,许可,license,官网,配置,示例"


def _shape_flags(text, cfg, anchor=""):
    """对一节原文跑形状扫描，返回命中家族的中文短名列表（可能为空）。

    判据分两层：**标题词表**（config `script.flag_title_words`，命中节标题
    即算——标题就写着「镜像」的节，正文大概率是清单）和**内容形状**（正则
    命中次数达到阈值——md 有围栏标记，txt/docx 抽出来没有，所以代码、表格
    这些家族都靠形状密度兜，不依赖任何一种标记语法）。总开关
    `script.shape_flags` 关掉就返回空表，一切照旧。
    """
    if not cfg.get("script.shape_flags", True):
        return []
    flags = []
    if not (text or "").strip():
        return flags
    for name, pat, need in _SHAPE_PATTERNS:
        if len(pat.findall(text)) >= need:
            flags.append(name)
    words = [w.strip() for w in
             str(cfg.get("script.flag_title_words", _SHAPE_TITLE_DEFAULT)
                 ).split(",") if w.strip()]
    if anchor and any(w and w.lower() in str(anchor).lower() for w in words):
        flags.append("清单")
    return flags


def _sec_note_text(flags):
    """命中节的【※取材注意】行：只写命中了的家族，指导句一条不少。"""
    if not flags:
        return ""
    names = "、".join(flags)
    guides = "；".join(_SHAPE_GUIDE[f] for f in flags if f in _SHAPE_GUIDE)
    return ("【※取材注意】本节含：%s。%s——这些内容不能直接读出来，"
            "转述含义或跳过，绝不逐字念。" % (names, guides))


def _apply_sec_notes(fit, sec_flags):
    """把命中节的标记行插进**展示用**素材文本；找不到节标签就原样返回。

    只动展示：压比（`_segment_user_prompt` 里对 fit 的 `effective_chars`）、
    装箱称重、节权重吃的都是原始素材——标记行归提示词固定开销，不进素材账。
    节标签行的格式由 `pipeline.section_material_of` 拼出（`【第 N 节…】`），
    这里按同一格式定位，格式对不上就一根都不插——宁可缺标记，不造假位置。
    """
    if not sec_flags or not fit:
        return fit
    lines = fit.split("\n")
    out, seen = [], []
    for ln in lines:
        out.append(ln)
        m = re.match(r"^【第 (\d+) 节", ln)
        if not m:
            continue
        no = int(m.group(1))
        note = _sec_note_text(sec_flags.get(no))
        if note:
            out.append(note)
            seen.append(no)
    if not seen:                                   # 没插进去一根 = 格式对不上
        return fit
    return "\n".join(out)


def _expected_line_chars(cfg):
    """期望句长：门禁句长区间的中点。

    「这一段大约多少句」不能拿句长上限当除数。上限是「一句最多能写多少」，拿它
    推句数等于假设每句都写满；模型按往常的句子长度写出来必然短一截（实测恒定少
    三成半，写的次数越多差得越准）。取区间中点才是它的常态产量。
    """
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    return max(1.0, (min_c + max_c) / 2.0)


def _soft_line_guide(cfg, quota):
    """提示词里给模型的软句数：配额 ÷ 期望句长。

    只作形态参考，**不进核账**（核账看字数）。之所以还要给，是因为 A/B 对话体太
    碎了接缝多、太长了模型自己也难维持形态；给个量级即可，不必精确。
    下限走配置键 `script.segment_min_sents`（默认 15，`SEGMENT_MIN_SENTS` 只是
    配置缺失时的兜底）——它同时也是碎段合并阈值的一因子，两处必须同源。
    """
    min_sents = int((cfg or {}).get("script.segment_min_sents",
                                    SEGMENT_MIN_SENTS))
    return max(min_sents,
               int(round(float(quota) / _expected_line_chars(cfg))))


def _lines_hard_cap(cfg, quota):
    """schema 层的失控刹车：本段**合法**输出的句数上界。

    约束解码下模型能把数组无限写下去，写到上限才被语法强制收尾，所以必须有封顶。
    上限按本段配额现算，不写死：段字数容差是 ±SEGMENT_TOL_FRAC、最短句是
    min_chars，那么任何「还在容差内」的输出都不可能超过
    `配额 × (1+容差) ÷ 最短句` 句。取这个数就永不误伤，而它只管防无限写——
    控长归字数核账，两件事不混。
    """
    min_c = int(cfg.get("gate.min_chars", 8))
    return max(1, int(math.ceil(float(quota) * (1.0 + SEGMENT_TOL_FRAC)
                                / max(1, min_c))))


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


def _hosts_text(card, cfg, form=""):
    """主持人站位与对话形式里的角色。两件事分工不同，别混：

    - **站位**（谁懂谁不懂、谁代表听众）取素材类型卡上的 `cast`；卡上没写就
      退回内置分工口径。
    - **对话形式里的角色**（A 是捧哏还是讲述人）由 `script.dialogue_form` 单拎
      出来。**选了就顶掉卡上那段站位说明**，没选就完全按卡走——形式不是第三个
      来源，是给卡上那一段留的一路覆盖（见 `paradigms.DIALOGUE_FORMS`）。播讲人
      称呼两种情况下都保留：称呼是人的名字，不是分工。

    **节奏不在这里**：怎么接（谁连着说几句、顺序固定不固定）是写作规矩，归
    【文体依据】那一节（`paradigms.run_block` / 分段路的生成要求 / 插入模板）。
    挂在这一节里模型会当人物介绍读，读不出「该怎么写」。
    """
    name_a = cfg.get("tts.name_a", "A")
    name_b = cfg.get("tts.name_b", "B")
    form_txt = paradigms.form_block(form) if form else ""
    if form_txt:
        return "A 称呼「%s」，B 称呼「%s」。\n%s" % (name_a, name_b, form_txt)
    cast = str(card.get("cast") or "").strip()
    if cast:
        return ("A 称呼「%s」，B 称呼「%s」。两人的站位与分工：\n%s"
                % (name_a, name_b, cast))
    return ("- A（%s）：负责抛出问题、提出判断、追问\n"
            "- B（%s）：负责解释、补充、举例、收束" % (name_a, name_b))


# 这里从前还有个 `_gist_chars`（gist + points 的字符数，即「摘要自己写得多长」），
# 用于在两处提示词里展示凝缩的详略。**两处展示都撤了**——逻辑拆分组看内容不看
# 体量、规划轮只写段主旨不看字数，摆着字数只会把模型往"按字数摊匀 / 按字数写"
# 上带。没有消费者，函数一并删除：一个量没有用途就不该留在代码里。

def _material_chars(sec):
    """一节的**原文有效字**：凝缩那一步顺手算出的这一节「念出来是多少字」。

    这才是配额权重。配额问的是"这一段该写多长"，而该写多长由**能念出来的料**
    有多少定——**单位必须与成稿目标同源**：成稿目标（6024 那一类）出自
    `duration_model.chars_for_target`，等于 `时长 × 速度 × STANDARD_K`，而
    `STANDARD_K` 的单位是**有效字/秒**；地图压比（`planner._ratio_issues`）
    也是拿期的有效字比成稿目标。三处同一把尺，配额摊出来才和压比可比。

    不用的两个数，各归各的用途：
    - `loc.chars`（原始字符数，含标题行、排版符号、西文数字）——那是**装箱**
      要折 token 的重量，不是产能；拿它当权重会把"排版符号多"的节当成"料多"；
    - `gist + points`（摘要自己写得多长）——只描述凝缩写得多详略，与料无关。

    缺了它就算不出配额，**报错**，不退回上面两把尺：退回去等于把摊歪的配额
    重新摊一遍，而且从日志上看不出来用的是哪把尺。
    """
    n = int(sec.get("chars") or 0)
    if n <= 0:
        raise ScriptError(
            "第「%s」节没有原文有效字（chars）——配额是按它摊的，"
            "缺了就算不出这一段该写多少字。回地图重跑这一期的凝缩，"
            "或确认素材文件还在。" % (sec.get("anchor") or "未命名"))
    return n


# 这里从前立着两条上限：`SEGMENT_MAX_PARTS`（单节按字符位置切成几块）与
# `SEGMENT_MERGE_MAX`（一段最多合并几节）。两条都撤了，因为**切分单位统一到了
# 整节凝缩**：
#
# - 按字符位置切块整个消失——半个节取不出原文（`source_store.compose` 一次只认
#   一个落点），也算不出账（配额按整节摊，内容却只有半节，两边不同源）。
# - 合并上限由**容量**接管：一段装不装得下由输入额度说了算，不由节数说了算。
#   逻辑分段已经按语义定好了「哪几节是一件事」，再拿一个常数去卡它，就是把
#   语义边界重新交给算术。
#
# 单个节自己就超额度时**报错**（见 `pack_segments`），不许无限往下拆：几千字
# 要调用十几次完全没意义，该做的是调大 `llm.input_ratio` 或回地图拆期。


def _sec_pieces(sec_no, n_chars):
    """把一节的原文位置表达成一块：**整节**。

    **切分单位只有这一个：整节凝缩。** 段与段之间的界线，不管按语义划（逻辑
    分段）还是按容量划（py 桶），都沿着整节的边界走——一节从此不再被从中间
    切开。两个后果：

    - 每一段对应的原文素材能**直接取出来**（`source_store.compose` 收的 refs
      全是整节），不必再按字符区间拼半节；
    - 每段的字数与 token 就是段内各节**直接相加**。原先那套「块配额 = 整节配额
      × 块字符 / 整节字符」的除法连同它的误差一起消失——配额一把尺、内容另一
      把尺，两边不同源时数字必然对不上（§17.3 的根子）。
    """
    n_chars = max(0, int(n_chars or 0))
    return {"sec": sec_no, "part": 1, "parts": 1,
            "start": 0, "end": n_chars, "chars": n_chars}


def _groups_asis(secs, sec_chars, target_chars, groups=None):
    """不装箱时的落法：**按给定分组直接落段**，组内各节保持原序；没分组就一节一段。

    没有输入额度能力（假模型 / 将来别的客户端），或拿不到逐节体量时用它——量不出
    重量就没法判断"装不装得下"，只能照分组原样落。**分组是语义结论，不该因为量不出
    重量就作废**：从前这里一律退成"一节一段"，等于把模型刚分好的组扔掉。确实没有
    分组信息（逻辑拆分也答不出）时才退成一节一段，那是最保守的落法。
    """
    qs = _section_quotas(secs, target_chars)
    if not groups:
        groups = [[k] for k in range(1, len(secs) + 1)]
    out = []
    for lg in groups:
        sec_nos = [k for k in lg if 1 <= k <= len(secs)]
        if not sec_nos:
            continue
        out.append({"sections": sec_nos,
                    "pieces": [_sec_pieces(k, sec_chars[k - 1]
                                           if k - 1 < len(sec_chars) else 0)
                               for k in sec_nos],
                    "quota": sum(qs[k - 1] for k in sec_nos)})
    return _finish_quotas(out, target_chars)


#: 逻辑拆分的重试上限。它只分「哪几节成一段」，产出的东西很少，一次打回
#: 多半是漏节或重排——把问题说清楚再给一次，够了。再不过就退回一节一段
#: （语义上最保守，等价于「不合并」，不会错，只是调用次数多）。
LOGIC_SPLIT_RETRIES = 1

#: 逻辑拆分的输出结构：**只有分组，一节号数组的数组**。
#: 模型不产标题、不产题目、不产任何数字——那些由程序从节号映射出来。
_LOGIC_SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {"type": "array",
                   "items": {"type": "array", "items": {"type": "integer"}}},
    },
    "required": ["groups"],
}


def _logic_split_system():
    return """你是播客脚本的分段者，回答只输出 JSON，不要任何其它文字。

任务：把【节清单】里的节按**内容逻辑**分成若干段——讲的是同一件事的节归到一段，
话题转折的地方断开。一段就是一次写作的单位：段内各节的原文会一起交给写作者，
写成连续的一整段。

铁律：
- **只填节序号。** 不要抄标题，不要写题目，不要写任何别的字段——标题与取材范围
  由程序取用，你多写一份只会多一处可能对不上的地方。
- 每一段是一串**连续的节号**，段内按节号从小到大写；段与段之间按先后排。
- **不许重排。** 节的先后就是播出顺序，由地图定死。后一节的料不许提到前面去写。
- **不许漏节，不许重复。** 从第 1 节到最后一节，每一节都必须出现，且只出现一次。
- 归类**看内容，不看体量**。几节的凝缩字数差多少，与它们是不是"同一件事"无关：
  一节长的和一节短的讲同一件事，就该归到一段里。
- 一段里别放太多节。段越长，写出来的东西越容易跑题（实测过一次写出目标的 289%）；
  确实是一件事就放一起，只有勉强才凑得上的，宁可分开。

输出格式：
{"groups": [[1, 2, 3], [4], [5, 6]]}"""


def _logic_split_user(project, secs):
    """【节清单】只给「节号 + 标题 + 主旨 + 要点」，**一个数字都不给**。

    分组看的是语义：主旨与要点够了。挂上「凝缩 N 字、原文 N 字」不但没用，还有害
    ——提示词里摆着字数，会把模型往"按字数摊匀"上带，而分组从来不看体量（铁律自己
    写着「归类看内容，不看体量」）。`_LOGIC_SPLIT_SCHEMA` 也早写着"模型不产任何
    数字"，user 侧却先递了两个进去，自相矛盾。
    """
    lines = []
    block = _episode_block(project).strip()
    if block:
        lines += [block, ""]
    lines.append("【节清单】（共 %d 节；把**连续的节号**分组成段）" % len(secs))
    for i, s in enumerate(secs, 1):
        lines.append("第 %d 节（%s）" % (i, s.get("anchor") or "未命名"))
        if s.get("gist"):
            lines.append("  主旨：%s" % s["gist"])
        for pt in s.get("points") or []:
            lines.append("  - %s" % pt)
    lines.append("")
    lines.append("【目标】把上面 %d 节按内容逻辑分组成段：讲同一件事的放一段，"
                 "话题转折处断开。只回节序号。" % len(secs))
    return "\n".join(lines)


def _clean_logic_groups(rows, n, warnings=None):
    """把模型给的逻辑分组归一成 1..n 的一个划分，返回 (分组, 是否认出了任何序号)。

    三道归一，都只在结果**可判定**时动手（与 `planner._clean_groups` 同一套
    口径，排图怎么防重排，分段就怎么防）：

    - 越界序号、重复序号：第一次出现有效，其余丢掉并记下；
    - 组内按序号升序、组间按各组首个序号排序；
    - 没被任何一组提到的节：**补成独立一段**并记下。

    最后一条不能省。漏掉的节不是「少讲一点」，是那一节**从此不在任何一段里**，
    而清单看上去完整——取料只看段落的取材范围，不会回头对账有多少节没落上。
    """
    warnings = warnings if warnings is not None else []
    seen, groups, dropped = set(), [], []
    for r in rows or []:
        if isinstance(r, dict):                     # 容忍模型写成 {"units": [...]}
            r = r.get("units") or r.get("sections") or r.get("secs") or []
        if not isinstance(r, (list, tuple)):
            dropped.append("非数组条目")
            continue
        idx = []
        for u in r:
            try:
                k = int(u)
            except (TypeError, ValueError):
                dropped.append("非数字序号 %s" % str(u)[:12])
                continue
            if k < 1 or k > n:
                dropped.append("越界序号 %d" % k)
                continue
            if k in seen:
                continue
            seen.add(k)
            idx.append(k)
        if not idx:
            dropped.append("空组")
            continue
        groups.append(sorted(idx))
    missing = [k for k in range(1, n + 1) if k not in seen]
    for k in missing:
        groups.append([k])
    groups.sort(key=lambda g: g[0])
    flat = [k for g in groups for k in g]
    if flat != sorted(flat):
        warnings.append("分组在节顺序上打了结（各段的取材范围前后交错），"
                        "已按各段首节的先后重排，请核对段与段的边界。")
    if missing:
        warnings.append("有 %d 节没被任何一段提到（%s），已各自补成独立一段。"
                        % (len(missing), "、".join(str(k) for k in missing[:6])))
    if dropped:
        warnings.append("有 %d 处分组标记没通过归一（%s），已按剩余的分组分段。"
                        % (len(dropped), "、".join(dropped[:6])))
    # 第二个返回值是给调用方判「这次到底算不算答出来了」：一个序号都没认出时，
    # 补出来的那一串独立段与「退回一节一段」长得一模一样，可它**不是**一次
    # 合法的回答——该打回重问，而不是当成模型的选择静默收下。
    return groups, bool(seen)


def split_by_logic(secs, llm, cfg, log=None, project=None):
    """逻辑拆分：让模型按**内容**把节分组成段。段边界的第一道来源。

    它只产「哪几节是一件事」，**不产标题、不产题目、不产任何数字**——数字全由
    程序按节号映射出来（段配额 = 组内各节配额之和，段 token = 组内各节之和）。
    模型碰不到数字，也就没有"抄错一个数就整篇对不上账"这件事。

    拿不到模型、或两次都没给出合法分组时，退回**一节一段**：语义上最保守的落法
    （等价于不合并），不会错，代价只是段数多、调用次数多。

    段边界到此**只剩一个来源**。从前让模型在规划轮里顺便分组被明令禁止，理由是
    「两边都去定边界，边界就不存在」——现在装箱不再定边界、降级成容量校验，
    两套边界打架的前提消失了（见 `pack_segments`）。
    """
    log = log or (lambda m: None)
    n = len(secs)
    if n <= 1:
        return [[k] for k in range(1, n + 1)]
    chat = getattr(llm, "chat", None)
    if chat is None:
        log("逻辑拆分：后端没有模型调用能力，退回一节一段")
        return [[k] for k in range(1, n + 1)]
    system = _logic_split_system()
    user = _logic_split_user(project, secs)
    fb = ""
    for attempt in range(LOGIC_SPLIT_RETRIES + 1):
        warnings = []                    # 每次重试各记各的，免得旧问题混进新日志
        u = user if not fb else (
            user + "\n\n【上一版分组的问题】%s\n请重新分组。" % fb)
        log("逻辑拆分：第 %d 次尝试…" % (attempt + 1))
        try:
            raw, _meta = chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": u}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=_LOGIC_SPLIT_SCHEMA)
        except Exception as e:                          # noqa: BLE001
            fb = "调用失败：%s" % e
            log("逻辑拆分打回（第 %d 次）：%s" % (attempt + 1, fb))
            continue
        try:
            data = _extract_json_object(raw)
            if not isinstance(data, dict):
                raise ScriptError("输出不是 JSON 对象。")
        except ScriptError as e:
            fb = str(e)
            log("逻辑拆分打回（第 %d 次）：%s" % (attempt + 1, fb))
            continue
        groups, ok = _clean_logic_groups(data.get("groups"), n, warnings)
        if ok and groups:
            for w in warnings:
                log("逻辑拆分归一：%s" % w)
            return groups
        fb = "没认出一个合法的节序号（groups 缺失、不是数组，或里面没有 1–%d 的号）" % n
        log("逻辑拆分打回（第 %d 次）：%s" % (attempt + 1, fb))
    log("逻辑拆分 %d 次都没给出合法分组，退回一节一段（不合并）"
        % (LOGIC_SPLIT_RETRIES + 1))
    return [[k] for k in range(1, n + 1)]


def _section_quotas(secs, target_chars):
    """把全篇目标字数摊到各节：q_i = target × w_i / Σw。

    **权重 w 是「这一节的原文有效字」（`_material_chars`）。** 该写多长由能念出来
    的料有多少定：料多的地方多写，料少的地方少写。另外两个数不能用——摘要自己的
    字数（gist + points）跟着摘要的详略走，与料无关；原始字符数（`loc.chars`）
    把排版符号与西文数字也算进去，而它们念不成本，是装箱折 token 的重量，不是产能。
    用错尺的后果是同一件事：那一段为了凑配额把自己刚写的话再讲一遍。

    摊到**节**这一层而不是段：段配额 ≡ 段内各节配额之和（摊派是线性的、加法
    可交换），所以先摊到节、再装箱，与旧代码按段摊是同一套算术。余数补给最大
    的一节。装箱前就能算完，不需要估计、不需要迭代。
    """
    n = len(secs)
    if not n:
        return []
    weights = [_material_chars(s) for s in secs]
    total_w = float(sum(weights)) or float(n)
    qs = [int(round(int(target_chars) * w / total_w)) for w in weights]
    qs[qs.index(max(qs))] += int(target_chars) - sum(qs)
    return qs


def _merge_tiny_groups(secs, groups, target_chars, cfg, log=None):
    """碎段合并：配额低于阈值的逻辑段，并进配额较小的相邻段。**纯算术，模型不参与。**

    动机：小节扎堆时会出一批 60~145 有效字的段——一次完整调用写三句话，
    烧时间、多接缝，而且「约 N 句」的软引导在配额过小时自相矛盾（15 句 × 每句
    最少 8 字 = 120 字 > 配额 60 字）。

    阈值 = `script.segment_min_sents` × 期望句长，**全部现算不写死**：软句数
    下限与期望句长是它的两个因子，改配置它就跟着变。段配额 ≥ 阈值时，
    「约 N 句」的引导才与配额自洽——这个阈值正好是引导不说谎的下限。

    位置在**装箱之前**：合并只改「哪几节归同一段」（节顺序不变），装箱按
    容量再切是它的兜底——合并段超容时桶照旧按整节切开，`written` 按实际
    封桶累计的账不受影响。链式合并（145+60=205 仍低于阈值 → 继续并）由
    循环天然覆盖；全篇都碎就并成一段——全篇目标本来就是几千字量级，方向没错。
    """
    log = log or (lambda m: None)
    merged = [list(g) for g in (groups or [])]
    if len(merged) <= 1:
        return merged
    qs = _section_quotas(secs, target_chars)
    min_sents = int((cfg or {}).get("script.segment_min_sents",
                                    SEGMENT_MIN_SENTS))
    thr = min_sents * _expected_line_chars(cfg)

    def weight(idxs):
        return sum(qs[s - 1] for s in idxs if 1 <= s <= len(qs))

    changed = False
    while len(merged) > 1:
        idx = next((i for i, g in enumerate(merged) if weight(g) < thr), None)
        if idx is None:
            break
        if idx == 0:
            j = 1
        elif idx == len(merged) - 1:
            j = idx - 1
        else:
            j = (idx - 1 if weight(merged[idx - 1]) <= weight(merged[idx + 1])
                 else idx + 1)
        lo, hi = min(idx, j), max(idx, j)
        merged[lo] = merged[lo] + merged[hi]   # 相邻升序段拼接，节号仍升序
        del merged[hi]
        changed = True
    if changed:
        log("碎段合并：%d 个逻辑段 → %d 个（阈值 %d 有效字 = 软句数下限 %d "
            "× 期望句长 %.0f；低于阈值的段并入配额较小的邻居）"
            % (len(groups), len(merged), int(thr), min_sents,
               _expected_line_chars(cfg)))
    return merged


def pack_segments(secs, sec_chars, llm, cfg, fixed_chars=0, target_chars=0, log=None,
                  groups=None):
    """按输入额度把**每一个逻辑段**装箱成写作段。**纯算术，模型不参与切分。**

    `groups` 是逻辑拆分给的节号分组（`split_by_logic`）。桶逐个逻辑段过：桶容量
    = 输入额度 − 固定开销 − 已写正文（前面各段的成稿配额），逐段递减——写过的
    正文要一直带着防断链，它涨一截，能留给原文的地方就少一截。

    - 一个逻辑段装得下 → 它**就是**一个写作段（组边界就是段边界，不再合并、不再拆）；
    - 装不下 → **在这个逻辑段内部**按整节贪心切成两个或多个写作段（5 节超容 → 3 节 + 2 节）；
    - 单个节自己就超桶容量 → **报错**。切分单位是整节凝缩，到这一节就切无可切：
      再往下只能切半个节，半个节取不出料、也算不出账。报错要求调大 `llm.input_ratio`
      或回地图拆期——不许无限往下拆（收益极差：几千字要调用十几次）。

    顺序不许变：原文顺序由地图定死（讲述顺序），只能"装得下就装、装不下开新桶"。

    `groups` 为 None 时退成「一节一段」（语义上最保守的落法），装箱逻辑不变。

    返回 groups，每段带：sections（节号）、pieces（取材块，恒为整节）、
    quota（成稿配额，= 段内各节配额之和）。
    """
    log = log or (lambda m: None)
    sec_chars = [int(c or 0) for c in (sec_chars or [])]
    n = len(secs)
    if not n:
        return []
    if not groups:
        groups = [[k] for k in range(1, n + 1)]
    budget_of = getattr(llm, "input_budget_tokens", None)
    tok_of = getattr(llm, "material_tokens", None)
    if budget_of is None or tok_of is None or not any(sec_chars):
        # 没有额度能力的后端、或拿不到逐节体量：不做容量判断，**照逻辑分组直接落段**
        # （没有分组信息才退成一节一段）。没有重量就没法判断"装不装得下"，但分组
        # 是语义结论——把量不出重量当成"分组作废"的理由，等于白分一次。
        return _groups_asis(secs, sec_chars, target_chars, groups)

    quotas = _section_quotas(secs, target_chars)
    max_tokens = int(cfg.get("llm.max_tokens", 8192))
    budget = budget_of(max_tokens)
    fixed = tok_of(int(fixed_chars or 0))
    if budget - fixed <= 0:
        raise ScriptError(
            "输入额度 %d token（= 最大输出 %d × 输入倍率 %.1f）扣掉提示词后放不下"
            "任何素材。请调大 llm.input_ratio，"
            "或减小 script.target_minutes。"
            % (budget, max_tokens, getattr(llm, "input_ratio", 0.0)))

    tks = [tok_of(sec_chars[i] if i < len(sec_chars) else 0) for i in range(n)]

    def row_of(sec_nos):
        return {"sections": list(sec_nos),
                "pieces": [_sec_pieces(s, sec_chars[s - 1]
                                       if s - 1 < len(sec_chars) else 0)
                           for s in sec_nos],
                "quota": sum(quotas[s - 1] for s in sec_nos)}

    rows, written = [], 0
    for lg in groups:
        cur, cur_tok = [], 0
        for sec_no in lg:
            tok = tks[sec_no - 1]
            while True:
                avail = budget - fixed - written
                if avail <= 0:
                    raise ScriptError(
                        "输入额度已被已写正文吃光（额度 %d token，已写正文折合 %d "
                        "token）。已写正文是硬开销——请调大 llm.input_ratio，"
                        "或减小 script.target_minutes / 拆期。" % (budget, written))
                if not cur:
                    if tok <= avail:
                        cur, cur_tok = [sec_no], tok
                        break
                    # 桶是空的还装不下 → 这一节自身就超额度。切分单位是整节，
                    # 到这就切无可切，报错（不再按字符位置切块）。
                    raise ScriptError(
                        "第 %d 节一节就超过单次输入额度（折合 %d token > 可用 %d "
                        "token）。段是按整节凝缩切的，到这一节就切无可切了——"
                        "再切只能切半个节，半个节取不出原文、也算不出账。"
                        "请调大 llm.input_ratio，或回地图把这一期拆成两期。"
                        % (sec_no, tok, avail))
                if cur_tok + tok <= avail:
                    cur.append(sec_no)
                    cur_tok += tok
                    break
                # 装不下：先封桶——这一桶写出来的正文要一直带着防断链，
                # 计入「已写正文」，后面各段的可用额度跟着变小。
                written += tok_of(sum(quotas[s - 1] for s in cur))
                rows.append(row_of(cur))
                cur, cur_tok = [], 0
        if cur:
            written += tok_of(sum(quotas[s - 1] for s in cur))
            rows.append(row_of(cur))
    return _finish_quotas(rows, target_chars)


def _finish_quotas(groups, target_chars):
    """段层补余数与下限：配额合计精确等于目标，且每段不低于最小可写段。"""
    if not groups:
        return groups
    diff = int(target_chars) - sum(g["quota"] for g in groups)
    k = max(range(len(groups)), key=lambda i: groups[i]["quota"])
    groups[k]["quota"] += diff
    for g in groups:
        g["quota"] = max(SEGMENT_TOL_MIN_CHARS, int(g["quota"]))
    return groups


def _segment_plan_schema(n_groups):
    """规划轮的输出结构：**只有标题与每段一句段主旨**。

    段边界已经定死了（逻辑拆分按内容分组，装箱按容量切）：让模型在规划轮里再
    分组，它分出来的就是第二套段划分，与前面那套打架——两边都去定边界，边界就
    不存在。数组长度用 minItems = maxItems = 段数钉死，模型没有增删的余地。
    """
    n = max(1, int(n_groups or 1))
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "topics": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": {"type": "string"},
            },
        },
        "required": ["title", "topics"],
    }


def _sec_nos_text(nums):
    """把一串连续节号压成「1–3」这样的短记（日志用，省得一行几十个数字）。"""
    ns = sorted(int(k) for k in (nums or []))
    if not ns:
        return ""
    runs, start, prev = [], ns[0], ns[0]
    for k in ns[1:]:
        if k == prev + 1:
            prev = k
            continue
        runs.append((start, prev))
        start = prev = k
    runs.append((start, prev))
    return "、".join(str(a) if a == b else "%d–%d" % (a, b) for a, b in runs)


def _piece_desc(group, secs):
    """把一段的取材范围写成一句话（第几节）。

    范围**由程序给死**：模型手里只有这些原文，段主旨若写成"讲第 3 节全部内容"，
    它就会去写自己没拿到的部分。所以范围描述归 py，段主旨只写"讲什么"。

    切分单位是整节凝缩，块恒为整节，不再有"第 N/M 块"这种说法。
    """
    out = []
    for pc in group.get("pieces") or []:
        i = pc.get("sec")
        name = ""
        if isinstance(i, int) and 1 <= i <= len(secs):
            name = str(secs[i - 1].get("anchor") or "")
        out.append("第 %s 节%s" % (i, ("《%s》" % name) if name else ""))
    return " + ".join(out) if out else "（未标注）"

def segment_schema(quota=None, cfg=None, vocab=None):
    """单段输出的结构 schema：只有 lines，一句是 speaker / emotion / text 三字段。

    lines 的 maxItems 是失控刹车：约束解码下模型能把数组无限写下去，写到上限
    语法强制收尾。刹车按本段配额现算（见 `_lines_hard_cap`），不写死——写死的
    形态上限会跟「本段该写多少字」脱钩，配额一大就先被刹车截断。

    `vocab` 为本篇语篇词表（与 `_parse_segment_lines` 的校验同源）。
    """
    schema = {
        "type": "object",
        "properties": {"lines": {"type": "array",
                                 "items": _line_item_schema(vocab or DISCOURSE_ORDER)}},
        "required": ["lines"],
    }
    if not quota or quota <= 0:
        # 调用方没给配额（如只校验结构）：按「一个最小可写段」开刹车兜底。
        quota = SEGMENT_TOL_MIN_CHARS * 10
    schema["properties"]["lines"]["maxItems"] = _lines_hard_cap(cfg or {}, quota)
    return schema


def _segment_plan_system(n_groups):
    return """你是播客脚本的规划者，回答只输出 JSON，不要任何其它文字。

任务：**段落边界已经定好了**（下面列出每一段覆盖哪些内容）。你只做两件事：
给全篇起一个标题，给每一段写一句**段主旨**（与期主旨同层级——这一段讲的是
什么事，不是给它起个名字）。**不许增删段，不许改动段的划分。**

铁律：
- topics 必须正好 %d 条，第 k 条对应第 k 段，顺序不许变。
- 段主旨只写「这一段讲什么」。不要写"从哪里起、到哪里止"——取材范围由程序
  给死，你写了也是两套说法互相打架。
- 只能用列出的凝缩要点，不得发明、引申任何新内容。

输出格式：
{"title": "本期标题", "topics": ["第一段的段主旨", "第二段的段主旨"]}
- title：%d 字以内，概括本期主旨。不要书名号、引号、期号，不要写成完整句子。
""" % (int(n_groups), TITLE_MAX)


def _segment_plan_user(project, secs, groups):
    """【段清单】只给「段号 + 取材范围 + 各节主旨要点」，**一个数字都不给**。

    规划轮只产「期标题 + 各段段主旨」，它不看字数也不需要看：段边界是程序定的，
    配额是程序摊的，字数在这里摆着只会把"这一段该讲什么"往"这一段该多长"上带。
    """
    lines = []
    block = _episode_block(project).strip()
    if block:
        lines += [block, ""]
    lines.append("【段清单】（边界已定，共 %d 段；按顺序为每段写一句段主旨）"
                 % len(groups))
    for gi, g in enumerate(groups, 1):
        lines.append("第 %d 段（取材：%s）" % (gi, _piece_desc(g, secs)))
        for i in g.get("sections") or []:
            if not (isinstance(i, int) and 1 <= i <= len(secs)):
                continue
            s = secs[i - 1]
            lines.append("  第 %d 节（%s）" % (i, s.get("anchor") or "未命名"))
            if s.get("gist"):
                lines.append("    主旨：%s" % s["gist"])
            for pt in s.get("points") or []:
                lines.append("    - %s" % pt)
    lines.append("")
    lines.append("【目标】为上面 %d 段各写一句段主旨，并给全篇起标题。"
                 % len(groups))
    return "\n".join(lines)


def _validate_plan(data, n_groups):
    """校验规划：只认「标题 + 每段一句段主旨」，条数必须与段数一致。

    段边界归程序，模型碰不到 sections 了——从前"同一节进了两个段就打回"那条
    校验随之失效：切分单位是整节凝缩，一节只可能落在一段里，那条规则本身就是
    旧分工的残留。
    """
    topics = (data or {}).get("topics")
    if not isinstance(topics, list):
        return None, "topics 缺失或不是数组"
    if len(topics) != int(n_groups):
        return None, ("topics 有 %d 条，段数是 %d：段边界由程序定死，"
                      "只能逐段写段主旨，不能增删段" % (len(topics), int(n_groups)))
    out = []
    for k, t in enumerate(topics, 1):
        s = str(t or "").strip()
        if not s:
            return None, "第 %d 段的段主旨是空的" % k
        out.append(s)
    return out, None

def _segment_system_prompt(cfg, card, preset_key, index, total, quota,
                           topic, is_first, feedback=None):
    preset = PRESET_SPEC.get(preset_key) or PRESET_SPEC["argument"]
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    chosen_form = cfg.get("script.dialogue_form") or ""
    cap_a, cap_b = _run_caps(card, cfg)
    # 分段路不拼【文体依据】段：它的模板里没有这一块，角色由【两位主持人】段给
    # （`_hosts_text` 里的 form_block），**节奏与连句上限直接写在生成要求里**。
    # 节奏那一句不能省：只给上限就是只划红线——上限里推不出「该连着说几句」，
    # 模型会拿自己的默认节奏（一问一答）来填，形式名写着「主讲＋捧哏」也没用。
    rhythm = paradigms.rhythm_text(_form_key(card, cfg))
    # 形状示范与节奏同一处取（`example_block`）：只描述不示范，模型照样
    # 一句一换——它得先看见一段。缩进跟着生成要求第 2 条走。
    example_txt = paradigms.example_block(_form_key(card, cfg), indent="   ")
    line_guide = _soft_line_guide(cfg, quota)
    style_block = _style_block(preset)
    # 语篇词表只有一份（`vocab_words()`），与 segment_schema 的枚举取自同一处——
    # 提示词列的词和枚举里可填的词永远一致。拼法同插入那一轮。
    vocab_help = _vocab_block()
    opening = ("本段是全篇的第一段：直接进入正题。"
               if is_first else
               "本段是正文中段：直接接住上文继续讲。")
    head_feedback = ("\n\n【上一版本段的问题】%s" % feedback if feedback else "")
    return """你负责把素材改写成两位主持人的对话脚本。本篇采用**分段写作**：你只写其中一段。

【本段任务】（硬性契约）
- 这是全篇正文的第 %d 段，共 %d 段。全篇正文长度由各段配额合计构成；**本段配额约 %d 字**（每句 %d~%d 字，约 %d 句——句数只是量级参考，**一切以字数为准**）。
- 本段段主旨：%s
- %s
- 只输出本段的句子：不要输出标题；不要问候（片头）或收尾（片尾）——它们由程序按模板补齐；不要「下面我们」「以上就是」这类过渡腔。
%s
【输出格式】（硬性契约，逐条满足）
只输出一个 JSON 对象，不要任何解释文字，不要代码块标记：
{"lines": [{"speaker": "A", "emotion": "追问", "text": "台词正文，只写要念出来的话。"}]}
- lines：正文数组，元素固定三个字段。**每个字段只装它自己的东西，互不串场**：
  - speaker：只写说话人，填一个大写字母 "A" 或 "B"。人名、称呼、其他字母一律不进。
  - emotion：只填语篇标签，标这句在对话里**干什么活**，每句必须从本篇词表中选一个
    （词表只有这一份，照抄下面列出的词，不在此列的一律不许编）：
%s
    正确：{"speaker": "A", "emotion": "追问", "text": "含碳12%%的铁属于钢，为什么这么说呢？"}
    正确：{"speaker": "A", "emotion": "承接", "text": "大明于1368年立朝。"}
    正确：{"speaker": "A", "emotion": "强调", "text": "这是胜利的预言家在叫喊：——让暴风雨来得更猛烈些吧！"}
    错误：{"speaker": "A", "emotion": "疑惑", "text": "疑惑含碳12%%的铁属于钢，为什么这么说呢"}
    （这一条错了三处：「疑惑」是心情不是语篇动作、不在本篇词表里；「疑惑」二字
    误进了 text 正文；句尾没有标点符号）
  - text：只填对话内容，即**会被逐字合成语音念出来的台词正文**。text 里的每个
    字都会被念出来——所以下面这些一律不进 text：语气/情绪描写（「小美疑惑地
    说」「他笑着说」）、说话人标记（「A:」「B:」）、任何形式的标签、注释或说明。
    错误：{"speaker": "A", "emotion": "追问", "text": "小美疑惑地说：为什么这么说呢？"}
    （「小美疑惑地说：」混进了 text——这是旁白腔，TTS 会把它原样念出来）
    每句必须以标点符号收尾。收什么符号由这句话的语义定：例如：疑问收「？」、感叹收
    「！」、陈述收「。」——照句子的语气挑，不许全篇拿「。」应付。
【生成要求】（逐条满足）
1. 每句台词在 %d 到 %d 字之间。
2. 说话人由**内容**定，不由位置定：一句完整的话由同一个人说完，不许拆给两个人。两人怎么接：%s 连着说的上限：A 最多 %d 句、B 最多 %d 句（上限不是目标，谁都不许超）。
%s
3. 台词里的每个论断都要能对应到素材内容，不添加素材之外的事实、数据或来源。
4. 措辞禁忌（写完逐句回查一遍，命中就当场改掉）：%s
5. **本段自己内部，同一件事只说一遍**（这一条管「本段前后自我重复」，与「不重复已写正文」是两件事，两条都要守）。
   说过的观点、举过的例子、算过的账，在本段更靠后的位置**换一套词再说一遍，照样算重复**。
   【这些算重复】同一件事换个说法再说：
     ✗ 前文：AI 把需要出力气的工作接管了。／后文：那些拼体力的岗位现在都交给机器了。
       → 重复。只是换了词，听众听到的是同一件事讲了两遍。
     ✗ 前文：人要往需要判断力的地方走。／后文：人能站住的位置只剩下得下判断的那种活。
       → 重复。同一条结论说了两遍。
     ✗ 前文用某个例子说明「门槛卡在数据上」，后文换个包装又讲一遍「卡在数据上」。
       → 重复。例子换了外壳，讲的还是同一件事。
   【这些不算重复】同一件事往下挖一层，或换到另一个侧面：
     ✓ 前文：AI 把需要出力气的工作接管了。／后文：但它接不了得跟病人当面交代病情的那种活。
       → 不重复。前面说它能做什么，后面补它做不了什么，听众听到了新东西。
     ✓ 前文：人要往需要判断力的地方走。／后文：这条路的前提，是有人肯为判断力付钱。
       → 不重复。后面是前面那条结论成立的条件，往下推了一层。
     ✓ 前文：门槛卡在数据上。／后文：具体到医疗场景，卡的是标注过的病例，公开数据集里几乎没有。
       → 不重复。落到了具体场景与具体对象，补的是新细节。
   一句话判据：把本段前后两句并排放进听众耳朵里听一遍——听到「同一件事又说了一遍」就是重复，听到「新东西」就不是。**换个说法不改变这个判断。**
   写到后面发现没新东西可写时，不要换个说法把前面重讲一遍，改为**往下挖一层**（补条件、补代价、补反例、补具体场景）；实在挖不出来，就换成素材里还没用到的点——**段内复读不允许**。字数够不够由程序核账，不归你判断，你只管不复读。
6. %s

【风格倾向】（全篇通用背景，与本期具体内容无关；与「本段任务」冲突时以本段任务为准）
%s

【两位主持人】（既定阵容，不由你指定）
%s
""" % (index, total, quota, min_c, max_c, line_guide, topic, opening, head_feedback,
       vocab_help,
       min_c, max_c, rhythm, cap_a, cap_b, example_txt,
       format_banned_rules(), READABLE_RULE,
       style_block,
       _hosts_text(card, cfg, chosen_form))


def _segment_user_prompt(project, evidence, group, quota, written_chars,
                         target_chars, fit, prev_text, feedback,
                         pieces=None, noted_fit=None):
    """noted_fit：插好【※取材注意】行的展示版素材；缺省回落 fit。

    压比（【本段用量】）**永远吃 fit 原版**——标记行是 py 写的指令，不是
    素材内容，混进去压比就虚高。两版分工见 `_apply_sec_notes`。
    """
    secs = evidence.get("sections") or []
    lines = []
    block = _episode_block(project).strip()
    if block:
        lines += [block, ""]
    lines.append("【本段覆盖的凝缩要点】（只用**本段素材**把这些点讲出来：已写正文里"
                 "讲过的不要重复，补还没讲到的那些；这是下限不是上限——要展开、"
                 "要讲透，把本段配额写满，不要点到为止）")
    for i in group["sections"]:
        if not (isinstance(i, int) and 1 <= i <= len(secs)):
            continue
        s = secs[i - 1]
        head = "第 %d 节（%s）" % (i, s.get("anchor") or "未命名")
        if s.get("gist"):
            lines.append("%s：%s" % (head, s["gist"]))
        else:
            lines.append(head)
        for p in s.get("points") or []:
            lines.append("  - %s" % p)
    lines.append("")
    if pieces:
        # 取材范围由程序给死：这一段只喂了这些原文。切开的那一节，前后两块各自
        # 只有半边原文——范围之外的细节写不出来，写就成了编造。
        lines.append("【本段取材范围】%s（本段只喂了这些原文；范围之外的细节不要写）"
                     % _piece_desc({"pieces": pieces}, secs))
        lines.append("")
    lines.append("【进度】全篇正文目标约 %d 字，已写 %d 字，本段配额约 %d 字。"
                 % (int(target_chars), int(written_chars), int(quota)))
    # 【本段用量】：素材有效字 ÷ 配额有效字，py 对实际喂入的 fit 现算，两边同尺。
    # 它给模型的是「素材相对目标是多是少」的尺度感——决定选料还是展开，方向写死
    # 在句子里，不让模型自己猜。锚必须真实：素材没喂或量不出来就不给这一行，
    # 假锚比没锚更坏。只报事实与任务方向，不上「必须写满」式的口号——压不压得满
    # 由核账与补写轮闭环兜底，提示词喊口号只会诱发为凑数复读。
    mat_chars = (int(round(duration_model.effective_chars(fit)))
                 if fit and fit.strip() else 0)
    low, high = _segment_window(quota)
    if mat_chars > 0 and quota > 0:
        ratio = mat_chars / float(quota)
        if ratio >= 1.0:
            task = ("你的任务是从素材选料铺满配额，细节取用密度要够；"
                    "写薄了就是浪费素材，不存在「素材用完」。")
        else:
            task = ("你的任务是靠追问与展开（补条件、补代价、补反例、补具体场景）"
                    "把素材讲透讲满，不编造素材之外的事实。")
        lines.append("【本段用量】素材约 %d 字，本段配额约 %d 字——素材约为配额的 %.1f 倍。%s"
                     % (mat_chars, int(quota), ratio, task))
    lines.append("【验收窗口】程序按 %d~%d 字收稿，超出会被打回继续补写或压紧。"
                 % (low, high))
    lines.append("")
    lines.append("【本期素材】")
    lines.append(noted_fit or fit or "（素材未喂入，按凝缩要点写）")
    lines.append("")
    if prev_text:
        lines += ["【已写正文】（只供两件事：接住语气、不重复已写论断；禁止续写它）",
                  prev_text, ""]
    if feedback:
        lines += ["【上一版的问题】%s" % feedback, ""]
    return "\n".join(lines)



# ------------------------------------------------- 段内按字数增删（非改句）
# 段生成阶段只管一件事：本段字数对不对得上配额。对不上时就两条路：
#
#   少了 → **新增句子插进去**（`apply_insert`），已有句子一个字不动；
#   多了 → **压紧措辞或整句拿掉**（`apply_trim`）。
#
# 这两件事与「定点修补」不是一回事，别混。那条路（`patch_schema`）是门禁点名之后
# 改已有句子——**替换，行数不变**。把两者混起来会得到最糟的组合：让补字数去改现有
# 句子，于是每一句都被点名，形式上补丁、效果上等于整段重打；而单句 8~40 字的门禁
# 又把「补长」锁死在句数 ×（上限 − 现有句长）这个天花板上——实测缺口 628 字时，
# 46 句 × 13.7 字余量 = 629 字，天花板刚好被顶满，模型实际只补进 24%。插入没有
# 天花板：想补多少字就插多少句。


def _numbered_rows(seg_lines, offset):
    """本段正文按全篇句号编号列出，供段内增删这两条路使用。

    编号用全篇口径（段内第 k 句 = offset + k，`offset` 是本段之前已有的句数），
    与门禁报的句号是同一套：同一份稿子里只能有一套句号，两套并行早晚对不上。
    每行带语篇标签：插与压都得让模型看见「这句是追问、话头递出去了」这类对话
    结构——只给 speaker 和 text，接缝对它是隐形的。
    """
    return "\n".join("%d. [%s·%s] %s" % (offset + k, ln["speaker"],
                                         ln.get("emotion", ""), ln["text"])
                     for k, ln in enumerate(seg_lines, 1))


def _insert_hard_cap(cfg, need):
    """插入条数的失控刹车：这次要补的字数，最多能由多少句凑出来。

    约束解码下模型能把数组无限写下去，写到上限才被语法强制收尾，所以必须有封顶。
    上限取 `要补的字数 × (1+容差) ÷ 最短句`：任何「还在容差内」的补法都不可能超过
    这么多条，取它永不误伤；它只管防无限写，控长归字数核账，两件事不混。
    """
    min_c = int((cfg or {}).get("gate.min_chars", 8))
    return max(1, int(math.ceil(abs(int(need)) * (1.0 + SEGMENT_TOL_FRAC)
                                / max(1, min_c))))


#: 段内补字数的输出契约。与 `PATCH_SCHEMA` 恰好互补——那个只有「替换」，这个只有
#: 「新增」：`after`（插在第几句之后）、`speaker`、`text`。结构里没有 index，所以
#: 「不许改已有句子」不靠提示词劝，靠这里根本写不出来。
#:
#: speaker 在这一端必须由模型给（`PATCH_SCHEMA` 那边恰好相反，明确不许它碰）：
#: 往一段对话里插一句，插在谁后面、该谁开口，是局部语境的事。门禁那条路上不动
#: 结构——那时稿子已经成型、动的是措辞；连说超限也在那条路上解决，而且**不靠
#: 换人**：换人是拿格式代替语义，靠并句（见 `_edit_item_schema` 的 `absorb`）。
SEGMENT_INSERT_SCHEMA = {
    "type": "object",
    "properties": {
        "inserts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "after": {"type": "integer"},
                    "speaker": {"type": "string"},
                    "emotion": {"type": "string", "enum": list(DISCOURSE_ORDER)},
                    "text": {"type": "string"},
                },
                "required": ["after", "speaker", "emotion", "text"],
            },
        },
    },
    "required": ["inserts"],
}


def insert_schema(need=0, cfg=None, vocab=None):
    """插入的输出 schema：条数按本次要补的字数现算。

    新句必须带语篇标签（必填枚举）：补进来的句子如果不参与「逐句选动作」
    的硬信号，问句率会在补写轮被稀释回去——2b 期的教训。
    """
    schema = copy.deepcopy(SEGMENT_INSERT_SCHEMA)
    schema["properties"]["inserts"]["items"]["properties"]["emotion"] = {
        "type": "string", "enum": list(vocab or DISCOURSE_ORDER)}
    schema["properties"]["inserts"]["maxItems"] = _insert_hard_cap(cfg, need)
    return schema


#: 段内压字数的输出契约：**只许改和删**。`drops` 是这里比 `PATCH_SCHEMA` 多出来
#: 的一条路——整句拿掉。门禁那条路不许删（全篇按句号定位，删一句后面全漂），分段
#: 这条是本段内收尾，删几句只影响本段；而「话多说了」最干净的修法就是把多余那句
#: 整个拿掉，而不是把它压缩成一句别扭的话。
SEGMENT_TRIM_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": _edit_item_schema(DISCOURSE_ORDER),
        },
        "drops": {"type": "array", "items": {"type": "integer"}},
    },
}


def trim_schema(n_lines=0, vocab=None):
    """压字数的输出 schema：改 / 删条数按本段句数封顶。

    edit 的 emotion 可选（同 patch_schema）：压紧措辞多数不动标签，
    但标签错了要有得改。
    """
    schema = copy.deepcopy(SEGMENT_TRIM_SCHEMA)
    schema["properties"]["edits"]["items"] = _edit_item_schema(vocab or DISCOURSE_ORDER)
    cap = max(1, int(n_lines))
    schema["properties"]["edits"]["maxItems"] = cap
    schema["properties"]["drops"]["maxItems"] = cap
    return schema


_INSERT_SYSTEM_TMPL = """你是播客脚本的补写者，回答只输出 JSON，不要任何其它文字。

任务：本段的字数不够，你要**新增**对话句子把它补够。**已经写好的句子一个字都不许改**。

输出格式：
{"inserts": [{"after": 58, "speaker": "A", "emotion": "解释", "text": "新增的整句文本。"}]}

铁律：
- **只许新增**。不许修改已有句子，不许删除已有句子。
- after：新句插在第几句**之后**，填正文里给出的那个句号。插在本段最前面，就填本段
  第一句的**前一句**句号；插在本段末尾，就填本段最后一句的句号。同一个位置要连插
  几句，把这几个条目按你想要的先后顺序依次排在一起。
- speaker：只能填 A 或 B。看着插入位置的上下文定——这句话夹在谁的话后面、该由
  谁来说，只有你看得见。
  **两人怎么接：%s**
  **连着说的上限：A 最多连着说 %d 句、B 最多连着说 %d 句**（上限不是目标，谁都不许超过它）。
  %s
- emotion：只填语篇标签，标这句在对话里干什么活，每句必填、**从本篇词表中选**
  （词表只有这一份，照抄下面列出的词，不在此列的一律不许编）：
%s
- text：新句的完整文本，%d–%d 字。text 只装对话内容——语气/情绪描写（「小美
  疑惑地说」这类）、说话人标记、任何标签都不进 text，text 里的每个字都会被念出来。
  每句必须以标点符号收尾，符号按语义选：疑问收「？」、感叹收「！」、陈述收「。」。
- 新增的每一句都要能在【素材】里找到依据。**不许编造**素材之外的事实、数字、结论。
- 插进去之后读起来必须是一段连贯的对话：接得上前面那句，也接得住后面那句。
  补的是话，不是注解——别写成「此外还需说明」这类书面插入语。
- **不许把已有的话换个说法再说一遍。** 判据看**意思**，不看措辞——换个词、换个
  句式、换个角度去问或去答，只要说的是同一件事，就是重复。照下面的例子判：

  【这些算重复】
  ✗ 换个词问同一件事
    上文已写：增强功能全开启时，系统性能开销会不会显著增加？
    你新写：增强功能全开时，性能开销会不会突破可接受范围？
    → 重复。问的都是「性能开销」这一件事，只是把「显著增加」换成了「突破可接受
      范围」——听众听到的是同一个问题问了两遍。
  ✗ 换个词答同一件事
    上文已写：本地部署让调用频次大幅下降。
    你新写：本地部署之后，模型调用次数会明显减少。
    → 重复。「调用频次下降」和「调用次数减少」是同一句话。
  ✗ 前一句还没答，后一句就替它下结论
    上文已写：面对开放式探索任务，这套方法还适用吗？
    你新写：开放式探索任务不适合这套方法，瓶颈在哪个环节？
    → 重复，而且更糟：前一句问的事还没有人回答，这一句已经替它定了性。

  【这些不算重复】
  ✓ 背景一样，问的是另一件事
    上文已写：增强功能全开启时，系统性能开销会不会显著增加？
    你新写：增强功能全开启时，日志体积会涨多少？
    → 不重复。背景相同不算重复——问的对象不同（性能开销 vs 日志体积）。
  ✓ 同一件事，补上了新的细节
    上文已写：开启之后响应会变慢。
    你新写：变慢集中在启动阶段，大约要多等两秒。
    → 不重复。加了位置和数字，听众听到了新东西。
  ✓ 同一个点，把另一条路补上
    上文已写：不勾选就直通执行。
    你新写：勾选了才走校验流程，多花的是一次判断的开销。
    → 不重复。补的是另一条分支的情况。

  一句话判据：把两句并排放进听众耳朵里听一遍——听到「同一件事又说了一遍」就是
  重复，听到「新东西」就不是。**换个说法不改变这个判断。**
- **每一句都得带进正文里还没有的东西**：新事实、新数字、新步骤、新角度都算。
  【素材】里有、而正文还没讲到的，优先补那些。一个信息点写成一句就够，最多写成
  一组一问一答（**一组，不是两组、也不是两问**），同一个点不许写两遍。挑不出
  新东西时，**把已经提到的点讲得更细**，也胜过重说一遍。
- 分几处插入时，**各处彼此也不许重复**，更不许跟别处已经补过的话重复；
  同一个人不许连着追问同一件事——那读起来像结巴。
- 各句字数合计要接近本次要补的字数，**不要明显超出**；但**复读不算补字**，
  凑不够就往细节里写，不许把说过的话再摆一遍。
- 你写的新句**同样要过措辞禁忌**：
%s"""


def insert_system(card=None, cfg=None):
    """段内补字数的系统提示词。

    带上**与写脚本同一份**的纪律：两人怎么接（`rhythm_text`，来自本期那种对话形式）、
    形状示范（`example_block`，同一种形式给的）、
    连句上限（同一种形式给的，两人各一条）、语篇词表（同一份）、句长区间（配置里
    那两个数）、风格倾向三行、可朗读规则、两位主持人的角色。从前这几样在这里是
    另写一份的硬编码——插入按 8–40 字补、人把上限调到 30，落盘就被 `line_length`
    打回，重写一遍还是同一段。

    **只统一纪律，不吞掉插入自己的东西**：只许新增、`after` 的语义、与已写正文
    不重复的那一整套判据（连例子带判词）、措辞禁忌，都留在下面那张模板里，一个字
    没动——那些是「补写」这件事特有的，写脚本那一轮根本没有它们。
    """
    cfg = cfg or {}
    card = card or {}
    preset = (PRESET_SPEC.get(cfg.get("script.style_preset", "argument"))
              or PRESET_SPEC["argument"])
    cap_a, cap_b = _run_caps(card, cfg)
    base = _INSERT_SYSTEM_TMPL % (
        paradigms.rhythm_text(_form_key(card, cfg)), cap_a, cap_b,
        paradigms.example_block(_form_key(card, cfg), indent="  "),
        _vocab_block(),
        int(cfg.get("gate.min_chars", 8)), int(cfg.get("gate.max_chars", 40)),
        format_banned_rules())
    # 站位与对话形式与写脚本那一处同源：选了形式就顶掉卡上的站位说明。
    base += "\n\n【两位主持人】（既定阵容，不由你指定）\n" + _hosts_text(
        card, cfg, cfg.get("script.dialogue_form") or "")
    base += ("\n\n【风格倾向】（与本次补写的内容无关的通用背景；与上面的铁律"
             "冲突时以铁律为准）\n" + _style_block(preset))
    base += "\n\n" + READABLE_RULE
    return base


_TRIM_SYSTEM_TMPL = """你是播客脚本的压缩者，回答只输出 JSON，不要任何其它文字。

任务：本段的字数超了，你要把多出来的字**减够**。压紧措辞和整句拿掉，两条路随你用。

输出格式：
{"edits": [{"index": 58, "text": "压紧后的整句文本。"}], "drops": [62, 63]}
只改不删就省掉 drops；只删不改就省掉 edits。两个都空等于没干活。

铁律：
- **不许新增句子。** 你只能把现有的话说得更紧，或者整句拿掉。
- edits：改后的**完整整句**，index 填正文里给出的句号；一句就是一句，不许并句、
  不许拆句。压紧后的每句仍然是 %d–%d 字。改后的句子必须以标点符号收尾，符号按
  语义选：疑问收「？」、感叹收「！」、陈述收「。」。被点名的句子若连语篇标签一起错
  （词表外、与句型不符），在对应条目里带上 emotion 键一并改；没点名就不带。
- drops：要整句删掉的句号。**删之前先在脑子里走一遍上下文**：删掉之后，前后两句话
  还接得上吗？接不上就别删，改成把措辞压紧。
- 保语义：只去掉多余的话，**不许把有用的事实、数据、结论一起删掉**。
- 减到差不多就停，**别减过头**——减多了下一轮还得再补回来，白烧一次调用。
- 你改出来的句子**同样要过措辞禁忌**：
%s"""


def trim_system(cfg=None):
    """段内压字数的系统提示词。

    句长区间同 `patch_system`：从配置读。压紧那一步最容易把人调的窄区间顶破
    ——它按「压到 8–40 里」减，减完落在 35 字，而人把上限设成了 30，一次
    调用白烧。
    """
    lo = int((cfg or {}).get("gate.min_chars", 8))
    hi = int((cfg or {}).get("gate.max_chars", 40))
    return _TRIM_SYSTEM_TMPL % (lo, hi, format_banned_rules())


def build_insert_prompt(seg_lines, need, quota, offset, material="", evidence=None,
                        noted_material=None, raw_material=None, topic=None):
    """拼段内补字数的用户提示词。

    给的是**本段全文**，不是窗口。插在哪儿要看整段的起承转合：可能插在两句之间，
    也可能在本段末尾再接一拍——只给上下两句，它看不见该往哪儿塞。段本来就不长
    （几十句），一次给全撑不爆上下文。

    raw_material / topic：本段对应的**原文**与**段主旨**——写脚本那一轮给过的
    上下文，插入这一轮同样要给。插入句的细节依据在原文里，只给凝缩（material）
    等于让它拿着二手摘要编细节。

    素材必须带，而且只在**这个方向**带：新增的每一句都得有出处。往「压」的方向走
    恰好相反（见 `build_trim_prompt`）——那边的信息全在草稿里，递素材进去只会让它
    把刚删掉的内容换个说法搬回来。
    """
    parts = []
    ev = _evidence_block(evidence)
    if ev:
        parts.append(ev)
    if topic and str(topic).strip():
        parts.append("【本段主旨】%s" % str(topic).strip())
    if raw_material and str(raw_material).strip():
        parts.append("【本段原文】（本段覆盖的那部分原文；插入句的细节依据在这里，"
                     "范围之外的细节不要写）\n%s" % raw_material)
    if material:
        parts.append("【素材】（新增句子的依据在这里；不要照抄它的句子）\n%s"
                     % (noted_material or material))
    parts.append("【本段已有正文】（编号是它在全篇里的句号。"
                 "**这些句子一个字都不许改**）\n%s" % _numbered_rows(seg_lines, offset))
    first, last = offset + 1, offset + len(seg_lines)
    low, high = _segment_window(quota)
    parts.append("【任务】本段目前 %d 字，验收区间 %d~%d 字，**还差 %d 字**。"
                 "请依据上面的素材新增对话句子，把缺的 %d 字补足。\n"
                 "插在哪儿由你判断：可以插在本段任意两句之间，也可以接在本段末尾；"
                 "想插几处、每处插几句，都按素材和上下文定。\n"
                 "after 怎么填：插在本段最前面填 %d；插在第 N 句之后填那个句号"
                 "（本段句号是 %d~%d）；插在最后填 %d。"
                 % (int(quota) - int(need), low, high, int(need), int(need),
                    offset, first, last, last))
    parts.append("【怎么补】（按这个顺序走，中间过程不要写出来）\n"
                 "1. 先通读上面那份【本段已有正文】，在脑子里列一份「已经讲过的话」清单。\n"
                 "2. 再到【素材】里挑**正文还没讲到**的点：新事实、新数字、新步骤、\n"
                 "   新角度。素材里有、正文里没有的，优先挑。\n"
                 "3. 用这些点写新句，一个点写成一句就够，最多写成一组一问一答。\n"
                 "写完每一句都拿回第 1 步那份清单对一遍：**意思已经讲过的，划掉重写**。\n"
                 "   换个说法不算新意思，换个词问同一件事也算讲过——判据照系统提示词里"
                 "那几组正反例。")
    parts.append("现在输出 JSON。")
    return "\n\n".join(parts)


def build_trim_prompt(seg_lines, over, quota, offset, evidence=None):
    """拼段内压字数的用户提示词。

    **不带素材**：要减的话都在草稿里——压紧措辞是就着现成的字改，整句删掉是判断
    哪句多余，两者都只看草稿。递素材进去反而有害：模型会把刚删掉的内容换个说法
    搬回来，字数减不下去。
    """
    parts = []
    ev = _evidence_block(evidence)
    if ev:
        parts.append(ev)
    parts.append("【本段正文】（编号是它在全篇里的句号）\n%s"
                 % _numbered_rows(seg_lines, offset))
    low, high = _segment_window(quota)
    parts.append("【任务】本段目前 %d 字，验收区间 %d~%d 字，**多了 %d 字**。"
                 "请压紧措辞、或把没必要的话整句拿掉，把字数减进验收区间（目标 %d 字）。\n"
                 "哪几句该压、哪几句该删，由你判断——哪句是赘述、哪句在重复前一句、"
                 "哪句其实一句话就能说清。删句之前先确认上下文还接得上；接不上就别删，"
                 "改成把措辞压紧。"
                 % (int(quota) + int(over), low, high, int(over), int(quota)))
    parts.append("现在输出 JSON。")
    return "\n\n".join(parts)


def parse_insert(raw):
    """从模型输出里取出 inserts 数组。取不出就抛 ScriptError。"""
    data = _extract_json_object(raw)
    if not isinstance(data, dict) or not isinstance(data.get("inserts"), list):
        raise ScriptError("新增输出里没有 inserts 数组。")
    if not data["inserts"]:
        raise ScriptError("新增是空的：inserts 数组里一条都没有。")
    return data["inserts"]


def parse_trim(raw):
    """从模型输出里取出 (edits, drops)。两条都空就抛 ScriptError。"""
    data = _extract_json_object(raw)
    if not isinstance(data, dict):
        raise ScriptError("压字数的输出不是 JSON 对象。")
    edits = data.get("edits") or []
    drops = data.get("drops") or []
    if not isinstance(edits, list) or not isinstance(drops, list):
        raise ScriptError("压字数的输出里 edits / drops 不是数组。")
    if not edits and not drops:
        raise ScriptError("压字数是空的：既没有改的句子，也没有删的句子。")
    return edits, drops


def apply_insert(seg_lines, inserts, offset, cfg=None):
    """把新增的句子插回本段，返回新的段列表（**行数只增不减**）。

    已有句子一个字不动——这正是这条路存在的理由：补字数不该动写好的话。

    `offset` 是本段之前已有的句数，段内第 k 句的全篇句号 = offset + k；
    `after = offset` 表示插在本段最前面（即接在上一段的末句之后）。
    越界一律报错不静默丢弃：插错位置会把话塞进别的段落，比不补更坏。
    """
    cfg = cfg or {}
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    n = len(seg_lines)
    buckets = {}
    for it in inserts:
        if not isinstance(it, dict):
            raise ScriptError("新增里有不是对象的条目：%r" % (it,))
        after = it.get("after")
        if isinstance(after, bool) or not isinstance(after, int):
            raise ScriptError("新增里的 after 不是整数：%r" % (after,))
        if not offset <= after <= offset + n:
            raise ScriptError("新增的落点越界：第 %d 句（本段句号 %d~%d）"
                              % (after, offset + 1, offset + n))
        sp = str(it.get("speaker") or "").strip().upper()
        if sp not in ("A", "B"):
            raise ScriptError("新增里的 speaker 不是 A/B：%r" % (it.get("speaker"),))
        emo = str(it.get("emotion") or "").strip()
        if emo not in DISCOURSE_ORDER:
            raise ScriptError("新增里的 emotion 不在语篇词表内：%r" % (it.get("emotion"),))
        text = re.sub(r"\s+", "", str(it.get("text") or "").strip())
        if not text:
            raise ScriptError("新增里有空文本。")
        if not min_c <= len(text) <= max_c:
            raise ScriptError("新增的第 %d 句 %d 字，不在 %d~%d 字门禁内。"
                              % (after + 1, len(text), min_c, max_c))
        buckets.setdefault(after - offset, []).append(
            {"speaker": sp, "emotion": emo, "text": text})
    out = list(buckets.get(0, []))
    for k, ln in enumerate(seg_lines, 1):
        out.append(dict(ln))
        out.extend(buckets.get(k, []))
    return out


def apply_trim(seg_lines, edits, drops, offset, cfg=None):
    """把压字数的结果落回本段，返回新的段列表（**行数只减不增**）。

    只改和删，不许新增：`edits` 替换整句、`drops` 整句拿掉。同一句不许既改又删
    （那说明它自己没想清是压还是删）；也不许把本段删光（段没了，后面的账就无从对起）。
    """
    cfg = cfg or {}
    min_c = int(cfg.get("gate.min_chars", 8))
    max_c = int(cfg.get("gate.max_chars", 40))
    n = len(seg_lines)
    lo, hi = offset + 1, offset + n
    drop_set = set()
    for d in drops:
        if isinstance(d, bool) or not isinstance(d, int):
            raise ScriptError("删除列表里的句号不是整数：%r" % (d,))
        if not lo <= d <= hi:
            raise ScriptError("删除列表句号越界：第 %d 句（本段 %d~%d）" % (d, lo, hi))
        drop_set.add(d)
    if drop_set and len(drop_set) >= n:
        raise ScriptError("删除列表要把本段整段删光，拒绝落地。")
    edit_map, seen = {}, set()
    for e in edits:
        if not isinstance(e, dict):
            raise ScriptError("压字数里有不是对象的条目：%r" % (e,))
        idx = e.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ScriptError("压字数里的句号不是整数：%r" % (idx,))
        if not lo <= idx <= hi:
            raise ScriptError("压字数句号越界：第 %d 句（本段 %d~%d）" % (idx, lo, hi))
        if idx in seen:
            raise ScriptError("第 %d 句在同一份压字数里出现两次。" % idx)
        if idx in drop_set:
            raise ScriptError("第 %d 句既在 edits 里又在 drops 里：一句只能选一条路。"
                              % idx)
        text = re.sub(r"\s+", "", str(e.get("text") or "").strip())
        if not text:
            raise ScriptError("压字数把第 %d 句改成了空文本。" % idx)
        if not min_c <= len(text) <= max_c:
            raise ScriptError("压完后第 %d 句 %d 字，不在 %d~%d 字门禁内。"
                              % (idx, len(text), min_c, max_c))
        seen.add(idx)
        edit_map[idx] = {"text": text}
        if e.get("emotion") in DISCOURSE_ORDER:
            edit_map[idx]["emotion"] = e["emotion"]
    out = []
    for k, ln in enumerate(seg_lines, 1):
        if offset + k in drop_set:
            continue
        row = dict(ln)
        if offset + k in edit_map:
            row.update(edit_map[offset + k])
        out.append(row)
    return out


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


def _parse_segment_lines(raw, vocab=None):
    """解析单段输出 {"lines": [...]}。解析失败抛 ScriptError。

    `vocab` 是本篇语篇词表；不传按全表收。emotion 缺失或不在
    词表内都算解析失败——约束解码下不该发生，发生了说明走了降级路，
    打回重摇比静默补「承接」好：缺标签的句子混进稿里，问句率的账就乱了。
    """
    data = _extract_json_object(raw)
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, list) or not lines:
        raise ScriptError("本段输出缺 lines 数组。")
    allow = set(vocab or DISCOURSE_ORDER)
    out = []
    for l in lines:
        if not isinstance(l, dict):
            raise ScriptError("lines 元素不是对象。")
        sp = str(l.get("speaker") or "").strip().upper()
        tx = str(l.get("text") or "").strip()
        emo = str(l.get("emotion") or "").strip()
        if sp not in ("A", "B") or not tx:
            raise ScriptError("lines 元素缺合法的 speaker 或 text。")
        if emo not in allow:
            raise ScriptError("lines 元素缺 emotion，或标签不在本篇词表内：%r"
                              % (l.get("emotion"),))
        out.append({"speaker": sp, "emotion": emo, "text": tx})
    return out


def _segments_plan(llm, cfg, project, evidence, groups, log, should_stop):
    """规划轮：段边界已由「逻辑拆分 + 装箱」定死，这一轮只产「本期标题 + 每段一句段主旨」。

    返回 (title, groups)（段主旨填进 groups）。**它不碰段边界**——哪几节是一件事
    是语义判断（逻辑拆分做的），一段装不装得下是容量算术（装箱做的）；这一轮
    只做"读懂内容才写得出"的那两件事。**也不看字数**：配额是程序摊的，字数摆进
    来只会把"这一段讲什么"带成"这一段该多长"。
    反复不过就退到按各段首节的凝缩主旨代填，不再让程序去猜内容。
    """
    secs = evidence.get("sections") or []
    n = len(groups)
    system = _segment_plan_system(n)
    user = _segment_plan_user(project, secs, groups)
    title, fb = "", ""
    for attempt in range(PLAN_RETRIES + 1):
        if should_stop and should_stop():
            break
        u = user if not fb else (
            user + "\n\n【上一版规划的问题】%s\n请重新给出标题与各段段主旨。" % fb)
        log("规划轮：第 %d 次尝试…" % (attempt + 1))
        raw, _meta = llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": u}],
            temperature=float(cfg.get("llm.temperature", 0.8)),
            # 预算一律取配置值，不在这里夹小值。推理型模型的思考段与答案段共用
            # 这一份预算，夹到 4096 时思考段会先把它吃光（实测 reasoning_tokens=
            # 4095），答案段一个字也轮不上——报出来是「模型返回空答案」，看着
            # 像模型不听话，起因却是预算被调用处改小了。输出短不短由 maxItems
            # 与字数核账管，都轮不到预算这一层出力。
            max_tokens=int(cfg.get("llm.max_tokens", 8192)),
            json_schema=_segment_plan_schema(n))
        try:
            data = _extract_json_object(raw)
            if not isinstance(data, dict):
                raise ScriptError("输出不是 JSON 对象。")
        except ScriptError as e:
            fb = str(e)
            log("规划打回（第 %d 次）：%s" % (attempt + 1, fb))
            continue
        title = str(data.get("title") or "").strip()[:TITLE_MAX]
        topics, err = _validate_plan(data, n)
        if topics:
            for g, t in zip(groups, topics):
                g["topic"] = t
            return title, groups
        fb = err or "规划不合法。"
        log("规划打回（第 %d 次）：%s" % (attempt + 1, fb))
    # 兜底：段主旨取本段首节的凝缩主旨。段边界与配额都不动——那是程序算好的。
    log("规划 %d 次仍未通过，段段主旨按各段首节的凝缩主旨代填" % (PLAN_RETRIES + 1))
    for g in groups:
        i = (g.get("sections") or [1])[0]
        gist = str((secs[i - 1].get("gist") if 1 <= i <= len(secs) else "") or "")
        g["topic"] = gist[:40] or "本节内容"
    return title, groups



def _refit_pieces(pieces, llm, avail_tokens, log=None):
    """把一段的 pieces 按**整节**分成几批，直到每批都装得下（运行时兜底）。

    装箱时按折算比估的重量，运行时可能偏大（折算比有波动，或已写正文比配额长了
    一截），一段就喂不完。这里的处理是**按整节分批发**——「分几次喂完」永远优先
    于「少喂」：不丢原文、也不换成凝缩。

    切分单位仍是整节：**单个节自己就装不下时报错**，不许再往下切。半个节取不出
    原文（`source_store.compose` 一次只认一个落点），也算不出账。
    """
    log = log or (lambda m: None)
    batches, cur, cur_tok = [], [], 0
    for pc in pieces:
        chars = int(pc.get("chars") or 0)
        tok = llm.material_tokens(chars) if chars > 0 else 0
        if tok > avail_tokens:
            raise ScriptError(
                "本段的第 %s 节（%d 字，折合 %d token）超过可用 %d token。"
                "分段按整节凝缩切，单个节到这就切无可切了——"
                "请调大 llm.input_ratio，或回地图拆期。"
                % (pc.get("sec"), chars, tok, avail_tokens))
        if cur and cur_tok + tok > avail_tokens:
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(pc)
        cur_tok += tok
    if cur:
        batches.append(cur)
    return batches


def _generate_segmented(llm, cfg, material, evidence, card, preset_key,
                        project, target_chars, log, should_stop, draft_sink,
                        sec_chars=None, material_of=None,
                        sec_flags=None):
    """分段生成整篇初稿：逻辑拆分 → 逐段过桶 → 规划轮起名 → 逐段生成 → 每段核账。

    **段边界分两步走，各有各的判据：**

    1. **逻辑拆分**（`split_by_logic`）：模型按**内容**把节分组成段——哪几节讲的
       是同一件事。它只回节序号，不回标题、不回段主旨、不回任何数字。
    2. **逐段过桶**（`pack_segments`）：桶逐个逻辑段过输入额度。装得下 → 这个
       逻辑段就是一段；装不下 → 在它**内部**按整节切成几段；单个节自己就超容
       → 报错（切分单位是整节，到这就切无可切）。

    段边界到此**只有一个来源**。从前禁止让模型分组，理由是「两边都去定边界，
    边界就不存在」——现在装箱不再定边界、降级成容量校验，那个前提消失了。
    规划轮只产「本期标题 + 每段一句段主旨」，它不碰边界，也不看字数。

    每段只喂**本段那几节的原文**（恒为整节，不再有半节），不再整期素材过一遍
    闸门。段内若仍装不下（折算比偏大或已写正文过长），按整节分成几批喂完——
    **不丢原文、也不换成凝缩**。

    返回 (title, plan, script_lines)；无可分段时返回 None（调用方退回整篇一路）。
    账本单位只有字：段写完程序数实际字数，超差的段不重写、也不改已有句子——
    少了插入新句、多了压紧删减；取最接近配额的一版收尾，差额按比例滚入后面各段。
    """
    preset_key = preset_key or cfg.get("script.style_preset", "argument")
    # 本篇语篇词表：schema 枚举、解析校验两处同源（只有一份）
    # （提示词侧由 _segment_system_prompt 内部按同一函数现算）。
    seg_vocab = vocab_words()
    secs = evidence.get("sections") or []
    if not secs:
        return None
    # 取材标记：命中节的节号 → 命中家族。判定在上游（web_ui 拿着逐节原文
    # 现判，这里不重复取料）；只改展示文本，压比/称重/节权重全吃原始素材。
    sec_flags = {int(k): list(v or []) for k, v in (sec_flags or {}).items()
                 if v}
    if sec_flags:
        log("取材标记：%s"
            % "、".join("第%d节（%s）" % (no, "/".join(fs))
                        for no, fs in sorted(sec_flags.items())))
    # 装箱要把「每次调用必带的固定开销」先量出来：一段的提示词长度量级 + 固定
    # 占位。段与段之间这点差异是几十字，不必逐段精算。
    probe_system = _segment_system_prompt(cfg, card, preset_key, 1, 1,
                                          SEGMENT_TOL_MIN_CHARS, "（段主旨）",
                                          is_first=True)
    fixed_chars = len(probe_system) + SEGMENT_FIXED_CHARS
    budget = (llm.input_budget_tokens(int(cfg.get("llm.max_tokens", 8192)))
              if hasattr(llm, "input_budget_tokens") else 0)
    log("分段 · 第1步 逻辑拆分：依据本期主旨与 %d 节凝缩，把连续的节按内容分组"
        "（只回节号）" % len(secs))
    logic = split_by_logic(secs, llm, cfg, log=log, project=project)
    log("逻辑拆分完成：%d 节 → %d 个逻辑段（%s）"
        % (len(secs), len(logic),
           "、".join("逻辑段%d = 第%s节" % (i + 1, _sec_nos_text(g))
                     for i, g in enumerate(logic))))
    # 碎段合并走在装箱前面：只改分组（节顺序不变），装箱按容量再切是兜底。
    logic = _merge_tiny_groups(secs, logic, target_chars, cfg, log=log)
    if budget:
        log("分段 · 第2步 装箱：逐个逻辑段量体量（输入额度 %d token − 固定开销 − "
            "已写正文，全篇目标约 %d 有效字）；装不下就在这一组内按整节再切"
            % (budget, int(target_chars)))
    else:
        # 量不出额度的后端（假模型、或将来别的客户端）：装不装得下判不了，逻辑
        # 分组原样落段。这里要是照旧打「输入额度 0 token」，读日志的人会以为
        # 额度真的被吃光了，而实际情况是这一路根本没量。
        log("分段 · 第2步 装箱：这个后端量不出输入额度，逻辑分组原样落段"
            "（不合并、不再切）；全篇目标约 %d 字" % int(target_chars))
    groups = pack_segments(secs, sec_chars or [], llm, cfg,
                           fixed_chars=fixed_chars, target_chars=target_chars,
                           log=log, groups=logic)
    log("装箱完成：%d 个逻辑段 → %d 个写作段，配额合计 %d 有效字（%s）"
        % (len(logic), len(groups), sum(g["quota"] for g in groups),
           "、".join("第%d段%d有效字·%s" % (i + 1, g["quota"], _piece_desc(g, secs))
                     for i, g in enumerate(groups))))
    title, groups = _segments_plan(llm, cfg, project, evidence, groups,
                                   log, should_stop)
    log("规划完成：%d 个写作段段主旨已定（%s）"
        % (len(groups),
           "、".join("第%d段「%s」" % (i + 1, str(g.get("topic") or "")[:16])
                     for i, g in enumerate(groups))))

    plan = int(project.get("planned_episodes") or 0) if project else 0

    def mat_of(pieces):
        """本段（本块）该喂的原文。拿不到逐节原文时退回整期素材（老路）。"""
        if material_of is None:
            return material
        try:
            return material_of(pieces) or ""
        except Exception as e:                              # noqa: BLE001
            log("本段取材失败（%s），退回整期素材" % e)
            return material

    total_groups = len(groups)
    queue = [[i, g, list(g.get("pieces") or []), g["quota"]]
             for i, g in enumerate(groups, 1)]
    script, written, done = [], 0, 0
    while queue:
        if should_stop and should_stop():
            log("收到中止：还剩 %d 段没写，带着已写 %d 有效字收工" % (len(queue), written))
            break
        i, g, pieces, quota0 = queue.pop(0)
        seg_mat = mat_of(pieces)
        # 动态配额：前面段的差额按比例摊到本段，总账始终咬住目标。
        remaining_planned = quota0 + sum(it[3] for it in queue)
        quota = quota0
        if done > 0 and remaining_planned > 0:
            quota = max(SEGMENT_TOL_MIN_CHARS,
                        int(round(quota0 * (int(target_chars) - written)
                                  / float(remaining_planned))))
        topic = g.get("topic") or ""
        attempt, best, best_gap = 0, None, None
        low, high = _segment_window(quota)
        tol = high - int(quota)
        seg_lines, feedback, resplit = None, None, False
        while True:
            if should_stop and should_stop():
                break
            if seg_lines is None:
                # 首轮：整段写。之后**不再重写整段**——超容差改走定点修补（见下），
                # 已写的句子留着，只改点名的那几句。
                system = _segment_system_prompt(cfg, card, preset_key, i,
                                                total_groups, quota, topic,
                                                is_first=(i == 1), feedback=feedback)
                prev_text = "\n".join("%s：%s" % (l["speaker"], l["text"])
                                      for l in script) if script else ""
                other_chars = (len(system) + len(prev_text)
                               + SEGMENT_FIXED_CHARS)
                # 段内还装不下：再切细、分几次喂完（不丢原文、不换凝缩）。
                if material_of is not None and hasattr(llm, "input_budget_tokens"):
                    avail = (llm.input_budget_tokens(
                        int(cfg.get("llm.max_tokens", 8192)))
                        - llm.material_tokens(other_chars))
                    if avail > 0 and llm.material_tokens(len(seg_mat)) > avail:
                        # 按整节分成几批分次喂完。单节自身就超容时，
                        # `_refit_pieces` 直接报错——不往下切半个节。
                        sub = _refit_pieces(pieces, llm, avail, log)
                        total_chars = sum(int(pc.get("chars") or 0)
                                          for pc in pieces) or 1
                        for n, one in enumerate(sub):
                            share = sum(int(pc.get("chars") or 0) for pc in one)
                            q = max(SEGMENT_TOL_MIN_CHARS, int(round(
                                quota0 * share / float(total_chars))))
                            queue.insert(n, [i, g, one, q])
                        log("段 %d：本段原文折合 %d token 超可用 %d，"
                            "按整节分成 %d 批分次喂完"
                            % (i, llm.material_tokens(len(seg_mat)), avail,
                               len(sub)))
                        resplit = True
                        break
                fit, _note = fit_material(llm, seg_mat, cfg,
                                          other_chars=other_chars,
                                          evidence=evidence, log=log)
                user = _segment_user_prompt(project, evidence, g, quota, written,
                                            target_chars, fit, prev_text,
                                            feedback, pieces=pieces,
                                            noted_fit=_apply_sec_notes(
                                                fit, sec_flags))
                log("段 %d/%d：调用模型…（配额 %d 有效字，取材 %s，段主旨：%s）"
                    % (i, total_groups, quota, _piece_desc({"pieces": pieces}, secs),
                       topic[:30]))
                raw, meta = llm.chat(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    temperature=float(cfg.get("llm.temperature", 0.8)),
                    max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                    json_schema=segment_schema(quota, cfg, vocab=seg_vocab))
                log("段 %d：%s" % (i, _call_telemetry(meta)))
                try:
                    seg_lines = _parse_segment_lines(raw, vocab=seg_vocab)
                except ScriptError as e:
                    attempt += 1
                    if attempt > SEGMENT_RETRIES:
                        raise ScriptError("第 %d 段连续 %d 次输出无法解析：%s"
                                          % (i, SEGMENT_RETRIES + 1, e))
                    feedback = ("本段输出没法解析（%s）。请只输出本段的 lines JSON。"
                                % e)
                    log("段 %d：%s" % (i, feedback))
                    continue
                # 返回行只报正文：解析出的 lines 逐句算有效字求和，与配额同一把尺。
                # 原始 HTTP 串的长度（含 JSON 壳）对核账毫无意义，不再进日志。
                log("段 %d：模型返回正文 %d 有效字%s"
                    % (i, int(round(sum(duration_model.effective_chars(l["text"])
                                        for l in seg_lines))),
                       "（约束解码已降级）" if meta.get("degraded") else ""))
            else:
                # 修正轮：**不重写整段，也不改已有句子来凑字数**。
                #   少了 → 新增句子插进去（现有句子一个字不动；插在哪几处、插几句、
                #          每句多长，全由模型按素材和上下文定）；
                #   多了 → 压紧措辞或整句拿掉（不带素材，信息都在草稿里）。
                # 两条路给模型的都只有字数（「少 / 多 N 字」），一个句数都不报。
                #
                # 核账口径 = **有效字**，与配额同一把尺。配额（target_chars 摊下来的）
                # 单位是有效字（汉字 1、标点 0.5、西文词 1.5，duration_model 一家
                # 定义）；从前的核账拿 len() 数字符去减它——当年 fit_material 修掉
                # 的「左边朗读字数、右边 len() 字符，比较没有意义」（v0.16.0 更新
                # 日志原话），在输出侧原样活着。现在两端同尺。
                have0 = int(round(sum(duration_model.effective_chars(l["text"])
                                      for l in seg_lines)))
                gap0 = have0 - quota
                # body 只干一件事：折 token 估提示词大小。tokenizer 吃的是原始
                # 字符，所以这一处**故意**留 len()——不是漏改，是单位本来就该不同。
                body = sum(len(l["text"]) for l in seg_lines) + len(seg_lines) * 24
                if gap0 < 0:
                    psystem = insert_system(card, cfg)
                    fit, _note = fit_material(
                        llm, seg_mat, cfg,
                        other_chars=len(psystem) + body + SEGMENT_FIXED_CHARS,
                        evidence=evidence, log=log)
                    log("段 %d：少 %d 有效字，新增句子插入…" % (i, abs(gap0)))
                    raw, meta = llm.chat(
                        [{"role": "system", "content": psystem},
                         {"role": "user", "content": build_insert_prompt(
                             seg_lines, -gap0, quota, len(script), material=fit,
                             evidence=evidence,
                             noted_material=_apply_sec_notes(fit, sec_flags),
                             raw_material=seg_mat, topic=topic)}],
                        temperature=float(cfg.get("llm.temperature", 0.8)),
                        max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                        json_schema=insert_schema(-gap0, cfg, vocab=seg_vocab))
                    log("段 %d：%s" % (i, _call_telemetry(meta)))
                    if meta.get("degraded"):
                        log("段 %d：约束解码已降级" % i)
                    try:
                        inserts = parse_insert(raw)
                        fixed = apply_insert(seg_lines, inserts, len(script), cfg)
                    except ScriptError as e:
                        # 新增没落地就收工：稿子一个字没动，退回上一版不算损失。
                        log("段 %d：新增没法落地（%s），取最接近配额的一版继续" % (i, e))
                        break
                    added = int(round(sum(duration_model.effective_chars(l["text"])
                                          for l in fixed))
                                - sum(duration_model.effective_chars(l["text"])
                                      for l in seg_lines))
                    log("段 %d：插进 %d 句 / %d 有效字"
                        % (i, len(fixed) - len(seg_lines), added))
                    seg_lines = fixed
                else:
                    psystem = trim_system(cfg)
                    log("段 %d：多 %d 有效字，压紧删减…" % (i, gap0))
                    raw, meta = llm.chat(
                        [{"role": "system", "content": psystem},
                         {"role": "user", "content": build_trim_prompt(
                             seg_lines, gap0, quota, len(script), evidence=evidence)}],
                        temperature=float(cfg.get("llm.temperature", 0.8)),
                        max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                        json_schema=trim_schema(len(seg_lines), vocab=seg_vocab))
                    log("段 %d：%s" % (i, _call_telemetry(meta)))
                    if meta.get("degraded"):
                        log("段 %d：约束解码已降级" % i)
                    try:
                        edits, drops = parse_trim(raw)
                        fixed = apply_trim(seg_lines, edits, drops, len(script), cfg)
                    except ScriptError as e:
                        log("段 %d：压字数没法落地（%s），取最接近配额的一版继续" % (i, e))
                        break
                    log("段 %d：压 %d 句 / 删 %d 句" % (i, len(edits), len(drops)))
                    seg_lines = fixed
                if not seg_lines:
                    break

            chars = int(round(sum(duration_model.effective_chars(l["text"])
                                  for l in seg_lines)))
            gap = chars - quota
            if best is None or abs(gap) < abs(best_gap):
                best, best_gap = list(seg_lines), gap
            if abs(gap) <= tol:
                break
            attempt += 1
            if attempt > SEGMENT_RETRIES:
                log("段 %d：修 %d 轮仍差 %d 有效字，取最接近配额的一版继续"
                    % (i, SEGMENT_RETRIES, abs(best_gap)))
                break
        if resplit:
            continue
        seg_lines = best or []
        script.extend(seg_lines)
        seg_chars = int(round(sum(duration_model.effective_chars(l["text"])
                                  for l in seg_lines)))
        written += seg_chars
        done += 1
        log("段 %d/%d 完成：%d 句 / %d 有效字（配额 %d 有效字；累计 %d/%d 有效字）"
            % (i, total_groups, len(seg_lines), seg_chars, quota,
               written, int(target_chars)))
        # 每段写完就落盘：十几段逐段生成，中途卡住、被中止，手上还有已写的部分。
        # 落的是**半期正文，不粘片头尾**——这不是成品，是写到一半的底稿。粘上就成了
        # 「半期 + 片头 + 片尾」：片尾已经在稿子里了，内容却只写了一半，看形态像一整
        # 期、其实是残缺。片头尾只在**整期拼完**、走完门禁与定点修补、定稿那一刻粘一次
        # （见 `generate` 里的 `_finish`），一段一次会让一整期粘好几遍。
        if draft_sink is not None:
            draft_sink({"script": list(script), "title": title,
                        "planned_episodes": plan,
                        "report": {"items": [], "fails": [], "warns": [],
                                   "pending": [], "soft": [], "passed": False,
                                   "partial": True}})
    if not script:
        return None
    # 段清单跟着正文一起出去：段主旨（`_segments_plan` 填进 groups 的那一句）
    # 从前只活在内存里，函数一返回就没了——「前期回顾」要引用上一期讲了哪几块，
    # 就得先把它留下来（落进 `layout.plan_file` 那份旁挂）。
    # 正文一个字不带它：出片、时长、字幕读的都是扁平句子数组。
    segments = [{"no": i + 1, "topic": str(g.get("topic") or ""),
                 "sections": list(g.get("sections") or []),
                 "quota": int(g.get("quota") or 0)}
                for i, g in enumerate(groups)]
    return title, plan, script, segments


# ------------------------------------------------------------------ 主流程
def generate(material, cfg, llm, preset_key=None,
             max_rounds=None, log=None, project=None, should_stop=None,
             draft_sink=None, evidence=None, segmented=False,
             sec_chars=None, material_of=None, sec_flags=None, review=None):
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
    无论出什么事，手上都有一份完整的稿子。它落的是**正文**（过程稿），片头尾
    要到整期定稿那一次才粘上去（见 `_finish`）。

    `evidence` 是本期判据包（主旨 + 各节凝缩）：内容检判「方向」时用它。素材
    按**输入额度**裁（见 `fit_material`）——额度 = 最大输出 × `llm.input_ratio`，
    倍率由使用者自己定（程序不探、也不管后端窗口多大）；与「这一期该讲多少料」
    是两回事（后者是画地图的压比，
    见 `material_capacity`）。装不下时按节装箱切成多段分别喂完，不丢原文。

    `segmented` 是**路线判据，由调用方按项目模式给出**：成稿规划（mapped）为真，
    逐期即兴与单集为假。它不是给人选的偏好——两条路各自成立的前提不同：mapped
    有地图与各节凝缩，逐期配额、防重复、段级核账才有依据；episodic 与 single
    是当场给料、出完即止，没有凝缩可分，整篇一次写完就是这条路该有的样子。
    mapped 但本期凝缩缺失（项目还没排图、凝缩版本对不上）时仍退回整篇，不让
    人卡在一步跑不动。

    `review` 是**前期回顾**（一组填好文本的句子，由 `pipeline.review_rows` 组），
    只影响定稿那一刻的粘合位置（片头之后），不进任何一轮生成、不过门禁、不核
    字数——与片头尾同一条纪律。
    """
    preset_key = preset_key or cfg.get("script.style_preset", "argument")
    # 本篇语篇词表：整篇路与定点修补共用（新增/压紧在分段路里各自取）。
    whole_vocab = vocab_words()
    max_rounds = int(max_rounds if max_rounds is not None else cfg.get("script.max_llm_rounds", 3))
    log = log or (lambda m: None)

    # 两个量各报各的，一期只报这一次。别混：
    #   内容额度 = 成稿目标 × 偏移 1.25 × 档位（下限 = 成稿 × 1.25 × 1.2）——
    #     回答「这一期该讲多少料」，参照系是成稿脚本，跟模型无关；
    #   输入额度 = llm.max_tokens × llm.input_ratio —— 回答「这一次调用装不装得下
    #     原文」，倍率由使用者自己定。后端窗口有多大不归程序管：那是加载时才定下
    #     的数、问不出来（懒加载的接口只会回"未加载"），够不够由使用者自己拿主意。
    #     日志只报换算结果，不对后端提要求。
    if hasattr(llm, "quota_note"):
        log("本期内容额度 %d 有效字（= 成稿目标 %d 有效字 × 偏移 1.25 × 1:%d 档；"
            "下限 = 成稿 × 1.25 × 1.2 = %d 有效字）——这是画地图的压比尺子，"
            "不是本次调用的输入额度"
            % (material_capacity(cfg),
               probe.target_chars(cfg), probe.ratio_of(cfg),
               int(probe.offset_chars(cfg) * probe.MIN_RATIO)))
        log(llm.quota_note(int(cfg.get("llm.max_tokens", 8192)),
                           len(material or "")))

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
    # 范式卡只取一次，提示词与门禁共用：分头各取一次，同一份素材可能拿到两张卡
    # （提示词按方法论写、门禁按自适应判），稿子会陷在「改了还是不过」。
    card = resolve_paradigm(project, cfg)
    # 档位在这一层也取一次：两条路的约束解码 schema（整篇生成、定点修补）都按它

    # 路线由项目模式定死（见 `segmented` 的说明）：成稿规划走分段——**先按输入
    # 额度装箱定段**，再让规划轮给每段写段主旨，然后逐段生成、逐段核字数配额，
    # 拼出整篇初稿；逐期即兴与单集走整篇。两条路之后都汇进同一套门禁与定点修补，
    # 判据只此一处。
    pre_draft = None
    # 段清单（段号/段主旨/覆盖节号/配额）：由分段路产出，整期定稿时随 payload
    # 交给落盘方写进旁挂规划档（`layout.plan_file`）。整篇路与退回整篇时为空——
    # 那两条路没有段主旨可言，旁挂也就没有内容可写。
    segments = []
    if segmented and (evidence or {}).get("sections"):
        seg = _generate_segmented(llm, cfg, material, evidence, card,
                                  preset_key, project, target_chars, log,
                                  should_stop, draft_sink,
                                  sec_chars=sec_chars, material_of=material_of,
                                  sec_flags=sec_flags)
        if seg is not None:
            seg_title, seg_plan, seg_script, seg_segs = seg
            segments = seg_segs
            # 片头尾不在这里粘：门禁与定点修补看到的只能是正文。粘合在整期定稿
            # 那一刻做一次（见 `glue_intro_outro` 与 `_finish`）。
            seg_script = normalize_script(seg_script, cfg)
            seg_title = seg_title or title_from_lines(seg_script)
            # 地图已定的事以人为准：标题与总期数不挂在模型自觉上。
            if project and project.get("title"):
                seg_title = project["title"]
            pre_draft = {"script": seg_script, "title": seg_title,
                         "planned_episodes": seg_plan}
        else:
            log("本期各节凝缩不可得，退回整篇生成")
    elif segmented:
        # mapped 但本期凝缩读不出来：不让人卡在「一步跑不动」上，退回整篇
        # 照常出稿，缺的那段判据由 `evidence_pack` 的日志说明。
        log("本期未取到各节凝缩，退回整篇生成")

    system = build_system_prompt(cfg, preset_key, target_chars, line_count,
                                 project=project, paradigm=card)
    form_key = paradigms.resolve_form(card, cfg.get("script.dialogue_form") or "")
    log("素材类型：%s；对话形式：%s（连着说的上限 A %d 句 / B %d 句）"
        % (card.get("label") or "自适应", paradigms.DIALOGUE_FORMS[form_key]["label"],
           *_run_caps(card, cfg)))
    log("目标：%.0f 秒 / 约 %.0f 字 / 约 %d 句（按标准语速 %.2f 有效字/秒 ≈ %d 汉字/分钟）"
        % (target_seconds, target_chars, line_count,
           duration_model.STANDARD_K, duration_model.STANDARD_CPM))

    feedback = None
    prev_lines = None   # 上一版正文（编号口径与门禁报的句号一致），整篇重出时对照用
    last = None
    content_seen = None   # 上一次内容检的结论，决定这一轮重判哪几项

    def _finish(payload):
        """整期定稿：**粘一次**片头尾 → 落盘 → 返回。粘合全篇只在这儿发生。

        片头尾不进轮：它不参与生成、不过门禁、不被定点修补、不核字数——从模板
        逐字拼出来的东西，没有「模型照没照做」可验（见 `glue_intro_outro`）。
        所以中间各轮落的盘一律是**裸正文**（过程稿），只有整期定稿这一刻才粘。

        两个出口都走这里：门禁过了从这里定稿；轮次用尽也从兜底的 `return` 定稿
        ——不管改成功没改成功，交到手上的那一份都带片头尾。

        定稿这一份**必须落盘**：出片读的是盘上那份（`draft_sink` 写的位置），
        界面拿的是返回值。只粘不落，盘上留着的就是上一轮的裸正文，看的和念的
        对不上。
        """
        glued = _glued_draft(payload, cfg, log, review=review)
        if draft_sink is not None:
            draft_sink(glued)
        return glued

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
                                                    len(last["script"]))
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
            psystem = patch_system(cfg)
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
                json_schema=patch_schema(vocab=whole_vocab))
            log("第 %d 轮：%s" % (attempt + 1, _call_telemetry(meta)))
            if meta.get("degraded"):
                log("第 %d 轮：约束解码已降级" % (attempt + 1))
            try:
                edits = parse_patch(raw)
                script = apply_patch(last["script"], edits, patch)
            except ScriptError as e:
                # 补丁没落地就整篇重出：这一轮的稿子一个字没动，退回上一版不算损失。
                # 不拿「半份补丁」凑合——那会让「改了几处、漏了哪几处」说不清。
                log("第 %d 轮：补丁没法落地（%s），改回整篇重出" % (attempt + 1, e))
                patch = None
            else:
                script = normalize_script(script, cfg)
                # 补丁路的返回行同样只报正文有效字（补完后的全稿求和），
                # 且保留「第 N 轮 … 模型返回」形态供 web_ui 推进轮次进度。
                log("第 %d 轮：模型返回正文 %d 有效字"
                    % (attempt + 1,
                       int(round(sum(duration_model.effective_chars(l["text"])
                                     for l in script)))))
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
            # 公共尾巴记 raw_chars/degraded：分段路没有「原始整篇输出」，按 0 记。
            raw, meta = "", {}
            pre_draft = None
        elif not patch:
            # 整篇重出：第 1 轮走这里，之后是结构坏了或补丁落不了地。
            # 素材按这次调用的余量裁：系统提示、上一版正文都占地方，
            # 先量再定能给素材多少字。放不下就改用凝缩，并把这件事写进日志——
            # 静默少喂会让模型在缺依据的情况下硬写，产物上看不出少了什么。
            fit, _note = fit_material(
                llm, material, cfg,
                other_chars=len(system) + len(prev_lines or "")
                            + WRITE_FIXED_CHARS,
                evidence=evidence, log=log)
            user = build_user_prompt(fit, cfg, feedback, prev_lines)
            # 这一行在调用之前写。生成一轮要十几分钟，日志里若只有「模型返回」那一行，
            # 整轮期间界面上是上一次留下的字——看着像卡住了。
            log("第 %d 轮：调用模型…" % (attempt + 1))
            raw, meta = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=script_schema(vocab=whole_vocab))
            if meta.get("degraded"):
                log("第 %d 轮：约束解码已降级" % (attempt + 1))
            log("第 %d 轮：%s" % (attempt + 1, _call_telemetry(meta)))

            try:
                parsed = parse_script(raw)
            except ScriptError as e:
                last = {"error": str(e)}
                feedback = "输出不是合法 JSON 对象。请只输出 JSON 对象本身。"
                continue

            # 返回行只报正文有效字（解析出的 lines 逐句求和，与配额同尺）；
            # 这一行同时是 web_ui 轮次进度的推进信号，必须保留「第 N 轮 … 模型返回」形态。
            log("第 %d 轮：模型返回正文 %d 有效字"
                % (attempt + 1,
                   int(round(sum(duration_model.effective_chars(l["text"])
                                 for l in parsed["lines"])))))
            script = normalize_script(parsed["lines"], cfg)
            # 连说超限不在这里掰：正文的归属由门禁点名、交模型并句（见 `patch_targets`
            # 里的 ab_run_limit）。片头尾也不参与——它们要到整期定稿那一刻才粘
            # （见 `glue_intro_outro`）。
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
            # 删句会让原本被重复段隔开的同一个人挨到一起，可能冒出新的连说超限。
            # 这里不掰：门禁会把整段点出来、交模型并句——程序翻的话，翻出来的是一句
            # 口气对不上、甚至根本不属于这个人的话。

        # 记下这一版的正文。编号必须取这一版定型后的序号（含片头尾写死与连续句
        # 翻转的结果）——门禁报的「第 58 句」就是按这个序号数的，拿别的版本去
        # 对，模型会改错行。
        prev_lines = "\n".join("%d. [%s] %s" % (i + 1, s["speaker"], s["text"])
                               for i, s in enumerate(script))

        report = gate_generate(script, cfg, card)
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
                # 段清单（分段路有、整篇路空）：随定稿一起交出去，落盘方据此写
                # 旁挂规划档。正文那份 json 一个字段都不加（见 layout.plan_file）。
                "segments": segments,
                "report": report, "raw_chars": len(raw),
                "degraded": meta.get("degraded", False), "attempt": attempt + 1}
        if changed is not None:
            last["patched_lines"] = changed
        if report["passed"]:
            log("门禁通过（第 %d 轮）" % (attempt + 1))
            return _finish(last)

        # 没过：这一轮落的是**过程稿**——裸正文，片头尾不粘。
        #
        # 每轮写完就落盘：稿子在手，后面哪一轮卡住、被中止、进程崩了，都不至于从头
        # 再来。但落的是「写到这儿的正文」，不是成品——整期还没定稿，片头尾挂上去
        # 等于给半成品盖上成品的样子；下一轮定点修补按句号改句子，粘合版还会让句号
        # 整体后移两位。每轮粘一遍，日志上也会跟着冒一串「片头尾已粘上」，读的人
        # 分不清是粘重了还是落了几次盘。粘合只在整期定稿那一刻做一次（见 `_finish`）。
        #
        # 最后一轮不落：紧接着的兜底出口会把定稿落上去，同一份文件写两遍是白写。
        if draft_sink is not None and attempt < max_rounds:
            draft_sink(last)

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
        # 门禁没过也要粘、也要落盘：片头尾不是「门禁通过」的奖励，是每一期都该有的
        # 固定结构（见 glue_intro_outro）。轮次用尽时手上这一版就是这一期的定稿——
        # 改成功没改成功，都由它出片。最后一轮的裸正文没落（见循环里那处说明），
        # 定稿这一落正好把它补上。
        return _finish(last)
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
                    "**不许并到相邻句里去**。"
                    % (h["line"], h["chars"], lo - h["chars"], lo))
    return "\n".join(rows)


def _feedback_end_punct(p):
    """句尾缺标点的回灌：逐句点名给尾字符与样例，方向给「按语义选符号」。

    与措辞禁忌同一条纪律：不明说「只改这几句」，模型会通篇重写。补什么符号
    是语义的事，py 只报「没有」，方向（疑问？感叹！陈述。）跟着每一条走。
    """
    hits = p.get("hits") or []
    if not hits:
        return ""
    rows = ["- 句尾缺标点 %d 处。**只改下面这几句，其余句子一律照抄**："
            % len(hits)]
    for h in hits:
        rows.append("    第 %d 句以「%s」收尾（「…%s」）：按这句话的语义补一个"
                    "合适的句尾标点——疑问收？、感叹收！、陈述收。"
                    % (h["line"], h.get("tail", ""), h.get("sample", "")))
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
        elif key == "line_end_punct":
            txt = _feedback_end_punct(p)
        else:
            txt = "- %s：%s" % (p["label"], p.get("detail", ""))
        if txt:
            rows.append(txt)
    return "\n".join(rows)
