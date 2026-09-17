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

"""结构探查：把一份素材扫成结构化 IR，供排地图使用。

排地图失败的头号原因不是模型不行，是**没人告诉它这批素材该怎么组织**。
一份稿件先是「有哪些层、哪一层算一个单元、哪些是正文哪些是附录」，然后才
谈得上排期。这些事实必须先在探查这一步落成数据，而不是让模型一边猜结构
一边排期。

## 三层分工

- **L1 形式证据**（纯代码，确定性）：层级归一、编号族归纳、杂项识别、
  切料可切性。同一份素材跑两次，结果必须一样。
- **L2 语义判定**（一次分类调用）：这是什么类型的素材、哪一层是切分单位。
  是**分类任务不是生成任务**——输出只有三个字段，不受长输出预算的摆布
  （排图那次「一会儿 8 千 token、一会儿 1 千」，正是把长输出押在一次调用上）。
- **L3 单元凝缩**（每个单元一次调用）：把这一节压成它的**逻辑骨架**——说清了什么、
  反对什么、有哪几条主线，以及跟着每条主线的细节区分。是**保逻辑的有损压缩**，
  不是写摘要：排图那一步只看凝缩、不看原文，凝缩没留住的后面永远补不回来。
  逐个单元调用，不是把全书押在一次输出上：一次调用要凝缩几十万字，模型只能
  按比例各切一刀，重点必然漂；逐个调用每次只看一节，**上下文与输出都不随全书
  体量增长**，慢的是调用次数，不是单次等待。凝缩结果就地写回单元并落盘。
  按哪种文体压，由范式卡的 `condense` 一项给出。
- **L4 人判定**：结果落界面，可改、可冻结。探查必然有错，**错的代价必须可逆**。

## 字数有两把尺子，不能混

| 字段 | 量的是什么 | 谁在用 |
|------|-----------|--------|
| `own_chars` | 直属正文：标题到下一个**任意**标题 | 判空壳标题、认目录页 |
| `chars` | **自含正文**：标题到下一个**同级或更高级**标题 | 准入、体量折算、取料 |

`chars` 的口径与 `ingest.slice_by_anchor` **逐字一致**——那才是真正取料时切走的
范围。从前这里数的是直属正文，于是有子节的章节把它所有的子节都让了出去：H2 层
实测差 2.61 倍，83 个有内容的 H2 被当成空壳挡在准入外，那几节从此不在任何一期里，
而地图看上去完整。

## 防错靠什么

1. **冲突不抹平**。层级不连续、序号跳号、疑似目录页，一律写进 `warnings`
   摆出来——冲突点就是信息量最大的地方，取一个等于把这条信息扔了。
2. **不猜**。认不出结构就报「未识别」，给全文一段并说明，**不套一套默认
   结构上去**。假结构比没结构更坏：它会一路传到切章、排图、取料。
3. **杂项默认不入节目**。参考文献、索引、术语表不是「排不进去」的问题，
   是「没人判断该不该排」。探查把它们挑出来单列，要的人自己勾。
4. **准入前移**。落点切不出非空正文的，在排图**之前**就挡掉，不等排完图
   再回头清理（前置规范优于后验证）。
5. **可复现**。`probe.json` 里记下模型名、提示词版本、证据来源与人的修改。
   换模型后发现结果漂了，能追到是哪一步漂的。
"""

import json
import os
import re
import time

from . import duration_model, ingest, llm_client, source_store

HEAD_CHARS = 280
#: IR 版本。2 起：字数分「直属 / 自含」两把尺子，单元带凝缩结果。3 起：单元
#: 带原文位置（起止行）与所属章节链——排图要按章节上下合并与切分、凝缩要能
#: 说清压的是哪一块。版本换了读旧文件只能按旧口径解释，位置与取料对不上，
#: 所以旧版本一律当没探查过。
IR_VERSION = 3

# 默认不入节目的行。这不是「排不进去」，是「不该默认排进去」——术语表、
# 索引、参考文献是查资料的入口，不是可听的内容。要的人自己勾上。
MISC_DROP = (
    "参考文献", "参考书目", "引用文献", "索引", "术语表", "词汇表", "版权页",
    "内容简介", "内容提要", "作者简介", "译者简介", "出版说明", "图表目录",
    "缩略语表", "目录", "凡例", "年表", "大事记", "附录",
)
# 默认入节目。序言、结语这一类是正文的一部分，播出来才完整。
MISC_KEEP = (
    "序言", "前言", "序", "自序", "代序", "导言", "引言", "绪论", "绪言",
    "导读", "阅读指南", "题记", "结语", "结论", "余论", "尾声", "后记",
    "跋", "致谢", "编后记", "译后记",
)
_APPENDIX_RE = re.compile(r"^(附录|Appendix)\s*[A-Za-z0-9\u4e00-\u9fa5]{0,3}$", re.I)
# 系列前缀：`重型技能构建的方法论沉淀：变量治理` 里的 `…沉淀` 是系列名。
# 前缀本身是编号（`1.2 判断`、`一、背景`、`第 I 部 约束`）时不算——那只是
# 标题自己的第一段。
_NUM_HEAD_RE = re.compile(
    r"^(?:第|chapter|part|section|appendix|附录|\d"
    r"|[一二三四五六七八九十百]+\s*[、.．：])", re.I)


class ProbeError(RuntimeError):
    pass


# ------------------------------------------------------------------ 层次
def _norm_map(levels):
    """量纲 → 连续层级的映射表。

    输入的量纲本来不同：`#` 的个数、单位词（篇/章/节）、编号点数。直接比较
    会得出「章比篇深」这种反的结论。按实际出现过的层级排序后重编号，
    保住相对顺序，丢掉量纲。

    **映射表必须先生成、再切块**：切块要比较两条标题谁更浅，拿未归一的量纲去比
    就是拿两种尺子量同一段布，切出来的块与取料时切走的块对不上。
    """
    seen = sorted({int(x) for x in levels})
    return {lv: i + 1 for i, lv in enumerate(seen)}




def _series_key(title):
    """从标题里取系列名候选：竖线之后、冒号之前的那一段。

    `第 III 部 · 09a｜方法论体系构建的沉淀：探测的边界——…`
    → `方法论体系构建的沉淀`

    这是作者自己写的系列名，比任何推断都硬。编号段（`1.2 判断`、`一、背景`、
    `第 I 部 约束`）不算系列名——那只是标题自己的第一段，认成系列会把互不
    相关的标题归成一组，而「同系列该合并排期」是整合依据里最硬的一条。
    """
    t = (title or "").strip()
    parts = re.split(r"[｜|]", t, maxsplit=1)
    if len(parts) == 2:
        t = parts[1].strip()
    m = re.match(r"^([^：:]{2,30})[：:]", t)
    if not m:
        return ""
    key = m.group(1).strip()
    return "" if _NUM_HEAD_RE.match(key) else key


