# Echo 提供商：无外部依赖的 M0 验收驱动

from __future__ import annotations

from typing import AsyncIterator

from .base import ModelProvider, ProviderConfig


class EchoProvider:
    """无外部依赖的 M0 验收驱动，不承担真实人格或推理。"""

    name = "echo"

    def __init__(self, config = None):
        if config is None:
            from .base import ProviderConfig
            config = ProviderConfig(
                name="echo",
                base_url="",
                model="echo",
                api_key="",
            )
        self.config = config

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
        current = messages[-1]["content"] if messages else ""
        if "记得" in current or "昨天" in current:
            previous = [
                item["content"]
                for item in messages[:-1]
                if item["role"] == "user" and item["content"].strip()
            ]
            if previous:
                return f"我记得。你之前说过：“{previous[-1]}”。"
            return "我记得我们聊过，但还没有找到更多细节。"
        if "我喜欢" in current or "我不喜欢" in current or "我讨厌" in current:
            return "我记住了。这件事和你的感受，我会好好放着。"
        if "我叫" in current or "叫我" in current:
            return "好，我记住你的名字了。下次见面，我还记得。"
        return f"我在。你说：“{current}”。"

    async def health_check(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return ["echo"]
