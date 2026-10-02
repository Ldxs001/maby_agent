#!/usr/bin/env python3
"""
Model Env Check — R1/3B 推理包环境探测与自动安装。

模仿 rag-assistant rag_env_setup 模式：setup.bat 启动阶段探测 transformers/torch，
缺失则从镜像源自动 pip install——防止"模型在 UI 下载了（权重就绪）但缺运行包跑不了"。

R1（DeepSeek-R1-Distill-Qwen-1.5B）与 3B（Qwen2.5-3B-Instruct）实际 import：
- transformers（AutoModelForCausalLM / AutoTokenizer / pipeline）
- torch（模型推理）
- accelerate（可选，单卡小模型不需要）

用法:
  python -m structured_writer.novel.model_env_check           # 探测 + 自动安装缺失
  python -m structured_writer.novel.model_env_check --check   # 只探测不安装（返回码 1=有缺失）
"""
import subprocess
import sys
from pathlib import Path

REQUIRED_PACKAGES = ["transformers", "torch"]
OPTIONAL_PACKAGES = {"accelerate": "accelerate"}

MIRRORS = {
    "default": "https://mirrors.aliyun.com/pypi/simple/",
}


def get_python_path():
    return sys.executable


def list_installed():
    """列出已安装包（包名小写+连字符标准化，匹配 pip list 的 _ / - 混用）"""
    try:
        result = subprocess.run(
            [get_python_path(), "-m", "pip", "list", "--format=columns"],
            capture_output=True, text=True, timeout=30,
        )
        pkgs = set()
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[2:]:
                parts = line.split()
                if len(parts) >= 2:
                    pkgs.add(parts[0].lower().replace("_", "-"))
        return pkgs
    except (subprocess.TimeoutExpired, OSError):
        return set()


def check_missing():
    """返回 (必需包缺失列表, 可选包缺失列表)"""
    installed = list_installed()
    required_missing = [p for p in REQUIRED_PACKAGES if p not in installed]
    optional_missing = [p for p in OPTIONAL_PACKAGES if p not in installed]
    return required_missing, optional_missing


def _pip_run(args, timeout=900):
    """pip 子进程执行；返回 (ok, err)"""
    try:
        result = subprocess.run(
            [get_python_path(), "-m", "pip"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0, (result.stderr or result.stdout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def install_missing(required_missing, optional_missing, mirror="default"):
    """镜像源安装缺失包；返回全部成功与否"""
    mirror_url = MIRRORS.get(mirror, mirror)
    host = mirror_url.split("/")[2] if "//" in mirror_url else "pypi.org"
    ok_all = True
    for pkg in required_missing + optional_missing:
        print(f"[env-check] installing {pkg} ({host}, may take minutes)...", end="", flush=True)
        ok, err = _pip_run([
            "install", pkg,
            "--index-url", mirror_url,
            "--trusted-host", host,
            "--timeout", "120",
        ])
        if ok:
            print(" OK")
        else:
            print(" FAIL")
            print(f"    {err.strip()[-250:]}")
            ok_all = False
    return ok_all


def check_lmstudio() -> dict:
    """LM Studio 环境探测（判定模型 8B/7B 的宿主）：lms 可用性 + server 可达性。

    返回 {"available": bool, "server_ok": bool, "reason": str}。
    LM Studio 不可用 → 统一管理不可勾，判定模型固定 transformers 3B+1.5B。
    """
    try:
        # 双兼容导入：包方式（python -m 运行）与顶层方式
        try:
            from .lmstudio_probe import probe_lmstudio
        except ImportError:
            from lmstudio_probe import probe_lmstudio
        p = probe_lmstudio(force=True)
        reason = p.get("reason", "")
        if p.get("lms_ok") and not p.get("server_ok"):
            reason += "（引擎未响应，需 lms server start）"
        return {"available": bool(p.get("lms_ok")),
                "server_ok": bool(p.get("server_ok")), "reason": reason}
    except Exception as e:
        return {"available": False, "server_ok": False, "reason": f"探测异常: {e}"}


def main():
    check_only = "--check" in sys.argv
    required_missing, optional_missing = check_missing()
    lm = check_lmstudio()

    if not required_missing and not optional_missing:
        print("[env-check] R1/3B packages ready (transformers + torch)")
    else:
        if required_missing:
            print(f"[env-check] missing required: {', '.join(required_missing)} (R1/3B cannot run)")
        if optional_missing:
            print(f"[env-check] missing optional: {', '.join(optional_missing)} (not needed for single-GPU small models, skipped)")
        if check_only:
            print("[env-check] --check mode: detect only, no install")
            return 1
        print("[env-check] auto-installing missing packages (aliyun mirror)...")
        ok = install_missing(required_missing, optional_missing)
        # 装完复查必需包
        required_missing2, _ = check_missing()
        if required_missing2:
            print(f"[env-check] still missing: {required_missing2} (install manually)")
            return 1

    # LM Studio 环境（B 条件的前提——判定模型 8B/7B 的宿主）
    if lm["available"]:
        print(f"[env-check] LM Studio: available ({lm['reason']}) — 统一管理勾选可用，判定模型可切换 8B+7B")
    else:
        print(f"[env-check] LM Studio: unavailable ({lm['reason']}) — 判定模型固定 transformers 3B+1.5B")
    print("[env-check] required packages ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
