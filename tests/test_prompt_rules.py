"""The prompt must say each thing once.

Measured 2026-07-28: `persona` was 2296 tokens, of which 2109 were rules and
187 were the character.  The rules had accumulated one sentence per past bug,
each individually justified, and nobody had read them back:

* "don't output the internals" appeared in **seven places**, and the list
  『The user is asking』『Drafting response』『Wait』『Final version』 was
  duplicated word for word between `persona` and `SurfaceRealizer`;
* the emotion tag was specified twice, once as "two tags" and once as
  "exactly one tag" — the parser tolerates both, but the model had to resolve
  the contradiction before emitting its very first token;
* one line was literally written twice (persona.py 68 and 69);
* "don't end on a question" appeared four times, although `ASK_FOLLOW_UP` is
  already decided in code and handed over as yes/no.

These tests do not judge the wording.  They hold down the property that a
given instruction exists in exactly one place, so the pile cannot rebuild
itself the next time a bug is fixed by adding a sentence.
"""
from __future__ import annotations

import pytest

from neuro_voice.dialogue.conversation_critic import ConversationCritic
from neuro_voice.dialogue.conversation_planner import ConversationPlanner
from neuro_voice.dialogue.echo import is_echo
from neuro_voice.dialogue.leakage import has_internal_leak
from neuro_voice.dialogue.surface_realizer import SurfaceRealizer
from neuro_voice.llm.context_budget import estimate_tokens
from neuro_voice.memory.persona import build_system_prompt


class FakeConfig:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        return self._values.get(key, default)

    def section(self, name):
        value = self._values.get(name)
        return value if isinstance(value, dict) else {}


@pytest.fixture
def prompts():
    cfg = FakeConfig({
        "persona": {"name": "ポッポ", "first_person": "私", "user_call": "きみ",
                    "character": "明るく好奇心旺盛", "speech_style": "フランク",
                    "interests": "ゲーム", "response_length": "1〜3文"},
    })
    planner = ConversationPlanner(cfg)
    plan = planner.plan(
        "u", "今日はどうだった？", {}, momentum=.5, memory_snippets=[],
        allow_follow_up=True, turn_index=3, learned_weights={},
        value_profile={}, working_memory={}, persona_traits={},
    )
    return {
        "persona": build_system_prompt(cfg),
        "planner": planner.prompt(plan),
        "surface": SurfaceRealizer().prompt(plan),
    }


def combined(prompts):
    return "\n".join(prompts.values())


# ---------------------------------------------------------------------------
# Said once
# ---------------------------------------------------------------------------


def test_the_thinking_out_loud_examples_are_gone_from_the_prompt(prompts):
    """The enumeration was duplicated word for word, then removed entirely.

    Counting the forbidden phrases in the prompt did not stop them being
    said.  Reading the finished sentence does, and costs no tokens.
    """
    text = combined(prompts)
    for phrase in ("The user is asking", "Drafting response", "Final version", "[thought]"):
        assert phrase not in text, f"プロンプトへ列挙が戻っている: {phrase}"
    assert has_internal_leak("The user is asking about the weather.")
    assert has_internal_leak("[thought] 内部の考え")


def test_only_one_block_specifies_the_emotion_tag(prompts):
    text = combined(prompts)
    assert text.count("[emotion:") == 0
    assert "1つだけ付けること" not in text, "「2つ」と「1つだけ」が同居していた"


def test_the_duplicated_sentence_is_gone(prompts):
    assert prompts["persona"].count("ただ質問に答えるだけのアシスタントにならない") == 1


def test_the_internals_rule_lives_in_one_place(prompts):
    """persona owns the statement; planner keeps only a short pointer."""
    assert "内部の分析・下書き・候補・数値は出さず" in prompts["persona"]
    assert prompts["persona"].count("内部の分析") == 1
    assert "内部の分析" not in prompts["surface"]


