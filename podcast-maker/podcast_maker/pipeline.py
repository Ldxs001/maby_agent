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

"""管线编排：素材 → 脚本 → 声音 → 字幕 → 画面 → 产物 + 校验报告。

- 每个阶段都有门禁，门禁不过即写锁文件硬中断（08a），不产出次品
- 产物校验覆盖音画时长差、实测码率/采样率、断词率、字体、水印
- 续跑幂等：已存在的产物默认跳过，便于 --continue 恢复
"""

import json
import os
import re
import threading
import time
import traceback
from datetime import datetime

from . import (aigc_label, assets_factory, audio_engine, duration_model, layout,
               paradigms, project_store)
from . import script_engine
from . import probe, source_store, subtitle_engine, tts_engine, video_engine
from .config_manager import GATE_BY_KEY, VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PipelineError(RuntimeError):
    pass


# ------------------------------------------------------------------ 任务
JOBS = {}
_JOBS_LOCK = threading.Lock()


def new_job(kind, title="", project=""):
    jid = time.strftime("%Y%m%d%H%M%S") + "-%04d" % (len(JOBS) % 10000)
    job = {
        "id": jid, "kind": kind, "title": title, "status": "running",
        # 归属项目：同项目的模型长活互斥（探查在写凝缩结果时，排图也在写
        # 同一批 <sid>.probe.json——并行跑会互相踩）。全局任务不填。
        "project": project,
        "stage": "排队", "progress": 0.0, "log": [], "result": None,
        "error": "", "started": datetime.now().isoformat(timespec="seconds"),
        "finished": "",
        # 中止标记：批任务每一步开头看它一眼。不硬杀线程——硬杀会把正写着的
        # 文件截断，半截的清单比没有更坏，它看着像做完了。
        "stop": False,
        # 批任务的第几期 / 共几期。单期任务没有这一项。
        "batch": None,
    }
    with _JOBS_LOCK:
        JOBS[jid] = job
    return job


def job_log(job, msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    job["log"].append(line)
    if len(job["log"]) > 400:
        del job["log"][:100]


def get_job(jid):
    return JOBS.get(jid)


def list_jobs(limit=30):
    with _JOBS_LOCK:
        items = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)
    return [dict(j, log=j["log"][-80:]) for j in items[:limit]]


def run_async(kind, fn, title="", project=""):
    job = new_job(kind, title, project=project)

    def _worker():
        try:
            job["result"] = fn(job)
            job["status"] = "done"
            job["stage"] = "完成"
            job["progress"] = 1.0
        except Exception as e:
            job["status"] = "failed"
            job["stage"] = "失败"
            job["error"] = str(e)
            job_log(job, "失败：" + str(e))
            job_log(job, traceback.format_exc()[-1200:])
        finally:
            job["finished"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_worker, daemon=True).start()
    return job["id"]


def running_for(pid, kinds=None):
    """某项目当前运行中的任务（用于同项目长活互斥）。

    同一项目的探查与排图都在写同一批 `<sid>.probe.json`——并行跑会互相踩
    （一边刚落盘凝缩结果、另一边按旧结构重写同一份文件）。起任务前查一把，
    撞了就拒绝，让等着的那个重试。`kinds` 缺省查全部类型。
    """
    with _JOBS_LOCK:
        rows = [j for j in JOBS.values()
                if j["status"] == "running" and j.get("project") == pid
                and (not kinds or j["kind"] in kinds)]
    return [dict(j, log=[]) for j in rows]


# ------------------------------------------------------------------ 目录
def _slug(text, maxlen=24):
    s = re.sub(r"[^0-9A-Za-z]+", "-", (text or "")).strip("-").lower()
    return s[:maxlen]


def new_project_dir(base, title):
    """新建集目录。已存在即报错，不用序号猜测（原项目的静默覆盖根因）。"""
    os.makedirs(base, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    slug = _slug(title)
    name = "%s_%s" % (ts, slug) if slug else ts
    path = os.path.join(base, name)
    if os.path.exists(path):
        raise PipelineError("目录已存在，请稍后重试：%s" % path)
    os.makedirs(path)
    return path


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def list_projects(base):
    """列出已产出的集。

    一期一份清单，放在所属那棵树的「报告」目录下——项目目录放项目的各期，
    单集目录放它唯一那一期。所以这里扫两层：树根、然后它下面的「报告」。
    `root` 是树根名（项目 id 或单集目录名），`no` 是期号，两者合起来定位一期。
    """
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base), reverse=True):
        root = os.path.join(base, name)
        if not os.path.isdir(root):
            continue
        rdir = layout.report_dir(root)
        if not os.path.isdir(rdir):
            continue
        for fn in sorted(os.listdir(rdir), reverse=True):
            if not fn.endswith(".manifest.json"):
                continue
            mf = read_json(os.path.join(rdir, fn))
            if not mf:
                continue
            out.append({"root": name, "dir": name,
                        "no": mf.get("episode_no", ""),
                        "path": os.path.join(rdir, fn),
                        "title": mf.get("title", ""),
                        "episode_no": mf.get("episode_no", ""),
                        "created": mf.get("created", ""),
                        "report": mf.get("report", {}),
                        "assets": mf.get("assets", {})})
    return out


