#!/usr/bin/env python3
"""
Reasoning Check - 推理审核引擎 (v3.0)
基于 deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B (transformers) 实现推理级别内容审核. CPU 可跑.

定位: finalize-chapter 第6步 (BERT 语义之后)
有模型 -> 执行5项推理审核
无模型 -> 自动跳过, 不影响现有流程

推理审核项目:
  1. 因果合理性 [HARD]
  2. 人物行为一致性 [HARD]
  3. 情绪弧自然度 [SOFT]
  4. 对话匹配度 [SOFT]
  5. 论证可靠性 [SOFT]

依赖:
  - transformers + torch (pip 安装, 有 prebuilt wheel, 无需编译)
  - deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B (transformers 格式, 首次加载自动下载 ~1GB)

安装:
  pip install transformers torch -i https://mirrors.aliyun.com/pypi/simple/
  HF_ENDPOINT=https://hf-mirror.com python -c "from transformers import AutoModel; AutoModel.from_pretrained('deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B', trust_remote_code=True)"
"""
import json, sys, re, os
from pathlib import Path

from nover_config import JUDGE_TEMPERATURE

_DEVICE = "cpu"

MODEL_NAME = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"
MODEL_CACHE = str(Path.home() / ".cache" / "huggingface" / "hub")

DIMENSIONS = [
    {"key": "causality", "name": "因果合理性", "hard": True,
     "desc": "事件是否有前文铺垫，转折是否有叙事逻辑（大转折允许，但需有因果呼应）"},
    {"key": "character_consistency", "name": "人物行为一致性", "hard": True,
     "desc": "角色行为是否符合其人格设定"},
    {"key": "emotion_arc", "name": "情绪弧自然度", "hard": False,
     "desc": "情绪转变是否有铺垫与后文呼应（情绪大起大落是人物弧光，允许；无铺垫无呼应的情绪跳跃=问题）"},
    {"key": "dialogue", "name": "对话匹配度", "hard": False,
     "desc": "对话是否符合角色身份/性格/处境"},
    {"key": "reasoning", "name": "论证可靠性", "hard": False,
     "desc": "角色的推理/判断是否有逻辑漏洞"},
]


def _download_model():
    """逐文件下载模型 (避免 snapshot_download 卡死), 支持 hf-mirror 降级"""
    import os as _os
    _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    _os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

    from huggingface_hub import hf_hub_download, list_repo_files
    import time

    try:
        files = list_repo_files(MODEL_NAME)
    except Exception:
        _os.environ["HF_ENDPOINT"] = "https://huggingface.co"
        try:
            files = list_repo_files(MODEL_NAME)
        except Exception as e:
            print(f"[推理审核] 无法获取文件列表: {e}")
            return False

    skip_patterns = [".gitattributes", "onnx/", "flax/", "tf/"]
    essentials = [f for f in files if not any(p in f for p in skip_patterns)]
    print(f"[推理审核] 需要下载 {len(essentials)} 个文件")

    success = True
    for fname in essentials:
        try:
            print(f"  -> {fname}...", end="", flush=True)
            t0 = time.time()
            hf_hub_download(MODEL_NAME, fname)
            elapsed = time.time() - t0
            print(f" 完成 ({elapsed:.1f}s)")
        except Exception as e:
            print(f" 失败: {e}")
            success = False
    return success


def _load_config() -> dict:
    """读项目根 config.json 的 novel_checks（与 config_manager 同源）；失败返回空。"""
    try:
        import json as _json
        # __file__ = structured_writer/novel/novel_reasoning_check.py → 项目根 = 上三级
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config.json"
        if cfg_path.is_file():
            d = _json.loads(cfg_path.read_text(encoding="utf-8"))
            return d.get("novel_checks", {}) or {}
    except Exception:
        pass
    return {}


