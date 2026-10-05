# -*- coding: utf-8 -*-
"""把 _pred_temp_probe 的音频与指标拼成一张离线试听页（单文件，双击即听）。

为什么要有这一步：指标表只能看、不能听，而「哪一档更稳」最终要靠耳朵定。
页面按「句 × 随机种子」分行，每行四个档位（现状 0.9 / 0.6 / 0.4 / 贪心）并排，
点单格播放；一行一键顺次播完，直接听同一句在四个温度下的差别。

音频一律 base64 内联（转 96k mp3 压体积），不依赖服务、不依赖网络。

    python tools/probes/make_pred_temp_page.py
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
D = os.path.join(HERE, "_pred_temp_probe")
OUT = os.path.join(D, "listen.html")

LEVELS = [("P0.9", "现状 0.9", "库默认，服务端够不着"),
          ("P0.6", "0.6", "中间档"),
          ("P0.4", "0.4", "目标档（与 talker 对齐）"),
          ("greedy", "贪心", "不做随机采样（极限对照）")]

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>predictor 温度对照试听</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--fg:#1f2328;--dim:#68707a;--hi:#b3392a;}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--fg);
font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:19px;margin:0 0 6px}
h2{font-size:16px;margin:0 0 10px}
.sub{color:var(--dim);margin:0 0 18px;max-width:960px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
.q{font-size:15px;font-weight:600;margin:0 0 4px}
.qt{font-size:12px;color:var(--dim);margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:stretch}
.cell{border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:158px;flex:1 1 158px}
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
code{background:#eef0f3;padding:1px 5px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<h1>predictor（15 个子码本）采样温度对照试听</h1>
<p class="sub">Qwen3-TTS 有两处采样。<b>talker</b>（第 1 码本）走 <code>temperature</code> 参数，
服务端传的 0.4 管得到；<b>code predictor</b>（另外 15 个子码本，决定音色细节与韵律）走
<code>PredictorGraph</code>，温度在构造 CUDA 图时写死 0.9，服务端传不进去。
本页把那一档重建出来，其余参数一字不动（talker 仍 0.4 / top_k 50 / top_p 1.0 / 惩罚 1.05），
听同一句在同一随机种子下换温度后有没有差。<br>
指标：<b>F0 中位</b>是音高中心，<b>F0 波动</b>是句内基频标准差（越大声音起伏越大），
<b>质心</b>是谱质心（音色亮度粗代理）。</p>

<div class="card">
  <h2>1 · 换图是否生效 / 图内采样是否确定性</h2>
  <ul class="fact">{FACTS}</ul>
</div>

<div class="card">
  <h2>2 · 按档汇总（三句 × 四种子，贪心档只有一句两种子）</h2>
  <table>
    <tr><th>档位</th><th>F0 中位（跨种子中位）</th><th>F0 中位极差</th>
    <th>F0 波动均值</th><th>质心中位</th><th>质心极差</th><th>时长极差</th><th>顶到上限</th></tr>
    {STATS}
  </table>
  <p class="note">F0 中位极差 / 质心极差 = 同一句话换随机种子后，音高中心与音色亮度漂了多远。
  这两个数越小，说明「同一个人念同一句话」越稳定。顶到上限 = 生成帧数跑满按字数设的上限，
  即模型没在按文本念（退化）。<b>注意：贪心档只有一句两种子，上表的极差与前三档不可直接比</b>，
  请看下表的同口径对照。</p>
</div>

<div class="card">
  <h2>3 · 同口径对照（只用 S1 的两个种子，四档样本量一致）</h2>
  <table>
    <tr><th>档位</th><th>F0 中位（两个种子）</th><th>质心中位（两个种子）</th></tr>
    {SAME}
  </table>
  <p class="note" style="margin-top:10px">这一组才说明「换 predictor 温度」本身值不值：说话人、文本、种子起跑线完全相同，
  唯一的差别就是那 15 个子码本怎么采样。若温度真有稳定作用，应当看到极差单调收窄。</p>

  <h2 style="margin-top:18px">4 · 副作用：长句语速被拖慢（S2 · 21 字）</h2>
  <table>
    <tr><th>档位</th><th>时长中位</th><th>语速</th><th>四个种子明细（秒）</th></tr>
    {RATE}
  </table>
  <p class="note">同一句话，降温后越念越长。播客的时长是按脚本字数预估的，
  语速被拖慢会直接顶穿整期时长门禁。</p>
</div>

{ROWS}

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
    """转 96k mp3 压体积，用于 base64 内联。ffmpeg 不在就退回原 wav。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    dst = os.path.join(tempfile.gettempdir(), os.path.basename(src) + ".mp3")
    r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src,
                        "-codec:a", "libmp3lame", "-b:a", "96k", "-ar", "24000", dst],
                       capture_output=True)
    if r.returncode != 0 or not os.path.exists(dst):
        return None
    return dst


