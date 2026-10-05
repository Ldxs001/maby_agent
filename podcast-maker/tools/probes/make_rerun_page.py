# -*- coding: utf-8 -*-
"""生成「重跑前后对照」试听页（自包含，音频 base64 内联）。

数据来自 _smoke/_rerun_probe.json 与 _smoke/_rerun_probe/ 下的 wav。
"""
import base64
import io
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "_smoke", "_rerun_probe")
OUT = os.path.join(SRC, "rerun_effect.html")

TS = time.strftime("%m/%d/%Y %H:%M")

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2330;--line:#2a3340;--fg:#e6edf3;
--dim:#9aa7b4;--accent:#58a6ff;--up:#ff5c5c;--dn:#3fb950;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 6px;font-weight:650}
h2{font-size:16px;margin:34px 0 12px;padding-bottom:8px;
border-bottom:1px solid var(--line);font-weight:600}
.ts{color:var(--dim);font-size:12px;margin-bottom:22px}
.verdict{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:8px;padding:16px 18px;margin:0 0 18px}
.verdict b{color:#fff}
.verdict .yes{color:var(--dn);font-weight:650}
.verdict .no{color:var(--warn);font-weight:650}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin-top:10px;font-size:13px}
.kv dt{color:var(--dim)}
.kv dd{margin:0;font-family:ui-monospace,Consolas,monospace}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:14px 16px;margin:0 0 14px}
.card .head{display:flex;align-items:center;gap:9px;margin-bottom:4px;flex-wrap:wrap}
.tag{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);
color:var(--dim);font-family:ui-monospace,Consolas,monospace}
.tag.real{color:var(--up);border-color:#6e2b2b;background:#2a1518}
.tag.disc{color:var(--accent);border-color:#1f4a7a;background:#12233a}
.txt{color:var(--fg);font-size:13.5px;margin:6px 0 12px;line-height:1.6}
.row{display:grid;grid-template-columns:58px 1fr;gap:10px;align-items:center;margin:7px 0}
.row .lb{font-size:12px;color:var(--dim);font-family:ui-monospace,Consolas,monospace}
.row.new .lb{color:var(--accent)}
audio{width:100%;height:34px}
.meta{font-size:11.5px;color:var(--dim);font-family:ui-monospace,Consolas,monospace;
margin-top:3px;word-break:break-all}
.same{color:var(--dn)}.diff{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left}
th{background:var(--panel2);color:var(--dim);font-weight:600}
td.num{font-family:ui-monospace,Consolas,monospace;text-align:right}
.ok{color:var(--dn)}.chg{color:var(--up)}
ul{margin:8px 0 0;padding-left:20px}li{margin:5px 0}
.note{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:13px 16px;
font-size:13px;color:var(--dim);margin-top:10px}
.note b{color:var(--fg)}
"""


def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def main():
    with io.open(os.path.join(ROOT, "_smoke", "_rerun_probe.json"),
                 encoding="utf-8") as f:
        rows = json.load(f)

    parts = []
    parts.append(
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>重跑前后对照</title><style>%s</style></head><body><div class="wrap">' % CSS)

    parts.append("<h1>重跑前后对照 · 同一句的真实差异</h1>")
    parts.append('<div class="ts">生成时间 %s · 项目 20260915-103249「我思故我写」第 1 期</div>' % TS)

    n_real = sum(1 for r in rows if not r["same"])
    n_same = sum(1 for r in rows if r["same"])

    parts.append(
        '<div class="verdict"><b>结论：重跑后档位一定是 <code>none</code>（不发语气指令）</b>'
        '<div class="kv">'
        '<dt>档位来源</dt><dd>项目范式卡 paradigm=methodology → emotion_level="none"</dd>'
        '<dt>但只有</dt><dd class="no">68 句真情绪（「平静」）的音频会变</dd>'
        '<dt>另外</dt><dd class="yes">234 句语篇标签重跑后逐字节相同</dd>'
        '</div></div>')

    parts.append("<h2>试听对照（同一句、同一音色、同一种子，只改档位）</h2>")

    for r in rows:
        kind = "real" if r["emotion"] in ("平静", "好奇", "疑惑", "恍然", "感慨", "肯定", "轻松") else "disc"
        kind_cn = "真情绪" if kind == "real" else "语篇标签"
        head = ('<div class="head"><span class="tag %s">%s</span>'
                '<span class="tag">speaker %s · voice %s</span>'
                '<span class="tag">emotion %s</span>'
                '<span class="tag">%s</span></div>'
                % (kind, kind_cn, r["speaker"], r["voice"], r["emotion"],
                   "新旧不同" if not r["same"] else "新旧逐字节相同"))
        parts.append('<div class="card">%s<div class="txt">%s</div>' % (head, r["text"]))

        for which, label in (("old", "旧 · 发指令"), ("new", "新 · none")):
            path = os.path.join(SRC, "%s_%s_%s.wav"
                                % (r["label"].replace("-", "_"), which, r["speaker"]))
            d = r[which]
            cls = "row new" if which == "new" else "row"
            parts.append(
                '<div class="%s"><div class="lb">%s</div><div>'
                '<audio controls preload="none" src="data:audio/wav;base64,%s"></audio>'
                '<div class="meta">时长 %ss · %d 字节 · sha %s · F0 %s Hz</div>'
                '</div></div>'
                % (cls, label, b64(path), d["dur"], d["bytes"], d["sha"],
                   ("%.1f" % d["f0"]) if d["f0"] == d["f0"] else "—"))

        if r["same"]:
            verdict = '<div class="meta same">→ 两条参数完全相同，波形逐字节一致（SHA 相同）</div>'
        else:
            arrow = "▲" if r["f0_delta_hz"] > 0 else "▼"
            verdict = ('<div class="meta diff">→ 波形不同：F0 %s%.1f Hz（%+.1f%%）</div>'
                       % (arrow, r["f0_delta_hz"], r["f0_delta_pct"]))
        parts.append(verdict + "</div>")

    parts.append("<h2>原始数据</h2>")
    parts.append("<table><thead><tr><th>句类</th><th>speaker</th><th>emotion</th>"
                 "<th>状态</th><th>字节</th><th>时长 s</th><th>F0 Hz</th>"
                 "<th>F0 变化</th></tr></thead><tbody>")
    for r in rows:
        for which, label in (("old", "旧"), ("new", "新")):
            d = r[which]
            chg = "—"
            if not r["same"] and which == "new":
                chg = "%+.1f%%" % r["f0_delta_pct"]
            cls = "chg" if (not r["same"] and which == "new") else "ok"
            parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                         '<td class="num">%d</td><td class="num">%s</td>'
                         '<td class="num">%s</td><td class="num %s">%s</td></tr>'
                         % (r["label"], r["speaker"], r["emotion"], label,
                            d["bytes"], d["dur"],
                            ("%.1f" % d["f0"]) if d["f0"] == d["f0"] else "—",
                            cls, chg))
    parts.append("</tbody></table>")

    parts.append("<h2>三条必须知道的边界</h2>")
    parts.append('<div class="note">')
    parts.append("<ul>")
    parts.append("<li><b>变化方向不可预测。</b>实测两句「平静」F0 一升一降"
                 "（%+.1f%% / %+.1f%%），并不都\"变平\"——"
                 "上一轮我预测的\"重跑后更平\"不成立。</li>"
                 % (rows[0]["f0_delta_pct"],
                    rows[1]["f0_delta_pct"] if len(rows) > 1 else 0.0))
    parts.append("<li><b>时长会变，下游要一并重跑。</b>去指令后句长变化明显"
                 "（+24%% / +10%%），整期总时长随之变化，混音、字幕、视频都会受影响。</li>")
    parts.append("<li><b>服务必须是新进程。</b>改的是 <code>tts_service/serve.py</code>，"
                 "旧进程里跑的还是老代码——重跑前务必重启。</li>")
    parts.append("</ul></div>")

    parts.append("</div></body></html>")

    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write("".join(parts))

    print("已生成：%s" % OUT)
    print("体积：%.2f MB" % (os.path.getsize(OUT) / 1048576.0))
    print("样本：%d 句（%d 句变化 / %d 句不变）" % (len(rows), n_real, n_same))


if __name__ == "__main__":
    main()
