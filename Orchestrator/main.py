"""
main.py — CLI 入口

功能:
  1. 多后端切换: LM Studio / Ollama / 自定义 API / 直接加载 GGUF
  2. 模型罗列: --list-models 显示所有可用模型
  3. 智能体: RAG + 文件 + 网络 + Python 执行，多工具协作

用法:
  python main.py                                       # 默认 (LM Studio)
  python main.py --list-models                         # 罗列所有模型
  python main.py --backend ollama                      # Ollama 后端
  python main.py --backend lm-studio                   # LM Studio 后端
  python main.py --backend custom --base-url http://x:8000/v1 --model xxx
  python main.py --direct                              # 直接加载 GGUF
  python main.py --direct --model 2                    # 选择列表中的第2个模型
  python main.py --no-rag                              # 不带 RAG 工具
"""

import os

# ======================================================================
# 【配置区】改这里，别的地方不用动
# ======================================================================
# 后端: "lm-studio" / "ollama" / "custom" / "direct" / "list-models"
CFG_BACKEND = "lm-studio"

# direct 模式：模型名称或序号，留空则运行时可选
CFG_MODEL = ""

# custom 模式
CFG_BASE_URL = ""
CFG_API_KEY = "not-needed"
CFG_CUSTOM_MODEL = ""


# 开关
CFG_VERBOSE = False
CFG_GPU_LAYERS = -1          # direct模式: -1=自动, 0=纯CPU
CFG_AUTO_INSTALL_GPU = True  # 自动装 GPU 版

