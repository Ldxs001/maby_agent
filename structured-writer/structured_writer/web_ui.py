"""Web UI — HTTP 服务器 + 内联 HTML/CSS/JS 界面"""
import json
import copy
import os
import sys
import time
import tempfile
import subprocess
import threading
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socketserver
import urllib.parse
import urllib.request
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional

from .config_manager import ConfigManager, BUILTIN_TEMPLATE_NAMES
from .llm_client import LLMClient, LLMClientError
from .state_manager import StateManager
from .planner import plan_outline, generate_template, replan_section, adapt_outline
from .writer import generate_article
from .plugins import get_plugin_manager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 后台生成任务跟踪 {session_id: {"thread": Thread, "done": bool, "result": dict|None, "error": str|None}}
_generation_tasks = {}
_gen_lock = threading.Lock()

# 修复引擎状态（T1 整段重构后台线程）
_repair_states: dict = {}  # session_id → {done, running, result, chapter, session_id}（b25 会话隔离）
_repair_lock = threading.Lock()

# 小说模型后台安装状态（点击「安装缺失模型」→ 后端自动下载，不弹窗不自装）
_install_state = {"running": False, "models": [], "log": [], "done": False}
_INSTALL_CMDS = {
    "r1": ['python -c "from transformers import AutoModel; AutoModel.from_pretrained(\'deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B\', trust_remote_code=True)"'],
    "qwen25": ['python -c "from transformers import AutoModel; AutoModel.from_pretrained(\'Qwen/Qwen2.5-3B-Instruct\', trust_remote_code=True)"'],
}
_INSTALL_LABELS = {"r1": "推理R1", "qwen25": "实体抽取Qwen2.5-3B"}
# LM Studio 判定模型（GGUF，hf-mirror 下载进 LM Studio 模型库 ~/.lmstudio/models/）：
# 4维 = Qwen3-8B Q4_K_M（5GB）；R1 = DeepSeek-R1-Distill-Qwen-7B Q4（4.4GB）
_GGUF_MODELS = {
    "gguf_4dim": {"repo": "Qwen/Qwen3-8B-GGUF", "file": "Qwen3-8B-Q4_K_M.gguf",
                  "label": "4维判定Qwen3-8B Q4_K_M"},
    "gguf_r1": {"repo": "unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF", "file": "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf",
                "label": "推理审核R1-Distill-7B Q4_K_M"},
}


def _run_model_install(models: list):
    """后台下载缺失模型（hf-mirror；transformers 格式或 GGUF），日志写 _install_state。"""
    global _install_state
    log = []
    def _log(msg):
        log.append(msg)
        if len(log) > 60:
            log.pop(0)
    for m in models:
        _log(f"[{_INSTALL_LABELS.get(m, _GGUF_MODELS.get(m, {}).get('label', m))}] 开始下载...")
        if m in _GGUF_MODELS:
            # GGUF：hf_hub_download 拉文件进 LM Studio 模型库（hf-mirror 强制）——
            # local_dir = 模型库根目录 → 自动落 <publisher>/<repo>/<file>，与 LM Studio 目录结构一致；
            # 下载后 lms import 注册（LM Studio 未自动扫描时兜底）。
            # 三保险走镜像：①环境变量强制覆盖（setdefault 遇已有值不生效，会走官方超时）
            # ②huggingface_hub.constants.ENDPOINT 直接覆盖（库在 import 时缓存，改 env 无效）
            # ③hf_hub_download 显式 endpoint 参数（旧版库无此参数则 TypeError 兜底）
            spec = _GGUF_MODELS[m]
            try:
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"  # 强制，非 setdefault
                os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
                try:
                    import huggingface_hub.constants as _hfc
                    _hfc.ENDPOINT = "https://hf-mirror.com"
                except Exception:
                    pass
                from huggingface_hub import hf_hub_download
                from .novel import model_backend as mb
                from .novel import lmstudio_probe
                # 已存在（LM Studio 库）→ 跳过下载
                if lmstudio_probe.gguf_exists(spec["repo"], spec["file"]):
                    _log(f"  已存在: {lmstudio_probe.gguf_expected_path(spec['repo'], spec['file'])}")
                    continue
                target_dir = mb.default_gguf_dir()  # = LM Studio 模型库根目录
                target_dir.mkdir(parents=True, exist_ok=True)
                _log(f"  repo: {spec['repo']} file: {spec['file']} (hf-mirror → LM Studio 模型库)")
                try:
                    path = hf_hub_download(spec["repo"], spec["file"], local_dir=str(target_dir),
                                           endpoint="https://hf-mirror.com")
                except TypeError:
                    path = hf_hub_download(spec["repo"], spec["file"], local_dir=str(target_dir))
                _log(f"  OK → {path}")
                # 注册进 LM Studio（幂等；失败不阻断——LM Studio 可能自动扫描到）
                try:
                    ok_imp, msg_imp = lmstudio_probe.lms_import(path, spec["repo"], copy=True)
                    _log(f"  lms import: {'OK' if ok_imp else msg_imp[:120]}")
                except Exception as e:
                    _log(f"  lms import 跳过: {e}")
            except Exception as e:
                _log(f"  [ERROR] {type(e).__name__}: {e}")
            continue
        for cmd in _INSTALL_CMDS.get(m, []):
            _log(f"  执行: {cmd[:90]}")
            try:
                env = os.environ.copy()
                env["HF_ENDPOINT"] = "https://hf-mirror.com"
                p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                   timeout=3600, env=env)
                tail = [ln for ln in (p.stdout or "").strip().split("\n") if ln.strip()][-2:]
                if p.returncode == 0:
                    _log("  OK")
                else:
                    err = [ln for ln in (p.stderr or "").strip().split("\n") if ln.strip()][-2:]
                    _log(f"  [ERROR] rc={p.returncode} " + (" | ".join(err) if err else ""))
            except Exception as e:
                _log(f"  [ERROR] {type(e).__name__}: {e}")
    with _gen_lock:
        _install_state = {"running": False, "models": models, "log": log, "done": True}

# RAG 子进程管理
_rag_process = None
_rag_process_stderr = ""
_rag_lock = threading.Lock()
_rag_starting = False  # True while cold start is in progress

# 批量自动撰写任务跟踪
_batch_tasks = {}
_batch_lock = threading.Lock()

# 停止生成标记 {session_id: "delay"|"immediate"}
_stop_flags = {}
_stop_lock = threading.Lock()

# MiKTeX bin 路径探测与 PATH 注入（winget 装完 MiKTeX 不会自动更新当前进程 PATH）
_MIKTEX_BIN_CANDIDATES = [
    r"C:\Program Files\MiKTeX\miktex\bin\x64",
    r"C:\Program Files (x86)\MiKTeX\miktex\bin",
    os.path.expanduser(r"~\AppData\Local\Programs\MiKTeX\miktex\bin\x64"),
]
for _mb in _MIKTEX_BIN_CANDIDATES:
    if os.path.exists(os.path.join(_mb, "lualatex.exe")) and _mb not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _mb + os.pathsep + os.environ["PATH"]


class StructuredWriterHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    config_mgr = None
    _lock = threading.Lock()

    # ---- 路由表 ----
    ROUTES = {
        "GET": {},
        "POST": {},
        "PUT": {}
    }

    @classmethod
    def _init_routes(cls):
        if cls.ROUTES["GET"]:
            return
        cls.ROUTES["GET"] = {
            "/": cls._handle_index,
            "/favicon.ico": cls._handle_favicon,
            "/api/config": cls._handle_get_config,
            "/api/llm/test": cls._handle_llm_test,
            "/api/llm/models": cls._handle_llm_models,
            "/api/llm/window": cls._handle_llm_window,
            "/api/progress": cls._handle_get_progress,
            "/api/result": cls._handle_get_result,
            "/api/sessions": cls._handle_list_sessions,
            "/api/session/load": cls._handle_session_load,
            "/api/rag/status": cls._handle_rag_status,
            "/api/novel/status": cls._handle_novel_status,
            "/api/novel/replan_status": cls._handle_novel_replan_status,
            "/api/novel/repair/preview": cls._handle_repair_preview,
            "/api/novel/repair/status": cls._handle_repair_status,
            "/api/batch_progress": cls._handle_batch_progress,
            "/api/outputs": cls._handle_outputs_list,
            "/api/outputs/read": cls._handle_outputs_read,
            "/api/outputs/merge": cls._handle_outputs_merge,
            "/api/outputs/texpdf": cls._handle_outputs_texpdf,
            "/api/examples": cls._handle_list_examples,
            "/api/plugins": cls._handle_list_plugins,
        }
        cls.ROUTES["POST"] = {
            "/api/config": cls._handle_update_config,
            "/api/outputs/delete": cls._handle_outputs_delete,
            "/api/plan": cls._handle_plan,
            "/api/generate": cls._handle_generate,
            "/api/session/new": cls._handle_new_session,
            "/api/chat": cls._handle_chat,
            "/api/rag/start": cls._handle_rag_start,
            "/api/rag/stop": cls._handle_rag_stop,
            "/api/novel/install": cls._handle_novel_install,
            "/api/novel/checks": cls._handle_novel_checks,
            "/api/novel/confirm": cls._handle_novel_confirm,
            "/api/novel/replan_sub": cls._handle_novel_replan_sub,
            "/api/novel/repair/apply": cls._handle_repair_apply,
            "/api/novel/repair/rollback": cls._handle_repair_rollback,
            "/api/novel/repair/skip": cls._handle_repair_skip,
            "/api/batch_auto": cls._handle_batch_auto,
            "/api/session/archive": cls._handle_session_archive,
            "/api/session/restore": cls._handle_session_restore,
            "/api/session/delete": cls._handle_session_delete,
            "/api/stop": cls._handle_stop,
            "/api/gen-template": cls._handle_gen_template,
            "/api/aux_upload": cls._handle_aux_upload,
            "/api/example/save": cls._handle_save_example,
            "/api/example/update_article": cls._handle_update_example_article,
            "/api/example/use": cls._handle_use_example,
            "/api/replan_section": cls._handle_replan_section,
            "/api/plugin/run": cls._handle_plugin_run,
        }

    def do_GET(self):
        self._init_routes()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        handler = self.ROUTES["GET"].get(path)
        if handler:
            handler(self)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())

    def do_POST(self):
        self._init_routes()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        handler = self.ROUTES["POST"].get(path)
        try:
            if handler:
                handler(self)
            else:
                self._json_response({"error": "Not found"}, 404)
        except Exception as e:
            try:
                self._json_response({"success": False, "error": str(e)}, 500)
            except Exception:
                pass

    # ---- 辅助方法 ----

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        # 容错：先 utf-8，失败则 latin-1（保 byte 不变）
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # 返回错误信息并让调用方处理
            raise ValueError(f"JSON 解析失败: {e}, 原始内容: {text[:200]}")

    def _json_response(self, data: dict, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        # 显式 Content-Length：120KB 内联页面无边界响应会被浏览器/代理截断
        # （症状：CSS 开头生效=无滚动轴、尾部 JS 丢失=tab 无响应、布局竖排）
        self.send_header("Content-Length", str(len(body)))
        # 禁缓存：INDEX_HTML 随代码更新，缓存旧页面会导致样式/功能错乱
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    # ---- 首页 ----

    def _handle_index(self):
        self._html_response(INDEX_HTML)

    def _handle_favicon(self):
        self.send_response(204)
        self.end_headers()

    # ---- 配置 API ----

    def _handle_get_config(self):
        cfg = self.config_mgr.get_all()
        # LM Studio 环境注入 → 前端显示统一管理可用性与模型库状态（llama.cpp 已废弃）
        try:
            from .novel import lmstudio_probe
            p = lmstudio_probe.probe_lmstudio()
            cfg["lmstudio"] = {"available": bool(p.get("lms_ok")),
                               "server_ok": bool(p.get("server_ok")),
                               "models_dir": p.get("models_dir") or "",
                               "reason": p.get("reason", "")}
        except Exception:
            cfg["lmstudio"] = {"available": False, "server_ok": False,
                               "models_dir": "", "reason": "探测异常"}
        self._json_response({"success": True, "config": cfg})

    def _handle_update_config(self):
        data = self._read_body()
        # 处理模板删除（透传给 config_manager）
        if "_delete_template" in data:
            name = data.pop("_delete_template")
            if name and name not in BUILTIN_TEMPLATE_NAMES:
                user_tpls = self.config_mgr._load_user_templates()
                if name in user_tpls:
                    del user_tpls[name]
                    self.config_mgr._save_user_templates(user_tpls)
        self.config_mgr.update(data)
        self._json_response({"success": True})

    # ---- LLM 客户端工厂（统一创建，一处改处处生效） ----

    @staticmethod
    def _model_profile(model_cfg: dict) -> dict:
        """按后端取配置槽（profiles 分槽结构，用户需求：切后端自动恢复对应落盘配置）。

        实现见 model_backend._model_profile（单一实现，repair engine 共用）。
        """
        from .novel.model_backend import _model_profile as _mb_profile
        return _mb_profile(model_cfg)

    @classmethod
    def _create_writer_client(cls):
        wm = cls._model_profile(cls.config_mgr.get("writer_model", {}))
        return LLMClient(
            backend=(cls.config_mgr.get("writer_model", {}) or {}).get("backend", "lmstudio"),
            base_url=wm.get("base_url", "http://localhost:1234"),
            timeout=wm.get("timeout", 300),
            model=wm.get("model", ""),
            max_tokens=wm.get("max_tokens", 8192),
            temperature=wm.get("temperature", 0.7)
        )

    @classmethod
    def _create_planner_client(cls):
        pm = cls._model_profile(cls.config_mgr.get("planner_model", {}))
        return LLMClient(
            backend=(cls.config_mgr.get("planner_model", {}) or {}).get("backend", "lmstudio"),
            base_url=pm.get("base_url", "http://localhost:1234"),
            timeout=pm.get("timeout", 180),
            model=pm.get("model", ""),
            max_tokens=pm.get("max_tokens", 4096),
            temperature=pm.get("temperature", 0.6)
        )

    # ---- LLM API ----

    def _handle_llm_test(self):
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        backend = (params.get("backend") or ["lmstudio"])[0]
        base_url = (params.get("base_url") or ["http://localhost:1234"])[0]
        client = LLMClient(backend=backend, base_url=base_url)
        ok, msg = client.test_connection()
        self._json_response({"success": ok, "message": msg})

    def _handle_llm_models(self):
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        backend = (params.get("backend") or ["lmstudio"])[0]
        base_url = (params.get("base_url") or ["http://localhost:1234"])[0]
        client = LLMClient(backend=backend, base_url=base_url)
        models = client.list_models()
        self._json_response({"success": True, "models": models})

    def _handle_llm_window(self):
        """窗口信息：llama.cpp 已废弃（LM Studio/Ollama 由服务端管理窗口）→ 恒返回空。"""
        self._json_response({"success": True, "n_ctx": None, "read_space": None,
                             "warn": None, "error": None})

    # ---- 大纲 API ----

    def _handle_plan(self):
        data = self._read_body()
        topic = data.get("topic", "").strip()
        if not topic:
            self._json_response({"success": False, "error": "主题不能为空"}, 400)
            return

        prompt = data.get("prompt", "") or self.config_mgr.get("default_prompt", "")
        session_id = data.get("session_id", "")

        # 获取当前选中的模板
        templates = self.config_mgr.get("templates", {})
        selected = data.get("template_name", "") or self.config_mgr.get("selected_template", "")
        template = templates.get(selected, {})

        # 用户已填的字段值（标题、作者等）
        user_meta = data.get("meta", {})

        # 小说线：题材必填（题材=场景配置/世界观根，缺失整篇漂移；篇幅可不填，novel_bridge 默认中篇）
        if (isinstance(template, dict) and (template.get("novel") or {}).get("mode")
                and not str((user_meta or {}).get("题材", "")).strip()):
            self._json_response({"success": False,
                                 "error": "小说需填写「题材」（如 科幻/武侠/悬疑/都市/奇幻/历史）——题材决定场景配置与世界观，缺失会导致 AI 瞎编。篇幅可不填（默认中篇）。"}, 400)
            return

        plan_hints = data.get("plan_hints", "")

        # 规划前先落 session（存用户要求）——规划可能耗时数分钟（LLM 生成场景配置/大纲），
        # 期间用户切走/切回必须能看到要求；规划失败 session 也保留（phase=config + 要求，可重试）。
        # 去重：经 /api/chat 的 writing_request → addOutlineProposal → startPlanning 链路时，
        # 该消息已在 _handle_chat 存过，避免重复。
        sm = StateManager(session_id) if session_id else StateManager()
        sm.init_session(self.config_mgr.get_all())
        _last = (sm.get_state().get("messages") or [None])[-1]
        if not _last or _last.get("content") != topic:
            sm.append_message("user", topic)

        # 获取规划模型配置
        client = self._create_planner_client()

        try:
            # 统一管理：规划模型装载 → 规划 → 卸载（一次一个模型；ollama/非 lmstudio 空跑）
            # 独占串行开（默认）：规划完即卸载（规划/写作同模型也严格两步）；关：加载常驻不卸
            _nc_plan = self.config_mgr.get("novel_checks", {}) or {}
            # 独占串行只在统一管理勾选时生效（不勾 → 3B/1.5B transformers，无 GPU 模型可串行）
            _serial_plan = bool(_nc_plan.get("exclusive_serial", True) and _nc_plan.get("unified_management", False))
            from .novel.model_backend import model_key_from_cfg, lms_session
            with lms_session(model_key_from_cfg(self.config_mgr.get("planner_model", {})), unload_on_exit=_serial_plan):
                # 兼容旧调用：如果有 meta/content 字段就走新方式
                if isinstance(template, dict) and (template.get("meta") is not None or template.get("content") is not None):
                    outline = plan_outline(topic, template=template, user_meta=user_meta, llm_client=client, plan_hints=plan_hints)
                elif isinstance(template, dict) and (template.get("meta") or template.get("content") or template.get("structure")):
                    outline = plan_outline(topic, template=template, user_meta=user_meta, llm_client=client, plan_hints=plan_hints)
                else:
                    style = template if isinstance(template, str) else ""
                    outline = plan_outline(topic, template=style or prompt, llm_client=client)
        except (ValueError, LLMClientError) as e:
            self._json_response({"success": False, "error": str(e)}, 500)
            return

        # 规划完成：更新 outline（复用规划前已建的 session，含用户要求消息）
        sm.set_outline(outline)

        self._json_response({
            "success": True,
            "outline": outline,
            "session_id": sm.session_id
        })

    # ---- 生成 API（异步后台） ----

    def _handle_generate(self):
        data = self._read_body()
        session_id = data.get("session_id", "")
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return

        # 重置停止标记和状态文本
        with _stop_lock:
            _stop_flags.pop(session_id, None)
        try:
            sm = StateManager()
            sm.load(session_id)
            sm.set_status_text("")
        except Exception:
            pass

        # 检查是否已存在正在进行的生成任务
        with _gen_lock:
            if session_id in _generation_tasks:
                existing = _generation_tasks[session_id]
                if not existing["done"]:
                    self._json_response({"success": False, "error": "该会话正在生成中"}, 409)
                    return

        try:
            sm = StateManager()
            sm.load(session_id)
        except FileNotFoundError:
            self._json_response({"success": False, "error": "会话不存在"}, 404)
            return

        state = sm.get_state()
        outline = state.get("outline", {})

        # 竞态防护：小说线重规划在途（章级/子结构）→ 拒绝启动生成。
        # 否则写作线程基于旧 outline/旧子结构开跑，与重规划返回的新数据竞态。
        # 前端 _replanBusy 已拦截正常路径；此处后端兜底（绕过前端直接 POST 也拒绝）。
        # 整篇重规划（/api/plan）不登记 _replan_inflight，由前端 _replanBusy 拦截。
        inflight_all = state.get("_replan_inflight", []) or []
        if inflight_all:
            import time as _t
            _now = _t.time()
            inflight_live = [t for t in inflight_all if (_now - float(t.get("started_at", 0))) <= 1800]
            if len(inflight_live) != len(inflight_all):
                sm._state["_replan_inflight"] = inflight_live
                sm.save()
            if inflight_live:
                descs = ", ".join(f"{t.get('chapter_id','?')}{t.get('s_key','')}" for t in inflight_live)
                self._json_response(
                    {"success": False, "error": f"小说线有重规划正在进行中（{descs}），请等待其完成后再开始生成"},
                    409,
                )
                return

        user_orders = data.get("orders", {}) or state.get("user_orders", {})
        rag_options = data.get("rag", {})

        # 应用标题修改（章节/子结构可改名；模板绑定走 _tmpl_key，不随标题断链）
        titles = data.get("titles", {})
        if titles:
            for s in outline.get("sections", []):
                if s["id"] in titles:
                    s["title"] = titles[s["id"]]
                for ss in s.get("sub_sections", []):
                    if ss["id"] in titles:
                        ss["title"] = titles[ss["id"]]

        # 应用用户的重点覆盖
        key_sections = data.get("key_sections", {})
        if key_sections:
            for s in outline.get("sections", []):
                if s["id"] in key_sections:
                    s["is_key"] = key_sections[s["id"]]

        # 应用勾选状态：过滤掉未选中的节和子结构
        checked = data.get("checked", {})
        if checked:
            sections = outline.get("sections", [])
            # 从后往前删，避免索引问题
            for i in range(len(sections) - 1, -1, -1):
                s = sections[i]
                sec_checked = checked.get(s["id"], True)
                if not sec_checked:
                    sections.pop(i)
                    continue
                # 过滤子结构
                subs = s.get("sub_sections", [])
                s["sub_sections"] = [ss for ss in subs if checked.get(ss["id"], True)]

        # 应用子结构排序（阿拉伯数字）
        sub_orders = data.get("sub_orders", {})
        if sub_orders:
            for s in outline.get("sections", []):
                subs = s.get("sub_sections", [])
                def sub_sort_key(ss):
                    ro = sub_orders.get(ss["id"], "")
                    try:
                        return int(ro.lstrip("s"))
                    except (ValueError, TypeError):
                        return 999
                subs.sort(key=sub_sort_key)

        # 应用子结构字数覆盖
        sub_words = data.get("sub_words", {})
        if sub_words:
            for s in outline.get("sections", []):
                for ss in s.get("sub_sections", []):
                    if ss["id"] in sub_words:
                        ss["word_count"] = sub_words[ss["id"]]
                # 重新计算章节总字数
                s["word_count"] = sum(ss.get("word_count", 0) for ss in s.get("sub_sections", []))

        # 应用 leaf 节字数覆盖
        sec_words = data.get("sec_words", {})
        if sec_words:
            for s in outline.get("sections", []):
                if s["id"] in sec_words:
                    s["word_count"] = sec_words[s["id"]]

        # 保存用户排序
        if user_orders:
            sm.set_user_orders(user_orders)

        # 保存过滤后的大纲（使进度计算用正确总数）
        sm2 = StateManager()
        sm2.load(session_id)
        sm2._state["outline"] = outline
        sm2.save()

        # 获取写作模型配置
        client = self._create_writer_client()

        # 获取前文回顾字数配置
        context_review_length = self.config_mgr.get("context_review_length", 800)

        # 获取辅助知识
        aux_knowledge = data.get("aux_knowledge", {})

        # 获取事实自检配置
        fact_check_enabled = self.config_mgr.get("fact_check_enabled", False)

        # 获取当前模板（为 meta 渲染提供 structure）
        templates = self.config_mgr.get("templates", {})
        selected = self.config_mgr.get("selected_template", "")
        current_template = templates.get(selected, {})
        if not isinstance(current_template, dict):
            current_template = {}

        # 从模板 content 项构建引用验证配置
        citation_config = {}
        for cf in (current_template.get("content") or []):
            if cf.get("citation_check"):
                citation_config[cf["name"]] = {
                    "enabled": True,
                    "format": cf.get("citation_format", "[x]=1."),
                    "desc": cf.get("desc", ""),
                }

        # 如果 8767 在线，创建 RAG 客户端
        rag_client = None
        try:
            probe = self._probe_rag_8767()
            if probe["online"]:
                from .rag_client import RAGClient
                rag_client = RAGClient()
        except Exception:
            pass

        # 在后台线程中运行生成
        tmpl = current_template
        def _run_generation(sid, outline, orders, rag_opt, llm_cli, aux_kn, ctx_len, fc_enabled, tmpl, cit_cfg):
            result = {"done": True, "success": False, "output_file": "",
                      "content": "", "word_count": 0, "error": ""}
            try:
                local_sm = StateManager()
                local_sm.load(sid)
                # 停止检测函数
                def _stop_check():
                    with _stop_lock:
                        return _stop_flags.get(sid)
                # 统一管理：写作模型装载 → 写全文 → 卸载（ollama/非 lmstudio 空跑）
                # 独占串行开（默认）：不在此包 35B——novel_writer 内部章级调度（写章加载→章检前卸载→8B/7B 判定）
                # 关（并行）：加载常驻不卸载，整本写作期间模型保持可用
                _nc_write = self.config_mgr.get("novel_checks", {}) or {}
                # 独占串行只在统一管理勾选时生效（不勾 → 判定 transformers，无 GPU 竞争 → 35B 常驻）
                _serial_write = bool(_nc_write.get("exclusive_serial", True) and _nc_write.get("unified_management", False))
                from .novel.model_backend import model_key_from_cfg
                _wkey = model_key_from_cfg(self.config_mgr.get("writer_model", {}))
                if _serial_write:
                    md_content, output_path = generate_article(
                        outline=outline,
                        user_orders=orders,
                        rag_options=rag_opt,
                        llm_client=llm_cli,
                        state_mgr=local_sm,
                        rag_client=rag_client,
                        aux_knowledge=aux_kn,
                        fact_check_enabled=fc_enabled,
                        context_review_length=ctx_len,
                        stop_check=_stop_check,
                        template=tmpl,
                        citation_config=cit_cfg
                    )
                else:
                    from .novel.model_backend import lms_session
                    with lms_session(_wkey, unload_on_exit=False):
                        md_content, output_path = generate_article(
                            outline=outline,
                            user_orders=orders,
                            rag_options=rag_opt,
                            llm_client=llm_cli,
                            state_mgr=local_sm,
                            rag_client=rag_client,
                            aux_knowledge=aux_kn,
                            fact_check_enabled=fc_enabled,
                            context_review_length=ctx_len,
                            stop_check=_stop_check,
                            template=tmpl,
                            citation_config=cit_cfg
                        )
                result["success"] = True
                result["output_file"] = output_path
                result["content"] = md_content[:8000] + ("...(截断) 完整文件见" + output_path if len(md_content) > 8000 else "")
                result["word_count"] = len(md_content.replace(" ", "").replace("\n", ""))
            except Exception as e:
                result["error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                # 更新状态为错误
                try:
                    local_sm = StateManager()
                    local_sm.load(sid)
                    local_sm.set_phase("error")
                except Exception:
                    pass
            finally:
                # 清理停止标记
                with _stop_lock:
                    _stop_flags.pop(sid, None)
                with _gen_lock:
                    if sid in _generation_tasks:
                        _generation_tasks[sid].update(result)
                        _generation_tasks[sid]["done"] = True

        thread = threading.Thread(
            target=_run_generation,
            args=(session_id, outline, user_orders, rag_options, client, aux_knowledge, context_review_length, fact_check_enabled, current_template, citation_config),
            daemon=True
        )
        with _gen_lock:
            _generation_tasks[session_id] = {
                "thread": thread, "done": False,
                "success": None, "output_file": "",
                "content": "", "word_count": 0, "error": ""
            }
        thread.start()

        self._json_response({
            "success": True,
            "task_id": session_id,
            "message": "生成任务已启动"
        })

    # ---- 获取生成结果（轮询后拉取） ----

    def _handle_get_result(self):
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        session_id = (params.get("session_id") or [""])[0]
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return
        with _gen_lock:
            task = _generation_tasks.get(session_id)
            if task is None:
                self._json_response({"success": False, "error": "没有生成任务"}, 404)
                return
            if not task["done"]:
                self._json_response({"success": False, "error": "生成中", "done": False}, 200)
                return
            # 任务完成，清除任务记录
            result = {
                "success": task.get("success", False),
                "output_file": task.get("output_file", ""),
                "content": task.get("content", ""),
                "word_count": task.get("word_count", 0),
                "error": task.get("error", ""),
                "done": True
            }
            del _generation_tasks[session_id]
        self._json_response(result)

    # ---- 停止 API ----

    def _handle_stop(self):
        data = self._read_body()
        session_id = data.get("session_id", "")
        stop_type = data.get("type", "delay")  # "delay" 或 "immediate"
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return
        with _stop_lock:
            _stop_flags[session_id] = stop_type
        self._json_response({"success": True, "type": stop_type})

    # ---- 已完成文章列表 ----

    def _handle_outputs_list(self):
        """列出 data/outputs/ 下的文章：兼容平铺 .md（旧）、文章目录（通用线，目录内 md + 图片集）、
        小说树状（目录含 chapters/ 子目录 → 题目下挂已完成章，可展开收起；整本由手动拼合生成）"""
        from .state_manager import OUTPUTS_DIR
        files = []
        if OUTPUTS_DIR.exists():
            for f in sorted(OUTPUTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file() and f.suffix == ".md" and f.stat().st_size > 0:
                    files.append({
                        "name": f.name, "is_dir": False,
                        "size": f.stat().st_size, "mtime": f.stat().st_mtime,
                    })
                elif f.is_dir():
                    ch_dir = f / "chapters"
                    if ch_dir.is_dir():  # 小说树状：题目下挂章
                        chs = []
                        for c in sorted(ch_dir.glob("*.md")):
                            if c.stat().st_size > 0:
                                chs.append({
                                    "name": c.name, "size": c.stat().st_size,
                                    "mtime": c.stat().st_mtime,
                                })
                        full_md = f / (f.name.rstrip("/") + ".md")
                        files.append({
                            "name": f.name + "/", "is_dir": True, "novel": True,
                            "chapter_count": len(chs), "children": chs,
                            "has_full": full_md.is_file(),
                            "full_size": full_md.stat().st_size if full_md.is_file() else 0,
                            "mtime": f.stat().st_mtime,
                        })
                        continue
                    mds = sorted(f.glob("*.md"))
                    if mds and mds[0].stat().st_size > 0:
                        img_count = sum(1 for p in f.iterdir()
                                        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif"))
                        files.append({
                            "name": f.name + "/", "is_dir": True,
                            "md_name": mds[0].name, "image_count": img_count,
                            "size": mds[0].stat().st_size, "mtime": f.stat().st_mtime,
                        })
        self._json_response({"success": True, "files": files})

    @classmethod
    def _safe_output_path(cls, name: str):
        """解析输出路径并做越界防护（防 ../ 与绝对路径穿越）"""
        from .state_manager import OUTPUTS_DIR
        if not name or ".." in name or name.startswith("/") or "\\" in name or ":" in name:
            return None
        fpath = (OUTPUTS_DIR / name).resolve()
        base = OUTPUTS_DIR.resolve()
        if not str(fpath).startswith(str(base)):
            return None
        return fpath

    def _handle_outputs_read(self):
        """读取文章内容：name 可为 .md 文件（旧）、目录（读目录内第一个 .md）、
        小说目录（含 chapters/ → 动态合并全部已完成章返回整本预览，不落盘）"""
        from .state_manager import OUTPUTS_DIR
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = params.get("file", [""])[0]
        if not name:
            self._json_response({"success": False, "error": "缺少 file 参数"}, 400)
            return
        fpath = self._safe_output_path(name)
        if fpath is None:
            self._json_response({"success": False, "error": "非法路径"}, 400)
            return
        if fpath.is_dir():
            ch_dir = fpath / "chapters"
            if ch_dir.is_dir():
                # 小说目录：整本预览 = 动态合并所有已完成章（按 L## 编号排序）
                chs = sorted(ch_dir.glob("*.md"))
                if not chs:
                    self._json_response({"success": False, "error": "暂无已完成章节"}, 404)
                    return
                title = fpath.name.replace("_", " ").strip() or "未命名"
                parts = [f"# {title}\n"]
                for c in chs:
                    if c.stat().st_size > 0:
                        parts.append(c.read_text(encoding="utf-8").strip())
                content = "\n\n".join(parts)
                safe = "".join(ch if ch.isprintable() or ch in "\n\r\t" else " " for ch in content)
                self._json_response({"success": True, "content": safe, "name": name,
                                     "images": [], "merged": True, "chapter_count": len(chs)})
                return
            mds = sorted(fpath.glob("*.md"))
            fpath = mds[0] if mds else None
        if not fpath or not fpath.is_file() or fpath.suffix != ".md":
            self._json_response({"success": False, "error": "文件不存在"}, 404)
            return
        content = fpath.read_text(encoding="utf-8")
        # 安全过滤不可见字符
        safe = "".join(c if c.isprintable() or c in "\n\r\t" else " " for c in content)
        # 附带所在目录的图片清单（供预览展示）
        images = []
        for p in fpath.parent.iterdir():
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif"):
                images.append(p.name)
        self._json_response({"success": True, "content": safe, "name": name, "images": images})

    def _handle_outputs_merge(self):
        """小说手动拼合：合并 chapters/ 全部章 md → 写 <标题>.md（整本完整版）"""
        from .state_manager import OUTPUTS_DIR
        data = self._read_body()
        name = data.get("file", "")
        if not name:
            self._json_response({"success": False, "error": "缺少 file 参数"}, 400)
            return
        fpath = self._safe_output_path(name)
        if fpath is None or not fpath.is_dir():
            self._json_response({"success": False, "error": "目录不存在"}, 404)
            return
        ch_dir = fpath / "chapters"
        if not ch_dir.is_dir():
            self._json_response({"success": False, "error": "非小说目录（无 chapters/）"}, 400)
            return
        chs = sorted(ch_dir.glob("*.md"))
        if not chs:
            self._json_response({"success": False, "error": "暂无已完成章节"}, 400)
            return
        title = fpath.name.replace("_", " ").strip() or "未命名"
        parts = [f"# {title}\n"]
        for c in chs:
            if c.stat().st_size > 0:
                parts.append(c.read_text(encoding="utf-8").strip())
        full_md = fpath / f"{fpath.name}.md"
        full_md.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        self._json_response({"success": True, "path": str(full_md),
                             "name": fpath.name + "/", "chapter_count": len(chs),
                             "size": full_md.stat().st_size})

    def _handle_outputs_delete(self):
        """删除文章：name 为 .md 文件 → 单删（旧）；为目录 → 删整个文件夹（新，含图片集）"""
        from .state_manager import OUTPUTS_DIR
        data = self._read_body()
        name = data.get("file", "")
        if not name:
            self._json_response({"success": False, "error": "缺少 file 参数"}, 400)
            return
        fpath = self._safe_output_path(name)
        if fpath is None:
            self._json_response({"success": False, "error": "非法路径"}, 400)
            return
        if fpath.is_dir():
            import shutil
            shutil.rmtree(fpath)
            self._json_response({"success": True, "deleted": "dir"})
            return
        if fpath.is_file() and fpath.suffix == ".md":
            fpath.unlink()
            self._json_response({"success": True, "deleted": "file"})
            return
        self._json_response({"success": False, "error": "文件不存在"}, 404)

    # ---- tex/pdf 生成 API ----

    _LATEX_SKILL_DIR = Path(os.path.expanduser("~")) / ".workbuddy" / "skills" / "latex-modular"

    @staticmethod
    def _find_lualatex() -> str:
        """保留向后兼容：返回找到的 LaTeX 引擎路径（优先 xelatex，ctex 兼容性最好）"""
        return StructuredWriterHandler._find_engine("xelatex")

    @staticmethod
    def _find_engine(engine: str = "xelatex") -> str:
        """查找 LaTeX 引擎（xelatex/lualatex）：PATH 优先，其次常见安装路径"""
        exe = engine + ".exe"
        # PATH 查找
        for d in os.environ.get("PATH", "").split(os.pathsep):
            cand = os.path.join(d, exe)
            if os.path.exists(cand):
                return cand
        # MiKTeX 常见路径
        for p in [
            r"C:\Program Files\MiKTeX\miktex\bin\x64",
            r"C:\Program Files (x86)\MiKTeX\miktex\bin",
            os.path.expanduser(r"~\AppData\Local\Programs\MiKTeX\miktex\bin\x64"),
        ]:
            cand = os.path.join(p, exe)
            if os.path.exists(cand):
                return cand
        return ""

    @staticmethod
    def _engine_available(engine: str) -> bool:
        """引擎是否可调用（PATH 或常见路径）"""
        return bool(StructuredWriterHandler._find_engine(engine))

    def _handle_outputs_texpdf(self):
        """生成 .tex + .pdf：读 md → 环境自检（无 LaTeX 自动安装）→ md2tex → 编译"""
        from .state_manager import OUTPUTS_DIR
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = params.get("file", [""])[0]
        if not name:
            self._json_response({"success": False, "error": "缺少 file 参数"}, 400)
            return
        fpath = self._safe_output_path(name)
        if fpath is None:
            self._json_response({"success": False, "error": "非法路径"}, 400)
            return
        if fpath.is_dir():
            ch_dir = fpath / "chapters"
            if ch_dir.is_dir():
                # 小说目录：先确保整本（未拼合则动态合并到临时整本 md），再走 md2tex
                chs = sorted(ch_dir.glob("*.md"))
                if not chs:
                    self._json_response({"success": False, "error": "暂无已完成章节"}, 404)
                    return
                full = fpath / f"{fpath.name}.md"
                if not full.is_file():
                    parts = [f"# {fpath.name.replace('_', ' ').strip() or '未命名'}\n"]
                    for c in chs:
                        if c.stat().st_size > 0:
                            parts.append(c.read_text(encoding="utf-8").strip())
                    full.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
                fpath = full
            else:
                mds = sorted(fpath.glob("*.md"))
                fpath = mds[0] if mds else None
        if not fpath or not fpath.is_file() or fpath.suffix != ".md":
            self._json_response({"success": False, "error": "文件不存在"}, 404)
            return

        out_dir = fpath.parent
        title = fpath.stem

        # ① 环境自检：选定可用引擎（xelatex 优先，ctex 兼容性最好；lualatex 回退）
        engine = "xelatex" if self._engine_available("xelatex") else (
            "lualatex" if self._engine_available("lualatex") else "")
        engine_path = self._find_engine(engine) if engine else ""
        install_msg = ""
        if not engine:
            # 自动安装 MiKTeX（默认装完整引擎集）
            try:
                r = subprocess.run(["winget", "install", "MiKTeX.MiKTeX", "--accept-package-agreements",
                                    "--accept-source-agreements", "--silent"],
                                   capture_output=True, text=True, timeout=900,
                                   encoding="utf-8", errors="replace")
                install_msg = "已自动安装 MiKTeX（winget）。" if r.returncode == 0 else f"MiKTeX 安装失败: {r.stderr.strip()[:200]}"
            except Exception as e:
                install_msg = f"MiKTeX 安装异常: {e}"
            engine = "xelatex" if self._engine_available("xelatex") else "lualatex"
            engine_path = self._find_engine(engine)

        if not engine_path:
            self._json_response({"success": False, "error": f"未检测到 LaTeX 引擎。{install_msg} 请手动安装 MiKTeX 后重试。"})
            return

        # ①.5 MiKTeX 宏包自动安装（AutoInstall=1，避免编译时弹窗逐个询问）
        try:
            miktex_bin = os.path.dirname(engine_path)
            initexmf = os.path.join(miktex_bin, "initexmf.exe")
            if os.path.exists(initexmf):
                subprocess.run([initexmf, "--set-config-value=[MPM]AutoInstall=1"],
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
        except Exception:
            pass  # 设置失败不阻塞编译

        # ② md → tex（image_base_dir：图片与 md/tex 同目录，供图片分类排版读像素尺寸）
        from .md2tex import md_to_tex
        md_text = fpath.read_text(encoding="utf-8")
        tex_text = md_to_tex(md_text, title=title, image_base_dir=str(out_dir))
        tex_path = out_dir / f"{title}.tex"
        tex_path.write_text(tex_text, encoding="utf-8")

        # ③ 编译：调用 latex-modular 技能的 validate.py
        #   PYTHONIOENCODING=utf-8：子进程 print/open 默认 UTF-8，避免 GBK 编码 Unicode 字符（×/✓）崩
        #   引擎优先 xelatex（ctex 兼容性最好），无则 lualatex
        sub_env = os.environ.copy()
        sub_env["PYTHONIOENCODING"] = "utf-8"
        sub_env["PYTHONUTF8"] = "1"
        validate_py = self._LATEX_SKILL_DIR / "scripts" / "validate.py"
        if validate_py.exists():
            cmd = [sys.executable, str(validate_py), str(tex_path),
                   "--engine", engine, "--fix"]
        else:
            # 技能缺失时回退直接引擎编译
            cmd = [engine_path, "-interaction=nonstopmode", "-halt-on-error", str(tex_path)]
        try:
            r = subprocess.run(cmd, cwd=str(out_dir), capture_output=True, text=True, timeout=600,
                               encoding="utf-8", errors="replace", env=sub_env)
        except Exception as e:
            self._json_response({"success": False, "error": f"编译异常: {e}", "tex": str(tex_path)})
            return

        pdf_path = out_dir / f"{title}.pdf"
        ok = pdf_path.exists()
        msg = "编译成功。" if ok else f"编译未生成 PDF。{r.stdout[-300:] if r.stdout else ''}{r.stderr[-200:] if r.stderr else ''}"
        self._json_response({
            "success": ok,
            "tex": str(tex_path),
            "pdf": str(pdf_path) if ok else "",
            "install_msg": install_msg,
            "message": msg,
        })

    # ---- 辅助资料上传 API ----
    _AUX_ALLOWED_EXT = (".csv", ".db", ".txt", ".md", ".png", ".jpg", ".jpeg", ".gif")
    _AUX_MAX_SIZE = 20 * 1024 * 1024  # 20MB

    @classmethod
    def _aux_type(cls, name: str) -> str:
        ext = os.path.splitext(name)[1].lower()
        if ext in (".csv", ".db"):
            return "table"
        if ext in (".txt", ".md"):
            return "text"
        if ext in (".png", ".jpg", ".jpeg", ".gif"):
            return "image"
        return "unknown"

    def _handle_aux_upload(self):
        """辅助资料上传：base64 JSON → 存会话临时目录，返回 {name, type, path}"""
        from .state_manager import SESSIONS_DIR
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        name = str(data.get("name", "")).strip()
        b64 = str(data.get("b64", ""))
        if not name or not b64:
            self._json_response({"success": False, "error": "缺少 name/b64"}, 400)
            return
        ftype = self._aux_type(name)
        if ftype == "unknown":
            self._json_response({"success": False, "error": f"不支持的文件类型: {name}"}, 400)
            return
        import base64
        try:
            raw = base64.b64decode(b64)
        except Exception:
            self._json_response({"success": False, "error": "base64 解码失败"}, 400)
            return
        if len(raw) > self._AUX_MAX_SIZE:
            self._json_response({"success": False, "error": f"文件超过 {self._AUX_MAX_SIZE // 1024 // 1024}MB 限制"}, 413)
            return
        # 存会话临时目录（不入 session JSON，防撑爆）
        aux_dir = SESSIONS_DIR / "aux"
        aux_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{int(time.time())}_{os.path.basename(name)}"
        target = aux_dir / safe_name
        target.write_bytes(raw)
        self._json_response({"success": True, "name": name, "type": ftype, "path": f"aux/{safe_name}"})

    # ---- 进度 API ----

    def _handle_get_progress(self):
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        session_id = (params.get("session_id") or [""])[0]
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return
        try:
            sm = StateManager()
            sm.load(session_id)
            progress = sm.get_progress()
            # 该会话是否有活跃生成线程（running=false 时 awaiting_confirm 是静态残留，
            # 前端不得据此弹出确认面板——确认面板只在用户点「开始生成」/线程确实运行时展示）
            with _gen_lock:
                task = _generation_tasks.get(session_id)
                progress["running"] = bool(task and not task.get("done"))
            if not progress.get("running"):
                progress["awaiting_confirm"] = None
            self._json_response({"success": True, "progress": progress})
        except FileNotFoundError:
            self._json_response({"success": False, "error": "会话不存在"}, 404)

    # ---- 会话 API ----

    def _handle_new_session(self):
        sm = StateManager()
        sm.init_session(self.config_mgr.get_all())
        # 自动归档旧会话（如超过 max_sessions）
        max_s = self.config_mgr.get("max_sessions", 20)
        StateManager.check_session_limit(max_s)
        self._json_response({
            "success": True,
            "session_id": sm.session_id
        })

    def _handle_list_sessions(self):
        sm = StateManager()
        sessions = sm.list_sessions()
        self._json_response({"success": True, "sessions": sessions})

    # ---- 会话恢复（断线重连） ----

    def _handle_session_load(self):
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        session_id = (params.get("session_id") or [""])[0]
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return
        try:
            sm = StateManager()
            sm.load(session_id)
            state = sm.get_state()
            progress = sm.get_progress()
            # 小说线：加载时自动尝试恢复项目备份（state 缺失 → 从 data/novel/backups/ 恢复）
            # 前端据此决定：恢复成功 → 非只读可续写；恢复失败 → 只读提示项目已丢失
            restore_result = None
            outline = state.get("outline", {})
            if outline.get("_novel") or any((s.get("_novel")) for s in outline.get("sections", [])):
                from .novel.novel_bridge import restore_novel_state
                from pathlib import Path as _P
                state_path = ""
                for s in outline.get("sections", []):
                    sp_ = (s.get("_novel") or {}).get("state_path")
                    if sp_:
                        state_path = sp_
                        break
                if state_path:
                    if _P(state_path).is_file():
                        restore_result = {"status": "ok", "reason": "state 存在"}
                    else:
                        ok = restore_novel_state(state_path)
                        restore_result = {"status": "restored" if ok else "missing",
                                          "reason": "从备份恢复" if ok else "无可用备份"}
            # 重规划在途：过滤僵尸（1800s）后随 session 返回，供前端刷新/重连后恢复禁用态
            import time as _t
            _now = _t.time()
            inflight_all = state.get("_replan_inflight", []) or []
            inflight_live = [t for t in inflight_all if (_now - float(t.get("started_at", 0))) <= 1800]
            if len(inflight_live) != len(inflight_all):
                sm._state["_replan_inflight"] = inflight_live
                sm.save()
            self._json_response({
                "success": True,
                "session": {
                    "session_id": state["session_id"],
                    "phase": state.get("phase", ""),
                    "outline": outline,
                    "user_orders": state.get("user_orders", {}),
                    "output_file": state.get("output_file", ""),
                    "created_at": state.get("created_at", ""),
                    "messages": state.get("messages", []),
                    "_replan_inflight": inflight_live
                },
                "progress": progress,
                "novel_restore": restore_result
            })
        except FileNotFoundError:
            self._json_response({"success": False, "error": "会话不存在"}, 404)

    def _handle_novel_replan_status(self):
        """GET /api/novel/replan_status — 轻量查询活的重规划 in-flight（供前端刷新后轮询恢复）。

        只返回过滤僵尸（1800s）后的 in-flight 数组；无则返回空数组。
        与 _handle_session_load 同规则，避免每次轮询拉全量 outline。
        """
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        session_id = (params.get("session_id") or [""])[0]
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return
        try:
            sm = StateManager()
            sm.load(session_id)
        except Exception:
            self._json_response({"success": False, "inflight": []})
            return
        inflight_all = sm._state.get("_replan_inflight", []) or []
        import time as _t
        _now = _t.time()
        inflight_live = [t for t in inflight_all if (_now - float(t.get("started_at", 0))) <= 1800]
        if len(inflight_live) != len(inflight_all):
            sm._state["_replan_inflight"] = inflight_live
            sm.save()
        self._json_response({"success": True, "inflight": inflight_live})

    # ---- 会话归档/恢复/删除 ----

    def _handle_session_archive(self):
        data = self._read_body()
        sid = data.get("id", "")
        if not sid:
            self._json_response({"success": False, "error": "缺少 id"}, 400)
            return
        ok = StateManager().archive_session(sid)
        self._json_response({"success": ok})

    def _handle_session_restore(self):
        data = self._read_body()
        sid = data.get("id", "")
        if not sid:
            self._json_response({"success": False, "error": "缺少 id"}, 400)
            return
        ok = StateManager().restore_session(sid)
        self._json_response({"success": ok})

    def _handle_session_delete(self):
        data = self._read_body()
        sid = data.get("id", "")
        if not sid:
            self._json_response({"success": False, "error": "缺少 id"}, 400)
            return
        ok = StateManager().delete_session(sid)
        self._json_response({"success": ok})

    # ---- 小说质检 ----

    def _novel_status_data(self):
        """R1/3B 模型就绪检测（data/models/ 优先，回退 HF 默认缓存）+ llama.cpp 后端判定"""
        try:
            from .novel._path_utils import MODELS_DIR
        except Exception:
            MODELS_DIR = None
        cfg = dict(self.config_mgr.get("novel_checks", {}) or {})
        default = {"chapter": True, "format": True, "reason": True, "full": True}
        for k in default:
            cfg.setdefault(k, default[k])

        # LM Studio 后端判定（B = 统一勾选；判定后端 = B → lmstudio 8B/7B，否则 transformers 3B/1.5B）
        try:
            from .novel import model_backend as mb
            from .novel import lmstudio_probe
            lm_env = lmstudio_probe.probe_lmstudio()
            judge_backend = mb.judge_backend(cfg)
            gguf_paths = mb.judge_gguf_paths(cfg) if judge_backend == "lmstudio" else {}
            try:
                gguf_dir = str(mb.default_gguf_dir())
            except Exception:
                gguf_dir = lm_env.get("models_dir") or ""
        except Exception:
            lm_env = {"reason": "探测异常", "server_ok": False, "lms_ok": False,
                      "models_dir": str(MODELS_DIR / "gguf") if MODELS_DIR else ""}
            judge_backend = "transformers"
            gguf_paths = {}
            gguf_dir = lm_env["models_dir"]

        def _has(model_dir):
            try:
                d = MODELS_DIR / model_dir
                return d.is_dir() and any(d.iterdir()) if d.exists() else False
            except Exception:
                return False

        def _hf_cache(model_name):
            """HF 默认缓存根目录（按 repo 名转目录名）"""
            try:
                return Path.home() / ".cache" / "huggingface" / "hub" / f"models--{model_name.replace('/', '--')}"
            except Exception:
                return None

        def _model_ready(model_name, multi_shard=True):
            """验证模型完整：index.json 引用的权重分片全部存在且非空（不只查目录）"""
            import json as _json
            base = None
            try:
                from .novel._path_utils import MODELS_DIR
                if MODELS_DIR:
                    base = MODELS_DIR / f"models--{model_name.replace('/', '--')}" / "snapshots"
            except Exception:
                pass
            if base is None or not base.exists():
                base = _hf_cache(model_name) / "snapshots" if _hf_cache(model_name) else None
            if base is None or not base.is_dir():
                return False
            for snap in base.iterdir():
                if not snap.is_dir():
                    continue
                index = snap / "model.safetensors.index.json"
                if index.is_file():
                    try:
                        wm = _json.loads(index.read_text(encoding="utf-8")).get("weight_map", {})
                        need = set(wm.values())
                        have = {f.name for f in snap.glob("*.safetensors") if f.stat().st_size > 0}
                        if need <= have:
                            return True
                    except Exception:
                        continue
                else:
                    # 无 index：单文件模型，有非空权重即可
                    if multi_shard:
                        continue
                    if any(f.is_file() and f.stat().st_size > 0 for f in snap.iterdir()):
                        return True
            return False

        r1 = _has("models--deepseek-ai--DeepSeek-R1-Distill-Qwen-1.5B/snapshots")
        qwen25 = _has("models--Qwen--Qwen2.5-3B-Instruct/snapshots")
        if not r1:
            r1 = _hf_cache("deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B").is_dir() if _hf_cache("deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B") else False
        if not qwen25:
            qwen25 = _model_ready("Qwen/Qwen2.5-3B-Instruct")
        # LM Studio 判定模型就绪（8B 4维 / 7B R1）
        gguf4 = bool(gguf_paths.get("4dim")) if gguf_paths else False
        gguf7 = bool(gguf_paths.get("r1")) if gguf_paths else False
        # 写作/规划后端（统一管理只对 LM Studio 后端有意义；ollama 未接入判定联动 → 禁用）
        try:
            model_backends = {
                "planner": (self.config_mgr.get("planner_model", {}) or {}).get("backend", "lmstudio"),
                "writer": (self.config_mgr.get("writer_model", {}) or {}).get("backend", "lmstudio"),
            }
        except Exception:
            model_backends = {"planner": "lmstudio", "writer": "lmstudio"}
        return {"r1": r1, "qwen25": qwen25, "dir": str(MODELS_DIR) if MODELS_DIR else "",
                "config": cfg, "install": dict(_install_state),
                "model_backends": model_backends,
                "lmstudio": {"available": lm_env.get("lms_ok", False),
                             "server_ok": lm_env.get("server_ok", False),
                             "reason": lm_env.get("reason", "")},
                "judge_backend": judge_backend,
                "gguf": {"dir": gguf_dir, "4dim": gguf_paths.get("4dim", ""),
                         "r1": gguf_paths.get("r1", ""), "4dim_ready": gguf4, "r1_ready": gguf7}}

    def _handle_novel_status(self):
        self._json_response({"success": True, **self._novel_status_data()})

    def _handle_novel_install(self):
        """点击「安装缺失模型」→ 后台自动下载（hf-mirror），按判定后端选模型：
        lmstudio 后端 → 8B+7B GGUF（进 LM Studio 模型库）+ 3B（实体抽取仍需）；transformers 后端 → 1.5B+3B"""
        global _install_state
        st = self._novel_status_data()
        if st.get("judge_backend") == "lmstudio":
            missing = []
            if not st.get("gguf", {}).get("4dim_ready"):
                missing.append("gguf_4dim")
            if not st.get("gguf", {}).get("r1_ready"):
                missing.append("gguf_r1")
            if not st.get("qwen25"):
                missing.append("qwen25")  # 实体抽取/行为提取仍用 3B transformers
        else:
            missing = [k for k in ("r1", "qwen25") if not st.get(k)]
        if not missing:
            self._json_response({"success": True, "message": "模型已就绪，无需安装", "started": False, "models": []})
            return
        with _gen_lock:
            if _install_state.get("running"):
                self._json_response({"success": True,
                                     "message": "安装已在后台进行中（可看状态区进度）",
                                     "started": True, "models": _install_state.get("models", [])})
                return
            labels = {**_INSTALL_LABELS, **{k: v["label"] for k, v in _GGUF_MODELS.items()}}
            _install_state = {"running": True, "models": missing,
                              "log": [f"开始安装: {', '.join(labels.get(m, m) for m in missing)}"],
                              "done": False}
        threading.Thread(target=_run_model_install, args=(missing,), daemon=True).start()
        self._json_response({"success": True,
                             "message": f"已启动后台下载 {len(missing)} 个模型（{', '.join(labels.get(m, m) for m in missing)}），完成后状态自动更新",
                             "started": True, "models": missing})

    def _handle_novel_checks(self):
        data = self._read_body()
        cfg = {}
        for k in ("chapter", "format", "reason", "full", "full_fidelity", "full_pledge", "full_ending", "auto_repair", "unified_management", "exclusive_serial"):
            if k in data:
                cfg[k] = bool(data.get(k))
        for k in ("gguf_4dim", "gguf_r1"):
            if k in data:
                cfg[k] = str(data.get(k) or "").strip()
        if "repair_rounds" in data:
            try:
                cfg["repair_rounds"] = max(1, min(5, int(data.get("repair_rounds"))))
            except (ValueError, TypeError):
                cfg["repair_rounds"] = 3
        if "judge_n_ctx" in data:
            try:
                cfg["judge_n_ctx"] = max(8192, int(data.get("judge_n_ctx")))
            except (ValueError, TypeError):
                cfg["judge_n_ctx"] = 16384
        self.config_mgr.set("novel_checks", cfg)
        self._json_response({"success": True})

    def _handle_novel_confirm(self):
        """章级门控：用户确认当前 planning 章（应用调整 → 章 status=confirmed）。

        调整项：checked（段勾选跳过）、sub_words（段字数覆盖）。
        """
        data = self._read_body()
        session_id = data.get("session_id", "")
        if not session_id:
            self._json_response({"success": False, "error": "缺少 session_id"}, 400)
            return
        try:
            sm = StateManager()
            sm.load(session_id)
        except Exception as e:
            self._json_response({"success": False, "error": f"会话加载失败: {e}"}, 404)
            return
        outline = sm._state.get("outline", {})
        target = None
        for s in outline.get("sections", []):
            if s.get("status") == "planning":
                target = s
                break
        if target is None:
            self._json_response({"success": False, "error": "没有待确认的章"}, 400)
            return
        # 竞态防护：该章有子结构正在被重规划 → 拒绝确认
        # 否则 LLM 跑完后端返回时会把 outline sub 改写，写作线程却已基于旧数据开写（状态错乱）
        target_chapter_id = (target.get("_novel") or {}).get("chapter", "")
        inflight_all = sm._state.get("_replan_inflight", []) or []
        # 僵尸清理：started_at 超过 1800s（30 分钟）视为进程崩溃遗留，自动丢弃
        import time as _t
        _now = _t.time()
        inflight_live = [t for t in inflight_all if (_now - float(t.get("started_at", 0))) <= 1800]
        if len(inflight_live) != len(inflight_all):
            sm._state["_replan_inflight"] = inflight_live
            sm.save()
        conflict = [
            t for t in inflight_live
            if t.get("chapter_id") == target_chapter_id  # 任意类型（novel_sub/section）覆盖该章均拒
        ]
        if conflict:
            descs = ", ".join(f"{t.get('chapter_id')}{t.get('s_key','')}" for t in conflict)
            self._json_response(
                {"success": False, "error": f"该章有子结构正在重规划中（{descs}），请等待其完成后再确认本章"},
                409,
            )
            return
        # 应用确认时调整：勾选跳过 / 字数覆盖 / 重点标记
        checked = data.get("checked", {}) or {}
        sub_words = data.get("sub_words", {}) or {}
        sub_keys = data.get("sub_keys", {}) or {}
        sub_orders = data.get("sub_orders", {}) or {}
        for ss in target.get("sub_sections", []):
            if ss["id"] in checked:
                ss["_checked"] = bool(checked[ss["id"]])
            if ss["id"] in sub_words:
                try:
                    ss["word_count"] = int(sub_words[ss["id"]])
                except (ValueError, TypeError):
                    pass
            if ss["id"] in sub_keys:
                ss["is_key"] = True
        # 应用子结构顺序（复用通用线 sub_orders 语义：s1/s2 → sort；无显式排序的段排最后）
        if sub_orders:
            def sub_sort_key(ss):
                ro = sub_orders.get(ss["id"], "")
                try:
                    return int(ro.lstrip("s"))
                except (ValueError, TypeError):
                    return 999
            target["sub_sections"].sort(key=sub_sort_key)
        # 章字数 = 各子结构字数汇总（与通用线一致）
        subs = target.get("sub_sections", [])
        if subs:
            target["word_count"] = sum(ss.get("word_count", 0) for ss in subs)
        # 同步 novel_state 的 sub_structures dict 顺序（s_key 不变、顺序变）——
        # 小说线写作线程按 state 的 dict 序写子结构，outline 重排不生效，必须同步
        nv = target.get("_novel") or {}
        if sub_orders and nv.get("state_path"):
            from pathlib import Path as _P
            _sp = _P(nv["state_path"])
            if _sp.is_file():
                from .novel.novel_state_manager import load_state, save_state
                nd = load_state(str(_sp))
                _ch = next((c for c in nd.get("chapters", []) if c["id"] == nv.get("chapter", "")), None)
                if _ch and _ch.get("sub_structures"):
                    ordered_keys = []
                    for ss in target["sub_sections"]:
                        sk = (ss.get("_novel") or {}).get("s_key", "")
                        if sk and sk in _ch["sub_structures"] and sk not in ordered_keys:
                            ordered_keys.append(sk)
                    # 未出现在 outline 的 s_key 追加到末尾（防御）
                    for k in _ch["sub_structures"]:
                        if k not in ordered_keys:
                            ordered_keys.append(k)
                    _ch["sub_structures"] = {k: _ch["sub_structures"][k] for k in ordered_keys}
                    save_state(str(_sp), nd, caller="novel-confirm")
        target["status"] = "confirmed"
        sm._state["outline"] = outline
        sm.save()
        self._json_response({"success": True, "chapter": target.get("title", "")})

    # ---- 修复引擎 API（P3） ----

    def _repair_engine_for(self, session_id):
        """加载 session + 解析出 state_path / chapter_dir。

        从 outline 的 _novel.state_path 定位当前项目——不 glob 猜第一个
        （多个 novel 项目时 glob 会选到最老的项目，修复引擎跑错目录导致"文件不存在"）。
        """
        from .state_manager import StateManager as _SM
        sm = _SM()
        sm.load(session_id)
        outline = sm._state.get("outline", {})
        state_path = ""
        for s in outline.get("sections", []):
            sp_ = (s.get("_novel") or {}).get("state_path", "")
            if sp_:
                state_path = sp_
                break
        if not state_path or not Path(state_path).is_file():
            return None, None, None, None
        proj = Path(state_path).parent.parent  # .../projects/<id>/data/novel_state.json → .../projects/<id>
        return sm, state_path, str(proj / "chapters"), outline

    def _handle_repair_preview(self):
        """GET /api/novel/repair/preview?session_id=X&chapter=L02
        返回该章 T0（自动修） + T1（待勾选重构）清单。"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        session_id = (params.get("session_id") or [""])[0]
        chapter = (params.get("chapter") or [""])[0]
        if not session_id or not chapter:
            self._json_response({"success": False, "error": "缺少 session_id/chapter"}, 400)
            return
        try:
            sm, state_path, chapter_dir, outline = self._repair_engine_for(session_id)
            if state_path is None:
                self._json_response({"success": False, "error": "novel 项目目录未找到"}, 404)
                return
            hints = sm.get_repair_hints()
            hint = hints.get(chapter, {})
            if not hint:
                self._json_response({"success": True, "preview": None,
                                     "message": "该章无章检结果（可能未跑六检）"})
                return
            # 直接从 hint.issues（章检跑完时记录的真实问题清单）解析——
            # hint.output 是 stdout 可能截断（如 finalize_chapter 在某步异常退出），
            # 解析 output 会误判为"无问题"。hint.issues 是当时章检记录的真实列表，更可靠。
            all_structured = self._parse_hint_issues(hint.get("issues") or [], hint.get("output", ""))
            # 拆分：子结构问题（file 带 .txt，可勾选）vs 章级问题（file=章级，仅查看）
            structured = [it for it in all_structured if it.get("file", "").endswith(".txt")]
            chapter_only = [it for it in all_structured if not it.get("file", "").endswith(".txt")]
            issues = []
            files_set = set()
            for it in structured:
                prefix = f"{it['file']}: " if it.get("file") else ""
                issues.append(f"{prefix}[{it['severity']}] {it['problem']}")
                # 只收 T1（需 LLM 重构）问题的文件进勾选列表；
                # T0（末行/禁用模式/行数）由 apply_t0 自动修复，用户无需勾选，issues 保留供"T0 已自动修复"计数
                if it.get("file") and not any(k in it["problem"] for k in ("末行", "禁用模式", "行数")):
                    files_set.add(it["file"])
            # 环节优先级：4维 先修 → 格式/逻辑 → 推理审核后修（用户要求"4维 先、R1 后"）
            # 聚合打印把全部 HARD 放 SOFT 前（R1 HARD 反而在前），必须显式按环节排序
            def _ring_rank(p: str) -> int:
                p = p or ""
                if "4维" in p or p.startswith("[时间衔接]") or p.startswith("[情绪匹配]") \
                        or p.startswith("[话题过渡]") or p.startswith("[角色承接]"):
                    return 0
                if "推理审核" in p:
                    return 2
                return 1
            _rank_of = {}
            for it in structured:
                f = it.get("file")
                if f and f not in _rank_of:
                    _rank_of[f] = _ring_rank(it.get("problem", ""))
            files_hit = sorted(files_set, key=lambda f: _rank_of.get(f, 1))
            # 章级问题拼接成 issue 字符串（用于面板顶部"仅查看"展示）
            ch_only_lines = [f"{it['severity']}: {it['problem']}" for it in chapter_only]
            self._json_response({
                "success": True,
                "preview": {
                    "chapter": chapter,
                    "ok": hint.get("ok", False),
                    "timeout": hint.get("timeout", False),
                    "issues": issues,
                    "files": files_hit,
                    "chapter_only": ch_only_lines,
                }
            })
        except Exception as e:
            self._json_response({"success": False, "error": f"预览失败: {e}"}, 500)

    def _handle_repair_apply(self):
        """POST /api/novel/repair/apply  {session_id, chapter, checked_subs: ["S01.txt",...], mode}
        执行 T0 自动修 + T1 勾选段整段重构（后台线程）。"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        session_id = str(data.get("session_id", "")).strip()
        chapter = str(data.get("chapter", "")).strip()
        checked = data.get("checked_subs") or None
        full_types = data.get("full_types") or None
        mode = str(data.get("mode", "manual"))
        if not session_id or not chapter:
            self._json_response({"success": False, "error": "缺少 session_id/chapter"}, 400)
            return
        # 防重入（b22 崩溃根因 + b24 竞态修复 + b25 会话隔离）：
        # 状态按 session_id 隔离（_repair_states 字典）——A 会话修复不影响 B 会话的显示/防重入；
        # run() 只处理请求的 session_id（不会修别的会话）。
        # 重复 apply（前端重复提交/双击/手动再点）会启动多个 _run 线程并发调用同一 Llama 实例
        # → llama_cpp 线程不安全 → segfault。检查与置位必须同一锁块（防 TOCTOU 竞态）。
        st = _repair_states.setdefault(session_id, {"done": False, "running": False, "result": None, "chapter": "", "session_id": session_id})
        with _repair_lock:
            if st.get("running"):
                self._json_response({"success": False, "error": "已有修复任务进行中，请等待完成（或先关闭修复面板）"})
                return
            st.update({"done": False, "running": True, "result": None, "chapter": chapter, "session_id": session_id})
        # 三检类型化：full_types 与 checked_subs 一一对应 → {file: type}
        repair_types = None
        if full_types and checked and len(full_types) == len(checked):
            repair_types = {str(f): str(t) for f, t in zip(checked, full_types) if t}
        sm, state_path, chapter_dir, outline = self._repair_engine_for(session_id)
        if state_path is None:
            self._json_response({"success": False, "error": "novel 项目目录未找到"}, 404)
            return
        # chapter_dir 是 chapters 根 → 拼章级子目录（子结构文件在 chapters/<chapter>/ 下）
        from pathlib import Path as _P
        chapter_dir = str(_P(chapter_dir) / chapter)
        # 构造 issues 清单（从 hint.issues + output 补 4 维判定；hint.output 可能截断）
        hints = sm.get_repair_hints()
        hint = hints.get(chapter, {}) or {}
        structured = self._parse_hint_issues(hint.get("issues") or [], hint.get("output", ""))
        # 三检修复项（full_items）并入 issues——历史 bug：reng.run 只消费章检 issues，
        # 三检项（fidelity/pledge/ending）被静默丢弃 → 勾选三检修复 → 35B 装载 → seg_map 空 →
        # 一个 token 不吐就卸载 → 重检 verify_ending 碰运气通过，正文根本没改（用户实锤"修的啥"）
        full_items = hint.get("full_items") or []
        for it in full_items:
            if not it.get("sub"):
                continue
            structured.append({
                "file": str(it["sub"]) + ".txt",
                "problem": str(it.get("problem") or "三检问题"),
                "desc": str(it.get("problem") or "三检问题"),
                "severity": "HARD",
            })
        if not structured:
            self._json_response({"success": False, "error": "该章无可用检查输出"}, 400)
            return
        from .novel import novel_repair_engine as reng
        # 后台线程执行（T1 重构慢）
        def _run():
            try:
                # 统一管理：修复用写作模型装载 → 修复 → 卸载（ollama/非 lmstudio 空跑）
                # 独占串行开（默认）：修复完即卸载；关（并行）：加载常驻不卸
                _nc_rep = self.config_mgr.get("novel_checks", {}) or {}
                # 独占串行只在统一管理勾选时生效（不勾 → 3B/1.5B，无 GPU 模型可串行）
                _serial_rep = bool(_nc_rep.get("exclusive_serial", True) and _nc_rep.get("unified_management", False))
                from .novel.model_backend import model_key_from_cfg, lms_session
                with lms_session(model_key_from_cfg(self.config_mgr.get("writer_model", {})), unload_on_exit=_serial_rep):
                    rep = reng.run(state_path, chapter_dir, chapter, structured,
                                   mode=mode, config_mgr=self.config_mgr,
                                   checked_subs=checked, repair_types=repair_types)
                # 串行明确步骤：35B 已卸载（with 退出）→ 重构段实体/时间线同步（8B 提取真 GPU——
                # 若在 35B 驻留窗口内跑，8B 会被 LM Studio CPU offload）
                try:
                    from .novel.novel_repair_engine import sync_after_rewrite as _sync_rewrite
                    for _x in (rep.get("t1") or {}).get("results") or []:
                        if _x.get("status") == "rewritten" and _x.get("file"):
                            try:
                                _sync_rewrite(state_path, chapter_dir, _x["file"])
                            except Exception:
                                pass
                except Exception:
                    pass
                # 标记该章修复结果（含三检当场重检）——done 在重检完成后才置 True，
                # 前端轮询在重检期间持续显示"修复中"（用户勾选即默认承担重检成本）
                try:
                    sm2 = StateManager()
                    sm2.load(session_id)
                    hints = sm2.get_repair_hints()
                    t1_res = (rep.get("t1") or {}).get("results") or []
                    failed_n = sum(1 for x in t1_res if x.get("status") == "failed")
                    if repair_types:
                        # ── 三检场景：重构成功后当场重检验证（全文完结无"下次"，勾选即承担成本） ──
                        # 三检项存于 _repair_hints[chapter].full_items。
                        # 时序：三检在最后一章章检通过后才触发，该章 issues 必然已空——不存在并存。
                        if failed_n == 0:
                            hint = hints.get(chapter) or {}
                            ch_items = hint.get("full_items") or []
                            checked_keys = {(t, f.replace(".txt", "")) for f, t in repair_types.items()}
                            ok_keys = _recheck_full_items(state_path, chapter, repair_types, ch_items)
                            # 勾选且重检通过 → 移除；未勾选 → 接受问题移除；勾选未通过 → 保留（面板再弹可继续修）
                            kept = []
                            for it in ch_items:
                                key = (it.get("type"), it.get("sub"))
                                if key in ok_keys:
                                    continue
                                if key in checked_keys:
                                    kept.append(it)
                            hint["full_items"] = kept
                            # 三检阶段章检已全部通过，full_items 清空即标记通过
                            if not kept:
                                hint["_repaired"] = True
                            hints[chapter] = hint
                            sm2._state["_repair_hints"] = hints
                            sm2.save()
                    elif chapter in hints:
                        import re as _re
                        hints[chapter]["_repair_result"] = {
                            "t0_fixed": len((rep.get("t0") or {}).get("fixed", [])),
                            "t1_rewritten": sum(1 for x in t1_res if x.get("status") == "rewritten"),
                        }
                        # 未勾选段 → 立即标记通过：从该章 issues 中移除这些段的问题
                        if checked:
                            checked_set = set(checked)
                            all_files = {s.get("file") for s in structured if s.get("file")}
                            unchecked = all_files - checked_set
                            hints[chapter]["_skipped_subs"] = sorted(unchecked)
                            if unchecked:
                                kept = []
                                for ln in hints[chapter].get("issues", []):
                                    m = _re.findall(r"S\d+(?:\.txt)?", ln)
                                    files_in_line = {f if f.endswith(".txt") else f + ".txt" for f in m}
                                    if files_in_line and (files_in_line & unchecked):
                                        continue  # 属于未勾选段 → 跳过
                                    kept.append(ln)
                                hints[chapter]["issues"] = kept
                        else:
                            hints[chapter]["_skipped_subs"] = []
                        if failed_n == 0:
                            # 修复全部成功 → 重检验证（过滤已跳过段后无问题才标记通过）
                            try:
                                from .novel.novel_bridge import finalize_novel_chapter
                                fc = finalize_novel_chapter(state_path, chapter)
                                skipped = set(hints[chapter].get("_skipped_subs") or [])
                                kept = []
                                for ln in (fc.get("issues") or []):
                                    m = _re.findall(r"S\d+(?:\.txt)?", ln)
                                    files_in_line = {f if f.endswith(".txt") else f + ".txt" for f in m}
                                    if files_in_line and (files_in_line & skipped):
                                        continue
                                    kept.append(ln)
                                if kept:
                                    hard_kept = [ln for ln in kept if "[HARD]" in ln or "[FAIL]" in ln]
                                    if hard_kept:
                                        hints[chapter]["issues"] = kept  # 重检仍有 HARD → 保持未通过（面板再弹可继续修）
                                    else:
                                        # 重检只剩 SOFT（非阻断）→ 标记通过；issues 保留供展示
                                        hints[chapter]["_repaired"] = True
                                        hints[chapter]["issues"] = kept
                                else:
                                    # 章检阶段全文三检未触发（串行时序），issues 清空即标记通过
                                    hints[chapter]["_repaired"] = True  # 重检通过 → 标记通过
                            except Exception:
                                hints[chapter]["_repaired"] = True  # 重检异常兜底按通过
                        sm2._state["_repair_hints"] = hints
                        sm2.save()
                except Exception:
                    pass
                with _repair_lock:
                    _repair_states[session_id]["result"] = rep
                    _repair_states[session_id]["done"] = True
                    _repair_states[session_id]["running"] = False
            except Exception as e:
                with _repair_lock:
                    _repair_states[session_id]["result"] = {"error": f"{type(e).__name__}: {e}"}
                    _repair_states[session_id]["done"] = True
                    _repair_states[session_id]["running"] = False
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._json_response({"success": True, "started": True, "chapter": chapter,
                             "checked_subs": checked, "mode": mode})

    @staticmethod
    def _parse_hint_issues(issues: list, output: str = "") -> list:
        """从 hint.issues（章检跑完时记录的真实问题列表，含 HARD/FAIL/SOFT 全量）+ hint.output 补 4 维判定合并 structured。

        hint.issues 是章检完成时记录的最终问题（字符串列表），更可靠；
        hint.output 是 stdout 可能截断（如某步异常退出），仅作为 4 维判定行兜底补充
        （finalize 在聚合打印前异常退出时 hint.issues 为空，靠 output 的 [4维判定] 行补）。
        """
        import re as _re
        structured = []

        # 1. 解析 hint.issues 字符串列表（如 "[HARD] [L01] 推理审核 - 对话匹配度: ..."）
        for ln in (issues or []):
            if not ln or "[" not in ln:
                continue
            sev = None
            for s in ("HARD", "SOFT", "WARN", "FAIL"):
                if s in ln:
                    sev = s
                    break
            if sev is None:
                continue
            # 提取文件（如 "[L01]" → L01；子结构 S0X 优先）
            fname = ""
            m_sub = _re.search(r"S\d+\.txt", ln)
            if m_sub:
                fname = m_sub.group(0)
            else:
                m_ch = _re.search(r"\[L\d+\]", ln)
                if m_ch:
                    fname = m_ch.group(0).strip("[]")  # L01（章级问题，无子结构关联）
            # problem 去掉前缀 [HARD]/[SOFT] 与 [S01.txt]（`[\w.]+` 兼容 .txt 后缀）
            problem = _re.sub(r"^\s*\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)?", "", ln).strip()[:120]
            if not problem:
                problem = ln[:120]
            structured.append({"file": fname, "problem": problem, "severity": sev})

        # 2. 补充：4 维判定行（hint.issues 是当时写入的，4 维判定行可能未完全归入）
        for ln in (output or "").split("\n"):
            ln = ln.strip()
            m_4dim = _re.search(r"\[4维判定\]\s*S(\d+)[→\-\-]+S(\d+):\s*t=(\w+)\s+e=(\w+)\s+p=(\w+)\s+c=(\w+)", ln)
            if not m_4dim:
                continue
            s1, s2, t, e, p, c = m_4dim.groups()
            labels = {"t": "时间衔接", "e": "情绪匹配", "p": "话题过渡", "c": "角色承接"}
            for k, ok in (("t", t), ("e", e), ("p", p), ("c", c)):
                if ok == "False":
                    structured.append({
                        "file": f"S{s1}.txt",
                        "problem": f"4维-{labels[k]}不通过",
                        "position": f"S{s1}→S{s2}",
                        "severity": "SOFT",
                        "suggestion": "LLM 重构使该段满足维度",
                    })
        return structured

    def _parse_check_output(self, output: str) -> list:
        """把章检 stdout 解析为结构化 issues（供修复引擎）。

        兼容三种格式：
        - 行首带 [ 的问题行（如 "[HARD][L01] ..."）
        - 缩进行 "→ [HARD] 阻断：语义跳断"（历史语义检查格式；前一行有段间标记 "S01→S02"，
          HARD 行通过 last_files 跨行关联到涉及的文件）
        - 4维判定行 "[4维判定] S01→S02: t=True e=True p=False c=False"
          （False 维度生成 SOFT issue，file 关联到前段）
        """
        issues = []
        import re as _re
        last_files = []
        for ln in (output or "").split("\n"):
            ln = ln.strip()
            # 段间标记行（无 [ 前缀）：记录涉及的子结构文件，供后续 HARD 行关联
            m_pair = _re.search(r"S(\d+)[→\-\-]+S(\d+)", ln)
            if m_pair:
                last_files = [f"S{m_pair.group(1)}.txt", f"S{m_pair.group(2)}.txt"]
            # 4维判定行（[4维判定] S01→S02: t/e/p/c=True/False）
            m_4dim = _re.search(r"\[4维判定\]\s*S(\d+)[→\-\-]+S(\d+):\s*t=(\w+)\s+e=(\w+)\s+p=(\w+)\s+c=(\w+)", ln)
            if m_4dim:
                s1, s2, t, e, p, c = m_4dim.groups()
                last_files = [f"S{s1}.txt", f"S{s2}.txt"]
                labels = {"t": "时间衔接", "e": "情绪匹配", "p": "话题过渡", "c": "角色承接"}
                for k, ok in (("t", t), ("e", e), ("p", p), ("c", c)):
                    if ok == "False":
                        issues.append({
                            "file": f"S{s1}.txt",
                            "problem": f"4维-{labels[k]}不通过",
                            "position": f"S{s1}→S{s2}",
                            "severity": "SOFT",
                            "suggestion": "LLM 重构使该段满足维度",
                        })
                continue
            if not ln or "[" not in ln:
                continue
            sev = None
            for s in ("HARD", "SOFT", "WARN", "FAIL"):
                if s in ln:
                    sev = s
                    break
            if sev is None:
                continue
            # 过滤统计/进度行（如 "[语义检查] 完成: 1 HARD + 2 SOFT"）——不是具体问题
            if "完成:" in ln or "开始" in ln or "加载模型" in ln or "检查文件数" in ln:
                continue
            # 过滤连通性"通过"标记（[WARN][角色OK] = 通过，不是问题）
            if "角色OK" in ln:
                continue
            m = _re.search(r"S\d+(?:\.txt)?", ln)
            fname = m.group(0) if m else ""
            if fname and not fname.endswith(".txt"):
                fname += ".txt"
            if not fname and last_files:
                # HARD 行本身无文件名 → 关联最近的段间标记（取第一个涉及文件）
                fname = last_files[0]
            problem = ln.split("]", 1)[-1].strip()[:120]
            issues.append({"file": fname, "problem": problem, "severity": sev})
        return issues

    @staticmethod
    def _recheck_full_items(state_path, chapter, repair_types, items):
        """三检修复项当场重检验证（全文完结无"下次"，勾选即承担成本）。

        按类型重跑对应检查，返回"重检通过"的 (type, sub) 集合：
        - fidelity → 重跑 fidelity_check，该 (chapter, sub) 不在 fail_items = 通过
        - pledge  → 重跑 extract_pledges + check_pledges，该章无该 sub 的承诺问题 = 通过
        - ending  → 重跑 verify_ending，pass = 通过
        """
        from pathlib import Path as _P
        chapters_dir = str(_P(state_path).parent.parent / "chapters")
        ok_keys = set()
        types_needed = {t for t in repair_types.values()}
        if "fidelity" in types_needed:
            try:
                from .novel.novel_workflow_engine import fidelity_check
                _, _, _, _, fail_items = fidelity_check(state_path, chapters_dir)
                fail_subs = {(it.get("chapter"), it.get("sub")) for it in fail_items}
                for f, t in repair_types.items():
                    if t == "fidelity":
                        sub = f.replace(".txt", "")
                        if (chapter, sub) not in fail_subs:
                            ok_keys.add((t, sub))
            except Exception as e:
                print(f"[三检重检] fidelity 异常（按未通过处理）: {e}")
        if "pledge" in types_needed:
            try:
                from .novel.novel_pledge_check import extract_pledges, check_pledges
                if extract_pledges(state_path, chapters_dir):
                    pledge_issues = check_pledges(state_path, chapters_dir)
                else:
                    pledge_issues = []
                ch_bad_subs = {it.get("sub") for it in pledge_issues if it.get("file") == chapter}
                for f, t in repair_types.items():
                    if t == "pledge":
                        sub = f.replace(".txt", "")
                        if sub not in ch_bad_subs:
                            ok_keys.add((t, sub))
            except Exception as e:
                print(f"[三检重检] pledge 异常（按未通过处理）: {e}")
        if "ending" in types_needed:
            try:
                from .novel.novel_fidelity import verify_ending
                r = verify_ending(str(_P(state_path).parent))
                if r.get("pass"):
                    for f, t in repair_types.items():
                        if t == "ending":
                            ok_keys.add((t, f.replace(".txt", "")))
            except Exception as e:
                print(f"[三检重检] ending 异常（按未通过处理）: {e}")
        return ok_keys

    def _handle_repair_skip(self):
        """POST /api/novel/repair/skip  {session_id, chapter}
        全部跳过：用户确认该章所有检出问题都不修复 → 标记通过（_repaired=True），不再弹面板。"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        session_id = str(data.get("session_id", "")).strip()
        chapter = str(data.get("chapter", "")).strip()
        if not session_id or not chapter:
            self._json_response({"success": False, "error": "缺少 session_id/chapter"}, 400)
            return
        sm = StateManager()
        sm.load(session_id)
        hints = sm.get_repair_hints()
        if chapter in hints:
            # 全部跳过 = 通过：统一 _repaired=True + 清 issues/full_items（无单独跳过标识——用户语义"跳过=通过"）
            hints[chapter]["_repaired"] = True
            hints[chapter]["issues"] = []
            hints[chapter]["full_items"] = []
            hints[chapter]["_repair_result"] = {"skipped": True}
            sm._state["_repair_hints"] = hints
        sm.save()
        self._json_response({"success": True, "chapter": chapter})

    def _handle_repair_rollback(self):
        """POST /api/novel/repair/rollback  {session_id, chapter, round}"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        session_id = str(data.get("session_id", "")).strip()
        chapter = str(data.get("chapter", "")).strip()
        round_no = int(data.get("round", 1))
        sm, state_path, chapter_dir, outline = self._repair_engine_for(session_id)
        if state_path is None:
            self._json_response({"success": False, "error": "novel 项目目录未找到"}, 404)
            return
        from .novel import novel_repair_engine as reng
        restored = reng.rollback_round(chapter_dir, round_no, state_path)
        self._json_response({"success": True, "restored": restored})

    def _handle_repair_status(self):
        """GET /api/novel/repair/status — 修复进度轮询（按 session_id 隔离，b25）"""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sid = (params.get("session_id") or [""])[0]
        with _repair_lock:
            st = _repair_states.get(sid) or {"done": False, "running": False, "result": None, "chapter": "", "session_id": sid}
            self._json_response({"success": True, "state": dict(st)})

    def _handle_novel_replan_sub(self):
        """POST /api/novel/replan_sub — 段级重规划（确认面板内单个子结构）。

        body: {session_id, target_id（段 id）, hints, aux（段级辅助知识，可选）}
        更新 novel_state.json 的 sub_structures[s_key] + session outline 该段；
        章保持 planning（不重置 pending），确认面板由前端刷新。
        """
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        session_id = str(data.get("session_id", "")).strip()
        target_id = str(data.get("target_id", "")).strip()
        hints = str(data.get("hints", "")).strip()
        aux = data.get("aux") or None   # 段级辅助知识 {text, files}（前端从该段 aux_knowledge 传入）
        if not session_id or not target_id:
            self._json_response({"success": False, "error": "缺少 session_id 或 target_id"}, 400)
            return
        try:
            sm = StateManager()
            sm.load(session_id)
        except FileNotFoundError:
            self._json_response({"success": False, "error": "会话不存在"}, 404)
            return
        outline = sm._state.get("outline", {})
        # 反查目标段所属章 + 小说身份
        parent = None
        sub = None
        for s in outline.get("sections", []):
            for ss in s.get("sub_sections", []):
                if ss.get("id") == target_id:
                    parent, sub = s, ss
                    break
            if sub:
                break
        if sub is None:
            self._json_response({"success": False, "error": f"未找到子结构 {target_id}"}, 404)
            return
        nv = sub.get("_novel") or {}
        chapter_id = nv.get("chapter", "")
        s_key = nv.get("s_key", "")
        state_path = nv.get("state_path", "") or ((parent.get("_novel") or {}).get("state_path", ""))
        if not chapter_id or not s_key or not state_path:
            self._json_response({"success": False, "error": "该子结构缺少小说身份字段（非小说线）"}, 400)
            return
        # ── 注册 in-flight 标记（防与章确认并发：确认接口拒绝有 in-flight 子结构的章） ─
        inflight = sm._state.setdefault("_replan_inflight", [])
        token = {"type": "novel_sub", "target_id": target_id, "chapter_id": chapter_id, "s_key": s_key, "started_at": time.time()}
        inflight.append(token)
        sm.save()
        try:
            try:
                from .novel import novel_bridge as nb
                client = self._create_planner_client()
                new_entry = nb.replan_novel_sub(str(Path(state_path).resolve()), chapter_id, s_key, hints, client,
                                                aux_knowledge={s_key: aux} if aux else None)
            except Exception as e:
                self._json_response({"success": False, "error": str(e)}, 500)
                return
            # 同步 session outline 该段（保留 id/is_key/rag/_checked/_novel，更新规划字段）
            sub.update({
                "title": new_entry.get("title", sub.get("title", "")),
                "summary": new_entry.get("summary", sub.get("summary", "")),
                "word_count": (new_entry.get("word_count_target") or {}).get("max", sub.get("word_count", 1500)),
                "status": "pending",
                "actual_word_count": 0,
            })
            if parent is not None and parent.get("sub_sections"):
                parent["word_count"] = sum(ss.get("word_count", 0) for ss in parent["sub_sections"])
            sm._state["outline"] = outline
            sm.save()
            self._json_response({"success": True, "title": sub.get("title", ""), "word_count": sub.get("word_count", 0)})
        finally:
            # 清理 in-flight 标记（finally 保证异常路径也清理）
            inflight_now = sm._state.get("_replan_inflight", [])
            sm._state["_replan_inflight"] = [
                t for t in inflight_now
                if not (t.get("type") == token["type"] and t.get("target_id") == token["target_id"] and t.get("started_at") == token["started_at"])
            ]
            sm.save()

    # ---- RAG 状态探测 ----

    @classmethod
    def _probe_rag_8767(cls) -> dict:
        """探测 :8767 是否在线，返回 (status, kbs)"""
        RAG_PORT = 8767
        try:
            req = urllib.request.Request(f"http://localhost:{RAG_PORT}/api/health")
            with urllib.request.urlopen(req, timeout=3) as resp:
                health = json.loads(resp.read().decode("utf-8"))
            # 获取 KB 列表（API 返回 {kbs: {name: {...}}, stats: {...}}）
            kbs = []
            try:
                req2 = urllib.request.Request(f"http://localhost:{RAG_PORT}/api/kb/list")
                with urllib.request.urlopen(req2, timeout=3) as resp2:
                    kb_data = json.loads(resp2.read().decode("utf-8"))
                    kbs_raw = kb_data.get("kbs", kb_data.get("data", []))
                    if isinstance(kbs_raw, dict):
                        kbs = list(kbs_raw.keys())  # dict → KB 名称列表
                    elif isinstance(kbs_raw, list):
                        kbs = [k if isinstance(k, str) else k.get("name", "") for k in kbs_raw]
                    else:
                        kbs = []
            except Exception:
                pass
            return {"online": True, "health": health, "kbs": kbs}
        except Exception:
            return {"online": False, "health": None, "kbs": []}

    def _handle_rag_status(self):
        result = self._probe_rag_8767()
        # 检查本地子进程状态
        with _rag_lock:
            proc_alive = False
            if _rag_process is not None:
                proc_alive = _rag_process.poll() is None
        result["local_process"] = proc_alive
        result["starting"] = _rag_starting
        result["stderr"] = _rag_process_stderr[:500] if _rag_process_stderr else ""
        self._json_response({"success": True, **result})

    # ---- RAG 冷启动（异步） ----

    def _handle_rag_start(self):
        global _rag_starting
        data = self._read_body()
        path = data.get("path", "").strip()
        if not path or not os.path.isdir(path):
            self._json_response({"success": False, "error": "路径无效或不存在"}, 400)
            return

        # 先检查是否已经在线
        probe = self._probe_rag_8767()
        if probe["online"]:
            self._json_response({"success": False, "error": "8767 已在运行，无需启动"}, 400)
            return

        # 检查是否正在启动中
        with _rag_lock:
            if _rag_starting:
                self._json_response({"success": False, "error": "正在启动中，请稍候"}, 400)
                return
            _rag_starting = True

        main_py = os.path.join(path, "main.py")
        if not os.path.isfile(main_py):
            with _rag_lock:
                _rag_starting = False
            self._json_response({"success": False, "error": f"路径下未找到 main.py: {main_py}"}, 400)
            return

        try:
            # 用临时文件接 stderr，避免 pipe 缓冲区满卡死子进程
            stderr_tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix='.log', prefix='rag_', mode='w', encoding='utf-8'
            )
            stderr_path = stderr_tmp.name
            # 强制子进程使用 UTF-8 输出（防止 emoji/中文在 GBK 下报错）
            rag_env = os.environ.copy()
            rag_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                [sys.executable, main_py, "--port", "18765", "--api-port", "8767"],
                cwd=path,
                env=rag_env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_tmp
            )
            stderr_tmp.close()
            with _rag_lock:
                global _rag_process, _rag_process_stderr
                _rag_process = proc
                _rag_process_stderr = ""

            # 后台线程轮询等待就绪
            def _poll_rag_ready():
                global _rag_starting, _rag_process_stderr
                stderr_path_local = stderr_path
                try:
                    for _ in range(45):
                        time.sleep(2)
                        p = self._probe_rag_8767()
                        if p["online"]:
                            return
                        # 检查子进程是否还活着
                        if proc.poll() is not None:
                            # 进程挂了！读临时文件找 Traceback
                            err = ""
                            try:
                                with open(stderr_path_local, "r", encoding="utf-8", errors="replace") as ef:
                                    full = ef.read()
                                # 找 Traceback 或最后的 Python 异常
                                idx = full.rfind("Traceback (most recent call last)")
                                if idx >= 0:
                                    err = full[idx:][:2000]
                                else:
                                    err = full[-2000:]
                            except Exception:
                                err = "(无法读取输出)"
                            with _rag_lock:
                                _rag_process_stderr = err
                            return
                finally:
                    with _rag_lock:
                        _rag_starting = False
                    # 清理临时文件
                    try:
                        os.unlink(stderr_path_local)
                    except Exception:
                        pass

            t = threading.Thread(target=_poll_rag_ready, daemon=True)
            t.start()

            self._json_response({
                "success": True,
                "message": "RAG 启动中，请稍候..."
            })
        except Exception as e:
            with _rag_lock:
                _rag_starting = False
            self._json_response({"success": False, "error": f"启动失败: {e}"})

    # ---- RAG 停止 ----

    def _handle_rag_stop(self):
        global _rag_process, _rag_process_stderr
        # 1. 杀子进程
        with _rag_lock:
            if _rag_process is not None:
                try:
                    _rag_process.terminate()
                    _rag_process.wait(timeout=3)
                except Exception:
                    try:
                        _rag_process.kill()
                    except Exception:
                        pass
                _rag_process = None
                _rag_process_stderr = ""
        # 2. 查 8767 端口上的所有 PID，彻底杀光
        import subprocess as _sp
        try:
            r = _sp.run('netstat -ano', capture_output=True, text=True, shell=True, timeout=5)
            for line in r.stdout.splitlines():
                if '8767' in line and ('LISTENING' in line or 'ESTABLISHED' in line):
                    parts = line.strip().split()
                    if parts:
                        pid = parts[-1]
                        if pid.isdigit():
                            _sp.run(['taskkill', '/F', '/T', '/PID', pid],
                                    capture_output=True, timeout=5)
        except Exception:
            pass
        # 3. 等端口释放
        import socket, time
        for _ in range(10):
            try:
                s = socket.socket()
                s.settimeout(0.5)
                s.connect(('127.0.0.1', 8767))
                s.close()
                time.sleep(0.5)
            except Exception:
                break
        # 4. 再测一次端口是否还活着（自动重启检测）
        auto_restart = False
        try:
            s = socket.socket()
            s.settimeout(1)
            s.connect(('127.0.0.1', 8767))
            s.close()
            auto_restart = True
        except Exception:
            pass
        if auto_restart:
            self._json_response({
                "success": False,
                "error": "RAG 有自动重启机制（kill 后端口立即复活），请到独立 CMD 窗口手动关闭，或检查系统进程管理器。"
            })
        else:
            self._json_response({"success": True})

    # ---- 批量自动撰写 API ----

    def _handle_batch_auto(self):
        data = self._read_body()
        topics = data.get("topics", [])
        if not topics:
            self._json_response({"success": False, "error": "主题列表为空"}, 400)
            return

        # 从配置获取 prompt 和模板
        prompt_text = data.get("prompt", "") or self.config_mgr.get("default_prompt", "")
        template_name = data.get("template_name", "") or self.config_mgr.get("selected_template", "")
        templates_dict = self.config_mgr.get("templates", {})
        template = templates_dict.get(template_name, {})
        if not isinstance(template, dict):
            template = {}

        import threading as _thr
        task_id = f"batch_{int(time.time())}"

        def _run_batch():
            fc_enabled = self.config_mgr.get("fact_check_enabled", False)
            ctx_len = self.config_mgr.get("context_review_length", 800)

            # 从模板 content 项构建引用验证配置（与单篇生成一致）
            citation_config = {}
            if isinstance(template, dict):
                for cf in (template.get("content") or []):
                    if cf.get("citation_check"):
                        citation_config[cf["name"]] = {
                            "enabled": True,
                            "format": cf.get("citation_format", "[x]=1."),
                            "desc": cf.get("desc", ""),
                        }

            writer_client = self._create_writer_client()
            planner_client = self._create_planner_client()

            # 检测 RAG
            rag_client = None
            try:
                probe = self._probe_rag_8767()
                if probe["online"]:
                    from .rag_client import RAGClient
                    rag_client = RAGClient()
            except Exception:
                pass

            results = []
            errors = []

            for topic in topics:
                with _batch_lock:
                    if task_id in _batch_tasks:
                        _batch_tasks[task_id]["current_topic"] = topic

                try:
                    # 批量模式下使用当前选中模板
                    if isinstance(template, dict) and (template.get("meta") or template.get("content") or template.get("structure")):
                        outline = plan_outline(topic, template=template, user_meta={}, llm_client=planner_client)
                    else:
                        outline = plan_outline(topic, prompt=prompt_text, llm_client=planner_client)
                    # 全量RAG：所有节+子结构启用
                    rag_opts = {}
                    for s in outline.get("sections", []):
                        if rag_client:
                            rag_opts[s["id"]] = {"enabled": True, "kb": ""}

                    local_sm = StateManager()
                    local_sm.init_session(self.config_mgr.get_all())
                    local_sm.set_outline(outline)
                    sid = local_sm.session_id

                    md_content, output_path = generate_article(
                        outline=outline,
                        user_orders={},
                        rag_options=rag_opts,
                        llm_client=writer_client,
                        state_mgr=local_sm,
                        rag_client=rag_client,
                        aux_knowledge=None,
                        fact_check_enabled=fc_enabled,
                        context_review_length=ctx_len,
                        template=template if isinstance(template, dict) else None,
                        citation_config=citation_config
                    )

                    results.append({
                        "topic": topic,
                        "success": True,
                        "output_file": output_path,
                        "word_count": len(md_content.replace(" ", "").replace("\n", ""))
                    })
                except Exception as e:
                    errors.append({"topic": topic, "error": str(e)})

                with _batch_lock:
                    if task_id in _batch_tasks:
                        _batch_tasks[task_id]["done"] += 1
                        _batch_tasks[task_id]["results"] = list(results)
                        _batch_tasks[task_id]["errors"] = list(errors)

            with _batch_lock:
                if task_id in _batch_tasks:
                    _batch_tasks[task_id]["done_flag"] = True
                    _batch_tasks[task_id]["current_topic"] = ""

        t = _thr.Thread(target=_run_batch, daemon=True)
        with _batch_lock:
            _batch_tasks[task_id] = {
                "total": len(topics), "done": 0,
                "current_topic": "", "results": [],
                "errors": [], "done_flag": False
            }
        t.start()

        self._json_response({"success": True, "task_id": task_id, "total": len(topics)})

    def _handle_batch_progress(self):
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        task_id = (params.get("task_id") or [""])[0]
        if not task_id:
            self._json_response({"success": False, "error": "缺少 task_id"}, 400)
            return
        with _batch_lock:
            task = _batch_tasks.get(task_id)
            if task is None:
                self._json_response({"success": False, "error": "任务不存在"}, 404)
                return
            if task["done_flag"]:
                del _batch_tasks[task_id]
            self._json_response({
                "success": True,
                "total": task["total"],
                "done": task["done"],
                "current_topic": task["current_topic"],
                "results": task["results"],
                "errors": task["errors"],
                "done_flag": task["done_flag"]
            })

    # ---- 聊天 API (Phase 2 增强) ----

    def _handle_chat(self):
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        message = data.get("message", "").strip()
        if not message:
            self._json_response({"success": False, "error": "消息不能为空"}, 400)
            return
        session_id = data.get("session_id", "")
        # 通用线对话也持久化：已有会话保留历史（load），新会话才初始化（init）——
        # 保证同会话多轮对话消息累积，切会话/重启后 loadSession 重建显示
        sm = StateManager(session_id) if session_id else StateManager()
        if session_id:
            try:
                sm.load(session_id)
            except FileNotFoundError:
                sm.init_session(self.config_mgr.get_all())
        else:
            sm.init_session(self.config_mgr.get_all())
        sm.append_message("user", message)
        # Phase 1 基础版：简单回显 + 尝试规划
        # 检测是否是写作请求
        if any(kw in message for kw in ["写", "生成", "创作", "撰写", "起草"]):
            self._json_response({
                "success": True,
                "type": "writing_request",
                "text": "请确认：是否需要为此主题生成大纲？点击下方按钮开始规划。\n\n主题：" + message,
                "topic": message,
                "session_id": sm.session_id
            })
        else:
            self._json_response({
                "success": True,
                "type": "chat",
                "text": f"已收到消息。如需撰写结构化文章，请直接说明主题和写作要求。\n\n您说：{message[:100]}",
                "session_id": sm.session_id
            })

    # ---- LLM 模板生成 API ----

    def _handle_gen_template(self):
        """对话生成模板：描述 → planner.generate_template（按 SCHEMA 规矩，逻辑已迁移）"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        description = data.get("description", "").strip()
        if not description:
            self._json_response({"success": False, "error": "描述不能为空"}, 400)
            return
        name = data.get("name", "").strip()
        try:
            client = self._create_planner_client()
            result = generate_template(description, client, name=name)
            self._json_response({"success": True, "template": result})
        except Exception as e:
            self._json_response({"success": False, "error": str(e)}, 500)

    # ---- 快速范例 API（前置存大纲 + 完成回填文章 + 快速调用） ----

    def _handle_list_examples(self):
        """GET /api/examples — 范例摘要列表（不含全文）"""
        try:
            examples = self.config_mgr.list_examples()
            self._json_response({"success": True, "examples": examples})
        except Exception as e:
            self._json_response({"success": False, "error": str(e)}, 500)

    def _handle_save_example(self):
        """POST /api/example/save — 前置保存大纲为范例（article 留空，生成完成后回填）"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        outline = data.get("outline")
        if not isinstance(outline, dict) or not outline.get("sections"):
            self._json_response({"success": False, "error": "大纲为空，无法保存范例"}, 400)
            return
        name = str(data.get("name", "")).strip()
        if not name:
            name = str(outline.get("title", "未命名范例")).strip() or "未命名范例"
        saved = self.config_mgr.save_example({
            "name": name,
            "topic": outline.get("title", ""),
            "template_name": str(data.get("template_name", "")),
            "outline": copy.deepcopy(outline),
        })
        self._json_response({"success": True, "name": saved})

    def _handle_update_example_article(self):
        """POST /api/example/update_article — 生成完成后回填文章全文
        （前端只传 output_file，后端从文件读全文，避免截断预览）"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        name = str(data.get("name", "")).strip()
        output_file = str(data.get("output_file", "")).strip()
        if not name:
            self._json_response({"success": False, "error": "缺少范例名"}, 400)
            return
        article = ""
        if output_file and os.path.exists(output_file):
            try:
                with open(output_file, "r", encoding="utf-8") as f:
                    article = f.read()
            except OSError:
                article = ""
        ok = self.config_mgr.update_example_article(name, article, output_file)
        if not ok:
            self._json_response({"success": False, "error": f"范例「{name}」不存在"}, 404)
            return
        self._json_response({"success": True, "name": name, "article_chars": len(article)})

    def _handle_use_example(self):
        """POST /api/example/use — 快速调用：加载范例大纲，跳过 LLM 规划。
        返回协议与 /api/plan 一致 {outline, session_id}，前端直接进评审界面。"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        name = str(data.get("name", "")).strip()
        topic = str(data.get("topic", "")).strip()
        adapt = bool(data.get("adapt", False))
        if not name:
            self._json_response({"success": False, "error": "缺少范例名"}, 400)
            return
        example = self.config_mgr.get_example(name)
        if not example or not isinstance(example.get("outline"), dict):
            self._json_response({"success": False, "error": f"范例「{name}」不存在或大纲为空"}, 404)
            return
        outline = copy.deepcopy(example["outline"])
        if topic:
            outline["title"] = topic
        # 勾选「适配新主题」：LLM 只重写内容项（章节/子结构标题与要点），
        # 结构/RAG/辅助知识/字数/数量物理不变（adapt_outline 内部深拷贝，原大纲不动）
        if adapt:
            try:
                client = self._create_planner_client()
                outline = adapt_outline(outline, outline.get("title", ""), client)
            except (ValueError, LLMClientError) as e:
                self._json_response({"success": False, "error": f"适配失败：{e}"}, 500)
                return
        # 重置写作状态（范例大纲可能带上次的 done 状态）
        for s in outline.get("sections", []):
            s["status"] = "pending"
            s["actual_word_count"] = 0
            for ss in s.get("sub_sections", []):
                ss["status"] = "pending"
                ss["actual_word_count"] = 0
        sm = StateManager()
        sm.init_session(self.config_mgr.get_all())
        sm.set_outline(outline)
        self._json_response({
            "success": True,
            "outline": outline,
            "session_id": sm.session_id
        })

    # ---- 局部重规划（两级：整章 / 单子结构） ----

    def _handle_replan_section(self):
        """POST /api/replan_section — 只重做目标章节或子结构，其余节点不动。
        body: {session_id, target_id, hints}"""
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        session_id = str(data.get("session_id", "")).strip()
        target_id = str(data.get("target_id", "")).strip()
        hints = str(data.get("hints", "")).strip()
        if not session_id or not target_id:
            self._json_response({"success": False, "error": "缺少 session_id 或 target_id"}, 400)
            return
        try:
            sm = StateManager()
            sm.load(session_id)
        except FileNotFoundError:
            self._json_response({"success": False, "error": "会话不存在"}, 404)
            return

        outline = sm.get_state().get("outline", {})
        sections = outline.get("sections", [])
        target = None
        parent = None
        is_sub = False
        for s in sections:
            if s.get("id") == target_id:
                target = s
                break
            for ss in s.get("sub_sections", []):
                if ss.get("id") == target_id:
                    target = ss
                    parent = s
                    is_sub = True
                    break
            if target:
                break
        if target is None:
            self._json_response({"success": False, "error": f"未找到目标节点 {target_id}"}, 404)
            return

        # 当前模板 style/logic（供局部重规划参考）
        templates = self.config_mgr.get("templates", {})
        selected = self.config_mgr.get("selected_template", "")
        tmpl = templates.get(selected, {})
        if not isinstance(tmpl, dict):
            tmpl = {}

        # ── 小说线分支：outline 含 _novel 标记 → 走 novel 重规划（保持小说结构，评审界面仍只显示章） ──
        is_novel_line = bool(outline.get("_novel")) or any((s.get("_novel")) for s in sections)
        if is_novel_line:
            from .novel import novel_bridge as nb
            nv = target.get("_novel") or {}
            if not is_sub:
                # 章级：重做该章级大纲条目（title + overview，hints 一次性驱动）。
                # 两级分离语义：章级重规划不碰子结构——子结构是写作阶段 plan_chapter_subs 的事。
                # 章状态回 pending（子结构需按新概述重新规划），outline 保持章级。
                chapter_id = nv.get("chapter", "")
                state_path = nv.get("state_path", "")
                if not chapter_id or not state_path:
                    self._json_response({"success": False, "error": "小说章缺少身份字段"}, 400)
                    return
                # 注册 in-flight 标记（章级重规划期间拒绝确认）
                inflight = sm._state.setdefault("_replan_inflight", [])
                token = {"type": "section", "target_id": target_id, "chapter_id": chapter_id, "started_at": time.time()}
                inflight.append(token)
                sm.save()
                try:
                    try:
                        client = self._create_planner_client()
                        new_entry = nb.replan_novel_chapter(str(Path(state_path).resolve()), chapter_id, hints, client)
                    except Exception as e:
                        self._json_response({"success": False, "error": str(e)}, 500)
                        return
                    # 同步 session outline 该章（章级条目更新；子结构交给写作阶段按新概述重新规划）
                    target["title"] = new_entry.get("title", target.get("title", ""))
                    target["summary"] = new_entry.get("overview", target.get("summary", ""))
                    target["status"] = "pending"   # 子结构需重新规划
                    target["sub_sections"] = []
                    sm._state["outline"] = outline
                    sm.save()
                    self._json_response({"success": True, "outline": outline})
                finally:
                    inflight_now = sm._state.get("_replan_inflight", [])
                    sm._state["_replan_inflight"] = [
                        t for t in inflight_now
                        if not (t.get("type") == token["type"] and t.get("target_id") == token["target_id"] and t.get("started_at") == token["started_at"])
                    ]
                    sm.save()
                return
            # 段级（防御：评审大纲子结构行触发的段级重规划）
            chapter_id = nv.get("chapter", "")
            s_key = nv.get("s_key", "")
            state_path = nv.get("state_path", "") or ((parent.get("_novel") or {}).get("state_path", ""))
            if not chapter_id or not s_key or not state_path:
                self._json_response({"success": False, "error": "小说子结构缺少身份字段"}, 400)
                return
            # 注册 in-flight 标记
            inflight = sm._state.setdefault("_replan_inflight", [])
            token = {"type": "novel_sub", "target_id": target_id, "chapter_id": chapter_id, "s_key": s_key, "started_at": time.time()}
            inflight.append(token)
            sm.save()
            try:
                try:
                    client = self._create_planner_client()
                    new_entry = nb.replan_novel_sub(str(Path(state_path).resolve()), chapter_id, s_key, hints, client)
                except Exception as e:
                    self._json_response({"success": False, "error": str(e)}, 500)
                    return
                target.update({
                    "title": new_entry.get("title", target.get("title", "")),
                    "summary": new_entry.get("summary", target.get("summary", "")),
                    "word_count": (new_entry.get("word_count_target") or {}).get("max", target.get("word_count", 1500)),
                    "status": "pending",
                    "actual_word_count": 0,
                })
                if parent is not None and parent.get("sub_sections"):
                    parent["word_count"] = sum(ss.get("word_count", 0) for ss in parent["sub_sections"])
                sm._state["outline"] = outline
                sm.save()
                self._json_response({"success": True, "outline": outline})
            finally:
                inflight_now = sm._state.get("_replan_inflight", [])
                sm._state["_replan_inflight"] = [
                    t for t in inflight_now
                    if not (t.get("type") == token["type"] and t.get("target_id") == token["target_id"] and t.get("started_at") == token["started_at"])
                ]
                sm.save()
            return

        try:
            client = self._create_planner_client()
            new_node = replan_section(
                topic=outline.get("title", ""),
                hints=hints,
                llm_client=client,
                target=target,
                parent_section=parent,
                style=tmpl.get("style", ""),
                logic=tmpl.get("logic", ""),
            )
        except (ValueError, LLMClientError) as e:
            self._json_response({"success": False, "error": str(e)}, 500)
            return

        # 替换目标节点
        if is_sub:
            for j, ss in enumerate(parent.get("sub_sections", [])):
                if ss.get("id") == target_id:
                    parent["sub_sections"][j] = new_node
                    break
            parent["word_count"] = sum(ss.get("word_count", 0) for ss in parent.get("sub_sections", []))
            parent["status"] = "pending"
            parent["actual_word_count"] = 0
        else:
            for i, s in enumerate(sections):
                if s.get("id") == target_id:
                    sections[i] = new_node
                    break

        sm2 = StateManager()
        sm2.load(session_id)
        sm2._state["outline"] = outline
        sm2.set_phase("reviewing")
        sm2.save()
        self._json_response({"success": True, "outline": outline})

    # ═══════════════════════════════════════════════════════════
    # 插件系统端点
    # ═══════════════════════════════════════════════════════════
    def _handle_list_plugins(self):
        """GET /api/plugins — 列出已注册插件"""
        try:
            pm = get_plugin_manager()
            self._json_response({"success": True, "plugins": pm.list()})
        except Exception as e:
            self._json_response({"success": False, "error": str(e)}, 500)

    def _handle_plugin_run(self):
        """POST /api/plugin/run — 执行插件，结果归一化三类并落盘临时文件
        body: {plugin_id, inputs}
        table → 写临时 CSV，返回 path（前端挂载 aux_knowledge.files 用）
        text  → 直接返回 content
        """
        try:
            data = self._read_body()
        except ValueError as e:
            self._json_response({"success": False, "error": str(e)}, 400)
            return
        plugin_id = str(data.get("plugin_id", "")).strip()
        inputs = data.get("inputs", {}) or {}
        if not plugin_id:
            self._json_response({"success": False, "error": "缺少 plugin_id"}, 400)
            return
        pm = get_plugin_manager()
        result = pm.run(plugin_id, inputs)
        if "error" in result:
            self._json_response({"success": False, "error": result["error"]}, 400)
            return
        rtype = result.get("type", "text")
        name = str(result.get("name", "plugin_data"))
        content = result.get("content", "")
        if rtype == "table":
            # 表格 → 临时 CSV（写作时 select_table 蓝皮书取数走 path）
            tmp_dir = Path(__file__).resolve().parent.parent / "data" / "tmp" / "plugins"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{uuid.uuid4().hex[:12]}_{name}"
            fpath = tmp_dir / fname
            fpath.write_text(content, encoding="utf-8")
            preview_lines = content.splitlines()[:6]
            self._json_response({
                "success": True, "type": "table", "name": name,
                "path": str(fpath),
                "row_count": max(0, len(content.splitlines()) - 1),
                "preview": preview_lines,
            })
            return
        # text
        self._json_response({
            "success": True, "type": "text", "name": name,
            "content": content, "preview": content[:500],
        })

    def log_message(self, format, *args):
        """抑制默认日志输出"""
        pass


# ============================================================
# 内联 HTML 页面
# ============================================================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Structured Writer · 结构化写作</title>
<style>
:root {
  --bg: #1a1a2e;
  --bg-card: #16213e;
  --bg-panel: #0f3460;
  --bg-input: #1a1a3e;
  --text: #e0e0e0;
  --text-dim: #8899aa;
  --accent: #e94560;
  --accent2: #533483;
  --green: #00b894;
  --border: #2a2a4e;
  --radius: 8px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  overflow: hidden;
  /* 垂直布局链：topbar → tab-bar → tab-content(flex:1) → 内容区滚动。
     缺失 → .tab-content 的 flex:1 失效、内容超高被 overflow:hidden 裁切、无滚动轴 */
  display: flex;
  flex-direction: column;
}

/* 顶栏 */
.topbar {
  height: 48px;
  background: linear-gradient(135deg, var(--accent2), var(--accent));
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 8px;
  flex-shrink: 0;
}
.topbar .logo { font-weight: 700; font-size: 16px; }
.topbar .tag { font-size: 11px; opacity: 0.7; }

/* Tab 导航：页签条深色底（对齐 Orchestrator .tab-bar 风格），
   页签顶部圆角（对齐 RAG/编排体圆角卡片观感），hover 高亮 + active 色块选中 */
.tab-bar {
  display: flex;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  padding: 0 12px;
}
.tab-btn {
  padding: 10px 22px;
  cursor: pointer;
  color: var(--text-dim);
  font-size: 14px;
  border-radius: 8px 8px 0 0;
  margin: 6px 2px 0 0;
  border: 1px solid transparent;
  border-bottom: none;
  transition: all 0.2s;
}
.tab-btn:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.05);
}
.tab-btn.active {
  color: var(--text);
  background: var(--bg-card);
  border-color: var(--border);
}

/* Tab 内容区：默认隐藏，active 时显示。
   缺失此规则 → 所有 tab-content 垂直堆叠、active class 无视觉效果、
   tab 点击「毫无反应」、内容被挤到竖向流式布局。*/
.tab-content { display: none; flex: 1 1 0; min-height: 0; overflow: hidden; }
.tab-content.active {
  display: flex;
  flex-direction: column;
  /* calc 定高：100vh - (topbar 48 + tab-bar 48) = 内容区可用高度。
     实测 flex:1 的 basis 0% 在 body 无确定高度链时不收缩（高度=内容 2012px），
     导致配置内容被 body overflow:hidden 裁切、无滚动轴。calc 硬定高可靠。
     页签改圆角后 tab-bar 高 48（margin-top 6 + padding 10*2 + line-height），93→96 */
  height: calc(100vh - 96px);
  flex: none;
  min-height: 0;
}

/* 当前页签高亮已并入上方 .tab-btn.active（hover/active 对齐 Orchestrator 风格） */

/* 配置面板：居中容器（对齐 RAG .container max-width:1000px 风格）
   缺失 → 配置内容全宽铺开，宽屏下横跨整个屏幕 */
.config-panel {
  max-width: 1000px;
  width: 100%;
  margin: 0 auto;
  padding: 20px 24px 40px;
  box-sizing: border-box;
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
/* 配置区块卡片化（对齐 RAG .card 风格） */
.config-section {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  margin-bottom: 16px;
}
.config-section h3 { font-size: 14px; margin-bottom: 14px; color: var(--text); }
/* 表单行：label 固定宽 + 控件弹性 */
.form-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.form-row label { min-width: 76px; font-size: 13px; color: var(--text-dim); flex-shrink: 0; }
.form-row input[type="text"], .form-row input[type="number"], .form-row select {
  flex: 1; min-width: 150px; padding: 6px 8px;
  background: var(--bg-input); border: 1px solid var(--border);
  border-radius: 4px; color: var(--text); font-size: 13px;
}
.form-row input:focus, .form-row select:focus { outline: none; border-color: var(--accent); }
/* 配置区 textarea（风格/逻辑提示词等）：统一深色背景，resize 只允许上下拉动（宽度固定）。
   缺失 → 裸 textarea 白底、可四向 resize，与整体主题脱节 */
.config-section textarea {
  width: 100%;
  padding: 8px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
  box-sizing: border-box;
}
.config-section textarea:focus { outline: none; border-color: var(--accent); }
.btn {
  padding: 6px 16px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  transition: opacity 0.2s;
}
.btn:hover { opacity: 0.85; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-secondary { background: var(--bg-panel); color: var(--text); border: 1px solid var(--border); }
.btn-success { background: var(--green); color: #fff; }
.btn-danger { background: #c0392b; color: #fff; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.status-msg {
  font-size: 12px;
  padding: 4px 0;
  color: var(--text-dim);
}
.status-msg.success { color: var(--green); }
.status-msg.error { color: var(--accent); }

/* ===== 对话 Tab ===== */
.chat-container {
  display: flex;
  flex: 1;
  height: 100%;
}
.chat-sidebar {
  width: 220px;
  background: var(--bg-card);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.chat-sidebar .sidebar-header {
  padding: 12px;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: 8px;
}
.chat-sidebar .sidebar-header button {
  flex: 1;
}
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.session-item {
  padding: 8px 12px;
  cursor: pointer;
  font-size: 13px;
  border-left: 3px solid transparent;
  transition: all 0.15s;
}
.session-item:hover { background: rgba(255,255,255,0.05); }
.session-item.active {
  background: rgba(233, 69, 96, 0.1);
  border-left-color: var(--accent);
}
.session-item.archived { opacity: 0.5; }
.session-item .s-actions { display: flex; gap: 4px; margin-left: auto; flex-shrink: 0; }
.session-item .s-actions button {
  background: transparent; border: none; cursor: pointer; font-size: 11px; color: var(--text-dim); padding: 2px 4px;
}
.session-item .s-actions button:hover { color: var(--accent); }
.session-item .s-title { color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-item .s-meta { font-size: 11px; color: var(--text-dim); margin-top: 2px; }

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

/* 输入框顶部拖拽条：向上拉 = 输入框变高（往上是消息区，空间充足） */
.input-resizer {
  height: 6px;
  flex-shrink: 0;
  cursor: ns-resize;
  background: transparent;
  position: relative;
  transition: background 0.15s;
}
.input-resizer::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 2px;
  transform: translateX(-50%);
  width: 48px;
  height: 2px;
  border-radius: 1px;
  background: var(--border);
  transition: background 0.15s;
}
.input-resizer:hover::after,
.input-resizer.dragging::after {
  background: var(--accent);
}
.input-resizer.dragging {
  background: rgba(93, 173, 226, 0.08);
}

/* ===== 输出列表侧栏 ===== */
.outputs-sidebar {
  width: 240px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  background: var(--bg-card);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.outputs-sidebar .sidebar-header {
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.outputs-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}
.output-item {
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  transition: background 0.15s;
}
.output-item:hover { background: var(--bg-hover); }
.output-item .name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.output-item .date {
  font-size: 10px;
  color: var(--text-dim);
  white-space: nowrap;
  flex-shrink: 0;
}
.output-item .img-badge {
  font-size: 10px;
  color: #f39c12;
  background: rgba(243,156,18,0.15);
  padding: 0 4px;
  border-radius: 3px;
  flex-shrink: 0;
}
/* 小说树状：题目下挂章（可收起）+ 手动拼合 */
.output-item .tree-arrow {
  font-size: 10px;
  color: var(--text-dim);
  width: 12px;
  flex-shrink: 0;
}
.output-item .merge-btn {
  font-size: 10px;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  background: var(--bg-card);
  color: var(--text);
  cursor: pointer;
  flex-shrink: 0;
}
.output-item .merge-btn:hover { border-color: var(--accent); color: var(--accent); }
.output-chapter { padding-left: 26px; background: rgba(128,128,128,0.06); }
.novel-children { display: none; }
.output-item .del-btn {
  font-size: 12px;
  color: var(--accent);
  cursor: pointer;
  flex-shrink: 0;
  padding: 0 2px;
  opacity: 0.5;
}
.output-item .del-btn:hover { opacity: 1; }
.output-item .del-cancel { font-size:11px;cursor:pointer;color:var(--text-dim);padding:2px 4px;margin-left:2px; }
.output-item .del-cancel:hover { color:#e74c3c; }
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.msg {
  margin-bottom: 16px;
  max-width: 85%;
}
.msg.user {
  margin-left: auto;
}
.msg-content {
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
}
.msg.user .msg-content {
  background: var(--accent);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.msg.assistant .msg-content {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-bottom-left-radius: 4px;
}
.msg-label {
  font-size: 11px;
  color: var(--text-dim);
  margin-bottom: 4px;
  padding: 0 4px;
}
.chat-input-area {
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg-card);
  display: flex;
  gap: 8px;
}
.chat-input-area textarea {
  flex: 1;
  padding: 8px 12px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  font-size: 14px;
  resize: none;              /* 原生手柄禁用（右下角下拉空间小）；高度由顶部 .input-resizer 向上拉控制 */
  min-height: 40px;
  max-height: 60vh;          /* 最大不超过视口 60%，往上有消息区可收缩 */
  font-family: inherit;
}
.chat-input-area textarea:focus { outline: none; border-color: var(--accent); }
.chat-input-area button {
  padding: 8px 20px;
  align-self: flex-end;
}

/* 交互大纲卡片 */
.outline-card {
  background: var(--bg-card);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  padding: 16px;
  margin: 8px 0;
}
.outline-card .oc-title {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 12px;
  color: var(--accent);
}
.section-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 8px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.section-card .sc-label {
  font-weight: 600;
  font-size: 13px;
  flex: 1;
  min-width: 120px;
}
.section-card .sc-meta {
  font-size: 12px;
  color: var(--text-dim);
}
.section-card select, .section-card input[type=number] {
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  padding: 3px 6px;
  font-size: 12px;
}
.section-card .sc-rag {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}
.section-card .sc-rag input[type=checkbox] { accent-color: var(--accent); }
.section-card .sc-key {
  color: #f39c12;
  font-size: 12px;
}
.outline-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

/* 进度条 */
.progress-bar {
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  margin: 8px 0;
  overflow: hidden;
}
.progress-bar .fill {
  height: 100%;
  background: var(--green);
  transition: width 0.3s;
  border-radius: 2px;
}

/* Markdown 基础样式 */
.msg-content h1, .msg-content h2, .msg-content h3 {
  margin: 8px 0 4px;
}
.msg-content p { margin: 4px 0; }
.msg-content ul, .msg-content ol { padding-left: 20px; }
.msg-content code {
  background: rgba(255,255,255,0.1);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 13px;
}
.msg-content pre code {
  display: block;
  padding: 8px;
  overflow-x: auto;
}
.msg-content a { color: #5dade2; }

/* 模态框 */
.modal-overlay {
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  z-index: 1000; align-items: center; justify-content: center;
}
.modal-overlay.show { display: flex; }
.modal-box {
  background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
  width: 520px; max-width: 90vw; max-height: 80vh; display: flex; flex-direction: column;
}
.modal-header {
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; justify-content: space-between;
}
.modal-header h3 { font-size: 14px; color: var(--accent); font-weight: 500; }
.modal-close { cursor: pointer; color: var(--text-dim); font-size: 18px; background: none; border: none; padding: 0 4px; }
.modal-body { padding: 16px; overflow-y: auto; flex: 1; }
.modal-body textarea {
  width: 100%; min-height: 120px; padding: 8px; background: var(--bg-input);
  border: 1px solid var(--border); border-radius: 4px; color: var(--text);
  font-size: 13px; font-family: inherit; resize: vertical;
}
.modal-body .file-upload-area {
  border: 1px dashed var(--border); border-radius: 4px; padding: 16px;
  text-align: center; margin-top: 12px; cursor: pointer; font-size: 13px; color: var(--text-dim);
}
.modal-body .file-upload-area:hover { border-color: var(--accent); }
.modal-body .file-list { margin-top: 8px; }
.modal-body .file-item {
  display: flex; align-items: center; gap: 8px; padding: 4px 8px;
  background: var(--bg); border-radius: 4px; margin-bottom: 4px; font-size: 12px;
}
.modal-body .file-item .file-del { cursor: pointer; color: var(--accent); font-size: 14px; }
.modal-footer {
  padding: 12px 16px; border-top: 1px solid var(--border);
  display: flex; gap: 8px; justify-content: flex-end;
}
</style>
</head>
<body>

<div class="topbar">
  <span class="logo">✎ Structured Writer</span>
  <span class="tag">结构化写作</span>
</div>

<div class="tab-bar">
  <button class="tab-btn active" data-tab="config">配置</button>
  <button class="tab-btn" data-tab="chat">对话</button>
</div>

<div class="main-container">

  <!-- ===== 配置面板 ===== -->
  <div class="tab-content active" id="tab-config">
    <div class="config-panel">
      <div class="config-section">
        <h3>🔧 规划模型</h3>
        <div class="form-row">
          <label>后端</label>
          <select id="planner-backend"><option value="lmstudio" selected>LM Studio</option><option value="ollama">Ollama</option></select>
        </div>
        <div class="form-row">
          <label>地址</label>
          <input type="text" id="planner-base-url" value="http://localhost:1234">
        </div>
        <div class="form-row">
          <label>模型</label>
          <select id="planner-model" style="flex:2"><option value="">(请选择)</option></select>
          <button class="btn btn-secondary btn-sm" onclick="refreshModels('planner')">刷新</button>
        </div>
        <div class="form-row" style="flex-wrap:nowrap">
          <label style="min-width:auto;white-space:nowrap">超时(s)</label>
          <input type="number" id="planner-timeout" value="180" style="width:100px;flex-shrink:0">
          <label style="min-width:auto;white-space:nowrap">最大Token</label>
          <input type="number" id="planner-max-tokens" value="4096" style="width:120px;flex-shrink:0" title="生成上限（写窗口）">
          <label style="min-width:auto;white-space:nowrap">温度</label>
          <input type="number" id="planner-temperature" value="0.6" min="0" max="1" step="0.05" style="width:70px;flex-shrink:0">
        </div>
        <div id="planner-window-tip" style="font-size:11px;color:var(--text-dim);margin:2px 0 0 8px;min-height:14px"></div>
      </div>

      <div class="config-section">
        <h3>✍️ 写作模型</h3>
        <div class="form-row">
          <label>后端</label>
          <select id="writer-backend"><option value="lmstudio" selected>LM Studio</option><option value="ollama">Ollama</option></select>
        </div>
        <div class="form-row">
          <label>地址</label>
          <input type="text" id="writer-base-url" value="http://localhost:1234">
        </div>
        <div class="form-row">
          <label>模型</label>
          <select id="writer-model" style="flex:2"><option value="">(请选择)</option></select>
          <button class="btn btn-secondary btn-sm" onclick="refreshModels('writer')">刷新</button>
        </div>
        <div class="form-row" style="flex-wrap:nowrap">
          <label style="min-width:auto;white-space:nowrap">超时(s)</label>
          <input type="number" id="writer-timeout" value="300" style="width:100px;flex-shrink:0">
          <label style="min-width:auto;white-space:nowrap">最大Token</label>
          <input type="number" id="writer-max-tokens" value="8192" style="width:120px;flex-shrink:0" title="生成上限（写窗口）">
          <label style="min-width:auto;white-space:nowrap">温度</label>
          <input type="number" id="writer-temperature" value="0.7" min="0" max="1" step="0.05" style="width:70px;flex-shrink:0">
        </div>
        <div id="writer-window-tip" style="font-size:11px;color:var(--text-dim);margin:2px 0 0 8px;min-height:14px"></div>
      </div>

      <div class="config-section">
        <h3>📝 模板管理</h3>
        <div class="form-row">
          <label>模板</label>
          <div style="flex:1;display:flex;gap:4px;align-items:center">
            <div style="position:relative;flex:1">
              <select id="template-select" style="width:100%;padding:4px 6px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:13px" onchange="onTemplateChange()" size="1">
              </select>
            </div>
            <span id="template-type-badge" style="font-size:11px;padding:2px 6px;border-radius:3px;white-space:nowrap"></span>
          </div>
          <button class="btn btn-primary btn-sm" onclick="saveCurrentTemplate()" id="save-current-template-btn" title="直接保存当前模板的修改">保存</button>
          <button class="btn btn-secondary btn-sm" onclick="saveTemplateAs()">另存为</button>
          <button class="btn btn-danger btn-sm" onclick="deleteTemplate()" title="删除当前自定义模板">删除</button>
          <button class="btn btn-success btn-sm" onclick="openGenTemplate()">从对话生成</button>
        </div>

        <div class="form-row">
          <label style="font-weight:600;color:#f39c12">元数据</label>
          <div style="flex:1;font-size:12px;color:var(--text-dim)">
            标识/管理信息，短数据（≤100字），以键值对渲染，不参与大纲规划。每行：名称 | 显 | 字段要求 | 填写者<br>
            <span style="color:var(--text-dim)">显：打钩=文章开头显示"字段名：值"（如"作者：张三"）；不打钩=不显示字段名标签，有值仍显示裸值（如"张三"）。<b>特殊</b>：小说模板的驱动字段（题材/篇幅/叙事视角）不打钩=彻底不显示，仅作流程参数（题材→场景配置、篇幅→字数目标）</span><br>
            <span style="color:var(--text-dim)">字段要求：该字段的写作提示词，作为元数据确定性注入写作 prompt</span><br>
            <span style="color:var(--text-dim)">填写者：<b>用户</b>（你手动填写的值）| <b>LLM</b>（由 AI 在规划时自动填写）| <b>自动</b>（你填了就保留，没填则 LLM 自动补）</span>
          </div>
        </div>
        <div id="meta-editor" style="overflow-x:auto;margin-bottom:12px">
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead>
              <tr style="background:var(--bg-input)">
                <th style="padding:4px 6px;text-align:left;min-width:80px">名称</th>
                <th style="padding:4px 6px;text-align:center;width:40px">显</th>
                <th style="padding:4px 6px;text-align:left;min-width:120px">字段要求</th>
                <th style="padding:4px 6px;text-align:center;width:65px">填写</th>
                <th style="padding:4px 6px;text-align:center;width:30px"></th>
              </tr>
            </thead>
            <tbody id="meta-rows">
            </tbody>
          </table>
        </div>
        <div class="form-row">
          <button class="btn btn-secondary btn-sm" onclick="addMetaRow()">+ 添加元数据</button>
        </div>

        <div class="form-row" style="margin-top:4px">
          <label style="font-weight:600;color:#5dade2">内容树</label>
          <div style="flex:1;font-size:12px;color:var(--text-dim)">
            文章主体，长文本（≥200字），参与大纲规划，可拆分子结构。每行：名称 | 显 | 字段要求 | 子结构 | 逻辑顺序 | 引用列表<br>
            <span style="color:var(--text-dim)">显：控制该节的标题行（## 标题）是否在文章中显示。不打钩=只输出内容、无标题行；且内容为空时整节彻底跳过（如小说设定节点"世界观设定"）。内容本身不受「显」控制，写了就一定输出</span><br>
            <span style="color:var(--text-dim)">字段要求：该节的写作提示词，指导 LLM 如何撰写此节内容</span><br>
            <span style="color:var(--text-dim)">引用列表：☐ [x] = 1.（勾选框 + 正文引用标记 + 参考文献编号格式）——☐ 勾选后该节跳过 LLM 写作，由系统根据 RAG 文档自动生成规范化参考文献（需配合 RAG 使用）；[x] 为正文引用标记格式，1. 为参考文献条目编号前缀格式</span><br>
            <span style="color:var(--text-dim)">子结构：该节是否可拆分为多个子段落分别撰写</span><br>
            <span style="color:var(--text-dim)">逻辑顺序：控制写作先后（先写/其次/最后），不影响文章最终排列</span>
          </div>
        </div>
        <div id="content-editor" style="overflow-x:auto;margin-bottom:8px">
          <table style="width:100%;border-collapse:collapse;font-size:12px">
            <thead>
              <tr style="background:var(--bg-input)">
                <th style="padding:4px 6px;text-align:left;min-width:80px">名称</th>
                <th style="padding:4px 6px;text-align:center;width:40px">显</th>
                <th style="padding:4px 6px;text-align:left;min-width:120px">字段要求</th>
                <th style="padding:4px 6px;text-align:center;width:65px">子结构</th>
                <th style="padding:4px 6px;text-align:center;width:65px">逻辑顺序</th>
                <th style="padding:4px 6px;text-align:center;width:70px">引用列表</th>
                <th style="padding:4px 6px;text-align:center;width:30px"></th>
              </tr>
            </thead>
            <tbody id="content-rows">
            </tbody>
          </table>
        </div>
        <div class="form-row">
          <button class="btn btn-secondary btn-sm" onclick="addContentRow()">+ 添加内容</button>
        </div>

        <div class="form-row">
          <label>风格提示词</label>
          <div style="display:flex;flex-direction:column;flex:1;gap:4px">
            <textarea id="template-style" rows="3" placeholder="写作风格说明，如：请以学术论文风格撰写..."></textarea>
            <span style="font-size:11px;color:var(--text-dim)">控制文风和语气，注入每一步写作 prompt</span>
          </div>
        </div>
        <div class="form-row">
          <label>逻辑提示词</label>
          <div style="display:flex;flex-direction:column;flex:1;gap:4px">
            <textarea id="template-logic" rows="2" placeholder="写作顺序说明，如：先写引言和正文，再写结论，最后提取关键词和摘要。留空则按文章顺序写。"></textarea>
            <span style="font-size:11px;color:var(--text-dim)">控制 LLM 认知流程顺序（先写什么后写什么），不改变文章最终排列</span>
          </div>
        </div>
        <div style="font-size:12px;color:#e67e22;margin-top:8px;padding:6px 10px;background:rgba(230,126,34,0.1);border-radius:4px">
          ⚠️ 修改表格后点「保存」直接更新当前模板，或「另存为」创建副本。
        </div>
      </div>



      <div class="config-section">
        <h3>🔗 RAG 知识库</h3>
        <div class="form-row">
          <label>RAG 路径</label>
          <input type="text" id="rag-path" value="" placeholder="C:\Users\YourName\WorkBuddy\rag-assistant" style="flex:2">
          <button class="btn btn-secondary btn-sm" onclick="saveRagPath()">保存路径</button>
        </div>
        <div class="form-row">
          <label>状态</label>
          <span id="rag-status-indicator" style="font-weight:600">检测中...</span>
        </div>
        <div class="form-row">
          <label>操作</label>
          <button class="btn btn-success" id="rag-start-btn" onclick="startRag()" disabled>启动 RAG</button>
          <button class="btn btn-secondary" id="rag-stop-btn" onclick="stopRag()" disabled style="margin-left:4px">停止 RAG</button>
          <button class="btn btn-secondary btn-sm" onclick="checkRagStatus()">刷新状态</button>
        </div>
        <div class="form-row" id="rag-kb-row" style="display:none">
          <label>可用知识库</label>
          <span id="rag-kb-list" style="font-size:13px;color:var(--text-dim)"></span>
        </div>
      </div>

      <div class="config-section">
        <h3>📖 小说质检</h3>
        <div class="form-row">
          <label>模型目录</label>
          <input type="text" id="novel-model-dir" value="" placeholder="data/models（R1推理 + 3B提取）" style="flex:2">
          <button class="btn btn-secondary btn-sm" onclick="checkNovelModels()">检测模型</button>
        </div>
        <div class="form-row">
          <label>状态</label>
          <span id="novel-model-status" style="font-weight:600">未检测</span>
        </div>
        <div class="form-row">
          <label>操作</label>
          <button class="btn btn-secondary btn-sm" id="novel-install-btn" onclick="installNovelModels()">安装缺失模型</button>
          <span style="font-size:11px;color:var(--text-dim)">transformers 方案：R1 约3.7GB / Qwen2.5-3B 约1.9GB；LM Studio 方案（统一勾选）：8B+7B GGUF 约9.4GB（自动下载进 LM Studio 模型库；判定+提取全走 8B/7B，无需 3B）。走镜像源；无模型时自动降级：4维 缺失 → 回退规则连通性+逻辑检查；抽取缺失 → 正则兜底提取；R1 缺失 → 跳过</span>
        </div>
        <div class="form-row">
          <label>判定后端</label>
          <label style="font-size:12px;cursor:pointer" title="统一管理 = 写作规划与审查判定统一走 LM Studio（lms load 进 GPU，显存错峰共用）：判定模型切换为 8B+7B。勾选 → 8B/7B 走 LM Studio GPU；未勾选 → 判定固定 transformers 3B+1.5B。写作后端为 Ollama 时不适用（Ollama 未接入判定联动）。判定窗口固定 16384（lms load -c，覆盖 4维~3K / R1~13K）"><input type="checkbox" id="novel-chk-unified" onchange="saveNovelChecks()"> 统一管理（LM Studio 判定 8B+7B）</label>
          <label style="font-size:12px;cursor:pointer;margin-left:10px" title="独占串行 = 一次只驻留一个模型，用完即卸载（写作 35B → 章检前卸载 → 8B 判定 → 卸载 → 7B 审核 → 卸载 → 下一章写作再加载）。保证 8B/7B 判定真正吃到 GPU（实测：35B 常驻时 8B/7B 会被 LM Studio 降级到 CPU，慢 5-8 倍）。⚠️ 关闭本功能请先确认硬件足够——关闭后模型加载常驻不卸载，多模型同时驻留需大显存/大内存"><input type="checkbox" id="novel-chk-serial" onchange="saveNovelChecks()"> 独占串行（一次一模型，用完即卸）</label>
          <span id="novel-serial-warn" style="display:none;color:#e67e22;font-size:11px;margin-left:8px">⚠️ 关闭独占串行：建议将 max concurrency 设置为 ≥2（多模型常驻可并行推理）</span>
          <span id="novel-judge-backend" style="font-size:11px;color:var(--text-dim);margin-left:8px"></span>
        </div>
        <div class="form-row" id="novel-gguf-row" style="display:none">
          <label>GGUF</label>
          <span id="novel-gguf-status" style="font-size:12px;color:var(--text-dim)"></span>
        </div>
        <div class="form-row">
          <label>章内检测</label>
          <span style="font-size:11px;color:var(--text-dim)">点位：每章完结 finalize-chapter 时执行</span>
        </div>
        <div class="form-row">
          <label style="padding-left:16px">开关</label>
          <label style="font-size:12px;cursor:pointer" title="3B 一次判时间衔接/情绪匹配/话题过渡/角色承接，约2-4分钟/章；3B 缺失自动回退规则连通性+逻辑检查"><input type="checkbox" id="novel-chk-chapter" onchange="saveNovelChecks()"> 章内连贯性4维（3B）</label>
          <label style="font-size:12px;cursor:pointer;margin-left:10px" title="末行标记/禁用模式/文件数，规则毫秒级"><input type="checkbox" id="novel-chk-format" onchange="saveNovelChecks()"> 格式校验（规则）</label>
          <label style="font-size:12px;cursor:pointer;margin-left:10px" title="对话匹配/行为一致，R1 本地离线，约1分钟/章"><input type="checkbox" id="novel-chk-reason" onchange="saveNovelChecks()"> 推理审核（R1）</label>
          <label style="font-size:12px;cursor:pointer;margin-left:16px;color:var(--sc-key)" title="章检 HARD 时自动全选修复，T0 格式自动修 + T1 写作模型重构"><input type="checkbox" id="novel-chk-autorepair" onchange="saveNovelChecks()"> 自动修复</label>
          <label style="font-size:12px;margin-left:8px;color:var(--text-dim)">轮次 <input type="number" id="novel-chk-rounds" value="3" min="1" max="5" style="width:44px;font-size:12px;background:var(--bg);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:1px 4px" onchange="saveNovelChecks()"></label>
        </div>
        <div class="form-row">
          <label>全文检测</label>
          <span style="font-size:11px;color:var(--text-dim)">点位：全部写完 finalize-novel 时执行</span>
        </div>
        <div class="form-row">
          <label style="padding-left:16px">开关</label>
          <label style="font-size:12px;cursor:pointer" title="子结构概述 vs 正文，词面全量筛 + 可疑段 3B 复核，约30s-1分钟；慢但比纯词面提升大（抓同义改写/语义反转）"><input type="checkbox" id="novel-chk-fid" onchange="saveNovelChecks()"> 大纲忠实度（3B复核）</label>
          <label style="font-size:12px;cursor:pointer;margin-left:10px;color:#e67e22" title="3B 提取 flag + 写作模型推理兑现，⚠️ 约3-5分钟/次（模型思考慢）；慢但提升大——flag 收束检查是纯规则做不到的"><input type="checkbox" id="novel-chk-pledge" onchange="saveNovelChecks()"> 全文承诺（⚠️慢，提升大）</label>
          <label style="font-size:12px;cursor:pointer;margin-left:10px" title="最后一段判收尾类型（封闭/开放/悬停），3B 约15s，回退特征词"><input type="checkbox" id="novel-chk-ending" onchange="saveNovelChecks()"> 结尾收束（3B）</label>
        </div>
      </div>

      <div class="config-section">
        <h3>⚙️ 写作参数</h3>
        <div class="form-row">
          <label>前文回顾字数</label>
          <input type="number" id="context-review-length" value="8000" min="100" max="32000" style="width:100px;">
          <span style="font-size:12px;color:var(--text-dim)">写作时注入前文上下文的最大字符数</span>
        </div>
        <div class="form-row">
          <label>事实自检</label>
          <label style="font-size:13px;cursor:pointer"><input type="checkbox" id="fact-check-enabled" onchange="saveConfig()"> 开启（写作完成后自动标记可疑事实）</label>
        </div>
      </div>

      <div class="form-row">
        <button class="btn btn-success" onclick="testConnection()">测试连接</button>
        <button class="btn btn-primary" onclick="saveConfig()">保存配置</button>
        <span id="config-status" class="status-msg"></span>
      </div>
    </div>
  </div>

  <!-- ===== 对话面板 ===== -->
  <div class="tab-content" id="tab-chat">
    <div class="chat-container">
      <div class="chat-sidebar">
        <div class="sidebar-header">
          <button class="btn btn-sm btn-primary" onclick="newSession()">新建</button>
        </div>
        <div class="session-list" id="session-list"></div>
        <div id="sidebar-archived" style="display:none;border-top:1px solid var(--border);flex-shrink:0">
          <div onclick="toggleArchived()" style="padding:6px 12px;cursor:pointer;font-size:12px;color:var(--text-dim);user-select:none;">
            <span id="archived-toggle">▸</span> 归档会话 (<span id="archived-count">0</span>)
          </div>
          <div id="sidebar-archived-list" style="max-height:200px;overflow-y:auto"></div>
        </div>
      </div>
      <div class="chat-main">
        <div class="chat-messages" id="chat-messages">
          <div class="msg assistant">
            <div class="msg-label">助手</div>
            <div class="msg-content">欢迎使用结构化写作助手。请在下方输入写作主题，我将为您生成大纲并协助完成文章。</div>
          </div>
        </div>
        <div id="meta-inputs-bar" style="display:none;padding:8px 16px;border-top:1px solid var(--border);background:var(--bg-card);font-size:13px">
          <div style="display:flex;flex-wrap:wrap;gap:8px" id="meta-inputs-container"></div>
        </div>
        <div class="input-resizer" id="input-resizer" title="向上拖动拉高输入框；双击复位" onmousedown="startInputResize(event)" ondblclick="resetInputResize()"></div>
        <div class="chat-input-area">
          <textarea id="chat-input" placeholder="输入写作主题...（多行=批量自动撰写）" rows="2"
            onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMessage()}"></textarea>
          <button class="btn btn-primary" onclick="sendMessage()">发送</button>
          <button class="btn btn-success" onclick="startAutoGeneration()">自动撰写</button>
        </div>
        <div class="example-quick-bar" style="padding:6px 16px;border-top:1px solid var(--border);background:var(--bg-card);font-size:12px;display:flex;gap:8px;align-items:center">
          <span style="color:var(--text-dim);flex-shrink:0">快速范例：</span>
          <select id="example-select" style="flex:1;min-width:120px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:4px 6px;font-size:12px">
            <option value="">（选择已保存的范例）</option>
          </select>
          <input type="text" id="example-topic" placeholder="新主题（可选，覆盖范例标题）" style="flex:1.2;min-width:140px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:4px 6px;font-size:12px">
          <label style="color:var(--text-dim);white-space:nowrap;cursor:pointer;flex-shrink:0" title="勾选后 LLM 按新主题重写章节/子结构标题与要点，结构/RAG/辅助知识/字数不变">
            <input type="checkbox" id="example-adapt" style="vertical-align:middle"> 适配新主题
          </label>
          <button class="btn btn-sm btn-primary" style="background:var(--accent2);flex-shrink:0" onclick="useExample()" title="跳过 LLM 规划，直接基于范例大纲写作">用范例写作（跳过规划）</button>
        </div>
        <div id="batch-progress" style="display:none;padding:8px 16px;border-top:1px solid var(--border);background:var(--bg-card);font-size:13px;color:var(--text-dim);flex-shrink:0"></div>
        <div id="stop-bar" style="display:none;padding:4px 16px;border-top:1px solid var(--border);background:var(--bg-card);font-size:12px;text-align:center;flex-shrink:0">
          <button class="btn btn-sm btn-secondary" onclick="stopGeneration('delay')">延时停止</button>
          <button class="btn btn-sm btn-secondary" style="background:var(--accent);color:#fff" onclick="stopGeneration('immediate')">立即停止</button>
        </div>
        <div id="novel-confirm-panel" style="display:none;padding:8px 16px;border-top:1px solid var(--border);background:var(--bg-card);flex-shrink:0"></div>
        <div id="novel-repair-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:2000;align-items:center;justify-content:center">
          <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;width:90%;max-width:560px;max-height:80vh;display:flex;flex-direction:column">
            <div id="novel-repair-panel" style="padding:14px 18px;display:flex;flex-direction:column;overflow:hidden"></div>
          </div>
        </div>
      </div>
      <div class="outputs-sidebar" id="outputs-sidebar">
        <div class="sidebar-header">已完成文章</div>
        <div class="outputs-list" id="outputs-list"></div>
      </div>
    </div>
  </div>

</div>

      <!-- LLM 生成模板模态框 -->
      <div class="modal-overlay" id="gen-template-modal">
        <div class="modal-box">
          <div class="modal-header">
            <h3>从对话生成模板</h3>
            <button class="modal-close" onclick="closeGenTemplate()">&times;</button>
          </div>
          <div class="modal-body">
            <p style="font-size:13px;color:var(--text-dim);margin-bottom:8px">描述你需要的文档结构，例如：</p>
            <p style="font-size:12px;color:#f39c12;margin-bottom:8px">"我要写技术报告，需要作者、版本号、背景、技术方案、风险评估、下一步计划"</p>
            <div style="display:flex;gap:8px;margin-bottom:8px">
              <input type="text" id="gen-template-name" style="flex:1;padding:6px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:13px" placeholder="模板名称（留空LLM自动生成）">
            </div>
            <textarea id="gen-template-desc" rows="4" placeholder="在这里描述你的文档结构需求..."></textarea>
          </div>
          <div class="modal-footer">
            <span id="gen-template-status" style="font-size:12px;color:var(--text-dim);flex:1"></span>
            <button class="btn btn-secondary" onclick="closeGenTemplate()">取消</button>
            <button class="btn btn-primary" onclick="generateTemplate()">生成并保存</button>
          </div>
        </div>
      </div>

      <!-- 字段要求编辑模态框 -->
      <div class="modal-overlay" id="desc-modal" style="z-index:100">
        <div class="modal-box" style="max-width:500px">
          <div class="modal-header">
            <h3>编辑字段要求</h3>
            <button class="modal-close" onclick="closeDescModal()">&times;</button>
          </div>
          <div class="modal-body">
            <p style="font-size:13px;color:var(--text-dim);margin-bottom:8px">该字段的写作提示词，将作为"本节要求"确定性注入写作 prompt，指导 LLM 如何撰写此节内容。</p>
            <p style="font-size:12px;color:#f39c12;margin-bottom:8px">如需多级子标题，在描述中写明即可，如："按 章→节→条→款 四级展开，子标题用 ####/#####"</p>
            <textarea id="desc-editor" rows="6" placeholder="输入字段的详细意义..."></textarea>
          </div>
          <div class="modal-footer">
            <span id="desc-modal-status" style="font-size:12px;color:var(--text-dim);flex:1"></span>
            <button class="btn btn-secondary" onclick="closeDescModal()">取消</button>
            <button class="btn btn-primary" onclick="saveDescModal()">确认</button>
          </div>
        </div>
      </div>

      <!-- 另存为模板模态框 -->
      <div class="modal-overlay" id="saveas-modal" style="z-index:100">
        <div class="modal-box" style="max-width:400px">
          <div class="modal-header">
            <h3>另存为模板</h3>
            <button class="modal-close" onclick="closeSaveAsModal()">&times;</button>
          </div>
          <div class="modal-body">
            <p style="font-size:13px;color:var(--text-dim);margin-bottom:8px">输入新模板名称：</p>
            <input type="text" id="saveas-name" style="width:100%;padding:6px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:14px" placeholder="模板名称" autofocus>
          </div>
          <div class="modal-footer">
            <span id="saveas-modal-status" style="font-size:12px;color:var(--text-dim);flex:1"></span>
            <button class="btn btn-secondary" onclick="closeSaveAsModal()">取消</button>
            <button class="btn btn-primary" onclick="confirmSaveAs()">确认保存</button>
          </div>
        </div>
      </div>

      <!-- 重新规划输入模态框（整篇 + 局部两级共用；_replanTarget 区分） -->
      <div class="modal-overlay" id="replan-modal" style="z-index:100">
        <div class="modal-box" style="max-width:500px">
          <div class="modal-header">
            <h3 id="replan-modal-title">调整规划</h3>
            <button class="modal-close" onclick="closeReplanModal()">&times;</button>
          </div>
          <div class="modal-body">
            <p id="replan-modal-hint" style="font-size:13px;color:var(--text-dim);margin-bottom:8px">输入对当前大纲的调整要求。留空则使用原有规划不变。</p>
            <p style="font-size:12px;color:#f39c12;margin-bottom:8px">例如：第2节加3个子结构、结论改800字、正文分5个部分每部分600字、删除第4节</p>
            <textarea id="replan-hints" rows="6" placeholder="输入调整要求（留空则按原规划重跑）..."></textarea>
          </div>
          <div class="modal-footer">
            <span id="replan-modal-status" style="font-size:12px;color:var(--text-dim);flex:1"></span>
            <button class="btn btn-secondary" onclick="closeReplanModal()">取消</button>
            <button class="btn btn-primary" onclick="confirmReplan()">确认重新规划</button>
          </div>
        </div>
      </div>

      <!-- 保存范例并生成模态框 -->
      <div class="modal-overlay" id="example-modal" style="z-index:100">
        <div class="modal-box" style="max-width:420px">
          <div class="modal-header">
            <h3>保存为快速范例并生成</h3>
            <button class="modal-close" onclick="closeExampleModal()">&times;</button>
          </div>
          <div class="modal-body">
            <p style="font-size:13px;color:var(--text-dim);margin-bottom:8px">先保存当前大纲为快速范例，然后开始写作；生成完成后文章自动回填进范例，下次可一键调用（跳过 LLM 规划）。</p>
            <input type="text" id="example-name" style="width:100%;padding:6px 8px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:14px" placeholder="范例名称（默认=文章标题）" autofocus>
          </div>
          <div class="modal-footer">
            <span id="example-modal-status" style="font-size:12px;color:var(--text-dim);flex:1"></span>
            <button class="btn btn-secondary" onclick="closeExampleModal()">取消</button>
            <button class="btn btn-primary" onclick="confirmSaveExample()">保存并生成</button>
          </div>
        </div>
      </div>

<script>
// ===== 全局状态 =====
let currentSessionId = '';
let currentOutline = null;
let isGenerating = false;
let ragOnline = false;
let ragKbs = [];
let _replanTarget = null;   // 局部重规划目标 {type: 'section'|'sub', id}
let _replanBusy = null;     // 局部重规划在途 {type:'novel_sub'|'section'|'sub', id}：行内反馈 + 防确认竞态
let _pendingExampleName = null;  // 保存范例并生成：等待生成完成后回填文章的范例名
let _ncConfirmId = null;    // 章级门控确认面板：当前确认章 id（防轮询重复重建丢用户输入）
let _ncConfirming = false;  // 确认提交中：防重复点击

function escapeAttr(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ===== 输入框高度拖拽（顶部条：向上拉 = 变高；双击复位） =====
function startInputResize(ev) {
  ev.preventDefault();
  const ta = document.getElementById('chat-input');
  const resizer = document.getElementById('input-resizer');
  if (!ta || !resizer) return;
  const startY = ev.clientY;
  const startH = ta.offsetHeight;
  resizer.classList.add('dragging');
  document.body.style.cursor = 'ns-resize';
  document.body.style.userSelect = 'none';

  function onMove(e) {
    // 向上拖（e.clientY < startY）→ dy 为正 → 高度增加
    const dy = startY - e.clientY;
    // 动态上限：chat-main 总高 - 其他固定区域（stop-bar/batch/confirm-panel/example-bar/meta 等）- 消息区最小保留 120px
    const main = ta.closest('.chat-main');
    const mainH = main ? main.clientHeight : window.innerHeight;
    const inputArea = ta.closest('.chat-input-area');
    let fixedH = 0;
    if (main) {
      Array.from(main.children).forEach(el => {
        if (el === inputArea || el.id === 'chat-messages') return;
        if (el.style && el.style.display === 'none') return;
        fixedH += el.offsetHeight || 0;
      });
    }
    const nonTaH = inputArea ? (inputArea.offsetHeight - ta.offsetHeight) : 0;
    const msgMin = 120;
    const maxH = Math.max(40, mainH - fixedH - nonTaH - msgMin);
    const h = Math.max(40, Math.min(startH + dy, maxH));
    ta.style.height = h + 'px';
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    resizer.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function resetInputResize() {
  const ta = document.getElementById('chat-input');
  if (!ta) return;
  ta.style.height = '';
}

// ===== 初始化 =====
document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  loadSessions();
  checkRagStatus();
  loadOutputs();
  loadExamples();

  // Tab 切换
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    });
  });

  // 后端切换触发模型刷新
  document.getElementById('planner-backend').addEventListener('change', () => onBackendChange('planner'));
  document.getElementById('writer-backend').addEventListener('change', () => onBackendChange('writer'));
  // 模型配置自动持久化：地址失焦/模型选择/参数改动即保存
  ['planner', 'writer'].forEach(p => {
    document.getElementById(p + '-base-url').addEventListener('change', autoSaveModelConfig);
    document.getElementById(p + '-model').addEventListener('change', () => {
      _modelValues[p] = document.getElementById(p + '-model').value;
      autoSaveModelConfig();
    });
    [p + '-timeout', p + '-max-tokens', p + '-temperature'].forEach(id =>
      document.getElementById(id).addEventListener('change', autoSaveModelConfig));
    // 最大Token 改动 → 查窗口提示（读写平衡，只提示不强制）
    document.getElementById(p + '-max-tokens').addEventListener('change', () => checkWindowTip(p));
    document.getElementById(p + '-backend').addEventListener('change', () => checkWindowTip(p));
  });
});

// 窗口提示：llama.cpp 后端已废弃（LM Studio/Ollama 无 n_ctx 直读需求）→ 恒清空
function checkWindowTip(prefix) {
  const tipEl = document.getElementById(prefix + '-window-tip');
  if (tipEl) tipEl.textContent = '';
}

// ===== 配置操作 =====
// 模型配置自动持久化：后端/地址/模型/参数改动即保存（不依赖"保存配置"按钮，与 novel_checks 一致）
// 模型值用内存变量记（_modelValues）——不读 DOM 下拉框：refreshModels 异步重建下拉框时 value 会短暂变空，
// autoSave 若读 DOM 会存空覆盖（用户实测"选了模型却存成空"的根因）。内存值在恢复/手动选择时同步更新。
let _modelValues = {planner: '', writer: ''};
let _modelCfgTimer = null;
function autoSaveModelConfig() {
  clearTimeout(_modelCfgTimer);
  _modelCfgTimer = setTimeout(() => {
    // 先读现有配置（保留其他后端已落盘的槽），只更新当前后端的槽——切后端配置互不覆盖（用户需求）
    fetch('/api/config').then(r => r.json()).then(d => {
      if (!d.success) return null;
      const c = d.config || {};
      const readProfile = (p) => ({
        base_url: document.getElementById(p + '-base-url').value,
        model: _modelValues[p] || document.getElementById(p + '-model').value,  // 内存值优先（防下拉框重建时读空）
        timeout: parseInt(document.getElementById(p + '-timeout').value) || 180,
        max_tokens: parseInt(document.getElementById(p + '-max-tokens').value) || 4096,
        temperature: parseFloat(document.getElementById(p + '-temperature').value) || 0.7
      });
      const merge = (p, cur) => {
        const backend = document.getElementById(p + '-backend').value;
        const profiles = Object.assign({}, (cur.profiles || {}));
        profiles[backend] = readProfile(p);
        return {backend, profiles};
      };
      return fetch('/api/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        planner_model: merge('planner', c.planner_model || {}),
        writer_model: merge('writer', c.writer_model || {})
      })});
    }).then(r => r ? r.json() : null).then(d => {
      if (d && d.success) console.log('[auto-save] 模型配置已持久化');
      else if (d) console.error('[auto-save] 保存失败', d);
    }).catch(e => console.error('[auto-save] 保存失败', e));
  }, 600);
}

// 后端切换引导：llama.cpp 已废弃（下拉仅 LM Studio / Ollama）
function onBackendChange(prefix) {
  const backend = document.getElementById(prefix + '-backend').value;
  const urlEl = document.getElementById(prefix + '-base-url');
  if (urlEl) {
    urlEl.placeholder = '';
    urlEl.title = '';
  }
  // 切后端 → 加载该后端落盘的配置槽（用户需求：切回来配置跟着回，各后端互不覆盖）
  fetch('/api/config').then(r => r.json()).then(d => {
    if (!d.success) return;
    const cfg = (d.config || {})[(prefix === 'planner' ? 'planner_model' : 'writer_model')] || {};
    const prof = (cfg.profiles || {})[backend];
    if (prof) {
      // 该后端有落盘槽 → 恢复
      if (document.getElementById(prefix + '-base-url')) document.getElementById(prefix + '-base-url').value = prof.base_url || '';
      if (document.getElementById(prefix + '-model')) document.getElementById(prefix + '-model').value = prof.model || '';
      _modelValues[prefix] = prof.model || '';  // 内存值同步（切后端恢复时先记内存，防 refreshModels 重建期间 autoSave 读空）
      if (document.getElementById(prefix + '-timeout')) document.getElementById(prefix + '-timeout').value = prof.timeout || 180;
      if (document.getElementById(prefix + '-max-tokens')) document.getElementById(prefix + '-max-tokens').value = prof.max_tokens || 4096;
      if (document.getElementById(prefix + '-temperature')) document.getElementById(prefix + '-temperature').value = prof.temperature != null ? prof.temperature : 0.7;
    } else {
      // 无该后端槽 → 填该后端默认地址（防止残留上一个后端的地址造成误配）
      const defUrl = backend === 'ollama' ? 'http://localhost:11434' : 'http://localhost:1234';
      if (document.getElementById(prefix + '-base-url')) document.getElementById(prefix + '-base-url').value = defUrl;
    }
    refreshModels(prefix);
    autoSaveModelConfig();  // 后端切换即持久化（恢复的槽值原样存回，无副作用）
  }).catch(() => {
    // 加载失败 → 填当前后端默认值（防残留上一个后端的值被保存污染槽）
    if (document.getElementById(prefix + '-base-url')) {
      document.getElementById(prefix + '-base-url').value = backend === 'ollama' ? 'http://localhost:11434' : 'http://localhost:1234';
    }
    refreshModels(prefix);
    autoSaveModelConfig();
  });
}

function loadConfig() {
  fetch('/api/config').then(r => r.json()).then(data => {
    if (!data.success) return;
    const c = data.config;
    const pm = c.planner_model || {};
    const wm = c.writer_model || {};
    // llama.cpp 已废弃：若旧配置仍指向 llama.cpp → 提示回退 LM Studio / Ollama
    ['planner', 'writer'].forEach(p => {
      const cur = (p === 'planner' ? pm : wm).backend;
      if (cur === 'llama.cpp') {
        const hint = document.getElementById(p + '-base-url');
        if (hint) hint.title = 'llama.cpp 后端已废弃，请改用 LM Studio / Ollama';
      }
    });
    // 按后端取配置槽（profiles 分槽结构——用户需求：切后端自动恢复对应落盘配置）
    // 槽缺失 → 用后端默认值（绝不用顶层残留——旧格式迁移后的顶层 base_url 不可靠）
    const pmBackend = pm.backend || 'lmstudio';
    const pmHasProf = !!(pm.profiles && Object.keys(pm.profiles).length);
    const pmProf = pmHasProf ? ((pm.profiles || {})[pmBackend] || {}) : pm;
    const pmDefUrl = pmBackend === 'ollama' ? 'http://localhost:11434' : 'http://localhost:1234';
    document.getElementById('planner-backend').value = pmBackend;
    document.getElementById('planner-base-url').value = pmProf.base_url || pmDefUrl;
    document.getElementById('planner-timeout').value = pmProf.timeout || 180;
    document.getElementById('planner-max-tokens').value = pmProf.max_tokens || 4096;
    document.getElementById('planner-temperature').value = pmProf.temperature != null ? pmProf.temperature : 0.6;
    // 缓存模板数据供只读控制
    window._lastTemplates = c.templates || {};
    window._lastBuiltins = c.builtin_templates || [];
    const wmBackend = wm.backend || 'lmstudio';
    const wmHasProf = !!(wm.profiles && Object.keys(wm.profiles).length);
    const wmProf = wmHasProf ? ((wm.profiles || {})[wmBackend] || {}) : wm;
    const wmDefUrl = wmBackend === 'ollama' ? 'http://localhost:11434' : 'http://localhost:1234';
    document.getElementById('writer-backend').value = wmBackend;
    document.getElementById('writer-base-url').value = wmProf.base_url || wmDefUrl;
    document.getElementById('writer-timeout').value = wmProf.timeout || 300;
    document.getElementById('writer-max-tokens').value = wmProf.max_tokens || 8192;
    document.getElementById('writer-temperature').value = wmProf.temperature != null ? wmProf.temperature : 0.7;
    // 加载模板
    const templates = c.templates || {};
    const selectedTemplate = c.selected_template || '日常写作';
    const sel = document.getElementById('template-select');
    const tmplNames = Object.keys(templates);
    // 字母排序，"自定义"永远最后
    tmplNames.sort((a, b) => a.localeCompare(b, 'zh-CN'));
    const ziDingYiIdx = tmplNames.indexOf('自定义');
    if (ziDingYiIdx >= 0) {
      tmplNames.splice(ziDingYiIdx, 1);
      tmplNames.push('自定义');
    }
    if (tmplNames.length) {
      sel.innerHTML = tmplNames.map(t => {
        const isBuiltin = c.builtin_templates && c.builtin_templates.includes(t);
        const label = isBuiltin ? `${t}` : `${t} ★`;
        return `<option value="${t}">${label}</option>`;
      }).join('');
    }
    sel.value = selectedTemplate;
    // 更新保存按钮状态
    updateSaveButtonState();
    // 加载 meta/content/style/logic
    const tmpl = templates[selectedTemplate] || {};
    if (typeof tmpl === 'object' && (tmpl.meta || tmpl.content)) {
      renderMetaRows(tmpl.meta || []);
      renderContentRows(tmpl.content || []);
      document.getElementById('template-style').value = tmpl.style || '';
      document.getElementById('template-logic').value = tmpl.logic || '';
    } else if (typeof tmpl === 'object' && tmpl.structure) {
      // structure 旧格式 → 转为 meta+content
      const m = [], c = [];
      (tmpl.structure || []).forEach(f => {
        const src = f.source || 'llm';
        if (src === 'user' || src === 'auto') {
          m.push({name: f.name, show_label: f.show_label, desc: f.desc, source: src});
        } else {
          c.push({name: f.name, show_label: f.show_label, desc: f.desc, type: f.type || 'section'});
        }
      });
      renderMetaRows(m);
      renderContentRows(c);
      document.getElementById('template-style').value = tmpl.style || '';
      document.getElementById('template-logic').value = '';
    } else if (typeof tmpl === 'string') {
      renderMetaRows([]);
      renderContentRows([]);
      document.getElementById('template-style').value = tmpl;
      document.getElementById('template-logic').value = '';
    } else {
      renderMetaRows([]);
      renderContentRows([]);
      document.getElementById('template-style').value = '';
      document.getElementById('template-logic').value = '';
    }
    // 确保选中值有效
    if (!sel.querySelector(`option[value="${selectedTemplate}"]`)) {
      sel.value = sel.options[0]?.value || '';
    }
    // 加载 RAG 路径
    if (c.rag_path) document.getElementById('rag-path').value = c.rag_path;
    if (c.context_review_length) document.getElementById('context-review-length').value = c.context_review_length;
    if (c.fact_check_enabled) document.getElementById('fact-check-enabled').checked = true;
    refreshModels('planner', pmProf.model);
    refreshModels('writer', wmProf.model);
    _modelValues.planner = pmProf.model || '';
    _modelValues.writer = wmProf.model || '';
    // 小说质检状态（模型就绪检测 + 开关）
    if (typeof checkNovelModels === 'function') checkNovelModels();
    // 加载完成后渲染对话区的 meta 输入框
    renderMetaInputs(selectedTemplate);
  });
}

function saveConfig() {
  const data = {
    planner_model: {
      backend: document.getElementById('planner-backend').value,
      base_url: document.getElementById('planner-base-url').value,
      model: document.getElementById('planner-model').value,
      timeout: parseInt(document.getElementById('planner-timeout').value) || 180,
      max_tokens: parseInt(document.getElementById('planner-max-tokens').value) || 4096,
      temperature: parseFloat(document.getElementById('planner-temperature').value) || 0.6
    },
    writer_model: {
      backend: document.getElementById('writer-backend').value,
      base_url: document.getElementById('writer-base-url').value,
      model: document.getElementById('writer-model').value,
      timeout: parseInt(document.getElementById('writer-timeout').value) || 300,
      max_tokens: parseInt(document.getElementById('writer-max-tokens').value) || 8192,
      temperature: parseFloat(document.getElementById('writer-temperature').value) || 0.7
    },
    selected_template: document.getElementById('template-select').value,
    context_review_length: parseInt(document.getElementById('context-review-length').value) || 8000,
    fact_check_enabled: document.getElementById('fact-check-enabled').checked,
    templates: {}  // 在下面通过 templData 更新
  };
    // 读取元数据表格
    const metaRows = document.querySelectorAll('#meta-rows tr');
    const meta = [];
    metaRows.forEach(tr => {
      const inputs = tr.querySelectorAll('input, select');
      if (inputs.length < 3) return;
      const name = inputs[0].value.trim();
      if (!name) return;
      const descSpan = tr.querySelector('.desc-preview');
      meta.push({name, show_label: inputs[1].checked, desc: descSpan ? (descSpan.dataset.fullDesc || '') : '', source: inputs[2].value});
    });
    // 读取内容树表格
    const contentRows = document.querySelectorAll('#content-rows tr');
    const content = [];
    contentRows.forEach(tr => {
      const inputs = tr.querySelectorAll('input, select');
      if (inputs.length < 4) return;
      const name = inputs[0].value.trim();
      if (!name) return;
      const descSpan = tr.querySelector('.desc-preview');
      content.push({name, show_label: inputs[1].checked, desc: descSpan ? (descSpan.dataset.fullDesc || '') : '', type: inputs[2].value, logical_order: inputs[3].value !== '' ? parseInt(inputs[3].value) : null});
    });
    const style = document.getElementById('template-style').value;
    const logic = document.getElementById('template-logic').value;
    const selTmpl = document.getElementById('template-select').value;
    const tmplObj = {};
    tmplObj[selTmpl] = {meta, content, style, logic};
    // 保留其他模板
      fetch('/api/config').then(r => r.json()).then(cfg => {
        if (!cfg.success) return;
        const existing = cfg.config.templates || {};
        // 合并：保留未选中的模板，更新当前选中的
        Object.keys(existing).forEach(k => {
          if (k !== selTmpl) {
            const v = existing[k];
            if (typeof v === 'object' && (v.meta || v.content)) {
              tmplObj[k] = v;  // 新格式 meta+content
            } else if (typeof v === 'object' && v.structure) {
              // structure 旧格式 → 转为 meta+content
              const m = [], c = [];
              (v.structure || []).forEach(f => {
                const src = f.source || 'llm';
                if (src === 'user' || src === 'auto') {
                  m.push({name: f.name, show_label: f.show_label, desc: f.desc, source: src});
                } else {
                  c.push({name: f.name, show_label: f.show_label, desc: f.desc, type: f.type || 'section'});
                }
              });
              tmplObj[k] = {meta: m, content: c, style: v.style || '', logic: ''};
            }
          }
        });
        data.templates = tmplObj;
      data.default_prompt = style || (tmplObj[selTmpl] && tmplObj[selTmpl].style) || '';

    fetch('/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) })
      .then(r => r.json()).then(d => {
        document.getElementById('config-status').textContent = d.success ? '✓ 已保存' : '✗ 保存失败';
        document.getElementById('config-status').className = 'status-msg ' + (d.success ? 'success' : 'error');
      });
  });
}

// ===== 内置模板只读控制 =====
function updateSaveButtonState() {
  const sel = document.getElementById('template-select');
  const name = sel ? sel.value : '';
  const templates = window._lastTemplates || {};
  const builtins = window._lastBuiltins || [];
  const isBuiltin = builtins.includes(name);
  const btn = document.getElementById('save-current-template-btn');
  const delBtn = document.querySelector('.btn-danger');
  if (btn) {
    btn.disabled = isBuiltin;
    btn.title = isBuiltin ? '内置模板只读，修改请使用"另存为"' : '直接保存当前模板的修改';
    btn.style.opacity = isBuiltin ? '0.5' : '1';
    btn.style.cursor = isBuiltin ? 'not-allowed' : 'pointer';
  }
  if (delBtn) {
    delBtn.disabled = isBuiltin;
    delBtn.title = isBuiltin ? '内置模板只读，不可删除' : '删除当前自定义模板';
    delBtn.style.opacity = isBuiltin ? '0.5' : '1';
    delBtn.style.cursor = isBuiltin ? 'not-allowed' : 'pointer';
  }
  const badge = document.getElementById('template-type-badge');
  if (badge) {
    if (isBuiltin) {
      badge.textContent = '[内置]';
      badge.style.background = '#5dade2';
      badge.style.color = '#fff';
    } else {
      badge.textContent = '[用户]';
      badge.style.background = '#f39c12';
      badge.style.color = '#fff';
    }
  }
}

// ===== 模板切换 =====
function onTemplateChange() {
  const sel = document.getElementById('template-select');
  const tmplName = sel.value;
  updateSaveButtonState();
  fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({selected_template: tmplName})
  }).catch(() => {});
  fetch('/api/config').then(r => r.json()).then(d => {
    if (!d.success) return;
    const templates = d.config.templates || {};
    const tmpl = templates[tmplName] || {};
    if (typeof tmpl === 'object' && (tmpl.meta || tmpl.content)) {
      renderMetaRows(tmpl.meta || []);
      renderContentRows(tmpl.content || []);
      document.getElementById('template-style').value = tmpl.style || '';
      document.getElementById('template-logic').value = tmpl.logic || '';
    } else if (typeof tmpl === 'object' && tmpl.structure) {
      const m = [], c = [];
      (tmpl.structure || []).forEach(f => {
        const src = f.source || 'llm';
        if (src === 'user' || src === 'auto') { m.push({name: f.name, show_label: f.show_label, desc: f.desc, source: src}); }
        else { c.push({name: f.name, show_label: f.show_label, desc: f.desc, type: f.type || 'section'}); }
      });
      renderMetaRows(m); renderContentRows(c);
      document.getElementById('template-style').value = tmpl.style || '';
      document.getElementById('template-logic').value = '';
    } else if (typeof tmpl === 'string') {
      renderMetaRows([]); renderContentRows([]);
      document.getElementById('template-style').value = tmpl;
      document.getElementById('template-logic').value = '';
    } else {
      renderMetaRows([]); renderContentRows([]);
      document.getElementById('template-style').value = '';
      document.getElementById('template-logic').value = '';
    }
  });
}

