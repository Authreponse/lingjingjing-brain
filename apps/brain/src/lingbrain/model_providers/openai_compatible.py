# OpenAI 兼容提供商：支持 OpenAI / Ollama / 硅基流动 / 任意兼容接口

from __future__ import annotations

import httpx
from typing import AsyncIterator, Any

from .base import (
    ModelProvider,
    ProviderConfig,
    ModelUnavailable,
    RateLimitError,
    AuthenticationError,
)


class OpenAICompatibleProvider:
    """OpenAI 兼容接口提供商：支持 OpenAI / Ollama / 硅基流动 / 任意兼容接口"""

    name = "openai_compatible"

    def __init__(self, config):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=self.config.timeout_seconds,
                headers=self._build_headers(),
            )
        return self._client

    def _build_headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: str,
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        stream: bool = False,
        **kwargs,
    ):
        client = self._get_client()
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # tools/tool_choice 透传（需上游支持）

        async def _do_request():
            resp = await self._client.post(
                "/chat/completions",
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = await self._retry_request(_do_request)
        except Exception as e:
            raise

        if stream:
            return self._stream_response(data)
        else:
            return self._parse_response(data)

    def _parse_response(self, data: dict) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ModelUnavailable(f"解析响应失败: {e}") from e
        if not isinstance(content, str) or not content.strip():
            raise ModelUnavailable("模型返回了空内容")
        return content.strip()

    async def _stream_response(self, data: dict) -> AsyncIterator[str]:
        # 简化：非真正流式，直接 yield 完整内容
        yield self._parse_response(data)

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            resp = await self._client.get("/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            resp = await self._client.get("/models", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        except Exception:
            return [self.config.model]
