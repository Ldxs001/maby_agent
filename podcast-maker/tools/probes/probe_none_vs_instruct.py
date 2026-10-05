# -*- coding: utf-8 -*-
"""档位剪枝后的听感对照探针 · none 档「不发指令」到底改变了什么。

背景
----
`serve.py:build_instruct` 刚改过一刀：`degree == "none"` 时整句不发情绪指令。
改之前，`degree="none"` + `emotion="平静"` 会发出「用平静的语气说」。

本探针与前一个 `_probe_emotion_instruct.py` 的**根本区别**：
    旧探针直接塞 `body["instruct"]`，绕过 `build_instruct`，测的是模型对自由文本的反应；
    本探针只传 `emotion` + `degree`，走**真实产品链路**，测的是这次改动本身的效果。

对照设计（唯一变量 = emotion 与 degree 的组合，种子 = sha256(speaker+text) 与二者无关）
    none_plain    degree=none   + 平静   ← 改动后：不发指令
    old_plain     不传 degree   + 平静   ← 改动前：发「用平静的语气说」
    light_plain   degree=light  + 平静   ← 发「用略带平静的语气说」
    none_emo      degree=none   + 感慨   ← 改动后：不发（真情绪也被档位挡掉）
    old_emo       不传 degree   + 感慨   ← 发「用感慨的语气说」（情绪指令上限参考）

指标
    dur / rate / rms / rms_std / f0_mean / f0_std / centroid

用法
----
    python tools/probes/probe_none_vs_instruct.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"))
from probe_emotion_instruct import measure                        # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
OUT = os.path.join(HERE, "_none_probe")
HOST = "http://127.0.0.1:9881"
SPEAKER = "Serena"

#: 五档 (key, emotion, degree, 说明)。degree=None 表示请求体里不带该字段。
LEVELS = [
    ("none_plain", "平静", "none",  "none 档 + 平静（改动后不发指令）"),
    ("old_plain",  "平静", None,    "不传档位 + 平静（改动前发「用平静的语气说」）"),
    ("light_plain", "平静", "light", "light 档 + 平静（「用略带平静的语气说」）"),
    ("none_emo",   "感慨", "none",  "none 档 + 感慨（真情绪也被挡）"),
    ("old_emo",    "感慨", None,    "不传档位 + 感慨（「用感慨的语气说」）"),
]

#: 三句技术型播客的典型文本（都是中性陈述，不自带情绪）。
TEXTS = [
    ("原理",  "我们先从原理讲起。"),
    ("流程",  "这套流程的核心，是把随机性关到最后一步。"),
    ("定位",  "它不是更聪明的模型，只是把约束写在了代码里。"),
]


def wait_health(timeout=600):
    """等本脚本自己的 HOST（9881）就绪。

    注意：不能复用 `_probe_emotion_instruct.wait_health` —— 那个模块的 HOST 硬编码
    9880，与本脚本的端口不同，会一直探到超时。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(HOST + "/health", timeout=10) as r:
                st = json.loads(r.read().decode("utf-8"))
            if st.get("loaded"):
                return st
            print("  …等待模型加载（%ds）" % int(time.time() - t0), flush=True)
        except Exception:                                   # noqa: BLE001
            pass
        time.sleep(5)
    raise SystemExit("服务未就绪，超时 %ds（HOST=%s）" % (timeout, HOST))


