# -*- coding: utf-8 -*-
"""BGM 模块（全局音乐库）的钉子。

覆盖四块：
  1. 真行为：music_gen CLI 的草稿保存 / 重名拒绝 / 幂等取消 / 删除守卫 /
     生成入参 fail-closed（子进程真跑，临时音乐库，不碰模型）。
  2. 常量钉：web_ui 与 music_gen 两边的草稿名 / 库名等值 —— 谁改一边不改
     另一边测试红。
  3. 端点钉：web_ui 六条 POST 路由与 GET 音频分支。
  4. 界面钉与实时刷新钉：BGM 卡的按钮都在页面里；每个长活的成功回调必须
     当场重渲染对应区块（回调即刷新，不依赖手动刷网页）—— 这是本模块
     立卡的硬要求，靠源码钉住，谁删了刷新调用谁红。
"""

import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MUSIC = os.path.join(_ROOT, "music_service")
sys.path.insert(0, _MUSIC)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import contextlib  # noqa: E402

import bgm_loudness  # noqa: E402  (ROOT/tools)
import music_gen  # noqa: E402
import podcast_maker.web_ui as web_ui  # noqa: E402


def have_ffmpeg():
    try:
        return bool(bgm_loudness.ffmpeg_bin())
    except Exception:
        return False


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _cli(*extra):
    """真跑一次 music_gen CLI，返回解析后的 JSON。stdout 的合同是干净的
    一份 JSON —— 多一行或少一行都算失败（解析取最后一个 JSON 对象会掩盖
    stdout 污染，所以这里硬按「stdout 只有一份 JSON」判）。"""
    p = subprocess.run([sys.executable, os.path.join(_MUSIC, "music_gen.py"),
                        *extra],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    lines = (p.stdout or "").strip()
    try:
        data = json.loads(lines)
    except ValueError:
        raise AssertionError("stdout 不是干净的一份 JSON：%r" % lines[:200])
    return p.returncode, data


def _write_draft(lib, seconds=30, seed=7):
    wav = os.path.join(lib, "_draft.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(struct.pack("<h", 8000) * 48000)
    with io.open(os.path.join(lib, "_draft.json"), "w",
                 encoding="utf-8", newline="\n") as fh:
        json.dump({"role": "_draft", "prompt": "测试曲", "seconds": seconds,
                   "seed": seed}, fh, ensure_ascii=False)


def _write_tone_wav(path, seconds=3.0, rate=48000):
    """写一段非静音的短 wav —— 冒充官方生成的产物（静音会被拒收）。

    最短 3 秒不是随便定的：cmd_generate 收尾要做响度归一（fail-closed），
    ffmpeg loudnorm 按 EBU R128 的 400ms 窗测 integrated loudness，比一个
    窗还短的音频测不出 input_i（实测 0.2 秒返回 None）—— 产物会被归一
    环节拒收，测试就全体连坐。真实产物是 10~30 秒，固件只须满足同一合同。
    """
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(rate * seconds))))


class _FakeHandler:
    """假 AceStepHandler / LLMHandler：按官方合同返回 (ok, msg)。"""

    def initialize_service(self, **_kw):
        return True, "ok"

    def initialize(self, **_kw):
        return True, "ok"


