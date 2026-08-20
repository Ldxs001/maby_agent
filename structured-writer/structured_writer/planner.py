"""大纲规划器 — 调用 LLM 生成结构化文章大纲"""
import json
import re
import copy
from typing import Optional
from .llm_client import LLMClient, LLMClientError

KEY_UPSCALE = 1.5

SUB_WORDS_MIN = 200
SUB_WORDS_MAX = 800
SUB_WORDS_DEFAULT = (SUB_WORDS_MIN + SUB_WORDS_MAX) // 2


def _build_planner_prompt(meta: list, content: list, user_meta: dict,
                          plan_hints: str = "") -> str:
    """
    根据 meta+content 结构和用户已填信息动态构建 planner system prompt。

    meta 字段 → 进入 meta{} 对象（短数据，不拆子结构）
    content 字段 → 进入 sections[] 数组（长文本，leaf/section）
    plan_hints → 用户对规划的额外要求（章节数、子结构数、字数等）
    """
    parts = [
        "你是结构化写作规划助手。严格执行以下命令：",
        "",
        "【输出规则】",
        "- 只输出 JSON，禁止任何其他文字、解释、礼貌用语。",
        "- 禁止 markdown 代码块标记（不要 ```json）。",
        "- 直接以 { 开头，以 } 结尾。",
        "",
        "【优先级规则】",
        "- 用户明确指定的结构要求（章节数、子结构数、字数等）优先于默认值",
        f"- 2-4 个子结构、{SUB_WORDS_MIN}-{SUB_WORDS_MAX} 字/子结构 这些只是默认值，用户说了就不遵守",
        "- 每个内容树字段的 desc 字段中可能包含字数要求（如200-300字），以此为准设置 word_count",
        "",
        "【层级边界规则】",
        "- 内容树只支持 2 级结构：## 章节(section) 和 ### 子节(sub_section)",
        "- ####/##### 及更深的层次不作为独立结构条目",
        "- 深层次内容（####+）直接在 ### 子节的正文中作为 Markdown 标题输出",
        "",
        "【数据分类】",
        "- 元数据（短数据：标题、作者、关键词等）→ 放入 meta 对象",
        "- 内容树（长文本：摘要、引言、正文、结论等）→ 放入 sections 数组",
        "",
    ]

    # 用户规划要求
    if plan_hints:
        parts.append("【用户对本次规划的明确要求】")
        parts.append(plan_hints)
        parts.append("——以上要求优先于所有默认值，必须严格遵守。")
        parts.append("")

    # meta 字段
    if meta:
        parts.append(f"【元数据字段（共 {len(meta)} 个）】")
        parts.append(json.dumps(meta, ensure_ascii=False, indent=2))
        parts.append("")
        # 分类展示
        user_filled = [f for f in meta if f.get("source") == "user"]
        auto_filled = [f for f in meta if f.get("source") == "auto" and user_meta.get(f["name"])]
        llm_fields = [f for f in meta if f.get("source") == "llm"]
        auto_empty = [f for f in meta if f.get("source") == "auto" and not user_meta.get(f["name"])]
        if user_filled:
            parts.append("source=user 字段（用户填写，直接抄入 meta，不要修改）：" + json.dumps([f["name"] for f in user_filled], ensure_ascii=False))
        if auto_filled:
            parts.append("source=auto 已填（用户提供了值，直接抄入 meta）：" + json.dumps([f["name"] for f in auto_filled], ensure_ascii=False))
        if auto_empty:
            parts.append("source=auto 未填（用户未提供，由你生成）：" + json.dumps([f["name"] for f in auto_empty], ensure_ascii=False))
        if llm_fields:
            parts.append("source=llm 元数据（必须由你生成）：" + json.dumps([f["name"] for f in llm_fields], ensure_ascii=False))
        parts.append("")
        if user_meta:
            parts.append("用户已提供的值：")
            parts.append(json.dumps(user_meta, ensure_ascii=False, indent=2))
            parts.append("")

    # content 字段
    if content:
        parts.append(f"【内容树字段（共 {len(content)} 个）】")
        parts.append(json.dumps(content, ensure_ascii=False, indent=2))
        parts.append("")
        parts.append("类型规则：")
        parts.append('- type="leaf"：无子结构 sub_sections=[]，直接写全部内容')
        parts.append('- type="section"：默认拆 2-4 个子结构，用户明确指定数量时按用户要求')
        parts.append(f'- 每子结构默认 {SUB_WORDS_MIN}-{SUB_WORDS_MAX} 字，用户指定则按用户要求')
        parts.append("")
        parts.append(f"- is_key: true = 该节为重点节，写作字数可上浮 {int((KEY_UPSCALE - 1) * 100)}%；false = 普通节")

        parts.append("【硬性要求】所有内容树字段**必须全部**在 sections 数组中输出，一条对应一个 sections 元素。")
        parts.append(f"内容树字段清单（共 {len(content)} 个，不准少）：{', '.join(cf['name'] for cf in content)}")
        parts.append("少输出任何一条，系统解析失败，文章将缺失该章节。")
        parts.append("")

    parts.extend([
        "【JSON 格式】",
        '{',
        '  "title": "标题值",',
        '  "meta": {"作者": "（待填写）", "文号": "〔2026〕12号"},',
        '  "sections": [',
        '    {"title": "关键词", "sub_sections": [], "type": "leaf", "is_key": false},',
        '    {"title": "摘要", "sub_sections": [], "type": "leaf", "is_key": false},',
        '    {"title": "引言", "sub_sections": [{"title":"子1","summary":"要点","word_count":500}], "type": "section", "is_key": true},',
        '  ]',
        '}',
        "",
        "【后果】如果输出包含 JSON 以外的任何文字，系统将无法解析，整个流程会失败。",
    ])

    return "\n".join(parts)


