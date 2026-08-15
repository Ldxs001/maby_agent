#!/usr/bin/env python3
"""Structured Writer — 结构化写作智能体 入口"""
import sys
import argparse
from structured_writer.web_ui import run_server


def main():
    parser = argparse.ArgumentParser(
        description="Structured Writer — 结构化写作智能体"
    )
    parser.add_argument("--port", type=int, default=8770,
                        help="Web UI 端口（默认 8770）")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--api-port", type=int, default=None,
                        help="对外写作 API 端口（默认不启动；指定端口即启动，如 8777，仿 rag-assistant 8767 模式）")
    parser.add_argument("--no-web", action="store_true",
                        help="不启动 Web UI（仅对外 API，需配合 --api-port）")
    args = parser.parse_args()

    if args.api_port:
        import threading
        from structured_writer.external_api import start_external_api
        t = threading.Thread(target=start_external_api,
                             args=(args.api_port, args.host), daemon=True)
        t.start()
        print(f"  对外写作 API 已启动: http://{args.host}:{args.api_port}")

    print("=" * 50)
    print("  Structured Writer · 结构化写作智能体")
    print(f"  版本: {get_version()}")
    print("=" * 50)
    print()

    if args.no_web and args.api_port:
        import time
        while True:
            time.sleep(3600)
    run_server(host=args.host, port=args.port)


def get_version():
    try:
        from structured_writer import __version__
        return __version__
    except ImportError:
        return "dev"


if __name__ == "__main__":
    main()
