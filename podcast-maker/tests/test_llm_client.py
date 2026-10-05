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

"""LLM 客户端：meta 是调用方判断依据的来源；兼容两种模型是客户端的责任。

meta 里少一个字段，下游读到的就是 None，而这种缺失不会报错——探查处
「记下判定用的模型」写了一个 `meta.get("model")`，客户端从来没给过这个键，
于是那行代码永远是空转，看起来该记的都记了。

**用推理型还是普通模型由使用者自己定**：客户端要保证两种都跑得通，而不是替
使用者决定要不要思考。所以这里的判据都盯着「兼容」：字段被拒能不能退、后端
自己出故障会不会被误判成"字段不认"、思考混在答案里会不会把好答案读成坏的。
"""

import json
import os
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from podcast_maker.llm_client import (INPUT_RESERVE_MIN, LLMClient,        # noqa: E402
                                      LLMError, extract_json, strip_thinking)


def fake_reply(content="好", finish="stop", usage=None, reasoning=None):
    msg = {"content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"choices": [{"message": msg, "finish_reason": finish}],
            "usage": usage or {}}


def client(model="qwen3-8b"):
    return LLMClient(backend="lm-studio", base_url="http://127.0.0.1:1234/v1",
                     model=model)


# ---------------------------------------------------------------- 流式桩
# 门禁的道理要能被验证，不能只在真后端上试。下面这个桩按脚本吐响应，
# 专门用来盯住流式那几条路：正常收完、中途静默、总时限、后端不吃流式、
# 流被掐断。
@contextmanager
def stub_backend(speak):
    """起一个只服务一个接口的桩。`speak(handler)` 拿 handler 自己写响应。"""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):                                   # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            try:
                speak(self)
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError):
                pass                                    # 客户端先走一步，正常

        def log_message(self, *a):                          # noqa: A003
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def sse_start(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.end_headers()


def sse_chunk(h, **delta):
    h.wfile.write(("data: %s\n\n" % json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        ensure_ascii=False)).encode("utf-8"))
    h.wfile.flush()


def sse_end(h, finish="stop", usage=None):
    h.wfile.write(("data: %s\n\n" % json.dumps(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
         "usage": usage or {}}, ensure_ascii=False)).encode("utf-8"))
    if usage:
        h.wfile.write(("data: %s\n\n" % json.dumps(
            {"choices": [], "usage": usage}, ensure_ascii=False)).encode("utf-8"))
    h.wfile.write(b"data: [DONE]\n\n")
    h.wfile.flush()