def _series_of(segs):
    """归纳同系列。两条依据都来自文本本身，不靠模型猜。

    - **编号族**：`08a / 08b / 08c` 同一数字前缀 → 同一篇的续篇
    - **系列名**：标题里竖线之后、冒号之前那一段，如「方法论体系构建的沉淀」

    这是「整合依据」里最硬的一条：同系列的几篇该合并排期，而不是各占一期。
    """
    by_key = {}
    for s in segs:
        t = s["title"]
        bk = ingest.branch_key(t)
        if bk:
            by_key.setdefault("分支 %s" % bk, []).append(t)
            continue
        key = _series_key(t)
        if key:
            by_key.setdefault("前缀 %s" % key, []).append(t)
    return [{"key": k, "titles": v} for k, v in by_key.items() if len(v) >= 2]


def _hit(title, keywords):
    """标题是否命中这组关键词（全等或前缀）。"""
    return any(title == kw or title.startswith(kw) for kw in (keywords or []))


def _misc_of(title, card=None):
    """这条是正文、还是查资料用的附录。返回 "drop" / "keep" / ""。

    判定顺序：**卡上写的 > 通用名单**。通用名单（`MISC_DROP` / `MISC_KEEP`）
    不认类型、只认字面，它是卡没写时的兜底，不是判据的来源。

    两处结论不同的地方真实存在，不是理论上的可能：论文的「致谢」在卡上是不播的
    （感谢导师与基金，与听众无关），而通用名单把它归进保留（散文、纪实里的致谢
    确实是作者的话）。这类分歧由卡裁决——所以卡要先判。

    卡上两栏都空时（`auto` 卡）一切都落通用兜底，与接线前的行为完全一致。
    换卡只改卡上写过的那些条目，不会连带把没写过的一起改了。
    """
    t = (title or "").strip()
    if not t:
        return ""
    pr = (card or {}).get("probe") or {}
    if _hit(t, pr.get("misc_drop")):
        return "drop"
    if _hit(t, pr.get("misc_keep")):
        return "keep"
    if _hit(t, MISC_DROP) or _APPENDIX_RE.match(t):
        return "drop"
    if _hit(t, MISC_KEEP):
        return "keep"
    return ""


def _looks_like_toc(segs, i):
    """疑似目录页：连着若干标题，每一条自身都极短。

    目录页被当成正文，会在开头堆出一批「有标题没内容」的期。这里量的是
    **直属正文**而不是自含正文：目录页的每一条都是标题挨着标题，直属正文近乎
    为空；用自含正文去量，第一条会把整页目录都算进自己名下，一条都不算短，
    目录页就再也认不出来了。门槛卡在 20 字是有意的——前置页（版权页、书名、
    作者）也短，但它们不连着排六条。
    """
    run = 0
    for s in segs[i:i + 6]:
        if s["own_chars"] <= 20:
            run += 1
        else:
            break
    return run >= 6


# ------------------------------------------------------------------ L1 探查
def scan(text, marks=None, head_chars=HEAD_CHARS, card=None):
    """扫一份素材，返回 IR。这是排地图与逐期取料的唯一结构来源。

    `card` 是项目定下的那张范式卡，用来判附属页（哪些页不播）。**传 None 不报错**
    ——单集模式没有项目、还没定类型时都会走到这里，那时按通用名单判，与接线前
    一致。要求「必须给卡」，等于让没立项目的场景直接跑不起来。
    """
    text = text or ""
    if not text.strip():
        raise ProbeError("素材内容为空，无法探查结构。")
    anchors = ingest.list_anchors(text, marks)
    lines = text.split("\n")

    if not anchors:
        # **不猜。** 认不出结构就明说，给全文一段，由人决定接受等分还是
        # 换个格式重给。套一套默认结构上去，后面每一步都会跟着错。
        body = text.strip()
        n = round(duration_model.effective_chars(body))
        segs = [{"i": 0, "title": "全文", "level": 1, "raw_level": 1, "line": 0,
                 "end": len(lines), "path": "",
                 "chars": n, "own_chars": n, "raw_chars": len(body),
                 "head": body[:head_chars],
                 "evidence": "none", "confidence": 0.0, "series": "",
                 "misc": "", "ok": True}]
        return _finish(segs, "未识别到任何章节结构（没有 Markdown 标记、没有 Word "
                            "标题样式、正文里也没有编号）。已按整篇一段处理。",
                       [], head_chars)

    # 量纲先归一，再按「下一个同级或更高级标题」切块。两条规则缺一不可：
    # 不归一，比较 `#` 的个数与编号点数是拿两种尺子量同一段布；不按同级切，
    # 切出来的块比取料时切走的小——`ingest.slice_by_anchor` 取的是含子节的整块。
    nm = _norm_map([a["level"] for a in anchors])
    nlv = [nm[int(a["level"])] for a in anchors]

    segs = []
    outer = []      # [(归一层级, 标题)] 外层标题栈，用来记这一节的章节路径
    for i, a in enumerate(anchors):
        start = a["line"]
        end = len(lines)
        for j in range(i + 1, len(anchors)):
            if nlv[j] <= nlv[i]:
                end = anchors[j]["line"]
                break
        # 标题行本身不算正文。喂给模型的开头、体量统计与准入判据都取标题
        # **之后**的内容——把标题行算进去，一条「只有标题、没有正文」的
        # 条目会显得非空，准入就形同虚设，排图会为它排出一期空节目。
        body = "\n".join(lines[start + 1:end]).strip()
        # 直属正文：到下一个「任意」标题为止。判空壳标题与认目录页用它——
        # 一个标题紧接着另一个标题就是空壳，不论后者是不是它的子节。
        nxt = anchors[i + 1]["line"] if i + 1 < len(anchors) else len(lines)
        own = "\n".join(lines[start + 1:nxt]).strip()
        # 章节路径：把这一节压在哪几个外层标题底下。排图那一步要按「这一节
        # 属于哪一篇、上下是谁」来合并与切分，扁平列表里看不出这层关系。
        while outer and outer[-1][0] >= nlv[i]:
            outer.pop()
        segs.append({
            "i": i, "title": a["title"], "level": nlv[i],
            "raw_level": int(a["level"]), "line": start,
            # `end` 是这一块正文的截止行（0 起、不含），与取料
            # `ingest.slice_by_anchor` 切走的范围同口径——两处用同一把尺子，
            # 地图上标的位置才等于实际取的那一段。少了它，「凝缩的是哪一块」
            # 只有一个起点，没有终点。
            "end": end,
            "path": " › ".join(t for _lv, t in outer),
            "chars": round(duration_model.effective_chars(body)),
            "own_chars": round(duration_model.effective_chars(own)),
            "raw_chars": len(body), "head": body[:head_chars],
            "evidence": a.get("evidence") or "marker",
            "confidence": float(a.get("confidence") or 0.9),
            "series": "", "misc": _misc_of(a["title"], card), "ok": bool(body),
        })
        outer.append((nlv[i], a["title"]))

    series = _series_of(segs)
    key_of_title = {}
    for grp in series:
        for t in grp["titles"]:
            key_of_title[t] = grp["key"]
    for s in segs:
        s["series"] = key_of_title.get(s["title"], "")
    return _finish(segs, "", series, head_chars)