class _FakeParams:
    """假 GenerationParams / GenerationConfig：任何关键字都收下。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestLibraryOps(unittest.TestCase):
    """草稿 → 入库 → 管理 的真行为（临时音乐库，不起任何模型）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mu_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib = self.tmp
        self.flag = ["--lib-dir", self.lib]

    def test_status_shape_on_empty_lib(self):
        rc, data = _cli("status", "x") if False else _cli("--status", *self.flag)
        self.assertEqual(rc, 0)
        for key in ("ok", "lib", "ready", "env", "draft", "library"):
            self.assertIn(key, data)
        self.assertFalse(data["draft"]["exists"])
        self.assertEqual(data["library"], [])

    def test_save_without_draft_fails(self):
        rc, data = _cli("--save", "轻快钢琴", *self.flag)
        self.assertNotEqual(rc, 0)
        self.assertIn("还没有草稿", data["error"])

    def test_save_renames_and_rewrites_role(self):
        _write_draft(self.lib)
        rc, data = _cli("--save", "轻快钢琴", *self.flag)
        self.assertEqual(rc, 0)
        self.assertEqual(data["name"], "轻快钢琴")
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "轻快钢琴.wav")))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "_draft.wav")))
        with io.open(os.path.join(self.lib, "轻快钢琴.json"),
                     encoding="utf-8") as fh:
            prof = json.load(fh)
        self.assertEqual(prof.get("role"), "轻快钢琴")
        self.assertEqual(prof.get("seconds"), 30)

    def test_save_duplicate_entry_rejected(self):
        _write_draft(self.lib)
        self.assertEqual(_cli("--save", "轻快钢琴", *self.flag)[0], 0)
        _write_draft(self.lib)
        rc, data = _cli("--save", "轻快钢琴", *self.flag)
        self.assertNotEqual(rc, 0)
        self.assertIn("已存在", data["error"])

    def test_save_invalid_names_rejected(self):
        for bad in ("", "   ", "_draft", "a/b", "a\\b", "x" * 25):
            _write_draft(self.lib)
            rc, data = _cli("--save", bad, *self.flag)
            self.assertNotEqual(rc, 0, msg=repr(bad))
            _cli("--discard", *self.flag)

    def test_discard_idempotent(self):
        self.assertEqual(_cli("--discard", *self.flag)[0], 0)
        _write_draft(self.lib)
        self.assertEqual(_cli("--discard", *self.flag)[0], 0)
        self.assertEqual(_cli("--discard", *self.flag)[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.lib, "_draft.wav")))

    def test_delete_guards(self):
        # 草稿保留名不走删除
        _write_draft(self.lib)
        rc, data = _cli("--delete", "_draft", *self.flag)
        self.assertNotEqual(rc, 0)
        self.assertIn("草稿", data["error"])
        # 不存在的名字如实报错
        rc, data = _cli("--delete", "不存在的", *self.flag)
        self.assertNotEqual(rc, 0)
        self.assertIn("不存在", data["error"])
        # 正常删除
        self.assertEqual(_cli("--save", "轻快钢琴", *self.flag)[0], 0)
        self.assertEqual(_cli("--delete", "轻快钢琴", *self.flag)[0], 0)
        self.assertEqual(_cli("--status", *self.flag)[1]["library"], [])

    def test_status_lists_library(self):
        _write_draft(self.lib)
        self.assertEqual(_cli("--save", "轻快钢琴", *self.flag)[0], 0)
        lib = _cli("--status", *self.flag)[1]["library"]
        self.assertEqual([e["name"] for e in lib], ["轻快钢琴"])
        self.assertEqual(lib[0]["seconds"], 30)


