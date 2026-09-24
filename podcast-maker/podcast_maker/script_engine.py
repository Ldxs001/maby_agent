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
                             GATE_BY_KEY, INTRO_OUTRO, PRESET_SPEC,
                             PROGRAM_ONLY_TAGS, STYLE_DIMS)


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


def _edit_item_schema(vocab, min_chars=None):
    """定点修补的 edit 条目：index+text 必填，emotion / absorb 可选。

    emotion 设为可选：edit 只在修被点名句子时出现，多数修补只动 text；
    但语篇标签错了（词表外、与句型不符）也要有得改——给了枚举，模型改得动。

    absorb 也设为可选，只给「同一人连着说超限」用：值 N 表示这一句替掉紧随其后的
    N 句（并句），行数因此少 N。**并句是减行数的唯一合法表达**——超限要落到上限
    以内，而补丁不许拆句、不许凭空加句，就只剩「几句并成一句」这条路。它没给
    模型开「随便增删」的口子：`apply_patch` 会核被并的每一句都在点名清单里、
    都与这一句同一个人、并出来的字没有被吃掉。

    `min_chars` 给「文本」加一个**最小长度**（`minLength`，后端数的是字符数）。
    **只有定点修补这一路传它**（见 `patch_schema`）：修补在门禁与检查阶段跑、
    改完直接落盘，一个残句没有下一道工序替它兜。写作阶段那三路（正文／补字／
    压字）不传——它们的字数账是**总账**（本段配额），单句长短归门禁
    `line_length` 判、且两个方向都有处方。

    **不加最大长度**：实测后端对 `maxLength` 是**硬截断**（要求 40 字，返回恰好
    40 字符、末字是逗号，话说了半句，还多带一条「句尾不是终止标点」要修），对
    `minLength` 正常（56 字符的完整句，末字句号）。所以只上最小长度。
    """
    text = {"type": "string"}
    if min_chars and int(min_chars) > 0:
        text["minLength"] = int(min_chars)
    return {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "emotion": {"type": "string", "enum": list(vocab)},
            "absorb": {"type": "integer"},
            "text": text,
        },
        "required": ["index", "text"],
    }


