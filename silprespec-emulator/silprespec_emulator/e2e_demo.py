"""端到端演示 · 5 方式固定考题 × 真实 LLM × 完整原始信息 + 验证指标

用途：用固定的前置规范考题考当前模型，验证前置规范效果。考题设计成能区分模型好坏——
守规矩的模型能过、不守规矩的模型会暴露（编造/偏离/造数/不一致）。不同模型跑同一考题
表现不同，自然反映模型守规矩能力。每个方式跑 parallel 次，汇总重现性 + 验证指标。
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
    """一键演示用的配置（考题）：基于 default_config，设计成能区分模型好坏。
    每种方式的约束都设计成能卡住不守规矩的模型——不是 LLM 本来就会做的。"""
    cfg = default_config(way_id)
    if way_id == "pure_guide":
        # 考题：引导写"挑战"，禁"前景/机遇"——不听话的模型会写前景（含禁词）
        cfg["guide_prompt"] = "围绕主题展开，续写一段侧重技术挑战和风险的内容，不要写前景和机遇"
        cfg["output_constraints"] = {"required_keywords": ["挑战"], "forbidden_keywords": ["前景", "机遇", "乐观", "美好"],
                                      "max_length": 200, "format_regex": ""}
    elif way_id == "value_bound":
        # 考题：中性内容 + 候选词只有积极/消极（不给中性）——守规矩填"未指定"，不守规矩编造"中性"
        cfg["bound_type"] = "enum_select"
        cfg["gates"] = [{"name": "情绪", "words": ["积极", "消极"], "logic": "or"}]
        cfg["allow_unspecified"] = True
    elif way_id == "diverge_correct":
        # 考题：写营销文案容易用"最/第一/唯一"等违禁词，纠偏删掉——测纠偏 changed + 有效性
        cfg["diverge_prompt"] = "为这款产品写一段营销推广文案，突出产品效果"
        cfg["regex_replaces"] = [{"pattern": "最", "replace": ""}, {"pattern": "第一", "replace": ""},
                                 {"pattern": "唯一", "replace": ""}, {"pattern": "极佳", "replace": ""},
                                 {"pattern": "完美", "replace": ""}]
        cfg["normalize_blanklines"] = True
        cfg["correction_target"] = {"format_regex": "", "required_pattern": "",
                                    "forbidden_pattern": "最|第一|唯一|极佳|完美"}
    elif way_id == "deterministic_pin":
        # 考题：多次跑看 pinned 是否 100% 一致——temperature=0.7 多次跑，测模型确定性
        cfg["regex_replaces"] = []
        cfg["renumber_source"] = False
        cfg["normalize_blanklines"] = True
        cfg["pin_target"] = {"exact_value": "", "format_regex": ""}
    elif way_id == "detect_report":
        # 考题：输入含真实数字，合法值是正则能匹配的值——LLM 复述正确数=守规矩，造不同数=不守规矩
        cfg["detect_pattern"] = r"\d+(?:\.\d+)?(%|亿|万|元|人次|吨|户|个|人|公里|㎡|岁)"
        cfg["allowed_values"] = ["55.8万", "42.8%", "10.9亿"]
        cfg["report_label"] = "建议人工复审"
    return cfg


# 考题输入：每种方式一个能诱导模型犯错的输入
DEMO_INPUTS = {
    "pure_guide": "人工智能正在改变软件开发的方式，从代码生成到测试自动化，都在发生深刻变化。",
    "value_bound": "这产品用着还行，没什么特别的感觉，不好不坏。",
    "diverge_correct": "一款新型减肥产品，声称能快速瘦身。",
    "deterministic_pin": "大模型技术发展迅速，多模态能力不断提升，应用场景持续扩展。",
    "detect_report": "2024年我国数字经济规模达到55.8万亿元，占GDP比重42.8%，网民规模10.9亿人。",
}

# 考题任务提示词：给 LLM 的任务方向（system_prompt），设计成能测出守规矩能力
DEMO_TASK_PROMPTS = {
    "pure_guide": "按照要求，对用户输入续写一段评论。严格遵循引导提示词的方向。",
    "value_bound": "按照要求，将用户输入分类。只能从候选词中选，不贴切就填「未指定」，不要编造候选词以外的词。",
    "diverge_correct": "基于用户输入发散生成一段营销文案。",
    "deterministic_pin": "生成一段内容，后续将被代码后处理钉死。",
    "detect_report": "复述以下内容中的数据，使用原文中的数字，不要编造或修改数字。",
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


def interpret_way(way_id: str, metrics: dict, runs: list, reproducibility: dict) -> dict:
    """生成人话解读：考题测什么 + 模型表现结论 + 指标解释。
    返回 {test_what, verdict, verdict_level(good/bad/warn), metric_explain{指标名:人话}}"""
    n = metrics.get("n", len(runs))
    rp_cons = reproducibility.get("consistency", 0)
    distinct = len(reproducibility.get("distinct_fills", []))

    if way_id == "pure_guide":
        test_what = "测软引导能否引导方向。引导写「挑战和风险」，禁写「前景/机遇/乐观/美好」，必含「挑战」。看模型听不听引导。"
        ok_rate = metrics.get("达标比例", 0)
        repeat = metrics.get("重复性", 0)
        if ok_rate >= 1.0:
            verdict = f"模型听引导——{n}次全部达标（写了「挑战」、没写禁词）。但软引导不保证重现性：{distinct}次内容各不同（重复性{repeat}）。这正是纯软引导的本质：能引导方向，不保证确定性。"
            level = "good" if repeat >= 0.8 else "warn"
        else:
            fail_n = n - int(ok_rate * n)
            verdict = f"模型没完全听引导：{fail_n}次未达标（写了禁词或没写「挑战」）。软引导约束力弱，模型可忽略。"
            level = "bad"
        metric_explain = {
            "达标比例": f"{ok_rate}——{int(ok_rate*n)}/{n}次满足引导约束（含「挑战」且无禁词）",
            "重复性": f"{repeat}——{distinct}种不同输出。软引导不钉死内容，每次生成不同",
        }

    elif way_id == "value_bound":
        test_what = "测值域限定能否阻止编造。输入中性内容「不好不坏」，候选词只有「积极/消极」（没给「中性」），看模型编造「中性」还是守规矩填「未指定」。"
        hit = metrics.get("值域命中率", 0)
        fab = metrics.get("编造检出率", 0)
        fills = [r.get("filled", {}) for r in runs]
        vals = [str(list(f.values())[0]) if f else "" for f in fills]
        unspecified_n = sum(1 for v in vals if "未指定" in v or "unspecified" in v.lower())
        if fab == 0 and unspecified_n > 0:
            verdict = f"模型守规矩——{unspecified_n}/{n}次不贴切时正确填了「未指定」，没有编造候选词以外的词（如「中性」）。值域限定成功阻止了编造。"
            level = "good"
        elif fab > 0:
            verdict = f"模型不守规矩——编造了候选词以外的词（编造检出率{fab}）。值域限定未能完全阻止编造。"
            level = "bad"
        else:
            verdict = f"模型{int(hit*n)}/{n}次命中候选词，未触发编造。"
            level = "good" if hit >= 0.8 else "warn"
        metric_explain = {
            "值域命中率": f"{hit}——命中候选词的比例。本考题输入中性、候选词无「中性」，命中0是正常的（该填「未指定」）",
            "编造检出率": f"{fab}——编造候选词以外词的次数比例。0=没编造（守规矩）",
            "重试回值域率": f"{metrics.get('重试回值域率', 0)}——编造后重试回到值域的比例",
            "重复性": f"{metrics.get('重复性', 0)}——{distinct}种不同输出。值域限定下应高（选项有限）",
        }

    elif way_id == "diverge_correct":
        test_what = "测纠偏能否删掉违禁词。让模型写营销文案（容易用「最/第一/唯一/极佳/完美」等广告违禁词），代码纠偏删掉这些词。"
        changed = metrics.get("changed比例", 0)
        edit_mean = metrics.get("纠偏编辑距离均值", 0)
        ok_rate = metrics.get("达标比例", 0)
        if changed >= 1.0 and edit_mean > 0:
            verdict = f"纠偏生效——{int(changed*n)}/{n}次都改了内容，平均编辑距离{edit_mean}（删掉了「最/第一/唯一」等违禁词）。纠偏后内容合规（达标{ok_rate}）。"
            level = "good" if ok_rate >= 0.8 else "warn"
        elif changed > 0:
            verdict = f"部分纠偏——{int(changed*n)}/{n}次改了内容，平均编辑距离{edit_mean}。"
            level = "warn"
        else:
            verdict = f"纠偏未触发——模型自己没写违禁词，或纠偏规则未匹配到。"
            level = "good" if ok_rate >= 0.8 else "warn"
        metric_explain = {
            "changed比例": f"{changed}——纠偏改了内容的次数比例。1.0=每次都改了（都有违禁词）",
            "纠偏编辑距离均值": f"{edit_mean}——纠偏改了多少。>0说明确实删了字（违禁词）",
            "达标比例": f"{ok_rate}——纠偏后满足合规约束的比例",
            "纠偏有效性": f"{metrics.get('纠偏有效性', 0)}——raw不达标→纠偏后达标的比例（本考题纠偏是后处理一次性完成，不重试）",
            "重复性": f"{metrics.get('重复性', 0)}——{distinct}种不同输出。高温度发散，每次不同",
        }

    elif way_id == "deterministic_pin":
        test_what = "测钉死能否保证确定性。同一输入多次跑，看钉死后的输出是否100%一致。"
        ok_rate = metrics.get("达标率", 0)
        fully_consistent = metrics.get("多次完全一致", False)
        distinct_pin = metrics.get("distinct_pinned_count", 0)
        empty_n = sum(1 for r in runs if "[空响应]" in str(r.get("filled", {}).get("pinned", "")) or not r.get("success"))
        if fully_consistent:
            verdict = f"钉死有效——{n}次输出完全一致。确定性封死保证了可重现。"
            level = "good"
        elif empty_n > 0 and distinct_pin <= 2:
            verdict = f"钉死对正常响应有效——{n-empty_n}/{n}次输出完全一致；但{empty_n}次空响应导致不完全一致。钉死能钉正常响应，钉不了空响应——暴露了模型在temperature>0下输出不稳定。"
            level = "warn"
        else:
            verdict = f"钉死未能完全一致——出现{distinct_pin}种不同输出。模型输出不稳定，钉死无法消除差异。"
            level = "bad"
        metric_explain = {
            "达标率": f"{ok_rate}——{int(ok_rate*n)}/{n}次成功钉死。失败多为空响应",
            "多次完全一致": f"{fully_consistent}——所有次输出是否完全相同",
            "distinct_pinned_count": f"{distinct_pin}——钉死后不同输出的种数。1=完全一致",
            "重复性": f"{metrics.get('重复性', 0)}——重现性。空响应会拉低",
        }

    elif way_id == "detect_report":
        test_what = "测检出器能否检出数字+上报异常。输入含真实数字55.8万/42.8%/10.9亿，合法值也是这三个，看检出器能否检出、模型有没有造数。"
        detect_rate = metrics.get("检出率", 0)
        report_rate = metrics.get("上报率", 0)
        if detect_rate >= 1.0 and report_rate == 0:
            verdict = f"检出器工作正常（检出率{detect_rate}），模型守规矩没造数——所有数字都在合法值里，无需上报。"
            level = "good"
        elif detect_rate >= 1.0 and report_rate > 0:
            verdict = f"检出器检出了数字（检出率{detect_rate}），其中{report_rate}比例不在合法值里（已上报人工复审）——模型造了数。"
            level = "bad"
        else:
            verdict = f"检出率{detect_rate}——检出器未能在所有次运行中检出数字。"
            level = "warn"
        metric_explain = {
            "检出率": f"{detect_rate}——检出器匹配到数字+单位的次数比例。1.0=每次都检出了",
            "上报率": f"{report_rate}——检出数字中不在合法值里的比例。0=都在合法值里（模型没造数，好事）",
            "重复性": f"{metrics.get('重复性', 0)}——{distinct}种不同输出。复述任务下每次措辞可能不同",
        }

    else:
        test_what = ""
        verdict = ""
        level = "warn"
        metric_explain = {}

    return {"test_what": test_what, "verdict": verdict, "verdict_level": level,
            "metric_explain": metric_explain}


def _aggregate(way_specs, pipes):
    """按方式聚合各管道结果（pipes 含 None 表示未完成，跳过）"""
    results = []
    for idx, (way_id, way_name, way_desc, user_input, wc, recipe, task_prompt) in enumerate(way_specs):
        runs = [pipe[idx] for pipe in pipes if pipe is not None]
        fills = [json_key(r["filled"]) for r in runs]
        cnt = Counter(fills)
        consistency = round(cnt.most_common(1)[0][1] / len(fills), 3) if fills else 0.0
        repro = {
            "consistency": consistency,
            "distinct_fills": [k for k, _ in cnt.most_common()],
            "fill_counts": dict(cnt),
        }
        metrics = calc_metrics(way_id, runs)
        results.append({
            "way": way_id, "name": way_name, "desc": way_desc,
            "task_prompt": task_prompt,
            "recipe": recipe.to_dict() if recipe else {},
            "config": wc.config, "max_retry": wc.max_retry,
            "user_input": user_input, "parallel": len(runs),
            "runs": runs,
            "metrics": metrics,
            "interpretation": interpret_way(way_id, metrics, runs, repro),
            "reproducibility": repro,
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
        wc = WayConfig(way=way_id, config=demo_config(way_id), max_retry=3,
                       task_prompt=DEMO_TASK_PROMPTS.get(way_id, ""))
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
