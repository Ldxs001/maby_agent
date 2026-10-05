# -*- coding: utf-8 -*-
"""固定种子 / 关子码本 实测报告页。

自包含 HTML：音频 base64 内联（mp3 32k 单声道），无外部 CDN，禁 emoji。
数据源：_smoke/_anchor_probe/anchor.json、_smoke/_anchor_probe2/anchor2.json
"""
import base64
import io
import json
import os
import subprocess

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
A1 = os.path.join(ROOT, "_smoke", "_anchor_probe")
A2 = os.path.join(ROOT, "_smoke", "_anchor_probe2")
OUT = os.path.join(ROOT, "_smoke", "_anchor_report")
TMP = os.path.join(OUT, "_mp3")

SHOW_SENT = [3, 45, 123, 237]          # 覆盖 F0 高 / 中 / 低

LABEL = [
    ("derived", "现状：每句各自派生种子", "P1", "baseline"),
    ("agg", "用整期语料算一个种子", "P1", "user"),
    ("voice", "用音色名算一个种子", "P1", "user"),
    ("zero", "固定 0", "P1", "fix"),
    ("fixed12345", "固定 12345", "P1", "fix"),
    ("rand", "固定 podcast-maker 串", "P1", "fix"),
    ("off_derived", "关子码本 + 现状种子", "P2", "off"),
    ("off_agg", "关子码本 + 整期语料种子", "P2", "off"),
    ("combo_12345_off", "关子码本 + 固定 12345", "P2", "combo"),
    ("combo_rand_off", "关子码本 + 固定串", "P2", "combo"),
]
METRICS = ("f0_med", "f1", "f2", "cent", "dur")


def to_mp3(src, tag):
    os.makedirs(TMP, exist_ok=True)
    dst = os.path.join(TMP, tag + ".mp3")
    if not os.path.exists(dst):
        subprocess.run(["ffmpeg", "-y", "-i", src, "-b:a", "32k", "-ac", "1", dst],
                       capture_output=True)
    return dst


def b64(path):
    with open(path, "rb") as f:
        return "data:audio/mpeg;base64," + base64.b64encode(f.read()).decode()


def disp(rs):
    s = {}
    for k in METRICS:
        v = np.array([r[k] for r in rs], dtype=float)
        s[k + "_sd"] = float(v.std(ddof=1)) if v.size > 1 else 0.0
        s[k + "_span"] = float(v.max() - v.min()) if v.size else 0.0
    return s


def find_wav(tag, idx):
    for base in (A1, A2):
        for pre in ("P1_", "P2_", ""):
            p = os.path.join(base, "%s%s_%04d.wav" % (pre, tag, idx))
            if os.path.exists(p):
                return p
    return None


CSS = """
:root{--bg:#0e1116;--panel:#151a21;--card:#1a1f28;--line:#28303a;--tx:#e8ecf1;
--dim:#8f99a6;--acc:#5b9dff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 22px 70px}
h1{font-size:23px;margin:0 0 6px;letter-spacing:.3px}
h2{font-size:17px;margin:34px 0 12px;padding-left:11px;
border-left:3px solid var(--acc)}
h3{font-size:14.5px;margin:20px 0 9px;color:#c9d1da}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:22px}
table{border-collapse:collapse;width:100%;margin:11px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left}
th{background:#1d232c;color:#cfd6de;font-weight:600;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr.hl td{background:#1b2431}
.good{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid #556;
border-radius:5px;padding:11px 14px;margin:13px 0;font-size:13px;color:#c6cdd6}
.note.warn{border-left-color:var(--warn)}
.note.bad{border-left-color:var(--bad)}
.note.ok{border-left-color:var(--ok)}
.note b{color:#fff}
code{background:#1d232c;padding:1px 5px;border-radius:3px;font-size:12.5px;color:#9fd0ff}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:13px 15px;margin:12px 0}
.ct{font-size:13px;color:#dbe2ea;margin-bottom:10px;padding-bottom:8px;
border-bottom:1px dashed #2c323c;display:flex;justify-content:space-between;
align-items:baseline;gap:10px;flex-wrap:wrap}
.ct .tag{font-size:11.5px;padding:1px 7px;border-radius:9px;
border:1px solid var(--line);color:var(--dim)}
.ct .tag.user{border-color:#3d5a8a;color:#8fb8ff}
.ct .tag.baseline{border-color:#4a4a4a;color:#a8a8a8}
.ct .tag.off{border-color:#4c6b3a;color:#a3d977}
.ct .tag.combo{border-color:#6b5a8a;color:#c1a3ff}
.aud{display:grid;grid-template-columns:1fr;gap:7px}
.aud .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.aud .lb{font-size:12px;color:var(--dim);min-width:132px}
audio{height:30px;flex:1;min-width:230px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px;margin:12px 0}
.kv div{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:9px 11px}
.kv .k{font-size:11.5px;color:var(--dim)}
.kv .v{font-size:18px;margin-top:3px;font-variant-numeric:tabular-nums}
.foot{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
color:var(--dim);font-size:12px}
"""

