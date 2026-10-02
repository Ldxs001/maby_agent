"""预置插件：数据库数据源（db_source）

对接 csv / sqlite / mysql / postgresql，读取表格数据 → 归一化输出
{type: "table", name: "<表名>.csv", content: "<CSV 文本>"}。

设计约束：
- 只读：SQLite 以 mode=ro 打开；MySQL/PG 连接后只执行 SELECT
- 表名白名单：只允许库内实际存在的表，杜绝注入
- 凭证仅会话内存使用，不落盘
- 驱动缺失时返回明确错误（sqlite/csv 标准库零依赖，永远可用）
- 取什么数据由「+」使用指令 + select_table 蓝皮书取数决定，插件不预设 SQL
"""
import csv
import io
import sqlite3
from pathlib import Path

from ...base import BasePlugin


class DbSourcePlugin(BasePlugin):
    id = "db_source"
    name = "数据库数据源"
    desc = "对接 SQLite / CSV / MySQL / PostgreSQL 数据源"

    def execute(self, inputs: dict) -> dict:
        source_type = str(inputs.get("source_type", "sqlite")).strip().lower()
        max_rows = _to_int(inputs.get("max_rows"), 100000)

        try:
            if source_type == "csv":
                header, rows, src_name = self._read_csv(inputs, max_rows)
            elif source_type == "sqlite":
                header, rows, src_name = self._read_sqlite(inputs, max_rows)
            elif source_type in ("mysql", "postgresql"):
                header, rows, src_name = self._read_netdb(source_type, inputs, max_rows)
            else:
                return {"error": f"不支持的数据源类型: {source_type}"}
        except Exception as e:
            return {"error": f"数据源读取失败: {e}"}

        if not header:
            return {"error": "数据源为空或无可读表"}
        content = _to_csv_text(header, rows)
        return {"type": "table", "name": f"{src_name}.csv", "content": content}

    # ── CSV ──
    def _read_csv(self, inputs, max_rows):
        path = str(inputs.get("path", "")).strip()
        if not path:
            raise ValueError("CSV 需要填写文件路径")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.reader(f)
            all_rows = [row for row in reader]
        if not all_rows:
            raise ValueError("CSV 为空")
        header = all_rows[0]
        rows = all_rows[1:max_rows + 1]
        return header, rows, p.stem

    # ── SQLite（只读）──
    def _read_sqlite(self, inputs, max_rows):
        path = str(inputs.get("path", "")).strip()
        if not path:
            raise ValueError("SQLite 需要填写 .db 文件路径")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()]
            table = _pick_table(tables, inputs.get("table"))
            cur = conn.execute(f'SELECT * FROM "{table}"')
            header = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
            return header, rows, table
        finally:
            conn.close()

    # ── MySQL / PostgreSQL（驱动检测，只读 SELECT）──
    def _read_netdb(self, source_type, inputs, max_rows):
        host = str(inputs.get("host", "")).strip() or "localhost"
        port = int(_to_int(inputs.get("port"), 3306 if source_type == "mysql" else 5432))
        user = str(inputs.get("user", "")).strip()
        password = str(inputs.get("password", ""))
        dbname = str(inputs.get("dbname", "")).strip()
        if not dbname:
            raise ValueError(f"{source_type} 需要填写数据库名")
        if not user:
            raise ValueError(f"{source_type} 需要填写账号")

        if source_type == "mysql":
            try:
                import pymysql
            except ImportError:
                raise RuntimeError("MySQL 需要安装驱动：pip install pymysql")
            conn = pymysql.connect(host=host, port=port, user=user, password=password,
                                   database=dbname, charset="utf8mb4", connect_timeout=10)
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW TABLES")
                    tables = [r[0] for r in cur.fetchall()]
                    table = _pick_table(tables, inputs.get("table"))
                    cur.execute(f"SELECT * FROM `{table}`")
                    header = [d[0] for d in cur.description]
                    rows = cur.fetchmany(max_rows + 1)
                    return header, rows, table
            finally:
                conn.close()
        else:  # postgresql
            try:
                import psycopg2
            except ImportError:
                raise RuntimeError("PostgreSQL 需要安装驱动：pip install psycopg2-binary")
            conn = psycopg2.connect(host=host, port=port, user=user, password=password,
                                    dbname=dbname, connect_timeout=10)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    tables = [r[0] for r in cur.fetchall()]
                    table = _pick_table(tables, inputs.get("table"))
                    cur.execute(f'SELECT * FROM "{table}"')
                    header = [d[0] for d in cur.description]
                    rows = cur.fetchmany(max_rows + 1)
                    return header, rows, table
            finally:
                conn.close()


def _pick_table(tables, wanted):
    """表名白名单：只允许库内实际存在的表"""
    if not tables:
        raise ValueError("数据库中无表")
    if wanted:
        w = str(wanted).strip()
        if w not in tables:
            raise ValueError(f"表「{w}」不存在，可用表: {', '.join(tables[:10])}")
        return w
    return tables[0]


def _to_int(v, default):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return default


def _to_csv_text(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else str(c) for c in r])
    return buf.getvalue()
