# -*- coding: utf-8 -*-
"""段内「查重 → 就地替换」与并句的可行域。

这一轮改掉两件事，各有各的钉子：

1. **重复不再靠删**。补字是复读的源头，所以补完立刻查、把「后出现的那种副本」
   就地换成新内容（`find_repeats` / `build_replace_prompt` / `apply_replace`），
   整期那一刀（`dedupe_script`）降级成最后兜底。步子不许退回「删了再补」——
   删会让这一段字数塌下去，下一轮又补、补出来又是重复，没尽头。

2. **并句不再是一个永远做不到的数**。从前判据要「并后 ≥ 原句合计 × 70%」，而提示
   词硬性要求「每句 ≤ 单句上限」（本机 40 字）——合计超过 57 字时两条数学上不可能
   同时满足，真机一整期一处都没落地。现在区间由 `absorb_span` 一家给，上限恒 ≤
   单句上限，判据与写给模型的数字同源。
"""

import unittest

from podcast_maker import script_engine as S


CFG = {"gate.max_chars": 40, "gate.min_chars": 8}

# 21 字、22 字——合计 43，落在「常规区间」里
S1 = "这套方法放到开放式探索任务上到底还适不适用"
S2 = "我的看法是要看任务边界是不是一开始就能说清楚"


def _line(text, speaker="A", emotion="承接"):
    return {"speaker": speaker, "emotion": emotion, "text": text}


class TestFindRepeats(unittest.TestCase):
    """查重：范围是整篇，替换范围是本段。"""

    def test_a_later_copy_inside_the_segment_is_named(self):
        script = [_line(S1), _line(S2), _line(S1)]
        self.assertEqual(S.find_repeats(script, 1, 3), {3: 1},
                         "同段里后出现的那一处要进名单，第一次出现的不进")

    def test_only_the_copies_inside_the_window_are_named(self):
        # 第 1 段写了 S1，第 2 段又写了一遍：写第 2 段时才看得见这次重复，
        # 而能改的也只有第 2 段——第 1 段早就定稿了。
        script = [_line(S1), _line(S2), _line(S1)]
        self.assertEqual(S.find_repeats(script, 3, 3), {3: 1})
        self.assertEqual(S.find_repeats(script, 1, 1), {},
                         "只查第 1 段时它没有重复（第一次出现就是它自己）")

    def test_short_lines_are_not_repeats(self):
        # 短句重复本来就是口语形态（「对。」「是的」），判重口径与整期兜底同一把尺。
        script = [_line("对吧"), _line("是的"), _line("对吧")]
        self.assertEqual(S.find_repeats(script, 1, 3), {})

    def test_punctuation_does_not_hide_a_repeat(self):
        script = [_line(S1 + "？"), _line(S2), _line(S1 + "。")]
        self.assertEqual(S.find_repeats(script, 1, 3), {3: 1},
                         "标点差异不该让同一句话逃过判重")


class TestApplyReplace(unittest.TestCase):
    """就地替换落地：行数不变、字数不塌、换完还得是新的。"""

    def setUp(self):
        self.script = [_line(S1), _line(S2), _line(S1)]

    def test_a_clean_swap_lands_and_keeps_the_line_count(self):
        out, rejected = S.apply_replace(
            self.script, [{"index": 3, "text": "换一句没讲过的话来把这个位置填上"}],
            [3], CFG)
        self.assertEqual(rejected, [])
        self.assertEqual(len(out), 3, "行数一个字都不许变")
        self.assertEqual(out[2]["text"], "换一句没讲过的话来把这个位置填上")
        self.assertEqual(out[2]["speaker"], "A", "说话人不归它改")

    def test_the_other_lines_are_untouched(self):
        out, _ = S.apply_replace(
            self.script, [{"index": 3, "text": "换一句没讲过的话来把这个位置填上"}],
            [3], CFG)
        self.assertEqual(out[0], self.script[0])
        self.assertEqual(out[1], self.script[1])

    def test_a_line_that_is_not_named_is_refused(self):
        out, rejected = S.apply_replace(
            self.script, [{"index": 1, "text": "这一句没被点名却被动了一下"}],
            [3], CFG)
        self.assertTrue(rejected, "点外的句子一律拒——它没毛病，重写它等于重摇骰子")
        self.assertEqual(out[0]["text"], S1)

    def test_too_short_is_refused(self):
        out, rejected = S.apply_replace(
            self.script, [{"index": 3, "text": "太短了"}], [3], CFG)
        self.assertTrue(rejected)
        self.assertIn("字数不许塌", rejected[0]["reason"])
        self.assertEqual(out[2]["text"], S1, "没落地就一个字都不动")

    def test_too_long_is_refused(self):
        long_text = "长" * 41
        _, rejected = S.apply_replace(
            self.script, [{"index": 3, "text": long_text}], [3], CFG)
        self.assertTrue(rejected)
        self.assertIn("超过单句上限", rejected[0]["reason"])

    def test_a_swap_that_collides_with_another_line_is_refused(self):
        # 换出来的还是别处已经讲过的话：等于把复读挪了个位置。
        _, rejected = S.apply_replace(
            self.script, [{"index": 3, "text": S2}], [3], CFG)
        self.assertTrue(rejected)
        self.assertIn("重复", rejected[0]["reason"])

    def test_an_emotion_outside_the_vocab_is_refused(self):
        _, rejected = S.apply_replace(
            self.script,
            [{"index": 3, "text": "换一句没讲过的话来把这个位置填上",
              "emotion": "开场"}], [3], CFG)
        self.assertTrue(rejected, "开场这类程序标签不归模型写")

    def test_an_emotion_inside_the_vocab_is_written(self):
        out, rejected = S.apply_replace(
            self.script,
            [{"index": 3, "text": "换一句没讲过的话来把这个位置填上",
              "emotion": "解释"}], [3], CFG)
        self.assertEqual(rejected, [])
        self.assertEqual(out[2]["emotion"], "解释")


