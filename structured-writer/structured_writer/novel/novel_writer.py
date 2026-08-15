"""novel_writer.py — 小说版写作引擎（章级门控 + 续写恢复）

流程（两阶段，用户确认门控）：
  规划阶段：只生成章数组（plan_novel_outline，sub_sections 空）
  写作阶段：逐章循环——
    done 章    → 从 novel 项目文件恢复正文（续写跳过）
    pending 章 → plan-chapter（注入前文状态摘要防漂移）→ 置 planning → 等用户确认
    planning 章→ 轮询 session 状态等确认（1s，支持停止）
    确认后     → 用 session outline 最新 sub_sections 写作 → 章检 → done
  全文尾：三检（fidelity + 收束 + 完结）
输出：Markdown 小说（标题 + meta + 各章正文）
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from ..llm_client import LLMClientError
from ..state_manager import SESSIONS_DIR
from . import novel_bridge

_logger = logging.getLogger("novel_writer")
_logger.setLevel(logging.ERROR)
try:
    _fh = logging.FileHandler("writer_error.log", mode="a", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [ERROR] %(message)s"))
    _logger.addHandler(_fh)
except Exception:
    pass

NOVEL_SYSTEM_PROMPT = """你是一个小说家。
请根据提供的上下文（角色设定、情绪基调、实体关系、时间线、写作命题），写出指定子结构的正文。

