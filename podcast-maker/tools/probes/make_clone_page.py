#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Base 变体音色实测报告页 —— 单文件、零外部依赖、音频内联。

把三件事摆在一页上，供人耳复核：
  1. 参考音频候选（CustomVoice 出的 6 条，Base 的音色源头）
  2. 三种条件的同句对比音频（custom / base_xvec / base_icl）
  3. 句间离散度总表（F0 中位极差、相对极差、标准差）

音频统一转 64 kbps mp3 内联，避免几十兆的 wav 把页面撑爆。
"""

from __future__ import annotations

import base64
import html
import io
import json
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SMOKE = os.path.join(ROOT, "_smoke")
CLONE = os.path.join(SMOKE, "_clone_probe")
AUDIO = os.path.join(CLONE, "audio")
REFGEN = os.path.join(SMOKE, "_refgen")
OUT = os.path.join(CLONE, "clone_report.html")

STAGES = [("custom", "CustomVoice（现状）", "音色靠 spk_id token"),
          ("base_xvec", "Base · 只喂向量", "xvec_only=True"),
          ("base_icl", "Base · ICL", "xvec_only=False，参考整段进上下文")]

PICK_LINES = [2, 4, 9]      # 页面上逐句对比用哪几句


def mp3_b64(path: str) -> str:
    """wav → 64 kbps mp3 → base64（失败就退回原始 wav）。"""
    if not os.path.isfile(path):
        return ""
    tmp = os.path.join(tempfile.gettempdir(), "clone_page_tmp.mp3")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
           "-ac", "1", "-ar", "24000", "-b:a", "64k", tmp]
    try:
        subprocess.run(cmd, check=True, timeout=60)
        with open(tmp, "rb") as fh:
            raw = fh.read()
        mime = "audio/mpeg"
    except Exception:
        with open(path, "rb") as fh:
            raw = fh.read()
        mime = "audio/wav"
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def load_json(path: str):
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def audio_tag(path: str, label: str) -> str:
    src = mp3_b64(path)
    if not src:
        return '<div class="miss">音频缺失：%s</div>' % html.escape(os.path.basename(path))
    return ('<div class="au"><span class="lbl">%s</span>'
            '<audio controls preload="none" src="%s"></audio></div>'
            % (html.escape(label), src))


def main() -> int:
    refs = load_json(os.path.join(REFGEN, "ref.json")) or {}
    stages = {}
    for tag, _, _ in STAGES:
        rows = load_json(os.path.join(CLONE, "%s.json" % tag))
        if rows:
            stages[tag] = rows
    summary = load_json(os.path.join(CLONE, "summary.json")) or {}

    parts: list[str] = []
    parts.append("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Base 变体音色一致性实测</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
background:#fff;color:#1b1b1b}
.top{background:#161a20;color:#e8eaed;padding:22px 32px}
.top h1{margin:0;font-size:19px;font-weight:600;letter-spacing:.3px}
.top p{margin:6px 0 0;font-size:12.5px;color:#9aa3ad}
.wrap{background-image:linear-gradient(#f0f0f0 1px,transparent 1px),
linear-gradient(90deg,#f0f0f0 1px,transparent 1px);
background-size:26px 26px;padding:26px 32px 60px}
.card{background:#fff;border:1px solid #e2e2e2;border-radius:10px;padding:20px 22px;
margin-bottom:20px;max-width:1180px}
h2{font-size:15px;font-weight:600;margin:0 0 4px}
h2 .sub{font-weight:400;color:#6b7280;font-size:12.5px;margin-left:8px}
h3{font-size:13.5px;font-weight:600;margin:18px 0 8px;color:#374151}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{border:1px solid #e5e7eb;padding:7px 10px;text-align:left}
th{background:#f7f8fa;font-weight:600;color:#374151;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.au{display:flex;align-items:center;gap:10px;margin:5px 0}
.au .lbl{font-size:12.5px;color:#4b5563;min-width:118px;flex-shrink:0}
audio{height:32px;flex:1;max-width:430px}
.miss{color:#b91c1c;font-size:12.5px}
.hot{color:#b91c1c;font-weight:600}
.cool{color:#15803d;font-weight:600}
.note{font-size:12.5px;color:#6b7280;margin-top:10px;line-height:1.65}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.pill{display:inline-block;font-size:11.5px;padding:2px 8px;border-radius:10px;
background:#f1f3f5;color:#4b5563;margin-left:6px}
</style></head><body>
<div class="top">
<h1>Base 变体音色一致性实测</h1>
<p>同一批 12 句文本 · 三种条件 · F0 中位数的句间极差为主判据</p>
</div>
<div class="wrap">""")

    # ---------------------------------------------------------- 参考音频
    parts.append('<div class="card"><h2>一、参考音频候选'
                 '<span class="sub">Base 的音色源头，CustomVoice 生成一次</span></h2>')
    for spk in ("A", "B"):
        items = refs.get(spk, [])
        if not items:
            continue
        voice = items[0].get("voice", "?")
        parts.append('<h3>说话人 %s（%s）</h3>' % (html.escape(spk), html.escape(voice)))
        for it in items:
            parts.append(audio_tag(
                os.path.join(REFGEN, it["wav"]),
                "%s  %.2fs  F0 %.1f" % (it["id"], it["dur"], it["f0_med"])))
    parts.append('<div class="note">参考音频挑 5-8 秒、语速平稳的一条。'
                 '时长过短（不足 4 秒）会让说话人向量不稳，过长容易夹进换气与句尾拖音。</div>')
    parts.append('</div>')

    # ---------------------------------------------------------- 逐句对比
    parts.append('<div class="card"><h2>二、同句对比'
                 '<span class="sub">同一条文本、同一个种子，只听音色</span></h2>')
    for spk in ("A", "B"):
        parts.append('<h3>说话人 %s</h3>' % html.escape(spk))
        for ln in PICK_LINES:
            text = ""
            for tag, _, _ in STAGES:
                for r in stages.get(tag, []):
                    if r["speaker"] == spk and r["line"] == ln:
                        text = r["text"]
                        break
                if text:
                    break
            parts.append('<div style="margin:10px 0 4px;font-size:12.5px;color:#374151">'
                         '第 %d 句 · %s</div>' % (ln, html.escape(text)))
            for tag, label, _ in STAGES:
                rows = stages.get(tag, [])
                hit = next((r for r in rows
                            if r["speaker"] == spk and r["line"] == ln), None)
                if not hit:
                    continue
                parts.append(audio_tag(
                    os.path.join(AUDIO, hit["wav"]),
                    "%s  F0 %.1f" % (label, hit["f0_med"])))
    parts.append('</div>')

    # ---------------------------------------------------------- 离散度
    if summary:
        parts.append('<div class="card"><h2>三、句间离散度'
                     '<span class="sub">12 句跨文本 · 越小越像同一个人</span></h2>')
        rows_html = []
        for tag, label, _ in STAGES:
            if tag not in summary:
                continue
            for spk in ("A", "B"):
                s = summary[tag].get(spk, {}).get("f0_med")
                if not s or not s.get("n"):
                    continue
                cls = "hot" if s["rel_range_pct"] >= 15 else "cool"
                rows_html.append(
                    "<tr><td>%s</td><td>%s</td><td class='num'>%.1f</td>"
                    "<td class='num'>%.1f</td><td class='num'>%.1f</td>"
                    "<td class='num'><span class='%s'>%.1f%%</span></td>"
                    "<td class='num'>%.1f</td></tr>"
                    % (html.escape(label), html.escape(spk), s["min"], s["max"],
                       s["median"], cls, s["rel_range_pct"], s["std"]))
        parts.append('<h3>F0 中位数（Hz）</h3><table>'
                     '<tr><th>条件</th><th>说话人</th><th>最低</th><th>最高</th>'
                     '<th>中位</th><th>相对极差</th><th>标准差</th></tr>'
                     + "".join(rows_html) + '</table>')
        parts.append('<div class="note">相对极差 = 极差 ÷ 中位，'
                     '直接体现「同一个人的音高波动幅度」。'
                     '15% 以上标红。</div>')
        parts.append('</div>')

    # ---------------------------------------------------------- 情绪指令
    ins = load_json(os.path.join(SMOKE, "_instruct_probe", "instruct.json"))
    if ins:
        CONDS = [("none", "无指令"), ("light", "轻松"), ("sigh", "感慨")]
        MODES = [("icl", "ICL（参考整段进上下文）"),
                 ("xvec", "只喂向量（官方标注实验性）")]
        parts.append('<div class="card"><h2>四、情绪指令效力'
                     '<span class="sub">同模式内三种指令的音高差异</span></h2>')
        for mode, mlabel in MODES:
            sub = [r for r in ins if r["mode"] == mode]
            if not sub:
                continue
            parts.append('<h3>%s</h3>' % html.escape(mlabel))
            body = []
            base_mean = None
            for tag, tlabel in CONDS:
                v = [r["f0_med"] for r in sub
                     if r["cond"] == tag and r["f0_med"] == r["f0_med"]]
                if not v:
                    continue
                mean = sum(v) / len(v)
                if tag == "none":
                    base_mean = mean
                delta = "" if base_mean is None else "%+.1f" % (mean - base_mean)
                body.append("<tr><td>%s</td><td>%s</td><td class='num'>%.1f</td>"
                            "<td class='num'>%.1f</td><td class='num'>%s</td></tr>"
                            % (html.escape(tlabel), html.escape(tag),
                               min(v), max(v), delta))
            parts.append('<table><tr><th>指令</th><th>标签</th><th>F0 最低</th>'
                         '<th>F0 最高</th><th>相对无指令</th></tr>'
                         + "".join(body) + '</table>')
            for tag, tlabel in CONDS:
                for ln in (2, 9):
                    hit = next((r for r in sub
                                if r["cond"] == tag and r["line"] == ln), None)
                    if not hit:
                        continue
                    parts.append(audio_tag(
                        os.path.join(SMOKE, "_instruct_probe", "audio", hit["wav"]),
                        "%s · 第%d句 F0 %.1f" % (tlabel, ln, hit["f0_med"])))
            parts.append('<div class="note">样本 12 句。'
                         '「相对无指令」是 F0 均值的差，正值表示音高被抬高。</div>')
        parts.append('</div>')

    parts.append('</div></body></html>')

    with io.open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    size = os.path.getsize(OUT) / 1e6
    print("产物 %s（%.2f MB）" % (OUT, size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
