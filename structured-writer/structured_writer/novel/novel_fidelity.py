#!/usr/bin/env python3
"""
novel-fidelity — 大纲忠实度报告生成器。

逐条对比大纲标题与实际写作内容，标记偏差位置和等级。

偏差等级：
  - PASS: 实际内容与大纲描述一致
  - INFO: 有偏差但可接受（细节补充/顺序微调）
  - WARN: 实质性偏差（新增场景/删除大纲内容/顺序调换）
  - ERROR: 完全偏离大纲描述

用法：
  python novel_fidelity.py <project_dir>
"""

import os
import sys
import json
import re

from nover_config import JUDGE_TEMPERATURE, JUDGE_MAX_TOKENS

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# — 收尾验证关键词库 —
KEY_CLOSURE_RESOLVE = ["终于", "再也不用", "结束了", "到此为止", "放下了", "释然", "和解", "再也不会",
                       "尘埃落定", "归于平静", "落定了", "划上", "句号"]
KEY_OPEN_OUTCOME = ["决定", "选择了", "放手", "转身", "离开", "踏上", "走向"]
KEY_OPEN_HINT = ["也许", "或许", "不知道", "会不会", "是否", "可能"]
KEY_SUSPENSE = ["不知道", "会不会", "是否", "还是未知数", "尚未"]
KEY_FORBIDDEN = ["未完待续", "预知后事如何", "一切才刚刚开始", "故事还在继续"]

ENDING_TYPES = ("封闭式", "开放式", "悬停式")


def _classify_ending_rules(content: str) -> str | None:
    """特征词回退：判结尾属于封闭式/开放式/悬停式。无法判定返回 None。"""
    if not content:
        return None
    tail = content[-400:] if len(content) > 400 else content
    score = {"封闭式": 0, "开放式": 0, "悬停式": 0}
    for kw in KEY_CLOSURE_RESOLVE:
        if kw in tail:
            score["封闭式"] += 1
    for kw in KEY_OPEN_OUTCOME + KEY_OPEN_HINT:
        if kw in tail:
            score["开放式"] += 1
    for kw in KEY_SUSPENSE:
        if kw in tail:
            score["悬停式"] += 1
    best = max(score, key=score.get)
    if score[best] == 0:
        return None  # 无特征词命中 → 无法判定，交给 LLM 或原规则
    return best


