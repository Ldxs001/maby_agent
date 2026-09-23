#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 wUwproject
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP 服务 + 单页界面。

界面为深色自包含单页：无外部 CDN、无 emoji、CSS/JS 内联、仅系统字体。
所有控件的值域、默认值、枚举档位由后端下发（统一推动点位），
页面内不写第二份数值。
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import assets_factory, audio_engine, duration_model, ingest, layout, paradigms, pipeline
from . import planner
from . import probe, project_store, script_engine, source_store, subtitle_engine, tts_engine
from .config_manager import (BANNED_RULES, CONTENT_CHECKS,
                             DISCOURSE_ORDER, GROUPS, PARAM_SPEC, PRESET_SPEC,
                             PROGRAM_ONLY_TAGS, STYLE_DIMS, VERSION, ConfigManager,
                             gates_payload,
                             modes_payload, param_options, params_payload,
                             presets_payload, resolve_base_url, ui_payload,
                             validate_config)
from .llm_client import LLMClient, LLMError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = ConfigManager()
CALIB = duration_model.Calibration()
LOCK = threading.Lock()


# ------------------------------------------------------------------ 工具
def voice_ratios():
    """每个已实测音色的「音色语速 ÷ 标准语速」，供配置页挂在音色旁边。

    比例由实测算出，不做人工录入：音色语速是量出来的，不是填出来的——
    能填的那个数一旦和实测对不上，页面上就有两个语速，又回到两把尺子。
    没有样本的音色不出现在表里，界面照实显示「尚无实测」。
    """
    out = []
    for row in CALIB.summary():
        ratio = duration_model.ratio_of(CALIB, row["engine"], row["voice"], row["speed"])
        if ratio is None:
            continue
        out.append(dict(row, ratio=ratio, standard_k=duration_model.STANDARD_K))
    return out


def make_llm(cfg):
    return LLMClient(backend=cfg.get("llm.backend", "lm-studio"),
                     base_url=resolve_base_url(cfg),
                     api_key=cfg.get("llm.api_key", ""),
                     model=cfg.get("llm.model", ""),
                     timeout=int(cfg.get("llm.timeout", 3600)),
                     idle_timeout=int(cfg.get("llm.idle_timeout", 300)),
                     input_ratio=float(cfg.get("llm.input_ratio", 1.0)))
    # 两个量各管各的：输入额度（= 最大输出 × 输入倍率）答「这一次调用装不装得下
    # 原文」；内容额度（= 成稿 × 压缩档 × 1.25，见 script_engine.material_capacity）
    # 答「这一期该讲多少料」，归画地图。前者是旋钮，后者是业务结论。


def safe_join(base, rel):
    """路径拼接并阻断目录穿越。"""
    base_abs = os.path.normpath(os.path.abspath(base))
    target = os.path.normpath(os.path.join(base_abs, rel))
    if target != base_abs and not target.startswith(base_abs + os.sep):
        raise ValueError("路径越界")
    return target


# ------------------------------------------------------------------ 业务
# 字体点位。画面与字幕各一款，共用同一份候选表、同一个自绘下拉
# （界面见 `fontPicker`）。这两款字体管的是两处不同的地方，列表却没理由各写一份。
FONT_KEYS = ("frame.font_family", "subtitle.font_family")


def _font_options(fonts):
    """字体下拉的选项表。

    每项带族名与许可，装没装由 `off` 表示——没装的照列，只暗显不可选。
    `url` 只给随包字体：它没装进系统，浏览器按族名找不到，得把文件发过去；
    系统字体反过来，本机浏览器本来就能按族名拿到，不必再多下一份几十兆的文件。
    """
    from urllib.parse import quote
    out = [{"value": "", "label": "自动选择（推荐）"}]
    for f in fonts:
        o = {"value": f["family"], "label": f["label"], "desc": f["license"],
             "off": (not f["installed"]), "family": f["family"]}
        if f.get("source") == "bundled" and f.get("path"):
            o["url"] = "/api/fontfile?family=" + quote(f["family"])
        out.append(o)
    return out


# ------------------------------------------------------------------ 选文件
# 路径类点位（片头音频、片尾音频、立绘 PNG、自备音乐、自定义声明音频）填的是
# **本机绝对路径**——管线一律拿 os.path.exists 去判它。手打一条 Windows 路径既慢
# 又容易错，所以框旁边给一个「选择…」，弹本机文件对话框，选完把绝对路径写回该点位。
#
# 对话框在本机弹：服务就跑在这台机器上，与浏览器同机。别处访问这个界面时弹不出来，
# 那时回一句人话，不让前端干等。
#
# 用子进程而不是在本进程里开 Tk：web 请求跑在 ThreadingHTTPServer 的工作线程上，
# 而 Tk 只保证能在主线程里建 root（Win 上多数能跑，别的平台直接崩）。子进程自己
# 占着主线程，行为与平台无关。
PICK_KINDS = {
    "audio": [("音频文件", "*.wav *.mp3 *.m4a *.flac *.ogg *.aac *.wma"),
              ("全部文件", "*.*")],
    "image": [("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("全部文件", "*.*")],
    "any": [("全部文件", "*.*")],
}

_PICK_SCRIPT = r'''
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:                      # 没有图形接口的构建
    sys.stderr.write("no-tk: %s" % exc)
    raise SystemExit(3)
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)         # 别让对话框藏在浏览器后面
except Exception:
    pass
types = []
for spec in sys.argv[2:]:
    name, pats = spec.split("=", 1)
    types.append((name, pats.split()))
path = filedialog.askopenfilename(title=sys.argv[1], filetypes=types)
root.destroy()
sys.stdout.write(path or "")
'''


def pick_file(title, kind):
    """弹本机文件对话框，返回 {"ok":..,"path":..} 或 {"ok":False,"error":..}。

    取消（对话框直接关掉）算正常动作：返回 cancelled，前端当什么都没发生，
    不该弹一个红字提示。
    """
    types = PICK_KINDS.get(kind) or PICK_KINDS["any"]
    args = [sys.executable, "-c", _PICK_SCRIPT, title or "选择文件"]
    args += ["%s=%s" % (name, pats) for name, pats in types]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")   # 路径里有中文也不乱码
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "等了太久没选，已放弃；可以直接手填路径"}
    except Exception as exc:
        return {"ok": False, "error": "打不开本机文件对话框：%s" % exc}
    if proc.returncode != 0:
        tail = [x for x in (proc.stderr or "").strip().splitlines() if x.strip()]
        return {"ok": False,
                "error": "本机文件对话框不可用（%s）——可以直接手填路径"
                         % (tail[-1] if tail else "未知原因")}
    path = (proc.stdout or "").strip()
    return {"ok": True, "path": path, "cancelled": not path}


def api_pickfile(body):
    key = str(body.get("key") or "")
    spec = PARAM_SPEC.get(key) or {}
    if spec.get("type") != "path":
        return {"ok": False, "error": "这个点位不是路径类，不该有选择按钮"}
    return pick_file(spec.get("label") or "选择文件", spec.get("pick") or "any")


def api_config_get():
    cfg = CFG.data()
    warns, errs = validate_config(cfg)
    warns2, errs2 = audio_engine.validate_params(cfg)
    params = params_payload()
    # 字体白名单全量下发（含没装的），由后端填进档位表。
    # 前端不去拼第二份字体列表，否则两处来源迟早对不上。
    # 没装的照样列出来但暗显不可选：「装了哪些」决定能不能用，「白名单有哪些」
    # 决定知不知道有这款。后者靠列出来解决，前者靠 installed 判定。
    try:
        fonts = assets_factory.font_catalog()
    except assets_factory.AssetError:
        fonts = []
    for bucket in params.values():
        for items in bucket.values():
            for it in items:
                if it["key"] in FONT_KEYS:
                    it["type"] = "enum"
                    it["font_pick"] = True
                    it["options"] = _font_options(fonts)
                    # 配置里存着本机没装的字体时，补一条把当前值显示出来。
                    # 不补的话下拉会静默跳回第一项，看着像配置被改过。
                    cur = str(cfg.get(it["key"]) or "")
                    if cur and cur not in {o["value"] for o in it["options"]}:
                        it["options"].append(
                            {"value": cur, "family": cur,
                             "label": "%s（当前值·本机未安装）" % cur})
    return {
        "ok": True, "version": VERSION,
        # 点位按阶段归拢下发，界面直接照着渲染。控件放哪个盒子由数据决定，
        # 界面不写第二份归属表——那正是漏项与错位的来源。
        "params": params,
        "ui": ui_payload(),
        "values": cfg,
        "modes": modes_payload(), "presets": presets_payload(),
        "gates": gates_payload(),
        "banned_rules": BANNED_RULES,
        "content_checks": CONTENT_CHECKS,
        "style_dims": STYLE_DIMS, "preset_spec": PRESET_SPEC,
        "warnings": warns + warns2, "errors": errs + errs2,
        "calibration": CALIB.summary(),
        # 脚本侧唯一的那把尺子，以及各音色相对它的比例。两样都由后端算好下发：
        # 界面不做换算，改了标准语速只改一处，页面上不会出现第二个数。
        "standard": duration_model.standard_rate(),
        "ratios": voice_ratios(),
    }


def api_config_post(body):
    patch = body.get("patch") or {}
    rejected = CFG.update(patch)
    CFG.save()
    data = CFG.data()
    warns, errs = validate_config(data)
    # 回传归一后的值：表单送来的是字符串，配置表按声明转成数字。
    # 不回传的话，界面留着 "44100" 而配置里是 44100，下次比较就判成两个档。
    return {"ok": not rejected and not errs, "rejected": rejected,
            "warnings": warns, "errors": errs,
            "values": {k: data.get(k) for k in patch}}


def extract_material(body):
    """从请求体取出素材正文。粘贴走 `text`，上传走 `data_base64`。

    这是素材正文的唯一入口：素材入库与脚本生成都从这里取，两处各写一套
    的话，上传路径的抽取规则迟早会在其中一处先漂。
    """
    data_b64 = body.get("data_base64")
    if data_b64:
        raw = base64.b64decode(data_b64)
        name = body.get("filename") or "upload.bin"
        tmp = os.path.join(tempfile.gettempdir(), "pm_" + os.path.basename(name))
        with open(tmp, "wb") as f:
            f.write(raw)
        try:
            return ingest.ingest(file_path=tmp, anchor=body.get("anchor") or None)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return ingest.ingest(text=body.get("text") or "",
                         anchor=body.get("anchor") or None)


def api_ingest(body):
    res = extract_material(body)
    anchors = ingest.list_anchors(res["text"])
    res["anchors"] = [a["title"] for a in anchors[:60]]
    return dict(ok=True, **res)


def api_estimate(body):
    script = body.get("script")
    cfg = CFG.data()
    if script:
        est = duration_model.explain(script, cfg)
    else:
        est = None
    # 脚本侧的尺子是标准语速，与音色无关：这里不传 CALIB，估时不受本期音色影响。
    hint = duration_model.target_hint(
        cfg, speeds=[round(x * 0.05, 2) for x in range(10, 41, 5)])
    return {"ok": True, "estimate": est, "hint": hint}


def api_script_gate(body):
    script = body.get("script") or []
    cfg = CFG.data()
    # 门禁判的「同一人连续句数」上限取自**对话形式**（配置里选的 > 卡上的默认），
    # 所以校验也得知道是哪一张卡：卡决定默认形式，而形式上头挂着 A/B 两条上限。
    # 页面上改一个字段就重判一次；这里若退回全局默认卡，判出来的标准会和
    # 生成这份脚本时用的不是同一套——同一份稿子在两个地方一会儿过一会儿不过。
    paradigm = None
    pid = (body.get("project_id") or "").strip()
    if pid:
        try:
            _base, item, _root = _tree_of(pid)
        except ValueError:
            item = None
        if item:
            # **项目级覆盖必须一起补上**：生成那一刻走的是「全局 + 项目」，
            # 重判只读全局的话，两条路拿的是两把尺子——同一份稿子，写的时候
            # 合法、判的时候成了「词表外标签」，而报错就出在页面上。
            # 从前这里只补了卡、没补配置，等于修了一半。
            cfg = project_store.apply_to_config(cfg, item)
            # **这一期自己记过的形式，优先于当前配置**：生成那一刻用的是哪种形式，
            # 已经跟着稿子写进这一期的旁挂档（`layout.form_file`）。配置里那个值是
            # 「下一次生成用哪种」，不是「这一期当初用哪种」——上限挂在形式上头，
            # 拿此刻的配置去判当时写的稿子，就是两把尺子量同一份东西。
            # 没记过（本功能上线前写的期）就什么都不动，回落当前配置，与从前一样。
            ep = str(body.get("episode_no") or "").strip()
            if ep:
                recorded = pipeline.episode_form(_root, ep)
                if recorded:
                    cfg["script.dialogue_form"] = recorded
            paradigm = script_engine.resolve_paradigm(
                {"paradigm": item.get("paradigm")}, cfg)
    # 门禁给片头尾那两个词放行（**只有这一处**）：这份稿子是盘上的成品，程序粘上去
    # 的片头尾已经在里面，标签是 `PROGRAM_ONLY_TAGS`、不在语篇词表内——不放行就会在
    # 页面上被报成「词表外标签」。生成侧不传这个参数，正文的门槛一点没松。
    rep = script_engine.gate_generate(script, cfg, paradigm,
                                      extra_tags=PROGRAM_ONLY_TAGS)
    est = duration_model.explain(script, cfg)
    return {"ok": True, "report": rep, "estimate": est}


def _tree_of(pid):
    """按项目 id 取（输出根, 项目, 项目目录）。项目不存在时抛 ValueError。"""
    base = os.path.join(ROOT, CFG.get("project.output_dir", "projects"))
    item = project_store.find(base, pid)
    if item is None:
        raise ValueError("项目不存在：%s" % pid)
    return base, item, layout.project_dir(base, pid)


def api_scripts(q):
    """某项目的各期，以及每期有没有脚本。

    期号以地图为准；没有地图（逐期即兴）时退到「已出过的期 + 当前下一期」。
    合成页只列有脚本的期，判据就是这里的 `has_script`——没脚本的期压根不出现在
    可选项里，于是「选了一期却没有脚本」这个状态产生不了。
    """
    pid = (q or {}).get("project_id") or ""
    try:
        _, item, root = _tree_of(pid)
    except ValueError as e:
        return {"ok": False, "error": str(e), "items": []}
    rows = []
    eps = project_store.map_episodes(item)
    if eps:
        for e in eps:
            rows.append({"no": str(e.get("no") or ""), "title": e.get("title") or "",
                         "done": bool(e.get("done"))})
    else:
        for e in item.get("episodes") or []:
            rows.append({"no": str(e.get("no") or ""), "title": e.get("title") or "",
                         "done": True})
        nxt = str(item.get("next_episode") or "1")
        if not any(r["no"] == nxt for r in rows):
            rows.append({"no": nxt, "title": "", "done": False})
    for r in rows:
        path = layout.script_file(root, r["no"])
        r["has_script"] = os.path.exists(path)
        sc = pipeline.read_json(path, []) if r["has_script"] else []
        r["lines"] = len(sc) if isinstance(sc, list) else 0
    return {"ok": True, "project_id": pid, "items": rows}


def api_script_get(q):
    """读某一期已经落盘的脚本，供调出来改。"""
    q = q or {}
    pid = q.get("project_id") or ""
    no = str(q.get("episode_no") or "").strip()
    try:
        _, _, root = _tree_of(pid)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not no:
        return {"ok": False, "error": "没给期号"}
    sc = pipeline.read_json(layout.script_file(root, no), None)
    if sc is None:
        return {"ok": False, "error": "第 %s 期还没有脚本。" % no}
    return {"ok": True, "script": sc, "episode_no": no}


def api_script_save(body):
    """把改过的脚本存回项目。合成时读的就是这一份，存不回来改了等于没改。"""
    pid = body.get("project_id") or ""
    no = str(body.get("episode_no") or "").strip()
    script = body.get("script")
    try:
        _, _, root = _tree_of(pid)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not no:
        return {"ok": False, "error": "没给期号"}
    if not isinstance(script, list) or not script:
        return {"ok": False, "error": "脚本为空，没东西可存"}
    pipeline.write_json(layout.script_file(root, no), script)
    return {"ok": True, "episode_no": no, "lines": len(script)}


def api_script_issues(body):
    """合成前的提醒：读脚本阶段落盘的门禁结论。只读、不判、不算。

    读的是脚本生成那一刻记下的那一份，不追踪此后的人工修改——改没改以手上的
    稿子为准，所以框里要把这一点说明白。读不到就当没有问题：不拿一份不存在
    或读坏了的文件去拦人，提醒的本分是提醒。
    """
    pid = body.get("project_id") or ""
    eps = [str(x).strip() for x in (body.get("episodes") or []) if str(x).strip()]
    if not eps:
        one = str(body.get("episode_no") or "").strip()
        eps = [one] if one else []
    if not pid or not eps:
        return {"ok": True, "issues": []}
    try:
        _, _, root = _tree_of(pid)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    out = []
    for no in eps:
        rep = script_engine.read_gate_report(layout.tmp_dir(root, no))
        if not rep or rep.get("passed", True):
            continue
        items = [{"label": i.get("label", i.get("key", "")),
                  "detail": i.get("detail", ""),
                  "level": i.get("level", "warn"),
                  "soft": bool(i.get("soft"))}
                 for i in (rep.get("items") or [])
                 if not i["ok"] and not i.get("advisory")]
        if items:
            out.append({"episode_no": no, "time": rep.get("time", ""),
                        "items": items})
    return {"ok": True, "issues": out}


def api_script_generate(body):
    """起一个后台任务写脚本，立刻返回任务号。

    这一趟是「模型慢慢写一整期」，本地模型十几分钟到半小时都正常。压在
    请求里等，浏览器和服务端线程都得一直挂着，中途关页面、刷新、服务重启
    就是全白跑，而且看不到任何中途进展。合成那一步早就这么办了，脚本没道理
    例外——两处的进度、日志、中止走的是同一套 /api/task 轮询。
    """
    title = "写脚本"
    if body.get("project_id"):
        title += " · 第 %s 期" % (body.get("episode_no") or "下一期")
    jid = pipeline.run_async("script", lambda job: _script_generate_work(body, job),
                             title)
    return {"ok": True, "task_id": jid}


# 轮次日志的段名与序号：两段各自成环，界面靠段名决定进度往哪一半折算。
_ROUND_LOG = re.compile(r"^(门禁|检查)第\s*(\d+)\s*轮")
_GEN_LOG = re.compile(r"^生成第\s*\d+\s*次")


def _script_stage(job, msg):
    """把一行日志记进任务，并按段折算轮次进度。

    三段串行：出货（整篇现写或分段初稿）→ 检查（内容检）→ 门禁（形式门禁）。
    后两段各跑各的轮，检查段占 0~50%、门禁段占 50~100%。所以主体把段名写进每一行
    ——「门禁第 N 轮」「检查第 N 轮」「生成第 N 次」。界面拿段名去认该用哪一段的
    预算折算；两段都喊「第 N 轮」的话，进度条会在门禁段开头往回跳一半。

    进度只在**一轮真的跑完**时前进（看「模型返回」那一行）；一轮刚开头那几行只把
    进度补到本段已跑完的格数。正在跑的那一轮有多长本来就没人知道，画一根匀速前进
    的假进度条只会让人以为快好了。「调用模型…」那几行只更新阶段——它的用处是让
    界面在漫长的一轮里说得出自己在干什么，而不是停在上一句话上。
    """
    pipeline.job_log(job, msg)
    if _GEN_LOG.match(msg):
        # 出货那条线（生成第 N 次）：它是三段里的第一段，只有整篇路才走。
        # 「输出坏了重发」不是一轮判定，所以它只改阶段、不动进度。
        job["stage"] = msg
        return
    if msg.startswith("检查通过"):
        # 检查段提前收工：把进度补齐到它的终点，好让门禁段从 50% 接着走。
        job["stage"] = msg
        job["progress"] = max(job.get("progress", 0.0), 0.5)
        return
    if msg.startswith("门禁通过"):
        # 门禁段是最后一段，它通过就等于定稿了。
        job["stage"] = msg
        job["progress"] = max(job.get("progress", 0.0), 0.95)
        return
    m = _ROUND_LOG.match(msg)
    if not m:
        return
    job["stage"] = msg
    seg, done = m.group(1), int(m.group(2))
    # 检查在前（0~50%）、门禁在后（50~100%）。门禁段多一格：首轮只判定不出货，
    # 所以它的轮次编号从 1 到 gate_rounds + 1。
    if seg == "检查":
        budget, base = int(CFG.get("script.check_rounds", 3)), 0.0
    else:
        budget, base = int(CFG.get("script.gate_rounds", 3)) + 1, 0.5
    step = 0.5 / max(1, budget)
    ratio = done if "模型返回" in msg else max(0, done - 1)
    job["progress"] = max(job.get("progress", 0.0),
                          min(base + step * ratio, base + 0.5))


def _script_generate_work(body, job=None):
    """写一期脚本（真的干活的那个）。返回给前端的结果形状与从前一致。

    `job` 只用来喂日志与阶段，缺席也能跑——命令行和批任务各有各的收纳处。
    """
    cfg = CFG.data()
    material = body.get("material") or ""
    logs = []

    def log(msg):
        """一行日志进三处：任务日志（界面实时看得到）、返回值、以及阶段标签。

        从前这些字只在跑完之后跟着返回体一起到，等于整段生成过程对人是黑的。
        """
        logs.append(msg)
        if job is not None:
            _script_stage(job, msg)

    # 归属项目：期号与计划期数取自项目进度，让模型知道自己在写第几期、共几期。
    # 项目的节目名/风格/音色也一并盖上来——出片阶段会这么盖，生成阶段不盖的话，
    # 脚本里写死的片头句用的是另一个节目名，到出片时就成了错的。
    base = _projects_base()
    proj_item = None
    plan_row = None
    proj_ctx = {}
    evidence = {}
    # 逐节原文的体量与取料回调：只有成稿规划（有地图落点）才有。没有时装箱
    # 退化成「一节一段」，写作退回整期素材——逐期即兴与单集本来就该走整篇。
    sec_chars = None
    material_of = None
    sec_flags = None
    project_id = body.get("project_id") or ""
    if project_id:
        proj_item = project_store.find(base, project_id)
        if proj_item is None:
            return {"ok": False, "error": "项目不存在：%s" % project_id, "logs": logs}
        cfg = project_store.apply_to_config(cfg, proj_item)
        # 指定了期号就用指定的——批量生成、以及回头改某一期，都要落到具体某一期上；
        # 没指定才落到「下一期」。
        episode_no = str(body.get("episode_no") or "").strip() or \
            proj_item.get("next_episode", "1")

        # 料源按范式定，与出片阶段调同一个函数：成稿规划一律走地图落点，调用方
        # 递来的素材不作数；逐期即兴用递来的。判据只有这一处实现，不会两处漂开。
        try:
            material, plan_row = pipeline.resolve_material(
                base, proj_item, episode_no, material, log=log)
        except (pipeline.PipelineError, source_store.SourceError) as e:
            return {"ok": False, "error": str(e), "logs": logs}
        proj_ctx = {"episode_no": episode_no,
                    "planned_episodes": proj_item.get("planned_episodes"),
                    # 素材类型也一并带上：写脚本这一步用项目已定的那张卡去定
                    # 两人的站位与默认对话形式（连句上限跟着形式走），与排图/
                    # 凝缩取的是同一张卡——项目定了就贯穿到底，不在这一段另判一次。
                    "paradigm": proj_item.get("paradigm") or "",
                    # 地图片段是本期"讲什么"的唯一来源，与出片阶段取同一份。
                    "title": (plan_row or {}).get("title") or "",
                    "gist": (plan_row or {}).get("gist") or "",
                    "points": (plan_row or {}).get("points") or [],
                    "sources": [r.get("anchor") for r in
                                ((plan_row or {}).get("refs") or [])
                                if r.get("anchor")]}
        # 本期判据包：主旨/要点 + 各节凝缩。内容检判「方向」时用它。与素材取自
        # 同一处（地图落点），不另立来源——两个来源迟早给出两套「本期讲什么」。
        evidence = pipeline.evidence_pack(base, proj_item, plan_row, log=log)
        # 逐节原文：装箱要知道每节多重，写作要按块取「本段那几节的原文」。
        # 与判据包同一份节清单（直接把它传进去），节号才对得上号——两处各取
        # 一次，迟早给出对不上号的编号。
        if evidence.get("sections"):
            sec_texts, sec_used = pipeline.section_materials(
                base, proj_item, plan_row, log=log,
                sections=evidence["sections"])
            sec_chars = [len(t) for t in sec_texts]
            material_of = pipeline.section_material_of(sec_texts, sec_used,
                                                       log=log)
            # 取材标记：手牌就是逐节原文，形状判定在这里做（一次判定，
            # 全程消费）；标记只改提示词展示，素材与配额分毫不动。
            if cfg.get("script.shape_flags", True):
                sec_flags = {
                    i + 1: script_engine._shape_flags(
                        t, cfg,
                        anchor=(evidence["sections"][i] or {}).get("anchor"))
                    for i, t in enumerate(sec_texts)
                    if script_engine._shape_flags(
                        t, cfg,
                        anchor=(evidence["sections"][i] or {}).get("anchor"))
                }

    if not material.strip():
        return {"ok": False, "logs": logs,
                "error": "素材为空：逐期即兴请粘贴内容或上传文件。"}
    # 本期素材体量：提示词要把它说给模型听。不说的话，模型只知道「要写多少」、
    # 不知道「手里有多少」——素材不够时它只能凑（实测凑法是把写过的整段再背
    # 一遍）。这就是交到这一步的原文长度，挂项目与不挂项目都算得出来。
    proj_ctx["chars"] = len(material.strip())

    # 项目定了风格就以项目为准：脚本页那个下拉显示的是全局值，
    # 让它顶掉项目设定会让项目内各期风格不一致。
    preset_key = body.get("preset") or cfg.get("script.style_preset")
    if proj_item and proj_item.get("style_preset"):
        preset_key = proj_item["style_preset"]

    try:
        llm = make_llm(cfg)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "LLM 后端不可用：%s" % e, "logs": logs}

    # 落点先算好：每轮写完都要往这儿写一份，不再等整段跑完。
    no = proj_ctx.get("episode_no") or "1"
    root = layout.project_dir(base, project_id) if project_id else None

    def save_draft(g):
        """把当前这一版稿子与它的门禁结论落到出片时读的那个位置。

        生成一轮要十几分钟。从前只在整段跑完才第一次写盘，中途卡住、被中止、
        进程被 setup.bat 杀掉，手上就什么都没有——稿子明明已经写好了。
        先落盘，后面不论出什么事，都有一份完整的稿子。

        门禁结论跟着稿子一起写：合成前读它给人提个醒（只提醒，不拦人）。
        每次生成都覆盖一份，过没过都写——留着上一次的失败结论不动，
        下次合成就读到一个早就修好的旧问题，提醒成了狼来了。
        """
        if root is None:
            return
        os.makedirs(layout.script_dir(root), exist_ok=True)
        pipeline.write_json(layout.script_file(root, no), g["script"])
        pipeline.write_plan_file(root, no, g)
        # 这一期用的对话形式，跟着稿子一起记进旁挂档。**记的是解析后的结果**
        # （配置里选的 > 卡上的默认），不是配置里的原字：回头重判这一期时，判的
        # 标准必须是当初写它的那一种，而不是此刻配置里的那一种——上限挂在形式上
        # 头，两把尺子量同一份稿子，就会出现「写时过、判时不过」。
        pipeline.write_form_file(
            root, no,
            paradigms.resolve_form(script_engine.resolve_paradigm(proj_ctx, cfg),
                                   cfg.get("script.dialogue_form") or ""))
        script_engine.write_gate_report(layout.tmp_dir(root, no), g["report"])

    try:
        gen = script_engine.generate(
            material, cfg, llm,
            preset_key=preset_key,
            log=log, project=proj_ctx,
            # 前期回顾：读上一期的期主旨（地图行）与段主旨（上一期的旁挂规划档），
            # 组一句固定文本。开关关、或哪一路取不到，这里就是空列表——「不粘」
            # 的判断全在这一处，粘合那层只管站位（见 `glue_intro_outro`）。
            review=(pipeline.review_rows(base, proj_item, no, cfg, log=log)
                    if root is not None else []),
            # 中止只在轮次之间生效：正在跑的那次调用掐不断（本地模型一个请求
            # 就是一次不可分割的生成），但下一轮不必再开。
            should_stop=(lambda: bool(job.get("stop"))) if job is not None else None,
            draft_sink=save_draft,
            evidence=evidence,
            # 装箱要的两样：每节原文多重（按它定段边界）、按块取本段原文。
            sec_chars=sec_chars,
            material_of=material_of,
            # 取材标记（命中节号 → 家族）：只改提示词提醒，不动素材与配额。
            sec_flags=sec_flags,
            # 路线按项目模式定死，不给人选：成稿规划有地图与各节凝缩，走分段
            # （逐期配额、防重复、段级核账才有依据）；逐期即兴与单集是当场
            # 给料、出完即止，整篇一次写完就是这条路该有的样子。
            segmented=((proj_item or {}).get("plan_mode") == "mapped"))
    except (script_engine.ScriptError, LLMError) as e:
        return {"ok": False, "error": str(e), "logs": logs}

    script = gen["script"]
    rep = gen["report"]
    if root is not None:
        log("脚本已存入项目：脚本/%s.json" % layout.safe_no(no))
    est = duration_model.explain(script, cfg)
    # 内容检（语义 / 承诺链）在 generate() 内部已经跑过，结论就在 rep 里。
    # 从前这里再调一次，等于同一份脚本、同一段素材，模型看两遍、判两遍。
    return {"ok": True, "script": script, "report": rep, "check6": rep,
            "estimate": est, "logs": logs,
            "title": gen.get("title", ""),
            "planned_episodes": gen.get("planned_episodes"),
            "episode_no": proj_ctx.get("episode_no", ""),
            "degraded": gen.get("degraded", False),
            "attempt": gen.get("attempt", 1)}