COND_ORDER = [c[0] for c in LABEL]
COND_META = {c[0]: (c[1], c[3]) for c in LABEL}
COND_BLK = {c[0]: c[2] for c in LABEL}


def main():
    d1 = json.load(io.open(os.path.join(A1, "anchor.json"), encoding="utf-8"))
    rows1 = d1["rows"]
    rows2 = []
    p2 = os.path.join(A2, "anchor2.json")
    if os.path.exists(p2):
        rows2 = json.load(io.open(p2, encoding="utf-8"))["rows"]
    rows = rows1 + rows2

    stats, alldisp = {}, {}
    for tag in COND_ORDER:
        rs = [r for r in rows if r["cond"] == tag]
        if not rs:
            continue
        alldisp[tag] = disp(rs)

    base = alldisp["derived"]
    p = []
    p.append("<h1>音色漂移：两条权宜路实测 + 一条治本路</h1>")
    p.append('<div class="sub">同一批 12 句（B 音色 Serena、情绪标签=解释，'
             '与产品实际链路一致）｜口径：pyin 基频 + LPC 复根共振峰｜'
             '时间戳 09/16/2026 11:52</div>')

    # ---------- 结论速览 ----------
    p.append("<h2>一、结论速览</h2>")
    p.append('<table><tr><th>你的问题</th><th>实测答案</th></tr>'
             '<tr><td>用全部 A 的文本算一个种子，整期固定</td>'
             '<td class="bad">技术上能做，但它反而是唯一让一致性变差的方案：'
             'F0 极差从 97.7 恶化到 119.8 Hz</td></tr>'
             '<tr><td>换其他固定种子值呢</td>'
             '<td class="warn">小幅改善 F0（最多 1.37 倍），但音色指纹 F1 几乎不动；'
             '<b>分两半验证时「最优值」直接翻转</b></td></tr>'
             '<tr><td>关掉子码本采样</td>'
             '<td>F2（第二共振峰）改善 1.65-1.81 倍、语速波动改善 1.18-1.25 倍，'
             'F0 基本不动 —— 有效但有限</td></tr>'
             '<tr><td>根因是什么</td>'
             '<td class="bad">这个模型没有「提取音色」的器官：'
             'CustomVoice 权重里 speaker_encoder 张量 = 0 个</td></tr>'
             '<tr><td>怎么做才治本</td>'
             '<td class="good">换同一家族的 Base 变体（多 76 个 speaker_encoder 张量），'
             '用固定的 2048 维音色向量 —— 速度与 CUDA 图全部保留</td></tr>'
             "</table>")

    # ---------- 方差分解 ----------
    allf0 = np.array([r["f0_med"] for r in rows1])
    grand = allf0.mean()
    picks1 = d1["picks"]
    conds1 = [c for c in ["derived", "agg", "voice", "zero", "fixed12345",
                          "rand", "off_derived", "off_agg"]]
    by_sent = np.array([np.mean([r["f0_med"] for r in rows1 if r["idx"] == i])
                        for i in picks1])
    by_cond = np.array([np.mean([r["f0_med"] for r in rows1 if r["cond"] == c])
                        for c in conds1])
    tot = ((allf0 - grand) ** 2).sum()
    ss_sent = len(conds1) * ((by_sent - grand) ** 2).sum()
    ss_cond = len(picks1) * ((by_cond - grand) ** 2).sum()
    pct_sent, pct_cond = 100 * ss_sent / tot, 100 * ss_cond / tot
    pct_rest = 100 - pct_sent - pct_cond

    p.append("<h2>二、先看清一件事：音高主要由「文本」决定，不由「种子」决定</h2>")
    p.append('<div class="kv">'
             '<div><div class="k">句子之间（文本决定）</div>'
             '<div class="v">%.1f%%</div></div>'
             '<div><div class="k">种子 / 采样策略之间（你能调的）</div>'
             '<div class="v warn">%.1f%%</div></div>'
             '<div><div class="k">交互与残余</div>'
             '<div class="v">%.1f%%</div></div></div>' % (pct_sent, pct_cond, pct_rest))
    p.append('<div class="note">把 12 句 × 8 个条件的 96 次合成的 F0 做方差分解：'
             '<b>句子本身（文字不同）解释了 %.1f%% 的音高差异</b>，'
             '而种子和采样策略加起来只解释 <b>%.1f%%</b>。'
             '换句话说，固定种子能动的天花板就是这几格。</div>'
             % (pct_sent, pct_cond))

    p.append("<h3>同一个句子在 8 个条件之间的摆幅</h3>")
    p.append('<table><tr><th>句号</th><th class="n">字数</th><th class="n">最低 F0</th>'
             '<th class="n">最高 F0</th><th class="n">摆幅</th></tr>')
    for idx in picks1:
        rs = [r for r in rows1 if r["idx"] == idx]
        f0 = [r["f0_med"] for r in rs]
        p.append('<tr><td>%04d</td><td class="n">%d</td><td class="n">%.1f</td>'
                 '<td class="n">%.1f</td><td class="n">%.1f</td></tr>'
                 % (idx, rs[0]["chars"], min(f0), max(f0), max(f0) - min(f0)))
    p.append("</table>")
    p.append('<div class="note">同一个句子、同一个音色，只换种子/采样策略，'
             '音高就能摆 24.7 到 98.9 Hz。这就是你说的「怎么突然变了」。</div>')

    # ---------- 固定种子实测 ----------
    p.append("<h2>三、固定种子：实测六种算法</h2>")
    p.append('<table><tr><th>种子策略</th><th class="n">F0 标</th>'
             '<th class="n">F0 极差</th><th class="n">F1 标</th><th class="n">F2 标</th>'
             '<th class="n">语速快慢比</th></tr>')
    for tag in ["derived", "agg", "voice", "zero", "fixed12345", "rand"]:
        rs = [r for r in rows1 if r["cond"] == tag]
        s = alldisp[tag]
        sp = np.array([r["chars"] / r["dur"] for r in rs])
        cls = ' class="hl"' if tag == "agg" else ""
        mark = " <span class=\"bad\">（变差）</span>" if tag == "agg" else ""
        p.append('<tr%s><td>%s%s</td><td class="n">%.1f</td><td class="n">%.1f</td>'
                 '<td class="n">%.1f</td><td class="n">%.1f</td><td class="n">%.2f</td></tr>'
                 % (cls, COND_META[tag][0], mark, s["f0_med_sd"], s["f0_med_span"],
                    s["f1_sd"], s["f2_sd"], sp.max() / sp.min()))
    p.append("</table>")

    p.append('<div class="note bad"><b>你提的那个方案（用全部 A 的文本算一个种子）'
             '是六种里唯一变差的</b>：F0 极差 97.7 → 119.8 Hz、F1 标 110.4 → 133.8、'
             '谱质心标 160.9 → 355.4。原因是「全体文本拼接」这个哈希值没有任何优选含义，'
             '它和随机挑一个数等价 —— 而随机挑数就是抽签。</div>')

    p.append('<div class="note warn"><b>分半验证（12 句切前 6 / 后 6）：</b>'
             '前 6 句最优的种子是<b>固定 12345</b>，后 6 句最优的是<b>固定 0</b>。'
             '<b>结论在换一批句子后就翻转</b> —— 说明「挑一个好种子」没有可推广性，'
             '它不是找到了规律，是碰上了运气。</div>')

    # 试听：固定种子
    p.append("<h3>试听：同一批句子、只换种子策略</h3>")
    for tag in ["derived", "agg", "voice", "zero", "fixed12345", "rand"]:
        name, kind = COND_META[tag]
        p.append('<div class="card"><div class="ct"><span>%s</span>'
                 '<span class="tag %s">%s</span></div><div class="aud">' % (name, kind, kind))
        for idx in SHOW_SENT:
            w = find_wav(tag, idx)
            if not w:
                continue
            mp3 = to_mp3(w, "%s_%d" % (tag, idx))
            p.append('<div class="row"><span class="lb">%04d · %s</span>'
                     '<audio controls preload="none" src="%s"></audio></div>'
                     % (idx, w.split("_")[-1].replace(".wav", ""), b64(mp3)))
        p.append("</div></div>")

    # ---------- 关子码本 ----------
    p.append("<h2>四、关掉子码本采样</h2>")
    p.append('<table><tr><th>条件</th><th class="n">F0 标</th><th class="n">F0 极差</th>'
             '<th class="n">F1 标</th><th class="n">F2 标</th><th class="n">语速快慢比</th></tr>')
    for tag in ["derived", "off_derived", "off_agg"]:
        rs = [r for r in rows1 if r["cond"] == tag]
        s = alldisp[tag]
        sp = np.array([r["chars"] / r["dur"] for r in rs])
        p.append('<tr><td>%s</td><td class="n">%.1f</td><td class="n">%.1f</td>'
                 '<td class="n">%.1f</td><td class="n">%.1f</td><td class="n">%.2f</td></tr>'
                 % (COND_META[tag][0], s["f0_med_sd"], s["f0_med_span"],
                    s["f1_sd"], s["f2_sd"], sp.max() / sp.min()))
    p.append("</table>")
    p.append('<div class="note">改善方向是实在的：<b>F2（第二共振峰，音色指纹里最硬的一维）'
             '改善 1.65-1.81 倍</b>，语速快慢比从 1.65 压到 1.40-1.64。'
             '但 F0（音高）基本没动（1.08-1.09 倍）。'
             '注意：上一轮 6 句的小样本曾测出 F0 极差收窄 5.2 倍，'
             '这次扩到 12 句后<b>没有复现</b> —— 那个数字是小样本假象。</div>')
    for tag in ["off_derived", "off_agg"]:
        name, kind = COND_META[tag]
        p.append('<div class="card"><div class="ct"><span>%s</span>'
                 '<span class="tag %s">%s</span></div><div class="aud">' % (name, kind, kind))
        for idx in SHOW_SENT:
            w = find_wav(tag, idx)
            if not w:
                continue
            mp3 = to_mp3(w, "%s_%d" % (tag, idx))
            p.append('<div class="row"><span class="lb">%04d</span>'
                     '<audio controls preload="none" src="%s"></audio></div>'
                     % (idx, b64(mp3)))
        p.append("</div></div>")

    # ---------- 叠加 ----------
    if rows2:
        p.append("<h2>五、两条路叠加</h2>")
        p.append('<table><tr><th>条件</th><th class="n">F0 标</th><th class="n">F0 极差</th>'
                 '<th class="n">F1 标</th><th class="n">F2 标</th>'
                 '<th class="n">语速快慢比</th></tr>')
        for tag in ["derived", "fixed12345", "off_derived", "combo_12345_off",
                    "combo_rand_off"]:
            if tag not in alldisp:
                continue
            rs = [r for r in rows if r["cond"] == tag]
            s = alldisp[tag]
            sp = np.array([r["chars"] / r["dur"] for r in rs])
            p.append('<tr><td>%s</td><td class="n">%.1f</td><td class="n">%.1f</td>'
                     '<td class="n">%.1f</td><td class="n">%.1f</td>'
                     '<td class="n">%.2f</td></tr>'
                     % (COND_META[tag][0], s["f0_med_sd"], s["f0_med_span"],
                        s["f1_sd"], s["f2_sd"], sp.max() / sp.min()))
        p.append("</table>")
        for tag in ["combo_12345_off", "combo_rand_off"]:
            name, kind = COND_META[tag]
            p.append('<div class="card"><div class="ct"><span>%s</span>'
                     '<span class="tag %s">%s</span></div><div class="aud">'
                     % (name, kind, kind))
            for idx in SHOW_SENT:
                w = find_wav(tag, idx)
                if not w:
                    continue
                mp3 = to_mp3(w, "%s_%d" % (tag, idx))
                p.append('<div class="row"><span class="lb">%04d</span>'
                         '<audio controls preload="none" src="%s"></audio></div>'
                         % (idx, b64(mp3)))
            p.append("</div></div>")

    # ---------- 治本路 ----------
    p.append("<h2>六、治本：同一个模型家族的 Base 变体</h2>")
    p.append('<table><tr><th>变体</th><th class="n">talker 张量</th>'
             '<th class="n">speaker_encoder 张量</th><th>音色从哪来</th></tr>'
             '<tr class="hl"><td>CustomVoice（你现在用的）</td><td class="n">404</td>'
             '<td class="n bad">0</td><td>一个离散的预设音色编号，模型要在生成过程里'
             '自己把它「演」出来</td></tr>'
             '<tr><td>Base（HF 上的）</td><td class="n">404</td>'
             '<td class="n good">76</td><td>ECAPA-TDNN 从参考录音提取 '
             '<b>2048 维音色向量</b>，直接前缀进 token 序列</td></tr></table>')
    p.append('<div class="note ok"><b>两个变体的 talker 张量数量完全一样（404）</b>，'
             '差别只在 Base 多了 76 个 <code>speaker_encoder.*</code> 张量。'
             '这就是根因：CustomVoice 没有「提取音色」的器官。</div>')
    p.append('<div class="note">加速包已经内置这条路：'
             '<code>faster_qwen3_tts</code> 提供 <code>generate_voice_clone()</code>，'
             '内部调用的就是同一套 <code>fast_generate(talker_graph=..., '
             'predictor_graph=...)</code> —— <b>CUDA 图与 5.5 倍速度全部保留</b>。'
             '且支持 <code>xvec_only=True</code>（只用那 2048 维向量，不带参考音频进上下文）'
             '与 <code>voice_clone_prompt</code> 预算缓存。</div>')

    # ---------- 局限 ----------
    p.append("<h2>七、必须说清的局限</h2>")
    p.append('<div class="note warn">'
             '<b>1. 样本量</b>：12 句，比上一轮的 6 句翻了一倍，但绝对值仍会随句子集合变化，'
             '只有同一批内的横向对比可信。<br>'
             '<b>2. 上一轮的 5.2 倍没有复现</b>：关子码本在 6 句小样本上测出的 F0 改善，'
             '扩到 12 句后消失。<br>'
             '<b>3. 听感未做主观评测</b>：所有指标都是声学量的，'
             '最终能不能接受只能靠耳朵 —— 页面里的音频就是为此准备的。<br>'
             '<b>4. Base 变体未实测</b>：结论来自权重结构对照与官方文档，'
             '未下载 4.23 GB 权重跑过。</div>')

    p.append('<div class="foot">所有音频由本机 Qwen3-TTS-12Hz-1.7B-CustomVoice 生成，'
             '同批句子、同分析口径。产品代码零改动，实验脚本位于 '
             '<code>tools/probes/probe_anchor.py</code> 与 '
             '<code>tools/probes/probe_anchor2.py</code>，可复跑。</div>')

    os.makedirs(OUT, exist_ok=True)
    html = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>音色漂移实测报告</title><style>" + CSS + "</style></head><body>"
            "<div class=\"wrap\">" + "\n".join(p) + "</div></body></html>")
    dst = os.path.join(OUT, "anchor_report.html")
    with io.open(dst, "w", encoding="utf-8") as f:
        f.write(html)
    print("页面: %s" % dst)
    print("大小: %.2f MB" % (len(html.encode("utf-8")) / 1048576.0))
    print("内联音频段数: %d" % html.count("data:audio/mpeg;base64"))
    ext = [t for t in ("http://", "https://", "<script", "<link")
           if t in html.replace("http://www.w3.org", "")]
    print("外部依赖: %s" % (ext or "无"))


if __name__ == "__main__":
    main()