def parse_outline(text: str) -> Optional[dict]:
    """尝试从 LLM 输出中解析大纲 JSON"""
    text = text.strip()

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start) if "```" in text[start:] else len(text)
        try:
            return json.loads(text[start:end].strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # 尝试提取 ``` ... ``` 代码块（无 json 标记）
    if "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start) if "```" in text[start:] else len(text)
        content = text[start:end].strip()
        if content.startswith("json\n"):
            content = content[5:]
        try:
            return json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass

    # 尝试找到第一个 { 或 [ 提取 JSON
    for ch, quote in [("{", "}"), ("[", "]")]:
        pos = text.find(ch)
        if pos >= 0:
            candidate = text[pos:]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                lines = candidate.split("\n")
                for cut in range(len(lines), 0, -1):
                    try:
                        return json.loads("\n".join(lines[:cut]))
                    except json.JSONDecodeError:
                        continue
            break

    return None


def _strip_word_desc(desc: str) -> str:
    """清洗模板 desc 中的独立字数描述（如"约200-300字"、"300字左右"）。

    字数已由规划器解析进大纲 word_count、由写作提示「字数要求」行确定，
    desc 注入【当前章节要求】时不应再携带字数，避免与用户在大纲中改过的
    字数（如 50）冲突（desc 原文"200-300字"会误导 LLM）。

    只删"独立成段的字数短语"（数字+字，前面是开头/分隔符/空白），
    不删嵌在语义中的（如"每个小标题不少于50字"——"50字"前是"于"，
    删了会导致语义残废）。与 _parse_word_count 匹配形态一致（数字[-数字]字）。
    """
    if not desc:
        return desc
    d = re.sub(r'(^|[，,。；;、\s（(：:])(?:约|大概)?\s*\d+\s*[-~至到]\s*\d+\s*字\s*(?:左右|上下)?', r'\1 ', desc)
    d = re.sub(r'(^|[，,。；;、\s（(：:])(?:约|大概)?\s*\d+\s*字\s*(?:左右|上下)?', r'\1 ', d)
    d = re.sub(r'\s+', ' ', d)
    return d.strip(' ，,。；;、的')


def _parse_word_count(cf: dict) -> int:
    """从模板 content 字段解析字数要求：
    desc 含 "200-300字" → 取中值；含 "300字" → 取该值；
    无数字时 leaf=0（字数不限，由 desc 指令约束）、section=800（默认）。
    leaf 拒绝 800 兜底——关键词这类输出列表的节不能被"约800字"诱导成长文。
    """
    _wc = 0 if cf.get("type") == "leaf" else 800
    _desc = cf.get("desc", "")
    _m = re.search(r"(\d+)\s*[-~至到]\s*(\d+)\s*字", _desc)
    if _m:
        return (int(_m.group(1)) + int(_m.group(2))) // 2
    _m = re.search(r"(\d+)\s*字", _desc)
    if _m:
        return int(_m.group(1))
    return _wc


def _normalize_outline(outline: dict, content_fields: list) -> dict:
    """
    规范化大纲：补默认值、填充 meta。
    """
    # 确保 title 存在
    if not outline.get("title"):
        meta = outline.get("meta", {})
        # 从 meta 或 content 字段中找标题
        outline["title"] = meta.get("标题", meta.get("文章标题", "未命名文章"))

    # 确保 sections 存在
    sections = outline.get("sections", [])
    if not sections:
        sections = []
    # 补充缺失的 content 字段（LLM 可能跳过某些字段）
    existing_titles = {s.get("title", "") for s in sections}
    for cf in content_fields:
        if cf["name"] not in existing_titles:
            sections.append({
                "id": f"s{len(sections)+1}",
                "title": cf["name"],
                "subtitle": "",
                "summary": cf.get("desc", ""),
                "word_count": _parse_word_count(cf),
                "is_key": False,
                "status": "pending",
                "actual_word_count": 0,
                "rag": {"enabled": False, "kb": ""},
                "_checked": True,
                "type": cf.get("type", "section"),
                "show_label": cf["show_label"],
                "_tmpl_key": cf["name"],
                "sub_sections": []
            })
    outline["sections"] = sections

    for i, s in enumerate(sections):
        if "id" not in s:
            s["id"] = f"s{i+1}"
        s.setdefault("subtitle", "")
        s.setdefault("summary", "")
        s.setdefault("word_count", SUB_WORDS_MAX)
        s.setdefault("is_key", False)
        s.setdefault("status", "pending")
        s.setdefault("actual_word_count", 0)
        s.setdefault("rag", {"enabled": False, "kb": ""})
        s.setdefault("_checked", True)
        s.setdefault("type", "section")

        # ── 逻辑顺序（从模板 content[].logical_order 读取，不设/0 表示按 content[] 顺序） ──
        stitle = s.get("title", "")
        matched = [cf for cf in content_fields if cf.get("name") == stitle]
        lo = matched[0].get("logical_order") if matched else None
        s["_logical_order"] = lo if lo is not None else None

        # ── 血缘标记：_tmpl_key = 模板 content 字段名（标题可改，绑定不断） ──
        # 匹配到模板字段 → 烙该字段名；未匹配（LLM 新增节）→ 自指（desc 查表 miss=空，行为不变）
        s["_tmpl_key"] = matched[0]["name"] if matched else s.get("title", "")

        # 从模板 content_fields 补充 show_label
        if matched:
            s["show_label"] = matched[0]["show_label"]
            # 引用校验节：字数强制为 0（不走 LLM 写作，由后处理接管）
            if matched[0].get("citation_check"):
                s["word_count"] = 0
            # leaf 节：字数按 desc 解析，拒绝 800 兜底
            # （规划器输出的大纲可能给关键词这类节兜底 800 字，
            #   导致写作提示变成"约800字"，诱导长文输出）
            elif matched[0].get("type") == "leaf":
                s["word_count"] = _parse_word_count(matched[0])

        subs = s.get("sub_sections", [])
        s_type = s.get("type", "section")

        if not subs and s_type == "section":
            subs = [{
                "id": f"{s['id']}_1",
                "title": s.get("subtitle") or s["title"],
                "summary": s.get("summary", ""),
                "word_count": s.get("word_count", SUB_WORDS_MAX),
            }]
            s["sub_sections"] = subs

        for j, ss in enumerate(subs):
            if "id" not in ss:
                ss["id"] = f"{s['id']}_{j+1}"
            ss.setdefault("summary", "")
            ss.setdefault("word_count", max(SUB_WORDS_MIN, s.get("word_count", SUB_WORDS_MAX) // max(len(subs), 1)))
            ss.setdefault("status", "pending")
            ss.setdefault("actual_word_count", 0)
            ss.setdefault("_checked", True)
            ss.setdefault("aux_knowledge", None)

        if not subs:
            s.setdefault("word_count", SUB_WORDS_MAX)
        else:
            s["word_count"] = sum(ss["word_count"] for ss in subs)

    return outline


def plan_outline(topic: str, template: dict = None,
                 user_meta: dict = None, llm_client: LLMClient = None,
                 prompt: str = "", plan_hints: str = "") -> dict:
    """
    生成结构化大纲。

    参数:
        topic: 写作主题
        template: {meta: [...], content: [...], style: "...", logic: "..."}
        user_meta: 用户已填的字段值 {"标题": "xxx", "作者": "（待填写）"}
        llm_client: LLM 客户端
        prompt: 旧兼容参数 — 作为风格提示词覆盖
        plan_hints: 用户对规划的额外要求（章节数、子结构数、字数等）

    返回:
        dict: 大纲 JSON {title, meta, sections}
    """
    if llm_client is None:
        raise ValueError("需要提供 llm_client")

    # ── 小说线分支：模板含 novel.mode → 走 novel 子包规划（场景配置→章数组→因果链）──
    if isinstance(template, dict) and (template.get("novel") or {}).get("mode"):
        from .novel import novel_bridge
        if not user_meta:
            user_meta = {}
        # 不把 topic 硬塞进 user_meta["标题"]——小说模板「标题」source=auto 语义是
        # "由 LLM 提炼短标题"（build_outline 四级优先级处理）。用 topic 塞入会导致
        # 完整需求文本成为标题（"写小说，要求如下\n1. ..."），且绕过模板 auto 配置——
        # 模板怎么配就怎么走，不做线内特判。
        plan = novel_bridge.plan_novel_outline(topic, template, user_meta, llm_client)
        outline = plan["outline"]
        # 会话侧记录 novel 状态（plan_novel_outline 已初始化项目，outline.sections[]._novel 带 state_path）
        outline["_novel"] = {
            "state_path": plan["state_path"],
            "setting": plan["setting"],
            "causality_issues": plan["causality_issues"],
        }
        return outline

    # 旧接口兼容
    if isinstance(template, str) or template is None:
        style = template or prompt or ""
        template = {
            "meta": [{"name": "标题", "show_label": False, "desc": "文章标题", "source": "auto"}],
            "content": [{"name": "正文", "show_label": False, "desc": "文章主体", "type": "section"}],
            "style": style,
            "logic": ""
        }
        if not user_meta:
            user_meta = {"标题": topic} if topic else {}

    if user_meta is None:
        user_meta = {}

    meta_fields = template.get("meta", [])
    content_fields = template.get("content", [])
    style = template.get("style", prompt or "")

    # 构建 system prompt（含 plan_hints）
    system_prompt = _build_planner_prompt(meta_fields, content_fields, user_meta, plan_hints)

    # 构建 user message
    user_msg_lines = [f"主题：{topic}"]
    if style:
        user_msg_lines.append(f"写作风格：{style}")
    user_msg_lines.append("\n请生成文章大纲。")
    user_msg = "\n".join(user_msg_lines)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
    ]

    # 最多重试 3 次（格式错误重试）
    outline = None
    last_raw = ""
    for attempt in range(3):
        # 下限 2048：保证推理模型能完成推理并输出大纲主体；
        # 仍被截断时由续接循环补全。低于 2048 推理吃光 token
        # 输出为空，续接无法挽救（空内容直接断）。
        plan_max_tokens = max(2048, llm_client.max_tokens)
        # 续接循环：检测 finish_reason=length 时追加"继续输出"，
        # 拼装完整 JSON 后再解析（与写作引擎机制一致）
        raw = ""
        cont_messages = messages.copy()
        for _cont in range(4):
            result = llm_client.chat_detailed(cont_messages, max_tokens=plan_max_tokens, temperature=None)
            chunk = result.get("content", "")
            finish_reason = result.get("finish_reason", "stop")
            raw += chunk
            if finish_reason != "length":
                break
            if not chunk.strip():
                break
            cont_messages.append({"role": "assistant", "content": chunk})
            cont_messages.append({
                "role": "user",
                "content": "大纲 JSON 输出被截断，请直接从截断处继续输出 JSON 内容，"
                           "不要重复已输出的内容，不要任何解释文字。"
            })
        last_raw = raw
        outline = parse_outline(raw)

        if outline is not None:
            break

        error_feedback = (
            f"【格式错误】你的输出包含 JSON 以外的文字，或 JSON 格式不正确。\n"
            f"只输出 JSON，以 {{ 开头，以 }} 结尾，不要任何其他文字。\n"
            f"重新生成："
        )
        messages.append({"role": "assistant", "content": raw[:800]})
        messages.append({"role": "user", "content": error_feedback})

    if outline is None:
        raise ValueError(
            f"LLM 连续 3 次无法输出正确格式的大纲。最后一次输出：\n{last_raw[:500]}"
        )

    # 规范化
    outline = _normalize_outline(outline, content_fields)

    # ── user 字段代码级固化：LLM 返回后强制覆盖回用户值（user = 用户填写，LLM 不经手）──
    # prompt 里已要求"直接抄入不要修改"，但执行权在 LLM；这里做硬约束兜底，
    # 防 LLM 篡改/占位（如填"（待填写）"）——与小说线 build_outline 同语义。
    if isinstance(template, dict) and user_meta:
        for f in template.get("meta", []):
            if f.get("source") == "user":
                fname = f.get("name", "")
                if fname and fname in user_meta and user_meta[fname]:
                    outline.setdefault("meta", {})[fname] = user_meta[fname]

    return outline


def replan_section(topic: str, hints: str, llm_client: LLMClient,
                   target: dict, parent_section: dict = None,
                   style: str = "", logic: str = "") -> dict:
    """局部重规划（两级：整章 / 单子结构）。

    跳过整篇 LLM 重规划，只重做目标节点。写作管线原样复用（吃更新后的 outline）。

    参数:
        topic: 文章主题
        hints: 用户对目标节点的调整要求（可为空=按原结构重做）
        llm_client: 规划 LLM 客户端
        target: 目标节点 dict —— 含 sub_sections → 整章重规划；
                否则视为单子结构 → 只重做这一个
        parent_section: 单子结构模式时的所在章节（提供上下文，可空）
        style/logic: 模板风格/逻辑提示词（注入供参考）

    返回:
        新节点 dict：
        - 整章模式：{id, title, summary, word_count, _tmpl_key, show_label,
                     is_key, rag, _logical_order, status, actual_word_count,
                     _checked, type, sub_sections: [...]}
        - 子结构模式：{id, title, summary, word_count, status, actual_word_count, _checked}

    异常:
        ValueError: LLM 3 次无法输出正确格式
    """
    is_section = isinstance(target.get("sub_sections"), list)

    # ── 构建 prompt ──
    parts = [
        "你是结构化写作规划助手。严格执行以下命令：",
        "",
        "【输出规则】",
        "- 只输出 JSON，禁止任何其他文字、解释、礼貌用语。",
        "- 禁止 markdown 代码块标记（不要 ```json）。",
        "- 直接以 { 开头，以 } 结尾。",
        "",
        f"【文章主题】\n{topic}",
        "",
    ]
    if style:
        parts.append(f"【全文风格】（仅作参考，不改变本节结构要求）\n{style}")
        parts.append("")
    if hints:
        parts.append("【用户对本节点的明确要求】（优先于一切默认值，必须严格遵守）")
        parts.append(hints)
        parts.append("")

    if is_section:
        parts.append("【任务】对该章节重新规划：输出新章节标题和全部子结构（可增减数量）。")
        parts.append("现有章节：")
        parts.append(json.dumps({
            "title": target.get("title", ""),
            "summary": target.get("summary", ""),
            "sub_sections": [
                {"title": ss.get("title", ""), "summary": ss.get("summary", ""),
                 "word_count": ss.get("word_count", SUB_WORDS_DEFAULT)}
                for ss in target.get("sub_sections", [])
            ],
        }, ensure_ascii=False, indent=2))
        parts.append("")
        parts.append("【JSON 格式】")
        parts.append('{')
        parts.append('  "title": "新章节标题（可沿用原标题，也可按用户要求调整）",')
        parts.append('  "sub_sections": [')
        parts.append('    {"title": "子结构1", "summary": "写作要点", "word_count": 500},')
        parts.append('    {"title": "子结构2", "summary": "写作要点", "word_count": 500}')
        parts.append('  ]')
        parts.append('}')
        parts.append(f"子结构 2-4 个、每子结构 {SUB_WORDS_MIN}-{SUB_WORDS_MAX} 字是默认值，用户明确要求时按用户要求。")
    else:
        parts.append("【任务】对该子结构重新规划方向和内容：输出新的子结构标题、要点、字数。")
        if parent_section:
            parts.append(f"所在章节：{parent_section.get('title', '')}")
        parts.append("现有子结构：")
        parts.append(json.dumps({
            "title": target.get("title", ""),
            "summary": target.get("summary", ""),
            "word_count": target.get("word_count", SUB_WORDS_DEFAULT),
        }, ensure_ascii=False, indent=2))
        parts.append("")
        parts.append("【JSON 格式】")
        parts.append('{')
        parts.append('  "title": "新子结构标题",')
        parts.append('  "summary": "写作要点",')
        parts.append('  "word_count": 500')
        parts.append('}')
        parts.append(f"字数默认 {SUB_WORDS_MIN}-{SUB_WORDS_MAX}，用户明确要求时按用户要求。")

    parts.append("")
    parts.append("【后果】如果输出包含 JSON 以外的任何文字，系统将无法解析，整个流程会失败。")

    messages = [
        {"role": "system", "content": "\n".join(parts)},
        {"role": "user", "content": "请输出该节点的 JSON。"}
    ]

    # ── 最多重试 3 次 ──
    result = None
    last_raw = ""
    for attempt in range(3):
        raw = ""
        cont_messages = messages.copy()
        for _cont in range(4):
            r = llm_client.chat_detailed(cont_messages, max_tokens=max(2048, llm_client.max_tokens), temperature=None)
            chunk = r.get("content", "")
            finish_reason = r.get("finish_reason", "stop")
            raw += chunk
            if finish_reason != "length" or not chunk.strip():
                break
            cont_messages.append({"role": "assistant", "content": chunk})
            cont_messages.append({"role": "user", "content": "JSON 输出被截断，请直接从截断处继续输出 JSON 内容，不要重复，不要任何解释文字。"})
        last_raw = raw
        parsed = parse_outline(raw)
        if parsed is not None:
            result = parsed
            break
        messages.append({"role": "assistant", "content": raw[:800]})
        messages.append({"role": "user", "content": "【格式错误】只输出 JSON，以 { 开头，以 } 结尾，不要任何其他文字。重新生成："})

    if result is None:
        raise ValueError(f"LLM 连续 3 次无法输出正确格式。最后一次输出：\n{last_raw[:500]}")

    # ── 组装新节点（身份属性显式继承，展示属性取 LLM 结果） ──
    if is_section:
        subs = []
        for j, ss in enumerate(result.get("sub_sections") or []):
            if not isinstance(ss, dict):
                continue
            subs.append({
                "id": f"{target.get('id', 's')}_{j+1}",
                "title": str(ss.get("title", "")).strip() or f"子结构{j+1}",
                "summary": str(ss.get("summary", "")).strip(),
                "word_count": int(ss.get("word_count") or SUB_WORDS_DEFAULT),
                "status": "pending",
                "actual_word_count": 0,
                "_checked": True,
            })
        if not subs:
            # 极端兜底：LLM 返回空子结构 → 保留原标题的单子结构
            subs = [{
                "id": f"{target.get('id', 's')}_1",
                "title": target.get("title", "正文"),
                "summary": target.get("summary", ""),
                "word_count": target.get("word_count", SUB_WORDS_MAX),
                "status": "pending",
                "actual_word_count": 0,
                "_checked": True,
            }]
        node = {
            "id": target.get("id", "s"),
            "title": str(result.get("title", "")).strip() or target.get("title", "未命名章节"),
            "subtitle": target.get("subtitle", ""),
            "summary": str(result.get("summary", "")).strip() or target.get("summary", ""),
            "word_count": sum(ss["word_count"] for ss in subs),
            "is_key": target.get("is_key", False),
            "status": "pending",
            "actual_word_count": 0,
            "rag": target.get("rag", {"enabled": False, "kb": ""}),
            "_checked": True,
            "type": "section",
            "show_label": target.get("show_label", True),
            "_tmpl_key": target.get("_tmpl_key", target.get("title", "")),
            "_logical_order": target.get("_logical_order"),
            "sub_sections": subs,
        }
    else:
        node = {
            "id": target.get("id", ""),
            "title": str(result.get("title", "")).strip() or target.get("title", ""),
            "summary": str(result.get("summary", "")).strip() or target.get("summary", ""),
            "word_count": int(result.get("word_count") or target.get("word_count", SUB_WORDS_DEFAULT)),
            "status": "pending",
            "actual_word_count": 0,
            "_checked": True,
        }

    return node