def _insert_item_schema(vocab, min_chars=None):
    """定点修补的 insert 条目：after + speaker + emotion + text 全必填。

    **插入是唯一允许增行数的表达**（`absorb` 只许减行），且只为「承诺没闭合」
    开：要补的是一句后文的回应，它本来就不存在，没法靠改某一句话变出来。硬塞进
    已有句子只会把那句撑破，还会顺带撞上句长上限。

    `speaker` 必填且只能是 A 或 B：闭合不只是内容对上，还得说清**这句回应由谁
    来说**——只给一句没有说话人的话，等于没闭合。留空或写错人，读起来就成了
    另一个人自问自答。

    `after` 是插入锚点（插在第几句之后），必须是**被点名的句号**：不许在没被
    点名的地方凭空加话。

    `min_chars` 同 `_edit_item_schema`：只有定点修补这一路传（插进去的也必须是一句
    完整的话），写作阶段的补字路不传。
    """
    text = {"type": "string"}
    if min_chars and int(min_chars) > 0:
        text["minLength"] = int(min_chars)
    return {
        "type": "object",
        "properties": {
            "after": {"type": "integer", "minimum": 1},
            "speaker": {"type": "string", "enum": ["A", "B"]},
            "emotion": {"type": "string", "enum": list(vocab)},
            "text": text,
        },
        "required": ["after", "speaker", "emotion", "text"],
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
    """程序硬去重（**最后兜底**）：同一句长句在整篇里出现多次时，只留第一次。

    正路已经改掉了「删」：段内每一轮收口前查重、把后出现的副本**就地换成新内容**
    （见 `_replace_repeats`）——删会让字数塌下去，下一轮又补、补出来又是重复，没尽头。
    这一刀只留给替换也没修干净的情形，所以它不再是主力，而是兜底。

    但兜底也得有：这类重复不是「改写」而是「复播」，留着只会让成片多出一段时间在说
    同一件事，删掉没有任何信息损失（实测整篇生成 302 句里有 129 句是把自己的话又背
    了一遍）。

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


def find_repeats(script, low, high, min_chars=DEDUPE_MIN_CHARS):
    """查出本段里「与别处重复、且自己是后出现的那一处」的句号。

    判重范围是**整篇**，替换范围是**本段**——两件事必须分开：

    - 范围整篇：段是逐段写的，写本段时前面几段已经定稿在手里。「第 2 段把第 1 段
      的话又说了一遍」这种重复，只有把整篇摆在一起才看得见；只看本段，段与段之间
      的复读永远是隐形的。补字轮从前就吃这个亏——它手里只有本段正文。
    - 只报本段：前面几段这一轮不动（它们已经过了自己的轮次），本段才是可以改的
      那一段。名单里的句号都是全篇口径，与门禁报的句号同一套。

    每组重复只留**第一次出现**那一处，其后每一处都进名单：最早说过的那一句留着，
    换掉后来重复的那些。判重口径与整期兜底（`dedupe_script`）同一把尺——同一个
    `_dedupe_key`、同一个 `DEDUPE_MIN_CHARS`，两处不会一个判得出、一个判不出。

    返回 `{要换掉的句号: 它跟哪一句重复}`（键升序看得见）。空字典＝这一段没有复读。
    带上「跟哪一句重复」这一半，是因为提示词要说清「这一句跟第 N 句撞了」——只给一个
    句号，模型不知道该躲开哪件事。
    """
    seen, hits = {}, {}
    for i, item in enumerate(script or [], 1):
        key = _dedupe_key((item or {}).get("text"))
        if len(key) < min_chars:
            continue
        if key in seen:
            if low <= i <= high:
                hits[i] = seen[key]
            continue
        seen[key] = i
    return hits


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
       一刻（见 `generate` 与 `_generate_segmented`），而且它**之后不再跑任何
       格式工序**（v0.34.6 起只补时长）——模板里写什么标签，落到稿上就是什么。
    3. **没有可失败的判断**。句子是从模板逐字拼出来的，模型没参与，也就没有
       「它照没照做」可验。缺料各退一步：受众缺 → 那一小段消失；期标题缺 →
       「本期讲述…」整句不粘。粘出半句话比少粘一句坏得多。

    `log` 只用来把「哪一句因为缺料没粘」说出来，不影响结果。

    `review` 是**前期回顾**，一组已经填好文本的句子，由 `pipeline.review_rows`
    读上一期的标题、期主旨与前三段段主旨**整切三条**（上期《…》。／聊的是…。／
    讲了…等。），拼完不再按字数切——边界由模板结构定，不由长度定，见那里的说明。
    位置是**第 2 句**：紧跟片头第一句，在片头其余句与正文之前——它是
    「上期讲到哪儿」的交代，得在开场之后紧接着让听众听到，摆到正文后面就成尾声了。不分档位（标准档片头两句、简档一句，两种下回顾都落
    第 2 句）。这一层只管位置——开关开没开、上一期取不取得到，全是调用方的事，
    取不到就给空，这里当没有。回顾与片头尾同类：模型没参与，也就没有「照没照做」
    可验。

    **标签序列**（＝本函数产出的头尾固定结构；正文段仍是词表八词）：

    | 片头档位 | 回顾 | 序列 |
    |---|---|---|
    | 标准（两句） | 开 | 开场／回顾／承接／正文…／收束 |
    | 标准（两句） | 关 | 开场／承接／正文…／收束 |
    | 简档（一句） | 开 | 开场／回顾／正文…／收束 |
    | 简档（一句） | 关 | 开场／正文…／收束 |

    **开场只有一个**（片头第二句挂的是 `DISCOURSE_NEUTRAL`，不是第二个「开场」）。
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
    # 前期回顾站在**第 2 句**：紧跟片头第一句，插在片头其余句（标准档那句「承接」）
    # 之前。文本由调用方组好（`pipeline.review_rows`），这里不填值、不判开关、
    # 不做降级——粘合只负责位置。空列表＝这一期没有回顾，序列退回「片头＋正文＋
    # 片尾」。片头只有一句（简档）时无「其余句」可插，回顾照样接在第 1 句之后。
    mid = [dict(r) for r in (review or [])]
    if mid:
        head = head[:1] + mid + head[1:] if len(head) > 1 else head + mid
        log("前期回顾已粘上：%d 句，位置是第 2 句（片头第一句之后）" % len(mid))
    log("片头尾已粘上：正文 %d 句，首 %d 句（含片头与前期回顾）、尾 %d 句固定结构"
        % (len(body), len(head), len(tail)))
    # 到这里就结束了：**粘合之后只剩补时长这一件事**。
    # 从前这里跑的是整篇格式收束（`normalize_script`），顺手把时长量了；代价是
    # 格式规则也一并套到程序自己拼的片头尾上——语篇词表一抬手，模板里写的
    # 「开场 / 收束」就被洗成中性档，片头尾丢掉位置信息。所以粘合挪到收束**之后**，
    # 这里改成只补时长（正文句的时长在正文收束时已经量过，值一模一样）。
    # `head` 里已经含了前期回顾（插在第 2 句），所以这里只需头＋正文＋尾。
    return _fill_line_seconds(head + body + tail, cfg)


def _fill_line_seconds(lines, cfg):
    """给还没有时长的句子补上 `estimated_seconds`，**其它字段一个不动**。

    这是粘合之后唯一该做的事。片头尾与前期回顾是从模板逐字拼出来的新句子，
    末尾少一个 `estimated_seconds`——不补就是 0 秒，字幕时间轴会错位、总时长
    会少算。正文句在正文收束（`normalize_script`）时已经量过，这里判为「有」
    就原样留着；补的时候与那一遍同一个口径（`duration_model.estimate_line`），
    不接音色标定——脚本阶段的时长一律按标准语速估。

    判据是「没有时长或为 0」：正常量出来最小也是 1 秒，0 只可能是没量过。
    这里**不做**任何格式收束：speaker / emotion / text 都是模板与正文里已经规范
    过的形态，跑一遍收束只会把表外的片头尾标签洗成中性档（v0.34.6 前就是这个
    毛病）。
    """
    out = []
    for item in lines or []:
        row = dict(item)
        if not row.get("estimated_seconds"):
            row["estimated_seconds"] = int(round(
                duration_model.estimate_line(row.get("text", ""),
                                             row.get("speaker", "A"), cfg)))
        out.append(row)
    return out


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
#: 紧挨终止标点前面**不许**出现的字符：花括号、方括号、尖括号、反斜杠这类
#: 「给眼睛看的」结构符号，念不出来。半角全角都算。
#: **故意不含 `%` `~` `@`**：「占比 15%。」是合法写法，加进去天天误报。
END_PUNCT_JUNK = "{}[]<>\\｛｝［］＜＞"


def end_punct_tail(text):
    """句尾不是一个像样的终止标点时，返回惹事的那个字符；合规返回 None。

    判两步，**两步都只看末位**：

    1. 末字符须是终止标点。这一步盖住两种坏形：完全没标点（收在汉字/字母上）、
       拿逗号顿号这类句中标点收尾。
    2. **紧挨终止标点前面那一个字符**不许是结构符号（`END_PUNCT_JUNK`）。这一步
       堵的是「补个标点就洗白」：`…{` 被第 1 步抓住、模型照处方补成 `…{。`，
       末字符成了 `。`，第 1 步就过了——那个 `{` 只是从末位挪到倒数第二位，
       照样念不出来，而它是唯一会被拦的机会。

    判据用**黑名单**而不是白名单（「标点前面必须是中/英/数字」）：叠标点是合法的
    中文写法，白名单会误伤「真的吗？！」（末字符 `！`，前一位是 `？`）和
    「占比 15%。」（前一位是 `%`）。

    不判句中：符号落在句子**中间**（既不在末位、也不在末位前一位）本函数管不着，
    这是已知漏点（这条路一直做的是句末）。
    """
    t = str(text or "").strip().rstrip(END_PUNCT_CLOSERS)
    if not t:
        return None
    if t[-1] not in END_PUNCT:
        return t[-1]
    if len(t) >= 2 and t[-2] in END_PUNCT_JUNK:
        return t[-2]
    return None


def gate_generate(script, cfg, paradigm=None, extra_tags=()):
    """生成阶段门禁（GATE_SPEC.stage == generate）。

    `paradigm` 是范式卡（`resolve_paradigm()` 的产物），连续句数上限取自它。
    与写脚本的提示词取同一个数——提示词说可以连说三句、门禁按两句卡，
    稿子会陷在「改了还是不过」的死循环里。

    `extra_tags` 是**只给重判留的口子**，默认空＝与从前一字不差。页面重判读的是
    落盘的成品稿，里面已经有程序粘上去的片头／回顾／片尾，标签是 `PROGRAM_ONLY_TAGS`
    那三个词、不在语篇词表内，不放行就会在页面上被报成「词表外标签」。生成侧一律
    不传——正文能写出的标签只有词表那八个（schema 枚举就是它），所以这个口子不会让
    正文的门槛变松。

    不管传没传 `extra_tags`，**粘合句都不进逐句判据**（见函数内 `glue_lines`）：
    它们是程序逐字拼的固定结构，模型没参与、也改不动。

    不接 calib：脚本阶段的时长一律按标准语速估，与音色无关。
    """
    items = []
    lo = int(cfg.get("gate.min_chars", 8))
    hi = int(cfg.get("gate.max_chars", 40))
    max_sec = float(cfg.get("gate.max_seconds_per_line", 15))

    # **程序粘合句**（片头／前期回顾／片尾）：标签取自 `PROGRAM_ONLY_TAGS`，由
    # `glue_intro_outro` 逐字粘上，模型没参与。它们**不进任何逐句判据**——粘合是
    # 纯字面拼接，没有「它照没照做」可验；判出来也修不了（定点修补是让模型改句子，
    # 改完就不是固定结构了）。生成过程中门禁看到的稿子本来就没有它们（粘合发生在
    # 门禁与修补**之后**），这条只在**重判**（页面复核盘上成品）时起作用。
    # 拿标签认粘合句不会误伤正文：正文侧产不出这三个词（schema 枚举锁死词表八词、
    # 解析层也校验词表）。**整篇量照算**（句数、总时长）——粘合句确实存在、要念。
    glue_lines = {i + 1 for i, s in enumerate(script or [])
                  if s.get("emotion") in PROGRAM_ONLY_TAGS}

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
        "共 %d 句" % len(script)
        + ("（其中 %d 句是程序粘合的片头／回顾／片尾，逐句判据已跳过）"
           % len(glue_lines) if glue_lines else ""))

    missing = [i + 1 for i, s in enumerate(script)
               if not s.get("text") or not s.get("speaker")
               or not s.get("emotion")]
    add("fields_complete", not missing,
        "缺字段句子：%s" % missing if missing else "全部齐备")

    # 语篇词表：emotion 只认 DISCOURSE_ORDER 里的八个词（`vocab_words()`）。
    # 枚举层已经拦了绝大多数越界，这里兜的是降级路（无约束解码）的漏网。
    # `extra_tags` 见函数说明：只给重判，用来认程序粘上去的片头尾标签。
    vocab = vocab_words()
    allowed = set(vocab) | set(extra_tags or ())
    off_vocab = [i + 1 for i, s in enumerate(script)
                 if i + 1 not in glue_lines and s.get("emotion") not in allowed]
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
    # 粘合句**既不计入连说账、也当分隔符**：它不是模型写的，拿它当连说的一环等于
    # 替模型背一个它没做的错；而它把前后两段正文分开，本来就该各算各的。做法是先把
    # 稿子按粘合句切成若干「正文块」，每块内部各扫各的连续段，报的仍是全篇句号。
    blocks, cur = [], []
    for i, s in enumerate(script, 1):
        if i in glue_lines:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cur.append(i)
    if cur:
        blocks.append(cur)
    runs, run_lines = [], []
    for blk in blocks:
        start = 0
        for k in range(1, len(blk) + 1):
            if (k < len(blk)
                    and script[blk[k] - 1]["speaker"]
                    == script[blk[start] - 1]["speaker"]):
                continue
            who = script[blk[start] - 1]["speaker"]
            cap = caps.get(who, 1)
            if k - start > cap:
                lines = blk[start:k]
                runs.append({"speaker": who, "lines": lines, "cap": cap})
                run_lines.extend(lines)
            start = k
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
                 for i, s in enumerate(script)
                 if i + 1 not in glue_lines and len(s["text"]) < lo]
    too_long = [{"line": i + 1, "chars": len(s["text"])}
                for i, s in enumerate(script)
                if i + 1 not in glue_lines and len(s["text"]) > hi]
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
            if i + 1 not in glue_lines
            and duration_model.estimate_line(s["text"], s["speaker"], cfg) > max_sec]
    add("line_duration", not over,
        "超时句：%s" % over[:6] if over else "全部 ≤ %.1f 秒" % max_sec,
        lines=over)

    hits = []
    for i, s in enumerate(script):
        if i + 1 in glue_lines:
            continue
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
        if i + 1 in glue_lines:
            continue
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
    # 两种坏形分开报（`kind`），回灌文案才能对症：
    #   missing — 末位根本不是终止标点，该补一个；
    #   junk    — 末位是终止标点，可紧挨它前面是个念不出来的结构符号，该去掉。
    # 合成一种报法，模型只会照着补标点，那个符号就从末位挪到倒数第二位了事。
    bad_punct = []
    for i, s in enumerate(script):
        if i + 1 in glue_lines:
            continue
        text = str(s.get("text") or "")
        tail = end_punct_tail(text)
        if tail is None:
            continue
        body = text.strip().rstrip(END_PUNCT_CLOSERS)
        kind = "junk" if (body and body[-1] in END_PUNCT) else "missing"
        bad_punct.append({"line": i + 1, "tail": tail, "kind": kind,
                          "sample": text[-12:]})
    add("line_end_punct", not bad_punct,
        ("命中 %d 处：%s"
         % (len(bad_punct),
            "、".join(("第 %d 句末尾混进念不出来的「%s」" if h["kind"] == "junk"
                       else "第 %d 句没有终止标点（以「%s」收尾）")
                      % (h["line"], h["tail"]) for h in bad_punct))) if bad_punct
        else "全部以终止标点收尾",
        hits=bad_punct)

    # 这里本来有一条 intro_outro：判首句像不像片头、末句像不像片尾。撤掉了——
    # 片头尾现在由程序在**整期定稿那一刻**逐字粘上去（`glue_intro_outro`），生成
    # 过程里门禁看到的稿子根本没有它们，判它只会恒为假，然后把一句模型没写过的
    # 句子交给它去改。粘合是纯字面拼接，没有可失败的实验，也就不需要「验」。
    # 重判（页面复核盘上成品）时它们确实在稿子里，但已由上面 `glue_lines` 那道
    # 豁免挡在逐句判据之外——两侧口径一致：粘合句不进任何逐句判据。

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


#: 内容检单项的输出契约。七条提示词里**只有这一条要求复杂结构**，从前也只有它把
#: 输出形状全押在提示词的一段叮嘱上。而定点修补完全依赖它给出句号——劝不住的代价
#: 是那一轮定不了点、只能交人工。所以照样上约束解码：能定点的前提不是「求模型给
#: 落点」，是**结构上必须给**。
#:
#: 两项的**问题项键不一样**：语义检答「这一句有什么毛病」，承诺链检答「承诺是什么 /
#: 怎么才算闭合 / 闭合位置在哪 / 这句闭合的话由谁来说」。共用一套键会逼承诺检去填
#: 它答不了的字段，模型只会把 problem 写成一句空话。
#:
#: `line` 的取值下限写死 1：0 与越界在下游等于「没得修」（从前它们会被归成
#: 「交人工」，而那一类会让循环当场早退、一次都不试）。探针实测本机后端认
#: `minimum`，所以这是**硬约束**；程序侧另有 `_locate_line` 的 quote 反查兜底。
_CHECK6_SEMANTIC_ITEM = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer", "minimum": 1},
                    "quote": {"type": "string"},
                    "problem": {"type": "string"},
                },
                "required": ["line", "quote", "problem"],
            },
        },
    },
    "required": ["pass", "issues"],
}

#: 承诺链检：`line` 是提出承诺那一句；`close_after` 是**建议插在第几句之后**；
#: `speaker` 只能填 A 或 B（脚本里的说话人代号，六种对话形式两侧都有数）。
_CHECK6_PROMISE_ITEM = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer", "minimum": 1},
                    "promise": {"type": "string"},
                    "how": {"type": "string"},
                    "speaker": {"type": "string", "enum": ["A", "B"]},
                    "close_after": {"type": "integer", "minimum": 1},
                },
                "required": ["line", "promise", "how", "speaker",
                             "close_after"],
            },
        },
    },
    "required": ["pass", "issues"],
}


def check6_schema(dims=None, total=0):
    """内容检的输出契约。

    `dims` 限定只判某几项时，`required` 跟着缩到那几项——约束解码按 schema
    剪裁输出，schema 里还留着这一轮不判的键，模型就得给它压根没判过的项下结论。

    `total` 是这份稿子有多少句，用来给问题项的 `line` 封顶（`maximum`）：句号
    越界与句号缺失一样定不了点。不传就不封顶（只验结构的调用方）。
    """
    dims = tuple(dims) if dims else tuple(k for k, _ in CHECK6_DIMS)
    props = {}
    for k, _ in CHECK6_DIMS:
        if k not in dims:
            continue
        spec = copy.deepcopy(_CHECK6_SEMANTIC_ITEM if k == "semantic"
                             else _CHECK6_PROMISE_ITEM)
        if total:
            spec["properties"]["issues"]["items"]["properties"]["line"][
                "maximum"] = int(total)
        props[k] = spec
    return {"type": "object", "properties": props, "required": list(props)}


CHECK6_SYSTEM = """你是播客脚本的审校者，回答只输出 JSON，不要任何其它文字。

输出格式（两个键都要给出，缺一不可）：
{"semantic": {"pass": true, "issues": []},
 "promise": {"pass": true, "issues": []}}

两块的问题项**形状不一样**，别串用。

语义检的问题项带三个键：
{"line": 12, "quote": "那一句里的原话片段", "problem": "一句话说清哪里不对"}

承诺链检的问题项带五个键，缺一不可：
{"line": 8, "promise": "开头许了什么", "how": "后文回应到什么程度才算闭合",
 "speaker": "A", "close_after": 20}

- line：出问题的是第几句（从 1 数起，不能超过本文的总句数）。一条问题跨了好几句
  时，填**其中最该改的那一句**——落点必须给得出：给不出，就没有人改得动它。
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
  判断依据是那件事有没有被回答，不是字面有没有重现。

判为没回应的，一次把「怎么补」答全，五个键缺一不可：
- line：填**提出那个承诺的句子**。
- promise：这个承诺**是什么**——一句话说清它许了听众什么。
- how：**怎么才算闭合**——后文要回应到什么程度才算数。不许写成「应予以回应」
  这类空话，要具体到该回应哪一件事。
- speaker：**这句闭合的话由谁来说**，只能填 A 或 B。按这一处的接续关系定：
  通常由另一方来接，同一个人把自己的话接着说完也成立。填了谁，这句就由谁念。
- close_after：**建议闭合位置**——这句话插在第几句之后。选一个接得上、读下来
  不突兀的位置。
- **不许把承诺删掉或改没**：闭合靠补上回应，不是靠取消承诺。

没有任何问题时 issues 是空数组。"""


def _locate_line(node, total, script=None):
    """定这一条问题落在第几句。

    先信模型给的 `line`；它取不出、或不在 1..total 之内时，**拿 `quote` 去稿子里
    反查**——问题本来就带着那一句的原话，句号是能算出来的（字符串比对，不花模型
    调用）。从前这里一律归 0，而 0 在下游等于「没得修」。

    绝不拿「最接近的一句」顶替：填了假句号，改稿的人会去改一句没毛病的台词，
    真正的问题原封不动，下一轮又原样报一遍。
    """
    try:
        line = int(node.get("line"))
    except (TypeError, ValueError):
        line = 0
    if 1 <= line <= total:
        return line
    quote = re.sub(r"\s+", "", str(node.get("quote") or ""))
    if quote and script:
        texts = [re.sub(r"\s+", "", s.get("text") or "") for s in script]
        for probe in (quote, quote[:12]):
            if len(probe) < 4:
                continue
            for i, t in enumerate(texts):
                if probe in t:
                    return i + 1
    return 0


def _norm_issues(node, total, script=None):
    """把模型给的问题清单归一化成带落点的结构。

    落点由 `_locate_line` 定：模型给了有效句号就用它，给不出就拿 `quote` 反查。
    两样都落空的才留 0，由 `patch_targets` 记「未修好」——**不早退、不重写**。

    两项的输出契约**本来就不一样**（见 `_CHECK6_SEMANTIC_ITEM` / `_CHECK6_PROMISE_ITEM`）：
    语义检答「哪一句有什么毛病」，字段是 `quote` + `problem`；承诺链答「开了什么口子 /
    怎么才算合上 / 谁来说 / 合在第几句之后」，字段是 `promise` + `how` + `speaker` +
    `close_after`。归一化的职责是**把两种形状映射到同一条记录上**，不是拿语义检的字段表
    去判承诺链的死活——「没有 quote / problem 就当这条没说过」会让承诺链报的每一条问题
    都在这里被丢掉，只剩一个判不通过、却指不出任何一处的空壳，下游无从修起。
    所以说明性字段按两种契约取齐：`quote ← quote|promise`，
    `problem ← problem|how|promise|text`；五键原样带下去。
    """
    out = []
    for it in (node.get("issues") or []):
        if isinstance(it, dict):
            line = _locate_line(it, total, script)
            quote = str(it.get("quote") or it.get("promise") or "").strip()
            problem = str(it.get("problem") or it.get("how")
                          or it.get("promise") or it.get("text")
                          or "").strip()
            rec = {"line": line, "quote": quote, "problem": problem}
            # 承诺项多出来的键原样带下去：定点修补要拿 promise / how /
            # speaker / close_after 去补那句回应，在这里压成三键等于把
            # 「补什么、谁来补、补在哪」在出口处丢掉，下游只能靠猜。
            for k in ("promise", "how", "speaker", "close_after"):
                v = it.get(k)
                if v not in (None, ""):
                    rec[k] = v
        else:
            # 旧形态（一句人话）：没有句号。兼容它不为好看，
            # 是为了后端降级、模型不守格式时不要整项变成「解析失败」。
            rec = {"line": 0, "quote": "", "problem": str(it).strip()}
        if not rec["problem"] and not rec["quote"]:
            continue
        out.append(rec)
    return out


def _issues_detail(issues, state="pass"):
    """给人看的那一行。句号缺失时明说「未指出句号」，不假装知道是哪一句。

    `state` 决定**空清单**怎么念：判通过时它就是「通过」；判不通过时它是
    「一条也没报出来」。后者若照旧印「通过」，条面上就是结论不通过、详情通过——
    一句话自相矛盾。报告是给人看的，结论与详情必须同源。
    """
    if not issues:
        return "通过" if state == "pass" else "未通过（模型未指出具体句子）"
    parts = []
    for it in issues[:4]:
        where = "第 %d 句" % it["line"] if it["line"] else "未指出句号"
        parts.append("%s：%s" % (where, it["problem"] or it["quote"]))
    return "；".join(parts)


def _parse_check6(text, dims, labels, total, script=None):
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
        issues = _norm_issues(node, total, script)
        state = "pass" if bool(node.get("pass")) else "fail"
        out[key] = (state, _issues_detail(issues, state), issues)
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
                json_schema=check6_schema(dims, len(script)),
            )
        except Exception as e:                                   # noqa: BLE001
            results.append(_unjudged(str(e), dims))
            continue
        results.append(_parse_check6(raw, dims, labels, len(script), script))

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
    """门禁不过 → 写锁文件**留痕**（08a）。

    只剩产物阶段在用：脚本阶段不再写锁——那边不过只是「有问题的稿子」，
    稿子和结论都落了盘，人愿意带着问题出片是他的选择，没有拦的道理。
    产物阶段同理（v0.41.1 起）：产物与报告都落了盘，这一期照常出片、照常记账，
    锁只用来记「哪一期、因为什么没全过」，供 `--continue` 定位与事后排查。
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
        # 插入（承诺闭合专用）：**唯一允许增行**的表达，见 `_insert_item_schema`。
        "inserts": {
            "type": "array",
            "items": _insert_item_schema(DISCOURSE_ORDER),
        },
    },
    "required": ["edits"],
}


def patch_schema(vocab=None, cfg=None):
    """定点补丁的输出 schema。

    一条 edit 是 index / emotion(可选) / absorb(可选) / text；一条 insert 是
    after / speaker / emotion / text（承诺闭合专用，见 `_insert_item_schema`）。
    `vocab` 为本篇词表（`vocab_words()`），不传也按它开。

    `cfg` 用来给这里两个 text 字段取**最小长度**（`gate.min_chars`）：修补在门禁与
    检查阶段跑、改完直接落盘，一个残句没有下一道工序替它兜。**只加最小长度、不加
    最大长度**（后端对上限是硬截断），见 `_edit_item_schema`。
    """
    schema = copy.deepcopy(PATCH_SCHEMA)
    min_c = int((cfg or {}).get("gate.min_chars", 8))
    schema["properties"]["edits"]["items"] = _edit_item_schema(
        vocab or DISCOURSE_ORDER, min_chars=min_c)
    schema["properties"]["inserts"]["items"] = _insert_item_schema(
        vocab or DISCOURSE_ORDER, min_chars=min_c)
    return schema


