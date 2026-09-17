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

"""多后端 LLM 客户端（仅标准库 urllib）。

后端：LM Studio / Ollama / 任意 OpenAI 兼容 API。
行为准则（fail-closed）：任何网络或协议异常都抛出 LLMError，
不返回空串、不静默兜底、不假功能。

**兼容两种模型，不替使用者决定**：推理型模型与普通模型都要能跑通。所以
这里既不主动要求"别思考"（那是使用者的选择），也不假设答案只在 `content`
的第一个花括号之后——思考与答案混在一段里的后端同样要接得住。

约束解码：优先请求 response_format=json_schema，后端明确拒绝这个参数时降级
为纯提示词约束，并在返回值里显式标注 degraded。**只有"请求被拒"才降级**：
超时、5xx、返回非 JSON 都是后端自己出故障，摘字段治不了它。
注意：门禁校验逻辑不依赖约束解码是否生效。

**超时判"卡死"，不判"慢"**：对话一律走流式，一字一收，收到就重置静默计时。
两道闸门分工不同——`idle_timeout` 判"多久没有下一个字"（后端真出故障），
`timeout` 判"整件事最久允许多久"（模型写得太久，该调大就调大）。
从前只有一个总时长上限、且回复要等全篇才到，于是"模型在慢慢写"和"后端死了"
被同一个数判死：写一期长稿必然撞墙，而日志上写着"后端响应超时"。
"""

import json
import time
import urllib.error
import urllib.request

from .config_manager import MODE_SPEC