// ===== 结构表格编辑器（元数据 + 内容树） =====

function renderMetaRows(meta) {
  const tbody = document.getElementById('meta-rows');
  tbody.innerHTML = '';
  (meta || []).forEach(f => addMetaRow(f));
  if (!meta || !meta.length) {
    addMetaRow({name:'标题',show_label:false,desc:'文章标题',source:'auto'});
  }
}

function renderContentRows(content) {
  const tbody = document.getElementById('content-rows');
  tbody.innerHTML = '';
  (content || []).forEach(f => addContentRow(f));
  if (!content || !content.length) {
    addContentRow({name:'正文',show_label:false,desc:'文章主体内容',type:'section'});
  }
}

// 当前选中模板是否为小说线（novel.mode）
function isNovelTemplateSelected() {
  const sel = document.getElementById('template-select');
  const cur = (window._lastTemplates || {})[sel ? sel.value : ''] || {};
  return !!(cur.novel && cur.novel.mode);
}

function addMetaRow(field) {
  field = field || {name:'',show_label:true,desc:'',source:'auto'};
  // 小说驱动字段锁定：题材/篇幅是规划输入，禁止删除（防删字段退化）
  const lockedDel = isNovelTemplateSelected() && (field.name === '题材' || field.name === '篇幅');
  const tr = document.createElement('tr');
  tr.style.borderBottom = '1px solid var(--border)';
  tr.innerHTML = [
    '<td style="padding:3px 6px"><input type="text" value="' + escHtml(field.name) + '" style="width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:3px 4px;color:var(--text);font-size:12px" placeholder="字段名"></td>',
    '<td style="padding:3px 6px;text-align:center"><input type="checkbox" ' + (field.show_label ? 'checked' : '') + ' style="accent-color:var(--accent)"></td>',
    '<td style="padding:3px 6px"><span class="desc-preview" onclick="openDescModal(this)" data-full-desc="' + escHtml(field.desc) + '" style="display:block;padding:3px 4px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;cursor:pointer;color:var(--text-dim);font-size:12px;min-height:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px" title="' + escHtml(field.desc) + '">' + escHtml(field.desc ? field.desc.substring(0,9)+(field.desc.length>9?'...':'') : '点击输入...') + '</span></td>',
    '<td style="padding:3px 6px"><select style="width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:3px 4px;color:var(--text);font-size:12px"><option value="user" ' + (field.source==='user'?'selected':'') + '>用户</option><option value="llm" ' + (field.source==='llm'?'selected':'') + '>LLM</option><option value="auto" ' + (field.source==='auto'?'selected':'') + '>自动</option></select></td>',
    '<td style="padding:3px 6px;text-align:center">' + (lockedDel
      ? '<span title="小说驱动字段，不可删除" style="color:var(--text-dim);font-size:15px;cursor:not-allowed">&times;</span>'
      : '<button onclick="this.closest(\'tr\').remove()" title="删除此行" style="background:none;border:none;color:#e74c3c;cursor:pointer;font-size:15px;line-height:1">&times;</button>') + '</td>'
  ].join('');
  document.getElementById('meta-rows').appendChild(tr);
}

