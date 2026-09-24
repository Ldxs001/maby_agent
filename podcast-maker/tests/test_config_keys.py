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

import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker.config_manager import (coerce, MODE_SPEC, PARAM_SPEC,   # noqa: E402
                                         param_options, params_payload,
                                         section_of, ui_payload,
                                         validate_config,
                                         VIEW_FALLBACK, VIEW_STAGES)
from podcast_maker.config_manager import STYLE_DIMS, PRESET_SPEC         # noqa: E402

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

    def test_the_global_extra_requirement_point_stays_removed(self):
        """全局「额外要求」已下线，重点由项目级「重点方向」承担。

        留着它的入口比删掉更坏：同一个「补充说明」有两处可写，人改了新的一处
        以为生效，旧那处还在往提示词里塞话，症状是「写了不管用」。所以按源码扫
        一遍，防止哪天顺手加回来。
        """
        self.assertNotIn("script.extra_requirement", PARAM_SPEC)
        hits = []
        for name in sorted(os.listdir(PKG)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                if "extra_requirement" in fh.read():
                    hits.append(name)
        self.assertFalse(hits, "「额外要求」已下线，这些文件里还有残留：%s" % hits)

    def test_the_two_dropped_style_dims_stay_dropped(self):
        """风格倾向不再有「提问频率」与「情绪密度」，按源码扫一遍防回潮。

        这两维各自都是一条通往两套口径的路：提问频率低档顺手把「追问」从语篇
        枚举里摘掉，情绪密度低档顺手摘「铺垫/过渡」——提示词那边照旧列十个词，
        门禁这边只剩八个，写稿时合法的标签判的时候成了「词表外标签」。
        节奏归对话形式（paradigms.DIALOGUE_FORMS），词表只有一份常量。
        """
        for dim in ("emotion_density", "question_rate"):
            self.assertNotIn(dim, STYLE_DIMS, "%s 不该再出现在风格维度里" % dim)
        self.assertEqual(sorted(STYLE_DIMS), ["genre", "interaction", "metaphor_density"])
        for name, preset in PRESET_SPEC.items():
            for dim in ("emotion_density", "question_rate"):
                self.assertNotIn(dim, preset, "%s 档还带着 %s" % (name, dim))
        hits = []
        for name in sorted(os.listdir(PKG)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                text = fh.read()
            if "emotion_density" in text or "question_rate" in text:
                hits.append(name)
        self.assertFalse(hits, "这两维已下线，这些文件里还有残留：%s" % hits)

    def test_the_writing_choices_are_pickable_on_the_script_page(self):
        """「对话形式」必须与「风格倾向」并排摆在脚本页那一排。

        两者同类：都是写脚本那一刻才要定、定完直接点「生成脚本」的文体选择。
        它在配置页也有一份（那份是给「翻总表改全部」用的），但只有配置页那份
        不够——生成一次要切两次页，改的人会以为「改了没生效」。

        快捷区是硬编码白名单，键名写错一个字母不会报错，只会**静默少一格**：
        `specOf` 返回 null，`control` 直接返回，界面上一片安静。所以键名一起查。
        """
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"put2\('quick-script',\[(.*?)\]\);", src, re.S)
        self.assertIsNotNone(m, "没找到脚本页快捷区的键清单，正则已失效")
        keys = re.findall(r"'([a-z_]+(?:\.[a-z_]+)+)'", m.group(1))
        self.assertTrue(keys, "脚本页快捷区一个键都没解析出来")
        for k in keys:
            with self.subTest(key=k):
                self.assertIn(k, PARAM_SPEC, "快捷区挂了总表里没有的点位")
        for k in ("script.style_preset", "script.dialogue_form"):
            with self.subTest(wanted=k):
                self.assertIn(k, keys, "这一项必须能在脚本页选到")

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


class TestConfigMigration(unittest.TestCase):
    """第十三类缺陷：点位改了名或下了线，存量配置里的值静默消失。

    `load()` 只收 `PARAM_SPEC` 里有的键（白名单），这是对的——但改过名的键
    **已不在总表里**，于是白名单会把它连同值一起丢掉：用户配的窗口、框高、
    上滚时长一律回到默认值，界面上看不出任何异常。所以搬迁必须跑在过滤之前，
    这一组就是钉这条顺序。

    旧值 `dual` / `dual_named` 是另一种情形：值还在，但它已不在档位表内，
    下拉会显示成空。搬到 `lyric`——存着 `dual` 的多半是当年的默认值，人没主动
    选过；`dual_named` 的人当初要的是「名字」，顺手把那个开关打开，意图不丢。
    """

    def _load(self, saved):
        """把一份配置写进临时文件，让 ConfigManager 读它。"""
        from podcast_maker.config_manager import ConfigManager
        fd, path = tempfile.mkstemp(prefix="pm-cfg-", suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(saved, fh, ensure_ascii=False)
        return ConfigManager(path).data()

    def test_the_two_deleted_presets_land_on_lyric(self):
        for old in ("dual", "dual_named"):
            with self.subTest(preset=old):
                got = self._load({"subtitle.preset": old})
                self.assertEqual(got["subtitle.preset"], "lyric")
                self.assertIn(got["subtitle.preset"],
                              MODE_SPEC["subtitle.preset"]["options"],
                              "搬过去的值不在档位表里，下拉会显示成空")

    def test_dual_named_keeps_its_one_intention(self):
        """`dual_named` 的人当初点它是要「名字」，于是补上那个开关——但只补缺。

        从前「行首写不写说话人名」是塞在版式里的，没有独立开关；后来拆出来。
        老配置里那个开关**根本不存在**，所以搬迁要把它补成「开」（带名档的人
        要的就是名字）。反过来，如果配置里已经有一个明确的值，那一定是人在
        独立开关上做过的选择，搬迁不许覆盖它——补缺与覆盖是两回事。
        """
        got = self._load({"subtitle.preset": "dual_named",
                          "speaker_indicator.name_shown": False})
        self.assertFalse(got["speaker_indicator.name_shown"],
                         "搬迁覆盖了人在独立开关上做过的选择")
        # 开关缺席的老存档：补成「开」，这是带名档唯一的意思
        got = self._load({"subtitle.preset": "dual_named"})
        self.assertTrue(got["speaker_indicator.name_shown"])

    def test_the_three_renamed_points_keep_their_values(self):
        """改名的键必须搬着值走——白名单过滤在搬迁之后，顺序反了值就没了。"""
        got = self._load({"subtitle.lyric_window": 5,
                          "subtitle.lyric_max_rows": 11,
                          "subtitle.lyric_scroll_ms": 850})
        self.assertEqual(got["subtitle.window"], 5)
        self.assertEqual(got["subtitle.frame_rows"], 11)
        self.assertEqual(got["subtitle.scroll_ms"], 850)
        for dead in ("subtitle.lyric_window", "subtitle.lyric_max_rows",
                     "subtitle.lyric_scroll_ms"):
            self.assertNotIn(dead, got, "%s 没搬走，界面总表里已经没有它了" % dead)

    def test_an_already_migrated_value_wins_over_the_old_one(self):
        """两边都在（手工改过一半、或新旧版本互相覆盖过）：认新键。"""
        got = self._load({"subtitle.lyric_window": 5, "subtitle.window": 9})
        self.assertEqual(got["subtitle.window"], 9)

    def test_a_migrated_file_is_rewritten_in_the_new_names(self):
        """存档要跟着搬，不然每次启动都走一遍迁移路。"""
        from podcast_maker.config_manager import ConfigManager
        fd, path = tempfile.mkstemp(prefix="pm-cfg-", suffix=".json")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"subtitle.preset": "dual", "subtitle.lyric_window": 5}, fh)
        cm = ConfigManager(path)
        cm.save()
        with open(path, encoding="utf-8") as fh:
            back = json.load(fh)
        self.assertNotIn("subtitle.lyric_window", back)
        self.assertEqual(back["subtitle.window"], 5)


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


