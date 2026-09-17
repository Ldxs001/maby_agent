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

"""期数地图：把一部成稿排成播出计划，以及往已有计划里插新料。

**代码管三件确定的事，模型只做语义。**

| 谁 | 管什么 | 为什么不能交给模型 |
|----|--------|--------------------|
| `probe` | 结构：层级、同系列、杂项、准入 | 结构来自文本与样式，是可查的事实 |
| `probe` | 单元凝缩：每一节讲了什么 | 一次调用只带一节原文，质量不随全书体量下降 |
| 这里 | 分量：一期装多少原文（压缩档折算）、产能预检、压比体检 | 尺子是算术；交给模型，同一份素材两次排出 60 期和 5 期，两个都「合乎提示词」 |
| 这里 | 期号、落点、每期体量 | 落点要落得准；让模型抄标题，抄错一条这一期就取不到料 |
| 模型 | 分组（哪些单元合成一期）、标题、主旨、要点 | 这是语义判断，代码判不了 |

## 画地图的流程（一次调用变成一批小调用）

1. 结构从稿子里直接读出来（`#`、`一、`、Word 标题样式），不喂模型。
2. **逐个单元凝缩**：一次调用只看一节，得到「这一节讲了什么、重点有哪几条」。
3. 把凝缩结果与组织依据一起给模型，由它分组，并给每一期定标题、主旨、要点。
4. 分组落到期上：期号、落点、体量由代码算——落点是「素材 + 标题 + 行号」，
   同名标题也能定位到具体那一条。

从前是第 3 步一口气要模型读完所有章节的开头、排出全部期、还要逐条照抄落点。
输出的每一部分都可能错，错一条就丢一期；现在模型只管分组与命名，输出的东西
少得多，**错了也只错一处，且立刻能看见**。

## 插入 = 换个容器再排一次

画地图与插入是**同一个操作**，只差一个参数——往哪个容器里建：

- 画地图：容器是空根。全部素材凝缩后分组，产出 1、2、3……
- 插入：容器是选定的那一期。新素材**同样逐节凝缩**（走缓存，已凝缩的节
  不再烧第二次），在锚点期这个容器里分组，产出 3a、3b……

所以插入复用排图的全套机制：逐单元凝缩、序号分组、落点由代码填、压比体检。
地图数据里**没有父子嵌套**——期与分支期是同一层数组里的平等条目，编号上的
字母只说明它从哪一期长出来；脚本合成按期独立取料，点哪期写哪期。分支插在
**锚点分支链的末尾**（不是锚点条目正后方），后插的批永远排在先插的批后面，
编号与物理位置天然咬合。
"""

from . import llm_client, paradigms, probe, project_store, source_store

# 压比体检不达标的整体重排上限：首版之外再排 2 次，共 3 版。重排的口径见
# `_regroup_loop`——排图与插入共用同一条体检回线。
MAP_RETRIES = 2

# 单元清单的格式说明。分组只认序号：标题、行号、体量由代码取用，
# 模型抄一遍只会多一处可能抄错的地方。
UNIT_LEGEND = ("单元清单格式：`序号. [素材号] 所属 › H<层级>「标题」〔标注〕"
               "（第 a–b 行，原文 N 字）`，下面几行是这一节的凝缩：一条主线，随后是"
               "若干条主干。N 字是**凝缩前**的原文体量——合并与切分时它跟着"
               "单元走，期数与每期分量都按它对账。分组时**只填序号**，标题与"
               "落点由程序取用，不要照抄标题。")

