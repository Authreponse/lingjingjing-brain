"""Memory domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from ...value_objects.memory import MemoryKind, MemoryWeight, MemoryContent
from ...value_objects.identity import MemoryId
from ...value_objects.common import Timestamp


@dataclass(frozen=True, slots=True)
class Memory:
    """Immutable memory entity."""
    
    id: MemoryId
    kind: str
    who: str
    content: str
    weight: float
    source: str
    source_event_id: Optional[str]
    created_at: Timestamp
    updated_at: Timestamp
    metadata: dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
    
    @classmethod
    def create(
        cls,
        kind: str,
        who: str,
        content: str,
        weight: float,
        source: str,
        source_event_id: Optional[str] = None,
        metadata: dict = None
    ) -> "Memory":
        """Factory method to create a new memory."""
        now = Timestamp.now()
        return cls(
            id=MemoryId(f"mem_{uuid4().hex[:12]}"),
            kind=kind,
            who=who,
            content=content,
            weight=weight,
            source=source,
            source_event_id=source_event_id,
            created_at=Timestamp.now(),
            updated_at=Timestamp.now(),
            metadata=metadata or {}
        )
    
    def update_weight(self, new_weight: float) -> "Memory":
        """Return a new memory with updated weight."""
        return Memory(
            id=self.id,
            kind=self.kind,
            who=self.who,
            content=self.content,
            weight=weight,
            source=self.source,
            source_event_id=self.source_event_id,
            created_at=self.created_at,
            updated_at=Timestamp.now(),
            metadata=self.metadata
        )
    
    def update_content(self, new_content: str) -> "Memory":
        """Return a new memory with updated content."""
        return Memory(
            id=self.id,
            kind=self.kind,
            who=self.who,
            content=new_content,
            weight=self.weight,
            source=self.source,
            source_event_id=self.source_event_id,
            created_at=self.created_at,
            updated_at=Timestamp.now(),
            metadata=self.metadata
        )
    
    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "who": self.who,
            "content": self.content,
            "weight": self.weight,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "created_at": self.created_at.to_iso(),
            "updated_at": self.updated_at.to_iso(),
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Memory":
        return cls(
            id=MemoryId(data["id"]),
            kind=data["kind"],
            who=data["who"],
            content=data["content"],
            weight=data["weight"],
            source=data["source"],
            source_event_id=data.get("source_event_id"),
            created_at=Timestamp.from_iso(data["created_at"]),
            updated_at=Timestamp.from_iso(data["updated_at"]),
            metadata=data.get("metadata", {})
        )


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A candidate memory for potential storage."""
    
    kind: str
    who: str
    content: str
    weight: float
    source_event_id: Optional[str] = None
    reason: str = ""
    source: str = "model-extractor"
    
    def to_memory(self, source: str) -> "Memory":
        """Convert candidate to a full Memory entity."""
        from ...value_objects.memory import MemoryWeight, MemoryContent
        from ...value_objects.identity import MemoryId
        from ...value_objects.common import Timestamp
        
        return Memory.create(
            kind=self.kind,
            who=self.who,
            content=self.content,
            weight=self.weight,
            source=source,
            source_event_id=self.source_event_id
        )
