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

"""锚点指针抽取：LLM 只出「引用 + 槽位」，值一律由 Python 从原文取（纯标准库）。

范式（与 structured-writer 的 aux_parser 同源：LLM 给引用、Python 校验取数）
    LLM    →  {"g":1,"formula":{"quote":"C4H11N5"}}   逐字抄一段原文，不给坐标、不给数值
    Python →  text.find(quote)                         反算区间，命中即自校验

为什么不把字符坐标交给 LLM：它的分词器不按字符切，中英混排 / 全角标点 / 换行都会让
计数漂移；而一个错误的坐标仍然是一个合法整数，Python 会照切不误——错得无声无息。
反过来，「抄一段原文」是 LLM 最擅长的，「在字符串里找位置」是 Python 最擅长的。
把索引的生成权交给 Python，同时白捡一个自校验：找不到 = 显式失败（可重试）；
找到 = 一定正确。这把「LLM 抄得对不对」（不可判定）降级成「锚点在不在原文」（可判定）。

两种协议，模式由 Python 按输入结构判定（零 LLM 参与）：
    anchors — 自然语言 / 中小表格：每个槽位给一个原文引用
    columns — 大表格：LLM 只给列语义映射，行由 Python 全量遍历

三条硬闸门（anchors 模式）
    ① 命中  引用必须在原文中逐字出现，否则该条 FAIL
    ② 有序  同组引用必须落在上一组结束位置之后，否则 FAIL（防编造顺序）
    ③ 连续  g 必须从 1 连续编号、无跳号，否则 FAIL（防编号漂移）

闸门校验的是「LLM 说的锚点对不对」，不提供「LLM 没漏看」的保证——漏看的兜底靠
提示词正反例（语义判断）与界面回显命中位置（人工复核），不做算法级的语义排除。

失败一律显式返回缺口清单，不静默丢弃、不降级放行。
"""
import csv
import io
import json
import re

from .mass_calc import formula_valid

# 槽位定义。name / product_* 可缺；formula 缺则阻塞（分组排序的唯一输入）；
# precursor 缺则告警（质量可分辨判据停用，但排序仍成立）。
SLOTS = ("name", "formula", "precursor", "product_quant", "product_qual")
REQUIRED_SLOTS = ("formula",)
NUMERIC_SLOTS = ("precursor", "product_quant", "product_qual")

SLOT_LABEL = {
    "name": "化合物名称",
    "formula": "化学式",
    "precursor": "母离子 m/z",
    "product_quant": "定量子离子 m/z",
    "product_qual": "定性子离子 m/z",
}

_TABLE_DELIMS = ("\t", ",", ";", "|")
_DELIM_ALIAS = {
    "tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|",
    "space": " ", "blank": " ", "whitespace": " ",
}
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# 单次抽取的输出预算：anchors 模式下每化合物约 60 token，取值放宽以容纳大列表。
MAX_OUTPUT_TOKENS = 8192


class ExtractionError(Exception):
    """协议层错误（LLM 未遵守输出形状）。"""


# ---------------------------------------------------------------------------
# 提示词：四段式——名词定义 / 正例 / 反例 / 边界例
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一个化学信息抽取助手。你的唯一职责是「指出从哪里取、怎么对应」，
不要输出任何数值、坐标或原文之外的字符——取值由程序按你给的引用从原文中完成。

## 一、名词定义

化学式（分子式）：由元素符号与其下标数字组成，仅允许 C H O N S P F Cl Br I 这几种元素。
下标 1 省略不写。例：C6H12O6、CH4、C4H11N5。
母离子 m/z：方法表中该化合物前体离子的质荷比，在文本里表现为一个纯数字。
子离子 m/z：方法表中用于定量/定性的碎片离子质荷比，同样是纯数字。

## 二、正例（这些是化学式，应当提取）

C6H12O6    C4H11N5    C25H43NO18    C9H8O4    CH4    C18H37NO    H2O

## 三、反例（长得像但不是化学式，不要提取）

