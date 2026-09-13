"""Deterministic recognition of explicit conversational corrections.

The response model still chooses ordinary wording.  A correction such as
``AっていうのはBっていう意味`` is different: the old interpretation is
known to be invalid, so retaining an interrupted answer or asking the model to
infer whether the user corrected it creates a state contradiction.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


_LEADING_FILLER = re.compile(
    r"^(?:(?:いや|いやいや|違う|ちがう|そうじゃなくて|そうではなくて|"
    r"あの[ー〜~]*|えっと|え[ー〜~]+と)[、,\s]*)+"
)
_MEANING_REPAIR = re.compile(
    r"(?P<subject>[^。！？!?]{1,90}?)(?:っていうのは|というのは)[、,\s]*"
    r"(?P<meaning>[^。！？!?]{1,100}?)(?:っていう意味|という意味)"
)
_RIGHT_HAND_REPAIR = re.compile(
    r"(?P<label>[^、,。！？!?]{1,50}?)(?:って|という)[、,\s]+"
    r"(?P<meaning>[^。！？!?]{1,90}?)(?:だよ|です|のこと|ってこと|ということ)?"
    r"[。.!！?？]*$"
)
_EXPLICIT_REPAIR = re.compile(
    r"(?:^|[、,\s])(?:いや|違う|ちがう|そうじゃない|そうじゃなくて|"
    r"ではなく|じゃなくて|という意味|っていう意味|訂正)"
)
_TRAILING = re.compile(
    r"(?:っていう意味|という意味|ってこと|ということ|のこと|だよ|です|"
    r"なんだ|なの)[。.!！?？]*$"
)
_INTERNAL_MARKER = re.compile(r"\[[A-Za-z_][A-Za-z0-9_-]{0,30}\]")


def _clean(value: str, *, limit: int = 100) -> str:
    text = _INTERNAL_MARKER.sub("", str(value or ""))
    text = " ".join(text.replace("　", " ").split())
    text = _LEADING_FILLER.sub("", text)
    text = text.strip(" 　、,。.!！?？「」『』\"'")
    return text[:limit]


@dataclass(frozen=True, slots=True)
class ConversationRepair:
    """A high-confidence correction to the current conversational state."""

    is_repair: bool = False
    supersedes_previous: bool = False
    subject: str = ""
    corrected_meaning: str = ""
    confidence: float = 0.0

    @property
    def canonical_fact(self) -> str:
        if self.subject and self.corrected_meaning:
            return f"{self.subject}＝{self.corrected_meaning}"
        return self.corrected_meaning

    def acknowledgement(self) -> str:
        """Safe short fallback when a model repeats the rejected answer."""
        if self.subject and self.corrected_meaning:
            return f"ああ、「{self.subject}」は{self.corrected_meaning}って意味ね。分かった。"
        if self.corrected_meaning:
            return f"ああ、{self.corrected_meaning}のことね。分かった。"
        return "うん、今の理解は違ってた。どこを直せばいい？"

    def system_context(self) -> str:
        fact = self.canonical_fact or "訂正内容は発話本文から確認する"
        return (
            "【会話の訂正・このターンの最優先事実】\n"
            f"ユーザーが確定した訂正: {fact}\n"
            "直前のAIの解釈と、その解釈から派生した保留説明は無効。再開しない。\n"
            "最初にこの訂正を短く正しく受け取り、必要な場合だけ今の話を続ける。"
            "長い謝罪、別の過去話、直前の誤答の言い換えを出さない。"
        )


def analyze_conversation_repair(text: str) -> ConversationRepair:
    """Extract only corrections whose meaning is explicit in the utterance."""
    value = " ".join(str(text or "").replace("　", " ").split()).strip()
    if not value:
        return ConversationRepair()

    without_filler = _LEADING_FILLER.sub("", value)
    meaning_match = _MEANING_REPAIR.search(without_filler)
    if meaning_match:
        subject = _clean(meaning_match.group("subject"), limit=70)
        meaning = _clean(meaning_match.group("meaning"), limit=90)
        if subject and meaning:
            return ConversationRepair(
                True, True, subject, meaning, 0.99,
            )

    if _EXPLICIT_REPAIR.search(value):
        right_match = _RIGHT_HAND_REPAIR.search(without_filler)
        if right_match:
            meaning = _clean(right_match.group("meaning"), limit=90)
            if meaning:
                return ConversationRepair(
                    True, True, _clean(right_match.group("label"), limit=50),
                    meaning, 0.96,
                )

        remainder = _clean(_TRAILING.sub("", without_filler), limit=90)
        # Bare disagreement invalidates the interrupted interpretation, but it
        # does not provide a replacement fact.
        generic = remainder in {"", "違う", "ちがう", "そうじゃない"}
        return ConversationRepair(
            True, True, "", "" if generic else remainder,
            0.88 if generic else 0.93,
        )

    return ConversationRepair()


def is_explicit_conversation_repair(text: str) -> bool:
    return analyze_conversation_repair(text).is_repair


def repeated_reply_fallback(user_text: str) -> str:
    """Return the last-resort text after a generated echo was suppressed.

    Ordinary repeats are retried once by the caller.  If that retry also
    fails, silence is less disruptive than reading a diagnostic apology into
    the conversation.  Explicit corrections remain deterministic because
    losing the corrected fact would leave state inconsistent.
    """
    repair = analyze_conversation_repair(user_text)
    if repair.is_repair:
        return repair.acknowledgement()
    return ""


def repeated_reply_retry_messages(
    messages: list[dict], user_text: str, rejected_reply: str,
) -> list[dict]:
    """Build one bounded, conversational repair attempt.

    The rejected draft is supplied only so the model knows what not to
    paraphrase.  ``<NO_REPLY>`` gives it a real silence option for a turn that
    naturally closes; callers strip the control token before UI/TTS.
    """
    rejected = _clean(rejected_reply, limit=240)
    current = _clean(user_text, limit=240)
    instruction = (
        "【重複応答の再生成・内部指示】\n"
        "直前の候補は、直前までのAI発話と重なりすぎたためユーザーには伝えていない。"
        "同じ内容の言い換え、謝罪、重複検出の説明は禁止。\n"
        f"今回のユーザー発話: {current or '（短い反応）'}\n"
        f"却下した候補: {rejected or '（空）'}\n"
        "今回の発話へ新しい情報・見方・自然な反応がある場合だけ、日本語の話し言葉で"
        "1〜2文返す。会話がここで自然に閉じるなら、<NO_REPLY> だけを出す。"
    )
    return [*messages, {"role": "system", "content": instruction}]


def normalize_repeated_reply_retry(text: str) -> str:
    """Remove the internal silence token from a bounded repair result."""
    value = str(text or "").strip()
    if not value or value.upper() == "<NO_REPLY>":
        return ""
    return value.replace("<NO_REPLY>", "").strip()
