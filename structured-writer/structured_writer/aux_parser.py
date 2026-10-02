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
from collections import Counter
from typing import List, Optional, Tuple

# 文字资料注入截断上限（约 8000 字符，防撑爆上下文）
TEXT_MAX_CHARS = 8000
# 小表阈值：行数不超过此值全量注入，不调 LLM 筛选
SMALL_TABLE_LIMIT = 100
# 蓝皮书行标识字符预算：一次 LLM 调用可携带的行标识总量上限
BLUEPRINT_IDENT_CHARS = 25000


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


# ═══════════════════════════════════════════════════════════
# 蓝皮书：大表结构压缩（天然算法：唯一值分析 + 前缀聚合 + 等频分箱）
# 目标：十几万行不撑爆上下文 —— LLM 只看压缩后的结构，py 在全量上精确切
# ═══════════════════════════════════════════════════════════

_DATE_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?$"
    r"|^\d{4}年(\d{1,2}月)?(\d{1,2}日)?$"
    r"|^\d{6,8}$"
)


def _date_groups(vals):
    """日期列前缀分组：采样判断日期格式 → 按 年(4) / 年月(7) 前缀聚合。
    返回 (kind, {key: count}) 或 None"""
    sample = [str(v).strip() for v in vals[:200] if str(v).strip()]
    if not sample:
        return None
    hit = sum(1 for v in sample if _DATE_RE.match(v))
    if hit / len(sample) < 0.8:
        return None
    for kind, ln in (("date-年", 4), ("date-年月", 7)):
        groups = {}
        for v in vals:
            k = str(v).strip()[:ln]
            if k:
                groups[k] = groups.get(k, 0) + 1
        if 2 <= len(groups) <= 500:
            return kind, groups
    return None


def _text_groups(vals):
    """文本前缀分组：分隔符优先（- / _ 空格）→ 字符截断（4→3→2）。
    返回 (kind, {key: count}) 或 None"""
    strs = [str(v).strip() for v in vals if str(v).strip()]
    for sep in ("-", "/", "_", " "):
        groups = {}
        for s in strs:
            if sep in s:
                k = s.split(sep)[0]
                groups[k] = groups.get(k, 0) + 1
        if 2 <= len(groups) <= 500:
            return f"prefix({sep})", groups
    for ln in (4, 3, 2):
        groups = {}
        for s in strs:
            k = s[:ln]
            groups[k] = groups.get(k, 0) + 1
        if 2 <= len(groups) <= 500:
            return f"prefix{ln}", groups
    return None