def adapt_outline(outline: dict, new_topic: str, llm_client: LLMClient) -> dict:
    """范例大纲适配（快速调用勾选「适配新主题」时调用）。

    只重写内容项——章节标题、章节要点(summary)、子结构标题、子结构要点；
    结构字段（RAG 配置、辅助知识挂载、字数、子结构数量、is_key、勾选状态、
    _tmpl_key、show_label、逻辑顺序）物理不碰——LLM 输出格式里没有这些字段，
    结构守恒从「约束」变成「物理保证」。

    方式 B：LLM 只输出文本映射 {"节点id": {"title": "...", "summary": "..."}}，
    代码校验已知 id、只取 title/summary 写回；LLM 未返回的节点保留原文。

    参数:
        outline: 范例大纲 dict（不修改原对象，内部深拷贝）
        new_topic: 新主题（已覆盖 outline.title）
        llm_client: 规划 LLM 客户端

    返回:
        适配后的新 outline dict（深拷贝）

    异常:
        ValueError: LLM 3 次无法输出正确格式（此时抛错，调用方提示用户）
    """
    result_outline = copy.deepcopy(outline)

    # ── 1. 收集节点清单（只取内容项字段） ──
    nodes = []
    for s in result_outline.get("sections", []):
        nodes.append({
            "id": s.get("id", ""),
            "kind": s.get("type", "section"),
            "title": s.get("title", ""),
            "summary": s.get("summary", ""),
        })
        for ss in s.get("sub_sections", []):
            nodes.append({
                "id": ss.get("id", ""),
                "kind": "sub",
                "title": ss.get("title", ""),
                "summary": ss.get("summary", ""),
            })
    if not nodes:
        return result_outline

    # ── 2. 构建 prompt ──
    parts = [
        "你是大纲适配助手。给定一篇范例大纲和一个新主题，把大纲的「内容项」适配到新主题。",
        "",
        "【硬性约束】",
        "- 只输出 JSON 映射，键是节点 id，值只含 title 和 summary 两个字段。",
        "- 只改标题(title)和写作要点(summary)，这两个是内容项；结构/数量/字数/RAG/辅助知识不在输出中，天然不变。",
        "- 与主题无关的通用节（如\"研究方法\"、\"结论\"）可保留原标题不变。",
        "- 文章结构、子结构数量、每节字数必须保持原样（不体现在输出中）。",
        "- 只输出 JSON，禁止任何其他文字，禁止 markdown 代码块标记。",
        "",
        f"【新主题】\n{new_topic}",
        "",
        "【节点清单（id + 旧标题 + 旧写作要点）】",
        json.dumps(nodes, ensure_ascii=False, indent=2),
        "",
        "【输出格式】",
        '{',
        '  "节点id1": {"title": "适配后的标题", "summary": "适配后的写作要点"},',
        '  "节点id2": {"title": "...", "summary": "..."}',
        '}',
        "只需要输出「需要修改」的节点；不需要改的节点可以省略。",
        "【后果】如果输出包含 JSON 以外的任何文字，系统将无法解析，整个流程会失败。",
    ]

    messages = [
        {"role": "system", "content": "\n".join(parts)},
        {"role": "user", "content": "请输出适配后的节点映射 JSON。"}
    ]

    # ── 3. 最多重试 3 次 ──
    parsed = None
    last_raw = ""
    for attempt in range(3):
        raw = ""
        cont_messages = messages.copy()
        for _cont in range(4):
            r = llm_client.chat_detailed(cont_messages, max_tokens=max(2048, llm_client.max_tokens), temperature=None)
            chunk = r.get("content", "")
            finish_reason = r.get("finish_reason", "stop")
            raw += chunk
            if finish_reason != "length" or not chunk.strip():
                break
            cont_messages.append({"role": "assistant", "content": chunk})
            cont_messages.append({"role": "user", "content": "JSON 输出被截断，请直接从截断处继续输出 JSON 内容，不要重复，不要任何解释文字。"})
        last_raw = raw
        p = parse_outline(raw)
        if p is not None:
            parsed = p
            break
        messages.append({"role": "assistant", "content": raw[:800]})
        messages.append({"role": "user", "content": "【格式错误】只输出 JSON 映射，以 { 开头，以 } 结尾，不要任何其他文字。重新生成："})

    if parsed is None:
        raise ValueError(f"LLM 连续 3 次无法输出正确格式。最后一次输出：\n{last_raw[:500]}")

    # ── 4. 校验 + 写回（只取已知 id 的 title/summary） ──
    if isinstance(parsed, dict):
        valid_ids = {n["id"] for n in nodes}
        for nid, patch in parsed.items():
            if nid not in valid_ids or not isinstance(patch, dict):
                continue
            for s in result_outline.get("sections", []):
                if s.get("id") == nid:
                    if patch.get("title"):
                        s["title"] = str(patch["title"]).strip()
                    if patch.get("summary") is not None:
                        s["summary"] = str(patch["summary"]).strip()
                    break
                for ss in s.get("sub_sections", []):
                    if ss.get("id") == nid:
                        if patch.get("title"):
                            ss["title"] = str(patch["title"]).strip()
                        if patch.get("summary") is not None:
                            ss["summary"] = str(patch["summary"]).strip()
                        break

    return result_outline


