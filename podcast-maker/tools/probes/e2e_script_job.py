#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：拿真素材、真模型，走真接口跑一期脚本。

盯的是这条路修好没有——同步接口时它会在一个固定上限上被判死，哪怕模型一直在
吐字。这里从头到尾只看两件事：任务是不是活着、日志是不是在往前走。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8834"
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 2400


def post(path, body):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


material = io.open(os.path.join(ROOT, "projects", "20260912-235659",
                                "素材", "s1.md"), encoding="utf-8").read()[:20263]
print("素材 %d 字 → 起任务" % len(material), flush=True)
t0 = time.time()
r = post("/api/script/generate", {"material": material, "extra": "",
                                  "preset": "science", "project_id": "",
                                  "episode_no": ""})
print("起任务返回：", json.dumps(r, ensure_ascii=False), flush=True)
if not r.get("ok"):
    sys.exit("起任务失败：" + str(r.get("error")))
tid = r["task_id"]

seen = 0
while time.time() - t0 < LIMIT:
    time.sleep(10)
    j = get("/api/task/" + tid).get("job") or {}
    log = j.get("log") or []
    if len(log) != seen:
        for line in log[seen:]:
            print("  %6.0fs  %s" % (time.time() - t0, line), flush=True)
        seen = len(log)
    if j.get("status") != "running":
        print("\n任务结束：status=%s  用时 %.0f 秒" % (j.get("status"), time.time() - t0),
              flush=True)
        res = j.get("result") or {}
        if j.get("status") == "done" and res.get("ok"):
            rep = res.get("report") or {}
            print("  标题   :", res.get("title"))
            print("  句数   :", len(res.get("script") or []))
            print("  门禁   :", "通过" if rep.get("passed") else "未通过")
            print("  估时   :", (res.get("estimate") or {}).get("total_seconds"), "秒")
        else:
            print("  失败原因:", j.get("error") or res.get("error"))
        break
 
else:
    print("到 %d 秒仍在跑（未判失败）" % LIMIT)
