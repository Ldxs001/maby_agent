"""LLM 统一客户端 — LM Studio / Ollama / 自定义 OpenAI 兼容 API

仅使用 Python 标准库（urllib），无第三方 SDK 依赖。
统一接口：chat / chat_detailed / test_connection / list_models
"""
import json
import time
import urllib.request
import urllib.error
import threading


class LLMClientError(Exception):
    pass


class LLMClient:
    """多后端 LLM 客户端

    backend:
      - "lm-studio": OpenAI 兼容，默认 http://localhost:1234/v1
      - "ollama":    原生 /api/chat，默认 http://localhost:11434
      - "custom":    任意 OpenAI 兼容 API
    """

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

    def _build_payload(self, messages, max_tokens, temperature):
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
            return {
                "model": model_name or "qwen2.5",
                "messages": messages,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
            }
        else:
            payload = {
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if model_name:
                payload["model"] = model_name
            return payload

    def _parse_content(self, body: bytes) -> str:
        data = json.loads(body)
        if self.backend == "ollama":
            return data.get("message", {}).get("content", "")
        else:
            return data["choices"][0]["message"]["content"]

    def _parse_finish_reason(self, body: bytes) -> str:
        try:
            data = json.loads(body)
            if self.backend == "ollama":
                return data.get("done_reason", "stop")
            else:
                return data["choices"][0].get("finish_reason", "stop")
        except Exception:
            return "stop"

    def chat(self, messages, max_tokens=None, temperature=None,
             timeout=None) -> str:
        url = self._chat_url()
        payload = json.dumps(self._build_payload(messages, max_tokens, temperature)).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
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

    def chat_detailed(self, messages, max_tokens=None, temperature=None,
                      timeout=None) -> dict:
        url = self._chat_url()
        payload = json.dumps(self._build_payload(messages, max_tokens, temperature)).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(), method="POST")
        try:
            t = timeout or self.timeout
            with urllib.request.urlopen(req, timeout=t) as resp:
                body = resp.read()
                return {
                    "content": self._parse_content(body),
                    "finish_reason": self._parse_finish_reason(body),
                }
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise LLMClientError(f"HTTP {e.code}: {err[:300]}")
        except urllib.error.URLError as e:
            raise LLMClientError(f"连接失败: {e.reason}")
        except Exception as e:
            raise LLMClientError(f"LLM 调用异常: {e}")

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