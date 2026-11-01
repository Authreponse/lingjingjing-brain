# 硅基流动提供商：硅基流动 API（OpenAI 兼容）

from __future__ import annotations

from typing import Any

from .openai_compatible import OpenAICompatibleProvider
from .base import ProviderConfig


class SiliconCloudProvider(OpenAICompatibleProvider):
    """硅基流动 API 提供商

    硅基流动完全兼容 OpenAI 接口格式：
    - base_url: https://api.siliconflow.cn/v1
    - 需要 API Key
    - 支持多种开源模型（Qwen, DeepSeek, Llama 等）
    """

    name = "siliconcloud"

    def __init__(self, config: "ProviderConfig"):
        # 硅基流动默认 base_url
        if not config.base_url:
            config = ProviderConfig(
                name=config.name,
                base_url="https://api.siliconflow.cn/v1",
                model=config.model,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
                retry_base_delay=config.retry_base_delay,
                max_retry_delay=config.max_retry_delay,
                extra_headers=config.extra_headers,
                extra_params=config.extra_params,
            )
        super().__init__(config)

    async def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            resp = await self._client.get("/models", timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return [m["id"] for m in data.get("data", [])]
        except Exception:
            return [self.config.model]
