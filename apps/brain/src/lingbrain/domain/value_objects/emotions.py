"""Emotion-related value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class EmotionType(str, Enum):
    """Types of emotions the agent can express."""
    
    JOY = "joy"
    SADNESS = "sadness"
    ANGER = "anger"
    FEAR = "fear"
    SURPRISE = "surprise"
    DISGUST = "disgust"
    NEUTRAL = "neutral"
    CURIOSITY = "curiosity"
    EXCITEMENT = "excitement"
    AFFECTION = "affection"
    ANXIETY = "anxiety"
    CONTENTMENT = "contentment"


class EmotionIntensity(str, Enum):
    """Intensity levels for emotions."""
    
    SUBTLE = "subtle"
    MODERATE = "moderate"
    INTENSE = "intense"
    OVERWHELMING = "overwhelming"


@dataclass(frozen=True, slots=True)
class EmotionState:
    """Complete emotion state with type and intensity."""
    
    type: EmotionType
    intensity: EmotionIntensity = EmotionIntensity.MODERATE
    trigger: str | None = None
    metadata: dict = None
    
    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
    
    @property
    def is_positive(self) -> bool:
        positive_types = {
            EmotionType.JOY, EmotionType.EXCITEMENT, 
            EmotionType.AFFECTION, EmotionType.CONTENTMENT,
            EmotionType.CURIOSITY, EmotionType.SURPRISE
        }
        return self.type in positive_types
    
    @property
    def is_negative(self) -> bool:
        negative_types = {
            EmotionType.SADNESS, EmotionType.ANGER,
            EmotionType.FEAR, EmotionType.DISGUST,
            EmotionType.ANXIETY
        }
        return self.type in negative_types
    
    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "intensity": self.intensity.value,
            "trigger": self.trigger,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "EmotionState":
        return cls(
            type=EmotionType(data["type"]),
            intensity=EmotionIntensity(data.get("intensity", "moderate")),
            trigger=data.get("trigger"),
            metadata=data.get("metadata", {})
        )
