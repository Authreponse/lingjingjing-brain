from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass(slots=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 7100
    auth_token: str = "lingjingjing-local-dev"

    # 模型接口一律"留空即降级"：不配置 provider 时用 echo 占位，绝不硬依赖外部模型。
    # 以后接 Ollama / 任意 OpenAI 兼容 API，只改 .env，不动代码。
    model_provider: str = "echo"
    model_name: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    model_timeout_seconds: float = 60.0
    heartbeat_seconds: float = 15.0

    # 其余需要模型的能力也留空：能力探测到未配置即返回明确"未配置"，不偷偷 mock。
    image_backend: str = ""
    comfyui_url: str = ""
    image_daily_budget: int = 10
    image_timeout_seconds: float = 120.0
    speech_asr_backend: str = ""
    speech_tts_backend: str = ""
    video_source: str = ""
    proactive_enabled: bool = True

    # M1 状态机（task-4）：静默时段按"本地时间"判断，用固定 UTC 偏移，
    # 不依赖 tzdata（Windows 上通常没装）。
    quiet_hours: str = "23:30-07:30"
    state_utc_offset_minutes: int = 480
    state_city: str = "Asia/Shanghai"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lingjingjing.db"

    @property
    def llm_configured(self) -> bool:
        """对话模型是否已配置。留空 = 未配置，通话走占位实现。"""
        return bool(self.model_base_url and self.model_name)

    @property
    def image_configured(self) -> bool:
        """生图是否已配置真实后端。"""
        return bool(self.comfyui_url) or self.image_backend not in ("", "auto")

    @property
    def speech_configured(self) -> bool:
        return bool(self.speech_asr_backend or self.speech_tts_backend)

    @property
    def video_configured(self) -> bool:
        return bool(self.video_source and self.video_source != "mock")

    def capability_report(self) -> dict[str, dict[str, object]]:
        """给 /v1/capabilities 用的"哪些能力已接、哪些还留空"清单。"""
        return {
            "chat": {
                "configured": self.llm_configured,
                "driver": self.model_provider if self.llm_configured else "echo",
                "hint": "在 .env 里设置 LING_MODEL_PROVIDER=openai_compatible 与 LING_MODEL_BASE_URL/LING_MODEL_NAME",
            },
            "image": {
                "configured": self.image_configured,
                "backend": "comfyui" if self.comfyui_url else (self.image_backend or "mock"),
                "hint": "设置 LING_COMFYUI_URL 接入真实生图",
            },
            "speech": {
                "configured": self.speech_configured,
                "hint": "设置 LING_SPEECH_ASR_BACKEND / LING_SPEECH_TTS_BACKEND 接入真实语音",
            },
            "video": {
                "configured": self.video_configured,
                "source": self.video_source or "mock",
                "hint": "设置 LING_VIDEO_SOURCE=bilibili 使用真实来源",
            },
        }

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        data_dir = Path(os.getenv("LING_DATA_DIR", "data"))
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir
        return cls(
            data_dir=data_dir,
            host=os.getenv("LING_HOST", "127.0.0.1"),
            port=int(os.getenv("LING_PORT", "7100")),
            auth_token=os.getenv("LING_AUTH_TOKEN", "lingjingjing-local-dev"),
            model_provider=os.getenv("LING_MODEL_PROVIDER", "echo"),
            # 留空即未配置：不写默认地址，避免误以为已经接了模型。
            model_name=os.getenv("LING_MODEL_NAME", "").strip(),
            model_base_url=os.getenv("LING_MODEL_BASE_URL", "").strip().rstrip("/"),
            model_api_key=os.getenv("LING_MODEL_API_KEY", "").strip(),
            model_timeout_seconds=float(os.getenv("LING_MODEL_TIMEOUT_SECONDS", "60")),
            heartbeat_seconds=float(os.getenv("LING_HEARTBEAT_SECONDS", "15")),
            quiet_hours=os.getenv("LING_QUIET_HOURS", "23:30-07:30"),
            state_utc_offset_minutes=int(os.getenv("LING_STATE_UTC_OFFSET_MINUTES", "480")),
            state_city=os.getenv("LING_STATE_CITY", "Asia/Shanghai"),
            image_backend=os.getenv("LING_IMAGE_BACKEND", "").strip(),
            comfyui_url=os.getenv("LING_COMFYUI_URL", "").strip().rstrip("/"),
            image_daily_budget=int(os.getenv("LING_IMAGE_DAILY_BUDGET", "10")),
            image_timeout_seconds=float(os.getenv("LING_IMAGE_TIMEOUT_SECONDS", "120")),
            speech_asr_backend=os.getenv("LING_SPEECH_ASR_BACKEND", "").strip(),
            speech_tts_backend=os.getenv("LING_SPEECH_TTS_BACKEND", "").strip(),
            video_source=os.getenv("LING_VIDEO_SOURCE", "").strip(),
            proactive_enabled=os.getenv("LING_PROACTIVE_ENABLED", "true").strip().lower()
            not in ("0", "false", "no", "off"),
        )