C18         —— 色谱柱型号（如 C18 柱）或碳链数，不是化合物的化学式
50-78-2     —— CAS 登记号
130.1       —— 这是 m/z，属于母离子槽位，不是化学式
MW 180.16   —— 分子量标签
>=98%       —— 纯度
270-280C    —— 沸点
C6H12O6 的「180.16」 —— 括号内分子量不是化学式的一部分

## 四、边界例（决定取不取）

| 原文写法          | 判定                                        |
|-------------------|---------------------------------------------|
| C18H37NO          | 是——含 C18 但整体是合法化学式                   |
| NaCl              | 不是——Na 不在允许元素内，该化合物化学式记为缺失     |
| C6H12O6 (180.16)  | 是，只取 C6H12O6                             |
| C6H12O6·H2O       | 是，保留完整写法（含水物）                      |
| 「约 130」         | m/z 槽位取原文的「约 130」或其中数字部分         |

## 五、输出协议（严格遵守）

### 模式 anchors（自然语言 / 中小表格）

{"mode":"anchors","items":[
  {"g":1,"name":{"quote":"二甲双胍"},"formula":{"quote":"C4H11N5"},"precursor":{"quote":"130.1"},
   "product_quant":{"quote":"71.1"},"product_qual":null},
  {"g":2,"name":null,"formula":{"quote":"C25H43NO18"},"precursor":{"quote":"646.2"},
   "product_quant":null,"product_qual":null}
]}

规则：
- quote 必须是原文中**逐字出现**的一段连续文字——空格、大小写、标点、全角半角都要与原文完全一致。
  不要改写、不要翻译、不要补全、不要规范化。
- 原文没有该信息时，对应槽位写 null。**绝对不要凭化合物名称去推断化学式**——
  推断出来的写法在原文中不存在，会被程序判定为无效。
- g 从 1 开始连续编号，一个化合物一个 g，不跳号、不重复。
- 五个槽位名固定为 name / formula / precursor / product_quant / product_qual，全部必须出现（可为 null）。

### 模式 columns（表格）

{"mode":"columns","delimiter":"\\t","header_rows":1,"orientation":"rows",
 "map":{"name":0,"formula":1,"precursor":2,"product_quant":3,"product_qual":4}}

规则：
- delimiter 用实际分隔符（制表符写 "\\t"）。
- header_rows 是表头占用的行数（没有表头写 0）。
- orientation 为 "rows" 表示化合物按行排列；"columns" 表示化合物按列排列。
- map 的键是槽位名，值是列序号（从 0 开始）。表中没有的槽位不要写进 map。
- 只输出映射，不要输出任何行内容。

只输出上述 JSON 对象本身，不要 markdown 代码块标记，不要任何解释文字。"""


def build_schema(mode="anchors"):
    """约束解码用的 json_schema（引擎级禁止协议外形状）。

    后端不支持时由调用方降级为「仅提示词约束」——校验逻辑不依赖本函数的生效与否。
    """
    if mode == "columns":
        return {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["columns"]},
                "delimiter": {"type": "string"},
                "header_rows": {"type": "integer"},
                "orientation": {"type": "string", "enum": ["rows", "columns"]},
                "map": {
                    "type": "object",
                    "properties": {s: {"type": "integer"} for s in SLOTS},
                    "additionalProperties": False,
                },
            },
            "required": ["mode", "map"],
            "additionalProperties": False,
        }

    quote_slot = {
        "anyOf": [
            {"type": "object",
             "properties": {"quote": {"type": "string"}},
             "required": ["quote"], "additionalProperties": False},
            {"type": "null"},
        ]
    }
    return {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["anchors"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": dict({"g": {"type": "integer"}},
                                       **{s: quote_slot for s in SLOTS}),
                    "required": ["g"] + list(SLOTS),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["mode", "items"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# 模式判定 / 小工具
# ---------------------------------------------------------------------------
def detect_mode(text):
    """按输入结构判定协议模式（纯 Python，零 LLM）。

    表格判据：非空行 >= 3，且前若干行在某个候选分隔符上的出现次数完全一致。
    自然语言段落里的逗号数量通常参差不齐，因此不会误判。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return "anchors"
    sample = lines[:20]
    for d in _TABLE_DELIMS:
        counts = [ln.count(d) for ln in sample]
        if counts[0] >= 1 and len(set(counts)) == 1:
            return "columns"
    return "anchors"


