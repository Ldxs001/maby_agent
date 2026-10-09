#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方基准音色 —— 把 9 个内置音色预录成三句混合参考音频，附试听页。

为什么要有这个脚本
------------------
CustomVoice 每次现录参考音频有两笔账：每建一个项目都要单独起一次进程加载
约 3.4GB 权重；而 CustomVoice 自己句间漂移（实证：同一音色每句听感像换人），
各项目各录各的，等于每次都掷一次骰子。把 9 个内置音色**各录一次、挑好定稿、
随仓库分发**，项目直接用官方档案（或继承），CustomVoice 就从必装降级为
「只有想重新录才需要」。

流程（人工在环）
----------------
  1. 本脚本一次性录完：每个音色默认 2 个 take（种子按 take 序号错开，确定性
     复现），全部过「念全校验」门禁（语速区间 + 语音段数 ≥3，不合格自动重念，
     与 make_voice.build() 同一套尺）。
  2. 跑完自动产出单文件试听页（音频 base64 内嵌），人在页上听、挑 take、
     写备注，一键生成反馈清单。
  3. 反馈回来后按意见重录（--voices 单挑、--force 重录），最终把选中的
     take 定为官方档案。

确定性边界与 make_voice 相同：同设备、同后端下同一套输入得到同一条波形；
profile.json 记了 device / backend / seed，对账有据。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_voice  # noqa: E402
import serve  # noqa: E402

OUT_DIR = os.path.join(HERE, "official_voices")
# 官方基准文案：**双文案**（2026-10-08 定稿，混合三句型 + 引号转述 + 省略号
# 拖停）。A 案 take1–3、B 案 take4–6：同一音色录两套韵律先验，A/B 角认领时
# 只认自己文案的 take（adopt_official 按 text_role 把关，旧档案无该字段视为
# 兼容任何角色）。混合句型的道理不变：Base 克隆整条继承 ref 的韵律先验。
# 仍必须与项目内容无关，否则音色基准会跟着项目漂。
OFFICIAL_TEXT = make_voice.REF_TEXTS["A"]      # 兼容旧读者（试听页标题等）
TEXT_ROLES = (("A", make_voice.REF_TEXTS["A"]),
              ("B", make_voice.REF_TEXTS["B"]))
TAKES = 3          # 每条文案的 take 数（老样子 +1、+2、+3）


