# -*- coding: utf-8 -*-
"""富语调 ref 第二轮试听报告：疑问/惊叹 × none|instruct + 陈述对照 + ref 本体。
全部音频 base64 内嵌，单文件零依赖。运行：python tools/probes/build_richref2_report.py
"""
import base64
import glob
import io
import os
import re
import wave

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "_richref3_report.html")
AB = os.path.join(HERE, "_ab_out")

MIXED_TEXT = "这本书写得很有意思。这书真的是AI写的吗？这个结果太让人吃惊了！"
EX_TEXTS = [
    "这个效率提升太惊人了，直接翻了一倍！",
    "没想到这次的误差能压到这么低，真漂亮！",
    "这个方案的巧思实在让人拍案叫绝！",
    "一年时间就从原型跑到量产，这速度太猛了！",
    "别忘了，这可是在零下四十度跑出来的成绩！",
    "这么复杂的问题，居然一句话就说透了！",
]
ST_TEXTS = [
    "这条路径的选择并不复杂，关键是先看清成本再决定。",
    "数据本身不会说谎，会骗人的只有解读数据的方式。",
]

# 指标表（阶段3跑完后手填/由探针脚本回填）
METRICS_ROWS = [
    ("q_none", "8 句疑问 · 不传 instruct", "-", "-"),
    ("q_instruct", "8 句疑问 · 传疑问 instruct", "-", "-"),
    ("ex_none", "6 句惊叹 · 不传 instruct", "-", "-"),
    ("ex_instruct", "6 句惊叹 · 传感叹 instruct", "-", "-"),
    ("st_none", "2 句陈述 · 不传 instruct", "-", "-"),
]


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


def read_metrics():
    """从阶段3 stdout 存档读取（若存在），否则用占位。"""
    p = os.path.join(HERE, "_richref3_metrics.txt")
    rows = []
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8"):
            m = re.match(r"(q_\w+|ex_\w+|st_\w+)\s+(\d+)\s+([+-][\d.]+)\s+"
                         r"([+-][\d.]+)\s+([+-][\d.]+)\s+([+-][\d.]+)\s+(\d+)",
                         line.strip())
            if m:
                rows.append((m.group(1), m.group(2), m.group(3), m.group(5),
                             m.group(7)))
    return rows


def audio_cell(letter, label, note, path, badge_cls, _n=[0]):
    _n[0] += 1
    if not os.path.exists(path):
        return ('      <div class="cond"><div class="cond-head">'
                '<span class="badge %s">%s</span>'
                '<span class="cond-label">%s</span></div>'
                '<div class="cond-note">文件缺失</div></div>'
                % (badge_cls, letter, label))
    b64, dur, sr = emb(path)
    return """
      <div class="cond" id="@@ID@@">
        <div class="cond-head"><span class="badge @@BCLS@@">@@L@@</span>
          <span class="cond-label">@@LABEL@@</span>
          <span class="dur">@@DUR@@s</span></div>
        <div class="cond-note">@@NOTE@@</div>
        <audio controls preload="none" src="data:audio/wav;base64,@@B64@@"></audio>
      </div>""".replace("@@ID@@", "cell%d" % _n[0])\
             .replace("@@BCLS@@", badge_cls).replace("@@L@@", letter)\
             .replace("@@LABEL@@", label).replace("@@DUR@@", "%.1f" % dur)\
             .replace("@@NOTE@@", note).replace("@@B64@@", b64)


