"""Privacy-first query construction for autonomous research."""
from __future__ import annotations

from dataclasses import dataclass
import re


_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:0\d{1,4}[-―ー ]?\d{1,4}[-―ー ]?\d{3,4}|\+\d[\d -]{8,}\d)(?!\d)")
_TOKEN = re.compile(r"\b(?:sk|pk|ghp|AIza|AKIA)[A-Za-z0-9_\-]{12,}\b|(?:api[_ -]?key|token|password|パスワード)\s*[:：=]\s*\S+", re.I)
_ID = re.compile(r"(?:discord\s*(?:user\s*)?id|ユーザーID|account)\s*[:：=]?\s*\d{8,}", re.I)
_ADDRESS = re.compile(r"(?:〒?\d{3}[-―ー]?\d{4}|(?:東京都|北海道|(?:京都|大阪)府|.{2,3}県).{0,28}(?:市|区|町|村|丁目|番地))")
_URL = re.compile(r"https?://\S+", re.I)
_PERSONAL = re.compile(r"(?:本名|勤務先|会社名|家族|恋人|病気|診断|住所|電話番号|メールアドレス)")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SanitizedQuery:
    query: str
    redactions: tuple[str, ...]
    blocked: bool
    reason: str = ""


def sanitize_query(text: str, *, allowed_terms: list[str] | None = None,
                   forbidden_terms: list[str] | None = None) -> SanitizedQuery:
    """Return a minimal public-search query, never the raw utterance.

    A secret or identifying datum blocks the run instead of merely replacing
    it.  This prevents an adjacent name/address from leaking through context.
    """
    raw = str(text or "").strip()
    if not raw:
        return SanitizedQuery("", (), True, "empty_query")
    patterns = (
        ("email", _EMAIL), ("phone", _PHONE), ("credential", _TOKEN),
        ("user_id", _ID), ("address", _ADDRESS), ("url", _URL),
    )
    redactions: list[str] = []
    for label, pattern in patterns:
        if pattern.search(raw):
            redactions.append(label)
    if _PERSONAL.search(raw):
        redactions.append("personal_context")
    if redactions:
        return SanitizedQuery("", tuple(dict.fromkeys(redactions)), True, "privacy_blocked")
    query = raw
    # Remove conversational commands; a persisted question should be a topic,
    # not a replay of an instruction to the assistant.
    query = re.sub(r"(?:これ|それ|この話題|この件)?(?:について)?(?:後で|あとで)?(?:調べて|検索して|学習して)(?:おいて|ください|くれる)?", "", query)
    query = re.sub(r"(?:ポッポ|ぽっぽ|あなた|君|きみ)(?:、|,)?", "", query)
    query = _SPACE.sub(" ", query).strip(" 、。!！?？")
    allowed = [str(x).strip() for x in (allowed_terms or []) if str(x).strip()]
    if allowed:
        # Caller-supplied terms are data from an internal safe event, not an
        # invitation to append arbitrary conversation text.
        query = " ".join(dict.fromkeys([*allowed, query]))[:180]
    for forbidden in forbidden_terms or []:
        if forbidden and str(forbidden) in query:
            return SanitizedQuery("", ("forbidden_term",), True, "forbidden_query_term")
    if len(query) < 2:
        return SanitizedQuery("", (), True, "query_too_short")
    return SanitizedQuery(query[:180], (), False)
