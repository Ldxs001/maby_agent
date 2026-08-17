#!/usr/bin/env python3
"""
model_backend.py — 判定模型统一后端管理（lmstudio / transformers 双后端路由）。

设计（用户定稿，2026-08 重构）：
  B = 用户勾选"统一管理"（写作规划与审查判定统一走 LM Studio）
  审核判定后端 = B → lmstudio（4维: Qwen3-8B Q4_K_M / R1: DeepSeek-R1-Distill-Qwen-7B Q4，
                          lms load 进 GPU → HTTP localhost:1234 生成）
               否则 → transformers（4维: Qwen2.5-3B / R1: DeepSeek-R1-Distill-Qwen-1.5B，CPU）

勾选（B）是用户决策；判定模型跟着 B 走，其余一律 3B+1.5B。
实体/行为提取永远走 transformers Qwen2.5-3B（CPU），不经本模块。

llama.cpp 直挂分支（llama-cpp-python 0.3.34）已整体废弃——旧内核无 MoE 优化导致
35B 写作 8 t/s 而 LM Studio 20+ t/s。判定模型（8B/7B）改走 LM Studio：
  lms load <key> --gpu max → HTTP 生成 → 测完 lms unload（--ttl 兜底自动卸）。

缺失场景通用化：8B/7B 不在 LM Studio 模型库时，由 web_ui「安装缺失模型」触发
hf_hub_download 下载进模型库（lmstudio_probe.gguf_target_dir()），再 lms import 注册。
"""
import gc
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

# 双兼容导入：包方式（web_ui 内）与顶层方式（4dim/reasoning 的 sys.path + from model_backend）
try:
    from . import lmstudio_probe
except ImportError:
    import lmstudio_probe

# 默认 LM Studio API（与 config 的 lmstudio backend 同源）
LMS_API_URL = "http://127.0.0.1:1234"


def is_llamacpp_range() -> bool:
    """兼容遗留调用：llama.cpp 已废弃，恒 False（选项不再出现）。"""
    return False


def detect_llamacpp() -> bool:
    """兼容遗留调用：llama.cpp 已废弃，恒 False。"""
    return False


def judge_backend(cfg: dict | None = None) -> str:
    """审核判定后端 = B（勾选统一管理）→ lmstudio（8B/7B 走 LM Studio GPU）；
    否则 transformers（3B/1.5B CPU）。不再依赖 Python 版本/llama-cpp-python。"""
    cfg = cfg or {}
    if bool(cfg.get("unified_management")):
        return "lmstudio"
    return "transformers"


def _model_profile(model_cfg: dict) -> dict:
    """按后端取配置槽（profiles 分槽结构，用户需求：切后端自动恢复对应落盘配置）。

    新结构：{"backend": "lmstudio", "profiles": {"lmstudio": {...}, "ollama": {...}, "llama.cpp": {...}}}
    旧格式（无 profiles）→ 直接返回顶层（兼容迁移前配置）。
    """
    model_cfg = model_cfg or {}
    profiles = model_cfg.get("profiles") or {}
    if profiles:
        cur = model_cfg.get("backend") or "lmstudio"
        return profiles.get(cur) or {}
    return model_cfg


def list_gguf_models(model_dir) -> list[str]:
    """扫描目录下的 *.gguf 文件（LM Studio 模型库扫描用）。目录不存在/空 → []。"""
    try:
        d = Path(model_dir)
        if not d.is_dir():
            return []
        return sorted(str(p) for p in d.rglob("*.gguf"))
    except Exception:
        return []


# ── LM Studio 模型库解析 ─────────────────────────────────────────────

def _lms_list_keys(timeout: int = 30) -> list[str]:
    """lms ls 输出解析为模型 key 列表（lms load 可直接使用的标识）。失败 → []。

    key 形态兼容两种：user/repo（HF 下载，如 qwen/qwen3.5-35b-a3b）与裸名
    （本地 GGUF 移入，如 qwen3-8b / deepseek-r1-distill-qwen-7b）——都接受。
    """
    ok, out = lmstudio_probe._lms_run(["ls"], timeout=timeout)
    if not ok or not out:
        return []
    keys = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # 跳过段头/表头/统计行（LLM / EMBEDDING / PARAMS / You have / Local / 分割线）
        low = ln.lower()
        if low.startswith(("llm", "embedding", "params", "you have", "local", "total")):
            continue
        tok = ln.split()
        if not tok:
            continue
        key = tok[0].strip()
        # 跳过明显非 key 的行（首列是数字/时间/空）
        if not key or key[0].isdigit():
            continue
        if key not in keys:
            keys.append(key)
    return keys


def judge_model_keys(cfg: dict | None = None) -> dict:
    """从 LM Studio 模型库解析 8B/7B 的 lms key：{4dim, r1}。

    匹配规则（文件名级关键词，覆盖 HF 官方名与本地特化名）：
      4dim: 名含 qwen3 且含 8b（Qwen3-8B）
      r1:   名含 r1 且含 7b，或名含 deepseek 且含 7b（R1-Distill-Qwen-7B）
    缺失 → 空串（调用方提示走「安装缺失模型」下载，见 ensure_judge_models）。
    """
    cfg = cfg or {}
    found = {"4dim": (cfg.get("lm_key_4dim") or ""),
             "r1": (cfg.get("lm_key_r1") or "")}
    if found["4dim"] and found["r1"]:
        return found
    for key in _lms_list_keys():
        n = key.lower()
        if not found["4dim"] and "qwen3" in n and "8b" in n:
            found["4dim"] = key
        elif not found["r1"] and (("r1" in n and "7b" in n) or ("deepseek" in n and "7b" in n)):
            found["r1"] = key
    return found


