# -*- coding: utf-8 -*-
"""排图容量机制是否真的在管事 —— 用产品自己的函数复算，不自己估。

回答三件事：
 1. capacity() 算出来的一期素材容量是多少，体检的两条红线落在哪；
 2. 这一期地图上每一期的**真实素材字数**（按 refs 落点回查 segment.chars）；
 3. 拿这些字数再跑一次 planner._ratio_issues，看机制当时会说什么。

只读，不改任何文件。
"""
import io
import json
import os
import sys

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import planner, probe, script_engine as S  # noqa: E402

BASE = os.path.join(ROOT, "projects")
PID = "20260915-103249"


class Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def pick_ir(sid):
    """读素材的探查结果；磁盘上有就用磁盘上的。"""
    ir = probe.load(BASE, PID, sid)
    if ir is None:
        for name in ("%s.probe.json" % sid,):
            p = os.path.join(BASE, PID, "素材", name)
            if os.path.exists(p):
                ir = json.load(io.open(p, encoding="utf-8"))
                break
    return ir


def main():
    man = json.load(io.open(os.path.join(BASE, PID, "报告", "1.manifest.json"),
                            encoding="utf-8"))
    cfg = Cfg(man.get("config_snapshot") or {})

    target = probe.target_chars(cfg)
    ratio = probe.ratio_of(cfg)
    per_ep = probe.capacity(cfg)
    hi = ratio * probe.RATIO_HEADROOM

    print("=" * 74)
    print("一、容量口径（产品自己的函数算出来的）")
    print("=" * 74)
    print("  每期目标时长        script.target_minutes = %s 分钟" % cfg.get("script.target_minutes"))
    print("  成稿目标字数        probe.target_chars()  = %d 字" % target)
    print("  压缩档              probe.ratio_of()      = 1:%d" % ratio)
    print("  一期素材名义容量    probe.capacity()      = %d 字" % per_ep)
    print()
    print("  压比体检的两条红线（按 素材 ÷ 成稿目标 算）：")
    print("    下限  MIN_RATIO      = %.2f  → 素材至少 %d 字" % (probe.MIN_RATIO, int(target * probe.MIN_RATIO)))
    print("    上限  档位 × %.2f    = %.2f  → 素材最多 %d 字" % (probe.RATIO_HEADROOM, hi, int(per_ep * probe.RATIO_HEADROOM)))
    print("  排图提示词里给模型的话：一期素材大致 %d 字、硬上限 %d 字"
          % (per_ep, int(per_ep * probe.RATIO_HEADROOM)))
    print("  脚本侧按目标字数反推的句数 = %d 句" % S.estimate_line_count(cfg, target))

    # ---------------------------------------------------------- 建单元队列
    idx = json.load(io.open(os.path.join(BASE, PID, "素材", "index.json"),
                            encoding="utf-8"))
    sids = [s["id"] for s in idx.get("sources") or []]
    irs = {}
    for sid in sids:
        ir = pick_ir(sid)
        if ir:
            irs[sid] = ir
    units = planner.units_of(irs, per_ep)
    print()
    print("=" * 74)
    print("二、取料单元队列（与排图当时同一口径）")
    print("=" * 74)
    print("  素材 %d 份，单元 %d 条" % (len(irs), len(units)))
    by_src = {}
    for sid, s in units:
        by_src.setdefault(sid, 0)
        by_src[sid] += 1
    for sid in sids:
        ir = irs.get(sid)
        print("    %s  单元 %-4s 有效正文 %-8s  unit_level=H%s"
              % (sid, by_src.get(sid, 0),
                 (ir or {}).get("body_chars"), (ir or {}).get("unit_level")))

    # 落点 → 单元
    loc = {}
    for sid, s in units:
        loc[(sid, int(s.get("line") or 0))] = s

    m = json.load(io.open(os.path.join(BASE, PID, "地图", "map.json"),
                          encoding="utf-8"))
    eps = m.get("episodes") or []

    print()
    print("=" * 74)
    print("三、逐期真实素材字数（落点回查，不读落盘的 chars 字段）")
    print("=" * 74)
    recomputed = []
    miss = 0
    for e in eps:
        tot, hit, lost = 0, 0, []
        for r in (e.get("refs") or []):
            sid = str(r.get("source") or "")
            ln = int(r.get("line") or 0)
            s = loc.get((sid, ln))
            if s is None:
                # 行号对不上时按标题在本素材内找
                for (ssid, sln), cand in loc.items():
                    if ssid == sid and cand.get("title") == str(r.get("anchor") or "").strip():
                        s = cand
                        break
            if s is None:
                lost.append("%s:%s" % (sid, r.get("anchor")))
                continue
            tot += int(s.get("chars") or 0)
            hit += 1
        miss += len(lost)
        recomputed.append({"no": e.get("no"), "title": e.get("title"),
                           "chars": tot, "refs": len(e.get("refs") or []),
                           "hit": hit, "lost": lost})
    for row in recomputed:
        r = row["chars"] / float(target) if target else 0
        zone = ("超上限" if r > hi else
                "低于下限" if r < probe.MIN_RATIO else
                "扩写区(偏松)" if r < 1.0 else "合格")
        print("  第 %-4s 期 素材 %-7d 字  压比 %-5.2f  %-12s  落点 %d/%d%s"
              % (row["no"], row["chars"], r, zone, row["hit"], row["refs"],
                 ("  未命中=%s" % row["lost"]) if row["lost"] else ""))

    print()
    print("=" * 74)
    print("四、把复算出来的字数喂回体检，看机制当时会说什么")
    print("=" * 74)
    eps_r = [{"no": r["no"], "chars": r["chars"], "title": r["title"]}
             for r in recomputed]
    issues = planner._ratio_issues(eps_r, target, ratio)
    print("  _ratio_issues  返回 %d 条（非空 = 会触发重排）" % len(issues))
    for x in issues:
        print("    · %s" % x)

    total_mat = sum(r["chars"] for r in recomputed)
    print()
    print("  全部素材 %d 字 / 名义容量 %d 字 → 折算约 %.1f 期"
          % (total_mat, per_ep, total_mat / float(per_ep) if per_ep else 0))
    print("  落盘地图里 chars 字段的真实值：%s"
          % sorted({str(e.get("chars")) for e in eps}))

    print()
    print("=" * 74)
    print("五、拿第一期做对账：素材能撑出多少脚本字")
    print("=" * 74)
    sc = json.load(io.open(os.path.join(BASE, PID, "脚本", "1.json"), encoding="utf-8"))
    seen, uniq = set(), []
    for x in sc:
        t = x.get("text") or ""
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    mat1 = recomputed[0]["chars"] if recomputed else 0
    print("  第 1 期素材（去重前口径） = %d 字" % mat1)
    print("  脚本落盘 %d 句 / %d 字；去重后 %d 句 / %d 字"
          % (len(sc), sum(len(x.get("text") or "") for x in sc),
             len(uniq), sum(len(t) for t in uniq)))
    print("  目标脚本字数 = %d 字" % target)
    print("  唯一内容 ÷ 目标 = %.2f  ← 模型实际能写出的量" % (sum(len(t) for t in uniq) / float(target)))
    print("  目标句数 = %d 句，唯一内容 %d 句，缺口 %d 句"
          % (S.estimate_line_count(cfg, target), len(uniq),
             S.estimate_line_count(cfg, target) - len(uniq)))
    print()
    print("  未命中的落点合计 %d 个（0 = 全部对上）" % miss)

    # ------------------------------------------------- 六、下限校准的代价推演
    print()
    print("=" * 74)
    print("六、把下限从 %.2f 往上挪，哪些期落马、期数会变成几期"
          % probe.MIN_RATIO)
    print("=" * 74)
    print("  口径：压比 = 期素材 ÷ 成稿目标（%d 字）。低于下限的期与相邻期合并，" % target)
    print("        合并到够下限为止；每期素材按相邻顺序累加。这只是**代价估算**，")
    print("        真跑时由模型重新分组，结果会比这更好看些也会更散些。")
    print()
    print("  %-8s %-10s %-10s %-8s %s"
          % ("下限", "素材至少", "落马期数", "并后", "落马的是哪几期"))
    for f in (0.5, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0):
        need = target * f
        bad = [r for r in recomputed if r["chars"] < need]
        merged, acc, group = [], 0.0, []
        for r in recomputed:
            acc += r["chars"]
            group.append(r)
            if acc >= need:
                merged.append((group, acc))
                acc, group = 0.0, []
        if group:
            merged.append((group, acc))
        tag = "（现值）" if abs(f - probe.MIN_RATIO) < 1e-9 else ""
        print("  %-8s %-10s %-10d %-8d %s"
              % ("%.2f%s" % (f, tag), "%d 字" % int(need), len(bad),
                 len(merged),
                 ("、".join(str(x["no"]) for x in bad) if bad else "无")))
    print()
    print("  读法：下限 0.5 时 0 期落马——体检放行，14 期原样落库，这就是本次事故。")
    print("        下限 1.0（素材不少于成稿目标 = 不许扩写）时，只有料最少的那几期落马，")
    print("        合并后仍能保住大部分期数。下限越往上，并得越狠、期数越少。")

    print()
    print("=" * 74)
    print("七、期数本身的账（项目定的 14 期 vs 素材撑得起几期）")
    print("=" * 74)
    print("  项目计划期数            planned_episodes = 14")
    print("  一期名义容量            %d 字" % per_ep)
    print("  14 期要多少素材         14 × %d = %d 字" % (per_ep, 14 * per_ep))
    print("  实际素材总量            %d 字" % total_mat)
    print("  到位率                  %.0f%%" % (100.0 * total_mat / (14 * per_ep)))
    print("  → 摊到每期平均          %d 字（名义容量的 %.0f%%）"
          % (total_mat // 14, 100.0 * (total_mat / 14.0) / per_ep))
    print()
    print("  代码里对期数的两道闸门（planner.plan_map）：")
    floor_chars = target * probe.MIN_RATIO
    print("    ① 报错闸门：期数 > 素材总量 ÷ (目标 × 下限) = %d ÷ %d = %d 期才拦"
          % (floor_chars, total_mat, int(total_mat // floor_chars)))
    print("       14 < %d → 放行" % int(total_mat // floor_chars))
    tight = -(-total_mat // int(target * ratio * probe.RATIO_HEADROOM))
    print("    ② 警告闸门：期数 < 素材总量 ÷ (目标 × 档位 × 1.25) = %d 期才警告"
          % tight)
    print("       14 < %d 不成立 → 不警告" % tight)
    print("    → 两道闸门一个管「期数太少」、一个管「期数多到破下限」，")
    print("      **「期数太多、每期料不够」这一侧没有任何闸门**。")


if __name__ == "__main__":
    main()
