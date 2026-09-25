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

"""配置总表与配置管理 —— 全项目唯一的推动点位。

依据《我思故我写》09b（配置推动的穷举一致性）：规则住在声明层，代码只负责解释。
本模块是唯一的声明层，四张封闭表：

    PARAM_SPEC  全部可配置点位（类型 / 值域 / 默认 / 归属组）
    MODE_SPEC   枚举型点位的档位定义（有限枚举）
    PRESET_SPEC 风格倾向预设（三维取值：体裁 / 比喻密度 / 互动词强度）
    GATE_SPEC   门禁条目（判据 / 阈值 / 级别 / 阶段）

界面控件的 min/max/step 与默认值、后端校验规则、流水线执行参数、文档描述，
全部由本表派生。禁止在其它模块另写阈值或值域——分散点位的枚举成本是乘积级的。
"""

import json
import os
import shutil
import tempfile

# 范式层只依赖标准库，且不反向引用本模块，可以在这里直接取。
from . import paradigms as _paradigms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

VERSION = "1.0.0"


# ============================================================================
# 物理约束表：采样率 → MP3 Layer III 码率上限
# 22050 Hz 属 MPEG-2 LSF，上限就是 160 kbps；配置 192k 会被编码器静默钳制。
# ============================================================================
MP3_BITRATE_LIMITS = {
    8000: 64, 11025: 64, 12000: 64,
    16000: 160, 22050: 160, 24000: 160,
    32000: 320, 44100: 320, 48000: 320,
}

AAC_BITRATE_LIMITS = {8000: 96, 11025: 96, 12000: 96, 16000: 128, 22050: 192,
                      24000: 192, 32000: 256, 44100: 320, 48000: 320}


def bitrate_ceiling(sample_rate, codec="mp3"):
    """返回该采样率下编码的码率上限（kbps）。未知采样率返回 320。"""
    table = MP3_BITRATE_LIMITS if codec == "mp3" else AAC_BITRATE_LIMITS
    if sample_rate in table:
        return table[sample_rate]
    nearest = min(table, key=lambda s: abs(s - sample_rate))
    return table[nearest]


# ============================================================================
# 受控词表：情绪标签（封闭集合，LLM 只能从中选）
# ============================================================================
# 这 14 个标签原本是混在一起用的，但它们不是一类东西：
#   · 7 个是真情绪（平静 / 好奇 / …）——描述「这句话是什么心情」，能转成语气；
#   · 7 个是语篇功能（追问 / 过渡 / 总结 / …）——描述「这句话在结构上干什么」，
#     跟心情无关。
# 混在一起有两个后果：语篇标签被当成语气送进 TTS，指令成了「用比喻的语气说」
# ——对模型是纯噪音；真情绪又淹没在这堆噪音里，等于语气压根没接上。所以拆开：
#   EMOTION_VOCAB   只放真情绪，供 TTS 生成语气指令
#   DISCOURSE_VOCAB 放语篇功能，供脚本侧做结构标记
#   EMOTION_TAGS    两者并集。脚本侧 emotion 字段 v0.27.0 起恢复，但**只吃语篇词**
#                   ——整份 DISCOURSE_ORDER 进 schema 枚举；真情绪词不回稿子。
#
# 「开场 / 收束」已从词表除名（v0.34.1）：片头尾由程序在定稿那一刻粘上
# （glue_intro_outro），正文里没有哪一句该背这两个标签——留在词表里，
# 等于给模型一个用不上的选项，还占下拉一行。
# **这两个词本身没有消失**：片头尾标签由 `PROGRAM_ONLY_TAGS` 提供（只归程序，
# 与本词表无关）。本词表仍是唯一那份「模型可填什么」的表。
EMOTION_VOCAB = [
    "平静", "好奇", "疑惑", "恍然", "肯定", "感慨", "轻松",
]

DISCOURSE_VOCAB = [
    "追问", "解释", "强调", "比喻", "铺垫", "总结", "过渡",
]

#: 「承接」是语篇枚举里的中性档——顺着上句往下讲、没有修辞动作的句子都用它。
#: 2a 期实证：全篇近半句子落在情绪词「平静」这个中性档上（118/250），没有它，
#: 约束解码会逼着模型给每一句都安一个修辞动作，标签必然失真。
DISCOURSE_NEUTRAL = "承接"

#: 枚举顺序：中性档排头（大多数句子是它），其余按「问→答→修辞→结构」排，
#: 与提示词里的释义顺序一致。**这一份就是全部**：提示词、输出 schema、界面下拉、
#: 门禁都从它取，没有任何按风格收窄的第二步——收窄过两次（提问频率去「追问」、
#: 情绪密度去「铺垫/过渡」），两次的结果都是「提示词给十个词、门禁判八个词」。
DISCOURSE_ORDER = [DISCOURSE_NEUTRAL, "追问", "解释", "强调", "比喻",
                   "铺垫", "过渡", "总结"]

# 顺序与拆分前逐字相同 —— 它决定提示词里列出的枚举顺序与界面下拉顺序，
# 排一下就是一次无谓的行为变更。（开场/收束已随 v0.34.1 除名，见上。）
EMOTION_TAGS = [
    "平静", "好奇", "追问", "疑惑", "恍然", "解释",
    "强调", "肯定", "感慨", "比喻", "铺垫", "总结", "过渡", "轻松",
]

#: 程序粘合句专用的三个语篇标签。**只归程序**：模板里写死、粘合时由程序带上；
#: 不发给模型（输出 schema 的枚举是 `DISCOURSE_ORDER`）、不进语篇词表。它们出现
#: 的地方有两处：① **重判**（页面把盘上那份成品整份拿回来复核）经
#: `gate_generate(..., extra_tags=...)` 放行，见 `web_ui.api_script_gate`；
#: ② **脚本页的标签下拉**（选项＝语篇词表 ＋ 本表，见 `web_ui.PAGE` 的注入）——
#: 盘上稿子里有这三个词，下拉里没有的话，那一格就没有任何选项能被选中，浏览器
#: 会显示第一项，看着像「数据错了」。
#:
#: v0.34.1 曾把开场/收束从词表除名、模板改写中性档「承接」，那是因为粘合之后还
#: 压着一道全稿收束、表外标签会被洗掉。v0.34.6 把粘合移到收束**之后**（glue 内部
#: 不再跑收束、只补时长），模板写什么就落什么，这几个词于是只活在程序侧。
#:
#: 「回顾」是 v0.36.0 加进来的第三词：前期回顾句与片头尾同类（程序逐字拼、
#: 模型没参与），挂在词表内的「承接」上等于把「这是回顾」这个位置信息丢掉。
PROGRAM_ONLY_TAGS = ("开场", "回顾", "收束")
#: 模板里引用的三个名字——解构自上面那唯一出处，不许再各写一份字面量。
INTRO_TAG, REVIEW_TAG, OUTRO_TAG = PROGRAM_ONLY_TAGS


# ============================================================================
# 内容规则表：可枚举的部分进硬门禁，不可枚举的交给 LLM 出报告
# ============================================================================
BANNED_RULES = [
    {
        "key": "absolute",
        "label": "绝对化表述",
        "words": ["一定", "必然", "所有", "绝对", "永远", "从不", "毫无",
                  "百分之百", "毫无例外", "无一例外"],
        "level": "fail",
        "why": "把话说满，听众一碰到反例，整段论断就跟着塌",
        # substitute 会原样写进 prompt 与回灌反馈：前置只给「不许用」就是让模型
        # 空手避开——它知道要绕开什么，却不知道绕去哪里，只好换个同义的说法
        # 继续把话说满。给出落点，这条规则才是可执行的。
        "substitute": "「往往」「容易」「不少」「多数」「大体上」",
        "note": "改「往往」「容易」「不少」",
    },
    {
        "key": "soft_directive",
        "label": "软约束式指令",
        "words": ["请你", "建议你", "你必须", "你应该"],
        "level": "warn",
        "why": "播客是陈述体，不是指令体",
        "substitute": "陈述句，如「我们会看到」「值得留意的是」",
        "note": "播客是陈述体，不是指令体",
    },
]

# 两项内容检测：判据都在语义层，交给模型，一次调用判完（见 script_engine.check6_llm）。
# 这里是给界面看的清单；判据与级别以 GATE_SPEC 为准，两处不各写一份。
CONTENT_CHECKS = [
    {"key": "semantic", "label": "语义检",
     "judge": "每条台词的内容能在素材中找到依据，未出现素材之外的事实、数据、来源",
     "owner": "llm"},
    {"key": "promise", "label": "承诺链检",
     "judge": "片头提出的疑问、设问或承诺，在后文有回应",
     "owner": "llm"},
]


# ============================================================================
# 字幕断行禁则表（中文避头尾规则）
#
# 收窄原则：只把「真正不作行首」的字列进来。像「就」「都」「也」「是」这些
# 可以出现在句首（"这就是答案"），若一并列入禁则，会把逗号后的最佳断点
# 一并否掉，反而被迫在词中间硬切。
# ============================================================================
KINSOKU_HEAD = set("的了着吗呢吧啊呀嘛哦噢")
KINSOKU_TAIL = set("一第每各某")


