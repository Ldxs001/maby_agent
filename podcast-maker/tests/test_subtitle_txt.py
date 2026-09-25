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

"""出片字幕 —— 四份同源（SRT / LRC / 整秒 TXT / 洁版 TXT）。

一期出四份字幕：SRT（起止齐全）、LRC（音频平台歌词位，百分秒）、TXT（整秒
`[mm:ss]`）、洁版 TXT（整秒摘掉方括号 `mm:ss`）。四者**同一份 script、同一份
timings、同一次生成**，这份测试钉住它：

  1. SRT 与 LRC 的输出**逐字节不变**（改动只在增料，没碰老两份）——由
     `_smoke/subtitle_txt_baseline.py` 的基线快照对账，这里补结构断言。
  2. TXT 与 LRC **逐字同源**，只差时间戳精度：去掉方括号里的时间戳之后两份
     文本必须逐字符相同。
  3. **同源律**：把 `build_lrc` 的输出交给离线取整工具
     （`tools/lrc_round_seconds.py`，独立实现）转换，结果必须与 `build_txt`
     的输出**逐字节相同**。这一条把「运行时算的整秒」和「事后拿 LRC 取整」
     锁成同一个答案，两边谁先漂都会响。
  4. 取整是**严格四舍五入**（`.50` 进），不是 Python `round` 的银行家舍入。
  5. 洁版 TXT 与整秒 TXT **逐行同源**：只差时间戳的方括号，摘掉之后逐字符
     相同。洁版**不是**从磁盘上那份 `.txt` 改出来的——四份都从同一份源现生成。
"""

import importlib.util
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys                                                       # noqa: E402
sys.path.insert(0, ROOT)

from podcast_maker import layout, subtitle_engine as S           # noqa: E402