# ═══════════════════════════════════════════════════════════
# 模板生成（按 SCHEMA 规矩，从 web_ui gen-template 迁移，行为一致）
# ═══════════════════════════════════════════════════════════

GEN_TEMPLATE_SYSTEM_PROMPT = """你是一个文档模板规划助手。根据用户描述生成模板定义。

输出 JSON：
{
  "name": "模板名称",
  "meta": [
    {"name": "字段名", "show_label": true/false, "desc": "字段要求", "source": "user/llm/auto"}
  ],
  "content": [
    {"name": "字段名", "show_label": true/false, "desc": "写作要点", "type": "leaf/section", "logical_order": 0}
  ],
  "style": "写作风格提示词",
  "logic": "写作顺序提示词（控制LLM的认知流程顺序，不控制文章顺序）"
}

规则：

一、元数据 vs 内容树 —— 严格的二分法：
- 元数据：标识/管理信息（标题、作者、单位、文号、密级、日期等）。
  特征：短（≤100字）、不参与大纲规划、不支持子结构、以键值对渲染。
  位置：放入 meta 数组。
- 内容树：文章正文构成（摘要、引言、正文、结论、参考文献等）。
  特征：长（≥200字）、参与大纲规划、可拆子结构、构成文章主体。
  位置：放入 content 数组。
  互斥：同一个字段不能同时出现在 meta 和 content 中。

二、元数据规则：
- source=user：用户必须填写，LLM不碰（如作者、单位、文号）
- source=auto：用户可填，留空LLM生成（如标题）
- source=llm：由LLM生成（如关键词）——但关键词推荐放入 content 尾部
- show_label=true 输出时带"字段名："前缀，false 不带
- 元数据固定为 leaf（无子结构），不要输出 type 字段

三、内容树规则：
- source 固定为 llm（不输出 source 字段）
- type=leaf：单段内容，不拆子结构（摘要、参考文献、关键词）
- type=section：需要拆 2-4 个子结构（引言、正文各节、结论）
- show_label=true 输出时带"字段名："前缀，false 不带
- desc 写清楚写作要点
- logical_order：可选。不设或留空=按 content[] 顺序写，不需特殊排序。
  需要特殊排序时才设置：0=先写，1=其次，2=最后写（如摘要/关键词需在全文写完后提取）。
  逻辑顺序只控制 LLM 写作时的认知流程，不影响文章最终排列
- **citation_check（引用校验）**：如果字段是引用列表/参考文献，必须设置 `"citation_check": true` 并在 `"citation_format"` 中指定行内引用标记格式和参考文献列表前缀格式，如 `"citation_format": "[x]=1."` 表示正文中用 `[文件名]` 标记引用，参考文献用 `1.` 前缀

四、逻辑提示词（logic）规则：
- 控制 LLM 的认知流程顺序，而非文章最终顺序
- 示例："引言和正文优先写，结论在正文完成后写，关键词和摘要在全文写完后从成品中提取"
- 如果用户未指定，根据字段类型推断合理顺序

五、其他规则：
- 字段数量：元数据 0-8 个 + 内容树 3-12 个
- name 用中文
- style 描述文风和语气"""


