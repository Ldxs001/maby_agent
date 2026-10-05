#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：主程序 tts_engine <-> 本地 Qwen3-TTS 服务。

验四件事
--------
1. 服务没起时：可用性探测必须报「不可用」，音色枚举必须报错
   —— 不许假装可用，也不许拿写死的假清单顶包。
2. 服务起来后：探测转正，音色表来自服务端。
3. 合成：A/B 两句对话真出 wav，实测时长回填进脚本项。
4. 开关 tts.unload_llm_before_synth：开则调用腾显存，关则一次都不调。

用法（用系统 python 即可，主程序不依赖 torch）：
    python tools/probes/e2e_tts_wire.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke", "_scratch")
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from podcast_maker import tts_engine  # noqa: E402

PORT = 9880
PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  —— " + detail) if detail else ""))
    return cond


def section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def service_alive(port=PORT, timeout=3):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def wait_up(proc, port=PORT, limit=180):
    t0 = time.time()
    while time.time() - t0 < limit:
        info = service_alive(port)
        if info:
            return info
        if proc and proc.poll() is not None:
            return None
        time.sleep(1.0)
    return None


def main() -> int:
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    print("引擎        %s" % cfg.get("tts.engine"))
    print("服务地址    %s:%s" % (cfg.get("tts.qwen3tts_host"), cfg.get("tts.qwen3tts_port")))
    print("A/B 音色    %s / %s" % (cfg.get("tts.qwen3tts_voice_a"),
                                   cfg.get("tts.qwen3tts_voice_b")))
    print("合成前腾显存 %s" % cfg.get("tts.unload_llm_before_synth"))

    # ---------------------------------------------------------------- 1
    section("一、服务未起时：必须 fail-closed")
    owned_proc = None
    logf = None
    info = service_alive()
    if info:
        print("  （检测到 9880 已有服务在跑，直接复用；不另起进程）")
    else:
        ok, msg = tts_engine.engine_available("qwen3tts", cfg)
        check("服务未起 → 探测报不可用", not ok, msg)
        try:
            tts_engine.list_voices("qwen3tts", refresh=True, cfg=cfg)
            check("服务未起 → 音色枚举报错", False, "竟然返回了列表")
        except tts_engine.TTSError as e:
            check("服务未起 → 音色枚举报错", True, str(e)[:80])

        # ------------------------------------------------------------ 2
        section("二、起服务")
        venv_py = os.path.join(ROOT, "tts_service", ".venv", "Scripts", "python.exe")
        serve = os.path.join(ROOT, "tts_service", "serve.py")
        log_path = os.path.join(ROOT, "tts_service", "_e2e_server.log")
        logf = open(log_path, "w", encoding="utf-8")
        print("  $ %s %s --port %d" % (venv_py, serve, PORT))
        owned_proc = subprocess.Popen(
            [venv_py, serve, "--port", str(PORT), "--device", "cuda"],
            stdout=logf, stderr=subprocess.STDOUT,
            cwd=os.path.join(ROOT, "tts_service"))
        info = wait_up(owned_proc)
        if not check("服务在 180 秒内就绪", bool(info),
                     (info or {}).get("prefer_device", "") if info else "超时"):
            _teardown(owned_proc, logf)
            return _report()

    ok, msg = tts_engine.engine_available("qwen3tts", cfg)
    check("服务在线 → 探测转正", ok, msg)

    try:
        voices = tts_engine.list_voices("qwen3tts", refresh=True, cfg=cfg)
        names = [v["name"] for v in voices]
        check("音色表来自服务端（9 个）", len(voices) == 9, "实得 %d：%s" % (len(voices), names))
        check("音色带描述（界面下拉可读）", all(v.get("note") for v in voices),
              voices[0].get("label", "") if voices else "")
    except tts_engine.TTSError as e:
        check("音色枚举成功", False, str(e))

    # ---------------------------------------------------------------- 3
    section("三、开关 tts.unload_llm_before_synth")
    calls = []
    real_unload = tts_engine.unload_llm_models

    def spy(log=None):
        calls.append(1)
        return {"unloaded": [], "skipped": ["（本次为验证开关，未真卸）"], "failed": []}

    tts_engine.unload_llm_models = spy
    try:
        tmp = tempfile.mkdtemp(prefix="tts_switch_")
        off = dict(cfg, **{"tts.unload_llm_before_synth": False})
        tts_engine.synthesize([], tmp, off, log=lambda m: None)
        check("开关关闭 → 一次都不调腾显存", len(calls) == 0, "调了 %d 次" % len(calls))

        calls.clear()
        on = dict(cfg, **{"tts.unload_llm_before_synth": True})
        tts_engine.synthesize([], tmp, on, log=lambda m: None)
        check("开关打开 → 调用腾显存", len(calls) == 1, "调了 %d 次" % len(calls))
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        tts_engine.unload_llm_models = real_unload

    # ---------------------------------------------------------------- 4
    section("四、真合成两句（A/B）")
    work = tempfile.mkdtemp(prefix="tts_e2e_")
    script = [
        {"speaker": "A", "text": "这一期我们来聊一件反直觉的事。"},
        {"speaker": "B", "text": "你是说，把话说得越满，反而越不可信？"},
    ]
    logs = []
    t0 = time.time()
    try:
        res = tts_engine.synthesize(script, work, cfg, log=logs.append)
    except tts_engine.TTSError as e:
        check("合成成功", False, str(e))
        _teardown(owned_proc, logf)
        return _report()

    elapsed = time.time() - t0
    dur = res["durations"]
    check("产出 2 个 wav", len(res["files"]) == 2, str([os.path.basename(f) for f in res["files"]]))
    check("每句时长 > 0", all(d > 0 for d in dur), "%.2f / %.2f 秒" % tuple(dur))
    check("时长回填进脚本项",
          all(abs(item.get("actual_seconds", -1) - d) < 0.01 for item, d in zip(script, dur)),
          str([item.get("actual_seconds") for item in script]))
    check("A/B 用了不同音色文件", res["files"][0].endswith("_A.wav")
          and res["files"][1].endswith("_B.wav"))

    print("\n  合成耗时    %.1f 秒" % elapsed)
    print("  实测时长    %.2f / %.2f 秒" % tuple(dur))
    print("  落地目录    %s" % res["audio_dir"])
    for m in logs:
        print("  日志        %s" % m)

    _teardown(owned_proc, logf)
    return _report()


def _teardown(proc, logf):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
    if logf:
        logf.close()


def _report():
    print("\n" + "=" * 68)
    print("结果  通过 %d · 失败 %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  失败项：%s" % f)
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
