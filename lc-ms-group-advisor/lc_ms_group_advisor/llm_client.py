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

"""LLM 统一客户端 — LM Studio / Ollama / 自定义 OpenAI 兼容 API

仅使用 Python 标准库（urllib），无第三方 SDK 依赖。
复用 silprespec-emulator 的 llm_client 设计。
"""
import json
import time
import urllib.request
import urllib.error
import threading


class LLMClientError(Exception):
    pass


class LLMClient:

    _MODELS_CACHE: dict = {}
    _MODELS_CACHE_TTL = 30.0
    _cache_lock = threading.Lock()

    def __init__(self, backend="lm-studio", base_url="http://localhost:1234",
                 model="", api_key="not-needed", timeout=180,
                 max_tokens=4096, temperature=0.7):
        self.backend = backend
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _chat_url(self) -> str:
        if self.backend == "ollama":
            return f"{self.base_url}/api/chat"
        else:
            base = self.base_url
            if not base.endswith("/v1"):
                base = base + "/v1"
            return f"{base}/chat/completions"

    def _models_url(self) -> str:
        if self.backend == "ollama":
            return f"{self.base_url}/api/tags"
        else:
            base = self.base_url
            if not base.endswith("/v1"):
                base = base + "/v1"
            return f"{base}/models"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "not-needed":
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _build_payload(self, messages, max_tokens, temperature, response_format=None):
        if temperature is None:
            temperature = self.temperature
        if max_tokens is None:
            max_tokens = self.max_tokens

        model_name = self.model
        if not model_name:
            try:
                models = self.list_models()
                if models:
                    model_name = models[0]
            except Exception:
                pass

        if self.backend == "ollama":
            payload = {
                "model": model_name or "qwen2.5",
                "messages": messages,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
            }
            if response_format is not None:
                # Ollama 用顶层 format 字段接收 JSON schema
                payload["format"] = response_format
            return payload
        else:
            payload = {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if model_name:
                payload["model"] = model_name
            if response_format is not None:
                # OpenAI 兼容后端（LM Studio / vLLM / llama.cpp server）走 response_format
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "extraction", "strict": True,
                                    "schema": response_format},
                }
            return payload

    def _parse_content(self, body: bytes) -> str:
        data = json.loads(body)
        if self.backend == "ollama":
            return data.get("message", {}).get("content", "")
        else:
            return data["choices"][0]["message"]["content"]

    def _post(self, url, payload, timeout) -> str:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        try:
            t = timeout or self.timeout
            with urllib.request.urlopen(req, timeout=t) as resp:
                return self._parse_content(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise LLMClientError(f"HTTP {e.code}: {err[:300]}")
        except urllib.error.URLError as e:
            raise LLMClientError(f"连接失败: {e.reason}")
        except Exception as e:
            raise LLMClientError(f"LLM 调用异常: {e}")

    def chat(self, messages, max_tokens=None, temperature=None, timeout=None,
             response_format=None) -> str:
        """发一次对话请求。

        response_format: 可选，json_schema 本体（dict）。传入时启用「约束解码」，
        让模型在采样阶段就无法输出协议外的形状——
          Ollama          → 顶层 format 字段
          OpenAI 兼容后端 → response_format.json_schema（strict）
        后端不认该参数时自动降级重试一次（不带约束）。**调用方的校验逻辑不依赖它**，
        约束解码是增强手段而非前提。
        """
        url = self._chat_url()
        try:
            return self._post(url, self._build_payload(messages, max_tokens, temperature,
                                                       response_format), timeout)
        except LLMClientError as e:
            if response_format is not None and "HTTP 4" in str(e):
                return self._post(url, self._build_payload(messages, max_tokens,
                                                           temperature, None), timeout)
            raise

    def test_connection(self) -> tuple:
        try:
            models = self.list_models()
            if models:
                return True, f"已连接，可用模型：{', '.join(models[:8])}"
            return True, "连接成功（未返回模型列表）"
        except Exception as e:
            return False, f"连接失败：{e}"

    def list_models(self) -> list:
        cache_key = f"{self.backend}|{self.base_url}"
        _now = time.monotonic()
        with self._cache_lock:
            cached = self._MODELS_CACHE.get(cache_key)
            if cached and (_now - cached[0]) < self._MODELS_CACHE_TTL:
                return cached[1]
        try:
            req = urllib.request.Request(self._models_url(), headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if self.backend == "ollama":
                    result = [m["name"] for m in data.get("models", [])]
                else:
                    result = [m.get("id") or m.get("name", "") for m in data.get("data", [])]
        except Exception:
            result = []
        with self._cache_lock:
            self._MODELS_CACHE[cache_key] = (_now, result)
        return result