def _mark_outliers(segs):
    """同层里形态离群的标题，标记为疑似误标。

    误标是这么来的：作者在正文中间用一个 `#` 标注分类小节（如「# 旧：先兜底」），
    它因此落进了章标题那一层。**两个信号合起来才指认它**，单用任何一个都会误伤
    真标题——`输入`、`设置`、`输出格式` 这类无编号小节遍地都是，只按「不带编号」
    筛会把它们全标上：

    1. 同层标题大多数带编号，而此条不带
    2. 它的层级明显**回升**：前一条比它深两层以上，说明这里本该还在深结构里面

    只标不改判。离群也可能是作者真写了一个无编号的章。
    """
    for lv in {int(s["level"]) for s in segs}:
        rows = [s for s in segs if int(s["level"]) == lv]
        if len(rows) < 4:
            continue
        numbered = sum(1 for s in rows if ingest.has_numbering_head(s["title"]))
        if numbered < len(rows) * 0.6:
            continue
        for i, s in enumerate(segs):
            if int(s["level"]) != lv or s.get("misc") or s.get("warn"):
                continue
            if ingest.has_numbering_head(s["title"]):
                continue
            prev = segs[i - 1] if i else None
            if prev is not None and int(prev["level"]) >= int(s["level"]) + 2:
                s["warn"] = "疑似误标（深层结构里冒出的浅层标题，同层其余都带编号）"


def _live_flags(segs):
    """逐条标出「是否在默认不播的子树之外」。

    「默认不播」的字样只写在它自己的标题上（`附录`、`参考文献`），它的子节标题
    不带这些字。不按**祖先**判断，附录里的每一个小节都会被当成正文列进清单，
    最后排出一期念参考文献的节目，而地图上看着毫无异常。
    """
    dead, out = [], []
    for s in segs:
        lv = int(s["level"])
        while dead and dead[-1] >= lv:
            dead.pop()
        # 标记「默认不播」的那一条自己也不播——它正是「参考文献」这一节。
        out.append(not dead and s.get("misc") != "drop")
        if s.get("misc") == "drop":
            dead.append(lv)
    return out


def body_chars(ir):
    """有效正文体量：全部正文里扣掉「默认不播」的子树。

    用**直属字数**累加，不用自含字数——自含字数会父子各算一遍，同一段正文进两次，
    折出来的期数直接虚高。直属字数的各条互不重叠，加起来正好是全文。
    """
    segs = ir.get("segments") or []
    live = _live_flags(segs)
    return int(sum(float(s.get("own_chars") or 0)
                   for s, l in zip(segs, live) if l))


def _finish(segs, note, series, head_chars):
    """补齐统计与告警，拼成 IR。"""
    ev, lv = {}, {}
    for s in segs:
        ev[s["evidence"]] = ev.get(s["evidence"], 0) + 1
        lv[str(s["level"])] = lv.get(str(s["level"]), 0) + 1
    warnings = []
    if note:
        warnings.append(note)
    levels = sorted(int(k) for k in lv)
    if levels and levels != list(range(levels[0], levels[0] + len(levels))):
        warnings.append(
            "层级不连续：只识别到 %s，中间有缺层——可能某一层没被认出来。"
            % "、".join("H%d" % x for x in levels))
    for s in segs:
        s.pop("warn", None)
    _mark_outliers(segs)
    for i, s in enumerate(segs):
        if not s.get("warn") and _looks_like_toc(segs, i):
            s["warn"] = "疑似目录页"
    suspects = [s for s in segs if s.get("warn")]
    if suspects:
        warnings.append(
            "有 %d 条标题形态可疑（%s），排期前请核对。"
            % (len(suspects),
               "、".join("%s：%s" % (s["title"][:16], s["warn"])
                        for s in suspects[:4])))
    weak = sum(1 for s in segs if s["confidence"] < 0.6)
    if weak:
        warnings.append("有 %d 条标题是弱证据（编号不连续或加粗推断），请核对。"
                        % weak)
    # 标题是取料的地址，行号是门牌号。两条同名标题的地址一样、门牌号不同，
    # 地图上只显示地址，看不出取的是哪一条——改稿后行号一挪就取错了人。
    seen_t, dup_t = set(), []
    for s in segs:
        if s["title"] in seen_t and s["title"] not in dup_t:
            dup_t.append(s["title"])
        seen_t.add(s["title"])
    if dup_t:
        warnings.append(
            "有 %d 组同名标题（%s）——落点按行号定位到具体那一条，界面上却只显示"
            "同一串字；建议在素材里改成不同的标题。"
            % (len(dup_t), "、".join(x[:20] for x in dup_t[:4])))
    return {
        "version": IR_VERSION,
        "scanned": time.strftime("%Y-%m-%d %H:%M:%S"),
        "segments": segs,
        "series": series,
        "evidence": ev,
        "level_counts": lv,
        "warnings": warnings,
        "total_chars": int(sum(s.get("own_chars") or 0 for s in segs)),
        "body_chars": body_chars({"segments": segs}),
        "raw_total": sum(s["raw_chars"] for s in segs),
        "unit_level": _guess_unit_level(segs),
        "kind": "",
        "classify_reason": "",
        "probe_model": "",
        "condense_model": "",
        "condense_version": "",
        "frozen": False,
        "capacity": 0,
        "est_episodes": 0,
    }


def _guess_unit_level(segs):
    """猜哪一层是切分单位：取「条数落在能排成节目区间」的那一层。

    规则确定的兜底，L2 判定或人的修改都能盖掉它。条数用**在播且切得出正文**
    的那一份，不用 `level_counts`——那里把附录、参考文献与空壳标题也算进来
    了，拿它去比区间，量到的不是「能排成多少期」。
    """
    cost = _unit_cost(segs)
    if not cost:
        return 1
    for lv_s in sorted(cost, key=int):
        if 8 <= cost[lv_s] <= 120:
            return int(lv_s)
    return sorted(int(k) for k in cost)[0]