class LLMError(RuntimeError):
    """LLM 调用失败。绝不被静默吞掉。

    `status` 是后端回的 HTTP 码（拿不到时为 None）。**"请求被拒"与"后端自己
    出故障"必须分得开**：前者可以摘掉可选字段重试，后者摘什么都没用，还会
    在日志上留下一个假结论、把真实起因丢掉。
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


#: 思考段与它的闭合标记。有些后端把思考放进单独的 `reasoning_content`，
#: 有些直接混在 `content` 里——后者不做处理，找 JSON 时「第一个花括号」可能
#: 落在思考里，答案本身没问题却被判成解析失败。
_THINK_MARKS = ((" thinking", "<｜end▁of▁thinking｜>"),
                ("<thinking>", "</thinking>"),
                ("<|thinking|>", "<|end_thinking|>"))


def strip_thinking(text):
    """去掉成对的思考段。**没闭合的不动**——分不清哪段是思考，硬剥只会砍掉答案。"""
    out = text or ""
    for a, b in _THINK_MARKS:
        while True:
            i = out.lower().find(a)
            if i == -1:
                break
            j = out.lower().find(b, i + len(a))
            if j == -1:
                break
            out = out[:i] + out[j + len(b):]
    return out


def extract_json(text):
    """从模型输出里取 JSON 对象；取不到返回 None。

    两处不按常理出牌，都是被后端逼的：

    - **不用「第一个 `{` 到最后一个 `}`」**：思考里只要出现过一次花括号
      （模型在思考里写模板、举例），那样截出来的是「半截思考 + 答案」，
      `json.loads` 必失败——而失败信息只会说"JSON 无法解析"，把人引向错的方向。
      改为逐个花括号试解析。
    - **取最后一个解析成功的**，不是第一个：没带思考标记的后端，模型是
      「先想（草稿）后答」，草稿里往往就有一份格式一样的 JSON。取第一个会
      把草稿当答案交上去——它看着完全合法，只是内容不是最终那份。
    """
    t = strip_thinking(text)
    dec = json.JSONDecoder()
    best = None
    i = t.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(t[i:])
        except ValueError:
            i = t.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            best = obj
            # 跳过刚解析掉的一整段：否则嵌套对象会被当成"下一个候选"，
            # 而取最后一个就会把内层那个交出去。
            i = t.find("{", i + end)
        else:
            i = t.find("{", i + 1)
    return best


#: 输入额度里留给「估算误差」的余量。折合比是按历史用量取的中位数，单次内容
#: 不同会有波动——真模型实测里同一份素材的两次调用，比值就差约 5%（0.55 与
#: 0.58）。余量按额度留 8%：留 5% 时实测已把余量吃到只剩 3%，估算再偏一点
#: 就顶爆后端；少喂几千字素材的代价，远小于把请求顶爆。
INPUT_RESERVE_FRAC = 0.08
INPUT_RESERVE_MIN = 1024


class LLMClient:
    def __init__(self, backend="lm-studio", base_url="", api_key="", model="",
                 timeout=3600, idle_timeout=300):
        self.backend = backend or "lm-studio"
        opts = MODE_SPEC["llm.backend"]["options"].get(self.backend, {})
        self.base_url = (base_url or opts.get("default_base_url", "")).rstrip("/")
        self.api_key = api_key or "not-needed"
        self.model = model or ""
        # 总时限：整次生成的硬天花板。它只防"永远吐不完"，不参与判故障。
        self.timeout = int(timeout or 3600)
        # 静默时限：多久没有下一个字就判后端卡死。本地模型预填充长提示词时
        # 会安静好几分钟才吐第一个字，这个数要按"预填充最长能有多久"来定，
        # 不是按"答一句话要多久"。
        self.idle_timeout = int(idle_timeout or 300)
        # 输入额度不再由本类推算（从前的「最大输出 × 输入倍率」参照系挂错了——
        # 素材容量的参照系是**成稿脚本**：1:x 的 1 是撑满每期时长的脚本，x 是
        # 它能消化的素材倍数，容量由业务线按 probe.capacity 推导后传进来）。
        # 本类只做两件事：把素材字数折成 token（含估算余量），把窗口要求换算
        # 成具体数字提醒使用者自查。不猜、不探、没有兜底值。
        # 每字符折合多少 token。首次调用前没有样本，取最保守的 1.0；之后由后端
        # 回传的 prompt_tokens 反标（见 `_note_usage`）——比任何经验常数都准。
        self._ratio_samples = []

    # ------------------------------------------------------------------ 基础
    def _endpoint(self, path):
        if not self.base_url:
            raise LLMError("未配置 LLM 地址（llm.base_url 为空）。")
        base = self.base_url
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + path

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "not-needed":
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def _request(self, method, url, payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(),
                                     method=method)
        streaming = bool(payload) and bool(payload.get("stream"))
        try:
            # socket 超时一律按"静默时限"设：readline 只在"这么久没有下一个字节"
            # 时抛，后端还在吐字就永远不算超时。整次生成的天花板由下面循环里的
            # 总时限管——socket 这一个数管不了"总量"，只能管"多久没动静"。
            # 非流式的请求（如取模型列表）没有循环，于是只受静默时限约束：它
            # 本来就给不出"还在干活"的证据，等超过一个静默时限毫无意义。
            with urllib.request.urlopen(req, timeout=self.idle_timeout) as resp:
                ctype = resp.headers.get("Content-Type") or ""
                if streaming and "event-stream" in ctype.lower():
                    return self._read_sse(resp)
                # 后端认了这个请求却没按流式回（规范允许服务端自行决定分块）。
                # 整段读走原来那条路，结果形状与流式拼出来的完全一致。
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:                                    # noqa: BLE001
                pass
            raise LLMError("后端返回 HTTP %s：%s" % (e.code, detail),
                           status=e.code)
        except urllib.error.URLError as e:
            raise self._connect_error(e.reason, streaming)
        except TimeoutError:
            raise self._idle_error(0, streaming)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise LLMError("后端返回的不是合法 JSON：%s" % body[:200])

    # -------------------------------------------------------------- 流式读
    def _read_sse(self, resp):
        """收一段 SSE 流，拼成与"整段回复"同形的 dict。

        拼成同一形状是刻意的：`chat()` 不必知道这一次是流式还是整段，
        两种后端在它眼里长得一样，往下游去的判据也就只有一套。

        每收到一行就把静默计时清零（socket 超时天然按"两次读之间"计时），
        所以只有"一个字都不再来"才会抛超时。
        """
        started = time.monotonic()
        content, reasoning = [], []
        got = 0                     # 累计字数。逐行现算等于每收一个字就把前面重数一遍
        finish = ""
        usage = {}
        closed = False
        while True:
            try:
                raw = resp.readline()
            except TimeoutError:
                raise self._idle_error(got, True)
            if not raw:
                break
            if time.monotonic() - started > self.timeout:
                raise LLMError(
                    "本次生成已超过总时限 %d 秒（已收到 %d 字），由客户端主动停下。"
                    "后端一直在吐字，说明它不是卡死、只是写得久——要让它写完，"
                    "把「总时限」调大；要它写得快些，换更小的模型或拆段生成。"
                    % (self.timeout, got))
            line = raw.decode("utf-8", "replace").strip()
            # 空行是事件分隔，`:` 开头是注释（有的后端拿它当心跳）。两者都算
            # "后端还活着"的证据，但都不是正文。
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                closed = True
                break
            if not body:
                continue
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                raise LLMError(
                    "流式响应里有一块不是合法 JSON：%s。"
                    "静默跳过它等于在稿子里留个洞，不如就此停下。" % body[:200])
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0] or {}
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
                closed = True
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
                got += len(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
                got += len(d["reasoning_content"])
        if not closed:
            raise LLMError(
                "流式响应在中途断了：收到 %d 字，却没有结束标记（[DONE] 或 "
                "finish_reason）。半截的稿子不能当成品用。" % got)
        msg = {"content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        return {"choices": [{"message": msg, "finish_reason": finish}],
                "usage": usage}

    def _connect_error(self, reason, streaming):
        """连不上与"连上了但对面不吭声"要分开。

        `URLError(reason=timeout)` 意思是连接建立了、一直没数据，那属于静默
        超时；DNS 解析不了、连接被拒才是真的连不上。把这两件事写成同一句话，
        排查时就得靠猜。
        """
        if isinstance(reason, TimeoutError):
            return self._idle_error(0, streaming)
        return LLMError("无法连接后端 %s：%s" % (self.base_url, reason))

    def _idle_error(self, got, streaming):
        if not streaming:
            return LLMError(
                "后端 %d 秒没有回应（这个请求没走流式，只能整段等）。"
                % self.idle_timeout)
        return LLMError(
            "后端 %d 秒没有输出任何内容，判定为卡死（此前已收到 %d 字）。"
            "本地模型遇到长提示词时，预填充本身可能就要几分钟——若确认它其实"
            "还在算，把这个「静默时限」调大。"
            % (self.idle_timeout, got))

    # ------------------------------------------------------------------ 预算
    def material_tokens(self, material_chars):
        """素材字数折成 token（含估算余量）。

        字数由业务线传入（probe.capacity × 体检上限，参照系是成稿脚本），
        本类不推算容量——只负责折算。折合比有波动，余量吸收它（见
        `INPUT_RESERVE_FRAC`）。
        """
        chars = max(0, int(material_chars or 0))
        if chars <= 0:
            return 0
        need = int(chars * self.tokens_per_char())
        return need + self._input_reserve(need)

    def required_context(self, max_tokens, material_chars=0):
        """这套分配要求后端窗口至少多大（token）：输出预算 + 素材需求。"""
        out = int(max_tokens or 0)
        return out + self.material_tokens(material_chars)

    def _input_reserve(self, need):
        """素材折算里留给「估算误差」的部分：max(1024, 需求 × 8%)。"""
        return max(INPUT_RESERVE_MIN, int(need * INPUT_RESERVE_FRAC))

    def quota_note(self, max_tokens, material_chars=0):
        """把素材需求与窗口要求换算成具体数字，任务开始时落进日志。

        参照系是**成稿脚本**：素材容量（字）由业务线按「成稿 × 档位 × 1.25」
        推导后传入，这里不再出现「最大输出的几倍」——那套参照系从来就不该
        存在。后端窗口是加载时才定下的数，程序不替使用者判断够不够——但要求
        多少必须摆到台面上，而不是等后端爆出一句看不懂的错。
        """
        out = int(max_tokens or 0)
        mt = self.material_tokens(material_chars)
        return ("本期素材容量 %d 字，折合输入约 %d token（含估算余量）；"
                "输出预算 %d token。请保证后端上下文窗口 ≥ %d"
                "（= 输出 %d + 输入 %d）。"
                % (int(material_chars or 0), mt, out,
                   self.required_context(out, material_chars), out, mt))

    def tokens_per_char(self):
        """每字符折合多少 token。有样本取中位数，没有则 1.0（最保守）。

        不硬编码 0.75 这类经验值：中文、数字、标点、代码块的折合比差得远，
        素材里多几段数字就能让估算偏两成。而后端**每次都会回传真实的
        prompt_tokens**，拿它反标出来的比值比任何经验值都贴近这台机器。
        """
        if not self._ratio_samples:
            return 1.0
        s = sorted(self._ratio_samples)
        return s[len(s) // 2]

    def _note_usage(self, prompt_chars, usage):
        """记一次成功调用的折合比。失败的那次不记：它可能压根没进模型。"""
        try:
            pt = int((usage or {}).get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            return
        if pt <= 0 or prompt_chars <= 0:
            return
        self._ratio_samples.append(pt / float(prompt_chars))
        if len(self._ratio_samples) > 20:
            del self._ratio_samples[0]

    # ------------------------------------------------------------------ 能力
    def list_models(self):
        """返回 (ok, models_or_message)。"""
        try:
            data = self._request("GET", self._endpoint("/models"))
        except LLMError as e:
            return False, str(e)
        items = data.get("data") or data.get("models") or []
        names = []
        for it in items:
            if isinstance(it, dict):
                names.append(it.get("id") or it.get("name") or "")
            elif isinstance(it, str):
                names.append(it)
        names = [n for n in names if n]
        return True, names

    def test_connection(self):
        ok, res = self.list_models()
        if not ok:
            return False, res
        if not res:
            return True, "连接成功，但后端未返回模型列表。"
        return True, "连接成功，可用模型 %d 个：%s" % (
            len(res), "、".join(res[:8]) + ("…" if len(res) > 8 else ""))

    # ------------------------------------------------------------------ 调用
    def chat(self, messages, temperature=0.8, max_tokens=8192, json_schema=None,
             reasoning_effort=None):
        """发起一次对话。返回 (text, meta)。

        `reasoning_effort` 只是个透传口——**要不要思考由使用者决定，这里不替
        他决定**。用推理型模型还是普通模型都该跑得通：后端明确拒绝这个参数时
        摘掉它重试并标 `meta.reasoning_dropped`。

        json_schema 非空时尝试约束解码；后端明确拒绝则降级重试，`meta.degraded`
        标注。可选字段**分开摘、不连坐**：为了摘掉一个而把另一个也牺牲掉，
        等于白丢一项能力还不吭声。摘除次序是"辅助字段先摘、约束解码最后"——
        `response_format` 是三者里最值钱的，不能因为后端不认 `stream_options`
        就顺手把它也丢了。

        返回的 `text` 已经**剥掉成对的思考段**（思考与答案混在 `content` 里的
        后端）；`meta.thinking_stripped` 记着这件事。
        """
        if not self.model:
            raise LLMError("未配置模型名（llm.model 为空）。")
        # 这次提示词一共多少字。后端回的 prompt_tokens 拿它当分母，反标出
        # 「这台机器上每字折合多少 token」——输入预算就靠这个比值换算。
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            # 一律流式。超时要能分清"卡死"和"慢"，就得知道后端还在不在吐字；
            # 整段等的时候这两件事长得一模一样，只能拿总时长一起判死。
            "stream": True,
            # 流式下 usage 默认不下发，显式要一次才拿得回 reasoning_tokens——
            # 推理型模型每轮都有一笔固定开销，那笔账得看得见。
            "stream_options": {"include_usage": True},
        }
        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "podcast_script", "strict": False,
                                "schema": json_schema},
            }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        # 可选字段逐个试摘：后端换了、版本升了、参数表收紧了，都表现为
        # 「这次请求被拒」。每次只摘一个，摘掉哪个就标哪个。
        degraded = dropped = usage_off = False
        optional = [k for k in ("reasoning_effort", "stream_options",
                                "response_format") if k in payload]
        while True:
            try:
                data = self._request("POST", self._endpoint("/chat/completions"),
                                     payload)
                break
            except LLMError as e:
                # **只有"请求本身被拒"才降级**。超时、5xx、连不上、返回非
                # JSON 都是后端出故障——摘字段治不了它，而把它当成「这个字段
                # 后端不认」会在日志上留下一个假结论，真实起因被丢掉。
                if not optional or e.status not in (400, 422):
                    raise
                key = optional.pop(0)
                payload.pop(key, None)
                if key == "reasoning_effort":
                    dropped = True
                elif key == "stream_options":
                    usage_off = True
                else:
                    degraded = True

        try:
            choice = data["choices"][0]
            msg = choice["message"] or {}
            raw_text = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("后端响应缺少 choices[0].message.content：%s"
                           % json.dumps(data, ensure_ascii=False)[:300])

        # 思考与答案混在同一字段里的后端：剥掉成对的思考段再交出去，否则
        # 下游找 JSON 时「第一个花括号」落在思考里，好答案被判成解析失败。
        text = strip_thinking(raw_text)
        thinking_stripped = text != raw_text

        finish = choice.get("finish_reason", "")
        usage = data.get("usage", {}) or {}
        r_tokens = int((usage.get("completion_tokens_details") or {})
                       .get("reasoning_tokens", 0) or 0)

        # 空答案必须在此处被翻译成可诊断的原因。放到下游只会变成
        # "JSON 解析失败"或"门禁未通过"，掩盖真正的起因。
        if not text.strip():
            if raw_text.strip():
                raise LLMError(
                    "模型返回的内容全在思考段里，剥掉之后没有答案：思考段把这一轮"
                    "的输出预算（max_tokens=%d）用完了，答案段没剩额度。思考段与"
                    "答案段共用同一份预算，提高 llm.max_tokens 即可；或在提示词里"
                    "把输出位置写得更死。" % int(max_tokens))
            raise LLMError(self._empty_reason(finish, reasoning, r_tokens, max_tokens))

        self._note_usage(prompt_chars, usage)

        meta = {
            # 模型名要回给调用方：探查结果里记着「这次判定用的是哪个模型」，
            # 同一份素材两次结论不同时，得先能分清是换了模型还是模型不稳。
            "model": self.model,
            "degraded": degraded,
            "reasoning_dropped": dropped,
            # 后端不吃 stream_options 时，usage 会整块是空的——这时
            # reasoning_tokens 的 0 是"没拿到"而不是"没花"，得说清楚，
            # 否则一个假零会被当成真账。
            "usage_dropped": usage_off,
            "thinking_stripped": thinking_stripped,
            "finish_reason": finish,
            "reasoning_tokens": r_tokens,
            "usage": usage,
        }
        return text, meta

    @staticmethod
    def _empty_reason(finish, reasoning, r_tokens, max_tokens):
        """把"空答案"翻译成人能据此行动的原因。"""
        if reasoning or r_tokens:
            return ("模型返回空答案：输出预算被思考段用完"
                    "（reasoning_tokens=%d，max_tokens=%d），答案段没有剩余额度。"
                    "思考段与答案段共用同一份输出预算，与模型类型无关；"
                    "把 llm.max_tokens 提到远高于这个点位的思考所需即可。"
                    % (r_tokens, max_tokens))
        if finish == "length":
            return ("模型返回空答案且因长度截断（finish_reason=length，"
                    "max_tokens=%d）。请提高 llm.max_tokens。" % max_tokens)
        return "后端返回空内容（finish_reason=%s）。" % (finish or "未知")
