#!/usr/bin/env python3
"""
Context Loader — 上下文加载器
验证子结构是否已注册，加载写作上下文
"""
import json, sys
from pathlib import Path

# ── 情绪强度映射表 ──
INTENSITY_LABELS = [
    (0.0, 0.2, "微弱"),
    (0.2, 0.4, "轻度"),
    (0.4, 0.6, "中等"),
    (0.6, 0.8, "强烈"),
    (0.8, 1.0, "极致"),
]

def _intensity_label(val: float) -> str:
    """数值 → 标签"""
    for lo, hi, label in INTENSITY_LABELS:
        if lo <= val < hi:
            return label
    return "极致" if val >= 0.8 else "微弱"


# ── 情绪混合解读映射 ──
EMOTION_MIX_MAP = [
    # (primary, secondary, description)
    ({"愤怒", "恐惧"}, "色厉内荏：愤怒主导，恐惧底色"),
    ({"悲伤", "释然"}, "含泪释怀：悲伤中透出解脱"),
    ({"喜悦", "不安"}, "隐忧之喜：表面快乐，心底不安"),
    ({"恐惧", "好奇"}, "战栗探索：在恐惧中前行"),
    ({"爱", "悲伤"}, "悲伤爱意：深爱伴随失去的痛"),
    ({"平静", "期待"}, "静待之姿：宁静中暗涌期待"),
    ({"愤怒", "悲伤"}, "悲愤交加：愤怒源于深层悲伤"),
    ({"恐惧", "坚定"}, "凛然：恐惧但不退缩"),
    ({"喜悦", "释然"}, "释然喜悦：解脱后的轻松"),
]

def _emotion_mix_description(emotions: list) -> str:
    """分析情绪混合，返回人类可读描述"""
    if not emotions or len(emotions) < 2:
        return ""
    types = {e.get("type", "") for e in emotions}
    for ps, desc in EMOTION_MIX_MAP:
        if ps == types:
            return desc
    # 默认根据主次比描述
    primary = max(emotions, key=lambda e: e.get("intensity", 0))
    secondary = max([e for e in emotions if e != primary], key=lambda e: e.get("intensity", 0)) if len(emotions) > 1 else None
    if secondary and secondary.get("intensity", 0) > 0.3:
        return f"混合情绪：{primary.get('type','')}主导，{secondary.get('type','')}底色"
    return f"单一主导：{primary.get('type','')}"


def _format_emotions(sub: dict) -> str:
    """格式化情绪输出：愤怒 强烈[0.8/1] + 恐惧 轻度[0.3/1]"""
    emos = sub.get("emotions", [])
    if not emos:
        tone = sub.get("tone", "")
        if tone:
            return f"[情绪基调] {tone}"
        return ""
    # 兼容两种格式：遗留的字符串数组 ["疑惑","不安"] 和新格式 [{"type":"疑惑","intensity":0.5}]
    parts = []
    for e in emos:
        if isinstance(e, str):
            parts.append(f"{e}")
        else:
            t = e.get("type", "")
            v = e.get("intensity", 0)
            label = _intensity_label(v)
            parts.append(f"{t} {label}[{v:.1f}/1]")
    line = " + ".join(parts)
    if all(isinstance(e, str) for e in emos):
        return f"[情绪提示] {line}"
    mix = _emotion_mix_description(emos)
    if mix:
        line += f"\n           → {mix}"
    return line


def _check_writing_prompt(sub: dict, chapter: str, sub_key: str) -> None:
    """检查 writing_prompt 是否齐全，缺失则 HARD-BLOCK（不再自动合成 fallback）"""
    wp = sub.get("writing_prompt", "")
    if wp and isinstance(wp, str) and len(wp) >= 50:
        return
    print(f"\n{'='*50}")
    print(f"[HOOK-BLOCK] {chapter}{sub_key} 缺少 writing_prompt")
    print(f"{'='*50}")
    print(f"  writing_prompt（≥50字符）是必填字段，规划阶段必须编写详细剧情指令。")
    print(f"  缺失原因：该子结构在 novel_state.json 中没有 writing_prompt 或长度不足。")
    print(f"")
    print(f"  [修复方案] 重新通过 plan-chapter 注册子结构（含 writing_prompt）：")
    print(f"    1. 编辑子结构 JSON 文件，为 {sub_key} 补全 writing_prompt（≥50字符）")
    print(f"    2. python novel_workflow_engine.py plan-chapter <state_path> <L##> @<file>")
    print(f"    3. 重新运行 context_loader")
    print(f"")
    print(f"  [存量兼容] 已完成章节（status=completed）的子结构不受影响，不会被重新加载。")
    print(f"{'='*50}\n")
    sys.exit(1)


