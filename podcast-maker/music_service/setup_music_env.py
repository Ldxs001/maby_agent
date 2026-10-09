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

"""本地音乐服务（ACE-Step 1.5）环境搭建 —— 全仓唯一的安装实现。

入口只有一个：配置页「背景音乐」卡上的「搭建」按钮。按钮跑的就是本脚本，
进度逐行喂给界面，没有第二份安装逻辑（连状态判定都直接复用
music_gen.env_state —— 两处各写一遍迟早对「装没装」走散）。

它做六件事，顺序不能换：

    1. 拿到 ACE-Step 官方仓的代码（clone，多源兜底）
    2. 找机器上能建环境的 Python（3.12 优先）
    3. 建独立环境 `music_service/.venv`
    4. 装 PyTorch 的 CUDA 构建 + 官方仓依赖
    5. 下模型权重（官方下载器，ModelScope 自动兜底）
    6. 自检

**为什么又是一个独立环境。** 官方仓在 Windows 上钉的是
torch 2.7.1+cu128，与 TTS 栈的 2.9.1+cu126 不同版本 —— 装进同一环境必炸
一方。三套栈（主程序 / TTS / 音乐）互不知晓、互不搅动，各用各的 venv。

**为什么 3.12 优先而不是 3.11。** 官方 requirements 里 Windows 的
flash-attn 轮子 URL 只给了 cp311；3.12 下那行自动跳过，安装链干净一截。
pyproject 限定 >=3.11,<3.13，所以可选项只有这两档。

用法::

    python setup_music_env.py              # 全流程
    python setup_music_env.py --check      # 只报状态，什么都不动
    python setup_music_env.py --skip-model # 装包但不下模型
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
REPO_DIR = os.path.join(HERE, "ACE-Step-1.5")

REPO_URL = "https://github.com/ace-step/ACE-Step-1.5.git"
TARBALL_URL = "https://codeload.github.com/ace-step/ACE-Step-1.5/tar.gz/refs/heads/main"

# 官方 requirements 钉的 Windows CUDA 构建（与 TTS 栈不同版本，见文件头）。
TORCH_PIN = ("torch==2.7.1+cu128", "torchvision==0.22.1+cu128",
             "torchaudio==2.7.1+cu128")
# torch 源：国内镜像优先，官方兜底（与 TTS 栈同一套路，源事实来源见
# tools/sources.py）。cu128 各镜像实测（2026-10-07，20MB 采样）：
#   上交大 6.3 MB/s ✅ ｜ 阿里云 0.79 MB/s ｜ 腾讯无 cu128（404）
#   pytorch 官方 0.65 MB/s —— 3.2GB 要下一小时，只当最后的兜底。
TORCH_INDEXES = (
    "https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/",
    "https://download.pytorch.org/whl/cu128",
)
# PyPI 依赖源：清华（机器 pip 没配全局镜像，必须显式指）。
PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple/"

# flash-attn 轮子只在 GitHub release 上（PyPI/清华/上交大都没有），而本机
# GitHub 443 常断——实测（2026-10-07）：直连与 ghfast/mirror.ghproxy 全断，
# ghproxy.net 174 KB/s、gh-proxy.com 99 KB/s 可用。轮子 250MB，链路按快到慢试。
FLASH_ATTN_URL = ("https://github.com/sdbds/flash-attention-for-windows/releases/"
                  "download/2.8.2/flash_attn-2.8.2+cu128torch2.7.1cxx11abiFALSE"
                  "fullbackward-cp311-cp311-win_amd64.whl")
FLASH_ATTN_MIRRORS = ("https://ghproxy.net/", "https://gh-proxy.com/")

# 要下的权重：主模型（DiT turbo + VAE + 文本编码器 + 1.7B LM）与 0.6B LM。
# 主模型整仓 ~10 GB，其中 1.7B LM 本配置用不上 —— 官方下载器按整仓拉，
# 没有跳过单目录的口子；多下 3 GB 换「用官方代码自己的下载器、不重写下载
# 逻辑」，值。
LM_SUBMODEL = "acestep-5Hz-lm-0.6B"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def log(msg=""):
    """一行进度。父进程（界面）按行读，每行 flush。"""
    print(msg, flush=True)


def _child_env():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    # 官方代码用它定位 checkpoints 目录；钉在 music_service/ 下。
    env["ACESTEP_PROJECT_ROOT"] = HERE
    return env


def _run(cmd, quiet=False):
    """跑一条命令，输出逐行转出去（理由同 tts_service/setup_env._run）。"""
    if not quiet:
        log("  $ %s" % " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace",
                            env=_child_env(), bufsize=1)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log("    " + line)
    proc.wait()
    return proc.returncode


def _capture(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=_child_env())
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------------ 状态
def venv_python():
    for parts in (("Scripts", "python.exe"), ("bin", "python")):
        p = os.path.join(VENV, *parts)
        if os.path.isfile(p):
            return p
    return None


def _state():
    """状态判定只有 music_gen 一份（这里连 import 都不另写）。"""
    sys.path.insert(0, HERE)
    import music_gen  # noqa: PLC0415  —— 纯标准库模块，主 Python 可加载
    return music_gen.env_state(), music_gen.ready()


def repo_ok():
    return os.path.isfile(os.path.join(REPO_DIR, "acestep", "inference.py"))


# ------------------------------------------------------------------ 各步
def _rmtree(path):
    """Windows 硬化删除 —— 不许静默吞失败。

    git 的 .git 对象文件全是只读的，默认 rmtree 一碰就 PermissionError；
    ignore_errors=True 只会把残骸静默留下，让下一步（clone / rename）
    在几百秒下载之后才炸。所以：先解只读、再删、删完必须不存在 ——
    删不干净当场报错停手（fail-closed），绝不带病往下走。"""
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass  # 改不动的交给 rmtree 自己大声报
    shutil.rmtree(path)
    if os.path.exists(path):
        raise SystemExit("[ERROR] 旧目录删不掉：%s —— 多半被别的进程占用，"
                         "处理后重跑。" % path)


def fetch_repo():
    """拿官方仓代码。git clone 先试（最省流量），败了退 tarball 解包。
    两条路都指向 GitHub —— 都断就如实报错，不静默、不造假源。
    上次跑到一半留下的 ACE-Step-1.5-main（tarball 已解好）完整就直接
    捡来归位，不重下。"""
    if repo_ok():
        log("官方仓代码已在本地，跳过：%s" % REPO_DIR)
        return
    inner = os.path.join(HERE, "ACE-Step-1.5-main")
    if not os.path.isfile(os.path.join(inner, "acestep", "inference.py")):
        log("  试 git clone --depth 1 …")
        _rmtree(REPO_DIR)
        rc = _run(["git", "clone", "--depth", "1", REPO_URL, REPO_DIR])
        if rc == 0 and repo_ok():
            return
        log("  git 不通，退 tarball 直接下载 …")
        tgz = os.path.join(HERE, "_ace_repo.tar.gz")
        try:
            _rmtree(inner)  # 残缺的旧解包不许和新解包的混
            with urllib.request.urlopen(TARBALL_URL, timeout=120) as resp, \
                    open(tgz, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            with tarfile.open(tgz, "r:gz") as tf:
                tf.extractall(HERE)  # noqa: S202 —— 内容来自官方仓，非用户输入
        finally:
            if os.path.isfile(tgz):
                os.remove(tgz)
    # 归位。Windows 目录 rename 目标存在必炸，rename 前必须清干净 ——
    # _rmtree 删不干净会自己报错停手，不会带着残骸撞 rename。
    _rmtree(REPO_DIR)
    os.rename(inner, REPO_DIR)
    if not repo_ok():
        raise SystemExit("[ERROR] 官方仓代码没拿到（git 与 tarball 都失败）。"
                         "检查网络后重试；或手动把 ACE-Step-1.5 放到 %s。"
                         % REPO_DIR)


def find_base_python():
    """找建环境的 Python。3.12 优先（理由见文件头），3.11 次之。
    pyproject 限定 >=3.11,<3.13，其余版本装了也白装，直接不试。"""
    check = "import sys; assert (3,11)<=sys.version_info[:2]<(3,13)"
    for cmd in (["py", "-3.12"], ["py", "-3.11"],
                ["python3.12"], ["python3.11"],
                ["python3"], ["python"]):
        r = _capture(cmd + ["-c", check], timeout=30)
        if r is None or r.returncode != 0:
            continue
        v = _capture(cmd + ["-c", "import sys;print(sys.version.split()[0])"],
                     timeout=30)
        ver = (v.stdout or "").strip() if v and v.returncode == 0 else "?"
        return cmd, ver
    return None, ""


def make_venv(base):
    if venv_python():
        log("环境已存在，跳过：%s" % VENV)
        return
    log("建独立环境：%s" % VENV)
    rc = _run(base + ["-m", "venv", VENV])
    if rc != 0 or not venv_python():
        raise SystemExit("[ERROR] 建环境失败（返回码 %s）。" % rc)


def install_torch(py):
    log("装 PyTorch（CUDA 12.8 构建，官方仓 Windows 钉的版本）…")
    for idx in TORCH_INDEXES:
        log("  试源：%s" % idx)
        if _run([py, "-m", "pip", "install", *TORCH_PIN,
                 "--index-url", idx, "--progress-bar", "off"]) == 0:
            if _has_cuda(py):
                return
            log("[WARN] 装上了，但这份 torch 没有 CUDA —— 可能落到了 CPU 构建，换源重装。")
        else:
            log("  这个源没装成，换下一个 …")
    raise SystemExit("[ERROR] PyTorch 的 CUDA 构建没装上。检查网络后重试"
                     "（源顺序：上交大镜像 → pytorch 官方）。")


def _has_cuda(py):
    r = _capture([py, "-c", "import torch;assert torch.version.cuda"],
                 timeout=300)
    return bool(r and r.returncode == 0)


def _flash_installed(py):
    r = _capture([py, "-c", "import flash_attn"], timeout=120)
    return bool(r and r.returncode == 0)


def _requirements_without_flash(req_path):
    """把 flash-attn 的 GitHub 直链行从清单里滤掉，写临时清单。
    这不是静默降级——调用方必须先打出 [WARN] 说明缺席后果。"""
    out = req_path + ".noflash"
    with open(req_path, encoding="utf-8") as fh, \
            open(out, "w", encoding="utf-8", newline="") as w:
        for line in fh:
            if line.strip().startswith("flash-attn @"):
                continue
            w.write(line)
    return out


def _fetch_flash_wheel(dest):
    """flash-attn 轮子只挂 GitHub release（PyPI 源没有），GitHub 443 常断，
    走加速镜像链兜底。拿到完整轮子返回 True。"""
    sources = [FLASH_ATTN_URL] + [m + FLASH_ATTN_URL
                                  for m in FLASH_ATTN_MIRRORS]
    for src in sources:
        host = src.split("/releases/")[0].replace("https://", "")
        log("  试下载 flash-attn（250MB）：%s" % host)
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as resp, \
                    open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            if os.path.getsize(dest) > 100_000_000:  # 250MB 轮子，防镜像错页
                return True
            log("    下载不完整（%d 字节），换下一个 …" % os.path.getsize(dest))
        except Exception as exc:  # 网络/镜像失败是预期分支，换下一个源
            log("    不通：%s" % exc)
    return False


def install_deps(py, with_flash=False):
    # flash-attn 是锦上添花不是刚需：缺了它 SDPA 自动回退，功能与音质零影响，
    # 只慢一点。轮子只挂 GitHub release（PyPI 源没有）而 GitHub 443 常断——
    # 默认不折腾：直接滤掉该行继续装，明示 WARN；真想要的人 --with-flash-attn
    # 显式要，那时才走镜像链下载。
    wheel = os.path.join(HERE, "_flash_attn.whl")
    if with_flash:
        log("预装 flash-attn（--with-flash-attn 显式要求，走镜像链）…")
        if _fetch_flash_wheel(wheel):
            if _run([py, "-m", "pip", "install", wheel,
                     "--progress-bar", "off"]) == 0:
                log("  flash-attn 已装上。")
            else:
                log("[WARN] 轮子下载了但 pip 装不上，按缺席处理。")
        if os.path.isfile(wheel):
            os.remove(wheel)
    req = os.path.join(REPO_DIR, "requirements.txt")
    if not _flash_installed(py):
        log("[WARN] flash-attn 缺席（默认跳过：锦上添花的注意力加速，"
            "SDPA 自动回退，功能与音质不受影响，仅生成稍慢）。"
            "想要它重跑本脚本加 --with-flash-attn。依赖安装改用去掉该行的清单继续。")
        req = _requirements_without_flash(req)
    log("装官方仓依赖（requirements.txt，走清华 PyPI 源）…")
    if _run([py, "-m", "pip", "install", "-r", req,
             "--index-url", PYPI_INDEX,
             "--progress-bar", "off"]) != 0:
        raise SystemExit("[ERROR] 官方仓依赖没装完。检查网络后重试。")
    # nano-vllm 是仓内的本地包，requirements 里只留了注释（官方口径：
    # pip install -e acestep/third_parts/nano-vllm）。它 pyproject 把 flash-attn
    # 声明成硬依赖（GitHub 直链），但代码里是 try/except 守卫导入、自带 SDPA
    # 回退（attention.py "no flash_attn dependency"），缺席仅慢不残；其余依赖
    # torch/triton-windows/transformers/xxhash 已由 requirements 装齐——
    # 故 --no-deps 挂载，不为一个可选加速轮触雷整轮失败。
    log("装仓内本地包 nano-vllm …")
    if _run([py, "-m", "pip", "install", "-e",
             os.path.join(REPO_DIR, "acestep", "third_parts", "nano-vllm"),
             "--no-deps",
             "--progress-bar", "off"]) != 0:
        raise SystemExit("[ERROR] nano-vllm 没装上。")
    log("以 --no-deps 挂载 acestep 包本体（依赖已由 requirements 装齐）…")
    if _run([py, "-m", "pip", "install", "-e", REPO_DIR, "--no-deps",
             "--progress-bar", "off"]) != 0:
        raise SystemExit("[ERROR] acestep 包没装上。")


def fetch_model(py):
    log("下模型权重（主模型约 10 GB + 0.6B LM 约 1.2 GB；断了重跑会续传）…")
    # 官方下载器自带网络探测：Google 通走 HF、不通走 ModelScope，互为兜底
    # —— 这里不替它选源，也不重写下载逻辑。
    if _run([py, "-m", "acestep.model_downloader"]) != 0:
        log("[WARN] 主模型没下完。可以重跑本脚本续传。")
    if _run([py, "-m", "acestep.model_downloader", "--model", LM_SUBMODEL,
             "--skip-main"]) != 0:
        log("[WARN] %s 没下完。可以重跑本脚本续传。" % LM_SUBMODEL)
    st, _ = _state()
    if not st["checkpoints"]["ok"]:
        log("[WARN] 权重仍不齐（缺 %s）。重跑本脚本会接着下。"
            % "、".join(k for k, v in st["checkpoints"]["parts"].items()
                        if not v))


def self_check(py):
    """真 import 一遍。这是「能不能用」的唯一判据。"""
    log("自检：真 import 一遍…")
    code = (
        "import torch\n"
        "import acestep.inference, acestep.handler, acestep.llm_inference\n"
        "print('torch', torch.__version__, 'cuda', torch.version.cuda,\n"
        "      'avail', torch.cuda.is_available())\n"
        "print('OK')\n")
    rc = _run([py, "-c", code], quiet=True)
    if rc != 0:
        raise SystemExit("[ERROR] 自检没过：上面的 import 有一处失败。")


# ------------------------------------------------------------------ 入口
def main(argv=None):
    ap = argparse.ArgumentParser(description="搭建本地音乐服务的运行环境")
    ap.add_argument("--check", action="store_true", help="只报状态，什么都不装")
    ap.add_argument("--skip-model", action="store_true", help="装包但不下模型")
    ap.add_argument("--with-flash-attn", action="store_true",
                    help="额外安装 flash-attn 加速轮（默认跳过：SDPA 自动回退，"
                         "功能与音质不受影响；该轮子只挂 GitHub release，国内慢）")
    args = ap.parse_args(argv)

    if args.check:
        st, rd = _state()
        print(json.dumps({"ok": True, "ready": rd, "state": st},
                         ensure_ascii=False, indent=2))
        return 0

    log("=" * 62)
    log("  本地音乐服务（ACE-Step 1.5）环境搭建")
    log("=" * 62)
    log()

    st, rd = _state()
    if rd:
        log("环境、代码、权重都齐了，不必重装。")
        return 0

    log("[1/6] 拿官方仓代码")
    try:
        fetch_repo()
    except SystemExit as e:
        log(str(e))
        return 1
    log()

    log("[2/6] 找机器上的 Python")
    base, ver = find_base_python()
    if not base:
        log("      没找到 Python 3.11 / 3.12（官方仓只支持这两档）。")
        log("      装一个再来：https://www.python.org/downloads/")
        return 1
    log("      用 %s（%s）" % (" ".join(base), ver))
    log()

    log("[3/6] 建独立环境")
    make_venv(base)
    py = venv_python()
    log()

    log("[4/6] 装依赖")
    install_torch(py)
    install_deps(py, with_flash=args.with_flash_attn)
    log()

    log("[5/6] 下模型")
    if args.skip_model:
        log("      按 --skip-model 跳过。")
    else:
        fetch_model(py)
    log()

    log("[6/6] 自检")
    self_check(py)
    log()
    log("搭建完成。生成 BGM 时自动加载模型、跑完即退，不常驻显存。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
