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

"""本地语音环境的三态，以及两处入口说的是同一句话。

环境、依赖、模型这三件事归**配置阶段**：用户把语音引擎选成 Qwen3-TTS，缺什么
就该在配置页补什么（点「搭建」，它跑 setup_env.py）。合成阶段只剩「在不在线」。
这里钉住：

- 三态说的话互不相同，尤其「不用手动开」只能出现在真的不用动手的那一态；
- 配置页读的 local_state() 与拉起时报的 TTSError，说的是同一句；
- 判定看文件在不在，不 import（探活会被反复调用，import torch 要几秒）；
- 安装实现只有 setup_env.py 一份，两个入口都调它。

另一个类钉的是脚本里那些不能退回去的事实：依赖版本、安装实现的唯一性、
环境归属、验收脚本取服务输出的方式。每一条都对应一次真实事故，类内有说明。
"""

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

from podcast_maker import layout
from podcast_maker import tts_engine

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 本机必然没人监听的端口：连上去立刻被拒，探活稳定走离线分支。
DEAD = {"tts.qwen3tts_host": "127.0.0.1", "tts.qwen3tts_port": 1}

VENV_PY = r"C:\pkg\tts_service\.venv\Scripts\python.exe"


def _fake_state(env=True, packages=None, model=True):
    """造一份三态。只替换判据，不碰真环境。"""
    packages = list(packages or [])
    return {
        "env": {"ok": env, "path": r"C:\pkg\tts_service\.venv",
                "python": VENV_PY if env else ""},
        "packages": {"ok": not packages, "missing": packages},
        "model": {"ok": model,
                  "path": r"C:\pkg\tts_service\models\M" if model else ""},
    }


def _patch(**kw):
    """把三态判据一起换掉，只留要验的那一态为「缺」。

    判据只有一份实现（setup_env.state），所以这里也只打一个桩 ——
    探活、配置页、拉起前拦阻走的都是它。
    """
    return mock.patch.object(tts_engine.tts_setup, "state",
                             return_value=_fake_state(**kw))


class TestThreeStates(unittest.TestCase):
    """连不上时，提示跟着「缺哪一样」变，并且都指向配置页那一个动作。"""

    def _msg(self, **kw):
        with _patch(**kw):
            ok, msg, _ = tts_engine.service_health(DEAD, timeout=1)
        self.assertFalse(ok)
        return msg

    def test_missing_env_points_at_the_config_page(self):
        msg = self._msg(env=False)
        self.assertIn("运行环境还没建", msg)
        self.assertIn("配置页", msg, "三态的动作都在配置页，不是让人去翻命令行")
        self.assertNotIn("不用手动开", msg)
        self.assertNotIn("模型权重", msg, "环境都没建，不该把人引去下模型")

    def test_missing_packages_names_them(self):
        msg = self._msg(packages=["torch", "transformers"])
        self.assertIn("依赖还没装齐", msg)
        self.assertIn("torch、transformers", msg, "要点名缺了什么，不是笼统一句")
        self.assertIn("配置页", msg)
        # 这两条是本次修复的要点：装都没装，就不能说「不用手动开」，
        # 也不能把人引去下那 4 GB 权重。
        self.assertNotIn("不用手动开", msg)
        self.assertNotIn("模型权重", msg)

    def test_missing_model_is_its_own_state(self):
        msg = self._msg(model=False)
        self.assertIn("模型权重还没下", msg)
        self.assertIn("配置页", msg)
        self.assertNotIn("依赖还没装齐", msg)
        self.assertNotIn("不用手动开", msg)

    def test_all_present_but_idle_promises_autostart(self):
        msg = self._msg()
        self.assertIn("没在跑", msg)
        self.assertIn("自动拉起", msg)
        for other in ("运行环境还没建", "依赖还没装齐", "模型权重还没下"):
            self.assertNotIn(other, msg)

    def test_the_four_states_never_share_a_sentence(self):
        """四态互不相同——否则「分态」只是摆设。"""
        got = {self._msg(env=False), self._msg(packages=["torch"]),
               self._msg(model=False), self._msg()}
        self.assertEqual(len(got), 4)

    def test_local_state_carries_the_same_wording(self):
        """配置页读的是 local_state()，它和探活必须说同一句。"""
        with _patch(packages=["torch"]):
            st = tts_engine.local_state()
            self.assertFalse(st["ready"])
            self.assertEqual(st["message"], self._msg(packages=["torch"]))
        with _patch():
            self.assertTrue(tts_engine.local_state()["ready"])


