"""DeepSearch: DuckDuckGo による Web 検索と結果の整形。

日時・ニュース・調べ物系の発話を検知したら検索し、
結果スニペットを LLM のコンテキストに注入する。
"""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
import json
import ipaddress
import logging
import math
import re
import socket
import time
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)


class _ArticleTextExtractor(HTMLParser):
    """Collect readable blocks, preferring an explicit article content root."""

    _BLOCK_TAGS = frozenset({"h1", "h2", "h3", "p", "li", "dd", "dt"})
    _EXCLUDED_TAGS = frozenset({
        "nav", "header", "footer", "aside", "script", "style", "noscript",
        "template",
    })
    _MEDIAWIKI_IDS = frozenset({"content", "bodycontent", "mw-content-text"})
    _VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._frames: list[tuple[str, bool, bool]] = []
        self._excluded_depth = 0
        self._preferred_depth = 0
        self._active: list[tuple[str, bool, list[str]]] = []
        self.preferred_blocks: list[tuple[str, str]] = []
        self.fallback_blocks: list[tuple[str, str]] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).lower(): str(value or "") for key, value in attrs}

    @classmethod
    def _is_preferred_root(cls, tag: str, attrs: dict[str, str]) -> bool:
        classes = set(attrs.get("class", "").lower().split())
        return (
            tag in {"main", "article"}
            or attrs.get("role", "").lower() == "main"
            or attrs.get("id", "").lower() in cls._MEDIAWIKI_IDS
            or "mw-parser-output" in classes
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._VOID_TAGS:
            return
        attributes = self._attributes(attrs)
        starts_exclusion = (
            tag in self._EXCLUDED_TAGS
            or attributes.get("role", "").lower() == "navigation"
            or attributes.get("aria-hidden", "").lower() == "true"
        )
        if starts_exclusion:
            self._excluded_depth += 1
        starts_preferred = False
        if self._excluded_depth == 0 and self._is_preferred_root(tag, attributes):
            self._preferred_depth += 1
            starts_preferred = True
        self._frames.append((tag, starts_exclusion, starts_preferred))
        if self._excluded_depth == 0 and tag in self._BLOCK_TAGS:
            self._active.append((tag, self._preferred_depth > 0, []))

    def handle_data(self, data: str) -> None:
        if self._excluded_depth == 0:
            for _tag, _preferred, parts in self._active:
                parts.append(data)

    def _finish_active(self, tag: str) -> None:
        for index in range(len(self._active) - 1, -1, -1):
            active_tag, preferred, parts = self._active[index]
            if active_tag != tag:
                continue
            del self._active[index]
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            if text:
                target = self.preferred_blocks if preferred else self.fallback_blocks
                target.append((tag, text))
            break

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        matching_index = next((
            index for index in range(len(self._frames) - 1, -1, -1)
            if self._frames[index][0] == tag
        ), None)
        if matching_index is None:
            return
        closing = self._frames[matching_index:]
        del self._frames[matching_index:]
        for frame_tag, starts_exclusion, starts_preferred in reversed(closing):
            self._finish_active(frame_tag)
            if starts_preferred:
                self._preferred_depth = max(0, self._preferred_depth - 1)
            if starts_exclusion:
                self._excluded_depth = max(0, self._excluded_depth - 1)

    def text(self, limit: int) -> str:
        blocks = self.preferred_blocks
        if not blocks:
            # A generic page without a semantic main element still yields
            # prose, but global navigation lists never become the fallback.
            blocks = [
                (tag, text) for tag, text in self.fallback_blocks
                if tag in {"h1", "h2", "h3", "p", "dd", "dt"}
            ]
        lines: list[str] = []
        size = 0
        for _tag, value in blocks:
            if value in lines:
                continue
            extra = len(value) + (1 if lines else 0)
            if size + extra > limit:
                if not lines:
                    lines.append(value[:limit])
                break
            lines.append(value)
            size += extra
        return "\n".join(lines)

# 検索が有効そうな発話パターン (日本語音声会話向けヒューリスティック)。
# 「今の」「今日の」だけでは会話内の指示語にもなるため検索条件にしない。
# 「調べて」の単純な部分一致は、「何調べてたの?」のような過去形の質問まで
# 検索要求として拾ってしまう。依頼形かどうかの判定は intent.py に一本化する。
from neuro_voice.search.intent import (
    extract_search_query, is_search_request as _is_search_request,
)
_EXTERNAL_FACT = re.compile(
    r"(最新|ニュース|天気|株価|為替|価格|値段|いくら|発売日|いつ発売|リリース)"
)
_DEFINITION = re.compile(r"(.+?)(?:とは何|とはなに|って何|ってなに)[。.!?！？]*$")
_PERSON_OR_PLACE = re.compile(r"(?:誰(?:です|だ)?[か？?]|どこ(?:です|だ)?[か？?])$")
_CONTEXT_REFERENCE = re.compile(
    r"(さっき(?:の話)?|今の(?:話|発言|返事|説明|質問)?|それ|あれ|その話|この話|そんな話|直前(?:の話)?)"
    r".{0,10}(?:って)?(?:何|なに|何のこと|どういうこと|どういう意味)"
)
_PAST_DIALOGUE_REFERENCE = re.compile(
    r"(?:"
    r"おすすめした|おすすめしてた|勧めた|紹介した|紹介してた|"
    r"言ってた|言った|話してた|話した|触れてた|説明した|教えてくれた|"
    r"覚えてる|覚えている|思い出して"
    r").{0,40}(?:って)?(?:何|なに|どれ|どの|誰|どこ|何のこと)"
)
#: 「何調べてたの?」「何してたの?」— asking what the assistant was just doing.
#: The answer is in the conversation; sending it to a search engine produces
#: unrelated pages that then get reported back as personal experience.
_OWN_ACTIVITY_QUESTION = re.compile(
    r"(?:何|なに|なん)(?:を|か)?(?:調べ|し|やっ|話し|見)(?:て)?(?:た|てた|てる|る)"
)
_DIALOGUE_SOURCE_QUESTION = re.compile(
    r"(?:ポッポ|ぽっぽ|あなた|君|きみ|そっち|AI|自分)(?:が|の|は)"
    r".{0,40}(?:言った|言ってた|話した|話してた|おすすめした|紹介した)"
)
_PREFACE = re.compile(r"(?:今から|これから).{0,16}(?:言う|話す|伝える).{0,12}(?:検索|調べ)")

# 検索クエリから除去する指示語
_STRIP = re.compile(r"(?:について|を)?(調べて|検索して|ググって|サーチして|教えて)(ください|くれる?|よ|ね)?[。.!?！？]?$")


def should_search(text: str) -> bool:
    """発話が Web 検索を必要としそうか判定する。"""
    value = (text or "").strip()
    if not value or is_contextual_clarification(value):
        return False
    if _is_search_request(value) or _EXTERNAL_FACT.search(value):
        return True
    definition = _DEFINITION.search(value)
    if definition:
        subject = re.sub(r"[\s、。,.!?！？]", "", definition.group(1))
        return len(subject) >= 2
    return bool(_PERSON_OR_PLACE.search(value))


def is_contextual_clarification(text: str) -> bool:
    """Return True for a repair question about the immediately preceding dialogue.

    These utterances must be resolved from conversation history, never sent to
    Web search. Explicit search wording and clearly external fact categories
    still take precedence.
    """
    value = (text or "").strip()
    if not value or _is_search_request(value) or _EXTERNAL_FACT.search(value):
        return False
    compact = re.sub(r"[\s、。,.!?！？]", "", value)
    if compact in {
        "何", "なに", "ん何", "え何", "何のこと", "どういうこと", "どういう意味",
        "今の何", "今の何のこと", "それ何", "あれ何",
    }:
        return True
    return bool(
        _CONTEXT_REFERENCE.search(compact)
        or _OWN_ACTIVITY_QUESTION.search(compact)
        or _PAST_DIALOGUE_REFERENCE.search(compact)
        or _DIALOGUE_SOURCE_QUESTION.search(compact)
        or "そんな話してたっけ" in compact
    )


def conversation_repair_context(user_text: str, messages: list[dict]) -> str:
    """Build transcript evidence for an anaphora/repair question.

    Only actual assistant utterances are quoted.  This makes an absent title
    observable to the response model instead of inviting it to fill the gap
    from Web results or general knowledge.
    """
    assistant_turns: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            assistant_turns.append(" ".join(content.split())[:500])
    recent = assistant_turns[-3:]
    evidence = "\n".join(
        f"- assistant[-{len(recent) - index}]: {text}"
        for index, text in enumerate(recent)
    ) or "- 該当するassistant発言は現在の会話履歴にない"
    return (
        "【会話内参照の修復・最優先】\n"
        f"ユーザーの確認: {user_text}\n"
        "これはWebで答えを探す質問ではなく、あなた自身の過去発言・会話記憶が何を指したかの確認。\n"
        "直近のassistant発言（会話証拠）:\n" + evidence + "\n"
        "まず会話証拠に、質問された固有名詞・具体名が実際に書かれているか確認する。"
        "書かれていればその名前と該当発言を短く答える。長期記憶に確実な一致がある場合だけ補助に使う。\n"
        "具体名が書かれていなければ、一般知識や検索結果から穴埋めしない。"
        "『具体的なゲーム名は言っていなかった。私が曖昧な説明を事実のように言ってしまった』のように誤りを認める。\n"
        "存在しない過去会話、作品名、記憶を作らない。検索を提案して話をそらさず、まず確認へ直接答える。"
    )


def is_search_preface(text: str) -> bool:
    """次の発話を検索対象にする、検索の前置きかどうか。"""
    return bool(_PREFACE.search(text))


def build_query(text: str) -> str:
    """発話文から検索クエリを作る。

    Plannerを待たない高速経路では、会話文をそのまま検索すると精度が
    下がる。「AはB。Cって何？」は文脈Bと質問対象Cへ圧縮する。
    """
    if _is_search_request(text):
        extracted = extract_search_query(text)
        if extracted:
            return extracted
    q = _STRIP.sub("", text.strip())
    context = re.search(r"(?:^|[。！？!?])[^。！？!?]{0,48}?は\s*([^。！？!?]{2,60})[。！？!?]", q)
    question = re.search(r"[。！？!?]\s*([^。！？!?]{1,60}?)(?:って何|とは何|とは|ってどんな)[？?]?\s*$", q)
    if context and question:
        subject = re.sub(r"(?:って何|とは何|とは|ってどんな)[？?]?\s*$", "", question.group(1)).strip()
        if subject:
            return f"{context.group(1).strip()} {subject}"
    return q.strip() or text.strip()


@dataclass(frozen=True)
class SearchPlan:
    """LLMが曖昧さを解いた後の検索設計。"""

    queries: tuple[str, ...]
    objective: str
    disambiguation: str


def parse_search_plan(raw: str, user_text: str, max_queries: int = 3) -> SearchPlan:
    """LLMのJSON出力を安全に読み、失敗時は従来クエリへフォールバックする。"""
    fallback = build_query(user_text)
    # An empty plan is the normal fast-path while the planner LLM is still
    # running. It is not an error: start a deterministic search immediately.
    if not raw.strip():
        return SearchPlan(_expand_queries((fallback,), max_queries), user_text, "")
    try:
        text = raw.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
        candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
        data = json.loads(candidate)
        values = data.get("queries") or [data.get("query", "")]
        queries = tuple(
            str(value).strip() for value in values
            if isinstance(value, str) and str(value).strip()
        )[:max(1, max_queries)]
        if not queries:
            queries = (fallback,)
        queries = _expand_queries(queries, max_queries)
        return SearchPlan(
            queries=queries,
            objective=str(data.get("objective") or user_text).strip(),
            disambiguation=str(data.get("disambiguation") or "").strip(),
        )
    except Exception:
        logger.warning("検索計画JSONを読めなかったため、単純クエリへフォールバック")
        return SearchPlan(_expand_queries((fallback,), max_queries), user_text, "")


def _expand_queries(queries: tuple[str, ...], max_queries: int) -> tuple[str, ...]:
    """Expand only queries that explicitly ask for game tactics."""
    candidates = list(queries)
    for query in list(queries):
        # よくある日本語の語順揺れ。LLMの正式名称展開が失敗した時の保険。
        if "ヒルラハード" in query:
            candidates.append(query.replace("ヒルラハード", "ハードヒルラ"))
        if re.search(r"(?:攻略|倒し方|解除|ギミック|対処法|手順|条件)", query):
            candidates.extend((f"{query} 対処法", f"{query} 条件 手順"))
    deduped: list[str] = []
    for query in candidates:
        query = query.strip()
        if query and query not in deduped:
            deduped.append(query)
        if len(deduped) >= max_queries:
            break
    return tuple(deduped)


class DeepSearch:
    """DuckDuckGo テキスト検索。API キー不要。"""

    def __init__(
        self,
        max_results: int = 4,
        region: str = "jp-jp",
        timeout: float = 10.0,
        max_pages: int = 3,
        page_max_chars: int = 3000,
        parallelism: int = 3,
    ):
        self._max_results = int(max_results)
        self._region = region
        self._timeout = timeout
        self._max_pages = max(0, int(max_pages))
        self._page_max_chars = max(500, int(page_max_chars))
        self._parallelism = max(1, int(parallelism))

    def search(self, query: str) -> str | None:
        """検索して LLM 注入用の整形テキストを返す。失敗時は None。"""
        return self.format_results(self.search_results(query))

    def search_many(self, queries: tuple[str, ...] | list[str]) -> str | None:
        """複数クエリの結果をURL重複なしでまとめる。"""
        return self.format_results(self.search_many_results(queries))

    def evidence_results(self, query: str, max_results: int | None = None) -> list[dict]:
        """Return ranked rows with bounded page evidence for a grounded reply."""
        limit = max_results or self._max_results
        rows = self.search_results(query, limit)
        return self._enrich_results(self._rank_results(rows)[:limit])

    def evidence_results_strict(
        self, query: str, max_results: int | None = None, *,
        deadline_seconds: float = 7.0,
        is_current: Callable[[], bool] = lambda: True,
    ) -> list[dict]:
        """Return evidence under one query+page monotonic deadline.

        Each I/O receives only the remaining budget.  A query that finishes
        after the deadline cannot start page fetches.
        """
        limit = max_results or self._max_results
        budget = min(max(0.001, float(deadline_seconds)),
                     max(0.001, float(self._timeout)))
        deadline_at = time.monotonic() + budget
        if not is_current():
            return []
        rows = self._search_results_raw(query, limit, deadline_at=deadline_at)
        if not is_current():
            return []
        if _remaining(deadline_at) <= 0:
            raise TimeoutError("search deadline exceeded before page fetch")
        return self._enrich_results(
            self._rank_results(rows)[:limit], deadline_at=deadline_at,
            is_current=is_current)

    def search_many_results(self, queries: tuple[str, ...] | list[str]) -> list[dict]:
        """Return deduplicated, enriched rows without serialising them for an LLM."""
        merged: list[dict] = []
        seen: set[str] = set()
        clean_queries = tuple(dict.fromkeys(str(q).strip() for q in queries if str(q).strip()))
        if not clean_queries:
            return []
        per_query = max(2, math.ceil(self._max_results / len(clean_queries)))
        batches: list[list[dict]] = [[] for _ in clean_queries]
        with ThreadPoolExecutor(max_workers=min(self._parallelism, len(clean_queries)), thread_name_prefix="web-query") as ex:
            futures = {ex.submit(self.search_results, query, per_query): index
                       for index, query in enumerate(clean_queries)}
            for future in as_completed(futures):
                try:
                    batches[futures[future]] = future.result()
                except Exception:
                    logger.debug("Parallel web search failed", exc_info=True)
        for batch in batches:
            for result in batch:
                href = str(result.get("href") or "")
                key = href or f"{result.get('title', '')}\n{result.get('body', '')}"
                if key in seen:
                    continue
                seen.add(key)
                merged.append(result)
        ranked = self._rank_results(merged)[:self._max_results]
        return self._enrich_results(ranked)

    def search_results(self, query: str, max_results: int | None = None) -> list[dict]:
        """検索結果の生データを返す。検索失敗時は空リスト。"""
        try:
            results = self._search_results_raw(query, max_results or self._max_results)
        except Exception as e:
            logger.warning("Web検索に失敗 (%s): %s", query, e)
            return []

        if not results:
            logger.info("Web検索: 結果なし (%s)", query)
            return []

        logger.info("Web検索: %d件 (%s)", len(results), query)
        return results

    def _search_results_raw(
        self, query: str, max_results: int, *, deadline_at: float | None = None,
    ) -> list[dict]:
        try:
            from ddgs import DDGS  # 新パッケージ名
        except ImportError:
            from duckduckgo_search import DDGS  # 旧パッケージ名
        timeout = self._timeout
        if deadline_at is not None:
            timeout = min(timeout, _remaining_or_raise(deadline_at))
        with DDGS(timeout=timeout) as ddgs:
            return list(ddgs.text(
                query, region=self._region, max_results=int(max_results),
            ))

    @staticmethod
    def _rank_results(results: list[dict]) -> list[dict]:
        """攻略に使える手順・条件のあるページを、感想・問題報告より優先する。"""
        useful = ("攻略", "ギミック", "対処", "解除", "条件", "手順", "方法", "倒し方", "注意")
        weak = ("苦戦", "難しい", "感想", "炎上", "雑談", "まとめ", "掲示板")

        def score(result: dict) -> int:
            text = f"{result.get('title', '')} {result.get('body', '')}".lower()
            return sum(3 for word in useful if word in text) - sum(2 for word in weak if word in text)

        return sorted(results, key=score, reverse=True)

    @staticmethod
    def format_results(results: list[dict]) -> str | None:
        if not results:
            return None
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = (r.get("body") or "").strip()
            content = (r.get("content") or "").strip()
            href = r.get("href", "")
            page_text = f"\n本文抜粋: {content}" if content else ""
            lines.append(f"[{i}] {title}\n要約: {body}{page_text}\n({href})")
        return "\n\n".join(lines)

    def _enrich_results(
        self, results: list[dict], *, deadline_at: float | None = None,
        is_current: Callable[[], bool] = lambda: True,
    ) -> list[dict]:
        """上位ページ本文を取得し、スニペット不足を補う（失敗は無視）。"""
        enriched = [dict(result) for result in results]
        targets = list(enumerate(enriched[:self._max_pages]))
        if not targets:
            return enriched
        if (not is_current()
                or deadline_at is not None and _remaining(deadline_at) <= 0):
            return enriched
        ex = ThreadPoolExecutor(
            max_workers=min(self._parallelism, len(targets)),
            thread_name_prefix="web-page")
        futures = {}
        try:
            for index, item in targets:
                if (not is_current()
                        or deadline_at is not None and _remaining(deadline_at) <= 0):
                    break
                future = ex.submit(
                    self._fetch_page_text, str(item.get("href") or ""),
                    deadline_at=deadline_at, is_current=is_current,
                )
                futures[future] = index
            wait = _remaining(deadline_at) if deadline_at is not None else None
            for future in as_completed(futures, timeout=wait):
                try:
                    content = future.result()
                except Exception:
                    logger.debug("Parallel page fetch failed", exc_info=True)
                    continue
                if content:
                    enriched[futures[future]]["content"] = content
        except FutureTimeoutError:
            logger.debug("Page evidence deadline expired")
        finally:
            for future in futures:
                future.cancel()
            ex.shutdown(wait=deadline_at is None, cancel_futures=True)
        return enriched

    def _fetch_page_text(
        self, url: str, *, deadline_at: float | None = None,
        is_current: Callable[[], bool] = lambda: True,
    ) -> str:
        if (not is_current()
                or deadline_at is not None and _remaining(deadline_at) <= 0):
            return ""
        if not _public_http_url(url):
            return ""
        try:
            import requests

            current = url
            response = None
            for _redirect in range(4):
                if (not is_current()
                        or deadline_at is not None and _remaining(deadline_at) <= 0):
                    return ""
                if not _public_http_url(current):
                    return ""
                request_timeout = self._timeout
                if deadline_at is not None:
                    request_timeout = min(
                        request_timeout, _remaining_or_raise(deadline_at))
                response = requests.get(
                    current,
                    timeout=request_timeout,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; NeuroVoice/1.0)"},
                    allow_redirects=False,
                )
                if int(getattr(response, "status_code", 200)) not in {301, 302, 303, 307, 308}:
                    break
                location = str(getattr(response, "headers", {}).get("location") or "")
                if not location or _redirect >= 3:
                    return ""
                current = urljoin(current, location)
            if response is None:
                return ""
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return ""
            raw_content = getattr(response, "content", None)
            if raw_content is not None:
                raw_content = bytes(raw_content)[:1_048_577]
                if len(raw_content) > 1_048_576:
                    return ""
                encoding = str(getattr(response, "encoding", "") or "utf-8")
                page_html = raw_content.decode(encoding, errors="replace")
            else:
                page_html = str(getattr(response, "text", ""))
                if len(page_html.encode("utf-8", errors="ignore")) > 1_048_576:
                    return ""
            extractor = _ArticleTextExtractor()
            extractor.feed(page_html)
            extractor.close()
            return extractor.text(self._page_max_chars)
        except Exception:
            logger.debug("検索結果ページ本文の取得に失敗: %s", url, exc_info=True)
            return ""


def _remaining(deadline_at: float) -> float:
    return float(deadline_at) - time.monotonic()


def _remaining_or_raise(deadline_at: float) -> float:
    remaining = _remaining(deadline_at)
    if remaining <= 0:
        raise TimeoutError("search deadline exceeded")
    return remaining


def _public_http_url(url: str) -> bool:
    """Fail closed for credentialed and non-public fetch destinations."""
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            ]
        return bool(addresses) and all(address.is_global for address in addresses)
    except (OSError, TypeError, ValueError):
        return False
