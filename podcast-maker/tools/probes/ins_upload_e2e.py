#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插入面板「就地入库」的端到端实测。

冒烟判据走的是桩，只能证明面板长对了；这一支要证明的是上传这条路真的通——
在插入面板里选文件、点入库，库里真的多出一份素材，并且它被勾上。

全程在一个临时项目里跑，测完连项目目录一起删掉。用的是真模型（探查要读
一遍标题），所以别在出片的时候跑这个。
"""

import json
import os
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8812"
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke", "_scratch")
FILES = os.path.join(HERE, "ins_e2e")

F1 = """# 佐证甲

## 一、开端

用来验证插入面板的就地入库这一段路真的通。

## 二、承接

第二段正文，够切出一个单元即可。
"""

F2 = """# 佐证乙

## 一、另一路

第二份材料，验证入库后它被勾上并标出「刚入库」。
"""

JS_INS_STATE = r"""async (pid) => {
  const mb = document.getElementById('m-body'); if (mb) mb.innerHTML = '';
  await openInsert(pid);
  const rows = Array.from(document.querySelectorAll('#ins-src .src-row'));
  return [rows.length,
          document.getElementById('ins-file') ? 1 : 0,
          rows.map(r => { const c = r.querySelector('input');
            const t = r.textContent;
            return c.value + ':' + (c.checked ? 1 : 0)
                   + ':' + (t.indexOf('刚入库') >= 0 ? 1 : 0)
                   + ':' + (t.indexOf('已在地图中') >= 0 ? 1 : 0); }).join('|')];
}"""

# 只读当前面板，不重画。入库后前端自己会带着「刚入库」标记重画一遍，
# 这时候再调一次 openInsert 会把那个标记洗掉——标记是「这一次入库的结果」，
# 不是素材的固有属性，关掉面板再打开本来就该没有。
JS_INS_READ = r"""() => {
  const rows = Array.from(document.querySelectorAll('#ins-src .src-row'));
  return [rows.length,
          document.getElementById('ins-file') ? 1 : 0,
          rows.map(r => { const c = r.querySelector('input');
            const t = r.textContent;
            return c.value + ':' + (c.checked ? 1 : 0)
                   + ':' + (t.indexOf('刚入库') >= 0 ? 1 : 0)
                   + ':' + (t.indexOf('已在地图中') >= 0 ? 1 : 0); }).join('|')];
}"""


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def n_sources(pid):
    return len(fetch("/api/sources?project_id=%s" % pid).get("sources") or [])


def wait_rows(pg, want, limit=240):
    """等插入面板自己把行数刷到 want。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        n = pg.evaluate("() => document.querySelectorAll('#ins-src .src-row').length")
        if n >= want:
            return True, time.time() - t0
        pg.wait_for_timeout(1500)
    return False, time.time() - t0


def wait_sources(pid, want, limit=240):
    """等库里素材到数。入库要跑一次探查，本地大模型读标题也要几十秒。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        if n_sources(pid) >= want:
            return True, time.time() - t0
        time.sleep(2)
    return False, time.time() - t0


def main():
    os.makedirs(FILES, exist_ok=True)
    f1 = os.path.join(FILES, "佐证甲.md")
    f2 = os.path.join(FILES, "佐证乙.md")
    with open(f1, "w", encoding="utf-8") as f:
        f.write(F1)
    with open(f2, "w", encoding="utf-8") as f:
        f.write(F2)

    made = post("/api/project", {"action": "create",
                                 "name": "E2E-插入上传试验",
                                 "plan_mode": "mapped"})
    if not made.get("ok"):
        print("立项失败：", made.get("error"))
        return 1
    pid = made["project"]["id"]
    print("临时项目：", pid)

    fails, ran = [], []

    def check(ok, label, detail=""):
        ran.append(label)
        print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label,
                               ("  " + detail) if detail else ""))
        if not ok:
            fails.append(label + ("  " + detail if detail else ""))

    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1620, "height": 1180})
            errs = []
            pg.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
            pg.on("console", lambda m: errs.append("console.error: %s" % m.text)
                  if m.type == "error" else None)
            pg.goto(BASE + "/#project")
            pg.wait_for_timeout(1600)
            pg.evaluate("async () => { await loadProjects() }")
            pg.wait_for_timeout(600)

            # ---- 1. 成稿面板上传第一份（原有的那条路，先证它没被改坏）----
            pg.evaluate("async (pid) => { await openSources(pid) }", pid)
            pg.wait_for_timeout(500)
            pg.set_input_files("#src-file", f1)
            pg.click("#src-add-btn")
            ok, dt = wait_sources(pid, 1)
            check(ok, "成稿面板上传入库（原路未被改坏）",
                  "库里 %d 份 / 等了 %.0f 秒" % (n_sources(pid), dt))

            # ---- 2. 插入面板：自带入库区 ----
            st = pg.evaluate(JS_INS_STATE, pid)
            check(st[1] == 1, "插入面板里有上传入口", "上传控件 %s" % st[1])
            check(st[0] == 1 and st[2].endswith(":0:0"),
                  "库里已有一份时，插入面板列出它并默认勾上（没进过地图）",
                  "行数 %s / 逐条「素材号:勾选:刚入库:已在地图中」= %s" % (st[0], st[2]))

            # ---- 3. 就地入库第二份：这一步才是这次的修复点 ----
            pg.set_input_files("#ins-file", f2)
            pg.click("#ins-add-btn")
            ok, dt = wait_sources(pid, 2)
            check(ok, "在插入面板里上传新素材，库里的份数真的增加",
                  "库里 %d 份 / 等了 %.0f 秒" % (n_sources(pid), dt))
            # 入库后前端自己会带着「刚入库」标记重画一遍，那一次要等探查回来。
            # 在它之前读 DOM 看到的是旧面板，读到的标记自然是空的。
            ok2, dt2 = wait_rows(pg, 2)
            check(ok2, "入库后插入面板自行重画（不必手动刷新）",
                  "面板行数到 2 / 又等了 %.0f 秒" % dt2)

            st2 = pg.evaluate(JS_INS_READ)
            rows = (st2[2] or "").split("|") if st2[2] else []
            marked = [r for r in rows if r.split(":")[2] == "1"]
            checked = [r for r in rows if r.split(":")[1] == "1"]
            check(st2[0] == 2, "插入面板就地重画后列出两份素材",
                  "行数 %s / 明细 %s" % (st2[0], st2[2]))
            check(len(marked) == 1 and len(checked) == 2,
                  "刚入库的那份被标出并勾上，另一份也仍在勾选",
                  "标「刚入库」%d 份 / 勾选 %d 份" % (len(marked), len(checked)))
            check(not errs, "无页面报错", "；".join(errs[:3]))

            if st2[0] == 2:
                pg.screenshot(path=os.path.join(HERE, "ins_panel_after_upload.png"))
            b.close()
    finally:
        gone = post("/api/project", {"action": "delete", "id": pid})
        print("清理临时项目：", "已删" if gone.get("ok") else gone.get("error"),
              "· 释放 %s 字节" % gone.get("freed_bytes"))

    print()
    if fails:
        print("插入上传实测：%d 项中 %d 项未通过" % (len(ran), len(fails)))
        for f in fails:
            print("  -", f)
        return 1
    print("插入上传实测：%d 项全部通过" % len(ran))
    return 0


if __name__ == "__main__":
    sys.exit(main())
