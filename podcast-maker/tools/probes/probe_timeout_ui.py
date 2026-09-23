#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截「语言模型」那张卡：两道超时闸门要并排看得见，标签与说明也要读得懂。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from playwright.sync_api import sync_playwright        # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8834"
SHOT = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"), "probe_llm_card.png")

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto(URL + "/#config")
    pg.wait_for_timeout(2500)
    rows = pg.evaluate(
        "() => Array.from(document.querySelectorAll('#stage-config .f'))"
        ".filter(w => (w.textContent||'').indexOf('超时') >= 0)"
        ".map(w => w.textContent.replace(/\\s+/g, ' ').trim().slice(0, 120))")
    for r in rows:
        print("  行：", r)
    card = pg.locator("#stage-config .f").filter(has_text="总时限").first.locator(
        "xpath=ancestor::div[contains(@class,'card')][1]")
    card.screenshot(path=SHOT)
    print("  截图：", SHOT)
    b.close()
