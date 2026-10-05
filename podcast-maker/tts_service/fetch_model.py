#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 Qwen3-TTS 权重。

国内优先 ModelScope，其次 HF 镜像，最后官方 HF —— 一条断了自动换下一条。
下载到 tts_service/models/<模型名>/，重复执行会断点续传，不会重下。

默认下**两个**变体，因为它们在本项目里各有各的活：CustomVoice 负责给每个新项目
录角色参考音频（一次性），Base 负责整期量产。少一个流程就跑不通。要单独下某一个
用 `--model <仓库 id>`，可重复。
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(HERE, "models")

DEFAULT_MODELS = ("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                  "Qwen/Qwen3-TTS-12Hz-1.7B-Base")


def try_modelscope(model_id: str, target: str) -> bool:
    try:
        from modelscope import snapshot_download  # type: ignore
    except ImportError:
        print("  [1/3] ModelScope 没装，跳过")
        return False
    print(f"  [1/3] ModelScope 下载 {model_id} …")
    try:
        snapshot_download(model_id, local_dir=target)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"        失败：{type(e).__name__}: {e}")
        return False


def try_hf_mirror(model_id: str, target: str) -> bool:
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    return _hf(model_id, target, "HF 镜像 (hf-mirror.com)", "2/3")


def try_hf_official(model_id: str, target: str) -> bool:
    os.environ.pop("HF_ENDPOINT", None)
    return _hf(model_id, target, "官方 HF", "3/3")


def _hf(model_id: str, target: str, label: str, idx: str) -> bool:
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError:
        print(f"  [{idx}] huggingface_hub 没装，跳过")
        return False
    print(f"  [{idx}] {label} 下载 {model_id} …")
    try:
        snapshot_download(
            repo_id=model_id, local_dir=target,
            resume_download=True, max_workers=4,
            ignore_patterns=["*.md", "*.txt", "*.png", "*.jpg"],
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(f"        失败：{type(e).__name__}: {e}")
        return False


def verify(target: str) -> bool:
    """确认下到的东西像不像一个能用的模型。"""
    if not os.path.isdir(target):
        return False
    files = os.listdir(target)
    heavy = [f for f in files if f.endswith((".safetensors", ".bin", ".pt"))]
    config = [f for f in files if f.startswith("config") or f.endswith(".json")]
    total = sum(os.path.getsize(os.path.join(target, f))
                for f in files if os.path.isfile(os.path.join(target, f)))
    print(f"\n  目录 {target}")
    print(f"  权重文件 {len(heavy)} 个 · 配置 {len(config)} 个 · 合计 {total / 1e9:.2f} GB")
    for f in sorted(os.listdir(target))[:14]:
        p = os.path.join(target, f)
        if os.path.isfile(p):
            print(f"    {os.path.getsize(p) / 1e6:10.1f} MB  {f}")
    return bool(heavy)


def download_one(model_id: str, out: str = "") -> int:
    """下一个模型。已齐则跳过；三条源都失败返回 1。"""
    target = out or os.path.join(MODELS_DIR, model_id.split("/")[-1])
    os.makedirs(target, exist_ok=True)

    print("=" * 66)
    print(f"下载模型 {model_id}")
    print(f"    目标 {target}")
    print("=" * 66)

    if verify(target):
        print("\n  已经有齐全的权重，跳过下载。")
        return 0

    for step in (try_modelscope, try_hf_mirror, try_hf_official):
        try:
            if step(model_id, target):
                if verify(target):
                    print("\n  下载完成。")
                    return 0
                print("        下完了但看着不齐，继续试下一条源。")
        except Exception:  # noqa: BLE001
            traceback.print_exc(limit=3)

    print("\n  [失败] 三条源都没成功。")
    print("  可以手工下载后放到：")
    print(f"      {target}")
    print("  再从 ModelScope / hf-mirror 手动拉也行。")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 Qwen3-TTS 权重")
    ap.add_argument("--model", action="append", default=[],
                    help="仓库 id，可重复。不传就下全部必需模型")
    ap.add_argument("--out", default="",
                    help="自定义落盘目录，仅单个 --model 时有意义")
    args = ap.parse_args()

    models = args.model or list(DEFAULT_MODELS)
    if args.out and len(models) > 1:
        print("[ERROR] --out 只能配一个 --model 用。")
        return 2

    bad = [mid for mid in models if download_one(mid, args.out) != 0]
    if bad:
        print("\n[失败] 以下模型没下成：%s" % "、".join(bad))
        return 1
    print("\n全部就绪：%s" % "、".join(m.split("/")[-1] for m in models))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
