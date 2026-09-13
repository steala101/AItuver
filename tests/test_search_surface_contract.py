from neuro_voice.cognition.search_result_presenter import (
    execute_search_route, present_search_outcome,
)
from neuro_voice.search.contracts import SearchDisposition, SearchEvidenceCache
from neuro_voice.search.intent import route_search_request


class FakeSearch:
    def __init__(self):
        self.calls = []

    def evidence_results(self, query, max_results=4):
        self.calls.append((query, max_results))
        return [{
            "title": "饅頭こわい",
            "href": "https://rakugo.example/manju",
            "content": "饅頭こわいは古典落語の演目です。",
        }]


def _run_surface(surface, text, *, is_current=lambda: True):
    route = route_search_request(text)
    search = FakeSearch()
    outcome = execute_search_route(route, search, is_current=is_current)
    response = present_search_outcome(outcome)
    return route, search, outcome, response


def test_local_and_discord_use_the_same_clean_query_and_one_execution():
    for surface in ("local", "discord"):
        route, search, outcome, response = _run_surface(
            surface, "饅頭こわいを検索して、短く教えて")
        assert route.disposition is SearchDisposition.EXECUTE
        assert search.calls == [("饅頭こわい", 4)]
        assert outcome.status == "SUCCEEDED"
        assert response.grounded is True


def test_negative_search_wording_executes_zero_times_on_both_surfaces():
    for surface in ("local", "discord"):
        route, search, outcome, response = _run_surface(
            surface, "検索しないで、知っている範囲で話して")
        assert route.disposition is SearchDisposition.NOT_REQUESTED
        assert search.calls == []
        assert outcome.status == "NOT_REQUESTED"


def test_provider_exception_is_failed_not_empty():
    class FailingSearch:
        def evidence_results(self, query, max_results=4):
            return []

        def evidence_results_strict(self, query, max_results=4):
            raise TimeoutError("provider timeout")

    route = route_search_request("饅頭こわいを検索して")
    outcome = execute_search_route(route, FailingSearch())
    assert outcome.status == "FAILED"
    assert outcome.error_code == "TimeoutError"


def test_stale_result_is_cancelled_before_it_can_be_presented():
    route, search, outcome, response = _run_surface(
        "discord", "饅頭こわいを検索して", is_current=lambda: False)
    assert search.calls == []
    assert outcome.status == "CANCELLED"
    assert response.response == ""
    assert response.selected is None


def test_result_that_becomes_stale_during_search_is_not_presented():
    checks = iter((True, False))
    route, search, outcome, response = _run_surface(
        "discord", "饅頭こわいを検索して", is_current=lambda: next(checks))
    assert search.calls == [("饅頭こわい", 4)]
    assert outcome.status == "CANCELLED"
    assert response.response == ""


def test_discord_real_entrypoint_uses_shared_route_and_rejects_stale_ui():
    import asyncio
    from types import SimpleNamespace

    from neuro_voice.discord_bridge.bot import DiscordBridge
    from neuro_voice.utils.latency import TurnMetrics

    emitted = []
    fake = SimpleNamespace(
        _cfg=SimpleNamespace(
            get=lambda key, default=None: True if key == "search.enabled" else default,
            section=lambda _name: {},
        ),
        _search=FakeSearch(),
        _search_evidence_cache=SearchEvidenceCache(ttl_seconds=600, clock=lambda: 100.0),
        _mind=SimpleNamespace(dialogue_allows_tool=lambda *a, **k: (True, "allowed")),
        _cancelled_response_ids=set(),
        _latest_input_epoch=2,
        _emit=lambda *a, **k: emitted.append((a, k)),
    )
    reply = asyncio.run(DiscordBridge._grounded_search_reply(
        fake, "饅頭こわいを検索して、短く教えて",
        response_id="response:old", metrics=TurnMetrics(), input_epoch=1,
    ))
    assert reply == ""
    assert fake._search.calls == []
    assert emitted == []