def _load_model():
    """懒加载推理审核模型：lmstudio 后端（7B 走 LM Studio GPU，lms load）或 transformers 后端（1.5B）。

    返回统一句柄（transformers: (model, tokenizer)；lmstudio: make_lms_handle 闭包）；
    不可用返回 (None, None)。不设模块级缓存——判定模型"测完即卸"（用户铁律：8B/7B 测完
    彻底卸载再加载后续模型；卸载由 release() 识别句柄后 lms unload）。
    """
    llm = None
    tok = None
    try:
        import os as _os
        sys.path.insert(0, str(Path(__file__).parent))
        from model_backend import judge_backend, judge_gguf_paths, make_lms_handle
        cfg = _load_config()
        if judge_backend(cfg) == "lmstudio":
            keys = judge_gguf_paths(cfg)
            key = keys.get("r1") or keys.get("4dim") or ""
            if not key:
                print("[推理审核] LM Studio 模型库无判定模型（需下载 DeepSeek-R1-Distill-Qwen-7B Q4），跳过")
                return None, None
            print(f"[推理审核] 后端: LM Studio（{key}）")
            # 窗口固定 16384（lms load -c；R1 思考链 1-3K + JSON ≈ 13K 覆盖）
            llm = make_lms_handle(key, ctx=cfg.get("judge_n_ctx") or 16384)
            return llm, None
    except Exception as e:
        print(f"[推理审核] lmstudio 后端异常（回退 transformers）: {e}")
    # transformers 后端（1.5B，现状）
    try:
        # 强制 CPU，避免与 LM Studio 抢夺 GPU 显存
        _os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
        import torch
        # transformers 导入时需要 HF_ENDPOINT 指向镜像以避免挂死
        _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from transformers import AutoModelForCausalLM, AutoTokenizer

        # 强制 CPU（CUDA_VISIBLE_DEVICES=-1 后 torch.cuda.is_available() 返回 False）
        _DEVICE = "cpu"
        device_map = _DEVICE
        print(f"[推理审核] 设备: {_DEVICE.upper()}" + (f" ({torch.cuda.get_device_name(0)})" if _DEVICE == "cuda" else ""))

        # 查找本地模型缓存（structured-writer data/models/ 优先，其次 HF 默认缓存）
        default_cache = str(Path.home() / ".cache" / "huggingface" / "hub")
        default_snap = _os.path.join(default_cache, f"models--{MODEL_NAME.replace('/', '--')}", "snapshots")
        try:
            from _path_utils import MODELS_DIR
            model_cache = str(MODELS_DIR)
        except Exception:
            model_cache = default_cache

        # 先检查 MODELS_DIR，再检查默认 HF 缓存
        model_local_path = ""
        for cache_dir in [model_cache, default_cache]:
            snap_dir = _os.path.join(cache_dir, f"models--{MODEL_NAME.replace('/', '--')}", "snapshots")
            if _os.path.isdir(snap_dir):
                snap_items = [d for d in _os.listdir(snap_dir) if _os.path.isdir(_os.path.join(snap_dir, d))]
                if snap_items:
                    model_local_path = _os.path.join(snap_dir, snap_items[-1])
                    print(f"[推理审核] 本地缓存: {model_local_path}")
                    break

        if not model_local_path:
            # 没本地模型 → 跳过，绝不联网下载
            print("[推理审核] 跳过：本地无模型缓存，用户需主动安装")
            print(f"[推理审核] 安装命令: HF_ENDPOINT=https://hf-mirror.com python -c \"from transformers import AutoModel; AutoModel.from_pretrained('{MODEL_NAME}', trust_remote_code=True)\"")
            return None, None

        print(f"[推理审核] 加载模型...")
        tok = AutoTokenizer.from_pretrained(model_local_path, trust_remote_code=True)
        llm = AutoModelForCausalLM.from_pretrained(
            model_local_path,
            trust_remote_code=True,
            torch_dtype="auto",
            device_map=device_map,
            low_cpu_mem_usage=True,
        )
        print(f"[推理审核] 加载完成")
    except ImportError:
        print("[推理审核] 模型不可用: 未安装 transformers/torch")
        print("[推理审核] 安装: pip install transformers torch -i https://mirrors.aliyun.com/pypi/simple/")
        llm = None
        tok = None
    except Exception as e:
        print(f"[推理审核] 模型加载失败: {e}")
        print("[推理审核] 模型将自动跳过, 不影响现有流程")
        llm = None
        tok = None
    return llm, tok