class TestStreamingTransport(unittest.TestCase):
    """超时判「卡死」不判「慢」。

    从前回复要整段等，后端在慢慢写和已经死了长得一模一样，于是只有一个总时长
    上限可用，写一期长稿必然撞墙、日志上却写着「后端响应超时」。改成流式以后
    两件事才分得开：静默多久算死、整件事最久允许多久，各管各的。
    """

    def c(self, base, **kw):
        return LLMClient(backend="lm-studio", base_url=base, model="stub", **kw)

    def test_stream_flag_and_usage_request_go_out(self):
        """两件事都得在请求体里看得见。

        `stream` 是这条路的全部理由——不流式就分不清卡死和慢；`stream_options`
        是为把 reasoning_tokens 要回来，推理型模型每轮的开销得看得见。
        """
        c = self.c("http://127.0.0.1:1")
        payloads = []
        c._request = lambda m, u, p=None: (payloads.append(dict(p or {})),
                                           fake_reply())[1]
        c.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(payloads[0]["stream"])
        self.assertEqual(payloads[0]["stream_options"], {"include_usage": True})

    def test_chunks_are_assembled_into_one_reply(self):
        def speak(h):
            sse_start(h)
            for t in ("今天", "聊一个", "话题。"):
                sse_chunk(h, content=t)
            sse_chunk(h, reasoning_content="先想想")
            sse_end(h, usage={"completion_tokens_details": {"reasoning_tokens": 12}})

        with stub_backend(speak) as base:
            text, meta = self.c(base).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "今天聊一个话题。")
        self.assertEqual(meta["finish_reason"], "stop")
        self.assertEqual(meta["reasoning_tokens"], 12)
        self.assertFalse(meta["usage_dropped"])

    def test_keepalive_comments_are_not_content(self):
        # 有的后端拿 `: ping` 当心跳。它是"还活着"的证据，不是正文。
        def speak(h):
            sse_start(h)
            h.wfile.write(b": ping\n\n")
            h.wfile.flush()
            sse_chunk(h, content="正文")
            sse_end(h)

        with stub_backend(speak) as base:
            text, _meta = self.c(base).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "正文")

    def test_silence_is_judged_as_a_stall_not_a_slow_writer(self):
        def speak(h):
            sse_start(h)
            sse_chunk(h, content="起了个头")
            time.sleep(3)                     # 然后一声不吭

        with stub_backend(speak) as base:
            with self.assertRaises(LLMError) as cm:
                self.c(base, idle_timeout=1).chat([{"role": "user", "content": "hi"}])
        msg = str(cm.exception)
        self.assertIn("卡死", msg)
        self.assertIn("4 字", msg)            # 报出已经收到多少，便于分辨是没开始还是断在中途

    def test_total_ceiling_stops_a_healthy_stream(self):
        def speak(h):
            sse_start(h)
            for i in range(40):
                sse_chunk(h, content="字")
                time.sleep(0.1)

        with stub_backend(speak) as base:
            with self.assertRaises(LLMError) as cm:
                self.c(base, timeout=1, idle_timeout=30).chat(
                    [{"role": "user", "content": "hi"}])
        self.assertIn("总时限", str(cm.exception))

    def test_backend_that_ignores_stream_still_works(self):
        # 规范允许服务端自己决定要不要分块。它整段回，客户端就得照整段收，
        # 而不是把一份完好的答案判成故障。
        def speak(h):
            body = json.dumps(fake_reply(content="整段回来的"), ensure_ascii=False)
            raw = body.encode("utf-8")
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(raw)))
            h.end_headers()
            h.wfile.write(raw)

        with stub_backend(speak) as base:
            text, _meta = self.c(base).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "整段回来的")

    def test_cut_stream_is_refused(self):
        # 没有结束标记就断：半截的稿子不能当成品交出去，否则错误会推迟到
        # 「JSON 解析失败」那一步，起因被埋掉。
        def speak(h):
            sse_start(h)
            sse_chunk(h, content='{"lines":[')
            h.close_connection = True

        with stub_backend(speak) as base:
            with self.assertRaises(LLMError) as cm:
                self.c(base).chat([{"role": "user", "content": "hi"}])
        self.assertIn("中途断了", str(cm.exception))

    def test_garbage_chunk_is_refused(self):
        def speak(h):
            sse_start(h)
            h.wfile.write("data: {这不是 JSON\n\n".encode("utf-8"))
            h.wfile.flush()

        with stub_backend(speak) as base:
            with self.assertRaises(LLMError) as cm:
                self.c(base).chat([{"role": "user", "content": "hi"}])
        self.assertIn("不是合法 JSON", str(cm.exception))


class TestMeta(unittest.TestCase):
    def test_meta_carries_model_name(self):
        c = client()
        c._request = lambda *a, **k: fake_reply()
        _text, meta = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(meta["model"], "qwen3-8b")

    def test_meta_carries_the_fields_callers_read(self):
        c = client()
        c._request = lambda *a, **k: fake_reply(usage={"total_tokens": 7})
        _text, meta = c.chat([{"role": "user", "content": "hi"}])
        for k in ("model", "degraded", "reasoning_dropped", "thinking_stripped",
                  "finish_reason", "reasoning_tokens", "usage"):
            self.assertIn(k, meta)

    def test_blank_model_is_refused(self):
        c = client(model="")
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "hi"}])


