#!/usr/bin/env python3
"""
Novel 4-Dimension Check — 章内连贯性一次判定（Qwen2.5-3B）

一次 LLM 调用同时判定相邻两段的四个维度：
  time_ok   时间衔接   （前段尾 → 后段头时间推进是否自然）
  emotion_ok 情绪匹配  （后段情绪基调是否符合规划 tone，允许同义表达）
  topic_ok  话题过渡   （两段话题衔接是否自然）
  char_ok   角色承接   （结合角色注册表检索出的出现上下文，判进出/消失是否合理）

输入组装：
  - 前段尾 300 字 + 后段头 300 字 + 后段尾 200 字（节选标注，D1 窗口实测最优）
  - 后段规划情绪（sub_structures[tone]）
  - 本章角色出现上下文（角色注册表 name+aliases 别名展开 → 各子结构全文检索
    → 每个出现位置 ±40 字 + 段标识 S0X，只列本章确出现过的角色）

3B 不可用 / 加载失败 / 输出不可解析 → 返回 None（调用方回退老规则检查）。

设计依据（实测）：
  - D1 窗口（尾300+头300+尾200 节选标注）与全段判定一致性 81%，成本减半
  - 角色承接不能靠窗口（角色名常在段中）→ 注册表全文检索上下文，3B 语义判合理性
"""
import json
import os
import sys
import re
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent

DIM_LABELS = {
    "time_ok": "时间衔接",
    "emotion_ok": "情绪匹配",
    "topic_ok": "话题过渡",
    "char_ok": "角色承接",
}

DIM_SUGGEST = {
    "time_ok": "在开头补充时间定位或过渡（当前文风不变）",
    "emotion_ok": "调整情绪基调以符合规划情绪",
    "topic_ok": "增加两段间的话题过渡句",
    "char_ok": "检查角色进出是否合理，补充退场/承接交代",
}

PROMPT_TPL = """你是小说章内连贯性审核员。基于【共享上下文】（下方全部输入，为各维度各自摘取的合并），判断【当前相邻两段】的四个维度。
【关键要求】下方所有输入为共享上下文——判断任一维度时可参考任意部分（含其他维度摘取的内容，如角色上下文里的段落、规划情绪、前段尾/后段头等）；四个维度的判定提示词相互独立（各按各的标准判），但上下文全程共享，不得因"某段内容属于另一维度摘取"而忽略它。通过的维度理由写"通过"或简短说明，不通过的写该维度自己的具体问题。

四个维度：
1. 时间衔接：前段尾到后段头的时间推进是否有叙事目的（闪回/蒙太奇/时间跳跃=文学手法，允许；无叙事目的的时间断裂或前后事实矛盾=不通过）
2. 情绪匹配：后段情绪基调是否符合规划情绪「{emotion}」（允许同义表达，如"专注"可用"凝神/目不转睛"）
3. 话题过渡：两段话题转折是否服务剧情推进（起承转合/视角切换/话题自然演化=正常叙事，允许；与剧情无关的游离、丢失前文关键线索=不通过）
4. 角色承接：结合【本章角色出现上下文】，判断前段出现的角色在后段的进出是否合理
   （自然退场/场景切换/情绪转变=合理；无交代的异常消失/状态断裂=不合理）

只输出一个 JSON 对象，不要解释、不要 markdown 围栏：
{{"time_ok": true/false, "time_reason": "仅时间维度的理由",
  "emotion_ok": true/false, "emotion_reason": "仅情绪维度的理由",
  "topic_ok": true/false, "topic_reason": "仅话题维度的理由",
  "char_ok": true/false, "char_reason": "仅角色维度的理由"}}

【共享上下文·前段结尾】（时间/话题维度摘取）
{prev_tail}

【共享上下文·后段开头】（时间/话题/情绪维度摘取）
{curr_head}

【共享上下文·后段结尾节选】（情绪维度摘取）
{curr_end}

【共享上下文·后段规划情绪】（情绪维度摘取）{emotion}

【共享上下文·本章角色出现上下文】（角色维度摘取：名字+代称全文检索，括号内为子结构段标识，……内为出现位置附近原文）
{char_ctx}"""


def _load_config() -> dict:
    """读项目根 config.json 的 novel_checks（与 config_manager 同源）；失败返回空。"""
    try:
        import json as _json
        # __file__ = structured_writer/novel/novel_4dim_check.py → 项目根 = 上三级
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config.json"
        if cfg_path.is_file():
            d = _json.loads(cfg_path.read_text(encoding="utf-8"))
            return d.get("novel_checks", {}) or {}
    except Exception:
        pass
    return {}


