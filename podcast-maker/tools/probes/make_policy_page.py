# -*- coding: utf-8 -*-
"""对照页：① 不同文字 + 同一个固定种子  ② 采样策略（关子码本 / 全贪心）

自包含 HTML，音频以 base64 内联，无外部 CDN，禁 emoji。
"""
import base64
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEED_JSON = os.path.join(ROOT, "_smoke", "_seed_probe", "policy.json")
POL_JSON = os.path.join(ROOT, "_smoke", "_policy2_probe", "policy2.json")
SEED_DIR = os.path.join(ROOT, "_smoke", "_seed_probe")
POL_DIR = os.path.join(ROOT, "_smoke", "_policy2_probe")
TMP = os.path.join(ROOT, "_smoke", "_policy_report", "_mp3")
OUT_HTML = os.path.join(ROOT, "_smoke", "_policy_report", "compare.html")

# 顺序：把用户实际听过的那两句排最前
PICKS = [3, 19, 7, 11, 23, 29]

SEED_COLS = [("P1_现状_按文本派生", "P1 现状（每句各自种子）"),
             ("P2_固定_12345", "P2 固定种子 12345"),
             ("P3_固定_0", "P3 固定种子 0")]
POL_COLS = [("Q1_现状_两处都采样", "Q1 现状（两处都采样）"),
            ("Q2_只关子码本", "Q2 只关子码本采样"),
            ("Q3_全贪心", "Q3 全贪心（两处都不采样）")]


def to_mp3(src, tag):
    os.makedirs(TMP, exist_ok=True)
    dst = os.path.join(TMP, tag + ".mp3")
    subprocess.run(["ffmpeg", "-y", "-i", src, "-b:a", "32k", "-ac", "1", dst],
                   capture_output=True)
    return dst


def b64(path):
    with open(path, "rb") as f:
        return "data:audio/mpeg;base64," + base64.b64encode(f.read()).decode()


def audio_row(label, src, note=""):
    mp3 = to_mp3(src, os.path.basename(src).replace(".wav", ""))
    uri = b64(mp3)
    return ('<div class="arow"><span class="alab">%s</span>'
            '<audio controls preload="none" src="%s"></audio>'
            '<span class="anote">%s</span></div>' % (label, uri, note))