# ------------------------------------------------------------------ 校验报告
def _gate(key, ok, detail):
    spec = GATE_BY_KEY.get(key, {"label": key, "level": "warn"})
    return {"key": key, "label": spec["label"], "level": spec["level"],
            "judge": spec["judge"], "ok": bool(ok), "detail": detail}


def build_report(ep, cfg):
    """产物校验报告。每一项都取自实测，不做估计。

    只报 `GATE_SPEC.stage == "render"` 的项。脚本阶段的门禁（stage == "generate"，
    含内容检）不并进来——那是脚本阶段的账，判完落在过程目录里（见
    `script_engine.gate_report_path`）。两个阶段的结论混进同一份报告，等于同一件
    事两个阶段都管：脚本的稿子被拒过，合成端还要再拒一次，人连出片的机会都没有。

    `ep` 是本期的成品路径表（见 `layout.episode_files`）。取路径表而不是取一个
    目录，是因为一期的东西不再堆在同一个目录里了——音频在「音视频」、字幕在
    「字幕」，判据要的是「这一期该产出的那些文件在不在」，路径表正是这个。

    校验的是「这一期被要求产出什么」，不是「永远要求全套」。关掉视频开关之后
    仍去要视频，每一次出片都判不过——片子过不了就不占期号，项目进度永远停在
    原地，而磁盘上每份产物看着都好好的，没人会想到是门禁在要一个没被要求的东西。
    不适用的项直接不进报告，比留一行「不适用」占位更诚实。
    """
    items = []
    manifest = read_json(ep.get("manifest") or "", {})
    assets = manifest.get("assets", {})
    want_video = bool(manifest.get("do_video", True))
    want_vertical = want_video and bool(cfg.get("video.produce_vertical", True))

    # 产物完整性
    need = []
    for k in ("audio", "article"):
        p = assets.get(k)
        if not p or not os.path.exists(p):
            need.append(k)
    for k, wanted in (("video", want_video), ("video_vertical", want_vertical)):
        if not wanted:
            continue
        p = assets.get(k)
        if not p or not os.path.exists(p):
            need.append(k)
    items.append(_gate("assets_complete", not need,
                       "缺失：%s" % "、".join(need) if need else "全部齐备"))

    # 音画同步：没有视频就没有音画这回事
    if want_video:
        vpath = assets.get("video")
        if vpath and os.path.exists(vpath):
            sync = video_engine.verify_av_sync(vpath)
            items.append(_gate("av_sync", sync["ok"], sync["detail"]))
        else:
            items.append(_gate("av_sync", False, "视频文件不存在"))

    # 音频参数
    apath = assets.get("audio")
    if apath and os.path.exists(apath):
        try:
            info = audio_engine.probe_audio(apath)
            sr_ok = info.get("sample_rate") == int(cfg.get("audio.sample_rate", 44100))
            items.append(_gate("rate_match", sr_ok,
                               "实测 %d Hz，配置 %d Hz" % (info.get("sample_rate", 0),
                                                        int(cfg.get("audio.sample_rate", 44100)))))
            want = int(cfg.get("audio.bitrate_kbps", 192))
            got = info.get("bit_rate_kbps", 0)
            items.append(_gate("bitrate_match", abs(got - want) <= max(8, want * 0.05),
                               "实测 %d kbps，配置 %d kbps" % (got, want)))
        except audio_engine.AudioError as e:
            items.append(_gate("rate_match", False, str(e)))

    # 断词率
    wrap = manifest.get("wrap_stats") or {}
    items.append(_gate("wrap_ok", int(wrap.get("violations", 0)) == 0,
                       "折行 %d 处，违规 %d 处，断词率 %.2f%%"
                       % (wrap.get("wraps", 0), wrap.get("violations", 0),
                          (wrap.get("rate", 0) or 0) * 100)))

    # 字体
    sub = manifest.get("subtitle_font") or {}
    items.append(_gate("font_ok", bool(sub.get("family")),
                       "使用字体：%s（%s）" % (sub.get("family", "未记录"),
                                            sub.get("source", "?"))))

    # 水印
    bg = assets.get("bg")
    if bg and os.path.exists(bg):
        try:
            wm = assets_factory.check_watermark(bg)
            items.append(_gate("watermark_ok", wm["ok"],
                               "水印像素 %d（阈值 %d）" % (wm["pixels"], wm["threshold"])))
        except Exception as e:
            items.append(_gate("watermark_ok", False, "水印检测失败：%s" % e))

    # 封面
    cov = assets.get("covers") or {}
    items.append(_gate("cover_sizes", len(cov) >= 2,
                       "已生成 %d 个尺寸：%s" % (len(cov), "、".join(cov.keys()))))

    # advisory 项（连续性/语义/推理）依赖模型判断，失败时交人工复核，
    # 不参与放行判定。把模型的不确定当硬事实会误杀正常内容。
    hard = [i for i in items if not i.get("advisory")]
    pending = [i for i in items if i.get("advisory") and not i["ok"]]
    strict = bool(cfg.get("script.gate_strict", True))
    fails = [i for i in hard if not i["ok"] and i["level"] == "fail"]
    warns = [i for i in hard if not i["ok"] and i["level"] == "warn"]
    return {"items": items, "fails": fails, "warns": warns, "pending": pending,
            "passed": not fails and not (strict and warns),
            "strict": strict, "time": datetime.now().isoformat(timespec="seconds")}


