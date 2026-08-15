#!/usr/bin/env python3
"""
Entity Extractor — 实体关系提取器（LLM 驱动版）。

从子结构正文中抽取实体-关系三元组，增量更新 entity_tracker。
写入 write-sub 管道的第 4 步（非阻断，仅 INFO/WARN）。

v2.3.7b0 架构（替代纯正则版）：
- 抽取：Qwen2.5-3B-Instruct（transformers 本地，非思考，CPU 可跑）——LLM 语义抽取实体/关系/状态
- 归并：bge-small-zh-v1.5 嵌入相似度（>0.85 合并，防同一实体多种写法重复建档）
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
BGE_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MERGE_SIM_THRESHOLD = 0.85
# 碎片特征：含标点/百分号/括号等残留（正则时代的产物）
FRAGMENT_RE = re.compile(r"[，。；：！？、】》「」\"'…%【】()（）]")

_EXTRACT_PIPE = None  # text-generation pipeline（Qwen2.5-3B）
_BGE_MODEL = None     # SentenceTransformer（bge-small-zh）


# ── 模型加载（照抄 novel_semantic_check / novel_reasoning_check 模式） ──

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


def _load_bge():
    """懒加载 bge-small-zh（实体归并嵌入，CPU）。缺失 → None。"""
    global _BGE_MODEL
    if _BGE_MODEL is not None:
        return _BGE_MODEL
    try:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        model_path = _find_local_model(BGE_MODEL_NAME)
        if model_path is None:
            print("[entity-extract] bge 归并模型缺失，跳过嵌入归并（仅名字/子串匹配）")
            _BGE_MODEL = None
            return None
        from sentence_transformers import SentenceTransformer
        _BGE_MODEL = SentenceTransformer(model_path)
    except ImportError:
        _BGE_MODEL = None
    except Exception as e:
        print(f"[entity-extract] bge 加载失败（非阻断）: {e}")
        _BGE_MODEL = None
    return _BGE_MODEL


# ── 清洗 / 注册 ──

def _sanitize_legacy(entities: list) -> list:
    """幂等清洗历史碎片（正则时代产物）：超短名 / 含标点残留 → 删除。"""
    keep = []
    for e in entities:
        name = (e.get("name") or "").strip()
        if len(name) < 2:
            continue
        if FRAGMENT_RE.search(name):
            continue
        keep.append(e)
    return keep


def _ensure_characters_registered(data: dict, entities: list) -> int:
    """characters（含别名）强制注册为 character 实体，返回新增数。"""
    known_names = {e.get("name") for e in entities}
    added = 0
    for c in data.get("characters", []):
        name = c.get("name", "")
        if not name:
            continue
        for cand in [name] + list(c.get("aliases") or []):
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
    """从模型输出中提取 JSON（剥思考/围栏/前后废话），失败返回 None。"""
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    # 去掉思考标记（若有）
    cleaned = re.sub(r"<\|?think\|?>.*?<\|?/\s*think\|?>", "", cleaned, flags=re.DOTALL)
    # 取第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_llm(content: str, char_hint: str) -> dict | None:
    """Qwen2.5-3B 抽取 {entities, relations, status_changes}；失败返回 None。"""
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
        f"已知角色：{char_hint or '无'}\n"
        f"正文：\n{content}"
    )
    try:
        # 走 ChatML（Qwen2 系标准格式） + model.generate：
        # 不用 pipeline（generation_config 混传导致 max_length=20 生效/BPE 后处理清空输出，实测空输出 288s）
        # 不用 apply_chat_template（本地 Qwen2.5-3B-Instruct 的 tokenizer.chat_template 缺失）
        import torch
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
        result = _extract_json(raw)
        if result is None:
            print("  [entity-extract] [WARN] 模型输出无法解析为 JSON，本轮抽取跳过")
            return None
        return result
    except Exception as e:
        print(f"  [entity-extract] [WARN] 抽取调用异常（非阻断）: {e}")
        return None


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


def _cosine(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _merge_or_create(new_ent: dict, existing: list, name_emb: dict, bge) -> str:
    """归并策略：精确名 → 子串 → bge 相似度>0.85 合并；否则新建。返回实体 id。"""
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
    # 3. bge 嵌入相似度
    if bge is not None and name_emb:
        nv = name_emb.get(new_name)
        if nv is not None:
            for e in existing:
                ev = name_emb.get(e.get("name"))
                if ev is not None and _cosine(nv, ev) > MERGE_SIM_THRESHOLD:
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

def extract(state_path: str, chapter: str, sub_key: str, content: str):
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
        # 抽取失败 → 保存清洗/注册结果，非阻断
        tracker["entities"] = entities
        tracker["relations"] = relations
        data["entity_tracker"] = tracker
        sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  [entity-extract] 本轮抽取跳过（模型缺失或失败），实体表已清洗/注册")
        return

    new_ents = result.get("entities", []) or []
    new_rels = result.get("relations", []) or []

    # 4. 归并（名字 + bge 相似度）
    bge = _load_bge()
    name_emb = {}
    if bge is not None:
        all_names = [e.get("name", "") for e in entities if e.get("name")] + \
                    [n.get("name", "") for n in new_ents if n.get("name")]
        all_names = list(dict.fromkeys(all_names))
        if all_names:
            try:
                vecs = bge.encode(all_names, normalize_embeddings=True)
                name_emb = dict(zip(all_names, [v.tolist() for v in vecs]))
            except Exception as e:
                print(f"  [entity-extract] bge 编码失败（降级名字匹配）: {e}")
                name_emb = {}

    new_count = 0
    for ne in new_ents:
        ne["first_chapter"] = chapter
        ne["first_sub"] = sub_key
        ne["last_chapter"] = chapter
        ne["last_sub"] = sub_key
        eid = _merge_or_create(ne, entities, name_emb, bge)
        if eid:
            # 已合并 → 更新 last 出现
            for e in entities:
                if e["id"] == eid:
                    e["last_chapter"] = chapter
                    e["last_sub"] = sub_key
                    if ne.get("status") and not e.get("attributes", {}).get("status"):
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