class TestDegrade(unittest.TestCase):
    """只有「请求本身被拒」才摘掉可选字段重试。

    守住这条判据，是因为另一条路看着更省事却会撒谎：后端超时、5xx、返回
    非 JSON 也会抛同一个 LLMError，早先的实现遇见它就摘字段重试，并在 meta
    上记一个 degraded——日志里写着「后端不接受约束解码」，真实原因是后端
    刚才抖了一下；而首次那个错误（带 detail 的那个）被丢掉了。
    """

    def test_constraint_drop_is_visible(self):
        c = client()
        payloads = []

        def boom(method, url, payload=None):
            payloads.append(dict(payload or {}))
            # 前两次被拒先摘掉 stream_options、再摘 response_format
            # （辅助字段排在约束解码前面），第三次才放行。
            if len(payloads) < 3:
                raise LLMError("后端不认这次请求", status=400)
            return fake_reply()

        c._request = boom
        _t, meta = c.chat([{"role": "user", "content": "hi"}],
                          json_schema={"a": 1})
        self.assertTrue(meta["degraded"])
        self.assertNotIn("response_format", payloads[-1])

    def test_dropping_usage_does_not_claim_the_schema_was_rejected(self):
        """别连坐的第二层：摘掉 stream_options 不等于「后端不支持约束解码」。

        `degraded` 会被界面读成「已降级为提示词约束」。后端只是不认一个
        usage 开关，却在日志里留下这句假话，下次排查就照着错的方向找。
        """
        c = client()
        seen = []

        def boom(method, url, payload=None):
            seen.append(dict(payload or {}))
            if len(seen) == 1:
                raise LLMError("后端不认 stream_options", status=400)
            return fake_reply()

        c._request = boom
        _t, meta = c.chat([{"role": "user", "content": "hi"}],
                          json_schema={"a": 1})
        self.assertTrue(meta["usage_dropped"])
        self.assertFalse(meta["degraded"])
        self.assertIn("response_format", seen[-1])        # 约束解码没被牵连

    def test_network_failure_drops_nothing(self):
        # 超时不是「字段不认」。这条路以前会摘掉字段重试，还在 meta 上标
        # 一个假的 degraded。
        c = client()
        seen = []

        def boom(method, url, payload=None):
            seen.append(dict(payload or {}))
            raise LLMError("后端响应超时（300 秒）。")

        c._request = boom
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "hi"}], json_schema={"a": 1})
        self.assertIn("超时", str(cm.exception))         # 原错误原样抛出
        self.assertEqual(len(seen), 1)                   # 没有重试
        self.assertIn("response_format", seen[0])        # 字段没被摘掉

    def test_5xx_is_not_a_refusal(self):
        c = client()
        seen = []

        def boom(method, url, payload=None):
            seen.append(dict(payload or {}))
            raise LLMError("后端返回 HTTP 500：内部错误", status=500)

        c._request = boom
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "hi"}],
                   json_schema={"a": 1}, reasoning_effort="none")
        self.assertEqual(len(seen), 1)

    def test_optional_fields_are_dropped_one_at_a_time(self):
        # 连坐会白丢能力：为了摘掉一个辅助开关，把约束解码也一起牺牲掉。
        # 次序是「辅助字段先摘、约束解码最后」——response_format 是三者里
        # 最值钱的，不能因为后端不认别的字段就顺手把它丢了。
        c = client()
        payloads = []

        def boom(method, url, payload=None):
            payloads.append(dict(payload or {}))
            if len(payloads) < 4:
                raise LLMError("后端不认这次请求", status=400)
            return fake_reply()

        c._request = boom
        _t, meta = c.chat([{"role": "user", "content": "hi"}],
                          json_schema={"a": 1}, reasoning_effort="none")
        self.assertEqual(len(payloads), 4)
        self.assertIn("reasoning_effort", payloads[0])
        self.assertIn("stream_options", payloads[0])
        self.assertIn("response_format", payloads[0])
        self.assertNotIn("reasoning_effort", payloads[1])
        self.assertIn("stream_options", payloads[1])      # 每次只摘一个
        self.assertIn("response_format", payloads[1])
        self.assertNotIn("stream_options", payloads[2])
        self.assertIn("response_format", payloads[2])     # 约束解码留到最后
        self.assertNotIn("response_format", payloads[3])
        self.assertTrue(meta["reasoning_dropped"])
        self.assertTrue(meta["usage_dropped"])
        self.assertTrue(meta["degraded"])

    def test_refusal_with_nothing_to_drop_still_raises(self):
        c = client()
        c._request = lambda *a, **k: (_ for _ in ()).throw(
            LLMError("后端返回 HTTP 400：bad request", status=400))
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "hi"}])


