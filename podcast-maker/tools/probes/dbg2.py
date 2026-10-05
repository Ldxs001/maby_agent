# -*- coding: utf-8 -*-
import json, re
from playwright.sync_api import sync_playwright

STUB_PROJECTS = {"ok": True, "projects": [
    {"id": "SMOKE-FAKE", "name": "冒烟桩项目", "archived": False,
     "progress": {"mode": "episodic", "next_episode": "1", "done": 0, "mapped": 0}}]}
STUB_SCRIPTS = {"ok": True, "project_id": "SMOKE-FAKE", "items": [
    {"no": "1", "title": "第一期", "done": True, "has_script": True, "lines": 20},
    {"no": "2", "title": "第二期", "done": False, "has_script": True, "lines": 18},
    {"no": "3", "title": "第三期", "done": False, "has_script": False, "lines": 0}]}

seen = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1620, "height": 1180})
    pg.on("pageerror", lambda e: print("PAGEERROR:", e))

    # --- 复现冒烟的导航前缀 ---
    pg.goto("http://127.0.0.1:8813/#config"); pg.wait_for_timeout(2600)
    for stage in ("script", "render"):
        pg.goto("http://127.0.0.1:8813/#" + stage); pg.wait_for_timeout(1500)
    pg.goto("http://127.0.0.1:8813/#project"); pg.wait_for_timeout(1500)
    pg.goto("http://127.0.0.1:8813/#script"); pg.wait_for_timeout(1500)
    # openSources + closeModal，同冒烟
    proj = json.load(open("projects/_projects.json", encoding="utf-8"))["projects"]
    pid0 = proj[0]["id"]
    pg.evaluate("async (p) => { await openSources(p) }", pid0)
    pg.wait_for_timeout(900)
    pg.evaluate("() => closeModal()")
    print("前缀跑完，当前 PROJECTS =", pg.evaluate("() => PROJECTS.map(p=>p.id)"))
    print("hash =", pg.evaluate("() => location.hash"))
    print("ACTIVE_PROJ =", pg.evaluate("() => ACTIVE_PROJ"))

    def stub(route):
        u = route.request.url
        seen.append(u.split("127.0.0.1:8813")[-1])
        if "/api/scripts" in u:
            body = STUB_SCRIPTS
        elif "/api/project" in u:
            body = {"ok": True, "orphans": []} if "scope=orphans" in u else STUB_PROJECTS
        else:
            route.continue_(); return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    pg.route(re.compile(r"/api/"), stub)
    pg.goto("http://127.0.0.1:8813/#render"); pg.wait_for_timeout(1200)
    pg.evaluate("async () => { await loadProjects() }")
    pg.wait_for_timeout(500)
    print("--- 桩后 ---")
    print("PROJECTS =", pg.evaluate("() => PROJECTS.map(p=>p.id)"))
    print(pg.evaluate("""() => {
      const sel = document.getElementById('r-project');
      return [sel.value, Array.from(sel.options).map(o=>o.value)];
    }"""))
    print("路由命中的 /api 请求：")
    for s in seen:
        print("   ", s)
    b.close()
