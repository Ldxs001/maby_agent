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

"""语音合成：音色注册表 + 逐句合成 + 实测时长回填 + 试听。

两条引擎适配：
    edge       Edge-TTS（客户端库 LGPL-3.0；声音来自微软 Edge 浏览器的在线
               朗读接口，非公开授权 API，产出音频受微软服务条款约束）。
               变速用 rate 参数（变速不变调）
    qwen3tts   本地 Qwen3-TTS 服务（见 tts_service/serve.py）。变速统一走 ffmpeg
               atempo（保持音高）—— 原项目用 np.interp 重采样，等于变速且变调，
               两条路径语义不等价。音色向服务端枚举，不在本地写死。

采样率不再硬编码：一律重采样到 audio.sample_rate，实测时长由 ffprobe 读出，
回填给时长模型做校准（时长模型的唯一换算入口）。

合成前腾显存：本地 TTS 上 GPU 要占约 5GB，而驻留的 LLM 动辄十几 GB，两者同时
在场必然抢显存（TTS 会被降级甚至加载失败）。所以 synthesize() 默认先把 LM Studio /
Ollama 的模型请下显存——语音合成本来就用不到任何语言模型。资源充裕、确实想一边
跑 LLM 一边合成的，把 tts.unload_llm_before_synth 关掉即可。
"""

import asyncio
import base64
import ctypes
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

from . import bins, layout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE_CACHE = os.path.join(ROOT, "voices_cache.json")

# 本地服务引擎：走同一套 HTTP 契约（POST /tts 合成，GET /speakers 枚举音色）
LOCAL_ENGINES = ("qwen3tts",)
DEFAULT_SERVICE_HOST = "127.0.0.1"
DEFAULT_SERVICE_PORT = 9880

PREVIEW_TEXT = "这是一段音色试听，用来判断语速与听感。"


class TTSError(RuntimeError):
    """合成失败。绝不静默跳过某一句。"""


# ------------------------------------------------------------------ 环境
def ffmpeg_bin():
    exe = bins.locate("ffmpeg")
    if not exe:
        raise TTSError(bins.missing_message("ffmpeg"))
    return exe


def ffprobe_bin():
    exe = bins.locate("ffprobe")
    if not exe:
        raise TTSError(bins.missing_message("ffprobe"))
    return exe


def probe_duration(path):
    r = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True)
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def service_url(cfg, path):
    """拼本地语音服务地址。"""
    cfg = cfg or {}
    host = cfg.get("tts.qwen3tts_host") or DEFAULT_SERVICE_HOST
    port = int(cfg.get("tts.qwen3tts_port") or DEFAULT_SERVICE_PORT)
    return "http://%s:%d%s" % (host, port, path)


def service_health(cfg, timeout=6):
    """问一次服务健康接口。返回 (ok, 说明, 原始字典)。

    服务没起来就是没起来——不猜、不假装可用。设置页据此显示真实状态。
    """
    url = service_url(cfg, "/health")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return False, _offline_reason(url, e.reason), {}
    except Exception as e:  # noqa: BLE001
        return False, _offline_reason(url, e), {}

    if not info.get("ok"):
        return False, "本地语音服务异常：%s" % (info.get("last_error") or "未知原因"), info
    if info.get("last_error"):
        return False, "服务在线但模型加载失败：%s" % info["last_error"], info
    return True, ("本地 TTS 服务在线 · 设备 %s · 空闲显存 %sMB"
                  % (info.get("device") or "未加载", info.get("vram_free_mb"))), info


def engine_available(engine, cfg=None):
    """引擎可用性探测。返回 (ok, message)。"""
    if engine in LOCAL_ENGINES:
        return service_health(cfg)[:2]
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False, "未安装 edge-tts（pip install edge-tts）"
    try:
        ffmpeg_bin()
    except TTSError as e:
        return False, str(e)
    return True, "Edge-TTS 可用"


# ------------------------------------------------------------------ 腾显存
def _lms_bin():
    """找 LM Studio 的命令行。找不到返回 None。"""
    exe = shutil.which("lms")
    if exe:
        return exe
    for name in ("lms.exe", "lms"):
        p = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", name)
        if os.path.exists(p):
            return p
    return None


def _cmd(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def unload_llm_models(log=None):
    """把驻留的语言模型请下显存，腾给语音合成。

    为什么要做：本地 TTS 上 GPU 要占约 5GB，而 LM Studio 里的模型动辄十几 GB。
    两者同时在场，TTS 会因空闲显存不足被降级到 CPU（慢十倍），严重时直接加载
    失败。语音合成全程用不到任何语言模型，所以合成前先把它们请下去。

    没装 lms / ollama 不算失败——本机没有可卸的东西而已。
    返回 {"unloaded": [...], "skipped": [...], "failed": [...]}。
    """
    log = log or (lambda m: None)
    result = {"unloaded": [], "skipped": [], "failed": []}

    lms = _lms_bin()
    if lms:
        try:
            r = _cmd([lms, "unload", "--all"])
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            tail = out.splitlines()[-1].strip() if out else ""
            if r.returncode == 0:
                result["unloaded"].append("LM Studio：%s" % (tail or "已卸载"))
            else:
                result["failed"].append("LM Studio 卸载失败：%s"
                                        % (tail or "返回码 %d" % r.returncode))
        except Exception as e:  # noqa: BLE001
            result["failed"].append("LM Studio 卸载异常：%s" % e)
    else:
        result["skipped"].append("没找到 LM Studio 命令行")

    ollama = shutil.which("ollama")
    if ollama:
        try:
            ps = _cmd([ollama, "ps"], timeout=60)
            names = []
            for ln in (ps.stdout or "").splitlines()[1:]:
                parts = ln.split()
                if parts:
                    names.append(parts[0])
            for n in names:
                _cmd([ollama, "stop", n], timeout=120)
            if names:
                result["unloaded"].append("Ollama：已停 %d 个（%s）"
                                          % (len(names), ", ".join(names)))
            else:
                result["skipped"].append("Ollama 没有驻留模型")
        except Exception as e:  # noqa: BLE001
            result["failed"].append("Ollama 卸载异常：%s" % e)
    else:
        result["skipped"].append("没找到 Ollama 命令行")

    for m in result["unloaded"]:
        log("合成前腾显存 · %s" % m)
    for m in result["failed"]:
        log("合成前腾显存 · %s" % m)
    return result


# ------------------------------------------------------------ 本地服务生命周期
# 选了本地引擎，服务就得在场上。让用户为了出一期片先去开一个命令行窗口，是把
# 程序自己该做的事推给人；而如果不管它，几十句语音会各自去撞一次「连接被拒」，
# 白白重试到最后一句话才失败。所以开工前探一次：在线就用，不在线就地拉起，
# 拉不起来当场说清原因。
#
# 谁起的谁负责关。自己拉起来的服务，等最后一个合成任务收工后停掉，把那几个 G
# 显存还回去；别人先起的（用户手动开着、上一批留下的）一律不动——那不是我们的
# 进程，不该由我们决定它的生死。
_SERVICE_LOCK = threading.Lock()
_SERVICE = {"refs": 0, "proc": None, "job": None, "owned": False}


# 本地服务要在一个独立环境里跑，理由写在 tts_service/setup_env.py 的文件头
# （torch 的 CUDA 轮子约 4.8 GB，装进主环境会把主程序自己的依赖一起搅动）。
#
# 探活分三件事——环境在不在、包齐不齐、模型下没下——因为这三件的补救动作完全
# 不同：包没装就让人去下模型，白下 4 GB 也照样跑不起来。三件事都由 setup_env
# 判定，全仓只有那一份实现；这里只负责把结论翻译成话说给人听。
#
# 这三件事属于**配置阶段**：用户把语音引擎选成 Qwen3-TTS 的那一刻，界面就该把
# 缺的东西补上（见 provision）。合成阶段只剩「在不在线」，不该再有「没装」这一态。
_SERVICE_DIR = os.path.join(ROOT, "tts_service")
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)
import setup_env as tts_setup  # noqa: E402

_GAPS = ("env", "packages", "model")


