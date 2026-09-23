#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""量一量「类型判定」那一次调用关掉思考会怎样。

判定要回答的是「哪一类素材、哪一层是切分单位」，两个都能从清单里看出来。
实测这一次调用要 8 分钟——每份素材都要花一次。这里看关掉思考后答案是否还稳。

跑法：python tools/probes/classify_cost.py
"""

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import config_manager, paradigms, probe                 # noqa: E402

CFG = {"script.target_minutes": 25.0, "script.map_head_chars": 280,
       "script.map_max_episodes": 60}

BOOK = """# 第一篇 怎么看

这一篇讲的是看事情的方法。

## 第一章 链与两头
每个判断都有两头的约束，中间是链。

## 第二章 位置比名字重要
名字会撞，位置不会。

# 第二篇 怎么做

这一篇讲落地。

## 第一章 先读结构
结构在稿子里，不在模型脑子里。

## 第二章 小结
把每一步的产物落盘。

# 第三篇 怎么不出错

## 第一章 小结
失败要响，不要静默。

# 附录 A

## 甲表
表格内容。
"""


class Grab(object):
    """把探查的提示词原样截下来，不发请求。"""

    def __init__(self):
        self.messages = None

    def chat(self, messages, **kw):
        self.messages = messages
        raise RuntimeError("截住了")


def send(url, model, messages, effort, label):
    payload = {"model": model, "messages": messages, "temperature": 0.1,
               "max_tokens": int(CFG.get("script.probe_max_tokens", 8192) or 8192),
               "stream": False,
               "response_format": {"type": "json_schema",
                                   "json_schema": {"name": "podcast_script",
                                                   "strict": False,
                                                   "schema": probe.PROBE_SCHEMA}}}
    if effort:
        payload["reasoning_effort"] = effort
    req = urllib.request.Request(
        (url if url.endswith("/v1") else url + "/v1") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=3600) as r:
        data = json.loads(r.read().decode("utf-8"))
    dt = time.time() - t0
    u = data.get("usage") or {}
    rt = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0)
    text = (data["choices"][0]["message"] or {}).get("content") or ""
    print("%-12s %6.1f 秒 | 推理 %5d | %s"
          % (label, dt, rt, text.strip().replace("\n", " ")[:150]))


def main():
    cfg = config_manager.ConfigManager().data()
    ir = probe.scan(BOOK)
    grab = Grab()
    try:
        probe.classify(ir, grab, CFG, paradigms.PARADIGMS)
    except RuntimeError:
        pass
    if not grab.messages:
        print("没能截到探查的提示词")
        return 1
    url = config_manager.resolve_base_url(cfg)
    model = cfg.get("llm.model", "")
    print("后端 %s / 模型 %s\n" % (url, model))
    send(url, model, grab.messages, None, "思考（默认）")
    send(url, model, grab.messages, "none", "关掉思考")
    return 0


if __name__ == "__main__":
    sys.exit(main())