def _find_characters_in_chapter(data: dict, chapter_id: str, sub_key: str) -> list:
    """扫描本章涉及的角色（基于子结构概述匹配角色名）"""
    chars = data.get("characters", [])
    if not chars:
        return []
    ch_info = None
    for ch in data.get("chapters", []):
        if ch["id"] == chapter_id:
            ch_info = ch
            break
    if not ch_info:
        return []
    subs = ch_info.get("sub_structures", {})
    involved = []
    # 从本章所有子结构概述中匹配角色名
    combined = ch_info.get("overview", "")
    for sk, sv in subs.items():
        combined += " " + sv.get("summary", "")
    for c in chars:
        if c.get("name") in combined:
            involved.append(c)
    return involved


def load_context(state_path, chapter, sub_key):
    """加载写作上下文：上一子结构的末3行+当前子结构规划+人格/情绪/文风约束"""
    sp = Path(state_path)
    if not sp.exists():
        print(f"[错误] state 文件不存在: {state_path}")
        sys.exit(1)

    data = json.loads(sp.read_text(encoding="utf-8-sig"))

    # 查找当前章节
    ch_info = None
    for ch in data.get("chapters", []):
        if ch["id"] == chapter:
            ch_info = ch
            break

    if not ch_info:
        print(f"[错误] 章节 {chapter} 未找到")
        sys.exit(1)

    # 验证子结构已注册
    subs = ch_info.get("sub_structures", {})
    if sub_key not in subs:
        print(f"[阻断] {chapter}{sub_key} 未注册，拒绝加载上下文")
        print(f"[提示] 先运行 plan-chapter 注册子结构")
        sys.exit(1)

    # ── 子结构顺序索引（取上一段末3行作上文；不做 CLI 串行阻断——
    #    Web 场景串行由 novel_writer 状态机保证，CLI 的 HOOK-BLOCK/sys.exit 是 novel-weaver 遗留，
    #    在 Web 进程内会误报（写失败遗留 pending 段）并 sys.exit 杀进程） ──
    sub_keys = sorted(subs.keys())
    current_idx = sub_keys.index(sub_key) if sub_key in sub_keys else -1

    # 查找上一个已完成的子结构（取末3行作为上文）
    prev_lines = []
    if current_idx > 0:
        prev_key = sub_keys[current_idx - 1]
        prev_file = Path(sp).parent.parent / "chapters" / chapter / f"{prev_key}.txt"
        if prev_file.exists():
            lines = prev_file.read_text(encoding="utf-8-sig").strip().split("\n")
            prev_text = [l for l in lines if not l.strip().startswith(f"{chapter}")]
            prev_lines = prev_text[-3:] if len(prev_text) >= 3 else prev_text

    # ── [硬性] 字数约束（从子结构 word_count_target 读取，无需硬编码）──
    LENGTH_LABELS = {"short": "短篇", "medium": "中篇", "long": "长篇"}
    length = data.get("meta", {}).get("length", "")
    length_label = LENGTH_LABELS.get(length, length)
    sub_target = subs[sub_key].get("word_count_target", {})
    word_count_note = ""
    if sub_target and sub_target.get("min") and sub_target.get("max"):
        lo, hi, check_hi = sub_target["min"], sub_target["max"], sub_target.get("check_max", int(sub_target["max"] * 1.15))
        word_count_note = f"  篇幅: {length_label}\n  每子结构字数范围: {lo}-{hi}（校验上浮至 {check_hi}）"
    else:
        word_count_note = f"  篇幅: {length_label}（未设定字数目标，请运行 plan-chapter 更新）"

    # ── 输出标准上下文（三层分级：目的★★★ / 背景★★ / 参考★，与规划阶段一致） ──
    print(f"{'='*50}")
    print(f"[上下文] {chapter}{sub_key}")
    print(f"{'='*50}")
    print(f"【优先级】★ 分级说明：")
    print(f"  ★★★ 目的 = 本次写作必须围绕（当前段 + 字数 + 命题框）")
    print(f"  ★★  背景 = 必须遵循的硬约束（文风/视角/署名/人格/收尾/原始需求）")
    print(f"  ★   参考 = 前文事实，尽量参考（实体/行为/时间线/节奏/钩子）")
    print(f"{'='*50}\n")

    # ── [目的★★★] 当前段（写作对象） ──
    print(f"{'='*50}")
    print(f"[目的★★★] 当前子结构（本次写作对象）")
    print(f"{'='*50}")
    print(f"[章节概述] {ch_info.get('overview', '')}")
    print(f"[子结构规划] title={subs[sub_key].get('title','')}")
    print(f"[子结构概述] {subs[sub_key].get('summary','')}")
    print(f"{_format_emotions(subs[sub_key])}")
    print(f"{'='*50}\n")

    # ── [目的★★★] 字数约束 ──
    print(f"{'='*50}")
    print(f"[目的★★★] 字数约束")
    print(f"{'='*50}")
    print(word_count_note)
    print(f"  提示: 以叙事单位自然结束为准，不强行撑到目标")
    print(f"{'='*50}\n")

    # ── [背景★★] 原始需求（用户最初输入，全量注入，写作须贴合） ──
    project_raw = str(data.get("project", "")).strip()
    if project_raw:
        print(f"\n{'='*50}")
        print(f"[背景★★] 原始需求（用户最初输入，全量，必须遵循）")
        print(f"{'='*50}")
        print(f"  {project_raw}")
        print(f"{'='*50}\n")

    # ── [背景★★] 文风约束 ──
    ws = data.get("writing_style", {})
    if ws:
        print(f"\n{'='*50}")
        print(f"[背景★★] 文风约束")
        print(f"{'='*50}")
        for key, label in [("narrative_voice", "叙事视角"),
                           ("tense", "时态"),
                           ("sentence_preference", "句式偏好"),
                           ("vocabulary_register", "词汇"),
                           ("description_depth", "描写深度"),
                           ("custom_rules", "自定义规则")]:
            val = ws.get(key, "")
            if val:
                print(f"  {label}: {val}")
        # 叙事视角强指令：把标签翻译成可执行规则（LLM 只看"第一人称"标签不会执行，
        # 必须明确禁止第三人称代词/角色名叙述主角）
        pov = str(ws.get("narrative_voice", "") or "")
        if "第一人称" in pov:
            print(f"  【视角强制】正文必须以「我」作为叙述者展开：主角的一切行为/感受/心理活动"
                  f"均以「我」发出，禁止用「他/她/主角姓名」作为主角的叙述主语，"
                  f"禁止旁观者视角描写主角的内心。其他角色可用名字/他/她。")
        elif "第二人称" in pov:
            print(f"  【视角强制】正文以「你」作为叙述者展开（第二人称），持续用「你」指代主角。")
        elif "第三人称" in pov and "全知" in pov:
            print(f"  【视角强制】第三人称全知：以「他/她/主角姓名」叙述，可自由进入任何角色内心。")
        elif "第三人称" in pov:
            print(f"  【视角强制】第三人称有限：以「他/她/主角姓名」叙述，仅跟随主角视角，"
                  f"不进入其他角色内心。")
        print(f"  提示: 全文文风一致，不可偏离")
        print(f"{'='*50}\n")

    # ── [背景★★] 署名约束（代码级硬阻断） ──
    sig = data.get("signature", {"enabled": False, "text": ""})
    sig_enabled = sig.get("enabled", False)
    sig_text = sig.get("text", "")
    print(f"\n{'='*50}")
    if sig_enabled:
        print(f"[背景★★] 署名约束")
        print(f"{'='*50}")
        print(f"  状态: 已开启")
        if sig_text:
            print(f"  署名: {sig_text}")
        print(f"  允许在作品末尾添加署名")
        print(f"  禁止使用自行编造的署名文本（必须 = 配置值）")
    else:
        print(f"[背景★★] 署名约束（代码级硬阻断）")
        print(f"{'='*50}")
        print(f"  状态: 已关闭")
        print(f"  禁止在正文中出现任何署名/代名内容")
        print(f"  atomic_writer 代码级阻断，写入即报错")
    print(f"{'='*50}\n")

    # ── [目的★★★] 写作命题框（必填，缺失则 HARD-BLOCK） ──
    _check_writing_prompt(subs[sub_key], chapter, sub_key)
    wp = subs[sub_key].get("writing_prompt", "")
    print(f"\n{'='*50}")
    print(f"[目的★★★] 写作命题框（规划阶段预先编写 — 必填）")
    print(f"{'='*50}")
    print(wp)
    print(f"\n  {'─'*40}")
    print(f"  提示: 命题框为规划阶段编制的核心剧情指令，必须遵循")
    print(f"  提示: 在遵循命题框的前提下可自由发挥文笔、对话和细节")
    print(f"{'='*50}\n")

    # ── [参考] 叙事节奏参考 ──
    sub_keys = sorted(subs.keys())
    current_idx = sub_keys.index(sub_key) if sub_key in sub_keys else -1
    _is_first = (current_idx == 0)
    _is_last_sub = (current_idx == len(sub_keys) - 1)
    print(f"{'='*50}")
    print(f"[参考★] 叙事节奏参考")
    print(f"{'='*50}")
    print(f"  建议按 建立→发展→收束 的弧线自然推进：")
    print(f"  开头：场景锚定 + 人物入场/状态")
    print(f"  中段：核心事件展开（{subs[sub_key].get('summary','')[:40]}...）")
    print(f"  结尾：情绪落点 + 衔接过渡（不强行收住，为下一子结构留流动感）")
    if _is_first:
        print(f"  ℹ️ 章首子结构 — 承接上章尾（第3轮及以上），建立本章基调")
    if _is_last_sub:
        print(f"  ℹ️ 末子结构 — 建议自然收束或设置钩子（非强制）")
    print(f"  提示: 百分比不重要，叙事弧的流动感才重要。段落之间靠因果链自然过渡，不机械分段。")
    print(f"{'='*50}\n")

    # ── [硬性] 输出模板（正文唯一填空，标题+别名+标记由系统生成）──
    title_lbl = subs[sub_key].get("title", "")
    ch_tag = chapter[1:]
    sk_tag = sub_key[1:]
    print(f"\n{'='*50}")
    print(f"[目的★★★] 输出模板（系统组装 — LLM 只需填写下方正文区域）")
    print(f"{'='*50}")
    print("┌─ 填写正文 ──────────────────────────────────────────────")
    print("| <填入正文叙事内容>")
    print("|")
    print("| [可选] 末尾可添加【别名】行（无别名可省略，系统自动补）")
    print("└─────────────────────────────────────────────────────────")
    print(f"  系统自动组装后的完整文件格式（不劳LLM费心）：")
    print(f"  L{ch_tag} · S{sk_tag}《{title_lbl}》  ← 系统生成")
    print(f"  <正文叙事内容>              ← 你填写的内容")
    print(f"  【别名】xxx                   ← 你填写或系统自动补")
    print(f"  L{ch_tag}{sk_tag}                    ← 系统自动追加")
    print(f"  校验规则（atomic_writer 代码级阻断）:")
    print(f"  □ 正文不能为空")
    print(f"  □ 正文禁止出现 L#S# 子结构标记")
    print(f"  □ 正文禁止署名/代名内容")
    print(f"  □ 正文末尾可带【别名】行（缺失则系统自动补，S01可省略）")
    print(f"{'='*50}")
    print(f"[下一步] 写入命令 — 将正文管道写入（不需标题行/标记行）:")
    print(f"  ⚠️ 强制: 必须通过管道写入！禁止使用 Write 工具直接写 S##.txt 文件")
    print(f"  ⚠️ 直接写文件会导致 state 不更新，后续 serial block 阻断")
    print(f"{'─'*50}")
    print(f"  cat <<'EOF' | python novel_workflow_engine.py write-sub \\")
    print(f"    \"{state_path}\" {chapter} {sub_key}")
    print("  <填入正文叙事内容>")
    print("")
    print("  [可选] 【别名】无")
    print("  EOF")
    print(f"{'='*50}\n")

    # ═══════════════════════════════════════════════
    # 以下 6 块原嵌在 if __name__=="__main__" 里——Web 场景（_load_context_captured 只调
    # load_context 函数）永远拿不到，且 main 块访问 data/subs（本函数局部变量）会 NameError。
    # 已移入函数内 + 统一三层分级标签。
    # ═══════════════════════════════════════════════

    # ── [背景★★] 已出场关键人物（登场即累加，不按章节过滤）──
    char_entries = []
    for c in data.get("characters", []):
        fa = c.get("first_appearance", "")
        if not fa:
            continue
        role = c.get("role", "")
        func = c.get("function", "")
        label = f"{c['name']}({role})" if role else c['name']
        aliases = c.get("aliases", [])
        if aliases and isinstance(aliases, list):
            label += f" [别名: {'/'.join(aliases)}]"
        if func:
            char_entries.append(f"  {label}: {func}")
        else:
            # function 为可选字段（历史数据常缺）：不阻断，降级仅显示姓名+角色
            # 曾用 sys.exit(1) 阻断——Web 场景会掐断后续所有上下文块（实体/行为/时间线等），
            # 且 function 缺失不影响写作一致性，改为降级显示。
            char_entries.append(f"  {label}")
    if char_entries:
        print(f"{'='*50}")
        print(f"[背景★★] 已出场关键人物")
        print(f"{'='*50}")
        for line in char_entries:
            print(line)
        print(f"{'='*50}\n")

    # ── [背景★★] 人格约束 ──
    involved = _find_characters_in_chapter(data, chapter, sub_key)
    if involved:
        has_personality = any(c.get("mbti") or c.get("archetype") for c in involved)
        if has_personality:
            print(f"\n{'='*50}")
            print(f"[背景★★] 人格约束")
            print(f"{'='*50}")
            for c in involved:
                mbti = c.get("mbti", "")
                archetype = c.get("archetype", "")
                if mbti or archetype:
                    parts = []
                    if mbti: parts.append(f"MBTI={mbti}")
                    if archetype: parts.append(f"原型={archetype}")
                    print(f"  {c['name']}: {', '.join(parts)}")
            print(f"  提示: 角色言行必须符合其人格设定")
            print(f"{'='*50}\n")

    # ── [参考★] 实体关系网（累计，登场即累加）──
    tracker = data.get("entity_tracker", {"entities": [], "relations": []})
    all_entities = tracker.get("entities", [])
    all_relations = tracker.get("relations", [])
    if all_entities:
        ent_lines = []
        type_order = {"character": "角色", "object": "物品", "location": "地点", "organization": "组织", "data": "数据"}
        for ent_type in ["character", "object", "location", "organization", "data"]:
            group = [e for e in all_entities if e.get("type") == ent_type]
            if not group:
                continue
            label = type_order.get(ent_type, ent_type)
            for e in group:
                attr = e.get("attributes", {})
                parts = [f"{e['name']}"]
                if attr:
                    attr_str = " | ".join(f"{k}={v}" for k, v in attr.items() if v)
                    if attr_str:
                        parts.append(f"[{attr_str}]")
                parts.append(f"[{e.get('first_chapter','?')}{e.get('first_sub','?')}]")
                ent_lines.append(f"    {'  '.join(parts)}")
        if ent_lines:
            print(f"{'='*50}")
            print(f"[参考★] 实体关系网（累计 {len(all_entities)} 实体, {len(all_relations)} 关系）")
            print(f"{'='*50}")
            for line in ent_lines:
                print(line)
            rel_lines = []
            for r in all_relations:
                from_e = next((e for e in all_entities if e["id"] == r.get("from_entity")), None)
                to_e = next((e for e in all_entities if e["id"] == r.get("to_entity")), None)
                if from_e and to_e:
                    _ch = r.get("chapter", "?")
                    _sub = r.get("sub", "?")
                    rel_lines.append(f"    {from_e['name']} → {r.get('predicate','?')} → {to_e['name']} [{_ch}{_sub}]")
            if rel_lines:
                print(f"  ── 关联关系 ──")
                for line in rel_lines:
                    print(line)
            print(f"{'='*50}\n")

    # ── [参考★] 上一章行为轨迹 ──
    prev_behavior = None
    chapters_list = data.get("chapters", [])
    prev_ch_id = ""
    for ci, ch in enumerate(chapters_list):
        if ch["id"] == chapter and ci > 0:
            prev_ch = chapters_list[ci - 1]
            prev_ch_id = prev_ch["id"]
            prev_behavior = prev_ch.get("behavior_summary", {})
            break
    if prev_behavior:
        behavior_lines = []
        for char_name, actions in prev_behavior.items():
            if actions:
                actions_str = " → ".join(str(a) for a in actions[:4])
                behavior_lines.append(f"  {char_name}: {actions_str}")
        if behavior_lines:
            print(f"{'='*50}")
            print(f"[参考★] 上一章行为轨迹（{prev_ch_id}）")
            print(f"{'='*50}")
            for line in behavior_lines:
                print(line)
            print(f"  提示: 当前章应自然延续以上轨迹，无重大断裂")
            print(f"{'='*50}\n")

    # ── [参考★] 时间线（故事进行到第几天，供时间推进参考）──
    tl = data.get("timeline", []) or []
    if tl:
        print(f"{'='*50}")
        print(f"[参考★] 时间线（最近 5 条，故事进行到）")
        print(f"{'='*50}")
        for t in tl[-5:]:
            day = f"day{t.get('day','?')}" if t.get("day") is not None else str(t.get("time_point", "?"))
            print(f"  {day}: {str(t.get('event',''))[:50]}")
        print(f"  提示: 当前段时间推进应顺延以上时间轴，勿倒序/跳变")
        print(f"{'='*50}\n")

    # ── [背景★★] 收尾命题框（is_ending=true 时追加） ──
    if subs[sub_key].get("is_ending"):
        ending_type = subs[sub_key].get("ending_type", "未指定")
        project = data.get("project", "未知项目")
        core_conflict = data.get("core_conflict", "未知冲突")
        protagonist = data.get("protagonist", "未知主角")
        theme = data.get("theme", "未知主题")
        print(f"\n{'='*50}")
        print(f"[背景★★] 收尾约束")
        print(f"{'='*50}")
        print(f"  收尾类型: {ending_type}")
        print(f"  {'─'*40}")
        if ending_type == "封闭式":
            print(f"  □ 核心冲突必须落地（起始于: {core_conflict}）")
            print(f"  □ 主角弧必须闭合（起始于: {protagonist}）")
            print(f"  □ 主题必须回扣（{theme}）")
            print(f"  □ 末句用动作收束（推门。/关灯。/转身。）")
        elif ending_type == "开放式":
            print(f"  □ 核心冲突必须有明确结果（起始于: {core_conflict}）")
            print(f"  □ 留白必须服务于主题（{theme}）")
            print(f"  □ 情绪基调必须收敛")
            print(f"  □ 禁止: 未完待续/预知后事如何/一切才刚刚开始")
        elif ending_type == "悬停式":
            print(f"  □ 留下一个具体悬念（必须可命名）")
            print(f"  □ 悬停点必须是节奏最高处")
            print(f"  □ 主角必须有阶段性成长（起始于: {protagonist}）")
            print(f"  □ 情绪必须有明确指向（焦虑/希望/恐惧/期待）")
            print(f"  □ 禁止: 未完待续/一切才刚刚开始")
        else:
            print(f"  ⚠️ 末子结构概述缺少【收尾类型】标签（应为封闭式/开放式/悬停式）")
            print(f"  □ 请修正概述后重新 plan-chapter")
        print(f"  {'─'*40}")
        print(f"  提示: 以上为命题约束，不可偏离")
        print(f"{'='*50}\n")

    # ── [参考★] 钩子位建议（is_hook_possible=true 时输出，不阻断） ──
    if subs[sub_key].get("is_hook_possible"):
        next_ch_title = ""
        for ci, ch in enumerate(chapters_list):
            if ch["id"] == chapter and ci + 1 < len(chapters_list):
                next_ch = chapters_list[ci + 1]
                next_ch_title = f"{next_ch.get('id', '')}: {next_ch.get('title', '')}"
                break
        print(f"\n{'='*50}")
        print(f"[参考★] 钩子位建议（不强制）")
        print(f"{'='*50}")
        print(f"  本子结构是本章末子结构，可考虑设为伏笔/悬念/承诺")
        if next_ch_title:
            print(f"  下章: {next_ch_title}")
        print(f"  可选类型:")
        print(f"    - 悬念：留下一个未解答的问题")
        print(f"    - 伏笔：埋设一个日后才揭示的线索")
        print(f"    - 承诺：暗示下一章将有重要发展")
        print(f"  如不设伏笔，请确保本子结构自然收束（非悬停式结尾）")
        print(f"{'='*50}\n")


def _load_context_captured(state_path, chapter, sub_key) -> str:
    """Web 场景包装：load_context 是 novel-weaver CLI 脚本（全程 print 无 return），
    用 redirect_stdout 捕获打印内容作为返回值——写作上下文才能真正注入 prompt。
    CLI 直接运行时行为不变（照样打印到终端）。
    """
    import io as _io
    from contextlib import redirect_stdout
    buf = _io.StringIO()
    try:
        with redirect_stdout(buf):
            load_context(state_path, chapter, sub_key)
    except SystemExit:
        # CLI 串行阻断（上一子结构未完成等）→ Web 场景降级返回已捕获内容（不杀进程）
        pass
    return buf.getvalue().strip()


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("用法: python novel_context_loader.py <state_path> <chapter> <sub_key>")
        sys.exit(1)
    load_context(sys.argv[1], sys.argv[2], sys.argv[3])
