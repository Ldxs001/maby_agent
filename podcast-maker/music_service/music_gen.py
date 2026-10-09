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

"""全局音乐库 —— BGM 生成与管理唯一的实现。

音乐不姓项目：一段 30 秒的 BGM 在哪期节目里都能用，把它埋进某个项目目录
等于让第二期再生成一遍。所以落点是**仓库根的全局音乐库**（`音乐库/`），
与项目的音色目录（项目私有）刻意不同。

与 make_voice.py 同一套进程纪律：**CLI 跑完即退，不设常驻模型槽**。生成是
一次一两分钟的活，为它养一个占显存的常驻服务，TTS 就得跟它抢 8 GB 显存。
ACE-Step 用官方仓自带的 Python API（acestep.handler / acestep.inference），
这里不重写任何推理逻辑。

与 make_voice.py 同一套解析契约：**进度走 stderr、结果留 stdout**——
stdout 上只有最后那一份 JSON，父进程照 _cli_json 的口径抠取。文件头的
reconfigure 与 make_voice 同理：GBK 管道上任何输出都打得出去。

草稿流与 VoiceDesign 造嗓同构：生成固定进 `音乐库/_draft.wav`（+_draft.json
账本），试听满意后 `--save --name X` 改名入库（重名 fail-closed），不满意
`--discard` 丢弃（幂等）或换种子重生成。管理= `--status` 清单 + `--delete`。

用法::

    python music_gen.py --generate --prompt "…" --seconds 30 --json
    python music_gen.py --status --json
    python music_gen.py --save --name "轻快钢琴" --json
    python music_gen.py --discard --json
    python music_gen.py --delete --name "轻快钢琴" --json
"""

import argparse
import contextlib
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 音乐库根目录。测试经 --lib-dir 指到临时目录；正式路径只有一个。
LIB_DIRNAME = "音乐库"
DEFAULT_LIB = os.path.join(ROOT, LIB_DIRNAME)

# 草稿名。与 tts_service/make_voice.DRAFT_ROLE 同值不同物：那是音色目录里的
# _draft 角色目录，这里是音乐库里 _draft 那一对文件——同名是因为它们说的是
# 同一件事（还没确认的生成产物）。web_ui 有测试钉死两边等值。
DRAFT_NAME = "_draft"

# 生成用的模型。8 GB 显存档（官方 GPU 表 6-8 GB 一档）：2B turbo DiT +
# 0.6B LM、pt 后端、CPU offload。XL（4B）权重就 ~9 GB，这台机器装不下，
# 别处也不必装 —— BGM 只要 10~30 秒的伴奏，turbo 档绰绰有余。
# LM 权重仍随环境装着（env_state 照查），但生成不加载不参与：CoT 全关。
DIT_MODEL = "acestep-v15-turbo"
LM_MODEL = "acestep-5Hz-lm-0.6B"

SECONDS_MIN = 10
SECONDS_MAX = 30

# 音频条目名：与音色名同一套黑名单（web_ui 界面上另有一道同样的拦截，
# 这里是权威判定）。_draft 是草稿保留名。
NAME_BAD_CHARS = '\\/:*?"<>|'


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def log(msg=""):
    """一行进度，走 stderr —— stdout 是结果通道，一行都不许占。"""
    print(msg, file=sys.stderr, flush=True)


@contextlib.contextmanager
def _stdout_to_stderr():
    """把这段调用期间的 stdout 全部赶到 stderr。

    stdout 是结果通道，一行都不许占；可第三方库不守约 —— 官方仓在生成时往
    stdout 裸 print（真机实测两行「Using precomputed LM hints」）。所以模型加载
    与推理整段重定向：那些杂音落进 stderr（父进程本来就把它当进度读），stdout
    上永远只剩最后那一份 JSON。与 tts_service/serve.py 同名同手法，一处一套。

    注意：**失败/结果的输出不许写在 with 里** —— 那些 print 会被一起赶走，
    父进程就抠不到结果 JSON 了。
    """
    old = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = old


# ------------------------------------------------------------------ 名字与路径
def sanitize_name(name: str) -> str:
    """入库名字的权威判定。返回空串 = 合法；否则返回一句人话错误。"""
    n = (name or "").strip()
    if not n:
        return "先给这段音乐起个名字。"
    for ch in NAME_BAD_CHARS:
        if ch in n:
            return "音乐名里不能有 %s 这类字符。" % NAME_BAD_CHARS
    if n == DRAFT_NAME:
        return "_draft 是草稿保留名，换一个名字。"
    if n in (".", ".."):
        return "这个名字不合法。"
    if len(n) > 24:
        return "音乐名太长（最多 24 个字符）。"
    return ""


