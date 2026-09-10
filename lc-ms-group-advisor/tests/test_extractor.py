"""extractor 离线测试：零 LLM，喂 mock 处方 + 原文，断言产物。

覆盖：模式判定、三条硬闸门、formula 必需性、自动编号、两种协议、重试回灌。
运行：python tests/test_extractor.py
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lc_ms_group_advisor import extractor as ex

PASS = 0
FAIL = 0


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


def cell(q):
    return None if q is None else {"quote": q}


TEXT = "阿司匹林 C9H8O4 181.05\n咖啡因 C8H10N4O2 195.09"


def payload(items):
    return {"mode": "anchors", "items": items}


def item(g, name, formula, prec=None):
    return {"g": g, "name": cell(name), "formula": cell(formula),
            "precursor": cell(prec), "product_quant": None, "product_qual": None}


# ---------------------------------------------------------------------------
print("=" * 72)
print("A. 模式判定（纯 Python，零 LLM）")
print("=" * 72)
chk("自然语言 → anchors", ex.detect_mode("葡萄糖 C6H12O6\n咖啡因 C8H10N4O2") == "anchors")
chk("少于 3 行 → anchors", ex.detect_mode("只有一行 C6H12O6") == "anchors")
chk("TSV 表格 → columns",
    ex.detect_mode("名称\t分子式\n阿司匹林\tC9H8O4\n咖啡因\tC8H10N4O2") == "columns")
chk("CSV 表格 → columns",
    ex.detect_mode("name,formula\na,C9H8O4\nb,C8H10N4O2") == "columns")
chk("逗号不等的散文 → anchors",
    ex.detect_mode("这是第一句，有逗号。\n第二句，有两个，逗号。\n第三句没有") == "anchors")

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("B. anchors 正常路径")
print("=" * 72)
r = ex.resolve(payload([item(1, "阿司匹林", "C9H8O4", "181.05"),
                        item(2, "咖啡因", "C8H10N4O2", "195.09")]), TEXT)
chk("ok = True", r["ok"], r.get("warnings"))
chk("mode = anchors", r["mode"] == "anchors")
chk("提取 2 个化合物", len(r["compounds"]) == 2)
chk("名称逐字还原", r["compounds"][0]["name"] == "阿司匹林")
chk("化学式逐字还原", r["compounds"][1]["formula"] == "C8H10N4O2")
chk("m/z 已转 float", r["compounds"][1]["precursor"] == 195.09)
chk("sources 记录命中区间",
    r["compounds"][0]["sources"]["formula"] == {"quote": "C9H8O4", "start": 5, "end": 11},
    r["compounds"][0]["sources"])
chk("组间有序（第 2 组锚点在原文更靠后）",
    r["compounds"][1]["sources"]["formula"]["start"] > r["compounds"][0]["sources"]["formula"]["end"])

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("C. 闸门① 命中：引用不在原文 → 显式 FAIL")
print("=" * 72)
r = ex.resolve(payload([item(1, "阿司匹林", "C9H8O4X", "181.05")]), TEXT)
chk("ok = False", not r["ok"])
chk("failure.reason = not_found", r["failures"] and r["failures"][0]["reason"] == "not_found")
chk("warnings 含 anchor_miss",
    any(w["code"] == "anchor_miss" for w in r["warnings"]))
chk("未静默丢弃：failure 带原始引用",
    r["failures"][0]["quote"] == "C9H8O4X")

# LLM 凭名称编造化学式（原文没有该写法）→ 必须被拦
r = ex.resolve(payload([item(1, "阿司匹林", "C9H8O4", "181.05"),
                        item(2, "咖啡因", "C8H10N4O2", "195.09")]), TEXT)
chk("正常引用不误报", r["ok"])

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("D. 闸门② 有序：引用位置早于前一化合物 → FAIL")
print("=" * 72)
r = ex.resolve(payload([item(1, "阿司匹林", "C9H8O4", None),
                        item(2, "阿司匹林", "C8H10N4O2", None)]), TEXT)
chk("ok = False", not r["ok"])
chk("reason = out_of_order",
    any(f["reason"] == "out_of_order" for f in r["failures"]), r["failures"])

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("E. 闸门③ 连续：g 跳号 → 协议错误")
print("=" * 72)
try:
    ex.resolve(payload([item(1, "阿司匹林", "C9H8O4", None),
                        item(3, "咖啡因", "C8H10N4O2", None)]), TEXT)
    chk("跳号应抛 ExtractionError", False)
except ex.ExtractionError as e:
    chk("跳号 → ExtractionError", "连续" in str(e), str(e))

try:
    ex.resolve(payload([item(2, "阿司匹林", "C9H8O4", None)]), TEXT)
    chk("未从 1 开始应抛错", False)
except ex.ExtractionError:
    chk("未从 1 开始 → ExtractionError", True)

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("F. formula 必需：显式 null → 阻塞；name 缺 → 自动编号")
print("=" * 72)
r = ex.resolve(payload([item(1, "阿司匹林", None, "181.05"),
                        item(2, "咖啡因", "C8H10N4O2", "195.09")]), TEXT)
chk("缺化学式 → ok = False", not r["ok"])
chk("missing.formula 命中 g1", r["missing"]["formula"] == [1], r["missing"]["formula"])
chk("warnings 含 missing_formula",
    any(w["code"] == "missing_formula" for w in r["warnings"]))
chk("其余化合物仍被提取（半成品可复核）", len(r["compounds"]) == 2)

r = ex.resolve(payload([item(1, None, "C9H8O4", "181.05")]), TEXT)
chk("缺名称不阻塞（口子开在名称）", r["ok"])
chk("自动编号为 化合物1", r["compounds"][0]["name"] == "化合物1")
chk("warnings 含 auto_named", any(w["code"] == "auto_named" for w in r["warnings"]))

r = ex.resolve(payload([item(1, "阿司匹林", "C9H8O4", None)]), TEXT)
chk("缺 m/z 不阻塞", r["ok"])
chk("warnings 含 missing_precursor",
    any(w["code"] == "missing_precursor" for w in r["warnings"]))
chk("missing_precursor 是 warn 级",
    [w["level"] for w in r["warnings"] if w["code"] == "missing_precursor"] == ["warn"])

# 化学式存在于原文、但抄成了无法解析的写法 → 降级为缺失并阻塞
TEXT_BAD = "阿司匹林 约130 181.05"
r = ex.resolve(payload([item(1, "阿司匹林", "约130", "181.05")]), TEXT_BAD)
chk("非法化学式 → 降级为缺失并阻塞",
    not r["ok"] and 1 in r["missing"]["formula"], r)
chk("非法化学式不阻塞「命中」闸门（引用确实在原文里）",
    not any(f["reason"] == "not_found" for f in r["failures"]), r["failures"])

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("G. columns 模式：LLM 只给列映射，行由 Python 遍历")
print("=" * 72)
T = "名称\t分子式\t母离子m/z\n阿司匹林\tC9H8O4\t181.05\n咖啡因\tC8H10N4O2\t195.09"
p = {"mode": "columns", "delimiter": "\\t", "header_rows": 1, "orientation": "rows",
     "map": {"name": 0, "formula": 1, "precursor": 2}}
r = ex.resolve(p, T)
chk("columns ok", r["ok"], r.get("warnings"))
chk("mode = columns", r["mode"] == "columns")
chk("2 个化合物", len(r["compounds"]) == 2, r["compounds"])
chk("列取值正确", r["compounds"][1]["formula"] == "C8H10N4O2")
chk("m/z 已转 float", r["compounds"][0]["precursor"] == 181.05)
chk("sources 记录行列", r["compounds"][0]["sources"]["name"]["col"] == 0)

p2 = dict(p, map={"name": 0, "formula": 1, "precursor": 9})
r = ex.resolve(p2, T)
chk("列序号越界 → FAIL",
    any(f["reason"] == "column_out_of_range" for f in r["failures"]), r["failures"])
chk("列越界不静默补 None", not r["ok"])

T2 = "名称\t阿司匹林\t咖啡因\n分子式\tC9H8O4\tC8H10N4O2"
p3 = {"mode": "columns", "delimiter": "\\t", "header_rows": 1, "orientation": "columns",
      "map": {"name": 0, "formula": 1}}
r = ex.resolve(p3, T2)
chk("转置表（化合物按列排）解析正确",
    r["ok"] and len(r["compounds"]) == 2 and r["compounds"][1]["name"] == "咖啡因",
    r["compounds"])

p4 = {"mode": "columns", "delimiter": ",", "header_rows": 1, "orientation": "rows",
      "map": {"name": 0, "formula": 1, "precursor": 2}}
r = ex.resolve(p4, T)   # T 是 TSV，按逗号切 → 只剩 1 列
chk("分隔符判错 → 列越界 → ok=False（不静默产出错数据）", not r["ok"], r["compounds"])
chk("分隔符判错时给出列越界原因",
    any(f["reason"] == "column_out_of_range" for f in r["failures"]), r["failures"])

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("H. json_schema 结构")
print("=" * 72)
for mode in ("anchors", "columns"):
    s = ex.build_schema(mode)
    ok = True
    try:
        json.dumps(s)
    except Exception:
        ok = False
    chk(f"build_schema({mode}) 可 JSON 序列化", ok)
    chk(f"build_schema({mode}) 含 required", "required" in s)
chk("anchors schema 五个槽位齐全",
    all(k in ex.build_schema("anchors")["properties"]["items"]["items"]["properties"]
        for k in ex.SLOTS))


# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("I. 端到端：extract() 走 mock LLM（含失败回灌重试）")
print("=" * 72)
class MockLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return self.replies.pop(0) if self.replies else "{}"


good = json.dumps(payload([item(1, "阿司匹林", "C9H8O4", "181.05"),
                           item(2, "咖啡因", "C8H10N4O2", "195.09")]),
                  ensure_ascii=False)
bad = json.dumps(payload([item(1, "阿司匹林", "C9H8O4X", "181.05"),
                          item(2, "咖啡因", "C8H10N4O2", "195.09")]),
                 ensure_ascii=False)

llm = MockLLM([good])
r = ex.extract(TEXT, llm, max_retries=1)
chk("一次成功 → 只调用 1 次", len(llm.calls) == 1)
chk("ok = True", r["ok"])
chk("传了 response_format（约束解码）", llm.calls[0][1].get("response_format") is not None)

llm = MockLLM([bad, good])
r = ex.extract(TEXT, llm, max_retries=1)
chk("首次失败 → 回灌重试一次", len(llm.calls) == 2)
chk("重试后通过", r["ok"])
retry_msg = llm.calls[1][0][1]["content"]
chk("回灌提示含修正要求", "修正要求" in retry_msg)
chk("回灌提示列出未命中引用", "C9H8O4X" in retry_msg)

llm = MockLLM([bad, bad])
r = ex.extract(TEXT, llm, max_retries=1)
chk("两次都失败 → 显式返回 ok=False", not r["ok"])
chk("失败上限 = max_retries + 1 次调用", len(llm.calls) == 2)

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print(f"结果：{PASS} PASS / {FAIL} FAIL")
print("=" * 72)
sys.exit(1 if FAIL else 0)
