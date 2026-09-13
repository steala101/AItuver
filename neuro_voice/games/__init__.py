"""Game-specific local knowledge and companion utilities."""

from neuro_voice.games.minecraft_knowledge import (
    MinecraftKnowledgeBase,
    is_minecraft_knowledge_query,
)
from neuro_voice.games.companion import GameCompanionDirector, GameCompanionEvent, companion_prompt

from .profiles import GameProfile, GameProfileRegistry, profile_allows_vision, profile_from_config
from .session import GameProfileSessionManager

__all__ = [
    "MinecraftKnowledgeBase",
    "is_minecraft_knowledge_query",
    "GameCompanionDirector",
    "GameCompanionEvent",
    "companion_prompt",
    "GameProfile",
    "GameProfileRegistry",
    "GameProfileSessionManager",
    "profile_allows_vision",
    "profile_from_config",
]
