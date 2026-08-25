#!/usr/bin/env python3
"""前置规范效果实验台 — 入口

按方法论（08a/08b/08c/09b）提供 8 种前置规范方式，用户选一种或多种，
对输入真实执行（LLM 真填空），观测填入了什么、重试次数、撑满失败、重现性。

用法：
  python main.py                                       # 启动 Web UI（默认 LM Studio）
  python main.py --backend ollama                      # Ollama 后端
  python main.py --backend lm-studio                   # LM Studio 后端
  python main.py --backend custom --base-url http://x:8000/v1 --model xxx
  python main.py --port 8790                           # 指定端口
  python main.py --check                               # 仅检测后端连接
  python main.py --batch input.json output.json        # 批处理模式
"""
import os
import sys
import argparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ======================================================================
# 【配置推动】默认值在 silprespec_emulator/config_manager.py DEFAULT_CONFIG
#              CLI 参数 > config.json > DEFAULT_CONFIG
# ======================================================================


def build_parser():
    p = argparse.ArgumentParser(
        description="silprespec-emulator · 前置规范效果模拟器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--web", action="store_true", help="启动 Web UI（默认行为）")
    p.add_argument("--port", type=str, default="8805",
                   help="Web UI 端口（默认 8805，auto=自动分配空闲端口）")
    p.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    p.add_argument("--pidfile", default="", help="PID 文件路径（setup.bat 用）")
    p.add_argument("--check", action="store_true", help="仅检测后端连接，不进入对话")

    p.add_argument("--batch", nargs=2, metavar=("INPUT", "OUTPUT"), default=None,
                   help="批处理模式: --batch input.json output.json")
    p.add_argument("--jsonl", action="store_true",
                   help="JSONL 管道模式: stdin 逐行读，stdout 逐行输出")

    backend = p.add_argument_group("后端选择（配置推动：不传则用 config.json）")
    backend.add_argument("--backend", default=None,
                         choices=["lm-studio", "ollama", "custom"],
                         help="LLM 后端（覆盖 config）")
    api = p.add_argument_group("API 后端参数")
    api.add_argument("--base-url", default="", help="API 地址（覆盖 config）")
    api.add_argument("--api-key", default="", help="API Key")
    api.add_argument("--model", "-m", default="", help="模型名称")
    return p


def make_llm(args):
    """根据参数创建 LLM 客户端（配置推动：CLI 覆盖 config）"""
    from silprespec_emulator.llm_client import LLMClient, LLMClientError
    from silprespec_emulator.config_manager import ConfigManager

    cfg = ConfigManager()
    if args.backend:
        cfg.set("llm.backend", args.backend)
    if args.base_url:
        cfg.set("llm.base_url", args.base_url)
    if args.model:
        cfg.set("llm.model", args.model)
    if args.api_key:
        cfg.set("llm.api_key", args.api_key)

    backend = cfg.get("llm.backend", "lm-studio")
    base_url = cfg.resolve_base_url()
    model = cfg.get("llm.model", "")

    if backend == "custom" and not base_url:
        print("[ERROR] custom 模式需要 --base-url")
        sys.exit(1)

    llm = LLMClient(backend=backend, base_url=base_url, model=model,
                    api_key=cfg.get("llm.api_key", "not-needed"))
    ok, msg = llm.test_connection()
    if not ok:
        print(f"[FAIL] [{backend}] 连接失败: {msg}")
        print(f"       地址: {base_url}")
        if backend == "lm-studio":
            print("       请启动 LM Studio 并加载模型")
        elif backend == "ollama":
            print("       请运行: ollama serve")
        sys.exit(1)
    print(f"  [OK] [{backend}] {msg}")
    return llm


def run_batch(input_path, output_path, llm):
    """批处理模式：读 input.json（含 pipeline + 输入），写 output.json（5 线程产出）"""
    import json
    from silprespec_emulator.simulator import ExperimentRunner
    from silprespec_emulator.pipeline_model import Experiment

    with open(input_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    runner = ExperimentRunner(llm=llm)
    exp = spec.get("experiment", {})
    user_input = spec.get("input", "")
    parallel = spec.get("parallel", 5)

    result = runner.run(exp, user_input, parallel=parallel)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[OK] 批处理完成 → {output_path}（{len(result.get('runs', []))} 并行）")


def main():
    args = build_parser().parse_args()

    print("=" * 56)
    print("  silprespec-emulator · 前置规范效果模拟器")
    print("=" * 56)
    print()

    if args.check:
        make_llm(args)
        return

    if args.batch:
        llm = make_llm(args)
        run_batch(args.batch[0], args.batch[1], llm)
        return

    # 默认启动 Web UI
    from silprespec_emulator.web_ui import run_server
    port = args.port
    if port == "auto":
        import socket
        with socket.socket() as s:
            s.bind(("", 0))
            port = str(s.getsockname()[1])
    run_server(host=args.host, port=int(port), backend=args.backend,
               base_url=args.base_url, model=args.model,
               api_key=args.api_key, pidfile=args.pidfile)


if __name__ == "__main__":
    main()