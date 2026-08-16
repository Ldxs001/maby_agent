#!/usr/bin/env python3
"""
Entity Extractor — 实体关系提取器（LLM 驱动版）。

从子结构正文中抽取实体-关系三元组，增量更新 entity_tracker。
写入 write-sub 管道的第 4 步（非阻断，仅 INFO/WARN）。

v2.3.7b0 架构（替代纯正则版）：
- 抽取：Qwen2.5-3B-Instruct（transformers 本地，非思考，CPU 可跑）——LLM 语义抽取实体/关系/状态
- 归并：精确名 → 子串（防包裹式命名重复建档）
- 注册：characters（含别名）强制注册为 character 实体，杜绝人物缺失
- 清洗：历史正则时代碎片（标点残留/超短名）幂等过滤
- 模型走 transformers 本地（data/models 优先 → HF 缓存回退），不走 LM Studio；失败非阻断

数据模型：
  entity_tracker.entities[i] = {
    "id": "ent_001",
    "type": "character|object|location|organization|abstract",
    "name": "实用擒拿格斗术",
    "attributes": {"status": "active", ...},
    "first_chapter": "L01", "first_sub": "S01",
    "last_chapter": "L01", "last_sub": "S01"
  }
  entity_tracker.relations[i] = {
    "id": "rel_001",
    "from_entity": "ent_001",   # 实体 id（非角色名）
    "predicate": "贩卖",
    "to_entity": "ent_002",
    "detail": "30信用点",
    "chapter": "L01", "sub": "S01"
  }
"""
import json
import os
import re
import sys
from pathlib import Path

EXTRACT_MODEL_NAME = "Qwen/Qwen2.5-3B-Instruct"
# 实体+关系 JSON 通常几百 token；1024 足够且 3B CPU 生成快（4096 会让写段阻塞 1-3 分钟/段）
EXTRACT_MAX_TOKENS = 2048  # 上限而非消耗：模型写完 JSON 自动停（实测 213/472 token），调大零成本，防长篇多实体截断
# 碎片特征：含标点/百分号/括号等残留（正则时代的产物）
FRAGMENT_RE = re.compile(r"[，。；：！？、】》「」\"'…%【】()（）]")

# ── 正则兜底提取（LLM 缺失/失败时启用，来源：novel-weaver v1.21.5 正则版） ──
LOCATION_SUFFIX = ["巷", "街", "路", "区", "市", "城", "楼", "大厦", "厂", "院", "铺", "摊", "场"]
ORGANIZATION_SUFFIX = ["公司", "集团", "协会", "会", "组织", "联盟", "厂", "社", "所", "院", "局", "处"]
STATUS_CHANGE_TRIGGERS = {
    "摧毁": "destroyed", "损坏": "damaged", "烧毁": "destroyed",
    "修复": "repaired", "重建": "rebuilt", "关闭": "closed",
    "开启": "open", "建立": "active", "成立": "active",
    "出售": "sold", "购买": "owned", "送给": "given",
    "抢夺": "stolen", "丢失": "lost", "死亡": "dead",
    "负伤": "injured", "受伤": "injured", "昏迷": "unconscious",
    "苏醒": "active", "恢复": "active",
}
REL_PATTERNS = [
    (r"(把|将)\s*(.{1,8})\s*(交给|递给|卖给|送给|还给)", "转移"),
    (r"(在|位于|来到|前往|离开)\s*(.{1,8})(?:巷|街|路|区|市|楼|厂|铺|场)", "位于"),
    (r"是\s*.{0,4}(?:的|一位|一名)\s*(?:员工|成员|头目|领导|首领)", "归属"),
    (r"领导\s*.{1,8}(?:组织|团体|联盟|会)", "领导"),
    (r"装有|配备|携带|持有|拥有|带着\s*.{1,8}", "拥有"),
    (r"来自\s*.{1,8}(?:公司|集团|组织|协会)", "来自"),
]

_EXTRACT_PIPE = None  # text-generation pipeline（Qwen2.5-3B）


# ── 模型加载（照抄 novel_reasoning_check 模式） ──

