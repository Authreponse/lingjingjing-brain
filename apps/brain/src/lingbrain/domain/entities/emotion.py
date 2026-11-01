"""Emotion domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from ...value_objects.emotions import EmotionState
from ...value_objects.identity import EmotionId
from ...value_objects.common import Timestamp


@dataclass(frozen=True, slots=True)
class Emotion:
    """Immutable emotion entity."""

    id: str
    state: "EmotionState"
    trigger: str
    context: str = ""
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
        state: "EmotionState",
        trigger: str,
        context: str = ""
    ) -> "Emotion":
        """Factory method to create a new emotion."""
        return cls(
            id=str(__import__("uuid").uuid4()),
            state=state,
            trigger=trigger,
            context=context,
            created_at=Timestamp.now()
        )
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state.to_dict(),
            "trigger": self.trigger,
            "context": self.context,
            "created_at": self.created_at.to_iso(),
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Emotion":
        from ...value_objects.emotions import EmotionState
        from ...value_objects.common import Timestamp
        
        return cls(
            id=data["id"],
            state=EmotionState.from_dict(data["state"]),
            trigger=data["trigger"],
            context=data.get("context", ""),
            created_at=Timestamp.from_iso(data["created_at"]),
            metadata=data.get("metadata", {})
        )