def wav_sha16(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def record(voices, takes: int, device: str, force: bool, log=print,
           rate_min: float = None, rate_max: float = None,
           max_attempts: int = None) -> dict:
    """为指定音色按 A/B 双文案各录 takes 条参考音频。已有且非 force 的 take 跳过。

    语速窗默认沿用 make_voice 的门禁常量；本轮收窄（4.00–4.36）时自然落点
    常在窗外，重试耗尽后取**念全（段数达标）里离窗最近**的一条定稿，take
    记 ``rate_out_of_window`` —— 试听页红标，人耳挑的时候看得见，不谎报过关。
    念全（段数 ≥3）没有兜底：缺句的音频是废品，连兜底资格都没有。
    """
    rate_min = make_voice.RATE_MIN if rate_min is None else float(rate_min)
    rate_max = make_voice.RATE_MAX if rate_max is None else float(rate_max)
    max_attempts = (make_voice.MAX_ATTEMPTS if max_attempts is None
                    else int(max_attempts))
    resolved = serve.resolve_model(serve.CUSTOM_VOICE_MODEL)
    kind = serve.model_kind(resolved)
    if kind != "custom_voice":
        raise RuntimeError(
            "录官方基准要用内置音色变体，当前解析到 %s（%s）。"
            % (resolved, kind))

    log("加载 %s —— 只用来出官方基准音频，跑完就退" % os.path.basename(resolved))
    eng = serve.Engine(resolved, device, log)
    t0 = time.time()
    eng.ensure()
    log(f"  后端 {eng.backend} · 设备 {eng.device} · 加载 {time.time() - t0:.1f}s")
    if str(eng.device or "").startswith("cpu"):
        log("  **注意**：本次跑在 CPU 上（%s），慢十几倍。要 GPU 先腾空显存。"
            % (eng.device_note or "空闲显存不足"))

    out = {"out_dir": OUT_DIR, "voices": {}}
    for meta in serve.SPEAKERS:
        name = meta["name"]
        if voices and name not in voices:
            continue
        vdir = os.path.join(OUT_DIR, name)
        os.makedirs(vdir, exist_ok=True)
        seed0 = serve.seed_for(OFFICIAL_TEXT, name)
        # 旧档案要先读出来：跳过的已有 take 要沿用其元数据（种子/sha 等），
        # 已定的 chosen 也要过到新档案上 —— profile.json 每次是整体重写，
        # 不带走就等于重录一次丢一次账。
        prev_path = os.path.join(vdir, "profile.json")
        prev = None
        if os.path.isfile(prev_path):
            try:
                prev = json.load(io.open(prev_path, encoding="utf-8"))
            except (ValueError, OSError):
                prev = None
        prev_takes = {t.get("take"): t
                      for t in (prev or {}).get("takes", []) if t.get("take")}
        takes_rec = []
        re_takes = set()     # 本轮真正重录的 take 号：旧人耳结论只认没重录的
        for text_role, text in TEXT_ROLES:
            seed0 = serve.seed_for(text, name)
            for i in range(1, takes + 1):
                take_no = i if text_role == "A" else takes + i
                wav_path = os.path.join(vdir, "take%d.wav" % take_no)
                if not force and os.path.isfile(wav_path):
                    log("  %s take%d（%s案）已有，跳过"
                        % (name, take_no, text_role))
                    takes_rec.append(prev_takes.get(take_no) or {"take": take_no})
                    continue
                re_takes.add(take_no)
                # take 序号参与种子错开，但**各 take 搜索区间必须互不相交**：
                # 起点按 take 错开 1000、重试 +7，max_attempts×7 ≤ 112 < 1000，
                # 区间永不重叠。窄窗下（2026-10-08 教训）旧方案 `(take_no-1)*7`
                # 的各 take 序列大量重叠，全收敛到同一条离窗最近的种子 —— 三条
                # take 逐字节相同，+1/+2/+3 就失去了意义。区间不相交保证兜底
                # 落点也不同波形。仍是纯确定性：种子全由 (text, voice, take,
                # attempt) 决定，可复现。
                attempts = 0
                s = seed0 + take_no * 1000
                best = None       # 窗外兜底：念全（段数达标）里离窗最近的一条
                while True:
                    attempts += 1
                    serve.set_seed(s)
                    samples, sr = eng.synth_one(text, name, instruct=None,
                                                language="Chinese", seed=s)
                    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
                    vd, nseg = make_voice.voiced_stats(audio, sr)
                    rate = (len(text) / vd) if vd > 0.1 else float("inf")
                    full = nseg >= make_voice.MIN_SEGMENTS
                    in_win = rate_min <= rate <= rate_max
                    ok = full and in_win
                    log("  %s take%d（%s案）校验 第%d次：有声%.2fs 语速%.2f字/s 段%d → %s"
                        % (name, take_no, text_role, attempts, vd, rate, nseg,
                           "PASS" if ok else ("窗内未落" if full else "FAIL")))
                    if ok:
                        break
                    if full:
                        dist = (0.0 if in_win else
                                (rate_min - rate if rate < rate_min
                                 else rate - rate_max))
                        if best is None or dist < best[0]:
                            best = (dist, s, attempts, audio, sr, samples,
                                    vd, nseg, rate)
                    if attempts >= max_attempts:
                        if best is None:
                            raise RuntimeError(
                                "%s take%d（%s案）连试 %d 次都没念全（段数 ≥%d）。"
                                % (name, take_no, text_role, attempts,
                                   make_voice.MIN_SEGMENTS))
                        (_, s, attempts, audio, sr, samples, vd, nseg,
                         rate) = best
                        log("  %s take%d（%s案）语速窗 [%.2f, %.2f] 未落上，"
                            "取念全里最近的 %.2f 字/s —— 试听页红标，人耳裁决。"
                            % (name, take_no, text_role, rate_min, rate_max,
                               rate))
                        break
                    s += 7
                    # 重试 +7 不会越出本 take 的 [seed0+take_no*1000,
                    # seed0+take_no*1000+112) 区间，与相邻 take 永不重合。
                wav_bytes = serve.to_wav_bytes(samples, sr)
                # 入库即归一：take 落盘前统一响度（-23 dBFS / 峰值 ≤ -1.5 dBFS），
                # 试听页听到的、日后认领上场的，都是这一份 —— 链路上不再有
                # 第二处响度逻辑。
                wav_bytes, loud_gain, loud_rms = make_voice.normalize_wav_bytes(
                    wav_bytes)
                with open(wav_path, "wb") as fh:
                    fh.write(wav_bytes)
                out_win = not (rate_min <= rate <= rate_max)
                takes_rec.append({
                    "take": take_no, "text_role": text_role, "text": text,
                    "seed": s, "attempts": attempts,
                    "seconds": round(len(audio) / float(sr), 2),
                    "voiced_seconds": round(vd, 2),
                    "speech_rate": round(rate, 2),
                    "rate_out_of_window": out_win,
                    "rate_window": [round(rate_min, 2), round(rate_max, 2)],
                    "f0_med": round(make_voice.f0_med(audio, sr), 1),
                    "sha16": wav_sha16(wav_path),
                    "loudness_gain_db": loud_gain,
                    "loudness_rms_dbfs": round(loud_rms + loud_gain, 2),
                })
                log("  %s take%d（%s案）定稿 · %.2fs · F0 %s%s"
                    % (name, take_no, text_role, takes_rec[-1]["seconds"],
                       takes_rec[-1]["f0_med"], " · 超窗" if out_win else ""))
        profile = {
            "schema": 2,
            "kind": "official_preset",
            "builtin_voice": name,
            "language_tag": meta["language"],
            "desc": meta["desc"],
            "text": OFFICIAL_TEXT,
            "texts": dict(TEXT_ROLES),
            "rate_window": [round(rate_min, 2), round(rate_max, 2)],
            "model": resolved,
            "device": str(eng.device or ""),
            "backend": eng.backend,
            "takes": takes_rec,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # chosen 只保留**没被本轮重录**的 take —— 重录的波形变了，旧人耳结论
        # 作废，回到未定稿（旧实现按「take 号还在」保留，force 重录同号会带上
        # 过期结论，这里按 re_takes 精确作废）。旧数据可能是单选 int，统一成列表。
        old_chosen = (prev or {}).get("chosen")
        if isinstance(old_chosen, int):
            old_chosen = [old_chosen]
        keep = [t for t in (old_chosen or [])
                if t in [x.get("take") for x in takes_rec]
                and t not in re_takes]
        if keep:
            profile["chosen"] = sorted(keep)
        tmp = os.path.join(vdir, "profile.json.tmp")
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(profile, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(vdir, "profile.json"))
        out["voices"][name] = profile
    return out


def load_all() -> list:
    """读现有官方档案，试听页与 --status 共用。"""
    out = []
    if not os.path.isdir(OUT_DIR):
        return out
    for meta in serve.SPEAKERS:
        name = meta["name"]
        p = os.path.join(OUT_DIR, name, "profile.json")
        if not os.path.isfile(p):
            out.append({"name": name, "language": meta["language"],
                        "desc": meta["desc"], "missing": True})
            continue
        try:
            with io.open(p, encoding="utf-8") as fh:
                prof = json.load(fh)
        except Exception:  # noqa: BLE001
            out.append({"name": name, "language": meta["language"],
                        "desc": meta["desc"], "missing": True})
            continue
        prof.setdefault("name", name)
        prof.setdefault("language", meta["language"])
        prof.setdefault("desc", meta["desc"])
        out.append(prof)
    return out


# --------------------------------------------------------------------- 试听页
_PAGE_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>官方基准音色试听 · Qwen3-TTS 九预设</title>
<style>
:root{--bg:#0d1117;--panel:#151b23;--panel2:#1c232c;--line:rgba(255,255,255,.08);
--fg:#e6edf3;--fg2:#9aa4af;--fg3:#6b7681;--gold:#c9a45c;--red:#e5534b;--green:#3fb950}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.7 "Microsoft YaHei","PingFang SC",sans-serif;padding:28px 20px 80px}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:17px;font-weight:500;margin:0 0 4px}
h1 b{color:var(--gold);font-weight:500}
.note{color:var(--fg2);font-size:12.5px;margin:0 0 22px;line-height:1.8}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:14px}
.card h2{font-size:14.5px;font-weight:500;margin:0 0 2px}
.card .zh{color:var(--fg3);font-size:12px;margin-bottom:8px}
.take{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
padding:10px 12px;margin-top:8px}
.take .meta{color:var(--fg2);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;
margin-bottom:6px}
audio{width:100%;height:36px}
.pick{display:flex;gap:14px;margin-top:8px;font-size:12.5px;color:var(--fg2)}
.pick label{cursor:pointer;display:flex;align-items:center;gap:5px}
.pick input{accent-color:var(--gold)}
.card textarea{width:100%;margin-top:8px;background:var(--bg);color:var(--fg);
border:1px solid var(--line);border-radius:6px;padding:7px 9px;font-size:12.5px;
resize:vertical;min-height:34px}
.missing{color:var(--fg3);font-size:12.5px;font-style:italic}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);
border-top:1px solid var(--line);padding:10px 20px;display:flex;gap:12px;
align-items:center;justify-content:center}
button{background:var(--gold);color:#14100a;border:0;border-radius:7px;
padding:9px 22px;font-size:13.5px;cursor:pointer;font-weight:500}
#fb{position:fixed;inset:auto 20px 70px 20px;max-height:46vh;overflow:auto;
background:var(--panel2);border:1px solid var(--gold);border-radius:9px;
padding:12px 14px;font-size:12.5px;white-space:pre-wrap;display:none;z-index:9}
.mono{font-family:Consolas,monospace;color:var(--fg3);font-size:11px}
.gold{color:var(--gold)}
.red{color:var(--red)}
</style>
</head>
<body>
<div class="wrap">
<h1>官方基准音色试听 · <b>Qwen3-TTS 9 预设 × A/B 双案 × 每案 __TAKES__ take</b></h1>
<p class="note">每个音色念的是 A/B 两条定稿文案（混合三句型：陈述 + 疑问 + 惊叹，
外加引号转述与省略号拖停）：<br>
<b>__TEXT__</b><br>
听的时候按「整期都要用这个人的声音」的标准挑：声音本身讨不讨喜、各案 take
哪条更稳、念得自不自然。选完点底部「生成反馈清单」，把弹出的文本复制回来即可。
挑选只定官方档案用哪条，不满意的可以直接标「废，重录」。</p>
__CARDS__
</div>
<div id="fb"></div>
<div class="bar"><button onclick="feedback()">生成反馈清单（复制给 AI）</button></div>
<script>
function feedback(){
  const out=[];
  document.querySelectorAll('.card').forEach(c=>{
    const name=c.dataset.name;
    const picks=[...c.querySelectorAll('input[name="p_'+name+'"]:checked')]
      .map(x=>x.value);
    const note=(c.querySelector('textarea')||{}).value||'';
    const parts=[name+': '+(picks.length?picks.join('、'):'未选')];
    if(note.trim())parts.push('备注：'+note.trim());
    out.push(parts.join(' | '));
  });
  const box=document.getElementById('fb');
  box.textContent=out.join('\\n');
  box.style.display='block';
  box.scrollIntoView({behavior:'smooth',block:'end'});
  try{navigator.clipboard.writeText(box.textContent);}catch(e){}
}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------- 定稿
def apply_picks(picks, log=print) -> int:
    """把人耳认可的 take 写进 profile.json 的 chosen 字段。

    picked 是 --pick 收集来的「音色名:takeN」列表。chosen 是**列表**——
    同一音色可以有多条人耳过关的 take（都是典型样本），官方档案分发时从
    里面选；一条都不选（空/缺字段）视为未定稿，分发环节必须拒绝。
    """
    changed = 0
    for item in picks:
        name, _, take_s = str(item).partition(":")
        name, take_s = name.strip(), take_s.strip().lower()
        take_s = take_s[4:] if take_s.startswith("take") else take_s
        if not name or not take_s.isdigit():
            raise RuntimeError("--pick 格式应为 音色名:takeN，收到 %r。" % item)
        take = int(take_s)
        prof_path = os.path.join(OUT_DIR, name, "profile.json")
        if not os.path.isfile(prof_path):
            raise RuntimeError("%s 还没有档案，先录再挑。" % name)
        prof = json.load(io.open(prof_path, encoding="utf-8"))
        have = [t.get("take") for t in prof.get("takes", [])]
        if take not in have:
            raise RuntimeError("%s 没有 take%d（现有 take：%s）。"
                               % (name, take, have or "无"))
        cur = prof.get("chosen") or []
        if isinstance(cur, int):        # 兼容旧单选数据
            cur = [cur]
        cur = [t for t in cur if t in have]
        if take in cur:
            log("  %s take%d 已在定稿，不动" % (name, take))
            continue
        cur.append(take)
        prof["chosen"] = sorted(cur)
        tmp = prof_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(prof, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, prof_path)
        log("  %s → take%s 定稿" % (name, "+".join(map(str, prof["chosen"]))))
        changed += 1
    return changed


