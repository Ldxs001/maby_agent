#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 TTS 薄服务 · Qwen3-TTS（CUDA 图加速）

为什么单独一个服务
------------------
主程序只需要「一句文字 → 一个 wav」。模型的重活（torch / CUDA / 几个 GB 权重）
放在这个独立环境里跑，主程序不引入 torch，双击 setup.bat 依旧能起。

对外接口
--------
    POST /tts               本项目既有形状（主程序现用，接入时零改动）
                            {"text": "...", "voice": "Serena", "speed": 1.0,
                             "instruct": "可选，自然语言语气指令",
                             "ref_audio": "可选，参考音频路径 → 走音色克隆",
                             "ref_text":  "走克隆时必填，参考音频的转录文本"}
    POST /v1/audio/speech   OpenAI 标准形状（通用，以后换引擎即插即用）
                            {"input": "...", "voice": "Serena", "response_format": "wav"}
    GET  /health            模型状态：是否已加载、跑在哪个设备、当前空闲显存
    GET  /speakers          该模型支持的音色清单（克隆变体下报参考音频要求）

两副面孔
--------
模型有两个变体，本服务的 `--model` 决定它此刻是哪一副：

  - **Base（默认）**：没有内置音色表，音色由 `ref_audio` 决定。传了就按参考音频
    克隆，走 ICL 模式（参考音频连同转录文本一起进上下文），句间音色漂移最小。
  - **CustomVoice**：自带九个音色，靠 `voice` 字段选。它不常驻本服务——两个变体
    各占约 3.4GB 显存，8GB 的卡上放不下。出参考音频由 make_voice.py 单独起一个
    进程调它，用完就退。

显存安全（本机最重要的一条）
----------------------------
这台机器同时跑着 LM Studio 的 35B。本服务**绝不抢占显存**：
加载前先读空闲显存，够用才上 GPU，不够就退 CPU。
宁可自己慢十倍，也不能把别人正在用的模型挤掉。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import wave
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))

# 默认模型：1.7B 的 Base 变体。音色不在模型里，而在参考音频里 —— 同一份参考
# 喂给整期，句间音色比内置音色表稳得多（实测句间 F0 相对极差 46.9% → 22.5%）。
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
# 内置音色变体。**不由本服务常驻**：两个变体各约 3.4GB 权重，8GB 的卡放不下。
# 它只被 make_voice.py 那个一次性进程加载，用来把「塞尔娜」这类内置音色录成一段
# 参考音频，之后整期都走 Base。显存比 Cold start 更贵。
CUSTOM_VOICE_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
# 与主程序 tts.qwen3tts_port 的默认值保持一致，接入时端口不用改
DEFAULT_PORT = 9880

# 上 GPU 的门槛：1.7B BF16 权重约 3.4GB，加激活与 CUDA 图缓冲，给 4500MB 起算
VRAM_NEED_MB = 4500
# 安全余量：留给系统与其他进程（本机常态还驻留着 LM Studio）
VRAM_SAFETY_MB = 800

# 中文可用的自带音色（其余为英/日/韩）
ZH_SPEAKERS = ("Serena", "Vivian", "Uncle_Fu", "Dylan", "Eric")

# 九个开箱音色，来源：模型官方 README 的 Speaker 表。
# 官方另有 model.get_supported_speakers() 运行时接口，但那要先加载权重；
# 音色表是模型的固定属性，写在这里可让界面在模型未加载时也能给出下拉。
SPEAKERS = (
    {"name": "Vivian", "language": "Chinese", "desc": "明亮、略带锋芒的年轻女声"},
    {"name": "Serena", "language": "Chinese", "desc": "温暖、轻柔的年轻女声"},
    {"name": "Uncle_Fu", "language": "Chinese", "desc": "低沉醇厚的成熟男声"},
    {"name": "Dylan", "language": "Chinese", "desc": "清亮自然的北京口音男声"},
    {"name": "Eric", "language": "Chinese", "desc": "活泼、带点沙哑的成都口音男声"},
    {"name": "Ryan", "language": "English", "desc": "节奏感强的男声"},
    {"name": "Aiden", "language": "English", "desc": "阳光的美式男声"},
    {"name": "Ono_Anna", "language": "Japanese", "desc": "轻快灵动的日语女声"},
    {"name": "Sohee", "language": "Korean", "desc": "情感丰沛的韩语女声"},
)

