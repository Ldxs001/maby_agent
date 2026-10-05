# -*- coding: utf-8 -*-
"""定位：带语气指令（instruct）时生成失控，且同一请求两次不一致。

矩阵只做两件事：
  1. 不带语气，同一个请求发两次 —— 应逐字节相同（种子已证实有效）；
  2. 带语气，同一个请求发两次 —— 看是长度炸了，还是种子不生效。

长度以服务端返回的 WAV 字节数换算（24 kHz / 16-bit / 单声道 → 字节/48000 = 秒）。
"""
import hashlib
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9881").rstrip("/")
TEXT = ("今天我们来聊一个有点意思的话题：为什么有些决定，"
        "明明想清楚了，事后还是会后悔？")


def post(body, timeout=900):
    req = urllib.request.Request(
        BASE + "/tts", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def secs(raw):
    return len(raw) / 48000.0


def run(label, body):
    print("── %s ──" % label, flush=True)
    outs = []
    for k in (1, 2):
        raw = post(body)
        outs.append(raw)
        print("   第%d次  %8.2f 秒  md5 %s" % (k, secs(raw),
                                              hashlib.md5(raw).hexdigest()), flush=True)
    same = outs[0] == outs[1]
    print("   两次相同: %s" % ("是" if same else "否"), flush=True)
    return same


base = {"text": TEXT, "voice": "Vivian", "speed": 1.0}
run("不带语气（纯文本）", dict(base))
run("带语气「用好奇的语气说」", dict(base, emotion="好奇"))
run("带语气，但换成中性动词（对照）", dict(base, instruct="语气自然一些"))
print("\n完成", flush=True)
