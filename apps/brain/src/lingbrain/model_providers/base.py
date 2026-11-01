# 模型提供商基类与通用类型定义

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal
from enum import Enum


class ModelError(Exception):
    """模型调用基础异常"""
    def __init__(self, message: str, code: str = "MODEL_ERROR", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelUnavailable(ModelError):
    """模型不可用（可重试）"""
    def __init__(self, message: str, code: str = "MODEL_UNAVAILABLE"):
        super().__init__(message, code, retryable=True)


class RateLimitError(ModelError):
    """限流错误（可重试）"""
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, "RATE_LIMITED", retryable=True)
        self.retry_after = retry_after


class AuthenticationError(ModelError):
    """认证失败（不可重试）"""
    def __init__(self, message: str):
        super().__init__(message, "AUTHENTICATION_FAILED", retryable=False)


class ModelErrorCodes:
    """标准错误码"""
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    INVALID_REQUEST = "INVALID_REQUEST"
    CONTENT_FILTER = "CONTENT_FILTER"
    CONTEXT_LENGTH_EXCEEDED = "CONTEXT_LENGTH_EXCEEDED"
    TOOL_CALL_ERROR = "TOOL_CALL_ERROR"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


@dataclass(frozen=True)
class ProviderConfig:
    """提供商配置"""
    name: str
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_base_delay: float = 1.0
    max_retry_delay: float = 30.0
    extra_headers: dict = None
    extra_params: dict = None

    def __post_init__(self):
        if self.extra_headers is None:
            object.__setattr__(self, 'extra_headers', {})
        if self.extra_params is None:
            object.__setattr__(self, 'extra_params', {})


class ModelProvider(ABC):
    """模型提供商抽象基类"""

    name: str

    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
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
    ) -> str | AsyncIterator[str]:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @abstractmethod
    async def list_models(self) -> list[str]:
        pass

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _build_payload(
        self,
        messages: list[dict[str, str]],
        system: str,
        temperature: float,
        max_tokens: int | None,
        tools: list[dict] | None,
        tool_choice: str | dict | None,
        stream: bool,
    ) -> dict:
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return payload

    async def _retry_request(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(self.config.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if attempt >= self.config.max_retries:
                    raise
                delay = min(
                    self.config.retry_base_delay * (2 ** attempt),
                    self.config.max_retry_delay,
                )
                import asyncio
                await asyncio.sleep(delay)
        raise last_exc
