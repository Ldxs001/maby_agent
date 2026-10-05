# -*- coding: utf-8 -*-
"""AIGC 标识落地可行性实测。

三件事：
  1) 隐式标识：往 mp3 / wav / mp4 三种容器写 GB 45438-2025 附录 E 规定的
     AIGC 隐式标识字段，再用 ffprobe 读回，验证「写得进、读得出、字段不丢」。
  2) 显式标识·节奏：合成「短长短短」（AI 的摩斯码）节奏音并实测节奏间隔。
  3) 查现有产物的元数据现状（改前基线）。

产物全部落在 _smoke/_aigc_probe/，不改动任何产品产物。
"""
import json
import math
import os
import shutil
import subprocess
import sys
import wave

sys.path.insert(0, os.path.abspath("."))
import numpy as np  # noqa: E402

OUT = os.path.join("_smoke", "_aigc_probe")
SRC_MP3 = "projects/20260911-113022/podcast.mp3"
SRC_MP4 = "projects/20260911-113022/final.mp4"

# GB 45438-2025 附录 E 规定的隐式标识值
PRODUCER = "wUwproject"
AIGC_VALUE = json.dumps({
    "AIGC": {
        "Label": "1",                 # 1=属于 AI 生成合成
        "ContentProducer": PRODUCER,   # 生成合成服务提供者名称或编码
        "ProduceID": "ep-20260911-113022",  # 内容唯一编号
        "ReservedCode1": "",           # 预留（安全防护）
        "ContentPropagator": PRODUCER,  # 首次写入：与 ContentProducer 一致
        "PropagateID": "ep-20260911-113022",  # 首次写入：与 ProduceID 一致
        "ReservedCode2": "",
    }
}, ensure_ascii=False, separators=(",", ":"))


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def probe(path):
    rc, out = run(["ffprobe", "-v", "quiet", "-show_format", "-of", "json", path])
    if rc != 0:
        return {}
    try:
        return json.loads(out).get("format", {}).get("tags", {}) or {}
    except Exception:
        return {}


def banner(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def main():
    os.makedirs(OUT, exist_ok=True)
    banner("AIGC 标识落地可行性实测")

    print("\n待写入的隐式标识值（GB 45438-2025 附录 E 格式）：")
    print("  %s" % AIGC_VALUE)

    # ---------------- ① 隐式标识：三种容器 ----------------
    banner("① 隐式标识写入 → 读回验证")

    cases = []
    if os.path.exists(SRC_MP3):
        dst = os.path.join(OUT, "podcast_tagged.mp3")
        shutil.copy2(SRC_MP3, dst)
        cases.append(("mp3 (ID3v2 TXXX)", ["-c:a", "copy"], dst))
    if os.path.exists(SRC_MP4):
        dst = os.path.join(OUT, "final_tagged.mp4")
        shutil.copy2(SRC_MP4, dst)
        cases.append(("mp4 (udta/ilst)", ["-c", "copy"], dst))
    # wav：从现有 mp3 转一份，再打标
    wav = os.path.join(OUT, "podcast.wav")
    run(["ffmpeg", "-y", "-i", SRC_MP3, "-c:a", "pcm_s16le", wav])
    if os.path.exists(wav):
        cases.append(("wav (LIST INFO)", ["-c:a", "copy"], wav))

    for label, cflag, path in cases:
        before = probe(path)
        tmp = path + ".tmp" + os.path.splitext(path)[1]
        rc, log = run(["ffmpeg", "-y", "-i", path] + cflag +
                      ["-metadata", "AIGC=" + AIGC_VALUE,
                       "-metadata", "comment=" + AIGC_VALUE,
                       tmp])
        if rc == 0:
            shutil.move(tmp, path)
        after = probe(path)
        print("\n  【%s】" % label)
        print("    写入前自定义标签: %s" % (sorted(before.keys()) or "无"))
        print("    ffmpeg 返回码    : %d" % rc)
        if rc != 0:
            tail = [ln for ln in (log or "").splitlines() if ln.strip()][-6:]
            print("    失败原因(末6行)  :")
            for ln in tail:
                print("      | %s" % ln[:150])
        print("    写入后全部标签   :")
        for k, v in sorted(after.items()):
            shown = v if len(str(v)) <= 100 else str(v)[:97] + "..."
            print("      %-22s = %s" % (k, shown))
        hit = [k for k, v in after.items() if "AIGC" in str(v)]
        print("    含 AIGC 字样的标签键: %s" % (hit or "【无】"))
        if hit:
            try:
                parsed = json.loads(after[hit[0]])
                keys = sorted(parsed.get("AIGC", {}).keys())
                print("    JSON 可解析       : 是，AIGC 下 %d 个字段 %s"
                      % (len(keys), keys))
            except Exception as e:
                print("    JSON 可解析       : 否 (%s)" % e)
        else:
            print("    JSON 可解析       : 不适用（没写进去）")

    # ---------------- ② 显式标识·节奏音 ----------------
    banner("② 显式标识·音频节奏标识「短长短短」（AI 的摩斯码）")

    unit = 0.12  # 单位时长（秒）；短=1单位，长=3单位，间隔=1单位
    pattern = [("短", 1), ("隔", 1), ("长", 3), ("隔", 1),
               ("短", 1), ("隔", 1), ("短", 1)]
    total = sum(u for _, u in pattern) * unit
    n = int(22050 * (total + 0.3))
    buf = np.zeros(n, dtype=np.float64)
    pos = 0
    marks = []
    for kind, units in pattern:
        dur = units * unit
        seg = int(22050 * dur)
        if kind != "隔":
            t = np.arange(seg) / 22050.0
            tone = 0.32 * np.sin(2 * np.pi * 1046.5 * t)  # C6，清晰可辨
            fade = max(1, int(0.008 * 22050))
            env = np.ones(seg)
            env[:fade] = np.linspace(0, 1, fade)
            env[-fade:] = np.linspace(1, 0, fade)
            buf[pos:pos + seg] += tone * env
            marks.append((kind, pos / 22050.0, dur))
        pos += seg
    pcm = (np.clip(buf, -1, 1) * 32767).astype("<i2")
    tone_path = os.path.join(OUT, "ai_rhythm_short_long_short_short.wav")
    with wave.open(tone_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(pcm.tobytes())

    print("\n  单位时长 %.0f ms，总时长 %.2f s" % (unit * 1000, total))
    print("  实际发声段：")
    for kind, start, dur in marks:
        print("    %s  起始 %5.2fs  时长 %5.2fs（%d 单位）"
              % (kind, start, dur, round(dur / unit)))
    # 读回验证
    with wave.open(tone_path, "rb") as wf:
        a = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(float)
    rms = np.sqrt(np.mean(a ** 2))
    print("  生成文件：%s" % tone_path)
    print("  RMS %.4f（满量程 32767）" % rms)

    # ---------------- ③ 现有产物基线 ----------------
    banner("③ 现有产物元数据基线（改前）")
    for f in (SRC_MP3, SRC_MP4):
        if os.path.exists(f):
            t = probe(f)
            print("  %-46s → %s" % (f, t or "无标签"))
    print("\n  （对照上面 ① 写入后的结果，即可看出差多少字段）")

    banner("产物目录")
    for root, _dirs, files in os.walk(OUT):
        for f in sorted(files):
            p = os.path.join(root, f)
            print("  %-58s %8.1f KB" % (p, os.path.getsize(p) / 1024.0))


if __name__ == "__main__":
    main()