class TestReasoningEffort(unittest.TestCase):
    """思考与否是使用者的选择，这里只保证字段能透传、不主动设。

    推理型模型每次调用有一笔固定开销（实测一段两行原文默认 334 秒 /
    3997 个推理 token）。关掉它是使用者的决定，客户端不替他决定——
    但要把 `reasoning_tokens` 如实带回来，代价得看得见。
    """

    def test_reaches_the_backend_when_asked(self):
        c = client()
        seen = {}

        def grab(method, url, payload=None):
            seen.update(payload or {})
            return fake_reply()

        c._request = grab
        c.chat([{"role": "user", "content": "hi"}], reasoning_effort="none")
        self.assertEqual(seen["reasoning_effort"], "none")

    def test_absent_unless_asked(self):
        c = client()
        seen = {}

        def grab(method, url, payload=None):
            seen.update(payload or {})
            return fake_reply()

        c._request = grab
        c.chat([{"role": "user", "content": "hi"}])
        self.assertNotIn("reasoning_effort", seen)

    def test_reasoning_tokens_come_back(self):
        c = client()
        c._request = lambda *a, **k: fake_reply(
            usage={"completion_tokens_details": {"reasoning_tokens": 3997}})
        _t, meta = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(meta["reasoning_tokens"], 3997)


class TestThinkingAndJson(unittest.TestCase):
    """思考与答案混在同一字段里的后端也要接得住。

    有些后端把思考单独放进 `reasoning_content`（分得开），有些直接混在
    `content` 里。后者不处理，找 JSON 时「第一个花括号」会落在思考上——
    答案本身没问题却被判成解析失败；凝缩那一步还会因此重试两次再停下，
    而凝缩失败是设计成致命的。
    """

    def test_paired_blocks_are_stripped(self):
        self.assertEqual(strip_thinking(" thinking想一下<｜end▁of▁thinking｜>答案"), "答案")

    def test_unclosed_block_is_left_alone(self):
        # 分不清哪段是思考，硬剥只会砍掉答案
        t = " thinking想到一半"
        self.assertEqual(strip_thinking(t), t)

    def test_json_survives_braces_in_the_thinking(self):
        raw = ('让我想想 {"gist":"草稿"} 这样不对……\n'
               '{"gist":"真正的答案","points":["一"]}')
        self.assertEqual(extract_json(raw)["gist"], "真正的答案")

    def test_json_inside_a_marked_thinking_block_is_skipped(self):
        raw = ' thinking{"gist":"思考里的草稿"}<｜end▁of▁thinking｜>{"gist":"答案"}'
        self.assertEqual(extract_json(raw)["gist"], "答案")

    def test_nested_object_is_not_taken_for_the_answer(self):
        raw = '{"gist":"答案","meta":{"x":1}}'
        self.assertEqual(extract_json(raw)["gist"], "答案")

    def test_nothing_parseable_returns_none(self):
        self.assertIsNone(extract_json("这里没有 JSON"))
        self.assertIsNone(extract_json(""))

    def test_chat_strips_and_says_so(self):
        c = client()
        c._request = lambda *a, **k: fake_reply(
            content=' thinking盘算一下<｜end▁of▁thinking｜>{"gist":"答案"}')
        text, meta = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, '{"gist":"答案"}')
        self.assertTrue(meta["thinking_stripped"])

    def test_answer_only_in_thinking_is_reported(self):
        c = client()
        c._request = lambda *a, **k: fake_reply(content=" thinking只有思考<｜end▁of▁thinking｜>")
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "hi"}])
        self.assertIn("思考段", str(cm.exception))