# ------------------------------------------------------------------ 容量
#: 压缩档的合法档位与默认档。写法统一「1:x = 压 x 倍」——成稿为 1，原文为 x；
#: 1:3 表示一期脚本最多消化 3 倍于自身的原文。三档是给人选的档位，
#: 不是连续旋钮：档位之间的小缝就近归档，加第四档只添选择负担。
RATIOS = (3, 5, 10)
DEFAULT_RATIO = 5
#: 体检上限在名义档位上再放 25%：档位是**上限制**——名义 1:5 的期实际压到
#: 6.25 倍仍算达标，超过才回炉。压比天生不均匀（故事点展开猛、数据点展不开），
#: 精确到名义值是假精确，红线之外才需要干预。
RATIO_HEADROOM = 1.25
#: 压比下限：一期素材必须**多出**脚本目标这一档，写作才有「压缩」可言——原文
#: 不够，模型就写不满，只能回头把写过的段落再背一遍（实测形态）。与上限制不同，
#: 这条线与档位无关、三档统一：它是「模型能不能照素材写」的物理线，不是「压得
#: 狠不狠」的策略线。1.2 是实测值——模型的产出与素材近乎 1:1 消耗（实测
#: 4034 ÷ 4132 ≈ 0.98），不留余量时素材等于目标也写不满，故留两成。
MIN_RATIO = 1.2


def target_chars(cfg):
    """一期脚本的**成稿目标字数**。由目标时长与标准语速反推，不问模型。

    压比体检的分母：画地图侧只回答「这一期原文 ÷ 这一期脚本」落在哪个区间，
    分母就是它。不接 calib：这一层的尺子是标准语速，跟本期用哪个音色无关。
    """
    tm = float(cfg.get("script.target_minutes", 12.0))
    return round(duration_model.chars_for_target(tm * 60.0, 1.0))


def offset_chars(cfg):
    """成稿目标的**偏移量**：成稿目标 × 1.25——只是个中间量，不是"1"。

    口径（使用者定，全链唯一的算法源）：
    - **1 = 输出 max = 每期时长 × 标准语速**（`target_chars`，不含偏移——
      它对应的是人设定的时长换算出的真实字数）；
    - **x = 输入 max = 1 × 偏移量 1.25 × 档位**（素材容量）；
    - **下限 = 1 × 偏移量 1.25 × 1.2**（25 分钟一期 ≈ 9877 字）。

    偏移量 1.25 只出现在 x 与下限的算式里，**不改变"1"**：压比体检的分母
    是 `target_chars`（见 `planner._ratio_issues`）。本函数只是把
    「× 1.25」这个公共因子收在一处，免得两处公式各写一遍、改漏一处。
    """
    return round(target_chars(cfg) * RATIO_HEADROOM)


def ratio_of(cfg):
    """压缩档位：只认 RATIOS 里的三档，非法值回默认。

    配置页是三档下拉，但配置文件可以手改——手改成 7、0 或非数时，宁可回到
    保守的默认档，也不能拿一个没对过账的倍数去画地图。
    """
    try:
        r = int(cfg.get("script.compress_ratio", DEFAULT_RATIO))
    except (TypeError, ValueError):
        return DEFAULT_RATIO
    return r if r in RATIOS else DEFAULT_RATIO


def capacity(cfg):
    """一期素材的**名义容量**：成稿目标字数 × 压缩档。

    三把尺子共用这一个口径，改一处三处同变：
    - 单元下钻（`pick_units`）：单条超过名义容量才往下一层拆；
    - 排图参照（`planner._map_prompt`）：一期素材大致这么多字，上限另加 25%；
    - 期数折算（`estimate_episodes`）：素材总量 ÷ 名义容量。

    压比体检不用名义值判生死——用上限（×1.25）与下限（1.2）两条红线，见
    `planner._ratio_issues`。期数也仍由内容结构定：拿容量去除总字数**定期数**，
    等于把分组变成除法——那是朗读软件干的事，不是这档节目干的事。

    从前的容量是「一期成稿字数」：隐含原文一字换脚本一字的 1:1 假设。照它
    分组，一期只装得下 6000 字原文，而实际一期脚本能消化几倍于此的素材——
    尺子系统性偏小，地图要么把内容切得稀碎，要么干脆对体量视而不见。口径
    升级成压缩档制后，画地图的尺子与脚本侧的压缩现实对齐。
    """
    return round(target_chars(cfg) * float(ratio_of(cfg)))


def estimate_episodes(total, per_ep):
    """按体量折算的期数**参照值**。确定性：同一份素材两次算出同一个数。

    它只用来说事：排图前算产能（素材撑不撑得起计划的期数）、排完图之后跟
    实际期数比一比。**它不参与决定排几期**——期数是合并与切分的结果，不是
    除法算出来的。
    """
    per_ep = float(per_ep or 0)
    if per_ep <= 0:
        return 1
    return max(1, int(round(float(total or 0) / per_ep)))


def fill_capacity(ir, cfg):
    """把容量与建议期数写进 IR（就地改，返回 IR）。"""
    per = capacity(cfg)
    ir["capacity"] = per
    body = body_chars(ir)
    ir["body_chars"] = body
    ir["est_episodes"] = estimate_episodes(body or ir["total_chars"], per)
    return ir


# ------------------------------------------------------------------ 准入
def admissible(ir):
    """排图准入：哪些条目能拿去排图。

    落点必须切得出非空正文。这件事在排图**之前**做——等到排完图再回头清理，
    模型已经按一条切不出料的落点写过要点了，那张地图从中间就是空的。
    返回 (可用条目, 被挡下的条目)。
    """
    ok, bad = [], []
    for s in ir.get("segments") or []:
        if s.get("ok") and s["chars"] > 0:
            ok.append(s)
        else:
            bad.append(s)
    return ok, bad


# ------------------------------------------------------------------ L2 判定
PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "unit_level": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["kind", "unit_level", "reason"],
}

PROBE_SYSTEM = """你在为一份稿件做结构勘察，判断它属于哪一类素材、以及哪一层
才是「一个完整的表达单元」。**层级按这类素材的文体判据来判**——这个类型里
什么算一个完整的表达单元，落在哪一层就是哪一层；不许凭标题的具体程度往下猜。
你只做判断，不改内容。只输出 JSON。"""


def _structure_brief(segs, per_level=14):
    """结构清单：每一层有多少条、各长什么样。

    模型要判「哪一层才是一个完整的表达单元」，它必须看得见**层级本身**。
    原先抽的是跳着取的扁平标题串，层级关系全乱——模型只能靠「这个标题像不
    像一个完整话题」去猜，而越具体的标题越像，于是层级越判越细。

    **最外一层全给**：那一层是「篇」的候选，也正是模型要在其中做选择的对象，
    抽样抽掉中间几条，正好把最该看的主体章节抽走了。更深的层才抽样。
    """
    buckets = {}
    for s in segs:
        buckets.setdefault(int(s["level"]), []).append(s.get("title") or "")
    levels = sorted(buckets)
    lines = []
    for lv in levels:
        ts = buckets[lv]
        if lv == levels[0] or len(ts) <= per_level * 2:
            shown = list(ts)
        else:
            shown = (ts[:per_level]
                     + ["…（此处省略 %d 条）…" % (len(ts) - per_level * 2)]
                     + ts[-per_level:])
        lines.append("H%d（共 %d 条）：%s" % (lv, len(ts), " / ".join(shown)))
    return "\n".join(lines)


