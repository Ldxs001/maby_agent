#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置页排布取证：按行打印每张卡的控件，并给「写作目标」卡单独截图。

行是按浏览器算出来的几何位置分的（同一个 top 即同一行），不是按源码里的顺序猜的。
用法：python main.py --port 8812 之后 python tools/probes/grid_probe.py
"""

import argparse
import os
import sys

from playwright.sync_api import sync_playwright

JS_ROWS = r"""() => {
  const kindOf = f => {
    if (f.querySelector('input[type=range]')) return '滑杆';
    if (f.querySelector('.sw')) return '开关';
    if (f.querySelector('.fpick')) return '下拉';   // 字体是自绘下拉，不带 select
    if (f.querySelector('select')) return '下拉';
    if (f.querySelector('textarea')) return '多行';
    if (f.querySelector('input[type=number]')) return '数字';
    return '输入';
  };
  const out = [];
  document.querySelectorAll('#stage-config .card').forEach(card => {
    const sec = ((card.querySelector('h2') || {}).textContent || '?').trim();
    const blocks = Array.from(card.querySelectorAll(':scope > .grid.g4'));
    if (!blocks.length) return;
    const lines = [];
    blocks.forEach((g, bi) => {
      const zhead = g.previousElementSibling;
      const label = (zhead && zhead.classList.contains('zhead'))
        ? zhead.textContent.trim() : '';
      if (label) lines.push('   〖' + label + '〗');
      const items = Array.from(g.children).filter(n => n.classList.contains('f'))
        .filter(n => n.offsetParent !== null);
      if (!items.length) return;
      const tracks = getComputedStyle(g).gridTemplateColumns.split(' ').length;
      const box = g.getBoundingClientRect();
      const colw = box.width / tracks;
      const rows = new Map();
      items.forEach(f => {
        const r = f.getBoundingClientRect();
        const key = Math.round(r.top / 6);
        if (!rows.has(key)) rows.set(key, []);
        rows.get(key).push({t: r.top, left: r.left,
          label: ((f.querySelector('label span') || {}).textContent || '?').trim(),
          k: kindOf(f), tw: tracks,
          col: Math.round((r.left - box.left) / colw) + 1,
          span: Math.round(r.width / colw)});
      });
      Array.from(rows.entries()).sort((a, b) => a[0] - b[0]).forEach(kv => {
        const list = kv[1].slice().sort((a, b) => a.left - b.left);
        lines.push('   第' + (bi + 1) + '块/行: ' + list.map(x =>
          x.label + '[' + x.k + ' 第' + x.col + '格×' + x.span + ']').join('  '));
      });
    });
    const tracks = getComputedStyle(blocks[0]).gridTemplateColumns.split(' ').length;
    out.push(sec + '（' + tracks + ' 列）\n' + lines.join('\n'));
  });
  return out.join('\n');
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8812")
    ap.add_argument("--shot", default="_smoke/_gridcheck/card_script.png")
    ap.add_argument("--card", default="写作目标")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1620, "height": 1400})
        pg.goto(base + "/#config")
        pg.wait_for_timeout(2600)
        print(pg.evaluate(JS_ROWS))
        if args.shot:
            os.makedirs(os.path.dirname(args.shot) or ".", exist_ok=True)
            cards = pg.query_selector_all("#stage-config .card")
            hit = None
            for c in cards:
                h = c.query_selector("h2")
                if h and args.card in (h.text_content() or ""):
                    hit = c
                    break
            if hit is None:
                print("没找到卡片：%s" % args.card, file=sys.stderr)
                return 1
            hit.screenshot(path=args.shot)
            print("\n卡片截图：%s" % args.shot)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
