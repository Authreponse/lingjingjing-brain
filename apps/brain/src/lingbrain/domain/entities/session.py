"""Session domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Literal
from uuid import uuid4

from ...value_objects.identity import SessionId, UserId
from ...value_objects.common import Timestamp


@dataclass(frozen=True, slots=True)
class Session:
    """Session aggregate root."""
    
    id: str
    user_id: str
    status: Literal["active", "expired", "revoked"] = "active"
    created_at: Timestamp = None
    expires_at: Timestamp = None
    last_activity: Timestamp = None
    metadata: dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
        if self.created_at is None:
            object.__setattr__(self, "created_at", Timestamp.now())
        if self.expires_at is None:
            expires = datetime.utcnow() + timedelta(days=30)
            object.__setattr__(self, "expires_at", Timestamp.from_timestamp(expires.timestamp()))
        if self.last_activity is None:
            object.__setattr__(self, "last_activity", Timestamp.now())
    
    @classmethod
    def create(cls, user_id: str, ttl_days: int = 30) -> "Session":
        """Factory method to create a new session."""
        now = datetime.utcnow()
        expires = now + timedelta(days=30)
        return Session(
            id=str(__import__("uuid").uuid4()),
            user_id=user_id,
            status="active",
            created_at=Timestamp.from_timestamp(datetime.utcnow().timestamp()),
            expires_at=Timestamp.from_timestamp((datetime.utcnow() + timedelta(days=30)).timestamp()),
            last_activity=Timestamp.from_timestamp(datetime.utcnow().timestamp())
        )
    
    def refresh(self, ttl_days: int = 30) -> "Session":
        """Return new session with extended expiry."""
        return Session(
            id=self.id,
            user_id=self.user_id,
            status=self.status,
            created_at=self.created_at,
            expires_at=Timestamp.from_timestamp((datetime.utcnow() + timedelta(days=30)).timestamp()),
            last_activity=Timestamp.now()
        )
    
    def revoke(self) -> "Session":
        """Return revoked session."""
        return Session(
            id=self.id,
            user_id=self.user_id,
            status="revoked",
            created_at=self.created_at,
            expires_at=self.expires_at,
            last_activity=Timestamp.now()
        )
    
    def touch(self) -> "Session":
        """Update last activity timestamp."""
        return Session(
            id=self.id,
            user_id=self.user_id,
            status=self.status,
            created_at=self.created_at,
            expires_at=self.expires_at,
            last_activity=Timestamp.now()
        )
    
    def is_expired(self) -> bool:
        """Check if session is expired."""
        if self.status != "active":
            return True
        return Timestamp.now().value > self.expires_at.value
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "status": self.status,
            "created_at": self.created_at.to_iso(),
            "expires_at": self.expires_at.to_iso(),
            "last_activity": self.last_activity.to_iso() if self.last_activity else None,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        from ...value_objects.common import Timestamp
        
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            status=data.get("status", "active"),
            created_at=Timestamp.from_iso(data["created_at"]) if data.get("created_at") else None,
            expires_at=Timestamp.from_iso(data["expires_at"]) if data.get("expires_at") else None,
            last_activity=Timestamp.from_iso(data["last_activity"]) if data.get("last_activity") else None,
            metadata=data.get("metadata", {})
        )