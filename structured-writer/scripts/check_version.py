#!/usr/bin/env python3
"""版本一致性校验：__init__.__version__（唯一源） vs CHANGELOG 最新条目 vs pyproject 动态配置
vs README 头部版本戳（有则必须相等，无则忽略）。

用法：python scripts/check_version.py
退出码 0 = 一致；1 = 不一致（发布前必须通过）。

README 头部版本戳判定（穷举一致性，2026-10-03 定稿）：
- 只查头部（前 HEADER_LINES 行）；头部无版本戳 → 忽略，不算不一致；
- 标签泛化匹配：版本 / Version / Ver.（大小写不敏感，冒号/管道可有可无，
  允许 v 前缀、Markdown 粗体与引用前缀），防止 "版本：3.1.9" 一种写法漏检其他写法；
- 找到一枚或多枚戳 → 全部必须等于 __version__，任何一枚不等即 FAIL。
"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "structured_writer" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"
README = ROOT / "README.md"
HEADER_LINES = 10  # "头部"范围：前 10 行

init_src = INIT.read_text(encoding="utf-8")
m = re.search(r'__version__\s*=\s*"([^"]+)"', init_src)
version = m.group(1) if m else ""

ch_src = CHANGELOG.read_text(encoding="utf-8")
m2 = re.search(r"^## \[([^\]]+)\]", ch_src, re.M)
ch_latest = m2.group(1) if m2 else ""

with PYPROJECT.open("rb") as f:
    py = tomllib.load(f)
dyn = (py.get("project", {}).get("dynamic") or []) == ["version"]

# README 头部版本戳：标签泛化（中英文），(?<![A-Za-z]) 防 Subversion 之类误命中
STAMP_RE = re.compile(
    r"(?<![A-Za-z])(?:版本|version|ver\.?)\s*[:：|]?\s*v?(?P<ver>\d+(?:\.\d+)+)",
    re.IGNORECASE,
)
stamps = []  # (行号, 戳值)
if README.exists():
    for i, line in enumerate(README.read_text(encoding="utf-8").splitlines()[:HEADER_LINES], 1):
        clean = line.replace("*", "").replace("`", "")  # 剥离 Markdown 强调，**Version** 形态才能命中
        for sm in STAMP_RE.finditer(clean):
            stamps.append((i, sm.group("ver")))

print(f"__init__.__version__  : {version}")
print(f"CHANGELOG 最新条目     : {ch_latest}")
print(f"pyproject 动态版本     : {'是' if dyn else '否'}")
if stamps:
    shown = ", ".join(f"第{l}行 {v}" for l, v in stamps)
    print(f"README 头部版本戳      : {shown}")
else:
    print("README 头部版本戳      : 无（忽略）")

readme_ok = all(v == version for _, v in stamps)

ok = bool(version) and version == ch_latest and dyn and readme_ok
if not dyn:
    print("pyproject 未配置 dynamic version，请检查 [tool.setuptools.dynamic]")
if stamps and not readme_ok:
    bad = ", ".join(f"第{l}行 {v}" for l, v in stamps if v != version)
    print(f"README 头部版本戳与 __version__ 不一致（{bad}），请修齐")
if ok:
    print("OK ✅ 版本单一来源一致")
else:
    print("不一致 ❌ 发布前必须修齐")
raise SystemExit(0 if ok else 1)
