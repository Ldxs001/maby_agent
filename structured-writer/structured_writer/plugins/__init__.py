"""插件系统 — 仿 RAG 插件契约

- base.py：BasePlugin（execute → {type: table|text|image, content}）
- manager.py：PluginManager（扫描 builtin/ + data/plugins/，注册与调用）
- builtin/db_source：预置「数据库数据源」插件（csv/sqlite/mysql/pg）

输出契约限定辅助知识三形态（table/text/image），插件写作者只需要
把数据归一化为三形态之一，不接触写作管道内部。
"""
from .base import BasePlugin
from .manager import PluginManager, get_plugin_manager

__all__ = ["BasePlugin", "PluginManager", "get_plugin_manager"]