def _load_tools_module():
    """按文件路径加载离线取整工具——**不复用产品实现**，否则交叉验证自证。"""
    path = os.path.join(ROOT, "tools", "lrc_round_seconds.py")
    spec = importlib.util.spec_from_file_location("_lrc_round_seconds", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LRC_ROUND = _load_tools_module()

TS_LRC = re.compile(r"\[\d+:\d{2}\.\d+\]")
TS_TXT = re.compile(r"\[\d+:\d{2}\]")

# 洁版规则的正则：**逐字照抄**主人那把离线脚本（`convert_to_clean.py`）里的
# PATTERN。它是一份独立于产品实现的写法，拿它交叉验证 `build_clean_txt`——
# 与 `LRC_ROUND` 同一个路数：产品改了口径，这条就响。
CLEAN_PATTERN = re.compile(r"^\[(\d{1,2}:\d{2})\](.*)$")


def strip_bracket(text):
    """整秒 TXT 逐行摘掉时间戳方括号（主人脚本的写法，独立实现）。"""
    out = []
    for ln in text.split("\n"):
        m = CLEAN_PATTERN.match(ln)
        out.append(m.group(1) + m.group(2) if m else ln)
    return "\n".join(out)

SCRIPT = [
    {"speaker": "A", "emotion": "追问", "text": "含碳 1.2% 且含铬 10.5% 的铁属于 stainless steel，为什么这么说呢？"},
    {"speaker": "B", "emotion": "承接", "text": "RAG Assistant 的分解准确率超过九成证明能力达标。"},
    {"speaker": "A", "emotion": "承接", "text": "上行\n下行——句内换行要变成空格。"},
    {"speaker": "B", "emotion": "承接", "text": ""},
]

# 覆盖 .49 / .50 / 秒进位 / 60 秒进位 / 超一小时 / 三位以上小数
SECONDS = [0.0, 0.49, 0.5, 0.4999, 0.5001, 2.005, 59.49, 59.5, 59.99,
           119.999, 3599.5, 3599.99, 3600.0, 7200.5]
TIMINGS = [{"start": s, "end": s + 0.8} for s in SECONDS]

CFGS = [
    {},                                                        # 名字关（默认）
    {"speaker_indicator.name_shown": True,                     # 名字开
     "tts.name_a": "小美", "tts.name_b": "大美"},
]


class TestFmtSecondTime(unittest.TestCase):
    """取整本体：严格四舍五入，进制与 LRC 同轴。"""

    def test_half_up_not_bankers(self):
        """`.50` 必须进位——这是全篇的立身之本。

        `round(2.5)` 在 Python 里给 2（银行家舍入到偶数）。整秒戳若走 `round`，
        一半的 `.50` 会原地不动，与「四舍五入」这个说法不符。
        """
        self.assertEqual(S.fmt_second_time(2.5), "00:03")   # round 会给 2 ← 反例
        self.assertEqual(S.fmt_second_time(3.5), "00:04")
        self.assertEqual(S.fmt_second_time(0.5), "00:01")   # round 会给 0 ← 反例
        self.assertEqual(S.fmt_second_time(4.5), "00:05")   # round 会给 4 ← 反例
        self.assertEqual(S.fmt_second_time(0.49), "00:00")
        self.assertEqual(S.fmt_second_time(2.005), "00:02")

    def test_quantum_is_the_percent_second(self):
        """取整发生在**百分秒刻度**上，不是原始浮点秒上。

        `0.4999` 秒落进 `00:01`——看着反直觉，但它是对的：字幕时间轴的最小刻度
        就是百分秒（LRC 那份把它写成 `[00:00.50]`），整秒版是对**这条时间轴**取整，
        不是对上游那个多带两位的浮点取整。要两处同轴，否则「LRC 事后取整」与
        「出片直接生成」会给出两个答案。
        """
        self.assertEqual(S.fmt_lrc_time(0.4999), "00:00.50")
        self.assertEqual(S.fmt_second_time(0.4999), "00:01")
        self.assertEqual(S.fmt_lrc_time(0.5001), "00:00.50")
        self.assertEqual(S.fmt_second_time(0.5001), "00:01")

    def test_second_carries_into_minute(self):
        """按总秒进位，秒溢出正常带动分钟。"""
        self.assertEqual(S.fmt_second_time(59.49), "00:59")
        self.assertEqual(S.fmt_second_time(59.5), "01:00")
        self.assertEqual(S.fmt_second_time(59.99), "01:00")
        self.assertEqual(S.fmt_second_time(119.999), "02:00")
        self.assertEqual(S.fmt_second_time(3599.5), "60:00")
        self.assertEqual(S.fmt_second_time(3600.0), "60:00")
        self.assertEqual(S.fmt_second_time(7200.5), "120:01")

    def test_format_shape(self):
        """`mm:ss`：分钟补零两位、允许超过 59；不接受负数。"""
        self.assertEqual(S.fmt_second_time(0), "00:00")
        self.assertEqual(S.fmt_second_time(5), "00:05")
        self.assertEqual(S.fmt_second_time(65), "01:05")
        self.assertEqual(S.fmt_second_time(-3), "00:00")
        for s in (0.0, 1.0, 59.9, 3600.0, 100000.0):
            with self.subTest(s=s):
                self.assertRegex(S.fmt_second_time(s), r"^\d{2,}:\d{2}$")

    def test_same_axis_as_lrc(self):
        """与 `fmt_lrc_time` 同一根数轴：LRC 那一份取整 == 整秒戳。

        逐值手算，不调产品函数自证：把 LRC 的 `mm:ss.ff` 拆开、按「百分秒总数
        +50 再 //100」算，必须等于 `fmt_second_time` 的答案。
        """
        for s in [i / 100.0 for i in range(0, 600, 1)] + list(SECONDS):
            with self.subTest(s=s):
                mm, ss, ff = re.match(r"(\d+):(\d{2})\.(\d{2})",
                                      S.fmt_lrc_time(s)).groups()
                total = (int(mm) * 60 + int(ss)) * 100 + int(ff)
                want = "%02d:%02d" % ((total + 50) // 100 // 60,
                                      (total + 50) // 100 % 60)
                self.assertEqual(S.fmt_second_time(s), want)


class TestBuildTxt(unittest.TestCase):
    """产物本体：一条一行、与 LRC 逐字同源。"""

    def test_one_line_per_sentence_with_second_stamp(self):
        out = S.build_txt(SCRIPT, {}, TIMINGS)
        lines = out.split("\n")
        self.assertEqual(len(lines), len(SCRIPT))
        for ln in lines:
            self.assertRegex(ln, r"^\[\d{2,}:\d{2}\]")

    def test_carries_start_time_only(self):
        """取的是**起始**时间，不是结束时间；取整按百分秒刻度。"""
        lines = S.build_txt(SCRIPT, {}, TIMINGS).split("\n")
        self.assertEqual(len(lines), len(SCRIPT))
        self.assertTrue(lines[0].startswith("[00:00]"))     # 0.00
        self.assertTrue(lines[1].startswith("[00:00]"))     # 0.49 舍
        self.assertTrue(lines[2].startswith("[00:01]"))     # 0.50 进
        self.assertTrue(lines[3].startswith("[00:01]"))     # 0.4999 → 00:00.50 → 进
        # 结束时间（start + 0.8）没有被采用：否则这几条会整体差一秒
        self.assertEqual(S.build_txt(SCRIPT, {}, TIMINGS),
                         S.build_txt(SCRIPT, {},
                                     [{"start": t["start"], "end": 999.0}
                                      for t in TIMINGS]))

    def test_missing_timings_fall_back_to_zero(self):
        out = S.build_txt([{"speaker": "A", "text": "只有一句"}], {}, [])
        self.assertEqual(out, "[00:00]只有一句")

    def test_empty_script(self):
        self.assertEqual(S.build_txt([], {}, []), "")

    def test_same_text_as_lrc_only_stamp_differs(self):
        """核心同源律（结构面）：去掉时间戳后两份文本逐字符相同。

        含句内换行的那一句是重点——LRC 与 TXT 都必须把它压成空格、留一条，
        否则一条歌词被劈成两条、只剩第一条带时间戳。
        """
        for cfg in CFGS:
            with self.subTest(cfg=cfg):
                lrc = S.build_lrc(SCRIPT, cfg, TIMINGS)
                txt = S.build_txt(SCRIPT, cfg, TIMINGS)
                self.assertEqual(TS_LRC.sub("", lrc), TS_TXT.sub("", txt))
                self.assertEqual(lrc.count("\n"), txt.count("\n"))

    def test_inner_newline_is_flattened(self):
        """句内换行压成空格、只留一条——两份歌词共用这一条清理。"""
        one = [SCRIPT[2]]
        stamp = [{"start": 1.0, "end": 2.0}]
        for build in (S.build_lrc, S.build_txt):
            with self.subTest(build=build.__name__):
                out = build(one, {}, stamp)
                self.assertNotIn("\n", out)
                self.assertIn("上行 下行", out)

    def test_name_switch_moves_both_files_together(self):
        """说话人名开关同时作用两份——名字只在加料层，不在时间戳层。"""
        named = {"speaker_indicator.name_shown": True,
                 "tts.name_a": "小美", "tts.name_b": "大美"}
        on = S.build_txt(SCRIPT, named, TIMINGS)
        off = S.build_txt(SCRIPT, {}, TIMINGS)
        self.assertNotEqual(on, off)
        self.assertIn("小美：", on)
        self.assertEqual(TS_TXT.sub("", on),
                         TS_LRC.sub("", S.build_lrc(SCRIPT, named, TIMINGS)))


class TestBuildCleanTxt(unittest.TestCase):
    """洁版产物：整秒 TXT 摘掉方括号，其余逐字相同。"""

    def test_one_line_per_sentence_with_stamp_and_no_brackets(self):
        out = S.build_clean_txt(SCRIPT, {}, TIMINGS)
        lines = out.split("\n")
        self.assertEqual(len(lines), len(SCRIPT))
        for ln in lines:
            self.assertNotIn("[", ln)
            self.assertNotIn("]", ln)
            self.assertRegex(ln, r"^\d{2,}:\d{2}")

    def test_is_exactly_txt_without_brackets(self):
        """核心同源律：拿主人那把离线脚本的正则去改整秒 TXT，结果逐字节相同。"""
        for cfg in CFGS:
            with self.subTest(cfg=cfg):
                txt = S.build_txt(SCRIPT, cfg, TIMINGS)
                self.assertEqual(strip_bracket(txt),
                                 S.build_clean_txt(SCRIPT, cfg, TIMINGS))

    def test_stamp_is_the_second_axis_not_the_lrc_axis(self):
        """时间戳走整秒那根数轴（与 TXT 同轴），不是 LRC 的百分秒。"""
        lines = S.build_clean_txt(SCRIPT, {}, TIMINGS).split("\n")
        self.assertTrue(lines[0].startswith("00:00"))     # 0.00
        self.assertTrue(lines[1].startswith("00:00"))     # 0.49 舍
        self.assertTrue(lines[2].startswith("00:01"))     # 0.50 进
        self.assertNotIn(".", lines[0][:5], "时间戳里不该有小数点")

    def test_carries_start_time_only(self):
        self.assertEqual(
            S.build_clean_txt(SCRIPT, {}, TIMINGS),
            S.build_clean_txt(SCRIPT, {},
                              [{"start": t["start"], "end": 999.0}
                               for t in TIMINGS]))

    def test_inner_newline_is_flattened(self):
        """句内换行压成空格、只留一条——四份共用这一条清理。"""
        out = S.build_clean_txt([SCRIPT[2]], {}, [{"start": 1.0, "end": 2.0}])
        self.assertNotIn("\n", out)
        self.assertEqual(out, "00:01上行 下行——句内换行要变成空格。")

    def test_name_switch_moves_it_with_the_other_three(self):
        named = {"speaker_indicator.name_shown": True,
                 "tts.name_a": "小美", "tts.name_b": "大美"}
        on = S.build_clean_txt(SCRIPT, named, TIMINGS)
        self.assertIn("小美：", on)
        self.assertEqual(strip_bracket(S.build_txt(SCRIPT, named, TIMINGS)), on)

    def test_missing_timings_fall_back_to_zero(self):
        self.assertEqual(
            S.build_clean_txt([{"speaker": "A", "text": "只有一句"}], {}, []),
            "00:00只有一句")

    def test_empty_script(self):
        self.assertEqual(S.build_clean_txt([], {}, []), "")


class TestSameSourceAsLrc(unittest.TestCase):
    """**同源律**：整秒戳 == 把 LRC 那份交给离线工具取整。

    一边是运行时算的（`fmt_second_time`），一边是事后拿文本转的
    （`tools/lrc_round_seconds.py`，独立实现）。两者对同一批时间戳必须给出
    逐字节相同的产物，否则「由同一份源出三种类型」这句话就不成立。
    """

    def test_converted_lrc_equals_txt(self):
        for cfg in CFGS:
            with self.subTest(cfg=cfg):
                lrc = S.build_lrc(SCRIPT, cfg, TIMINGS)
                txt = S.build_txt(SCRIPT, cfg, TIMINGS)
                converted, rep = LRC_ROUND.convert_text(lrc)
                self.assertEqual(converted, txt,
                                 "把 LRC 取整得到的文本应与整秒 TXT 逐字节相同")
                self.assertEqual(rep["total"], len(SCRIPT))
                # 这批时间轴是**故意挤在 0–0.6 秒**的边界样本，取整后必然撞车；
                # 撞车不是这份的判据（正常语速那条单独测），这里只钉同源律。

    def test_converted_lrc_equals_txt_on_dense_axis(self):
        """密集时间轴（每 0.01 秒一句，跨 .49/.50 两侧共 600 条）再压一遍。"""
        script = [{"speaker": "A", "emotion": "承接", "text": "第 %d 句。" % i}
                  for i in range(600)]
        timings = [{"start": i / 100.0, "end": i / 100.0 + 0.05}
                   for i in range(600)]
        lrc = S.build_lrc(script, {}, timings)
        txt = S.build_txt(script, {}, timings)
        self.assertEqual(LRC_ROUND.convert_text(lrc)[0], txt)
        self.assertEqual(len(txt.split("\n")), 600)

    def test_round_trip_is_idempotent(self):
        """整秒戳再过一次取整工具，纹丝不动。"""
        txt = S.build_txt(SCRIPT, {}, TIMINGS)
        # 工具只认带小数的戳；整秒戳应原样保留（不改、也不认成时间戳）
        again, rep = LRC_ROUND.convert_text(txt)
        self.assertEqual(again, txt)
        self.assertEqual(rep["total"], 0)

    def test_no_collision_on_realistic_timeline(self):
        """正常语速（每句 2.4 秒）取整后不会撞车。"""
        script = [{"speaker": "A" if i % 2 else "B", "emotion": "承接",
                   "text": "这是第 %d 句台词，用来占位。" % i} for i in range(300)]
        timings = [{"start": i * 2.437, "end": i * 2.437 + 2.4} for i in range(300)]
        lrc = S.build_lrc(script, {}, timings)
        txt = S.build_txt(script, {}, timings)
        self.assertEqual(LRC_ROUND.convert_text(lrc)[0], txt)
        self.assertEqual(LRC_ROUND.convert_text(lrc)[1]["dup"], [])


class TestFourSubtitlesAreRegisteredAsAssets(unittest.TestCase):
    """路径表：四份字幕同目录；前三份同前缀，洁版在前缀后加 `_clean`。"""

    PLAIN = ("subtitle", "subtitle_lrc", "subtitle_txt")
    ALL = ("subtitle", "subtitle_lrc", "subtitle_txt", "subtitle_clean_txt")

    def test_episode_files_carry_four_subtitles(self):
        ep = layout.episode_files("C:/root", "0007")
        sub = layout.sub_dir("C:/root")
        self.assertEqual(ep["subtitle"], os.path.join(sub, "0007.srt"))
        self.assertEqual(ep["subtitle_lrc"], os.path.join(sub, "0007.lrc"))
        self.assertEqual(ep["subtitle_txt"], os.path.join(sub, "0007.txt"))
        self.assertEqual(ep["subtitle_clean_txt"],
                         os.path.join(sub, "0007_clean.txt"))

    def test_four_subtitles_share_one_dir(self):
        ep = layout.episode_files("C:/root", "2db")
        sub = layout.sub_dir("C:/root")
        for k in self.ALL:
            with self.subTest(key=k):
                self.assertEqual(os.path.dirname(ep[k]), sub)

    def test_three_share_stem_and_clean_carries_a_suffix(self):
        ep = layout.episode_files("C:/root", "2db")
        stems = {os.path.splitext(ep[k])[0] for k in self.PLAIN}
        self.assertEqual(len(stems), 1, "前三份必须同前缀")
        self.assertEqual(os.path.splitext(ep["subtitle_clean_txt"])[0],
                         stems.pop() + "_clean", "洁版在前缀后加 _clean")
        codes = {os.path.basename(ep[k]).split(".")[-1] for k in self.ALL}
        self.assertEqual(codes, {"srt", "lrc", "txt"})

    def test_safe_no_applies_to_all_four(self):
        """期号里的危险字符由 `safe_no` 统一归置，四份一起受约束。"""
        ep = layout.episode_files("C:/root", "../2 a")
        safe = layout.safe_no("../2 a")
        self.assertTrue(ep["subtitle"].endswith(safe + ".srt"))
        self.assertTrue(ep["subtitle_lrc"].endswith(safe + ".lrc"))
        self.assertTrue(ep["subtitle_txt"].endswith(safe + ".txt"))
        self.assertTrue(ep["subtitle_clean_txt"].endswith(safe + "_clean.txt"))


class TestFourSubtitlesAreWiredIn(unittest.TestCase):
    """出片那一步必须**四份一起写**——这一条只能从源码上看，所以钉死在源码文本上。

    不真跑 pipeline（要 TTS 权重与渲染，几十分钟），而是断言「写盘那一处」的三件
    东西同时在场：路径取自路径表、内容取自生成函数、写盘结果进 `assets`。少任何
    一件，产物区/报告/清单就会与磁盘不一致。与 `test_gates.py` 里断言提示词文本
    是同一路数：钉的是**契约**，不是实现写法。
    """

    PAIRS = (("subtitle", "build_srt"),
             ("subtitle_lrc", "build_lrc"),
             ("subtitle_txt", "build_txt"),
             ("subtitle_clean_txt", "build_clean_txt"))

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "podcast_maker", "pipeline.py"),
                  encoding="utf-8") as f:
            cls.pipeline = f.read()
        with open(os.path.join(ROOT, "podcast_maker", "web_ui.py"),
                  encoding="utf-8") as f:
            cls.web = f.read()

    def test_pipeline_writes_all_four(self):
        for key, builder in self.PAIRS:
            with self.subTest(key=key):
                self.assertIn('ep["%s"]' % key, self.pipeline)
                self.assertIn("subtitle_engine.%s(" % builder, self.pipeline)
        # 四份都进 result 与 manifest 的 assets（报告与清单据此找产物）
        self.assertIn('result["subtitle_clean_txt"]', self.pipeline)
        self.assertIn('"subtitle_clean_txt": clean_path', self.pipeline)

    def test_clean_is_written_from_the_same_generation_step(self):
        """四份写在**同一段**（同一次生成、同一份 timings），不是另起一轮。

        洁版尤其要盯：它若跑去读磁盘上那份 `.txt` 再摘括号，这段里就会只剩文件
        读写、没有 `build_clean_txt(`——那正是「从产物反解」，本次明令不走。
        """
        seg = self.pipeline[self.pipeline.index('ep["subtitle_lrc"]'):
                            self.pipeline.index('result["subtitle_clean_txt"]')]
        for builder in ("build_lrc(", "build_txt(", "build_clean_txt("):
            with self.subTest(builder=builder):
                self.assertIn(builder, seg)
        self.assertIn('ep["subtitle_clean_txt"]', seg)
        self.assertIn("timings", seg)

    def test_product_page_lists_all_four(self):
        for key in ("ep.subtitle", "ep.subtitle_lrc", "ep.subtitle_txt",
                    "ep.subtitle_clean_txt"):
            with self.subTest(key=key):
                self.assertIn(key, self.web)


if __name__ == "__main__":
    unittest.main()
