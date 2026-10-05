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

"""界面冒烟：在真实浏览器里验证「控件真的接上了」。

单元测试跑在 Python 里，看不见界面；而最贵的两类界面缺陷——控件找不到自己、
下拉只剩英文裸值——只在浏览器里才现形。所以这一层必须用真浏览器跑。

用法：
    python main.py --port 8812
    python tools/ui_smoke.py --url http://127.0.0.1:8812 --shot-dir _smoke

判据（任一不满足即非零退出）：
    1. 无 console error / pageerror
    2. 配置页控件数量与后端下发的点位数一致
    3. 拖动滑块后，服务端配置真的变了（回读确认）
    4. 枚举下拉的选项标签不是英文裸值
    5. 每个控件都能被读到值（不存在空节点）
    6. 控件 id 不含点号（点号会被当成类选择符）
    7. 合成页只列有脚本的期（没脚本的期不进可选项）
    8. 选期表：全选 / 清空 / 单勾与计数一致，无归属项目时整个收起
    9. 勾了多期走批任务，不落到单期接口
   10. 门禁在接口层也拦得住（缺脚本的批合成、空脚本存盘、中止不存在的任务）
   11. 插入面板自带入库区；已进地图的素材默认不勾并标出；一份素材都没有时
       面板照样开得出来
   12. 配置页的排布：一格一控件（不跨列）、一行里形态不混（滑杆／开关／下拉／
       输入各占各的行）、行尾的空档不被后面的控件回头填上
   13. 卡内功能区：一张卡讲几件事时，一件事一块网格、配一行小标题；只讲一件事的
       卡不画标题
   14. 路径类点位（片头音频、立绘 PNG、自备音乐…）都说明「本机绝对路径」并带
       「选择…」按钮；点它——接口被拦下来时——选中的路径会写回服务端
   15. 批任务跑着的时候，开工按钮与中止按钮跟任务状态走：跑着就摁不动、收工才
       恢复（轮询一次就交差的话，按钮在任务刚起步时就亮了，看着像已经收工）
"""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.request

from playwright.sync_api import sync_playwright

JS_BAD_READ = r"""() => {
  const out = [];
  for (const k in REG) {
    for (const it of REG[k]) {
      if (!it.node) out.push(k + ' node-null');
      else if (it.kind === 'ctl' || it.kind === 'range') {
        if (it.node.value === undefined) out.push(k + ' no-value');
      }
    }
  }
  return out.slice(0, 12);
}"""

JS_DOTTED = r"""() => Array.from(document.querySelectorAll('[id]'))
  .map(n => n.id).filter(s => s.indexOf('.') >= 0).slice(0, 8)"""

# 界面里每一个控件都必须在登记表 REG 里。登记表按节点存活状态清理，
# 一旦某次清理把还挂在文档里的控件一起摘掉，这里就会露出「有控件、没登记」。
JS_REG_COVER = r"""() => {
  const dom = Array.from(document.querySelectorAll(
    '#quick-script [id^=f_], #quick-render [id^=f_], #stage-config [id^=f_]'));
  const reg = new Set();
  for (const k in REG) for (const it of REG[k]) if (it.node && it.node.id) reg.add(it.node.id);
  const miss = dom.filter(n => !reg.has(n.id)).map(n => n.id);
  const extra = Array.from(reg).filter(id => !document.getElementById(id));
  return [dom.length, reg.size, miss.slice(0, 6), extra.slice(0, 6)];
}"""

# 配置页的排布规矩：一格一控件、一类控件一行。
# 这两条只在浏览器里量得出来（列宽、行高、谁跟谁在同一水平线上都是布局算出来的），
# Python 侧只能核对类名有没有发，量不出「两个开关后面紧跟的下拉有没有顶上来」。
JS_GRID = r"""() => {
  const kindOf = f => {
    if (f.querySelector('input[type=range]')) return '滑杆';
    if (f.querySelector('.sw')) return '开关';
    if (f.querySelector('.fpick')) return '下拉';   // 字体是自绘下拉，不带 select
    if (f.querySelector('select')) return '下拉';
    if (f.querySelector('textarea')) return '多行';
    if (f.querySelector('input[type=number]')) return '数字';
    return '输入';
  };
  const bad = [];
  document.querySelectorAll('#stage-config .card').forEach(card => {
    const g = card.querySelector('.grid.g4');
    if (!g) return;
    const sec = ((card.querySelector('h2') || {}).textContent || '?').trim();
    const items = Array.from(g.children).filter(n => n.classList.contains('f'))
      .filter(n => n.offsetParent !== null);          // 收起的那套音色不参与
    if (!items.length) return;
    const tracks = getComputedStyle(g).gridTemplateColumns.split(' ').length;
    const box = g.getBoundingClientRect();
    const colw = box.width / tracks;                  // 含列间距：第 i 格左缘＝左起 i×colw
    const rows = new Map();
    items.forEach(f => {
      const r = f.getBoundingClientRect();
      const key = Math.round(r.top / 6);              // 同一行的顶几乎相等，留 6px 抵亚像素
      if (!rows.has(key)) rows.set(key, []);
      rows.get(key).push({f: f, left: r.left, w: r.width, k: kindOf(f),
        label: ((f.querySelector('label span') || {}).textContent || '?').trim()});
    });
    Array.from(rows.values()).forEach(list => {
      const kinds = Array.from(new Set(list.map(x => x.k)));
      if (kinds.length > 1)
        bad.push(sec + ' 一行里混了形态：' +
          list.map(x => x.label + '(' + x.k + ')').join(' + '));
      list.sort((a, b) => a.left - b.left);
      let cursor = 0;
      list.forEach(x => {
        const col = Math.round((x.left - box.left) / colw);
        if (col !== cursor)
          bad.push(sec + ' ' + x.label + ' 落在第 ' + (col + 1) + ' 格，'
            + '应第 ' + (cursor + 1) + ' 格——行尾的空档被后面的控件顶上来填了');
        const span = Math.round(x.w / colw);
        if (span > (x.k === '多行' ? 2 : 1))
          bad.push(sec + ' ' + x.label + ' 跨了 ' + span + ' 列');
        cursor += span;
      });
    });
  });
  return bad.slice(0, 8);
}"""

# 卡内功能区：一个功能区一块网格，多区才画小标题。少画一块网格＝那几项挤在别的区
# 里；多画一行小标题而只有一块网格＝把简单卡片也套上了标题。
JS_ZONE = r"""() => {
  const bad = [];
  document.querySelectorAll('#stage-config .card').forEach(card => {
    const sec = ((card.querySelector('h2') || {}).textContent || '?').trim();
    const grids = Array.from(card.querySelectorAll(':scope > .grid.g4'));
    const heads = Array.from(card.querySelectorAll(':scope > .zhead'));
    if (!grids.length) { bad.push(sec + ' 一块网格都没有'); return; }
    if (heads.length && heads.length !== grids.length)
      bad.push(sec + ' 小标题 ' + heads.length + ' 个 / 网格 ' + grids.length
        + ' 块——两者必须一一对应');
    if (!heads.length && grids.length > 1)
      bad.push(sec + ' 有多块网格却没画小标题，人看不出这是两件事');
  });
  return bad.slice(0, 6);
}"""

