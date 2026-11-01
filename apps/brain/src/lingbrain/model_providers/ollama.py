# Ollama 提供商：本地 Ollama 服务

from __future__ import annotations

from typing import Any

from .openai_compatible import OpenAICompatibleProvider
from .base import ProviderConfig


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama 本地服务提供商

    Ollama 的 /chat/completions 兼容 OpenAI 格式，但有一些差异：
    - 模型名使用 Ollama 的标签名（如 qwen2.5:7b-instruct-q4_K_M）
    - 支持 /api/tags 列出模型
    - 不需要 API Key
    """

    name = "ollama"

    def __init__(self, config: "ProviderConfig"):
        # Ollama 默认不需要 API Key
        if not config.api_key:
            config = ProviderConfig(
                name=config.name,
                base_url=config.base_url,
                model=config.model,
                api_key="",
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                retry_base_delay=config.retry_base_delay,
                max_retry_delay=config.max_retry_delay,
                extra_headers=config.extra_headers,
                extra_params=config.extra_params,
            )
        super().__init__(config)

    async def list_models(self) -> list[str]:
        """使用 Ollama 原生 /api/tags 接口"""
        try:
            client = self._get_client()
            resp = await self._client.get("/api/tags", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return [self.config.model]

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            resp = await self._client.get("/api/tags", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False