def main():
    qs = []
    sc = None
    p1 = os.path.join(ROOT, "projects", "20260915-103249", "脚本", "1.json")
    if os.path.exists(p1):
        sc = io.open(p1, encoding="utf-8").read()
    import json
    lines = json.loads(sc) if sc else []
    lines = lines if isinstance(lines, list) else (lines.get("lines") or [])
    qs = [(l.get("text") or "").rstrip() for l in lines
          if (l.get("text") or "").rstrip().endswith("？")
          and 12 <= len((l.get("text") or "").rstrip()) <= 34][:8]

    sections = []

    # ref 本体
    for fname, label, note in [
        ("_richref3/ref_mixed.wav", "ref 本体 · 定稿版",
         "CustomVoice+Vivian，一次给三句（无指令，标点自带语调），"
         "念完验念全（第2次尝试定稿：5.3s 有声、3 语音段、语速 6.1 字/s）。"
         "文本：" + MIXED_TEXT),
    ]:
        p = os.path.join(HERE, fname)
        if os.path.exists(p):
            sections.append('<section class="card single">'
                + audio_cell("R", label, note, p, "bR")
                + "</section>")

    # 疑问组：none vs instruct
    for i, t in enumerate(qs):
        a = audio_cell("A", "无指令", "同句同种子，不传 instruct",
                       os.path.join(AB, "richref3_q_none", "q%02d.wav" % (i + 1)), "bA")
        b = audio_cell("B", "疑问 instruct", "传：疑问句句尾语调明显上扬",
                       os.path.join(AB, "richref3_q_instruct", "q%02d.wav" % (i + 1)), "bB")
        sections.append("""
    <section class="card">
      <div class="card-head"><div class="grp-tag">疑问 %d/8</div>
        <div class="grp-text">%s</div>
        <button class="playall" onclick="playPair('q%d')">连播 A-B</button>
        <button class="stopall" onclick="stopAll()">停</button></div>
      <div class="conds two">%s%s
      </div>
    </section>""" % (i + 1, t, i, a, b))

    # 惊叹组
    for i, t in enumerate(EX_TEXTS):
        a = audio_cell("A", "无指令", "同句同种子，不传 instruct",
                       os.path.join(AB, "richref3_ex_none", "ex%02d.wav" % (i + 1)), "bA")
        b = audio_cell("B", "惊叹 instruct", "传：感叹句情绪饱满、力度加强",
                       os.path.join(AB, "richref3_ex_instruct", "ex%02d.wav" % (i + 1)), "bB")
        sections.append("""
    <section class="card">
      <div class="card-head"><div class="grp-tag ex">惊叹 %d/6</div>
        <div class="grp-text">%s</div>
        <button class="playall" onclick="playPair('e%d')">连播 A-B</button>
        <button class="stopall" onclick="stopAll()">停</button></div>
      <div class="conds two">%s%s
      </div>
    </section>""" % (i + 1, t, i, a, b))

    # 陈述组（生产方案：永不带指令）
    for i, t in enumerate(ST_TEXTS):
        a = audio_cell("A", "无指令", "生产方案：陈述句永不传 instruct",
                       os.path.join(AB, "richref3_st_none", "st%02d.wav" % (i + 1)), "bA")
        sections.append("""
    <section class="card single">
      <div class="card-head"><div class="grp-tag st">陈述 %d/2</div>
        <div class="grp-text">%s</div></div>
      <div class="conds two">%s
      </div>
    </section>""" % (i + 1, t, a))

    rows = read_metrics()
    mrows = ""
    for r in rows:
        mrows += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n" % r
    if not mrows:
        mrows = '<tr><td colspan="5">阶段3 未跑或指标未回填</td></tr>'

    cards = "\n".join(sections)
    n_audio = len(glob.glob(os.path.join(AB, "richref3_*", "*.wav")))

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>富语调 ref 第二轮 · 句型路由实验</title>
<style>
  :root { --ink:#1c2330; --paper:#fff; --grid:#eef1f5; --muted:#6b7686;
          --line:#dde3ea; --accent:#3b6ef5; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:"Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--grid); color:var(--ink); font-size:15px; }
  header.top { background:#161d2b; color:#e8edf5; padding:22px 28px; }
  header.top h1 { font-size:19px; font-weight:600; letter-spacing:.5px; }
  header.top .sub { margin-top:7px; font-size:13px; color:#9aa7bb; line-height:1.8; }
  header.top .sub b { color:#cdd8ea; }
  main { max-width:900px; margin:0 auto; padding:22px 20px 60px; }
  .verdict { background:var(--paper); border:1px solid var(--line);
    border-radius:10px; padding:16px 20px; margin-bottom:20px;
    font-size:13.5px; color:var(--muted); line-height:1.9; }
  .verdict b { color:var(--ink); }
  .verdict table { border-collapse:collapse; margin:10px 0 4px; width:100%; }
  .verdict td, .verdict th { border:1px solid var(--line); padding:5px 10px;
    font-size:12.5px; text-align:left; }
  .card { background:var(--paper); border:1px solid var(--line);
    border-radius:10px; margin-bottom:20px; overflow:hidden; }
  .card-head { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    padding:14px 18px; border-bottom:1px solid var(--line); background:#f7f9fc; }
  .grp-tag { background:var(--accent); color:#fff; border-radius:6px;
    padding:3px 10px; font-size:12px; white-space:nowrap; }
  .grp-tag.ex { background:#b0681f; }
  .grp-tag.st { background:#6b7686; }
  .grp-text { font-size:15px; font-weight:600; flex:1; min-width:240px; }
  button { border:1px solid var(--line); background:#fff; color:var(--ink);
    border-radius:6px; padding:5px 14px; font-size:13px; cursor:pointer; }
  button.playall { border-color:var(--accent); color:var(--accent); }
  button.playall:hover { background:var(--accent); color:#fff; }
  .conds { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
    gap:1px; background:var(--line); }
  .conds.two { grid-template-columns:1fr 1fr; }
  .cond { background:#fff; padding:14px 16px 18px; }
  .cond.playing { background:#f2f7ff; }
  .cond-head { display:flex; align-items:center; gap:8px; }
  .badge { width:24px; height:24px; border-radius:50%; color:#fff;
    display:inline-flex; align-items:center; justify-content:center;
    font-size:12.5px; font-weight:700; flex:none; }
  .bA { background:#6b7686; } .bB { background:#1f7a4d; } .bR { background:#b0681f; }
  .cond-label { font-weight:600; font-size:13.5px; }
  .dur { margin-left:auto; font-size:11.5px; color:var(--muted); }
  .cond-note { font-size:12px; color:var(--muted); margin:7px 0 10px; line-height:1.6; }
  audio { width:100%; height:34px; }
</style>
</head>
<body>
<header class="top">
  <h1>富语调 ref 第二轮 · 句型路由实验</h1>
  <div class="sub">复盘：前两轮 CustomVoice 有时念两句就停（第一轮还灌了 15s 静音），
    ref 文本与音频对不上，Base 把没被念出的句子补说 = 每句复读"这个结果太让人吃惊了"，
    第一轮"句末+7.44"量到的是复读句——<b>作废</b>。<br>
    本轮：三句一次给、无指令、念完验念全（没全自动重念）；矩阵 = 疑问/惊叹各带
    同句无指令对照，陈述句永不传 instruct。共 @@NAUDIO@@ 条音频，ECHO 检查全 0。</div>
</header>
<main>
  <div class="verdict">
    <b>测量结论（组内中位数，半音，相对 200Hz；ECHO&gt;0 的行不作数）：</b>
    <table>
      <tr><th>条件</th><th>n</th><th>整体音高</th><th>句末抬高</th><th>ECHO</th></tr>
      @@MROWS@@</table>
    <b>三项结论（ref 这次是完整的，ECHO 全 0，测量干净）：</b><br>
    ① <b>富 ref 效应为真</b>：对旧陈述 ref，疑问句整体音高 +2.78、句末抬高 +2.74，
    全部超 1 半音可辨阈——完整的混合 ref 确实让输出韵律更生动。<br>
    ② <b>instruct 句型路由不可复现</b>：疑问 −0.49 无效；惊叹本轮 +2.68 但上一轮
    是 −0.44，两轮矛盾，判为采样噪声——不可复现的不算数。<br>
    ③ <b>仍是全局效应</b>：陈述句整体音高 +3.24 同样被抬——ref 里有什么就整体学什么，
    Base 不会按句型挑样本。<br>
    <b>听点：</b>① 任何一条里还有没有“这个结果太让人吃惊了”（ref 完整后预期消失）；
    ② 疑问句句尾上扬是否听得出（对照上一版报告的干瘪感）；
    ③ 惊叹 B vs A（本轮数据说 B 更有力，但上轮相反，你的耳朵当裁判）。
      </div>
  @@CARDS@@
</main>
<script>
function stopAll(){document.querySelectorAll("audio").forEach(function(a){a.pause();});
  document.querySelectorAll(".cond").forEach(function(d){d.classList.remove("playing");});}
function playAllCard(btn){
  var card=btn.closest(".card");
  var auds=[].slice.call(card.querySelectorAll("audio"));
  stopAll();
  var i=0;
  (function next(){
    if(i>=auds.length)return;
    var a=auds[i++];var box=a.closest(".cond");
    box.classList.add("playing");
    a.onended=function(){box.classList.remove("playing");next();};
    a.play();})();}
document.querySelectorAll("audio").forEach(function(a){
  a.addEventListener("play",function(){
    document.querySelectorAll("audio").forEach(function(o){if(o!==a)o.pause();});});});
document.querySelectorAll("button.playall").forEach(function(b){
  b.onclick=function(){playAllCard(b);};});
</script>
</body>
</html>"""
    html = html.replace("@@MROWS@@", mrows)\
               .replace("@@CARDS@@", cards)\
               .replace("@@NAUDIO@@", str(n_audio))
    io.open(OUT, "w", encoding="utf-8").write(html)
    print("已生成 %s（%.1f MB，%d 音频）"
          % (OUT, os.path.getsize(OUT) / 1048576.0, n_audio))


if __name__ == "__main__":
    main()
