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

"""配置点位静态检查。

第四类缺陷：管线按某个点位名取值，这个点位却从未在配置总表里声明。取值
永远落回默认值，界面上也没有对应控件，值改不动——症状只在产出的图上看得见，
查起来要从图倒推回配置，成本极高。

这类缺陷靠人看不出，只能靠扫描代码里读了哪些点位、与总表逐一对账。
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker.config_manager import (coerce, MODE_SPEC, PARAM_SPEC,   # noqa: E402
                                         param_options, params_payload,
                                         section_of, ui_payload,
                                         VIEW_FALLBACK, VIEW_STAGES)

PKG = os.path.join(ROOT, "podcast_maker")

# 配置点位一律带命名空间（如 llm.model）。按点号筛，避免把同名的局部字典
# 当成配置读数——`cfg` 在若干函数里只是本地临时变量的名字。
KEY_RE = re.compile(r"\bcfg\.get\(\s*[\"']([a-z_]+(?:\.[a-z_]+)+)[\"']")


def _keys_read_in_source():
    """扫出管线代码里实际读取的配置点位，附上出处文件名。"""
    found = {}
    for name in sorted(os.listdir(PKG)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(PKG, name), encoding="utf-8") as fh:
            src = fh.read()
        for m in KEY_RE.finditer(src):
            found.setdefault(m.group(1), set()).add(name)
    return found


class TestConfigKeys(unittest.TestCase):
    """总表、档位表、代码读数三者必须对得上。"""

    def test_every_key_read_by_the_pipeline_is_declared(self):
        from podcast_maker import project_store
        read = _keys_read_in_source()
        self.assertTrue(read, "一条配置读数都没扫到，正则已失效")
        # 通道键是例外：它们不由人配，值是项目送下来的（见 project_store）。
        # 它们依然「有人读」——只是不摆在配置页上，所以放行而非塞回总表。
        allowed = set(PARAM_SPEC) | set(project_store.CONFIG_PASS_THROUGH)
        undeclared = {k: sorted(v) for k, v in read.items() if k not in allowed}
        self.assertFalse(undeclared,
                         "以下点位被代码读取，却没写进配置总表，取值只会落空：%s"
                         % undeclared)

    def test_every_key_is_namespaced(self):
        bad = [k for k in PARAM_SPEC if "." not in k]
        self.assertFalse(bad, "配置点位必须带命名空间：%s" % bad)

    def test_mode_spec_only_describes_declared_points(self):
        orphan = [k for k in MODE_SPEC if k not in PARAM_SPEC]
        self.assertFalse(orphan, "档位表里有总表没声明的点位：%s" % orphan)

    def test_every_mode_point_is_an_enum(self):
        for key in MODE_SPEC:
            with self.subTest(key=key):
                self.assertEqual(PARAM_SPEC[key]["type"], "enum",
                                 "%s 有档位表却不是枚举型" % key)

    def test_every_mode_default_is_one_of_its_options(self):
        for key, ms in MODE_SPEC.items():
            with self.subTest(key=key):
                default = PARAM_SPEC[key]["default"]
                self.assertIn(default, ms["options"],
                              "%s 的默认值 %r 不在档位里" % (key, default))


class TestUiEntry(unittest.TestCase):
    """第五类缺陷：点位声明了，界面上却没有入口。

    点位改动量本来就不一致——加一个点位只要往总表写一行，界面那边却要记得
    再手工挂一次。漏挂的症状是「这个参数调不了」，而且配置页看起来一切正常，
    因为缺的那一格只是不存在，不会报错。所以这里按总表逐项对账，缺一个就拦。
    """

    def test_every_point_reaches_the_config_page(self):
        groups = params_payload()["config"]
        listed = {}
        for sec, items in groups.items():
            for it in items:
                listed.setdefault(it["key"], []).append(sec)
        missing = sorted(k for k in PARAM_SPEC if k not in listed)
        self.assertFalse(missing,
                         "以下点位在配置页没有入口，用户永远调不到：%s" % missing)

    def test_no_point_is_rendered_twice_on_the_config_page(self):
        dup = {}
        for sec, items in params_payload()["config"].items():
            for it in items:
                dup[it["key"]] = dup.get(it["key"], 0) + 1
        twice = sorted(k for k, n in dup.items() if n > 1)
        self.assertFalse(twice, "以下点位在配置页被渲染了多次：%s" % twice)

    def test_every_point_has_resolved_stage_ownership(self):
        # 留白比填错更难查：一个点位没有阶段归属时，界面照样渲染（配置页
        # 收全量），只是它永远不出现在该出现的阶段页上，没人会注意到。
        for key, spec in PARAM_SPEC.items():
            with self.subTest(key=key):
                self.assertTrue(
                    "views" in spec or spec.get("group") in VIEW_FALLBACK,
                    "%s 既没声明 views，group=%r 也不在 VIEW_FALLBACK 里"
                    % (key, spec.get("group")))

    def test_every_point_carries_section_and_views(self):
        for stage, groups in params_payload().items():
            for sec, items in groups.items():
                for it in items:
                    with self.subTest(key=it["key"], stage=stage):
                        self.assertEqual(it["section"], sec)
                        self.assertEqual(it["section"], section_of(it["key"]))

    def test_declared_views_are_real_stages(self):
        for groups in params_payload().values():
            for items in groups.values():
                for it in items:
                    for v in it["views"]:
                        self.assertIn(v, VIEW_STAGES)

    def test_sections_have_a_label(self):
        labels = ui_payload()["section_labels"]
        used = {section_of(k) for k in PARAM_SPEC}
        missing = sorted(s for s in used if not labels.get(s))
        self.assertFalse(missing, "以下分区没有中文名，界面会直接显示英文前缀：%s" % missing)

    def test_both_font_points_share_one_card(self):
        """画面字体与字幕字体要并排摆。

        这两款管的是两处不同的地方，人挑的时候却在比字形——分在两张卡片，
        就得到两个地方各看一遍。点位前缀（frame / subtitle）本是默认归属，
        这里靠 SECTION_OF_OVERRIDE 把它们并到同一张「字体」卡片。
        """
        a = section_of("frame.font_family")
        b = section_of("subtitle.font_family")
        self.assertEqual(a, b, "两款字体没归到同一张卡片")
        self.assertTrue(ui_payload()["section_labels"].get(a),
                        "「字体」卡片没有中文名")

    def test_font_points_are_marked_for_the_self_drawn_picker(self):
        """两款字体都由自绘下拉渲染，标记由后端给。

        原生 <select> 的选项不接受自定义字体——浏览器直接忽略选项上的
        font-family，整列字长得一模一样，选字体就成了盲选。前端据此标记改走
        picker；标记若在页面里按点名硬写，加一款字体就得改两处。
        """
        # 标记在下发时补（见 web_ui._font_options）：候选表要靠本机字体探测，
        # 那是界面层的事，config_manager 不该反过来依赖它。
        from podcast_maker import web_ui as W
        want = {"frame.font_family", "subtitle.font_family"}
        self.assertEqual(set(W.FONT_KEYS), want)
        got = {}
        for items in W.api_config_get()["params"]["config"].values():
            for it in items:
                if it["key"] in want:
                    got[it["key"]] = it
        self.assertEqual(set(got), want, "两款字体点位都该在配置页出现")
        for key, it in got.items():
            with self.subTest(key=key):
                self.assertEqual(it["type"], "enum")
                self.assertTrue(it.get("font_pick"), "%s 没标 font_pick" % key)
                self.assertTrue(it["options"], "%s 的候选表是空的" % key)


class TestEnumLabels(unittest.TestCase):
    """第六类缺陷：档位只有值、没有中文标签，界面回退成英文裸值。

    `param_options` 是全项目唯一的枚举标签来源。标签一旦漏写，界面就把
    `argument` / `ultrafast` 这样的值直接摆给人看——值本身是对的，所以
    功能测试全过，只有人读界面时才看得出来。
    """

    def test_labels_are_never_the_raw_value(self):
        bad = []
        for key, spec in PARAM_SPEC.items():
            if spec["type"] != "enum":
                continue
            for it in param_options(key):
                if it["label"] == str(it["value"]):
                    bad.append("%s=%r" % (key, it["value"]))
        self.assertFalse(bad, "以下档位的标签就是值本身，界面会显示英文裸值：%s" % bad)

    def test_every_enum_has_at_least_two_options(self):
        for key, spec in PARAM_SPEC.items():
            if spec["type"] != "enum":
                continue
            with self.subTest(key=key):
                self.assertGreaterEqual(len(param_options(key)), 2,
                                        "%s 只有一个档位，等于没得选" % key)

    def test_values_keep_their_declared_type(self):
        # 表单回传一律是字符串，选项表却必须保留声明类型：44100 是 int，
        # 若在选项表里被字符串化，回填时 select 的 selected 判定就会失败。
        for key, spec in PARAM_SPEC.items():
            if spec["type"] != "enum":
                continue
            for it in param_options(key):
                with self.subTest(key=key, value=it["value"]):
                    self.assertIs(type(it["value"]),
                                  type(PARAM_SPEC[key]["default"]),
                                  "%s 的档位值类型与默认值不一致" % key)

    def test_style_preset_labels_come_from_preset_spec(self):
        from podcast_maker.config_manager import PRESET_SPEC
        got = {it["value"]: it["label"] for it in param_options("script.style_preset")}
        want = {k: v["label"] for k, v in PRESET_SPEC.items()}
        self.assertEqual(got, want, "风格档位标签与 PRESET_SPEC 脱钩了")

    def test_style_preset_desc_does_not_repeat_its_own_label(self):
        for it in param_options("script.style_preset"):
            with self.subTest(value=it["value"]):
                first = (it.get("desc") or "").split("、")[0]
                self.assertNotIn(it["label"], first,
                                 "%s 的说明以自身标签开头，等于把档位名念了两遍" % it["value"])


class TestCoerce(unittest.TestCase):
    """表单回传一律是字符串，归一必须按声明类型来比。"""

    def test_numeric_enum_accepts_form_strings(self):
        self.assertEqual(coerce("audio.sample_rate", "44100"), 44100)
        self.assertIsInstance(coerce("audio.sample_rate", "44100"), int)
        self.assertEqual(coerce("video.fps", "30"), 30)

    def test_enum_rejects_values_outside_the_table(self):
        for key, value in (("audio.sample_rate", "99999"), ("video.fps", "31"),
                           ("video.encoder_preset", "insane")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    coerce(key, value)

    def test_free_strings_pass_through(self):
        # 音色名由后端枚举，不可能写进档位表，必须原样放行
        self.assertEqual(coerce("tts.voice_a", "zh-CN-XiaoxiaoNeural"),
                         "zh-CN-XiaoxiaoNeural")
        self.assertEqual(coerce("tts.name_a", "老吴"), "老吴")

    def test_numeric_range_is_enforced(self):
        with self.assertRaises(ValueError):
            coerce("audio.bitrate_kbps", "999")
        self.assertEqual(coerce("audio.bitrate_kbps", "192"), 192)


class TestEngineScope(unittest.TestCase):
    """第十类缺陷：点位声明了引擎归属，引擎表里却没有这个值。

    音色按引擎分两套键（`tts.voice_a` 与 `tts.qwen3tts_voice_a`），界面据
    `engine_scope` 决定哪一组露出。scope 写错——拼错引擎名、或引擎改名后没跟着
    改——的症状是那一组**永远不显示**：用户只能改另一半，切过去发现是空的，
    而配置页乍看一切正常，也不会报错。
    """

    def test_engine_scope_matches_a_real_engine(self):
        engines = {o["value"] for o in param_options("tts.engine")}
        bad = []
        for key, spec in PARAM_SPEC.items():
            sc = spec.get("engine_scope")
            if sc is not None and sc not in engines:
                bad.append("%s -> %r" % (key, sc))
        self.assertFalse(bad,
                         "以下点位的 engine_scope 不是 tts.engine 的档位值：%s" % bad)

    def test_every_engine_has_both_voice_points(self):
        # 每个引擎都得有自己的一套 A/B 角音色，否则切到它就没音色可配。
        by_scope = {}
        for key, spec in PARAM_SPEC.items():
            if spec.get("engine_scope"):
                by_scope.setdefault(spec["engine_scope"], set()).add(key)
        for eng in sorted({o["value"] for o in param_options("tts.engine")}):
            keys = by_scope.get(eng, set())
            missing = [tail for tail in ("voice_a", "voice_b")
                       if not any(k.endswith(tail) for k in keys)]
            self.assertFalse(missing,
                             "引擎 %s 缺少 %s 的专属音色点位" % (eng, missing))

    def test_voice_points_all_declare_a_scope(self):
        # 音色点位不声明 scope 就会在两个引擎下同时露出，又回到「四个下拉
        # 并排、分不清哪个生效」的老样子。
        loose = [k for k in PARAM_SPEC
                 if k.startswith("tts.") and "voice_" in k
                 and not PARAM_SPEC[k].get("engine_scope")]
        self.assertFalse(loose,
                         "以下音色点位没声明引擎归属，会在两个引擎下同时露出：%s" % loose)


class TestVersionStep(unittest.TestCase):
    """第十一类缺陷：更新日志写到新版本了，程序报的还是旧版本号。

    v0.6.0 那次就是这个：`CHANGELOG.md` 已经开了 `## v0.6.0`，`config_manager.VERSION`
    还停在 0.5.0 —— 启动横幅、`/api/status`、HTTP `Server` 头三处一起报错版本，
    而「日志与版本号齐步」这条纪律全靠人记，迟早再漏一次。判据只有一条：日志里
    **最上面那个已发布的版本标题**必须就是 `VERSION`。

    允许在它上面另开「未发布」段攒批（那是发布前的工作区），但不能再出现一个比
    `VERSION` 更高的已发布标题 —— 那意味着日志先跑了、代码没跟上。
    """

    def _changelog(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_reported_version_is_the_top_released_heading(self):
        from podcast_maker.config_manager import VERSION
        headings = re.findall(r"^## (.+)$", self._changelog(), re.M)
        released = [h.strip() for h in headings if h.strip().startswith("v")]
        self.assertTrue(released, "更新日志里一个版本标题都没有")
        self.assertEqual(released[0], "v" + VERSION,
                         "更新日志最上面的已发布版本是 %s，而程序报的是 %s —— "
                         "两者必须齐步（v0.6.0 漏改过一次）"
                         % (released[0], VERSION))

    def test_unreleased_section_if_any_sits_on_top(self):
        # 「未发布」只能压在已发布标题之上；夹在中间说明有版本漏开标题。
        headings = [h.strip() for h in
                    re.findall(r"^## (.+)$", self._changelog(), re.M)]
        if "未发布" in headings:
            self.assertEqual(headings[0], "未发布",
                             "「未发布」段被挤到了已发布版本之下")


if __name__ == "__main__":
    unittest.main()