def _load_model():
    """懒加载判定模型：lmstudio 后端（8B/7B 走 LM Studio GPU，lms load）或 transformers 后端（3B）。

    返回统一句柄（transformers: (model, tokenizer)；lmstudio: make_lms_handle 闭包）；
    失败返回 None。不设模块级缓存——判定模型"测完即卸"（用户铁律：8B 测完彻底卸载再加载 7B，
    显存错峰；卸载由 release() 识别句柄后 lms unload）。
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from model_backend import judge_backend, judge_gguf_paths, make_lms_handle
        cfg = _load_config()
        backend = judge_backend(cfg)
        if backend == "lmstudio":
            keys = judge_gguf_paths(cfg)
            key = keys.get("4dim") or keys.get("r1") or ""
            if not key:
                print("[4维判定] LM Studio 模型库无判定模型（需下载 Qwen3-8B Q4_K_M），回退规则")
                return None
            print(f"[4维判定] 后端: LM Studio（{key}）")
            # 窗口固定 16384（lms load -c；覆盖 4维 prompt ~3K + 输出，R1 ~13K）——LM Studio 默认窗口不保证够
            return make_lms_handle(key, ctx=cfg.get("judge_n_ctx") or 16384)
    except Exception as e:
        print(f"[4维判定] lmstudio 后端异常（回退 transformers）: {e}")
    # transformers 后端（3B，现状）
    try:
        from novel_timeline_extractor import _load_extract_model
        loaded = _load_extract_model()
        if loaded is None:
            return None
        model, tokenizer = loaded
        return model, tokenizer
    except Exception as e:
        print(f"[4维判定] 模型加载失败（回退规则）: {e}")
        return None


def _strip_title(text: str) -> str:
    """去掉正文首行标题（L01 · S01《xxx》），避免标题污染判定。"""
    lines = text.split("\n")
    return "\n".join(
        l for l in lines
        if l.strip() and not re.match(r"^L0\d+\s*·\s*S\d+", l.strip())
    )


def _char_contexts(state: dict, chapter_dir) -> str:
    """角色注册表别名展开 → 各子结构全文检索 → 出现位置 ±40 字 + 段标识。

    只列本章确出现过的角色；每角色最多 4 条上下文（跨段取样，防输入爆炸）。
    无注册角色或零命中 → 返回占位说明（prompt 仍可用）。
    """
    chars = state.get("characters", [])
    if not isinstance(chars, list) or not chars:
        return "（无注册角色数据）"
    entries = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if not name:
            continue
        aliases = c.get("aliases") or []
        keys = [name] + [a for a in aliases if isinstance(a, str) and a]
        entries.append((name, keys))

    seg_texts = {}
    files = sorted(Path(chapter_dir).glob("S*.txt"))
    for f in files:
        seg_texts[f.stem] = f.read_text(encoding="utf-8-sig")

    ctx_lines = []
    for name, keys in entries:
        hits = []
        for seg in sorted(seg_texts.keys()):
            text = seg_texts[seg]
            for key in keys:
                start = 0
                while True:
                    i = text.find(key, start)
                    if i < 0:
                        break
                    ctx = re.sub(r"\s+", " ", text[max(0, i - 40):i + 40])
                    hits.append(f"{seg}@…{ctx}…")
                    start = i + len(key)
        if hits:
            ctx_lines.append(f"{name}: " + " | ".join(hits[:4]))
    return "\n".join(ctx_lines) if ctx_lines else "（本章无注册角色出现）"


def _parse_json(raw: str) -> dict | None:
    """容错解析 3B 输出 JSON。"""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if isinstance(obj, dict) and all(f"{k}" in obj for k in DIM_LABELS):
            return obj
    except Exception:
        pass
    return None


def _dim_reason(obj: dict, dim: str) -> str:
    """取某维度的独立理由（time_ok → time_reason），缺失时回退空。"""
    key = dim.replace("_ok", "") + "_reason"
    r = obj.get(key) or obj.get("reason") or ""
    return str(r).strip()[:100]


def _judge(handle, prompt: str) -> dict | None:
    """单次调用判 5 维（统一生成接口，双后端）。"""
    from model_backend import generate
    # LM Studio 后端（Qwen3-8B）：Qwen3 默认思考模式会吞掉大量输出 token——
    # 判定要的是直接 JSON，注入 /no_think 关思考（transformers 3B 无思考概念，不注入）
    if not isinstance(handle, tuple):
        prompt = "/no_think\n" + prompt
    raw = generate(handle, prompt, max_tokens=1024, temperature=0.2)
    return _parse_json(raw)


FIDELITY_PROMPT = """你是小说大纲忠实度判定器。判断【正文】是否支持【规划概述】——正文是否实现了概述承诺的内容（允许同义改写、细节补充，但关键事件/状态不得缺失或反转）。
只输出一个 JSON 对象，不要解释、不要 markdown 围栏：
{{"fidelity_ok": true/false, "reason": "一句话理由（支持/不支持的具体依据）"}}