def api_tts_preview(body):
    cfg = CFG.data()
    voice = body.get("voice") or cfg.get("tts.voice_a")
    speed = float(body.get("speed") or 1.0)
    local = cfg.get("tts.engine", "edge") in tts_engine.LOCAL_ENGINES

    # 本地引擎跑的是 Base 变体：它没有内置音色，试听同样要一段参考音频。
    # 项目里那份档案就是答案 —— 听到的正好是这一期会用的音色。
    ref = None
    if local:
        role = str(body.get("role") or "A").upper()
        pid = str(body.get("project") or "").strip()
        if pid:
            root = layout.project_dir(_projects_base(), pid)
            ref = tts_engine.voice_profiles(root).get(role)
        if ref is None:
            return {"ok": False, "error":
                    "本地引擎走音色克隆，试听需要一段参考音频。本项目还没有 %s 角"
                    "的音色档案 —— 首次合成时它会自动录一份，之后就能试听。"
                    % role}

    try:
        if local:
            # 试听也要服务在场。人点「试听」是想马上听到声音，不是想先看一行
            # 「服务没起来」；起停与合成同一套规矩，用完即还。
            tts_engine.acquire_service(cfg)
        return dict(ok=True, **tts_engine.preview(voice, speed, cfg, ref=ref))
    except tts_engine.TTSError as e:
        return {"ok": False, "error": str(e)}
    finally:
        if local:
            tts_engine.release_service()


def api_voices(refresh=False, engine=None):
    # engine 由请求方指定：界面里每个音色点位带着自己的 engine_scope，
    # 要按点位所属引擎取清单，而不是一律按当前引擎取——否则切到本地引擎时，
    # Edge 那组下拉也会被塞进本地音色名。
    cfg = CFG.data()
    eng = engine or cfg.get("tts.engine", "edge")
    try:
        voices = tts_engine.list_voices(eng, refresh=refresh, cfg=cfg)
        return {"ok": True, "engine": eng, "voices": voices}
    except tts_engine.TTSError as e:
        return {"ok": False, "engine": eng, "error": str(e), "voices": []}


def api_fonts():
    try:
        return {"ok": True, "fonts": assets_factory.list_fonts()}
    except assets_factory.AssetError as e:
        return {"ok": False, "error": str(e), "fonts": []}


FONT_MIME = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection"}


def font_file_bytes(family):
    """按族名取出本机这款字体的文件，返回 (字节, MIME)。

    界面要按每款字体自己的字形渲染候选项，浏览器得有字体文件。系统字体它按
    族名就能拿到，随包字体拿不到，只能由这里发过去。
    """
    fam = (family or "").strip()
    if not fam:
        raise ValueError("缺少字体族名")
    for f in assets_factory.list_fonts():
        if f["family"] == fam:
            with open(f["path"], "rb") as fh:
                ext = os.path.splitext(f["path"])[1].lower()
                return fh.read(), FONT_MIME.get(ext, "application/octet-stream")
    raise ValueError("本机没有这款字体：%s" % fam)


def api_engines():
    cfg = CFG.data()
    out = []
    for eng in ("edge", "qwen3tts"):
        ok, msg = tts_engine.engine_available(eng, cfg)
        out.append({"value": eng, "available": ok, "message": msg,
                    "current": cfg.get("tts.engine") == eng})
    return {"ok": True, "engines": out}


def api_tts_state():
    """本地语音环境的三态 + 服务在不在线。

    三态（环境 / 包 / 模型）回答的是**配置阶段**的问题：缺什么，就在配置页补什么。
    「在不在线」是另一件事——服务平时不用开，合成前会自动拉起，所以它不是「缺
    什么」，只作为状态显示。
    """
    cfg = CFG.data()
    st = tts_engine.local_state()
    ok, msg, _ = tts_engine.service_health(cfg, timeout=2)
    st["service"] = {"online": bool(ok), "message": msg}
    return {"ok": True, "state": st}


def api_tts_setup(body):
    """配置页的「搭建」按钮：建环境 → 装依赖 → 下模型 → 自检。

    走异步任务：这一趟是十几分钟和 4 GB 的下载，压在请求里浏览器会先超时。
    进度沿用现有的 /api/task/<id> 轮询与 /api/job/stop 中止，不另造一套。
    """
    cfg = CFG.data()
    if cfg.get("tts.engine") not in tts_engine.LOCAL_ENGINES:
        return {"ok": False, "error": "当前语音引擎不是 Qwen3-TTS，不需要本地环境。"}

    # setup_env.py 每步打一行 [N/5]，照它报进度。界面拿到的 stage 是中文步骤名。
    marks = (("[1/5]", "找机器上的 Python"), ("[2/5]", "建独立环境"),
             ("[3/5]", "装依赖"), ("[4/5]", "下模型"), ("[5/5]", "自检"))

    def _run(job):
        done = {"n": 0}

        def log(msg):
            pipeline.job_log(job, msg)
            for mark, name in marks:
                if msg.startswith(mark):
                    done["n"] += 1
                    job["stage"] = name
                    job["progress"] = min(0.99, done["n"] / float(len(marks)))
                    break

        log("搭建本地语音环境：建环境 → 装依赖 → 下模型 → 自检。")
        log("依赖约 4.8 GB、模型约 4 GB，第一次会慢；断了可以重跑，会接着来。")
        st = tts_engine.provision(log, should_stop=lambda: bool(job.get("stop")))
        if not st["ready"]:
            raise tts_engine.TTSError("搭建跑完了，环境仍然不完整，看上面的日志。")
        log("环境已就绪。合成时会自动拉起服务，整批跑完自动停掉、归还显存。")
        return {"state": st}

    return {"ok": True, "task_id": pipeline.run_async("tts-setup", _run,
                                                      "搭建本地语音环境")}


# --------------------------------------------------------------- 角色音色档案
# 本地引擎走 Base 变体：音色不在模型里，而在项目自己那份参考音频里。这两个接口
# 让「音色」这件事在界面上看得见、管得着 —— 否则它只是磁盘上一个没人知道的文件。

def _voice_project_root(body):
    """项目 id → 项目根目录。取不到就返回 (空串, 一句人话)。"""
    pid = str(body.get("project") or "").strip()
    if not pid:
        return "", "要先选一个项目。"
    root = layout.project_dir(_projects_base(), pid)
    if not os.path.isdir(root):
        return "", "项目目录不存在：%s" % root
    return root, ""


def _voice_tool(py, root, *extra):
    """组装 make_voice.py 的命令行。

    音色档案的全部判定（什么算「齐」、指纹怎么算、继承时拷哪些文件）只有
    tts_service/make_voice.py 那一份实现，这里不重写一遍 —— 两处各写一遍，
    迟早对「有档案」的理解走散。
    """
    return [py, os.path.join(ROOT, "tts_service", "make_voice.py"),
            "--project-dir", root, "--voice-dir", layout.DIR_VOICE, *extra]


def api_voice_profile(body):
    """本项目的角色音色档案实况（只读）。"""
    root, err = _voice_project_root(body)
    if err:
        return {"ok": False, "error": err}
    py = tts_engine.venv_python()
    if not py:
        return {"ok": False, "error":
                "本地语音环境还没建。到配置页点一次「搭建本地语音环境」。"}
    try:
        r = subprocess.run(_voice_tool(py, root, "--list", "--json"),
                           capture_output=True,
                           cwd=os.path.join(ROOT, "tts_service"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "读取音色档案失败：%s" % e}
    raw = (r.stdout or b"").decode("utf-8", "replace").strip()
    try:
        st = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"ok": False,
                "error": "音色档案工具返回了看不懂的内容：%s" % raw[-300:]}
    cfg = CFG.data()
    return {"ok": True, "status": st,
            "local": cfg.get("tts.engine", "edge") in tts_engine.LOCAL_ENGINES,
            "kind": tts_engine.service_kind(cfg),
            "voices": {"A": cfg.get("tts.qwen3tts_voice_a", ""),
                       "B": cfg.get("tts.qwen3tts_voice_b", "")}}


