# -*- coding: utf-8 -*-
"""把 _emo_probe 的音频与指标拼成一张离线试听页（单文件，双击即听）。

为什么要有这一步：指标表只能看、不能听，而「哪一档好听」只能靠耳朵定。
页面把同一句的五个档位并排放，点一下换一档，还能顺次播完五档做直接对照。
音频一律 base64 内联，不依赖服务、不依赖网络，双击打开就能播。

    python tools/probes/make_listen_page.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import statistics as st

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
D = os.path.join(HERE, "_emo_probe")
OUT = os.path.join(D, "listen.html")

LEVELS = [("none", "不加指令", "（不给任何语气）"),
          ("bare", "用感慨的语气说", "现状口径：只给情绪词"),
          ("light", "用略带感慨的语气说", "程度：略"),
          ("mid", "用明显感慨的语气说", "程度：明显"),
          ("heavy", "用非常感慨的语气说", "程度：非常")]

ORDER = ["中性", "感慨", "疑问", "肯定"]

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>情绪指令对照试听</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--fg:#1f2328;--dim:#68707a;--hi:#b3392a;}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--fg);
font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:19px;margin:0 0 6px}
.sub{color:var(--dim);margin:0 0 18px;max-width:900px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.q{font-size:15px;font-weight:600;margin:0 0 4px}
.qt{font-size:12px;color:var(--dim);margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:8px}
.cell{border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:150px;flex:1 1 150px;transition:border-color .12s}
.cell.on{border-color:var(--hi);box-shadow:0 0 0 2px rgba(179,57,42,.13)}
.lv{font-size:12px;font-weight:600;margin-bottom:1px}
.ins{font-size:11px;color:#9aa1a9;min-height:15px;margin-bottom:7px}
button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--line);background:#fff;
border-radius:6px;cursor:pointer;color:var(--fg)}
button:hover{border-color:#b6bcc4}
button.pri{background:var(--fg);color:#fff;border-color:var(--fg)}
.m{font-size:11px;color:var(--dim);margin-top:7px;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-weight:600}
.best{color:var(--hi);font-weight:700}
audio{display:none}
.note{font-size:12px;color:var(--dim);margin-top:10px}
</style>
</head>
<body>
<h1>情绪指令对照试听</h1>
<p class="sub">同一句文本、同一音色、<b>同一个随机种子</b>（服务端种子由文本与音色派生，与语气指令无关），
所以同一行的五个按钮起跑线完全一样，唯一的差别就是那句语气指令。
点单格播放；点「依次播五档」可以一口气听完再判断。指标里 <b>F0 波动</b>指基频标准差，越大代表声音起伏越大。</p>

{ROWS}

<div class="card">
  <h1 style="font-size:16px">跨句统计</h1>
  <p class="sub" style="margin-bottom:12px">同一档位下，四句文本的指标有多分散。越集中代表整期里句与句之间越一致。</p>
  <table>
    <tr><th>档位</th><th>F0 波动均值</th><th>F0 波动标准差</th><th>时长跨度</th><th>响度跨度</th></tr>
    {STATS}
  </table>
  <p class="note">注：每格只有一条音频（种子确定，同一档重复必然得到同一条波形）。表中数字来自一次采样，样本量小，只用于看趋势。</p>
</div>

{AUDIO}

<script>
var au = null;
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
function seq(key, ids){
  stopAll();
  var i = 0;
  function step(){
    if(i >= ids.length){ return; }
    var id = ids[i++];
    var a = document.getElementById('au_' + id);
    var c = document.querySelector('[data-id="' + id + '"]');
    if(!a){ step(); return; }
    if(c) c.classList.add('on');
    a.onended = function(){ setTimeout(step, 350); };
    a.play();
  }
  step();
}
</script>
</body>
</html>
"""


def main():
    res = json.load(io.open(os.path.join(HERE, "_emo_probe.json"), encoding="utf-8"))
    g = {(r["text_tag"], r["level"]): r for r in res["rows"]}

    texts = {}
    for tag, text in [("中性", "这件事在当时并没有引起太多人的注意。"),
                      ("感慨", "我们花了整整三年，才把这个问题想明白。"),
                      ("疑问", "如果当初换一条路，结果会不会完全不同？"),
                      ("肯定", "说到底，工具只是工具，真正要紧的是用它的人。")]:
        texts[tag] = text

    # ---- 每句一组卡片 ---------------------------------------------------- #
    rows_html, audio_html = [], []
    for tag in ORDER:
        cells = []
        ids = []
        for lv, label, hint in LEVELS:
            key = "%s_%s" % (tag, lv)
            path = os.path.join(D, key + ".wav")
            if not os.path.exists(path):
                continue
            rid = "%s_%s" % (tag, lv)
            ids.append(rid)
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            audio_html.append(
                '<audio id="au_%s" preload="none" src="data:audio/wav;base64,%s"></audio>'
                % (rid, b64))
            m = g.get((tag, lv), {})
            cells.append(
                '<div class="cell" data-id="%s">'
                '<div class="lv">%s</div><div class="ins">%s</div>'
                '<button onclick="one(\'%s\')">播放</button> '
                '<span class="m">F0 波动 %s · %ss</span></div>'
                % (rid, label, hint, rid, m.get("f0_std", "-"), m.get("dur", "-")))
        rows_html.append(
            '<div class="card"><div class="q">%s</div><div class="qt">%s</div>'
            '<div class="row">%s</div>'
            '<div style="margin-top:12px"><button class="pri" onclick="seq(\'%s\', %s)">'
            '依次播五档</button></div></div>'
            % (tag, texts[tag], "".join(cells), tag, json.dumps(ids)))

    # ---- 跨句统计 -------------------------------------------------------- #
    stats_html = []
    for lv, label, _h in LEVELS:
        v = [g[(t, lv)]["f0_std"] for t in ORDER]
        dur = [g[(t, lv)]["dur"] for t in ORDER]
        rms = [g[(t, lv)]["rms"] for t in ORDER]
        sd = st.pstdev(v)
        cls = ' class="best"' if lv == "light" else ""
        stats_html.append(
            "<tr%s><td>%s</td><td>%.1f</td><td>%.2f</td><td>%.2fs</td><td>%.2fdB</td></tr>"
            % (cls, label, st.mean(v), sd, max(dur) - min(dur), max(rms) - min(rms)))

    html = (TPL.replace("{ROWS}", "".join(rows_html))
               .replace("{STATS}", "".join(stats_html))
               .replace("{AUDIO}", "".join(audio_html)))
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s（%.1f MB）" % (OUT, os.path.getsize(OUT) / 1048576.0))


if __name__ == "__main__":
    main()
