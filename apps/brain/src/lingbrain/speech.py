"""灵鲸鲸 M3 一级语音：ASR/TTS 后端抽象、Mock 后端、受管音频目录与语音链路处理器。

**本机没有 Whisper / TTS 模型，本模块交付的是可验收的「架构 + Mock」，不是真实语音能力。**
接真模型时只新增/替换 ASRBackend / TTSBackend 的实现，路由、校验、落盘、chat 注入点
与错误语义都不用动。

## 后端协议（可替换点）

- ASRBackend.transcribe(audio, *, filename, duration_seconds) -> AsrResult
- TTSBackend.synthesize(text) -> TtsResult
- v1 明确不下载大模型，所以只实现 Mock：MockASRBackend / MockTTSBackend
  （以及测试用的 UnavailableASRBackend / UnavailableTTSBackend 表示"后端不可用"）。
  审查与验收必须把它们当作 mock：响应里 real_model 恒为 false，notice 写明是 Mock。

## Mock 的行为是确定的（不用随机数）

- MockASR：
  - 只有真的能解码的 WAV（PCM 8/16/24/32 bit、mono 或 stereo）才给转写。
    WAV 头 + data 块的时长必须与字节数吻合，截断/伪造的头一律当"格式无法解析"。
  - 去掉首尾静音后的有声时长 < min_speech_seconds -> empty_audio，**不编造文本**。
  - 有声时长够、但 RMS 低于 noise_rms_threshold -> noise，**不编造文本**。
  - 其余：文本取自文件名主干（按住说话.wav -> "按住说话"），文件名看起来不像一句话
    （audio_001 / 1730456789012 / clip）就回一句固定的中文占位，
    并标 transcript_kind="placeholder"，前端据此知道这句不是真的听写结果。
  - 同样的音频字节永远得到同样的转写；probe 探测音频结构，未持久化的内存字节也能用。
- MockTTS：用标准库 wave 生成一段可播放、可解码的 16 bit PCM WAV，
  长度由文本长度决定，音高由文本哈希决定；一句话含多少汉字由 speech_seconds() 决定。

## 错误分类（路由层映射成 HTTP 状态码，绝不 500、绝不臆测文本）

| code | HTTP | 含义 |
|---|---|---|
| empty_audio | 422 | 没有文件 / 0 字节 / 有声时长太短 |
| audio_too_large | 413 | 超过单条上限（默认 8 MiB） |
| audio_too_long | 422 | 时长超过上限（默认 30 秒） |
| audio_unsupported_format | 415 | 扩展名不是 wav（v1 只保证 Mock 能解析 WAV） |
| audio_invalid | 422 | 扩展名是 wav，但内容解析不了（文件头不一致/损坏/不是 WAV） |
| audio_noise | 422 | 有能量但没有可识别语音 |
| asr_backend_unavailable / tts_backend_unavailable | 503 | 后端不可用（未安装模型、连接失败等） |
| asr_timeout / tts_timeout | 504 | 后端超时 |
| text_too_long | 422 | 要合成的话超过上限（默认 200 字） |

## 一级语音链路

按住说话 -> 落 data/audio/（受管目录）-> ASR -> 注入的 chat 函数 -> 文本回复 -> TTS -> 音频地址
由 SpeechService.voice_turn() 串起来；chat 函数与 app.py 的 /v1/chat 同签名
async (message, client_id) -> ChatResponse，所以默认直接注入 BrainService.chat，
走的是同一套事件、记忆与状态逻辑。任何一步失败都返回 status != "ok" 并且
reply / transcript 留空，降级信息写在 status / error / notice 里，不隐藏。
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import struct
import sys
import uuid
import wave
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict

LOGGER = logging.getLogger("lingbrain.speech")

ACTOR = "灵鲸鲸"

#: 语音（包括 Mock）全部落在受管目录名；相对 data_dir。
AUDIO_DIR_NAME = "audio"

#: Mock 后端在响应里必须自报家门：real_model=false，绝不冒充真实转写/合成。
MOCK_NOTICE = "当前是 Mock 语音后端：不调用任何语音模型，转写与合成结果只用于验证链路。"

DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MiB
DEFAULT_MAX_AUDIO_SECONDS = 30.0
DEFAULT_ASR_TIMEOUT_SECONDS = 20.0
DEFAULT_TTS_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_TTS_CHARS = 200

#: MockASR 判定"没人说话"与"只有噪声"的阈值。
DEFAULT_SILENCE_RMS = 60.0
DEFAULT_MIN_SPEECH_SECONDS = 0.3
DEFAULT_NOISE_RMS_THRESHOLD = 300.0

#: MockTTS 默认音色与采样率（单声道 16 bit PCM WAV，任何播放器都能放）。
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_VOICE = "mock-ling"

WAV_MIME = "audio/wav"
#: v1 只有 WAV 能被 Mock 解析；wav 之外的"音频"明确 415，而不是硬着头皮解析。
_WAV_EXTENSIONS = frozenset({"wav", "wave"})
_OTHER_AUDIO_EXTENSIONS = frozenset(
    {"mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "webm", "amr", "silk", "aiff", "caf"}
)


# --------------------------------------------------------------- 错误与状态码
class SpeechError(Exception):
    """语音链路的可识别错误。code 是给前端/测试的稳定枚举。"""

    code = "speech_error"
    http_status = 422
    message = "语音处理失败"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        self.detail = message or self.message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "http_status": self.http_status, "detail": self.detail}


class EmptyAudio(SpeechError):
    code = "empty_audio"
    http_status = 422
    message = "没有听到内容：音频为空或太短"


class AudioTooLarge(SpeechError):
    code = "audio_too_large"
    http_status = 413
    message = "音频过大"


class AudioTooLong(SpeechError):
    code = "audio_too_long"
    http_status = 422
    message = "音频太长"


class UnsupportedAudioFormat(SpeechError):
    code = "audio_unsupported_format"
    http_status = 415
    message = "不支持的音频格式"


class InvalidAudio(SpeechError):
    code = "audio_invalid"
    http_status = 422
    message = "音频文件损坏或无法解析"


class NoiseOnly(SpeechError):
    code = "audio_noise"
    http_status = 422
    message = "只听到了噪声，没有听清在说什么"


class SpeechBackendUnavailable(SpeechError):
    code = "speech_backend_unavailable"
    http_status = 503
    message = "语音后端不可用"


class AsrUnavailable(SpeechBackendUnavailable):
    code = "asr_backend_unavailable"
    message = "语音识别后端不可用"


class TtsUnavailable(SpeechBackendUnavailable):
    code = "tts_backend_unavailable"
    message = "语音合成后端不可用"


class AsrTimeout(SpeechError):
    code = "asr_timeout"
    http_status = 504
    message = "语音识别超时"


class TtsTimeout(SpeechError):
    code = "tts_timeout"
    http_status = 504
    message = "语音合成超时"


class TextTooLong(SpeechError):
    code = "text_too_long"
    http_status = 422
    message = "要合成的话太长"


class ReplyFailed(SpeechError):
    code = "reply_failed"
    http_status = 503
    message = "回复生成失败"


class AudioPathError(SpeechError):
    code = "audio_path_invalid"
    http_status = 409
    message = "音频路径不在受管目录内"


class ChatNotConfigured(SpeechError):
    code = "chat_not_configured"
    http_status = 503
    message = "没有注入 chat 函数，只完成转写"


# --------------------------------------------------------------- 数据结构
class AudioProbe(TypedDict, total=False):
    format: str
    readable: bool
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    rms: float
    peak: int
    speech_seconds: float
    reason: str


class ProbeFailure(TypedDict):
    readable: bool
    reason: str


@dataclass(slots=True)
class SpeechRecord:
    """一次语音输入/输出的留痕。不落 SQLite，只写 data/audio/records/*.json。

    选择不落库的原因：task-9 的写范围不含 db.py/schemas.py，新增表需要 Lead 另开任务。
    """

    id: str
    kind: str  # "input" | "output"
    path: str  # 相对 data_dir，分隔符统一为 "/"
    backend: str
    real_model: bool
    created_at: str
    transcript: str | None = None
    text: str | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    status: str | None = None
    error: str | None = None
    probe: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "backend": self.backend,
            "real_model": self.real_model,
            "created_at": self.created_at,
        }
        if self.transcript is not None:
            payload["transcript"] = self.transcript
        if self.text is not None:
            payload["text"] = self.text
        if self.duration_seconds is not None:
            payload["duration_seconds"] = round(self.duration_seconds, 3)
        if self.size_bytes is not None:
            payload["size_bytes"] = self.size_bytes
        if self.content_type:
            payload["content_type"] = self.content_type
        if self.status:
            payload["status"] = self.status
        if self.error:
            payload["error"] = self.error
        if self.probe is not None:
            payload["probe"] = self.probe
        payload.update(self.extra)
        return payload


@dataclass(slots=True)
class AsrResult:
    text: str
    backend: str = "mock"
    real_model: bool = False
    language: str = "zh"
    confidence: float | None = None
    duration_seconds: float | None = None
    #: speech=由文件名注入的真实可控文本；placeholder=固定占位；见错误码看空/噪声/损坏
    transcript_kind: str = "speech"
    notice: str | None = None
    probe: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "backend": self.backend,
            "real_model": self.real_model,
            "language": self.language,
            "confidence": self.confidence,
            "duration_seconds": self.duration_seconds,
            "transcript_kind": self.transcript_kind,
            "notice": self.notice,
            "probe": self.probe,
        }


@dataclass(slots=True)
class TtsResult:
    audio: bytes
    content_type: str = WAV_MIME
    backend: str = "mock"
    real_model: bool = False
    voice: str = DEFAULT_VOICE
    duration_seconds: float = 0.0
    sample_rate: int = DEFAULT_SAMPLE_RATE
    voice_tier: str = "mock"
    notice: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """只回元信息；音频字节走 /v1/speech/audio/{record_id}。"""
        return {
            "content_type": self.content_type,
            "backend": self.backend,
            "real_model": self.real_model,
            "voice": self.voice,
            "duration_seconds": round(self.duration_seconds, 3),
            "sample_rate": self.sample_rate,
            "voice_tier": self.voice_tier,
            "notice": self.notice,
        }


# --------------------------------------------------------------- 后端协议
class ASRBackend(Protocol):
    """语音识别后端。接 faster-whisper 等真实模型时实现这一个接口即可。"""

    name: str
    real_model: bool

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        duration_seconds: float | None = None,
    ) -> AsrResult: ...

    def probe(self, audio: bytes, *, filename: str) -> AudioProbe | ProbeFailure: ...


class TTSBackend(Protocol):
    """语音合成后端。接 Piper 等真实音色时实现这一个接口即可。"""

    name: str
    real_model: bool

    async def synthesize(self, text: str) -> TtsResult: ...


# --------------------------------------------------------------- WAV 解析
def detect_audio_format(data: bytes, filename: str | None = None) -> str:
    """按魔数判断音频容器；判断不出来时再看扩展名（后端可能返回任意字节）。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    head = data[:12]
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "webm"
    if not data:
        return "empty"
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    return suffix or "unknown"


def normalize_audio_name(filename: str | None) -> tuple[str, str]:
    """返回 (扩展名小写, 渲染用的文件名)。只取 basename，拒绝路径穿越。"""
    basename = Path(filename or "").name
    basename = re.sub(r'[\x00-\x1f<>:"|?*]', "_", basename).strip()
    if not basename or basename in {".", ".."}:
        return "", ""
    extension = Path(basename).suffix.lower().lstrip(".")
    return extension, basename


def probe_wav(data: bytes) -> AudioProbe | ProbeFailure:
    """只读解析 WAV 头：时长、采样参数、RMS/峰值、去掉静音后的有声时长。"""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return {"readable": False, "reason": "不是 RIFF/WAVE 容器"}
    offset = 12
    total = len(data)
    fmt: dict[str, int] = {}
    pcm: bytes | None = None
    while offset + 8 <= total:
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        body = offset + 8
        if chunk_id == b"fmt ":
            if size < 16 or body + 16 > total:
                return {"readable": False, "reason": "fmt 块不完整"}
            audio_format, channels, sample_rate = struct.unpack("<HHI", data[body : body + 8])
            (bits,) = struct.unpack("<H", data[body + 14 : body + 16])
            if audio_format == 0xFFFE and size >= 40 and body + 26 <= total:
                # WAVE_FORMAT_EXTENSIBLE：真正的格式在 SubFormat 前两个字节里
                (audio_format,) = struct.unpack("<H", data[body + 24 : body + 26])
            fmt = {
                "audio_format": audio_format,
                "channels": channels,
                "sample_rate": sample_rate,
                "bits": bits,
            }
        elif chunk_id == b"data":
            if body + size > total:
                return {"readable": False, "reason": "data 块被截断（时长与字节数不符）"}
            pcm = data[body : body + size]
            break
        offset = body + size + (size & 1)
    if not fmt:
        return {"readable": False, "reason": "缺少 fmt 块"}
    if pcm is None:
        return {"readable": False, "reason": "缺少 data 块"}
    if fmt["audio_format"] not in (1, 3) or fmt["bits"] not in (8, 16, 24, 32):
        return {"readable": False, "reason": f"不支持的编码（format={fmt['audio_format']}, bits={fmt['bits']}）"}
    if fmt["channels"] < 1 or fmt["sample_rate"] < 1:
        return {"readable": False, "reason": "采样参数非法"}

    stats = _pcm_samples(pcm, fmt["bits"])
    frame_bytes = max(1, fmt["channels"] * max(1, fmt["bits"] // 8))
    frames = len(pcm) // frame_bytes
    duration = frames / fmt["sample_rate"] if fmt["sample_rate"] else 0.0
    if stats:
        peak = max(abs(value) for value in stats)
        rms = math.sqrt(sum(value * value for value in stats) / len(stats))
        speech_seconds = _speech_seconds(pcm, fmt["bits"], fmt["channels"], fmt["sample_rate"])
    else:
        speech_seconds, peak, rms = 0.0, 0, 0.0
    return {
        "format": "wav",
        "readable": True,
        "duration_seconds": round(duration, 3),
        "sample_rate": int(fmt["sample_rate"]),
        "channels": int(fmt["channels"]),
        "sample_width": int(max(1, fmt["bits"] // 8)),
        "frames": frames,
        "rms": round(rms, 2),
        "peak": int(peak),
        "speech_seconds": round(speech_seconds, 3),
    }


#: 判定"有声"用的短时 RMS 窗口（毫秒）。全局 RMS 会把短暂响亮的东西（如咔哒声）
#: 摊薄到听不见；按 20ms 一窗各自算，才符合"人耳听到的响度"。
SPEECH_WINDOW_MS = 20


def _speech_seconds(pcm: bytes, bits: int, channels: int, sample_rate: int) -> float:
    """按短时 RMS 统计"有声时长"：窗口 RMS 达到阈值才算有声。"""
    frame_bytes = max(1, channels * max(1, bits // 8))
    frame_count = len(pcm) // frame_bytes
    if frame_count <= 0 or sample_rate <= 0:
        return 0.0
    window = max(1, int(sample_rate * SPEECH_WINDOW_MS / 1000))
    loud_frames = 0
    for start in range(0, frame_count, window):
        chunk = pcm[start * frame_bytes : min(frame_count, start + window) * frame_bytes]
        samples = _pcm_samples(chunk, bits)
        if not samples:
            continue
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        if rms >= DEFAULT_SILENCE_RMS:
            loud_frames += min(window, frame_count - start)
    return loud_frames / sample_rate


def _pcm_samples(pcm: bytes, bits: int) -> list[int]:
    """把 PCM 解成有符号整数序列，供 RMS / 静音判断。"""
    if bits == 8:
        return [value - 128 for value in pcm]
    if bits == 16:
        aligned = pcm[: len(pcm) - (len(pcm) % 2)]
        samples = array.array("h")
        samples.frombytes(aligned)
        if sys.byteorder == "big":
            samples.byteswap()
        return samples.tolist()
    step = bits // 8
    values: list[int] = []
    for index in range(0, len(pcm) - step + 1, step):
        chunk = pcm[index : index + step]
        value = int.from_bytes(chunk, "little", signed=True)
        if bits == 24:
            values.append(value)
        else:
            values.append(value)
    return values


def read_audio_metadata(path: Path) -> dict[str, Any]:
    """读取已落盘音频的头部信息（运维/测试用）。"""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return {"readable": False, "reason": f"读取失败：{exc}"}
    return dict(probe_wav(data))


def wav_duration(data: bytes) -> float | None:
    probe = probe_wav(data)
    if not probe.get("readable"):
        return None
    return float(probe.get("duration_seconds") or 0.0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------- Mock ASR
def _stem_looks_like_sentence(stem: str) -> bool:
    """文件名主干是不是一句"人话"（用来做确定性可控文本）。"""
    cleaned = stem.strip()
    if not cleaned:
        return False
    stripped = re.sub(r"[\s\-_.]+", "", cleaned)
    if len(stripped) < 2:
        return False
    if re.fullmatch(r"[A-Za-z0-9\-_. ]+", stripped) and not re.search(r"[\u4e00-\u9fff]", stripped):
        # 纯英文：只认"一个四字母以上的英文词或短语"（hello_whale -> "hello whale"）；
        # audio_001 / IMG20231031 / rec01 这类录音机默认名一律当机器名
        words = re.split(r"[\-_.\s]+", stripped)
        if any(re.search(r"\d{2,}", word) for word in words):
            return False
        return all(re.fullmatch(r"[A-Za-z]{2,}", word) for word in words) and any(
            len(word) >= 4 for word in words
        )
    return len(re.findall(r"[\u4e00-\u9fff]", stripped)) >= 2


def _placeholder_for(filename: str, duration_seconds: float) -> str:
    label = Path(filename or "语音").stem.strip() or "语音"
    return f"（Mock 占位转写：收到约 {duration_seconds:.1f} 秒语音，文件名「{label}」）"


class MockASRBackend:
    """无模型验收后端：对 WAV 做确定性转写，空音频/噪声/损坏/超长一律报错，绝不臆测。"""

    name = "mock"
    real_model = False

    def __init__(
        self,
        *,
        max_duration_seconds: float = DEFAULT_MAX_AUDIO_SECONDS,
        min_speech_seconds: float = DEFAULT_MIN_SPEECH_SECONDS,
        noise_rms_threshold: float = DEFAULT_NOISE_RMS_THRESHOLD,
        notice: str = MOCK_NOTICE,
    ):
        self.max_duration_seconds = max_duration_seconds
        self.min_speech_seconds = min_speech_seconds
        self.noise_rms_threshold = noise_rms_threshold
        self.notice = notice
        self.calls = 0
        self.last_filename = ""

    def probe(self, audio: bytes, *, filename: str) -> AudioProbe | ProbeFailure:
        if not audio:
            return {"readable": False, "reason": "空音频"}
        return probe_wav(audio)

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        duration_seconds: float | None = None,
    ) -> AsrResult:
        self.calls += 1
        self.last_filename = filename
        if not audio:
            raise EmptyAudio("没有收到音频数据")
        probe = probe_wav(audio)
        if not probe.get("readable"):
            raise InvalidAudio(f"音频无法解析：{probe.get('reason') or '未知原因'}")
        duration = float(probe.get("duration_seconds") or 0.0)
        if duration <= 0:
            raise EmptyAudio("音频时长是 0 秒")
        if duration > self.max_duration_seconds:
            raise AudioTooLong(
                f"音频 {duration:.1f} 秒，超过上限 {self.max_duration_seconds:.0f} 秒"
            )
        # 两级判断：
        #   1) 有没有"够响的东西"？——按 20ms 短时 RMS 统计的有声时长；
        #   2) 有东西但它听不出语音成分（底噪/嗡声）——单独报 audio_noise，
        #      而不是笼统说"没听到内容"。
        speech = float(probe.get("speech_seconds") or 0.0)
        rms = float(probe.get("rms") or 0.0)
        if speech < self.min_speech_seconds:
            raise EmptyAudio(
                f"去掉静音后只有 {speech:.2f} 秒有声音，低于 {self.min_speech_seconds:.2f} 秒"
            )
        if rms < self.noise_rms_threshold:
            raise NoiseOnly(f"检测到能量（RMS {rms:.0f}）但没有可识别的语音成分")

        stem = Path(filename or "").stem
        if _stem_looks_like_sentence(stem):
            text = " ".join(stem.replace("_", " ").split())
            transcript_kind = "speech"
            confidence = round(min(0.99, 0.5 + rms / 1000.0), 2)
        else:
            text = _placeholder_for(filename, speech)
            transcript_kind = "placeholder"
            confidence = 0.0
        return AsrResult(
            text=text,
            backend=self.name,
            real_model=False,
            language="zh",
            confidence=confidence,
            duration_seconds=round(duration, 3),
            transcript_kind=transcript_kind,
            notice=self.notice,
            probe=dict(probe),
        )


# --------------------------------------------------------------- Mock TTS
def speech_seconds(text: str) -> float:
    """一句话大概说多久：汉字约 0.22 秒，其它字符 0.1 秒。"""
    body = text or ""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
    others = len(re.sub(r"[\u4e00-\u9fff\s]", "", body))
    seconds = cjk * 0.22 + others * 0.1 + 0.3
    return max(0.4, min(6.0, round(seconds, 2)))


def render_wav(
    text: str,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    seconds: float | None = None,
) -> bytes:
    """用标准库 wave 生成单声道 16 bit PCM WAV：一句话一段可播放的"咕噜"声。"""
    seconds = seconds if seconds is not None else speech_seconds(text or "")
    frames = max(1, int(round(seconds * sample_rate)))
    digest = hashlib.sha256((text or "").encode("utf-8")).digest()
    base_freq = 150.0 + (digest[0] / 255.0) * 90.0
    wobble_freq = 3.0 + (digest[1] / 255.0) * 3.0
    amplitude = 0.28 * 32767.0
    fade = max(1, int(0.008 * sample_rate))
    samples = array.array("h")
    two_pi = 2.0 * math.pi
    for index in range(frames):
        t = index / sample_rate
        value = math.sin(two_pi * base_freq * t) * 0.75
        value += math.sin(two_pi * base_freq * 0.5 * t) * 0.2
        value += math.sin(two_pi * (base_freq * 2.0) * t) * 0.08
        value *= 1.0 + 0.12 * math.sin(two_pi * wobble_freq * t)
        gain = 1.0
        if index < fade:
            gain = index / fade
        elif index >= frames - fade:
            gain = max(0.0, (frames - index) / fade)
        samples.append(int(max(-1.0, min(1.0, value)) * amplitude * gain))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


class MockTTSBackend:
    """无模型验收后端：生成真实可播放的 WAV（标准库 wave），不做任何语音克隆。"""

    name = "mock"
    real_model = False

    def __init__(
        self,
        *,
        voice: str = DEFAULT_VOICE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        max_chars: int = DEFAULT_MAX_TTS_CHARS,
        notice: str = MOCK_NOTICE,
    ):
        self.voice = voice
        self.sample_rate = sample_rate
        self.max_chars = max_chars
        self.notice = notice
        self.calls = 0
        self.last_text = ""

    async def synthesize(self, text: str) -> TtsResult:
        text = (text or "").strip()
        if not text:
            raise EmptyAudio("要合成的文本为空")
        if len(text) > self.max_chars:
            raise TextTooLong(f"文本 {len(text)} 字，超过上限 {self.max_chars} 字")
        self.calls += 1
        self.last_text = text
        seconds = speech_seconds(text)
        audio = render_wav(text, sample_rate=self.sample_rate, seconds=seconds)
        return TtsResult(
            audio=audio,
            content_type=WAV_MIME,
            backend=self.name,
            real_model=False,
            voice=self.voice,
            duration_seconds=seconds,
            sample_rate=self.sample_rate,
            voice_tier="mock",
            notice=self.notice,
        )


class UnavailableASRBackend:
    """表示"真实 ASR 模型尚未安装/不可达"。存在即明确失败，绝不偷偷退回 Mock。"""

    name = "unavailable"
    real_model = True

    def __init__(self, reason: str = "本机未安装 ASR 模型（faster-whisper 未就位）"):
        self.reason = reason

    def probe(self, audio: bytes, *, filename: str) -> AudioProbe | ProbeFailure:
        # 结构探测用不着模型：仍按 WAV 解析，让"格式不对"比"后端没装"更早暴露
        return probe_wav(audio)

    async def transcribe(
        self, audio: bytes, *, filename: str, duration_seconds: float | None = None
    ) -> AsrResult:
        raise AsrUnavailable(self.reason)


class UnavailableTTSBackend:
    """表示"真实 TTS 音色尚未选定/未安装"。存在即明确失败，绝不偷偷退回 Mock。"""

    name = "unavailable"
    real_model = True

    def __init__(self, reason: str = "本机未安装 TTS 音色（Piper 未就位）"):
        self.reason = reason

    async def synthesize(self, text: str) -> TtsResult:
        raise TtsUnavailable(self.reason)



# --------------------------------------------------------------- 受管目录
class SpeechStore:
    """data/audio/ 受管目录：所有音频只能写进 data_dir/audio/ 下的相对路径。"""

    def __init__(self, root: Path | str, *, data_dir: Path | str | None = None):
        self.root = Path(root).resolve()
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else self.root.parent
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 路径 -------------------------------------------------------------
    def relative_to_data(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.data_dir).as_posix()

    def resolve(self, relative_path: str) -> Path:
        """把相对 data_dir 的路径解析成绝对路径；越界或越出 audio/ 一律拒绝。"""
        candidate = (self.data_dir / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise AudioPathError(f"音频路径越界：{relative_path}")
        return candidate

    def new_day_path(self, suffix: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d")
        folder = self.root / stamp
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{uuid.uuid4().hex}.{suffix or 'bin'}"

    # -- 写入 -------------------------------------------------------------
    def save_bytes(self, data: bytes, *, suffix: str) -> tuple[Path, str]:
        """把字节写成受管文件，返回 (绝对路径, 相对 data_dir 的 posix 路径)。"""
        target = self.new_day_path(suffix)
        target.write_bytes(data)
        return target, self.relative_to_data(target)

    def copy_from(self, source: Path, *, suffix: str) -> tuple[Path, str]:
        target = self.new_day_path(suffix)
        with Path(source).open("rb") as handle, target.open("wb") as out:
            while chunk := handle.read(1024 * 1024):
                out.write(chunk)
        return target, self.relative_to_data(target)

    # -- 留痕（JSON sidecar，不落 SQLite）---------------------------------
    @property
    def records_root(self) -> Path:
        folder = self.root / "records"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _record_path(self, record_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record_id or ""):
            raise AudioPathError("非法的音频记录 id")
        return self.records_root / f"{record_id}.json"

    def save_record(self, record: SpeechRecord, body: dict[str, Any] | None = None) -> None:
        payload = record.to_dict()
        if body:
            payload["body"] = body
        self._record_path(record.id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_record(self, record_id: str) -> dict[str, Any] | None:
        path = self._record_path(record_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list_records(self, limit: int = 50, *, kind: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        files = sorted(
            self.records_root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for path in files:
            if len(items) >= limit:
                break
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if kind and payload.get("kind") != kind:
                continue
            payload.pop("body", None)
            items.append(payload)
        return items


# --------------------------------------------------------------- 处理器
ChatFn = Callable[[str, str], Awaitable[Any]]

#: voice_turn 的总状态：ok 才代表"转写 + 回复 + 语音"全部成功。
TURN_OK = "ok"
TURN_ASR_FAILED = "asr_failed"
TURN_CHAT_UNAVAILABLE = "chat_unavailable"
TURN_CHAT_FAILED = "chat_failed"
TURN_TTS_DEGRADED = "tts_degraded"


class SpeechService:
    """把 ASR / chat / TTS 串成一级语音链路，并负责校验、落盘、留痕与错误映射。"""

    def __init__(
        self,
        db: Any,
        *,
        asr: ASRBackend | None = None,
        tts: TTSBackend | None = None,
        store: SpeechStore | None = None,
        chat: ChatFn | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_audio_seconds: float = DEFAULT_MAX_AUDIO_SECONDS,
        max_tts_chars: int = DEFAULT_MAX_TTS_CHARS,
        asr_timeout_seconds: float = DEFAULT_ASR_TIMEOUT_SECONDS,
        tts_timeout_seconds: float = DEFAULT_TTS_TIMEOUT_SECONDS,
    ):
        self.db = db
        self.asr = asr or MockASRBackend(max_duration_seconds=max_audio_seconds)
        self.tts = tts or MockTTSBackend(max_chars=max_tts_chars)
        if store is None:
            base = Path(getattr(db, "path", Path("data") / "lingjingjing.db")).parent
            store = SpeechStore(base / AUDIO_DIR_NAME, data_dir=base)
        self.store = store
        self.chat = chat
        self.max_upload_bytes = max_upload_bytes
        self.max_audio_seconds = max_audio_seconds
        self.max_tts_chars = max_tts_chars
        self.asr_timeout_seconds = asr_timeout_seconds
        self.tts_timeout_seconds = tts_timeout_seconds

    # -- 观测 -------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        asr_name = getattr(self.asr, "name", "unknown")
        tts_name = getattr(self.tts, "name", "unknown")
        mode = "mock" if asr_name == "mock" and tts_name == "mock" else "custom"
        return {
            "mode": mode,
            "asr": {
                "backend": asr_name,
                "real_model": bool(getattr(self.asr, "real_model", False)),
            },
            "tts": {
                "backend": tts_name,
                "real_model": bool(getattr(self.tts, "real_model", False)),
                "voice": getattr(self.tts, "voice", None),
            },
            "audio_root": str(self.store.root),
            "limits": {
                "max_upload_bytes": self.max_upload_bytes,
                "max_audio_seconds": self.max_audio_seconds,
                "max_tts_chars": self.max_tts_chars,
            },
            "notice": MOCK_NOTICE if mode == "mock" else "使用自定义语音后端",
        }

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self.db.append_event(ACTOR, kind, payload)
        except Exception:  # 留痕失败不能吞掉主流程，但也要留日志，不假装留过痕
            LOGGER.exception("语音事件写入失败：%s", kind)

    # -- ASR --------------------------------------------------------------
    def validate(self, audio: bytes, filename: str | None) -> dict[str, Any]:
        """上传前的确定性校验。任何不通过都抛带 code 的 SpeechError，不臆测文本。"""
        extension, basename = normalize_audio_name(filename)
        if not audio:
            raise EmptyAudio()
        if len(audio) > self.max_upload_bytes:
            raise AudioTooLarge(f"音频 {len(audio)} 字节，超过上限 {self.max_upload_bytes} 字节")
        if extension in _OTHER_AUDIO_EXTENSIONS:
            raise UnsupportedAudioFormat(
                f"v1 Mock 只解析 WAV；收到 .{extension}，请前端先转成 16kHz 单声道 WAV"
            )
        if extension not in _WAV_EXTENSIONS:
            raise UnsupportedAudioFormat(f"不是音频文件（{basename or '未命名'}）：需要扩展名 .wav")
        probe = self.asr.probe(audio, filename=basename)
        if not probe.get("readable"):
            raise InvalidAudio(f"音频无法解析：{probe.get('reason') or '未知原因'}")
        duration = float(probe.get("duration_seconds") or 0.0)
        if duration > self.max_audio_seconds:
            raise AudioTooLong(
                f"音频 {duration:.1f} 秒，超过上限 {self.max_audio_seconds:.0f} 秒"
            )
        return {"extension": extension, "filename": basename, "probe": dict(probe)}

    def _save_input(
        self, audio: bytes, meta: dict[str, Any], *, content_type: str | None
    ) -> SpeechRecord:
        _, relative = self.store.save_bytes(audio, suffix=meta["extension"])
        record = SpeechRecord(
            id=uuid.uuid4().hex,
            kind="input",
            path=relative,
            backend=getattr(self.asr, "name", "unknown"),
            real_model=bool(getattr(self.asr, "real_model", False)),
            created_at=_now_iso(),
            duration_seconds=float(meta["probe"].get("duration_seconds") or 0.0),
            size_bytes=len(audio),
            content_type=content_type,
            status="stored",
            probe=dict(meta["probe"]),
            extra={"filename": meta["filename"]},
        )
        self.store.save_record(record)
        return record

    def _finish_input(
        self,
        record: SpeechRecord,
        *,
        status: str,
        transcript: str | None = None,
        error: str | None = None,
    ) -> None:
        record.status = status
        record.transcript = transcript
        record.error = error
        self.store.save_record(record)

    async def transcribe_upload(
        self,
        audio: bytes,
        *,
        filename: str | None = None,
        source: str = "hold_to_talk",
        client_id: str = "desktop",
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """一级语音第一步：校验 -> 落受管目录 -> 转写。失败时也留痕（事件 + 拒绝记录）。"""
        try:
            meta = self.validate(audio, filename)
        except SpeechError as exc:
            self._event(
                "speech.asr.rejected",
                {
                    "code": exc.code,
                    "bytes": len(audio),
                    "filename": filename,
                    "client_id": client_id,
                },
            )
            raise
        record = self._save_input(audio, meta, content_type=content_type)
        duration = float(meta["probe"].get("duration_seconds") or 0.0)
        try:
            result = await asyncio.wait_for(
                self.asr.transcribe(audio, filename=meta["filename"], duration_seconds=duration),
                timeout=self.asr_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            self._finish_input(record, status="timeout", error="asr_timeout")
            self._event("speech.asr.error", {"record_id": record.id, "code": "asr_timeout"})
            raise AsrTimeout() from exc
        except SpeechError as exc:
            self._finish_input(record, status="error", error=exc.code)
            self._event(
                "speech.asr.error", {"record_id": record.id, "code": exc.code, "detail": exc.detail}
            )
            raise
        except Exception as exc:  # 真实后端可能抛任意异常，统一映射为 503
            LOGGER.exception("ASR 后端异常")
            self._finish_input(record, status="error", error="asr_backend_unavailable")
            self._event(
                "speech.asr.error",
                {"record_id": record.id, "code": "asr_backend_unavailable", "error": str(exc)},
            )
            raise AsrUnavailable(f"语音识别后端异常：{exc}") from exc

        text = (result.text or "").strip()
        if not text:
            # 后端说"没听清"却给了空串：按空音频处理，绝不兜底一句占位文本
            self._finish_input(record, status="error", error="empty_audio")
            self._event("speech.asr.error", {"record_id": record.id, "code": "empty_audio"})
            raise EmptyAudio("语音后端没有返回任何文字")
        self._finish_input(record, status="ok", transcript=text)
        payload = result.to_dict()
        payload.update(
            {
                "text": text,
                "status": "ok",
                "audio_id": record.id,
                "audio_path": record.path,
                "audio_url": f"/v1/speech/audio/{record.id}",
                "filename": meta["filename"],
                "source": source,
                "client_id": client_id,
                "real_model": bool(result.real_model),
                "notice": result.notice
                or (MOCK_NOTICE if not result.real_model else None),
            }
        )
        self._event(
            "speech.asr.ok",
            {
                "record_id": record.id,
                "text": text,
                "transcript_kind": result.transcript_kind,
                "backend": result.backend,
                "real_model": bool(result.real_model),
                "duration_seconds": duration,
                "source": source,
                "client_id": client_id,
            },
        )
        return payload

    # -- TTS --------------------------------------------------------------
    async def synthesize(
        self,
        text: str,
        *,
        source: str = "api",
        client_id: str = "desktop",
        chat: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise EmptyAudio("要合成的文本为空")
        if len(text) > self.max_tts_chars:
            raise TextTooLong(f"文本 {len(text)} 字，超过上限 {self.max_tts_chars} 字")
        try:
            result = await asyncio.wait_for(
                self.tts.synthesize(text), timeout=self.tts_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            self._event("speech.tts.error", {"code": "tts_timeout"})
            raise TtsTimeout() from exc
        except SpeechError as exc:
            self._event("speech.tts.error", {"code": exc.code, "detail": exc.detail})
            raise
        except Exception as exc:
            LOGGER.exception("TTS 后端异常")
            self._event(
                "speech.tts.error", {"code": "tts_backend_unavailable", "error": str(exc)}
            )
            raise TtsUnavailable(f"语音合成后端异常：{exc}") from exc

        audio = result.audio or b""
        if not audio:
            self._event("speech.tts.error", {"code": "tts_empty_result"})
            raise TtsUnavailable("语音合成后端返回了空音频")
        if detect_audio_format(audio) != "wav" or wav_duration(audio) is None:
            # 真实后端可能返回 mp3/ogg；v1 Mock 只产 WAV，收到别的就明确失败而不是假装成功
            self._event("speech.tts.error", {"code": "tts_bad_audio"})
            raise TtsUnavailable("语音合成后端返回的音频不是可解析的 WAV")
        _, relative = self.store.save_bytes(audio, suffix="wav")
        record = SpeechRecord(
            id=uuid.uuid4().hex,
            kind="output",
            path=relative,
            backend=result.backend,
            real_model=bool(result.real_model),
            created_at=_now_iso(),
            text=text,
            duration_seconds=result.duration_seconds,
            size_bytes=len(audio),
            content_type=result.content_type,
            status="ok",
            probe=dict(probe_wav(audio)),
            extra={"voice": result.voice, "voice_tier": result.voice_tier, "source": source},
        )
        self.store.save_record(record)
        voice_payload = {
            "status": "ok",
            "audio_id": record.id,
            "audio_url": f"/v1/speech/audio/{record.id}",
            "content_type": result.content_type,
            "backend": result.backend,
            "real_model": bool(result.real_model),
            "voice": result.voice,
            "voice_tier": result.voice_tier,
            "duration_seconds": round(result.duration_seconds, 3),
            "notice": result.notice,
        }
        if chat is not None:
            chat["voice"] = dict(voice_payload)
        payload = result.to_dict()
        payload.update(voice_payload)
        payload.update(
            {
                "audio_path": record.path,
                "text": text,
                "chars": len(text),
                "source": source,
                "client_id": client_id,
            }
        )
        self._event(
            "speech.tts.ok",
            {
                "record_id": record.id,
                "chars": len(text),
                "backend": result.backend,
                "real_model": bool(result.real_model),
                "voice": result.voice,
                "duration_seconds": result.duration_seconds,
                "source": source,
                "client_id": client_id,
            },
        )
        return payload

    # -- 一级语音完整链路 --------------------------------------------------
    async def voice_turn(
        self,
        audio: bytes,
        *,
        filename: str | None = None,
        client_id: str = "desktop",
        content_type: str | None = None,
        source: str = "hold_to_talk",
    ) -> dict[str, Any]:
        """按住说话 -> ASR -> 注入的 chat -> TTS。只有 status="ok" 代表全链路成功。

        任何一步失败都返回结构化降级信息（status != "ok" + error + notice），
        不做假的补全：ASR 失败时 transcript 为 None；只有 TTS 失败时 reply 仍保留文本。
        """
        response: dict[str, Any] = {
            "status": TURN_OK,
            "transcript": None,
            "reply": None,
            "asr": None,
            "chat": None,
            "audio": None,
            "error": None,
            "notice": None,
        }
        try:
            asr = await self.transcribe_upload(
                audio,
                filename=filename,
                source=source,
                client_id=client_id,
                content_type=content_type,
            )
        except SpeechError as exc:
            response.update(status=TURN_ASR_FAILED, error=exc.to_dict())
            response["notice"] = f"{exc.detail}。这一句没有转写，也没有继续对话，请重说一次。"
            return response

        response["transcript"] = asr["text"]
        response["asr"] = {
            key: asr.get(key)
            for key in (
                "backend",
                "real_model",
                "transcript_kind",
                "confidence",
                "duration_seconds",
                "audio_id",
                "audio_url",
                "notice",
            )
        }
        if self.chat is None:
            exc = ChatNotConfigured()
            response.update(status=TURN_CHAT_UNAVAILABLE, error=exc.to_dict())
            response["notice"] = "只完成了转写；对话链路未接线，所以没有回复也没有语音。"
            return response

        try:
            reply = await self.chat(asr["text"], client_id)
        except Exception as exc:  # 模型不可用等：保留转写，明确报错
            self._event(
                "speech.chat.error",
                {"record_id": asr.get("audio_id"), "error": f"{type(exc).__name__}: {exc}"},
            )
            error = _chat_error_payload(exc)
            response.update(status=TURN_CHAT_FAILED, error=error)
            response["notice"] = f"听清了「{asr['text']}」，但回复失败：{error['detail']}"
            return response

        reply_text = _reply_text(reply)
        chat_payload = _chat_payload(reply)
        response["reply"] = reply_text
        if chat_payload is not None:
            chat_payload["transcript"] = asr["text"]
            response["chat"] = chat_payload

        try:
            tts = await self.synthesize(
                reply_text, source="voice_reply", client_id=client_id, chat=chat_payload
            )
        except SpeechError as exc:
            # 文本回复已经成功；语音失败只降级这一半，不丢弃文本
            response.update(status=TURN_TTS_DEGRADED, error=exc.to_dict())
            response["notice"] = f"文字回复正常，但语音没合成出来：{exc.detail}"
            if chat_payload is not None:
                chat_payload["voice"] = {
                    "status": "unavailable",
                    "error": exc.code,
                    "detail": exc.detail,
                    "notice": response["notice"],
                }
            return response

        response["audio"] = {
            key: tts.get(key)
            for key in (
                "audio_id",
                "audio_url",
                "content_type",
                "duration_seconds",
                "backend",
                "real_model",
                "voice",
                "voice_tier",
                "notice",
            )
        }
        if not (asr.get("real_model") and tts.get("real_model")):
            response["notice"] = MOCK_NOTICE
        return response

    # -- 读取 --------------------------------------------------------------
    def open_audio(self, record_id: str) -> tuple[Path, dict[str, Any]]:
        record = self.store.load_record(record_id)
        if record is None:
            raise FileNotFoundError(record_id)
        path = self.store.resolve(record["path"])
        if not path.is_file():
            raise FileNotFoundError(record_id)
        return path, record

    def list_records(self, limit: int = 50, *, kind: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_records(limit, kind=kind)


def _reply_text(reply: Any) -> str:
    if isinstance(reply, str):
        return reply.strip()
    for attribute in ("reply", "text", "content"):
        value = getattr(reply, attribute, None)
        if isinstance(value, str):
            return value.strip()
    if isinstance(reply, Mapping):
        for key in ("reply", "text", "content"):
            value = reply.get(key)
            if isinstance(value, str):
                return value.strip()
    return str(reply).strip()


def _chat_payload(reply: Any) -> dict[str, Any] | None:
    """把 chat 结果整理成可序列化 dict；ChatResponse 的字段全部保留。"""
    if reply is None or isinstance(reply, str):
        return None
    if hasattr(reply, "model_dump"):
        try:
            return dict(reply.model_dump())
        except Exception:
            return None
    if isinstance(reply, Mapping):
        return dict(reply)
    return None


def _chat_error_payload(exc: Exception) -> dict[str, Any]:
    name = type(exc).__name__
    detail = str(exc) or name
    if name == "ModelUnavailable":
        return {"code": "model_unavailable", "http_status": 503, "detail": detail}
    if isinstance(exc, ValueError):
        return {"code": "chat_rejected", "http_status": 422, "detail": detail}
    return {"code": "chat_failed", "http_status": 503, "detail": f"{name}: {detail}"}


# --------------------------------------------------------------- 组装
def backend_kind(name: str | None) -> str:
    """配置解析：v1 只有 mock / unavailable；真实 faster-whisper、Piper 未安装。"""
    value = (name or "mock").strip().lower()
    if value in {"", "mock", "none"}:
        return "mock"
    if value in {"unavailable", "off", "disabled"}:
        return "unavailable"
    raise ValueError(f"未知语音后端：{name}（v1 只有 mock / unavailable，真实模型尚未安装）")


def _get(settings: Any, key: str, default: Any) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(key, default)
    return getattr(settings, key, default)


def _env(name: str) -> str | None:
    return os.getenv(f"LING_SPEECH_{name.upper()}")


def build_speech_service(
    settings: Any,
    db: Any,
    *,
    asr: ASRBackend | None = None,
    tts: TTSBackend | None = None,
    store: SpeechStore | None = None,
    chat: ChatFn | None = None,
) -> SpeechService:
    """从 Settings（或同名字典/环境变量）组装语音服务。默认 = MockASR + MockTTS。"""
    data_dir = Path(_get(settings, "data_dir", Path("data"))).resolve()
    asr_kind = backend_kind(_get(settings, "speech_asr_backend", None) or _env("asr_backend"))
    tts_kind = backend_kind(_get(settings, "speech_tts_backend", None) or _env("tts_backend"))
    max_audio_seconds = float(
        _get(settings, "speech_max_audio_seconds", None) or _env("max_audio_seconds") or DEFAULT_MAX_AUDIO_SECONDS
    )
    max_upload_bytes = int(
        _get(settings, "speech_max_upload_bytes", None) or _env("max_upload_bytes") or DEFAULT_MAX_UPLOAD_BYTES
    )
    max_tts_chars = int(
        _get(settings, "speech_max_tts_chars", None) or _env("max_tts_chars") or DEFAULT_MAX_TTS_CHARS
    )
    asr_timeout = float(
        _get(settings, "speech_asr_timeout_seconds", None) or _env("asr_timeout_seconds") or DEFAULT_ASR_TIMEOUT_SECONDS
    )
    tts_timeout = float(
        _get(settings, "speech_tts_timeout_seconds", None) or _env("tts_timeout_seconds") or DEFAULT_TTS_TIMEOUT_SECONDS
    )
    if asr is None:
        asr = (
            MockASRBackend(max_duration_seconds=max_audio_seconds)
            if asr_kind == "mock"
            else UnavailableASRBackend()
        )
    if tts is None:
        tts = MockTTSBackend(max_chars=max_tts_chars) if tts_kind == "mock" else UnavailableTTSBackend()
    if store is None:
        store = SpeechStore(data_dir / AUDIO_DIR_NAME, data_dir=data_dir)
    return SpeechService(
        db,
        asr=asr,
        tts=tts,
        store=store,
        chat=chat,
        max_upload_bytes=max_upload_bytes,
        max_audio_seconds=max_audio_seconds,
        max_tts_chars=max_tts_chars,
        asr_timeout_seconds=asr_timeout,
        tts_timeout_seconds=tts_timeout,
    )


__all__ = [
    "ACTOR",
    "ASRBackend",
    "AUDIO_DIR_NAME",
    "AsrResult",
    "AsrTimeout",
    "AsrUnavailable",
    "AudioPathError",
    "AudioProbe",
    "AudioTooLarge",
    "AudioTooLong",
    "ChatFn",
    "ChatNotConfigured",
    "DEFAULT_ASR_TIMEOUT_SECONDS",
    "DEFAULT_MAX_AUDIO_SECONDS",
    "DEFAULT_MAX_TTS_CHARS",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_TTS_TIMEOUT_SECONDS",
    "DEFAULT_VOICE",
    "EmptyAudio",
    "InvalidAudio",
    "MOCK_NOTICE",
    "MockASRBackend",
    "MockTTSBackend",
    "NoiseOnly",
    "ProbeFailure",
    "ReplyFailed",
    "SpeechBackendUnavailable",
    "SpeechError",
    "SpeechRecord",
    "SpeechService",
    "SpeechStore",
    "TTSBackend",
    "TURN_ASR_FAILED",
    "TURN_CHAT_FAILED",
    "TURN_CHAT_UNAVAILABLE",
    "TURN_OK",
    "TURN_TTS_DEGRADED",
    "TextTooLong",
    "TtsResult",
    "TtsTimeout",
    "TtsUnavailable",
    "UnavailableASRBackend",
    "UnavailableTTSBackend",
    "WAV_MIME",
    "backend_kind",
    "build_speech_service",
    "detect_audio_format",
    "normalize_audio_name",
    "probe_wav",
    "read_audio_metadata",
    "render_wav",
    "speech_seconds",
    "wav_duration",
]