要求：
- 只输出纯叙事正文（Markdown 段落），不要输出标题、不要输出元数据、不要解释
- 严格遵守上下文中的文风约束与命题指令
- 上下文中的【视角强制】是最高优先级写作规则，必须逐字执行：它决定了全文用「我/你/他」中的哪个叙述者，禁止违反（例如要求第一人称时，禁止用角色姓名或"他/她"作为主角叙述主语）
- 情绪通过人物行为/生理反应表达（show don't tell），禁止纯抒情段落
- 对话必须符合角色身份与人格
- 字数尽量接近要求，以叙事自然结束为准
- 若引入了角色的新别名，在正文末尾单独一行输出【别名】原名 = 别名；否则输出【别名】无"""


def _extract_state_path(outline) -> str:
    for s in outline.get("sections", []):
        nv = s.get("_novel") or {}
        if nv.get("state_path"):
            return nv["state_path"]
    return ""


def _render_meta_block(outline, template) -> str:
    """小说线 meta 渲染：show_label=false 的字段（题材/篇幅/叙事视角等流程参数）彻底不渲染。

    与通用线 writer.py 的渲染解耦——小说线专属行为，不影响其他模板输出。
    """
    meta_lines = []
    for field in (template or {}).get("meta", []):
        fname = field.get("name", "")
        if not field.get("show_label", True):
            continue  # 小说线流程参数（题材/篇幅/视角）不进文章
        v = outline.get("meta", {}).get(fname, "")
        meta_lines.append(f"> {fname}：{v}" if v else f"> {fname}：")
    return "\n".join(meta_lines) + "\n\n" if meta_lines else ""


def _reload_session_section(session_id: str, sid: str) -> dict:
    """重载 session 文件中指定章的最新状态（web_ui 确认时更新文件，本线程内存不共享）"""
    try:
        p = SESSIONS_DIR / f"{session_id}.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        for s in data.get("outline", {}).get("sections", []):
            if s.get("id") == sid:
                return s
    except Exception:
        pass
    return {}


def _reload_repair_hint(state_mgr, chapter_id: str) -> dict:
    """重载 session 文件中指定章的修复 hint（修复引擎 apply 完成后写 _repaired=True）"""
    try:
        p = SESSIONS_DIR / f"{state_mgr.session_id}.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        hints = data.get("_repair_hints", {}) or {}
        return hints.get(chapter_id, {}) or {}
    except Exception:
        pass
    return {}


def _wait_confirm(state_mgr, sid: str, stop_check) -> dict | None:
    """轮询等待用户确认当前章（planning → confirmed），返回确认后的章 outline。

    None = 用户停止（立即停止）。
    """
    session_id = state_mgr.session_id
    while True:
        sec = _reload_session_section(session_id, sid)
        status = sec.get("status", "planning")
        if status != "planning":
            return sec or None  # confirmed / done / 其他（确认或已处理）
        if stop_check and stop_check() == "immediate":
            state_mgr.set_status_text("已停止（等待确认中被中断）")
            return None
        time.sleep(1)


def _sync_subs_from_state(section: dict, sid: str, ndata: dict) -> list:
    """从 novel_state.json 同步章的子结构到 outline sub_sections（含 s_key 映射）。

    字段与通用线子结构对齐（is_key/rag/subtitle/_logical_order），
    确认面板/评审界面才能显示 ⭐重点、RAG、字数覆盖、重规划等配置。
    """
    chapter_id = (section.get("_novel") or {}).get("chapter", "")
    ch_state = next((c for c in ndata.get("chapters", []) if c["id"] == chapter_id), {})
    subs_state = ch_state.get("sub_structures", {}) or {}
    sub_sections = []
    for j, sk in enumerate(sorted(subs_state.keys()), 1):
        sv = subs_state[sk]
        wt = sv.get("word_count_target") or {}
        sub_sections.append({
            "id": f"{sid}_{j}",
            "title": sv.get("title", f"子结构{j}"),
            "subtitle": "",
            "summary": sv.get("summary", ""),
            "word_count": int(sv.get("word_count") or 0) or (wt.get("max") if wt.get("max") else _sub_word_target(ndata)),
            "is_key": bool(sv.get("is_key", False)),
            "status": "pending",
            "actual_word_count": 0,
            "rag": {"enabled": False, "kb": ""},
            "_checked": True,
            "type": "leaf",
            "show_label": True,
            "_tmpl_key": None,
            "_logical_order": None,
            "_novel": {"chapter": chapter_id, "s_key": sk},
        })
    # 章字数 = 各子结构字数汇总（与通用线 collectOutlineData 的 sum 语义一致）
    if sub_sections:
        section["word_count"] = sum(ss["word_count"] for ss in sub_sections)
    return sub_sections


def _write_sub_inline(state_path: str, chapter_id: str, s_key: str, title: str, content: str) -> bool:
    """进程内直接落盘子结构正文（替代 write-sub 子进程）。

    Web 服务线程下调子进程写正文存在管道/编码/环境不确定性（真实流程出现过
    写出的 S##.txt 为 0 字节）；进程内写保证「写了必有文件」。
    保留：别名剥离、实体提取、原子写入（tmp + os.replace）、state 字数/状态更新。
    """
    try:
        # 剥离末尾【别名】行（如有），正文清洗
        lines = content.split("\n")
        alias_line = "【别名】无"
        for i, ln in enumerate(lines):
            st = ln.strip()
            if st.startswith("【别名】"):
                alias_line = st
                lines.pop(i)
                break
        clean_body = "\n".join(lines).strip()
        if not clean_body:
            return False
        title_line = f"{chapter_id} · {s_key}\u300a{title}\u300b"
        chapter_dir = Path(state_path).parent.parent / "chapters" / chapter_id
        chapter_dir.mkdir(parents=True, exist_ok=True)
        fp = chapter_dir / f"{s_key}.txt"
        final = f"{title_line}\n\n{clean_body}\n{alias_line}\n{chapter_id}{s_key}\n"
        tmp = fp.with_suffix(".txt.tmp")
        tmp.write_text(final, encoding="utf-8")
        tmp.replace(fp)  # 原子覆盖（Windows 安全）
        # 落盘回读校验：文件真实存在且非空才算落盘成功（防 0 字节假标记）
        if not fp.is_file() or fp.stat().st_size == 0:
            _logger.error(f"落盘回读校验失败 {chapter_id}{s_key}: 文件缺失或 0 字节")
            return False
        # 实体提取（novel-weaver 能力，进程内执行）
        try:
            from .novel_entity_extractor import extract
            extract(str(Path(state_path).resolve()), chapter_id, s_key, clean_body)
        except Exception:
            pass
        # 行为提取（write-sub 逐段，LLM 优先 + 正则回退，进程内执行）
        try:
            from .novel_behavior_extractor import extract_behavior
            extract_behavior(str(Path(state_path).resolve()), chapter_id, s_key, clean_body)
        except Exception:
            pass
        # 时间线提取（write-sub 逐段，LLM 优先 + 正则回退，进程内执行）
        try:
            from .novel_timeline_extractor import extract_timeline
            extract_timeline(str(Path(state_path).resolve()), chapter_id, s_key, clean_body)
        except Exception:
            pass
        # 更新 state：字数 + 状态（运行时字段，不触发指纹）
        from .novel_state_manager import load_state, save_state
        data = load_state(state_path)
        for ch in data.get("chapters", []):
            if ch["id"] == chapter_id:
                sub = ch.get("sub_structures", {}).get(s_key)
                if sub:
                    sub["word_count"] = len(clean_body.replace(" ", "").replace("\n", ""))
                    sub["status"] = "completed"
        save_state(state_path, data, caller="write-sub-inline")
        return True
    except Exception:
        return False


def _read_sub_content(state_path: str, chapter_id: str, s_key: str) -> str:
    """读取单个子结构正文（去标题/末行/别名标记），无文件返回空串。"""
    f = Path(state_path).parent.parent / "chapters" / chapter_id / f"{s_key}.txt"
    if not f.is_file():
        return ""
    lines = []
    for ln in f.read_text(encoding="utf-8-sig").split("\n"):
        st = ln.strip()
        if re.match(r"L\d+ · S\d+《", st):      # 标题行
            continue
        if re.match(r"L\d+S\d+$|S\d+$", st):    # 末行标记（新格式 L02S01；兼容旧格式 S01）
            continue
        if st.startswith("【别名】"):            # 别名行
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def _chapter_files_complete(state_path: str, section: dict, ndata: dict) -> bool:
    """章级续写校验：该章所有子结构文件是否真实落盘且非空（文件为真相源）。

    标记 done 只是预期；文件存在且非空才算「实际写了」。任一段文件缺失/空 → 不完整。
    """
    chapter_id = (section.get("_novel") or {}).get("chapter", "")
    chapter_dir = Path(state_path).parent.parent / "chapters" / chapter_id
    if not chapter_dir.is_dir():
        return False
    # 章内子结构清单：优先 outline 的 sub_sections（含 _checked 勾选状态），否则 state
    sub_keys = []
    for sub in (section.get("sub_sections") or []):
        sk = (sub.get("_novel") or {}).get("s_key", "")
        if sk and sub.get("_checked", True) is not False:
            sub_keys.append(sk)
    if not sub_keys:
        ch_state = next((c for c in ndata.get("chapters", []) if c["id"] == chapter_id), {})
        sub_keys = list((ch_state.get("sub_structures") or {}).keys())
    for sk in sub_keys:
        f = chapter_dir / f"{sk}.txt"
        if not f.is_file() or f.stat().st_size == 0:
            return False
    return bool(sub_keys)


def _chapter_has_subs(section: dict, ndata: dict) -> bool:
    """该章是否规划过子结构（outline sub_sections 或 state sub_structures 有定义即可）。

    空章（从未规划子结构）不允许标 done——续写时必须回 pending 重新规划。
    """
    if section.get("sub_sections"):
        return True
    chapter_id = (section.get("_novel") or {}).get("chapter", "")
    ch_state = next((c for c in ndata.get("chapters", []) if c["id"] == chapter_id), {})
    return bool(ch_state.get("sub_structures"))


def _chapter_any_sub_written(state_path: str, section: dict) -> bool:
    """章内勾选的子结构是否至少一个真实落盘（文件存在且非空）。

    勾选 = _checked 非 False（取消勾选的段用户主动跳过，不算）。
    全部取消勾选 → 无勾选段 → 视为用户主动跳过本章，返回 True（允许标 done）。
    """
    chapter_id = (section.get("_novel") or {}).get("chapter", "")
    chapter_dir = Path(state_path).parent.parent / "chapters" / chapter_id
    sub_keys = []
    for sub in (section.get("sub_sections") or []):
        sk = (sub.get("_novel") or {}).get("s_key", "")
        if sk and sub.get("_checked", True) is not False:
            sub_keys.append(sk)
    if not sub_keys:
        return True  # 全部取消勾选 = 用户主动跳过本章
    for sk in sub_keys:
        f = chapter_dir / f"{sk}.txt"
        if f.is_file() and f.stat().st_size > 0:
            return True
    return False


def _read_chapter_md(state_path: str, section: dict, ndata: dict) -> str:
    """已写章：从 novel 项目文件恢复正文（续写跳过时拼装输出）"""
    chapter_id = (section.get("_novel") or {}).get("chapter", "")
    chapter_dir = Path(state_path).parent.parent / "chapters" / chapter_id
    md = f"\n\n## {section['title']}\n\n"
    if section.get("subtitle"):
        md += f"*{section['subtitle']}*\n\n"
    if not chapter_dir.is_dir():
        return md
    ch_state = next((c for c in ndata.get("chapters", []) if c["id"] == chapter_id), {})
    subs_state = ch_state.get("sub_structures", {}) or {}
    for f in sorted(chapter_dir.glob("S*.txt")):
        content = _read_sub_content(state_path, chapter_id, f.stem)
        if not content:
            continue
        title = subs_state.get(f.stem, {}).get("title", f.stem)
        md += f"### {title}\n\n{content}\n"
    return md


def _resolve_novel_out_dir(state_path: str, title: str) -> Path:
    """小说输出父级目录：跨会话统一（一个小说项目 = 一个父级）。

    首次 → 新建 outputs/<标题>_<时间戳>/ 并写入 .project 标记（记录 state_path）；
    再次续写 → 扫描 outputs/ 找到 .project 匹配同一 state_path 的目录并复用，
    保证跨天/跨会话续写都追加到同一父级，不分裂出多个同名目录。
    """
    from ..state_manager import OUTPUTS_DIR
    _safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip() or "untitled"
    _key = os.path.normcase(str(Path(state_path).resolve()))
    if OUTPUTS_DIR.exists():
        for _d in sorted(OUTPUTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not _d.is_dir():
                continue
            _mark = _d / ".project"
            if _mark.is_file() and os.path.normcase(_mark.read_text(encoding="utf-8").strip()) == _key:
                return _d
    _out_dir = OUTPUTS_DIR / f"{_safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _out_dir.mkdir(parents=True, exist_ok=True)
    (_out_dir / ".project").write_text(state_path, encoding="utf-8")
    return _out_dir


def generate_novel_article(outline, user_orders, rag_options, llm_client,
                           state_mgr, template=None, stop_check=None, rag_client=None,
                           aux_knowledge=None) -> tuple:
    """小说线主写作入口（章级门控），返回 (md_content, output_filepath)。

    outline.sections = 章（_novel.chapter 为 L## 编号，_novel.state_path 为项目状态路径）
    章 status 状态机：pending → planning(待确认) → confirmed(用户确认) → in_progress → done
    rag_options: 评审界面收集的 RAG 开关 {section_id: {enabled, kb}}；rag_client: RAG 服务客户端
    aux_knowledge: 前端「辅助知识」用户指定内容 {section_id: {text, files}}（与通用线同语义）
    """
    title = outline.get("title", "未命名小说")
    state_path = _extract_state_path(outline)
    if not state_path:
        raise ValueError("小说线缺少 state_path（未走 novel 规划）")
    state_path = str(Path(state_path).resolve())

    # 项目状态文件必须存在；缺失时优先从备份自动恢复（角色/设定/实体/命题框不丢），
    # 无备份才 fail-closed（不静默重建——重建 = 残缺设定，比报错更糟）
    if not Path(state_path).is_file():
        restored = novel_bridge.restore_novel_state(state_path)
        if not restored:
            raise ValueError(
                "小说项目状态已丢失（data/novel/projects 下的项目文件不存在），"
                "且无可用备份（data/novel/backups）。无法续写——角色/设定/实体数据不完整。"
                "请重新生成大纲（新开会话），或从「输出」列表查看已完成的章节。"
            )
        _logger.warning(f"项目状态从备份自动恢复: {state_path}")
        # 恢复后正文文件可能也已丢失（chapters/*.txt）——由续写恢复逻辑按文件真相
        # 逐段处理：文件在的跳过，文件丢的置回 pending 重写（_chapter_files_complete/_read_sub_content 已保证）

    state_mgr.set_phase("writing")
    md_parts = []

    # 小说输出目录（章级 md 实时落盘到 chapters/，整本由手动「拼合」生成）
    # 父级跨会话统一：按 .project 标记复用同项目目录，避免跨天续写分裂出多个父级
    _out_dir = _resolve_novel_out_dir(state_path, title)
    _chapters_out = _out_dir / "chapters"
    _chapters_out.mkdir(parents=True, exist_ok=True)

    # 小说质检开关（配置面板「小说质检」区，控制用户权力）
    _checks = ((state_mgr._state or {}).get("config") or {}).get("novel_checks", {}) or {}

    # 加载 novel 项目（角色/文风/章节）
    from .novel_state_manager import load_state
    ndata = load_state(state_path)

    total = len(outline.get("sections", []))
    for idx, section in enumerate(outline.get("sections", []), 1):
        chapter_id = (section.get("_novel") or {}).get("chapter", "")
        sid = section["id"]

        # ── 续写恢复：文件为真相源 ──
        # 只要该章子结构文件真实齐全（磁盘写完），无论 session 状态如何（done/in_progress/pending）
        # 都视为"实际写完"→ 跳过 + 同步 session 为 done（防 session 与磁盘分叉导致重复重写）。
        # 历史根因：写段完成只更新 novel_state（update_sub），session 子结构状态不同步 →
        # 重启后 session 章状态停留在 in_progress/planning → 续写无视磁盘文件直接重写。
        if _chapter_files_complete(state_path, section, ndata):
            if section.get("status") != "done":
                _logger.warning(f"续写恢复：{chapter_id} 文件齐全但 session 状态为 {section.get('status')}，同步为 done（文件为真相源）")
                state_mgr.update_section(sid, {"status": "done",
                                               "actual_word_count": len(_read_chapter_md(state_path, section, ndata).replace(" ", "").replace("\n", ""))})
            md_parts.append(_read_chapter_md(state_path, section, ndata))
            continue
        # 文件不齐全：才看 session 状态决定行为
        if section.get("status") == "done":
            if not _chapter_has_subs(section, ndata):
                # 空章：从未规划过子结构却被标 done（历史误标）→ 回 pending 重新规划
                _logger.warning(f"续写恢复：{chapter_id} 标记 done 但从未规划子结构（空章），回 pending 重新规划")
                state_mgr.update_section(sid, {"status": "pending"})
            else:
                # 有子结构但文件不齐全/为空 = 实际没写完 → 降级段级处理（缺的段重写）
                _logger.warning(f"续写恢复：{chapter_id} 标记 done 但子结构文件不齐全，降级段级处理（缺的段重写）")
                state_mgr.update_section(sid, {"status": "confirmed"})

        # ── 章级门控：仅 pending 章规划子结构（confirmed 章续写时保留已确认的子结构，不重新规划覆盖） ──
        if section.get("status") == "pending":
            state_mgr.update_section(sid, {"status": "in_progress"})
            state_mgr.set_status_text(f"规划子结构: {chapter_id} {section['title']}")
            try:
                novel_bridge.plan_chapter_subs(state_path, chapter_id, template, llm_client)
            except Exception as e:
                state_mgr.set_status_text(f"子结构规划失败: {e}")
                md_parts.append(f"\n\n## {section['title']}\n\n> **子结构规划失败**: {e}\n\n")
                state_mgr.update_section(sid, {"status": "pending"})
                continue
            # 同步子结构到 outline + 置 planning 待确认
            ndata = load_state(state_path)
            section["sub_sections"] = _sync_subs_from_state(section, sid, ndata)
            state_mgr._state["outline"]["sections"] = outline.get("sections", [])
            state_mgr.update_section(sid, {"status": "planning"})
            state_mgr.save()
            state_mgr.set_status_text(f"第 {idx}/{total} 章子结构已规划，请在下方确认面板中确认后继续")
        elif section.get("status") == "planning" and not section.get("sub_sections"):
            # 章级重规划后：子结构已在 state（plan_chapter_subs 已注册），outline 保持章级 →
            # 写作时从 state 同步出来，不重新规划
            ndata = load_state(state_path)
            section["sub_sections"] = _sync_subs_from_state(section, sid, ndata)
            state_mgr._state["outline"]["sections"] = outline.get("sections", [])
            state_mgr.save()

        # ── 等待用户确认（轮询 session 文件，1s；支持停止） ──
        sec = _wait_confirm(state_mgr, sid, stop_check)
        if sec is None:
            break  # 用户停止
        section.update(sec)  # 确认时可能调整了 sub_sections/字数/勾选

        state_mgr.update_section(sid, {"status": "in_progress"})
        state_mgr.set_status_text(f"写作: {chapter_id} {section['title']}")

        sub_sections = section.get("sub_sections", []) or []
        if not sub_sections:
            # 兜底：从 state 读
            ndata = load_state(state_path)
            sub_sections = _sync_subs_from_state(section, sid, ndata)
            section["sub_sections"] = sub_sections
        if not sub_sections:
            # 该章没有任何子结构（从未规划或规划被清空）→ 回 pending 重新规划，
            # 不允许空转直接标 done（否则续写时"一闪而过"跳过整章）
            _logger.warning(f"{chapter_id} 无任何子结构（空章），回 pending 重新规划")
            state_mgr.update_section(sid, {"status": "pending"})
            continue

        section_md = f"\n\n## {section['title']}\n\n"
        if section.get("subtitle"):
            section_md += f"*{section['subtitle']}*\n\n"

        # ── 章级 RAG 查询（评审界面开了 RAG 才查；章背景一次 + 每段一次） ──
        rag_opt = (rag_options or {}).get(sid, {})
        rag_enabled = bool(rag_opt.get("enabled")) and rag_client is not None
        kb = rag_opt.get("kb", "")
        chapter_rag_ctx = None
        sub_rag_ctxs = {}
        if rag_enabled:
            state_mgr.set_status_text(f"RAG查询: {kb or '自动'} → {chapter_id} 章背景")
            try:
                q = f"{title} {section.get('title','')} {section.get('summary','')}"
                r = rag_client.query(kb, q)
                ctx = (r.get("context") or "").strip()
                if ctx:
                    chapter_rag_ctx = ctx
                    state_mgr.set_status_text(f"RAG完成: {kb or '自动'} → {chapter_id}（{len(r.get('sources',[]))}条）")
                else:
                    state_mgr.set_status_text(f"RAG无结果: {kb or '自动'} → {chapter_id}")
            except Exception:
                state_mgr.set_status_text(f"RAG超时: {kb or '自动'} → {chapter_id}")
            for _sub in sub_sections:
                try:
                    q = f"{section.get('title','')} {_sub.get('title','')} {_sub.get('summary','')}"
                    r = rag_client.query(kb, q)
                    ctx = (r.get("context") or "").strip()
                    if ctx:
                        sub_rag_ctxs[_sub["id"]] = ctx
                except Exception:
                    pass

        for j, sub in enumerate(sub_sections, 1):
            if not sub.get("_checked", True):
                continue  # 确认时取消勾选的段跳过（与通用线一致）
            if stop_check and stop_check() == "immediate":
                state_mgr.set_status_text(f"已停止: {sub['title']}（立即停止）")
                break
            ssid = sub["id"]
            # 续写恢复：以文件为真相——**无论 session 状态**，文件真实落盘（非空）即视为已写，跳过；
            # 文件缺失/空 = 实际没写 → 重写该段（不静默丢正文）。
            # 历史根因：段级只看 sub.status==done（同章级 bug），session 状态滞后（写段完成前
            # 线程中断/重启）→ 已写好的段被重写。
            sub_content = _read_sub_content(state_path, chapter_id, sub["_novel"]["s_key"])
            if sub_content:
                if sub.get("status") != "done":
                    _logger.warning(f"续写恢复：{chapter_id}{sub['_novel']['s_key']} 文件已存在但 session 状态为 {sub.get('status')}，同步为 done（文件为真相源）")
                    state_mgr.update_section(ssid, {"status": "done", "actual_word_count": len(sub_content.replace(" ", "").replace("\n", ""))})
                section_md += f"### {sub['title']}\n\n{sub_content}\n"
                continue
            if sub.get("status") == "done":
                _logger.warning(f"续写恢复：{chapter_id}{sub['_novel']['s_key']} 标记 done 但文件缺失/为空，视为未写，重写该段")
                state_mgr.update_section(ssid, {"status": "pending"})
                # 不 continue：落到下方正常写作流程（重写该段）
            state_mgr.update_section(ssid, {"status": "in_progress"})
            state_mgr.set_status_text(f"写作中: {chapter_id} {sub['title']}")

            # 上下文注入（角色/人格/实体/时间线/情绪/命题框/字数）+ RAG 辅助知识
            ctx = novel_bridge.build_writing_context(state_path, chapter_id, sub["_novel"]["s_key"])
            word_target = int(sub.get("word_count") or 0) or _sub_word_target(ndata)
            if sub.get("is_key") or section.get("is_key"):
                word_target = int(word_target * 1.5)  # 重点段/重点章：字数上浮 50%（对齐通用线 is_key 语义）
            rag_ctx = sub_rag_ctxs.get(ssid) or chapter_rag_ctx
            # 用户指定辅助知识（与通用线 writer.py 同语义：text=使用指令，files=文字/表格资料）
            aux_text = _build_sub_aux(aux_knowledge, ssid)
            prompt = (
                f"# 小说：{title}\n\n"
                f"【当前子结构】{chapter_id}{sub['_novel']['s_key']}《{sub['title']}》\n\n"
                f"【字数目标】约 {word_target} 字（该段正文的合理长度，勿明显超出或不足）\n\n"
                + (f"【辅助知识】（用户指定参考内容，请优先采用，化用进叙事）：\n{aux_text}\n\n" if aux_text else "")
                + (f"【RAG 参考资料】（来自知识库检索，结合主题选择性参考）：\n{rag_ctx}\n\n" if rag_ctx else "")
                + f"---\n\n{ctx}\n\n---\n\n请写出该子结构的正文（纯叙事）。只输出正文。"
            )
            messages = [
                {"role": "system", "content": NOVEL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            try:
                accumulated = ""
                cont_messages = messages.copy()
                empty_retries = 0
                for _attempt in range(8):
                    if stop_check and stop_check() == "immediate":
                        break
                    # 可靠超时：urllib timeout 在 Windows 下对读取 socket 不可靠（模型无响应可无限挂）→
                    # 用 daemon 线程 + join(timeout) 兜底，超时抛 LLMClientError 由外层捕获继续
                    _call_result = {}
                    def _call_llm():
                        try:
                            _call_result["r"] = llm_client.chat_detailed(cont_messages)
                        except Exception as _e:
                            _call_result["e"] = _e
                    _th = threading.Thread(target=_call_llm, daemon=True)
                    _th.start()
                    # 超时跟随用户配置（writer_model.timeout，默认 300s 与通用线一致）——
                    # 不拍脑袋设短值：长正文+思考可能超 120s，掐断会误杀正常写作
                    _call_timeout = int(getattr(llm_client, "timeout", None) or 300)
                    _th.join(timeout=_call_timeout)
                    if _th.is_alive():
                        raise LLMClientError(f"LLM 调用超时（{_call_timeout}s 无响应），该段已跳过")
                    if "e" in _call_result:
                        raise _call_result["e"]
                    result = _call_result["r"]
                    chunk = result.get("content", "") or ""
                    if not chunk.strip():
                        # 模型只输出了思考（reasoning）没落地正文（LM Studio 推理模型常见）→ 反馈重试
                        # 不禁止思考——要求"思考已接收，正文请落地输出"
                        empty_retries += 1
                        if empty_retries >= 3:
                            break  # 连续 3 次空输出，放弃该段（降级，不阻断）
                        cont_messages.append({"role": "user",
                                              "content": "（未收到正文）你上一轮只输出了思考过程，没有输出实际正文。请直接输出该子结构的叙事正文，从第一句话开始写，不要思考过程、不要标题、不要任何格式标记。"})
                        continue
                    accumulated += chunk
                    if result.get("finish_reason") != "length":
                        break
                    cont_messages.append({"role": "assistant", "content": chunk})
                    cont_messages.append({"role": "user", "content": "请继续写，紧接着上一段结尾，不要重复已写内容。"})
            except LLMClientError as e:
                accumulated = f"\n\n> **写作失败**: {e}\n\n"
            content = accumulated.strip()
            if not content:
                state_mgr.update_section(ssid, {"status": "pending"})
                continue

            # 写入 novel 项目（进程内直接落盘，替代 write-sub 子进程——写了必有文件；
            # 失败停止整章——该段没写，后续段写了下文断链，等重试整章）
            if not _write_sub_inline(state_path, chapter_id, sub["_novel"]["s_key"], sub.get("title", ""), content):
                _logger.error(f"落盘失败 {chapter_id}{sub['_novel']['s_key']}")
                state_mgr.update_section(ssid, {"status": "pending", "actual_word_count": 0})
                state_mgr.set_status_text(f"写入失败: {chapter_id}{sub['_novel']['s_key']}（本章停止，可重新生成本章）")
                break

            sub_md = f"### {sub['title']}\n\n{content}\n"
            section_md += sub_md
            actual_chars = len(content.replace(" ", "").replace("\n", ""))
            state_mgr.update_section(ssid, {"status": "done", "actual_word_count": actual_chars})

        md_parts.append(section_md)
        # 章末尾落盘校验：勾选的子结构必须至少一个真实落盘（文件为真相源）。
        # 全勾选段都未落盘（写失败/全跳过）→ 章不标 done，回 pending 等重试；
        # 全部取消勾选（用户主动跳过本章）→ 视为用户选择，标 done 允许跳过。
        if not _chapter_any_sub_written(state_path, section):
            _logger.warning(f"{chapter_id} 勾选子结构均未落盘，章不标 done，回 pending")
            state_mgr.update_section(sid, {"status": "pending"})
            continue

        # 章检六检（规则4检 + bge/R1，子进程；失败不阻断主流程；开关控制）
        # 顺序：先检后标 done——finalize 是章级裁判，有 HARD → 拦截等修复，通过后才标 done
        # 判定用 issues 非空（HARD/FAIL 行）而非 fc.ok——ok 是子进程退出码==0，
        # 有 HARD 时 workflow_engine 只写 fixes 正常 return（退出码 0），ok 恒 True，不可信。
        _fc_final = None
        try:
            fc = novel_bridge.finalize_novel_chapter(state_path, chapter_id, checks=_checks)
            _fc_final = fc
            _has_hard = bool(fc.get("issues"))
            if _has_hard:
                state_mgr.set_status_text(f"章检发现 HARD 问题: {chapter_id}（等待修复）")
            # 保存章检结果到 state（供前端修复面板读取：T0/T1 分级清单）
            try:
                _checks_result = {
                    "chapter": chapter_id,
                    "ok": fc.get("ok", False),
                    "timeout": fc.get("timeout", False),
                    "issues": fc.get("issues", []),           # HARD/FAIL 行
                    "output": (fc.get("output") or "")[-3000:],  # 完整 stdout（含 SOFT）
                }
                state_mgr.save_repair_hint(chapter_id, _checks_result)
            except Exception:
                pass
            # ── HARD 拦截：章级待修复，主循环暂停等修复引擎（最多 max_repair_rounds 轮） ──
            if _has_hard:
                _repair_rounds = int(_checks.get("repair_rounds", 3) or 3)
                _round = 0
                while _round < _repair_rounds:
                    _round += 1
                    state_mgr.set_status_text(f"章检 HARD: {chapter_id}（修复轮次 {_round}/{_repair_rounds}，等待修复引擎）")
                    # 轮询 hint._repaired（修复引擎 apply 完成后置 True）；支持停止
                    _hint = _reload_repair_hint(state_mgr, chapter_id)
                    while not (_hint and _hint.get("_repaired")):
                        if stop_check and stop_check() == "immediate":
                            state_mgr.set_status_text(f"已停止（{chapter_id} 修复等待中被中断）")
                            return None
                        time.sleep(2)
                        _hint = _reload_repair_hint(state_mgr, chapter_id)
                    # 修复完成 → 重检全六检（聚合重检：修复后全跑）
                    state_mgr.set_status_text(f"重检: {chapter_id}（修复后第 {_round} 轮全六检）")
                    fc2 = novel_bridge.finalize_novel_chapter(state_path, chapter_id, checks=_checks)
                    _fc_final = fc2
                    _checks_result2 = {
                        "chapter": chapter_id,
                        "ok": fc2.get("ok", False),
                        "timeout": fc2.get("timeout", False),
                        "issues": fc2.get("issues", []),
                        "output": (fc2.get("output") or "")[-3000:],
                        "_repaired": True,
                    }
                    state_mgr.save_repair_hint(chapter_id, _checks_result2)
                    if not fc2.get("issues"):
                        break  # 重检通过（issues 无 HARD）
                    # 重检仍 HARD → 重置 _repaired，再弹面板等下一轮修复
                    _checks_result3 = dict(_checks_result2)
                    _checks_result3["_repaired"] = False
                    state_mgr.save_repair_hint(chapter_id, _checks_result3)
                if _fc_final is None or _fc_final.get("issues"):
                    # 3 轮仍 HARD → 章回 pending 交人工（正文保留，可手动改后重 finalize）
                    state_mgr.update_section(sid, {"status": "pending"})
                    state_mgr.set_status_text(f"{chapter_id} 修复 {_repair_rounds} 轮仍未通过，章回 pending 待人工处理")
                    continue  # 跳过本章，继续下一章
        except Exception as e:
            _logger.error(f"finalize-chapter 异常 {chapter_id}: {e}")

        # finalize 通过（或异常兜底按通过处理）→ 标章级 done
        state_mgr.update_section(sid, {
            "status": "done",
            "actual_word_count": len(section_md.replace(" ", "").replace("\n", "")),
        })

        # 章级 md 实时落盘（树状输出：题目下挂已完成章，随时可读；整本由手动「拼合」生成）
        try:
            _ch_out = _chapters_out / f"{chapter_id}.md"
            _ch_out.write_text(section_md.strip() + "\n", encoding="utf-8")
        except Exception as e:
            _logger.error(f"章级 md 落盘失败 {chapter_id}: {e}")

        state_mgr.set_status_text(f"进度: {idx}/{total} 章完成")

    # ── meta 块 + 全文组装 ──
    meta_block = _render_meta_block(outline, template)
    article_md = f"# {title}\n{meta_block}" + "".join(md_parts)
    article_md = article_md.strip()

    # ── 全文三检（fidelity + 收束 + 完结；开关控制） ──
    state_mgr.set_status_text("全文质检中（忠实度+结尾收束）…")
    try:
        fn = novel_bridge.finalize_novel_full(state_path, checks=_checks)
        if fn.get("issues"):
            article_md += "\n\n---\n\n## 质检报告（HARD/FAIL 问题）\n\n" + "\n".join(f"- {i}" for i in fn["issues"][:20])
        elif fn.get("output"):
            ok_lines = [ln for ln in (fn.get("output") or "").split("\n") if "[OK]" in ln or "通过" in ln]
            if ok_lines:
                article_md += "\n\n---\n\n## 质检报告\n\n" + "\n".join(f"- {ln.strip()}" for ln in ok_lines[:10])
    except Exception as e:
        _logger.error(f"finalize-novel 异常: {e}")

    # ── 输出目录（章级 md 已在写作中落盘到 chapters/；整本 md 由用户手动「拼合」生成） ──
    # 不自动拼合整本——长篇跨天写作，已完成章通过树状输出随时可读；需要完整版时手动拼合。
    output_path = str(_out_dir)
    state_mgr.set_output_file(output_path)
    state_mgr.set_phase("done")
    return article_md, output_path


def _sub_word_target(ndata) -> int:
    """每子结构字数目标（meta.length 三档，与 plan-chapter 同源）"""
    length_key = ndata.get("meta", {}).get("length", "medium")
    lo, hi = novel_bridge.LENGTH_TARGETS.get(length_key, (1000, 1500))
    return (lo + hi) // 2


def _build_sub_aux(aux_knowledge, ssid: str) -> str:
    """组装用户指定辅助知识（与通用线 writer.py 同语义）。

    aux_knowledge: {section_id: {text: 使用指令, files: [{type, name, content|path}]}}
    返回组装文本（空 = 无辅助知识）。文字资料原样注入（截断防撑爆）；表格资料注入原始
    content 文本（小说线暂不做通用线的表格解析/选列，后续对齐）；图片登记跳过。
    """
    if not aux_knowledge:
        return ""
    ak = aux_knowledge.get(ssid, {}) or {}
    cmd = ak.get("text", "") or ""
    parts = []
    for f in ak.get("files") or []:
        ftype = f.get("type", "text")
        fname = f.get("name", "file")
        if ftype == "text" and f.get("content"):
            content = str(f["content"])
            parts.append(f"[{fname}]\n{content[:4000]}")
        elif ftype == "table" and f.get("content"):
            parts.append(f"[{fname}]\n{str(f['content'])[:4000]}")
        elif ftype == "table" and f.get("path"):
            # 表格路径形式：读 CSV 文本注入（简单兜底）
            try:
                from pathlib import Path as _P
                fp = _P(f["path"])
                if fp.is_file():
                    parts.append(f"[{fname}]\n{fp.read_text(encoding='utf-8-sig', errors='ignore')[:4000]}")
            except Exception:
                pass
        # image 类型：小说线不做插图，跳过
    if cmd:
        parts.insert(0, cmd)
    return "\n\n---\n\n".join(parts)