# ============================================================================
# 片头片尾模板
#
# 形状是「句子列表」，每句自带说话人。它在**整期定稿那一刻**由程序逐字拼上去（见
# `script_engine.glue_intro_outro`）：模型只写正文，片头尾不进生成、不进任何
# 门禁——格式轴的事由代码办，模型省下的注意力正好用在内容上。
#
# 为什么是列表而不是「intro_first / outro_last」两个字符串：片头本来就不止一句。
# 标准档片头两句（A 报欢迎、B 报本期题目与播讲人），精简档只留一句欢迎。从前表里
# 只有一个 `intro_first`，另有 `intro_lines: 2` 这个数从来没人消费——「片头有两句」
# 这件事只写在数里、没写在句里，于是第二句永远是模型自己发挥的，各期不一样。
#
# `{audience_clause}` 是**可选片段**：项目里没填受众时它整段消失，句子退化成
# 「欢迎收听《X》。」，不会留下「面向的听众」这种半句话。受众的取值与生成时机见
# `project_store`（排地图时由模型给第一版，界面可改，人填过重排不覆盖）。
#
# `{title}` 是期标题（`TITLE_MAX` 字以内）。为空时那一句整个不粘——与其粘出
# 「本期讲述，播讲人小美、大美」这种半句，不如少一句。
#
# `review`（前期回顾）**不是档位**，是个位置：它与 standard / brief 并列，不随
# 档位变。粘合顺序是 `片头 → 前期回顾 → 正文 → 片尾`——回顾紧跟片头之后，因为
# 它是「上期讲到哪儿」的交代，得在正文开始前让听众听到；放到正文后面就成了尾声。
#
# 内容是**逐字引用上一期**：期主旨（地图上的 gist）＋ 段主旨（上一期的旁挂规划
# 档，见 `layout.plan_file`），取值与拼句由 `pipeline.review_rows` 负责。取不到
# 就整句不粘——第 1 期、前一期不在本项目、上一期没留下段主旨，三种情况一律按
# 「这句没有」处理，绝不留「上期我们聊了……」后面空着。
#
# 模板是**三条**（标题 / 期主旨 / 段主旨各一条），底下 `review` 那一组写的就是
# 它们。引用值进位前只剃尾（去掉尾部的句号与空格），中间一个字不动——见
# `pipeline.review_rows`。
#
# `{prev_topics}` 后面那个「等」**写死在模板里**：一期三五段，全念出来是流水账，
# 只引前三段；「等」是告诉听众"还有"，一条时也照留。不给程序按条数决定加不加——
# 条数一变句子形状就变，听感上像两套模板。
# ============================================================================
INTRO_OUTRO = {
    # 标签分工（v0.36.0 定型；四种序列见 `glue_intro_outro`）：
    #   片头第一句 = 开场（`INTRO_TAG`）；片头**其余句** = 词表内的中性档
    #   `DISCOURSE_NEUTRAL`「承接」——第二句是「接开场的话头，往下讲本期」，
    #   是承接不是第二个开场；两句都挂「开场」会得出「开场、开场」，而目标序列里
    #   **只有一个开场**。挂中性档还顺带省掉一个例外：它本来就是词表里的词，
    #   生成侧、门禁、下拉一路天然都认。
    #   前期回顾 = 回顾（`REVIEW_TAG`）；片尾 = 收束（`OUTRO_TAG`）。
    # 开场/回顾/收束只归程序：模板里写死、粘合时带上，不进语篇词表。粘合发生在
    # **全稿格式收束之后**——v0.34.6 起 glue 内部不再跑收束、只补时长，所以模板
    # 写什么就落什么，不受语篇词表管；它们唯一被认的地方是重判那条放行路。
    "standard": {
        "intro": [
            {"speaker": "A", "emotion": INTRO_TAG,
             "text": "欢迎收听《{program}》{audience_clause}。"},
            {"speaker": "B", "emotion": DISCOURSE_NEUTRAL,
             "text": "本期讲述{title}{names_clause}。"},
        ],
        "outro": [
            {"speaker": "B", "emotion": OUTRO_TAG,
             "text": "这里是《{program}》，欢迎关注。"},
        ],
    },
    "brief": {
        "intro": [
            {"speaker": "A", "emotion": INTRO_TAG,
             "text": "欢迎收听《{program}》{audience_clause}。"},
        ],
        "outro": [
            {"speaker": "B", "emotion": OUTRO_TAG,
             "text": "这里是《{program}》，欢迎关注。"},
        ],
    },
    # 前期回顾：不随档位变（所以不在 standard / brief 里面），位置是**第 2 句**
    # ——紧跟片头第一句，见 `glue_intro_outro`。开关见 PARAM_SPEC 的
    # `intro_outro.review`，默认关。
    # 标签用程序专有的「回顾」（v0.36.0 起）：它跟片头尾同类——程序逐字拼、
    # 模型没参与，也就没有「它照没照做」可验。挂词表内的「承接」会把「这是回顾」
    # 这个位置信息丢掉，还让它混进正文标签里。
    # 模板写三条，一物一句：标题 / 期主旨 / 段主旨各归各的句子。**不再拼成一整段
    # 再按尺切**——按尺切的边界由长度说了算，切出过「…收窄至"仅填空"的本质，」
    # 下一句以「的架构迭代」开头这种半句话（切点落在西文词后的空格上，虚词被甩到
    # 下一句）。三条各自是一句完整的话，拼完不再切（见 `pipeline.review_rows`）。
    # **引用值里的标点只剃尾，中间半字不碰**。所以「上期标题／期主旨／段主旨」这三样
    # 源数据自己写坏了，坏字会逐字进到这三句里：地图 gist 上就真出现过
    # `…硬约束逻辑。：将抽象方法论…` 这种「。：」（真人手输的，不是程序拼的——按尺切
    # 只把它切开露出来，不是它造成的）。回顾这一层**不替数据删字符**：替它删就是
    # 「中间也管」，正是要避免的自行发挥。要干净就去改地图那一格。
    "review": [
        {"speaker": "B", "emotion": REVIEW_TAG, "text": "上期《{prev_title}》。"},
        {"speaker": "B", "emotion": REVIEW_TAG, "text": "聊的是{prev_gist}。"},
        {"speaker": "B", "emotion": REVIEW_TAG, "text": "讲了{prev_topics}等。"},
    ],
}


