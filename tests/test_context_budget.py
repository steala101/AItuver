"""The prompt must leave room for the answer.

Observed during a long story session: replies started stopping mid-sentence
(「きみが近づくと」, 「ごめんね！さっき私が「」).  The log showed
``finish=length`` on a twelve-character reply — not an answer that ran long,
but a prompt that had filled the whole 4096-token window.
"""
from __future__ import annotations

import pytest

from neuro_voice.llm.prompt_metrics import PromptProfiler
from neuro_voice.llm.context_budget import (
    BudgetResult, estimate_tokens, fit_messages, messages_tokens,
    resolve_reserve_tokens,
)


# ---------------------------------------------------------------------------
# The reserve follows max_tokens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("configured", [None, "", "auto", "AUTO"])
def test_auto_derives_the_reserve_from_the_reply_limit(configured):
    assert resolve_reserve_tokens(
        configured, max_tokens=1024, context_tokens=8192,
    ) == 1024 + 256
    assert resolve_reserve_tokens(
        configured, max_tokens=2048, context_tokens=8192,
    ) == 2048 + 256


def test_a_reserve_smaller_than_the_reply_limit_is_raised():
    """Otherwise raising max_tokens silently brings back mid-sentence cutoffs."""
    assert resolve_reserve_tokens(
        1280, max_tokens=2048, context_tokens=8192,
    ) == 2048 + 256


def test_a_deliberate_larger_reserve_is_respected():
    assert resolve_reserve_tokens(3000, max_tokens=1024, context_tokens=8192) == 3000


def test_a_reserve_that_would_eat_the_window_is_capped():
    reserve = resolve_reserve_tokens(9000, max_tokens=1024, context_tokens=4096)
    assert reserve < 4096
    assert reserve > 0


def test_a_nonsense_value_falls_back_to_auto():
    assert resolve_reserve_tokens(
        "たくさん", max_tokens=1024, context_tokens=8192,
    ) == 1024 + 256


def test_the_resolved_reserve_actually_leaves_room():
    reserve = resolve_reserve_tokens(None, max_tokens=1024, context_tokens=8192)
    messages = [{"role": "system", "content": "ペルソナ"}]
    messages += [{"role": "assistant", "content": "あ" * 400} for _ in range(40)]
    result = fit_messages(
        messages, context_tokens=8192, reserve_tokens=reserve,
    )
    assert messages_tokens(result.messages) <= 8192 - reserve


def system(text):
    return {"role": "system", "content": text}


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


# ---------------------------------------------------------------------------
# Estimating
# ---------------------------------------------------------------------------


def test_japanese_costs_about_a_token_per_character():
    assert 90 <= estimate_tokens("あ" * 100) <= 130


def test_ascii_is_cheaper_than_japanese():
    assert estimate_tokens("a" * 100) < estimate_tokens("あ" * 100)


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_message_overhead_is_counted():
    assert messages_tokens([user("")]) > 0


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def test_a_short_conversation_is_untouched():
    messages = [system("ペルソナ"), user("こんばんは"), assistant("やあ")]
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=1280)
    assert result.messages == messages
    assert result.trimmed is False


def test_a_long_story_session_is_trimmed_to_fit():
    """Six paragraphs of narration is enough to fill a 4096-token window."""
    messages = [system("ペルソナ")]
    for index in range(12):
        messages.append(user(f"行動{index}"))
        messages.append(assistant("描写。" * 120))       # ~360 chars each
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=1280)
    assert result.trimmed is True
    assert messages_tokens(result.messages) <= result.budget


def test_room_is_actually_left_for_the_reply():
    messages = [system("ペルソナ")] + [assistant("あ" * 500) for _ in range(20)]
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=1280)
    assert messages_tokens(result.messages) + 1280 <= 4096 + result.budget
    assert messages_tokens(result.messages) <= 4096 - 1280


def test_system_messages_are_never_dropped():
    """They carry the persona and the turn's decision; losing them silently
    changes who is answering."""
    messages = [
        system("ペルソナ"), system("今回の判断"), system("検索の根拠"),
    ] + [assistant("あ" * 400) for _ in range(20)]
    result = fit_messages(messages, context_tokens=4096, reserve_tokens=1280)
    kept = [item for item in result.messages if item["role"] == "system"]
    assert len(kept) == 3


def test_the_newest_exchange_survives_even_when_huge():
    """The reply has to answer *this* turn, however little else fits."""
    messages = [system("ペルソナ")]
    messages += [assistant("古" * 400) for _ in range(20)]
    messages.append(user("いま聞きたいこと"))
    result = fit_messages(messages, context_tokens=2048, reserve_tokens=1024)
    assert result.messages[-1]["content"] == "いま聞きたいこと"


