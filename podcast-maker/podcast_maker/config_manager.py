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
    PRESET_SPEC 风格倾向预设（七维取值）
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

VERSION = "0.12.2"


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
# 这 16 个标签原本是混在一起用的，但它们不是一类东西：
#   · 7 个是真情绪（平静 / 好奇 / …）——描述「这句话是什么心情」，能转成语气；
#   · 9 个是语篇功能（开场 / 过渡 / 总结 / …）——描述「这句话在结构上干什么」，
#     跟心情无关。
# 混在一起有两个后果：语篇标签被当成语气送进 TTS，指令成了「用比喻的语气说」
# ——对模型是纯噪音；真情绪又淹没在这堆噪音里，等于语气压根没接上。所以拆开：
#   EMOTION_VOCAB   只放真情绪，供 TTS 生成语气指令
#   DISCOURSE_VOCAB 放语篇功能，供脚本侧做结构标记
#   EMOTION_TAGS    两者并集，是脚本 emotion 字段的合法取值（与拆分前完全一致）
EMOTION_VOCAB = [
    "平静", "好奇", "疑惑", "恍然", "肯定", "感慨", "轻松",
]

DISCOURSE_VOCAB = [
    "开场", "收束", "追问", "解释", "强调", "比喻", "铺垫", "总结", "过渡",
]

