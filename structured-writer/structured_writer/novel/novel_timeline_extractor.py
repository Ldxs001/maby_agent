#!/usr/bin/env python3
"""
Timeline Extractor — 故事内时间线提取器（write-sub 逐段，LLM 优先 + 正则回退）。

与 entity/behavior 提取器同构的降级语义：
- LLM（Qwen2.5-3B）可用 → 语义提取时间事件（时间推进 + 该时间发生的事）
- LLM 缺失/失败 → 正则兜底（第X天/翌日/三天后等时间词 + 所在句子），不丢数据

写入 novel_state.json 的 timeline[]（逐段增量合并）：
  {"time_point": "第2天", "event": "林渊在实验室启动防火墙", "day": 2,
   "chapter": "L01", "sub": "S01"}

设计说明（接活历史空壳 timeline）：
- 之前 timeline 只有手动 CLI add-timeline，Web 流水线零调用 → 永远 []
- 本提取器在 write-sub 后自动逐段登记，时间线才真正活起来
- day 字段供 novel_logic_check._check_timeline_logic 检测时间倒序
- 非阻断：任何异常都不影响写正文
"""
import json
import os
import re
import sys
from pathlib import Path

# 复用 entity_extractor 的模型加载（Qwen2.5-3B 懒加载 + data/models 优先）
sys.path.insert(0, str(Path(__file__).parent))
from novel_entity_extractor import _load_extract_model  # noqa: E402

TIMELINE_MAX_TOKENS = 1024

# ── 正则兜底：时间词模式（含 相对时间 → 天 的换算） ──
TIME_PATTERNS = [
    (r"第([一二三四五六七八九十百千\d]+)天", "day"),      # 第X天
    (r"第([一二三四五六七八九十百千\d]+)日", "day"),      # 第X日
    (r"翌日|第二天|次日", "day+1"),                        # 翌日/第二天/次日
    (r"第三天", "day+2"),                                 # 第三天
    (r"([一二三四五六七八九十百\d]+)天后", "day+N"),       # N天后
    (r"([一二三四五六七八九十百\d]+)周后", "week+N"),      # N周后
    (r"清晨|早晨|黎明", "timeofday"),                     # 清晨
    (r"深夜|午夜|半夜", "timeofday"),                     # 深夜
    (r"傍晚|黄昏|黄昏时分", "timeofday"),                 # 傍晚
]

# 中文数字 → 阿拉伯数字（day 换算用）
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}


def _cn_to_int(s: str) -> int:
    """中文数字 → int（支持一到百千，简单组合）"""
    if s.isdigit():
        return int(s)
    total = 0
    cur = 0
    for ch in s:
        if ch in _CN_NUM:
            v = _CN_NUM[ch]
            if v >= 10:
                total += (cur or 1) * v
                cur = 0
            else:
                cur = v
    return total + cur


def _resolve_day(time_point: str, base_day: int) -> int | None:
    """把 time_point 解析成绝对 day（基于当前 base_day），无法解析返回 None。"""
    for pat, kind in TIME_PATTERNS:
        m = re.search(pat, time_point)
        if not m:
            continue
        if kind == "day":
            return _cn_to_int(m.group(1))
        if kind == "day+1":
            return base_day + 1
        if kind == "day+2":
            return base_day + 2
        if kind == "day+N":
            return base_day + _cn_to_int(m.group(1))
        if kind == "week+N":
            return base_day + _cn_to_int(m.group(1)) * 7
        if kind == "timeofday":
            return base_day  # 同一天的不同时段
    return None


