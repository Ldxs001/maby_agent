"""插件管理器 — 扫描内置 + 用户插件目录，注册与调用

目录约定（仿 RAG 形态）：
- 内置：structured_writer/plugins/builtin/<name>/plugin.json + plugin_<name>.py
- 用户：data/plugins/<name>/plugin.json + plugin_<name>.py（可覆盖同名内置）

plugin.json 最小结构：
{
  "name": "db_source",
  "display_name": "数据库数据源",
  "desc": "...",
  "module": "plugin_db_source",
  "class": "DbSourcePlugin",
  "input_fields": [...],
  "output_types": ["table"]
}
"""
import importlib
import json
from pathlib import Path
from typing import List, Optional

from .base import BasePlugin

# 内置插件目录
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"
# 用户插件目录（项目 data/plugins/）
USER_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "plugins"


class PluginManager:
    def __init__(self):
        self._plugins: dict = {}

    # ── 加载 ──
    def discover(self) -> List[dict]:
        """扫描目录，注册所有插件。返回插件元信息列表。"""
        self._plugins = {}
        for base_dir in (BUILTIN_DIR, USER_DIR):
            if not base_dir.exists():
                continue
            for pdir in sorted(base_dir.iterdir()):
                if not pdir.is_dir():
                    continue
                meta_path = pdir / "plugin.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                name = meta.get("name", pdir.name)
                module_name = meta.get("module", f"plugin_{pdir.name}")
                class_name = meta.get("class", "")
                try:
                    mod = importlib.import_module(
                        f"{__package__}.builtin.{pdir.name}.{module_name}"
                    )
                    cls = getattr(mod, class_name)
                    plugin = cls(plugin_dir=str(pdir))
                    plugin.meta = meta
                    self._plugins[name] = plugin
                except Exception:
                    # 用户插件目录里 import 失败时尝试绝对路径导入
                    try:
                        import sys
                        sys.path.insert(0, str(pdir))
                        mod = importlib.import_module(module_name)
                        cls = getattr(mod, class_name)
                        plugin = cls(plugin_dir=str(pdir))
                        plugin.meta = meta
                        self._plugins[name] = plugin
                    except Exception:
                        continue
        return self.list()

    def list(self) -> List[dict]:
        """插件元信息（前端渲染用）"""
        out = []
        for p in self._plugins.values():
            out.append({
                "id": p.id or p.meta.get("name", ""),
                "name": p.meta.get("display_name", p.meta.get("name", "")),
                "desc": p.meta.get("desc", ""),
                "input_fields": p.meta.get("input_fields", []),
                "output_types": p.meta.get("output_types", ["table"]),
            })
        return out

    def get(self, plugin_id: str) -> Optional[BasePlugin]:
        return self._plugins.get(plugin_id)

    def run(self, plugin_id: str, inputs: dict) -> dict:
        """执行插件，返回 {type, name, content} 或 {error}"""
        plugin = self._plugins.get(plugin_id)
        if not plugin:
            return {"error": f"插件「{plugin_id}」不存在"}
        try:
            return plugin.execute(inputs or {})
        except Exception as e:
            return {"error": f"插件执行失败: {e}"}


# 单例（web_ui 复用）
_manager = None


def get_plugin_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
        _manager.discover()
    return _manager
