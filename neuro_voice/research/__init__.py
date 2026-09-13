"""Safe, low-priority autonomous research.

This package is deliberately separate from the immediate Web-search path.
Research may only start from a concrete, persisted question and never from a
"fill the silence" LLM prompt.
"""
from .models import (
    ResearchMode, ResearchQuestion, ResearchReason, ResearchStatus,
    GateDecision, ResearchGateResult,
)
from .service import AutonomousResearchService

__all__ = [
    "AutonomousResearchService", "GateDecision", "ResearchGateResult",
    "ResearchMode", "ResearchQuestion", "ResearchReason", "ResearchStatus",
]
