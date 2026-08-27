"""Web UI — 前置规范效果实验台

界面：深色主题（对齐 structured-writer），三 Tab：
  - 配置：从 5 种前置规范方式中选一种或多种，各方式配置 + 并行数
  - 运行：输入 + 启动并行
  - 结果：每种方式每次并行的填入内容、重试次数、撑满失败、重现性
"""
import json
import os
import sys
import time
import threading
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from .pipeline_model import (
    Experiment, WayConfig, default_config, WAYS, WAY_HELPS, TASK_PROMPTS,
)
from .simulator import ExperimentRunner
from .llm_client import LLMClient, LLMClientError
from .config_manager import ConfigManager, BACKEND_DEFAULTS
from .atoms import WAY_RECIPES, recipe_for
from .e2e_demo import run_e2e_demo

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_EXP_FILE = _DATA_DIR / "experiment.json"
_RESULTS_DIR = _DATA_DIR / "results"
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

config_mgr = ConfigManager()

_run_tasks: dict = {}
_run_lock = threading.Lock()


def _load_exp() -> dict:
    if _EXP_FILE.exists():
        try:
            return json.loads(_EXP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return Experiment.default().to_dict()


def _save_exp(d: dict):
    _EXP_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_result(rtype: str, payload: dict) -> str:
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    rid = f"{ts}_{rtype}"
    (_RESULTS_DIR / f"{rid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return rid


def _list_results() -> list:
    out = []
    for f in sorted(_RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": f.stem, "type": d.get("type", ""), "saved_at": d.get("saved_at", ""), "summary": d.get("summary", "")})
    return out


def _read_result(rid: str) -> dict:
    p = _RESULTS_DIR / f"{rid}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _delete_result(rid: str) -> bool:
    p = _RESULTS_DIR / f"{rid}.json"
    if p.exists():
        p.unlink()
        return True
    return False


class SilPrespecEmulatorHandler(BaseHTTPRequestHandler):


    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _make_llm(self) -> LLMClient:
        backend = config_mgr.get("llm.backend", "lm-studio")
        base_url = config_mgr.get("llm.base_url", "")
        if not base_url:
            base_url = BACKEND_DEFAULTS.get(backend, "")
        return LLMClient(backend=backend, base_url=base_url,
                         model=config_mgr.get("llm.model", ""),
                         api_key=config_mgr.get("llm.api_key", "not-needed"))

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/experiment":
            self._send(200, _load_exp())
        elif self.path == "/api/config":
            self._send(200, config_mgr.get_all())
        elif self.path == "/api/ways":
            ways_list = []
            for w in WAYS:
                dc = default_config(w[0])
                r = recipe_for(w[0], dc)
                ways_list.append({"id": w[0], "name": w[1], "desc": w[2],
                                   "help": WAY_HELPS.get(w[0], ""),
                                   "default_config": dc,
                                   "default_task_prompt": TASK_PROMPTS.get(w[0], ""),
                                   "default_recipe": r.to_dict() if r else {}})
            self._send(200, {"ways": ways_list,
                             "custom_help": WAY_HELPS.get("custom", ""),
                             "custom_templates": config_mgr.get_custom_templates()})
        elif self.path == "/api/backends":
            self._send(200, {"backends": ["lm-studio", "ollama", "custom"],
                             "current": config_mgr.get("llm.backend", "lm-studio"),
                             "base_url": config_mgr.get("llm.base_url", ""),
                             "model": config_mgr.get("llm.model", "")})
        elif self.path.startswith("/api/llm/models"):
            import urllib.parse
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            backend = (params.get("backend") or ["lm-studio"])[0]
            base_url = (params.get("base_url") or [""])[0]
            if not base_url:
                base_url = BACKEND_DEFAULTS.get(backend, "")
            try:
                client = LLMClient(backend=backend, base_url=base_url)
                models = client.list_models()
                self._send(200, {"success": True, "models": models})
            except Exception as e:
                self._send(200, {"success": False, "models": [], "error": str(e)})
        elif self.path.startswith("/api/run/status"):
            tid = self.path.split("id=")[-1] if "id=" in self.path else ""
            with _run_lock:
                task = _run_tasks.get(tid)
            if task is None:
                self._send(404, {"error": "任务不存在"})
            else:
                self._send(200, {"done": task["done"], "running": task["running"],
                                 "result": task["result"], "error": task["error"], "progress": task["progress"]})
        elif self.path == "/api/results":
            self._send(200, {"results": _list_results()})
        elif self.path.startswith("/api/results/read"):
            rid = self.path.split("id=")[-1] if "id=" in self.path else ""
            self._send(200, _read_result(rid))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/experiment":
            body = self._read_body()
            _save_exp(body)
            self._send(200, {"ok": True})
        elif self.path == "/api/config":
            body = self._read_body()
            config_mgr.update(body)
            self._send(200, {"ok": True})
        elif self.path == "/api/backend":
            body = self._read_body()
            if "backend" in body: config_mgr.set("llm.backend", body["backend"])
            if "base_url" in body: config_mgr.set("llm.base_url", body["base_url"])
            if "model" in body: config_mgr.set("llm.model", body["model"])
            if "api_key" in body: config_mgr.set("llm.api_key", body["api_key"])
            self._send(200, {"ok": True})
        elif self.path == "/api/backend/test":
            body = self._read_body()
            backend = body.get("backend", config_mgr.get("llm.backend", "lm-studio"))
            base_url = body.get("base_url", "")
            if not base_url:
                base_url = BACKEND_DEFAULTS.get(backend, "")
            llm = LLMClient(backend=backend, base_url=base_url,
                            model=body.get("model", config_mgr.get("llm.model", "")),
                            api_key=body.get("api_key", config_mgr.get("llm.api_key", "not-needed")))
            ok, msg = llm.test_connection()
            self._send(200, {"ok": ok, "message": msg})
        elif self.path == "/api/run":
            body = self._read_body()
            exp = body.get("experiment", _load_exp())
            user_input = body.get("input", "")
            parallel = int(body.get("parallel", 5))
            tid = f"run_{int(time.time() * 1000)}"
            with _run_lock:
                _run_tasks[tid] = {"done": False, "running": True, "result": None, "error": "", "progress": 0}
            t = threading.Thread(target=self._run_task, args=(tid, exp, user_input, parallel), daemon=True)
            t.start()
            self._send(200, {"task_id": tid})
        elif self.path == "/api/e2e_demo":
            body = self._read_body()
            parallel = int(body.get("parallel", 3))
            tid = f"e2e_{int(time.time() * 1000)}"
            with _run_lock:
                _run_tasks[tid] = {"done": False, "running": True, "result": None, "error": "", "progress": 0}
            t = threading.Thread(target=self._e2e_task, args=(tid, parallel), daemon=True)
            t.start()
            self._send(200, {"task_id": tid})
        elif self.path == "/api/custom_templates":
            body = self._read_body()
            tid = config_mgr.save_custom_template({
                "id": body.get("id", ""),
                "name": body.get("name", "未命名模板"),
                "recipe": body.get("recipe", {}),
                "task_prompt": body.get("task_prompt", ""),
                "default_config": body.get("default_config", {}),
            })
            self._send(200, {"ok": True, "id": tid, "custom_templates": config_mgr.get_custom_templates()})
        elif self.path == "/api/results/delete":
            body = self._read_body()
            ok = _delete_result(body.get("id", ""))
            self._send(200, {"ok": ok, "results": _list_results()})
        elif self.path == "/api/results/clear":
            for f in _RESULTS_DIR.glob("*.json"):
                try: f.unlink()
                except Exception: pass
            self._send(200, {"ok": True, "results": []})
        else:
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/api/custom_templates?"):
            import urllib.parse
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tid = (params.get("id") or [""])[0]
            ok = config_mgr.delete_custom_template(tid)
            self._send(200, {"ok": ok, "custom_templates": config_mgr.get_custom_templates()})
        else:
            self._send(404, {"error": "not found"})

    def _run_task(self, tid, exp, user_input, parallel):
        with _run_lock:
            task = _run_tasks[tid]
        try:
            llm = self._make_llm()
            ok, msg = llm.test_connection()
            if not ok:
                with _run_lock:
                    task["error"] = f"LLM 连接失败：{msg}"; task["done"] = True; task["running"] = False
                return
            runner = ExperimentRunner(llm=llm, verbose=False)
            with _run_lock:
                task["progress"] = 10
            result = runner.run(exp, user_input, parallel=parallel)
            import datetime
            _ways = [w.get("way","") for w in (exp.get("ways",[]) if isinstance(exp,dict) else []) if w.get("enabled",True)]
            _save_result("run", {"type":"run","saved_at":datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 "input":user_input,"summary":f"运行 {'/'.join(_ways) or '?'} · 并行{parallel}","result":result})
            with _run_lock:
                task["result"] = result; task["progress"] = 100; task["done"] = True; task["running"] = False
        except Exception as e:
            with _run_lock:
                task["error"] = f"{e}\n{traceback.format_exc()}"; task["done"] = True; task["running"] = False

    def _e2e_task(self, tid, parallel=3):
        with _run_lock:
            task = _run_tasks[tid]
        try:
            backend = config_mgr.get("llm.backend", "lm-studio")
            base_url = config_mgr.get("llm.base_url", "")
            if not base_url:
                base_url = BACKEND_DEFAULTS.get(backend, "")
            llm = LLMClient(backend=backend, base_url=base_url,
                            model=config_mgr.get("llm.model", ""),
                            api_key=config_mgr.get("llm.api_key", "not-needed"),
                            timeout=config_mgr.get("llm.timeout", 1200),
                            max_tokens=config_mgr.get("llm.max_tokens", 4096),
                            temperature=config_mgr.get("llm.temperature", 0.7))
            ok, msg = llm.test_connection()
            if not ok:
                with _run_lock:
                    task["error"] = f"LLM 连接失败：{msg}"; task["done"] = True; task["running"] = False
                return
            def on_progress(done, total, res):
                with _run_lock:
                    task["result"] = {"demo_results": res}
                    task["progress"] = int(done / total * 100)
            results = run_e2e_demo(llm, parallel=parallel, on_progress=on_progress)
            import datetime
            _save_result("e2e", {"type":"e2e","saved_at":datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 "summary":f"端到端演示 {len(results)}方式 · 并行{parallel}","result":results})
            with _run_lock:
                task["result"] = {"demo_results": results}
                task["progress"] = 100; task["done"] = True; task["running"] = False
        except Exception as e:
            with _run_lock:
                task["error"] = f"{e}\n{traceback.format_exc()}"; task["done"] = True; task["running"] = False


# ======================================================================
# HTML
# ======================================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>silprespec-emulator · LLM 有限行为量化工具</title>
<style>
:root{--bg:#1a1a2e;--bg-card:#16213e;--bg-panel:#0f3460;--bg-input:#1a1a3e;--text:#e0e0e0;--text-dim:#8899aa;--accent:#e94560;--accent2:#533483;--green:#00b894;--border:#2a2a4e;--radius:8px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}
.topbar{height:48px;background:linear-gradient(135deg,var(--accent2),var(--accent));display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}
.topbar .logo{font-weight:700;font-size:16px}.topbar .tag{font-size:11px;opacity:.75}.topbar .spacer{flex:1}
.topbar select,.topbar input{background:var(--bg-input);border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-size:12px}
.topbar button{padding:4px 12px;border:none;border-radius:4px;background:var(--accent);color:#fff;cursor:pointer;font-size:12px}
.topbar .status{font-size:12px}.topbar .status.ok{color:var(--green)}.topbar .status.fail{color:var(--accent)}
.tab-bar{display:flex;background:var(--bg-panel);border-bottom:1px solid var(--border);flex-shrink:0;padding:0 12px}
.tab-btn{padding:10px 22px;cursor:pointer;color:var(--text-dim);font-size:14px;border-radius:8px 8px 0 0;margin:6px 2px 0 0;border:1px solid transparent;border-bottom:none;transition:all .2s}
.tab-btn:hover{color:var(--text);background:rgba(255,255,255,.05)}
.tab-btn.active{color:var(--text);background:var(--bg-card);border-color:var(--border)}
.tab-content{display:none;flex:1 1 0;min-height:0;overflow:hidden}
.tab-content.active{display:flex;flex-direction:column;height:calc(100vh - 96px);flex:none;min-height:0}
.panel{max-width:1100px;width:100%;margin:0 auto;padding:20px 24px 40px;box-sizing:border-box;flex:1;overflow-y:auto;min-height:0}
.section{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;margin-bottom:16px}
.section h3{font-size:14px;margin-bottom:14px}.section h4{font-size:13px;margin:10px 0 8px;color:var(--accent)}
.form-row{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.form-row label{min-width:80px;font-size:13px;color:var(--text-dim);flex-shrink:0}
.form-row input,.form-row select,.form-row textarea{flex:1;min-width:150px;padding:6px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:13px;font-family:inherit}
.form-row input:focus,.form-row select:focus,.form-row textarea:focus{outline:none;border-color:var(--accent)}
textarea{width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;font-family:inherit;resize:vertical;box-sizing:border-box}
textarea:focus{outline:none;border-color:var(--accent)}
.btn{padding:6px 16px;border:none;border-radius:4px;cursor:pointer;font-size:13px}
.btn:hover{opacity:.85}.btn-primary{background:var(--accent);color:#fff}.btn-secondary{background:var(--bg-panel);color:var(--text);border:1px solid var(--border)}.btn-success{background:var(--green);color:#fff}.btn-sm{padding:4px 10px;font-size:12px}.btn-danger{background:#c0392b;color:#fff}
.way-card{background:var(--bg-panel);border:1px solid var(--border);border-radius:6px;padding:14px 16px;margin-bottom:12px}
.way-card .wc-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.way-card .wc-title{font-weight:700;font-size:14px}.way-card .wc-desc{font-size:11px;color:var(--text-dim);flex:1;min-width:200px}
.checkbox-row{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-dim)}
.badge{display:inline-block;padding:1px 6px;border-radius:8px;font-size:11px}
.badge.ok{background:var(--green);color:#fff}.badge.fail{background:var(--accent);color:#fff}.badge.warn{background:#d68910;color:#fff}.badge.dim{background:var(--bg-input);color:var(--text-dim)}
.stages-area{background:var(--bg-input);border-radius:6px;padding:8px 12px;margin-bottom:8px}
.stage-row{display:flex;gap:8px;padding:4px 0;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--border)}
.stage-row:last-child{border-bottom:none}
.stage-label{min-width:64px;font-size:12px;color:var(--accent);flex-shrink:0}
.stage-body{flex:1;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.atom-sel{flex:0 0 auto;min-width:auto;padding:3px 6px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:12px}
.atom-readonly{font-size:12px;color:var(--text);background:var(--bg-panel);padding:2px 8px;border-radius:3px}
.results-layout{display:flex;gap:12px;align-items:flex-start}
.results-main{flex:1;min-width:0}
.history-sidebar{width:240px;flex-shrink:0;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);display:flex;flex-direction:column;max-height:calc(100vh - 160px);position:sticky;top:0}
.history-sidebar .sidebar-header{padding:10px 12px;font-size:13px;font-weight:600;color:var(--text-dim);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.history-list{flex:1;overflow-y:auto;padding:0}
.history-item{padding:6px 10px;font-size:12px;cursor:pointer;border-bottom:1px solid var(--border);transition:background .15s}
.history-item:hover{background:rgba(255,255,255,.05)}
.history-item.active{background:var(--bg-panel)}
.history-item .h-type{display:inline-block;padding:0 5px;border-radius:8px;font-size:10px;margin-right:4px}
.history-item .h-type.run{background:var(--accent2);color:#fff}
.history-item .h-type.e2e{background:var(--accent);color:#fff}
.history-item .h-summary{color:var(--text);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.history-item .h-date{font-size:10px;color:var(--text-dim)}
.history-item .h-del{font-size:12px;color:var(--accent);cursor:pointer;opacity:.5;float:right;padding:0 2px}
.history-item .h-del:hover{opacity:1}
.history-item .h-del-cancel{font-size:10px;cursor:pointer;color:var(--text-dim);margin-left:4px}
.config-header{font-size:12px;color:var(--text-dim);margin:6px 0 4px;border-bottom:1px solid var(--border);padding-bottom:3px}
.run-block{background:var(--bg-panel);border:1px solid var(--border);border-radius:6px;padding:12px;margin-bottom:10px}
.run-block .rb-head{display:flex;align-items:center;gap:8px;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:6px}
.wr-block{background:var(--bg-input);border-radius:4px;padding:8px;margin-bottom:6px;font-size:12px}
.wr-block .wb-head{color:var(--accent);margin-bottom:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.wr-block pre{background:#0d0d1f;padding:6px;border-radius:3px;font-size:11px;white-space:pre-wrap;word-break:break-word;max-height:200px;overflow-y:auto;margin:4px 0}
.progress-bar{height:6px;background:var(--bg-input);border-radius:3px;overflow:hidden;margin:8px 0}
.progress-bar .fill{height:100%;background:var(--accent);transition:width .3s}
.kv{color:var(--text-dim);font-size:11px}.kv b{color:var(--text)}
select{background:var(--bg-input);color:var(--text)}
select option{background:var(--bg-input);color:var(--text)}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;align-items:center;justify-content:center}
.modal-overlay.show{display:flex}
.modal-box{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);width:460px;max-width:90vw;max-height:80vh;display:flex;flex-direction:column}
.modal-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-header h3{font-size:14px;color:var(--accent);font-weight:500}
.modal-close{cursor:pointer;color:var(--text-dim);font-size:18px;background:none;border:none;padding:0 4px}
.modal-close:hover{color:var(--text)}
.modal-body{padding:16px;overflow-y:auto;flex:1}
.modal-body p{font-size:13px;color:var(--text);margin-bottom:10px;line-height:1.6}
.modal-body input[type="text"]{width:100%;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:14px;font-family:inherit;box-sizing:border-box}
.modal-body input:focus{outline:none;border-color:var(--accent)}
.modal-footer{padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end;align-items:center}
</style>
</head>
<body>
<div class="topbar">
  <span class="logo">⚡ silprespec-emulator</span>
  <span class="tag">LLM 有限行为量化工具</span>
  <span style="flex:1"></span>
  <span class="status" id="autosave-status">● 已就绪</span>
</div>
<div class="tab-bar">
  <div class="tab-btn active" data-tab="config">配置</div>
  <div class="tab-btn" data-tab="run">运行</div>
  <div class="tab-btn" data-tab="result">结果</div>
</div>

<div class="tab-content active" id="tab-config">
  <div class="panel">
    <div class="section">
      <h3>🔧 LLM 后端</h3>
      <div class="form-row">
        <label>后端</label>
        <select id="llm-backend"><option value="lm-studio" selected>LM Studio</option><option value="ollama">Ollama</option><option value="custom">Custom</option></select>
      </div>
      <div class="form-row">
        <label>地址</label>
        <input type="text" id="llm-base-url" value="http://localhost:1234">
      </div>
      <div class="form-row">
        <label>模型</label>
        <select id="llm-model" style="flex:2"><option value="">(请选择)</option></select>
        <button class="btn btn-sm btn-secondary" id="btn-refresh-models">刷新</button>
      </div>
      <div class="form-row">
        <label>超时(s)</label>
        <input type="number" id="llm-timeout" value="120" style="width:80px">
        <label>最大Token</label>
        <input type="number" id="llm-max-tokens" value="4096" style="width:100px">
        <label>温度</label>
        <input type="number" id="llm-temperature" value="0.7" min="0" max="1" step="0.05" style="width:70px">
      </div>
      <div class="form-row">
        <button class="btn btn-sm btn-primary" id="btn-test-conn">测试连接</button>
        <span id="conn-status" class="status">未检测</span>

      </div>
    </div>
    <div class="section">
      <h3>实验基本信息</h3>
      <div class="form-row"><label>名称</label><input type="text" id="exp-name"></div>
      <div class="form-row"><label>说明</label><input type="text" id="exp-desc"></div>

    </div>
    <div class="section">
      <h3>前置规范方式（选一种或多种） <button class="btn btn-sm btn-secondary" id="btn-add-way" style="margin-left:12px">+ 添加方式</button></h3>
      <div id="ways-list"></div>
    </div>
    <div class="section">
      <h3>★ 自定义模板库 <span style="font-size:12px;color:var(--text-dim);font-weight:normal">（保存的原子配方模板，可多次复用）</span></h3>
      <div id="template-library-list"></div>
    </div>
    <div class="section">
      <span class="kv">配置改动自动保存</span>
      <button class="btn btn-secondary" id="btn-reset">重置</button>
    </div>
  </div>
</div>

<div class="tab-content" id="tab-run">
  <div class="panel">
    <div class="section">
      <h3>运行实验</h3>
      <div class="form-row"><label>输入</label><textarea id="run-input" rows="4" placeholder="要验证的内容"></textarea></div>
      <div class="form-row"><label>并行数</label><input type="number" id="run-parallel" value="5" min="1" max="20"></div>
      <button class="btn btn-success" id="btn-run">启动并行实验</button>
      <span id="run-status" class="status" style="margin-left:12px"></span>
      <div class="progress-bar"><div class="fill" id="progress-fill" style="width:0%"></div></div>
    </div>
    <div class="section">
      <h3>一键端到端演示 <span style="font-size:12px;color:var(--text-dim);font-weight:400">5 方式 × 预设输入 × 真实 LLM × 完整原始信息</span></h3>
      <p style="font-size:13px;color:var(--text-dim);margin-bottom:10px">用预设输入对 5 种方式逐一端到端真实调用 LLM，展示从输入到输出的完整信息（配置/每次 LLM 调用/attempt/结果/观测），供有限实证。</p>
      <div class="form-row" style="margin-bottom:10px"><label>每方式并行</label><input type="number" id="e2e-parallel" value="3" min="1" max="10" style="max-width:80px"><button class="btn btn-primary" id="btn-e2e">一键端到端演示（5 方式）</button></div>
      <span id="e2e-status" class="status" style="margin-left:12px"></span>
      <div class="progress-bar" style="margin-top:8px"><div class="fill" id="e2e-progress" style="width:0%"></div></div>
    </div>
    <div class="section">
      <h3>说明</h3>
      <p style="font-size:13px;color:var(--text-dim);line-height:1.7">
        <b style="color:var(--text)">本工具是 LLM 的有限行为量化工具</b>：对 LLM 在前置规范下的填空行为做有限、可重复的量化观测（填入内容/重试/撑满/重现性），供有限实证。<br>
        • 从 5 种前置规范方式中选一种或多种，对输入真实执行（LLM 真填空），观测<b>填入了什么</b>。<br>
        • 指标：填入内容、重试次数、撑满失败、重现性 + <b>验证指标</b>（值域命中率/纠偏编辑距离/纠偏有效性/钉死确定性/检出率/上报率，量化每种后置是否真的生效）。<br>
        • 并行 N 次观测<b>重现性</b>（各方式跨 run 填入一致率）。<br>
        • 5 种都是前置规范（生成通道/填空出口）；后置验证（任务完成后全量验证）不在本系统。
      </p>
    </div>
  </div>
</div>

<div class="tab-content" id="tab-result">
  <div class="panel">
    <div class="results-layout">
      <div class="results-main">
        <div class="section"><h3>实验结果 <button class="btn btn-sm btn-secondary" id="btn-refresh" style="margin-left:12px">刷新</button></h3><div id="results-list"><p style="color:var(--text-dim);font-size:13px">尚未运行。</p></div></div>
        <div class="section"><h3>重现性</h3><div id="repro-list"></div></div>
        <div class="section"><h3>端到端演示结果</h3><div id="e2e-list"><p style="color:var(--text-dim);font-size:13px">点击「一键端到端演示」后展示。</p></div></div>
      </div>
      <div class="history-sidebar">
        <div class="sidebar-header"><span>历史结果</span><button class="btn btn-sm btn-danger" id="btn-clear-history">清空</button></div>
        <div class="history-list" id="history-list"></div>
      </div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modal">
  <div class="modal-box">
    <div class="modal-header"><h3 id="modal-title">提示</h3><button class="modal-close" onclick="closeModal()">&times;</button></div>
    <div class="modal-body">
      <p id="modal-msg"></p>
      <input type="text" id="modal-input" style="display:none" placeholder="">
    </div>
    <div class="modal-footer">
      <span id="modal-status" style="font-size:12px;color:var(--text-dim);flex:1"></span>
      <button class="btn btn-secondary" id="modal-cancel" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="modal-ok">确认</button>
    </div>
  </div>
</div>

<script>
let experiment=null, waysMeta=null, customTemplates=null, customHelp='', currentTaskId=null, pollTimer=null;
let _modalCb=null;
function showModal(opts){
  document.getElementById('modal-title').textContent=opts.title||'提示';
  document.getElementById('modal-msg').textContent=opts.message||'';
  const inp=document.getElementById('modal-input');
  inp.style.display=opts.input?'block':'none';
  inp.value=opts.value||'';inp.placeholder=opts.placeholder||'';
  document.getElementById('modal-status').textContent='';
  document.getElementById('modal-cancel').style.display=opts.hideCancel?'none':'inline-block';
  document.getElementById('modal-cancel').textContent=opts.cancelText||'取消';
  document.getElementById('modal-ok').textContent=opts.okText||'确认';
  _modalCb=opts.onConfirm||null;
  document.getElementById('modal').classList.add('show');
  if(opts.input)setTimeout(()=>inp.focus(),50);
}
function closeModal(){document.getElementById('modal').classList.remove('show');_modalCb=null;}
document.getElementById('modal-ok').onclick=()=>{
  const inp=document.getElementById('modal-input');
  const hasInput=inp.style.display!=='none';
  const val=hasInput?inp.value:null;
  if(_modalCb){const keep=_modalCb(val);if(keep!==true)closeModal();}
};
function alertModal(msg,title){showModal({title:title||'提示',message:msg,hideCancel:true,okText:'知道了',onConfirm:()=>{}});}
function confirmModal(msg,onYes,title){showModal({title:title||'确认',message:msg,okText:'确认',onConfirm:()=>{onYes();}});}
function promptModal(msg,defVal,onOk,title,placeholder){
  showModal({title:title||'输入',message:msg,input:true,value:defVal||'',placeholder:placeholder||'',okText:'确认',onConfirm:(v)=>{
    if(v&&v.trim()){onOk(v.trim());}else{document.getElementById('modal-status').textContent='不能为空';return true;}
  }});
}
document.querySelectorAll('.tab-btn').forEach(btn=>btn.onclick=()=>{
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');document.getElementById('tab-'+btn.dataset.tab).classList.add('active');
});
function loadLLMConfig(){fetch('/api/config').then(r=>r.json()).then(d=>{const llm=d.llm||{};document.getElementById('llm-backend').value=llm.backend||'lm-studio';const defUrl={'lm-studio':'http://localhost:1234','ollama':'http://localhost:11434','custom':''};document.getElementById('llm-base-url').value=llm.base_url||defUrl[llm.backend]||'';document.getElementById('llm-timeout').value=llm.timeout||120;document.getElementById('llm-max-tokens').value=llm.max_tokens||4096;document.getElementById('llm-temperature').value=llm.temperature||0.7;if(llm.model)refreshModels(llm.model);else refreshModels();});}
function refreshModels(savedModel){
  const backend=document.getElementById('llm-backend').value;
  const base_url=document.getElementById('llm-base-url').value;
  const sel=document.getElementById('llm-model');
  sel.innerHTML='<option value="">(加载中...)</option>';sel.disabled=true;
  fetch(`/api/llm/models?backend=${encodeURIComponent(backend)}&base_url=${encodeURIComponent(base_url)}`).then(r=>r.json()).then(d=>{
    const models=(d.success&&d.models)||[];
    if(models.length){sel.innerHTML='<option value="">(请选择)</option>';models.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=m;sel.appendChild(o);});if(savedModel&&Array.from(sel.options).some(o=>o.value===savedModel))sel.value=savedModel;}
    else{sel.innerHTML='<option value="">(未获取到模型 — 请检查后端服务与地址)</option>';}
    sel.disabled=false;
  }).catch(()=>{sel.innerHTML='<option value="">(获取失败)</option>';sel.disabled=false;});
}
document.getElementById('llm-backend').onchange=()=>{
  const b=document.getElementById('llm-backend').value;
  const def={'lm-studio':'http://localhost:1234','ollama':'http://localhost:11434','custom':''};
  document.getElementById('llm-base-url').value=def[b]||'';
  refreshModels();saveLLMAuto();
};
document.getElementById('btn-refresh-models').onclick=()=>refreshModels(document.getElementById('llm-model').value);
document.getElementById('btn-test-conn').onclick=()=>{
  const llm={backend:document.getElementById('llm-backend').value,base_url:document.getElementById('llm-base-url').value,model:document.getElementById('llm-model').value};
  fetch('/api/backend/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(llm)}).then(r=>r.json()).then(d=>{const el=document.getElementById('conn-status');el.textContent=d.ok?'已连接':'连接失败';el.className='status '+(d.ok?'ok':'fail');if(!d.ok)el.textContent+=': '+d.message;});
};
let _saveTimer=null;
function setAutosave(text,cls){const el=document.getElementById('autosave-status');if(!el)return;el.textContent=text;el.className='status '+(cls||'');}
function saveLLMAuto(){
  const llm={backend:document.getElementById('llm-backend').value,base_url:document.getElementById('llm-base-url').value,model:document.getElementById('llm-model').value,timeout:parseInt(document.getElementById('llm-timeout').value)||120,max_tokens:parseInt(document.getElementById('llm-max-tokens').value)||4096,temperature:parseFloat(document.getElementById('llm-temperature').value)||0.7};
  setAutosave('● 保存中…','');
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({llm})}).then(r=>r.json()).then(()=>setAutosave('● 已保存 '+new Date().toLocaleTimeString(),'ok'));
}
function saveExpAuto(){
  if(_saveTimer)clearTimeout(_saveTimer);
  setAutosave('● 编辑中…','');
  _saveTimer=setTimeout(()=>{const e=collectExp();fetch('/api/experiment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(e)}).then(r=>r.json()).then(()=>setAutosave('● 已保存 '+new Date().toLocaleTimeString(),'ok'));},500);
}
['llm-base-url','llm-timeout','llm-max-tokens','llm-temperature'].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener('blur',saveLLMAuto);});
document.getElementById('llm-model').addEventListener('change',saveLLMAuto);
['exp-name','exp-desc'].forEach(id=>{const el=document.getElementById(id);if(el)el.addEventListener('blur',saveExpAuto);});
document.getElementById('run-parallel').addEventListener('change',saveExpAuto);
document.getElementById('ways-list').addEventListener('change',saveExpAuto);
document.getElementById('ways-list').addEventListener('focusout',saveExpAuto);
function loadWays(){fetch('/api/ways').then(r=>r.json()).then(d=>{waysMeta=d.ways;customTemplates=d.custom_templates||[];customHelp=d.custom_help||'';renderTemplateLibrary();loadExp();});}
function loadExp(){fetch('/api/experiment').then(r=>r.json()).then(d=>{experiment=d;renderExp();});}
function renderExp(){
  document.getElementById('exp-name').value=experiment.name||'';
  document.getElementById('exp-desc').value=experiment.description||'';
  const rp=document.getElementById('run-parallel');if(rp)rp.value=experiment.parallel||5;
  const list=document.getElementById('ways-list');list.innerHTML='';
  (experiment.ways||[]).forEach(w=>list.appendChild(renderWay(w)));
}
function wayName(id){return(waysMeta||[]).find(w=>w.id===id)?.name||id;}
const ATOM_AXES={
  'text':{axis:'内容轴',cls:'fail',note:'自由文本·不可枚举'},
  'select':{axis:'集合轴',cls:'dim',note:'穷举选择·可枚举'},
  'slot':{axis:'集合轴',cls:'dim',note:'槽位填空·可枚举'},
  'deterministic':{axis:'格式轴',cls:'ok',note:'代码封死·A形态'},
  'enum_filter':{axis:'集合轴',cls:'dim',note:'枚举过滤·A形态'},
  'detect_report':{axis:'数值轴',cls:'warn',note:'检出即上报·B形态'},
  'json_parse':{axis:'集合轴',cls:'dim',note:'解析槽位'},
  'in_set':{axis:'集合轴',cls:'dim',note:'点对面'},
  'no_extra':{axis:'集合轴',cls:'dim',note:'无多余'},
  'required_full':{axis:'集合轴',cls:'dim',note:'必填齐全'},
  'in_range':{axis:'数值轴',cls:'warn',note:'面对面·收窄后校验'},
  'eq_exact':{axis:'数值轴',cls:'warn',note:'点对点·收窄后校验'},
  'guide':{axis:'集合轴',cls:'dim',note:'软引导·输出约束'},
  'diverge':{axis:'格式轴',cls:'warn',note:'放开+收紧·误差抵消'},
  'none':{axis:'—',cls:'dim',note:'不校验'},
};
const ATOM_GLOSS={
  'text':'文本生成：LLM 自由填空输出一段文本',
  'select':'穷举选择：LLM 从候选词表每道选一个词或未指定',
  'slot':'槽位填空：LLM 从输入提取信息填入预定义槽位，输出 JSON',
  'deterministic':'确定性后处理：正则替换+编号重排+空行归一化，LLM 零参与',
  'enum_filter':'枚举过滤：只留允许词列表中的词，标记编造',
  'detect_report':'检出即上报：正则扫描+白名单对照+标记人工复审',
  'json_parse':'JSON 解析：解析槽位 dict，找多余 key',
  'in_set':'集合成员校验：值必须在候选词表或未指定（点对面）',
  'no_extra':'无多余校验：查编造词或多余字段',
  'required_full':'必填齐全校验：required 槽位必须有内容',
  'in_range':'区间容差校验：数值必须在区间内（面对面）',
  'eq_exact':'精确相等校验：值必须等于指定值（点对点）',
  'guide':'软引导输出约束校验：检查续写满足必含关键词/禁词/长度/格式正则',
  'diverge':'发散纠偏目标校验：检查纠偏后 corrected 满足格式/必含/禁含模式',
  'deterministic':'确定性后处理/封死目标校验：正则替换+编号重排+空行归一化，并校验钉死后达标',
  'detect_report':'检出即上报/上报校验：正则扫描+白名单对照，空响应/无检出判失败，有检出=success不阻塞',
  'none':'不校验：直接通过',
  'hit':'命中分布：统计命中/未指定/编造',
  'fabricated':'编造统计：造了不在允许集的词数',
  'extra_keys':'多余字段：LLM 编造的槽位以外的 key',
  'left_empty':'留空统计：必填/可留空/实际留空数',
  'flagged':'检出统计：检出项数和未命中白名单数',
  'changed':'改过标记：后处理前后是否改过',
};
function axisTag(atom){const a=ATOM_AXES[atom];if(!a)return'';return ` <span class="badge ${a.cls}" style="font-size:10px">${a.axis}</span><span class="kv" style="font-size:10px;margin-left:4px">${a.note}</span>`;}
function renderStages(way,recipe,isCustom){
  recipe=recipe||{};const r=recipe;
  const optT=(vals,cur)=>vals.map(v=>`<option value="${v[0]}" title="${esc(ATOM_GLOSS[v[0]]||'')}" ${v[0]===cur?'selected':''}>${v[1]}</option>`).join('');
  const row=(label,content)=>`<div class="stage-row"><span class="stage-label">${label}</span><div class="stage-body">${content}</div></div>`;
  const ro=(atom)=>`<span class="atom-readonly" title="${esc(ATOM_GLOSS[atom]||'')}">${atom}</span>`;
  let gen;
  if(isCustom){
    gen=`<select data-w="r_generate" class="atom-sel" title="${esc(ATOM_GLOSS[r.generate||'text']||'')}" onchange="var a=this.nextElementSibling;a.style.display=this.value==='slot'?'inline-block':'none'">${optT([['text','text'],['select','select'],['slot','slot']],r.generate||'text')}</select>`;
    gen+=`<select data-w="r_generate_arg" class="atom-sel" style="display:${(r.generate||'text')==='slot'?'inline-block':'none'}">${optT([['','（无）'],['extra_check','extra_check'],['required_min','required_min']],r.generate_arg||'')}</select>`;
  }else{
    gen=ro(r.generate||'text');
    if(r.generate_arg)gen+=` ${ro(r.generate_arg)}`;
  }
  gen+=axisTag(r.generate||'text');
  const ppAtoms=['deterministic','enum_filter','detect_report','json_parse'];
  let pp;
  if(isCustom){
    pp=ppAtoms.map(a=>`<label class="checkbox-row" style="font-size:11px;gap:3px" title="${esc(ATOM_GLOSS[a]||'')}"><input type="checkbox" data-w="r_pp_${a}" ${(r.postprocess||[]).includes(a)?'checked':''}>${a}</label>`).join('');
    if((r.postprocess||[]).length)pp+=axisTag((r.postprocess||[])[0]);
  }else{
    pp=(r.postprocess&&r.postprocess.length)?r.postprocess.map(a=>ro(a)+axisTag(a)).join(' '):'<span class="kv">（无）</span>';
  }
  let val;
  if(isCustom)val=`<select data-w="r_validate" class="atom-sel" title="${esc(ATOM_GLOSS[r.validate||'none']||'')}">${optT([['none','none'],['in_set','in_set'],['no_extra','no_extra'],['required_full','required_full'],['in_range','in_range'],['eq_exact','eq_exact'],['guide','guide'],['diverge','diverge'],['deterministic','deterministic'],['detect_report','detect_report']],r.validate||'none')}</select>`;
  else val=ro(r.validate||'none');
  val+=axisTag(r.validate||'none');
  let rt;
  if(isCustom)rt=`<label class="checkbox-row"><input type="checkbox" data-w="r_retry" ${r.retry!==false?'checked':''}>启用重试</label>`;
  else rt=`<span class="atom-readonly">${r.retry!==false?'☑ 启用':'☐ 不启用'}</span>`;
  const obAtoms=['hit','fabricated','extra_keys','left_empty','flagged','changed'];
  let ob;
  if(isCustom)ob=obAtoms.map(a=>`<label class="checkbox-row" style="font-size:11px;gap:3px" title="${esc(ATOM_GLOSS[a]||'')}"><input type="checkbox" data-w="r_ob_${a}" ${(r.observe||[]).includes(a)?'checked':''}>${a}</label>`).join('');
  else ob=(r.observe&&r.observe.length)?r.observe.map(a=>ro(a)).join(' '):'<span class="kv">（无）</span>';
  return row('① 生成',gen)+row('② 后处理',pp)+row('③ 校验',val)+row('④ 重试',rt)+row('⑤ 观测',ob);
}

function renderWay(w){
  const card=document.createElement('div');card.className='way-card';
  const meta=(waysMeta||[]).find(x=>x.id===w.way)||{desc:'',help:''};
  const helpText=(w.way==='custom')?customHelp:(meta.help||'');
  const isCustom=w.way==='custom';
  const presetMeta=(waysMeta||[]).find(x=>x.id===w.way);
  const recipe=isCustom?(w.recipe||{}):(presetMeta&&presetMeta.default_recipe)||{};
  const tmplOpts=(customTemplates||[]).map(t=>`<option value="custom" data-tmpl="${t.id}" ${w.way==='custom'&&w.template_id===t.id?'selected':''}>★ ${esc(t.name)}</option>`).join('');
  card.innerHTML=`
    <div class="wc-head">
      <select data-w="way"><option value="custom" ${w.way==='custom'&&!w.template_id?'selected':''}>自定义组合（临时）</option>${tmplOpts}${(waysMeta||[]).map(x=>`<option value="${x.id}" ${x.id===w.way?'selected':''}>${x.name}</option>`).join('')}</select>
      <span class="wc-desc">${esc(meta.desc)}</span>
      <label class="checkbox-row"><input type="checkbox" data-w="enabled" ${w.enabled?'checked':''}>启用</label>
      <input type="number" data-w="max_retry" value="${w.max_retry||3}" min="0" max="10" style="width:70px" title="max_retry">
      <button class="btn btn-sm btn-secondary" data-act="saveas-tmpl">另存为模板</button>
      <button class="btn btn-sm btn-success tmpl-acts" data-act="save-tmpl" style="display:${isCustom&&w.template_id?'inline-block':'none'}">更新模板</button>
      <button class="btn btn-sm btn-danger" data-act="del">删除</button>
    </div>
    <input type="hidden" data-w="template_id" value="${esc(w.template_id||'')}">
    <details style="margin-top:4px;margin-bottom:6px"><summary style="cursor:pointer;color:var(--text-dim);font-size:12px">📖 说明</summary><pre class="way-help" style="background:#0d0d1f;padding:8px;border-radius:4px;font-size:11px;white-space:pre-wrap;word-break:break-word;margin-top:6px;max-height:300px;overflow-y:auto">${esc(helpText)}</pre></details>
    <div class="stages-area">${renderStages(w.way,recipe,isCustom)}</div>
    <div class="config-header">配置</div>
    <div data-w="config-area">${renderConfigForm(w.way,w.config)}</div>
    <div class="form-row"><label>任务提示词（系统提示词）</label><textarea data-w="task_prompt" rows="2">${esc(w.task_prompt||meta.default_task_prompt||'')}</textarea></div>
  `;
  card.querySelector('[data-act="del"]').onclick=()=>{card.remove();saveExpAuto();};
  card.querySelector('[data-act="save-tmpl"]').onclick=()=>saveAsTemplate(card,'update');
  card.querySelector('[data-act="saveas-tmpl"]').onclick=()=>saveAsTemplate(card,'saveAs');
  card.addEventListener('click',ev=>{
    const act=ev.target.getAttribute('data-act');
    if(act==='del-row'){const row=ev.target.closest('.cfg-row');if(row)row.remove();saveExpAuto();}
    else if(act==='add-gate'){const c=card.querySelector('[data-w="cfg_gates"]');if(c)c.insertAdjacentHTML('beforeend',configGateRow({}));}
    else if(act==='add-slot'){const c=card.querySelector('[data-w="cfg_slots"]');if(c)c.insertAdjacentHTML('beforeend',configSlotRow({}));}
    else if(act==='add-replace'){const c=card.querySelector('[data-w="cfg_replaces"]');if(c)c.insertAdjacentHTML('beforeend',configReplaceRow({}));}
  });
  card.addEventListener('change',ev=>{
    if(ev.target.getAttribute('data-act')==='bound-type-change'){const bt=ev.target.value;card.querySelectorAll('[data-w="cfg_bound_enum_select"],[data-w="cfg_bound_slots"],[data-w="cfg_bound_condense_enum"]').forEach(d=>d.style.display='none');const map={'enum_select':'cfg_bound_enum_select','slot_extract':'cfg_bound_slots','required_min':'cfg_bound_slots','condense_enum':'cfg_bound_condense_enum'};const t=card.querySelector(`[data-w="${map[bt]||'cfg_bound_enum_select'}"]`);if(t)t.style.display='block';}
  });
  card.querySelector('[data-w="way"]').onchange=(e)=>{
    const sel=e.target.selectedOptions[0];
    const tmplId=sel.getAttribute('data-tmpl')||'';
    const newIsCustom=e.target.value==='custom';
    const newWay=e.target.value;
    card.querySelector('[data-w="template_id"]').value=tmplId;
    const upBtn=card.querySelector('[data-act="save-tmpl"]');if(upBtn)upBtn.style.display=(newIsCustom&&tmplId)?'inline-block':'none';
    let cfg={},newRecipe={},taskPrompt='',desc='',help='';
    if(newIsCustom&&tmplId){const t=(customTemplates||[]).find(x=>x.id===tmplId)||{};desc=t.name?('模板：'+t.name):'自定义原子组合';cfg=t.default_config||{};newRecipe=t.recipe||{};taskPrompt=t.task_prompt||'';help=customHelp;}
    else if(newIsCustom){desc='自定义原子组合';help=customHelp;}
    else{const nm=(waysMeta||[]).find(x=>x.id===newWay)||{desc:'',help:'',default_config:{},default_task_prompt:'',default_recipe:{}};desc=nm.desc;help=nm.help||'';cfg=nm.default_config||{};taskPrompt=nm.default_task_prompt||'';newRecipe=nm.default_recipe||{};}
    card.querySelector('.wc-desc').textContent=desc;
    card.querySelector('.way-help').textContent=help;
    card.querySelector('.stages-area').innerHTML=renderStages(newWay,newRecipe,newIsCustom);
    card.querySelector('[data-w="config-area"]').innerHTML=renderConfigForm(newWay,cfg);
    card.querySelector('[data-w="task_prompt"]').value=taskPrompt;
  };
  return card;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function collectRecipe(card){
  const get=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.value:'';};
  const chk=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.checked:false;};
  let postprocess=[];['deterministic','enum_filter','detect_report','json_parse'].forEach(a=>{const el=card.querySelector(`[data-w="r_pp_${a}"]`);if(el&&el.checked)postprocess.push(a);});
  if(!postprocess.length){const el=card.querySelector('[data-w="r_postprocess"]');if(el)postprocess=Array.from(el.selectedOptions).map(o=>o.value);}
  let observe=[];['hit','fabricated','extra_keys','left_empty','flagged','changed'].forEach(a=>{const el=card.querySelector(`[data-w="r_ob_${a}"]`);if(el&&el.checked)observe.push(a);});
  if(!observe.length){const el=card.querySelector('[data-w="r_observe"]');if(el)observe=Array.from(el.selectedOptions).map(o=>o.value);}
  return {generate:get('r_generate'),generate_arg:get('r_generate_arg'),postprocess,validate:get('r_validate'),retry:chk('r_retry'),observe};
}
function configGateRow(g){return `<div class="form-row cfg-row" style="gap:6px"><input data-w="cfg_gate_name" value="${esc(g.name||'')}" placeholder="维度名" style="flex:1"><input data-w="cfg_gate_words" value="${esc((g.words||[]).join(','))}" placeholder="词1,词2,词3" style="flex:2"><button class="btn btn-sm btn-danger" data-act="del-row">删</button></div>`;}
function configSlotRow(s){return `<div class="form-row cfg-row" style="gap:6px"><input data-w="cfg_slot_name" value="${esc(s.name||'')}" placeholder="槽位名" style="flex:2"><label class="checkbox-row"><input type="checkbox" data-w="cfg_slot_req" ${s.required?'checked':''}>必填</label><button class="btn btn-sm btn-danger" data-act="del-row">删</button></div>`;}
function configReplaceRow(r){return `<div class="form-row cfg-row" style="gap:6px"><input data-w="cfg_repl_pat" value="${esc(r.pattern||'')}" placeholder="正则 pattern" style="flex:2"><input data-w="cfg_repl_rep" value="${esc(r.replace||'')}" placeholder="替换" style="flex:1"><button class="btn btn-sm btn-danger" data-act="del-row">删</button></div>`;}
function renderConfigForm(way,cfg){
  cfg=cfg||{};
  if(way==='custom'||!way) return `<div class="form-row"><label>配置JSON</label><textarea data-w="config" rows="6">${esc(JSON.stringify(cfg,null,2))}</textarea></div>`;
  if(way==='pure_guide'){const con=cfg.output_constraints||{};return `<div class="form-row"><label>引导提示词</label><textarea data-w="cfg_guide_prompt" rows="3">${esc(cfg.guide_prompt||'')}</textarea></div><div class="form-row"><label>必含关键词</label><input data-w="cfg_req_kw" value="${esc((con.required_keywords||[]).join(','))}" placeholder="逗号分隔，留空不校验"></div><div class="form-row"><label>禁词</label><input data-w="cfg_forbid_kw" value="${esc((con.forbidden_keywords||[]).join(','))}" placeholder="逗号分隔，留空不校验"></div><div class="form-row"><label>长度上限</label><input type="number" data-w="cfg_max_len" value="${con.max_length||0}" placeholder="0=不限"></div><div class="form-row"><label>格式正则</label><input data-w="cfg_fmt_regex" value="${esc(con.format_regex||'')}" placeholder="留空不校验"></div>`;}
  if(way==='value_bound'){const bt=cfg.bound_type||'enum_select';const gates=(cfg.gates&&cfg.gates.length)?cfg.gates.map(configGateRow).join(''):configGateRow({});const slots=(cfg.slots&&cfg.slots.length)?cfg.slots.map(configSlotRow).join(''):configSlotRow({});const showE=bt==='enum_select'?'block':'none',showS=(bt==='slot_extract'||bt==='required_min')?'block':'none',showC=bt==='condense_enum'?'block':'none';return `<div class="form-row"><label>值域类型</label><select data-w="cfg_bound_type" data-act="bound-type-change">${optT([['enum_select','可枚举选择'],['slot_extract','槽位提取'],['required_min','必填最小化'],['condense_enum','凝练+枚举过滤']],bt)}</select></div><div data-w="cfg_bound_enum_select" style="display:${showE}"><div data-w="cfg_gates">${gates}</div><div class="form-row"><button class="btn btn-sm btn-secondary" data-act="add-gate">+ 门禁</button></div><div class="form-row"><label>允许未指定</label><input type="checkbox" data-w="cfg_allow_unspec" ${cfg.allow_unspecified!==false?'checked':''}></div></div><div data-w="cfg_bound_slots" style="display:${showS}"><div data-w="cfg_slots">${slots}</div><div class="form-row"><button class="btn btn-sm btn-secondary" data-act="add-slot">+ 槽位</button></div></div><div data-w="cfg_bound_condense_enum" style="display:${showC}"><div class="form-row"><label>凝练规则</label><textarea data-w="cfg_condense_rule" rows="2">${esc(cfg.condense_rule||'')}</textarea></div><div class="form-row"><label>枚举词</label><input data-w="cfg_enums" value="${esc((cfg.enums||[]).join(','))}" placeholder="词1,词2,词3"></div></div>`;}
  if(way==='diverge_correct'){const reps=(cfg.regex_replaces&&cfg.regex_replaces.length)?cfg.regex_replaces.map(configReplaceRow).join(''):configReplaceRow({});const tgt=cfg.correction_target||{};return `<div class="form-row"><label>发散提示词</label><textarea data-w="cfg_diverge_prompt" rows="2">${esc(cfg.diverge_prompt||'')}</textarea></div><div data-w="cfg_replaces">${reps}</div><div class="form-row"><button class="btn btn-sm btn-secondary" data-act="add-replace">+ 替换规则</button></div><div class="form-row"><label>空行归一化</label><input type="checkbox" data-w="cfg_norm_blank" ${cfg.normalize_blanklines?'checked':''}></div><div class="form-row"><label>纠偏·格式正则</label><input data-w="cfg_ct_fmt" value="${esc(tgt.format_regex||'')}" placeholder="纠偏后须匹配，留空不校验"></div><div class="form-row"><label>纠偏·必含模式</label><input data-w="cfg_ct_req" value="${esc(tgt.required_pattern||'')}" placeholder="纠偏后须包含的正则，留空不校验"></div><div class="form-row"><label>纠偏·禁含模式</label><input data-w="cfg_ct_forbid" value="${esc(tgt.forbidden_pattern||'')}" placeholder="纠偏后不得包含的正则，留空不校验"></div>`;}
  if(way==='deterministic_pin'){const reps=(cfg.regex_replaces&&cfg.regex_replaces.length)?cfg.regex_replaces.map(configReplaceRow).join(''):configReplaceRow({});const tgt=cfg.pin_target||{};return `<div data-w="cfg_replaces">${reps}</div><div class="form-row"><button class="btn btn-sm btn-secondary" data-act="add-replace">+ 替换规则</button></div><div class="form-row"><label>编号重排</label><input type="checkbox" data-w="cfg_renumber" ${cfg.renumber_source?'checked':''}></div><div class="form-row"><label>空行归一化</label><input type="checkbox" data-w="cfg_norm_blank" ${cfg.normalize_blanklines?'checked':''}></div><div class="form-row"><label>封死·精确值</label><textarea data-w="cfg_pt_exact" rows="2">${esc(tgt.exact_value||'')}</textarea></div><div class="form-row"><label>封死·格式正则</label><input data-w="cfg_pt_fmt" value="${esc(tgt.format_regex||'')}" placeholder="钉死后须匹配，留空不校验"></div>`;}
  if(way==='detect_report') return `<div class="form-row"><label>检出正则</label><input data-w="cfg_detect_pat" value="${esc(cfg.detect_pattern||'')}" placeholder="\\d+(?:\\.\\d+)?(%|亿|万|元|人次)"></div><div class="form-row"><label>合法值</label><input data-w="cfg_allowed" value="${esc((cfg.allowed_values||[]).join(','))}" placeholder="100%,3.5亿（逗号分隔，可留空）"></div><div class="form-row"><label>上报标签</label><input data-w="cfg_report_label" value="${esc(cfg.report_label||'')}" placeholder="建议人工复审"></div>`;
  return `<div class="form-row"><label>配置JSON</label><textarea data-w="config" rows="6">${esc(JSON.stringify(cfg,null,2))}</textarea></div>`;
}
function collectReplaces(card){const out=[];card.querySelectorAll('[data-w="cfg_replaces"] .cfg-row').forEach(row=>{const p=row.querySelector('[data-w="cfg_repl_pat"]').value;if(p)out.push({pattern:p,replace:row.querySelector('[data-w="cfg_repl_rep"]').value});});return out;}
function collectConfig(card,way){
  const get=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.value:'';};
  const chk=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.checked:false;};
  if(way==='custom'||!way){const el=card.querySelector('[data-w="config"]');try{return JSON.parse(el.value);}catch(e){return {};}}
  if(way==='pure_guide') return {guide_prompt:get('cfg_guide_prompt'),output_constraints:{required_keywords:get('cfg_req_kw').split(',').map(s=>s.trim()).filter(Boolean),forbidden_keywords:get('cfg_forbid_kw').split(',').map(s=>s.trim()).filter(Boolean),max_length:parseInt(get('cfg_max_len')||'0',10)||0,format_regex:get('cfg_fmt_regex')}};
  if(way==='value_bound'){const bt=get('cfg_bound_type')||'enum_select';const out={bound_type:bt};if(bt==='enum_select'){const gates=[];card.querySelectorAll('[data-w="cfg_gates"] .cfg-row').forEach(row=>{const name=row.querySelector('[data-w="cfg_gate_name"]').value.trim();const words=row.querySelector('[data-w="cfg_gate_words"]').value.split(',').map(s=>s.trim()).filter(Boolean);if(name)gates.push({name,words,logic:'or'});});out.gates=gates;out.allow_unspecified=chk('cfg_allow_unspec');}else if(bt==='slot_extract'||bt==='required_min'){const slots=[];card.querySelectorAll('[data-w="cfg_slots"] .cfg-row').forEach(row=>{const name=row.querySelector('[data-w="cfg_slot_name"]').value.trim();if(name)slots.push({name,required:row.querySelector('[data-w="cfg_slot_req"]').checked});});out.slots=slots;}else if(bt==='condense_enum'){out.condense_rule=get('cfg_condense_rule');out.enums=get('cfg_enums').split(',').map(s=>s.trim()).filter(Boolean);}return out;}
  if(way==='diverge_correct') return {diverge_prompt:get('cfg_diverge_prompt'),regex_replaces:collectReplaces(card),normalize_blanklines:chk('cfg_norm_blank'),correction_target:{format_regex:get('cfg_ct_fmt'),required_pattern:get('cfg_ct_req'),forbidden_pattern:get('cfg_ct_forbid')}};
  if(way==='deterministic_pin') return {regex_replaces:collectReplaces(card),renumber_source:chk('cfg_renumber'),normalize_blanklines:chk('cfg_norm_blank'),pin_target:{exact_value:get('cfg_pt_exact'),format_regex:get('cfg_pt_fmt')}};
  if(way==='detect_report') return {detect_pattern:get('cfg_detect_pat'),allowed_values:get('cfg_allowed').split(',').map(s=>s.trim()).filter(Boolean),report_label:get('cfg_report_label')};
  return {};
}
function rebuildWayDropdown(card,selTmplId){
  const sel=card.querySelector('[data-w="way"]');const cur=sel.value;
  const tmplOpts=(customTemplates||[]).map(t=>`<option value="custom" data-tmpl="${t.id}" ${selTmplId===t.id?'selected':''}>★ ${esc(t.name)}</option>`).join('');
  sel.innerHTML=`<option value="custom" ${cur==='custom'&&!selTmplId?'selected':''}>自定义组合（临时）</option>${tmplOpts}${(waysMeta||[]).map(x=>`<option value="${x.id}" ${x.id===cur&&cur!=='custom'?'selected':''}>${x.name}</option>`).join('')}`;
}
function saveAsTemplate(card,mode){
  const get=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.value:'';};
  const way=get('way');
  let recipe={};
  if(way==='custom')recipe=collectRecipe(card);
  else{const m=(waysMeta||[]).find(x=>x.id===way);recipe=(m&&m.default_recipe)||{};}
  const cfg=collectConfig(card,way);
  const taskPrompt=get('task_prompt');
  const curTmplId=get('template_id');
  const doSave=(name)=>{
    const body={id:mode==='update'?curTmplId:'',name:name||'',recipe,task_prompt:taskPrompt,default_config:cfg};
    fetch('/api/custom_templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).then(d=>{
      if(!d.ok){alertModal('保存失败');return;}
      customTemplates=d.custom_templates||[];
      card.querySelector('[data-w="template_id"]').value=d.id;
      const waySel=card.querySelector('[data-w="way"]');
      if(way!=='custom'){waySel.value='custom';card.querySelector('[data-w="config-area"]').innerHTML=renderConfigForm('custom',cfg);card.querySelector('.stages-area').innerHTML=renderStages('custom',recipe,true);}
      const upBtn=card.querySelector('[data-act="save-tmpl"]');if(upBtn)upBtn.style.display='inline-block';
      rebuildWayDropdown(card,d.id);
      renderTemplateLibrary();
      setAutosave('● 模板已保存：'+(name||'已更新')+' '+new Date().toLocaleTimeString(),'ok');
    });
  };
  if(mode==='update'){doSave('');return;}
  const curTmpl=(customTemplates||[]).find(t=>t.id===curTmplId);
  promptModal('模板名称：',curTmpl?curTmpl.name:'',doSave,'另存为自定义模板','输入模板名称');
}
function deleteTemplate(id){
  confirmModal('删除该自定义模板？',()=>{
    fetch('/api/custom_templates?id='+encodeURIComponent(id),{method:'DELETE'}).then(r=>r.json()).then(d=>{
      customTemplates=d.custom_templates||[];
      document.querySelectorAll('#ways-list .way-card').forEach(card=>{
        const tidEl=card.querySelector('[data-w="template_id"]');
        if(tidEl&&tidEl.value===id){tidEl.value='';rebuildWayDropdown(card,'');}
      });
      renderTemplateLibrary();
    });
  },'删除模板');
}
function renderTemplateLibrary(){
  const el=document.getElementById('template-library-list');if(!el)return;
  const ts=customTemplates||[];
  if(!ts.length){el.innerHTML='<p class="kv">暂无自定义模板。在方式卡片中编辑配方后点"存为模板"。</p>';return;}
  el.innerHTML=ts.map(t=>`<div class="wr-block" style="display:flex;align-items:center;gap:10px">
    <span class="badge dim">★</span><b>${esc(t.name)}</b>
    <span class="kv">id: ${esc(t.id)}</span>
    <span style="flex:1"></span>
    <button class="btn btn-sm btn-secondary" data-load="${t.id}">加载到新卡片</button>
    <button class="btn btn-sm btn-danger" data-del="${t.id}">删除</button>
  </div>`).join('');
  el.querySelectorAll('[data-load]').forEach(b=>b.onclick=()=>{
    const t=(customTemplates||[]).find(x=>x.id===b.dataset.load);if(!t)return;
    document.getElementById('ways-list').appendChild(renderWay({way:'custom',enabled:true,config:t.default_config||{},max_retry:3,task_prompt:t.task_prompt||'',recipe:t.recipe||{},template_id:t.id}));
  });
  el.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>deleteTemplate(b.dataset.del));
}
function collectExp(){
  const ways=[];
  document.querySelectorAll('#ways-list .way-card').forEach(card=>{
    const get=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.value:'';};
    const chk=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.checked:false;};
    const way=get('way');
    const cfg=collectConfig(card,way);
    let recipe={};if(way==='custom')recipe=collectRecipe(card);
    ways.push({way,enabled:chk('enabled'),config:cfg,max_retry:parseInt(get('max_retry'))||3,task_prompt:get('task_prompt'),recipe,template_id:get('template_id')});
  });
  return {name:document.getElementById('exp-name').value,description:document.getElementById('exp-desc').value,
    parallel:parseInt(document.getElementById('run-parallel').value)||5,ways};
}
document.getElementById('btn-add-way').onclick=()=>{
  const list=document.getElementById('ways-list');
  list.appendChild(renderWay({way:'pure_guide',enabled:true,config:{},max_retry:3}));
  saveExpAuto();
};
document.getElementById('btn-reset').onclick=()=>{confirmModal('重置当前实验配置？',()=>{fetch('/api/experiment').then(r=>r.json()).then(d=>{experiment=d;renderExp();});},'重置');};
document.getElementById('btn-run').onclick=()=>{
  const e=collectExp();const input=document.getElementById('run-input').value;const parallel=parseInt(document.getElementById('run-parallel').value)||5;
  if(!input.trim()){alertModal('请输入内容');return;}
  fetch('/api/experiment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(e)}).then(()=>fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({experiment:e,input,parallel})})).then(r=>r.json()).then(d=>{currentTaskId=d.task_id;document.getElementById('run-status').textContent='运行中...';document.getElementById('progress-fill').style.width='10%';if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(pollStatus,800);});
};
function pollStatus(){
  if(!currentTaskId)return;
  fetch('/api/run/status?id='+currentTaskId).then(r=>r.json()).then(d=>{
    document.getElementById('progress-fill').style.width=d.progress+'%';
    if(d.done){clearInterval(pollTimer);pollTimer=null;const el=document.getElementById('run-status');if(d.error){el.textContent='失败: '+d.error.slice(0,100);el.className='status fail';}else{el.textContent='完成';el.className='status ok';}renderResult(d.result);document.querySelector('.tab-btn[data-tab="result"]').click();loadHistory();}
  });
}
function renderResult(result){
  if(!result){document.getElementById('results-list').innerHTML='<p style="color:var(--text-dim)">无结果</p>';return;}
  const runs=result.runs||[];
  const rl=document.getElementById('results-list');rl.innerHTML='';
  runs.forEach(r=>{
    const div=document.createElement('div');div.className='run-block';
    let wrs=(r.way_results||[]).map(w=>{
      const callsHtml=(w.calls||[]).map((c,i)=>`<div style="margin:4px 0;padding:6px;background:var(--bg-panel);border-radius:4px">
        <div class="kv">[调用 ${i+1}] 耗时 <b>${c.elapsed}s</b> prompt_tokens=<b>${c.prompt_tokens}</b> response_tokens=<b>${c.response_tokens}</b></div>
        ${c.system_prompt?`<div class="kv">system: <b>${esc(c.system_prompt)}</b></div>`:''}
        <div class="kv">prompt:</div><pre style="margin:2px 0;white-space:pre-wrap;font-size:11px">${esc(c.prompt)}</pre>
        <div class="kv">返回:</div><pre style="margin:2px 0;white-space:pre-wrap;font-size:11px">${esc(c.response)}</pre></div>`).join('');
      const attHtml=(w.attempts||[]).map(a=>{const o=a||{};const reason=o.retry_reason||'';
        return `<div style="margin:4px 0;padding:6px;background:var(--bg-panel);border-radius:4px">
        <div class="kv">[attempt ${o.attempt!=null?o.attempt:'?'}] valid=<b>${o.valid!=null?o.valid:'?'}</b>${reason?` 重试理由=<b style="color:var(--accent)">${esc(reason)}</b>`:''}</div>
        ${o.raw!=null?`<div class="kv">raw: <span style="color:var(--text-dim)">${esc(String(o.raw).slice(0,200))}${String(o.raw).length>200?'…':''}</span></div>`:''}
        ${o.filled!=null?`<div class="kv">filled: <b>${esc(JSON.stringify(o.filled))}</b></div>`:''}
        ${o.fabricated&&o.fabricated.length?`<div class="kv">fabricated: <b style="color:var(--accent)">${esc(JSON.stringify(o.fabricated))}</b></div>`:''}
        ${o.missing_required&&o.missing_required.length?`<div class="kv">missing_required: <b style="color:#d68910">${esc(JSON.stringify(o.missing_required))}</b></div>`:''}
        ${o.flagged&&o.flagged.length?`<div class="kv">flagged: <b style="color:var(--accent)">${esc(JSON.stringify(o.flagged))}</b></div>`:''}</div>`;
      }).join('');
      const extraStr=Object.keys(w.extra||{}).length?`<pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(w.extra,null,2))}</pre>`:'';
      return `<div class="wr-block">
        <div class="wb-head">${esc(wayName(w.way))}
          <span class="badge ${w.success?'ok':'fail'}">${w.success?'成功':'失败'}</span>
          <span class="badge ${w.exhausted?'fail':'dim'}">${w.exhausted?'撑满上限':'未撑满'}</span>
          <span class="kv">重试 <b>${w.retry_count}</b> · 耗时 <b>${w.elapsed_total}s</b> · tokens <b>${w.total_tokens}</b></span>
        </div>
        <div class="config-header">LLM 调用（${(w.calls||[]).length} 次）</div>${callsHtml}
        <div class="config-header">attempt 记录（${(w.attempts||[]).length} 次，含重试理由）</div>${attHtml}
        <div class="config-header">最终填入</div>
        <pre>${esc(JSON.stringify(w.filled,null,2))}</pre>
        ${extraStr?`<div class="config-header">观测 extra</div>${extraStr}`:''}
        ${w.error?`<div style="color:var(--accent)">${esc(w.error)}</div>`:''}
      </div>`;
    }).join('');
    div.innerHTML=`<div class="rb-head"><span class="badge dim">run ${r.run_id}</span></div>${wrs||'<p class="kv">无方式结果</p>'}`;
    rl.appendChild(div);
  });
  const repro=result.reproducibility||[];const pl=document.getElementById('repro-list');pl.innerHTML='';
  if(!repro.length){pl.innerHTML='<p class="kv">无重现性数据</p>';return;}
  repro.forEach(rp=>{
    const div=document.createElement('div');div.className='wr-block';
    const m=result.metrics&&result.metrics[rp.way];
    const mStr=m&&Object.keys(m).length?`<div class="config-header" style="color:var(--accent)">验证指标（量化后置是否生效）</div><pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(m,null,2))}</pre>`:'';
    div.innerHTML=`<div class="wb-head">${esc(wayName(rp.way))} <span class="badge ${rp.consistency>=0.8?'ok':(rp.consistency>=0.5?'warn':'fail')}">一致率 ${rp.consistency}</span></div>
      <div class="kv">出现 ${rp.distinct_fills.length} 种不同填入：</div><pre>${esc(rp.distinct_fills.map(f=>{try{return JSON.stringify(JSON.parse(f),null,2);}catch(e){return f;}}).join('\n---\n'))}</pre>${mStr}`;
    pl.appendChild(div);
  });
}
document.getElementById('btn-refresh').onclick=()=>{if(currentTaskId)pollStatus();};
let e2eTaskId=null,e2eTimer=null;
document.getElementById('btn-e2e').onclick=()=>{
  const parallel=parseInt(document.getElementById('e2e-parallel').value)||3;
  fetch('/api/e2e_demo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parallel})}).then(r=>r.json()).then(d=>{
    e2eTaskId=d.task_id;document.getElementById('e2e-status').textContent='运行中...';document.getElementById('e2e-status').className='status';document.getElementById('e2e-progress').style.width='0%';
    if(e2eTimer)clearInterval(e2eTimer);
    e2eTimer=setInterval(()=>{
      fetch('/api/run/status?id='+e2eTaskId).then(r=>r.json()).then(s=>{
        document.getElementById('e2e-progress').style.width=s.progress+'%';
        if(s.result&&s.result.demo_results)renderE2E(s.result.demo_results);
        if(s.done){clearInterval(e2eTimer);e2eTimer=null;const el=document.getElementById('e2e-status');if(s.error){el.textContent='失败: '+s.error.slice(0,100);el.className='status fail';}else{el.textContent='完成';el.className='status ok';}document.querySelector('.tab-btn[data-tab="result"]').click();loadHistory();}
      });
    },1000);
  });
};
function renderE2E(results){
  const el=document.getElementById('e2e-list');el.innerHTML='';
  if(!results||!results.length){el.innerHTML='<p style="color:var(--text-dim)">无结果</p>';return;}
  results.forEach(r=>{
    const div=document.createElement('div');div.className='wr-block';div.style.cssText='background:var(--bg-input);border-radius:4px;padding:10px;margin-bottom:12px;font-size:12px';
    const runsHtml=(r.runs||[]).map(run=>{
      const callsHtml=(run.calls||[]).map((c,i)=>`<div style="margin:4px 0;padding:6px;background:var(--bg-panel);border-radius:4px">
        <div class="kv">[调用 ${i+1}] 耗时 <b>${c.elapsed}s</b> prompt_tokens=<b>${c.prompt_tokens}</b> response_tokens=<b>${c.response_tokens}</b></div>
        ${c.system_prompt?`<div class="kv">system: <b>${esc(c.system_prompt)}</b></div>`:''}
        <div class="kv">prompt:</div><pre style="margin:2px 0;white-space:pre-wrap;font-size:11px">${esc(c.prompt)}</pre>
        <div class="kv">返回:</div><pre style="margin:2px 0;white-space:pre-wrap;font-size:11px">${esc(c.response)}</pre></div>`).join('');
      const attHtml=(run.attempts||[]).map(a=>`<div style="margin:4px 0;padding:6px;background:var(--bg-panel);border-radius:4px">
        <div class="kv">[attempt ${a.attempt}] valid=<b>${a.valid}</b>${a.retry_reason?` 重试理由=<b style="color:var(--accent)">${esc(a.retry_reason)}</b>`:''}</div>
        <div class="kv">raw: <span style="color:var(--text-dim)">${esc((a.raw||'').slice(0,200))}${(a.raw||'').length>200?'…':''}</span></div>
        <div class="kv">filled: <b>${esc(JSON.stringify(a.filled))}</b></div>
        ${a.fabricated&&a.fabricated.length?`<div class="kv">fabricated: <b style="color:var(--accent)">${esc(JSON.stringify(a.fabricated))}</b></div>`:''}
        ${a.missing_required&&a.missing_required.length?`<div class="kv">missing_required: <b style="color:#d68910">${esc(JSON.stringify(a.missing_required))}</b></div>`:''}
        ${a.flagged&&a.flagged.length?`<div class="kv">flagged: <b style="color:var(--accent)">${esc(JSON.stringify(a.flagged))}</b></div>`:''}</div>`).join('');
      return `<div style="margin:8px 0;padding:8px;border:1px solid var(--border);border-radius:4px">
        <div class="wb-head"><b>run ${run.run_id}</b>
          <span class="badge ${run.success?'ok':'fail'}">${run.success?'成功':'失败'}</span>
          <span class="badge ${run.exhausted?'fail':'dim'}">${run.exhausted?'撑满上限':'未撑满'}</span>
          <span class="kv">重试 <b>${run.retry_count}</b> · 耗时 <b>${run.elapsed_total}s</b> · tokens <b>${run.total_tokens}</b></span></div>
        <div class="config-header">LLM 调用（${(run.calls||[]).length} 次）</div>${callsHtml}
        <div class="config-header">attempt 记录（${(run.attempts||[]).length} 次，含重试理由）</div>${attHtml}
        <div class="config-header">最终 filled</div>
        <pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(run.filled,null,2))}</pre>
        <div class="config-header">观测 extra</div>
        <pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(run.extra,null,2))}</pre>
        ${run.error?`<div style="color:var(--accent)">error: ${esc(run.error)}</div>`:''}</div>`;
    }).join('');
    const rp=r.reproducibility||{};
    const rpPre=rp.distinct_fills&&rp.distinct_fills.length?`<pre style="white-space:pre-wrap;font-size:11px">${esc(rp.distinct_fills.map(f=>{try{return JSON.stringify(JSON.parse(f),null,2);}catch(e){return f;}}).join('\n---\n'))}</pre>`:'';
    div.innerHTML=`<div class="wb-head"><b>${esc(r.way)} · ${esc(r.name)}</b>
      <span class="badge ${r.success_all?'ok':'fail'}">${r.success_all?'全部成功':'有失败'}</span>
      <span class="kv">并行 <b>${r.parallel}</b> · 总耗时 <b>${r.elapsed_all}s</b> · 总 tokens <b>${r.total_tokens_all}</b></span></div>
      <div class="kv">说明: ${esc(r.desc)}</div>
      <div class="kv">输入: <b>${esc(r.user_input)}</b></div>
      <div class="kv">task_prompt: <b>${esc(r.task_prompt)}</b></div>
      <div class="kv">recipe: <b>${esc(JSON.stringify(r.recipe))}</b></div>
      <div class="kv">config: <b>${esc(JSON.stringify(r.config))}</b> · max_retry=<b>${r.max_retry}</b></div>
      <div class="config-header" style="color:var(--accent)">重现性: consistency=<b>${rp.consistency}</b> · ${rp.distinct_fills?rp.distinct_fills.length:0} 种不同填入</div>${rpPre}
      ${r.metrics&&Object.keys(r.metrics).length?`<div class="config-header" style="color:var(--accent)">验证指标（量化后置是否生效）</div><pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(r.metrics,null,2))}</pre>`:''}
      <div class="config-header">各次运行（共 ${(r.runs||[]).length} 次）</div>${runsHtml}`;
    el.appendChild(div);
  });
}
function loadHistory(){
  fetch('/api/results').then(r=>r.json()).then(d=>{
    const list=document.getElementById('history-list');if(!list)return;
    const rs=d.results||[];
    if(!rs.length){list.innerHTML='<p style="color:var(--text-dim);font-size:12px;padding:8px 10px">暂无历史结果</p>';return;}
    list.innerHTML=rs.map(r=>{
      const tb=r.type==='e2e'?'<span class="h-type e2e">演示</span>':'<span class="h-type run">运行</span>';
      return `<div class="history-item" data-id="${esc(r.id)}" onclick="openHistory('${esc(r.id)}')">
        ${tb}<span class="h-del" onclick="event.stopPropagation();deleteHistory(this,'${esc(r.id)}')" title="删除">✕</span>
        <span class="h-summary">${esc(r.summary||'')}</span>
        <span class="h-date">${esc(r.saved_at||'')}</span></div>`;
    }).join('');
  });
}
function openHistory(id){
  document.querySelectorAll('.history-item').forEach(el=>el.classList.toggle('active',el.dataset.id===id));
  fetch('/api/results/read?id='+encodeURIComponent(id)).then(r=>r.json()).then(d=>{
    if(!d||!d.type)return;
    document.querySelector('.tab-btn[data-tab="result"]').click();
    if(d.type==='e2e'){renderE2E(d.result||[]);document.getElementById('results-list').innerHTML='<p class="kv">演示结果见下方「端到端演示结果」</p>';document.getElementById('repro-list').innerHTML='';}
    else{renderResult(d.result||{});document.getElementById('e2e-list').innerHTML='<p class="kv">运行结果见上方「实验结果」</p>';}
  });
}
function deleteHistory(btn,id){
  if(btn.dataset.confirming!=='true'){btn.dataset.confirming='true';btn.textContent='确认?';btn.style.background='#e74c3c';btn.style.color='#fff';
    const c=document.createElement('span');c.className='h-del-cancel';c.textContent='取消';c.onclick=function(e){e.stopPropagation();btn.dataset.confirming='false';btn.textContent='✕';btn.style.background='';btn.style.color='';c.remove();};btn.parentNode.insertBefore(c,btn.nextSibling);return;}
  fetch('/api/results/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}).then(r=>r.json()).then(()=>loadHistory());
}
document.getElementById('btn-clear-history').onclick=()=>{confirmModal('清空全部历史结果？不可恢复',()=>{fetch('/api/results/clear',{method:'POST'}).then(r=>r.json()).then(()=>loadHistory());},'清空');};
loadLLMConfig();loadWays();loadHistory();
</script>
</body>
</html>
""";


def run_server(host="0.0.0.0", port=8805, backend="",
               base_url="", model="", api_key="", pidfile=""):
    if backend:
        config_mgr.set("llm.backend", backend)
    if base_url:
        config_mgr.set("llm.base_url", base_url)
    if model:
        config_mgr.set("llm.model", model)
    if api_key:
        config_mgr.set("llm.api_key", api_key)
    if pidfile:
        try:
            with open(pidfile, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass
    server = ThreadingHTTPServer((host, port), SilPrespecEmulatorHandler)
    cur_backend = config_mgr.get("llm.backend", "lm-studio")
    cur_url = config_mgr.resolve_base_url()
    cur_model = config_mgr.get("llm.model", "")
    print(f"[silprespec-emulator] 服务启动: http://{host}:{port}")
    print(f"[silprespec-emulator] 后端: {cur_backend} | base_url: {cur_url} | model: {cur_model or '(自动)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[silprespec-emulator] 服务停止")
        server.server_close()
    finally:
        if pidfile:
            try:
                os.remove(pidfile)
            except Exception:
                pass
