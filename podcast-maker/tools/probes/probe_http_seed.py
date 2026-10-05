# -*- coding: utf-8 -*-
"""HTTP 层验收：随机种子与语气指令是否真的落到服务上。

问题：日志里记了 seed、代码里设了种子，但请求经过 HTTP 之后还成立吗？
这里就发两次一模一样的请求，比返回的 WAV 字节 —— 逐字节相同才算数。

另外顺手确认语篇功能标签（如「过渡」）不会再拼出语气指令。
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


base = {"text": TEXT, "voice": "Vivian", "speed": 1.0}

print("第一次请求（会触发模型加载，慢）…", flush=True)
a = post(dict(base, emotion="好奇"))
print("  拿到 %d 字节，md5 %s" % (len(a), hashlib.md5(a).hexdigest()), flush=True)

print("第二次请求（同文本同音色同情绪，不同进程内的另一次调用）…", flush=True)
b = post(dict(base, emotion="好奇"))
print("  拿到 %d 字节，md5 %s" % (len(b), hashlib.md5(b).hexdigest()), flush=True)

print()
print("同请求两次逐字节相同 : %s" % ("是" if a == b else "否"))
if a != b:
    n = min(len(a), len(b))
    diff = sum(1 for i in range(n) if a[i] != b[i])
    print("  长度 %d vs %d，前 %d 字节里有 %d 处不同" % (len(a), len(b), n, diff))

print()
print("语篇功能标签「过渡」发一次（应正常出声，但日志里不该出现语气指令）…", flush=True)
c = post(dict(base, emotion="过渡"))
print("  拿到 %d 字节" % len(c), flush=True)

print()
print("换一个字，种子应当变（波形也应当变）…", flush=True)
d = post({"text": TEXT + "。", "voice": "Vivian", "speed": 1.0, "emotion": "好奇"})
print("  md5 %s  与原句不同: %s" % (hashlib.md5(d).hexdigest(),
                                    "是" if d != a else "否"), flush=True)
print("\n完成", flush=True)