def _replace_item_schema(vocab, min_chars=None, max_index=None):
    """就地替换的 edit 条目：index + text 必填，emotion 可选。

    与 `_edit_item_schema`（门禁那条路）只差一处，但很关键：**这里没有 `absorb`**。
    替换是「同一个位置换一件新内容」，行数不许变——结构里写不出并句，就不用靠提示词
    去劝，也不会出现「换着换着整段短了一截」这种没人管的账。

    `min_chars` 给 text 加最小长度：替换在段内轮次里落地、改完直接进下一轮核账，
    残句没有下一道工序替它兜。**不加最大长度**（后端对上限是硬截断，见
    `_edit_item_schema`）——上限由落地判据守（`apply_replace`：超了就拒，不是截断）。

    `max_index` 给句号一个硬上界（全篇句数）：约束解码实测吃硬约束，能少一轮
    「越界了重来」。
    """
    text = {"type": "string"}
    if min_chars and int(min_chars) > 0:
        text["minLength"] = int(min_chars)
    idx = {"type": "integer", "minimum": 1}
    if max_index:
        idx["maximum"] = int(max_index)
    return {
        "type": "object",
        "properties": {
            "index": idx,
            "emotion": {"type": "string", "enum": list(vocab)},
            "text": text,
        },
        "required": ["index", "text"],
    }


#: 就地替换的输出契约：只有 `edits`。结构里没有 inserts，所以「不许增行」不靠提示词
#: 劝；也没有 absorb，所以「不许减行」同样写不出来。
REPLACE_SCHEMA = {
    "type": "object",
    "properties": {"edits": {"type": "array", "items": {}}},
    "required": ["edits"],
}


def replace_schema(cfg=None, vocab=None, max_index=None):
    """就地替换的输出 schema。字数区间由落地判据守（`apply_replace`），这里只给
    `text` 一个最小长度——与定点修补同一条理由：落地即定稿，没有下一道工序兜残句。
    """
    schema = copy.deepcopy(REPLACE_SCHEMA)
    min_c = int((cfg or {}).get("gate.min_chars", 8))
    schema["properties"]["edits"]["items"] = _replace_item_schema(
        vocab or DISCOURSE_ORDER, min_chars=min_c, max_index=max_index)
    return schema


_PATCH_SYSTEM_TMPL = """你是播客脚本的定点修补者，回答只输出 JSON，不要任何其它文字。

输出格式：
{"edits": [{"index": 58, "text": "改好后的整句文本。"}]}
要并句时多带一个 absorb：
{"edits": [{"index": 58, "text": "并好的一整句文本。", "absorb": 2}]}
要补一句回应时改用 inserts：
{"inserts": [{"after": 20, "speaker": "A", "emotion": "总结",
              "text": "新加的那句回应。"}]}

铁律：
- 默认**只允许替换**被点名句子的内容，全篇行数一个字都不能变。有两处例外，都只在
  问题清单**明确点名**时才用：「连着说超限」→ 用 `absorb` 并句（减行）；
  「在第 N 句之后插入一句」→ 用 `inserts` 插一句（增行）。除此之外不许新增句子、
  不许凭空删句。
- 被点名「在第 N 句之后插入一句」的：用 inserts，一条就是一句新话，四个键都要给：
    {"after": 20, "speaker": "B", "emotion": "总结", "text": "回应那句话的整句。"}
  after 必须是问题清单点名的那一句；speaker 只能填 A 或 B（问题清单给了就照它填，
  没给就按这一处该谁接着谁说定——**不许留空**：没有说话人的一句，念出来不知道是
  谁在说）；emotion 从本篇词表中选一个合适的。**插入不是用来解决「连着说超限」的**
  （那是 absorb 的事），也不许在同一处连插好几句来凑。
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


def _reject_feedback(rejected):
    """被拒条目的说明：下一轮喂回去，让「重发同一批」从盲重摇变成有依据的重试。

    不把原因递回去，模型上一轮为什么栽（并句缩水、跨人并、并到篇外、越界）它就
    一无所知，只能再猜一次——实测里一次补丁烧掉十六分钟、整批作废、下一轮原样
    重发，那等于重摇一次骰子。
    """
    if not rejected:
        return ""
    rows = ["- 上一轮有 %d 条没落地，**这一轮别重犯**：" % len(rejected)]
    for r in rejected[:12]:
        rows.append("    第 %s 句：%s" % (r.get("index"), r.get("reason") or ""))
    if len(rejected) > 12:
        rows.append("    （另有 %d 条同类，按上面这些判据一并改）"
                    % (len(rejected) - 12))
    return "\n".join(rows)


def build_patch_prompt(script, targets, feedback, material="", window=2,
                       evidence=None, rejected_note=""):
    """拼定点修补的用户提示词。

    只给被点名句与它前后各 `window` 句，**不给全篇**。这不是省字那么简单：实测里
    把全篇 220 句一起递过去，模型三次里有两次直接不吐 JSON（被上下文带跑，改写起
    解释性文字）；只给窗口则三次全中。窗口取 2 也不是凭空定的——A/B 对话体的局部
    语境就是上下两句的事，再远的句子跟这一句的措辞没有关系。

    `material` 只在需要时才带：修「编造」这类内容问题，模型得知道素材支持什么；
    纯形式项（措辞、句长、单句时长）只跟这一句自己的字面有关，带上它就是白占上下文。

    `rejected_note` 是**上一轮没落地的那几条、以及为什么**（见 `_reject_feedback`）。
    只在重发时有值：这一轮的任务和上一轮往往一字不差，唯一的新信息就是「上次为什么
    不行」，不带上它，这次重发就是重摇骰子。

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
    if rejected_note:
        parts.append("【上一轮为什么没落地】（这是上次没能改上的条目和原因，"
                     "**按这些判据重做**；不在下面的条目里、也没被点名的句子一律不动）\n%s"
                     % rejected_note)
    parts.append("【相关段落】（被点名的句子，以及它们前后各 %d 句）\n%s"
                 % (window, "\n".join(rows)))
    parts.append("现在输出 JSON。")
    return "\n\n".join(parts)


def parse_patch(raw):
    """从模型输出里取出 edits 与 inserts。两样都空就抛 ScriptError。

    返回 `(edits, inserts)`：edits 是替换（行数不变，`absorb` 减行），inserts
    是插入（增行）。插入只有「承诺闭合」用得上，所以多数补丁的 inserts 是空数组。
    """
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
    if not isinstance(data, dict):
        raise ScriptError("补丁输出不是 JSON 对象。")
    edits = data.get("edits")
    inserts = data.get("inserts")
    edits = [] if edits is None else edits
    inserts = [] if inserts is None else inserts
    if not isinstance(edits, list) or not isinstance(inserts, list):
        raise ScriptError("补丁里的 edits / inserts 都必须是数组。")
    if not edits and not inserts:
        raise ScriptError("补丁是空的：edits 与 inserts 里一条改动都没有。")
    return edits, inserts


def absorb_span(was, cfg=None):
    """并句的字数可行域：返回 `(下限, 上限)`。

    **取值只有这一处**——判据（`apply_patch` 的保真校验）与处方（`patch_targets`
    写给模型的数字）都调它。两处各算一遍，改了一处就会变成「提示词按一个区间写、
    落地按另一个区间判」，模型照着提示词做还是被拒，而且看上去毫无异常。

    为什么不是一个单一的百分比：

    - 并句要删掉合并处的重复衔接（「对。」这类接话），所以允许比原来短——这是
      「七成」的来处；
    - 但**七成必须与「每句不超过单句上限」相容**，否则这一组永远无解。实测的形态
      正是：两句合计 60 字，按七成要 42 字，而上限是 40 字——模型怎么写都被拒，
      真机一整期一处都没落地（A 的上限只有 1 句，A 连说两句就得并成一句，最常见的
      恰恰是这种情形）。

    所以区间按两步定：

    1. **常规**：`[单句上限 × 0.7, 单句上限]`（本机 28~40）。上限恒 ≤ 单句上限，
       并出来的句子不会再被句长门禁打回，不会「并一次、报一次、再精简一次」。
    2. **原句合计装不下这个下限时**（合计比下限还短）：退成 `[合计 × 0.7, 合计]`
       ——合并两句总共才 20 字的短句，不该被要求写出 28 字。

    再兜两处边界：下限不低于单句下限；原句合计连单句下限都不到时（两句「对。」），
    上限抬到下限，**保证区间恒非空**——空的区间等于把这一组判死。
    """
    cfg = cfg or {}
    hi = int(cfg.get("gate.max_chars", 40))
    lo = int(round(hi * 0.7))
    if was < lo:
        lo, hi = int(round(was * 0.7)), int(was)
    lo = max(lo, int(cfg.get("gate.min_chars", 8)))
    hi = max(hi, lo)
    return lo, hi


def replace_span(was, cfg=None):
    """就地替换的字数可行域：返回 `(下限, 上限)`。

    **取值只有这一处**——落地判据（`apply_replace`）与提示词里写给模型的那个
    `%d~%d` 都调它。两处各算一遍，就会出现「提示词按一个区间写、落地按另一个区间
    判」，模型照着做还是被判没落地，还看不出原因。

    算法与并句（`absorb_span`）同族，只差**上限**那一层：并句在「原句合计很短」时
    会把上限收到合计（合并两句总共才 20 字，不该被要求写出 28 字）；替换不设这一层
    ——以句换句，新句比原来那句长是好事，没必要封顶，只要不超单句上限。

    下限取「原句的七成」且**封顶到「单句上限的七成」**：原句本身就是个正常句子
    （≤上限），所以这个区间恒非空。为什么不是「逐字不低于原句」——那等于要求一个字
    都不能少，模型少写两个字就被拒，落地率会掉得跟从前并句一样惨（那条路就是被
    过严的判据卡死过一整期的）。
    """
    cfg = cfg or {}
    hi = int(cfg.get("gate.max_chars", 40))
    lo = min(int(round(was * 0.7)), int(round(hi * 0.7)))
    lo = max(lo, int(cfg.get("gate.min_chars", 8)))
    return min(lo, hi), hi


def apply_patch(script, edits, targets, inserts=None, cfg=None):
    """把补丁落回原稿。默认只换字段、行数不变；**并句与插入是两个例外**。

    行数不变是这条路的根基：门禁报的「第 N 句」在补丁前后指的是同一行。两个例外
    各有各的用途，都不是「随便增删」的口子：
    - `absorb` 并句（**减行**）：只给「同一人连着说超限」，见 `_edit_item_schema`；
    - `inserts` 插入（**增行**）：只给「承诺没闭合」，见 `_insert_item_schema`。

    所以这里按**补丁前那一版的句号**逐行重建：被 `absorb` 并掉的句号整行不出现，
    插入落在锚句之后，`targets` 里的「第 N 句」全程指补丁前那一句，编号不会漂。

    **只许改被点名的句。** 越界一律报错，不静默丢弃：模型顺手改了别的句子，等于把
    没毛病的地方重新摇一次骰子，而这正是这条路要根除的东西；悄悄放行它，下一轮
    若因此冒出新毛病，要跨过整整一个流程才查得到根源。

    并句另有三条硬校验，缺一条这一组就不落地：被并的每一句都得在点名清单里
    （没点名的说明它自己没毛病）、都得与并进的那句**同一个人**说的（跨人并就是把
    一个人的话塞进另一个人嘴里）、并出来的字数得落在**可行区间**内（见
    `absorb_span`——区间与「每句不超过单句上限」这条硬规定相容，从前那个「不低于
    合计七成」的单一阈值在上限之下常常根本无解）。

    **单条不合格只拒该条，其余照落**（返回 `(稿子, rejected)`）。从前是「全有或
    全无」：一次点名四十多句，只要一条过不了校验，整份补丁作废、稿子一个字不动，
    另外四十条白写——实测里一轮就这样白烧了十六分钟。现在分两类：

    - **结构性**（条目不是对象、句号不是整数）→ 仍抛 `ScriptError`，整份退：
      这种输出从根上不能用；
    - **单条不合格**（越界、没点名、空文本、并句三条硬校验、两条互相冲突）
      → **拒该条、记下原因**，其余照常落地。`rejected` 是 `[{index, reason}]`，
      调用方拿它写日志，并在下一轮重发时喂回给模型——「重发同一批」从此是有
      依据的重试，不是重摇骰子。

    并句与冲突都是**成组**的：第 N 句并掉紧随的 M 句是一个整体，这一组里任何一条
    不合格，整组一起拒（落一半等于把一段话截断）；某句被 A 条并走、又被 B 条单独
    改，**涉及的两个条目一起拒**。
    """
    total = len(script)
    target_set = set(int(x) for x in targets)
    heads, swallowed, texts = {}, {}, {}
    rejected, ins_after = [], {}

    def _drop_group(head):
        """把已经接收的这一组（head 及它并掉的尾句）整组撤回。"""
        for k, h in list(swallowed.items()):
            if h == head:
                del swallowed[k]
        heads.pop(head, None)
        texts.pop(head, None)

    # 结构性校验走在前面：过了这一关，后面才敢直接读 e["index"]。这一类错误整份退
    # ——条目的骨架都不成形，谈不上「其余照落」。
    for e in (edits or []):
        if not isinstance(e, dict):
            raise ScriptError("补丁里有不是对象的条目：%r" % (e,))
        idx = e.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ScriptError("补丁里的句号不是整数：%r" % (idx,))
    for ins in (inserts or []):
        if not isinstance(ins, dict):
            raise ScriptError("补丁的 inserts 里有不是对象的条目：%r" % (ins,))
        after = ins.get("after")
        if isinstance(after, bool) or not isinstance(after, int):
            raise ScriptError("inserts 里的 after 不是整数：%r" % (after,))

    for e in (edits or []):
        idx = int(e["index"])
        if not 1 <= idx <= total:
            rejected.append({"index": idx,
                             "reason": "句号越界（全篇共 %d 句）" % total})
            continue
        if idx in heads or idx in swallowed:
            # 同一句被两条同时动了：涉及的两个条目一起拒——留下任何一条，改出来的
            # 都不是模型想表达的那一版。
            if idx in swallowed:
                other = swallowed[idx]
                _drop_group(other)
                rejected.append({"index": other,
                                 "reason": "它并掉的第 %d 句又被另一条单独动了，"
                                           "两条一起拒" % idx})
            else:
                _drop_group(idx)
                rejected.append({"index": idx,
                                 "reason": "它和另一条动了同一句，两条一起拒"})
            rejected.append({"index": idx,
                             "reason": "第 %d 句在同一份补丁里被动了两次" % idx})
            continue
        if idx not in target_set:
            rejected.append({"index": idx,
                             "reason": "这一句没被点名——定点修补只动被点名的句子，"
                                       "其余一律照抄"})
            continue
        text = re.sub(r"\s+", "", str(e.get("text") or "").strip())
        if not text:
            rejected.append({"index": idx, "reason": "这一条把整句改成了空文本"})
            continue

        raw_absorb = e.get("absorb")
        absorb = 0 if raw_absorb is None else raw_absorb
        if isinstance(absorb, bool) or not isinstance(absorb, int) or absorb < 0:
            rejected.append({"index": idx,
                             "reason": "absorb 只许是正整数（并掉紧随其后的几句）："
                                       "%r" % (raw_absorb,)})
            continue
        tail = [idx + k for k in range(1, absorb + 1)]
        if tail and tail[-1] > total:
            rejected.append({"index": idx,
                             "reason": "要并掉 %d 句，已经并到篇外（全篇共 %d 句）"
                                       % (absorb, total)})
            continue
        bad = ""
        for k in tail:
            if k in heads or k in swallowed:
                # 这一句已经被另一条动过：两条一起拒（同上）。
                other = k if k in heads else swallowed[k]
                _drop_group(other)
                rejected.append({"index": other,
                                 "reason": "它动过的第 %d 句又要被并进第 %d 句，"
                                           "两条一起拒" % (k, idx)})
                bad = "第 %d 句已被另一条动过" % k
                break
            if k not in target_set:
                bad = ("第 %d 句没被点名——门禁没点它就说明它自己没毛病，并句只许"
                       "并被点名的那一段" % k)
                break
            if script[k - 1]["speaker"] != script[idx - 1]["speaker"]:
                bad = ("第 %d 句不是同一个人说的（%s / %s）——并句只许并同一人紧接着"
                       "的句子"
                       % (k, script[idx - 1]["speaker"], script[k - 1]["speaker"]))
                break
        if bad:
            # 并句是成组的：这一组里任何一条不合格，**整组拒**——落一半等于把
            # 一段话截断。
            rejected.append({"index": idx,
                             "reason": "这一组（第 %d 句并掉紧随的 %d 句）整组没"
                                       "落地：%s" % (idx, absorb, bad)})
            continue
        if tail:
            was = sum(len(re.sub(r"\s+", "", script[x - 1]["text"] or ""))
                      for x in [idx] + tail)
            # 并后的字数区间由 `absorb_span` 一家给（判据与处方同源）：常规取
            # 「单句上限的七成 ~ 单句上限」，原句合计装不下这个下限时退成
            # 「合计的七成 ~ 合计」。上限恒 ≤ 单句上限，所以并出来的句子不会转身
            # 又被句长门禁点名——从前那版只判「小于合计七成即拒」，而合计超过
            # 57 字时七成已经越过 40 字上限，那一组从数学上就不可能落地。
            lo_c, hi_c = absorb_span(was, cfg)
            if not (lo_c <= len(text) <= hi_c):
                rejected.append({
                    "index": idx,
                    "reason": "并出来的 %d 字，不在这一组要求的 %d~%d 字之内——并句"
                              "只许删掉合并处的重复衔接，意思一个都不许少"
                              % (len(text), lo_c, hi_c)})
                continue
        heads[idx] = e
        texts[idx] = text
        for k in tail:
            swallowed[k] = idx

    # 插入（承诺闭合）：只许插在**被点名的锚句**之后。行数因此增加，其后所有编号
    # 后移——两段都在补丁之后重新判，会按新编号重新报，不会错位。
    # 说话人必填，且不许拿插入去解决「连说超限」：那是并句的事（见 absorb）。
    for ins in (inserts or []):
        after = int(ins["after"])
        if not 1 <= after <= total:
            rejected.append({"index": after,
                             "reason": "插入位置越界（全篇共 %d 句）" % total})
            continue
        if after not in target_set:
            rejected.append({"index": after,
                             "reason": "这个位置没被点名——插入只许落在被点名的位置"})
            continue
        if after in swallowed:
            rejected.append({"index": after,
                             "reason": "第 %d 句已被并走，不能再插在它后面" % after})
            continue
        speaker = str(ins.get("speaker") or "").strip().upper()
        if speaker not in ("A", "B"):
            rejected.append({"index": after,
                             "reason": "speaker 只许是 A 或 B：%r"
                                       % (ins.get("speaker"),)})
            continue
        emotion = ins.get("emotion")
        if emotion not in DISCOURSE_ORDER:
            rejected.append({"index": after,
                             "reason": "emotion 不在本篇词表内：%r" % (emotion,)})
            continue
        text = re.sub(r"\s+", "", str(ins.get("text") or "").strip())
        if not text:
            rejected.append({"index": after, "reason": "插入的句子是空文本"})
            continue
        ins_after.setdefault(after, []).append(
            {"speaker": speaker, "emotion": emotion, "text": text,
             "estimated_seconds": 0})

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
        # 插入的句子紧跟在锚句之后；同一锚点要插几句时按模型给的先后顺序排。
        out.extend(ins_after.get(i) or [])
    return out, rejected


