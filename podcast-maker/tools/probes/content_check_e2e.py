#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端实测：内容检是否真的挂在生成回路上。

看三件事：
1. 生成过程中内容检被调用了几次（每轮一次）
2. 报告里出现两项内容检条目（语义检 / 承诺链检），且带三态结论
3. 报告带 content_checked 标记（合成阶段据此决定要不要补检）

真调本地后端。为了让轮次可控，目标时长压到两分钟。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from podcast_maker import duration_model, script_engine as SE      # noqa: E402
from podcast_maker.config_manager import ConfigManager             # noqa: E402
from podcast_maker.web_ui import make_llm                          # noqa: E402

MATERIAL = """人工智能体的记忆机制可以分成三类。第一类是上下文窗口，它把最近的对话
原样保留，优点是保真，缺点是长度一到上限，最早的内容就被挤出去。第二类是外部检索，
把历史存进向量库，需要时按相似度捞回来，优点是容量近乎无限，缺点是捞回来的片段彼此
不连续，读起来像一堆散页。第三类是摘要压缩，让模型把历史写成更短的版本，优点是省，
缺点是每压一次就丢一次细节，压得越多次丢得越狠。三种机制不是替代关系，实践中通常叠加
使用：窗口管最近，检索管久远，摘要管中段。真正难的地方不在存，而在取——取错了，存得
再多也是噪声。"""


def main():
    cfg = ConfigManager().data()
    cfg["script.target_minutes"] = 2.0
    cfg["script.max_llm_rounds"] = 1        # 最多两轮，够看回路
    cfg["script.gate_strict"] = False

    llm = make_llm(cfg)
    calls = {"n": 0}
    real_chat = llm.chat

    def counted(messages, **kw):
        calls["n"] += 1
        sysmsg = (messages[0].get("content") or "")
        kind = "内容检" if "审校" in sysmsg else "生成脚本"
        print("   >> 调用 #%d（%s）" % (calls["n"], kind))
        return real_chat(messages, **kw)

    llm.chat = counted

    print("开始生成（目标 %.1f 分钟，重试上限 %d）" % (
        cfg["script.target_minutes"], cfg["script.max_llm_rounds"]))
    gen = SE.generate(MATERIAL, cfg, duration_model.Calibration(), llm,
                      log=lambda m: print("   " + m))

    rep = gen["report"]
    print("\n== 报告 ==")
    print("passed=%s  content_checked=%s" % (rep.get("passed"),
                                             rep.get("content_checked")))
    for it in rep["items"]:
        if it["key"].startswith("check_"):
            print("   %-18s ok=%-5s advisory=%-5s %s"
                  % (it["key"], it["ok"], bool(it.get("advisory")),
                     it.get("detail", "")[:70]))
    print("pending=%d  fails=%d  warns=%d"
          % (len(rep["pending"]), len(rep["fails"]), len(rep["warns"])))
    print("模型总调用次数=%d（生成 + 内容检）" % calls["n"])


if __name__ == "__main__":
    main()