# 只有真情绪才配折成语气指令。脚本里的标签另有一类是「语篇功能」
# （开场 / 过渡 / 总结 / 解释 / 比喻 …），说的是这句话在结构上干什么，跟心情
# 无关；拼成「用比喻的语气说」对模型是纯噪音，还会把真情绪淹没。所以只认情绪。
# 这七个与主程序 podcast_maker/config_manager.py 的 EMOTION_VOCAB 同源，
# tests/test_tts_engine.py 逐条比对，两边不许走散。
INSTRUCT_EMOTIONS = ("平静", "好奇", "疑惑", "恍然", "肯定", "感慨", "轻松")

# 采样温度。库默认 0.9。
# 压到 0.2 本来是想换音色更稳（音色质心漂移 278 Hz，0.9 是 550 Hz），但实测
# 20 个种子里有 4 句直接跑满生成上限、炸成两分半的噪声；0.4 与 0.9 都是 0/20。
# 也就是 0.2 这一档不能用——省下的那点抖动，换来的是「每五句废一句」。
# 取 0.4：退化率与音色漂移都与库默认同级，但采样幅度收一档，属不额外冒险的保守值。
# 定档依据见 _smoke/_probe_degen_rate.py 与 _smoke/_probe_temp_clean.py。
TEMPERATURE = 0.4

# 采样参数。库默认就是这几个值，这里显式写出来：它们是「音色稳不稳」的直接
# 旋钮，藏在默认值里以后没人改得动。
SAMPLE_KWARGS = {"temperature": TEMPERATURE, "top_k": 50, "top_p": 1.0,
                 "do_sample": True, "repetition_penalty": 1.05}