def test_evidence_cache_is_epoch_audience_and_ttl_bound():
    now = [100.0]
    route, search, outcome, _response = _run_surface(
        "local", "饅頭こわいを検索して")
    cache = SearchEvidenceCache(ttl_seconds=600, clock=lambda: now[0])
    cache.put(
        outcome, persona_id="b", surface="local", audience="owner",
        epoch=3, source_turn_id="turn:1",
    )
    assert cache.get(persona_id="b", surface="local", audience="owner", epoch=3)
    assert cache.get(persona_id="c", surface="local", audience="owner", epoch=3) is None
    assert cache.get(persona_id="b", surface="discord", audience="owner", epoch=3) is None
    assert cache.get(persona_id="b", surface="local", audience="group", epoch=3) is None
    assert cache.get(persona_id="b", surface="local", audience="owner", epoch=4) is None

    cache.put(
        outcome, persona_id="b", surface="local", audience="owner",
        epoch=3, source_turn_id="turn:1",
    )
    now[0] = 701.0
    assert cache.get(persona_id="b", surface="local", audience="owner", epoch=3) is None


def _cached_story(cache, *, surface, audience):
    route = route_search_request("まんじゅうこわいを検索して、短く教えて")
    outcome = __import__(
        "neuro_voice.cognition.search_result_presenter", fromlist=["build_search_outcome"]
    ).build_search_outcome(route, [{
        "title": "まんじゅうこわい",
        "href": "https://rakugo.example/story",
        "content": (
            "まんじゅうこわいは古典落語の演目です。"
            "男は饅頭が怖いとうそをつきます。"
            "最後に男は、本当に怖いものは濃いお茶だと言います。"
        ),
    }])
    cache.put(
        outcome, persona_id="b", surface=surface, audience=audience,
        epoch=3, source_turn_id="turn:search",
    )
    return outcome


def test_local_followup_uses_current_ending_mode_without_second_tool_execution():
    import asyncio
    from types import SimpleNamespace

    from neuro_voice.pipeline import VoicePipeline
    from neuro_voice.utils.latency import TurnMetrics

    class Runtime:
        def __init__(self):
            self.proposals = 0

        def set_read_only_session(self, _enabled):
            pass

        def propose(self, _intent):
            self.proposals += 1
            raise AssertionError("cached follow-up must not execute a second Tool")

    runtime = Runtime()
    cache = SearchEvidenceCache(ttl_seconds=600, clock=lambda: 100.0)
    _cached_story(cache, surface="local", audience="local:mic")
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._bind_tool_runtime = lambda: None
    pipeline._mind = SimpleNamespace(tool_runtime=lambda: runtime)
    pipeline._search_evidence_cache = cache
    pipeline._search_enabled = True
    pipeline._cognitive_decision = SimpleNamespace(
        state_snapshot_id="state", event_ids=(), turn_id="turn:followup")
    metrics = TurnMetrics(
        turn_id="turn:followup", persona_id="b", persona_epoch=3)

    reply = asyncio.run(VoicePipeline._execute_production_read_only_tool(
        pipeline, "さっきの話の結末もわかる？", metrics, "response:followup"))

    assert reply == "最後に男は、本当に怖いものは濃いお茶だと言います。"
    assert runtime.proposals == 0


def test_discord_followup_uses_current_ending_mode_without_second_search():
    import asyncio
    from types import SimpleNamespace

    from neuro_voice.discord_bridge.bot import DiscordBridge
    from neuro_voice.utils.latency import TurnMetrics

    cache = SearchEvidenceCache(ttl_seconds=600, clock=lambda: 100.0)
    _cached_story(cache, surface="discord", audience="discord")
    search = FakeSearch()
    fake = SimpleNamespace(
        _cfg=SimpleNamespace(get=lambda key, default=None: True),
        _search=search,
        _search_evidence_cache=cache,
        _mind=None,
        _cancelled_response_ids=set(),
        _latest_input_epoch=1,
        _emit=lambda *_args, **_kwargs: None,
    )

    reply = asyncio.run(DiscordBridge._grounded_search_reply(
        fake, "さっきの話の結末もわかる？", response_id="response:followup",
        metrics=TurnMetrics(
            turn_id="turn:followup", persona_id="b", persona_epoch=3),
        input_epoch=1,
    ))

    assert reply == "最後に男は、本当に怖いものは濃いお茶だと言います。"
    assert search.calls == []