def _kind_blocks(paradigms_map):
    """每种素材类型的**文体判据**。

    这一步判的是「按哪一级凝缩」，凭据只能是文体——这个类型里什么算一个
    完整的表达单元。原先只把类型的**名字**递过去，判据一个字没给，等于让
    模型看着标题猜。
    """
    out = []
    for k, v in sorted((paradigms_map or {}).items()):
        if k == "auto":
            continue
        pr = v.get("probe") or {}
        rows = ["【%s】%s" % (k, v.get("label") or k)]
        if v.get("unit"):
            rows.append("- 凝缩单位：%s" % v["unit"])
        for key, title in (("split", "切分依据"), ("merge", "整合依据"),
                           ("focus", "重点判据"), ("flow", "推进方式")):
            if v.get(key):
                rows.append("- %s：%s" % (title, v[key]))
        if pr.get("unit_hint"):
            rows.append("- 层级期望：%s" % pr["unit_hint"])
        if pr.get("numbering"):
            rows.append("- 编号体系：%s" % pr["numbering"])
        out.append("\n".join(rows))
    return "\n\n".join(out)


def _unit_cost(segs):
    """各层「真正会被凝缩的条数」。

    用**在播且切得出正文**的条数，不用 `level_counts`——那里含附录、参考文献
    与空壳标题，拿它算出来的数不是这一步的代价。模型看一眼就知道选哪层要花
    多少次调用。
    """
    live = _live_flags(segs)
    cnt = {}
    for s, l in zip(segs, live):
        if l and s.get("ok"):
            lv = int(s["level"])
            cnt[lv] = cnt.get(lv, 0) + 1
    return cnt


def classify(ir, llm, cfg, paradigms):
    """L2：判定素材类型与切分单位。结果写回 IR。

    **temperature 固定低值**并记下模型名：同一份素材两次探查必须给出同一套
    结果。用排图那个 0.8 去做分类，探查本身就不可复现，后面所有基于它的
    争论都无从对质。
    """
    segs = ir.get("segments") or []
    if not segs or ir.get("frozen"):
        return ir
    cost = _unit_cost(segs) or {}
    cost_txt = "、".join("H%d：%d 条" % (k, v) for k, v in sorted(cost.items()))
    prompt = (
        "【稿件结构】（本次要勘察的稿件，按标题层级列出）\n\n%s\n\n"
        "【各层按文体真正要凝缩的条数】（选 unit_level 要付的代价；"
        "附录、参考文献、只有标题没有正文的空壳不计入）\n\n%s\n\n"
        "【可选素材类型与各自的文体判据】（从这里挑一个填 kind）\n\n%s\n\n"
        "【当前任务：判断三件事】\n\n"
        "1. kind：属于上面哪一类（填 key）。拿不准填 auto。\n"
        "2. unit_level：**按这个类型的文体判据**，哪一层 H 才是「一个完整的表达"
        "单元」，填层级数字。\n"
        "   - 判据是文体：这个类型里「凝缩单位」指什么，落在哪一层就是它；"
        "「层级期望」是同一件事的另一种说法。\n"
        "   - 这一层选完就定了凝缩的颗粒：上面「真正要凝缩的条数」就是代价。"
        "选细了不只多花时间与调用——单元被切碎之后，后面把碎片拼回「一期」，"
        "比按天然的整体分组更容易拼歪。\n"
        "   - **不要因为「某个标题看起来更像一个完整话题」就往下多选一层**，"
        "那正是判细的来处。\n"
        "3. reason：一句话说明依据，点出你用的是哪一条文体判据。\n\n"
        "只输出 JSON：{\"kind\":\"methodology\",\"unit_level\":1,"
        "\"reason\":\"...\"}" % (_structure_brief(segs), cost_txt,
                                 _kind_blocks(paradigms)))
    try:
        raw, meta = llm.chat(
            [{"role": "system", "content": PROBE_SYSTEM},
             {"role": "user", "content": prompt}],
            temperature=0.1,
            # 会思考的后端把思考段与答案段记在同一份输出预算里。预算给 800 时
            # 答案部分一个字也轮不上，类型判定永远退回规则推断，L2 这层等于没跑。
            # 与凝缩、排图、写作取同一个全局口径，不在这里另写小值。
            # 探查结果会冻结落盘，这份开销一份素材只花一次。
            max_tokens=int(cfg.get("llm.max_tokens", 8192)),
            json_schema=PROBE_SCHEMA)
    except Exception as e:                                   # noqa: BLE001
        # 探查是建议性的：模型不可用不该拦住用户，退回规则推断并说明。
        ir["warnings"] = list(ir.get("warnings") or []) + [
            "类型判定未完成（模型不可用：%s），按规则推断。" % str(e)[:80]]
        return ir
    text = (raw or "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e <= s:
        ir["warnings"] = list(ir.get("warnings") or []) + [
            "类型判定没有给出可解析的结果，按规则推断。"]
        return ir
    try:
        data = json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        ir["warnings"] = list(ir.get("warnings") or []) + [
            "类型判定的 JSON 解析失败，按规则推断。"]
        return ir
    kind = str(data.get("kind") or "").strip()
    ir["kind"] = kind if kind in paradigms else "auto"
    try:
        lv = int(data.get("unit_level"))
        if lv in {int(k) for k in ir["level_counts"]}:
            ir["unit_level"] = lv
    except (TypeError, ValueError):
        pass
    ir["classify_reason"] = str(data.get("reason") or "").strip()
    if meta and meta.get("model"):
        ir["probe_model"] = str(meta["model"])
    return ir


# ------------------------------------------------------------------ 单元
def loc_text(s):
    """一个单元在原文里的位置：「第 a–b 行」。

    行号 1 起、闭区间，与取料 `ingest.slice_by_anchor` 切走的范围同口径
    （含标题行）。凝缩的是哪一块、地图上标的落点是哪一块、写脚本时取的是
    哪一块，三处必须是同一个答案——否则地图看着完整，取的却是别的段落。

    优先用凝缩回填的 `loc`（那是取料实测的范围）；没有它就退回结构自算的
    起止行。两者都缺时不编一个范围出来，只说从哪一行起。
    """
    loc = s.get("loc") or {}
    if loc.get("start"):
        a, b = int(loc["start"]), int(loc.get("end") or 0)
        return "第 %d–%d 行" % (a, max(a, b))
    ln = s.get("line")
    if ln is None:
        return ""
    a = int(ln) + 1
    e = s.get("end")
    if not e:
        return "第 %d 行起" % a
    return "第 %d–%d 行" % (a, max(a, int(e)))


def units(ir):
    """切分单位那一层、且通过准入的条目——就是「一个完整的表达单元」。

    只取**正好那一层**，不把父层一并列出：父层的自含正文已经把它所有的子节
    包括在内，两层同时列出等于同一段正文要排两次期。同一层的各条互不重叠，
    加起来正好是全文。
    """
    lv = int(ir.get("unit_level") or 1)
    live = _live_flags(ir.get("segments") or [])
    out = []
    for s, l in zip(ir.get("segments") or [], live):
        if not l or int(s["level"]) != lv or not s.get("ok"):
            continue
        out.append(s)
    return out


def pick_units(ir, per_ep=0):
    """取料单元：切分单位那一层；某一条装不下一期时，按下一层结构下钻。

    两个规则合起来定单元：

    1. **切分单位那一层**（`unit_level`，探查判定或人指定）。
    2. **装不下一期就下钻一层**：单条体量超过一期容量时，用它的下一层结构展开。
       「一条链不许拆开」是不把论证劈成两片残骸，不是要把《第 I 部》整部塞进
       一期——一期吞掉几倍于时长的原文，写出来的脚本必然又长又空。

    返回的是 **segment 对象本身**（非副本）：凝缩结果就地写回，随后随 IR 落盘。
    """
    segs = ir.get("segments") or []
    if not segs:
        return []
    live = _live_flags(segs)
    lv = int(ir.get("unit_level") or 1)
    out = []
    for i, s in enumerate(segs):
        if not live[i] or int(s["level"]) != lv or not s.get("ok"):
            continue
        out.extend(_fit(segs, live, i, lv, per_ep))
    return out


def _fit(segs, live, i, lv, per_ep):
    """装得下整块取，装不下按下一层展开。"""
    s = segs[i]
    if not per_ep or float(s.get("chars") or 0) <= per_ep:
        return [s]
    kids = []
    for j in range(i + 1, len(segs)):
        t = segs[j]
        if int(t["level"]) <= lv:
            break
        if live[j] and int(t["level"]) == lv + 1 and t.get("ok"):
            kids.append(j)
    if not kids:
        # 已经是最后一层，拆无可拆。整块交出去，标记出来让排图阶段在告警里说清：
        # 这一期会明显超时长，要么调大单期时长，要么在素材里给这一节分小节。
        s["oversize"] = True
        return [s]
    out = []
    for j in kids:
        out.extend(_fit(segs, live, j, lv + 1, per_ep))
    return out


# ------------------------------------------------------------------ L3 凝缩
#: 凝缩结果的结构。`concepts` 是「本节提出的、会在别处复用的概念」——排图要
#: 靠它判断哪几节在讲同一件事，光看标题看不出来。
CONDENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "gist": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "concepts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["gist", "points"],
}

