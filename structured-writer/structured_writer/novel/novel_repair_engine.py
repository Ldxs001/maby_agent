#!/usr/bin/env python3
"""
Repair Engine — 六检问题修复引擎（P1: T0 自动修复先行）。

v0.3 设计：
- T0 纯格式问题（末行编号/禁用模式）→ 代码直修正文 txt，零 LLM
- T1 内容问题 → 35b 整段重构（P2 实现）
- 双份备份（正文 + state 快照）+ 回滚
"""
import gc
import json
import os
import re
import shutil
import sys
from pathlib import Path

# 与 novel_style_check 保持一致的禁用模式
FORBIDDEN_PATTERNS = [
    (r"\[?(系统提示|助手提示|AI提示|注意：|提示：|请确保|不要|请勿)\]?", "元文本/指令残留"),
    (r"```", "markdown 围栏"),
]

SUFFIX_RE = re.compile(r"^(?:L\d+S\d+|S\d+)$")


# ── T0 修复 ──

def fix_missing_tail(chapter_dir: str, chapter: str, file_name: str) -> dict:
    """T0: 末行编号缺失 → 补全 {chapter}{stem}。返回 {ok, detail}。"""
    f = Path(chapter_dir) / file_name
    if not f.exists():
        return {"ok": False, "detail": f"文件不存在: {file_name}"}
    raw = f.read_text(encoding="utf-8-sig")
    lines = raw.rstrip("\n").split("\n")
    last = lines[-1].strip() if lines else ""
    target = f"{chapter}{f.stem}"
    if SUFFIX_RE.match(last):
        return {"ok": False, "detail": f"{file_name} 末行已存在，跳过"}
    # 旧格式 S01 → 替换；其他 → 追加
    if re.match(r"^S\d+$", last):
        lines[-1] = target
    else:
        lines.append(target)
    _atomic_write(f, "\n".join(lines) + "\n")
    return {"ok": True, "detail": f"{file_name}: 末行 {last!r} → {target}"}


def fix_forbidden_patterns(chapter_dir: str, file_name: str) -> dict:
    """T0: 禁用模式 → 删除匹配行。返回 {ok, detail, removed}。"""
    f = Path(chapter_dir) / file_name
    if not f.exists():
        return {"ok": False, "detail": f"文件不存在: {file_name}"}
    raw = f.read_text(encoding="utf-8-sig")
    lines = raw.split("\n")
    removed = []
    new_lines = []
    for ln in lines:
        hit = False
        for pattern, desc in FORBIDDEN_PATTERNS:
            if re.search(pattern, ln):
                removed.append(ln.strip()[:40])
                hit = True
                break
        if not hit:
            new_lines.append(ln)
    if not removed:
        return {"ok": False, "detail": f"{file_name}: 无禁用模式，跳过"}
    _atomic_write(f, "\n".join(new_lines))
    return {"ok": True, "detail": f"{file_name}: 删除 {len(removed)} 行禁用模式", "removed": removed}


def apply_t0(chapter_dir: str, chapter: str, issues: list) -> dict:
    """对问题清单应用 T0 修复。返回 {fixed: [...], skipped: [...]}。"""
    fixed, skipped = [], []
    for iss in issues:
        file_name = iss.get("file", "")
        problem = iss.get("problem", "")
        if not file_name or not problem:
            continue
        if "末行" in problem:
            r = fix_missing_tail(chapter_dir, chapter, file_name)
            (fixed if r["ok"] else skipped).append(r["detail"])
        elif "禁用模式" in problem:
            r = fix_forbidden_patterns(chapter_dir, file_name)
            (fixed if r["ok"] else skipped).append(r["detail"])
    return {"fixed": fixed, "skipped": skipped}


# ── 工具 ──