# 顺序与拆分前逐字相同 —— 它决定提示词里列出的枚举顺序与界面下拉顺序，
# 排一下就是一次无谓的行为变更。
EMOTION_TAGS = [
    "开场", "收束", "平静", "好奇", "追问", "疑惑", "恍然", "解释",
    "强调", "肯定", "感慨", "比喻", "铺垫", "总结", "过渡", "轻松",
]


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
# ============================================================================
INTRO_OUTRO = {
    "standard": {
        "intro_lines": 2,
        "outro_lines": 2,
        "intro_first": "欢迎收听《{program}》，面向关注方法论与认知边界的听众。",
        "outro_last": "这里是《{program}》，欢迎关注。",
    },
    "brief": {
        "intro_lines": 1,
        "outro_lines": 1,
        "intro_first": "欢迎收听《{program}》。",
        "outro_last": "这里是《{program}》，欢迎关注。",
    },
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
        "label": "说话人指示",
        "options": {
            "none": {"label": "无", "desc": "仅靠音色区分"},
            "style": {"label": "字幕色区分", "desc": "A/B 两套字幕样式 + 名字前缀"},
            "block": {"label": "侧栏色块", "desc": "左右色块按句高亮当前说话方"},
            "portrait": {"label": "自备立绘", "desc": "用户提供透明 PNG，说话方高亮"},
        },
    },
    "subtitle.preset": {
        "label": "字幕版式",
        "options": {
            "single": {"label": "单行", "max_lines": 1},
            "dual": {"label": "双行", "max_lines": 2},
            "dual_named": {"label": "双行带名", "max_lines": 2, "show_name": True},
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
# PRESET_SPEC：风格倾向（七维）—— 划界权在人，LLM 不选
# ============================================================================
STYLE_DIMS = {
    "genre": {"label": "体裁", "options": {
        "argument": "论证型", "story": "故事型", "science": "科普型",
        "debate": "对辩型", "review": "复盘型"}},
    "emotion_density": {"label": "情绪密度", "options": {"low": "低", "mid": "中", "high": "高"}},
    "question_rate": {"label": "提问频率", "options": {"low": "低", "mid": "中", "high": "高"}},
    "metaphor_density": {"label": "比喻密度", "options": {"low": "低", "mid": "中", "high": "高"}},
    "interaction": {"label": "互动词强度", "options": {
        "restrained": "克制", "natural": "自然", "warm": "热络"}},
}

PRESET_SPEC = {
    "argument": {"label": "论证型", "genre": "argument", "emotion_density": "low",
                 "question_rate": "mid", "metaphor_density": "low", "interaction": "restrained"},
    "story": {"label": "故事型", "genre": "story", "emotion_density": "high",
              "question_rate": "low", "metaphor_density": "mid", "interaction": "warm"},
    "science": {"label": "科普型", "genre": "science", "emotion_density": "mid",
                "question_rate": "high", "metaphor_density": "high", "interaction": "natural"},
    "debate": {"label": "对辩型", "genre": "debate", "emotion_density": "mid",
               "question_rate": "high", "metaphor_density": "low", "interaction": "warm"},
    "review": {"label": "复盘型", "genre": "review", "emotion_density": "low",
               "question_rate": "mid", "metaphor_density": "mid", "interaction": "natural"},
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
    "script.extra_requirement": _p("text", "", "script", "额外要求",
                                   views=("script",),
                                   help="自由文本，仅作补充说明；判据一律由门禁代码执行"),
    "script.gate_strict": _p("bool", True, "script", "门禁严格模式",
                             views=("script",),
                             help="开启后 warn 级条目也不放行"),
    "script.max_llm_rounds": _p("int", 3, "script", "回灌重试上限",
                                min=0, max=5, step=1, views=("script",),
                                help="每轮都会重跑一次内容检（语义/承诺链），轮数越多越慢"),
    "script.map_max_episodes": _p("int", 60, "script", "地图期数上限",
                                  min=1, max=500, step=1, views=("script",),
                                  help="成稿规划排地图时最多排多少期；项目已定计划期数时以项目为准"),
    "script.paradigm": _p("enum", "auto", "script", "素材类型",
                        views=("script",),
                        help="排地图时的组织依据：切分单位、整合依据、重点判据、推进方式。"
                             "拿不准选「自适应」，由探查按结构推断；项目里可以逐个改"),
    "intro_outro.preset": _p("enum", "standard", "script", "片头尾档位",
                             views=("script", "render"),
                             help="首句与末句由结构写死，不交给模型自由发挥"),

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
    "audio.intro_path": _p("path", "", "voice", "片头音频"),
    "audio.outro_path": _p("path", "", "voice", "片尾音频"),
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
    "audio.ai_disclosure_path": _p("path", "", "voice", "自定义声明音频",
                                   help="留空则用 A 角音色按声明文案合成；填了则直接用该文件"),

    "bgm.mode": _p("enum", "builtin", "voice", "背景音乐来源"),
    "bgm.preset": _p("enum", "pensive", "voice", "背景音乐档位",
                     preview_base="/api/bgm/",
                     help="试听按钮播放的是该档位的原始循环素材，成片里会被"
                          "拉长混音、垫在人声底下"),
    "bgm.custom_path": _p("path", "", "voice", "自备音乐文件"),
    "bgm.volume": _p("float", 0.18, "voice", "音乐音量",
                     min=0.0, max=1.0, step=0.01),
    "bgm.ducking": _p("bool", True, "voice", "人声闪避",
                      help="人声起时自动压低音乐"),
    "bgm.duck_threshold": _p("float", 0.03, "voice", "闪避触发阈值",
                             min=0.001, max=0.5, step=0.001),
    "bgm.duck_ratio": _p("float", 8.0, "voice", "闪避压缩比", min=1.0, max=20.0, step=0.5, unit="倍"),
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
    "speaker_indicator.mode": _p("enum", "style", "frame", "说话人指示"),
    "speaker_indicator.color_a": _p("str", "#C9A45C", "frame", "A 角色彩"),
    "speaker_indicator.color_b": _p("str", "#6FA8DC", "frame", "B 角色彩"),
    "speaker_indicator.portrait_a": _p("path", "", "frame", "A 角立绘 PNG"),
    "speaker_indicator.portrait_b": _p("path", "", "frame", "B 角立绘 PNG"),

    # 画面上的字（背景、封面）与字幕各有一款字体，两个下拉并排放在「字体」卡片里。
    # 从前画面字体写死成「本机第一个可用字体」，人看得见字、却选不了它用哪款。
    # 两项的 section 都是 frame（见 SECTION_OF_OVERRIDE）。
    "frame.font_family": _p("str", "", "frame", "画面字体",
                            help="背景与封面上的字用这款。留空则自动选择可用字体"),
    "subtitle.preset": _p("enum", "dual", "frame", "字幕版式"),
    "subtitle.font_family": _p("str", "", "frame", "字幕字体",
                               help="字幕用这款。留空则自动选择可用字体"),
    "subtitle.font_size": _p("int", 52, "frame", "横屏字号", min=16, max=140, step=2, unit="像素"),
    "subtitle.font_size_vertical": _p("int", 40, "frame", "竖屏字号", min=16, max=140, step=2, unit="像素"),
    "subtitle.margin_lr": _p("int", 90, "frame", "字幕左右边距", min=0, max=500, step=5, unit="像素"),
    "subtitle.margin_v": _p("int", 90, "frame", "横屏底部边距", min=0, max=800, step=5, unit="像素"),
    "subtitle.margin_v_vertical": _p("int", 220, "frame", "竖屏底部边距", min=0, max=1200, step=5, unit="像素"),
    "subtitle.outline": _p("int", 6, "frame", "字幕框边距", min=0, max=30, step=1, unit="像素"),
    "subtitle.bg_alpha": _p("int", 128, "frame", "字幕底框透明度", min=0, max=255, step=1),
    "subtitle.color_a": _p("str", "&HFFFFFF", "frame", "A 角字幕色"),
    "subtitle.color_b": _p("str", "&HFFFFFF", "frame", "B 角字幕色"),

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
    # 素材容量不在这里配：参照系是成稿脚本，由业务线按「成稿目标字数 × 压缩档
    # × 1.25 体检上限」推导（script_engine.material_capacity），下限恒 1:1.2。
    # llm.max_tokens 只是输出预算（思考段与答案共用），与素材容量脱钩——
    # 从前的「输入额度 = 最大输出 × 输入倍率」参照系挂错了，已删（见
    # llm_client.quota_note，窗口要求换算成具体数字提醒自查）。
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
    "speaker_indicator": "说话人指示", "subtitle": "字幕", "cover": "封面",
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
    "intro_outro": "首末句由结构写死，模型不得自由发挥",
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
SECTION_OF_OVERRIDE = {"subtitle.font_family": "frame"}


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
         "judge": "speaker / text / emotion 齐备且非空", "level": "fail", "stage": "generate"},
        {"key": "ab_run_limit", "label": "同一人连续句数",
         "judge": "连续同一说话人 ≤ 范式卡给的 max_run", "level": "fail",
         "stage": "generate"},
        {"key": "line_length", "label": "句长区间",
         "judge": "gate.min_chars ≤ 字数 ≤ gate.max_chars", "level": "fail", "stage": "generate"},
        {"key": "line_duration", "label": "单句时长",
         "judge": "≤ gate.max_seconds_per_line", "level": "fail", "stage": "generate"},
        {"key": "emotion_vocab", "label": "情绪标签",
         "judge": "取自受控词表", "level": "warn", "stage": "generate"},
        {"key": "emotion_level", "label": "情绪档位",
         "judge": "情绪标签符合文体档位：不写心情的文体只允许语篇标签与平静",
         "level": "fail", "stage": "generate"},
        {"key": "banned_words", "label": "禁用词",
         "judge": "BANNED_RULES 未命中", "level": "fail", "stage": "generate"},
        {"key": "readable_text", "label": "可朗读",
         "judge": "台词不含 emoji / 图标 / 不可见字符 / 网址 / 命令参数 / 排版符号",
         "level": "fail", "stage": "generate"},
        {"key": "intro_outro", "label": "片头片尾",
         "judge": "首句含问候语与节目名，末句含收束语与节目名",
         "level": "fail", "stage": "generate"},
        # soft：这一条判的是估算值，不是实测值——真章要等合成出来。估算系数带着
        # 校准误差，拿它把稿子打回重写，等于让模型围着一个它既测不出、也控不住的
        # 秒数反复改。所以不达标只记账，不参与放行（严格模式下也不拦）。
        {"key": "total_duration", "label": "总时长偏差",
         "judge": "≤ max(目标×gate.max_deviation_pct, gate.min_deviation_seconds)",
         "level": "warn", "soft": True, "stage": "generate"},
        # 内容检：跑在脚本生成阶段内（不是等产物出来之后），不通过就回灌重写。
        # 与上面那些形式项同属一个阶段，但判法不同——这些由模型判，形式项由代码判。
        {"key": "check_semantic", "label": "语义检（LLM）",
         "judge": "正文台词忠于素材、未编造；首末句为结构写死的片头尾语，不受本条约束",
         "level": "fail", "stage": "generate"},
        {"key": "check_promise", "label": "承诺链检（LLM）",
         "judge": "片头设问或承诺在后文有回应",
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
        return self._data

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
