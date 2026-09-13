import sys
import time
from types import SimpleNamespace

import pytest

from neuro_voice.cognition.search_result_presenter import execute_search_route
from neuro_voice.search.deepsearch import DeepSearch, parse_search_plan
from neuro_voice.search.intent import route_search_request


def test_general_subject_does_not_receive_game_strategy_expansions():
    plan = parse_search_plan("", "饅頭こわいを検索して、短く教えて", max_queries=3)
    assert plan.queries == ("饅頭こわい",)


def test_literal_private_destination_is_rejected_before_http(monkeypatch):
    calls = []
    fake_requests = SimpleNamespace(get=lambda *a, **k: calls.append((a, k)))
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert DeepSearch()._fetch_page_text("http://127.0.0.1/private") == ""
    assert calls == []


def test_redirect_to_private_destination_is_not_read(monkeypatch):
    class Response:
        status_code = 302
        headers = {"location": "http://169.254.169.254/latest"}
        text = "must not be read"

        def raise_for_status(self):
            return None

    calls = []

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
    ])
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(get=get))
    assert DeepSearch()._fetch_page_text("https://public.example/page") == ""
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False


def test_strict_evidence_uses_one_monotonic_deadline_for_query_and_pages(monkeypatch):
    search = DeepSearch(timeout=10.0)
    seen = {}

    def raw(query, max_results, *, deadline_at=None):
        seen["query"] = deadline_at
        return []

    def enrich(rows, *, deadline_at=None, **kwargs):
        seen["pages"] = deadline_at
        return rows

    monkeypatch.setattr(search, "_search_results_raw", raw)
    monkeypatch.setattr(search, "_enrich_results", enrich)
    started = time.monotonic()
    search.evidence_results_strict("饅頭こわい", deadline_seconds=7.0)

    assert seen["query"] == seen["pages"]
    assert 0 < seen["query"] - started <= 7.0


def test_route_prefers_the_searchers_shorter_configured_timeout():
    class Search:
        _timeout = 0.03

        def __init__(self):
            self.deadline_seconds = None

        def evidence_results_strict(
            self, query, max_results=4, *, deadline_seconds=7.0,
            is_current=lambda: True,
        ):
            self.deadline_seconds = deadline_seconds
            return []

    search = Search()
    outcome = execute_search_route(
        route_search_request("饅頭こわいを検索して"), search,
        deadline_seconds=0.5,
    )

    assert outcome.status == "EMPTY"
    assert search.deadline_seconds == pytest.approx(0.03)


def test_expired_deadline_stops_page_fetch_before_http(monkeypatch):
    calls = []
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
    ])
    monkeypatch.setitem(
        sys.modules, "requests",
        SimpleNamespace(get=lambda *a, **k: calls.append((a, k))),
    )
    result = DeepSearch()._fetch_page_text(
        "https://public.example/page", deadline_at=time.monotonic() - 0.01)
    assert result == ""
    assert calls == []


def _install_html_response(monkeypatch, html):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = html.encode("utf-8")
        encoding = "utf-8"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 0)),
    ])
    monkeypatch.setitem(
        sys.modules, "requests",
        SimpleNamespace(get=lambda *a, **k: Response()),
    )


def test_wikipedia_article_root_excludes_navigation_list_items(monkeypatch):
    html = """
    <html><body>
      <header><nav><ul>
        <li>メインページ</li><li>最近の出来事</li><li>おまかせ表示</li>
      </ul></nav></header>
      <main role="main">
        <div id="mw-content-text"><div class="mw-parser-output">
          <h1>まんじゅうこわい</h1>
          <p>まんじゅうこわいは、古典落語の演目の一つである。</p>
          <h2>あらすじ</h2>
          <p>男は仲間をだまして饅頭を食べ、最後に今度は濃いお茶が怖いと言う。</p>
        </div></div>
      </main>
      <footer><ul><li>プライバシー</li></ul></footer>
    </body></html>
    """
    _install_html_response(monkeypatch, html)

    text = DeepSearch(page_max_chars=2000)._fetch_page_text(
        "https://public.example/wiki/manju")

    assert text.splitlines()[0] == "まんじゅうこわい"
    assert "古典落語の演目" in text
    assert "濃いお茶が怖い" in text
    assert "メインページ" not in text
    assert "最近の出来事" not in text
    assert "プライバシー" not in text


def test_generic_html_fallback_keeps_heading_and_paragraph_not_nav(monkeypatch):
    html = """
    <html><body>
      <nav><ul><li>Home</li><li>Topics</li><li>Sign in</li></ul></nav>
      <div class="page-shell">
        <h1>彗星の観測</h1>
        <p>彗星は太陽に近づくと、ちりとガスの尾が見えることがある。</p>
      </div>
      <aside><p>おすすめメニュー</p></aside>
    </body></html>
    """
    _install_html_response(monkeypatch, html)

    text = DeepSearch(page_max_chars=1200)._fetch_page_text(
        "https://public.example/comet")

    assert text == "彗星の観測\n彗星は太陽に近づくと、ちりとガスの尾が見えることがある。"
    assert "Home" not in text
    assert "Topics" not in text
    assert "おすすめメニュー" not in text


