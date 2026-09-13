from pathlib import Path
import tempfile

from neuro_voice.dialogue.adaptive_store import AdaptiveConversationStore
from neuro_voice.dialogue.conversation_contract import (
    BRIEF_REACTION, DIRECT_ANSWER, REPAIR,
)
from neuro_voice.dialogue.expression_plan import build_expression_plan
from neuro_voice.dialogue.intelligence import DialogueIntelligence
from neuro_voice.dialogue.reaction_learning import ReactionLearner
from neuro_voice.dialogue.selection_kernel import ConversationSelectionKernel
from neuro_voice.dialogue.semantic_graph import SemanticAssociationGraph


def test_selection_kernel_answers_questions_before_expanding():
    result = ConversationSelectionKernel().select(
        text="肉はどうやって焼くの？",
        intent="question",
        primary_style="explanatory",
        target_length="medium",
        allow_follow_up=True,
        momentum=.72,
    )
    assert result.selected_move == DIRECT_ANSWER
    assert len(result.candidates) >= 4


def test_selection_kernel_closes_acknowledgement_briefly():
    result = ConversationSelectionKernel().select(
        text="うん",
        intent="statement",
        primary_style="reflective",
        target_length="short",
        allow_follow_up=True,
        momentum=.60,
    )
    assert result.selected_move == BRIEF_REACTION


def test_selection_kernel_keeps_correction_as_hard_obligation():
    result = ConversationSelectionKernel().select(
        text="違う、そういう意味じゃない",
        intent="correction",
        primary_style="playful",
        target_length="medium",
        allow_follow_up=True,
        momentum=.80,
        group=True,
    )
    assert result.selected_move == REPAIR
    assert result.reason_codes == ("hard_obligation",)


def test_selection_kernel_varies_soft_moves_but_not_required_answers():
    kernel = ConversationSelectionKernel()
    first = kernel.select(
        text="この設計、私は面白いと思う",
        intent="statement",
        primary_style="opinionated",
        target_length="medium",
        allow_follow_up=False,
        momentum=.65,
    )
    varied = kernel.select(
        text="この設計、私は面白いと思う",
        intent="statement",
        primary_style="opinionated",
        target_length="medium",
        allow_follow_up=False,
        momentum=.65,
        recent_moves=[first.selected_move],
    )
    assert varied.selected_move != first.selected_move

    required = kernel.select(
        text="理由は何？",
        intent="question",
        primary_style="explanatory",
        target_length="medium",
        allow_follow_up=False,
        momentum=.65,
        recent_moves=[DIRECT_ANSWER],
    )
    assert required.selected_move == DIRECT_ANSWER


def test_reaction_learning_is_bounded_and_structured():
    signal = ReactionLearner.classify(
        "また同じこと言ってるよ",
        previous_move="EXTEND_TOPIC",
        surface="discord",
        group=True,
    )
    assert signal is not None
    assert signal.kind == "repeat_complaint"
    assert signal.group is True
    assert .70 <= ReactionLearner.updated_weight(1.0, signal) < 1.0
    assert "また同じ" not in str(signal.snapshot())


def test_semantic_graph_direct_and_group_session_boundaries():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "adaptive.db"
        store = AdaptiveConversationStore(path, session_id="session-a")
        graph = SemanticAssociationGraph(store)
        graph.observe(
            "alice", ["人工知能", "会話設計"], group=False,
            source="local",
        )
        direct = graph.associations(
            "alice", ["人工知能"], group=False,
        )
        assert direct and direct[0].target == "会話設計"
        assert graph.observe_correction(
            "alice", "深堀りじゃなくて深掘りだよ",
            group=False, source="local",
        )
        corrected = graph.associations(
            "alice", ["深堀り"], group=False,
        )
        assert corrected and corrected[0].target == "深掘り"
        assert corrected[0].relation == "corrected_to"

        graph.observe(
            "alice", ["マイクラ", "料理"], group=True,
            source="discord",
        )
        current_group = graph.associations(
            "alice", ["マイクラ"], group=True,
        )
        assert current_group and current_group[0].target == "料理"

        other_session = AdaptiveConversationStore(path, session_id="session-b")
        other_graph = SemanticAssociationGraph(other_session)
        assert not other_graph.associations(
            "alice", ["マイクラ"], group=True,
        )


def test_expression_plan_keeps_avatar_as_disabled_future_contract():
    plan = build_expression_plan(
        {
            "response_id": "r-1",
            "source": "local",
            "conversation_move": "PLAYFUL_REACTION",
            "ai_emotion": "happy",
            "primary_style": "playful",
            "temperature": "lively",
            "target_energy": .8,
        },
        source="local",
    )
    snapshot = plan.snapshot()
    assert snapshot["voice"]["delivery"] == "playful"
    assert snapshot["avatar"]["expression"] == "smile"
    assert snapshot["avatar"]["enabled"] is False
    assert snapshot["response_id"] == "r-1"


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_dialogue_integrates_selection_reaction_graph_and_expression():
    with tempfile.TemporaryDirectory() as tmp:
        dialogue = DialogueIntelligence(
            _Config(), Path(tmp) / "dialogue_neuro.json",
            persona_key="neuro",
        )
        dialogue.observe_turn(
            "alice", "人工知能と会話設計を一緒に考えたい",
            source="local", group=False,
        )
        context = dialogue.prompt_context(
            "alice", "会話設計はどうすれば自然になる？",
            response_id="r-1", source="local",
        )
        plan = dialogue.current_plan("alice")
        assert context and "会話行為=" in context
        assert plan["move_candidates"]
        assert plan["conversation_move"] == DIRECT_ANSWER
        assert dialogue.expression_plan(
            "alice", source="local",
        )["avatar"]["enabled"] is False

        dialogue.record_response(
            "alice", "私は、返し方を一つに固定しないのが大事だと思う。",
        )
        dialogue.observe_turn(
            "alice", "また同じこと言ってるよ",
            source="local", group=False,
        )
        updates = dialogue.snapshot("alice")["learning_updates"]
        move_updates = [
            item for item in updates
            if str(item.get("feature", "")).startswith("move:")
        ]
        assert move_updates
        assert move_updates[0]["weight"] < 1.0
        dialogue.close()
