"""novel_bridge.py — structured-writer ↔ novel 子包桥接层

职责：
1. 检测小说模板（novel.mode）
2. 小说线规划：场景配置 → 章数组 → 因果链验证 → 组装标准 outline → 初始化 novel 项目
3. 逐章子结构规划（plan-chapter，writing_prompt 硬校验）
4. 检查编排（章检/全文检，子进程调移植脚本）
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent

# 因果链验证：概述必需因果动词（与 novel-weaver novel_causality_check.py 同源）
CAUSAL_VERBS = ["因为", "所以", "导致", "发现", "决定", "开始", "被迫", "意识到"]
ENDING_TYPES = ("封闭式", "开放式", "悬停式")

# 篇幅 → 每子结构字数目标（三阶段同源，plan-chapter 注入 word_count_target）
LENGTH_TARGETS = {
    "short": (1000, 1500),
    "medium": (1500, 2000),
    "long": (2000, 4000),
}


def is_novel_template(template) -> bool:
    """检测模板是否为小说线（novel.mode 开关）"""
    if not template or not isinstance(template, dict):
        return False
    novel = template.get("novel")
    return isinstance(novel, dict) and bool(novel.get("mode"))


def _run_script(script, args, input_text=None, timeout=900, env_extra=None):
    """子进程调 novel 脚本。强制 CPU（CUDA_VISIBLE_DEVICES=-1），避免与 LM Studio 抢显存。"""
    cmd = [sys.executable, str(SCRIPTS_DIR / script)] + [str(a) for a in args]
    try:
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = "-1"
        env["PYTHONIOENCODING"] = "utf-8"   # 强制子进程 stdout/stderr 用 UTF-8（Windows 默认 GBK，主进程 utf-8 读会 UnicodeDecodeError）
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            cmd, input=input_text, capture_output=True, text=True,
            encoding="utf-8", cwd=str(SCRIPTS_DIR), timeout=timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return None


# 思考泄漏污染特征（推理模型的 reasoning 混入 content 时的标记）
_THINK_POLLUTION = [
    r"```", r"\bWait,", r"\bLet's", r"\bOne more", r"\bOkay,", r"\bproceeding",
    r"\bcarefully", r"\bcheck\b", r"\bmake sure", r"\bWithout the backticks",
    r"\bconstruct the", r"\bI need to",
]


def _has_think_pollution(text: str) -> bool:
    """检测 content 是否混入推理模型的思考文本（reasoning 泄漏）"""
    return any(re.search(p, text) for p in _THINK_POLLUTION)


def _llm_json(messages, llm_client, label, max_tokens=None, retries=3):
    """LLM 输出 → JSON（对齐通用线 plan_outline 的重试机制）。

    与通用线行为一致：解析失败 → 携带错误反馈重试（最多 retries 次），
    而不是一次失败就抛错（这是之前"小说线脆、通用线稳"的结构差异）。

    注意：不做任何"禁止思考"类提示词——推理模型的思考是特性，不禁。
    但要求思考不泄漏进输出：若检测到思考文本混入（reasoning 泄漏），
    在重试反馈中明确指出，让模型重新输出纯 JSON。
    """
    from ..planner import parse_outline

    cont = messages.copy()
    mt = max_tokens or max(4096, llm_client.max_tokens)

    for attempt in range(retries):
        raw = ""
        work = cont.copy()
        for _ in range(4):
            result = llm_client.chat_detailed(work, max_tokens=mt)
            chunk = result.get("content", "") or ""
            raw += chunk
            if result.get("finish_reason") != "length" or not chunk.strip():
                break
            work.append({"role": "assistant", "content": chunk})
            work.append({"role": "user", "content": "JSON 输出被截断，请直接从截断处继续输出 JSON 内容，不要重复，不要任何解释文字。"})
        # 解析层跳过 <think> 块（模型该思考就思考，解析时只看 JSON 部分）
        raw_clean = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
        obj = parse_outline(raw_clean)
        if obj is not None:
            return obj
        if attempt < retries - 1:
            # 错误反馈（对齐通用线 plan_outline 的重试反馈风格）
            if _has_think_pollution(raw):
                hint = "你的输出在 JSON 中间混入了思考过程文本（如 Wait/Let's/代码块标记等），导致 JSON 断裂无法解析。重新输出：只输出完整的 JSON，不要输出任何思考过程或解释文字。"
            else:
                hint = "【格式错误】你的输出包含 JSON 以外的文字，或 JSON 格式不正确。只输出 JSON，以 { 开头，以 } 结尾，不要任何其他文字。重新生成："
            cont.append({"role": "assistant", "content": raw[:800]})
            cont.append({"role": "user", "content": hint})
    raise ValueError(f"[{label}] LLM 连续 {retries} 次无法输出正确格式的 JSON。最后一次输出：\n{raw[:300]}")


# ─────────────────────────────────────────────
# 规划：场景配置 → 章数组 → 因果链 → outline
# ─────────────────────────────────────────────

def generate_scene_config(topic, user_meta, template, llm_client) -> dict:
    """步骤1：场景配置（人物/时代/地点/风土人情/核心冲突）"""
    genre = (user_meta or {}).get("题材", "") or "未指定"
    pov = (user_meta or {}).get("叙事视角", "") or "未指定"
    style = (template or {}).get("style", "")[:600]
    sys_prompt = """你是小说设定规划师。根据主题生成场景配置 JSON。