# ============================================================================
# MODE_SPEC：枚举型点位的档位定义（有限枚举，封闭表）
# ============================================================================
MODE_SPEC = {
    "background.preset": {
        "label": "背景档位",
        "options": {
            "ink": {"label": "墨色极简", "bg_top": "#12224A", "bg_bottom": "#081630",
                    "accent": "#C9A45C", "light": "#E8EDF8", "muted": "#96A5C3"},
            "slate": {"label": "冷灰学术", "bg_top": "#1A2430", "bg_bottom": "#0C1218",
                      "accent": "#6FA8DC", "light": "#E4EDF5", "muted": "#8FA3B8"},
            "sepia": {"label": "暖褐书卷", "bg_top": "#2A2018", "bg_bottom": "#16100A",
                      "accent": "#C08A4A", "light": "#F0E6D8", "muted": "#B8A288"},
        },
    },
    "cover.preset": {
        "label": "封面档位",
        "options": {
            "book": {"label": "书名主导"},
            "episode": {"label": "标题主导"},
            "minimal": {"label": "极简"},
        },
    },
    "bgm.preset": {
        "label": "背景音乐档位",
        "options": {
            "still":   {"label": "静水（钢琴弦乐垫）"},
            "pensive": {"label": "沉思（柔琴内省）"},
            "bright":  {"label": "轻快（木吉他明快）"},
            "deep":    {"label": "深沉（大提琴电影感）"},
            "chimes":  {"label": "金玉清音（编钟玉磬）"},
            "blades":  {"label": "兵气寒锋（金属暗弦）"},
            "summer":  {"label": "夏虫低鸣（虫声入乐）"},
            "frost":   {"label": "冰晶寒境（冰铃钢琴）"},
            "nature":  {"label": "山林自然（溪鸟入乐）"},
            "warm":    {"label": "暖光（柔琴暖吉他）"},
            "horror":  {"label": "惊悚（不协和弦乐）"},
            "mech":    {"label": "机械律动（齿轮滴答）"},
            "tech":    {"label": "科技感（合成脉冲）"},
            "chaos":   {"label": "混乱（冲突纹理）"},
            "riot":    {"label": "暴动（急促强打击）"},
        },
    },
    "animation.mode": {
        "label": "动画档位",
        "options": {
            "static": {"label": "静止", "filter": "", "cost": "最省",
                       "desc": "不做任何运动，纯静止背景"},
            "kenburns": {"label": "缓推", "filter": "zoompan", "cost": "低",
                         "desc": "全程匀速缩放。会带动背景元素位移，构图会被放大"},
            "waveform": {"label": "波形", "filter": "showwaves", "cost": "中",
                         "desc": "半透明声波随音频律动"},
            "spectrum": {"label": "频谱", "filter": "showspectrum", "cost": "高",
                         "desc": "频谱条铺底，技术向"},
        },
    },
    "speaker_indicator.mode": {
        "label": "说话人提示",
        "options": {
            "none": {"label": "无", "desc": "仅靠音色区分"},
            "style": {"label": "AB两套字幕样式",
                      "desc": "A 说的句子走 A 角字幕色、B 的走 B 角字幕色"},
            "block": {"label": "侧栏色块", "desc": "左右色块按句高亮当前说话方"},
            "portrait": {"label": "自备立绘", "desc": "用户提供透明 PNG，说话方高亮"},
        },
    },
    # 版式只管「字在框里怎么排」。两档共用同一个矩形框（左右与底边取自边距、
    # 高 = 行数 × 行距），**唯一的分岔是轴**：
    #   lyric   纵轴 —— 框内若干句、折行后纵向排布，换句时整块上滚
    #   single  横轴 —— 框内一行，装不下就横向滚过框口（不折行、不裁字）
    # window / frame_rows 写在档位里，因为它们是**框的形状**：单行滚动的框按定义
    # 就是一行（1 行高、只显示当前句），给用户一个「框排几行」的旋钮去调它等于
    # 摆一个死框。歌词档不写这两个，取 `subtitle.window` / `subtitle.frame_rows`。
    # 从前是三档（single / dual / lyric），各有各的模型：单行与双行是「per-line
    # 填充块 + Style 静态定位」，歌词是「整块框 + \move」——同一个「字幕版式」
    # 下拉里塞了两套定位模型。双行已于 0.42.0 删除，单行改横滚，三档并两档。
    # 「行首要不要写说话人名」从前塞在版式里（dual_named 档），那是把两个维度
    # 绑在一起：名字归「说话人提示」的角色名称开关，版式不再管。
    "subtitle.preset": {
        "label": "字幕版式",
        "options": {
            "single": {"label": "单行滚动", "axis": "x",
                       "window": 1, "frame_rows": 1},
            "lyric": {"label": "歌词", "axis": "y"},
        },
    },
    "intro_outro.preset": {
        "label": "片头片尾",
        "options": {
            "standard": {"label": "标准"},
            "brief": {"label": "精简"},
        },
    },
    "tts.engine": {
        "label": "语音引擎",
        "options": {
            "edge": {"label": "Edge-TTS（LGPL-3.0）"},
            "qwen3tts": {"label": "Qwen3-TTS（Apache-2.0）"},
        },
    },
    "llm.backend": {
        "label": "LLM 后端",
        "options": {
            "lm-studio": {"label": "LM Studio", "default_base_url": "http://127.0.0.1:1234"},
            "ollama": {"label": "Ollama", "default_base_url": "http://127.0.0.1:11434"},
            "custom": {"label": "自定义 OpenAI 兼容", "default_base_url": ""},
        },
    },
    "bgm.mode": {
        "label": "背景音乐来源",
        "options": {
            "none": {"label": "不用音乐"},
            "builtin": {"label": "内置合成"},
            "custom": {"label": "自备文件"},
        },
    },
    # ---- 技术档位：值即档位。写进本表是为了让「标签来源」只有一处，
    # 界面上不再出现 argument / veryfast 这类英文裸值。
    "script.compress_ratio": {
        "label": "压缩档（原文:成稿）",
        "options": {
            3: {"label": "1:3 贴原文", "desc": "脚本保留最多细节；期数偏多、整体偏长"},
            5: {"label": "1:5 推荐", "desc": "取舍有肉：既不贴原文照念，也压得动"},
            10: {"label": "1:10 浓缩", "desc": "干货浓缩、细节大量舍去；素材体量大时用"},
        },
    },
    "script.style_preset": {
        "label": "风格倾向",
        "options": {},          # 由 PRESET_SPEC 填充（见文件末尾的 _fill_preset_modes）
    },
    "audio.sample_rate": {
        "label": "采样率",
        "options": {
            16000: {"label": "16000 Hz", "desc": "语音通话档，音质受限"},
            22050: {"label": "22050 Hz", "desc": "MP3 上限 160 kbps"},
            24000: {"label": "24000 Hz", "desc": "语音增强常用"},
            32000: {"label": "32000 Hz", "desc": "通用档"},
            44100: {"label": "44100 Hz", "desc": "播客推荐"},
            48000: {"label": "48000 Hz", "desc": "视频剪辑常用"},
        },
    },
    "audio.channels": {
        "label": "声道数",
        "options": {
            1: {"label": "单声道", "desc": "人声节目首选，体积小"},
            2: {"label": "立体声", "desc": "留有声场，体积约一倍"},
        },
    },
    "audio.codec": {
        "label": "音频编码",
        "options": {
            "mp3": {"label": "MP3", "desc": "兼容性最好"},
            "aac": {"label": "AAC", "desc": "同码率下音质更好"},
        },
    },
    "video.fps": {
        "label": "视频帧率",
        "options": {
            24: {"label": "24 fps", "desc": "电影感"},
            25: {"label": "25 fps", "desc": "PAL 制"},
            30: {"label": "30 fps", "desc": "通用推荐"},
            50: {"label": "50 fps", "desc": "PAL 双倍"},
            60: {"label": "60 fps", "desc": "顺滑，体积大"},
        },
    },
    # 值是 x264 的 preset 名，必须原样送进编码器；标签给人看，所以写中文。
    # 这里图省事把标签也写成 ultrafast 之类，界面就退化成英文裸值了。
    "video.encoder_preset": {
        "label": "编码预设",
        "options": {
            "ultrafast": {"label": "极速", "desc": "编码最快，文件最大"},
            "veryfast": {"label": "很快", "desc": "偏快，文件偏大"},
            "fast": {"label": "较快", "desc": "折中偏快"},
            "medium": {"label": "均衡", "desc": "默认推荐"},
            "slow": {"label": "慢速", "desc": "编码最慢，文件最小"},
        },
    },
}


# ============================================================================
# PRESET_SPEC：风格倾向（三维）—— 划界权在人，LLM 不选
# ============================================================================
# 风格倾向只管**调子**：全篇按什么路子推进、拿多少比方、用不用互动词。
# 它不管「谁说几句、多久问一次」——那是对话形式的事（见 paradigms.DIALOGUE_FORMS）。
#
# 从前这里还挂着两维，都拆掉了，理由各不一样：
#
#   · 提问频率：它手里握着两把钥匙。一是提示词里那一行带数的硬要求（「每两句到
#     三句有一处问句」）——整份提示词里唯一带数的节奏规定；二是低档时把「追问」
#     从语篇枚举里摘掉，等于顺手管了标签表。而「多久问一次」只有两个人对话才
#     成立，一个人念稿子哪来的提问频率？它挂在风格里，就跟对话形式抢方向盘：
#     形式给的是上限（上限不是目标），风格给的是一条带数的硬要求——带数的那个
#     赢，于是选了「主讲＋捧哏」也照样被逼成一句一问。
#
#   · 情绪密度：它一个字都不进提示词，唯一作用是低档时把「铺垫」「过渡」从枚举
#     里删掉。人在界面上配不出、也验不了它，只有提示词列的词与门禁判的词对不上
#     的时候才露出来——「词表外标签」那条报错正是它的来路。
#
# 两维都拆掉之后，语篇词表**只剩一份**（DISCOURSE_ORDER，八词）：提示词列的、
# 输出 schema 枚举的、界面下拉给的、门禁判的，读的是同一个常量，不可能再出现
# 「写的与判的对不上」。
STYLE_DIMS = {
    "genre": {"label": "体裁", "options": {
        "argument": "论证型", "story": "故事型", "science": "科普型",
        "debate": "对辩型", "review": "复盘型"}},
    "metaphor_density": {"label": "比喻密度", "options": {"low": "低", "mid": "中", "high": "高"}},
    "interaction": {"label": "互动词强度", "options": {
        "restrained": "克制", "natural": "自然", "warm": "热络"}},
}

PRESET_SPEC = {
    "argument": {"label": "论证型", "genre": "argument",
                 "metaphor_density": "low", "interaction": "restrained"},
    "story": {"label": "故事型", "genre": "story",
              "metaphor_density": "mid", "interaction": "warm"},
    "science": {"label": "科普型", "genre": "science",
                "metaphor_density": "high", "interaction": "natural"},
    "debate": {"label": "对辩型", "genre": "debate",
               "metaphor_density": "low", "interaction": "warm"},
    "review": {"label": "复盘型", "genre": "review",
               "metaphor_density": "mid", "interaction": "natural"},
}

# 风格倾向的档位标签取自 PRESET_SPEC，不另写一份。原先这里只有键名数组，
# 界面回退成 String(value)，于是下拉里全是 argument / story 这样的英文裸值。
# 说明文字里跳过 genre：它和档位名是同一个词，列出来只会变成
# 「论证型 · 论证型、低、中、低、克制」，等于把标签念了两遍。
MODE_SPEC["script.style_preset"]["options"] = {
    k: {"label": v["label"], "desc": "、".join(
        "%s%s" % (STYLE_DIMS[d]["label"], STYLE_DIMS[d]["options"].get(v[d], v[d]))
        for d in STYLE_DIMS if d != "genre" and v.get(d))}
    for k, v in PRESET_SPEC.items()
}

# 范式选项取自范式层，不在本表重列一遍：列两处，加一张卡就要改两处，
# 而漂掉的那处会把界面上的下拉与提示词里的组织依据指成两套。
MODE_SPEC["script.paradigm"] = {"label": "素材类型",
                              "options": _paradigms.options()}

# 对话形式的选项同样取自范式层：六种形式的标签、说明、A/B 角色、上限只写一处，
# 界面下拉与提示词共用一份。空值是默认项「跟随素材类型」——它代表「按卡上的站位
# 走」，不是「没有形式」，所以在下拉里要有一行看得见的说明，而不是留空。
#
# 说明后面挂上**这一种形式里 A 是谁、B 是谁**，以及它的**连句上限**：上限由形式
# 定（见 paradigms.run_caps），门禁按它判、超限也按它点出来交模型并句，人在界面
# 上却看不到这两
# 样——它们都是能被执行的数/角色，界面上得看得见出处。角色更是选这种形式的主要
# 理由：选「主讲＋捧哏」的人要知道这里谁主讲。
def _form_note(f):
    """把一种形式的角色与两条上限说成一句人话。"""
    roles = f.get("roles") or {}
    run = f.get("run") or {}
    who = "；".join("%s 是%s" % (k, str(roles[k]).split("：")[0].strip())
                    for k in ("A", "B") if roles.get(k))
    caps = "连着说的上限：A %d 句、B %d 句" % (int(run["A"]), int(run["B"]))
    return "；".join(x for x in (who, caps) if x)


