# -*- coding: utf-8 -*-
"""生成结论页：同句换种子的试听对照（自包含，禁 emoji / 禁外部 CDN）。"""
import base64
import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
D = os.path.join(ROOT, "_smoke", "_seed_probe2")
OUT = os.path.join(D, "verdict.html")

fin = json.load(io.open(os.path.join(ROOT, "_smoke/_speaker_probe/final.json"),
                        encoding="utf-8"))
rows = fin["rows"]
verdict = fin["verdict"]

groups = {}
for r in rows:
    groups.setdefault(r["tag"], []).append(r)
for g in groups.values():
    g.sort(key=lambda x: x["k"])

LABEL = {"0003": "脚本第 3 句（32 字）", "0019": "脚本第 19 句（25 字）"}
TEXT = {t: groups[t][0]["text"] for t in groups}


def audio_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


p = []
p.append("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>同句换种子 · 说话人一致性实测</title>
<style>
:root{--ink:#1c1f24;--muted:#6b7280;--line:#e5e7eb;--bg:#eef0f3;--card:#fff;
--accent:#2563eb;--warn:#b45309;--hot:#b91c1c;--calm:#0f766e;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.7 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;
background-image:linear-gradient(#dfe3e8 1px,transparent 1px),linear-gradient(90deg,#dfe3e8 1px,transparent 1px);
background-size:26px 26px;}
header{background:#111827;color:#f9fafb;padding:26px 22px 22px;}
header .wrap{max-width:1080px;margin:0 auto}
header h1{margin:0 0 6px;font-size:21px;letter-spacing:.3px}
header .sub{color:#9ca3af;font-size:13px}
main{max-width:1080px;margin:0 auto;padding:22px 16px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin:0 0 18px;box-shadow:0 1px 3px rgba(16,24,40,.05)}
h2{font-size:17px;margin:0 0 12px;padding-left:10px;border-left:4px solid var(--accent)}
h3{font-size:15px;margin:18px 0 8px;color:#111827}
p{margin:8px 0}
.lede{background:#fff7ed;border:1px solid #fed7aa;border-left:4px solid var(--warn)}
table{width:100%;border-collapse:collapse;margin:10px 0 4px;font-size:13.5px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{background:#f3f4f6;font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.prod td{background:#eff6ff;font-weight:600}
.small{font-size:12.5px;color:var(--muted)}
.big{color:var(--hot);font-weight:700}
.ok{color:var(--calm);font-weight:700}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;
padding:9px 11px;border:1px solid var(--line);border-radius:8px;margin:7px 0;background:#fafbfc}
.row.prod{background:#eff6ff;border-color:#bfdbfe}
.row .tag{font-weight:700;width:56px;flex:none;font-size:13px}
.row audio{flex:1;min-width:230px;height:34px}
.row .meta{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0}
.kpi{background:#f9fafb;border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.kpi b{display:block;font-size:19px;color:var(--accent);font-variant-numeric:tabular-nums}
.kpi span{font-size:12.5px;color:var(--muted)}
footer{max-width:1080px;margin:0 auto;padding:0 16px 40px;color:var(--muted);font-size:12.5px}
@media(max-width:640px){.row .tag{width:auto}header h1{font-size:18px}}
</style></head><body>
<header><div class="wrap">
<h1>同一句话，只换随机种子 —— 声音会变成另一个人吗</h1>
<div class="sub">对象：20260915-103249 第 3 句与第 19 句 · 音色均为 Serena · 情绪标签均为「解释」</div>
</div></header><main>""")

p.append("""<div class="card lede">
<h2>先给结论</h2>
<p><b>这两个文件确实是同一个音色 Serena 生成的</b>，但听起来不像同一个人——
这不是你的错觉，也不是改坏了什么。</p>
<p>真相是：<span class="big">同一句话，只把随机种子换一下，产生的差异和这两句之间的差异一样大，有时更大。</span>
下面 12 段音频就是证据：前 6 段是第 3 句的 6 个版本，后 6 段是第 19 句的 6 个版本，
每一段的文字、音色、参数完全相同，<b>唯一的差别是随机种子</b>。</p>
</div>""")

# ---------------- 试听 ----------------
for tag in ("0003", "0019"):
    g = groups[tag]
    p.append('<div class="card"><h2>%s_B · %s</h2>' % (tag, LABEL[tag]))
    p.append('<p class="small">「%s」</p>' % TEXT[tag])
    for r in g:
        cls = "row prod" if r["k"] == 0 else "row"
        note = "（这是现有产物）" if r["k"] == 0 else ""
        p.append(
            '<div class="%s"><div class="tag">种子 +%d %s</div>'
            '<audio controls preload="none" src="data:audio/wav;base64,%s"></audio>'
            '<div class="meta">F0 %s Hz ｜ F1 %s ｜ F2 %s ｜ 谱质心 %s ｜ %.2f 秒</div></div>'
            % (cls, r["k"], note, audio_b64(os.path.join(D, r["file"])),
               "%.0f" % r["f0_med"], "%.0f" % r["f1"], "%.0f" % r["f2"],
               "%.0f" % r["cent"], r["dur"]))
    p.append('<p class="small">6 段全部同文本、同音色、同指令（不发）、同温度（0.4），'
             '唯一变量是种子。听不出「换人」感的话，把音量调大再听 F0 高低差。</p></div>')

# ---------------- 数据 ----------------
p.append('<div class="card"><h2>数据：两句之差 vs 每句自己换种子</h2>')
p.append('<table><thead><tr><th>指标</th><th class="num">产物本身两句之差</th>'
         '<th class="num">第 3 句自己的波动</th><th class="num">第 19 句自己的波动</th>'
         '<th class="num">比值</th></tr></thead><tbody>')
NAMES = [("f0_med", "F0 中位（音高）"), ("f0_p10", "F0 低端"), ("f0_p90", "F0 高端"),
         ("f1", "F1 共振峰"), ("f2", "F2 共振峰"), ("cent", "谱质心（音色亮度）"),
         ("dur", "时长")]
for k, label in NAMES:
    v = verdict[k]
    ratio = v["ratio"]
    flag = ' class="big"' if ratio < 1.0 else ''
    mark = " ←" if ratio < 1.0 else ""
    p.append('<tr><td>%s</td><td class="num">%.1f</td><td class="num">%.1f</td>'
             '<td class="num">%.1f</td><td class="num"%s>%.2f×%s</td></tr>'
             % (label, v["between"], v["span_0003"], v["span_0019"], flag, ratio, mark))
p.append('</tbody></table>')
p.append('<p class="small">「自己波动」= 同一句话 6 个种子的极差。'
         '比值 = 两句之差 ÷ 第 3 句自己的波动；<span class="big">小于 1 意味着：'
         '同一句话自己换个种子，差异就比这两句之间还大。</span></p></div>')

# ---------------- 关键细节 ----------------
a = fin["prod_a"]
b = fin["prod_b"]
p.append('<div class="card"><h2>最刺眼的两个细节</h2>')
p.append(
    '<div class="grid">'
    '<div class="kpi"><b>200 → 277 Hz</b><span>第 19 句自己换种子，音高跨度 77 Hz（+32%%）</span></div>'
    '<div class="kpi"><b>%.1f Hz</b><span>现有产物的音高 —— 落在这句话自己 6 个版本里的<b>最低端</b></span></div>'
    '<div class="kpi"><b>%.1f Hz</b><span>第 3 句换个种子后能到的音高，比上面那个高了 39 Hz</span></div>'
    '</div>' % (b["f0_med"], groups["0003"][1]["f0_med"]))
p.append('<h3>换句话说</h3><p>第 19 句这次抽到的是它自己范围里最低的那个声音（%.1f Hz），'
         '第 3 句抽到的是中间偏高（%.1f Hz）。两句之间 25.6 Hz 的落差，'
         '恰好把两个分布拉开到听得出来的程度 —— 而它们各自的分布原本是重叠的。</p>'
         % (b["f0_med"], a["f0_med"]))
p.append('<p>第 19 句如果当时抽到 +3（%.1f Hz），它会比第 3 句的任何版本都高，'
         '听感上「更年轻」——但文字一个字都没变。</p>' % groups["0019"][3]["f0_med"])
p.append('<p class="small">注：谱质心是唯一一个「两句之差」明显超过两组自身波动的指标'
         '（%.1f vs %.1f / %.1f，比值 %.2f×）。它反映的是频谱重心，'
         '受文本本身的音素构成影响 —— 也就是说，音色亮度这一项，两句确实有内容层面的差别，'
         '不是纯随机。</p></div>'
         % (verdict["cent"]["between"], verdict["cent"]["span_0003"],
            verdict["cent"]["span_0019"], verdict["cent"]["ratio"]))

# ---------------- 复现性 ----------------
p.append('<div class="card"><h2>同时验证了一件事：重跑不会变</h2>')
p.append("""<p>我用产品完全相同的调用路径（HTTP <code>/tts</code>，同一个文本、音色、
情绪标签），把这两个文件重新生成了一遍：</p>
<table><thead><tr><th>文件</th><th>现有产物</th><th>重新生成</th><th>结果</th></tr></thead><tbody>
<tr><td>0003_B.wav</td><td class="num">257544 样本</td><td class="num">257544 样本</td>
<td class="ok">逐样本完全一致</td></tr>
<tr class="prod"><td>0019_B.wav</td><td class="num">229320 样本</td><td class="num">229320 样本</td>
<td class="ok">逐样本完全一致</td></tr>
</tbody></table>
<p class="small">相关系数 1.000000000，逐样本最大差 0。种子由「音色 + 文本」派生，
所以每句话的音色一旦定下来就永久固定。<b>你这次（今天 09:57～10:16）重跑过的整期，
再跑一次还是同一个结果 —— 这两句不会变好，也不会变坏。</b></p>
</div>""")

p.append("""<div class="card"><h2>为什么会这样</h2>
<p>Qwen3-TTS 是自回归生成：一个字一个字往外吐，每个字都基于「已吐出的全部 + 条件」。
音色不是独立控制的量，它和内容一起被采样出来。于是：</p>
<p>文字变了 → 轨迹变了 → 音色跟着重新抽签。<br>
种子变了 → 起点变了 → 音色也重新抽签。</p>
<p>而文字是每句必然不同的，所以<b>音色必然逐句漂移</b>。这不是参数没调对，
是这套架构里「谁说」和「说什么」没有解耦。1.7B 的体量下，这个纠缠尤其明显。</p>
<p class="small">我另外测过：把所有句子改成固定同一个种子，音色一致性并没有改善
（F1 共振峰离散度 1.01×、F2 反而变差 0.29×）—— 说明种子不是主因，
换种子这条路救不了。</p>
</div>""")

p.append('</main><footer>数据来源：本机 Qwen3-TTS-12Hz-1.7B-CustomVoice（Serena）· '
         'faster-qwen3-tts CUDA 图路径 · 温度 0.4 · 无指令。<br>'
         '分析口径：F0 用 pyin，共振峰用 LPC 复根法，均为帧级中位数。<br>'
         '所有音频与结论可由 _smoke/ 下脚本复跑。</footer></body></html>')

with io.open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(p))
print("已生成 %s（%.2f MB）" % (OUT, os.path.getsize(OUT) / 1048576.0))
