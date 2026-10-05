# -*- coding: utf-8 -*-
"""发布前干跑构建：照抄 git-sync 的 PyPI 打包模板在本地构建一次 wheel，
**不上传**，只把「这次会打进去什么」列出来。

用途：发布前核对产物形态——条目数、各文件字节、entry_points 指到的模块
是否真的在包里。历史教训：入口声明 `main:main` 但 `packages=[...]` 没带
`main` 模块，wheel 里就找不到它，装完命令直接失败；这种缺陷只有把 wheel
解开来逐条看才会暴露，光看源码看不出来。

用法（在本项目根目录下跑均可）：
    python tools/probes/verify_pypi_wheel.py            构建 + 列清单
    python tools/probes/verify_pypi_wheel.py --keep     保留构建目录以便进一步翻看
    python tools/probes/verify_pypi_wheel.py --against <另一个.whl>   与已有 wheel 比内容

构建源 = 本文件往上三层（tools/probes/ 的祖父目录，即项目根）。
构建产物落 <项目根>/_smoke/pypi_build/。
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # tools/probes → tools → 项目根
NAME = os.path.basename(ROOT)                          # 目录名即项目名（如 podcast-maker）
PKG = NAME.replace("-", "_")
OUT = os.path.join(ROOT, "_smoke", "pypi_build")

BS = chr(92)   # 反斜杠：模板里要写字面 \n，不能在这里被解释


def read_version(src):
    """版本号取与项目同名的包目录（podcast-maker → podcast_maker/）。"""
    init_p = os.path.join(src, PKG, "__init__.py")
    if os.path.exists(init_p):
        with open(init_p, encoding="utf-8") as f:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', f.read())
            if m:
                return m.group(1)
    return "0"


def build(src, ver):
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(src, OUT,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dist", "build",
                                                  "*.egg-info", ".git", ".gitignore",
                                                  "nul", "con", "prn", "aux",
                                                  "NUL", "CON", "PRN", "AUX",
                                                  # 以下只在「构建源＝开发主目录」时才会撞上：
                                                  # 它们是本机过程物，不属于任何分发包
                                                  ".venv", "venv", "site-packages",
                                                  "projects", "_smoke", "_scratch",
                                                  "_backup", "models", "_bgm_pack*",
                                                  "*.log", "*.pid"))
    with open(os.path.join(OUT, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write('[build-system]\nrequires = ["setuptools>=64", "wheel"]\n'
                'build-backend = "setuptools.build_meta"\n')
    setup_py = textwrap.dedent(f'''\
    import os, re
    from setuptools import setup
    BS = chr(92)
    root = os.path.dirname(__file__)
    V = "{ver}"
    init_p = os.path.join(root, "{PKG}", "__init__.py")
    if os.path.exists(init_p):
        with open(init_p) as f:
            for l in f:
                if l.startswith("__version__"):
                    V = l.split('"')[1]; break
    REQ = []
    req_p = os.path.join(root, "requirements.txt")
    if os.path.exists(req_p):
        with open(req_p) as f:
            REQ = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    LD = "{NAME}"
    readme_p = os.path.join(root, "README.md")
    if os.path.exists(readme_p):
        with open(readme_p, encoding="utf-8") as f:
            LD = f.read()
    setup(name="{NAME}-ldxs", version=V, description="{NAME} — AI Agent",
          long_description=LD, long_description_content_type="text/markdown",
          packages=["{PKG}"], include_package_data=True,
          python_requires=">=3.10", install_requires=REQ,
          entry_points={{"console_scripts":["{NAME}-ldxs=main:main"]}})
    ''')
    with open(os.path.join(OUT, "setup.py"), "w", encoding="utf-8") as f:
        f.write(setup_py)
    with open(os.path.join(OUT, "MANIFEST.in"), "w", encoding="utf-8") as f:
        f.write(f"include requirements.txt\ninclude README.md\ninclude CHANGELOG.md\n"
                f"include LICENSE\ninclude setup.py\ninclude main.py\n"
                f"graft {PKG}/\nprune __pycache__\nprune *.pyc\n")
    r = subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation"],
                       cwd=OUT, capture_output=True, text=True)
    if r.returncode != 0:
        print("构建失败：")
        print(r.stderr[-3000:])
        sys.exit(1)
    dist = os.path.join(OUT, "dist")
    whls = sorted(f for f in os.listdir(dist) if f.endswith(".whl"))
    if not whls:
        print("dist 下没有 wheel")
        sys.exit(1)
    return os.path.join(dist, whls[0])


def members(whl):
    with zipfile.ZipFile(whl) as z:
        return {i.filename: (i.file_size, hashlib.sha256(z.read(i.filename)).hexdigest())
                for i in z.infolist()}


def report(whl):
    ms = members(whl)
    total = sum(v[0] for v in ms.values())
    print("wheel : %s" % os.path.basename(whl))
    print("大小  : %.2f MB（解压后 %.2f MB）/ %d 个条目"
          % (os.path.getsize(whl) / 1048576, total / 1048576, len(ms)))
    print()
    print("--- 内容 ---")
    for k in sorted(ms):
        print("  %9d  %s" % (ms[k][0], k))
    print()
    print("--- 入口声明与实际是否对得上 ---")
    with zipfile.ZipFile(whl) as z:
        ep = [n for n in z.namelist() if n.endswith("entry_points.txt")]
        tl = [n for n in z.namelist() if n.endswith("top_level.txt")]
        if ep:
            print("entry_points:", z.read(ep[0]).decode("utf-8").strip())
        if tl:
            print("top_level   :", z.read(tl[0]).decode("utf-8").strip())
        declared = set()
        if ep:
            for ln in z.read(ep[0]).decode("utf-8").splitlines():
                if "=" in ln:
                    mod = ln.split("=", 1)[1].strip().split(":")[0].strip()
                    declared.add(mod)
        present = {n.split("/")[0].replace(".py", "") for n in z.namelist()}
        missing = sorted(m for m in declared if m and m not in present)
        print("→ 入口模块 %s 是否在包内：%s"
              % (sorted(declared), "缺 %s" % missing if missing else "齐全"))
        if missing:
            print("  ⚠ 装完执行该命令会 ModuleNotFoundError —— packages= 列表里少声明了它")


def main():
    ap = argparse.ArgumentParser(description="发布前干跑构建 PyPI wheel（不上传）")
    ap.add_argument("--src", default=ROOT, help="构建源目录（默认：项目根）")
    ap.add_argument("--keep", action="store_true", help="保留构建目录")
    ap.add_argument("--against", default="", help="与另一个 .whl 比对内容")
    a = ap.parse_args()

    ver = read_version(a.src)
    print("构建源: %s" % a.src)
    print("版本  : %s" % ver)
    whl = build(a.src, ver)
    print()
    report(whl)

    if a.against:
        print()
        print("--- 与 %s 比对 ---" % os.path.basename(a.against))
        def _norm(d):
            # dist-info 目录名里带版本号，归一成 DIST 再比，否则版本一升就全成「新增/消失」
            return {re.sub(r"[\w.]+-\d[\w.]*\.dist-info", "DIST.dist-info", k): v
                    for k, v in d.items()}
        nx, y = _norm(members(a.against)), _norm(members(whl))
        only_x = sorted(set(nx) - set(y))
        only_y = sorted(set(y) - set(nx))
        diff = sorted(k for k in set(nx) & set(y) if nx[k][1] != y[k][1])
        print("只有参照有：%s" % (only_x or "无"))
        print("只有本次有：%s" % (only_y or "无"))
        print("内容不同  ：%d 个 %s" % (len(diff), diff[:10] if diff else ""))
        print("内容相同  ：%d 个" % (len(set(nx) & set(y)) - len(diff)))

    print()
    print("构建目录：%s" % OUT)
    if not a.keep:
        shutil.rmtree(OUT, ignore_errors=True)
        print("（已清；加 --keep 可保留）")


if __name__ == "__main__":
    main()