def venv_python():
    """本地服务独立环境里的解释器。环境还没建时返回 None。"""
    return tts_setup.venv_python()


def local_state():
    """三态 + 一句人话。配置页据此显示缺什么、要不要给「搭建」按钮。

    `ready` 为真表示能干活，**不代表服务正在跑**——那是另一件事，由
    service_health 回答。
    """
    st = tts_setup.state()
    st["ready"] = not [k for k in _GAPS if not st[k]["ok"]]
    st["message"] = "" if st["ready"] else _state_message(st)
    return st


def _state_message(st):
    """缺什么说什么，并且说清去哪补。

    三条都指向同一个动作（配置页的「搭建」），因为它们本来就是同一件事的三个
    进度：环境 → 包 → 模型。分开说是因为「装到哪一步了」决定了用户还会等多久。
    """
    if not st["env"]["ok"]:
        return ("本地语音服务的运行环境还没建（%s）。到配置页点一次「搭建本地语音"
                "环境」——环境、依赖、模型都由程序自己装，不需要你预先装任何 Python 包。"
                % os.path.join("tts_service", ".venv"))
    if not st["packages"]["ok"]:
        return ("运行环境在，但依赖还没装齐（缺 %s）。配置页点「搭建」，它会接着"
                "装完；已经装好的不会重装。"
                % "、".join(st["packages"]["missing"]))
    missing = [m["id"].split("/")[-1]
               for m in (st.get("model", {}).get("models") or []) if not m["ok"]]
    if missing:
        return ("依赖装齐了，模型权重还缺 %s（两个变体合计约 7.7 GB：一个给新项目"
                "录角色音色，一个用来批量合成）。配置页点「搭建」，也可以直接执行 %s。"
                % ("、".join(missing), os.path.join("tts_service", "setup_env.py")))
    return ("依赖装齐了，模型权重还没下（两个变体合计约 7.7 GB）。配置页点「搭建」，"
            "也可以直接执行 %s。" % os.path.join("tts_service", "setup_env.py"))


def provision(log=None, should_stop=None):
    """搭建（或补齐）本地语音环境：建环境 → 装依赖 → 下模型 → 自检。

    这是「用户选了 Qwen3-TTS 之后该发生的事」，所以它是**配置阶段的动作**，
    不是合成阶段的前置检查。实现只有 tts_service/setup_env.py 一份，这里把它
    跑起来，输出逐行转给调用方——界面拿它当进度日志，不另写一套。

    should_stop 是个无参函数，返回真就中止。这件事必须能停：里面有 4 GB 的
    下载，没停的办法等于让人干等。
    """
    log = log or (lambda m: None)
    script = os.path.join(_SERVICE_DIR, "setup_env.py")
    if not os.path.exists(script):
        raise TTSError("找不到搭建脚本：%s" % script)
    proc = subprocess.Popen([sys.executable, script],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, cwd=_SERVICE_DIR)
    try:
        for line in proc.stdout:
            if should_stop and should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log("已中止。装到一半的包会留着，重跑时 pip 不会重装。")
                raise TTSError("已中止。")
            line = line.rstrip()
            if line:
                log(line)
        proc.wait()
    finally:
        if proc.stdout:
            proc.stdout.close()
    if proc.returncode != 0:
        raise TTSError("搭建没有完成（返回码 %s）。上面的日志里有原因。"
                       % proc.returncode)
    return local_state()


def _offline_reason(url, why):
    """连不上时，先分清到底是哪一态（服务探活用）。

    这几种情况用户要做的完全不同：环境没建的得去搭，包没装的得接着装，模型没下
    的得等下完，只是没在跑的才真的什么都不用做——说错了就是让人白忙一场。
    """
    st = tts_setup.state()
    if [k for k in _GAPS if not st[k]["ok"]]:
        return _state_message(st)
    return ("本地语音服务没在跑（%s）：%s。合成时会自动拉起，不用手动开。"
            % (url, why))


def _install_problem():
    """拉起之前兜一道。配置阶段已经搭过了，这是防有人跳过那一步。"""
    st = tts_setup.state()
    if [k for k in _GAPS if not st[k]["ok"]]:
        return _state_message(st)
    return ""


def _kill(proc):
    """停掉一个进程，先礼后兵。进程已不在就什么都不做。"""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _wait_ready(cfg, proc, timeout=90):
    """等服务应答健康接口。返回 (ok, 说明)。

    服务启动本身只绑端口、不加载权重（模型懒加载），所以通常几秒就应答；
    上限给足是因为同一台机器上可能正忙着别的重活。
    """
    deadline = time.time() + timeout
    last = "还没应答"
    while time.time() < deadline:
        ok, msg, _ = service_health(cfg, timeout=2)
        if ok:
            return True, msg
        last = msg
        if proc is not None and proc.poll() is not None:
            return False, "服务进程起来后立刻退出了（退出码 %s）" % proc.returncode
        time.sleep(0.7)
    return False, "等了 %.0f 秒仍未就绪（%s）" % (timeout, last)


_JOB_KILL_ON_CLOSE = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
_JOB_EXTENDED_LIMIT_INFO = 9  # JobObjectExtendedLimitInformation


def _job_assign(proc, log=None):
    """Windows：把服务进程挂进一个随主进程存亡的 Job 对象。

    服务是独立进程（自己的隐藏控制台，没有 Job 绑定时父进程死了它照活），
    主程序若被强杀（关窗、崩溃、任务管理器），它会带着已加载的权重变孤儿，
    几个 G 显存一直占着。挂进 Job 并设 KILL_ON_JOB_CLOSE 后，内核在收走
    主进程时自动关闭 Job 句柄，一并终止 Job 里整棵进程树（venv 启动器拉起
    的真身解释器自动继承成员资格）——显存必还，不留孤儿。

    返回 Job 句柄。非 Windows 返回 None；内核调用失败也返回 None（行为退回
    从前，只少这层保险，不挡合成）。句柄的关闭交给 _job_close。
    """
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXT_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT_LIMIT()
    info.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
    if not k32.SetInformationJobObject(
            job, _JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info),
            ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    ph = getattr(proc, "_handle", None)
    if not ph:
        # Popen 没给出句柄就自己开一个（SET_QUOTA|TERMINATE 是挂 Job 的推荐权限）。
        ph = k32.OpenProcess(0x0100 | 0x0001, False, proc.pid)
        if not ph:
            k32.CloseHandle(job)
            return None
    if not k32.AssignProcessToJobObject(job, ctypes.c_void_p(int(ph))):
        k32.CloseHandle(job)
        return None
    if log:
        log("服务已挂进 Job 对象：主程序退出时内核会自动回收服务进程与显存")
    return job


def _job_close(job):
    """关掉 Job 句柄。KILL_ON_JOB_CLOSE 生效时，句柄一关内核清掉整棵进程树。

    正常收工路径先 _kill(proc) 再关句柄，双保险；句柄无效或非 Windows 什么都不做。
    """
    if job and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(job)