class TestSpans(unittest.TestCase):
    """两个字数区间：替换的与并句的。"""

    def test_replace_span_is_never_empty(self):
        for was in (8, 12, 20, 31, 40, 60, 120):
            lo, hi = S.replace_span(was, CFG)
            self.assertLessEqual(lo, hi, "%d 字这一档区间空了" % was)
            self.assertLessEqual(hi, CFG["gate.max_chars"])
            self.assertGreaterEqual(lo, CFG["gate.min_chars"])

    def test_replace_span_is_capped_by_seventy_percent_of_the_cap(self):
        # 原句很长时，下限封顶在「单句上限的七成」：要求「一个字都不能少」会把
        # 落地率压到跟从前并句一样惨。
        self.assertEqual(S.replace_span(200, CFG), (28, 40))

    def test_absorb_span_keeps_the_ceiling_inside_the_cap(self):
        # 本机这档最要紧：A 的上限只有 1 句，A 连说两句就得并成一句。
        # 旧判据要 60 × 0.7 = 42 字，而上限是 40 —— 数学上无解。
        lo, hi = S.absorb_span(60, CFG)
        self.assertEqual((lo, hi), (28, 40))
        self.assertLessEqual(hi, CFG["gate.max_chars"])

    def test_absorb_span_falls_back_when_the_sum_is_small(self):
        # 合并两句总共才 25 字，不该被要求写出 28 字。
        self.assertEqual(S.absorb_span(25, CFG), (18, 25))

    def test_absorb_span_never_returns_an_empty_interval(self):
        for was in (4, 6, 8, 20, 25, 30, 57, 58, 60, 120, 400):
            lo, hi = S.absorb_span(was, CFG)
            self.assertLessEqual(lo, hi, "%d 字这一档区间空了" % was)

    def test_the_target_script_numbers_take_their_interval_from_the_same_span(self):
        """判据与处方**同源**：一个 60 字的两句，处方写的就是判据要的那个区间。"""
        script = [_line("甲" * 30), _line("乙" * 30)]
        report = {"items": [{"key": "ab_run_limit", "label": "连说超限",
                             "level": "fail", "judge": "code", "ok": False,
                             "runs": [{"speaker": "A", "cap": 1, "lines": [1, 2]}]}]}
        targets, _unfixed = S.patch_targets(report, 2, cfg=CFG)
        tip = "".join(targets.get(1) or [])
        lo, hi = S.absorb_span(60, CFG)
        self.assertIn("%d~%d 字" % (lo, hi), tip,
                      "处方里的数字必须取自 `absorb_span`，不许另算一遍")