def apply_replace(script, edits, targets, cfg=None):
    """就地替换：把点名的句子换成新内容，**行数不变**。

    这是段内「查重 → 替换」那一步的落地口，与门禁那条定点路（`apply_patch`）分开
    放：那条路还要管并句（减行）与插入（增行）两个例外，这条路只管一件事——**同一个
    位置换一件新的事**。重复了 30 句，不是删掉 30 句（删完字数掉下来、下一轮又去补、
    补出来又是重复，没尽头），而是把「后出现的那几处副本」就地换成新内容。

    四条判据，缺一即拒：

    1. **只动点名的句子**。点外一律拒——那几句没被查出重复，重写它们等于把没毛病的
       地方重新摇一次骰子。
    2. **换出来的新句不许与整篇任何其它一句重复**（就地再查一次重）。不查这一条，
       换出来的可能还是复读，这一轮白跑。这是本路的专项判据，`apply_patch` 没有。
    3. **字数守恒**：新句不低于原句的 **七成**（与并句同一把尺，见 `apply_patch` 的
       保真判），上限是单句上限。换短了 → 段字数塌 → 轮末核账又报缺口 → 下一轮又去
       补 → 又可能复读，正好绕回这条路要根除的东西。口径取「原句 × 0.7」且封顶到
       「单句上限 × 0.7」——原句本来就是个正常句子（≤上限），所以区间恒非空。
       这里**不用「逐字不低于」**：那等于要求一个字都不能少，模型少写两个字就被拒，
       落地率会掉到跟从前并句一样惨（并句那条就是被过严的判据卡死的）。
    4. 带 `emotion` 时必须在语篇词表内；不带就保留原标签。

    返回 (新脚本, 被拒条目列表)。`rejected` 的每条都带可回灌给模型的原因——下一轮
    重发同批补丁时，模型看见的是「为什么没落地」。
    """
    cfg = cfg or {}
    max_c = int(cfg.get("gate.max_chars", 40))
    vocab = list(vocab_words())
    target_set = set()
    for x in (targets or []):
        try:
            target_set.add(int(x))
        except (TypeError, ValueError):
            continue
    total = len(script or [])
    out = [dict(l) for l in (script or [])]
    done, rejected = set(), []

    for e in (edits or []):
        idx = e.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            rejected.append({"index": idx, "reason": "index 必须是整数句号"})
            continue
        if not 1 <= idx <= total:
            rejected.append({"index": idx,
                             "reason": "句号越界（全篇共 %d 句）" % total})
            continue
        if idx not in target_set:
            rejected.append({"index": idx,
                             "reason": "这一句没被点名——替换只动被点名的句子，"
                                       "其余一律照抄"})
            continue
        if idx in done:
            rejected.append({"index": idx,
                             "reason": "第 %d 句在同一份补丁里被动了两次" % idx})
            continue
        text = re.sub(r"\s+", "", str(e.get("text") or "").strip())
        if not text:
            rejected.append({"index": idx, "reason": "这一条把整句改成了空文本"})
            continue
        was = len(re.sub(r"\s+", "", str(script[idx - 1].get("text") or "")))
        # 字数区间与提示词里给模型的数字**同源**（`replace_span`）。
        floor, _ceil = replace_span(was, cfg)
        if len(text) < floor:
            rejected.append({
                "index": idx,
                "reason": "换出来的只有 %d 字、原来那一句 %d 字——替换是**同长度换"
                          "内容**，字数不许塌（塌下去这一段的账就少了，下一轮又得"
                          "补字）。请重写这一句：%d~%d 字"
                          % (len(text), was, floor, max_c)})
            continue
        if len(text) > max_c:
            rejected.append({"index": idx,
                             "reason": "换出来 %d 字、超过单句上限 %d 字：请压进 %d~%d 字"
                                       % (len(text), max_c, floor, max_c)})
            continue
        key = _dedupe_key(text)
        clash = 0
        if len(key) >= DEDUPE_MIN_CHARS:
            for j, item in enumerate(out, 1):
                if j != idx and _dedupe_key(item.get("text")) == key:
                    clash = j
                    break
        if clash:
            rejected.append({"index": idx,
                             "reason": "换出来的这一句跟第 %d 句重复了——换完还得是"
                                       "新内容，不是把复读挪到别处。请讲一件前文还"
                                       "没讲过的事" % clash})
            continue
        emotion = e.get("emotion")
        if emotion is not None:
            emotion = str(emotion).strip()
            if emotion and emotion not in vocab:
                rejected.append({"index": idx,
                                 "reason": "语篇标签 %r 不在本篇词表内（可用：%s）"
                                           % (emotion, "/".join(vocab))})
                continue
        row = dict(out[idx - 1])
        row["text"] = text
        if emotion:
            row["emotion"] = emotion
        out[idx - 1] = row
        done.add(idx)

    return out, rejected


def _needs_material(report, want=None):
    """这一轮要修的问题里有没有内容类的。

    内容类（语义检、承诺链检）的修法要看素材，才知道什么能说什么不能说。
    `want` 同 `patch_targets`：只看本次真的要修的那几项——检查段与门禁段各修各的，
    门禁段修形式项时把素材递进去纯属白占上下文。
    """
    for it in report.get("items") or []:
        if it.get("ok") or it.get("soft") or it.get("advisory"):
            continue
        key = it.get("key") or ""
        if want is not None and not want(key):
            continue
        if key.startswith("check_"):
            return True
    return False


def patch_targets(report, total, want=None, cfg=None):
    """把不通过项分成两类：能定点的、定不了点的。

    能定点的定义窄得只有一条：**问题自带句号**。形式门禁全都自带（它本来就是逐句
    判的）；内容检要模型自己给，给了才算；给不出就靠 `quote` 反查（见 `_norm_issues`）。

    两段（检查段、门禁段）**没有任何重写权限**：对一份可用稿子，动作只有「判」和
    「定点修」两件。结构坏了（稿子为空）是**生成步骤**的事，不由这里处置——从前
    这里把「结构坏了」归成「只能整篇重出」、把「指不出句号」归成「交人工」，而
    后者会让循环当场早退、一次都不试。两条路都不对：前者越权重写，后者等于不修。

    `want` 是**本次要修哪几项**的过滤器（`key -> bool`）。两段各自成环、各修各的
    判据：检查段只修内容项（`check_` 开头）、门禁段只修形式项——让一段去修另一段
    的判据，改完又没人复判，报告上的结论就跟稿子两张皮。不给（None）＝全都算，
    与从前一致。

    返回 (targets, unfixed)：
      targets — {句号: [问题说明]}，交给模型定点改
      unfixed — 定不了点、也没能反查回来的项：记一笔**不早退、不重写**，
                其余项照修
    """
    targets, unfixed = {}, []
    for it in report.get("items") or []:
        if it.get("ok") or it.get("soft") or it.get("advisory"):
            continue
        key = it.get("key") or ""
        if want is not None and not want(key):
            continue
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
            # 两种坏形两条处方：缺终止标点的补一个；结构符号混在末位的把它去掉。
            # 后者若也照「补标点」办，`…{` 会变成 `…{。`——末位一合规，那个符号
            # 就再没人查了。
            for h in it.get("hits") or []:
                tail = h.get("tail", "")
                if h.get("kind") == "junk":
                    targets.setdefault(int(h["line"]), []).append(
                        "句尾终止标点前面紧挨着念不出来的符号「%s」：把那个符号"
                        "从这一句里去掉（它不该被念进台词），终止标点留着。"
                        % tail)
                else:
                    targets.setdefault(int(h["line"]), []).append(
                        "句尾没有终止标点（以「%s」收尾）：按这句话的语义补一个"
                        "合适的句尾标点——疑问收？、感叹收！、陈述收。"
                        % tail)
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
        elif key == "check_semantic":
            located = False
            for iss in it.get("issues") or []:
                ln = int(iss.get("line") or 0)
                if 1 <= ln <= total:
                    located = True
                    # 方向必须给全，且措辞要和别的条目一样是「第 N 句：怎么改」。
                    # 实测里只把检查结论原样贴出来（「语义检：素材里没有这个数据」），
                    # 模型会把它当成一份报告而不是一条指令，连续两次都跳过这一句没改。
                    targets.setdefault(ln, []).append(
                        "语义检：%s。改成素材支持的说法，去掉素材里没有的事实、"
                        "数据或来源"
                        % (iss.get("problem") or "内容在素材里找不到依据"))
            if not located:
                unfixed.append(it)
        elif key == "check_promise":
            located = False
            for iss in it.get("issues") or []:
                ln = int(iss.get("line") or 0)
                if not (1 <= ln <= total):
                    continue
                located = True
                # 要补的是**后文那句回应**，报出来的却是开头提出承诺的那一句。
                # 所以这里点名两处：承诺句（明令不许删）＋建议闭合位置（插入锚点）。
                # 修法是 `inserts`（在锚句之后插一句），不是 `edits` 改哪一句——
                # 补回应本来就是一句新话，硬塞进已有句子只会把那句撑破。
                close_after = int(iss.get("close_after") or 0)
                if not (1 <= close_after <= total):
                    close_after = total      # 给不出位置就补在末尾，不至于没处落
                speaker = str(iss.get("speaker") or "").strip().upper()
                if speaker not in ("A", "B"):
                    speaker = ""             # 填错就当没给，由插入提示词按上下文定
                targets.setdefault(ln, []).append(
                    "第 %d 句开了口子、后文没有回应它（承诺：%s；回应到什么程度"
                    "才算闭合：%s）：**这一句不许删、也不许改小**。"
                    % (ln, iss.get("promise") or "开头提出的问题",
                       iss.get("how") or "把这件事讲清楚"))
                targets.setdefault(close_after, []).append(
                    "要把上面那个承诺回应上：**在第 %d 句之后插入一句**（用 inserts，"
                    "不是改这一句）%s。"
                    % (close_after,
                       "，由 %s 来说" % speaker if speaker else
                       "，由谁来说按这一处的接续关系定"))
            if not located:
                unfixed.append(it)
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
                # 并后的字数区间：与落地判据**同源**（`absorb_span`）。从前
                # 这条判据只写在程序里、提示词一个字都没提，模型不知道有这条规矩，
                # 只会按「合并＝精简」写——真机一整期 15 处一处都没落地。
                lo_c, hi_c = absorb_span(10 ** 9, cfg)   # 大数＝取常规区间
                tip = ("第 %d–%d 句都是 %s 说的，连着说了 %d 句，超过上限"
                       "（%s 最多连着说 %d 句）：把这 %d 句**并成 %d 句以内**。写法是把"
                       "并好的整段放进这一段的第一句、带上 absorb，absorb 是并掉的"
                       "句数（至少 %d）：{\"index\": %d, \"text\": \"并好的一整句。\", "
                       "\"absorb\": %d}。**说话人一个字都不许动**——换人是拿格式代替"
                       "语义。**并出来的字数必须落在 %d~%d 字之间**（这是硬判据："
                       "不在区间里这一组整组不落地；你并掉的那几句若加起来比 %d 字"
                       "还少，区间就退成「那几句合计的七成 ~ 合计」）；并句只删掉"
                       "合并处的重复衔接，意思一个都不许少。"
                       % (lines[0], lines[-1], who, len(lines), who, cap,
                          len(lines), cap, len(lines) - cap, lines[0],
                          len(lines) - cap, lo_c, hi_c, lo_c))
                for ln in lines:
                    targets.setdefault(ln, []).append(tip)
        elif key == "emotion_vocab":
            # 语篇词表自带句号、代码判的，跟措辞禁忌同一条路：定点改。
            # 换成词表里的哪一个由这句话的语义定（词表只有一份，见 `vocab_words`）。
            # 从前它掉进 `else` 被当成「交人工」——一个自己带句号的项，不该走那条路。
            for ln in it.get("lines") or []:
                targets.setdefault(int(ln), []).append(
                    "这句的语篇标签不在本篇词表内（可用：%s）：从词表里换一个"
                    "与这句话相称的——它在对话里干什么活就选哪个"
                    % "/".join(it.get("vocab") or vocab_words()))
        else:
            # 只可能是「稿子结构坏了」（json_valid / fields_complete）：那是**生成
            # 步骤**的问题。门禁段不重写，记一笔、不早退——轮次照走，其余项照修。
            unfixed.append(it)
    return targets, unfixed