function addContentRow(field) {
  field = field || {name:'',show_label:true,desc:'',type:'section',logical_order:0};
  const lo = field.logical_order !== undefined ? field.logical_order : 0;
  const citeCheck = field.citation_check !== undefined ? field.citation_check : false;
  // 从映射中拆出两个格式
  const fmtStr = field.citation_format || '[x]=1.';
  const eqPos = fmtStr.indexOf('=');
  const inlineFmt = eqPos >= 0 ? fmtStr.substring(0, eqPos).trim() : fmtStr.trim();
  const refFmt = eqPos >= 0 ? fmtStr.substring(eqPos + 1).trim() : '';
  // 小说结构节点锁定：kind=setting/chapters 是小说线声明，禁止删除
  const lockedDel = isNovelTemplateSelected() && !!field.kind;
  const tr = document.createElement('tr');
  tr.style.borderBottom = '1px solid var(--border)';
  if (field.kind) tr.dataset.kind = field.kind;  // 小说节点分类（setting/chapters），行内隐藏保留
  tr.innerHTML = [
    '<td style="padding:3px 6px"><input type="text" value="' + escHtml(field.name) + '" style="width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:3px 4px;color:var(--text);font-size:12px" placeholder="字段名"></td>',
    '<td style="padding:3px 6px;text-align:center"><input type="checkbox" ' + (field.show_label ? 'checked' : '') + ' style="accent-color:var(--accent)"></td>',
    '<td style="padding:3px 6px"><span class="desc-preview" onclick="openDescModal(this)" data-full-desc="' + escHtml(field.desc) + '" style="display:block;padding:3px 4px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;cursor:pointer;color:var(--text-dim);font-size:12px;min-height:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px" title="' + escHtml(field.desc) + '">' + escHtml(field.desc ? field.desc.substring(0,9)+(field.desc.length>9?'...':'') : '点击输入...') + '</span></td>',
    '<td style="padding:3px 6px"><select style="width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:3px 4px;color:var(--text);font-size:12px"><option value="leaf" ' + (field.type==='leaf'?'selected':'') + '>无</option><option value="section" ' + (field.type==='section'?'selected':'') + '>有</option></select></td>',
    '<td style="padding:3px 6px"><select style="width:100%;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:3px 4px;color:var(--text);font-size:12px"><option value="" ' + (!lo && lo!==0?'selected':'') + '>自动</option><option value="0" ' + (lo===0?'selected':'') + '>先写</option><option value="1" ' + (lo===1?'selected':'') + '>其次</option><option value="2" ' + (lo===2?'selected':'') + '>最后</option></select></td>',
    '<td style="padding:3px 6px;text-align:center;white-space:nowrap"><input type="checkbox" class="cite-cb" ' + (citeCheck ? 'checked' : '') + ' style="accent-color:var(--accent);width:14px;height:14px;vertical-align:middle"> <input type="text" class="cite-inline" value="' + escHtml(inlineFmt) + '" style="width:38px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:2px 2px;color:var(--text);font-size:10px;text-align:center;vertical-align:middle" placeholder="[x]" title="正文引用格式"> <span style="font-size:11px;color:var(--text-dim);vertical-align:middle">=</span> <input type="text" class="cite-ref" value="' + escHtml(refFmt) + '" style="width:38px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:2px 2px;color:var(--text);font-size:10px;text-align:center;vertical-align:middle" placeholder="1." title="参考文献条目格式"></td>',
    '<td style="padding:3px 6px;text-align:center">' + (lockedDel
      ? '<span title="小说结构节点，不可删除" style="color:var(--text-dim);font-size:15px;cursor:not-allowed">&times;</span>'
      : '<button onclick="this.closest(\'tr\').remove()" title="删除此行" style="background:none;border:none;color:#e74c3c;cursor:pointer;font-size:15px;line-height:1">&times;</button>') + '</td>'
  ].join('');
  document.getElementById('content-rows').appendChild(tr);
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ===== 收集模板数据 =====
function collectTemplateData() {
  const meta = [];
  document.querySelectorAll('#meta-rows tr').forEach(tr => {
    const inputs = tr.querySelectorAll('input, select');
    if (inputs.length < 3) return;
    const n = inputs[0].value.trim();
    if (!n) return;
    const descSpan = tr.querySelector('.desc-preview');
    meta.push({name:n, show_label:inputs[1].checked, desc:descSpan ? (descSpan.dataset.fullDesc || '') : '', source:inputs[2].value});
  });
  const content = [];
  document.querySelectorAll('#content-rows tr').forEach(tr => {
    const inputs = tr.querySelectorAll('input, select');
    if (inputs.length < 4) return;
    const n = inputs[0].value.trim();
    if (!n) return;
    const descSpan = tr.querySelector('.desc-preview');
    const citeCb = tr.querySelector('.cite-cb');
    const citeInline = tr.querySelector('.cite-inline');
    const citeRef = tr.querySelector('.cite-ref');
    content.push({
      name:n, show_label:inputs[1].checked,
      desc:descSpan ? (descSpan.dataset.fullDesc || '') : '',
      type:inputs[2].value,
      logical_order: inputs[3].value !== '' ? parseInt(inputs[3].value) : null,
      citation_check: citeCb ? citeCb.checked : false,
      citation_format: (citeInline ? citeInline.value.trim() || '[x]' : '[x]') + '=' + (citeRef ? citeRef.value.trim() || '1.' : '1.'),
      ...(tr.dataset.kind ? {kind: tr.dataset.kind} : {})
    });
  });
  const style = document.getElementById('template-style').value;
  const logic = document.getElementById('template-logic').value;
  // 小说线标记：保留当前模板的 novel.mode（另存为/保存副本不丢小说线）
  const selTmpl = document.getElementById('template-select').value;
  const curTpl = (window._lastTemplates || {})[selTmpl] || {};
  const novel = (curTpl.novel && typeof curTpl.novel === 'object') ? curTpl.novel : undefined;
  return novel ? {meta, content, style, logic, novel} : {meta, content, style, logic};
}

// ===== 小说模板驱动字段保护 =====
// 小说线特化模板：题材/篇幅是驱动字段（题材→场景配置、篇幅→字数目标），
// 删除会导致规划退化。保存/另存为时代码级拦截。
function validateNovelTemplate(data) {
  if (!(data.novel && data.novel.mode)) return '';
  const metaNames = (data.meta || []).map(m => m.name);
  const missing = [];
  if (!metaNames.includes('题材')) missing.push('题材');
  if (!metaNames.includes('篇幅')) missing.push('篇幅');
  if (!missing.length) return '';
  return '小说模板缺少驱动字段：' + missing.join('、') + '（题材→场景配置、篇幅→字数目标，删除会导致小说规划退化，禁止保存）';
}

// ===== 保存到当前模板 =====
function saveCurrentTemplate() {
  const name = document.getElementById('template-select').value;
  if (!name) { alert('未选择模板'); return; }
  const builtins = window._lastBuiltins || [];
  if (builtins.includes(name)) { alert('内置模板只读，请使用"另存为"创建副本修改'); return; }
  const data = collectTemplateData();
  const novelErr = validateNovelTemplate(data);
  if (novelErr) { alert(novelErr); return; }
  const btn = document.getElementById('save-current-template-btn');
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.textContent = '保存中...'; btn.disabled = true; }
  fetch('/api/config').then(r => r.json()).then(cfg => {
    if (!cfg.success) { if (btn) { btn.textContent = origText; btn.disabled = false; } return; }
    const templates = Object.assign({}, cfg.config.templates || {});
    templates[name] = data;
    fetch('/api/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({templates})
    }).then(r => r.json()).then(d => {
      if (btn) {
        if (d.success) {
          btn.textContent = '已保存 ✓';
          setTimeout(() => { btn.textContent = origText; btn.disabled = false; }, 1200);
        } else {
          btn.textContent = '保存失败';
          setTimeout(() => { btn.textContent = origText; btn.disabled = false; }, 1500);
        }
      }
      if (d.success) loadConfig();
    }).catch(() => {
      if (btn) { btn.textContent = '保存失败'; setTimeout(() => { btn.textContent = origText; btn.disabled = false; }, 1500); }
    });
  });
}