MODE_SPEC["script.dialogue_form"] = {
    "label": "对话形式",
    "options": dict(
        [("", {"label": "跟随素材类型",
               "desc": "按素材类型卡上写的两人站位分工走，连着说的上限也随卡上"
                       "默认的那种形式；选了下面某一种，角色与上限都用那一种"
                       "顶掉卡上的"})]
        + [(k, {"label": v["label"],
                "desc": "%s（%s）" % (v["desc"], _form_note(v))})
           for k, v in _paradigms.DIALOGUE_FORMS.items()]),
}


# ============================================================================
# PARAM_SPEC：全部可配置点位
# ============================================================================
def _p(type_, default, group, label, **kw):
    d = {"type": type_, "default": default, "group": group, "label": label}
    d.update(kw)
    return d


PARAM_SPEC = {
    # ---- 脚本 ----
    "script.target_minutes": _p("float", 4.0, "script", "目标时长（分钟）",
                                min=0.5, max=60, step=0.5,
                                unit="分钟", views=("script",),
                                help="脚本总时长的目标值，字数由时长模型反推"),
    "script.compress_ratio": _p("enum", 5, "script", "压缩档（原文:成稿）",
                                views=("script",),
                                help="画地图的分量尺子：一期脚本目标字数最多消化多少倍原文。"
                                     "如 1:5 即一期 6000 字脚本最多消化约 3.75 万字原文"
                                     "（上限 = 档位 × 1.25）；一期原文不足目标字数一半"
                                     "（压比 0.5）判没话硬写，排图时回炉"),
    "script.style_preset": _p("enum", "argument", "script", "风格倾向",
                              views=("script",)),
    "script.segment_min_sents": _p("int", 15, "script", "分段软句数下限",
                                   min=3, max=60, step=1, unit="句",
                                   views=("script",),
                                   help="提示词里「约 N 句」的下限，也是碎段合并"
                                        "阈值的一因子（阈值 = 本值 × 期望句长）。"
                                        "低于阈值的相邻段会并进配额较小的邻居，"
                                        "避免一次调用只写三句话"),
    "script.segment_tol_frac": _p("int", 15, "script", "段字数容差（%）",
                                  min=1, max=50, step=1, unit="%",
                                  views=("script",),
                                  help="一段写多长算合格：配额 ±max(配额×本比例, "
                                       "最小可写段字数×本比例)。落在区间内即收工，"
                                       "不再修"),
    "script.segment_fix_rounds": _p("int", 5, "script", "段内修正轮次",
                                    min=1, max=50, step=1, unit="轮",
                                    views=("script",),
                                    help="一段写完发现字数对不上配额，最多还能修几次"
                                         "（少了插入新句、多了压紧删减，共用这一份预算；"
                                         "整段重写不存在）。轮数用完就取最接近配额的一版"
                                         "继续"),
    "script.segment_parse_rounds": _p("int", 2, "script", "输出坏了重发轮次",
                                      min=1, max=10, step=1, unit="轮",
                                      views=("script",),
                                      help="模型输出拆不开（不是内容不对，是格式坏了）"
                                           "时重发几次；整篇出货重试与分段解析重试共用"
                                           "这一个数。用尽仍拆不开就报错停下"),
    "script.plan_rounds": _p("int", 2, "script", "规划打回轮次",
                             min=1, max=10, step=1, unit="轮",
                             views=("script",),
                             help="分段生成时，规划轮（给各段起名、写段主旨）最多打回"
                                  "几次；仍不通过就按各段首节的凝缩主旨代填"),
    "script.shape_flags": _p("bool", True, "script", "取材标记",
                             views=("script",),
                             help="素材里含网址、代码、公式、表格、路径、清单等"
                                  "「给眼睛看的」内容时，在提示词里给模型打标记"
                                  "提醒转述或跳过，不逐字念进台词。只改提示词，"
                                  "不动素材与配额；出口台词门禁照常兜底"),
    "script.flag_title_words": _p("str", "镜像,下载,安装,命令,参数,许可,license,"
                                       "官网,配置,示例", "script", "清单类标题词表",
                                  views=("script",),
                                  help="节标题命中任一词即标为清单类节（逗号分隔，"
                                       "不区分大小写）；只影响提示词提醒，不删料"),
    "script.gate_strict": _p("bool", True, "script", "门禁严格模式",
                             views=("script",),
                             help="开启后 warn 级条目也不放行"),
    "script.gate_rounds": _p("int", 3, "script", "门禁轮次上限",
                             min=1, max=5, step=1, unit="轮", views=("script",),
                             help="形式门禁最多判几轮（含第 1 轮判定）：它排在最后一道，"
                                  "修满仍不通过就带着问题落盘交人，不在这里空转。"
                                  "轮数越多越慢"),
    "script.check_rounds": _p("int", 3, "script", "检查轮次上限",
                              min=1, max=5, step=1, unit="轮", views=("script",),
                              help="内容检（承诺链检；语义检开着时一并判）最多跑几轮"
                                   "（含第 1 次检查）：修满仍不通过就转门禁段，"
                                   "不在这里空转。轮数越多越慢"),
    "script.check_semantic": _p("bool", False, "script", "语义检（逐字核素材）",
                                views=("script",),
                                help="开启后，内容检逐句核每条台词能否在素材原文里找到"
                                     "依据；素材装不下时按调用额度分批核，是全链最贵的"
                                     "一项检查。默认关：只判承诺链检，不带素材、一次"
                                     "调用判完"),
    "script.map_max_episodes": _p("int", 60, "script", "地图期数上限",
                                  min=1, max=500, step=1, views=("script",),
                                  help="成稿规划排地图时最多排多少期；项目已定计划期数时以项目为准"),
    "script.dialogue_form": _p("enum", "", "script", "对话形式",
                               views=("script",),
                               help="两位主持人「话怎么接」，同时决定同一人连着"
                                    "说的上限（A / B 各一条）；留空＝跟随素材类型。"
                                    "生成用的哪一种会记在这**一期**上，回头重判、"
                                    "重写都按这一期自己那份走"),
    "script.paradigm": _p("enum", "auto", "script", "素材类型",
                        views=("script",),
                        help="排地图时的组织依据：切分单位、整合依据、重点判据、推进方式。"
                             "拿不准选「自适应」，由探查按结构推断；项目里可以逐个改"),
    "intro_outro.preset": _p("enum", "standard", "script", "片头尾档位",
                             views=("script", "render"),
                             help="片头片尾由结构写死：模型只写正文，这几句在整期"
                                  "定稿那一刻由程序粘上去，不进生成、也不进任何门禁"),
    "intro_outro.review": _p("bool", False, "script", "前期回顾",
                             views=("script",),
                             help="开启后，在片头之后、正文之前拼三句前期回顾"
                                  "（上期标题 / 期主旨 / 前三段段主旨），逐字引用、"
                                  "程序拼、模型不参与、也不进任何门禁。"
                                  "第 1 期、上一期不在本项目、或上一期没留下段主旨时"
                                  "这三句都不出现"),

    "gate.max_deviation_pct": _p("float", 15.0, "gate", "总时长偏差阈值（%）",
                                 min=0.5, max=50, step=0.5, unit="%",
                                 views=("script",),
                                 help="长集按此比例判定；短集另受绝对下限保护。"
                                      "这一条是软门禁：不达标只记录，不阻断生成"),
    "gate.min_deviation_seconds": _p("float", 20.0, "gate", "总时长绝对容差（秒）",
                                     min=0.0, max=120, step=1.0, unit="秒",
                                     views=("script",),
                                     help="实际允许的偏差 = max(目标×百分比, 本值)。"
                                          "短集句数少，每句几字之差就超过百分比，只按比例判会永远不过"),
    "gate.min_chars": _p("int", 8, "gate", "单句最少字数",
                         min=1, max=60, step=1, unit="字", views=("script",)),
    "gate.max_chars": _p("int", 40, "gate", "单句最多字数",
                         min=10, max=200, step=1, unit="字", views=("script",)),
    "gate.max_seconds_per_line": _p("float", 15.0, "gate", "单句最长时长（秒）",
                                    min=1, max=60, step=0.5, unit="秒",
                                    views=("script",)),

    # ---- 声音 ----
    "tts.engine": _p("enum", "edge", "voice", "语音引擎", views=("render",),
                     help="两个引擎的授权不同：Edge-TTS 的客户端库是 LGPL-3.0，"
                          "但它调用的是微软 Edge 浏览器的在线语音接口（非官方公开 API，"
                          "无授权凭据），合成出的音频受微软服务条款约束；"
                          "Qwen3-TTS 的权重与代码是 Apache-2.0（阿里云通义千问），"
                          "在本地运行，不经过任何第三方服务。"),
    # 音色名由后端枚举（Edge 有几百个），写不进档位表；声明成 str 表示
    # 「任意音色名都合法」，再挂 options_source 告诉界面去哪里取下拉。
    # 只声明 str 而不给来源，界面就只剩一个文本框，几百个音色得手打 ID。
    #
    # engine_scope：同一语义的参数在不同引擎下取值互不相通（Edge 用
    # zh-CN-XiaoxiaoNeural，本地用 Serena），必须各存一份，否则切换引擎
    # 会把另一边的选择覆盖掉。界面按当前引擎只显示对应那一份——同屏永远
    # 只有一个「A 角音色」，不会让人分不清哪个在生效。
    "tts.voice_a": _p("str", "zh-CN-XiaoxiaoNeural", "voice", "A 角音色（Edge）",
                      options_source="voices", engine_scope="edge",
                      views=("script", "render"),
                      help="仅在引擎选 Edge-TTS 时出现。音色名沿用微软的 ShortName"),
    "tts.voice_b": _p("str", "zh-CN-YunyangNeural", "voice", "B 角音色（Edge）",
                      options_source="voices", engine_scope="edge",
                      views=("script", "render"),
                      help="仅在引擎选 Edge-TTS 时出现"),
    "tts.name_a": _p("str", "小思", "voice", "A 角称呼", views=("script", "render")),
    "tts.name_b": _p("str", "小笔", "voice", "B 角称呼", views=("script", "render")),
    "tts.speed_a": _p("float", 1.0, "voice", "A 角语速",
                      min=0.5, max=2.0, step=0.05, unit="倍",
                      views=("script", "render")),
    "tts.speed_b": _p("float", 1.0, "voice", "B 角语速",
                      min=0.5, max=2.0, step=0.05, unit="倍",
                      views=("script", "render")),
    "tts.qwen3tts_host": _p("str", "127.0.0.1", "voice", "语音服务地址",
                            engine_scope="qwen3tts"),
    "tts.qwen3tts_port": _p("int", 9880, "voice", "语音服务端口",
                            min=1, max=65535, step=1,
                            engine_scope="qwen3tts"),
    "tts.qwen3tts_voice_a": _p("str", "Serena", "voice", "A 角音色（Qwen3-TTS）",
                               options_source="voices", engine_scope="qwen3tts",
                               views=("script", "render"),
                               help="仅在引擎选 Qwen3-TTS 时出现。"
                                    "音色由本地服务 /speakers 枚举"),
    "tts.qwen3tts_voice_b": _p("str", "Uncle_Fu", "voice", "B 角音色（Qwen3-TTS）",
                               options_source="voices", engine_scope="qwen3tts",
                               views=("script", "render"),
                               help="仅在引擎选 Qwen3-TTS 时出现"),
    "tts.unload_llm_before_synth": _p(
        "bool", True, "voice", "合成前腾显存", engine_scope="qwen3tts",
        help="合成前卸载 LM Studio / Ollama 的驻留模型。本地 TTS 上 GPU 要占约 5GB，"
             "和 LLM 同时在场会抢显存；语音合成本来用不到语言模型。"
             "机器资源充足、想一边跑 LLM 一边合成的，关掉此项"),

    "audio.sample_rate": _p("enum", 44100, "voice", "采样率（Hz）"),
    "audio.bitrate_kbps": _p("int", 192, "voice", "码率（kbps）",
                             min=64, max=320, step=8, unit="kbps"),
    "audio.channels": _p("enum", 1, "voice", "声道数"),
    "audio.codec": _p("enum", "mp3", "voice", "音频编码"),
    "audio.loudnorm_target": _p("float", -14.0, "voice", "响度目标（LUFS）",
                                min=-24, max=-6, step=0.5, unit="LUFS"),
    "audio.denoise": _p("bool", True, "voice", "降噪"),
    "audio.pause_between_lines": _p("float", 0.35, "voice", "句间停顿（秒）",
                                    min=0.0, max=3.0, step=0.05, unit="秒"),
    "tts.throttle_seconds": _p("float", 0.4, "voice", "逐句请求间隔（秒）",
                               min=0.0, max=3.0, step=0.1, unit="秒",
                               help="连续高频请求会被语音服务限流，逐句合成之间留出间歇"),
    "tts.max_retries": _p("int", 4, "voice", "单句重试次数", min=1, max=8, step=1, unit="次"),
    "audio.intro_path": _p("path", "", "voice", "片头音频", pick="audio",
                           help="本机绝对路径，留空即不使用。整期音频按「声明 → 片头 → "
                                "正文 → 片尾」拼，这个文件接在声明之后；它的时长会从"
                                "脚本目标时长里扣掉"),
    "audio.outro_path": _p("path", "", "voice", "片尾音频", pick="audio",
                           help="本机绝对路径，留空即不使用。拼在整期音频的最末尾；"
                                "它的时长同样从脚本目标时长里扣掉"),
    "aigc.labeling": _p("bool", True, "voice", "AIGC 合规标识",
                        help="按《人工智能生成合成内容标识办法》写入元数据隐式标识"
                             "（AIGC 字段），并在片头拼一句 AI 语音声明；封面背景的"
                             "显式水印不受此开关影响。开启后内容制作者为必填项"),
    "aigc.content_producer": _p("str", "", "voice", "内容制作者", required=True,
                                help="法定必填（GB 45438-2025）：元数据七要素中"
                                     "Label、ContentProducer、ProduceID 必填，标签与"
                                     "编号由程序负责，唯独制作者只能由你填——写你自己"
                                     "的名称或编码，不是模型名、不是工具名；留空则拒绝出片"),
    "audio.ai_disclosure_text": _p("str", "本节目人声由人工智能合成。", "voice", "AI 声明文案",
                                   help="片头语音声明的内容；声明排在片头音频之前，"
                                        "字幕时间轴已自动包含它的时长"),
    "audio.ai_disclosure_path": _p("path", "", "voice", "自定义声明音频", pick="audio",
                                   help="本机绝对路径。留空则用 A 角音色按声明文案合成；"
                                        "填了则直接用该文件（这条填错会直接报错，不会"
                                        "悄悄退回合成）"),

    "bgm.mode": _p("enum", "builtin", "voice", "背景音乐来源"),
    "bgm.preset": _p("enum", "pensive", "voice", "背景音乐档位",
                     preview_base="/api/bgm/",
                     help="试听按钮播放的是该档位的原始循环素材，成片里会被"
                          "拉长混音、垫在人声底下"),
    "bgm.custom_path": _p("path", "", "voice", "自备音乐文件", pick="audio",
                          help="本机绝对路径，留空即不使用。只在上面「背景音乐来源」"
                               "选「自备文件」时生效；**文件不在等同于没填**——成品"
                               "会没有背景音乐（这一条不会报错，所以界面上会提醒）"),
    "bgm.volume": _p("float", 0.50, "voice", "音乐音量",
                     min=0.0, max=1.0, step=0.01,
                     help="默认值由实测确定：素材已统一到 -23 LUFS，此音量下"
                          "音乐平均比人声低约 13 dB，落在播客垫底的常规区间"),
    "bgm.ducking": _p("bool", True, "voice", "人声闪避",
                      help="人声起时自动压低音乐"),
    "bgm.duck_threshold": _p("float", 0.25, "voice", "闪避触发阈值",
                             min=0.001, max=0.5, step=0.001,
                             help="超过该电平才触发压低；原值 0.03 远低于人声峰值，"
                                  "人声一起就压满，音乐再也抬不起来"),
    "bgm.duck_ratio": _p("float", 2.0, "voice", "闪避压缩比", min=1.0, max=20.0, step=0.5, unit="倍",
                         help="压低幅度；配合 400 ms 固定恢复时间，比值越大"
                              "音乐越难在句间停顿里回升"),
    "bgm.fade_seconds": _p("float", 3.0, "voice", "淡入淡出（秒）",
                           min=0.0, max=10.0, step=0.5, unit="秒"),

    # ---- 画面 ----
    "video.width": _p("int", 1920, "frame", "横屏宽", min=320, max=3840, step=2, unit="像素"),
    "video.height": _p("int", 1080, "frame", "横屏高", min=240, max=2160, step=2, unit="像素"),
    "video.fps": _p("enum", 30, "frame", "视频帧率（fps）"),
    "video.encoder_preset": _p("enum", "medium", "frame", "编码预设"),
    "video.crf": _p("int", 20, "frame", "画质（CRF）", min=0, max=51, step=1),
    "video.produce_vertical": _p("bool", True, "frame", "同时产出竖屏"),
    "video.bg_dim": _p("float", 0.15, "frame", "背景压暗",
                       min=0.0, max=0.9, step=0.05),

    "background.preset": _p("enum", "ink", "frame", "背景档位"),
    "animation.mode": _p("enum", "static", "frame", "动画档位"),
    "animation.zoom_max": _p("float", 1.04, "frame", "缓推终点倍率",
                             min=1.0, max=1.20, step=0.01, unit="倍"),
    "speaker_indicator.mode": _p("enum", "style", "frame", "说话人提示"),
    # 角色名称独立成一个开关，不再由「说话人提示」的某一档兼职：
    # 从前名字只在「字幕色区分」那一档里出现（侧栏色块/自备立绘两档反而没有），
    # 于是「谁在说」这件事在四档之间飘。现在它是唯一判据 `speaker_name_shown`，
    # 画面字幕与 LRC 共用——LRC 没有样式层，名字是它唯一能区分说话人的办法。
    "speaker_indicator.name_shown": _p("bool", True, "frame", "角色名称",
                                       help="在字幕句首写上说话人名字（A/B 取「声音」里的称呼）。"
                                            "与版式无关：选了哪一档都按这个开关走"),
    "speaker_indicator.color_a": _p("str", "#C9A45C", "frame", "A 角色彩"),
    "speaker_indicator.color_b": _p("str", "#6FA8DC", "frame", "B 角色彩"),
    "speaker_indicator.portrait_a": _p("path", "", "frame", "A 角立绘 PNG", pick="image",
                                       help="本机绝对路径的透明 PNG，留空即不使用。"
                                            "只在「说话人提示」选「自备立绘」时生效："
                                            "该说话人开口的句子里出现在左下角，其余时间不显示。"
                                            "两张必须同尺寸同高才会一样大——立绘高 = 源图高 × 42%；"
                                            "两张都缺时退回「侧栏色块」"),
    "speaker_indicator.portrait_b": _p("path", "", "frame", "B 角立绘 PNG", pick="image",
                                       help="同上，B 角那一张（右下角）"),

    # 画面上的字（背景、封面）与字幕各有一款字体，两个下拉并排放在「字体」卡片里。
    # 从前画面字体写死成「本机第一个可用字体」，人看得见字、却选不了它用哪款。
    # 两项的 section 都是 frame（见 SECTION_OF_OVERRIDE）。
    "frame.font_family": _p("str", "", "frame", "画面字体",
                            help="背景与封面上的字用这款。留空则自动选择可用字体"),
    "subtitle.preset": _p("enum", "lyric", "frame", "字幕版式"),
    "subtitle.font_family": _p("str", "", "frame", "字幕字体",
                               help="字幕用这款。留空则自动选择可用字体"),
    "subtitle.font_size": _p("int", 52, "frame", "横屏字号", min=16, max=140, step=2, unit="像素"),
    "subtitle.font_size_vertical": _p("int", 40, "frame", "竖屏字号", min=16, max=140, step=2, unit="像素"),
    # 字幕这几项「一套配置管两档」：两档都是**一个固定宽高的半透明框**，文字在
    # 框里——歌词档纵向排、单行滚动档横向走。每一档的语义只剩一处差别（轴），
    # 所以下面每项的标签与 help 只写一套含义，不再有「单双行＝…；歌词＝…」这种
    # 两义并列（见 CHANGELOG 0.42.0 的接驳表）。
    "subtitle.margin_lr": _p("int", 90, "frame", "字幕框左右边距",
                             min=0, max=500, step=5, unit="像素",
                             help="框左右各收这么多，同时决定框内一行能放几个字"),
    "subtitle.margin_v": _p("int", 90, "frame", "横屏框底距边",
                            min=0, max=800, step=5, unit="像素",
                            help="框底距画面底边多远"),
    "subtitle.margin_v_vertical": _p("int", 220, "frame", "竖屏框底距边",
                                     min=0, max=1200, step=5, unit="像素",
                                     help="竖屏那一份（横竖两幅各配一套）"),
    "subtitle.outline": _p("int", 2, "frame", "字幕框描边宽",
                           min=0, max=30, step=1, unit="像素",
                           help="框四周那一道淡边的粗细"),
    "subtitle.bg_alpha": _p("int", 128, "frame", "字幕框透明度",
                            min=0, max=255, step=1,
                            help="整块框填充的透明度。0 全实、255 全透"),
    "subtitle.color_a": _p("str", "&HFFFFFF", "frame", "A 角字幕色",
                           help="A 说的句子的字幕颜色。只有「说话人提示」选"
                                "「AB两套字幕样式」时才生效"),
    "subtitle.color_b": _p("str", "&HFFFFFF", "frame", "B 角字幕色",
                           help="B 说的句子的字幕颜色。两色配成同一个颜色即等于不分色"),

    # ---- 高亮：当前台词的重点色 ----
    # 只有一个色：高亮是「盖在底色上的一层」。非当前句走自己的本色（说话人档的
    # A/B 色，或「无」档的单色）并压暗——不能只换当前句的色而不压暗其余：
    # A/B 默认都是纯白，比金色更亮，不压暗就成了「高亮的那句反而更暗」。
    "subtitle.highlight": _p("bool", False, "frame", "高亮",
                             help="把当前这句台词染成高亮配色。歌词版式下染的是"
                                  "「正在读的那一句」；单行滚动版式下屏幕上只有"
                                  "当前句，等于给全部字幕换个色"),
    "subtitle.highlight_color": _p("str", "#FFD98A", "frame", "高亮配色",
                                   help="当前句的颜色，接受 #RRGGBB"),

    # ---- 框与滚动：两档共用一套旋钮（切档位时值跟着档位走）----
    # window / frame_rows 是框的形状：歌词档取这里，单行滚动档由档位表写死 1 / 1
    # （滚动的框按定义就是一行）。scroll_ms 只给歌词档用——横滚的速度由语速算出，
    # 不另给旋钮（见 subtitle_engine 的说明）。
    "subtitle.window": _p("int", 3, "frame", "框内窗口句数",
                          min=1, max=7, step=1, unit="句",
                          help="歌词档：框里最多显示几句（含当前句）。当前句必留，"
                               "然后先按住下面的句子、再按住上面的。"
                               "单行滚动档固定为 1 句"),
    "subtitle.frame_rows": _p("int", 8, "frame", "框高（行）",
                              min=2, max=24, step=1, unit="行",
                              help="歌词档：**含空行**——每句占 1 个空行 + 它自己折的"
                                   "行数，放不下就往远里丢句子。它同时就是框高"
                                   "（行数 × 行距）。单行滚动档固定为 1 行"),
    "subtitle.scroll_ms": _p("int", 600, "frame", "上滚时长",
                             min=0, max=3000, step=50, unit="毫秒",
                             help="歌词档：换句时整块自下而上滑到新位置用的时间，"
                                  "0 即不滚、直接跳。单行滚动档不用这一项"
                                  "（它的横滚速度跟着语速走）"),

    "cover.preset": _p("enum", "book", "frame", "封面档位"),

    # ---- 系统 ----
    # 节目名与副标题不在这里：它们归项目，每个节目一套，在项目设置里填。
    # 这里只留「所有项目都一样」的那几行字——品牌行、标语、版权行。
    # 期标题也不在这里：项目模式取自地图对应那一期，人不用填两遍。
    "project.brand": _p("str", "", "system", "品牌行", views=("render",)),
    "project.tagline": _p("str", "", "system", "标语", views=("render",)),
    "project.output_dir": _p("str", "projects", "system", "产物目录", views=("render",)),
    "project.attribution": _p("str", "", "system", "版权行", views=("render",)),
    "llm.backend": _p("enum", "lm-studio", "system", "LLM 后端", views=("script",)),
    "llm.base_url": _p("str", "", "system", "API 地址", views=("script",)),
    "llm.api_key": _p("str", "", "system", "API Key", views=("script",)),
    "llm.model": _p("str", "", "system", "模型名", options_source="models",
                    views=("script",),
                    help="从本机可用模型里选，也可以直接填一个列表里没有的名字"),
    "llm.temperature": _p("float", 0.8, "system", "温度",
                          min=0.0, max=2.0, step=0.05, views=("script",)),
    "llm.max_tokens": _p("int", 8192, "system", "最大输出",
                         min=512, max=65536, step=256, unit="token", views=("script",)),
    "llm.input_ratio": _p("float", 1.0, "system", "输入倍率",
                          min=0.5, max=16.0, step=0.5, unit="倍", views=("script",),
                          help="单次调用能装多少原文 = 最大输出 × 本倍率。这是"
                               "唯一决定「一次调用装多少」的旋钮，与画地图的压比"
                               "无关（那管的是「这一期该讲多少料」）。后端窗口"
                               "有多大不归程序管，也不需要告诉它——倍率由你按自己"
                               "的后端定。调小 → 原文被多切几块，不丢料、只是多写"
                               "几段；调大 → 一次喂更多"),
    # 两个量各管各的，别用一个数兼两件事：
    #   内容额度（画地图用）= 成稿目标 × 压缩档 × 1.25，参照系是成稿脚本，
    #     判「这一期该讲多少料」，不合格要拆期——由 probe.capacity 推导，
    #     不进配置项（它是业务结论，不是旋钮）。
    #   输入额度（写作侧用）= llm.max_tokens × llm.input_ratio，参照系是模型
    #     后端，判「这一次调用装不装得下原文」——就是上面那一项。
    # 从前把后者删掉、让前者去顶班，于是两个不同单位的数被拿来做减法再比大小。
    # llm.max_tokens 只是输出预算（思考段与答案共用），与输入额度分开。
    # 两道闸门分工不同，别用一个数兼两件事：「多久没有下一个字」判后端是不是
    # 卡死，「整件事最久允许多久」判模型写得是不是太久。从前只有后者，且回复
    # 要等全篇才到，于是「模型慢慢写」和「后端死了」被同一个数一起判死——
    # 写一期长稿必然撞墙，日志上却写着「后端响应超时」。
    "llm.idle_timeout": _p("int", 300, "system", "静默超时（秒）",
                           min=30, max=3600, step=30, unit="秒", views=("script",),
                           help="多久没有收到下一个字就判后端卡死。本地模型预填充"
                                "长素材时可能安静几分钟才吐第一个字，按最坏情况定"),
    "llm.timeout": _p("int", 3600, "system", "总时限（秒）",
                      min=60, max=14400, step=60, unit="秒", views=("script",),
                      help="一次生成最多允许多久。写满一整期长稿可能要半小时以上，"
                           "这里卡住的是「永远吐不完」，不是「慢」"),
}


