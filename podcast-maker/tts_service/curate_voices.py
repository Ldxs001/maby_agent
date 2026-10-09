# -*- coding: utf-8 -*-
"""人耳挑选结果落库：official_voices（车间素材池）→ podcast_maker/resources/voices。

为什么要有这一步
----------------
official_voices 是**车间**：9 预设 × A/B 双案 × 各 3 take 的原始产出，含落选
样本，不随仓推送。人耳挑完之后，被选中的 take 才是**声库**——落在
podcast_maker/resources/voices/（与 bgm 同级），随仓走、永不丢；认领（adopt_official）
的源就是这里。车间与声库分离：重录只动车间，声库里的已定稿嗓子不受影响。

每条声库音色 = 一个目录（目录名即音色名）：
  ref.wav        逐字节复制自车间 take（响度已在入库时归一）
  ref.txt        这条 take 自己念的那份文案（schema 2 逐 take 存 text）
  profile.json   kind="voice_library" + text_role（A/B 案）+ 全套溯源
                 （builtin_voice / official_take / seed / sha16 / F0 / 语速）

认领把关在 adopt_official：A 角只认 text_role=A 的音色、B 角只认 B——
ref.txt 与音频逐字对应是 ICL 的命门，落库时就按案分立，认领时按案放行。

用法
----
  python curate_voices.py            # 按 PICKS 表落库（已存在的目录跳过）
  python curate_voices.py --force    # 覆盖重写
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time

import make_voice as mv

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY_DIR = os.path.join(os.path.dirname(HERE), "podcast_maker",
                           "resources", "voices")

# 人耳挑选结论（2026-10-08 试听页反馈）。键 = (预设名, take 号)，值 = (家族名, 风味名)。
# 目录名 = 家族-风味，全局唯一、不重样；家族名给人认，风味名给这条 take 认。
PICKS = {
    # 万恶之源男（Ryan，男，F0 103~168，全场最慢 3.71~4.80 字/s）
    ("Ryan", 1):     ("万恶之源男", "老磨"),
    ("Ryan", 2):     ("万恶之源男", "晨钟"),
    ("Ryan", 3):     ("万恶之源男", "低语"),
    ("Ryan", 4):     ("万恶之源男", "暗河"),
    ("Ryan", 5):     ("万恶之源男", "深巷"),
    ("Ryan", 6):     ("万恶之源男", "暮鼓"),
    # 万恶之源女（Ono_Anna，女，F0 231~289，3.90~5.09 字/s）
    ("Ono_Anna", 1): ("万恶之源女", "萤火"),
    ("Ono_Anna", 2): ("万恶之源女", "晚风"),
    ("Ono_Anna", 3): ("万恶之源女", "初雪"),
    ("Ono_Anna", 4): ("万恶之源女", "檐雨"),
    ("Ono_Anna", 5): ("万恶之源女", "星野"),
    ("Ono_Anna", 6): ("万恶之源女", "清晓"),
    # 老傅（Uncle_Fu，男中音，F0 181~250，4.65~5.79 字/s）
    ("Uncle_Fu", 1): ("老傅", "说书"),
    ("Uncle_Fu", 2): ("老傅", "敲桌"),
    ("Uncle_Fu", 3): ("老傅", "拍案"),
    ("Uncle_Fu", 4): ("老傅", "压嗓"),
    ("Uncle_Fu", 6): ("老傅", "收束"),
    # 薇薇（Vivian，女，F0 209~293，5.19~5.94 字/s）
    ("Vivian", 1):   ("薇薇", "清亮"),
    ("Vivian", 2):   ("薇薇", "明快"),
    ("Vivian", 4):   ("薇薇", "温言"),
    ("Vivian", 5):   ("薇薇", "轻喃"),
    # 瑟琳（Serena，女，F0 205~258，5.50~6.62 字/s）
    ("Serena", 1):   ("瑟琳", "利落"),
    ("Serena", 2):   ("瑟琳", "爽朗"),
    ("Serena", 3):   ("瑟琳", "快语"),
    ("Serena", 4):   ("瑟琳", "沉静"),
    ("Serena", 6):   ("瑟琳", "舒展"),
    # 迪伦（Dylan，男，F0 103~182，6.11~6.81 字/s 全场最快一档）
    ("Dylan", 1):    ("迪伦", "疾走"),
    ("Dylan", 3):    ("迪伦", "冲线"),
    ("Dylan", 4):    ("迪伦", "深流"),
    ("Dylan", 6):    ("迪伦", "跳脱"),
    # 埃里克（Eric，男，F0 149~175，6.27~6.33 字/s）
    ("Eric", 1):     ("埃里克", "直球"),
    ("Eric", 5):     ("埃里克", "稳拍"),
    # 艾登（Aiden，男，F0 134~146，5.07~6.10 字/s）
    ("Aiden", 3):    ("艾登", "急板"),
    ("Aiden", 4):    ("艾登", "从容"),
    # 秀熙（Sohee，女，F0 200~231，4.87~6.40 字/s）
    ("Sohee", 1):    ("秀熙", "轻快"),
    ("Sohee", 2):    ("秀熙", "雀跃"),
    ("Sohee", 5):    ("秀熙", "温声"),
}


def curate(picks=PICKS, library_dir: str = LIBRARY_DIR, force: bool = False,
           log=print) -> dict:
    """按挑选表把车间 take 逐字节落库。缺 take / 缺元数据当场报错，不猜。"""
    presets = {p["name"]: p for p in mv.official_presets()}
    done, skipped = [], []
    for (voice, take_no), (family, flavor) in sorted(picks.items()):
        name = "%s-%s" % (family, flavor)
        prof = presets.get(voice)
        if not prof:
            raise RuntimeError("车间素材池里没有预设「%s」。" % voice)
        meta = next((t for t in prof["takes"] if t.get("take") == take_no), None)
        if not meta:
            raise RuntimeError("「%s」没有 take%d 的元数据。" % (voice, take_no))
        role = meta.get("text_role")
        if role not in ("A", "B"):
            raise RuntimeError("「%s」take%d 没有 text_role，落库无从分案。"
                               % (voice, take_no))
        text = meta.get("text")
        if not text:
            raise RuntimeError("「%s」take%d 缺逐 take 文案。" % (voice, take_no))
        wav_src = os.path.join(mv.OFFICIAL_DIR, voice, "take%d.wav" % take_no)
        if not os.path.isfile(wav_src):
            raise RuntimeError("「%s」take%d 音频缺失，重跑 record_official_presets.py。"
                               % (voice, take_no))
        vdir = os.path.join(library_dir, name)
        if os.path.isdir(vdir) and not force:
            skipped.append(name)
            log("  %-14s 已有，跳过（--force 覆盖）" % name)
            continue
        os.makedirs(vdir, exist_ok=True)
        with open(wav_src, "rb") as fh:
            wav_bytes = fh.read()
        # 波形逐字节复制；sha16 与车间账本对账 —— 对不上说明车间在落库前被动过。
        if meta.get("sha16"):
            actual = hashlib.sha256(wav_bytes).hexdigest()[:16]
            if actual != meta["sha16"]:
                raise RuntimeError("「%s」take%d 字节指纹对不上（账本 %s / 实际 %s）。"
                                   % (voice, take_no, meta["sha16"], actual))
        rec = {
            "kind": "voice_library",
            "name": name,
            "family": family,
            "flavor": flavor,
            "text_role": role,
            "text": text,
            "builtin_voice": voice,
            "official_take": take_no,
            "seed": meta.get("seed"),
            "sha16": meta.get("sha16"),
            "f0_med": meta.get("f0_med"),
            "seconds": meta.get("seconds"),
            "voiced_seconds": meta.get("voiced_seconds"),
            "speech_rate": meta.get("speech_rate"),
            "rate_out_of_window": bool(meta.get("rate_out_of_window")),
            "loudness_gain_db": meta.get("loudness_gain_db"),
            "loudness_rms_dbfs": meta.get("loudness_rms_dbfs"),
            "language_tag": prof.get("language_tag", ""),
            "desc": prof.get("desc", ""),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ref_md5": hashlib.sha256(wav_bytes).hexdigest()[:16],
        }
        with open(os.path.join(vdir, "ref.wav"), "wb") as fh:
            fh.write(wav_bytes)
        with io.open(os.path.join(vdir, "ref.txt"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        with io.open(os.path.join(vdir, "profile.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        done.append(name)
        log("  %-14s ← %s take%d（%s案，md5 %s）"
            % (name, voice, take_no, role, rec["ref_md5"]))
    return {"done": done, "skipped": skipped,
            "total": len(os.listdir(library_dir)) if os.path.isdir(library_dir) else 0}


def main() -> int:
    ap = argparse.ArgumentParser(description="人耳挑选结果落库为随仓声库")
    ap.add_argument("--force", action="store_true", help="覆盖已有条目重写")
    args = ap.parse_args()
    if not os.path.isdir(mv.OFFICIAL_DIR):
        print("车间素材池不存在：%s" % mv.OFFICIAL_DIR, file=sys.stderr)
        return 1
    res = curate(force=args.force)
    print("落库 %d 条，跳过 %d 条，声库共 %d 条 → %s"
          % (len(res["done"]), len(res["skipped"]), res["total"], LIBRARY_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
