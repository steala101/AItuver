"""段階的な有効化と、発話の出どころの統制。

Phase 2.5 で沈黙は本当に沈黙になったが、**認知層を通らない発話経路**
（ゲーム警告の高速経路、自発発話、相槌、システム通知）はそのまま残っている。
経路が分かれていること自体は正しい——危険警告をLLMの応答時間の後ろに置く
わけにはいかない。問題は、**どこから出た音声か分からない**ことである。
分からなければ、通常回答が高速経路へ紛れ込んでも検出できない。

そこで
1. すべての出力要求に出どころを付ける（`SpeechRequest`）
2. 認知層を通らずに話してよい経路を**明示的に許可**する（暗黙に許さない）
3. 有効化を段階に分ける（いきなり全環境で常時ONにしない）

大きな仕組みは作らない。判定は関数1つで済む。
"""
from __future__ import annotations

import time
import uuid
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from neuro_voice.cognition.types import ActionType


class RolloutMode(StrEnum):
    """どこまで認知層を効かせるか。**既定は従来経路。**"""

    DISABLED = "disabled"
    TEST_SESSION = "test_session"
    PRODUCTION_SESSION = "production_session"
    PRODUCTION = "production"
    PROFILE_ALLOWLIST = "profile_allowlist"
    #: Legacy configuration spelling.  Kept readable, but new configuration
    #: must use ``production`` so production activation is unmistakable.
    ENABLED = "enabled"


@dataclass(frozen=True, slots=True)
class CognitionRolloutResolution:
    """The sole, immutable rollout decision for one turn.

    Session overrides take precedence but are process-local.  Configuration
    values never turn a session mode on by themselves.
    """

    requested_mode: str = "disabled"
    resolved_mode: str = "disabled"
    effective_enabled: bool = False
    execution_path: str = "legacy"
    activation_source: str = "config"
    warning: str = ""
    config_fingerprint: str = ""


