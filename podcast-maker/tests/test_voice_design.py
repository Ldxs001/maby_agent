# -*- coding: utf-8 -*-
"""VoiceDesign 接入的钉子。

覆盖三块：
  1. 真行为：make_voice.sanitize_role_name / adopt / named_roles / status 的
     档案判定与复制语义（临时目录，不起模型）。
  2. 源码钉：serve.py 的 VoiceDesign 显式分流（描述走 instruct、无 speaker）、
     make_voice 的 design 路径、安装链两个清单。
  3. 界面钉：web_ui 两条新路由、面板输入框与认领按钮在最终下发页面里。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICE = os.path.join(_ROOT, "tts_service")
sys.path.insert(0, _SERVICE)
sys.path.insert(0, _ROOT)

import contextlib  # noqa: E402
import unittest.mock as mock  # noqa: E402

import make_voice  # noqa: E402
import serve  # noqa: E402
import podcast_maker.web_ui as web_ui  # noqa: E402
import podcast_maker.layout as layout  # noqa: E402


def _fake_ensure_base_ref(project_dir, role, *a, **k):
    """认领链里 ref_base 环节的替身：不打模型，账本语义由专门的测试类钉。"""
    return {"role": role, "hit": True, "sign": "fakesign", "entry": {}}


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


class TestSanitizeRoleName(unittest.TestCase):
    def test_strips_whitespace_and_clean_chars(self):
        self.assertEqual(make_voice.sanitize_role_name("  老陈说书 "), "老陈说书")
        self.assertEqual(make_voice.sanitize_role_name("a/b\\c"), "abc")

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            make_voice.sanitize_role_name("   ")
        with self.assertRaises(RuntimeError):
            make_voice.sanitize_role_name("/..")

    def test_role_names_reserved(self):
        for bad in ("A", "B", "a", "b"):
            with self.assertRaises(RuntimeError, msg=bad):
                make_voice.sanitize_role_name(bad)

    def test_too_long_raises(self):
        with self.assertRaises(RuntimeError):
            make_voice.sanitize_role_name("x" * 25)


class TestAdoptAndStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vd_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "音色")
        p = mock.patch.object(make_voice, "ensure_base_ref", _fake_ensure_base_ref)
        p.start()
        self.addCleanup(p.stop)

    def _install_profile(self, name):
        d = make_voice.role_dir(self.root, name)
        os.makedirs(d, exist_ok=True)
        make_voice.write_profile(d, name, b"RIFF" + b"\0" * 2048, "测试文案。", {
            "source": "voicedesign", "builtin_voice": "",
            "design_description": "测试嗓音", "ref_md5": "x",
        })

    def test_adopt_copies_files_byte_identical(self):
        self._install_profile("老陈")
        make_voice.adopt(self.tmp, "老陈", "A", force=True)
        got = make_voice.ref_paths(self.root, "A")
        self.assertIsNotNone(got)
        with open(got[0], "rb") as fh:
            self.assertEqual(fh.read(), b"RIFF" + b"\0" * 2048)
        rec = make_voice.read_profile(self.root, "A")
        self.assertEqual(rec["adopted_from"], "老陈")

    def test_adopt_refuses_missing_profile_and_bad_role(self):
        with self.assertRaises(RuntimeError):
            make_voice.adopt(self.tmp, "没有的", "A")
        self._install_profile("老陈")
        with self.assertRaises(RuntimeError):
            make_voice.adopt(self.tmp, "老陈", "C")

    def test_adopt_refuses_overwrite_without_force(self):
        self._install_profile("老陈")
        self._install_profile("A")
        with self.assertRaises(RuntimeError):
            make_voice.adopt(self.tmp, "老陈", "A")

    def test_adopt_refuses_self(self):
        self._install_profile("A")
        with self.assertRaises(RuntimeError):
            make_voice.adopt(self.tmp, "A", "A")

    def test_named_roles_and_status_see_design_profiles(self):
        self._install_profile("老陈")
        self._install_profile("A")
        self.assertEqual(make_voice.named_roles(self.root), ["老陈"])
        st = make_voice.status(self.tmp)
        self.assertIn("A", st["roles"])
        self.assertIn("老陈", st["roles"])
        self.assertEqual(st["roles"]["老陈"]["design_description"], "测试嗓音")


class TestServeDesignRouting(unittest.TestCase):
    """serve.py 的 VoiceDesign 分流。

    接口事实：faster-qwen3-tts 的 generate_voice_design(text, instruct, language)
    没有 speaker 参数 —— 描述必须走 instruct，否则 TypeError 两连直接失败。
    """

    def setUp(self):
        self.src = _read(os.path.join(_SERVICE, "serve.py"))

    def test_constant_present(self):
        self.assertIn('"Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"', self.src)

    def test_synth_one_routes_design_before_generic_loop(self):
        seg = self.src[self.src.index("def synth_one"):]
        seg = seg[:seg.index("\n    def ", 10)]
        self.assertIn('self.kind == "voice_design"', seg)
        design_call = seg.index('self.kind == "voice_design"')
        generic = seg.index('for attr in ("generate_custom_voice"')
        self.assertLess(design_call, generic,
                        "voice_design 必须在通用参数循环之前显式分流")

    def test_gen_design_uses_instruct_not_speaker(self):
        seg = self.src[self.src.index("def _gen_design"):]
        seg = seg[:seg.index("\n    def ", 10)]
        self.assertIn('"instruct": desc', seg)
        self.assertNotIn('"speaker"', seg)
        self.assertIn("if not desc:", seg)

    def test_warm_is_kind_aware(self):
        seg = self.src[self.src.index("def _consume_first_inference"):]
        seg = seg[:seg.index("\n    def ", 10)]
        self.assertIn('self.kind == "voice_design"', seg)


class TestMakeVoiceDesignPath(unittest.TestCase):
    def setUp(self):
        self.src = _read(os.path.join(_SERVICE, "make_voice.py"))

    def test_design_uses_voice_design_variant_and_gate(self):
        seg = self.src[self.src.index("def design("):]
        seg = seg[:seg.index("\ndef ", 10)]
        self.assertIn("serve.VOICE_DESIGN_MODEL", seg)
        self.assertIn('kind != "voice_design"', seg)
        self.assertIn('"source": "voicedesign"', seg)
        self.assertIn("voiced_stats", seg)
        self.assertIn("MAX_ATTEMPTS", seg)

    def test_design_seed_covers_description(self):
        seg = self.src[self.src.index("def design("):]
        seg = seg[:seg.index("\ndef ", 10)]
        # 种子键 = 该案文案 + 档案名（基准名+案） + 描述 —— 双案文案不同
        # 种子天然岔开；同名同描述换文案也必出不同波形。
        self.assertIn('dir_name + "\\x00" + desc', seg)

    def test_cli_flags_wired(self):
        self.assertIn("--design-name", self.src)
        self.assertIn("--design-desc", self.src)
        self.assertIn("--adopt-from", self.src)
        self.assertIn("--adopt-role", self.src)


class TestSetupChain(unittest.TestCase):
    def test_fetch_model_lists_voice_design(self):
        src = _read(os.path.join(_SERVICE, "fetch_model.py"))
        self.assertIn('"Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"', src)

    def test_setup_env_lists_voice_design_and_keeps_base_serve(self):
        src = _read(os.path.join(_SERVICE, "setup_env.py"))
        self.assertIn('"Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"', src)
        # 常驻服务的必须是 Base：VoiceDesign 只造音色，不做常态合成。
        self.assertIn('SERVE_MODEL = MODELS[1]', src)
        self.assertLess(src.index('"Qwen/Qwen3-TTS-12Hz-1.7B-Base"'),
                        src.index('"Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"'),
                        "MODELS 里 Base 必须排第二（SERVE_MODEL = MODELS[1]）")


class TestWebUiDesignPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web_ui._ensure_page_built() if hasattr(web_ui, "_ensure_page_built") else None
        cls.page = web_ui.PAGE if hasattr(web_ui, "PAGE") else ""
        cls.src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))

    def test_routes_registered(self):
        # 路由注册在 Python 的 ROUTES 表里，不在下发页面里
        self.assertIn('"/api/voice/design"', self.src)
        self.assertIn('"/api/voice/adopt"', self.src)

    def test_page_has_design_inputs_and_adopt_buttons(self):
        self.assertIn('id="vd-name"', self.page)
        self.assertIn('id="vd-desc"', self.page)
        self.assertIn('id="vd-text"', self.page)
        self.assertIn("voiceDesign()", self.page)
        self.assertIn("voiceAdopt(", self.page)

    def test_adopt_uses_inline_double_confirm_not_alert(self):
        self.assertIn("dataset.armed", self.page)
        self.assertNotIn("confirm(", self.page.split("function voiceAdopt")[1]
                         .split("function ")[0])
        self.assertNotIn("alert(", self.page.split("function voiceAdopt")[1]
                         .split("function ")[0])

    def test_validate_design_name_is_fail_closed(self):
        self.assertTrue(web_ui._validate_design_name(""))
        self.assertTrue(web_ui._validate_design_name("a/b"))
        self.assertTrue(web_ui._validate_design_name("A"))
        self.assertEqual(web_ui._validate_design_name("老陈说书"), "")


class TestOfficialAdopt(unittest.TestCase):
    """声库认领：真行为打在临时 resources/voices 上，不起模型。

    认领源是随仓声库（make_voice.LIBRARY_DIR），每条音色自带 text_role ——
    A 角只认 A 案、B 角只认 B 案是认领层唯一的硬把关。车间素材池
    （OFFICIAL_DIR / official_presets）只服务重录与试听，行为保持不变。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="official_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.off_dir = os.path.join(self.tmp, "official_voices")
        self.lib_dir = os.path.join(self.tmp, "voices")
        self._real_official_dir = make_voice.OFFICIAL_DIR
        self._real_library_dir = make_voice.LIBRARY_DIR
        make_voice.OFFICIAL_DIR = self.off_dir
        make_voice.LIBRARY_DIR = self.lib_dir
        self.addCleanup(setattr, make_voice, "OFFICIAL_DIR",
                        self._real_official_dir)
        self.addCleanup(setattr, make_voice, "LIBRARY_DIR",
                        self._real_library_dir)
        p = mock.patch.object(make_voice, "ensure_base_ref", _fake_ensure_base_ref)
        p.start()
        self.addCleanup(p.stop)
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj, exist_ok=True)
        self.wav = b"RIFF" + b"\x01" * 4096
        self.text_b = "我们先把问题理清楚，一步一步来。总有人问：\"这样讲大家能听明白吗？\"可没想到……答案竟这么简单！"

    def _install_official(self, name, chosen, takes=None):
        d = os.path.join(self.off_dir, name)
        os.makedirs(d, exist_ok=True)
        prof = {"schema": 1, "kind": "official_preset", "builtin_voice": name,
                "language_tag": "Chinese", "desc": "测试预设",
                "text": "这本书写得很有意思。这书真的是AI写的吗？这个结果太让人吃惊了！",
                "chosen": chosen, "takes": takes or []}
        with io.open(os.path.join(d, "profile.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(prof, fh, ensure_ascii=False, indent=1)
        for t in (takes and [x["take"] for x in takes] or chosen):
            with open(os.path.join(d, "take%d.wav" % t), "wb") as fh:
                fh.write(self.wav)

    def _install_library(self, name, role, wav=None, take=1, text=None):
        d = os.path.join(self.lib_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ref.wav"), "wb") as fh:
            fh.write(wav if wav is not None else self.wav)
        text = text or self.text_b
        with io.open(os.path.join(d, "ref.txt"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        prof = {"schema": 1, "kind": "voice_library", "name": name,
                "text_role": role, "text": text, "family": "测试家族",
                "builtin_voice": "Serena", "official_take": take,
                "seed": 11, "sha16": "aaaaaaaaaaaaaaaa",
                "f0_med": 214.3, "seconds": 6.4, "voiced_seconds": 6.1,
                "speech_rate": 7.9, "language_tag": "Chinese", "desc": "测试预设"}
        with io.open(os.path.join(d, "profile.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(prof, fh, ensure_ascii=False, indent=1)

    def test_official_presets_lists_only_official_kind(self):
        self._install_official("Serena", [1])
        d = os.path.join(self.off_dir, "杂鱼")
        os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(d, "profile.json"), "w",
                     encoding="utf-8") as fh:
            json.dump({"kind": "something_else"}, fh)
        got = make_voice.official_presets()
        self.assertEqual([p["name"] for p in got], ["Serena"])
        self.assertEqual(got[0]["chosen"], [1])

    def test_library_voices_lists_only_library_kind(self):
        self._install_library("老傅-收束", "B")
        d = os.path.join(self.lib_dir, "杂鱼")
        os.makedirs(d, exist_ok=True)
        with io.open(os.path.join(d, "profile.json"), "w",
                     encoding="utf-8") as fh:
            json.dump({"kind": "something_else"}, fh)
        got = make_voice.library_voices()
        self.assertEqual([v["name"] for v in got], ["老傅-收束"])
        self.assertEqual(got[0]["text_role"], "B")

    def test_adopt_official_copies_byte_identical(self):
        self._install_library("老傅-收束", "B")
        res = make_voice.adopt_official(self.proj, "老傅-收束", "B")
        self.assertEqual((res["role"], res["voice"]), ("B", "老傅-收束"))
        wav, txt = make_voice.ref_paths(
            os.path.join(self.proj, "音色"), "B")
        self.assertIsNotNone(wav)
        with open(wav, "rb") as fh:
            self.assertEqual(fh.read(), self.wav)       # 逐字节相同
        self.assertIn("听明白吗", txt)                  # 转录随档案走（ref_paths 返回内容）
        rec = make_voice.read_profile(os.path.join(self.proj, "音色"), "B")
        self.assertEqual(rec["source"], "voice_library")
        self.assertEqual(rec["official_take"], 1)
        self.assertEqual(rec["ref_md5"],
                         __import__("hashlib").sha256(self.wav).hexdigest()[:16])
        # 预录时实测的语速/有声时长认领时照抄 —— 生成那天就量好的，
        # 不该让槽位档案在这两栏空着，逼人回头查预录账本。
        self.assertEqual(rec["voiced_seconds"], 6.1)
        self.assertEqual(rec["speech_rate"], 7.9)

    def test_adopt_official_refuses_role_mismatch(self):
        self._install_library("老傅-收束", "B")
        with self.assertRaises(RuntimeError) as ctx:
            make_voice.adopt_official(self.proj, "老傅-收束", "A")
        self.assertIn("A 案", str(ctx.exception))

    def test_adopt_official_refuses_take_mismatch(self):
        self._install_library("老傅-收束", "B", take=6)
        with self.assertRaises(RuntimeError):
            make_voice.adopt_official(self.proj, "老傅-收束", "B", 2)

    def test_adopt_official_refuses_unknown_voice(self):
        self._install_library("老傅-收束", "B")
        with self.assertRaises(RuntimeError):
            make_voice.adopt_official(self.proj, "不存在的", "A")

    def test_adopt_official_refuses_overwrite_without_force(self):
        self._install_library("老傅-收束", "B")
        make_voice.adopt_official(self.proj, "老傅-收束", "B")
        with self.assertRaises(RuntimeError):
            make_voice.adopt_official(self.proj, "老傅-收束", "B")
        make_voice.adopt_official(self.proj, "老傅-收束", "B", force=True)

    def test_cli_flags_present(self):
        src = _read(os.path.join(_SERVICE, "make_voice.py"))
        for flag in ('"--officials"', '"--official-adopt"', '"--official-take"'):
            self.assertIn(flag, src)
        self.assertIn("adopt_official(project, args.official_adopt", src)

    def test_web_ui_routes_and_audio_endpoint(self):
        src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))
        self.assertIn('"/api/voice/officials"', src)
        self.assertIn('"/api/voice/official-adopt"', src)
        page = getattr(web_ui, "PAGE", "") or ""
        self.assertIn('"/api/voice/library-audio/', page)   # 声库试听在下发页里
        self.assertIn("/api/voice/official-audio/", page)   # 旧值兼容（rawPreview 单引号形式）
        self.assertIn("voiceAdoptOfficial", page)


class TestBaseRefLedger(unittest.TestCase):
    """ref_base 版本账本：sign 绑原生 ref、命中不重做、换嗓保留旧版、
    读者 fail-closed、声纹锚与合成条件同源。生成环节用替身（不起模型），
    落盘与账本语义走真代码。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="base_ref_ledger_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "音色")
        self.wav_a = b"RIFF" + b"\x01" * 4096
        self.wav_b = b"RIFF" + b"\x02" * 4096

    def _install_role(self, role, wav=None, text="这本书写得很有意思。"):
        d = make_voice.role_dir(self.root, role)
        os.makedirs(d, exist_ok=True)
        w = wav or self.wav_a
        make_voice.write_profile(d, role, w, text, {"source": "test"})
        return d

    def _sign_of(self, role):
        with open(os.path.join(make_voice.role_dir(self.root, role),
                               "ref.wav"), "rb") as fh:
            import hashlib
            return hashlib.sha256(fh.read()).hexdigest()[:8]

    @staticmethod
    def _fake_mbr(wav_bytes=b"RIFFbase" + b"\x03" * 2048):
        """make_base_ref.build 的替身：写三件 + 记账 + 切 active，不起模型。"""

        class _M:
            calls = []

            @classmethod
            def build(cls, project_dir, roles=("A", "B"),
                      voice_dir_name="音色", force=False, device="auto",
                      log=print, sample_rate=None):
                cls.calls.append(list(roles))
                root = os.path.join(project_dir, voice_dir_name)
                out = {"roles": {}}
                for r in roles:
                    d = make_voice.role_dir(root, r)
                    sign = make_voice.digest_of(
                        os.path.join(d, "ref.wav"))[:8]
                    names = {"ref_base": "%s_%s.wav" % (make_voice.BASEREF_STEM, sign),
                             "icl_wav": "%s_%s.wav" % (make_voice.ICL_STEM, sign),
                             "icl_txt": "%s_%s.txt" % (make_voice.ICL_STEM, sign)}
                    for k, n in names.items():
                        p = os.path.join(d, n)
                        if k == "icl_txt":
                            with io.open(p, "w", encoding="utf-8",
                                         newline="\n") as fh:
                                fh.write("文本\n文本\n")
                        else:
                            with open(p, "wb") as fh:
                                fh.write(wav_bytes)
                    led = make_voice.read_base_refs(root, r) or {
                        "schema": 1, "active": None, "entries": {}}
                    led["entries"][sign] = dict(names, ref_sha16=sign * 2,
                                                created="t")
                    led["active"] = sign
                    make_voice.write_base_refs(root, r, led)
                    out["roles"][r] = {"role": r, "skipped": False,
                                       "sign": sign}
                return out

        return _M

    def _patch_mbr(self, fake):
        p = mock.patch.dict(sys.modules, {"make_base_ref": fake})
        p.start()
        self.addCleanup(p.stop)

    def test_hit_reuses_without_regeneration(self):
        self._install_role("A")
        sign = self._sign_of("A")
        d = make_voice.role_dir(self.root, "A")
        names = {"ref_base": "ref_base_%s.wav" % sign,
                 "icl_wav": "icl_%s.wav" % sign, "icl_txt": "icl_%s.txt" % sign}
        for n in names.values():
            with open(os.path.join(d, n), "wb") as fh:
                fh.write(b"x" * 512)
        led = {"schema": 1, "active": None,
               "entries": {sign: dict(names, ref_sha16=sign * 2, created="t")}}
        make_voice.write_base_refs(self.root, "A", led)

        fake = self._fake_mbr()
        fake.build = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("账本命中不该再生成"))
        self._patch_mbr(fake)
        out = make_voice.ensure_base_ref(self.tmp, "A")
        self.assertTrue(out["hit"])
        led = make_voice.read_base_refs(self.root, "A")
        self.assertEqual(led["active"], sign)

    def test_miss_generates_and_records(self):
        self._install_role("A")
        fake = self._fake_mbr()
        self._patch_mbr(fake)
        out = make_voice.ensure_base_ref(self.tmp, "A")
        self.assertFalse(out["hit"])
        self.assertEqual(fake.calls, [["A"]])
        sign = self._sign_of("A")
        d = make_voice.role_dir(self.root, "A")
        led = make_voice.read_base_refs(self.root, "A")
        self.assertEqual(led["active"], sign)
        self.assertIn(sign, led["entries"])
        for k in ("ref_base", "icl_wav", "icl_txt"):
            self.assertTrue(os.path.isfile(
                os.path.join(d, led["entries"][sign][k])), msg=k)

    def test_voice_switch_keeps_old_versions(self):
        self._install_role("A", wav=self.wav_a)
        fake = self._fake_mbr()
        self._patch_mbr(fake)
        make_voice.ensure_base_ref(self.tmp, "A")
        old_sign = self._sign_of("A")

        self._install_role("A", wav=self.wav_b)      # 换嗓：ref.wav 换字节
        make_voice.ensure_base_ref(self.tmp, "A")
        new_sign = self._sign_of("A")
        self.assertNotEqual(old_sign, new_sign)
        d = make_voice.role_dir(self.root, "A")
        led = make_voice.read_base_refs(self.root, "A")
        self.assertEqual(led["active"], new_sign)
        self.assertIn(old_sign, led["entries"])
        self.assertTrue(os.path.isfile(
            os.path.join(d, "ref_base_%s.wav" % old_sign)),
            "旧嗓的 ref_base 版本件必须原样保留")

    def test_switch_back_reuses_entry_without_regeneration(self):
        self._install_role("A", wav=self.wav_a)
        fake = self._fake_mbr()
        self._patch_mbr(fake)
        make_voice.ensure_base_ref(self.tmp, "A")
        old_sign = self._sign_of("A")
        self._install_role("A", wav=self.wav_b)
        make_voice.ensure_base_ref(self.tmp, "A")

        self._install_role("A", wav=self.wav_a)      # 换回旧嗓
        fake.build = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("换回旧嗓应命中账本，不该重新克隆"))
        out = make_voice.ensure_base_ref(self.tmp, "A")
        self.assertTrue(out["hit"])
        led = make_voice.read_base_refs(self.root, "A")
        self.assertEqual(led["active"], old_sign)

    def test_reader_fail_closed_when_active_files_missing(self):
        import podcast_maker.tts_engine as tts_engine
        self._install_role("A")
        sign = self._sign_of("A")
        make_voice.write_base_refs(self.root, "A", {
            "schema": 1, "active": sign, "entries": {}})   # 有指向、没有件
        with self.assertRaises(tts_engine.TTSError):
            tts_engine._ref_pair(self.tmp, "A")

    def test_reader_returns_active_icl_pair_and_same_source_anchor(self):
        import podcast_maker.tts_engine as tts_engine
        self._install_role("A")
        fake = self._fake_mbr()
        self._patch_mbr(fake)
        make_voice.ensure_base_ref(self.tmp, "A")
        sign = self._sign_of("A")

        iw, it = tts_engine._ref_pair(self.tmp, "A")
        self.assertEqual(os.path.basename(iw), "icl_%s.wav" % sign)
        self.assertEqual(os.path.basename(it), "icl_%s.txt" % sign)
        prof = tts_engine.voice_profiles(self.tmp)["A"]
        self.assertEqual(prof["base_sign"], sign)
        self.assertEqual(os.path.basename(prof["base_wav"]),
                         "ref_base_%s.wav" % sign)
        # 锚与合成条件同源：合成用 icl_<sign>，锚就是同 sign 的 ref_base。
        self.assertEqual(tts_engine._sv_ref_path(prof), prof["base_wav"])

    def test_reader_without_ledger_keeps_legacy_ref_pair(self):
        import podcast_maker.tts_engine as tts_engine
        self._install_role("A")
        iw, it = tts_engine._ref_pair(self.tmp, "A")
        self.assertEqual(os.path.basename(iw), "ref.wav")
        self.assertEqual(os.path.basename(it), "ref.txt")
        prof = tts_engine.voice_profiles(self.tmp)["A"]
        self.assertNotIn("base_wav", prof)
        self.assertEqual(tts_engine._sv_ref_path(prof), prof["ref_wav"])


class TestBuildSources(unittest.TestCase):
    """build() 的改造：CustomVoice 现录已退役，「备齐档案」＝ 纯复制。

    音色来源只有三种已落地的音频 —— library:（随仓声库，首选）、official:
    （旧预录引用，经声库溯源对账兼容）与 named:（具名档案）。裸内置音色名
    当场 fail-closed 拒绝，全程不加载任何模型。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="build_sources_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib_dir = os.path.join(self.tmp, "voices")
        self._real_library_dir = make_voice.LIBRARY_DIR
        make_voice.LIBRARY_DIR = self.lib_dir
        self.addCleanup(setattr, make_voice, "LIBRARY_DIR",
                        self._real_library_dir)
        p = mock.patch.object(make_voice, "ensure_base_ref", _fake_ensure_base_ref)
        p.start()
        self.addCleanup(p.stop)
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj, exist_ok=True)
        self.wav_a = b"RIFF" + b"\x01" * 4096
        self.wav_b = b"RIFF" + b"\x02" * 4096

    def _install_library(self, name, role, wav, take=1):
        d = os.path.join(self.lib_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ref.wav"), "wb") as fh:
            fh.write(wav)
        text = "这本书写得很有意思。这书真的是AI写的吗？这个结果太让人吃惊了！"
        with io.open(os.path.join(d, "ref.txt"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        prof = {"schema": 1, "kind": "voice_library", "name": name,
                "text_role": role, "text": text, "family": "测试家族",
                "builtin_voice": "Serena", "official_take": take,
                "seed": 11, "sha16": "aaaaaaaaaaaaaaaa", "f0_med": 214.3,
                "seconds": 6.4, "voiced_seconds": 6.1, "speech_rate": 7.9,
                "language_tag": "Chinese", "desc": "测试预设"}
        with io.open(os.path.join(d, "profile.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump(prof, fh, ensure_ascii=False, indent=1)

    def _install_named(self, name, wav):
        d = os.path.join(self.proj, "音色", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ref.wav"), "wb") as fh:
            fh.write(wav)
        with io.open(os.path.join(d, "ref.txt"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write("我们先把问题理清楚。这样讲大家能听明白吗？没想到答案这么简单！")
        with io.open(os.path.join(d, "profile.json"), "w",
                     encoding="utf-8", newline="\n") as fh:
            json.dump({"schema": 1, "source": "voicedesign",
                       "design_description": "测试嗓"},
                      fh, ensure_ascii=False, indent=1)

    def test_library_and_named_land_byte_identical(self):
        self._install_library("瑟琳-利落", "A", self.wav_a)
        self._install_named("老陈", self.wav_b)
        res = make_voice.build(self.proj,
                               voices={"A": "library:瑟琳-利落",
                                       "B": "named:老陈"},
                               log=lambda m: None)
        self.assertEqual(res["generated"], ["A", "B"])
        wav, _ = make_voice.ref_paths(os.path.join(self.proj, "音色"), "A")
        with open(wav, "rb") as fh:
            self.assertEqual(fh.read(), self.wav_a)     # 逐字节相同
        wav, _ = make_voice.ref_paths(os.path.join(self.proj, "音色"), "B")
        with open(wav, "rb") as fh:
            self.assertEqual(fh.read(), self.wav_b)
        rec = make_voice.read_profile(os.path.join(self.proj, "音色"), "B")
        self.assertEqual(rec["adopted_from"], "老陈")
        rec = make_voice.read_profile(os.path.join(self.proj, "音色"), "A")
        self.assertEqual(rec["source"], "voice_library")

    def test_legacy_official_spec_resolves_via_library(self):
        # 旧 official: 引用走声库溯源对账：take 对上就照常认领，对不上拒绝。
        self._install_library("瑟琳-利落", "A", self.wav_a, take=1)
        res = make_voice.build(self.proj, roles=("A",),
                               voices={"A": "official:瑟琳-利落#1"},
                               log=lambda m: None)
        self.assertEqual(res["roles"]["A"]["take"], 1)
        with self.assertRaises(RuntimeError):
            make_voice.build(self.proj, roles=("A",),
                             voices={"A": "official:瑟琳-利落#2"},
                             force=True, log=lambda m: None)

    def test_build_refuses_role_mismatch(self):
        # A 角认 B 案：把关在认领层，build 也一样拒绝 —— 不静默换案。
        self._install_library("老傅-收束", "B", self.wav_a)
        with self.assertRaises(RuntimeError):
            make_voice.build(self.proj, roles=("A",),
                             voices={"A": "library:老傅-收束"},
                             log=lambda m: None)

    def test_bare_customvoice_name_is_refused(self):
        self._install_library("瑟琳-利落", "A", self.wav_a)
        with self.assertRaises(RuntimeError) as ctx:
            make_voice.build(self.proj, roles=("A",),
                             voices={"A": "Serena"}, log=lambda m: None)
        self.assertIn("退役", str(ctx.exception))

    def test_existing_profiles_are_left_alone(self):
        self._install_library("瑟琳-利落", "A", self.wav_a)
        self._install_library("老傅-收束", "B", self.wav_b)
        make_voice.build(self.proj, roles=("A", "B"),
                         voices={"A": "library:瑟琳-利落",
                                 "B": "library:老傅-收束"},
                         log=lambda m: None)
        wav_a = make_voice.ref_paths(os.path.join(self.proj, "音色"), "A")[0]
        with open(wav_a, "ab") as fh:
            fh.write(b"tail")                           # 弄脏 A 的现有档案
        res = make_voice.build(self.proj, roles=("A", "B"),
                               voices={"A": "library:瑟琳-利落",
                                       "B": "library:老傅-收束"},
                               log=lambda m: None)
        self.assertEqual(res["skipped"], ["A", "B"])    # 已有不 force 不动


class TestRawVoicePreview(unittest.TestCase):
    """原声试听：把选中嗓子的那条本地音频原样播放，不经标尺/语速任何换算。

    声库走 /api/voice/library-audio/，旧官方预录走 /api/voice/official-audio/
    （兼容读取），具名音色走 /api/voice/named-audio/（卡目录穿越，名字可以是中文）。
    """

    def test_named_audio_endpoint_registered_with_guard(self):
        src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))
        self.assertIn('"/api/voice/named-audio/"', src)
        seg = src[src.index('"/api/voice/named-audio/"'):
                  src.index('"/api/report/"')]
        self.assertIn("safe_join", seg)

    def test_page_has_raw_preview_button(self):
        page = getattr(web_ui, "PAGE", "") or ""
        self.assertIn("rawPreview(", page)
        self.assertIn("▶ 原声", page)
        self.assertIn("⏹ 停止", page)


class TestDraftFlow(unittest.TestCase):
    """造嗓草稿流：生成进 _draft、试听后保存/丢弃；管理删除带引用守卫。

    全部真行为，打在临时项目目录上，不起模型。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vd_draft_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        real_base = web_ui._projects_base
        web_ui._projects_base = lambda: self.tmp
        self.addCleanup(setattr, web_ui, "_projects_base", real_base)
        p = mock.patch.object(make_voice, "ensure_base_ref", _fake_ensure_base_ref)
        p.start()
        self.addCleanup(p.stop)
        self.pid = "20261006-draft"
        self.vdir = os.path.join(layout.project_dir(self.tmp, self.pid),
                                 layout.DIR_VOICE)
        os.makedirs(self.vdir, exist_ok=True)

    def _make_draft(self, role=None, text_role=None):
        """造一份草稿。role=None 时造**双案草稿**（_draftA + _draftB，
        各带 text_role）；传具体名字则造一份指定目录的档案。"""
        if role is None:
            for case in ("A", "B"):
                self._make_draft(make_voice.DRAFT_ROLE + case, text_role=case)
            return None
        d = os.path.join(self.vdir, role)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, layout.VOICE_WAV), "wb") as f:
            f.write(b"RIFF" + b"\x03" * 2048)
        with io.open(os.path.join(d, layout.VOICE_TXT), "w",
                     encoding="utf-8") as f:
            f.write("三句混合基准文案。\n")
        prof = {"role": role, "design_description": "低沉男声",
                "base_name": role.rstrip("AB") or role,
                "ref_seconds": 9.9}
        if text_role:
            prof["text_role"] = text_role
        with io.open(os.path.join(d, layout.VOICE_PROFILE), "w",
                     encoding="utf-8") as f:
            json.dump(prof, f)
        return d

    def test_draft_role_names_match_across_files(self):
        self.assertEqual(web_ui._VOICE_DRAFT, make_voice.DRAFT_ROLE)
        # 双案草稿目录 = 基准名 + 案后缀，两边同源（谁改一边不改另一边测试红）
        self.assertEqual(web_ui._VOICE_DRAFTS, make_voice.DRAFT_ROLES)
        self.assertEqual(web_ui._VOICE_DRAFTS,
                         tuple(web_ui._VOICE_DRAFT + c for c in "AB"))

    def test_named_roles_and_options_skip_draft(self):
        self._make_draft()
        self.assertEqual(make_voice.named_roles(self.vdir), [])
        # 档案本身是完整的：ref_paths 认它，草稿试听端点才有东西可放
        for case in ("A", "B"):
            self.assertIsNotNone(
                make_voice.ref_paths(self.vdir, make_voice.DRAFT_ROLE + case))
        self.assertEqual(web_ui._named_voice_options(self.pid), [])

    def test_draft_save_renames_and_rewrites_role(self):
        self._make_draft()
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "老陈说书", "case": "A"})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["name"], "老陈说书A")     # 目录名 = 名字 + 案后缀
        d = os.path.join(self.vdir, "老陈说书A")
        self.assertTrue(os.path.isdir(d))
        self.assertFalse(os.path.exists(
            os.path.join(self.vdir, make_voice.DRAFT_ROLE + "A")))
        self.assertTrue(os.path.isdir(os.path.join(
            self.vdir, make_voice.DRAFT_ROLE + "B")))   # B 案草稿不受影响
        prof = json.load(io.open(os.path.join(d, layout.VOICE_PROFILE),
                                 encoding="utf-8"))
        self.assertEqual(prof["role"], "老陈说书A")
        self.assertEqual(prof["text_role"], "A")     # 分案标记跟着落盘
        self.assertEqual(prof["base_name"], "老陈说书")

    def test_draft_save_fail_closed(self):
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "老陈说书", "case": "A"})
        self.assertFalse(r.get("ok"))          # 没有草稿
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "老陈说书"})
        self.assertFalse(r.get("ok"))          # 缺 case → 拒绝，不猜
        self._make_draft()
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "a/b", "case": "A"})
        self.assertFalse(r.get("ok"))          # 非法名
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "老陈说书", "case": "A"})
        self.assertTrue(r.get("ok"))
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "老陈说书", "case": "B"})
        self.assertTrue(r.get("ok"))           # 同名两案不冲突（B 案存 B）
        self._make_draft()                     # 再造草稿，撞已有名字
        r = web_ui.api_voice_draft_save({"project": self.pid,
                                         "name": "老陈说书", "case": "A"})
        self.assertFalse(r.get("ok"))          # 已存在 → 拒绝，不悄悄覆盖

    def test_draft_state_reports_cases_independently(self):
        r = web_ui.api_voice_draft_state({"project": self.pid})
        self.assertTrue(r["ok"])
        self.assertFalse(r["exists"])
        self._make_draft()
        r = web_ui.api_voice_draft_state({"project": self.pid})
        self.assertTrue(r["exists"])
        self.assertTrue(r["cases"]["A"]["exists"])
        self.assertTrue(r["cases"]["B"]["exists"])
        self.assertEqual(r["cases"]["A"]["profile"]["text_role"], "A")
        # 保存掉 A 案后，state 里只剩 B 案 —— 各案独立，不连坐
        web_ui.api_voice_draft_save({"project": self.pid,
                                     "name": "老陈说书", "case": "A"})
        r = web_ui.api_voice_draft_state({"project": self.pid})
        self.assertFalse(r["cases"]["A"]["exists"])
        self.assertTrue(r["cases"]["B"]["exists"])

    def test_draft_discard_removes_and_is_idempotent(self):
        self._make_draft()
        r = web_ui.api_voice_draft_discard({"project": self.pid})
        self.assertTrue(r.get("ok"))
        for case in ("A", "B"):
            self.assertFalse(os.path.exists(os.path.join(
                self.vdir, make_voice.DRAFT_ROLE + case)))
        r = web_ui.api_voice_draft_discard({"project": self.pid})
        self.assertTrue(r.get("ok"))           # 没有草稿也成功（幂等）

    def test_named_delete_refuses_draft_dirs(self):
        self._make_draft()
        for case in ("A", "B"):
            r = web_ui.api_voice_named_delete(
                {"project": self.pid, "name": make_voice.DRAFT_ROLE + case})
            self.assertFalse(r.get("ok"))
            self.assertIn("草稿", r["error"])

    def test_named_options_carry_role_and_base_name(self):
        self._make_draft("老陈说书A", text_role="A")
        self._make_draft("老陈说书B", text_role="B")
        self._make_draft("旧嗓")               # 无 text_role 的旧档案
        opts = {o["name"]: o for o in web_ui._named_voice_options(self.pid)}
        self.assertEqual(opts["named:老陈说书A"]["role"], "A")
        self.assertEqual(opts["named:老陈说书B"]["role"], "B")
        self.assertIn("老陈说书·A案", opts["named:老陈说书A"]["label"])
        self.assertIsNone(opts["named:旧嗓"]["role"])   # 旧档案两边都留

    def test_adopt_honours_text_role(self):
        self._make_draft("老陈说书A", text_role="A")
        # A 案嗓子认给 B 角 = B 角拿 A 案文案的节奏先验，当场拒绝
        with self.assertRaises(RuntimeError) as ctx:
            make_voice.adopt(self.tmp_project(), "老陈说书A", "B", force=True)
        self.assertIn("B 角只能认B案", str(ctx.exception))
        res = make_voice.adopt(self.tmp_project(), "老陈说书A", "A",
                               force=True)
        self.assertEqual(res["adopted_from"], "老陈说书A")
        # 无 text_role 的旧档案不受限制
        self._make_draft("旧嗓")
        res = make_voice.adopt(self.tmp_project(), "旧嗓", "B", force=True)
        self.assertEqual(res["adopted_from"], "旧嗓")

    def tmp_project(self):
        return self.pid and layout.project_dir(self.tmp, self.pid)

    def test_library_promote(self):
        self._make_draft("老陈说书A", text_role="A")
        lib = os.path.join(_ROOT, "podcast_maker", "resources", "voices")
        dst = os.path.join(lib, "老陈说书A")
        self.assertFalse(os.path.exists(dst))   # 测试前置：声库里没有这条
        r = web_ui.api_voice_library_promote({"project": self.pid,
                                              "name": "老陈说书A"})
        try:
            self.assertTrue(r.get("ok"), r)
            self.assertEqual(r["text_role"], "A")
            self.assertTrue(os.path.isfile(os.path.join(dst, "ref.wav")))
            prof = json.load(io.open(os.path.join(dst, layout.VOICE_PROFILE),
                                     encoding="utf-8"))
            self.assertEqual(prof["kind"], "voice_library")
            self.assertEqual(prof["text_role"], "A")
            # 项目内原档案不动
            self.assertTrue(os.path.isdir(os.path.join(self.vdir, "老陈说书A")))
            # 声库同名已存在 → 拒绝
            self._make_draft("老陈说书A", text_role="A")
            r = web_ui.api_voice_library_promote({"project": self.pid,
                                                  "name": "老陈说书A"})
            self.assertFalse(r.get("ok"))
        finally:
            shutil.rmtree(dst, ignore_errors=True)
        # 无 text_role 的旧档案进不了声库
        self._make_draft("旧嗓")
        r = web_ui.api_voice_library_promote({"project": self.pid,
                                              "name": "旧嗓"})
        self.assertFalse(r.get("ok"))
        self.assertIn("text_role", r["error"])

    def test_named_delete_refuses_when_role_uses_it(self):
        self._make_draft("说书人")
        saved = {"tts.qwen3tts_voice_a": "named:说书人",
                 "tts.qwen3tts_voice_b": "library:老傅-收束"}

        class _Cfg:
            def data(self):
                return saved

        real = web_ui.CFG
        web_ui.CFG = _Cfg()
        self.addCleanup(setattr, web_ui, "CFG", real)
        r = web_ui.api_voice_named_delete({"project": self.pid, "name": "说书人"})
        self.assertFalse(r.get("ok"))
        self.assertIn("A 角", r["error"])
        saved["tts.qwen3tts_voice_a"] = "library:薇薇-清亮"
        r = web_ui.api_voice_named_delete({"project": self.pid, "name": "说书人"})
        self.assertTrue(r.get("ok"), r)
        self.assertFalse(os.path.exists(os.path.join(self.vdir, "说书人")))

    def test_draft_routes_registered(self):
        src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))
        for route in ('"/api/voice/draft"', '"/api/voice/draft/save"',
                      '"/api/voice/draft/discard"',
                      '"/api/voice/named/delete"',
                      '"/api/voice/library/promote"'):
            self.assertIn(route, src)

    def test_page_has_draft_buttons_and_manage(self):
        page = getattr(web_ui, "PAGE", "") or ""
        for token in ("vdDraftPreview(this,", "vdDraftSave(",
                      "vdDraftDiscard(", "vdDraftCheck(", "vdPromote(",
                      "vdManageToggle(", "vdManageDelete(", "管理音色"):
            self.assertIn(token, page)
        seg = page.split("function vdManageDelete")[1].split("function ")[0]
        self.assertNotIn("alert(", seg)
        self.assertNotIn("confirm(", seg)

    def test_official_adopt_endpoint_honours_force(self):
        src = _read(os.path.join(_ROOT, "podcast_maker", "web_ui.py"))
        seg = src.split("def api_voice_official_adopt(")[1].split("\ndef ")[0]
        self.assertIn('body.get("force")', seg)
        self.assertIn('"--force"', seg)

    def test_design_seed_offset_wired_end_to_end(self):
        src = _read(os.path.join(_SERVICE, "make_voice.py"))
        self.assertIn("--design-seed-offset", src)
        seg = src.split("def design(")[1].split("\ndef ")[0]
        self.assertIn("seed_offset", seg)
        self.assertIn("+ int(seed_offset)", seg)

    def test_cli_error_extracts_message_from_json_shell(self):
        # 工具失败时 stdout 是 {"error": ...}，报给用户的必须是里面那句人话，
        # 不是连壳的引号大括号；stderr 里有话时也取得到。
        r = web_ui._cli_error('{"error": "B 角已有档案，需要 --force。"}', "")
        self.assertEqual(r, "B 角已有档案，需要 --force。")
        r = web_ui._cli_error("", "环境没建好")
        self.assertIn("环境没建好", r)


class TestLoudnessNormalize(unittest.TestCase):
    """入库即归一：可选项落盘前统一响度（-23 dBFS RMS / 峰值 ≤ -1.5 dBFS）。

    Base 克隆继承 ref 响度 —— 实测定稿 take 间极差约 12 dB，不归一的话
    A/B 成片一个像吼一个像蚊子。归一在入库那一刻做一次，试听=上场=同一份。
    """

    @staticmethod
    def _wav_bytes(amplitude, freq=440.0, seconds=1.0, sr=24000):
        import math
        import struct
        import wave as _wave
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            frames = b"".join(
                struct.pack("<h", int(amplitude * 32767 *
                                      math.sin(2 * math.pi * freq * i / sr)))
                for i in range(int(sr * seconds)))
            w.writeframes(frames)
        return buf.getvalue()

    def test_normalize_raises_quiet_audio_to_target(self):
        wav = self._wav_bytes(0.05)          # 很轻（约 -26 dBFS 峰值档）
        out, gain, rms = make_voice.normalize_wav_bytes(wav)
        self.assertIsNot(out, wav)           # 字节确实被改写
        a, _ = make_voice._wav_pcm16_to_array(out)
        self.assertAlmostEqual(make_voice.rms_dbfs(a), -23.0, delta=0.15)
        self.assertGreater(gain, 0)

    def test_normalize_caps_peak_instead_of_clipping(self):
        # 高峰值素材：响度提到目标会让峰值超 -1.5 dBFS，增益必须被峰值收住
        wav = self._wav_bytes(0.85, seconds=0.5)
        out, gain, _ = make_voice.normalize_wav_bytes(wav)
        a, _ = make_voice._wav_pcm16_to_array(out)
        self.assertLessEqual(make_voice.peak_dbfs(a), -1.49)
        self.assertEqual(gain, round(gain, 2))

    def test_normalize_noop_near_target(self):
        wav = self._wav_bytes(0.05)
        already, _, _ = make_voice.normalize_wav_bytes(wav)
        again, gain, _ = make_voice.normalize_wav_bytes(already)
        self.assertIs(again, already)        # ±0.2 dB 内原样返回，不改字节
        self.assertEqual(gain, 0.0)

    def test_silence_is_rejected(self):
        wav = self._wav_bytes(0.0)
        with self.assertRaises(ValueError):
            make_voice.normalize_wav_bytes(wav)

    def test_backfill_updates_ledger_and_keeps_chosen(self):
        tmp = tempfile.mkdtemp(prefix="loud_backfill_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        quiet = self._wav_bytes(0.02)        # 明显偏轻，必被抬升
        for name in ("Serena", "Vivian"):
            d = os.path.join(tmp, name)
            os.makedirs(d)
            with open(os.path.join(d, "take1.wav"), "wb") as f:
                f.write(quiet)
            with io.open(os.path.join(d, "profile.json"), "w",
                         encoding="utf-8") as f:
                json.dump({"kind": "official_preset", "builtin_voice": name,
                           "chosen": [1],
                           "takes": [{"take": 1, "sha16": "old-old"}]}, f)
        rc = make_voice.normalize_official_archive(tmp, log=lambda *_: None)
        self.assertEqual(rc, 0)
        prof = json.load(io.open(
            os.path.join(tmp, "Serena", "profile.json"), encoding="utf-8"))
        t = prof["takes"][0]
        self.assertNotEqual(t["sha16"], "old-old")     # 字节变了，指纹重算
        self.assertEqual(t["sha16"], make_voice.digest_of(
            os.path.join(tmp, "Serena", "take1.wav"))[:16])
        self.assertGreater(t["loudness_gain_db"], 0)    # 轻的素材被抬升
        self.assertAlmostEqual(t["loudness_rms_dbfs"], -23.0, delta=0.3)
        self.assertEqual(prof["chosen"], [1])           # 人耳定稿结论不动
        # 二次回填幂等：已在目标 → 全部跳过
        rc = make_voice.normalize_official_archive(tmp, log=lambda *_: None)
        self.assertEqual(rc, 0)

    def test_design_and_record_call_normalize(self):
        mv_src = _read(os.path.join(_SERVICE, "make_voice.py"))
        design_seg = mv_src.split("def design(")[1].split("\ndef ")[0]
        self.assertIn("normalize_wav_bytes", design_seg)
        rec_src = _read(os.path.join(_ROOT, "tts_service",
                                     "record_official_presets.py"))
        self.assertIn("make_voice.normalize_wav_bytes", rec_src)
        self.assertIn("make_voice.normalize_official_archive", rec_src)


class TestStdoutContract(unittest.TestCase):
    """stdout 是结果通道：库的杂行不许垫进结果 JSON（实测炸过的契约）。

    2026-10-06 用户实测：VoiceDesign 造嗓成功落盘，但 faster-qwen3-tts 的
    CUDA 图预热用裸 print 直写 stdout，六行 Warming/Captured 垫在结果 JSON
    前面，父进程 json.loads 当场炸成「没返回可解析的结果」。两层防御：
    serve 层把库输出赶到 stderr（_stdout_to_stderr），父进程侧 _cli_json
    抠最外层对象兜底。
    """

    def test_cli_json_extracts_from_polluted_stdout(self):
        polluted = ("\r\nWarming up predictor (3 runs)...\r\n"
                    "Capturing CUDA graph for predictor...\r\n"
                    "CUDA graph captured!\r\n"
                    'Warming up talker graph (3 runs)...\r\n'
                    '{"result": {"role": "_draft"}, "status": {}}\r\n')
        data = web_ui._cli_json(polluted)
        self.assertEqual(data["result"]["role"], "_draft")

    def test_cli_json_survives_braces_inside_values(self):
        obj = {"error": "描述里有花括号 { 和 } 也要能解析"}
        polluted = "某行杂音\n" + json.dumps(obj, ensure_ascii=False) + "\n"
        self.assertEqual(web_ui._cli_json(polluted)["error"], obj["error"])

    def test_cli_json_returns_none_fail_closed(self):
        self.assertIsNone(web_ui._cli_json(""))
        self.assertIsNone(web_ui._cli_json("压根没有 JSON"))
        self.assertIsNone(web_ui._cli_json('{"error": "被截断的'))

    def test_stdout_to_stderr_redirects_lib_prints(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with serve._stdout_to_stderr():
                print("Warming up predictor (3 runs)...")
                print("CUDA graph captured!")
        self.assertIn("Warming up predictor", buf.getvalue())
        self.assertIn("CUDA graph captured", buf.getvalue())

    def test_serve_wraps_all_lib_call_sites(self):
        """四块调库区全部在结果通道纪律之下：加载/预热、克隆、设计、内置。

        少一处，那条路第一次跑就会把库的 stdout 杂行垫进结果 JSON。
        """
        src = _read(os.path.join(_SERVICE, "serve.py"))
        self.assertEqual(src.count("with _stdout_to_stderr():"), 5)
        # _cli_error 也走 _cli_json（错误提取同样会被污染坑）
        err_seg = _read(os.path.join(_ROOT, "podcast_maker",
                                     "web_ui.py")).split("def _cli_error")[1]
        self.assertIn("_cli_json(c)", err_seg)


if __name__ == "__main__":
    unittest.main()
