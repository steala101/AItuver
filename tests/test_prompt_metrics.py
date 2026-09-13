"""Measuring why the first token takes 2.5 seconds.

Observed on real hardware: `LLM初回 p50=2540ms` out of a `合計 3500ms`, with a
prompt trimmed to the 6912-token budget on every single turn.  Evaluating
~6900 tokens on a local 12B costs about that long, so the hypothesis is that
the entire perceived delay is prompt evaluation.

If so, the fix is not to cut content but to stop re-evaluating text the server
has already seen.  An inference server can reuse the KV cache for whatever
*leading* portion of the prompt is byte-identical to the previous request —
which a per-turn block inserted near the top destroys, because everything
after the insertion point differs.

These tests cover the measurement, not a remedy.  Nothing here changes what is
sent to the model.
"""
from __future__ import annotations

import pytest

from neuro_voice.llm.prompt_metrics import PromptProfiler, label_of


def system(text):
    return {"role": "system", "content": text}


def user(text):
    return {"role": "user", "content": text}


PERSONA = "あなたはポッポ。" + "人格の説明。" * 60
RULES = "【Surface Realizer・最終発話規則】" + "話し方の規則。" * 40


# ---------------------------------------------------------------------------
# Naming the blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content,expected", [
    ("【今回の確信度】次の点について…", "system:今回の確信度"),
    ("[BEHAVIOR DIRECTIVE - 継続中の依頼]\nid=…", "system:BEHAVIOR DIRECTIVE - 継続中の依頼"),
    ("【対話制御・内部用】\nUser Model…", "system:対話制御・内部用"),
])
def test_a_block_is_named_from_its_own_first_line(content, expected):
    assert label_of(system(content)) == expected


def test_a_user_turn_is_just_a_user_turn():
    assert label_of(user("こんばんは")) == "user"


# ---------------------------------------------------------------------------
# How much of the prompt repeats
# ---------------------------------------------------------------------------


def test_the_first_prompt_has_nothing_to_reuse():
    profile = PromptProfiler().profile([system(PERSONA), user("やあ")])
    assert profile.reusable_prefix_tokens == 0
    assert profile.total_tokens > 0


def test_an_unchanged_prefix_is_fully_reusable():
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system(RULES), user("一つ目")])
    profile = profiler.profile([system(PERSONA), system(RULES), user("二つ目")])
    assert profile.reusable_messages == 2
    assert profile.reuse_ratio > 0.9
    assert profile.first_changed_label == "user"


def test_a_block_inserted_near_the_top_destroys_the_reuse():
    """This is the shape the pipeline currently produces."""
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system("【今回の確信度】迷いなし"),
                      system(RULES), user("一つ目")])
    profile = profiler.profile([system(PERSONA), system("【今回の確信度】聞き取りが怪しい"),
                                system(RULES), user("二つ目")])
    assert profile.reusable_messages == 1, "personaより後ろが全部作り直しになる"
    assert profile.first_changed_label == "system:今回の確信度"


def test_the_same_block_moved_to_the_back_keeps_the_reuse():
    """The same content, ordered so the stable part leads."""
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system(RULES),
                      system("【今回の確信度】迷いなし"), user("一つ目")])
    profile = profiler.profile([system(PERSONA), system(RULES),
                                system("【今回の確信度】聞き取りが怪しい"), user("二つ目")])
    assert profile.reusable_messages == 2
    assert profile.first_changed_label == "system:今回の確信度"


def test_ordering_alone_changes_how_much_is_reusable():
    """Same blocks, same content, different order — measurably different."""
    front, back = PromptProfiler(), PromptProfiler()
    volatile_a, volatile_b = system("【状態】A"), system("【状態】B")
    front.profile([system(PERSONA), volatile_a, system(RULES), user("x")])
    front_profile = front.profile([system(PERSONA), volatile_b, system(RULES), user("y")])
    back.profile([system(PERSONA), system(RULES), volatile_a, user("x")])
    back_profile = back.profile([system(PERSONA), system(RULES), volatile_b, user("y")])
    assert back_profile.reusable_prefix_tokens > front_profile.reusable_prefix_tokens


def test_a_partly_changed_block_still_counts_its_shared_opening():
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA + "むかしの続き"), user("x")])
    profile = profiler.profile([system(PERSONA + "あたらしい続き"), user("y")])
    assert profile.reusable_prefix_tokens > 0
    assert profile.reusable_messages == 0


