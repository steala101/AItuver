"""Is this utterance asking me to search, or asking what I did?

The distinction matters because "調べて" appears in both, and treating the
second as the first sends a conversational question to a Web search engine:

    user: 「え? 何調べて、何調べてたの?」   <- asking about my previous turn
    bad:  Web検索: "え? 何調べて、何調べてたの? 対処法"

The results of that search then look like evidence, and the model reports them
as things it personally did.  One substring match produces a fabricated memory.

This module is the single place that answers the question.  The conversation
kernel and the search planner both import it; a second copy is how the two
would drift apart.
"""
from __future__ import annotations

import re

from neuro_voice.search.contracts import (
    SearchAnswerMode, SearchDisposition, SearchRouteDecision,
)

#: The dictionary stem, for interrogatives like 「何を調べて」.
_STEM = r"(?:検索|サーチ|ググ|調べ)"
#: Correct て-form per verb.  サ変 needs the し ("検索して", not "検索て").
_TE = r"(?:検索し|サーチし|ググっ|調べ)て"
#: Plain past: 「調べた」「検索した」「ググった」
_PAST = r"(?:検索し|サーチし|ググっ|調べ)た"

#: Sentence-final or auxiliary-marked forms that really are a request.
_REQUEST = re.compile(
    rf"{_TE}"
    rf"(?:(?:み|おい|とい|置い)て?)?"
    rf"(?:"
    rf"ください|下さい|くださる|くれ(?:る|ない|ます|ませんか|よ)?|"
    rf"もらえ(?:る|ない|ますか|ませんか)|もらって|ほしい|欲しい|ちょうだい|頂戴"
    rf"|\s*[。.!！?？、,]?\s*$"
    rf")"
)

#: Past, progressive, or completed action.  These ask *about* an action.
_NOT_REQUEST = re.compile(
    # 「何を調べて」「なんで検索する」— an interrogative about the act itself.
    rf"(?:何|なに|なん|どこ|いつ|どう|なぜ|どんな|どの)\s*(?:を|で|か|の)?\s*{_STEM}"
    # 「調べてた」「調べてる」「調べています」「調べていた」
    rf"|{_TE}(?:た|る|い(?:た|る|ます|ました)|ます|ました|まし)"
    # 「調べた」「検索した」
    rf"|{_PAST}"
    # 「調べてくれた」「調べてもらった」— reporting, not requesting.
    rf"|{_TE}(?:くれた|くれて|もらった|くれてた)"
)

#: Wording that names an external source explicitly.
_EXTERNAL_SOURCE = re.compile(
    r"(?:ウェブで|webで|ネットで|ネット で|最新情報を|ニュースを|検索結果)"
)

_QUOTED = re.compile(r"[「『\"](?:[^」』\"]|\\.)*[」』\"]")
_NEGATED = re.compile(
    rf"(?:{_STEM})(?:し)?(?:ないで|なくて|ない|不要|しなくていい)"
)
_DEFERRED = re.compile(
    rf"(?:あとで|後で|ついでに).{{0,40}}{_TE}(?:おい|置い)?て?"
)
_COMMAND = re.compile(
    rf"{_TE}(?:(?:み|おい|とい|置い)て?)?"
    rf"(?:から)?"
)
_TARGET_PRONOUN = re.compile(r"^(?:これ|それ|あれ|この件|その件|あの件)$")
_BRIEF = re.compile(r"(?:短く|簡潔に|一言で)")
_ENDING = re.compile(r"(?:結末|オチ|最後)")
_OFFICIAL = re.compile(r"(?:公式(?:サイト|ページ|URL)|公式の(?:サイト|ページ|URL))", re.I)
_EXPLAIN = re.compile(r"(?:説明して|教えて|まとめて|要約して)")


def is_search_request(text: str) -> bool:
    """True only when the person is asking for a search *now*.

    A question about what someone was doing is never a search request, even
    when it contains the same verb and even when it names the Web.
    """
    value = " ".join(str(text or "").split())
    if not value:
        return False
    # Search words quoted as language examples are data, not executable intent.
    unquoted = _QUOTED.sub("", value)
    if not unquoted.strip() or _NEGATED.search(unquoted) or _DEFERRED.search(unquoted):
        return False
    if _NOT_REQUEST.search(unquoted):
        # 「ネットで何調べてたの?」is a question about my past turn, not a task.
        return False
    return bool(_REQUEST.search(unquoted) or _COMMAND.search(unquoted) or _EXTERNAL_SOURCE.search(unquoted))


def extract_search_query(text: str) -> str:
    """Extract only the current search target from a compound request."""
    value = " ".join(str(text or "").strip().split())
    unquoted = _QUOTED.sub("", value)
    match = _COMMAND.search(unquoted)
    if match:
        target = unquoted[:match.start()]
    else:
        target = unquoted
    target = re.sub(r"^(?:ちょっと|今すぐ|ネットで|ウェブで|webで)\s*", "", target, flags=re.I)
    target = re.sub(r"(?:について)?(?:を)?\s*$", "", target)
    target = re.sub(r"[、,。.!！?？\s]+$", "", target)
    return target.strip()


def _answer_mode(value: str) -> SearchAnswerMode:
    if _OFFICIAL.search(value):
        return SearchAnswerMode.OFFICIAL_URL
    if _ENDING.search(value):
        return SearchAnswerMode.ENDING
    if _BRIEF.search(value):
        return SearchAnswerMode.BRIEF
    if _EXPLAIN.search(value):
        return SearchAnswerMode.EXPLANATION
    return SearchAnswerMode.BRIEF


def route_search_request(
    text: str, *, enabled: bool = True, allowed: bool = True,
    has_reusable_evidence: bool = False,
) -> SearchRouteDecision:
    """Return one explicit routing decision shared by both surfaces."""
    value = " ".join(str(text or "").strip().split())
    mode = _answer_mode(value)
    if _DEFERRED.search(_QUOTED.sub("", value)):
        return SearchRouteDecision(
            SearchDisposition.DEFERRED, "deferred_search_request", "", mode)
    if not is_search_request(value):
        if has_reusable_evidence and re.search(
            r"(?:さっき|その|前の).{0,12}(?:話|検索|結果|結末|オチ|続き)", value
        ):
            return SearchRouteDecision(
                SearchDisposition.REUSE_EVIDENCE, "matching_recent_evidence", "", mode)
        return SearchRouteDecision(
            SearchDisposition.NOT_REQUESTED, "not_search_request", "", mode)
    query = extract_search_query(value)
    if not query or _TARGET_PRONOUN.fullmatch(query):
        return SearchRouteDecision(
            SearchDisposition.CLARIFY, "unresolved_search_target", "", mode)
    if not enabled:
        return SearchRouteDecision(
            SearchDisposition.BLOCKED, "search_disabled", query, mode)
    if not allowed:
        return SearchRouteDecision(
            SearchDisposition.BLOCKED, "search_policy_denied", query, mode)
    return SearchRouteDecision(
        SearchDisposition.EXECUTE, "explicit_search_request", query, mode)


def mentions_search_wording(text: str) -> bool:
    """True when search vocabulary appears at all, request or not.

    Used where the old behaviour only needed to know that the topic of
    searching came up, without implying that a search was asked for.
    """
    value = str(text or "")
    return bool(re.search(_STEM, value) or _EXTERNAL_SOURCE.search(value))


__all__ = [
    "extract_search_query", "is_search_request", "mentions_search_wording",
    "route_search_request",
]
