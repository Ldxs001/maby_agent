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

"""项目登记表：一期一片，期号自己往前走。

第八类缺陷：换一档节目就要重填一遍风格与期号，出片之后也没人记得第几期了。
登记表是这份记忆的唯一载体，所以它的两条纪律要守住——
一是登记表坏了宁可报错也不重建（重建等于清空记忆），
二是只有过了门禁的片子才占期号（没过的片子占了号，后面全部错位）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker import layout as L                                     # noqa: E402
from podcast_maker import project_store as P                              # noqa: E402
from podcast_maker.project_store import ProjectError                      # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pm_proj_")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)


class TestEpisodeNumber(Base):
    def test_plain_numbers(self):
        self.assertEqual(P.bump_episode("1"), "2")
        self.assertEqual(P.bump_episode("9"), "10")
        self.assertEqual(P.bump_episode(7), "8")

    def test_width_is_preserved(self):
        # SP01 → SP02 而不是 SP2，位宽一丢，文件名排序就乱了
        self.assertEqual(P.bump_episode("SP01"), "SP02")
        self.assertEqual(P.bump_episode("ep009"), "ep010")

    def test_tail_suffix_is_kept(self):
        self.assertEqual(P.bump_episode("12-a"), "13-a")

    def test_non_numeric_is_returned_as_is(self):
        self.assertEqual(P.bump_episode("上"), "上")
        self.assertEqual(P.bump_episode(""), "")


class TestCreate(Base):
    def test_blank_name_is_refused(self):
        with self.assertRaises(ProjectError):
            P.create(self.base, "   ", "episodic")

    def test_unset_plan_means_the_model_decides(self):
        p = P.create(self.base, "甲档", "episodic", planned_episodes=None)
        self.assertIsNone(p["planned_episodes"])
        self.assertIsNone(P.progress(p)["planned"])
        self.assertIn("模型规划", P.progress(p)["label"])

    def test_set_plan_is_kept_as_a_number(self):
        p = P.create(self.base, "甲档", "episodic", planned_episodes="12")
        self.assertEqual(p["planned_episodes"], 12)
        self.assertEqual(P.progress(p)["label"], "0 / 12 期")

    def test_program_name_defaults_to_the_project_name(self):
        self.assertEqual(P.create(self.base, "甲档", "episodic")["program_name"], "甲档")
        self.assertEqual(
            P.create(self.base, "乙档", "episodic", program_name="乙节目")["program_name"], "乙节目")

    def test_projects_are_independent(self):
        a = P.create(self.base, "甲档", "episodic", style_preset="story", voice_a="v1")
        b = P.create(self.base, "乙档", "episodic", style_preset="debate", voice_a="v2")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(P.find(self.base, a["id"])["style_preset"], "story")
        self.assertEqual(P.find(self.base, b["id"])["style_preset"], "debate")

    def test_voice_dir_exists_from_birth(self):
        """音色档案是项目资产，立项就得有地方放。

        它不在出片时才建：先录参考音频、后写脚本也要能落盘，不然那一趟白跑。
        """
        p = P.create(self.base, "有声书", "episodic")
        root = L.project_dir(self.base, p["id"])
        self.assertTrue(os.path.isdir(L.voice_dir(root)),
                        "立项没建「音色」目录，参考音频没有落点")

    def test_local_engine_voice_fields_are_kept(self):
        """本地引擎的音色名与 Edge 的分开存。

        两套取值互不相通（Edge 是 zh-CN-XiaoxiaoNeural 这种，本地是 Vivian 这种），
        共用一个字段的话，在一边设好的值挪到另一边就是非法的。
        """
        p = P.create(self.base, "甲档", "episodic",
                     voice_a="zh-CN-XiaoxiaoNeural",
                     qwen_voice_a="Vivian", qwen_voice_b="Serena")
        got = P.find(self.base, p["id"])
        self.assertEqual(got["voice_a"], "zh-CN-XiaoxiaoNeural")
        self.assertEqual(got["qwen_voice_a"], "Vivian")
        self.assertEqual(got["qwen_voice_b"], "Serena")

    def test_old_registry_without_the_new_fields_still_loads(self):
        """老登记表里没有这两个字段，读出来是空而不是报错。

        空值即「用全局默认」，与立项时留空的行为一致 —— 不需要任何迁移。
        """
        pid = "20260101-000000"
        with open(os.path.join(self.base, L.STORE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 1, "projects": [
                {"id": pid, "name": "老项目", "plan_mode": "single",
                 "voice_a": "v1", "voice_b": "v2"}]}, f, ensure_ascii=False)
        got = P.find(self.base, pid)
        self.assertIsNotNone(got)
        self.assertEqual(got.get("qwen_voice_a", ""), "")
        cfg = P.apply_to_config({"tts.qwen3tts_voice_a": "Serena"}, got)
        self.assertEqual(cfg["tts.qwen3tts_voice_a"], "Serena",
                         "老项目没设本地音色，应当沿用全局")


class TestUpdate(Base):
    def test_plan_can_be_cleared_back_to_auto(self):
        p = P.create(self.base, "甲档", "episodic", planned_episodes=12)
        for blank in ("", "0", "None"):
            with self.subTest(blank=blank):
                P.update(self.base, p["id"], {"planned_episodes": blank})
                self.assertIsNone(P.find(self.base, p["id"])["planned_episodes"])

    def test_only_whitelisted_fields_are_written(self):
        p = P.create(self.base, "甲档", "episodic")
        P.update(self.base, p["id"], {"id": "改不了", "episodes": ["伪造"]})
        got = P.find(self.base, p["id"])
        self.assertEqual(got["id"], p["id"])
        self.assertEqual(got["episodes"], [])

    def test_missing_project_raises(self):
        with self.assertRaises(ProjectError):
            P.update(self.base, "不存在", {"note": "x"})

    def test_archive_keeps_everything(self):
        p = P.create(self.base, "甲档", "episodic", planned_episodes=3)
        P.record_episode(self.base, p["id"], "头一期", "1")
        P.archive(self.base, p["id"], True)
        got = P.find(self.base, p["id"])
        self.assertTrue(got["archived"])
        self.assertEqual(len(got["episodes"]), 1, "归档不是删除")


class TestDelete(Base):
    def test_delete_removes_the_entry_and_the_tree(self):
        p = P.create(self.base, "甲档", "mapped")
        root = L.project_dir(self.base, p["id"])
        self.assertTrue(os.path.isdir(root), "立项即建目录")
        with open(L.script_file(root, "1"), "w", encoding="utf-8") as f:
            f.write("{}")
        rep = P.delete(self.base, p["id"])
        self.assertIsNone(P.find(self.base, p["id"]))
        self.assertFalse(os.path.exists(root), "项目目录要一并删掉")
        self.assertTrue(rep["dir_removed"])
        self.assertGreater(rep["freed_bytes"], 0)

    def test_delete_only_touches_the_named_project(self):
        a = P.create(self.base, "甲档", "episodic")
        b = P.create(self.base, "乙档", "episodic")
        P.delete(self.base, a["id"])
        self.assertIsNone(P.find(self.base, a["id"]))
        self.assertIsNotNone(P.find(self.base, b["id"]))
        self.assertTrue(os.path.isdir(L.project_dir(self.base, b["id"])))

    def test_missing_project_raises_and_nothing_is_removed(self):
        a = P.create(self.base, "甲档", "episodic")
        with self.assertRaises(ProjectError):
            P.delete(self.base, "不存在")
        self.assertTrue(os.path.isdir(L.project_dir(self.base, a["id"])))

    def test_archived_project_can_still_be_deleted(self):
        # 归档是标记、删除是动作，两者独立：先归档再删，照样删得掉。
        p = P.create(self.base, "甲档", "episodic")
        P.archive(self.base, p["id"], True)
        rep = P.delete(self.base, p["id"])
        self.assertTrue(rep["dir_removed"])
        self.assertIsNone(P.find(self.base, p["id"]))

    def test_missing_tree_is_not_an_error(self):
        # 目录被手工挪走时条目仍要能拔掉：删不掉不是「安全」，是卡住人。
        p = P.create(self.base, "甲档", "episodic")
        shutil.rmtree(L.project_dir(self.base, p["id"]))
        rep = P.delete(self.base, p["id"])
        self.assertFalse(rep["dir_removed"])
        self.assertEqual(rep["freed_bytes"], 0)
        self.assertIsNone(P.find(self.base, p["id"]))

    def test_path_outside_the_store_is_refused(self):
        # 登记表被手改坏之后，删目录这一步不该连累到项目库之外。
        p = P.create(self.base, "甲档", "episodic")
        store = os.path.join(self.base, P.STORE_NAME)
        with open(store, encoding="utf-8") as f:
            data = json.load(f)
        for item in data["projects"]:
            if item["id"] == p["id"]:
                item["id"] = "../逃逸"
        with open(store, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        with self.assertRaises(ProjectError):
            P.delete(self.base, "../逃逸")


class TestRecordEpisode(Base):
    def test_recording_advances_the_next_number(self):
        p = P.create(self.base, "甲档", "episodic", first_episode="5")
        self.assertEqual(P.find(self.base, p["id"])["next_episode"], "5")
        P.record_episode(self.base, p["id"], "第一期", "5")
        got = P.find(self.base, p["id"])
        self.assertEqual(got["next_episode"], "6")
        self.assertEqual(P.progress(got)["done"], 1)

    def test_rerun_of_the_same_dir_does_not_double_count(self):
        p = P.create(self.base, "甲档", "episodic")
        P.record_episode(self.base, p["id"], "第一期", "1")
        P.record_episode(self.base, p["id"], "第一期改", "1")
        got = P.find(self.base, p["id"])
        self.assertEqual(len(got["episodes"]), 1)
        self.assertEqual(got["episodes"][0]["title"], "第一期改")

    def test_blank_pid_is_a_no_op(self):
        # 没有项目就不记账。界面已不再产生这种请求（单集也是一个项目），
        # 这条留着是接口层的兜底：pid 为空时不该写出半条记录。
        self.assertIsNone(P.record_episode(self.base, "", "t", "1"))

    def test_missing_project_raises(self):
        with self.assertRaises(ProjectError):
            P.record_episode(self.base, "不存在", "t", "1")


class TestPlanMode(Base):
    def test_mode_is_required(self):
        with self.assertRaises(ProjectError):
            P.create(self.base, "甲档", "")
        with self.assertRaises(ProjectError):
            P.create(self.base, "甲档", "随便写")

    def test_mode_is_kept(self):
        p = P.create(self.base, "甲档", "mapped")
        self.assertEqual(P.find(self.base, p["id"])["plan_mode"], "mapped")

    def test_mode_is_frozen_after_create(self):
        # 立项定死：中途换模式会让已排的地图失去来处。明确要求改就明确报错，
        # 静默忽略会让调用方以为改成了。
        p = P.create(self.base, "甲档", "episodic")
        with self.assertRaises(ProjectError):
            P.update(self.base, p["id"], {"plan_mode": "mapped"})
        self.assertEqual(P.find(self.base, p["id"])["plan_mode"], "episodic")

    def test_new_project_has_no_map(self):
        p = P.create(self.base, "甲档", "mapped")
        self.assertIsNone(P.find(self.base, p["id"])["map"])

    def test_old_store_reads_as_undecided(self):
        # 老登记表里没有 plan_mode。记成「未定」而不是替它选成逐期即兴：
        # 那些项目当年没做过这个选择，替它选掉，成稿规划就再也走不上了。
        os.makedirs(self.base, exist_ok=True)
        with open(P.store_path(self.base), "w", encoding="utf-8") as f:
            json.dump({"projects": [{"id": "old", "name": "旧档",
                                     "episodes": [], "next_episode": "3"}]}, f)
        got = P.find(self.base, "old")
        self.assertEqual(got["plan_mode"], "legacy")
        self.assertIsNone(got["map"])
        pr = P.progress(got)
        self.assertTrue(pr["legacy"])
        self.assertEqual(pr["mode_label"], "未定（旧项目）")
        # 未定的项目不进地图分支，行为与逐期即兴一致
        self.assertNotIn("已排图", pr["label"])

    def test_old_project_may_choose_a_mode_once(self):
        os.makedirs(self.base, exist_ok=True)
        with open(P.store_path(self.base), "w", encoding="utf-8") as f:
            json.dump({"projects": [{"id": "old", "name": "旧档",
                                     "episodes": [], "next_episode": "3"}]}, f)
        P.update(self.base, "old", {"plan_mode": "mapped"})
        got = P.find(self.base, "old")
        self.assertEqual(got["plan_mode"], "mapped")
        self.assertFalse(P.progress(got)["legacy"])
        # 补选一次之后同样定死
        with self.assertRaises(ProjectError):
            P.update(self.base, "old", {"plan_mode": "episodic"})
        self.assertEqual(P.find(self.base, "old")["plan_mode"], "mapped")

    def test_old_project_rejects_a_bogus_mode(self):
        os.makedirs(self.base, exist_ok=True)
        with open(P.store_path(self.base), "w", encoding="utf-8") as f:
            json.dump({"projects": [{"id": "old", "name": "旧档",
                                     "episodes": []}]}, f)
        with self.assertRaises(ProjectError):
            P.update(self.base, "old", {"plan_mode": "随便写"})
        self.assertEqual(P.find(self.base, "old")["plan_mode"], "legacy")

    def test_old_project_can_get_a_map_after_choosing(self):
        os.makedirs(self.base, exist_ok=True)
        with open(P.store_path(self.base), "w", encoding="utf-8") as f:
            json.dump({"projects": [{"id": "old", "name": "旧档",
                                     "episodes": []}]}, f)
        with self.assertRaises(ProjectError):
            P.set_map(self.base, "old", [{"no": "1", "title": "甲",
                                          "points": [], "refs": []}])
        P.update(self.base, "old", {"plan_mode": "mapped"})
        P.set_map(self.base, "old", [{"no": "1", "title": "甲",
                                      "points": [], "refs": []}])
        self.assertEqual(
            [e["no"] for e in P.map_episodes(P.find(self.base, "old"))], ["1"])

    def test_progress_reports_mode(self):
        p = P.create(self.base, "甲档", "mapped")
        pr = P.progress(P.find(self.base, p["id"]))
        self.assertEqual(pr["mode"], "mapped")
        self.assertEqual(pr["mode_label"], "成稿规划")
        self.assertEqual(pr["mapped"], 0)
        self.assertFalse(pr["legacy"])


class TestSingleMode(Base):
    """单集是一个项目，不是第二条路。

    从前它是「不选项目」——产物落到一个没人认领的目录里等人认领，画面也因此
    印不出节目名（没有项目就没有节目名）。收编成 `single` 之后，流程只剩一条：
    立项 → 脚本 → 合成。它同样排不出地图，也同样有节目名与副标题。
    """

    def test_it_is_a_project_like_any_other(self):
        p = P.create(self.base, "独一期", "single",
                     program_name="我思故我写", subtitle="AI 协作写成的书")
        got = P.find(self.base, p["id"])
        self.assertEqual(got["plan_mode"], "single")
        # 节目名与副标题照旧跟着项目走——画面文字就是从这儿取的
        self.assertEqual(got["program_name"], "我思故我写")
        self.assertEqual(got["subtitle"], "AI 协作写成的书")
        # 立项即建目录，与别的项目一视同仁
        self.assertTrue(os.path.isdir(L.project_dir(self.base, p["id"])))

    def test_plan_is_forced_to_one_episode(self):
        # 传什么都不作数：单集没有第二期，留着这个数会让进度条指向一个
        # 永远不会到的第二期。
        p = P.create(self.base, "独一期", "single", planned_episodes=9)
        self.assertEqual(P.find(self.base, p["id"])["planned_episodes"], 1)

    def test_plan_cannot_be_changed_later(self):
        p = P.create(self.base, "独一期", "single")
        P.update(self.base, p["id"], {"planned_episodes": 5})
        self.assertEqual(P.find(self.base, p["id"])["planned_episodes"], 1)

    def test_next_episode_does_not_advance(self):
        p = P.create(self.base, "独一期", "single")
        P.record_episode(self.base, p["id"], "链与两头", "1")
        got = P.find(self.base, p["id"])
        self.assertEqual(got["next_episode"], "1")
        pr = P.progress(got)
        self.assertTrue(pr["single"])
        self.assertEqual(pr["label"], "已出片")

    def test_progress_before_the_first_episode(self):
        p = P.create(self.base, "独一期", "single")
        pr = P.progress(P.find(self.base, p["id"]))
        self.assertEqual(pr["label"], "尚未出片")
        self.assertEqual(pr["mode_label"], "单集")
        self.assertFalse(pr["legacy"])

    def test_episodic_still_advances(self):
        # 对照：逐期即兴仍按期推进。单集与它的分别就是「有没有第二期」，
        # 这一条钉住两者不会一起漂。
        p = P.create(self.base, "连环档", "episodic")
        P.record_episode(self.base, p["id"], "第一期", "1")
        pr = P.progress(P.find(self.base, p["id"]))
        self.assertFalse(pr["single"])
        self.assertEqual(pr["next_episode"], "2")

    def test_it_never_gets_a_map(self):
        p = P.create(self.base, "独一期", "single")
        with self.assertRaises(ProjectError):
            P.set_map(self.base, p["id"], [{"no": "1", "title": "甲",
                                            "points": [], "refs": []}])


class TestParadigm(Base):
    """素材类型是排地图的组织依据。立项时可指定，留空表示按探查推断；
    与规划方式不同，它此后可以改——不产生产物，只是下一次重排的依据。"""

    def test_paradigm_is_kept(self):
        p = P.create(self.base, "甲档", "mapped", paradigm="methodology")
        got = P.find(self.base, p["id"])
        self.assertEqual(got["paradigm"], "methodology")
        self.assertEqual(P.progress(got)["paradigm_label"],
                         "方法论 / 理论专著")

    def test_unknown_paradigm_is_refused(self):
        # 写错类型名不能静默落成空串——那样界面显示「自适应」，人会以为
        # 自己选的是推断档，实际上什么都没选中。
        with self.assertRaises(ProjectError):
            P.create(self.base, "甲档", "mapped", paradigm="no-such-kind")

    def test_blank_falls_back_to_inference(self):
        p = P.create(self.base, "甲档", "mapped")
        got = P.find(self.base, p["id"])
        self.assertEqual(got["paradigm"], "")
        self.assertEqual(P.progress(got)["paradigm_label"],
                         "自适应（按结构推断）")

    def test_paradigm_can_be_changed_later(self):
        p = P.create(self.base, "甲档", "mapped", paradigm="methodology")
        P.update(self.base, p["id"], {"paradigm": "narrative"})
        self.assertEqual(P.find(self.base, p["id"])["paradigm"], "narrative")

    def test_update_rejects_unknown_paradigm(self):
        p = P.create(self.base, "甲档", "mapped")
        with self.assertRaises(ProjectError):
            P.update(self.base, p["id"], {"paradigm": "随便写"})
        self.assertEqual(P.find(self.base, p["id"])["paradigm"], "")

    def test_focus_note_is_kept_and_editable(self):
        # 侧重是人给这档节目写的方向，归项目（与素材类型同一档）：立项时可写，
        # 此后可改。留空是常态——空串就是「人没写过」，不是「人写了空话」。
        note = "多解析方法论，少讲技术细节与实现"
        p = P.create(self.base, "甲档", "mapped", focus_note=note)
        self.assertEqual(P.find(self.base, p["id"])["focus_note"], note)

        P.update(self.base, p["id"], {"focus_note": "只讲结论与边界"})
        self.assertEqual(P.find(self.base, p["id"])["focus_note"], "只讲结论与边界")

    def test_focus_note_defaults_to_empty(self):
        p = P.create(self.base, "甲档", "mapped")
        self.assertEqual(P.find(self.base, p["id"])["focus_note"], "")

    def test_focus_note_is_stripped(self):
        # 前后空白不算内容：排图提示词里拼的是这一段，留着空白只会在提示词里
        # 凭空多出几行空行，读日志的人还以为人写了什么。
        p = P.create(self.base, "甲档", "mapped", focus_note="  多讲方法论  \n")
        self.assertEqual(P.find(self.base, p["id"])["focus_note"], "多讲方法论")


class TestBranchNumber(Base):
    def test_single_letters(self):
        self.assertEqual(P.branch_no("3", 1), "3a")
        self.assertEqual(P.branch_no("3", 2), "3b")
        self.assertEqual(P.branch_no("21", 26), "21z")
        self.assertEqual(P.branch_no(" 7 ", 3), "7c")

    def test_carries_to_two_letters(self):
        self.assertEqual(P.branch_no("3", 27), "3aa")
        self.assertEqual(P.branch_no("3", 28), "3ab")
        self.assertEqual(P.branch_no("3", 52), "3az")
        self.assertEqual(P.branch_no("3", 53), "3ba")

    def test_missing_anchor_raises(self):
        with self.assertRaises(ProjectError):
            P.branch_no("", 1)

    def test_zero_index_raises(self):
        with self.assertRaises(ProjectError):
            P.branch_no("3", 0)

    def test_is_branch_no(self):
        self.assertTrue(P.is_branch_no("3a"))
        self.assertTrue(P.is_branch_no("21ab"))
        self.assertFalse(P.is_branch_no("3"))
        self.assertFalse(P.is_branch_no(""))
        self.assertFalse(P.is_branch_no("SP01"))


class TestMap(Base):
    def _rows(self, n=3):
        return [{"no": str(i), "title": "第%d期" % i, "points": ["要点"],
                 "refs": [{"source": "s1", "anchor": "第一章"}]}
                for i in range(1, n + 1)]

    def _mapped(self, **kw):
        return P.create(self.base, "甲档", "mapped", **kw)

    def test_set_and_read_map(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], self._rows())
        got = P.find(self.base, p["id"])
        self.assertEqual([e["no"] for e in P.map_episodes(got)], ["1", "2", "3"])
        self.assertEqual(got["map"]["episodes"][0]["title"], "第1期")

    def test_map_sets_planned_episodes(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], self._rows(5))
        self.assertEqual(P.find(self.base, p["id"])["planned_episodes"], 5)

    def test_episodic_project_has_no_map(self):
        p = P.create(self.base, "乙档", "episodic")
        with self.assertRaises(ProjectError):
            P.set_map(self.base, p["id"], self._rows())

    def test_duplicate_episode_no_rejected(self):
        p = self._mapped()
        rows = self._rows(2)
        rows[1]["no"] = "1"
        with self.assertRaises(ProjectError):
            P.set_map(self.base, p["id"], rows)

    def test_missing_title_rejected(self):
        p = self._mapped()
        rows = self._rows(1)
        rows[0]["title"] = "  "
        with self.assertRaises(ProjectError):
            P.set_map(self.base, p["id"], rows)

    def test_missing_episode_no_rejected(self):
        p = self._mapped()
        rows = self._rows(1)
        rows[0]["no"] = ""
        with self.assertRaises(ProjectError):
            P.set_map(self.base, p["id"], rows)

    def test_string_points_normalised(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], [{"no": "1", "title": "甲",
                                        "points": "单条", "refs": []}])
        self.assertEqual(P.map_episodes(P.find(self.base, p["id"]))[0]["points"],
                         ["单条"])

    def test_refs_without_source_dropped(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], [{
            "no": "1", "title": "甲",
            "refs": [{"anchor": "第一章"}, {"source": "s1", "anchor": "第一章"}]}])
        refs = P.map_episodes(P.find(self.base, p["id"]))[0]["refs"]
        self.assertEqual(len(refs), 1)

    def test_map_of_unknown_project_raises(self):
        with self.assertRaises(ProjectError):
            P.set_map(self.base, "不存在", self._rows())

    def test_next_from_map_follows_map_order(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], self._rows(3))
        self.assertEqual(P.next_from_map(P.find(self.base, p["id"])), "1")

    def test_next_from_map_skips_done(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], self._rows(3))
        P.record_episode(self.base, p["id"], "第1期", "1")
        got = P.find(self.base, p["id"])
        self.assertEqual(P.next_from_map(got), "2")
        self.assertEqual(got["next_episode"], "2")

    def test_next_from_map_is_empty_when_finished(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], self._rows(1))
        P.record_episode(self.base, p["id"], "第1期", "1")
        self.assertEqual(P.next_from_map(P.find(self.base, p["id"])), "")

    def test_branch_episode_advances_within_the_map(self):
        # 分支期号靠数字进位算不出来：bump_episode("2a") 会得到 "3a"，
        # 把插进来的分支当成了新主线。
        p = self._mapped()
        rows = self._rows(3)
        rows.insert(2, {"no": "2a", "title": "插进来的", "points": [],
                        "refs": []})
        P.set_map(self.base, p["id"], rows)
        P.record_episode(self.base, p["id"], "第1期", "1")
        P.record_episode(self.base, p["id"], "第2期", "2")
        P.record_episode(self.base, p["id"], "插进来的", "2a")
        got = P.find(self.base, p["id"])
        # 若按数字进位，2a 之后会算出 3a，把插入的分支当成了新主线
        self.assertEqual(got["next_episode"], "3")

    def test_no_map_keeps_plain_increment(self):
        p = P.create(self.base, "乙档", "episodic", first_episode="7")
        P.record_episode(self.base, p["id"], "第七期", "7")
        self.assertEqual(P.find(self.base, p["id"])["next_episode"], "8")

    def test_progress_label_shows_map_size(self):
        p = self._mapped()
        P.set_map(self.base, p["id"], self._rows(4))
        pr = P.progress(P.find(self.base, p["id"]))
        self.assertEqual(pr["mapped"], 4)
        self.assertIn("已排图", pr["label"])


class TestInsertBranches(Base):
    def _mapped_with_map(self):
        p = P.create(self.base, "甲档", "mapped")
        P.set_map(self.base, p["id"], [
            {"no": str(i), "title": "第%d期" % i, "points": [], "refs": []}
            for i in range(1, 4)])
        return p

    def test_branch_lands_after_the_anchor(self):
        p = self._mapped_with_map()
        P.insert_branches(self.base, p["id"], "2", [
            {"no": "2a", "title": "插一", "points": ["p"], "refs": []},
            {"no": "2b", "title": "插二", "points": [], "refs": []}])
        got = P.find(self.base, p["id"])
        self.assertEqual([e["no"] for e in P.map_episodes(got)],
                         ["1", "2", "2a", "2b", "3"])

    def test_second_batch_lands_after_the_first(self):
        # 回归：第二批插在锚点**正后方**时，会挤到第一批前面（3、新批、旧批）
        # ——落位必须在锚点分支链的末尾，后插的批永远排在先插的批后面。
        p = self._mapped_with_map()
        P.insert_branches(self.base, p["id"], "2", [
            {"no": "2a", "title": "第一批", "points": [], "refs": []}])
        P.insert_branches(self.base, p["id"], "2", [
            {"no": "2b", "title": "第二批", "points": [], "refs": []}])
        got = P.find(self.base, p["id"])
        self.assertEqual([e["no"] for e in P.map_episodes(got)],
                         ["1", "2", "2a", "2b", "3"])

    def test_lands_at_chain_end_even_if_chain_is_out_of_order(self):
        # 历史脏数据不假设链连续：分支在地图上乱序时，插到全表最后一条分支之后
        p = self._mapped_with_map()
        P.set_map(self.base, p["id"], [
            {"no": "1", "title": "一", "points": [], "refs": []},
            {"no": "2", "title": "二", "points": [], "refs": []},
            {"no": "2e", "title": "旧批", "points": [], "refs": []},
            {"no": "2a", "title": "更旧的批", "points": [], "refs": []},
            {"no": "3", "title": "三", "points": [], "refs": []}])
        P.insert_branches(self.base, p["id"], "2", [
            {"no": "2f", "title": "新批", "points": [], "refs": []}])
        got = P.find(self.base, p["id"])
        self.assertEqual([e["no"] for e in P.map_episodes(got)],
                         ["1", "2", "2e", "2a", "2f", "3"])

    def test_anchor_branch_gets_nested_branches(self):
        # 锚点本身是分支期（2a）时，新分支号继续往下长（2aa），落在它的链尾
        p = self._mapped_with_map()
        P.insert_branches(self.base, p["id"], "2", [
            {"no": "2a", "title": "上层分支", "points": [], "refs": []}])
        P.insert_branches(self.base, p["id"], "2a", [
            {"no": "2aa", "title": "下层分支", "points": [], "refs": []}])
        got = P.find(self.base, p["id"])
        self.assertEqual([e["no"] for e in P.map_episodes(got)],
                         ["1", "2", "2a", "2aa", "3"])

    def test_digit_episodes_are_not_mistaken_for_branches(self):
        # 30、31 是数字期号，不能被当成锚点 3 的字母分支
        p = P.create(self.base, "甲档", "mapped")
        P.set_map(self.base, p["id"], [
            {"no": "3", "title": "三", "points": [], "refs": []},
            {"no": "30", "title": "三十", "points": [], "refs": []}])
        P.insert_branches(self.base, p["id"], "3", [
            {"no": "3a", "title": "分支", "points": [], "refs": []}])
        got = P.find(self.base, p["id"])
        self.assertEqual([e["no"] for e in P.map_episodes(got)],
                         ["3", "3a", "30"])

    def test_planned_episodes_grows(self):
        p = self._mapped_with_map()
        P.insert_branches(self.base, p["id"], "1", [
            {"no": "1a", "title": "插一", "points": [], "refs": []}])
        self.assertEqual(P.find(self.base, p["id"])["planned_episodes"], 4)

    def test_duplicate_branch_no_rejected(self):
        p = self._mapped_with_map()
        P.insert_branches(self.base, p["id"], "2", [
            {"no": "2a", "title": "插一", "points": [], "refs": []}])
        with self.assertRaises(ProjectError):
            P.insert_branches(self.base, p["id"], "2", [
                {"no": "2a", "title": "重号", "points": [], "refs": []}])

    def test_missing_anchor_rejected(self):
        p = self._mapped_with_map()
        with self.assertRaises(ProjectError):
            P.insert_branches(self.base, p["id"], "99", [
                {"no": "99a", "title": "插一", "points": [], "refs": []}])

    def test_empty_payload_rejected(self):
        p = self._mapped_with_map()
        with self.assertRaises(ProjectError):
            P.insert_branches(self.base, p["id"], "1", [])

    def test_project_without_map_rejected(self):
        p = P.create(self.base, "乙档", "mapped")
        with self.assertRaises(ProjectError):
            P.insert_branches(self.base, p["id"], "1", [
                {"no": "1a", "title": "插一", "points": [], "refs": []}])

    def test_insert_does_not_touch_done_episodes(self):
        p = self._mapped_with_map()
        P.record_episode(self.base, p["id"], "第1期", "1")
        P.insert_branches(self.base, p["id"], "1", [
            {"no": "1a", "title": "插一", "points": [], "refs": []}])
        got = P.find(self.base, p["id"])
        self.assertEqual(got["episodes"][0]["no"], "1")
        self.assertEqual(P.progress(got)["done"], 1)


class TestStoreIntegrity(Base):
    def test_corrupt_store_raises_instead_of_rebuilding(self):
        # 静默重建会把全部期号记忆抹掉，而且没人知道发生过
        os.makedirs(self.base, exist_ok=True)
        with open(P.store_path(self.base), "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        with self.assertRaises(ProjectError):
            P.summary(self.base)

    def test_wrong_shape_raises(self):
        with open(P.store_path(self.base), "w", encoding="utf-8") as f:
            json.dump({"projects": "不是数组"}, f)
        with self.assertRaises(ProjectError):
            P.summary(self.base)

    def test_absent_store_means_empty_not_error(self):
        self.assertEqual(P.summary(self.base), [])

    def test_write_leaves_no_temp_file(self):
        P.create(self.base, "甲档", "episodic")
        self.assertFalse(os.path.exists(P.store_path(self.base) + ".tmp"))


class TestOrphans(Base):
    def _tree(self, name, manifest=True):
        """建一棵没登记过的树，产物摆进它的「报告」子目录。"""
        root = os.path.join(self.base, name)
        os.makedirs(root, exist_ok=True)
        if manifest:
            rdir = L.report_dir(root)
            os.makedirs(rdir, exist_ok=True)
            with open(os.path.join(rdir, "%s.manifest.json" % name), "w",
                      encoding="utf-8") as f:
                f.write("{}")
        return root

    def test_unclaimed_products_are_listed(self):
        self._tree("20260101-010101")
        dirs = [o["dir"] for o in P.orphans(self.base)]
        self.assertEqual(dirs, ["20260101-010101"])

    def test_claimed_products_disappear_from_the_list(self):
        # 归属的判据是目录名就是项目 id：登记过的树不列。
        p = P.create(self.base, "甲档", "episodic")
        self._tree(p["id"])
        self.assertEqual(P.orphans(self.base), [])

    def test_trees_without_products_are_ignored(self):
        # 只写了半截的目录不算产物，不提示人去认领
        self._tree("20260101-010101", manifest=False)
        self.assertEqual(P.orphans(self.base), [])


class TestApplyToConfig(Base):
    """项目设定叠加到配置上：生成与出片必须共用同一份覆盖结果。

    这两处各写一份映射是极容易犯的错：生成时按全局节目名写片头句，出片时
    按项目节目名去验，片头句就变成错的，而两处代码单看都挑不出毛病。
    """

    CFG = {"project.program_name": "播客", "script.style_preset": "argument",
           "tts.voice_a": "v1", "tts.voice_b": "v2",
           "tts.name_a": "小思", "tts.name_b": "小笔",
           "audio.bitrate_kbps": 192}

    def test_local_engine_voices_have_their_own_fields(self):
        """本地引擎的音色另有一套键，项目里也必须另存一份。

        此前项目字段只映射到 `tts.voice_a`（Edge 那套），而本地引擎读的是
        `tts.qwen3tts_voice_a` —— 于是项目里选了音色对本地引擎完全不生效，
        界面上那个下拉是个死框。
        """
        cfg = dict(self.CFG, **{"tts.qwen3tts_voice_a": "Serena",
                                "tts.qwen3tts_voice_b": "Uncle_Fu"})
        got = P.apply_to_config(cfg, {"qwen_voice_a": "Vivian",
                                      "qwen_voice_b": "Eric"})
        self.assertEqual(got["tts.qwen3tts_voice_a"], "Vivian")
        self.assertEqual(got["tts.qwen3tts_voice_b"], "Eric")
        # 两套键互不相通：本地那套被盖，Edge 那套不该跟着动
        self.assertEqual(got["tts.voice_a"], "v1")
        self.assertEqual(got["tts.voice_b"], "v2")

    def test_blank_local_voice_keeps_the_global(self):
        got = P.apply_to_config(self.CFG, {"qwen_voice_a": ""})
        self.assertNotIn("tts.qwen3tts_voice_a", got,
                         "项目里留空就不该往配置上叠一个空值")

    def test_project_values_win(self):
        item = {"program_name": "验收节目", "style_preset": "story",
                "voice_a": "va", "voice_b": "vb", "name_a": "甲", "name_b": "乙"}
        got = P.apply_to_config(self.CFG, item)
        self.assertEqual(got["project.program_name"], "验收节目")
        self.assertEqual(got["script.style_preset"], "story")
        self.assertEqual(got["tts.voice_a"], "va")
        self.assertEqual(got["tts.name_a"], "甲")

    def test_the_input_config_is_not_touched(self):
        # 回写全局的后果是：做完 A 项目再开 B 项目，A 的风格已经渗进默认值
        before = dict(self.CFG)
        P.apply_to_config(self.CFG, {"program_name": "验收节目"})
        self.assertEqual(self.CFG, before, "覆盖结果绝不能回写全局配置")

    def test_blank_project_fields_do_not_clobber_globals(self):
        got = P.apply_to_config(self.CFG, {"program_name": "", "style_preset": "",
                                           "name_a": None})
        self.assertEqual(got["project.program_name"], "播客")
        self.assertEqual(got["script.style_preset"], "argument")
        self.assertEqual(got["tts.name_a"], "小思")

    def test_untouched_keys_survive(self):
        got = P.apply_to_config(self.CFG, {"program_name": "验收节目"})
        self.assertEqual(got["audio.bitrate_kbps"], 192)

    def test_no_project_is_a_plain_copy(self):
        got = P.apply_to_config(self.CFG, None)
        self.assertEqual(got, self.CFG)
        self.assertIsNot(got, self.CFG, "必须是副本，调用方改它不该影响原对象")

    def test_mapping_targets_are_real_points(self):
        from podcast_maker.config_manager import PARAM_SPEC
        allowed = tuple(PARAM_SPEC) + tuple(P.CONFIG_PASS_THROUGH)
        for pkey, ckey in P.PROJECT_CONFIG_MAP:
            with self.subTest(ckey=ckey):
                self.assertIn(ckey, allowed,
                              "覆盖目标 %s 既不是配置点位，也不是通道键，"
                              "写了也不会有人读" % ckey)

    def test_pass_through_keys_stay_out_of_the_config_page(self):
        """通道键不能同时又当配置点位——那就等于配置文件里那个死框。

        配置页摆一个节目名输入框，填完却总被项目盖掉，人只会以为程序坏了。
        """
        from podcast_maker.config_manager import PARAM_SPEC
        for k in P.CONFIG_PASS_THROUGH:
            with self.subTest(key=k):
                self.assertNotIn(k, PARAM_SPEC, "%s 不该出现在配置总表里" % k)

    def test_mapping_matches_the_stored_project_fields(self):
        for pkey, _ in P.PROJECT_CONFIG_MAP:
            with self.subTest(pkey=pkey):
                self.assertIn(pkey, P.create(self.base, "甲档", "episodic"))


if __name__ == "__main__":
    unittest.main()
