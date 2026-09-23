# -*- coding: utf-8 -*-
"""句型语调通道的 A/B 判决实验（只读产品代码，只写 _smoke/ 下的产物）。

要回答两个问题：

  一、`instruct` 走 ICL 克隆这条路，到底进不进模型？
      ——「判决性实验」：同一句文本、同一音色档案、同参数，唯一变量是有无
      instruct。服务端的种子按 (文本, 音色指纹) 派生，两次请求拿到的是同一个
      种子；所以波形只要不同，差异就只能来自 instruct。相同 = 被静默丢弃。

  二、哪种措辞真能把疑问句的句尾抬起来？
      ——「主实验」：一组文本 × 若干条件，量句尾 F0 走向。

措辞从哪里来（官方依据，不自己编）：

  * `qwen.ai` 官方 VoiceDesign 样例（中文）：
      「模仿电视购物主持人,中年男性,声音洪亮有激情,语速极快,**音调夸张上扬**,
        用极具煽动性的语气来介绍产品…」
    说明「用中文自然语言描述音高走向」是官方自己用过的用法。
  * `qwen.ai` 官方英文对照样例：「…rapid-fire delivery with **exaggerated
    pitch rises**」。中英通吃。
  * `faster_qwen3_tts` 的 `generate_voice_clone` docstring 例：
      instruct="请用纯正广东话朗读"  —— 指令是「风格/方言」级，一句话即可。
  * `faster_qwen3_tts/model.py:510` 的官方告警：Base 模型的 instruct 在
      **x-vector-only** 克隆下不可靠，**用 ICL（xvec_only=False）才可靠**。
      本项目生产路径正是 ICL，合规。

句型号从哪里来：**句尾标点**。程序直接文本派生，不改 schema、不烧模型调用、
模型也无法改口供。标点全集见 `PUNCT_SENTENCE_TYPE`。
"""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "_smoke")
ROOT = os.path.dirname(HERE)
PROJ = os.path.join(ROOT, "projects", "20260915-103249")
SCRIPT = os.path.join(PROJ, "脚本", "1.json")
REF_WAV = os.path.join(PROJ, "音色", "A", "ref.wav")
REF_TXT = os.path.join(PROJ, "音色", "A", "ref.txt")
VOICE = "Vivian"
OUT = os.path.join(HERE, "_ab_out")
SERVICE = "http://127.0.0.1:9880/tts"

# 复用上一轮的量测函数，不另写一套（两套 F0 算法会让前后数据不可比）
_spec = importlib.util.spec_from_file_location(
    "_probe_intonation", os.path.join(HERE, "_probe_intonation.py"))
PI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PI)


# ---------------------------------------------------------------- 句型号全集

#: 句尾标点 → 句型号。**剥壳后再判**：闭引号、闭括号、书名号、右括号包在外面
#: 时，真正定调的是里面那个标点（「他问我：『走吗？』」的句型号是疑问，不是引号）。
PUNCT_SENTENCE_TYPE = {
    "？": "question",
    "?": "question",
    "？！": "question_exclaim",
    "！？": "question_exclaim",
    "?！": "question_exclaim",
    "！?": "question_exclaim",
    "！": "exclaim",
    "!": "exclaim",
    "……": "trailing",
    "…": "trailing",
    "——": "break",
    "—": "break",
    "－": "break",
    "。": "statement",
    ".": "statement",
    "，": "pause",
    ",": "pause",
    "、": "pause",
    "；": "pause",
    ";": "pause",
    "：": "pause",
    ":": "pause",
}

#: 外壳字符：这些只是包裹，不参与判定，剥掉后看内层。
SHELL = "」』】》〉）)]}”’\"'"

#: 兜底：没有可识别的句尾标点。
DEFAULT_TYPE = "statement"


def sentence_type(text: str) -> str:
    """从句尾标点派生句型号。纯文本操作，无模型依赖。

    先剥外壳（可反复嵌套），再看剩下部分的结尾是不是标点表里的键。长键优先
    （「？！」要压过「？」），不然「疑问+感叹」会被当成纯疑问。
    """
    s = str(text or "").rstrip()
    while s and s[-1] in SHELL:
        s = s[:-1].rstrip()
    if not s:
        return DEFAULT_TYPE
    for key in sorted(PUNCT_SENTENCE_TYPE, key=len, reverse=True):
        if s.endswith(key):
            return PUNCT_SENTENCE_TYPE[key]
    return DEFAULT_TYPE


# ------------------------------------------------------------------ 措辞候选

