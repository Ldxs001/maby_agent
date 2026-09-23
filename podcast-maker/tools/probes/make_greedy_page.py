# -*- coding: utf-8 -*-
"""把 _greedy_probe 的音频与指标拼成离线试听页（单文件，双击即听）。

页面按「句 × 种子」分行，四档并排：现状 / 关子码本 / 只关主路 / 全贪心。
点单格播放，一键顺次播完一行做直接对照。

    python tools/probes/make_greedy_page.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
D = os.path.join(HERE, "_greedy_probe")
OUT = os.path.join(D, "listen.html")

ARMS = ["ref", "g_pred", "g_talk", "g_all"]

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>贪心档四配置对照试听</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--fg:#1f2328;--dim:#68707a;--hi:#b3392a;--ok:#1d7a4c;}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--fg);
font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:19px;margin:0 0 6px}
h2{font-size:16px;margin:0 0 10px}
.sub{color:var(--dim);margin:0 0 18px;max-width:980px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch}
.cell{border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:172px;flex:1 1 172px}
.cell.on{border-color:var(--hi);box-shadow:0 0 0 2px rgba(179,57,42,.13)}
.lv{font-size:12px;font-weight:600;margin-bottom:1px}
.ins{font-size:11px;color:#9aa1a9;min-height:15px;margin-bottom:7px}
button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--line);background:#fff;
border-radius:6px;cursor:pointer;color:var(--fg)}
button:hover{border-color:#b6bcc4}
button.pri{background:var(--fg);color:#fff;border-color:var(--fg)}
.m{font-size:11px;color:var(--dim);margin-top:7px;font-variant-numeric:tabular-nums;line-height:1.5}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-weight:600}
audio{display:none}
.note{font-size:12px;color:var(--dim);margin-top:10px}
.seedrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-top:1px dashed var(--line);padding-top:10px;margin-top:10px}
.seedtag{font-size:12px;font-weight:600;color:var(--dim);min-width:96px}
.fact{font-size:13px;margin:0;padding-left:18px}
.fact li{margin:3px 0}
.yes{color:var(--ok);font-weight:700}
.no{color:var(--hi);font-weight:700}
code{background:#eef0f3;padding:1px 5px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<h1>贪心档四配置对照试听</h1>
<p class="sub">Qwen3-TTS 有两处采样：<b>主路（talker，第 1 码本）</b>与<b>子码本（code predictor，另外 15 个，
管音色细节与韵律）</b>。本页把两处分别关掉，看是谁在起作用。<br>
<b>指标读法</b>：<b>F0 中位极差</b> = 同一句话换随机种子后音高中心漂多远；
<b>质心极差</b> = 音色亮度漂多远（听感上「是不是同一个人在念」主要看这个，比 F0 灵敏）；
<b>最长连</b> = 主路相邻重复的最长连跑步数，越大越像卡住念车轱辘话。</p>

<div class="card">
  <h2>1 · 硬判据：换随机种子后，输出还变不变</h2>
  <ul class="fact">{FACTS}</ul>
  <p class="note">主路与子码本两处都关掉随机采样时，换种子输出完全一致 —— 说明种子只作用在这两处采样上。
  只关主路、留着子码本采样时，换种子输出仍然不同 —— 说明 <b>CUDA 图内那处采样确实吃 torch 随机种子</b>
  （这一条纠正了此前「图内 RNG 与种子无关」的推断）。</p>
</div>

<div class="card">
  <h2>2 · 按档汇总（3 句 × 4 种子）</h2>
  <table>
    <tr><th>档位</th><th>F0 中位极差（均值/最大）</th><th>质心极差（均值/最大）</th>
    <th>F0 波动均值</th><th>最长连</th><th>时长极差</th><th>顶上限</th></tr>
    {STATS}
  </table>
  <p class="note">极差取「同一句、换种子」的口径（先按句内算，再跨三句取均值/最大），
  这样不会像总量口径那样被句子本身的差异淹没。</p>
</div>

{KEYS}

{ROWS}

{AUDIO}

<script>
function stopAll(){
  document.querySelectorAll('audio').forEach(function(a){ a.onended = null; a.pause(); a.currentTime = 0; });
  document.querySelectorAll('.cell').forEach(function(c){ c.classList.remove('on'); });
}
function one(id){
  stopAll();
  var a = document.getElementById('au_' + id);
  var c = document.querySelector('[data-id="' + id + '"]');
  if(!a) return;
  if(c) c.classList.add('on');
  a.play();
}
function seq(ids){
  stopAll();
  var i = 0;
  function step(){
    if(i >= ids.length){ return; }
    var id = ids[i++];
    var a = document.getElementById('au_' + id);
    var c = document.querySelector('[data-id="' + id + '"]');
    if(!a){ step(); return; }
    if(c) c.classList.add('on');
    a.onended = function(){ setTimeout(step, 320); };
    a.play();
  }
  step();
}
</script>
</body>
</html>
"""


