# 模型模块统一入口：保持向后兼容，内部使用新的 model_providers

from __future__ import annotations

from typing import Protocol

from .model_providers import (
    ModelProvider,
    ModelError,
    ModelUnavailable,
    ProviderConfig,
    EchoProvider,
    OpenAICompatibleProvider,
    OllamaProvider,
    SiliconCloudProvider,
    create_provider,
    build_provider_from_settings,
)


# 向后兼容导出
class ChatModel(Protocol):
    name: str
    async def chat(self, messages: list[dict[str, str]], system: str) -> str: ...


class EchoModel(EchoProvider):
    """向后兼容：EchoModel"""
    pass


class OpenAICompatibleModel(OpenAICompatibleProvider):
    """向后兼容：OpenAICompatibleModel"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
    ):
        from .model_providers.base import ProviderConfig
        config = ProviderConfig(
            name="openai_compatible",
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        super().__init__(config)