function saveTemplateAs() {
  document.getElementById('saveas-name').value = '';
  document.getElementById('saveas-modal-status').textContent = '';
  document.getElementById('saveas-modal').classList.add('show');
  setTimeout(() => document.getElementById('saveas-name').focus(), 100);
}

function closeSaveAsModal() {
  document.getElementById('saveas-modal').classList.remove('show');
}

function confirmSaveAs() {
  const name = document.getElementById('saveas-name').value.trim();
  if (!name) { document.getElementById('saveas-modal-status').textContent = '名称不能为空'; return; }
  closeSaveAsModal();
  const data = collectTemplateData();
  const novelErr = validateNovelTemplate(data);
  if (novelErr) {
    document.getElementById('saveas-modal').classList.add('show');
    document.getElementById('saveas-modal-status').textContent = novelErr;
    return;
  }
  const builtins = window._lastBuiltins || [];
  if (builtins.includes(name)) {
    document.getElementById('saveas-modal-status').textContent = '名称与内置模板重复，请换一个';
    return;
  }
  fetch('/api/config').then(r => r.json()).then(cfg => {
    if (!cfg.success) return;
    const templates = Object.assign({}, cfg.config.templates || {});
    templates[name] = data;
    fetch('/api/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({templates})
    }).then(r => r.json()).then(d => {
      if (d.success) { loadConfig(); document.getElementById('template-select').value = name; onTemplateChange(); }
    });
  });
}

// ===== 删除自定义模板 =====
const _delTplPending = {pending: false, name: '', timer: null};

function deleteTemplate() {
  if (_delTplPending.pending) {
    const name = _delTplPending.name;
    clearTimeout(_delTplPending.timer);
    _delTplPending.pending = false;
    const btn = document.querySelector('.btn-danger');
    if (btn) { btn.textContent = '删除'; btn.style.background = '#c0392b'; }
    const builtins = window._lastBuiltins || [];
    if (builtins.includes(name)) return;
    const templates = {};
    templates[name] = null;
    // 发送 null 标记删除用户模板
    fetch('/api/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({_delete_template: name})
    }).then(r => r.json()).then(d => { if (d.success) loadConfig(); });
    return;
  }
  const sel = document.getElementById('template-select');
  const name = sel.value;
  const builtins = window._lastBuiltins || [];
  if (builtins.includes(name)) return;
  const btn = document.querySelector('.btn-danger');
  if (btn) { btn.textContent = '确认?'; btn.style.background = '#e74c3c'; btn.style.color = '#fff'; }
  _delTplPending.pending = true;
  _delTplPending.name = name;
  _delTplPending.timer = setTimeout(() => {
    _delTplPending.pending = false;
      if (btn) { btn.textContent = '删除'; btn.style.background = '#c0392b'; btn.style.color = '#fff'; }
    }, 2500);
}

// ===== LLM 模板生成 =====

function openGenTemplate() {
  document.getElementById('gen-template-modal').classList.add('show');
  document.getElementById('gen-template-desc').value = '';
  document.getElementById('gen-template-status').textContent = '';
}

function closeGenTemplate() {
  document.getElementById('gen-template-modal').classList.remove('show');
}

function generateTemplate() {
  const nameInput = document.getElementById('gen-template-name').value.trim();
  const desc = document.getElementById('gen-template-desc').value.trim();
  if (!desc) { document.getElementById('gen-template-status').textContent = '请输入描述'; return; }
  const status = document.getElementById('gen-template-status');
  status.textContent = '⏳ 生成中...';
  fetch('/api/gen-template', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({description: desc, name: nameInput || undefined})
  }).then(r => r.json()).then(d => {
    if (d.success && d.template && (d.template.meta || d.template.content)) {
      const templateName = d.template.name || nameInput || '自定义模板';
      fetch('/api/config').then(r => r.json()).then(cfg => {
        if (!cfg.success) return;
        const templates = Object.assign({}, cfg.config.templates || {});
        templates[templateName] = {meta: d.template.meta || [], content: d.template.content || [], style: d.template.style || '', logic: d.template.logic || ''};
        fetch('/api/config', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({templates})
        }).then(r2 => r2.json()).then(d2 => {
          if (d2.success) {
            loadConfig();
            document.getElementById('template-select').value = templateName;
            onTemplateChange();
            status.textContent = `✓ 已创建模板「${templateName}」`;
          } else {
            status.textContent = '✗ 保存失败';
          }
        });
        closeGenTemplate();
      });
    } else {
      status.textContent = '✗ ' + (d.error || '生成失败');
    }
  }).catch(e => {
    status.textContent = '✗ 请求失败';
  });
}

// ===== 字段要求模态框 =====
let _descModalTarget = null;

function openDescModal(span) {
  _descModalTarget = span;
  const fullDesc = span.dataset.fullDesc || (span.textContent === '点击输入...' ? '' : span.textContent);
  document.getElementById('desc-editor').value = fullDesc;
  document.getElementById('desc-modal-status').textContent = '';
  document.getElementById('desc-modal').classList.add('show');
}

function closeDescModal() {
  document.getElementById('desc-modal').classList.remove('show');
  _descModalTarget = null;
}

function saveDescModal() {
  if (!_descModalTarget) return;
  const value = document.getElementById('desc-editor').value.trim();
  const display = value ? value.substring(0, 12) + (value.length > 12 ? '...' : '') : '点击输入...';
  _descModalTarget.textContent = display;
  _descModalTarget.dataset.fullDesc = value;
  _descModalTarget.title = value || '点击编辑字段要求';
  _descModalTarget.dataset.fullDesc = value;
  closeDescModal();
}

// ===== 小说质检 =====
let novelChecksConfig = {chapter:true, format:true, reason:true, full_fidelity:true, full_pledge:true, full_ending:true, auto_repair:false, repair_rounds:3};

async function checkNovelModels() {
  const statusEl = document.getElementById('novel-model-status');
  if (!statusEl) return;
  statusEl.textContent = '检测中...';
  try {
    const r = await fetch('/api/novel/status');
    const d = await r.json();
    const parts = [];
    if (d.judge_backend === 'lmstudio') {
      const gg = d.gguf || {};
      parts.push('<span style="color:#2ecc71">判定后端: LM Studio（统一）</span>');
      parts.push(gg['4dim_ready'] ? '<span style="color:#2ecc71">8B 就绪</span>' : '<span style="color:#e94560">8B 缺失</span>');
      parts.push(gg['r1_ready'] ? '<span style="color:#2ecc71">7B 就绪</span>' : '<span style="color:#e94560">7B 缺失</span>');
      parts.push('<span style="color:var(--text-dim)">提取: 8B（统一管理下无需 3B）</span>');
    } else {
      if (d.r1) parts.push('<span style="color:#2ecc71">推理R1 就绪</span>');
      else parts.push('<span style="color:#e94560">推理R1 缺失</span>');
      if (d.qwen25) parts.push('<span style="color:#2ecc71">实体抽取Qwen2.5-3B 就绪</span>');
      else parts.push('<span style="color:#e94560">实体抽取Qwen2.5-3B 缺失</span>');
      parts.push('<span style="color:var(--text-dim)">判定后端: transformers</span>');
    }
    // LM Studio 环境提示（已装？引擎在跑？）
    const lm = d.lmstudio || {};
    if (lm.available && lm.server_ok) parts.push('<span style="color:var(--text-dim)">LM Studio 就绪</span>');
    else if (lm.available) parts.push('<span style="color:#e67e22">LM Studio 已装但引擎未响应（lms server start）</span>');
    else parts.push('<span style="color:#e94560">LM Studio 未安装</span>');
    statusEl.innerHTML = parts.join(' | ');
    const dirEl = document.getElementById('novel-model-dir');
    if (dirEl && d.dir) dirEl.value = d.dir;
    // 判定后端徽标
    const jbEl = document.getElementById('novel-judge-backend');
    if (jbEl) {
      jbEl.textContent = lm.available
        ? `（LM Studio：${lm.reason || ''}；勾选统一 → 8B+7B 走 GPU，不勾 → 3B+1.5B）`
        : (lm.reason ? `（${lm.reason} → 固定 3B+1.5B）` : '');
      jbEl.style.color = lm.available ? 'var(--text-dim)' : '#e67e22';
    }
    // 统一管理勾选：LM Studio 存在 且 写作/规划后端都是 LM Studio 时可勾（ollama 未接入判定联动 → 禁用）
    const unifiedEl = document.getElementById('novel-chk-unified');
    if (unifiedEl) {
      const mb = d.model_backends || {};
      const allLms = (mb.planner === 'lmstudio') && (mb.writer === 'lmstudio');
      unifiedEl.disabled = !(lm.available && allLms);
      unifiedEl.checked = !!(d.config && d.config.unified_management);
      unifiedEl.title = (!lm.available)
        ? 'LM Studio 未安装，统一管理不可用'
        : (!allLms ? '写作/规划后端为 Ollama，统一管理（LM Studio 判定）不适用' : unifiedEl.title);
    }
    const ggufRow = document.getElementById('novel-gguf-row');
    const ggufSt = document.getElementById('novel-gguf-status');
    if (ggufRow && ggufSt) {
      const unifiedOn = unifiedEl && unifiedEl.checked;
      if (unifiedOn) {
        const gg = d.gguf || {};
        ggufRow.style.display = '';
        ggufSt.innerHTML = `目录: ${gg.dir || ''}<br>` +
          (gg['4dim_ready'] ? `✅ 4维 8B: ${gg['4dim']}` : `❌ 4维 8B 缺失（Qwen3-8B Q4_K_M 约5GB，点「安装缺失模型」下载）`) + '<br>' +
          (gg['r1_ready'] ? `✅ R1 7B: ${gg['r1']}` : `❌ R1 7B 缺失（R1-Distill-Qwen-7B Q4 约4.4GB，点「安装缺失模型」下载）`);
      } else {
        ggufRow.style.display = 'none';
      }
    }
    if (d.config) {
      novelChecksConfig = d.config;
      const map = {chapter:'novel-chk-chapter', format:'novel-chk-format', reason:'novel-chk-reason', full_fidelity:'novel-chk-fid', full_pledge:'novel-chk-pledge', full_ending:'novel-chk-ending', auto_repair:'novel-chk-autorepair'};
      Object.keys(map).forEach(k => {
        const el = document.getElementById(map[k]);
        // 旧配置只有 full → 三检同开关
        const v = d.config[k] !== undefined ? d.config[k] : d.config.full;
        if (el && v !== undefined) el.checked = !!v;
      });
      // 独占串行：默认开（True）；旧配置无此字段 → 按默认 True 勾选
      const serialEl = document.getElementById('novel-chk-serial');
      if (serialEl) serialEl.checked = d.config.exclusive_serial !== undefined ? !!d.config.exclusive_serial : true;
      // 独占串行依赖统一管理：统一管理不勾 → 串行禁用（3B/1.5B 无 GPU 模型可串行）
      if (serialEl && unifiedEl) {
        serialEl.disabled = !unifiedEl.checked;
        if (!unifiedEl.checked) serialEl.checked = false;
      }
      // 关闭独占串行 → 显示 max concurrency 提醒（仅此时提醒，串行模式无并发）
      const serialWarn = document.getElementById('novel-serial-warn');
      if (serialWarn && serialEl) serialWarn.style.display = serialEl.checked ? 'none' : '';
      const roundsEl = document.getElementById('novel-chk-rounds');
      if (roundsEl && d.config.repair_rounds) roundsEl.value = d.config.repair_rounds;
    }
  } catch(e) { statusEl.textContent = '检测失败: ' + e; }
}

