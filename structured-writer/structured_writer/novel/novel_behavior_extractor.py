#!/usr/bin/env python3
"""
Behavior Extractor — 角色行为提取器（write-sub 逐段，LLM 优先 + 正则回退）。

与 entity_extractor 同构的降级语义：
- LLM（Qwen2.5-3B）可用 → 语义提取角色行为（动作/决定/对话）
- LLM 缺失/失败 → 正则兜底（action_kws 关键词 + 切句），不丢数据

写入 novel_state.json 的 chapters[].behavior_summary（逐段增量合并）：
  behavior_summary[角色名] = ["行为1", "行为2", ...]（每角色上限 5 条）

v1.0 架构（方案 B：从 finalize 整章正则 → write-sub 逐段 LLM+正则）：
- 调用点：CLI write-sub 步骤 5 / Web _write_sub_inline
- 输出：增量合并，同章重复段不覆盖已有点
- 非阻断：任何异常都不影响写正文
"""
import json
import os
import re
import sys
from pathlib import Path

# 复用 entity_extractor 的模型加载（Qwen2.5-3B 懒加载 + data/models 优先）与统一生成后端
# （统一管理勾选 → 8B LM Studio；未勾选 → 3B transformers——流程不变，仍一子结构一次）
sys.path.insert(0, str(Path(__file__).parent))
from novel_entity_extractor import _load_extract_model, _llm_generate  # noqa: E402

# 行为提取 max tokens（行为列表比实体更短，1024 足够）
BEHAVIOR_MAX_TOKENS = 1024

# 每角色行为条数上限（与 finalize 版 _generate_behavior_summary 一致）
MAX_BEHAVIORS_PER_CHAR = 5

# ── 正则兜底：动作关键词（沿用 finalize 版 _generate_behavior_summary 的规则） ──
ACTION_KWS = ["把", "将", "用", "对", "给", "从", "在", "说", "问", "答",
              "打", "踢", "走", "跑", "跳", "拿", "放", "看", "听", "吃",
              "买", "卖", "修", "装", "拆", "调查", "决定", "发现", "开始"]


def _extract_behavior_llm(content: str, char_names: list) -> dict | None:
    """提取角色行为：{角色名: [行为...]}；失败返回 None。
    统一后端：统一管理勾选 → 8B LM Studio；未勾选 → Qwen2.5-3B transformers。"""
    chars = "、".join(char_names) if char_names else "（未知）"
    prompt = (
        "你是小说角色行为提取引擎。从正文中提取每个角色的核心行为（做了什么/说了什么/决定什么），"
        "只输出一个 JSON 对象，不要任何解释、不要思考过程、不要 markdown 围栏。\n"
        "JSON 格式：\n"
        '{"behaviors": {"角色名": ["行为1", "行为2"]}}\n'
        "要求：\n"
        f"- 角色名必须来自已知角色列表：{chars}\n"
        "- 每条行为是简短动作描述（不超过 15 字），如\"启动防火墙\"、\"交出芯片\"\n"
        "- 只提取有意义的行动，不提取心理独白/环境描写\n"
        "- 每个角色最多 3 条；无行为的角色可以省略\n"
        "输出示例：\n"
        '{"behaviors": {"林渊": ["启动防火墙", "把芯片交给苏婉"], "苏婉": ["带着芯片离开"]}}\n'
        f"正文：\n{content}"
    )
    last_raw = ""
    for attempt in range(1, 4):
        try:
            raw = _llm_generate(prompt) or ""
            last_raw = raw
            obj = _extract_behavior_json(raw)
            if obj is not None and isinstance(obj.get("behaviors"), dict):
                return obj["behaviors"]
            prompt = (
                "你上一次的输出不是合法的 JSON（必须是 {\"behaviors\": {\"角色名\": [\"行为\"]}} 结构）。\n\n"
                f"你上一次的输出：\n---\n{last_raw[:500]}\n---\n\n"
                "请忽略上一次输出，重新严格输出同一段正文的角色行为 JSON，不要任何解释：\n"
                '{"behaviors": {"角色名": ["行为1"]}}'
            )
        except Exception as e:
            print(f"  [behavior-extract] [WARN] 行为提取异常（第{attempt}次，非阻断）: {e}")
            if attempt == 3:
                return None
            continue
    print("  [behavior-extract] [WARN] 模型 3 次输出均无法解析，行为提取跳过")
    return None