def test_the_parrot_rule_moved_out_of_the_prompt(prompts):
    """「オウム返しはしない」は ReplyEchoGuard が担う。"""
    assert "オウム返し" not in combined(prompts)
    user_said = "今のAIって話しててもAI感が強いんだよね。返事は返ってくるけど中身が薄い。"
    assert is_echo(user_said, ["", user_said])
    assert not is_echo("分かる。定型文だと冷めちゃうよね。", ["", user_said])


def test_the_canned_opening_rule_is_no_longer_always_on(prompts):
    """起きてもいない違反を毎ターン先回りで禁じない。

    ConversationCritic が実際に検出した時だけ、フィードバックが同じことを言う。
    """
    assert "『なるほど』『確かに』『そうだね』から始めない" not in combined(prompts)
    feedback = ConversationCritic.feedback_prompt({"issues": ["canned_opening"]})
    assert "同じ相槌から始めず" in feedback
    assert ConversationCritic.feedback_prompt({"issues": []}) == ""


def test_the_planner_no_longer_dumps_persona_numbers(prompts):
    """SurfaceRealizer が同じ数値を日本語の指示へ変換済み。二重だった。"""
    assert "人格モジュール=" not in prompts["planner"]
    assert "curiosity=" not in prompts["planner"]


def test_recent_reply_context_uses_concrete_openings_and_is_bounded(prompts):
    """Dynamic evidence may vary; it must not become another full transcript."""
    planner = ConversationPlanner(FakeConfig())
    plan = planner.plan(
        "u", "続きを話そう", {}, momentum=.5, turn_index=4,
    )
    surface = SurfaceRealizer().prompt(
        plan,
        recent_replies=[
            "最初の具体的な返答です。ここから先の本文は渡さない。",
            "別の具体的な返答です。これも後半は不要。",
        ],
        recent_reply_count=2,
        recent_reply_max_chars=24,
        recent_reply_max_total_chars=180,
    )
    assert "最初の具体的な返答です。" in surface
    assert "ここから先の本文は渡さない" not in surface
    dynamic = surface.split("【直近の自分の発話", 1)[1]
    assert len(dynamic) < 300


def test_the_candidate_list_is_trimmed(prompts):
    """Planner は既に最上位を選んでいる。残りは「選ばれなかったもの」。"""
    planner = prompts["planner"]
    assert "score=" not in planner
    assert planner.count("- 候補") <= 2


def test_the_planner_still_marks_its_own_dump(prompts):
    """The note has to sit next to the numbers it is about."""
    assert "上記はすべて内部値" in prompts["planner"]


def test_the_question_rule_is_the_per_turn_one(prompts):
    """`ASK_FOLLOW_UP` is decided in code; only the surface layer repeats it."""
    assert "質問だけで終わらない" in prompts["surface"]
    assert "質問だけで終える" not in prompts["planner"]
    assert "会話を続けるためだけの質問を足さない" not in prompts["persona"]


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


def test_the_always_on_prompt_stays_under_its_budget(prompts):
    """3642 → 3177（重複の削除）→ 2899（規則をコードへ移設）。

    上限は、削れた分だけ下げる。下げないと、次に不具合を直す人が
    「まだ余裕がある」と読んで一文足すところから同じ山が積み上がる。
    """
    total = sum(estimate_tokens(text) for text in prompts.values())
    assert total <= 3000, f"常時ONのプロンプトが増えている: {total}tok"


def test_the_character_is_still_there(prompts):
    """Cutting rules must not cut Poppo."""
    persona = prompts["persona"]
    for fragment in ("ポッポ", "【キャラクター設定】", "好奇心"):
        assert fragment in persona


def test_the_rules_that_need_judgement_are_kept(prompts):
    """Only duplicates were removed, not the constitution's invariants."""
    persona = prompts["persona"]
    assert "事実性・非迎合ルール" in persona
    assert "自動的に正しいものとして扱わない" in persona
    assert "音声認識" in persona, "ASR誤変換の扱いは判断が要る"