# 路径类点位：占位符要说清「本机绝对路径」，旁边要有「选择…」。
JS_PATHBTN = r"""() => {
  const bad = [];
  const nodes = Array.from(
    document.querySelectorAll('#stage-config [id^=f_cfg_]'));
  const paths = nodes.filter(n => n.tagName === 'INPUT'
    && n.getAttribute('placeholder')
    && n.getAttribute('placeholder').indexOf('本机绝对路径') === 0);
  paths.forEach(n => {
    const f = n.closest('.f');
    const btn = f ? f.querySelector('[data-pick]') : null;
    if (!btn) { bad.push(n.id + ':没有「选择…」按钮'); return; }
    if (btn.textContent.replace(/\s/g, '').indexOf('选择') < 0)
      bad.push(n.id + ':按钮文字不是「选择…」');
  });
  return [paths.length, bad.slice(0, 6)];
}"""

JS_ENUM = r"""() => {
  const keys = ['script.style_preset','audio.sample_rate','video.fps',
                'video.encoder_preset','audio.channels','audio.codec',
                'tts.engine','llm.backend','subtitle.preset','cover.preset'];
  const en = ['argument','story','science','debate','review','ultrafast',
              'veryfast','medium','slow','edge','qwen3tts','lm-studio',
              'ollama','custom','single','dual','minimal','book','episode'];
  const bad = [];
  for (const k of keys) {
    const n = document.getElementById('f_cfg_' + k.replace(/\./g, '__'));
    if (!n) { bad.push(k + ':missing'); continue; }
    if (!n.options.length) { bad.push(k + ':empty'); continue; }
    for (const o of Array.from(n.options)) {
      if (en.indexOf(o.textContent.trim()) >= 0) { bad.push(k + '=' + o.textContent.trim()); break; }
    }
  }
  return bad;
}"""

# 字幕预览：两档是**同一个框**，只有轴不同；框与裁切矩形同四点；配色必须是合法
# CSS 色。最后这条不是吹毛求疵——`subtitle.color_a` 存的是 ASS 的 `&HAABBGGRR`，
# 直接喂给 SVG 的 fill 是非法值，浏览器丢掉它、回落到黑色，深色框里的「非当前句」
# 就成了看不见的字，而界面上不会有任何报错（`&HFFFFFF` 倒过来还是白，白字下看不出）。
JS_SUBPREV = r"""() => {
  const HEX = /^#[0-9A-Fa-f]{6}$/;
  const bad = [];
  const svgs = Array.from(document.querySelectorAll('.sub-prev svg'));
  if (svgs.length !== 2) return ['预览格数 ' + svgs.length + '（应为 2）'];
  for (const svg of svgs) {
    const box = svg.querySelector('rect[stroke]');
    const clip = svg.querySelector('clipPath rect');
    if (!box) { bad.push('没有框'); continue; }
    if (!clip) { bad.push('框没有配裁切矩形'); continue; }
    for (const a of ['x', 'y', 'width', 'height']) {
      if (box.getAttribute(a) !== clip.getAttribute(a)) {
        bad.push('框与裁切矩形的 ' + a + ' 不一致');
      }
    }
    const texts = Array.from(svg.querySelectorAll('text'));
    if (!texts.length) { bad.push('没画字'); continue; }
    for (const t of texts) {
      const f = t.getAttribute('fill') || '';
      if (!HEX.test(f)) bad.push('fill 不是 #RRGGBB：' + f);
    }
  }
  return bad;
}"""

# 选期表的桩数据：三期里有两期有脚本、一期没有。合成页只该列出前两期——
# 「选了一期却没有脚本」这个状态要在入口就产生不了，而不是等合成报错。
STUB_PROJECTS = {"ok": True, "projects": [
    {"id": "SMOKE-FAKE", "name": "冒烟桩项目", "archived": False,
     "progress": {"mode": "episodic", "next_episode": "1", "done": 0, "mapped": 0}},
]}
STUB_SCRIPTS = {"ok": True, "project_id": "SMOKE-FAKE", "items": [
    {"no": "1", "title": "第一期", "done": True, "has_script": True, "lines": 20},
    {"no": "2", "title": "第二期", "done": False, "has_script": True, "lines": 18},
    {"no": "3", "title": "第三期", "done": False, "has_script": False, "lines": 0},
]}

# 插入面板的桩：两份素材，s1 已经排进地图、s2 没有。判据靠这个区别——
# 已在地图里的那份若被默认勾上，等于把同一本书再插一遍。
STUB_INS_SOURCES = {"ok": True, "project_id": "SMOKE-FAKE", "sources": [
    {"id": "s1", "name": "已排进地图的书.md", "chars": 273753, "sections": 756,
     "probe": {"segments": 241, "levels": 3, "unit_level": 2, "kind_label": "书籍"}},
    {"id": "s2", "name": "新佐证.md", "chars": 12000, "sections": 30},
]}
STUB_INS_EMPTY = {"ok": True, "project_id": "SMOKE-FAKE", "sources": []}
STUB_MAP_PROJECT = {"ok": True, "projects": [
    {"id": "SMOKE-FAKE", "name": "冒烟桩项目", "archived": False,
     "map": {"episodes": [
         {"no": "1", "title": "第一期", "refs": [{"source": "s1", "anchor": "序言"}]},
         {"no": "2", "title": "第二期", "refs": [{"source": "s1", "anchor": "基石"}]}]},
     "progress": {"mode": "mapped", "next_episode": "1", "done": 0, "mapped": 2}},
]}

JS_PICK = r"""async (pid) => {
  const sel = document.getElementById('%(w)s-project');
  if (!sel) return ['no-select'];
  sel.value = pid;
  await loadEpisodes('%(w)s');
  const box = document.getElementById('%(w)s-eps-box');
  const rows = Array.from(box.querySelectorAll('.pickrow'));
  return [rows.length, rows.map(r => r.textContent).join('|'),
          EPISODES['%(w)s'].map(x => x.no + ':' + (x.has_script ? 1 : 0)).join(','),
          getComputedStyle(document.getElementById('%(w)s-eps-wrap')).display,
          (document.getElementById('%(w)s-eps-cnt') || {}).textContent || ''];
}"""

JS_MULTI = r"""() => {
  closePick();
  togglePick('r');
  const opened = document.getElementById('r-eps-box').classList.contains('on');
  togglePick('r');
  const closed = !document.getElementById('r-eps-box').classList.contains('on');
  pickAll('r', true);
  const all = picked('r'), cntAll = (document.getElementById('r-eps-cnt')||{}).textContent||'';
  pickAll('r', false);
  const none = picked('r'), cntNone = (document.getElementById('r-eps-cnt')||{}).textContent||'';
  toggleEp('r', '1', true);
  const one = picked('r'), cntOne = (document.getElementById('r-eps-cnt')||{}).textContent||'';
  pickAll('r', true);
  return [all.join(','), cntAll, none.length, cntNone, one.join(','), cntOne,
          opened, closed, picked('r').length];
}"""

