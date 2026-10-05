# -*- coding: utf-8 -*-
"""把 _degree_probe 的三条音频拼成一张离线试听页（单文件，双击即听）。

同一句文本、同一音色、同一个种子，唯一变量是档位拼出来的那句话：
  不加指令 / 用感慨的语气说 / 用略带感慨的语气说

音频 base64 内联，不依赖服务与网络。

    python tools/probes/make_degree_page.py
"""

from __future__ import annotations

import base64
import io
import json
import os

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
D = os.path.join(HERE, "_degree_probe")
OUT = os.path.join(D, "listen.html")

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>情绪档位试听：不加 / 用X说 / 用略带X说</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--fg:#1f2328;--dim:#68707a;--hi:#b3392a;}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--fg);
font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:19px;margin:0 0 6px}
.sub{color:var(--dim);margin:0 0 16px;max-width:900px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.q{font-size:15px;font-weight:600;margin:0 0 4px}
.qt{font-size:12px;color:var(--dim);margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:8px}
.cell{border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:200px;flex:1 1 200px;transition:border-color .12s}
.cell.on{border-color:var(--hi);box-shadow:0 0 0 2px rgba(179,57,42,.13)}
.lv{font-size:13px;font-weight:600;margin-bottom:2px}
.ins{font-size:11px;color:#9aa1a9;min-height:15px;margin-bottom:8px}
button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--line);background:#fff;
border-radius:6px;cursor:pointer;color:var(--fg)}
button:hover{border-color:#b6bcc4}
button.pri{background:var(--fg);color:#fff;border-color:var(--fg)}
.m{font-size:11px;color:var(--dim);margin-top:8px;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--dim);font-weight:600}
audio{display:none}
.note{font-size:12px;color:var(--dim);margin-top:12px}
</style>
</head>
<body>
<h1>情绪档位试听</h1>
<p class="sub">同一句文本、同一音色、<b>同一个种子</b>（服务端种子由「文本 + 音色」派生，与语气指令无关），
所以三格起跑线完全一样，差别只在档位拼出来的那句话。点单格播放，
或点「依次播三档」一口气听完再判断。<b>F0 波动</b>是基频标准差，越大代表声音起伏越大。</p>

<div class="card">
  <div class="q">{TEXT}</div>
  <div class="qt">音色 {VOICE} · 三档同种子单变量对照</div>
  <div class="row">{CELLS}</div>
  <div style="margin-top:14px"><button class="pri" onclick="seq({IDS})">依次播三档</button></div>
</div>

<div class="card">
  <h1 style="font-size:16px">指标</h1>
  <table>
    <tr><th>档位</th><th>时长</th><th>响度</th><th>F0 均值</th><th>F0 波动</th></tr>
    {ROWS}
  </table>
  <p class="note">每格只有一条音频：种子确定，同一档重跑必然得到逐字节相同的一条波形。
  单样本只用于看趋势，不做统计推断。</p>
</div>

{AUDIO}

<script>
var cur = null;
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
  cur = a;
  a.play();
}
function seq(ids){
  stopAll();
  var i = 0;
  function step(){
    if(i >= ids.length) return;
    var id = ids[i++];
    var a = document.getElementById('au_' + id);
    var c = document.querySelector('[data-id="' + id + '"]');
    if(!a){ step(); return; }
    if(c) c.classList.add('on');
    a.onended = function(){ setTimeout(step, 400); };
    a.play();
  }
  step();
}
</script>
</body>
</html>
"""


def main():
    res = json.load(io.open(os.path.join(D, "degree.json"), encoding="utf-8"))
    rows = res["rows"]
    cells, audio, table = [], [], []
    ids = []
    for r in rows:
        rid = r["tag"].split()[0]          # A / B / C
        path = os.path.join(D, {"A": "A_bare.wav", "B": "B_light.wav",
                                "C": "C_none.wav"}[rid])
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        audio.append('<audio id="au_%s" preload="none" '
                     'src="data:audio/wav;base64,%s"></audio>' % (rid, b64))
        ids.append(rid)
        instr = {"A": "用感慨的语气说（现状口径）",
                 "B": "用略带感慨的语气说（light 档）",
                 "C": "不发任何语气指令"}[rid]
        cells.append(
            '<div class="cell" data-id="%s"><div class="lv">%s</div>'
            '<div class="ins">%s</div><button onclick="one(\'%s\')">播放</button>'
            '<div class="m">F0 波动 %s · %ss</div></div>'
            % (rid, r["tag"], instr, rid, r["f0_std"], r["dur"]))
        table.append(
            "<tr><td>%s</td><td>%.2fs</td><td>%.2fdB</td>"
            "<td>%.1fHz</td><td>%.1fHz</td></tr>"
            % (r["tag"], r["dur"], r["rms"], r["f0_mean"], r["f0_std"]))

    html = (TPL.replace("{TEXT}", res["text"])
               .replace("{VOICE}", res["voice"])
               .replace("{CELLS}", "".join(cells))
               .replace("{IDS}", json.dumps(ids))
               .replace("{ROWS}", "".join(table))
               .replace("{AUDIO}", "".join(audio)))
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s（%.2f MB）" % (OUT, os.path.getsize(OUT) / 1048576.0))


if __name__ == "__main__":
    main()
