#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""项目卡片「节目名 / 副标题」编辑入口的端到端验证（真浏览器 + 桩数据）。

要证的五件事：
    1. 项目卡片上有这个入口，点得开
    2. 弹框里的两个输入框带着现在的值（不是空白）
    3. 保存打的是 /api/project action=update，两个字段一起送
    4. 留空也能保存（节目名留空回落到项目名由服务端管，界面照发空串）
    5. 保存后卡片上把新值显示出来

服务端一个字节都不动：/api/project 的读写全部走桩。

用法：
    python main.py --port 8812
    python tools/probes/identity_edit_e2e.py --url http://127.0.0.1:8812
"""

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

STUB_PROJECTS = {"ok": True, "projects": [
    {"id": "SMOKE-FAKE", "name": "冒烟桩项目", "archived": False,
     "program_name": "我思故我写", "subtitle": "AI协作写成的书",
     "progress": {"mode": "episodic", "next_episode": "1",
                  "done": 0, "mapped": 0}},
]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8812")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    fails, ran = [], []

    def check(ok, label, detail=""):
        ran.append(label)
        print("  %-4s %s%s" % ("PASS" if ok else "FAIL", label,
                               ("  " + detail) if detail else ""))
        if not ok:
            fails.append(label + ("  " + detail if detail else ""))

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1620, "height": 1180})
        errs = []
        pg.on("pageerror", lambda e: errs.append("pageerror: %s" % e))
        pg.on("console", lambda m: errs.append("console.error: %s" % m.text)
              if m.type == "error" else None)

        posts = []
        # 桩里维护一份「服务端现在的状态」，保存之后 GET 要能读到新值
        state = {"program_name": "我思故我写", "subtitle": "AI协作写成的书"}

        def stub(route):
            u = route.request.url
            if "/api/project" not in u:
                route.continue_()
                return
            if route.request.method == "POST":
                body = json.loads(route.request.post_data or "{}")
                posts.append(body)
                if body.get("action") == "update":
                    if "program_name" in body:
                        state["program_name"] = (body.get("program_name") or "").strip()
                    if "subtitle" in body:
                        state["subtitle"] = (body.get("subtitle") or "").strip()
                proj = dict(STUB_PROJECTS["projects"][0], **state)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "project": proj}))
                return
            proj = dict(STUB_PROJECTS["projects"][0], **state)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "projects": [proj]}))

        pg.route("**/api/project**", stub)
        pg.goto(base, wait_until="networkidle")
        pg.evaluate("async () => { await loadProjects() }")

        # 1. 卡片上有入口
        row = pg.evaluate(
            "() => { const a=document.querySelector('.proj .acts');"
            " return a?a.textContent:''; }")
        check("节目名/副标题" in row, "项目卡片上有编辑入口",
              "按钮行「%s」" % (row or "").strip())
        check(pg.evaluate("() => typeof editIdentity === 'function'"),
              "editIdentity 函数存在（按钮点得到）",
              pg.evaluate("() => typeof editIdentity"))

        # 2. 打开弹框，两个框带现值
        pg.evaluate("() => editIdentity('SMOKE-FAKE')")
        title = pg.evaluate("() => document.getElementById('m-title').textContent")
        check("节目名" in title and "副标题" in title, "弹框标题写明要改什么", title)
        prog = pg.evaluate("() => { const n=document.getElementById('ei-program'); return n?n.value:null }")
        sub = pg.evaluate("() => { const n=document.getElementById('ei-subtitle'); return n?n.value:null }")
        check(prog == "我思故我写", "节目名输入框带现值", repr(prog))
        check(sub == "AI协作写成的书", "副标题输入框带现值", repr(sub))

        # 3. 改值 → 保存
        pg.fill("#ei-program", "新节目名")
        pg.fill("#ei-subtitle", "新副标题")
        pg.click("#m-ok")
        pg.wait_for_timeout(600)
        last = posts[-1] if posts else {}
        check(last.get("action") == "update", "保存打的是 action=update",
              json.dumps(last, ensure_ascii=False))
        check(last.get("id") == "SMOKE-FAKE", "带了项目 id", repr(last.get("id")))
        check(last.get("program_name") == "新节目名"
              and last.get("subtitle") == "新副标题",
              "两个字段一起送出去",
              "program=%r subtitle=%r" % (last.get("program_name"), last.get("subtitle")))

        # 4. 保存后卡片显示新值
        pg.wait_for_timeout(400)
        head = pg.evaluate("() => { const n=document.querySelector('.proj .top');"
                           " return n?n.textContent:''; }")
        check("新节目名" in head and "新副标题" in head, "卡片上显示的是新值",
              (head or "").strip())

        # 5. 留空也能存（服务端负责回落项目名，界面照发空串）
        posts.clear()
        pg.evaluate("() => editIdentity('SMOKE-FAKE')")
        pg.fill("#ei-program", "")
        pg.fill("#ei-subtitle", "")
        pg.click("#m-ok")
        pg.wait_for_timeout(600)
        last = posts[-1] if posts else {}
        check("program_name" in last and last.get("program_name") == "",
              "节目名留空照发空串（由服务端回落项目名）",
              json.dumps(last, ensure_ascii=False))
        check("subtitle" in last and last.get("subtitle") == "",
              "副标题留空照发空串（画面少印那一行）",
              json.dumps(last, ensure_ascii=False))

        pg.wait_for_timeout(300)
        check(not errs, "无页面报错", " / ".join(errs[:3]))
        b.close()

    print("\n" + "=" * 60)
    if fails:
        print("节目名/副标题编辑入口：%d 项中 %d 项未过" % (len(ran), len(fails)))
        for f in fails:
            print("  FAIL " + f)
        return 1
    print("节目名/副标题编辑入口：%d 项全部通过" % len(ran))
    return 0


if __name__ == "__main__":
    sys.exit(main())