# 收起状态：把项目下拉置空再重算一次，选期表该整个收起来——单集模式没有
# 「第几期」这回事，留着它等于问一个不成立的问题。
JS_PICK_HIDE = r"""async () => {
  const sel = document.getElementById('%(w)s-project');
  sel.value = '';
  await loadEpisodes('%(w)s');
  return [getComputedStyle(document.getElementById('%(w)s-eps-wrap')).display,
          document.getElementById('%(w)s-eps-box').querySelectorAll('.pickrow').length];
}"""


# 删除要点两下。第一下只点亮按钮，**一个请求都不该发出去**——这是全部动作里
# 唯一不可逆的一个，误触的代价是一整个项目目录（素材、地图、脚本、成品全在
# 里面）。所以这里不只要验「第二下真的删」，更要验「第一下什么都没做」。
JS_DEL_ARM = r"""() => {
  const btns = () => Array.from(document.querySelectorAll('#projects .proj .acts button'));
  const b = btns().find(x => x.textContent.trim() === '删除');
  if (!b) return ['no-button'];
  b.click();
  const armed = btns().filter(x => x.classList.contains('arm'));
  return [1, armed.length, armed.length ? armed[0].textContent.trim() : '',
          btns().map(x => x.textContent.trim()).join('|')];
}"""

JS_DEL_FIRE = r"""() => {
  const b = document.querySelector('#projects .proj .acts button.arm');
  if (!b) return ['no-arm'];
  b.click();
  return [1];
}"""

# 插入面板的三件事：入库区在面板里、已进地图的默认不勾、空库照样开得出来。
# 每段先清空 m-body 与 toasts：面板里残留的 #ins-src 会让勾选状态被上一次
# 的渲染带进来，而残留的红字会被当成这一次产生的。
JS_INS_PANEL = r"""async (pid) => {
  const mb = document.getElementById('m-body'); if (mb) mb.innerHTML = '';
  const ts = document.getElementById('toasts'); if (ts) ts.innerHTML = '';
  await openInsert(pid);
  const rows = Array.from(document.querySelectorAll('#ins-src .src-row'));
  return [document.getElementById('ins-file') ? 1 : 0,
          document.getElementById('ins-text') ? 1 : 0,
          document.getElementById('ins-add-btn') ? 1 : 0,
          rows.map(r => { const c = r.querySelector('input');
            return c.value + ':' + (c.checked ? 1 : 0) + ':'
                   + (r.textContent.indexOf('已在地图中') >= 0 ? 1 : 0); }).join('|')];
}"""

JS_INS_EMPTY = r"""async (pid) => {
  const mb = document.getElementById('m-body'); if (mb) mb.innerHTML = '';
  const ts = document.getElementById('toasts'); if (ts) ts.innerHTML = '';
  await openInsert(pid);
  const box = document.getElementById('ins-src');
  return [document.getElementById('ins-file') ? 1 : 0,
          box ? box.querySelectorAll('.src-row').length : -1,
          document.querySelectorAll('#toasts .toast.err').length];
}"""

# 地图已用素材的判定。空 refs 与没有 refs 的期都要跨过，否则一张刚排过、
# 还有几期没定落点的地图会让判定整体落空。
JS_MAP_SRC = r"""() => {
  const s = mapSourceIds({map: {episodes: [
    {refs: [{source: 's9'}, {source: 's1'}]}, {refs: [{source: 's2'}]},
    {refs: []}, {}]}});
  return [s.has('s1') ? 1 : 0, s.has('s2') ? 1 : 0,
          s.has('s9') ? 1 : 0, s.has('s3') ? 1 : 0];
}"""