def _strip_think(text: str) -> str:
    """剥离 <think>...</think> 推理块"""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _read_sub_file(chapter_dir, sub_key):
    """读取子结构文件正文 (跳过标题行和末行标记)"""
    p = Path(chapter_dir) / f"{sub_key}.txt"
    if not p.exists():
        return ""
    lines = p.read_text(encoding="utf-8-sig").strip().split("\n")
    lines = [l for l in lines if not re.match(r'L\d+ \xb7 S\d+<', l.strip())]
    if lines and re.match(r'L\d+S\d+', lines[-1].strip()):
        lines = lines[:-1]
    return "\n".join(lines)


def _build_prompt(data, chapter, chapter_dir) -> str:
    """构建推理审核 prompt"""
    ch_info = None
    for ch in data.get("chapters", []):
        if ch["id"] == chapter:
            ch_info = ch
            break
    if not ch_info:
        return ""

    subs = ch_info.get("sub_structures", {})
    sorted_keys = sorted(subs.keys())

    char_lines = []
    for c in data.get("characters", []):
        name = c.get("name", "")
        role = c.get("role", "")
        mbti = c.get("mbti", "")
        archetype = c.get("archetype", "")
        traits = c.get("traits", [])
        aliases = c.get("aliases", [])

        parts = [f"[{name}]"]
        if role:
            parts.append(f"[{role}]")
        if mbti or archetype:
            parts.append(f"[{mbti or ''}] [{archetype or ''}]")
        if traits:
            parts.append(f"[特质: {', '.join(traits[:4])}]")
        if aliases:
            parts.append(f"[别名: {', '.join(aliases)}]")
        char_lines.append(" ".join(parts))

    char_setting = "\n".join(char_lines) if char_lines else "(无角色设定)"

    # 文风规则注入：判定前必须知晓题材/叙事约束——设定内合法的表达（如叙述者视角/系统日志/代码块）
    # 不算文风问题。缺失时审核模型按通用小说标准误判（如"机器腔=问题"）。
    style = data.get("writing_style") or {}
    rules = style.get("custom_rules") or ""
    if rules:
        style_block = (
            f"\n[文风规则]\n{rules}\n\n"
            "[判定约束]\n"
            "1. 先读[文风规则]再判各维度——规则允许的表达（如叙述者本身的设定视角、系统日志/代码块/警告标记等修辞工具）不属于文风问题。\n"
            "2. 各维度只判其自身维度（因果/人物一致性/情绪弧/对话匹配/论证可靠），不把文风规则允许的内容当作违规。\n"
        )
    else:
        style_block = ""

    sub_lines = []
    for sk in sorted_keys:
        sv = subs[sk]
        tone = sv.get("tone", "")
        emotions = sv.get("emotions", [])
        emo_str = ""
        if emotions:
            emo_parts = []
            for e in emotions:
                if isinstance(e, dict):
                    emo_parts.append(f"{e.get('type','')}({e.get('intensity',0):.1f})")
                else:
                    emo_parts.append(str(e))
            emo_str = " [" + ", ".join(emo_parts) + "]"
        sub_lines.append(f"  {sk} <{sv.get('title','')}>: {sv.get('summary','')} | tone={tone}{emo_str}")
    sub_plan = "\n".join(sub_lines) if sub_lines else "(无子结构规划)"

    content_parts = []
    for sk in sorted_keys:
        text = _read_sub_file(chapter_dir, sk)
        if not text.strip():
            continue
        lines = text.strip().split("\n")
        preview = "\n".join(lines[:15])
        if len(lines) > 23:
            preview += "\n    ...(中间省略)..."
            preview += "\n" + "\n".join(lines[-8:])
        content_parts.append(f"-- {sk} --\n{preview}")
    chapter_content = "\n\n".join(content_parts) if content_parts else "(无正文)"

    dim_lines = []
    for d in DIMENSIONS:
        level = "[硬性]" if d["hard"] else "[参考]"
        dim_lines.append(f"{level} {d['name']}: {d['desc']}")
    dims_str = "\n".join(dim_lines)

    prompt = f"""你是一个专业的小说审核编辑. 请审核以下章节内容, 严格按指定 JSON 格式输出审核结果.

[角色设定]
{char_setting}
{style_block}
[章节概述]
{ch_info.get('overview', '(无概述)')}

[子结构规划]
{sub_plan}

[正文预览]
{chapter_content}

[审核维度]
{dims_str}

[输出要求]
以 JSON 数组格式输出, 每项格式:
{{"dimension": "维度名", "result": "PASS"|"HARD"|"SOFT", "detail": "具体说明(20-50字)", "sub": "涉及的具体子结构编号"}}
sub 字段：从[正文预览]的段标识（-- S01 -- 等）定位该问题主要涉及的子结构，填 "S01"/"S02"...；
不涉及具体某段（整章性/跨段问题）或无法定位时填 null。
必须包含全部 5 个维度, 仅输出 JSON 数组, 不要有其他文字.

[输出示例]
[
  {{"dimension": "因果合理性", "result": "PASS", "detail": "前文铺垫充分，转折逻辑合理", "sub": null}},
  {{"dimension": "人物行为一致性", "result": "SOFT", "detail": "角色情绪转变略显突兀，缺过渡铺垫", "sub": "S03"}},
  {{"dimension": "情绪弧自然度", "result": "PASS", "detail": "情绪递进自然，与事件节奏匹配", "sub": null}},
  {{"dimension": "对话匹配度", "result": "HARD", "detail": "对话用词超出角色设定，与身份不符", "sub": "S02"}},
  {{"dimension": "论证可靠性", "result": "PASS", "detail": "推理链条完整，无逻辑漏洞", "sub": null}}
]"""
    return prompt


