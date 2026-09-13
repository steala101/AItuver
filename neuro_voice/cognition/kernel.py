"""行動候補を作り、点をつけ、選び、結果を状態へ戻す。

**LLMに最終決定をさせない。** 明らかなイベント（ゲームの危険、聞き取り失敗、
割り込み）はコードで候補を作り、制約と優先度もコードで保証する。LLMは
選ばれた行動の中身と言い方を作る側にいる（第2条）。

候補は2〜5個に絞る。全部の行動型を毎回並べても、点をつける根拠が無い
ものが増えるだけで、選択の質は上がらない。

重みは `WEIGHTS` の1箇所だけ。呼び出し側に散らさない。
"""
from __future__ import annotations

import logging
from typing import Any

from neuro_voice.cognition.state import CognitiveState
from neuro_voice.cognition.types import (
    ActionCandidate, ActionDecision, ActionOutcome, ActionType, CognitiveEvent, EventType,
)

logger = logging.getLogger(__name__)

#: 点数の重み。**ここ以外に書かない。**
#:
#: 設定から差し替えられるようにしてあるのは、実機で回してみないと妥当な値が
#: 分からないため。ハードコードして散らすと、次に触る人が全部を探す羽目になる。
WEIGHTS: dict[str, float] = {
    "goal_relevance": 1.00,
    "user_need": 1.20,
    "urgency": 1.60,
    "safety": 1.80,
    "relationship_fit": 0.60,
    "affect_fit": 0.70,
    "conversation_coherence": 0.80,
    "novelty": 0.30,
    "uncertainty_penalty": 1.30,
    "repetition_penalty": 0.50,
    "interruption_cost": 0.90,
}

#: 聞き取りの確かさがこれを下回ると、断定的な回答を許さない。
LOW_CONFIDENCE = 0.55
#: この値を超える危険は、通常会話より優先する。
DANGER_THRESHOLD = 0.60

#: **自分へ向けられていない出来事。** 答えるものが無い。
#:
#: 画面が変わった、道具が結果を返した、といった出来事に対して
#: 通常の会話候補を出すと、誰も何も聞いていないのに `ANSWER` が最有力になる。
_UNADDRESSED_EVENTS = frozenset({
    str(EventType.VISUAL_CHANGE), str(EventType.TOOL_RESULT),
    str(EventType.ACTION_COMPLETED), str(EventType.ACTION_FAILED),
    str(EventType.SESSION_STARTED), str(EventType.SESSION_ENDED),
})