def _equal_freq_bins(vals, n_bins: int = 10):
    """数值列等频分箱：每箱行数相等（数据库 histogram 标准做法，偏态友好）。
    返回 [(label, count)]"""
    nums = []
    for v in vals:
        try:
            nums.append(float(str(v).replace(",", "").replace("￥", "").replace("¥", "").strip()))
        except (ValueError, TypeError):
            continue
    if len(nums) < 2:
        return []
    nums.sort()
    n = len(nums)
    step = max(1, n // n_bins)
    bins = []
    for i in range(0, n, step):
        seg = nums[i:i + step]
        if not seg:
            continue
        lo, hi = seg[0], seg[-1]
        label = f"{lo:g}~{hi:g}" if lo != hi else f"{lo:g}"
        bins.append((label, len(seg)))
    return bins


def build_blueprint(header: List[str], rows: List[List[str]], max_dims: int = 5) -> dict:
    """生成大表结构蓝皮书。

    每列独立分析（天然算法，无领域预设）：
    - 唯一值 2~500 → category（天然维度）
    - 唯一值 ≈ 行数（随机列）→ 日期前缀聚合 / 文本前缀聚合 / 数值等频分箱
    - 唯一值 = 1 → 丢弃

    返回 {"cols": [...], "total_rows": n, "dims": [{"col", "kind", "segments": [[key, count]]}]}
    """
    total = len(rows)
    n_cols = len(header)
    col_values = [[] for _ in range(n_cols)]
    for r in rows:
        for i in range(n_cols):
            col_values[i].append(str(r[i]) if i < len(r) else "")
    dims = []
    for ci, col in enumerate(header):
        vals = [v for v in col_values[ci] if v.strip()]
        if not vals:
            continue
        uniq = Counter(vals)
        n_uniq = len(uniq)
        if n_uniq == 1:
            continue
        # 日期列优先：无论唯一值多少，聚合到 年→月 层级（时间是最好的分段键）
        dg = _date_groups(vals)
        if dg:
            kind, groups = dg
            dims.append({"col": col, "kind": kind,
                         "segments": [[k, c] for k, c in sorted(groups.items(), key=lambda x: -x[1])]})
            continue
        if n_uniq / len(vals) > 0.9:
            # 接近全随机（非日期）：文本前缀聚合 → 数值分箱
            tg = _text_groups(vals)
            if tg:
                kind, groups = tg
                dims.append({"col": col, "kind": kind,
                             "segments": [[k, c] for k, c in sorted(groups.items(), key=lambda x: -x[1])]})
                continue
            bins = _equal_freq_bins(vals)
            if bins:
                dims.append({"col": col, "kind": "number", "segments": [list(b) for b in bins]})
            continue
        # 天然维度：直接按唯一值分段
        dims.append({"col": col, "kind": "category",
                     "segments": [[k, c] for k, c in uniq.most_common()]})
    # 排序：category > date > prefix > number；取前 max_dims
    def _order(d):
        k = d["kind"]
        if k.startswith("date"):
            return 1
        if k.startswith("prefix"):
            return 2
        if k == "number":
            return 3
        return 0
    dims.sort(key=_order)
    return {"cols": header, "total_rows": total, "dims": dims[:max_dims]}


def _blueprint_text(bp: dict) -> str:
    """蓝皮书 → 文本（供 LLM 阅读）"""
    lines = [f"总行数：{bp['total_rows']}", "结构分布："]
    for d in bp["dims"]:
        segs = ", ".join(f"{k}({c})" for k, c in d["segments"][:60])
        lines.append(f"  - 列「{d['col']}」（{d['kind']}）：{segs}")
    return "\n".join(lines)


def _match_ident(ident: str, target: str) -> bool:
    """行标识匹配：精确 或 前缀（日期聚合段 label='2016' 命中 '2016-03-15'）"""
    ident = str(ident).strip()
    target = str(target).strip()
    if not ident or not target:
        return False
    return ident == target or ident.startswith(target) or target.startswith(ident)


def _select_once(header, rows, first_col, blueprint, command, llm_client, limit):
    """中表（行标识可一次装下）：蓝皮书 + 全量行标识，1 次 LLM 调用选列行"""
    ident_text = json.dumps(first_col, ensure_ascii=False)
    if len(ident_text) > BLUEPRINT_IDENT_CHARS:
        ident_text = json.dumps(first_col[:2000], ensure_ascii=False) + f"（共 {len(first_col)} 个，仅展示前 2000 个）"
    prompt = (
        "你是数据筛选助手。下面是一个数据表的列标题、结构分布和行标识，"
        "请根据使用指令选出需要的列和行。\n\n"
        f"列标题：{json.dumps(header, ensure_ascii=False)}\n"
        f"{_blueprint_text(blueprint)}\n"
        f"行标识（首列值）：{ident_text}\n\n"
        f"使用指令：{command}\n\n"
        "只输出 JSON：{\"cols\": [...], \"rows\": [...]}\n"
        "cols 必须是列标题中存在的值；rows 必须是行标识中存在的值（或前缀，可留空数组=全部行）。"
    )
    try:
        result = llm_client.chat([{"role": "user", "content": prompt}],
                                 max_tokens=None, temperature=0.1)
        data = _parse_llm_json(result)
        return _apply_selection(header, rows, first_col, data, limit)
    except Exception:
        pass
    return header, rows[:limit], f"（{len(rows)} 行，LLM 筛选失败，取前 {limit} 行）"


def _select_twice(header, rows, first_col, blueprint, command, llm_client, limit):
    """大表（行标识装不下）：2 次调用——粗筛选段（蓝皮书）→ 精取列行（段内）"""
    # ── 第一次：粗筛 —— LLM 看蓝皮书选维度和段 ──
    try:
        prompt1 = (
            "你是数据筛选助手。下面是一个大表的结构分布（蓝皮书），"
            "请根据使用指令选出相关数据所在的维度和分段。\n\n"
            f"列标题：{json.dumps(header, ensure_ascii=False)}\n"
            f"{_blueprint_text(blueprint)}\n\n"
            f"使用指令：{command}\n\n"
            "只输出 JSON：{\"dims\": [\"维度列名\"], \"segments\": [\"段标签\", ...]}\n"
            "dims 必须是上面列标题中出现的列名；segments 必须原样抄写该维度下的段标签。"
        )
        r1 = llm_client.chat([{"role": "user", "content": prompt1}],
                             max_tokens=None, temperature=0.1)
        d1 = _parse_llm_json(r1)
        sel_dims = [c for c in d1.get("dims", []) if c in header]
        sel_segs = [str(s) for s in d1.get("segments", [])]
        # py 按段过滤候选行（全量上精确匹配/前缀匹配）
        cand_rows = []
        for r in rows:
            if r and any(_match_ident(r[0], s) for s in sel_segs):
                cand_rows.append(r)
    except Exception:
        cand_rows = []
        sel_dims, sel_segs = [], []

    if not cand_rows:
        # 粗筛失败/无命中 → 兜底前 limit 行
        return header, rows[:limit], f"（{len(rows)} 行，粗筛无命中，取前 {limit} 行）"

    # ── 第二次：精取 —— 段内行标识 + 列标题 → LLM 选列行 ──
    cand_first = [str(r[0]) if r else "" for r in cand_rows]
    ident_text = json.dumps(cand_first, ensure_ascii=False)
    if len(ident_text) > BLUEPRINT_IDENT_CHARS:
        ident_text = json.dumps(cand_first[:2000], ensure_ascii=False) + f"（候选 {len(cand_first)} 行，仅展示前 2000 个）"
    try:
        prompt2 = (
            "你是数据筛选助手。已按使用指令粗筛出以下候选行，请进一步选出需要的列和行。\n\n"
            f"列标题：{json.dumps(header, ensure_ascii=False)}\n"
            f"候选行（首列值）：{ident_text}\n\n"
            f"使用指令：{command}\n\n"
            "只输出 JSON：{\"cols\": [...], \"rows\": [...]}\n"
            "cols 必须是列标题中存在的值；rows 必须是候选行标识中存在的值（或前缀，可留空数组=全部候选行）。"
        )
        r2 = llm_client.chat([{"role": "user", "content": prompt2}],
                             max_tokens=None, temperature=0.1)
        d2 = _parse_llm_json(r2)
        h2, rows2, note = _apply_selection(header, cand_rows, cand_first, d2, limit)
        if rows2:
            return h2, rows2, f"（{len(rows)} 行，粗筛「{','.join(sel_segs[:3])}」→ 精取 {len(rows2)} 行 × {len(h2)} 列）"
    except Exception:
        pass
    # 精取失败 → 用粗筛候选前 limit 行
    return header, cand_rows[:limit], f"（{len(rows)} 行，粗筛「{','.join(sel_segs[:3])}」取前 {limit} 行）"


def _apply_selection(header, rows, first_col, data, limit):
    """按 LLM 的 cols/rows 选择切子表（py 校验存在性 + 宽松前缀匹配）"""
    sel_cols = [c for c in data.get("cols", []) if c in header]
    sel_rows = [str(r) for r in data.get("rows", []) if str(r).strip()]
    if not sel_cols and not sel_rows:
        raise ValueError("LLM 未选中任何列/行")
    col_idx = [header.index(c) for c in sel_cols] if sel_cols else list(range(len(header)))
    if sel_rows:
        out_rows = [r for r in rows if r and any(_match_ident(r[0], s) for s in sel_rows)]
        if not out_rows:
            raise ValueError("选中的行标识未命中")
    else:
        out_rows = rows
    sel_header = [header[i] for i in col_idx]
    rows_out = [[r[i] if i < len(r) else "" for i in col_idx] for r in out_rows]
    return sel_header, rows_out, f"（{len(rows)} 行，选中 {len(rows_out)} 行 × {len(sel_header)} 列）"


def select_table(
    header: List[str],
    rows: List[List[str]],
    command: str,
    llm_client=None,
    limit: int = SMALL_TABLE_LIMIT,
) -> Tuple[List[str], List[List[str]], str]:
    """筛选表格：小表全量（0 次 LLM）；中表蓝皮书+全量行标识（1 次）；
    大表蓝皮书粗筛选段 → 精取列行（2 次）。LLM 失败 py 兜底。

    返回 (选中表头, 选中行, 说明文本)
    """
    if not rows:
        return header, rows, "（空表）"
    if len(rows) <= limit:
        return header, rows, f"（{len(rows)} 行全量）"
    if llm_client is None:
        return header, rows[:limit], f"（{len(rows)} 行，取前 {limit} 行）"

    first_col = [str(r[0]) if r else "" for r in rows]
    blueprint = build_blueprint(header, rows)

    # 行标识字符预算内 → 1 次；否则 2 次
    if sum(len(s) for s in first_col) <= BLUEPRINT_IDENT_CHARS:
        return _select_once(header, rows, first_col, blueprint, command, llm_client, limit)
    return _select_twice(header, rows, first_col, blueprint, command, llm_client, limit)


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