class CognitionRolloutResolver:
    """Resolve all rollout states without scattering flag checks."""

    _CONFIG_MODES = {"disabled", "production", "enabled", "profile_allowlist"}
    _SESSION_MODES = {"test_session", "production_session"}

    @classmethod
    def resolve(cls, cfg: Any, *, session=None) -> CognitionRolloutResolution:
        requested = str(cfg.get("cognition.rollout_mode", "disabled") or "disabled")
        master_enabled = bool(cfg.get("cognition.enabled", False))
        fingerprint = cls.config_fingerprint(cfg)
        if session is not None and bool(getattr(session, "effective_enabled", False)):
            mode = str(getattr(session, "rollout_mode", "disabled"))
            if mode in cls._SESSION_MODES:
                return CognitionRolloutResolution(
                    requested_mode=requested, resolved_mode=mode,
                    effective_enabled=True, execution_path="cognitive",
                    activation_source="session_override",
                    config_fingerprint=fingerprint,
                )
        if requested not in cls._CONFIG_MODES | cls._SESSION_MODES:
            return CognitionRolloutResolution(
                requested_mode=requested, warning="unknown_rollout_mode",
                config_fingerprint=fingerprint,
            )
        if requested in cls._SESSION_MODES:
            return CognitionRolloutResolution(
                requested_mode=requested, warning="session_mode_requires_override",
                config_fingerprint=fingerprint,
            )
        if not master_enabled:
            warning = "rollout_disabled_by_master_switch" if requested != "disabled" else ""
            return CognitionRolloutResolution(
                requested_mode=requested, warning=warning,
                config_fingerprint=fingerprint,
            )
        if requested == "production":
            return CognitionRolloutResolution(
                requested_mode=requested, resolved_mode="production",
                effective_enabled=True, execution_path="cognitive",
                activation_source="persistent_config", config_fingerprint=fingerprint,
            )
        # ``enabled`` and profile allowlist remain safely non-production until
        # their former rollout semantics are explicitly migrated.
        warning = "legacy_rollout_mode_not_production" if requested != "disabled" else ""
        return CognitionRolloutResolution(
            requested_mode=requested, warning=warning,
            config_fingerprint=fingerprint,
        )

    @staticmethod
    def config_fingerprint(cfg: Any) -> str:
        safe = {
            "enabled": bool(cfg.get("cognition.enabled", False)),
            "rollout_mode": str(cfg.get("cognition.rollout_mode", "disabled") or "disabled"),
            "persona_scope": bool(cfg.get("turn_integrity.persona_scope_enabled", False)),
            "memory_write": bool(cfg.get("memory.write_enabled", False)),
        }
        payload = json.dumps(safe, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class SpeechSource(StrEnum):
    COGNITIVE_DECISION = "cognitive_decision"
    GAME_WARNING_FAST_PATH = "game_warning_fast_path"
    #: 自分から話す機会が Action Selector を通って選ばれたもの（Phase 5）。
    #: `SPONTANEOUS_SPEECH` と違い、**決定と突き合わせて検証する**。
    PROACTIVE_OPPORTUNITY = "proactive_opportunity"
    #: 道具の結果を伝える（Phase 7B）。**`ToolResult` から直接TTSへ
    #: 行く経路を無くしたので、通す先はここしかない。**
    COGNITIVE_TOOL_OUTCOME = "cognitive_tool_outcome"
    SPONTANEOUS_SPEECH = "spontaneous_speech"
    BACKCHANNEL = "backchannel"
    SYSTEM_NOTIFICATION = "system_notification"
    LEGACY_PATH = "legacy_path"


class BypassReason(StrEnum):
    NONE = "none"
    REALTIME_DANGER = "realtime_danger"
    LATENCY_CRITICAL = "latency_critical"
    COGNITION_DISABLED = "cognition_disabled"
    LEGACY_COMPATIBILITY = "legacy_compatibility"


class ClosurePolicy(StrEnum):
    """「もういいよ」への構え。**固定ルールではなく小さな傾き。**

    どちらが自然かは聞いてみないと分からない。構造としては両方選べる
    ままにして、実機で比べられるようにする。
    """

    ADAPTIVE = "adaptive"
    SILENT_PREFERRED = "silent_preferred"
    BRIEF_ACK_PREFERRED = "brief_ack_preferred"


#: 高速経路（＝認知層を通らない）から話してよい行動。
#: **ここに無い行動は、高速経路から出せない。**
FAST_PATH_ALLOWLIST: dict[SpeechSource, frozenset[ActionType]] = {
    SpeechSource.GAME_WARNING_FAST_PATH: frozenset({ActionType.WARN}),
    SpeechSource.BACKCHANNEL: frozenset(),      # 行動を伴わない短い相槌のみ
    SpeechSource.SYSTEM_NOTIFICATION: frozenset(),
}
#: 危険警告として通すのに必要な確からしさ。これ未満は普通の会話として扱う。
DANGER_CONFIDENCE = .60

#: 自分から話す時に通してよい行動（Phase 5）。
#:
#: **`ANSWER` と `WARN` は入れない。** 前者は聞かれてもいないのに答える形に、
#: 後者は危険警告を自発発話の予算と条件で扱う形になる。どちらも別の経路の仕事。
PROACTIVE_ALLOWLIST: frozenset[ActionType] = frozenset({
    ActionType.COMMENT, ActionType.REACT, ActionType.REMIND,
    ActionType.RESUME_OBLIGATION, ActionType.LIGHT_FOLLOW_UP,
    ActionType.SHARE_MEMORY, ActionType.CONTINUE_PREVIOUS_TOPIC,
    ActionType.BRIEF_ACKNOWLEDGE,
    # 出入りへの反応 (Phase 6D)。**挨拶もここを通る。**
    # 固定文を直接TTSへ流す経路を無くしたので、通す先はここしかない。
    ActionType.GREET, ActionType.ACKNOWLEDGE_PRESENCE, ActionType.FAREWELL,
})
#: 自分から話すのに要る確からしさ。**聞かれて答える時より高くする。**
#: こちらから話しかけて外すのは、聞かれて外すより気まずい。
PROACTIVE_CONFIDENCE = .55

#: 道具の結果として通してよい行動（Phase 7B）。
#:
#: **`ANSWER` も `COMMENT` も入れない。** 入れると、ツールが1つ動いた
#: というだけで通常回答が結果報告の経路から出せるようになる。ここを
#: 通ってよいのは「何が起きたかを伝える」5つだけ。
TOOL_OUTCOME_ALLOWLIST: frozenset[ActionType] = frozenset({
    ActionType.REPORT_TOOL_SUCCESS, ActionType.REPORT_TOOL_FAILURE,
    ActionType.REPORT_PARTIAL_RESULT, ActionType.REPORT_UNKNOWN_OUTCOME,
    ActionType.ASK_TOOL_RECOVERY_CONFIRMATION,
})
#: 「できたよ」と言ってよい行動。**検証を通っていないと出せない。**
TOOL_SUCCESS_ACTIONS: frozenset[ActionType] = frozenset({
    ActionType.REPORT_TOOL_SUCCESS,
})


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """1回の出力要求。**出どころの無い音声を許さないための型。**"""

    source_type: SpeechSource
    source_action: ActionType | None = None
    turn_id: str = ""
    bypass_reason: BypassReason = BypassReason.NONE
    priority: int = 0
    confidence: float = 1.0
    topic_id: str = ""
    created_at: float = field(default_factory=time.time)
    speech_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # -- 自発発話の検証材料 (Phase 5) ------------------------------------
    #
    # 入口で1度見るだけでは足りない。候補を選んでから音が出るまでに、
    # 相手が話し始めることも、期限が切れることもある。
    participant_ids: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    expires_at: float | None = None
    user_speaking: bool = False
    closure_suppressed: str = ""
    # -- 道具の結果の検証材料 (Phase 7B) ----------------------------------
    #
    # **どの実行の話か、誰へ、どこへ返すのか**を要求そのものが持つ。
    # 持たせないと、出口で「いま繋がっている方」へ流すしかなくなり、
    # Local の結果が Discord へ、別チャンネルの人のところへ出る。
    execution_id: str = ""
    tool_outcome_event_id: str = ""
    conversation_id: str = ""
    channel_id: str = ""
    outcome_verified: bool = False
    already_reported: bool = False

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.monotonic() if now is None else now) > self.expires_at

    def summary(self) -> dict[str, Any]:
        """ログ用。**本文は入れない**（第12条）。"""
        return {
            "speech_id": self.speech_id,
            "source": str(self.source_type),
            "action": str(self.source_action) if self.source_action else "",
            "bypass": str(self.bypass_reason),
            "turn_id": self.turn_id,
            "confidence": round(float(self.confidence), 2),
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    reason: str

    def snapshot(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason}


def cognition_active(
    mode: RolloutMode | str, *, enabled: bool, test_session: bool = False,
    profile_id: str = "", allowlist: tuple[str, ...] | list[str] = (),
) -> bool:
    """このターン、認知層を効かせるか。

    `enabled` が false なら何があっても効かせない——止め方が1つだと
    困った時に迷わない。
    """
    if not enabled:
        return False
    value = RolloutMode(str(mode)) if str(mode) in set(RolloutMode) else RolloutMode.DISABLED
    if value is RolloutMode.DISABLED:
        return False
    if value in {RolloutMode.ENABLED, RolloutMode.PRODUCTION}:
        return True
    if value in {RolloutMode.TEST_SESSION, RolloutMode.PRODUCTION_SESSION}:
        return bool(test_session)
    return str(profile_id) in {str(item) for item in allowlist}


def check_speech(
    request: SpeechRequest, *, cognition_enabled: bool,
    has_valid_decision: bool = False, decision_allows_speech: bool = False,
    active_conversation_id: str = "", active_channel_id: str = "",
) -> GateResult:
    """最終出口の判定。**出どころごとに条件が違う。**

    入口で1度見るだけでは足りない。fallback も古い非同期タスクも、
    最後はここを通る。
    """
    source = request.source_type

    if not cognition_enabled:
        # 従来動作。ここで塞ぐと、既定のまま使っている人が黙る。
        return GateResult(True, "cognition_disabled")

    if source is SpeechSource.COGNITIVE_DECISION:
        if not has_valid_decision:
            return GateResult(False, "no_decision_for_cognitive_speech")
        return GateResult(decision_allows_speech, "decision")

    if source is SpeechSource.GAME_WARNING_FAST_PATH:
        allowed = FAST_PATH_ALLOWLIST[source]
        if request.source_action not in allowed:
            # 通常回答が危険警告の経路から出るのを止める。
            return GateResult(False, "fast_path_action_not_allowlisted")
        if request.confidence < DANGER_CONFIDENCE:
            return GateResult(False, "danger_confidence_too_low")
        return GateResult(True, "warn_fast_path")

    if source in {SpeechSource.BACKCHANNEL, SpeechSource.SYSTEM_NOTIFICATION}:
        # 相槌は行動を伴わない短い反応だけ。回答や質問を始めさせない。
        if request.source_action is not None:
            return GateResult(False, "fast_path_action_not_allowlisted")
        return GateResult(True, str(source))

    if source is SpeechSource.PROACTIVE_OPPORTUNITY:
        # **自分から話す時こそ、条件を確かめる。**
        # 相手の番でも、期限切れでも、決定と食い違っていても通さない。
        if not has_valid_decision:
            return GateResult(False, "proactive_speech_without_decision")
        if not decision_allows_speech:
            return GateResult(False, "decision_does_not_speak")
        if request.source_action not in PROACTIVE_ALLOWLIST:
            # 通常回答を自発発話の出どころから出さない。
            return GateResult(False, "proactive_action_not_allowlisted")
        if request.expired():
            return GateResult(False, "opportunity_expired")
        if request.user_speaking:
            return GateResult(False, "user_is_speaking")
        if request.closure_suppressed:
            return GateResult(False, f"closure:{request.closure_suppressed}")
        if request.confidence < PROACTIVE_CONFIDENCE:
            return GateResult(False, "proactive_confidence_too_low")
        return GateResult(True, "proactive_opportunity")

    if source is SpeechSource.COGNITIVE_TOOL_OUTCOME:
        # **`ToolResult` から直接ここへ来られない。** 出来事へ変換され、
        # Action Selector を通り、決定と一致していないと出せない。
        if not request.execution_id:
            return GateResult(False, "tool_outcome_without_execution")
        if not request.tool_outcome_event_id:
            return GateResult(False, "tool_outcome_without_event")
        if not has_valid_decision:
            return GateResult(False, "tool_outcome_without_decision")
        if not decision_allows_speech:
            return GateResult(False, "decision_does_not_speak")
        if request.source_action not in TOOL_OUTCOME_ALLOWLIST:
            return GateResult(False, "tool_outcome_action_not_allowlisted")
        if request.already_reported:
            # **同じ実行の完了報告を二度出さない。**
            return GateResult(False, "tool_outcome_already_reported")
        if (request.source_action in TOOL_SUCCESS_ACTIONS
                and not request.outcome_verified):
            # **確かめていないのに「完了した」と言わない。**
            return GateResult(False, "tool_outcome_unverified_success")
        if request.expired():
            return GateResult(False, "tool_outcome_expired")
        if request.user_speaking:
            return GateResult(False, "user_is_speaking")
        if request.closure_suppressed:
            return GateResult(False, f"closure:{request.closure_suppressed}")
        # **頼まれた場所へ返す。** ここを見ないと、Local の結果が
        # Discord へ、別チャンネルの人のところへ出る。
        if active_conversation_id and request.conversation_id and (
                active_conversation_id != request.conversation_id):
            return GateResult(False, "tool_outcome_conversation_mismatch")
        if active_channel_id and request.channel_id and (
                active_channel_id != request.channel_id):
            return GateResult(False, "tool_outcome_channel_mismatch")
        return GateResult(True, "tool_outcome")

    if source is SpeechSource.SPONTANEOUS_SPEECH:
        return GateResult(True, "spontaneous_allowed")

    # LEGACY_PATH。**理由の無いものは通さない。**
    if request.bypass_reason in {
        BypassReason.LEGACY_COMPATIBILITY, BypassReason.LATENCY_CRITICAL,
    }:
        return GateResult(True, f"legacy_bypass:{request.bypass_reason}")
    return GateResult(False, "legacy_speech_without_reason")


def spontaneous_suppressed(
    *, last_status: str, last_topic_id: str, topic_id: str,
    seconds_since_turn: float, quiet_seconds: float = 20.0,
) -> str:
    """沈黙のあとに、同じ話題を自分から蒸し返さないための判定。

    時間だけで決めない。**同じ話題かどうか**を先に見る。時間だけだと、
    黙ると決めた直後に別件で話しかけるのまで止めてしまう。
    """
    if last_status != "silent_completed":
        return ""
    if topic_id and topic_id == last_topic_id:
        return "same_topic_after_silence"
    if seconds_since_turn < quiet_seconds:
        return "too_soon_after_silence"
    return ""


#: 終了シグナルへの短い了承に載せる制約。**通常回答へ膨らませない。**
BRIEF_ACK_CONSTRAINTS: dict[str, Any] = {
    "max_sentences": 1,
    "ask_follow_up": False,
    "continue_explanation": False,
    "introduce_new_topic": False,
    "humor": False,
    "resume_interrupted_response": False,
}


#: 場の読みが動かせる幅。**上書きにはしない。**
#: 安全警告や、相手の明示的な質問を押しのけない大きさに留める。
CLOSURE_BIAS_LIMIT = .15


def adaptive_closure_bias(state) -> tuple[float, tuple[str, ...]]:
    """その場の空気から、無言と短い了承のどちらへ寄せるか。

    正なら「わかった」、負なら無言。**どちらも正解**なので、決め打ちせず
    状況で傾きを作る。理由も返すのは、後から「なぜ黙ったのか」を読める
    ようにするため。

    分けている手がかり:

    * **話している途中で止められた** — 黙ると固まったように聞こえる。
      「わかった」の方が、聞こえていて止めたことが伝わる
    * **相手が苛立っている** — 無言は拗ねたようにも取れる。短く受ける
    * **まだ距離がある相手** — 沈黙は冷たさに読める。親しいほど無言でよい
    * **聞いている人がいる** — 配信や通話で無音が続くのは事故に見える
    * **「もういいよ、それで」型の同意** — 打ち切りではなく了承なので受ける
    * **こちらが何も話していない場面** — 相槌を返す相手がいない。無言でよい
    """
    reasons: list[str] = []
    bias = .0

    if state.interrupted_response:
        bias += .10
        reasons.append("was_mid_utterance")
    else:
        bias -= .04
        reasons.append("nothing_to_close")

    heat = max(state.irritation, state.user_frustration)
    if heat > .5:
        bias += .08
        reasons.append("user_is_irritated")

    # 親しいほど無言が許される。まだ距離がある相手だと冷たく聞こえる。
    bias += (.5 - state.comfort) * .12
    if state.comfort >= .7:
        reasons.append("comfortable_silence_is_fine")
    elif state.comfort <= .35:
        reasons.append("silence_may_read_as_cold")

    if state.is_group:
        bias += .08
        reasons.append("others_are_listening")

    if state.closure_is_affirmative:
        # 「もういいよ、それで」は打ち切りではなく同意。無視しない。
        bias += .12
        reasons.append("closure_is_agreement")

    limited = max(-CLOSURE_BIAS_LIMIT, min(CLOSURE_BIAS_LIMIT, bias))
    return round(limited, 4), tuple(reasons)


def closure_bias(
    policy: ClosurePolicy | str, action: ActionType, state=None,
) -> float:
    """終了シグナルの時だけ効く小さな加点。**上書きではない。**

    `adaptive` は場の空気から決める。明示的な方針を選んだ時は、そちらが
    優先される——聞き比べたい時に勝手に揺れては困る。
    """
    value = str(policy)
    if value == ClosurePolicy.SILENT_PREFERRED:
        return CLOSURE_BIAS_LIMIT if action is ActionType.REMAIN_SILENT else .0
    if value == ClosurePolicy.BRIEF_ACK_PREFERRED:
        return CLOSURE_BIAS_LIMIT if action is ActionType.BRIEF_ACKNOWLEDGE else .0
    if value != ClosurePolicy.ADAPTIVE or state is None:
        return .0
    signed, _reasons = adaptive_closure_bias(state)
    if action is ActionType.BRIEF_ACKNOWLEDGE:
        return signed
    if action is ActionType.REMAIN_SILENT:
        return -signed
    return .0
