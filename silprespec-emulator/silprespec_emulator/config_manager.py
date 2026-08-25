"""配置管理器 — 读写 config.json，配置推动（09b 穷举一致性）

所有运行时参数（LLM 后端、base_url、模型、超时等）由配置推动：
  - DEFAULT_CONFIG 代码级默认
  - config.json 用户持久化（前端 /api/config 读写）
  - 命令行参数最高优先级（覆盖配置）
LLM 创建永远从 config 读取，不硬编码。
"""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

BACKEND_DEFAULTS = {
    "lm-studio": "http://localhost:1234",
    "ollama": "http://localhost:11434",
    "custom": "",
}

DEFAULT_CONFIG = {
    "llm": {
        "backend": "lm-studio",
        "base_url": "",
        "model": "",
        "api_key": "not-needed",
        "timeout": 120,
        "max_tokens": 4096,
        "temperature": 0.7,
    },
    "parallel": 5,
    "custom_templates": [],
}


class ConfigManager:
    def __init__(self, path=None):
        self.path = Path(path) if path else CONFIG_PATH
        self._cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self._merge(saved)
            except Exception:
                pass

    def _merge(self, saved: dict):
        for k, v in saved.items():
            if k in self._cfg and isinstance(self._cfg[k], dict) and isinstance(v, dict):
                self._cfg[k].update(v)
            else:
                self._cfg[k] = v

    def get(self, key, default=None):
        parts = key.split(".")
        cur = self._cfg
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return default
        return cur

    def set(self, key, value):
        parts = key.split(".")
        cur = self._cfg
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        self.save()

    def update(self, data: dict):
        self._merge(data)
        self.save()

    def get_all(self) -> dict:
        return json.loads(json.dumps(self._cfg))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def resolve_base_url(self) -> str:
        backend = self.get("llm.backend", "lm-studio")
        base_url = self.get("llm.base_url", "")
        if not base_url:
            base_url = BACKEND_DEFAULTS.get(backend, "")
        return base_url

    def get_custom_templates(self) -> list:
        return list(self._cfg.get("custom_templates", []))

    def save_custom_template(self, tmpl: dict) -> str:
        import time, re
        tmpls = self._cfg.setdefault("custom_templates", [])
        tid = tmpl.get("id", "")
        if not tid:
            tid = "tmpl_" + str(int(time.time() * 1000))[-10:]
            tmpl["id"] = tid
        for i, t in enumerate(tmpls):
            if t.get("id") == tid:
                tmpls[i] = tmpl
                self.save()
                return tid
        tmpls.append(tmpl)
        self.save()
        return tid

    def delete_custom_template(self, tid: str) -> bool:
        tmpls = self._cfg.get("custom_templates", [])
        before = len(tmpls)
        self._cfg["custom_templates"] = [t for t in tmpls if t.get("id") != tid]
        if len(self._cfg["custom_templates"]) != before:
            self.save()
            return True
        return False