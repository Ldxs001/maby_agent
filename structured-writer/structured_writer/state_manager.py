"""状态管理器 — 会话状态、进度追踪"""
import json
import copy
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
ARCHIVES_DIR = DATA_DIR / "archives" / "sessions"
OUTPUTS_DIR = DATA_DIR / "outputs"


class StateManager:
    def __init__(self, session_id=None):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

        if session_id:
            self.session_id = session_id
        else:
            self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.path = SESSIONS_DIR / f"{self.session_id}.json"
        self._state = None

    def init_session(self, config: dict = None):
        self._state = {
            "session_id": self.session_id,
            "created_at": datetime.now().isoformat(),
            "config": config or {},
            "outline": {
                "title": "",
                "sections": []
            },
            "user_orders": {},
            "messages": [],       # 会话消息历史（用户要求等，切会话/重启后恢复显示）
            "output_file": "",
            "phase": "config",    # config → planning → reviewing → writing → done
        }
        self.save()

    def set_outline(self, outline: dict):
        self._state["outline"] = outline
        self._state["phase"] = "reviewing"
        self.save()

    def set_user_orders(self, orders: dict):
        self._state["user_orders"] = orders
        self.save()

    def append_message(self, role: str, content: str):
        """追加会话消息（role: user/assistant），切会话/重启后 loadSession 重建显示。"""
        msgs = self._state.setdefault("messages", [])
        msgs.append({"role": role, "content": content})
        if len(msgs) > 200:
            del msgs[:len(msgs) - 200]  # 上限防膨胀
        self.save()

    def set_phase(self, phase: str):
        self._state["phase"] = phase
        self.save()

    def set_output_file(self, path: str):
        self._state["output_file"] = path
        self.save()

    def update_section(self, section_id: str, updates: dict):
        """更新某个 section 或 sub_section 的状态"""
        for s in self._state["outline"].get("sections", []):
            if s["id"] == section_id:
                s.update(updates)
                self.save()
                return
            # 搜子结构
            for ss in s.get("sub_sections", []):
                if ss["id"] == section_id:
                    ss.update(updates)
                    self.save()
                    return
        self.save()

    def get_progress(self) -> dict:
        sections = self._state["outline"].get("sections", [])
        total_sections = len(sections)
        done_sections = sum(1 for s in sections if s.get("status") == "done")

        # 统计子结构粒度
        total_subs = 0
        done_subs = 0
        for s in sections:
            subs = s.get("sub_sections", [])
            if subs:
                total_subs += len(subs)
                done_subs += sum(1 for ss in subs if ss.get("status") == "done")
            else:
                total_subs += 1
                done_subs += 1 if s.get("status") == "done" else 0

        total_words = sum(s.get("actual_word_count", 0) for s in sections)
        # 小说线章级门控：等待用户确认的章（status=planning）
        awaiting = None
        for s in sections:
            if s.get("status") == "planning":
                awaiting = {
                    "id": s["id"],
                    "title": s.get("title", ""),
                    "chapter": (s.get("_novel") or {}).get("chapter", ""),
                    "sub_sections": s.get("sub_sections", []),
                }
                break
        # 修复引擎章级触发：章检（finalize-chapter 写 issues）与全文三检（finalize-novel 写 full_items）
        # 是串行的两个功能点位——最后一章章检通过后才触发全文三检，同一章不会同时有 issues 和 full_items。
        # 一套循环统一判定两个字段，覆盖式最近章（前端轮询发现 repair_pending 非空即弹修复面板）。
        repair_pending = None
        hints = self._state.get("_repair_hints", {})
        if self._state.get("phase") in ("writing", "done"):
            for s in sections:
                ch = (s.get("_novel") or {}).get("chapter", "")
                hint = hints.get(ch)
                if not hint:
                    continue
                issues = hint.get("issues") or []
                full_items = hint.get("full_items") or []
                # 判定依据：章检 HARD/FAIL 行（SOFT 非阻断——不触发弹窗，仅进修复面板展示）
                # 或三检 full_items 非空 = 有待处理
                hard_lines = [ln for ln in issues if "[HARD]" in ln or "[FAIL]" in ln]
                if not hard_lines and not full_items:
                    continue
                # 该章未标记"已处理/已跳过" → 待弹面板（覆盖式：最后命中=最近章）
                if not hint.get("_repaired"):
                    repair_pending = {
                        "chapter": ch,
                        "section_id": s["id"],
                        "issues": issues[:20],
                        "has_output": bool(hint.get("output")),
                        "full_items": full_items,
                    }
        return {
            "total": total_subs,
            "done": done_subs,
            "total_sections": total_sections,
            "done_sections": done_sections,
            "total_words": total_words,
            "phase": self._state.get("phase"),
            "title": self._state.get("outline", {}).get("title", ""),
            "status_text": self._state.get("_status_text", "") if self._state.get("phase") in ("writing",) else "",
            "awaiting_confirm": awaiting,
            "repair_pending": repair_pending,
        }

    def set_status_text(self, text: str):
        """设置当前状态文本（显示在进度条下方）"""
        self._state["_status_text"] = text
        self.save()

    def save_repair_hint(self, chapter_id: str, result: dict):
        """保存章检结果（供前端修复面板读取：T0/T1 分级清单）。"""
        hints = self._state.setdefault("_repair_hints", {})
        hints[chapter_id] = result
        self.save()

    def get_repair_hints(self) -> dict:
        """读取全部章检结果。"""
        return copy.deepcopy(self._state.get("_repair_hints", {}))

    def get_state(self):
        return copy.deepcopy(self._state)

    def load(self, session_id: str = None):
        sid = session_id or self.session_id
        p = SESSIONS_DIR / f"{sid}.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                self._state = json.load(f)
            self.session_id = sid
            self.path = p
            # 注意：不要在这里清空 _replan_inflight——会误杀进程内正在进行的 in-flight 标记
            # 僵尸标记判定放在调用方（_handle_novel_confirm），按 started_at + timeout 自动清理
        else:
            raise FileNotFoundError(f"Session {sid} 不存在")

    def save(self):
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def list_sessions(self) -> list[dict]:
        """列出所有活跃和已归档会话"""
        sessions = []
        for is_archived, base_dir in [(False, SESSIONS_DIR), (True, ARCHIVES_DIR)]:
            if not base_dir.is_dir():
                continue
            for p in sorted(base_dir.glob("*.json"), reverse=True):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        s = json.load(f)
                    sessions.append({
                        "id": s.get("session_id", p.stem),
                        "title": s.get("outline", {}).get("title", "") or (
                            (s.get("messages") or [{}])[0].get("content", "")[:20] or "未命名"
                        ),
                        "phase": s.get("phase", "unknown"),
                        "created_at": s.get("created_at", ""),
                        "active": not is_archived
                    })
                except Exception:
                    pass
        # 按活跃优先、再按时间倒序
        sessions.sort(key=lambda x: (not x["active"], x.get("created_at", "")), reverse=True)
        return sessions

    def archive_session(self, session_id: str) -> bool:
        """归档指定会话：移入 archives/sessions/"""
        try:
            ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
            src = SESSIONS_DIR / f"{session_id}.json"
            if src.exists():
                dst = ARCHIVES_DIR / f"{session_id}.json"
                src.replace(dst)
            return True
        except Exception:
            return False

    def restore_session(self, session_id: str) -> bool:
        """恢复归档会话：移回 sessions/"""
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            src = ARCHIVES_DIR / f"{session_id}.json"
            if src.exists():
                dst = SESSIONS_DIR / f"{session_id}.json"
                src.replace(dst)
            return True
        except Exception:
            return False

    def delete_session(self, session_id: str) -> bool:
        """永久删除会话（从两种目录中都删除）"""
        try:
            for base_dir in [SESSIONS_DIR, ARCHIVES_DIR]:
                p = base_dir / f"{session_id}.json"
                if p.exists():
                    p.unlink()
            return True
        except Exception:
            return False

    @classmethod
    def check_session_limit(cls, max_sessions: int = 20):
        """检查活跃会话数，超过则归档最旧的非当前会话"""
        sm = cls()
        sessions = sm.list_sessions()
        active = [s for s in sessions if s.get("active")]
        if len(active) > max_sessions:
            # 最旧的（排序已按时间倒序，最后一个最旧）
            oldest = active[-1]
            sm.archive_session(oldest["id"])
