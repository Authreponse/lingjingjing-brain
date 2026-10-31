from __future__ import annotations

from typing import Protocol

import httpx


class ModelUnavailable(RuntimeError):
    pass


class ChatModel(Protocol):
    name: str

    async def chat(
        self, messages: list[dict[str, str]], system: str
    ) -> str: ...


class EchoModel:
    """无外部依赖的 M0 验收驱动，不承担真实人格或推理。"""

    name = "echo"

    async def chat(self, messages: list[dict[str, str]], system: str) -> str:
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


class OpenAICompatibleModel:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
    ):
        self.name = "openai_compatible"
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def chat(self, messages: list[dict[str, str]], system: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0.7,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelUnavailable(f"模型调用失败：{exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelUnavailable("模型返回了空内容")
        return content.strip()