class TestAbsorbLands(unittest.TestCase):
    """并句真的能落地——这条从前永远做不到。"""

    def setUp(self):
        self.script = [_line("甲" * 30), _line("乙" * 30)]
        self.targets = {1: ["t"], 2: ["t"]}

    def _land(self, text):
        return S.apply_patch(self.script,
                             [{"index": 1, "text": text, "absorb": 1}],
                             self.targets, None, CFG)

    def test_a_thirty_character_merge_now_lands(self):
        # 合计 60 字：旧判据要 ≥42 字，而单句上限 40 —— 怎么写都被拒。
        out, rejected = self._land("丙" * 30)
        self.assertEqual(rejected, [], "合计 60 字并出 30 字必须落地")
        self.assertEqual(len(out), 1, "absorb 1 → 两句并成一句")

    def test_the_whole_interval_lands(self):
        for n in (28, 34, 40):
            out, rejected = self._land("丙" * n)
            self.assertEqual(rejected, [], "%d 字应落在 28~40 内" % n)

    def test_below_the_floor_is_still_refused(self):
        _, rejected = self._land("丙" * 20)
        self.assertTrue(rejected, "20 字不到下限，仍要拒")
        self.assertIn("28~40 字", rejected[0]["reason"], "拒稿理由要报出区间")

    def test_above_the_cap_is_refused(self):
        _, rejected = self._land("丙" * 41)
        self.assertTrue(rejected, "超过单句上限的并句不能再放行——门禁下一轮就会打回它")


class TestThePrompts(unittest.TestCase):
    """提示词：口径改掉了，共享的判据没被抄成两份。"""

    def test_the_insert_prompt_forbids_restating(self):
        s = S.insert_system(None, CFG)
        self.assertIn("禁止重复已有的脚本内容", s)
        self.assertNotIn("但**复读不算补字**，凑不够就往细节里写", s,
                         "不许再从「字数」上给复读开脱")
        self.assertIn("复读不算补字", s, "但要让模型知道复读白补——否则它不懂代价")

    def test_the_back_door_is_gone(self):
        s = S.insert_system(None, CFG)
        self.assertNotIn("把已经提到的点讲得更细", s,
                         "「挑不出新东西就重讲已讲过的点」与禁令直接打架")

    def test_the_norepeat_rules_are_one_shared_block(self):
        block = S._norepeat_block()
        self.assertIn(block, S.insert_system(None, CFG))
        self.assertIn(block, S.replace_system(None, CFG),
                      "插入与替换必须听到同一份判据，不许各写一份")

    def test_the_replace_prompt_carries_the_whole_script(self):
        script = [_line(S1), _line(S2), _line(S1)]
        p = S.build_replace_prompt(script, {3: 1}, raw_material="原文在这一段",
                                   cfg=CFG)
        self.assertIn("【已写脚本】", p)
        self.assertIn(S1, p, "要换掉的那一句要摆出来给模型看")
        self.assertIn("与第 1 句重复", p, "要指名道姓说清跟哪一句撞了")
        self.assertIn("【要换掉的句子】", p)
        self.assertIn("【本段原文】", p)

    def test_the_replace_prompt_carries_the_interval(self):
        script = [_line("甲" * 30), _line(S2), _line("甲" * 30)]
        p = S.build_replace_prompt(script, {3: 1}, cfg=CFG)
        lo, hi = S.replace_span(30, CFG)
        self.assertIn("%d~%d 字" % (lo, hi), p,
                      "提示词与落地判据必须同一个区间")

    def test_the_insert_prompt_shows_the_earlier_segments(self):
        seg = [_line(S1), _line(S2)]
        prev = S._numbered_rows([_line("前面那一段讲过的话在这里摆着")], 0)
        p = S.build_insert_prompt(seg, 30, 100, 1, prev_rows=prev, cfg=CFG)
        self.assertIn("【已写脚本（前文）】", p)
        self.assertIn("前面那一段讲过的话在这里摆着", p,
                      "补字轮从前只给本段，前几段一个字都不给——就是这里漏的")

    def test_the_replace_schema_has_no_absorb(self):
        schema = S.replace_schema(CFG, max_index=3)
        item = schema["properties"]["edits"]["items"]
        self.assertNotIn("absorb", item["properties"],
                         "替换是「同位置换内容」，行数不许变——结构里根本写不出并句")
        self.assertEqual(item["properties"]["index"]["maximum"], 3)
        self.assertIn("minLength", item["properties"]["text"])


class TestTheBottomLinesAreHonest(unittest.TestCase):
    """报告与日志：不把重复说成「素材不够」。"""

    def test_the_dedupe_note_no_longer_blames_the_material(self):
        import io
        import re
        src = io.open(S.__file__, encoding="utf-8").read()
        self.assertNotIn("这说明素材撑不满目标时长", src,
                         "料的账在画地图时已按压比算过，这句话与它打架")
        self.assertIn("兜底删除", src)
        self.assertIn("兜底去重", src, "整期那一刀现在是兜底，日志要这么说")


if __name__ == "__main__":
    unittest.main()
