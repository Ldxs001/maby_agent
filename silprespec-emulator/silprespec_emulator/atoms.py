"""前置规范原子库 — 10 个原子 + 配方 + 通用执行器

5 类原子：
  生成(3): text / select / slot
  后处理(4): deterministic / enum_filter / detect_report / json_parse
  校验(1, 可配): in_set(点对面) / no_extra / required_full / in_range(面对面) / eq_exact(点对点) / none
  控制流(1): retry 循环（exec_recipe 编排）
  观测(1, 可配): hit / fabricated / extra_keys / left_empty / flagged / changed

8 方式 = 原子配方（WAY_RECIPES）。执行层无 way_id 分支；
filled/extra 的展示格式由 _filled_for/_record_attempt 按 way_id 兼容（保 UI 不变，
第二步统一展示格式后可去掉）。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable
import re

from .pipeline_model import WayResult, TASK_PROMPTS


# ======================================================================
# 确定性代码辅助（不依赖 LLM）
# ======================================================================
def parse_json_slots(raw: str, slot_names: list) -> tuple:
    """解析 LLM 输出为槽位 dict，并找出多余 key（编造）"""
    import json as _json
    filled = {}
    extra = []
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            d = _json.loads(raw[start:end + 1])
        else:
            d = {}
    except Exception:
        d = {}
    for k in slot_names:
        filled[k] = d.get(k, "未指定")
    for k in d:
        if k not in slot_names:
            extra.append(k)
    return filled, extra


def apply_deterministic(raw: str, cfg: dict) -> str:
    """按配置执行确定性后处理：正则替换 + 编号重排 + 空行归一化"""
    s = raw
    for r in cfg.get("regex_replaces", []):
        try:
            s = re.sub(r.get("pattern", ""), r.get("replace", ""), s)
        except Exception:
            pass
    if cfg.get("renumber_source", False):
        refs = re.findall(r"来源(\d+)", s)
        seen = []
        for r in refs:
            if r not in seen:
                seen.append(r)
        for i, old in enumerate(seen, 1):
            s = s.replace(f"来源{old}", f"来源{i}")
    if cfg.get("normalize_blanklines", False):
        s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def detect_and_report(raw: str, pattern: str, allowed: list, label: str) -> list:
    """检出+对照数据源：未命中 allowed 的标记上报"""
    flagged = []
    try:
        for m in re.finditer(pattern, raw):
            val = m.group(0)
            unmatched = bool(allowed) and val not in allowed
            flagged.append({"value": val, "pos": m.start(), "report": label, "unmatched": unmatched})
    except Exception:
        pass
    return flagged


# ======================================================================
# 原子执行上下文（贯穿一次 attempt）
# ======================================================================
@dataclass
class AtomCtx:
    user_input: str
    cfg: dict
    chat: Callable
    attempt: int = 0
    output: Any = None
    filled: dict = field(default_factory=dict)
    raw: str = ""
    valid: bool = True
    offset: str = ""
    corrected: str = ""
    flagged: list = field(default_factory=list)
    extra_keys: list = field(default_factory=list)
    fabricated: list = field(default_factory=list)
    valid_words: list = field(default_factory=list)
    missing_required: list = field(default_factory=list)
    task_prompt: str = ""        # 任务提示词（系统提示词）


# ======================================================================
# 生成原子（3）
# ======================================================================
def gen_text(ctx: AtomCtx):
    """文本生成：按 cfg 字段构造 prompt → LLM → 文本"""
    cfg = ctx.cfg
    mt = cfg.get("max_tokens")
    if "guide_prompt" in cfg:
        prompt = f"引导提示词：{cfg['guide_prompt']}\n基于此引导，对以下内容给出你的填空结果：\n{ctx.user_input[:500]}"
        temp = 0.6
    elif "diverge_prompt" in cfg:
        prompt = f"{cfg['diverge_prompt']}\n基于以下内容发散生成一段：\n{ctx.user_input[:400]}"
        temp = 0.9
    elif "condense_rule" in cfg:
        enums = cfg.get("enums", [])
        prompt = (f"凝练规则：{cfg['condense_rule']}\n把以下内容浓缩为短词。只允许使用候选词 {enums} 中的词或其子串，"
                  f"禁止造新词（泛化）。\n内容：{ctx.user_input[:400]}\n输出一个或多个候选词，逗号分隔。")
        temp = 0.3
    elif cfg.get("detect_pattern"):
        prompt = f"生成一段可能含数值的内容：\n{ctx.user_input[:400]}"
        temp = 0.7
    else:
        prompt = f"生成一段内容（将被代码后处理钉死）：\n{ctx.user_input[:400]}"
        temp = 0.7
    try:
        ctx.output = ctx.chat(prompt, max_tokens=mt, temperature=temp, system_prompt=ctx.task_prompt)
        if not ctx.output.strip():
            ctx.output = "[空响应]"
    except Exception as e:
        ctx.output = f"[异常]{e}"
    ctx.raw = ctx.output


def gen_select(ctx: AtomCtx):
    """穷举选择：每道门禁在有限词表中选一个或'未指定'"""
    cfg = ctx.cfg
    gates = cfg.get("gates", [])
    allow_unspec = cfg.get("allow_unspecified", True)
    filled = {}
    for g in gates:
        words = g.get("words", [])
        name = g.get("name", "")
        prompt = (f"在「{name}」维度的候选词 {words} 中，为以下内容选一个最贴切的词。\n"
                  f"若都不贴切，填「未指定」。\n内容：{ctx.user_input[:400]}\n只输出一个词。")
        try:
            out = ctx.chat(prompt, max_tokens=cfg.get("max_tokens"), temperature=0.2, system_prompt=ctx.task_prompt)
            if not out.strip():
                out = "[空响应]"
        except Exception as e:
            out = f"[异常]{e}"
        if out in words:
            filled[name] = out
        elif allow_unspec and "未指定" in out:
            filled[name] = "未指定"
        else:
            filled[name] = out
    ctx.filled = filled
    ctx.output = filled


def gen_slot(ctx: AtomCtx):
    """槽位填空：填给定槽位，输出 JSON。generate_arg 区分 prompt 风格"""
    cfg = ctx.cfg
    slots = cfg.get("slots", [])
    slot_names = [s.get("name", "") for s in slots]
    required = [s["name"] for s in slots if s.get("required")]
    optional = [s["name"] for s in slots if not s.get("required")]
    style = ctx.cfg.get("_gen_style", "extra_check")
    if style == "required_min":
        prompt = (f"填槽位。必填：{required}（必须有内容）；可留空：{optional}（无内容填「未指定」）。\n"
                  f"内容：{ctx.user_input[:400]}\n输出 JSON。")
    else:
        prompt = (f"为以下内容填这些槽位：{slot_names}。\n"
                  f"只填给定槽位，不要输出槽位以外的东西。无内容的槽位填「未指定」。\n"
                  f"内容：{ctx.user_input[:400]}\n输出 JSON，key 为槽位名。")
    try:
        out = ctx.chat(prompt, max_tokens=cfg.get("max_tokens"), temperature=0.3, system_prompt=ctx.task_prompt)
    except Exception as e:
        out = f"[异常]{e}"
    ctx.raw = out
    ctx.output = out


GENERATORS = {"text": gen_text, "select": gen_select, "slot": gen_slot}


# ======================================================================
# 后处理原子（4）
# ======================================================================
def pp_json_parse(ctx: AtomCtx):
    """解析 LLM 输出为槽位 dict，找多余 key"""
    cfg = ctx.cfg
    slot_names = [s.get("name", "") for s in cfg.get("slots", [])]
    filled, extra = parse_json_slots(ctx.raw, slot_names)
    ctx.filled = filled
    ctx.extra_keys = extra


def pp_deterministic(ctx: AtomCtx):
    """regex 替换 + 编号重排 + 空行归一化"""
    ctx.corrected = apply_deterministic(ctx.raw, ctx.cfg)


def pp_enum_filter(ctx: AtomCtx):
    """只留 enums 内的词/子串，标记编造"""
    enums = ctx.cfg.get("enums", [])
    picked = [w.strip() for w in ctx.raw.split(",") if w.strip()]
    valid = [w for w in picked if any(w in e or e in w for e in enums)]
    ctx.valid_words = valid
    ctx.fabricated = [w for w in picked if w not in valid and not w.startswith("[异常]")]


def pp_detect_report(ctx: AtomCtx):
    """正则检出 + 白名单对照 + 标记上报"""
    cfg = ctx.cfg
    pattern = cfg.get("detect_pattern", r"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)")
    allowed = cfg.get("allowed_values", [])
    label = cfg.get("report_label", "建议人工复审")
    ctx.flagged = detect_and_report(ctx.raw, pattern, allowed, label)


POSTPROCESSORS = {"json_parse": pp_json_parse, "deterministic": pp_deterministic,
                  "enum_filter": pp_enum_filter, "detect_report": pp_detect_report}


# ======================================================================
# 校验原子（1，可配 4 种判定）
# ======================================================================
def validate_in_set(ctx: AtomCtx):
    """集合内：每个维度值 ∈ 对应词表 或 允许未指定"""
    cfg = ctx.cfg
    gates = cfg.get("gates", [])
    allow_unspec = cfg.get("allow_unspecified", True)
    all_valid = True
    parts = []
    for g in gates:
        name = g.get("name", "")
        words = g.get("words", [])
        v = ctx.filled.get(name, "")
        if v in words:
            parts.append(f"{name}={v}")
        elif allow_unspec and v == "未指定":
            parts.append(f"{name}=未指定")
        else:
            all_valid = False
            parts.append(f"{name}=编造({v})")
    ctx.valid = all_valid
    ctx.offset = " · ".join(parts)


def validate_no_extra(ctx: AtomCtx):
    """无多余：condense 查编造词，slot 查多余 key"""
    if ctx.fabricated:
        ctx.valid = False
        ctx.offset = f"编造: {ctx.fabricated}"
    elif ctx.extra_keys:
        ctx.valid = False
        ctx.offset = f"多余key: {ctx.extra_keys}"
    else:
        ctx.valid = True
        ctx.offset = "无编造"


def validate_required_full(ctx: AtomCtx):
    """必填齐全：required 槽都填了（非空非未指定）"""
    cfg = ctx.cfg
    required = [s["name"] for s in cfg.get("slots", []) if s.get("required")]
    missing = [k for k in required if not ctx.filled.get(k) or ctx.filled.get(k) == "未指定"]
    ctx.missing_required = missing
    ctx.valid = not missing
    ctx.offset = f"缺失必填: {missing}" if missing else "必填齐全"


def _to_number(v):
    """从值中提取首个数值（支持 '95%'/'3.5亿'/'100' 等）"""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def validate_in_range(ctx: AtomCtx):
    """区间容差（面对面）：每个 field 的数值 ∈ [lo, hi]"""
    checks = ctx.cfg.get("range_checks", [])
    bad = []
    for c in checks:
        f = c.get("field", "")
        num = _to_number(ctx.filled.get(f, ""))
        if num is None:
            bad.append(f"{f}=非数值({ctx.filled.get(f, '')})")
        elif num < c.get("lo", float("-inf")) or num > c.get("hi", float("inf")):
            bad.append(f"{f}={num}∉[{c.get('lo')},{c.get('hi')}]")
    ctx.valid = not bad
    ctx.offset = "全部在区间内" if not bad else " · ".join(bad)


def validate_eq_exact(ctx: AtomCtx):
    """严格相等（点对点）：每个 field 的值 == 指定值"""
    checks = ctx.cfg.get("exact_checks", [])
    bad = []
    for c in checks:
        f = c.get("field", "")
        if str(ctx.filled.get(f, "")) != str(c.get("value", "")):
            bad.append(f"{f}={ctx.filled.get(f, '')}≠{c.get('value')}")
    ctx.valid = not bad
    ctx.offset = "全部精确命中" if not bad else " · ".join(bad)


def validate_none(ctx: AtomCtx):
    ctx.valid = True
    ctx.offset = ""


VALIDATORS = {"in_set": validate_in_set, "no_extra": validate_no_extra,
              "required_full": validate_required_full,
              "in_range": validate_in_range, "eq_exact": validate_eq_exact,
              "none": validate_none}


# ======================================================================
# 观测原子（1，可配 6 种统计）
# ======================================================================
def ob_hit(ctx: AtomCtx, wr: WayResult, attempts: list):
    cfg = ctx.cfg
    gates = cfg.get("gates", [])
    filled = wr.filled
    all_words = {w for g in gates for w in g.get("words", [])}
    wr.extra.update({"hit": sum(1 for v in filled.values() if v != "未指定" and not str(v).startswith("[异常]")),
                     "total": len(gates),
                     "unspecified": sum(1 for v in filled.values() if v == "未指定"),
                     "fabricated": sum(1 for v in filled.values()
                                       if v != "未指定" and not str(v).startswith("[异常]") and v not in all_words)})


def ob_fabricated(ctx: AtomCtx, wr: WayResult, attempts: list):
    wr.extra.update({"enum_combination": ctx.valid_words, "fabricated_count": len(ctx.fabricated)})


def ob_extra_keys(ctx: AtomCtx, wr: WayResult, attempts: list):
    wr.extra.update({"extra_fabrication": ctx.extra_keys})


def ob_left_empty(ctx: AtomCtx, wr: WayResult, attempts: list):
    slots = ctx.cfg.get("slots", [])
    wr.extra.update({"required_count": len([s for s in slots if s.get("required")]),
                     "optional_count": len([s for s in slots if not s.get("required")]),
                     "left_empty": sum(1 for v in wr.filled.values() if v == "未指定")})


def ob_flagged(ctx: AtomCtx, wr: WayResult, attempts: list):
    wr.extra.update({"flagged_count": len(ctx.flagged),
                     "unmatched_count": sum(1 for f in ctx.flagged if f.get("unmatched"))})


def ob_changed(ctx: AtomCtx, wr: WayResult, attempts: list):
    raw = wr.filled.get("raw", "")
    proc = wr.filled.get("corrected", wr.filled.get("pinned", ""))
    wr.extra.update({"changed": raw != proc})


OBSERVERS = {"hit": ob_hit, "fabricated": ob_fabricated, "extra_keys": ob_extra_keys,
             "left_empty": ob_left_empty, "flagged": ob_flagged, "changed": ob_changed}


# ======================================================================
# 配方 + 8 方式配方声明
# ======================================================================
@dataclass
class Recipe:
    generate: str = "text"           # text / select / slot
    generate_arg: str = ""           # slot: extra_check / required_min
    postprocess: list = field(default_factory=list)   # deterministic/enum_filter/detect_report/json_parse
    validate: str = "none"           # in_set / no_extra / required_full / none
    retry: bool = True               # True=校验驱动重试, False=单次
    observe: list = field(default_factory=list)       # hit/fabricated/extra_keys/left_empty/flagged/changed

    def to_dict(self) -> dict:
        return {"generate": self.generate, "generate_arg": self.generate_arg,
                "postprocess": list(self.postprocess), "validate": self.validate,
                "retry": self.retry, "observe": list(self.observe)}

    @staticmethod
    def from_dict(d: dict) -> "Recipe":
        return Recipe(generate=d.get("generate", "text"), generate_arg=d.get("generate_arg", ""),
                      postprocess=d.get("postprocess", []), validate=d.get("validate", "none"),
                      retry=d.get("retry", True), observe=d.get("observe", []))


WAY_RECIPES: dict[str, Recipe] = {
    "gate":         Recipe("select", "",        [],                  "in_set",       True,  ["hit"]),
    "guide":        Recipe("text",  "",        [],                  "none",         False, []),
    "condense":     Recipe("text",  "",        ["enum_filter"],     "no_extra",     True,  ["fabricated"]),
    "slot":         Recipe("slot",  "extra_check", ["json_parse"],  "no_extra",     True,  ["extra_keys"]),
    "diverge":      Recipe("text",  "",        ["deterministic"],   "none",         False, ["changed"]),
    "deterministic":Recipe("text",  "",        ["deterministic"],   "none",         False, ["changed"]),
    "detect_report":Recipe("text",  "",        ["detect_report"],   "none",         False, ["flagged"]),
    "required_min": Recipe("slot",  "required_min", ["json_parse"], "required_full",True,  ["left_empty"]),
}


def recipe_of(way_id: str) -> Recipe:
    return WAY_RECIPES.get(way_id)


# ======================================================================
# 展示格式兼容（按 way_id 产出与旧实现一致的 filled/attempts；第二步统一后可去）
# ======================================================================
def _filled_for(way_id: str, ctx: AtomCtx, recipe=None) -> dict:
    if way_id == "custom" and recipe is not None:
        g, pp = recipe.generate, recipe.postprocess
        if g in ("select", "slot"):
            return dict(ctx.filled)
        if "deterministic" in pp:
            return {"raw": ctx.raw, "corrected": ctx.corrected}
        if "detect_report" in pp:
            return {"raw": ctx.raw, "flagged": ctx.flagged}
        if "enum_filter" in pp:
            return {"condensed": ctx.valid_words, "raw": ctx.raw}
        return {"output": ctx.output}
    if way_id == "gate":
        return dict(ctx.filled)
    if way_id == "guide":
        return {"output": ctx.output}
    if way_id == "condense":
        return {"condensed": ctx.valid_words, "raw": ctx.raw}
    if way_id == "slot":
        return dict(ctx.filled)
    if way_id == "diverge":
        return {"raw": ctx.raw, "corrected": ctx.corrected}
    if way_id == "deterministic":
        return {"raw": ctx.raw, "pinned": ctx.corrected}
    if way_id == "detect_report":
        return {"raw": ctx.raw, "flagged": ctx.flagged}
    if way_id == "required_min":
        return dict(ctx.filled)
    return {"output": ctx.output}


def _attempt_for(way_id: str, ctx: AtomCtx, recipe=None) -> dict:
    if way_id == "custom" and recipe is not None:
        g, pp = recipe.generate, recipe.postprocess
        if g in ("select", "slot"):
            return {"raw": ctx.raw, "filled": dict(ctx.filled), "valid": ctx.valid, "offset": ctx.offset}
        if "deterministic" in pp:
            return {"raw": ctx.raw, "corrected": ctx.corrected}
        if "detect_report" in pp:
            return {"raw": ctx.raw, "flagged": ctx.flagged}
        if "enum_filter" in pp:
            return {"raw": ctx.raw, "valid": ctx.valid_words, "fabricated": ctx.fabricated}
        return {"output": ctx.output}
    if way_id == "gate":
        return dict(ctx.filled)
    if way_id == "guide":
        return {"error": str(ctx.output)} if str(ctx.output).startswith("[异常]") else {"output": ctx.output}
    if way_id == "condense":
        return {"raw": ctx.raw, "valid": ctx.valid_words, "fabricated": ctx.fabricated}
    if way_id == "slot":
        return {"raw": ctx.raw, "filled": dict(ctx.filled), "extra_keys": ctx.extra_keys}
    if way_id == "diverge":
        return {"raw": ctx.raw, "corrected": ctx.corrected}
    if way_id == "deterministic":
        return {"raw": ctx.raw, "pinned": ctx.corrected}
    if way_id == "detect_report":
        return {"raw": ctx.raw, "flagged": ctx.flagged}
    if way_id == "required_min":
        return {"raw": ctx.raw, "filled": dict(ctx.filled), "missing_required": ctx.missing_required}
    return {"output": ctx.output}


# ======================================================================
# 通用执行器：按配方跑一次方式
# ======================================================================
def exec_recipe(way_id: str, wc, user_input: str, chat: Callable) -> WayResult:
    custom = getattr(wc, "recipe", None)
    recipe = Recipe.from_dict(custom) if custom else WAY_RECIPES.get(way_id)
    if recipe is None:
        return WayResult(way=way_id, error=f"无配方: {way_id}")
    wr = WayResult(way=way_id)
    task_prompt = getattr(wc, "task_prompt", "") or TASK_PROMPTS.get(way_id, "")
    last_ctx: AtomCtx | None = None
    for attempt in range(wc.max_retry + 1):
        wr.retry_count = attempt
        cfg = dict(wc.config)
        if recipe.generate == "slot":
            cfg["_gen_style"] = recipe.generate_arg or "extra_check"
        ctx = AtomCtx(user_input=user_input, cfg=cfg, chat=chat, attempt=attempt, task_prompt=task_prompt)
        GENERATORS[recipe.generate](ctx)
        for pp in recipe.postprocess:
            POSTPROCESSORS[pp](ctx)
        VALIDATORS[recipe.validate](ctx)
        wr.attempts.append(_attempt_for(way_id, ctx, recipe))
        last_ctx = ctx
        if ctx.valid or not recipe.retry:
            wr.success = ctx.valid
            wr.filled = _filled_for(way_id, ctx, recipe)
            break
    else:
        wr.success = False
        wr.exhausted = True
        wr.filled = _filled_for(way_id, last_ctx, recipe) if last_ctx else {}
    if last_ctx is not None:
        for ob in recipe.observe:
            OBSERVERS[ob](last_ctx, wr, wr.attempts)
    return wr