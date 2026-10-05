# -*- coding: utf-8 -*-
"""把 _listen/ 试听包打包成自包含 HTML 对比报告。

- 音频 base64 内嵌，单文件、零外部依赖（无 CDN、无相对路径引用）
- 同一句的四条（A/B/C/D）一屏并排，支持一键连播整组
- 深色顶栏 + 白色画布 + 浅灰网格；禁 emoji
用法：python tools/probes/build_listen_report.py
输出：_smoke/_listen_report.html
"""
import base64
import io
import json
import os
import re
import struct
import wave

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
SRC = os.path.join(HERE, "_listen")
OUT = os.path.join(HERE, "_listen_report.html")

TEXTS = {
    "01": "很多人第一眼看到书名就会问，这书真的是AI写的吗？",
    "02": "那判断的标准和取舍的意志，到底归谁呢？",
    "03": "所以书名是在提醒我们，力气活被终结后什么变得更贵了？",
}
CONDS = [
    ("A", "现状 · 无任何指令", "生产同款基线：ICL 参考音频是陈述句"),
    ("B", "最强措辞", "句型 + 整体音高 + 夸张，三维度组合指令"),
    ("C", "换参考音频", "用疑问句先验的 ref 生成（唯一在数据上脱离 0 的条件）"),
    ("D", "你的原话", "「这一句是疑问句，句尾语调上扬」"),
]


def wav_info(b):
    """读 wav 时长与采样率（只解析头部，不引库）。"""
    try:
        w = wave.open(io.BytesIO(b))
        return w.getnframes() / float(w.getframerate()), w.getframerate()
    except Exception:
        return 0.0, 0


def collect():
    """{组号: {条件字母: (b64, 时长, 采样率)}}"""
    out = {}
    for fn in sorted(os.listdir(SRC)):
        m = re.match(r"^(\d+)_([ABCD])", fn)
        if not m or not fn.lower().endswith(".wav"):
            continue
        grp, cond = m.group(1), m.group(2)
        raw = open(os.path.join(SRC, fn), "rb").read()
        dur, sr = wav_info(raw)
        out.setdefault(grp, {})[cond] = (
            base64.b64encode(raw).decode("ascii"), dur, sr)
    return out


