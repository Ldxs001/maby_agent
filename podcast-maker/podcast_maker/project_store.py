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

"""项目登记：一档节目一个项目，项目下按期推进。

一切都是项目。哪怕只出一集，也立项——它同样有节目名、有封面、有产物，
同样要有个地方记住这些。从前单集走的是"不建项目"的第二条路，产物落到
一个谁都不认识、等人认领的目录里，画面也因此印不出节目名。两条线合一
之后，流程只剩一条：立项 → 脚本 → 合成。

因此：

- 项目是播出计划的载体：节目名、风格、音色、计划期数、下一期号
- 立项时必选规划方式（`plan_mode`），此后不可更改。三种方式回答的都是
  同一个问题——**本期素材从哪来、要不要排图**：
  `mapped` 成稿规划——已有整部作品，先由素材排出期数地图，再逐期按图出片；
  `episodic` 逐期即兴——每期临时定内容，当场给素材，不排图；
  `single` 单集——只出一集，当场给素材，不排图，出完即完结。
  事后改口会让已出的期与地图对不上账，所以定死。
- 每一次出片落成项目下的一期。有地图时期号顺序由地图决定——分支期号
  （`3a`）夹在主期之间，靠数字进位算不出来；无地图时才走 `bump_episode`。
- 计划期数留空表示交给模型规划；填了以人为准，模型的话不作数
- 下线有两条路，分得很清：`archive()` 只打标记，产物留着、可恢复；
  `delete()` 连项目目录一起删掉，不可逆。两者独立，可以先归档再删

存储分两处，各管一件事：

- `projects/_projects.json` —— 登记表：项目的身份与设定（名称、范式、风格、
  音色、期号、已出期列表）。**不含地图、不含产物路径**。
- `projects/<项目>/` —— 项目自己的目录树，素材、地图、脚本、各期成品都在
  里面（布局见 `layout`）。

地图从前塞在登记表里，改一次要连整张表一起重写，而且它跟期目录谁也管不着谁。
搬进项目目录后，"这个项目的全部材料"就是一整个文件夹——备份、迁移、整个
删掉，都是对一个目录动手。
"""

import json
import os
import re
import shutil
import time

from . import layout

STORE_NAME = "_projects.json"
STORE_VERSION = 2

# 立项时选定，此后不可更改（理由见模块 docstring）。
PLAN_MODES = ("mapped", "episodic", "single")
PLAN_MODE_LABELS = {"mapped": "成稿规划", "episodic": "逐期即兴",
                    "single": "单集",
                    "legacy": "未定（旧项目）"}

# 单集：只有一期，不排图，出完即完结。它同样是一个项目——节目名、副标题、
# 画面与别处一致，产物落在自己的目录里、登记在册，不必等人认领。
MODE_SINGLE = "single"

# 本功能上线前立的项目，落盘没有 plan_mode。它们是「还没选过」，
# 不是「选了逐期即兴」——所以允许补选一次，选定之后同样定死。
MODE_LEGACY = "legacy"


class ProjectError(RuntimeError):
    pass


# ------------------------------------------------------------------ 读写
def store_path(base):
    return os.path.join(base, STORE_NAME)