# ============================================================================
# 界面分区：卡片归属与顺序 —— 由点位名前缀推导，另可显式覆盖
# 界面不再手工枚举「哪个控件放哪个盒子」，那正是漏项与错位的来源。
# ============================================================================
SECTION_LABELS = {
    "script": "写作目标", "gate": "门禁阈值", "intro_outro": "片头片尾",
    "llm": "语言模型", "project": "项目",
    "tts": "角色与音色", "audio": "音频输出", "bgm": "背景音乐",
    "aigc": "AIGC 标识",
    "video": "画面输出", "background": "背景", "animation": "动画",
    "speaker_indicator": "说话人提示", "subtitle": "字幕", "cover": "封面",
    "frame": "字体",
}

# 卡片顺序。未列出的分区排在最后，按名称排序。
SECTION_ORDER = [
    "script", "gate", "intro_outro",
    "project", "llm",
    "tts", "audio", "bgm", "aigc",
    "video", "background", "animation", "speaker_indicator", "subtitle", "cover",
    "frame",
]

# 分区的详细程度：simple 的分区在阶段页收起，只在「配置」页展开全部。
SECTION_NOTE = {
    "script": "决定脚本写多长、写成什么调子",
    "gate": "不达标的脚本直接拦下，不进入合成",
    "intro_outro": "片头尾由结构写死，整期定稿那一刻粘上；模型只写正文",
    "llm": "脚本由本地或自建模型生成，此处指定用哪一个",
    "project": "品牌行、标语、版权行——每期画面上都印的那几行字",
    "tts": "两位主持人的称呼、音色与语速。音色随所选引擎切换——同屏只显示当前引擎那一套",
    "audio": "导出的音频文件参数；码率受采样率的物理上限约束",
    "bgm": "背景音乐的来源、音量与人声闪避",
    "aigc": "AI 生成内容的合规标识：元数据隐式标识与片头语音声明"
            "（《人工智能生成合成内容标识办法》，2025-09-01 施行）",
    "video": "画幅、帧率与编码质量",
    "background": "背景配色档位",
    "animation": "背景是否运动。播客重点不在画面，默认静止",
    "speaker_indicator": "画面如何区分当前说话人",
    "subtitle": "字幕版式、字号与边距",
    "cover": "封面档位；封面上的字与背景共用同一份，取自项目与配置",
    "frame": "画面与字幕各用哪款字体。下拉里每款都按自己的字形渲染，一眼可见长什么样",
}