#: 凝缩提示词版本。落在 IR 里——换版本后发现地图漂了，能追到是这一步漂的。
#: 版本一变，带旧版本号的凝缩结果**按没有处理**，重新凝缩：凝缩的取舍标准改了，
#: 留着旧结果会让同一张图上出现两套标准排出来的期。
#: 1 → 2：删掉「points 3 到 6 条」这条凑数要求。
#: 2 → 3：**口径从「挑重点的摘要」改成「保逻辑的有损压缩」**。摘要是从一节里
#:         挑几条最重要的，压完只剩骨架的骨架；而排图那一步**只看凝缩、不看
#:         原文**，凝缩没留住的，后面永远补不回来——两节讲同一条论证链的不同
#:         环节，各自的摘要都写着同一句空话，模型就看不出它们该合。所以撤掉
#:         「40 字内」这类上限，改成「说清什么、反对什么、几条主线、该有的
#:         细节区分都要留」，并给出一条完整的填充示例。
#:         同时：凝缩规则从 `focus` 字段里拆出来，单独用 `condense`。
CONDENSE_VERSION = "3"

CONDENSE_SYSTEM = """你在为一档播客准备素材：把一个结构单元凝缩成它的**逻辑骨架**，
供后面排图（决定哪几节该合成一期）与改写脚本取用。

你做的是**保逻辑的有损压缩**，不是写摘要：留下这一节说清了什么、反对什么、
有哪几条主线，以及跟着每条主线的细节区分（数字、条件、边界、前提、反例）。
压掉的是举例的过程、重复的论证、过渡性的叙述。**例子本身是论据时，它的结论要留。**

不改写、不评价、不补充材料里没有的内容。只输出 JSON。"""


def _condense_prompt(title, rule, text):
    """凝缩那一步的提示词。

    **文本里的百分号要写 `%%`**：整段是用 `%` 拼的，一个裸 `%` 会被当成格式符
    （示例里写「超标 40%」就会在运行时炸掉，而且报错指向的是格式化而不是提示词，
    很难从现象追到这里）。
    """
    return """把下面这一节凝缩出来，供后续排图与改写脚本取用。

本节标题：%s

【凝缩依据】（这批素材的文体，决定凝缩的颗粒与取舍）

%s

【要做到什么程度】（这是这一节的交付标准，逐条满足）

后面的人**只读这份凝缩来排图**——哪几节合成一期、哪几节该切开，都看它。
原文不会再进那一步。所以：

- 主干要留全：说清了什么、反对什么；**有几条主线就留几条**，并列的条目不许
  并成一句。
- **该有的细节区分要留住**：数字、条件、边界、前提、反例、结论的适用范围，
  要跟着它所属的那条主线一起写进去。丢了它们，两节看着就像在讲同一件事。
- 压掉的是**解释与铺陈**：举例的过程、重复的论证、过渡叙述。

自检：把两节的凝缩放在一起——**能不能看出它们是同一条论证链的两段，
或者看出它们讲的是两件不同的事**？看不出，就是压过头了。

**不要把主干写成「说明了 X 的重要性」这样的空话**：那是把主语抄了一遍，
没留下任何可用来判断的内容。

【输出】（只输出 JSON 对象，不要输出别的字）

{"gist":"这一节的主线，一句话",
 "points":["主干条目，一条一件事","下一条主干","有几条主干就写几条"],
 "concepts":["本节提出或反复使用的概念、术语；没有就填空数组"]}

- `gist`：这一节的主线，一句话。**不是对它的评价**——不要写「本节论述了……」。
- `points`：这一节的逻辑骨架，**按材料里的先后顺序**排。每条自成一个可判断的
  陈述，该带的数字、条件、边界、反例写在同一条里。**不设条数上限**：有几条
  主干写几条；也不要把一条主干拆成两条来凑数。
- `concepts`：本节提出或反复使用的、后面还会用到的概念或术语。没有就给 `[]`。

示例（供参照格式，内容与本次要凝缩的材料无关）：

{"gist":"故障集中在电源模块，不是传感器",
 "points":["三台设备都在通电瞬间报错，稳态下正常——问题出在供电建立的过程中",
  "换过传感器，故障码不变——排除了传感器本身",
  "电源模块在低温下启动电流超标 40%%，室温复现不了——这是直接原因",
  "结论：加预热电路即可，不必换电源模块"],
 "concepts":["启动电流","故障码"]}

【本节原文】（要凝缩的内容）

%s""" % (title, rule, text)


