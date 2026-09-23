# -*- coding: utf-8 -*-
"""富语调 ref 实验试听报告：8 句疑问 × 3 条件 + ref 本体 + 陈述句音色对照。
全部音频 base64 内嵌，单文件零依赖。运行：python tools/probes/build_richref_report.py
"""
import base64
import io
import json
import os
import re
import wave

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "_richref_report.html")

CONDS = [
    ("A", "旧基线 · 陈述句 ref", "现状：ICL 参考音频是一条平稳陈述句", "q"),
    ("B", "新 ref · 无指令版", "CustomVoice 合的混合语调 ref，没带 instruct", "plain"),
    ("C", "新 ref · instruct 版", "同一条混合 ref，合成时带官方 instruct 指令", "instruct"),
]


def texts():
    sc = json.load(io.open(os.path.join(
        ROOT, "projects", "20260915-103249", "脚本", "1.json"),
        encoding="utf-8"))
    lines = sc if isinstance(sc, list) else (sc.get("lines") or [])
    qs = [(l.get("text") or "").rstrip() for l in lines
          if (l.get("text") or "").rstrip().endswith("？")
          and 12 <= len((l.get("text") or "").rstrip()) <= 34]
    return qs[:8]


def wav_info(b):
    try:
        w = wave.open(io.BytesIO(b))
        return w.getnframes() / float(w.getframerate()), w.getframerate()
    except Exception:
        return 0.0, 0


def emb(path):
    raw = open(path, "rb").read()
    dur, sr = wav_info(raw)
    return base64.b64encode(raw).decode("ascii"), dur, sr