class TestGenerateFailClosed(unittest.TestCase):
    """生成的入参与环境 fail-closed：不装环境、不递描述，一律当场拒绝。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mu_gen_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.flag = ["--lib-dir", self.tmp]

    def test_empty_prompt_rejected(self):
        rc, data = _cli("--generate", "--prompt", "  ", *self.flag)
        self.assertNotEqual(rc, 0)
        self.assertIn("描述不能空", data["error"])

    def test_seconds_out_of_range_rejected(self):
        for bad in (9, 31, -1):
            rc, data = _cli("--generate", "--prompt", "轻钢琴",
                            "--seconds", str(bad), *self.flag)
            self.assertNotEqual(rc, 0, msg=str(bad))
            self.assertIn("时长", data["error"])

    def test_env_missing_rejected_with_setup_hint(self):
        # 本机只要没把 music_service/.venv + 官方仓 + 权重全部凑齐，
        # 生成必须 fail-closed 并指路「搭建音乐环境」，绝不静默 fallback。
        rc, data = _cli("--generate", "--prompt", "轻钢琴",
                        "--seconds", "30", *self.flag)
        if music_gen.ready():
            self.skipTest("本机音乐环境已就绪，跳过缺环境分支")
        self.assertNotEqual(rc, 0)
        self.assertIn("搭建音乐环境", data["error"])

    def test_stdout_json_only_contract(self):
        # 无论成败，stdout 上只有一份 JSON；进度只许走 stderr。
        rc, _ = _cli("--generate", "--prompt", "  ", *self.flag)
        self.assertNotEqual(rc, 0)


class TestGenerateHandoffOrdering(unittest.TestCase):
    """生成收尾顺序钉：**产物先搬出临时目录，再清场**。

    事故：官方把 wav 写进 save_dir（`音乐库/.generating/`），而清理写在 finally
    里、搬运写在 finally 之后 —— 刚生成的文件被连目录一起删掉，随后的 isfile
    判假，报「音频产物没落盘」，模型加载 + 推理 + VAE 解码全白做。

    本测试用假 acestep 模块把 cmd_generate 全链真跑一遍（不加载任何权重）：
    假 generate_music 照官方的合同往 save_dir 写 wav、返回 success 载荷，
    真断言草稿落位、账本齐、临时目录已清。顺序一改回去，本测试立刻红。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mu_order_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib = os.path.join(self.tmp, "音乐库")
        self._saved_mods = {}
        for name in ("torch", "acestep", "acestep.handler",
                     "acestep.inference", "acestep.llm_inference"):
            self._saved_mods[name] = sys.modules.get(name)
        self._saved_ready = music_gen.ready
        music_gen.ready = lambda: True          # 环境判定与权重无关，这里只验顺序
        self._install_fake_acestep()

    def tearDown(self):
        music_gen.ready = self._saved_ready
        for name, mod in self._saved_mods.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def _install_fake_acestep(self):
        """假官方仓：只需四个名字，行为按官方合同来。"""
        written = {}

        def _fake_generate_music(dit, lm, params, config, save_dir=""):
            # 照官方那次真机实测来：往 stdout 裸 print 一行杂音
            print("Using precomputed LM hints")
            path = os.path.join(save_dir, "fake-uuid.wav")
            _write_tone_wav(path)
            written["path"] = path
            written["params"] = params
            written["lm"] = lm
            return types.SimpleNamespace(
                success=True, audios=[{"path": path,
                                       "params": {"seed": 424242}}])

        handler = types.ModuleType("acestep.handler")
        handler.AceStepHandler = _FakeHandler
        inference = types.ModuleType("acestep.inference")
        inference.GenerationConfig = _FakeParams
        inference.GenerationParams = _FakeParams
        inference.generate_music = _fake_generate_music
        llm = types.ModuleType("acestep.llm_inference")
        llm.LLMHandler = _FakeHandler
        pkg = types.ModuleType("acestep")
        pkg.handler, pkg.inference, pkg.llm_inference = handler, inference, llm
        for name, mod in (("torch", types.ModuleType("torch")),
                          ("acestep", pkg), ("acestep.handler", handler),
                          ("acestep.inference", inference),
                          ("acestep.llm_inference", llm)):
            sys.modules[name] = mod
        self.written = written

    def test_draft_survives_scratch_cleanup(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = music_gen.cmd_generate(self.lib, "低频弦乐", 30, -1, 0)
        # stdout 合同：干净的一份 JSON，第三方裸 print 全被赶去 stderr
        data = json.loads(out.getvalue().strip())
        self.assertIn("Using precomputed LM hints", err.getvalue())
        self.assertEqual(rc, 0, msg="成品搬出临时目录之前被清场删掉了")
        self.assertTrue(data.get("ok"), msg=str(data))
        # 假官方确实把文件写进了临时目录 —— 真删了才有得测
        self.assertTrue(self.written["path"].startswith(
            os.path.join(self.lib, ".generating")))
        # 草稿落位、账本齐、临时目录已清
        wav, js = music_gen.draft_paths(self.lib)
        self.assertTrue(os.path.isfile(wav), "草稿音频没落位")
        self.assertGreater(os.path.getsize(wav), 0)
        self.assertEqual(music_gen.read_json_file(js).get("seed"), 424242)
        self.assertFalse(os.path.isdir(os.path.join(self.lib, ".generating")),
                         "临时目录没清干净")

    def test_scratch_clean_failure_never_kills_product(self):
        """临时目录删不掉只许 WARN，不许把已成品的生成判死。"""
        wav, _js = music_gen.draft_paths(self.lib)
        os.makedirs(self.lib, exist_ok=True)
        _write_tone_wav(wav)                       # 假装成品已搬出
        scratch = os.path.join(self.lib, ".generating")
        os.makedirs(scratch, exist_ok=True)
        with mock.patch.object(music_gen.shutil, "rmtree",
                               side_effect=OSError("被占用")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                music_gen._clear_scratch(scratch)
        self.assertIn("[WARN]", err.getvalue())
        self.assertTrue(os.path.isfile(wav), "成品被误删")

    def test_prompt_raw_recorded_not_fed_to_model(self):
        """转译链溯源：推理吃转译后的 caption，原始中文只进账本；
        draft_state / library_entries 都要能读回来，不然账记了也查不了。"""
        raw = "以低音贝斯为主的偏低音乐曲，不要任何打击乐"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = music_gen.cmd_generate(self.lib, "dark ambient bass drone",
                                        30, -1, 0, prompt_raw=raw)
        self.assertEqual(rc, 0, msg=err.getvalue()[-200:])
        _wav, js = music_gen.draft_paths(self.lib)
        prof = music_gen.read_json_file(js)
        self.assertEqual(prof.get("prompt"), "dark ambient bass drone")
        self.assertEqual(prof.get("prompt_raw"), raw)
        self.assertEqual(music_gen.draft_state(self.lib).get("prompt_raw"), raw)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(music_gen.cmd_save(self.lib, "转译溯源"), 0)
        entry = [e for e in music_gen.library_entries(self.lib)
                 if e["name"] == "转译溯源"][0]
        self.assertEqual(entry["prompt_raw"], raw)
        self.assertEqual(entry["prompt"], "dark ambient bass drone")

    def test_thinking_codes_caption_kept(self):
        """thinking 谱曲定时长，caption 三连关保真：LM 只写 audio codes、
        不碰 caption。同 caption 同 seed 四组对照实测（2026-10-09）：全关
        14 秒断崖（DiT 裸写稀疏曲撑不满 30 秒），thinking + caption 保真
        28 秒铺满；caption 覆盖只由 use_cot_caption 控制（官方
        inference.py:826），thinking 开着也不碰转译稿。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = music_gen.cmd_generate(self.lib, "dark ambient bass drone",
                                        30, -1, 0)
        self.assertEqual(rc, 0, msg=err.getvalue()[-300:])
        params = self.written["params"]
        self.assertTrue(params.thinking)
        self.assertFalse(params.use_cot_caption)
        self.assertFalse(params.use_cot_language)
        self.assertFalse(params.use_cot_metas)
        self.assertIsNotNone(self.written["lm"],
                             msg="thinking 谱曲必须有 LM 参与")

    def test_draft_is_loudness_normalized(self):
        """草稿落位即归一：与内置 15 档同一标尺（-23 LUFS），规格统一 s16
        单声道（bgm_loudness 的增益施加只吃这个规格）。响度自由发挥的
        素材进库就是压场——实测 ACE-Step 产物比内置档响 6.5 dB。"""
        if not have_ffmpeg():
            self.skipTest("需要 ffmpeg 测量响度")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = music_gen.cmd_generate(self.lib, "dark ambient bass drone",
                                        30, -1, 0)
        self.assertEqual(rc, 0, msg=err.getvalue()[-300:])
        wav, js = music_gen.draft_paths(self.lib)
        with wave.open(wav) as wf:
            self.assertEqual(wf.getnchannels(), 1, msg="素材必须是单声道")
            self.assertEqual(wf.getsampwidth(), 2, msg="素材必须是 s16")
        import bgm_loudness
        lufs, _tp = bgm_loudness.measure(wav, bgm_loudness.ffmpeg_bin())
        self.assertIsNotNone(lufs)
        self.assertLessEqual(abs(lufs - (-23.0)), bgm_loudness.VERIFY_TOL_LU,
                             msg="草稿响度 %.2f 偏离标尺 -23" % lufs)
        prof = music_gen.read_json_file(js)
        self.assertIn("norm_gain_db", prof)
        self.assertIn("norm_limited", prof)
        self.assertIn("norm_after_lufs", prof)


class TestBgmCaption(unittest.TestCase):
    """web 侧转译层：中文描述 → ACE-Step 的三层输入（caption/lyrics/元数据）。

    钉四件事：预算必须走统一推动点 llm.max_tokens（4096 写死把思考型模型
    答案段挤空的教训）；转译成功吃三层结构；结构性字段（bpm 区间、拍号枚举、
    lyrics 换行）由代码层确定性校验，不靠模型自觉；LLM 不可用或答不出结构时
    WARN 降级按原文直出不拦生成（与环境音决策同一套降级口径）。
    """

    def setUp(self):
        self.cfg = {"llm.backend": "lm-studio", "llm.base_url": "http://x",
                    "llm.model": "m", "llm.max_tokens": 47872}

    def test_translated_and_budget_from_unified_config(self):
        captured = {}

        class _Fake:
            def chat(self, messages, temperature=0.8, max_tokens=8192, **kw):
                captured["max_tokens"] = max_tokens
                captured["prompt"] = messages[0]["content"]
                return ('{"caption": "dark ambient, deep bass drone", '
                        '"lyrics": "[Intro - ambient]\\n[Outro - fade out]", '
                        '"bpm": 76, "keyscale": "A minor", '
                        '"timesignature": "4"}'), {}

        with mock.patch.object(web_ui, "make_llm", return_value=_Fake()):
            res = web_ui.translate_bgm_prompt(self.cfg, "诡异的低音")
        self.assertFalse(res["degraded"])
        self.assertEqual(res["caption"], "dark ambient, deep bass drone")
        self.assertEqual(res["lyrics"], "[Intro - ambient]\n[Outro - fade out]")
        self.assertEqual(res["bpm"], 76)
        self.assertEqual(res["keyscale"], "A minor")
        self.assertEqual(res["timesignature"], "4")
        self.assertEqual(captured["max_tokens"], 47872,
                         msg="预算没走 llm.max_tokens 统一推动点")
        self.assertIn("诡异的低音", captured["prompt"])

    def test_llm_failure_degrades_to_raw(self):
        def _boom(cfg):
            class _F:
                def chat(self, *a, **k):
                    raise web_ui.LLMError("连不上后端")
            return _F()

        logs = []
        with mock.patch.object(web_ui, "make_llm", _boom):
            res = web_ui.translate_bgm_prompt(
                self.cfg, "低音贝斯", log=logs.append)
        self.assertTrue(res["degraded"])
        self.assertEqual(res["caption"], "低音贝斯")
        self.assertEqual(res["lyrics"], "[Instrumental]")
        self.assertTrue(any("转译失败" in m for m in logs), msg=logs)

    def test_empty_answer_degrades_too(self):
        class _F:
            def chat(self, *a, **k):
                return "   ", {}

        with mock.patch.object(web_ui, "make_llm", return_value=_F()):
            res = web_ui.translate_bgm_prompt(self.cfg, "低音贝斯")
        self.assertTrue(res["degraded"])
        self.assertEqual(res["caption"], "低音贝斯")

    def test_source_text_answer_degrades(self):
        """答的不是 JSON 三层结构就降级：不把半截解析结果喂进模型。"""
        class _F:
            def chat(self, *a, **k):
                return "dark ambient", {}

        logs = []
        with mock.patch.object(web_ui, "make_llm", return_value=_F()):
            res = web_ui.translate_bgm_prompt(self.cfg, "低音贝斯",
                                              log=logs.append)
        self.assertTrue(res["degraded"])
        self.assertEqual(res["caption"], "低音贝斯")
        self.assertTrue(any("三层结构" in m for m in logs), msg=logs)

    def test_structural_fields_validated_by_code(self):
        """结构性字段由代码层兜底：越界 bpm 置回自动、非法拍号清空、
        挤在一行的相邻结构标记拆行——不靠模型自觉。"""
        class _F:
            def chat(self, *a, **k):
                return ('{"caption": "warm ambient", '
                        '"lyrics": "[Intro - ambient] [Outro - fade out]", '
                        '"bpm": 999, "keyscale": "", "timesignature": "5"}'), {}

        with mock.patch.object(web_ui, "make_llm", return_value=_F()):
            res = web_ui.translate_bgm_prompt(self.cfg, "温暖的氛围")
        self.assertIsNone(res["bpm"], "越界 bpm 没被置回自动估计")
        self.assertEqual(res["timesignature"], "", "非法拍号没被清空")
        self.assertEqual(res["lyrics"], "[Intro - ambient]\n[Outro - fade out]",
                         "相邻结构标记没被拆成一行一个")


class TestConstantsPins(unittest.TestCase):
    """web_ui 与 music_gen 两边的常量必须同源 —— 有测试钉死，谁改一边
    不改另一边就红（与 _VOICE_DRAFT 的钉法同一纪律）。"""

    def test_draft_name_equal(self):
        self.assertEqual(web_ui._MUSIC_DRAFT, music_gen.DRAFT_NAME)

    def test_lib_dirname_equal(self):
        self.assertEqual(web_ui._MUSIC_LIB, music_gen.LIB_DIRNAME)

    def test_seconds_bounds(self):
        self.assertEqual(music_gen.SECONDS_MIN, 10)
        self.assertEqual(music_gen.SECONDS_MAX, 30)


class TestEndpointPins(unittest.TestCase):
    """六条 POST 路由注册 + GET 音频分支 + 搭建入口存在。"""

    def test_routes_registered(self):
        for route in ("/api/music/state", "/api/music/setup",
                      "/api/music/generate", "/api/music/draft/save",
                      "/api/music/draft/discard", "/api/music/delete"):
            self.assertIn(route, web_ui.ROUTES_POST, route)

    def test_audio_get_branch(self):
        src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))
        self.assertIn('/api/music/audio/', src)
        self.assertIn("_MUSIC_LIB", src)

    def test_setup_entry_exists(self):
        self.assertTrue(os.path.isfile(
            os.path.join(_MUSIC, "setup_music_env.py")))


class TestFrontendPins(unittest.TestCase):
    """界面钉：按钮在页面里；实时刷新回调在源码里钉住。"""

    @classmethod
    def setUpClass(cls):
        cls.page = web_ui.PAGE
        cls.src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))

    def test_card_mounted_in_bgm_section(self):
        self.assertIn("sec==='bgm'", self.src)
        self.assertIn("musicCfgHtml()", self.src)

    def test_probe_after_mount_not_inside_loop(self):
        """进配置页就自动探测 —— 且**必须在卡片挂进文档之后**探。

        事故：探测曾写在 forEach 的 bgm 分区块里，而在那里 card 还没
        appendChild 进文档，el('music-cfg') 取到 null，refreshMusicState
        第一句就静默早退（一次请求都不发），徽标永远停在「探测中…」——
        用户必须手点「重新探测」（那时卡已在文档里）才看得到状态。
        ffmpeg 与语音环境两处都在循环外、挂载之后，BGM 是三处里唯一的例外。
        这里把规矩钉死：bgm 块内只挂内容，探测与另两处同款放循环外。
        """
        start = self.src.index("if(sec==='bgm')")
        block = self.src[start:self.src.index("\n    }", start)]
        self.assertNotIn("refreshMusicState()", block,
                         "bgm 块内在卡片挂载前探测 = 静默早退，徽标不会归位")
        # 挂载之后的探测段：三个探测都在，BGM 不许再掉队
        tail_start = self.src.index("el('cfg-warn').innerHTML")
        tail = self.src[tail_start:self.src.index("drawSubtitlePreview();",
                                                  tail_start)]
        self.assertIn("refreshDepsState();", tail)
        self.assertIn("refreshTtsState();", tail)
        self.assertIn("refreshMusicState();", tail)

    def test_buttons_present(self):
        for needle in ("btn-music-setup", "setupMusic()", "btn-stop-m",
                       "mu-prompt", "mu-seconds", "mu-go",
                       "musicDraftPreview(this)", "musicDraftSave()",
                       "musicGenerate()", "musicDiscard()",
                       "muManageToggle", "muManageDelete(this)"):
            self.assertIn(needle, self.page, needle)

    def test_draft_audio_endpoint_used(self):
        self.assertIn("new Audio('/api/music/audio/_draft')", self.page)

    def test_realtime_refresh_pins(self):
        """实时刷新铁律：每个成功回调都必须当场重渲染对应区块。"""
        # 搭建完成 → refreshMusicState（徽标/生成区当场归位）
        self.assertRegex(self.page,
                         r"watchMusic[\s\S]{0,900}await refreshMusicState\(\)")
        # 生成成功 → 亮出草稿按钮行 + refreshMusicState
        self.assertRegex(self.page,
                         r"watchTask\(r\.task_id,\s*\n\s*j=>\{el\('mu-go'\)"
                         r"\.disabled=false;[\s\S]{0,400}refreshMusicState\(\);")
        # 保存成功 → 收起草稿按钮行 + 刷新管理清单
        self.assertIn("已入库", self.page)
        self.assertIn("muManageRender();", self.page)
        # 删除成功 → 重渲染管理清单
        self.assertRegex(self.page,
                         r"已删除[\s\S]{0,120}muManageRender\(\);")
        # 造嗓同享：成功回调按盘上真状态亮灯（不盲显示）
        self.assertIn("vdDraftCheck();", self.page)

    def test_delete_inline_confirm_no_alert(self):
        """删除走 inline 红字二次确认，禁 alert/confirm/prompt 弹框。"""
        self.assertIn("确认?", self.page)
        self.assertNotIn("alert(", self.page)
        self.assertNotIn("confirm(", self.page)

    def test_bgm_show_if_wiring(self):
        """来源相关行的显隐：前端必须有通用 show_if 求值，且在值变更与
        卡片重建两处都重算——漏一处就出现「切了来源、别的下拉还杵着」。"""
        self.assertIn("function applyShowIf", self.src)
        self.assertIn("function showIfMatch", self.src)
        self.assertGreaterEqual(self.src.count("applyShowIf()"), 3)

    def test_library_name_is_a_real_select(self):
        """曲目下拉必须由 control() 的 music-library 分支渲染成真 SELECT：
        fillMusicLibrarySelects 只认 tagName==='SELECT' 的登记节点，掉进
        文本框兜底分支就永远没人来填——来源切到 AI 音乐库也没得选。"""
        self.assertRegex(self.src,
                         r"options_source==='music-library'\)\{[\s\S]{0,400}<select")
        # 回填时存值为空必须显式说出「未选择」，不许亮着第一项装作已选。
        self.assertIn("（未选择——点开挑一条）", self.src)
        # 试听与混合必须与档位行同一套：原始素材走库曲目前端点，混合共用
        # bgmMixPreview（与成片同一个 mix_bgm）。
        self.assertRegex(self.src,
                         r"options_source==='music-library'\)\{[\s\S]{0,600}data-pv")
        self.assertRegex(self.src,
                         r"options_source==='music-library'\)\{[\s\S]{0,700}bgmMixPreview")
        # 库曲目解析唯一来源 assets_factory.library_bgm_path（fail-closed）：
        # GET 试听与 mix_preview 都得走它，后者不许静默拿内置档顶上。
        self.assertGreaterEqual(self.src.count("assets_factory.library_bgm_path"), 2)

    def test_empty_select_falls_back_to_config(self):
        """动态回填的空壳 SELECT（音乐库曲目、模型名）在重建那一刻 options 为空，
        .value 规范返回 ""；valOf 若把它当取值返回，cfgVal 会因为 "" 不是 undefined
        而不回退 CFG.values，把配置里存的选中项盖成空——切页重建后回填就显示
        「未选择」。valOf 必须在空壳时回 undefined，让 cfgVal 落到 CFG.values。"""
        self.assertRegex(
            self.src,
            r"function valOf\(key\)\{[\s\S]{0,900}"
            r"tagName==='SELECT'&&!n\.node\.options\.length\)\s*return undefined")


class TestSetupScriptPins(unittest.TestCase):
    """搭建脚本的关键决策钉死：多源拿代码、3.12 优先、官方下载器、
    独立 venv、GBK 防御。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(os.path.join(_MUSIC, "setup_music_env.py"))

    def test_repo_source(self):
        self.assertIn("github.com/ace-step/ACE-Step-1.5", self.src)
        self.assertIn("codeload.github.com", self.src)  # tarball 兜底

    def test_python_preference_312_first(self):
        self.assertLess(self.src.index('"py", "-3.12"'),
                        self.src.index('"py", "-3.11"'))

    def test_official_downloader_used(self):
        self.assertIn("acestep.model_downloader", self.src)
        self.assertIn(music_gen.LM_MODEL, self.src)

    def test_gbk_defense_frontloaded(self):
        self.assertIn('reconfigure(errors="replace")', self.src)

    def test_domestic_mirrors_first(self):
        # 事故钉：torch / PyPI 依赖必须国内镜像优先、官方兜底 ——
        # 官方源实测 0.65 MB/s，3.2GB torch 要下一小时（TTS 栈同一教训）。
        self.assertIn("mirror.sjtu.edu.cn/pytorch-wheels/cu128", self.src)
        self.assertIn("pypi.tuna.tsinghua.edu.cn", self.src)
        self.assertLess(self.src.index("mirror.sjtu.edu.cn/pytorch-wheels/cu128"),
                        self.src.index("download.pytorch.org/whl/cu128"))

    def test_flash_attn_mirror_chain(self):
        # 事故钉一：flash-attn 轮子只挂 GitHub release（PyPI/清华/上交大都没有），
        # GitHub 443 常断曾让整轮依赖安装失败——镜像链兜底必须存在（供显式要求时用）。
        self.assertIn("sdbds/flash-attention-for-windows", self.src)
        self.assertIn("ghproxy.net", self.src)      # 实测 174 KB/s 可用
        self.assertIn("gh-proxy.com", self.src)
        self.assertLess(self.src.index("FLASH_ATTN_URL"),
                        self.src.index("FLASH_ATTN_MIRRORS"))  # 直连先试，镜像兜底

    def test_flash_attn_optional_by_default(self):
        # 事故钉二：flash-attn 是锦上添花不是刚需（SDPA 自动回退，功能/音质
        # 零影响）——默认必须跳过，不为 250MB 慢通道折腾；缺席要打 [WARN]
        # 说明后果并滤行继续，不许整轮崩、不许静默；想要的人 --with-flash-attn。
        self.assertIn("with_flash=False", self.src)             # 默认参数
        self.assertIn("--with-flash-attn", self.src)            # 显式旗标
        self.assertIn("with_flash=args.with_flash_attn", self.src)  # 接线
        self.assertIn("[WARN] flash-attn", self.src)            # 降级必须喊出来
        self.assertIn("_requirements_without_flash", self.src)  # 滤行继续，非崩溃
        # 滤行必须先于 requirements 安装（pip 碰到 GitHub 直链就会整轮失败）。
        self.assertLess(self.src.index("if not _flash_installed(py):"),
                        self.src.index("pip\", \"install\", \"-r\", req"))

    def test_nano_vllm_no_deps(self):
        # 事故钉三：nano-vllm 的 pyproject 把 flash-attn 声明成硬依赖（GitHub
        # 直链），pip 装它必炸——但代码是 try/except 守卫导入 + SDPA 回退，
        # 缺席仅慢不残。必须 --no-deps 挂载（其余依赖 requirements 已覆盖）。
        self.assertIn('"--no-deps"', self.src)
        self.assertLess(self.src.index("nano-vllm"), self.src.index("--no-deps"))

    def test_rmtree_fail_closed(self):
        # 事故钉：rmtree 带 ignore_errors 会把 Windows 只读文件（.git 对象
        # 全是）的删除失败静默吞掉，让 clone/rename 在几百秒下载后才炸
        # （WinError 183）。整个脚本禁止静默删除调用，必须走 _rmtree：
        # 解只读 + 删完断言不存在。只查真实调用行，注释里的提及不算。
        calls = [ln for ln in self.src.splitlines()
                 if "rmtree(" in ln and "ignore_errors" in ln]
        self.assertEqual(calls, [])
        self.assertIn("def _rmtree", self.src)
        self.assertIn("os.rename(inner, REPO_DIR)", self.src)

    def test_check_mode(self):
        p = subprocess.run([sys.executable,
                            os.path.join(_MUSIC, "setup_music_env.py"),
                            "--check"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(p.returncode, 0)
        data = json.loads(p.stdout)
        for key in ("ok", "ready", "state"):
            self.assertIn(key, data)
        self.assertIn("checkpoints", data["state"])


class TestGenerationCallPins(unittest.TestCase):
    """生成调用钉住官方推荐档：turbo 8 步 + shift 3.0、纯器乐、固定种子、
    8 GB 卡的 offload 配置。调错档位（比如 guidance 没交给官方自动纠正）
    在这里红。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(os.path.join(_MUSIC, "music_gen.py"))

    def test_turbo_settings(self):
        self.assertIn("inference_steps=8", self.src)
        self.assertIn("shift=3.0", self.src)

    def test_instrumental(self):
        self.assertIn("instrumental=True", self.src)
        self.assertIn("[Instrumental]", self.src)

    def test_fixed_seed(self):
        self.assertIn("use_random_seed=False", self.src)
        self.assertIn("seeds=[eff]", self.src)

    def test_8gb_offload(self):
        self.assertIn("offload_to_cpu=True", self.src)
        self.assertIn("offload_dit_to_cpu=True", self.src)
        # LM 是 thinking 谱曲的执行者：pt 后端加载；caption 三连关保真
        # （劫持只由 use_cot_caption 控制，thinking 开着不碰 caption）。
        self.assertIn('backend="pt"', self.src)
        self.assertIn("use_cot_caption=False", self.src)
        self.assertIn("thinking=True", self.src)

    def test_progress_on_stderr_only(self):
        # log() 必须写 stderr —— stdout 是结果通道（解析契约的另一半）。
        self.assertIn("file=sys.stderr", self.src)

    def test_empty_value_flags_dispatch(self):
        # `--save ""` / `--delete ""` 必须进对应分支并报错，而不是因空串
        # 为假滑进 status 分支假装成功（Windows argv 实测会传空串进来）。
        tmp = tempfile.mkdtemp(prefix="mu_empty_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for flag in ("--save", "--delete"):
            rc, data = _cli(flag, "", "--lib-dir", tmp)
            self.assertNotEqual(rc, 0, msg=flag)
            self.assertIn("error", data, msg=flag)


class TestBgmLibraryWiring(unittest.TestCase):
    """AI 音乐库 → 项目 BGM 选择链：生成入库的曲子必须能被项目选用作 BGM。"""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *parts):
        with io.open(os.path.join(self.root, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_mode_has_library_option(self):
        from podcast_maker import config_manager as cm
        opts = cm.MODE_SPEC["bgm.mode"]["options"]
        self.assertIn("library", opts)
        self.assertIn("library", cm.PARAM_SPEC["bgm.mode"].get("options", opts))

    def test_library_name_point_registered(self):
        from podcast_maker import config_manager as cm
        spec = cm.PARAM_SPEC["bgm.library_name"]
        self.assertEqual(spec.get("options_source"), "music-library")

    def test_library_mode_without_name_rejected(self):
        from podcast_maker import config_manager as cm
        warns, errs = cm.validate_config({"bgm.mode": "library",
                                          "bgm.library_name": ""})
        self.assertTrue(any("AI 音乐库" in e for e in errs))

    def test_assets_factory_library_branch(self):
        src = self._read("podcast_maker", "assets_factory.py")
        self.assertIn("def library_bgm_path", src)
        self.assertIn('mode == "library"', src)
        self.assertIn("library_bgm_path(name)", src)
        # 空曲名/缺曲目都是停产报错，绝不静默降级到内置档。
        self.assertIn("没有选曲目", src)
        self.assertIn("没有曲目", src)

    def test_web_ui_wiring(self):
        src = self._read("podcast_maker", "web_ui.py")
        self.assertIn("fillMusicLibrarySelects", src)
        self.assertGreaterEqual(src.count("options_source==='music-library'"), 1)
        # 刷新链：refreshMusicState 内必须回填曲目下拉（实时刷新铁律）。
        self.assertLess(src.index("async function refreshMusicState"),
                        src.index("fillMusicLibrarySelects();\n  if(el('mu-manage')"))
        # bgm 卡点位清单必须带曲目下拉。
        self.assertIn("'bgm.library_name'", src)


if __name__ == "__main__":
    unittest.main()
