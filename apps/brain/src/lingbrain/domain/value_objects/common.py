"""Common value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import json


@dataclass(frozen=True, slots=True)
class Timestamp:
    """UTC timestamp with nanosecond precision."""
    
    value: datetime
    
    def __post_init__(self):
        if self.value.tzinfo is None:
            object.__setattr__(self, "value", self.value.replace(tzinfo=None))
    
    @classmethod
    def now(cls) -> "Timestamp":
        return cls(datetime.utcnow())
    
    @classmethod
    def from_iso(cls, iso_string: str) -> "Timestamp":
        return cls(datetime.fromisoformat(iso_string))
    
    @classmethod
    def from_timestamp(cls, timestamp: float) -> "Timestamp":
        return cls(datetime.utcfromtimestamp(timestamp))
    
    def to_iso(self) -> str:
        return self.value.isoformat()
    
    def to_timestamp(self) -> float:
        return self.value.timestamp()
    
    def __str__(self) -> str:
        return self.to_iso()


@dataclass(frozen=True, slots=True)
class Duration:
    """Duration in seconds with nanosecond precision."""
    
    seconds: float
    
    def __post_init__(self):
        if self.seconds < 0:
            raise ValueError("Duration cannot be negative")
    
    @classmethod
    def from_seconds(cls, seconds: float) -> "Duration":
        return cls(seconds)
    
    @classmethod
    def from_minutes(cls, minutes: float) -> "Duration":
        return cls(minutes * 60)
    
    @classmethod
    def from_hours(cls, hours: float) -> "Duration":
        return cls(hours * 3600)
    
    @property
    def total_seconds(self) -> float:
        return self.seconds
    
    @property
    def minutes(self) -> float:
        return self.seconds / 60
    
    @property
    def hours(self) -> float:
        return self.seconds / 3600


@dataclass(frozen=True, slots=True)
class JsonDict:
    """Type-safe JSON-serializable dictionary."""
    
    value: dict
    
    def __post_init__(self):
        # Validate JSON serializability
        json.dumps(self.value)
    
    @classmethod
    def empty(cls) -> "JsonDict":
        return cls({})
    
    def get(self, key: str, default: Any = None) -> Any:
        return self.value.get(key, default)
    
    def __getitem__(self, key: str) -> Any:
        return self.value[key]
    
    def __contains__(self, key: str) -> bool:
        return key in self.value
    
    def to_json(self) -> str:
        return json.dumps(self.value, ensure_ascii=False)
    
    @classmethod
    def from_json(cls, json_str: str) -> "JsonDict":
        return cls(json.loads(json_str))
