#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收脚本：本机能不能把 Qwen3-TTS 跑通。

它做什么
--------
1. 只读看一眼当前环境：显卡空闲多少、LM Studio 正在跑什么、torch/CUDA 是否可用。
2. 起薄服务（serve.py）→ 打 /health → 打 /tts 合成一句中文 → 落成 wav 文件。
3. 报出：跑在哪个设备、加载耗时、合成耗时、音频时长、实时倍率。

它不做什么
----------
**不卸载、不重启、不改动你的 LM Studio。** 全程只读查询。
显存不够时判定会退 CPU，但那条兜底目前实际走不通（见 `README.md` 第三节）。

用法
----
    .venv\\Scripts\\python.exe check.py
    .venv\\Scripts\\python.exe check.py --text "自定义正文" --voice Serena

解释器用本服务自己的 .venv —— 那份环境由 `setup_env.py` 建（配置页的「搭建
本地语音环境」按钮跑的是同一个脚本），与主程序的环境互不干扰。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "_check_out")

DEFAULT_TEXT = (
    "这一期我们来聊一件反直觉的事：把话说得越满，听起来反而越不可信。"
    "留白不是含糊，而是把判断的权利还给听众。"
)


# --------------------------------------------------------------------------- #
# 只读环境速览
# --------------------------------------------------------------------------- #

def sh(cmd: list[str], timeout: int = 8) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"(取不到：{e})"


def show_env() -> dict:
    print("=" * 70)
    print("一、当前环境（全部只读，不动任何东西）")
    print("=" * 70)

    gpu = sh(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
              "--format=csv,noheader"])
    print(f"  显卡      {gpu or '(取不到)'}")

    lms = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe")
    if not os.path.exists(lms):
        lms = os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms")
    ps = sh([lms, "ps"]) if os.path.exists(lms) else "(没装 lms)"
    running = [ln for ln in ps.splitlines()
               if ln.strip() and not ln.upper().startswith("IDENTIFIER")]
    if running:
        print("  LM Studio 正在跑（**本脚本不会碰它**）：")
        for ln in running:
            print(f"      {ln.strip()}")
    else:
        print("  LM Studio 当前没有驻留模型")

    print(f"  Python    {sys.version.split()[0]}")
    try:
        import torch
        avail = torch.cuda.is_available()
        print(f"  torch     {torch.__version__}  ·  CUDA 可用={avail}"
              + (f"  ·  设备={torch.cuda.get_device_name(0)}" if avail else ""))
        torch_info = {"torch": torch.__version__, "cuda": bool(avail)}
    except Exception as e:  # noqa: BLE001
        print(f"  torch     导入失败：{e}")
        torch_info = {"torch": None, "cuda": False}

    try:
        import faster_qwen3_tts
        print(f"  加速包    faster-qwen3-tts {getattr(faster_qwen3_tts, '__version__', '?')}")
    except Exception:  # noqa: BLE001
        print("  加速包    faster-qwen3-tts 未安装（会退回官方实现，慢很多）")

    return torch_info


# --------------------------------------------------------------------------- #
# 起服务 / 收服务
# --------------------------------------------------------------------------- #

