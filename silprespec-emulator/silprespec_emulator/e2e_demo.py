"""端到端演示 · 5 方式预设输入 + 真实 LLM 调用 + 完整原始信息 + 验证指标

用途：一键展示 5 种前置规范方式从输入到输出的完整端到端信息，供有限实证。
每个方式跑 parallel 次，记录每次的完整 trace（含重试理由），汇总重现性 + 验证指标。
"""
from __future__ import annotations
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .pipeline_model import WayConfig, default_config, TASK_PROMPTS, WAYS, WayResult, json_key
from .atoms import (recipe_for, AtomCtx, Recipe, GENERATORS, POSTPROCESSORS,
                    VALIDATORS, OBSERVERS, _filled_for, calc_metrics)
from .llm_client import LLMClient


def demo_config(way_id: str) -> dict:
    """一键演示用的配置：基于 default_config。
    pure_guide/detect_report 设非空约束看校验生效；diverge_correct 设非空纠偏目标看纠偏生效。"""
    cfg = default_config(way_id)
    if way_id == "pure_guide":
        cfg["output_constraints"] = {"required_keywords": ["软件"], "forbidden_keywords": [],
                                      "max_length": 300, "format_regex": ""}
    elif way_id == "detect_report":
        cfg["allowed_values"] = ["55.8万亿元", "42.8%", "10.9亿人"]
    elif way_id == "diverge_correct":
        cfg["correction_target"] = {"format_regex": "", "required_pattern": "生物|海洋|深海|发光",
                                    "forbidden_pattern": ""}
    return cfg


DEMO_INPUTS = {
    "pure_guide": "人工智能正在改变软件开发的方式，从代码生成到测试自动化，都在发生深刻变化。",
    "value_bound": "今天阳光真好，心情特别棒，感觉一切都很顺利！",
    "diverge_correct": "深海中的发光生物",
    "deterministic_pin": "大模型技术发展迅速，多模态能力不断提升，应用场景持续扩展。",
    "detect_report": "2024年我国数字经济规模达到55.8万亿元，占GDP比重42.8%，网民规模10.9亿人。",
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
    recipe = Recipe.from_dict(custom) if custom else recipe_for(way_id, wc.config)
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


def _aggregate(way_specs, pipes):
    """按方式聚合各管道结果（pipes 含 None 表示未完成，跳过）"""
    results = []
    for idx, (way_id, way_name, way_desc, user_input, wc, recipe, task_prompt) in enumerate(way_specs):
        runs = [pipe[idx] for pipe in pipes if pipe is not None]
        fills = [json_key(r["filled"]) for r in runs]
        cnt = Counter(fills)
        consistency = round(cnt.most_common(1)[0][1] / len(fills), 3) if fills else 0.0
        results.append({
            "way": way_id, "name": way_name, "desc": way_desc,
            "task_prompt": task_prompt,
            "recipe": recipe.to_dict() if recipe else {},
            "config": wc.config, "max_retry": wc.max_retry,
            "user_input": user_input, "parallel": len(runs),
            "runs": runs,
            "metrics": calc_metrics(way_id, runs),
            "reproducibility": {
                "consistency": consistency,
                "distinct_fills": [k for k, _ in cnt.most_common()],
                "fill_counts": dict(cnt),
            },
            "success_all": all(r["success"] for r in runs) if runs else False,
            "total_tokens_all": sum(r["total_tokens"] for r in runs),
            "elapsed_all": round(sum(r["elapsed_total"] for r in runs), 2),
        })
    return results


def run_e2e_demo(llm: LLMClient, ways: Optional[list] = None,
                 parallel: int = 3,
                 on_progress: Optional[Callable[[int, int, list], None]] = None) -> list:
    """跑 5 方式（或指定子集）端到端，实验级并行：parallel 个管道并发，每管道内方式串行
    （各方式用预设输入），收齐按方式聚合算重现性。on_progress(done_pipes, total_pipes, 聚合列表)。"""
    target = ways or [w[0] for w in WAYS if w[0] != "custom"]
    way_map = {w[0]: w for w in WAYS}
    way_specs = []
    for way_id in target:
        if way_id not in way_map:
            continue
        way_name, way_desc = way_map[way_id][1], way_map[way_id][2]
        user_input = DEMO_INPUTS.get(way_id, "测试输入")
        wc = WayConfig(way=way_id, config=demo_config(way_id), max_retry=3)
        recipe = recipe_for(way_id, wc.config)
        task_prompt = wc.task_prompt or TASK_PROMPTS.get(way_id, "")
        way_specs.append((way_id, way_name, way_desc, user_input, wc, recipe, task_prompt))

    def _one_pipe(pipe_id):
        pipe = []
        for (way_id, way_name, way_desc, user_input, wc, recipe, task_prompt) in way_specs:
            calls = []
            chat = _make_chat(llm, calls)
            res = _exec_with_trace(way_id, wc, user_input, chat)
            res["run_id"] = pipe_id
            res["calls"] = calls
            res["total_tokens"] = sum(c["prompt_tokens"] + c["response_tokens"] for c in calls)
            res["elapsed_total"] = round(sum(c["elapsed"] for c in calls), 2)
            pipe.append(res)
        return pipe

    pipes: list = [None] * parallel
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(_one_pipe, pid): pid for pid in range(1, parallel + 1)}
        done = 0
        for fut in as_completed(futures):
            pid = futures[fut]
            pipes[pid - 1] = fut.result()
            done += 1
            if on_progress:
                on_progress(done, parallel, _aggregate(way_specs, pipes))
    return _aggregate(way_specs, pipes)
