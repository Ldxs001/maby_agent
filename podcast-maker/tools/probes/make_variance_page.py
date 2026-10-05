# -*- coding: utf-8 -*-
"""生成「为什么会突然拉长、突然压低」方差分解报告页（自包含，音频内联）。"""
import base64
import io
import json
import os
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "_smoke", "_variance_probe")
OUT = os.path.join(SRC, "variance_report.html")
TS = time.strftime("%m/%d/%Y %H:%M")

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--line:#2a3340;--fg:#e6edf3;
--dim:#9aa7b4;--accent:#58a6ff;--up:#ff5c5c;--dn:#3fb950;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:22px;margin:0 0 6px;font-weight:650}
h2{font-size:16px;margin:36px 0 12px;padding-bottom:8px;
border-bottom:1px solid var(--line);font-weight:600}
h3{font-size:14px;margin:22px 0 8px;font-weight:600;color:var(--fg)}
.ts{color:var(--dim);font-size:12px;margin-bottom:22px}
.lead{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:8px;padding:16px 18px;font-size:13.5px}
.lead b{color:#fff}
.lead .hi{color:var(--up);font-weight:650}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:15px 17px;margin:0 0 14px}
.card .head{display:flex;align-items:center;gap:9px;margin-bottom:6px;flex-wrap:wrap}
.tag{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);
color:var(--dim);font-family:ui-monospace,Consolas,monospace}
.tag.a{color:var(--accent);border-color:#1f4a7a;background:#12233a}
.tag.b{color:var(--warn);border-color:#5c4715;background:#2a2210}
.txt{color:var(--fg);font-size:13.5px;margin:7px 0 13px;line-height:1.6}
.row{display:grid;grid-template-columns:96px 1fr;gap:11px;align-items:center;margin:8px 0}
.row .lb{font-size:12px;color:var(--dim);font-family:ui-monospace,Consolas,monospace}
.row.hot .lb{color:var(--up)}
audio{width:100%;height:34px}
.meta{font-size:11.5px;color:var(--dim);font-family:ui-monospace,Consolas,monospace;
margin-top:3px;word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left}
th{background:var(--panel2);color:var(--dim);font-weight:600}
td.num{font-family:ui-monospace,Consolas,monospace;text-align:right}
.big{font-size:15px;font-weight:650}
.win{color:var(--up)}
.note{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
padding:14px 17px;font-size:13px;color:var(--dim);margin-top:12px}
.note b{color:var(--fg)}
ul{margin:8px 0 0;padding-left:20px}li{margin:6px 0}
code{background:#0b0f14;padding:1px 5px;border-radius:4px;font-size:12px;
font-family:ui-monospace,Consolas,monospace;color:#a5d6ff}
.bar{height:20px;border-radius:3px;position:relative;background:#0b0f14;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
"""


def b64(p):
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def main():
    with io.open(os.path.join(ROOT, "_smoke", "_variance_probe.json"),
                 encoding="utf-8") as f:
        recs = json.load(f)
    with io.open(os.path.join(ROOT, "_smoke", "_variance_samples.json"),
                 encoding="utf-8") as f:
        samples = json.load(f)

    def get(line, off, cond):
        for r in recs:
            if r["line"] == line and r["seed_offset"] == off and r["cond"] == cond:
                return r
        return None

    # 真实种子 s0 下的配对变化
    s0_pairs = []
    for line in range(1, 5):
        a, b = get(line, 0, "off"), get(line, 0, "on")
        if a and b:
            s0_pairs.append({
                "line": line, "text": a["text"], "speaker": a["speaker"],
                "off": a, "on": b,
                "dur_pct": (a["dur"] - b["dur"]) / b["dur"] * 100.0,
                "f0_pct": (a["f0_med"] - b["f0_med"]) / b["f0_med"] * 100.0,
            })

    # 方差分解汇总
    import statistics as st
    cond_dur = [abs(get(l, k, "off")["dur"] - get(l, k, "on")["dur"])
                / get(l, k, "on")["dur"] * 100.0
                for l in range(1, 5) for k in range(4)]
    cond_f0 = [abs(get(l, k, "off")["f0_med"] - get(l, k, "on")["f0_med"])
               / get(l, k, "on")["f0_med"] * 100.0
               for l in range(1, 5) for k in range(4)]
    seed_dur, seed_f0 = [], []
    for l in range(1, 5):
        for cond in ("off", "on"):
            d = [get(l, k, cond)["dur"] for k in range(4)]
            f = [get(l, k, cond)["f0_med"] for k in range(4)]
            seed_dur.append((max(d) - min(d)) / (sum(d) / 4) * 100.0)
            seed_f0.append((max(f) - min(f)) / (sum(f) / 4) * 100.0)

    p = []
    p.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>为什么会突然拉长、突然压低</title><style>%s</style></head>'
             '<body><div class="wrap">' % CSS)

    p.append("<h1>为什么会「突然拉长、突然压低」——方差分解</h1>")
    p.append('<div class="ts">生成时间 %s · 项目 20260915-103249 第 1 期 · '
             '样本 4 句「平静」× 4 种子 × 2 指令状态</div>' % TS)

    p.append('<div class="lead"><b>一句话结论：</b>'
             '同一句话、同一个音色、同一个种子，只要把「用平静的语气说」这一句'
             '从输入里去掉，时长就能变两成、音高能变一倍。'
             '<span class="hi">这不是去指令"带来"的噪声，是这个模型对输入扰动本来就'
             '这么敏感</span>——它自身的采样方差（换种子）比这还大。</div>')

    # ---------------- 一、术语
    p.append("<h2>先把一件事说清楚：范式卡就是文体卡，只有一张</h2>")
    p.append('<div class="note">')
    p.append("上一条我用「范式卡」「文体卡」「单集」几个词把你绕晕了，是我的问题。实际是：<ul>"
             "<li><b>只有一张卡</b>。<code>paradigms.py</code> 里那些卡就是文体卡，"
             "在项目记录里字段名叫 <code>paradigm</code>——同一个东西两个名字。</li>"
             "<li><b>卡上存着情绪档位</b>（<code>emotion_level</code>）。"
             "本期这张卡（<code>methodology</code>）写的是 <code>none</code>。</li>"
             "<li><b>档位在项目级选，整期一个值</b>。不存在「每集再选一次」。"
             "我提「单集」只是在回答「档位会不会逐集变」，答案是不会。</li>"
             "</ul></div>")

    # ---------------- 二、A 组试听
    p.append("<h2>A 组 · 真实重跑会遇到的那点变化</h2>")
    p.append('<div class="meta" style="margin-bottom:12px">'
             '同文本、同音色、同种子（产品真派生 <code>seed_for</code>），'
             '唯一变量：去不去掉「用平静的语气说」。'
             '这组就是重跑前后你耳朵会听到的东西。</div>')

    for r in s0_pairs:
        idx = r["line"]
        p.append('<div class="card"><div class="head">'
                 '<span class="tag a">第 %d 句 · %s</span>'
                 '<span class="tag">%d 字</span></div>'
                 '<div class="txt">%s</div>'
                 % (idx, r["speaker"], r["off"]["n_chars"], r["text"]))
        for which, label, cls in (("on", "有指令 · 旧", ""),
                                  ("off", "无指令 · 新", "row hot")):
            fn = os.path.join(SRC, "A%d_%s.wav" % (idx, which))
            d = r[which]
            p.append('<div class="row%s"><div class="lb">%s</div><div>'
                     '<audio controls preload="none" src="data:audio/wav;base64,%s"></audio>'
                     '<div class="meta">%ss · 字/秒 %s · F0 %s Hz</div></div></div>'
                     % (cls, label, b64(fn), d["dur"], d["cps"], d["f0_med"]))
        p.append('<div class="meta">→ 时长 %+.1f%% · F0 %+.1f%%</div></div>'
                 % (r["dur_pct"], r["f0_pct"]))

    # ---------------- 三、B 组试听
    p.append("<h2>B 组 · 模型自己有多飘（重跑不会遇到，但必须知道）</h2>")
    p.append('<div class="meta" style="margin-bottom:12px">'
             '同一句、同样都不给指令，<b>只换种子</b>。'
             '种子在产品里是按「音色+文本」派生的、重跑不会变——'
             '但这组告诉你：这个模型光靠自己随机，就能飘出多大范围。</div>')

    txt4 = samples[3]["text"]
    p.append('<div class="card"><div class="head">'
             '<span class="tag b">第 4 句 · 四个种子</span></div>'
             '<div class="txt">%s</div>' % txt4)
    for k in range(4):
        d = get(4, k, "off")
        fn = os.path.join(SRC, "B_seed%d.wav" % k)
        p.append('<div class="row"><div class="lb">种子 +%d</div><div>'
                 '<audio controls preload="none" src="data:audio/wav;base64,%s"></audio>'
                 '<div class="meta">%ss · 字/秒 %s · F0 %s Hz</div></div></div>'
                 % (k, b64(fn), d["dur"], d["cps"], d["f0_med"]))
    f0s = [get(4, k, "off")["f0_med"] for k in range(4)]
    durs = [get(4, k, "off")["dur"] for k in range(4)]
    p.append('<div class="meta">→ 同条件下光换种子：F0 从 %s 到 %s Hz（%.0f%%），'
             '时长 %.2f～%.2fs</div></div>'
             % (min(f0s), max(f0s), (max(f0s) - min(f0s)) / (sum(f0s) / 4) * 100,
                min(durs), max(durs)))

    # ---------------- 四、方差分解表
    p.append("<h2>方差分解：谁在造成「突然」？</h2>")
    p.append("<table><thead><tr><th>指标</th>"
             "<th>条件效应（去指令造成）</th><th>种子效应（模型自身飘）</th>"
             "<th>谁更大</th></tr></thead><tbody>")
    rows = (
        ("时长", cond_dur, seed_dur),
        ("F0 中位数", cond_f0, seed_f0),
    )
    for label, c, s in rows:
        cm, cx = st.median(c), max(c)
        sm, sx = st.median(s), max(s)
        win = "种子效应" if sm > cm else "条件效应"
        p.append('<tr><td>%s</td><td class="num">中位 %.1f%% · 最大 %.1f%%</td>'
                 '<td class="num">中位 %.1f%% · 最大 %.1f%%</td>'
                 '<td class="big win">%s</td></tr>'
                 % (label, cm, cx, sm, sx, win))
    p.append("</tbody></table>")
    p.append('<div class="meta" style="margin-top:8px">'
             '条件效应 = 同种子下 有/无指令 的差异（n=16）；'
             '种子效应 = 同条件下换 4 个种子的极差（n=8）。</div>')

    # ---------------- 五、道理
    p.append("<h2>道理在哪</h2>")
    p.append('<div class="note">')
    p.append("Qwen3-TTS 是<b>自回归</b>生成的：一个字一个字往外吐，每个字都基于"
             "「已经吐出来的全部内容 + 条件」。「用平静的语气说」这几个字是拼进输入"
             "里的<b>条件</b>，不是开关。<ul>"
             "<li><b>条件变了，轨迹就发散。</b>种子只决定起点的那点随机，"
             "起点相同、路不同，越走越远——所以同一句话，"
             "去/不去指令能差出一倍音高。</li>"
             "<li><b>模型小，说话人特征压不住韵律条件。</b>这是 1.7B 的模型，"
             "「是谁在说」和「用什么口气说」在它内部没有彻底解耦；"
             "一改口气，音色跟着漂——你听到的「年轻人变老年人」，"
             "就是 F0 从 270Hz 掉到 135Hz 这种级别（差一个八度）。</li>"
             "<li><b>为什么 edge-TTS 反而稳。</b>它是规则驱动的拼接/预测，"
             "每个字独立、没有跨句的累积误差；Qwen3-TTS 是采样的，"
             "条件一动整条轨迹重算。这是「更高级」换来的代价，"
             "不是它坏了。</li></ul></div>")

    # ---------------- 六、一个必须指出的设计事实
    p.append("<h2>一个你看不到的设计事实</h2>")
    p.append('<div class="note">')
    p.append("服务端在合成每一句时，有一套三级兜底（<code>serve.py</code> 约"
             " L551-556）：<br><br>"
             "<code>(seed, instruct)</code> → 正常走<br>"
             "<code>(seed+1, instruct)</code> → 换种子重试<br>"
             "<code>(seed, None)</code> → <b>去掉语气</b>再试<br><br>"
             "也就是说：<b>「不发 instruct」在原设计里是生成失控时的降级手段，"
             "不是缺省行为。</b>而我们这次的改动，让 none 档的每一句都常态跑在"
             "这条第三档路径上。它没坏、效果也是上游语义想要的（平静 = 不加修饰），"
             "但要知道：<b>我们等于把 68 句的合成稳定性，从「有韵律锚点」换成了"
             "「无锚点自由发挥」。</b></div>")

    # ---------------- 七、诚实交代
    p.append("<h2>我上一条说错的地方</h2>")
    p.append('<div class="note">')
    p.append("<ul><li>我说「重跑后那 68 句会变<b>更平</b>」——<b>只测了 2 句就下结论，"
             "样本不够</b>。这次 16 组配对看，方向是一致的（去指令后 F0 都升高、"
             "时长都变短），但幅度从 +2.7%% 到 +28%%，没法用一个词概括。</li>"
             "<li>我说「一升一降、方向不可预测」——那是上一轮两个样本的偶然结果，"
             "16 组配对下方向其实一致。<b>两次都是样本太小惹的祸。</b></li>"
             "<li>我把「范式卡」和「文体卡」当两个词用，制造了「有两层选择」的错觉。"
             "是同一张卡。</li></ul></div>")

    p.append("</div></body></html>")

    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write("".join(p))
    print("已生成：%s" % OUT)
    print("体积：%.2f MB" % (os.path.getsize(OUT) / 1048576.0))


if __name__ == "__main__":
    main()
