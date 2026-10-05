# -*- coding: utf-8 -*-
"""验证「重跑是否等于 none 档」这条链，两端各取真源码实测。

上游（管线侧）：项目卡 → resolve_paradigm → emotion_level_of → degree 值
下游（服务端）：build_instruct(emotion, speed, degree) 的真实返回

下游不重写逻辑，用 ast 从 tts_service/serve.py 抠出真函数执行——
保证测的就是产品里跑的那段代码。
"""
import ast
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


# ---------- 上游：管线取值链 ----------
def upstream():
    from podcast_maker import paradigms, script_engine
    from podcast_maker.config_manager import ConfigManager

    cfg = ConfigManager()
    with io.open(os.path.join(ROOT, "projects/_projects.json"), encoding="utf-8") as f:
        data = json.load(f)

    def find(o):
        if isinstance(o, dict):
            if o.get("id") == "20260915-103249":
                return o
            for v in o.values():
                r = find(v)
                if r:
                    return r
        elif isinstance(o, list):
            for i in o:
                r = find(i)
                if r:
                    return r

    proj = find(data)
    card = script_engine.resolve_paradigm(proj, cfg)
    level = paradigms.emotion_level_of(card)
    return proj, card, level


# ---------- 下游：抠出 serve.py 的真函数 ----------
def load_build_instruct():
    """从 serve.py 真源码里取出 build_instruct，连同它依赖的模块级赋值一起执行。"""
    path = os.path.join(ROOT, "tts_service", "serve.py")
    with io.open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    ns = {}
    fn = None
    need = {"INSTRUCT_EMOTIONS"}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {getattr(t, "id", None) for t in node.targets}
            if targets & need:
                mod = ast.Module(body=[node], type_ignores=[])
                ast.fix_missing_locations(mod)
                try:
                    exec(compile(mod, path, "exec"), ns)
                except Exception as e:      # 依赖别的名字就跳过，下面会报缺
                    print("  [warn] 常量执行失败 %s：%s" % (targets, e))
        elif isinstance(node, ast.FunctionDef) and node.name == "build_instruct":
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, path, "exec"), ns)
            fn = ns.get("build_instruct")
    if fn is None:
        raise RuntimeError("serve.py 里没找到 build_instruct")
    missing = [n for n in need if n not in ns]
    if missing:
        raise RuntimeError("缺依赖常量：%s" % missing)
    fn.__globals__.update(ns)   # 让真函数拿到真词表
    return fn


def main():
    proj, card, level = upstream()
    build_instruct = load_build_instruct()

    print("=" * 68)
    print("上游：管线侧取到的档位")
    print("=" * 68)
    print("  项目 paradigm 字段        : %r" % proj.get("paradigm"))
    print("  resolve_paradigm 命中卡片 : 键=%r 标题=%r"
          % (card.get("key"), card.get("title", card.get("name", ""))))
    print("  卡上 emotion_level        : %r" % card.get("emotion_level"))
    print("  emotion_level_of 判定     : %r   ← 就是重跑时会传下去的 degree"
          % level)

    print()
    print("=" * 68)
    print("下游：服务端 build_instruct 真实返回")
    print("=" * 68)
    cases = [
        ("本次重跑会走这条", "平静", 1.0, level),
        ("对照：老逻辑（不传档位）", "平静", 1.0, None),
        ("对照：小说卡 light 档", "平静", 1.0, "light"),
        ("语篇标签（本来就不转）", "追问", 1.0, level),
        ("语篇标签 + 老逻辑", "追问", 1.0, None),
    ]
    for label, emo, speed, deg in cases:
        out = build_instruct(emo, speed, deg)
        verdict = "不发 instruct ✓" if out is None else "发 %r" % out
        print("  %-22s emotion=%-4s degree=%-7s → %s"
              % (label, emo, repr(deg), verdict))

    print()
    print("=" * 68)
    print("脚本侧实际分布（决定重跑后到底有多少句会变）")
    print("=" * 68)
    import collections
    with io.open(os.path.join(ROOT, "projects/20260915-103249/脚本/1.json"),
                 encoding="utf-8") as f:
        script = json.load(f)
    counter = collections.Counter(s.get("emotion", "") for s in script)
    instruct_emotions = build_instruct.__globals__["INSTRUCT_EMOTIONS"]

    changed = same = 0
    for emo, n in counter.most_common():
        is_real = emo in instruct_emotions
        if is_real:
            changed += n
        else:
            same += n
        print("  %-6s %4d 句  真情绪词表里%s → 重跑后%s"
              % (emo, n, "有" if is_real else "没有",
                 "由「发指令」变「不发」" if is_real else "本来就不发，无变化"))

    print()
    print("  合计：%d 句会变（真情绪词），%d 句无变化（语篇标签）"
          % (changed, same))
    print("  INSTRUCT_EMOTIONS 词表共 %d 个" % len(instruct_emotions))

    return {
        "degree": level,
        "changed_lines": changed,
        "same_lines": same,
        "instruct_emotions": sorted(instruct_emotions),
    }


if __name__ == "__main__":
    res = main()
    print()
    print("机器可读:", json.dumps(res, ensure_ascii=False))
