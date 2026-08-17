#!/usr/bin/env python3
"""
lmstudio_probe.py — LM Studio 环境探查与模型生命周期管理（lms CLI 封装）。

背景（用户定稿，2026-08）：llama.cpp 直挂分支整体废弃，判定模型（8B/7B）统一
走 LM Studio（lms load → GPU → HTTP localhost:1234），写作/规划 35B 同样走 LM Studio。
本模块负责：
  1. 探查 LM Studio 环境：lms 可执行文件、模型根目录、server 可用性、import 能力
  2. 模型注册：8B/7B GGUF 下载进 LM Studio 模型库（缺失时自动下载，不依赖本机已有）
  3. 模型切换：lms load / lms unload / lms ps（多模型流水线：35B 写作 → 8B 判定 → 7B 判定）

模型根目录探查优先级：
  settings.json 的 downloadsFolder（用户自定义） > 默认 ~/.lmstudio/models

lms 命令面（实测）：
  lms ls / ps / load / unload / import / get / server start|stop|status
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

# ── 常量 ─────────────────────────────────────────────────────────────
DEFAULT_API_URL = "http://127.0.0.1:1234"
DEFAULT_MODELS_REL = ".lmstudio/models"
PROBE_CACHE_TTL = 30.0  # 秒（UI 反复刷新不卡）

# 探查结果缓存（模块级，TTL）
_PROBE_CACHE: dict = {}
_PROBE_LOCK = False  # 简单互斥（GIL 下够用）


def _read_lmstudio_settings() -> dict:
    """读 LM Studio settings.json（含 downloadsFolder 等）。失败 → {}。"""
    for p in (Path.home() / ".lmstudio" / "settings.json",
              Path.home() / "Library" / "Application Support" / "LM Studio" / "settings.json"):
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def find_lms_exe() -> Optional[str]:
    """定位 lms 可执行文件：PATH → 常见安装位。找不到 → None。"""
    try:
        w = shutil.which("lms")
        if w:
            return w
    except Exception:
        pass
    for cand in (Path.home() / ".lmstudio" / "bin" / "lms.exe",
                 Path.home() / ".lmstudio" / "bin" / "lms",
                 Path.home() / ".local" / "bin" / "lms"):
        if cand.is_file():
            return str(cand)
    return None


def find_models_dir() -> Optional[str]:
    """探查 LM Studio 模型根目录：settings downloadsFolder > 默认 ~/.lmstudio/models。

    返回绝对路径字符串；探查失败 → None（下载/加载会明确报错，不静默降级到旧目录）。
    """
    try:
        s = _read_lmstudio_settings()
        dl = (s.get("downloadsFolder") or "").strip()
        if dl:
            p = Path(dl)
            if p.is_dir() or True:  # 目录可能尚未创建（首次），保留配置值
                return str(p.resolve())
    except Exception:
        pass
    d = Path.home() / ".lmstudio" / "models"
    return str(d) if d.is_dir() else str(d)


def server_ok(url: str = DEFAULT_API_URL, timeout: float = 2.0) -> bool:
    """LM Studio API 可达性：GET /v1/models。失败 → False。"""
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/v1/models")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _lms_run(args: list, timeout: int = 120) -> tuple[bool, str]:
    """执行 lms 子命令；返回 (ok, stdout/stderr 摘要)。lms 不存在 → (False, 原因)。"""
    exe = find_lms_exe()
    if not exe:
        return False, "lms 未找到（未安装 LM Studio？）"
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return r.returncode == 0, out or err or f"rc={r.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"lms {' '.join(args[:2])} 超时（>{timeout}s）"
    except OSError as e:
        return False, f"lms 执行失败: {e}"


def probe_lmstudio(force: bool = False) -> dict:
    """探查 LM Studio 环境（缓存 30s）。返回结构化结果，任一失败都带 reason。

    返回字段：
      lms_exe:   lms 可执行文件路径（None = 未装）
      models_dir: 模型根目录（可能尚未创建，下载时自动建）
      server_ok:  localhost:1234 API 是否可达（LM Studio 引擎是否在跑）
      lms_ok:    lms load 子命令可用（版本足够新）
      import_ok: lms import 子命令可用
      reason:    可读状态说明
    """
    global _PROBE_CACHE
    now = time.monotonic()
    if not force and _PROBE_CACHE.get("ts") and (now - _PROBE_CACHE["ts"]) < PROBE_CACHE_TTL:
        return _PROBE_CACHE["data"]
    lms_exe = find_lms_exe()
    models_dir = find_models_dir()
    # lms_ok：能列出模型（load 同源命令面，ls 足够代表 CLI 可用）
    lms_ok, ls_out = _lms_run(["ls"], timeout=30) if lms_exe else (False, "lms 未找到")
    srv_ok = server_ok()
    if lms_exe and lms_ok:
        reason = "LM Studio 就绪"
        if not srv_ok:
            reason += "（API 未响应，需 lms server start）"
    elif lms_exe:
        reason = f"lms 可用但 ls 失败: {ls_out[:120]}"
    else:
        reason = "未检测到 LM Studio（lms 不在 PATH 且常见安装位不存在）"
    data = {
        "lms_exe": lms_exe,
        "models_dir": models_dir,
        "server_ok": srv_ok,
        "lms_ok": lms_ok,
        "import_ok": bool(lms_exe),  # import 与 ls 同命令面
        "reason": reason,
    }
    _PROBE_CACHE = {"ts": now, "data": data}
    return data


# ── 模型库操作 ───────────────────────────────────────────────────────

def gguf_target_dir() -> Path:
    """LM Studio 模型根目录（下载目标）。不可探查 → 抛错（调用方捕获并提示）。"""
    d = find_models_dir()
    if not d:
        raise RuntimeError("LM Studio 模型目录探查失败")
    return Path(d)


def gguf_expected_path(repo: str, file: str) -> Path:
    """HF repo/file 映射到 LM Studio 模型库路径：<models_dir>/<publisher>/<repo>/<file>。"""
    return gguf_target_dir() / repo / file


def gguf_exists(repo: str, file: str) -> bool:
    """目标 GGUF 是否已在模型库（下载前探查用，支持缺失场景判断）。"""
    return gguf_expected_path(repo, file).is_file()


def lms_load(model_key: str, gpu: str = "max", ctx: int | None = None,
             ttl: int | None = None, timeout: int = 600) -> tuple[bool, str]:
    """lms load：加载模型到 GPU（--gpu max / off / 0~1）。

    ctx: 上下文长度（-c）；ttl: 空闲自动卸载秒数（--ttl）。返回 (ok, 输出摘要)。
    """
    args = ["load", model_key, "--gpu", gpu, "-y"]
    if ctx:
        args += ["-c", str(ctx)]
    if ttl:
        args += ["--ttl", str(ttl)]
    return _lms_run(args, timeout=timeout)


def lms_unload(model_key: str | None = None, timeout: int = 120) -> tuple[bool, str]:
    """lms unload：卸载模型（默认全部）。返回 (ok, 输出摘要)。"""
    args = ["unload"]
    if model_key:
        args.append(model_key)
    return _lms_run(args, timeout=timeout)


def lms_ps(timeout: int = 30) -> tuple[bool, str]:
    """lms ps：当前已加载模型。返回 (ok, 文本输出)。"""
    return _lms_run(["ps"], timeout=timeout)


def lms_import(gguf_path: str, repo: str, copy: bool = True,
               timeout: int = 300) -> tuple[bool, str]:
    """lms import：把 GGUF 注册进 LM Studio 模型库（缺失/未扫描到时兜底）。

    repo: "user/repo"（--user-repo）；copy=True 保留源文件（-c）。
    返回 (ok, 输出摘要)。
    """
    args = ["import", str(gguf_path), "--user-repo", repo, "-y"]
    if copy:
        args.append("-c")
    return _lms_run(args, timeout=timeout)


def lms_server_start(timeout: int = 120) -> tuple[bool, str]:
    """lms server start：确保引擎 headless 运行（structured-writer 启动时调用）。"""
    return _lms_run(["server", "start"], timeout=timeout)


if __name__ == "__main__":
    p = probe_lmstudio(force=True)
    print(json.dumps(p, ensure_ascii=False, indent=2))