# ------------------------------------------------------------------ 料源
def resolve_material(base, proj_item, episode_no, material, log=None):
    """定本期料源，返回 (素材文本, 本期地图落点或 None)。

    两条路的料源不同，按项目范式在这里一次定死；脚本生成与出片共用这一个函数，
    各写一份的话迟早有一处先漂。

    - 成稿规划（mapped）：料源是地图落点。调用方递来的素材一律不作数——地图是
      本期"讲什么"的唯一来源，允许手填覆盖就等于造出第二个真值来源。没排图的
      项目在选项目那一步就挡住了，走到这儿仍然没地图，是绕开界面直调接口。
    - 逐期即兴（episodic）、单集（single）与未定模式（legacy）：料源由调用方给，
      这一层不管。这三者都不排图，走到这儿天然取不到地图落点。
    """
    log = log or (lambda m: None)
    if proj_item is None:
        return material, None
    episode_no = str(episode_no or "").strip()
    episodes = project_store.map_episodes(proj_item)
    plan_row = None
    for e in episodes:
        if str(e.get("no") or "").strip() == episode_no:
            plan_row = e
            break
    if (proj_item.get("plan_mode") or project_store.MODE_LEGACY) != "mapped":
        return material, plan_row
    name = proj_item.get("name") or proj_item.get("id") or "（未命名）"
    if not episodes:
        raise PipelineError("项目「%s」是成稿规划，还没有排出期数地图，"
                            "取不到本期素材。先到「成稿」排图。" % name)
    if plan_row is None:
        raise PipelineError("第 %s 期不在地图里：地图上没有这一期。" % episode_no)
    refs = plan_row.get("refs") or []
    if not refs:
        raise PipelineError("第 %s 期在地图里没有落点，取不到本期素材。" % episode_no)
    if (material or "").strip():
        log("成稿规划：料源由地图落点决定，已忽略调用方递来的素材 %d 字"
            % len(material))
    text, mmeta = source_store.compose(base, proj_item.get("id") or "", refs)
    log("按地图落点取素材 %d 字（%d 节，未截断）" % (mmeta["chars"], len(refs)))
    return text, plan_row


def evidence_pack(base, proj_item, plan_row, log=None):
    """本期判据包：本期讲什么（主旨/要点）+ 各节凝缩。取不到就给空包，不报错。

    这两样都是排图那一步定下的，也正是写脚本时已经喂过的依据。它们有两处用处：
    内容检判「方向」（本期该讲的讲到没有、片头开的口子收了没有）拿它当判据，
    比拿几万字原文更直接；素材超出输入预算时也拿它顶替原文（见
    `script_engine.fit_material`）——所以必须能单独取出来，而不是藏在素材里。

    取不到不算错误：逐期即兴的项目没排过图，本来就没有凝缩；凝缩版本对不上时
    `probe.load` 会返回 None（旧口径的骨架不能用），这里也只是少一段判据。少
    哪一段要在日志里说清，不能让「判据缺了一块」变成没人知道的事。
    """
    log = log or (lambda m: None)
    row = plan_row or {}
    pack = {"gist": str(row.get("gist") or "").strip(),
            "points": [str(p).strip() for p in (row.get("points") or [])
                       if str(p).strip()],
            "sections": []}
    if proj_item is None or not row:
        return pack
    pid = proj_item.get("id") or ""
    refs = row.get("refs") or []
    seen, missed = set(), []
    for rf in refs:
        sid = str(rf.get("source") or "").strip()
        anchor = str(rf.get("anchor") or "").strip()
        if not sid:
            continue
        try:
            ir = probe.load(base, pid, sid)
        except probe.ProbeError as e:
            log("判据包：第 %s 节凝缩读不出来（%s）" % (anchor or sid, e))
            continue
        if ir is None:
            missed.append(anchor or sid)
            continue
        for s in (ir.get("segments") or []):
            title = str(s.get("title") or "").strip()
            # 同名标题靠行号区分——只按标题找会取到另一个同名小节，给出一段
            # 对不上号的「判据」，比没有更坏。
            if anchor and title != anchor:
                continue
            if (anchor and rf.get("line") and s.get("line")
                    and int(rf["line"]) != int(s["line"])):
                continue
            gist = str(s.get("gist") or "").strip()
            if not gist:
                continue
            key = (sid, title, s.get("line"))
            if key in seen:
                continue
            seen.add(key)
            pack["sections"].append({
                "source": sid, "anchor": title, "gist": gist,
                "points": [str(p).strip() for p in (s.get("points") or [])
                           if str(p).strip()]})
    if missed:
        log("判据包：%d 节的凝缩取不到（%s），本期判据只剩主旨与要点"
            % (len(missed), "、".join(missed[:4])))
    return pack


