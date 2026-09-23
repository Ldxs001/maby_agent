#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端料源门禁实测：绕过界面直调接口，成稿规划项目必须被拒。

用 Python 发请求而不是 curl：Windows 下 curl 会把中文按本地代码页编码发出去，
服务端按 UTF-8 解直接抛 UnicodeDecodeError——那样测到的是编码，不是门禁。

用法：python backend_gate_check.py [http://127.0.0.1:8813] [项目id]
不传项目 id 时自动挑一个「成稿规划且未排图」的项目。
"""

import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8813").rstrip("/")
PID = sys.argv[2] if len(sys.argv) > 2 else ""


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def main():
    pid = PID
    if not pid:
        rows = get("/api/project")["projects"]
        todo = [p for p in rows
                if (p.get("progress") or {}).get("mode") == "mapped"
                and not (p.get("progress") or {}).get("mapped")]
        if not todo:
            print("库里没有「未排图的成稿规划」项目，跳过")
            return 0
        pid = todo[-1]["id"]
        print("目标项目：%s（%s，未排图）" % (pid, todo[-1].get("name")))

    res = post("/api/script/generate",
               {"project_id": pid, "material": "手填素材，不该被采纳"})
    print("ok    =", res.get("ok"))
    print("error =", res.get("error"))
    print("logs  =", res.get("logs"))
    ok = res.get("ok") is False and "排" in (res.get("error") or "")
    print("\n结果：", "PASS 未排图的成稿规划项目被拒（递了素材也不作数）"
          if ok else "FAIL 没有被拒")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
