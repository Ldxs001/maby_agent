#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目目录树落位实测：立项建树、素材/地图/脚本/成品各归其位。

跑在一个临时输出目录里，不碰真实的 projects/。
"""

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from podcast_maker import layout, pipeline, project_store as P, source_store   # noqa: E402

OUT = []


def say(msg):
    OUT.append(msg)
    print(msg, flush=True)


def tree(root, prefix=""):
    lines = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p):
            lines.append("%s%s/" % (prefix, name))
            lines.extend(tree(p, prefix + "  "))
        else:
            lines.append("%s%s" % (prefix, name))
    return lines


def main():
    base = tempfile.mkdtemp(prefix="pm_tree_")
    try:
        say("== 1. 立项即建树 ==")
        p = P.create(base, "试作", "mapped")
        pid = p["id"]
        root = P.project_dir(base, pid)
        names = sorted(os.listdir(root))
        say("项目目录：%s" % pid)
        say("子目录（%d 个）：%s" % (len(names), "、".join(names)))
        missing = [n for n in layout.SUBDIRS if n not in names]
        say("应建未建：%s" % ("无" if not missing else "、".join(missing)))

        say("")
        say("== 2. 素材落进项目目录 ==")
        source_store.add_source(base, pid, "s1", "# 第一章\n甲。\n\n# 第二章\n乙。")
        say("素材目录：%s" % source_store.source_dir(base, pid))
        say("素材文件：%s" % "、".join(sorted(os.listdir(source_store.source_dir(base, pid)))))

        say("")
        say("== 3. 地图落成独立文件 ==")
        P.set_map(base, pid, [
            {"no": "1", "title": "第一期", "points": ["要点一"],
             "refs": [{"source": "s1", "anchor": "第一章"}]},
            {"no": "2", "title": "第二期", "points": ["要点二"],
             "refs": [{"source": "s1", "anchor": "第二章"}]},
        ])
        mpath = layout.map_file(root)
        say("地图文件：%s（存在 %s）" % (mpath, os.path.exists(mpath)))
        raw = json.load(io.open(P.store_path(base), encoding="utf-8"))
        has_map = "map" in (raw["projects"][0] or {})
        say("登记表里还带 map 字段：%s（应为 False——两处存就是两个真值来源）" % has_map)
        say("读回地图：%s" % [e["no"] for e in P.map_episodes(P.find(base, pid))])

        say("")
        say("== 4. 脚本与成品各归其位 ==")
        ep = layout.episode_files(root, "1")
        pipeline.write_json(ep["script"], [{"speaker": "A", "text": "甲"}])
        for k, v in (("artifact", ep["audio"]), ("video", ep["video"])):
            with io.open(v, "wb") as f:
                f.write(b"x")
        with io.open(ep["manifest"], "w", encoding="utf-8") as f:
            json.dump({"do_video": False, "assets": {"audio": ep["audio"],
                                                     "article": ep["article"]}}, f)
        for k in ("script", "audio", "video", "manifest"):
            say("%-9s -> %s" % (k, os.path.relpath(ep[k], base)))
        rep = pipeline.build_report(ep, __import__("podcast_maker.config_manager",
                                                   fromlist=["ConfigManager"]).ConfigManager().data())
        say("报告能读到本期清单：%s（产物齐备 %s）"
            % (rep["items"][0]["label"], rep["items"][0]["ok"]))

        say("")
        say("== 5. 记账后下一期前移 ==")
        P.record_episode(base, pid, "第一期", "1")
        got = P.find(base, pid)
        say("下一期：%s（应为 2）" % got["next_episode"])
        say("地图第 1 期 done：%s（应为 True，且已落盘）"
            % P.map_episodes(P.find(base, pid))[0].get("done"))

        say("")
        say("== 6. 历史列表扫得到 ==")
        rows = pipeline.list_projects(base)
        say("列出 %d 期：%s" % (len(rows),
                              ["%s/第%s期" % (r["root"], r["no"]) for r in rows]))

        say("")
        say("== 目录树 ==")
        for line in tree(base):
            say("  " + line)
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