def to_mp3(src: str) -> str | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    dst = os.path.join(tempfile.gettempdir(), os.path.basename(src) + ".mp3")
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src,
                        "-codec:a", "libmp3lame", "-b:a", "96k", "-ar", "24000", dst],
                       capture_output=True)
    return dst if r.returncode == 0 and os.path.exists(dst) else None


def main() -> None:
    res = json.load(io.open(os.path.join(D, "probe.json"), encoding="utf-8"))
    rows = res["rows"]
    arms = res["arms"]
    index = {(r["tag"], r["seed"], r["arm"]): r for r in rows}
    seeds = res["seeds"]

    order, seen = [], set()
    for r in rows:
        if r["tag"] not in seen:
            seen.add(r["tag"])
            order.append((r["tag"], r["text"]))

    audio_html, rows_html = [], []

    # ---- 判据 A ---- #
    facts = []
    for arm in ARMS:
        if arm not in arms:
            continue
        for tag, _t in order[:1]:
            ms = [r["md5"] for r in rows if r["arm"] == arm and r["tag"] == tag]
            same = len(set(ms)) == 1
            cls = "yes" if same else "no"
            facts.append(
                "<li><b>%s</b>（主路 %s·子码本 %s）：%s 句换四个种子，波形 <span class='%s'>%s</span>%s</li>"
                % (arms[arm]["label"],
                   "采样" if arms[arm]["talker_sample"] else "贪心",
                   "采样" if arms[arm]["pred_sample"] else "贪心",
                   tag, cls, "全同" if same else "各不相同",
                   "（四个种子产出同一条波形，说明剩下的随机性为零）" if same else ""))

    # ---- 汇总表 ---- #
    stats_html = []
    for arm in ARMS:
        s = res["summary"].get(arm)
        if not s:
            continue
        stats_html.append(
            "<tr><td>%s</td><td>%.1f / %.1f Hz</td><td>%.0f / %.0f Hz</td>"
            "<td>%.1f Hz</td><td>%d 步</td><td>%.2f s</td><td>%d/%d</td></tr>"
            % (s["label"], s["f0_range_mean"], s["f0_range_max"],
               s["centroid_range_mean"], s["centroid_range_max"],
               s["f0_std_mean"], s["max_run"], s["dur_range"],
               s["cap_hits"], s["n"]))

    # ---- 每句一组 ---- #
    for tag, text in order:
        seed_blocks = []
        for seed in seeds:
            cells, ids = [], []
            for arm in ARMS:
                rec = index.get((tag, seed, arm))
                if not rec:
                    continue
                rid = "%s_%s_%d" % (tag, arm, seed)
                path = os.path.join(D, rec["wav"])
                if not os.path.exists(path):
                    continue
                mp3 = to_mp3(path)
                src = mp3 or path
                with open(src, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                audio_html.append(
                    '<audio id="au_%s" preload="none" src="data:%s;base64,%s"></audio>'
                    % (rid, "audio/mpeg" if mp3 else "audio/wav", b64))
                ids.append(rid)
                flags = []
                if rec["hit_cap"]:
                    flags.append('<span class="no">顶到上限</span>')
                if rec["rate"] < 2.6:
                    flags.append('<span class="no">语速异常慢 %.2f 字/秒</span>' % rec["rate"])
                if rec["max_run"] >= 8:
                    flags.append('<span class="no">念车轱辘话</span>')
                flag = (" " + " ".join(flags)) if flags else ""
                cells.append(
                    '<div class="cell" data-id="%s">'
                    '<div class="lv">%s</div><div class="ins">%s</div>'
                    '<button onclick="one(\'%s\')">播放</button>'
                    '<div class="m">F0 %s · 波动 %s<br>质心 %s<br>%s s · %.2f 字/秒 · 最长连 %d 步%s</div></div>'
                    % (rid, arms[arm]["label"],
                       "主路%s·子码本%s" % ("采样" if arms[arm]["talker_sample"] else "贪心",
                                          "采样" if arms[arm]["pred_sample"] else "贪心"),
                       rid, rec["f0_mean"], rec["f0_std"], rec["centroid"],
                       rec["dur"], rec["rate"], rec["max_run"], flag))
            if not cells:
                continue
            seed_blocks.append(
                '<div class="seedrow"><div class="seedtag">种子 %d</div>'
                '<div class="row" style="flex:1 1 auto">%s</div></div>'
                % (seed, "".join(cells)))

        first_ids = json.dumps(["%s_%s_%d" % (tag, a, seeds[0]) for a in ARMS
                                if index.get((tag, seeds[0], a))])
        rows_html.append(
            '<div class="card"><h2>%s ｜ %s</h2>'
            '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
            '<button class="pri" onclick=\'seq(%s)\'>依次播四个档位（第一行）</button>'
            '</div>%s</div>'
            % (tag, text, first_ids, "".join(seed_blocks)))

    # ---- 关键对照：同一句同一种子，四档并排 ---- #
    key_html = []
    for tag, seed, why in [("S2", 3007, "长句（21 字）· 关子码本后这一条出现明显拖沓，是它能不能上生产的关键样本"),
                           ("S1", 3007, "短句（16 字）· 四档里最干净的一组，稳定收益看得最清楚")]:
        cells, ids = [], []
        for arm in ARMS:
            rec = index.get((tag, seed, arm))
            if not rec:
                continue
            rid = "K_%s_%s_%d" % (tag, arm, seed)
            path = os.path.join(D, rec["wav"])
            if not os.path.exists(path):
                continue
            mp3 = to_mp3(path)
            src = mp3 or path
            with open(src, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            audio_html.append('<audio id="au_%s" preload="none" src="data:%s;base64,%s"></audio>'
                              % (rid, "audio/mpeg" if mp3 else "audio/wav", b64))
            ids.append(rid)
            bad = rec["rate"] < 2.6 or rec["max_run"] >= 8 or rec["hit_cap"]
            cells.append(
                '<div class="cell%s" data-id="%s"><div class="lv">%s</div>'
                '<div class="ins">%s</div><button onclick="one(\'%s\')">播放</button>'
                '<div class="m">%s s · %.2f 字/秒<br>最长连 %d 步%s</div></div>'
                % (" on" if arm == "ref" else "", rid, arms[arm]["label"],
                   "← 生产现状" if arm == "ref" else
                   ("主路%s·子码本%s" % ("采样" if arms[arm]["talker_sample"] else "贪心",
                                      "采样" if arms[arm]["pred_sample"] else "贪心")),
                   rid, rec["dur"], rec["rate"], rec["max_run"],
                   ' <span class="no">异常</span>' if bad else ""))
        key_html.append(
            '<div class="card"><h2>关键对照 ｜ %s · 种子 %d</h2>'
            '<p class="sub" style="margin:0 0 10px">%s</p>'
            '<div class="row">%s</div>'
            '<div style="margin-top:10px"><button class="pri" onclick=\'seq(%s)\'>依次播四档</button></div></div>'
            % (tag, seed, why, "".join(cells), json.dumps(ids)))

    html = (TPL.replace("{FACTS}", "".join(facts))
               .replace("{STATS}", "".join(stats_html))
               .replace("{KEYS}", "".join(key_html))
               .replace("{ROWS}", "".join(rows_html))
               .replace("{AUDIO}", "".join(audio_html)))
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s（%.2f MB）" % (OUT, os.path.getsize(OUT) / 1048576.0))


if __name__ == "__main__":
    main()