def entry_wav(lib: str, name: str) -> str:
    return os.path.join(lib, name + ".wav")


def entry_json(lib: str, name: str) -> str:
    return os.path.join(lib, name + ".json")


def draft_paths(lib: str):
    return entry_wav(lib, DRAFT_NAME), entry_json(lib, DRAFT_NAME)


def read_json_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_file(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sha16(path: str) -> str:
    """文件内容指纹前 16 位（与 make_voice.digest_of 同一口径）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def library_entries(lib: str) -> list:
    """入库清单：每条带账本摘要。草稿不进清单（它有自己的位置）。"""
    out = []
    if not os.path.isdir(lib):
        return out
    for fn in sorted(os.listdir(lib)):
        if not fn.endswith(".wav"):
            continue
        name = fn[:-len(".wav")]
        if name == DRAFT_NAME:
            continue
        wav = os.path.join(lib, fn)
        prof = read_json_file(entry_json(lib, name))
        out.append({"name": name, "wav": wav,
                    "created": prof.get("created", ""),
                    "seconds": prof.get("seconds", ""),
                    "prompt": prof.get("prompt", ""),
                    "prompt_raw": prof.get("prompt_raw", ""),
                    "sha16": prof.get("sha16", "")})
    return out


def draft_state(lib: str) -> dict:
    wav, js = draft_paths(lib)
    if not os.path.isfile(wav):
        return {"exists": False}
    prof = read_json_file(js)
    return {"exists": True, "name": DRAFT_NAME,
            "created": prof.get("created", ""),
            "seconds": prof.get("seconds", ""),
            "prompt": prof.get("prompt", ""),
            "prompt_raw": prof.get("prompt_raw", ""),
            "seed": prof.get("seed", ""),
            "sha16": prof.get("sha16", "")}


def env_state() -> dict:
    """环境三态（纯文件判定，不 import 任何重家伙）：
    venv 在不在 / 官方仓 clone 没 clone / 权重下齐没有。"""
    venv_py = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    if not os.path.isfile(venv_py):
        venv_py = os.path.join(HERE, ".venv", "bin", "python")
    venv_ok = os.path.isfile(venv_py)
    repo_ok = os.path.isfile(os.path.join(HERE, "ACE-Step-1.5",
                                          "acestep", "inference.py"))
    ckpt = os.path.join(HERE, "checkpoints")
    parts = {}
    for comp, marker in ((DIT_MODEL, "model.safetensors"),
                         ("vae", "diffusion_pytorch_model.safetensors"),
                         ("Qwen3-Embedding-0.6B", "model.safetensors"),
                         (LM_MODEL, "model.safetensors")):
        parts[comp] = os.path.isfile(os.path.join(ckpt, comp, marker))
    return {"venv": {"ok": venv_ok, "python": venv_py if venv_ok else ""},
            "repo": {"ok": repo_ok,
                     "path": os.path.join(HERE, "ACE-Step-1.5")},
            "checkpoints": {"ok": all(parts.values()), "dir": ckpt,
                            "parts": parts}}


def ready() -> bool:
    st = env_state()
    return st["venv"]["ok"] and st["repo"]["ok"] and st["checkpoints"]["ok"]


# ------------------------------------------------------------------ 生成
def _fail(err: str) -> int:
    print(json.dumps({"ok": False, "error": err}, ensure_ascii=False),
          flush=True)
    return 1


def _clear_scratch(path: str) -> None:
    """清掉生成用的临时目录。Windows 硬化删除（先解只读再删，不 ignore_errors）。

    与 setup_music_env._rmtree 同一套手法，但**失败口径不同**：那里删不干净会
    让下一步（clone/rename）在几百秒后炸，所以 fail-closed 当场停手；这里删不掉
    **什么都不影响**（调用点在产物搬出之后、或生成开始之前），为它报错等于把一次
    成功的生成判死。所以：删不干净 → 显式 WARN 一行，绝不静默，也绝不误杀成品。
    """
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o666)
            except OSError:
                pass  # 改不动的交给 rmtree 大声报
    try:
        shutil.rmtree(path)
    except OSError as e:
        log("[WARN] 临时目录没清干净（%s）：%s。不影响已生成的成品。" % (path, e))


def _loudnorm_lib():
    """响度归一实现唯一来源 tools/bgm_loudness.py——内置 15 档同一条标尺，
    两边各写一份迟早跑出两个标准（test_bgm_loudness 的 TestSingleImplementation
    钉的就是这条纪律）。"""
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import bgm_loudness
    return bgm_loudness


def _mono_s16(wav_path: str, ffmpeg: str) -> None:
    """把生成产物统一成仓库素材标准规格：s16 单声道（采样率不动）。

    ACE-Step 出的是 f32 立体声；bgm_loudness 的增益施加只吃 s16 单声道，
    成片混音最终也是 -ac 1 下混——素材一落盘就是标准规格，混音侧不再各转各的。
    """
    tmp = wav_path + ".mono.wav"
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-y", "-i", wav_path,
         "-ac", "1", "-c:a", "pcm_s16le", tmp],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.isfile(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("规格统一失败（mono/s16）：%s"
                           % (proc.stderr or "")[-300:])
    os.replace(tmp, wav_path)


def _check_not_silence(wav_path: str) -> str:
    """生成产物是全静音 = 白跑一趟（模型抽风时会发生）。fail-closed 拒收。"""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        return ""  # 自检已保证装上了；真没装也不拦着收货，让耳朵判
    try:
        data, _sr = sf.read(wav_path, dtype="float32")
    except Exception as e:  # noqa: BLE001
        return "生成的文件读不出来：%s" % e
    if data.size == 0:
        return "生成的文件是空的。"
    rms = float(np.sqrt(np.mean(np.square(data))))
    if rms < 1e-3:  # < -60 dBFS
        return "生成结果是静音（模型这次没出声），换一条重试。"
    return ""


def cmd_generate(lib: str, prompt: str, seconds: int,
                 seed: int, seed_offset: int, prompt_raw: str = "",
                 lyrics: str = "", bpm=None, keyscale: str = "",
                 timesignature: str = "") -> int:
    """生成一条 BGM 进草稿。重家伙全部懒加载 —— 本函数之外的路径
    （save/discard/delete/status）只靠标准库，主程序的 Python 也能跑。"""
    if not (prompt or "").strip():
        return _fail("音乐描述不能空（例如「轻快的钢琴曲，温暖明亮，适合开场」）。")
    prompt = prompt.strip()
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return _fail("时长要是 10~30 的整数秒。")
    if not (SECONDS_MIN <= seconds <= SECONDS_MAX):
        return _fail("时长超出范围：%d 秒。本模块只做 %d~%d 秒的 BGM。"
                     % (seconds, SECONDS_MIN, SECONDS_MAX))
    if not ready():
        st = env_state()
        miss = [k for k, v in (("环境", st["venv"]["ok"]),
                               ("官方仓", st["repo"]["ok"]),
                               ("模型权重", st["checkpoints"]["ok"])) if not v]
        return _fail("音乐生成环境不完整（缺%s）。到配置页点一次「搭建音乐环境」。"
                     % "、".join(miss))

    # checkpoints 定位：官方代码按 ACESTEP_PROJECT_ROOT 找，这里钉在
    # music_service/ 下 —— 换个工作目录跑也不漂。
    os.environ["ACESTEP_PROJECT_ROOT"] = HERE

    # 开关取舍（2026-10-09 同 caption 同 seed 四组对照实测，见 CHANGELOG）：
    # - **thinking=True 是铺满时长的唯一开关**：LM 两阶段里 Phase 2 按
    #   target_duration 谱 audio codes，DiT 只负责渲染——时长由 codes 长度
    #   保证。全关时 DiT 裸写，稀疏的低音氛围曲 14 秒就「写完」，后面全是
    #   恒定 -55 dB 的地板（真机「测试音」15 秒断崖，同 seed 稳定复现）。
    # - **use_cot_caption=False 是 caption 保真的唯一条件**：官方
    #   inference.py:826 只认这一个开关决定是否用 LM 产物覆盖 DiT 输入，
    #   thinking 开着也不碰 caption。描述的「理解」由主程序 LLM 转译层做完，
    #   转译稿原样进 DiT——早前劫持（「低音诡异无鼓」被改写成 energetic
    #   synthwave + drum machine）发生在三连全开，关掉这一个就够了。
    # - use_cot_language=False：纯器乐 [Instrumental]，人声语言检测无意义。
    # - use_cot_metas=False：LM 的 bpm/调性规划对铺满时长没贡献（仅开 metas
    #   实测只铺到 21 秒），不值得为它跑 Phase 1。
    log("加载 ACE-Step DiT（%s，首次加载要一会儿）…" % DIT_MODEL)
    try:
        import torch
        from acestep.handler import AceStepHandler
        from acestep.inference import (GenerationConfig, GenerationParams,
                                       generate_music)
        from acestep.llm_inference import LLMHandler
    except ImportError as e:
        return _fail("音乐生成依赖没装齐（%s）。到配置页点一次「搭建音乐环境」。"
                     % type(e).__name__)

    try:
        with _stdout_to_stderr():
            dit = AceStepHandler()
            ok, msg = dit.initialize_service(
                project_root=HERE, config_path=DIT_MODEL, device="cuda",
                use_flash_attention=False,
                offload_to_cpu=True,      # 8 GB 卡：组件常驻换 offload 往返
                offload_dit_to_cpu=True)
            load_err = "" if ok else "DiT 模型加载失败：%s" % msg
    except Exception as e:  # noqa: BLE001
        return _fail("模型加载失败：%s: %s" % (type(e).__name__, e))
    if load_err:
        return _fail(load_err)   # 报错文案在重定向之外打印：结果通道不被改写

    # 5Hz LM：thinking 谱曲的执行者。加载失败 WARN 降级不停产——lm=None 时
    # 官方判定 use_lm=False，DiT 裸写（稀疏曲可能写不满时长），后果说清，
    # 让耳朵判这一条的去留，不为它废一整趟生成。
    lm = None
    lm_backend = ""
    try:
        with _stdout_to_stderr():
            lm = LLMHandler()
            msg, ok = lm.initialize(
                checkpoint_dir=os.path.join(HERE, "checkpoints"),
                lm_model_path=LM_MODEL,
                backend="pt", device="cuda", offload_to_cpu=True)
        if ok:
            lm_backend = "pt"
        else:
            log("[WARN] 5Hz LM 加载失败（%s），按无谱曲模式生成——"
                "稀疏编曲可能写不满时长。" % msg)
    except Exception as e:  # noqa: BLE001
        lm = None
        log("[WARN] 5Hz LM 加载异常（%s: %s），按无谱曲模式生成。"
            % (type(e).__name__, e))

    if seed is not None and seed >= 0:
        eff = (int(seed) + int(seed_offset)) % (2 ** 31 - 1)
    else:
        eff = random.randrange(2 ** 31 - 1)
    log("生成中：seed=%d，%d 秒 …" % (eff, seconds))
    # 三层输入（官方 Tutorial 的 caption/lyrics/元数据协同）：caption 是整体
    # 画像、lyrics 是时间脚本、bpm/keyscale/timesignature 是节奏调性锚点。
    # lyrics 与元数据由 web 侧转译层给出（--lyrics/--bpm/…）；缺省时 lyrics 退回
    # 官方纯器乐写法 [Instrumental]、元数据留空交模型自定——但那样 30 秒里没有
    # 演进指令，模型建完开场就退化成一条静止 drone 铺到底（实测「前 6 秒有内容、
    # 后面全低频」），所以要尽量给全。
    params = GenerationParams(
        task_type="text2music", caption=prompt,
        lyrics=(lyrics or "[Instrumental]"), instrumental=True,
        bpm=bpm, keyscale=keyscale or "", timesignature=timesignature or "",
        duration=float(seconds),
        inference_steps=8, shift=3.0,   # turbo 档官方推荐：8 步 + shift 3.0
        thinking=True,                  # LM 谱曲定时长；caption 由下面三连关保真
        use_cot_caption=False, use_cot_language=False, use_cot_metas=False,
        seed=eff)
    config = GenerationConfig(batch_size=1, audio_format="wav",
                              use_random_seed=False, seeds=[eff])

    tmp_dir = os.path.join(lib, ".generating")
    _clear_scratch(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    wav, js = draft_paths(lib)
    os.makedirs(lib, exist_ok=True)
    try:
        with _stdout_to_stderr():
            result = generate_music(dit, lm, params, config, save_dir=tmp_dir)
        if not getattr(result, "success", False):
            return _fail("生成失败：%s" % (getattr(result, "error", "") or "原因不明"))
        audios = getattr(result, "audios", None) or []
        if not audios:
            return _fail("生成完成但没有音频产物。")
        src = audios[0].get("path") or ""
        if not src or not os.path.isfile(src):
            return _fail("音频产物没落盘（%r）。" % src)

        # 记账用实际生效的种子（官方返回值，别自算自记）
        try:
            real_seed = int(audios[0].get("params", {}).get("seed", eff))
        except (TypeError, ValueError):
            real_seed = eff

        # **先搬后清，顺序不能反。** 官方把音频写进 save_dir（就是上面那个临时
        # 目录），而 finally 会清掉它 —— 搬运若留在 finally 之后，刚生成的文件
        # 已被连目录一起带走，随后的 isfile 必然为假，报「音频产物没落盘」，
        # 白跑一整趟（模型加载 + 推理 + VAE 解码都白做）。所以：产物搬进草稿位
        # 之后再让 finally 清场。
        shutil.move(src, wav)
        err = _check_not_silence(wav)
        if err:
            if os.path.isfile(wav):
                os.remove(wav)
            return _fail(err)

        # 素材规格 + 响度：与内置 15 档同一条标尺（-23 LUFS / 真峰值 -1 dBTP），
        # 实现唯一来源 tools/bgm_loudness。ACE-Step 产物是 f32 立体声且响度
        # 自由发挥（实测 -16.4 LUFS，比内置档响 6.5 dB），不归一就会压场。
        # 削峰红线不动（normalize_file 的合同：拒绝 clip，失败时文件保持
        # 原样）；峰均比超标的素材按 limited 显式放行落稿（见下），其余
        # 归一失败照样删稿停产。
        try:
            loudness = _loudnorm_lib()
            ff = loudness.ffmpeg_bin()
            _mono_s16(wav, ff)
            # 显式放行 limited：素材峰均比大到峰值先撞线（稀疏瞬态类 BGM
            # 的常态，实测 23.0 dB 撞 22.0 dB 余量）时按真峰值上限落稿、
            # 账本记 norm_limited——不达标但受控可追溯，而不是删稿停产
            # 让生成随机失败。削峰红线不动：增益照旧按上限算。
            norm = loudness.normalize_file(wav, ffmpeg=ff, allow_limited=True)
        except Exception as e:  # noqa: BLE001
            if os.path.isfile(wav):
                os.remove(wav)
            return _fail("响度归一失败（%s: %s）。" % (type(e).__name__, e))
        if not norm.get("ok"):
            if os.path.isfile(wav):
                os.remove(wav)
            return _fail("响度归一失败（%s）。" % norm.get("reason"))

        # limited 放行必须显式告知：照出，但不静默。日志明说差多少，账本
        # 记实际落点，试听的人自己判断要不要这条。
        if norm.get("limited"):
            log("[WARN] 素材峰均比 %.1f dB 超出标尺余量（%.1f dB），按真峰值"
                "上限落库：实际 %.2f LUFS（标尺 %.1f，差 %.2f LU）。未削峰，"
                "账本已记 norm_limited。"
                % (float(norm["tp"]) - float(norm["lufs"]),
                   loudness.DEFAULT_TARGET_LUFS - loudness.DEFAULT_TP_CEIL,
                   float(norm["after_lufs"]),
                   loudness.DEFAULT_TARGET_LUFS,
                   loudness.DEFAULT_TARGET_LUFS - float(norm["after_lufs"])))

        write_json_file(js, {
            "role": DRAFT_NAME, "prompt": prompt, "prompt_raw": prompt_raw,
            "seconds": seconds,
            "seed": real_seed, "model": DIT_MODEL, "lm": lm_backend,
            "lyrics": lyrics, "bpm": bpm, "keyscale": keyscale,
            "timesignature": timesignature,
            "norm_gain_db": round(float(norm.get("gain_db") or 0.0), 2),
            "norm_limited": bool(norm.get("limited")),
            "norm_after_lufs": (None if norm.get("after_lufs") is None
                                else round(float(norm["after_lufs"]), 2)),
            "sample_rate": 48000, "sha16": sha16(wav),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        log("草稿已生成，试听满意后保存入库。")
        print(json.dumps({"ok": True, "draft": draft_state(lib)},
                         ensure_ascii=False), flush=True)
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail("生成失败：%s: %s" % (type(e).__name__, e))
    finally:
        _clear_scratch(tmp_dir)


# ------------------------------------------------------------------ 管理
def cmd_save(lib: str, name: str) -> int:
    err = sanitize_name(name)
    if err:
        return _fail(err)
    name = name.strip()
    wav, js = draft_paths(lib)
    if not os.path.isfile(wav):
        return _fail("还没有草稿：先点「生成音乐」造一条。")
    dst_wav, dst_js = entry_wav(lib, name), entry_json(lib, name)
    if os.path.exists(dst_wav) or os.path.exists(dst_js):
        return _fail("音乐「%s」已存在。到管理里删掉旧的，或换一个名字。" % name)
    os.rename(wav, dst_wav)
    if os.path.isfile(js):
        prof = read_json_file(js)
        prof["role"] = name
        write_json_file(dst_js, prof)
        os.remove(js)
    else:
        write_json_file(dst_js, {"role": name,
                                 "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    print(json.dumps({"ok": True, "name": name}, ensure_ascii=False),
          flush=True)
    return 0


def cmd_discard(lib: str) -> int:
    """丢弃草稿。没有草稿时静默成功（幂等）——「取消」按两下不该报错。"""
    wav, js = draft_paths(lib)
    for p in (wav, js):
        if os.path.isfile(p):
            os.remove(p)
    print(json.dumps({"ok": True}, ensure_ascii=False), flush=True)
    return 0


def cmd_delete(lib: str, name: str) -> int:
    err = sanitize_name(name)
    if err:
        return _fail(err)
    name = name.strip()
    if name == DRAFT_NAME:
        return _fail("草稿用「取消」丢弃，不走删除。")
    wav, js = entry_wav(lib, name), entry_json(lib, name)
    if not os.path.isfile(wav) and not os.path.isfile(js):
        return _fail("音乐「%s」不存在。" % name)
    for p in (wav, js):
        if os.path.isfile(p):
            os.remove(p)
    print(json.dumps({"ok": True, "name": name}, ensure_ascii=False),
          flush=True)
    return 0


def cmd_status(lib: str) -> int:
    print(json.dumps({"ok": True, "lib": lib, "ready": ready(),
                      "env": env_state(), "draft": draft_state(lib),
                      "library": library_entries(lib)},
                     ensure_ascii=False), flush=True)
    return 0


# ------------------------------------------------------------------ 入口
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全局音乐库：BGM 生成与管理")
    ap.add_argument("--lib-dir", default="", help="音乐库根目录（默认仓库根/音乐库）")
    ap.add_argument("--generate", action="store_true", help="生成一条 BGM 进草稿")
    ap.add_argument("--prompt", default="", help="音乐描述（自然语言）")
    ap.add_argument("--prompt-raw", default="",
                    help="转译前的原始描述（只记账，不参与推理；"
                         "未做转译时与 --prompt 同值或留空）")
    ap.add_argument("--lyrics", default="",
                    help="时间脚本：结构标记分段（纯器乐缺省 [Instrumental]）")
    ap.add_argument("--bpm", type=int, default=None,
                    help="BPM 锚点 30~300（缺省交模型自定）")
    ap.add_argument("--keyscale", default="", help="调性锚点，如 A minor")
    ap.add_argument("--timesignature", default="",
                    help="拍号锚点：2/3/4/6（缺省交模型自定）")
    ap.add_argument("--seconds", type=int, default=30, help="时长 10~30 秒")
    ap.add_argument("--seed", type=int, default=-1, help="种子（-1 = 随机）")
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="种子偏移：重生成时换一个数，出的是另一条")
    ap.add_argument("--status", action="store_true", help="报告草稿、清单与环境")
    ap.add_argument("--save", default=None, metavar="NAME", help="把草稿改名入库")
    ap.add_argument("--discard", action="store_true", help="丢弃草稿（幂等）")
    ap.add_argument("--delete", default=None, metavar="NAME", help="删除一条已入库音乐")
    ap.add_argument("--json", action="store_true", help="结果以 JSON 打印（保留字，恒为真）")
    args = ap.parse_args(argv)

    lib = args.lib_dir or DEFAULT_LIB
    if args.generate:
        return cmd_generate(lib, args.prompt, args.seconds,
                            args.seed, args.seed_offset, args.prompt_raw,
                            lyrics=args.lyrics, bpm=args.bpm,
                            keyscale=args.keyscale,
                            timesignature=args.timesignature)
    # 分发按「传没传」判，不按值真假判 —— 否则 `--save ""` 会因空串为假
    # 静默滑进 status 分支，操作没做还报成功。
    if args.save is not None:
        return cmd_save(lib, args.save)
    if args.discard:
        return cmd_discard(lib)
    if args.delete is not None:
        return cmd_delete(lib, args.delete)
    return cmd_status(lib)


if __name__ == "__main__":
    sys.exit(main())
