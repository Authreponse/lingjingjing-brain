"""task-9 M3 一级语音：ASR/TTS 抽象、Mock 后端、错误路径、路由契约与完整链路集成测试。

本机没有 Whisper/TTS 模型，这里全部跑在 Mock 后端上：**只证明架构与错误语义成立**，
不构成真实语音识别/合成的验证。真实模型接入后必须另做真人与真模型验收。
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import random
import struct
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from lingbrain.db import Database
from lingbrain.service import BrainService
from lingbrain.models import EchoModel, ModelUnavailable
from lingbrain.speech import (
    AUDIO_DIR_NAME,
    DEFAULT_MAX_TTS_CHARS,
    MOCK_NOTICE,
    AsrResult,
    AsrTimeout,
    AsrUnavailable,
    AudioPathError,
    AudioTooLarge,
    AudioTooLong,
    EmptyAudio,
    InvalidAudio,
    MockASRBackend,
    MockTTSBackend,
    NoiseOnly,
    SpeechError,
    SpeechService,
    SpeechStore,
    TtsResult,
    TtsTimeout,
    TtsUnavailable,
    UnavailableASRBackend,
    UnavailableTTSBackend,
    build_speech_service,
    detect_audio_format,
    normalize_audio_name,
    probe_wav,
    render_wav,
    speech_seconds,
    wav_duration,
)
from lingbrain.speech_routes import HARD_UPLOAD_LIMIT_BYTES, register_speech_routes

TOKEN = "test-token"
HEADERS = {"X-Ling-Token": TOKEN}


# --------------------------------------------------------------- 音频夹具
def make_wav(
    seconds: float = 1.0,
    *,
    sample_rate: int = 16000,
    amplitude: float = 0.3,
    freq: float = 220.0,
    channels: int = 1,
    sample_width: int = 2,
    seed: int = 0,
) -> bytes:
    """生成一段可解码的 WAV。amplitude=0 就是纯静音。"""
    rng = random.Random(seed)
    frames = max(1, int(seconds * sample_rate))
    max_value = (1 << (8 * sample_width - 1)) - 1
    chunk = bytearray()
    for index in range(frames):
        value = amplitude * max_value * math.sin(2 * math.pi * freq * index / sample_rate)
        if seed:
            value += amplitude * max_value * 0.2 * (rng.random() * 2 - 1)
        value = int(max(-max_value - 1, min(max_value, value)))
        if sample_width == 2:
            sample = struct.pack("<h", value)
        elif sample_width == 1:
            sample = struct.pack("<B", max(0, min(255, value + 128)))
        else:
            sample = value.to_bytes(sample_width, "little", signed=True)
        chunk += sample * channels
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(chunk))
    return buffer.getvalue()


def noise_wav(seconds: float = 1.0, *, level: float = 0.006, sample_rate: int = 16000) -> bytes:
    """底噪/嗡声：每个 20ms 窗口都高于静音阈值（60），但整体 RMS 低于噪声阈值（300）。

    语义是"确实有能量，但没有可识别的语音成分"：MockASR 必须报 audio_noise，
    而不是编造文本；如果电平再高，就变成"能当声音处理"的输入了。
    """
    rng = random.Random(7)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = [int(rng.uniform(-1, 1) * level * 32767) for _ in range(int(seconds * sample_rate))]
        handle.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return buffer.getvalue()


def truncated_wav(data: bytes, *, keep: int = 64) -> bytes:
    """只保留文件头但把 data 块声明得很大：解析时会被判成截断。"""
    header = bytearray(data[:keep])
    return bytes(header)


def run(coro):
    return asyncio.run(coro)


AUDIO_CASES_LAZY = {
    "empty": lambda: b"",
    "not-audio": lambda: b"hello",
    "silent": lambda: make_wav(1.0, amplitude=0.0),
    "noise": lambda: noise_wav(1.0),
    "wav": lambda: make_wav(1.0),
    "oversize": lambda: b"\x00" * (HARD_UPLOAD_LIMIT_BYTES + 1),
}


# --------------------------------------------------------------- 夹具
@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(tmp_path / "speech.db")
    yield database
    database.close()


def service_for(db: Database, **overrides) -> SpeechService:
    base = Path(db.path).parent
    return SpeechService(
        db,
        store=SpeechStore(base / AUDIO_DIR_NAME, data_dir=base),
        **overrides,
    )


def make_client(service: SpeechService) -> TestClient:
    app = FastAPI()

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            raise HTTPException(status_code=401, detail="认证失败")

    register_speech_routes(app, service, authorized)
    return TestClient(app)


# =============================================================== MockASR
def test_mock_asr_transcribes_filename_deterministically():
    backend = MockASRBackend()
    audio = make_wav(1.0)

    async def scenario():
        first = await backend.transcribe(audio, filename="按住说话.wav")
        second = await backend.transcribe(audio, filename="按住说话.wav")
        return first, second

    first, second = run(scenario())
    assert first.text == "按住说话"
    assert first.text == second.text
    assert first.transcript_kind == "speech"
    assert first.real_model is False
    assert first.notice == MOCK_NOTICE
    assert first.duration_seconds == pytest.approx(1.0, abs=0.01)
    assert first.confidence and 0 < first.confidence <= 1


def test_mock_asr_uses_placeholder_for_machine_like_names():
    backend = MockASRBackend()

    async def scenario():
        return await backend.transcribe(make_wav(1.0), filename="audio_001.wav")

    result = run(scenario())
    assert result.transcript_kind == "placeholder"
    assert "Mock 占位转写" in result.text
    assert result.confidence == 0.0


def test_mock_asr_empty_and_silent_audio_raise_without_text():
    backend = MockASRBackend()

    async def scenario():
        with pytest.raises(EmptyAudio):
            await backend.transcribe(b"", filename="按住说话.wav")
        # 1 秒纯静音：时长够，但没有声音
        with pytest.raises(EmptyAudio):
            await backend.transcribe(make_wav(1.0, amplitude=0.0), filename="按住说话.wav")
        # 只有 10ms 的声音，低于 min_speech_seconds
        with pytest.raises(EmptyAudio):
            await backend.transcribe(make_wav(0.01, amplitude=0.3), filename="按住说话.wav")
        # 静音 1s + 10ms 咔哒：有声时长仍不够
        with pytest.raises(EmptyAudio):
            await backend.transcribe(
                make_wav(1.0, amplitude=0.0)[:44] + make_wav(0.01, amplitude=0.5)[44:] + b"\x00" * 31956,
                filename="按住说话.wav",
            )

    run(scenario())


def test_mock_asr_noise_and_broken_wav_are_rejected():
    backend = MockASRBackend()

    async def scenario():
        with pytest.raises(NoiseOnly):
            await backend.transcribe(noise_wav(1.0), filename="按住说话.wav")
        with pytest.raises(InvalidAudio):
            await backend.transcribe(b"RIFF\x00\x00\x00\x00WAVE", filename="按住说话.wav")
        with pytest.raises(InvalidAudio):
            await backend.transcribe(truncated_wav(make_wav(1.0)), filename="按住说话.wav")

    run(scenario())


def test_mock_asr_rejects_too_long_audio():
    backend = MockASRBackend(max_duration_seconds=2.0)

    async def scenario():
        with pytest.raises(AudioTooLong):
            await backend.transcribe(make_wav(3.0), filename="按住说话.wav")

    run(scenario())


def test_probe_wav_reads_metadata_and_detects_truncation():
    audio = make_wav(0.5, sample_rate=8000, channels=2, sample_width=2)
    probe = probe_wav(audio)
    assert probe["readable"] is True
    assert probe["duration_seconds"] == pytest.approx(0.5, abs=0.01)
    assert probe["channels"] == 2
    assert probe["sample_rate"] == 8000
    assert probe_wav(truncated_wav(audio))["readable"] is False
    assert probe_wav(b"not a wav")["readable"] is False
    assert wav_duration(audio) == pytest.approx(0.5, abs=0.01)
    assert wav_duration(b"garbage") is None


def test_probe_wav_supports_8_and_24_bit_pcm():
    for width in (1, 2, 3):
        probe = probe_wav(make_wav(0.2, sample_width=width))
        assert probe["readable"] is True, width
        assert probe["sample_width"] == width


def test_detect_audio_format_by_magic_bytes():
    assert detect_audio_format(make_wav(0.1)) == "wav"
    assert detect_audio_format(b"ID3\x04\x00") == "mp3"
    assert detect_audio_format(b"OggS\x00\x02") == "ogg"
    assert detect_audio_format(b"") == "empty"
    assert detect_audio_format(b"hello", filename="a.wav") == "wav"


# =============================================================== MockTTS
def test_mock_tts_produces_playable_wav():
    async def scenario():
        return await MockTTSBackend().synthesize("我在呢，今天过得怎么样？")

    result = run(scenario())
    assert isinstance(result, TtsResult)
    assert result.audio[:4] == b"RIFF"
    with wave.open(io.BytesIO(result.audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == result.sample_rate
        frames = handle.getnframes()
        assert frames > 0
        samples = struct.unpack(f"<{frames}h", handle.readframes(frames))
    assert max(abs(value) for value in samples) > 1000  # 真的有声，不是全零占位
    assert result.duration_seconds == pytest.approx(frames / result.sample_rate, abs=0.01)
    assert result.real_model is False
    assert result.voice_tier == "mock"
    assert result.notice == MOCK_NOTICE


def test_mock_tts_is_deterministic_and_length_driven():
    async def scenario():
        backend = MockTTSBackend()
        short = await backend.synthesize("嗯。")
        long_text = await backend.synthesize("这是一段很长很长的话，" * 3)
        again = await backend.synthesize("嗯。")
        return short, long_text, again

    short, long, again = run(scenario())
    assert short.audio == again.audio
    assert short.duration_seconds < long.duration_seconds
    assert speech_seconds("嗯。") < speech_seconds("这是一段很长很长的话")


def test_mock_tts_rejects_empty_and_overlong_text():
    async def scenario():
        backend = MockTTSBackend(max_chars=10)
        with pytest.raises(EmptyAudio):
            await backend.synthesize("   ")
        with pytest.raises(SpeechError) as info:
            await backend.synthesize("一" * 11)
        return info.value

    exc = run(scenario())
    assert exc.code == "text_too_long"


# =============================================================== 处理器：ASR
def test_transcribe_upload_stores_audio_and_writes_events(db):
    service = service_for(db)
    audio = make_wav(1.2)
    result = run(
        service.transcribe_upload(audio, filename="按住说话.wav", client_id="desktop")
    )
    assert result["status"] == "ok"
    assert result["text"] == "按住说话"
    assert result["real_model"] is False
    assert result["notice"] == MOCK_NOTICE
    assert result["audio_path"].startswith(f"{AUDIO_DIR_NAME}/")
    stored = Path(db.path).parent / result["audio_path"]
    assert stored.is_file()
    assert stored.read_bytes() == audio
    kinds = [item["kind"] for item in db.list_events(20)]
    assert "speech.asr.ok" in kinds


@pytest.mark.parametrize(
    "case, filename, expected, code",
    [
        ("empty", "按住说话.wav", EmptyAudio, "empty_audio"),
        ("empty", None, EmptyAudio, "empty_audio"),
        ("silent", "按住说话.wav", EmptyAudio, "empty_audio"),
        ("noise", "按住说话.wav", NoiseOnly, "audio_noise"),
        ("not-audio", "voice.wav", InvalidAudio, "audio_invalid"),
        ("not-audio", "notes.txt", SpeechError, "audio_unsupported_format"),
    ],
)
def test_transcribe_upload_error_paths_do_not_invent_text(db, case, filename, expected, code):
    service = service_for(db)
    audio = AUDIO_CASES_LAZY[case]()
    with pytest.raises(expected) as info:
        run(service.transcribe_upload(audio, filename=filename))
    assert info.value.code == code
    # 失败时绝不留下一段"编造"的转写
    for item in service.list_records(limit=50):
        assert not item.get("transcript")


def test_transcribe_upload_rejects_unsupported_audio_and_other_files(db):
    service = service_for(db)
    with pytest.raises(SpeechError) as mp3:
        run(service.transcribe_upload(b"ID3\x04\x00fake", filename="voice.mp3"))
    assert mp3.value.code == "audio_unsupported_format"
    assert mp3.value.http_status == 415

    with pytest.raises(SpeechError) as text:
        run(service.transcribe_upload(b"hello", filename="notes.txt"))
    assert text.value.code == "audio_unsupported_format"

    with pytest.raises(SpeechError) as no_name:
        run(service.transcribe_upload(make_wav(0.5)))
    assert no_name.value.code == "audio_unsupported_format"

    # 失败也要留痕：事件里能看到拒绝码
    rejected = [e for e in db.list_events(20) if e["kind"] == "speech.asr.rejected"]
    assert {e["payload"]["code"] for e in rejected} == {"audio_unsupported_format"}


def test_transcribe_upload_rejects_too_long_and_too_large(db):
    service = service_for(db, max_audio_seconds=2.0, max_upload_bytes=100_000)
    with pytest.raises(AudioTooLong) as too_long:
        run(service.transcribe_upload(make_wav(3.0), filename="按住说话.wav"))
    assert too_long.value.http_status == 422

    service2 = service_for(db, max_upload_bytes=1000)
    with pytest.raises(AudioTooLarge) as too_large:
        run(service2.transcribe_upload(make_wav(1.0), filename="按住说话.wav"))
    assert too_large.value.http_status == 413


def test_transcribe_upload_routes_unavailable_backend_to_503(db):
    service = service_for(db, asr=UnavailableASRBackend())
    with pytest.raises(AsrUnavailable) as info:
        run(service.transcribe_upload(make_wav(1.0), filename="按住说话.wav"))
    assert info.value.http_status == 503
    assert info.value.code == "asr_backend_unavailable"
    # 音频仍然落盘 + 留痕（明确记成失败，不记成成功）
    records = service.list_records(limit=10)
    assert records and records[0]["status"] == "error"
    assert records[0]["error"] == "asr_backend_unavailable"


def test_transcribe_upload_maps_backend_exception_to_503(db):
    class Exploding:
        name = "broken"
        real_model = True

        def probe(self, audio, *, filename):
            return probe_wav(audio)

        async def transcribe(self, audio, *, filename, duration_seconds=None):
            raise RuntimeError("cuda out of memory")

    service = service_for(db, asr=Exploding())
    with pytest.raises(AsrUnavailable):
        run(service.transcribe_upload(make_wav(1.0), filename="按住说话.wav"))


def test_transcribe_upload_timeout_is_explicit(db):
    class Slow:
        name = "slow"
        real_model = True

        def probe(self, audio, *, filename):
            return probe_wav(audio)

        async def transcribe(self, audio, *, filename, duration_seconds=None):
            await asyncio.sleep(5)
            return AsrResult(text="不该出现")

    service = service_for(db, asr=Slow(), asr_timeout_seconds=0.05)
    with pytest.raises(AsrTimeout) as info:
        run(service.transcribe_upload(make_wav(1.0), filename="按住说话.wav"))
    assert info.value.http_status == 504
    records = service.list_records(limit=5)
    assert records[0]["status"] == "timeout"


def test_blank_backend_text_is_never_turned_into_a_fake_transcript(db):
    class Quiet:
        name = "quiet"
        real_model = True

        def probe(self, audio, *, filename):
            return probe_wav(audio)

        async def transcribe(self, audio, *, filename, duration_seconds=None):
            return AsrResult(text="   ")

    service = service_for(db, asr=Quiet())
    with pytest.raises(EmptyAudio):
        run(service.transcribe_upload(make_wav(1.0), filename="按住说话.wav"))


# =============================================================== 处理器：TTS
def test_synthesize_stores_wav_in_managed_dir(db):
    service = service_for(db)
    result = run(service.synthesize("我在呢。"))
    assert result["status"] == "ok"
    assert result["audio_url"] == f"/v1/speech/audio/{result['audio_id']}"
    path = Path(db.path).parent / result["audio_path"]
    assert path.is_file()
    assert path.suffix == ".wav"
    assert path.read_bytes()[:4] == b"RIFF"
    assert result["real_model"] is False
    assert any(e["kind"] == "speech.tts.ok" for e in db.list_events(10))


def test_synthesize_error_paths(db):
    service = service_for(db, max_tts_chars=5)
    with pytest.raises(EmptyAudio):
        run(service.synthesize("  "))
    with pytest.raises(SpeechError) as info:
        run(service.synthesize("一" * 6))
    assert info.value.code == "text_too_long"

    unavailable = service_for(db, tts=UnavailableTTSBackend())
    with pytest.raises(TtsUnavailable) as info2:
        run(unavailable.synthesize("你好"))
    assert info2.value.http_status == 503


def test_synthesize_rejects_binary_junk_from_backend(db):
    class Junk:
        name = "junk"
        real_model = True

        async def synthesize(self, text):
            return TtsResult(audio=b"\x89PNG\r\n\x1a\nnot audio", backend="junk")

    service = service_for(db, tts=Junk())
    with pytest.raises(TtsUnavailable):
        run(service.synthesize("你好"))


def test_synthesize_timeout_is_explicit(db):
    class Slow:
        name = "slow"
        real_model = True

        async def synthesize(self, text):
            await asyncio.sleep(5)
            return TtsResult(audio=render_wav("迟到的声音"))

    service = service_for(db, tts=Slow(), tts_timeout_seconds=0.05)
    with pytest.raises(TtsTimeout) as info:
        run(service.synthesize("你好"))
    assert info.value.http_status == 504


# =============================================================== 受管目录
def test_store_rejects_paths_outside_managed_dir(tmp_path):
    root = tmp_path / "data"
    store = SpeechStore(root / AUDIO_DIR_NAME, data_dir=root)
    with pytest.raises(AudioPathError):
        store.resolve("../../etc/passwd")
    with pytest.raises(AudioPathError):
        store.resolve("lingjingjing.db")
    with pytest.raises(AudioPathError):
        store.load_record("../escape")
    assert store.resolve("audio/20260101/a.wav").name == "a.wav"


def test_normalize_audio_name_strips_paths():
    assert normalize_audio_name("C:\\tmp\\按住说话.wav") == ("wav", "按住说话.wav")
    assert normalize_audio_name("../../evil.wav") == ("wav", "evil.wav")
    assert normalize_audio_name(None) == ("", "")
    assert normalize_audio_name("bad\x00name.wav")[0] == "wav"


# =============================================================== 一级语音链路
class RecordingChat:
    """可注入的 chat 替身：记录被调用的原文，按需失败。"""

    def __init__(self, *, reply="我在呢，刚才那句话我听清了。", raises=None):
        self.calls: list[tuple[str, str]] = []
        self.reply = reply
        self.raises = raises

    async def __call__(self, message: str, client_id: str):
        self.calls.append((message, client_id))
        if self.raises is not None:
            raise self.raises
        return {"reply": self.reply, "user_event_id": "u1", "assistant_event_id": "a1"}


def test_voice_turn_full_chain_audio_to_asr_to_chat_to_tts(db):
    chat = RecordingChat()
    service = service_for(db, chat=chat)
    audio = make_wav(1.0)

    result = run(service.voice_turn(audio, filename="按住说话.wav", client_id="pet"))

    assert result["status"] == "ok"
    assert result["transcript"] == "按住说话"
    assert chat.calls == [("按住说话", "pet")], "转写结果必须原样进入 chat 链路"
    assert result["reply"] == "我在呢，刚才那句话我听清了。"
    assert result["chat"]["transcript"] == "按住说话"
    # 文本 + 语音都在：语音链接指向受管目录里真实存在的 WAV
    voice = result["audio"]
    assert voice["real_model"] is False
    assert voice["voice_tier"] == "mock"
    audio_file = Path(db.path).parent / service.store.load_record(voice["audio_id"])["path"]
    assert audio_file.is_file()
    assert wav_duration(audio_file.read_bytes()) and wav_duration(audio_file.read_bytes()) > 0
    assert result["chat"]["voice"]["audio_id"] == voice["audio_id"]
    assert result["notice"] == MOCK_NOTICE


def test_voice_turn_asr_failure_stops_chain_without_reply(db):
    chat = RecordingChat()
    service = service_for(db, chat=chat)

    result = run(service.voice_turn(noise_wav(1.0), filename="按住说话.wav"))

    assert result["status"] == "asr_failed"
    assert result["transcript"] is None
    assert result["reply"] is None
    assert result["error"]["code"] == "audio_noise"
    assert chat.calls == [], "没听清就不能继续对话"

    empty = run(service.voice_turn(b"", filename="按住说话.wav"))
    assert empty["status"] == "asr_failed"
    assert empty["error"]["code"] == "empty_audio"


def test_voice_turn_backend_unavailable_is_reported(db):
    chat = RecordingChat()
    service = service_for(db, asr=UnavailableASRBackend(), chat=chat)
    result = run(service.voice_turn(make_wav(1.0), filename="按住说话.wav"))
    assert result["status"] == "asr_failed"
    assert result["error"]["code"] == "asr_backend_unavailable"
    assert result["error"]["http_status"] == 503
    assert chat.calls == []


def test_voice_turn_without_chat_reports_chat_unavailable(db):
    service = service_for(db)
    result = run(service.voice_turn(make_wav(1.0), filename="按住说话.wav"))
    assert result["status"] == "chat_unavailable"
    assert result["transcript"] == "按住说话"
    assert result["error"]["code"] == "chat_not_configured"


def test_voice_turn_chat_failure_keeps_transcript(db):
    chat = RecordingChat(raises=ModelUnavailable("模型未就绪"))
    service = service_for(db, chat=chat)
    result = run(service.voice_turn(make_wav(1.0), filename="按住说话.wav"))
    assert result["status"] == "chat_failed"
    assert result["transcript"] == "按住说话"
    assert result["reply"] is None
    assert result["error"]["code"] == "model_unavailable"
    assert result["error"]["http_status"] == 503
    assert "模型未就绪" in result["notice"]


def test_voice_turn_tts_failure_degrades_only_voice(db):
    chat = RecordingChat()
    service = service_for(db, chat=chat, tts=UnavailableTTSBackend())
    result = run(service.voice_turn(make_wav(1.0), filename="按住说话.wav"))
    assert result["status"] == "tts_degraded"
    assert result["reply"] == "我在呢，刚才那句话我听清了。"
    assert result["audio"] is None
    assert result["error"]["code"] == "tts_backend_unavailable"
    assert result["chat"]["voice"]["status"] == "unavailable"


def test_voice_turn_with_real_brain_service_reaches_db(tmp_path):
    """集成：真实 BrainService + EchoModel 装配，转写文本真的进了事件与记忆链路。"""
    db = Database(tmp_path / "brain.db")
    brain = BrainService(db, EchoModel())
    service = SpeechService(
        db,
        store=SpeechStore(tmp_path / AUDIO_DIR_NAME, data_dir=tmp_path),
        chat=brain.chat,
    )
    result = run(
        service.voice_turn(make_wav(1.2), filename="我叫小鲸.wav", client_id="desktop")
    )
    assert result["status"] == "ok"
    assert result["transcript"] == "我叫小鲸"
    assert result["chat"]["reply"], "BrainService 必须真的回复"
    assert result["chat"]["memories"], "走的是同一套记忆提取链路"
    messages = [e for e in db.list_events(30) if e["kind"] == "user.message"]
    assert messages[0]["payload"]["content"] == "我叫小鲸"
    assert result["audio"]["audio_url"].startswith("/v1/speech/audio/")
    db.close()


# =============================================================== 路由契约
def test_asr_route_requires_token(db):
    client = make_client(service_for(db))
    response = client.post(
        "/v1/speech/asr?filename=a.wav", content=make_wav(1.0), headers={"Content-Type": "audio/wav"}
    )
    assert response.status_code == 401


def test_asr_route_returns_transcript_and_audio_url(db):
    client = make_client(service_for(db))
    response = client.post(
        "/v1/speech/asr?filename=%E6%8C%89%E4%BD%8F%E8%AF%B4%E8%AF%9D.wav&client_id=desktop",
        content=make_wav(1.0),
        headers={**HEADERS, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "按住说话"
    assert body["real_model"] is False
    assert body["notice"] == MOCK_NOTICE
    audio = client.get(body["audio_url"], headers=HEADERS)
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.content == make_wav(1.0)


@pytest.mark.parametrize(
    "case, filename, content_type, status, code",
    [
        ("empty", "按住说话.wav", "audio/wav", 422, "empty_audio"),
        ("not-audio", "notes.txt", "audio/wav", 415, "audio_unsupported_format"),
        ("silent", "按住说话.wav", "audio/wav", 422, "empty_audio"),
        ("noise", "按住说话.wav", "audio/wav", 422, "audio_noise"),
        ("wav", "按住说话.wav", "text/plain", 415, "unsupported_media_type"),
    ],
)
def test_asr_route_error_contract(db, case, filename, content_type, status, code):
    payload = AUDIO_CASES_LAZY[case]()
    client = make_client(service_for(db))
    response = client.post(
        f"/v1/speech/asr?filename={filename}",
        content=payload,
        headers={**HEADERS, "Content-Type": content_type},
    )
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["status"] == "error"
    assert detail["error"]["code"] == code
    assert detail["error"]["http_status"] == status
    assert "text" not in detail, "错误响应里绝不能带编造的转写"


def test_asr_route_413_for_oversized_upload(db):
    """超大请求体只按 Content-Length 拒绝（TestClient 在 Windows 上通过环境变量发 body，
    32KB 以上会先在传输层失败，所以这里直接驱动 ASGI 应用验证 413 分支）。"""
    app = FastAPI()

    async def authorized(x_ling_token: str = Header(default="")) -> None:
        if x_ling_token != TOKEN:
            raise HTTPException(status_code=401, detail="认证失败")

    register_speech_routes(app, service_for(db), authorized)
    statuses: list[int] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/speech/asr",
        "raw_path": b"/v1/speech/asr",
        "query_string": "filename=%E6%8C%89%E4%BD%8F%E8%AF%B4%E8%AF%9D.wav".encode(),
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"x-ling-token", TOKEN.encode()),
            (b"content-type", b"audio/wav"),
            (b"content-length", str(HARD_UPLOAD_LIMIT_BYTES + 1).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    assert statuses == [413]


def test_asr_route_503_when_backend_unavailable(db):
    client = make_client(service_for(db, asr=UnavailableASRBackend()))
    response = client.post(
        "/v1/speech/asr?filename=按住说话.wav",
        content=make_wav(1.0),
        headers={**HEADERS, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "asr_backend_unavailable"


def test_tts_route_returns_audio_metadata(db):
    client = make_client(service_for(db))
    response = client.post("/v1/speech/tts", json={"text": "我在呢。"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["real_model"] is False
    assert body["content_type"] == "audio/wav"
    audio = client.get(body["audio_url"], headers=HEADERS)
    assert audio.status_code == 200
    with wave.open(io.BytesIO(audio.content), "rb") as handle:
        assert handle.getnframes() > 0


def test_tts_route_error_paths(db):
    client = make_client(service_for(db, max_tts_chars=5))
    too_long = client.post("/v1/speech/tts", json={"text": "一" * 6}, headers=HEADERS)
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["error"]["code"] == "text_too_long"

    blank = client.post("/v1/speech/tts", json={"text": ""}, headers=HEADERS)
    assert blank.status_code == 422  # pydantic min_length 挡住

    unavailable = make_client(service_for(db, tts=UnavailableTTSBackend()))
    response = unavailable.post("/v1/speech/tts", json={"text": "你好"}, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["code"] == "tts_backend_unavailable"


def test_voice_turn_route_full_chain(db):
    chat = RecordingChat(reply="我在呢。")
    client = make_client(service_for(db, chat=chat))
    response = client.post(
        "/v1/speech/voice-turn?filename=按住说话.wav",
        content=make_wav(1.0),
        headers={**HEADERS, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["transcript"] == "按住说话"
    assert body["reply"] == "我在呢。"
    assert body["audio"]["audio_url"].startswith("/v1/speech/audio/")


def test_voice_turn_route_asr_and_chat_failures_use_http_status(db):
    chat = RecordingChat(raises=ModelUnavailable("模型未就绪"))
    client = make_client(service_for(db, chat=chat))
    bad = client.post(
        "/v1/speech/voice-turn?filename=按住说话.wav",
        content=noise_wav(1.0),
        headers={**HEADERS, "Content-Type": "audio/wav"},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["status"] == "asr_failed"

    chat_failed = client.post(
        "/v1/speech/voice-turn?filename=按住说话.wav",
        content=make_wav(1.0),
        headers={**HEADERS, "Content-Type": "audio/wav"},
    )
    assert chat_failed.status_code == 503
    assert chat_failed.json()["detail"]["status"] == "chat_failed"


def test_voice_turn_route_tts_only_failure_is_200_with_degraded_status(db):
    client = make_client(service_for(db, chat=RecordingChat(), tts=UnavailableTTSBackend()))
    response = client.post(
        "/v1/speech/voice-turn?filename=按住说话.wav",
        content=make_wav(1.0),
        headers={**HEADERS, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "tts_degraded"
    assert body["reply"]


def test_audio_route_404_and_traversal(db):
    client = make_client(service_for(db))
    assert client.get("/v1/speech/audio/nope", headers=HEADERS).status_code == 404
    assert client.get("/v1/speech/audio/..%2F..%2Flingjingjing.db", headers=HEADERS).status_code == 404


def test_status_route_declares_mock(db):
    client = make_client(service_for(db))
    response = client.get("/v1/speech/status", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "mock"
    assert body["asr"]["real_model"] is False
    assert body["tts"]["real_model"] is False
    assert body["notice"] == MOCK_NOTICE
    assert body["limits"]["max_audio_seconds"] > 0


def test_records_route_lists_latest(db):
    client = make_client(service_for(db))
    client.post(
        "/v1/speech/tts", json={"text": "第一句"}, headers=HEADERS
    )
    response = client.get("/v1/speech/records?limit=5", headers=HEADERS)
    assert response.status_code == 200
    items = response.json()["items"]
    assert items and items[0]["kind"] == "output"
    assert items[0]["text"] == "第一句"


# =============================================================== 组装
def test_build_speech_service_defaults_to_mock_and_honours_env(tmp_path, monkeypatch):
    settings = {"data_dir": tmp_path}
    service = build_speech_service(settings, None)
    status = service.status()
    assert status["mode"] == "mock"
    assert service.store.root == (tmp_path / AUDIO_DIR_NAME).resolve()

    monkeypatch.setenv("LING_SPEECH_ASR_BACKEND", "unavailable")
    service2 = build_speech_service(settings, None)
    assert service2.status()["asr"]["backend"] == "unavailable"

    with pytest.raises(ValueError):
        build_speech_service({"data_dir": tmp_path, "speech_asr_backend": "faster-whisper"}, None)


def test_database_upgrade_path_notes():
    """记录一条已知限制：语音留痕暂存 JSON sidecar，不落 SQLite（task-9 不改 db.py）。"""
    assert Path("apps/brain/src/lingbrain/speech.py").is_file()