# 技能路径（自包含优先，回退到用户全局技能目录）
# 把你想要智能体用的技能复制到这两个目录下
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
CFG_SKILL_DIRS = [
    os.path.join(_PARENT_DIR, "skills"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", "skills"),
]
# ======================================================================

import argparse
import json
import sys

if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from orchestrator.agent_config import AgentConfig
from orchestrator.llm_client import LLMClient
from orchestrator.tools.file_tool import ReadFileTool, WriteFileTool, ListDirTool
from orchestrator.tools.file_ops_tool import (
    CopyFileTool, MoveFileTool, DeleteFileTool,
    AppendFileTool, MakeDirTool, FindFilesTool,
)
from orchestrator.tools.web_tool import WebFetchTool, WebSearchTool, PythonExecuteTool
from orchestrator.tools.data_tool import DBQueryTool, ReadTableTool, ImageInfoTool
from orchestrator.tools.skill_loader import LoadSkillTool

CAPABILITY_TEXT = """
  链驱动智能体 — 核心工作方式：执行技能链（Pipeline）。

  可用工具:
    load_skill       → 加载任意技能（读 SKILL.md，自动理解用法）
    read_file        → 读取本地文件
    write_file       → 写入本地文件
    list_directory   → 列出目录内容
    web_fetch        → 获取网页内容
    web_search       → 搜索网络
    python_execute   → 执行 Python 代码

  技能在配置的路径下，用 SKILL.md 描述能力。
  链编排：在 Web UI 的 Pipeline Tab 中拖拽组合技能。
"""


# ======================================================================
# CLI 参数
# ======================================================================
def build_parser():
    p = argparse.ArgumentParser(
        description="Orchestrator - 动态技能加载智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 运行模式
    p.add_argument("--check", action="store_true", help="仅检测后端连接，不进入对话")
    p.add_argument("--list-models", action="store_true", help="罗列所有可用模型")
    p.add_argument("--verbose", default="", choices=["True", "False", ""],
                   help="是否打印思考过程")
    p.add_argument("--web", action="store_true", help="启动 Web UI")
    p.add_argument("--port", type=str, default="8788", help="Web UI 端口（默认 8788，设为 auto 自动分配空闲端口）")
    p.add_argument("--pidfile", default="", help="PID 文件路径（setup.bat 用）")
    # 批处理 / 管道模式
    p.add_argument("--batch", nargs=2, metavar=("INPUT", "OUTPUT"), default=None,
                   help="批处理模式: --batch input.json output.json")
    p.add_argument("--jsonl", action="store_true",
                   help="JSONL 管道模式: stdin 逐行读，stdout 逐行输出")

    # 后端选择
    backend = p.add_argument_group("后端选择（四选一，默认 lm-studio）")
    backend.add_argument("--backend", default="lm-studio",
                         choices=["lm-studio", "ollama", "custom", "direct"],
                         help="LLM 后端")
    backend.add_argument("--direct", action="store_true",
                         help="等同 --backend direct")

    # API 后端参数
    api = p.add_argument_group("API 后端参数 (lm-studio / ollama / custom)")
    api.add_argument("--base-url", default="",
                     help="自定义 API 地址 (custom 模式下必填)")
    api.add_argument("--api-key", default="", help="API Key")

    # 直接加载参数
    direct = p.add_argument_group("直接加载参数 (direct)")
    direct.add_argument("--model", "-m", default="",
                        help="模型名称或列表中序号 (如 '2' 或 'qwen3.6-35b-a3b-Q4_K_M')")
    direct.add_argument("--gpu-layers", type=int, default=-1,
                        help="GPU 卸载层数 (-1=自动, 0=CPU)")

    # 其他
    p.add_argument("--config", "-c", default="", help="配置文件路径")
    p.add_argument("--no-rag", action="store_true", help="不加载 RAG 工具")
    p.add_argument("--no-web", action="store_true", help="不加载网络工具")

    return p


# ======================================================================
# 模型罗列
# ======================================================================
def list_models():
    """扫描并打印所有可用模型"""
    from orchestrator.model_manager import get_model_manager

    mgr = get_model_manager()
    mgr.discover(force_rescan=True)
    all_models = mgr.list()

    if not all_models:
        print("未发现任何本地模型。")
        print("  GGUF 模型请放在: ~/.lmstudio/models/ 或 ~/models/")
        print("  HF 模型会自动从 ~/.cache/huggingface/hub/ 识别")
        return

    print(f"\n发现 {len(all_models)} 个模型:")
    print()

    # 按类型分组
    from orchestrator.model_manager import ModelType
    for mtype in [ModelType.GGUF, ModelType.SENTENCE_TRANSFORMER,
                  ModelType.CAUSAL_LM, ModelType.RERANKER]:
        models = [m for m in all_models if m.model_type == mtype]
        if not models:
            continue
        print(f"  [{mtype.value}]")
        for i, m in enumerate(models, 1):
            loaded = " [已加载]" if mgr.is_loaded(m.name) else ""
            gpu_tag = f" ~~> {m.vram_estimate_gb:.0f}GB VRAM" if m.vram_estimate_gb > 0 else ""
            print(f"    {i}. {m.name}  ({m.size_gb:.1f}GB, {m.source}){loaded}{gpu_tag}")
            print(f"       路径: {m.path}")
        print()

    print("用法示例:")
    print("  python main.py --direct --model 1")
    print("  python main.py --direct --model qwen3.6-35b-a3b-Q4_K_M")
    print()


# ======================================================================
# LLM 工厂
# ======================================================================
def make_llm(config, args):
    """根据参数创建 LLM 客户端"""

    # === 1. direct: 直接加载 GGUF ===
    if args.backend == "direct" or args.direct:
        return _make_direct_llm(config, args)

    # === 2. API 后端 ===
    base_url = args.base_url
    model_name = args.model

    # 优先用已持久化配置（settings.json），CLI 参数可覆盖
    saved_base = config.data["llm"].get("base_url", "")
    saved_model = config.data["llm"].get("model_name", "")

    if args.backend == "lm-studio":
        base_url = base_url or saved_base or "http://localhost:1234/v1"
        model_name = model_name or saved_model or "qwen/qwen3.6-35b-a3b"
    elif args.backend == "ollama":
        base_url = base_url or "http://localhost:11434/v1"
        model_name = model_name or saved_model or ""
    elif args.backend == "custom":
        if not base_url:
            print("❌ custom 模式需要 --base-url")
            print("   例: --backend custom --base-url http://localhost:8000/v1 --model xxx")
            sys.exit(1)
        if not model_name:
            print("⚠️  custom 模式建议指定 --model")
    else:
        print(f"❌ 未知后端: {args.backend}")
        sys.exit(1)

    # 写入配置
    config.data["llm"]["base_url"] = base_url
    config.data["llm"]["api_key"] = args.api_key or "not-needed"
    if model_name:
        config.data["llm"]["model_name"] = model_name

    llm = LLMClient(config)
    ok, msg = llm.check_connection()
    if not ok:
        print(f"❌ [{args.backend}] 连接失败: {msg}")
        print(f"   地址: {base_url}")
        print(f"   模型: {model_name}")
        if args.backend == "lm-studio":
            print(f"   请启动 LM Studio 并加载模型")
        elif args.backend == "ollama":
            print(f"   请运行: ollama serve")
        sys.exit(1)
    print(f"  ✅ [{args.backend}] {msg}")
    return llm


def _make_direct_llm(config, args):
    """通过 ModelManager 直接加载 GGUF"""

    # 自动检测+安装 GPU 版 llama-cpp-python
    if CFG_AUTO_INSTALL_GPU:
        from orchestrator.direct_llm_client import ensure_gpu_llama
        ensure_gpu_llama()

    from orchestrator.model_manager import get_model_manager

    mgr = get_model_manager()
    mgr.discover()
    gguf_models = mgr.list(type_filter="gguf", llm_only=True)

    if not gguf_models:
        print("❌ 未找到 GGUF 模型文件")
        print("   请先下载 GGUF 模型放在 ~/.lmstudio/models/ 或 ~/models/")
        print("   或使用 --backend lm-studio 走 LM Studio API")
        sys.exit(1)

    # 选择模型
    model_name = args.model
    if not model_name:
        # 没指定 → 显示列表让用户选
        print("\n可用的 GGUF 模型:")
        for i, m in enumerate(gguf_models, 1):
            print(f"  {i}. {m.name}  ({m.size_gb:.1f}GB)")
        print()
        try:
            choice = input("选择模型 (1-{}): ".format(len(gguf_models))).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(1)

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(gguf_models):
                model_name = gguf_models[idx].name
            else:
                print(f"❌ 序号超出范围 (1-{len(gguf_models)})")
                sys.exit(1)
        elif choice:
            model_name = choice  # 直接输入名称
        else:
            model_name = gguf_models[0].name

    # 检查名称是否存在，不存在则尝试序号匹配
    matched = mgr.get(model_name)
    if matched is None:
        # 尝试序号
        if model_name.isdigit():
            idx = int(model_name) - 1
            if 0 <= idx < len(gguf_models):
                matched = gguf_models[idx]
                model_name = matched.name

    if matched is None:
        print(f"❌ 未找到模型: {model_name}")
        print(f"   可用: {', '.join(m.name for m in gguf_models)}")
        sys.exit(1)

    print(f"  📦 加载: {model_name} ({matched.size_gb:.1f}GB)")

    # 通过 ModelManager 加载
    device = "gpu" if args.gpu_layers != 0 else "cpu"
    n_gpu_layers = args.gpu_layers if args.gpu_layers >= 0 else -1

    try:
        instance = mgr.load(
            model_name,
            device=device,
            n_gpu_layers=n_gpu_layers,
        )
    except ImportError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 模型加载失败: {e}")
        sys.exit(1)

    device_info = mgr._device_map.get(model_name, "?")
    print(f"  ✅ [direct] 加载成功 (设备: {device_info})")

    # 包装为 LLMClient 兼容接口
    class _DirectWrapper(LLMClient):
        def __init__(self, inst, name, gpu_info):
            self._model = inst
            self._model_name = name
            self._gpu_info = gpu_info
            self.base_url = "direct://local"
            self.model = name

        def check_connection(self):
            return True, f"GGUF: {self._model_name} ({self._gpu_info})"

        def chat(self, messages, **kwargs):
            return self._model.create_chat_completion(
                messages=messages,
                temperature=kwargs.get("temperature", 0.3),
                max_tokens=kwargs.get("max_tokens", 4096),
            )["choices"][0]["message"]["content"]

    return _DirectWrapper(instance, model_name, device_info)


# ======================================================================
# Pipeline 扁平化与执行（批处理/管道模式用）
# ======================================================================
def _flatten_pipeline(nodes, depth=0):
    """递归展开 Pipeline 树为扁平步骤列表"""
    result = []
    for i, node in enumerate(nodes):
        mode = node.get("mode", "seq")
        name = node.get("name", "")
        display = node.get("display", name or "(unnamed)")
        children = node.get("children", [])
        if mode == "par":
            names = [c.get("display", c.get("name","(unnamed)")) for c in children]
            result.append({"mode":"par","display":display,"children_names":names})
            for child in children:
                child_name = child.get("name","")
                if child_name:
                    result.append({"mode":"seq","display":child.get("display",child_name),"name":child_name})
        elif mode == "loop":
            times = node.get("loop_times", 3) or node.get("times", 3)
            result.append({"mode":"loop","display":display,"times":times})
            for t in range(times):
                sub = _flatten_pipeline(children, depth+1)
                for s in sub:
                    s["_loop"] = t + 1
                result.extend(sub)
        else:
            result.append({"mode":"seq","display":display,"name":name})
    return result

def _run_skill_node(name, display, params, llm):
    """真执行单个技能节点：找脚本 → subprocess 跑（与 web_ui 相同逻辑）"""
    from orchestrator.chain_engine import (
        _find_skill_dir, _get_skill_scripts, _get_main_script, _run_script,
    )
    sdir = _find_skill_dir(name)
    if not sdir:
        return f"[错误] 找不到技能目录: {name}"
    scripts = _get_skill_scripts(sdir)
    main_script = _get_main_script(name, scripts)
    if not main_script:
        return f"[不可编排] 技能「{name}」没有可执行脚本（纯提示词技能无法参与链执行）"
    cli_args = []
    if isinstance(params, dict):
        cmd = params.get("command") or params.get("cmd") or params.get("subcommand")
        if cmd:
            cli_args.append(str(cmd))
        args_list = params.get("args") or params.get("arguments") or []
        if isinstance(args_list, str):
            args_list = [args_list]
        if isinstance(args_list, (list, tuple)):
            cli_args.extend(str(a) for a in args_list)
        for k, v in params.items():
            if k in ("command", "cmd", "subcommand", "args", "arguments"):
                continue
            cli_args.extend([f"--{k}", str(v)])
    return _run_script(main_script, 180, cli_args=cli_args)


def _execute_pipeline_batch(nodes, llm=None):
    """执行 Pipeline 并返回结果文本（批处理/管道模式，真执行）"""
    flat = _flatten_pipeline(nodes)
    if not flat:
        return "（空 Pipeline）"
    lines = []
    for i, step in enumerate(flat):
        mode = step.get("mode","seq")
        display = step.get("display","")
        loop_info = f" [第{step['_loop']}轮]" if step.get("_loop") else ""
        if mode == "par":
            names = step.get("children_names",[])
            lines.append(f"  [{i+1}] ⬡ 并行组: {' | '.join(names)}")
        elif mode == "loop":
            lines.append(f"  [{i+1}] ↻ 循环组: {display} ({step['times']}次)")
        else:
            name = step.get("name","")
            params = step.get("params", {})
            lines.append(f"  [{i+1}] → {display}{loop_info}")
            if name:
                if llm is None:
                    lines.append(f"      结果: （LLM 不可用，仅规划）")
                    continue
                try:
                    result = _run_skill_node(name, display, params, llm)
                    lines.append(f"      结果: {result[:200]}")
                except Exception as e:
                    lines.append(f"      错误: {e}")
    return "\n".join(lines)


def run_batch(input_path, output_path, llm=None):
    """JSON 批处理模式"""
    import time
    start = time.time()
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        _write_json_output({"success": False, "error": f"读取失败: {e}"}, output_path)
        return
    nodes = data
    if isinstance(data, dict):
        nodes = data.get("nodes", data.get("tree", []))
    if not nodes or not isinstance(nodes, list):
        nodes = data if isinstance(data, list) else []
    try:
        output = _execute_pipeline_batch(nodes, llm)
        elapsed = int((time.time() - start) * 1000)
        result = {"success": True, "output": output, "steps": len(_flatten_pipeline(nodes)), "latency_ms": elapsed}
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        result = {"success": False, "error": str(e), "latency_ms": elapsed}
    _write_json_output(result, output_path)


def _write_json_output(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  [batch] 结果已写入: {path}")


def run_jsonl(llm=None):
    """JSONL 管道模式（仅 Pipeline 节点执行，无普通对话）"""
    import sys, time
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        start = time.time()
        try:
            data = json.loads(line)
            nodes = data.get("nodes", data.get("tree", []))
            if nodes:
                output = _execute_pipeline_batch(nodes, llm)
            else:
                output = "（空输入，JSONL 仅支持 Pipeline 节点执行）"
            elapsed = int((time.time() - start) * 1000)
            sys.stdout.write(json.dumps({"success": True, "output": output, "latency_ms": elapsed}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            sys.stdout.write(json.dumps({"success": False, "error": str(e), "latency_ms": elapsed}, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# ======================================================================
# 入口
# ======================================================================
def main():
    args = build_parser().parse_args()

    # 【配置区】优先使用代码里的配置
    if CFG_BACKEND == "list-models":
        list_models()
        return

    # 用代码配置覆盖命令行参数
    if args.backend == "lm-studio" and CFG_BACKEND != "lm-studio":
        args.backend = CFG_BACKEND
    if CFG_MODEL:
        args.model = CFG_MODEL
    if CFG_BASE_URL:
        args.base_url = CFG_BASE_URL
    if CFG_API_KEY:
        args.api_key = CFG_API_KEY
    if CFG_CUSTOM_MODEL and not args.model:
        args.model = CFG_CUSTOM_MODEL
    if CFG_VERBOSE:
        args.verbose = "True"
    if CFG_GPU_LAYERS != -1:
        args.gpu_layers = CFG_GPU_LAYERS

    if args.direct:
        args.backend = "direct"

    # 加载配置（统一使用 data/config/settings.json）
    if args.config:
        cfg_path = args.config
    else:
        script_cfg = os.path.join(_SCRIPT_DIR, "data", "config", "settings.json")
        cfg_path = script_cfg

    config = AgentConfig.load(cfg_path)

    if args.verbose == "True":
        config.data["agent"]["verbose"] = True
    elif args.verbose == "False":
        config.data["agent"]["verbose"] = False

    # 批处理模式：真实执行（需 LLM）
    if args.batch:
        input_path, output_path = args.batch
        try:
            batch_llm = make_llm(config, args)
        except SystemExit:
            print("  [batch] LLM 不可用，降级为步骤规划输出（不执行技能）")
            batch_llm = None
        run_batch(input_path, output_path, llm=batch_llm)
        return

    # JSONL 管道模式：真实执行
    if args.jsonl:
        try:
            pipe_llm = make_llm(config, args)
        except SystemExit:
            print("  [jsonl] LLM 不可用，降级为步骤规划输出（不执行技能）")
            pipe_llm = None
        run_jsonl(llm=pipe_llm)
        return

    # 创建 LLM
    llm = make_llm(config, args)
    if args.check:
        return

    # 显示能力说明
    print(CAPABILITY_TEXT)

    # Web UI 模式（编排器唯一入口；无 --web 时提示使用方式）
    if args.web:
        import socket
        port = args.port
        if port == "auto":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", 0))
            port = s.getsockname()[1]
            s.close()
        else:
            port = int(port)
        if args.pidfile:
            with open(args.pidfile, "w") as f:
                f.write(f"{os.getpid()}\n{port}")
                portfile = args.pidfile.replace(".pid", ".port")
                with open(portfile, "w") as pf:
                    pf.write(str(port))
        from orchestrator.web_ui import start_web_ui
        start_web_ui(config=config, llm=llm, port=port)
        return

    # 无交互对话：编排器不是聊天工具，仅提示使用方式
    print("Orchestrator 是链驱动编排器，不是聊天工具。")
    print("使用方式:")
    print("  python main.py --web              # 启动 Web UI（编排 Pipeline）")
    print("  python main.py --batch in.json out.json   # 批处理执行 Pipeline")
    print("  python main.py --jsonl < in.jsonl        # JSONL 管道执行")


if __name__ == "__main__":
    main()