class TestConfigGrid(unittest.TestCase):
    """配置页的网格与格子骨架。

    这一页是「一格一控件」的密集排布，最容易出的毛病是**参差**：滑块自带一行
    刻度、下拉没有，开关连标题行都没有，于是同一行里各控件的水平线都不一样。
    治法是把每格钉成固定的四段（标题行 22px / 控件槽 34px / 刻度槽 15px / 说明），
    前两段是硬高度，控件自然落在同一条线上。

    但骨架**只能挂在配置页那张网格上**：脚本页与合成页的快捷区在左栏里只有
    ~370px 宽，用的还是自适应铺列。骨架若用裸 `.grid>` 挂到通用网格上，窄栏
    也会被套上固定列数——7 个控件挤成一列竖字。这一版正是这么踩过一次。
    """

    def _css(self):
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"<style>(.*?)</style>", src, re.S)
        self.assertIsNotNone(m, "没找到样式块，正则已失效")
        return m.group(1)

    def test_the_narrow_columns_keep_an_auto_filled_grid(self):
        """基础网格必须按可用宽度自动铺列，不许写死列数。

        它服务的是脚本页/合成页那两条窄栏（左栏 ~370px）。写死列数会让 7 个
        控件挤在一条竖线里，标题全部折行。
        """
        m = re.search(r"\n\.grid\{([^}]*)\}", self._css())
        self.assertIsNotNone(m, "基础 .grid 规则没了，正则已失效")
        self.assertIn("auto-fill", m.group(1),
                      "基础网格必须按可用宽度自动铺列，否则窄栏会被挤爆")

    def test_the_layout_skeleton_only_touches_the_config_grid(self):
        """骨架的每条选择器都必须限定在 `.grid.g4`（＝配置页那张网格）。

        漏掉限定不会报错，只会让窄栏跟着变形——所以按样式块逐条扫，逮住就拦。
        """
        bad = []
        for sel, body in re.findall(r"^([^\n{}]*)\{([^}]*)\}", self._css(), re.M):
            if not re.search(r"\.f(?![\w-])", sel):
                continue                       # 不是格子里的那条规则
            if not re.search(r"height:(?:22|34|15)px", body):
                continue                       # 不是骨架的硬高度
            if ".g4" not in sel:
                bad.append(sel.strip())
        self.assertFalse(bad, "这些骨架规则没限定在配置页网格上，窄栏会被一起约束：%s"
                              % bad)

    def test_bool_controls_sit_in_the_same_slot(self):
        """开关也得有标题行与控件槽。

        它从前自带文字、整块贴左，跟旁边的下拉既不同高也不在同一条线上，
        一行里就它最扎眼。拨杆进槽、标题上行、状态写在标题右端，才算齐。
        """
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"if\(spec\.type==='bool'\)\{(.*?)\n  \}", src, re.S)
        self.assertIsNotNone(m, "没找到开关那一支，正则已失效")
        self.assertIn('class="hold"', m.group(1),
                      "开关没有控件槽，会跟旁边的下拉错开一条水平线")
        self.assertIn('class="scale"', m.group(1),
                      "开关没有刻度槽占位，那一格会比邻居矮一截")

    # ---- 一格一控件 + 同类控件各占一行 ----
    # 上一版把「上下对齐」做成了「骨架固定四段」，但宽度仍按说明字数变：说明长的
    # 占两格、短的占一格，于是同一张卡里滑杆有的跨两格有的跨一格，行的起点对不齐；
    # 再加上 dense 回头填空，排在后头的控件被提上来，同类控件被拆到两行里去。
    # 这一版改两条：一律一格；形态变了就另起一行。

    def test_a_control_never_spans_two_columns_for_a_long_help(self):
        """格子宽度不许由说明长短决定。

        「说明长于 40 字就跨两列」是参差的直接来源：同一张卡里同一种控件宽度不一。
        说明长短只该决定这一格多高。
        """
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"const cls=\['f'\];(.*?)wrap\.className=cls\.join",
                      src, re.S)
        self.assertIsNotNone(m, "没找到格子类名那一段，正则已失效")
        body = m.group(1)
        self.assertNotIn("length", body,
                         "还在按说明字数决定跨不跨列，同一种控件就会有宽有窄")
        self.assertNotIn("w2", body, "跨两列的 w2 类还在发")
        self.assertNotIn(".w2", self._css(), "跨两列的 .w2 规则没下线")

    def test_rows_are_not_backfilled(self):
        """配置页网格不许 dense。

        dense 的回头填补会把排在后头的控件提上来填空档，形态分组就散了——
        而分组正是这一页的读法。
        """
        m = re.search(r"\n\.grid\.g4\{([^}]*)\}", self._css())
        self.assertIsNotNone(m, "配置页网格规则没了，正则已失效")
        self.assertNotIn("dense", m.group(1),
                         "dense 会让后面的控件回头填空，同类控件被拆到两行")

    def test_controls_are_grouped_by_shape_and_each_shape_starts_a_row(self):
        """滑杆归滑杆、开关归开关、下拉归下拉，且形态一变就另起一行。

        只把格子做齐不够：滑杆照样会跟下拉隔着五个格子。判定（ctlKind）必须与
        control() 里那几支 if 一一对应，判错了桶里就混进另一种控件。
        """
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"function ctlKind\(spec\)\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "没找到形态判定 ctlKind，正则已失效")
        body = m.group(1)
        # 开关 → 2，下拉 → 3（枚举 / 音色 / 模型 / 字体），滑杆 → 1、数字框 → 4，
        # 多行输入 → 5。顺序即排布顺序：滑杆 → 开关 → 下拉 → 输入框。
        self.assertIn("spec.type==='bool'", body)
        self.assertIn("return 2", body)
        self.assertIn("spec.type==='enum'||spec.options_source", body)
        self.assertIn("return 3", body)
        self.assertIn("spec.min!==undefined&&spec.max!==undefined", body)
        self.assertIn("?1:4", body)
        self.assertIn("if(spec.type==='text') return 5", body)
        self.assertLess(body.index("return 2"), body.index("return 3"),
                        "开关桶与下拉桶的先后被调了个儿")
        # 两个渲染口都必须走分桶铺法，少一处就有一处漏网
        self.assertIn("fillByKind(g,'cfg'", src, "配置页没走分桶铺法")
        self.assertIn("fillByKind(box,'quick'", src, "脚本/合成页快捷区没走分桶铺法")
        # 形态变了要另起一行：靠给每桶第一格加 .kstart 钉在列 1
        self.assertIn("node.classList.add('kstart')", src)
        self.assertIn("grid-column-start:1", self._css(),
                      "没有强制换行，两个开关后面紧跟的下拉会顶上来填空")

    # ---- 卡内功能区 ----
    # 只按类型分行会把一张卡里的几件事切成几段：LLM 那张卡既有「连到哪个后端、
    # 用哪个模型」，也有「生成参数」「超时」，分完行看不出谁和谁是一组。所以在卡内
    # 再切一层功能区（小标题），区内才按类型分行。表在 web_ui.py 的 ZONES 里。

    def _zones(self):
        """解析界面侧的 ZONES 表，返回 [(分区名, [点位…]), …] 按卡片分组。"""
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"const ZONES=\[(.*?)\n\];", src, re.S)
        self.assertIsNotNone(m, "没找到 ZONES 表，正则已失效")
        out = []
        for sec, label, keys in re.findall(
                r"\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*\[([^\]]*)\]\s*\]",
                m.group(1)):
            out.append((sec, label, re.findall(r"'([^']+)'", keys)))
        self.assertTrue(out, "ZONES 表解析出来是空的，正则已失效")
        return out

    def test_zone_table_matches_the_point_table(self):
        """功能区表必须与点位表对得上。

        表里写了不存在的点位＝打错字：那一条永远匹配不到，分组静默少一项。
        反过来，一张卡只要在表里出现过，它的点位就必须**全部**登记——漏掉的那个
        会掉进末尾的「其它」区，界面上就会凭空多出一行叫「其它」的小标题。
        """
        zones = self._zones()
        known = set(PARAM_SPEC)
        seen, by_sec = set(), {}
        for sec, label, keys in zones:
            self.assertTrue(label.strip(), "%s 里有个功能区没起名" % sec)
            for k in keys:
                self.assertIn(k, known, "%s 的功能区「%s」写了不存在的点位 %s"
                              % (sec, label, k))
                self.assertNotIn(k, seen, "点位 %s 被登记进两个功能区" % k)
                seen.add(k)
            labels = [l for s, l, _ in zones if s == sec]
            self.assertEqual(len(labels), len(set(labels)),
                             "%s 里有重名的功能区：%s" % (sec, labels))
            by_sec.setdefault(sec, []).extend(keys)
        for sec, keys in by_sec.items():
            mine = {k for k in known if section_of(k) == sec}
            self.assertEqual(mine, set(keys),
                             "%s 这张卡在表里出现了，但点位没登记全：漏 %s / 多 %s"
                             % (sec, sorted(mine - set(keys)),
                                sorted(set(keys) - mine)))
            self.assertGreaterEqual(len({l for s, l, _ in zones if s == sec}), 2,
                                    "%s 只切出一个功能区，等于白切——不画标题"
                                    "就该跟别的卡一样不进这张表" % sec)
        # 表里的分区名必须是界面自己的话，不许拿点位名当标题
        for sec, label, keys in zones:
            self.assertNotIn("script.", label)

    def test_each_zone_is_drawn_as_its_own_block_with_a_heading(self):
        """区是怎么落到界面上的：一个区一块网格，多区才画小标题。"""
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"function renderConfig\(\)\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "没找到 renderConfig，正则已失效")
        body = m.group(1)
        self.assertIn("zoneList(sec,", body, "配置页没走功能区归拢")
        self.assertIn("zones.length>1", body,
                      "单功能区也会画小标题，跟从前就不一样了")
        self.assertIn("fillByKind(g,'cfg',z.items)", body,
                      "区内的项没按类型分行")
        self.assertIn(".zhead", self._css(), "小标题没有样式")

    # ---- 路径类点位 ----
    # 它们填的是本机绝对路径，管线拿 os.path.exists 直接判：文件不在就当作没填。
    # 界面必须说明「这是什么」（占位符），并给一个「选择…」（本机文件对话框）。

    def test_path_points_declare_what_to_pick(self):
        """每个路径点位都要声明自己是选音频还是选图片，且只在这两类里。"""
        from podcast_maker import web_ui
        want = {"audio.intro_path": "audio", "audio.outro_path": "audio",
                "audio.ai_disclosure_path": "audio", "bgm.custom_path": "audio",
                "speaker_indicator.portrait_a": "image",
                "speaker_indicator.portrait_b": "image"}
        paths = {k: s for k, s in PARAM_SPEC.items() if s["type"] == "path"}
        self.assertEqual(set(paths), set(want),
                         "路径点位增删了，测试要跟着改：%s" % sorted(paths))
        for k, kind in want.items():
            self.assertEqual(paths[k].get("pick"), kind,
                             "%s 没声明选哪类文件（pick）" % k)
            self.assertIn(paths[k].get("pick"), web_ui.PICK_KINDS,
                          "%s 的 pick 值在后端没有对应的文件过滤表" % k)

    def test_path_controls_carry_a_placeholder_and_a_pick_button(self):
        """空框子要自己说清楚要什么，旁边还要有「选择…」。"""
        with open(os.path.join(PKG, "web_ui.py"), encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r"else if\(spec\.type==='path'\)\{(.*?)\n  \}",
                      src, re.S)
        self.assertIsNotNone(m, "没找到路径那一支，正则已失效")
        body = m.group(1)
        self.assertIn("本机绝对路径", body, "占位符没说清这是什么")
        self.assertIn('data-pick=', body, "路径框旁边没有「选择…」按钮")
        self.assertIn("/api/pickfile", src, "前端没有对应接口调用")

    def test_a_missing_path_file_is_reported(self):
        """填了不存在的文件必须说出来。

        片头音频、片尾音频、立绘、自备音乐这四类，文件不在是**静默当没填**——
        成品里什么都不出现，也不报错，人以为填上了。这一条把那种沉默打破。
        """
        cfg = {k: s["default"] for k, s in PARAM_SPEC.items()}
        warns, _errs = validate_config(cfg)
        self.assertFalse([w for w in warns if "找不到这个文件" in w],
                         "空路径不该报「找不到文件」")
        cfg["speaker_indicator.portrait_a"] = os.path.join(
            tempfile.gettempdir(), "pm-no-such-portrait-9527.png")
        warns, _errs = validate_config(cfg)
        hit = [w for w in warns if "找不到这个文件" in w]
        self.assertEqual(len(hit), 1, "路径不存在没报出来：%s" % warns)
        self.assertIn("A 角立绘", hit[0])
        # 真存在就不该报
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            real = fh.name
        try:
            cfg["speaker_indicator.portrait_a"] = real
            warns, _errs = validate_config(cfg)
            self.assertFalse([w for w in warns if "找不到这个文件" in w],
                             "文件明明在，却报了找不到")
        finally:
            os.unlink(real)


if __name__ == "__main__":
    unittest.main()
