from pathlib import Path

from neuro_voice.dialogue.conversation_quality_eval import (
    evaluate_events, evaluate_offline_corpus, load_corpus,
)


def test_evaluator_does_not_call_a_search_claim_a_success():
    report = evaluate_events([{
        "case_id": "S01",
        "explicit_search_requested": True,
        "search_attempted": False,
        "search_completed": False,
        "answer_grounded": False,
        "delivery_finalized": True,
    }])
    assert report.explicit_search_successes == 0
    assert report.case_results[0].passed is False


def test_evaluator_keeps_failed_delivery_in_the_denominator():
    report = evaluate_events([{
        "case_id": "D01",
        "explicit_search_requested": False,
        "search_attempted": False,
        "search_completed": False,
        "answer_grounded": False,
        "delivery_finalized": False,
    }])
    assert report.total_cases == 1
    assert report.failed_delivery_turns == 1


def test_unobserved_human_axes_are_not_fabricated_as_numbers():
    report = evaluate_events([])
    assert report.unmeasured_axes == {
        "naturalness", "emotional_fit", "voice_quality",
    }
    assert report.axis_scores["naturalness"] is None


def test_offline_corpus_calls_the_real_search_router_with_fake_io_only():
    corpus = load_corpus(Path("tests/fixtures/conversation_quality.json"))
    report = evaluate_offline_corpus(corpus)
    by_id = {item.case_id: item for item in report.case_results}
    assert by_id["S01"].passed is True
    assert by_id["S02"].passed is True
    assert by_id["S05"].passed is True
    assert report.explicit_search_successes >= 3