【输出规则】只输出 JSON，禁止任何其他文字，禁止 markdown 代码块，直接以 { 开头。
【JSON 格式】
{
  "era": "时代背景（如近未来2099/古代架空王朝）",
  "location": "核心地点（如赛博城市下层区）",
  "customs": "风土人情/世界观规则",
  "core_conflict": "核心冲突（一句话，驱动全文）",
  "theme": "主题立意",
  "writing_style": {"narrative_voice": "叙事视角（第一人称/第三人称有限/第三人称全知/第二人称，严格沿用 user 消息中给定的叙事视角）", "tense": "时态", "sentence_preference": "句式偏好", "vocabulary_register": "词汇风格", "description_depth": "描写深度", "custom_rules": "自定义文风规则"},
  "characters": [
    {"name": "角色名", "role": "身份/定位", "mbti": "16型人格", "archetype": "荣格12原型之一", "traits": ["特质1", "特质2"], "alias": "常用别名或空", "motivation": "动机"}
  ]
}
【要求】角色 2-6 个；mbti 必须为 16 型之一(INTJ/INTP/ENTJ/ENTP/INFJ/INFP/ENFJ/ENFP/ISTJ/ISFJ/ESTJ/ESFJ/ISTP/ISFP/ESTP/ESFP)；archetype 必须为荣格12原型之一(Innocent/Sage/Explorer/Outlaw/Magician/Hero/Lover/Jester/Everyperson/Caregiver/Ruler/Creator)。
【叙事视角】writing_style.narrative_voice 必须严格采用 user 消息中给定的叙事视角，不得擅自更改（例如 user 给定"第一人称"，则写"第一人称"，正文叙述必须以"我"展开；给定"第三人称有限"则严格跟随主角视角）。
【完整示例】（照着这个结构填，不要自己改结构）：
{
  "era": "近未来2099年",
  "location": "赛博城市下层区",
  "customs": "机械义体改造普及，黑市交易盛行",
  "core_conflict": "主角发现AI觉醒的秘密，被迫在人类与机器之间选择",
  "theme": "人性与机器的边界",
  "writing_style": {"narrative_voice": "第一人称", "tense": "过去式为主", "sentence_preference": "长短句交错", "vocabulary_register": "文学化", "description_depth": "中等", "custom_rules": ""},
  "characters": [
    {"name": "林铁生", "role": "主角·义体修理工", "mbti": "INTJ", "archetype": "Hero", "traits": ["固执", "重情义"], "alias": "铁生", "motivation": "查清哥哥失踪真相"},
    {"name": "三浦", "role": "导师·研究所主任", "mbti": "ISTJ", "archetype": "Sage", "traits": ["谨慎", "守规则"], "alias": "", "motivation": "维护系统稳定"}
  ]
}"""
    user_msg = f"主题：{topic}\n题材：{genre}\n叙事视角：{pov}\n文风约束：{style}\n请生成场景配置 JSON。"
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    return _llm_json(messages, llm_client, "场景配置")


def generate_chapters(setting, topic, length, llm_client) -> list:
    """步骤2：一级大纲（章数组 L01-L15）。LLM 同时提炼小说标题（短标题）。"""
    length_key = _length_key(length)
    sys_prompt = """你是小说大纲规划师。根据场景配置生成章节大纲。
【输出规则】只输出 JSON，禁止任何其他文字。格式：
{"title": "小说标题", "chapters": [{"id": "L01", "title": "章标题", "overview": "概述", "is_key": false}]}
【title】为整本小说的短标题（≤12字，精炼有记忆点，贴合主题与题材，不抄用户原文）。
【概述要求】≥12 有效字符，必须含因果动词（因为/所以/导致/发现/决定/开始/被迫/意识到），描述"谁做了什么事导致什么"。
【is_key】true = 重点章（转折/高潮/冲突爆发/关键揭秘的章），写作字数可上浮 50%；false = 普通章。每本 2-4 个重点章，标在 overview 因果最强、矛盾最烈的章上。
【章节数】按篇幅：短篇3-6章、中篇8-10章、长篇11-15章。
【因果递进】章与章之间必须环环相扣：L01→L02→... 前一章结果为后一章起因。
【末章】最后一章 overview 末尾标注【收尾类型: 封闭式】或【收尾类型: 开放式】或【收尾类型: 悬停式】三选一。
【完整示例】（照着这个结构填）：
{"title": "赛博搏杀记",
 "chapters": [
  {"id": "L01", "title": "深夜警报", "overview": "义体修理工林铁生在维修AI核心时发现异常脉冲，决定暗中调查", "is_key": false},
  {"id": "L02", "title": "导师的警告", "overview": "调查惊动导师三浦，被迫停止调查但已留下线索", "is_key": true},
  {"id": "L03", "title": "终局", "overview": "真相揭露导致系统崩溃，林铁生选择直面AI本体【收尾类型: 开放式】", "is_key": true}
]}"""
    user_msg = (
        f"主题：{topic}\n篇幅：{length}\n"
        f"场景配置：\n{json.dumps(setting, ensure_ascii=False, indent=2)}\n请生成章节大纲 JSON（含 title 与 chapters）。"
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    obj = _llm_json(messages, llm_client, "章大纲")
    title = str(obj.get("title") or "").strip()
    chapters = obj.get("chapters") or []
    out = []
    for i, c in enumerate(chapters, 1):
        if i > 15:
            break
        out.append({
            "id": str(c.get("id") or f"L{i:02d}"),
            "title": str(c.get("title") or f"第{i}章"),
            "overview": str(c.get("overview") or ""),
            "is_key": bool(c.get("is_key", False)),
        })
    if not out:
        raise ValueError("[章大纲] LLM 未返回任何章节")
    # 标题包装成 (title, chapters)：便于 plan_novel_outline 统一消费
    return (title, out)


def _length_key(length: str) -> str:
    """篇幅字段归一化：短篇/中篇/长篇 → short/medium/long"""
    if not length:
        return "medium"
    if "短" in length:
        return "short"
    if "长" in length:
        return "long"
    return "medium"


def verify_causality(chapters) -> list:
    """步骤3：章级因果链验证（概述≥12字符 + 因果动词）"""
    issues = []
    for c in chapters:
        ov = re.sub(r"[\s,，。！？、；：\"\"''【】《》（）\n\t]", "", c.get("overview", ""))
        if len(ov) < 12:
            issues.append(f"{c['id']} 概述过短（{len(ov)}有效字符 < 12）")
        if not any(v in c.get("overview", "") for v in CAUSAL_VERBS):
            issues.append(f"{c['id']} 概述缺少因果动词（因为/所以/导致/发现/决定/被迫/意识到）")
    return issues


def _normalize_characters(chars) -> list:
    """场景配置角色 → novel_state.characters 规范格式（init 时一次写入，避免二次写）"""
    out = []
    for c in (chars or []):
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        out.append({
            "name": name,
            "role": str(c.get("role", "")),
            "traits": [str(t) for t in (c.get("traits") or [])],
            "mbti": str(c.get("mbti", "")),
            "archetype": str(c.get("archetype", "")),
            "motivation": str(c.get("motivation", "")),
            "aliases": [str(c.get("alias", ""))] if c.get("alias") else [],
            "first_appearance": "L01",
        })
    return out


def restore_novel_state(state_path: str) -> bool:
    """项目状态丢失时从备份恢复（data/novel/backups/<项目名>/novel_state.json）。

    备份目录在 projects 的兄弟级（data/novel/backups/）——projects 被误删时备份不受影响。
    返回是否恢复成功。恢复成功后，正文文件（chapters/*.txt）仍可能缺失——
    由 novel_writer 的续写恢复按文件真相逐段处理（在的跳过，丢的重写）。
    """
    from pathlib import Path
    sp = Path(state_path)
    try:
        project_dir = sp.parent.parent            # .../projects/<name>/
        project_name = project_dir.name
        if not project_name or project_name == "projects":
            return False
        backup_dir = project_dir.parent.parent / "backups" / project_name   # data/novel/backups/<name>/
        bak = backup_dir / "novel_state.json"
        if not bak.is_file():
            return False
        data = json.loads(bak.read_text(encoding="utf-8-sig"))
        sp.parent.mkdir(parents=True, exist_ok=True)
        tmp = sp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(sp)
        return True
    except Exception:
        return False


# meta 字段 → state.writing_style 字段 的配置映射（模板驱动，不是字段特判：
# 模板里 source=auto/user 的 meta 字段，若用户填了值，按此映射落进写作文风状态）
META_TO_WRITING_STYLE = {
    "叙事视角": "narrative_voice",
}


def _apply_meta_to_writing_style(user_meta: dict, meta_fields: list, writing_style: dict) -> dict:
    """按模板 meta 声明的 source 语义，把用户已填值统一应用到 writing_style。

    三模式统一（与通用线 planner 权威定义一致）：
      user：用户填了 → 直接用，LLM 不经手；没填 → 空着
      auto：用户填了 → 直接用；没填 → 由 LLM 生成（此处不动）
      llm ：一律由 LLM 生成（用户填了也不用）
    仅处理「用户已填」的情况——有值就覆盖，无值不动（留给 LLM 生成路径）。
    """
    ws = dict(writing_style or {})
    for f in meta_fields:
        name = f.get("name", "")
        target = META_TO_WRITING_STYLE.get(name)
        if not target:
            continue
        source = f.get("source", "user")
        if source == "llm":
            continue  # llm：一律 LLM 生成，用户填了也不用
        v = str((user_meta or {}).get(name, "") or "").strip()
        if v:
            ws[target] = v  # user/auto 已填 → 直接采用
    return ws


def init_novel_project(project_id, topic, length, chapters, characters=None) -> str:
    """初始化 novel 项目（data/novel/projects/<id>/data/novel_state.json），返回 state_path。

    characters 直接随初始化一次写入（场景配置角色），避免"先创建空骨架再覆盖填角色"的二次写。
    """
    from ._path_utils import DATA_DIR
    state_path = DATA_DIR / project_id / "data" / "novel_state.json"
    if state_path.exists():
        return str(state_path)
    length_key = _length_key(length)
    data = {
        "project": topic,
        "created": __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
        "meta": {"current_phase": "stage1_outline", "version": "1.0", "length": length_key},
        "writing_style": {"narrative_voice": "", "tense": "过去式为主", "sentence_preference": "长短句交错",
                          "vocabulary_register": "文学化", "description_depth": "中等", "custom_rules": ""},
        "characters": _normalize_characters(characters),
        "entity_tracker": {"entities": [], "relations": []},
        "chapters": [
            {"id": c["id"], "title": c["title"], "overview": c["overview"],
             "word_count": 0, "status": "pending", "sub_structures": {},
             **({"is_final": True} if i == len(chapters) else {})}
            for i, c in enumerate(chapters)
        ],
        "timeline": [],
        "signature": {"enabled": False, "text": ""},
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(state_path)


def build_outline(topic, chapters, template, user_meta, state_path, llm_title="") -> dict:
    """步骤5：章数组 → structured-writer 标准 outline（两阶段：只到章级）。

    子结构在写作时逐章规划（novel_writer 章级门控），规划阶段 sub_sections 为空。
    章 word_count = 篇幅档估算（每段目标中间值 × 默认3段），写作同步子结构后由汇总覆盖。
    title 优先级：用户 meta「标题」（合理短标题）> LLM 提炼标题 > 原文截断兜底。
    """
    sections = []
    n = len(chapters)
    length_key = _length_key((user_meta or {}).get("篇幅", ""))
    lo, hi = LENGTH_TARGETS.get(length_key, (1000, 1500))
    chapter_est = ((lo + hi) // 2) * 3  # 每段目标中间值 × 默认 3 段
    for i, ch in enumerate(chapters, 1):
        sections.append({
            "id": f"n{i}",
            "title": ch["title"],
            "subtitle": "",
            "summary": ch["overview"],
            "word_count": chapter_est,
            "is_key": bool(ch.get("is_key", False)),
            "status": "pending",
            "actual_word_count": 0,
            "rag": {"enabled": False, "kb": ""},
            "_checked": True,
            "type": "section",
            "show_label": True,
            "_tmpl_key": "正文",
            "_logical_order": None,
            "_novel": {"chapter": ch["id"], "is_ending": (i == n), "state_path": state_path},
            "sub_sections": [],
        })
    meta_out = {}
    meta_fields = (template or {}).get("meta", [])
    for f in meta_fields:
        name = f.get("name", "")
        if not name:
            continue
        source = f.get("source", "user")
        v = (user_meta or {}).get(name, "")
        # 三模式统一（与通用线 planner 同语义）：
        #   user：用户填了直接抄入，LLM 不经手；没填就空着（不补默认值）
        #   auto：用户填了直接抄入；没填由 LLM 生成（留空走后续 LLM 环节）
        #   llm ：一律由 LLM 生成（用户填了也不用——LLM 润色/提炼），此处留空
        if source == "llm":
            v = ""
        elif source == "auto" and not v:
            v = ""
        meta_out[name] = v
    # 标题（模板 meta 声明的 auto 字段）：统一走 source 语义——
    #   auto：用户填了 → 直接用；没填 → 用 LLM 提炼的 llm_title；都空 → 兜底。
    # 仅"标题"特殊在它驱动 outline.title（小说线 LLM 提炼在 generate_chapters 产出 llm_title），
    # 其余 meta 字段一律走上方统一循环，无字段级分支。
    user_title = str((user_meta or {}).get("标题", "") or "").strip()
    if user_title:
        final_title = user_title
    elif llm_title:
        final_title = llm_title
    else:
        final_title = "".join(topic.split("\n")[0].split("要求如下", 1)[-1]).strip()[:30] or "未命名小说"
    return {"title": final_title, "meta": meta_out, "sections": sections}


def plan_novel_outline(topic, template, user_meta, llm_client) -> dict:
    """小说线规划主入口：场景配置 → 章数组 → 因果链 → outline + 项目初始化

    返回: {outline, setting, state_path, causality_issues}
    """
    setting = generate_scene_config(topic, user_meta, template, llm_client)
    length = (user_meta or {}).get("篇幅", "") or "中篇"
    llm_title, chapters = generate_chapters(setting, topic, length, llm_client)
    issues = verify_causality(chapters)
    if issues:
        # 因果链不通过：把问题反馈给 LLM 重生成（最多 2 次）
        for attempt in range(2):
            fix_msg = "以下章节概述不满足要求，请修正后重新输出完整章数组 JSON：\n" + "\n".join(f"- {i}" for i in issues)
            messages = [
                {"role": "system", "content": "你是小说大纲规划师。修正上一版大纲的问题。"},
                {"role": "user", "content": fix_msg + f"\n原章数组：{json.dumps(chapters, ensure_ascii=False)}"},
            ]
            llm_title, chapters = generate_chapters(setting, topic, length, llm_client)
            issues = verify_causality(chapters)
            if not issues:
                break
    # 场景配置中的角色随初始化一次写入 characters（避免"先建空骨架再覆盖"的二次写）
    project_id = f"novel_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}"
    state_path = init_novel_project(project_id, topic, length, chapters, characters=setting.get("characters"))
    # 场景配置的 writing_style 落库：LLM 生成的字段有值则采用；
    # 用户已填的 meta 字段（叙事视角等）按 source 语义统一覆盖（模板驱动，见 _apply_meta_to_writing_style）
    from .novel_state_manager import load_state, save_state
    _sd = load_state(state_path)
    _ws = dict(_sd.get("writing_style") or {})
    _gen_ws = (setting.get("writing_style") or {})
    _ws.update({k: v for k, v in _gen_ws.items() if v})          # LLM 生成的文风字段（有值才覆盖）
    _ws = _apply_meta_to_writing_style(user_meta, (template or {}).get("meta", []), _ws)
    _sd["writing_style"] = _ws
    save_state(state_path, _sd, caller="scene-config")
    # 两阶段：规划只到章（子结构在写作时逐章规划 + 用户确认门控，见 novel_writer）
    outline = build_outline(topic, chapters, template, user_meta, state_path, llm_title=llm_title)
    return {"outline": outline, "setting": setting, "state_path": state_path, "causality_issues": issues}


# ─────────────────────────────────────────────
# 子结构规划 + 检查编排
# ─────────────────────────────────────────────

def plan_chapter_subs(state_path, chapter_id, template, llm_client, hints: str = "") -> list:
    """步骤6：子结构先行规划（LLM 生成 S01-S05，注册进 state，writing_prompt ≥50 硬校验）

    hints: 章级重规划时的新要求（注入规划 prompt，其余流程不变）
    返回已注册的 subs 列表
    """
    from .novel_state_manager import load_state
    data = load_state(state_path)
    ch_info = next((c for c in data.get("chapters", []) if c["id"] == chapter_id), None)
    if ch_info is None:
        raise ValueError(f"章节 {chapter_id} 未找到")
    length_key = data.get("meta", {}).get("length", "medium")
    lo, hi = LENGTH_TARGETS.get(length_key, (1000, 1500))
    sys_prompt = """你是小说章节规划师。为指定章节规划子结构（S01-S05）。
【输出规则】只输出 JSON 数组，禁止任何其他文字。格式：
[{{"s_key":"S01","title":"子结构标题","summary":"概述（≥12有效字符，含动作+人物+事件）","tone":"情绪基调（紧张/平静/悬疑/温馨/愤怒/悲伤/恐惧/欢乐等）","emotions":[{{"type":"情绪","intensity":0.0-1.0}}],"word_count":1200,"writing_prompt":"预编写作命题（≥50字符，含场景建立+核心事件+情绪弧的完整剧情指令）","is_key":false}}]
【word_count】该段正文目标字数（数字），必须在 user 消息【本章字数目标】的 lo-hi 区间内，**按内容重要度浮动，同章各段不许相同**：重点段（is_key=true）/高潮/冲突爆发段取区间中上或接近上限；普通段取中下；开头/过渡/收尾段可更短。is_key=true 的段 word_count 应明显大于同章普通段。
【is_key】true = 重点段（本章高潮/冲突爆发/关键转折的段），word_count 给区间中上（重点段目标字数已含上浮，写作按 word_count 执行，无需再放大）；false = 普通段。每章最多 1-2 个重点段。
【要求】每章 3-5 个子结构；每个 summary 必须因果递进；writing_prompt 必须 ≥50 字符且包含具体剧情指令；末章末子结构 summary 末尾标注收尾类型。
【新角色声明】若子结构引入了【有名字的具体角色】（如"王医生""老陈"），必须在 summary 中用【新角色: 名字】标注（系统将自动登记）；泛化职业称呼（医生/护士/警察/司机/路人等）和一次性出场人物**不需要**标注；场景配置已有的角色（见 user 消息中场景配置的 characters）无需标注。
【完整示例】（照着这个结构填，writing_prompt 必须写足剧情）：
[
  {"s_key": "S01", "title": "实验室初试", "summary": "林铁生首次接触AI核心，紧张中触发异常警报", "tone": "紧张",
   "emotions": [{"type": "紧张", "intensity": 0.7}, {"type": "好奇", "intensity": 0.5}],
   "word_count": 1450,
   "writing_prompt": "实验室灯光惨白，林铁生站在操作台前，义肢测试臂正在做循环动作。他按下启动键，测试臂突然加速远超预设参数，警报炸响，导师冲过来拍下急停键，但林铁生注意到导师查看日志时眼神闪过一丝异样。", "is_key": true},
  {"s_key": "S02", "title": "导师的警告", "summary": "导师要求停止调查，林铁生决定暗中继续", "tone": "压抑",
   "emotions": [{"type": "压抑", "intensity": 0.7}],
   "word_count": 1100,
   "writing_prompt": "导师把林铁生叫到办公室，语气严肃警告不要多管闲事。林铁生注意到导师眼神回避、手指轻敲桌面，他决定暗中调查。", "is_key": false}
]"""
    # 前文状态摘要（防漂移）：已注册章节的子结构 + 角色表 + 行为轨迹
    # 规模受控（一行一条摘要，不是全量前文），随篇幅增长不线性膨胀
    prev_plan_lines = []
    for pc in data.get("chapters", []):
        if pc["id"] >= chapter_id:
            break
        subs_state = pc.get("sub_structures", {}) or {}
        if not subs_state:
            continue
        prev_plan_lines.append(f"{pc['id']}《{pc.get('title','')}》：")
        for sk in sorted(subs_state.keys()):
            sv = subs_state[sk]
            prev_plan_lines.append(f"  {sk} {sv.get('title','')} — {sv.get('summary','')[:40]}（{sv.get('tone','')}）")
    prev_plan = "\n".join(prev_plan_lines) if prev_plan_lines else "（无前文规划，本章为第一章）"
    char_lines = [
        f"{c.get('name','')}[{c.get('role','')}|{c.get('mbti','')}|{c.get('archetype','')}]"
        for c in data.get("characters", [])
    ]
    chars = "、".join(char_lines) if char_lines else "（暂无角色）"
    user_msg = (
        f"章节：{chapter_id}《{ch_info.get('title','')}》\n"
        f"章概述：{ch_info.get('overview','')}\n"
        + (f"【新要求】（章级重规划指令，规划须体现）\n{hints}\n" if hints else "")
        + f"【本章字数目标】每个子结构约 {lo}-{hi} 字（篇幅档位：短篇1000-1500 / 中篇1500-2000 / 长篇2000-4000）。子结构的数量与单段内容容量须与该档位匹配——篇幅越长，每段内容越充实；篇幅越短，每段越精炼。\n"
        f"【前文已规划章节】（规划当前章时须与之衔接，伏笔/情绪/角色状态连贯）\n{prev_plan}\n\n"
        f"【角色表】\n{chars}\n\n"
        f"场景配置：{json.dumps(data.get('setting', {}), ensure_ascii=False)}\n"
        f"文风：{json.dumps(data.get('writing_style', {}), ensure_ascii=False)}\n"
        f"请生成该章子结构 JSON 数组，与前文规划保持因果与情绪连贯。"
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    subs = _llm_json(messages, llm_client, f"子结构规划 {chapter_id}")
    if not isinstance(subs, list):
        raise ValueError(f"[子结构规划 {chapter_id}] LLM 未返回数组")
    # writing_prompt 硬校验：不足 50 字符拒绝
    for s in subs:
        wp = str(s.get("writing_prompt", "")).strip()
        if len(wp) < 50:
            raise ValueError(f"[子结构规划 {chapter_id}] {s.get('s_key','?')} writing_prompt 不足 50 字符，拒绝注册")
    # 注册（plan-chapter 含新角色检测 + 门禁）
    r = _run_script("novel_workflow_engine.py", ["plan-chapter", state_path, chapter_id, json.dumps(subs, ensure_ascii=False)])
    if r is None:
        raise RuntimeError(f"[plan-chapter {chapter_id}] 子进程超时")
    if r.returncode != 0:
        raise RuntimeError(f"[plan-chapter {chapter_id}] 注册失败: {r.stdout[-800:]} {r.stderr[-400:]}")
    return subs


def build_writing_context(state_path, chapter_id, sub_key) -> str:
    """写作上下文：novel_context_loader 输出（角色/人格/实体/时间线/情绪/命题框）。

    Web 场景注意：load_context 是 novel-weaver 的 CLI 脚本，内部有 sys.exit(1) 串行阻断
    （SystemExit 不被 except Exception 捕获，会杀死整个 Web 服务进程）——
    必须捕获 SystemExit 降级。Web 场景串行由 novel_writer 状态机保证，CLI 阻断是遗留。
    """
    from .novel_context_loader import _load_context_captured
    try:
        return _load_context_captured(state_path, chapter_id, sub_key) or ""
    except SystemExit:
        return ""  # CLI 串行阻断 → Web 降级为空上下文（不阻断写作）
    except Exception as e:
        return f"（上下文加载降级: {e}）"


def replan_novel_sub(state_path, chapter_id, s_key, hints, llm_client) -> dict:
    """段级重规划：单个子结构重新生成（保留 s_key/word_count/status/word_count_target，
    重做 title/summary/tone/emotions/writing_prompt）。

    与通用线 replan_section 的区别：保留小说字段（tone/emotions/writing_prompt≥50），
    更新 novel_state.json 的 sub_structures[s_key]（caller=replan-novel-sub 跳过指纹并刷新）。
    返回新子结构（供 session outline 同步）。
    """
    from .novel_state_manager import load_state, save_state
    data = load_state(state_path)
    ch_info = next((c for c in data.get("chapters", []) if c["id"] == chapter_id), None)
    if ch_info is None:
        raise ValueError(f"章节 {chapter_id} 未找到")
    subs_state = ch_info.get("sub_structures", {}) or {}
    old = subs_state.get(s_key)
    if old is None:
        raise ValueError(f"子结构 {s_key} 未找到")
    length_key = data.get("meta", {}).get("length", "medium")
    lo, hi = LENGTH_TARGETS.get(length_key, (1000, 1500))
    sys_prompt = """你是小说子结构重规划师。根据新要求重新规划【单个子结构】，其余子结构不变。
【输出规则】只输出 JSON 对象（不是数组），禁止任何其他文字。格式：
{"title":"子结构标题","summary":"概述（≥12有效字符，含动作+人物+事件）","tone":"情绪基调","emotions":[{"type":"情绪","intensity":0.0-1.0}],"writing_prompt":"预编写作命题（≥50字符，含场景建立+核心事件+情绪弧的完整剧情指令）"}
【新角色声明】若引入有名字的具体角色（如"王医生""老陈"），必须在 summary 中用【新角色: 名字】标注（系统将自动登记）；泛化职业（医生/护士/警察等）不需要标注。"""
    prev_lines = []
    for pc in data.get("chapters", []):
        if pc["id"] >= chapter_id:
            break
        psubs = pc.get("sub_structures", {}) or {}
        if not psubs:
            continue
        prev_lines.append(f"{pc['id']}《{pc.get('title','')}》：")
        for sk in sorted(psubs.keys()):
            sv = psubs[sk]
            prev_lines.append(f"  {sk} {sv.get('title','')} — {sv.get('summary','')[:40]}（{sv.get('tone','')}）")
    prev_plan = "\n".join(prev_lines) if prev_lines else "（无前文规划）"
    char_lines = [
        f"{c.get('name','')}[{c.get('role','')}|{c.get('mbti','')}|{c.get('archetype','')}]"
        for c in data.get("characters", [])
    ]
    chars = "、".join(char_lines) if char_lines else "（暂无角色）"
    user_msg = (
        f"章节：{chapter_id}《{ch_info.get('title','')}》\n"
        f"要重规划的子结构：{s_key}《{old.get('title','')}》\n"
        f"原概述：{old.get('summary','')}\n"
        f"新要求：{hints or '（无，按原方向优化）'}\n"
        f"【字数目标】该子结构约 {lo}-{hi} 字（篇幅档位约束，内容容量须匹配）\n"
        f"【前文已规划章节】\n{prev_plan}\n\n"
        f"【角色表】\n{chars}\n\n"
        f"场景配置：{json.dumps(data.get('setting', {}), ensure_ascii=False)}\n"
        f"文风：{json.dumps(data.get('writing_style', {}), ensure_ascii=False)}\n"
        f"请重新生成该子结构 JSON 对象。"
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    obj = _llm_json(messages, llm_client, f"重规划 {chapter_id}{s_key}")
    if not isinstance(obj, dict):
        raise ValueError(f"[重规划 {chapter_id}{s_key}] LLM 未返回对象")
    wp = str(obj.get("writing_prompt", "")).strip()
    if len(wp) < 50:
        raise ValueError(f"[重规划 {chapter_id}{s_key}] writing_prompt 不足 50 字符，拒绝更新")
    # 更新 state（保留 word_count/status/word_count_target，重做规划字段）
    new_entry = {
        "title": str(obj.get("title", old.get("title", ""))),
        "summary": str(obj.get("summary", old.get("summary", ""))),
        "tone": str(obj.get("tone", old.get("tone", ""))),
        "word_count_target": old.get("word_count_target") or {"min": lo, "max": hi, "check_max": int(hi * 1.15)},
        "word_count": old.get("word_count", 0),
        "status": old.get("status", "pending"),
        "writing_prompt": wp,
    }
    if isinstance(obj.get("emotions"), list) and obj["emotions"]:
        new_entry["emotions"] = obj["emotions"]
    subs_state[s_key] = new_entry
    ch_info["sub_structures"] = subs_state
    save_state(state_path, data, caller="replan-novel-sub")
    return new_entry


def write_novel_sub(state_path, chapter_id, sub_key, body) -> dict:
    """写入子结构正文（write-sub：原子写入+别名拦截+实体提取+字数校验）。

    注意：跳过 write-sub 内部的自动 finalize-chapter（NOVEL_SKIP_AUTOFINALIZE=1）——
    Web 场景章检由 novel_writer 统一控制（受配置开关），避免写最后一段时隐式加载
    bge/R1 跑六检卡死线程。
    """
    r = _run_script("novel_workflow_engine.py", ["write-sub", state_path, chapter_id, sub_key],
                    input_text=body, env_extra={"NOVEL_SKIP_AUTOFINALIZE": "1"})
    if r is None:
        return {"ok": False, "error": "write-sub 子进程超时"}
    return {"ok": r.returncode == 0, "output": r.stdout.strip()[-2000:], "error": r.stderr.strip()[-500:] if r.returncode else ""}


def finalize_novel_chapter(state_path, chapter_id, checks=None) -> dict:
    """章检六检（规则4检 + bge语义 + R1推理），HARD 阻断返回 issues

    checks: {"chapter": bool, "semantic": bool, "reason": bool, "full": bool}
    """
    if checks and not checks.get("chapter", True):
        return {"ok": True, "skipped": True, "issues": []}
    env_extra = {}
    if checks and not checks.get("semantic", True):
        env_extra["NOVEL_SKIP_SEMANTIC"] = "1"
    if checks and not checks.get("reason", True):
        env_extra["NOVEL_SKIP_REASON"] = "1"
    r = _run_script("novel_workflow_engine.py", ["finalize-chapter", state_path, chapter_id], timeout=1800, env_extra=env_extra)
    if r is None:
        return {"ok": False, "timeout": True, "issues": []}
    stdout = r.stdout or ""
    issues = [ln for ln in stdout.split("\n") if "[HARD]" in ln or "[FAIL]" in ln]
    return {"ok": r.returncode == 0, "issues": issues, "output": stdout[-3000:]}


def finalize_novel_full(state_path, checks=None) -> dict:
    """全文三检（fidelity 忠实度 + 结尾收束 + 完结）"""
    if checks and not checks.get("full", True):
        return {"ok": True, "skipped": True, "issues": []}
    r = _run_script("novel_workflow_engine.py", ["finalize-novel", state_path], timeout=1800)
    if r is None:
        return {"ok": False, "timeout": True, "issues": []}
    stdout = r.stdout or ""
    issues = [ln for ln in stdout.split("\n") if "[HARD]" in ln or "[FAIL]" in ln]
    return {"ok": r.returncode == 0, "issues": issues, "output": stdout[-3000:]}


def novel_project_state(state_path) -> dict:
    """novel_state.json 摘要（供结果页/会话展示）"""
    from .novel_state_manager import load_state
    try:
        data = load_state(state_path)
    except Exception:
        return {}
    return {
        "characters": [{"name": c.get("name"), "role": c.get("role"), "mbti": c.get("mbti"),
                        "archetype": c.get("archetype")} for c in data.get("characters", [])],
        "chapters": [{"id": c["id"], "title": c["title"], "status": c.get("status"),
                      "subs": len(c.get("sub_structures", {}))} for c in data.get("chapters", [])],
        "length": data.get("meta", {}).get("length", ""),
        "phase": data.get("meta", {}).get("current_phase", ""),
    }
