"""Where a per-turn block goes decides how much of the prompt is re-read.

Measured on this machine over 40 turns:

    初トークン ≈ 1114ms + 0.241ms × (キャッシュされなかったトークン数)

The pipeline used to insert every per-turn block at index 1, immediately after
the persona.  Everything from there to the end therefore differed from the
previous request and had to be evaluated again — 28-33% reuse, 2.5s to the
first token.  Every one of those insertion sites carried a comment saying the
block belonged next to the user turn; the code did the opposite.

These tests fix the placement rule.  They do not assert anything about *what*
is sent, only where it lands.
"""
from __future__ import annotations

import pytest

from neuro_voice.llm.context_budget import estimate_tokens
from neuro_voice.llm.prompt_layout import (
    insert_after_stable_prefix, insert_before_user_turn, last_user_index,
    volatile_tokens,
)
from neuro_voice.llm.prompt_metrics import PromptProfiler


def system(text):
    return {"role": "system", "content": text}


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


PERSONA = system("あなたはポッポ。" + "人格の説明。" * 80)
HISTORY = [user("前のターン"), assistant("前の返事")]


def base():
    return [PERSONA, *HISTORY, user("いまの発話")]


def roles(messages):
    return [m["role"] for m in messages]


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_a_per_turn_block_lands_just_before_the_user_turn():
    result = insert_before_user_turn(base(), "【今回の確信度】迷いなし")
    assert result[-1]["role"] == "user"
    assert result[-2]["content"] == "【今回の確信度】迷いなし"


def test_the_persona_stays_first():
    result = insert_before_user_turn(base(), "【状態】なにか")
    assert result[0] is PERSONA


def test_history_is_not_disturbed():
    result = insert_before_user_turn(base(), "【状態】なにか")
    assert roles(result) == ["system", "user", "assistant", "system", "user"]


def test_several_blocks_stack_in_order_of_insertion():
    result = base()
    for text in ("【一つ目】", "【二つ目】", "【三つ目】"):
        result = insert_before_user_turn(result, text)
    assert [m["content"] for m in result[-4:-1]] == ["【一つ目】", "【二つ目】", "【三つ目】"]


def test_an_empty_block_is_not_inserted():
    assert insert_before_user_turn(base(), "") == base()
    assert insert_before_user_turn(base(), "   ") == base()


def test_a_prompt_without_a_user_turn_appends():
    result = insert_before_user_turn([PERSONA], "【状態】")
    assert result[-1]["content"] == "【状態】"


def test_the_last_user_turn_is_the_one_found():
    messages = [PERSONA, user("古い"), assistant("返事"), user("新しい")]
    assert last_user_index(messages) == 3


# ---------------------------------------------------------------------------
# The stable side
# ---------------------------------------------------------------------------


def test_a_stable_block_joins_the_leading_system_run():
    result = insert_after_stable_prefix(base(), "【変わらない規則】")
    assert result[1]["content"] == "【変わらない規則】"
    assert result[0] is PERSONA


def test_a_stable_block_goes_after_every_leading_system_message():
    messages = [PERSONA, system("もう一つの固定"), *HISTORY, user("いま")]
    result = insert_after_stable_prefix(messages, "【追加の固定】")
    assert result[2]["content"] == "【追加の固定】"


# ---------------------------------------------------------------------------
# The point of it all
# ---------------------------------------------------------------------------


#: A prompt shaped like the real one: a persona, then a large block that does
#: not change between turns, then history.  What the placement decides is
#: whether that large block stays inside the reusable prefix.
RULES = system("【対話制御・内部用】" + "毎ターン同じ規則。" * 300)


def realistic():
    return [PERSONA, RULES, *HISTORY, user("いまの発話")]


def early(messages, content):
    """What the pipeline used to do."""
    return [messages[0], system(content), *messages[1:]]


def test_placing_a_block_late_keeps_the_prefix_reusable():
    profiler = PromptProfiler()
    profiler.profile(insert_before_user_turn(realistic(), "【状態】A"))
    profile = profiler.profile(insert_before_user_turn(realistic(), "【状態】B"))
    assert profile.reuse_ratio > 0.8


def test_placing_the_same_block_early_does_not():
    """The behaviour that was measured at 28-33% reuse on real hardware."""
    profiler = PromptProfiler()
    profiler.profile(early(realistic(), "【状態】A"))
    profile = profiler.profile(early(realistic(), "【状態】B"))
    assert profile.reuse_ratio < 0.5, "personaより後ろが全部再評価される"


def test_late_placement_beats_early_placement_on_identical_content():
    """Same blocks, same words — only the order differs."""
    late_p, early_p = PromptProfiler(), PromptProfiler()
    early_p.profile(early(realistic(), "【状態】A"))
    early_profile = early_p.profile(early(realistic(), "【状態】B"))
    late_p.profile(insert_before_user_turn(realistic(), "【状態】A"))
    late_profile = late_p.profile(insert_before_user_turn(realistic(), "【状態】B"))
    assert late_profile.reusable_prefix_tokens > early_profile.reusable_prefix_tokens
    assert late_profile.total_tokens == early_profile.total_tokens, "量は同じ"


def test_the_volatile_tail_can_be_measured():
    messages = insert_before_user_turn(base(), "【状態】" + "あ" * 200)
    assert volatile_tokens(messages, estimate=estimate_tokens) > 0


@pytest.mark.parametrize("content", ["", "   ", "\n"])
def test_nothing_is_inserted_for_blank_content(content):
    assert len(insert_after_stable_prefix(base(), content)) == len(base())