async function installNovelModels() {
  const btn = document.getElementById('novel-install-btn');
  const statusEl = document.getElementById('novel-model-status');
  if (!btn || !statusEl) return;
  btn.disabled = true;
  try {
    const r = await fetch('/api/novel/install', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    const d = await r.json();
    statusEl.innerHTML = (d.message || '完成') + (d.started ? '' : '');
    if (d.started) {
      // 轮询安装状态（内联显示，不弹窗）
      const timer = setInterval(async () => {
        try {
          const rr = await fetch('/api/novel/status');
          const dd = await rr.json();
          const ins = dd.install;
          if (ins && ins.running) {
            const last = (ins.log && ins.log.length) ? ins.log[ins.log.length-1] : '';
            statusEl.textContent = '安装中... ' + (last.length > 70 ? last.slice(0,70) + '…' : last);
          } else {
            clearInterval(timer);
            btn.disabled = false;
            checkNovelModels();
          }
        } catch(e) { clearInterval(timer); btn.disabled = false; checkNovelModels(); }
      }, 3000);
      return;
    }
  } catch(e) { statusEl.textContent = '安装失败: ' + e; }
  btn.disabled = false;
  checkNovelModels();
}

async function saveNovelChecks() {
  const roundsEl = document.getElementById('novel-chk-rounds');
  const unifiedEl = document.getElementById('novel-chk-unified');
  const serialEl = document.getElementById('novel-chk-serial');
  // 独占串行依赖统一管理：统一管理不勾 → 串行禁用且不生效（3B/1.5B 无 GPU 模型可串行）
  const unifiedOn = unifiedEl ? !!unifiedEl.checked : false;
  if (serialEl) {
    serialEl.disabled = !unifiedOn;
    if (!unifiedOn) serialEl.checked = false;
  }
  const cfg = {
    chapter: !!document.getElementById('novel-chk-chapter').checked,
    format: !!document.getElementById('novel-chk-format').checked,
    reason: !!document.getElementById('novel-chk-reason').checked,
    full_fidelity: !!document.getElementById('novel-chk-fid').checked,
    full_pledge: !!document.getElementById('novel-chk-pledge').checked,
    full_ending: !!document.getElementById('novel-chk-ending').checked,
    auto_repair: !!document.getElementById('novel-chk-autorepair').checked,
    unified_management: unifiedOn,
    exclusive_serial: serialEl ? (unifiedOn && serialEl.checked) : false,
    repair_rounds: roundsEl ? (parseInt(roundsEl.value) || 3) : 3
  };
  novelChecksConfig = cfg;
  // 关闭独占串行 → 显示 max concurrency 提醒（仅此时；串行模式一次一模型无并发）
  const serialWarn = document.getElementById('novel-serial-warn');
  if (serialWarn) serialWarn.style.display = cfg.exclusive_serial ? 'none' : '';
  try {
    await fetch('/api/novel/checks', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cfg)});
  } catch(e) { console.error('保存小说质检开关失败', e); }
  // 勾选统一管理后立即刷新 GGUF 状态行
  checkNovelModels();
}

// ===== RAG 状态管理 =====

function onRagToggle(cb, sectionId) {
  // 显示/隐藏 KB 下拉框
  const card = cb.closest('.section-card');
  const kbSelect = card?.querySelector('.sc-kb');
  if (kbSelect) kbSelect.style.display = cb.checked ? '' : 'none';
  collectOutlineData();
}

function checkRagStatus() {
  // 加 cache-buster 防止浏览器缓存 GET 响应
  fetch('/api/rag/status?_=' + Date.now()).then(r => r.json()).then(d => {
    const indicator = document.getElementById('rag-status-indicator');
    const btn = document.getElementById('rag-start-btn');
    const stopBtn = document.getElementById('rag-stop-btn');
    const kbRow = document.getElementById('rag-kb-row');
    const kbList = document.getElementById('rag-kb-list');

    if (d.online) {
      // 用户已点击停止：不再显示"运行中"，直到手动重新启动
      if (window._ragManuallyStopped) {
        indicator.innerHTML = '<span style="color:#e94560;font-weight:600">RAG 离线（端口被占用）</span>';
        syncRagOutlineState();
        return;
      }
      ragOnline = true;
      ragKbs = Array.isArray(d.kbs) ? d.kbs : [];
      indicator.innerHTML = '<span style="color:#00b894;font-weight:600">RAG 运行中 (port 8767)</span>';
      btn.disabled = true;
      btn.textContent = 'RAG 已运行';
      stopBtn.disabled = false;
      stopBtn.textContent = '停止 RAG';
      kbRow.style.display = '';
      kbList.textContent = ragKbs.length ? ragKbs.join('、') : '(无知识库)';
      // 清除"等待就绪"等旧状态文本
      const cs = document.getElementById('config-status');
      if (cs) { cs.textContent = ''; cs.className = ''; }
      // 如果之前是在轮询中检测到上线，停止轮询
      if (window._ragPollTimer) {
        clearInterval(window._ragPollTimer);
        window._ragPollTimer = null;
      }
    } else if (d.starting) {
      window._ragManuallyStopped = false;
      window._ragStoppedAt = 0;
      ragOnline = false;
      indicator.innerHTML = '<span style="color:#f39c12;font-weight:600">RAG 启动中...</span>';
      btn.disabled = true;
      btn.textContent = '启动中...';
      stopBtn.disabled = true;
      stopBtn.textContent = '停止 RAG';
      kbRow.style.display = 'none';
      const cs = document.getElementById('config-status');
      if (cs && cs.textContent === '已提交启动请求，等待就绪...') { cs.textContent = '等待 RAG 上线...'; }
    } else if (d.stderr) {
      // 子进程挂了，显示错误
      window._ragManuallyStopped = false;
      window._ragStoppedAt = 0;
      ragOnline = false;
      indicator.innerHTML = '<span style="color:#e94560;font-weight:600">RAG 启动失败</span>';
      btn.disabled = false;
      btn.textContent = '冷启动 RAG';
      kbRow.style.display = 'none';
      document.getElementById('config-status').textContent = '子进程错误: ' + d.stderr.substring(0, 1000);
      document.getElementById('config-status').className = 'status-msg error';
    } else {
      ragOnline = false;
      ragKbs = [];
      indicator.innerHTML = '<span style="color:#e94560;font-weight:600">RAG 离线</span>';
      window._ragManuallyStopped = false;
      window._ragStoppedAt = 0;
      btn.disabled = false;
      btn.textContent = '冷启动 RAG';
      stopBtn.disabled = true;
      stopBtn.textContent = '停止 RAG';
      kbRow.style.display = 'none';
      const cs = document.getElementById('config-status');
      if (cs) { cs.textContent = ''; cs.className = ''; }
    }
    // 同步已渲染大纲卡片上的 RAG 控件状态
    syncRagOutlineState();
  }).catch(() => {
    document.getElementById('rag-status-indicator').innerHTML = '<span style="color:#e94560">RAG 检测失败</span>';
  });
}

function syncRagOutlineState() {
  document.querySelectorAll('.sc-rag-cb').forEach(cb => {
    cb.disabled = !ragOnline;
    cb.title = ragOnline ? '' : 'RAG未连接';
    if (!ragOnline) cb.checked = false;
    // 同步 KB 下拉框
    const card = cb.closest('.section-card');
    if (card) {
      let kbSelect = card.querySelector('.sc-kb');
      if (ragOnline) {
        if (!kbSelect && ragKbs.length) {
          const newKb = document.createElement('select');
          newKb.className = 'sc-kb';
          newKb.style.cssText = 'display:none;width:120px;font-size:12px';
          newKb.onchange = () => collectOutlineData();
          const kbLabel = card.querySelector('.sc-rag');
          if (kbLabel) {
            const opts = '<option value="">自动KB</option>' + ragKbs.map(k => `<option value="${k}">${k}</option>`).join('');
            newKb.innerHTML = opts;
            kbLabel.after(newKb);
          }
        }
        if (kbSelect) kbSelect.style.display = cb.checked ? '' : 'none';
      } else {
        if (kbSelect) kbSelect.style.display = 'none';
      }
    }
  });
}

function saveRagPath() {
  const path = document.getElementById('rag-path').value.trim();
  fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({rag_path: path})
  }).then(r => r.json()).then(d => {
    document.getElementById('config-status').textContent = d.success ? '✓ RAG 路径已保存' : '✗ 保存失败';
    document.getElementById('config-status').className = 'status-msg ' + (d.success ? 'success' : 'error');
  });
}

function startRag() {
  window._ragManuallyStopped = false;  // 允许再次显示"RAG 运行中"
  window._ragStoppedAt = 0;
  const path = document.getElementById('rag-path').value.trim();
  if (!path) {
    document.getElementById('config-status').textContent = '请先填写 RAG 路径';
    document.getElementById('config-status').className = 'status-msg error';
    return;
  }
  const btn = document.getElementById('rag-start-btn');
  btn.disabled = true;
  btn.textContent = '启动中...';
  document.getElementById('rag-status-indicator').innerHTML = '<span style="color:#f39c12;font-weight:600">RAG 启动中...</span>';

  fetch('/api/rag/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({path: path})
  }).then(r => r.json()).then(d => {
    if (d.success) {
      document.getElementById('config-status').textContent = '已提交启动请求，等待就绪...';
      document.getElementById('config-status').className = 'status-msg';
      // 启动轮询检测上线
      if (window._ragPollTimer) clearInterval(window._ragPollTimer);
      window._ragPollTimer = setInterval(checkRagStatus, 1500);
      checkRagStatus();  // 立即查一次，不等 interval
    } else {
      document.getElementById('config-status').textContent = d.error || '启动失败';
      document.getElementById('config-status').className = 'status-msg error';
      checkRagStatus();
    }
  }).catch(err => {
    document.getElementById('config-status').textContent = '请求失败';
    document.getElementById('config-status').className = 'status-msg error';
    checkRagStatus();
  });
}

function stopRag() {
  window._ragManuallyStopped = true;  // 不再显示"RAG 运行中"
  window._ragStoppedAt = Date.now();
  const stopBtn = document.getElementById('rag-stop-btn');
  const startBtn = document.getElementById('rag-start-btn');
  stopBtn.disabled = true;
  stopBtn.textContent = '停止中...';
  startBtn.disabled = true;
  startBtn.textContent = '停止中...';
  document.getElementById('rag-status-indicator').innerHTML = '<span style="color:#f39c12;font-weight:600">正在停止 RAG...</span>';

  fetch('/api/rag/stop', { method: 'POST' })
    .then(r => r.json()).then(d => {
      if (d.success) {
        document.getElementById('rag-status-indicator').innerHTML = '<span style="color:#e94560;font-weight:600">RAG 离线</span>';
        document.getElementById('rag-stop-btn').disabled = true;
        document.getElementById('rag-stop-btn').textContent = '停止 RAG';
        document.getElementById('rag-start-btn').disabled = false;
        document.getElementById('rag-start-btn').textContent = '冷启动 RAG';
        document.getElementById('rag-kb-row').style.display = 'none';
      } else if (d.error) {
        document.getElementById('config-status').textContent = d.error;
        document.getElementById('config-status').className = 'status-msg error';
        document.getElementById('rag-stop-btn').disabled = false;
        document.getElementById('rag-stop-btn').textContent = '停止 RAG';
        document.getElementById('rag-start-btn').disabled = false;
        document.getElementById('rag-start-btn').textContent = '冷启动 RAG';
      }
    })
    .catch(() => {});
}

function testConnection() {
  const backend = document.getElementById('planner-backend').value;
  const base_url = document.getElementById('planner-base-url').value;
  const el = document.getElementById('config-status');
  el.textContent = '⏳ 测试中...';
  el.className = 'status-msg';
  fetch(`/api/llm/test?backend=${encodeURIComponent(backend)}&base_url=${encodeURIComponent(base_url)}`)
    .then(r => r.json()).then(d => {
      el.textContent = d.success ? '✓ ' + d.message : '✗ ' + d.message;
      el.className = 'status-msg ' + (d.success ? 'success' : 'error');
    });
}

function refreshModels(prefix, savedValue) {
  const backend = document.getElementById(prefix + '-backend').value;
  const base_url = document.getElementById(prefix + '-base-url').value;
  const sel = document.getElementById(prefix + '-model');
  const currentVal = sel.value || savedValue;
  sel.innerHTML = '<option value="">(加载中...)</option>';
  sel.disabled = true;
  fetch(`/api/llm/models?backend=${encodeURIComponent(backend)}&base_url=${encodeURIComponent(base_url)}`)
    .then(r => r.json()).then(d => {
      const models = (d.success && d.models) || [];
      if (models.length) {
        sel.innerHTML = '<option value="">(请选择)</option>';
        models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          sel.appendChild(opt);
        });
        // 只恢复「当前后端返回列表内」的值——跨后端一律不恢复，每个后端各管各的模型，杜绝配置互相污染
        const restoreVal = currentVal || savedValue;
        if (restoreVal && Array.from(sel.options).some(o => o.value === restoreVal)) {
          sel.value = restoreVal;
          _modelValues[prefix] = restoreVal;  // 内存值同步（防 autoSave 读空）
        }
      } else {
        sel.innerHTML = '<option value="">(未获取到模型 — 请检查后端服务与地址)</option>';
      }
      sel.disabled = false;
    }).catch(() => {
      sel.innerHTML = '<option value="">(获取失败)</option>';
      sel.disabled = false;
    });
}

// ===== 会话操作 =====
function loadSessions() {
  fetch('/api/sessions').then(r => r.json()).then(d => {
    if (!d.success) return;
    const list = document.getElementById('session-list');
    const archList = document.getElementById('sidebar-archived-list');
    const archSection = document.getElementById('sidebar-archived');
    list.innerHTML = '';
    archList.innerHTML = '';
    let active = 0, archived = 0;
    (d.sessions || []).forEach(s => {
      const item = document.createElement('div');
      item.className = 'session-item' + (s.id === currentSessionId && s.active ? ' active' : '') + (s.active ? '' : ' archived');
      const actions = s.active
        ? `<div class="s-actions"><button onclick="event.stopPropagation();archiveSession('${s.id}')" title="归档">🗂</button></div>`
        : `<div class="s-actions"><button onclick="event.stopPropagation();restoreSession('${s.id}')" title="恢复">↩</button><button id="del-${s.id}" onclick="event.stopPropagation();deleteSession('${s.id}')" title="单击确认，再单击删除" style="transition:all 0.2s">✕</button></div>`;
      item.innerHTML = `<div style="display:flex;align-items:center;width:100%"><div style="flex:1;min-width:0"><div class="s-title">${s.title || '未命名'}</div><div class="s-meta">${s.phase} · ${s.created_at?.slice(0,10) || ''}</div></div>${actions}</div>`;
      item.onclick = () => { if (s.active) loadSession(s.id); };
      if (s.active) {
        list.appendChild(item);
        active++;
      } else {
        archList.appendChild(item);
        archived++;
      }
    });
    document.getElementById('archived-count').textContent = archived;
    archSection.style.display = archived > 0 ? 'block' : 'none';
  });
}

function newSession() {
  fetch('/api/session/new', { method: 'POST' }).then(r => r.json()).then(d => {
    if (d.success) {
      currentSessionId = d.session_id;
      currentOutline = null;
      stopProgressPolling();  // 停旧轮询 + 隐藏确认面板 + 重置 _ncConfirmId
      const msgs = document.getElementById('chat-messages');
      msgs.innerHTML = `<div class="msg assistant"><div class="msg-label">助手</div><div class="msg-content">已创建新会话。请输入写作主题开始。</div></div>`;
      msgs.scrollTop = msgs.scrollHeight;
      loadSessions();
    }
  });
}

function archiveSession(id) {
  fetch('/api/session/archive', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id: id}) })
    .then(r => r.json()).then(d => { if (d.success) loadSessions(); });
}

function restoreSession(id) {
  fetch('/api/session/restore', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id: id}) })
    .then(r => r.json()).then(d => { if (d.success) loadSessions(); });
}

const _delPending = {};

function deleteSession(id) {
  // 清理其他待确认（避免多个按钮同时处于待确认状态）
  for (const pid in _delPending) {
    if (pid !== id) {
      clearTimeout(_delPending[pid]);
      delete _delPending[pid];
      const oldBtn = document.getElementById('del-' + pid);
      if (oldBtn) { oldBtn.textContent = '✕'; oldBtn.style.background = ''; oldBtn.style.color = ''; oldBtn.style.padding = ''; }
    }
  }
  const btn = document.getElementById('del-' + id);
  if (_delPending[id]) {
    // 双击确认
    clearTimeout(_delPending[id]);
    delete _delPending[id];
    if (btn) { btn.textContent = ''; btn.style.background = ''; btn.style.color = ''; btn.style.padding = ''; }
    fetch('/api/session/delete', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id: id}) })
      .then(r => r.json()).then(d => { if (d.success) loadSessions(); });
  } else {
    // 第一次单击：进入待确认状态
    if (btn) { btn.textContent = '确认?'; btn.style.background = '#c0392b'; btn.style.color = '#fff'; btn.style.borderRadius = '3px'; btn.style.padding = '2px 6px'; }
    _delPending[id] = setTimeout(() => {
      delete _delPending[id];
      if (btn) { btn.textContent = '✕'; btn.style.background = ''; btn.style.color = ''; btn.style.padding = ''; }
    }, 2500);
  }
}

function toggleArchived() {
  const list = document.getElementById('sidebar-archived-list');
  const toggle = document.getElementById('archived-toggle');
  if (list.style.display === 'none') {
    list.style.display = '';
    toggle.textContent = '▾';
  } else {
    list.style.display = 'none';
    toggle.textContent = '▸';
  }
}

function loadSession(sid) {
  currentSessionId = sid;
  currentOutline = null;
  // 停止旧会话的进度轮询（含隐藏确认面板/重置 _ncConfirmId）——
  // 否则切到非写作会话时，旧轮询闭包仍持有旧 sessionId，1.5s 后把旧确认面板弹回来（残留不稳定根因）
  stopProgressPolling();
  stopReplanRecoverPolling();  // 切会话/重载时停旧的重规划恢复轮询
  // 清除旧消息，切换到该会话
  document.getElementById('chat-messages').innerHTML = '';
  loadSessions();

  // 加载会话状态，恢复大纲
  fetch(`/api/session/load?session_id=${sid}`)
    .then(r => r.json()).then(d => {
      if (!d.success) {
        addAssistantMsg('会话 ' + sid + ' 加载失败');
        return;
      }
      const s = d.session;
      const p = d.progress;
      currentOutline = s.outline;

      // 重建会话消息历史（用户要求等）——切会话/重启/规划未完成时切回都必须能看到输入内容
      (s.messages || []).forEach(m => {
        if (m.role === 'user') addUserMsg(m.content);
      });

      if (s.phase === 'config') {
        // 规划中/通用线对话会话：outline 可能为空，历史消息已在上方重建显示
        const msgs = s.messages || [];
        addAssistantMsg(msgs.length
          ? '会话已恢复：上方为历史消息。输入写作要求可生成大纲；若规划进行中请稍候。'
          : '会话 ' + sid + ' 已切换到（尚未提交内容）');
        return;
      }
      if (s.phase === 'done' || s.phase === 'error') {
        // 已完成/失败的会话
        let msg = '恢复会话：' + (s.outline?.title || '未命名') + '\n';
        msg += '状态：' + (s.phase === 'done' ? '已完成' : '失败') + '\n';
        msg += '进度：' + p.done + '/' + p.total + ' 节，' + p.total_words + ' 字\n';
        if (s.output_file) {
          msg += '\n输出文件：' + (s.output_file.split('/').pop() || s.output_file.split('\\').pop());
        }
        addAssistantMsg(msg);
        if (s.outline?.sections?.length) {
          // 小说线：先看后端是否恢复成功（loadSession 已自动尝试备份恢复）——
          // 恢复成功/state 存在 → 非只读渲染（「开始生成」= 续写入口）；
          // 恢复失败（无备份）→ 只读 + 明确提示项目已丢失
          const isNovel = !!(s.outline._novel || (s.outline.sections || []).some(x => x._novel));
          const nr = d.novel_restore;
          const restoredOk = isNovel && nr && (nr.status === 'ok' || nr.status === 'restored');
          const editable = isNovel && (s.phase === 'error' || restoredOk);
          renderOutline(s.outline, editable ? false : true);
          if (editable && s.phase === 'error') {
            addAssistantMsg('⚠️ 上次写作失败（可能是模型超时/项目状态丢失）。点下方「开始生成」可重试。');
          } else if (isNovel && nr && nr.status === 'missing') {
            addAssistantMsg('⚠️ 项目状态已丢失且无可用备份，无法续写。大纲只读展示，请删除此会话重新规划。');
          } else if (editable && restoredOk && nr.status === 'restored') {
            addAssistantMsg('♻️ 项目状态已从备份自动恢复，可继续写作。点下方「开始生成」续写。');
          }
        }
      } else if (s.phase === 'writing') {
        let msg = '恢复写作中的会话：' + (s.outline?.title || '未命名') + '\n';
        msg += '进度：' + p.done + '/' + p.total + ' 节已完成';
        addAssistantMsg(msg);
        if (s.outline?.sections?.length) {
          // 小说线：非只读渲染（子结构可见 + 底部「开始生成」按钮 = 续写入口）；
          // 通用线保持只读（写作中大纲不可改）
          const isNovel = !!(s.outline._novel || (s.outline.sections || []).some(x => x._novel));
          renderOutline(s.outline, isNovel ? false : true);
          const nr = d.novel_restore;
          if (isNovel && nr && nr.status === 'restored') {
            addAssistantMsg('♻️ 项目状态已从备份自动恢复，可继续写作。点下方「开始生成」续写。');
          } else if (isNovel && nr && nr.status === 'missing') {
            addAssistantMsg('⚠️ 项目状态已丢失且无可用备份，无法续写。大纲只读展示，请删除此会话重新规划。');
          }
        }
        // 加载 writing 会话：不主动弹章级确认面板。确认面板是「开始生成」流程的子确认步骤——
        // 先查一次进度：线程确实在跑（断线重连）才轮询恢复进度；否则不轮询、不弹面板，
        // 等用户点「开始生成」→ 启动线程 → 遇到 planning 章自然弹出确认面板。
        fetch(`/api/progress?session_id=${sid}`).then(r => r.json()).then(d => {
          const running = !!(d.progress && d.progress.running);
          if (running) {
            startProgressPolling(sid);
          }
        }).catch(() => {});
      } else if (s.phase === 'reviewing') {
        addAssistantMsg('恢复会话：大纲已准备，请确认或修改后开始生成');
        if (s.outline?.sections?.length) {
          renderOutline(s.outline);
        }
      } else {
        addAssistantMsg('已切换到会话 ' + sid);
      }
      // 重规划在途恢复（刷新/重连后）：session/load 返回活 in-flight →
      // 重建 _replanBusy + 禁用三按钮/章卡片/确认面板行，轮询直到重规划完成自动恢复。
      // 必须在 renderOutline 之后执行（按钮 HTML 已渲染，才能禁用到位）
      const inflight = s._replan_inflight || [];
      if (inflight.length) {
        const fi = inflight[0];
        _replanBusy = {type: fi.type, id: fi.target_id || null};
        _markActionButtonsBusy(true);
        if (fi.type === 'section') _markSectionBusy(fi.target_id, true);
        if (fi.type === 'novel_sub') _markReplanRowBusy(fi.target_id, true);
        addAssistantMsg('⚠️ 检测到重规划进行中（' + (fi.type === 'section' ? '章节' : '子结构') + '），操作按钮已禁用，完成后自动恢复');
        startReplanRecoverPolling(sid, fi.type, fi.target_id);
      }
    });
}

// ===== 重规划在途恢复轮询（刷新/重连后） =====
// 刷新后 _replanBusy（内存态）丢失，但后端 _replan_inflight 持久化——从 session/load 恢复禁用态后，
// 轮询轻量接口直到 in-flight 清空（重规划完成）→ 恢复按钮 + 提示
let _replanRecoverTimer = null;
function startReplanRecoverPolling(sid, type, id) {
  stopReplanRecoverPolling();
  _replanRecoverTimer = setInterval(() => {
    fetch(`/api/novel/replan_status?session_id=${sid}`)
      .then(r => r.json()).then(d => {
        if (!d.success) return;
        if (!d.inflight || !d.inflight.length) {
          // 重规划完成：恢复全部禁用态
          _replanBusy = null;
          _markActionButtonsBusy(false);
          if (type === 'section') _markSectionBusy(id, false);
          if (type === 'novel_sub') _markReplanRowBusy(id, false);
          if (currentOutline) _syncActionButtonsBusy();
          stopReplanRecoverPolling();
          addAssistantMsg('✅ 重规划已完成，操作按钮已恢复');
        }
      }).catch(() => {});
  }, 2000);
}
function stopReplanRecoverPolling() {
  if (_replanRecoverTimer) {
    clearInterval(_replanRecoverTimer);
    _replanRecoverTimer = null;
  }
}

// ===== 元数据输入框渲染 =====

function renderMetaInputs(templateName) {
  const bar = document.getElementById('meta-inputs-bar');
  const container = document.getElementById('meta-inputs-container');
  container.innerHTML = '';
  fetch('/api/config').then(r => r.json()).then(d => {
    if (!d.success) return;
    const templates = d.config.templates || {};
    const tmpl = templates[templateName] || {};
    const metaFields = tmpl.meta || [];
    if (!metaFields.length) {
      bar.style.display = 'none';
      return;
    }
    const userFields = metaFields.filter(f => f.source === 'user' || f.source === 'auto');
    if (!userFields.length) { bar.style.display = 'none'; return; }
    bar.style.display = '';
    // 使用 grid 布局，每行最多 4 个
    container.style.display = 'grid';
    container.style.gridTemplateColumns = 'repeat(4, 1fr)';
    container.style.gap = '8px';
    userFields.forEach(f => {
      const wrap = document.createElement('div');
      wrap.style.cssText = 'display:flex;align-items:center;gap:4px;min-width:0';
      const label = document.createElement('label');
      label.textContent = f.name + (f.source === 'auto' ? '(可选)' : '');
      label.style.cssText = 'font-size:12px;color:var(--text-dim);white-space:nowrap;min-width:50px';
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'meta-field-input';
      input.dataset.fieldName = f.name;
      input.placeholder = f.desc + (f.source === 'auto' ? '（留空LLM生成）' : '');
      input.style.cssText = 'flex:1;padding:4px 6px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px';
      wrap.appendChild(label);
      wrap.appendChild(input);
      container.appendChild(wrap);
    });
  });
}

// Also update onTemplateChange to refresh meta inputs
const _origOnTemplateChange = onTemplateChange;
onTemplateChange = function() {
  if (_origOnTemplateChange) _origOnTemplateChange();
  const sel = document.getElementById('template-select');
  renderMetaInputs(sel.value);
};

// ===== 消息处理 =====
function sendMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';

  addUserMsg(text);

  // 检查是否是大纲规划请求
  const isWritingReq = /写|生成|创作|撰写|起草/.test(text);
  if (isWritingReq) {
    startPlanning(text);
  } else {
    fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message: text, session_id: currentSessionId})
    }).then(r => r.json()).then(d => {
      if (d.session_id) currentSessionId = d.session_id;  // 通用线对话也建会话（消息持久化）
      if (d.type === 'writing_request') {
        addOutlineProposal(d.topic, d.text);
      } else {
        addAssistantMsg(d.text || '(无响应)');
      }
    });
  }
}

function startPlanning(topic) {
  const statusEl = addAssistantMsg('⏳ 正在生成大纲...');
  // 收集 meta 字段（用户/auto 已填的值）
  const meta = {};
  document.querySelectorAll('#meta-inputs-container .meta-field-input').forEach(el => {
    const val = el.value.trim();
    if (val) meta[el.dataset.fieldName] = val;
  });
  // topic 不作为 meta 注入，让 LLM 根据主题自动生成 auto 字段
  // 继续保留 topic 本身的上下文
  // 当前选中模板名
  const templateName = document.getElementById('template-select').value;
  // 小说线：题材必填（题材=场景配置/世界观根，缺失整篇漂移）；篇幅可不填（默认中篇）
  if (isNovelTemplateSelected() && !meta['题材']) {
    statusEl.remove();
    alert('小说需要填写「题材」（如 科幻/武侠/悬疑/都市/奇幻/历史）——题材决定场景配置与世界观，缺失会导致 AI 瞎编。篇幅可不填（默认中篇）。');
    return;
  }
  fetch('/api/plan', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      topic: topic,
      session_id: currentSessionId,
      template_name: templateName,
      meta: meta
    })
  }).then(r => r.json()).then(d => {
    // 移除状态消息
    statusEl.remove();
    if (d.success) {
      currentSessionId = d.session_id;
      currentOutline = d.outline;
      renderOutline(d.outline);
      loadSessions();
    } else {
      addAssistantMsg('❌ 大纲生成失败：' + (d.error || '未知错误'));
    }
  }).catch(err => {
    statusEl.remove();
    addAssistantMsg('❌ 请求失败：' + err.message);
  });
}

// ===== 罗马数字转换 =====
// ===== 交互式大纲渲染 =====
function renderOutline(outline, readOnly) {
  readOnly = readOnly || false;
  const html = buildOutlineHTML(outline, readOnly);
  addAssistantMsg(html);
  // 重渲染后同步按钮状态：若 _replanBusy 仍在途，重建按钮不会被禁用，需重新应用
  if (!readOnly) _syncActionButtonsBusy();
}

function buildOutlineHTML(outline, readOnly) {
  const sections = outline.sections || [];
  let secHTML = '';
  sections.forEach((s, i) => {
    const orderOpts = ['', ...Array.from({length: sections.length}, (_, i) => String(i+1))]
      .map(v => `<option value="${v}" ${i===0 && v==='' ? 'selected' : ''}>${v || '自动'}</option>`).join('');
    const statusIcon = s.status === 'done' ? '✅' : (s.status === 'in_progress' ? '⏳' : '');
    const secTag = s.type === 'leaf'
      ? '<span style="font-size:10px;color:#f39c12;background:rgba(243,156,18,0.15);padding:1px 5px;border-radius:3px;margin-left:4px">LEAF</span>'
      : '<span style="font-size:10px;color:#5dade2;background:rgba(93,173,226,0.15);padding:1px 5px;border-radius:3px;margin-left:4px">SEC</span>';

    // 子结构行
    const subs = s.sub_sections || [];
    const subCount = subs.length;
    let subHTML = '';
    subs.forEach(ss => {
      const subOpts = ['', ...Array.from({length: subCount}, (_, i) => `s${i+1}`)]
        .map(v => `<option value="${v}" ${ss.id.endsWith('_1') && v==='' ? 'selected' : ''}>${v === '' ? '自动' : v}</option>`).join('');
      subHTML += `
        <div class="sub-card" data-sid="${ss.id}" style="margin-left:24px;padding:4px 8px;border-left:2px solid var(--border);margin-bottom:4px;">
          <div style="display:flex;align-items:center;gap:8px;">
            ${readOnly ? '' : `<input type="checkbox" class="sc-sub-cb" ${ss._checked !== false ? 'checked' : ''} onchange="onSubToggle(this, '${s.id}')">`}
            ${readOnly ? '' : `<select class="sc-sub-order" style="width:48px;font-size:11px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:2px" onchange="collectOutlineData()">${subOpts}</select>`}
            ${readOnly
              ? `<span style="font-size:13px;flex:1;color:var(--text-dim)">${ss.title}</span>`
              : `<input class="sub-title-input" data-sid="${ss.id}" value="${escapeAttr(ss.title)}" onchange="onTitleChange(this)" style="flex:1;min-width:90px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:2px 4px;font-size:12px">`}
            ${readOnly ? '' : `<input type="number" class="sub-words" value="${ss.word_count || 400}" style="width:58px;font-size:11px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:2px" min="100" max="2000" onchange="onSubWordChange(this, '${ss.id}', '${s.id}')"><span style="font-size:11px;color:var(--text-dim)">字</span>`}
            ${readOnly ? `<span style="font-size:11px;color:var(--text-dim)">${ss.word_count || ''}字</span>` : ''}
            ${ss.status === 'done' ? '<span style="font-size:11px;color:var(--green)">✓</span>' : ''}
            ${readOnly ? '' : `<button class="btn btn-sm btn-secondary" style="font-size:10px;padding:2px 6px" onclick="openAuxModal('${ss.id}')" title="辅助知识">+</button>`}
            ${readOnly ? '' : `<button class="btn btn-sm btn-secondary" style="font-size:10px;padding:2px 6px" onclick="openReplanModal('sub','${ss.id}')" title="重新规划该子结构（只重做这一个）">重规划</button>`}
          </div>
          ${ss.summary ? `<div style="font-size:11px;color:var(--text-dim);margin-left:80px;margin-top:2px;line-height:1.3">${ss.summary}</div>` : ''}
        </div>`;
    });

    secHTML += `
      <div class="section-card" data-sid="${s.id}">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;width:100%">
          ${readOnly ? '' : `<input type="checkbox" class="sc-section-cb" ${s._checked !== false ? 'checked' : ''} onchange="onSectionToggle(this, '${s.id}')" style="flex-shrink:0">`}
          ${readOnly
            ? `<div class="sc-label" style="flex:1">${s.title} ${secTag}${s.is_key ? ' <span class="sc-key">⭐重点</span>' : ''} ${statusIcon}</div>`
            : `<input class="sec-title-input" data-sid="${s.id}" value="${escapeAttr(s.title)}" onchange="onTitleChange(this)" style="flex:1;min-width:120px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:3px 6px;font-size:13px"> ${secTag} ${statusIcon}`}
          <div class="sc-meta">${s.subtitle || ''}</div>
          ${readOnly ? `<span style="font-size:12px;color:var(--text-dim)">${s.status === 'done' ? s.actual_word_count + '字' : (s.status === 'in_progress' ? '写作中...' : '')}</span>` : ''}
          ${readOnly ? '' : `<label style="font-size:12px;color:var(--sc-key);cursor:pointer"><input type="checkbox" class="sc-key-cb" ${s.is_key ? 'checked' : ''} onchange="collectOutlineData()"> ⭐重点</label>`}
          ${readOnly ? '' : `<select class="sc-order" onchange="collectOutlineData()">${orderOpts}</select>`}
          ${readOnly ? '' : (s.type === 'leaf'
            ? (s.word_count === 0
              ? `<span style="font-size:12px;color:var(--text-dim)">自由</span>`
              : `<input type="number" class="sec-word-input" data-sid="${s.id}" value="${s.word_count || 800}" style="width:58px;font-size:11px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:2px" min="50" max="5000" onchange="onLeafWordChange(this, '${s.id}')"><span style="font-size:13px;color:var(--text-dim)">字</span>`)
            : `<span class="sec-word-sum" data-sid="${s.id}" style="font-size:13px;color:var(--text-dim)">${s.word_count}</span><span style="font-size:13px;color:var(--text-dim)">字</span>`)}
          ${readOnly ? '' : `<label class="sc-rag"><input type="checkbox" class="sc-rag-cb" onchange="onRagToggle(this, '${s.id}')" ${!ragOnline ? 'disabled title="RAG未连接"' : ''}> RAG</label>` + (ragOnline && Array.isArray(ragKbs) ? `<select class="sc-kb" style="display:none;width:120px;font-size:12px" onchange="collectOutlineData()">${'<option value=\"\">自动KB</option>' + ragKbs.map(k => '<option value=\"' + k + '\">' + k + '</option>').join('')}</select>` : '')}
          ${readOnly ? '' : `<button class="btn btn-sm btn-secondary sec-replan-btn" style="font-size:10px;padding:2px 6px" onclick="openReplanModal('section','${s.id}')" title="重新规划该章节（子结构全部重做）">重规划</button>`}
        </div>
        ${s.summary ? `<div style="font-size:11px;color:var(--text-dim);margin:2px 0 2px 26px;line-height:1.3">📖 ${s.summary}</div>` : ''}
        ${readOnly ? '' : subHTML}
      </div>`;
  });

  let actionsHTML = '';
  if (!readOnly) {
    actionsHTML = `
      <div class="progress-bar" id="progress-bar"><div class="fill" style="width:0%"></div></div>
      <div class="outline-actions">
        <button class="btn btn-primary" id="btn-start-gen" onclick="startGeneration()">开始生成</button>
        <button class="btn btn-secondary" id="btn-replan-outline" onclick="replanOutline()">重新规划</button>
        <button class="btn btn-success" id="btn-save-example" onclick="saveExampleAndGenerate()" title="先保存为快速范例，再开始生成；完成后文章自动回填进范例">保存范例并生成</button>
        <div id="rag-status-text" style="font-size:11px;color:var(--text-dim);margin-top:6px"></div>
      </div>`;
  } else {
    const allSubs = sections.flatMap(s => s.sub_sections || []);
    const doneSubs = allSubs.filter(ss => ss.status === 'done').length;
    const pct = allSubs.length > 0 ? Math.round(doneSubs / allSubs.length * 100) : 0;
    actionsHTML = `
      <div class="progress-bar"><div class="fill" style="width:${pct}%"></div></div>
      <div style="font-size:12px;color:var(--text-dim);margin-top:4px">${doneSubs}/${allSubs.length} 子结构已完成</div>`;
  }

  return `<div class="outline-card" id="outline-card">
    <div class="oc-title" style="display:flex;align-items:center;gap:8px">
      <span>大纲：${outline.title}</span>
      <span style="font-size:11px;color:var(--text-dim);font-weight:normal">☑ 勾选 = 写入，取消 = 跳过</span>
    </div>
    ${secHTML}
    ${actionsHTML}
  </div>`;
}

function collectOutlineData() {
  // 收集用户操作数据（用于生成时提交）
  return true;
}

// ===== 大纲勾选/取消 =====
function onSectionToggle(cb, sectionId) {
  const card = cb.closest('.section-card');
  const checked = cb.checked;
  // 同步所有子结构 checkbox
  card.querySelectorAll('.sc-sub-cb').forEach(subCb => {
    subCb.checked = checked;
  });
  // 重新计算章节字数
  recalcSectionWordSum(sectionId);
  collectOutlineData();
}

