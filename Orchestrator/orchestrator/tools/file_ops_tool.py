"""
tools/file_ops_tool.py — 通用文件系统衔接工具集

编排器的基础能力：链的中间产物需要在节点间流转，
复制/移动/删除/追加/建目录/查找 是最底层的衔接操作。

所有工具带路径安全校验：拒绝空路径、拒绝操作系统关键目录。
"""

import os
import shutil
import glob as globlib
from typing import Optional

from ..tool_base import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# 路径安全校验（通用）
# ---------------------------------------------------------------------------
_CRITICAL_DIRS = {
    os.path.abspath(os.path.expanduser("~") + os.sep + ".workbuddy"),
    os.path.abspath(os.environ.get("WINDIR", "C:\\Windows")),
    os.path.abspath("C:\\"),
    os.path.abspath("/"),
    os.path.abspath(os.path.expanduser("~") + os.sep + "Desktop"),
    os.path.abspath(os.path.expanduser("~") + os.sep + "Documents"),
    os.path.abspath(os.path.expanduser("~") + os.sep + "Downloads"),
}


def _safe_abspath(path: str) -> Optional[str]:
    """规范化路径；空路径或关键目录返回 None"""
    if not path or not str(path).strip():
        return None
    ap = os.path.abspath(os.path.expanduser(path))
    ap = os.path.normpath(ap)
    # 关键目录本身（或其父链为空）拒绝
    for c in _CRITICAL_DIRS:
        if ap == c:
            return None
    return ap


def _is_within(base: str, target: str) -> bool:
    """target 是否位于 base 之下（含相等）"""
    try:
        return os.path.commonpath([base, target]) == base
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 复制
# ---------------------------------------------------------------------------
class CopyFileTool(BaseTool):
    """复制文件或目录"""

    def __init__(self):
        super().__init__(
            name="copy_file",
            description="复制文件或目录到目标路径（可改名）。源不存在或目标已存在会报错。",
        )

    def execute(self, src: str = "", dst: str = "") -> ToolResult:
        s = _safe_abspath(src)
        d = _safe_abspath(dst)
        if not s:
            return ToolResult(False, error="源路径不能为空")
        if not d:
            return ToolResult(False, error="目标路径不能为空")
        if not os.path.exists(s):
            return ToolResult(False, error=f"源路径不存在: {s}")
        if os.path.exists(d):
            return ToolResult(False, error=f"目标路径已存在: {d}")
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d)
            else:
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
            return ToolResult(True, output=f"已复制: {s} → {d}")
        except Exception as e:
            return ToolResult(False, error=f"复制失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件或目录路径"},
                    "dst": {"type": "string", "description": "目标路径（可含新文件名）"},
                },
                "required": ["src", "dst"],
            },
        }


# ---------------------------------------------------------------------------
# 移动/重命名
# ---------------------------------------------------------------------------
class MoveFileTool(BaseTool):
    """移动文件或目录（可重命名）"""

    def __init__(self):
        super().__init__(
            name="move_file",
            description="移动文件或目录到目标路径（可重命名）。源不存在或目标已存在会报错。",
        )

    def execute(self, src: str = "", dst: str = "") -> ToolResult:
        s = _safe_abspath(src)
        d = _safe_abspath(dst)
        if not s:
            return ToolResult(False, error="源路径不能为空")
        if not d:
            return ToolResult(False, error="目标路径不能为空")
        if not os.path.exists(s):
            return ToolResult(False, error=f"源路径不存在: {s}")
        if os.path.exists(d):
            return ToolResult(False, error=f"目标路径已存在: {d}")
        try:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.move(s, d)
            return ToolResult(True, output=f"已移动: {s} → {d}")
        except Exception as e:
            return ToolResult(False, error=f"移动失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "源文件或目录路径"},
                    "dst": {"type": "string", "description": "目标路径（可含新文件名）"},
                },
                "required": ["src", "dst"],
            },
        }


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
class DeleteFileTool(BaseTool):
    """删除文件或目录（目录需为空，或递归删除）"""

    def __init__(self):
        super().__init__(
            name="delete_file",
            description="删除文件或目录。recursive=True 时递归删除非空目录。危险操作，请确认路径。",
        )

    def execute(self, path: str = "", recursive: bool = False) -> ToolResult:
        p = _safe_abspath(path)
        if not p:
            return ToolResult(False, error="路径不能为空")
        if not os.path.exists(p):
            return ToolResult(False, error=f"路径不存在: {p}")
        try:
            if os.path.isdir(p):
                if recursive:
                    shutil.rmtree(p)
                else:
                    os.rmdir(p)  # 非空目录会抛 OSError
            else:
                os.remove(p)
            return ToolResult(True, output=f"已删除: {p}")
        except OSError as e:
            return ToolResult(False, error=f"删除失败（目录非空？用 recursive=True）: {e}")
        except Exception as e:
            return ToolResult(False, error=f"删除失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件或目录路径"},
                    "recursive": {"type": "boolean", "description": "删除非空目录时设为 true（可选，默认 false）"},
                },
                "required": ["path"],
            },
        }