def test_void_elements_do_not_leave_header_exclusion_open(monkeypatch):
    html = """
    <html><body>
      <header><img src="logo.png"><input value="search"></header>
      <main><p>Target article paragraph.</p></main>
    </body></html>
    """
    _install_html_response(monkeypatch, html)

    text = DeepSearch(page_max_chars=1200)._fetch_page_text(
        "https://public.example/article")

    assert text == "Target article paragraph."


def test_mismatched_markup_closes_the_matching_excluded_frame(monkeypatch):
    html = """
    <html><body>
      <header><div>Site furniture</header>
      <main><p>Meaningful article prose.</p></main>
    </body></html>
    """
    _install_html_response(monkeypatch, html)

    text = DeepSearch(page_max_chars=1200)._fetch_page_text(
        "https://public.example/malformed")

    assert text == "Meaningful article prose."
    assert "Site furniture" not in text


def test_internal_navigation_with_void_content_does_not_hide_later_prose(monkeypatch):
    html = """
    <main>
      <p>Article introduction.</p>
      <nav><img src="icon.png"><ul><li>Section menu</li></ul></nav>
      <p>Article conclusion.</p>
    </main>
    """
    _install_html_response(monkeypatch, html)

    text = DeepSearch(page_max_chars=1200)._fetch_page_text(
        "https://public.example/article-with-menu")

    assert text == "Article introduction.\nArticle conclusion."
    assert "Section menu" not in text


def test_cancelled_owner_does_not_start_search():
    class Search:
        def __init__(self):
            self.calls = 0

        def evidence_results_strict(self, query, max_results=4, **kwargs):
            self.calls += 1
            return []

    search = Search()
    outcome = execute_search_route(
        route_search_request("饅頭こわいを検索して"), search,
        is_current=lambda: False, deadline_seconds=0.1)
    assert outcome.status == "CANCELLED"
    assert search.calls == 0


def test_route_deadline_returns_without_committing_late_provider_result():
    class SlowSearch:
        def evidence_results_strict(self, query, max_results=4, **kwargs):
            time.sleep(0.08)
            return [{
                "title": "饅頭こわい",
                "href": "https://rakugo.example/manju",
                "content": "饅頭こわいは古典落語です。",
            }]

    started = time.monotonic()
    outcome = execute_search_route(
        route_search_request("饅頭こわいを検索して"), SlowSearch(),
        deadline_seconds=0.01)
    elapsed = time.monotonic() - started
    assert outcome.status == "FAILED"
    assert outcome.error_code == "SearchDeadlineExceeded"
    assert elapsed < 0.06


def test_route_cancelled_during_provider_wait_releases_wait_early():
    class SlowSearch:
        def evidence_results_strict(self, query, max_results=4, **kwargs):
            time.sleep(0.2)
            return []

    started = time.monotonic()
    outcome = execute_search_route(
        route_search_request("饅頭こわいを検索して"), SlowSearch(),
        is_current=lambda: time.monotonic() - started < 0.02,
        deadline_seconds=0.5,
    )

    assert outcome.status == "CANCELLED"
    assert time.monotonic() - started < 0.1


def test_cancelled_query_does_not_start_page_enrichment(monkeypatch):
    search = DeepSearch(timeout=1.0)
    current = [True]
    page_enrichments = []

    def query_then_cancel(query, max_results, *, deadline_at=None):
        current[0] = False
        return [{"title": "late", "href": "https://public.example/late"}]

    def enrich(rows, *, deadline_at=None, is_current=lambda: True):
        page_enrichments.append(rows)
        return rows

    monkeypatch.setattr(search, "_search_results_raw", query_then_cancel)
    monkeypatch.setattr(search, "_enrich_results", enrich)

    rows = search.evidence_results_strict(
        "饅頭こわい", deadline_seconds=0.5,
        is_current=lambda: current[0],
    )

    assert rows == []
    assert page_enrichments == []


def test_query_finishing_after_deadline_does_not_start_page_fetch(monkeypatch):
    search = DeepSearch(timeout=1.0)
    page_fetches = []

    def slow_query(query, max_results, *, deadline_at=None):
        time.sleep(0.02)
        return [{"title": "late", "href": "https://public.example/late"}]

    def enrich(rows, *, deadline_at=None):
        page_fetches.append(rows)
        return rows

    monkeypatch.setattr(search, "_search_results_raw", slow_query)
    monkeypatch.setattr(search, "_enrich_results", enrich)
    with pytest.raises(TimeoutError):
        search.evidence_results_strict(
            "饅頭こわい", deadline_seconds=0.005)
    assert page_fetches == []
