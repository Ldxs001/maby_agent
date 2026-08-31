"""Web UI — 前置规范编排器界面

基于 http.server（标准库），提供：
  GET  /              → 主页面（static/index.html）
  GET  /static/*      → 静态文件（CSS/JS）
  POST /api/run       → 执行编排
  GET  /api/combos    → 列出 14 种组合
  GET  /api/tools     → 列出已注册工具
  GET  /api/config    → 获取配置
  GET  /api/llm/models → 列出 LLM 可用模型
  GET  /api/llm/test   → 测试 LLM 连接
  POST /api/config    → 保存配置到文件
"""
from __future__ import annotations
import json
import os
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_static_file("index.html")
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self._serve_static_file(rel)
        elif path == "/api/combos":
            from .combo_registry import list_combos
            self._send_json(200, {"combos": [c.to_dict() for c in list_combos()]})
        elif path == "/api/tools":
            from .tool_registry import list_tools
            self._send_json(200, {"tools": [t.to_dict() for t in list_tools()]})
        elif path == "/api/config":
            self._send_json(200, self.server.config)
        elif path == "/api/llm/models":
            self._handle_llm_models(parsed.query)
        elif path == "/api/llm/test":
            self._handle_llm_test(parsed.query)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/run":
            self._handle_run()
        elif path == "/api/config":
            self._handle_save_config()
        else:
            self._send_json(404, {"error": "not found"})

    def _serve_static_file(self, filename):
        fpath = os.path.join(_STATIC_DIR, filename)
        if not os.path.isfile(fpath):
            self._send_json(404, {"error": f"not found: {filename}"})
            return
        ext = os.path.splitext(filename)[1].lower()
        mime = _MIME_TYPES.get(ext, "application/octet-stream")
        with open(fpath, "rb") as f:
            body = f.read()
        self._send_raw(200, body, mime)

    def _handle_llm_models(self, query):
        from .llm_client import LLMClient
        qs = urllib.parse.parse_qs(query)
        backend = qs.get("backend", ["lm-studio"])[0]
        base_url = qs.get("base_url", ["http://localhost:1234"])[0]
        api_key = qs.get("api_key", ["not-needed"])[0]
        llm = LLMClient(backend=backend, base_url=base_url, api_key=api_key)
        models = llm.list_models()
        self._send_json(200, {"models": models})

    def _handle_llm_test(self, query):
        from .llm_client import LLMClient
        qs = urllib.parse.parse_qs(query)
        backend = qs.get("backend", ["lm-studio"])[0]
        base_url = qs.get("base_url", ["http://localhost:1234"])[0]
        api_key = qs.get("api_key", ["not-needed"])[0]
        llm = LLMClient(backend=backend, base_url=base_url, api_key=api_key)
        ok, msg = llm.test_connection()
        self._send_json(200, {"success": ok, "msg": msg})

    def _handle_save_config(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = self.rfile.read(length)
            new_cfg = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "无效 JSON"})
            return
        cfg = self.server.config
        if "llm" in new_cfg:
            cfg["llm"] = {**cfg.get("llm", {}), **new_cfg["llm"]}
        if "orchestrator" in new_cfg:
            cfg["orchestrator"] = {**cfg.get("orchestrator", {}), **new_cfg["orchestrator"]}
        config_path = getattr(self.server, "config_path", "")
        if config_path:
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                self._send_json(200, {"success": True})
            except Exception as e:
                self._send_json(200, {"success": False, "error": str(e)})
        else:
            self._send_json(200, {"success": True})

    def _handle_run(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = self.rfile.read(length)
            req = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "无效 JSON"})
            return

        user_input = req.get("message", "")
        tool_names = req.get("tools", None)
        verbose = req.get("verbose", False)

        from .orchestrator import Orchestrator
        orch = Orchestrator(self.server.llm, self.server.config, verbose=verbose)
        try:
            result = orch.run(user_input, tool_names)
            self._send_json(200, {"success": True, "result": result})
        except Exception as e:
            self._send_json(200, {"success": False, "error": str(e)})


def run_web(llm, config, port: int = 8789, pidfile: str = "", config_path: str = ""):
    if pidfile:
        try:
            with open(pidfile, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
    server = HTTPServer(("0.0.0.0", port), WebHandler)
    server.llm = llm
    server.config = config
    server.config_path = config_path
    print(f"  ✅ Web UI 启动: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        server.server_close()
        if pidfile and os.path.isfile(pidfile):
            try:
                os.remove(pidfile)
            except Exception:
                pass