def test_a_shorter_prompt_than_last_time_is_handled():
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system(RULES), user("x")])
    profile = profiler.profile([system(PERSONA), user("y")])
    assert profile.reusable_messages == 1


def test_history_trimming_to_a_different_boundary_breaks_reuse():
    """Dropping a different number of old turns shifts everything after it."""
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), user("古い1"), user("古い2"), user("新しい")])
    profile = profiler.profile([system(PERSONA), user("古い2"), user("新しい")])
    assert profile.reusable_messages == 1
    assert profile.first_changed_label == "user"


# ---------------------------------------------------------------------------
# What gets logged
# ---------------------------------------------------------------------------


def test_the_breakdown_lists_every_block():
    profile = PromptProfiler().profile([system(PERSONA), system(RULES), user("やあ")])
    assert len(profile.blocks) == 3
    assert sum(block.tokens for block in profile.blocks) > 0


def test_the_summary_names_the_first_changed_block():
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system("【状態】A"), user("x")])
    summary = profiler.profile([system(PERSONA), system("【状態】B"), user("y")]).summary()
    assert "状態" in summary
    assert "再利用可" in summary


def test_the_snapshot_is_serializable():
    snapshot = PromptProfiler().profile([system(PERSONA), user("やあ")]).snapshot()
    assert set(snapshot) >= {"total_tokens", "reusable_prefix_tokens", "reuse_ratio"}
    assert isinstance(snapshot["blocks"], list)


def test_an_empty_prompt_does_not_divide_by_zero():
    profile = PromptProfiler().profile([])
    assert profile.reuse_ratio == 0.0
    assert profile.total_tokens == 0


# ---------------------------------------------------------------------------
# Inside the one block that is 60% of the prompt
# ---------------------------------------------------------------------------


MIND_CONTEXT = (
    "[CONVERSATION KERNEL - authoritative decision for this turn]\n"
    "turn_id=abc; obligations=ANSWER\n"
    "【いま話している相手】\nチビ (親しい)\n"
    "【この会話のこれまで】\n" + "要約。" * 200 + "\n"
    "【関連する過去の記憶・内部用】\n- 前に話した内容\n"
)


def test_a_concatenated_message_is_split_into_its_sections():
    from neuro_voice.llm.prompt_metrics import split_sections

    names = [name for name, _ in split_sections(MIND_CONTEXT)]
    assert names[0].startswith("CONVERSATION KERNEL")
    assert "いま話している相手" in names
    assert "この会話のこれまで" in names


def test_a_message_without_headings_is_one_section():
    from neuro_voice.llm.prompt_metrics import split_sections

    assert len(split_sections("見出しのない本文")) == 1


def test_the_unchanged_sections_are_identified():
    """A section that repeats belongs in front of the ones that do not."""
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system(MIND_CONTEXT), user("x")])
    changed = MIND_CONTEXT.replace("turn_id=abc", "turn_id=def")
    profile = profiler.profile([system(PERSONA), system(changed), user("y")])
    assert "この会話のこれまで" in profile.biggest_label or profile.biggest_label
    stable = {row["name"] for row in profile.sections if not row["changed"]}
    assert "この会話のこれまで" in stable
    assert "いま話している相手" in stable
    assert profile.stable_section_tokens > 0


def test_the_section_summary_reports_both_sides():
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), system(MIND_CONTEXT), user("x")])
    changed = MIND_CONTEXT.replace("turn_id=abc", "turn_id=def")
    summary = profiler.profile(
        [system(PERSONA), system(changed), user("y")],
    ).section_summary()
    assert "変わらない=" in summary
    assert "変わった=" in summary


def test_the_biggest_block_is_the_one_taken_apart():
    profiler = PromptProfiler()
    profile = profiler.profile([system("短い"), system(MIND_CONTEXT), user("x")])
    assert profile.biggest_label == label_of(system(MIND_CONTEXT))


def test_a_new_section_is_marked_as_new():
    from neuro_voice.llm.prompt_metrics import section_report

    rows = section_report("【あ】ほんぶん", "【あ】ほんぶん\n【い】あたらしい")
    added = [row for row in rows if row["name"] == "い"]
    assert added and added[0]["new"] is True


def test_resetting_forgets_the_previous_prompt():
    profiler = PromptProfiler()
    profiler.profile([system(PERSONA), user("x")])
    profiler.reset()
    assert profiler.profile([system(PERSONA), user("x")]).reusable_messages == 0