def _find_item(report, key):
    for it in report.get("items") or []:
        if it.get("key") == key:
            return it
    return None


def dims_to_recheck(prev_report, dims=None):
    """这一轮改完，内容检哪几项需要重判。

    只重判**上一轮没过、或压根没判过**的那几项。上一轮已经判过、且判通过的那一项，
    本轮改的又是措辞、句长这类形的事，没有理由把同一段上下文再喂一遍——真出了问题，
    形式门禁那一轮已经先把它拦下了。贵的东西（走模型）按需跑，便宜的东西（走代码）
    每轮全跑，这是这一段的成本纪律。

    `dims` 是**本次该判的项**（承诺链检必判；语义检开着时才算）。不按它收窄的话，
    语义检一关掉，它永远是 `None`、每轮都被算成「要重判」，开关就白设了。
    """
    dims = tuple(dims) if dims else tuple(k for k, _ in CHECK6_DIMS)
    if not prev_report:
        return set(dims)
    need = set()
    for key in dims:
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

#: 段级参数的兜底默认。**每一根都能在配置页改**（`script.segment_*` 与
#: `script.plan_rounds`），常量只在那几个键缺席时顶上——一个数写死在源码里，
#: 人就调不动它。
SEGMENT_MIN_SENTS = 15       # 软句数下限：软引导的句数下限，也是碎段合并阈值的一因子
SEGMENT_TOL_FRAC = 0.15      # 段字数容差（比例）——与总时长门禁同一把尺
SEGMENT_FIX_ROUNDS = 5       # 单段超容差的修正轮上限（少了插入、多了压删，都不是重写）
SEGMENT_PARSE_RETRIES = 2    # 「输出坏了重发」的轮上限：整篇出货重试与段级解析重试共用
PLAN_RETRIES = 2             # 规划打回上限，仍不过就按「一节一段」程序硬分
SEGMENT_FIXED_CHARS = 400    # 段提示词固定文案的字符数（算素材额度用，量级即可）


def _tol_frac(cfg):
    """段字数容差的比例。配置里是**整数百分数**（15 就是 15%），常量是小数。"""
    v = (cfg or {}).get("script.segment_tol_frac")
    if v is None:
        return SEGMENT_TOL_FRAC
    return max(0.01, float(v) / 100.0)


def _segment_fix_rounds(cfg):
    """一段超容差后还能修几次（少了插入、多了压删，共用这一份预算）。"""
    return int((cfg or {}).get("script.segment_fix_rounds", SEGMENT_FIX_ROUNDS))


def _segment_parse_rounds(cfg):
    """输出坏了重发几次。全项目**只有这一个数**：整篇出货重试、段级解析重试共用。"""
    return int((cfg or {}).get("script.segment_parse_rounds",
                               SEGMENT_PARSE_RETRIES))


def _plan_rounds(cfg):
    """规划打回上限；仍不过就按各段首节的凝缩主旨代填。"""
    return int((cfg or {}).get("script.plan_rounds", PLAN_RETRIES))


def _quota_floor(cfg):
    """段配额的地板：**软句数下限 × 每句最少字**＝提示词开始说做不到的话的那条线。

    地板只管一件事：提示词里那句「约 N 句」不许与配额矛盾。「约 N 句」的算式带一个
    软句数下限（`script.segment_min_sents`，默认 15），所以配额低于「15 句 × 每句
    最少字（`gate.min_chars`，默认 8）」时，提示词会一边要它写 15 句、一边只给它不足
    120 字的额度——两句话打架，模型怎么答都是错。120 就是这条线，本机落在这个量级，
    正常一期碰不到。

    **不是碎段合并那把尺**（那一把是 `软句数下限 × 期望句长`，本机 360）。两把尺管
    两件事，值不同才对：

    - 碎段合并判「这一段的料够不够**单开一段**」——产能问题，尺要取**常态**值（模型
      写一句话的常态产量是期望句长），不够就并进邻居；
    - 地板判「摊到的配额有没有低到**提示词自相矛盾**」——尺只取到**下界**（每句最少
      字），越过这条线才拦。

    从前两处共用「× 期望句长」，等于拿产能尺当地板：配额是按料摊的（见
    `_section_quotas`），料少的地方配额本来就该少，抬到 360 就是给一部分段**偷偷改了
    压比**——同一期里邻居还按原压比写，这几段忽然要拿更少的料写更多的字。本机目标
    6585 字、地板 360 时，写作段数超过 18 个就会触发。
    """
    min_sents = int((cfg or {}).get("script.segment_min_sents", SEGMENT_MIN_SENTS))
    min_chars = int((cfg or {}).get("gate.min_chars", 8))
    return int(round(min_sents * min_chars))


def _segment_window(quota, cfg=None):
    """本段验收区间 (lower, upper)：配额 ± max(配额 × 容差比例, 地板 × 容差比例)。

    单一出口：核账判停（调用处 `tol`）与提示词里的【验收窗口】都从这里取数，
    两处永远一致——提示词给模型报的窗口若和程序实际收稿的窗口对不上，
    模型按假窗口收工就会被真窗口打回，白烧调用。

    **容差下限是地板的派生量**（`_quota_floor × 容差比例`，本机 120 × 15% = 18）。
    从前它是个独立常数（配置点 `script.segment_tol_min_chars`，本机 60），那个 60
    没有算式出处，还让「各段容差之和 = 全篇容差」这笔账对不上。改用地板的派生量后，
    全篇各段容差之和恰是 15% × 全篇配额（配额本身低于地板的段除外）。
    """
    frac = _tol_frac(cfg)
    tol = max(int(quota * frac), int(round(_quota_floor(cfg) * frac)))
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
    上限按本段配额现算，不写死：段字数容差是 ±`script.segment_tol_frac`、最短句是
    min_chars，那么任何「还在容差内」的输出都不可能超过
    `配额 × (1+容差) ÷ 最短句` 句。取这个数就永不误伤，而它只管防无限写——
    控长归字数核账，两件事不混。
    """
    min_c = int(cfg.get("gate.min_chars", 8))
    return max(1, int(math.ceil(float(quota) * (1.0 + _tol_frac(cfg))
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


def _groups_asis(secs, sec_chars, target_chars, groups=None, cfg=None):
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
    return _finish_quotas(out, cfg, target_chars)


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
    烧时间、多接缝，形态也难维持。

    阈值 = `script.segment_min_sents` × 期望句长，**全部现算不写死**：软句数
    下限与期望句长是它的两个因子，改配置它就跟着变。乘的是**期望句长**（模型写一
    句话的常态产量）——这把尺判的是「这一段的料够不够**单开一段**」，是**产能**
    判据，所以取常态值。从前这里多写了一句「这个阈值正好是引导不说谎的下限」，
    那是错的：引导不说谎的下限是乘**每句最少字**（15 × 8 = 120），不是乘期望句长
    （15 × 24 = 360）——差三倍。配额地板是另一把尺，见 `_quota_floor`。

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
        return _groups_asis(secs, sec_chars, target_chars, groups, cfg)

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
    return _finish_quotas(rows, cfg, target_chars)


def _finish_quotas(groups, cfg, target_chars):
    """段层补余数与地板：配额合计对着目标，且每段不低于「提示词不说谎」的下限。

    地板取 `_quota_floor`（软句数下限 × **每句最少字**）。配额是**按料摊的**
    （`_section_quotas`）：料多的地方多写、料少的地方少写，这就是对的结果，不需要
    谁去救。地板唯一要拦的是「配额低到提示词那句『约 N 句』自己打自己的脸」。所以
    这把尺比碎段合并那把（× 期望句长，本机 360）**小得多**（本机 120）——大了就不是
    拦，而是给一部分段偷偷改压比。

    抬地板会让合计**超过**目标（从前只补齐、没回头重算，是根不熔断的保险丝），
    所以抬完把超出部分从**最大的一段**扣回来；扣到它会低于地板就作罢——总数宁可
    贵一点，也不让某一段小到提示词说不出话。
    """
    if not groups:
        return groups
    floor = _quota_floor(cfg)
    diff = int(target_chars) - sum(g["quota"] for g in groups)
    k = max(range(len(groups)), key=lambda i: groups[i]["quota"])
    groups[k]["quota"] += diff
    for g in groups:
        g["quota"] = max(floor, int(g["quota"]))
    over = sum(g["quota"] for g in groups) - int(target_chars)
    if over > 0:
        k = max(range(len(groups)), key=lambda i: groups[i]["quota"])
        groups[k]["quota"] -= min(over, max(0, groups[k]["quota"] - floor))
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
        # 调用方没给配额（如只校验结构）：按「一个最小可写段」开刹车兜底。尺取
        # `_quota_floor`（软句数下限 × 每句最少字）——那正是「一段最少写多少」的
        # 定义。从前这里写 `_tol_min_chars(cfg) * 10`，那个 ×10 的倍数在代码与配置
        # 里都找不到出处，算出来的句数上限（本机 87 句）也跟真配额派生不出关系。
        quota = _quota_floor(cfg)
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
                         pieces=None, noted_fit=None, cfg=None):
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
    low, high = _segment_window(quota, cfg)
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
    lines.append("【验收窗口】本段按 %d~%d 字验收——把字数控制在这个区间之内。"
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


def _clip_script_rows(script, width=24):
    """把整篇正文压成「够认脸」的形态：每句只留前 `width` 字。

    给额度紧张时的替换提示词用。它的【已写脚本】块本该给全篇正文（主人定的规矩：
    「我要看到所有已写的」），但小额度后端装不下几千字的正文。压缩之后句号、说话人、
    语篇标签、句子大意都还在——够模型判断「这件事已经讲过了」；细节本来就该回
    【本段原文】里取，不靠这块。
    """
    rows = []
    for n, ln in enumerate(script or [], 1):
        text = re.sub(r"\s+", "", str(ln.get("text") or ""))
        if len(text) > width:
            text = text[:width] + "…"
        rows.append("%d. [%s·%s] %s" % (n, ln.get("speaker", ""),
                                        ln.get("emotion", ""), text))
    return "\n".join(rows)


def _insert_hard_cap(cfg, need):
    """插入条数的失控刹车：这次要补的字数，最多能由多少句凑出来。

    约束解码下模型能把数组无限写下去，写到上限才被语法强制收尾，所以必须有封顶。
    上限取 `要补的字数 × (1+容差) ÷ 最短句`：任何「还在容差内」的补法都不可能超过
    这么多条，取它永不误伤；它只管防无限写，控长归字数核账，两件事不混。
    """
    min_c = int((cfg or {}).get("gate.min_chars", 8))
    return max(1, int(math.ceil(abs(int(need)) * (1.0 + _tol_frac(cfg))
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


def _norepeat_block():
    """「禁止重复已写内容」的判据与正反例——补写与就地替换**共用同一份**。

    两处都要这条纪律：补写要它（补出来的必须是新东西），就地替换更要它（替换本来
    就是因为重复才发生的）。抄成两份就会出现一处改了、另一处还是旧的，模型在两个
    环节听到两种规矩。插入与替换各自特有的条条（只许新增、after 的语义、行数不变）
    不在这里面，留在各自的模板里。

    一份提示词只管它自己那一步的事：这块里没有字数、没有落点、没有流程。
    """
    return """- **禁止重复已有的脚本内容。** 判据看**意思**，不看措辞——换个词、换个
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
  重复，听到「新东西」就不是。**换个说法不改变这个判断。**"""


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
%s
- **每一句都得带进正文里还没有的东西**：新事实、新数字、新步骤、新角度都算。
  【素材】里有、而正文还没讲到的，优先补那些。一个信息点写成一句就够，最多写成
  一组一问一答（**一组，不是两组、也不是两问**），同一个点不许写两遍。
- 分几处插入时，**各处彼此也不许重复**，更不许跟别处已经补过的话重复；
  同一个人不许连着追问同一件事——那读起来像结巴。
- 各句字数合计要接近本次要补的字数，**不要明显超出**。**禁止重复已有的脚本内容**
  ——凑不够就换没讲到的点讲得更细（新数字、新步骤、新角度、另一条分支的情况），
  把一件事讲透，而不是把说过的话再摆一遍。复读不算补字：程序会把重复查出来、
  就地换掉，那一轮等于白补。
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
        _norepeat_block(),
        format_banned_rules())
    # 站位与对话形式与写脚本那一处同源：选了形式就顶掉卡上的站位说明。
    base += "\n\n【两位主持人】（既定阵容，不由你指定）\n" + _hosts_text(
        card, cfg, cfg.get("script.dialogue_form") or "")
    base += ("\n\n【风格倾向】（与本次补写的内容无关的通用背景；与上面的铁律"
             "冲突时以铁律为准）\n" + _style_block(preset))
    base += "\n\n" + READABLE_RULE
    return base


_REPLACE_SYSTEM_TMPL = """你是播客脚本的改写者，回答只输出 JSON，不要任何其它文字。

任务：名单里点名的句子**跟前面的内容重复了**。你要就地把它们换掉——**位置不动、
行数不动、说话人不动**，只把内容换成正文里还没讲过的东西。

输出格式：
{"edits": [{"index": 58, "text": "换好后的整句文本。"}]}

铁律：
- **只许改名单里点名的句子**。没被点名的句子一个字都不许动，也不许出现在 edits 里。
- **行数一个字都不能变**：不许新增句子、不许删句、不许把两句并成一句。一句话就是
  一条 edit，不许把一句拆成两条。
- 说话人不在你的职责内，**不要输出 speaker 这个键**——换人是拿格式代替语义，把 A 的
  一句话翻给 B，读起来就成了另一个人自问自答。
  **两人怎么接：%s**
  **连着说的上限：A 最多连着说 %d 句、B 最多连着说 %d 句**（上限不是目标，谁都不许超过它）。
  %s
- emotion：只有名单点名要改语篇标签时才带上；从本篇词表中选（词表只有这一份，
  不在此列的一律不许编）：
