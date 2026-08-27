"""端到端演示 · 8 方式预设输入 + 真实 LLM 调用 + 完整原始信息

用途：一键展示 8 种前置规范方式从输入到输出的完整端到端信息，供有限实证。
每个方式跑 parallel 次，记录每次的完整 trace（含重试理由），汇总重现性。
"""
from __future__ import annotations
import time
from collections import Counter
from typing import Callable, Optional

from .pipeline_model import WayConfig, default_config, TASK_PROMPTS, WAYS, WayResult, json_key
from .atoms import (WAY_RECIPES, AtomCtx, Recipe, GENERATORS, POSTPROCESSORS,
                    VALIDATORS, OBSERVERS, _filled_for)
from .llm_client import LLMClient


DEMO_INPUTS = {
    "gate": "今天阳光真好，心情特别棒，感觉一切都很顺利！",
    "guide": "人工智能正在改变软件开发的方式，从代码生成到测试自动化，都在发生深刻变化。",
    "condense": "近年来我国在环境治理方面取得显著成效，生态文明建设深入推进，绿色发展理念贯穿经济社会发展全过程，污染防治攻坚战取得重大成果。",
    "slot": "张三于2024年3月在北京大学发表了关于大模型训练优化的演讲，吸引了数百名研究者参与。",
    "diverge": "深海中的发光生物",
    "deterministic": "大模型技术发展迅速【引用自来源5】。多模态能力不断提升【引用自来源3】。应用场景持续扩展【引用自来源5】。",
    "detect_report": "2024年我国数字经济规模达到55.8万亿元，占GDP比重42.8%，网民规模10.9亿人。",
    "required_min": "茅台酒的价格是多少？",
}


def _est_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1.5 token/字，取 1.5 偏保守）"""
    return int(len(text) * 1.5) if text else 0


def _make_chat(llm: LLMClient, calls: list):
    def chat(prompt, max_tokens=None, temperature=0.5, system_prompt=None):
        rec = {"prompt": prompt, "system_prompt": system_prompt,
               "max_tokens": max_tokens, "temperature": temperature}
        t0 = time.time()
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": prompt})
        try:
            resp = llm.chat(msgs, max_tokens=max_tokens, temperature=temperature)
            rec["response"] = resp.strip() if isinstance(resp, str) else str(resp)
        except Exception as e:
            rec["response"] = f"[异常]{e}"
        rec["elapsed"] = round(time.time() - t0, 2)
        rec["prompt_tokens"] = _est_tokens(prompt) + _est_tokens(system_prompt or "")
        rec["response_tokens"] = _est_tokens(rec["response"])
        calls.append(rec)
        return rec["response"]
    return chat


def _exec_with_trace(way_id: str, wc, user_input: str, chat):
    """跑一次方式，记录每次 attempt 的完整 trace（含重试理由）"""
    custom = getattr(wc, "recipe", None)
    recipe = Recipe.from_dict(custom) if custom else WAY_RECIPES.get(way_id)
    if recipe is None:
        return {"success": False, "retry_count": 0, "exhausted": False,
                "filled": {}, "extra": {}, "attempts": [], "error": f"无配方: {way_id}"}
    task_prompt = getattr(wc, "task_prompt", "") or TASK_PROMPTS.get(way_id, "")
    attempts = []
    last_ctx = None
    success = False
    retry_count = 0
    exhausted = False
    for attempt in range(wc.max_retry + 1):
        retry_count = attempt
        cfg = dict(wc.config)
        if recipe.generate == "slot":
            cfg["_gen_style"] = recipe.generate_arg or "extra_check"
        ctx = AtomCtx(user_input=user_input, cfg=cfg, chat=chat, attempt=attempt, task_prompt=task_prompt)
        GENERATORS[recipe.generate](ctx)
        for pp in recipe.postprocess:
            POSTPROCESSORS[pp](ctx)
        VALIDATORS[recipe.validate](ctx)
        attempts.append({
            "attempt": attempt,
            "valid": ctx.valid,
            "retry_reason": ctx.offset if not ctx.valid else "",
            "raw": ctx.raw,
            "filled": dict(ctx.filled),
            "output": ctx.output,
            "extra_keys": list(ctx.extra_keys),
            "fabricated": list(ctx.fabricated),
            "missing_required": list(ctx.missing_required),
            "flagged": list(ctx.flagged),
        })
        last_ctx = ctx
        if ctx.valid or not recipe.retry:
            success = ctx.valid
            break
    else:
        success = False
        exhausted = True
    filled = _filled_for(way_id, last_ctx, recipe) if last_ctx else {}
    extra = {}
    if last_ctx is not None:
        wr = WayResult(way=way_id)
        wr.filled = filled
        for ob in recipe.observe:
            OBSERVERS[ob](last_ctx, wr, attempts)
        extra = wr.extra
    return {"success": success, "retry_count": retry_count, "exhausted": exhausted,
            "filled": filled, "extra": extra, "attempts": attempts, "error": ""}


def run_e2e_demo(llm: LLMClient, ways: Optional[list] = None,
                 parallel: int = 3,
                 on_progress: Optional[Callable[[int, int, dict], None]] = None) -> list:
    """跑 8 方式（或指定子集）端到端，每个方式跑 parallel 次，返回完整原始信息 + 重现性。"""
    target = ways or [w[0] for w in WAYS]
    way_map = {w[0]: w for w in WAYS}
    results = []
    for i, way_id in enumerate(target):
        if way_id not in way_map:
            continue
        way_name, way_desc = way_map[way_id][1], way_map[way_id][2]
        user_input = DEMO_INPUTS.get(way_id, "测试输入")
        wc = WayConfig(way=way_id, config=default_config(way_id), max_retry=3)
        recipe = WAY_RECIPES.get(way_id)
        task_prompt = wc.task_prompt or TASK_PROMPTS.get(way_id, "")

        runs = []
        for run_idx in range(parallel):
            calls = []
            chat = _make_chat(llm, calls)
            res = _exec_with_trace(way_id, wc, user_input, chat)
            res["run_id"] = run_idx + 1
            res["calls"] = calls
            res["total_tokens"] = sum(c["prompt_tokens"] + c["response_tokens"] for c in calls)
            res["elapsed_total"] = round(sum(c["elapsed"] for c in calls), 2)
            runs.append(res)

        fills = [json_key(r["filled"]) for r in runs]
        cnt = Counter(fills)
        consistency = round(cnt.most_common(1)[0][1] / len(fills), 3) if fills else 0.0

        res = {
            "way": way_id, "name": way_name, "desc": way_desc,
            "task_prompt": task_prompt,
            "recipe": recipe.to_dict() if recipe else {},
            "config": wc.config, "max_retry": wc.max_retry,
            "user_input": user_input, "parallel": parallel,
            "runs": runs,
            "reproducibility": {
                "consistency": consistency,
                "distinct_fills": [k for k, _ in cnt.most_common()],
                "fill_counts": dict(cnt),
            },
            "success_all": all(r["success"] for r in runs),
            "total_tokens_all": sum(r["total_tokens"] for r in runs),
            "elapsed_all": round(sum(r["elapsed_total"] for r in runs), 2),
        }
        results.append(res)
        if on_progress:
            on_progress(i + 1, len(target), res)
    return results