def build_html(log=print) -> str:
    """把现有官方档案拼成单文件试听页（base64 内嵌，离线可开）。"""
    profiles = load_all()
    os.makedirs(OUT_DIR, exist_ok=True)
    cards = []
    for prof in profiles:
        name = prof["name"]
        rows = ""
        if prof.get("missing"):
            rows = '<div class="missing">还没有音频 —— 跑 record_official_presets.py 录制。</div>'
        else:
            chosen = prof.get("chosen") or []
            if isinstance(chosen, int):
                chosen = [chosen]
            opts = []
            for t in prof.get("takes", []):
                i = t.get("take")
                wav = os.path.join(OUT_DIR, name, "take%s.wav" % i)
                if not os.path.isfile(wav):
                    continue
                with open(wav, "rb") as fh:
                    b64 = base64.b64encode(fh.read()).decode("ascii")
                meta = []
                tr = t.get("text_role")
                if t.get("rate_out_of_window"):
                    meta.append('<b class="red">超窗</b>')
                if t.get("seconds"):
                    meta.append("%.2fs" % t["seconds"])
                if t.get("f0_med"):
                    meta.append("F0 %s Hz" % t["f0_med"])
                if t.get("speech_rate"):
                    meta.append("语速 %s 字/s" % t["speech_rate"])
                if t.get("attempts") and t["attempts"] > 1:
                    meta.append("重念 %s 次" % (t["attempts"] - 1))
                meta.append('<span class="mono">sha %s</span>' % t.get("sha16", "?"))
                badge = ('<b class="gold">定稿</b> · ' if i in chosen else "")
                role_tag = ('<b class="gold">%s案</b> · ' % tr) if tr else ""
                ck = " checked" if i in chosen else ""
                rows += ('<div class="take"><div class="meta">%s%s<b>take%d</b> · %s</div>'
                         '<audio controls preload="none" src="data:audio/wav;base64,%s">'
                         '</audio>'
                         '<div class="pick">'
                         '<label><input type="checkbox" name="p_%s" value="take%d 选这条"'
                         '%s>要</label>'
                         '<label><input type="checkbox" name="p_%s" value="take%d 不行">不行</label>'
                         '</div></div>'
                         % (badge, role_tag, i, " · ".join(meta), b64, name, i, ck, name, i))
                opts.append("take%d" % i)
            if not opts:
                rows = '<div class="missing">档案在但音频文件缺失，重跑脚本补。</div>'
        cards.append(
            '<div class="card" data-name="%s"><h2>%s</h2>'
            '<div class="zh">%s · %s</div>%s'
            '<textarea placeholder="备注（可选）：哪里不对、想要什么感觉……"></textarea></div>'
            % (name, name, prof.get("language", ""), prof.get("desc", ""), rows))
    texts_html = "<br>".join(
        "<b>%s案</b> %s" % (r, t) for r, t in TEXT_ROLES)
    html = (_PAGE_TMPL
            .replace("__TAKES__", str(TAKES))
            .replace("__TEXT__", texts_html)
            .replace("__CARDS__", "\n".join(cards)))
    page = os.path.join(OUT_DIR, "官方基准音色试听.html")
    with io.open(page, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    log("试听页已生成：%s（%.1f MB）"
        % (page, os.path.getsize(page) / 1048576.0))
    return page


def normalize_archive(log=print) -> int:
    """存量回填入口：实现与账本维护都在 make_voice.normalize_official_archive。"""
    return make_voice.normalize_official_archive(OUT_DIR, log=log)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="官方基准音色：预录 9 预设的三句混合参考音频 + 试听页")
    ap.add_argument("--voices", default="",
                    help="只录这些音色（逗号分隔，如 Serena,Uncle_Fu；默认全部）")
    ap.add_argument("--takes", type=int, default=TAKES,
                    help="每条文案录几条 take（A 案 take1–N、B 案 takeN+1–2N，"
                         "默认 %d）" % TAKES)
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--rate-min", type=float, default=None,
                    help="语速窗下限（默认沿用 make_voice.RATE_MIN=%.1f）"
                         % make_voice.RATE_MIN)
    ap.add_argument("--rate-max", type=float, default=None,
                    help="语速窗上限（默认沿用 make_voice.RATE_MAX=%.1f）"
                         % make_voice.RATE_MAX)
    ap.add_argument("--max-attempts", type=int, default=None,
                    help="每条 take 最多试念几次（默认 make_voice.MAX_ATTEMPTS"
                         "=%d）" % make_voice.MAX_ATTEMPTS)
    ap.add_argument("--force", action="store_true", help="已有 take 也重录")
    ap.add_argument("--pick", action="append", default=[], metavar="音色:takeN",
                    help="人耳定稿：把选中的 take 写进 profile.json（可多次出现，"
                         "如 --pick Vivian:take1 --pick Ryan:take2）")
    ap.add_argument("--html-only", action="store_true",
                    help="不录音，只用现有档案重建试听页")
    ap.add_argument("--status", action="store_true", help="只打印现状")
    ap.add_argument("--normalize", action="store_true",
                    help="存量回填：把已有 take 统一归一到目标响度"
                         "（-23 dBFS RMS，峰值 ≤ -1.5 dBFS），重算 sha16；"
                         "不录音、不加载引擎")
    args = ap.parse_args()

    if args.normalize:
        return normalize_archive(log=print)

    if args.status:
        for prof in load_all():
            if prof.get("missing"):
                print("  %-10s 没有档案" % prof["name"])
                continue
            ch = prof.get("chosen") or []
            if isinstance(ch, int):
                ch = [ch]
            line = "  %-10s 定稿 take%s" % (prof["name"],
                                            "+".join(map(str, ch)) if ch else "—")
            for t in prof.get("takes", []):
                star = " ★" if t.get("take") in ch else ""
                line += "  take%d %ss F0 %s%s" % (t.get("take"), t.get("seconds"),
                                                  t.get("f0_med"), star)
            print(line)
        return 0

    if not args.html_only:
        voices = [v.strip() for v in args.voices.split(",") if v.strip()]
        # 只挑不定录（有 --pick 且没给录音范围）就不必加载引擎。
        pick_only = bool(args.pick) and not voices and not args.force
        if not pick_only:
            record(voices, args.takes, args.device, args.force, log=print,
                   rate_min=args.rate_min, rate_max=args.rate_max,
                   max_attempts=args.max_attempts)
    if args.pick:
        apply_picks(args.pick, log=print)
    build_html()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