def _spawn_service(cfg, log=None):
    """就地拉起本地 TTS 服务，等到健康接口应答。返回 (Popen, Job句柄或None)。"""
    log = log or (lambda m: None)
    svc = os.path.join(ROOT, "tts_service")
    serve = os.path.join(svc, "serve.py")
    py = venv_python()
    if not os.path.exists(serve):
        raise TTSError("找不到本地语音服务本体：%s" % serve)
    if not py:
        # 配置阶段没搭过。这里不替用户装——4.8 GB 的安装不该在合成中途开跑，
        # 而且那个动作的进度得摆在配置页上给人看。
        raise TTSError(_state_message(tts_setup.state()))
    problem = _install_problem()
    if problem:
        # 包没装 / 模型没下这两态起不来，而且用户要做的动作不一样，逐态说清。
        raise TTSError(problem)
    host = cfg.get("tts.qwen3tts_host") or DEFAULT_SERVICE_HOST
    port = int(cfg.get("tts.qwen3tts_port") or DEFAULT_SERVICE_PORT)
    logf = os.path.join(svc, "_serve.log")

    # 服务要活过这一次合成：独立进程组、不吃父进程的标准输入、输出落盘。
    # 用 CREATE_NO_WINDOW（隐藏控制台）而不是 DETACHED_PROCESS：DETACHED 下
    # 服务自身没有控制台，venv 启动器（Scripts\python.exe）再拉真身解释器时
    # 不透传这个标志，真身发现自己没台可继承就自建一个新控制台——黑窗从合成
    # 开工一直挂到收工。隐藏控制台则全链路都有台可继承，谁也弹不出来。
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log("本地语音服务未在线，已拉起（%s:%d）" % (host, port))
    fh = open(logf, "ab")
    try:
        proc = subprocess.Popen([py, serve, "--host", host, "--port", str(port)],
                                cwd=svc, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    finally:
        fh.close()
    job = _job_assign(proc, log)

    ok, msg = _wait_ready(cfg, proc)
    if not ok:
        _kill(proc)
        _job_close(job)
        raise TTSError("本地语音服务没能就绪：%s\n服务日志：%s" % (msg, logf))
    log("本地语音服务已就绪 · %s" % msg)
    return proc, job


def acquire_service(cfg, log=None):
    """合成开工：确保本地服务在线。引用计数 +1，与 release_service 配对。

    批量合成时由外层包一次即可 —— 内层各期只是把计数加上去，最后一个退出来
    才轮到停服务，免得每一期都重启一遍（每次重启都要重新加载权重）。
    """
    log = log or (lambda m: None)
    with _SERVICE_LOCK:
        _SERVICE["refs"] += 1
        if _SERVICE["owned"]:
            return
        if service_health(cfg)[0]:
            # 已经有服务在跑（不管是用户开的还是上一批留下的），用就是了，别动它。
            return
        _SERVICE["proc"], _SERVICE["job"] = _spawn_service(cfg, log)
        _SERVICE["owned"] = True


def release_service(log=None):
    """合成收工：引用计数 -1。归零且服务是自己起的，才停掉。"""
    log = log or (lambda m: None)
    with _SERVICE_LOCK:
        _SERVICE["refs"] = max(0, _SERVICE["refs"] - 1)
        if _SERVICE["refs"] or not _SERVICE["owned"]:
            return
        proc = _SERVICE["proc"]
        job = _SERVICE["job"]
        _SERVICE["proc"] = None
        _SERVICE["job"] = None
        _SERVICE["owned"] = False
    _kill(proc)
    _job_close(job)
    log("本地语音服务已停止，显存已归还")


def service_owned():
    """当前跑着的服务是不是我们拉起来的（界面状态用）。"""
    with _SERVICE_LOCK:
        return bool(_SERVICE["owned"])


# ------------------------------------------------------------------ 音色表
def _atomic_write(path, obj):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_cache():
    if os.path.exists(VOICE_CACHE):
        try:
            with open(VOICE_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def service_kind(cfg, timeout=6):
    """本地服务此刻加载的是哪个变体：base / custom_voice / 空串。

    界面据此决定「音色」那一栏长什么样：内置音色变体给一个下拉，Base 变体给
    本项目的音色档案 —— 后者没有音色可挑，只有一段参考音频。
    服务不在线时返回空串，不报错：这只是决定画法的提示，不是可用性判据。
    """
    try:
        url = service_url(cfg, "/health")
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return str((json.loads(r.read().decode("utf-8")) or {})
                       .get("model_kind") or "")
    except Exception:  # noqa: BLE001
        return ""


def _service_voices(cfg):
    """向本地服务枚举可选的内置音色名。服务不在就报错——不拿写死的假清单顶包。

    Base 变体下这个清单**本来就是空的**：那种模型没有内置音色，音色来自项目里
    的参考音频。空清单不是故障，所以这里不报错、如实返回空；界面据
    `service_kind()` 判断该显示音色下拉还是音色档案。
    """
    url = service_url(cfg, "/speakers")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise TTSError("拿不到音色清单（%s）：%s" % (url, e))

    detail = data.get("detail") or [{"name": n} for n in (data.get("speakers") or [])]
    ref_based = bool(data.get("need_reference"))
    if not detail and ref_based:
        # Base 变体：合成走参考音频，没有「合成音色」可选。这个清单换成内置音色名，
        # 因为它此刻的意义是「用哪个内置音色去录本项目的参考音频」。界面据
        # ref_based 把那一行文案改掉，免得让人以为选了它就直接换音色。
        detail = data.get("builtin") or []
    if not detail:
        raise TTSError("本地语音服务没有返回任何音色。")

    voices = []
    for s in detail:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        lang = s.get("language") or ""
        desc = s.get("desc") or ""
        label = "%s（%s）" % (name, desc) if desc else name
        if lang and lang != "Chinese":
            label = "%s［%s］" % (label, lang)
        voices.append({"name": name, "label": label,
                       "language": lang, "note": desc,
                       "ref_based": ref_based})
    return voices


def list_voices(engine="edge", refresh=False, cfg=None):
    """返回可用音色列表。结果带本地缓存。"""
    cache = _read_cache()
    if not refresh and cache.get(engine, {}).get("voices"):
        return cache[engine]["voices"]

    if engine in LOCAL_ENGINES:
        voices = _service_voices(cfg)
        cache[engine] = {"voices": voices, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        _atomic_write(VOICE_CACHE, cache)
        return voices

    try:
        import edge_tts
    except ImportError:
        raise TTSError("未安装 edge-tts（pip install edge-tts）。")

    async def _run():
        return await edge_tts.list_voices()

    try:
        raw = asyncio.run(_run())
    except Exception as e:
        raise TTSError("获取音色列表失败：%s" % e)

    voices = []
    for v in raw or []:
        loc = (v.get("Locale") or "").lower()
        if not loc.startswith("zh"):
            continue
        short = v.get("ShortName") or ""
        gender = v.get("Gender") or ""
        friendly = ""
        for tag in (v.get("VoiceTag") or {}).get("VoicePersonalities") or []:
            friendly += tag
        voices.append({
            "name": short,
            "label": "%s（%s%s）" % (short, "女" if gender == "Female" else "男" if gender == "Male" else "",
                                    "，" + loc if loc else ""),
            "gender": gender,
            "locale": loc,
            "note": friendly,
        })
    voices.sort(key=lambda x: (x["locale"], x["name"]))
    if voices:
        cache[engine] = {"voices": voices, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        _atomic_write(VOICE_CACHE, cache)
    return voices


# ------------------------------------------------------------------ 单句合成
def _edge_synth(text, voice, speed, sample_rate):
    import edge_tts
    rate = "%+d%%" % int(round((float(speed) - 1.0) * 100))

    async def _run():
        comm = edge_tts.Communicate(text, voice, rate=rate)
        buf = b""
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                buf += chunk["data"]
        return buf

    mp3 = asyncio.run(_run())
    if not mp3:
        raise TTSError("Edge-TTS 返回空音频。")
    r = subprocess.run(
        [ffmpeg_bin(), "-y", "-i", "pipe:0", "-f", "wav", "-ac", "1",
         "-ar", str(sample_rate), "pipe:1"],
        input=mp3, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise TTSError("mp3→wav 转换失败：%s"
                       % (r.stderr or b"").decode("utf-8", "replace")[-300:])
    return r.stdout


# ------------------------------------------------------------------ 音色档案
# 本地引擎走 Base 变体：音色不在模型里，在一段参考音频里。这段音频是**项目资产**，
# 落在 `音色/<角色>/`，由 tts_service/make_voice.py 一次性录好。此后整期读的都是
# 那份文件 —— 换项目不会互相影响，要有意保持一致就把上个项目那份复制过来。

def voice_profiles(out_dir):
    """读项目现有的音色档案，返回 {"A": {"wav":…, "text":…}, …}。

    只认「波形 + 转录」两件齐的：ICL 模式要拿转录当示例台词，缺了它参考音频的
    内容会串进结果。宁可当它没有、重新录一份，也不要拿半份档案去合成。
    """
    have = {}
    for role in ("A", "B"):
        wav = layout.voice_ref_file(out_dir, role)
        txt = layout.voice_text_file(out_dir, role)
        if not (os.path.isfile(wav) and os.path.isfile(txt)):
            continue
        try:
            with open(txt, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if text and os.path.getsize(wav) > 1024:
            have[role] = {"wav": wav, "text": text}
    return have


def ensure_voice_profiles(out_dir, voices, cfg, log=None, force=False):
    """备齐本项目的角色音色档案，缺谁录谁。

    放到合成这一步而不是立项那一步，理由是显存：录档案要独占跑一次内置音色模型
    （约 3.4GB），而立项时 LM Studio 通常正开着；到合成前本来就要腾显存，顺路做掉
    不额外制造一次冲突。

    **不做任何选择交互**：用哪个内置音色由项目自己那套 qwen_voice_a/b 决定（已经
    通过 apply_to_config 叠进 cfg），参考文案是脚本里固定的那段**混合三句型**
    （陈述 + 疑问 + 惊叹各一句，见 make_voice.REF_TEXTS —— Base 克隆整条继承
    ref 的韵律先验，混合 ref 让输出全局更生动；念全校验由工具自己兜底）——
    于是在**同一设备、同一后端**下，同一套音色名在任何项目里录出来的音频
    逐字节相同，不同音色名则自然不同。

    这个前提不能省：`pick_device()` 在显存不足时会静默退 CPU，而 CPU 路径出的
    是另一条波形（实测同一套输入，GPU 得 sha256[:16] 713bab2d382ab0fd / F0
    228.6Hz，CPU 得另一条且 F0 偏走）。所以本函数要排在腾显存之后调用，让
    make_voice.py 拿得到 GPU；profile.json 里记了 `device`，事后可对账。
    要跨项目强一致、不依赖设备，用 `make_voice.py --from` 复制文件。
    """
    log = log or (lambda m: None)
    have = voice_profiles(out_dir)
    need = [r for r in ("A", "B") if force or r not in have]
    if not need:
        log("音色档案已就绪（%s）" % "、".join(
            "%s 角 %.1f 秒" % (r, os.path.getsize(have[r]["wav"]) / 48000.0)
            for r in sorted(have)))
        return have

    py = venv_python()
    if not py:
        raise TTSError(
            "要录角色音色档案，但本地语音环境还没建。到配置页点一次「搭建本地"
            "语音环境」，或手动执行 %s。" % os.path.join("tts_service", "setup_env.py"))
    script = os.path.join(_SERVICE_DIR, "make_voice.py")
    if not os.path.isfile(script):
        raise TTSError("缺少音色档案工具：%s" % script)

    log("本项目缺 %s 角的音色档案，现在录一次（约半分钟，只录这一次）"
        % "、".join(need))
    cmd = [py, script, "--project-dir", out_dir,
           "--voice-dir", layout.DIR_VOICE, "--json",
           "--role", ("both" if len(need) == 2 else need[0])]
    va = str(voices.get("A") or "").strip()
    vb = str(voices.get("B") or "").strip()
    if va:
        cmd += ["--voice-a", va]
    if vb:
        cmd += ["--voice-b", vb]
    if force:
        cmd.append("--force")

    r = subprocess.run(cmd, capture_output=True, cwd=_SERVICE_DIR)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        raise TTSError("录角色音色档案失败：%s"
                       % (err.strip()[-400:] or out.strip()[-400:]))

    # 工具的 JSON 里那句 error 也要看 —— 它自己捕了异常，退出码同样是 1，
    # 但失败原因在 stdout 而不在 stderr。
    if '"error"' in out:
        raise TTSError("录角色音色档案失败：%s" % out.strip()[-400:])

    got = voice_profiles(out_dir)
    for role in need:
        rec = got.get(role)
        if not rec:
            raise TTSError("录完 %s 角的音色档案仍未落盘，检查 %s 目录。"
                           % (role, layout.voice_dir(out_dir)))
        log("%s 角音色档案已就绪（%.1f 秒）"
            % (role, os.path.getsize(rec["wav"]) / 48000.0))
        _warn_if_recorded_on_cpu(out_dir, role, log)
    return got


def _warn_if_recorded_on_cpu(out_dir, role, log):
    """录参考音频时跑了 CPU 就在主程序日志里点名。

    make_voice.py 自己会吼一声，但那句走的是它的 stderr —— 主程序只读它的
    stdout，用户在界面日志里看不到。而这条又必须让人看见：CPU 与 GPU 出的是
    两条不同的波形，音色档基准已经和别的项目不一样了，不说是查不出来的。
    """
    path = layout.voice_profile_file(out_dir, role)
    try:
        with open(path, encoding="utf-8") as f:
            device = str((json.load(f) or {}).get("device") or "")
    except (OSError, ValueError):
        return
    if device.startswith("cpu"):
        log("注意：%s 角的参考音频是跑 CPU 录出来的，与 GPU 路径不是同一条波形，"
            "音色基准已与其他项目不同。要一致，先腾空显存重录，或用「继承」"
            "从别的项目复制。" % role)


def _service_synth(text, voice, speed, cfg, degree=None, ref=None,
                   seed_offset=0, temperature=None):
    """调用本地 TTS 服务。采样率不硬编码；变速走 atempo（保持音高）。

    语速为什么不交给服务端：服务端也能吃自然语言调语速，但那是概率性的；
    本地 atempo 是确定性的，还能保证两条引擎的变速语义一致。所以 speed 本地做。

    `degree`（文体情绪档位）原样带下去：这一层不认识"文体"，只负责把值送到
    拼措辞的那一处，免得档位的判断在三个模块里各写一遍。

    `seed_offset` / `temperature` 只有音色体检的修复重试会传：前者换一个
    确定性的抽样点（+1/+2/+3），后者收窄抽样分布。缺省两者都不进请求体，
    与既往行为逐比特一致。

    **不收 `emotion`（v0.27.0 硬隔离）**：脚本行里的语篇标签止步于脚本层，
    请求体里永远没有 emotion 键——服务端的 instruct 判据（`if emotion and …`）
    因此恒为假，instruct 路径彻底死透，不靠任何调用方自觉。

    `ref` 是本项目的角色音色档案 `{"wav": …, "text": …}`。有它就带下去走音色克隆：
    服务端此刻的类型由这个文件决定，`voice` 只作为日志里的名字。转录文本必须一起
    给 —— ICL 模式拿它当参考音频的「台词」，缺了它参考内容会渗进结果。
    """
    url = service_url(cfg, "/tts")
    body = {"text": text, "voice": voice, "speed": 1.0}
    if degree:
        body["degree"] = degree
    if ref:
        body["ref_audio"] = ref["wav"]
        body["ref_text"] = ref["text"]
    if seed_offset:
        body["seed_offset"] = int(seed_offset)
    if temperature is not None:
        body["temperature"] = float(temperature)
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        # 首次请求要加载权重；显存不足退 CPU 时一句可能跑几分钟，上限给足
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body).get("error") or body
        except Exception:  # noqa: BLE001
            err = body
        raise TTSError("本地 TTS 服务合成失败：%s" % str(err)[-400:])
    except Exception as e:  # noqa: BLE001
        raise TTSError("本地 TTS 服务调用失败：%s" % e)
    if not raw:
        raise TTSError("本地 TTS 服务返回空音频。")

    # 服务返回的采样率不确定（Qwen3-TTS 是 24 kHz），一律以 ffprobe 实测为准
    tmp = os.path.join(ROOT, ".cache_tts_raw.wav")
    with open(tmp, "wb") as f:
        f.write(raw)
    try:
        filters = []
        if abs(float(speed) - 1.0) > 1e-3:
            filters.append("atempo=%.4f" % max(0.5, min(2.0, float(speed))))
        filters.append("aresample=%d" % int(cfg.get("audio.sample_rate", 44100)))
        filters.append("aformat=channel_layouts=mono")
        out = tmp + ".out.wav"
        r = subprocess.run(
            [ffmpeg_bin(), "-y", "-i", tmp, "-af", ",".join(filters), out],
            capture_output=True)
        if r.returncode != 0:
            raise TTSError("本地 TTS 音频后处理失败：%s"
                           % (r.stderr or b"").decode("utf-8", "replace")[-300:])
        with open(out, "rb") as f:
            data = f.read()
    finally:
        for p in (tmp, tmp + ".out.wav"):
            if os.path.exists(p):
                os.remove(p)
    return data


def synth_line(text, voice, speed, cfg, degree=None, ref=None,
               seed_offset=0, temperature=None):
    """合成一句，返回 wav 字节。

    `degree` 只有本地引擎用得上（转成语气指令）；Edge 那档没有等价的入口，
    传进去也是白传，索性不接。档位是整期一个值，逐句透传。

    **没有 `emotion` 参数，这不是省事，是硬隔离（v0.27.0）**：脚本行里的
    语篇标签（追问/解释/…）是给生成侧当句型触发器用的，合成链一个字都
    不读它——脚本里带不带标签，这边的行为一模一样。instruct 路径是 v0.9.0
    判死的东西（Base+instruct 实测更差），不能让标签的回归把它顺手复活。
    语气由 Base + ICL 的参考音频决定。

    `ref`（音色档案）同理只有本地引擎用：Edge 的音色是服务端在线的，没有
    「一段参考音频」这个输入。整期用同一份，逐句透传。
    """
    engine = cfg.get("tts.engine", "edge")
    sr = int(cfg.get("audio.sample_rate", 44100))
    if engine in LOCAL_ENGINES:
        return _service_synth(text, voice, float(speed), cfg,
                              degree=degree, ref=ref,
                              seed_offset=seed_offset, temperature=temperature)
    return _edge_synth(text, voice, float(speed), sr)


# ------------------------------------------------------------------ 批量合成
# 语音服务对连续高频请求会限流，表现为“No audio was received”。
# 因此逐句之间留出间歇，失败按指数退避重试；固定短间隔重试在限流下等于没有重试。
BACKOFF_BASE = 1.5


def synthesize(script, out_dir, cfg, log=None, emotion_level=None,
               voice_root=None):
    """逐句合成。返回 {"files":[...], "durations":[...]}。

    `emotion_level` 是文体情绪档位（`paradigms.emotion_level_of` 的产物），
    整期一个值、逐句透传。它不在这层判断含义——档位怎么落到措辞上，
    只有服务端那一处说了算。调用方不传即按"不贴"走。

    `voice_root` 是**角色音色档案**该落的那棵树的根。它跟 `out_dir` 不是一回事：
    逐句语音是过程件，落在「过程/第 N 期」下、清理时删掉不心疼；音色档案是项目
    资产，要在「音色/」里跟着项目走、被下一期复用。所以调用方要把项目根单独传
    进来，不能图省事拿 out_dir 顶。不传时退回 out_dir（单集之类没有更外层时）。
    """
    log = log or (lambda m: None)
    engine = cfg.get("tts.engine", "edge")

    # 本地引擎独占显存，先把驻留的语言模型请下去。语音合成本来就用不到它们。
    if engine in LOCAL_ENGINES and bool(cfg.get("tts.unload_llm_before_synth", True)):
        unload_llm_models(log)

    audio_dir = os.path.join(out_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    refs = {}
    if engine in LOCAL_ENGINES:
        va = cfg.get("tts.qwen3tts_voice_a", "")
        vb = cfg.get("tts.qwen3tts_voice_b", "")
        # 音色档案必须在上面的腾显存**之后**准备：录档案要独占跑一次内置音色
        # 模型，而它和 LM Studio 抢显存正是那个顺序要避免的事。
        refs = ensure_voice_profiles(voice_root or out_dir,
                                     {"A": va, "B": vb}, cfg, log)
    else:
        va = cfg.get("tts.voice_a", "")
        vb = cfg.get("tts.voice_b", "")

    retries = max(1, int(cfg.get("tts.max_retries", 4)))
    # 只有走远端服务时才需要节流；本地服务不受限流影响
    throttle = float(cfg.get("tts.throttle_seconds", 0.4)) if engine == "edge" else 0.0

    files, durations = [], []
    total = len(script)
    for i, item in enumerate(script):
        speaker = item.get("speaker", "A")
        voice = va if speaker == "A" else vb
        # 本角色自己的那一份音色档案。整期就是它，不逐句换 —— 换一次就等于换人。
        ref = refs.get(speaker)
        speed = float(cfg.get("tts.speed_a" if speaker == "A" else "tts.speed_b", 1.0))
        text = item.get("text", "")
        # 语篇标签不读：emotion 是生成侧的句型触发器，合成链硬隔离
        # （见 synth_line 的说明）。脚本里带不带标签，这边行为一模一样。
        path = os.path.join(audio_dir, "%04d_%s.wav" % (i, speaker))

        if i and throttle > 0:
            time.sleep(throttle)

        for attempt in range(retries):
            try:
                data = synth_line(text, voice, speed, cfg,
                                  degree=emotion_level, ref=ref)
                with open(path, "wb") as f:
                    f.write(data)
                dur = probe_duration(path)
                if dur <= 0:
                    raise TTSError("合成音频时长为 0")
                files.append(path)
                durations.append(dur)
                item["actual_seconds"] = round(dur, 3)
                break
            except Exception as e:
                if attempt == retries - 1:
                    raise TTSError("%s 合成失败（已重试 %d 次）：%s"
                                   % (os.path.basename(path), retries, e))
                wait = BACKOFF_BASE * (2 ** attempt)
                log("%s 第 %d 次失败，%.1f 秒后重试：%s"
                    % (os.path.basename(path), attempt + 1, wait, e))
                time.sleep(wait)

        if (i + 1) % 5 == 0 or i + 1 == total:
            log("合成 %d/%d 句" % (i + 1, total))

    # 音色体检：全部句子落地后统一判定、点名修复（见 timbre_repair 的说明）。
    # 修复改写的是「过程/第 N 期/audio」里的句文件，返回的新时长回填给
    # 字幕与视频——下游拿到的从波形到时间轴都是修复后的。
    rep = timbre_repair(files, script, cfg, log=log,
                        emotion_level=emotion_level,
                        voice_root=voice_root or out_dir, refs=refs,
                        report_path=os.path.join(out_dir, "timbre_repair.json"))
    if rep:
        for i, dur in rep["changed"].items():
            durations[i] = dur

    return {"files": files, "durations": durations, "audio_dir": audio_dir}


# ------------------------------------------------------------------ 音色体检
# 全部单句合成完之后跑一遍：每个角色以自己全体句子为基线（F0 中位、谱质心、
# 响度、存在感频带、低频占比、基音抖动六维），逐句过三条判据，任一命中即
# 点名，按 +1/+2/+3 换种子重出，候选必须三条全过才算修复，首次过线即停。
#
# 三条判据全部来自人工试听定标，各管一族病：
# ① 四维严苛分（谱心 1.0 + 音高 0.8 + 响度 0.4 + 存在感 0.5 单边，4.25）：
#    2c 六句定标 + 2da 0054 补存在感维（频谱形状重分布，谱心失明）；
# ② 暗淡子分（F0 下偏 + 谱心变暗 + 0.6×基音抖动，均单边，4.9）：ep3 58 句
#    a2 候选揪出——F0 向下还不稳 = 病态嗓（发虚发哑），四维分 2.82 照样过线；
# ③ 身份下限（F0 ≤ -2.4σ；或 F0 ≤ -2.0σ 且低频占比 ≥ +1.5σ）：主人复听
#    15 个耳标样本定出——大美是单薄女高音，「音高掉了 + 低频反而饱满」=
#    滑向中音、不是本人（a2 像"小美"案）；掉了但依然单薄 = 只是嗓子累，
#    放行（2c-0169：F0 -2.26σ 但低频 +0.09σ，可接受）。04 vs 05 是铁证：
#    同样 F0 -2.26σ，低频 +2.27σ 的判坏、+0.09σ 的放行。
# 15 个耳标样本（9 坏 6 好）三规则合验 15/15，无一漏无一冤。
#
# 位置为什么在全部句子之后：判定要拿角色全体做基线，逐句进行中基线不存在；
# 修完才拼整期，后续工序（拼接 / 字幕 / 视频）拿到的就是修复后的最终波形。
# 修复是确定性的：第几次重试对应哪个种子是固定的，整期重跑逐比特一致。

_TIMBRE_MIN_BASELINE = 10   # 角色基线至少要这么多句，σ 才稳得住


def _timbre_features(path):
    """单句六维特征：F0 中位（自相关）、谱质心中位、响度（ffmpeg RMS dB）、
    存在感频带（2.4-4.8kHz 能量占比）、低频占比（<300Hz 能量份额）、
    基音抖动（jitter，有声帧 F0 相对差均值 %）。

    测量口径与定标探针完全一致——基线就是拿这套口径量的，换口径等于换尺子，
    分数全体失真。存在感与低频占比必须在降采样之前的原始采样率上量：降到
    16k 后 Nyquist 只有 8k，带内功率谱形状会变；占比对比整段功率谱，
    幅度归一（响度另算）。
    """
    import numpy as np

    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if ch > 1:
        x = x[::ch]

    # 频带占比：原始采样率上整段（≤20s）加 hanning 窗的功率谱。
    # 存在感 = 2.4-4.8kHz 份额（刺耳方向）；低频 = <300Hz 份额（丰满方向，
    # 身份判据用：大美是单薄女高音，低频鼓起来就是滑向中音）。
    pres = s300 = None
    L = min(len(x), sr * 20)
    if L > 3:
        seg = x[:L] * np.hanning(L)
        spec = np.abs(np.fft.rfft(seg)) ** 2
        freqs = np.fft.rfftfreq(L, 1.0 / sr)
        tot = spec.sum() + 1e-12
        pres = float(spec[(freqs >= 2400) & (freqs < 4800)].sum() / tot)
        s300 = float(spec[freqs < 300].sum() / tot)

    if sr > 16000:
        step = int(round(sr / 16000.0))
        x = x[::step]
        sr = sr // step

    # F0 自相关：帧 50ms / 跳 25ms，音高带 60~420Hz，相关峰 0.35 起信
    frame, hop = int(0.05 * sr), int(0.025 * sr)
    lag_min, lag_max = int(sr / 420.0), int(sr / 60.0)
    f0s, rmss = [], []
    for s in range(0, len(x) - frame, hop):
        w = x[s:s + frame]
        r = float(np.sqrt(np.mean(w * w)))
        rmss.append(r)
        wc = w - w.mean()
        if np.sqrt(np.mean(wc * wc)) < 1e-4:
            f0s.append(np.nan)
            continue
        ac = np.correlate(wc, wc, "full")[frame - 1:]
        if ac[0] <= 0:
            f0s.append(np.nan)
            continue
        ac = ac / ac[0]
        seg = ac[lag_min:lag_max]
        k = int(np.argmax(seg))
        if seg[k] < 0.35:
            f0s.append(np.nan)
            continue
        f0s.append(sr / float(lag_min + k))
    f0s = np.array(f0s, dtype=float)
    rmss = np.array(rmss, dtype=float)
    good = f0s[~np.isnan(f0s) & (rmss > np.nanmedian(rmss[rmss > 0]) * 0.3)]
    f0_med = float(np.median(good)) if len(good) else None
    # 基音抖动：相邻有声帧 F0 的相对差均值（%）。病态嗓「发虚发哑」的
    # 声源层指标——暗淡子分的第三项，与 F0 同一套自相关机制，零新增依赖。
    jit = (float(np.mean(np.abs(np.diff(good)) / (good[:-1] + 1e-9)) * 100)
           if len(good) >= 2 else None)

    # 谱质心：1024 点窗 / 512 跳，取中位
    nfft, hopc = 1024, 512
    cents = []
    for s in range(0, len(x) - nfft, hopc):
        w = x[s:s + nfft] * np.hanning(nfft)
        mag = np.abs(np.fft.rfft(w))
        if mag.sum() <= 0:
            continue
        freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
        cents.append(float((mag * freqs).sum() / mag.sum()))
    cent = float(np.median(cents)) if cents else None

    # 响度：ffmpeg astats 的整体 RMS level dB（与定标探针同款）
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path,
         "-af", "astats", "-f", "null", "-"], capture_output=True)
    m = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)",
                   r.stderr.decode("utf-8", "replace"))
    rms_db = float(m[-1]) if m and m[-1] != "-inf" else None
    return f0_med, cent, rms_db, pres, s300, jit


def _timbre_score(row, base):
    """感知加权分。谱心只计变暗方向（变亮不出戏），音高与响度取绝对偏离，
    存在感频带只计刺耳方向（占比偏低是音色自然差异，不点名）。

    权重来自人工试听定标：0233 响度偏移全场最重仍「可接受」、0157 谱心一维
    断崖就「非常明显」——响度是最弱的感知维度，谱心最强；2da 0054 证明频谱
    形状畸变独立于谱心可闻，存在感带以 0.5 计入。
    """
    import numpy as np

    s = 0.0
    if row["cent"] is not None:
        s += (base["cent_mu"] - row["cent"]) / (base["cent_sd"] or 1.0)
    if row["f0"] is not None:
        s += 0.8 * abs(row["f0"] - base["f0_mu"]) / (base["f0_sd"] or 1.0)
    if row["rms"] is not None:
        s += 0.4 * abs(row["rms"] - base["rms_mu"]) / (base["rms_sd"] or 1.0)
    if row.get("pres") is not None:
        s += 0.5 * max(0.0, (row["pres"] - base["pres_mu"])
                       / (base["pres_sd"] or 1.0))
    return float(s)


def _dull_score(row, base):
    """暗淡子分：F0 下偏 + 谱心变暗 + 0.6×基音抖动，全部单边只计恶化方向。

    来自 ep3 58 句 a2 候选的定标：四维总分 2.82 照样「明显有问题」——F0 向下
    且基音发抖 = 病态嗓。阈值 4.9 骑在 15 个耳标样本的分离带正中（可接受区
    最高 4.51，坏区最低 5.42）。
    """
    import numpy as np

    s = 0.0
    if row["f0"] is not None:
        s += max(0.0, (base["f0_mu"] - row["f0"]) / (base["f0_sd"] or 1.0))
    if row["cent"] is not None:
        s += max(0.0, (base["cent_mu"] - row["cent"]) / (base["cent_sd"] or 1.0))
    if row.get("jit") is not None:
        s += 0.6 * max(0.0, (row["jit"] - base["jit_mu"])
                       / (base["jit_sd"] or 1.0))
    return float(s)


def _identity_flag(row, base, deep, full, s300_cap):
    """身份下限判据：音高掉了且低频鼓起来 = 滑向中音，不是本人。

    大美是单薄女高音（主人定标原话）。两种抓法：
    - F0 掉破 deep（-2.4σ）——掉这么深，单薄与否都是别人；
    - F0 掉过 full（-2.0σ）且低频占比 ≥ +1.5σ——「哪怕女高音但低频更丰满」，
      正是主人对 a2「反而有点像小美」的描述。
    掉了但依然单薄 = 嗓子累，放行（2c-0169：F0 -2.26σ、低频 +0.09σ）。
    """
    import numpy as np

    if row["f0"] is None or row.get("s300") is None:
        return False
    f0z = (base["f0_mu"] - row["f0"]) / (base["f0_sd"] or 1.0)   # 下偏为正
    lz = (row["s300"] - base["s300_mu"]) / (base["s300_sd"] or 1.0)
    return bool(f0z >= deep or (f0z >= full and lz >= s300_cap))


def _timbre_baselines(rows, log):
    """按角色建基线并给每句打分。句数不足的角色不建基线、不参与判定。"""
    import numpy as np

    baselines, scored = {}, []
    for spk in sorted({r["speaker"] for r in rows}):
        rs = [r for r in rows if r["speaker"] == spk]
        if len(rs) < _TIMBRE_MIN_BASELINE:
            log("音色体检：%s 角只有 %d 句，不足以建基线，跳过该角色"
                % (spk, len(rs)))
            continue
        base = {}
        for key, attr in (("f0", "f0"), ("cent", "cent"), ("rms", "rms"),
                          ("pres", "pres"), ("s300", "s300"), ("jit", "jit")):
            vals = np.array([r[attr] for r in rs if r[attr] is not None])
            base[key + "_mu"] = float(vals.mean()) if len(vals) else 0.0
            base[key + "_sd"] = float(vals.std()) if len(vals) else 0.0
        baselines[spk] = base
        for r in rs:
            if any(r[k] is None for k in
                   ("f0", "cent", "rms", "pres", "s300", "jit")):
                r["score"] = None
            else:
                r["score"] = round(_timbre_score(r, base), 2)
                r["dull"] = round(_dull_score(r, base), 2)
            scored.append(r)
    return baselines, scored


def _timbre_capture(r, base, threshold, dull_thr, id_deep, id_full, id_s300):
    """三条判据合验，返回命中原因列表（空列表 = 放行）。

    检测与修复候选验收共用这一把尺：原始句命中任何一条就点名重出，
    重出候选必须三条全过才准转正——检测放进去的病，验收时必须保证治好了。
    """
    reasons = []
    if r.get("score") is not None and r["score"] >= threshold:
        reasons.append("四维%.2f" % r["score"])
    if r.get("dull") is not None and r["dull"] >= dull_thr:
        reasons.append("暗淡%.2f" % r["dull"])
    if r.get("score") is not None and _identity_flag(
            r, base, id_deep, id_full, id_s300):
        f0z = (base["f0_mu"] - r["f0"]) / (base["f0_sd"] or 1.0)
        reasons.append("身份下偏%.2fσ" % f0z)
    return reasons


def _voice_fingerprint(ref):
    """角色音色档案的内容指纹，与服务端 voice_key_for 同一口径。

    种子的音色标识只认内容不认路径——保底复用别句种子时，产线要自己算出
    与服务端相同的那个 key，偏移才落在同一个种子空间里。
    """
    import hashlib

    h = hashlib.sha256()
    with open(ref["wav"], "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "ref:" + h.hexdigest()[:16]


def _line_seed(voice_key, text):
    """句子种子：sha256(音色标识 + NUL + 文本) 前 4 字节，与服务端 seed_for 同口径。"""
    import hashlib

    h = hashlib.sha256(("%s\x00%s" % (voice_key, text)).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def timbre_repair(audio_files, script, cfg, log=None, emotion_level=None,
                  voice_root=None, refs=None, report_path=None):
    """音色体检与修复。全部单句合成完之后调用，返回 None 或修复摘要。

    判定：每角色以自己全体句子为基线算 z 加权分，达到阈值即点名。
    修复：点名句依次用 seed+1/+2/+3 重出（温度不变），首次过线即停；全败
    再做保底一次——复用同角色最近一条未点名句子的种子（温度同样不动，只换
    抽样点），不管过不过都落盘这条保底；仍不过线则标记人工审。找不到可复用
    的句子时退回旧行为：保留历次里分数最低的一条并标记人工审。
    修复是确定性的：重试序号与保底参考句都由内容决定，整期重跑逐比特一致。

    返回 {"changed": {句号: 新时长}}——调用方据此刷新 durations 与
    actual_seconds，下游（拼接 / 字幕 / 视频）拿到的就是修复后的波形。
    """
    log = log or (lambda m: None)
    engine = cfg.get("tts.engine", "edge")
    if engine not in LOCAL_ENGINES or not audio_files:
        return None
    if not bool(cfg.get("tts.timbre_guard", True)):
        return None
    threshold = float(cfg.get("tts.timbre_threshold", 4.25))
    retries = max(1, int(cfg.get("tts.timbre_seed_retries", 3)))
    dull_thr = float(cfg.get("tts.timbre_dull_threshold", 4.9))
    id_deep = float(cfg.get("tts.timbre_identity_f0_deep", 2.4))
    id_full = float(cfg.get("tts.timbre_identity_f0_full", 2.0))
    id_s300 = float(cfg.get("tts.timbre_identity_s300", 1.5))

    t0 = time.time()
    rows = []
    for i, item in enumerate(script):
        try:
            f0, cent, rms_db, pres, s300, jit = _timbre_features(audio_files[i])
        except Exception as e:  # noqa: BLE001
            log("音色体检：%s 测量失败，不参与判定（%s）"
                % (os.path.basename(audio_files[i]), e))
            continue
        rows.append({"index": i, "speaker": item.get("speaker", "A"),
                     "f0": f0, "cent": cent, "rms": rms_db, "pres": pres,
                     "s300": s300, "jit": jit})
    baselines, scored = _timbre_baselines(rows, log)
    if not baselines:
        log("音色体检：没有任何角色建得起基线，跳过")
        return None
    for r in scored:
        r["why"] = _timbre_capture(r, baselines[r["speaker"]], threshold,
                                   dull_thr, id_deep, id_full, id_s300) \
            if r["score"] is not None else []
        r["flag"] = bool(r["why"])
    flagged = [r for r in scored if r["flag"]]
    log("音色体检：%d 句测完，%d 句点名（四维 %.2f / 暗淡 %.2f / 身份，%.0f 秒）"
        % (len(scored), len(flagged), threshold, dull_thr, time.time() - t0))

    voices = {"A": cfg.get("tts.qwen3tts_voice_a", ""),
              "B": cfg.get("tts.qwen3tts_voice_b", "")}
    if refs is None:
        refs = ensure_voice_profiles(
            voice_root or os.path.dirname(os.path.dirname(audio_files[0])),
            voices, cfg, log)

    changed, repairs, exhausted = {}, [], []
    # 保底第 4 招的准备：同角色全部未点名句（保底复用它们的种子）。key 缓存
    # 按角色算一次——指纹读整个参考音频，别每句都读一遍。
    ok_rows, vk_cache = {}, {}
    for r in scored:
        if r["score"] is not None and not r["flag"]:
            ok_rows.setdefault(r["speaker"], []).append(r["index"])
    for r in flagged:
        i = r["index"]
        # 日志一律用音频文件名（0054_B 这种），所见即文件所在——
        # 「第 N 句」从 1 数，与 0 基文件名永远差一位，看日志找文件要对暗号。
        fname = os.path.basename(audio_files[i])
        item = script[i]
        spk = item.get("speaker", "A")
        voice = voices.get(spk, "")
        speed = float(cfg.get("tts.speed_a" if spk == "A" else "tts.speed_b", 1.0))
        ref = refs.get(spk)
        text = item.get("text", "")
        # 候选池里始终保住分数最低的一条：修复不成就留最好的那条，不静默出货。
        # 原句先占坑当第一名；每次重出量完立即裁决——过线直接转正收工，
        # 没过线但比现有最优好就顶替它（旧的最优临时件删掉），否则当场删掉。
        best_score = r["score"]
        best_tmp = None
        fixed = None            # 过线候选的种子偏移（int）；未过线保持 None
        fixed_via = ""          # "seed"（+1/+2/+3）| "reuse"（保底复用）
        fb = None               # 保底走过后必有：{"ref_index", "seed_offset", "passed"}
        # 保底参考句：同角色离当前句最近的未点名句（并列取前面那条）。
        pool = ok_rows.get(spk) or []
        fb_ref = (min(pool, key=lambda j: (abs(j - i), j - i))
                  if pool else None)
        fb_off = None
        if fb_ref is not None:
            vk = vk_cache.setdefault(spk, _voice_fingerprint(ref))
            # 复用偏移 = 参考句种子 − 当前句种子，归一到非负；同输入必同偏移。
            fb_off = ((_line_seed(vk, script[fb_ref].get("text", ""))
                       - _line_seed(vk, text)) % (1 << 32))
        plans = ([(k, None) for k in range(1, retries + 1)]
                 + ([("reuse", None)] if fb_off is not None else []))
        for pos, (attempt, temp) in enumerate(plans, 1):
            if attempt == "reuse":
                tag = "保底·复用 %s 种子" % os.path.basename(audio_files[fb_ref])
                offset = fb_off
            else:
                tag = "seed+%d" % attempt
                offset = attempt
            data = synth_line(text, voice, speed, cfg, degree=emotion_level,
                              ref=ref, seed_offset=offset, temperature=temp)
            # 每次重试独立临时件（按循环位置编号：保底那次 seed_offset 会与
            # 首次重试相同，用 attempt 编号会撞名，撞名即误删最优候选）
            tmp = "%s.r%d.tmp" % (audio_files[i], pos)
            with open(tmp, "wb") as f:
                f.write(data)
            f0, cent, rms_db, pres, s300, jit = _timbre_features(tmp)
            cand = {"f0": f0, "cent": cent, "rms": rms_db, "pres": pres,
                    "s300": s300, "jit": jit}
            s = _timbre_score(cand, baselines[spk])
            cand["score"] = s
            cand["dull"] = _dull_score(cand, baselines[spk])
            why = _timbre_capture(cand, baselines[spk], threshold, dull_thr,
                                  id_deep, id_full, id_s300)
            log("音色体检：%s %s 重出 → 四维 %.2f 暗淡 %.2f%s（原 %.2f）"
                % (fname, tag, s, cand["dull"],
                   ("，命中：" + "、".join(why)) if why else "，三判据全过",
                   r["score"]))
            if not why:
                os.replace(tmp, audio_files[i])
                best_score = round(s, 2)
                fixed = offset
                fixed_via = "reuse" if attempt == "reuse" else "seed"
                if attempt == "reuse":
                    fb = {"ref_index": fb_ref, "seed_offset": offset,
                          "passed": True}
                if best_tmp:
                    os.remove(best_tmp)
                break
            if attempt == "reuse":
                # 保底是最后一招，不管过不过都落盘——覆盖任何历史最优候选
                os.replace(tmp, audio_files[i])
                best_score = round(s, 2)
                fb = {"ref_index": fb_ref, "seed_offset": offset,
                      "passed": False}
                if best_tmp:
                    os.remove(best_tmp)
                break
            if s < best_score:
                if best_tmp:
                    os.remove(best_tmp)
                best_score, best_tmp = s, tmp
            else:
                os.remove(tmp)
        if fixed is None and fb is None and best_tmp:
            # 重出全部没过线且没有可复用的保底：历次里分数最低的那条转正
            os.replace(best_tmp, audio_files[i])
        dur = probe_duration(audio_files[i])
        if dur > 0:
            changed[i] = dur
            item["actual_seconds"] = round(dur, 3)
        rec = {"index": i, "speaker": spk, "score0": r["score"],
               "final_score": round(best_score, 2),
               "attempt": fixed, "fixed": fixed is not None,
               "via": (fixed_via if fixed is not None
                       else ("fallback" if fb else "best")),
               "duration": dur}
        if fb:
            rec["fallback"] = fb
        if fixed is not None:
            repairs.append(rec)
            how = ("复用 %s 种子" % os.path.basename(audio_files[fb["ref_index"]])
                   if fb else "seed+%d" % fixed)
            log("音色体检：%s 已修复（%s，分数 %.2f）"
                % (fname, how, rec["final_score"]))
        else:
            exhausted.append(rec)
            if fb:
                log("音色体检：%s 保底落盘仍未过线（分数 %.2f），标记人工审"
                    % (fname, rec["final_score"]))
            else:
                log("音色体检：%s %d 次重出均未过线，保留最优一条"
                    "（分数 %.2f），标记人工审" % (fname, retries,
                                                  rec["final_score"]))

    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump({"threshold": threshold, "seed_retries": retries,
                           "fallback": "reuse_last_clean_seed",
                           "dull_threshold": dull_thr,
                           "identity": {"f0_deep": id_deep,
                                        "f0_full": id_full, "s300": id_s300},
                           "baselines": baselines,
                           "rows": scored, "repairs": repairs,
                           "exhausted": exhausted,
                           "seconds": round(time.time() - t0, 1)}, f,
                          ensure_ascii=False, indent=1)
        except OSError as e:
            log("音色体检：报告写不进 %s（%s）" % (report_path, e))
    return {"changed": changed, "repairs": repairs, "exhausted": exhausted}


def preview(voice, speed, cfg, ref=None):
    """试听：合成固定样例句，返回 base64 wav。

    `ref` 是音色档案。本地引擎走 Base 变体时它是必需的 —— 那种模型没有内置
    音色，不给参考音频根本出不了声；界面在项目里试听时把项目那份档案传进来，
    听到的就是这一期实际会用的音色。
    """
    engine = cfg.get("tts.engine", "edge")
    text = PREVIEW_TEXT
    if engine in LOCAL_ENGINES:
        data = _service_synth(text, voice, float(speed), cfg, ref=ref)
    else:
        data = _edge_synth(text, voice, float(speed), int(cfg.get("audio.sample_rate", 44100)))
    return {"audio_base64": base64.b64encode(data).decode("ascii"),
            "text": text, "bytes": len(data)}


# ------------------------------------------------------ 标准音频＝语速标尺
# 标定文本：三句固定文案。标定按钮逐句合成计时（duration_model.calibrate 要求
# 至少 3 对样本）；标准标尺音频把同样三句连成一段一次合成。同一份文本池，
# 两条路量的都是同一种「话」，口径不打架。
CALIBRATION_LINES = (
    "这是用来测量语速的第一句话，字数适中，念起来不赶也不拖。",
    "第二句稍微长一点，中间带上逗号和句号，让停顿也一起进入统计。",
    "第三句收尾，短促干脆，用来压住前两句可能带来的拖沓。",
)
STANDARD_RULER_TEXT = "".join(CALIBRATION_LINES)

# 标尺落盘是一对文件：音频 + 它自己的实测语速。json 不在场（旧版 Edge 合成的
# 残档就没有）视为「没有标尺」，下次重新生成交付——不认没有刻度的尺子。
RULER_WAV = "standard.wav"
RULER_JSON = "standard.json"


def standard_ruler(cache_dir, synth_one, carrier=None):
    """标准音频：全局唯一的一份语速标尺，合成一次、落盘、之后只读。

    尺子 ＝ 一段固定文本的原速录音 ＋ 它自己的实测语速（有效字 ÷ 秒）。
    首次调用经 `synth_one`（调用方闭包：绑定引擎、音色、参考音频与服务起停）
    一次合成为 wav，ffprobe 读出实际时长算出 k，与音频成对落盘；之后每次调用
    直接读文件，不再合成，也不随配置漂移——尺子不动，量的对象才会可比。

    `carrier`（{"engine","voice"}）是载体的身份，随 json 落盘可查：尺子是
    谁的嗓子必须说得清，不靠生成那一刻的运气。

    `synth_one` 只在标尺不存在时才被调用：缓存命中时连参考音频都不检查。
    合成、测时长任何一步失败都照实报错（TTSError 向上冒），不写半个标尺、
    不返回假音频。换算公式（播放速度 × 标尺 k ÷ 音色实测 k）里标尺自己的
    语速被精确消去，所以用谁的嗓子做载体都成立——但载体身份必须钉死，
    不许随「当前选了哪个引擎」漂移（钉在谁身上由调用方闭包定）。
    """
    wav = os.path.join(cache_dir, RULER_WAV)
    meta = os.path.join(cache_dir, RULER_JSON)
    if os.path.exists(wav) and os.path.exists(meta):
        try:
            with open(meta, "r", encoding="utf-8") as f:
                d = json.load(f)
            if float(d.get("k") or 0) > 0:
                return {"path": wav, "k": float(d["k"])}
        except (OSError, ValueError):
            pass  # 残档：json 缺损等同于没有标尺，往下走重新生成
    from .audio_engine import probe_duration_safe
    from . import duration_model
    os.makedirs(cache_dir, exist_ok=True)
    data = synth_one(STANDARD_RULER_TEXT)
    tmp = wav + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    sec = float(probe_duration_safe(tmp) or 0)
    if sec <= 0:
        os.remove(tmp)
        raise TTSError("标准音频合成后读不出时长，标尺没有落盘。")
    k = duration_model.effective_chars(STANDARD_RULER_TEXT) / sec
    os.replace(tmp, wav)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    meta_tmp = meta + ".tmp"
    meta_doc = {"k": round(k, 4), "seconds": round(sec, 3),
                "text": STANDARD_RULER_TEXT, "created": stamp}
    if carrier:
        meta_doc["carrier"] = dict(carrier)
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta_doc, f, ensure_ascii=False, indent=2)
    os.replace(meta_tmp, meta)
    return {"path": wav, "k": round(k, 4)}