#: 句型号 → 候选措辞。每条都标了来源，方便回头审「这句凭什么这么写」。
#: 键名带 `_` 前缀的是对照条件（不该有效果，或就是现状）。
PHRASINGS = {
    # 「直陈」：把句型号用一句人话说明白（用户给定的表述）
    "q_direct": ("question", "这一句是疑问句，句尾语调上扬"),
    # 「语气+走向」：官方样例的路子（「用…的语气」「音调上扬」）
    "q_tone": ("question", "用疑问的语气说，句尾语调上扬"),
    # 「只给音高」：最短，只动一个维度，排除「语气」这个词的干扰
    "q_pitch": ("question", "句尾音调上扬"),
    # 「情绪+走向」：测试真情绪词与句型号叠加会不会互相抵消
    "q_emo": ("question", "用疑惑的语气说，句尾语调上扬"),
    # 「对比式」：显式排除陈述句，堵住「照参考音频的陈述先验念」这条路
    "q_contrast": ("question",
                   "这是疑问句不是陈述句，句尾语调要上扬，不要下沉"),
    # 「英文对照」：官方英文样例证明英文可用，验一下中文环境里哪种更灵
    "q_en": ("question",
             "This sentence is a question. Raise your pitch at the end."),
    # 「组合」：上面六条都只动一个维度，结果都只有半个半音。这一条把三个维度
    # 一次说完（整体抬高 + 句尾上扬 + 显式禁止下沉），看是不是「一次只说一件
    # 事」把它说弱了。
    "q_combo": ("question",
                "这一句是疑问句。整句音调抬高一些，句尾明显上扬，不要往下沉。"),
    # 「强度词」：官方 VoiceDesign 样例用的是「音调**夸张**上扬」——官方自己
    # 靠强化词拉效果。照抄这个路子。
    "q_exagg": ("question", "句尾音调夸张上扬，疑问语气强烈"),
    # 「语气+整体」：只强调整体音高这一个维度（现场实测疑问句整体音高比陈述句
    # 低 2 个半音，这是最大的一处反向差距）
    "q_high": ("question", "用疑问的语气说，整句音调抬高"),
    # 感叹句候选（现场零样本，先备着，验证时按需启用）
    "e_direct": ("exclaim", "这一句是感叹句，语气强烈，音调扬起"),
    "e_tone": ("exclaim", "用感叹的语气说，音调夸张上扬"),
    # 余韵候选（省略号）
    "t_len": ("trailing", "这一句句尾拖长放缓，语气渐弱，留有余韵"),
    # 陈述候选：陈述句要不要也发指令，是本次要顺带问的
    "s_flat": ("statement", "这一句是陈述句，句尾语调平稳，自然下沉"),
}


def _abs(p):
    return os.path.abspath(p)


