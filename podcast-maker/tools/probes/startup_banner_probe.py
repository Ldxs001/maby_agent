#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动抬头只印一份的**真机验收**：起一次真服务，数控制台里抬头出现了几次。

判据：抬头文案 **1 次**、分隔线 **2 条**（一套抬头 = 上下各一条）、界面地址 **1 次**。

收这份抬头之前，控制台是这么两段挨着出现的：`main.py` 印一份不带版本号的，
紧接着 `web_ui.run_server()` 又印一份带版本号的（隔一行、中间什么都没打印）。

为什么把输出**写文件**而不是抓管道：`p.stdout.read1()` 是阻塞的，服务不退出就
永远不返回 —— 本探针第一版就是这么把自己挂死的（进程一直挂着、一行判定都不出）。
写文件可以随时读，判定条件到齐就杀进程。落临时目录，不碰项目的 `server.pid`。
"""
import os
import re
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 本文件住在 tools/probes/ 下 —— 上溯三层才是项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TAGLINE = "播客制作智能体 · 脚本 → 声音 → 字幕 → 画面 → 产物"
RULE = "=" * 62

PORT = "8899"
READY = "界面地址"


def read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


with tempfile.TemporaryDirectory(prefix="banner-probe-") as tmp:
    out_path = os.path.join(tmp, "console.txt")
    pid_path = os.path.join(tmp, "server.pid")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")

    with open(out_path, "wb") as out:
        p = subprocess.Popen([sys.executable, "main.py", "--port", PORT,
                              "--pidfile", pid_path],
                             cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        try:
            while time.time() < deadline:
                time.sleep(0.5)
                text = read(out_path)
                if TAGLINE in text and READY in text:
                    break
                if p.poll() is not None:
                    break
        finally:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)

    text = read(out_path)

lines = text.splitlines()

print("== 真机控制台输出（原样）==")
for i, ln in enumerate(lines, 1):
    print("  %2d | %s" % (i, ln))
print()

ok = True


def check(name, cond, detail=""):
    global ok
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name,
                           ("  " + str(detail)) if detail else ""))
    if not cond:
        ok = False


print("== 判据 ==")
n_tag = text.count(TAGLINE)
n_rule = text.count(RULE)
n_ver = len(re.findall(r"Podcast Maker\s+v\d+\.\d+\.\d+", text))
check("抬头文案只出现 1 次", n_tag == 1, "实际 %d 次" % n_tag)
check("分隔线 2 条（正好一套抬头）", n_rule == 2, "实际 %d 条" % n_rule)
check("带版本号的抬头 1 处", n_ver == 1, "实际 %d 处" % n_ver)
check("界面地址只印 1 次", text.count(READY) == 1,
      "实际 %d 次" % text.count(READY))
check("没有两条分隔线挨着（不是两套抬头叠在一起）",
      not any(lines[i].strip() == RULE and lines[i + 1].strip() == RULE
              for i in range(len(lines) - 1)))

print()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