# 没显式声明 views 时按组归类。表里查不到的组必须在点位上自己写 views——
# 留白会让点位悄悄丢掉阶段归属，等到界面按阶段过滤时才暴露，那时已经晚了。
VIEW_STAGES = ("script", "render")
VIEW_FALLBACK = {
    "script": ("script",),
    "gate": ("script",),
    "voice": ("render",),
    "frame": ("render",),
    "system": (),          # 仅配置页：项目身份、版权行这类不归任何单个阶段
}


def _views_of(key, spec):
    """该点位出现在哪些阶段页。显式声明优先，其次按组推导。"""
    if "views" in spec:
        return list(spec["views"])
    group = spec.get("group")
    if group not in VIEW_FALLBACK:
        raise ValueError(
            "%s 既没声明 views，group=%r 也不在 VIEW_FALLBACK 里；"
            "请二选一补齐，不要让它悄悄没有阶段归属。" % (key, group))
    return list(VIEW_FALLBACK[group])


# 卡片归属的例外。默认按命名空间前缀归（subtitle.* 一律归「字幕」），但字体是
# 另一类东西：画面与字幕各一款，分在两张卡片就得跑两个地方去挑字号长什么样。
# 归到一张「字体」卡片，两个下拉并排，谁管哪一块一眼可见。
# 卡片归属的例外表。键名带上「subtitle.」前缀不等于它归字幕卡：
# A/B 角字幕色只有「说话人提示」的「AB两套字幕样式」那一档在读，把它留在字幕卡，
# 就等于「选在哪、配色在哪」隔了一张卡——改的人要来回翻。整体归到说话人提示卡。
SECTION_OF_OVERRIDE = {"subtitle.font_family": "frame",
                       "subtitle.color_a": "speaker_indicator",
                       "subtitle.color_b": "speaker_indicator"}


