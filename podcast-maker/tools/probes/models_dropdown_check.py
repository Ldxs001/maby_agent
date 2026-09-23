#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在真浏览器里验证模型字段的下拉。

判据：点开（= 读 options）能看到全部候选，而不是只剩当前值那一项。
顺带调 tools/ui_smoke.py 跑一遍完整界面冒烟。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from playwright.sync_api import sync_playwright                       # noqa: E402

PORT = 8812
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
            for _ in range(20):            # 等首屏配置渲染 + 模型列表取回来
                pg.wait_for_timeout(500)
                if pg.evaluate("() => !!document.getElementById('f_cfg_llm__model')"):
                    break
            pg.wait_for_timeout(1500)

            info = pg.evaluate("""() => {
              const m = document.getElementById('f_cfg_llm__model');
              if (!m) return {err: '找不到模型控件'};
              return {tag: m.tagName, value: m.value,
                      count: m.options.length,
                      opts: Array.from(m.options).map(o => o.textContent)};
            }""")
            print("控件：%s" % info.get("tag"))
            print("当前值：%s" % info.get("value"))
            print("下拉里能看到 %s 项：" % info.get("count"))
            for o in (info.get("opts") or []):
                print("   - %s" % o)
            pg.evaluate("() => { const m = document.getElementById('f_cfg_llm__model');"
                        " if (m) m.scrollIntoView({block: 'center'}); }")
            pg.wait_for_timeout(400)
            pg.screenshot(path=os.path.join(ROOT, "_smoke",
                                            "ui_models_dropdown.png"),
                          full_page=False)
            b.close()

        print("\n=== tools/ui_smoke.py ===")
        r = subprocess.run([sys.executable, "tools/ui_smoke.py",
                            "--url", "http://127.0.0.1:%d" % PORT,
                            "--shot-dir", "_smoke"],
                           cwd=ROOT)
        print("ui_smoke 退出码：%d" % r.returncode)
        return r.returncode
    finally:
        srv.terminate()


if __name__ == "__main__":
    sys.exit(main())
