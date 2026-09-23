# -*- coding: utf-8 -*-
"""档位端到端：`degree` 从 HTTP 请求进去，真的改变了音频。

单测能钉住"body 里带了 degree""服务端遇到 light 拼『略带』"，但钉不住
**这条链真的接通了**——比如字段名在某处写错、或者服务端没读这个键，单测
照样全绿。这里走真服务、发真请求、量真波形。

三组对照（同一句文本、同一音色 → 服务端种子相同，唯一变量是语气指令）：
  A. emotion=感慨            → 用感慨的语气说
  B. emotion=感慨 degree=light → 用略带感慨的语气说
  C. 不带 emotion            → 无指令

判据：A 与 B 的波形必须不同（否则 degree 没进到措辞）；C 与 A 也应不同
（否则 instruct 根本没生效）。差异按时长、F0 波动、逐字节是否相同三条看。

跑法：先起服务，再 python tools/probes/probe_degree_e2e.py
"""
import hashlib
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"))

import probe_emotion_instruct as P                             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "_smoke", "_degree_probe")
URL = "http://127.0.0.1:9880/tts"
TEXT = "这件事我们等了很多年，今天终于有了一个结果。"
VOICE = "Serena"


def call(tag, out_path, emotion=None, degree=None):
    body = {"text": TEXT, "voice": VOICE, "speed": 1.0}
    if emotion:
        body["emotion"] = emotion
    if degree:
        body["degree"] = degree
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        raw = resp.read()
    with open(out_path, "wb") as f:
        f.write(raw)
    m = dict(P.measure(out_path, len(TEXT)))
    m["tag"] = tag
    m["body"] = body
    m["sha1"] = hashlib.sha1(raw).hexdigest()[:12]
    return m


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    P.wait_health()
    rows = [
        call("A 用感慨的语气说", os.path.join(OUT_DIR, "A_bare.wav"),
             emotion="感慨"),
        call("B 用略带感慨的语气说", os.path.join(OUT_DIR, "B_light.wav"),
             emotion="感慨", degree="light"),
        call("C 不加指令", os.path.join(OUT_DIR, "C_none.wav")),
    ]
    print("\n%-22s %6s %8s %8s %8s  %s"
          % ("档位", "时长", "RMS", "F0均值", "F0波动", "波形指纹"))
    for r in rows:
        print("%-22s %6.2f %8.2f %8.1f %8.1f  %s"
              % (r["tag"], r["dur"], r["rms"], r["f0_mean"], r["f0_std"],
                 r["sha1"]))
    same_ab = rows[0]["sha1"] == rows[1]["sha1"]
    same_ac = rows[0]["sha1"] == rows[2]["sha1"]
    print("\ndegree 改变了输出（A≠B）：%s" % ("否——档位没进到措辞" if same_ab
                                           else "是"))
    print("instruct 本身有效（A≠C）：%s" % ("否——语气指令没生效" if same_ac
                                         else "是"))
    with io.open(os.path.join(OUT_DIR, "degree.json"), "w",
                 encoding="utf-8") as f:
        json.dump({"text": TEXT, "voice": VOICE, "rows": rows},
                  f, ensure_ascii=False, indent=2)
    print("已落盘 %s" % os.path.join(OUT_DIR, "degree.json"))


if __name__ == "__main__":
    main()
