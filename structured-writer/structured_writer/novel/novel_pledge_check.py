#!/usr/bin/env python3
"""
Novel Pledge Check — 全文承诺检查（flag 提取 + 推理判定）

- extract_pledges: 3B（Qwen2.5-3B）按章提取"角色做出的决定/计划/承诺"（意图事件 flag），
  写入 novel_state.json 的 pledges[]。每章一次调用（输入=章概述+子结构 summary 列表）。
- check_pledges: writer_model 全套配置（config 驱动）创建 LLMClient，一次推理判每个 flag：
  已兑现 / 未兑现 / 悬停（无后续提及）。未兑现/悬停 → 产出 issues（SOFT 提示）。
- 回退链：
  ① 推理 client 不可用/超时 → 关键词回退（flag 核心词在提出章之后的章节正文出现 → 视为兑现）
  ② 3B 提取不可用 → 无 flag，检查跳过（返回空）

数据模型（novel_state.pledges[]）：
  {"id": "P01", "text": "决定潜入实验室", "char": "林渊", "chapter": "L01", "status": "unresolved"}
"""
import json
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent

EXTRACT_PROMPT = """你是小说承诺提取器。从【本章内容】中提取"角色做出的决定/计划/承诺"——即**尚未兑现的意图事件**。
flag 信号词：尝试做X、打算做X、决定做X、计划做X、发誓要X、承诺X、必须找到X、要查明X。
输出一个 JSON 数组，不要解释、不要 markdown 围栏：
[{{"text": "意图描述（10-30字）", "char": "角色名"}}]
示例：
[{{"text": "决定潜入实验室查明数据被篡改的真相", "char": "林渊", "sub": "S04"}}]
要求：
- 只提取"打算做某事/尝试做某事"的意图（含"尝试/决定/计划/打算/发誓/承诺/必须/要"等信号词），不提取已完成的动作
- sub：该承诺在哪个子结构提出（S01/S02...）
- 无意图事件 → 输出空数组 []
- char 用角色名（无明确角色用"未知"）

【本章内容】（子结构正文，含段标识）
{chapter_info}"""

JUDGE_PROMPT = """你是小说承诺兑现审核员。以下是小说中角色提出的【承诺清单】和全书【章节内容摘要】。
判断每个承诺是否兑现：
- 已兑现：后续正文明确实现了该承诺
- 未兑现：后续正文明确否定、放弃或违背该承诺
- 悬停：后续正文完全未提及，承诺悬而未决
只输出一个 JSON 数组，不要解释、不要 markdown 围栏：
[{{"id": "P01", "status": "已兑现/未兑现/悬停", "evidence": "依据（引用相关章节的概述或情节）"}}]

【承诺清单】
{flags}

【全书章节内容摘要】
{book_summary}"""


def _load_3b():
    """懒加载 3B（复用提取器缓存）。"""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from novel_timeline_extractor import _load_extract_model
        loaded = _load_extract_model()
        if loaded is None:
            return None
        return loaded
    except Exception:
        return None


def _gen_3b(model, tok, prompt: str) -> str:
    import torch
    chatml = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
    inputs = tok(chatml, return_tensors="pt")
    with torch.no_grad():
        out = model.generate(
            inputs["input_ids"],
            max_new_tokens=300,
            do_sample=False,
            temperature=0.2,
            pad_token_id=tok.eos_token_id,
        )
    return tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


def _parse_json_array(raw: str):
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    return None


_SIGNAL_RE = re.compile(r"(决定|计划|打算|发誓|承诺|试图|尝试|必须|一定要|要)")
_SIGNAL_WORDS = {"决定", "计划", "打算", "发誓", "承诺", "试图", "尝试", "必须", "一定要", "要", "找到", "发现", "开始"}


