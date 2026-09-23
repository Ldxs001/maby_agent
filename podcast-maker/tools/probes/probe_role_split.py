# -*- coding: utf-8 -*-
"""只读探针：量化「一个人被拆成 A/B 两个标签」的次数。

判定口径（透明可复核）：
  脚本设定 A = 提问方、B = 回答方，且严格交替。
  因此以偶数位开头的二元组 (A_i, B_{i+1}) 期望是 (问句, 陈述句)；
  以奇数位开头的二元组 (B_i, A_{i+1}) 期望是 (陈述句, 问句)。
  只要不满足，就说明「同一方的连续发言」被标签边界切断了
  —— 前一句是问、后一句还是问，或前一句是陈述、后一句还是陈述。
"""

import json
import os

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "projects", "20260912-235659")


def is_q(t):
    t = (t or "").strip()
    return t.endswith("？") or t.endswith("?")


def look(d, i, n=2):
    out = []
    for j in range(i, min(i + n, len(d))):
        out.append("#%d[%s]%s" % (j, d[j].get("speaker"), (d[j].get("text") or "")[:30]))
    return out


def run(name):
    d = json.load(open(os.path.join(ROOT, name), encoding="utf-8"))
    bad = []
    for i in range(len(d) - 1):
        a, b = d[i], d[i + 1]
        qa, qb = is_q(a.get("text")), is_q(b.get("text"))
        if i % 2 == 0:
            ok = qa and not qb          # A 问 → B 答
        else:
            ok = (not qa) and qb        # B 答 → A 问
        if not ok:
            bad.append((i, qa, qb))
    print("== %s  共 %d 句，错位二元组 %d 处" % (name, len(d), len(bad)))
    # 分类
    qq = [i for i, qa, qb in bad if qa and qb]
    ss = [i for i, qa, qb in bad if (not qa) and (not qb)]
    print("   两问相连（问方被切断）%d 处；两陈述相连（答方被切断）%d 处" % (len(qq), len(ss)))
    print("   —— 前 4 处明细 ——")
    for i, qa, qb in bad[:4]:
        for ln in look(d, i):
            print("      " + ln)
        print("      ----")
    return len(bad), len(qq), len(ss)


if __name__ == "__main__":
    tot = [0, 0, 0]
    for nm in ("脚本/1.json", "脚本/2.json"):
        r = run(nm)
        tot = [t + x for t, x in zip(tot, r)]
    print("== 两期合计：错位 %d 处（问方被切 %d / 答方被切 %d）" % tuple(tot))
