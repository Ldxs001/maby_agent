# -*- coding: utf-8 -*-
"""把 _none_probe 的音频与指标拼成离线试听页（单文件，双击即听）。

页面按「句」分行，五档并排，其中前两档是本次的主对照：
    none_plain  改动后（档位=none → 不发指令）   ← 现在的声音
    old_plain   改动前（不发档位 → 发「用平静的语气说」） ← 之前的声音

    python tools/probes/make_none_page.py
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import wave

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
D = os.path.join(HERE, "_none_probe")
OUT = os.path.join(D, "listen.html")
SPEAKER = "Serena"

#: 主对照两档（页面里高亮）
KEY_ARMS = ("none_plain", "old_plain")

OFFICIAL = {
    "none_plain": "不发任何指令",
    "old_plain": "用平静的语气说",
    "light_plain": "用略带平静的语气说",
    "none_emo": "不发任何指令（档位把它挡了）",
    "old_emo": "用感慨的语气说",
}

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>none 档剪枝前后 · 听感对照</title>
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
.cell{border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:176px;flex:1 1 176px}
.cell.key{border-color:#b3392a;background:#fdf6f5}
.cell.on{box-shadow:0 0 0 2px rgba(179,57,42,.18)}
.lv{font-size:12px;font-weight:600;margin-bottom:1px}
.ins{font-size:11px;color:#9aa1a9;min-height:15px;margin-bottom:7px}
.badge{font-size:10px;font-weight:700;color:#fff;background:#b3392a;border-radius:4px;
padding:0 5px;margin-left:5px;vertical-align:1px}
button{font:inherit;font-size:12px;padding:4px 10px;border:1px solid var(--line);background:#fff;
border-radius:6px;cursor:pointer;color:var(--fg)}
button:hover{border-color:#b6bcc4}
button.pri{background:var(--fg);color:#fff;border-color:var(--fg)}
.m{font-size:11px;color:var(--dim);margin-top:7px;font-variant-numeric:tabular-nums;line-height:1.5}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--dim);font-weight:600}
audio{display:none}
.note{font-size:12px;color:var(--dim);margin-top:10px}
.fact{font-size:13px;margin:0;padding-left:18px}
.fact li{margin:3px 0}
.yes{color:var(--ok);font-weight:700}
.no{color:var(--hi);font-weight:700}
code{background:#eef0f3;padding:1px 5px;border-radius:4px;font-size:12px}
.hint{font-size:12px;color:var(--dim);margin:6px 0 0}
.qhead{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;margin-bottom:8px}
.qtext{font-size:14px;font-weight:600}
.qmeta{font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums}
</style>
</head>
<body>
<h1>none 档剪枝前后 · 听感对照</h1>
<p class="sub">用真实产品链路生成：只传 <code>emotion</code> 与 <code>degree</code>，不直接塞 <code>instruct</code>。
同一句文本、同一音色（Serena）、同一随机种子（种子由 音色+文本 派生，与档位无关），
所以<b>组间差别只能来自那一句指令本身</b>。<br>
<b>主对照</b>是每行的前两格：<span class="badge">改后</span>不发指令 与 <span class="badge">改前</span>发「用平静的语气说」。</p>

<div class="card">
  <h2>1 · 硬判据</h2>
  <ul class="fact">{FACTS}</ul>
  <p class="note">档位是硬开关而非措辞开关：<code>degree="none"</code> 时，脚本里填「平静」还是「感慨」
  得到的是<b>同一段音频</b>（逐字节相同）；不传档位时两条各自出指令、互不相同。</p>
</div>

<div class="card">
  <h2>2 · 逐句汇总（相对「不发指令」的变化）</h2>
  <table>
    <tr><th>句</th><th>档位</th><th>指令</th><th>时长 s</th><th>语速 字/s</th>
    <th>F0 均值</th><th>F0 波动</th><th>音色质心</th></tr>
    {STATS}
  </table>
  <p class="note">F0 均值 = 音高中心（越低越低沉）；F0 波动 = 句内音高起伏；
  音色质心 = 亮度。三项都是相对左侧「不发指令」的百分比变化。</p>
</div>

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
    if(i >= ids.length){ stopAll(); return; }
    var id = ids[i++];
    var a = document.getElementById('au_' + id);
    var c = document.querySelector('[data-id="' + id + '"]');
    if(!a){ step(); return; }
    if(c) c.classList.add('on');
    a.onended = function(){ setTimeout(step, 380); };
    a.play();
  }
  step();
}
</script>
</body>
</html>
"""


def wav_meta(path):
    with wave.open(path, "rb") as w:
        return w.getframerate(), w.getnframes(), w.getnchannels()


def dur_of(path):
    sr, n, _ch = wav_meta(path)
    return n / float(sr)


def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def pct(v, base):
    if not base:
        return "—"
    d = 100.0 * (v - base) / base
    return "%+.1f%%" % d


