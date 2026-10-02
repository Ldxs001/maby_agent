#!/usr/bin/env python3
"""版本一致性校验：__init__.__version__（唯一源） vs CHANGELOG 最新条目 vs pyproject 动态配置。

用法：python scripts/check_version.py
退出码 0 = 一致；1 = 不一致（发布前必须通过）。
"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "structured_writer" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "pyproject.toml"

init_src = INIT.read_text(encoding="utf-8")
m = re.search(r'__version__\s*=\s*"([^"]+)"', init_src)
version = m.group(1) if m else ""

ch_src = CHANGELOG.read_text(encoding="utf-8")
m2 = re.search(r"^## \[([^\]]+)\]", ch_src, re.M)
ch_latest = m2.group(1) if m2 else ""

with PYPROJECT.open("rb") as f:
    py = tomllib.load(f)
dyn = (py.get("project", {}).get("dynamic") or []) == ["version"]

print(f"__init__.__version__  : {version}")
print(f"CHANGELOG 最新条目     : {ch_latest}")
print(f"pyproject 动态版本     : {'是' if dyn else '否'}")

ok = bool(version) and version == ch_latest and dyn
if not dyn:
    print("pyproject 未配置 dynamic version，请检查 [tool.setuptools.dynamic]")
if ok:
    print("OK ✅ 版本单一来源一致")
else:
    print("不一致 ❌ 发布前必须修齐")
raise SystemExit(0 if ok else 1)