function onSubToggle(cb, sectionId) {
  // 重新计算章节字数（取消的子结构不计入）
  recalcSectionWordSum(sectionId);
  collectOutlineData();
}

function recalcSectionWordSum(secId) {
  const card = document.getElementById('outline-card');
  if (!card || !currentOutline) return;
  const sec = currentOutline.sections.find(s => s.id === secId);
  if (!sec) return;
  const sc = card.querySelector(`.section-card[data-sid="${secId}"]`);
  if (!sc) return;
  let sum = 0;
  sc.querySelectorAll('.sub-card').forEach(sub => {
    const subCb = sub.querySelector('.sc-sub-cb');
    if (subCb && !subCb.checked) return;  // 未勾选的子结构不计入
    const wordEl = sub.querySelector('.sub-words');
    sum += parseInt(wordEl?.value) || 0;
  });
  sec.word_count = sum;
  const sumEl = sc.querySelector('.sec-word-sum');
  if (sumEl) sumEl.textContent = sum;
}

// ===== 子结构字数编辑 =====
function onSubWordChange(el, subId, secId) {
  const val = parseInt(el.value) || 400;
  if (currentOutline) {
    const sec = currentOutline.sections.find(s => s.id === secId);
    if (sec) {
      const sub = sec.sub_sections.find(ss => ss.id === subId);
      if (sub) sub.word_count = val;
    }
  }
  recalcSectionWordSum(secId);
  collectOutlineData();
}

// ===== 辅助知识模态框 =====
let _auxModalSubId = null;
let _pluginList = [];
let _pluginResult = null;

// ===== 数据源插件 =====
function loadPlugins() {
  fetch('/api/plugins').then(r => r.json()).then(d => {
    if (!d.success) return;
    _pluginList = d.plugins || [];
    const sel = document.getElementById('plugin-select');
    if (!sel) return;
    sel.innerHTML = '<option value="">（选择插件）</option>' + _pluginList.map(p =>
      '<option value="' + p.id + '">' + escapeHtml(p.name) + '</option>').join('');
  }).catch(() => {});
}

function renderPluginForm() {
  const sel = document.getElementById('plugin-select');
  const plugin = _pluginList.find(p => p.id === sel.value);
  const box = document.getElementById('plugin-fields');
  const btn = document.getElementById('plugin-run-btn');
  const resBox = document.getElementById('plugin-result');
  _pluginResult = null;
  if (resBox) resBox.innerHTML = '';
  if (!plugin) { box.innerHTML = ''; btn.disabled = true; return; }
  btn.disabled = false;
  box.innerHTML = plugin.input_fields.map(f => {
    let ctrl;
    if (f.type === 'select') {
      ctrl = `<select id="pf-${f.key}" style="flex:1;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:4px 6px;font-size:12px">` +
        (f.options || []).map(o => `<option value="${o.value}">${escapeHtml(o.label)}</option>`).join('') + '</select>';
    } else {
      const itype = f.type === 'password' ? 'password' : 'text';
      ctrl = `<input id="pf-${f.key}" type="${itype}" style="flex:1;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:4px 6px;font-size:12px" placeholder="${escapeAttr(f.hint || '')}">`;
    }
    return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px"><span style="width:150px;font-size:12px;color:var(--text-dim);flex-shrink:0">${escapeHtml(f.label)}</span>${ctrl}</div>`;
  }).join('');
}

function runPlugin() {
  const sel = document.getElementById('plugin-select');
  const plugin = _pluginList.find(p => p.id === sel.value);
  if (!plugin) return;
  const inputs = {};
  plugin.input_fields.forEach(f => {
    const el = document.getElementById('pf-' + f.key);
    if (el) inputs[f.key] = el.value.trim();
  });
  const box = document.getElementById('plugin-result');
  box.innerHTML = '<div style="font-size:12px;color:var(--text-dim)">⏳ 正在执行插件...</div>';
  fetch('/api/plugin/run', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({plugin_id: plugin.id, inputs})
  }).then(r => r.json()).then(d => {
    if (!d.success) {
      box.innerHTML = '<div style="font-size:12px;color:#e74c3c">❌ ' + escapeHtml(d.error || '') + '</div>';
      return;
    }
    _pluginResult = d;
    const preview = (d.preview || []).map(l => escapeHtml(l)).join('<br>');
    const rowsText = d.row_count !== undefined ? ('，' + d.row_count + ' 行') : '';
    box.innerHTML =
      '<div style="font-size:12px;color:var(--green)">✅ 已获取「' + escapeHtml(d.name) + '」' + rowsText + '</div>' +
      '<div style="font-family:monospace;font-size:11px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;padding:6px;margin:4px 0;max-height:130px;overflow:auto;color:var(--text)">' + preview + '</div>' +
      '<button class="btn btn-sm btn-primary" onclick="mountPluginResult()">挂载到本子结构</button>';
  }).catch(e => {
    box.innerHTML = '<div style="font-size:12px;color:#e74c3c">❌ ' + escapeHtml(e.message) + '</div>';
  });
}

function mountPluginResult() {
  if (!_pluginResult) return;
  const file = _pluginResult.type === 'table'
    ? {name: _pluginResult.name, type: 'table', path: _pluginResult.path}
    : {name: _pluginResult.name, type: 'text', content: _pluginResult.content};
  addAuxFile(file);
  const box = document.getElementById('plugin-result');
  box.innerHTML = '<div style="font-size:12px;color:var(--green)">✅ 已挂载，点「保存」生效</div>';
}

function openAuxModal(subId) {
  _auxModalSubId = subId;
  const overlay = document.getElementById('aux-modal');
  const textarea = document.getElementById('aux-text-input');
  const fileList = document.getElementById('aux-file-list');
  textarea.value = '';
  fileList.innerHTML = '';
  if (currentOutline) {
    for (const sec of currentOutline.sections) {
      for (const ss of sec.sub_sections || []) {
        if (ss.id === subId && ss.aux_knowledge) {
          textarea.value = ss.aux_knowledge.text || '';
          if (ss.aux_knowledge.files) {
            const label = {table: '表格', text: '文字', image: '图片'};
            ss.aux_knowledge.files.forEach((f, i) => {
              const tag = label[f.type] || '';
              const hint = f.type === 'image' ? '（自动插图至末尾）' : '';
              fileList.innerHTML += `<div class="file-item"><span>${tag ? '[' + tag + '] ' : ''}${f.name}${hint}</span><span class="file-del" onclick="removeAuxFile(${i})">&times;</span></div>`;
            });
          }
          break;
        }
      }
    }
  }
  loadPlugins();
  renderPluginForm();
  overlay.classList.add('show');
}

function closeAuxModal() {
  document.getElementById('aux-modal').classList.remove('show');
  _auxModalSubId = null;
}

function onAuxFilesSelected(event) {
  const fileList = document.getElementById('aux-file-list');
  Array.from(event.target.files).forEach(file => {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    const isText = ext === 'txt' || ext === 'md';
    const isTable = ext === 'csv' || ext === 'db';
    const isImage = ['png','jpg','jpeg','gif'].includes(ext);
    if (!isText && !isTable && !isImage) { alert('不支持的文件类型: ' + file.name); return; }
    if (isText) {
      // 文字：本地读内容
      const reader = new FileReader();
      reader.onload = function(e) { addAuxFile({name: file.name, type: 'text', content: e.target.result}); };
      reader.readAsText(file);
    } else if (isTable || isImage) {
      // 表格/图片：base64 上传后端存临时目录
      const reader = new FileReader();
      reader.onload = function(e) {
        const b64 = String(e.target.result).split(',')[1] || '';
        fetch('/api/aux_upload', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: file.name, b64: b64})
        }).then(r => r.json()).then(d => {
          if (d.success) addAuxFile({name: d.name, type: d.type, path: d.path});
          else alert('上传失败: ' + (d.error || ''));
        }).catch(() => alert('上传失败（网络错误）'));
      };
      reader.readAsDataURL(file);
    }
  });
  event.target.value = '';
}

function addAuxFile(file) {
  if (!currentOutline || !_auxModalSubId) return;
  for (const sec of currentOutline.sections) {
    for (const ss of sec.sub_sections || []) {
      if (ss.id === _auxModalSubId) {
        if (!ss.aux_knowledge) ss.aux_knowledge = {text: '', files: []};
        if (!ss.aux_knowledge.files) ss.aux_knowledge.files = [];
        const idx = ss.aux_knowledge.files.findIndex(f => f.name === file.name);
        if (idx >= 0) ss.aux_knowledge.files[idx] = file;
        else ss.aux_knowledge.files.push(file);
        break;
      }
    }
  }
  const fileList = document.getElementById('aux-file-list');
  const label = {table: '表格', text: '文字', image: '图片'}[file.type] || file.type;
  const hint = file.type === 'image' ? '（自动插图至末尾）' : '';
  fileList.innerHTML += `<div class="file-item"><span>[${label}] ${file.name}${hint}</span><span class="file-del" onclick="removeAuxFile(${document.querySelectorAll('#aux-file-list .file-item').length})">&times;</span></div>`;
}

function removeAuxFile(idx) {
  if (currentOutline && _auxModalSubId) {
    for (const sec of currentOutline.sections) {
      for (const ss of sec.sub_sections || []) {
        if (ss.id === _auxModalSubId && ss.aux_knowledge && ss.aux_knowledge.files) {
          ss.aux_knowledge.files.splice(idx, 1);
          break;
        }
      }
    }
  }
  const fileList = document.getElementById('aux-file-list');
  const items = fileList.querySelectorAll('.file-item');
  if (items[idx]) items[idx].remove();
}

function saveAuxModal() {
  const text = document.getElementById('aux-text-input').value.trim();
  if (!currentOutline || !_auxModalSubId) { closeAuxModal(); return; }
  for (const sec of currentOutline.sections) {
    for (const ss of sec.sub_sections || []) {
      if (ss.id === _auxModalSubId) {
        if (!ss.aux_knowledge) ss.aux_knowledge = {text: '', files: []};
        ss.aux_knowledge.text = text;
        break;
      }
    }
  }
  closeAuxModal();
}

function collectAuxKnowledge() {
  const result = {};
  if (!currentOutline) return result;
  for (const sec of currentOutline.sections) {
    for (const ss of sec.sub_sections || []) {
      if (ss.aux_knowledge && (ss.aux_knowledge.text || (ss.aux_knowledge.files && ss.aux_knowledge.files.length))) {
        result[ss.id] = ss.aux_knowledge;
      }
    }
  }
  return result;
}

// ===== 停止生成 =====
function stopGeneration(type) {
  if (!currentSessionId) return;
  fetch('/api/stop', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({session_id: currentSessionId, type: type})
  }).then(r => r.json()).then(d => {
    if (d.success) {
      document.getElementById('stop-bar').style.display = 'none';
      addAssistantMsg('⏹ 已请求' + (type === 'immediate' ? '立即' : '延时') + '停止，等待当前段落完成后生效...');
    }
  });
}

function getOutlineData() {
  const card = document.getElementById('outline-card');
  if (!card) return null;
  const orders = {};
  const rag = {};
  const keySections = {};
  const checked = {};  // {sectionId: bool, subId: bool}
  const subOrders = {}; // {subId: int}
  const subWords = {}; // {subId: word_count}
  const secWords = {}; // {sectionId: word_count} for leaf sections
  const titles = {};   // {id: 修改后的标题}（章节 + 子结构）
  const auxKnowledge = currentOutline ? collectAuxKnowledge() : {};
  card.querySelectorAll('.sec-title-input').forEach(inp => {
    const v = inp.value.trim();
    if (v) titles[inp.dataset.sid] = v;
  });
  card.querySelectorAll('.sub-title-input').forEach(inp => {
    const v = inp.value.trim();
    if (v) titles[inp.dataset.sid] = v;
  });
  card.querySelectorAll('.section-card').forEach(sc => {
    const sid = sc.dataset.sid;
    const secCb = sc.querySelector('.sc-section-cb');
    if (secCb) checked[sid] = secCb.checked;

    const orderVal = sc.querySelector('.sc-order')?.value;
    if (orderVal) orders[sid] = parseInt(orderVal);
    const keyChecked = sc.querySelector('.sc-key-cb')?.checked;
    if (keyChecked !== undefined) keySections[sid] = keyChecked;
    const ragChecked = sc.querySelector('.sc-rag-cb')?.checked;
    const kb = sc.querySelector('.sc-kb')?.value || '';
    if (ragChecked) rag[sid] = {enabled: true, kb: kb};

    // 子结构 checkbox + 排序 + 字数 + 辅助知识
    sc.querySelectorAll('.sub-card').forEach(sub => {
      const subCb = sub.querySelector('.sc-sub-cb');
      if (subCb) checked[sub.dataset.sid] = subCb.checked;
      const subOrder = sub.querySelector('.sc-sub-order')?.value;
      if (subOrder) subOrders[sub.dataset.sid] = subOrder;
      const subWord = sub.querySelector('.sub-words')?.value;
      if (subWord) subWords[sub.dataset.sid] = parseInt(subWord) || 400;
    });
    // leaf 节字数（允许 0 = 不做字数限制）
    const secWord = sc.querySelector('.sec-word-input')?.value;
    if (secWord !== undefined && secWord !== '') secWords[sid] = parseInt(secWord) || 0;
  });
  return {orders, rag, keySections, checked, subOrders, subWords, secWords, titles, auxKnowledge};
}

function startGeneration() {
  if (isGenerating) return;
  // 重规划在途拦截：章级/子结构/整篇重规划进行中禁止启动生成，
  // 否则写作线程基于旧 outline/旧子结构开跑，与重规划返回的新数据竞态
  if (_replanBusy) {
    addAssistantMsg('⚠️ 有重规划正在进行中，请等待其完成后再开始生成。');
    return;
  }
  if (!currentSessionId || !currentOutline) {
    addAssistantMsg('❌ 请先生成大纲');
    return;
  }
  isGenerating = true;
  _ncConfirmId = null;  // 新一轮生成：重置章确认状态，确保首轮轮询重建确认面板

  const data = getOutlineData();
  const msgEl = addAssistantMsg('⏳ 正在启动生成任务...');

  const genBody = {
    session_id: currentSessionId,
    orders: data?.orders || {},
    rag: data?.rag || {},
    key_sections: data?.keySections || {},
    checked: data?.checked || {},
    sub_orders: data?.subOrders || {},
    sub_words: data?.subWords || {},
    sec_words: data?.secWords || {},
    titles: data?.titles || {},
    aux_knowledge: data?.auxKnowledge || {}
  };
  if (_pendingExampleName) genBody.save_example_name = _pendingExampleName;

  fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(genBody)
  }).then(r => r.json()).then(d => {
    if (d.success) {
      msgEl.querySelector('.msg-content').innerHTML = '⏳ 生成任务已启动，正在写作...';
      document.getElementById('stop-bar').style.display = '';
      // 开始轮询进度
      startProgressPolling(currentSessionId);
    } else {
      msgEl.querySelector('.msg-content').innerHTML = '❌ 启动失败：' + (d.error || '未知错误');
      isGenerating = false;
    }
  }).catch(err => {
    msgEl.querySelector('.msg-content').innerHTML = '❌ 请求失败：' + err.message;
    isGenerating = false;
  });
}

// ===== 自动撰写 + 批量撰写 =====
function startAutoGeneration() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;

  const lines = text.split('\n').map(l => l.trim()).filter(l => l);
  if (lines.length === 0) return;

  if (lines.length === 1) {
    // 单篇自动：plan → 全量RAG → generate → 轮询
    input.value = '';
    const statusEl = addAssistantMsg('⏳ 自动撰写中（规划中...）');
    // 带 RAG 状态发 plan
    const meta = {};
    document.querySelectorAll('#meta-inputs-container .meta-field-input').forEach(el => {
      const val = el.value.trim();
      if (val) meta[el.dataset.fieldName] = val;
    });
    const templateName = document.getElementById('template-select').value;
    // 小说线：题材必填（自动撰写入口同样拦截）
    if (isNovelTemplateSelected() && !meta['题材']) {
      statusEl.remove();
      alert('小说需要填写「题材」（如 科幻/武侠/悬疑/都市/奇幻/历史）——题材决定场景配置与世界观。篇幅可不填（默认中篇）。');
      return;
    }
    fetch('/api/plan', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({topic: text, session_id: currentSessionId, template_name: templateName, meta: meta})
    }).then(r => r.json()).then(d => {
      if (!d.success) {
        statusEl.querySelector('.msg-content').innerHTML = '❌ 规划失败：' + (d.error || '');
        return;
      }
      currentSessionId = d.session_id;
      currentOutline = d.outline;
      // 全量自动 RAG：所有节+子结构启用
      const autoRag = {};
      (d.outline.sections || []).forEach(s => {
        autoRag[s.id] = {enabled: ragOnline, kb: ''};
      });
      statusEl.querySelector('.msg-content').innerHTML = '⏳ 自动撰写中（写作中...）';
      fetch('/api/generate', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          session_id: currentSessionId,
          rag: autoRag,
          sub_words: {},
          aux_knowledge: {}
        })
      }).then(r2 => r2.json()).then(d2 => {
        if (d2.success) {
          startProgressPolling(currentSessionId);
          loadSessions();
        } else {
          statusEl.querySelector('.msg-content').innerHTML = '❌ 生成失败：' + (d2.error || '');
        }
      });
    }).catch(err => {
      statusEl.querySelector('.msg-content').innerHTML = '❌ 请求失败：' + err.message;
    });
  } else {
    // 批量自动：发到后端逐个处理
    input.value = '';
    addAssistantMsg('⏳ 批量自动撰写已启动（共 ' + lines.length + ' 篇）...');
    document.getElementById('batch-progress').style.display = '';
    document.getElementById('batch-progress').innerHTML = '批量进度：0/' + lines.length;
    const templateName = document.getElementById('template-select').value;
    const meta = {};
    document.querySelectorAll('#meta-inputs-container .meta-field-input').forEach(el => {
      const val = el.value.trim();
      if (val) meta[el.dataset.fieldName] = val;
    });
    fetch('/api/batch_auto', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({topics: lines, prompt: '', template_name: templateName, meta: meta})
    }).then(r => r.json()).then(d => {
      if (d.success) {
        const batchId = d.task_id;
        startBatchPolling(batchId, lines.length);
      } else {
        document.getElementById('batch-progress').innerHTML = '❌ 批量启动失败：' + (d.error || '');
      }
    });
  }
}

function startBatchPolling(batchId, totalCount) {
  if (window._batchPollTimer) clearInterval(window._batchPollTimer);
  window._batchPollTimer = setInterval(() => {
    fetch(`/api/batch_progress?task_id=${batchId}`)
      .then(r => r.json()).then(d => {
        if (!d.success) { clearInterval(window._batchPollTimer); return; }
        const done = d.done || 0;
        const progEl = document.getElementById('batch-progress');
        if (progEl) {
          let html = `批量进度：${done}/${d.total}`;
          if (d.current_topic) html += ` &nbsp; 当前：${d.current_topic}`;
          if (d.errors && d.errors.length) html += ` &nbsp; <span style="color:var(--accent)">错误：${d.errors.length}</span>`;
          progEl.innerHTML = html;
        }
        if (d.done_flag) {
          clearInterval(window._batchPollTimer);
          window._batchPollTimer = null;
          // 显示结果
          let resultMsg = `✅ 批量完成！${d.done}/${d.total} 篇成功`;
          const errors = d.errors || [];
          if (errors.length) {
            resultMsg += `\n❌ ${errors.length} 篇失败：\n` + errors.map(e => `  - ${e.topic}: ${e.error}`).join('\n');
          }
          addAssistantMsg(resultMsg);
          if (d.results && d.results.length) {
            d.results.forEach(r => {
              if (r.output_file) {
                const fname = r.output_file.split('/').pop() || r.output_file.split('\\').pop();
                addAssistantMsg(`📄 ${r.topic || '文章'} → ${fname}（${r.word_count || 0}字）`);
              }
            });
          }
          const progEl2 = document.getElementById('batch-progress');
          if (progEl2) progEl2.style.display = 'none';
          loadSessions();
        }
      }).catch(() => {});
  }, 1500);
}

function replanOutline() {
  // 整篇重规划：清空局部目标
  // 在途保护：有局部重规划或整篇重规划在途时禁止再开（防止请求互相覆盖）
  if (_replanBusy) {
    addAssistantMsg('⚠️ 已有重规划进行中，请等待其完成后再发起新的重规划。');
    return;
  }
  _replanTarget = null;
  document.getElementById('replan-modal-title').textContent = '调整规划';
  document.getElementById('replan-modal-hint').textContent = '输入对当前大纲的调整要求。留空则使用原有规划不变。';
  document.getElementById('replan-hints').value = '';
  document.getElementById('replan-modal-status').textContent = '';
  document.getElementById('replan-modal').classList.add('show');
}

function openReplanModal(type, id) {
  // 局部重规划：type='section'（整章，子结构全部重做）| 'sub'|'novel_sub'（单子结构）
  // 在途保护：已有节点在重规划（LLM 未返回）时禁止再开新重规划，防止多个请求互相覆盖
  if (_replanBusy) {
    addAssistantMsg('⚠️ 已有重规划进行中，请等待其完成后再发起新的重规划。');
    return;
  }
  _replanTarget = {type, id};
  document.getElementById('replan-modal-title').textContent = type === 'section' ? '重新规划章节' : '重新规划子结构';
  document.getElementById('replan-modal-hint').textContent = type === 'section'
    ? '输入对该章节的新要求（如方向、子结构数量、融合角度）。章节内全部子结构将按新要求重做，其他章节不受影响。'
    : '输入对该子结构的新要求（如内容方向、重点）。只重做这一个子结构，其余内容不受影响。';
  document.getElementById('replan-hints').value = '';
  document.getElementById('replan-modal-status').textContent = '';
  document.getElementById('replan-modal').classList.add('show');
}

function onLeafWordChange(input, sid) {
  const val = parseInt(input.value);
  if (isNaN(val)) return;
  if (!currentOutline) return;
  const sec = (currentOutline.sections || []).find(s => s.id === sid);
  if (sec) { sec.word_count = val; }
  collectOutlineData();
}

function closeReplanModal() {
  document.getElementById('replan-modal').classList.remove('show');
}

// 确认面板行内反馈 + 底部确认按钮实时同步（不等轮询重建）
// 关键：重规划期间按钮 HTML 已固化，必须主动操作 DOM 设 disabled，
// 否则用户可绕过 JS 守卫点击确认导致竞态（虽然后端也会拒绝，但前端视觉必须一致）。
// subId: 被点击的子结构 id；busy=true 进入 / false 离开
function _markReplanRowBusy(subId, busy) {
  const panel = document.getElementById('novel-confirm-panel');
  if (!panel) return;
  // 1. 底部"确认，写本章"按钮：必须实时同步（HTML 渲染时按 _replanBusy 加 disabled，
  //    但重规划期间面板不会重建，按钮 HTML 早已固化——只能 DOM 直改）
  const confirmBtn = panel.querySelector('.nc-confirm-btn');
  if (confirmBtn) {
    if (busy) {
      confirmBtn.disabled = true;
      confirmBtn.textContent = '重规划中...';
      confirmBtn.title = '有子结构正在重规划，完成后才能确认';
    } else if (!_replanBusy) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = '确认，写本章';
      confirmBtn.title = '';
    }
  }
  // 2. 该子结构所在行：行内反馈（只有被点击的那行显示"重规划中..."）
  const row = panel.querySelector(`[data-subid="${subId}"]`);
  if (row) {
    const replBtn = row.querySelector('.nc-replan-btn');
    const statusEl = row.querySelector('.nc-replan-status');
    if (busy) {
      if (replBtn) replBtn.style.display = 'none';
      if (statusEl) {
        statusEl.textContent = '⏳ 重规划中...';
        statusEl.style.display = 'inline';
      }
      row.style.opacity = '0.6';
    } else {
      if (replBtn) replBtn.style.display = '';
      if (statusEl) statusEl.style.display = 'none';
      row.style.opacity = '';
    }
  }
}

// 章卡片行内反馈：章级重规划在途 → 该章"重规划"按钮禁用 + 文案变"重规划中..." + 卡片半透明
function _markSectionBusy(sectionId, busy) {
  const card = document.querySelector(`.section-card[data-sid="${sectionId}"]`);
  if (!card) return;
  const replBtn = card.querySelector('.sec-replan-btn');
  if (replBtn) {
    replBtn.disabled = busy;
    replBtn.textContent = busy ? '重规划中...' : '重规划';
    replBtn.title = busy ? '该章正在重规划，请等待完成' : '重新规划该章节（子结构全部重做）';
  }
  card.style.opacity = busy ? '0.6' : '';
}

// 底部三个按钮反馈：开始生成 / 重新规划 / 保存范例并生成
// 重规划在途时统一禁用，恢复时还原。文案一字不改——按钮就该是按钮样，按钮禁用不需要告诉用户原因
function _markActionButtonsBusy(busy) {
  const btns = [
    document.getElementById('btn-start-gen'),
    document.getElementById('btn-replan-outline'),
    document.getElementById('btn-save-example'),
  ];
  for (const el of btns) {
    if (el) el.disabled = busy;
  }
}

// 每次 outline 重渲染后调用：根据当前 _replanBusy 状态同步按钮（防止重渲染丢 disable）
function _syncActionButtonsBusy() {
  _markActionButtonsBusy(!!_replanBusy);
}

function confirmReplan() {
  const hints = document.getElementById('replan-hints').value.trim();
  closeReplanModal();

  // ── 局部重规划分支：只重做目标节点，原卡片原地刷新 ──
  if (_replanTarget) {
    const t = _replanTarget;
    _replanTarget = null;
    // 小说线段级重规划（确认面板内的单个子结构）
    if (t.type === 'novel_sub') {
      // 在途标记：确认面板该行显示"重规划中..."，同时禁用"确认，写本章"（防旧数据确认竞态）；
      // 底部三按钮（开始生成/重新规划/保存范例并生成）同步禁用（防竞态启动生成）
      _replanBusy = {type: 'novel_sub', id: t.id};
      _markReplanRowBusy(t.id, true);
      _markActionButtonsBusy(true);
      addAssistantMsg('⏳ 正在重新规划该子结构...');
      // 该段的辅助知识（+按钮挂载的）→ 注入重规划参考层（有才传）
      let subAux = null;
      if (currentOutline) {
        for (const sec of currentOutline.sections || []) {
          const ss = (sec.sub_sections || []).find(x => x.id === t.id);
          if (ss && ss.aux_knowledge && (ss.aux_knowledge.text || (ss.aux_knowledge.files || []).length)) {
            subAux = ss.aux_knowledge;
          }
        }
      }
      fetch('/api/novel/replan_sub', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({session_id: currentSessionId, target_id: t.id, hints, aux: subAux})
      }).then(r => r.json()).then(d => {
        _replanBusy = null;
        _markActionButtonsBusy(false);
        if (d.success) {
          addAssistantMsg('✅ 子结构已重新规划：' + (d.title || '') + '（' + (d.word_count || '') + '字），请在下方确认面板中确认');
          _ncConfirmId = null;  // 强制下轮轮询重建确认面板（显示新子结构）
        } else {
          addAssistantMsg('❌ 子结构重规划失败：' + (d.error || ''));
          _markReplanRowBusy(t.id, false);
        }
      }).catch(err => {
        _replanBusy = null;
        _markActionButtonsBusy(false);
        addAssistantMsg('❌ 请求失败：' + err.message);
        _markReplanRowBusy(t.id, false);
      });
      return;
    }
    addAssistantMsg('⏳ 正在重新规划目标节点...');
    // 章级重规划在途：该章卡片"重规划"按钮禁用 + 文案变"重规划中..." + 卡片半透明；
    // 确认面板同步禁用"确认，写本章"（新子结构将写入 state，等待轮询刷新）；
    // 底部三按钮（开始生成/重新规划/保存范例并生成）同步禁用（防竞态启动生成）
    _replanBusy = {type: t.type, id: t.id};
    if (t.type === 'section') _markSectionBusy(t.id, true);
    _markActionButtonsBusy(true);
    // 小说线判定：章级重规划 → 大纲卡片必须刷新（章级内容更新，outline 保持章级不泄露子结构）；
    // 段级重规划已在上方 novel_sub 分支单独处理（只刷确认面板，不碰大纲卡片）
    const isNovelUI = !!(currentOutline && (currentOutline._novel || (currentOutline.sections || []).some(s => s._novel)));
    fetch('/api/replan_section', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: currentSessionId, target_id: t.id, hints})
    }).then(r => r.json()).then(d => {
      _replanBusy = null;
      _markActionButtonsBusy(false);
      if (t.type === 'section') _markSectionBusy(t.id, false);
      if (d.success) {
        currentOutline = d.outline;
        const card = document.getElementById('outline-card');
        if (card) {
          card.closest('.msg').querySelector('.msg-content').innerHTML = buildOutlineHTML(d.outline);
          _syncActionButtonsBusy();
        } else {
          renderOutline(d.outline);
        }
        addAssistantMsg('✅ 目标节点已重新规划，其他章节保持不变');
        if (isNovelUI) {
          _ncConfirmId = null;  // 章级重规划后新子结构在 state，下轮轮询刷新底部确认面板
        }
      } else {
        addAssistantMsg('❌ 局部重规划失败：' + (d.error || ''));
      }
    }).catch(err => {
      _replanBusy = null;
      _markActionButtonsBusy(false);
      if (t.type === 'section') _markSectionBusy(t.id, false);
      addAssistantMsg('❌ 请求失败：' + err.message);
    });
    return;
  }

  // ── 整篇重规划分支（原有逻辑） ──
  const topic = currentOutline?.title || '';
  if (!topic) return;
  // 整篇重规划在途：底部三个按钮（开始生成/重新规划/保存范例并生成）统一禁用 + 文案变化
  _replanBusy = {type: 'outline', id: null};
  _markActionButtonsBusy(true);
  addAssistantMsg(hints ? '⏳ 正在按新要求重新规划...' : '⏳ 正在重新规划...');
  const meta = {};
  document.querySelectorAll('#meta-inputs-container .meta-field-input').forEach(el => {
    const val = el.value.trim();
    if (val) meta[el.dataset.fieldName] = val;
  });
  const templateName = document.getElementById('template-select').value;
  fetch('/api/plan', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({topic, session_id: currentSessionId, template_name: templateName, meta, plan_hints: hints})
  }).then(r => r.json()).then(d => {
    _replanBusy = null;
    _markActionButtonsBusy(false);
    if (d.success) {
      currentSessionId = d.session_id;
      currentOutline = d.outline;
      renderOutline(d.outline);
      loadSessions();
    } else {
      addAssistantMsg('❌ 重新规划失败：' + (d.error || ''));
    }
  }).catch(err => {
    _replanBusy = null;
    _markActionButtonsBusy(false);
    addAssistantMsg('❌ 请求失败：' + err.message);
  });
}

// ===== 标题修改（章节/子结构可改名） =====
function onTitleChange(input) {
  // 标题修改收集在 getOutlineData() 中从 DOM 读取，这里仅同步 currentOutline 供前端状态一致
  if (!currentOutline) return;
  const id = input.dataset.sid;
  const v = input.value.trim();
  if (!id || !v) return;
  (currentOutline.sections || []).forEach(s => {
    if (s.id === id) { s.title = v; return; }
    (s.sub_sections || []).forEach(ss => {
      if (ss.id === id) ss.title = v;
    });
  });
}

// ===== 保存范例并生成（前置存大纲 + 完成回填文章） =====
function saveExampleAndGenerate() {
  if (isGenerating) return;
  // 重规划在途拦截：与 startGeneration 一致，重规划进行中禁止进入"保存范例并生成"流程
  if (_replanBusy) {
    addAssistantMsg('⚠️ 有重规划正在进行中，请等待其完成后再保存范例并生成。');
    return;
  }
  if (!currentSessionId || !currentOutline) {
    addAssistantMsg('❌ 请先生成大纲');
    return;
  }
  document.getElementById('example-name').value = currentOutline.title || '';
  document.getElementById('example-modal-status').textContent = '';
  document.getElementById('example-modal').classList.add('show');
}

function closeExampleModal() {
  document.getElementById('example-modal').classList.remove('show');
}

function confirmSaveExample() {
  const name = document.getElementById('example-name').value.trim();
  if (!name) {
    document.getElementById('example-modal-status').textContent = '范例名称不能为空';
    return;
  }
  const templateName = document.getElementById('template-select').value;
  fetch('/api/example/save', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, template_name: templateName, outline: currentOutline})
  }).then(r => r.json()).then(d => {
    if (!d.success) {
      document.getElementById('example-modal-status').textContent = '❌ ' + (d.error || '保存失败');
      return;
    }
    closeExampleModal();
    _pendingExampleName = d.name;
    loadExamples();
    addAssistantMsg('✅ 大纲已保存为范例「' + escapeHtml(d.name) + '」，开始生成（完成后自动回填文章）');
    startGeneration();
  }).catch(err => {
    document.getElementById('example-modal-status').textContent = '❌ ' + err.message;
  });
}

// ===== 快速范例调用（跳过 LLM 规划） =====
function loadExamples() {
  fetch('/api/examples').then(r => r.json()).then(d => {
    if (!d.success) return;
    const sel = document.getElementById('example-select');
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">（选择已保存的范例）</option>' + (d.examples || []).map(e => {
      const badge = e.has_article ? ' ✓' : '';
      return `<option value="${escapeAttr(e.name)}">${escapeHtml(e.name)}${badge}（${escapeHtml(e.topic || '')}）</option>`;
    }).join('');
    if (cur) sel.value = cur;
  }).catch(() => {});
}

function useExample() {
  const sel = document.getElementById('example-select');
  const name = sel ? sel.value : '';
  if (!name) { addAssistantMsg('❌ 请先选择一个快速范例'); return; }
  const topic = document.getElementById('example-topic').value.trim();
  const adapt = !!(document.getElementById('example-adapt') && document.getElementById('example-adapt').checked);
  addAssistantMsg('⏳ 正在加载范例「' + escapeHtml(name) + '」...（跳过 LLM 规划' + (adapt ? '，正在按新主题适配大纲' : '') + '）');
  fetch('/api/example/use', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, topic, adapt})
  }).then(r => r.json()).then(d => {
    if (!d.success) { addAssistantMsg('❌ 加载范例失败：' + (d.error || '')); return; }
    currentSessionId = d.session_id;
    currentOutline = d.outline;
    addAssistantMsg(adapt ? '✅ 已加载范例并按新主题适配大纲（结构/RAG/字数不变），可开始生成或继续调整。' : '✅ 已加载范例，规划阶段已跳过。可直接开始生成，或在评审中改标题 / 局部重规划。');
    renderOutline(d.outline);
    loadSessions();
    if (document.getElementById('example-topic')) document.getElementById('example-topic').value = '';
  }).catch(err => addAssistantMsg('❌ 请求失败：' + err.message));
}

function showFileContent(filepath) {
  // 简单提示
  addAssistantMsg(`📎 文件已保存至：${filepath}`);
}