def test_the_oldest_turns_go_first():
    messages = [system("ペルソナ")]
    # Big enough that not all of them can fit, so the choice actually matters.
    for index in range(20):
        messages.append(user(f"発言{index}" + "あ" * 100))
    result = fit_messages(messages, context_tokens=600, reserve_tokens=200)
    kept = [item["content"] for item in result.messages if item["role"] == "user"]
    assert result.trimmed is True
    assert any(item.startswith("発言19") for item in kept)
    assert not any(item.startswith("発言0あ") for item in kept)


def test_a_tiny_window_still_produces_something_usable():
    messages = [system("ペルソナ"), user("質問")]
    result = fit_messages(messages, context_tokens=64, reserve_tokens=1024)
    assert result.messages
    assert any(item["role"] == "user" for item in result.messages)


def test_the_result_reports_what_happened():
    messages = [system("ペルソナ")] + [assistant("あ" * 400) for _ in range(20)]
    result = fit_messages(messages, context_tokens=2048, reserve_tokens=512)
    assert isinstance(result, BudgetResult)
    assert result.dropped > 0
    assert result.budget == 2048 - 512
    assert result.estimated_tokens > 0


@pytest.mark.parametrize("context,reserve", [(4096, 1280), (8192, 1280), (2048, 512)])
def test_the_budget_holds_at_any_window_size(context, reserve):
    messages = [system("ペルソナ")] + [assistant("あ" * 300) for _ in range(40)]
    result = fit_messages(messages, context_tokens=context, reserve_tokens=reserve)
    assert messages_tokens(result.messages) <= max(256, context - reserve)


def test_an_empty_conversation_is_safe():
    assert fit_messages([], context_tokens=4096, reserve_tokens=1280).messages == []


# ---------------------------------------------------------------------------
# Cutting with slack, so the boundary can stay put
# ---------------------------------------------------------------------------
#
# Measured after the layout fix: reuse was still 30-35% and the first block to
# differ was `assistant` — the history.  Trimming to exactly the budget means
# trimming again next turn at a slightly different place, so the oldest kept
# message keeps changing and the server can never reuse the prefix.


def conversation(turns):
    messages = [{"role": "system", "content": "ペルソナ"}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"ユーザー{index}: " + "話" * 60})
        messages.append({"role": "assistant", "content": f"返事{index}: " + "答" * 60})
    return messages


def test_a_prompt_that_fits_is_never_trimmed_even_with_slack():
    messages = conversation(2)
    result = fit_messages(
        messages, context_tokens=8192, reserve_tokens=1280, slack_ratio=0.4,
    )
    assert result.dropped == 0


def test_slack_drops_more_than_the_bare_minimum():
    messages = conversation(40)
    tight = fit_messages(messages, context_tokens=2048, reserve_tokens=256)
    loose = fit_messages(
        messages, context_tokens=2048, reserve_tokens=256, slack_ratio=0.3,
    )
    assert loose.dropped > tight.dropped
    assert loose.estimated_tokens < tight.estimated_tokens


def test_slack_is_bounded():
    """Half the budget is the most it may ever cut."""
    messages = conversation(40)
    result = fit_messages(
        messages, context_tokens=2048, reserve_tokens=256, slack_ratio=5.0,
    )
    assert result.estimated_tokens > 0
    assert result.messages[0]["role"] == "system"


def test_zero_slack_keeps_the_previous_behaviour():
    messages = conversation(40)
    a = fit_messages(messages, context_tokens=2048, reserve_tokens=256)
    b = fit_messages(
        messages, context_tokens=2048, reserve_tokens=256, slack_ratio=0.0,
    )
    assert a.dropped == b.dropped


def test_the_boundary_survives_the_next_turn():
    """The point of the slack: no re-cut means an identical prefix."""
    messages = conversation(30)
    trimmed = fit_messages(
        messages, context_tokens=2048, reserve_tokens=256, slack_ratio=0.25,
    ).messages
    profiler = PromptProfiler()
    profiler.profile(trimmed)
    # One more exchange arrives; nothing needs dropping yet.
    grown = [*trimmed,
             {"role": "user", "content": "つぎ"},
             {"role": "assistant", "content": "こたえ"}]
    after = fit_messages(
        grown, context_tokens=2048, reserve_tokens=256, slack_ratio=0.25,
    )
    profile = profiler.profile(after.messages)
    assert profile.reuse_ratio > 0.7, "先頭が変わらなければ再利用できる"


def test_without_slack_the_boundary_moves_every_turn():
    messages = conversation(30)
    trimmed = fit_messages(messages, context_tokens=2048, reserve_tokens=256).messages
    profiler = PromptProfiler()
    profiler.profile(trimmed)
    grown = [*trimmed,
             {"role": "user", "content": "つぎ" * 40},
             {"role": "assistant", "content": "こたえ" * 40}]
    after = fit_messages(grown, context_tokens=2048, reserve_tokens=256)
    profile = profiler.profile(after.messages)
    assert after.dropped > 0, "予算ぎりぎりなので毎ターン切り直しになる"
    assert profile.reuse_ratio < 0.7