def ensure_judge_models(cfg: dict | None = None) -> dict:
    """探查 8B/7B 就绪状态（含缺失检测，供 UI/加载前提示）。返回：
    {ok, keys, missing: [key名...], reason}
    missing 非空 = 模型不在库（走 web_ui 下载，不在这里下载——下载是用户显式动作）。
    """
    cfg = cfg or {}
    keys = judge_model_keys(cfg)
    missing = [label for label, k in keys.items() if not k]
    reason = ""
    if missing:
        reason = "模型库缺少: " + ", ".join(
            "4维 Qwen3-8B" if m == "4dim" else "R1 7B" for m in missing)
    return {"ok": not missing, "keys": keys, "missing": missing, "reason": reason}


# ── LM Studio 生成（HTTP）───────────────────────────────────────────

def lms_generate(model_key: str, prompt: str, max_tokens: int = 512,
                 temperature: float = 0.2, stop: list | None = None,
                 ctx: int | None = None, timeout: int = 600) -> str:
    """LM Studio HTTP 生成（OpenAI 兼容 /v1/chat/completions）。

    调用方负责先 lms_load（本函数只发请求）。失败 → print + 返回 ""（调用方回退）。
    ctx 传给 lms load 用——生成本身只发 prompt。
    """
    if not model_key:
        return ""
    payload = {
        "model": model_key,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
    }
    if stop:
        payload["stop"] = list(stop)
    req = urllib.request.Request(
        f"{LMS_API_URL}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except urllib.error.HTTPError as e:
        print(f"[lmstudio] HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
    except Exception as e:
        print(f"[lmstudio] 生成失败: {e}")
    return ""


def make_lms_handle(model_key: str, ctx: int | None = None, ttl: int | None = 120):
    """LM Studio 生成句柄（可调用，兼容 generate() 统一调用面）。

    句柄带 _lms_model_key 标记 → release() 识别后 lms unload（测完即卸）；
    生成前确保 lms load（--gpu max，幂等：已加载则快速返回）。
    """
    if not model_key:
        return None
    ok, msg = lmstudio_probe.lms_load(model_key, gpu="max", ctx=ctx, ttl=ttl)
    if not ok:
        print(f"[lmstudio] lms load 失败（{model_key}）: {msg[:120]}")
        return None

    def _gen(prompt: str, max_tokens: int = 512, temperature: float = 0.2,
             stop: list | None = None) -> str:
        return lms_generate(model_key, prompt, max_tokens, temperature, stop, ctx)

    _gen._lms_model_key = model_key
    return _gen


def release(handle) -> None:
    """释放模型句柄：lms 句柄 → lms unload（测完即卸，显存让给下一模型）；
    transformers 句柄（tuple）→ del + gc。llama.cpp 已废弃，无 Llama 实例分支。"""
    if handle is None:
        return
    key = getattr(handle, "_lms_model_key", None)
    if key:
        lmstudio_probe.lms_unload(key, timeout=60)
        print(f"[lmstudio] 已卸载判定模型: {key}")
        return
    try:
        del handle
    except Exception:
        pass
    gc.collect()


def generate(handle, prompt: str, max_tokens: int = 512, temperature: float = 0.2,
             stop: list | None = None) -> str:
    """统一生成接口，两种后端共用一个调用面。

    - transformers 句柄 = (model, tokenizer)：chatml 模板 + generate + decode
    - lmstudio 句柄 = make_lms_handle 闭包（可调用）：HTTP 生成
    """
    if handle is None:
        return ""
    if isinstance(handle, tuple) and len(handle) == 2:
        model, tok = handle
        import torch
        chatml = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
        inputs = tok(chatml, return_tensors="pt")
        with torch.no_grad():
            out = model.generate(
                inputs["input_ids"],
                max_new_tokens=max_tokens,
                do_sample=temperature > 0,
                temperature=temperature or 0.2,
                pad_token_id=tok.eos_token_id,
            )
        return tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    # lmstudio 句柄（可调用）
    try:
        return handle(prompt, max_tokens=max_tokens, temperature=temperature, stop=stop) or ""
    except Exception as e:
        print(f"[model-backend] 生成失败: {e}")
        return ""


# ── 下载目标（LM Studio 模型库）──────────────────────────────────────

def default_gguf_dir() -> Path:
    """判定模型 GGUF 下载/存放目录 = LM Studio 模型根目录（探查得出）。

    替代旧 data/models/gguf——8B/7B 必须进 LM Studio 模型库才能 lms load。
    探查失败 → 抛错（调用方捕获提示安装/检查 LM Studio）。
    """
    return lmstudio_probe.gguf_target_dir()


def judge_gguf_paths(cfg: dict | None = None) -> dict:
    """兼容遗留名：判定模型库就绪状态（旧返回路径，新返回 lms key）。
    返回 {4dim, r1}，值为 lms model key（非磁盘路径）；缺失空串。"""
    return judge_model_keys(cfg)


if __name__ == "__main__":
    p = lmstudio_probe.probe_lmstudio(force=True)
    print(f"LM Studio: {p['reason']} | models_dir={p['models_dir']}")
    print(f"判定后端: {judge_backend()}")
    print(f"模型库 keys: {judge_model_keys()}")
    print(f"就绪: {ensure_judge_models()}")