def section_of(key):
    """卡片归属：取点位名的命名空间前缀，例外见上表。"""
    return SECTION_OF_OVERRIDE.get(key) or key.split(".", 1)[0]


def ui_payload():
    """界面元数据：分区标签、顺序、说明、各点位的归属。"""
    return {"section_labels": SECTION_LABELS, "section_order": SECTION_ORDER,
            "section_note": SECTION_NOTE}

GROUPS = {
    "script": "脚本",
    "gate": "门禁",
    "voice": "声音",
    "frame": "画面",
    "system": "系统",
}


# ============================================================================
# GATE_SPEC：门禁条目（判据 / 级别 / 阶段）
# stage: generate（脚本生成阶段）| render（产物阶段）
# level: fail（阻断）| warn（记录，严格模式下也阻断）
# ============================================================================
GATE_SPEC = {
    "items": [
        {"key": "json_valid", "label": "JSON 合法", "judge": "可解析为数组",
         "level": "fail", "stage": "generate"},
        {"key": "fields_complete", "label": "字段完整",
         "judge": "speaker / emotion / text 齐备且非空", "level": "fail",
         "stage": "generate"},
        {"key": "emotion_vocab", "label": "语篇词表",
         "judge": "emotion 只能填语篇词表里的词（承接/追问/解释/强调/比喻/铺垫/过渡/总结）",
         "level": "fail", "stage": "generate"},
        {"key": "ab_run_limit", "label": "同一人连续句数",
         "judge": "同一人连着说的句数 ≤ 对话形式给的上限（A / B 各一条）",
         "level": "fail", "stage": "generate"},
        {"key": "line_length", "label": "句长区间",
         "judge": "gate.min_chars ≤ 字数 ≤ gate.max_chars", "level": "fail", "stage": "generate"},
        {"key": "line_duration", "label": "单句时长",
         "judge": "≤ gate.max_seconds_per_line", "level": "fail", "stage": "generate"},
        {"key": "banned_words", "label": "禁用词",
         "judge": "BANNED_RULES 未命中", "level": "fail", "stage": "generate"},
        {"key": "readable_text", "label": "可朗读",
         "judge": "台词不含 emoji / 图标 / 不可见字符 / 网址 / 命令参数 / 排版符号",
         "level": "fail", "stage": "generate"},
        {"key": "line_end_punct", "label": "句尾标点",
         "judge": "每句以终止标点（。！？…）收尾；补什么符号由语义定，py 只判有没有",
         "level": "fail", "stage": "generate"},
        # 这里本来还有一条 intro_outro。撤掉了：片头尾现在由程序在**整期定稿那一刻**
        # 逐字粘上去（见 script_engine.glue_intro_outro），模型根本没参与，也就没有
        # 「它照不照做」可验——判据留在这里只会恒真，或者更糟：拿正文去验首末句，
        # 每次都报「未命中」，然后让模型去改一句它压根没写过的句子。
        # soft：这一条判的是估算值，不是实测值——真章要等合成出来。估算系数带着
        # 校准误差，拿它把稿子打回重写，等于让模型围着一个它既测不出、也控不住的
        # 秒数反复改。所以不达标只记账，不参与放行（严格模式下也不拦）。
        {"key": "total_duration", "label": "总时长偏差",
         "judge": "≤ max(目标×gate.max_deviation_pct, gate.min_deviation_seconds)",
         "level": "warn", "soft": True, "stage": "generate"},
        # 内容检：跑在脚本生成阶段内（不是等产物出来之后），不通过就回灌重写。
        # 与上面那些形式项同属一个阶段，但判法不同——这些由模型判，形式项由代码判。
        {"key": "check_semantic", "label": "语义检（LLM）",
         "judge": "正文台词忠于素材、未编造（片头尾不在稿子里，不必豁免）",
         "level": "fail", "stage": "generate"},
        {"key": "check_promise", "label": "承诺链检（LLM）",
         "judge": "正文开头提出的设问或承诺，在后文有回应",
         "level": "warn", "stage": "generate"},
        {"key": "av_sync", "label": "音画时长差",
         "judge": "|视频时长 − 音频时长| ≤ 0.05 秒", "level": "fail", "stage": "render"},
        {"key": "bitrate_match", "label": "实测码率",
         "judge": "== audio.bitrate_kbps", "level": "fail", "stage": "render"},
        {"key": "rate_match", "label": "实测采样率",
         "judge": "== audio.sample_rate", "level": "fail", "stage": "render"},
        {"key": "wrap_ok", "label": "断词率", "judge": "== 0", "level": "fail", "stage": "render"},
        {"key": "font_ok", "label": "字体族名",
         "judge": "在可用字体表内", "level": "fail", "stage": "render"},
        {"key": "watermark_ok", "label": "背景水印",
         "judge": "右上角像素检测通过", "level": "fail", "stage": "render"},
        {"key": "assets_complete", "label": "产物完整性",
         "judge": "视频 / 竖版 / 音频 / 封面 齐备", "level": "fail", "stage": "render"},
        {"key": "cover_sizes", "label": "封面三尺寸",
         "judge": "16:9 与 3:4 与 1:1 齐备", "level": "warn", "stage": "render"},
    ],
}

GATE_BY_KEY = {g["key"]: g for g in GATE_SPEC["items"]}


# ============================================================================
# 配置对象
# ============================================================================
def default_config():
    """由 PARAM_SPEC 派生默认配置（扁平键）。"""
    return {k: v["default"] for k, v in PARAM_SPEC.items()}