# ---------------------------------------------------------------------------
# 追加写入
# ---------------------------------------------------------------------------
class AppendFileTool(BaseTool):
    """向文件追加内容（不存在则创建）"""

    def __init__(self):
        super().__init__(
            name="append_file",
            description="向文件末尾追加内容（UTF-8）。文件不存在时自动创建。",
        )

    def execute(self, path: str = "", content: str = "") -> ToolResult:
        p = _safe_abspath(path)
        if not p:
            return ToolResult(False, error="路径不能为空")
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(content)
                if content and not content.endswith("\n"):
                    f.write("\n")
            return ToolResult(True, output=f"已追加 {len(content)} 字符到: {p}")
        except Exception as e:
            return ToolResult(False, error=f"追加失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "content": {"type": "string", "description": "要追加的内容"},
                },
                "required": ["path", "content"],
            },
        }


# ---------------------------------------------------------------------------
# 创建目录
# ---------------------------------------------------------------------------
class MakeDirTool(BaseTool):
    """创建目录（含父目录）"""

    def __init__(self):
        super().__init__(
            name="make_dir",
            description="递归创建目录（已存在则无操作，成功返回）。",
        )

    def execute(self, path: str = "") -> ToolResult:
        p = _safe_abspath(path)
        if not p:
            return ToolResult(False, error="路径不能为空")
        try:
            os.makedirs(p, exist_ok=True)
            return ToolResult(True, output=f"目录就绪: {p}")
        except Exception as e:
            return ToolResult(False, error=f"创建目录失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录路径"},
                },
                "required": ["path"],
            },
        }


# ---------------------------------------------------------------------------
# 查找文件
# ---------------------------------------------------------------------------
class FindFilesTool(BaseTool):
    """按 glob 模式查找文件"""

    def __init__(self):
        super().__init__(
            name="find_files",
            description="按 glob 模式在目录中查找文件（如 'data/*.md'、'**/*.json'），返回匹配路径列表。",
        )

    def execute(self, pattern: str = "", max_items: int = 100) -> ToolResult:
        if not pattern or not str(pattern).strip():
            return ToolResult(False, error="查找模式不能为空")
        try:
            matches = sorted(globlib.glob(pattern, recursive=True))
            matches = [m for m in matches if _safe_abspath(m) is not None]
            if not matches:
                return ToolResult(True, output=f"未找到匹配文件: {pattern}")
            shown = matches[:max_items]
            lines = [f"找到 {len(matches)} 个文件:"]
            for m in shown:
                sz = os.path.getsize(m) if os.path.isfile(m) else 0
                kind = "目录" if os.path.isdir(m) else f"{sz} 字节"
                lines.append(f"  {m}  ({kind})")
            if len(matches) > max_items:
                lines.append(f"  ... 其余 {len(matches) - max_items} 个省略")
            return ToolResult(True, output="\n".join(lines), data={"matches": matches})
        except Exception as e:
            return ToolResult(False, error=f"查找失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 'data/*.md' 或 '**/*.json'"},
                    "max_items": {"type": "integer", "description": "最多返回条数（可选，默认 100）"},
                },
                "required": ["pattern"],
            },
        }
