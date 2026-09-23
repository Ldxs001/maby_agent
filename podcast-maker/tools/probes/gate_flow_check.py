# -*- coding: utf-8 -*-
"""端到端走一次 generate()：验证 soft 项不阻断、回灌带上上一版正文。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from podcast_maker import duration_model as dm
from podcast_maker import script_engine as se
from podcast_maker.config_manager import ConfigManager


class FakeLLM:
    """第一轮交一份带禁用词的稿子，第二轮把它改掉。"""

    def __init__(self):
        self.calls = []
        self.seen_prev = False
        self.seen_sub = False

    def chat(self, msgs, **kw):
        self.calls.append(msgs)
        user = msgs[-1]["content"]
        if "上一版正文" in user:
            self.seen_prev = True
        if "换成「往往」" in user:
            self.seen_sub = True
        n = 30
        lines = [{"speaker": "A" if i % 2 == 0 else "B", "text": "中" * 10,
                  "emotion": "平静"} for i in range(n)]
        if len(self.calls) == 1:
            lines[3]["text"] = "所有" + "中" * 8      # 故意踩一个
        return json.dumps({"title": "验收", "planned_episodes": 0,
                           "lines": lines}, ensure_ascii=False), {}


cfg = ConfigManager().data()
cfg["project.program_name"] = "播客"
cfg["script.target_minutes"] = 10.0
calib = dm.Calibration(path=os.path.join(os.path.dirname(__file__), "no_calib.json"))

llm = FakeLLM()
logs = []
res = se.generate("素材正文。", cfg, calib, llm, log=logs.append)

print("=== 流程日志 ===")
print("\n".join(logs))
print()
print("=== 结论 ===")
print("轮次:", len(llm.calls))
print("回灌里带了上一版正文:", llm.seen_prev)
print("回灌里带了替换说法:", llm.seen_sub)
print("门禁 passed:", res["report"]["passed"])
print("fails:", [i["key"] for i in res["report"]["fails"]])
print("warns:", [i["key"] for i in res["report"]["warns"]])
print("soft :", [(i["key"], i["detail"][:60]) for i in res["report"]["soft"]])
