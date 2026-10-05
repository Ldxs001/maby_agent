# -*- coding: utf-8 -*-
"""情绪档位真模型探针：none 档到底管不管得住模型的 emotion 字段。

要验的不是「提示词里有没有那句话」——那单测已经钉死了。要验的是**模型读了那句话
之后真的照办**：给它一段最容易被写成情绪化的素材（小说、故事），挂上一张 none 档
的卡，看它会不会照样给每句填心情词；再挂 light 档，看它会不会填。

对照成立的话，说明"写脚本这一头"由档位说了算；配上单测里那条"档位逐句透传到
服务端"，两头就都对上了。

跑法：python tools/probes/probe_emotion_level.py
产物：_smoke/_probe_emotion_level.json + 控制台逐句打印
"""
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from podcast_maker import paradigms as PG                      # noqa: E402
from podcast_maker import script_engine as S                   # noqa: E402
from podcast_maker.config_manager import ConfigManager, DISCOURSE_VOCAB  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "_smoke", "_probe_emotion_level.json")

MOOD_WORDS = ["好奇", "疑惑", "恍然", "肯定", "感慨", "轻松"]

# ---- 两段素材：写法上都是"情绪浓"的，正好用来试档位管不管得住 --------
STORY = """
那天夜里，陈默在旧仓库门口站了很久。铁门半掩着，里面没有一点灯光。
他手里捏着那把钥匙——这把钥匙原本属于他的父亲，父亲走了七年，
钥匙一次也没有用过。

他推门进去。灰尘在电筒光柱里翻。货架还是当年的样子，第三排最上层，
那只木箱还在。他搬下来，锁扣锈得厉害，撬了两下才开。

箱子里只有一本薄薄的笔记本。扉页上写着一行字：
"如果哪天你想明白了，就把这个烧掉。"

陈默翻到最后一页，日期是七年前的那个雨天。他愣住了——
那一页上只有两个字："对不起。"后面再没有别的。

他坐在箱子上，不知道过了多久。外面开始下雨，雨点打在铁皮顶上，
一下一下，像有人在敲门。

他最终没有烧掉那本笔记本。他把它放进外套内袋，锁好仓库的门，
走进雨里。他想，有些事情留着比烧掉更重。
"""

ESSAY = """
先有规范，后有验证。这句话听起来像一句正确的废话，但它决定了一个系统
能不能自我纠正。

道理并不复杂。验证是在输出之后做的，它只能告诉你"哪里不对"；
规范是在输出之前给的，它决定"什么才算对"。前者是补救，后者是约束。
一个只靠验证的系统，会把大量的算力花在来回返工上——它每次都能发现
自己错了，却总是说不出下次要怎么才对。

这里有一个常见的反例。有人以为，只要把验证做得足够严格，就等价于
把规范做好了。其实不然。验证越严，返工越多，而返工本身是有代价的：
它会把模型已经做对的部分也一起搅动。改一处、坏两处，最后不是收敛，
而是原地打转。

所以正确的顺序是：先把判据前移，让它落在生成的入口上；生成之后再验证，
只用来兜住那些确实漏掉的。两者不是替代关系，是先后关系。

这个道理在工程上有一个直接的推论：能写进结构里的约束，就不要留给
自然语言去劝。结构上的约束是确定的，语言上的劝告是概率的。
"""


def load_llm(cfg):
    # 取程序自己那一处工厂，不在这里另拼一遍参数：客户端那套（窗口、超时、
    # 后端）只有一处来源，探针另拼一份就会与生产跑的不是同一个东西。
    from podcast_maker.web_ui import make_llm
    return make_llm(cfg)