def _normalize_template(t: dict) -> dict:
    """校验 + 补默认值，确保模板结构正确（从 web_ui 迁移，行为一致）"""
    if not t.get("name"):
        t["name"] = "自定义模板"

    meta = t.get("meta", [])
    if not isinstance(meta, list):
        meta = []
    cleaned_meta = []
    for f in meta:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        cleaned_meta.append({
            "name": name,
            "show_label": bool(f.get("show_label", True)),
            "desc": str(f.get("desc", "")),
            "source": f.get("source", "auto") if f.get("source") in ("user", "auto", "llm") else "auto"
        })
    t["meta"] = cleaned_meta

    content = t.get("content", [])
    if not isinstance(content, list):
        content = []
    cleaned_content = []
    for f in content:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        entry = {
            "name": name,
            "show_label": bool(f.get("show_label", True)),
            "desc": str(f.get("desc", "")),
            "type": f.get("type", "leaf") if f.get("type") in ("leaf", "section") else "leaf"
        }
        # 小说节点分类（novel 子包专用：setting=设定节点 / chapters=多章展开节点）
        if f.get("kind") in ("setting", "chapters"):
            entry["kind"] = f["kind"]
        lo = f.get("logical_order")
        if lo is not None and lo in (0, 1, 2):
            entry["logical_order"] = lo
        # 引用校验：字段名含"参考文献"或已有 citation_check=true 时自动开启
        if f.get("citation_check") or "参考文献" in name or "引用" in name:
            entry["citation_check"] = True
            entry["citation_format"] = str(f.get("citation_format", "[x]=1."))
        cleaned_content.append(entry)
    t["content"] = cleaned_content

    # style / logic 字符串
    t.setdefault("style", "")
    t.setdefault("logic", "")

    # 清理多余字段（novel 顶层标记放行，novel 子包路由用）
    allowed = {"name", "meta", "content", "style", "logic", "citation_check", "citation_format", "novel", "kind"}
    for k in list(t.keys()):
        if k not in allowed:
            del t[k]

    return t


