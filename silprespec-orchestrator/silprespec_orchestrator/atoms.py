"""前置规范原子库 — 10 个原子 + 配方 + 通用执行器

复用自 silprespec-emulator，为编排器提供原子化执行能力。

5 类原子：
  生成(3): text / select / slot
  后处理(4): deterministic / enum_filter / detect_report / json_parse
  校验(1, 可配): in_set / no_extra / required_full / in_range / eq_exact / none
  控制流(1): retry 循环
  观测(1, 可配): hit / fabricated / extra_keys / left_empty / flagged / changed
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable
import re

from .pipeline_model import WayResult, TASK_PROMPTS, json_key


def parse_json_slots(raw: str, slot_names: list) -> tuple:
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
    flagged = []
    try:
        for m in re.finditer(pattern, raw):
            val = m.group(0)
            if allowed:
                unmatched = val not in allowed
            else:
                unmatched = True
            flagged.append({"value": val, "pos": m.start(), "report": label, "unmatched": unmatched})
    except Exception:
        pass
    return flagged


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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
    task_prompt: str = ""


# ======================================================================
# 生成原子（3）
# ======================================================================
def gen_text(ctx: AtomCtx):
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
    cfg = ctx.cfg
    slot_names = [s.get("name", "") for s in cfg.get("slots", [])]
    filled, extra = parse_json_slots(ctx.raw, slot_names)
    ctx.filled = filled
    ctx.extra_keys = extra


def pp_deterministic(ctx: AtomCtx):
    ctx.corrected = apply_deterministic(ctx.raw, ctx.cfg)


def pp_enum_filter(ctx: AtomCtx):
    enums = ctx.cfg.get("enums", [])
    picked = [w.strip() for w in ctx.raw.split(",") if w.strip()]
    valid = [w for w in picked if any(w in e or e in w for e in enums)]
    ctx.valid_words = valid
    ctx.fabricated = [w for w in picked if w not in valid and not w.startswith("[异常]")]


def pp_detect_report(ctx: AtomCtx):
    cfg = ctx.cfg
    pattern = cfg.get("detect_pattern", r"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)")
    allowed = cfg.get("allowed_values", [])
    label = cfg.get("report_label", "建议人工复审")
    ctx.flagged = detect_and_report(ctx.raw, pattern, allowed, label)


POSTPROCESSORS = {"json_parse": pp_json_parse, "deterministic": pp_deterministic,
                  "enum_filter": pp_enum_filter, "detect_report": pp_detect_report}


# ======================================================================
# 校验原子
# ======================================================================
def validate_in_set(ctx: AtomCtx):
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
    cfg = ctx.cfg
    required = [s["name"] for s in cfg.get("slots", []) if s.get("required")]
    missing = [k for k in required if not ctx.filled.get(k) or ctx.filled.get(k) == "未指定"]
    ctx.missing_required = missing
    ctx.valid = not missing
    ctx.offset = f"缺失必填: {missing}" if missing else "必填齐全"


def _to_number(v):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def validate_in_range(ctx: AtomCtx):
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
    checks = ctx.cfg.get("exact_checks", [])
    bad = []
    for c in checks:
        f = c.get("field", "")
        if str(ctx.filled.get(f, "")) != str(c.get("value", "")):
            bad.append(f"{f}={ctx.filled.get(f, '')}≠{c.get('value')}")
    ctx.valid = not bad
    ctx.offset = "全部精确命中" if not bad else " · ".join(bad)


def validate_guide(ctx: AtomCtx):
    con = ctx.cfg.get("output_constraints", {})
    output = str(ctx.output)
    bad = []
    for kw in con.get("required_keywords", []):
        if kw and kw not in output:
            bad.append(f"缺关键词:{kw}")
    for kw in con.get("forbidden_keywords", []):
        if kw and kw in output:
            bad.append(f"含禁词:{kw}")
    ml = con.get("max_length")
    if ml and len(output) > ml:
        bad.append(f"超长:{len(output)}>{ml}")
    fr = con.get("format_regex", "")
    if fr:
        try:
            if not re.search(fr, output):
                bad.append(f"不匹配格式:{fr}")
        except re.error:
            bad.append(f"正则无效:{fr}")
    ctx.valid = not bad
    ctx.offset = "满足输出约束" if not bad else " · ".join(bad)


def validate_diverge(ctx: AtomCtx):
    output = str(ctx.output)
    if not output.strip() or output.strip() == "[空响应]":
        ctx.valid = False
        ctx.offset = "空响应无法纠偏"
        return
    tgt = ctx.cfg.get("correction_target", {})
    corrected = ctx.corrected
    bad = []
    fr = tgt.get("format_regex", "")
    if fr:
        try:
            if not re.search(fr, corrected):
                bad.append(f"纠偏后不匹配格式:{fr}")
        except re.error:
            bad.append(f"正则无效:{fr}")
    rp = tgt.get("required_pattern", "")
    if rp:
        try:
            if not re.search(rp, corrected):
                bad.append(f"纠偏后缺模式:{rp}")
        except re.error:
            bad.append(f"正则无效:{rp}")
    fp = tgt.get("forbidden_pattern", "")
    if fp:
        try:
            if re.search(fp, corrected):
                bad.append(f"纠偏后含禁模式:{fp}")
        except re.error:
            bad.append(f"正则无效:{fp}")
    ctx.valid = not bad
    ctx.offset = "纠偏达标" if not bad else " · ".join(bad)


def validate_deterministic(ctx: AtomCtx):
    output = str(ctx.output)
    if not output.strip() or output.strip() == "[空响应]":
        ctx.valid = False
        ctx.offset = "空响应无法钉死"
        return
    tgt = ctx.cfg.get("pin_target", {})
    corrected = ctx.corrected
    bad = []
    ev = tgt.get("exact_value", "")
    if ev and corrected != ev:
        bad.append(f"钉死后≠目标:{ev[:60]}")
    fr = tgt.get("format_regex", "")
    if fr:
        try:
            if not re.search(fr, corrected):
                bad.append(f"钉死后不匹配格式:{fr}")
        except re.error:
            bad.append(f"正则无效:{fr}")
    ctx.valid = not bad
    ctx.offset = "封死达标" if not bad else " · ".join(bad)


def validate_detect_report(ctx: AtomCtx):
    output = str(ctx.output)
    if not output.strip() or output.strip() == "[空响应]":
        ctx.valid = False
        ctx.offset = "空响应无法检出"
        return
    if not ctx.flagged:
        ctx.valid = False
        ctx.offset = "未检出任何项"
        return
    unmatched = [f for f in ctx.flagged if f.get("unmatched")]
    ctx.valid = True
    ctx.offset = f"检出{len(ctx.flagged)}项，{len(unmatched)}项需上报（不阻塞）"


def validate_none(ctx: AtomCtx):
    ctx.valid = True
    ctx.offset = ""


VALIDATORS = {"in_set": validate_in_set, "no_extra": validate_no_extra,
              "required_full": validate_required_full,
              "in_range": validate_in_range, "eq_exact": validate_eq_exact,
              "guide": validate_guide, "diverge": validate_diverge,
              "deterministic": validate_deterministic, "detect_report": validate_detect_report,
              "none": validate_none}


# ======================================================================
# 观测原子
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
# 配方 + 方式配方声明
# ======================================================================
@dataclass
class Recipe:
    generate: str = "text"
    generate_arg: str = ""
    postprocess: list = field(default_factory=list)
    validate: str = "none"
    retry: bool = True
    observe: list = field(default_factory=list)

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
    "pure_guide":       Recipe("text",  "",        [],                  "guide",        True,  []),
    "diverge_correct":  Recipe("text",  "",        ["deterministic"],   "diverge",      True,  ["changed"]),
    "deterministic_pin":Recipe("text",  "",        ["deterministic"],   "deterministic",False, ["changed"]),
    "detect_report":    Recipe("text",  "",        ["detect_report"],   "detect_report",False, ["flagged"]),
}
_VALUE_BOUND_RECIPES = {
    "enum_select":  Recipe("select", "",            [],          "in_set",       True,  ["hit"]),
    "slot_extract": Recipe("slot",  "extra_check",  ["json_parse"], "no_extra",  True,  ["extra_keys"]),
    "required_min": Recipe("slot",  "required_min", ["json_parse"], "required_full",True,["left_empty"]),
    "condense_enum":Recipe("text",  "",            ["enum_filter"], "no_extra",   True,  ["fabricated"]),
}


def recipe_for(way_id: str, cfg: dict | None = None) -> Recipe | None:
    if way_id == "value_bound":
        bt = (cfg or {}).get("bound_type", "enum_select")
        return _VALUE_BOUND_RECIPES.get(bt, _VALUE_BOUND_RECIPES["enum_select"])
    return WAY_RECIPES.get(way_id)


def recipe_of(way_id: str) -> Recipe:
    return WAY_RECIPES.get(way_id)


# ======================================================================
# 展示格式兼容
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
    if way_id == "pure_guide":
        return {"output": ctx.output}
    if way_id == "value_bound":
        bt = ctx.cfg.get("bound_type", "enum_select")
        if bt == "condense_enum":
            return {"condensed": ctx.valid_words, "raw": ctx.raw}
        return dict(ctx.filled)
    if way_id == "diverge_correct":
        return {"raw": ctx.raw, "corrected": ctx.corrected}
    if way_id == "deterministic_pin":
        return {"raw": ctx.raw, "pinned": ctx.corrected}
    if way_id == "detect_report":
        return {"raw": ctx.raw, "flagged": ctx.flagged}
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
    if way_id == "pure_guide":
        return {"error": str(ctx.output)} if str(ctx.output).startswith("[异常]") else {"output": ctx.output}
    if way_id == "value_bound":
        bt = ctx.cfg.get("bound_type", "enum_select")
        if bt == "condense_enum":
            return {"raw": ctx.raw, "valid": ctx.valid_words, "fabricated": ctx.fabricated}
        if bt == "slot_extract":
            return {"raw": ctx.raw, "filled": dict(ctx.filled), "extra_keys": ctx.extra_keys}
        if bt == "required_min":
            return {"raw": ctx.raw, "filled": dict(ctx.filled), "missing_required": ctx.missing_required}
        return dict(ctx.filled)
    if way_id == "diverge_correct":
        return {"raw": ctx.raw, "corrected": ctx.corrected}
    if way_id == "deterministic_pin":
        return {"raw": ctx.raw, "pinned": ctx.corrected}
    if way_id == "detect_report":
        return {"raw": ctx.raw, "flagged": ctx.flagged}
    return {"output": ctx.output}


# ======================================================================
# 通用执行器
# ======================================================================
def exec_recipe(way_id: str, wc, user_input: str, chat: Callable) -> WayResult:
    custom = getattr(wc, "recipe", None)
    recipe = Recipe.from_dict(custom) if custom else recipe_for(way_id, wc.config)
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