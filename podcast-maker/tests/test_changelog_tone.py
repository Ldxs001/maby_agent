#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""更新日志的文风钉子。

更新日志记录的是**现象、根因、修法、验证**四件事，读者是后来查这件事的人。
人称叙事、产品立场论证、口语化措辞都不属于这四件事：它们让一段技术记录读起来
像现场对白，也让「这件事改了什么」被埋在话里。

> 「现象／根因／修复／验证」与「禁止对话主语」两条不是本文件新立的规矩 ——
> 它们在编写规范里已经写过一次，但只靠人记，于是又漏了。本文件把它们变成词表：
> 规则从「记得住」改成「绕不过」。

判据分三类，全部按**词表**扫正文（跳过头部体例区，那里写着词表本身）：

- 人称叙事：把「谁提的、我认为」写进技术记录；
- 产品立场论证：与代码行为无关的定位辩护（如「这个智能体是要分发出去的」）；
- 口语化措辞：坑 / 顺带 / 白跑 这类只在口头成立的词。

另有一条正向判据：体例区里那条款必须**在场**——规则删了，词表也就没人知道了。
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHANGELOG = os.path.join(_ROOT, "CHANGELOG.md")

#: 人称叙事与对话主语。技术记录不写「谁说的」——作者（提问、纠正、定稿）的反馈
#: 就是问题本身，不标注出处；情绪化的原话也不进工程文档。
BANNED_PERSON = ("主人", "我推荐", "我认为", "我判断", "我踩", "我说", "本机的我",
                 "用户说", "用户反馈", "用户纠正", "用户定稿", "用户提出", "用户要求")

#: 产品立场论证。与代码行为无关的辩护，不写进变更记录。
BANNED_POSITIONING = ("是要分发出去的", "这个智能体是", "大多数用户唯一", "用户机器上")

#: 口语化措辞。这些词在口头成立，在文档里不成立。
BANNED_COLLOQUIAL = ("坑", "顺带", "顺手", "压根", "白跑", "白烧", "白占",
                     "白写", "白干", "死档", "这轮", "卧槽")

#: 体例区里那条款必须存在。
REQUIRED_CLAUSE = "文风："


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _split():
    """按版本段标题切开，返回 (体例区, 正文)。

    头部体例区要写词表本身（「不出现『主人』」这种），扫进去是自证。切点只能按
    **行首**的 `## v` 认——体例那行里就写着 `` `## vX.Y.Z` ``，按子串切会在那里断。
    """
    text = _read("CHANGELOG.md")
    m = re.search(r"^## v", text, re.M)
    assert m, "CHANGELOG.md 里找不到版本段标题"
    return text[:m.start()], text[m.start():]


def _body():
    return _split()[1]


def _hits(words):
    out = []
    for i, line in enumerate(_body().splitlines(), 1):
        for w in words:
            if w in line:
                out.append((w, i, line.strip()[:90]))
    return out


class TestChangelogTone(unittest.TestCase):

    def test_no_first_person_narration(self):
        """不写人称叙事——记录的是这件事，不是谁提的。"""
        hits = _hits(BANNED_PERSON)
        self.assertEqual(hits, [], "更新日志里出现了人称叙事：%s" % hits)

    def test_no_product_positioning_argument(self):
        """不写产品立场论证——与代码行为无关的辩护不进变更记录。"""
        hits = _hits(BANNED_POSITIONING)
        self.assertEqual(hits, [], "更新日志里出现了产品立场论证：%s" % hits)

    def test_no_colloquial_markers(self):
        """不用口语化措辞。"""
        hits = _hits(BANNED_COLLOQUIAL)
        self.assertEqual(hits, [], "更新日志里出现了口语化措辞：%s" % hits)

    def test_the_style_clause_is_present(self):
        """体例区里那条款在场——规则删了，词表也就没人知道了。"""
        head = _split()[0]
        self.assertIn(REQUIRED_CLAUSE, head,
                      "体例区缺少「%s」那条款" % REQUIRED_CLAUSE)
        self.assertIn("test_changelog_tone", head,
                      "体例区该写明这条规则由哪个测试守住")

    def test_every_version_section_says_what_changed(self):
        """每个版本段都要有加粗小标题——「改了什么」不能只有流水句。"""
        body = _body()
        sections = re.split(r"^## v", body, flags=re.M)[1:]
        self.assertGreater(len(sections), 10, "版本段数量异常：%d" % len(sections))
        bare = []
        for sec in sections:
            ver = sec.splitlines()[0].strip()
            if "**" not in sec:
                bare.append(ver)
        self.assertEqual(bare, [], "这些版本段没有一个加粗小标题：%s" % bare)


if __name__ == "__main__":
    unittest.main()