def generate_template(description: str, llm_client: LLMClient, name: str = "") -> dict:
    """按 SCHEMA 规矩生成模板（3 次重试 + 容错解析，从 web_ui 迁移，行为一致）。

    参数:
        description: 模板描述（必填，非空）
        llm_client: LLM 客户端
        name: 模板名称（可选，注入到 user prompt）

    返回:
        规范化后的模板 dict {name, meta, content, style, logic}

    异常:
        LLMClientError: LLM 3 次均未返回正确格式
    """
    user_content = f"模板名称：{name}\n" if name else ""
    user_content += description
    messages = [
        {"role": "system", "content": GEN_TEMPLATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]

    result = None
    last_raw = ""
    for attempt in range(3):
        try:
            raw = llm_client.chat(messages, max_tokens=None, temperature=0.3)
            last_raw = raw
        except Exception as e:
            if attempt < 2:
                continue
            raise LLMClientError(f"LLM 调用失败: {e}") from e

        # 尝试直接解析
        try:
            result = json.loads(raw.strip())
            if result.get("meta") or result.get("content"):
                break
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 代码块
        if "```" in raw:
            start = raw.index("```")
            end = raw.index("```", start + 3) if "```" in raw[start + 3:] else len(raw)
            content = raw[start + 3:end].strip()
            if content.startswith("json\n"):
                content = content[5:]
            try:
                result = json.loads(content)
                if result.get("meta") or result.get("content"):
                    break
            except (json.JSONDecodeError, ValueError):
                pass

        # 尝试找到第一个 { 提取 JSON
        brace = raw.find("{")
        if brace >= 0:
            try:
                result = json.loads(raw[brace:])
                if result.get("meta") or result.get("content"):
                    break
            except json.JSONDecodeError:
                lines = raw[brace:].split("\n")
                for cut in range(len(lines), 0, -1):
                    try:
                        r = json.loads("\n".join(lines[:cut]))
                        if r.get("meta") or r.get("content"):
                            result = r
                            break
                    except json.JSONDecodeError:
                        continue
                if result:
                    break

        if attempt < 2:
            error_feedback = (
                f"【格式错误】输出必须包含非空的 meta 和 content 字段（至少一个有内容）。\n"
                f"只输出 JSON，不要任何其他文字。\n重新生成："
            )
            messages.append({"role": "assistant", "content": raw[:500]})
            messages.append({"role": "user", "content": error_feedback})

    if result:
        result = _normalize_template(result)
        if result.get("meta") or result.get("content"):
            return result
    raise LLMClientError(
        f"模板生成失败，LLM 3 次均未返回正确格式。最后输出：{last_raw[:300]}"
    )
