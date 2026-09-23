#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「选择…」的后端管路取证：不弹对话框，照样把整条路走一遍。

做法：给子进程塞一个假的 tkinter（PYTHONPATH 垫在前面），假的
filedialog.askopenfilename 把收到的 title / filetypes 写到 stderr，并按
PM_FAKE_PICK 返回一个路径。于是「argv 拼得对不对、文件过滤表有没有传下去、
子进程 stdout 上的路径有没有被读回来、取消算不算错误」全部可核——而屏幕上
不会弹出任何窗口。

用法：python tools/probes/pick_probe.py
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from podcast_maker import web_ui                                   # noqa: E402

FAKE_TK = '''\
class Tk(object):
    def __init__(self):
        pass
    def withdraw(self):
        pass
    def attributes(self, *a):
        pass
    def destroy(self):
        pass
'''

FAKE_FD = '''\
import json, os, sys


def askopenfilename(title=None, filetypes=None):
    sys.stderr.write("HOOK " + json.dumps(
        {"title": title, "filetypes": filetypes}, ensure_ascii=False) + "\\n")
    return os.environ.get("PM_FAKE_PICK", "")
'''


def make_shim():
    tmp = tempfile.mkdtemp(prefix="pm_faketk_")
    pkg = os.path.join(tmp, "tkinter")
    os.makedirs(pkg)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(FAKE_TK)
    with open(os.path.join(pkg, "filedialog.py"), "w", encoding="utf-8") as fh:
        fh.write(FAKE_FD)
    return tmp


def main():
    shim = make_shim()
    seen = []
    real_run = web_ui.subprocess.run

    def spy(args, **kw):
        r = real_run(args, **kw)
        seen.append((list(args), dict(kw), r))
        return r

    web_ui.subprocess.run = spy
    old_env = {k: os.environ.get(k) for k in ("PYTHONPATH", "PM_FAKE_PICK")}
    os.environ["PYTHONPATH"] = shim
    os.environ["PM_FAKE_PICK"] = r"D:\素材\A角 立绘.png"     # 带中文与空格
    ok = True
    try:
        r = web_ui.pick_file("A 角立绘 PNG", "image")
        argv, kw, proc = seen[-1]
        print("返回：%r" % (r,))
        print("argv[0..1]：%s" % " ".join(argv[:2]))
        print("传给对话框的过滤表：%s" % " ".join(argv[3:]))
        print("子进程 stderr：%s" % (proc.stderr or "").strip())
        print("子进程 stdout：%r" % (proc.stdout,))

        checks = [
            ("路径原样回传（中文+空格不丢）", r.get("path") == r"D:\素材\A角 立绘.png"),
            ("没被当成取消", r.get("cancelled") is False),
            ("标题传到对话框", "A 角立绘 PNG" in (proc.stderr or "")),
            ("图片过滤表传到对话框", "*.png" in (proc.stderr or "")
             and "图片" in (proc.stderr or "")),
            ("子进程 stdout 编成 utf-8", kw.get("encoding") == "utf-8"
             and kw.get("env", {}).get("PYTHONIOENCODING") == "utf-8"),
            ("走的是当前解释器", argv[0] == sys.executable),
        ]

        # 取消：对话框直接关掉 → 正常返回，不是错误
        os.environ["PM_FAKE_PICK"] = ""
        r2 = web_ui.pick_file("片头音频", "audio")
        print("取消时返回：%r" % (r2,))
        checks.append(("取消不算错误", r2.get("ok") and r2.get("cancelled")))

        # 没有图形接口：假 tkinter 换成 import 就炸的那种
        os.environ["PM_FAKE_PICK"] = ""
        bad = os.path.join(shim, "tkinter", "__init__.py")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("raise ImportError('no display')\n")
        r3 = web_ui.pick_file("片头音频", "audio")
        print("无图形接口时返回：%r" % (r3,))
        checks.append(("环境缺图形接口时给人话不抛出",
                       r3.get("ok") is False and "选择" in r3.get("error", "")
                       or "手填" in r3.get("error", "")))

        for label, good in checks:
            print("  %s %s" % ("PASS" if good else "FAIL", label))
            ok = ok and good
    finally:
        web_ui.subprocess.run = real_run
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(shim, ignore_errors=True)
    print("结论：%s" % ("全部通过" if ok else "有失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
