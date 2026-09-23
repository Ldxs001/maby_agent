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
    pg.on("console", lambda m: print("CONSOLE[%s]:" % m.type, m.text) if m.type in ("error",) else None)

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
    pg.goto("http://127.0.0.1:8813/#render")
    pg.wait_for_timeout(1500)
    print("--- routed urls ---")
    for s in seen[-12:]:
        print("   ", s)
    print("--- before loadProjects ---")
    print(pg.evaluate("() => [PROJECTS.map(p=>p.id), ACTIVE_PROJ]"))
    pg.evaluate("async () => { await loadProjects() }")
    pg.wait_for_timeout(600)
    print("--- after loadProjects ---")
    print(pg.evaluate("""() => {
      const sel = document.getElementById('r-project');
      return [PROJECTS.map(p=>p.id), ACTIVE_PROJ, sel.value,
              Array.from(sel.options).map(o=>o.value+'|'+o.textContent)];
    }"""))
    pg.evaluate("() => { document.getElementById('r-project').value='SMOKE-FAKE'; }")
    print("--- after set value ---")
    print(pg.evaluate("() => document.getElementById('r-project').value"))
    print(pg.evaluate("""async () => {
      await loadEpisodes('r');
      const box = document.getElementById('r-eps-box');
      return [EPISODES['r'].length, EPISODES['r'].map(x=>x.no),
              box.querySelectorAll('.pickrow').length,
              getComputedStyle(document.getElementById('r-eps-wrap')).display];
    }"""))
    b.close()