def fetch(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url, body):
    """给门禁判据用：只发拒绝类请求，不触发任何真活。"""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8812")
    ap.add_argument("--shot-dir", default="")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    cfg = fetch(base + "/api/config")
    n_config = sum(len(v) for v in cfg["params"]["config"].values())
    fails = []
    ran = []

    def check(ok, label, detail=""):
        # 判据数由这里数：写进文档的数字必须来自工具，不靠人去点源码。
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

        # ---- 配置页：全部点位都必须有控件 ----
        pg.goto(base + "/#config")
        pg.wait_for_timeout(2600)
        got = pg.evaluate("document.querySelectorAll('#stage-config [id^=f_cfg_]').length")
        check(got == n_config, "配置页控件数量", "实际 %d / 应有 %d" % (got, n_config))

        bad_read = pg.evaluate(JS_BAD_READ)
        check(not bad_read, "每个控件都能被读到值", ", ".join(bad_read))

        dotted = pg.evaluate(JS_DOTTED)
        check(not dotted, "控件 id 不含点号", ", ".join(dotted))

        raw_en = pg.evaluate(JS_ENUM)
        check(not raw_en, "枚举下拉不是英文裸值", ", ".join(raw_en))

        # 排布：一格一控件、一类控件一行、行尾空着不回头填
        grid_bad = pg.evaluate(JS_GRID)
        check(not grid_bad, "配置页一格一控件、一类控件一行",
              "；".join(grid_bad))

        # 排布第二层：卡内功能区（一张卡讲几件事时，一件事一块网格 + 一行小标题）
        zone_bad = pg.evaluate(JS_ZONE)
        check(not zone_bad, "卡内功能区一块网格配一行小标题", "；".join(zone_bad))

        # 字幕预览：两档同一个框（框与裁切矩形同四点）、配色是合法 CSS 色、
        # 单行滚动档真的在滚。这里换的是版式档位，验完复原。
        keep_preset = pg.eval_on_selector("#f_cfg_subtitle__preset", "e=>e.value")
        for preset in ("single", "lyric"):
            pg.select_option("#f_cfg_subtitle__preset", preset)
            pg.wait_for_timeout(500)
            prev_bad = pg.evaluate(JS_SUBPREV)
            check(not prev_bad, "字幕预览画对了（%s）" % preset, "；".join(prev_bad))
            if preset == "single":
                rolling = pg.evaluate(
                    "() => Array.from(document.querySelectorAll('.sub-prev text'))"
                    ".filter(t => t.querySelector('animateTransform')).length")
                check(rolling == 2, "单行滚动档的预览在滚（两格都带动画）",
                      "带动画的字 %d 个" % rolling)
        pg.select_option("#f_cfg_subtitle__preset", keep_preset)
        pg.wait_for_timeout(300)

        # 路径类点位：说清要什么 + 给「选择…」
        n_path = sum(1 for sec in cfg["params"]["config"].values()
                     for it in sec if it["type"] == "path")
        pk = pg.evaluate(JS_PATHBTN)
        check(pk[0] == n_path and not pk[1], "路径框都带「选择…」与占位符",
              "认出 %d 个 / 配置里 %d 个；%s" % (pk[0], n_path,
                                                "；".join(pk[1]) or "无异常"))

        # 「选择…」的接线：按钮 → 接口 → 把路径写回该点位。真弹对话框会把跑冒烟的人
        # 卡住（对话框等人点），所以这里把接口拦下来，只验接线与写回这一段。
        pick_key = "speaker_indicator.portrait_a"
        pick_id = "f_cfg_" + pick_key.replace(".", "__")
        fake = os.path.join(tempfile.gettempdir(), "pm smoke 立绘.png")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("x")
        before_pick = fetch(base + "/api/config")["values"].get(pick_key) or ""
        pg.route("**/api/pickfile", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "path": fake, "cancelled": False})))
        try:
            pg.evaluate("(id) => document.querySelector('[data-pick=\"' + id + '\"]')"
                        ".click()", pick_id)
            pg.wait_for_timeout(1500)
            wrote = fetch(base + "/api/config")["values"].get(pick_key)
            shown = pg.evaluate("(id) => (document.getElementById(id) || {}).value",
                                pick_id)
            check(wrote == fake and shown == fake,
                  "「选择…」把选中的路径写回服务端",
                  "服务端 %r / 控件 %r" % (wrote, shown))
        finally:
            pg.unroute("**/api/pickfile")
            post(base + "/api/config", {"patch": {pick_key: before_pick}})
            os.unlink(fake)

        # 此时项目/脚本/合成/配置四处容器都已渲染过，可以做全覆盖核对
        cov = pg.evaluate(JS_REG_COVER)
        check(cov[0] == cov[1] and not cov[2] and not cov[3],
              "每个控件都在登记表里（无漏登记）",
              "DOM %d / 登记 %d；漏登记 %s；幽灵登记 %s"
              % (cov[0], cov[1], cov[2] or "无", cov[3] or "无"))

        # 项目页 / 脚本页 / 合成页
        for stage in ("script", "render"):
            pg.goto(base + "/#" + stage)
            pg.wait_for_timeout(1500)
            n = pg.evaluate("document.querySelectorAll('#tab-%s [id^=f_quick_]').length" % stage)
            if args.shot_dir:
                pg.screenshot(path=os.path.join(args.shot_dir, "ui_%s.png" % stage))
            check(n > 0, "%s 页有快捷控件" % stage, "实际 %d" % n)

        # 项目页没有配置点位，判据换成「立项表单能用」：风格下拉必须已被
        # 档位表填满，否则用户立不了项。
        pg.goto(base + "/#project")
        pg.wait_for_timeout(1500)
        npj = pg.evaluate(
            "() => { const n = document.getElementById('np-style');"
            " const f = ['np-name','np-program','np-planned','np-first','np-note']"
            "   .filter(i => !!document.getElementById(i)).length;"
            " return [n && n.options ? n.options.length : 0, f]; }")
        check(npj[0] >= 3 and npj[1] == 5, "项目页立项表单可用",
              "风格档位 %s 个 / 字段 %s 个" % (npj[0], npj[1]))

        # 素材类型必须能选：它决定排地图的组织依据（怎么切、怎么合、重点抓
        # 什么、靠什么承接）。选项来自范式层，少一张卡就会少一个选项。
        npa = pg.evaluate(
            "() => { const s = document.getElementById('np-paradigm');"
            " return [!!s, s && s.options ? s.options.length : 0]; }")
        check(npa[0] and npa[1] >= 8, "立项可选素材类型",
              "选项 %s 个" % npa[1])

        # 上传不接受 PDF：PDF 是渲染结果不是源，硬读只会读出乱序文本。
        # 界面留着它，等于承诺一个不存在的功能。
        acc = pg.evaluate(
            "() => { const i = document.getElementById('file-input');"
            " return i ? i.accept : ''; }")
        check(".pdf" not in str(acc), "上传不接受 PDF", str(acc))

        # 规划方式必须能选、且默认选中一个：立项时选了什么就是什么，之后不可改，
        # 所以这个选择器不是装饰品——缺了它，成稿规划这条路根本走不通。
        mode = pg.evaluate(
            "() => { const b = document.getElementById('np-mode');"
            " if (!b) return [0, 0];"
            " const on = b.querySelector('button.on');"
            " return [b.querySelectorAll('button').length, on ? 1 : 0]; }")
        check(mode[0] >= 3 and mode[1] == 1, "立项可选规划方式且默认选中一项",
              "选项 %s 个 / 已选 %s 个" % (mode[0], mode[1]))

        # 单集：选了它，计划期数与起始期号要收起来——单集没有第二期，留着这两个
        # 框就是「填了不生效」的死框。切回成稿规划要能再展开。
        single = pg.evaluate(
            "() => { const b = document.getElementById('np-mode');"
            " const g = id => { const n = document.getElementById(id);"
            "   return n ? n.style.display : 'missing'; };"
            " const click = m => { const x = b.querySelector('button[data-mode=\"'+m+'\"]');"
            "   if (!x) return false; x.click(); return true; };"
            " if (!click('single')) return ['missing'];"
            " const after = [g('np-planned-wrap'), g('np-first-wrap')];"
            " click('mapped');"
            " const back = [g('np-planned-wrap'), g('np-first-wrap')];"
            " return [after, back]; }")
        check(single[0] == ["none", "none"] and single[1] == ["", ""],
              "选单集收起计划期数与起始期号，切回成稿规划再展开", str(single))

        # 单集项目卡片不写「下一期」。它没有第二期，写一个出来就是错的——
        # 而单看卡片，那一行字与别的项目长得一模一样。
        card = pg.evaluate("""() => {
          const fake = {id: 'SMOKE-SINGLE', name: '独一期', created: '2026-09-13 10:00:00',
            program_name: '独一期', subtitle: '', style_preset: '', note: '',
            episodes: [], archived: false,
            progress: {mode: 'single', mode_label: '单集', single: true, done: 0,
                       next_episode: '1', label: '尚未出片', paradigm_label: '自适应'}};
          const keep = PROJECTS;
          try {
            PROJECTS = [fake]; renderProjects();
            const html = document.getElementById('projects').innerHTML;
            return [html.indexOf('下一期') >= 0, html.indexOf('单集') >= 0];
          } finally { PROJECTS = keep; renderProjects(); }
        }""")
        check(card[0] is False and card[1] is True,
              "单集卡片不写「下一期」，只标「单集」", str(card))
        if args.shot_dir:
            pg.screenshot(path=os.path.join(args.shot_dir, "ui_project.png"))

        # 脚本页必须能直接改称呼：「为什么叫小思和小笔、没法改名？」
        pg.goto(base + "/#script")
        pg.wait_for_timeout(1500)
        nm = pg.evaluate(
            "() => { const a = document.getElementById('f_quick_tts__name_a');"
            " const b = document.getElementById('f_quick_tts__name_b');"
            " return [a ? a.value : null, b ? b.value : null,"
            "         document.getElementById('f_quick_script__style_preset')"
            "           ? document.getElementById('f_quick_script__style_preset').options.length : 0]; }")
        check(nm[0] and nm[1], "脚本页可直接改 A/B 角称呼", "A=%s B=%s" % (nm[0], nm[1]))
        check(nm[2] >= 3, "脚本页风格下拉已填充", "档位 %s 个" % nm[2])

        # 脚本页必须能选归属项目：成稿规划的项目靠它把本期素材接到地图落点上。
        # 这个下拉一旦不在，生成脚本会直接报错，而配置页那几十项全都正常。
        sp = pg.evaluate(
            "() => { const n = document.getElementById('s-project');"
            " const nt = document.getElementById('s-proj-note');"
            " return [n ? n.tagName : null, n && n.options ? n.options.length : 0,"
            "         nt ? nt.textContent.trim().length : -1]; }")
        check(sp[0] == "SELECT" and sp[1] >= 1 and sp[2] > 0,
              "脚本页有归属项目下拉并给出说明",
              "%s / 选项 %s / 说明 %s 字" % (sp[0], sp[1], sp[2]))

        # 料源判据分两条路：成稿规划必须先排图才能选，逐期即兴立了项就能选。
        # 判据只有 planReady 一处，这里直接问它本身，四条输入覆盖两种范式。
        ready = pg.evaluate(
            "() => { const mk = (m, n) => ({progress: {mode: m, mapped: n}});"
            " return [planReady(mk('mapped', 0)), planReady(mk('mapped', 3)),"
            "         planReady(mk('episodic', 0)), planReady({}),"
            "         planReady(mk('single', 0))]; }")
        check(ready == [False, True, True, True, True],
              "料源判据：成稿规划未排图不可选，逐期即兴与单集不受限", str(ready))

        # 用库里真实项目核对两个入口：未排图的成稿规划项目既不该出现在下拉里，
        # 卡片上的「设为当前」也该是禁用的。过滤漏一个口，就等于没堵。
        lock = pg.evaluate(
            "() => { const pr = p => p.progress || {};"
            " const bad = PROJECTS.filter(p => pr(p).mode === 'mapped' && !pr(p).mapped);"
            " const sel = document.getElementById('s-project');"
            " const ids = sel ? Array.from(sel.querySelectorAll('option'))"
            "   .map(o => o.value) : [];"
            " const leaked = bad.filter(p => ids.indexOf(p.id) >= 0).map(p => p.id);"
            " const btns = Array.from(document.querySelectorAll('#projects .acts button'))"
            "   .filter(b => b.textContent.trim() === '设为当前');"
            " return [bad.length, leaked.length, btns.length,"
            "         btns.filter(b => b.disabled).length]; }")
        check(lock[1] == 0 and lock[0] == lock[3],
              "未排图的成稿规划项目进不了入口（下拉不列 + 设为当前禁用）",
              "应禁 %s 项 / 漏进下拉 %s 项 / 按钮 %s 个其中禁用 %s 个"
              % (lock[0], lock[1], lock[2], lock[3]))

        # 素材区随范式切换：成稿规划收起输入口、只读摆出本期落点；逐期即兴给回
        # 输入口。这里用一次假数据触发成稿规划那一路，看完立刻还原，不动任何落盘。
        mat = pg.evaluate(
            "() => {"
            " renderMaterialSource({progress: {mode: 'mapped', next_episode: '1'},"
            "   map: {episodes: [{no: '1', title: '试验期',"
            "     refs: [{source: 's1', anchor: '第一章'}]}]}});"
            " const inp = document.getElementById('mat-input');"
            " const lk = document.getElementById('mat-locked');"
            " const src = document.getElementById('mat-src');"
            " const out = [getComputedStyle(inp).display,"
            "   getComputedStyle(lk).display,"
            "   src.textContent.indexOf('第一章') >= 0,"
            "   src.textContent.indexOf('地图落点') >= 0];"
            " renderMaterialSource(null);"
            " const back = getComputedStyle(inp).display;"
            " return out.concat([back]); }")
        check(mat[0] == "none" and mat[1] != "none" and mat[2] and mat[3]
              and mat[4] != "none",
              "素材区随范式切换：成稿规划只读落点、逐期即兴给回输入口",
              "输入区 %s / 只读 %s / 落点 %s / 还原 %s"
              % (mat[0], mat[1], mat[2], mat[4]))

        if args.shot_dir:
            os.makedirs(args.shot_dir, exist_ok=True)
            pg.goto(base + "/#config")
            pg.wait_for_timeout(2200)
            pg.screenshot(path=os.path.join(args.shot_dir, "ui_config.png"))

        # ---- 真拖滑块，看服务端是否真的变了 ----
        pg.goto(base + "/#script")
        pg.wait_for_timeout(1800)
        before = fetch(base + "/api/config")["values"]["script.target_minutes"]
        target = 7.5 if abs(float(before) - 7.5) > 1e-6 else 5.5
        js_move = r"""(v) => {
          const n = document.getElementById('f_quick_script__target_minutes');
          if (!n) return 'no-node';
          n.value = String(v);
          n.dispatchEvent(new Event('input', {bubbles:true}));
          n.dispatchEvent(new Event('change', {bubbles:true}));
          return n.value;
        }"""
        moved = pg.evaluate(js_move, target)
        pg.wait_for_timeout(1200)
        after = fetch(base + "/api/config")["values"]["script.target_minutes"]
        check(abs(float(after) - float(target)) < 1e-6, "拖动滑块真的写回服务端",
              "改前 %s -> 目标 %s -> 服务端 %s（控件报 %s）"
              % (before, target, after, moved))
        shown = pg.evaluate(
            "() => { const n = document.getElementById('v_quick_script__target_minutes');"
            " return n ? n.textContent : ''; }")
        check("分钟" in shown, "滑块旁显示带单位的数值", "显示「%s」" % shown)

        # 同一点位在脚本页与配置页各有一份控件。配置页那份此刻仍在文档里
        # （只是被切走了，没有销毁），改一处另一处就该跟着走，而不是等下次
        # 重新渲染才碰巧显示对——那样测不出同步，只测出了重新渲染。
        mirror = pg.evaluate(
            "() => { const n = document.getElementById('f_cfg_script__target_minutes');"
            " return n ? n.value : 'missing'; }")
        check(str(mirror) == str(target), "同一点位跨页控件值同步",
              "配置页 %s / 本页 %s" % (mirror, target))
        pg.evaluate(js_move, before)
        pg.wait_for_timeout(800)

        # ---- 音色与称呼 ----
        # 音色按引擎分两套键，同屏只露出当前引擎那一套。所以断言必须两问：
        # 当前引擎的下拉有真清单，另一套确实被收起。只问前者会漏掉「两套
        # 并排摆着、用户分不清哪个在生效」这种回归——那正是这组键的初衷。
        voices = pg.evaluate(
            "() => {"
            " const engEl = document.getElementById('f_cfg_tts__engine');"
            " const eng = engEl ? engEl.value : '';"
            " const local = eng === 'qwen3tts';"
            " const pick  = local ? 'f_cfg_tts__qwen3tts_voice_a' : 'f_cfg_tts__voice_a';"
            " const other = local ? 'f_cfg_tts__voice_a' : 'f_cfg_tts__qwen3tts_voice_a';"
            " const hidden = id => { const x = document.getElementById(id);"
            "   if (!x) return null; const r = x.closest('.f');"
            "   return r ? getComputedStyle(r).display === 'none' : null; };"
            " const n = document.getElementById(pick);"
            " return [eng, n ? n.tagName : 'missing',"
            "         n && n.options ? n.options.length : 0,"
            "         hidden(other), hidden(pick)]; }")
        check(voices[1] == "SELECT" and voices[2] > 1, "当前引擎的 A 角音色下拉有选项",
              "引擎 %s / %s / 选项数 %s" % (voices[0], voices[1], voices[2]))
        check(voices[3] is True and voices[4] is False, "非当前引擎的音色组已收起",
              "另一套 hidden=%s / 本套 hidden=%s" % (voices[3], voices[4]))
        names = pg.evaluate(
            "() => { const a = document.getElementById('f_cfg_tts__name_a');"
            " return a ? a.value : null; }")
        check(names is not None, "A/B 角称呼可改（有输入框）", "当前 %s" % names)

        # ---- 模型 ----
        llm = pg.evaluate(
            "() => { const b = document.getElementById('f_cfg_llm__backend');"
            " const m = document.getElementById('f_cfg_llm__model');"
            " const o = (m && m.options) ? Array.from(m.options).map(x => x.value) : [];"
            " return [b && b.options ? b.options.length : 0, m ? m.tagName : null,"
            "         o.length, o.indexOf('__manual__')]; }")
        check(llm[0] >= 2, "LLM 后端下拉有多个后端", "选项 %d 个" % llm[0])
        # 判据是「点开能列全部」，不是「有个能打字的框」：datalist 那种「输入框 +
        # 候选」是浏览器的补全，拿框里已有的字去筛——框里填着 qwen/qwen3.5-35b-a3b
        # 时 15 个候选只剩它自己，看着就像下拉拉不出来。所以必须是 select，
        # 且带「手动填写…」入口（列表里没有的名字走这一条）。
        check(llm[1] == "SELECT" and llm[2] > 1 and llm[3] >= 0,
              "模型名可下拉选（含手动填写入口）",
              "控件 %s / 选项 %s 个 / 手动项位置 %s" % (llm[1], llm[2], llm[3]))

        # ---- 模型列表接口 ----
        models = fetch(base + "/api/models")
        check("models" in models, "模型列表接口可用",
              "ok=%s, %s" % (models.get("ok"), str(models.get("error") or "无需后端")[:60]))

        # ---- 字体下拉（画面 + 字幕两款）----
        # 四件事一起判，缺一都不算过：白名单全量列出（没装的也在）、没装的暗显
        # 不可选、每项带许可说明、每项按自己的字形渲染。只判「有选项」会漏掉
        # 「未装判定没生效」；只判暗显会漏掉「选项没有 font-family」——那正是
        # 换回原生 select 时会出现的形状（浏览器忽略选项上的字体），界面看着
        # 一切正常，字却全长一个样。
        font = pg.evaluate("""() => {
          const ids = ['f_cfg_frame__font_family','f_cfg_subtitle__font_family'];
          const out = [];
          for (const id of ids) {
            const n = document.getElementById(id);
            if (!n) { out.push({id: id, missing: true}); continue; }
            const os = Array.from(n.querySelectorAll('.fopt'));
            const off = os.filter(o => o.classList.contains('off')).length;
            const marks = ['系统自带','OFL','Apache','免费','GPL','自备','当前值'];
            const bare = os.filter(o => {
              const t = (o.querySelector('.fn') || {}).textContent || '';
              return t.indexOf('自动选择') < 0
                && !marks.some(m => t.indexOf(m) >= 0);
            }).length;
            const styled = os.filter(o => {
              const s = (o.querySelector('.fs') || {}).style;
              return s && s.fontFamily;
            }).length;
            out.push({id: id, cls: n.className, n: os.length, off: off,
                      bare: bare, styled: styled});
          }
          return out;
        }""")
        for f in font:
            who = f["id"].split("__")[-1]
            check(not f.get("missing") and "fpick" in (f.get("cls") or ""),
                  "字体下拉是自绘控件（%s）" % who, str(f))
            check(f.get("n", 0) >= 20, "字体白名单全量列出（%s）" % who,
                  "共 %s 项" % f.get("n"))
            check(f.get("off", 0) > 0, "未装字体暗显不可选（%s）" % who,
                  "暗显 %s 项" % f.get("off"))
            check(f.get("bare", 0) == 0, "候选项都带许可说明（%s）" % who,
                  "缺说明 %s 项" % f.get("bare"))
            check(f.get("styled", 0) == f.get("n", 0),
                  "候选项都按各自字形渲染（%s）" % who,
                  "%s / %s 项带 font-family" % (f.get("styled"), f.get("n")))

        # ---- 项目接口 ----
        proj = fetch(base + "/api/project")
        check(proj.get("ok"), "项目登记表可读",
              "已有 %d 个项目" % len(proj.get("projects") or []))

        # 素材面板：入库的每一份素材要显示自己的结构摘要（条数 / 层数 / 切分
        # 单位 / 判定类型）。这里不真上传——上传要跑探查、要等模型——只把面板
        # 渲染一遍：渲染分支写错时这一块整片空白，而接口单看全是好的。
        plist = proj.get("projects") or []
        if plist:
            pg.evaluate("async (p) => { await openSources(p) }", plist[0].get("id"))
            pg.wait_for_timeout(900)
            render = pg.evaluate(
                "() => { const box = document.getElementById('src-list');"
                " return [!!box,"
                "         box ? box.querySelectorAll('.src-row').length : -1,"
                "         document.getElementById('src-file') ? 1 : 0]; }")
            check(render[0] and render[2] == 1, "素材面板可渲染",
                  "列表 %s / 现有行 %s" % (render[0], render[1]))
            pg.evaluate("() => closeModal()")
        else:
            print("    （登记表为空，素材面板渲染跳过）")

        # ---- 选期表与批量 ----
        # 这一段全程走浏览器内的桩：项目表、期表、批任务接口就地换掉。于是
        # 「多期是不是真的走了批任务」「合成页是不是真的只列有脚本的期」能在
        # 真浏览器里答出来，而服务端一期活都不干、落盘一个字节都不动。
        # 桩里另接了单期接口：多期若从那儿漏过去，这里就记一笔。
        batch_calls, forbidden, del_calls = [], [], []
        # 任务台账桩：先报几轮「还在跑」，再收工。用来验「跑着的时候开工按钮
        # 摁不动」——默认直接给 done，那一段就无从验起。
        task_box = {"running_left": 0}

        def stub(route):
            u = route.request.url
            if "/api/project" in u and route.request.method == "POST":
                # 删除是唯一不可逆的动作，真删掉的是一个项目目录，所以这里只记
                # 请求、回一个假结果，服务端一个字节都不动。
                body_in = json.loads(route.request.post_data or "{}")
                del_calls.append(body_in)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "ok": True, "id": body_in.get("id") or "", "name": "冒烟桩项目",
                    "dir": "/tmp/桩", "dir_removed": True, "freed_bytes": 1048576}))
                return
            if "/api/scripts" in u:
                body = STUB_SCRIPTS
            elif "/api/project" in u:
                body = ({"ok": True, "orphans": []} if "scope=orphans" in u
                        else STUB_PROJECTS)
            elif "/api/batch" in u:
                batch_calls.append(json.loads(route.request.post_data or "{}"))
                body = {"ok": True, "task_id": "SMOKE-BATCH", "total": 3}
            elif "/api/task/" in u:
                if task_box["running_left"] > 0:
                    task_box["running_left"] -= 1
                    body = {"ok": True, "job": {
                        "id": "SMOKE-BATCH", "kind": "batch", "status": "running",
                        "stage": "第 1 步 · 干活", "progress": 0.3,
                        "log": ["桩任务：还在跑"],
                        "batch": {"index": 1, "total": 2, "no": "1"},
                        "result": None}}
                else:
                    body = {"ok": True, "job": {
                        "id": "SMOKE-BATCH", "kind": "batch", "status": "done",
                        "stage": "完成", "progress": 1.0, "log": ["桩任务"],
                        "batch": {"index": 2, "total": 2, "no": "2"},
                        "result": {"done": ["1", "2"], "failed": []}}}
            elif "/api/render" in u or "/api/script/generate" in u:
                forbidden.append(u.split("/api/")[-1].split("?")[0])
                body = {"ok": False, "error": "多期不该落到单期接口"}
            else:
                route.continue_()
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))

        pg.route(re.compile(r"/api/"), stub)
        pg.goto(base + "/#render")
        pg.wait_for_timeout(1200)
        pg.evaluate("async () => { await loadProjects() }")
        pg.wait_for_timeout(500)

        rp = pg.evaluate(JS_PICK % {"w": "r"}, "SMOKE-FAKE")
        check(rp[0] == 2, "合成页只列有脚本的期",
              "列出 %s 期 / 接口期表 %s" % (rp[0], rp[2]))
        check("第 3 期" not in rp[1] and "第 1 期" in rp[1] and "第 2 期" in rp[1],
              "没脚本的那一期不进合成可选", "明细「%s」" % rp[1])
        check(rp[3] != "none", "有归属项目时选期表展开", "display=%s" % rp[3])

        sp = pg.evaluate(JS_PICK % {"w": "s"}, "SMOKE-FAKE")
        check(sp[0] == 3 and "无脚本" in sp[1], "脚本页列出全部期（含无脚本的）",
              "列出 %s 期 / 明细「%s」" % (sp[0], sp[1]))

        mu = pg.evaluate(JS_MULTI)
        check(mu[6] and mu[7], "选期面板可开可合", "开 %s / 合 %s" % (mu[6], mu[7]))
        check(mu[0] == "1,2" and "已选 2 期" in mu[1],
              "全选：选上的期与计数一致", "选上「%s」/ 计数「%s」" % (mu[0], mu[1]))
        check(mu[2] == 0 and not mu[3].strip(), "清空：一期不剩且计数归零",
              "剩 %s 期 / 计数「%s」" % (mu[2], mu[3]))
        check(mu[4] == "1" and "已选 1 期" in mu[5], "单勾一期：计数跟着走",
              "选上「%s」/ 计数「%s」" % (mu[4], mu[5]))
        check(mu[8] == 2, "全选后可开工的期数", "已选 %s 期" % mu[8])

        # 展开选期表留一张图：判据是数字，但「下拉里带勾选、收起只显示已选几期」
        # 这件事得看得见才算证过。
        if args.shot_dir:
            pg.evaluate("() => togglePick('r')")
            pg.wait_for_timeout(300)
            pg.screenshot(path=os.path.join(args.shot_dir, "ui_pick_render.png"))
            pg.evaluate("() => closePick()")

        pg.evaluate("() => document.getElementById('btn-render').click()")
        pg.wait_for_timeout(1500)
        check(len(batch_calls) == 1 and batch_calls[0].get("kind") == "render"
              and len(batch_calls[0].get("episodes") or []) == 2,
              "合成页勾两期走批任务（不是单期接口）",
              "请求 %s" % json.dumps(batch_calls[0] if batch_calls else {}, ensure_ascii=False))

        pg.goto(base + "/#script")
        pg.wait_for_timeout(1200)
        pg.evaluate("async () => { await loadProjects() }")
        pg.wait_for_timeout(500)
        pg.evaluate(
            "async () => { document.getElementById('s-project').value = 'SMOKE-FAKE';"
            " await loadEpisodes('s'); pickAll('s', true); }")
        pg.wait_for_timeout(300)
        pg.evaluate("() => document.getElementById('btn-gen').click()")
        pg.wait_for_timeout(1500)
        check(len(batch_calls) == 2 and batch_calls[1].get("kind") == "script"
              and len(batch_calls[1].get("episodes") or []) == 3,
              "脚本页勾三期走批任务（不是单次生成）",
              "请求 %s" % json.dumps(batch_calls[1] if len(batch_calls) > 1 else {},
                                    ensure_ascii=False))
        check(not forbidden, "多期不落到单期接口", "；".join(forbidden) or "无")

        # ---- 任务跑着的时候，开工按钮必须摁不动 ----
        # 桩里让任务先报两轮「还在跑」再收工，判据只有一条：这期间按钮一直是灰
        # 的。从前的轮询第一次就交差（等下一轮的那句 setTimeout 一扔就走），
        # await 的调用方当场往下执行——按钮在任务刚起步时就亮了，看着像已经收
        # 工，人一点就起了第二个任务。
        JS_BTN = r"""() => {
  const b = document.getElementById('btn-gen');
  const s = document.getElementById('btn-stop-s');
  const bar = document.getElementById('gen-bar');
  return [b.disabled ? 1 : 0, s ? s.style.display : 'na',
          bar ? bar.style.width : 'na',
          (document.getElementById('gen-status').textContent || '')];
}"""
        task_box["running_left"] = 2
        # 上一段跑完时刷了一次期表，勾选跟着回到默认——这里重新摆好「有项目、
        # 勾了三期」，否则点下去会走单期那条路，验的不是这一段。
        pg.evaluate(
            "async () => { document.getElementById('s-project').value = 'SMOKE-FAKE';"
            " await loadEpisodes('s'); pickAll('s', true); }")
        pg.wait_for_timeout(300)
        pg.evaluate("() => document.getElementById('btn-gen').click()")
        pg.wait_for_timeout(600)
        mid = pg.evaluate(JS_BTN)
        check(mid[0] == 1, "任务还在跑：开工按钮摁不动（不再点完就亮）",
              "按钮 disabled=%s / 阶段「%s」/ 进度条 %s"
              % (bool(mid[0]), mid[3], mid[2]))
        check(mid[1] != "none", "任务还在跑：中止按钮露着", "display=%s" % mid[1])
        pg.wait_for_timeout(4200)
        end = pg.evaluate(JS_BTN)
        check(end[0] == 0 and end[1] == "none",
              "任务收工：按钮恢复、中止按钮收起",
              "disabled=%s / 中止 %s / 阶段「%s」"
              % (bool(end[0]), end[1], end[2]))

        hd = pg.evaluate(JS_PICK_HIDE % {"w": "s"})
        check(hd[0] == "none" and hd[1] == 0, "未选项目时收起选期表",
              "display=%s / 行数 %s" % (hd[0], hd[1]))

        # ---- 删除要点两下 ----
        # 删掉的是一个项目目录，没有回收站。所以这里既验「第二下真的发请求」，
        # 也验「第一下什么都不发」——后者才是防误触的那一半。
        pg.goto(base + "/#project")
        pg.wait_for_timeout(900)
        pg.evaluate("async () => { await loadProjects() }")
        pg.wait_for_timeout(600)
        before = len(del_calls)
        arm = pg.evaluate(JS_DEL_ARM)
        check(arm[0] == 1, "卡片上找得到删除按钮",
              "按钮行「%s」" % (arm[3] if len(arm) > 3 else arm[0]))
        if arm[0] == 1:
            check(len(del_calls) == before, "第一下只点亮，不发请求",
                  "删除请求 %s → %s" % (before, len(del_calls)))
            check(arm[1] == 1 and arm[2] == "确认删除", "第一下把按钮变成确认态",
                  "带确认态的按钮 %s 个 / 文字「%s」" % (arm[1], arm[2]))
            pg.screenshot(path=os.path.join(args.shot_dir, "ui_project_del.png"))
            fire = pg.evaluate(JS_DEL_FIRE)
            pg.wait_for_timeout(800)
            check(fire[0] == 1, "确认态按钮点得到", str(fire))
            check(len(del_calls) == before + 1
                  and (del_calls[-1].get("action") or "") == "delete",
                  "第二下才发删除请求（action=delete）",
                  "请求 %s" % json.dumps(del_calls[-1] if del_calls else {},
                                        ensure_ascii=False))

        # ---- 插入素材：入库区必须在面板里 ----
        # 插入的本意是「把新材料加进来」。桩里两份素材，一份已经排进地图、
        # 一份没有。要验三件事——面板自带上传区；已进地图的那份默认不勾（勾
        # 下去，模型会把同一份素材当新素材再插一遍，期号变 3a/3b 而内容与第
        # 3 期重复，且这件事不报错）；库空时面板照样开得出来、上传区就在原地，
        # 而不是弹一句提示把人支到别的面板去。
        ms = pg.evaluate(JS_MAP_SRC)
        check(ms == [1, 1, 1, 0],
              "地图已用素材判定（含空落点的期）",
              "s1/s2/s9 命中 %s / 未用过的 s3 %s" % (ms[:3], ms[3]))

        ins_box = {"body": STUB_INS_SOURCES}

        def stub_ins(route):
            u = route.request.url
            if "/api/sources" in u:
                body = ins_box["body"]
            elif "/api/project" in u and route.request.method == "GET":
                body = STUB_MAP_PROJECT
            else:
                route.continue_()
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))

        pg.route(re.compile(r"/api/"), stub_ins)
        ip = pg.evaluate(JS_INS_PANEL, "SMOKE-FAKE")
        check(ip[0] == 1 and ip[1] == 1 and ip[2] == 1,
              "插入面板自带入库区（文件 / 粘贴 / 入库按钮）",
              "文件 %s / 粘贴 %s / 按钮 %s" % (ip[0], ip[1], ip[2]))
        check(ip[3] == "s1:0:1|s2:1:0",
              "插入面板：已进地图的默认不勾并标出，未用过的默认勾上",
              "逐条「素材号:勾选:已在地图中」= %s" % ip[3])
        ins_box["body"] = STUB_INS_EMPTY
        ie = pg.evaluate(JS_INS_EMPTY, "SMOKE-FAKE")
        check(ie[0] == 1 and ie[1] == 0 and ie[2] == 0,
              "库里没有素材时插入面板照样开（上传区在原地，不把人支走）",
              "上传区 %s / 素材行 %s / 红字提示 %s" % (ie[0], ie[1], ie[2]))
        pg.evaluate("() => closeModal()")
        pg.unroute(re.compile(r"/api/"), stub_ins)

        pg.unroute(re.compile(r"/api/"), stub)

        # ---- 门禁：绕过界面直调接口也得拦住 ----
        # 界面已经堵了前置，但接口是另一道门。两道门都得会拒绝，否则「先期在
        # 命令里凑得出来」这种事，换个入口就复现了。
        if plist:
            pid0 = plist[0].get("id")
            b1 = post(base + "/api/batch",
                      {"kind": "render", "project_id": pid0, "episodes": ["999"]})
            check(not b1.get("ok") and "999" in str(b1.get("error")),
                  "批合成缺脚本：接口层拒绝并点名", str(b1.get("error"))[:70])
            b2 = post(base + "/api/batch",
                      {"kind": "render", "project_id": pid0, "episodes": []})
            check(not b2.get("ok"), "批任务没选期：拒绝", str(b2.get("error"))[:70])
            b3 = post(base + "/api/script/save",
                      {"project_id": pid0, "episode_no": "1", "script": []})
            check(not b3.get("ok"), "存空脚本：拒绝", str(b3.get("error"))[:70])
            g1 = fetch(base + "/api/script?project_id=%s&episode_no=999" % pid0)
            check(not g1.get("ok"), "调没有落盘的期：拒绝", str(g1.get("error"))[:70])
        else:
            print("    （登记表为空，批任务门禁判据跳过）")
        s1 = post(base + "/api/job/stop", {"task_id": "不存在的任务"})
        check(not s1.get("ok"), "中止不存在的任务：拒绝", str(s1.get("error"))[:70])

        b.close()

    check(not errs, "无页面报错", "；".join(errs[:3]))
    print()
    print("=" * 60)
    if fails:
        print("界面冒烟：%d 项中 %d 项未通过" % (len(ran), len(fails)))
        for f in fails:
            print("  -", f)
        return 1
    print("界面冒烟：%d 项全部通过" % len(ran))
    return 0


if __name__ == "__main__":
    sys.exit(main())
