#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考音频「跨进程是否逐字节可复现」实测。

动因：端到端验证里发现两次独立调用 make_voice.py（同内置音色名、同参考文案）
产出的 ref.wav 不同。产品代码与其 docstring 都声称「逐字节相同」，需要判定
这到底是可复现的进程间差异，还是逐次随机。

矩阵：同一目录、同参数、--force 重建 3 次，每次记 sha256 / 时长 / F0。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
SVC = os.path.join(ROOT, "tts_service")
PY = os.path.join(SVC, ".venv", "Scripts", "python.exe")
SCRATCH = os.path.join(HERE, "_voice_repro")
RUNS = 3


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 16), b""):
            h.update(c)
    return h.hexdigest()


def main():
    if os.path.isdir(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)

    rows = []
    for i in range(RUNS):
        r = subprocess.run(
            [PY, os.path.join(SVC, "make_voice.py"),
             "--project-dir", SCRATCH, "--role", "both",
             "--force", "--json"],
            capture_output=True, cwd=SVC)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        if r.returncode != 0 or '"error"' in out:
            print("run %d 失败：%s" % (i + 1, (err or out)[-400:]))
            return 1
        # 不看 stdout 的 JSON（里面混着进度行），直接读盘上刚落的那份记录，
        # 顺带验证「记录里的 ref_md5 与实际文件一致」。
        row = {"run": i + 1}
        for role in ("A", "B"):
            d = os.path.join(SCRATCH, "音色", role)
            wav = os.path.join(d, "ref.wav")
            rec = json.load(open(os.path.join(d, "profile.json"), encoding="utf-8"))
            real = sha(wav)[:16]
            row[role] = {
                "sha": real,
                "recorded": rec.get("ref_md5"),
                "record_ok": real == rec.get("ref_md5"),
                "sec": rec.get("ref_seconds"),
                "f0": rec.get("f0_med"),
            }
        rows.append(row)
        print("run %d  A %s %ss %sHz %s   B %s %ss %sHz %s"
              % (i + 1, row["A"]["sha"], row["A"]["sec"], row["A"]["f0"],
                 "档案一致" if row["A"]["record_ok"] else "档案不符",
                 row["B"]["sha"], row["B"]["sec"], row["B"]["f0"],
                 "档案一致" if row["B"]["record_ok"] else "档案不符"), flush=True)

    verdict = {}
    for role in ("A", "B"):
        shas = [r[role]["sha"] for r in rows]
        verdict[role] = {
            "unique_sha": len(set(shas)),
            "cross_process_reproducible": len(set(shas)) == 1,
            "record_matches_file": all(r[role]["record_ok"] for r in rows),
            "secs": [r[role]["sec"] for r in rows],
            "f0s": [r[role]["f0"] for r in rows],
        }
    print("\n=== VERDICT ===")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    with open(os.path.join(HERE, "_voice_repro.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "verdict": verdict}, f,
                  ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