class TestOneWordingEverywhere(unittest.TestCase):
    """拉起时报的错，和探活时说的话，是同一句。"""

    def test_spawn_error_reuses_the_not_installed_message(self):
        with _patch(packages=["torch"]):
            with self.assertRaises(tts_engine.TTSError) as cm:
                tts_engine._spawn_service(DEAD)
        self.assertEqual(str(cm.exception),
                         tts_engine._state_message(_fake_state(packages=["torch"])))

    def test_spawn_error_also_reports_a_missing_model(self):
        with _patch(model=False):
            with self.assertRaises(tts_engine.TTSError) as cm:
                tts_engine._spawn_service(DEAD)
        self.assertEqual(str(cm.exception),
                         tts_engine._state_message(_fake_state(model=False)))

    def test_spawn_error_reports_a_missing_env_before_anything_else(self):
        """环境没建时，拉起不该去谈依赖或模型——先得把环境建出来。"""
        with _patch(env=False), \
                mock.patch.object(tts_engine, "venv_python", return_value=None):
            with self.assertRaises(tts_engine.TTSError) as cm:
                tts_engine._spawn_service(DEAD)
        self.assertIn("运行环境还没建", str(cm.exception))


class TestProbeDoesNotImport(unittest.TestCase):
    """判定按文件看，不 import：探活会被反复调用，import torch 要好几秒。"""

    def _service_src(self):
        with open(os.path.join(_ROOT, "tts_service", "setup_env.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_missing_packages_only_stats_the_filesystem(self):
        body = self._service_src().split("def missing_packages")[1].split("\ndef ")[0]
        for line in body.splitlines():
            s = line.strip()
            self.assertFalse(s.startswith(("import ", "from ")),
                             "判据里不能真去 import，那是几秒级的开销：%s" % s)
        self.assertIn("os.path.exists", body)

    def test_package_list_covers_what_the_service_imports(self):
        for need in ("torch", "transformers", "onnxruntime",
                     "faster_qwen3_tts", "qwen_tts"):
            self.assertIn(need, tts_engine.tts_setup.PACKAGES)

    def test_tts_engine_keeps_no_second_copy_of_the_probe(self):
        """判据只有 setup_env 一份。tts_engine 自己再数一遍目录，两份就会走散。"""
        with open(os.path.join(_ROOT, "podcast_maker", "tts_engine.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("def _missing_packages", src)
        self.assertNotIn("def _site_packages", src)
        self.assertNotIn("def _model_dir", src)


class TestInstallHasOneImplementation(unittest.TestCase):
    """装环境这件事只有一份实现，两个入口都调它。

    两份实现的走散方式很具体：换了源、动了版本、加了包，总有一边忘。
    """

    def _read(self, *parts):
        with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_setup_bat_defers_to_setup_env(self):
        text = self._read("tts_service", "setup.bat")
        self.assertIn("setup_env.py", text)
        self.assertNotIn("-m pip", text,
                         "批处理里再抄一份 pip 命令，就又是两份实现")

    def test_provision_runs_that_same_file(self):
        with open(os.path.join(_ROOT, "podcast_maker", "tts_engine.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def provision(")[1].split("\ndef ")[0]
        self.assertIn("setup_env.py", body,
                      "配置页的「搭建」必须跑同一个脚本，不能自己另写一套")

    def test_setup_env_builds_the_env_with_venv(self):
        src = self._read("tts_service", "setup_env.py")
        self.assertIn('"-m", "venv"', src)

    def test_the_service_owns_its_env_and_the_main_program_does_not(self):
        """本地服务跑在自己的 .venv 里；主程序用机器上的 Python。"""
        src = self._read("podcast_maker", "tts_engine.py")
        self.assertIn('".venv"', src)
        self.assertNotIn("runtime", src,
                         "主程序这一侧不该再出现包内运行时的痕迹")
        for rel in (("setup.bat",), ("_run.bat",)):
            text = self._read(*rel)
            self.assertNotIn("runtime", text, "/".join(rel))
            self.assertIn("%PY%", text,
                          "主程序要在机器上找 Python（3.11+），不是带一份")


class TestScriptPins(unittest.TestCase):
    """钉住脚本里不能退回去的几件事。

    1. `transformers` 的版本上限：5.17.0 起加载不了权重，见 requirements 里的说明；
    2. 环境由 `python -m venv` 现建，版本跟机器上那个 Python 走 —— 3.11 优先，
       因为周边那串包在新版 Python 上常常还没有轮子；
    3. 验收脚本收服务输出走落盘，不接管道：接管道会在服务抛异常时死锁。
    """

    def _read(self, *parts):
        with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_ceiling_locks_out_the_broken_release(self):
        text = self._read("tts_service", "requirements-tts.txt")
        self.assertIn("transformers==5.15.1", text)
        self.assertIn("5.17", text, "上界挡的是哪个版本，要写在文件里，"
                                    "不然下一个人会把上限松回去")

    def test_python_311_is_probed_first(self):
        src = self._read("tts_service", "setup_env.py")
        body = src.split("_BASE_CANDIDATES = (")[1].split(")")[0]
        self.assertLess(body.index("3.11"), body.index("3.14"),
                        "3.11 优先：轮子覆盖面最广，高版本常常还没轮子")

    def test_check_script_writes_the_service_log_to_a_file(self):
        text = self._read("tts_service", "check.py")
        block = text.split("def start_server")[1].split("\ndef ")[0]
        self.assertNotIn("subprocess.PIPE", block,
                         "接管道会在服务抛异常时死锁：缓冲区只有几 KB，写满就互等")
        self.assertIn("stdout=fh", block)


def _serve_module():
    """把 tts_service/serve.py 当模块加载。

    serve.py 顶层只 import 标准库（torch 是函数内延迟导入），所以主程序的 Python
    也能安全加载它 —— 测这些旋钮不必进那个 4.8 GB 的环境。
    """
    path = os.path.join(_ROOT, "tts_service", "serve.py")
    spec = importlib.util.spec_from_file_location("tts_serve_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVoiceControls(unittest.TestCase):
    """听感的三个旋钮：情绪白名单、随机种子、采样温度。

    这三样出了问题在日志里看不出来，所以每条都钉在原地，免得被改回去。
    """

    @classmethod
    def setUpClass(cls):
        cls.serve = _serve_module()
        from podcast_maker.config_manager import (DISCOURSE_VOCAB, EMOTION_TAGS,
                                                  EMOTION_VOCAB)
        cls.real = EMOTION_VOCAB
        cls.disc = DISCOURSE_VOCAB
        cls.tags = EMOTION_TAGS

    def test_the_two_vocabularies_split_the_tags(self):
        """真情绪与语篇功能不重不漏地覆盖脚本标签 —— 漏一个，LLM 就选不出来。"""
        self.assertEqual(sorted(set(self.real) | set(self.disc)),
                         sorted(set(self.tags)))
        self.assertEqual(set(self.real) & set(self.disc), set())

    def test_service_whitelist_matches_the_main_program(self):
        """服务端的情绪白名单与主程序的 EMOTION_VOCAB 同源，两处不许走散。"""
        self.assertEqual(tuple(self.serve.INSTRUCT_EMOTIONS), tuple(self.real))

    def test_discourse_tags_never_become_an_instruction(self):
        """语篇功能不是语气。拼成「用过渡的语气说」是噪音，还会盖住真情绪。"""
        for tag in self.disc:
            self.assertIsNone(self.serve.build_instruct(tag, 1.0),
                              "「%s」是语篇功能，不该生成语气指令" % tag)

    def test_real_emotions_do_become_an_instruction(self):
        for tag in self.real:
            self.assertEqual(self.serve.build_instruct(tag, 1.0),
                             "用%s的语气说" % tag)

    def test_light_degree_softens_the_instruction(self):
        """「略」这一档要落在措辞上：同一个情绪，加档后必须带「略带」。

        「明显/非常」不进产品（实测方向会反转），所以只认这两档——
        传了别的档位名等于没传，不许静默当成「略」。`none` 不在此列：它是
        「不贴语气指令」那一档，另有专门用例盯着（见下一条）。
        """
        for tag in self.real:
            self.assertEqual(self.serve.build_instruct(tag, 1.0, "light"),
                             "用略带%s的语气说" % tag)
            for other in ("mid", "heavy", "", None):
                self.assertEqual(self.serve.build_instruct(tag, 1.0, other),
                                 "用%s的语气说" % tag,
                                 "档位 %r 不是「略」，措辞不该变" % (other,))

    def test_none_degree_suppresses_the_instruction(self):
        """不写心情的文体：档位必须在合成这一跳把情绪指令拦掉。

        上游把「平静」当「不加修饰」的代码字在用（`emotion` 是必填，词表里没有
        表示「无」的合法值），所以这一档的稿子里照样会出现真情绪词。档位不拦，
        就会发出「用平静的语气说」——正与上游 `paradigms` 写的「不贴语气指令」
        相反。语篇标签本来就不转，这里一并覆盖。
        """
        for tag in self.real:
            self.assertIsNone(self.serve.build_instruct(tag, 1.0, "none"),
                              "「%s」在 none 档不该生成语气指令" % tag)
        for tag in self.disc:
            self.assertIsNone(self.serve.build_instruct(tag, 1.0, "none"))
        self.assertIsNone(self.serve.build_instruct(None, 1.0, "none"))
        # 档位只关情绪那一段：语速那一句不归它管，别顺手一起吞掉。
        self.assertEqual(self.serve.build_instruct("平静", 0.8, "none"),
                         "语速放慢一些")
        # 别的档位不受影响——改的是 none 的语义，不是把整条路关掉。
        self.assertEqual(self.serve.build_instruct("感慨", 1.0, "light"),
                         "用略带感慨的语气说")
        self.assertEqual(self.serve.build_instruct("感慨", 1.0),
                         "用感慨的语气说")

    def test_seed_is_derived_from_text_and_speaker(self):
        """同一句同一个音色 → 同一种子；换个字或换个音色 → 换个种子。"""
        a = self.serve.seed_for("大家好", "Vivian")
        self.assertEqual(a, self.serve.seed_for("大家好", "Vivian"))
        self.assertNotEqual(a, self.serve.seed_for("大家好。", "Vivian"))
        self.assertNotEqual(a, self.serve.seed_for("大家好", "Serena"))
        self.assertTrue(0 <= a < 2 ** 32)

    def test_sampling_is_pinned_explicitly(self):
        """采样参数必须显式写出来：它们是音色稳不稳的旋钮，藏在默认值里改不动。"""
        for key in ("temperature", "top_k", "top_p", "do_sample",
                    "repetition_penalty"):
            self.assertIn(key, self.serve.SAMPLE_KWARGS)
        self.assertLess(self.serve.SAMPLE_KWARGS["temperature"], 0.6,
                        "库默认 0.9 是「每句重掷骰子」的档位，不能再退回去")

    def test_synthesize_never_passes_emotion_through(self):
        """v0.27.0 硬隔离：脚本行里的语篇标签**不许**进合成链。

        2a 期实证 emotion 标签是生成侧的句型触发器（追问→99% 问句），但合成侧
        消费它的 instruct 路径是 v0.9.0 判死的东西（Base+instruct 实测更差）。
        脚本里带不带标签，合成行为必须一模一样——synth_line 连收都不收 emotion。

        音色档案（ref）与档位（degree）仍走透传：断在哪一句，那一句就换了个人；
        所以这里连它们一起断言。
        """
        cfg = {"tts.engine": "qwen3tts",
               "tts.qwen3tts_voice_a": "Vivian", "tts.qwen3tts_voice_b": "Serena",
               "tts.speed_a": 1.0, "tts.speed_b": 1.0,
               "tts.unload_llm_before_synth": False, "tts.max_retries": 1,
               "audio.sample_rate": 24000}
        script = [{"speaker": "A", "text": "第一句", "emotion": "追问"},
                  {"speaker": "B", "text": "第二句", "emotion": "过渡"}]
        seen = []

        def fake_line(text, voice, speed, cfg, degree=None, ref=None):
            seen.append((voice, degree, (ref or {}).get("wav")))
            return b"wav"

        # 音色档案那一步会起子进程加载模型，测试里必须替掉：这里验的是「透传」，
        # 不是「录音」。替成两份假档案，顺带就能验 A/B 不会互相串。
        fake_refs = {"A": {"wav": "/x/A/ref.wav", "text": "甲"},
                     "B": {"wav": "/x/B/ref.wav", "text": "乙"}}
        with tempfile.TemporaryDirectory() as tmp,                 mock.patch.object(tts_engine, "synth_line", side_effect=fake_line),                 mock.patch.object(tts_engine, "ensure_voice_profiles",
                                  return_value=fake_refs),                 mock.patch.object(tts_engine, "probe_duration", return_value=1.0):
            tts_engine.synthesize(script, tmp, cfg, emotion_level="light")
        # 档位透传不变；emotion 被硬隔离——synth_line 根本没有这个参数。
        self.assertEqual(seen,
                         [("Vivian", "light", "/x/A/ref.wav"),
                          ("Serena", "light", "/x/B/ref.wav")])

    # ---- 角色音色档案 ----------------------------------------------------
    # 本地引擎走 Base 变体：音色不在模型里，而在项目自己那份参考音频里。这一段
    # 钉住「那份文件怎么被找到、怎么被传下去、什么时候不算数」。

    def _serve_src(self):
        with open(os.path.join(_ROOT, "tts_service", "serve.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def _engine_src(self):
        with open(os.path.join(_ROOT, "podcast_maker", "tts_engine.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def _make_profile(self, root, role, text="参考句", size=4096):
        d = layout.voice_role_dir(root, role)
        os.makedirs(d, exist_ok=True)
        with open(layout.voice_ref_file(root, role), "wb") as f:
            f.write(b"\0" * size)
        with open(layout.voice_text_file(root, role), "w",
                  encoding="utf-8") as f:
            f.write(text)

    def test_reference_goes_into_the_request_body(self):
        """音色是一份参考音频而不是一个名字，必须整份传下去。

        只传名字的话，服务端拿不到克隆要的波形，Base 变体会直接拒绝这个请求；
        而转录文本也要一起给 —— ICL 拿它当参考音频念的那句话。
        """
        body = self._engine_src().split("def _service_synth")[1].split("\ndef ")[0]
        self.assertIn('body["ref_audio"] = ref["wav"]', body)
        self.assertIn('body["ref_text"] = ref["text"]', body)

    def test_profiles_are_keyed_by_role(self):
        """A 角与 B 角各取各的档案。张冠李戴的后果是整期两个人同一个嗓子。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._make_profile(tmp, "A", "甲")
            self._make_profile(tmp, "B", "乙")
            got = tts_engine.voice_profiles(tmp)
        self.assertEqual(sorted(got), ["A", "B"])
        self.assertEqual(got["A"]["text"], "甲")
        self.assertEqual(got["B"]["text"], "乙")

    def test_half_a_profile_is_no_profile(self):
        """只有波形没有转录的档案不算数。

        缺转录就等于让模型听了一段不知道在念什么的示例，参考内容会串进结果。
        宁可当它没有、重新录一份。
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = layout.voice_role_dir(tmp, "A")
            os.makedirs(d, exist_ok=True)
            with open(layout.voice_ref_file(tmp, "A"), "wb") as f:
                f.write(b"\0" * 4096)
            self.assertEqual(tts_engine.voice_profiles(tmp), {})

    def test_tiny_audio_is_not_a_profile(self):
        """几百字节的残片不算档案：那是写了一半的文件，当音色源头会出怪声。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._make_profile(tmp, "A", size=100)
            self.assertEqual(tts_engine.voice_profiles(tmp), {})

    def test_voice_file_names_match_the_tool(self):
        """档案的三个文件名在两处各写一份，必须一致。

        make_voice.py 跑在服务那个独立环境里，不引入主程序的 layout，所以只能各
        写一份 —— 那就由这条测试锁住（同 INSTRUCT_EMOTIONS 与 EMOTION_VOCAB 的
        处置：同源、逐条比对）。
        """
        with open(os.path.join(_ROOT, "tts_service", "make_voice.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('WAV_NAME = "%s"' % layout.VOICE_WAV, src)
        self.assertIn('TXT_NAME = "%s"' % layout.VOICE_TXT, src)
        self.assertIn('PROFILE_NAME = "%s"' % layout.VOICE_PROFILE, src)

    def test_the_voice_dir_name_has_one_source(self):
        """音色目录名只有一处真源（layout.DIR_VOICE），工具那侧靠参数收。"""
        with open(os.path.join(_ROOT, "tts_service", "make_voice.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"--voice-dir"', src)
        self.assertIn('default="音色"', src)

    def test_the_voice_tool_defaults_match_the_factory_defaults(self):
        """命令行直接跑与主程序自动跑，必须录出同一套音色。

        `make_voice.py` 跑在服务那个独立环境里，不 import 主程序，所以它那份
        `DEFAULT_VOICES` 是**另抄的一份**出厂默认值。抄歪的后果是：同一台机器上
        两个入口录出两套音色，而档案里只看得到内置音色名、看不出是哪个入口来的
        —— 没有任何线索能解释「为什么音色不一样」。所以这里逐条比对。
        """
        from podcast_maker.config_manager import PARAM_SPEC
        with open(os.path.join(_ROOT, "tts_service", "make_voice.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        line = [ln for ln in src.splitlines()
                if ln.startswith("DEFAULT_VOICES")][0]
        for role in ("A", "B"):
            factory = PARAM_SPEC["tts.qwen3tts_voice_%s" % role.lower()]["default"]
            self.assertIn('"%s": "%s"' % (role, factory), line,
                          "%s 角的工具默认值必须与出厂默认一致（出厂是 %s）"
                          % (role, factory))

    def test_the_reference_records_which_device_made_it(self):
        """档案必须记下录制设备。

        实测踩到的坑：同一套音色名 + 同一句参考文案，GPU 与 CPU 出的是**两条
        不同波形**（GPU 得 sha256[:16] 713bab2d382ab0fd / F0 228.6Hz，CPU 得
        另一条且 F0 偏走）。而 `pick_device()` 在空闲显存不足时是**静默**退
        CPU 的 —— 上一期已把服务拉起来占住显存，新项目录档案就会悄悄落到 CPU
        上，音色与别的项目不再对齐，事后却查不出原因。所以 profile.json 里
        要留这一栏，生成时写、查账时读。
        """
        with open(os.path.join(_ROOT, "tts_service", "make_voice.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        build = src.split("def build(")[1].split("\ndef ")[0]
        self.assertIn('"device": str(eng.device', build)
        self.assertIn('"backend": eng.backend', build)
        st = src.split("def status(")[1].split("\ndef ")[0]
        self.assertIn('rec.get("device"', st)
        self.assertIn('rec.get("backend"', st)

    def test_the_voice_tool_shouts_when_it_falls_back_to_cpu(self):
        """退 CPU 录参考音频必须吼一声，不能只当普通日志。

        这条路慢十几倍，且音色基准与 GPU 路径不再是同一条 —— 跟「显存不够就
        凑合跑完」是两回事。静默降级正是这条测试要拦下的东西。
        """
        with open(os.path.join(_ROOT, "tts_service", "make_voice.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        build = src.split("def build(")[1].split("\ndef ")[0]
        self.assertIn('startswith("cpu")', build)
        self.assertIn("不是", build)

    def test_a_cpu_recorded_reference_is_called_out_in_the_main_log(self):
        """主程序日志里也要点名 —— 工具那句走自己的 stderr，界面看不到。

        界面上只有主程序这一条日志通道。工具的告警写进 stderr，主程序只读
        stdout，于是「音色为什么跟上一期不一样」在用户那边完全没有线索。
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._make_profile(tmp, "A")
            prof = layout.voice_profile_file(tmp, "A")
            with open(prof, "w", encoding="utf-8") as f:
                json.dump({"device": "cpu", "backend": "qwen-tts（官方，慢）"}, f)
            seen = []
            tts_engine._warn_if_recorded_on_cpu(tmp, "A", seen.append)
            self.assertEqual(len(seen), 1, "CPU 录的档案必须在主日志里点名")
            self.assertIn("CPU", seen[0])

            with open(prof, "w", encoding="utf-8") as f:
                json.dump({"device": "cuda", "backend": "faster-qwen3-tts"}, f)
            seen = []
            tts_engine._warn_if_recorded_on_cpu(tmp, "A", seen.append)
        self.assertEqual(seen, [], "GPU 录的不该报")

    def test_a_profile_without_a_recorded_device_stays_quiet(self):
        """旧档案没有 device 字段时不报错、不刷屏 —— 那是兼容，不是故障。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._make_profile(tmp, "A")
            seen = []
            tts_engine._warn_if_recorded_on_cpu(tmp, "A", seen.append)
        self.assertEqual(seen, [])
        self.assertEqual(layout.DIR_VOICE, "音色",
                         "改目录名要同时改这里的默认值，否则直接跑工具会写去别处")

    def test_base_model_takes_the_clone_branch(self):
        """有参考音频就走克隆、没有才走内置音色 —— 显式分流，不靠挨个试。

        以前是遍历几个方法去猜、只兜 TypeError；Base 抛的是 ValueError，兜不住，
        于是「换个模型就跑不起来」，而报错完全指不到病根。
        """
        body = self._serve_src().split("def synth_one")[1].split("\ndef ")[0]
        self.assertIn("if ref_audio:", body)
        self.assertIn("_gen_clone", body)

    def test_seed_comes_from_the_reference_content_not_its_path(self):
        """种子的音色标识必须用音频内容指纹。

        用路径的话，同一份参考音频复制到新项目就换了种子，波形跟着全变 ——
        而「换个项目音色照旧」正是这套档案要保证的事。
        """
        src = self._serve_src()
        fn = src.split("def voice_key_for")[1].split("\ndef ")[0]
        self.assertIn("_digest_of", fn, "指纹要按文件内容算")

    def test_clone_uses_icl_not_the_vector_only_mode(self):
        """克隆固定走 ICL（xvec_only=False）。

        只喂向量的那一档官方标为实验性，且句间音色漂移更大（实测相对极差
        27.4% vs ICL 的 13.1%）。方向改回去等于把音色一致性让掉。
        """
        fn = self._serve_src().split("def _gen_clone")[1].split("\ndef ")[0]
        self.assertIn('"xvec_only": False', fn)

    def test_missing_ref_text_is_rejected_at_the_door(self):
        """走克隆却没给转录文本要当场拒绝，不能合成出一段串了内容的音频。"""
        body = self._serve_src().split("def do_POST")[1]
        self.assertIn("ref_text", body)
        self.assertIn("400", body)

    def test_builtin_voices_are_offered_for_recording(self):
        """Base 变体下 /speakers 的「现在能用的」如实报空，但要一并给出内置音色名。

        界面靠后者录参考音频 —— 只报空，那一栏就没得选；把内置音色填进
        `speakers` 又会让人以为选了它就直接换音色。两件事分开报。
        """
        body = self._serve_src().split('path == "/speakers"')[1].split("def do_POST")[0]
        self.assertIn("builtin", body)
        self.assertIn("need_reference", body)

    def test_seed_input_is_the_voice_key_not_the_speaker_name(self):
        """请求侧的种子输入在有档案时必须是内容指纹。

        写成 `seed_for(text, speaker)` 会退回「按音色名派生」——同一份参考音频在
        两个项目里种子不同，复制档案过去也复现不出同一条波形。
        """
        body = self._serve_src().split("def do_POST")[1]
        self.assertIn("voice_key = voice_key_for(ref_audio)", body)
        self.assertIn("seed_for(text, voice_key)", body)

    def test_voice_root_is_separate_from_the_work_dir(self):
        """音色档案落在项目根，不跟着逐句语音的过程目录走。

        过程件（过程/第 N 期/audio）是清理时删掉不心疼的东西，而音色档案是项目
        资产 —— 下一期要用同一份。混进过程目录，清理一次就把音色弄丢，或者每期
        各录一份、期与期之间悄悄换了嗓子。
        """
        body = self._engine_src().split("def synthesize")[1].split("\ndef ")[0]
        self.assertIn("voice_root", body)
        self.assertIn("ensure_voice_profiles(voice_root or out_dir", body)

    def test_pipeline_passes_the_project_root(self):
        with open(os.path.join(_ROOT, "podcast_maker", "pipeline.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        call = src.split("tts_engine.synthesize")[1].split(")")[0]
        self.assertIn("voice_root=root", call,
                      "不传项目根，音色档案会落进「过程」里跟着被清掉")

    def test_degree_goes_into_the_request_body(self):
        with open(os.path.join(_ROOT, "podcast_maker", "tts_engine.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def _service_synth")[1].split("\ndef ")[0]
        self.assertIn('body["degree"] = degree', body)

    def test_emotion_never_enters_the_request_body(self):
        """v0.27.0 硬隔离：请求体里永远没有 emotion 键，instruct 判据恒为假。"""
        with open(os.path.join(_ROOT, "podcast_maker", "tts_engine.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src.split("def _service_synth")[1].split(chr(10) + "def ")[0]
        code = body.split('"""', 2)[2]
        self.assertNotIn("emotion", code,
                         "_service_synth 函数体里不得出现 emotion（文档字符串除外）")
        self.assertIn('body["degree"] = degree', body)

    def test_log_line_carries_the_seed(self):
        """日志不带种子，用户说「那句不好听」就永远查不动。"""
        with open(os.path.join(_ROOT, "tts_service", "serve.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("seed {seed}", src)


class TestDegenerationGuard(unittest.TestCase):
    """防退化：模型偶尔会把一句话念到生成上限，出来几十秒的噪声。

    这一档调参消不掉（温度 0.2 实测 4/20 句失控），只能分三步兜住：
    按字数给生成上限 → 顶到上限就判为失控 → 换一条随机路径再试 → 三试都炸就报错。
    """

    @classmethod
    def setUpClass(cls):
        cls.serve = _serve_module()

    def _serve_src(self):
        with open(os.path.join(_ROOT, "tts_service", "serve.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_frame_budget_grows_with_text_and_stays_bounded(self):
        short = self.serve.max_frames_for("短句")
        long_ = self.serve.max_frames_for("长" * 200)
        self.assertGreater(long_, short)
        self.assertGreaterEqual(short, self.serve.FRAME_FLOOR)
        self.assertLessEqual(long_, self.serve.MAX_FRAMES)

    def test_a_normal_line_is_not_flagged(self):
        text = "今天我们来聊一个有点意思的话题。"
        self.assertFalse(self.serve.looks_degenerate(4.0, text))

    def test_hitting_the_ceiling_is_flagged(self):
        text = "今天我们来聊一个有点意思的话题。"
        ceiling = self.serve.max_frames_for(text) / 12.0
        self.assertTrue(self.serve.looks_degenerate(ceiling, text))
        self.assertTrue(self.serve.looks_degenerate(ceiling * 1.5, text))

    def test_the_ceiling_is_passed_to_the_model(self):
        body = self._serve_src().split("def synth_one")[1].split("\ndef ")[0]
        self.assertIn("max_new_tokens", body,
                      "上限算了却不传给模型，等于没算")

    def test_degenerate_output_is_retried_not_returned(self):
        body = self._serve_src().split("def do_POST")[1]
        self.assertIn("looks_degenerate", body)
        self.assertIn("去掉语气", body, "三试的退路要写出来")
        self.assertIn("raise RuntimeError", body,
                      "三试都炸要报错，不能静默返回一段噪声")

    def test_temperature_is_out_of_the_degenerate_band(self):
        self.assertGreaterEqual(self.serve.TEMPERATURE, 0.3,
                                "0.2 实测 20 个种子里 4 句失控，是禁用档")


if __name__ == "__main__":
    unittest.main()