def synth(text, emotion, degree, out_path):
    body = {"text": text, "voice": SPEAKER, "speed": 1.0, "emotion": emotion}
    if degree is not None:
        body["degree"] = degree
    req = urllib.request.Request(
        HOST + "/tts", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as resp:
        raw = resp.read()
    with open(out_path, "wb") as f:
        f.write(raw)
    return round(time.time() - t0, 2), hashlib.md5(raw).hexdigest()[:10]


def main():
    os.makedirs(OUT, exist_ok=True)
    print("等待服务就绪 …", flush=True)
    st = wait_health()
    print("服务就绪：%s / %s\n" % (st.get("device"), st.get("model")))

    rows = []
    total = len(TEXTS) * len(LEVELS)
    n = 0
    for tag, text in TEXTS:
        for key, emo, deg, _desc in LEVELS:
            n += 1
            path = os.path.join(OUT, "%s_%s.wav" % (tag, key))
            try:
                secs, md5 = synth(text, emo, deg, path)
            except urllib.error.HTTPError as e:
                print("[%d/%d] %s/%s 失败：%s" % (n, total, tag, key, e.read()[:200]),
                      flush=True)
                continue
            m = measure(path, len(text))
            m.update({"text_tag": tag, "text": text, "level": key,
                      "emotion": emo, "degree": deg,
                      "gen_secs": secs, "md5": md5,
                      "bytes": os.path.getsize(path)})
            rows.append(m)
            print("[%2d/%d] %-4s %-11s %5.2fs 时长 %5.2f 语速 %5.2f · "
                  "F0 %5.1f±%4.1f · 质心 %6.0f · %s"
                  % (n, total, tag, key, secs, m["dur"], m["rate"],
                     m["f0_mean"], m["f0_std"], m["centroid"], md5), flush=True)

    res = {"speaker": SPEAKER, "host": HOST,
           "levels": [{"key": k, "emotion": e, "degree": d, "desc": ds}
                      for k, e, d, ds in LEVELS],
           "texts": [{"tag": t, "text": x} for t, x in TEXTS],
           "rows": rows}
    with io.open(os.path.join(HERE, "_none_probe.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    # ---- 汇总：以 none_plain（改动后的实际行为）为基准 --------------------- #
    print("\n" + "=" * 100)
    print("相对 none 档（不发指令）的变化")
    print("=" * 100)
    print("%-4s %-11s %6s %7s %7s %8s %8s %8s"
          % ("文本", "档位", "时长", "语速", "F0均", "F0波动", "质心", "字节"))
    for tag, _t in TEXTS:
        base = next((r for r in rows if r["text_tag"] == tag and r["level"] == "none_plain"),
                    None)
        for key, _e, _d, _ds in LEVELS:
            r = next((x for x in rows if x["text_tag"] == tag and x["level"] == key), None)
            if not r:
                continue
            def pct(k):
                if not base:
                    return "      -"
                b = base[k]
                return "%+7.1f" % (100.0 * (r[k] - b) / b) if b else "      -"
            print("%-4s %-11s %6.2f %7.2f %7s %8s %8s %8d"
                  % (tag, key, r["dur"], r["rate"], pct("f0_mean"),
                     pct("f0_std"), pct("centroid"), r["bytes"]))
        print("-" * 100)

    # ---- 关键判定：none 档内部必须完全一致（emotion 填什么都不该有影响）---- #
    print("\n关键判定 A：none 档下 emotion 填「平静」与「感慨」，输出必须逐字节一致")
    for tag, _t in TEXTS:
        a = next((r for r in rows if r["text_tag"] == tag and r["level"] == "none_plain"), None)
        b = next((r for r in rows if r["text_tag"] == tag and r["level"] == "none_emo"), None)
        if a and b:
            print("  %-4s %s  (%s vs %s)" % (tag, "一致" if a["md5"] == b["md5"] else "不一致",
                                             a["md5"], b["md5"]))
    print("\n关键判定 B：不传档位时，平静/感慨应各自出指令（旧行为未被误伤）")
    for tag, _t in TEXTS:
        a = next((r for r in rows if r["text_tag"] == tag and r["level"] == "old_plain"), None)
        b = next((r for r in rows if r["text_tag"] == tag and r["level"] == "old_emo"), None)
        if a and b:
            print("  %-4s 平静 %s / 感慨 %s  → %s"
                  % (tag, a["md5"], b["md5"],
                     "两条不同（指令生效）" if a["md5"] != b["md5"] else "两条相同（异常）"))

    print("\n结果写入 _smoke/_none_probe.json，音频在 _smoke/_none_probe/")


if __name__ == "__main__":
    main()
