# -*- coding: utf-8 -*-
"""只读探针：看 speaker 标签和文本内容是否对得上。

背景：script_engine.enforce_alternation() 会强制 A/B 交替，把同角色的
连续两句翻转成另一人。翻完之后脚本里就查不到翻转痕迹了，所以只能从
「内容承接关系」反推：一句话如果明显在接上一句，却换了角色，那它多半
是被掰过去的。

不看音频，纯文本统计。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = [
    os.path.join(ROOT, "projects", "20260912-235659", "脚本", "1.json"),
    os.path.join(ROOT, "projects", "20260912-235659", "脚本", "2.json"),
]

# 句首承接词：出现它，说明这句在接着上一句说，通常还是同一个人
CONT = ("但是", "但", "不过", "然而", "可是", "所以", "因此", "于是", "而且",
        "另外", "还有", "接着", "然后", "这时", "同时", "总之", "比如", "例如",
        "也就是说", "换句话说", "换言之", "其实", "当然", "况且", "甚至",
        "这就", "这样一来", "说到底", "本质上", "实际上", "问题是", "再看",
        "首先", "其次", "最后", "第一", "第二", "第三")

# 应答词：出现它，说明这句在回答上一句
ANSWER = ("不，", "不是", "不会", "不行", "不能", "是的", "对", "没错", "确实",
          "正是", "当然", "其实", "可以说", "答案是", "简单说", "一句话")

# 疑问特征
QWORDS = ("吗", "呢", "什么", "如何", "为什么", "怎么", "难道", "是否", "哪",
          "谁", "多少", "几", "何处", "何时")
QEND = ("？", "?")


def is_q(text):
    if text.endswith(QEND):
        return True
    head = text[:14]
    return any(w in head for w in QWORDS) and len(text) < 40


def head_hit(text, words):
    t = text.lstrip("「『\"'")
    return any(t.startswith(w) for w in words)


def run(path):
    lines = json.load(open(path, encoding="utf-8"))
    n = len(lines)
    print("  句数 %d" % n)

    # 1) 问句的角色分布
    qa = sum(1 for it in lines if it["speaker"] == "A" and is_q(it["text"]))
    qb = sum(1 for it in lines if it["speaker"] == "B" and is_q(it["text"]))
    na = sum(1 for it in lines if it["speaker"] == "A")
    nb = n - na
    print("  问句：A %d/%d = %.0f%%，B %d/%d = %.0f%%"
          % (qa, na, qa / max(1, na) * 100, qb, nb, qb / max(1, nb) * 100))

    # 2) 承接词开头的句子，角色有没有跟着换
    cont_total = cont_switch = 0
    samples = []
    for prev, cur in zip(lines, lines[1:]):
        if head_hit(cur["text"], CONT):
            cont_total += 1
            if cur["speaker"] != prev["speaker"]:
                cont_switch += 1
                if len(samples) < 12:
                    samples.append((prev, cur))
    print("  承接词开头的句子：%d 句，其中「换了角色」%d 句 = %.0f%%"
          % (cont_total, cont_switch, cont_switch / max(1, cont_total) * 100))

    # 3) 应答词开头的句子出现在哪个角色
    ansA = sum(1 for it in lines if it["speaker"] == "A" and head_hit(it["text"], ANSWER))
    ansB = sum(1 for it in lines if it["speaker"] == "B" and head_hit(it["text"], ANSWER))
    print("  应答词开头（明显在答话）：A %d 句，B %d 句" % (ansA, ansB))

    if samples:
        print("  承接却被换角色的样例（上一句 → 这一句）：")
        for prev, cur in samples:
            print("    [%s] %s" % (prev["speaker"], prev["text"][:26]))
            print("    [%s] %s   ← 承接上句，却换了角色" % (cur["speaker"], cur["text"][:26]))


for p in SCRIPTS:
    print("== %s" % os.path.basename(p))
    if os.path.exists(p):
        run(p)
    print()
