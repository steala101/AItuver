"""活動（遊び・共同作業）の確定事実を扱う基盤。

会話履歴は表現のための記録であり、活動の正本ではない。この package は
イベント台帳と再構築可能な canonical state を提供する。
"""

from .engine import (
    ActivityEvent,
    ActivityOutcome,
    ActivityStateManager,
    ActionProposal,
    CanonicalWord,
    FactRecord,
    ValidationResult,
)
from .japanese_reading import (
    DEFAULT_READINGS,
    JapaneseReadingService,
    first_kana,
    last_kana,
    normalize_kana,
)

__all__ = [
    "ActivityEvent", "ActivityOutcome", "ActivityStateManager", "ActionProposal", "CanonicalWord",
    "FactRecord", "ValidationResult",
    "DEFAULT_READINGS", "JapaneseReadingService", "first_kana", "last_kana",
    "normalize_kana",
]
