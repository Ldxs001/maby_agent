"""辅助资料解析器 — 表格数据提取 + LLM 选列行 + JSON 注入

辅助资料按类型分流（writer.py 调用本模块）：
- table（.csv/.db）→ 小表全量 / 大表 LLM 选列行 → JSON 对象数组注入
- text（.txt/.md）→ 原样注入（截断上限，防撑爆上下文）
- image（.png/.jpg）→ 不进 prompt，py 直接插图（writer.py 处理）

零第三方依赖（csv / sqlite3 标准库）。
"""

import csv
import json
import re
import sqlite3
from typing import List, Optional, Tuple

# 文字资料注入截断上限（约 8000 字符，防撑爆上下文）
TEXT_MAX_CHARS = 8000
# 小表阈值：行数不超过此值全量注入，不调 LLM 筛选
SMALL_TABLE_LIMIT = 100


def parse_csv(path: str) -> List[List[str]]:
    """解析 .csv → 全量行列表（含可能的标题/说明/表头，未切分，由 locate_header 处理）"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(c.strip() for c in r)]
    return rows


def parse_sqlite(path: str) -> List[List[str]]:
    """解析 .db（SQLite）→ 全量行列表，只读模式，取第一个非空表"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            cur.execute(f'SELECT * FROM "{t}" LIMIT {SMALL_TABLE_LIMIT + 100}')
            rows = cur.fetchall()
            if rows:
                return [list(r) for r in rows]
        return []
    finally:
        conn.close()


def parse_table(path: str, ext: str) -> List[List[str]]:
    """按扩展名分发表格解析 → 全量行"""
    if ext == ".csv":
        return parse_csv(path)
    if ext == ".db":
        return parse_sqlite(path)
    return []


# ── 表头定位（格式鲁棒：标题行/说明行/英文表头）────────────────────

def _is_text_cell(cell) -> bool:
    s = str(cell).strip()
    if not s:
        return False
    try:
        float(s)
        return False
    except (TypeError, ValueError):
        return True


def _row_has_number(row) -> bool:
    for c in row:
        s = str(c).strip()
        if not s:
            continue
        try:
            float(s)
            return True
        except (TypeError, ValueError):
            continue
    return False


def locate_header(raw_rows: List[List[str]], llm_client=None,
                  max_preview: int = 8) -> Tuple[List[str], List[List[str]], str]:
    """定位真正的表头行（列名所在行），丢弃前置的标题/说明行。

    启发式：表头行 = 文本单元格占比 ≥ 50% 且下一行含数字；
    失败 → LLM 看原始前 N 行定位（英文表头/合并单元格/双行表头兜底）。
    返回 (header, data_rows, 说明)
    """
    if not raw_rows:
        return [], [], "（空表）"

    # ① 启发式：表头行 = 多单元格 + 至少 1 个文本列名 + 单元格数与后继数据行一致 + 后继含数字
    for i, row in enumerate(raw_rows):
        cells = [c for c in row if str(c).strip()]
        if len(cells) < 2:
            continue  # 单格大标题/合并单元格行排除
        text_cells = sum(1 for c in cells if _is_text_cell(c))
        if text_cells < 1:
            continue  # 全数字行不可能是表头
        if i + 1 >= len(raw_rows):
            continue
        next_row = raw_rows[i + 1]
        next_cells = [c for c in next_row if str(c).strip()]
        if len(cells) != len(next_cells):
            continue  # 说明行（列数不匹配数据行）排除
        if not _row_has_number(next_row):
            continue
        return row, raw_rows[i + 1:], f"（表头定位第 {i + 1} 行）"

    # ② 启发式失败 → LLM 兜底
    if llm_client is not None:
        try:
            preview = json.dumps(raw_rows[:max_preview], ensure_ascii=False)
            prompt = (
                "以下是表格的前几行原始内容（可能包含大标题、说明行、表头行）：\n"
                f"{preview}\n\n"
                "请找出真正的表头行（列名所在行），只输出该行的行号（从 1 开始计数）："
            )
            result = llm_client.chat([{"role": "user", "content": prompt}],
                                     max_tokens=None, temperature=0.0)
            m = re.search(r"\d+", str(result))
            if m:
                idx = int(m.group(0)) - 1
                if 0 <= idx < len(raw_rows):
                    return raw_rows[idx], raw_rows[idx + 1:], "（LLM 定位表头）"
        except Exception:
            pass

    # ③ 兜底：默认第一行
    return raw_rows[0], raw_rows[1:], "（默认第一行）"