function addOutlineProposal(topic, text) {
  addAssistantMsg(text + '\n\n<button class="btn btn-sm btn-primary" onclick="startPlanning(\'' + topic.replace(/'/g, "\\'") + '\')">📋 生成大纲</button>');
}

// ===== UI 辅助 =====
function addUserMsg(text) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg user';
  div.innerHTML = `<div class="msg-label">我</div><div class="msg-content">${escapeHtml(text)}</div>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function addAssistantMsg(html) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg assistant';
  div.innerHTML = `<div class="msg-label">助手</div><div class="msg-content">${html}</div>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// ===== 轮询进度（实时） =====
let progressInterval = null;

function startProgressPolling(sid) {
  const sessionId = sid || currentSessionId;
  if (!sessionId) return;
  if (progressInterval) clearInterval(progressInterval);
  _lastProgressKey = null;  // 重置防抖（每次启动轮询重新渲染一次最新状态）

  progressInterval = setInterval(() => {
    fetch(`/api/progress?session_id=${sessionId}`)
      .then(r => r.json()).then(d => {
        // 会话守卫：响应返回时若已切换到其他会话，直接丢弃（防 in-flight 竞态残留）
        if (currentSessionId !== sessionId) return;
        if (!d.success) {
          stopProgressPolling();
          return;
        }
        const p = d.progress;

        // 更新进度条（writing 阶段封顶 95%：收尾——参考文献格式化/保存无进度单元，避免提前满格）
        const fill = document.querySelector('.progress-bar .fill');
        if (fill && p.total > 0) {
          const pct = Math.round(p.done / p.total * 100);
          const capped = (pct >= 100 && p.phase === 'writing') ? 95 : Math.min(pct, 100);
          fill.style.width = capped + '%';
        }

        // 更新状态文本
        const statusEl = document.getElementById('rag-status-text');
        if (statusEl && p.status_text) {
          statusEl.textContent = p.status_text;
        }

        // 小说线章级门控：待确认章 → 显示子结构 + 配置（字数/重点/概述）+ 确认按钮
        const ncPanel = document.getElementById('novel-confirm-panel');
        if (ncPanel) {
          if (p.awaiting_confirm) {
            const ac = p.awaiting_confirm;
            if (_ncConfirmId !== ac.id) {
              _ncConfirmId = ac.id;
              const rows = (ac.sub_sections || []).map(ss => {
                // 行内重规划状态：在途 → 该行禁用重规划按钮 + 显示"重规划中..."
                const rowBusy = !!(_replanBusy && _replanBusy.type === 'novel_sub' && _replanBusy.id === ss.id);
                // 子结构顺序下拉（复用通用线 sc-sub-order 语义：自动/s1/s2/s3，确认前可调写作顺序）
                const subCount = (ac.sub_sections || []).length;
                const subOpts = ['', ...Array.from({length: subCount}, (_, i) => `s${i+1}`)]
                  .map(v => `<option value="${v}" ${ss.id.endsWith('_1') && v === '' ? 'selected' : ''}>${v === '' ? '自动' : v}</option>`).join('');
                return `
                <div data-subid="${ss.id}" style="border:1px solid var(--border);border-radius:4px;padding:5px 8px;margin:4px 0;background:var(--bg-input);${rowBusy ? 'opacity:0.6' : ''}">
                  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                    <input type="checkbox" class="nc-check" data-id="${ss.id}" ${ss._checked === false ? '' : 'checked'} title="取消勾选 = 跳过该段">
                    <select class="nc-order" data-id="${ss.id}" style="width:52px;font-size:11px;background:var(--bg);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:2px" title="调整该段在写作中的顺序">${subOpts}</select>
                    <span style="flex:1;min-width:100px;font-size:12px">${ss.title}</span>
                    <input type="number" class="nc-words" data-id="${ss.id}" value="${ss.word_count || 1000}" style="width:58px;font-size:11px;background:var(--bg);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:2px" min="100" max="5000" title="字数目标"><span style="font-size:11px;color:var(--text-dim)">字</span>
                    <label style="font-size:11px;color:var(--sc-key);cursor:pointer"><input type="checkbox" class="nc-key" data-id="${ss.id}" ${ss.is_key ? 'checked' : ''}> ⭐重点</label>
                    <button class="btn btn-sm btn-secondary" style="font-size:10px;padding:2px 6px" onclick="openAuxModal('${ss.id}')" title="辅助知识">+辅助</button>
                    <button class="btn btn-sm btn-secondary nc-replan-btn" style="font-size:10px;padding:2px 6px;${rowBusy ? 'display:none' : ''}" onclick="openReplanModal('novel_sub','${ss.id}')" title="只重新规划这一个子结构">重规划</button>
                    <span class="nc-replan-status" style="font-size:10px;color:#f39c12;display:${rowBusy ? 'inline' : 'none'}">⏳ 重规划中...</span>
                  </div>
                  ${ss.summary ? `<div style="font-size:11px;color:var(--text-dim);margin-left:24px;margin-top:2px;line-height:1.3">${ss.summary}</div>` : ''}
                </div>`;
              }).join('');
              ncPanel.innerHTML = `<div style="font-size:13px;font-weight:500;margin-bottom:4px">⏸ 等待确认：${ac.chapter || ''}《${ac.title}》子结构规划（取消勾选=跳过该段；可改字数/标重点/重规划单段）</div>
                ${rows}
                <button class="btn btn-primary btn-sm nc-confirm-btn" style="margin-top:6px" onclick="confirmNovelChapter()" ${_replanBusy ? 'disabled title="有子结构正在重规划，完成后才能确认"' : ''}>${_replanBusy ? '重规划中...' : '确认，写本章'}</button>`;
            }
            ncPanel.style.display = 'block';
          } else {
            _ncConfirmId = null;
            ncPanel.style.display = 'none';
          }
        }

        // 更新卡片上的状态图标 + 大纲卡片（关键修复：轮询必须用 session 最新 outline 重渲染，
        // 否则切会话再切回后，生成线程后续的章/子结构进度永远不显示——"状态丢失、结果不体现"）
        // 防抖动：仅当进度戳变化才重渲染，避免每 1.5s 全量重绘打断用户交互
        const pkey = (p.done || 0) + '|' + (p.total || 0) + '|' + (p.status_text || '') + '|' + (p.phase || '');
        if (pkey !== _lastProgressKey) {
          _lastProgressKey = pkey;
          fetch(`/api/session/load?session_id=${sessionId}`)
            .then(r2 => r2.json()).then(d2 => {
              if (!d2.success) return;
              if (currentSessionId !== sessionId) return;  // 再守卫一次（异步返回竞态）
              const latest = d2.session.outline;
              if (!latest || !latest.sections) return;
              currentOutline = latest;   // 同步内存大纲（后续确认/重规划基于最新）
              const oldCard = document.getElementById('outline-card');
              if (oldCard && oldCard.closest('.msg')) {
                // 写作中保持可编辑（确认面板在生成控制区，大纲卡片用最新数据重建）
                const editable = !!(latest._novel || (latest.sections || []).some(x => x._novel));
                oldCard.closest('.msg').querySelector('.msg-content').innerHTML = buildOutlineHTML(latest, !editable);
              }
            }).catch(() => {});
        }

        // 检查是否完成
        if (p.phase === 'done' || p.phase === 'error') {
          stopProgressPolling();
          fetchResult(sessionId);
        }

        // 修复引擎章级触发：finalize 后有 HARD 且未修复 → 弹修复面板（不依赖全书 done）
        // 后端 get_progress 的 repair_pending 只在该章 session 章级 done + hint 有 HARD 且未标记 _repaired 时返回
        if (p.repair_pending && p.repair_pending.chapter) {
          const chId = p.repair_pending.chapter;
          showRepairPanel(chId, p.repair_pending.full_items);
        }
      }).catch(() => {});
  }, 1500);
}

function stopProgressPolling() {
  if (progressInterval) { clearInterval(progressInterval); progressInterval = null; }
  const bar = document.getElementById('stop-bar');
  if (bar) bar.style.display = 'none';
  const ncPanel = document.getElementById('novel-confirm-panel');
  if (ncPanel) ncPanel.style.display = 'none';
  _ncConfirmId = null;
}

// 小说线章级门控：确认当前章（应用勾选/字数/重点 → 章 status=confirmed → 写作线程继续）
async function confirmNovelChapter() {
  if (!currentSessionId) return;
  // 重规划在途守卫：子结构重规划未返回前确认 = 用旧子结构写本章（新规划覆盖后状态错乱）
  if (_replanBusy) {
    addAssistantMsg('⚠️ 有子结构正在重规划，请等待其完成后再确认本章。');
    return;
  }
  const checked = {}, subWords = {}, subKeys = {}, subOrders = {};
  document.querySelectorAll('#novel-confirm-panel .nc-check').forEach(cb => {
    checked[cb.dataset.id] = cb.checked;
  });
  document.querySelectorAll('#novel-confirm-panel .nc-words').forEach(inp => {
    const v = parseInt(inp.value);
    if (!isNaN(v) && v > 0) subWords[inp.dataset.id] = v;
  });
  document.querySelectorAll('#novel-confirm-panel .nc-key').forEach(cb => {
    if (cb.checked) subKeys[cb.dataset.id] = true;
  });
  document.querySelectorAll('#novel-confirm-panel .nc-order').forEach(sel => {
    if (sel.value) subOrders[sel.dataset.id] = sel.value;
  });
    if (_ncConfirming) return;  // 防重复点击
    _ncConfirming = true;
  try {
    const r = await fetch('/api/novel/confirm', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: currentSessionId, checked, sub_words: subWords, sub_keys: subKeys, sub_orders: subOrders})
    });
    const d = await r.json();
    if (!d.success) {
      alert(d.error || '确认失败');
      _ncConfirming = false;
      return;
    }
    // 确认成功：立即收掉确认面板（章已 confirmed，写作线程恢复；轮询随后刷新大纲卡片）
    _ncConfirmId = null;
    _ncConfirming = false;
    const ncPanel = document.getElementById('novel-confirm-panel');
    if (ncPanel) ncPanel.style.display = 'none';
    addAssistantMsg('✅ 本章已确认，开始写作。');  // 大纲卡片状态由轮询在 1.5s 内自动刷新
  } catch (e) { alert('确认失败: ' + e); _ncConfirming = false; }
}

// ===== 修复引擎面板（P3：章检问题 → 勾选子结构 → 写作模型整段重构） =====
let _repairPollTimer = null;
let _repairPanelChapter = null;  // 当前已弹出的修复章（防轮询重复弹）
let _autoRecheckRound = 0;      // 自动重检轮次（修复完成后自动重检并迭代修复）
let _repairMode = 'manual';     // 修复模式：manual=重检有问题刷新面板让人再点（无上限）；auto=自动循环修复到通过/超次数

function showRepairPanel(chapter, fullItems) {
  const panel = document.getElementById('novel-repair-panel');
  const modal = document.getElementById('novel-repair-modal');
  if (!panel || !modal || !currentSessionId) return;
  // 防重：同一章已在展示中（或修复中）→ 不重复弹
  if (_repairPanelChapter === chapter && modal.style.display === 'flex') return;
  if (_repairPanelChapter === chapter && _repairPollTimer) return;  // 修复轮询中
  _repairPanelChapter = chapter;
  _autoRecheckRound = 0;  // 新弹窗：重置自动重检轮次
  // 全文三检修复项（fidelity/pledge/ending）：直接渲染，不走 preview
  if (fullItems && fullItems.length) {
    const typeNames = {fidelity: '大纲忠实度', pledge: '全文承诺', ending: '结尾收束'};
    const rows = fullItems.map((it, idx) => {
      const subLabel = it.sub ? it.sub.replace('.txt','') : '（章级）';
      return `<label style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;border-bottom:1px solid var(--border)">
        <input type="checkbox" class="rp-check" data-file="${it.sub ? it.sub + '.txt' : ''}" data-type="${it.type || ''}" checked>
        <span style="font-size:11px;color:var(--accent);flex-shrink:0">[${typeNames[it.type] || it.type}]</span>
        <span style="font-size:12px;flex:1">${subLabel}：${escapeHtml((it.problem || '').slice(0, 80))}</span>
      </label>`;
    }).join('');
    panel.innerHTML = `<div style="font-size:13px;font-weight:500;margin-bottom:4px">🔧 ${chapter} 全文质检需处理（三检修复项）</div>
      <div id="repair-scroll-body" style="flex:1 1 auto;overflow-y:auto;min-height:40px;max-height:calc(80vh - 130px);padding-right:4px">
        <div style="font-size:12px;color:var(--text-dim);margin:4px 0">勾选 = 用写作模型重构修复（保持文风）；不勾选 = 立即标记通过</div>
        ${rows}
      </div>
      <div style="display:flex;gap:8px;margin-top:8px;padding-top:6px;border-top:1px solid var(--border);background:var(--bg-card)">
        <button class="btn btn-sm btn-secondary" onclick="skipAllRepair('${chapter}')" title="确认该章所有全文质检问题都不修复，标记通过，不再弹出">全部跳过</button>
        <button class="btn btn-sm btn-primary" onclick="applyFullRepair('${chapter}')">开始修复</button>
      </div>
      <div id="repair-status" style="font-size:12px;color:var(--text-dim);margin-top:6px"></div>`;
    modal.style.display = 'flex';
    return;
  }
  // P4 自动模式：配置 auto_repair=on → 不弹面板，直接全选自动修复
  if (novelChecksConfig && novelChecksConfig.auto_repair) {
    _repairMode = 'auto';  // 自动循环修复：重检有问题继续自动修，直到通过或超次数
    fetch(`/api/novel/repair/preview?session_id=${encodeURIComponent(currentSessionId)}&chapter=${encodeURIComponent(chapter)}`)
      .then(r => r.json()).then(d => {
        if (!d.success || !d.preview) return;
        const files = d.preview.files || [];
        if (!files.length) return;
        addAssistantMsg('⚡ 自动修复模式：' + chapter + ' 检出 ' + files.length + ' 个子结构问题，开始自动重构...');
        fetch('/api/novel/repair/apply', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({session_id: currentSessionId, chapter, checked_subs: files, mode: 'auto'})
        }).then(r2 => r2.json()).then(d2 => {
          if (d2.success) {
            _repairPollTimer = setInterval(() => pollRepairStatus(chapter), 5000);
          } else {
            addAssistantMsg('❌ 自动修复启动失败：' + (d2.error || ''));
          }
        }).catch(e => addAssistantMsg('❌ 自动修复启动失败：' + e.message));
      }).catch(() => {});
    return;
  }
  _repairMode = 'manual';
  renderRepairPreview(chapter);
}

// 拉 preview 渲染修复面板（手动模式首次弹出 + 重检后刷新共用）
let _repairPv = null;  // 当前修复面板的 preview 数据缓存（HARD/SOFT 过滤重渲染复用）

function renderRepairPreview(chapter) {
  const modal = document.getElementById('novel-repair-modal');
  const panel = document.getElementById('novel-repair-panel');
  if (!modal || !panel) return;
  fetch(`/api/novel/repair/preview?session_id=${encodeURIComponent(currentSessionId)}&chapter=${encodeURIComponent(chapter)}`)
    .then(r => r.json()).then(d => {
      if (!d.success || !d.preview) { modal.style.display = 'none'; return; }
      _repairPv = d.preview;
      renderRepairBody();
      panel.style.display = 'flex';
      modal.style.display = 'flex';
    }).catch(() => {});
}

// HARD/SOFT 级别过滤重渲染（完全隐藏未勾选级别的问题子结构）：
// 只勾 HARD → SOFT 问题行被滤掉 → 全 SOFT 的子结构从列表消失（防错误勾选）
function renderRepairBody() {
  const panel = document.getElementById('novel-repair-panel');
  const pv = _repairPv;
  if (!panel || !pv) return;
  const wantHard = document.getElementById('rp-filter-hard') ? document.getElementById('rp-filter-hard').checked : true;
  const wantSoft = document.getElementById('rp-filter-soft') ? document.getElementById('rp-filter-soft').checked : true;
  const t0Lines = (pv.issues || []).filter(l => l.includes('末行') || l.includes('禁用模式'));
  const t1Lines = (pv.issues || []).filter(l => !l.includes('末行') && !l.includes('禁用模式'));
  // 级别过滤：行内 [HARD]/[SOFT]（preview 格式 `S01.txt: [HARD] problem`）决定归属；未勾选的级别整行滤掉
  const filtered = t1Lines.filter(l => {
    const m = l.match(/\[(HARD|SOFT|WARN|FAIL)\]/);
    const sev = m ? m[1] : '';
    if (sev === 'HARD' || sev === 'FAIL') return wantHard;
    if (sev === 'SOFT' || sev === 'WARN') return wantSoft;
    return true;  // 无级别标记（罕见）→ 保留
  });
  // 按文件聚合（只聚合 pv.files 里已有的文件；过滤后无问题的文件不生成条目 → 完全隐藏）
  const fileMap = {};
  filtered.forEach(l => {
    const m = l.match(/S\d+\.txt/);
    const f = m ? m[0] : '';
    if (f && (pv.files || []).includes(f)) {
      if (!fileMap[f]) fileMap[f] = [];
      fileMap[f].push(l.replace(/^S\d+\.txt:\s*/, '').replace(/^\[(HARD|SOFT|WARN|FAIL)\]\s*/, '').trim());
    }
  });
  const subRows = Object.keys(fileMap).map(f => {
    const probs = (fileMap[f] || []).slice(0, 3).map(p => `<div style="font-size:11px;color:var(--text-dim);margin-left:26px">• ${escapeHtml(p.slice(0, 60))}</div>`).join('');
    return `<label style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;border-bottom:1px solid var(--border)">
      <input type="checkbox" class="rp-check" data-file="${f}" checked>
      <span style="font-size:12px;flex:1">${f.replace('.txt','')}</span>
    </label>${probs}`;
  }).join('');
  const t0Msg = t0Lines.length ? `<div style="font-size:12px;color:#2ecc71;margin:2px 0">⚡ T0 已自动修复：${t0Lines.length} 处格式问题</div>` : '';
  const chOnlyMsg = (pv.chapter_only && pv.chapter_only.length) ? `<div style="font-size:12px;color:#e94560;margin:6px 0;padding:6px 8px;background:var(--bg-input);border-radius:4px;border-left:3px solid #e94560">⚠️ 章级问题（无法整段重构，需人工处理或全部跳过）：${pv.chapter_only.map(escapeHtml).join('；')}</div>` : '';
  panel.innerHTML = `<div style="font-size:13px;font-weight:500;margin-bottom:4px">🔧 ${pv.chapter} 六检结果：${pv.ok ? '通过' : (pv.timeout ? '超时' : '需修复')}（HARD/SOFT ${t1Lines.length + (pv.chapter_only ? pv.chapter_only.length : 0)} 条）</div>
    <div style="font-size:12px;color:var(--text-dim);margin:4px 0">级别过滤（未勾选级别的问题子结构完全隐藏，防错误勾选）：
      <label style="cursor:pointer;margin-left:6px"><input type="checkbox" id="rp-filter-hard" ${wantHard ? 'checked' : ''} onchange="renderRepairBody()"> HARD</label>
      <label style="cursor:pointer;margin-left:10px"><input type="checkbox" id="rp-filter-soft" ${wantSoft ? 'checked' : ''} onchange="renderRepairBody()"> SOFT</label>
      <span style="margin-left:10px;color:var(--text-dim)">当前显示 ${Object.keys(fileMap).length} 个子结构</span>
    </div>
    ${t0Msg}
    ${chOnlyMsg}
    <div id="repair-scroll-body" style="flex:1 1 auto;overflow-y:auto;min-height:40px;max-height:calc(80vh - 130px);padding-right:4px">
      <div style="font-size:12px;color:var(--text-dim);margin:4px 0">选择要修复的子结构（勾掉 = 跳过，写作模型整段重构，字数±15%）</div>
      ${subRows || '<div style="font-size:12px;color:var(--text-dim)">（当前级别过滤下无可重构的子结构）</div>'}
    </div>
    <div style="display:flex;gap:8px;margin-top:8px;padding-top:6px;border-top:1px solid var(--border);background:var(--bg-card)">
      <button class="btn btn-sm btn-secondary" onclick="skipAllRepair('${pv.chapter}')" title="确认该章所有检出问题都不修复，标记通过，不再弹出">全部跳过</button>
      <button class="btn btn-sm btn-primary" onclick="applyRepair('${pv.chapter}')">开始修复</button>
    </div>
    <div id="repair-status" style="font-size:12px;color:var(--text-dim);margin-top:6px"></div>`;
}

function closeRepairPanel() {
  const modal = document.getElementById('novel-repair-modal');
  if (modal) modal.style.display = 'none';
  if (_repairPollTimer) { clearInterval(_repairPollTimer); _repairPollTimer = null; }
  _repairPanelChapter = null;
}

function skipAllRepair(chapter) {
  // 全部跳过：确认该章所有检出问题都不修复 → 后端标记通过（_repaired=True），不再弹
  const modal = document.getElementById('novel-repair-modal');
  fetch('/api/novel/repair/skip', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({session_id: currentSessionId, chapter})
  }).then(r => r.json()).then(d => {
    if (d.success) {
      if (modal) modal.style.display = 'none';
      if (_repairPollTimer) { clearInterval(_repairPollTimer); _repairPollTimer = null; }
      _repairPanelChapter = null;
      addAssistantMsg('✅ ' + chapter + ' 全部跳过：检出问题已确认不修复，标记通过');
    } else {
      addAssistantMsg('❌ 跳过失败：' + (d.error || ''));
    }
  }).catch(e => addAssistantMsg('❌ 请求失败：' + e.message));
}

function applyRepair(chapter) {
  const checked = [...document.querySelectorAll('.rp-check:checked')].map(cb => cb.dataset.file);
  const stEl = document.getElementById('repair-status');
  if (!checked.length) { stEl.textContent = '未选择任何子结构'; return; }
  _repairMode = 'manual';  // 手动：重检有问题刷新面板让人再点（无上限）
  stEl.textContent = '⏳ 修复中（写作模型重构 + 重检，每段 3-10 分钟）...';  fetch('/api/novel/repair/apply', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({session_id: currentSessionId, chapter, checked_subs: checked, mode: 'manual'})
  }).then(r => r.json()).then(d => {
    if (!d.success) { stEl.textContent = '❌ ' + (d.error || '启动失败'); return; }
    _repairPollTimer = setInterval(() => pollRepairStatus(chapter), 4000);
  }).catch(e => { stEl.textContent = '❌ ' + e.message; });
}

function applyFullRepair(chapter) {
  // 三检修复项：勾选带 data-file + data-type → 后端类型化重构（fidelity/pledge/ending）
  const items = [...document.querySelectorAll('.rp-check:checked')].map(cb => ({file: cb.dataset.file, type: cb.dataset.type}));
  const stEl = document.getElementById('repair-status');
  if (!items.length) { stEl.textContent = '未选择任何修复项'; return; }
  _repairMode = 'manual';
  stEl.textContent = '⏳ 修复中（写作模型类型化重构 + 平滑衔接，可能需要几分钟）...';
  fetch('/api/novel/repair/apply', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      session_id: currentSessionId, chapter,
      checked_subs: items.map(x => x.file),
      full_types: items.map(x => x.type),
      mode: 'manual'
    })
  }).then(r => r.json()).then(d => {
    if (!d.success) { stEl.textContent = '❌ ' + (d.error || '启动失败'); return; }
    _repairPollTimer = setInterval(() => pollRepairStatus(chapter), 5000);
  }).catch(e => { stEl.textContent = '❌ ' + e.message; });
}

function pollRepairStatus(chapter) {
  fetch(`/api/novel/repair/status?session_id=${encodeURIComponent(currentSessionId)}`).then(r => r.json()).then(d => {
    if (!d.success) return;
    const st = d.state || {};
    if (!st.done) { const el = document.getElementById('repair-status'); if (el) el.textContent = '⏳ 修复中...'; return; }
    clearInterval(_repairPollTimer); _repairPollTimer = null;
    const el = document.getElementById('repair-status');
    if (!el) return;
    const res = st.result || {};
    if (res.error) { el.textContent = '❌ 修复失败：' + res.error; _autoRecheckRound = 0; return; }
    const t1 = (res.t1 && res.t1.results) || [];
    const okN = t1.filter(x => x.status === 'rewritten').length;
    const failN = t1.filter(x => x.status === 'failed').length;
    const maxRounds = (novelChecksConfig && novelChecksConfig.repair_rounds) || 3;
    _autoRecheckRound++;
    const base = `修复完成：重写 ${okN} 段${failN ? '，失败 ' + failN + ' 段（已保留原稿）' : ''}`;
    let statusColor = failN ? '#e94560' : '#2ecc71';
    let failDetail = '';
    if (failN) {
      failDetail = t1.filter(x => x.status === 'failed')
        .map(x => `${x.file}${x.problems && x.problems.length ? ': ' + x.problems.join(';') : ''}`).join(' | ');
    }
    if (_repairMode === 'manual') {
      // 手动：重检一次，有问题刷新面板让人再点（无上限）
      el.textContent = `🔄 ${base}${failDetail ? '（' + failDetail + '）' : ''}。自动重检中...`;
      el.style.color = statusColor;
      triggerRecheck(chapter, 0);
    } else {
      // 自动：循环修复到通过或超次数
      el.textContent = `🔄 ${base}${failDetail ? '（' + failDetail + '）' : ''}。自动重检中 (${_autoRecheckRound}/${maxRounds})...`;
      el.style.color = statusColor;
      triggerRecheck(chapter, maxRounds);
    }
  }).catch(() => {});
}

// 修复完成后自动重检（手动/自动共用）：
// 手动：有问题 → 刷新面板显示本次问题（无上限，全凭人）；无问题 → 关闭界面走下一章
// 自动：有问题 & 未超轮次 → 继续 apply 全量修复；无问题 → 关闭；超轮次 → 保留让用户处理
function triggerRecheck(chapter, maxRounds) {
  const modal = document.getElementById('novel-repair-modal');
  if (!modal || modal.style.display === 'none' || _repairPanelChapter !== chapter) {
    // 已被用户跳过/全部跳过/关闭，重置轮次
    _autoRecheckRound = 0;
    return;
  }
  fetch(`/api/novel/repair/preview?session_id=${encodeURIComponent(currentSessionId)}&chapter=${encodeURIComponent(chapter)}`)
    .then(r => r.json()).then(d => {
      const el = document.getElementById('repair-status');
      if (!d.success || !d.preview) {
        // 拿不到预览 = _repaired=True 已清空 issues → 全通过
        _autoRecheckRound = 0;
        if (el) el.textContent = '✅ 自动重检：全通过，章节已合格';
        if (modal) modal.style.display = 'none';
        _repairPanelChapter = null;
        return;
      }
      const files = d.preview.files || [];
      const chapterOnly = d.preview.chapter_only || [];
      if (!files.length && !chapterOnly.length) {
        _autoRecheckRound = 0;
        if (el) el.textContent = '✅ 自动重检：全通过，章节已合格';
        if (modal) modal.style.display = 'none';
        _repairPanelChapter = null;
        return;
      }
      if (_repairMode === 'manual') {
        // 手动：重检还有问题 → 界面不关，刷新为本次问题与勾选（无上限，全凭人点不点）
        _autoRecheckRound = 0;
        renderRepairPreview(chapter);
        return;
      }
      // 自动模式：还有问题
      if (_autoRecheckRound >= maxRounds) {
        if (el) el.textContent = `⚠️ 已自动修复 ${maxRounds} 轮，仍有 ${files.length} 个子结构问题未解决，请人工处理或全部跳过`;
        if (el) el.style.color = '#e94560';
        _autoRecheckRound = 0;
        return;  // 保留 modal
      }
      // 继续下一轮（自动全量修复）
      if (el) el.textContent = `🔄 重检发现 ${files.length} 个子结构问题，自动继续修复 (${_autoRecheckRound + 1}/${maxRounds})...`;
      fetch('/api/novel/repair/apply', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: currentSessionId, chapter, checked_subs: files, mode: 'auto'})
      }).then(r2 => r2.json()).then(d2 => {
        if (d2.success) {
          _repairPollTimer = setInterval(() => pollRepairStatus(chapter), 5000);
        } else {
          if (el) el.textContent = '❌ 继续修复启动失败：' + (d2.error || '');
          _autoRecheckRound = 0;
        }
      }).catch(e => {
        if (el) el.textContent = '❌ 继续修复失败：' + e.message;
        _autoRecheckRound = 0;
      });
    }).catch(() => {
      const el = document.getElementById('repair-status');
      _autoRecheckRound = 0;
      if (el) el.textContent = '⚠️ 重检请求失败';
    });
}

function fetchResult(sessionId) {
  fetch(`/api/result?session_id=${sessionId}`)
    .then(r => r.json()).then(d => {
      // 重新加载会话以获取最新状态
      fetch(`/api/session/load?session_id=${sessionId}`)
        .then(r2 => r2.json()).then(d2 => {
          if (d2.success) {
            // 刷新大纲卡片状态
            const oldCard = document.getElementById('outline-card');
            if (oldCard) {
              const container = oldCard.closest('.msg');
              if (container) {
                // 用只读模式刷新大纲显示完成状态
                const readOnlyHTML = buildOutlineHTML(d2.session.outline, true);
                container.querySelector('.msg-content').innerHTML = readOnlyHTML;
              }
            }
          }
        });

      // 显示结果
      let resultMsg = '';
      if (d.success) {
        resultMsg = `✅ 写作完成！总字数：${d.word_count || 0} 字`;
        if (d.output_file) {
          const fname = d.output_file.split('/').pop() || d.output_file.split('\\\\').pop();
          resultMsg += `\n📎 文件：${fname}`;
        }
        if (d.content) {
          resultMsg += `\n\n--- 预览 ---\n${d.content}`;
        }
        // 保存范例并生成：生成完成后自动回填文章全文
        if (_pendingExampleName && d.output_file) {
          const exName = _pendingExampleName;
          _pendingExampleName = null;
          fetch('/api/example/update_article', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({name: exName, output_file: d.output_file})
          }).then(r => r.json()).then(d2 => {
            if (d2.success) {
              addAssistantMsg('📦 范例「' + escapeHtml(exName) + '」已更新：文章已回填（' + (d2.article_chars || 0) + ' 字）');
            } else {
              addAssistantMsg('⚠️ 范例「' + escapeHtml(exName) + '」文章回填失败：' + (d2.error || ''));
            }
            loadExamples();
          }).catch(() => loadExamples());
        } else if (_pendingExampleName) {
          _pendingExampleName = null;
        }
      } else {
        resultMsg = '❌ 写作失败：' + (d.error || '');
        if (_pendingExampleName) {
          addAssistantMsg('⚠️ 范例「' + escapeHtml(_pendingExampleName) + '」仅保存了大纲（生成失败，文章未回填），可稍后重新生成再回填');
          _pendingExampleName = null;
        }
      }
      addAssistantMsg(resultMsg);
      isGenerating = false;
      loadSessions();
    });
}

// ===== 右侧已完成文章列表 =====

function loadOutputs() {
  fetch('/api/outputs').then(r => r.json()).then(d => {
    if (!d.success) return;
    const list = document.getElementById('outputs-list');
    if (!list) return;
    list.innerHTML = (d.files || []).map(f => {
      const date = new Date(f.mtime * 1000);
      const dateStr = `${date.getMonth()+1}/${date.getDate()} ${String(date.getHours()).padStart(2,'0')}:${String(date.getMinutes()).padStart(2,'0')}`;
      const name = escapeHtml(f.name);
      if (f.novel) {
        // 小说树状：题目下挂已完成章，可展开收起；父级 = 整本预览，旁有手动「拼合」
        const chItems = (f.children || []).map(c => {
          const cd = new Date(c.mtime * 1000);
          const cdStr = `${cd.getMonth()+1}/${cd.getDate()} ${String(cd.getHours()).padStart(2,'0')}:${String(cd.getMinutes()).padStart(2,'0')}`;
          return '<div class="output-item output-chapter" onclick="openOutput(\'' + name + 'chapters/' + escapeHtml(c.name) + '\')">' +
            '<span class="name" title="' + escapeHtml(c.name) + '">' + escapeHtml(c.name) + '</span>' +
            '<span class="date">' + cdStr + '</span></div>';
        }).join('');
        const fullBadge = f.has_full ? '<span class="img-badge" title="已拼合整本">📄</span>' : '';
        return '<div class="output-novel">' +
          '<div class="output-item" onclick="toggleNovelTree(this)">' +
          '<span class="tree-arrow">▸</span>' +
          '<span class="name" title="整本预览（动态合并已完成章）" onclick="event.stopPropagation();openOutput(\'' + name + '\')" style="cursor:pointer">' + name + '</span>' +
          '<span class="img-badge" title="已完成章数">📚 ' + (f.chapter_count || 0) + '章</span>' + fullBadge +
          '<button class="merge-btn" onclick="event.stopPropagation();mergeNovel(\'' + name + '\')" title="合并全部已完成章为整本 md">拼合</button>' +
          '<span class="date">' + dateStr + '</span>' +
          '<span class="del-btn" onclick="event.stopPropagation();deleteOutput(this,\'' + name + '\',1,0)" title="删除整篇（含全部章）">✕</span>' +
          '</div><div class="novel-children" style="display:none">' + chItems + '</div></div>';
      }
      const badge = f.is_dir && f.image_count > 0
        ? '<span class="img-badge" title="含图片">🖼 ' + f.image_count + '</span>' : '';
      const isDir = f.is_dir ? '1' : '0';
      return '<div class="output-item" onclick="openOutput(\'' + name + '\')">' +
        '<span class="name" title="' + name + '">' + name + '</span>' +
        badge +
        '<span class="date">' + dateStr + '</span>' +
        '<span class="del-btn" onclick="event.stopPropagation();deleteOutput(this,\'' + name + '\',' + isDir + ',' + (f.image_count||0) + ')" title="删除">✕</span>' +
        '</div>';
    }).join('');
    // 自动刷新
    setTimeout(loadOutputs, 30000);
  }).catch(() => setTimeout(loadOutputs, 30000));
}

// 小说树状：展开/收起已完成章列表
function toggleNovelTree(el) {
  const box = el.nextElementSibling;
  if (!box) return;
  const arrow = el.querySelector('.tree-arrow');
  if (box.style.display === 'none') {
    box.style.display = 'block';
    if (arrow) arrow.textContent = '▾';
  } else {
    box.style.display = 'none';
    if (arrow) arrow.textContent = '▸';
  }
}

// 小说手动拼合：合并全部已完成章 → 整本 md
function mergeNovel(name) {
  const btn = event && event.target ? event.target : null;
  if (btn) { btn.textContent = '拼合中...'; btn.disabled = true; }
  fetch('/api/outputs/merge', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({file: name})
  }).then(r => r.json()).then(d => {
    if (btn) { btn.textContent = '拼合'; btn.disabled = false; }
    if (d.success) {
      addAssistantMsg('✅ 已拼合整本：' + d.name + '（' + d.chapter_count + ' 章，' + (d.size||0) + ' 字节），输出列表已更新');
      loadOutputs();
    } else {
      alert('拼合失败：' + (d.error || '未知错误'));
    }
  }).catch(e => {
    if (btn) { btn.textContent = '拼合'; btn.disabled = false; }
    alert('拼合请求失败：' + e.message);
  });
}

function openOutput(name) {
  fetch('/api/outputs/read?file=' + encodeURIComponent(name))
    .then(r => r.json()).then(d => {
      if (!d.success) return;
      // 用模态框展示
      const modal = document.getElementById('output-modal') || createOutputModal();
      _texPdfName = name;
      modal.querySelector('.modal-header h3').textContent = d.name;
      const info = modal.querySelector('#texpdf-info');
      if (info) { info.style.display = 'none'; info.innerHTML = ''; info.style.color = ''; }
      const body = modal.querySelector('#output-content');
      body.innerHTML = '';  // 清空
      body.style.whiteSpace = 'pre-wrap';
      body.style.fontSize = '13px';
      body.style.lineHeight = '1.6';
      body.style.maxHeight = '70vh';
      body.style.overflowY = 'auto';
      body.textContent = d.content;
      // 附图片清单（目录文章）
      if (d.images && d.images.length) {
        const imgBox = document.createElement('div');
        imgBox.style.cssText = 'margin-top:10px;padding-top:8px;border-top:1px solid var(--border);font-size:12px;color:var(--text-dim)';
        imgBox.textContent = '本文章图片（' + d.images.length + '）：' + d.images.join('、');
        body.appendChild(imgBox);
      }
      modal.classList.add('show');
    });
}

function createOutputModal() {
  const div = document.createElement('div');
  div.className = 'modal-overlay';
  div.id = 'output-modal';
  div.innerHTML = '<div class="modal-box" style="width:80%;max-width:900px">' +
    '<div class="modal-header"><h3></h3>' +
    '<button class="modal-btn" id="btn-texpdf" onclick="genTexPdf()" style="margin-right:8px;padding:3px 10px;font-size:12px;border:1px solid var(--border);border-radius:4px;background:var(--bg-card);color:var(--text);cursor:pointer">生成 tex+pdf</button>' +
    '<button class="modal-close" onclick="this.closest(\'.modal-overlay\').classList.remove(\'show\')">&times;</button></div>' +
    '<div class="modal-body">' +
    '<div id="texpdf-info" style="display:none;margin-bottom:10px;padding:8px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;font-size:12px;line-height:1.7;word-break:break-all"></div>' +
    '<div id="output-content"></div></div></div>';
  document.body.appendChild(div);
  return div;
}

let _texPdfName = null;
function genTexPdf() {
  if (!_texPdfName) return;
  const btn = document.getElementById('btn-texpdf');
  btn.disabled = true;
  btn.textContent = '生成中…（首次可能需装 LaTeX）';
  fetch('/api/outputs/texpdf?file=' + encodeURIComponent(_texPdfName))
    .then(r => r.json()).then(d => {
      btn.disabled = false;
      const info = document.getElementById('texpdf-info');
        if (d.success) {
        btn.textContent = '已生成 tex+pdf';
        if (info) {
          info.style.display = 'block';
          info.innerHTML = '<b>tex：</b>' + escapeHtml(d.tex) + '<br><b>pdf：</b>' + escapeHtml(d.pdf) +
            (d.install_msg ? '<br>' + escapeHtml(d.install_msg) : '');
        }
      } else {
        btn.textContent = '生成 tex+pdf';
        if (info) {
          info.style.display = 'block';
          info.style.color = '#e74c3c';
          info.innerHTML = '生成失败：' + escapeHtml((d.error || d.message || '未知错误').slice(0, 300));
        }
      }
    }).catch(e => {
      btn.disabled = false;
      btn.textContent = '生成 tex+pdf';
      const info = document.getElementById('texpdf-info');
      if (info) {
        info.style.display = 'block';
        info.style.color = '#e74c3c';
        info.innerHTML = '生成失败：网络错误（' + escapeHtml(String(e)) + '）';
      }
    });
}

function deleteOutput(btn, name, isDir, imageCount) {
  // 二次确认：第一次点击变为确认态（目录文章提示删整个文件夹）
  if (btn.dataset.confirming !== 'true') {
    btn.dataset.confirming = 'true';
    btn.dataset.isDir = isDir || '0';
    btn.dataset.imgCount = imageCount || 0;
    btn.textContent = '确认?';
    btn.style.background = '#e74c3c';
    btn.style.color = '#fff';
    if (isDir === 1 || isDir === '1') {
      btn.title = '将删除整篇文章文件夹（含 ' + (imageCount || 0) + ' 张图片），不可恢复';
    }
    // 添加取消按钮
    const cancel = document.createElement('span');
    cancel.className = 'del-cancel';
    cancel.textContent = '取消';
    cancel.onclick = function(e) {
      e.stopPropagation();
      btn.dataset.confirming = 'false';
      btn.textContent = '✕';
      btn.style.background = '';
      btn.style.color = '';
      btn.title = '删除';
      cancel.remove();
    };
    btn.parentNode.insertBefore(cancel, btn.nextSibling);
    return;
  }
  // 第二次点击：直接执行删除（内联确认已足够，不再弹 confirm 弹窗）
  fetch('/api/outputs/delete', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({file: name})
  }).then(r => r.json()).then(d => {
    if (d.success) loadOutputs();
  });
}
</script>

<!-- 辅助知识模态框 -->
<div class="modal-overlay" id="aux-modal">
  <div class="modal-box">
    <div class="modal-header">
      <h3>辅助知识</h3>
      <button class="modal-close" onclick="closeAuxModal()">&times;</button>
    </div>
    <div class="modal-body">
      <label style="font-size:12px;color:var(--text-dim);display:block;margin-bottom:4px">使用指令（作用于文字/表格资料；图片自动插图至本子结构末尾，不受指令控制）：</label>
      <textarea id="aux-text-input" placeholder="如：必须真实采用以下资料进行营收分析（图片无需指令，默认插在末尾）"></textarea>
      <div class="file-upload-area" onclick="document.getElementById('aux-file-input').click()">
        + 上传资料（表格 .csv/.db ｜ 文字 .txt/.md ｜ 图片 .png/.jpg）
      </div>
      <input type="file" id="aux-file-input" accept=".csv,.db,.txt,.md,.png,.jpg,.jpeg,.gif" style="display:none" multiple onchange="onAuxFilesSelected(event)">
      <div class="file-list" id="aux-file-list"></div>
      <div class="plugin-section" style="margin-top:12px;border-top:1px solid var(--border);padding-top:10px">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px">数据源插件（对接 db/csv 取数，取什么由上方使用指令决定）：</div>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="plugin-select" style="flex:1;min-width:120px;background:var(--bg-input);border:1px solid var(--border);border-radius:3px;color:var(--text);padding:4px 6px;font-size:12px" onchange="renderPluginForm()">
            <option value="">（选择插件）</option>
          </select>
          <button class="btn btn-sm btn-secondary" id="plugin-run-btn" style="flex-shrink:0" onclick="runPlugin()" disabled>执行取数</button>
        </div>
        <div id="plugin-fields" style="margin-top:6px"></div>
        <div id="plugin-result" style="margin-top:6px"></div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeAuxModal()">取消</button>
      <button class="btn btn-primary" onclick="saveAuxModal()">保存</button>
    </div>
  </div>
</div>

</body>
</html>"""


def run_server(host="0.0.0.0", port=8770):
    """启动 HTTP 服务器"""
    cfg = ConfigManager()
    StructuredWriterHandler.config_mgr = cfg
    server = ThreadingHTTPServer((host, port), StructuredWriterHandler)
    print(f"[Structured Writer] 服务启动: http://{host}:{port}")
    print(f"[Structured Writer] 配置面板: http://localhost:{port} (配置Tab)")
    print(f"[Structured Writer] 写作界面: http://localhost:{port} (对话Tab)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Structured Writer] 服务停止")
        server.server_close()