def _load_config() -> dict:
    """读项目根 config.json 的 novel_checks（与 4dim 同源）；失败返回空。"""
    try:
        cfg_path = os.path.join(os.path.dirname(SCRIPTS_DIR), "..", "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d.get("novel_checks", {}) or {}
    except Exception:
        pass
    return {}


def _classify_ending_llm(content: str) -> tuple | None:
    """收尾类型判定：统一管理勾选 → 8B（LM Studio GPU，/no_think 直出）；否则 Qwen2.5-3B（CPU）。

    返回 (ending_type, reason)；模型不可用/输出不可解析返回 None（触发特征词回退）。
    输入取最后 3 段（段落级，不按字符硬截）。（1.5B 实测判收尾类型不可靠，故 3B/8B。）
    """
    paras = [p.strip() for p in content.split("\n") if p.strip()]
    ending_text = "\n".join(paras[-3:]) if len(paras) >= 3 else content
    prompt = f"""你是小说收尾类型判定器。判断下面的【结尾段落】属于哪种收尾类型。

【收尾类型判据】
- 封闭式：主要冲突全部解决，无遗留悬念，有明确结束感（所有线闭合、角色获得安定、"从此以后"式收束）
- 开放式：冲突基本解决但留有"也许/未来/可能回来/门还开着"等开放信号，即使角色离开或启程，只要未来仍不确定就是开放式，不是封闭式
- 悬停式：关键冲突未解决，戛然而止（危机未解除、决定未做出、"不知如何是好"）

【结尾段落】
{ending_text}

请只输出：封闭式 / 开放式 / 悬停式（三选一）+ 一句话理由。"""
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from model_backend import judge_backend, judge_gguf_paths, make_lms_handle
        from model_backend import generate as mb_generate, release as mb_release
        if judge_backend(_load_config()) == "lmstudio":
            keys = judge_gguf_paths(_load_config())
            key = keys.get("4dim") or keys.get("r1") or ""
            if key:
                h = make_lms_handle(key, ctx=16384)
                if h:
                    try:
                        resp = mb_generate(h, "/no_think\n" + prompt, max_tokens=JUDGE_MAX_TOKENS, temperature=JUDGE_TEMPERATURE)
                        for t in ENDING_TYPES:
                            if t in resp:
                                after = resp.split(t, 1)[1]
                                reason = after.strip().split("\n")[0].strip(" ：:，,。")[:60]
                                return (t, reason or "（无理由）")
                    finally:
                        # 独占串行（默认）：用完即卸；关闭（并行）：驻留不卸
                        if _load_config().get("exclusive_serial", True):
                            mb_release(h)
                    return None
        # transformers 3B（现状，CPU）
        from novel_entity_extractor import _load_extract_model
        pipe = _load_extract_model()
        if pipe is None:
            return None
        model, tok = pipe
        inputs = tok(prompt, return_tensors="pt").to(model.device)
        with __import__("torch").no_grad():
            out = model.generate(**inputs, max_new_tokens=1024, do_sample=False)
        resp = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        # 提取三选一（3B 无 think，直接输出判定）
        for t in ENDING_TYPES:
            if t in resp:
                after = resp.split(t, 1)[1]
                reason = after.strip().split("\n")[0].strip(" ：:，,。")[:60]
                return (t, reason or "（无理由）")
        return None
    except Exception as e:
        print(f"[收尾判定] 判定失败（回退特征词）: {e}")
        return None


def _model_ending_verdict(content: str, planned_type: str) -> dict | None:
    """模型层收尾判定：1.5B 判实际类型 → 与规划类型对比。

    返回 verdict dict（直接作为 verify_ending 结果返回），或 None（模型不可用 → 走原规则）。
    判据：类型一致 → PASS；不一致 → FAIL（模型说实际是 X，规划是 Y）。
    """
    actual = _classify_ending_llm(content)
    if actual is None:
        # 模型不可用 → 特征词回退
        actual = _classify_ending_rules(content)
        source = "特征词"
    else:
        source = "模型"
    if actual is None:
        return None  # 特征词也无法判定 → 原规则细查

    match = (actual == planned_type)
    if match:
        return {"pass": True, "ending_type": planned_type,
                "summary": f"{source} 判定「{actual}」与规划一致",
                "details": [{"name": "收尾类型匹配", "pass": True,
                             "reason": f"{source} 判定实际为{actual}，与规划一致", "required": True}]}
    return {"pass": False, "ending_type": planned_type,
            "summary": f"{source} 判定实际为「{actual}」，与规划的「{planned_type}」不一致",
            "details": [{"name": "收尾类型匹配", "pass": False,
                         "reason": f"{source} 判定实际为{actual}，规划要求{planned_type}", "required": True}]}


def _locate_ending_sub(data: dict) -> tuple | None:
    """从 novel_state.json 定位 marked is_ending 的子结构"""
    chapters = data.get("chapters", [])
    if not chapters:
        return None
    last_ch = chapters[-1]
    subs = last_ch.get("sub_structures", {})
    for sk in sorted(subs.keys()):
        sv = subs[sk]
        if sv.get("is_ending"):
            return (last_ch["id"], sk, sv, last_ch)
    # 降级：末章最后一个子结构
    if subs:
        ending_key = sorted(subs.keys())[-1]
        return (last_ch["id"], ending_key, subs[ending_key], last_ch)
    return None


def _read_sub_content(project_dir: str, ch_id: str, sub_key: str) -> str:
    """读取末子结构内容（跳过标题行和编号标记行）"""
    sub_path = os.path.join(project_dir, "chapters", ch_id, f"{sub_key}.txt")
    if not os.path.exists(sub_path):
        return ""
    with open(sub_path, "r", encoding="utf-8") as f:
        raw = f.read().split("\n")
    body = []
    for ln in raw:
        s = ln.strip()
        if not s:
            continue
        if re.match(rf'{ch_id}S\d+', s, re.IGNORECASE):
            continue
        if re.match(r'L\d+ · S\d+《', s):
            continue
        body.append(s)
    return "\n".join(body)


def verify_ending(project_dir: str) -> dict:
    """
    验证结尾子结构的收尾质量。
    仅读取末子结构内容 + 项目配置，不通读全文。
    返回: {"pass": bool, "ending_type": str, "summary": str, "details": list}
    """
    state_path = os.path.join(project_dir, "data", "novel_state.json")
    if not os.path.exists(state_path):
        return {"pass": False, "ending_type": "未指定",
                "summary": "novel_state.json 不存在", "details": []}
    with open(state_path, "r", encoding="utf-8") as f:
        data = json.loads(f.read())

    # 1. 定位
    loc = _locate_ending_sub(data)
    if not loc:
        return {"pass": False, "ending_type": "未指定",
                "summary": "末章无子结构", "details": []}
    ch_id, sk, sv, last_ch = loc

    ending_type = sv.get("ending_type", "未指定")

    # 2. 读取内容
    content = _read_sub_content(project_dir, ch_id, sk)
    if not content:
        return {"pass": False, "ending_type": ending_type,
                "summary": f"{ch_id}{sk} 内容为空或未完成", "details": []}

    # 3. 提取项目配置（降级保障）
    core_conflict = data.get("core_conflict", data.get("project", ""))
    protagonist = data.get("protagonist", "")
    theme = data.get("theme", "")

    # 3.5 模型层收尾类型判定（1.5B 判类型 → 与规划对比；回退特征词）
    # 主判定：模型判出的类型 == 规划类型 → PASS；不一致 → FAIL（给理由）
    model_result = _model_ending_verdict(content, ending_type)
    if model_result is not None:
        return model_result

    # 4. 按类型执行检查
    checks = []
    if ending_type == "封闭式":
        checks = _check_closure(content, core_conflict, protagonist, theme)
    elif ending_type == "开放式":
        checks = _check_open(content, core_conflict, protagonist, theme)
    elif ending_type == "悬停式":
        body = _read_sub_content(project_dir, ch_id, sk).split("\n") if content else []
        checks = _check_hover(content, core_conflict, protagonist, theme, body)
    else:
        checks = [{"name": "收尾类型检查", "pass": False,
                    "reason": f"未知收尾类型: {ending_type}",
                    "required": True}]

    # 5. 判定
    required = [c for c in checks if c.get("required", True)]
    optional = [c for c in checks if not c.get("required", True)]
    pass_required = all(c["pass"] for c in required)
    pass_optional = (sum(1 for c in optional if c["pass"]) >= max(1, len(optional) / 2)
                     ) if optional else True
    overall_pass = pass_required and pass_optional

    result = {
        "pass": overall_pass,
        "ending_type": ending_type,
        "summary": f"{sum(1 for c in checks if c['pass'])}/{len(checks)} 项通过",
        "details": checks
    }

    # 6. 写报告 + stdout 输出
    _write_ending_report(project_dir, result, ch_id, sk)
    return result


def _check_closure(content: str, core_conflict: str, protagonist: str, theme: str) -> list:
    """封闭式结尾 4 项检查"""
    last_200 = content[-200:] if len(content) > 200 else content
    last_sentence = content.split("\n")[-1].strip() if content.strip() else ""
    checks = []
    # 1. 冲突落点
    hit = [kw for kw in KEY_CLOSURE_RESOLVE if kw in content]
    checks.append({"name": "1. 冲突落点", "pass": bool(hit),
                    "reason": f"命中解决词: {hit}" if hit else "未命中解决词",
                    "required": True})
    # 2. 主角变化
    has_change = True
    if protagonist:
        change_markers = ["不再是", "再也不是", "不再", "已经", "终于", "明白了", "意识到"]
        has_change = any(m in content for m in change_markers)
    checks.append({"name": "2. 主角变化", "pass": has_change,
                    "reason": "命中变化标记" if has_change else "未检测到明确变化（可能数据不足）",
                    "required": True})
    # 3. 主题回扣
    has_theme = True
    if theme:
        has_theme = theme in last_200
    checks.append({"name": "3. 主题回扣", "pass": has_theme,
                    "reason": f"主题词「{theme}」末段匹配" if has_theme else f"主题词「{theme}」末段未出现",
                    "required": True})
    # 4. 末句动作
    is_action = len(last_sentence) >= 2 and not last_sentence.rstrip("。").endswith("了")
    checks.append({"name": "4. 末句动作", "pass": is_action and len(last_sentence) >= 2,
                    "reason": f"末句: 「{last_sentence[:30]}」" if last_sentence else "无末句",
                    "required": True})
    return checks


def _check_open(content: str, core_conflict: str, protagonist: str, theme: str) -> list:
    """开放式结尾 4 项（2硬+2软）"""
    last_200 = content[-200:] if len(content) > 200 else content
    checks = []
    # 1. [硬] 冲突结果
    hit = [kw for kw in KEY_OPEN_OUTCOME if kw in content]
    checks.append({"name": "1. [硬] 冲突结果", "pass": bool(hit),
                    "reason": f"命中决策词: {hit}" if hit else "未命中决策词",
                    "required": True})
    # 2. [软] 留白意图
    hint = [kw for kw in KEY_OPEN_HINT if kw in last_200]
    checks.append({"name": "2. [软] 留白意图", "pass": bool(hint),
                    "reason": f"末段留白暗示: {hint}" if hint else "末段无留白标记",
                    "required": False})
    # 3. [软] 情绪收束
    tone_markers = ["平静", "安宁", "释然", "沉默", "寂静", "等待", "看着", "望着"]
    hit_tone = [m for m in tone_markers if m in last_200]
    checks.append({"name": "3. [软] 情绪收束", "pass": bool(hit_tone),
                    "reason": f"末段情绪收敛: {hit_tone}" if hit_tone else "末段无明显情绪标记",
                    "required": False})
    # 4. [硬] 禁逃
    forbidden = [kw for kw in KEY_FORBIDDEN if kw in content]
    checks.append({"name": "4. [硬] 禁逃", "pass": not bool(forbidden),
                    "reason": f"命中禁逃词: {forbidden}" if forbidden else "无禁逃词",
                    "required": True})
    return checks


def _check_hover(content: str, core_conflict: str, protagonist: str,
                 theme: str, body: list) -> list:
    """悬停式结尾 6 项（全硬）"""
    last_200 = content[-200:] if len(content) > 200 else content
    sentences = [l for l in body if l.strip()]
    checks = []
    # 1. 悬念存在
    hit = [kw for kw in KEY_SUSPENSE if kw in content]
    checks.append({"name": "1. 悬念存在", "pass": bool(hit),
                    "reason": f"命中悬念词: {hit}" if hit else "未命中悬念词",
                    "required": True})
    # 2. 位置合理
    checks.append({"name": "2. 位置合理", "pass": len(content) >= 200,
                    "reason": f"内容长度 {len(content)}字" if len(content) >= 200 else f"内容过短（{len(content)}字），需≥200字",
                    "required": True})
    # 3. 主角成长
    has_growth = True
    if protagonist:
        growth_markers = ["明白了", "意识到", "发现", "不再是", "第一次", "从未"]
        has_growth = any(m in content for m in growth_markers)
    checks.append({"name": "3. 主角成长", "pass": has_growth,
                    "reason": "命中成长标记" if has_growth else "未检测到主角成长",
                    "required": True})
    # 4. 情绪锚定
    emotion_words = ["焦虑", "恐惧", "期待", "希望", "绝望", "不安", "平静",
                     "兴奋", "紧张", "愤怒", "悲伤", "喜悦", "茫然", "困惑", "坚定"]
    found = [w for w in emotion_words if w in last_200]
    checks.append({"name": "4. 情绪锚定", "pass": bool(found),
                    "reason": f"末段情绪: {found}" if found else "末段无可命名情绪词",
                    "required": True})
    # 5. 节奏检测
    is_compact = True
    if len(sentences) >= 4:
        mid = max(len(sentences) // 2, 2)
        first_half = sentences[:mid]
        last_half = sentences[-mid:]
        avg_first = sum(len(s) for s in first_half) / max(len(first_half), 1)
        avg_last = sum(len(s) for s in last_half) / max(len(last_half), 1)
        is_compact = avg_last >= avg_first * 0.85
    checks.append({"name": "5. 节奏检测", "pass": is_compact,
                    "reason": f"后半句均 {avg_last:.0f}字 vs 前半 {avg_first:.0f}字" if len(sentences) >= 4 else "句子数过少，跳过",
                    "required": True})
    # 6. 禁逃
    forbidden = [kw for kw in KEY_FORBIDDEN if kw in content]
    checks.append({"name": "6. 禁逃", "pass": not bool(forbidden),
                    "reason": f"命中禁逃词: {forbidden}" if forbidden else "无禁逃词",
                    "required": True})
    return checks


def _write_ending_report(project_dir: str, result: dict, ch_id: str, ending_key: str):
    """写入结尾收束验证报告并输出到 stdout"""
    lines = []
    lines.append("# 结尾收束验证报告")
    lines.append("")
    lines.append(f"收尾类型: {result.get('ending_type', '未指定')}")
    lines.append(f"末子结构: {ch_id}{ending_key}")
    lines.append(f"结果: {'[OK] PASS' if result.get('pass') else '[FAIL] BLOCK'}")
    lines.append(f"概要: {result.get('summary', '')}")
    lines.append("")
    lines.append("| 检查项 | 结果 | 说明 |")
    lines.append("|--------|------|------|")
    for d in result.get("details", []):
        icon = "[OK]" if d.get("pass") else "[FAIL]"
        lines.append(f"| {d.get('name', '?')} | {icon} | {d.get('reason', '')} |")
    lines.append("")
    lines.append("---")
    lines.append("*报告由 novel-fidelity verify-ending 生成*")

    report_path = os.path.join(project_dir, "data", "ending_report.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.flush()
        os.fsync(f.fileno())
    print(f"\n[结尾收束报告已写入] {report_path}")

    pass_count = sum(1 for d in result.get("details", []) if d.get("pass"))
    print(f"[结尾收束] {result.get('ending_type')} — {result.get('summary', '')}")
    for d in result.get("details", []):
        icon = "[OK]" if d.get("pass") else "[FAIL]"
        print(f"  {icon} {d.get('name', '')}")


def generate_report(project_dir: str):
    """大纲忠实度报告：从 novel_state.json 读取大纲 vs 实际内容"""
    state_path = os.path.join(project_dir, "data", "novel_state.json")
    if not os.path.exists(state_path):
        print(f"ERROR: novel_state.json 未找到（{state_path}）")
        sys.exit(1)

    # 阶段门禁：需要 ≥ stage3_ready
    _order = {"none": 0, "init": 10, "stage1_done": 20, "writing": 30, "chapter_done": 40, "stage3_ready": 50, "complete": 60}
    with open(state_path, "r", encoding="utf-8") as f:
        _state = json.load(f)
    _p = _order.get(_state.get("current_phase", "none"), 0)
    if _p < 50:
        print(f"ERROR: novel_fidelity 需要阶段 ≥ stage3_ready(50)，当前为 {_state.get('current_phase', 'none')}({_p})")
        print(f"  全文写作未完成，不能生成大纲忠实度报告。")
        sys.exit(1)

    with open(state_path, "r", encoding="utf-8") as f:
        outline = json.load(f)

    chapters_raw = outline.get("chapters", [])
    # 兼容两种 chapters 数据格式：
    #   list[dict]：每个元素有 chapter_number / title / summary / themes
    #   dict[L##]：key 为 "L01".."L15"，每个值有 title / summary / themes
    if isinstance(chapters_raw, dict):
        chapters = []
        for ch_key in sorted(chapters_raw.keys()):
            ch = chapters_raw[ch_key]
            num = int(ch_key.replace("L", ""))
            chapters.append({
                "chapter_number": num,
                "title": ch.get("title", ""),
                "summary": ch.get("summary", ""),
                "themes": ch.get("themes", [])
            })
    else:
        chapters = chapters_raw
    actual = _load_chapters(project_dir)

    report_lines = []
    report_lines.append(f"# 大纲忠实度报告")
    report_lines.append(f"")
    report_lines.append(f"| 章节 | 大纲概述 | 实际摘要 | 偏差等级 |")
    report_lines.append(f"|------|---------|---------|---------|")

    pass_count = 0
    info_count = 0
    warn_count = 0
    error_count = 0

    for ch in chapters:
        ch_num = ch.get("chapter_number", 0)
        ch_title = ch.get("title", "")
        summary = ch.get("summary", "")
        themes = ch.get("themes", [])

        # 查找对应的实际章节目录
        ch_dir_key = None
        for key in actual:
            if f"{ch_num:02d}" in key or ch_title in key:
                ch_dir_key = key
                break

        actual_sample = actual.get(ch_dir_key, "")
        actual_short = actual_sample[:100].replace("\n", " ") if actual_sample else "(未完成)"

        # 简单偏差检测
        level = "PASS"
        detail = "内容一致"

        if not actual_sample:
            level = "ERROR"
            detail = "章节未完成"
            error_count += 1
        else:
            # 检查主题词是否出现在正文中
            theme_hits = sum(1 for t in themes if t in actual_sample)
            if theme_hits == 0 and themes:
                level = "WARN"
                detail = f"大纲主题词[{', '.join(themes)}]未在正文中出现"
                warn_count += 1
            elif theme_hits < len(themes) / 2:
                level = "INFO"
                detail = f"部分主题词未出现（{theme_hits}/{len(themes)}）"
                info_count += 1
            else:
                level = "PASS"
                pass_count += 1

        ch_label = f"Ch{ch_num} {ch_title}"
        report_lines.append(f"| {ch_label} | {summary[:40]}... | {actual_short[:40]}... | {level} |")

    report_lines.append(f"")
    report_lines.append(f"## 统计")
    report_lines.append(f"- [OK] PASS: {pass_count}")
    report_lines.append(f"- ℹ️ INFO: {info_count}")
    report_lines.append(f"- [WARN] WARN: {warn_count}")
    report_lines.append(f"- [FAIL] ERROR: {error_count}")
    report_lines.append(f"")
    report_lines.append(f"---")
    report_lines.append(f"*报告由 novel-fidelity 生成*")

    report_path = os.path.join(project_dir, "data", "fidelity_report.md")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
        f.flush()
        os.fsync(f.fileno())

    # 门禁：大纲忠实度报告完成
    state_path = os.path.join(project_dir, "data", "novel_state.json")
    if os.path.exists(state_path):
        gate_script = os.path.join(SCRIPTS_DIR, "novel_pipeline_gate.py")
        import subprocess as _sp
        _sp.run([sys.executable, gate_script, "pass", state_path, "fidelity"],
                capture_output=True, check=False)
    print(f"OK report={report_path} pass={pass_count} info={info_count} warn={warn_count} error={error_count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  novel_fidelity.py <project_dir>              # 大纲忠实度报告")
        print("  novel_fidelity.py verify-ending <project_dir> # 结尾收束验证")
        sys.exit(1)

    if sys.argv[1] == "verify-ending":
        result = verify_ending(sys.argv[2])
        sys.exit(0 if result.get("pass") else 1)
    else:
        generate_report(sys.argv[1])
