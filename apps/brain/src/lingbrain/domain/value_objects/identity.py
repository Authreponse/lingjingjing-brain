"""Identity value objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType
import uuid


# Type-safe ID types using NewType
UserId = NewType("UserId", str)
SessionId = NewType("SessionId", str)
MessageId = NewType("MessageId", str)
EventId = NewType("EventId", str)
MemoryId = NewType("MemoryId", str)
ArtworkId = NewType("ArtworkId", str)


def generate_id(prefix: str = "") -> str:
    """Generate a new UUID-based ID with optional prefix."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def generate_user_id() -> UserId:
    return UserId(generate_id("usr_"))


def generate_session_id() -> SessionId:
    return SessionId(generate_id("ses_"))


def generate_message_id() -> MessageId:
    return MessageId(generate_id("msg_"))


def generate_event_id() -> EventId:
    return EventId(generate_id("evt_"))


def generate_memory_id() -> MemoryId:
    return MemoryId(generate_id("mem_"))


def generate_artwork_id() -> ArtworkId:
    return ArtworkId(generate_id("art_"))
