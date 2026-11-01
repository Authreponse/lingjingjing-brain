"""Memory-related value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class MemoryKind(str, Enum):
    """Types of memories that can be stored."""
    
    EVENT = "event"
    PREFERENCE = "preference"
    PROFILE = "profile"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class MemoryWeight:
    """Weight of a memory (0.0 to 1.0)."""
    
    value: float
    
    def __post_init__(self):
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"Memory weight must be between 0.0 and 1.0, got {self.value}")
    
    def __float__(self) -> float:
        return self.value
    
    def __lt__(self, other: MemoryWeight) -> bool:
        return self.value < other.value
    
    def __le__(self, other: MemoryWeight) -> bool:
        return self.value <= other.value
    
    def __gt__(self, other: MemoryWeight) -> bool:
        return self.value > other.value
    
    def __ge__(self, other: MemoryWeight) -> bool:
        return self.value >= other.value


@dataclass(frozen=True, slots=True)
class MemoryContent:
    """Memory content with validation."""
    
    value: str
    max_length: int = 120
    
    def __post_init__(self):
        if not self.value or not self.value.strip():
            raise ValueError("Memory content cannot be empty")
        if len(self.value) > self.max_length:
            raise ValueError(f"Memory content exceeds max length of {self.max_length}")
    
    def __str__(self) -> str:
        return self.value
    
    @property
    def is_empty(self) -> bool:
        return not self.value.strip()
    
    @property
    def length(self) -> int:
        return len(self.value)
