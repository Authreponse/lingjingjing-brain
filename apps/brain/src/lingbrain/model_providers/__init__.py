# 统一模型接口层
# 统一 OpenAI / Ollama / 硅基流动 / 自定义 等多提供商接口
# 统一错误码、重试、流式、工具调用

from .base import ModelProvider, ModelError, ModelUnavailable, ProviderConfig
from .echo import EchoProvider
from .openai_compatible import OpenAICompatibleProvider
from .ollama import OllamaProvider
from .siliconcloud import SiliconCloudProvider
from .factory import create_provider, ProviderRegistry, build_provider_from_settings

__all__ = [
    "ModelProvider",
    "ModelError",
    "ModelUnavailable",
    "ProviderConfig",
    "EchoProvider",
    "OpenAICompatibleProvider",
    "OllamaProvider",
    "SiliconCloudProvider",
    "create_provider",
    "ProviderRegistry",
    "build_provider_from_settings",
]