【规划概述】
{summary}

【正文】
{content}"""


def fidelity_judge(handle, summary: str, content: str) -> tuple | None:
    """3B/8B 判"正文是否支持子结构 summary"。返回 (bool, reason)；不可解析返回 None。

    正文过长时截取前 1000 字（忠实度看"承诺是否兑现"，主体信息集中在前段）。
    LM Studio 后端（8B）同样注入 /no_think 关思考。
    """
    if len(content) > 1000:
        content = content[:1000] + "\n……（后略）……"
    prompt = FIDELITY_PROMPT.format(summary=summary[:300], content=content)
    if not isinstance(handle, tuple):
        prompt = "/no_think\n" + prompt
    from model_backend import generate
    raw = generate(handle, prompt, max_tokens=1024, temperature=0.2)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        ok = obj.get("fidelity_ok")
        if isinstance(ok, bool):
            return ok, str(obj.get("reason") or "").strip()[:120]
    except Exception:
        pass
    return None


def check_4dim(state_path: str, chapter: str, chapter_dir) -> list | None:
    """章内连贯性 4 维判定。返回 issues 列表；3B 不可用返回 None（规则回退）。

    issues 元素与现有章检兼容：
      {"file", "problem", "position", "severity", "suggestion"}
    """
    loaded = _load_model()
    if loaded is None:
        return None
    handle = loaded
    try:
        return _check_4dim_impl(handle, state_path, chapter, chapter_dir)
    finally:
        # 独占串行（默认）：判定模型测完即卸（lms unload），显存让给下一模型（7B/35B）；
        # 关闭（并行）：驻留不卸——多模型常驻，适合显存充足硬件
        try:
            if _load_config().get("exclusive_serial", True):
                from model_backend import release as _mb_release
                _mb_release(handle)
        except Exception:
            pass


def _check_4dim_impl(handle, state_path: str, chapter: str, chapter_dir) -> list | None:
    """4维判定主体（handle 统一句柄；异常/不可用 → None 规则回退）。"""
    if not Path(state_path).is_file():
        return None
    state = json.loads(Path(state_path).read_text(encoding="utf-8-sig"))

    # 子结构规划情绪（按文件名 S0X → s_key）
    ch_info = next((c for c in state.get("chapters", []) if c.get("id") == chapter), None)
    subs = (ch_info or {}).get("sub_structures") or {}
    tones = {}
    for sk, sv in subs.items():
        if isinstance(sv, dict):
            tones[sk] = sv.get("tone", "")

    char_ctx = _char_contexts(state, chapter_dir)

    files = sorted(Path(chapter_dir).glob("S*.txt"))
    if len(files) < 2:
        return []

    issues = []
    for i in range(1, len(files)):
        prev_stem, curr_stem = files[i - 1].stem, files[i].stem
        prev_full = _strip_title(files[i - 1].read_text(encoding="utf-8-sig"))
        curr_full = _strip_title(files[i].read_text(encoding="utf-8-sig"))
        emotion = tones.get(curr_stem) or "（未规划）"

        prev_tail = prev_full[-300:]  # 时间/话题维度取前段尾部局部接缝（每维各自摘取：时间/话题=局部，情绪=规划对照，角色=全文上下文）
        curr_head = curr_full[:300]
        curr_end = curr_full[-200:]
        prompt = PROMPT_TPL.format(
            prev_tail=prev_tail,
            curr_head=curr_head,
            curr_end=curr_end,
            emotion=emotion,
            char_ctx=char_ctx,
        )
        obj = _judge(handle, prompt)
        if obj is None:
            print(f"[4维判定] {prev_stem}→{curr_stem} 输出不可解析，跳过该对")
            continue
        for dim, label in DIM_LABELS.items():
            if obj.get(dim) is False:
                issues.append({
                    "file": f"{prev_stem}.txt",   # 前段（修复目标；output 补 4维 路径同指前段）
                    "problem": f"[{label}] {_dim_reason(obj, dim)}",
                    "position": f"{curr_stem} 开头",
                    "severity": "SOFT",
                    "suggestion": DIM_SUGGEST[dim],
                })
        print(f"[4维判定] {prev_stem}→{curr_stem}: t={obj.get('time_ok')} e={obj.get('emotion_ok')} p={obj.get('topic_ok')} c={obj.get('char_ok')}")

    return issues