def free_port(start: int = 9880) -> int:
    import socket
    for p in range(start, start + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def read_log_tail(path: str, lines: int = 25) -> str:
    """读日志文件的末尾若干行。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            got = fh.read().strip().splitlines()
    except OSError:
        return ""
    return "\n".join(got[-lines:])


def start_server(port: int, model: str, device: str):
    """起服务，输出落进日志文件，返回 (进程, 日志路径, 日志句柄)。

    **不能接管道。** 服务一旦抛异常，`Engine.ensure` 的 except 分支就把几 KB 的
    异常文本写进 stderr，而管道缓冲在 Windows 上只有约 4KB —— 服务阻塞在写、
    这里阻塞在读，双方互等，表现为「卡住好几分钟、一句线索也没有」。落盘没有这个
    问题（文件不设上限），日志还完整可查。主程序的 `_spawn_service` 也是这么做的。
    """
    cmd = [sys.executable, os.path.join(HERE, "serve.py"),
           "--port", str(port), "--model", model, "--device", device]
    print(f"\n  $ {' '.join(cmd)}")
    os.makedirs(OUT_DIR, exist_ok=True)
    log_path = os.path.join(OUT_DIR, "server.log")
    fh = open(log_path, "w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=fh, stderr=subprocess.STDOUT)
    except Exception:  # noqa: BLE001
        fh.close()
        raise
    return proc, log_path, fh


def wait_health(port: int, limit: int = 30) -> dict:
    url = f"http://127.0.0.1:{port}/health"
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError(f"服务 {limit} 秒内没起来")


def post_tts(port: int, payload: dict, timeout: int) -> tuple[bytes, float]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), time.time() - t0


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _kind_of(model: str) -> str:
    """这个模型是哪个变体。判不出来返回空串（不拦，交给服务去报错）。"""
    try:
        import serve  # noqa: PLC0415
        return serve.model_kind(serve.resolve_model(model))
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3-TTS 本机验收")
    ap.add_argument("--model", default="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    help="权重仓库 id 或本地目录。默认 Base 变体 —— 它才是量产"
                         "引擎，音色来自 --ref 指定的参考音频")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--voice", default="Serena",
                    help="内置音色名（Serena/Vivian/Uncle_Fu/…）。只在跑内置音色"
                         "变体时有意义；Base 变体不认这个字段")
    ap.add_argument("--ref", default="",
                    help="参考音频路径。Base 变体必给 —— 它没有内置音色表")
    ap.add_argument("--ref-text", default="",
                    help="参考音频的转录文本。走克隆时必给：ICL 模式拿它当示例台词")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--timeout", type=int, default=900,
                    help="单句合成的等待上限（秒）。CPU 上第一次会很慢，默认给 15 分钟")
    args = ap.parse_args()

    kind = _kind_of(args.model)
    if kind == "base" and not args.ref:
        print("[ERROR] %s 是 Base 变体，没有内置音色，必须给 --ref <参考音频>。" % args.model)
        print("        让脚本自己录一段：")
        print("          python make_voice.py --project-dir <项目目录> --json")
        return 2
    if kind == "base" and not args.ref_text:
        print("[ERROR] 走参考音频克隆必须同时给 --ref-text（参考音频念的那句话）。")
        return 2

    info = show_env()

    print("\n" + "=" * 70)
    print("二、起服务并合成一句")
    print("=" * 70)

    os.makedirs(OUT_DIR, exist_ok=True)
    port = free_port()
    proc, log_path, log_fh = start_server(port, args.model, args.device)

    rc = 0
    try:
        health = wait_health(port)
        print(f"  服务就绪  · 首选设备={health.get('prefer_device')}  ·  "
              f"空闲显存={health.get('vram_free_mb')}MB  ·  门槛={health.get('vram_need_mb')}MB")

        use_ref = bool(args.ref)
        if use_ref:
            print(f"\n  正在合成（克隆 {os.path.basename(args.ref)}，"
                  f"{len(args.text)} 字）…")
        else:
            print(f"\n  正在合成（内置音色 {args.voice}，{len(args.text)} 字）…")
        print("  首次要加载权重，CPU 上可能要几分钟，请耐心——进度会打在下边。")
        t_all = time.time()
        body = {"text": args.text, "voice": args.voice, "speed": 1.0}
        if use_ref:
            body["ref_audio"] = args.ref
            body["ref_text"] = args.ref_text
        wav, elapsed = post_tts(port, body, timeout=args.timeout)

        out = os.path.join(OUT_DIR, "sample.wav")
        with open(out, "wb") as f:
            f.write(wav)

        dur = max(0.0, (len(wav) - 44) / (24000 * 2.0))
        after = wait_health(port)

        print("\n" + "=" * 70)
        print("三、结果")
        print("=" * 70)
        print(f"  跑在哪个设备     {after.get('device')}")
        print(f"  判定说明         {after.get('device_note')}")
        print(f"  用的哪套实现     {after.get('backend')}")
        print(f"  模型加载耗时     {after.get('load_seconds')} 秒")
        print(f"  合成耗时         {elapsed:.1f} 秒（含首次加载）")
        print(f"  音频时长         {dur:.2f} 秒")
        if dur > 0:
            ratio = dur / elapsed
            print(f"  实时倍率         {ratio:.2f}x"
                  f"  （1 = 实时；>1 比实时快，<1 比实时慢）")
        print(f"  音频文件         {out}")
        print(f"  总耗时           {time.time() - t_all:.1f} 秒")
        print(f"  显存占用后       {after.get('vram_used_mb')}MB"
              f"（空闲 {after.get('vram_free_mb')}MB）")
        print("\n  提示：LM Studio 全程没有被碰过。")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"\n  [失败] 服务返回 {e.code}：{body[:600]}")
        rc = 1
    except Exception as e:  # noqa: BLE001
        print(f"\n  [失败] {e}")
        rc = 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        log_fh.close()
        tail = read_log_tail(log_path)
        if tail:
            print("\n" + "-" * 70)
            print("服务日志：")
            for ln in tail.splitlines():
                print("  " + ln)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
