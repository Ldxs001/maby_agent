"""
tools/data_tool.py — 数据访问工具（LLM 前处理用）

避免 LLM 直接读整个 db/大表格导致的 token 爆炸：
- db_query: 对数据库执行 SQL 查询，只返回结果集（而非整库）
- read_table: 读 csv/xlsx 摘要（前 N 行 + 列名），不整读
- image_info: 读图片元数据（尺寸/格式/大小），不塞二进制
"""

import os
import sqlite3
from typing import Optional

from ..tool_base import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# 数据库查询
# ---------------------------------------------------------------------------
class DBQueryTool(BaseTool):
    """对 SQLite/CSV 数据库执行查询，返回结果集"""

    def __init__(self):
        super().__init__(
            name="db_query",
            description="对 SQLite 数据库文件执行 SQL 查询，返回结果集（最多 50 行）。用于查数据而非读整个库。",
        )

    def execute(self, db_path: str = "", sql: str = "", limit: int = 50) -> ToolResult:
        if not db_path:
            return ToolResult(False, error="db_path 不能为空")
        if not sql:
            return ToolResult(False, error="sql 不能为空")
        if not os.path.isfile(db_path):
            return ToolResult(False, error=f"数据库文件不存在: {db_path}")
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # 只读查询，防止 LLM 注入写操作
            stripped = sql.strip().lower()
            if stripped.startswith(("insert", "update", "delete", "drop", "alter", "create")):
                conn.close()
                return ToolResult(False, error="仅允许 SELECT 查询")
            cur.execute(sql)
            rows = cur.fetchmany(limit)
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            if not rows:
                return ToolResult(True, output=f"查询完成，0 行结果。列: {cols}")
            lines = [" | ".join(str(c) for c in cols)]
            for r in rows:
                lines.append(" | ".join(str(r[c]) for c in cols))
            return ToolResult(True, output="\n".join(lines),
                              data={"columns": cols, "rows": len(rows)})
        except Exception as e:
            return ToolResult(False, error=f"查询失败: {e}")

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "db_path": {"type": "string", "description": "SQLite 数据库文件路径"},
                    "sql": {"type": "string", "description": "SQL 查询语句（仅 SELECT）"},
                    "limit": {"type": "integer", "description": "最多返回行数（可选，默认 50）"},
                },
                "required": ["db_path", "sql"],
            },
        }


# ---------------------------------------------------------------------------
# 表格摘要读取（csv / xlsx）
# ---------------------------------------------------------------------------
class ReadTableTool(BaseTool):
    """读取表格文件摘要：列名 + 前 N 行，避免整读大表"""

    def __init__(self):
        super().__init__(
            name="read_table",
            description="读取 csv/xlsx 表格的列名与前若干行数据（摘要），不整读大文件。",
        )

    def execute(self, path: str = "", rows: int = 20, sheet: str = "") -> ToolResult:
        if not path or not os.path.isfile(path):
            return ToolResult(False, error=f"表格文件不存在: {path}")
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".csv":
                return self._read_csv(path, rows)
            elif ext in (".xlsx", ".xls"):
                return self._read_excel(path, rows, sheet)
            else:
                return ToolResult(False, error=f"不支持的表格格式: {ext}（支持 csv/xlsx/xls）")
        except ImportError as e:
            return ToolResult(False, error=f"缺少依赖（pip install pandas openpyxl）: {e}")
        except Exception as e:
            return ToolResult(False, error=f"读取失败: {e}")

    def _read_csv(self, path: str, rows: int) -> ToolResult:
        import csv
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            all_rows = list(reader)
        if not all_rows:
            return ToolResult(True, output="空表格")
        header = all_rows[0]
        body = all_rows[1:rows + 1]
        total = len(all_rows) - 1
        lines = [f"列: {header}"]
        lines.append(f"共 {total} 行数据，预览前 {len(body)} 行:")
        for r in body:
            lines.append(" | ".join(str(c) for c in r))
        return ToolResult(True, output="\n".join(lines),
                          data={"columns": header, "total_rows": total})

    def _read_excel(self, path: str, rows: int, sheet: str) -> ToolResult:
        import pandas as pd
        df = pd.read_excel(path, sheet_name=sheet or 0, nrows=rows + 1)
        total_est = "未知（仅预览）"
        lines = [f"列: {list(df.columns)}"]
        lines.append(f"预览前 {min(rows, len(df))} 行:")
        for idx, r in df.head(rows).iterrows():
            lines.append(" | ".join(str(v) for v in r.tolist()))
        return ToolResult(True, output="\n".join(lines),
                          data={"columns": list(df.columns), "total_rows": total_est})

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "表格文件路径（csv/xlsx/xls）"},
                    "rows": {"type": "integer", "description": "预览行数（可选，默认 20）"},
                    "sheet": {"type": "string", "description": "xlsx 工作表名（可选）"},
                },
                "required": ["path"],
            },
        }


# ---------------------------------------------------------------------------
# 图片元数据
# ---------------------------------------------------------------------------
class ImageInfoTool(BaseTool):
    """读取图片元数据（尺寸/格式/大小），不加载二进制进上下文"""

    def __init__(self):
        super().__init__(
            name="image_info",
            description="读取图片文件的元数据（格式/尺寸/大小），用于识别图片类型，不读取像素内容。",
        )

    def execute(self, path: str = "") -> ToolResult:
        if not path or not os.path.isfile(path):
            return ToolResult(False, error=f"图片文件不存在: {path}")
        size_bytes = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        dims = ""
        try:
            from PIL import Image
            with Image.open(path) as im:
                dims = f"{im.width}x{im.height} ({im.format})"
        except ImportError:
            dims = "（PIL 未安装，仅识别扩展名）"
        except Exception:
            dims = "（无法解析尺寸）"
        return ToolResult(
            True,
            output=f"文件: {os.path.basename(path)}\n格式: {ext}\n大小: {size_bytes} 字节\n尺寸: {dims}",
            data={"path": path, "ext": ext, "size": size_bytes, "dims": dims},
        )

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "图片文件路径"},
                },
                "required": ["path"],
            },
        }
