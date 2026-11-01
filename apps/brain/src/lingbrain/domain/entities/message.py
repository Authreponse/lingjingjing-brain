"""Message and Conversation domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Literal
from uuid import UUID, uuid4

from ...value_objects.identity import MessageId, ConversationId, UserId
from ...value_objects.common import Timestamp


@dataclass(frozen=True, slots=True)
class Message:
    """Immutable message entity."""
    
    id: str
    conversation_id: str
    sender_id: str
    sender_role: Literal["user", "assistant", "system"]
    content: str
    metadata: dict = None
    created_at: Timestamp = None
    
    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
        if self.created_at is None:
            object.__setattr__(self, "created_at", Timestamp.now())
    
    @classmethod
    def create(
        cls,
        conversation_id: str,
        sender_id: str,
        sender_role: Literal["user", "assistant", "system"],
        content: str,
        metadata: dict = None
    ) -> "Message":
        """Factory method to create a new message."""
        return cls(
            id=str(__import__("uuid").uuid4()),
            conversation_id=conversation_id,
            sender_id=sender_id,
            sender_role=sender_role,
            content=content,
            metadata=metadata or {},
            created_at=Timestamp.now()
        )
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "sender_id": self.sender_id,
            "sender_role": self.sender_role,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at.to_iso()
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        from ...value_objects.common import Timestamp
        
        return cls(
            id=data["id"],
            conversation_id=data["conversation_id"],
            sender_id=data["sender_id"],
            sender_role=data["sender_role"],
            content=data["content"],
            metadata=data.get("metadata", {}),
            created_at=Timestamp.from_iso(data["created_at"])
        )


@dataclass(frozen=True, slots=True)
class Conversation:
    """Conversation aggregate root."""
    
    id: str
    user_id: str
    title: str = ""
    message_ids: list[str] = None
    created_at: Timestamp = None
    updated_at: Timestamp = None
    metadata: dict = None
    
    def __post_init__(self):
        if self.message_ids is None:
            object.__setattr__(self, "message_ids", [])
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
        if self.created_at is None:
            object.__setattr__(self, "created_at", Timestamp.now())
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", Timestamp.now())
    
    @classmethod
    def create(cls, user_id: str, title: str = "") -> "Conversation":
        """Factory method to create a new conversation."""
        return cls(
            id=str(__import__("uuid").uuid4()),
            user_id=user_id,
            title=title,
            message_ids=[],
            created_at=Timestamp.now(),
            updated_at=Timestamp.now()
        )
    
    def add_message(self, message_id: str) -> "Conversation":
        """Return new conversation with added message."""
        return Conversation(
            id=self.id,
            user_id=self.user_id,
            title=self.title,
            message_ids=self.message_ids + [message_id],
            created_at=self.created_at,
            updated_at=__import__("datetime").datetime.utcnow()
        )
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "message_ids": self.message_ids,
            "created_at": self.created_at.to_iso() if self.created_at else None,
            "updated_at": self.updated_at.to_iso() if self.updated_at else None,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Conversation":
        from ...value_objects.common import Timestamp
        
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            title=data.get("title", ""),
            message_ids=data.get("message_ids", []),
            created_at=Timestamp.from_iso(data["created_at"]) if data.get("created_at") else None,
            updated_at=Timestamp.from_iso(data["updated_at"]) if data.get("updated_at") else None,
            metadata=data.get("metadata", {})
        )