def seed_of(text):
    """与 serve.py 同源的种子派生：sha256(speaker + 文本)[:4]。"""
    import hashlib
    h = hashlib.sha256(("%s\x00%s" % (SPEAKER, text)).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def main():
    res = json.load(io.open(os.path.join(HERE, "_none_probe.json"), encoding="utf-8"))
    rows = res["rows"]
    levels = res["levels"]
    texts = res["texts"]
    order = [lv["key"] for lv in levels]

    # ---- 服务端日志：本次真实发出去了哪些指令 ---- #
    log_path = os.path.join(os.path.dirname(HERE), "tts_service", "_audit_none.log")
    said = 0
    total = 0
    if os.path.exists(log_path):
        for ln in io.open(log_path, encoding="utf-8", errors="replace"):
            if "[合成]" in ln:
                total += 1
                if "语气" in ln:
                    said += 1

    facts = []
    # 判定 A
    okA = all(
        next((r for r in rows if r["text_tag"] == t["tag"] and r["level"] == "none_plain"),
             {}).get("md5")
        == next((r for r in rows if r["text_tag"] == t["tag"] and r["level"] == "none_emo"),
                {}).get("md5")
        for t in texts)
    facts.append('<li>档位 <code>none</code> 下填「平静」与填「感慨」，三句全部<span class="%s">逐字节一致</span>'
                 ' —— 该档不再翻译任何情绪词。</li>' % ("yes" if okA else "no"))
    okB = all(
        next((r for r in rows if r["text_tag"] == t["tag"] and r["level"] == "old_plain"),
             {}).get("md5")
        != next((r for r in rows if r["text_tag"] == t["tag"] and r["level"] == "old_emo"),
                {}).get("md5")
        for t in texts)
    facts.append('<li>不传档位时，「平静」与「感慨」三句全部<span class="%s">互不相同</span>'
                 ' —— 旧行为未被误伤，指令照发。</li>' % ("yes" if okB else "no"))
    facts.append('<li>本次 %d 段合成中，服务端日志记录到 <b>%d 段带语气指令</b>、<b>%d 段无指令</b>'
                 '（无指令的正是 <code>none</code> 档那两组）。</li>'
                 % (total, said, total - said))

    # ---- 汇总表 ---- #
    stats = []
    for t in texts:
        base = next((r for r in rows if r["text_tag"] == t["tag"] and r["level"] == "none_plain"),
                    None)
        for i, key in enumerate(order):
            r = next((x for x in rows if x["text_tag"] == t["tag"] and x["level"] == key), None)
            if not r or not base:
                continue
            first = ('<td rowspan="%d">%s<br><span class="qmeta">%s</span></td>'
                     % (len(order), t["tag"], t["text"][:8] + "…")) if i == 0 else ""
            mark = ' style="font-weight:600"' if key in KEY_ARMS else ""
            stats.append("<tr>%s<td%s>%s</td><td>%s</td><td>%.2f</td><td>%.2f</td>"
                         "<td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (first, mark, key, OFFICIAL.get(key, "—"),
                            r["dur"], r["rate"],
                            pct(r["f0_mean"], base["f0_mean"]),
                            pct(r["f0_std"], base["f0_std"]),
                            pct(r["centroid"], base["centroid"])))

    # ---- 逐句试听行 ---- #
    rows_html = []
    for t in texts:
        tag = t["tag"]
        ids = ["%s_%s" % (tag, k) for k in order]
        cells = []
        for key in order:
            rid = "%s_%s" % (tag, key)
            r = next((x for x in rows if x["text_tag"] == tag and x["level"] == key), None)
            if not r:
                continue
            cls = "cell key" if key in KEY_ARMS else "cell"
            badge = ('<span class="badge">%s</span>' % ("改后" if key == "none_plain"
                                                        else "改前")) if key in KEY_ARMS else ""
            cells.append(
                '<div class="%s" data-id="%s">'
                '<div class="lv">%s%s</div><div class="ins">%s</div>'
                '<button class="%s" onclick="one(\'%s\')">播放</button>'
                '<div class="m">时长 %.2fs · 语速 %.2f 字/s<br>F0 %.0f±%.0f · 质心 %.0f<br>'
                '%d KB · %s</div></div>'
                % (cls, rid, key, badge, OFFICIAL.get(key, ""),
                   "pri" if key in KEY_ARMS else "", rid,
                   r["dur"], r["rate"], r["f0_mean"], r["f0_std"], r["centroid"],
                   r["bytes"] // 1024, r["md5"]))
        rows_html.append(
            '<div class="card">\n'
            '  <div class="qhead"><span class="qtext">「%s」</span>'
            '<span class="qmeta">%d 字 · 种子 %d</span></div>\n'
            '  <div class="row">%s</div>\n'
            '  <p class="hint">顺序听：<button class="pri" onclick="seq([%s])">'
            '顺次播 5 档</button></p>\n'
            '</div>' % (t["text"], len(t["text"]), seed_of(t["text"]),
                        "".join(cells),
                        ",".join("'%s'" % i for i in ids)))

    audio = []
    for t in texts:
        for key in order:
            p = os.path.join(D, "%s_%s.wav" % (t["tag"], key))
            if not os.path.exists(p):
                continue
            audio.append('<audio id="au_%s_%s" preload="none" src="data:audio/wav;base64,%s"></audio>'
                         % (t["tag"], key, b64(p)))

    html = (TPL.replace("{FACTS}", "".join(facts))
               .replace("{STATS}", "".join(stats))
               .replace("{ROWS}", "".join(rows_html))
               .replace("{AUDIO}", "".join(audio)))
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("写入 %s（%.2f MB）" % (OUT, len(html.encode("utf-8")) / 1048576.0))


if __name__ == "__main__":
    main()