def _quote_of(cell):
    """从槽位单元取出引用文本。null / 空串 / 非对象一律视为「该槽位缺失」。"""
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell.strip() or None
    if isinstance(cell, dict):
        q = cell.get("quote")
        if q is None:
            return None
        q = str(q).strip()
        return q or None
    return None


def _to_float(v):
    """从引用文本里取出第一个数字。取不到返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM_RE.search(str(v))
    return float(m.group(0)) if m else None


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _norm_delim(v):
    """分隔符归一化。" " 表示按任意空白切分。"""
    if not v:
        return None
    s = str(v)
    low = s.strip().lower()
    if low in _DELIM_ALIAS:
        return _DELIM_ALIAS[low]
    if s == "\\t":
        return "\t"
    return s[0]


def _split_rows(text, delim):
    """按分隔符切行。返回去除空行后的二维列表。"""
    if delim and delim != " ":
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    else:
        rows = [ln.split() for ln in text.splitlines()]
    return [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]


def _skeleton():
    d = {s: None for s in SLOTS}
    d["sources"] = {}
    return d


def _finalize(compounds, failures, missing, mode, hint=""):
    """组装统一返回结构 + 判定 ok。"""
    # name 缺失由 Python 赋名——编号是机器给的，不是 LLM 编的
    unnamed = missing.get("name", [])
    for i, g in enumerate(unnamed):
        compounds[g - 1]["name"] = f"化合物{g}"

    warnings = []
    if failures:
        show = "；".join(f"g{f['g']}/{SLOT_LABEL.get(f['slot'], f['slot'])}"
                         f"「{f['quote']}」({_reason_text(f['reason'])})"
                         for f in failures[:6])
        show += "…" if len(failures) > 6 else ""
        warnings.append({
            "level": "error", "code": "anchor_miss",
            "message": f"{len(failures)} 个引用未能在原文中定位：{show}。"
                       f"请核对原文是否包含这些内容，或改用更精确的写法。",
        })
    if missing.get("formula"):
        gs = "、".join(f"化合物{g}" for g in missing["formula"][:8])
        warnings.append({
            "level": "error", "code": "missing_formula",
            "message": f"{gs} 缺少化学式。化学式是出峰排序的唯一输入，缺了它分组预测不成立——"
                       f"请补齐后重试（程序不会代为推断分子式）。",
        })
    if missing.get("precursor"):
        gs = "、".join(f"化合物{g}" for g in missing["precursor"][:8])
        warnings.append({
            "level": "warn", "code": "missing_precursor",
            "message": f"{gs} 缺少母离子 m/z。出峰排序不受影响，但「质量可分辨」约束"
                       f"对涉及这些化合物的组**不会启用**，同组内的质量混峰不会被发现。",
        })
    if unnamed:
        gs = "、".join(f"化合物{g}" for g in unnamed[:8])
        warnings.append({
            "level": "info", "code": "auto_named",
            "message": f"{gs} 原文未给出名称，已自动编号（不影响分组计算）。",
        })

    ok = not failures and not missing.get("formula")
    if hint and not ok:
        warnings.append({"level": "info", "code": "retry_hint", "message": hint})
    return {
        "ok": ok,
        "mode": mode,
        "compounds": compounds,
        "failures": failures,
        "missing": missing,
        "warnings": warnings,
    }


def _reason_text(reason):
    return {"not_found": "原文中查无此串",
            "out_of_order": "位置早于前一个化合物",
            "column_out_of_range": "列序号超出表格宽度",
            "no_rows": "表格无数据行"}.get(reason, reason)


# ---------------------------------------------------------------------------
# anchors 模式：逐引用解引用
# ---------------------------------------------------------------------------
def resolve_anchors(payload, text):
    """把 anchors 处方解引用为化合物列表。

    闸门①命中 闸门②有序 在此实现；闸门③连续 在编号校验处实现。
    """
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ExtractionError("协议错误：anchors 模式缺少 items 数组")

    # 闸门③：g 必须从 1 连续编号
    gs = []
    for it in items:
        if not isinstance(it, dict):
            raise ExtractionError("协议错误：items 元素不是对象")
        gs.append(_to_int(it.get("g"), -1))
    if gs != list(range(1, len(gs) + 1)):
        raise ExtractionError(f"协议错误：g 必须从 1 连续编号，实际为 {gs}")

    compounds = []
    failures = []
    missing = {s: [] for s in SLOTS}
    cursor = 0        # 上一组的结束位置——组间必须严格递增

    for g, it in zip(gs, items):
        rec = _skeleton()
        spans = []
        for slot in SLOTS:
            quote = _quote_of(it.get(slot))
            if quote is None:
                missing[slot].append(g)
                continue
            pos = text.find(quote, cursor)   # 组内复用同一游标：组内槽位顺序任意
            if pos < 0:
                seen = text.find(quote) >= 0
                failures.append({
                    "g": g, "slot": slot, "quote": quote,
                    "reason": "out_of_order" if seen else "not_found",
                })
                continue
            spans.append((pos, pos + len(quote)))
            rec[slot] = quote
            rec["sources"][slot] = {"quote": quote, "start": pos, "end": pos + len(quote)}
        # 类型化：数值槽位从引用里取数字；保留原始引用在 sources 中
        for slot in NUMERIC_SLOTS:
            if rec[slot] is not None:
                rec[slot] = _to_float(rec[slot])
        if spans:
            cursor = max(e for _, e in spans)   # 组结束，推进组间游标
        compounds.append(rec)

    # 化学式的语法校验：抄错到无法解析时降级为「缺失」（语义噪音不在本层处理）
    for g, rec in zip(gs, compounds):
        f = rec.get("formula")
        if f:
            okf, _msg = formula_valid(f)
            if not okf:
                rec["formula"] = None
                if g not in missing["formula"]:
                    missing["formula"].append(g)
    return _finalize(compounds, failures, missing, "anchors")


# ---------------------------------------------------------------------------
# columns 模式：列语义映射 + Python 全量遍历
# ---------------------------------------------------------------------------
def resolve_columns(payload, text):
    """把列映射处方解引用为化合物列表。行由 Python 遍历，LLM 不接触任何行内容。"""
    cmap = payload.get("map")
    if not isinstance(cmap, dict) or not cmap:
        raise ExtractionError("协议错误：columns 模式缺少 map")
    bad_keys = [k for k in cmap if k not in SLOTS]
    if bad_keys:
        raise ExtractionError(f"协议错误：map 含未知槽位 {bad_keys}")

    delim = _norm_delim(payload.get("delimiter"))
    header_rows = max(0, _to_int(payload.get("header_rows"), 0))
    orient = str(payload.get("orientation") or "rows").strip().lower()

    rows = _split_rows(text, delim)
    if not rows:
        raise ExtractionError("表格为空")

    # 转置先于表头切分：化合物按列排列时，字段名行转置后才是第 0 行
    if orient == "columns":
        width = max(len(r) for r in rows)
        rows = [[r[i] if i < len(r) else "" for r in rows] for i in range(width)]

    if len(rows) <= header_rows:
        raise ExtractionError(f"表格只有 {len(rows)} 行，被 header_rows={header_rows} 全部占满")
    body = rows[header_rows:]

    width = max(len(r) for r in body)
    failures = []
    active = {}
    for slot, idx in cmap.items():
        if not isinstance(idx, int) or idx < 0:
            failures.append({"g": 0, "slot": slot, "quote": str(idx),
                             "reason": "column_out_of_range"})
            continue
        if idx >= width:
            failures.append({"g": 0, "slot": slot, "quote": f"第 {idx} 列",
                             "reason": "column_out_of_range"})
            continue
        active[slot] = idx

    compounds = []
    missing = {s: [] for s in SLOTS}

    for i, row in enumerate(body):
        g = i + 1
        rec = _skeleton()
        for slot, idx in active.items():
            val = row[idx] if idx < len(row) else ""
            if not val:
                missing[slot].append(g)
                continue
            rec[slot] = val
            rec["sources"][slot] = {"quote": val, "row": i, "col": idx}
        for slot in NUMERIC_SLOTS:
            if rec[slot] is not None:
                rec[slot] = _to_float(rec[slot])
        if rec.get("formula"):
            okf, _msg = formula_valid(rec["formula"])
            if not okf:
                rec["formula"] = None
        for slot in SLOTS:
            if rec.get(slot) is None and g not in missing[slot]:
                missing[slot].append(g)
        compounds.append(rec)

    if not compounds:
        raise ExtractionError("表格没有数据行")
    return _finalize(compounds, failures, missing, "columns")


def resolve(payload, text):
    """按 payload 的 mode 分发解引用。"""
    if not isinstance(payload, dict):
        raise ExtractionError("协议错误：LLM 输出不是 JSON 对象")
    mode = str(payload.get("mode") or "anchors").strip().lower()
    if mode == "columns":
        return resolve_columns(payload, text)
    return resolve_anchors(payload, text)


# ---------------------------------------------------------------------------
# 端到端：调用 LLM → 解引用 → 失败回灌重试
# ---------------------------------------------------------------------------
def parse_payload(resp_text):
    """从 LLM 回复中取出 JSON 对象（容忍代码块围栏与前后废话）。"""
    s = (resp_text or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    lo, hi = s.find("{"), s.rfind("}")
    if lo >= 0 and hi > lo:
        try:
            return json.loads(s[lo:hi + 1])
        except Exception as e:
            raise ExtractionError(f"LLM 输出不是合法 JSON：{e}")
    raise ExtractionError("LLM 输出中找不到 JSON 对象")


def _feedback(result):
    """把上一轮的缺口整理成回灌提示。"""
    parts = []
    if result.get("failures"):
        lines = []
        for f in result["failures"][:12]:
            where = (f"g{f['g']}" if f["g"] else "表格映射")
            lines.append(f"- {where} / {SLOT_LABEL.get(f['slot'], f['slot'])}："
                         f"「{f['quote']}」——{_reason_text(f['reason'])}")
        parts.append("你上一轮给出的以下引用未能在原文中定位，请重新核对原文后修正：\n"
                     + "\n".join(lines))
    if result.get("missing", {}).get("formula"):
        gs = "、".join(f"g{g}" for g in result["missing"]["formula"][:12])
        parts.append(f"{gs} 的化学式为空。化学式是必需项：若原文确实没有，保持 null；"
                     f"若有，请给出与原文逐字一致的引用（不要自己写分子式）。")
    return "\n\n".join(parts)


def extract(text, llm, max_retries=1, schema_strict=True, on_attempt=None):
    """完整抽取流程。返回 resolve() 的结果结构。

    text       : 原文
    llm        : LLMClient 实例（需支持 chat(messages, ...)）
    max_retries: 失败后回灌重试的次数（默认 1 —— 一次修正机会，二次显式失败）
    on_attempt : 可选回调 (attempt, mode, result)，供调用方记录过程
    """
    mode = detect_mode(text)
    result = None
    for attempt in range(max_retries + 1):
        user = text if attempt == 0 else f"{text}\n\n---\n【修正要求】\n{_feedback(result)}"
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user + f"\n\n【本次使用模式】{mode}"}]
        resp = llm.chat(messages, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.1,
                        response_format=build_schema(mode) if schema_strict else None)
        payload = parse_payload(resp)
        result = resolve(payload, text)
        if on_attempt:
            on_attempt(attempt, mode, result)
        if result["ok"]:
            break
    return result