def main():
    ts = texts()
    extra = ""
    n_extra = 0
    # ref 本体与陈述句对照
    for name, label, note in [
        ("_richref/ref_mixed_instruct.wav", "ref 本体 · instruct 版（20.5s）",
         "CustomVoice+Vivian+官方指令合成的混合语调参考音频：陈述。疑问？惊叹！"),
        ("_richref/ref_mixed_plain.wav", "ref 本体 · 无指令版（7.1s）",
         "同一文本，不带指令"),
        ("_ab_out/richref_instruct/st.wav", "陈述句 · 用 instruct 版 ref 合成",
         "检查音色有没有因为换 ref 而漂走（原文：这条路径的选择并不复杂……）"),
    ]:
        b64, dur, sr = emb(os.path.join(HERE, name))
        n_extra += 1
        extra += """
    <section class="card single">
      <div class="card-head"><div class="grp-tag">%s</div>
        <div class="grp-text">%s</div></div>
      <div class="conds"><div class="cond"><div class="cond-note">%s</div>
        <audio controls preload="none" src="data:audio/wav;base64,%s"></audio>
      </div></div>
    </section>""" % (("%.1fs / %dHz" % (dur, sr)), label, note, b64)

    cards = []
    for i, t in enumerate(ts):
        auds = []
        for letter, label, note, sub in CONDS:
            if sub == "q":
                path = os.path.join(HERE, "_ab_out", "_baseline",
                                    "%02d.wav" % i)
            else:
                path = os.path.join(HERE, "_ab_out",
                                    "richref_" + sub, "q%02d.wav" % (i + 1))
            b64, dur, sr = emb(path)
            auds.append("""
      <div class="cond" id="g%d-%s">
        <div class="cond-head"><span class="badge b%s">%s</span>
          <span class="cond-label">%s</span>
          <span class="dur">%.1fs</span></div>
        <div class="cond-note">%s</div>
        <audio controls preload="none" src="data:audio/wav;base64,%s"></audio>
      </div>""" % (i, letter, letter, letter, label, dur, note, b64))
        cards.append("""
    <section class="card" id="grp%d">
      <div class="card-head"><div class="grp-tag">第 %d 组</div>
        <div class="grp-text">%s</div>
        <button class="playall" onclick="playGroup(%d)">连播 A-B-C</button>
        <button class="stopall" onclick="stopAll()">停</button></div>
      <div class="conds">%s
      </div>
    </section>""" % (i, i + 1, t, i, "".join(auds)))

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>富语调参考音频实验 · 试听报告</title>
<style>
  :root { --ink:#1c2330; --paper:#fff; --grid:#eef1f5; --muted:#6b7686;
          --line:#dde3ea; --accent:#3b6ef5; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:"Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--grid); color:var(--ink); font-size:15px; }
  header.top { background:#161d2b; color:#e8edf5; padding:22px 28px; }
  header.top h1 { font-size:19px; font-weight:600; letter-spacing:.5px; }
  header.top .sub { margin-top:7px; font-size:13px; color:#9aa7bb; line-height:1.7; }
  header.top .sub b { color:#cdd8ea; }
  main { max-width:1080px; margin:0 auto; padding:22px 20px 60px; }
  .verdict { background:var(--paper); border:1px solid var(--line);
    border-radius:10px; padding:16px 20px; margin-bottom:20px;
    font-size:13.5px; color:var(--muted); line-height:2.0; }
  .verdict b { color:var(--ink); }
  .verdict table { border-collapse:collapse; margin:10px 0 4px; width:100%; }
  .verdict td, .verdict th { border:1px solid var(--line); padding:5px 10px;
    font-size:12.5px; text-align:left; }
  .win { color:#1f7a4d; font-weight:700; }
  .card { background:var(--paper); border:1px solid var(--line);
    border-radius:10px; margin-bottom:20px; overflow:hidden; }
  .card-head { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    padding:14px 18px; border-bottom:1px solid var(--line); background:#f7f9fc; }
  .grp-tag { background:var(--accent); color:#fff; border-radius:6px;
    padding:3px 10px; font-size:12px; white-space:nowrap; }
  .grp-text { font-size:15px; font-weight:600; flex:1; min-width:260px; }
  button { border:1px solid var(--line); background:#fff; color:var(--ink);
    border-radius:6px; padding:5px 14px; font-size:13px; cursor:pointer; }
  button.playall { border-color:var(--accent); color:var(--accent); }
  button.playall:hover { background:var(--accent); color:#fff; }
  .conds { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
    gap:1px; background:var(--line); }
  .cond { background:#fff; padding:14px 16px 18px; }
  .cond.playing { background:#f2f7ff; }
  .cond-head { display:flex; align-items:center; gap:8px; }
  .badge { width:24px; height:24px; border-radius:50%; color:#fff;
    display:inline-flex; align-items:center; justify-content:center;
    font-size:12.5px; font-weight:700; flex:none; }
  .bA { background:#6b7686; } .bB { background:#b0681f; } .bC { background:#1f7a4d; }
  .cond-label { font-weight:600; font-size:13.5px; }
  .dur { margin-left:auto; font-size:11.5px; color:var(--muted); }
  .cond-note { font-size:12px; color:var(--muted); margin:7px 0 10px; line-height:1.6; }
  audio { width:100%; height:34px; }
</style>
</head>
<body>
<header class="top">
  <h1>富语调参考音频实验 · 试听报告</h1>
  <div class="sub">你的提案：用 CustomVoice（官方支持指令）合一条含<b>陈述。疑问？惊叹！</b>的
    参考音频，让 Base+ICL 克隆它时继承语调多样性。<br>
    8 组疑问句 × 3 条件，同种子同音色，唯一变量是参考音频。</div>
</header>
<main>
  <div class="verdict">
    <b>测量结论（8 句疑问句中位数）：</b>
    <table>
      <tr><th>条件</th><th>整体音高</th><th>句末抬高</th><th>判定</th></tr>
      <tr><td>A 旧基线（陈述 ref）</td><td>+2.40</td><td>+3.14</td><td>句尾不上扬</td></tr>
      <tr><td>B 新 ref · 无指令版</td><td>+4.04（+1.65）</td><td>+4.09（+0.95）</td><td>整句变高，非句尾效果</td></tr>
      <tr><td>C 新 ref · instruct 版</td><td>+2.81（不变）</td><td class="win">+7.44（+4.30）</td>
          <td class="win">句尾精准扬起，远超可辨阈</td></tr>
    </table>
    <b>听点：</b>① C 的句尾上扬是否明显；② C 相对 A 音色是否还是同一个人；
    ③ B 的"整句变高"听着是否自然。
  </div>
  @@EXTRA@@
  @@CARDS@@
</main>
<script>
var SEQ = ["A","B","C"];
function stopAll(){document.querySelectorAll("audio").forEach(function(a){a.pause();});
  document.querySelectorAll(".cond").forEach(function(d){d.classList.remove("playing");});}
function playGroup(g){stopAll();var s=SEQ.slice();
  (function next(){if(!s.length)return;var l=s.shift();
    var box=document.getElementById("g"+g+"-"+l);
    var a=box&&box.querySelector("audio");if(!a){next();return;}
    box.classList.add("playing");a.onended=function(){box.classList.remove("playing");next();};a.play();})();}
document.querySelectorAll("audio").forEach(function(a){
  a.addEventListener("play",function(){
    document.querySelectorAll("audio").forEach(function(o){if(o!==a)o.pause();});});});
</script>
</body>
</html>"""
    html = html.replace("@@EXTRA@@", extra).replace("@@CARDS@@", "".join(cards))
    io.open(OUT, "w", encoding="utf-8").write(html)
    print("已生成 %s（%.1f MB）" % (OUT, os.path.getsize(OUT) / 1048576.0))


if __name__ == "__main__":
    main()
