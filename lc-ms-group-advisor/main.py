#!/usr/bin/env python3
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""LC-MS 分组顾问 — 入口

用法：
  python main.py                    # 启动 Web UI（默认端口 8810）
  python main.py --port 8820        # 指定端口
  python main.py --check            # 检测 LLM 后端连接
"""
import os
import sys
import argparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def build_parser():
    p = argparse.ArgumentParser(description="LC-MS 分组顾问 · 色谱出峰预测 + 质谱分组优化")
    p.add_argument("--port", type=str, default="8810", help="Web UI 端口")
    p.add_argument("--host", type=str, default="0.0.0.0", help="监听地址")
    p.add_argument("--pidfile", default="", help="PID 文件路径")
    p.add_argument("--check", action="store_true", help="仅检测后端连接")
    p.add_argument("--backend", default=None, choices=["lm-studio", "ollama", "custom"])
    p.add_argument("--base-url", default="")
    p.add_argument("--api-key", default="")
    p.add_argument("--model", "-m", default="")
    return p


def main():
    args = build_parser().parse_args()

    print("=" * 56)
    print("  LC-MS 分组顾问")
    print("  色谱出峰预测 + 质谱分组优化")
    print("=" * 56)
    print()

    if args.check:
        from lc_ms_group_advisor.llm_client import LLMClient
        from lc_ms_group_advisor.config_manager import ConfigManager
        cfg = ConfigManager()
        backend = args.backend or cfg.get("llm.backend", "lm-studio")
        base_url = args.base_url or cfg.resolve_base_url()
        model = args.model or cfg.get("llm.model", "")
        llm = LLMClient(backend=backend, base_url=base_url, model=model)
        ok, msg = llm.test_connection()
        print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
        return

    from lc_ms_group_advisor.web_ui import run_server
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