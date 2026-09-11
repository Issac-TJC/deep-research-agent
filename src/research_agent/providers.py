"""Provider wire formats terminate here. Fixture mode is explicit and never a live fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from research_agent.contracts import ModelUsage
from research_agent.settings import Settings


class ProviderError(Exception):
    def __init__(self, code: str, *, retryable=True, unknown=False):
        super().__init__(code)
        self.retryable = retryable
        self.unknown = unknown


@dataclass
class Completion:
    message: dict[str, Any]
    usage: ModelUsage
    finish_reason: str
    provider_model: str


class ModelProvider(Protocol):
    async def complete(
        self, messages: list[dict], *, tools: list[dict] | None, structured: bool, max_tokens: int
    ) -> Completion: ...


class DeepSeekProvider:
    def __init__(self, settings: Settings):
        self.s = settings

    async def complete(self, messages, *, tools=None, structured=False, max_tokens=8192):
        if not self.s.deepseek_api_key:
            raise ProviderError("deepseek_key_missing", retryable=False)
        body = {
            "model": self.s.deepseek_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "thinking": {"type": self.s.deepseek_thinking},
            "stream": False,
        }
        if self.s.deepseek_thinking == "enabled":
            body["reasoning_effort"] = self.s.deepseek_effort
        if tools:
            body["tools"] = tools
        if structured:
            body["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self.s.request_timeout, trust_env=False) as client:
                r = await client.post(
                    self.s.deepseek_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.s.deepseek_api_key},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ProviderError("model_transport_error", unknown=True) from exc
        if r.status_code != 200:
            raise ProviderError(
                f"model_http_{r.status_code}",
                retryable=r.status_code == 429 or r.status_code >= 500,
                unknown=r.status_code >= 500,
            )
        try:
            data = r.json()
            choice = data["choices"][0]
            message = {
                k: v
                for k, v in choice["message"].items()
                if k in {"role", "content", "tool_calls", "reasoning_content"} and v is not None
            }
            message["role"] = "assistant"
            raw = data.get("usage", {})
            if "prompt_tokens" not in raw or "completion_tokens" not in raw:
                raise ValueError("missing usage")
            inp, out = int(raw["prompt_tokens"]), int(raw["completion_tokens"])
            hit = int(raw.get("prompt_cache_hit_tokens", 0))
            if min(inp, out, hit) < 0 or hit > inp:
                raise ValueError("invalid usage")
            miss = inp - hit
            usage = ModelUsage(
                input_tokens=inp,
                output_tokens=out,
                cache_hit_tokens=hit,
                cache_miss_tokens=miss,
                usd=(
                    miss * self.s.model_input_usd_per_million
                    + hit * self.s.model_cache_usd_per_million
                    + out * self.s.model_output_usd_per_million
                )
                / 1_000_000,
            )
            return Completion(
                message, usage, choice["finish_reason"], data.get("model", self.s.deepseek_model)
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("model_invalid_envelope", unknown=True) from exc


class TavilySearch:
    def __init__(self, settings: Settings):
        self.s = settings

    async def search(self, query: str) -> dict:
        if not self.s.tavily_api_key:
            raise ProviderError("tavily_key_missing", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                r = await client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + self.s.tavily_api_key},
                    json={
                        "query": query,
                        "search_depth": "basic",
                        "max_results": 5,
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError("search_transport_error", unknown=True) from exc
        if r.status_code != 200:
            raise ProviderError(
                f"search_http_{r.status_code}",
                retryable=r.status_code == 429 or r.status_code >= 500,
                unknown=r.status_code >= 500,
            )
        try:
            data = r.json()
            return {
                "results": [
                    {"url": x["url"], "title": x.get("title", ""), "snippet": x.get("content", "")[:1200]}
                    for x in data["results"]
                ],
                "request_id": data.get("request_id"),
                "evidence": False,
            }
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError("search_invalid_envelope", unknown=True) from exc