def main() -> None:
    res = json.load(io.open(os.path.join(D, "probe.json"), encoding="utf-8"))
    rows = res["rows"]
    key_of = {(r["tag"], r["seed"], r["temp_key"]): r for r in rows}
    seeds = res["seeds"]
    order: list[str] = []
    for r in rows:
        if r["tag"] not in order:
            order.append(r["tag"])
    texts = [(tag, next(r["text"] for r in rows if r["tag"] == tag)) for tag in order]

    audio_html = []
    rows_html = []

    for tag, text in texts:
        seed_blocks = []
        for seed in seeds:
            cells = []
            ids = []
            for key, label, hint in LEVELS:
                rec = key_of.get((tag, seed, key))
                if not rec:
                    continue
                rid = "%s_%s_%d" % (tag, key, seed)
                path = os.path.join(D, rec["wav"])
                if not os.path.exists(path):
                    continue
                mp3 = to_mp3(path)
                src = mp3 or path
                with open(src, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                mime = "audio/mpeg" if mp3 else "audio/wav"
                audio_html.append(
                    '<audio id="au_%s" preload="none" src="data:%s;base64,%s"></audio>'
                    % (rid, mime, b64))
                ids.append(rid)
                flag = ' <span style="color:#b3392a;font-weight:700">顶上限</span>' if rec["hit_cap"] else ""
                cells.append(
                    '<div class="cell" data-id="%s">'
                    '<div class="lv">%s</div><div class="ins">%s</div>'
                    '<button onclick="one(\'%s\')">播放</button>'
                    '<div class="m">F0 %s Hz · 波动 %s<br>质心 %s Hz · %s s%s</div></div>'
                    % (rid, label, hint, rid, rec["f0_mean"], rec["f0_std"],
                       rec["centroid"], rec["dur"], flag))
            if not cells:
                continue
            seed_blocks.append(
                '<div class="seedrow"><div class="seedtag">种子 %d</div>'
                '<div class="row" style="flex:1 1 auto">%s</div></div>'
                % (seed, "".join(cells)))

        all_ids = []
        for seed in seeds:
            all_ids.append(json.dumps(["%s_%s_%d" % (tag, k, seed) for k, _l, _h in LEVELS
                                       if key_of.get((tag, seed, k))]))
        rows_html.append(
            '<div class="card"><h2>%s ｜ %s</h2>'
            '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
            '%s'
            '<button class="pri" onclick=\'seq(%s)\'>依次播第一行的四档</button></div>'
            '%s</div>'
            % (tag, text,
               "".join('<button onclick=\'seq(%s)\'>播种子 %d 的四档</button>' % (ids, s)
                       for ids, s in zip(all_ids, seeds)),
               all_ids[0] if all_ids else "[]",
               "".join(seed_blocks)))

    # ---- 汇总表 ---- #
    stats_html = []
    for key, label, _h in LEVELS:
        s = res["summary"].get(key)
        if not s:
            continue
        stats_html.append(
            "<tr><td>%s</td><td>%.1f Hz</td><td>%.1f Hz</td><td>%.1f Hz</td>"
            "<td>%.0f Hz</td><td>%.0f Hz</td><td>%.2f s</td><td>%d/%d</td></tr>"
            % (label, s["f0_mean_median"], s["f0_mean_range"], s["f0_std_mean"],
               s["centroid_median"], s["centroid_range"], s["dur_range"],
               s["cap_hits"], s["n"]))

    # ---- 同口径对照 + 语速副作用 ---- #
    import statistics as st

    ref_seeds = seeds[:2]
    same_html = []
    for key, label, _h in LEVELS:
        v = sorted([r for r in rows if r["temp_key"] == key and r["tag"] == "S1"
                    and r["seed"] in ref_seeds], key=lambda r: r["seed"])
        if len(v) < 2:
            continue
        f = [r["f0_mean"] for r in v]
        c = [r["centroid"] for r in v]
        same_html.append(
            "<tr><td>%s</td><td>%.1f → %.1f（极差 %.1f）</td>"
            "<td>%.0f → %.0f（极差 %.0f）</td></tr>"
            % (label, f[0], f[1], max(f) - min(f), c[0], c[1], max(c) - min(c)))

    rate_html = []
    for key, label, _h in LEVELS[:3]:
        v = [r for r in rows if r["temp_key"] == key and r["tag"] == "S2"]
        if not v:
            continue
        durs = [r["dur"] for r in v]
        med = st.median(durs)
        rate_html.append(
            "<tr><td>%s</td><td>%.2f s</td><td>%.2f 字/秒</td><td>%s</td></tr>"
            % (label, med, len(v[0]["text"]) / med, " / ".join("%.2f" % d for d in durs)))

    # ---- 事实行 ---- #
    facts = []
    caps = res["caps"]
    for key, label, _h in LEVELS:
        c = caps.get(key)
        if not c:
            continue
        d = c.get("determinism") or {}
        facts.append(
            "<li><b>%s</b>：重建图内 <code>temperature=%s</code>，捕获耗时 %.1fs；"
            "同种子重复两次，predictor 输出 token %s，波形 %s。</li>"
            % (label, c["graph_temperature"], c["capture_s"],
               "一致" if d.get("pred_tokens_same") else "不一致",
               "一致" if d.get("wav_same") else "不一致"))

    html = (TPL.replace("{FACTS}", "".join(facts))
               .replace("{STATS}", "".join(stats_html))
               .replace("{SAME}", "".join(same_html))
               .replace("{RATE}", "".join(rate_html))
               .replace("{ROWS}", "".join(rows_html))
               .replace("{AUDIO}", "".join(audio_html)))
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成 %s（%.2f MB）" % (OUT, os.path.getsize(OUT) / 1048576.0))


if __name__ == "__main__":
    main()
