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
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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
_SERVICE = {"refs": 0, "proc": None, "owned": False}


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


def _spawn_service(cfg, log=None):
    """就地拉起本地 TTS 服务，等到健康接口应答。返回 Popen。"""
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
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    log("本地语音服务未在线，已拉起（%s:%d）" % (host, port))
    fh = open(logf, "ab")
    try:
        proc = subprocess.Popen([py, serve, "--host", host, "--port", str(port)],
                                cwd=svc, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    finally:
        fh.close()

    ok, msg = _wait_ready(cfg, proc)
    if not ok:
        _kill(proc)
        raise TTSError("本地语音服务没能就绪：%s\n服务日志：%s" % (msg, logf))
    log("本地语音服务已就绪 · %s" % msg)
    return proc


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
        _SERVICE["proc"] = _spawn_service(cfg, log)
        _SERVICE["owned"] = True


def release_service(log=None):
    """合成收工：引用计数 -1。归零且服务是自己起的，才停掉。"""
    log = log or (lambda m: None)
    with _SERVICE_LOCK:
        _SERVICE["refs"] = max(0, _SERVICE["refs"] - 1)
        if _SERVICE["refs"] or not _SERVICE["owned"]:
            return
        proc = _SERVICE["proc"]
        _SERVICE["proc"] = None
        _SERVICE["owned"] = False
    _kill(proc)
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


def _service_synth(text, voice, speed, cfg, degree=None, ref=None):
    """调用本地 TTS 服务。采样率不硬编码；变速走 atempo（保持音高）。

    语速为什么不交给服务端：服务端也能吃自然语言调语速，但那是概率性的；
    本地 atempo 是确定性的，还能保证两条引擎的变速语义一致。所以 speed 本地做。

    `degree`（文体情绪档位）原样带下去：这一层不认识"文体"，只负责把值送到
    拼措辞的那一处，免得档位的判断在三个模块里各写一遍。

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


def synth_line(text, voice, speed, cfg, degree=None, ref=None):
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
                              degree=degree, ref=ref)
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
                    raise TTSError("第 %d 句合成失败（已重试 %d 次）：%s"
                                   % (i + 1, retries, e))
                wait = BACKOFF_BASE * (2 ** attempt)
                log("第 %d 句第 %d 次失败，%.1f 秒后重试：%s"
                    % (i + 1, attempt + 1, wait, e))
                time.sleep(wait)

        if (i + 1) % 5 == 0 or i + 1 == total:
            log("合成 %d/%d 句" % (i + 1, total))

    return {"files": files, "durations": durations, "audio_dir": audio_dir}


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
