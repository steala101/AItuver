"""Safe, deterministic presentation of untrusted web-search results.

This boundary accepts only a compact result view.  It never accepts HTML,
snippets, or a Tool instruction as an instruction to the application.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from neuro_voice.cognition.tool_exec import instruction_like
from neuro_voice.search.contracts import (
    EvidenceSpan, SearchAnswerMode, SearchDisposition, SearchOutcome,
    SearchRouteDecision,
)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_TAG = re.compile(r"<[^>]{0,400}>")
_MAX_TITLE = 160
_MAX_URL = 2048
_MAX_EVIDENCE = 360
_MAX_EVIDENCE_SOURCE = 1200
_MAX_BRIEF_RESPONSE = 190
_MAX_BRIEF_EVIDENCE = 170
_SENSITIVE_QUERY_KEYS = re.compile(
    r"(?:token|key|secret|password|passwd|auth|signature|credential|session)", re.I)
_ENDING_STRONG_HINT = re.compile(
    r"(?:最後(?:に|は)|最終的に|結局|ついに|その末|オチ(?:は|として)|結末(?:は|で))"
)
_ENDING_ACTION_HINT = re.compile(
    r"(?:終わ(?:る|ります|った|って)|終え(?:る|ます|た)|締めくく|話を締め|幕を閉じ)"
)
_ENDING_META_HINT = re.compile(
    r"(?:結末|オチ|落ち|終わり|ラスト|最後)[^。！？.!?]{0,24}"
    r"(?:有名|紹介|解説|説明|記事|ネタバレ|話題|注目|魅力|特徴|"
    r"意外|衝撃|感動|印象)"
)
_NAVIGATION_HINTS = (
    "メインページ", "最近の出来事", "おまかせ表示", "ヘルプ", "井戸端",
    "ページ先頭", "ログイン", "アカウント作成", "ナビゲーション", "メニュー",
    "main page", "random article", "help", "navigation", "sign in",
)


@dataclass(frozen=True, slots=True)
class SearchResultView:
    result_id: str
    title: str
    url: str
    normalized_domain: str
    source_type: str
    rank: int
    relevance: float
    is_official_candidate: bool
    #: Short, sanitized source evidence.  It remains untrusted data and is
    #: never interpreted as an instruction or passed to an action chooser.
    excerpt: str = ""
    evidence_source_kind: str = "SNIPPET"
    #: Richer but still bounded/sanitized evidence retained process-locally so
    #: a follow-up can select a supported span without another Web request.
    supporting_text: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "title": self.title,
            "url": self.url,
            "normalized_domain": self.normalized_domain,
            "source_type": self.source_type,
            "rank": self.rank,
            "relevance": self.relevance,
            "is_official_candidate": self.is_official_candidate,
            "excerpt": self.excerpt,
            "evidence_source_kind": self.evidence_source_kind,
            "supporting_text": self.supporting_text,
        }


@dataclass(frozen=True, slots=True)
class SearchResultPresentation:
    response: str
    presenter_type: str
    grounded: bool
    selected: SearchResultView | None = None

    @property
    def ui_result(self) -> dict[str, Any] | None:
        if self.selected is None:
            return None
        # This is already normalized.  Do not add snippets or Tool output.
        return {
            "title": _display_label(self.selected),
            "url": self.selected.url,
            "domain": self.selected.normalized_domain,
            "official_candidate": self.selected.is_official_candidate,
            "excerpt": self.selected.excerpt,
        }


def normalise_search_results(
    raw_results: Any, *, limit: int = 4, query: str = "",
    answer_mode: SearchAnswerMode = SearchAnswerMode.BRIEF,
) -> list[SearchResultView]:
    """Return compact views; malformed, unsafe and non-web URLs are omitted."""
    views: list[SearchResultView] = []
    seen: set[str] = set()
    for raw in raw_results if isinstance(raw_results, (list, tuple)) else ():
        if not isinstance(raw, dict):
            continue
        url, domain = _safe_url(raw.get("url") or raw.get("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        rank = len(views) + 1
        title = _clean_text(raw.get("title")) or domain
        carried = _safe_evidence_source(raw.get("supporting_text"))
        carried_kind = str(raw.get("evidence_source_kind") or "SNIPPET").upper()
        if carried:
            page = carried if carried_kind.startswith("PAGE") else ""
            snippet = carried if not carried_kind.startswith("PAGE") else ""
        else:
            page = _safe_evidence_source(raw.get("content"))
            snippet = _safe_evidence_source(raw.get("excerpt") or raw.get("body"))
        supporting_text, source_kind = _select_evidence_source(query, page, snippet)
        excerpt = _safe_evidence(supporting_text)
        result_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        relevance = _result_relevance(
            query=query, title=title, excerpt=excerpt, domain=domain,
            answer_mode=answer_mode,
        )
        views.append(SearchResultView(
            result_id=result_id,
            title=title,
            url=url,
            normalized_domain=domain,
            source_type="web_search",
            rank=rank,
            relevance=relevance,
            is_official_candidate=(domain == "openai.com" or domain.endswith(".openai.com")),
            excerpt=excerpt,
            evidence_source_kind=source_kind,
            supporting_text=supporting_text,
        ))
        if len(views) >= max(1, min(int(limit), 8)):
            break
    if query:
        views.sort(key=lambda item: (-item.relevance, item.rank))
        views = [
            SearchResultView(
                result_id=item.result_id, title=item.title, url=item.url,
                normalized_domain=item.normalized_domain,
                source_type=item.source_type, rank=index,
                relevance=item.relevance,
                is_official_candidate=item.is_official_candidate,
                excerpt=item.excerpt,
                evidence_source_kind=item.evidence_source_kind,
                supporting_text=item.supporting_text,
            )
            for index, item in enumerate(views, 1)
        ]
    return views


def build_search_outcome(
    route: SearchRouteDecision, raw_results: Any, *, status: str = "SUCCEEDED",
    error_code: str = "", latency_ms: float = 0.0,
) -> SearchOutcome:
    """Build a query-aware, source-bounded outcome from untrusted rows."""
    normalized_status = str(status or "FAILED").upper()
    if normalized_status != "SUCCEEDED":
        return SearchOutcome(
            status=normalized_status, route=route, error_code=str(error_code)[:80],
            latency_ms=max(0.0, float(latency_ms)),
        )
    views = normalise_search_results(
        raw_results, limit=4, query=route.query, answer_mode=route.answer_mode)
    evidence: list[EvidenceSpan] = []
    primary_view: SearchResultView | None = None
    for view in views:
        if not view.excerpt or view.relevance <= 0:
            continue
        text = view.excerpt[:_MAX_EVIDENCE]
        evidence.append(_make_evidence_span(
            view, start=0, end=len(text), text=text,
            source_kind=view.evidence_source_kind,
        ))
        primary_view = view
        break
    if primary_view is not None:
        ending = _ending_span(primary_view.supporting_text)
        if ending is not None:
            start, end, text = ending
            evidence.append(_make_evidence_span(
                primary_view, start=start, end=end, text=text,
                source_kind=f"{primary_view.evidence_source_kind}_ENDING",
            ))
    if not views:
        result_status = "EMPTY"
        answerability = "NONE"
    elif not evidence:
        result_status = "SUCCEEDED"
        answerability = "NONE"
    elif evidence[0].source_kind == "SNIPPET":
        result_status = "SUCCEEDED"
        answerability = "PARTIAL"
    elif views[0].relevance >= .65:
        result_status = "SUCCEEDED"
        answerability = "SUFFICIENT"
    else:
        result_status = "SUCCEEDED"
        answerability = "PARTIAL"
    return SearchOutcome(
        status=result_status, route=route, results=tuple(views),
        evidence=tuple(evidence), answerability=answerability,
        latency_ms=max(0.0, float(latency_ms)),
    )


def execute_search_route(
    route: SearchRouteDecision, searcher: Any, *, is_current=lambda: True,
    deadline_seconds: float = 7.0,
) -> SearchOutcome:
    """Execute one bounded logical search and reject stale/late results."""
    if route.disposition is not SearchDisposition.EXECUTE:
        return SearchOutcome(status=route.disposition.value, route=route)
    if not is_current():
        return SearchOutcome(status="CANCELLED", route=route)
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
    import inspect
    import time

    started = time.monotonic()
    budget = min(7.0, max(0.001, float(deadline_seconds)))
    configured = getattr(searcher, "_timeout", None)
    if configured is not None:
        budget = min(budget, max(0.001, float(configured)))
    deadline_at = started + budget
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-deadline")
    try:
        strict = getattr(searcher, "evidence_results_strict", None)
        method = strict if callable(strict) else searcher.evidence_results
        parameters = inspect.signature(method).parameters
        accepts_deadline = (
            "deadline_seconds" in parameters
            or any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in parameters.values())
        )
        accepts_current = (
            "is_current" in parameters
            or any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in parameters.values())
        )
        kwargs = {"max_results": 4}
        if accepts_deadline:
            kwargs["deadline_seconds"] = budget
        if accepts_current:
            kwargs["is_current"] = is_current
        future = pool.submit(method, route.query, **kwargs)
        while True:
            if not is_current():
                future.cancel()
                return SearchOutcome(
                    status="CANCELLED", route=route,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                future.cancel()
                return SearchOutcome(
                    status="FAILED", route=route,
                    error_code="SearchDeadlineExceeded",
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            try:
                raw = future.result(timeout=min(0.05, remaining))
                break
            except FutureTimeoutError:
                if not future.done():
                    continue
                raw = future.result()
                break
    except FutureTimeoutError as exc:
        if 'future' in locals() and future.done():
            error = future.exception()
            code = type(error).__name__ if error is not None else type(exc).__name__
        else:
            if 'future' in locals():
                future.cancel()
            code = "SearchDeadlineExceeded"
        return SearchOutcome(
            status="FAILED", route=route, error_code=code,
            latency_ms=(time.monotonic() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary
        return SearchOutcome(
            status="FAILED", route=route,
            error_code=type(exc).__name__,
            latency_ms=(time.monotonic() - started) * 1000,
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    elapsed = (time.monotonic() - started) * 1000
    if not is_current():
        return SearchOutcome(status="CANCELLED", route=route, latency_ms=elapsed)
    return build_search_outcome(route, raw, latency_ms=elapsed)


def present_search_outcome(
    outcome: SearchOutcome, *, answer_mode: SearchAnswerMode | None = None,
) -> SearchResultPresentation:
    """Present only evidence whose hash and bounds match its selected source."""
    mode = answer_mode or outcome.route.answer_mode
    if outcome.status in {"NOT_REQUESTED", "DEFERRED", "CANCELLED"}:
        return SearchResultPresentation("", "search_not_presented", False)
    if outcome.status == "BLOCKED":
        return SearchResultPresentation(
            "検索機能を使えない状態だよ。", "deterministic_search_blocked", False)
    if outcome.status == "FAILED":
        return SearchResultPresentation(
            "検索に失敗したよ。", "deterministic_search_error", False)
    if outcome.status == "EMPTY":
        return SearchResultPresentation(
            "検索結果は見つからなかったよ。", "deterministic_search_empty", False)
    if mode is SearchAnswerMode.ENDING:
        ending = next(
            (item for item in outcome.evidence if item.source_kind.endswith("_ENDING")),
            None,
        )
        selected = next((
            item for item in outcome.results
            if ending is not None and item.result_id == ending.result_id
        ), None)
        if ending is None or selected is None or not _evidence_matches(selected, ending):
            fallback = outcome.results[0] if outcome.results else None
            return SearchResultPresentation(
                "結末を確認できる根拠は、直前の検索結果にはなかったよ。",
                "deterministic_search_ending_insufficient", False, fallback,
            )
        return SearchResultPresentation(
            ending.text, "deterministic_search_ending_evidence", True, selected,
        )
    if not outcome.results or not outcome.evidence:
        selected = outcome.results[0] if outcome.results else None
        source = f"（{selected.normalized_domain}）" if selected is not None else ""
        return SearchResultPresentation(
            f"関連する結果{source}はあったけど、説明に使える根拠を確認できなかったよ。",
            "deterministic_search_insufficient", False, selected)
    evidence = next(
        (item for item in outcome.evidence if not item.source_kind.endswith("_ENDING")),
        outcome.evidence[0],
    )
    selected = next(
        (item for item in outcome.results if item.result_id == evidence.result_id), None)
    if selected is None or not _evidence_matches(selected, evidence):
        return SearchResultPresentation(
            "確認できる根拠が一致しなかったよ。",
            "deterministic_search_evidence_rejected", False)
    label = _display_label(selected)
    if mode is SearchAnswerMode.OFFICIAL_URL:
        response = f"公式サイト候補は {selected.normalized_domain} の「{label}」だよ。"
    elif mode is SearchAnswerMode.BRIEF:
        brief = _brief_evidence(evidence.text)
        if not brief:
            return SearchResultPresentation(
                "関連する結果はあったけど、短く言い切れる根拠を確認できなかったよ。",
                "deterministic_search_brief_insufficient", False, selected,
            )
        response = (
            f"確認できた範囲では、{brief}"
            if outcome.answerability == "PARTIAL" else brief
        )
        if len(response) > _MAX_BRIEF_RESPONSE:
            return SearchResultPresentation(
                "関連する結果はあったけど、短く言い切れる根拠を確認できなかったよ。",
                "deterministic_search_brief_insufficient", False, selected,
            )
    elif outcome.answerability == "PARTIAL":
        response = f"確認できた範囲では、{evidence.text}"
    else:
        response = evidence.text
    return SearchResultPresentation(
        response=response, presenter_type="deterministic_search_evidence",
        grounded=True, selected=selected)


def present_search_result(*, status: Any, structured_output: Any) -> SearchResultPresentation:
    """Build the one allowed response from re-normalized, untrusted data."""
    if str(status) != "succeeded":
        return SearchResultPresentation(
            response="検索に失敗したよ。", presenter_type="deterministic_search_error",
            grounded=False)
    payload = structured_output if isinstance(structured_output, dict) else {}
    from neuro_voice.search.intent import route_search_request

    query = str(payload.get("search_query") or "")
    if query:
        route = route_search_request(f"{query}を検索して")
    else:
        route = SearchRouteDecision(
            SearchDisposition.EXECUTE, "legacy_presenter_adapter", "",
            SearchAnswerMode.OFFICIAL_URL if any(
                "openai.com" in str(item) for item in payload.get("search_result_views", ())
            ) else SearchAnswerMode.BRIEF,
        )
    outcome = build_search_outcome(
        route, payload.get("search_result_views"), status="SUCCEEDED")
    return present_search_outcome(outcome)


def safe_present_search_result(*, status: Any, structured_output: Any) -> SearchResultPresentation:
    """A presenter fault must not retry the Tool or escape to Legacy."""
    try:
        return present_search_result(
            status=status, structured_output=structured_output)
    except Exception:  # noqa: BLE001 - presentation is a non-execution boundary
        return SearchResultPresentation(
            response="検索結果を表示できなかったよ。",
            presenter_type="deterministic_search_presenter_error",
            grounded=False)


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = _TAG.sub(" ", text)
    text = _CONTROL.sub(" ", text)
    return " ".join(text.split())[:_MAX_TITLE]


def _clean_unbounded(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = _TAG.sub(" ", text)
    text = _CONTROL.sub(" ", text)
    return " ".join(text.split())


def _safe_evidence(value: Any) -> str:
    """Keep one small factual excerpt; omit instruction-like source text."""
    text = _safe_evidence_source(value)
    if not text or instruction_like(text):
        return ""
    # Search pages occasionally contain prompt-injection boilerplate next to
    # otherwise useful prose.  Splitting before truncation lets us discard an
    # unsafe sentence without discarding the whole result.
    kept: list[str] = []
    size = 0
    for sentence in re.split(r"(?<=[。！？.!?])\s*", text):
        sentence = sentence.strip()
        if not sentence or instruction_like(sentence):
            continue
        room = _MAX_EVIDENCE - size
        if room <= 0:
            break
        piece = sentence[:room]
        kept.append(piece)
        size += len(piece)
        if len(piece) < len(sentence):
            break
    return " ".join(kept).strip()


def _safe_evidence_source(value: Any) -> str:
    text = _clean_unbounded(value)[:_MAX_EVIDENCE_SOURCE]
    if not text:
        return ""
    kept = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？.!?])\s*", text)
        if sentence.strip() and not instruction_like(sentence.strip())
    ]
    return " ".join(kept)[:_MAX_EVIDENCE_SOURCE].strip()


def _sentence_spans(text: str) -> tuple[tuple[int, int, str], ...]:
    spans: list[tuple[int, int, str]] = []
    for match in re.finditer(r"[^。！？.!?]+[。！？.!?]+", str(text or "")):
        raw = match.group(0)
        value = raw.strip()
        if not value:
            continue
        leading = len(raw) - len(raw.lstrip())
        start = match.start() + leading
        spans.append((start, start + len(value), value))
    return tuple(spans)


def _brief_evidence(text: str) -> str:
    selected: list[str] = []
    size = 0
    for _start, _end, sentence in _sentence_spans(text):
        extra = len(sentence) + (1 if selected else 0)
        if size + extra > _MAX_BRIEF_EVIDENCE:
            break
        selected.append(sentence)
        size += extra
        if len(selected) >= 2:
            break
    return " ".join(selected)


def _ending_span(text: str) -> tuple[int, int, str] | None:
    candidates: list[tuple[int, int, tuple[int, int, str]]] = []
    for order, span in enumerate(_sentence_spans(text)):
        sentence = span[2]
        if _ENDING_META_HINT.search(sentence):
            continue
        if _ENDING_STRONG_HINT.search(sentence):
            score = 2
        elif _ENDING_ACTION_HINT.search(sentence):
            score = 1
        else:
            continue
        candidates.append((-score, order, span))
    if not candidates:
        return None
    return min(candidates)[2]


def _make_evidence_span(
    view: SearchResultView, *, start: int, end: int, text: str,
    source_kind: str,
) -> EvidenceSpan:
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return EvidenceSpan(
        evidence_id=hashlib.sha256(
            f"{view.result_id}:{start}:{end}:{content_hash}".encode("utf-8")
        ).hexdigest()[:16],
        result_id=view.result_id, start=start, end=end, text=text,
        content_hash=content_hash, source_kind=source_kind,
    )


def _query_terms(query: str) -> tuple[str, ...]:
    compact = re.sub(r"[\s、。,.!?！？・『』「」()（）]", "", str(query or "")).lower()
    terms = [
        term for term in re.split(r"(?:について|とは|の|を|は|が|に|で|と)", compact)
        if len(term) >= 2 and term not in {"公式サイト", "公式ページ"}
    ]
    if compact and compact not in terms:
        terms.append(compact)
    return tuple(dict.fromkeys(terms))


def _text_relevance(query: str, text: str) -> float:
    haystack = re.sub(r"[\s、。,.!?！？]", "", str(text or "")).lower()
    terms = _query_terms(query)
    if not haystack or not terms:
        return 0.0
    matched = sum(term in haystack for term in terms)
    return matched / len(terms)


def _navigation_boilerplate(text: str) -> bool:
    lowered = str(text or "").lower()
    hits = sum(lowered.count(item) for item in _NAVIGATION_HINTS)
    sentences = _sentence_spans(lowered)
    return hits >= 3 and hits >= max(3, len(sentences) // 2)


def _select_evidence_source(query: str, page: str, snippet: str) -> tuple[str, str]:
    if _navigation_boilerplate(page):
        page = ""
    if _navigation_boilerplate(snippet):
        snippet = ""
    page_score = _text_relevance(query, page)
    snippet_score = _text_relevance(query, snippet)
    if page and (page_score >= snippet_score or not snippet):
        return page, "PAGE"
    if snippet:
        return snippet, "SNIPPET"
    return "", "SNIPPET"


def _result_relevance(
    *, query: str, title: str, excerpt: str, domain: str,
    answer_mode: SearchAnswerMode,
) -> float:
    if not query:
        return .5
    title_score = _text_relevance(query, title)
    evidence_score = _text_relevance(query, excerpt)
    official_bonus = 0.0
    if answer_mode is SearchAnswerMode.OFFICIAL_URL:
        subject_terms = _query_terms(query)
        official_bonus = .3 if any(term in domain.replace(".", "") for term in subject_terms) else 0.0
    title_weight = .55 if excerpt else .2
    return round(min(1.0, title_score * title_weight + evidence_score * .35 + official_bonus), 3)


def _evidence_matches(view: SearchResultView, evidence: EvidenceSpan) -> bool:
    if evidence.start < 0 or evidence.end < evidence.start:
        return False
    source = (
        view.supporting_text
        if evidence.source_kind.endswith("_ENDING") else view.excerpt
    )
    if evidence.end > len(source):
        return False
    expected = source[evidence.start:evidence.end]
    if expected != evidence.text:
        return False
    return hashlib.sha256(evidence.text.encode("utf-8")).hexdigest() == evidence.content_hash


def _safe_url(value: Any) -> tuple[str, str]:
    raw = _CONTROL.sub("", str(value or "").strip())
    if not raw or len(raw) > _MAX_URL or "<" in raw or ">" in raw:
        return "", ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return "", ""
        if parsed.username is not None or parsed.password is not None:
            return "", ""
        domain = parsed.hostname.lower().rstrip(".")
        if domain == "localhost" or domain.endswith(".localhost"):
            return "", ""
        try:
            address = ipaddress.ip_address(domain)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return "", ""
        port = f":{parsed.port}" if parsed.port is not None else ""
    except (TypeError, ValueError):
        return "", ""
    query = urlencode([
        (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not _SENSITIVE_QUERY_KEYS.search(key)
    ])
    return urlunsplit((
        parsed.scheme.lower(), f"{domain}{port}", parsed.path or "/", query, "",
    )), domain


def _display_label(view: SearchResultView) -> str:
    # A title is untrusted data.  An instruction-like title is shown only as
    # its domain and is never fed to an LLM, command layer, or action chooser.
    return view.normalized_domain if instruction_like(view.title) else view.title