def _parse_reasoning_results(cleaned: str) -> list:
    """从模型输出中提取审核结果列表（兼容代码块/裸 JSON/单对象包装）。失败返回空列表。"""
    # 先提取 ```json ... ``` 代码块
    code_match = re.search(r'```(?:json)?\s*(.*?)\s*```', cleaned, re.DOTALL)
    text_to_parse = code_match.group(1) if code_match else cleaned

    results = None
    # 找所有独立的 JSON 对象
    all_objs = []
    for m in re.finditer(r'\{[^{}]*\}', text_to_parse):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                all_objs.append(obj)
        except (json.JSONDecodeError, TypeError):
            pass

    if all_objs and len(all_objs) >= 3:
        results = all_objs
    elif len(all_objs) == 1:
        obj = all_objs[0]
        for key in ("dimensions", "results", "items", "issues"):
            if isinstance(obj.get(key), list) and len(obj[key]) > 0:
                results = obj[key]
                break
        if results is None:
            results = all_objs
    if results is None:
        try:
            obj = json.loads(text_to_parse)
            if isinstance(obj, list):
                results = obj
            elif isinstance(obj, dict):
                for key in ("dimensions", "results", "items", "issues"):
                    if isinstance(obj.get(key), list):
                        results = obj[key]
                        break
        except json.JSONDecodeError:
            pass
    if not isinstance(results, list):
        results = [results] if results else []
    return results


def check_reasoning(state_path, chapter, chapter_dir):
    """
    推理审核主入口.
    返回 issues list, 格式同 finalize-chapter 标准.
    """
    issues = []

    sp = Path(state_path)
    if not sp.exists():
        return issues
    data = json.loads(sp.read_text(encoding="utf-8-sig"))

    model, tokenizer = _load_model()
    if model is None:
        print("\n  [推理审核] 跳过 (判定模型不可用：LM Studio 库缺 7B 或 transformers 缺 1.5B)")
        return issues
    try:
        return _reasoning_impl(model, tokenizer, data, issues, state_path, chapter, chapter_dir)
    finally:
        # 独占串行（默认）：判定模型测完即卸（lms unload），显存让给下一模型（8B/35B）；
        # 关闭（并行）：驻留不卸——多模型常驻，适合显存充足硬件
        try:
            if _load_config().get("exclusive_serial", True):
                from model_backend import release as _mb_release
                _mb_release(model)
        except Exception:
            pass