def build_lines_text(cfg, title, episode_no=""):
    """画面上要出现的字，一次备齐，背景与封面共用这一份。

    谁是谁、从哪来，在这一处看得全：节目身份（品牌行 / 标语 / 版权行 /
    节目名 / 副标题）来自项目与配置，本期信息（期标题 / 期号）来自地图与
    项目进度。出片与「重生成资源」两处各拼一份的话，同一个封面在两处会长
    出不同的字来。

    画面上的层级也在这里定：`program`（节目名）是主标题，最大；`subtitle`
    次之；`title`（期标题）再次。期标题字数不定，长起来只能折行，不占最大档。
    """
    return {
        "brand": cfg.get("project.brand", ""),
        "kind": "PODCAST",
        "tagline": cfg.get("project.tagline", ""),
        "title": title,
        "subtitle": cfg.get("project.subtitle", ""),
        "attribution": cfg.get("project.attribution", ""),
        "program": cfg.get("project.program_name", ""),
        "episode_no": episode_no,
        "bg_preset": cfg.get("background.preset", "ink"),
    }


def _ai_declaration(work, cfg, root, log):
    """片头 AI 语音声明（显式标识）的来源解析，唯一入口。

    自定义音频（`audio.ai_disclosure_path`）优先，填了就必须存在——指了路却
    不存在，说明配置坏了，报错比悄悄用合成音顶上更诚实。未填则用 A 角音色
    按声明文案现场合成：就是让"说话的这个声音"自己报身份，认知上最直接。
    """
    custom = cfg.get("audio.ai_disclosure_path") or ""
    if custom:
        if not os.path.exists(custom):
            raise PipelineError("AI 声明音频不存在：%s" % custom)
        return custom
    text = str(cfg.get("audio.ai_disclosure_text") or aigc_label.DISCLOSURE_TEXT)
    # 输出目录必须与台词音频（work/audio）隔离：synthesize 按台词的
    # %04d 命名空间写文件，共用目录时声明会以 0000_A.wav 的身份覆盖掉
    # 正文第一句——声明响两遍、欢迎收听消失，且续跑逻辑见文件已存在
    # 直接跳过，错位永久化。
    decl_dir = os.path.join(work, "decl")
    res = tts_engine.synthesize([{"speaker": "A", "text": text}], decl_dir, cfg,
                                log=log, voice_root=root)
    if not res.get("files"):
        raise PipelineError("AI 声明音频合成失败，本期停产。"
                            "可改用自定义声明音频（audio.ai_disclosure_path）绕开合成。")
    return res["files"][0]


# ------------------------------------------------------------------ 主流程
def run_episode(cfg, calib, material, title, episode_no="", project_dir=None,
                script=None, preset_key=None, extra="", llm=None,
                do_video=True, reuse=True, job=None, project_id="",
                ack_script_issues=False):
    """跑完整条管线。返回 result dict。

    本地语音引擎的起停包在这里：服务没在线就先拉起来，整期跑完按引用计数收工。
    放在最外层而不是语音那一步，是因为「服务起不来」必须在烧掉一期封面之前就
    知道——等几十句语音各自失败一遍才报，白搭一整轮。edge 引擎不碰服务。
    """
    log = (lambda m: job_log(job, m)) if job else (lambda m: None)
    local = cfg.get("tts.engine", "edge") in tts_engine.LOCAL_ENGINES
    if local:
        tts_engine.acquire_service(cfg, log)
    try:
        return _run_episode(cfg, calib, material, title,
                            episode_no=episode_no, project_dir=project_dir,
                            script=script, preset_key=preset_key, extra=extra,
                            llm=llm, do_video=do_video, reuse=reuse, job=job,
                            project_id=project_id,
                            ack_script_issues=ack_script_issues)
    finally:
        if local:
            tts_engine.release_service(log)


