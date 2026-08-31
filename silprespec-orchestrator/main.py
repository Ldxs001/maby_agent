"""main.py — silprespec-orchestrator CLI 入口

功能：
  1. 根据用户任务+工具集，从 14 种穷举组合里选最合适的前置规范
  2. PY 确定性组合，LLM 填空执行，输出给工具（智能体）走内部流程
  3. 多 agent 协同的头部规划，也可插入任意阶段的前置规范中

用法：
  python main.py --web                         # 启动 Web UI（默认端口 8789）
  python main.py --query "分析这份报告"         # 单次任务
  python main.py --backend ollama              # 切换 Ollama 后端
  python main.py --check                       # 检测 LLM 连接
"""

import os
import sys
import json
import argparse

# ======================================================================
# 配置区
# ======================================================================
CFG_BACKEND = "lm-studio"
CFG_BASE_URL = "http://localhost:1234"
CFG_MODEL = "qwen/qwen3.6-35b-a3b"
CFG_API_KEY = "not-needed"
CFG_MAX_TOKENS = 4096
CFG_TEMPERATURE = 0.7
CFG_TIMEOUT = 180

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")


# ======================================================================
# 配置加载
# ======================================================================
def load_config(path=""):
    p = path or _CONFIG_PATH
    cfg = {}
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    return cfg


def save_config(cfg, path=""):
    p = path or _CONFIG_PATH
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存配置失败: {e}")


# ======================================================================
# CLI 参数
# ======================================================================
def build_parser():
    p = argparse.ArgumentParser(
        description="silprespec-orchestrator — 前置规范编排器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--check", action="store_true", help="仅检测 LLM 连接")
    p.add_argument("--web", action="store_true", help="启动 Web UI")
    p.add_argument("--port", type=str, default="8789", help="Web UI 端口（默认 8789）")
    p.add_argument("--query", type=str, default="", help="单次任务执行")
    p.add_argument("--verbose", action="store_true", help="打印详细过程")

    backend = p.add_argument_group("后端选择")
    backend.add_argument("--backend", default="", choices=["", "lm-studio", "ollama", "custom"])
    backend.add_argument("--base-url", default="")
    backend.add_argument("--api-key", default="")
    backend.add_argument("--model", default="")

    p.add_argument("--config", default="", help="配置文件路径")
    p.add_argument("--pidfile", default="", help="PID 文件路径（setup.bat 用）")
    p.add_argument("--list-combos", action="store_true", help="列出 14 种穷举组合")
    p.add_argument("--list-tools", action="store_true", help="列出已注册工具")
    return p


# ======================================================================
# LLM 工厂
# ======================================================================
def make_llm(cfg, args):
    llm_cfg = cfg.get("llm", {})
    backend = args.backend or llm_cfg.get("backend", CFG_BACKEND)
    base_url = args.base_url or llm_cfg.get("base_url", CFG_BASE_URL)
    model = args.model or llm_cfg.get("model", CFG_MODEL)
    api_key = args.api_key or llm_cfg.get("api_key", CFG_API_KEY)
    max_tokens = llm_cfg.get("max_tokens", CFG_MAX_TOKENS)
    temperature = llm_cfg.get("temperature", CFG_TEMPERATURE)
    timeout = llm_cfg.get("timeout", CFG_TIMEOUT)

    from silprespec_orchestrator.llm_client import LLMClient, LLMClientError
    llm = LLMClient(backend=backend, base_url=base_url, model=model,
                    api_key=api_key, timeout=timeout,
                    max_tokens=max_tokens, temperature=temperature)
    return llm


# ======================================================================
# 主入口
# ======================================================================
def main():
    args = build_parser().parse_args()
    cfg = load_config(args.config)

    if args.list_combos:
        from silprespec_orchestrator.combo_registry import list_combos
        for c in list_combos():
            print(f"  [{c.id:2d}] {c.name:30s} {c.desc}")
        return

    if args.list_tools:
        from silprespec_orchestrator.tool_registry import TOOL_REGISTRY
        for name, spec in TOOL_REGISTRY.items():
            print(f"  {name:20s} 输入:{spec.input_requirements} 输出:{spec.output_schema}")
        return

    llm = make_llm(cfg, args)

    if args.check:
        ok, msg = llm.test_connection()
        print(f"  {'✅' if ok else '❌'} {msg}")
        return

    if args.web:
        from silprespec_orchestrator.web_ui import run_web
        port = int(args.port) if args.port.isdigit() else 8789
        run_web(llm, cfg, port, pidfile=args.pidfile, config_path=_CONFIG_PATH)
        return

    if args.query:
        from silprespec_orchestrator import Orchestrator
        orch = Orchestrator(llm, cfg, verbose=args.verbose)
        result = orch.run(args.query)
        print(result)
        return

    build_parser().print_help()


if __name__ == "__main__":
    main()