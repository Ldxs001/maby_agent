#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分段生成（方案 B）的真模型探针：小目标快速走通「规划 → 逐段 → 组装 → 门禁」。

盯四件事：
1. 规划轮：凝缩分组是否覆盖全部节、配额合计是否精确等于目标字数。
2. 段级核账：不足的段会不会按素材插句、超出的段会不会压紧删减、差额会不会滚入下段配额。
3. 组装后门禁照旧：片头尾写死、情绪档位、总时长（软）、内容检照跑。
4. 路线按项目模式给：成稿规划（mapped）走分段，逐期即兴与单集走整篇
   （回归由单元测试覆盖，这里只跑分段态）。

跑法：python tools/probes/probe_segmented.py   （需要 LM Studio 已加载模型）
"""

import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import script_engine as S                      # noqa: E402
from podcast_maker.config_manager import ConfigManager            # noqa: E402
from podcast_maker.llm_client import LLMClient                    # noqa: E402

MODEL = "0gm-1.0-35b-a3b-0427-i1"

EVIDENCE = {
    "gist": "为什么把「先定规范再做活」当作智能体协作的第一原则",
    "points": ["规范前置优于事后验证", "确定性事务归代码"],
    "sections": [
        {"source": "s1", "anchor": "规范的边界",
         "gist": "前置规范划定模型能自由发挥的范围：范围内是创作，范围外是事故。",
         "points": ["规范先行让验收有据可依",
                    "模型只做范围内的事，越界即是缺陷"]},
        {"source": "s2", "anchor": "记账的归属",
         "gist": "编号、配额、合计这类算术必须归代码；模型数不了数，也不该让它数。",
         "points": ["LLM 计数误差随数量级增长",
                    "程序核账是唯一可靠的闭环"]},
        {"source": "s3", "anchor": "最小闭环",
         "gist": "反馈粒度决定漂移幅度：每写一小段就核对一次，误差就地吸收不累积。",
         "points": ["开环一次赌整篇，闭环每步小赌",
                    "差额滚入下一段配额，总账咬住目标"]},
    ],
}

MATERIAL = """规范前置优于事后验证，这是智能体协作里最容易被低估的一条。
很多团队先让模型自由发挥，再拿一堆验收规则去筛，筛出来的问题改一轮坏一轮。
把规范放在前面，模型在写第一个字之前就知道边界在哪里，验收自然有据可依。
确定性的事务必须归代码：编号、配额、合计、去重，这些算术交给模型就是事故。
语言模型数不了数，这不是提示词能修好的缺陷，而是自回归结构的本性。
闭环的反馈粒度决定漂移幅度：一次生成整篇，误差从头累积到尾没人纠。
把整篇切成小段，每段写完立刻核对字数：不足就按素材新增句子插进去，超出就压紧措辞或
把多余的话拿掉，差额滚进下一段的配额。
每一步都在咬住总账，漂移就不再是倍数级的事故，而是段内几句话的扰动。
程序核账是唯一可靠的闭环：模型负责内容，程序负责数量，各干各的。
""" * 3

PROJECT = {"title": "先定规范再做活",
           "gist": EVIDENCE["gist"], "points": EVIDENCE["points"],
           "sources": ["s1", "s2", "s3"], "planned_episodes": 1,
           "episode_no": "1"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    cfg = ConfigManager().data()
    cfg["script.target_minutes"] = 1.0      # 小目标：约 250 字、2~3 段，跑得快
    llm = LLMClient(backend="lm-studio",
                    base_url="http://127.0.0.1:1234/v1",
                    model=MODEL, timeout=7200, idle_timeout=1800,
                    input_ratio=cfg.get("llm.input_ratio", 1.0))
    logs = []
    # 路线按项目模式给：成稿规划（mapped）走分段。这个探针就是为分段写的。
    res = S.generate(MATERIAL, cfg, llm, project=PROJECT, evidence=EVIDENCE,
                     log=logs.append, segmented=True)
    for m in logs:
        print(m)
    lines = res.get("script") or []
    chars = sum(len(l.get("text") or "") for l in lines)
    rep = res.get("report") or {}
    print("\n==== 结果 ====")
    print("标题：%s" % res.get("title"))
    print("整篇：%d 句 / %d 字" % (len(lines), chars))
    print("门禁 passed=%s；fails=%s；pending=%d"
          % (rep.get("passed"),
             [i.get("label") for i in rep.get("fails") or []],
             len(rep.get("pending") or [])))
    out = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke"),
                       "_probe_segmented.json")
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"logs": logs, "title": res.get("title"),
                   "lines": lines, "report": rep},
                  f, ensure_ascii=False, indent=2)
    print("结论已写入 %s" % out)


if __name__ == "__main__":
    main()
