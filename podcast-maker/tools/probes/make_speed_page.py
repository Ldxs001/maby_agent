# -*- coding: utf-8 -*-
"""把三路速度基准拼成单文件对比页。

    cuda_graph   faster-qwen3-tts · CUDA 图（产品现状）
    cuda_eager   qwen_tts 官方包 · GPU 前向（无图）
    cpu          qwen_tts 官方包 · 纯 CPU（float32）

    python tools/probes/make_speed_page.py
"""
from __future__ import annotations

import io
import json
import os

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
D = os.path.join(HERE, "_speed_probe")
OUT = os.path.join(D, "speed.html")

ARMS = [
    ("cuda_graph", "CUDA 图（现状）", "faster-qwen3-tts 0.4.0 · device=cuda · bf16"),
    ("cuda_eager", "官方包 · GPU 前向", "qwen_tts · device_map=cuda:0 · bf16 · 无图"),
    ("cpu", "官方包 · 纯 CPU", "qwen_tts · device_map=cpu · float32"),
]

TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三路推理路径速度对比</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--fg:#1f2328;--dim:#68707a;--hi:#b3392a;--ok:#1d7a4c;}
*{box-sizing:border-box}
body{margin:0;padding:22px;background:var(--bg);color:var(--fg);
font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:19px;margin:0 0 6px}
h2{font-size:16px;margin:0 0 10px}
.sub{color:var(--dim);margin:0 0 18px;max-width:980px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
th,td{border-bottom:1px solid var(--line);padding:7px 9px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--dim);font-weight:600}
.note{font-size:12px;color:var(--dim);margin-top:10px}
.fact{font-size:13px;margin:0;padding-left:18px}
.fact li{margin:4px 0}
.big{font-size:15px;font-weight:700}
.hi{color:var(--hi)}
.ok{color:var(--ok)}
code{background:#eef0f3;padding:1px 5px;border-radius:4px;font-size:12px}
.bar{height:16px;border-radius:3px;background:#b3392a;display:inline-block;vertical-align:middle}
.bar.c2{background:#8a8f96}
.bar.c3{background:#c9a227}
td.barcell{text-align:left}
</style>
</head>
<body>
<h1>三路推理路径速度对比</h1>
<p class="sub">同一份权重（Qwen3-TTS-12Hz-1.7B-CustomVoice）、同一句文本、同一音色（Serena）、
同一随机种子、同一组采样参数（<code>temperature 0.4 / top_k 50 / top_p 1.0 / rep 1.05</code>）。
<b>唯一变量是推理路径</b>。RTF = 生成耗时 ÷ 音频时长，越小越快，&lt;1 即快于实时。</p>

<div class="card">
  <h2>1 · 同一句（「我们先从原理讲起。」9 字，max_new_tokens=120）</h2>
  <table>
    <tr><th>推理路径</th><th>配置</th><th>加载 s</th><th>生成 s</th><th>音频 s</th>
    <th>RTF</th><th>相对现状</th><th>说明</th></tr>
    {MAIN}
  </table>
  <p class="note">「相对现状」= 该路生成耗时 ÷ CUDA 图耗时。数字越大越慢。</p>
</div>

<div class="card">
  <h2>2 · 各路径逐句明细</h2>
  <table>
    <tr><th>路径</th><th>句</th><th>字数</th><th>生成 s</th><th>音频 s</th><th>RTF</th></tr>
    {ROWS}
  </table>
  <p class="note">CPU 侧只跑了最短那句：它是三路里最慢的，跑满三句耗时过长且不改变结论。</p>
</div>

<div class="card">
  <h2>3 · 结论</h2>
  <ul class="fact">{FACTS}</ul>
</div>

</body>
</html>
"""


def load(path):
    if not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def steady_of(d):
    """取稳态记录（round>1）；没有 repeat 数据时退回首条。"""
    if not d or not d.get("rows"):
        return None
    steady = [r for r in d["rows"] if r.get("round", 1) > 1]
    pool = steady or d["rows"]
    return {
        "secs": sum(r["secs"] for r in pool) / len(pool),
        "audio_secs": sum(r["audio_secs"] for r in pool) / len(pool),
        "rtf": sum(r["rtf"] for r in pool) / len(pool),
        "n": len(pool),
        "is_steady": bool(steady),
    }


def main():
    got = {}
    rep = {}
    for key, _label, _cfg in ARMS:
        got[key] = load(os.path.join(D, "speed_%s.json" % key))
        rep[key] = load(os.path.join(D, "speed_%s_repeat.json" % key))

    rows_all = []
    main_rows = []
    ref = None
    for key, label, cfg in ARMS:
        # 主表：优先用同口径的稳态（repeat 文件），没有就退回首句
        st = steady_of(rep[key]) or steady_of(got[key])
        if st is None:
            main_rows.append("<tr><td>%s</td><td>%s</td><td colspan=6>缺数据</td></tr>"
                             % (label, cfg))
            continue
        if key == "cuda_graph":
            ref = st["secs"]
        # 多句明细：只并入「同口径重复文件里没有出现过的句子」
        seen = set(r["key"] for r in (rep[key] or {}).get("rows", []))
        extra = {}
        for rr in (got[key] or {}).get("rows", []):
            if rr["key"] in seen:
                continue
            extra[rr["key"]] = rr
        for rr in extra.values():
            rows_all.append(
                "<tr><td>%s</td><td>%s</td><td>%d</td><td>%.2f</td>"
                "<td>%.2f</td><td>%.2f</td></tr>"
                % (label, rr["key"], len(rr["text"]), rr["secs"],
                   rr["audio_secs"], rr["rtf"]))
        # 同口径重复：逐轮列出，首次（含冷启动）与稳态分开可见
        for rr in (rep[key] or {}).get("rows", []):
            rows_all.append(
                "<tr><td>%s</td><td>%s 第%d次</td><td>%d</td><td>%.2f</td>"
                "<td>%.2f</td><td>%.2f</td></tr>"
                % (label, rr["key"], rr.get("round", 1), len(rr["text"]),
                   rr["secs"], rr["audio_secs"], rr["rtf"]))
        main_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%.1f</td><td class=\"big\">%.2f</td>"
            "<td>%.2f</td><td class=\"big\">%.2f</td><td>%s</td><td>%s</td></tr>"
            % (label, cfg, (rep[key] or got[key])["load_secs"],
               st["secs"], st["audio_secs"], st["rtf"],
               "%.2f×" % (st["secs"] / ref) if ref else "—",
               "基准" if key == "cuda_graph" else "%.0f 次均值" % st["n"]))

    facts = []
    cg = steady_of(rep.get("cuda_graph")) or steady_of(got.get("cuda_graph"))
    ce = steady_of(rep.get("cuda_eager")) or steady_of(got.get("cuda_eager"))
    cp = steady_of(rep.get("cpu")) or steady_of(got.get("cpu"))
    if cg and ce:
        facts.append("<li>官方包走 GPU 前向（无图）比 CUDA 图慢 <span class=\"hi\">%.2f×</span>"
                     "（%.2fs → %.2fs）—— 这就是 CUDA 图本身带来的加速。</li>"
                     % (ce["secs"] / cg["secs"], cg["secs"], ce["secs"]))
    if cg and cp:
        facts.append("<li>官方包走 CPU 比 CUDA 图慢 <span class=\"hi\">%.1f×</span>"
                     "（%.2fs → %.2fs）。</li>" % (cp["secs"] / cg["secs"],
                                                   cg["secs"], cp["secs"]))
    if ce and cp:
        facts.append("<li>纯 CPU → 纯 GPU 的净提升只有 <span class=\"ok\">%.2f×</span>"
                     "（%.2fs → %.2fs）；官方宣传的「快 6–10 倍」指的是"
                     "「CUDA 图 vs 普通前向」，不是「GPU vs CPU」。</li>"
                     % (cp["secs"] / ce["secs"], ce["secs"], cp["secs"]))
    if cp and cg:
        facts.append("<li>CPU 路 RTF %.1f（生成 1 秒音频要算 %.1f 秒，跑不了实时）；"
                     "CUDA 图路 RTF %.2f（快于实时）。</li>"
                     % (cp["rtf"], cp["rtf"], cg["rtf"]))
    facts.append("<li>三路用的是同一份权重、同一句文本、同一组采样参数；"
                 "CUDA 图那路的控制面更窄（子码本采样参数被烧进图），是这条速度的代价。</li>")

    html = (TPL.replace("{MAIN}", "".join(main_rows))
               .replace("{ROWS}", "".join(rows_all))
               .replace("{FACTS}", "".join(facts)))
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("写入 %s（%.1f KB）" % (OUT, len(html.encode("utf-8")) / 1024.0))


if __name__ == "__main__":
    main()