def main():
    sd = json.load(io.open(SEED_JSON, encoding="utf-8"))
    pd = json.load(io.open(POL_JSON, encoding="utf-8"))
    srows = {(r["policy"], r["idx"]): r for r in sd["rows"]}
    prows = {(r["cond"], r["idx"]): r for r in pd["rows"]}

    p = []
    p.append('<div class="wrap">')
    p.append('<h1>同一个音色，为什么听起来像两个人</h1>')
    p.append('<p class="sub">本期真实句子 / 音色 Serena（B）/ 情绪标签「解释」/ 同一台机器同一把尺子。'
             '素材：<code>projects/20260915-103249/脚本/1.json</code></p>')

    # ================= 块一：固定种子 =================
    p.append('<h2>一、不同文字 + 同一个固定种子（你问的这个）</h2>')
    p.append('<p class="lead">6 句<b>不同文本</b>，三种种子策略。P2/P3 就是「所有句子共用同一个随机起点」。</p>')

    p.append('<table><thead><tr><th>句</th><th>字数</th>')
    for _, lab in SEED_COLS:
        p.append('<th>%s</th>' % lab)
    p.append('</tr></thead><tbody>')
    for idx in PICKS:
        ch = srows[(SEED_COLS[0][0], idx)]["chars"]
        p.append('<tr><td class="mono">%04d</td><td>%d</td>' % (idx, ch))
        for key, _ in SEED_COLS:
            r = srows[(key, idx)]
            p.append('<td class="mono">F0 %.0f<br><span class="dim">%.2fs</span></td>'
                     % (r["f0_med"], r["dur"]))
        p.append('</tr>')
    p.append('</tbody></table>')

    p.append('<h3>离散度：越小 = 这一组句子越像同一个人</h3>')
    p.append('<table><thead><tr><th>条件</th><th>F0 标准差</th><th>F0 极差</th>'
             '<th>F1 标准差</th><th>F2 标准差</th></tr></thead><tbody>')
    base = sd["summary"][SEED_COLS[0][0]]
    for key, lab in SEED_COLS:
        s = sd["summary"][key]
        cls = ""
        if key != SEED_COLS[0][0]:
            r_f0 = base["f0_med"] / s["f0_med"]
            cls = "win" if r_f0 > 1.1 else "lose"
        p.append('<tr class="%s"><td>%s</td><td class="mono">%.2f</td>'
                 '<td class="mono">%.2f Hz</td><td class="mono">%.1f</td>'
                 '<td class="mono">%.1f</td></tr>'
                 % (cls, lab, s["f0_med"], s["f0_span"], s["f1"], s["f2"]))
    p.append('</tbody></table>')

    p.append('<div class="note"><b>读法</b>：固定种子把 <b>F0 极差从 42.8Hz 收到 26～29Hz（改善约 1.5 倍）</b>，'
             '这条是实的。但 F1/F2（音色指纹）没有跟着收敛——<b>固定 12345 让它变差，固定 0 让它略好</b>，'
             '换个值结论就反转。说明种子能锚住音高，锚不住音色。</div>')

    p.append('<h3>逐句试听</h3>')
    for idx in PICKS:
        txt = srows[(SEED_COLS[0][0], idx)]["text"]
        p.append('<div class="card"><div class="ct">%04d ・ %s</div>' % (idx, txt))
        for key, lab in SEED_COLS:
            r = srows[(key, idx)]
            src = os.path.join(SEED_DIR, r["file"])
            p.append(audio_row(lab, src, "F0 %.0f ・ %.2fs" % (r["f0_med"], r["dur"])))
        p.append('</div>')

    # ================= 块二：采样策略 =================
    p.append('<h2>二、顺手挖到的：比固定种子强得多的旋钮</h2>')
    p.append('<p class="lead">固定种子只把 F0 收了 1.5 倍。那如果<b>干脆把随机性掐掉</b>呢？'
             '同样是 6 句不同文本。Q2/Q3 的种子<b>照旧按文本派生</b>（每句不同），'
             '动的只是采样开关——所以这一块跟种子策略无关。</p>')

    p.append('<table><thead><tr><th>句</th><th>字数</th>')
    for _, lab in POL_COLS:
        p.append('<th>%s</th>' % lab)
    p.append('</tr></thead><tbody>')
    for idx in PICKS:
        ch = prows[(POL_COLS[0][0], idx)]["chars"]
        p.append('<tr><td class="mono">%04d</td><td>%d</td>' % (idx, ch))
        for key, _ in POL_COLS:
            r = prows[(key, idx)]
            flag = ' <span class="bad">拖沓</span>' if r["dur"] > 9 else ""
            p.append('<td class="mono">F0 %.0f<br><span class="dim">%.2fs</span>%s</td>'
                     % (r["f0_med"], r["dur"], flag))
        p.append('</tr>')
    p.append('</tbody></table>')

    p.append('<h3>离散度对照</h3>')
    p.append('<table><thead><tr><th>条件</th><th>F0 标准差</th><th>F0 极差</th>'
             '<th>F1 标准差</th><th>F2 标准差</th><th>时长效</th>'
             '<th>相对现状</th></tr></thead><tbody>')
    qbase = pd["summary"][POL_COLS[0][0]]
    for key, lab in POL_COLS:
        s = pd["summary"][key]
        if key == POL_COLS[0][0]:
            gain = "—"
        else:
            imp = pd["improve"][key]
            gain = "F0 <b>%.2f×</b> ・ 时长 <b>%.2f×</b>" % (imp["F0"], imp["时长"])
        p.append('<tr class="%s"><td>%s</td><td class="mono">%.1f</td>'
                 '<td class="mono">%.1f Hz</td><td class="mono">%.1f</td>'
                 '<td class="mono">%.1f</td><td class="mono">%.2f</td>'
                 '<td>%s</td></tr>'
                 % ("win" if key == POL_COLS[1][0] else "", lab,
                    s["f0_med_sd"], s["f0_span"], s["f1_sd"], s["f2_sd"],
                    s["dur_sd"], gain))
    p.append('</tbody></table>')

    g = pd["gap_3_19"]
    p.append('<h3>你听过的那两句（0003 vs 0019），音高差缩到多少</h3>')
    p.append('<table><thead><tr><th>条件</th><th>ΔF0</th><th>ΔF1</th><th>ΔF2</th>'
             '<th>Δ时长</th></tr></thead><tbody>')
    for key, lab in POL_COLS:
        d = g[key]
        p.append('<tr class="%s"><td>%s</td><td class="mono">%.1f Hz</td>'
                 '<td class="mono">%.1f</td><td class="mono">%.1f</td>'
                 '<td class="mono">%.2fs</td></tr>'
                 % ("win" if key == POL_COLS[1][0] else "", lab,
                    d["d_f0"], d["d_f1"], d["d_f2"], d["d_dur"]))
    p.append('</tbody></table>')

    p.append('<div class="note"><b>结论</b>：关掉子码本采样后，'
             '<b>F0 极差 103.7Hz → 17.3Hz（改善 5.2 倍）</b>，'
             '<b>时长稳定性改善 2.4 倍</b>，你那两句的音高差 <b>34.0Hz → 5.5Hz</b>。'
             '全贪心把 F1 收得最好（1.75 倍）却把时长搞崩（0007 拖到 17.28s），不能用。</div>')

    p.append('<div class="note warn"><b>必须说清的两条边界</b>：'
             '① 这一块 6 句的「现状」离散度（F0 极差 103.7Hz）比上一批同条件的 42.8Hz 大一倍多，'
             '说明 <b>6 句样本太小，离散度指标本身不稳</b>——所以这里是「同一批内三条件横向对比」有意义，'
             '绝对值别当常数。② 关子码本会让相邻 token 重复次数上升（上轮实测：现状 2.3 次/句 → 6.2 次/句），'
             '<b>听感上是否可接受只能你来判断</b>，这也是本页存在的意义。</div>')

    p.append('<h3>逐句试听</h3>')
    for idx in PICKS:
        txt = prows[(POL_COLS[0][0], idx)]["text"]
        p.append('<div class="card"><div class="ct">%04d ・ %s</div>' % (idx, txt))
        for key, lab in POL_COLS:
            r = prows[(key, idx)]
            src = os.path.join(POL_DIR, r["file"])
            note = "F0 %.0f ・ %.2fs" % (r["f0_med"], r["dur"])
            if r["dur"] > 9:
                note += ' ・ 明显拖长'
            p.append(audio_row(lab, src, note))
        p.append('</div>')

    rc = pd.get("recheck_q3", [])
    if rc:
        p.append('<div class="note"><b>复验</b>：全贪心下同一句换 3 个种子（%s），'
                 '三条波形 sha16 全部相同 → <b>随机性确实被完全掐掉了</b>。</div>'
                 % "、".join(str(x["seed"]) for x in rc))

    p.append('<h2>三、怎么理解这两块的关系</h2>')
    p.append('<ul class="cli">'
             '<li><b>固定种子</b>：只改「起点抽签的随机数」。句子越往后走，轨迹各走各的，'
             '所以只能锚住起音的音高（改善 1.5 倍），锚不住整句音色。</li>'
             '<li><b>关子码本采样</b>：子码本那 15 层码本负责声学细节（含音高轮廓）。'
             '把它从「每层各自抽签」改成「取最大值」，细节层不再抖动，句间一致性大幅提升。</li>'
             '<li><b>去掉全部随机性</b>：连主路都不抽签，变成纯确定性——一致性最好，'
             '但模型会掉进重复循环（0007 拖到 17.28 秒），播客不能用。</li>'
             '<li><b>三类都解决不了的</b>：共振峰（F1/F2）代表的音色本体。'
             '它在文本不同时必然漂——那是 1.7B 模型「谁在说」与「说什么」没解耦，'
             '只有换带 speaker embedding 的模型才治本。</li>'
             '</ul>')

    p.append('</div>')

    css = """
:root{--bg:#14161a;--panel:#1b1f26;--line:#2b313b;--fg:#e8eaed;--dim:#98a1ae;
--acc:#e5484d;--ok:#e5484d;--warn:#d9a441;--card:#1f242c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
font-size:14px;line-height:1.65}
.wrap{max-width:1080px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:23px;margin:0 0 6px;letter-spacing:.3px}
.sub{color:var(--dim);font-size:12.5px;margin:0 0 26px}
.sub code{background:var(--panel);padding:1px 5px;border-radius:3px;font-size:12px}
h2{font-size:17px;margin:34px 0 8px;padding-left:10px;border-left:3px solid var(--acc)}
h3{font-size:14.5px;margin:22px 0 8px;color:#cfd5dd;font-weight:600}
.lead{color:#c3cad4;margin:6px 0 16px}
table{width:100%;border-collapse:collapse;margin:10px 0 18px;background:var(--panel);
border:1px solid var(--line);border-radius:6px;overflow:hidden}
th{text-align:left;padding:9px 11px;background:#232936;font-weight:600;
font-size:12.5px;color:#dfe4ea;border-bottom:1px solid var(--line)}
td{padding:9px 11px;border-bottom:1px solid #232830;font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr.win td{background:rgba(229,72,77,.09)}
tr.win td:first-child{box-shadow:inset 3px 0 0 var(--acc)}
tr.lose td{background:rgba(70,167,88,.07)}
.mono{font-family:Consolas,"Cascadia Mono",monospace;font-size:12.5px}
.dim{color:var(--dim);font-size:11.5px}
.bad{color:var(--warn);font-size:11px}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid #556;
border-radius:5px;padding:12px 14px;margin:14px 0;font-size:13px;color:#cbd2db}
.note.warn{border-left-color:var(--warn)}
.note b{color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:12px 14px;margin:12px 0}
.ct{font-size:12.5px;color:#b9c1cb;margin-bottom:9px;padding-bottom:7px;
border-bottom:1px dashed #2c323c}
.arow{display:flex;align-items:center;gap:12px;margin:5px 0}
.alab{flex:0 0 190px;font-size:12.5px;color:#ccd3dc}
.anote{flex:0 0 170px;font-size:11.5px;color:var(--dim);font-family:Consolas,monospace}
audio{height:30px;flex:1 1 auto;min-width:180px}
ul.cli{margin:10px 0 0;padding-left:18px}
ul.cli li{margin:8px 0;color:#c8cfd8}
ul.cli b{color:#fff}
@media(max-width:720px){.arow{flex-wrap:wrap}.alab{flex:1 1 100%}
.anote{flex:1 1 100%}audio{flex:1 1 100%}}
"""
    html = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>音色一致性对照</title><style>%s</style></head><body>%s</body></html>"
            % (css, "\n".join(p)))

    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with io.open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("页面: %s" % OUT_HTML)
    print("大小: %.2f MB" % (os.path.getsize(OUT_HTML) / 1048576.0))
    print("内联音频段数: %d" % html.count("data:audio/mpeg;base64"))
    print("外部引用: %s" % ("有" if ("cdn" in html or "http://" in html.replace("http://www.w3.org", "")) else "无"))
    print("emoji: %s" % ("有" if any(ord(c) > 0x1F000 for c in html) else "无"))


if __name__ == "__main__":
    main()