def _reasoning_impl(model, tokenizer, data, issues, state_path, chapter, chapter_dir):
    # ── 生成 + 解析（格式失败 → 打回纠正重试，最多 3 次；避免"格式漂移直接标记失败"导致审核结果不可见）──
    # 统一句柄：lmstudio（make_lms_handle 闭包）→ mb_generate 直出 HTTP；transformers → pipeline
    prompt = _build_prompt(data, chapter, chapter_dir)
    if not prompt:
        print("  [推理审核] 跳过: 无法构建 prompt")
        return issues
    from model_backend import generate as mb_generate, judge_backend
    _is_lms = judge_backend(_load_config()) == "lmstudio"
    pipe = None
    if not _is_lms:
        try:
            from transformers import pipeline as _pipeline
            pipe = _pipeline("text-generation", model=model, tokenizer=tokenizer, device=0 if _DEVICE == "cuda" else -1)
        except Exception as e:
            print(f"\n  [推理审核] 模型加载异常: {e}")
            return issues

    results = None
    last_raw = ""
    _R1_MAX_TOKENS = 8192  # R1 是思考模型：思考链 1-3K + JSON 5 维 ≈ 800，4096 偏紧 → 8192 给思考留足
    for attempt in range(1, 4):  # 最多 3 次（第 1 次原始 prompt，第 2-3 次带纠错提示）
        try:
            if _is_lms:
                raw_output = mb_generate(model, prompt, max_tokens=_R1_MAX_TOKENS, temperature=JUDGE_TEMPERATURE)
            else:
                output = pipe(
                    prompt,
                    max_new_tokens=_R1_MAX_TOKENS,
                    temperature=JUDGE_TEMPERATURE,
                    top_p=0.9,
                    do_sample=True,
                    pad_token_id=tokenizer.eos_token_id,
                )
                raw_output = output[0]["generated_text"]
                if raw_output.startswith(prompt):
                    raw_output = raw_output[len(prompt):]
        except Exception as e:
            print(f"\n  [推理审核] 推理异常(第{attempt}次): {e}")
            if attempt == 3:
                print("  -> 跳过推理审核, 不影响现有流程")
                return issues
            continue

        last_raw = raw_output
        cleaned = _strip_think(raw_output)

        results = _parse_reasoning_results(cleaned)
        if results:
            if attempt > 1:
                print(f"  [推理审核] ✅ 第{attempt}次纠正后解析成功")
            break

        # 解析失败 → 打回纠正：把原始输出 + 错误说明喂回，要求重新按格式输出
        print(f"  [推理审核] ⚠️ 第{attempt}次输出格式不规范，打回纠正重试...")
        prompt = (
            "你上一次的输出格式不符合要求（必须是 JSON 数组，每项含 dimension/result/detail/sub，"
            "result 只能是 PASS/HARD/SOFT 之一）。\n\n"
            "你上一次的输出如下：\n"
            f"---\n{last_raw[:800]}\n---\n\n"
            "请忽略上一次输出，重新严格按以下 JSON 数组格式审核同一章节，仅输出 JSON 数组，不要任何解释：\n"
            "[\n"
            '  {"dimension": "因果合理性", "result": "PASS|HARD|SOFT", "detail": "说明", "sub": "S01"},\n'
            '  {"dimension": "人物行为一致性", "result": "PASS|HARD|SOFT", "detail": "说明", "sub": null},\n'
            '  {"dimension": "情绪弧自然度", "result": "PASS|HARD|SOFT", "detail": "说明", "sub": "S02"},\n'
            '  {"dimension": "对话匹配度", "result": "PASS|HARD|SOFT", "detail": "说明", "sub": "S03"},\n'
            '  {"dimension": "论证可靠性", "result": "PASS|HARD|SOFT", "detail": "说明", "sub": null}\n'
            "]"
        )

    if not results:
        # 3 次重试仍失败：明确标记「推理审核失败」（SOFT，调用方聚合可见，人工复核）
        print("  [推理审核] ⚠️ 3 次重试后仍无法解析 — 标记审核失败（SOFT，需人工复核）")
        issues.append({
            "file": chapter,
            "problem": "推理审核失败：模型 3 次输出均无法解析为审核 JSON（格式持续漂移/模型异常）",
            "position": f"{chapter} reasoning",
            "severity": "SOFT",
            "suggestion": "推理审核未完成：本章因果/人格/情绪弧未经模型审核。可重跑完结审核，或人工复核后确认。"
        })
        return issues

    for item in results:
        if not isinstance(item, dict):
            continue
        dim = item.get("dimension", "") or item.get("name", "") or item.get("dim", "") or ""
        result_raw = str(item.get("result", "PASS"))
        detail = item.get("detail", "") or item.get("description", "") or item.get("explanation", "") or ""
        # 模型有时把值填反了: dimension 字段有 SOFT/HARD, result 字段有说明文字
        if result_raw in ("HARD", "SOFT", "PASS"):
            result = result_raw
        elif dim in ("HARD", "SOFT", "PASS"):
            result = dim
            dim = ""
        else:
            # 模型可能用自然语言写 result 字段（如"符合设定"）——PASS 语义绝不误报 SOFT 逼用户修复
            _txt = f"{result_raw} {detail}"
            if any(w in _txt for w in ("符合", "一致", "正常", "无问题", "通过", "合理", "恰当", "到位", "没有矛盾", "无异常", "无误")):
                result = "PASS"
            elif any(w in _txt for w in ("不符合", "不一致", "矛盾", "突兀", "异常", "缺失", "不足", "欠妥", "生硬", "断裂")):
                result = "SOFT"  # 问题词 → 保守 SOFT（供人工复核）
            else:
                result = "SOFT"
        result = result.upper()
        # 无法评估语义（R1 明确表示该维度无法评估——暂无对话/无内容/无法判断/不适用）
        # → 跳过，不是问题（b19：R1 会把"无法评估"标成 HARD/SOFT 逼用户处理，同"符合设定"类误报）
        if any(w in f"{result_raw} {detail}" for w in (
                "无法评估", "无法判断", "无法确认", "无法核验", "无法验证", "暂无法",
                "暂无对话", "无对话内容", "没有对话", "对话内容不足", "暂无内容", "无内容可", "不适用")):
            continue
        if result == "PASS":
            continue
        # sub 定位：模型输出 S01/S02 → 子结构文件（修复面板可勾选重构）；否则章级
        sub = str(item.get("sub") or "").strip()
        issue_file = f"{sub}.txt" if re.match(r"^S\d+$", sub) else chapter
        issue_pos = f"{sub} {chapter} reasoning" if issue_file != chapter else f"{chapter} reasoning"
        if result == "HARD":
            issues.append({
                "file": issue_file,
                "problem": f"推理审核 - {dim}: {detail}",
                "position": issue_pos,
                "severity": "HARD",
                "suggestion": f"请检查{dim}问题, 根据审核建议修改后重新 finalize-chapter"
            })
        elif result == "SOFT":
            issues.append({
                "file": issue_file,
                "problem": f"推理审核 - {dim}: {detail}",
                "position": issue_pos,
                "severity": "SOFT",
                "suggestion": "参考审核建议, 如需要可手动修改"
            })

    h_count = len([i for i in issues if i.get("severity") == "HARD"])
    s_count = len([i for i in issues if i.get("severity") == "SOFT"])
    print(f"\n{'─'*50}")
    print(f"[推理审核] 完成: {h_count} HARD + {s_count} SOFT")
    print(f"{'='*50}")

    return issues


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("用法: python novel_reasoning_check.py <state_path> <chapter> <chapter_dir>")
        print("  安装: pip install transformers torch -i https://mirrors.aliyun.com/pypi/simple/")
        print("  模型: 首次运行自动下载 DeepSeek-R1-Distill-Qwen-1.5B (~1GB)")
        print("  下载镜像: HF_ENDPOINT=https://hf-mirror.com python ...")
        sys.exit(1)
    issues = check_reasoning(sys.argv[1], sys.argv[2], sys.argv[3])
    if issues:
        print(f"\n发现 {len(issues)} 个推理审核问题:")
        for i in issues:
            print(f"  [{i.get('severity','?')}] {i.get('problem','?')}")
    else:
        print("\n推理审核全部通过.")
