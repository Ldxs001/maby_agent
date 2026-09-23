#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探针：确认「标准语速 + 音色比例」在真实页面上的样子。

只看不改，跑完即弃。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from playwright.sync_api import sync_playwright   # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8833"
SHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1500, "height": 1100})

    pg.goto(URL + "/#config")
    pg.wait_for_timeout(2500)
    rows = pg.evaluate(
        "() => Array.from(document.querySelectorAll('#stage-config .f'))"
        ".filter(w => w.querySelector('.ratio-note'))"
        ".map(w => { const sel = w.querySelector('select');"
        "  const note = w.querySelector('.ratio-note');"
        "  const lab = w.querySelector('label span');"
        "  return [lab ? lab.textContent.trim() : '', sel ? sel.value : '',"
        "          getComputedStyle(w).display !== 'none',"
        "          note ? note.textContent : '']; })")
    print("音色行（配置页）—— 标签 / 当前音色 / 是否露出 / 比例行")
    for r in rows:
        print("  %-22s %-24s 露出=%-5s %s" % tuple(r))
    print("\nCFG.standard =", pg.evaluate("() => JSON.stringify(CFG.standard)"))
    print("CFG.ratios   =", pg.evaluate("() => JSON.stringify(CFG.ratios)"))
    ring = pg.locator("#stage-config .f").filter(has=pg.locator(".ratio-note")).first
    ring.locator("xpath=ancestor::div[contains(@class,'card')][1]").screenshot(
        path=os.path.join(SHOT, "probe_config_ratio.png"))

    # 只在浏览器里改内存值并重绘，不发 POST：探针不得动用户的 config.json。
    print("\n（只改内存值，不动 config.json）切到 Edge 后：")
    pg.evaluate("() => { CFG.values['tts.engine'] = 'edge'; renderConfig(); }")
    pg.wait_for_timeout(600)
    rows2 = pg.evaluate(
        "() => Array.from(document.querySelectorAll('#stage-config .f'))"
        ".filter(w => w.querySelector('.ratio-note'))"
        ".map(w => { const sel = w.querySelector('select');"
        "  const note = w.querySelector('.ratio-note');"
        "  const lab = w.querySelector('label span');"
        "  return [lab ? lab.textContent.trim() : '', sel ? sel.value : '',"
        "          getComputedStyle(w).display !== 'none',"
        "          note ? note.textContent : '']; })")
    for r in rows2:
        print("  %-22s %-24s 露出=%-5s %s" % tuple(r))
    pg.locator("#stage-config .f").filter(has=pg.locator(".ratio-note")).first.locator(
        "xpath=ancestor::div[contains(@class,'card')][1]").screenshot(
        path=os.path.join(SHOT, "probe_config_ratio_edge.png"))

    pg.goto(URL + "/#script")
    pg.wait_for_timeout(2500)
    print("\n脚本页「字与时间」卡：")
    print("  口径行 =", pg.evaluate(
        "() => (document.getElementById('standard-box')||{}).textContent"))
    print("  音色名出现在该卡里 =", pg.evaluate(
        "() => { const c = document.getElementById('standard-box');"
        "  const g = document.getElementById('gauge');"
        "  const t = (c?c.textContent:'') + (g?g.textContent:'');"
        "  return ['Serena','Vivian','Xiaoxiao','Uncle'].filter(v => t.indexOf(v) >= 0); }"))
    # 拿一份假估算喂进 gauge：只为看清四格里第四格现在报的是什么（真跑一次要 LLM）
    print("  gauge 四格（喂一份假估算）= ", pg.evaluate(
        "() => { renderGauge({total_chars: 812, line_count: 46, total_seconds: 244.6,"
        "  target_seconds: 240.0, deviation_pct: 1.9},"
        "  {options: [{speed: 1.0, effective_chars: 1054},"
        "   {speed: 1.2, effective_chars: 1265}]});"
        "  return Array.from(document.querySelectorAll('#gauge .g'))"
        "  .map(x => x.textContent.trim()); }"))
    print("  反推那行 =", pg.evaluate(
        "() => document.getElementById('hint').textContent"))
    pg.screenshot(path=os.path.join(SHOT, "probe_script_standard.png"))
    pg.locator("#standard-box").locator(
        "xpath=ancestor::div[contains(@class,'card')][1]").screenshot(
        path=os.path.join(SHOT, "probe_script_card.png"))
    b.close()
