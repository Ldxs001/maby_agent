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

"""字体下拉与单集收编的端到端验证（真浏览器 + 桩数据）。

字体这边要证：候选项真的带着各自的 font-family（不是全长一个样）、点了会把值
写回服务端、没装的点不动。单集这边要证：立项表单多一张「单集」卡片、选它时
计划期数与起始期号收起、卡片上不写「下一期」、脚本页下拉里还能选到它。

配置只桩 POST（GET 要留真的，页面加载就靠它）；项目读写全桩。
跑完不改服务端一个字节。

用法：
    python main.py --port 8812
    python tools/probes/font_single_e2e.py --url http://127.0.0.1:8812
"""

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

SINGLE_PROJECT = {
    "id": "SMOKE-SINGLE", "name": "独一期", "archived": False,
    "program_name": "我思故我写", "subtitle": "",
    "plan_mode": "single", "style_preset": "", "note": "",
    "episodes": [], "created": "2026-09-13 10:00:00",
    "progress": {"mode": "single", "mode_label": "单集", "single": True,
                 "done": 0, "next_episode": "1", "mapped": 0, "planned": 1,
                 "label": "尚未出片", "paradigm_label": "自适应（按结构推断）"},
}


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

        patches, creates = [], []

        def stub(route):
            u = route.request.url
            req = route.request
            if "/api/config" in u:
                if req.method != "POST":
                    route.continue_()          # GET 走真的：页面靠它渲染
                    return
                body = json.loads(req.post_data or "{}")
                patches.append(body.get("patch") or {})
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True,
                                               "values": body.get("patch") or {}}))
                return
            if "/api/project" in u:
                if req.method == "POST":
                    body = json.loads(req.post_data or "{}")
                    if body.get("action") == "create":
                        creates.append(body)
                        route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({"ok": True, "project": dict(
                                SINGLE_PROJECT, id="SMOKE-NEW",
                                plan_mode=body.get("plan_mode"))}))
                        return
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "project": SINGLE_PROJECT}))
                    return
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True,
                                               "projects": [SINGLE_PROJECT]}))
                return
            route.continue_()

        pg.route("**/api/config**", stub)
        pg.route("**/api/project**", stub)
        pg.goto(base, wait_until="networkidle")
        pg.evaluate("async () => { await loadProjects(); loadConfig() }")
        pg.wait_for_timeout(1200)

        # ---------------- 字体下拉 ----------------
        pg.evaluate("() => showTab('config')")
        pg.wait_for_timeout(600)

        ids = pg.evaluate("""() => {
          const ids = ['f_cfg_frame__font_family','f_cfg_subtitle__font_family'];
          const card = Array.from(document.querySelectorAll('.tab')).map(
            s => (s.textContent || '').indexOf('画面字体') >= 0);
          return [ids.filter(i => !!document.getElementById(i)), card.some(Boolean)];
        }""")
        check(len(ids[0]) == 2, "配置页有画面字体与字幕字体两个下拉",
              ", ".join(ids[0]))
        check(ids[1], "两款字体在同一张「字体」卡片里（同屏可见）", str(ids[1]))

        body = pg.evaluate("""() => {
          const out = {};
          for (const id of ['f_cfg_frame__font_family','f_cfg_subtitle__font_family']) {
            const n = document.getElementById(id);
            const items = Array.from(n.querySelectorAll('.fopt'));
            out[id] = items.map(o => ({
              v: o.dataset.v, fam: o.dataset.fam, off: o.classList.contains('off'),
              css: (o.querySelector('.fs') || {style: {}}).style.fontFamily || '',
              txt: ((o.querySelector('.fn') || {}).textContent || '').trim(),
            }));
          }
          return out;
        }""")

        for key, items in body.items():
            who = key.split("__")[-1]
            with_fam = [o for o in items if o["fam"]]
            styled = [o for o in with_fam if o["css"]]
            carries = [o for o in styled if o["fam"] in o["css"]]
            check(len(styled) == len(with_fam),
                  "每个候选项都按各自字形渲染（%s）" % who,
                  "%d / %d 项带 font-family" % (len(styled), len(with_fam)))
            check(len(carries) == len(with_fam),
                  "样本用的就是该款字体本身（%s）" % who,
                  "%d / %d 项族名对得上" % (len(carries), len(with_fam)))
            check(len(styled) == len(items) - 1 or items[0]["v"] == "",
                  "首项是「自动选择」（不指定字体）（%s）" % who,
                  items[0]["txt"] if items else "")
            check(any(o["off"] for o in items),
                  "本机没装的照列但暗显（%s）" % who,
                  "暗显 %d 项" % len([o for o in items if o["off"]]))

        # 点一个已装的候选 → 发 patch 且键名正确
        patches.clear()
        picked = pg.evaluate("""() => {
          const n = document.getElementById('f_cfg_frame__font_family');
          // 跳过首项「自动选择」：它是空值，写回空串与「没点」看起来一样，
          // 验不出「点中的是哪一款」这件事。
          const list = n.querySelectorAll('.fopt:not(.off)');
          const t = list[1] || list[0];
          n.querySelector('.fsel').click();
          t.click();
          return [t.dataset.v, t.dataset.fam];
        }""")
        pg.wait_for_timeout(700)
        check(patches and "frame.font_family" in patches[-1],
              "点选画面字体写回 frame.font_family",
              json.dumps(patches[-1] if patches else {}, ensure_ascii=False))
        check(patches and patches[-1].get("frame.font_family") == picked[0],
              "写回的就是点中的那一款", "点中 %r / 写回 %r"
              % (picked[0], (patches[-1] or {}).get("frame.font_family")))

        # 未装的那一项点不动
        patches.clear()
        off_click = pg.evaluate("""() => {
          const n = document.getElementById('f_cfg_frame__font_family');
          const off = n.querySelector('.fopt.off');
          if (!off) return 'none';
          n.querySelector('.fsel').click();
          off.click();
          return off.dataset.v;
        }""")
        pg.wait_for_timeout(600)
        check(not patches, "点未装的候选不发请求（暗显即不可选）",
              "点的是 %r / 请求 %d 次" % (off_click, len(patches)))

        # 收起后那一行的样本字体跟着走
        shown = pg.evaluate("""() => {
          const n = document.getElementById('f_cfg_frame__font_family');
          const s = n.querySelector('.fsel .fsample');
          return [n.dataset.v, s ? s.style.fontFamily : null];
        }""")
        check(shown[1] and picked[1] in shown[1],
              "收起状态那一行显示的是选中的字形",
              "值 %r / 样式 %r" % (shown[0], shown[1]))

        # ---------------- 单集 ----------------
        pg.evaluate("() => showTab('project')")
        pg.wait_for_timeout(700)

        modes = pg.evaluate("""() => {
          const b = document.getElementById('np-mode');
          return Array.from(b.querySelectorAll('button')).map(x => x.dataset.mode);
        }""")
        check("single" in modes, "立项表单有「单集」这一张卡", str(modes))

        fields = pg.evaluate("""() => {
          const b = document.getElementById('np-mode');
          const g = id => { const n = document.getElementById(id); return n ? n.style.display : 'missing'; };
          b.querySelector('button[data-mode="single"]').click();
          const single = [g('np-planned-wrap'), g('np-first-wrap')];
          b.querySelector('button[data-mode="mapped"]').click();
          const mapped = [g('np-planned-wrap'), g('np-first-wrap')];
          return [single, mapped];
        }""")
        check(fields[0] == ["none", "none"], "选单集后收起计划期数与起始期号",
              str(fields[0]))
        check(fields[1] == ["", ""], "切回成稿规划再展开", str(fields[1]))

        creates.clear()
        pg.evaluate("""() => {
          const b = document.getElementById('np-mode');
          b.querySelector('button[data-mode="single"]').click();
          document.getElementById('np-name').value = '独一期';
          document.getElementById('np-program').value = '我思故我写';
          createProject();
        }""")
        pg.wait_for_timeout(900)
        check(creates and creates[-1].get("plan_mode") == "single",
              "立项请求带 plan_mode=single",
              json.dumps(creates[-1] if creates else {}, ensure_ascii=False))

        # 卡片：单集不写「下一期」
        pg.wait_for_timeout(600)
        card = pg.evaluate("""() => {
          const n = document.querySelector('.proj');
          return n ? n.textContent : '';
        }""")
        check("下一期" not in card, "单集卡片不写「下一期」",
              (card or "").strip()[:90])
        check("改下一期号" not in card, "单集卡片没有「改下一期号」按钮",
              (card or "").strip()[:90])
        check("单集" in card, "单集卡片标出「单集」", (card or "").strip()[:60])

        # 脚本页下拉：单集项目可选，且不再有「不归属项目」这一项
        pg.evaluate("() => showTab('script')")
        pg.wait_for_timeout(900)
        sel = pg.evaluate("""() => {
          const n = document.getElementById('s-project');
          if (!n) return null;
          return Array.from(n.querySelectorAll('option')).map(
            o => [o.value, o.textContent.trim()]);
        }""")
        check(sel is not None and any(v == "SMOKE-SINGLE" for v, _ in sel),
              "脚本页项目下拉里能选到单集项目",
              json.dumps(sel, ensure_ascii=False)[:160])
        check(all("不归属项目" not in t for _v, t in (sel or [])),
              "下拉里不再有「不归属项目·单集模式」这一项",
              json.dumps(sel, ensure_ascii=False)[:160])

        pg.wait_for_timeout(300)
        check(not errs, "无页面报错", " / ".join(errs[:3]))
        b.close()

    print("\n" + "=" * 60)
    if fails:
        print("字体下拉与单集收编：%d 项中 %d 项未过" % (len(ran), len(fails)))
        for f in fails:
            print("  FAIL " + f)
        return 1
    print("字体下拉与单集收编：%d 项全部通过" % len(ran))
    return 0


if __name__ == "__main__":
    sys.exit(main())
