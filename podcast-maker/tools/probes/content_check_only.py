#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚焦实测：两项内容检在真模型上能否一次调用判完。

脚本里故意埋两处问题：
- 一处编造：素材里没有任何数字，脚本里出现「百分之七十」
- 一处断链：片头问「到底是哪一种更可靠」，后文从头到尾没回答

看模型是否一次调用同时给出两项结论，并指出具体是哪一句。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from podcast_maker import script_engine as SE                  # noqa: E402
from podcast_maker.config_manager import ConfigManager         # noqa: E402
from podcast_maker.web_ui import make_llm                      # noqa: E402

MATERIAL = """人工智能体的记忆机制可以分成三类。第一类是上下文窗口，它把最近的对话
原样保留，优点是保真，缺点是长度一到上限，最早的内容就被挤出去。第二类是外部检索，
把历史存进向量库，需要时按相似度捞回来，优点是容量近乎无限，缺点是捞回来的片段彼此
不连续，读起来像一堆散页。第三类是摘要压缩，让模型把历史写成更短的版本，优点是省，
缺点是每压一次就丢一次细节，压得越多次丢得越狠。三种机制不是替代关系，实践中通常叠加
使用：窗口管最近，检索管久远，摘要管中段。真正难的地方不在存，而在取——取错了，存得
再多也是噪声。"""

SCRIPT = [
    {"speaker": "A", "emotion": "平和", "text": "欢迎收听《播客》，今天我们聊聊智能体的记忆。"},
    {"speaker": "B", "emotion": "好奇",
     "text": "记忆机制分三类，窗口、检索、压缩，这三者里到底哪一种更可靠呢？"},
    {"speaker": "A", "emotion": "解释",
     "text": "上下文窗口把最近的对话原样留着，好处是保真，坏处是一到长度上限最早的就被挤出去。"},
    {"speaker": "B", "emotion": "追问", "text": "那外部检索呢，它的好处在哪儿？"},
    {"speaker": "A", "emotion": "解释",
     "text": "检索把历史存进向量库按相似度捞回来，容量近乎无限，但捞回来的片段彼此不连续，像一堆散页。"},
    {"speaker": "B", "emotion": "怀疑", "text": "实测下来压缩能保住多少细节，有数据吗？"},
    {"speaker": "A", "emotion": "断言",
     "text": "实测表明摘要压缩的细节保留率大约是百分之七十，压三次之后基本只剩框架。"},
    {"speaker": "B", "emotion": "总结",
     "text": "所以三者不是替代关系，窗口管最近，检索管久远，摘要管中段。"},
    {"speaker": "A", "emotion": "收束",
     "text": "真正难的地方不在存而在取，今天就聊到这里。"},
    {"speaker": "B", "emotion": "告别", "text": "这里是《播客》，欢迎关注。"},
]


def main():
    cfg = ConfigManager().data()
    llm = make_llm(cfg)
    print("调用一次，问两项……")
    out = SE.check6_llm(SCRIPT, cfg, llm, MATERIAL)
    print("\n== 判定结果 ==")
    for key, _label in SE.CHECK6_DIMS:
        state, detail = out[key]
        print("  %-10s -> %-8s %s" % (key, state, detail[:160]))

    sem = out["semantic"][0]
    pro = out["promise"][0]
    print("\n埋点复核：")
    print("  编造的数字是否被判出（期望 fail）: %s" % sem)
    print("  片头设问未回应是否被判出（期望 fail）: %s" % pro)


if __name__ == "__main__":
    main()
