#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一件事：真实模型 + 真实长提示词，第一个字要等多久。

这个数是「静默超时」该定多大的唯一依据。定小了，模型还在老老实实预填充就被
判成卡死；定大了，后端真崩了要等很久才知道。用真模型真素材量一次，不拍脑袋。
"""
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from podcast_maker import paradigms, script_engine as SE          # noqa: E402
from podcast_maker.config_manager import ConfigManager, resolve_base_url  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
cfg = ConfigManager().data()

material = io.open(os.path.join(ROOT, "projects", "20260912-235659",
                                "素材", "s1.md"), encoding="utf-8").read()[:20263]
project = {"paradigm": "methodology", "episode_no": "2", "title": "试"}
card = SE.resolve_paradigm(project, cfg)
system = SE.build_system_prompt(cfg, cfg.get("script.style_preset", "science"),
                                6024, 335, project=project, paradigm=card)
user = SE.build_user_prompt(material, cfg, "")

base = (resolve_base_url(cfg) or "").rstrip("/")
if not base.endswith("/v1"):
    base += "/v1"
payload = {
    "model": cfg.get("llm.model"),
    "messages": [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
    "temperature": 0.8, "max_tokens": 64, "stream": True,
    "stream_options": {"include_usage": True},
}
print("模型      :", payload["model"])
print("提示词字数: system %d + user %d = %d"
      % (len(system), len(user), len(system) + len(user)), flush=True)

req = urllib.request.Request(base + "/chat/completions",
                             data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json"},
                             method="POST")
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=1800) as resp:
        print("响应头到达 : %.1f 秒" % (time.time() - t0), flush=True)
        n = 0
        for line in resp:
            n += 1
            if n <= 3 or b"[DONE]" in line:
                print("第 %-3d 行  %7.1f 秒  %s"
                      % (n, time.time() - t0, line.decode("utf-8", "replace").strip()[:90]),
                      flush=True)
            if b"[DONE]" in line or n >= 60:
                break
except Exception as e:                                            # noqa: BLE001
    print("失败（%.1f 秒）：%s" % (time.time() - t0, e), flush=True)
print("总耗时    : %.1f 秒" % (time.time() - t0))
