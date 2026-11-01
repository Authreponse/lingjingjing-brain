# 提供商工厂：从配置创建模型提供商实例

from __future__ import annotations

from typing import Any

from .base import ProviderConfig, ModelProvider
from .echo import EchoProvider
from .openai_compatible import OpenAICompatibleProvider
from .ollama import OllamaProvider
from .siliconcloud import SiliconCloudProvider


class ProviderRegistry:
    """提供商注册表"""

    _providers: dict[str, type] = {
        "echo": EchoProvider,
        "openai": OpenAICompatibleProvider,
        "openai_compatible": OpenAICompatibleProvider,
        "ollama": OllamaProvider,
        "siliconcloud": SiliconCloudProvider,
        "siliconflow": SiliconCloudProvider,  # 别名
    }

    @classmethod
    def register(cls, name: str, provider_class: type) -> None:
        cls._providers[name.lower()] = provider_class

    @classmethod
    def get(cls, name: str) -> type | None:
        return cls._providers.get(name.lower())

    @classmethod
    def list_available(cls) -> list[str]:
        return list(cls._providers.keys())


def create_provider(config: "ProviderConfig") -> Any:
    """根据配置创建提供商实例

    Args:
        config: ProviderConfig 实例

    Returns:
        ModelProvider 实例

    Raises:
        ValueError: 未知的提供商名称
    """
    provider_class = ProviderRegistry.get(config.name)
    if provider_class is None:
        available = ", ".join(ProviderRegistry.list_available())
        raise ValueError(
            f"未知的模型提供商: {config.name}。可用: {available}"
        )
    return provider_class(config)


def build_provider_from_settings(settings: Any) -> Any:
    """从 Settings 对象构建提供商

    读取 settings 中的模型相关配置，自动选择提供商：
    - LING_MODEL_PROVIDER: echo / openai / ollama / siliconcloud
    - 对应的 base_url, model, api_key 等配置

    如果配置不完整（如 openai_compatible 但无 base_url/model），自动降级为 EchoProvider。

    Args:
        settings: Settings 对象（需有 model_provider, model_name, model_base_url, model_api_key 等属性）

    Returns:
        ModelProvider 实例
    """
    from .base import ProviderConfig

    provider_name = getattr(settings, "model_provider", "echo").lower()

    config = ProviderConfig(
        name=provider_name,
        base_url=getattr(settings, "model_base_url", "").strip(),
        model=getattr(settings, "model_name", "").strip(),
        api_key=getattr(settings, "model_api_key", "").strip(),
        timeout_seconds=float(getattr(settings, "model_timeout_seconds", 60)),
        max_retries=int(getattr(settings, "model_max_retries", 3)),
    )

    # 根据提供商设置默认 base_url
    if provider_name == "ollama" and not config.base_url:
        config.base_url = "http://127.0.0.1:11434/v1"
    elif provider_name in ("siliconcloud", "siliconflow") and not config.base_url:
        config.base_url = "https://api.siliconflow.cn/v1"

    # 如果是 openai_compatible 等需要配置的提供商，但缺少必要配置，降级为 EchoProvider
    if provider_name in ("openai", "openai_compatible", "siliconcloud", "siliconflow"):
        if not config.base_url or not config.model:
            # 配置不完整，降级为 EchoProvider
            echo_config = ProviderConfig(
                name="echo",
                base_url="",
                model="echo",
                api_key="",
            )
            return EchoProvider(echo_config)

    return create_provider(config)
