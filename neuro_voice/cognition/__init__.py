"""認知カーネル: **文章を作る前に「何をするか」を決める層**。

これまでの経路は、入力を受けたらすぐ Conversation Planner が「どう話すか」を
決めていた。話す以外の選択肢——黙る、聞き返す、警告する、中断した話を再開
する、覚えるだけ——が構造として無かった。

    入力 → 状況解釈 → 内部状態参照 → 行動候補 → 行動選択
         → 応答計画 → 言語化 → 結果評価 → 状態更新

この閉ループを最小限で通す。既存のモジュールは持ち主のまま:

* 義務・参照解決・検索可否は `dialogue/kernel.py` の `TurnFrame`
* 「どう話すか」は `dialogue/conversation_planner.py`
* 言い方は `dialogue/surface_realizer.py`
* 関係性は `mind/relationship.py`、感情は `mind/temporal_self.py`
* 反射（VAD・割り込み・TTS停止）は `pipeline.py`。**ここへ通さない**

Phase 3 で、経験を残して次に使う部分が加わった。**保存先は既存の
`mind/store.py` のまま**で、新しい記憶基盤は作っていない:

* 何を覚えるかの判断は `episodic.py` の `MemoryWriteGate`
* いつ思い出すかと、思い出した結果の行動への効かせ方は `recall.py`
* 複数の経験から出る仮説は `reflection.py`

`cognition.enabled` が false の間は既存経路のまま動く。記憶側も
`memory.episodic_enabled` / `retrieval_enabled` / `reflection_enabled` で
個別に止められる（すべて既定 false）。
"""
from neuro_voice.cognition.attention import (
    AttentionEvent, AttentionEventType, ContinuousAttentionState, EventIntake,
    FocusDecision, FocusKind, FocusManager, apply_event, note_initiative,
)
from neuro_voice.cognition.episodic import (
    EpisodicMemory, MemoryCandidate, MemoryStatus, MemoryWriteGate,
    WriteDecision, WriteVerdict, propose_candidates, self_failure_candidate,
)
from neuro_voice.cognition.initiative import (
    GameEventCategory, InitiativeBudget, InitiativeOpportunity, OpportunityType,
    SpeakingConditions, TargetScope, WarnThrottle, apply_internal_state,
    classify_game_event, farewell_opportunity, greeting_opportunity,
    merge_opportunities, opportunities_from_events,
    opportunities_from_memories, revalidate, silent_opportunity,
    social_cost_in_group, suppression_reason,
)
from neuro_voice.cognition.goals import (
    GOAL_TO_OBLIGATION, OBLIGATION_TO_GOAL, ActionCategory, AdmissionDecision,
    GoalAdmissionGate, GoalRecord, GoalStatus, GoalType, NextAction, Permission,
    PermissionDecision, PermissionResult, categorise, decide_permission,
    goal_bias, mark_completed, next_actions, pause_for, permission_for,
    requires_approval, resume_candidates,
)
from neuro_voice.cognition.identity import (
    IdentityLink, IdentityResolution, IdentityResolver, IdentityType, LinkStatus,
    PersonIdentity, ResolutionStatus, TransportIdentity, VoiceIdentity,
)
from neuro_voice.cognition.kernel import CognitiveKernel
from neuro_voice.cognition.presence import (
    PresenceEntry, PresenceRegistry, PresenceSource, PresenceStatus, PresenceSummary,
)
from neuro_voice.cognition.observation import (
    SESSION_SCOPED_PREDICATES, EntityObservation, FactObservation,
    ObservationIntake, ObservationResult, WorldObservationAdapter,
    from_game_event, from_speaker, from_vision,
)
from neuro_voice.cognition.recall import (
    MemoryInfluence, RecallQuery, ScoredMemory, build_query, influence_from,
    planner_payload, rank, retrieval_trigger,
)
from neuro_voice.cognition.reflection import (
    Reflection, ReflectionStatus, ReflectionTarget,
    derive_conversation_strategy, revise, should_reflect,
)
from neuro_voice.cognition.state import CognitiveState, build_state
from neuro_voice.cognition.world import (
    DeltaOperation, EntityType, GroundedWorldState, ItemStatus, Presence,
    WorldEntity, WorldFact, WorldStateDelta, WorldStateGate, resolve_entity,
)
from neuro_voice.cognition.types import (
    ActionCandidate, ActionDecision, ActionOutcome, ActionType,
    CognitiveEvent, EventType, InformationType,
)

__all__ = [
    "ActionCandidate", "ActionDecision", "ActionOutcome", "ActionType",
    "AttentionEvent", "AttentionEventType", "CognitiveEvent", "CognitiveKernel",
    "CognitiveState", "ContinuousAttentionState", "EpisodicMemory", "EventIntake",
    "EventType", "FocusDecision", "FocusKind", "FocusManager", "GameEventCategory",
    "InformationType", "InitiativeBudget", "InitiativeOpportunity",
    "MemoryCandidate", "MemoryInfluence", "MemoryStatus", "MemoryWriteGate",
    "OpportunityType", "RecallQuery", "TargetScope", "Reflection", "ReflectionStatus",
    "ReflectionTarget", "ScoredMemory", "SpeakingConditions", "WarnThrottle",
    "WriteDecision", "WriteVerdict", "apply_event", "build_query", "build_state",
    "apply_internal_state", "classify_game_event", "derive_conversation_strategy",
    "influence_from", "opportunities_from_memories", "social_cost_in_group",
    "merge_opportunities", "note_initiative", "opportunities_from_events",
    "planner_payload", "propose_candidates", "rank", "retrieval_trigger",
    "revalidate", "revise", "self_failure_candidate", "should_reflect",
    "silent_opportunity", "suppression_reason",
    "AdmissionDecision", "DeltaOperation", "EntityType", "GoalAdmissionGate",
    "GoalRecord", "GoalStatus", "GoalType", "GroundedWorldState", "ItemStatus",
    "NextAction", "Permission", "Presence", "WorldEntity", "WorldFact",
    "WorldStateDelta", "WorldStateGate", "goal_bias", "mark_completed",
    "next_actions", "pause_for", "permission_for", "requires_approval",
    "resolve_entity", "resume_candidates",
    "ActionCategory", "EntityObservation", "FactObservation",
    "GOAL_TO_OBLIGATION", "OBLIGATION_TO_GOAL", "ObservationIntake",
    "ObservationResult", "PermissionDecision", "PermissionResult",
    "SESSION_SCOPED_PREDICATES", "WorldObservationAdapter", "categorise",
    "decide_permission", "from_game_event", "from_speaker", "from_vision",
    "IdentityLink", "IdentityResolution", "IdentityResolver", "IdentityType",
    "LinkStatus", "PersonIdentity", "PresenceEntry", "PresenceRegistry",
    "PresenceSource", "PresenceStatus", "PresenceSummary", "ResolutionStatus",
    "TransportIdentity", "VoiceIdentity",
    "farewell_opportunity", "greeting_opportunity",
]