def _atomic_write(path: Path, text: str):
    """utf-8 原子写（先写临时文件再 replace）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def backup_segment(chapter_dir: str, file_name: str, state_path: str, round_no: int) -> str:
    """双份备份：正文 + state 快照。返回备份目录。"""
    bak_dir = Path(chapter_dir) / ".backup"
    bak_dir.mkdir(exist_ok=True)
    src = Path(chapter_dir) / file_name
    if src.exists():
        shutil.copy(src, bak_dir / f"{file_name}.r{round_no}")
    if state_path and Path(state_path).exists():
        shutil.copy(state_path, bak_dir / f"state_r{round_no}.json")
    return str(bak_dir)


def rollback_round(chapter_dir: str, round_no: int, state_path: str) -> list:
    """回滚某轮：恢复正文 + state。返回恢复的文件列表。"""
    bak_dir = Path(chapter_dir) / ".backup"
    restored = []
    if not bak_dir.is_dir():
        return restored
    # 恢复正文
    for bak in sorted(bak_dir.glob(f"S*.txt.r{round_no}")):
        target = bak_dir.parent / bak.name.replace(f".r{round_no}", "")
        shutil.copy(bak, target)
        restored.append(target.name)
    # 恢复 state
    state_bak = bak_dir / f"state_r{round_no}.json"
    if state_bak.exists() and state_path:
        shutil.copy(state_bak, state_path)
        restored.append("novel_state.json")
    return restored


# ── T1 整段重构（P2） ──

_REPAIR_CLIENT = None
_REPAIR_CLIENT_KEY = None  # 配置指纹：配置变了才重建（一次修复循环内多段复用同一模型，避免每段重载爆内存）


def _create_repair_client(config_mgr=None):
    """配置驱动：修复模型 = config writer_model（按后端分槽 profile），timeout/max_tokens 全继承。模块级复用。"""
    global _REPAIR_CLIENT, _REPAIR_CLIENT_KEY
    from ..llm_client import LLMClient
    from .model_backend import _model_profile
    if config_mgr is not None:
        wm_cfg = config_mgr.get("writer_model", {}) or {}
    else:
        # 无 config_mgr（CLI）→ 读 config.json
        try:
            cfg = json.loads(Path(__file__).resolve().parent.parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
            wm_cfg = cfg.get("writer_model", {}) or {}
        except Exception:
            wm_cfg = {}
    backend = (wm_cfg or {}).get("backend", "lmstudio")
    wm = _model_profile(wm_cfg)
    key = (backend, wm.get("base_url"), wm.get("model"), wm.get("max_tokens"), wm.get("temperature"))
    if _REPAIR_CLIENT is not None and _REPAIR_CLIENT_KEY == key:
        return _REPAIR_CLIENT
    _REPAIR_CLIENT = LLMClient(
        backend=backend,
        base_url=wm.get("base_url", "http://localhost:1234"),
        timeout=wm.get("timeout", 300),
        model=wm.get("model", ""),
        max_tokens=wm.get("max_tokens", 8192),
        temperature=wm.get("temperature", 0.7),
        # share=True（b28 修正）：修复复用共享实例——规划/写作/修复同一模型只加载一次
        # （用户铁律：跳过修复→规划→弹修复→点修复 不重复加载）。
        # 僵尸根因是 close 与共享表冲突，不是共享本身——修复不主动 close（见 _release_repair_client）。
        # n_ctx 不传：LLMClient 按 max_tokens 自动推导（同一设置）
    )
    _REPAIR_CLIENT_KEY = key
    return _REPAIR_CLIENT


def _release_repair_client() -> None:
    """修复结束释放修复 client 的强引用——不 close 共享实例！

    b28 修正（用户实测"跳过修复→规划→弹修复→点修复 内存不足"）：
    修复复用共享实例（share=True）后，**绝不能 close**——close 会杀掉规划/写作正在用的 35B，
    且 close 后共享表弱引用仍指向已释放底层 ctx 的僵尸实例 → 下次复用 segfault（b24 崩溃根因）。
    正确语义：只释放本 client 引用，35B 由弱引用表自然管理——
    还有其他持有者（规划/写作在跑）→ 继续复用；全部回收 → 自动卸载（b20"整章完成后卸载"）。
    """
    global _REPAIR_CLIENT, _REPAIR_CLIENT_KEY
    if _REPAIR_CLIENT is not None:
        _REPAIR_CLIENT._llama = None
        _REPAIR_CLIENT = None
        _REPAIR_CLIENT_KEY = None
        gc.collect()
        print("[repair] 修复 client 已释放（35B 由共享表管理：有其他任务则复用，无则自动卸载）")


def _build_rewrite_prompt(original: str, title_line: str, alias_line: str, tail_marker: str,
                          plan: dict, prev_tail: str, next_head: str, problems: list,
                          repair_type: str = "", source_text: str = "") -> str:
    """整段重构契约 prompt（v0.3 设计 4.1 节）。repair_type 支持三检类型化修复：
    fidelity（正文兑现概述承诺）/ pledge（移除悬置承诺+平滑衔接）/ ending（收尾类型符合规划）。"""
    original_wc = len(original)
    lo, hi = int(original_wc * 0.85), int(original_wc * 1.15)
    prob_lines = "\n".join(f"- {p}" for p in problems)
    emo = plan.get("emotions") or []
    emo_str = ", ".join(str(e) for e in emo) if emo else "（无）"
    repair_goal = _repair_goal_block(repair_type, plan, source_text)
    return f"""你是小说重写编辑。根据问题清单重写下面的子结构正文，只输出重写后的正文，不要任何解释、思考或 markdown 围栏。