def run_one(tag, material, card_key, cfg, llm, preset):
    card = PG.get(card_key)
    level = PG.emotion_level_of(card)
    system = S.build_system_prompt(cfg, preset, 800, 20, paradigm=card)
    user = S.build_user_prompt(material, cfg, "")
    t0 = time.time()
    raw, meta = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        temperature=0.8,
        max_tokens=int(cfg.get("llm.max_tokens", 8192)),
        json_schema=S.script_schema(level))
    dt = time.time() - t0
    # 解析口径与生产一致：parse_script 失败会抛，normalize_script 收的是 lines
    # 而不是整个对象——这里另写一套宽容解析，量的就不是同一个东西了。
    try:
        script = S.normalize_script(S.parse_script(raw)["lines"], cfg)
    except Exception as e:                                       # noqa: BLE001
        print("\n[%s] 解析失败：%s" % (tag, e))
        return {"tag": tag, "card": card_key, "level": level,
                "seconds": round(dt, 1), "error": str(e), "raw_head": raw[:200]}
    emos = [s.get("emotion") for s in script]
    mood = [e for e in emos if e in MOOD_WORDS]
    outside = [e for e in emos if e not in S.EMOTION_TAGS]
    rep = S.gate_generate(script, cfg, card)
    gate = {i["key"]: {"ok": i["ok"], "detail": i["detail"]}
            for i in rep["items"] if i["key"] in ("emotion_level", "emotion_vocab")}
    print("\n[%s] 卡=%s 档位=%s  用时 %.0fs  行数 %d"
          % (tag, card_key, level, dt, len(script)))
    print("  标签序列：%s" % "、".join(emos))
    print("  心情词 %d 个：%s" % (len(mood), "、".join(sorted(set(mood))) or "无"))
    if outside:
        print("  词表外：%s" % "、".join(sorted(set(outside))))
    print("  门禁：%s" % json.dumps(gate, ensure_ascii=False))
    return {"tag": tag, "card": card_key, "level": level, "seconds": round(dt, 1),
            "lines": len(script), "emotions": emos, "mood_words": sorted(set(mood)),
            "outside_vocab": sorted(set(outside)), "gate": gate,
            "usage": (meta or {}).get("usage")}


CASES = {
    # 同一段小说素材、同一张卡的位置，只有档位不同 —— 这是主对照。
    "story-none": ("小说素材 × 论文卡（none）", STORY, "paper"),
    "story-light": ("小说素材 × 小说卡（light）", STORY, "narrative"),
    "essay-none": ("论述素材 × 论文卡（none）", ESSAY, "paper"),
}


def main():
    cfg = ConfigManager().data()
    # 本地 35B 模型预填充长提示词时，安静五分钟是常态（实测第一组总耗时 26 分钟）。
    # 默认 300 秒的静默时限会把这种"还在算"误判成卡死。探针按最坏情况放宽，
    # 不动产品默认值——那是产品决策，不是探针能顺手改的。
    cfg["llm.idle_timeout"] = 1800
    preset = cfg.get("script.style_preset") or "argument"
    llm = load_llm(cfg)
    want = sys.argv[1:] or list(CASES)
    print("后端模型：%s，静默时限 %ss" % (cfg.get("llm.model"),
                                         cfg["llm.idle_timeout"]))
    rows = []
    for key in want:
        tag, material, card_key = CASES[key]
        rows.append(run_one(tag, material, card_key, cfg, llm, preset))

    print("\n=== 结论 ===")
    none_rows = [r for r in rows if r.get("level") == "none" and "emotions" in r]
    light_rows = [r for r in rows if r.get("level") == "light" and "emotions" in r]
    if none_rows:
        print("none 档作品里未出现心情词：%s"
              % ("是" if all(not r["mood_words"] for r in none_rows) else "否"))
    if light_rows:
        print("light 档作品里出现了心情词：%s"
              % ("是" if any(r["mood_words"] for r in light_rows) else "否"))
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump({"preset": preset, "model": cfg.get("llm.model"),
                   "discourse_vocab": DISCOURSE_VOCAB, "rows": rows},
                  f, ensure_ascii=False, indent=2)
    print("已落盘 %s" % OUT)


if __name__ == "__main__":
    main()