%s
- text：换好后的**完整整句**，不是片段；%d–%d 字。text 只装对话内容——语气/情绪
  描写（「小美疑惑地说」这类）、说话人标记、任何标签都不进 text，text 里的每个字
  都会被念出来。每句必须以标点符号收尾，符号按这句话的语义定：疑问收「？」、感叹
  收「！」、陈述收「。」——不许一律补句号应付。
- **先读名单再动笔**：名单里每一条都写明了「这一句跟第 N 句重复」。那件事已经讲过
  了，你要讲的是**另一件事**——回【本段原文】里找还没进过正文的点；挑不出新东西
  就换一条角度（补数字、补步骤、补反例、补另一条分支），而不是换个说法重说一遍。
%s
- 你换出来的句子**同样要过措辞禁忌**：
%s"""


def replace_system(card=None, cfg=None):
    """段内「查重 → 替换」那一步的系统提示词。

    与补写（`insert_system`）**共用同一份纪律**：两人怎么接、形状示范、连句上限、
    语篇词表、句长区间、措辞禁忌、主持人阵容、可朗读规则，连「禁止重复」那整套判据
    也是同一个 `_norepeat_block()`。差别只在**动作**——那边是「新增一句、已有句子
    一个字不许动」，这边是「原地换掉点名的那一句、行数不动」。各自的模板只写各自的
    动作，纪律一个字不另写：几份提示词各按一个口径要求，模型在不同轮次听到的就是
    两种规矩（插入这一处从前就是另写一份硬编码，人把上限调到 30、它还在按 40 补）。
    """
    cfg = cfg or {}
    card = card or {}
    preset = (PRESET_SPEC.get(cfg.get("script.style_preset", "argument"))
              or PRESET_SPEC["argument"])
    cap_a, cap_b = _run_caps(card, cfg)
    base = _REPLACE_SYSTEM_TMPL % (
        paradigms.rhythm_text(_form_key(card, cfg)), cap_a, cap_b,
        paradigms.example_block(_form_key(card, cfg), indent="  "),
        _vocab_block(),
        int(cfg.get("gate.min_chars", 8)), int(cfg.get("gate.max_chars", 40)),
        _norepeat_block(),
        format_banned_rules())
    base += "\n\n【两位主持人】（既定阵容，不由你指定）\n" + _hosts_text(
        card, cfg, cfg.get("script.dialogue_form") or "")
    base += ("\n\n【风格倾向】（与本次替换的内容无关的通用背景；与上面的铁律"
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
- %s
- 你改出来的句子**同样要过措辞禁忌**：
%s"""


def trim_system(cfg=None, low=0, high=0):
    """段内压字数的系统提示词。

    句长区间同 `patch_system`：从配置读。压紧那一步最容易把人调的窄区间顶破
    ——它按「压到 8–40 里」减，减完落在 35 字，而人把上限设成了 30，一次
    调用白烧。

    `low` / `high` 是本段的验收区间（`_segment_window` 算的）。把两个数摆出来，
    「减到差不多就停」才有一个能照着做的意思——含糊的「差不多」等于没给标准。
    """
    lo = int((cfg or {}).get("gate.min_chars", 8))
    hi = int((cfg or {}).get("gate.max_chars", 40))
    if low and high:
        rule = ("减进任务里给的验收区间就算完成（本段区间是 %d–%d 字），减到区间内"
                "即停，**不许减到区间以下**。" % (int(low), int(high)))
    else:
        rule = "减进任务里给的验收区间就算完成，减到区间内即停，**不许减到区间以下**。"
    return _TRIM_SYSTEM_TMPL % (lo, hi, rule, format_banned_rules())


