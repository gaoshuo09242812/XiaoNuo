from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..config import ProviderSpec
from ..errors import ProviderError


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage | None


class Transport(Protocol):
    def post(self, url: str, headers: dict, body: dict, timeout: int) -> dict: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    """No automatic retries or redirects; never include response bodies in errors."""

    def __init__(self,*,no_proxy=False):
        self.no_proxy=no_proxy

    def post(self, url: str, headers: dict, body: dict, timeout: int) -> dict:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=payload, headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            handlers=[NoRedirect()]+([ProxyHandler({})] if self.no_proxy else [])
            with build_opener(*handlers).open(request, timeout=timeout) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ProviderError("PROVIDER_RESPONSE_TOO_LARGE")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ProviderError("INVALID_PROVIDER_RESPONSE")
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            raise ProviderError(f"PROVIDER_HTTP_{code}") from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise ProviderError("PROVIDER_NETWORK_ERROR") from None
        except (ValueError, UnicodeError):
            raise ProviderError("INVALID_PROVIDER_RESPONSE") from None


def parse_usage(data: dict | None, input_name: str, output_name: str, cached: int = 0) -> Usage | None:
    if not isinstance(data, dict):
        return None
    values = (data.get(input_name), data.get(output_name), cached)
    if any(type(v) is not int or v < 0 for v in values) or values[2] > values[0]:
        return None
    return Usage(*values)


def completion(text, usage) -> Completion:
    if not isinstance(text, str) or not text.strip() or len(text) > 32768:
        raise ProviderError("EMPTY_OR_INVALID_REPLY")
    return Completion(text.strip(), usage)


class LLMProvider(ABC):
    def __init__(self, spec: ProviderSpec, transport: Transport | None = None, timeout: int = 30):
        self.spec = spec
        self.transport = transport or HTTPTransport()
        self.timeout = timeout

    @abstractmethod
    def generate(self, system: str, messages: list[dict], max_output_tokens: int) -> Completion: ...


class OpenAIProvider(LLMProvider):
    def generate(self, system, messages, max_output_tokens):
        data = self.transport.post("https://api.openai.com/v1/responses",
            {"Authorization": f"Bearer {self.spec.key}"},
            {"model": self.spec.model, "instructions": system, "input": messages,
             "max_output_tokens": max_output_tokens, "reasoning": {"effort": "none"},
             "store": False, "service_tier": "default"}, self.timeout)
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        texts = [part.get("text", "") for item in output if isinstance(item, dict) and item.get("type") == "message"
                 for part in item.get("content", []) if isinstance(part, dict) and part.get("type") == "output_text"]
        if any(not isinstance(text, str) for text in texts):
            raise ProviderError("INVALID_PROVIDER_RESPONSE")
        usage = data.get("usage")
        details = usage.get("input_tokens_details") or {} if isinstance(usage, dict) else {}
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        return completion("".join(texts), parse_usage(usage, "input_tokens", "output_tokens", cached))


class ChatCompletionsProvider(LLMProvider):
    endpoint: str

    def extra(self) -> dict:
        return {}

    def generate(self, system, messages, max_output_tokens):
        data = self.transport.post(self.endpoint, {"Authorization": f"Bearer {self.spec.key}"},
            {"model": self.spec.model, "messages": [{"role": "system", "content": system}, *messages],
             **self.extra(), **self.output_limit(max_output_tokens)}, self.timeout)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("INVALID_PROVIDER_RESPONSE") from None
        usage = data.get("usage")
        cached = usage.get("prompt_cache_hit_tokens", 0) if isinstance(usage, dict) else 0
        return completion(text, parse_usage(usage, "prompt_tokens", "completion_tokens", cached))

    def output_limit(self, limit: int) -> dict:
        return {"max_completion_tokens": limit}


class GroqProvider(ChatCompletionsProvider):
    endpoint = "https://api.groq.com/openai/v1/chat/completions"


class DeepSeekProvider(ChatCompletionsProvider):
    endpoint = "https://api.deepseek.com/chat/completions"

    def extra(self):
        return {"thinking": {"type": "disabled"}}

    def output_limit(self, limit):
        return {"max_tokens": limit}


class GeminiProvider(LLMProvider):
    def generate(self, system, messages, max_output_tokens):
        model = quote(self.spec.model, safe="-._")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in messages]
        data = self.transport.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": self.spec.key},
            {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
             "generationConfig": {"maxOutputTokens": max_output_tokens}}, self.timeout)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        except (KeyError, IndexError, TypeError):
            raise ProviderError("INVALID_PROVIDER_RESPONSE") from None
        usage = data.get("usageMetadata")
        normalized = None
        if isinstance(usage, dict):
            values = [usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount", 0)]
            if all(type(v) is int and v >= 0 for v in values):
                normalized = Usage(values[0], values[1] + values[2])
        return completion(text, normalized)


class LocalQwenProvider(ChatCompletionsProvider):
    endpoint='http://127.0.0.1:8766/v1/chat/completions'

    def __init__(self,spec,transport=None,timeout=30):
        super().__init__(spec,transport or HTTPTransport(no_proxy=True),timeout)

    def extra(self):
        return {'chat_template_kwargs':{'enable_thinking':False},'temperature':0.3,'top_p':0.9}

    def output_limit(self,limit):
        return {'max_tokens':min(limit,384)}


PROVIDERS = {"openai": OpenAIProvider, "deepseek": DeepSeekProvider, "groq": GroqProvider, "gemini": GeminiProvider,'local_qwen':LocalQwenProvider}
