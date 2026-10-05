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

"""合成前提醒的端到端验证（真浏览器 + 桩数据，服务端一个字节都不动）。

要证的四件事：
    1. 点合成先问一次脚本阶段的问题
    2. 有问题就弹框，逐条列出（带句号），且写明这份记录不追踪后续修改
    3. 没点确认之前不发合成请求——提醒不是开工
    4. 点了确认，请求里带 ack 留痕

用法：
    python main.py --port 8812
    python tools/probes/render_ack_check.py --url http://127.0.0.1:8812
"""

import argparse
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

STUB_PROJECTS = {"ok": True, "projects": [
    {"id": "SMOKE-FAKE", "name": "冒烟桩项目", "archived": False,
     "progress": {"mode": "episodic", "next_episode": "1",
                  "done": 0, "mapped": 0}},
]}
STUB_SCRIPTS = {"ok": True, "project_id": "SMOKE-FAKE", "items": [
    {"no": "1", "title": "第一期", "done": False, "has_script": True, "lines": 20},
    {"no": "2", "title": "第二期", "done": False, "has_script": True, "lines": 18},
]}
STUB_ISSUES = {"ok": True, "issues": [
    {"episode_no": "1", "time": "2026-09-13T15:27:04",
     "items": [{"label": "禁用词",
                "detail": "命中 6 处：第 58 句「所有」、第 92 句「所有」",
                "level": "fail", "soft": False},
               {"label": "总时长偏差",
                "detail": "预估 1894.8 秒 vs 目标 1500.0 秒，超出 394.8 秒",
                "level": "warn", "soft": True}]}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8812")
    ap.add_argument("--shot-dir", default="")
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

        batch_calls, issues_calls = [], []

        def stub(route):
            u = route.request.url
            if "/api/script/issues" in u:
                issues_calls.append(json.loads(route.request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(STUB_ISSUES))
                return
            if "/api/scripts" in u:
                body = STUB_SCRIPTS
            elif "/api/project" in u:
                body = ({"ok": True, "orphans": []} if "scope=orphans" in u
                        else STUB_PROJECTS)
            elif "/api/batch" in u:
                batch_calls.append(json.loads(route.request.post_data or "{}"))
                body = {"ok": True, "task_id": "SMOKE-BATCH", "total": 2}
            elif "/api/task/" in u:
                body = {"ok": True, "job": {
                    "id": "SMOKE-BATCH", "kind": "batch", "status": "done",
                    "stage": "完成", "progress": 1.0, "log": ["桩任务"],
                    "batch": {"index": 1, "total": 1, "no": "1"},
                    "result": {"done": ["1"], "failed": []}}}
            elif "/api/render" in u:
                batch_calls.append({"__single__": json.loads(
                    route.request.post_data or "{}")})
                body = {"ok": True, "task_id": "SMOKE-R"}
            else:
                route.continue_()
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))

        pg.route(re.compile(r"/api/"), stub)
        pg.goto(base + "/#render")
        pg.wait_for_timeout(1200)
        pg.evaluate("async () => { await loadProjects() }")
        pg.wait_for_timeout(400)
        pg.evaluate("async (pid) => { document.getElementById('r-project').value = pid;"
                    " await loadEpisodes('r'); }", "SMOKE-FAKE")
        pg.wait_for_timeout(400)

        pg.evaluate("() => pickAll('r', true)")
        pg.wait_for_timeout(200)
        check(pg.evaluate("() => picked('r').length") == 2,
              "勾上了两期（点合成前的状态）",
              "已选 %s 期" % pg.evaluate("() => picked('r').length"))
        pg.evaluate("() => document.getElementById('btn-render').click()")
        pg.wait_for_timeout(900)

        st = pg.evaluate(
            "() => { const m=document.getElementById('mask'); return ["
            "m.classList.contains('on'),"
            "document.getElementById('m-title').textContent,"
            "document.getElementById('m-body').innerText,"
            "document.getElementById('m-ok').textContent]; }")

        check(bool(issues_calls), "点合成先问一次脚本阶段的问题",
              "请求 %s" % json.dumps(issues_calls[:1], ensure_ascii=False))
        check(issues_calls and issues_calls[0].get("episodes") == ["1", "2"],
              "问的是勾上的那几期", "episodes=%s"
              % (issues_calls[0].get("episodes") if issues_calls else None))
        check(st[0], "有问题就弹框（不是直接开工）", "mask.on=%s" % st[0])
        check(st[1] == "脚本阶段的问题", "弹框标题", st[1])
        check("禁用词" in st[2] and "命中 6 处" in st[2],
              "问题逐条列出（带句子）", st[2].replace("\n", " | ")[:110])
        check("以当前稿子为准" in st[2],
              "框里写明这份记录不追踪后续修改",
              "已含该句" if "以当前稿子为准" in st[2] else st[2][-90:])
        check(st[3] == "仍然合成", "确认按钮写明是「仍然合成」", st[3])
        check(not batch_calls, "没确认之前不发合成请求",
              "已发 %d 个" % len(batch_calls))

        if args.shot_dir:
            pg.screenshot(path=os.path.join(args.shot_dir,
                                            "ui_script_issues.png"))

        pg.evaluate("() => document.getElementById('m-ok').click()")
        pg.wait_for_timeout(900)
        got = batch_calls[0] if batch_calls else {}
        check(bool(batch_calls), "确认后才发合成请求",
              "请求 %s" % json.dumps(got, ensure_ascii=False))
        check(got.get("ack_script_issues") is True,
              "确认这个动作留了痕（ack=true）",
              "ack=%s" % got.get("ack_script_issues"))
        check(pg.evaluate(
            "() => !document.getElementById('mask').classList.contains('on')"),
            "确认后弹框收起")
        check(not errs, "无页面报错", "；".join(errs[:3]))

        b.close()

    print("\n" + "=" * 60)
    print("合成前提醒：%d 项中 %d 项通过" % (len(ran), len(ran) - len(fails)))
    for f in fails:
        print("  FAIL %s" % f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
