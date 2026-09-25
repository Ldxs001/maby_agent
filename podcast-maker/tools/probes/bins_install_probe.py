#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验收：一键装出来的 ffmpeg，四个模块能不能**真的引用到**、**真的能用**。

主人问的就是这件事 ——「安装后，现在的机制能够正确引用到这个 8.1.3 吗？」
所以这里**什么都不 mock**：真下载、真校验、真解压、真把 PATH 清空、真跑 ffmpeg。

关键在第 3、4 步：把 PATH 抹成空串，模拟「用户机器上什么都没装」——
这正是分发出去之后的常态。若四个模块仍能拿到它，才说明收口真的成立。

全程在临时目录里跑：**不动项目 bin/、不留二进制残留**。
"""
import os
import shutil
import subprocess
import sys
import tempfile

# 本文件住在 tools/probes/ 下 —— 上溯三层才是项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from podcast_maker import bins  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    tail = ("  → %s" % str(extra)[:150]) if extra else ""
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, tail))


def main():
    tmp = tempfile.mkdtemp(prefix="probe-bins-")
    saved_path = os.environ.get("PATH", "")
    try:
        bins.BIN_DIR = tmp

        print("== 1. 真下载 + 真校验 + 真解压（装临时目录，不碰项目 bin/）==")
        steps = []
        st = bins.install(lambda m: print("   " + m), on_step=steps.append)
        check("install 返回 ok", st["ok"])

        print("\n== 2. 装出来的东西 ==")
        for n in bins.TOOLS:
            p = os.path.join(tmp, n + bins.EXE_SUFFIX)
            print("   %-8s %s（%d B）" % (n, p, os.path.getsize(p)))
        # 这里**故意不验版本**：此刻 PATH 还在，install() 内部那次 state() 报的
        # 是 PATH 里那份（9.0.1）—— 这是设计如此（PATH 优先），不是缺陷。
        # 「装出来的到底是不是 8.1.3、能不能被引用」由第 3 步（PATH 已清空）来验。
        print("   （此刻 state 报的是 PATH 那份：%s —— 设计如此，PATH 优先）"
              % st["tools"]["ffmpeg"]["version"].split(" Copyright")[0])
        check("两件工具都落在临时 bin/ 里",
              all(os.path.isfile(os.path.join(tmp, n + bins.EXE_SUFFIX))
                  for n in bins.TOOLS))
        check("进度只增不减", all(a <= b for a, b in zip(steps, steps[1:])),
              "%d 个进度点" % len(steps))

        print("\n== 3. 抹掉 PATH —— 模拟「用户机器上什么都没装」==")
        os.environ["PATH"] = ""
        ff = bins.locate("ffmpeg")
        fp = bins.locate("ffprobe")
        check("PATH 空时 locate 仍命中", bool(ff and fp))
        check("命中的是 bin/ 里那份",
              bool(ff) and os.path.normcase(ff).startswith(os.path.normcase(tmp)), ff)
        st2 = bins.state()
        check("state 报 source=bin", st2["tools"]["ffmpeg"]["source"] == "bin",
              st2["tools"]["ffmpeg"]["source"])
        check("state 报的版本就是刚装的那个",
              "8.1.3" in st2["tools"]["ffmpeg"]["version"])

        print("\n== 4. 四个模块在 PATH 空时还能不能拿到它（收口是否真的成立）==")
        from podcast_maker import (aigc_label, audio_engine,  # noqa: F401
                                   tts_engine, video_engine)
        for mod, fn in ((audio_engine, "ffmpeg_bin"),
                        (tts_engine, "ffmpeg_bin"),
                        (video_engine, "ffmpeg_bin"),
                        (tts_engine, "ffprobe_bin"),
                        (video_engine, "ffprobe_bin")):
            short = "%s.%s()" % (mod.__name__.split(".")[-1], fn)
            try:
                got = getattr(mod, fn)()
                check(short, os.path.normcase(got).startswith(os.path.normcase(tmp)),
                      got)
            except Exception as e:  # noqa: BLE001
                check(short, False, e)

        print("\n== 5. 真跑：这 8.1.3 到底能不能干活 ==")
        wav = os.path.join(tmp, "t.wav")
        r = subprocess.run([ff, "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", wav],
                           capture_output=True)
        check("ffmpeg 生成 wav（退出码 0）", r.returncode == 0,
              (r.stderr or b"")[-150:])

        r2 = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", wav],
                            capture_output=True, text=True)
        dur = (r2.stdout or "").strip()
        check("ffprobe 读出时长 ≈ 1 秒", dur.startswith("1.0"), dur)

        ass = os.path.join(ROOT, "projects", "20260911-113022", "sub.ass")
        if os.path.isfile(ass):
            png = os.path.join(tmp, "frame.png")
            r3 = subprocess.run(
                [ff, "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1",
                 "-vf", "ass=sub.ass", "-frames:v", "1", png],
                capture_output=True, cwd=os.path.dirname(ass))
            check("libass 烧中文字幕（退出码 0）", r3.returncode == 0,
                  (r3.stderr or b"")[-170:])
            size = os.path.getsize(png) if os.path.isfile(png) else 0
            check("烧出的帧不是空白（>2KB）", size > 2000, "%d B" % size)
        else:
            print("   （跳过烧字幕：找不到 %s）" % ass)
    finally:
        os.environ["PATH"] = saved_path
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 汇总 ===")
    print("PASS %d / FAIL %d" % (len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  x", f)
    print("RESULT:", "PASS" if not FAILED else "FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