def seed_for(text: str, voice_key: str) -> int:
    """按 (文本, 音色标识) 派生随机种子。

    同一条文本配同一个音色 → 同一种子 → 同一条波形。这样「那一句不对劲」才有得
    复现：拿同样的输入重跑，得到的就是同一条。不同句子仍旧各自不同——种子本身就是
    内容的函数，所以它不会把整期磨成同一个调子。

    `voice_key` 走内置音色时是音色名；走参考音频克隆时必须传**音频内容的指纹**，
    不能传文件路径（见 voice_key_for）。路径换个项目就变，拿它当输入，同一份参考
    音频在两个项目里会合成出完全不同的波形。
    """
    h = hashlib.sha256(("%s\x00%s" % (voice_key, text)).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


@lru_cache(maxsize=32)
def _digest_of(path: str, mtime: float, size: int) -> str:
    """文件内容摘要。mtime 与 size 只参与缓存键，不进摘要本身。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def voice_key_for(ref_audio: str) -> str:
    """参考音频的内容指纹，种子拿它当音色标识。

    为什么不用路径：路径随项目变，种子跟着变。同一份参考音频复制到新项目，
    波形本该一模一样，用路径就做不到了。指纹只跟内容走，复制到哪都是同一条。
    """
    st = os.stat(ref_audio)
    return "ref:" + _digest_of(ref_audio, st.st_mtime, st.st_size)[:16]


def model_kind(model_path: str) -> str:
    """模型是哪个变体：base / custom_voice / voice_design。

    读的是权重目录里的 config.json，不是目录名 —— 目录可以改名，字段不会。
    这个值决定两件事：要不要参考音频、/speakers 该报什么。
    """
    try:
        with open(os.path.join(model_path, "config.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("tts_model_type") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def set_seed(seed: int) -> None:
    """把这一句的随机性钉住。torch 可用才设（调用点都在模型加载之后）。"""
    import torch  # 延迟导入：服务启动本身不引入 torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


# 生成帧数上限。库默认 2048 帧，12 Hz → 170 秒。对一句话来说宽得离谱：模型偶尔
# 会落进重复循环，一路念到上限才停，出来一段两分半的噪声。按字数给上限，把它截在
# 合理区间。中文播客语速约 5 字/秒 → 12 Hz 下 1 字约 2.4 帧；这里给到 8 帧/字，
# 是正常时长的三倍余量，短句另设 120 帧兜底（约 10 秒）。
MAX_FRAMES = 2048
FRAMES_PER_CHAR = 8
FRAME_FLOOR = 120


def max_frames_for(text: str) -> int:
    """这句话最多允许生成多少帧。"""
    return int(min(MAX_FRAMES, max(FRAME_FLOOR, len(text) * FRAMES_PER_CHAR)))


def looks_degenerate(seconds: float, text: str) -> bool:
    """时长顶到上限，说明它没在按文本念，而是在循环。

    判据取「到达上限的九成五」而不是「超过某个绝对秒数」：上限是按文本算的，
    长句的上限本来就高，用绝对秒数会误伤。
    """
    limit = max_frames_for(text) / 12.0
    return seconds >= limit * 0.95


# --------------------------------------------------------------------------- #
# 显存与设备
# --------------------------------------------------------------------------- #

def resolve_model(model_id: str) -> str:
    """模型路径解析：本地下过就用本地，否则用仓库 id（在线拉）。

    这样 setup.bat 下完权重之后，服务默认就走本地，不再联网。
    """
    if os.path.isdir(model_id):
        return model_id
    local = os.path.join(HERE, "models", model_id.split("/")[-1])
    if os.path.isdir(local):
        try:
            if any(f.endswith((".safetensors", ".bin", ".pt"))
                   for f in os.listdir(local)):
                return local
        except OSError:
            pass
    return model_id


def free_vram_mb() -> int | None:
    """读当前空闲显存（MB）。读不到返回 None。"""
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return int(p.stdout.strip().splitlines()[0])
    except Exception:
        return None


def used_vram_mb() -> int | None:
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return int(p.stdout.strip().splitlines()[0])
    except Exception:
        return None


def pick_device(prefer: str) -> tuple[str, str]:
    """决定跑在哪个设备上，返回 (device, 人话说明)。

    核心原则：绝不抢占。空闲显存不够就退 CPU。
    """
    need = VRAM_NEED_MB + VRAM_SAFETY_MB

    if prefer == "cpu":
        return "cpu", "配置指定 CPU"

    free = free_vram_mb()
    if free is None:
        return "cpu", "读不到显存（无 GPU 或无驱动），走 CPU"

    if free >= need:
        tag = "按配置强制" if prefer == "cuda" else "自动判定可用"
        return "cuda", f"空闲 {free}MB ≥ {need}MB（{tag}）→ GPU + CUDA 图"

    # 空间不够：无论是否强制，都退 CPU，并说清原因
    if prefer == "cuda":
        return "cpu", (f"配置要求 GPU，但空闲只剩 {free}MB（需 ≥{need}MB）"
                       f"→ 已退 CPU，避免挤掉正在运行的模型")
    return "cpu", f"空闲只剩 {free}MB < {need}MB → 走 CPU（不占显存，不动别人的模型）"


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

class Engine:
    """模型单例。懒加载：服务启动本身不占显存，第一个请求才加载。"""

    def __init__(self, model_id: str, prefer_device: str, log):
        self.model_id = model_id
        self.prefer_device = prefer_device
        self.log = log
        self._model = None
        self._lock = threading.RLock()
        self.device: str | None = None
        self.device_note = ""
        self.load_seconds: float | None = None
        self.backend = ""
        self.last_error: str | None = None
        # 模型变体（base / custom_voice）。加载后按权重目录的 config.json 定，
        # 不看目录名 —— 目录可以改名，字段不会。
        self.kind = model_kind(model_id)
        # 「首帧不确定性」是否已经吃过。内置音色与参考音频两条路的预热方式不同，
        # 所以这个标记由实际跑到的那一条来置位，而不是在 ensure 里无脑置位。
        self._warmed = False

    # -- 加载 ------------------------------------------------------------- #

    def ensure(self):
        with self._lock:
            if self._model is not None:
                return self._model

            self.device, self.device_note = pick_device(self.prefer_device)
            self.log(f"[引擎] 设备判定：{self.device_note}")

            t0 = time.time()
            backend_errors = []

            for name, loader in (
                ("faster-qwen3-tts（CUDA 图）", self._load_faster),
                ("qwen-tts（官方，慢）", self._load_official),
            ):
                try:
                    self._model = loader(self.device)
                    self.backend = name
                    break
                except Exception as e:  # noqa: BLE001
                    backend_errors.append(f"{name}: {e}")
                    self.log(f"[引擎] {name} 加载失败：{e}")

            if self._model is None:
                self.last_error = " | ".join(backend_errors)
                raise RuntimeError("模型加载失败：\n" + self.last_error)

            self._consume_first_inference()
            self.load_seconds = time.time() - t0
            self.log(f"[引擎] 就绪 · {self.backend} · {self.device} · "
                     f"耗时 {self.load_seconds:.1f}s")
            return self._model

    def _consume_first_inference(self):
        """吃掉「本进程第一次推理」的不确定性。

        实测：模型刚加载完时的第一个请求，结果与同种子、同参数的后续请求不同 ——
        CUDA 图捕获之后，首次 replay 的状态与之后并不一致。跑一次极短的哑合成把它
        耗掉，这样「同一句永远同一条」对第一个请求也成立。

        内置音色模型只有音色表这一条路，这里就能吃掉；Base 变体没有音色表，连哑
        合成都要一段参考音频，所以这一跳**必然失败** —— 失败时不置位，留给第一次
        真正拿到参考音频的请求去做（见 _warm_with_ref）。
        """
        try:
            self.synth_one("嗯", ZH_SPEAKERS[0], seed=0)
            self._warmed = True
        except Exception as e:  # noqa: BLE001
            self.log(f"[引擎] 内置音色预热不适用（{type(e).__name__}）：{e}")
            self.log("[引擎] 改在首次使用参考音频时预热")

    def _warm_with_ref(self, ref_audio: str, ref_text: str) -> None:
        """Base 变体的预热：用一次极短的哑克隆吃掉首帧不确定性。

        为什么必须做：不做的话整期的**第一句**会与同种子、同参数重跑的结果不同 ——
        用户听到「第一句跟别人不一样」，重跑一次又对了，极难定位。
        """
        self._warmed = True          # 先置位：预热本身失败也不该反复重来
        try:
            self._gen_clone("嗯", ref_audio, ref_text, None, "Chinese", 0)
        except Exception as e:  # noqa: BLE001
            self.log(f"[引擎] 参考音频预热失败（不影响使用）：{e}")

    def _gen_clone(self, text: str, ref_audio: str, ref_text: str,
                   instruct: str | None, language: str, seed: int | None):
        """参考音频克隆一次，走 ICL 模式（xvec_only=False）。

        为什么不用只喂向量的 xvec_only：官方把它标为实验性，而且参考音频的信息量
        被压成一个 2048 维向量，句间音色漂移更大（实测 F0 相对极差 27.4% vs
        ICL 的 13.1%）。ICL 把参考音频连同它的转录文本一起放进上下文 ——
        相当于跟模型说「你听一遍这个人怎么说话，再照这个念」。

        参考音频的编码在库内按 (路径, 转录文本) 缓存（`_voice_prompt_cache`），
        逐句不重复计算，所以这里逐句传路径即可，不需要在服务层另建缓存。
        """
        model = self.ensure()
        if seed is not None:
            set_seed(seed)
        fn = getattr(model, "generate_voice_clone", None)
        if fn is None:
            raise RuntimeError(
                "当前模型不支持参考音频克隆；音色克隆要用 Base 变体，"
                "把 --model 指到 %s。" % DEFAULT_MODEL)

        rich: dict[str, Any] = {
            "text": text, "language": language,
            "ref_audio": ref_audio, "ref_text": ref_text or "",
            "xvec_only": False,
            "max_new_tokens": max_frames_for(text),
            **SAMPLE_KWARGS,
        }
        if instruct:
            rich["instruct"] = instruct
        try:
            out = fn(**rich)
        except TypeError:
            # 实现不认这套参数：退到最小必要集。采样参数保不住也得先出声，
            # 但这一退会改变音色稳定度，所以记一条日志，不要静默。
            self.log("[引擎] 克隆接口不吃完整参数集，已退到最小集（音色可能更抖）")
            out = fn(text=text, language=language, ref_audio=ref_audio,
                     ref_text=ref_text or "", xvec_only=False)
        return _split_out(out)

    def _load_faster(self, device: str):
        # from_pretrained 自带 device 参数，直接交给它（显式 CPU 时它就不碰显存）
        from faster_qwen3_tts import FasterQwen3TTS  # type: ignore
        return FasterQwen3TTS.from_pretrained(self.model_id, device=device)

    def _load_official(self, device: str):
        from qwen_tts import Qwen3TTSModel  # type: ignore
        try:
            m = Qwen3TTSModel.from_pretrained(
                self.model_id,
                device_map=("cuda:0" if device == "cuda" else "cpu"),
            )
        except TypeError:
            m = Qwen3TTSModel.from_pretrained(self.model_id)
            self._to_device(m, device)
        return m

    def _to_device(self, model, device: str) -> None:
        """把模型搬到目标设备。能搬就搬，搬不动就留着（部分实现自己管）。"""
        if device != "cuda":
            return
        inner = getattr(model, "model", model)
        try:
            inner.to("cuda")
        except Exception as e:  # noqa: BLE001
            self.log(f"[引擎] 无法整体搬上 GPU（{e}），沿用其自身的设备策略")

    def actual_device(self) -> str:
        """实测参数真正落在哪 —— 防止「写了 cuda 却跑在 CPU」的老问题。"""
        if self._model is None:
            return "未加载"
        try:
            for holder in (getattr(self._model, "model", None), self._model):
                if holder is None:
                    continue
                params = getattr(holder, "parameters", None)
                if params is None:
                    continue
                first = next(params())
                return str(first.device)
        except Exception:  # noqa: BLE001
            pass
        return self.device or "未知"

    # -- 合成 ------------------------------------------------------------- #

    def synth_one(self, text: str, speaker: str, instruct: str | None = None,
                  language: str = "Chinese", seed: int | None = None,
                  ref_audio: str | None = None, ref_text: str | None = None,
                  ) -> tuple[Any, int]:
        """合成一句。两条路**显式分流**，不靠「挨个方法试一遍」去猜。

          - 给了 `ref_audio` → 参考音频克隆（ICL）。Base 变体走这条。
          - 没给 → 内置音色表。CustomVoice / voice_design 走这条。

        以前只有一个遍历式的实现：挨个试 generate_custom_voice / voice_design /
        voice_clone，只对 TypeError 兜底。Base 模型下 generate_custom_voice 抛的是
        ValueError（"does not support custom voice generation"），兜不住，于是
        「换个模型就跑不起来」，而且报错完全指不到病根。
        """
        if ref_audio:
            if not self._warmed:
                self._warm_with_ref(ref_audio, ref_text or "")
            return self._gen_clone(text, ref_audio, ref_text or "", instruct,
                                   language, seed)

        model = self.ensure()
        if seed is not None:
            set_seed(seed)

        # 两套参数：全套（采样参数 + 语气指令）与必要集。后者是签名不认全套时的
        # 退路——不同实现的接口并不完全一致，但采样参数是「音色稳不稳」的旋钮，
        # 能带就必须带上。
        rich: dict[str, Any] = {"text": text, "language": language,
                                "speaker": speaker,
                                "max_new_tokens": max_frames_for(text),
                                **SAMPLE_KWARGS}
        if instruct:
            rich["instruct"] = instruct
        lean: dict[str, Any] = {"text": text, "speaker": speaker}

        gen = None
        for attr in ("generate_custom_voice", "generate_voice_design"):
            fn = getattr(model, attr, None)
            if fn is None:
                continue
            for kwargs in (rich, lean):
                try:
                    gen = fn(**kwargs)
                    break
                except TypeError:
                    # 这个实现不认这套参数，退到下一套再试
                    continue
            if gen is not None:
                break
        if gen is None:
            raise RuntimeError(
                "模型没有可用的内置音色合成方法。若是 Base 变体，请带 ref_audio "
                "走参考音频克隆。")

        samples, sr = _split_out(gen)
        return samples, int(sr)


def _split_out(out: Any) -> tuple[Any, int]:
    """把合成结果拆成 (单条波形, 采样率)。

    不同实现的返回形状不完全一致，这里统一兜住：
    (wavs, sr) / [[wav]] / tensor / ndarray 都接受。
    """
    sr = 24000
    wavs = out
    if isinstance(out, (tuple, list)) and len(out) == 2 and isinstance(out[1], (int, float)):
        wavs, sr = out[0], int(out[1])
    if isinstance(wavs, (list, tuple)):
        while wavs and isinstance(wavs[0], (list, tuple)):
            wavs = wavs[0]
        wavs = wavs[0] if wavs else []
    elif hasattr(wavs, "ndim") and getattr(wavs, "ndim", 0) == 2:
        wavs = wavs[0]
    return wavs, sr


def to_wav_bytes(samples: Any, sr: int) -> bytes:
    """float 波形 → 16-bit 单声道 PCM WAV 字节。"""
    import numpy as np

    if hasattr(samples, "detach"):
        samples = samples.detach().cpu().numpy()
    a = np.asarray(samples, dtype=np.float32).reshape(-1)
    if a.size:
        peak = float(np.max(np.abs(a)))
        if peak > 1.5:            # 回来的已是 int16 量级
            a = a / 32768.0
        elif peak > 1.0:          # 轻微过载，归一化后再削顶
            a = a / peak
    a = np.clip(a, -1.0, 1.0)
    pcm = (a * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def build_instruct(emotion: str | None, speed: float | None,
                   degree: str | None = None) -> str | None:
    """把脚本里的情绪、语速与情绪档位折成自然语言指令。

    Qwen3-TTS 没有 speed 参数，但它吃自然语言 —— 这是它比老式 TTS 强的地方。
    情绪只认 INSTRUCT_EMOTIONS 里的真情绪；语篇功能标签不转（见那里的注释），
    否则整期会灌进一堆「用过渡的语气说」这样的噪音，把真情绪也盖住。

    `degree` 是文体情绪档位，取值与上游 `paradigms.EMOTION_LEVELS` 同源，
    它管两件事：

    - **`none`（不写心情的文体，卡上不写、写错也落这一档）不发情绪指令。**
      上游把「平静」当「不加修饰」的代码字在用（`emotion` 是必填，词表里恰好
      没有表示「无」的合法值），所以这一档的稿子里仍会出现真情绪词；档位在
      这一跳把它们拦掉，代码才等于上游文档写的「不贴语气指令」。语篇标签本来
      就不转，这条分支把它们一并覆盖。
    - `light` 加「略带」——实测里这一档能把句与句之间的起伏收窄；「明显/非常」
      方向会反转（同一个词在肯定句上抬高、在中性句上压低），不进产品。
      其余值（含没传）按「不加『略带』」处理，不静默当成 `light`。

    语速不归档位管：它只由上游的 `speed` 决定（主程序送的是恒 1.0，变速在本地
    用 atempo 做），所以 `none` 档若哪天收到非 1.0 的语速，仍会拼出语速那句。
    措辞表只在这一处，上游给的是档位名，不是句子。
    """
    parts: list[str] = []
    if emotion and degree != "none":
        emo = str(emotion).strip()
        if emo in INSTRUCT_EMOTIONS:
            lead = "用略带%s的语气说" if degree == "light" else "用%s的语气说"
            parts.append(lead % emo)
    if speed is not None:
        try:
            s = float(speed)
        except (TypeError, ValueError):
            s = 1.0
        if s <= 0.85:
            parts.append("语速放慢一些")
        elif s >= 1.15:
            parts.append("语速稍快一些")
    return "，".join(parts) if parts else None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

ENGINE: Engine | None = None
ARGS: argparse.Namespace | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "podcast-tts/0.1"

    def log_message(self, fmt, *a):  # 静音默认访问日志，改成统一格式
        if ARGS and getattr(ARGS, "verbose", False):
            sys.stderr.write("[http] " + fmt % a + "\n")

    # -- 工具 ------------------------------------------------------------- #

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- 路由 ------------------------------------------------------------- #

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health"):
            eng = ENGINE
            info = {
                "ok": True,
                "service": "podcast-tts",
                "model": ARGS.model if ARGS else None,
                # base 变体要参考音频，custom_voice 要音色名。界面据此决定显示哪一栏。
                "model_kind": (eng.kind if eng else
                               model_kind(ARGS.model if ARGS else "")),
                "prefer_device": ARGS.device if ARGS else None,
                "loaded": bool(eng and eng._model is not None),
                "backend": (eng.backend if eng else ""),
                "device": (eng.actual_device() if eng else "未加载"),
                "device_note": (eng.device_note if eng else ""),
                "load_seconds": (eng.load_seconds if eng else None),
                "vram_free_mb": free_vram_mb(),
                "vram_used_mb": used_vram_mb(),
                "vram_need_mb": VRAM_NEED_MB + VRAM_SAFETY_MB,
                "last_error": (eng.last_error if eng else None),
            }
            return self._json(200, info)

        if path == "/speakers":
            kind = (ENGINE.kind if ENGINE
                    else model_kind(ARGS.model if ARGS else ""))
            builtin = {"speakers": [s["name"] for s in SPEAKERS],
                       "detail": [dict(s) for s in SPEAKERS]}
            if kind == "base":
                # Base 变体的音色表是空的 —— 它的音色不在模型里，在参考音频里。
                # `speakers` 如实报空（这里是「现在能用的」），`builtin` 给出另一
                # 变体可用的音色名：录参考音频时要挑一个，那条路才认这些名字。
                # 两者分开报，界面就不会把「录参考音频用」误当成「合成可用」。
                return self._json(200, {
                    "speakers": [],
                    "detail": [],
                    "builtin": builtin["detail"],
                    "model_kind": kind,
                    "need_reference": True,
                    "note": "Base 变体没有内置音色，音色来自 ref_audio。"
                            "参考音频由 make_voice.py 生成，随项目归档在「音色」目录；"
                            "builtin 里那些名字是录参考音频时可挑的内置音色。",
                })
            return self._json(200, {
                **builtin,
                "model_kind": kind,
                "need_reference": False,
                "note": "九个开箱音色，无需参考录音。中文推荐 "
                        "Serena（温润女声）/ Uncle_Fu（低沉男声）",
            })

        return self._json(404, {"ok": False, "error": f"没有这个地址：{self.path}"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path not in ("/tts", "/v1/audio/speech"):
            return self._json(404, {"ok": False, "error": f"没有这个地址：{self.path}"})

        body = self._read_json()
        # 两个入口的字段名不同，统一归一化
        if path == "/v1/audio/speech":
            text = body.get("input") or body.get("text") or ""
            voice = body.get("voice") or ""
        else:
            text = body.get("text") or body.get("input") or ""
            voice = body.get("voice") or ""

        text = str(text).strip()
        if not text:
            return self._json(400, {"ok": False, "error": "text/input 不能为空"})

        speaker = str(voice).strip() or ZH_SPEAKERS[0]
        instruct = body.get("instruct") or build_instruct(
            body.get("emotion"), body.get("speed"), body.get("degree"))
        language = str(body.get("language") or "Chinese")

        # 参考音频：给了就走克隆。两个必须挡在前面的检查 ——
        # 剧本里少一句是听得出来的，音频路径写错却是「合成到一半才发现」。
        ref_audio = str(body.get("ref_audio") or "").strip()
        ref_text = str(body.get("ref_text") or "").strip()
        if ref_audio:
            if not os.path.isfile(ref_audio):
                return self._json(400, {
                    "ok": False,
                    "error": f"参考音频不存在：{ref_audio}"})
            if not ref_text:
                return self._json(400, {
                    "ok": False,
                    "error": "走参考音频克隆时必须给 ref_text（ICL 要拿这段转录"
                             "当「示例台词」，缺了它参考音频的内容会串进结果）。"})
            # 种子由内容派生：同一份参考音频 + 同一段文本，永远合成出同一条波形。
            # 这里用音频的内容指纹而不是路径 —— 路径随项目变，指纹不随。
            voice_key = voice_key_for(ref_audio)
        else:
            voice_key = speaker
        seed = seed_for(text, voice_key)

        t0 = time.time()
        retried = ""
        try:
            if ENGINE is None:
                raise RuntimeError("引擎未初始化")
            # 模型偶尔会落进重复循环：一句三十几个字的话念到生成上限才停。种子由
            # 文本派生，所以「哪一句炸」是确定的 —— 原样重跑还是同一条。于是这里
            # 不是重试，而是换一条随机路径：先换种子，再退到不给语气指令；三试都
            # 还炸就如实报错，不静默返回一段噪声。
            for attempt, (sd, ins, why) in enumerate((
                    (seed, instruct, ""),
                    (seed + 1, instruct, "换种子"),
                    (seed, None, "去掉语气"))):
                samples, sr = ENGINE.synth_one(
                    text, speaker, instruct=ins, language=language, seed=sd,
                    ref_audio=(ref_audio or None), ref_text=(ref_text or None))
                dur = len(samples) / float(sr)
                if not looks_degenerate(dur, text):
                    if why:
                        seed, instruct, retried = sd, ins, "（%s后成功）" % why
                    break
                if attempt == 2:
                    raise RuntimeError(
                        "这一句生成失控：三次尝试都跑满 %d 帧上限（约 %.0f 秒）。"
                        "换一个音色，或把这句话改写短一些。"
                        % (max_frames_for(text), max_frames_for(text) / 12.0))
                sys.stderr.write(
                    "[合成] 第 %d 句失控（%.1fs，上限 %.1fs）→ %s重试\n"
                    % (attempt + 1, dur, max_frames_for(text) / 12.0,
                       ("换种子" if attempt == 0 else "去掉语气")))
            wav = to_wav_bytes(samples, sr)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc(limit=6)
            sys.stderr.write(tb + "\n")
            return self._json(500, {"ok": False, "error": str(e), "trace": tb})

        secs = time.time() - t0
        dur = len(wav) / (sr * 2.0)
        if ARGS and not getattr(ARGS, "quiet", False):
            # seed 与语气都记下来：出了问题要能拿同样的输入复现同一条波形；
            # 只报一句「第 N 句不好听」，是查不动的。克隆时把角色与音频名一起记，
            # 因为「音色」此刻是两个文件而不是一个字符串。
            note = f" · 语气 {instruct}" if instruct else ""
            who = (f"克隆 {os.path.basename(os.path.dirname(ref_audio))}/"
                   f"{os.path.basename(ref_audio)}" if ref_audio else speaker)
            sys.stderr.write(
                f"[合成] {dur:5.2f}s 音频 / {secs:5.2f}s 耗时 · {who} · "
                f"{len(text)} 字 · seed {seed} · {ENGINE.actual_device()}"
                f"{note}{retried}\n")
        self._send(200, wav, "audio/wav")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    global ENGINE, ARGS

    ap = argparse.ArgumentParser(
        description="本地 TTS 薄服务（Qwen3-TTS / CUDA 图）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"),
                    help="auto=够显存就用 GPU，不够退 CPU（默认）；cuda=强制要 GPU，"
                         "但显存不足时仍会退 CPU 以免挤掉别人；cpu=强制 CPU")
    ap.add_argument("--preload", action="store_true",
                    help="启动时就把模型加载好（默认懒加载，第一个请求才加载）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ARGS = ap.parse_args()
    ARGS.model = resolve_model(ARGS.model)

    def log(msg: str):
        if not ARGS.quiet:
            sys.stderr.write(msg + "\n")

    log("=" * 62)
    log("本地 TTS 薄服务 · Qwen3-TTS")
    log(f"  模型    {ARGS.model}")
    _kind = model_kind(ARGS.model)
    _note = ("（音色来自 ref_audio，随项目归档）" if _kind == "base"
             else "（内置音色表，靠 voice 字段选）" if _kind == "custom_voice"
             else "（变体未知，按内置音色处理）")
    log(f"  变体    {_kind} {_note}")
    log(f"  设备    {ARGS.device}（auto：够显存上 GPU，不够退 CPU）")
    log(f"  监听    http://{ARGS.host}:{ARGS.port}")
    log(f"  显存    空闲 {free_vram_mb()}MB / 需要 {VRAM_NEED_MB + VRAM_SAFETY_MB}MB")
    log("  接口    POST /tts · POST /v1/audio/speech · GET /health")
    log("=" * 62)

    ENGINE = Engine(ARGS.model, ARGS.device, log)

    if ARGS.preload:
        try:
            ENGINE.ensure()
        except Exception as e:  # noqa: BLE001
            log(f"[警告] 预加载失败：{e}")

    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("\n收到中断，退出。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
