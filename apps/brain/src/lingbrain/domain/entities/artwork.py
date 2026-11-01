"""Artwork domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4

from ...value_objects.identity import ArtworkId
from ...value_objects.common import Timestamp


@dataclass(frozen=True, slots=True)
class Artwork:
    """Immutable artwork entity."""
    
    id: str
    prompt: str
    reason: str
    mood: str = ""
    style_preset: str = ""
    file_path: str = ""
    file_size: int = 0
    width: int = 0
    height: int = 0
    created_at: Timestamp = None
    metadata: dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
        if self.created_at is None:
            object.__setattr__(self, "created_at", Timestamp.now())
    
    @classmethod
    def create(
        cls,
        prompt: str,
        reason: str,
        mood: str = "",
        style_preset: str = "",
        file_path: str = "",
        file_size: int = 0,
        width: int = 0,
        height: int = 0
    ) -> "Artwork":
        """Factory method to create a new artwork."""
        return cls(
            id=str(__import__("uuid").uuid4()),
            prompt=prompt,
            reason=reason,
            mood=mood,
            style_preset=style_preset,
            file_path=file_path,
            file_size=file_size,
            width=width,
            height=height,
            created_at=Timestamp.now()
        )
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "reason": self.reason,
            "mood": self.mood,
            "style_preset": self.style_preset,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "width": self.width,
            "height": self.height,
            "created_at": self.created_at.to_iso() if self.created_at else None,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Artwork":
        from ...value_objects.common import Timestamp
        
        return cls(
            id=data["id"],
            prompt=data["prompt"],
            reason=data["reason"],
            mood=data.get("mood", ""),
            style_preset=data.get("style_preset", ""),
            file_path=data.get("file_path", ""),
            file_size=data.get("file_size", 0),
            width=data.get("width", 0),
            height=data.get("height", 0),
            created_at=Timestamp.from_iso(data["created_at"]) if data.get("created_at") else None,
            metadata=data.get("metadata", {})
        )