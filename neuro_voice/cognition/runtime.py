"""注意と発話機会を、実際の会話の中で持ち回る。

第一弾・第二弾で作った部品（`attention.py` / `initiative.py`）は、どれも
状態を持たない純粋な形にしてある。テストしやすい代わりに、**誰かが
持ち回らないと動かない。** ここがその「誰か」。

`pipeline` が直接これらを組み立てないのは、Local と Discord で同じ順序を
2回書くことになるため。順序が2箇所にあると、片方だけ直されて食い違う。

**発話は決めない。** ここが返すのは機会の一覧までで、選ぶのは Action
Selector、出してよいかを決めるのは Speech Gate。
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.attention import (
    AttentionEvent, ContinuousAttentionState, EventIntake, FocusDecision,
    FocusManager, apply_event, note_initiative,
)
from neuro_voice.cognition.initiative import (
    InitiativeBudget, InitiativeOpportunity, OpportunityType, SpeakingConditions,
    WarnThrottle, apply_internal_state, merge_opportunities,
    opportunities_from_events, opportunities_from_memories, revalidate,
    silent_opportunity, social_cost_in_group, suppression_reason, timing_score,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InitiativeResult:
    """1回の評価の結果。**空でも正常**——話す理由が無いのが普通。"""

    opportunities: tuple[InitiativeOpportunity, ...] = ()
    focus: FocusDecision | None = None
    #: 落とした機会と、その理由。**なぜ黙ったかを読めるようにする。**
    suppressed: tuple[tuple[str, str], ...] = ()
    latency_ms: dict[str, float] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.opportunities

    def snapshot(self) -> dict[str, Any]:
        return {
            "opportunities": [item.snapshot() for item in self.opportunities[:4]],
            "focus": self.focus.snapshot() if self.focus is not None else None,
            "suppressed": [list(item) for item in self.suppressed[:6]],
            "latency_ms": {k: round(v, 2) for k, v in self.latency_ms.items()},
        }


class InitiativeRuntime:
    """注意状態・間引き・注目対象・予算を1つに束ねる。

    機能フラグで丸ごと止められる。止まっている間は `evaluate()` が
    常に空を返すので、呼び出し側は分岐を書かなくてよい。
    """

    def __init__(self, cfg=None, *, source: str = "local") -> None:
        get = getattr(cfg, "get", None) or (lambda _key, default=None: default)
        self.enabled = bool(get("initiative.enabled", False))
        self.speech_enabled = bool(get("initiative.speech_enabled", False))
        # **段階を細かく分ける。** どこまで通すかを1つずつ上げられるように。
        self.attention_enabled = bool(get("initiative.attention_enabled", True))
        self.game_commentary_enabled = bool(
            get("initiative.game_commentary_enabled", False))
        self.memory_initiative_enabled = bool(
            get("initiative.memory_initiative_enabled", False))
        self.idle_initiative_enabled = bool(
            get("initiative.idle_initiative_enabled", False))
        # **既定 true。** 再検証を止める理由は普通は無い——止めると
        # 「相手が話し始めたのに喋り出す」が復活する。
        self.pre_speech_revalidation_enabled = bool(
            get("initiative.pre_speech_revalidation_enabled", True))
        self.source = str(source)
        self.state = ContinuousAttentionState()
        self.intake = EventIntake(
            min_salience=float(get("initiative.min_salience", .15)),
            dedup_window=float(get("initiative.dedup_window_s", 4.0)),
        )
        self.focus_manager = FocusManager(
            switch_margin=float(get("initiative.switch_margin", .05)),
            min_dwell=float(get("initiative.min_dwell_s", 1.5)),
        )
        self.budget = InitiativeBudget(
            window_seconds=float(get("initiative.budget.window_s", 120.0)),
            max_utterances=int(get("initiative.budget.max_utterances", 4)),
            max_same_topic_utterances=int(get("initiative.budget.max_same_topic", 2)),
            cooldown_seconds=float(get("initiative.budget.cooldown_s", 8.0)),
            max_cost=float(get("initiative.budget.max_cost", 3.0)),
        )
        self.warn_throttle = WarnThrottle(
            repeat_seconds=float(get("initiative.warn_repeat_s", 6.0)))
        #: 既に話した内容の鍵と、扱ったイベント。二度言わないため。
        self._spoken_keys: list[str] = []
        self._handled_events: list[str] = []
        #: 観測できる数。**「配線したが一度も動いていない」を見分ける。**
        self.counters: dict[str, int] = {
            "events_seen": 0, "events_accepted": 0, "opportunities_built": 0,
            "opportunities_suppressed": 0, "spoke": 0, "revalidation_cancelled": 0,
        }
        self.last_result: InitiativeResult | None = None
        self.last_revalidation_ms: float = .0
        #: 各段の所要時間。**通常イベントごとにLLMを呼ばない**ことの証拠。
        self._latency: dict[str, float] = {}
        # -- いまどうなっているか / 何を目指しているか (Phase 6) ----------
        #
        # **記憶（過去）とは別に持つ。** 増えていく記憶と、古くなって消える
        # 現在状況を同じ入れ物へ入れると、どちらかが必ずおかしくなる。
        from neuro_voice.cognition.goals import GoalAdmissionGate
        from neuro_voice.cognition.world import GroundedWorldState, WorldStateGate

        self.world_enabled = bool(get("world_state.enabled", False))
        self.entity_tracking_enabled = bool(get("world_state.entity_tracking_enabled", False))
        self.fact_tracking_enabled = bool(get("world_state.fact_tracking_enabled", False))
        self.goals_enabled = bool(get("goals.enabled", False))
        self.obligation_integration_enabled = bool(
            get("goals.obligation_integration_enabled", False))
        self.next_action_enabled = bool(get("goals.next_action_enabled", False))
        self.world = GroundedWorldState()
        self.world_gate = WorldStateGate()
        self.goal_gate = GoalAdmissionGate(
            max_active=int(get("goals.max_active", 5)))
        self.goals: dict[str, Any] = {}
        # -- 実経路への接続と永続化 (Phase 6B) ---------------------------
        self.observation_wiring_enabled = bool(
            get("world_state.observation_wiring_enabled", False))
        self.speaker_observation_enabled = bool(
            get("world_state.speaker_observation_enabled", False))
        self.vision_observation_enabled = bool(
            get("world_state.vision_observation_enabled", False))
        self.game_observation_enabled = bool(
            get("world_state.game_observation_enabled", False))
        # -- 入力元ごとの差を埋める (Phase 6C) ---------------------------
        #
        # **1つずつ上げられるようにする。** まとめて上げると、
        # おかしくなった時にどの経路が原因か分からない。
        self.discord_presence_enabled = bool(
            get("world_state.discord_presence_enabled", False))
        self.vision_scene_parity_enabled = bool(
            get("world_state.vision_scene_parity_enabled", False))
        self.ktane_observation_enabled = bool(
            get("world_state.ktane_observation_enabled", False))
        # -- 誰が居るのか / 誰なのか (Phase 6D) --------------------------
        #
        # **接続・声紋・人物を別々に持つ。** 1つの番号が3つの違うことを
        # 意味していると、声紋が一度外れただけで関係値が別人へ移る。
        self.identity_resolution_enabled = bool(
            get("identity.resolution_enabled", False))
        self.voice_transport_linking_enabled = bool(
            get("identity.voice_transport_linking_enabled", False))
        #: **既定 true。** 取り消せない統合を作る理由が無い。
        self.reversible_links_enabled = bool(
            get("identity.reversible_links_enabled", True))
        self.presence_registry_enabled = bool(get("presence.registry_enabled", False))
        self.local_estimation_enabled = bool(get("presence.local_estimation_enabled", False))
        #: 挨拶を Action Selector と Speech Gate へ通すか。
        #: **false の間は従来の固定文経路。** 二重に喋らせないため排他。
        self.cognitive_greeting_enabled = bool(
            get("discord.cognitive_greeting_enabled", False))
        self.farewell_enabled = bool(
            get("discord.farewell_opportunity_enabled", False))
        self.extended_vision_fact_enabled = bool(
            get("world_state.extended_vision_fact_enabled", False))
        self._resolver: Any = None
        self._presence: Any = None
        #: 出入りの候補。**溜めるだけ。ここからは喋らない。**
        self._pending_social: list[Any] = []
        self._last_seen: dict[str, float] = {}
        self._greeted_at: dict[str, float] = {}
        self._talked_with: dict[str, float] = {}
        self.world_persistence_enabled = bool(get("world_state.persistence_enabled", False))
        self.goal_persistence_enabled = bool(get("goals.persistence_enabled", False))
        self.obligation_bridge_enabled = bool(get("goals.obligation_bridge_enabled", False))
        self.permission_enforcement_enabled = bool(
            get("permissions.enforcement_enabled", False))
        self._store = None
        self._adapter = None
        #: 同じ更新が往復しないようにする印。
        self._sync_markers: set[str] = set()
        self.last_persistence_error: str = ""
        self.counters.update({
            "entities_seen": 0, "facts_seen": 0, "facts_stale": 0,
            "goals_admitted": 0, "goals_held": 0, "goals_rejected": 0,
            "approval_required": 0, "world_persisted": 0, "world_persist_failed": 0,
            "observations_speaker": 0, "observations_vision": 0, "observations_game": 0,
            "observations_discord": 0, "observations_ktane": 0,
            "discord_reconciled": 0,
            "identity_resolved": 0, "identity_conflicts": 0, "voice_samples": 0,
            "greetings_offered": 0, "greetings_skipped": 0, "farewells_offered": 0,
        })

    # ------------------------------------------------------------------
    # 観測
    # ------------------------------------------------------------------

    def observe(self, event: AttentionEvent, *, now: float | None = None) -> bool:
        """出来事を1つ取り込む。**間引かれたら False。**"""
        if not (self.enabled and self.attention_enabled):
            return False
        self.counters["events_seen"] += 1
        moment = time.monotonic() if now is None else now
        if not self.intake.accept(event, now=moment):
            return False
        self.counters["events_accepted"] += 1
        apply_event(self.state, event, now=moment)
        return True

    # ------------------------------------------------------------------
    # いまどうなっているか (Phase 6)
    # ------------------------------------------------------------------

    def observe_entity(self, name: str, **kwargs):
        """対象を1つ観測する。**確信が足りなければ既知へ寄せない。**"""
        if not (self.world_enabled and self.entity_tracking_enabled):
            return None, None
        started = time.perf_counter()
        try:
            entity, delta = self.world_gate.observe_entity(self.world, name, **kwargs)
        except Exception:
            logger.exception("対象の観測でエラー")
            return None, None
        self.counters["entities_seen"] += 1
        self._latency["entity_resolution_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        return entity, delta

    def observe_fact(self, subject_id: str, predicate: str, value: Any = True, **kwargs):
        """事実を1つ観測する。**矛盾は僅差では覆さない。**"""
        if not (self.world_enabled and self.fact_tracking_enabled):
            return None
        started = time.perf_counter()
        try:
            delta = self.world_gate.observe_fact(
                self.world, subject_id, predicate, value, **kwargs)
        except Exception:
            logger.exception("事実の観測でエラー")
            return None
        self.counters["facts_seen"] += 1
        self._latency["world_state_delta_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        return delta

    def sweep_world(self, *, now: float | None = None) -> list[str]:
        """古い事実へ印を付ける。**消さずに落とす。**"""
        if not self.world_enabled:
            return []
        started = time.perf_counter()
        marked = self.world.sweep(now=now)
        self.counters["facts_stale"] += len(marked)
        self._latency["fact_validation_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        return marked

    def propose_goal(self, goal, *, source: str = "", approved: bool = False):
        """目標を登録してよいか。**判断は `GoalAdmissionGate` の1箇所だけ。**"""
        if not self.goals_enabled:
            return None
        from neuro_voice.cognition.goals import AdmissionDecision, GoalStatus

        started = time.perf_counter()
        active = sum(1 for item in self.goals.values()
                     if str(item.status) == str(GoalStatus.ACTIVE))
        verdict = self.goal_gate.evaluate(
            goal, source=source, active_count=active, approved=approved)
        goal.status = verdict.status
        if verdict.decision is AdmissionDecision.REJECT:
            self.counters["goals_rejected"] += 1
        else:
            self.goals[goal.goal_id] = goal
            self.counters["goals_admitted" if verdict.decision is
                          AdmissionDecision.ADMIT else "goals_held"] += 1
        self._latency["goal_matching_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        return verdict

    def live_goals(self) -> list[Any]:
        from neuro_voice.cognition.goals import GoalStatus

        return [item for item in self.goals.values()
                if str(item.status) in {str(GoalStatus.ACTIVE), str(GoalStatus.BLOCKED)}]

    def next_actions(self, *, end_signal: float = .0):
        """目標から次の1〜3手。**発話ではない。**"""
        if not (self.goals_enabled and self.next_action_enabled):
            return []
        from neuro_voice.cognition.goals import next_actions

        started = time.perf_counter()
        facts = self.world.usable_facts()
        confidence = min((item.confidence for item in facts), default=1.0)
        out = next_actions(
            list(self.goals.values()), fact_confidence=confidence,
            end_signal=end_signal)
        self.counters["approval_required"] += sum(
            1 for item in out if str(item.permission) == "needs_approval")
        self._latency["next_action_generation_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        self._latency["permission_decision_ms"] = round(sum(
            float(getattr(item.decision, "decided_ms", .0) or .0)
            for item in out if item.decision is not None), 4)
        return out

    def goal_bias(self, *, end_signal: float = .0) -> dict[str, float]:
        """目標から行動候補への**小さな**補正。上書きではない。"""
        if not self.goals_enabled:
            return {}
        from neuro_voice.cognition.goals import goal_bias

        facts = self.world.usable_facts()
        confidence = min((item.confidence for item in facts), default=1.0)
        return goal_bias(
            list(self.goals.values()), fact_confidence=confidence,
            end_signal=end_signal)

    # ------------------------------------------------------------------
    # 永続化と復元 (Phase 6B)
    #
    # **新しいDBへ移らない。** 既存の `MemoryStore` に表を足すだけ。
    # 書き込みは1件ずつではなくまとめて——映像1フレームごとにSQLiteを
    # 叩くと音声応答が止まる。
    # ------------------------------------------------------------------

    def attach_store(self, store) -> None:
        self._store = store

    def observe_speaker(self, *, speaker_id: int, name: str, confirmed: bool,
                        event_id: str = "", now: float | None = None) -> int:
        """話者確認 → 参加者と、いま話している人。"""
        if not (self.observation_wiring_enabled and self.speaker_observation_enabled):
            return 0
        self.counters["observations_speaker"] += 1
        return self.observation_adapter().observe_speaker(
            speaker_id=speaker_id, name=name, confirmed=confirmed,
            event_id=event_id, now=now)

    def observe_vision(self, observation, *, now: float | None = None) -> int:
        """映像の解析結果 → いまの画面。**全フレームは取り込まない。**

        `vision_scene_parity_enabled` が下りている間は、Phase 6B と同じく
        **ゲーム中の画面だけ**を取り込む。メニューや読み込み中を扱うのは
        新しい振る舞いなので、別のフラグで上げられるようにしてある。
        """
        if not (self.observation_wiring_enabled and self.vision_observation_enabled):
            return 0
        self.counters["observations_vision"] += 1
        if not self.vision_scene_parity_enabled:
            from neuro_voice.cognition.observation import normalise_scene

            if normalise_scene(getattr(observation, "scene_type", "")) != "gameplay":
                return 0
        return self.observation_adapter().observe_vision(observation, now=now)

    def observe_game_event(self, event, *, now: float | None = None) -> int:
        """ゲームの出来事 → いまのプレイ状況。"""
        if not (self.observation_wiring_enabled and self.game_observation_enabled):
            return 0
        self.counters["observations_game"] += 1
        return self.observation_adapter().observe_game_event(event, now=now)

    def observe_discord_presence(self, *, user_id: int, display_name: str,
                                 present: bool, **kwargs) -> int:
        """**実経路D**: Discord の入退室 → 誰が居るか。"""
        if not (self.observation_wiring_enabled and self.discord_presence_enabled):
            return 0
        self.counters["observations_discord"] += 1
        return self.observation_adapter().observe_discord_presence(
            user_id=user_id, display_name=display_name, present=present, **kwargs)

    def reconcile_discord(self, members, **kwargs) -> dict[str, Any]:
        """**いま実際に居る人の一覧**で合わせ直す。

        再起動後・再接続後は、前回の参加状態を根拠にしてはいけない。
        切断中の出入りはイベントが来ないので、一覧で埋める。
        """
        if not (self.observation_wiring_enabled and self.discord_presence_enabled):
            return {"joined": [], "left": [], "present": 0, "reason": "disabled"}
        self.counters["discord_reconciled"] += 1
        return self.observation_adapter().reconcile_discord(members, **kwargs)

    def observe_ktane_event(self, *, event: str, **kwargs) -> int:
        """**実経路E**: KTANE の出来事 → 爆弾の状態。会話由来のみ。

        あわせて「爆弾を解除する」目標を開閉する。**目標は発話しない**
        ——行動候補への小さな補正までしか届かない。
        """
        if not (self.observation_wiring_enabled and self.ktane_observation_enabled):
            return 0
        self.counters["observations_ktane"] += 1
        applied = self.observation_adapter().observe_ktane_event(event=event, **kwargs)
        with contextlib.suppress(Exception):
            self._sync_ktane_goal(str(event or ""), **kwargs)
        return applied

    #: KTANE の目標。**1つの爆弾に1つ。** 増やさない。
    KTANE_GOAL_KEY = "ktane_defuse"

    def _sync_ktane_goal(self, event: str, *, session_id: str = "",
                         outcome: str = "", confidence: float = .9, **_) -> None:
        """爆弾の開始・終了を目標へ写す。

        **低い確信の画面認識だけで目標を ACTIVE にしない。**
        ここが受け取る `bomb_started` は、ユーザーが「始めよう」と
        言った時にしか出ない（`confidence` が高いのはそのため）。
        """
        if not self.goals_enabled:
            return
        from neuro_voice.cognition.goals import GoalRecord, GoalStatus, GoalType

        existing = next(
            (item for item in self.goals.values()
             if item.goal_id.startswith(self.KTANE_GOAL_KEY)), None)
        if event == "bomb_started":
            if float(confidence) < .8:
                return
            if existing is not None and existing.live:
                return
            goal = GoalRecord(
                description="爆弾を解除する",
                goal_type=GoalType.GAME_OBJECTIVE, owner_ids=("user", "poppo"),
                confidence=float(confidence), priority=.9,
                completion_conditions=("爆弾が解除された", "爆弾が爆発した"),
                source_event_ids=(f"ktane:{session_id}",),
                goal_id=f"{self.KTANE_GOAL_KEY}:{session_id or 'session'}"[:32])
            # **ユーザーが明示した共同作業。** 自前の欲求ではない。
            self.propose_goal(goal, source="user_stated")
        elif event == "bomb_ended" and existing is not None:
            existing.status = GoalStatus.COMPLETED
            # 終わったことと、うまくいったことは別。
            existing.outcome = str(outcome or "unknown")
            existing.updated_at = time.monotonic()

    # ------------------------------------------------------------------
    # 誰が居るのか (Phase 6D)
    #
    # **接続が保証する人数と、声から推した人数を分ける。**
    # 混ぜた瞬間に「たぶん」が「確定」として出てくる。
    # ------------------------------------------------------------------

    def resolver(self):
        if self._resolver is None:
            from neuro_voice.cognition.identity import IdentityResolver

            self._resolver = IdentityResolver()
        return self._resolver

    def presence(self):
        if self._presence is None:
            from neuro_voice.cognition.presence import PresenceRegistry

            self._presence = PresenceRegistry(session_id=self.source)
        return self._presence

    def resolve_identity(self, *, transport=None, voice=None, event_id: str = ""):
        """この出来事は誰か。**判断はここ1箇所だけ。**"""
        if not self.identity_resolution_enabled:
            from neuro_voice.cognition.identity import IdentityResolution

            return IdentityResolution()
        self.counters["identity_resolved"] += 1
        started = time.perf_counter()
        result = self.resolver().resolve(
            transport=transport, voice=voice, event_id=event_id)
        self._latency["identity_resolution_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        if str(result.resolution_status) == "conflicted" or result.conflict_reason:
            self.counters["identity_conflicts"] += 1
        return result

    def note_voice_sample(self, *, transport, voice, event_id: str = ""):
        """**接続が正本と分かっている音声**を、声紋の根拠として積む。

        Discord direct audio は「この user が喋った」が確実なので、
        その時の声紋を本人のものとして数えられる。**1回では確定しない。**
        """
        if not (self.identity_resolution_enabled
                and self.voice_transport_linking_enabled):
            return None
        self.counters["voice_samples"] += 1
        return self.resolver().note_voice_sample(
            transport=transport, voice=voice, event_id=event_id)

    def note_participant_joined(
        self, *, transport_key: str, display_name: str = "", channel: str = "",
        group_size: int = 1, joined_together: int = 1, session_id: str = "",
        now: float | None = None,
    ) -> bool:
        """入室 → 人物解決 → 参加状態 → **挨拶の候補**。

        **ここでは喋らない。** 候補を積むだけで、話すかどうかは
        Action Selector と Speech Gate が決める。
        """
        from neuro_voice.cognition.identity import TransportIdentity
        from neuro_voice.cognition.initiative import greeting_opportunity

        moment = time.monotonic() if now is None else now
        provider, _, user_id = str(transport_key).partition(":")
        transport = TransportIdentity(
            provider=provider or "discord", transport_user_id=user_id,
            display_name=display_name, authoritative=True)
        # **出入りのたびに別のID。** 同じIDを使い回すと、2回目の入室が
        # 「同じイベントの再送」として落ちる（実際そうなった）。
        # 入り直しは別の出来事で、重複ではない。
        event_id = f"join:{transport_key}:{moment:.3f}"
        resolution = self.resolve_identity(transport=transport, event_id=event_id)
        person_id = resolution.person_id or transport.key
        if self.presence_registry_enabled:
            self.presence().note_transport(
                person_id=person_id, transport_identity=transport.key,
                present=True, now=moment)
        # 世界状態にも入れる（第19条: Local と同じ意味論）。
        self.observe_discord_presence(
            user_id=int(user_id) if user_id.isdigit() else -1,
            display_name=display_name, present=True, channel=channel,
            session_id=session_id, event_id=event_id, now=moment)
        if not self.cognitive_greeting_enabled:
            return False
        last_seen = self._last_seen.get(person_id, 0.0)
        opportunity = greeting_opportunity(
            person_id=person_id,
            identity_confidence=float(resolution.confidence),
            known=resolution.known,
            channel_context=channel, group_size=int(group_size),
            joined_together=int(joined_together),
            seconds_since_last_seen=(moment - last_seen) if last_seen else 1e9,
            greeted_recently_seconds=(
                moment - self._greeted_at[person_id]
                if person_id in self._greeted_at else 1e9),
            recent_interaction_seconds=(
                moment - last_seen) if last_seen else 1e9,
            source_event_id=event_id, now=moment)
        self._last_seen[person_id] = moment
        if opportunity is None:
            self.counters["greetings_skipped"] += 1
            return False
        self._pending_social.append(opportunity)
        del self._pending_social[:-4]
        self.counters["greetings_offered"] += 1
        return True

    def note_participant_left(
        self, *, transport_key: str, display_name: str = "", group_size: int = 1,
        session_id: str = "", abrupt: bool = False, now: float | None = None,
    ) -> bool:
        """退出 → **参加状態は必ず更新**、別れの一言は必要な時だけ。"""
        from neuro_voice.cognition.identity import TransportIdentity
        from neuro_voice.cognition.initiative import farewell_opportunity

        moment = time.monotonic() if now is None else now
        provider, _, user_id = str(transport_key).partition(":")
        transport = TransportIdentity(
            provider=provider or "discord", transport_user_id=user_id,
            display_name=display_name, authoritative=True)
        event_id = f"leave:{transport_key}:{moment:.3f}"
        resolution = self.resolve_identity(transport=transport, event_id=event_id)
        person_id = resolution.person_id or transport.key
        if self.presence_registry_enabled:
            # **接続が切れた時だけ `LEFT` を確定できる。**
            self.presence().note_transport(
                person_id=person_id, transport_identity=transport.key,
                present=False, now=moment)
        self.observe_discord_presence(
            user_id=int(user_id) if user_id.isdigit() else -1,
            display_name=display_name, present=False,
            session_id=session_id, event_id=event_id, now=moment)
        self._last_seen[person_id] = moment
        if not self.farewell_enabled:
            return False
        talked_with = person_id in self._talked_with
        opportunity = farewell_opportunity(
            person_id=person_id, known=resolution.known,
            was_talking_with=talked_with,
            identity_confidence=float(resolution.confidence),
            group_size=int(group_size), abrupt=bool(abrupt),
            handled_event_ids=tuple(self._handled_events),
            source_event_id=event_id, now=moment)
        if opportunity is None:
            return False
        self._pending_social.append(opportunity)
        del self._pending_social[:-4]
        self.counters["farewells_offered"] += 1
        return True

    def note_talked_with(self, person_id: str) -> None:
        """その人と実際に言葉を交わした。**別れの一言はここが根拠。**"""
        if person_id:
            self._talked_with[str(person_id)] = time.monotonic()

    def social_opportunities(self, *, now: float | None = None) -> list[Any]:
        """溜まっている出入りの候補。**期限切れは落とす。**"""
        moment = time.monotonic() if now is None else now
        self._pending_social = [
            item for item in self._pending_social if not item.expired(now=moment)]
        return list(self._pending_social)

    def note_greeted(self, person_id: str, *, now: float | None = None) -> None:
        """実際に挨拶した。**同じ相手へ続けて言わないため。**"""
        self._greeted_at[str(person_id)] = (
            time.monotonic() if now is None else now)
        self._pending_social = [
            item for item in self._pending_social
            if person_id not in item.participant_ids]

    def presence_summary(self, *, now: float | None = None):
        from neuro_voice.cognition.presence import PresenceSummary

        if not self.presence_registry_enabled:
            return PresenceSummary()
        return self.presence().summary(now=now)

    def note_local_voice(
        self, *, voice_key: str, person_id: str = "", known: bool = False,
        confidence: float = .5, now: float | None = None,
    ):
        """Local マイクで声が聞こえた。**人数は推測にとどめる。**

        同じマイクから別人の声が入ることはある。**確信が足りない声を
        既知の人物として数えると、その人の関係値が動く。**
        """
        if not (self.presence_registry_enabled and self.local_estimation_enabled):
            return None
        return self.presence().note_voice(
            voice_key=voice_key, person_id=person_id, known=known,
            confidence=confidence, now=now)

    def observation_adapter(self):
        if self._adapter is None:
            from neuro_voice.cognition.observation import WorldObservationAdapter

            self._adapter = WorldObservationAdapter(self)
        return self._adapter

    def flush_world(self, *, now: float | None = None) -> dict[str, Any]:
        """溜まった状態をまとめて書く。**失敗しても会話は止めない**（第17条）。"""
        if not (self.world_enabled and self.world_persistence_enabled):
            return {"persisted": False, "reason": "disabled"}
        store = getattr(self, "_store", None)
        if store is None:
            return {"persisted": False, "reason": "no_store"}
        from neuro_voice.cognition.observation import SESSION_SCOPED_PREDICATES

        started = time.perf_counter()
        try:
            entities = [{
                "entity_id": item.entity_id, "entity_type": str(item.entity_type),
                "canonical_name": item.canonical_name,
                "external_id": str(item.attributes.get("external_id", "")),
                "aliases": list(item.aliases), "attributes": dict(item.attributes),
                "confidence": item.confidence, "status": str(item.status),
                "source_event_ids": list(item.source_event_ids),
                "first_observed_at": item.first_observed_at,
                "last_observed_at": item.last_observed_at,
            } for item in self.world.entities.values()]
            facts = [{
                "fact_key": item.key, "subject_id": item.subject_id,
                "predicate": item.predicate, "value": item.value,
                "confidence": item.confidence, "source_type": str(item.source_type),
                "source_event_ids": list(item.source_event_ids),
                "status": str(item.status), "observed_at": item.observed_at,
                "last_verified_at": item.last_verified_at,
                "ttl": (item.expires_at - item.observed_at) if item.expires_at else None,
                # **一時的な事実には印を付ける。** 起動時に古い扱いへ落とす。
                "session_scoped": item.predicate in SESSION_SCOPED_PREDICATES,
            } for item in self.world.facts.values()]
            store.save_world_entities(entities)
            store.save_world_facts(facts)
            self.counters["world_persisted"] += len(entities) + len(facts)
            self._latency["world_state_persist_ms"] = round(
                (time.perf_counter() - started) * 1000, 3)
            self.last_persistence_error = ""
            return {"persisted": True, "entities": len(entities), "facts": len(facts)}
        except Exception as error:  # noqa: BLE001 — 保存の失敗で会話を止めない
            logger.exception("世界状態の保存に失敗（会話は続ける）")
            self.counters["world_persist_failed"] += 1
            self.last_persistence_error = f"{type(error).__name__}: {error}"[:120]
            # **未保存を成功扱いしない。**
            return {"persisted": False, "reason": self.last_persistence_error}

    def flush_goals(self) -> dict[str, Any]:
        if not (self.goals_enabled and self.goal_persistence_enabled):
            return {"persisted": False, "reason": "disabled"}
        store = getattr(self, "_store", None)
        if store is None:
            return {"persisted": False, "reason": "no_store"}
        try:
            store.save_goals([{
                "goal_id": item.goal_id, "goal_type": str(item.goal_type),
                "description": item.description, "owner_ids": list(item.owner_ids),
                "status": str(item.status), "priority": item.priority,
                "confidence": item.confidence,
                "source_event_ids": list(item.source_event_ids),
                "related_entity_ids": list(item.related_entity_ids),
                "related_memory_ids": list(item.related_memory_ids),
                "completion_conditions": list(item.completion_conditions),
                "obligation_ids": list(item.obligation_ids),
                "blocked_reason": item.blocked_reason,
                "created_at": item.created_at, "updated_at": item.updated_at,
            } for item in self.goals.values()])
            self.last_persistence_error = ""
            return {"persisted": True, "goals": len(self.goals)}
        except Exception as error:  # noqa: BLE001
            logger.exception("目標の保存に失敗（会話は続ける）")
            self.counters["world_persist_failed"] += 1
            self.last_persistence_error = f"{type(error).__name__}: {error}"[:120]
            return {"persisted": False, "reason": self.last_persistence_error}

    def flush_identity(self) -> dict[str, Any]:
        """人物とリンクを書く。**参加している状態は書かない。**

        「この Discord ID はこの人」は再起動しても変わらないので残す。
        「いま居る」は再起動したら確かめ直すので残さない——
        **残すと、起動しただけで「居る」と言い出す。**
        """
        if not self.identity_resolution_enabled:
            return {"persisted": False, "reason": "disabled"}
        store = getattr(self, "_store", None)
        if store is None or self._resolver is None:
            return {"persisted": False, "reason": "no_store"}
        started = time.perf_counter()
        try:
            resolver = self.resolver()
            store.save_identity_links(
                [{"person_id": item.person_id,
                  "canonical_name": item.canonical_name,
                  "created_at": item.created_at}
                 for item in resolver.people.values()],
                [{"link_id": item.link_id, "person_id": item.person_id,
                  "identity_type": str(item.identity_type),
                  "identity_value": item.identity_value,
                  "status": str(item.status), "confidence": item.confidence,
                  "support_count": item.support_count,
                  "contradiction_count": item.contradiction_count,
                  "model_version": item.model_version,
                  "revoked_reason": item.revoked_reason,
                  "superseded_by": item.superseded_by,
                  "source_event_ids": list(item.source_event_ids),
                  "created_at": item.created_at, "updated_at": item.updated_at}
                 for item in resolver.links.values()])
            self._latency["identity_persist_ms"] = round(
                (time.perf_counter() - started) * 1000, 3)
            self.last_persistence_error = ""
            return {"persisted": True, "people": len(resolver.people),
                    "links": len(resolver.links)}
        except Exception as error:  # noqa: BLE001 — 保存の失敗で会話を止めない
            logger.exception("Identityの保存に失敗（会話は続ける）")
            self.counters["world_persist_failed"] += 1
            self.last_persistence_error = f"{type(error).__name__}: {error}"[:120]
            return {"persisted": False, "reason": self.last_persistence_error}

    def hydrate_identity(self) -> dict[str, Any]:
        """人物とリンクを戻す。**取り消した分もそのまま戻す。**"""
        if not self.identity_resolution_enabled:
            return {"hydrated": False, "reason": "disabled"}
        store = getattr(self, "_store", None)
        if store is None:
            return {"hydrated": False, "reason": "no_store"}
        from neuro_voice.cognition.identity import IdentityLink, PersonIdentity

        started = time.perf_counter()
        try:
            resolver = self.resolver()
            for row in store.person_identities():
                record = PersonIdentity(
                    person_id=str(row["person_id"]),
                    canonical_name=str(row.get("canonical_name", "") or ""),
                    created_at=float(row.get("created_at", 0.0) or 0.0))
                resolver.people[record.person_id] = record
            for row in store.identity_links():
                link = IdentityLink(
                    person_id=str(row["person_id"]),
                    identity_type=str(row["identity_type"]),
                    identity_value=str(row["identity_value"]),
                    status=str(row["status"]),
                    confidence=float(row.get("confidence", 0.0) or 0.0),
                    source_event_ids=tuple(row.get("source_event_ids") or ()),
                    support_count=int(row.get("support_count", 0) or 0),
                    contradiction_count=int(row.get("contradiction_count", 0) or 0),
                    model_version=str(row.get("model_version", "") or ""),
                    revoked_reason=str(row.get("revoked_reason", "") or ""),
                    superseded_by=str(row.get("superseded_by", "") or ""),
                    created_at=float(row.get("created_at", 0.0) or 0.0),
                    updated_at=float(row.get("updated_at", 0.0) or 0.0),
                    link_id=str(row["link_id"]))
                resolver.links[link.link_id] = link
            self._latency["identity_hydrate_ms"] = round(
                (time.perf_counter() - started) * 1000, 3)
            return {"hydrated": True, "people": len(resolver.people),
                    "links": len(resolver.links)}
        except Exception as error:  # noqa: BLE001
            logger.exception("Identityの復元に失敗（会話は続ける）")
            self.last_persistence_error = f"{type(error).__name__}: {error}"[:120]
            return {"hydrated": False, "reason": self.last_persistence_error}

    def revoke_identity_link(self, link_id: str, *, reason: str = "",
                             relink_to: str = ""):
        """誤統合を戻す。**物理削除しない。**

        `relink_to` を渡すと、新しい人物へ繋ぎ直す。古いリンクは
        `SUPERSEDED` として残り、何をどう間違えたかが追える。

        **過去の会話や記憶は動かさない。** 動かすと、繋ぎ直しが
        また間違いだった時に二重に壊れる。
        """
        if not self.reversible_links_enabled:
            return None
        resolver = self.resolver()
        if relink_to:
            return resolver.relink(link_id, person_id=relink_to, reason=reason)
        return resolver.revoke(link_id, reason=reason)

    def hydrate(self, *, now: float | None = None) -> dict[str, Any]:
        """起動時の復元。**一時的な事実を現在の事実として戻さない。**

        再起動しただけでゲーム途中の状態を「いまこう」と言うのは嘘になる。
        """
        store = getattr(self, "_store", None)
        if store is None:
            return {"hydrated": False, "reason": "no_store"}
        from neuro_voice.cognition.goals import GoalRecord, GoalStatus
        from neuro_voice.cognition.world import (
            ItemStatus, Presence, WorldEntity, WorldFact,
        )

        started = time.perf_counter()
        moment = time.monotonic() if now is None else now
        out = {"entities": 0, "facts": 0, "stale": 0, "goals": 0}
        try:
            # **一時的な事実は、読む前に古い扱いへ落とす。**
            out["stale"] = store.mark_session_facts_stale()
            if self.world_enabled and self.world_persistence_enabled:
                for row in store.world_entities():
                    entity = WorldEntity(
                        canonical_name=str(row.get("canonical_name", "")),
                        entity_type=str(row.get("entity_type", "unknown")),
                        aliases=tuple(row.get("aliases", []) or ()),
                        attributes=dict(row.get("attributes", {}) or {}),
                        # **見えているとは限らない。** 復元は「在ると推測」まで。
                        presence=Presence.INFERRED,
                        confidence=float(row.get("confidence", .6)),
                        source_event_ids=tuple(row.get("source_event_ids", []) or ()),
                        first_observed_at=moment, last_observed_at=moment,
                        status=str(row.get("status", "active")),
                        entity_id=str(row.get("entity_id", "")))
                    self.world.entities[entity.entity_id] = entity
                    out["entities"] += 1
                for row in store.world_facts():
                    status = str(row.get("status", "active"))
                    if int(row.get("session_scoped", 0)):
                        status = str(ItemStatus.STALE)
                    fact = WorldFact(
                        subject_id=str(row.get("subject_id", "")),
                        predicate=str(row.get("predicate", "")),
                        value=row.get("value"),
                        confidence=float(row.get("confidence", .6)),
                        source_type=str(row.get("source_type", "observation")),
                        source_event_ids=tuple(row.get("source_event_ids", []) or ()),
                        observed_at=moment, last_verified_at=moment,
                        expires_at=moment + float(row.get("ttl") or 120.0),
                        status=status)
                    self.world.facts[fact.key] = fact
                    out["facts"] += 1
            if self.goals_enabled and self.goal_persistence_enabled:
                for row in store.goals():
                    status = str(row.get("status", "proposed"))
                    if status == str(GoalStatus.ACTIVE):
                        # **状態を確かめるまで動かさない。**
                        status = str(GoalStatus.PAUSED)
                    goal = GoalRecord(
                        description=str(row.get("description", "")),
                        goal_type=str(row.get("goal_type", "user_goal")),
                        owner_ids=tuple(row.get("owner_ids", []) or ()),
                        status=status, priority=float(row.get("priority", .5)),
                        confidence=float(row.get("confidence", .6)),
                        source_event_ids=tuple(row.get("source_event_ids", []) or ()),
                        related_entity_ids=tuple(row.get("related_entity_ids", []) or ()),
                        related_memory_ids=tuple(
                            int(x) for x in row.get("related_memory_ids", []) or ()),
                        completion_conditions=tuple(
                            row.get("completion_conditions", []) or ()),
                        obligation_ids=tuple(row.get("obligation_ids", []) or ()),
                        blocked_reason=str(row.get("blocked_reason", "")),
                        created_at=moment, updated_at=moment,
                        goal_id=str(row.get("goal_id", "")))
                    self.goals[goal.goal_id] = goal
                    out["goals"] += 1
            self._latency["world_state_hydrate_ms"] = round(
                (time.perf_counter() - started) * 1000, 3)
            # **人物と、接続・声紋の対応は残す。** 参加している状態は残さない。
            identity = self.hydrate_identity()
            out["identity_links"] = int(identity.get("links", 0) or 0)
            out["hydrated"] = True
            return out
        except Exception as error:  # noqa: BLE001
            logger.exception("起動時の復元に失敗（空の状態で続ける）")
            self.last_persistence_error = f"{type(error).__name__}: {error}"[:120]
            return {"hydrated": False, "reason": self.last_persistence_error}

    # ------------------------------------------------------------------
    # 未完了事項との橋 (Phase 6B)
    # ------------------------------------------------------------------

    def link_obligation(self, goal_id: str, obligation_id: str, *, event_id: str = "") -> bool:
        """目標と未完了事項を繋ぐ。**既存Obligationは置き換えない。**"""
        if not (self.goals_enabled and self.obligation_bridge_enabled):
            return False
        goal = self.goals.get(goal_id)
        if goal is None:
            return False
        started = time.perf_counter()
        goal.obligation_ids = tuple(dict.fromkeys(
            (*goal.obligation_ids, str(obligation_id))))[:8]
        store = getattr(self, "_store", None)
        if store is not None:
            with contextlib.suppress(Exception):
                store.link_goal_obligation(goal_id, obligation_id, event_id=event_id)
        self._latency["goal_obligation_sync_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        return True

    def sync_obligation_state(
        self, obligation_id: str, obligation_status: str, *, event_id: str = "",
    ) -> list[str]:
        """未完了事項の状態を目標へ反映する。**無限ループを作らない。**

        同じ event_id で戻ってきた更新は無視する。片方を直したら
        もう片方が直り、それがまた片方を直す、を止めるため。
        """
        if not (self.goals_enabled and self.obligation_bridge_enabled):
            return []
        from neuro_voice.cognition.goals import OBLIGATION_TO_GOAL

        marker = f"{obligation_id}:{event_id}"
        if event_id and marker in self._sync_markers:
            return []
        if event_id:
            self._sync_markers.add(marker)
            if len(self._sync_markers) > 128:
                self._sync_markers = set(list(self._sync_markers)[-64:])
        target = OBLIGATION_TO_GOAL.get(str(obligation_status))
        if target is None:
            return []
        changed: list[str] = []
        for goal in self.goals.values():
            if str(obligation_id) not in goal.obligation_ids:
                continue
            if str(goal.status) == str(target):
                continue
            goal.status = target
            goal.updated_at = time.monotonic()
            changed.append(goal.goal_id)
        if changed:
            self.flush_goals()
        return changed

    def obligation_updates_for(self, goal_id: str) -> list[tuple[str, str]]:
        """目標の状態から、未完了事項へ返すべき状態。**呼び出し側が適用する。**"""
        from neuro_voice.cognition.goals import GOAL_TO_OBLIGATION

        goal = self.goals.get(goal_id)
        if goal is None:
            return []
        target = GOAL_TO_OBLIGATION.get(str(goal.status))
        if target is None:
            return []
        return [(item, target) for item in goal.obligation_ids]

    # ------------------------------------------------------------------
    # 評価
    # ------------------------------------------------------------------

    #: 機会の種類 → それを許す機能フラグの名前。
    #: **段階ごとに止められるようにする**のが目的で、既定はどれも off。
    _KIND_FLAGS: dict[str, str] = {
        OpportunityType.COMMENT_ON_GAME: "game_commentary_enabled",
        OpportunityType.REACT_TO_EVENT: "game_commentary_enabled",
        OpportunityType.ACKNOWLEDGE_CHANGE: "game_commentary_enabled",
        OpportunityType.SHARE_RELEVANT_MEMORY: "memory_initiative_enabled",
        OpportunityType.LIGHT_FOLLOW_UP: "memory_initiative_enabled",
        OpportunityType.RESUME_OBLIGATION: "idle_initiative_enabled",
        OpportunityType.CONTINUE_SHARED_TOPIC: "idle_initiative_enabled",
        OpportunityType.REMIND: "idle_initiative_enabled",
    }

    def _stage_allows(self, opportunity: InitiativeOpportunity) -> bool:
        flag = self._KIND_FLAGS.get(str(opportunity.opportunity_type))
        return True if flag is None else bool(getattr(self, flag, False))

    def evaluate(
        self, conditions: SpeakingConditions, *,
        obligations: tuple[str, ...] = (), relationship_fit: float = .0,
        goals: tuple[str, ...] = (), now: float | None = None,
        internal_state=None, participant_id: str = "",
        memories=(), participant_count: int = 1, others_talking: bool = False,
        addressed_to_me: bool = False,
        seconds_since_user_turn: float = 999.0,
        seconds_since_own_speech: float = 999.0,
    ) -> InitiativeResult:
        """いま自分から話す理由があるか。**発話はしない。**

        返すのは Action Selector へ渡す機会の一覧。**沈黙を必ず含める**ので、
        呼び出し側が「候補が空なら黙る」を書く必要はない。
        """
        if not self.enabled:
            return InitiativeResult()
        moment = time.monotonic() if now is None else now
        latency: dict[str, float] = {}
        try:
            mark = time.perf_counter()
            focus = self.focus_manager.select(self.state, now=moment, goals=goals)
            self.focus_manager.commit(self.state, focus, now=moment)
            latency["focus_ms"] = (time.perf_counter() - mark) * 1000

            mark = time.perf_counter()
            # **いま口を開くのが自然か**を、価値とは別軸で持つ。
            # ここが既定値のままだと「価値はあるが今じゃない」を表せない。
            timing = timing_score(
                seconds_since_user_turn=seconds_since_user_turn,
                seconds_since_own_speech=seconds_since_own_speech,
                topic_just_closed=conditions.topic_closed,
                assistant_speaking=conditions.assistant_speaking,
                user_speaking=conditions.user_speaking)
            built = opportunities_from_events(
                self.state.pending_opportunities, obligations=obligations,
                relationship_fit=relationship_fit, timing=timing, now=moment)
            if self.memory_initiative_enabled and memories:
                # **記憶をそのまま喋らない。** 機会にしてから同じ列へ入れる。
                from_memory = opportunities_from_memories(
                    memories, participant_ids=(participant_id,) if participant_id else (),
                    now=moment)
                for item in from_memory:
                    item.timing_score = timing
                built += from_memory
            # 出入りへの反応 (Phase 6D)。**同じ列に並べる。**
            #
            # 挨拶だけを別の経路で先に喋らせると、そこだけ予算も
            # 割り込み判定も通らない。実際そうなっていた。
            social = self.social_opportunities(now=moment)
            for item in social:
                item.timing_score = timing
            built += social
            latency["opportunity_generation_ms"] = (time.perf_counter() - mark) * 1000

            mark = time.perf_counter()
            built = merge_opportunities(built, now=moment)
            latency["deduplication_ms"] = (time.perf_counter() - mark) * 1000
            self.counters["opportunities_built"] += len(built)

            # 内面と場の広さを、**小さな補正**として乗せる。
            mark = time.perf_counter()
            for item in built:
                apply_internal_state(item, internal_state, participant_id=participant_id)
                extra = social_cost_in_group(
                    item.target_scope, participant_count=participant_count,
                    others_talking=others_talking, addressed_to_me=addressed_to_me,
                    topic_is_shared=bool(item.topic_id))
                if extra:
                    item.social_cost = min(1.0, item.social_cost + extra)
                    item.reasons = tuple(dict.fromkeys(
                        (*item.reasons, f"group_cost:{participant_count}")))[:6]
            latency["initiative_scoring_ms"] = (time.perf_counter() - mark) * 1000

            mark = time.perf_counter()
            conditions.handled_event_ids = tuple(self._handled_events[-32:])
            conditions.spoken_dedup_keys = tuple(self._spoken_keys[-32:])
            kept: list[InitiativeOpportunity] = []
            suppressed: list[tuple[str, str]] = []
            for item in built:
                if not self._stage_allows(item):
                    suppressed.append((item.opportunity_id, "stage_disabled"))
                    continue
                reason = suppression_reason(
                    item, conditions,
                    budget_reason=self.budget.check(item, now=moment), now=moment)
                if reason:
                    suppressed.append((item.opportunity_id, reason))
                    self.counters["opportunities_suppressed"] += 1
                    continue
                kept.append(item)
            latency["suppression_ms"] = (time.perf_counter() - mark) * 1000
        except Exception:
            # 機会づくりで落ちても会話は続ける（第17条）。黙るだけ。
            logger.exception("発話機会の評価でエラー（今回は黙る）")
            return InitiativeResult()

        # **沈黙は常に候補。** 機会があることと話すべきことは別。
        kept.sort(key=lambda item: -item.initiative_score)
        result = InitiativeResult(
            opportunities=(*kept[:3], silent_opportunity()),
            focus=focus, suppressed=tuple(suppressed), latency_ms=latency,
        )
        self.last_result = result
        return result

    # ------------------------------------------------------------------
    # 発話の直前と直後
    # ------------------------------------------------------------------

    def confirm(
        self, candidate, conditions: SpeakingConditions, *,
        higher_priority_pending: bool = False, now: float | None = None,
    ) -> str:
        """TTSへ入れる直前の再確認。空文字なら出してよい。"""
        if not self.pre_speech_revalidation_enabled:
            return ""
        started = time.perf_counter()
        moment = time.monotonic() if now is None else now
        conditions.handled_event_ids = tuple(self._handled_events[-32:])
        conditions.spoken_dedup_keys = tuple(self._spoken_keys[-32:])
        reason = revalidate(
            candidate, conditions,
            higher_priority_pending=higher_priority_pending, now=moment)
        self.last_revalidation_ms = round((time.perf_counter() - started) * 1000, 3)
        if reason:
            self.counters["revalidation_cancelled"] += 1
        return reason

    def record_spoken(
        self, opportunity: InitiativeOpportunity | None = None, *,
        cost: float = 1.0, event_ids: tuple[str, ...] = (),
        dedup_key: str = "", topic_id: str = "", now: float | None = None,
    ) -> None:
        """話したことを記録する。**同じことを二度言わないための材料。**"""
        moment = time.monotonic() if now is None else now
        if opportunity is not None:
            self.budget.record(opportunity, cost=cost, now=moment)
            dedup_key = dedup_key or opportunity.dedup()
            event_ids = event_ids or opportunity.source_event_ids
            topic_id = topic_id or opportunity.topic_id
        else:
            self.budget.record(
                silent_opportunity(), cost=cost, now=moment)
        if dedup_key:
            self._spoken_keys.append(dedup_key)
            del self._spoken_keys[:-32]
        for item in event_ids:
            self._handled_events.append(item)
        del self._handled_events[:-64]
        note_initiative(self.state, topic_id or "unknown", now=moment)
        self.counters["spoke"] += 1

    def allows_warning(self, key: str, *, now: float | None = None) -> bool:
        """同じ危険を叫び直してよいか。**別の危険は止めない。**"""
        return self.warn_throttle.allows(key, now=now)

    # ------------------------------------------------------------------

    #: 実イベントから保存までの通し（Phase 6B §18）。
    #: **合計を出しておかないと、どこが遅いかの前に「遅いのか」が分からない。**
    SLICE_LATENCY_KEYS = (
        "entity_resolution_ms", "world_state_delta_ms", "fact_validation_ms",
        "goal_matching_ms", "next_action_generation_ms", "permission_decision_ms",
        "goal_obligation_sync_ms", "world_state_persist_ms",
    )

    def latency_ms(self) -> dict[str, float]:
        out = dict(self._latency)
        if self._adapter is not None:
            out.update(self._adapter.last_latency_ms)
        out["vertical_slice_total_ms"] = round(sum(
            float(out.get(key, .0) or .0)
            for key in (*self.SLICE_LATENCY_KEYS, "observation_adapter_ms")), 3)
        return out

    def status(self) -> dict[str, Any]:
        """診断用。**本文は入れない**（第12条）。"""
        return {
            "enabled": self.enabled,
            "speech_enabled": self.speech_enabled,
            "stages": {
                "attention": self.attention_enabled,
                "game_commentary": self.game_commentary_enabled,
                "memory_initiative": self.memory_initiative_enabled,
                "idle_initiative": self.idle_initiative_enabled,
                "pre_speech_revalidation": self.pre_speech_revalidation_enabled,
            },
            "source": self.source,
            "revalidation_ms": self.last_revalidation_ms,
            "world": {
                "enabled": self.world_enabled,
                "entity_tracking": self.entity_tracking_enabled,
                "fact_tracking": self.fact_tracking_enabled,
                **self.world.summary(),
            },
            "sources": {
                "speaker": self.speaker_observation_enabled,
                "vision": self.vision_observation_enabled,
                "game": self.game_observation_enabled,
                "discord_presence": self.discord_presence_enabled,
                "vision_scene_parity": self.vision_scene_parity_enabled,
                "ktane": self.ktane_observation_enabled,
                "extended_vision_fact": self.extended_vision_fact_enabled,
            },
            "identity": {
                "enabled": self.identity_resolution_enabled,
                "voice_transport_linking": self.voice_transport_linking_enabled,
                "reversible": self.reversible_links_enabled,
                **(self._resolver.status() if self._resolver is not None else {}),
            },
            "presence": {
                "registry": self.presence_registry_enabled,
                "local_estimation": self.local_estimation_enabled,
                "cognitive_greeting": self.cognitive_greeting_enabled,
                "farewell": self.farewell_enabled,
                "pending_social": len(self._pending_social),
                **(self._presence.status() if self._presence is not None else {}),
            },
            "persistence": {
                "world": self.world_persistence_enabled,
                "goals": self.goal_persistence_enabled,
                "obligation_bridge": self.obligation_bridge_enabled,
                "permission_enforcement": self.permission_enforcement_enabled,
                "last_error": self.last_persistence_error,
            },
            "observation": (
                self._adapter.status() if self._adapter is not None
                else {"counters": {}, "intake": {}, "latency_ms": {}, "last": {}}),
            "goals": {
                "enabled": self.goals_enabled,
                "obligation_integration": self.obligation_integration_enabled,
                "next_action": self.next_action_enabled,
                "records": [item.snapshot() for item in self.goals.values()][:6],
            },
            "latency_ms": self.latency_ms(),
            "counters": dict(self.counters),
            "attention": self.state.summary(),
            "intake": self.intake.snapshot(),
            "budget": self.budget.snapshot(),
            "last": self.last_result.snapshot() if self.last_result is not None else None,
        }


__all__ = ["InitiativeResult", "InitiativeRuntime"]
