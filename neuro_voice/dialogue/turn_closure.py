"""Decide whether a human turn naturally closes the current exchange.

This policy runs before the LLM.  Silence is an intentional conversational
act here, not a failed generation or an instruction hidden in a prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from neuro_voice.dialogue.addressing import AddressingDecision
from neuro_voice.dialogue.state import ConversationState


class TurnDisposition(StrEnum):
    CONTINUE = "continue"
    SILENCE = "silence"
    BRIEF_ACK = "brief_ack"


@dataclass(frozen=True, slots=True)
class TurnClosureDecision:
    disposition: TurnDisposition
    reason: str
    confidence: float = 0.0
    direct_reply: str | None = None


_FEEDBACK_PHRASES = (
    "ほんとそれ", "本当それ", "ほんとそう", "本当そう",
    "まさにそれ", "マジでそれ", "まじでそれ", "マジでそう", "まじでそう",
    "それな", "それね",
    "そういうことなんだ", "そういうことだね", "そういうことか", "そういうことね",
    "そうだったんだ", "そうなんだね", "理解できた", "よくわかった",
    "よく分かった", "ありがとうございます", "ありがとうございました",
    "なるほどね", "なるほど", "たしかに", "確かに", "納得した", "理解した",
    "わかりました", "分かりました", "わかった", "分かった", "了解した",
    "了解です", "了解", "りょうかい", "そうなんだ", "そうだね", "そうだな",
    "そっかそっか", "そっか", "そうか", "そう",
    "面白いね", "おもしろいね", "すごいね", "いいね", "助かった",
    "ありがとう", "ありがと", "大丈夫", "オッケー", "おっけー",
    "うんうん", "はーい", "はい", "うん", "ええ", "へえ", "へー", "ふーん",
    "ああ", "あー",
)

_QUESTION_WORDS = (
    "なぜ", "なんで", "どうして", "どういう", "どっち", "どれ", "いつ", "どこ",
    "だれ", "誰", "何", "教えて", "聞かせて", "説明して",
)
_CORRECTION_WORDS = (
    "違う", "ちがう", "間違", "訂正", "そうじゃない", "そうではない",
    "いや", "でも", "だけど", "けど", "むしろ", "とは思わない",
)
_REQUEST_WORDS = (
    "お願い", "してほしい", "して欲しい", "やって", "続けて", "進めて",
    "調べて", "検索して", "覚えて", "流して", "再生して", "止めて",
    "消して", "変えて", "上げて", "下げて", "始めて", "終わって",
)
_ACTIONABLE_QUESTION = re.compile(
    r"(?:しようか|やろうか|"
    r"(?:して|やって|試して|流して|再生して|続けて|進めて|始めて|止めて|"
    r"変えて|調べて|見せて|手伝って)?みる|"
    r"(?:する|やる|試す|流す|再生する|続ける|進める|始める|止める|変える))"
    r"(?:[？?]|$)"
)


class TurnClosurePolicy:
    """Conservative deterministic gate for silence and tiny acknowledgements."""

    def __init__(self, cfg) -> None:
        self._enabled = bool(cfg.get("conversation.turn_closure.enabled", True))
        self._max_feedback_chars = max(
            8, int(cfg.get("conversation.turn_closure.max_feedback_chars", 48)),
        )
        self._brief_gratitude = bool(
            cfg.get(
                "conversation.turn_closure.brief_reply_on_gratitude",
                cfg.get("conversation.turn_closure.brief_reply_on_explicit_gratitude", True),
            ),
        )

    def decide(
        self,
        text: str,
        addressing: AddressingDecision,
        state: ConversationState,
    ) -> TurnClosureDecision:
        if not self._enabled:
            return TurnClosureDecision(TurnDisposition.CONTINUE, "turn_closure_disabled")
        if state.previous_speaker != "assistant":
            return TurnClosureDecision(TurnDisposition.CONTINUE, "not_after_assistant")
        if state.activity_active:
            return TurnClosureDecision(TurnDisposition.CONTINUE, "activity_input_has_priority")
        if state.directive_waiting_for_user:
            # The session asked for this person's move and handed the floor
            # over.  Silence here is not a closed exchange, it is a stalled
            # game.
            return TurnClosureDecision(
                TurnDisposition.CONTINUE, "directive_awaiting_user_move",
            )

        surface = str(text or "").strip()
        if not surface or len(surface) > self._max_feedback_chars:
            return TurnClosureDecision(TurnDisposition.CONTINUE, "not_short_feedback")
        if self._needs_content_response(surface):
            return TurnClosureDecision(TurnDisposition.CONTINUE, "content_requires_response")
        if self._answers_actionable_question(state.last_assistant_text, surface):
            return TurnClosureDecision(TurnDisposition.CONTINUE, "actionable_confirmation")
        if not self._is_feedback_only(surface):
            return TurnClosureDecision(TurnDisposition.CONTINUE, "contains_new_content")

        compact = self._compact(surface)
        gratitude_is_addressed = (
            self._brief_gratitude
            and (
                addressing.explicit_wake_word
                or state.voice_human_count == 1
                or (state.conversation_mode == "dialogue" and len(state.participants) == 1)
            )
            and ("ありがとう" in compact or "ありがと" in compact)
        )
        if gratitude_is_addressed:
            return TurnClosureDecision(
                TurnDisposition.BRIEF_ACK,
                "addressed_gratitude",
                confidence=0.98,
                direct_reply="どういたしまして。",
            )
        return TurnClosureDecision(
            TurnDisposition.SILENCE,
            "exchange_naturally_closed",
            confidence=0.94,
        )

    @staticmethod
    def _compact(text: str) -> str:
        return re.sub(r"[\s\u3000、。,.!！?？〜～…・「」『』（）()]", "", text).lower()

    @staticmethod
    def _needs_content_response(text: str) -> bool:
        if "?" in text or "？" in text:
            return True
        if any(word in text for word in _QUESTION_WORDS):
            return True
        if any(word in text for word in _CORRECTION_WORDS):
            return True
        return any(word in text for word in _REQUEST_WORDS)

    @staticmethod
    def has_explicit_answer_request(text: str) -> bool:
        """Reuse the closure policy's request intent without exposing text."""
        return any(word in str(text or "") for word in _REQUEST_WORDS)

    @staticmethod
    def _answers_actionable_question(last_assistant_text: str, text: str) -> bool:
        if not last_assistant_text:
            return False
        if not _ACTIONABLE_QUESTION.search(last_assistant_text.strip()):
            return False
        compact = TurnClosurePolicy._compact(text)
        return any(word in compact for word in ("うん", "はい", "お願い", "やって", "そうして"))

    @staticmethod
    def _is_feedback_only(text: str) -> bool:
        residual = TurnClosurePolicy._compact(text)
        residual = re.sub(r"^(?:ポッポ|ぽっぽ|ポポ|ぽぽ|ポチ|ぽち)", "", residual)
        original = residual
        for phrase in sorted(_FEEDBACK_PHRASES, key=len, reverse=True):
            residual = residual.replace(phrase, "")
        # These particles and fillers are allowed only after at least one
        # actual feedback phrase was consumed.
        if residual == original:
            return False
        for connector in (
            "ほんとに", "本当に", "それは", "これは", "それ", "これ", "なんか", "まあ",
            "ほんと", "本当", "まさに", "マジで", "まじで", "マジ", "まじ",
            "うーん", "えーと", "えっと", "じゃあ", "だね", "ですね",
            "だな", "かな", "かも", "ね", "よ", "な", "わ",
        ):
            residual = residual.replace(connector, "")
        return not residual
