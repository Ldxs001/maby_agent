#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输入额度与分批核对的真模型探针。

盯四件事：

1. 额度换算对不对——额度 = 最大输出 × 输入倍率，要求窗口 = 输出 + 额度，
   `quota_note` 应把三个数字都摆出来（不再探窗口，也没有兜底值）。
2. 一期量级的素材（约 2 万字）是不是真的整段喂进去（不再截到前 6000 字）。
3. 依据落在 6000 字**之后**的句子不再被误报，而真编造仍然抓得住。这是核心对照：
   台词直接取自素材 12000 / 16000 字处的原文，旧口径下必然被记成「素材里没有」。
4. 素材超过额度时会不会分批：10 万字素材按额度一批，应切出多批且不报误。

跑法：python tools/probes/probe_budget.py
"""

import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import script_engine as S                      # noqa: E402
from podcast_maker.config_manager import ConfigManager            # noqa: E402
from podcast_maker.llm_client import LLMClient                    # noqa: E402

MODEL = "0gm-1.0-35b-a3b-0427-i1"
SRC = os.path.join(ROOT, "PROTOCOL.md")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class Recorder(object):
    """把每次调用记下来（提示词字符数、耗时、用量），再转给真客户端。

    额度相关的方法原样透传：内容检要按 `budget_chars` 切批，挡在这一层后面就
    等于探针测的不是生产那条路。
    """

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def chat(self, messages, **kw):
        n = sum(len(m.get("content") or "") for m in messages)
        t0 = time.time()
        text, meta = self.inner.chat(messages, **kw)
        self.calls.append({"chars": n, "sec": round(time.time() - t0, 1),
                           "usage": (meta or {}).get("usage") or {}})
        return text, meta

    def budget_chars(self, *a, **k):
        return self.inner.budget_chars(*a, **k)

    def input_tokens(self, *a, **k):
        return self.inner.input_tokens(*a, **k)

    def required_context(self, *a, **k):
        return self.inner.required_context(*a, **k)

    def quota_note(self, *a, **k):
        return self.inner.quota_note(*a, **k)

    def tokens_per_char(self):
        return self.inner.tokens_per_char()


def slice_of(text, at, width=34):
    """取素材某个位置附近的一小段，去掉换行与标记线，作为台词原文。"""
    seg = text[at:at + 200].replace("\n", "").replace("#", "").strip()
    return seg[:width]


def main():
    cfg = ConfigManager().data()
    llm = Recorder(LLMClient(backend="lm-studio",
                             base_url="http://127.0.0.1:1234/v1",
                             model=MODEL, timeout=7200, idle_timeout=600,
                             input_ratio=cfg.get("llm.input_ratio", 1.0)))
    text = io.open(SRC, encoding="utf-8").read()
    print("素材源：%s（全 %d 字）" % (os.path.basename(SRC), len(text)))

    out_tokens = int(cfg.get("llm.max_tokens", 8192))
    print("额度自查：%s" % llm.quota_note(out_tokens))
    budget = llm.budget_chars(out_tokens, other_chars=3000)
    print("素材额度：%d 字（输出 %d × 倍率 %.1f，扣估算余量，再扣模板 3000）"
          % (budget, out_tokens, llm.input_ratio))
    print("折合比：%s（无样本时 1.0，最保守）" % llm.tokens_per_char())

    # ---- 用例一：一期量级素材（2 万字），台词取自 6000 字之后 --------------
    material = text[:20000]
    script = [
        {"speaker": "A", "text": "欢迎收听《播客》，今天聊一聊输入预算。",
         "emotion": "平稳"},
        {"speaker": "B", "text": slice_of(material, 12000), "emotion": "平稳"},
        {"speaker": "A", "text": slice_of(material, 16000), "emotion": "平稳"},
        {"speaker": "B", "text": "据 2024 年一项统计，这类结构让故障率下降 37%。",
         "emotion": "平稳"},
        {"speaker": "A", "text": "这里是《播客》，欢迎关注。", "emotion": "平稳"},
    ]
    print("\n[用例一] 素材 20000 字，其中两句台词取自 12000 / 16000 字处")
    print("  第 2 句：%s" % script[1]["text"])
    print("  第 3 句：%s" % script[2]["text"])
    n0 = len(llm.calls)
    rep = S.gate_check6(script, cfg, llm=llm, material=material)
    for it in rep["items"]:
        print("  %s → ok=%s：%s" % (it["label"], it["ok"], it["detail"]))
        for iss in (it.get("issues") or []):
            print("      第 %s 句 / %s" % (iss["line"] or "未指出",
                                          iss["quote"][:30]))
    for c in llm.calls[n0:]:
        print("  调用：提示词 %d 字，耗时 %.1fs，用量 %s"
              % (c["chars"], c["sec"], c["usage"].get("prompt_tokens") or "未回"))

    # ---- 用例二：多文档拼成约 10 万字素材，逼出分批 ------------------------
    # 折合比在第一轮之后已经由真实用量标定（本例实测约 0.55），预算跟着变大，
    # 所以要真超过它，素材得比 6 万字更大。拼接而不是重复：重复文本会让模型
    # 先去报「素材是重复的」，那会把这一条测偏。
    parts = []
    for name in ("README.md", "PROTOCOL.md", "PLAN.md", "CHANGELOG.md"):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            parts.append(io.open(p, encoding="utf-8").read())
    big = "\n\n".join(parts)
    need = 100000
    if len(big) < need:
        big = (big + "\n\n") * (need // max(1, len(big)) + 1)
        big = big[:need]
    near = max(0, len(big) - 3000)
    script2 = [
        {"speaker": "A", "text": "欢迎收听《播客》，这一期讲分批核对。",
         "emotion": "平稳"},
        {"speaker": "B", "text": slice_of(big, near), "emotion": "平稳"},
        {"speaker": "A", "text": "这里是《播客》，欢迎关注。", "emotion": "平稳"},
    ]
    print("\n[用例二] 素材 %d 字（超预算），第 2 句取自末尾 %d 字处"
          % (len(big), near))
    b = llm.budget_chars(int(cfg.get("llm.max_tokens", 8192)), other_chars=2000)
    print("  本次预算约 %d 字（折合比 %.2f）→ 预计 %d 批"
          % (b, llm.tokens_per_char(), len(S.split_material(big, max(1, b)))))
    n1 = len(llm.calls)
    rep2 = S.gate_check6(script2, cfg, llm=llm, material=big)
    for it in rep2["items"]:
        print("  %s → ok=%s：%s" % (it["label"], it["ok"], it["detail"]))
    calls = llm.calls[n1:]
    print("  实际调用 %d 次（分批数）" % len(calls))
    for i, c in enumerate(calls, 1):
        print("    第 %d 批：提示词 %d 字，耗时 %.1fs，prompt_tokens=%s"
              % (i, c["chars"], c["sec"],
                 c["usage"].get("prompt_tokens") or "未回"))

    out = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"),
                       "_probe_budget.json")
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"quota_note": llm.quota_note(out_tokens), "budget": budget,
                   "case1": rep, "case2": rep2,
                   "calls": llm.calls}, f, ensure_ascii=False, indent=2)
    print("\n结论已写入 %s" % out)


if __name__ == "__main__":
    main()
