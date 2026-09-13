"""Reasoned, low-latency planning for varied spoken conversation.

The planner decides *what kind of conversational move to make*.  It does not
write the final reply and it never exposes chain-of-thought.  Selection is
deterministic for the current state, with penalties for recently used styles
and shapes, so variation has a conversational reason rather than pure random
noise.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import re
from typing import Any, Iterable, Protocol

from neuro_voice.dialogue.feature_engines import ConversationFeatureSuite
from neuro_voice.dialogue.feature_models import (
    CandidateEvaluation, ConversationFeatureProposal, ValueProfile,
)
from neuro_voice.dialogue.conversation_contract import (
    ASK_QUESTION,
    choose_conversation_move,
)
from neuro_voice.dialogue.selection_kernel import ConversationSelectionKernel
from neuro_voice.dialogue.working_memory import WorkingMemory, normalize_persona_traits


@dataclass(frozen=True, slots=True)
class TopicCandidate:
    topic: str
    source: str
    move: str
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class ConversationPlan:
    turn_id: int
    intent: str
    primary_style: str
    secondary_style: str
    response_shape: str
    ai_emotion: str
    temperature: str
    target_length: str
    rhythm: str
    humor_level: str
    ask_follow_up: bool
    selected_topic: str
    candidates: tuple[TopicCandidate, ...]
    reason: str
    primary_goal: str = "ユーザーへ直接応答する"
    secondary_goal: str | None = None
    direct_answer_required: bool = True
    selected_features: tuple[ConversationFeatureProposal, ...] = ()
    feature_evaluations: tuple[CandidateEvaluation, ...] = ()
    rejected_features: tuple[CandidateEvaluation, ...] = ()
    engine_variant: str = "enhanced"
    target_energy: float = .5
    intimacy_level: float = .5
    creativity_level: float = .4
    question_policy: str = "optional"
    memory_usage_policy: str = "high_confidence_only"
    prohibited_patterns: tuple[str, ...] = ()
    continuity_focus: str = ""
    continuity_question: str = ""
    persona_traits: dict[str, float] | None = None
    continuation_requested: bool = False
    minimum_moves: int = 2
    conversation_move: str = "EXTEND_TOPIC"
    response_id: str = ""
    source: str = ""
    recall_requested: bool = False
    recall_grounded: bool = False
    grounded_experience_claims: tuple[str, ...] = ()
    move_candidates: tuple[dict[str, Any], ...] = ()
    selection_reasons: tuple[str, ...] = ()
    semantic_associations: tuple[str, ...] = ()
    group_context: bool = False
    #: 持続している内面から来た表現の制約（Phase 4 の `expression_constraints`）。
    #:
    #: **粗いラベルだけ。** 生の数値も履歴も入らない。ここから文章を作らせず、
    #: 口調・返答量・断定の強さ・冗談の可否にだけ効かせる。
    internal_state: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class ConversationCandidateProvider(Protocol):
    """Extension point for future humor/story/imagination/value engines."""

    name: str

    def propose(
        self, *, text: str, profile: dict[str, Any], intent: str, momentum: float,
    ) -> Iterable[TopicCandidate]: ...


_STYLE_INSTRUCTIONS = {
    "direct": "結論や率直な感想から入り、前置きを短くする",
    "empathetic": "感情を決めつけず、具体的な一点に気持ちを寄せる",
    "explanatory": "要点を整理して、理由か具体例を一つだけ添える",
    "playful": "空気を読んだ軽いツッコミか小さな冗談を混ぜる",
    "reflective": "少し考えた見解や別の見方を返す",
    "imaginative": "もしもの展開や情景を一緒に広げる",
    "analogy": "分かりやすい短い例えを使う",
    "storylike": "小さな場面や流れとして語る",
    "curious": "発話中の具体的な一点へ、本物の興味を向ける",
    "concise": "一息で返せる短文にする",
    "deep_dive": "答え、予想、考察をつなぎ、必要なら追加質問する",
    "cautious": "確信度を分け、知らないことや推測を率直に示す",
    "energetic": "喜びや驚きを少し強め、テンポよく返す",
    "quiet": "落ち着いた短い言葉で、余白を残す",
    "opinionated": "迎合せず、理由のある自分の見解を示す",
}

_SHAPE_INSTRUCTIONS = {
    "direct_take": "直接の答え・見解 → 必要な補足",
    "react_expand": "具体的な反応 → 関連する一歩先の話",
    "prediction_reflection": "こうかもしれないという予想 → その理由",
    "association": "今の話 → 自然に連想した近い話題",
    "playful_twist": "短い反応 → 軽いツッコミ → 本題",
    "imagine_together": "もしもの情景 → 今の話へ戻す",
    "concise_ping": "短い一言または二言で返し、説明を足しすぎない",
    "memory_bridge": "関連する確かな過去の話 → 今どうつながるか",
    "question_hypothesis": "答え → 予想 → 考察 → 答えやすい追加質問",
    "proposal_path": "直接の答え → AI自身の見方 → 実行しやすい次の一歩",
    "gentle_support": "具体的な受け止め → 小さな選択肢または余白",
    "self_reconsider": "第一印象 → 『いや待って』程度の自然な考え直し → 見解",
    "threaded_monologue": "一つの話題 → 自分の見方 → 近い連想 → 次の区間へ残す余韻",
}

_INTENT_RE = {
    "monologue": re.compile(
        r"(?:ラジオ風|ラジオみたい|しばらく|少しの間|話し続け|喋り続け|"
        r"しゃべり続け|何か話してて)"
    ),
    "correction": re.compile(
        r"(?:違う|訂正|修正|そうじゃない|いや、それ|いやそれ|"
        r"っていう意味|という意味)"
    ),
    "support": re.compile(r"(?:つら|疲れ|しんど|不安|悲し|最悪|困っ|痛い|眠い)"),
    "good_news": re.compile(r"(?:できた|成功|勝った|受かった|嬉し|最高|達成|やった)"),
    "preference": re.compile(r"(?:好き|嫌い|苦手|興味|やりたい|欲しい|ハマっ)"),
    "story": re.compile(r"(?:今日|昨日|この前|さっき|行った|見た|食べた|会った|遊んだ)"),
    "question": re.compile(r"[?？]|(?:教えて|どう|なぜ|なんで|何|どこ|いつ|誰)"),
}

_BRIEF_REPLY = re.compile(
    r"^(?:うん+|はい+|そう|そうだね|なるほど|了解|わかった|分かった|ありがとう|"
    r"へえ+|ほう|ああ|おっけー|オッケー)[。！!？?、,\s]*$"
)
_SHORT_REQUEST = re.compile(
    r"(?:短め|簡潔|手短|要点だけ|一言で)(?:に|で)?(?:答え|返|説明|話|教え)"
)
_DETAIL_REQUEST = re.compile(
    r"(?:詳しく|詳細に|丁寧に|深掘り|掘り下げ|理由も|手順も)"
    r"(?:教え|説明|話|聞かせ|知りたい)?"
)


#: 内面のラベル → プロンプトへ書く一言。
#:
#: **ラベルからしか作らない。** 生の数値を渡すと、モデルが数字を根拠に
#: 語り始める（「今の私の苛立ちは0.4なので」）。中身も作らせない——
#: 効かせるのは口調・返答量・断定の強さ・冗談の可否だけ。
_STANCE_WORDS = {
    "guarded": "少し身構えている。語気を強めず、短く落ち着いて返す",
    "winding_down": "会話を終える流れ。広げずに締める",
    "relaxed": "打ち解けている。肩の力を抜いた言い方でよい",
    "neutral": "",
}
_VERBOSITY_WORDS = {"short": "短く", "medium": "ふつうの長さで"}
_ASSERTIVENESS_WORDS = {
    "hedged": "断定を避け、確信の度合いを自然に示す",
    "plain": "",
}


def _internal_state_line(internal: dict[str, Any] | None) -> str:
    """持続している内面を、口調の指示として一行に落とす。

    **これは Phase 4 から繋がっていなかった配線。** `expression_constraints()`
    は形を返していたが、読む側が無かったのでフラグを上げても口調が変わらなかった。
    """
    if not internal:
        return ""
    constraints = dict(internal.get("expression_constraints") or {})
    stance = dict(internal.get("stance") or {})
    parts: list[str] = []
    mood = _STANCE_WORDS.get(str(stance.get("mode", "")), "")
    if mood:
        parts.append(mood)
    verbosity = _VERBOSITY_WORDS.get(str(constraints.get("verbosity", "")), "")
    if verbosity:
        parts.append(f"返答は{verbosity}")
    assertiveness = _ASSERTIVENESS_WORDS.get(str(constraints.get("assertiveness", "")), "")
    if assertiveness:
        parts.append(assertiveness)
    if constraints.get("humor_allowed") is False:
        parts.append("いまは冗談を挟まない")
    if str(constraints.get("intensity", "")) == "subtle":
        parts.append("表現の強度は控えめに")
    if not parts:
        return ""
    return "いまの構え=" + "、".join(parts) + "。\n"


def _stable_jitter(seed: str) -> float:
    raw = hashlib.sha256(seed.encode("utf-8", errors="ignore")).digest()
    return int.from_bytes(raw[:2], "big") / 65535.0 * 0.08


class ConversationPlanner:
    """Produce four weighted topic/move candidates and select one plan."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._enabled = bool(cfg.get("dialogue.conversation_generation.enabled", True))
        self._candidate_count = max(2, min(6, int(cfg.get(
            "dialogue.conversation_generation.candidate_count", 4,
        ))))
        self._repeat_penalty = max(0.0, min(1.0, float(cfg.get(
            "dialogue.conversation_generation.style_repeat_penalty", 0.72,
        ))))
        self._emotion_inertia = max(0.45, min(0.95, float(cfg.get(
            "dialogue.conversation_generation.emotion_inertia", 0.78,
        ))))
        self._candidate_providers: list[ConversationCandidateProvider] = []
        self._feature_suite = ConversationFeatureSuite(cfg)
        self._selection_kernel = ConversationSelectionKernel()
        self._selection_enabled = bool(
            cfg.get("dialogue.selection_kernel.enabled", True)
        )
        self._variant = str(cfg.get("conversation_engine.variant", "enhanced"))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self) -> None:
        """Refresh runtime-selectable settings after the GUI writes Config."""
        self._variant = str(self._cfg.get("conversation_engine.variant", "enhanced"))

    def register_candidate_provider(self, provider: ConversationCandidateProvider) -> None:
        """Register a bounded provider without coupling it to response wording."""
        if provider not in self._candidate_providers:
            self._candidate_providers.append(provider)

    @staticmethod
    def ensure_state(profile: dict[str, Any]) -> dict[str, Any]:
        state = profile.setdefault("conversation_generation", {})
        state.setdefault("recent_styles", [])
        state.setdefault("recent_shapes", [])
        state.setdefault("recent_replies", [])
        state.setdefault("ai_emotion", {"valence": 0.15, "arousal": 0.35, "label": "calm"})
        state.setdefault("last_plan", {})
        state.setdefault("last_critique", {})
        state.setdefault("recent_features", [])
        state.setdefault("recent_feature_patterns", [])
        state.setdefault("feature_feedback", {})
        state.setdefault("last_feature_evaluations", [])
        state.setdefault("recent_moves", [])
        return state

    @staticmethod
    def _intent(text: str) -> str:
        for intent in ("correction", "support", "monologue", "good_news", "question", "preference", "story"):
            if _INTENT_RE[intent].search(text):
                return intent
        return "statement"

    @staticmethod
    def _temperature(intent: str, momentum: float, text: str) -> str:
        if intent in {"support", "correction"}:
            return "quiet" if intent == "support" else "serious"
        if intent == "good_news" or re.search(r"[!！]{1,}|(?:笑|ウケる|面白)", text):
            return "lively"
        if momentum >= 0.72:
            return "engaged"
        if momentum <= 0.28:
            return "calm"
        return "casual"

    def _emotion(self, state: dict[str, Any], intent: str, temperature: str) -> str:
        old = state["ai_emotion"]
        targets = {
            "support": (-0.05, 0.20), "correction": (0.02, 0.30),
            "monologue": (0.42, 0.48),
            "good_news": (0.80, 0.78), "question": (0.35, 0.43),
            "preference": (0.45, 0.48), "story": (0.48, 0.52),
            "statement": (0.25, 0.36),
        }
        target_v, target_a = targets[intent]
        inertia = self._emotion_inertia
        valence = float(old.get("valence", .15)) * inertia + target_v * (1 - inertia)
        arousal = float(old.get("arousal", .35)) * inertia + target_a * (1 - inertia)
        if temperature == "lively" and arousal > .48:
            label = "excited" if valence > .42 else "surprised"
        elif intent == "support" and valence < .18:
            label = "concerned"
        elif valence > .42:
            label = "happy"
        elif arousal < .27:
            label = "gentle"
        elif intent in {"question", "preference", "story"}:
            label = "interested"
        else:
            label = "calm"
        state["ai_emotion"] = {
            "valence": round(valence, 3), "arousal": round(arousal, 3), "label": label,
        }
        return label

    @staticmethod
    def _target_length(
        text: str, *, intent: str, primary_style: str,
        momentum: float, learned_preference: str,
    ) -> str:
        """Resolve this turn's length; the learned preference is only a bias.

        A one-off request containing ``詳しく`` used to persist ``long`` in
        UserModel and then force every later turn to be long.  Content demand
        and the current conversational turn must win over that stale setting.
        """
        value = str(text or "").strip()
        preference = (
            learned_preference if learned_preference in {"short", "medium", "long"}
            else "medium"
        )
        if intent == "monologue" or primary_style == "deep_dive" or _DETAIL_REQUEST.search(value):
            return "long"
        if (
            primary_style == "concise"
            or momentum < .23
            or _BRIEF_REPLY.fullmatch(value)
            or _SHORT_REQUEST.search(value)
        ):
            return "short"
        if preference == "short":
            return "short"
        # A durable long-form preference may expand a genuinely substantive
        # turn, but must not turn greetings, acknowledgements, and ordinary
        # one-line exchanges into a monologue.
        if preference == "long" and (
            len(value) >= 56
            or (intent == "question" and len(value) >= 32)
        ):
            return "long"
        return "medium"

    @staticmethod
    def _style_weights(
        intent: str, temperature: str, persona_traits: dict[str, float] | None = None,
    ) -> dict[str, float]:
        common = {"direct": .56, "reflective": .52, "concise": .40, "opinionated": .42}
        by_intent = {
            "correction": {"direct": .95, "cautious": .86, "reflective": .58},
            "support": {"empathetic": .96, "quiet": .91, "reflective": .66, "direct": .32},
            "monologue": {"storylike": .95, "reflective": .88, "imaginative": .72, "curious": .32},
            "good_news": {"energetic": .96, "playful": .82, "curious": .61, "storylike": .42},
            "question": {"explanatory": .90, "direct": .86, "analogy": .65, "deep_dive": .61, "cautious": .55},
            "preference": {"curious": .82, "opinionated": .73, "playful": .56, "imaginative": .48},
            "story": {"curious": .84, "storylike": .75, "playful": .58, "reflective": .57},
            "statement": {"opinionated": .70, "reflective": .68, "playful": .56, "imaginative": .48, "concise": .45},
        }
        common.update(by_intent[intent])
        traits = normalize_persona_traits(persona_traits)
        # These are weights for independent persona modules, not wording
        # commands.  Repeat penalties still win, so a high trait never turns
        # into a fixed verbal template.
        common["curious"] = common.get("curious", .0) + (traits["curiosity"] - .5) * .55
        common["playful"] = (
            common.get("playful", .0)
            + (traits["humor"] - .5) * .42
            + (traits["playfulness"] - .5) * .48
            + (traits["spontaneity"] - .5) * .18
        )
        common["energetic"] = (
            common.get("energetic", .0)
            + (traits["spontaneity"] - .5) * .28
            + (traits["emotional_expression"] - .5) * .18
        )
        common["opinionated"] = (
            common.get("opinionated", .0)
            + (traits["contrarian"] - .5) * .52
            + (traits["cheekiness"] - .5) * .24
        )
        common["empathetic"] = common.get("empathetic", .0) + (traits["empathy"] - .5) * .55
        common["reflective"] = common.get("reflective", .0) + (traits["emotional_expression"] - .5) * .25
        if temperature == "lively":
            common["energetic"] = max(common.get("energetic", 0), .84)
            common["playful"] = max(common.get("playful", 0), .72)
        return common

    def _pick_ranked(self, weights: dict[str, float], recent: list[str], seed: str) -> list[str]:
        scored = []
        for name, score in weights.items():
            if name in recent[-1:]:
                score -= self._repeat_penalty
            elif name in recent[-4:]:
                score -= self._repeat_penalty * .45
            score += _stable_jitter(f"{seed}:{name}")
            scored.append((score, name))
        scored.sort(reverse=True)
        return [name for _, name in scored]

    def _candidates(
        self, text: str, profile: dict[str, Any], memory_snippets: list[str],
        intent: str, momentum: float,
    ) -> tuple[TopicCandidate, ...]:
        topics = list(dict.fromkeys(profile.get("topics", [])))
        current = topics[-1] if topics else (text.strip()[:50] or "今の話")
        candidates: list[TopicCandidate] = [
            TopicCandidate(current, "current", "発話の具体的な一点へ直接返す", .96, "現在の発話との関連が最も高い"),
        ]
        if memory_snippets:
            candidates.append(TopicCandidate(
                memory_snippets[0][:90], "long_memory", "以前の話と今を自然につなぐ",
                .76 if momentum >= .48 else .58, "明示的に取得できた高関連記憶",
            ))
        facts = profile.get("model", {}).get("facts", {})
        interests = list(facts.get("interests", [])) + list(facts.get("current_projects", []))
        if interests:
            candidates.append(TopicCandidate(
                interests[-1][:80], "user_model", "確定している関心との接点を一つ示す",
                .68, "ユーザーが明示した興味・プロジェクト",
            ))
        if len(topics) >= 2:
            candidates.append(TopicCandidate(
                topics[-2][:80], "recent_topic", "近い話題へ少しだけ連想を広げる",
                .57, "直近の会話に現れた話題",
            ))
        for provider in self._candidate_providers:
            try:
                for proposed in provider.propose(
                    text=text, profile=profile, intent=intent, momentum=momentum,
                ):
                    if isinstance(proposed, TopicCandidate):
                        candidates.append(proposed)
            except Exception:
                # Optional engines must never break the realtime response path.
                continue
        fallback = [
            ("別の見方", "perspective", "今の話を別角度から見る", .52),
            ("具体的な次の場面", "continuation", "一歩先の展開を想像する", .50),
            ("短い予想", "inference", "断定せず予想と理由を添える", .48),
            ("軽いツッコミ", "humor", "空気に合う場合だけ軽く茶化す", .42),
        ]
        for topic, source, move, score in fallback:
            if len(candidates) >= self._candidate_count:
                break
            if intent in {"support", "correction"} and source == "humor":
                continue
            candidates.append(TopicCandidate(topic, source, move, score, "現在の会話状態から選べる展開"))
        candidates.sort(key=lambda item: item.score, reverse=True)
        return tuple(candidates[:self._candidate_count])

    def plan(
        self, user_key: str, text: str, profile: dict[str, Any], *, momentum: float,
        memory_snippets: list[str] | None = None, allow_follow_up: bool = False,
        turn_index: int = 0, learned_weights: dict[str, float] | None = None,
        value_profile: ValueProfile | None = None,
        working_memory: dict[str, Any] | None = None,
        persona_traits: dict[str, float] | None = None,
        response_id: str = "", source: str = "",
        recall_requested: bool = False, recall_grounded: bool = False,
        semantic_associations: list[str] | tuple[str, ...] | None = None,
        group: bool = False,
        internal_state: dict[str, Any] | None = None,
    ) -> ConversationPlan:
        state = self.ensure_state(profile)
        traits = normalize_persona_traits(persona_traits)
        intent = self._intent(text)
        temperature = self._temperature(intent, momentum, text)
        emotion = self._emotion(state, intent, temperature)
        seed = f"{user_key}:{turn_index}:{text[:120]}"
        ranked = self._pick_ranked(
            self._style_weights(intent, temperature, traits), list(state["recent_styles"]), seed,
        )
        primary = ranked[0]
        secondary = next((item for item in ranked[1:] if item != primary), "direct")
        shape_weights = {
            "direct_take": .68, "react_expand": .62, "prediction_reflection": .55,
            "association": .48,
            "playful_twist": (
                .34 + .34 * traits["playfulness"] + .18 * traits["cheekiness"]
                if intent not in {"support", "correction"} else .05
            ),
            "imagine_together": .50 if primary == "imaginative" else .25,
            "concise_ping": .58 if primary == "concise" else .31,
            "memory_bridge": .70 if memory_snippets else .10,
            "question_hypothesis": .66 if intent == "question" and allow_follow_up else .24,
            "gentle_support": .92 if intent == "support" else .12,
            "self_reconsider": .36 if intent in {"statement", "preference", "story"} else .08,
            "proposal_path": (.34 + .38 * traits["suggestion"])
                if intent in {"statement", "preference", "question"} else .08,
            "threaded_monologue": .98 if intent == "monologue" else .04,
        }
        shape = self._pick_ranked(shape_weights, list(state["recent_shapes"]), seed + ":shape")[0]
        if intent == "monologue":
            shape = "threaded_monologue"
        candidates = self._candidates(text, profile, memory_snippets or [], intent, momentum)
        ask = bool(allow_follow_up and traits["curiosity"] >= .30 and intent not in {"correction", "support"})
        if intent == "monologue":
            ask = False
        if state.get("last_critique", {}).get("consecutive_question"):
            ask = False
        length_pref = profile.get("model", {}).get(
            "interaction_policy", {},
        ).get("response_length", "medium")
        length = self._target_length(
            text,
            intent=intent,
            primary_style=primary,
            momentum=momentum,
            learned_preference=str(length_pref),
        )
        rhythm = {
            "short": "一息か二息。短文を許可", "medium": "文の長短を混ぜ、二〜四文",
            "long": "要点ごとに区切るが、音声として長引かせすぎない",
        }[length]
        humor = "none" if intent in {"support", "correction"} else ("light" if primary in {"playful", "energetic"} else "optional")
        effective_weights = dict(learned_weights or {})
        effective_weights["humor_engine"] = effective_weights.get("humor_engine", 1.0) * (
            .45 + .38 * traits["humor"] + .28 * traits["playfulness"] + .14 * traits["cheekiness"]
        )
        effective_weights["casual_conversation_engine"] = effective_weights.get("casual_conversation_engine", 1.0) * (.65 + .70 * traits["topic_shift"])
        effective_weights["value_system"] = effective_weights.get("value_system", 1.0) * (
            .55 + .48 * traits["contrarian"] + .22 * traits["cheekiness"]
        )
        effective_weights["relationship_evolution"] = effective_weights.get("relationship_evolution", 1.0) * (.65 + .70 * traits["empathy"])
        if self._variant == "baseline":
            feature_selection = self._feature_suite.select(
                text=text, profile=profile, intent=intent, momentum=momentum,
                learned_weights={}, values=value_profile,
            )
            selected_features: tuple[ConversationFeatureProposal, ...] = ()
        else:
            if self._variant == "experimental":
                # Experimental keeps the same safety gates but lets borderline
                # proposals compete slightly more often for A/B observation.
                for feature in self._feature_suite.ENGINES:
                    name = feature.feature_type
                    effective_weights[name] = min(1.75, effective_weights.get(name, 1.0) * 1.08)
            feature_selection = self._feature_suite.select(
                text=text, profile=profile, intent=intent, momentum=momentum,
                learned_weights=effective_weights, values=value_profile,
            )
            selected_features = feature_selection.selected
        relationship = _feature_relationship(profile)
        working = working_memory or WorkingMemory.snapshot(profile)
        revisit = WorkingMemory.revisit_candidate(
            profile, intent=intent, momentum=momentum, turn_index=turn_index,
            topic_shift=traits["topic_shift"], curiosity=traits["curiosity"],
        )
        creativity = max(
            [0.25] + [item.activation_score for item in selected_features
                      if item.feature_type in {"imagination_engine", "story_engine", "playful_fantasy_engine"}],
        )
        # 「内部スコアの読み上げ」は直後の「上記はすべて内部値」と、personaの最終出力
        # ルールで担う。「質問だけで終える」は SurfaceRealizer が ASK_FOLLOW_UP の
        # 判定結果に応じて毎ターン書き分けている。ここへ重ねると、判定済みの答えが
        # あるのに同じことを三度言うことになる。
        prohibited = ["架空経験の事実化"]
        if intent in {"support", "correction"}:
            prohibited.extend(["不必要な冗談", "話題の過剰な拡張"])
        if self._selection_enabled:
            selection = self._selection_kernel.select(
                text=text,
                intent=intent,
                primary_style=primary,
                target_length=length,
                allow_follow_up=ask,
                momentum=momentum,
                recent_moves=list(state.get("recent_moves", [])),
                learned_weights=learned_weights,
                group=group,
                has_memory=bool(memory_snippets),
                has_association=bool(semantic_associations),
                recall_requested=recall_requested,
                recall_grounded=recall_grounded,
            )
            move = selection.selected_move
            move_candidates = tuple(item.snapshot() for item in selection.candidates)
            selection_reasons = selection.reason_codes
        else:
            move = choose_conversation_move(
                intent=intent, primary_style=primary, target_length=length,
                ask_follow_up=ask, recall_is_requested=recall_requested,
                recall_is_grounded=recall_grounded,
            )
            move_candidates = ()
            selection_reasons = ("selection_kernel_disabled",)
        # A question is an explicit conversational move, not a suffix added to
        # every otherwise complete response.
        ask = move == ASK_QUESTION
        plan = ConversationPlan(
            turn_id=turn_index, intent=intent, primary_style=primary,
            secondary_style=secondary, response_shape=shape, ai_emotion=emotion,
            temperature=temperature, target_length=length, rhythm=rhythm,
            humor_level=humor, ask_follow_up=ask,
            selected_topic=candidates[0].topic if candidates else "今の話",
            candidates=candidates,
            reason=f"intent={intent}, momentum={momentum:.2f}, recent styles were penalized",
            secondary_goal=(
                (f"回答後、保留していた問いを自然に一つだけ戻す: {revisit['question']}"
                 if revisit else (selected_features[0].expected_effect if selected_features else None))
            ),
            selected_features=selected_features,
            feature_evaluations=feature_selection.evaluations,
            rejected_features=feature_selection.rejected,
            engine_variant=self._variant,
            target_energy=round(.35 + .45 * momentum, 3),
            intimacy_level=round(float(relationship.get("comfort", .5)), 3),
            creativity_level=round(min(1.0, creativity), 3),
            question_policy="one_after_opinion" if ask else "do_not_force",
            prohibited_patterns=tuple(prohibited),
            continuity_focus=str(working.get("continuing_thought", ""))[:180],
            continuity_question=str(revisit.get("question", "")) if revisit else "",
            persona_traits=traits,
            continuation_requested=intent == "monologue",
            minimum_moves=3 if intent == "monologue" else (1 if length == "short" else 2),
            conversation_move=move,
            response_id=str(response_id or ""),
            source=str(source or ""),
            recall_requested=bool(recall_requested),
            recall_grounded=bool(recall_grounded),
            move_candidates=move_candidates,
            selection_reasons=selection_reasons,
            semantic_associations=tuple(
                str(item)[:80] for item in (semantic_associations or [])[:2]
            ),
            group_context=bool(group),
            internal_state=dict(internal_state or {}),
        )
        state["last_plan"] = plan.snapshot()
        state["last_feature_evaluations"] = [item.snapshot() for item in feature_selection.evaluations]
        return plan

    @staticmethod
    def prompt(plan: ConversationPlan) -> str:
        # Candidate scores and rejected alternatives have already done their
        # job in Python. Passing them to the model made the prompt larger
        # without changing the selected answer in the measured A/B run.
        features = " / ".join(
            f"{item.feature_type}: {item.suggested_expression}"
            for item in plan.selected_features[:2]
        )
        speculative = any(item.is_speculative for item in plan.selected_features)
        return (
            "【Conversation Planner・内部方針】\n"
            f"会話行為={plan.conversation_move}; 目的={plan.intent}; "
            f"温度={plan.temperature}; 感情={plan.ai_emotion}; "
            f"長さ={plan.target_length}; 質問={'する' if plan.ask_follow_up else 'しない'}。\n"
            f"主={_STYLE_INSTRUCTIONS[plan.primary_style]}; "
            f"副={_STYLE_INSTRUCTIONS[plan.secondary_style]}。\n"
            f"運び={_SHAPE_INSTRUCTIONS[plan.response_shape]}。"
            f"会話行為は最低{plan.minimum_moves}個。\n"
            + (
                "継続トークの最初の区間。質問で返さず、次へ自然につながる余韻を残す。\n"
                if plan.continuation_requested else ""
            )
            + (f"継続中の会話焦点={plan.continuity_focus}。\n" if plan.continuity_focus else "")
            + (
                "過去会話の質問には取得できた具体的記録だけで答える。\n"
                if plan.recall_requested and plan.recall_grounded else
                "過去会話の質問だが根拠記録がない。検索へ逃げず、思い出せないと率直に答える。\n"
                if plan.recall_requested else ""
            )
            + ("直接回答の後なら、保留していた問いを一つだけ戻してよい: "
               f"{plan.continuity_question}\n" if plan.continuity_question else "")
            + f"選択話題={plan.selected_topic}。ユーザーへの直接回答を優先する。\n"
            + (
                "意味連想（事実ではなく、自然な話題展開の候補）="
                + " / ".join(plan.semantic_associations) + "。\n"
                if plan.semantic_associations and plan.conversation_move == "EXTEND_TOPIC"
                else ""
            )
            + (f"任意表現={features}。全部を詰め込まず自然な一つを使う。\n" if features else "")
            + ("推測は事実と分け、自然に確度を示す。\n" if speculative else "")
            + _internal_state_line(plan.internal_state)
            + "禁止=" + "、".join(plan.prohibited_patterns) + "。\n"
            + "上記はすべて内部値。発話に出さない。"
        )

    def commit(self, profile: dict[str, Any], reply: str, critique: dict[str, Any]) -> None:
        state = self.ensure_state(profile)
        plan = state.get("last_plan", {})
        if plan:
            state["recent_styles"] = (list(state["recent_styles"]) + [plan.get("primary_style", "direct")])[-8:]
            state["recent_shapes"] = (list(state["recent_shapes"]) + [plan.get("response_shape", "direct_take")])[-8:]
            state["recent_moves"] = (
                list(state.get("recent_moves", []))
                + [str(plan.get("conversation_move", "EXTEND_TOPIC"))]
            )[-10:]
            selected = list(plan.get("selected_features", []))
            names = [str(item.get("feature_type", "")) for item in selected if item.get("feature_type")]
            patterns = [str(item.get("pattern_key", "")) for item in selected if item.get("pattern_key")]
            state["recent_features"] = (list(state["recent_features"]) + names)[-12:]
            state["recent_feature_patterns"] = (list(state["recent_feature_patterns"]) + patterns)[-12:]
        if reply.strip():
            state["recent_replies"] = (list(state["recent_replies"]) + [reply.strip()[:300]])[-10:]
        state["last_critique"] = dict(critique)


def _feature_relationship(profile: dict[str, Any]) -> dict[str, Any]:
    context = profile.get("_feature_context", {})
    relationship = context.get("relationship", {}) if isinstance(context, dict) else {}
    return relationship if isinstance(relationship, dict) else {}