def _find_local_model(model_id: str) -> str | None:
    """data/models/ 优先 → HF 默认缓存回退；返回 snapshot 目录，无则 None"""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from _path_utils import MODELS_DIR
        model_cache = str(MODELS_DIR)
    except Exception:
        model_cache = str(Path.home() / ".cache" / "huggingface" / "hub")
    hub_path = Path(model_cache) / ("models--" + model_id.replace("/", "--"))
    if hub_path.exists() and (hub_path / "snapshots").exists():
        return str(sorted((hub_path / "snapshots").iterdir())[-1])
    default_path = Path.home() / ".cache" / "huggingface" / "hub" / ("models--" + model_id.replace("/", "--"))
    if default_path.exists() and (default_path / "snapshots").exists():
        return str(sorted((default_path / "snapshots").iterdir())[-1])
    return None


def _load_extract_model():
    """懒加载 Qwen2.5-3B（AutoModelForCausalLM + AutoTokenizer，CPU）。
    失败/缺失 → None，绝不联网。返回 (model, tokenizer)。"""
    global _EXTRACT_PIPE
    if _EXTRACT_PIPE is not None:
        return _EXTRACT_PIPE
    try:
        # 强制 CPU，避免与 LM Studio 抢夺 GPU 显存；镜像源
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        model_path = _find_local_model(EXTRACT_MODEL_NAME)
        if model_path is None:
            print(f"[entity-extract] 实体抽取模型缺失，跳过（安装: HF_ENDPOINT=https://hf-mirror.com python -c \"from transformers import AutoModel; AutoModel.from_pretrained('{EXTRACT_MODEL_NAME}', trust_remote_code=True)\"）")
            _EXTRACT_PIPE = None
            return None
        from transformers import AutoModelForCausalLM, AutoTokenizer
        model = AutoModelForCausalLM.from_pretrained(model_path, trust_remote_code=True, local_files_only=True)
        tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True, local_files_only=True)
        _EXTRACT_PIPE = (model, tokenizer)
        print(f"[entity-extract] 抽取模型已加载 (Qwen2.5-3B, CPU): {model_path[:80]}")
    except ImportError:
        print("[entity-extract] 实体抽取模型不可用: 未安装 transformers/torch")
        _EXTRACT_PIPE = None
    except Exception as e:
        print(f"[entity-extract] 抽取模型加载失败（非阻断）: {e}")
        _EXTRACT_PIPE = None
    return _EXTRACT_PIPE


# ── 清洗 / 注册 ──

def _sanitize_legacy(entities: list) -> list:
    """幂等清洗历史碎片（正则时代产物）：仅删含标点残留的实体。

    设计原则（v2.3.25b0）：
    - 只做**确定性**清洗：名字里带标点（，。；：！？等）必然不是合法实体名
    - 不做**伪语义**判断：不按长度删（单字可能是合法名"渊"），"是否像名字"是语义判断，硬编码规则无权审判
    - character 类型永不删（角色来自 characters 权威源，硬编码规则无权删除）
    """
    keep = []
    for e in entities:
        name = (e.get("name") or "").strip()
        if e.get("type") == "character":
            keep.append(e)  # 角色权威数据，永不删
            continue
        if not name:
            continue
        if FRAGMENT_RE.search(name):
            continue
        keep.append(e)
    return keep


def _ensure_characters_registered(data: dict, entities: list) -> int:
    """characters（含别名）强制注册为 character 实体，返回新增数。

    v2.3.25b0：占位符精确挡截（无/未知/待定/None 等不注册）——与规划端
    _detect_new_chars_in_plan 的 PLACEHOLDER_NAMES 同源，双端一致，防脏角色再进表。
    """
    PLACEHOLDER_NAMES = {"无", "未知", "待定", "None", "暂无", "未定", "未命名"}
    known_names = {e.get("name") for e in entities}
    added = 0
    for c in data.get("characters", []):
        name = c.get("name", "")
        if not name:
            continue
        for cand in [name] + list(c.get("aliases") or []):
            if cand in PLACEHOLDER_NAMES:
                continue  # 占位符不注册（精确匹配，不误伤"无风/无面人"）
            if cand in known_names:
                continue
            entities.append({
                "id": _make_entity_id(entities),
                "type": "character",
                "name": cand,
                "attributes": {"role": c.get("role", "")},
                "first_chapter": "", "first_sub": "",
                "last_chapter": "", "last_sub": "",
            })
            known_names.add(cand)
            added += 1
    return added