def _condensed(s):
    """这一节的凝缩结果是不是**按当前版本**做的。

    只看「有没有 gist」不够：凝缩的取舍标准写在提示词里，提示词一改，旧结果
    就成了另一套标准下的产物。留着它，同一张地图上会混着两套标准排出来的期，
    而且从界面上看不出来。版本号就是为这件事留的。
    """
    return bool(s.get("gist")) and s.get("condense_version") == CONDENSE_VERSION


def _mark_loc(s, pos):
    """把这一单元在原文里的位置记到 segment 上。返回 1 表示与结构自算的不符。

    位置不是另算的，是取料那一步**顺手就有的**：`slice_by_anchor` 返回的
    `line_start`/`line_end`/`chars` 就是它切走的那一段。原先取了就扔，于是
    「凝缩压的是哪一块」在数据里只剩一个起点、没有终点。

    这里回填，并跟结构自算的 `line`/`end` 对一次账：两处口径一旦漂开，地图
    上标的位置与实际取的那段就对不上了——位置会变成假证据。
    """
    if not pos:
        return 0
    start = int(pos.get("line_start") or 0)      # 1 起，含标题行
    end = int(pos.get("line_end") or 0)          # 1 起，闭区间
    s["loc"] = {"start": start, "end": end, "chars": int(pos.get("chars") or 0)}
    if start and (start != int(s.get("line") or 0) + 1
                  or (s.get("end") and end != int(s["end"]))):
        return 1
    return 0


def condense_units(base, pid, units, llm, cfg, card, force=False, log=None,
                   attempts=2, progress=None):
    """逐单元凝缩（L3）。返回 (完成数, 跳过数)。

    **一次调用只看一节**。把全书押在一次输出上，模型面对几十万字只能按比例
    各切一刀，重点必然漂，而且输出越长越不听话（09e：拉得动的是分布结构，
    不是篇幅）。逐个调用每次只带一节原文，上下文与输出都不随全书体量增长。

    `units` 是 `[(sid, segment)]`，segment 就地写入 `gist`/`points`/`concepts`。
    已有凝缩结果的单元直接跳过——重排一次图不该把 N 次调用再烧一遍。
    `progress(text, frac)` 在每个单元开工前上报局部进度（frac 0~1），给后台
    任务的进度条用；缺席就什么都不做。

    原文走 `slice_by_anchor` 取，与写脚本那一步取的是同一段：凝缩看的与写的
    是同一种料，中间不再有一次口径转换。

    失败是**致命**的，不做降级：凝缩结果正是排图分组的依据，拿不到它还要往下
    走，排出来的图看着完整，实际是按字数硬切出来的——那才是假功能。

    这一步的口径是**保逻辑的压缩**，不是写摘要：排图那一步只看凝缩、不看原文
    （见 `planner._units_text`），凝缩没留住的，后面永远补不回来。所以「说清了
    什么、反对什么、有几条主线、该有的细节区分」都要留住；按哪种文体压，由
    范式卡的 `condense` 一项给出。

    **不干预模型要不要思考**：用推理型还是普通模型由使用者自己定，这里只把
    要求写清楚、把结果接住。真烧了推理额度，日志里的 `reasoning_tokens` 记着。
    """
    log = log or (lambda m: None)
    # 凝缩依据取范式卡里**专给凝缩**的那一项。取 `focus` 就错了：那里的口径是
    # 「挑」（什么值得单开一期），与凝缩要的「不丢」方向相反——混用会让模型先
    # 按挑办事，该留的细节区分被当成次要信息丢掉，而排图再也看不到它们。
    rule = (card or {}).get("condense") or ""
    texts = {}

    def material(sid, title, line):
        """一个单元的取料原文（含子节、含标题行），按行号定位。

        同一份稿子里同名标题不罕见，只按标题取会取到靠前那一条——凝缩看的
        与写脚本要写的必须是同一段料，否则地图上的重点对不上任何一期的正文。
        同一份素材只读一次盘。
        """
        if sid not in texts:
            texts[sid] = (source_store.read_source(base, pid, sid),
                          source_store.marks_of(base, pid, sid))
        text, marks = texts[sid]
        body, pos = ingest.slice_by_anchor(text, title, marks, line=line)
        return body, pos

    done = skipped = drift = 0
    for n, (sid, s) in enumerate(units, 1):
        if progress:
            progress("凝缩 %d/%d：%s" % (n, len(units),
                                        (s.get("title") or "")[:24]),
                     (n - 1) / float(len(units) or 1))
        if not force and _condensed(s):
            skipped += 1
            # 老结果没有位置（那时取了就扔）。补位置只读本地盘、不调模型，
            # 顺手补上——不然清单里这一条只有凝缩、没有「压的是哪一块」。
            if not s.get("loc"):
                _body, pos = material(sid, s["title"], s.get("line"))
                _mark_loc(s, pos)
            continue
        body, pos = material(sid, s["title"], s.get("line"))
        drift += _mark_loc(s, pos)
        last = ""
        for _ in range(max(1, attempts)):
            try:
                raw, meta = llm.chat(
                    [{"role": "system", "content": CONDENSE_SYSTEM},
                     {"role": "user", "content": _condense_prompt(
                         s.get("title") or "", rule, body)}],
                    temperature=0.1,
                    # 凝缩的输出比摘要长（逻辑骨架要留细节区分），预算不能抠：
                    # 会思考的后端的思考段与答案段共用这份预算，抠了会「答案一个字
                    # 也轮不上」而静默退化成空——整张地图就按空重点分组了。
                    max_tokens=int(cfg.get("llm.max_tokens", 8192)),
                    json_schema=CONDENSE_SCHEMA)
                data = _json_obj(raw, "凝缩「%s」" % (s.get("title") or "")[:20])
            except Exception as e:                               # noqa: BLE001
                last = str(e)
                continue
            s["gist"] = str(data.get("gist") or "").strip()
            s["points"] = _str_list(data.get("points"))
            s["concepts"] = _str_list(data.get("concepts"))
            if not s["gist"]:
                last = "凝缩结果里没有主旨"
                continue
            s["condense_version"] = CONDENSE_VERSION
            if meta and meta.get("model"):
                s["condense_model"] = str(meta["model"])
            done += 1
            # 思考额度一并报出来：不按模型类型分叉，但代价要看得见——逐单元调用
            # 会把它乘以单元数，日志里没这个数就查不出一次排图为什么慢。
            rt = int((meta or {}).get("reasoning_tokens") or 0)
            log("凝缩 %d/%d：%s → %s%s"
                % (n, len(units), (s.get("title") or "")[:24], s["gist"][:40],
                   "（思考 %d token）" % rt if rt else ""))
            break
        else:
            raise ProbeError(
                "「%s」这一节凝缩失败：%s。凝缩结果是排图的依据，拿不到它排出"
                "来的图只能按字数硬切，所以在这里停下。" % (s.get("title"), last))
    if drift:
        log("位置对账：有 %d 处凝缩取料的行号范围与结构自算的不一致——地图上的"
            "落点按取料那一段为准，建议核一眼。" % drift)
    return done, skipped


