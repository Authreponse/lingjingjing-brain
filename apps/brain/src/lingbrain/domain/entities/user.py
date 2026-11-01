"""User domain entity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from ...value_objects.identity import UserId
from ...value_objects.common import Timestamp


@dataclass(frozen=True, slots=True)
class UserProfile:
    """User profile value object."""
    
    display_name: str = ""
    avatar_url: str = ""
    bio: str = ""
    preferences: dict = None
    
    def __post_init__(self):
        if self.preferences is None:
            object.__setattr__(self, "preferences", {})
    
    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "bio": self.bio,
            "preferences": self.preferences
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        return cls(
            display_name=data.get("display_name", ""),
            avatar_url=data.get("avatar_url", ""),
            bio=data.get("bio", ""),
            preferences=data.get("preferences", {})
        )


@dataclass(frozen=True, slots=True)
class User:
    """User aggregate root."""
    
    id: str
    username: str
    email: str = ""
    profile: "UserProfile" = None
    created_at: Timestamp = None
    updated_at: Timestamp = None
    is_active: bool = True
    metadata: dict = None
    
    def __post_init__(self):
        if self.profile is None:
            object.__setattr__(self, "profile", UserProfile())
        if self.created_at is None:
            object.__setattr__(self, "created_at", Timestamp.now())
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", Timestamp.now())
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
    
    @classmethod
    def create(cls, username: str, email: str = "") -> "User":
        """Factory method to create a new user."""
        return cls(
            id=str(__import__("uuid").uuid4()),
            username=username,
            email=email,
            profile=UserProfile(),
            created_at=Timestamp.now(),
            updated_at=Timestamp.now()
        )
    
    def update_profile(self, profile: "UserProfile") -> "User":
        """Return new user with updated profile."""
        return User(
            id=self.id,
            username=self.username,
            email=self.email,
            profile=profile,
            created_at=self.created_at,
            updated_at=__import__("datetime").datetime.utcnow(),
            is_active=self.is_active,
            metadata=self.metadata
        )
    
    def deactivate(self) -> "User":
        """Return deactivated user."""
        return User(
            id=self.id,
            username=self.username,
            email=self.email,
            profile=self.profile,
            created_at=self.created_at,
            updated_at=__import__("datetime").datetime.utcnow(),
            is_active=False,
            metadata=self.metadata
        )
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "profile": self.profile.to_dict() if self.profile else {},
            "created_at": self.created_at.to_iso(),
            "updated_at": self.updated_at.to_iso(),
            "is_active": self.is_active,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "User":
        from ...value_objects.common import Timestamp
        
        return cls(
            id=data["id"],
            username=data["username"],
            email=data.get("email", ""),
            profile=UserProfile.from_dict(data["profile"]) if data.get("profile") else None,
            created_at=Timestamp.from_iso(data["created_at"]) if data.get("created_at") else None,
            updated_at=Timestamp.from_iso(data["updated_at"]) if data.get("updated_at") else None,
            is_active=data.get("is_active", True),
            metadata=data.get("metadata", {})
        )