# ── LLM 抽取 ──

def _extract_json(text: str) -> dict | None:
    """从模型输出中提取 JSON（剥思考/围栏/前后废话），失败返回 None。

    v2.3.22b0 增强：
    - NaN/Infinity/-Infinity 替换为 null（模型偶发输出非标准 JSON 数字）
    - 截断修复：JSON 不完整时（找最大合法前缀），尝试补全闭合括号
    """
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    # 去掉思考标记（若有）
    cleaned = re.sub(r"<\|?think\|?>.*?<\|?/\s*think\|?>", "", cleaned, flags=re.DOTALL)
    # 非标准 JSON 数字 → null（模型偶发输出）
    cleaned = re.sub(r"\b(NaN|Infinity|-Infinity)\b", "null", cleaned)
    # 取第一个 {
    start = cleaned.find("{")
    if start < 0:
        return None
    # 截断修复：从最后往前找可解析的 JSON 边界（模型可能被 max_new_tokens 截断，
    # 表现为缺失闭合括号；这里取最大合法前缀 + 补全闭合）
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
    # 最大合法前缀：从末尾逐步缩进，找第一个可解析的完整对象
    for e in range(end, start, -1):
        trial = cleaned[start:e]
        # 补全缺失的闭合括号
        for depth in range(1, 5):
            t2 = trial + "}" * depth
            try:
                obj = json.loads(t2)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _extract_llm(content: str, char_hint: str) -> dict | None:
    """Qwen2.5-3B 抽取 {entities, relations, status_changes}；失败返回 None。

    v2.3.22b0：few-shot 示例（治本）+ 打回重试最多 3 次（偶发格式漂移兜底）+ WARN 打印原始输出。
    """
    loaded = _load_extract_model()
    if loaded is None:
        return None
    model, tokenizer = loaded
    prompt = (
        "你是小说实体关系抽取引擎。从正文中抽取有意义的实体和关系，只输出一个 JSON 对象，"
        "不要任何解释、不要思考过程、不要 markdown 围栏。\n"
        "JSON 格式：\n"
        '{"entities": [{"name": "正文原词", "type": "character|object|location|organization|abstract", "status": "active|dead|injured|unconscious|destroyed|damaged|closed"}], '
        '"relations": [{"from": "实体名", "predicate": "简短谓词", "to": "实体名"}]}\n'
        "要求：\n"
        "- 人物优先（与已知角色匹配或明显是人名）标 type=character\n"
        "- 只抽有意义的实体（人物/关键事物/概念/地点），不要碎片短语、不要单字\n"
        "- 正文有状态变化（死亡/昏迷/摧毁/修复/苏醒等）才写 status，否则省略\n"
        "- 关系无则 relations 为空数组\n"
        "输出示例：\n"
        '{"entities": [{"name": "陈默", "type": "character"}, {"name": "防火墙", "type": "object", "status": "damaged"}], '
        '"relations": [{"from": "陈默", "predicate": "关闭", "to": "防火墙"}]}\n'
        f"已知角色：{char_hint or '无'}\n"
        f"正文：\n{content}"
    )
    import torch
    last_raw = ""
    for attempt in range(1, 4):  # 最多 3 次（第 1 次原始 prompt，第 2-3 次带纠错提示）
        try:
            # 走 ChatML（Qwen2 系标准格式） + model.generate：
            # 不用 pipeline（generation_config 混传导致 max_length=20 生效/BPE 后处理清空输出，实测空输出 288s）
            # 不用 apply_chat_template（本地 Qwen2.5-3B-Instruct 的 tokenizer.chat_template 缺失）
            chatml = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
            model_inputs = tokenizer(chatml, return_tensors="pt")
            with torch.no_grad():
                gen_out = model.generate(
                    model_inputs["input_ids"],
                    max_new_tokens=EXTRACT_MAX_TOKENS,
                    do_sample=False,
                    temperature=0.2,
                    pad_token_id=tokenizer.eos_token_id,
                )
            raw = tokenizer.decode(gen_out[0][model_inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            last_raw = raw
            result = _extract_json(raw)
            if result is not None:
                if attempt > 1:
                    print(f"  [entity-extract] ✅ 第{attempt}次纠正后解析成功")
                return result
            print(f"  [entity-extract] ⚠️ 第{attempt}次输出格式不规范，打回纠正重试...")
            if attempt == 1:
                print(f"    [原始输出前200字] {raw[:200]!r}")
            prompt = (
                "你上一次的输出不是合法的 JSON 对象（必须是 {\"entities\": [...], \"relations\": [...]} 结构）。\n\n"
                "你上一次的输出：\n"
                f"---\n{last_raw[:500]}\n---\n\n"
                "请忽略上一次输出，重新严格按以下格式输出同一段正文的实体关系，仅输出 JSON 对象，不要任何解释：\n"
                '{"entities": [{"name": "实体名", "type": "character|object|location|organization|abstract", "status": "..."}], '
                '"relations": [{"from": "实体名", "predicate": "谓词", "to": "实体名"}]}'
            )
        except Exception as e:
            print(f"  [entity-extract] [WARN] 抽取调用异常（第{attempt}次，非阻断）: {e}")
            if attempt == 3:
                return None
            continue
    print("  [entity-extract] [WARN] 模型 3 次输出均无法解析为 JSON，本轮抽取跳过")
    return None


# ── 正则兜底（LLM 缺失/失败时启用，来源：novel-weaver v1.21.5 正则版改编） ──

def _regex_classify_type(name: str) -> str:
    """根据名称猜测实体类型（后缀匹配）"""
    if any(name.endswith(s) for s in LOCATION_SUFFIX):
        return "location"
    if any(name.endswith(s) for s in ORGANIZATION_SUFFIX):
        return "organization"
    if re.match(r"^[\d.]+%?$", name):
        return "abstract"
    return "object"


def _regex_extract_noun_entities(text: str) -> list:
    """从正文中提取疑似实体的名词短语（引号内容/地点后缀/组织后缀/介词后名词）"""
    candidates = set()
    for m in re.finditer(r'[「《"\'][^」》"\']{2,8}[」》"\']', text):
        candidates.add(m.group()[1:-1])
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}(?:巷|街|路|区|市|楼|厂|铺|场|院|社|所)", text):
        candidates.add(m.group())
    for m in re.finditer(r"[\u4e00-\u9fff]{2,8}(?:公司|集团|协会|联盟|组织)", text):
        candidates.add(m.group())
    for m in re.finditer(r"(?<=[的把在被和与到对从给向关于])([\u4e00-\u9fff]{2,6})(?=[，。；：！？\s])", text):
        candidates.add(m.group(1))
    return list(candidates)


def _regex_extract_status_changes(text: str) -> list:
    """检测正文中的状态变更关键词，返回 [(实体名, 新状态, 触发词)]"""
    changes = []
    for trigger_word, new_status in STATUS_CHANGE_TRIGGERS.items():
        if trigger_word in text:
            pattern = rf"(.{{1,8}}){trigger_word}"
            for m in re.finditer(pattern, text):
                target = m.group(1).strip()
                target = re.sub(r"^[的把将被由从给向对与和了]", "", target)
                target = re.sub(r"[的，。；：！？、\s]+$", "", target)
                if target and 2 <= len(target) <= 8:
                    changes.append((target, new_status, trigger_word))
    return changes


def _extract_regex(content: str, char_hint: str) -> dict | None:
    """正则兜底：从正文提取实体关系（schema 与 _extract_llm 一致，供同一归并管线消费）。

    返回 {"entities": [{"name","type","status"}], "relations": [{"from","predicate","to"}]}。
    LLM 缺失/失败时调用——不丢数据（降级优于跳过）。
    """
    known_chars = {c for c in char_hint.split("，") if c} if char_hint else set()
    entities = []
    entities_by_name = {}

    def _add_entity(name, etype, status=None):
        name = name.strip()
        if not name or len(name) < 2:
            return
        if name in entities_by_name:
            if status:
                entities_by_name[name]["status"] = status
            return
        ent = {"name": name, "type": etype}
        if status:
            ent["status"] = status
        entities.append(ent)
        entities_by_name[name] = ent

    # 1. 状态变更
    for target_name, new_status, trigger in _regex_extract_status_changes(content):
        # 目标若是已知角色 → character，否则按后缀猜类型
        etype = "character" if target_name in known_chars else _regex_classify_type(target_name)
        _add_entity(target_name, etype, new_status)
    # 2. 名词候选
    for cand in _regex_extract_noun_entities(content):
        etype = "character" if cand in known_chars else _regex_classify_type(cand)
        _add_entity(cand, etype)
    # 3. 关系（模式匹配，两端取实体名）
    relations = []
    for pattern, default_pred in REL_PATTERNS:
        for m in re.finditer(pattern, content):
            matched = []
            for name in entities_by_name:
                if name in m.group():
                    matched.append(name)
            for c in known_chars:
                if c in m.group() and c not in matched:
                    _add_entity(c, "character")
                    matched.append(c)
            if len(matched) >= 2:
                relations.append({"from": matched[0], "predicate": default_pred, "to": matched[1]})
    if not entities and not relations:
        return None
    return {"entities": entities, "relations": relations}


# ── 归并 ──

def _make_entity_id(existing: list) -> str:
    max_id = 0
    for e in existing:
        m = re.match(r"ent_(\d+)", e.get("id", ""))
        if m:
            max_id = max(max_id, int(m.group(1)))
    return f"ent_{max_id + 1:03d}"


def _make_relation_id(existing: list) -> str:
    max_id = 0
    for r in existing:
        m = re.match(r"rel_(\d+)", r.get("id", ""))
        if m:
            max_id = max(max_id, int(m.group(1)))
    return f"rel_{max_id + 1:03d}"


def _merge_or_create(new_ent: dict, existing: list) -> str:
    """归并策略：精确名 → 子串；否则新建。返回实体 id。（bge 相似度档已移除）"""
    new_name = new_ent.get("name", "").strip()
    if not new_name:
        return ""
    # 1. 精确名
    for e in existing:
        if e.get("name") == new_name:
            return e["id"]
    # 2. 子串（防包裹式命名重复：一方包含另一方）
    for e in existing:
        en = e.get("name", "")
        if en and (new_name in en or en in new_name) and (len(en) >= 2 or len(new_name) >= 2):
            return e["id"]
    # 新建
    eid = _make_entity_id(existing)
    existing.append({
        "id": eid,
        "type": new_ent.get("type", "object"),
        "name": new_name,
        "attributes": {"status": new_ent["status"]} if new_ent.get("status") else {},
        "first_chapter": new_ent.get("first_chapter", ""),
        "first_sub": new_ent.get("first_sub", ""),
        "last_chapter": new_ent.get("last_chapter", ""),
        "last_sub": new_ent.get("last_sub", ""),
    })
    return eid


# ── 主入口 ──

def extract(state_path: str, chapter: str, sub_key: str, content: str, force_status: bool = False):
    """从子结构正文 LLM 抽取实体关系，增量合并 entity_tracker。失败非阻断。"""
    sp = Path(state_path)
    if not sp.exists():
        print(f"[entity-extract] state_path 不存在: {state_path}")
        return

    data = json.loads(sp.read_text(encoding="utf-8-sig"))
    tracker = data.setdefault("entity_tracker", {"entities": [], "relations": []})
    entities = tracker.get("entities", [])
    relations = tracker.get("relations", [])

    # 1. 历史碎片清洗（幂等）
    before = len(entities)
    entities = _sanitize_legacy(entities)
    if len(entities) != before:
        print(f"  [entity-extract] 清洗历史碎片: {before} → {len(entities)} 实体")

    # 2. characters（含别名）强制注册
    added = _ensure_characters_registered(data, entities)
    if added:
        print(f"  [entity-extract] 角色注册: 新增 {added} 个 character 实体")

    # 3. LLM 抽取
    char_hint = "，".join(c.get("name", "") for c in data.get("characters", []) if c.get("name"))
    result = _extract_llm(content, char_hint)

    if result is None:
        # LLM 缺失/失败 → 正则兜底（降级优于跳过：不丢数据）
        result = _extract_regex(content, char_hint)
        if result is not None:
            print("  [entity-extract] ⚠️ LLM 抽取不可用，已用正则兜底提取实体关系")

    if result is None:
        # 正则兜底也空（正文过短/无实体）→ 保存清洗/注册结果，非阻断
        tracker["entities"] = entities
        tracker["relations"] = relations
        data["entity_tracker"] = tracker
        sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  [entity-extract] 本轮抽取跳过（模型缺失且正则无命中），实体表已清洗/注册")
        return

    new_ents = result.get("entities", []) or []
    new_rels = result.get("relations", []) or []

    # 4. 归并（精确名 → 子串；bge 相似度档已移除）
    new_count = 0
    for ne in new_ents:
        ne["first_chapter"] = chapter
        ne["first_sub"] = sub_key
        ne["last_chapter"] = chapter
        ne["last_sub"] = sub_key
        eid = _merge_or_create(ne, entities)
        if eid:
            # 已合并 → 更新 last 出现
            for e in entities:
                if e["id"] == eid:
                    e["last_chapter"] = chapter
                    e["last_sub"] = sub_key
                    if ne.get("status"):
                        if force_status:
                            # force（重构后同步）：状态以新正文为准，覆盖式刷新
                            e.setdefault("attributes", {})["status"] = ne["status"]
                        elif not e.get("attributes", {}).get("status"):
                            e.setdefault("attributes", {})["status"] = ne["status"]
                    break
        else:
            new_count += 1

    # 5. 关系写入（from/to 名 → 实体 id）
    name_to_id = {e.get("name"): e["id"] for e in entities if e.get("name")}
    relation_count = 0
    for nr in new_rels:
        f = (nr.get("from") or "").strip()
        t = (nr.get("to") or "").strip()
        fid, tid = name_to_id.get(f), name_to_id.get(t)
        if not fid or not tid or fid == tid:
            continue
        # 去重（同谓词同两端已存在）
        dup = any(r.get("from_entity") == fid and r.get("to_entity") == tid and r.get("predicate") == nr.get("predicate")
                  for r in relations)
        if dup:
            continue
        relations.append({
            "id": _make_relation_id(relations),
            "from_entity": fid,
            "predicate": (nr.get("predicate") or "").strip(),
            "to_entity": tid,
            "detail": "",
            "chapter": chapter, "sub": sub_key,
        })
        relation_count += 1

    # 6. 持久化
    tracker["entities"] = entities
    tracker["relations"] = relations
    data["entity_tracker"] = tracker
    sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    parts = []
    if new_count:
        parts.append(f"新增 {new_count} 实体")
    if relation_count:
        parts.append(f"新增 {relation_count} 关系")
    if parts:
        print(f"  [entity-extract] 总结: {'; '.join(parts)}")
    else:
        print(f"  [entity-extract] [OK] 无新实体或变更")
    print(f"  [entity-extract] 当前: {len(entities)} 实体, {len(relations)} 关系")


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("用法: python novel_entity_extractor.py <state_path> <chapter> <sub_key> <content_file>")
        print("  content_file: 子结构内容文件路径（如 chapters/L01/S01.txt）")
        print("  或传 - 从 stdin 读取")
        sys.exit(1)

    state_path = sys.argv[1]
    chapter = sys.argv[2]
    sub_key = sys.argv[3]
    content_src = sys.argv[4]

    if content_src == "-":
        content = sys.stdin.read()
    else:
        content = Path(content_src).read_text(encoding="utf-8-sig")

    if not content.strip():
        print("[entity-extract] [WARN] 内容为空，跳过提取")
        sys.exit(0)

    extract(state_path, chapter, sub_key, content)