def build_insert_prompt(seg_lines, need, quota, offset, material="", evidence=None,
                        noted_material=None, raw_material=None, topic=None,
                        prev_rows="", cfg=None):
    """拼段内补字数的用户提示词。

    给的是**本段全文**，不是窗口。插在哪儿要看整段的起承转合：可能插在两句之间，
    也可能在本段末尾再接一拍——只给上下两句，它看不见该往哪儿塞。段本来就不长
    （几十句），一次给全撑不爆上下文。

    `prev_rows`：**本段之前**已经写好的正文（带全篇句号）。首写那一轮本来就看得见
    全篇已写正文（`prev_text`），补字这一轮从前只给本段——于是模型手里有九千字原文、
    有本段这几十句，**独独不知道前面几段讲过什么**，提示词却要它「挑正文还没讲到的
    点」：它只能撞。实测第 2 期那 33 句复读就是这么来的。主人一句话定的规矩：
    「虽然问题发生在本段，虽然原文素材只给本段的，但是我要看到所有已写的」。

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
    if prev_rows and str(prev_rows).strip():
        parts.append("【已写脚本（前文）】（本段**之前**已经写好的正文，编号是它在"
                     "全篇里的句号。**这些句子一个字都不许改**；它们同时是「已经讲过"
                     "什么」的清单——新增的句子不许跟这里任何一句重复）\n%s"
                     % prev_rows)
    parts.append("【本段已有正文】（编号是它在全篇里的句号。"
                 "**这些句子一个字都不许改**）\n%s" % _numbered_rows(seg_lines, offset))
    first, last = offset + 1, offset + len(seg_lines)
    low, high = _segment_window(quota, cfg)
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


def build_replace_prompt(script, hits, material="", noted_material=None,
                         raw_material=None, topic=None, evidence=None,
                         script_rows=None, cfg=None):
    """拼「就地替换」的用户提示词（段内轮次的「查重 → 替换」那一步）。

    `script` 是**整篇**正文，不只本段。替换这件事的发生原因就是「这一句跟前面某句
    撞了」——要它写出新的，就得让它同时看见两样东西：**撞到的那一句**长什么样、
    **整篇已经讲过什么**。只给本段等于把「别重复」交给猜，而它猜的结果就是换个说法
    再讲一遍（实机形态）。

    `hits` 是 `find_repeats` 的返回：`{要换掉的句号: 它跟哪一句重复}`。每一条都带上
    区间数字（`replace_span` 现算，与落地判据同源）——模型不知道区间，只会按「改写
    ＝写短点」的直觉写，然后被判没落地。

    素材只给**本段**（`raw_material`）：这一段该讲哪一块原文是画地图时定死的，范围
    之外的细节写出来就是编造。与补字那一轮同一条规矩。
    """
    parts = []
    ev = _evidence_block(evidence)
    if ev:
        parts.append(ev)
    if topic and str(topic).strip():
        parts.append("【本段主旨】%s" % str(topic).strip())
    if raw_material and str(raw_material).strip():
        parts.append("【本段原文】（本段覆盖的那部分原文；换出来的新句依据在这里，"
                     "范围之外的细节不要写）\n%s" % raw_material)
    if material:
        parts.append("【素材】（同一块内容的凝缩版，供你快速找点）\n%s"
                     % (noted_material or material))
    parts.append("【已写脚本】（**整篇**已经写好的正文，编号是它在全篇里的句号。"
                 "**除了【要换掉的句子】里点名的那几句，一个字都不许改**。它同时就是"
                 "「已经讲过什么」的清单——说一句「重复了」，指的就是跟这里的某一句"
                 "撞上了）\n%s"
                 % (script_rows if script_rows is not None
                    else _numbered_rows(script, 0)))
    rows = []
    for idx in sorted(hits or {}):
        first = int(hits[idx])
        src = script[idx - 1]
        was = len(re.sub(r"\s+", "", str(src.get("text") or "")))
        lo, hi = replace_span(was, cfg)
        rows.append("第 %d 句（%s 说）——与第 %d 句重复：\n"
                    "  原来写的是：%s\n"
                    "  换成新的：**%d~%d 字**，讲一件前文还没讲过的事"
                    % (idx, src.get("speaker", ""), first,
                       str(src.get("text") or ""), lo, hi))
    parts.append("【要换掉的句子】\n%s" % "\n".join(rows))
    parts.append("【任务】上面每一句都**原地**换成新内容：句子位置不变、说话人不变、"
                 "全篇行数一个字都不变。\n"
                 "换出来的必须讲一件**前文还没讲过**的事——先把【已写脚本】通读一遍，"
                 "在脑子里列出「已经讲过的话」清单，再回【本段原文】里挑还没进去的点。\n"
                 "每一条 edit 只填 `index` 与换好后的整句 `text`（带标点）。")
    parts.append("现在输出 JSON。")
    return "\n\n".join(parts)


def _replace_repeats(seg_lines, script, llm, cfg, vocab, card=None,
                     evidence=None, raw_material=None, material=None,
                     noted_material=None, topic=None, log=None):
    """查重 → 就地替换：把本段里跟前面重复的句子换成新内容，返回新的本段正文。

    这是段内每一轮收口前的最后一道工序（主人定的顺序：补字 → 查重 → 替换 → 计算）。
    放在这儿而不是等整期跑完再统一处理，因为**补字正是复读的源头**：模型为凑字数会把
    讲过的话再摆一遍，补完立刻查、就地换掉，下一轮的开头「计算」看到的才是干净稿。
    从前只有整期跑完那一刀去重，而删完**没有任何补字环节**——缺口只在删完才现形，
    补字的窗口早关了（真机第 2 期：4915 字，配额的 81.6%，报告还说料不够）。

    **就地替换而不是删**：重复 30 句就删 30 句，这一段字数塌下去，下一轮又去补、补出来
    又是重复，没尽头。换成新内容，行数与字数都守得住，也不会把同一个人挨到一起。

    查重范围是**整篇**、替换范围是**本段**：段是逐段写的，写本段时前面几段已经定稿在
    `script` 里；「第 2 段把第 1 段的话又说了一遍」只有整篇摆在一起才看得见（这正是
    补字轮从前的盲区——它手里只有本段正文）。

    一律**不抛异常**：模型调不动、输出坏了、条目没过判据，都在这里记一笔、原样返回。
    轮次由调用方管，这一步不许把整段按停。
    """
    log = log or (lambda m: None)
    if llm is None or not seg_lines:
        return seg_lines
    offset = len(script or [])
    combined = list(script or []) + list(seg_lines)
    hits = find_repeats(combined, offset + 1, offset + len(seg_lines))
    if not hits:
        return seg_lines                     # 没重复就一个调用都不花
    log("段：查重发现 %d 处与前文重复（%s），就地替换…"
        % (len(hits), "、".join("第 %d 句" % i for i in sorted(hits)[:6])))
    system = replace_system(card, cfg)
    rows = _numbered_rows(combined, 0)
    budget_of = getattr(llm, "input_budget_tokens", None)
    tok_of = getattr(llm, "material_tokens", None)
    if budget_of is not None and tok_of is not None:
        hold = (len(system) + len(raw_material or "") + len(material or "")
                + len(rows) + SEGMENT_FIXED_CHARS)
        if tok_of(hold) > budget_of(int(cfg.get("llm.max_tokens", 8192))):
            # 装不下就压前文：每句留前 24 字，句号与人还在，够判「讲过没有」。
            # 静默超预算发出去是不行的——小额度后端会直接顶爆。
            rows = _clip_script_rows(combined)
            log("段：替换提示词装不下全篇正文，前文压成每句前 24 字")
    user = build_replace_prompt(combined, hits, material=material,
                                noted_material=noted_material,
                                raw_material=raw_material, topic=topic,
                                evidence=evidence, script_rows=rows, cfg=cfg)
    try:
        raw, meta = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=float(cfg.get("llm.temperature", 0.8)),
            max_tokens=int(cfg.get("llm.max_tokens", 8192)),
            json_schema=replace_schema(cfg, vocab=vocab,
                                       max_index=len(combined)))
    except Exception as e:                  # noqa: BLE001
        log("段：替换这一轮跑不动（%s），这一轮不改稿" % e)
        return seg_lines
    if isinstance(meta, dict) and meta.get("degraded"):
        log("段：替换的约束解码已降级")
    try:
        edits, _inserts = parse_patch(raw)
    except ScriptError as e:
        log("段：替换输出没法解析（%s），这一轮不改稿" % e)
        return seg_lines
    out, rejected = apply_replace(combined, edits, sorted(hits), cfg)
    if rejected:
        log("段：%d 条替换没落地（%s）"
            % (len(rejected), "；".join(
                "第 %s 句：%s" % (r.get("index"), r.get("reason"))
                for r in rejected[:2])))
    fresh = out[offset:]
    if fresh and len(fresh) == len(seg_lines):
        return fresh
    return seg_lines                        # 出了预料外的形状就原样退回


def build_trim_prompt(seg_lines, over, quota, offset, evidence=None, cfg=None):
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
    low, high = _segment_window(quota, cfg)
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

    **这里不判句长**（v0.36.0 起）。句长是**全篇一把尺**的量，门禁 `line_length`
    两个方向都有处方（过长→精简措辞、过短→就地补内容），补字阶段替它把关的代价
    是把「一条坏句」放大成「整段停摆」——真机第 2 期就是这么停的（一条 7 字句让
    40+ 条新增整批作废，配额缺口 1872 字一次都没再试）。**结构类校验全部保留**
    （越界／非对象／非整数／空文本／speaker 非 A-B／emotion 非词表）：它们错了会
    塞错段、进错人嘴，必须拦。`cfg` 参数留着只为调用处签名不变。

    段内失败也不该在这里「交卷」：抛错由调用方接住、算作这一轮没改动，轮次照跑
    （见 `_generate_segmented` 里补字那一段的 except）。
    """
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

    **这里不判句长**（v0.36.0 起），同 `apply_insert`：句长归门禁 `line_length`
    管，压字阶段只管「少了多少字」这一笔账。`cfg` 参数留着只为调用处签名不变。
    """
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
    for attempt in range(_plan_rounds(cfg) + 1):
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
    log("规划 %d 次仍未通过，段段主旨按各段首节的凝缩主旨代填"
        % (_plan_rounds(cfg) + 1))
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
    # 三个轮次全部走配置，一处都不写死：段内修正轮（少了插入、多了压删，
    # 共用一份预算）、解析重发轮（输出坏了重发）、规划打回轮（见 `_segments_plan`）。
    fix_rounds = _segment_fix_rounds(cfg)
    parse_rounds = _segment_parse_rounds(cfg)
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
                                          _quota_floor(cfg), "（段主旨）",
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
            # 地板只防提示词自相矛盾（`_quota_floor`＝软句数下限 × 每句最少字）：
            # 再摊出来的配额不许低到「约 N 句」说不通。它比碎段合并那把尺小得多，
            # 正常一期碰不到，所以不会把前面段写超的账硬摊给后面段。
            quota = max(_quota_floor(cfg),
                        int(round(quota0 * (int(target_chars) - written)
                                  / float(remaining_planned))))
        topic = g.get("topic") or ""
        attempt, parse_try, best, best_gap = 0, 0, None, None
        low, high = _segment_window(quota, cfg)
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
                            # 每批的配额 = 本段配额 × 本批料的字 ÷ 本段料的字，
                            # 兜的是**有效字**——不是 token（token 只用于上面判
                            # 「要不要分批、按哪切」那一件事）。下界取 `_quota_floor`
                            # （一个最小可写段），与容差下限同一把尺。
                            q = max(_quota_floor(cfg), int(round(
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
                                                fit, sec_flags), cfg=cfg)
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
                    # 解析失败有**自己的一份预算**（`script.segment_parse_rounds`）：
                    # 它是「输出坏了」，跟「字数不对」不是一回事，不该挤占修正轮次。
                    parse_try += 1
                    if parse_try > parse_rounds:
                        raise ScriptError("第 %d 段连续 %d 次输出无法解析：%s"
                                          % (i, parse_rounds + 1, e))
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
                    # 【已写脚本（前文）】：补字这一轮从前只给本段，前几段一个字都不给。
                    # 模型手里有九千字原文、有本段这几十句，**独独不知道前面讲过什么**，
                    # 提示词却要它「挑正文还没讲到的点」——它只能撞。主人定的规矩：
                    # 问题发生在本段，但**所有已写的**都要让它看见。
                    prev_rows = _numbered_rows(script, 0) if script else ""
                    # 占位要把提示词里**真实出现的块**都算上：【本段原文】是全量喂的、
                    # 【已写脚本（前文）】也是全量喂的。从前只算了 fit 那一遍，等于按
                    # 不含原文的预算去裁素材——超预算用额度，本机额度大所以没露出来，
                    # 小额度后端会直接顶爆。加进来之后 fit 会自动降级成凝缩：预算里装
                    # 不下两份全文，留一份原文就够（凝缩只作快速找点的索引）。
                    other_chars = (len(psystem) + body + SEGMENT_FIXED_CHARS
                                   + len(seg_mat) + len(prev_rows))
                    fit, _note = fit_material(
                        llm, seg_mat, cfg, other_chars=other_chars,
                        evidence=evidence, log=log)
                    log("段 %d：少 %d 有效字，新增句子插入…" % (i, abs(gap0)))
                    raw, meta = llm.chat(
                        [{"role": "system", "content": psystem},
                         {"role": "user", "content": build_insert_prompt(
                             seg_lines, -gap0, quota, len(script), material=fit,
                             evidence=evidence,
                             noted_material=_apply_sec_notes(fit, sec_flags),
                             raw_material=seg_mat, topic=topic,
                             prev_rows=prev_rows, cfg=cfg)}],
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
                        # 这一轮作废，但**不收工**：轮次预算是给「改到进容差」的，
                        # 不是「一次失败即终点」——从前这里 break，真机第 2 期就是
                        # 被它按停的（缺口 1872 字后面一轮都没跑）。稿子一个字没动，
                        # 落到底部的核账就是「这一轮没改动」，轮次照减。
                        fixed = None
                        log("段 %d：新增没法落地（%s），这一轮不改稿" % (i, e))
                    if fixed is not None:
                        added = int(round(sum(duration_model.effective_chars(l["text"])
                                              for l in fixed))
                                    - sum(duration_model.effective_chars(l["text"])
                                          for l in seg_lines))
                        log("段 %d：插进 %d 句 / %d 有效字"
                            % (i, len(fixed) - len(seg_lines), added))
                        seg_lines = fixed
                else:
                    psystem = trim_system(cfg, low, high)
                    log("段 %d：多 %d 有效字，压紧删减…" % (i, gap0))
                    raw, meta = llm.chat(
                        [{"role": "system", "content": psystem},
                         {"role": "user", "content": build_trim_prompt(
                             seg_lines, gap0, quota, len(script), evidence=evidence,
                             cfg=cfg)}],
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
                        # 同补字那一路：这一轮不改稿，但不收工，轮次照减。
                        fixed = None
                        log("段 %d：压字数没法落地（%s），这一轮不改稿" % (i, e))
                    if fixed is not None:
                        log("段 %d：压 %d 句 / 删 %d 句" % (i, len(edits), len(drops)))
                        seg_lines = fixed
                if not seg_lines:
                    break
                # 补字与压字都做完，接着**查重 → 替换**，然后才轮末「计算」。
                # 主人定的顺序：第一轮「写作、计算」；后 N 轮「补字、查重、替换、计算」。
                # 补字是复读的唯一源头，补完立刻查、就地换掉，下一轮开头「计算」看到的
                # 就是干净稿。没查出重复时这一步不花任何模型调用（见 `_replace_repeats`）。
                seg_lines = _replace_repeats(
                    seg_lines, script, llm, cfg, seg_vocab, card=card,
                    evidence=evidence, raw_material=seg_mat, material=fit,
                    noted_material=_apply_sec_notes(fit, sec_flags),
                    topic=topic, log=log)
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
            if attempt > fix_rounds:
                log("段 %d：修 %d 轮仍差 %d 有效字，取最接近配额的一版继续"
                    % (i, fix_rounds, abs(best_gap)))
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
             gate_rounds=None, check_rounds=None, log=None, project=None,
             should_stop=None,
             draft_sink=None, evidence=None, segmented=False,
             sec_chars=None, material_of=None, sec_flags=None, review=None):
    """生成脚本并过检查与门禁。返回 dict。

    **三段串行，各管各的事**（顺序是定死的）：

    1. **出货段**——只拿一份可用稿子（分段初稿，或整篇现写）。不判内容、不判形式；
       产物不可用（输出拆不开 / 一句可用内容都没有）在本段重试，用尽就报错停下。
    2. **检查段**（内容检）——承诺链检必判、语义检按开关，判 ＋ 定点修，最多
       `check_rounds` 轮；过了或修满都往下走（不早退）。
    3. **门禁段**（形式门禁）——代码判九项，判 ＋ 定点修，最多 `gate_rounds` 轮；
       过了或修满都落盘。

    顺序为什么要紧：内容检的补丁会**插入新句**（加行，可能顶破连说上限），所以形式
    判定必须排在它后面，结论才是对着最终稿的。反过来排（门禁在前）就会出现「报告绿、
    稿子坏」——中间改过的形式没人复核，报告上还是旧结论。

    三段的定点修补都**不重写**：只把点名的句子交给模型改，全篇行数一个字不动。省的不
    只是 token：整篇重出会把没毛病的两百句重新摇一次骰子，改好的地方又带进新毛病，
    轮次全耗在打地鼠上。能定点的判据是「问题带不带句号」（见 `patch_targets`）；带
    句号的定点改，指不出句号的记一笔「未修好」、不重写也不硬猜。

    轮次用尽仍不通过 → **把最后一版连同未通过的结论一并返回**，不抛错。脚本阶段
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
    # 轮次是**两段各自的一条预算**，不是「一个数管两种检查」：检查段（内容检）修到过、
    # 或修满 check_rounds；然后才进门禁段（形式门禁），再修到过、或修满 gate_rounds。
    # 两段互不侵占对方的轮次；出货段不吃这两份预算（它有自己那个「输出坏了重发」的数）。
    # 段序见函数说明——检查在前、门禁在后的理由写在「门禁段」那一段的注释里。
    gate_rounds = int(gate_rounds if gate_rounds is not None
                      else cfg.get("script.gate_rounds", 3))
    check_rounds = int(check_rounds if check_rounds is not None
                       else cfg.get("script.check_rounds", 3))
    # 内容检这一次判哪几项：承诺链检必判；语义检是开关、默认关。关掉时它既不出现在
    # 请求里，也不算「待重判」（见 `dims_to_recheck`）。
    # 这里只装**项的代号**（"semantic"/"promise"），不带中文名——`gate_check6` 与
    # `dims_to_recheck` 都拿它去拼 `check_<代号>`、做 `key in dims`，塞元组进去当场崩。
    check_dims = (["promise"] if not cfg.get("script.check_semantic", False)
                  else [k for k, _ in CHECK6_DIMS])
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

    prev_lines = None   # 上一版正文（编号口径与门禁报的句号一致），定点修补的上下文用
    last = None         # 最后一版产物；出货段全败时拿它报错
    # 「输出坏了重发」的轮次：全项目只有这一个数——整篇出货重试与分段解析重试共用
    # 它（见 `_segment_parse_rounds`），任何一处都不许自己写死一个数。
    parse_rounds = _segment_parse_rounds(cfg)
    # 两段各自只看自己那一摊：检查段管内容项（`check_` 开头）、门禁段管形式项。
    # 让一段去修另一段的判据，改完又没人复判，报告上的结论就跟稿子两张皮。
    content_keys = set("check_" + k for k in check_dims)

    def is_content(key):
        return key in content_keys

    def is_form(key):
        return not key.startswith("check_")

    def _finish(payload):
        """整期定稿：**粘一次**片头尾 → 落盘 → 返回。粘合全篇只在这儿发生。

        片头尾不进轮：它不参与生成、不过门禁、不被定点修补、不核字数——从模板
        逐字拼出来的东西，没有「模型照没照做」可验（见 `glue_intro_outro`）。
        所以中间各轮落的盘一律是**裸正文**（过程稿），只有整期定稿这一刻才粘。

        唯一出口：三段跑完（不管改成功没改成功）都从这里定稿——片头尾不是「通过」
        的奖励，是每一期都该有的固定结构。

        定稿这一份**必须落盘**：出片读的是盘上那份（`draft_sink` 写的位置），
        界面拿的是返回值。只粘不落，盘上留着的就是上一轮的裸正文，看的和念的
        对不上。
        """
        glued = _glued_draft(payload, cfg, log, review=review)
        if draft_sink is not None:
            draft_sink(glued)
        return glued

    # ============================== 出货段：正文生成 ==============================
    #
    # 只干一件事：拿出一份**可用稿子**——内容对不对、形式合不合规都不判。它没有
    # 轮次表：产物不可用（输出拆不开 / 一句可用内容都没有）时在本段重试，次数吃
    # 「输出坏了重发」那个数，用尽就报错，不往下走。
    #
    # 独立成段是两段顺序调换逼出来的：检查段排在门禁段前面，它一进来就要判稿，
    # 而从前出货挂在门禁段第 1 轮里——那时候还没有任何一段跑过。
    if pre_draft is not None:
        # 分段初稿已到手：这一路只做检查与修补，不再整篇生成。
        script = pre_draft["script"]
        title = pre_draft["title"]
        plan = pre_draft["planned_episodes"]
        raw, meta = "", {}          # 分段路没有「原始整篇输出」，按 0 记
        pre_draft = None
    else:
        parsed, feedback = None, None
        for k in range(parse_rounds + 1):
            if k and should_stop and should_stop():
                break
            # 素材按这次调用的余量裁：系统提示、上一版正文都占地方，先量再定能给
            # 素材多少字。放不下就改用凝缩，并把这件事写进日志——静默少喂会让
            # 模型在缺依据的情况下硬写，产物上看不出少了什么。
            fit, _note = fit_material(
                llm, material, cfg,
                other_chars=len(system) + len(prev_lines or "")
                            + WRITE_FIXED_CHARS,
                evidence=evidence, log=log)
            user = build_user_prompt(fit, cfg, feedback, prev_lines)
            # 这一行在调用之前写。生成一轮要十几分钟，日志里若只有「模型返回」
            # 那一行，整轮期间界面上是上一次留下的字——看着像卡住了。
            log("生成第 %d 次：调用模型…" % (k + 1))
            raw, meta = llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=script_schema(vocab=whole_vocab))
            if meta.get("degraded"):
                log("生成第 %d 次：约束解码已降级" % (k + 1))
            log("生成第 %d 次：%s" % (k + 1, _call_telemetry(meta)))

            try:
                parsed = parse_script(raw)
            except ScriptError as e:
                log("生成第 %d 次：输出解析不出稿子（%s），重试" % (k + 1, e))
                parsed = None
                last = {"error": str(e)}
                feedback = "输出不是合法 JSON 对象。请只输出 JSON 对象本身。"
                continue
            script = normalize_script(parsed["lines"], cfg)
            if not script:
                # 解析出来了、但一句可用内容都没有：等于没有稿子，同样重试。
                log("生成第 %d 次：解析出的稿子一句可用内容都没有，重试" % (k + 1))
                parsed = None
                last = {"error": "稿子为空"}
                continue
            break
        if parsed is None:
            raise ScriptError("脚本生成失败：连续 %d 次都没能产出可用稿子"
                              % (parse_rounds + 1))
        # 返回行只报正文有效字（按归一化后的正文逐句求和，与配额同尺）。
        # 数数必须排在归一化之后：原始条目可能被模型省掉 text 键，直接取
        # `l["text"]` 会在缺键时整轮崩掉，而空 text 的条目本就该丢弃。
        # 这一行同时是界面判断「出货段跑到第几次」的信号。
        log("生成第 %d 次：模型返回正文 %d 有效字"
            % (k + 1, int(round(sum(duration_model.effective_chars(l["text"])
                                    for l in script)))))
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

    # **最后兜底**：正常路径不该走到这里。段内每一轮收口前都有「查重 → 就地替换」
    # （见 `_replace_repeats`），重复在那一步就换成新内容了；这一刀留给替换也没修干净
    # 的情形——模型给不出可用的替换件、轮次用尽，或逐期即兴那条路本来就不走段循环。
    # 仍然放在门禁与落盘之前、两条路共用：重复的内容不该进成片，也不该让时长门禁
    # 对着注水后的句数判「达标」（对注水稿判「达标」正是当年那次事故的样子）。
    script, dropped = dedupe_script(script)
    if dropped:
        log("兜底去重：替换轮没清干净，删掉 %d 句仍与前文完全重复的内容" % dropped)
        # 删句会让原本被重复段隔开的同一个人挨到一起，可能冒出新的连说超限。
        # 这里不掰：门禁会把整段点出来、交模型并句——程序翻的话，翻出来的是一句
        # 口气对不上、甚至根本不属于这个人的话。

    def _side_notes(report):
        """把「去重」与「没有标题」这两笔账加进报告。

        它们不是门禁判出来的，但必须进最终报告；报告每一轮都是新算的，所以每
        重建一次就补一遍（不会重复累加）。
        """
        if dropped:
            # 删得掉不等于没发生：这一笔要留痕，人才知道「替换轮没清干净」。
            # **不许再把责任推给素材**——料的账在画地图那一步已经按压比算过，这里
            # 再写一句「素材撑不满」，就会和刚判过「这一期料富余」的压比体检打架，
            # 而人只看得见后面这句话，方向被带偏。照实说：在哪一步没清掉、为什么。
            # 记成 soft 项：稿子已经被程序改干净了，没有理由再拦人。
            report["items"].append({
                "key": "no_repeat", "label": "重复凑数", "level": "warn",
                "judge": "code", "ok": False, "soft": True,
                "detail": "有 %d 句仍与前文完全重复（同一段被复播），替换轮没把它们"
                          "换成新内容，已由程序**兜底删除**。重复内容不会进成片。"
                          "**这不是素材不够**——多半是那一轮没给出可用的替换件、"
                          "或轮次用尽；料的账在画地图时已按压比算过。" % dropped})
        if not title:
            report["items"].append({
                "key": "title_present", "label": "本期标题", "level": "fail",
                "judge": "code", "ok": False,
                "detail": "模型未产出标题，正文也无法推出可用标题。"})
        return report

    # ============================== 检查段：内容检 ==============================
    #
    # 形状与门禁段一样——判 ＋ 定点修，只是判的人从代码换成了模型：承诺链检必判、
    # 语义检按开关。修到过、或修满 `check_rounds` 都往下走（**不早退**：形式还没
    # 判，内容没过不等于稿子不能用）。
    #
    # 排在门禁段**前面**：内容检的判据（承诺收没收、跟素材对不对得上）会被措辞
    # 改动带偏，先判完再动形式，最后那一轮形式判定就不会把内容结论弄陈旧。
    # 反过来的残留风险已知并备案：门禁段的并句与换措辞发生在内容检之后（并句有
    # 「保真 ≥ 原句七成」的硬校验、换措辞是同句换说法，两者都不删内容）。
    content_items = []
    # 检查段实际跑过的轮数。落盘那份 json 里的「第几轮」**跨段连续**：检查段先跑、
    # 门禁段接在它后面，两段各自从 1 数起的话，界面上的「第 N 轮」会从检查段的第 3
    # 轮跳回门禁段的第 1 轮——门禁段的 1 把前面几轮遮掉，看起来像全程只跑了一轮。
    check_used = 0
    if llm is not None:
        content_seen = None      # 上一次内容检的结论，决定这一轮重判哪几项
        retry_patch, retry_note = None, ""
        for attempt in range(check_rounds):
            check_used = attempt + 1
            if attempt and should_stop and should_stop():
                log("收到中止：检查第 %d 轮不跑了，带着当前稿子收工" % (attempt + 1))
                break
            patch = None
            if attempt:
                if retry_patch is not None:
                    patch, retry_patch = retry_patch, None
                else:
                    targets, unfixed = patch_targets(content_seen, len(script),
                                                     want=is_content, cfg=cfg)
                    # 与门禁段同一条规矩：定不了点的记一笔「未修好」，不早退、
                    # 不重写。静默丢掉这一笔，报告里就只剩「没过」，人看不出是
                    # 模型指不出句号。
                    if unfixed:
                        log("检查第 %d 轮：%d 个检查项指不出具体句子（%s），不重写"
                            "也不硬猜，记「未修好」"
                            % (attempt + 1, len(unfixed),
                               "、".join(p["label"] for p in unfixed)))
                    patch = targets
                    if not patch:
                        log("检查第 %d 轮：没有可定点修的项，转门禁" % (attempt + 1))
                        break
            if patch:
                log("检查第 %d 轮：定点修补 %d 句…" % (attempt + 1, len(patch)))
                fb = _patch_feedback(patch)
                # 修补的素材同样按这次调用的余量裁：提示词里还有系统提示、要改的
                # 条目、被点名句的前后文，它们都占地方。素材放不下就用凝缩顶
                # （见 fit_material），并在日志里说明。
                psystem = patch_system(cfg)
                fit, _note = fit_material(
                    llm,
                    material if _needs_material(content_seen, is_content) else "",
                    cfg,
                    other_chars=len(psystem) + len(fb) + len(prev_lines or "")
                                + PATCH_FIXED_CHARS,
                    evidence=evidence, log=log)
                praw, pmeta = llm.chat(
                    [{"role": "system", "content": psystem},
                     {"role": "user", "content": build_patch_prompt(
                         script, patch, fb, material=fit, evidence=evidence,
                         rejected_note=retry_note)}],
                    temperature=float(cfg.get("llm.temperature", 0.8)),
                    max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                    json_schema=patch_schema(vocab=whole_vocab, cfg=cfg))
                retry_note = ""
                if pmeta.get("degraded"):
                    log("检查第 %d 轮：约束解码已降级" % (attempt + 1))
                log("检查第 %d 轮：%s" % (attempt + 1, _call_telemetry(pmeta)))
                try:
                    edits, inserts = parse_patch(praw)
                    script, rejected = apply_patch(script, edits, patch, inserts, cfg)
                except ScriptError as e:
                    # 补丁没落地**不重写**：这一轮的稿子一个字没动，退回上一版不算
                    # 损失；而重写是把整段重摇一遍、还有概率依旧修不好。改为**下一轮
                    # 重发同一批补丁**，并把这次为什么整份退一并说清——不说，下一轮
                    # 就是重摇骰子。次数计入本段轮次。
                    log("检查第 %d 轮：补丁没法落地（%s），下一轮重发同一批补丁"
                        % (attempt + 1, e))
                    retry_patch = patch
                    retry_note = "上一轮这份补丁整份没落地：%s" % e
                    continue
                script = normalize_script(script, cfg)
                log("检查第 %d 轮：模型返回正文 %d 有效字"
                    % (attempt + 1,
                       int(round(sum(duration_model.effective_chars(l["text"])
                                     for l in script)))))
                if rejected:
                    # 拒了哪几条、为什么：进日志，也留在 `retry_note` 里给下一轮的
                    # 提示词——下一轮的任务多半一模一样，唯一的新信息就是它。
                    retry_note = _reject_feedback(rejected)
                    log("检查第 %d 轮：%d 条补丁没落地（%s），其余已改上"
                        % (attempt + 1, len(rejected),
                           "；".join("第 %s 句：%s"
                                     % (r.get("index"), r.get("reason"))
                                     for r in rejected[:2])))
                prev_lines = "\n".join("%d. [%s] %s"
                                       % (n + 1, s["speaker"], s["text"])
                                       for n, s in enumerate(script))

            # 内容检：只判本次该判的项（承诺链检必判；语义检开着时才加上）。
            # 重判范围取「上一轮没过、或压根没判过」的那几项——上一轮已判通过的，
            # 本轮改的又是措辞这类形的事，不必把同一段上下文再喂一遍。
            dims = (dims_to_recheck(content_seen, check_dims) if content_seen
                    else set(check_dims))
            if dims:
                content = gate_check6(script, cfg, llm=llm, material=material,
                                      dims=dims, evidence=evidence)
            else:
                content = {"items": []}
            report = merge_check({"items": []}, content, cfg, carry=content_seen)
            content_seen = report
            content_items = list(report["items"])
            if report.get("soft"):
                log("检查提示（不阻断）：%s"
                    % "、".join("%s %s" % (p["label"], p.get("detail", ""))
                                for p in report["soft"]))
            problems = [i for i in report["items"]
                        if not i["ok"] and not i.get("soft") and not i.get("advisory")]
            if attempt == 0:
                # 首轮既不出货也不修补（出货归出货段），从前日志上看像跳了一轮。
                log("检查第 1 轮：判定完成（%s）"
                    % ("通过" if report["passed"] else
                       "未过 %d 个检查项" % len(problems)))
            if report["passed"]:
                log("检查通过（第 %d 轮）" % (attempt + 1))
                break

            # 没过：这一轮落的是**过程稿**——裸正文，片头尾不粘（见 `_finish`）。
            if draft_sink is not None and attempt < check_rounds - 1:
                draft_sink({"script": script, "title": title,
                            "planned_episodes": plan, "segments": segments,
                            "report": report, "raw_chars": len(raw),
                            "degraded": meta.get("degraded", False),
                            "attempt": attempt + 1})
            if problems:
                log("检查未过：%d 个检查项（%s）"
                    % (len(problems), "、".join(p["label"] for p in problems[:6])))
            feedback = _build_feedback(problems)

    # ============================== 门禁段：形式门禁 ==============================
    #
    # 动作只有两件：判（代码）＋ 定点修（模型）。**没有重写权限**——稿子不可用是
    # 出货段的事，进到这里的一定是可用稿。修到过、或修满 `gate_rounds` 都落盘退出。
    #
    # 排在**最后**：形式结论因此永远是对着最终稿判的。从前它跑在最前面，而中间
    # 检查段的补丁会插入新句（加行，可能冒出连说超限）——改完没人复核形式，报告上
    # 门禁那几项还是旧结论，「报告绿、稿子坏」的静默放行就是这么来的。
    retry_patch, retry_note = None, ""
    for attempt in range(gate_rounds + 1):
        if attempt and should_stop and should_stop():
            # 已经有一版完整稿子在手，收工。写了一半的那次调用本来就掐不断，
            # 但「下一轮」是新的一次调用，没有再开一次的理由。
            log("收到中止：门禁第 %d 轮不跑了，带着当前稿子收工" % (attempt + 1))
            break

        patch = None
        if attempt:
            if retry_patch is not None:
                patch, retry_patch = retry_patch, None
            else:
                targets, unfixed = patch_targets(last["report"], len(script),
                                                 want=is_form, cfg=cfg)
                if unfixed:
                    log("门禁第 %d 轮：%d 个检查项指不出具体句子（%s），不重写也不"
                        "硬猜，记「未修好」"
                        % (attempt + 1, len(unfixed),
                           "、".join(p["label"] for p in unfixed)))
                if targets:
                    patch = targets
                else:
                    log("门禁第 %d 轮：没有可定点修的项，收工" % (attempt + 1))
                    break

        changed = None
        if patch:
            log("门禁第 %d 轮：定点修补 %d 句…" % (attempt + 1, len(patch)))
            fb = _patch_feedback(patch)
            psystem = patch_system(cfg)
            fit, _note = fit_material(
                llm,
                material if _needs_material(last["report"], is_form) else "", cfg,
                other_chars=len(psystem) + len(fb) + len(prev_lines or "")
                            + PATCH_FIXED_CHARS,
                evidence=evidence, log=log)
            raw, meta = llm.chat(
                [{"role": "system", "content": psystem},
                 {"role": "user", "content": build_patch_prompt(
                     last["script"], patch, fb, material=fit,
                     evidence=evidence, rejected_note=retry_note)}],
                temperature=float(cfg.get("llm.temperature", 0.8)),
                max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                json_schema=patch_schema(vocab=whole_vocab, cfg=cfg))
            retry_note = ""
            if meta.get("degraded"):
                log("门禁第 %d 轮：约束解码已降级" % (attempt + 1))
            log("门禁第 %d 轮：%s" % (attempt + 1, _call_telemetry(meta)))
            try:
                edits, inserts = parse_patch(raw)
                script, rejected = apply_patch(last["script"], edits, patch, inserts, cfg)
            except ScriptError as e:
                # 补丁没落地**不重写**：这一轮的稿子一个字没动，退回上一版不算损失；
                # 而重写是把整篇重摇一遍、还有概率依旧修不好。改为**下一轮重发同一
                # 批补丁**，并把这次为什么整份退一并说清。次数计入本段轮次。
                log("门禁第 %d 轮：补丁没法落地（%s），下一轮重发同一批补丁"
                    % (attempt + 1, e))
                retry_patch = patch
                retry_note = "上一轮这份补丁整份没落地：%s" % e
                continue
            script = normalize_script(script, cfg)
            # 返回行同样只报正文有效字（补完后的全稿求和），且保留「模型返回」形态
            # 供界面推进轮次进度。
            log("门禁第 %d 轮：模型返回正文 %d 有效字"
                % (attempt + 1,
                   int(round(sum(duration_model.effective_chars(l["text"])
                                 for l in script)))))
            title = last["title"]
            plan = last["planned_episodes"]
            refused = {r.get("index") for r in rejected}
            changed = len({e["index"] for e in edits} - refused)
            # 模型会漏改：实测里点名 7 句、它只回了 6 句，没回的那句原样留着。
            # 不把它当成失败——半份补丁也是净收益，漏掉的那句下一轮门禁会再
            # 报一次，接着补就是。但必须说出来，否则「为什么还是没过」会变成一个
            # 没人知道的悬案。
            if changed < len(patch):
                log("门禁第 %d 轮：只补到 %d 句，还差 %d 句没回（下一轮再补）"
                    % (attempt + 1, changed, len(patch) - changed))
            if rejected:
                retry_note = _reject_feedback(rejected)
                log("门禁第 %d 轮：%d 条补丁没落地（%s），其余已改上"
                    % (attempt + 1, len(rejected),
                       "；".join("第 %s 句：%s" % (r.get("index"), r.get("reason"))
                                 for r in rejected[:2])))
            prev_lines = "\n".join("%d. [%s] %s" % (n + 1, s["speaker"], s["text"])
                                   for n, s in enumerate(script))

        # 形式判定重新跑一遍（**每一轮都是对着当下这一版稿子判的**），再把这之前
        # 内容检的结论并回来——内容那几项归检查段，这里不重判、也不改它的结论。
        report = merge_check(_side_notes(gate_generate(script, cfg, card)),
                             {"items": content_items}, cfg)

        last = {"script": script, "title": title,
                "planned_episodes": plan,
                # 段清单（分段路有、整篇路空）：随定稿一起交出去，落盘方据此写
                # 旁挂规划档。正文那份 json 一个字段都不加（见 layout.plan_file）。
                "segments": segments,
                "report": report, "raw_chars": len(raw),
                "degraded": meta.get("degraded", False),
                # 轮号**跨段连续**：接在检查段实际跑过的轮数后面（见 `check_used`）。
                # 两段各自从 1 数起会让界面看到轮号回跳。
                "attempt": check_used + attempt + 1}
        if changed is not None:
            last["patched_lines"] = changed

        if report.get("soft"):
            log("门禁提示（不阻断）：%s"
                % "、".join("%s %s" % (p["label"], p.get("detail", ""))
                            for p in report["soft"]))
        problems = [i for i in report["items"]
                    if not i["ok"] and not i.get("soft") and not i.get("advisory")]
        if attempt == 0:
            # 首轮只判定、不出货也不修补（出货归出货段），从前日志上看像跳了一轮；
            # 而「%d 处」曾被读成「%d 句」——量词统一成「检查项」，句数一律说「句」。
            log("门禁第 1 轮：判定完成（%s）"
                % ("通过" if report["passed"] else
                   "未过 %d 个检查项" % len(problems)))
        if report["passed"]:
            log("门禁通过（第 %d 轮）" % (attempt + 1))
            break

        # 没过：这一轮落的是**过程稿**——裸正文，片头尾不粘。
        #
        # 每轮写完就落盘：稿子在手，后面哪一轮卡住、被中止、进程崩了，都不至于从头
        # 再来。但落的是「写到这儿的正文」，不是成品——整期还没定稿，片头尾挂上去
        # 等于给半成品盖上成品的样子；下一轮定点修补按句号改句子，粘合版还会让句号
        # 整体后移两位。粘合只在整期定稿那一刻做一次（见 `_finish`）。
        if draft_sink is not None:
            draft_sink(last)

        # 回灌只给「够格拦人」的项。soft 项（总时长）不拦人，也就不该拿去让模型
        # 重写：它既测不出也控不住，围着它改只会把内容改坏。advisory 项是模型自己
        # 没给出结论的，把「交人工复核」这种话递给写作模型毫无意义。
        if problems:
            log("门禁未过：%d 个检查项（%s）"
                % (len(problems), "、".join(p["label"] for p in problems[:6])))
        feedback = _build_feedback(problems)

    if last and "script" in last:
        # 没过也要粘、也要落盘：片头尾不是「通过」的奖励，是每一期都该有的固定结构
        # （见 glue_intro_outro）。轮次用尽时手上这一版就是这一期的定稿——改成功
        # 没改成功，都由它出片。
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