def _read(base):
    path = store_path(base)
    if not os.path.exists(path):
        return {"version": STORE_VERSION, "projects": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # 登记表损坏不能静默重建——那会丢掉全部期号记忆。
        raise ProjectError("项目登记表无法解析：%s（%s）" % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        raise ProjectError("项目登记表结构不对：%s" % path)
    data.setdefault("version", STORE_VERSION)
    # 旧登记表没有这两个字段。规划方式记成「未定」而不是直接当成逐期即兴：
    # 那些项目当年并没有做过这个选择，替它们选掉，成稿规划这条路就再也走不上
    # 了（除重建项目之外），而它们恰恰是最想排地图的那些。
    for p in data["projects"]:
        if isinstance(p, dict):
            if not str(p.get("plan_mode") or "").strip():
                p["plan_mode"] = MODE_LEGACY
            # 地图存在项目自己的目录里，读到内存时挂回 `map` 字段：上层
            # （`map_episodes` 等）照旧读 item["map"]，不必知道它存在哪。
            stored = p.get("map")
            p["map"] = load_map(base, p.get("id") or "")
            if p["map"] is None and isinstance(stored, dict) and stored.get("episodes"):
                # 登记表里还留着地图（搬结构之前建的）：顺手搬进项目目录。
                # 不搬的话，下一次写盘就把它抹掉了——排一张图是几百次调用，
                # 抹掉它连一句提示都不会有。
                p["map"] = save_map(base, p.get("id") or "",
                                    stored.get("note", ""),
                                    stored.get("episodes") or [])
    return data


def _write(base, data):
    os.makedirs(base, exist_ok=True)
    path = store_path(base)
    tmp = path + ".tmp"
    # 地图不进登记表。带着它写会把同一份地图存成两份——改了一处，另一处
    # 就是旧的，而两份都看着像真的。
    payload = {"version": data.get("version", STORE_VERSION),
               "projects": [{k: v for k, v in p.items() if k != "map"}
                            for p in data.get("projects", []) if isinstance(p, dict)]}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------ 地图存取
def project_dir(base, pid):
    """项目目录。布局由 `layout` 定，这里只转发一次，免得各处自己拼。"""
    return layout.project_dir(base, pid)


def load_map(base, pid):
    """读项目的地图。没有地图文件返回 None（mapped 项目排图之前就是这样）。"""
    path = layout.map_file(project_dir(base, pid))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # 地图坏了不能当"没有地图"——那会让排过图的项目退回"未排图"，
        # 界面上看着只是还没排，实际是把图弄丢了。宁可报错。
        raise ProjectError("项目地图无法解析：%s（%s）" % (path, e))
    return data if isinstance(data, dict) else None


def save_map(base, pid, note, rows):
    """写项目的地图文件。调用方保证 `rows` 已归一。"""
    root = project_dir(base, pid)
    os.makedirs(layout.map_dir(root), exist_ok=True)
    path = layout.map_file(root)
    tmp = path + ".tmp"
    payload = {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "note": note or "", "episodes": rows}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return payload


# ------------------------------------------------------------------ 工具
def _slug(text):
    s = re.sub(r"[^0-9A-Za-z]+", "-", (text or "")).strip("-").lower()
    return s[:24]


def new_id(base, name):
    os.makedirs(base, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = _slug(name)
    pid = "%s-%s" % (stamp, slug) if slug else stamp
    taken = {p.get("id") for p in _read(base)["projects"]}
    if pid not in taken:
        return pid
    # 项目名多为中文，slug 一空下来 id 就只剩秒级时间戳，同一秒连立两个
    # 项目必然撞号。撞号不会报错，只会让 find() 永远返回先立的那一个，
    # 后立的项目看着建成了、点进去却是别人的数据。所以这里必须自己去重。
    for n in range(2, 1000):
        cand = "%s-%d" % (pid, n)
        if cand not in taken:
            return cand
    raise ProjectError("同一秒内立了太多项目，请稍后再试。")


def bump_episode(no):
    """期号进位。非数字期号（如 SP01）在末尾数字上进位，无数字则原样返回。"""
    s = str(no or "").strip()
    m = re.search(r"(\d+)(\D*)$", s)
    if not m:
        return s
    digits, tail = m.group(1), m.group(2)
    width = len(digits)
    return s[:m.start(1)] + str(int(digits) + 1).zfill(width) + tail


def branch_no(anchor_no, n):
    """分支期号：锚点期号 + 小写字母后缀。`branch_no("3", 1)` → `3a`。

    同一锚点下插第二批时字母继续往后排（`3a 3b 3c` → `3d 3e`），
    既不重置也不另立一套编号——呈现出来的就是主线上多出来的几格。
    超过 26 个进位成双字母（`3z` → `3aa`）。
    """
    anchor = str(anchor_no or "").strip()
    if not anchor:
        raise ProjectError("分支期号缺少锚点期号。")
    n = int(n)
    if n < 1:
        raise ProjectError("分支序号从 1 起。")
    # 双射式 26 进制：1→a、26→z、27→aa。写成 n-=1 再取模，
    # 否则 z 之后会跳到 ba，中间的 aa…az 全部被跳过。
    letters = ""
    while n > 0:
        n -= 1
        letters = chr(ord("a") + n % 26) + letters
        n //= 26
    return anchor + letters


def is_branch_no(no):
    """分支期号以字母结尾，如 `3a`。"""
    return bool(re.search(r"[a-z]+$", str(no or "").strip()))


def find(base, pid):
    if not pid:
        return None
    for p in _read(base)["projects"]:
        if p["id"] == pid:
            return p
    return None


# ------------------------------------------------------------------ 增删改
def create(base, name, plan_mode, program_name="", planned_episodes=None,
           style_preset="", voice_a="", voice_b="", name_a="", name_b="",
           first_episode="1", note="", paradigm="", subtitle="",
           qwen_voice_a="", qwen_voice_b="", audience="", focus_note=""):
    """立项。`plan_mode` 必填，且此后不可更改。返回项目 dict。

    `paradigm`（素材类型）是排地图的组织依据：立项时可指定，也可留空由
    探查推断。它与 `plan_mode` 不同 —— **此后可以改**，因为它不产生产物，
    只影响下一次重排用什么依据。
    """
    name = (name or "").strip()
    if not name:
        raise ProjectError("项目名不能为空。")
    if plan_mode not in PLAN_MODES:
        raise ProjectError(
            "立项必须选定规划方式（%s），收到的是 %r。"
            % ("、".join("%s（%s）" % (k, PLAN_MODE_LABELS[k]) for k in PLAN_MODES),
               plan_mode))
    if int(planned_episodes or 0) < 0:
        raise ProjectError("计划期数不能为负。")
    if plan_mode == MODE_SINGLE:
        # 单集恒为一期。计划期数对它没有意义——它不会有「下一期」，留着这个
        # 数只会让进度条指向一个永远不会到的第二期。
        planned_episodes = 1
    from . import paradigms as _pg
    para = (paradigm or "").strip()
    if para and para not in _pg.PARADIGMS:
        raise ProjectError("未知素材类型：%r——可选 %s。"
                           % (para, "、".join(sorted(_pg.PARADIGMS))))
    data = _read(base)
    pid = new_id(base, name)
    item = {
        "id": pid,
        "name": name,
        "program_name": (program_name or name).strip(),
        # 副标题：印在主标题下面的那一行，也是整档节目固定的一句。
        # 它跟节目名一样归项目——同一个节目每一期的封面都写这句。
        "subtitle": (subtitle or "").strip(),
        # 受众：片头「面向___的听众」里填的那个定语，整档节目一句、每期逐字一致。
        # 排地图那一步会给出第一版（见 planner.MAP_SCHEMA），此后由人在界面上改；
        # **人填过就以人为准，重排不覆盖**——与 planned_episodes 同一条纪律。
        # 留空不是错误：模板会把「面向…的听众」整个子句收起来，片头退化成
        # 「欢迎收听《X》。」，不会留下半句话。
        "audience": (audience or "").strip(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 立项时定死，不列入 EDITABLE。
        "plan_mode": plan_mode,
        # 期数地图。mapped 项目在排完地图前为 None；episodic 与 single 恒为 None。
        "map": None,
        # None 表示交给模型规划；填了数字以人为准
        "planned_episodes": int(planned_episodes) if planned_episodes else None,
        "next_episode": str(first_episode or "1"),
        "episodes": [],
        "style_preset": style_preset or "",
        # 素材类型：排地图的组织依据。空串表示按探查推断。
        # 与 plan_mode 不同，它**可以改** —— 它不产生产物，只是下一次重排的依据；
        # 改完要显式选「重排地图」还是「保留旧地图」，不静默生效。
        "paradigm": para,
        # 本档侧重：人给这档节目定的方向（自然语言），与范式卡的重点判据
        # **同时**进排图提示词。它管的是分组粒度——侧重的内容可以分得更细、
        # 次要的合得更粗——所以归排图，不进写脚本那一步。
        # 空串表示人没写过：那时排图提示词与从前**逐字一致**，不让一句默认话
        # 把老口径带偏（见 `paradigms.prompt_block` 的 `extra_focus`）。
        "focus_note": (focus_note or "").strip(),
        "voice_a": voice_a or "", "voice_b": voice_b or "",
        # 本地引擎的音色：它决定「用哪个内置音色去录参考音频」。录出来的音频落在
        # 项目自己的「音色」目录里，此后整期读的都是那份文件，与这个字段无关了 ——
        # 换音色要重新录，改这个字段不会悄悄换掉已经在用的音色。
        "qwen_voice_a": qwen_voice_a or "",
        "qwen_voice_b": qwen_voice_b or "",
        "name_a": name_a or "", "name_b": name_b or "",
        "note": note or "",
        "archived": False,
    }
    data["projects"].append(item)
    # 立项即建目录：素材、地图、脚本、成品此后都落在里面。目录建在立项这一步，
    # 而不是等第一次出片才建——脚本、地图都要有地方放，等出片就晚了。
    layout.ensure(base, pid)
    _write(base, data)
    return item


EDITABLE = ("name", "program_name", "subtitle", "audience", "planned_episodes",
            "next_episode", "style_preset", "voice_a", "voice_b",
            "qwen_voice_a", "qwen_voice_b",
            "name_a", "name_b", "note", "archived", "paradigm", "focus_note")

# 项目设定盖过哪些全局配置。这个映射只能有一份：生成脚本与出片若各写一份，
# 两边迟早不一致——比如生成时按全局节目名写片头句，出片时按项目节目名去验，
# 片头句就成了错的，而两处的代码单看都挑不出毛病。
# 右边列进 CONFIG_PASS_THROUGH 的是通道键，不是配置点位（见下）。
PROJECT_CONFIG_MAP = (
    ("program_name", "project.program_name"),
    ("subtitle", "project.subtitle"),
    # 受众进通道：片头那句「面向___的听众」由脚本引擎在粘合时取用。同理于副标题
    # ——整档节目固定一句，归项目，不归某一期。
    ("audience", "project.audience"),
    ("style_preset", "script.style_preset"),
    ("voice_a", "tts.voice_a"),
    ("voice_b", "tts.voice_b"),
    # 本地引擎（Qwen3-TTS）的音色另存一份，不跟 Edge 共用字段。
    # 两套取值互不相通（Edge 是 zh-CN-XiaoxiaoNeural 这种，本地是 Vivian 这种），
    # 共用一个字段的话，在本地引擎下设好的音色一切到 Edge 就是非法值，反之亦然。
    # 此前这里只有上面两行，本地引擎那套键从来没被项目盖过 —— 项目里选了音色
    # 也不生效，界面上那个下拉是个死框。
    ("qwen_voice_a", "tts.qwen3tts_voice_a"),
    ("qwen_voice_b", "tts.qwen3tts_voice_b"),
    ("name_a", "tts.name_a"),
    ("name_b", "tts.name_b"),
)

# 通道键：不出现在配置页，也不由人直接配。它们是「项目设定送下去」的唯一通道，
# 值只可能来自项目（见 apply_to_config）。单集模式没有项目，就取到空，画面上
# 少印那一行。把它们塞进配置点位的表里会误导人——配置页摆一个框，填了也照样
# 被项目盖掉，那就是一开始那个「填了不生效」的死框换了个地方长出来。
CONFIG_PASS_THROUGH = ("project.program_name", "project.subtitle",
                       "project.audience")


def apply_to_config(cfg, item):
    """把项目设定叠到一份配置副本上，返回新副本。

    只返回副本，绝不回写全局：否则做完 A 项目再开 B 项目，A 的风格已经
    渗进全局默认值里，而界面上那个下拉还敢显示成「默认」。
    """
    out = dict(cfg)
    if not item:
        return out
    for pkey, ckey in PROJECT_CONFIG_MAP:
        val = item.get(pkey)
        if val not in (None, ""):
            out[ckey] = val
    return out


def update(base, pid, patch):
    """改项目。计划期数传空即回到「由模型规划」。

    `plan_mode` 不在 EDITABLE 里，只有一种例外：旧项目补选一次。
    补选之后就与立项时一样定死。
    """
    data = _read(base)
    for p in data["projects"]:
        if p["id"] != pid:
            continue
        want_mode = str(patch.get("plan_mode") or "").strip()
        if want_mode:
            if p.get("plan_mode") != MODE_LEGACY:
                raise ProjectError("规划方式在立项时定死，不可更改。")
            if want_mode not in PLAN_MODES:
                raise ProjectError(
                    "规划方式只能是 %s。" % "、".join(PLAN_MODES))
        want_para = str(patch.get("paradigm") or "").strip()
        if "paradigm" in patch and want_para:
            from . import paradigms
            if want_para not in paradigms.PARADIGMS:
                raise ProjectError("未知素材类型：%s" % want_para)
        for k in EDITABLE:
            if k not in patch:
                continue
            v = patch[k]
            if k == "planned_episodes":
                if (p.get("plan_mode") or "") == MODE_SINGLE:
                    # 单集恒为一期。这个数由立项定死，改它只会让进度标签自相矛盾。
                    continue
                p[k] = int(v) if str(v or "").strip() not in ("", "0", "none", "None") else None
            elif k == "next_episode":
                p[k] = str(v or "").strip() or p.get("next_episode", "1")
            elif k == "program_name":
                # 与立项同一条规矩：留空就同项目名。两处口径不一致的话，立项时
                # 填过、后来清空，节目名会变成一个空串，画面上就少了一行字。
                p[k] = (v or "").strip() or p.get("name", "")
            elif k == "archived":
                p[k] = bool(v)
            else:
                p[k] = (v or "").strip() if isinstance(v, str) else v
        if want_mode:
            p["plan_mode"] = want_mode
        _write(base, data)
        return p
    raise ProjectError("项目不存在：%s" % pid)


def archive(base, pid, on=True):
    """下线。产物与期目录一律保留（归档优于删除）。"""
    return update(base, pid, {"archived": bool(on)})


def _dir_bytes(path):
    """目录占用字节数。只用于告诉人「删掉了多少」，算不出就当 0。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:                                      # noqa: PERF203
                pass
    return total


def delete(base, pid):
    """删除项目：登记表条目与项目目录一并删掉，不留东西。

    与 `archive()` 分得很清，两者独立、可以叠加：

    - 归档是「从列表里下线」——只翻一个标记，产物留着，随时能恢复。
    - 删除是「不留了」——登记表条目拔掉，项目目录（素材、地图、脚本、各期
      成品）整个删掉。**不可逆**。

    所以顺序是**先删目录、后写登记表**：中途出错时表还是原样，项目照旧出现
    在列表里，重试即可；反过来出错就留下「表里查不到、磁盘上还占着」的孤儿
    目录，只能扫盘才找得回来。
    """
    data = _read(base)
    hit = None
    for i, p in enumerate(data["projects"]):
        if p.get("id") == pid:
            hit = data["projects"].pop(i)
            break
    if hit is None:
        raise ProjectError("项目不存在：%s" % pid)
    root = os.path.abspath(project_dir(base, pid))
    # 路径由登记表里的 pid 拼出，正常不会越界；仍核一次前缀——手改坏登记表
    # 之后，删目录这一步不该连累到仓库之外。
    home = os.path.abspath(base)
    if os.path.dirname(root) != home:
        raise ProjectError("项目目录不在项目库里，拒绝删除：%s" % root)
    freed, removed = 0, False
    if os.path.isdir(root):
        freed = _dir_bytes(root)
        shutil.rmtree(root)
        removed = True
    _write(base, data)
    return {"id": pid, "name": hit.get("name") or "", "dir": root,
            "dir_removed": removed, "freed_bytes": freed,
            "episodes": len(hit.get("episodes") or [])}


# ------------------------------------------------------------------ 期数地图
def map_episodes(item):
    """项目的地图期列表（有序）。没有地图返回空表。"""
    m = (item or {}).get("map") or {}
    rows = m.get("episodes")
    return rows if isinstance(rows, list) else []


def next_from_map(item):
    """地图里第一个未出片的期号；地图已全部出完则返回空串。"""
    for e in map_episodes(item):
        if not e.get("done"):
            return str(e.get("no") or "")
    return ""


def mark_episode_done(item, no):
    """就地（不写盘）把地图里某期标成已出片。"""
    no = str(no or "").strip()
    for e in map_episodes(item):
        if str(e.get("no") or "").strip() == no:
            e["done"] = True
            return True
    return False


def _line_no(value):
    """落点行号。取不到就留空——空表示「按标题找第一条」，与从前一致。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_map(episodes):
    """地图条目归一。入口即拦，不把坏数据留到出片时才现形。"""
    rows, seen = [], set()
    for i, e in enumerate(episodes or []):
        if not isinstance(e, dict):
            raise ProjectError("地图第 %d 项不是对象。" % (i + 1))
        no = str(e.get("no") or "").strip()
        if not no:
            raise ProjectError("地图第 %d 项缺期号。" % (i + 1))
        if no in seen:
            raise ProjectError("地图期号重复：%s。" % no)
        seen.add(no)
        title = str(e.get("title") or "").strip()
        if not title:
            raise ProjectError("地图第 %s 期缺标题。" % no)
        raw_points = e.get("points") or []
        if isinstance(raw_points, str):
            raw_points = [raw_points]
        points = [str(x).strip() for x in raw_points if str(x).strip()]
        refs = []
        for r in (e.get("refs") or []):
            if not isinstance(r, dict):
                continue
            sid = str(r.get("source") or "").strip()
            if sid:
                refs.append({"source": sid,
                             "anchor": str(r.get("anchor") or "").strip(),
                             "line": _line_no(r.get("line"))})
        rows.append({"no": no, "title": title, "gist": str(e.get("gist") or "").strip(),
                     "points": points, "refs": refs,
                     "chars": int(e.get("chars") or 0),
                     # 这一期出过片没有。从前存的是产物目录名，那会儿一期一个
                     # 目录；现在产物按类型分放在项目里，没有「期目录」可指，
                     # 需要知道的只是「出没出过」。
                     "done": bool(e.get("done"))})
    return rows


def set_map(base, pid, episodes, note="", audience=None):
    """写入地图。`episodes` 有序，每项 `{no, title, points, refs}`。

    `audience` 是排图时模型给的那一句受众（见 `planner.MAP_SCHEMA`）。**只在项目
    里还空着的时候写入**：人改过就以人为准——重排一次把人的话顶掉，等于让人白填，
    而片头每期都要用这一句。传 None（插入、补排等不产受众的路子）就整个不动它。

    只有 `mapped` 项目有地图。校验放在入口而不是出片时：地图的期号一旦
    重复或错位，按图取素材会取到别人的料，而产物表面上完全正常。
    """
    data = _read(base)
    for p in data["projects"]:
        if p["id"] != pid:
            continue
        if p.get("plan_mode") != "mapped":
            raise ProjectError("只有「成稿规划」项目才有期数地图。")
        rows = _clean_map(episodes)
        p["map"] = save_map(base, pid, note, rows)
        # 计划期数跟着地图走：地图排了几期就是几期，不必再手填一遍。
        p["planned_episodes"] = len(rows) or None
        got = str(audience or "").strip()
        if got and not (p.get("audience") or "").strip():
            p["audience"] = got
        _write(base, data)
        return p
    raise ProjectError("项目不存在：%s" % pid)


def branch_chain_end(rows, anchor_no):
    """锚点分支链在地图里的最后位置（下标）；锚点没有分支时就是锚点自身。

    分支号是「锚点号 + 字母尾巴」（3a、3aa…），正则认准锚点号前缀加纯字母
    尾巴，数字期号（30、31）不会被误认成锚点 3 的分支。链在地图上**不假设
    连续**——历史上插过的批次、人手动挪过的顺序都存在——所以扫全表取最后
    一条分支的下标。新批插在它后面：后插的永远排在先插的后面，编号与物理
    位置天然咬合。锚点不在地图里返回 None，由调用方报错。

    落位为什么是链尾而不是锚点正后方：往期 3 插第二批时，第一批（3a、3b…）
    已经排在 3 后面，插在 3 正后方会把新批挤到旧批前面——顺序就成了
    3、新批、旧批，听着像节目倒着长。插在链尾，地图永远是顺着长的。
    """
    anchor = str(anchor_no or "").strip()
    pat = re.compile(r"^%s[a-z]+$" % re.escape(anchor))
    idx = end = None
    for i, e in enumerate(rows):
        no = str(e.get("no") or "").strip()
        if no == anchor:
            idx = i
        elif pat.match(no):
            end = i
    if idx is None:
        return None
    return end if (end is not None and end > idx) else idx


def insert_branches(base, pid, anchor_no, episodes, note=""):
    """把分支期插到锚点分支链的末尾，返回项目 dict。

    只改地图，不动任何已出片的期。分支期号由调用方按 `branch_no()` 生成——
    期号的编排规则属于代码，不该由模型决定。落位在**分支链末尾**而不是
    锚点条目正后方：同一锚点插第二批时，新批要排在第一批的后面，不能挤到
    它前面（见 `branch_chain_end`）。
    """
    data = _read(base)
    for p in data["projects"]:
        if p["id"] != pid:
            continue
        rows = map_episodes(p)
        if not rows:
            raise ProjectError("项目还没有地图，无处插入。")
        anchor_no = str(anchor_no or "").strip()
        idx = branch_chain_end(rows, anchor_no)
        if idx is None:
            raise ProjectError("锚点期号在地图里不存在：%s。" % anchor_no)
        new_rows = _clean_map(episodes)
        if not new_rows:
            raise ProjectError("没有要插入的期。")
        exist = {str(e.get("no") or "") for e in rows}
        for r in new_rows:
            if r["no"] in exist:
                raise ProjectError("插入的期号与现有重复：%s。" % r["no"])
            exist.add(r["no"])
        rows[idx + 1:idx + 1] = new_rows
        p["map"] = save_map(base, pid, (p.get("map") or {}).get("note", ""), rows)
        p["planned_episodes"] = len(rows) or None
        _write(base, data)
        return p
    raise ProjectError("项目不存在：%s" % pid)


def record_episode(base, pid, title, no, created=""):
    """把一期落进项目，并推进期号。

    产物落在项目目录里，位置由期号决定（见 `layout`），所以这里不再记目录名——
    记下来的话就是第二个真值来源，跟磁盘上的实际情况迟早对不上。
    """
    if not pid:
        return None
    no = str(no or "").strip()
    data = _read(base)
    for p in data["projects"]:
        if p["id"] != pid:
            continue
        row = {"no": no, "title": title or "",
               "created": created or time.strftime("%Y-%m-%d %H:%M:%S")}
        p["episodes"] = [e for e in p["episodes"]
                         if str(e.get("no") or "").strip() != no]
        p["episodes"].append(row)
        p["episodes"].sort(key=lambda e: e.get("created", ""))
        # 有地图时下一期由地图决定。分支期号（3a）夹在主期之间，靠数字进位
        # 算不出来——bump_episode("3a") 会得到 "4a"，把分支当成了新主线。
        mark_episode_done(p, no)
        # 地图不在登记表里，改过就要单独写回——不然「这期出过了」只活在
        # 这次调用的内存里，重下一期还是这一期。
        if p.get("map"):
            save_map(base, pid, (p["map"] or {}).get("note", ""), map_episodes(p))
        nxt = next_from_map(p)
        if nxt:
            p["next_episode"] = nxt
        elif (p.get("plan_mode") or "") == MODE_SINGLE:
            # 单集出完即完结：不再排「下一期」。「一集接一集地出」是逐期即兴，
            # 立项时就分了家，不在这一步回头改口。
            p["next_episode"] = no
        elif str(no or "").strip():
            p["next_episode"] = bump_episode(no)
        else:
            p["next_episode"] = bump_episode(p.get("next_episode", "1"))
        _write(base, data)
        return p
    raise ProjectError("项目不存在：%s" % pid)


# ------------------------------------------------------------------ 查询
def progress(item):
    """项目进度：已出期数 / 计划期数 / 下一期号 / 地图规模。"""
    done = len(item.get("episodes") or [])
    plan = item.get("planned_episodes")
    mode = item.get("plan_mode") or MODE_LEGACY
    mapped = len(map_episodes(item))
    if mode == MODE_SINGLE:
        # 单集没有「下一期」，进度就是出没出片这一件事。
        label = "已出片" if done else "尚未出片"
    elif mapped:
        label = "%d / %d 期（已排图）" % (done, mapped)
    elif plan:
        label = "%d / %d 期" % (done, plan)
    else:
        label = "已出 %d 期 · 期数由模型规划" % done
    from . import paradigms
    para = str(item.get("paradigm") or "").strip()
    return {
        "done": done,
        "planned": plan,
        "mapped": mapped,
        "mode": mode,
        "mode_label": PLAN_MODE_LABELS.get(mode, mode),
        "paradigm": para,
        "paradigm_label": (paradigms.get(para)["label"] if para
                           else "自适应（按结构推断）"),
        "legacy": mode == MODE_LEGACY,
        "single": mode == MODE_SINGLE,
        "next_episode": item.get("next_episode", "1"),
        "ratio": (float(done) / plan) if plan else None,
        "label": label,
    }


def summary(base):
    """项目登记表 + 归档标记。产物是否齐全由调用方按需核对。"""
    out = []
    for p in _read(base)["projects"]:
        item = dict(p)
        item["progress"] = progress(p)
        out.append(item)
    out.sort(key=lambda x: (bool(x.get("archived")), x.get("created", "")),
             reverse=False)
    return out


def orphans(base):
    """磁盘上有产物、登记表里却没有的树 —— 单集模式的产出。

    不自动并进任何项目：归属是人决定的，猜错了比不猜更糟。
    """
    if not os.path.isdir(base):
        return []
    known = {p.get("id") for p in _read(base)["projects"]}
    out = []
    for name in sorted(os.listdir(base), reverse=True):
        root = os.path.join(base, name)
        if not os.path.isdir(root) or name in known:
            continue
        # 登记表里没有这棵树：看它有没有产出过。有「报告」且有清单才算，
        # 光有目录不算——空目录满街都是，不该混进「待认领」里。
        rdir = layout.report_dir(root)
        if not os.path.isdir(rdir):
            continue
        files = [f for f in sorted(os.listdir(rdir), reverse=True)
                 if f.endswith(".manifest.json")]
        if files:
            out.append({"dir": name, "no": "", "path": os.path.join(rdir, files[0])})
    return out