def _locate_source_text(chapters_dir, ch_id: str, sub: str, text: str) -> str:
    """规则定位提出句：flag.sub 对应正文里，找含信号词 + flag 核心词的句子（原句）。

    3B 输出不了原句（source_text 字段不可靠），改用规则在正文中定位——
    信号词（决定/计划...）命中 + flag 文本核心词（2-4 字窗口）部分命中 → 该句即提出句。
    """
    if not sub:
        return ""
    fpath = Path(chapters_dir) / ch_id / f"{sub}.txt"
    if not fpath.is_file():
        return ""
    content = fpath.read_text(encoding="utf-8-sig")
    sents = [s.strip() for s in re.split(r"(?<=[。！？])", content) if s.strip()]
    if not sents:
        return ""
    # flag 文本核心词（2-4 字窗口，去信号词）
    cleaned = re.sub(r"[^\u4e00-\u9fff]", "", text)
    cores = set()
    for wlen in (4, 3, 2):
        for i in range(len(cleaned) - wlen + 1):
            cores.add(cleaned[i:i + wlen])
    cores -= _SIGNAL_WORDS
    if not cores:
        # 无核心词 → 取第一个含信号词的句子
        for s in sents:
            if _SIGNAL_RE.search(s):
                return s[:100]
        return ""
    for s in sents:
        if _SIGNAL_RE.search(s):
            hit = sum(1 for w in cores if w in s)
            if hit >= max(1, len(cores) // 4):
                return s[:100]
    # 兜底：第一个含信号词的句子
    for s in sents:
        if _SIGNAL_RE.search(s):
            return s[:100]
    return ""


def extract_pledges(state_path: str, chapters_dir) -> bool:
    """3B 按章提取 flag 写入 state.pledges。成功 True；3B 不可用 False。"""
    sp = Path(state_path)
    if not sp.is_file():
        return False
    state = json.loads(sp.read_text(encoding="utf-8-sig"))
    loaded = _load_3b()
    if loaded is None:
        return False
    model, tok = loaded

    all_flags = []
    fid = 1
    for ch in state.get("chapters", []):
        if ch.get("status") != "completed":
            continue
        ch_id = ch["id"]
        ch_dir = Path(chapters_dir) / ch_id
        # 从子结构正文提取（正文含原句，供减法重构定位）
        parts = []
        if ch_dir.is_dir():
            for sf in sorted(ch_dir.glob("S*.txt")):
                content = sf.read_text(encoding="utf-8-sig")
                lines = [l for l in content.split("\n") if l.strip()
                         and not re.match(rf'{ch_id}S\d+', l.strip())
                         and not re.match(r'L\d+ · S\d+《', l.strip())]
                body = "".join(lines)
                if body:
                    parts.append(f"【{sf.stem}】{body[:400]}")
        chapter_info = "\n".join(parts)[:1600]
        raw = _gen_3b(model, tok, EXTRACT_PROMPT.format(chapter_info=chapter_info))
        arr = _parse_json_array(raw)
        if not arr:
            print(f"[承诺提取] {ch_id}: 无 flag 或解析失败")
            continue
        for it in arr[:6]:
            if not isinstance(it, dict) or not it.get("text"):
                continue
            sub = str(it.get("sub") or "")[:8]
            all_flags.append({
                "id": f"P{fid:02d}",
                "text": str(it["text"])[:60],
                "char": str(it.get("char") or "未知")[:20],
                "chapter": ch_id,
                "sub": sub,
                "source_text": _locate_source_text(chapters_dir, ch_id, sub, str(it["text"])),
                "status": "unresolved",
            })
            fid += 1
        print(f"[承诺提取] {ch_id}: {len(arr)} 个 flag")

    state["pledges"] = all_flags
    sp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[承诺提取] 全书共 {len(all_flags)} 个 flag")
    return True


def _create_writer_client():
    """writer_model 全套配置驱动 LLMClient（绝对导入直接构造，不依赖包上下文）。"""
    try:
        sys.path.insert(0, str(SCRIPTS_DIR.parent))
        from llm_client import LLMClient
    except Exception as e:
        print(f"[全文承诺] LLMClient 导入失败: {e}")
        return None
    try:
        cfg = json.loads(
            Path(__file__).resolve().parent.parent.parent.joinpath("config.json").read_text(encoding="utf-8")
        )
        wm = cfg.get("writer_model", {}) or {}
    except Exception:
        wm = {}
    return LLMClient(
        backend=wm.get("backend", "lmstudio"),
        base_url=wm.get("base_url", "http://localhost:1234"),
        timeout=wm.get("timeout", 300),
        model=wm.get("model", ""),
        max_tokens=wm.get("max_tokens", 4096),
        temperature=wm.get("temperature", 0.7),
    )


def _keyword_fallback(state, flags, chapters_dir) -> list:
    """关键词回退：flag 核心词（text 去虚词后的实词）在提出章之后的章节正文出现 → 视为兑现。"""
    import jieba  # 可用则分词取实词；不可用取 2-4 字窗口
    issues = []
    chapters = state.get("chapters", [])
    for fl in flags:
        fl_ch = fl.get("chapter", "")
        text = fl.get("text", "")
        # 关键词：取 text 中 2-4 字窗口（去常见虚词）
        words = set()
        if 'jieba' in sys.modules:
            for w in jieba.cut(text):
                if len(w) >= 2 and w not in ("决定", "计划", "承诺", "打算", "发誓", "必须", "一定"):
                    words.add(w)
        cleaned = re.sub(r"[^\u4e00-\u9fff]", "", text)
        for wlen in range(4, 1, -1):
            for i in range(len(cleaned) - wlen + 1):
                words.add(cleaned[i:i + wlen])
        words = words - {"决定", "计划", "承诺", "打算", "发誓", "必须", "一定", "要"}
        if not words:
            continue
        # 检查提出章之后的所有章节正文
        found = False
        later = [c for c in chapters if c.get("id", "") > fl_ch and c.get("status") == "completed"]
        for c in later:
            cd = Path(chapters_dir) / c["id"]
            if not cd.is_dir():
                continue
            for sf in sorted(cd.glob("S*.txt")):
                content = sf.read_text(encoding="utf-8-sig")
                hit = sum(1 for w in words if w in content)
                if hit >= max(1, len(words) // 2):
                    found = True
                    break
            if found:
                break
        if not found:
            issues.append({
                "file": fl_ch,
                "problem": f"[全文承诺] {fl.get('char', '')}「{text}」未在后续章节兑现（关键词回退判定）",
                "position": fl_ch,
                "severity": "SOFT",
                "sub": fl.get("sub", ""),
                "source_text": fl.get("source_text", ""),
                "suggestion": "检查该承诺是否在后续章节回收，或补充收束",
            })
    return issues


def check_pledges(state_path: str, chapters_dir) -> list:
    """全文承诺检查：writer 配置推理 flag 兑现状态。返回 issues。"""
    sp = Path(state_path)
    if not sp.is_file():
        return []
    state = json.loads(sp.read_text(encoding="utf-8-sig"))
    flags = state.get("pledges") or []
    if not flags:
        return []

    # 组装全书摘要（各章概述 + 子结构 summary，按章序）
    parts = []
    for ch in state.get("chapters", []):
        if ch.get("status") != "completed":
            continue
        ch_id = ch["id"]
        subs = ch.get("sub_structures") or {}
        sub_part = "；".join(
            f"{sk}: {subs[sk].get('summary', '')[:60]}" for sk in sorted(subs.keys())
            if isinstance(subs[sk], dict)
        )
        parts.append(f"章{ch_id}: {ch.get('overview', '')[:100]}" + (f"（{sub_part}）" if sub_part else ""))
    book_summary = "\n".join(parts)[:4000]
    flags_desc = "\n".join(f"{f['id']}: [{f['chapter']}] {f.get('char', '')}「{f['text']}」" for f in flags)

    try:
        client = _create_writer_client()
        # 调用不覆盖 max_tokens/timeout——完全继承 config writer_model 全套配置
        resp = client.chat_detailed(
            [
                {"role": "system", "content": "你是严谨的小说承诺兑现审核员，输出严格 JSON。"},
                {"role": "user", "content": JUDGE_PROMPT.format(flags=flags_desc, book_summary=book_summary)},
            ],
            temperature=0.2,
        )
    except Exception as e:
        print(f"[全文承诺] 推理 client 不可用（{e}），回退关键词判定")
        return _keyword_fallback(state, flags, chapters_dir)

    content = resp.get("content") or ""
    arr = _parse_json_array(content)
    if arr is None:
        print("[全文承诺] 推理输出不可解析，回退关键词判定")
        return _keyword_fallback(state, flags, chapters_dir)

    status_map = {it.get("id"): it for it in arr if isinstance(it, dict)}
    issues = []
    for fl in flags:
        r = status_map.get(fl["id"]) or {}
        st = r.get("status", "")
        ev = str(r.get("evidence") or "")[:100]
        print(f"[全文承诺] {fl['id']} [{fl['chapter']}] {fl.get('text')[:20]}... → {st}")
        if st in ("未兑现", "悬停"):
            issues.append({
                "file": fl.get("chapter", ""),
                "problem": f"[全文承诺] {fl.get('char', '')}「{fl['text']}」{st}：{ev}",
                "position": fl.get("chapter", ""),
                "severity": "SOFT",
                "sub": fl.get("sub", ""),
                "source_text": fl.get("source_text", ""),
                "suggestion": "检查该承诺是否在后续章节回收，或补充收束",
            })
    return issues