def _atomic_write_json(path, obj):
    """UTF-8 原子写入：先写临时文件再替换，避免半截文件。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def resolve_base_url(cfg):
    """由配置推导 LLM API 地址。

    唯一的推导入口，接受 dict。管理器与调用方都走这里，
    避免 dict 与管理器两种用法各写一份而其中一份必然失效。
    """
    url = cfg.get("llm.base_url")
    if not url:
        be = cfg.get("llm.backend")
        url = MODE_SPEC["llm.backend"]["options"].get(be, {}).get("default_base_url", "")
    return url


class ConfigManager:
    """配置读写。优先级：CLI 覆盖 > config.json > PARAM_SPEC 默认值。"""

    def __init__(self, path=None):
        self.path = path or CONFIG_PATH
        self._data = default_config()
        self._overrides = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    # 迁移必须跑在白名单过滤**之前**：改过名的旧键（如
                    # `subtitle.lyric_window`）已不在 PARAM_SPEC 里，过滤会把它
                    # 连同值一起丢掉——人配好的窗口会静默回到默认值。
                    self._migrate(saved)
                    for k, v in saved.items():
                        if k in PARAM_SPEC and v is not None:
                            self._data[k] = v
            except (json.JSONDecodeError, OSError):
                # 配置损坏不静默吞掉：备份后用默认值，并在启动日志里可见
                bak = self.path + ".corrupt"
                try:
                    shutil.copy2(self.path, bak)
                except OSError:
                    pass
        self._migrate(self._data)
        return self._data

    @staticmethod
    def _migrate(data):
        """存量配置里已下线的键与值，读到就改掉（下次保存时落盘）。

        两件事，都在这里做，别处不许再写一份：

        1. **版式三档并两档**（0.42.0）：`dual`（双行）已从版式里删除，
           `dual_named`（双行带名）更早下线。两个旧值都搬到 `lyric`——旧配置里
           存着 `dual` 的多半是当年的默认值，人没主动选过；留在配置里会变成
           一个不在档位表内的值（界面上那个下拉会显示成空）。
           `dual_named` 还多一件事：名字归「角色名称」开关，旧值的人当初点它的
           目的是「要名字」，所以顺手把那个开关打开，意图不丢。
        2. **歌词那三个键去掉歌词前缀**（0.42.0）：窗口与框高两档共用，
           改名为 `subtitle.window` / `subtitle.frame_rows` / `subtitle.scroll_ms`。
           不搬就丢值——旧键不在 PARAM_SPEC 里，会被白名单过滤掉。
        """
        if data.get("subtitle.preset") in ("dual", "dual_named"):
            if data.get("subtitle.preset") == "dual_named":
                data.setdefault("speaker_indicator.name_shown", True)
            data["subtitle.preset"] = "lyric"
        for old_key, new_key in (("subtitle.lyric_window", "subtitle.window"),
                                 ("subtitle.lyric_max_rows", "subtitle.frame_rows"),
                                 ("subtitle.lyric_scroll_ms", "subtitle.scroll_ms")):
            if old_key in data:
                v = data.pop(old_key)
                data.setdefault(new_key, v)
        return data

    def save(self):
        _atomic_write_json(self.path, self._data)

    # -- 取值 --
    def get(self, key, fallback=None):
        if key in self._overrides:
            return self._overrides[key]
        if key in self._data:
            return self._data[key]
        if key in PARAM_SPEC:
            return PARAM_SPEC[key]["default"]
        return fallback

    def set(self, key, value):
        if key not in PARAM_SPEC:
            raise KeyError("未知配置项: %s" % key)
        self._data[key] = coerce(key, value)
        return self._data[key]

    def update(self, patch):
        """部分键合并写入，返回被拒绝的键列表。"""
        rejected = []
        for k, v in (patch or {}).items():
            if k not in PARAM_SPEC:
                rejected.append(k)
                continue
            try:
                self._data[k] = coerce(k, v)
            except (TypeError, ValueError) as e:
                rejected.append("%s (%s)" % (k, e))
        return rejected

    def override(self, key, value):
        """CLI 覆盖，不落盘。"""
        if value not in (None, ""):
            self._overrides[key] = coerce(key, value)

    def resolve_base_url(self):
        return resolve_base_url(self._data)

    def data(self):
        return dict(self._data)


def param_options(key):
    """枚举点位的选项 —— 全项目唯一入口。

    返回 [{"value": 原值, "label": 中文标签, ...}]，值保留声明时的类型
    （采样率是整数就不能变成字符串，否则回写时值域判定会失配）。

    标签来源只有 MODE_SPEC 一处；PARAM_SPEC 里再写 options 会被本函数忽略，
    这是刻意的：同一个量两处来源，迟早对不上。
    """
    spec = PARAM_SPEC.get(key)
    if spec is None:
        raise KeyError("未知配置项: %s" % key)
    table = MODE_SPEC.get(key, {}).get("options")
    if table is None:
        table = spec.get("options")
    if not table:
        raise ValueError("%s 声明为枚举，却没有档位定义" % key)
    if isinstance(table, dict):
        items = [dict({"value": k}, **(v if isinstance(v, dict) else {"label": v}))
                 for k, v in table.items()]
    else:
        items = [{"value": v, "label": str(v)} for v in table]
    for it in items:
        it.setdefault("label", str(it["value"]))
    return items


def coerce(key, value):
    """按 PARAM_SPEC 声明做类型转换与值域校验（唯一入口）。"""
    spec = PARAM_SPEC.get(key)
    if spec is None:
        raise KeyError("未知配置项: %s" % key)
    t = spec["type"]
    if t in ("int", "float"):
        try:
            v = int(value) if t == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError("需要数字")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            raise ValueError("低于下限 %s" % lo)
        if hi is not None and v > hi:
            raise ValueError("高于上限 %s" % hi)
        return v
    if t == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if t == "enum":
        opts = param_options(key)
        for it in opts:
            # 表单回传一律是字符串，这里按声明类型归一后再比，
            # 否则 44100 与 "44100" 会被判成两个不同的档位。
            if it["value"] == value or str(it["value"]) == str(value):
                return it["value"]
        raise ValueError("不在档位内: %s" % "、".join(str(o["value"]) for o in opts))
    return "" if value is None else str(value)


def validate_config(cfg):
    """对一份完整配置做整体校验，返回 (警告列表, 错误列表)。

    音频码率与采样率的物理约束在此拦截——不自动改数，只报错。
    """
    warns, errs = [], []
    sr = cfg.get("audio.sample_rate", 44100)
    br = cfg.get("audio.bitrate_kbps", 192)
    codec = cfg.get("audio.codec", "mp3")
    ceil = bitrate_ceiling(sr, codec)
    if br > ceil:
        errs.append(
            "%s 在 %d Hz 下码率上限为 %d kbps，当前配置 %d kbps 不可达"
            "（编码器会静默钳制）。请调整采样率或码率。"
            % (codec.upper(), sr, ceil, br)
        )
    if sr < 32000 and codec == "mp3":
        warns.append("%d Hz 属低采样率档，成品音质受限；播客建议 44100 Hz。" % sr)

    mode = cfg.get("bgm.mode")
    if mode == "custom" and not cfg.get("bgm.custom_path"):
        errs.append("背景音乐来源选择了自备文件，但未指定文件路径。")

    if cfg.get("tts.speed_a") and cfg.get("tts.speed_b"):
        ratio = max(cfg["tts.speed_a"], cfg["tts.speed_b"]) / min(cfg["tts.speed_a"], cfg["tts.speed_b"])
        if ratio > 1.5:
            warns.append("A/B 语速差异超过 1.5 倍，听感上会像两个人抢话。")

    # 路径类点位填了就必须在。管线一律拿 os.path.exists 判它：片头音频、片尾音频、
    # 立绘、自备音乐这几类文件不在，就**静默当没填**——成品里什么都不出现，也不报错；
    # 只有 AI 声明音频会直接报错。静默那几处最坑，人以为填上了。所以统一在这里说一句：
    # 指哪一条、指哪个文件。
    for key, spec in PARAM_SPEC.items():
        if spec.get("type") != "path":
            continue
        val = str(cfg.get(key) or "").strip()
        if val and not os.path.exists(val):
            warns.append("%s：找不到这个文件 —— %s（成品里会当作没填）"
                         % (spec["label"], val))

    return warns, errs


def modes_payload():
    """下发给前端的档位表（含 label 与说明）。"""
    out = {}
    for key, spec in MODE_SPEC.items():
        opts = []
        for it in param_options(key):
            opts.append(it)
        out[key] = {"label": spec["label"], "options": opts,
                    "default": PARAM_SPEC.get(key, {}).get("default")}
    return out


def params_payload():
    """按阶段归拢的点位表，界面据此渲染，不再手工枚举控件去向。

    每项带 section（卡片归属）与 views（出现在哪些阶段页）。
    """
    stages = {"script": {}, "render": {}, "config": {}}
    for key, spec in PARAM_SPEC.items():
        item = dict(spec)
        item.pop("options", None)          # 枚举选项一律从 param_options 取
        item["key"] = key
        item["section"] = section_of(key)
        item["views"] = _views_of(key, spec)
        if spec["type"] == "enum":
            item["options"] = param_options(key)
        stages["config"].setdefault(item["section"], []).append(item)
        for view in item["views"]:
            stages[view].setdefault(item["section"], []).append(item)
    return stages


def presets_payload():
    return {"dims": STYLE_DIMS, "presets": PRESET_SPEC}


def gates_payload():
    return {"items": GATE_SPEC["items"]}