def stamp_condense(ir):
    """把凝缩这一步的来历写到 IR 上：哪个版本的提示词、哪个模型。

    段一级已经各记一份；IR 上这一份是给「这张图是怎么来的」看的——换过模型
    或换过提示词之后回头看，能一眼看出这道工序是不是同一套。
    """
    segs = ir.get("segments") or []
    models = [s.get("condense_model") for s in segs if s.get("condense_model")]
    ir["condense_version"] = CONDENSE_VERSION if any(_condensed(s) for s in segs) else ""
    ir["condense_model"] = models[0] if models else ""
    return ir


def _str_list(value):
    if isinstance(value, str):
        value = [value]
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def _json_obj(raw, what):
    """取 LLM 输出里的 JSON 对象。取不到抛 ProbeError，不返回空表。

    解析交给 `llm_client.extract_json`：它逐个花括号试解析，思考段混在答案
    里的输出也取得到。凝缩一次调用只看一节，失败已是最贵的一种——不能因为
    「思考里写过一个模板」把好答案判成坏的。
    """
    data = llm_client.extract_json(raw)
    if data is None:
        raise ProbeError("%s：模型输出里没有可解析的 JSON 对象（%s）"
                         % (what, (raw or "").strip()[:120]))
    return data


# ------------------------------------------------------------------ 落盘
def probe_path(base, pid, sid):
    return os.path.join(source_store.source_dir(base, pid),
                        "%s.probe.json" % sid)


def save(base, pid, sid, ir):
    path = probe_path(base, pid, sid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ir, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load(base, pid, sid):
    """读落盘的探查结果。**版本对不上当没探查过**，返回 None 让调用方重扫。

    旧版本的字数只有一把尺子（直属正文），自含体量与取料对不上，拿它排图会
    把有子节的章节算小、把附录的子节算进正文。宁可重扫一次，也不把两种口径
    混在一张图上。
    """
    path = probe_path(base, pid, sid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            ir = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ProbeError("探查结果无法解析：%s（%s）" % (path, e))
    if int((ir or {}).get("version") or 0) != IR_VERSION:
        return None
    return ir


# ------------------------------------------------------------------ 项目级
def _project_card(base, pid, cfg, paradigms_map):
    """项目定下的那张范式卡。取不到返回 `None`，那时按通用名单判附属页。

    **必须在扫结构之前取到。** 附属页是扫标题那一步判的，而类型判定在它之后；
    先扫再按类型重判，等于把同一份素材的标题判两遍，统计与告警还得挨个重算，
    而重算漏一处就出现「告警说某条可疑、它其实已经不在判断里」这类自相矛盾。
    项目范式立项时就定了，读它不依赖探查结果，没有这个来回的必要。

    取卡的顺序与排图完全一致（见 `paradigms.chosen_of`），而且只有这一条通道：
    各判一遍迟早分叉——卡上写着「致谢不播」，探查按通用名单放行、排图又按卡
    去排，就排出一期该念的致谢，而两处都不会报错。

    `paradigms_map` 是给调用方注入自定义卡表用的；没传就取内置那张表。**不能
    因为没传就返回 None** ——那等于「忘了传参」与「项目没定类型」表现成同一个
    结果，卡静默失效，从日志到产物都看不出哪里不对。
    """
    try:
        from . import project_store
        item = project_store.find(base, pid)
    except Exception:                                        # noqa: BLE001
        # 项目记录读不出来（单集模式、目录异常）不该拦住探查：没有项目就没有
        # 项目范式，按通用名单判即可。这里不做兜底猜测。
        item = None
    from . import paradigms
    key = paradigms.chosen_of(item, cfg)
    return (paradigms_map or paradigms.PARADIGMS).get(key) if key else None


def scan_project(base, pid, cfg, llm=None, paradigms_map=None,
                 force=False, log=None, progress=None):
    """探查项目里全部素材，落盘并汇总。

    探查与排图分开走，结果冻结后落盘：排图失败（模型不可用、输出被截断）
    不至于把探查也赔进去，不必重扫一遍再重等一次。
    `progress(text, frac)` 在每份素材处理前上报局部进度（frac 0~1），
    供后台任务推进度条；缺席就什么都不做。
    """
    log = log or (lambda m: None)
    card = _project_card(base, pid, cfg, paradigms_map)
    pr = (card or {}).get("probe") or {}
    if card and (pr.get("misc_drop") or pr.get("misc_keep")):
        log("哪些页不播，按「%s」这本类型判" % card.get("label"))
    out, warns = {}, []
    sources = source_store.list_sources(base, pid)
    for i, s in enumerate(sources, 1):
        if progress:
            progress("探查 %d/%d：%s" % (i, len(sources), s["name"][:24]),
                     (i - 1) / float(len(sources) or 1))
        sid = s["id"]
        ir = None if force else load(base, pid, sid)
        if ir is None:
            text = source_store.read_source(base, pid, sid)
            ir = scan(text, source_store.marks_of(base, pid, sid), HEAD_CHARS,
                      card=card)
            log("探查 %s：%d 条标题、%d 层、证据 %s"
                % (sid, len(ir["segments"]), len(ir["level_counts"]),
                   "/".join("%s×%d" % (k, v) for k, v in ir["evidence"].items())))
        if llm is not None and paradigms_map and not ir.get("kind"):
            classify(ir, llm, cfg, paradigms_map)
            log("类型判定 %s → %s（H%d 为切分单位）"
                % (sid, ir["kind"] or "auto", int(ir["unit_level"])))
        fill_capacity(ir, cfg)
        save(base, pid, sid, ir)
        out[sid] = ir
        for w in ir["warnings"]:
            warns.append("%s：%s" % (sid, w))
    per = capacity(cfg)
    body = sum(ir["body_chars"] for ir in out.values())
    if not out:
        raise ProbeError("项目里还没有素材，无从探查。请先载入成稿。")
    return {"sources": out, "capacity": per, "body_chars": body,
            "est_episodes": estimate_episodes(body, per), "warnings": warns}

