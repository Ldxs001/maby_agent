#!/usr/bin/env python3
"""前置规范效果实验台 — 入口

按方法论（08a/08b/08c/09b）提供 5 种前置规范方式，用户选一种或多种，
对输入真实执行（LLM 真填空），观测填入了什么、重试次数、撑满失败、重现性 + 验证指标。

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
        description="silprespec-emulator · LLM 有限行为量化工具（前置规范效果模拟器）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--web", action="store_true", help="启动 Web UI（默认行为）")
    p.add_argument("--port", type=str, default="8805",
                   help="Web UI 端口（默认 8805，auto=自动分配空闲端口）")
    p.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    p.add_argument("--pidfile", default="", help="PID 文件路径（setup.bat 用）")
    p.add_argument("--check", action="store_true", help="仅检测后端连接，不进入对话")
    p.add_argument("--e2e", action="store_true", help="一键端到端演示：5 方式 × 预设输入 × 真实 LLM，输出完整原始信息")

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


def run_e2e_cli(llm):
    """命令行一键端到端演示：5 方式 × 预设输入 × 真实 LLM × 并行重现性"""
    import json
    from silprespec_emulator.e2e_demo import run_e2e_demo

    print("=" * 70)
    print("  一键端到端演示 · 5 方式 × 预设输入 × 真实 LLM × 并行重现性")
    print("=" * 70)

    def on_progress(done, total, res):
        print(f"\n  [{done}/{total}] 管道完成，已聚合 {len(res)} 方式")

    results = run_e2e_demo(llm, parallel=3, on_progress=on_progress)

    print("\n" + "=" * 70)
    print("  完整原始信息")
    print("=" * 70)
    for r in results:
        print(f"\n{'█'*70}\n  {r['way']} · {r['name']}\n{'█'*70}")
        print(f"  说明       : {r['desc']}")
        print(f"  输入       : {r['user_input']}")
        print(f"  task_prompt: {r['task_prompt']}")
        print(f"  recipe     : {json.dumps(r['recipe'], ensure_ascii=False)}")
        print(f"  config     : {json.dumps(r['config'], ensure_ascii=False)}")
        print(f"  max_retry  : {r['max_retry']}  并行: {r['parallel']}")
        print(f"  汇总       : success_all={r['success_all']}  总耗时={r['elapsed_all']}s  总tokens={r['total_tokens_all']}")
        rp = r.get("reproducibility", {})
        print(f"  重现性     : consistency={rp.get('consistency')}  不同填入={len(rp.get('distinct_fills',[]))} 种")
        for k, v in rp.get("fill_counts", {}).items():
            print(f"    [{v}次] {k}")
        for run in r.get("runs", []):
            print(f"\n  {'─'*60}")
            print(f"  run {run['run_id']}: success={run['success']}  retry={run['retry_count']}  exhausted={run['exhausted']}  耗时={run['elapsed_total']}s  tokens={run['total_tokens']}")
            print(f"  LLM 调用（{len(run.get('calls',[]))} 次）:")
            for i, c in enumerate(run.get('calls', []), 1):
                print(f"    [调用 {i}] 耗时 {c['elapsed']}s  prompt_tokens={c['prompt_tokens']}  response_tokens={c['response_tokens']}")
                if c['system_prompt']:
                    print(f"      system : {c['system_prompt']}")
                print(f"      prompt : {c['prompt']}")
                print(f"      返回   : {c['response']}")
            print(f"  attempt 记录（{len(run.get('attempts',[]))} 次，含重试理由）:")
            for a in run.get('attempts', []):
                print(f"    [attempt {a['attempt']}] valid={a['valid']}  重试理由={a.get('retry_reason','') or '(无)'}")
                print(f"      raw   : {(a.get('raw','') or '')[:200]}")
                print(f"      filled: {json.dumps(a.get('filled',{}), ensure_ascii=False)}")
                if a.get('fabricated'):
                    print(f"      fabricated: {json.dumps(a['fabricated'], ensure_ascii=False)}")
                if a.get('missing_required'):
                    print(f"      missing_required: {json.dumps(a['missing_required'], ensure_ascii=False)}")
                if a.get('flagged'):
                    print(f"      flagged: {json.dumps(a['flagged'], ensure_ascii=False)}")
            print(f"  最终 filled: {json.dumps(run.get('filled',{}), ensure_ascii=False)}")
            print(f"  观测 extra : {json.dumps(run.get('extra',{}), ensure_ascii=False)}")
            if run.get('error'):
                print(f"  error: {run['error']}")
    print(f"\n{'='*70}\n  端到端演示完成（{len(results)} 方式）\n{'='*70}")


def main():
    args = build_parser().parse_args()

    print("=" * 56)
    print("  silprespec-emulator · LLM 有限行为量化工具")
    print("  前置规范效果模拟器 · 5 方式 × 真实填空 × 量化观测")
    print("=" * 56)
    print()

    if args.check:
        make_llm(args)
        return

    if args.e2e:
        llm = make_llm(args)
        llm.timeout = 1200
        run_e2e_cli(llm)
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