# 约束解码用的 schema。**分组只给序号**：模型不产出落点，也就没有「抄错
# 标题导致取不到料」这件事；期号、行号、体量由代码填。
_EPISODE_ITEM = {
    "type": "object",
    "properties": {
        "units": {"type": "array", "items": {"type": "integer"}},
        "title": {"type": "string"},
        "gist": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["units", "title", "gist", "points"],
}

MAP_SCHEMA = {
    "type": "object",
    "properties": {"episodes": {"type": "array", "items": _EPISODE_ITEM}},
    "required": ["episodes"],
}

# 插入比排图多一个字段：插入点。**必须各用各的 schema** —— 约束解码按 schema
# 剪裁输出，拿排图的 schema 去约束插入，anchor_no 连放的地方都没有，模型不是
# 答错，是根本答不出来；而错误表现为"模型给的插入点是空的"，看着像模型不听话。
# 条目形状与排图**同款**（units/title/gist/points）：插入就是换个容器再排一次，
# 模型照样只填序号，落点由代码从序号映射——「模型抄标题抄错落点」这一类错，
# 从根上不存在。
INSERT_SCHEMA = {
    "type": "object",
    "properties": {"anchor_no": {"type": "string"},
                   "episodes": {"type": "array", "items": _EPISODE_ITEM}},
    "required": ["anchor_no", "episodes"],
}


class PlanError(RuntimeError):
    pass


# ------------------------------------------------------------------ 单元清单
def units_of(irs, per_ep):
    """把全部素材摊成一条有序的取料单元队列：`[(素材号, segment)]`。

    顺序就是播出顺序：素材按编号、素材内部按结构顺序。跨素材的地图正是靠这
    一条队列拼起来的——两本书合成一档节目时，第二本接着第一本往后排。
    """
    out = []
    for sid, ir in (irs or {}).items():
        for s in probe.pick_units(ir, per_ep):
            out.append((sid, s))
    return out


def _units_text(units):
    """单元清单：序号、素材号、所属章节、层级、标题、标注、位置、体量，以及凝缩。

    喂给模型的是**凝缩结果而不是原文**——这正是「每次上下文小」的来处：
    几十万字的原文进不了上下文，几十条凝缩可以。

    每条还带**它在原文里的位置**（行号范围）与**它属于哪一篇**（章节路径）。
    这两样不是装饰：合并与切分的依据是逻辑与章节上下，给一张只有标题和体量的
    扁平表，模型只能靠标题字面去猜哪几节该合——而判据本来就在稿子的结构里。

    **凝缩的条目一条不砍**。这段文字是排图那一次调用能看到的全部依据，砍掉
    第七条主干，模型就永远看不到它——而两节该合还是该拆，靠的正是这些主干。
    清单因此变长，那是这一步应付的代价：它仍比原文短两个数量级。
    """
    lines = [UNIT_LEGEND, ""]
    for i, (sid, s) in enumerate(units, 1):
        tags = ""
        if s.get("series"):
            tags += "〔系列 %s〕" % s["series"]
        if s.get("misc") == "keep":
            tags += "〔序跋类〕"
        if s.get("oversize"):
            tags += "〔体量超一期〕"
        path = s.get("path") or ""
        bits = []
        if probe.loc_text(s):
            bits.append(probe.loc_text(s))
        bits.append("原文 %d 字" % int(s.get("chars") or 0))
        lines.append("%d. [%s] %sH%d「%s」%s（%s）"
                     % (i, sid, ("%s › " % path) if path else "",
                        int(s["level"]), s["title"], tags, "，".join(bits)))
        if s.get("gist"):
            lines.append("   主线：%s" % s["gist"])
        for p in (s.get("points") or []):
            lines.append("   · %s" % p)
        if s.get("concepts"):
            lines.append("   概念：%s" % "、".join(s["concepts"]))
    return "\n".join(lines)


# ------------------------------------------------------------------ 分组归一
def _clean_groups(rows, n, warnings=None):
    """把模型给的分组归一成 1..n 的一个划分，返回 (分组, 备注)。

    三道归一，都只在结果**可判定**时动手：

    - 序号越界、重复：第一次出现有效，其余丢掉并记下；
    - 每组内部按序号升序，组与组按各组首个序号排序；
    - 没被任何一组提到的单元：**补成独立一期**并记下。

    最后一条不能省。漏掉的单元不是「少讲一点」，是那几节从此不在任何一期里，
    而地图看上去完整——取料只看落点，不会回头对账有多少节没排上。
    """
    warnings = warnings if warnings is not None else []
    seen, groups, dropped = set(), [], []
    for r in rows or []:
        if not isinstance(r, dict):
            dropped.append("非对象条目")
            continue
        idx = []
        for u in (r.get("units") or []):
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
        groups.append(dict(r, units=sorted(idx)))
    if not groups:
        return [], dropped
    groups.sort(key=lambda g: g["units"][0])
    missing = [k for k in range(1, n + 1) if k not in seen]
    for k in missing:
        groups.append({"units": [k], "title": "", "gist": "", "points": [],
                       "orphan": True})
    groups.sort(key=lambda g: g["units"][0])
    flat = [k for g in groups for k in g["units"]]
    if flat != sorted(flat):
        warnings.append("分组之间在素材顺序上打了结（各期的取材范围前后交错），"
                        "已按每期第一个单元的先后重排，请核对期与期的边界。")
    if dropped:
        warnings.append("有 %d 处分组标记没有通过归一（%s），已按剩余的分组排期。"
                        % (len(dropped), "、".join(dropped[:6])))
    if missing:
        warnings.append("有 %d 个单元没有被分进任何一期（%s），已各自补成一期——"
                        "漏掉它们等于那几节不在任何一期里，而地图看上去是完整的。"
                        % (len(missing),
                           "、".join("#%d" % k for k in missing[:8])))
    return groups, dropped


def _episodes_of(groups, units, warnings=None):
    """分组落到期上：期号、落点、体量由代码填，标题、主旨、要点取自模型输出。"""
    warnings = warnings if warnings is not None else []
    out, untitled, empty_pts = [], [], []
    for i, g in enumerate(groups, 1):
        members = [units[k - 1] for k in g["units"] if 0 < k <= len(units)]
        refs = [{"source": sid, "anchor": s["title"], "line": s.get("line")}
                for sid, s in members]
        title = str(g.get("title") or "").strip()
        if not title and members:
            title = members[0][1]["title"]
            untitled.append(str(i))
        points = [str(x).strip() for x in (g.get("points") or [])
                  if str(x).strip()]
        if not points:
            # 没给要点就退回各单元自己的凝缩主旨：地图上的「要点」一栏空着，
            # 后面写脚本时这一期就只剩标题可依。
            points = [s["gist"] for _sid, s in members if s.get("gist")][:5]
            empty_pts.append(str(i))
        out.append({
            "no": str(i),
            "title": title,
            "gist": str(g.get("gist") or "").strip(),
            "points": points,
            "refs": refs,
            "chars": int(sum(float(s.get("chars") or 0) for _sid, s in members)),
        })
    if untitled:
        warnings.append("有 %d 期模型没给标题（第 %s 期），已用该期首个单元的标题顶上。"
                        % (len(untitled), "、".join(untitled[:6])))
    if empty_pts:
        warnings.append("有 %d 期模型没给要点（第 %s 期），已用各单元的凝缩主旨顶上。"
                        % (len(empty_pts), "、".join(empty_pts[:6])))
    return out


def _series_split_warning(episodes, units):
    """同系列被拆到两期以上时出声。

    「同系列合并排期」是整合依据里最硬的一条，但它是**软约束**：某一期装不下
    整个系列时拆开反而对。所以只报，不拦——由人决定接受还是重排。
    """
    ep_of = {}
    for e in episodes:
        for r in e["refs"]:
            ep_of.setdefault((r["source"], r["anchor"]), []).append(e["no"])
    seen, split = set(), []
    for sid, s in units:
        key = s.get("series")
        if not key or (sid, key) in seen:
            continue
        seen.add((sid, key))
        nos = []
        for s2, t in units:
            if s2 != sid or t.get("series") != key:
                continue
            for n in ep_of.get((sid, t["title"]), []):
                if n not in nos:
                    nos.append(n)
        if len(nos) > 1:
            split.append("%s（第 %s 期）" % (key, "、".join(nos)))
    if split:
        return ("同系列被拆到多期：%s。范式里的整合依据建议同系列按序合并考虑，"
                "接受与否由你定。" % "；".join(split[:4]))
    return ""


# ------------------------------------------------------------------ 解析
def _json_object(raw, what):
    """取 LLM 输出里的 JSON 对象。取不到抛 PlanError。

    解析交给 `llm_client.extract_json`（逐个花括号试解析，思考段混在答案里的
    输出也取得到）。取不到时给出最可能的原因：排图那一次要吐出整张地图，
    几千字输出被 max_tokens 截断是最常见的一种，而报「JSON 无法解析」会把
    人引向提示词去改措辞。
    """
    data = llm_client.extract_json(raw)
    if data is None:
        raise PlanError(
            "%s：模型输出里没有可解析的 JSON 对象（输出 %d 字符）。"
            "输出很长时多半是被 max_tokens 截断，请调大 llm.max_tokens 后重试。"
            % (what, len((raw or "").strip())))
    return data


# ------------------------------------------------------------------ 排地图
MAP_SYSTEM = """你为播客排播出计划（期数地图）。素材的结构已经逐节探查过，每一节
都带了它在原文里的位置、体量与凝缩（主线与主干）。你决定**哪些节合成一期**——按凝缩
呈现的逻辑关系、章节上下与这批素材的文体来合并与切分。**你分出几组就是几期**，
期数不由程序算定。合并的依据是内容，但合出来的每一期都受**分量的上下限**双重约束：
一期的素材体量按清单里的字数合计，超出上限或低于下限的期都会被程序退回重排。
下限尤其要紧——料不够的期，写的时候只能把写过的段落再背一遍凑数。每一期的标题、
主旨与要点也由你给。期号与落点由程序填。只输出 JSON。"""


def _map_prompt(brief, block, want_eps, per_ep, target, ratio, feedback=None):
    """排图的分组提示词。`target` 是一期成稿目标字数——压比的"1"，**不含
    偏移**；素材大致 x = target × 1.25 × 档位，下限 = target × 1.25 × 1.2。"""
    per_ep_max = int(per_ep * probe.RATIO_HEADROOM)
    # 下限与上限是同一把尺的两端，缺一端等于没给尺：只印上限时，模型的分组
    # 依据只剩「别超」，「每期至少要有多少料」它一无所知——而恰恰是这一端
    # 决定了稿子能不能写满。底下这句数字由代码算，不让模型自己估。
    per_ep_min = int(target * probe.RATIO_HEADROOM * probe.MIN_RATIO)
    if feedback:
        # 重排版：期数放开。上一版的问题就是分组本身，再命令它凑原定数，
        # 等于一边说「你分错了」一边不许它改答案——压比达标优先于凑数。
        how = ("【期数】（重排版：按内容需要重定，不受原定数约束）\n\n"
               "上一版分组在压比体检里不达标，重新分组时期数按内容需要定。"
               "压比达标优先于凑数：该拆的拆、该并的并。")
    elif want_eps:
        how = ("【期数】（项目定了，按这个数排）\n\n"
               "项目定的是 **%d 期**，按这个数排。一期脚本目标 %d 字（成稿，"
               "偏移 1.25 后 %d 字），按压缩档 1:%d 折算，一期素材**大致 %d 字、"
               "硬上限 %d 字、硬下限 %d 字**——某一节自身接近一期时让它单独成期。\n\n"
               "上下限都是准绳，**下限优先**：一期素材合计低于下限，这一期就"
               "不成立——不是因为写不出来，而是因为写不满时模型只能把写过的"
               "段落再背一遍。料不够就把相邻的期并进来，或给这一期补进更多单元；"
               "宁可少排几期，也不要排出料不够的期。高于上限则拆开。"
               % (want_eps, target,
                  int(target * probe.RATIO_HEADROOM), ratio,
                  per_ep, per_ep_max, per_ep_min))
    else:
        how = ("【期数】（由你决定，不必凑数）\n\n"
               "**按下面的文体依据做合并与切分，你分出几组就是几期。** 不要为了"
               "凑成一个好看的数把内容拆开或硬塞。拿捏一期的分量：一期脚本"
               "目标 %d 字（成稿，偏移 1.25 后 %d 字），按压缩档 1:%d 折算，"
               "一期素材**大致 %d 字、硬上限 %d 字、硬下限 %d 字**；某一节自身"
               "已接近一期时，让它单独成期。\n\n"
               "上下限都是准绳，**下限优先**：一期素材合计低于下限，这一期就"
               "不成立——写不满时模型只能把写过的段落再背一遍。料不够就与相邻"
               "的期合并。高于上限则拆开。"
               % (target, int(target * probe.RATIO_HEADROOM), ratio,
                  per_ep, per_ep_max, per_ep_min))
    tail = ""
    if feedback:
        tail = ("\n\n【上一版的问题】（压比体检不达标；逐条修正后重新分组）\n%s\n\n"
                "上面每一条都带了期号与字数：超上限的期把内容拆开或舍去，不足"
                "下限的期与相邻的期合并或补进更多单元。其余期若没有问题，分法"
                "可以保持。" % feedback)
    return """【结构单元清单】（每一节都已单独看过：压在原文的哪一块、字数、凝缩。
据此把全部单元分组成播出计划）

%s

%s

%s

【当前任务：分组】（依着凝缩与章节上下）

凝缩写的是一节的**逻辑骨架**：说清了什么、反对什么、有哪几条主线。该合的依据
是**内容**——讲同一件事、同一条论证链的不同环节、同一个系列的续篇；如果两节
的主干里出现同一个概念、同一条结论的不同环节，那就是该合的信号。清单里每一条
都标了它压在原文的哪一块、属于哪一篇：**同一篇里的各节、上下文紧密相接的几节，
是天然的同一期**；换了一篇、换成另一条线，就是分期的边界。分组依据是内容，
不是字数——但合出来的每一期都受上面的分量上限约束。顺序按清单里的序号，
不要打乱稿子自身的推进顺序。
%s

【每期给四项】（缺一不可）

- title：本期标题，不超过 20 字，要能独立成集
- gist：本期主旨，一句话说清这一期讲什么（40 字内）
- points：3 到 5 条主要内容要点，每条不超过 40 字
- units：本期包含的单元序号（整数），按序号从小到大列出

【输出】（只输出 JSON 对象）

{"episodes":[{"units":[1,2,3],"title":"标题","gist":"本期主旨",
 "points":["要点一","要点二"]}]}

units 要**覆盖清单里的全部序号，每个序号恰好出现一次**——漏掉的单元不会有人
替你补，那几节就直接不在任何一期里了。落点、期号、体量由程序填，你不用写。""" % (
        brief, how, block, tail)


def _ratio_issues(episodes, target, ratio):
    """压比体检：逐期对照压缩档的两条红线，返回问题列表（空 = 全员达标）。

    压比 = 期素材字数 ÷ **成稿目标**（`probe.target_chars`，不含偏移——
    口径法源：1 = 每期时长 × 标准语速，偏移量不改变"1"）。两条红线：

    - **上限 = 档位 × 1.25**（压不动线）：素材容量 x = 1 × 1.25 × 档位，超过
      它，模型只能反复删减，有损之上再有损，损到语义不保——这是把内容压进
      一期的死任务，必须拆期或舍内容；
    - **下限 = 1.2 × 1.25 = 1.5**（没话硬写线）：素材至少 1 × 1.25 × 1.2，
      即原文不足偏移后脚本的 1.2 倍，「压缩」无从谈起——模型写不满，只能
      回头把写过的段落再背一遍。必须并期或补料。25 分钟一期即素材至少
      约 9877 字。

    两条红线之间都是合格。压得松（压比 1 上下）是自由度，不是错误：素材
    就这么多，写透写松比注水诚实。每条问题自带期号、字数与方向——它们会
    原样进重排反馈，模型照着数字改，不是盲摇。

    期与期的相对悬殊（某期比别的重一倍）不再单独体检：绝对红线卡住两端之后，
    中间怎么分布是分组的事，相对比较只会产生既拦不住超限、又误伤合理悬殊
    的噪声。
    """
    issues = []
    hi = float(ratio) * probe.RATIO_HEADROOM
    lo = probe.MIN_RATIO * probe.RATIO_HEADROOM
    for e in episodes:
        chars = int(e.get("chars") or 0)
        if target <= 0 or chars <= 0:
            continue
        r = chars / float(target)
        no = str(e.get("no") or "?")
        if r > hi:
            issues.append(
                "第 %s 期素材 %d 字，目标脚本约 %d 字，压比 %.1f 超过上限 %.2f"
                "（1:%d 档 × 偏移 1.25）——请把这一期拆成几期，或重新划分哪些"
                "内容本期一笔带过"
                % (no, chars, target, r, hi, ratio))
        elif r < lo:
            issues.append(
                "第 %s 期素材只有 %d 字，目标脚本约 %d 字，压比 %.2f 低于下限"
                " %.2f（1.2 × 偏移 1.25，素材至少 %d 字）——原文不足硬写必然"
                "捏造，请与相邻的期合并，或给这一期补进更多单元"
                % (no, chars, target, r, lo,
                   int(target * probe.RATIO_HEADROOM * probe.MIN_RATIO)))
    return issues


def _oversize_warnings(units, per_ep, ratio, warnings):
    """超一期容量的单元：落警告，**不阻断排图**。

    这类单元「结构上已无处可拆」（`probe._fit` 已探到底），它成期之后压比必然
    超上限——但阻断排图是错的时机：出问题的是其中某一期，不是整张图。让它先
    排出图、由逐期体检点名那一期、由重排试着一拆，拆不动才报错——那时候人
    看得见是哪一期、超了多少。在这里直接抛错，等于因为一节书的体量否掉整张
    图，人连问题出在哪一期都不知道。
    """
    for _sid, s in units:
        if s.get("oversize"):
            warnings.append("「%s」自身体量 %d 字，超过一期容量 %d 字（1:%d 档）"
                            "且已无处可拆，这一期的压缩比将超出档位上限。"
                            % (s["title"], int(s["chars"]), per_ep, ratio))


def _regroup_loop(ask, build, episodes, target, ratio, warnings,
                  progress=None, log=None, floor_blocks=False):
    """压比体检 → 带反馈**整体重排**（排图与插入共用同一条回线）。

    重排只做「重新分组」——凝缩全程复用，一次分组调用的成本，不值得为省它
    引入局部子排图的一整套机制（子清单、窗口选择、期号重编、落库替换）。反馈
    是算术化的（哪期、多少字、超/欠多少），不是盲摇骰子，两轮内收敛是合理
    预期。收口时**上限与下限分开办**：仍超上限就报错停下交人（超限的期写不
    出来，而地图一旦落库，后面每一期都按这张错的图出片）；仍低于下限则落警告、
    按最后一版落库（料不够的期写出来是短、不是错，源头另有产能预检与下限两道
    闸门，出口还有硬去重兜着）。

    `floor_blocks=False`（排图）：下限问题同样进重排——并期或补单元救得回来。
    `floor_blocks=True`（插入）：下限问题不重排——插入的素材就是勾选的那几节，
    重排变不出更多内容，只会白白烧调用；单独落一条人说得清的警告交出去。
    上限问题两种场景都重排：把一期塞爆是分组的错，重排拆得开。

    `ask(feedback)` 发一次分组调用（feedback 为上一版问题的算术清单），
    `build(rows)` 把回答落成期列表（空表 = 这次回答没法用，重试或放弃）。
    """
    log = log or (lambda m: None)

    def _classify(eps):
        out = _ratio_issues(eps, target, ratio)
        if not floor_blocks:
            return out, []
        hard, floor = [], []
        for i in out:
            (floor if "低于下限" in i else hard).append(i)
        return hard, floor

    retry, floor = _classify(episodes)
    retried = 0
    while retry and retried < MAP_RETRIES:
        retried += 1
        if progress:
            progress("压比体检重排（第 %d/%d 次）" % (retried, MAP_RETRIES), 0.90)
        log("压比体检：%d 期不达标，带问题整体重排（第 %d/%d 次）"
            % (len(retry), retried, MAP_RETRIES))
        rows = ask(feedback="\n".join("- " + i for i in retry))
        if not rows:
            break
        rebuilt = build(rows)
        if not rebuilt:
            break
        episodes = rebuilt
        retry, floor = _classify(episodes)
    for i in floor:
        warnings.append(
            "插入期分量：%s——插入的素材就是勾选的这几节，重排变不出更多内容，"
            "这一期会比别的期短；想加长请补素材后重新插入。" % i.split("——")[0])
    # 上限与下限的出路不同，收口方式也不同：
    # 超上限是**物理线**——素材比一期能消化的量还多，照着写只能反复删减、损到
    # 语义不保，重排拆不开就是真拆不开，落库等于让后面每一期都照错的图出片。
    # 所以报错停下交人。
    # 低于下限是**质量线**——料不够的期写出来是「短」，不是「错」，而且源头还有
    # 两道闸门（产能预检拦「期数太多」、下限 1.2 拦「总量不够」）与出口的硬去重
    # 兜着。这里再报错，人会卡在一个只能靠改期数或补素材才能解的死局上。
    hard = [i for i in retry if "低于下限" not in i]
    low = [i for i in retry if "低于下限" in i]
    if hard:
        raise PlanError(
            "压比体检重排 %d 轮后，仍有 %d 期超过压缩上限：\n%s\n"
            "超上限的期压不动——素材比一期能消化的量还多，照着写只能反复删减、"
            "损到语义不保。请把这几期拆开（「地图期数上限」还有余地时先调大它）、"
            "缩短每期时长，或减少这几期的取材。"
            % (MAP_RETRIES, len(hard), "\n".join("- " + i for i in hard)))
    if low and retried:
        warnings.append(
            "压比体检重排 %d 次后仍有 %d 期素材低于下限（料不够，写出来会偏短），"
            "已按最后一版落库，请人工核对：%s"
            % (retried, len(low),
               "；".join(i.split("——")[0] for i in low[:3])))
    return episodes


def _span(progress, lo, hi):
    """把局部进度（0~1）映射到全局区间 [lo, hi]。

    下层函数（探查、凝缩）只知道自己跑到了几分之几，不知道整个排图流程里
    自己占哪一段——这个换算发生在 plan_map 这一层，区间就写在区间旁边。
    """
    if not progress:
        return None
    return lambda text, f: progress(text, lo + (hi - lo) * min(1.0, max(0.0, f)))


def plan_map(base, pid, item, cfg, llm, log=None, force=False, progress=None):
    """排出期数地图。

    流程见模块开头。返回 {"episodes", "warnings", "capacity", "planned",
    "kind", "ratio", "unit_level"}。期号、落点、每期体量已由代码填好。
    排图前做产能预检（素材撑不起计划的期数直接拦）；排完做压比体检，
    超压缩档红线（上限档位×1.25、下限 1.2）的带数字反馈整体重排至多
    MAP_RETRIES 次，凝缩全程复用，仍不达标落警告交人。
    `progress(text, frac)` 上报全局进度（frac 0~1）供后台任务推进度条，
    区间分配写在各调用点旁；缺席就什么都不做。
    """
    log = log or (lambda m: None)
    if llm is None:
        raise PlanError("排地图需要 LLM：单元凝缩与分组都由模型做。")
    cap = int(cfg.get("script.map_max_episodes", 60))

    if progress:
        progress("探查素材结构", 0.02)
    irs = probe.scan_project(base, pid, cfg, llm=llm,
                             paradigms_map=paradigms.PARADIGMS,
                             force=force, log=log,
                             progress=_span(progress, 0.02, 0.10))
    sources = irs["sources"]
    per_ep = irs["capacity"]
    body = irs["body_chars"]
    if progress:
        progress("核对产能", 0.12)

    want = item.get("planned_episodes")
    target = probe.target_chars(cfg)
    off = probe.offset_chars(cfg)
    ratio = probe.ratio_of(cfg)
    n_eps = int(want) if want else 0
    warnings = list(irs["warnings"])
    if want and int(want) > cap:
        n_eps = cap
        warnings.append("项目计划 %s 期，超出上限 %d 期，按上限排。"
                        % (want, cap))

    # 产能预检（凝缩之前）：素材总量连「每期下限」都撑不起，排出来就是整图
    # 没话硬写——这种账在凝缩前算清，别烧完几十次凝缩调用才发现底子不够。
    # 按 n_eps（上限截断后的实际计划数）算：人想排 5 期被上限压成 3 期时，
    # 产能对的是 3 期。反方向（内容多、期数少）只警告不拦：模型重排时可以
    # 增期数，人也可以事后取舍，不是死局。
    if n_eps and target > 0:
        # 口径：1 = 成稿目标（不含偏移），素材下限 = 1 × 1.25 × 1.2
        # （25 分钟一期 ≈ 9877 字）。偏移量进算式，不改变"1"。
        floor_chars = int(probe.offset_chars(cfg) * probe.MIN_RATIO)
        max_eps = int(body // floor_chars) if floor_chars > 0 else 0
        if n_eps > max_eps:
            raise PlanError(
                "素材共 %d 有效字，按压缩档 1:%d 撑不起 %d 期：每期脚本目标约 %d 字"
                "，原文至少要 %d 字才够写（= 目标 × 偏移 1.25 × 1.2，不足就是"
                "没话硬写），现有素材最多排 %d 期。请降低每期时长、减少期数，"
                "或补充素材。"
                % (body, ratio, n_eps, target, floor_chars, max_eps))
        tight = -(-body // max(1, int(off * ratio)))
        if n_eps < tight:
            warnings.append(
                "素材 %d 字按压缩档 1:%d 至少需要 %d 期才不超压缩上限，项目只定"
                " %d 期——排出的图期数可能多于计划，以压比体检结果为准。"
                % (body, ratio, tight, n_eps))

    kind = _resolve_kind(sources, item, cfg)
    units = units_of(sources, per_ep)
    if not units:
        raise PlanError("项目里的素材都不可用（切不出正文），无法排地图。")
    log("结构已读出：%d 个取料单元（H%s 为切分单位，超过单期体量的按下层展开）"
        % (len(units), max([int(ir.get("unit_level") or 1)
                            for ir in sources.values()] or [1])))
    # 凝缩结果写回 segment 并落盘：重排一次图不该把 N 次调用再烧一遍。
    done, skipped = probe.condense_units(
        base, pid, units, llm, cfg, paradigms.get(kind), force=force, log=log,
        progress=_span(progress, 0.12, 0.80))
    log("单元凝缩：新凝缩 %d 节，复用已有 %d 节" % (done, skipped))
    for sid, ir in sources.items():
        probe.save(base, pid, sid, probe.stamp_condense(ir))

    block = paradigms.prompt_block(kind)
    brief = _units_text(units)
    if progress:
        progress("模型分组", 0.85)
    log("组织依据：%s；一期素材约 %d 字（压缩档 1:%d 参照）；期数由合并与切分的结果决定"
        % (paradigms.get(kind)["label"], per_ep, ratio))
    _oversize_warnings(units, per_ep, ratio, warnings)

    def _ask(feedback=None):
        raw, _meta = llm.chat(
            [{"role": "system", "content": MAP_SYSTEM},
             {"role": "user", "content": _map_prompt(brief, block, n_eps, per_ep,
                                                     target, ratio,
                                                     feedback=feedback)}],
            temperature=float(cfg.get("llm.temperature", 0.8)),
            max_tokens=int(cfg.get("llm.max_tokens", 8192)),
            json_schema=MAP_SCHEMA)
        return _json_object(raw, "排地图").get("episodes")

    def _build(rows):
        groups, _dropped = _clean_groups(rows, len(units), warnings)
        return _episodes_of(groups, units, warnings)

    rows = _ask()
    if not rows:
        raise PlanError("模型没有排出任何一期。可能是输出被截断，"
                        "或素材清单里没有可用的章节。")
    episodes = _build(rows)
    if not episodes:
        raise PlanError("模型给的分组一个单元都没覆盖到，没有可落库的期。")

    # 压比体检 → 带反馈整体重排（回线在 _regroup_loop，排图与插入共用一条）。
    # 整体重排让模型通盘看边界，不会有「挪给邻居把邻居挪瘦」的传染链；重排时
    # 期数放开——上一版分错了还命令它凑原定数，等于一边说错了一边不许改。
    if off > 0 and body < off * probe.MIN_RATIO:
        # 总量连一期的下限都撑不起：无论怎么分，每期都破下限——这不是分组
        # 问题，是素材问题，重排注定徒劳。落一条人说得清的警告交出去。
        # 口径：下限 = 成稿目标 × 偏移 1.25 × 1.2，"1"本身不含偏移。
        warnings.append(
            "素材共 %d 有效字，不足一期的素材下限（脚本目标约 %d 字 × 偏移 1.25"
            " × 下限 1.2 = %d 字）——无论怎么分组都够不上「压缩」的门槛，写作"
            "只能展开硬凑或重复凑数。请降低每期时长或补充素材。"
            % (body, target, int(off * probe.MIN_RATIO)))
    else:
        episodes = _regroup_loop(_ask, _build, episodes, target, ratio,
                                 warnings, progress=progress, log=log)

    # 上限是**排完之后的红线**，不是排之前的除法。原先先 min 截断再命令模型
    # 照着那个数排——素材需要 100 期时，等于命令它把 100 期的内容塞进 60 期，
    # 每一期都超载，而且一句提示都没有。红线就报错，不无声挤压。
    if len(episodes) > cap:
        raise PlanError(
            "内容按逻辑分出来 %d 期，超过上限 %d 期。多出来的内容不会自动挤进"
            "别的期里——请调大「地图期数上限」再排。" % (len(episodes), cap))
    # 期数与项目定的不一样时**不废掉整张地图**：那是几分钟的等待。落库、
    # 把差异说清楚，由人决定重排还是接受。
    if want and len(episodes) != n_eps:
        warnings.append("模型排出 %d 期，与项目定的 %d 期不符——已按模型的分法"
                        "落库，请核对后决定是否重排。" % (len(episodes), n_eps))
    split = _series_split_warning(episodes, units)
    if split:
        warnings.append(split)
    if progress:
        progress("落库", 0.98)
    log("地图排出 %d 期，覆盖 %d 个单元"
        % (len(episodes), sum(len(e["refs"]) for e in episodes)))
    return {"episodes": episodes, "warnings": warnings, "capacity": per_ep,
            "planned": n_eps, "kind": kind, "ratio": ratio,
            "unit_level": max([int(ir.get("unit_level") or 1)
                               for ir in sources.values()] or [1])}


def _kind_of(sources):
    """取素材的范式类型。多份素材不一致时以第一份为准。"""
    kinds = [ir.get("kind") for ir in sources.values() if ir.get("kind")]
    if not kinds:
        return "auto"
    return kinds[0]


def _resolve_kind(sources, item, cfg):
    """定用哪张范式卡。项目指定 > 全局默认 > 探查推断。

    顺序不能反：探查是推断，人指定的是判断。推断盖掉判断，等于把人的
    一次选择当没发生——而他改的就是「这批素材不该按推断的那套来切」。

    前两级走 `paradigms.chosen_of`，与探查判附属页用的是**同一条**：探查那一步
    也要先知道「这批素材算哪一类」，才能查出哪些页不播。写在这里就成了第二处
    口径，两处一旦不同，就会出现「地图按方法论排、附属页按通用名单判」这种
    各说各话的结果——而且两处都不报错，只是成品自相矛盾。
    """
    return paradigms.chosen_of(item, cfg) or _kind_of(sources)


# ------------------------------------------------------------------ 插入
INSERT_SYSTEM = """你为已有的播客计划插入新素材。新素材的每一节都已经单独凝缩过。
你做两件事：判断新素材插在哪一期之后（anchor_no），并把它分组成期——分出几组
就是几期，每一期的标题、主旨与要点由你给。期号与落点由程序填：期号按锚点期号
加字母编号，落点按分到的单元序号直接取用。只输出 JSON。"""


def _insert_prompt(plan_brief, units_text, block):
    return """【已有计划】（一档播客已排好的播出计划，含此前插入产生的分支期；新素材要插进它）

%s

【新素材的凝缩单元清单】（每一节都已单独凝缩过：压在原文的哪一块、字数、凝缩）

%s

%s

【当前任务：定插入点并在锚点下分组建期】

1. 通读新素材各节的凝缩，找出它们与已有计划中哪些期的主题相关，给出
   anchor_no：新排的期插在这一期之后。必须是上面已有计划里出现过的期号
   （含分支期号，如 3a）。
2. 把新素材的全部单元分组成期，**只填序号**：依据凝缩呈现的逻辑关系与
   章节上下合并与切分，你分出几组就是几期——新素材体量小时分成一期
   很正常，不要硬拆。
3. 每期给四项（与排图同口径）：title、gist、points、units（整数序号，
   按从小到大列出，覆盖清单里的全部序号，每个序号恰好出现一次）。
4. 新素材与已有计划互为佐证或补充时，要点里写出这种关系；与已有各期
   重复的部分不排——插入是为了带上新的论证角度。
5. 落点、期号、每期体量由程序填，你不用写。

【输出】（只输出 JSON 对象）

{"anchor_no":"3","episodes":[{"units":[1,2],"title":"标题","gist":"本期主旨",
 "points":["要点一","要点二"]}]}""" % (plan_brief, units_text, block)


def _plan_brief(item):
    """已有各期的简报：期号、标题、主旨、要点。插入时靠这份定锚与避重。

    分支期也在里面（3a、3b…）：锚点可以选到分支期上，模型也得知道分支链里
    已经讲了什么——新批与旧批重复的部分不该再排。
    """
    rows = []
    for e in project_store.map_episodes(item):
        pts = "；".join(e.get("points") or [])
        gist = str(e.get("gist") or "").strip()
        line = "- 第 %s 期「%s」" % (e.get("no"), e.get("title"))
        if gist:
            line += " 主旨：%s" % gist
        if pts:
            line += "：" + pts
        rows.append(line)
    return "\n".join(rows)


def assign_branch_nos(item, anchor_no, count):
    """给待插入的期编分支期号，自动避开地图上已占用的号。

    期号规则只此一处——排图、插入、界面都不再各编一套，否则迟早有一处
    先漂，而漂出来的期号在产物上看着都正常。同一个锚点插过一批之后，
    再插一批续用后面的字母（3a 3b → 3c 3d），不重号也不重置。避让对
    全图做是防御：`branch_no` 生成的号只可能撞上同一锚点的分支链，多避
    不会错，少避才会。
    """
    taken = {str(e.get("no") or "") for e in project_store.map_episodes(item)}
    out, n = [], 1
    for _ in range(int(count)):
        while project_store.branch_no(anchor_no, n) in taken:
            n += 1
        no = project_store.branch_no(anchor_no, n)
        taken.add(no)
        n += 1
        out.append(no)
    return out


def plan_insert(base, pid, item, cfg, llm, source_ids=None, log=None,
                progress=None):
    """为新素材定插入点与拆分——在锚点容器里跑与排图同一条管线。

    新素材**逐节新凝缩**（`condense_units` 走缓存，已凝缩的节不再烧第二次），
    凝缩后的单元清单交给模型：定锚点、分组、给每期标题/主旨/要点——与排图
    同一套归一（`_clean_groups`）与落点映射（`_episodes_of`），落点由代码从
    序号取，模型不抄标题。分出的期再过压比体检：超上限带反馈重排
    （`_regroup_loop`），不足下限只警告——插入的素材就勾选了那几节，重排
    变不出内容。

    期号由 `assign_branch_nos()` 编好给建议表显示；人确认落库时
    （`insert_apply`）会按最终锚点重编——人改锚点是常事，号跟着锚点走。
    返回 {"anchor_no", "episodes", "warnings"}；`progress(text, frac)` 上报
    全局进度供后台任务推进度条，区间写在各调用点旁；缺席就什么都不做。
    """
    log = log or (lambda m: None)
    if llm is None:
        raise PlanError("插入需要 LLM：单元凝缩与分组都由模型做。")
    plan_brief = _plan_brief(item)
    if not plan_brief:
        raise PlanError("项目还没有地图，无法判断插入点。请先排地图。")
    exist = [str(e.get("no") or "") for e in project_store.map_episodes(item)]

    if progress:
        progress("探查素材结构", 0.02)
    irs = probe.scan_project(base, pid, cfg, llm=llm,
                             paradigms_map=paradigms.PARADIGMS, log=log,
                             progress=_span(progress, 0.02, 0.10))
    sources = irs["sources"]
    used = {str(r.get("source") or "")
            for e in project_store.map_episodes(item)
            for r in (e.get("refs") or [])}
    if source_ids:
        only = set(source_ids)
    else:
        # 没勾就取「还没排进地图的素材」——已在地图里的料再插一遍，等于把
        # 同一本书重插一次；界面上勾选的默认口径也是它（已排素材置灰）。
        only = {sid for sid in sources if sid not in used}
        if not only:
            raise PlanError("项目里的素材都已排进地图，没有可插入的新素材。"
                            "请先入库新素材，或改用重排地图调整计划。")
    work = {sid: ir for sid, ir in sources.items() if sid in only}
    if not work:
        raise PlanError("没有可用于插入的素材：勾选的素材不在项目里。")

    per_ep = irs["capacity"]
    target = probe.target_chars(cfg)
    ratio = probe.ratio_of(cfg)
    warnings = list(irs["warnings"])
    # 范式与排图同一套优先级（项目指定 > 全局默认 > 探查推断）：插入是同一
    # 个操作换了个容器，没有理由另立口径。
    kind = _resolve_kind(work, item, cfg)
    units = units_of(work, per_ep)
    if not units:
        raise PlanError("勾选的素材切不出可用的正文单元，无从插入。")
    log("结构已读出：%d 个取料单元（只处理勾选的新素材）" % len(units))

    # 新素材逐节凝缩——插入与排图吃同一种料。缓存在这里生效：已凝缩过的节
    # 直接跳过，补两段小素材就是两次调用，成本只跟新素材走。
    done, skipped = probe.condense_units(
        base, pid, units, llm, cfg, paradigms.get(kind), log=log,
        progress=_span(progress, 0.10, 0.45))
    log("单元凝缩：新凝缩 %d 节，复用已有 %d 节" % (done, skipped))
    for sid, ir in work.items():
        probe.save(base, pid, sid, probe.stamp_condense(ir))

    _oversize_warnings(units, per_ep, ratio, warnings)

    block = paradigms.prompt_block(kind)
    units_text = _units_text(units)
    if progress:
        progress("模型定插入点并分组", 0.55)
    log("组织依据：%s；一期素材约 %d 字（压缩档 1:%d 参照）"
        % (paradigms.get(kind)["label"], per_ep, ratio))

    def _ask(feedback=None):
        prompt = _insert_prompt(plan_brief, units_text, block)
        if feedback:
            prompt += ("\n\n【上一版的问题】（压比体检不达标；重新分组，"
                       "锚点不变）\n\n%s" % feedback)
        raw, _meta = llm.chat(
            [{"role": "system", "content": INSERT_SYSTEM},
             {"role": "user", "content": prompt}],
            temperature=float(cfg.get("llm.temperature", 0.8)),
            max_tokens=int(cfg.get("llm.max_tokens", 8192)),
            json_schema=INSERT_SCHEMA)
        d = _json_object(raw, "定插入点")
        a = str(d.get("anchor_no") or "").strip()
        # 归一警告记在局部：重试成功后，失败那一次的噪声不该留着吓人——
        # 最终生效的是最后一次分组。
        local = []
        groups, _dropped = _clean_groups(d.get("episodes"), len(units), local)
        return groups, a, local

    def _ask_groups(feedback=None):
        """重排回线用的分组调用：直接返回归一后的分组（`_build` 要的形状）。"""
        return _ask(feedback)[0]

    def _build(groups):
        """分组落期：落点、体量由代码填，分支期号按锚点链现编。"""
        eps = _episodes_of(groups, units, warnings)
        for e, no in zip(eps, assign_branch_nos(item, anchor_no, len(eps))):
            e["no"] = no
        return eps

    # 首版分组与锚点。整批无效（没锚点或没组）重试一次，两次都不行明确失败
    # ——不返回一张看着成功的空建议。
    groups, anchor_no, local = _ask()
    if not (groups and anchor_no):
        log("定插入点：这一次没拿到可用的插入点或分组，换一次采样重来…")
        groups, anchor_no, local = _ask()
    # 空插入点与空分组分开报：前者多半是约束解码没给它地方放，后者是分组
    # 整批没对上，两句提示指向的排查方向完全不同。
    if not anchor_no:
        raise PlanError("模型没有给出插入点，请重试，或手动指定插在哪一期之后。")
    if anchor_no not in exist:
        raise PlanError("模型给的插入点「%s」不在现有计划里。" % anchor_no)
    if not groups:
        raise PlanError(
            "两次都没能从模型拿到可用的分组：回答里没有一个有效的单元序号。"
            "请换一个更肯照清单分组的模型重试。")
    warnings.extend(local)

    episodes = _build(groups)
    if not episodes:
        raise PlanError("模型给的分组一个单元都没覆盖到，没有可落库的期。")

    # 压比体检：超上限带反馈重排（把一期塞爆是分组的错，重排拆得开），
    # 不足下限只警告（素材就勾选了这几节，重排变不出内容）。
    episodes = _regroup_loop(_ask_groups, _build, episodes, target, ratio,
                             warnings, progress=progress, log=log,
                             floor_blocks=True)

    if progress:
        progress("完成", 0.98)
    log("在第 %s 期之后插入 %d 期：%s"
        % (anchor_no, len(episodes), "、".join(e["no"] for e in episodes)))
    return {"anchor_no": anchor_no, "episodes": episodes, "warnings": warnings}