[保留格式]
- 首行标题必须原样保留: {title_line}
- 末行编号必须原样保留: {tail_marker}
- 别名行必须原样保留: {alias_line}

[字数约束]
- 原文 {original_wc} 字，重写后需在 {lo}-{hi} 字（±15%）

[衔接上下文]
- 上一段末尾（前100字）:
{prev_tail or "（无）"}
- 下一段开头（前100字）:
{next_head or "（无）"}

[子结构规划]
- 标题: {plan.get('title', '')}
- 概述: {plan.get('summary', '')}
- 情绪: {emo_str}
- 目标字数: {plan.get('word_count', original_wc)}

[需解决的问题]
{prob_lines}

[修复目标]{repair_goal}

[原文]
{original}

输出：仅重写后的正文（保留上述三行格式）。"""


def _repair_goal_block(repair_type: str, plan: dict, source_text: str) -> str:
    """按修复类型生成 [修复目标] 指令（三检类型化；章检 T1 无类型 → 空）。"""
    if repair_type == "fidelity":
        summary = (plan.get("summary") or "").strip()
        return f"\n该段正文未兑现子结构概述承诺。重写使正文完整实现概述中的承诺内容（关键事件/状态不得缺失或反转），保持文风。概述：{summary[:150]}"
    if repair_type == "pledge":
        return f"\n该段包含未兑现的悬置承诺「{source_text or '（已定位）'}」。重写时移除该承诺意图并平滑衔接上下文，保持文风与字数。"
    if repair_type == "ending":
        return "\n本章结尾收束验证未通过。重写末段使其收尾类型符合规划（封闭式：冲突全解决；开放式：留未来可能；悬停式：悬而未决），保持文风。"
    return ""


def _validate_rewrite(new_text: str, title_line: str, alias_line: str, tail_marker: str,
                      original_wc: int) -> dict:
    """输出校验（v0.3 设计 4.2 节）。返回 {ok, problems}。"""
    problems = []
    if not new_text.strip():
        problems.append("输出为空")
    if title_line and title_line not in new_text:
        problems.append(f"首行标题丢失: {title_line[:30]}")
    if tail_marker and tail_marker not in new_text:
        problems.append(f"末行编号丢失: {tail_marker}")
    if alias_line and alias_line not in new_text:
        problems.append(f"别名行丢失: {alias_line[:20]}")
    wc = len(new_text.strip())
    lo, hi = int(original_wc * 0.85), int(original_wc * 1.15)
    if wc < lo or wc > hi:
        problems.append(f"字数 {wc} 超出 ±15%（{lo}-{hi}）")
    return {"ok": not problems, "problems": problems, "wc": wc}


def rewrite_segment(chapter_dir: str, file_name: str, chapter: str, plan: dict,
                    prev_tail: str, next_head: str, problems: list,
                    config_mgr=None, timeout_extra=180,
                    repair_type: str = "", source_text: str = "", guided: bool = False) -> dict:
    """T1: 整段重构单个子结构；guided=True（R1 推理审核）→ 完整正文 + 问题描述引导局部改写。
    返回 {ok, new_text, problems, wc}。"""
    f = Path(chapter_dir) / file_name
    if not f.exists():
        return {"ok": False, "problems": [f"文件不存在: {file_name}"]}
    raw = f.read_text(encoding="utf-8-sig")
    lines = raw.rstrip("\n").split("\n")
    title_line = lines[0] if lines else ""
    alias_line = next((ln for ln in lines if ln.startswith("【别名】")), "")
    tail_marker = lines[-1].strip() if lines else ""
    body = "\n".join(l for l in lines[1:] if l.strip() and not l.startswith("【别名】")
                     and not SUFFIX_RE.match(l.strip()))

    # 引导式局部改写（R1 推理审核）：R1 的 detail 问题描述本身就是引导——
    # writer 拿到完整正文 + 描述，自行定位问题句并只改那里，其余逐字保留
    if guided:
        prompt = _build_guided_prompt(body, title_line, alias_line, tail_marker, problems)
        client = _create_repair_client(config_mgr)
        call_timeout = int(getattr(client, "timeout", None) or 300) + timeout_extra
        print(f"[repair] 正在重构 {file_name}（引导式局部改写）...")
        try:
            r = client.chat_detailed(
                [{"role": "user", "content": prompt}],
                timeout=call_timeout,
            )
        except Exception as e:
            return {"ok": False, "problems": [f"LLM 调用失败: {type(e).__name__}: {e}"]}
        content = (r or {}).get("content") or ""
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-z]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        valid = _validate_guided(content, body, title_line, alias_line, tail_marker)
        if valid["ok"]:
            return {"ok": True, "new_text": content, "problems": [], "wc": valid["wc"]}
        return {"ok": False, "problems": valid["problems"], "wc": valid["wc"]}

    prompt = _build_rewrite_prompt(body, title_line, alias_line, tail_marker,
                                   plan, prev_tail, next_head, problems,
                                   repair_type=repair_type, source_text=source_text)
    client = _create_repair_client(config_mgr)
    print(f"[repair] 正在重构 {file_name}（整段重构）...")
    # 超时 = 配置 timeout + 额外余量（thinking 模型重写长文）
    call_timeout = int(getattr(client, "timeout", None) or 300) + timeout_extra
    try:
        r = client.chat_detailed(
            [{"role": "user", "content": prompt}],
            timeout=call_timeout,
        )
    except Exception as e:
        return {"ok": False, "problems": [f"LLM 调用失败: {type(e).__name__}: {e}"]}
    content = (r or {}).get("content") or ""
    content = content.strip()
    # 剥 markdown 围栏（若有）
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

    valid = _validate_rewrite(content, title_line, alias_line, tail_marker, len(body))
    if not valid["ok"]:
        return {"ok": False, "problems": valid["problems"], "wc": valid["wc"]}
    return {"ok": True, "new_text": content, "problems": [], "wc": valid["wc"]}


def _build_guided_prompt(body: str, title_line: str, alias_line: str, tail_marker: str,
                         problems: list) -> str:
    """引导式局部改写契约：完整正文 + R1 的问题描述（detail 本身就是引导），
    writer 自行定位问题句并只改那里，其余正文逐字保留。"""
    prob_lines = "\n".join(f"- {p}" for p in problems)
    return f"""你是小说局部改写编辑。以下正文存在审核发现的问题，只改写与问题描述直接相关的句子，其余正文必须逐字保留（不得增删改任何其他字符）。

