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

"""Podcast Maker 入口。

用法：
    python main.py                             启动 Web 界面（默认端口 8811）
    python main.py --port 8820                 指定端口
    python main.py --check                     仅检测 LLM 后端连接
    python main.py --continue <树根> --episode 3   从锁文件恢复续跑第 3 期
    python main.py --voices                    列出可用音色
    python main.py --fonts                     列出可用字体

`--continue` 收的是**树根**（项目目录，或单集的目录），期号由 `--episode` 给；
没给时只有「恰好一期卡在门禁上」才自动定，其余一律把选择权交回去。
"""

import argparse
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def build_parser():
    p = argparse.ArgumentParser(
        description="Podcast Maker · 播客制作智能体（脚本 / 声音 / 字幕 / 画面）")
    p.add_argument("--port", default="8811", help="Web 界面端口，传 auto 自动选空闲端口")
    p.add_argument("--host", default="0.0.0.0", help="监听地址")
    p.add_argument("--pidfile", default="server.pid", help="PID 文件路径")
    p.add_argument("--check", action="store_true", help="仅检测 LLM 后端连接后退出")
    p.add_argument("--continue", dest="resume", default="",
                   help="从指定树根（项目目录）续跑；期号见 --episode")
    p.add_argument("--episode", default="", help="续跑的期号；省略时按锁文件反推")
    p.add_argument("--voices", action="store_true", help="列出可用音色后退出")
    p.add_argument("--fonts", action="store_true", help="列出可用字体后退出")
    p.add_argument("--backend", default=None,
                   choices=["lm-studio", "ollama", "custom"], help="LLM 后端")
    p.add_argument("--base-url", default="", help="LLM API 地址")
    p.add_argument("--api-key", default="", help="LLM API Key")
    p.add_argument("--model", "-m", default="", help="模型名")
    return p


def _cfg():
    from podcast_maker.config_manager import ConfigManager
    return ConfigManager()


def cmd_check():
    from podcast_maker.llm_client import LLMClient
    cfg = _cfg()
    llm = LLMClient(backend=cfg.get("llm.backend"), base_url=cfg.resolve_base_url(),
                    api_key=cfg.get("llm.api_key"), model=cfg.get("llm.model"),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)),
                    input_ratio=float(cfg.get("llm.input_ratio", 1.0)))
    ok, msg = llm.test_connection()
    print("  [%s] %s" % ("OK" if ok else "FAIL", msg))
    return 0 if ok else 1


def cmd_voices():
    from podcast_maker import tts_engine
    cfg = _cfg()
    engine = cfg.get("tts.engine", "edge")
    ok, msg = tts_engine.engine_available(engine)
    print("  引擎 %s：%s" % (engine, msg))
    if not ok:
        return 1
    for v in tts_engine.list_voices(engine):
        print("  %-34s %s" % (v["name"], v.get("note", "")))
    return 0


def cmd_fonts():
    from podcast_maker import assets_factory
    fonts = assets_factory.list_fonts()
    if not fonts:
        print("  未找到可用中文字体。请把字体放入 assets/fonts/")
        return 1
    for f in fonts:
        print("  %-28s %-10s %s" % (f["family"], f["source"], f["path"]))
    return 0


def _blocked_episode(root, layout, script_engine):
    """没给期号时，从锁文件反推要续哪一期。

    只有**恰好一个**锁文件才自动定；零个或多个都把选择权交回去——猜错了就是拿着
    另一期的产物当这一期的接着做，而这种错在产物上看不出来。
    """
    tmp = os.path.join(root, layout.DIR_TMP)
    if not os.path.isdir(tmp):
        print("  这个目录里没有「%s」子目录，看不出要续哪一期；请加 --episode <期号>"
              % layout.DIR_TMP)
        return ""
    blocked = sorted(d for d in os.listdir(tmp)
                     if os.path.isdir(os.path.join(tmp, d))
                     and os.path.exists(script_engine.lock_path(os.path.join(tmp, d))))
    if len(blocked) == 1:
        return blocked[0]
    if not blocked:
        print("  没找到锁文件（没有哪一期卡在门禁上），请用 --episode <期号> 指明续哪一期")
        return ""
    print("  有 %d 期卡在门禁上，请用 --episode 指明：%s"
          % (len(blocked), "、".join(blocked)))
    return ""


def cmd_resume(args):
    from podcast_maker import duration_model, layout, pipeline, script_engine
    from podcast_maker.llm_client import LLMClient
    cfg = _cfg()
    calib = duration_model.Calibration()
    llm = LLMClient(backend=cfg.get("llm.backend"), base_url=cfg.resolve_base_url(),
                    api_key=cfg.get("llm.api_key"), model=cfg.get("llm.model"),
                    timeout=int(cfg.get("llm.timeout", 3600)),
                    idle_timeout=int(cfg.get("llm.idle_timeout", 300)),
                    input_ratio=float(cfg.get("llm.input_ratio", 1.0)))
    target = args.resume
    if not os.path.isabs(target):
        target = os.path.join(_SCRIPT_DIR, target)
    if not os.path.isdir(target):
        print("  目录不存在：%s" % target)
        return 1
    root = target
    no = (args.episode or "").strip() or _blocked_episode(root, layout, script_engine)
    if not no:
        return 1
    print("  续跑：%s · 第 %s 期" % (root, no))
    job = pipeline.new_job("continue", "%s · 第 %s 期" % (os.path.basename(root), no))
    try:
        res = pipeline.continue_episode(cfg, calib, root, no, llm=llm, job=job)
    except Exception as e:
        print("  续跑失败：%s" % e)
        return 1
    print("  项目目录：%s" % res["project_dir"])
    rep = res.get("report", {})
    print("  校验：%s" % ("全部通过" if rep.get("passed") else "未通过"))
    return 0 if rep.get("passed") else 1


def main():
    args = build_parser().parse_args()

    print("=" * 62)
    print("  Podcast Maker")
    print("  播客制作智能体 · 脚本 → 声音 → 字幕 → 画面 → 产物")
    print("=" * 62)
    print()

    if args.check:
        return cmd_check()
    if args.voices:
        return cmd_voices()
    if args.fonts:
        return cmd_fonts()
    if args.resume:
        return cmd_resume(args)

    # 命令行覆盖（不落盘，优先于 config.json）
    cfg = _cfg()
    if args.backend:
        cfg.update({"llm.backend": args.backend})
    if args.base_url:
        cfg.update({"llm.base_url": args.base_url})
    if args.api_key:
        cfg.update({"llm.api_key": args.api_key})
    if args.model:
        cfg.update({"llm.model": args.model})
    cfg.save()

    from podcast_maker.web_ui import run_server
    port = args.port
    if port == "auto":
        import socket
        with socket.socket() as s:
            s.bind(("", 0))
            port = str(s.getsockname()[1])
    run_server(host=args.host, port=int(port), pidfile=args.pidfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