def api_voice_build(body):
    """给本项目录角色音色档案（新录一份，或从另一个项目继承）。

    走异步任务：录一次要加载内置音色模型，半分钟起步，压在请求里浏览器会先超时。
    进度沿用现有的任务轮询与中止机制。
    """
    cfg = CFG.data()
    if cfg.get("tts.engine", "edge") not in tts_engine.LOCAL_ENGINES:
        return {"ok": False, "error": "当前语音引擎不是 Qwen3-TTS，不需要音色档案。"}
    root, err = _voice_project_root(body)
    if err:
        return {"ok": False, "error": err}
    py = tts_engine.venv_python()
    if not py:
        return {"ok": False, "error":
                "本地语音环境还没建。到配置页点一次「搭建本地语音环境」。"}

    role = str(body.get("role") or "both").upper()
    role_arg = "both" if role in ("BOTH", "") else role
    if role_arg not in ("A", "B", "both"):
        return {"ok": False, "error": "角色只能是 A / B / both。"}
    source = str(body.get("from_project") or "").strip()
    force = bool(body.get("force"))
    voices = {"A": cfg.get("tts.qwen3tts_voice_a", ""),
              "B": cfg.get("tts.qwen3tts_voice_b", "")}

    src_root = ""
    if source:
        src_root = layout.project_dir(_projects_base(), source)
        if not os.path.isdir(src_root):
            return {"ok": False, "error": "要继承的项目不存在：%s" % src_root}

    def _run(job):
        if src_root:
            pipeline.job_log(job, "从「%s」继承音色档案 —— 复制文件，音色逐字节相同。"
                             % source)
            cmd = _voice_tool(py, root, "--json", "--role", role_arg,
                              "--from", src_root)
        else:
            pipeline.job_log(job, "用内置音色现录一份（A=%s，B=%s）。"
                             % (voices["A"] or "默认", voices["B"] or "默认"))
            cmd = _voice_tool(py, root, "--json", "--role", role_arg)
            if voices["A"]:
                cmd += ["--voice-a", voices["A"]]
            if voices["B"]:
                cmd += ["--voice-b", voices["B"]]
        if force:
            cmd.append("--force")

        proc = subprocess.Popen(cmd, cwd=os.path.join(ROOT, "tts_service"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        # 工具的进度走 stderr、结果留在 stdout：这里逐行报进度，最后才读那段
        # JSON，免得进度行混进结果里解析不了。
        for line in iter(proc.stderr.readline, ""):
            line = line.rstrip()
            if line:
                pipeline.job_log(job, line)
        tail = proc.stdout.read()
        proc.wait()
        if proc.returncode != 0:
            raise tts_engine.TTSError(tail.strip()[-400:] or "录音色档案失败，看上面的日志。")
        try:
            data = json.loads(tail)
        except Exception:  # noqa: BLE001
            raise tts_engine.TTSError("音色档案工具没返回可解析的结果。")
        if data.get("error"):
            raise tts_engine.TTSError(str(data["error"]))
        got = sorted(tts_engine.voice_profiles(root))
        if not got:
            raise tts_engine.TTSError("跑完了但没有档案落盘，检查 %s。"
                                      % layout.voice_dir(root))
        pipeline.job_log(job, "完成：%s 角的档案已就位。" % "、".join(got))
        return {"roles": got, "status": data.get("status", {})}

    return {"ok": True, "task_id": pipeline.run_async("voice-build", _run,
                                                      "录制角色音色")}


def api_backend_get():
    cfg = CFG.data()
    return {"ok": True, "backend": cfg.get("llm.backend"),
            "base_url": CFG.resolve_base_url(), "model": cfg.get("llm.model", "")}


def api_models(q=None):
    """拉取后端可用模型列表。

    允许带上候选的后端与地址先试连——切换后端时不该强迫用户先保存再试。
    """
    cfg = CFG.data()
    q = q or {}
    backend = q.get("backend") or cfg.get("llm.backend", "lm-studio")
    base_url = q.get("base_url")
    if base_url is None:
        base_url = resolve_base_url(dict(cfg, **{"llm.backend": backend}))
    try:
        llm = LLMClient(backend=backend, base_url=base_url,
                        api_key=cfg.get("llm.api_key", ""), model="",
                        timeout=min(60, int(cfg.get("llm.timeout", 3600))),
                        idle_timeout=min(60, int(cfg.get("llm.idle_timeout", 300))))
        ok, res = llm.list_models()
    except LLMError as e:
        return {"ok": False, "models": [], "error": str(e)}
    if not ok:
        return {"ok": False, "models": [], "error": res}
    return {"ok": True, "models": res, "backend": backend,
            "base_url": base_url or "",
            "message": ("发现 %d 个模型" % len(res)) if res else "后端未返回模型列表"}


def api_backend_post(body):
    patch = {
        "llm.backend": body.get("backend") or CFG.get("llm.backend"),
        "llm.base_url": body.get("base_url") or "",
        "llm.api_key": body.get("api_key") or "",
        "llm.model": body.get("model") or "",
    }
    rejected = CFG.update(patch)
    CFG.save()
    return {"ok": not rejected, "rejected": rejected}


def api_backend_test(body):
    cfg = CFG.data()
    if body.get("backend"):
        CFG.update({"llm.backend": body["backend"]})
    if body.get("base_url") is not None:
        CFG.update({"llm.base_url": body["base_url"]})
    if body.get("model") is not None:
        CFG.update({"llm.model": body["model"]})
    CFG.save()
    llm = make_llm(CFG.data())
    ok, msg = llm.test_connection()
    return {"ok": ok, "message": msg}


def _render_sync(body, job=None):
    """单期出片的同步实现。

    界面点一次合成、批任务里跑一期，走的都是这一处——两处各写一份的话，
    批量的口径迟早跟单期漂开（单期读盘上的脚本、批量却在现生成之类）。
    """
    cfg = CFG.data()
    _, errs = audio_engine.validate_params(cfg)
    if errs:
        raise RuntimeError("；".join(errs))
    # 项目模式下这里留空：期标题由 pipeline 从地图取（见 run_episode）。
    # 从前兜一个「未命名」，是个非空值，把后面整条取值链挡住——封面就印着
    # 「未命名」三个字出片了。
    title = (body.get("title") or "").strip()
    script = body.get("script") or []
    material = body.get("material") or ""
    do_video = bool(body.get("do_video", True))
    project_id = body.get("project_id") or ""
    episode_no = str(body.get("episode_no") or "").strip()

    # 没递脚本就按（项目, 期号）读盘上那一份——批量合成走的就是这条路。
    # 读不到就报错，不顺手现生成：批量合成的意思是「合成我已经定稿的那几期」，
    # 现场生成等于把审过的稿换成模型新写的另一份。
    if not script and project_id:
        proot = layout.project_dir(_projects_base(), project_id)
        script = pipeline.read_json(layout.script_file(proot, episode_no), None) or []
        if not script:
            raise RuntimeError("第 %s 期还没有脚本，先到「脚本」页把它生成出来。"
                               % (episode_no or "?"))

    # 不构造 LLM：合成端不写稿子。素材与模型都归脚本阶段，递进来只是为了
    # 不破坏既有调用口径，合成这一路不会碰它们，也就没有理由白连一次后端。
    return pipeline.run_episode(cfg, CALIB, material, title,
                                episode_no=episode_no,
                                script=script or None,
                                preset_key=body.get("preset"),
                                do_video=do_video,
                                project_dir=None, reuse=False, job=job,
                                project_id=project_id,
                                ack_script_issues=bool(
                                    body.get("ack_script_issues")))


def api_render(body):
    cfg = CFG.data()
    _, errs = audio_engine.validate_params(cfg)
    if errs:
        return {"ok": False, "error": "；".join(errs)}
    jid = pipeline.run_async("render", lambda job: _render_sync(body, job),
                             body.get("title") or "未命名")
    return {"ok": True, "task_id": jid}


# 连续几期栽在同一个原因上就停手。失败若是系统性的（模型后端挂了、合成服务
# 没起），跳过等于把同一个错误重复 N 遍：几小时白等，机器还一直占着。
BATCH_FAIL_STREAK = 3


def api_batch(body):
    """批量：勾了几期就跑几期。

    **串行**。语音合成与视频渲染吃满机器，两个一起跑只会互相拖慢，还可能
    爆显存——批量的价值在「不用守着」，不在「同时开工」。

    单期失败跳过、继续跑后面的；连续同一原因失败到阈值就停下，把已完成的
    记好账、把没做的留给下一次重选。
    """
    kind = body.get("kind") or "render"
    pid = body.get("project_id") or ""
    eps = [str(x).strip() for x in (body.get("episodes") or []) if str(x).strip()]
    try:
        _, item, root = _tree_of(pid)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not eps:
        return {"ok": False, "error": "没选期数"}
    if kind == "render":
        # 合成页只列有脚本的期；这里再挡一道，绕过界面直调接口也拦得住。
        missing = [no for no in eps
                   if not os.path.exists(layout.script_file(root, no))]
        if missing:
            return {"ok": False,
                    "error": "这些期还没有脚本，先到「脚本」页生成：%s"
                             % "、".join(missing)}

    def _run(job):
        done, failed = [], []
        streak, last_key = 0, ""
        total = len(eps)
        # 本地语音引擎：整批共用一次服务起停。逐期各起各停的话，每一期都要重新
        # 加载一遍权重（约十秒），二十一期就是白等三分多钟。
        blog = lambda m: pipeline.job_log(job, m)               # noqa: E731
        cfg0 = CFG.data()
        serve = (kind == "render"
                 and cfg0.get("tts.engine", "edge") in tts_engine.LOCAL_ENGINES)
        if serve:
            tts_engine.acquire_service(cfg0, blog)
        try:
            for i, no in enumerate(eps, 1):
                if job.get("stop"):
                    pipeline.job_log(job, "已中止：做完 %d 期，剩 %d 期没做"
                                          % (len(done), total - i + 1))
                    break
                job["batch"] = {"index": i, "total": total, "no": no}
                # 批任务里的进度报的是**本期**的进度，整批进度由界面按
                # (index-1+本期进度)/总期数 折算。进度口一律只增不减（见
                # pipeline._step），不在这里归零的话，第二期一开局就顶着
                # 上一期跑完时的高位，整期都看不出在动。
                job["progress"] = 0.0
                pipeline.job_log(job, "—— 第 %s 期（%d/%d）——" % (no, i, total))
                try:
                    if kind == "script":
                        # 直接调干活的那个函数，不再绕「起任务」的入口一圈：
                        # 批任务自己就是一个任务，里面再起一个，日志会分叉成两处，
                        # 「第几期跑到哪一轮」在界面上看不全。
                        res = _script_generate_work(
                            dict(body, project_id=pid, episode_no=no), job)
                        if not res.get("ok"):
                            raise RuntimeError(res.get("error") or "生成失败")
                        done.append(no)
                    else:
                        _render_sync(dict(body, project_id=pid, episode_no=no,
                                          script=[]), job=job)
                        done.append(no)
                    streak, last_key = 0, ""
                except Exception as e:                        # noqa: BLE001
                    msg = str(e)
                    failed.append({"no": no, "error": msg})
                    key = msg[:60]
                    streak = streak + 1 if key == last_key else 1
                    last_key = key
                    pipeline.job_log(job, "第 %s 期失败：%s" % (no, msg))
                    if streak >= BATCH_FAIL_STREAK:
                        pipeline.job_log(
                            job, "连续 %d 期同一个原因失败，停下——再跑下去只是把"
                                 "同一个错误重复一遍。" % streak)
                        break
        finally:
            if serve:
                tts_engine.release_service(blog)
        job["batch"] = {"index": total, "total": total, "no": ""}
        return {"kind": kind, "done": done, "failed": failed, "requested": total}

    jid = pipeline.run_async("batch", _run,
                             "%s · %d 期" % (item.get("name") or pid, len(eps)))
    return {"ok": True, "task_id": jid, "total": len(eps)}


def api_job_stop(body):
    """中止一个正在跑的任务。

    不是硬杀线程：只置一个标记，当前这一步做完就不再开新的一期。硬杀会把
    正写着的文件截断——半截的 manifest 比没有更坏，它看着像是做完了。
    """
    jid = body.get("task_id") or ""
    job = pipeline.get_job(jid)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    if job.get("status") != "running":
        return {"ok": False, "error": "任务已经结束了"}
    job["stop"] = True
    pipeline.job_log(job, "收到中止：当前这一步做完就停，不再开新的一期。")
    return {"ok": True}


def _projects_base():
    return os.path.join(ROOT, CFG.get("project.output_dir", "projects"))


def api_project_get(q=None):
    """项目登记表。scope=orphans 时只回磁盘上有产物却无归属的集目录。"""
    base = _projects_base()
    q = q or {}
    try:
        if q.get("scope") == "orphans":
            return {"ok": True, "orphans": project_store.orphans(base)}
        return {"ok": True, "projects": project_store.summary(base)}
    except project_store.ProjectError as e:
        return {"ok": False, "error": str(e), "projects": [], "orphans": []}


def api_project_post(body):
    """立项 / 改项目 / 归档 / 删除。

    归档只打标记，期目录与产物一律保留；删除连项目目录一起删掉，不可逆。
    """
    base = _projects_base()
    action = (body.get("action") or "create").strip()
    try:
        if action == "create":
            item = project_store.create(
                base, body.get("name") or "",
                plan_mode=(body.get("plan_mode") or "").strip(),
                program_name=body.get("program_name") or "",
                subtitle=body.get("subtitle") or "",
                # 受众也可以立项时就填。留空不是错：排地图那一步会补一版
                # （见 planner.MAP_SCHEMA），此后在编辑弹窗里改。
                audience=body.get("audience") or "",
                planned_episodes=body.get("planned_episodes"),
                style_preset=body.get("style_preset") or "",
                voice_a=body.get("voice_a") or "", voice_b=body.get("voice_b") or "",
                qwen_voice_a=body.get("qwen_voice_a") or "",
                qwen_voice_b=body.get("qwen_voice_b") or "",
                name_a=body.get("name_a") or "", name_b=body.get("name_b") or "",
                first_episode=body.get("first_episode") or "1",
                note=body.get("note") or "",
                paradigm=body.get("paradigm") or "",
                # 人给这档节目写的侧重（可留空）。它进排图提示词，管分组粒度：
                # 侧重的内容合得细、次要的合得粗。留空＝老口径，不放默认话。
                focus_note=body.get("focus_note") or "")
            return {"ok": True, "project": dict(item,
                                                progress=project_store.progress(item))}
        if action == "update":
            pid = body.get("id") or ""
            item = project_store.update(base, pid, body)
            return {"ok": True, "project": dict(item,
                                                progress=project_store.progress(item))}
        if action == "delete":
            pid = body.get("id") or ""
            # 「删掉磁盘上那个目录」这句话本身就是后果，所以由服务端回报删了
            # 什么、有多大——界面照实转述，不再自己编一句「已删除」。
            return dict({"ok": True}, **project_store.delete(base, pid))
        return {"ok": False, "error": "未知操作：%s" % action}
    except project_store.ProjectError as e:
        return {"ok": False, "error": str(e)}


def api_sources(q=None):
    """某项目的素材清单。"""
    base = _projects_base()
    pid = ((q or {}).get("project_id") or "").strip()
    if not pid:
        return {"ok": False, "error": "未指定项目。", "sources": []}
    try:
        return {"ok": True, "sources": _sources_view(base, pid)}
    except source_store.SourceError as e:
        return {"ok": False, "error": str(e), "sources": []}


def _probe_brief(ir):
    """探查 IR → 界面够用的一份摘要。界面只看中文与数字，键留给代码。"""
    lv = ir.get("level_counts") or {}
    kind = str(ir.get("kind") or "").strip() or "auto"
    if kind not in paradigms.PARADIGMS:
        kind = "auto"
    return {
        "segments": len(ir.get("segments") or []),
        "levels": len(lv),
        "level_counts": lv,
        "unit_level": int(ir.get("unit_level") or 1),
        "kind": kind,
        "kind_label": paradigms.get(kind)["label"],
        "reason": str(ir.get("classify_reason") or ""),
        "model": str(ir.get("probe_model") or ""),
        "body_chars": int(ir.get("body_chars") or 0),
        "capacity": int(ir.get("capacity") or 0),
        "est_episodes": int(ir.get("est_episodes") or 0),
        "warnings": list(ir.get("warnings") or []),
        "scanned": str(ir.get("scanned") or ""),
    }


def _sources_view(base, pid):
    """素材清单，各自附一份结构摘要。

    结构来源唯一化在 `probe`（`source_store` 只存取文本，不知道结构为何物）；
    两份数据在接口层合起来，不让仓储去读探查结果。
    """
    rows = []
    for r in source_store.list_sources(base, pid):
        row = dict(r)
        try:
            ir = probe.load(base, pid, r["id"])
        except probe.ProbeError:
            ir = None
        if ir:
            row["probe"] = _probe_brief(ir)
        rows.append(row)
    return rows


def api_source_post(body):
    """素材入库 / 移除。入库与脚本生成走同一个抽取入口。

    入库与补跑探查都带一次模型调用（类型判定），是分钟级的长活——与排图
    同款后台化：立刻回任务号，进度与日志走 /api/task 轮询。
    """
    base = _projects_base()
    pid = (body.get("project_id") or "").strip()
    action = (body.get("action") or "add").strip()
    if not pid:
        return {"ok": False, "error": "未指定项目。"}
    try:
        if action in ("add", "scan"):
            busy = _project_busy(pid)
            if busy:
                return {"ok": False, "error": busy}
            jid = pipeline.run_async("probe", lambda job: _probe_work(body, job),
                                     "素材探查", project=pid)
            return {"ok": True, "task_id": jid}
        if action == "remove":
            source_store.remove_source(base, pid, body.get("id") or "")
            return {"ok": True, "sources": _sources_view(base, pid)}
        return {"ok": False, "error": "未知操作：%s" % action}
    except (source_store.SourceError, ingest.IngestError,
            probe.ProbeError, LLMError) as e:
        return {"ok": False, "error": str(e)}


def _kind_label(kind):
    """范式键 → 中文名。界面只认中文，键留给代码。"""
    try:
        return paradigms.get(kind)["label"]
    except Exception:                                            # noqa: BLE001
        return kind or ""


def api_plan_post(body):
    """期数地图：排图 / 存图 / 插入建议 / 插入落库。

    插入分两步走：`insert_plan` 只回建议不落库，人看过、改过再 `insert_apply`。
    一步到位的写法看着省事，但插入点是模型定的，落错了要回退整个地图。
    """
    base = _projects_base()
    pid = (body.get("project_id") or "").strip()
    action = (body.get("action") or "").strip()
    logs = []
    try:
        item = project_store.find(base, pid) if pid else None
        if pid and item is None:
            return {"ok": False, "error": "项目不存在：%s" % pid, "logs": logs}
        if action == "map":
            # 排图是几十分钟的模型长活（逐节凝缩 + 分组 + 压比体检重排），压在
            # 请求里等与脚本生成是同一种病——改走同一套后台任务：立刻回任务号，
            # 进度与日志由 /api/task 轮询，结果形状不变，装在 job["result"] 里。
            busy = _project_busy(pid)
            if busy:
                return {"ok": False, "error": busy, "logs": logs}
            jid = pipeline.run_async("map", lambda job: _map_work(body, job),
                                     "排地图 · %s" % _project_title(item),
                                     project=pid)
            return {"ok": True, "task_id": jid}
        if action == "paradigms":
            return {"ok": True, "logs": logs,
                    "options": paradigms.options(),
                    "current": (item.get("paradigm") or "") if item else "",
                    # 侧重与素材类型同在「排图依据」这一档，界面上也是一起改的，
                    # 所以跟着同一次请求回来——分两次取，改到一半的界面会把
                    # 两个值配成不同的项目。
                    "focus_note": (item.get("focus_note") or "") if item else ""}
        if action == "save":
            item = project_store.set_map(base, pid, body.get("episodes") or [])
            return {"ok": True, "logs": logs,
                    "project": dict(item,
                                    progress=project_store.progress(item))}
        if action == "insert_plan":
            # 与排图同病同治：插入建议也是一次模型长调用（还要先探查新素材）。
            busy = _project_busy(pid)
            if busy:
                return {"ok": False, "error": busy, "logs": logs}
            jid = pipeline.run_async(
                "insert-plan", lambda job: _insert_plan_work(body, job),
                "插入建议 · %s" % _project_title(item), project=pid)
            return {"ok": True, "task_id": jid}
        if action == "insert_apply":
            anchor_no = body.get("anchor_no") or ""
            rows = [r for r in (body.get("episodes") or []) if isinstance(r, dict)]
            # 期号由后端编，调用方传什么号都不作数。这套规则若在排图、插入、
            # 界面三处各写一遍，迟早有一处先漂，而漂出来的期号在产物上看着都正常。
            nos = planner.assign_branch_nos(item, anchor_no, len(rows))
            for r, no in zip(rows, nos):
                r["no"] = no
            item = project_store.insert_branches(base, pid, anchor_no, rows)
            return {"ok": True, "logs": logs, "episodes": nos,
                    "project": dict(item,
                                    progress=project_store.progress(item))}
        return {"ok": False, "error": "未知操作：%s" % action, "logs": logs}
    except (project_store.ProjectError, planner.PlanError, probe.ProbeError,
            source_store.SourceError, LLMError) as e:
        return {"ok": False, "error": str(e), "logs": logs}


# ---------------------------------------------------------------- 项目长活
# 探查 / 排图 / 插入建议都是「模型慢慢算」的活，与脚本生成同病同治：入口只起
# 任务、立刻回任务号，进度与日志走 /api/task 轮询，结果装在 job["result"] 里，
# 形状与同步时代一致。真正干活的抽成 work 函数（job 缺席也能跑），测试里
# mock 掉它们就不必烧真模型。


def _project_busy(pid):
    """该项目是否有运行中的模型长活，有就给一句人说得清的拒绝理由。

    探查与排图写的是同一批 `<sid>.probe.json`（凝缩结果落在探查文件里），
    并行跑会互相踩——一边刚落盘、另一边按旧结构重写同一份文件。
    """
    running = pipeline.running_for(pid, kinds=("probe", "map", "insert-plan"))
    if not running:
        return ""
    j = running[0]
    return ("该项目有一个「%s」任务正在跑——探查与排图写的是同一批探查文件，"
            "不能并行；等它结束再试。" % (j.get("title") or j.get("kind") or "长活"))


def _project_title(item):
    """项目卡片标题：节目名优先，缺席退 id。"""
    return ((item or {}).get("program_name") or (item or {}).get("id")
            or "未命名项目")


def _job_progress(job):
    """把 plan_map / scan_project 的进度回调接到 job 上。"""
    def _p(text, frac):
        if not job:
            return
        job["stage"] = str(text)
        job["progress"] = min(0.98, max(0.0, float(frac)))
    return _p


def _map_work(body, job=None):
    """排地图（真的干活的那个）。返回给前端的结果形状与同步时代一致。

    排图交给模型的是 {episodes, warnings, capacity, planned, kind}，不是裸期
    列表；落库只把 episodes 递给 set_map——整张 dict 递过去会在校验层被当成
    字符串列表丢掉，而界面上只看得到一句「第 1 项不是对象」。
    """
    base = _projects_base()
    cfg = CFG.data()
    pid = (body.get("project_id") or "").strip()
    item = project_store.find(base, pid)
    if item is None:
        raise project_store.ProjectError("项目不存在：%s" % pid)
    pcfg = project_store.apply_to_config(cfg, item)
    res = planner.plan_map(base, pid, item, pcfg, make_llm(pcfg),
                           log=(lambda m: pipeline.job_log(job, m)) if job else None,
                           force=bool(body.get("force")),
                           progress=_job_progress(job))
    item = project_store.set_map(base, pid, res["episodes"],
                                 note=body.get("note") or "",
                                 # 受众只在项目里还空着时才落进去（见 set_map）：
                                 # 人改过就以人为准，重排不该把人的话顶掉。
                                 audience=res.get("audience"))
    return {"ok": True, "warnings": res["warnings"],
            "capacity": res["capacity"],
            "ratio": res.get("ratio", 5),
            "planned": res["planned"],
            "kind": res["kind"],
            "kind_label": _kind_label(res["kind"]),
            "project": dict(item,
                            progress=project_store.progress(item))}


def _insert_plan_work(body, job=None):
    """插入建议（真的干活的那个）。只回建议不落库，人看过、改过再 insert_apply。"""
    base = _projects_base()
    cfg = CFG.data()
    pid = (body.get("project_id") or "").strip()
    item = project_store.find(base, pid)
    if item is None:
        raise project_store.ProjectError("项目不存在：%s" % pid)
    pcfg = project_store.apply_to_config(cfg, item)
    sug = planner.plan_insert(base, pid, item, pcfg, make_llm(pcfg),
                              source_ids=body.get("source_ids"),
                              log=(lambda m: pipeline.job_log(job, m)) if job else None,
                              progress=_job_progress(job))
    return {"ok": True, "warnings": sug["warnings"], "suggestion": sug}


def _probe_work(body, job=None):
    """素材入库+探查 / 补跑探查（真的干活的那个）。

    入库本身是纯代码，跟着探查一起进任务：docx 解析大文件也就几秒，而
    探查是分钟级的长活——一个任务说完一整件事，界面只需盯一个进度。
    探查失败不回滚入库：素材已经在库里，补跑探查就是给这种情况留的。
    """
    base = _projects_base()
    cfg = CFG.data()
    pid = (body.get("project_id") or "").strip()
    action = (body.get("action") or "add").strip()
    log = (lambda m: pipeline.job_log(job, m)) if job else None
    progress = _job_progress(job)
    if action == "add":
        res = extract_material(body)
        row = source_store.add_source(
            base, pid,
            body.get("name") or res["meta"].get("source") or "",
            res["text"], note=body.get("note") or "",
            # marks（docx 的标题样式表）只在读文件那一刻存在。不跟着正文
            # 入库，第二次读素材就只剩纯文本，样式证据白记一场，docx 实际
            # 退化成一个更大的 txt。extract_material 已经把它提出来了，
            # 这里必须接住。
            marks=res.get("marks") or [])
        # 入库即探查。结构与类型在这一步定下来并落盘冻结：排图直接用冻结
        # 结果，不必再等一次；排图失败（模型不可用、输出被截断）也不至于
        # 把探查赔进去。已在库的素材走缓存，不重扫也不重判。
        pr = probe.scan_project(base, pid, cfg, llm=make_llm(cfg),
                                paradigms_map=paradigms.PARADIGMS,
                                log=log, progress=progress)
        return {"ok": True, "source": row,
                "probe": _probe_brief(
                    (pr.get("sources") or {}).get(row["id"]) or {}),
                "sources": _sources_view(base, pid)}
    if action == "scan":
        # 补跑探查：旧版本入库没探过、或人改了判定依据要重判。已在库且有
        # 结果的走缓存，不重扫。
        pr = probe.scan_project(base, pid, cfg, llm=make_llm(cfg),
                                paradigms_map=paradigms.PARADIGMS,
                                force=bool(body.get("force")),
                                log=log, progress=progress)
        return {"ok": True, "capacity": pr["capacity"],
                "body_chars": pr["body_chars"],
                "est_episodes": pr["est_episodes"],
                "warnings": pr["warnings"],
                "sources": _sources_view(base, pid)}
    raise source_store.SourceError("未知操作：%s" % action)


def api_assets_regen(body):
    """重生成背景与封面。

    归属项目时按（项目, 期号）取同一份画面文字——期标题照样取自地图，
    与出片走同一条取值路。否则这里重生成的封面会和出片那张印着不同的标题。
    """
    cfg = CFG.data()
    base = os.path.join(ROOT, cfg.get("project.output_dir", "projects"))
    pid = body.get("project_id") or ""
    episode_no = str(body.get("episode_no") or "").strip()
    item = project_store.find(base, pid) if pid else None
    title = (body.get("title") or "").strip()
    if item:
        cfg = project_store.apply_to_config(cfg, item)
        for e in project_store.map_episodes(item):
            if str(e.get("no") or "").strip() == episode_no:
                title = str(e.get("title") or "").strip() or title
                break
    proj = body.get("project_dir")
    if proj:
        target = safe_join(base, proj)
    else:
        target = pipeline.new_project_dir(base, title or "未命名")
    lines_text = pipeline.build_lines_text(cfg, title, episode_no)
    # 预览：背景与封面都落在这个临时目录里，不带期号前缀（文件名与从前一致）。
    built = assets_factory.build_all(cfg, {"bg": target, "cover": target}, lines_text)
    return {"ok": True, "project_dir": os.path.relpath(target, ROOT), "assets": built}


def api_projects():
    base = os.path.join(ROOT, CFG.get("project.output_dir", "projects"))
    return {"ok": True, "projects": pipeline.list_projects(base)}


def api_report(root_name, no):
    """读某一期的校验报告。`root_name` 是树根（项目 id 或单集目录），`no` 是期号。
    同时回产物的路径表：报告本来就要为这一期算 `episode_files`，扔掉它前端就
    只能画门禁表——历史期的成片试听靠这张表才有入口。"""
    base = os.path.join(ROOT, CFG.get("project.output_dir", "projects"))
    tree = safe_join(base, root_name)
    ep = layout.episode_files(tree, no)
    rep = pipeline.read_json(ep["report"])
    if rep is None:
        rep = pipeline.build_report(ep, CFG.data())
    return {"ok": True, "report": rep, "episode_files": ep}


def api_continue(body):
    """续做某一期：树根 + 期号才指得到一期。"""
    root_name = body.get("root") or body.get("project_dir") or ""
    no = str(body.get("episode_no") or "").strip()
    base = os.path.join(ROOT, CFG.get("project.output_dir", "projects"))
    root = safe_join(base, root_name)
    if not os.path.isdir(root):
        return {"ok": False, "error": "这一期所属的目录不存在"}
    if not no:
        return {"ok": False, "error": "没给期号，不知道续做哪一期"}

    def _run(job):
        # 不连 LLM：续跑也是合成，合成端不写稿子。
        return pipeline.continue_episode(CFG.data(), CALIB, root, no, job=job)
    jid = pipeline.run_async("continue", _run, "%s 第 %s 期" % (root_name, no))
    return {"ok": True, "task_id": jid}


# ------------------------------------------------------------------ 路由
ROUTES_GET = {
    "/api/config": lambda q, b: api_config_get(),
    "/api/voices": lambda q, b: api_voices(refresh=q.get("refresh") == "1",
                                          engine=q.get("engine")),
    "/api/fonts": lambda q, b: api_fonts(),
    "/api/engines": lambda q, b: api_engines(),
    "/api/tts/state": lambda q, b: api_tts_state(),
    "/api/backends": lambda q, b: api_backend_get(),
    "/api/models": lambda q, b: api_models(q),
    "/api/projects": lambda q, b: api_projects(),
    "/api/project": lambda q, b: api_project_get(q),
    "/api/sources": lambda q, b: api_sources(q),
    "/api/scripts": lambda q, b: api_scripts(q),
    "/api/script": lambda q, b: api_script_get(q),
}

ROUTES_POST = {
    "/api/config": lambda b: api_config_post(b),
    "/api/pickfile": lambda b: api_pickfile(b),
    "/api/backend": lambda b: api_backend_post(b),
    "/api/backend/test": lambda b: api_backend_test(b),
    "/api/project": lambda b: api_project_post(b),
    "/api/ingest": lambda b: api_ingest(b),
    "/api/source": lambda b: api_source_post(b),
    "/api/plan": lambda b: api_plan_post(b),
    "/api/estimate": lambda b: api_estimate(b),
    "/api/script/generate": lambda b: api_script_generate(b),
    "/api/script/gate": lambda b: api_script_gate(b),
    "/api/script/issues": lambda b: api_script_issues(b),
    "/api/script/save": lambda b: api_script_save(b),
    "/api/tts/preview": lambda b: api_tts_preview(b),
    "/api/tts/setup": lambda b: api_tts_setup(b),
    "/api/voice/profile": lambda b: api_voice_profile(b),
    "/api/voice/build": lambda b: api_voice_build(b),
    "/api/render": lambda b: api_render(b),
    "/api/batch": lambda b: api_batch(b),
    "/api/job/stop": lambda b: api_job_stop(b),
    "/api/assets/regenerate": lambda b: api_assets_regen(b),
    "/api/continue": lambda b: api_continue(b),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PodcastMaker/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs, unquote
        # 路径要先解码：项目内各阶段的子目录是中文名（「素材」「音视频」…），
        # 浏览器发出去按百分号编码，不解码就是拿一串 %E9… 去找目录。
        u = urlparse(self.path)
        path = unquote(u.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path.startswith("/api/task/"):
            jid = path.rsplit("/", 1)[-1]
            job = pipeline.get_job(jid)
            if not job:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            return self._json({"ok": True, "job": job})
        if path == "/api/jobs":
            return self._json({"ok": True, "jobs": pipeline.list_jobs()})
        if path.startswith("/api/bgm/"):
            # BGM 试听：配置页档位下拉旁的播放按钮从这里取原始循环素材。
            preset = path[len("/api/bgm/"):]
            try:
                target = assets_factory.bgm_resource(preset)
            except (ValueError, RuntimeError) as e:
                return self._json({"ok": False, "error": str(e)}, 404)
            with open(target, "rb") as f:
                return self._send(200, f.read(), "audio/wav")
        if path.startswith("/api/report/"):
            # 一期由「树根 + 期号」定位，一段路径指不到一期。
            rest = path[len("/api/report/"):]
            parts = [p for p in rest.split("/") if p]
            if len(parts) != 2:
                return self._json({"ok": False,
                                   "error": "报告地址要写成 /api/report/<树根>/<期号>"}, 400)
            try:
                return self._json(api_report(parts[0], parts[1]))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if path.startswith("/api/file/"):
            rel = path[len("/api/file/"):]
            base = os.path.join(ROOT, CFG.get("project.output_dir", "projects"))
            try:
                target = safe_join(base, rel)
            except ValueError:
                return self._json({"ok": False, "error": "路径越界"}, 400)
            if not os.path.isfile(target):
                return self._json({"ok": False, "error": "文件不存在"}, 404)
            ext = os.path.splitext(target)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".mp4": "video/mp4",
                     ".mp3": "audio/mpeg", ".wav": "audio/wav", ".srt": "text/plain; charset=utf-8",
                     ".md": "text/plain; charset=utf-8", ".json": "application/json",
                     ".ass": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
            with open(target, "rb") as f:
                return self._send(200, f.read(), ctype)

        if path == "/api/fontfile":
            try:
                data, ctype = font_file_bytes(q.get("family") or "")
            except (ValueError, OSError) as e:
                return self._json({"ok": False, "error": str(e)}, 404)
            # 字体文件内容不变，允许多天缓存：下拉一展开要取十几款，每次开页面
            # 都重下一遍是纯浪费，几十兆一款的量级拖不起。
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            return self.wfile.write(data)

        fn = ROUTES_GET.get(path)
        if not fn:
            return self._json({"ok": False, "error": "未知路由"}, 404)
        try:
            return self._json(fn(q, None))
        except Exception as e:
            return self._json({"ok": False, "error": str(e),
                               "trace": traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)

        fn = ROUTES_POST.get(path)
        if not fn:
            return self._json({"ok": False, "error": "未知路由"}, 404)
        try:
            return self._json(fn(body))
        except Exception as e:
            return self._json({"ok": False, "error": str(e),
                               "trace": traceback.format_exc()[-800:]}, 500)


def run_server(host="0.0.0.0", port=8811, pidfile="", **kwargs):
    if kwargs.get("backend") or kwargs.get("base_url") or kwargs.get("model"):
        patch = {}
        if kwargs.get("backend"):
            patch["llm.backend"] = kwargs["backend"]
        if kwargs.get("base_url"):
            patch["llm.base_url"] = kwargs["base_url"]
        if kwargs.get("model"):
            patch["llm.model"] = kwargs["model"]
        if kwargs.get("api_key"):
            patch["llm.api_key"] = kwargs["api_key"]
        CFG.update(patch)
        CFG.save()

    if pidfile:
        with open(pidfile, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    httpd = ThreadingHTTPServer((host, port), Handler)
    print("=" * 62)
    print("  Podcast Maker  v%s" % VERSION)
    print("  播客制作智能体 · 脚本 → 声音 → 字幕 → 画面 → 产物")
    print("=" * 62)
    print("  界面地址   http://127.0.0.1:%d" % port)
    print("  配置优先   命令行 > config.json > 参数表默认值")
    print("  停止服务   Ctrl+C")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        if pidfile and os.path.exists(pidfile):
            os.remove(pidfile)


# ------------------------------------------------------------------ 页面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>播客制作智能体</title>
<style>
:root{
  --bg:#0d1117; --panel:#151b23; --panel2:#1c232c; --panel3:#212a34;
  --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.16);
  --fg:#e6edf3; --fg2:#9aa4af; --fg3:#6b7681;
  --gold:#c9a45c; --blue:#6fa8dc; --red:#e5534b; --green:#3fb950; --amber:#d29922;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:13px/1.65 "Microsoft YaHei","PingFang SC",-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
#root{display:flex;flex-direction:column;height:100vh}

header{display:flex;align-items:center;gap:14px;padding:0 22px;height:54px;border-bottom:1px solid var(--line);background:var(--panel);flex:none}
header h1{font-size:14px;font-weight:500;margin:0;letter-spacing:.4px}
header .ver{color:var(--fg3);font-size:12px}
header .spacer{flex:1}
header .stat{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--fg2);max-width:46vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--fg3);flex:none}
.dot.on{background:var(--green)}.dot.off{background:var(--red)}

nav{display:flex;gap:2px;padding:0 22px;border-bottom:1px solid var(--line);background:var(--panel);flex:none}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:11px 18px;color:var(--fg2);cursor:pointer;transition:color .12s}
nav button:hover{color:var(--fg)}
nav button.on{color:var(--gold);border-bottom-color:var(--gold)}

main{flex:1;overflow:auto;padding:22px}
.tab{display:none}.tab.on{display:block}
.cols{display:flex;gap:20px;align-items:flex-start}
.col-l{width:404px;flex:none}
.col-r{flex:1;min-width:0}

.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.card>h2{font-size:13px;font-weight:500;margin:0 0 4px;display:flex;align-items:center;gap:8px}
.card>h2 .tag{font-size:11px;color:var(--fg3);font-weight:400}
.card>p.note{margin:0 0 14px;font-size:12px;color:var(--fg3);line-height:1.6}
/* 网格：默认按可用宽度自动铺列（用在脚本页/合成页那两条窄栏里）。 */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px 20px;margin-top:14px}
/* 配置页专用网格：列数是固定的四档，只按可用宽度整档切换，不随窗口连续伸缩——
   连续伸缩会让同一张卡在不同窗口下排到完全不同的位置，读起来没有规矩。
   一行填不满就让它空着（左对齐）：五个控件＝第一行四个、第二行最左一个，后面
   三格空着。**不许用 dense 回头填补空档**——回头填会把排在后头的控件提前来填空，
   同类控件就被拆散到两行里去了。 */
.grid.g4{grid-template-columns:repeat(4,minmax(0,1fr));gap:12px 20px;align-items:start}
@media(max-width:1420px){.grid.g4{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1060px){.grid.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:720px){.grid.g4{grid-template-columns:1fr}}

/* 卡内功能区的小标题。一张卡里常混着好几件事（LLM 那张卡既有「连到哪个后端、
   用哪个模型」，也有「生成参数」「超时」）：只按类型分行，这几件事会被切成几段，
   人看不出谁和谁是一组。所以在卡内再切一层功能区，区内才按类型分行。
   一张卡只讲一件事时**不画**这行小标题，与从前完全一样。 */
.card .zhead{font-size:11px;color:var(--fg3);letter-spacing:.3px;
             margin:16px 0 0;padding-bottom:5px;border-bottom:1px solid var(--line)}
.card .zhead+.grid{margin-top:10px}
/* 配置页每一格固定四段：标题行 / 控件槽 / 刻度槽 / 说明。
   头两段是硬高度，滑块、下拉、输入框、开关因此落在同一条水平线上——「上下
   对不齐」就是因为从前它们各按各的自然高度排（滑块 18px、下拉 37px），谁也
   不知道谁在哪。没有刻度的项也留一格空的刻度槽，那样连格与格的底部都对得上。
   骨架只管配置页这张网格：脚本页那两条窄栏照旧按内容自然排。 */
.grid.g4>.f{display:flex;flex-direction:column;gap:3px;min-width:0}
/* 一格一控件。跨列的只剩多行输入（textarea）——250px 宽写不下一段文字。
   从前还按说明字数给「跨两列」，结果是同一张卡里滑杆有的占两格有的占一格、
   下拉同理，行的起点与终点全乱。说明长短只该影响这一格**多高**，不该动它多宽。 */
.grid.g4>.f.wide{grid-column:span 2}
@media(max-width:720px){.grid.g4>.f.wide{grid-column:span 1}}
/* 形态一变就另起一行：滑杆排完，开关从下一行第 1 格起；开关只剩两个，后面两格
   就空着，紧跟着的下拉不许顶上来填空——一顶上来，两种控件又混在一行了。 */
.grid>.f.kstart{grid-column-start:1}
.grid.g4>.f>label{font-size:12px;color:var(--fg2);display:flex;align-items:center;
                  justify-content:space-between;gap:10px;height:22px;flex:none}
.grid.g4>.f>label .val{color:var(--gold);font-variant-numeric:tabular-nums;flex:none;text-align:right}
.grid.g4>.f>label .hint{color:var(--fg3);font-size:11px;font-weight:400;min-width:0;
                        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grid.g4>.f .hold{height:34px;display:flex;align-items:center;flex:none}
.grid.g4>.f .hold>*{width:100%}
.grid.g4>.f .hold>label.sw,.grid.g4>.f .hold>button{width:auto;flex:none}
.grid.g4>.f .hold.pv>select,.grid.g4>.f .hold.pv>input{flex:1;width:auto;min-width:0}
.grid.g4>.f .hold.pv>button{margin-left:8px}
.grid.g4>.f .scale{display:flex;justify-content:space-between;font-size:10px;color:var(--fg3);
                   font-variant-numeric:tabular-nums;height:15px;align-items:center;
                   flex:none;margin-top:0}
.grid.g4>.f .desc{font-size:11px;color:var(--fg3);line-height:1.55;max-width:900px}
.grid.g4>.f>audio{height:32px;max-width:100%;margin-top:4px}
.f{display:flex;flex-direction:column;gap:6px;min-width:0}
.f>label{font-size:12px;color:var(--fg2);display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.f>label .val{color:var(--gold);font-variant-numeric:tabular-nums;flex:none;text-align:right}
.f>label .hint{color:var(--fg3);font-size:11px;font-weight:400}
.f .scale{display:flex;justify-content:space-between;font-size:10px;color:var(--fg3);font-variant-numeric:tabular-nums;margin-top:-2px}
.f .desc{font-size:11px;color:var(--fg3);line-height:1.55}
.f .sub{font-size:11px;color:var(--fg3);line-height:1.55}

input[type=text],input[type=number],select,textarea{
  background:var(--panel2);border:1px solid var(--line);border-radius:6px;
  padding:7px 10px;outline:none;width:100%;transition:border-color .12s}
input[type=text]:focus,input[type=number]:focus,select:focus,textarea:focus{border-color:var(--gold)}
select{appearance:none;cursor:pointer;padding-right:28px;
  background-image:linear-gradient(45deg,transparent 50%,var(--fg3) 50%),linear-gradient(135deg,var(--fg3) 50%,transparent 50%);
  background-position:calc(100% - 15px) 52%,calc(100% - 10px) 52%;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat}
select option{background:var(--panel3);color:var(--fg)}
select option:disabled{color:var(--fg3)}
/* 单行控件一律 34px：与网格里那条控件槽同高，横向看是一条线。 */
input[type=text],input[type=number],select{height:34px}
textarea{resize:vertical;min-height:110px;line-height:1.7;font-family:inherit}
input[type=range]{width:100%;accent-color:var(--gold);height:20px;cursor:pointer;margin:0}

button.btn{background:var(--panel2);border:1px solid var(--line2);border-radius:6px;padding:7px 15px;cursor:pointer;transition:.12s}
button.btn:hover{border-color:var(--gold)}
button.btn.primary{background:var(--gold);color:#1a1206;border-color:var(--gold);font-weight:500}
button.btn.primary:hover{background:#d8b26c}
button.btn.danger{border-color:var(--red);color:var(--red)}
button.btn:disabled{opacity:.42;cursor:not-allowed}
button.mini{background:none;border:1px solid var(--line);border-radius:5px;padding:3px 9px;font-size:12px;cursor:pointer;color:var(--fg2);transition:.12s;white-space:nowrap}
button.mini:hover{color:var(--gold);border-color:var(--gold)}
button.mini:disabled{opacity:.42;cursor:not-allowed}
button.mini:disabled:hover{color:var(--fg2);border-color:var(--line)}
/* 删除要点两下：第一下只是把按钮点亮，第二下才动手。删除连项目目录一起删，
   误点没有回头路，所以不提供「一键完成」的省事。 */
button.mini.del:hover{color:var(--red);border-color:var(--red)}
button.mini.del.arm{color:#fff;background:var(--red);border-color:var(--red)}
button.mini.del.arm:hover{color:#fff;background:#c9403a;border-color:#c9403a}
.btn-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}

/* 期数多选：收起时只占一行，点开才铺开。一屏几十期的项目摊开摆会顶掉半屏，
   而选期是偶尔才用一次的事。 */
.pick{position:relative}
.picksum{width:100%;text-align:left;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:7px 10px;cursor:pointer;color:var(--fg);transition:.12s}
.picksum:hover{border-color:var(--line2)}
.picksum .cnt{color:var(--gold)}
.pickbox{display:none;position:absolute;z-index:40;left:0;right:0;top:100%;margin-top:5px;max-height:300px;overflow:auto;background:var(--panel3);border:1px solid var(--line2);border-radius:8px;box-shadow:0 12px 32px rgba(0,0,0,.45);padding:6px}
.pickbox.on{display:block}
.pickbox .empty{padding:10px 8px;text-align:left}
.pickrow{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:5px;cursor:pointer}
.pickrow:hover{background:var(--panel2)}
.pickrow .no{color:var(--fg3);min-width:58px;flex:none}
.pickrow .ti{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pickrow .badge{font-size:11px;color:var(--fg3);border:1px solid var(--line);border-radius:4px;padding:0 5px;flex:none}
.req{font-size:10px;color:#d05a4e;border:1px solid rgba(208,90,78,.5);border-radius:4px;padding:0 4px;margin-left:6px;font-weight:400;vertical-align:1px}
.pickrow .badge.ok{color:var(--gold);border-color:rgba(201,164,92,.4)}
.pickrow .peek{color:var(--blue);font-size:12px;flex:none}
.pickfoot{display:flex;gap:10px;align-items:center;padding:7px 8px 2px;border-top:1px solid var(--line);margin-top:5px;font-size:12px}
.pickfoot a{color:var(--fg2);cursor:pointer}
.pickfoot a:hover{color:var(--gold)}

/* 字体下拉：自绘一层。原生 select 的选项不接受自定义字体——浏览器直接忽略
   选项上的 font-family，整列字长得一模一样，选字体就成了盲选。 */
.fpick{position:relative}
.fpick .fsel{width:100%;display:flex;align-items:center;gap:10px;padding:6px 10px;height:34px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;cursor:pointer;color:var(--fg);text-align:left;transition:.12s}
.fpick .fsel:hover{border-color:var(--line2)}
.fpick .fsel .fsample{flex:1;font-size:16px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fpick .fsel .fname{color:var(--fg3);font-size:11px;white-space:nowrap;flex:none}
.fpick .fbox{display:none;position:absolute;z-index:40;left:0;right:0;top:100%;margin-top:5px;max-height:330px;overflow:auto;background:var(--panel3);border:1px solid var(--line2);border-radius:8px;box-shadow:0 12px 32px rgba(0,0,0,.45);padding:6px}
.fpick.open .fbox{display:block}
.fpick .fopt{display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:5px;cursor:pointer}
.fpick .fopt:hover{background:var(--panel2)}
.fpick .fopt.on{background:rgba(201,164,92,.13)}
.fpick .fopt.off{opacity:.36;cursor:not-allowed}
.fpick .fopt.off:hover{background:none}
.fpick .fopt .fs{flex:1;font-size:16px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fpick .fopt .fn{color:var(--fg3);font-size:11px;white-space:nowrap;flex:none}
.fpick .fopt .fk{color:var(--gold);font-size:11px;white-space:nowrap;flex:none}

.sw{display:inline-flex;align-items:center;gap:9px;cursor:pointer;user-select:none;font-size:12px;color:var(--fg2)}
.sw i{width:34px;height:18px;border-radius:9px;background:var(--panel2);border:1px solid var(--line2);position:relative;transition:.15s;flex:none}
.sw i::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--fg3);transition:.15s}
.sw.on i{background:rgba(201,164,92,.25);border-color:var(--gold)}
.sw.on i::after{left:18px;background:var(--gold)}
.sw.on{color:var(--fg)}

table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--fg2);font-weight:400;white-space:nowrap}
td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}

.gauge{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.gauge .g{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.gauge .g .k{font-size:11px;color:var(--fg2)}
.gauge .g .v{font-size:20px;font-variant-numeric:tabular-nums;margin-top:3px;line-height:1.3}
.gauge .g .s{font-size:11px;color:var(--fg3);margin-top:2px}

.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.dim{color:var(--fg3)}

.gate{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--line);font-size:12px}
.gate:last-child{border-bottom:0}
.gate .mk{width:14px;text-align:center;flex:none}
.gate .lb{width:118px;flex:none;color:var(--fg2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gate .dt{flex:1;color:var(--fg3);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gate .lv{font-size:11px;color:var(--fg3);flex:none;white-space:nowrap}

.toast-wrap{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:8px;z-index:60}
.toast{background:var(--panel3);border:1px solid var(--line2);border-left:3px solid var(--gold);border-radius:7px;padding:10px 15px;max-width:380px;font-size:12px;box-shadow:0 8px 28px rgba(0,0,0,.5);animation:sl .18s ease}
.toast.err{border-left-color:var(--red)}.toast.ok{border-left-color:var(--green)}
@keyframes sl{from{transform:translateX(18px);opacity:0}to{transform:none;opacity:1}}
.mask{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:70}
.mask.on{display:flex}
.modal{background:var(--panel);border:1px solid var(--line2);border-radius:10px;padding:20px 22px;min-width:380px;max-width:560px}
.modal h3{margin:0 0 12px;font-size:14px;font-weight:500}
.modal .body{font-size:12px;color:var(--fg2);margin-bottom:18px;white-space:pre-wrap;max-height:46vh;overflow:auto;line-height:1.7}
.modal .acts{display:flex;justify-content:flex-end;gap:10px}
.modal.wide{max-width:1040px;width:94vw}
.modal .body.wide{max-height:64vh;white-space:normal}
.mode-pick{display:flex;gap:8px;margin-bottom:8px}
.mode-pick button{flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:9px 11px;cursor:pointer;color:var(--fg2);font-size:12px;text-align:left;line-height:1.55;transition:.12s}
.mode-pick button:hover{border-color:var(--line2)}
.mode-pick button.on{border-color:var(--gold)}
.mode-pick b{display:block;font-size:12px;font-weight:500;margin-bottom:3px;color:var(--fg2)}
.mode-pick button.on b{color:var(--gold)}
.mode-pick span{font-size:11px;color:var(--fg3)}
.map-tbl{width:100%;border-collapse:collapse;font-size:12px}
.map-tbl th{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line2);color:var(--fg3);font-weight:400;white-space:nowrap}
.map-tbl td{padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.map-tbl tr.branch td:first-child{color:var(--gold)}
.map-tbl input,.map-tbl textarea{width:100%;padding:5px 7px;font-size:12px;background:var(--panel2);border:1px solid var(--line);border-radius:5px;color:var(--fg);font-family:inherit;resize:vertical}
.map-tbl .refs{color:var(--fg3);font-size:11px;line-height:1.7;word-break:break-all}
.map-tbl .srcs{font-size:11px;color:var(--fg3);line-height:1.7}
.src-row{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);font-size:12px}
.src-row:last-child{border-bottom:0}
.src-row .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.bar{height:4px;background:var(--panel2);border-radius:2px;overflow:hidden;margin:10px 0}
.bar i{display:block;height:100%;background:var(--gold);width:0;transition:width .25s}
pre.log{background:#0a0e13;border:1px solid var(--line);border-radius:7px;padding:11px;max-height:220px;overflow:auto;font:11px/1.75 Consolas,Menlo,monospace;color:var(--fg2);white-space:pre-wrap;margin:0}

.kv{display:flex;gap:10px;font-size:12px;padding:5px 0;border-bottom:1px solid var(--line);align-items:baseline}
.kv:last-child{border-bottom:0}
.kv b{color:var(--fg2);font-weight:400;min-width:92px;flex:none}
.pvbtn{font-size:12px;color:var(--fg2);background:none;border:1px solid var(--line);border-radius:4px;padding:2px 10px;cursor:pointer;flex:none}
.pvbtn:hover{color:var(--gold);border-color:rgba(201,164,92,.4)}
#outputs audio,#outputs video{display:block;width:100%;max-width:560px;margin:2px 0 10px;border-radius:6px}
.kv span,.kv a{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
.empty{color:var(--fg3);font-size:12px;padding:16px 0;text-align:center}
.tbl-scroll{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:7px}
.tbl-scroll table th{position:sticky;top:0;background:var(--panel);z-index:1}
td input,td select{padding:4px 7px;font-size:12px}

.tabs2{display:flex;gap:8px;margin-bottom:14px}
.tabs2 button{flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:8px;cursor:pointer;color:var(--fg2);font-size:12px;transition:.12s}
.tabs2 button.on{border-color:var(--gold);color:var(--gold)}
.split{display:flex;gap:9px;align-items:center}
.split>*{min-width:0}

.proj{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:15px 17px;margin-bottom:14px;transition:border-color .12s}
.proj:hover{border-color:var(--line2)}
.proj.on{border-color:var(--gold)}
.proj.arch{opacity:.5}
.proj .top{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}
.proj .top .nm{font-size:14px}
.proj .top .sp{flex:1}
.proj .meta{font-size:11px;color:var(--fg3);display:flex;gap:16px;flex-wrap:wrap;margin-bottom:4px}
.proj .acts{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.proj .eps{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
.ep{display:flex;gap:10px;font-size:12px;padding:3px 0;align-items:baseline}
.ep .no{color:var(--gold);font-variant-numeric:tabular-nums;min-width:34px;flex:none}
.ep .ti{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ep .dt{color:var(--fg3);font-size:11px;flex:none}
.vc-row{display:flex;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
.vc-row .vc-role{color:var(--gold);font-size:13px;min-width:46px;flex:none}
.vc-row .vc-body{flex:1;min-width:0;font-size:12px;display:flex;flex-direction:column;gap:3px}
.vc-row .vc-body .dim{color:var(--fg3)}
</style>
</head>
<body>
<div id="root">
<header>
  <h1>播客制作智能体</h1><span class="ver" id="ver"></span>
  <span class="spacer"></span>
  <span class="stat"><i class="dot" id="backend-dot"></i><span id="backend-txt">后端未检测</span></span>
  <button class="mini" onclick="testBackend()">测试连接</button>
</header>
<nav>
  <button class="on" data-tab="project">项目</button>
  <button data-tab="script">脚本</button>
  <button data-tab="render">合成</button>
  <button data-tab="config">配置</button>
</nav>
<main>

<section class="tab on" id="tab-project">
  <div class="cols">
    <div class="col-l">
      <div class="card">
        <h2>立项</h2>
        <p class="note">一档节目一个项目。项目记住期号与风格，出片时自动往后排，不必每次重填。</p>
        <div class="f"><label>项目名称 <span class="hint">必填</span></label>
          <input type="text" id="np-name" placeholder="例如：我思故我写"></div>
        <div class="f"><label>规划方式 <span class="hint">立项后不可更改</span></label>
          <div class="mode-pick" id="np-mode">
            <button type="button" class="on" data-mode="mapped"><b>成稿规划</b>
              <span>已有一整部作品。先载入成稿排出期数地图，之后逐期按落点自动取素材</span></button>
            <button type="button" data-mode="episodic"><b>逐期即兴</b>
              <span>每期临时定内容。每次生成前自己粘或传这一期的素材</span></button>
            <button type="button" data-mode="single"><b>单集</b>
              <span>只出一集，不排地图。素材当场给，出完即完结</span></button>
          </div>
        </div>
        <div class="f"><label>节目名 <span class="hint">留空同项目名</span></label>
          <input type="text" id="np-program" placeholder="写进片头句与封面"></div>
        <div class="f"><label>副标题 <span class="hint">封面主标题下面的一行</span></label>
          <input type="text" id="np-subtitle" placeholder="如：AI 协作写成的书"></div>
        <div class="f"><label>受众 <span class="hint">片头「面向…的听众」里的那一句；留空则排地图时生成</span></label>
          <input type="text" id="np-audience" placeholder="如：关注方法论与认知边界"></div>
        <div class="f" id="np-planned-wrap"><label>计划期数 <span class="hint">留空即由地图的合并与切分决定；排完地图自动回填</span></label>
          <input type="number" id="np-planned" min="1" max="999" placeholder="留空即按内容分组定"></div>
        <div class="f" id="np-first-wrap"><label>起始期号</label><input type="text" id="np-first" value="1"></div>
        <div class="f"><label>素材类型 <span class="hint">排地图的组织依据，之后可改</span></label>
          <select id="np-paradigm"><option value="">自适应（按结构推断）</option></select></div>
        <div class="f"><label>重点方向 <span class="hint">可留空。填了与上面的组织依据一起进排图：侧重的部分合得细、多占期数，次要的合得粗</span></label>
          <input type="text" id="np-focus" placeholder="如：多解析方法论，少讲技术细节与实现"></div>
        <div class="f"><label>风格倾向 <span class="hint">项目内各期统一</span></label>
          <select id="np-style"></select></div>
        <div class="f"><label>备注</label><input type="text" id="np-note" placeholder="可留空"></div>
        <div class="btn-row" style="margin-top:4px"><button class="btn primary" onclick="createProject()">立项</button></div>
      </div>
      <div class="card">
        <h2>未归属的产物 <span class="tag" id="orphan-sum"></span></h2>
        <p class="note">磁盘上有产物、但不在任何项目下。归属由人决定，不自动猜。</p>
        <div id="orphans"><div class="empty">无</div></div>
      </div>
    </div>
    <div class="col-r">
      <div class="card">
        <h2>项目列表 <span class="tag" id="proj-sum"></span>
          <span class="spacer" style="flex:1"></span>
          <button class="mini" onclick="loadProjects()">刷新</button>
        </h2>
        <div id="projects"><div class="empty">尚无项目</div></div>
      </div>
    </div>
  </div>
</section>

<section class="tab" id="tab-script">
  <div class="cols">
    <div class="col-l">
      <div class="card">
        <h2>本期归属 <span class="tag">期号与素材按项目</span></h2>
        <div class="f"><label>归属项目</label>
          <select id="s-project" onchange="pickProject('s')"></select></div>
        <div class="f" id="s-eps-wrap" style="display:none"><label>要做哪几期
          <span class="hint">可多选 · 勾几期就生成几期</span></label>
          <div class="pick">
            <button type="button" class="picksum" id="s-eps-btn" onclick="togglePick('s')">
              <span id="s-eps-t">选期</span><span class="cnt" id="s-eps-cnt"></span></button>
            <div class="pickbox" id="s-eps-box"></div>
          </div>
        </div>
        <div id="s-proj-note" class="sub"></div>
      </div>
      <div class="card">
        <h2>素材 <span class="tag" id="mat-meta">未载入</span></h2>
        <!-- 料源随范式走：成稿规划的料源是地图落点，这里只读展示，不给输入口；
             逐期即兴的料源是当场给的，输入区就是它该有的样子。两种不混。 -->
        <div id="mat-locked" style="display:none">
          <div class="desc" id="mat-src" style="line-height:1.8"></div>
        </div>
        <div id="mat-input">
          <div class="tabs2">
            <button class="on" data-mat="paste">粘贴</button>
            <button data-mat="file">上传文件</button>
          </div>
          <div id="mat-paste"><textarea id="material" placeholder="粘贴要改写成播客的文章、书稿片段或要点"></textarea></div>
          <div id="mat-file" style="display:none">
            <input type="file" id="file-input" accept=".md,.markdown,.txt,.docx">
            <div class="desc" style="margin-top:8px">支持 md / txt / docx。PDF 不支持，请提供源文件</div>
          </div>
          <div class="f" style="margin-top:14px">
            <label>章节锚点 <span class="hint">按标题切章</span></label>
            <div class="split"><select id="anchor"><option value="">全文</option></select>
            <button class="mini" onclick="loadAnchors()">刷新</button></div>
          </div>
        </div>
      </div>
      <div class="card">
        <h2>写作要点 <span class="tag">本次生效</span></h2>
        <p class="note">这几项最常调。全部可调项在「配置」页。</p>
        <div id="quick-script" class="grid" style="margin-top:10px"></div>
      </div>
      <div class="card">
        <h2>生成 <span class="tag" id="gen-status">就绪</span></h2>
        <div class="f" id="gen-title-wrap" style="display:none">
          <label>本期标题 <span class="hint">由脚本产出，可改</span></label>
          <input type="text" id="gen-title">
        </div>
        <div class="btn-row" style="margin-top:6px">
          <button class="btn primary" onclick="genScript()" id="btn-gen">生成脚本</button>
          <button class="btn" onclick="gateOnly()">只校验</button>
          <button class="btn" onclick="saveScript()" id="btn-save-s">存回本期</button>
          <button class="btn" id="btn-stop-s" style="display:none" onclick="stopJob('s')">中止</button>
        </div>
        <div class="bar"><i id="gen-bar"></i></div>
        <pre class="log" id="gen-log" style="margin-top:10px">就绪。</pre>
      </div>
    </div>
    <div class="col-r">
      <div class="card">
        <h2>字与时间 <span class="tag">口径＝标准语速</span></h2>
        <div class="gauge" id="gauge"></div>
        <div id="hint" style="margin-top:12px"></div>
        <div id="standard-box" style="margin-top:14px"></div>
      </div>
      <div class="card">
        <h2>门禁 <span class="tag" id="gate-sum">未校验</span></h2>
        <div id="gates"><div class="empty">生成脚本后自动校验</div></div>
      </div>
      <div class="card">
        <h2>脚本 <span class="tag" id="script-meta">0 句</span>
          <span class="spacer" style="flex:1"></span>
          <button class="mini" onclick="recalc()">重算</button>
        </h2>
        <div class="tbl-scroll" style="margin-top:10px"><table id="script-tbl">
          <thead><tr><th style="width:40px">#</th><th style="width:76px">说话人</th><th style="width:64px">标签</th><th>台词</th>
          <th style="width:58px">字数</th>
          <th style="width:66px">预估秒</th><th style="width:66px">实测秒</th></tr></thead>
          <tbody><tr><td colspan="7" class="empty">尚无脚本</td></tr></tbody>
        </table></div>
      </div>
    </div>
  </div>
</section>

<section class="tab" id="tab-render">
  <div class="cols">
    <div class="col-l">
      <div class="card">
        <h2>本期 <span class="tag">按项目选期</span></h2>
        <div class="f"><label>归属项目</label>
          <select id="r-project" onchange="pickProject('r')"></select></div>
        <div class="f" id="r-eps-wrap" style="display:none"><label>合成哪几期
          <span class="hint">可多选 · 只列已有脚本的期</span></label>
          <div class="pick">
            <button type="button" class="picksum" id="r-eps-btn" onclick="togglePick('r')">
              <span id="r-eps-t">选期</span><span class="cnt" id="r-eps-cnt"></span></button>
            <div class="pickbox" id="r-eps-box"></div>
          </div>
        </div>
        <div id="r-proj-note" class="sub"></div>
        <div class="f" style="margin-top:10px"><label>本期标题 <span class="hint">来自脚本，可改</span></label>
          <input type="text" id="r-title"></div>
        <div id="quick-render" class="grid" style="margin-top:6px"></div>
        <div style="margin-top:14px">
          <label class="sw on" id="sw-do_video" onclick="toggleSw(this)"><i></i><span>输出视频（关掉只出音频与图文）</span></label>
        </div>
        <div class="bar" style="margin-top:14px"><i id="render-bar"></i></div>
        <div class="btn-row" style="margin-top:12px">
          <button class="btn primary" style="flex:1" onclick="render()" id="btn-render">开始合成</button>
          <button class="btn" id="btn-stop-r" style="display:none" onclick="stopJob('r')">中止</button>
        </div>
        <div id="render-stage" class="sub" style="margin-top:10px">未开始</div>
        <pre class="log" id="render-log" style="margin-top:10px">等待任务。</pre>
      </div>
      <div class="card">
        <h2>历史</h2>
        <div id="history"><div class="empty">尚无产出</div></div>
      </div>
    </div>
    <div class="col-r">
      <div class="card">
        <h2>校验报告 <span class="tag" id="rep-sum">未生成</span></h2>
        <div id="report"><div class="empty">合成完成后显示</div></div>
      </div>
      <div class="card">
        <h2>产物</h2>
        <div id="outputs"><div class="empty">尚无产物</div></div>
      </div>
    </div>
  </div>
</section>

<section class="tab" id="tab-config">
  <div class="card">
    <h2>配置总表 <span class="tag" id="cfg-sum"></span></h2>
    <p class="note">全部可调项只有这一份定义，界面照表渲染。分区按点位命名空间归拢：新增一个可调项，它会自己出现在该去的地方。</p>
    <div id="cfg-warn"></div>
  </div>
  <div id="stage-config"></div>
</section>

</main>
</div>
<div class="toast-wrap" id="toasts"></div>
<div class="mask" id="mask"><div class="modal">
  <h3 id="m-title">确认</h3><div class="body" id="m-body"></div>
  <div class="acts"><button class="btn" onclick="closeModal()">取消</button>
  <button class="btn primary" id="m-ok">确定</button></div>
</div></div>
<script>
let CFG=null, SCRIPT=[], CUR_JOB=null, VOICES_BY_ENGINE={}, PROJECTS=[];
/* 标签下拉选项：占位串由 Python 侧替换成 config_manager 两份标签合成的 JSON 数组
   ——语篇词表 DISCOURSE_ORDER ＋ 程序专用的 PROGRAM_ONLY_TAGS（单源注入，见文件
   尾部的 replace）。 */
const EMOTIONS="__DISCOURSE_VOCAB__";
/* 两页各一份「可选的期」。EPISEL 是勾上的那些——勾几期就做几期。
   顺序即勾选顺序不保证，所以实际按 EPISODES 的顺序取，跟地图一致。 */
let EPISODES={s:[],r:[]}, EPISEL={s:new Set(),r:new Set()}, LOADED_EP='';
let EPILOADED={s:false,r:false};
function picked(which){return (EPISODES[which]||[]).filter(x=>EPISEL[which].has(x.no)).map(x=>x.no)}
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
const el=id=>document.getElementById(id);

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function toast(msg,kind){
  const d=document.createElement('div');
  d.className='toast '+(kind||'');
  d.textContent=msg; el('toasts').appendChild(d);
  setTimeout(()=>{d.style.opacity='0';setTimeout(()=>d.remove(),200)},kind==='err'?6000:3200);
}
function modalBox(){return el('mask').querySelector('.modal')}
function modal(title,body,onOk,okText){
  modalBox().classList.remove('wide'); el('m-body').classList.remove('wide');
  el('m-title').textContent=title; el('m-body').textContent=body;
  el('m-ok').textContent=okText||'确定';
  el('mask').classList.add('on');
  el('m-ok').onclick=()=>{closeModal();onOk&&onOk()};
}
/* 需要放 HTML 的模态框：地图表格、素材清单这类。wide 给更大的宽度与更高的内容区。 */
function modalHtml(title,html,onOk,okText,wide){
  modalBox().classList.toggle('wide',!!wide);
  el('m-body').classList.toggle('wide',!!wide);
  el('m-title').textContent=title;
  el('m-body').innerHTML=html;
  el('m-ok').textContent=okText||'确定';
  el('mask').classList.add('on');
  el('m-ok').onclick=()=>{closeModal();onOk&&onOk()};
}
function closeModal(){el('mask').classList.remove('on')}
/* 项目页三类长活（探查 / 排图 / 插入建议）共用的「进行中」弹窗。
   后端立刻回任务号，进度、阶段、日志由 watchTask 追着 /api/task 画。
   弹窗可以关——任务在后端照跑，结果照常落库；重开面板再点同类操作
   会被互斥拦住并说明原因。 */
function taskModal(title){
  modalHtml(title,
    '<div class="bar" style="margin-bottom:8px"><i id="task-bar"></i></div>'+
    '<div id="task-stage" class="dim" style="font-size:12px;margin-bottom:8px">排队中…</div>'+
    '<div id="task-log" style="font-size:12px;max-height:240px;overflow:auto;'+
    'white-space:pre-wrap;border:1px solid var(--line);border-radius:6px;padding:8px">'+
    '等待中…</div>',
    ()=>{},'收起（任务继续跑）',true);
}
/* 轮询一个后台任务到出结果为止。onDone/onFail 拿到的就是 job——
   结果在 j.result，错误在 j.error，全程日志在 j.log。 */
async function watchTask(tid,onDone,onFail){
  const tick=async()=>{
    const r=await api('/api/task/'+encodeURIComponent(tid));
    if(!r.ok){toast(r.error||'任务查不到','err');return}
    const j=r.job||{};
    setBar('task-bar',j.progress||0);
    const st=el('task-stage'); if(st) st.textContent=j.stage||'';
    const lg=el('task-log');
    if(lg){lg.textContent=(j.log||[]).join('\n')||'等待中…';lg.scrollTop=lg.scrollHeight}
    if(j.status==='running'){setTimeout(tick,1200);return}
    if(j.status==='done'){onDone&&onDone(j)}
    else{onFail?onFail(j):toast('任务失败：'+(j.error||''),'err')}
  };
  tick();
}
function modalInput(title,body,def,onOk,okText){
  modalBox().classList.remove('wide'); el('m-body').classList.remove('wide');
  el('m-title').textContent=title;
  el('m-body').innerHTML='<div style="margin-bottom:10px">'+esc(body)+'</div>'+
    '<input type="text" id="m-input" value="'+esc(def==null?'':def)+'">';
  el('m-ok').textContent=okText||'确定';
  el('mask').classList.add('on');
  el('m-ok').onclick=()=>{const v=el('m-input').value;closeModal();onOk&&onOk(v)};
  const inp=el('m-input'); if(inp){inp.focus();inp.select()}
}
async function api(path,body){
  const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{};
  const r=await fetch(path,opt);
  return await r.json();
}
function toggleSw(node){node.classList.toggle('on')}

/* ---- 控件登记：同一个点位可能在多处出现，值必须同步 ---- */
const REG={};
function cid(stage,key){return 'f_'+stage+'_'+key.replace(/\./g,'__')}
function vid(stage,key){return 'v_'+stage+'_'+key.replace(/\./g,'__')}
function reg(stage,key,item){item.stage=stage;(REG[key]=REG[key]||[]).push(item)}
/* 一次登记只在节点还挂在文档里时有效。清空容器后旧节点就脱离了文档，
   此时把它们从登记表摘掉——既不会重复堆叠，也不会留下读不到值的空壳。
   早先按 stage 名清理，两个容器共用一个 stage 名，后一次清理会把前一次
   刚登记的控件一起摘掉，于是跨页的同名点位不再同步。 */
function pruneReg(){
  for(const k in REG) REG[k]=REG[k].filter(n=>n.node&&n.node.isConnected);
}
function syncKey(key,v){
  (REG[key]||[]).forEach(n=>{
    // 标题行右侧那枚「当前值」在**每一次值变化**时重填，不分控件形态：滑杆、开关、
    // 下拉、数字框、字体全都走这一句。先前只在这里填了滑杆与开关，下拉和数字框
    // 的金字于是停在渲染那一刻的旧值上——下拉里换了值，金字不动。
    // 开关的字是「开/关」不是取值本身，单独一支处理。
    if(n.val) n.val.textContent=(n.kind==='sw')?(v?'开':'关'):fmtVal(n.spec,v);
    if(n.kind==='sw'){
      n.node.classList.toggle('on',!!v);
      // 拨杆只表达状态，字还是得有一行——网格里每个格子的标题行高度是写死的，
      // 这里跟着亮暗一起改字，视觉上「开」与「关」的差别就不用靠猜。
    }
    else if(n.kind==='font') syncFontPick(n.node,v);
    else if(n.node){
      // 模型下拉不能直接塞 value：名字不在候选里时 select 会被清成空选中。
      // 一律走重填，把当前值本身作为一项补进去。
      if(n.spec&&n.node.dataset&&n.node.dataset.models){
        n.node.dataset.cur=(v==null?'':String(v));
        fillModelSelect(n.node);
      }else if(String(n.node.value)!==String(v==null?'':v)) n.node.value=(v==null?'':v);
    }
  });
}
function syncAll(){if(!CFG)return;for(const k in REG) syncKey(k,CFG.values[k])}

function decimalsOf(spec){
  const st=String(spec.step==null?1:spec.step);
  const i=st.indexOf('.');
  return i<0?0:(st.length-i-1);
}
function fmtNum(spec,v){
  if(spec.type==='float') return Number(v).toFixed(decimalsOf(spec));
  return String(v);
}
function fmtVal(spec,v){
  if(v==null||v==='') return '';
  if(spec.type==='int'||spec.type==='float'){
    let s=fmtNum(spec,v);
    if(spec.unit) s+=' '+spec.unit;
    if(spec.unit==='分钟'){
      const tot=Math.round(Number(v)*60);
      s+='　'+Math.floor(tot/60)+' 分 '+String(tot%60).padStart(2,'0')+' 秒';
    }
    return s;
  }
  if(spec.type==='enum'){
    const o=(spec.options||[]).find(x=>String(x.value)===String(v));
    return o?o.label:String(v);
  }
  return '';
}

/* ------------------------------------------------------------------ 字体下拉
   画面字体与字幕字体共用这一个控件。候选项按每款字体自己的字形渲染同一句样本，
   一眼就能看出选的是哪款——这正是原生 select 做不到的那件事。
   装没装的照样都列：装了=可选，没装=暗显。前者决定能不能用，后者决定知不知
   道有这款，两件事，所以不合并、也不另写「未安装」字样。 */
var FONT_SAMPLE='我思故我写 Aa 123';
var FONT_FACES={};
function fontKeyOf(family){
  return 'pmf_'+(String(family||'').replace(/[^A-Za-z0-9]/g,'_')||'x');
}
/* 随包字体没装进系统，浏览器按族名找不到，只能把文件取回来注册成一条临时
   font-face。每个族只注册一次；系统字体反过来，按族名就能拿到，不必再多下
   一份几十兆的文件。 */
function ensureFontFace(key,url){
  if(FONT_FACES[key]) return;
  FONT_FACES[key]='pending';
  if(!window.FontFace) return;
  try{
    const ff=new FontFace(key,'url("'+url+'")');
    ff.load().then(f=>{document.fonts.add(f);FONT_FACES[key]='ok'})
             .catch(()=>{FONT_FACES[key]='fail'});
  }catch(e){FONT_FACES[key]='fail'}
}
/* 族名一律用单引号包。这段样式是拼进 style="…" 属性里的，族名再带一对双引号
   就把属性值提前截断，浏览器把 font-family 整条丢掉——所有候选项于是又长成
   一个样，而控件本身看起来完全正常。 */
function fontCssOf(o){
  const fam=String((o&&(o.family||o.value))||'').replace(/["']/g,'');
  if(o&&o.url){const k=fontKeyOf(fam);ensureFontFace(k,o.url);return "'"+k+"'"}
  return "'"+fam+"'";
}
function fontPickerHtml(id,key,spec,v){
  const opts=spec.options||[];
  const cur=opts.find(o=>String(o.value)===String(v))||opts[0]||{};
  const one=o=>'<div class="fopt'+(String(o.value)===String(v)?' on':'')+(o.off?' off':'')+'"'+
    (o.off?'':' onclick="pickFont(this)"')+
    ' data-v="'+esc(String(o.value))+'" data-fam="'+esc(o.family||'')+'"'+
    ' data-url="'+esc(o.url||'')+'">'+
    '<span class="fs" style="font-family:'+fontCssOf(o)+',sans-serif">'+esc(FONT_SAMPLE)+'</span>'+
    '<span class="fn">'+esc(o.label||'')+(o.desc?' · '+esc(o.desc):'')+'</span>'+
    (String(o.value)===String(v)?'<span class="fk">当前</span>':'')+'</div>';
  return '<div class="fpick" id="'+id+'" data-key="'+esc(key)+'"'+
      ' data-v="'+esc(String(v==null?'':v))+'">'+
      '<button type="button" class="fsel" onclick="toggleFontPick(this)">'+
        '<span class="fsample" style="font-family:'+fontCssOf(cur)+',sans-serif">'+
          esc(FONT_SAMPLE)+'</span>'+
        '<span class="fname">'+esc(cur.label||'')+'</span><span class="fname">▾</span>'+
      '</button>'+
      '<div class="fbox">'+opts.map(one).join('')+'</div>'+
    '</div>';
}
function toggleFontPick(btn){
  const box=btn.closest('.fpick'); if(!box) return;
  const open=box.classList.contains('open');
  document.querySelectorAll('.fpick.open').forEach(x=>x.classList.remove('open'));
  box.classList.toggle('open',!open);
}
function pickFont(node){
  const box=node.closest('.fpick'); if(!box) return;
  box.querySelectorAll('.fopt').forEach(x=>x.classList.remove('on'));
  node.classList.add('on');
  box.classList.remove('open');
  applyFontPick(box,node.dataset.v,node);
  put(box.dataset.key||'',node.dataset.v);
}
/* 把某个值反映到收起状态的那一行上（样本字形 + 名字）。切换值与别处同步
   都走这里，两处各写一遍的话，改完配置再刷新会看见名字没跟着走。 */
function applyFontPick(box,v,hit){
  box.dataset.v=(v==null?'':String(v));
  let node=hit;
  if(!node){
    node=Array.prototype.find.call(box.querySelectorAll('.fopt'),
                                   x=>x.dataset.v===(v==null?'':String(v)));
  }
  if(!node) return;
  const fs=box.querySelector('.fsel .fsample');
  if(fs) fs.style.fontFamily=fontCssOf({family:node.dataset.fam,url:node.dataset.url})+',sans-serif';
  const fn=box.querySelector('.fsel .fname');
  const lab=node.querySelector('.fn');
  if(fn&&lab) fn.textContent=lab.textContent;
}
function syncFontPick(box,v){
  if(!box||!box.querySelectorAll) return;
  const sv=(v==null?'':String(v));
  box.querySelectorAll('.fopt').forEach(x=>x.classList.toggle('on',x.dataset.v===sv));
  applyFontPick(box,sv,null);
}
/* 点到别处就收起：字体列表很长，展开着不关会挡住下面的项。 */
document.addEventListener('click',e=>{
  if(e.target.closest&&e.target.closest('.fpick')) return;
  document.querySelectorAll('.fpick.open').forEach(x=>x.classList.remove('open'));
});

function control(stage,key,spec){
  const wrap=document.createElement('div');
  const id=cid(stage,key);
  const v=CFG.values[key];
  /* 占几列：一律一格。只有多行输入（textarea）例外——250px 宽塞不下一段文字。
     从前是「长文本与长说明都跨两列」，于是同一张卡里滑杆有的占两格、有的占一格，
     下拉也一样，行的起点对不齐。说明长短只影响这一格自身的高度，不动宽度。 */
  const cls=['f'];
  if(spec.type==='text') cls.push('wide');
  wrap.className=cls.join(' ');
  let hold='', scale='', note='', auHtml='';

  /* 开关也走同一副骨架：标题在标题行、拨杆在控件槽、状态写在 val。
     从前它自带文字、整块贴左，跟旁边的下拉既不同高也不在同一条线上，一行里就它
     最扎眼——「滑杆、输入框混杂」有一半是这个。 */
  if(spec.type==='bool'){
    wrap.innerHTML='<label><span>'+esc(spec.label)+
        (spec.required?'<span class="req">必填</span>':'')+'</span>'+
        '<span class="val" id="'+vid(stage,key)+'"></span></label>'+
      '<div class="hold"><label class="sw'+(v?' on':'')+'" id="'+id+'"><i></i></label></div>'+
      '<div class="scale"></div>'+
      (spec.help?'<div class="desc">'+esc(spec.help)+'</div>':'');
    // 在 wrap 内部找，不能用 getElementById：此刻 wrap 还没进文档树，
    // 文档里根本没有这个 id，查回来是 null，于是控件注册成空节点、
    // 事件也没绑上——开关点了不生效就是这个形状。
    const sw=wrap.querySelector('.sw');
    const val=wrap.querySelector('[id="'+vid(stage,key)+'"]');
    if(val) val.textContent=(v?'开':'关');
    // 亮不亮由服务端回值驱动（写成功才亮），这里只负责把点击发出去。
    // 发**目标态**（点之前是关就发开），不是当前态——发当前态的话服务端
    // 永远写回旧值、开关永远翻不了（亮/暗只由 syncKey 按服务端回值驱动）。
    sw.onclick=()=>{put(key,!sw.classList.contains('on'))};
    reg(stage,key,{kind:'sw',node:sw,spec:spec,val:val});
    return wrap;
  }

  const title='<label><span>'+esc(spec.label)+
    (spec.required?'<span class="req">必填</span>':'')+'</span>'+
    '<span class="val" id="'+vid(stage,key)+'"></span></label>';

  if(spec.font_pick&&spec.type==='enum'){
    hold='<div class="hold">'+fontPickerHtml(id,key,spec,v)+'</div>';
  }else if(spec.type==='enum'||spec.options_source==='voices'){
    let opts=spec.options||[];
    if(spec.options_source==='voices'){
      // 清单按该项所属引擎取，不是一律取当前引擎——不这么分，切到本地引擎后
      // Edge 的两个下拉也会被填进本地音色名，选中就写进一个永不生效的键。
      const list=voicesFor(spec);
      opts=list.length?list.map(x=>({value:x.name,label:x.label||x.name}))
                      :[{value:v,label:String(v)}];
    }
    // 白名单里没装的项由后端标 off：照列不误，但置 disabled。
    // 暗显表达「不可选」，不再另写「未安装」字样——那是同一件事说两遍。
    const sel='<select id="'+id+'">'+opts.map(o=>'<option value="'+esc(String(o.value))+'"'+
      (o.off?' disabled':'')+
      (String(o.value)===String(v)?' selected':'')+'>'+esc(o.label)+
      (o.desc?' · '+esc(o.desc):'')+'</option>').join('')+'</select>';
    if(spec.preview_base){
      // 档位试听：按钮与下拉同槽；音频单占控件下方一行、默认不出现，点了才铺开。
      // data-* 寻址不用 id 拼接——id 里有「.」，querySelector 裸拼会被当类名吃掉。
      hold='<div class="hold pv">'+sel+
        '<button type="button" class="pvbtn" data-pv="'+id+'">▶ 试听</button></div>';
      auHtml='<audio controls preload="none" data-pvau="'+id+'" style="display:none"></audio>';
    }else{
      hold='<div class="hold">'+sel+'</div>';
    }
    // 音色这一行多带一句它相对标准语速的比例：换音色会改成品快慢，
    // 而这个数就是那件事的全部依据。跟着下拉的当前值走，换一个音色重算一次。
    // 不占 id：它是一句说明不是控件，界面上「带 id 的即控件」这条口径不能破
    // （冒烟按 id 数控件、核登记，给它一个 f_ 开头的 id 就多出一个查无登记的幽灵）。
    if(spec.options_source==='voices'){
      note+='<div class="desc ratio-note"></div>';
      // 克隆变体下这些名字不是「合成用的音色」，而是「录参考音频时挑的嗓子」。
      // 不说明白，人会以为在这里换个名字整期声音就跟着换 —— 实际要重录才生效。
      if((voicesFor(spec)[0]||{}).ref_based) note+='<div class="desc">'+
        '本机跑的是克隆变体：这几个名字只决定用哪把嗓子录本项目的参考音频。'+
        '录好后整期都用那份音频，要换得去项目卡片上的「音色」重录一次。</div>';
    }
  }else if(spec.options_source==='models'){
    // 模型名用下拉（select）。从前这里是「输入框 + datalist」——datalist 是浏览器的
    // **补全候选**，它拿框里已有的字去筛：框里填着 qwen/qwen3.5-35b-a3b 时，15 个
    // 候选只剩含这串字的那一个，看着就像"下拉拉不出来"。select 才是点开列全部。
    // 列表里没有的名字从末项「手动填写…」进，这条能力不丢。
    hold='<div class="hold"><select id="'+id+'" data-models="1"></select></div>';
  }else if(spec.type==='int'||spec.type==='float'){
    const step=spec.step||(spec.type==='int'?1:0.1);
    if(spec.min!==undefined&&spec.max!==undefined){
      hold='<div class="hold"><input type="range" id="'+id+'" min="'+spec.min+
           '" max="'+spec.max+'" step="'+step+'" value="'+v+'"></div>';
      scale='<div class="scale"><span>'+fmtNum(spec,spec.min)+'</span>'+
            '<span>'+fmtNum(spec,spec.max)+'</span></div>';
    }else{
      hold='<div class="hold"><input type="number" id="'+id+'" value="'+v+'"></div>';
    }
  }else if(spec.type==='path'){
    /* 路径类点位填的是**本机绝对路径**——管线一律拿 os.path.exists 判它，文件不在
       就当作没填（片头尾、立绘、自备音乐都是静默跳过）。手打一条 Windows 路径既慢
       又容易错，所以在框旁边给「选择…」：弹本机文件对话框，选完把绝对路径写回这个
       点位，与手填完全等价——不复制文件、不改读取位置。
       占位符必须把「这是什么」说出来：一个空框配「留空即不使用」，只说了可不可以
       不填，没说该往里放什么。 */
    hold='<div class="hold pv"><input type="text" id="'+id+'" value="'+esc(v==null?'':v)+
         '" placeholder="本机绝对路径，留空即不使用">'+
         '<button type="button" class="pvbtn" data-pick="'+id+'">选择…</button></div>';
  }else if(spec.type==='text'){
    // 多行输入自成一块，不塞进 34px 的控件槽。help 已经作为常驻说明渲染在下方，
    // 再塞进 placeholder 就是同一句话写两遍：框里一句、框下面又一句。
    hold='<textarea id="'+id+'">'+esc(v||'')+'</textarea>';
  }else{
    hold='<div class="hold"><input type="text" id="'+id+'" value="'+esc(v==null?'':v)+'"></div>';
  }
  wrap.innerHTML=title+hold+(scale||'<div class="scale"></div>')+note+
    (spec.help?'<div class="desc">'+esc(spec.help)+'</div>':'')+auHtml;

  const node=wrap.querySelector('[id="'+id+'"]');
  const val=wrap.querySelector('[id="'+vid(stage,key)+'"]');
  if(val) val.textContent=fmtVal(spec,v);
  if(spec.options_source==='voices'&&node){
    const rb=wrap.querySelector('.ratio-note');
    const fillRatio=()=>{if(rb) rb.textContent=ratioNote(spec,node.value)};
    fillRatio();
    // 用 addEventListener 而不是覆盖 onchange：下面那段还要给 node.onchange
    // 绑取值，两件事各绑各的，谁先谁后都不影响。
    node.addEventListener('change',fillRatio);
  }
  if(spec.preview_base&&node){
    const pv=wrap.querySelector('[data-pv="'+id+'"]');
    const au=wrap.querySelector('[data-pvau="'+id+'"]');
    if(pv&&au){
      const stop=()=>{au.pause();au.removeAttribute('src');au.style.display='none';
        pv.textContent='▶ 试听';};
      pv.onclick=()=>{
        if(au.paused){au.src=spec.preview_base+encodeURIComponent(node.value);
          au.style.display='';au.play();pv.textContent='⏹ 停止';}
        else stop();
      };
      au.onended=stop;
      node.addEventListener('change',stop);
    }
  }
  if(spec.type==='path'&&node){
    // 对话框在本机弹（服务就跑在这台机器上），选完把路径当普通取值写回去——
    // 走的是与手填同一条 put()，所以校验、跨页同步、落盘全都照旧。
    // 用户取消＝什么都没发生，不当成错误弹提示。
    const pb=wrap.querySelector('[data-pick="'+id+'"]');
    if(pb) pb.onclick=async()=>{
      const old=pb.textContent;
      pb.disabled=true; pb.textContent='选择中…';
      let r=null;
      try{r=await api('/api/pickfile',{key:key});}
      finally{pb.disabled=false; pb.textContent=old;}
      if(!r||!r.ok){toast((r&&r.error)||'打不开本机文件对话框','err');return}
      if(r.cancelled) return;
      put(key,r.path);
    };
  }
  const isFont=!!(spec.font_pick&&spec.type==='enum');
  if(node&&!isFont){
    if(node.type==='range'){
      node.oninput=()=>{if(val)val.textContent=fmtVal(spec,node.value)};
      node.onchange=()=>put(key,spec.type==='int'?parseInt(node.value,10):parseFloat(node.value));
    }else if(spec.type==='int'||spec.type==='float'){
      node.onchange=()=>put(key,spec.type==='int'?parseInt(node.value,10):parseFloat(node.value));
    }else{
      node.onchange=()=>put(key,node.value);
    }
    if(spec.options_source==='models') wireModelSelect(node,key,v);
  }
  // 自绘字体下拉没有原生 value/change，自己写值（见 pickFont），
  // 因此登记成 'font'：同步与取值都走 dataset，不走 node.value。
  reg(stage,key,{kind:(isFont?'font':(node&&node.type==='range'?'range':'ctl')),
                 node:node,val:val,spec:spec});
  return wrap;
}

function valOf(key){
  const a=REG[key]||[];
  if(!a.length) return undefined;
  const n=a[0];
  if(n.kind==='sw') return n.node.classList.contains('on');
  if(n.kind==='font') return n.node.dataset.v;
  const spec=n.spec||{};
  if(spec.type==='int') return parseInt(n.node.value,10);
  if(spec.type==='float') return parseFloat(n.node.value);
  if(spec.type==='bool') return n.node.classList.contains('on');
  return n.node.value;
}
async function put(key,value){
  const r=await api('/api/config',{patch:{[key]:value}});
  if(r.rejected&&r.rejected.length) toast('写入被拒：'+r.rejected.join('；'),'err');
  else if(r.errors&&r.errors.length) toast(r.errors[0],'err');
  else if(r.warnings&&r.warnings.length) toast(r.warnings[0],'');
  if(r.ok){
    if(r.values) for(const k in r.values){CFG.values[k]=r.values[k];syncKey(k,r.values[k])}
    else {CFG.values[key]=value;syncKey(key,value)}
  }else{
    await loadConfig(); syncAll();
  }
  if(key.indexOf('audio.')===0) renderAudioWarn();
  // 换了后端或地址，模型列表整个作废，得重新问一遍才准
  if(key==='llm.backend'||key==='llm.base_url') loadModels();
  // 换引擎 = 换一整套音色：重拉清单，并按 engine_scope 重排哪些点位露出
  // 同时决定「本地语音环境」那块要不要露出来（只有 Qwen3-TTS 需要它）
  if(key==='tts.engine'){loadVoices(false); paintTts();}
  if(key.indexOf('tts.speed')===0||key==='script.target_minutes') recalcSoon();
}
let recalcTimer=null;
function recalcSoon(){clearTimeout(recalcTimer);recalcTimer=setTimeout(()=>{if(SCRIPT.length)recalc()},700)}

/* ---- 渲染 ---- */
/* 卡内功能区：一张卡里常混着好几件事。LLM 那张卡既有「连到哪个后端、用哪个模型」
   也有「生成参数」「超时」——只按类型分行，这三件事会被切成三段、彼此不相邻，
   人看不出谁和谁是一组。所以在卡内再切一层：功能区在上（小标题），区内的项再按
   类型分行。
   这张表是「谁和谁一起」的唯一出处，顺序即卡里的顺序。规则两条：
     ①一张卡只有一个功能区时不画小标题，跟从前逐字一样；
     ②表里漏登记的点位掉进末尾的「其它」，不至于凭空消失（真正防漏的是
       tests/test_config_keys 里那条与 PARAM_SPEC 的对账）。 */
const ZONES=[
  ['script','时长与规模',['script.target_minutes','script.segment_min_sents',
                        'script.segment_tol_frac',
                        'script.segment_fix_rounds','script.segment_parse_rounds',
                        'script.gate_rounds','script.check_rounds',
                        'script.map_max_episodes']],
  ['script','切分与文体',['script.paradigm','script.compress_ratio',
                        'script.style_preset','script.dialogue_form',
                        'script.plan_rounds']],
  ['script','取材与门禁',['script.shape_flags','script.gate_strict',
                        'script.flag_title_words','script.check_semantic']],
  ['gate','总时长容差',['gate.max_deviation_pct','gate.min_deviation_seconds']],
  ['gate','单句长短',['gate.min_chars','gate.max_chars','gate.max_seconds_per_line']],
  ['llm','连接',['llm.backend','llm.model','llm.base_url','llm.api_key']],
  ['llm','生成参数与超时',['llm.temperature','llm.max_tokens','llm.input_ratio',
                         'llm.idle_timeout','llm.timeout']],
  ['tts','引擎与音色',['tts.engine','tts.voice_a','tts.voice_b',
                     'tts.qwen3tts_voice_a','tts.qwen3tts_voice_b']],
  ['tts','称呼与语速',['tts.name_a','tts.name_b','tts.speed_a','tts.speed_b']],
  ['tts','本地语音服务',['tts.qwen3tts_host','tts.qwen3tts_port','tts.throttle_seconds',
                      'tts.max_retries','tts.unload_llm_before_synth']],
  ['audio','编码与响度',['audio.sample_rate','audio.bitrate_kbps','audio.channels',
                       'audio.codec','audio.loudnorm_target']],
  ['audio','停顿与降噪',['audio.pause_between_lines','audio.denoise']],
  ['audio','片头尾与声明',['audio.intro_path','audio.outro_path',
                        'audio.ai_disclosure_text','audio.ai_disclosure_path']],
  ['bgm','来源与音量',['bgm.mode','bgm.preset','bgm.custom_path','bgm.volume']],
  ['bgm','人声闪避',['bgm.ducking','bgm.duck_threshold','bgm.duck_ratio','bgm.fade_seconds']],
  ['video','画幅与帧率',['video.width','video.height','video.fps','video.produce_vertical']],
  ['video','编码与画面处理',['video.encoder_preset','video.crf','video.bg_dim']],
  ['speaker_indicator','提示方式',['speaker_indicator.mode','speaker_indicator.name_shown']],
  ['speaker_indicator','角色字幕色',['subtitle.color_a','subtitle.color_b']],
  ['speaker_indicator','色块与立绘配色',['speaker_indicator.color_a','speaker_indicator.color_b']],
  ['speaker_indicator','立绘',['speaker_indicator.portrait_a','speaker_indicator.portrait_b']],
  ['subtitle','版式与字号',['subtitle.preset','subtitle.font_size','subtitle.font_size_vertical']],
  ['subtitle','高亮',['subtitle.highlight','subtitle.highlight_color']],
  ['subtitle','歌词窗口',['subtitle.lyric_window','subtitle.lyric_max_rows',
                        'subtitle.lyric_anchor_y','subtitle.lyric_scroll_ms']],
  ['subtitle','边距与底框',['subtitle.margin_lr','subtitle.margin_v','subtitle.margin_v_vertical',
                          'subtitle.outline','subtitle.bg_alpha']]
];
const ZONE_OF={};
ZONES.forEach(z=>{(z[2]||[]).forEach(k=>{ZONE_OF[k]={sec:z[0],label:z[1]}})});
/* 把一张卡的点位按功能区归拢。没在表里登记的卡（整张卡只讲一件事）回落成一个
   无名的区，标题也不画——与从前完全一样。 */
function zoneList(sec,items){
  const out=[], idx={};
  const box=label=>{
    if(!(label in idx)){idx[label]=out.length;out.push({label:label,items:[]})}
    return out[idx[label]];
  };
  ZONES.forEach(z=>{if(z[0]===sec) box(z[1])});
  items.forEach(it=>{
    const z=ZONE_OF[it.key];
    box(z&&z.sec===sec?z.label:'其它').items.push(it);
  });
  return out.filter(z=>z.items.length);
}
function orderSections(keys){
  const ord=(CFG.ui.section_order||[]);
  return keys.slice().sort((a,b)=>{
    const ia=ord.indexOf(a), ib=ord.indexOf(b);
    if(ia<0&&ib<0) return a<b?-1:1;
    if(ia<0) return 1; if(ib<0) return -1;
    return ia-ib;
  });
}
function secLabel(s){return (CFG.ui.section_labels||{})[s]||s}

/* 控件按形态分桶，同形态的排在一起：滑杆一行、开关一行、下拉一行、输入框一行。
   这是「一格一控件」之外的另一半规矩——只把格子做齐，滑杆照样会跟下拉隔着五个
   格子，调数值时要在一整张卡上来回找。分桶之后，每一行是什么类控件，扫一眼就
   知道该往哪一行看。
   形态判定必须与 control() 里那几支 if 一一对应：判错了，桶里会混进另一种控件，
   比不分桶更乱。 */
function ctlKind(spec){
  if(!spec) return 9;
  if(spec.type==='bool') return 2;                       // 开关
  if(spec.type==='enum'||spec.options_source) return 3;  // 下拉（含音色/模型/字体）
  if(spec.type==='int'||spec.type==='float')
    return (spec.min!==undefined&&spec.max!==undefined)?1:4;   // 滑杆 / 数字框
  if(spec.type==='text') return 5;                       // 多行输入
  return 4;                                              // 单行文本 / 路径
}
/* 稳定排序：同形态内保持原来的声明顺序——这次改的是排布，配置项的先来后到不动。 */
function byKind(list){
  return list.map((it,i)=>{return {it:it,i:i}})
    .sort((a,b)=>(ctlKind(a.it)-ctlKind(b.it))||(a.i-b.i))
    .map(x=>x.it);
}
/* 按形态铺进网格：一类一行（形态一变就另起一行，见样式里的 .kstart）。
   一行填不满就空着，后面的控件不回头填空。 */
function fillByKind(box,stage,list){
  let prev=null;
  byKind(list).forEach(spec=>{
    const k=ctlKind(spec);
    const node=control(stage,spec.key,spec);
    if(k!==prev) node.classList.add('kstart');
    prev=k;
    box.appendChild(node);
  });
}

function put2(target,keys){
  const box=(typeof target==='string')?el(target):target;
  if(!box) return;
  box.innerHTML=''; pruneReg();
  fillByKind(box,'quick',keys.map(k=>specOf(k)).filter(x=>x));
}
function specOf(key){
  for(const st in CFG.params){
    for(const s in CFG.params[st]){
      const f=CFG.params[st][s].find(x=>x.key===key);
      if(f) return f;
    }
  }
  return null;
}
function renderConfig(){
  const box=el('stage-config'); if(!box) return;
  box.innerHTML=''; pruneReg();
  const groups=CFG.params.config||{};
  const secs=orderSections(Object.keys(groups));
  secs.forEach(sec=>{
    const card=document.createElement('div');
    card.className='card';
    card.innerHTML='<h2>'+esc(secLabel(sec))+'</h2>'+
      (CFG.ui.section_note[sec]?'<p class="note">'+esc(CFG.ui.section_note[sec])+'</p>':'');
    const zones=zoneList(sec,groups[sec]||[]);
    zones.forEach(z=>{
      // 只有一件事的卡不画小标题：多一行标题等于给一个不言自明的分区加注解
      if(zones.length>1)
        card.insertAdjacentHTML('beforeend','<div class="zhead">'+esc(z.label)+'</div>');
      const g=document.createElement('div'); g.className='grid g4';
      fillByKind(g,'cfg',z.items);
      card.appendChild(g);
    });
    // 语音引擎那张卡上多挂一块「本地环境」。它属于配置阶段：用户把引擎选成
    // Qwen3-TTS 的那一刻，缺的环境/依赖/模型就该在这一页补上，而不是等合成
    // 时才被告知「没装」——那时候人已经在等的另一件事上了。
    // 分区键是参数名前缀（tts.* → tts），不是那三个字的中文名。
    if(sec==='tts') card.insertAdjacentHTML('beforeend', ttsEnvHtml());
    box.appendChild(card);
  });
  const total=Object.keys(CFG.values).length;
  el('cfg-sum').textContent=total+' 项 · '+secs.length+' 个分区';
  const w=[]; (CFG.warnings||[]).forEach(m=>w.push('<div class="warn" style="font-size:12px">'+esc(m)+'</div>'));
  (CFG.errors||[]).forEach(m=>w.push('<div class="bad" style="font-size:12px">'+esc(m)+'</div>'));
  el('cfg-warn').innerHTML=w.join('');
  patchModelNote();
  // 点位刚重建，显隐状态得重算一遍：切到配置页时该收起的组要收起
  applyEngineScope();
  // 本地语音环境那块刚重建出来，探一次现状（缺环境 / 缺包 / 缺模型）
  if(el('tts-env')) refreshTtsState();
}
function renderQuick(){
  // 称呼与语速跟脚本同时生效：称呼直接写进提示词，模型据此称呼两位说话人。
  // 放在这里是为了「想改名就在脚本页改得到」，不必翻到配置页去找。
  // 对话形式与风格倾向同理：两者都是「写脚本那一刻才定的文体选择」，摆在这一排
  // 改完直接点下面的「生成脚本」；藏在配置页里，改一次要切两次页，人会以为
  // 「改了没生效」。它跟风格倾向是同一类东西，处置也必须一致。
  put2('quick-script',['script.target_minutes','script.style_preset',
                       'script.dialogue_form',
                       'tts.name_a','tts.name_b','tts.speed_a','tts.speed_b']);
  put2('quick-render',['video.fps','video.produce_vertical','subtitle.preset','background.preset',
                       'animation.mode','audio.bitrate_kbps']);
}
/* 脚本页只讲口径，不列音色。
   估时用的是一把与音色无关的尺子（标准语速），把音色名与实测系数搬到这里，
   等于让人以为「换音色会改脚本的时长目标」——那两件事分属两个阶段。
   各音色相对标准的快慢，在「配置 → 声音」里、每个音色自己那一行看。 */
function renderStandard(){
  const box=el('standard-box'); if(!box) return;
  const s=CFG.standard||{};
  if(!s.k){box.innerHTML='';return;}
  box.innerHTML='<div class="desc">估时口径：标准语速 <b>'+s.k.toFixed(2)+'</b> 有效字/秒'+
    '（≈'+s.cpm+' 汉字/分钟）。全篇按这一把尺子估，与本期用哪个音色无关；'+
    '各音色相对标准的快慢见「配置 → 声音」。</div>';
}
</script>
"""


# 页面逻辑：脚本生成 / 合成 / 项目。与上一段拼接，便于维护。
PAGE += r"""<script>
let ACTIVE_PROJ=localStorage.getItem('pm_proj')||'';

/* ---- 页面切换：hash 是唯一路由。可以直接把 #config 发给人，也能让
   自动化冒烟落到指定页；只靠按钮的 onclick 切页，外部就没法定位了。 ---- */
let TAB='';
const TABS=['project','script','render','config'];
function hashTab(){
  const t=(location.hash||'').replace(/^#/,'');
  return TABS.indexOf(t)>=0?t:'project';
}
function showTab(name,boot){
  name=name||hashTab();
  if(name===TAB) return;
  TAB=name;
  $$('nav button').forEach(x=>x.classList.toggle('on',x.dataset.tab===name));
  $$('.tab').forEach(t=>t.classList.toggle('on',t.id==='tab-'+name));
  syncAll();
  // 首屏时项目数据已在启动阶段取过，不重复拉；配置页是纯前端渲染，
  // 无论何时第一次显示都必须渲染一次。
  if(boot&&name!=='config') return;
  if(name==='config') renderConfig();
  if(name==='project') loadProjects();
  /* 脚本/合成两页切进来补一次重拉：页面之外发生的变化（另开窗口改了、
     别的任务写完了稿）不刷新就永远看不见。勾选由 refreshEpisodes 保留。 */
  if(name==='script') refreshEpisodes('s');
  if(name==='render') refreshEpisodes('r');
}
function goTab(name){
  if(hashTab()!==name) location.hash=name;
  TAB='';                 // 已在这一页时再点一次＝手动刷新，清标记让它重跑
  showTab(name);
}
function bindNav(){
  $$('nav button').forEach(b=>b.onclick=()=>goTab(b.dataset.tab));
  $$('[data-mat]').forEach(b=>b.onclick=()=>{
    $$('[data-mat]').forEach(x=>x.classList.remove('on')); b.classList.add('on');
    el('mat-paste').style.display=b.dataset.mat==='paste'?'':'none';
    el('mat-file').style.display=b.dataset.mat==='file'?'':'none';
  });
  window.addEventListener('hashchange',()=>showTab(hashTab()));
}
function cfgVal(key){const v=valOf(key);return v===undefined?CFG.values[key]:v}
function fmt(s){s=Math.round(s);return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}

/* ---- 配置加载 ---- */
async function loadConfig(){
  const c=await api('/api/config');
  if(!c.ok){toast('配置加载失败','err');return}
  CFG=c; el('ver').textContent='v'+c.version;
  renderQuick(); renderStandard(); fillStyleSelect(); renderGauge(null);
  if(TAB==='config') renderConfig();
  renderAudioWarn();
  recalc();
}
function fillStyleSelect(){
  const f=specOf('script.style_preset');
  const n=el('np-style'); if(!f||!n) return;
  n.innerHTML=(f.options||[]).map(o=>'<option value="'+esc(String(o.value))+'"'+
    (String(o.value)===String(cfgVal('script.style_preset'))?' selected':'')+'>'+esc(o.label)+'</option>').join('');
}
function styleLabel(v){
  if(!v) return '';
  const f=specOf('script.style_preset'); if(!f) return v;
  const o=(f.options||[]).find(x=>String(x.value)===String(v));
  return o?o.label:v;
}
function renderAudioWarn(){
  if(!CFG) return;
  const sr=cfgVal('audio.sample_rate'), br=cfgVal('audio.bitrate_kbps'), codec=cfgVal('audio.codec');
  const mp3={8000:64,11025:64,12000:64,16000:160,22050:160,24000:160,32000:320,44100:320,48000:320};
  const aac={8000:96,11025:96,12000:96,16000:128,22050:192,24000:192,32000:256,44100:320,48000:320};
  const ceil=(codec==='aac'?aac:mp3)[sr]||320;
  const box=el('cfg-warn'); if(!box) return;
  const old=box.querySelector('.rate-note'); if(old) old.remove();
  if(br>ceil){
    box.insertAdjacentHTML('beforeend','<div class="rate-note bad" style="font-size:12px;margin-top:8px">'+
      String(codec).toUpperCase()+' 在 '+sr+' Hz 下的码率上限是 '+ceil+' kbps，当前 '+br+
      ' kbps 不可达，编码器会静默钳制。请调整采样率或码率。</div>');
  }
}

/* ---- 音色 ----
   清单按引擎分桶缓存。Edge 与本地是两套完全不同的音色名（zh-CN-XiaoxiaoNeural
   vs Serena），混在一个数组里会互相顶掉；每个音色点位按自己的 engine_scope 取桶。
   两套都拉：收起的那一套也得有清单，否则切过去时下拉是空的。 */
function voicesFor(spec){
  const eng=(spec&&spec.engine_scope)||cfgVal('tts.engine')||'edge';
  return (VOICES_BY_ENGINE[eng]||{}).list||[];
}
/* 音色语速 ÷ 标准语速 —— 挂在音色下拉框下面那一行。
   只读：音色语速是量出来的，不是填出来的。能填的那个数一旦与实测对不上，
   页面上就有两个语速，又回到「两把尺子量同一份稿子」。
   没实测过就照实说没有，不拿 1.00 冒充量过的数。 */
function ratioNote(spec,voice){
  const eng=(spec&&spec.engine_scope)||cfgVal('tts.engine')||'edge';
  const s=CFG.standard||{};
  const row=(CFG.ratios||[]).find(x=>x.engine===eng&&x.voice===voice&&
                                     Math.abs(x.speed-1.0)<1e-6);
  if(!row) return '比例 —：该音色尚无实测样本，合成一次后自动算出';
  const r=row.ratio;
  return '比例 '+r.toFixed(2)+' ＝ 音色语速 ÷ 标准语速（'+(s.k?s.k.toFixed(2):'—')+
         ' 有效字/秒）· '+(r<1?'比标准慢':(r>1?'比标准快':'与标准同速'))+
         ' · 实测 '+row.samples+' 句';
}
const VOICES_LOADING={}, VOICES_TRIED={};
async function fetchVoices(eng,refresh){
  // 同一个引擎只试一次：拉不到时若无条件重试，fillVoiceSelects→拉取→
  // fillVoiceSelects 会自己转成死循环。refresh 是用户手动触发，才放行。
  if(VOICES_LOADING[eng]) return;
  if(VOICES_TRIED[eng]&&!refresh) return;
  VOICES_LOADING[eng]=1; VOICES_TRIED[eng]=1;
  try{
    const r=await api('/api/voices?engine='+encodeURIComponent(eng)+
                      (refresh?'&refresh=1':''));
    if(r.ok) VOICES_BY_ENGINE[eng]={list:r.voices||[], at:Date.now()};
    // 只有当前引擎拉不到才值得打断用户：另一个引擎的服务没开是常态。
    else if(eng===String(cfgVal('tts.engine')||'')) toast(r.error||'音色表不可用','err');
  }catch(e){
    if(eng===String(cfgVal('tts.engine')||'')) toast('音色表请求失败','err');
  }
  delete VOICES_LOADING[eng];
  fillVoiceSelects(); applyEngineScope();
}
function ensureVoices(eng){ if(eng&&!VOICES_BY_ENGINE[eng]) fetchVoices(eng,false); }
function loadVoices(refresh,engine){
  const eng=engine||cfgVal('tts.engine')||'edge';
  return fetchVoices(eng,!!refresh);
}
function fillVoiceSelects(){
  // 遍历注册表，不写死键名：将来多一个引擎，这里一行都不用改。
  for(const key in REG){
    (REG[key]||[]).forEach(n=>{
      if(!n.node||n.node.tagName!=='SELECT') return;
      const spec=n.spec||{};
      if(spec.options_source!=='voices') return;
      const eng=spec.engine_scope||cfgVal('tts.engine')||'edge';
      const cur=cfgVal(key);
      const list=voicesFor(spec);
      if(!list.length) ensureVoices(eng);
      const opts=list.length?list:[{name:cur,label:cur}];
      n.node.innerHTML=opts.map(v=>'<option value="'+esc(v.name)+'"'+
        (v.name===cur?' selected':'')+'>'+esc(v.label||v.name)+'</option>').join('');
      if(!list.length&&cur) n.node.value=cur;
    });
  }
}
/* 带 engine_scope 的点位只在当前引擎下露出：同屏永远只有一个「A 角音色」，
   不必让人去分辨哪个在生效；切引擎时两组互换，各自的选择都还留着。 */
function applyEngineScope(){
  const cur=String(cfgVal('tts.engine')||'');
  for(const key in REG){
    (REG[key]||[]).forEach(n=>{
      const sc=(n.spec||{}).engine_scope;
      if(!sc||!n.node) return;
      const row=n.node.closest('.f');
      if(row) row.style.display=(String(sc)===cur)?'':'none';
    });
  }
}

/* ---- 本地语音环境（Qwen3-TTS）----
   环境、依赖、模型这三件事归配置阶段：选了 Qwen3-TTS 就该在这儿把缺的补上，
   而不是等合成时才被告知「没装」。「服务在不在线」是另一回事，不属于缺什么——
   它平时不用开，合成前会自动拉起。 */
let TTS_STATE=null;
function ttsEnvHtml(){
  return '<div id="tts-env" style="display:none;margin-top:16px;border-top:1px solid var(--line);padding-top:12px">'+
    '<div style="display:flex;align-items:center;gap:8px">'+
      '<b>本地语音环境</b><span id="tts-badge" class="dim">探测中…</span></div>'+
    '<div id="tts-note" style="font-size:12px;color:var(--fg3);line-height:1.6;margin-top:5px"></div>'+
    '<div class="btn-row" style="margin-top:9px">'+
      '<button class="btn primary" id="btn-tts-setup" onclick="setupTts()">搭建本地语音环境</button>'+
      '<button class="btn" onclick="refreshTtsState()">重新探测</button>'+
      '<button class="btn" id="btn-stop-t" style="display:none" onclick="stopJob(\'t\')">中止</button>'+
    '</div>'+
    '<div class="bar"><i id="tts-bar"></i></div>'+
    '<pre class="log" id="tts-log" style="display:none;margin-top:10px">等待任务。</pre>'+
  '</div>';
}
async function refreshTtsState(){
  if(!el('tts-env')) return;
  const r=await api('/api/tts/state');
  if(!r.ok) return;
  TTS_STATE=r.state||null;
  paintTts();
}
function paintTts(){
  const box=el('tts-env'); if(!box) return;
  const on=String(cfgVal('tts.engine')||'')==='qwen3tts';
  box.style.display=on?'':'none';
  if(!on||!TTS_STATE) return;
  const st=TTS_STATE, svc=st.service||{};
  const badge=el('tts-badge'), note=el('tts-note'), btn=el('btn-tts-setup');
  btn.disabled=false;
  if(!st.ready){
    badge.className='bad'; badge.textContent='待搭建';
    note.textContent=st.message||'';
    btn.textContent='搭建本地语音环境';
  }else if(svc.online){
    badge.className='ok'; badge.textContent='已就绪 · 服务在线';
    note.textContent=svc.message||'';
    btn.textContent='重新搭建';
  }else{
    badge.className='ok'; badge.textContent='已就绪 · 服务未启动';
    note.textContent='环境与模型都齐了。服务不用手动开：合成前自动拉起，整批跑完自动停掉、归还显存。';
    btn.textContent='重新搭建';
  }
}
async function setupTts(){
  const btn=el('btn-tts-setup'); btn.disabled=true;
  const r=await api('/api/tts/setup',{});
  if(!r.ok){btn.disabled=false; toast(r.error||'启动失败','err'); return}
  const lg=el('tts-log'); lg.style.display=''; lg.textContent='已开始…';
  watchTts(r.task_id);
}
async function watchTts(tid){
  CUR_JOB=tid;
  const btn=el('btn-tts-setup'), stop=el('btn-stop-t'), lg=el('tts-log');
  if(stop) stop.style.display='';
  const tick=async()=>{
    const r=await api('/api/task/'+encodeURIComponent(tid));
    if(!r.ok){toast(r.error||'任务查不到','err'); return}
    const j=r.job||{};
    setBar('tts-bar', j.progress||0);
    if(lg) lg.textContent=(j.log||[]).join('\n')||'等待中…';
    if(j.status==='running'){setTimeout(tick,1500);return}
    if(stop) stop.style.display='none';
    if(btn) btn.disabled=false;
    toast(j.status==='done'?'本地语音环境已就绪':'搭建没成功：'+(j.error||''),
          j.status==='done'?'ok':'err');
    await refreshTtsState();
  };
  tick();
}

/* ---- 模型列表：本机装了什么由后端说了算，界面只负责摆出来 ---- */
let MODEL_LIST=[], MODEL_NOTE='';
const MODEL_MANUAL='__manual__';      // 下拉末项：填写列表外的名字
async function loadModels(){
  const backend=cfgVal('llm.backend')||'';
  const base=cfgVal('llm.base_url')||'';
  const r=await api('/api/models?backend='+encodeURIComponent(backend)+
                    '&base_url='+encodeURIComponent(base));
  MODEL_LIST=(r&&r.models)||[];
  MODEL_NOTE=(r&&r.ok)
    ? (MODEL_LIST.length?('本机可用 '+MODEL_LIST.length+' 个模型'):'后端没返回模型列表')
    : ('模型列表取不到：'+((r&&r.error)||'未知原因'));
  // 控件先渲染、列表后到（取列表是异步的），所以列表一到就回填下拉选项。
  $$('select[data-models]').forEach(fillModelSelect);
  patchModelNote();
}

/* 模型下拉的选项 = 当前值 ＋ 全部候选 ＋ 手动填写。
   列表取不到时（后端没开正是这种情况）也要能用：那时只剩「手动填写…」这条活路。 */
function fillModelSelect(sel){
  const cur=sel.dataset.cur||'';
  const list=MODEL_LIST.slice();
  const opts=[];
  if(cur&&list.indexOf(cur)<0) opts.push({v:cur,t:cur+'　（当前值，不在列表里）'});
  if(!cur) opts.push({v:'',t:'（未选模型）'});
  if(list.length) opts.push.apply(opts,list.map(m=>({v:m,t:m})));
  else opts.push({v:'',t:'（取不到列表）',off:1});
  opts.push({v:MODEL_MANUAL,t:'〔手动填写…〕'});
  sel.innerHTML=opts.map(o=>'<option value="'+esc(o.v)+'"'+(o.off?' disabled':'')+
    (o.v===cur&&!o.off?' selected':'')+'>'+esc(o.t)+'</option>').join('');
  sel.value=cur;
}
function wireModelSelect(node,key,v){
  node.dataset.cur=(v==null?'':String(v));
  fillModelSelect(node);
  node.onchange=()=>{
    if(node.value===MODEL_MANUAL){ askModel(node,key); return }
    put(key,node.value);
  };
}
function askModel(sel,key){
  modalInput('填写模型名','列表里没有的名字直接填在这里（远端后端常见）。',
    sel.dataset.cur||'', v=>{
      const name=String(v==null?'':v).trim();
      if(!name){ fillModelSelect(sel); return }    // 填了空：当作放弃，恢复原样
      sel.dataset.cur=name;
      fillModelSelect(sel);
      put(key,name);
    });
}
function patchModelNote(){
  const n=el(cid('cfg','llm.model')); if(!n) return;
  const wrap=n.closest('.f'); if(!wrap) return;
  let d=wrap.querySelector('.desc.model-note');
  if(!d){d=document.createElement('div');d.className='desc model-note';wrap.appendChild(d)}
  d.textContent=(MODEL_NOTE?MODEL_NOTE+'　':'')+
    '点开列全部；列表里没有的名字选末项「手动填写…」。';
}

/* ---- 素材 ---- */
async function loadAnchors(){
  const text=el('material').value;
  if(!text.trim()){toast('先粘贴内容');return}
  const r=await api('/api/ingest',{text:text});
  if(!r.ok){toast(r.error,'err');return}
  el('anchor').innerHTML='<option value="">全文（'+r.meta.chars+' 字）</option>'+
    (r.anchors||[]).map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('');
  el('mat-meta').textContent=r.meta.chars+' 字 · 锚点 '+(r.anchors||[]).length+' 个';
}
function bindFile(){
  const fi=el('file-input'); if(!fi) return;
  fi.onchange=async e=>{
    const f=e.target.files[0]; if(!f) return;
    const r=await api('/api/ingest',{filename:f.name,data_base64:await readFileB64(f)});
    if(!r.ok){toast(r.error,'err');return}
    el('material').value=r.text;
    el('mat-meta').textContent=f.name+' · '+r.meta.chars+' 字';
    el('anchor').innerHTML='<option value="">全文</option>'+
      (r.anchors||[]).map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('');
    toast('已载入 '+f.name+'（'+r.meta.chars+' 字）','ok');
  };
}
async function prepMaterial(){
  const text=el('material').value||'';
  const anchor=el('anchor').value;
  if(!anchor) return {material:text};
  const r=await api('/api/ingest',{text:text,anchor:anchor});
  if(!r.ok){toast(r.error,'err');return null}
  return {material:r.text};
}
function setBar(id,v){const n=el(id);if(n)n.style.width=Math.round(v*100)+'%'}

/* ---- 脚本 ---- */
function renderGauge(est,hint){
  const g=el('gauge');
  // 第四格报的是尺子本身（标准语速），不是这份稿子的实测值：脚本阶段与音色无关，
  // 摆一个「这份稿子的语速」在那儿，只会让人以为它可以随音色变。
  const s=CFG.standard||{};
  const rate=s.k?s.k.toFixed(2):'—';
  if(est){
    const dev=est.deviation_pct;
    const lim=cfgVal('gate.max_deviation_pct');
    const cls=Math.abs(dev)<=lim?'ok':'warn';
    g.innerHTML=
      '<div class="g"><div class="k">台词字数</div><div class="v">'+est.total_chars+'</div><div class="s">'+est.line_count+' 句</div></div>'+
      '<div class="g"><div class="k">预估时长</div><div class="v">'+fmt(est.total_seconds)+'</div><div class="s">目标 '+fmt(est.target_seconds)+'</div></div>'+
      '<div class="g"><div class="k">偏差</div><div class="v '+cls+'">'+(dev>0?'+':'')+dev.toFixed(1)+'%</div><div class="s">阈值 ±'+lim+'%</div></div>'+
      '<div class="g"><div class="k">标准语速</div><div class="v">'+rate+'</div><div class="s">有效字/秒 ≈'+(s.cpm||'—')+' 汉字/分</div></div>';
  }else{
    g.innerHTML='<div class="empty">生成脚本后显示</div>';
  }
  if(hint&&hint.options){
    el('hint').innerHTML='<div class="desc">目标时长反推（按标准语速）：'+
      hint.options.map(o=>'语速 '+o.speed.toFixed(2)+'x 约 '+o.effective_chars+' 字').join(' · ')+
      '</div><div class="desc">口径：全篇按标准语速估，与音色无关。</div>';
  }else{
    el('hint').innerHTML='';
  }
}
// 门禁结果有两个落点：脚本页看生成时那一次，合成页看落盘后的复核。
// 都往 #gates 写的话，合成页那张「校验报告」卡永远不会被填，一直显示
// 「合成完成后显示」——容器在、却没人往里写，比没有这个容器更误导。
function renderGates(rep,box,sum){
  if(!rep) return;
  box=box||'gates'; sum=sum||'gate-sum';
  const items=rep.items||[], pend=rep.pending||[], soft=rep.soft||[];
  // soft 项（总时长偏差）判的是估算值，不达标也不拦人，所以标记成「提示」
  // 而不是「阻断」——报告上要看得见它没达标，但不能让人以为稿子被卡住了。
  const mk=i=>(i.soft&&!i.ok)?['warn','·']:(i.advisory?(i.ok?['ok','✓']:['warn','?'])
    :(i.ok?['ok','✓']:['bad','✕']));
  const lv=i=>(i.soft&&!i.ok)?'提示·不阻断'
    :(i.advisory?'待复核':(i.level==='fail'?'阻断':'记录'));
  const s=el(sum);
  if(s){
    s.textContent=(rep.passed?'通过':'未通过')+'（'+items.filter(i=>i.ok).length+'/'+items.length+
      (pend.length?'，待复核 '+pend.length+' 项':'')+
      (soft.length?'，提示 '+soft.length+' 项（估算值，不阻断）':'')+'）';
    s.className='tag '+(rep.passed?((pend.length||soft.length)?'warn':'ok'):'bad');
  }
  const bx=el(box); if(!bx) return;
  bx.innerHTML=items.map(i=>{const c=mk(i)[0], m2=mk(i)[1];
    return '<div class="gate"><span class="mk '+c+'">'+m2+'</span>'+
    '<span class="lb" title="'+esc(i.label)+'">'+esc(i.label)+'</span>'+
    '<span class="dt" title="'+esc(i.detail)+'">'+esc(i.detail)+'</span>'+
    '<span class="lv'+((i.advisory||i.soft)&&!i.ok?' warn':'')+'">'+lv(i)+'</span></div>'}).join('');
}
function renderScript(){
  const tb=el('script-tbl').querySelector('tbody');
  el('script-meta').textContent=SCRIPT.length+' 句';
  if(!SCRIPT.length){tb.innerHTML='<tr><td colspan="7" class="empty">尚无脚本</td></tr>';return}
  tb.innerHTML=SCRIPT.map((s,i)=>
    '<tr><td class="num">'+(i+1)+'</td>'+
    '<td><select onchange="SCRIPT['+i+'].speaker=this.value;recalc()" style="width:100%">'+
      '<option value="A"'+(s.speaker==='A'?' selected':'')+'>A</option>'+
      '<option value="B"'+(s.speaker==='B'?' selected':'')+'>B</option></select></td>'+
    '<td><select onchange="SCRIPT['+i+'].emotion=this.value" style="width:100%">'+
      EMOTIONS.map(w=>'<option value="'+w+'"'+(s.emotion===w?' selected':'')+'>'+w+'</option>').join('')+
      '</select></td>'+
    '<td><input type="text" value="'+esc(s.text)+'" onchange="SCRIPT['+i+'].text=this.value;recalc()" style="width:100%"></td>'+
    '<td class="num">'+(s.text||'').length+'</td>'+
    '<td class="num">'+(s.estimated_seconds!=null?s.estimated_seconds:'-')+'</td>'+
    '<td class="num">'+(s.actual_seconds!=null?s.actual_seconds:'-')+'</td></tr>').join('');
}
function recalc(){
  if(!SCRIPT.length) return;
  api('/api/script/gate',{script:SCRIPT, project_id:(el('s-project')||{}).value||'',
      episode_no:(LOADED_EP||picked('s')[0]||'')}).then(r=>{
    if(!r.ok) return;
    SCRIPT=r.estimate.rows.map((row,i)=>Object.assign({},SCRIPT[i],
      {estimated_seconds:row.estimated_seconds,actual_seconds:row.actual_seconds}));
    renderGauge(r.estimate); renderGates(r.report); renderScript();
  });
}
/* ---- 选期（脚本页与合成页共用一套） ---- */
async function loadEpisodes(which){
  const wrap=el(which+'-eps-wrap');
  const pid=(el(which+'-project')||{}).value||'';
  EPILOADED[which]=true;
  EPISEL[which].clear();
  /* 切回单集模式时收起选期表，顺手把上一次的期表与计数抹掉：整个收起却留着
     旧行，下次展开前若有一帧没重绘，show 出来的就是上一个项目的期。 */
  if(!pid){
    EPISODES[which]=[];
    if(wrap) wrap.style.display='none';
    const box=el(which+'-eps-box'), cnt=el(which+'-eps-cnt');
    if(box) box.innerHTML='';
    if(cnt) cnt.textContent='';
    return
  }
  if(wrap) wrap.style.display='';
  const r=await api('/api/scripts?project_id='+encodeURIComponent(pid));
  let items=(r&&r.items)||[];
  /* 合成页只列有脚本的期。没脚本的期不出现在可选项里，「选了一期却没脚本」
     这个状态就产生不了——门禁在前，不做事后补救。 */
  if(which==='r') items=items.filter(x=>x.has_script);
  EPISODES[which]=items;
  /* 默认替人勾上「该做的那一期」：脚本页是第一个还没有脚本的，合成页是第一个
     有脚本却还没出片的。一次都不点也能直接开工。 */
  const first = which==='s' ? items.find(x=>!x.has_script)
                            : items.find(x=>x.has_script&&!x.done);
  if(first) EPISEL[which].add(first.no);
  renderPick(which);
}
/* 切页进来补一次数据刷新（showTab 用）：只重拉期表，**不动勾选**。
   loadEpisodes 会清空选择并按默认重勾——拿来当切页刷新的话，切一次页
   勾选就没一次。这里把仍存在的勾选原样保留（用户清过空或勾过自定义组合
   都不顶掉），新出现的期不自动勾；这一页从没载入过才走默认勾选那条路。
   只重绘选期表，正画布上已调出的稿子（SCRIPT）一概不碰。 */
async function refreshEpisodes(which){
  const pid=(el(which+'-project')||{}).value||'';
  if(!pid||!EPILOADED[which]){ await loadEpisodes(which); return }
  const prev=[...EPISEL[which]];
  const r=await api('/api/scripts?project_id='+encodeURIComponent(pid));
  if(!r||!r.ok) return;
  let items=(r.items)||[];
  if(which==='r') items=items.filter(x=>x.has_script);
  EPISODES[which]=items;
  EPISEL[which]=new Set(prev.filter(no=>items.some(x=>x.no===no)));
  renderPick(which);
}
function renderPick(which){
  const items=EPISODES[which]||[], sel=EPISEL[which];
  const box=el(which+'-eps-box'), cnt=el(which+'-eps-cnt');
  if(cnt) cnt.textContent=sel.size?(' · 已选 '+sel.size+' 期'):'';
  if(!box) return;
  if(!items.length){
    box.innerHTML='<div class="empty">'+(which==='r'
      ?'这个项目还没有任何一期有脚本——先到「脚本」页生成。'
      :'没有可选的期（成稿规划要先排图）。')+'</div>';
    return;
  }
  box.innerHTML=items.map(x=>{
    const badge=x.has_script?('<span class="badge ok">'+x.lines+' 句</span>')
                            :'<span class="badge">无脚本</span>';
    const done=x.done?'<span class="badge">已出片</span>':'';
    const peek=(which==='s'&&x.has_script)
      ?'<a class="peek" onclick="event.stopPropagation();peekScript(\''+x.no+'\')">调出来改</a>':'';
    return '<label class="pickrow"><input type="checkbox" '+(sel.has(x.no)?'checked':'')+
      ' onchange="toggleEp(\''+which+'\',\''+x.no+'\',this.checked)">'+
      '<span class="no">第 '+x.no+' 期</span><span class="ti">'+esc(x.title||'')+'</span>'+
      badge+done+peek+'</label>';
  }).join('')+
  '<div class="pickfoot"><a onclick="pickAll(\''+which+'\',true)">全选</a>'+
  '<a onclick="pickAll(\''+which+'\',false)">清空</a>'+
  '<a onclick="closePick()">收起</a></div>';
}
function toggleEp(which,no,on){
  if(on) EPISEL[which].add(no); else EPISEL[which].delete(no);
  renderPick(which);
}
function pickAll(which,on){
  const s=EPISEL[which]; s.clear();
  if(on) (EPISODES[which]||[]).forEach(x=>s.add(x.no));
  renderPick(which);
}
function togglePick(which){
  const box=el(which+'-eps-box'); if(!box) return;
  const was=box.classList.contains('on');
  closePick();
  if(!was) box.classList.add('on');
}
function closePick(){ document.querySelectorAll('.pickbox.on').forEach(b=>b.classList.remove('on')) }
document.addEventListener('click',e=>{ if(!e.target.closest('.pick')) closePick() });

/* 把某一期已经落盘的脚本调出来改。改完点「存回本期」，合成时读的就是这一份。 */
async function peekScript(no){
  const pid=(el('s-project')||{}).value||'';
  const r=await api('/api/script?project_id='+encodeURIComponent(pid)+
                    '&episode_no='+encodeURIComponent(no));
  if(!r.ok){toast(r.error,'err');return}
  SCRIPT=r.script||[]; LOADED_EP=no;
  el('gen-title-wrap').style.display='';
  renderScript(); recalc();
  closePick();
  toast('已载入第 '+no+' 期脚本（'+SCRIPT.length+' 句）· 改完点「存回本期」','ok');
}
async function saveScript(){
  const pid=(el('s-project')||{}).value||'';
  if(!pid){toast('单集模式没有「本期」可存——它不归属任何项目','err');return}
  if(!SCRIPT.length){toast('没有脚本可存','err');return}
  const no=LOADED_EP||picked('s')[0]||'';
  if(!no){toast('先选一期','err');return}
  const r=await api('/api/script/save',{project_id:pid,episode_no:no,script:SCRIPT});
  if(!r.ok){toast(r.error,'err');return}
  toast('第 '+no+' 期脚本已存回项目','ok');
  loadEpisodes('s');
}
async function stopJob(which){
  if(!CUR_JOB){toast('没有正在跑的任务');return}
  const r=await api('/api/job/stop',{task_id:CUR_JOB});
  toast(r.ok?'已请求中止：当前这一步做完就停，不再开新的一期':(r.error||'中止失败'),
        r.ok?'ok':'err');
}
/* 盯一个批任务：进度写成「第 N/M 期 · 第 k 步」，跑完报账。
   中止按钮只在任务跑着的时候露出来——平时摆着容易误点。 */
async function watchBatch(tid,which){
  const btn=el('btn-stop-'+which);
  const st=el(which==='s'?'gen-status':'render-stage');
  const lg=el(which==='s'?'gen-log':'render-log');
  CUR_JOB=tid;
  if(btn) btn.style.display='';
  const tick=async()=>{
    const r=await api('/api/task/'+encodeURIComponent(tid));
    if(!r.ok){toast(r.error||'任务查不到','err');return}
    const j=r.job||{}, b=j.batch||{};
    const head=b.total?('第 '+b.index+'/'+b.total+' 期 · '):'';
    setBar(which==='s'?'gen-bar':'render-bar',
           b.total?((b.index-1+(j.progress||0))/b.total):(j.progress||0));
    if(st) st.textContent=head+(j.stage||'');
    if(lg) lg.textContent=(j.log||[]).join('\n')||'等待中…';
    // 等下一轮要**等着**：链子串成一条 Promise，外层 await 才等得到真跑完。
    // 从前这里 setTimeout 一扔就走，await 的调用方当场就往下执行——按钮于是
    // 在任务刚起步时就恢复了，看着像已经收工。
    if(j.status==='running'){await new Promise(r=>setTimeout(r,1200));return tick()}
    if(btn) btn.style.display='none';
    const res=j.result||{};
    if(j.status==='done'){
      const ok=(res.done||[]).length, bad=(res.failed||[]).length;
      if(lg&&bad) lg.textContent+='\n\n失败明细：\n'+
        (res.failed||[]).map(f=>'第 '+f.no+' 期：'+f.error).join('\n');
      toast('批量跑完：成功 '+ok+' 期'+(bad?('，失败 '+bad+' 期（可在选期里重勾再跑）'):''),
            bad?'err':'ok');
    }else{
      toast('批任务失败：'+(j.error||''),'err');
    }
    loadProjects(); loadHistory(); loadEpisodes(which);
  };
  return tick();
}

async function genScript(){
  const pid=(el('s-project')||{}).value||'';
  /* 料源按范式分流：成稿规划的本期料源由后端按地图落点取，本页输入口不参与；
     逐期即兴才在这里读人填的内容。下拉里已经不会出现未排图的成稿规划项目，
     所以这里不必再处理"缺地图"这种情形——那一步已经在上游堵掉了。 */
  const p=PROJECTS.find(x=>x.id===pid);
  const fromMap=!!p&&((p.progress||{}).mode==='mapped');
  const pm=fromMap?{material:''}:(await prepMaterial());
  if(!pm) return;
  if(!pm.material.trim()&&!pid){
    toast('素材为空：粘贴内容、上传文件，或选择一个项目走地图落点','err');return
  }
  const eps=pid?picked('s'):[];
  if(pid&&!eps.length){toast('先在「要做哪几期」里勾上期数','err');return}
  /* 多期交给批任务：勾八期就得点八次、每回还得盯着跑完，批量就是替掉这份守候。
     批任务里每一期走的是与单期同一条生成路径，口径不会漂成两样。 */
  if(pid&&eps.length>1){
    const rb=await api('/api/batch',{kind:'script',project_id:pid,episodes:eps});
    if(!rb.ok){toast(rb.error,'err');return}
    el('btn-gen').disabled=true;
    toast('开始生成 '+eps.length+' 期脚本（串行，失败跳过）','ok');
    await watchBatch(rb.task_id,'s');
    el('btn-gen').disabled=false;
    return;
  }
  /* 单期也走后台任务：模型要慢慢写十几分钟到半小时，压在请求里等，中途
     刷新、关页面、服务重启就是全白跑，而且一路上看不到任何进展。合成与批任务
     早就这么办了，脚本没道理例外——两者共用同一套 /api/task 轮询与中止。 */
  el('btn-gen').disabled=true; el('gen-status').textContent='排队'; setBar('gen-bar',0);
  el('gen-log').textContent=(fromMap||!pm.material.trim())?'按地图落点取素材…':'调用模型…';
  try{
    const r0=await api('/api/script/generate',{
      material:pm.material,
      preset:cfgVal('script.style_preset'), project_id:pid, episode_no:eps[0]||''});
    if(!r0.ok){el('gen-log').textContent='失败：'+(r0.error||'');
      el('gen-status').textContent='失败'; toast(r0.error||'起任务失败','err'); return}
    await watchScript(r0.task_id, eps[0]||'');
  }catch(e){toast('请求失败：'+e,'err'); el('gen-status').textContent='失败'}
  finally{el('btn-gen').disabled=false}
}

/* 单期脚本任务的进度看板。它与 watchBatch、watchTts 是同一套路子：
   读 /api/task/<id>，跑着就再等一会儿，完了按结果分支。 */
async function watchScript(tid,no){
  const btn=el('btn-gen'), stop=el('btn-stop-s');
  CUR_JOB=tid;
  if(stop) stop.style.display='';
  const tick=async()=>{
    const r=await api('/api/task/'+encodeURIComponent(tid));
    if(!r.ok){toast(r.error||'任务查不到','err'); if(btn) btn.disabled=false; return}
    const j=r.job||{};
    setBar('gen-bar',j.progress||0);
    el('gen-status').textContent=j.stage||'写作中';
    el('gen-log').textContent=(j.log||[]).join('\n')||'等待中…';
    // 与 watchBatch 同一条规矩：链子串成一条 Promise，外层 await 才等得到
    // 真跑完。单期生成按钮由调用方的 finally 恢复，这里一扔就走的话，
    // 按钮在任务刚起步时就亮了。
    if(j.status==='running'){await new Promise(r=>setTimeout(r,1500));return tick()}
    if(stop) stop.style.display='none';
    if(btn) btn.disabled=false;
    if(j.status!=='done'){
      // 任务级失败（模型连不上、后端返回非 JSON 之类）：原样说清起因，
      // 不比从前那种「请求失败」少一个字。
      el('gen-status').textContent='失败';
      el('gen-log').textContent=(j.log||[]).join('\n')+'\n失败：'+(j.error||'');
      toast('生成失败：'+(j.error||''),'err'); return;
    }
    const r2=j.result||{};
    if(!r2.ok){
      el('gen-status').textContent='失败';
      el('gen-log').textContent=(r2.logs||[]).join('\n')+'\n失败：'+(r2.error||'');
      toast(r2.error||'生成失败','err'); return;
    }
    SCRIPT=r2.script;
    LOADED_EP=no;
    if(r2.title){el('gen-title').value=r2.title; el('r-title').value=r2.title; el('gen-title-wrap').style.display=''}
    el('gen-log').textContent=(r2.logs||[]).join('\n')+'\n'+
      '标题：'+(r2.title||'（无）')+'\n'+
      '门禁 '+(r2.report.passed?'通过':'未通过（'+(r2.report.fails.length+r2.report.warns.length)+' 项）')+
      ((r2.report.pending||[]).length?'，待复核 '+r2.report.pending.length+' 项（模型未给出结论，不阻断）':'')+
      ((r2.report.soft||[]).length?'，提示 '+r2.report.soft.length+' 项（估算值，不阻断）':'')+
      (r2.degraded?'\n注意：后端不支持约束解码，已降级为提示词约束。':'');
    el('gen-status').textContent='第 '+(r2.attempt||1)+' 轮';
    // 内容检的结论就在 report 里（同一个对象），不再单独渲染一遍。
    renderScript(); renderGauge(r2.estimate); renderGates(r2.report);
    toast('脚本已生成'+(r2.title?('：'+r2.title):'')+(r2.planned_episodes?('（建议共 '+r2.planned_episodes+' 期）'):''),'ok');
    if(el('s-project').value) loadProjects();
  };
  return tick();
}
async function gateOnly(){
  if(!SCRIPT.length){toast('尚无脚本');return}
  await recalc(); toast('已重新校验','ok');
}

/* ---- 合成 ---- */
function swOn(id){const n=el(id);return n?n.classList.contains('on'):false}
async function render(){
  const pid=el('r-project').value||'';
  const eps=pid?picked('r'):[];
  if(pid&&!eps.length){toast('先在「合成哪几期」里勾上期数','err');return}

  /* 脚本阶段留下的问题，在这里提醒一次：只提醒，不拦人。读的是脚本生成那一刻
     记下的结论，此后改没改以手上的稿子为准——框里把这话写明，否则改好了还弹，
     提醒就成了狼来了。 */
  if(pid){
    const iss=await api('/api/script/issues',{project_id:pid,episodes:eps});
    if(iss.ok&&(iss.issues||[]).length){
      modalHtml('脚本阶段的问题', scriptIssuesHtml(iss.issues),
                ()=>doRender(pid,eps,true),'仍然合成',true);
      return;
    }
  }
  await doRender(pid,eps,false);
}
function scriptIssuesHtml(list){
  return list.map(e=>'<div style="margin:0 0 14px">'+
    '<div style="margin-bottom:6px"><b>第 '+esc(e.episode_no)+' 期</b>'+
    (e.time?'<span style="color:var(--fg3);margin-left:8px">'+esc(e.time)+'</span>':'')+
    '</div>'+
    e.items.map(i=>'<div class="gate"><span class="mk '+(i.soft?'warn':'bad')+'">'+
      (i.soft?'·':'✕')+'</span><span class="lb">'+esc(i.label)+'</span>'+
      '<span class="dt" title="'+esc(i.detail)+'">'+esc(i.detail)+'</span>'+
      '<span class="lv'+(i.soft?' warn':'')+'">'+(i.soft?'提示·不阻断':'未通过')+
      '</span></div>').join('')+'</div>').join('')+
    '<div style="color:var(--fg3);font-size:11px;margin-top:10px">'+
    '以上是脚本生成时记录的问题。本记录不追踪此后的人工修改——若已改过，'+
    '以当前稿子为准。</div>';
}
async function doRender(pid,eps,ack){
  const ACK=ack?{ack_script_issues:true}:{};
  /* 归属项目时一律按（项目, 期号）读盘上那份脚本，不用本页内存里这一份。
     脚本的落点只有一个——就是「存回本期」存下的那一版。用内存里那份的话，
     改了忘了存、合成出来的却是旧稿，这种事一句提示都不会有。 */
  if(pid&&eps.length>1){
    const rb=await api('/api/batch',Object.assign({kind:'render',project_id:pid,
      episodes:eps,do_video:swOn('sw-do_video'),
      preset:cfgVal('script.style_preset')},ACK));
    if(!rb.ok){toast(rb.error,'err');return}
    el('btn-render').disabled=true;
    toast('开始合成 '+eps.length+' 期（串行，失败跳过）','ok');
    await watchBatch(rb.task_id,'r');
    el('btn-render').disabled=false;
    return;
  }
  if(pid){
    const rb=await api('/api/render',Object.assign({project_id:pid,episode_no:eps[0],
      do_video:swOn('sw-do_video'),preset:cfgVal('script.style_preset')},ACK));
    if(!rb.ok){toast(rb.error,'err');return}
    CUR_JOB=rb.task_id; el('btn-render').disabled=true;
    el('btn-stop-r').style.display='';
    toast('已开始合成第 '+eps[0]+' 期','ok'); pollJob();
    return;
  }

  /* 单集模式：不归属任何项目，用本页这份脚本，与从前一致。 */
  const title=el('r-title').value.trim();
  if(!title){toast('本期标题为空——先到「脚本」页生成脚本，标题随脚本产出','err');return}
  if(!SCRIPT.length){toast('尚无脚本，先到「脚本」页生成','err');return}
  const r=await api('/api/render',Object.assign({title:title,script:SCRIPT,
    material:el('material').value,preset:cfgVal('script.style_preset'),
    do_video:swOn('sw-do_video')},ACK));
  if(!r.ok){toast(r.error,'err');return}
  CUR_JOB=r.task_id; el('btn-render').disabled=true;
  el('btn-stop-r').style.display='';
  toast('已开始合成','ok'); pollJob();
}
async function pollJob(){
  if(!CUR_JOB) return;
  const r=await api('/api/task/'+CUR_JOB);
  if(!r.ok){el('btn-render').disabled=false;return}
  const j=r.job;
  setBar('render-bar',j.progress);
  el('render-stage').textContent=j.stage+(j.error?' · '+j.error:'');
  el('render-log').textContent=(j.log||[]).join('\n')||'等待中…';
  if(j.status==='running'){setTimeout(pollJob,1200);return}
  el('btn-render').disabled=false;
  const sb=el('btn-stop-r'); if(sb) sb.style.display='none';
  if(j.status==='done'){
    toast('合成完成','ok');
    const res=j.result||{};
    if(res.episode_files) showOutputs(res);
    loadProjects(); loadHistory(); loadEpisodes('r');
  }else{
    toast('合成失败：'+(j.error||''),'err');
    modal('合成失败',(j.error||'')+'\n\n'+(j.log||[]).slice(-8).join('\n'));
  }
}
/* 产物路径 → /api/file 认的相对地址（相对输出目录）。 */
function relOf(p){
  const parts=String(p||'').split(/[\\/]/).filter(Boolean);
  const i=parts.lastIndexOf('projects');
  return i<0?'':parts.slice(i+1).join('/');
}
function fileHref(rel){return '/api/file/'+String(rel).split('/').filter(Boolean).map(encodeURIComponent).join('/')}
/* 产物按类分放在项目里，所以逐个取路径表里的确切位置，不再拿目录名去猜文件名。 */
function showOutputs(res){
  const ep=res.episode_files||{};
  let h='';
  const item=(p,f)=>{if(p){const n=String(p).split(/[\\/]/).pop();
    h+='<div class="kv"><b>'+f+'</b><a target="_blank" href="'+fileHref(relOf(p))+'">'+esc(n)+'</a></div>'}};
  /* 试听就地播：音频视频不再是只有文件名的链接，下面直接给播放器。 */
  const media=(p,tag)=>{if(p){h+='<'+tag+' controls preload="none" src="'+
    fileHref(relOf(p))+'"></'+tag+'>'}};
  item(ep.video,'横屏视频'); item(ep.video_vertical,'竖屏视频'); item(ep.audio,'音频');
  media(ep.audio,'audio'); media(ep.video,'video'); media(ep.video_vertical,'video');
  item(ep.subtitle,'字幕'); item(ep.subtitle_lrc,'字幕（歌词 LRC）'); item(ep.article,'图文');
  item(ep.bg_h,'背景图（横屏）'); item(ep.bg_v,'背景图（竖屏）');
  const cov=ep.cover||{};
  Object.keys(cov).forEach(k=>{const n=String(cov[k]).split(/[\\/]/).pop();
    h+='<div class="kv"><b>封面 '+esc(k)+'</b><a target="_blank" href="'+fileHref(relOf(cov[k]))+'">'+esc(n)+'</a></div>'});
  el('outputs').innerHTML=h||'<div class="empty">尚无产物</div>';
  renderGates(res.report,'report','rep-sum');
}
async function loadHistory(){
  const r=await api('/api/projects');
  if(!r.ok) return;
  const ps=r.projects||[];
  /* 一期由「树根 + 期号」定位：产物按类型分放在项目里，没有「期目录」
     这种东西可以指。 */
  el('history').innerHTML=ps.length?ps.slice(0,20).map(p=>
    '<div class="kv"><b>'+esc((p.created||'').slice(0,16))+'</b><span>'+
    (p.episode_no?('第 '+esc(p.episode_no)+' 期 · '):'')+esc(p.title||p.root)+
    ' <a href="#" onclick="viewReport(\''+esc(p.root)+'\',\''+esc(p.no)+'\');return false">报告</a></span></div>').join('')
    :'<div class="empty">尚无产出</div>';
}
async function viewReport(root,no){
  const r=await api('/api/report/'+encodeURIComponent(root)+'/'+encodeURIComponent(no));
  if(!r.ok){toast(r.error,'err');return}
  /* 报告接口同时回了 episode_files：产物区一起填——历史期的成片就地试听，
     与「刚合成完」走的是同一个 showOutputs，不另写第二份渲染。 */
  showOutputs(r);
  goTab('render');
}

/* ---- 项目 ---- */
async function loadProjects(){
  const r=await api('/api/project');
  if(!r.ok){toast(r.error||'项目登记表不可用','err');return}
  PROJECTS=r.projects||[];
  if(!PROJECTS.find(p=>p.id===ACTIVE_PROJ)) ACTIVE_PROJ='';
  renderProjects(); renderProjectOptions(); renderOrphans();
  loadHistory();
}
function renderOrphans(){
  api('/api/project?scope=orphans').then(r=>{
    const list=(r&&r.orphans)||[];
    el('orphan-sum').textContent=list.length?list.length+' 个':'无';
    el('orphans').innerHTML=list.length?list.slice(0,20).map(o=>
      '<div class="kv"><b>'+esc(o.dir)+'</b><span>整棵树没有归属项目（单集产出）</span></div>').join('')
      :'<div class="empty">无</div>';
  });
}
/* 成稿规划必须排过图才算「可归属」：本期料源由地图落点决定，没有地图就没有
   料源。判据只有这一条，项目卡片与两个页面的下拉共用，免得各写一份漂开。 */
function planReady(p){
  const pr=p.progress||{};
  return !(pr.mode==='mapped')||!!pr.mapped;
}
function renderProjects(){
  const box=el('projects');
  el('proj-sum').textContent=PROJECTS.length?PROJECTS.length+' 个项目':'';
  if(!PROJECTS.length){box.innerHTML='<div class="empty">尚无项目。左侧立项后即可按期推进。</div>';return}
  box.innerHTML=PROJECTS.map(p=>{
    const pr=p.progress||{};
    const mapped=pr.mode==='mapped';
    const pct=pr.planned?Math.min(100,Math.round(pr.done/pr.planned*100))
                        :(pr.done?Math.min(100,pr.done*10):0);
    let h='<div class="proj'+(p.archived?' arch':'')+(p.id===ACTIVE_PROJ?' on':'')+'">';
    h+='<div class="top"><span class="nm">'+esc(p.name)+'</span>'+
       '<span class="tag" style="font-size:11px">'+esc(pr.mode_label||'')+'</span>'+
       (p.program_name&&p.program_name!==p.name?'<span class="dim" style="font-size:11px">节目名 '+esc(p.program_name)+'</span>':'')+
       (p.subtitle?'<span class="dim" style="font-size:11px">副标题 '+esc(p.subtitle)+'</span>':'')+
       '<span class="sp"></span><span class="dim" style="font-size:11px">'+esc(pr.label||'')+'</span></div>';
    h+='<div class="bar" style="margin:8px 0 10px"><i style="width:'+pct+'%"></i></div>';
    h+='<div class="meta">'+
       (pr.single?'<span>单集 · 不排图</span>'
                 :'<span>下一期 第 '+esc(pr.next_episode||'1')+' 期</span>')+
       '<span>风格 '+esc(styleLabel(p.style_preset)||'跟随全局')+'</span>'+
       (pr.single?'':'<span>素材 '+esc(pr.paradigm_label||'')+'</span>')+
       '<span>建立 '+esc((p.created||'').slice(0,16))+'</span></div>';
    if(p.note) h+='<div class="meta"><span>'+esc(p.note)+'</span></div>';
    if((p.episodes||[]).length){
      h+='<div class="eps">'+p.episodes.slice().reverse().map(e=>
        '<div class="ep"><span class="no">第'+esc(e.no)+'期</span><span class="ti">'+esc(e.title)+
        '</span><span class="dt">'+esc((e.created||'').slice(5,16))+'</span>'+
        '<a href="#" onclick="viewReport(\''+esc(p.id)+'\',\''+esc(e.no)+'\');return false">报告</a></div>').join('')+'</div>';
    }
    const takeable=planReady(p);
    h+='<div class="acts">'+
       (takeable?('<button class="mini" onclick="chooseProject(\''+esc(p.id)+'\')">设为当前</button>')
                :'<button class="mini" disabled title="成稿规划需先排出期数地图，才能选为当前">设为当前</button>')+
       (pr.legacy?('<button class="mini" onclick="setPlanMode(\''+esc(p.id)+'\',\'mapped\')">定为成稿规划</button>'+
                   '<button class="mini" onclick="setPlanMode(\''+esc(p.id)+'\',\'episodic\')">定为逐期即兴</button>'+
                   '<button class="mini" onclick="setPlanMode(\''+esc(p.id)+'\',\'single\')">定为单集</button>'):'')+
       (mapped?('<button class="mini" onclick="editParadigm(\''+esc(p.id)+'\')">素材类型</button>'+
                '<button class="mini" onclick="openSources(\''+esc(p.id)+'\')">成稿</button>'+
                '<button class="mini" onclick="openMap(\''+esc(p.id)+'\')">地图'+
                (pr.mapped?(' · '+pr.mapped+' 期'):' · 未排')+'</button>'):'')+
       // 「音色」只在本地引擎下出现：Edge 那档的音色是在线的名字，没有「本项目
       // 的参考音频」这回事，摆一个点开是空的按钮不如不摆。
       (String((CFG||{})['tts.engine']||'')==='qwen3tts'
         ?'<button class="mini" onclick="openVoices(\''+esc(p.id)+'\')">音色</button>':'')+
       '<button class="mini" onclick="editIdentity(\''+esc(p.id)+'\')">节目名/副标题</button>'+
       // 单集没有下一期，这个按钮对它没有意义——摆着只会让人以为还能再出一集。
       (pr.single?'':'<button class="mini" onclick="editNext(\''+esc(p.id)+'\',\''+esc(pr.next_episode||'1')+'\')">改下一期号</button>')+
       '<button class="mini" onclick="setArchived(\''+esc(p.id)+'\','+(p.archived?'false':'true')+')">'+(p.archived?'恢复':'归档')+'</button>'+
       '<button class="mini del'+(ARM_DEL===p.id?' arm':'')+'" onclick="delProject(\''+esc(p.id)+'\')">'+
       (ARM_DEL===p.id?'确认删除':'删除')+'</button>'+
       '</div></div>';
    return h;
  }).join('');
}
function renderProjectOptions(){
  /* 没排图的成稿规划项目不进下拉：让它列在这里，等于把「该不该能选」推到
     生成时才作答。归档的照旧不列。
     单集不再是一个下拉项——它就是一种项目（见 project_store 的 plan_mode）。
     单列成「不归属项目」等于把流程又劈成两条，而它们本该是同一条。 */
  const usable=PROJECTS.filter(p=>!p.archived&&planReady(p));
  if(!usable.find(p=>p.id===ACTIVE_PROJ)){
    // 当前项目没了或还没选过，就落到第一个可用的。不这么做，进去就是空选，
    // 而空选在合成端等于单集——又变回「不说一声就走了另一条路」。
    ACTIVE_PROJ=usable.length?usable[0].id:'';
    localStorage.setItem('pm_proj',ACTIVE_PROJ);
  }
  const opts=usable.length
    ? usable.map(p=>{
        const pr=p.progress||{};
        const tail=pr.single?' · 单集':' · 下一期第 '+esc(pr.next_episode||'1')+' 期';
        return '<option value="'+esc(p.id)+'">'+esc(p.name)+tail+'</option>';
      }).join('')
    : '<option value="">（请先在项目页立项）</option>';
  ['s-project','r-project'].forEach(id=>{
    const n=el(id); if(!n) return;
    n.innerHTML=opts; n.value=ACTIVE_PROJ||'';
  });
  renderProjNote('r');
  renderProjNote('s');
}
/* 两个页面的项目下拉共用一个当前项目：在这里选与在项目卡片上「设为当前」
   是同一件事。否则重新拉一次项目表，手选的归属会被打回原值。 */
function pickProject(which){
  const sel=el(which+'-project'); if(!sel) return;
  ACTIVE_PROJ=sel.value||'';
  localStorage.setItem('pm_proj',ACTIVE_PROJ);
  renderProjectOptions();
  renderProjects();
}
function renderProjNote(which){
  const sel=el(which+'-project'); if(!sel) return;
  const id=sel.value;
  const p=PROJECTS.find(x=>x.id===id);
  const note=el(which+'-proj-note');
  if(p){
    const pr=p.progress||{};
    if(note) note.textContent=p.name+' · 已出 '+(pr.done||0)+' 期'+
      (pr.single?'（单集）'
                :(pr.planned?(' / 计划 '+pr.planned+' 期'):'（总期数未定）')+
                 ' · 下一期第 '+(pr.next_episode||'1')+' 期');
    if(which==='s') renderMaterialSource(p);
  }else{
    if(note) note.textContent='还没有可用的项目。先到「项目」页立项——只出一集也是一次立项。';
    if(which==='s') renderMaterialSource(null);
  }
  /* 选期表跟着项目走。没项目就把选期收起来——单集模式没有「第几期」这回事。 */
  loadEpisodes(which);
}
/* 料源随范式切换：成稿规划的料源由地图落点决定，输入口收起来、只读摆出本期
   会取哪几节——自动取料最怕取错了没人看出来；逐期即兴的料源是当场给的，输入
   口就是正解。这里只回答「料从哪来」，"能不能做"的门禁在上游入口，不在这儿。 */
function renderMaterialSource(p){
  const locked=el('mat-locked'), src=el('mat-src'), input=el('mat-input');
  const pr=(p||{}).progress||{};
  const fromMap=!!p&&pr.mode==='mapped';
  if(input) input.style.display=fromMap?'none':'';
  if(locked) locked.style.display=fromMap?'':'none';
  if(!fromMap) return;
  const eps=(p.map&&p.map.episodes)||[];
  const row=eps.find(e=>String(e.no)===String(pr.next_episode||'1'));
  const refs=(row&&row.refs)||[];
  if(src) src.innerHTML='<b>本期料源＝地图落点</b>　第 '+esc(pr.next_episode||'1')+' 期'+
    (row?('「'+esc(row.title||'未命名')+'」'):'')+'<br>'+
    (refs.length?('将取用：'+refs.map(r=>esc(r.source)+' · '+esc(r.anchor||'全文')).join('；'))
               :'（地图里这一期没有落点）')+
    '<br>要改本期讲什么，去项目卡片上的「地图」改。';
  const meta=el('mat-meta'); if(meta) meta.textContent='按地图落点';
}
function chooseProject(pid){
  ACTIVE_PROJ=(ACTIVE_PROJ===pid)?'':pid;
  localStorage.setItem('pm_proj',ACTIVE_PROJ);
  renderProjects(); renderProjectOptions();
  toast(ACTIVE_PROJ?'已切到该项目，脚本与合成都会挂在它下面':'已取消归属','ok');
}
function bindMode(){
  const box=el('np-mode'); if(!box) return;
  box.querySelectorAll('button').forEach(b=>b.onclick=()=>{
    box.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    syncModeFields();
  });
  syncModeFields();
}
/* 单集不出第二期，计划期数与起始期号对它没有意义。不收起的话，人会以为填了
   能出两期——那就成了「填了不生效」的死框，跟从前那个封面标题框一样。 */
function syncModeFields(){
  const single=pickMode()==='single';
  ['np-planned-wrap','np-first-wrap'].forEach(id=>{
    const n=el(id); if(n) n.style.display=single?'none':'';
  });
}
function pickMode(){
  const on=el('np-mode').querySelector('button.on');
  return on?on.dataset.mode:'mapped';
}
async function createProject(){
  const name=el('np-name').value.trim();
  if(!name){toast('请填项目名称','err');return}
  const mode=pickMode();
  const planned=el('np-planned').value.trim();
  const body={action:'create',name:name,plan_mode:mode,
    program_name:el('np-program').value.trim(),
    subtitle:el('np-subtitle').value.trim(),
    audience:el('np-audience').value.trim(),
    planned_episodes:planned?parseInt(planned,10):null,
    first_episode:el('np-first').value.trim()||'1',
    style_preset:el('np-style').value,note:el('np-note').value.trim(),
    paradigm:el('np-paradigm').value,
    focus_note:el('np-focus').value.trim()};
  const r=await api('/api/project',body);
  if(!r.ok){toast(r.error,'err');return}
  el('np-name').value=''; el('np-program').value=''; el('np-subtitle').value='';
  el('np-audience').value='';
  el('np-planned').value=''; el('np-note').value=''; el('np-focus').value='';
  ACTIVE_PROJ=r.project.id; localStorage.setItem('pm_proj',ACTIVE_PROJ);
  await loadProjects();
  if(mode==='mapped'){
    toast('已立项：'+name+'（成稿规划）','ok');
    // 成稿规划的价值全在"先把稿子给进来"。立完项直接把素材面板推出来，
    // 否则用户会以为还得自己去某个角落找入口。
    openSources(r.project.id);
  }else if(mode==='single'){
    // 单集的料源当场给，没有「先入库」这一步，因此不推成稿面板。
    toast('已立项：'+name+'（单集）','ok');
  }else{
    toast('已立项：'+name+'（逐期即兴）','ok');
  }
}

/* ---- 成稿与期数地图 ---- */
function projName(pid){const p=PROJECTS.find(x=>x.id===pid);return p?p.name:pid}
/* 分支期号的判据与后端 branch_no() 同一条规则，这里只用来给行上色；
   真正编期号的地方只有后端一处。 */
function isBranchNo(no){return /[a-z]+$/.test(String(no||''))}
function refsText(refs){
  const list=refs||[];
  if(!list.length) return '<span style="color:var(--red)">无落点</span>';
  return list.map(r=>esc(r.source)+' · '+esc(r.anchor||'全文')).join('<br>');
}
/* 分块转 base64。逐字节拼接在大文件上是 O(n²)，一本几十万字的书会卡住页面。 */
async function readFileB64(f){
  const bytes=new Uint8Array(await f.arrayBuffer());
  let bin=''; const CH=0x8000;
  for(let i=0;i<bytes.length;i+=CH) bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+CH));
  return btoa(bin);
}

/* 上传区。成稿面板与插入面板共用同一份实现：两处各写一套 DOM 的话，迟早
   一处加了 docx 支持、另一处没有，而两个面板单看都正常。scope 既是元素 id
   前缀，也是 addSource() 入库后该重画哪个面板的依据。
   「重新探查」只出现在成稿面板：那是补跑与重判的入口，插入时用不上。 */
function uploadBlock(pid,scope){
  const p=scope+'-';
  return '<div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">'+
    '<div class="f"><label>上传文件 <span class="hint">md / txt / docx</span></label>'+
    '<input type="file" id="'+p+'file" accept=".md,.markdown,.txt,.docx"></div>'+
    '<div class="f"><label>或粘贴正文</label><textarea id="'+p+'text" rows="4" placeholder="粘贴要排进播出的正文"></textarea></div>'+
    '<div class="btn-row"><button class="btn primary" id="'+p+'add-btn" onclick="addSource(\''+esc(pid)+'\',\''+scope+'\')">入库</button>'+
    (scope==='src'?('<button class="btn" id="src-scan-btn" onclick="scanSources(\''+esc(pid)+'\')">重新探查</button>'):'')+
    '</div></div>';
}

/* 取上传区的文件或粘贴文本。都没有就回 null，由调用方提示。 */
async function collectUpload(scope){
  const box=el(scope+'-file');
  const f=box&&box.files&&box.files[0];
  if(f) return {filename:f.name,data_base64:await readFileB64(f)};
  const t=((el(scope+'-text')||{}).value||'').trim();
  return t?{text:t}:null;
}

/* 素材号在地图里用过的集合。插入面板靠它区分「新材料」与「已经排进地图的
   那一份」——后者勾下去，模型会把同一份素材当新素材再插一遍，期号变
   3a/3b、内容与第 3 期重复，而这件事不报错，只安静地交出一张错地图。 */
function mapSourceIds(p){
  const s=new Set();
  (((p||{}).map||{}).episodes||[]).forEach(e=>
    (e.refs||[]).forEach(r=>{ if(r&&r.source) s.add(r.source) }));
  return s;
}

/* 插入面板当前勾了哪些。入库后要重画面板，得先把人的勾选接住，
   否则传完一份素材回来，之前挑好的全没了。 */
function checkedInsIds(){
  const box=el('ins-src'); if(!box) return [];
  return Array.from(box.querySelectorAll('input:checked')).map(n=>n.value);
}

async function openSources(pid){
  const r=await api('/api/sources?project_id='+encodeURIComponent(pid));
  if(!r.ok){toast(r.error,'err');return}
  const list=r.sources||[];
  let h='<div class="desc" style="margin-bottom:12px">成稿入库后按章节切分。排地图只喂章节清单，生成某一期时才按落点取回原文。'
       +'入库会自动探查一次结构与类型，结果落盘冻结——排图直接用这一份，不必再等。</div>';
  h+='<div id="src-list">'+(list.length?list.map(s=>{
    const pv=s.probe||{};
    /* 结构摘要取自探查结果，不由前端另算一份。未探查时退回入库时的粗计数，
       并写明「尚未探查」，免得把两种口径混成一句。 */
    const meta=pv.segments
      ? ('结构 '+pv.segments+' 条 / '+pv.levels+' 层 · 切分单位 H'+pv.unit_level+' · 判定 '+esc(pv.kind_label||'未判定'))
      : (s.chars+' 字 · '+s.sections+' 节 · 尚未探查');
    const warn=(pv.warnings&&pv.warnings.length)
      ? '<div class="dim" style="color:var(--amber);padding:0 0 6px 0">'+esc(pv.warnings[0])+(pv.warnings.length>1?('　等 '+pv.warnings.length+' 条'):'')+'</div>'
      : '';
    return '<div><div class="src-row"><span class="nm">'+esc(s.name)+'</span>'+
      '<span class="dim">'+meta+'</span>'+
      '<button class="mini" onclick="delSource(\''+esc(pid)+'\',\''+esc(s.id)+'\')">移除</button></div>'+warn+'</div>';
  }).join(''):'<div class="empty">还没有成稿。上传文件或粘贴正文。</div>')+'</div>';
  h+=uploadBlock(pid,'src');
  modalHtml('成稿 · '+esc(projName(pid)),h,()=>{},'关闭',true);
}

/* 入库。两个面板共用：scope 决定读哪个上传区、回来后重画哪个面板。
   插入面板入库后把新素材就地勾上——上传就是为了插它，不该再要人找一遍。
   入库带一次模型探查（分钟级），与排图同款后台化：弹窗里看进度，跑完
   自动重画面板。 */
async function addSource(pid,scope){
  scope=scope||'src';
  let part=null;
  try{ part=await collectUpload(scope) }
  catch(e){ toast('读取文件失败：'+e.message,'err'); return }
  if(!part){toast('选文件或粘贴正文','err');return}
  const body={action:'add',project_id:pid};
  Object.keys(part).forEach(k=>{body[k]=part[k]});
  const r=await api('/api/source',body);
  if(!r.ok){toast(r.error,'err');return}
  taskModal('入库并探查 · '+esc(projName(pid)));
  watchTask(r.task_id, async j=>{
    const res=j.result||{};
    const pv=res.probe||{};
    const sid=((res.source||{}).id)||'';
    toast('已入库：'+((res.source||{}).name||'')
      +(pv.segments?(' · '+pv.segments+' 条 / '+pv.levels+' 层 · 判定 '+(pv.kind_label||'未判定')):''),'ok');
    if(scope==='ins') await openInsert(pid,sid);
    else await openSources(pid);
  }, async j=>{
    /* 探查失败不回滚入库：素材已经在库里，重画面板能看到「尚未探查」的那条，
       「重新探查」就是给这一刻留的。 */
    toast('已入库，但探查没走完：'+(j.error||''),'err');
    if(scope==='ins') await openInsert(pid); else await openSources(pid);
  });
}

/* 补跑探查：这一份素材进库时没探查过（旧版本入库），或人改了判定依据要重判。
   已探查过且有结果的走缓存，不会白烧一遍模型。 */
async function scanSources(pid){
  const r=await api('/api/source',{action:'scan',project_id:pid});
  if(!r.ok){toast(r.error,'err');return}
  taskModal('重新探查 · '+esc(projName(pid)));
  watchTask(r.task_id, async j=>{
    const res=j.result||{};
    toast('已探查 '+((res.sources||[]).length)+' 份素材'
      +(res.est_episodes?(' · 按体量约 '+res.est_episodes+' 期'):''),'ok');
    await openSources(pid);
  }, async j=>{
    toast('探查失败：'+(j.error||''),'err');
    await openSources(pid);
  });
}

async function delSource(pid,sid){
  const r=await api('/api/source',{action:'remove',project_id:pid,id:sid});
  if(!r.ok){toast(r.error,'err');return}
  toast('已移除','ok');
  await openSources(pid);
}

async function doPlanMap(pid){
  // 排图不再是「一次长调用」：结构直接读、逐节凝缩、再分组，交给模型的上下文
  // 一次比一次小。节数多时调用次数也多，这一等就是几十分钟——与脚本生成同款
  // 后台化：弹窗里看逐条日志与进度，跑完自动重开地图面板。
  const r=await api('/api/plan',{action:'map',project_id:pid});
  if(!r.ok){showPlanReport('排图未开始',[],[r.error||'未知原因']);return}
  taskModal('排图中 · '+esc(projName(pid)));
  watchTask(r.task_id, async j=>{
    const res=j.result||{};
    await loadProjects();
    closeModal();
    const n=(((res.project||{}).map||{}).episodes||[]).length;
    toast('地图已排出 '+n+' 期（'+(res.kind_label||'')+'，每期素材约 '+res.capacity+' 字 · 1:'+(res.ratio||5)+' 档）','ok');
    openMap(pid);
    // 告警不能只写进进度日志：模型排的期数与项目定的不符、单元没被分进任何一期、
    // 同系列被拆，这些都是「结果已落库但需要人核对」，不弹出来就等于没报。
    showPlanReport('排图结果',j.log,res.warnings);
  }, j=>{
    showPlanReport('排图未完成',j.log,[j.error||'未知原因']);
  });
}
function showPlanReport(title,logs,warns){
  const ws=(warns||[]).filter(Boolean);
  const ls=(logs||[]).slice(-14);
  if(!ws.length&&!ls.length) return;
  let h='';
  if(ws.length){
    h+='<p class="note">以下几条请核对后再出片：</p><ul style="margin:0 0 0 18px">'+
       ws.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ul>';
  }
  if(ls.length){
    h+='<p class="note" style="margin-top:10px">过程日志</p>'+
       '<div class="dim" style="font-size:12px;max-height:180px;overflow:auto;'+
       'white-space:pre-wrap">'+ls.map(l=>esc(l)).join('\n')+'</div>';
  }
  modalHtml(title,h,()=>{},'知道了',true);
}
async function loadParadigmOptions(){
  const sel=el('np-paradigm'); if(!sel) return;
  const r=await api('/api/plan',{action:'paradigms'});
  if(!r.ok) return;
  Object.keys(r.options||{}).forEach(k=>{
    if(k==='auto') return;
    const o=document.createElement('option');
    o.value=k; o.textContent=r.options[k].label; sel.appendChild(o);
  });
}
async function editParadigm(pid){
  const r=await api('/api/plan',{action:'paradigms',project_id:pid});
  if(!r.ok){toast(r.error,'err');return}
  let opt='<option value="">自适应（按结构推断）</option>';
  Object.keys(r.options||{}).forEach(k=>{
    if(k==='auto') return;
    opt+='<option value="'+esc(k)+'"'+(k===r.current?' selected':'')+'>'+
         esc(r.options[k].label)+'</option>';
  });
  let h='<p class="note">这两样都是排地图的组织依据，只影响分组（怎么切、怎么并），'+
        '不影响写脚本。与规划方式不同，它们不产生产物，改完自己决定要不要重排。</p>';
  h+='<div class="f"><label>素材类型</label><select id="para-sel" onchange="paraDesc()">'+
     opt+'</select><div class="desc" id="para-desc" style="margin-top:6px"></div></div>';
  h+='<div class="f"><label>重点方向 <span class="hint">可留空。填了与上面的组织依据一起进排图：'+
     '侧重的部分合得细、多占期数，次要的合得粗</span></label>'+
     '<textarea id="para-focus" rows="3" placeholder="如：多解析方法论，少讲技术细节与实现">'+
     esc(r.focus_note||'')+'</textarea></div>';
  h+='<div class="btn-row" style="margin-top:12px">'+
     '<button class="btn" onclick="saveParadigm(\''+esc(pid)+'\',false)">只保存（保留旧地图）</button>'+
     '<button class="btn primary" onclick="saveParadigm(\''+esc(pid)+'\',true)">保存并重排地图</button>'+
     '</div>';
  modalHtml('排图依据 · 可改',h,()=>{},'关闭',true);
  paraDesc();
}
function paraDesc(){
  const s=el('para-sel'), d=el('para-desc');
  if(!s||!d) return;
  const v=r=>r.options[r.selectedIndex];
  d.textContent = s.value
    ? ('排图时按「'+v(s).text+'」的组织依据，与探查推断无关')
    : '由探查按结构推断。拿不准就留这个';
}
async function saveParadigm(pid, redo){
  const sel=el('para-sel'); if(!sel) return;
  const foc=el('para-focus');
  const r=await api('/api/project',{action:'update',id:pid,paradigm:sel.value,
    focus_note:foc?foc.value.trim():''});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  closeModal();
  // 「只保存」是正当选择（旧地图不跟着变），但这两样都只在排图那一刻被读——
  // 不说清，人会以为存了就已经生效。
  toast(redo?'排图依据已更新，正在重排地图':'排图依据已保存（重排地图后才生效）','ok');
  if(redo) doPlanMap(pid);
}
async function openMap(pid){
  const r=await api('/api/project');
  const p=(r.projects||[]).find(x=>x.id===pid);
  if(!p){toast('项目不存在','err');return}
  const rows=(p.map&&p.map.episodes)||[];
  let h='<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'+
    '<button class="btn" onclick="doPlanMap(\''+esc(pid)+'\')">'+(rows.length?'重新排图':'排地图')+'</button>'+
    '<button class="btn" onclick="openInsert(\''+esc(pid)+'\')">插入素材</button>'+
    '<span style="flex:1"></span>'+
    '<span class="dim" style="font-size:12px">共 '+rows.length+' 期</span></div>';
  if(!rows.length){
    h+='<div class="empty">还没有地图。点「排地图」让模型按成稿的章节排出期数、每期要点与落点。</div>';
    return modalHtml('期数地图 · '+esc(p.name),h,()=>{},'关闭',true);
  }
  h+='<div class="tbl-scroll"><table class="map-tbl" id="map-tbl"><thead><tr>'+
     '<th style="width:56px">期号</th><th style="width:170px">标题</th>'+
     '<th style="width:150px">主旨</th>'+
     '<th>要点（每行一条）</th><th style="width:160px">素材落点</th>'+
     '<th style="width:36px"></th></tr></thead><tbody>';
  rows.forEach(e=>{
    h+='<tr data-no="'+esc(e.no)+'" data-refs="'+esc(JSON.stringify(e.refs||[]))+'"'+
       ' data-chars="'+(parseInt(e.chars||0,10)||0)+'"'+
       ' data-done="'+(e.done?'1':'0')+'"'+
       (isBranchNo(e.no)?' class="branch"':'')+'>'+
       '<td>'+esc(e.no)+'</td>'+
       '<td><input class="m-title" value="'+esc(e.title)+'"></td>'+
       '<td><input class="m-gist" value="'+esc(e.gist||'')+'"></td>'+
       '<td><textarea class="m-points" rows="2">'+esc((e.points||[]).join('\n'))+'</textarea></td>'+
       '<td class="refs">'+refsText(e.refs)+'</td>'+
       '<td><button class="mini" onclick="this.closest(\'tr\').remove()">✕</button></td></tr>';
  });
  h+='</tbody></table></div>';
  h+='<div style="margin-top:10px"><button class="mini" onclick="addMapRow()">+ 加一期</button></div>';
  modalHtml('期数地图 · '+esc(p.name),h,()=>saveMap(pid),'保存',true);
}

function addMapRow(){
  const tb=el('map-tbl').querySelector('tbody');
  const nums=Array.from(tb.querySelectorAll('tr')).map(tr=>{
    const m=/^(\d+)/.exec(tr.dataset.no||''); return m?parseInt(m[1],10):0;});
  const no=String((nums.length?Math.max.apply(null,nums):0)+1);
  const tr=document.createElement('tr');
  tr.dataset.no=no; tr.dataset.refs='[]';
  tr.innerHTML='<td>'+esc(no)+'</td>'+
    '<td><input class="m-title" value=""></td>'+
    '<td><input class="m-gist" value=""></td>'+
    '<td><textarea class="m-points" rows="2"></textarea></td>'+
    '<td class="refs"><span style="color:var(--red)">无落点</span></td>'+
    '<td><button class="mini" onclick="this.closest(\'tr\').remove()">✕</button></td>';
  tb.appendChild(tr);
}

async function saveMap(pid){
  const tbl=el('map-tbl'); if(!tbl) return;
  const eps=Array.from(tbl.querySelectorAll('tbody tr')).map(tr=>({
    no:tr.dataset.no,
    title:(tr.querySelector('.m-title').value||'').trim(),
    gist:(tr.querySelector('.m-gist').value||'').trim(),
    points:(tr.querySelector('.m-points').value||'').split('\n').map(s=>s.trim()).filter(Boolean),
    refs:JSON.parse(tr.dataset.refs||'[]'),
    // 体量与出片状态不在表单里可编辑，但必须原样回传：它们是排图时算出来、
    // 出片时更新的，界面这一趟如果不带回去，保存一次就等于把体检数据清零、
    // 把所有期重置成没出过片。
    chars:parseInt(tr.dataset.chars||'0',10)||0,
    done:tr.dataset.done==='1'
  }));
  if(!eps.length){toast('地图是空的，未保存','err');return}
  const r=await api('/api/plan',{action:'save',project_id:pid,episodes:eps});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  toast('地图已保存（'+eps.length+' 期）','ok');
}

/* 插入素材：勾要插的成稿，模型读完现有各期给插入点与拆分。
   面板自带入库区——插入的本意就是「把新材料加进来」。把上传留在另一个面板
   里，等于要人退出去传完再回来；而库里若只有那份已经排进地图的素材，
   这个面板能勾的便只剩它自己，插它等于把同一本书再插一遍。
   presetSid 是刚入库的那一份，重画后勾上并标出来。 */
async function openInsert(pid,presetSid){
  const keep=checkedInsIds();
  const rs=await api('/api/sources?project_id='+encodeURIComponent(pid));
  if(!rs.ok){toast(rs.error,'err');return}
  const rp=await api('/api/project');
  const p=(rp.projects||[]).find(x=>x.id===pid);
  const used=mapSourceIds(p);
  const list=rs.sources||[];
  let h='<div class="desc" style="margin-bottom:12px">勾选要插入的成稿；要插的那份还没入库，'
       +'就在下面直接传。模型会先逐节凝缩新素材（已凝缩过的节不重烧），再对照现有各期，'
       +'给出插入点与拆分——和画地图同一条管线，只是容器换成了锚点期。</div>';
  h+='<div id="ins-src">'+(list.length?list.map(s=>{
    const pv=s.probe||{};
    /* 摘要与「成稿」面板同一口径。同一份素材在两处各说一套数字，看的人
       得先猜哪套当真。 */
    const meta=pv.segments
      ? ('结构 '+pv.segments+' 条 / '+pv.levels+' 层 · 切分单位 H'+pv.unit_level+' · 判定 '+esc(pv.kind_label||'未判定'))
      : (s.chars+' 字 · '+s.sections+' 节 · 尚未探查');
    const inMap=used.has(s.id);
    const ck=(s.id===presetSid||keep.indexOf(s.id)>=0||!inMap)?' checked':'';
    return '<label class="src-row" style="cursor:pointer'+(inMap?' opacity:.7':'')+'">'+
      '<input type="checkbox" value="'+esc(s.id)+'"'+ck+'>'+
      '<span class="nm">'+esc(s.name)+'</span>'+
      '<span class="dim">'+meta+'</span>'+
      (inMap?'<span class="dim" style="color:var(--gold)">已在地图中</span>':'')+
      (s.id===presetSid?'<span class="dim ok">刚入库</span>':'')+
      '</label>';
  }).join(''):'<div class="empty">还没有成稿。在下面上传文件或粘贴正文，入库后即可插入。</div>')+'</div>';
  h+=uploadBlock(pid,'ins');
  modalHtml('插入素材 · 定插入点',h,()=>doInsertPlan(pid),'让模型定插入点',true);
}

async function doInsertPlan(pid){
  const ids=Array.from(document.querySelectorAll('#ins-src input:checked')).map(n=>n.value);
  if(!ids.length){toast('先勾选一份素材','err');return}
  // 判断插入点要先探查再调一次模型——与排图同款后台化，弹窗里看进度。
  const r=await api('/api/plan',{action:'insert_plan',project_id:pid,source_ids:ids});
  if(!r.ok){toast(r.error,'err');return}
  taskModal('定插入点 · '+esc(projName(pid)));
  watchTask(r.task_id, async j=>{
    closeModal();
    showInsertSuggestion(pid,(j.result||{}).suggestion||{});
  }, async j=>{
    toast('插入建议没生成：'+(j.error||''),'err');
    await openInsert(pid);
  });
}

function showInsertSuggestion(pid,sug){
  const eps=sug.episodes||[];
  if(!eps.length){toast('模型没有给出要插入的期','err');return}
  let h='<div class="desc" style="margin-bottom:10px">模型判断新素材插在第 <b style="color:var(--gold)">'+
    esc(sug.anchor_no)+'</b> 期之后，拆成 '+eps.length+' 期。插入点与内容都可以改。</div>';
  h+='<div class="f"><label>插在哪一期之后 <span class="hint">必须是现有期号；改后由程序按新锚点重编</span></label>'+
     '<input type="text" id="ins-anchor" value="'+esc(sug.anchor_no)+'"></div>';
  h+='<table class="map-tbl"><thead><tr><th style="width:56px">期号</th><th style="width:150px">标题</th>'+
     '<th>主旨</th><th>要点（每行一条）</th><th style="width:140px">素材落点</th></tr></thead><tbody id="ins-body">';
  eps.forEach(e=>{
    h+='<tr data-refs="'+esc(JSON.stringify(e.refs||[]))+'">'+
       '<td>'+esc(e.no)+'</td>'+
       '<td><input class="i-title" value="'+esc(e.title)+'"></td>'+
       '<td><input class="i-gist" value="'+esc(e.gist||'')+'"></td>'+
       '<td><textarea class="i-points" rows="2">'+esc((e.points||[]).join('\n'))+'</textarea></td>'+
       '<td class="refs">'+refsText(e.refs)+'</td></tr>';
  });
  h+='</tbody></table>';
  modalHtml('插入建议 · 确认后落库',h,()=>applyInsert(pid),'插入',true);
}

async function applyInsert(pid){
  const anchor=(el('ins-anchor').value||'').trim();
  if(!anchor){toast('插入点不能空','err');return}
  // 不在这边编期号：编了就要跟后端各写一套规则，换锚点后两边对不上。
  // 只把内容与锚点递上去，号由后端按锚点编，回什么就显示什么。
  const rows=Array.from(el('ins-body').querySelectorAll('tr')).map(tr=>({
    title:(tr.querySelector('.i-title').value||'').trim(),
    gist:(tr.querySelector('.i-gist').value||'').trim(),
    points:(tr.querySelector('.i-points').value||'').split('\n').map(s=>s.trim()).filter(Boolean),
    refs:JSON.parse(tr.dataset.refs||'[]')
  }));
  const r=await api('/api/plan',{action:'insert_apply',project_id:pid,anchor_no:anchor,episodes:rows});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  toast('已插入 '+rows.length+' 期：'+(r.episodes||[]).join('、'),'ok');
  openMap(pid);
}

/* 旧项目补选规划方式。它们当年没做过这个选择，所以要给一次机会；
   选完与立项时一样定死，因此提示里写明不可改。 */
async function setPlanMode(pid,mode){
  const label={mapped:'成稿规划',episodic:'逐期即兴',single:'单集'}[mode]||mode;
  const r=await api('/api/project',{action:'update',id:pid,plan_mode:mode});
  if(!r.ok){toast(r.error,'err');return}
  await loadProjects();
  toast('规划方式定为「'+label+'」，此后不可更改','ok');
  if(mode==='mapped') openSources(pid);
}
/* 节目名、副标题与受众是整档节目共用的身份，不是某一期的内容，所以挂在项目上：
   改一次，往后每一期都带上同一份。三者都可以留空——节目名留空即同项目名，
   副标题留空则画面上不印那一行，受众留空则片头里「面向…的听众」那一小句整个不出现。
   生效时机分两种，说清楚免得人改完看不见变化：画面（背景与封面）是出片时现取的，
   改完立刻生效；片头句是生成脚本时粘上去的，只有**之后生成的期**才用新值。 */
function editIdentity(pid){
  const p=(PROJECTS||[]).find(x=>x.id===pid)||{};
  const html=
    '<div class="dim" style="font-size:12px;margin-bottom:10px">这三样是整档节目共用的身份。'+
    '画面上的改动立刻生效；片头句要重生成那一期才会变。</div>'+
    '<div style="margin-bottom:10px"><div class="dim" style="font-size:12px;margin-bottom:4px">'+
    '节目名（留空即同项目名）</div>'+
    '<input type="text" id="ei-program" value="'+esc(p.program_name||'')+'"></div>'+
    '<div style="margin-bottom:10px"><div class="dim" style="font-size:12px;margin-bottom:4px">'+
    '副标题（留空则画面不印这一行）</div>'+
    '<input type="text" id="ei-subtitle" value="'+esc(p.subtitle||'')+'"></div>'+
    '<div><div class="dim" style="font-size:12px;margin-bottom:4px">'+
    '受众（片头「面向…的听众」里的那一句；留空则那一小句整个不出现。改完要重出那一期才会变）</div>'+
    '<input type="text" id="ei-audience" value="'+esc(p.audience||'')+'"></div>';
  modalHtml('节目名 / 副标题 / 受众',html,async()=>{
    const program=el('ei-program').value.trim(), subtitle=el('ei-subtitle').value.trim(),
          audience=el('ei-audience').value.trim();
    const r=await api('/api/project',{action:'update',id:pid,
                                      program_name:program,subtitle:subtitle,
                                      audience:audience});
    if(!r.ok){toast(r.error||'保存失败','err');return}
    toast('节目名 / 副标题 / 受众已更新','ok'); loadProjects();
  },'保存');
}
/* ------------------------------------------------------------------ 角色音色
   本地引擎走 Base 变体：音色不在模型里，而在项目自己那份参考音频里。这个面板
   把「音色是什么、在哪、和谁一样」摆出来 —— 否则它只是磁盘上一个没人知道的
   文件，表现成「这期声音怎么变了」时无从查起。

   两件事面板不做：不替你挑音色、不替你决定要不要一致。前者在配置页的
   「A 角音色」选（那是录参考音频用的内置音色），后者是「继承」这个动作本身。 */
let VC_PROJ='';
function voiceRow(role,st,voice){
  const r=((st||{}).roles||{})[role]||{};
  const head='<div class="vc-role">'+role+' 角</div>';
  if(!r.ok){
    return '<div class="vc-row">'+head+'<div class="vc-body">'+
      '<div>还没有音色档案</div>'+
      '<div class="dim" style="font-size:11px">首次合成时会自动录一份（用 '+
      esc(voice||'默认音色')+'）。</div></div></div>';
  }
  return '<div class="vc-row">'+head+'<div class="vc-body">'+
    '<div>'+esc(r.builtin_voice||'未知音色')+' · '+Number(r.seconds||0).toFixed(2)+
    ' 秒 · 来源 '+esc(r.source||'')+
    (r.inherited_from?'（继承自 '+esc(r.inherited_from)+'）':'')+'</div>'+
    '<div class="dim" style="font-size:11px">指纹 '+esc(r.content_sha||'')+
    (r.matches_record?'':' · 与记录不符')+'</div>'+
    '<div class="dim" style="font-size:11px">参考文案 '+esc(r.text||'')+'</div>'+
    '</div></div>';
}
async function refreshVoices(){
  const box=el('vc-body'); if(!box) return;
  const r=await api('/api/voice/profile',{project:VC_PROJ});
  if(!r.ok){box.innerHTML='<div class="dim">'+esc(r.error||'读取失败')+'</div>';return}
  if(!r.local){
    box.innerHTML='<div class="dim">当前语音引擎不是 Qwen3-TTS，音色由所选引擎'+
      '自己决定，不需要参考音频。</div>';
    return;
  }
  const st=r.status||{}, vs=r.voices||{};
  const others=(PROJECTS||[]).filter(p=>p.id!==VC_PROJ&&!p.archived);
  box.innerHTML=
    '<div class="dim" style="font-size:12px;margin-bottom:10px">'+
    '整期所有句子都由这两段参考音频决定音色。它随项目保存，换项目互不影响；'+
    '要与别的项目一致，用下面的「继承」把它复制过来 —— 复制的是文件，'+
    '波形逐字节相同，不靠重新生成碰运气。</div>'+
    voiceRow('A',st,vs.A)+voiceRow('B',st,vs.B)+
    '<div class="dim" style="font-size:12px;margin:12px 0 4px">要与某个项目一致，'+
    '就从它那里继承：</div>'+
    '<select id="vc-src"><option value="">（选一个项目）</option>'+
    others.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+'</option>').join('')+
    '</select>'+
    '<div style="margin-top:12px">'+
    '<button class="mini" onclick="voiceBuild(\'both\')">重录 A + B</button> '+
    '<button class="mini" onclick="voiceBuild(\'A\')">只重录 A</button> '+
    '<button class="mini" onclick="voiceBuild(\'B\')">只重录 B</button> '+
    '<button class="mini" onclick="voiceInherit()">从选中项目继承</button>'+
    '</div>';
}
function openVoices(pid){
  VC_PROJ=pid;
  const p=(PROJECTS||[]).find(x=>x.id===pid)||{};
  modalHtml('角色音色 · '+esc(p.name),
            '<div id="vc-body" class="dim" style="font-size:12px">读取中…</div>',
            ()=>{},'关闭',true);
  refreshVoices();
}
async function voiceBuild(role){
  const r=await api('/api/voice/build',{project:VC_PROJ,role:role,force:true});
  if(!r.ok){toast(r.error||'起不来','err');return}
  taskModal('录制角色音色');
  watchTask(r.task_id,
    ()=>{toast('音色档案已就绪','ok');openVoices(VC_PROJ)},
    j=>toast('录制失败：'+(j.error||''),'err'));
}
async function voiceInherit(){
  const src=((el('vc-src')||{}).value||'');
  if(!src){toast('先选一个要继承的项目','err');return}
  const r=await api('/api/voice/build',{project:VC_PROJ,from_project:src,force:true});
  if(!r.ok){toast(r.error||'起不来','err');return}
  taskModal('继承角色音色');
  watchTask(r.task_id,
    ()=>{toast('已继承，音色与该项目逐字节相同','ok');openVoices(VC_PROJ)},
    j=>toast('继承失败：'+(j.error||''),'err'));
}
function editNext(pid,cur){
  modalInput('改下一期号','下一个出片的期号。取消即不改动。',cur,async v=>{
    if(v==null||String(v).trim()===''){toast('未改动');return}
    const r=await api('/api/project',{action:'update',id:pid,next_episode:String(v).trim()});
    if(!r.ok){toast(r.error,'err');return}
    toast('下一期号已改为 '+String(v).trim(),'ok'); loadProjects();
  },'保存');
}
async function setArchived(pid,on){
  const r=await api('/api/project',{action:'update',id:pid,archived:on});
  if(!r.ok){toast(r.error,'err');return}
  toast(on?'已归档（产物保留）':'已恢复','ok'); loadProjects();
}
/* 删除要点两下。第一下只把按钮点亮，第二下才真删——删掉的是整个项目目录
   （素材、地图、脚本、各期成品），没有回收站。点亮后 6 秒没下文自动熄灭，
   免得「点过一次」的按钮一直等着被误触。 */
let ARM_DEL='', ARM_TIMER=null;
function fmtBytes(n){
  n=Number(n||0);
  if(n<1024) return n+' B';
  if(n<1048576) return (n/1024).toFixed(0)+' KB';
  if(n<1073741824) return (n/1048576).toFixed(1)+' MB';
  return (n/1073741824).toFixed(2)+' GB';
}
function delProject(pid){
  if(ARM_DEL!==pid){
    ARM_DEL=pid; renderProjects();
    clearTimeout(ARM_TIMER);
    ARM_TIMER=setTimeout(()=>{ if(ARM_DEL===pid){ ARM_DEL=''; renderProjects(); } },6000);
    return;
  }
  clearTimeout(ARM_TIMER); ARM_DEL='';
  doDeleteProject(pid);
}
async function doDeleteProject(pid){
  const r=await api('/api/project',{action:'delete',id:pid});
  if(!r.ok){toast(r.error||'删除失败','err'); loadProjects(); return}
  if(ACTIVE_PROJ===pid){ ACTIVE_PROJ=''; localStorage.removeItem('pm_proj'); }
  toast('已删除「'+(r.name||pid)+'」'+
        (r.dir_removed?('，项目目录一并删掉，释放 '+fmtBytes(r.freed_bytes))
                      :'（磁盘上本就没有这个项目目录）'),'ok');
  loadProjects();
}

/* ---- 后端 ---- */
async function testBackend(){
  const r=await api('/api/backend/test',{});
  const dot=el('backend-dot'), txt=el('backend-txt');
  dot.className='dot '+(r.ok?'on':'off');
  txt.textContent=r.ok?'后端可用':'后端不可用';
  toast(r.message,r.ok?'ok':'err');
}
async function testBackendSilent(){
  const r=await api('/api/backend/test',{});
  el('backend-dot').className='dot '+(r.ok?'on':'off');
}

window.onload=async()=>{
  bindNav(); bindFile(); bindMode(); loadParadigmOptions();
  await loadConfig();
  await loadProjects();          // 脚本页与合成页的项目下拉都靠这份数据
  loadVoices();
  loadModels();
  api('/api/backends').then(r=>{if(r.ok&&r.backend)el('backend-txt').textContent=r.backend+' · '+(r.model||'未设模型')});
  testBackendSilent();
  recalc();
  showTab(hashTab(),true);       // 首屏按 hash 落地，没有 hash 就是项目页
};
</script>
</body>
</html>
"""

# 脚本页标签下拉的单源注入：选项＝**语篇词表**（`DISCOURSE_ORDER`，也是生成侧枚举
# 与门禁判据读的那唯一一份）＋ **程序粘合句专用标签**（`PROGRAM_ONLY_TAGS`）。
#
# 词表那一份：没有任何按风格收窄的第二步——收窄过两次，两次都导致「界面能选、门禁
# 打回」。词表改了这里自动跟，不存在两处各一份的账。
# 程序标签那一份（v0.36.0 加）：盘上的成品稿里有「开场／回顾／收束」，下拉里没有的
# 话，那一格就没有任何选项能被选中，浏览器会退回显示第一项——**数据是对的，界面
# 是错的**。两个元组都取自 config_manager，页面不留字面量。
PAGE = PAGE.replace('"__DISCOURSE_VOCAB__"',
                    json.dumps(list(DISCOURSE_ORDER) + list(PROGRAM_ONLY_TAGS),
                               ensure_ascii=False))

