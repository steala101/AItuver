"""Low-latency optional conversation features integrated by the planner."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable, Protocol

from neuro_voice.dialogue.feature_models import (
    CandidateEvaluation, ConversationFeatureProposal, ValueProfile,
)


_SERIOUS = re.compile(
    r"(?:死|自殺|病院|診断|薬|法律|訴訟|借金|投資|株価|事故|危険|緊急|謝って|謝罪|怒って|ふざけないで)"
)
_LOW_MOOD = re.compile(r"(?:つら|しんど|悲し|落ち込|疲れ切|不安|怖い|最悪|泣)" )
_IMAGINE = re.compile(r"(?:もし|想像|仮説|可能性|未来|こうなったら|アイデア|発展させ|別の見方)")
_FANTASY = re.compile(r"(?:妄想|架空|世界観|能力|宇宙|異世界|ゲーム世界|キャラ|設定を作)")
_STORY = re.compile(r"(?:物語|ストーリー|たとえ|例え|シミュレーション|場面|実況|昔話)")
_FRESH = re.compile(r"(?:最新|今日の|現在の|ニュース|価格|株価|規約|法律|バージョン|発売|現行)")
_FEEDBACK_NEGATIVE = re.compile(r"(?:つまらない|微妙|寒い|その冗談|ふざけないで|質問が多い|機械的|同じこと)")


class ConversationFeatureEngine(Protocol):
    feature_type: str

    def propose(
        self, *, text: str, profile: dict[str, Any], intent: str, momentum: float,
        values: ValueProfile,
    ) -> ConversationFeatureProposal | None: ...


def _context(profile: dict[str, Any]) -> dict[str, Any]:
    value = profile.get("_feature_context", {})
    return value if isinstance(value, dict) else {}


def _relationship(profile: dict[str, Any]) -> dict[str, Any]:
    value = _context(profile).get("relationship", {})
    return value if isinstance(value, dict) else {}


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class HumorEngine:
    feature_type = "humor_engine"
    _PATTERNS = (
        ("light_tsukkomi", "発話中の具体的な一点への短いツッコミ"),
        ("unexpected_comparison", "状況に合う意外だが分かりやすい比較"),
        ("cute_small_joke", "相手を傷つけない、かわいらしい小ボケ"),
        ("dry_observation", "説明を邪魔しない乾いた一言"),
        ("gentle_exaggeration", "事実と誤認しない範囲の軽い誇張"),
        ("mini_drama", "小さな出来事を一瞬だけ大事件のように扱い、すぐ本題へ戻る"),
        ("playful_challenge", "発話中の具体点を材料に、相手と軽く張り合う"),
        ("mock_grandiosity", "自分を少し大げさに持ち上げ、すぐ自分で落として本題へ戻る"),
        ("inside_reference", "確かな過去会話がある場合だけ使う内輪ネタ"),
    )

    def propose(self, *, text, profile, intent, momentum, values):
        if intent in {"support", "correction"} or _SERIOUS.search(text) or _LOW_MOOD.search(text):
            return None
        recent = list(profile.get("conversation_generation", {}).get("recent_feature_patterns", []))
        traits = _context(profile).get("persona_traits", {})
        traits = traits if isinstance(traits, dict) else {}
        playfulness = float(traits.get("playfulness", .5))
        cheekiness = float(traits.get("cheekiness", .5))
        digest = int(hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
        preferred_names: tuple[str, ...] = ()
        if cheekiness >= .55:
            preferred_names = ("playful_challenge", "light_tsukkomi", "mock_grandiosity")
        elif playfulness >= .65:
            preferred_names = ("mini_drama", "cute_small_joke", "unexpected_comparison")
        preferred = [item for item in self._PATTERNS if item[0] in preferred_names]
        others = [item for item in self._PATTERNS if item[0] not in preferred_names]
        preferred = preferred[digest % len(preferred):] + preferred[:digest % len(preferred)] if preferred else []
        others = others[digest % len(others):] + others[:digest % len(others)] if others else []
        ordered = preferred + others
        pattern, expression = next((item for item in ordered if item[0] not in recent[-4:]), ordered[0])
        rel = _relationship(profile)
        comfort = float(rel.get("comfort", .5))
        score = (
            .36 + .25 * momentum + .18 * values.humor + .12 * comfort
            + .08 * (playfulness - .5) + .06 * (cheekiness - .5)
        )
        if re.search(r"(?:笑|面白|ウケ|やば|まじ|！|!)", text):
            score += .12
        return ConversationFeatureProposal(
            self.feature_type, _bounded(score), .76, "深刻度が低く会話テンポと相性がよい",
            f"{pattern}を一度だけ使う", expression, "playful", .08,
            "返答の意外性と親しみを増やす", pattern_key=pattern,
        )


class CasualConversationEngine:
    feature_type = "casual_conversation_engine"

    def propose(self, *, text, profile, intent, momentum, values):
        if intent == "correction":
            return None
        ctx = _context(profile)
        memories = ctx.get("memory_snippets", [])
        unresolved = ctx.get("active_curiosities", [])
        if memories and momentum >= .42:
            summary = "高関連の過去話題を、今の話との接点だけ示して再利用する"
            source, refs = "verified_memory", (str(memories[0])[:120],)
            score = .67
        elif unresolved and momentum >= .48:
            summary = "途中だった関心を押しつけず今の話へ接続する"
            source, refs = "verified_memory", (str(unresolved[0].get("topic", ""))[:80],)
            score = .63
        else:
            summary = "発話中の具体点から近い横方向へ一歩だけ話題を広げる"
            source, refs = "inference", ()
            score = .46 + .25 * momentum
        return ConversationFeatureProposal(
            self.feature_type, _bounded(score), .78, "会話を質問だけで維持せず自然に展開できる",
            summary, "AI側の感想か予想を先に置き、必要な時だけ質問を一つ添える",
            "warm", .05, "話題継続と自然な広がり", refs, 2, source_type=source,
        )


class ImaginationEngine:
    feature_type = "imagination_engine"

    def propose(self, *, text, profile, intent, momentum, values):
        explicit = bool(_IMAGINE.search(text))
        if intent == "support" or _SERIOUS.search(text):
            return None
        if not explicit and intent not in {"preference", "statement", "question"}:
            return None
        if re.search(r"(?:逆の立場|立場を逆|役割を逆|入れ替え)", text):
            mode = "role_reversal"
            expression = "立場を入れ替えた場合を一つだけ考える"
        elif re.search(r"(?:制約|縛り|条件付き|限られ)", text):
            mode = "constraint_creativity"
            expression = "与えられた制約を守る案だけを広げる"
        elif re.search(r"(?:シミュレーション|場面|状況なら)", text):
            mode = "scenario_simulation"
            expression = "短い状況シミュレーションとして展開する"
        elif re.search(r"(?:別の見方|別案|別の説明|違う角度)", text):
            mode = "alternate_explanation"
            expression = "同じ事柄を別角度から一案だけ説明する"
        else:
            mode = "what_if"
            expression = "『もし〜なら』の現実的な仮説を一つ、別案を一つまで提示する"
        score = (.78 if explicit else .37) + .16 * values.creativity + .08 * momentum
        return ConversationFeatureProposal(
            self.feature_type, _bounded(score), .66 if explicit else .52,
            "ユーザーが可能性を広げる話をしている" if explicit else "別視点が会話を一段進められる",
            f"{mode}モードで可能性を広げる",
            expression + "。『まだ仮説だけど』等で不確実性を自然に示す", "curious", .10,
            "共同思考と新しい視点", source_type="inference", is_speculative=True,
            requires_verification=bool(_FRESH.search(text)), pattern_key=mode,
        )


class StoryEngine:
    feature_type = "story_engine"

    def propose(self, *, text, profile, intent, momentum, values):
        explicit = bool(_STORY.search(text))
        explanatory = intent == "question" and bool(re.search(r"(?:仕組み|概念|分かりやすく|例えば)", text))
        if not explicit and not explanatory:
            return None
        if re.search(r"(?:続き|前回の物語|続きを)", text):
            mode = "story_continuation"
        elif re.search(r"(?:世界観|設定|舞台)", text):
            mode = "worldbuilding"
        elif re.search(r"(?:なりき|ロールプレイ|役として)", text):
            mode = "roleplay"
        elif re.search(r"(?:架空|作品|キャラ).*(?:話|考察|議論)", text):
            mode = "fictional_discussion"
        else:
            mode = "storytelling"
        return ConversationFeatureProposal(
            self.feature_type, .74 if explicit else .55, .72,
            "短い場面化で説明または共同創作が分かりやすくなる",
            f"{mode}として、通常会話では1〜3文のmicro_storyだけを使う",
            "事実説明と創作部分の境界が自然に分かる短い場面として語る",
            "imaginative", .08, "印象に残る説明", source_type="simulation",
            is_speculative=True, pattern_key=mode,
        )


class PlayfulFantasyEngine:
    feature_type = "playful_fantasy_engine"

    def propose(self, *, text, profile, intent, momentum, values):
        if not _FANTASY.search(text) or _SERIOUS.search(text):
            return None
        return ConversationFeatureProposal(
            self.feature_type, _bounded(.74 + .14 * momentum), .82,
            "ユーザーが明示的に自由な創作を求めている",
            "現実と混同しない遊びとして設定を一段だけ広げる",
            "『もしその世界なら』のように創作だと自然に分かる表現にする",
            "playful", .06, "共同創作の楽しさ", source_type="fiction",
            is_speculative=True, pattern_key="playful_worldbuilding",
        )


class WorldKnowledgeEngine:
    feature_type = "world_knowledge_engine"

    def propose(self, *, text, profile, intent, momentum, values):
        if intent != "question" or re.search(r"(?:どう思う|気持ち|好き|覚えてる|さっき|前の話)", text):
            return None
        fresh = bool(_FRESH.search(text))
        return ConversationFeatureProposal(
            self.feature_type, .84 if fresh else .56, .72 if not fresh else .48,
            "具体的知識が直接回答の質を上げる",
            "内部知識または取得済み根拠から要点を一つ具体化する",
            "検索結果の羅列ではなく、ユーザーの目的へ結び付けて解釈する",
            "serious" if fresh else "neutral", .04, "具体性と有用性",
            source_type="realtime" if fresh else "llm_knowledge",
            requires_verification=fresh,
        )


class ValueOpinionEngine:
    feature_type = "value_system"

    def propose(self, *, text, profile, intent, momentum, values):
        if intent not in {"preference", "statement", "question"}:
            return None
        strongest = sorted(values.snapshot().items(), key=lambda item: item[1], reverse=True)[:3]
        labels = {"honesty": "誠実さ", "curiosity": "好奇心", "compassion": "思いやり",
                  "autonomy": "自律性", "safety": "安全性", "creativity": "創造性",
                  "practicality": "実用性", "fairness": "公平さ"}
        axes = "・".join(labels.get(name, name) for name, _ in strongest)
        return ConversationFeatureProposal(
            self.feature_type, .49 + .14 * momentum, .84,
            "迎合せず一貫したAI側の見方を添えられる",
            f"{axes}を重視した見解を一つ示す",
            "ユーザーの考えを正確に受け止めた上で、理由付きで賛成または異論を述べる",
            "thoughtful", .03, "人格の一貫性", source_type="inference",
        )


class RelationshipExpressionEngine:
    feature_type = "relationship_evolution"

    def propose(self, *, text, profile, intent, momentum, values):
        rel = _relationship(profile)
        interactions = int(rel.get("meaningful_interaction_count", rel.get("interaction_count", 0)) or 0)
        comfort = float(rel.get("comfort", .5))
        trust = float(rel.get("trust", .5))
        if interactions < 4 and not _context(profile).get("memory_snippets"):
            return None
        score = .38 + .24 * comfort + .18 * trust
        return ConversationFeatureProposal(
            self.feature_type, _bounded(score), .88,
            "継続した相互作用または確かな共有文脈がある",
            "距離感に合う省略、率直さ、または共有文脈を軽く表現する",
            "関係レベルを説明せず、馴れ馴れしさや依存表現を避ける",
            "warm", .05, "積み重ねを感じる自然な距離感", source_type="verified_memory",
        )


@dataclass(slots=True)
class FeatureSelection:
    proposed: tuple[ConversationFeatureProposal, ...]
    selected: tuple[ConversationFeatureProposal, ...]
    rejected: tuple[CandidateEvaluation, ...]
    evaluations: tuple[CandidateEvaluation, ...]


class ConversationFeatureSuite:
    """Generate, score and bound optional features without an extra LLM call."""

    ENGINES = (
        HumorEngine, CasualConversationEngine, ImaginationEngine, StoryEngine,
        PlayfulFantasyEngine, WorldKnowledgeEngine, ValueOpinionEngine,
        RelationshipExpressionEngine,
    )

    def __init__(self, cfg):
        self._cfg = cfg
        self._engines = [engine() for engine in self.ENGINES]

    def _enabled(self, feature_type: str) -> bool:
        return bool(self._cfg.get(f"features.{feature_type}", True))

    def select(
        self, *, text: str, profile: dict[str, Any], intent: str, momentum: float,
        learned_weights: dict[str, float] | None = None, values: ValueProfile | None = None,
    ) -> FeatureSelection:
        mode = str(self._cfg.get("conversation_features.mode", "normal"))
        multiplier = float(self._cfg.get(
            f"conversation_features.modes.{mode}.feature_activation_multiplier", 1.0,
        ))
        force = str(self._cfg.get("conversation_features.debug.force_feature", "") or "").strip()
        weights = learned_weights or {}
        values = values or ValueProfile()
        recent = list(profile.get("conversation_generation", {}).get("recent_features", []))
        proposals: list[ConversationFeatureProposal] = []
        evaluations: list[CandidateEvaluation] = []
        for engine in self._engines:
            if not self._enabled(engine.feature_type) and force != engine.feature_type:
                continue
            proposal = engine.propose(
                text=text, profile=profile, intent=intent, momentum=momentum, values=values,
            )
            if proposal is None:
                continue
            repeat_penalty = .24 if proposal.feature_type in recent[-2:] else (.10 if proposal.feature_type in recent[-5:] else 0.0)
            learned = max(.25, min(1.75, float(weights.get(proposal.feature_type, 1.0))))
            adjusted = proposal.activation_score * multiplier * learned - repeat_penalty
            if force == proposal.feature_type:
                adjusted = 1.0
            adjusted = _bounded(adjusted)
            risk_limit = .25 if intent not in {"support", "correction"} else .08
            reasons = []
            if proposal.risk_level > risk_limit:
                reasons.append("risk_gate")
            threshold = .50 if mode != "conservative" else .62
            if adjusted < threshold and force != proposal.feature_type:
                reasons.append("activation_below_threshold")
            scores = {
                "relevance": round(proposal.activation_score, 3),
                "naturalness": round(1.0 - repeat_penalty, 3),
                "novelty": round(1.0 - repeat_penalty, 3),
                "emotional_fit": round(1.0 - proposal.risk_level, 3),
                "personality_consistency": round(min(1.0, learned / 1.2), 3),
                "factual_confidence": round(proposal.confidence, 3),
                "latency_cost": round(proposal.latency_cost, 3),
            }
            accepted = not reasons
            evaluations.append(CandidateEvaluation(
                proposal.feature_type, proposal.feature_type, scores,
                round(adjusted, 3), accepted, tuple(reasons),
            ))
            proposals.append(proposal)
        ranked = sorted(
            zip(proposals, evaluations), key=lambda item: item[1].total, reverse=True,
        )
        max_features = 3 if momentum >= .74 else 2
        selected = [proposal for proposal, evaluation in ranked if evaluation.accepted][:max_features]
        selected_names = {item.feature_type for item in selected}
        rejected = tuple(evaluation for _, evaluation in ranked if evaluation.candidate_id not in selected_names)
        return FeatureSelection(tuple(proposals), tuple(selected), rejected, tuple(evaluations))
