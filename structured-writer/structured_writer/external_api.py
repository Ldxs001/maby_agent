"""external_api — structured-writer 对外写作 API（仿 rag-assistant 8767 模式）

端口：`--api-port`（默认 8777），与 Web UI(8770) 完全隔离。
核心：POST /api/write 同步写作 —— 传提示词/模板/图片 → 写作管道（两级 RAG）→ md/latex/pdf。

端点：
  GET  /api/health           服务状态 + 版本
  GET  /api/capabilities     模板列表 / format 列表 / RAG 可达性
  GET  /api/rag/status       RAG 可达性（只探测，不启动）
  POST /api/write            同步写作（核心）

统一响应：{"success": bool, ...}；错误 body 带 error 字段（非 2xx）。
写作管道串行承前启后不变（writer.generate_article 原样复用，本模块只做编排）。
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config_manager import ConfigManager
from .llm_client import LLMClient, LLMClientError
from .md2tex import md_to_tex
from .planner import plan_outline, generate_template
from .rag_client import RAGClient, RAGClientError
from .state_manager import StateManager, SESSIONS_DIR
from .writer import generate_article

# ── 常量 ────────────────────────────────────────────────
DEFAULT_PORT = 8777
RAG_BASE_URL = "http://localhost:8767"
# 冷启动时 rag-assistant 的本地目录（外部拉起其进程，不改其代码）
RAG_DIR = Path.home() / "WorkBuddy" / "rag-assistant"

_PROMPT_MAX = 2000
_INSTR_MAX = 1000
_TEMPLATE_DESC_MAX = 500
_IMAGE_MAX_BYTES = 20 * 1024 * 1024   # 单张 20MB
_IMAGE_MAX_COUNT = 20
_IMG_EXTS = ("png", "jpg", "jpeg", "gif")

_XELATEX_CANDIDATES = [
    r"C:\Program Files\MiKTeX\miktex\bin\x64",
    r"C:\Program Files (x86)\MiKTeX\miktex\bin",
    str(Path.home() / r"AppData\Local\Programs\MiKTeX\miktex\bin\x64"),
]


def _find_engine() -> str:
    """查找 xelatex（PATH 优先，其次 MiKTeX 常见路径）"""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d, "xelatex.exe")
        if os.path.exists(cand):
            return cand
    for p in _XELATEX_CANDIDATES:
        cand = os.path.join(p, "xelatex.exe")
        if os.path.exists(cand):
            return cand
    return ""


def _rag_online(timeout: float = 3.0) -> bool:
    """探测 rag-assistant 8767 是否在线"""
    try:
        req = urllib.request.Request(f"{RAG_BASE_URL}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
            return bool(d.get("success")) and d.get("status") == "running"
    except Exception:
        return False


def _cold_start_rag(timeout: float = 90.0) -> tuple:
    """拉起 rag-assistant 子进程并等待就绪。

    返回: (ok, reason)
    """
    if not RAG_DIR.exists() or not (RAG_DIR / "main.py").exists():
        return False, f"rag-assistant 目录不存在: {RAG_DIR}"
    try:
        subprocess.Popen(
            [sys.executable, "main.py", "--no-web", "--api-port", "8767"],
            cwd=str(RAG_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return False, f"拉起 rag-assistant 失败: {e}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _rag_online(timeout=2.0):
            return True, ""
        time.sleep(3)
    return False, f"冷启动超时（{int(timeout)}s），RAG 服务未就绪"


class ExternalAPIHandler(BaseHTTPRequestHandler):
    """对外 API HTTP Handler（仿 rag-assistant 8767 风格）"""

    protocol_version = "HTTP/1.1"
    config_mgr = None  # 类级共享

    # ── 基础 ──────────────────────────────────────────────
    def log_message(self, fmt, *args):
        pass  # 抑制默认日志

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, **kw):
        self._send(200, {"success": True, **kw})

    def _err(self, error: str, code: int = 400):
        self._send(code, {"success": False, "error": error})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"请求体 JSON 解析失败: {e}")

    # ── LLM 客户端工厂（与 web_ui 同配置） ────────────────
    def _planner_client(self) -> LLMClient:
        pm = self.config_mgr.get("planner_model", {})
        return LLMClient(
            backend=pm.get("backend", "lmstudio"),
            base_url=pm.get("base_url", "http://localhost:1234"),
            timeout=pm.get("timeout", 180),
            model=pm.get("model", ""),
            max_tokens=pm.get("max_tokens", 4096),
            temperature=pm.get("temperature", 0.6),
            # n_ctx 不传：LLMClient 按 max_tokens 自动推导（同一设置）
        )

    def _writer_client(self) -> LLMClient:
        wm = self.config_mgr.get("writer_model", {})
        return LLMClient(
            backend=wm.get("backend", "lmstudio"),
            base_url=wm.get("base_url", "http://localhost:1234"),
            timeout=wm.get("timeout", 300),
            model=wm.get("model", ""),
            max_tokens=wm.get("max_tokens", 8192),
            temperature=wm.get("temperature", 0.7),
            # n_ctx 不传：LLMClient 按 max_tokens 自动推导（同一设置）
        )

    def _template_names(self) -> list:
        builtin = self.config_mgr.get("templates", {})
        user = self.config_mgr._load_user_templates()
        return sorted(set(builtin.keys()) | set(user.keys()))

    # ── 路由 ──────────────────────────────────────────────
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._handle_health()
            elif path == "/api/capabilities":
                self._handle_capabilities()
            elif path == "/api/rag/status":
                self._handle_rag_status()
            elif path == "/api/novel/projects":
                self._handle_novel_projects()
            elif path == "/api/novel/status":
                self._handle_novel_status(query)
            elif path == "/api/novel/extract":
                self._handle_novel_extract(query)
            elif path == "/api/novel/export":
                self._handle_novel_export(query)
            else:
                self._err(f"未知 GET 路径: {path}", 404)
        except ValueError as e:
            self._err(str(e), 404 if "项目不存在" in str(e) else 400)
        except Exception as e:
            self._err(f"处理异常: {e}", 500)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        try:
            body = self._read_body()
        except ValueError as e:
            self._err(str(e), 400)
            return
        try:
            if path == "/api/write":
                self._handle_write(body)
            elif path == "/api/novel/init":
                self._handle_novel_init(body)
            elif path == "/api/novel/plan":
                self._handle_novel_plan(body)
            elif path == "/api/novel/plan_chapter":
                self._handle_novel_plan_chapter(body)
            elif path == "/api/novel/write":
                self._handle_novel_write(body)
            elif path == "/api/novel/write_sub":
                self._handle_novel_write_sub(body)
            elif path == "/api/novel/check":
                self._handle_novel_check(body)
            elif path == "/api/novel/check_full":
                self._handle_novel_check_full(body)
            elif path == "/api/novel/repair":
                self._handle_novel_repair(body)
            elif path == "/api/novel/replan":
                self._handle_novel_replan(body)
            else:
                self._err(f"未知 POST 路径: {path}", 404)
        except ValueError as e:
            self._err(str(e), 404 if "项目不存在" in str(e) else 400)
        except Exception as e:
            self._err(f"处理异常: {e}", 500)

    # ── 端点实现 ──────────────────────────────────────────
    def _handle_health(self):
        try:
            from . import __version__
            ver = __version__
        except Exception:
            ver = "unknown"
        self._ok(status="running", version=ver)

    def _handle_capabilities(self):
        self._ok(
            templates=self._template_names(),
            formats=["md", "latex", "pdf"],
            rag_online=_rag_online(),
            rag_base_url=RAG_BASE_URL,
        )

    def _handle_rag_status(self):
        self._ok(
            online=_rag_online(),
            base_url=RAG_BASE_URL,
            cold_startable=str(RAG_DIR) if RAG_DIR.exists() else "",
        )

    # ── 小说线（novel）对外 API ───────────────────────────
    # 双线并存：/api/write（通用线）+ /api/novel/*（小说线）。
    # 最小侵入：全部复用 novel_bridge / novel_writer / novel_repair_engine 已有封装
    #（Web 安全场景），external_api 只做路由与参数解析——novel 侧零改动。
    @staticmethod
    def _novel_state_path(name: str) -> str:
        from pathlib import Path as _P
        return str(_P(__file__).resolve().parent.parent / "data" / "novel" / "projects"
                   / name / "data" / "novel_state.json")

    def _novel_project_names(self) -> list:
        from pathlib import Path as _P
        base = _P(__file__).resolve().parent.parent / "data" / "novel" / "projects"
        if not base.is_dir():
            return []
        return sorted(d.name for d in base.iterdir()
                      if (d / "data" / "novel_state.json").is_file())

    def _novel_check_project(self, name: str) -> str:
        """校验项目存在，返回 state_path；不存在抛 404 语义。"""
        from pathlib import Path as _P
        sp = self._novel_state_path(name)
        if not _P(sp).is_file():
            raise ValueError(f"项目不存在: {name}")
        return sp

    # GET /api/novel/projects — 项目列表
    def _handle_novel_projects(self):
        self._ok(projects=self._novel_project_names())

    # GET /api/novel/status?name= — 项目状态（章/子结构/实体/时间线摘要）
    def _handle_novel_status(self, query: dict):
        name = (query.get("name") or [""])[0].strip()
        if not name:
            raise ValueError("缺少 name 参数")
        sp = self._novel_check_project(name)
        from .novel.novel_bridge import novel_project_state
        self._ok(project=name, state=novel_project_state(sp))

    # POST /api/novel/init {project_id?, topic, length?, chapters?, characters?}
    def _handle_novel_init(self, body: dict):
        topic = str(body.get("topic", "")).strip()
        if not topic:
            raise ValueError("topic 不能为空")
        from .novel.novel_bridge import init_novel_project
        sp = init_novel_project(
            str(body.get("project_id") or "auto"),
            topic,
            body.get("length", "medium"),
            body.get("chapters"),
            body.get("characters"),
        )
        from pathlib import Path as _P
        self._ok(state_path=sp, project=_P(sp).parent.parent.name)

    # POST /api/novel/plan {topic, template?, meta?} → {plan: {outline, setting, state_path, causality_issues}}
    def _handle_novel_plan(self, body: dict):
        topic = str(body.get("topic", "")).strip()
        if not topic:
            raise ValueError("topic 不能为空")
        template = self._resolve_template(body, self._planner_client())
        user_meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        from .novel.novel_bridge import plan_novel_outline
        r = plan_novel_outline(topic, template, user_meta, self._planner_client())
        self._ok(plan=r)

    # POST /api/novel/plan_chapter {project, chapter, template?, hints?} — 单章子结构规划
    def _handle_novel_plan_chapter(self, body: dict):
        name = str(body.get("project", "")).strip()
        chapter = str(body.get("chapter", "")).strip()
        if not name or not chapter:
            raise ValueError("缺少 project/chapter")
        sp = self._novel_check_project(name)
        template = self._resolve_template(body, self._planner_client())
        from .novel.novel_bridge import plan_chapter_subs
        r = plan_chapter_subs(sp, chapter, template, self._planner_client(),
                              hints=str(body.get("hints", "")))
        self._ok(result=r)

    # POST /api/novel/write {outline?|topic?, template?, rag?, checks?} → 整本写作（同步阻塞）
    def _handle_novel_write(self, body: dict):
        from .state_manager import StateManager
        from .novel.novel_bridge import plan_novel_outline
        from .novel.novel_writer import generate_novel_article
        outline = body.get("outline")
        template = body.get("template")
        if not (isinstance(outline, dict) and outline.get("sections")):
            # topic 模式：内部 init+plan → outline
            topic = str(body.get("topic", "")).strip()
            if not topic:
                raise ValueError("需要 outline（含 _novel.sections）或 topic")
            tpl = self._resolve_template(body, self._planner_client())
            r = plan_novel_outline(topic, tpl, body.get("meta") or {}, self._planner_client())
            outline = r["outline"]
        if isinstance(template, str):
            template = self._resolve_template(body, self._planner_client())
        state_mgr = StateManager()
        md, out_path = generate_novel_article(
            outline=outline,
            user_orders=None,
            rag_options=None,
            llm_client=self._writer_client(),
            state_mgr=state_mgr,
            template=template if isinstance(template, dict) else None,
            stop_check=None,
            rag_client=None,
            aux_knowledge=None,
        )
        self._ok(md=md, output_path=out_path)

    # POST /api/novel/write_sub {project, chapter, sub, body} — 单子结构落盘
    def _handle_novel_write_sub(self, body: dict):
        name = str(body.get("project", "")).strip()
        chapter = str(body.get("chapter", "")).strip()
        sub = str(body.get("sub", "")).strip()
        text = str(body.get("body", "")).strip()
        if not name or not chapter or not sub:
            raise ValueError("缺少 project/chapter/sub")
        if not text:
            raise ValueError("body 不能为空")
        sp = self._novel_check_project(name)
        from .novel.novel_bridge import write_novel_sub
        r = write_novel_sub(sp, chapter, sub, text)
        self._ok(result=r)

    # POST /api/novel/check {project, chapter, checks?} — 章检（六检）
    def _handle_novel_check(self, body: dict):
        name = str(body.get("project", "")).strip()
        chapter = str(body.get("chapter", "")).strip()
        if not name or not chapter:
            raise ValueError("缺少 project/chapter")
        sp = self._novel_check_project(name)
        from .novel.novel_bridge import finalize_novel_chapter
        fc = finalize_novel_chapter(sp, chapter, checks=body.get("checks"))
        self._ok(ok=bool(fc.get("ok")), issues=fc.get("issues", []),
                 timeout=bool(fc.get("timeout")))

    # POST /api/novel/check_full {project, checks?} — 全文三检
    def _handle_novel_check_full(self, body: dict):
        name = str(body.get("project", "")).strip()
        if not name:
            raise ValueError("缺少 project")
        sp = self._novel_check_project(name)
        from .novel.novel_bridge import finalize_novel_full
        fc = finalize_novel_full(sp, checks=body.get("checks"))
        self._ok(ok=bool(fc.get("ok")), issues=fc.get("issues", []),
                 timeout=bool(fc.get("timeout")))

    # POST /api/novel/repair {project, chapter, issues, checked_subs?, repair_types?, mode?}
    def _handle_novel_repair(self, body: dict):
        name = str(body.get("project", "")).strip()
        chapter = str(body.get("chapter", "")).strip()
        if not name or not chapter:
            raise ValueError("缺少 project/chapter")
        sp = self._novel_check_project(name)
        from pathlib import Path as _P
        chapter_dir = str(_P(sp).parent.parent / "chapters" / chapter)
        issues = body.get("issues") or []
        if not isinstance(issues, list):
            raise ValueError("issues 必须是数组")
        from .novel import novel_repair_engine as reng
        rep = reng.run(sp, chapter_dir, chapter, issues,
                       mode=str(body.get("mode", "manual")),
                       config_mgr=self.config_mgr,
                       checked_subs=body.get("checked_subs"),
                       repair_types=body.get("repair_types"))
        self._ok(report=rep)

    # POST /api/novel/replan {project, chapter, sub?, hints?} — 重规划（章/子结构）
    def _handle_novel_replan(self, body: dict):
        name = str(body.get("project", "")).strip()
        chapter = str(body.get("chapter", "")).strip()
        if not name or not chapter:
            raise ValueError("缺少 project/chapter")
        sp = self._novel_check_project(name)
        hints = str(body.get("hints", ""))
        client = self._planner_client()
        from .novel.novel_bridge import replan_novel_chapter, replan_novel_sub
        sub = str(body.get("sub", "")).strip()
        if sub:
            r = replan_novel_sub(sp, chapter, sub, hints, client)
        else:
            r = replan_novel_chapter(sp, chapter, hints, client)
        self._ok(result=r)

    # GET /api/novel/extract?name= — 实体/关系/时间线/行为提取状态
    def _handle_novel_extract(self, query: dict):
        name = (query.get("name") or [""])[0].strip()
        if not name:
            raise ValueError("缺少 name 参数")
        sp = self._novel_check_project(name)
        from pathlib import Path as _P
        import json as _json
        try:
            d = _json.loads(_P(sp).read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise ValueError(f"读取项目状态失败: {e}")
        et = d.get("entity_tracker") or {}
        self._ok(project=name,
                 entities=et.get("entities", []),
                 relations=et.get("relations", []),
                 pledges=d.get("pledges", []),
                 timeline=d.get("timeline", []))

    # GET /api/novel/export?name=&format=md|latex|pdf — 整本导出（现场拼 md → 可选转 latex/pdf）
    def _handle_novel_export(self, query: dict):
        name = (query.get("name") or [""])[0].strip()
        fmt = (query.get("format") or ["md"])[0].lower()
        if fmt not in ("md", "latex", "pdf"):
            raise ValueError(f"format 非法: {fmt}，可选 md/latex/pdf")
        if not name:
            raise ValueError("缺少 name 参数")
        sp = self._novel_check_project(name)
        import re as _re
        from pathlib import Path as _P
        import json as _json
        d = _json.loads(_P(sp).read_text(encoding="utf-8-sig"))
        chapters = d.get("chapters", [])
        if not chapters:
            raise ValueError("项目无章节")
        base = _P(sp).parent.parent
        title = str((d.get("meta") or {}).get("title", "") or name)
        parts = [f"# {title}\n"]
        for ch in chapters:
            ch_dir = base / "chapters" / ch["id"]
            if not ch_dir.is_dir():
                continue
            txts = sorted(ch_dir.glob("S*.txt"))
            if not txts:
                continue
            parts.append(f"\n## {ch.get('title', ch['id'])}\n")
            for t in txts:
                lines = t.read_text(encoding="utf-8-sig").rstrip("\n").split("\n")
                sub_title = lines[0].strip() if lines else t.stem
                body = "\n".join(l for l in lines[1:] if l.strip()
                                 and not l.startswith("【别名】")
                                 and not _re.match(rf'{ch["id"]}S\d+', l.strip())
                                 and not _re.match(r'L\d+ · S\d+《', l.strip()))
                parts.append(f"### {sub_title}\n\n{body}\n")
        md = "\n".join(parts)
        out_dir = _P(__file__).resolve().parent.parent / "data" / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        field, value = self._convert(md, fmt, title, str(out_dir))
        self._ok(project=name, format=fmt, **{field: value})


    # ── 模板解析（三形态） ────────────────────────────────
    def _resolve_template(self, body: dict, planner_client: LLMClient) -> dict:
        """模板解析：模板名 / 内联 JSON / template_desc 生成。缺省用 config 默认。

        返回模板 dict；失败抛 ValueError（400）。
        """
        tpl = body.get("template")
        desc = str(body.get("template_desc", "")).strip()
        if tpl is not None and desc:
            raise ValueError("template 与 template_desc 互斥，只能传一个")
        if desc:
            if len(desc) > _TEMPLATE_DESC_MAX:
                raise ValueError(f"template_desc 超过 {_TEMPLATE_DESC_MAX} 字")
            try:
                return generate_template(desc, planner_client)
            except LLMClientError as e:
                raise ValueError(f"模板生成失败: {e}")
        if isinstance(tpl, str):
            tpl_name = tpl.strip()
            builtin = self.config_mgr.get("templates", {})
            user = self.config_mgr._load_user_templates()
            t = builtin.get(tpl_name) or user.get(tpl_name)
            if not t:
                names = "、".join(sorted(set(builtin) | set(user))) or "（无）"
                raise ValueError(f"模板「{tpl_name}」不存在，可用: {names}")
            return t if isinstance(t, dict) else {"style": str(t), "content": []}
        if isinstance(tpl, dict):
            if not (tpl.get("meta") or tpl.get("content")):
                raise ValueError("内联模板必须包含 meta 或 content 字段")
            for f in tpl.get("content", []):
                if isinstance(f, dict) and f.get("type") not in (None, "leaf", "section"):
                    raise ValueError(f"content 字段 type 非法: {f.get('type')}")
            return tpl
        # 缺省：config 的 selected_template
        selected = self.config_mgr.get("selected_template", "")
        builtin = self.config_mgr.get("templates", {})
        t = builtin.get(selected)
        return t if isinstance(t, dict) else {"content": [], "style": "", "logic": ""}

    # ── images 处理 ───────────────────────────────────────
    def _save_images(self, images: list, outline: dict) -> dict:
        """base64 图片落盘 + 构建 aux_knowledge（挂到目标子结构，缺省第一个 section 节末尾）。

        返回: aux_knowledge = {ssid: {"text": "", "files": [{"name", "type": "image", "path"}]}}
        """
        aux = {}
        if not images:
            return aux
        if len(images) > _IMAGE_MAX_COUNT:
            raise ValueError(f"图片数量超过上限 {_IMAGE_MAX_COUNT}")

        # 收集大纲子结构标题 → ssid 映射（用于 target 模糊匹配）
        sub_map = {}  # 标题 → ssid
        first_section_sub = None  # 第一个 section 节的最后一个子结构
        for s in outline.get("sections", []):
            subs = s.get("sub_sections") or []
            if s.get("type") == "section" and subs and first_section_sub is None:
                first_section_sub = subs[-1]["id"]
            for sub in subs:
                sub_map[sub.get("title", "")] = sub["id"]
                sub_map[sub.get("id", "")] = sub["id"]

        aux_dir = SESSIONS_DIR / "aux"
        aux_dir.mkdir(parents=True, exist_ok=True)

        for img in images:
            if not isinstance(img, dict):
                raise ValueError("images 元素必须是对象 {name, type, base64, target?}")
            name = str(img.get("name", "")).strip()
            ftype = str(img.get("type", "")).lower()
            b64 = str(img.get("base64", "")).strip()
            target = str(img.get("target", "")).strip()
            if not name or not ftype or not b64:
                raise ValueError(f"图片 {name or '(无名)'} 缺少 name/type/base64")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ftype not in _IMG_EXTS or ftype != ext:
                raise ValueError(f"图片 {name} type({ftype}) 与扩展名不一致")
            # 解码
            raw_b64 = b64.split(",", 1)[1] if "," in b64 and b64.split(",", 1)[0].startswith("data:") else b64
            try:
                data = base64.b64decode(raw_b64, validate=True)
            except Exception:
                raise ValueError(f"图片 {name} base64 解码失败")
            if len(data) > _IMAGE_MAX_BYTES:
                raise ValueError(f"图片 {name} 超过 {_IMAGE_MAX_BYTES // 1024 // 1024}MB")
            # 目标子结构
            if target:
                ssid = None
                for key, sid in sub_map.items():
                    if target in key or key in target:
                        ssid = sid
                        break
                if ssid is None:
                    raise ValueError(
                        f"图片 target「{target}」未匹配到任何子结构，可用: {'、'.join(sub_map.keys())[:200]}"
                    )
            else:
                ssid = first_section_sub
                if ssid is None:
                    raise ValueError("大纲无 section 节可插图，请用 target 指定子结构")
            # 落盘
            fpath = aux_dir / name
            if fpath.exists():
                stem, e2 = os.path.splitext(name)
                fpath = aux_dir / f"{stem}_{int(time.time())}{e2}"
            with open(fpath, "wb") as f:
                f.write(data)
            rel = str(fpath.relative_to(SESSIONS_DIR)).replace("\\", "/")
            aux.setdefault(ssid, {"text": "", "files": []})
            aux[ssid]["files"].append({"name": fpath.name, "type": "image", "path": rel})
        return aux

    # ── RAG 探测 / 冷启动 / 降级 ──────────────────────────
    def _ensure_rag(self, cold_start: bool) -> tuple:
        """返回 (status, reason)；status ∈ online/cold_started/degraded"""
        if _rag_online():
            return "online", ""
        if cold_start:
            ok, reason = _cold_start_rag()
            if ok:
                return "cold_started", ""
            return "degraded", reason
        return "degraded", "RAG 服务未运行（cold_start=false）"

    # ── format 转换 ───────────────────────────────────────
    def _convert(self, md: str, fmt: str, title: str, out_dir: str) -> tuple:
        """按 format 转换。返回 (字段名, 值)；pdf 失败抛 RuntimeError"""
        fmt = (fmt or "md").lower()
        if fmt == "md":
            return "content", md
        if fmt == "latex":
            return "content", md_to_tex(md, title=title, image_base_dir=out_dir)
        if fmt == "pdf":
            tex = md_to_tex(md, title=title, image_base_dir=out_dir)
            tex_path = Path(out_dir) / f"{title or 'article'}.tex"
            tex_path.write_text(tex, encoding="utf-8")
            engine = _find_engine()
            if not engine:
                raise RuntimeError("未检测到 xelatex，无法编译 PDF（format=pdf 需本机安装 MiKTeX）")
            sub_env = os.environ.copy()
            sub_env["PYTHONIOENCODING"] = "utf-8"
            sub_env["PYTHONUTF8"] = "1"
            r = subprocess.run(
                [engine, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                cwd=str(out_dir), capture_output=True, text=True, timeout=600,
                encoding="utf-8", errors="replace", env=sub_env,
            )
            pdf_path = Path(out_dir) / f"{title or 'article'}.pdf"
            if not pdf_path.exists():
                raise RuntimeError(f"PDF 编译失败: {(r.stdout or r.stderr)[-300:]}")
            return "pdf_base64", base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        raise ValueError(f"format 非法: {fmt}，可选 md/latex/pdf")

    # ── 核心：POST /api/write ─────────────────────────────
    def _handle_write(self, body: dict):
        try:
            self._do_write(body)
        except ValueError as e:
            self._err(str(e), 400)
        except Exception as e:
            self._err(f"写作管道异常: {e}", 500)

    def _do_write(self, body: dict):
        # ── 1. 参数校验 ──
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        if len(prompt) > _PROMPT_MAX:
            raise ValueError(f"prompt 超过 {_PROMPT_MAX} 字")
        instructions = str(body.get("instructions", "")).strip()
        if len(instructions) > _INSTR_MAX:
            raise ValueError(f"instructions 超过 {_INSTR_MAX} 字")
        title_override = str(body.get("title", "")).strip()
        meta_override = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        fmt = str(body.get("format", "md")).lower()
        if fmt not in ("md", "latex", "pdf"):
            raise ValueError(f"format 非法: {fmt}，可选 md/latex/pdf")
        word_count = body.get("word_count")
        if word_count is not None and not isinstance(word_count, int):
            raise ValueError("word_count 必须是整数")
        crl = body.get("context_review_length")
        if crl is not None and not isinstance(crl, int):
            raise ValueError("context_review_length 必须是整数")

        # ── 2. RAG 探测 / 冷启动 / 降级 ──
        rag_cfg = body.get("rag") if isinstance(body.get("rag"), dict) else {}
        rag_enabled = bool(rag_cfg.get("enabled", False))
        rag_status, rag_reason = ("off", "")
        if rag_enabled:
            rag_status, rag_reason = self._ensure_rag(bool(rag_cfg.get("cold_start", False)))
        rag_usable = rag_status in ("online", "cold_started")

        # ── 3. LLM 客户端 ──
        planner_client = self._planner_client()
        writer_client = self._writer_client()

        # ── 4. 模板解析（名 / 内联 JSON / 描述生成）──
        template = self._resolve_template(body, planner_client)

        # ── 5. 大纲 ──
        user_meta = {}
        for f in template.get("meta", []):
            n = f.get("name", "")
            if n and n in meta_override:
                user_meta[n] = str(meta_override[n])
        outline = plan_outline(
            prompt,
            template=template,
            user_meta=user_meta,
            llm_client=planner_client,
        )
        if title_override:
            outline["title"] = title_override

        # ── 6. images 落盘登记（需大纲定 target）──
        aux_knowledge = self._save_images(body.get("images") or [], outline)

        # ── 7. 写作（两级 RAG + 串行承前启后）──
        state_mgr = StateManager()
        rag_options = {}
        if rag_usable:
            kb = str(rag_cfg.get("kb", ""))
            for s in outline.get("sections", []):
                rag_options[s["id"]] = {"enabled": True, "kb": kb}
        rag_client = RAGClient() if rag_usable else None
        md, out_path = generate_article(
            outline=outline,
            user_orders=None,
            rag_options=rag_options or None,
            llm_client=writer_client,
            state_mgr=state_mgr,
            rag_client=rag_client,
            aux_knowledge=aux_knowledge or None,
            fact_check_enabled=bool(body.get("fact_check", False)),
            context_review_length=crl if crl is not None else self.config_mgr.get("context_review_length", 800),
            template=template,
            citation_config=None,
        )

        # ── 8. format 转换 ──
        out_dir = str(Path(out_path).parent)
        key, val = self._convert(md, fmt, outline.get("title", ""), out_dir)

        # ── 9. 统计 + 返回 ──
        stats = {
            "sections": len(outline.get("sections", [])),
            "subs": sum(len(s.get("sub_sections") or []) for s in outline.get("sections", [])),
            "chars": len(md.replace(" ", "").replace("\n", "")),
        }
        resp = {
            "title": outline.get("title", ""),
            "format": fmt,
            "rag": {"enabled": rag_enabled, "status": rag_status, "reason": rag_reason},
            "images": list({f["name"] for ss in aux_knowledge.values() for f in ss.get("files", [])}),
            "stats": stats,
            "out_file": out_path,
        }
        resp[key] = val
        self._ok(**resp)


def start_external_api(port: int = DEFAULT_PORT, host: str = "0.0.0.0"):
    """启动对外 API 服务（阻塞）。config_mgr 类级共享。"""
    ExternalAPIHandler.config_mgr = ConfigManager()
    server = ThreadingHTTPServer((host, port), ExternalAPIHandler)
    print(f"  对外写作 API 已启动: http://{host}:{port}（仿 rag-assistant 8767 模式）")
    server.serve_forever()