def _extract_behavior_json(text: str) -> dict | None:
    """从模型输出提取 JSON（剥围栏/思考/前后废话）"""
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    cleaned = re.sub(r"<\|?think\|?>.*?<\|?/\s*think\|?>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\b(NaN|Infinity|-Infinity)\b", "null", cleaned)
    start = cleaned.find("{")
    if start < 0:
        return None
    end = cleaned.rfind("}")
    if end < 0:
        end = len(cleaned)
    candidate = cleaned[start:end + 1]
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for e in range(end, start, -1):
        trial = cleaned[start:e]
        for depth in range(1, 5):
            try:
                obj = json.loads(trial + "}" * depth)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _extract_behavior_regex(content: str, char_names: list) -> dict:
    """正则兜底：切句 + 动作关键词匹配 + 截取（沿用 finalize 版规则）"""
    char_actions = {}
    for name in char_names:
        if name not in content:
            continue
        sentences = re.split(r"[。！？\n]", content)
        for sent in sentences:
            if name not in sent:
                continue
            sent = sent.strip()
            if len(sent) < 4:
                continue
            if not any(kw in sent for kw in ACTION_KWS):
                continue
            idx = sent.index(name)
            action = sent[idx:idx + 25]
            action = action.replace(name, "", 1)[:25]
            action = action.strip("，。；：！？、 ")
            if not action or len(action) < 2:
                continue
            if name not in char_actions:
                char_actions[name] = []
            if action not in char_actions[name]:
                char_actions[name].append(action)
    for name in char_actions:
        char_actions[name] = char_actions[name][:MAX_BEHAVIORS_PER_CHAR]
    return char_actions


def _merge_behaviors(existing: dict, new: dict) -> dict:
    """逐段增量合并：新行为去重追加到已有角色名下，每角色上限 MAX_BEHAVIORS_PER_CHAR。"""
    merged = dict(existing or {})
    for name, actions in (new or {}).items():
        cur = list(merged.get(name, []))
        for a in actions:
            if a not in cur:
                cur.append(a)
        merged[name] = cur[:MAX_BEHAVIORS_PER_CHAR]
    return merged


def extract_behavior(state_path: str, chapter: str, sub_key: str, content: str) -> dict:
    """write-sub 逐段行为提取：LLM 优先 + 正则回退，增量合并进 chapters[].behavior_summary。

    非阻断：任何异常返回空 dict，不干扰写正文流程。
    返回本次新增的行为 dict（供日志）。
    """
    sp = Path(state_path)
    if not sp.exists() or not content.strip():
        return {}
    try:
        data = json.loads(sp.read_text(encoding="utf-8-sig"))
        char_names = [c.get("name", "") for c in data.get("characters", []) if c.get("name")]
        if not char_names:
            return {}
        # 1. LLM 优先
        new_actions = _extract_behavior_llm(content, char_names)
        if new_actions is None:
            # 2. 正则回退
            new_actions = _extract_behavior_regex(content, char_names)
            if new_actions:
                print("  [behavior-extract] ⚠️ LLM 不可用，已用正则兜底提取角色行为")
        if not new_actions:
            return {}
        # 3. 定位当前章，增量合并
        ch_data = next((c for c in data.get("chapters", []) if c["id"] == chapter), None)
        if ch_data is None:
            return {}
        old_summary = ch_data.get("behavior_summary", {}) or {}
        merged = _merge_behaviors(old_summary, new_actions)
        ch_data["behavior_summary"] = merged
        # 原子写入（直接写，绕过指纹——behavior_summary 是运行时字段）
        tmp = sp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(sp)
        total = sum(len(v) for v in merged.values())
        print(f"  [behavior-extract] {chapter}{sub_key}: 合并后 {len(merged)} 角色, {total} 条行为")
        return new_actions
    except Exception as e:
        print(f"  [behavior-extract] [WARN] 提取失败（非阻断）: {e}")
        return {}


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("用法: python novel_behavior_extractor.py <state_path> <chapter> <sub_key> <content_file>")
        sys.exit(1)
    state_path, chapter, sub_key, content_src = sys.argv[1:5]
    if content_src == "-":
        content = sys.stdin.read()
    else:
        content = Path(content_src).read_text(encoding="utf-8-sig")
    extract_behavior(state_path, chapter, sub_key, content)
