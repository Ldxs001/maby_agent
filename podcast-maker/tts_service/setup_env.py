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

"""本地语音服务（Qwen3-TTS）环境搭建 —— 全仓唯一的安装实现。

这件事有两个入口：用户双击 `setup.bat`，或者在配置页把语音引擎选成 Qwen3-TTS
之后点「搭建」。两个入口各写一遍必然走散——换了源、改了版本、加了包，总有一边
忘。所以两边都只是把本脚本跑起来，输出逐行喂给界面，实现只有这一份。

它做五件事，顺序不能换：

    1. 找一台机器上能用来建环境的 Python
    2. 用它 `python -m venv` 建出 `tts_service/.venv`
    3. 装 PyTorch 的 CUDA 构建（单独一份 requirements，源也不同）
    4. 装其余运行期依赖，以及两个 TTS 包本身
    5. 下模型权重

**为什么用独立环境，而不是装进机器上那份 Python。** torch 的 CUDA 轮子约
4.8 GB，外加一串钉死版本的包（numpy、onnxruntime、librosa…）。装进主环境会把
主程序自己的依赖一起搅动，而主程序对这件事的全部要求只是「能用」——它不需要
知道里面装了什么。独立环境还顺手把版本钉住：机器上那个 Python 将来升级，
不影响这里。

**为什么 3.11 优先。** PyTorch 的 CUDA 轮子在这份镜像上覆盖 cp310–cp314，
但周边那串包在新版 Python 上常常还没有轮子。3.11 是覆盖面最广的一档。找不到
3.11 就顺着往上找（3.12 → 3.13 → 3.14），一个都没有才报错。

用法::

    python setup_env.py              # 全流程
    python setup_env.py --check      # 只报状态，什么都不动
    python setup_env.py --skip-model # 装包但不下模型
    python setup_env.py --recreate   # 环境已存在也推倒重建
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENV = os.path.join(HERE, ".venv")

REQ_TORCH = os.path.join(HERE, "requirements-torch.txt")
REQ_TTS = os.path.join(HERE, "requirements-tts.txt")

# 两个 TTS 包本身。必须 --no-deps：它们在 PyPI 上的依赖声明里带着 gradio、
# 另外一份 onnxruntime，以及只有源码包的 sox（装它要 C 编译器）。这三样代码里
# 一个都没 import，全量装会白白拖进来几百 MB，还会在没编译器的机器上直接失败。
TTS_PACKAGES = ("faster-qwen3-tts==0.4.0", "qwen-tts-hf==0.1.1.post1")

# 要下哪两个模型。**两个都要，少一个流程就跑不通**：
#
#   CustomVoice  自带九个音色（Serena / Vivian / Uncle_Fu…）。它不做常态合成，
#                只在「给一个新项目录角色参考音频」时被加载一次，录完就退出 ——
#                一次性选型器。
#   Base         常态引擎。没有内置音色表，音色来自上一步录出来的那段参考音频。
#                整期所有句子都走它（见 serve.py 的 DEFAULT_MODEL）。
#
# 只有 CustomVoice 就量产不了，只有 Base 就录不出参考音频。两个各约 3.8 GB 磁盘，
# 但**不同时驻留显存**（见 make_voice.py 的文件头），所以 8 GB 的卡照样跑。
MODELS = ("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
          "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
# 常驻服务的那个。顺序依赖写法与 models/ 下的目录名一致（取仓库 id 的最后一段）。
SERVE_MODEL = MODELS[1]

# 「装没装」按这两件事判，和配置页探活用的是同一组名字。按目录在不在判，
# 不 import —— import torch 要好几秒，而这个判定会被反复调用。
PACKAGES = ("torch", "transformers", "onnxruntime", "faster_qwen3_tts",
            "qwen_tts", "librosa", "soundfile", "sox")

# 建环境时按这个顺序试。3.11 优先的理由见文件头。
_VERSION_FLOOR = (3, 11)
_BASE_CANDIDATES = (
    ["py", "-3.11"], ["py", "-3.12"], ["py", "-3.13"], ["py", "-3.14"], ["py", "-3"],
    ["python3.11"], ["python3.12"], ["python3.13"], ["python3.14"],
    ["python3"], ["python"],
)

_CHECK_CODE = "import sys; assert sys.version_info[:2] >= %r" % (_VERSION_FLOOR,)


# ------------------------------------------------------------------ 输出
def log(msg=""):
    """一行进度。父进程（界面）按行读，所以每行都要 flush，不能攒。"""
    print(msg, flush=True)


def _child_env():
    """子进程环境：强制 UTF-8，免得 pip 的输出在 GBK 控制台上炸成乱码。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def _run(cmd, quiet=False):
    """跑一条命令，输出逐行转出去。返回退出码。

    不接 capture_output：接了就只剩「返回码非零」，真实错误被吞掉，而装依赖
    失败的原因（版本没有轮子、连不上源）恰恰全在那几行输出里。
    """
    if not quiet:
        log("  $ %s" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", env=_child_env(), bufsize=1)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("    " + line)
    proc.wait()
    return proc.returncode


def _capture(cmd, timeout=60):
    """跑一条命令取输出，用于探测类的小调用。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return r


# ------------------------------------------------------------------ 状态
def venv_python():
    """独立环境的解释器。环境还没建时返回 None。"""
    for parts in (("Scripts", "python.exe"), ("bin", "python")):
        p = os.path.join(VENV, *parts)
        if os.path.exists(p):
            return p
    return None


def site_packages():
    """独立环境里装第三方包的地方。没建过时这个目录还不存在，也照样返回路径。"""
    cands = [os.path.join(VENV, "Lib", "site-packages")]
    lib = os.path.join(VENV, "lib")
    if os.path.isdir(lib):
        for n in sorted(os.listdir(lib)):
            if n.startswith("python3"):
                cands.append(os.path.join(lib, n, "site-packages"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


def missing_packages():
    """哪些包还没装。只看目录在不在，不 import。"""
    sp = site_packages()
    out = []
    for name in PACKAGES:
        if not (os.path.exists(os.path.join(sp, name))
                or os.path.exists(os.path.join(sp, name + ".py"))):
            out.append(name)
    return out


def model_paths():
    """每个模型落在哪、下齐没有。返回 [(模型 id, 目录, 齐否), …]。

    「齐」= 目录在且有权重文件。逐文件校验哈希太贵，也没必要 —— fetch_model
    是最后才落权重，有权重就说明那一趟走完了。
    """
    root = os.path.join(HERE, "models")
    out = []
    for mid in MODELS:
        d = os.path.join(root, mid.split("/")[-1])
        ok = False
        if os.path.isdir(d):
            try:
                ok = any(f.endswith((".safetensors", ".bin", ".pt"))
                         for f in os.listdir(d))
            except OSError:
                ok = False
        out.append((mid, d, ok))
    return out


def model_dir():
    """常驻服务那个模型的目录。它没下就返回空串。

    这里只认 SERVE_MODEL，不认「随便哪个模型在」—— 合成用的是它，
    别的模型下得再齐也替不了。
    """
    for mid, d, ok in model_paths():
        if mid == SERVE_MODEL:
            return d if ok else ""
    return ""


def state():
    """三态：环境在不在 / 包齐不齐 / 模型下没下。

    这三件事的补救动作完全不同，所以必须分开报：包没装就让人去下模型，
    白下 4 GB 也照样跑不起来。

    「模型」这一态要求**两个都齐**，因为录参考音频与量产各用一个，缺谁都不完整。
    明细在 `models` 里，界面据此说清缺的是哪一个。
    """
    py = venv_python()
    miss = missing_packages() if py else list(PACKAGES)
    mps = model_paths()
    return {
        "env": {"ok": bool(py), "path": VENV, "python": py or ""},
        "packages": {"ok": not miss, "missing": miss},
        "model": {"ok": all(ok for _, _, ok in mps), "path": model_dir(),
                  "models": [{"id": mid, "path": d, "ok": ok}
                             for mid, d, ok in mps]},
    }


def ready():
    """三态齐了才算能干活。"""
    st = state()
    return st["env"]["ok"] and st["packages"]["ok"] and st["model"]["ok"]


# ------------------------------------------------------------------ 各步
def find_base_python():
    """找一台机器上能用来建环境的 Python。返回 (命令行 list, 版本号)。

    找不到返回 (None, 原因说明)。
    """
    for cmd in _BASE_CANDIDATES:
        r = _capture(cmd + ["-c", _CHECK_CODE], timeout=30)
        if r is None or r.returncode != 0:
            continue
        v = _capture(cmd + ["-c", "import sys;print(sys.version.split()[0])"],
                     timeout=30)
        ver = (v.stdout or "").strip() if v and v.returncode == 0 else "?"
        return cmd, ver
    return None, ""


def make_venv(base, recreate=False):
    """用 `python -m venv` 建独立环境。已存在就跳过。"""
    if venv_python() and not recreate:
        log("环境已存在，跳过：%s" % VENV)
        return
    if recreate and os.path.isdir(VENV):
        log("按 --recreate 删掉旧环境：%s" % VENV)
        import shutil
        shutil.rmtree(VENV)
    log("建独立环境：%s" % VENV)
    rc = _run(base + ["-m", "venv", VENV])
    if rc != 0 or not venv_python():
        raise SystemExit("[ERROR] 建环境失败（返回码 %s）。" % rc)


def install_torch(py, pypi, torch_index, torch_fallback):
    """装 PyTorch 的 CUDA 构建。

    这一步必须单独装，而且必须把 pytorch 源当**主 index**：带 `+cu126` 这种
    本地版本号的轮子只存在于 pytorch-wheels 源；PyPI 上那个同名 torch 是
    CPU-only 构建，装上去不报错，但永远用不到 GPU。
    """
    log("装 PyTorch（CUDA 构建，源在 requirements-torch.txt 里）…")
    if _run([py, "-m", "pip", "install", "-r", REQ_TORCH,
             "--progress-bar", "off"]) == 0:
        if _has_cuda(py):
            return
        log("[WARN] 装上了，但这份 torch 没有 CUDA —— 可能是 CPU-only 构建。")
    log("主源没成，换备用源：%s" % torch_fallback)
    rc = _run([py, "-m", "pip", "install",
               "torch==2.9.1+cu126", "torchaudio==2.9.1+cu126",
               "--index-url", torch_fallback, "--extra-index-url", pypi,
               "--progress-bar", "off"])
    if rc != 0 or not _has_cuda(py):
        raise SystemExit("[ERROR] PyTorch 的 CUDA 构建没装上。检查网络后重试；"
                         "国内源见 %s" % torch_index)


def _has_cuda(py):
    r = _capture([py, "-c", "import torch;assert torch.version.cuda"], timeout=300)
    return bool(r and r.returncode == 0)


def install_deps(py):
    log("装其余运行期依赖…")
    if _run([py, "-m", "pip", "install", "-r", REQ_TTS,
             "--progress-bar", "off"]) != 0:
        raise SystemExit("[ERROR] 依赖没装完。检查网络后重试。")


def install_tts_packages(py, pypi):
    log("装两个 TTS 包本身（--no-deps，理由见文件头）…")
    cmd = [py, "-m", "pip", "install"] + list(TTS_PACKAGES) + [
        "--no-deps", "-i", pypi, "--progress-bar", "off"]
    if _run(cmd) != 0:
        raise SystemExit("[ERROR] faster-qwen3-tts / qwen-tts-hf 没装上。")


def fetch_model(py):
    log("下模型权重（两个，合计约 7.7 GB；断了重跑会续传）…")
    script = os.path.join(HERE, "fetch_model.py")
    failed = []
    for mid, _dir, ok in model_paths():
        short = mid.split("/")[-1]
        if ok:
            log("  %s 已在本地，跳过。" % short)
            continue
        log("  %s …" % short)
        if _run([py, script, "--model", mid]) != 0:
            failed.append(short)
    missing = [mid.split("/")[-1] for mid, _d, ok in model_paths() if not ok]
    if failed or missing:
        log("[WARN] 模型没下完（缺 %s）。可以重跑本脚本，它会续传；"
            "也可以单独跑 tts_service/fetch_model.py --model <仓库 id>。"
            % "、".join(failed or missing))


def self_check(py):
    """真 import 一遍，把版本打出来。这是「能不能用」的唯一判据 ——
    前面的判据都是「装没装」，只有这里回答「跑不跑得起来」。"""
    log("自检：真 import 一遍…")
    code = (
        "import torch, transformers, onnxruntime, librosa, soundfile\n"
        "import qwen_tts, faster_qwen3_tts\n"
        "print('torch', torch.__version__, 'cuda', torch.version.cuda,\n"
        "      'avail', torch.cuda.is_available())\n"
        "print('transformers', transformers.__version__)\n"
        "print('onnxruntime', onnxruntime.__version__)\n"
        "print('OK')\n")
    rc = _run([py, "-c", code], quiet=True)
    if rc != 0:
        raise SystemExit("[ERROR] 自检没过：上面的 import 有一处失败。")


# ------------------------------------------------------------------ 入口
def _load_sources():
    """取源表。全仓唯一写死下载地址的地方在 tools/sources.py。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    try:
        import sources  # noqa: PLC0415
    except ImportError:
        raise SystemExit("[ERROR] 找不到 tools/sources.py —— 下载源表不在，"
                         "这份拷贝不完整。")
    return sources


def main(argv=None):
    ap = argparse.ArgumentParser(description="搭建本地语音服务的运行环境")
    ap.add_argument("--check", action="store_true", help="只报状态，什么都不装")
    ap.add_argument("--skip-model", action="store_true", help="装包但不下模型")
    ap.add_argument("--recreate", action="store_true", help="环境已存在也推倒重建")
    args = ap.parse_args(argv)

    if args.check:
        print(json.dumps(state(), ensure_ascii=False, indent=2))
        return 0

    src = _load_sources()

    log("=" * 62)
    log("  本地语音服务（Qwen3-TTS）环境搭建")
    log("=" * 62)
    log()

    log("[1/5] 找机器上的 Python")
    base, ver = find_base_python()
    if not base:
        log("      没找到 Python %d.%d 或更新的一档。"
            % _VERSION_FLOOR)
        log("      装一个再来：https://www.python.org/downloads/")
        return 1
    log("      用 %s（%s）" % (" ".join(base), ver))
    log()

    # 环境已就绪时直接收工，不重装 —— 这套东西装一次要十几分钟。
    if ready() and not args.recreate:
        log("环境、依赖、模型都齐了，不必重装。")
        return 0

    log("[2/5] 建独立环境")
    make_venv(base, recreate=args.recreate)
    py = venv_python()
    log()

    log("[3/5] 装依赖")
    install_torch(py, src.PYPI_MIRROR, src.PYTORCH_INDEX, src.PYTORCH_INDEX_FALLBACK)
    install_deps(py)
    install_tts_packages(py, src.PYPI_MIRROR)
    log()

    log("[4/5] 下模型")
    if args.skip_model:
        log("      按 --skip-model 跳过。")
    else:
        fetch_model(py)
    log()

    log("[5/5] 自检")
    self_check(py)
    log()
    log("搭建完成。服务平时不用手动起：主程序在合成前会自动拉起，"
        "整批跑完自动停掉、归还显存。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
