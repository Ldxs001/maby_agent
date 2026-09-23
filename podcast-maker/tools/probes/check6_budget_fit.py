#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚焦实测：审校预算从写死的小值改成按全局预算取之后，是否真判得出来。

背景：本机模型以 8192 上下文加载，而 config.json 里 llm.max_tokens 是 48640。
后者是**输出上限**，服务端要校验「输入 + 该上限 ≤ 上下文」，所以照配置原样发请求，
在提示词偏长时会被后端拒（HTTP 400 上下文超限）；提示词短时后端不拒，
但会一路生成到上下文塞满，看起来像挂住。

所以这里只做两件事：
1. 拿出两个数字对照——服务端报的实际加载上下文，与配置里的最大输出；
2. 用能塞进上下文的预算发一次审校，看两项判定是不是真的出来了。

脚本里故意埋两处问题：
- 一处编造：素材里没有任何数字，脚本里出现「百分之七十」
- 一处断链：片头问「哪一种更可靠」，后文从头到尾没回答
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from podcast_maker import script_engine as SE                  # noqa: E402
from podcast_maker.config_manager import ConfigManager         # noqa: E402
from podcast_maker.web_ui import make_llm                      # noqa: E402

MATERIAL = """智能体的记忆机制分三类。窗口保留最近对话，保真，但一到长度上限最早的就被挤出去。
检索把历史存进向量库按相似度捞回，容量近乎无限，但捞回的片段彼此不连续，像一堆散页。
摘要压缩让模型把历史写成更短的版本，省，但每压一次丢一次细节。三者通常叠加使用。
难的地方不在存，而在取。"""

SCRIPT = [
    {"speaker": "A", "emotion": "平和", "text": "欢迎收听《播客》，今天聊智能体的记忆。"},
    {"speaker": "B", "emotion": "好奇", "text": "窗口、检索、压缩这三类里，哪一种更可靠？"},
    {"speaker": "A", "emotion": "解释", "text": "窗口保真，但到长度上限最早的就被挤出去。"},
    {"speaker": "B", "emotion": "追问", "text": "那压缩能保住多少细节，有数据吗？"},
    {"speaker": "A", "emotion": "断言", "text": "实测表明摘要压缩的细节保留率大约是百分之七十。"},
    {"speaker": "B", "emotion": "总结", "text": "真正难的地方不在存而在取，今天就聊到这里。"},
]


def server_context(base):
    """问服务端：这个模型实际以多长上下文加载，上限又是多少。"""
    if not base:
        return None, None, "base_url 为空"
    url = base.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    try:
        with urllib.request.urlopen(url + "/api/v0/models", timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                                   # noqa: BLE001
        return None, None, "读取失败：%s" % str(e)[:100]
    for m in data.get("data", []):
        if m.get("state") == "loaded":
            return (m.get("loaded_context_length"),
                    m.get("max_context_length"), m.get("id"))
    return None, None, "没有已加载的模型"


def main():
    cfg = ConfigManager().data()
    budget = int(cfg.get("llm.max_tokens", 8192))
    llm = make_llm(cfg)
    base = getattr(llm, "base_url", "") or cfg.get("llm.base_url", "")
    print("后端地址      %s" % base)
    print("模型          %s" % cfg.get("llm.model", ""))
    loaded, maxctx, who = server_context(base)
    print("实际加载上下文 %s（模型上限 %s）" % (loaded, maxctx))
    print("配置最大输出   %s" % budget)
    print("→ 该上限落在上下文之外，据此发请求会被拒或一路生成到塞满" if (
        loaded and budget > loaded) else "→ 该上限在上下文之内")

    fit = max(1024, min(budget, (loaded or 8192) - 3500))
    cfg2 = dict(cfg)
    cfg2["llm.max_tokens"] = fit
    llm2 = make_llm(cfg2)
    print("\n[预算取 %d，其余不动，提示词也收到千余字]" % fit, flush=True)
    out = SE.check6_llm(SCRIPT, cfg2, llm2, MATERIAL[:1200])
    print("== 判定结果 ==", flush=True)
    for key, _lab in SE.CHECK6_DIMS:
        state, detail = out[key]
        print("  %-10s -> %-8s %s" % (key, state, detail[:200]), flush=True)


if __name__ == "__main__":
    main()