[保留格式]
- 首行标题必须原样保留: {title_line}
- 末行编号必须原样保留: {tail_marker}
- 别名行必须原样保留: {alias_line}

[问题说明]（审核模型判定——问题就出在以下描述指出的句子/行为上，请在[正文]中定位并仅改写该处）
{prob_lines}

[改写要求]
- 仅修改问题描述直接指出的句子，其余正文逐字保留（不得增删改）
- 修正后符合角色设定/身份处境/推理逻辑，保持文风与字数
- 输出完整正文（含保留格式三行），不要任何解释

[正文]
{body}

输出：仅改写后的完整正文。"""


def _validate_guided(new_text: str, body: str, title_line: str, alias_line: str,
                     tail_marker: str) -> dict:
    """引导式局部改写校验：三行保留 + 输出与原文确有差异（防止未改写空转）。"""
    problems = []
    if not new_text.strip():
        problems.append("输出为空")
    if title_line and title_line not in new_text:
        problems.append(f"首行标题丢失: {title_line[:30]}")
    if tail_marker and tail_marker not in new_text:
        problems.append(f"末行编号丢失: {tail_marker}")
    if alias_line and alias_line not in new_text:
        problems.append(f"别名行丢失: {alias_line[:20]}")
    orig_full = (title_line + "\n" if title_line else "") \
        + (alias_line + "\n" if alias_line else "") \
        + body + ("\n" + tail_marker if tail_marker else "")
    if new_text.strip() == orig_full.strip():
        problems.append("输出与原文完全一致（未改写）")
    return {"ok": not problems, "problems": problems,
            "wc": len(new_text.strip())}


# ── 引擎（P1 T0 + P2 T1 骨架） ──

def run(state_path: str, chapter_dir: str, chapter: str, issues: list,
        mode: str = "manual", max_rounds: int = 3, config_mgr=None,
        checked_subs: list = None, repair_types: dict = None):
    """修复引擎入口。T0 自动修；T1 按勾选子结构整段重构（P2）。
    repair_types: {file: "fidelity"/"pledge"/"ending"} 三检类型化重构（可选）。
    R1 推理审核问题（problem 含"推理审核"）自动走引导式局部改写（detail 描述引导）。"""
    report = {"chapter": chapter, "rounds": [], "mode": mode}
    t0_result = apply_t0(chapter_dir, chapter, issues)
    report["t0"] = t0_result

    t1_issues = [i for i in issues if _is_t1(i)]
    if not t1_issues:
        report["t1"] = {"fixed": [], "skipped": "无 T1 问题"}
        _release_repair_client()  # 无 T1 也释放（防模块级单例残留）
        return report

    try:
        # 按段聚合
        seg_map = {}
        for iss in t1_issues:
            fname = iss.get("file", "")
            if fname:
                seg_map.setdefault(fname, []).append(iss.get("problem", iss.get("desc", "")))
        if checked_subs is not None:
            seg_map = {k: v for k, v in seg_map.items() if k in checked_subs}

        # 三检 source_text 定位（从 state._full_repair 按 chapter+sub 查）
        src_map = {}
        if repair_types:
            try:
                _d = json.loads(Path(state_path).read_text(encoding="utf-8-sig"))
                for _ch, _frc in (_d.get("_full_repair", {}) or {}).items():
                    for _it in (_frc.get("items") or []):
                        if _it.get("sub"):
                            src_map[_it["sub"] + ".txt"] = _it.get("source_text", "")
            except Exception:
                pass

        results = []
        syncs = []
        for fname, probs in seg_map.items():
            rt = (repair_types or {}).get(fname, "")
            # R1 推理审核问题（problem 以"推理审核"开头）→ 引导式局部改写（detail 描述引导 writer 只改问题句）
            guided = any("推理审核" in p for p in probs)
            r = rewrite_segment(chapter_dir, fname, chapter, _load_plan(state_path, chapter, fname),
                                _prev_tail(chapter_dir, fname), _next_head(chapter_dir, fname),
                                probs, config_mgr=config_mgr,
                                repair_type=rt, source_text=src_map.get(fname, ""),
                                guided=guided)
            if r["ok"]:
                backup_segment(chapter_dir, fname, state_path, 1)
                _atomic_write(Path(chapter_dir) / fname, r["new_text"])
                # 重构后同步：实体状态 force 刷新（新正文为准）+ 时间线重扫
                sync_result = sync_after_rewrite(state_path, chapter_dir, fname)
                syncs.append({"file": fname, **sync_result})
                _entry = {"file": fname, "status": "rewritten", "wc": r["wc"]}
                if r.get("problems"):
                    _entry["note"] = "; ".join(r["problems"])
                results.append(_entry)
            else:
                results.append({"file": fname, "status": "failed", "problems": r["problems"]})
        report["t1"] = {"results": results, "syncs": syncs}
        return report
    finally:
        _release_repair_client()  # 修复完成 → 卸载写作模型（35B），显存/内存让给判定模型


def _is_t1(issue: dict) -> bool:
    p = issue.get("problem", "") or issue.get("desc", "")
    if "末行" in p or "禁用模式" in p or "行数" in p:
        return False
    return True


def _load_plan(state_path: str, chapter: str, file_name: str) -> dict:
    """从 state 读子结构规划（title/summary/emotions/word_count）。"""
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8-sig"))
        for ch in data.get("chapters", []):
            if ch["id"] == chapter:
                subs = ch.get("sub_structures", {})
                s_key = Path(file_name).stem
                return subs.get(s_key, {}) or {}
    except Exception:
        pass
    return {}


def _prev_tail(chapter_dir: str, file_name: str) -> str:
    """上一段末尾 100 字（衔接上下文，仅正文，去掉标题/别名/末行）。"""
    files = sorted(Path(chapter_dir).glob("S*.txt"))
    try:
        idx = files.index(Path(chapter_dir) / file_name)
        if idx > 0:
            lines = files[idx - 1].read_text(encoding="utf-8-sig").strip().split("\n")
            body = "\n".join(l for l in lines[1:] if l.strip() and not l.startswith("【别名】")
                             and not SUFFIX_RE.match(l.strip()))
            return body[-100:]
    except (ValueError, OSError):
        pass
    return ""


def _next_head(chapter_dir: str, file_name: str) -> str:
    """下一段开头 100 字（衔接上下文，仅正文，去掉标题/别名/末行）。"""
    files = sorted(Path(chapter_dir).glob("S*.txt"))
    try:
        idx = files.index(Path(chapter_dir) / file_name)
        if idx < len(files) - 1:
            lines = files[idx + 1].read_text(encoding="utf-8-sig").strip().split("\n")
            body = "\n".join(l for l in lines[1:] if l.strip() and not l.startswith("【别名】")
                             and not SUFFIX_RE.match(l.strip()))
            return body[:100]
    except (ValueError, OSError):
        pass
    return ""


# ── 重构后同步（P4：实体状态 force 刷新 + 时间线重扫 + characters 对齐） ──

def sync_after_rewrite(state_path: str, chapter_dir: str, file_name: str) -> dict:
    """重构落盘后同步 state：实体状态以新正文为准（force 覆盖）、时间线重扫。返回 {synced, skipped}。"""
    synced, skipped = [], []
    # 1. 实体状态 force 刷新（复用 extract，force_status=True）
    try:
        from .novel_entity_extractor import extract as _extract
        f = Path(chapter_dir) / file_name
        if f.exists():
            raw = f.read_text(encoding="utf-8-sig")
            lines = raw.rstrip("\n").split("\n")
            body = "\n".join(l for l in lines[1:] if l.strip() and not l.startswith("【别名】")
                             and not SUFFIX_RE.match(l.strip()))
            _extract(state_path, _chapter_of(chapter_dir), f.stem, body, force_status=True)
            synced.append(f"实体: {file_name}")
    except Exception as e:
        skipped.append(f"实体同步失败: {e}")
    return {"synced": synced, "skipped": skipped}


def _chapter_of(chapter_dir: str) -> str:
    """从章节目录名反推章号（如 .../chapters/L02 → L02）。"""
    return Path(chapter_dir).name


# ── 自动模式（P4） ──

def should_auto_repair(config_mgr=None) -> bool:
    """读配置：auto_repair 开关（默认关）。"""
    if config_mgr is not None:
        cfg = config_mgr.get("novel_checks", {}) or {}
        return bool(cfg.get("auto_repair", False))
    try:
        cfg = json.loads(Path(__file__).resolve().parent.parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
        nc = cfg.get("novel_checks", {}) or {}
        return bool(nc.get("auto_repair", False))
    except Exception:
        return False


if __name__ == "__main__":
    # CLI 测试: python novel_repair_engine.py <chapter_dir> <chapter> <issues_json> [state_path]
    if len(sys.argv) >= 4:
        issues = json.loads(sys.argv[3])
        sp = sys.argv[4] if len(sys.argv) > 4 else ""
        rep = run(sp, sys.argv[1], sys.argv[2], issues)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