def post_tts(text, instruct=None, emotion=None, degree=None, timeout=600,
             ref_wav=None, ref_text=None):
    """直接打 /tts。返回 (wav 字节, 耗时)。

    `ref_wav`/`ref_text` 不给就用现场那份陈述句参考音频（= 生产现状）。
    换参考音频是本次最根本的一根杠杆：ICL 把参考音频的**韵律**连同音色一起
    放进上下文，参考音频是陈述句，模型就先学会「照这个调念」；换掉它，等于
    换掉先验，而指令只是在这个先验上追加劝说。
    """
    rw = ref_wav or REF_WAV
    rt = ref_text
    if rt is None:
        rt = io.open(REF_TXT, encoding="utf-8").read().strip()
    body = {"text": text, "voice": VOICE, "speed": 1.0,
            "ref_audio": _abs(rw), "ref_text": rt}
    if emotion:
        body["emotion"] = emotion
    if degree:
        body["degree"] = degree
    if instruct:
        body["instruct"] = instruct
    req = urllib.request.Request(
        SERVICE, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("服务返回 %s：%s"
                           % (e.code, e.read().decode("utf-8", "replace")[:300]))
    if not raw:
        raise RuntimeError("服务返回空音频")
    return raw, time.time() - t0


def wait_service(limit=600):
    """等服务起床（--preload 要加载权重与预热）。"""
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:9880/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    return False


def measure(path):
    """量一句的句尾走向。复用上一轮的 intonation()。"""
    x, sr = PI.read_wav(path)
    return PI.intonation(x, sr)


def group_stats(rows):
    return PI.group_stats([r for r in rows if r])


# --------------------------------------------------------------- 判决性实验

def decisive(texts, outdir):
    """同文本、同音色、同种子，唯一变量是 instruct。比波形指纹。"""
    print("=" * 78)
    print("判决性实验：instruct 到底进不进模型")
    print("=" * 78)
    print("原理：服务端种子按 (文本, 音色指纹) 派生 → 两次请求同种子。"
          "波形不同 ⇒ 只能是 instruct 改的。")
    print()
    rows = []
    for i, text in enumerate(texts):
        a_raw, a_t = post_tts(text, emotion="追问", degree="none")
        b_raw, b_t = post_tts(text, instruct=PHRASINGS["q_direct"][1],
                              emotion="追问", degree="none")
        ha = hashlib.sha256(a_raw).hexdigest()[:16]
        hb = hashlib.sha256(b_raw).hexdigest()[:16]
        same = ha == hb
        naive = sum(1 for k in ("平静", "好奇", "疑惑", "恍然", "肯定", "感慨", "轻松")
                    if k in (PHRASINGS["q_direct"][1],))
        rows.append({"i": i, "text": text, "sha_a": ha, "sha_b": hb,
                     "same": same, "bytes_a": len(a_raw), "bytes_b": len(b_raw),
                     "sec_a": a_t, "sec_b": b_t})
        print("[%d] %s" % (i, text))
        print("    A 组（无指令，生产现状）  %s  %d 字节  %.1fs" % (ha, len(a_raw), a_t))
        print("    B 组（带句型指令）        %s  %d 字节  %.1fs" % (hb, len(b_raw), b_t))
        print("    → %s" % ("波形完全相同：instruct 被丢弃（死参数）"
                            if same else "波形不同：instruct 已进入生成过程"))
        print()
        os.makedirs(os.path.join(outdir, "decisive"), exist_ok=True)
        for cond, raw in (("A", a_raw), ("B", b_raw)):
            with open(os.path.join(outdir, "decisive", "%02d_%s.wav" % (i, cond)), "wb") as f:
                f.write(raw)
    n_same = sum(1 for r in rows if r["same"])
    verdict = "FAIL（instruct 是死参数，不必往下做）" if n_same == len(rows) else \
              ("PASS" if n_same == 0 else "部分生效，需再看")
    print("判决：%d/%d 条波形相同 → %s" % (n_same, len(rows), verdict))
    return {"rows": rows, "n_same": n_same, "verdict": verdict}


# ------------------------------------------------------------------ 主实验

def main_ab(texts, conds, outdir):
    """一组文本 × 若干条件，量句尾走向。"""
    print()
    print("=" * 78)
    print("主实验：多措辞对句尾走向的效果")
    print("=" * 78)
    conds = list(conds)
    print("条件数 %d × 文本数 %d = %d 次合成" % (len(conds), len(texts),
                                                len(conds) * len(texts)))
    print()
    detail = []
    for cname in conds:
        stype, wording = PHRASINGS.get(cname, (None, None))
        d = os.path.join(outdir, cname)
        os.makedirs(d, exist_ok=True)
        rows = []
        for i, text in enumerate(texts):
            try:
                if cname == "_baseline":
                    raw, _ = post_tts(text, emotion="追问", degree="none")
                else:
                    raw, _ = post_tts(text, instruct=wording,
                                      emotion="追问", degree="none")
            except Exception as e:  # noqa: BLE001
                print("    [%s][%d] 失败：%s" % (cname, i, str(e)[:120]))
                continue
            p = os.path.join(d, "%02d.wav" % i)
            with open(p, "wb") as f:
                f.write(raw)
            m = measure(p)
            if m:
                m.update({"cond": cname, "i": i, "text": text,
                          "sha": hashlib.sha256(raw).hexdigest()[:12]})
                rows.append(m)
        detail.extend(rows)
        s = group_stats(rows)
        print("  %-12s n=%2d  斜率中位 %+6.2f  落差中位 %+5.2f  上扬占比 %3.0f%%"
              % (cname, s.get("n", 0), s.get("slope_med", 0),
                 s.get("delta_med", 0), 100 * s.get("rise_ratio", 0)))

    base = group_stats([r for r in detail if r["cond"] == "_baseline"])
    print()
    print("相对基线的增量（基线 = 生产现状，无任何语气指令）：")
    print("  %-14s %-10s %-10s %-10s %s"
          % ("条件", "斜率Δ", "落差Δ", "上扬Δ", "判定"))
    verdicts = {}
    for cname in conds:
        if cname == "_baseline":
            continue
        s = group_stats([r for r in detail if r["cond"] == cname])
        if not s or not base:
            continue
        ds = s["slope_med"] - base["slope_med"]
        dd = s["delta_med"] - base["delta_med"]
        dr = s["rise_ratio"] - base["rise_ratio"]
        ok = (dr >= 0.30 and dd > 0.5)
        verdicts[cname] = {"slope_delta": ds, "delta_delta": dd,
                           "rise_delta": dr, "pass": bool(ok),
                           "rise_ratio": s["rise_ratio"]}
        print("  %-14s %+9.2f %+9.2f %+9.0f%%  %s"
              % (cname, ds, dd, 100 * dr, "PASS" if ok else "FAIL"))
    return {"baseline": base, "verdicts": verdicts, "detail": detail}


# ------------------------------------------------------- 换参考音频实验

def field_ref(i):
    """取现场第 i 句的音频与文本，当参考音频用。"""
    sc = json.load(io.open(SCRIPT, encoding="utf-8"))
    lines = sc if isinstance(sc, list) else (sc.get("lines") or [])
    item = lines[i]
    wav = os.path.join(PROJ, "过程", "1", "audio",
                       "%04d_%s.wav" % (i, item.get("speaker", "?")))
    return wav, (item.get("text") or "").rstrip()


#: 参考音频候选。`_statement` 就是生产现状那份；另两条是现场疑问句里句末走向
#: 最好的（索引取自 _smoke/_intonation.json 的实测排序）。
REFS = {
    "ref_statement": None,        # None = 用 REF_WAV（现状）
    "ref_q_flat": 70,             # 「…这合理吗？」句尾最接近持平（delta −0.33）
    "ref_q_rise": 150,            # 「…怎么计算？」句尾斜率最升（slope +5.59）
}


def ref_swap(texts, outdir):
    """同一批文本 × 不同参考音频 × (无指令 / 组合指令)。"""
    print()
    print("=" * 78)
    print("换参考音频实验：换先验 vs 加指令")
    print("=" * 78)
    print("原理：ICL 把参考音频的韵律放进上下文。参考音频是陈述句，模型就先学")
    print("      「照这个调念」——指令只是在先验之上追加劝说。")
    print()
    for name, idx in REFS.items():
        if idx is None:
            print("  %-14s = 生产现状（陈述句 ref）" % name)
        else:
            w, t = field_ref(idx)
            print("  %-14s = 现场第 %d 句  %s" % (name, idx, t[:32]))
    print()

    detail, summary = [], {}
    for name, idx in REFS.items():
        rw, rt = (None, None) if idx is None else field_ref(idx)
        for cond, wording in (("none", None), ("instruct", PHRASINGS["q_combo"][1])):
            key = "%s__%s" % (name, cond)
            d = os.path.join(outdir, "refswap", key)
            os.makedirs(d, exist_ok=True)
            rows = []
            for i, text in enumerate(texts):
                try:
                    raw, _ = post_tts(text, instruct=wording,
                                      emotion="追问", degree="none",
                                      ref_wav=rw, ref_text=rt)
                except Exception as e:  # noqa: BLE001
                    print("    [%s][%d] 失败：%s" % (key, i, str(e)[:100]))
                    continue
                p = os.path.join(d, "%02d.wav" % i)
                with open(p, "wb") as f:
                    f.write(raw)
                m = measure(p)
                if m:
                    m.update({"key": key, "ref": name, "cond": cond, "i": i})
                    rows.append(m)
            detail.extend(rows)
            s = group_stats(rows)
            summary[key] = s
            print("  %-26s n=%2d  斜率中位 %+6.2f  落差中位 %+5.2f  上扬占比 %3.0f%%"
                  % (key, s.get("n", 0), s.get("slope_med", 0),
                     s.get("delta_med", 0), 100 * s.get("rise_ratio", 0)))
    return {"summary": summary, "detail": detail}


def pick_texts(n):
    sc = json.load(io.open(SCRIPT, encoding="utf-8"))
    lines = sc if isinstance(sc, list) else (sc.get("lines") or [])
    out = []
    for l in lines:
        t = (l.get("text") or "").rstrip()
        if t.endswith("？") and 12 <= len(t) <= 34:
            out.append(t)
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    stage = sys.argv[1] if len(sys.argv) > 1 else "decisive"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    conds = sys.argv[3].split(",") if len(sys.argv) > 3 else [
        "_baseline", "q_direct", "q_tone", "q_pitch", "q_contrast"]

    print("等服务起来…")
    if not wait_service():
        print("服务没起来，看 tts_service/_ab_serve.log")
        raise SystemExit(1)
    print("服务在线")
    print()

    texts = pick_texts(max(n, 8))
    result = {}
    if stage in ("decisive", "all"):
        result["decisive"] = decisive(texts[:n], OUT)
    if stage in ("main", "all"):
        result["main"] = main_ab(texts[:n], conds, OUT)
    if stage in ("ref", "all"):
        result["refswap"] = ref_swap(texts[:n], OUT)

    meta = {"stage": stage, "conds": conds, "n": n,
            "ref_wav": REF_WAV, "voice": VOICE,
            "phrasings": PHRASINGS}
    with io.open(os.path.join(HERE, "_intonation_ab.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "result": result}, f, ensure_ascii=False, indent=1)
    print()
    print("结果写入 _smoke/_intonation_ab.json")
