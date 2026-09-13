"""Rule-first privacy boundary for memory and Discord group conversations.

The boundary intentionally runs before LLM context construction; prompting an
LLM not to disclose a secret is not treated as an access-control mechanism.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Sensitivity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    RESTRICTED = "restricted"


class Visibility(StrEnum):
    PUBLIC = "public"
    CURRENT_CONVERSATION = "current_conversation"
    SOURCE_AUDIENCE_ONLY = "source_audience_only"
    OWNER_AND_AI = "owner_and_ai"
    DO_NOT_STORE = "do_not_store"


_RESTRICTED = re.compile(r"(?:password|passcode|api[_ -]?key|secret|token|認証コード|パスワード|暗証番号|秘密鍵|クレジットカード|口座番号)", re.I)
_CONTACT = re.compile(r"(?:\b\d{2,4}-\d{2,4}-\d{3,4}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")
_HIGH = re.compile(r"(?:住所|本名|病院|通院|病気|診断|薬|年収|借金|恋愛|家族問題|電話番号|メールアドレス)")
_MEDIUM = re.compile(r"(?:会社|職場|学校|住んで|予定|人間関係)")
_SECRET = re.compile(r"(?:秘密にして|他の人には言わないで|ポッポだけ覚えて)")
_NO_STORE = re.compile(r"(?:覚えないで|記録しないで|保存しないで)")
_CURRENT_ONLY = re.compile(r"(?:今いる(?:人|メンバー).*(?:だけ|内)|この通話だけ)")


@dataclass(frozen=True, slots=True)
class PrivacyDecision:
    sensitivity: Sensitivity
    visibility: Visibility
    should_store: bool
    command: str = ""


class PrivacyManager:
    def __init__(self, cfg) -> None:
        self._enabled = bool(cfg.get("privacy.enabled", True))
        self._context_gate = bool(cfg.get("privacy.context_gate_enabled", True))
        self._output_check = bool(cfg.get("privacy.output_check_enabled", True))

    def classify(self, text: str, *, group: bool = False) -> PrivacyDecision:
        text = str(text or "")
        if _NO_STORE.search(text):
            return PrivacyDecision(Sensitivity.HIGH, Visibility.DO_NOT_STORE, False, "do_not_store")
        if _SECRET.search(text):
            return PrivacyDecision(Sensitivity.HIGH, Visibility.OWNER_AND_AI, True, "secret")
        if _CURRENT_ONLY.search(text):
            return PrivacyDecision(Sensitivity.MEDIUM, Visibility.SOURCE_AUDIENCE_ONLY, True, "audience_only")
        if _RESTRICTED.search(text):
            return PrivacyDecision(Sensitivity.RESTRICTED, Visibility.DO_NOT_STORE, False)
        if _CONTACT.search(text) or _HIGH.search(text):
            return PrivacyDecision(Sensitivity.HIGH, Visibility.OWNER_AND_AI if not group else Visibility.SOURCE_AUDIENCE_ONLY, False)
        if _MEDIUM.search(text):
            return PrivacyDecision(Sensitivity.MEDIUM, Visibility.OWNER_AND_AI if not group else Visibility.SOURCE_AUDIENCE_ONLY, True)
        return PrivacyDecision(Sensitivity.LOW, Visibility.CURRENT_CONVERSATION if group else Visibility.OWNER_AND_AI, True)

    def allow_context(self, record: dict[str, Any], audience: set[str], *, group: bool) -> bool:
        if not self._enabled or not self._context_gate:
            return True
        sensitivity = str(record.get("sensitivity") or Sensitivity.HIGH)
        visibility = str(record.get("visibility") or Visibility.OWNER_AND_AI)
        if sensitivity == Sensitivity.RESTRICTED or visibility == Visibility.DO_NOT_STORE:
            return False
        if not group:
            return True
        if visibility == Visibility.PUBLIC:
            return sensitivity == Sensitivity.LOW
        if visibility == Visibility.SOURCE_AUDIENCE_ONLY:
            source = {str(x) for x in (record.get("source_audience") or [])}
            return sensitivity == Sensitivity.LOW and bool(source) and audience.issubset(source)
        # Private one-to-one and legacy records never enter a group prompt.
        return False

    def safe_output(self, text: str) -> str:
        if not self._enabled or not self._output_check:
            return text
        if _RESTRICTED.search(text) or _CONTACT.search(text):
            return "今ここで私から付け加えることはないかな。"
        return text
