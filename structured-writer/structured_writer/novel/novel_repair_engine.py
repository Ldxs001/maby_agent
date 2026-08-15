#!/usr/bin/env python3
"""
Repair Engine — 六检问题修复引擎（P1: T0 自动修复先行）。

v0.3 设计：
- T0 纯格式问题（末行编号/禁用模式）→ 代码直修正文 txt，零 LLM
- T1 内容问题 → 35b 整段重构（P2 实现）
- 双份备份（正文 + state 快照）+ 回滚
"""
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

def _create_repair_client(config_mgr=None):
    """配置驱动：修复模型 = config writer_model，timeout/max_tokens 全继承。"""
    from ..llm_client import LLMClient
    if config_mgr is not None:
        wm = config_mgr.get("writer_model", {}) or {}
    else:
        # 无 config_mgr（CLI）→ 读 config.json
        try:
            cfg = json.loads(Path(__file__).resolve().parent.parent.parent.joinpath("config.json").read_text(encoding="utf-8"))
            wm = cfg.get("writer_model", {}) or {}
        except Exception:
            wm = {}
    return LLMClient(
        backend=wm.get("backend", "lmstudio"),
        base_url=wm.get("base_url", "http://localhost:1234"),
        timeout=wm.get("timeout", 300),
        model=wm.get("model", ""),
        max_tokens=wm.get("max_tokens", 8192),
        temperature=wm.get("temperature", 0.7),
    )


def _build_rewrite_prompt(original: str, title_line: str, alias_line: str, tail_marker: str,
                          plan: dict, prev_tail: str, next_head: str, problems: list) -> str:
    """整段重构契约 prompt（v0.3 设计 4.1 节）。"""
    original_wc = len(original)
    lo, hi = int(original_wc * 0.85), int(original_wc * 1.15)
    prob_lines = "\n".join(f"- {p}" for p in problems)
    emo = plan.get("emotions") or []
    emo_str = ", ".join(str(e) for e in emo) if emo else "（无）"
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

[原文]
{original}

输出：仅重写后的正文（保留上述三行格式）。"""


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
                    config_mgr=None, timeout_extra=180) -> dict:
    """T1: 整段重构单个子结构。返回 {ok, new_text, problems, wc}。"""
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

    prompt = _build_rewrite_prompt(body, title_line, alias_line, tail_marker,
                                   plan, prev_tail, next_head, problems)
    client = _create_repair_client(config_mgr)
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


# ── 引擎（P1 T0 + P2 T1 骨架） ──

def run(state_path: str, chapter_dir: str, chapter: str, issues: list,
        mode: str = "manual", max_rounds: int = 3, config_mgr=None,
        checked_subs: list = None):
    """修复引擎入口。T0 自动修；T1 按勾选子结构整段重构（P2）。"""
    report = {"chapter": chapter, "rounds": [], "mode": mode}
    t0_result = apply_t0(chapter_dir, chapter, issues)
    report["t0"] = t0_result

    t1_issues = [i for i in issues if _is_t1(i)]
    if not t1_issues:
        report["t1"] = {"fixed": [], "skipped": "无 T1 问题"}
        return report

    # 按段聚合
    seg_map = {}
    for iss in t1_issues:
        fname = iss.get("file", "")
        if fname:
            seg_map.setdefault(fname, []).append(iss.get("problem", iss.get("desc", "")))
    if checked_subs is not None:
        seg_map = {k: v for k, v in seg_map.items() if k in checked_subs}

    results = []
    syncs = []
    for fname, probs in seg_map.items():
        r = rewrite_segment(chapter_dir, fname, chapter, _load_plan(state_path, chapter, fname),
                            _prev_tail(chapter_dir, fname), _next_head(chapter_dir, fname),
                            probs, config_mgr=config_mgr)
        if r["ok"]:
            backup_segment(chapter_dir, fname, state_path, 1)
            _atomic_write(Path(chapter_dir) / fname, r["new_text"])
            # 重构后同步：实体状态 force 刷新（新正文为准）+ 时间线重扫
            sync_result = sync_after_rewrite(state_path, chapter_dir, fname)
            syncs.append({"file": fname, **sync_result})
            results.append({"file": fname, "status": "rewritten", "wc": r["wc"]})
        else:
            results.append({"file": fname, "status": "failed", "problems": r["problems"]})
    report["t1"] = {"results": results, "syncs": syncs}
    return report


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