def _run_episode(cfg, calib, material, title, episode_no="", project_dir=None,
                 script=None, preset_key=None, extra="", llm=None,
                 do_video=True, reuse=True, job=None, project_id="",
                 ack_script_issues=False):
    """跑完整条管线。返回 result dict。

    project_id 非空即挂到项目下：期号缺省取自项目进度，风格与音色以项目为准，
    出片后把这一期记进项目并前进期号。不传就是单集模式，行为与从前一致。

    **这一段只管合成**：取稿、合成、出产物、验产物。脚本怎么写、过没过门禁，
    是脚本阶段的事（见 `script_engine.generate`）——这里不写稿，也不判稿。

    material / llm / preset_key / extra 是脚本阶段的调用口径，合成端用不到，
    留着只为调用方兼容；不要在这里拿它们做判断。

    ack_script_issues：界面上点过「仍然合成」，即已知脚本阶段留有未通过项。
    只作留痕，不改变行为——合成照跑。

    job 传入时用于汇报进度与日志。
    """
    log = lambda m: job_log(job, m) if job else None
    step = (lambda n, t, p: _step(job, n, t, p))

    base = os.path.join(ROOT, cfg.get("project.output_dir", "projects"))

    proj_item = None
    if project_id:
        proj_item = project_store.find(base, project_id)
        if proj_item is None:
            raise PipelineError("项目不存在：%s" % project_id)
        if not str(episode_no or "").strip():
            episode_no = proj_item.get("next_episode", "1")
        # 项目层面的设定盖过全局配置，但不回写全局。生成脚本与出片走的是
        # 同一个覆盖函数，否则两阶段拿到的节目名不同，写死的片头句会对不上。
        cfg = project_store.apply_to_config(cfg, proj_item)
        log("项目「%s」第 %s 期%s" % (
            proj_item.get("name", project_id), episode_no,
            "（计划共 %s 期）" % proj_item["planned_episodes"]
            if proj_item.get("planned_episodes") else "（总期数未定）"))

        # 期标题取自地图。这一期讲什么，排图的时候已经定过，标题就是它的名字；
        # 让人在合成这一步再填一遍，等于给同一个东西造第二个真值来源——填了
        # 也不该生效（地图才是准的），不填又会被当成「未命名」写到封面上。
        map_title = ""
        for e in project_store.map_episodes(proj_item):
            if str(e.get("no") or "").strip() == str(episode_no).strip():
                map_title = str(e.get("title") or "").strip()
                break
        if map_title:
            title = map_title

        # 料源不在这里取。取料是脚本阶段的事（`resolve_material` 由脚本页调用）；
        # 合成端不再生成脚本，也就没有理由每出一期片都去拼一遍素材文本——
        # 拼出来也没人用，白读一遍地图和素材。

    if project_dir:
        root = (project_dir if os.path.isabs(project_dir)
                else os.path.join(ROOT, project_dir))
    elif project_id:
        # 产物落进项目自己那棵树：素材、地图、脚本、成品同处一处，
        # 「这个项目的全部材料」就是一整个文件夹。
        root = layout.project_dir(base, project_id)
    else:
        # 单集：不归属任何项目，但它也有一棵树，产物照样分门别类。
        root = new_project_dir(base, title or ("第%s期" % episode_no if episode_no else ""))
    layout.ensure_dirs(root)
    ep = layout.episode_files(root, episode_no)
    work = layout.tmp_dir(root, episode_no)
    os.makedirs(work, exist_ok=True)

    result = {"project_dir": root, "title": title, "episode_no": episode_no}
    # ---- 1. 脚本 ----
    # 合成不管脚本怎么写、过没过门禁。脚本是脚本阶段的产物：在那边改得动、判得准，
    # 也有回灌重写的余地；到了这里稿子已经是定稿，合成只负责把它变成声音和画面。
    # 所以这一段只做一件事——取稿。取不到就报错，不顺手现写一份：现场生成等于
    # 把审过的稿换成模型新写的另一份，而且合成端根本没有判它的资格。
    # 脚本排在资源之前：背景与封面上的主视觉就是本期标题，而标题由脚本产出。
    step(1, "脚本", 0.05)
    script_path = ep["script"]
    if script is None and reuse and os.path.exists(script_path):
        script = read_json(script_path, [])
        if script:
            log("复用已有脚本（%d 句）" % len(script))
    if not script:
        raise PipelineError("本期还没有脚本，合成无从下手。先到「脚本」页生成，"
                            "或把改好的稿子存回本期。")

    # 脚本阶段留下的未通过项在这里只提醒、不拦人。稿子已经在手上，合成该做的是
    # 出片；拿脚本的账把合成挡在门外，就是同一件事两个阶段都管——脚本判过一遍，
    # 合成再判一遍，人连出片的机会都没有。界面上另有一次确认（见 api_script_issues）。
    issues = script_engine.read_gate_report(work) or {}
    if not issues.get("passed", True):
        bad = [i for i in (issues.get("items") or []) if not i["ok"]]
        log("脚本阶段留有 %d 项未通过（%s）——照常合成，只作提醒"
            % (len(bad), "、".join(i["label"] for i in bad)))
    if ack_script_issues:
        # 人在界面上点过「仍然合成」。留个凭证：这一期是在知情下出的片。
        result["script_issues_ack"] = True
        log("已确认：带着脚本阶段的未通过项继续合成")

    if title:
        log("本期标题：%s" % title)
    plan = (proj_item or {}).get("planned_episodes")
    result["planned_episodes"] = plan
    if plan:
        log("项目计划：共 %s 期（人为指定）" % plan)

    write_json(script_path, script)
    est = duration_model.explain(script, cfg)
    result["estimate"] = est
    log("脚本 %d 句 / %d 字 / 预估 %.1f 秒（目标 %.1f 秒，偏差 %+.1f%%）"
        % (est["line_count"], est["total_chars"], est["total_seconds"],
           est["target_seconds"], est["deviation_pct"]))

    # ---- 2. 背景 / 封面 / 音乐 ----
    step(2, "生成背景 / 封面 / 音乐", 0.15)
    aigc = aigc_label.labeled(cfg)
    lines_text = build_lines_text(cfg, title, episode_no)
    if reuse and os.path.exists(ep["bg_h"]):
        log("资源已存在，跳过生成")
        built = {"bg_h": ep["bg_h"], "bg_v": ep["bg_v"],
                 "bgm": ep["bgm"] if os.path.exists(ep["bgm"]) else None,
                 "covers": {k: v for k, v in ep["cover"].items() if os.path.exists(v)}}
        if aigc:
            # 复用的旧图可能没有标识（本功能上线前出的）：tag_image 幂等，
            # 已带同值标的不会重复插入。
            for p in [ep["bg_h"], ep["bg_v"]] + list(built["covers"].values()):
                aigc_label.tag_image(p, cfg)
            log("AIGC 元数据已核对（背景与封面）")
    else:
        built = assets_factory.build_all(
            cfg, {"bg": layout.bg_dir(root), "cover": layout.cover_dir(root)},
            lines_text, log=log or (lambda m: None),
            prefix="%s_" % layout.safe_no(episode_no))
    result["assets"] = built

    # ---- 3. 语音合成 ----
    step(3, "语音合成", 0.30)
    # 逐句语音是过程件：合成完就只剩那条成品音频有用。放进「过程」下的本期
    # 目录，成品不跟它混放，清理时删掉也不心疼。
    audio_dir = os.path.join(work, "audio")
    audio_files = []
    durations = []
    if reuse and os.path.isdir(audio_dir):
        names = sorted(f for f in os.listdir(audio_dir) if f.endswith(".wav"))
        if len(names) == len(script):
            for f in names:
                p = os.path.join(audio_dir, f)
                audio_files.append(p)
                durations.append(tts_engine.probe_duration(p))
            log("复用已有音频（%d 段）" % len(names))
    if not audio_files:
        # 情绪档位与写脚本那一步同源：同一张范式卡。写脚本按它决定能不能填
        # 心情词，合成按它决定拼不拼「略带」。两头各取一次就会岔开——稿子按略
        # 写、声音按不贴说，正是要避免的那种不一致。
        level = paradigms.emotion_level_of(
            script_engine.resolve_paradigm(proj_item, cfg))
        tts_result = tts_engine.synthesize(script, work, cfg, log=log,
                                           emotion_level=level,
                                           voice_root=root)
        audio_files = tts_result["files"]
        durations = tts_result["durations"]

    for i, item in enumerate(script):
        if i < len(durations):
            item["actual_seconds"] = round(durations[i], 3)

    # ---- 3b. 实测回填校准 ----
    # 引擎与音色必须一起取：本地引擎的音色另存一套键（Serena / Vivian），
    # 从前这里写死读 `tts.voice_a`，本地合成的实测就全记在了 Edge 的音色名下
    # （表里出现过 `qwen3tts|zh-CN-XiaoxiaoNeural` 这种混血分组，样本越攒越多、
    # 却谁也用不上）。取法归一到 speaker_ctx，与估时、合成用同一处口径。
    engine, voice, speed = duration_model.speaker_ctx(cfg, "A")
    pairs = [{"text": script[i]["text"], "seconds": durations[i]}
             for i in range(min(len(script), len(durations)))
             if script[i].get("speaker") == "A"]
    if len(pairs) >= 3:
        st = duration_model.calibrate(calib, engine, voice, speed, pairs)
        # 顺带报一句该音色相对标准语速的比例——它是「这一期换音色后会长/短多少」
        # 的唯一依据，配置页显示的就是这个数。
        ratio = duration_model.ratio_of(calib, engine, voice, speed)
        log("校准已更新：%s k=%.3f（%s，样本 %d）%s"
            % (voice or "-", float(st.get("k") or duration_model.STANDARD_K),
               st.get("mode"), st.get("samples", 0),
               "，比例 %.2f（音色语速÷标准语速）" % ratio if ratio else ""))

    measured = sum(durations)
    result["measured_seconds"] = round(measured, 2)
    log("实测语音总时长 %.1f 秒（预估 %.1f 秒，实测语速 %.2f 字/秒）"
        % (measured, est["speech_seconds"],
           (sum(len(s["text"]) for s in script) / measured) if measured else 0))

    # ---- 4. 音频后处理（BGM 混音只在 process 内部做一次）----
    step(4, "音频后处理", 0.45)
    audio_cfg = dict(cfg)
    if built.get("bgm"):
        audio_cfg["_bgm_src"] = built["bgm"]
    if aigc:
        # AI 语音声明（显式标识，排在音频最前）。合成失败即整期停下——
        # 合规动作不许静默跳过，出了无声明的片比停下更糟。
        audio_cfg["_ai_decl_src"] = _ai_declaration(work, cfg, root, log)
        log("AI 语音声明已合成（%s）"
            % os.path.basename(audio_cfg["_ai_decl_src"]))
    mixed = audio_engine.process(audio_files, work, audio_cfg,
                                 pause=float(cfg.get("audio.pause_between_lines", 0.35)),
                                 log=log or (lambda m: None))
    audio_final = mixed["final_audio"]
    audio_duration = audio_engine.probe_duration_safe(audio_final)
    result["audio_duration"] = audio_duration

    # ---- 5. 编码音频（源固定为混音链的最终产物，不猜文件名）----
    step(5, "导出音频", 0.55)
    ext = cfg.get("audio.codec", "mp3")
    # 编码格式可配，所以扩展名以配置为准，不写死在文件表里。
    ep["audio"] = os.path.join(layout.av_dir(root),
                               "%s.%s" % (layout.safe_no(episode_no), ext))
    mp3 = audio_engine.to_encoded(audio_final, ep["audio"], cfg)
    if aigc:
        aigc_label.tag_file(mp3, cfg)
        log("音频已导出：%s（含 AIGC 元数据）" % os.path.basename(mp3))
    else:
        log("音频已导出：%s" % os.path.basename(mp3))

    # ---- 6. 字幕（时间轴 = 片头偏移 + 逐句实测，再按最终音频总长校正）----
    step(6, "生成字幕", 0.62)
    pause = float(cfg.get("audio.pause_between_lines", 0.35))
    timings = subtitle_engine.probe_timings(script, durations, pause,
                                            offset=float(mixed.get("intro_seconds", 0.0)))
    derived_end = timings[-1]["end"] if timings else 0.0
    content_end = max(0.0, audio_duration - float(mixed.get("outro_seconds", 0.0)))
    k, rescaled = subtitle_engine.rescale_timings(timings, derived_end, content_end)
    result["timeline"] = {"derived_end": round(derived_end, 3),
                          "measured_end": round(content_end, 3),
                          "scale": round(k, 4), "rescaled": rescaled,
                          "intro_seconds": mixed.get("intro_seconds", 0.0),
                          "outro_seconds": mixed.get("outro_seconds", 0.0)}
    if rescaled:
        log("时间轴按实测时长校正：推导 %.2f 秒 → 实测 %.2f 秒（系数 %.4f）"
            % (derived_end, content_end, k))

    srt = subtitle_engine.build_srt(script, cfg, timings)
    srt_path = ep["subtitle"]
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt)
    result["subtitle"] = srt_path

    font = assets_factory.resolve_font(cfg.get("subtitle.font_family", ""))
    font_dir = assets_factory.fonts_dir_of(font)

    # ---- 7. 画面 ----
    video_h = video_v = ""
    wrap_stats = {}
    if do_video:
        step(7, "合成视频", 0.70)
        w = int(cfg.get("video.width", 1920))
        h = int(cfg.get("video.height", 1080))

        ass_h, max_chars, max_lines = subtitle_engine.build_ass(script, cfg, timings, w, h, "")
        p_h = os.path.join(work, "sub.ass")
        with open(p_h, "w", encoding="utf-8") as f:
            f.write(ass_h)
        video_engine.render_ass(p_h, p_h, script, timings, cfg, w, h, "")
        wrap_stats = subtitle_engine.wrap_stats(script, max_chars, max_lines)

        video_h = ep["video"]
        video_engine.compose(audio_final, p_h, video_h, cfg, built["bg_h"],
                             audio_duration, w, h, "", font_dir=font_dir,
                             timings=timings, log=log or (lambda m: None))
        if aigc:
            aigc_label.tag_file(video_h, cfg, faststart=True)

        if cfg.get("video.produce_vertical", True):
            step(7, "合成竖屏视频", 0.85)
            wv, hv = h, w
            ass_v, mc_v, ml_v = subtitle_engine.build_ass(script, cfg, timings, wv, hv, "_v")
            p_v = os.path.join(work, "sub_v.ass")
            with open(p_v, "w", encoding="utf-8") as f:
                f.write(ass_v)
            video_engine.render_ass(p_v, p_v, script, timings, cfg, wv, hv, "_v")
            video_v = ep["video_vertical"]
            video_engine.compose(audio_final, p_v, video_v, cfg, built["bg_v"],
                                 audio_duration, wv, hv, "_v", font_dir=font_dir,
                                 timings=timings, log=log or (lambda m: None))
            if aigc:
                aigc_label.tag_file(video_v, cfg, faststart=True)

    # ---- 8. 文章与清单 ----
    step(8, "生成文章与清单", 0.95)
    article = build_article(script, cfg, title, episode_no, est)
    article_path = ep["article"]
    with open(article_path, "w", encoding="utf-8") as f:
        f.write(article)

    assets = {"video": video_h, "video_vertical": video_v, "audio": mp3,
              "article": article_path, "subtitle": srt_path,
              "bg": built["bg_h"], "covers": built.get("covers", {})}

    # 内容检（语义 / 承诺链）不在这里跑。它与其余生成阶段门禁同属脚本阶段，
    # 在 `script_engine.generate` 里判完、结论落在过程目录。合成端补跑一遍，
    # 就是同一份稿子同一段素材让模型看两遍——白花一次调用，结论还一模一样。
    manifest = {
        "version": VERSION, "title": title, "episode_no": episode_no,
        "project_id": project_id,
        "planned_episodes": plan,
        # 本期被要求产出什么。报告据此判定，不写下来它只能一律按「全套」要。
        "do_video": bool(do_video),
        "created": datetime.now().isoformat(timespec="seconds"),
        "line_count": len(script), "chars": est["total_chars"],
        "estimated_seconds": est["total_seconds"],
        "measured_seconds": measured,
        "audio_duration": audio_duration,
        "subtitle_font": {"family": font["family"], "source": font["source"]},
        "wrap_stats": wrap_stats,
        "assets": assets,
        "aigc": {"labeling": aigc,
                 "content_producer": str(cfg.get("aigc.content_producer") or ""),
                 "declaration_seconds": mixed.get("declaration_seconds", 0.0)},
        "config_snapshot": {k: v for k, v in cfg.items()
                            if not str(k).startswith(("llm.", "_"))},
    }
    if ack_script_issues:
        # 人在界面上点过「仍然合成」。凭证落进清单：这一期是在知情下出的片，
        # 不是系统替他做的决定。
        manifest["script_issues_ack"] = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "items": [i["label"] for i in (issues.get("items") or []) if not i["ok"]],
        }
    write_json(ep["manifest"], manifest)

    report = build_report(ep, cfg)
    manifest["report"] = {"passed": report["passed"],
                          "fails": [i["label"] for i in report["fails"]],
                          "warns": [i["label"] for i in report["warns"]],
                          "pending": [i["label"] for i in report["pending"]]}
    write_json(ep["manifest"], manifest)
    write_json(ep["report"], report)

    if report["passed"]:
        script_engine.clear_lock(work)
        # 只在整条链都过了才记账。未过的片子不占期号——
        # 占了下一次重跑就得跳号，进度表会与现实对不上。
        if project_id:
            try:
                project_store.record_episode(
                    base, project_id, title, episode_no,
                    manifest.get("created", ""))
                nxt = (project_store.find(base, project_id) or {}).get("next_episode")
                log("已记入项目「%s」，下一期自动排到第 %s 期"
                    % ((proj_item or {}).get("name", project_id), nxt))
            except project_store.ProjectError as e:
                log("项目记账失败（产物不受影响）：%s" % e)
    else:
        script_engine.write_lock(work, "render",
                                 "；".join(i["label"] for i in report["fails"]) or "warn 项未放行",
                                 {}, 1)

    result.update({"assets": assets, "report": report, "script_file": script_path,
                   "episode_files": ep,
                   "manifest": ep["manifest"],
                   "project_id": project_id,
                   "next_episode": (project_store.find(base, project_id) or {})
                   .get("next_episode") if project_id else ""})
    step(9, "完成", 1.0)
    log("产物校验：%s" % ("全部通过" if report["passed"] else
                        "未通过（%d 项）" % len(report["fails"] + report["warns"])))
    return result


