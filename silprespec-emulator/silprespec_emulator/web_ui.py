"""Web UI — 前置规范效果实验台

界面：深色主题（对齐 structured-writer），三 Tab：
  - 配置：从 8 种前置规范方式中选一种或多种，各方式配置 + 并行数
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
from .atoms import WAY_RECIPES

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_EXP_FILE = _DATA_DIR / "experiment.json"

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
            self._send(200, {"ways": [{"id": w[0], "name": w[1], "desc": w[2],
                                       "help": WAY_HELPS.get(w[0], ""),
                                       "default_config": default_config(w[0]),
                                       "default_task_prompt": TASK_PROMPTS.get(w[0], ""),
                                       "default_recipe": (WAY_RECIPES[w[0]].to_dict() if w[0] in WAY_RECIPES else {})} for w in WAYS],

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
            with _run_lock:
                task["result"] = result; task["progress"] = 100; task["done"] = True; task["running"] = False
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
<title>silprespec-emulator · 前置规范效果模拟器</title>
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
  <span class="tag">前置规范效果模拟器</span>
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
        <span style="flex:1"></span>
        <button class="btn btn-sm btn-success" id="btn-save-llm">保存后端配置</button>
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
      <button class="btn btn-primary" id="btn-save">保存</button>
      <button class="btn btn-secondary" id="btn-reset">重置</button>
      <span id="save-status" class="status" style="margin-left:12px"></span>
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
      <h3>说明</h3>
      <p style="font-size:13px;color:var(--text-dim);line-height:1.7">
        • 从 8 种前置规范方式中选一种或多种，对输入真实执行（LLM 真填空），观测<b>填入了什么</b>。<br>
        • 指标：填入内容、重试次数、是否撑满 max_retry、撑满失败次数、命中/留空分布。<br>
        • 并行 N 次观测<b>重现性</b>（各方式跨 run 填入一致率）。<br>
        • 8 种都是前置规范（生成通道/填空出口）；后置验证（任务完成后全量验证）不在本系统。
      </p>
    </div>
  </div>
</div>

<div class="tab-content" id="tab-result">
  <div class="panel">
    <div class="section"><h3>实验结果 <button class="btn btn-sm btn-secondary" id="btn-refresh" style="margin-left:12px">刷新</button></h3><div id="results-list"><p style="color:var(--text-dim);font-size:13px">尚未运行。</p></div></div>
    <div class="section"><h3>重现性</h3><div id="repro-list"></div></div>
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
let experiment=null, waysMeta=null, customTemplates=null, currentTaskId=null, pollTimer=null;
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
};
document.getElementById('btn-refresh-models').onclick=()=>refreshModels(document.getElementById('llm-model').value);
document.getElementById('btn-test-conn').onclick=()=>{
  const llm={backend:document.getElementById('llm-backend').value,base_url:document.getElementById('llm-base-url').value,model:document.getElementById('llm-model').value};
  fetch('/api/backend/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(llm)}).then(r=>r.json()).then(d=>{const el=document.getElementById('conn-status');el.textContent=d.ok?'已连接':'连接失败';el.className='status '+(d.ok?'ok':'fail');if(!d.ok)el.textContent+=': '+d.message;});
};
document.getElementById('btn-save-llm').onclick=()=>{
  const llm={backend:document.getElementById('llm-backend').value,base_url:document.getElementById('llm-base-url').value,model:document.getElementById('llm-model').value,timeout:parseInt(document.getElementById('llm-timeout').value)||120,max_tokens:parseInt(document.getElementById('llm-max-tokens').value)||4096,temperature:parseFloat(document.getElementById('llm-temperature').value)||0.7};
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({llm})}).then(r=>r.json()).then(()=>{const el=document.getElementById('conn-status');el.textContent='已保存';el.className='status ok';setTimeout(()=>el.textContent='',2000);});
};
function loadWays(){fetch('/api/ways').then(r=>r.json()).then(d=>{waysMeta=d.ways;customTemplates=d.custom_templates||[];renderTemplateLibrary();loadExp();});}
function loadExp(){fetch('/api/experiment').then(r=>r.json()).then(d=>{experiment=d;renderExp();});}
function renderExp(){
  document.getElementById('exp-name').value=experiment.name||'';
  document.getElementById('exp-desc').value=experiment.description||'';
  const rp=document.getElementById('run-parallel');if(rp)rp.value=experiment.parallel||5;
  const list=document.getElementById('ways-list');list.innerHTML='';
  (experiment.ways||[]).forEach(w=>list.appendChild(renderWay(w)));
}
function wayName(id){return(waysMeta||[]).find(w=>w.id===id)?.name||id;}
function renderWay(w){
  const card=document.createElement('div');card.className='way-card';
  const meta=(waysMeta||[]).find(x=>x.id===w.way)||{desc:'',help:''};
  const tmplOpts=(customTemplates||[]).map(t=>`<option value="custom" data-tmpl="${t.id}" ${w.way==='custom'&&w.template_id===t.id?'selected':''}>★ ${esc(t.name)}</option>`).join('');
  card.innerHTML=`
    <div class="wc-head">
      <select data-w="way"><option value="custom" ${w.way==='custom'&&!w.template_id?'selected':''}>自定义模板（临时）</option>${tmplOpts}${(waysMeta||[]).map(x=>`<option value="${x.id}" ${x.id===w.way?'selected':''}>${x.name}</option>`).join('')}</select>
      <span class="wc-desc">${esc(meta.desc)}</span>
      <label class="checkbox-row"><input type="checkbox" data-w="enabled" ${w.enabled?'checked':''}>启用</label>

      <input type="number" data-w="max_retry" value="${w.max_retry||3}" min="0" max="10" style="width:70px" title="max_retry">
      <button class="btn btn-sm btn-secondary" data-act="saveas-tmpl">另存为模板</button>
      <button class="btn btn-sm btn-success tmpl-acts" data-act="save-tmpl" style="display:${w.way==='custom'&&w.template_id?'inline-block':'none'}">更新模板</button>
      <button class="btn btn-sm btn-danger" data-act="del">删除</button>
    </div>
    <input type="hidden" data-w="template_id" value="${esc(w.template_id||'')}">
    <div class="form-row"><label>配置JSON</label><textarea data-w="config" rows="6">${esc(JSON.stringify(w.config||{},null,2))}</textarea></div>
    <div class="form-row"><label>任务提示词（系统提示词）</label><textarea data-w="task_prompt" rows="2">${esc(w.task_prompt||meta.default_task_prompt||'')}</textarea></div>
    <div class="recipe-block" style="display:${w.way==='custom'?'block':'none'}">
      <div class="form-row"><label>原子配方JSON（自定义模板）</label><textarea data-w="recipe" rows="6" placeholder='{"generate":"text","postprocess":[],"validate":"none","retry":false,"observe":[]}'>${esc(JSON.stringify(w.recipe||{},null,2))}</textarea></div>
    </div>
    <details style="margin-top:8px"><summary style="cursor:pointer;color:var(--text-dim);font-size:12px">📖 说明+示例</summary><pre class="way-help" style="background:#0d0d1f;padding:8px;border-radius:4px;font-size:11px;white-space:pre-wrap;word-break:break-word;margin-top:6px;max-height:300px;overflow-y:auto">${esc(meta.help||'')}</pre></details>
  `;
  card.querySelector('[data-act="del"]').onclick=()=>card.remove();
  card.querySelector('[data-act="save-tmpl"]').onclick=()=>saveAsTemplate(card,'update');
  card.querySelector('[data-act="saveas-tmpl"]').onclick=()=>saveAsTemplate(card,'saveAs');
  card.querySelector('[data-w="way"]').onchange=(e)=>{
    const sel=e.target.selectedOptions[0];
    const tmplId=sel.getAttribute('data-tmpl')||'';
    const isCustom=e.target.value==='custom';
    card.querySelector('[data-w="template_id"]').value=tmplId;
    const upBtn=card.querySelector('[data-act="save-tmpl"]');if(upBtn)upBtn.style.display=(isCustom&&tmplId)?'inline-block':'none';
    if(isCustom&&tmplId){
      const t=(customTemplates||[]).find(x=>x.id===tmplId)||{};
      card.querySelector('.wc-desc').textContent=t.name?('模板：'+t.name):'自定义原子组合';
      card.querySelector('.way-help').textContent='';
      card.querySelector('[data-w="config"]').value=JSON.stringify(t.default_config||{},null,2);
      card.querySelector('[data-w="task_prompt"]').value=t.task_prompt||'';
      card.querySelector('[data-w="recipe"]').value=JSON.stringify(t.recipe||{},null,2);
    }else if(isCustom){
      card.querySelector('.wc-desc').textContent='自定义原子组合';
      card.querySelector('.way-help').textContent='';
      card.querySelector('[data-w="config"]').value='{}';
      card.querySelector('[data-w="task_prompt"]').value='';
      card.querySelector('[data-w="recipe"]').value='{}';
    }else{
      const nm=(waysMeta||[]).find(x=>x.id===e.target.value)||{desc:'',help:'',default_config:{}};
      card.querySelector('.wc-desc').textContent=nm.desc;
      card.querySelector('.way-help').textContent=nm.help||'';
      card.querySelector('[data-w="config"]').value=JSON.stringify(nm.default_config||{},null,2);
      card.querySelector('[data-w="task_prompt"]').value=nm.default_task_prompt||'';
    }
    const rb=card.querySelector('.recipe-block');if(rb)rb.style.display=isCustom?'block':'none';
  };
  return card;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function rebuildWayDropdown(card,selTmplId){
  const sel=card.querySelector('[data-w="way"]');const cur=sel.value;
  const tmplOpts=(customTemplates||[]).map(t=>`<option value="custom" data-tmpl="${t.id}" ${selTmplId===t.id?'selected':''}>★ ${esc(t.name)}</option>`).join('');
  sel.innerHTML=`<option value="custom" ${cur==='custom'&&!selTmplId?'selected':''}>自定义模板（临时）</option>${tmplOpts}${(waysMeta||[]).map(x=>`<option value="${x.id}" ${x.id===cur&&cur!=='custom'?'selected':''}>${x.name}</option>`).join('')}`;
}
function saveAsTemplate(card,mode){
  const get=f=>{const el=card.querySelector(`[data-w="${f}"]`);return el?el.value:'';};
  const way=get('way');
  let recipe={};
  if(way==='custom'){try{recipe=JSON.parse(get('recipe'));}catch(e){alertModal('配方JSON解析失败');return;}}
  else{const m=(waysMeta||[]).find(x=>x.id===way);recipe=(m&&m.default_recipe)||{};}
  let cfg={};try{cfg=JSON.parse(get('config'));}catch(e){}
  const taskPrompt=get('task_prompt');
  const curTmplId=get('template_id');
  const doSave=(name)=>{
    const body={id:mode==='update'?curTmplId:'',name:name||'',recipe,task_prompt:taskPrompt,default_config:cfg};
    fetch('/api/custom_templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).then(d=>{
      if(!d.ok){alertModal('保存失败');return;}
      customTemplates=d.custom_templates||[];
      card.querySelector('[data-w="template_id"]').value=d.id;
      const waySel=card.querySelector('[data-w="way"]');
      if(way!=='custom'){waySel.value='custom';const rb=card.querySelector('.recipe-block');if(rb)rb.style.display='block';card.querySelector('[data-w="recipe"]').value=JSON.stringify(recipe,null,2);}
      const upBtn=card.querySelector('[data-act="save-tmpl"]');if(upBtn)upBtn.style.display='inline-block';
      rebuildWayDropdown(card,d.id);
      renderTemplateLibrary();
      const el=document.getElementById('save-status');el.textContent='模板已保存：'+(name||'已更新');el.className='status ok';setTimeout(()=>el.textContent='',2000);
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
    let cfg={};try{cfg=JSON.parse(get('config'));}catch(e){}
    let recipe={};if(get('way')==='custom'){try{recipe=JSON.parse(get('recipe'));}catch(e){}}
    ways.push({way:get('way'),enabled:chk('enabled'),config:cfg,max_retry:parseInt(get('max_retry'))||3,task_prompt:get('task_prompt'),recipe,template_id:get('template_id')});
  });
  return {name:document.getElementById('exp-name').value,description:document.getElementById('exp-desc').value,
    parallel:parseInt(document.getElementById('run-parallel').value)||5,ways};
}
document.getElementById('btn-save').onclick=()=>{const e=collectExp();fetch('/api/experiment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(e)}).then(r=>r.json()).then(()=>{const el=document.getElementById('save-status');el.textContent='已保存';el.className='status ok';setTimeout(()=>el.textContent='',2000);});};
document.getElementById('btn-add-way').onclick=()=>{
  const list=document.getElementById('ways-list');
  list.appendChild(renderWay({way:'gate',enabled:true,config:{},max_retry:3}));
};
document.getElementById('btn-reset').onclick=()=>{confirmModal('重置当前实验配置？',()=>{fetch('/api/experiment').then(r=>r.json()).then(d=>{experiment=d;renderExp();});},'重置');};
document.getElementById('run-parallel').onchange=()=>{};
document.getElementById('btn-run').onclick=()=>{
  const e=collectExp();const input=document.getElementById('run-input').value;const parallel=parseInt(document.getElementById('run-parallel').value)||5;
  if(!input.trim()){alertModal('请输入内容');return;}
  fetch('/api/experiment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(e)}).then(()=>fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({experiment:e,input,parallel})})).then(r=>r.json()).then(d=>{currentTaskId=d.task_id;document.getElementById('run-status').textContent='运行中...';document.getElementById('progress-fill').style.width='10%';if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(pollStatus,800);});
};
function pollStatus(){
  if(!currentTaskId)return;
  fetch('/api/run/status?id='+currentTaskId).then(r=>r.json()).then(d=>{
    document.getElementById('progress-fill').style.width=d.progress+'%';
    if(d.done){clearInterval(pollTimer);pollTimer=null;const el=document.getElementById('run-status');if(d.error){el.textContent='失败: '+d.error.slice(0,100);el.className='status fail';}else{el.textContent='完成';el.className='status ok';}renderResult(d.result);document.querySelector('.tab-btn[data-tab="result"]').click();}
  });
}
function renderResult(result){
  if(!result){document.getElementById('results-list').innerHTML='<p style="color:var(--text-dim)">无结果</p>';return;}
  const runs=result.runs||[];
  const rl=document.getElementById('results-list');rl.innerHTML='';
  runs.forEach(r=>{
    const div=document.createElement('div');div.className='run-block';
    let wrs=(r.way_results||[]).map(w=>{
      const filledStr=JSON.stringify(w.filled,null,2);
      const attemptsStr=(w.attempts||[]).map((a,i)=>{const parts=Object.entries(a).map(([k,v])=>typeof v==='object'&&v!==null?`${k}=${JSON.stringify(v)}`:`${k}=${v}`);return `  尝试${i+1}/${w.attempts.length}: ${parts.join(' · ')}`;}).join('\n');
      const extraStr=Object.keys(w.extra||{}).length?`<div class="kv">${Object.entries(w.extra).map(([k,v])=>`${k}=<b>${esc(JSON.stringify(v))}</b>`).join(' · ')}</div>`:'';
      return `<div class="wr-block">
        <div class="wb-head">${esc(wayName(w.way))}
          <span class="badge ${w.success?'ok':'fail'}">${w.success?'成功':'失败'}</span>
          <span class="badge ${w.exhausted?'fail':'dim'}">${w.exhausted?'撑满失败':'未撑满'}</span>
          <span class="kv">重试 <b>${w.retry_count}</b></span>
        </div>
        <div class="kv">最终填入：</div><pre>${esc(filledStr)}</pre>
        ${attemptsStr?`<div class="kv">每次尝试（偏移方向）：</div><pre>${esc(attemptsStr)}</pre>`:''}
        ${extraStr}
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
    div.innerHTML=`<div class="wb-head">${esc(wayName(rp.way))} <span class="badge ${rp.consistency>=0.8?'ok':(rp.consistency>=0.5?'warn':'fail')}">一致率 ${rp.consistency}</span></div>
      <div class="kv">出现 ${rp.distinct_fills.length} 种不同填入：</div><pre>${esc(rp.distinct_fills.map(f=>{try{return JSON.stringify(JSON.parse(f),null,2);}catch(e){return f;}}).join('\n---\n'))}</pre>`;
    pl.appendChild(div);
  });
}
document.getElementById('btn-refresh').onclick=()=>{if(currentTaskId)pollStatus();};
loadLLMConfig();loadWays();
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