class TestInputBudget(unittest.TestCase):
    """输入额度 = 最大输出 × 输入倍率：它答的是「这一次调用装不装得下」。

    「这一期该讲多少料」由画地图按压比定死（成稿 × 档位 × 1.25），是另一个量，
    参照系是成稿脚本、跟模型无关。两个量混成一个数，就会出现「内容明明合格、
    却因上下文放不下而降级凝缩」的错配——所以两把尺各有各的用例，谁也不替代谁。
    """

    def _client(self, ratio=1.0):
        c = LLMClient(backend="lm-studio", base_url="http://127.0.0.1:1/v1",
                      model="m", input_ratio=ratio)

        def boom(*a, **k):
            raise AssertionError("折算不该碰网络")

        c._request = boom
        return c

    def test_material_tokens_folds_chars_with_reserve(self):
        c = self._client()
        # 没有样本时折合比取最保守的 1.0：10000 字 → 10000 token
        # + max(1024, 10000×8%)=1024 余量。
        self.assertEqual(c.material_tokens(10000), 11024)
        self.assertEqual(c.material_tokens(0), 0)

    def test_learned_ratio_shrinks_the_token_demand(self):
        c = self._client()
        c._note_usage(1000, {"prompt_tokens": 750})
        self.assertAlmostEqual(c.tokens_per_char(), 0.75)
        # 10000 字 × 0.75 = 7500 token + max(1024, 7500×8%)=1024 余量。
        self.assertEqual(c.material_tokens(10000), 8524)

    def test_material_tokens_floor_at_zero(self):
        c = self._client()
        self.assertEqual(c.material_tokens(-5), 0, "负数没有任何下游读得懂")

    def test_input_budget_is_output_times_ratio(self):
        """额度就是最大输出乘倍率——这是「一次装多少」的唯一算法源。"""
        c = self._client(ratio=2.0)
        self.assertEqual(c.input_budget_tokens(47872), 95744)
        self.assertEqual(c.input_budget_tokens(0), 0)
        self.assertEqual(self._client(ratio=0.0).input_budget_tokens(999), 0)

    def test_budget_chars_discounts_overhead_and_the_reserve(self):
        """按字符切批用的额度：两端同口径折 token，折回字符时倒扣余量（保守）。"""
        c = self._client(ratio=2.0)
        # 额度 20000；占位 1000 字 → 1000 + 1024 余量 = 2024 token，剩 17976；
        # 折回字符时按 1.08 倒扣 → 16644。
        self.assertEqual(c.budget_chars(10000, other_chars=1000), 16644)

    def test_budget_chars_returns_zero_when_overhead_eats_it(self):
        """额度被占位吃光返回 0：调用方据此报「未核」，不许硬塞素材。"""
        c = self._client(ratio=1.0)
        self.assertEqual(c.budget_chars(100, other_chars=10000), 0)

    def test_quota_note_reports_what_we_feed_and_asks_nothing(self):
        """日志只报「准备喂多少」，不对后端提要求——窗口多大不归程序管。"""
        c = self._client(ratio=2.0)
        note = c.quota_note(8192, 10000)
        self.assertIn("16384", note, "输入额度（= 8192 × 2）必须在句子里")
        self.assertIn("11024", note, "素材折合输入 token 必须在句子里")
        self.assertIn("2.0", note, "倍率必须在句子里")
        self.assertIn("不丢料", note, "放不下会切成多段喂完，这句要写明")
        self.assertNotIn("窗口", note, "不许对后端窗口提要求：那是使用者自己的事")
        self.assertNotIn("19216", note, "「输出 + 输入」那个窗口要求不该再出现")

    def test_no_window_demand_is_left_in_the_client(self):
        """程序不再回答「后端窗口要多大」——这个入口已经从客户端删掉。"""
        self.assertFalse(hasattr(self._client(), "required_context"),
                         "required_context 是「算窗口要求」用的，已整条删除")

    def test_usage_without_prompt_tokens_is_ignored(self):
        c = client()
        c._note_usage(1000, {})
        c._note_usage(0, {"prompt_tokens": 500})
        self.assertEqual(c.tokens_per_char(), 1.0,
                         "后端没回用量、或分母为 0 时不能反标出假比值")


if __name__ == "__main__":
    unittest.main()
