"""Deterministic contract between conversation planning and spoken output.

The main LLM still decides the wording.  This module owns the facts that are
not safe to leave to wording alone: whether this turn may ask a question,
whether a claimed memory was actually retrieved, and whether the assistant has
evidence for a first-person past experience.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


DIRECT_ANSWER = "DIRECT_ANSWER"
BRIEF_REACTION = "BRIEF_REACTION"
SHARE_OPINION = "SHARE_OPINION"
PLAYFUL_REACTION = "PLAYFUL_REACTION"
EXTEND_TOPIC = "EXTEND_TOPIC"
ASK_QUESTION = "ASK_QUESTION"
REPAIR = "REPAIR"
SUPPORT = "SUPPORT"
HONEST_RECALL = "HONEST_RECALL"
SAY_NOT_REMEMBERED = "SAY_NOT_REMEMBERED"

_RECALL_REQUEST = re.compile(
    r"(?:"
    r"(?:前に|以前|この前|さっき|昨日|このあいだ).{0,28}"
    r"(?:何|どれ|どんな|誰|どこ|話|言|勧め|おすすめ|覚えて|思い出)|"
    r"(?:覚えてる|覚えている|思い出せる|何を話した|何て言った|何を勧めた)"
    r")"
)
_QUESTION_END = re.compile(r"[?？][」』】）)]*\s*$")
_QUESTION_LEAD = re.compile(
    r"^(?:ところで|ちなみに|そういえば|それで|じゃあ|一つ聞きたい(?:んだけど)?)[、,\s]*$"
)
_FIRST_PERSON_PAST_EXPERIENCE = re.compile(
    r"(?:私は|私も|僕は|僕も|俺は|俺も|ポッポは|自分は|実際に|個人的に)"
    r".{0,70}?"
    r"(?:読んだ|読んだことがある|見た|観た|見たことがある|観たことがある|"
    r"遊んだ|プレイした|行った|行ったことがある|食べた|買った|作った|"
    r"使った|会った|聞いた|聴いた|触った|経験した|住んだ)"
)
_FIRST_PERSON_MEMORY_CLAIM = re.compile(
    r"(?:私は|私も|僕は|僕も|俺は|俺も|ポッポは)?"
    r"(?:前に|以前|この前|さっき).{0,70}?"
    r"(?:話した|言った|聞いた|考えた|相談した|提案した|覚えてる|思い出した)"
    r"|(?:私は|私も|ポッポは).{0,45}(?:覚えてる|思い出した)"
)
_NEGATED_EXPERIENCE = re.compile(
    r"(?:したこと(?:は|が)?ない|読んだこと(?:は|が)?ない|"
    r"見たこと(?:は|が)?ない|観たこと(?:は|が)?ない|"
    r"読んでいない|見ていない|観ていない|"
    r"遊んでいない|行っていない|経験はない|覚えていない|思い出せない)"
)
_PRESENT_PERCEPTION = re.compile(r"(?:今|いま|現在).{0,12}(?:見え|見て|聞こえ|聞い)")
_FICTIONAL_FRAME = re.compile(r"(?:もし|仮に|想像|妄想|物語|設定|役として|ふりをして)")
_GROUNDING_LEAD = re.compile(
    r"(?:私は|私も|僕は|僕も|俺は|俺も|ポッポは|自分は|実際に|個人的に|"
    r"前に|以前|この前|さっき)"
)
_LOGICAL_END = re.compile(r"[。！？!?][」』】）)]*\s*$")


def recall_requested(text: str) -> bool:
    """Whether the user is asking the assistant to remember a past exchange."""
    return bool(_RECALL_REQUEST.search(" ".join(str(text or "").split())))


def needs_grounding_buffer(text: str) -> bool:
    """Whether a TTS fragment must be joined before experience validation."""
    value = str(text or "")
    return bool(_GROUNDING_LEAD.search(value) and not _LOGICAL_END.search(value))


def choose_conversation_move(
    *,
    intent: str,
    primary_style: str,
    target_length: str,
    ask_follow_up: bool,
    recall_is_requested: bool,
    recall_is_grounded: bool,
) -> str:
    """Choose one primary communicative move for the turn."""
    if intent == "correction":
        return REPAIR
    if intent == "support":
        return SUPPORT
    if recall_is_requested:
        return HONEST_RECALL if recall_is_grounded else SAY_NOT_REMEMBERED
    if intent == "question":
        return DIRECT_ANSWER
    if target_length == "short":
        return BRIEF_REACTION
    if ask_follow_up:
        return ASK_QUESTION
    if primary_style == "playful":
        return PLAYFUL_REACTION
    if primary_style in {"opinionated", "reflective", "cautious"}:
        return SHARE_OPINION
    return EXTEND_TOPIC


@dataclass(frozen=True, slots=True)
class ConversationContract:
    response_id: str
    source: str
    move: str
    allow_question: bool
    recall_requested: bool
    recall_grounded: bool
    grounded_experience_claims: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_plan(cls, plan: dict[str, Any]) -> "ConversationContract":
        return cls(
            response_id=str(plan.get("response_id") or ""),
            source=str(plan.get("source") or ""),
            move=str(plan.get("conversation_move") or EXTEND_TOPIC),
            allow_question=bool(plan.get("ask_follow_up", False)),
            recall_requested=bool(plan.get("recall_requested", False)),
            recall_grounded=bool(plan.get("recall_grounded", False)),
            grounded_experience_claims=tuple(
                str(item) for item in plan.get("grounded_experience_claims", ())
                if str(item).strip()
            ),
        )


@dataclass(frozen=True, slots=True)
class ContractResult:
    text: str
    reasons: tuple[str, ...] = ()


def _experience_is_grounded(text: str, contract: ConversationContract) -> bool:
    normalized = re.sub(r"\s+", "", text)
    return any(
        re.sub(r"\s+", "", claim) in normalized
        for claim in contract.grounded_experience_claims
    )


def enforce_sentence(text: str, contract: ConversationContract) -> ContractResult:
    """Enforce high-confidence invariants on one TTS-sized sentence."""
    value = str(text or "").strip()
    if not value:
        return ContractResult("")
    reasons: list[str] = []
    if not contract.allow_question and _QUESTION_LEAD.fullmatch(value):
        return ContractResult("", ("dangling_question_lead",))
    if not contract.allow_question and _QUESTION_END.search(value):
        return ContractResult("", ("question_not_in_plan",))
    if (
        _FIRST_PERSON_PAST_EXPERIENCE.search(value)
        and not _NEGATED_EXPERIENCE.search(value)
        and not _PRESENT_PERCEPTION.search(value)
        and not _FICTIONAL_FRAME.search(value)
        and not _experience_is_grounded(value, contract)
    ):
        return ContractResult(
            "私はそれを実際に体験した記録はないよ。",
            ("unsupported_personal_experience",),
        )
    if (
        _FIRST_PERSON_MEMORY_CLAIM.search(value)
        and not _NEGATED_EXPERIENCE.search(value)
        and not contract.recall_grounded
    ):
        return ContractResult(
            "そのことを覚えていると言える確かな記録は見つからないよ。",
            ("unsupported_memory_claim",),
        )
    return ContractResult(value, tuple(reasons))


def _sentences(text: str) -> list[str]:
    value = str(text or "")
    if not value:
        return []
    return [
        item.strip()
        for item in re.findall(r".+?(?:[。！？!?](?:[」』】）)])?|$)", value, re.S)
        if item.strip()
    ]


def enforce_reply(text: str, contract: ConversationContract) -> ContractResult:
    """Return the canonical text to display, remember and hand to later turns."""
    kept: list[str] = []
    reasons: list[str] = []
    for sentence in _sentences(text):
        result = enforce_sentence(sentence, contract)
        reasons.extend(result.reasons)
        if result.text and (not kept or result.text != kept[-1]):
            kept.append(result.text)
    if not kept and str(text or "").strip():
        kept.append(
            "今は問い返すより、分かる範囲をそのまま答えるね。"
            if "question_not_in_plan" in reasons
            else "その内容は、確かな根拠を確認できないよ。"
        )
    return ContractResult("".join(kept), tuple(dict.fromkeys(reasons)))