def _extract_timeline_llm(content: str, prev_timeline: list) -> list | None:
    """Qwen2.5-3B 提取时间事件列表；失败返回 None。"""
    loaded = _load_extract_model()
    if loaded is None:
        return None
    model, tokenizer = loaded
    prev_summary = ""
    if prev_timeline:
        prev_summary = "；".join(
            f"{t.get('time_point','?')}: {t.get('event','')[:40]}" for t in prev_timeline[-5:]
        )
    prompt = (
        "你是小说时间线提取引擎。从正文中提取故事内的时间事件（时间推进 + 该时间发生的关键事），"
        "只输出一个 JSON 数组，不要任何解释、不要思考过程、不要 markdown 围栏。\n"
        "JSON 格式：\n"
        '[{"time_point": "第2天", "event": "林渊在实验室启动防火墙"}]\n'
        "要求：\n"
        "- time_point 用故事内时间表述（第N天/次日/三天后/清晨/深夜等）\n"
        "- event 是 10-30 字的事件描述（谁做了什么）\n"
        "- 只提取有时间推进或关键转折的事件；纯日常无推进 → 空数组\n"
        f"- 已记录的时间线（勿重复）：{prev_summary or '无'}\n"
        "输出示例：\n"
        '[{"time_point": "次日清晨", "event": "苏婉带着芯片离开实验楼"}]\n'
        f"正文：\n{content}"
    )
    import torch
    last_raw = ""
    for attempt in range(1, 4):
        try:
            chatml = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
            model_inputs = tokenizer(chatml, return_tensors="pt")
            with torch.no_grad():
                gen_out = model.generate(
                    model_inputs["input_ids"],
                    max_new_tokens=TIMELINE_MAX_TOKENS,
                    do_sample=False,
                    temperature=0.2,
                    pad_token_id=tokenizer.eos_token_id,
                )
            raw = tokenizer.decode(gen_out[0][model_inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            last_raw = raw
            obj = _extract_timeline_json(raw)
            if obj is not None:
                return obj
            prompt = (
                "你上一次的输出不是合法的 JSON 数组（必须是 [{\"time_point\": \"...\", \"event\": \"...\"}] 结构）。\n\n"
                f"你上一次的输出：\n---\n{last_raw[:500]}\n---\n\n"
                "请忽略上一次输出，重新严格输出同一段正文的时间事件 JSON 数组，不要任何解释。"
            )
        except Exception as e:
            print(f"  [timeline-extract] [WARN] 时间线提取异常（第{attempt}次，非阻断）: {e}")
            if attempt == 3:
                return None
            continue
    print("  [timeline-extract] [WARN] 模型 3 次输出均无法解析，时间线提取跳过")
    return None


def _extract_timeline_json(text: str) -> list | None:
    """从模型输出提取 JSON 数组（剥围栏/思考/前后废话）"""
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    cleaned = re.sub(r"<\|?think\|?>.*?<\|?/\s*think\|?>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\b(NaN|Infinity|-Infinity)\b", "null", cleaned)
    start = cleaned.find("[")
    if start < 0:
        return None
    end = cleaned.rfind("]")
    if end < 0:
        end = len(cleaned)
    candidate = cleaned[start:end + 1]
    try:
        obj = json.loads(candidate)
        if isinstance(obj, list):
            return obj
    except json.JSONDecodeError:
        pass
    for e in range(end, start, -1):
        trial = cleaned[start:e]
        try:
            obj = json.loads(trial + "]")
            if isinstance(obj, list):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _extract_timeline_regex(content: str, base_day: int) -> list:
    """正则兜底：抓时间词 + 所在句子（截 40 字作事件）。无时间词 → 空。

    段内连续解析：相对时间（翌日/次日/N天后）基于段内已出现的最大 day，
    而非段外 base_day——保证同段"第一天→次日"正确推进为 day 1→2。
    """
    events = []
    local_max_day = base_day
    sentences = re.split(r"[。！？\n]", content)
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        tp_match = None
        for pat, kind in TIME_PATTERNS:
            m = re.search(pat, sent)
            if m:
                tp_match = m.group(0)
                break
        if not tp_match:
            continue
        # 事件 = 句子主体（去时间词前缀，截 40 字）
        event = re.sub(r"^[，,、\s]*", "", sent)
        event = event[:40]
        if len(event) < 6:
            continue
        # day 解析：绝对时间直接用；相对时间基于 local_max_day（段内游标）
        day = _resolve_day(tp_match, local_max_day)
        if day is not None:
            if day > local_max_day:
                local_max_day = day  # 推进段内游标
        entry = {"time_point": tp_match, "event": event}
        if day is not None:
            entry["day"] = day
        events.append(entry)
    # 去重（同 time_point + event 前缀相同）
    seen = set()
    dedup = []
    for e in events:
        key = (e.get("time_point", ""), e.get("event", "")[:10])
        if key not in seen:
            seen.add(key)
            dedup.append(e)
    return dedup


def _merge_timeline(existing: list, new: list) -> list:
    """增量合并：同 chapter+sub 或同 time_point+event 前缀不重复追加。"""
    merged = list(existing or [])
    existing_keys = set()
    for t in merged:
        existing_keys.add((t.get("chapter", ""), t.get("sub", ""), t.get("event", "")[:10]))
    added = 0
    for e in new or []:
        key = (e.get("chapter", ""), e.get("sub", ""), (e.get("event") or "")[:10])
        if key in existing_keys:
            continue
        existing_keys.add(key)
        merged.append(e)
        added += 1
    return merged, added


def extract_timeline(state_path: str, chapter: str, sub_key: str, content: str) -> list:
    """write-sub 逐段时间线提取：LLM 优先 + 正则回退，增量合并进 state.timeline。

    非阻断：任何异常返回空 list，不干扰写正文流程。
    返回本次新增的事件 list（供日志）。
    """
    sp = Path(state_path)
    if not sp.exists() or not content.strip():
        return []
    try:
        data = json.loads(sp.read_text(encoding="utf-8-sig"))
        prev_timeline = data.get("timeline", []) or []
        # 当前故事 day（取已有 timeline 最大 day）
        base_day = max((t.get("day") or 0 for t in prev_timeline), default=0)
        # 1. LLM 优先
        new_events = _extract_timeline_llm(content, prev_timeline)
        if new_events is None:
            # 2. 正则回退
            new_events = _extract_timeline_regex(content, base_day)
            if new_events:
                print("  [timeline-extract] ⚠️ LLM 不可用，已用正则兜底提取时间事件")
        if not new_events:
            return []
        # 归一化：补 chapter/sub，解析 day（LLM 输出的 day 可能 0/缺失/倒序 → 正则重解析 + 段内游标推进）
        local_max_day = base_day
        for e in new_events:
            e["chapter"] = chapter
            e["sub"] = sub_key
            parsed_day = _resolve_day(e.get("time_point", ""), local_max_day)
            if parsed_day is not None:
                e["day"] = parsed_day
                if parsed_day > local_max_day:
                    local_max_day = parsed_day
            elif "day" not in e:
                e["day"] = local_max_day  # 无时间词可解析 → 沿用当前 day（同天事件）
        merged, added = _merge_timeline(prev_timeline, new_events)
        if added:
            data["timeline"] = merged
            tmp = sp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(sp)
            print(f"  [timeline-extract] {chapter}{sub_key}: 新增 {added} 条时间事件（累计 {len(merged)}）")
        return new_events
    except Exception as e:
        print(f"  [timeline-extract] [WARN] 提取失败（非阻断）: {e}")
        return []


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("用法: python novel_timeline_extractor.py <state_path> <chapter> <sub_key> <content_file>")
        sys.exit(1)
    state_path, chapter, sub_key, content_src = sys.argv[1:5]
    if content_src == "-":
        content = sys.stdin.read()
    else:
        content = Path(content_src).read_text(encoding="utf-8-sig")
    extract_timeline(state_path, chapter, sub_key, content)