def main():
    data = collect()
    groups = sorted(data)
    n_wav = sum(len(v) for v in data.values())
    total_mb = sum(
        len(v[0]) * 3 / 4 / 1048576.0
        for v in data.values() for v in v.values())

    rows_js = json.dumps(
        {g: sorted(data[g]) for g in groups}, ensure_ascii=False)

    cards = []
    for gi, g in enumerate(groups):
        auds = []
        for letter, label, note in CONDS:
            if letter not in data[g]:
                continue
            b64, dur, sr = data[g][letter]
            auds.append("""
      <div class="cond" id="{g}-{l}">
        <div class="cond-head">
          <span class="badge b{l}">{l}</span>
          <span class="cond-label">{label}</span>
          <span class="dur">{dur:.1f}s / {sr}Hz</span>
        </div>
        <div class="cond-note">{note}</div>
        <audio controls preload="none" src="data:audio/wav;base64,{b64}"></audio>
      </div>""".replace("{g}", g).replace("{l}", letter)
                .replace("{label}", label).replace("{note}", note)
                .replace("{dur:.1f}s / {sr}Hz", "%.1fs / %dHz" % (dur, sr))
                .replace("{b64}", b64))
        cards.append("""
    <section class="card" id="grp{g}">
      <div class="card-head">
        <div class="grp-tag">第 {gi} 组</div>
        <div class="grp-text">{text}</div>
        <button class="playall" onclick="playGroup('{g}')">连播 A-B-C-D</button>
        <button class="stopall" onclick="stopAll()">停</button>
      </div>
      <div class="conds">{auds}
      </div>
    </section>""".replace("{g}", g).replace("{gi}", str(gi + 1))
            .replace("{text}", TEXTS.get(g, ""))
            .replace("{auds}", "".join(auds)))

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>句型语调 A/B 试听报告</title>
<style>
  :root {
    --ink: #1c2330; --paper: #ffffff; --grid: #eef1f5;
    --muted: #6b7686; --line: #dde3ea; --accent: #3b6ef5;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    background: var(--grid); color: var(--ink); font-size: 15px;
  }
  header.top {
    background: #161d2b; color: #e8edf5; padding: 22px 28px;
  }
  header.top h1 { font-size: 19px; font-weight: 600; letter-spacing: .5px; }
  header.top .sub { margin-top: 7px; font-size: 13px; color: #9aa7bb; line-height: 1.7; }
  header.top .sub b { color: #cdd8ea; font-weight: 600; }
  main { max-width: 1080px; margin: 0 auto; padding: 22px 20px 60px; }
  .verdict {
    background: var(--paper); border: 1px solid var(--line); border-radius: 10px;
    padding: 16px 20px; margin-bottom: 20px; font-size: 13.5px; color: var(--muted);
    line-height: 1.9;
  }
  .verdict b { color: var(--ink); }
  .card {
    background: var(--paper); border: 1px solid var(--line); border-radius: 10px;
    margin-bottom: 20px; overflow: hidden;
  }
  .card-head {
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 14px 18px; border-bottom: 1px solid var(--line); background: #f7f9fc;
  }
  .grp-tag {
    background: var(--accent); color: #fff; border-radius: 6px;
    padding: 3px 10px; font-size: 12px; white-space: nowrap;
  }
  .grp-text { font-size: 15px; font-weight: 600; flex: 1; min-width: 260px; }
  button.playall, button.stopall {
    border: 1px solid var(--line); background: #fff; color: var(--ink);
    border-radius: 6px; padding: 5px 14px; font-size: 13px; cursor: pointer;
  }
  button.playall { border-color: var(--accent); color: var(--accent); }
  button.playall:hover { background: var(--accent); color: #fff; }
  .conds {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 1px; background: var(--line);
  }
  .cond { background: #fff; padding: 14px 16px 18px; }
  .cond.playing { background: #f2f7ff; }
  .cond-head { display: flex; align-items: center; gap: 8px; }
  .badge {
    width: 24px; height: 24px; border-radius: 50%; color: #fff;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 12.5px; font-weight: 700; flex: none;
  }
  .bA { background: #6b7686; } .bB { background: #b0681f; }
  .bC { background: #1f7a4d; } .bD { background: #7a3ba8; }
  .cond-label { font-weight: 600; font-size: 13.5px; }
  .dur { margin-left: auto; font-size: 11.5px; color: var(--muted); white-space: nowrap; }
  .cond-note { font-size: 12px; color: var(--muted); margin: 7px 0 10px; line-height: 1.6; }
  audio { width: 100%; height: 34px; }
  footer {
    max-width: 1080px; margin: 0 auto; padding: 0 20px 40px;
    font-size: 12px; color: var(--muted); line-height: 1.8;
  }
</style>
</head>
<body>
<header class="top">
  <h1>句型语调 A/B 试听报告</h1>
  <div class="sub">
    同一句疑问句文本 · 同一音色（Vivian, ICL）· 同种子，<b>唯一变量是语气处理方式</b>。<br>
    共 @@N_GROUPS@@ 组 × 4 条件，@@N_WAV@@ 个音频全部内嵌本页（约 @@TOTAL_MB@@ MB），离线可开、断网可听。
  </div>
</header>
<main>
  <div class="verdict">
    <b>测量结论（供对照，最终以耳朵为准）：</b>
    A 基线的疑问句句尾不上扬（现场疑问句整体音高比陈述句低 2 个半音）；
    B 与 D 的指令改动只有 0.5~0.7 半音，低于人耳约 1 半音的可辨阈；
    C（换参考音频）是唯一在测量上脱离 0 的条件，但音色会漂移。
    <b>重点听：</b>每条句尾有没有往上扬；C 相对 A 音色像不像原来的人。
  </div>
  @@CARDS@@
</main>
<footer>
  生成自 podcast-maker/_smoke/_listen/ · 12 wav base64 内嵌 · 同组连播按钮按 A-B-C-D 顺序播放，便于逐条件切换对比。<br>
  测量明细见 _smoke/_intonation_ab.json 与 _smoke/_intonation_rich.json。
</footer>
<script>
var GROUPS = @@ROWS@@;
var cur = null;
function stopAll() {
  document.querySelectorAll("audio").forEach(function (a) { a.pause(); });
  document.querySelectorAll(".cond").forEach(function (d) { d.classList.remove("playing"); });
  cur = null;
}
function playGroup(g) {
  stopAll();
  var seq = (GROUPS[g] || []).slice();
  (function next() {
    if (!seq.length) { return; }
    var letter = seq.shift();
    var box = document.getElementById(g + "-" + letter);
    var a = box && box.querySelector("audio");
    if (!a) { next(); return; }
    box.classList.add("playing");
    cur = a;
    a.onended = function () { box.classList.remove("playing"); next(); };
    a.play();
  })();
}
document.querySelectorAll("audio").forEach(function (a) {
  a.addEventListener("play", function () {
    document.querySelectorAll("audio").forEach(function (o) { if (o !== a) { o.pause(); } });
  });
});
</script>
</body>
</html>"""
    html = (html.replace("@@N_GROUPS@@", str(len(groups)))
                .replace("@@N_WAV@@", str(n_wav))
                .replace("@@TOTAL_MB@@", "%.1f" % total_mb)
                .replace("@@CARDS@@", "".join(cards))
                .replace("@@ROWS@@", rows_js))

    io.open(OUT, "w", encoding="utf-8").write(html)
    print("已生成 %s（%.1f MB，%d 组 %d 音频）"
          % (OUT, os.path.getsize(OUT) / 1048576.0, len(groups), n_wav))


if __name__ == "__main__":
    main()