def _step(job, n, title, progress):
    if not job:
        return
    job["stage"] = "第 %d 步 · %s" % (n, title)
    job["progress"] = max(job.get("progress", 0.0), progress)


def build_article(script, cfg, title, episode_no, est):
    """生成公众号图文（对话体）。不硬编码占位符，期数与节目名取自项目。"""
    name_a = cfg.get("tts.name_a", "A")
    name_b = cfg.get("tts.name_b", "B")
    prog = cfg.get("project.program_name", "播客")
    dur_min = est.get("total_seconds", 0) / 60.0

    lines = ["# %s" % title, ""]
    if episode_no:
        lines.append("> %s · 第 %s 期 · 约 %.1f 分钟" % (prog, episode_no, dur_min))
    else:
        lines.append("> %s · 约 %.1f 分钟" % (prog, dur_min))
    lines.append("")
    for item in script:
        who = name_a if item.get("speaker") == "A" else name_b
        lines.append("**%s**：%s" % (who, item.get("text", "")))
        lines.append("")
    attr = cfg.get("project.attribution", "")
    if attr:
        lines += ["---", "", attr]
    return "\n".join(lines) + "\n"


def continue_episode(cfg, calib, root, no, llm=None, job=None):
    """续跑：读锁文件，复用已有产物，从缺口继续。

    `root` 是这一期所属那棵树的根（项目目录，或单集的目录），`no` 是期号——
    产物位置两者合起来才定得下来。
    """
    ep = layout.episode_files(root, no)
    work = layout.tmp_dir(root, no)
    lock = script_engine.read_lock(work)
    if lock and job:
        job_log(job, "检测到锁文件：%s（%s）" % (lock.get("blocked_at"), lock.get("reason")))
    manifest = read_json(ep["manifest"], {})
    script = read_json(ep["script"], [])
    title = manifest.get("title") or os.path.basename(root)
    return run_episode(cfg, calib, material="", title=title,
                       episode_no=no or manifest.get("episode_no", ""),
                       project_dir=root, script=script or None,
                       llm=llm, reuse=True, job=job,
                       project_id=manifest.get("project_id", ""))