def select_table(
    header: List[str],
    rows: List[List[str]],
    command: str,
    llm_client=None,
    limit: int = SMALL_TABLE_LIMIT,
) -> Tuple[List[str], List[List[str]], str]:
    """筛选表格：小表全量；大表 LLM 按指令选列/行，失败回退前 50 行。

    返回 (选中表头, 选中行, 说明文本)
    """
    if not rows:
        return header, rows, "（空表）"
    if len(rows) <= limit:
        return header, rows, f"（{len(rows)} 行全量）"

    if llm_client is None:
        return header, rows[:50], f"（{len(rows)} 行，取前 50 行）"

    # 大表：列标题 + 行标识 + 命令 → LLM 一次调用选列/行
    first_col = [str(r[0]) if r else "" for r in rows]
    prompt = (
        "以下是数据表的列标题和行标识，请根据使用指令选出相关的列和行。\n\n"
        f"列标题：{json.dumps(header, ensure_ascii=False)}\n"
        f"行标识（首列值，仅前 50 个）：{json.dumps(first_col[:50], ensure_ascii=False)}\n"
        f"总行数：{len(rows)}\n\n"
        f"使用指令：{command}\n\n"
        "只输出 JSON：{\"cols\": [\"列名1\", ...], \"rows\": [\"行标识1\", ...]}\n"
        "cols 必须是列标题中存在的值，rows 必须是行标识中存在的值（可留空数组）。"
    )
    try:
        result = llm_client.chat([{"role": "user", "content": prompt}],
                                 max_tokens=None, temperature=0.1)
        data = _parse_llm_json(result)
        sel_cols = [c for c in data.get("cols", []) if c in header]
        sel_rows = [r for r in data.get("rows", []) if r in set(first_col)]
        # 有效选中 → 切子表
        if sel_cols or sel_rows:
            col_idx = [header.index(c) for c in sel_cols] if sel_cols else list(range(len(header)))
            out_rows = []
            if sel_rows:
                for r in rows:
                    if r and r[0] in sel_rows:
                        out_rows.append([r[i] if i < len(r) else "" for i in col_idx])
            else:
                out_rows = [[r[i] if i < len(r) else "" for i in col_idx] for r in rows[:limit]]
            sel_header = [header[i] for i in col_idx]
            return sel_header, out_rows, f"（{len(rows)} 行，LLM 选中 {len(out_rows)} 行 × {len(sel_header)} 列）"
    except Exception:
        pass
    # 兜底：前 50 行
    return header, rows[:50], f"（{len(rows)} 行，LLM 筛选失败，取前 50 行）"


def _parse_llm_json(text) -> dict:
    """从 LLM 输出提取 JSON（容忍代码块/前缀文本）"""
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0]
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return json.loads(s[start:end + 1])
    return {}


def to_json(header: List[str], rows: List[List[str]]) -> str:
    """表格 → JSON 对象数组（列名做键），列名做键保证 LLM 引用精确"""
    items = []
    for r in rows:
        items.append({header[i]: r[i] if i < len(r) else "" for i in range(len(header))})
    return json.dumps(items, ensure_ascii=False)


def truncate_text(content: str, max_chars: int = TEXT_MAX_CHARS) -> str:
    """文字资料截断，防撑爆上下文"""
    if content is None:
        return ""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n...（原文 {len(content)} 字符，已截断）"


def build_text_block(content: str) -> str:
    """文字资料最终注入块（含截断说明）"""
    return truncate_text(content)


# ── 防造数据：数据源数字 vs 正文数字校验 ──────────────────────────

_NUM_UNIT_PAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:%|％|亿|万|元|万元|亿元|人次|吨|万吨|户|家|个|人|公里|平方米|㎡|台|套|辆|件)"
)


def extract_numbers(rows: List[List[str]]) -> set:
    """从数据行提取全部数值（float 集合），供正文校验比对"""
    nums = set()
    for r in rows:
        for cell in r:
            try:
                nums.add(float(cell))
            except (TypeError, ValueError):
                continue
    return nums


def verify_article_numbers(text: str, data_nums: set, tol: float = 0.5) -> List[str]:
    """扫描正文中带单位的数值，未在数据源出现的追加警告。

    排除：4 位年份（1000-2999）、明显编号。返回可疑数值文本列表。
    """
    if not data_nums:
        return []
    suspicious = []
    for m in _NUM_UNIT_PAT.finditer(text):
        try:
            v = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if 1000 <= v <= 2999 and v == int(v):
            continue  # 年份
        if not any(abs(v - d) <= tol for d in data_nums):
            suspicious.append(m.group(0))
    return suspicious