class CognitiveKernel:
    """`propose → select → decide`、そして `record_outcome`。

    状態は持たない。直近の行動履歴だけは選択に要るので、呼び出し側が
    `CognitiveState.recent_actions` として渡す。
    """

    def __init__(
        self, weights: dict[str, float] | None = None, *,
        closure_policy: str = "adaptive", memory_influence: Any = None,
        state_bias: Any = None, goal_bias: dict[str, float] | None = None,
    ) -> None:
        self.weights = {**WEIGHTS, **(weights or {})}
        #: 「もういいよ」への構え。**小さな傾きであって、上書きではない。**
        self.closure_policy = str(closure_policy or "adaptive")
        #: 想起した記憶からの補正（`recall.MemoryInfluence`）。無ければ効かない。
        #:
        #: **上書きではなく加点・減点**として入れる。記憶が候補を消せると、
        #: 「前に嫌がられたから今回も黙る」が安全警告まで止めてしまう。
        self.memory_influence = memory_influence
        #: 持続している内面からの補正（`internal_state.StateBias`）。
        #:
        #: **`WARN` と `REMAIN_SILENT` には触れない**（`PROTECTED_ACTIONS`）。
        #: 嫌いな相手への警告を弱めるのと、沈黙を不機嫌の表明に使うのは、
        #: どちらもやってはいけない。触らせない側で構造的に防ぐ。
        self.state_bias = state_bias
        #: 共有している目標からの補正（`goals.goal_bias`）。
        #:
        #: **目標は命令ではない。** 打ち切りの合図が出ていれば、目標が
        #: ACTIVE でも何も足さない（そこは `goal_bias` 側で落としている）。
        self.goal_bias = dict(goal_bias or {})

    # ------------------------------------------------------------------
    # 候補づくり
    # ------------------------------------------------------------------

    def propose(
        self, event: CognitiveEvent, state: CognitiveState,
        opportunities: list[Any] | tuple[Any, ...] = (),
        now: float | None = None,
    ) -> list[ActionCandidate]:
        """明らかなイベントは規則で候補を作る。LLMには渡さない。

        `opportunities` は自分から話す機会（`initiative.InitiativeOpportunity`）。
        **専用の Action Selector は作らず、同じ列に並べて比べる。** 別系統に
        すると、通常会話と自発発話のどちらを優先するかを決める場所が
        もう1つ増えて必ず食い違う。
        """
        kind = str(event.event_type)

        if kind == EventType.GAME_DANGER and event.confidence >= DANGER_THRESHOLD:
            # 危険は会話の都合より先。ここで候補を絞るのは迷いを作らないため。
            return [
                self._score(ActionType.WARN, state, event, urgency=1.0, safety=1.0,
                            reasons=("game_danger",)),
                self._score(ActionType.ANSWER, state, event, urgency=.1,
                            reasons=("normal_conversation",)),
            ]

        if kind == EventType.AI_INTERRUPTED:
            resume_fit = .8 if state.interrupted_response else .1
            return [
                self._score(ActionType.RESUME_INTERRUPTED_RESPONSE, state, event,
                            goal_relevance=resume_fit, coherence=.9,
                            reasons=("had_unfinished_utterance",)),
                self._score(ActionType.ABANDON_INTERRUPTED_RESPONSE, state, event,
                            goal_relevance=.45, coherence=.3,
                            reasons=("user_took_the_floor",)),
                self._score(ActionType.ANSWER, state, event, goal_relevance=.6,
                            reasons=("respond_to_the_interruption",)),
            ]

        if kind == EventType.ASR_UNCERTAIN or event.confidence < LOW_CONFIDENCE:
            # 聞き取れていないのに答えると、作り話になる。
            return [
                self._score(ActionType.ASK_CLARIFICATION, state, event,
                            goal_relevance=.7, reasons=("low_input_confidence",)),
                self._score(ActionType.REMAIN_SILENT, state, event,
                            goal_relevance=.5, reasons=("not_addressed_clearly",)),
                self._score(ActionType.ANSWER, state, event, goal_relevance=.6,
                            reasons=("guess_the_meaning",),
                            blocking=("input_confidence_too_low",)),
            ]

        if kind in _UNADDRESSED_EVENTS:
            # **自分に向けられた発話ではない。答えるものが無い。**
            # ここで通常の会話候補を出すと、画面が変わっただけで
            # `ANSWER` が最有力になる（誰も何も聞いていないのに）。
            return self._with_opportunities([
                self._score(ActionType.REMAIN_SILENT, state, event, goal_relevance=.6,
                            reasons=("nothing_was_addressed_to_me",)),
            ], opportunities, state, event, now)

        if kind == EventType.SILENCE_TIMEOUT:
            return self._with_opportunities([
                self._score(ActionType.REMAIN_SILENT, state, event, goal_relevance=.6,
                            reasons=("silence_is_allowed",)),
                self._score(ActionType.CONTINUE_PREVIOUS_TOPIC, state, event,
                            goal_relevance=.55, reasons=("thread_left_open",)),
            ], opportunities, state, event, now)

        return self._with_opportunities(
            self._conversation_candidates(event, state), opportunities, state, event, now)

    def _with_opportunities(
        self, candidates: list[ActionCandidate], opportunities, state, event,
        now: float | None = None,
    ) -> list[ActionCandidate]:
        """自分から話す機会を、同じ列へ足す。

        **沈黙を必ず入れる。** 機会があることと話すべきことは別で、
        ここを外すと「機会を見つけた＝話す」になる。

        `now` を受け取るのは、期限の判定に**呼び出し側と同じ時計**を
        使うため。ここだけ `time.monotonic()` を直接読んでいたせいで、
        別の時計で作られた機会が全部「期限切れ」として静かに消えていた。
        """
        if not opportunities:
            return candidates
        merged = list(candidates)
        for opportunity in opportunities:
            try:
                candidate = opportunity.to_candidate()
            except Exception:  # noqa: BLE001 — 壊れた候補で会話を止めない
                logger.debug("機会を候補へ変換できない", exc_info=True)
                continue
            if candidate.expired(now=now):
                continue
            merged.append(self._rescore_opportunity(candidate, state, event))
        if not any(item.action_type is ActionType.REMAIN_SILENT for item in merged):
            merged.append(self._score(
                ActionType.REMAIN_SILENT, state, event, goal_relevance=.5,
                reasons=("silence_is_always_an_option",)))
        return merged[:8]

    def _rescore_opportunity(
        self, candidate: ActionCandidate, state: CognitiveState, event: CognitiveEvent,
    ) -> ActionCandidate:
        """機会の点数へ、記憶と内面の補正を同じように掛ける。

        自発発話だけ補正を免れると、「過去に嫌がられた話題」を自分からは
        平気で持ち出すことになる。
        """
        import dataclasses

        components = dict(candidate.score_components)
        total = float(candidate.total_score)
        reasons = candidate.reasons
        if self.memory_influence is not None:
            adjustment = self.memory_influence.for_action(candidate.action_type)
            if adjustment:
                components["memory_adjustment"] = adjustment
                total += adjustment
                reasons = reasons + tuple(
                    f"memory:{item}" for item in
                    self.memory_influence.sources(candidate.action_type)[:2])
        if self.state_bias is not None:
            adjustment = self.state_bias.for_action(candidate.action_type)
            if adjustment:
                components["state_adjustment"] = adjustment
                total += adjustment
        goal_adjustment = self.goal_bias.get(str(candidate.action_type), .0)
        if goal_adjustment:
            components["goal_adjustment"] = goal_adjustment
            total += goal_adjustment
        # 打ち切りの合図が出ているなら、自分から広げる候補は下げる。
        if state.end_signal > .5:
            components["user_end_signal_penalty"] = -state.end_signal
            total -= state.end_signal
        return dataclasses.replace(
            candidate, score_components=components,
            total_score=round(total, 4), reasons=reasons)

    def _conversation_candidates(
        self, event: CognitiveEvent, state: CognitiveState,
    ) -> list[ActionCandidate]:
        """普通の発話。感情・関係性・会話の継続性が**点数として**効く。"""
        ending = state.end_signal > .5
        candidates = [
            self._score(
                ActionType.ANSWER, state, event,
                goal_relevance=.8 - (.5 if ending else .0),
                reasons=("addressed",) if not ending else ("addressed", "user_is_ending"),
            ),
        ]
        if state.user_distress > 0.35 or state.user_frustration > 0.5:
            candidates.append(self._score(
                ActionType.ACKNOWLEDGE_EMOTION, state, event,
                goal_relevance=.75, user_need=max(state.user_distress, state.user_frustration),
                reasons=("user_distress",),
            ))
        if state.ambiguity > .5 or state.curiosity > 0.55:
            candidates.append(self._score(
                ActionType.ASK_CLARIFICATION, state, event,
                goal_relevance=.45 + (.35 if state.ambiguity > .5 else .0),
                reasons=("ambiguous_reference",) if state.ambiguity > .5 else ("curiosity",),
            ))
        from neuro_voice.cognition.recall import obligation_retrieval_gate
        obligation_gate = obligation_retrieval_gate(event.content, state)
        # End-signal handling keeps a CONTINUE candidate for the existing
        # score/cancellation comparison, but recall.py still forbids lookup
        # there.  Ordinary unrelated turns have neither the candidate nor a
        # memory retrieval.
        if obligation_gate.triggered or (state.unresolved_obligations and ending):
            candidates.append(self._score(
                ActionType.CONTINUE_PREVIOUS_TOPIC, state, event, goal_relevance=.6,
                coherence=.85, reasons=(obligation_gate.reason,),
            ))
        if ending or state.user_engagement < .25:
            # 打ち切りの合図に、話を広げて食い下がらない。無言と短い了承の
            # どちらが自然かは聞いてみないと分からないので、**両方候補に出す**。
            # 設定はどちらかへ小さく傾けるだけで、固定はしない。
            candidates.append(self._score(
                ActionType.REMAIN_SILENT, state, event, goal_relevance=.75,
                reasons=("user_is_ending",),
            ))
            candidates.append(self._score(
                ActionType.BRIEF_ACKNOWLEDGE, state, event, goal_relevance=.72,
                reasons=("user_is_ending", "short_acknowledgement"),
            ))
        if (
            state.comfort > 0.6 and state.irritation < 0.3
            and state.user_distress < 0.2 and state.user_frustration < 0.3 and not ending
        ):
            candidates.append(self._score(
                ActionType.MAKE_LIGHT_JOKE, state, event, goal_relevance=.35,
                novelty=.7, reasons=("relaxed_conversation",),
            ))
        return candidates[:5]

    @staticmethod
    def _supporting(primary: ActionType, state: CognitiveState) -> ActionType | None:
        """主要行動に添える1つだけ。**任意個は連結しない。**

        つらいと言っている人に答えを返す時、共感を先に置く——この組だけで
        実用上は足りる。
        """
        if primary is ActionType.ANSWER and state.user_distress > .5:
            return ActionType.ACKNOWLEDGE_EMOTION
        if primary is ActionType.ACKNOWLEDGE_EMOTION and state.ambiguity < .3:
            return ActionType.ANSWER
        return None

    # ------------------------------------------------------------------
    # 点づけ
    # ------------------------------------------------------------------

    def _score(
        self, action: ActionType, state: CognitiveState, event: CognitiveEvent, *,
        goal_relevance: float = .5, user_need: float = .0, urgency: float = .0,
        safety: float = .0, coherence: float = .5, novelty: float = .3,
        reasons: tuple[str, ...] = (), blocking: tuple[str, ...] = (),
    ) -> ActionCandidate:
        components = {
            "goal_relevance": goal_relevance,
            "user_need": max(user_need, state.user_distress if action is ActionType.ACKNOWLEDGE_EMOTION else 0.0),
            "urgency": urgency,
            "safety": safety,
            "relationship_fit": self._relationship_fit(action, state),
            "affect_fit": self._affect_fit(action, state),
            "conversation_coherence": coherence + self._continuity_fit(action, state),
            "novelty": novelty,
            "uncertainty_penalty": -self._uncertainty_penalty(action, state),
            "repetition_penalty": -self._repetition_penalty(action, state),
            "interruption_cost": -self._interruption_cost(action, state),
        }
        total = sum(self.weights.get(key, 1.0) * value for key, value in components.items())
        # 終了シグナルの時だけ効く小さな傾き。安全警告や明示的な質問を
        # 押しのけない大きさに留める。
        if state.end_signal > .5:
            from neuro_voice.cognition.rollout import closure_bias

            bias = closure_bias(self.closure_policy, action, state)
            if bias:
                components["closure_policy_bias"] = bias
                total += bias
            if action in {ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE}:
                # なぜそちらへ寄せたのかを候補へ残す。
                # 「黙った理由」を後から読めないと、調整のしようがない。
                from neuro_voice.cognition.rollout import adaptive_closure_bias

                _signed, why = adaptive_closure_bias(state)
                reasons = reasons + why
        # 過去の経験からの補正。**どの記憶が効いたかを候補へ残す**——
        # 点だけ動いて理由が読めないと、調整のしようがない（第20条）。
        if self.memory_influence is not None:
            adjustment = self.memory_influence.for_action(action)
            if adjustment:
                components["memory_adjustment"] = adjustment
                total += adjustment
                sources = self.memory_influence.sources(action)
                if sources:
                    reasons = reasons + tuple(f"memory:{item}" for item in sources[:3])
                else:
                    reasons = reasons + ("memory:reflection",)
        # 持続している内面からの補正。**記憶と同じく上書きではない。**
        # `WARN` / `REMAIN_SILENT` は `action_bias` 側で除外済み。
        if self.state_bias is not None:
            adjustment = self.state_bias.for_action(action)
            if adjustment:
                components["state_adjustment"] = adjustment
                total += adjustment
                reasons = reasons + tuple(
                    f"state:{name}" for name in self.state_bias.dimensions_used[:2]
                )
        # 共有している目標からの補正。**これも小さな加点・減点だけ。**
        goal_adjustment = self.goal_bias.get(str(action), .0)
        if goal_adjustment:
            components["goal_adjustment"] = goal_adjustment
            total += goal_adjustment
            reasons = reasons + ("goal",)
        blocked = tuple(blocking)
        if action is ActionType.ANSWER and state.input_confidence < LOW_CONFIDENCE:
            blocked = blocked + ("input_confidence_too_low",)
        return ActionCandidate(
            action_type=action, reasons=reasons, score_components=components,
            total_score=round(total, 4), confidence=state.input_confidence,
            blocking_conditions=blocked,
        )

    @staticmethod
    def _relationship_fit(action: ActionType, state: CognitiveState) -> float:
        """信頼が低いのに断定や踏み込みをしない。"""
        if action is ActionType.CHALLENGE_ASSUMPTION:
            return state.trust - .5
        if action is ActionType.MAKE_LIGHT_JOKE:
            return state.comfort - .5
        if action is ActionType.ASK_CLARIFICATION:
            return .1
        return .0

    @staticmethod
    def _affect_fit(action: ActionType, state: CognitiveState) -> float:
        """苛立ちが高い時に冗談や過剰な迎合を増やさない。"""
        if action is ActionType.MAKE_LIGHT_JOKE:
            # distress_penalty と irritation_penalty の両方を掛ける。
            return -(state.irritation + state.user_distress + state.user_frustration)
        if action is ActionType.ACKNOWLEDGE_EMOTION:
            return max(state.user_distress, state.user_frustration)
        if action is ActionType.ANSWER and state.user_distress > .5:
            # つらいと言っている人に、いきなり解決策だけを返さない。
            return -.4
        return .0

    @staticmethod
    def _continuity_fit(action: ActionType, state: CognitiveState) -> float:
        """会話の続き方。**打ち切りの合図を無視して話を広げない。**"""
        if action is ActionType.CONTINUE_PREVIOUS_TOPIC:
            value = .3 if state.unresolved_obligations else -.2
            return value - state.end_signal            # user_end_signal_penalty
        if action in {ActionType.RESUME_INTERRUPTED_RESPONSE, ActionType.MAKE_LIGHT_JOKE}:
            return -state.end_signal
        if action in {ActionType.REMAIN_SILENT, ActionType.BRIEF_ACKNOWLEDGE}:
            # 打ち切りの合図に対しては、黙るのも短く受けるのも同じくらい妥当。
            # **ほぼ拮抗させておいて、方針の小さな加点で決まる**ようにする。
            # 片方を構造的に強くすると、設定を切り替えても結果が動かない。
            return state.end_signal * .5
        return .0

    @staticmethod
    def _uncertainty_penalty(action: ActionType, state: CognitiveState) -> float:
        if action in {ActionType.ANSWER, ActionType.CHALLENGE_ASSUMPTION}:
            return max(0.0, 1.0 - state.input_confidence)
        return .0

    @staticmethod
    def _repetition_penalty(action: ActionType, state: CognitiveState) -> float:
        recent = state.recent_actions[-3:]
        if not recent:
            return .0
        repeats = sum(1 for item in recent if item == str(action))
        # 質問の連投は特に嫌われる。
        if action is ActionType.ASK_CLARIFICATION and repeats:
            return min(1.0, .5 + .3 * repeats)
        return min(1.0, .25 * repeats)

    @staticmethod
    def _interruption_cost(action: ActionType, state: CognitiveState) -> float:
        if action is ActionType.RESUME_INTERRUPTED_RESPONSE and not state.interrupted_response:
            return 1.0
        return .0

    # ------------------------------------------------------------------
    # 選択
    # ------------------------------------------------------------------

    def decide(
        self, event: CognitiveEvent, state: CognitiveState,
        candidates: list[ActionCandidate] | None = None,
        opportunities: list[Any] | tuple[Any, ...] = (),
        now: float | None = None,
    ) -> ActionDecision:
        options = list(
            candidates if candidates is not None
            else self.propose(event, state, opportunities, now))
        allowed = [item for item in options if not item.blocked] or options
        # A high-confidence ConversationKernel response plan is an input
        # contract, not a soft preference. Memory and closure heuristics may
        # shape a response, but cannot turn an addressed request into silence.
        if state.response_required:
            # An explicit Local PROD read-only Tool request is itself the
            # required response.  Do not lose that one bounded candidate to
            # the ordinary spoken-ANSWER filter before the Tool path runs.
            requested_tools = [
                item for item in allowed
                if item.action_type is ActionType.EXECUTE_TOOL
                and item.source_type == "cognitive_tool_request"
            ]
            answers = [
                item for item in allowed
                if item.action_type is ActionType.ANSWER
            ]
            if requested_tools:
                allowed = requested_tools
            elif answers:
                allowed = answers
        initially_selected = max(allowed, key=lambda item: item.total_score)
        from neuro_voice.cognition.action_constraints import ActionConstraintValidator, ActionEligibility
        eligibility = ActionEligibility.from_state(state)
        best, constraint = ActionConstraintValidator.validate(
            initially_selected, options, eligibility)
        rejected = tuple(
            sorted((item for item in options if item is not best),
                   key=lambda item: -item.total_score)
        )
        reason = ",".join(best.reasons) or "highest_score"
        if best.blocked:
            reason = f"{reason}(all_blocked)"
        return ActionDecision(
            selected_action=best.action_type,
            supporting_action=self._supporting(best.action_type, state),
            rejected_actions=rejected,
            decision_reason=reason,
            state_snapshot_id=state.snapshot_id,
            event_ids=(event.event_id,),
            confidence=best.confidence,
            # **選ばれた候補の出どころを決定まで持ち上げる。**
            # ここで落とすと、Speech Gate に判定材料が無くなる。
            source_type=best.source_type,
            source_event_ids=best.source_event_ids,
            topic_id=best.topic_id,
            participant_ids=best.participant_ids,
            expires_at=best.expires_at,
            parameters={"action_constraint": constraint.snapshot()},
        )

    # ------------------------------------------------------------------
    # 結果を戻す
    # ------------------------------------------------------------------

    @staticmethod
    def apply_outcome(
        decision: ActionDecision, outcome: ActionOutcome, state: CognitiveState,
    ) -> dict[str, Any]:
        """結果から、状態へ返すべき差分を作る。**書き込みは呼び出し側**。

        ここで直接書かないのは、状態の持ち主が各モジュールのままだから
        （第5条）。何を更新すべきかだけを決めて返す。
        """
        updates: dict[str, Any] = {
            "recent_action": str(decision.selected_action),
            "obligations": list(state.unresolved_obligations),
        }
        if outcome.interrupted or outcome.status == "interrupted":
            # 言い終えていないなら、義務はまだ残っている。
            updates["obligations"] = [
                *state.unresolved_obligations, "finish_interrupted_response",
            ]
            updates["interrupted_response"] = outcome.observable_effects.get("spoken", "")
        elif outcome.status == "completed":
            updates["obligations"] = [
                item for item in state.unresolved_obligations
                if item != "finish_interrupted_response"
            ]
            updates["interrupted_response"] = ""
        # 関係性と感情は**一度の出来事で大きく動かさない**。
        # 上限・基準値・戻り方は `trace.STATE_LIMITS` の1箇所だけで決める。
        from neuro_voice.cognition.trace import limited_change

        raw: dict[str, float] = {}
        if outcome.status == "failed":
            raw["irritation"] = .05
        elif outcome.status == "completed":
            if decision.selected_action is ActionType.ACKNOWLEDGE_EMOTION:
                raw["comfort"] = .02
            if outcome.interrupted:
                raw["trust"] = -.01
        changes = [
            limited_change(
                key,
                {"irritation": state.irritation, "comfort": state.comfort,
                 "trust": state.trust}.get(key, .0),
                delta, reason=f"{decision.selected_action}:{outcome.status}",
                source_event_id=decision.event_ids[0] if decision.event_ids else "",
            )
            for key, delta in raw.items()
        ]
        if changes:
            updates["state_changes"] = [item.snapshot() for item in changes]
            updates["relationship_delta"] = {
                item.key: item.delta for item in changes if item.key in {"trust", "comfort"}
            }
            updates["affect_delta"] = {
                item.key: item.delta for item in changes if item.key == "irritation"
            }
        return updates
