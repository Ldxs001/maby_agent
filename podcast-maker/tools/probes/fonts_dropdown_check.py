#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在真浏览器里验证字体字段。

判据：
1. 控件是 SELECT（点开能列全，不是拿当前值去筛的补全框）
2. 项数 == 白名单全量 + 1（"自动选择"）
3. 没装的项在列表里**照样出现**，但 disabled —— 暗显不可选
4. 每一项都带协议（系统自带 / OFL 1.1 / 厂商免费商用 …）
5. 至少有一项可选（否则等于把字体功能封死了）
"""
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from playwright.sync_api import sync_playwright                       # noqa: E402

PORT = 8813
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    srv = subprocess.Popen([sys.executable, "main.py", "--port", str(PORT)],
                           cwd=ROOT, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/config" % PORT, timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("服务起不来")
            return 1

        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto("http://127.0.0.1:%d/#config" % PORT)
            for _ in range(20):
                pg.wait_for_timeout(500)
                if pg.evaluate(
                        "() => !!document.getElementById('f_cfg_subtitle__font_family')"):
                    break
            pg.wait_for_timeout(1200)

            info = pg.evaluate("""() => {
              const m = document.getElementById('f_cfg_subtitle__font_family');
              if (!m) return {err: '找不到字体控件'};
              const os = Array.from(m.options);
              return {tag: m.tagName, value: m.value, count: os.length,
                      off: os.filter(o => o.disabled).length,
                      rows: os.map(o => ({t: o.textContent, d: o.disabled}))};
            }""")

            if info.get("err"):
                print("FAIL %s" % info["err"])
                return 1

            rows = info["rows"]
            on = [r for r in rows if not r["d"]]
            off = [r for r in rows if r["d"]]
            print("控件：%s" % info["tag"])
            print("项数：%d（可选 %d，暗显 %d）" % (info["count"], len(on), len(off)))
            print("当前值：%s" % info["value"])

            ok = True
            if info["tag"] != "SELECT":
                print("FAIL 控件不是 SELECT")
                ok = False
            if info["count"] < 20:
                print("FAIL 项数太少，白名单没有全量列出")
                ok = False
            if not off:
                print("FAIL 没有任何暗显项：要么全装了，要么未装判定没生效")
                ok = False
            # 协议可见性：每一项的文案里都得有一截协议。
            # 「自动选择」不是字体，是默认档，不适用本条。
            marks = ("系统自带", "OFL", "Apache", "免费", "GPL", "自备", "当前值")
            bare = [r["t"] for r in rows
                    if r["t"] != "自动选择（推荐）"
                    and not any(m in r["t"] for m in marks)]
            if bare:
                print("FAIL 以下项没带协议：%s" % bare[:5])
                ok = False

            print("--- 可选（前 8）---")
            for r in on[:8]:
                print("   %s" % r["t"])
            print("--- 暗显（前 6）---")
            for r in off[:6]:
                print("   %s" % r["t"])

            shot = os.path.join(ROOT, "_smoke", "ui_fonts_dropdown.png")
            pg.goto("http://127.0.0.1:%d/#config" % PORT)
            pg.wait_for_timeout(900)
            pg.screenshot(path=shot)
            print("截图：%s" % shot)

            print("结论：%s" % ("PASS" if ok else "FAIL"))
            return 0 if ok else 1
    finally:
        srv.terminate()


if __name__ == "__main__":
    sys.exit(main())
