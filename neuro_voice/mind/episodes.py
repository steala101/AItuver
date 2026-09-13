"""エピソード記憶の窓口。**保存先は既存の `MemoryStore` のまま。**

`cognition/episodic.py` が「覚えるか」を決め、`cognition/recall.py` が
「いつ思い出すか」と「思い出して何が変わるか」を決める。ここは、その2つと
SQLite を繋ぐだけの薄い層。

分けてあるのは、判断の側をDBなしでテストできるようにするため。判断とIOを
同じ関数に混ぜると、「なぜ覚えなかったか」を確かめるのにDBが要るようになる。

`Mind` から呼ばれる。機能フラグは3つとも**既定 false**——実機で確かめて
いない層を勝手に本番へ入れない。
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.episodic import (
    EpisodicMemory, MemoryCandidate, MemoryStatus, MemoryWriteGate, WriteDecision,
    WriteVerdict, propose_candidates, self_failure_candidate, to_memory,
)
from neuro_voice.cognition.recall import (
    MemoryInfluence, RecallQuery, ScoredMemory, build_query, influence_from,
    ObligationRetrievalGate, obligation_retrieval_gate, planner_payload, rank,
    retrieval_trigger,
)
from neuro_voice.cognition.reflection import (
    Reflection, ReflectionStatus, ReflectionTarget, derive_conversation_strategy,
    revise, should_reflect,
)
from neuro_voice.cognition.types import InformationType

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievalResult:
    """1ターンの想起の結果。**空でも正常**——探す理由が無ければ探さない。"""

    trigger: str = ""
    query: RecallQuery | None = None
    scored: tuple[ScoredMemory, ...] = ()
    influence: MemoryInfluence | None = None
    reflections: tuple[Reflection, ...] = ()
    latency_ms: dict[str, float] = field(default_factory=dict)
    # -- どの範囲で探したか (Phase 6F) ------------------------------------
    #
    # **記憶は移していない。** 人物単位で引いた時に、どの `speaker_id` を
    # 束ねたのかが読めないと、別人の過去が混ざっても気づけない。
    retrieval_mode: str = "legacy"
    scope_speaker_ids: tuple[str, ...] = ()
    legacy_result_count: int = 0
    person_result_count: int = 0
    deduplicated_count: int = 0
    excluded_revoked_ids: tuple[str, ...] = ()
    #: `shadow_read` の時だけ。legacy と人物単位で拾えた件数の差。
    scope_difference: int = 0
    #: 重複を落とした結果。**元のレコードは消していない。**
    dedup: Any = None
    obligation_gate: ObligationRetrievalGate = field(default_factory=ObligationRetrievalGate)

    @property
    def empty(self) -> bool:
        return not self.scored and not self.reflections

    def scope_snapshot(self) -> dict[str, Any]:
        """診断・Trace用。**本文もIDの中身も入れない**（第12条）。"""
        return {
            "mode": self.retrieval_mode,
            "speakers": list(self.scope_speaker_ids[:6]),
            "legacy_results": self.legacy_result_count,
            "person_results": self.person_result_count,
            "deduplicated": self.deduplicated_count,
            "excluded": list(self.excluded_revoked_ids[:4]),
            "difference": self.scope_difference,
            "dedup": self.dedup.snapshot() if self.dedup is not None else {},
        }

    def planner_memories(self) -> list[dict[str, Any]]:
        """Planner へ渡す `relevant_memories`。**数件の構造だけ。**"""
        items = planner_payload(self.scored)
        for reflection in self.reflections:
            items.append({
                "memory_id": reflection.reflection_id,
                "type": str(InformationType.REFLECTION).upper(),
                "summary": reflection.statement[:90],
                "confidence": round(float(reflection.confidence), 2),
                "relevance": "conversation_strategy",
            })
        return items[:4]


def _row_to_memory(row: dict[str, Any]) -> EpisodicMemory:
    meta: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        meta = json.loads(row.get("meta") or "{}") or {}
    return EpisodicMemory(
        memory_id=int(row.get("id", 0)),
        summary=str(row.get("text", "")),
        information_type=str(row.get("information_type") or InformationType.OBSERVATION),
        event_type=str(row.get("event_type") or "conversation"),
        occurred_at=float(row.get("occurred_at") or row.get("created_at") or 0.0),
        created_at=float(row.get("created_at") or 0.0),
        session_id=str(meta.get("session_id", "")),
        participant_ids=tuple(meta.get("participant_ids", ()) or ()),
        topic_ids=tuple(meta.get("topic_ids", ()) or ()),
        source_event_ids=tuple(meta.get("source_event_ids", ()) or ()),
        confidence=float(row.get("confidence") or .5),
        # 旧レコードの importance は 1〜5 の整数。0〜1へ揃える。
        importance=float(meta.get("importance", (float(row.get("importance") or 3)) / 5.0)),
        emotional_salience=float(meta.get("emotional_salience", 0.0)),
        relationship_salience=float(meta.get("relationship_salience", 0.0)),
        goal_relevance=float(meta.get("goal_relevance", 0.0)),
        retrieval_keys=tuple(meta.get("topic_ids", ()) or ()),
        status=str(row.get("status") or MemoryStatus.ACTIVE),
        superseded_by=int(row.get("superseded_by") or 0),
        support_count=int(row.get("support_count") or 1),
        contradiction_count=int(row.get("contradiction_count") or 0),
        access_count=int(row.get("access_count") or 0),
    )


def _reflection_from_row(row: dict[str, Any]) -> Reflection:
    return Reflection(
        statement=str(row.get("statement", "")),
        target_type=str(row.get("target_type") or ReflectionTarget.CONVERSATION_STRATEGY),
        source_memory_ids=tuple(int(x) for x in (row.get("source_memory_ids") or [])),
        support_count=int(row.get("support_count") or 0),
        contradiction_count=int(row.get("contradiction_count") or 0),
        confidence=float(row.get("confidence") or .3),
        action_deltas=dict(row.get("action_deltas") or {}),
        status=str(row.get("status") or ReflectionStatus.ACTIVE),
        created_at=float(row.get("created_at") or time.time()),
        last_verified_at=float(row.get("last_verified_at") or time.time()),
        reflection_id=str(row.get("reflection_id", "")),
    )


class EpisodicMemoryService:
    """覚える・思い出す・仮説を作る。**3つとも個別に止められる。**"""

    def __init__(self, store, cfg=None) -> None:
        self._store = store
        get = getattr(cfg, "get", None) or (lambda _key, default=None: default)
        self.episodic_enabled = bool(get("memory.episodic_enabled", False))
        # Independent retrieval-only gate for smoke tests.  Do not overload
        # episodic_enabled because older callers use it to gate retrieval too.
        self.write_enabled = bool(get("memory.write_enabled", True))
        self.retrieval_enabled = bool(get("memory.retrieval_enabled", False))
        self.reflection_enabled = bool(get("memory.reflection_enabled", False))
        self._max_retrieved = max(1, int(get("memory.max_retrieved", 3)))
        self._scan_limit = max(20, int(get("memory.scan_limit", 200)))
        self._gate = MemoryWriteGate(
            min_value=float(get("memory.min_value", .45)),
            duplicate_similarity=float(get("memory.duplicate_similarity", .78)),
            hypothesis_confidence=float(get("memory.hypothesis_confidence", .60)),
            storage_budget=int(get("mind.max_memories", 2000)),
        )
        self._reflection_min_episodes = max(3, int(get("memory.reflection_min_episodes", 8)))
        #: 直近で観測した重要エピソードと失敗の数。Reflection の実行条件に使う。
        self._since_reflection = 0
        self._failures_since_reflection = 0
        self._corrections_since_reflection = 0
        self._last_verdicts: list[WriteVerdict] = []
        self._last_candidates: list[MemoryCandidate] = []

    # ------------------------------------------------------------------
    # 覚える
    # ------------------------------------------------------------------

    def _existing(
        self,
        speaker_key: str = "",
        *,
        query_text: str = "",
        include_archived: bool = False,
    ) -> list[EpisodicMemory]:
        # **検索の時点で人格を絞る**（Phase 7C）。取ってからプロンプトで
        # 外す形にすると、`LIMIT` を別人格の記憶が先に埋めて、自分の
        # 記憶が1件も出てこないのに「漏れてはいない」状態になる。
        #
        # 人格が結ばれていない時は**引数を足さない**。ここで常に渡すと、
        # 古い呼び出し規約の Store が `TypeError` になり、それが
        # `suppress` に飲まれて**検索が静かに0件になる**（実際に踏んだ）。
        scoped: dict[str, Any] = {}
        if self.persona_id:
            scoped = {"persona_id": self.persona_id,
                      "allow_legacy_unscoped": self.allow_legacy_unscoped}
        with contextlib.suppress(Exception):
            if query_text and hasattr(self._store, "search_episodes"):
                return [
                    _row_to_memory(row)
                    for row in self._store.search_episodes(
                        query_text,
                        limit=self._scan_limit,
                        speaker_key=speaker_key,
                        include_archived=include_archived,
                        **scoped,
                    )
                ]
            return [
                _row_to_memory(row)
                for row in self._store.episodes(
                    limit=self._scan_limit, speaker_key=speaker_key, **scoped,
                )
            ]
        return []

    @property
    def persona_id(self) -> str:
        """いま誰の記憶を引くか。空なら従来どおり（絞らない）。"""
        return getattr(self, "_persona_id", "")

    @property
    def allow_legacy_unscoped(self) -> bool:
        """所有者不明の古い記憶を出すか。**既定は出さない。**"""
        return bool(getattr(self, "_allow_legacy_unscoped", False))

    def bind_persona(self, persona_id: str, *,
                     allow_legacy_unscoped: bool = False) -> None:
        self._persona_id = str(persona_id or "")
        self._allow_legacy_unscoped = bool(allow_legacy_unscoped)

    def _across(
        self,
        keys,
        *,
        query_text: str = "",
        include_archived: bool = False,
    ) -> tuple[list[EpisodicMemory], int]:
        """複数の `speaker_id` をまたいで集める。**同じ記憶を重ねない。**

        人物に複数の声紋が紐づいていても、**検索の量を人数倍にしない。**
        1つあたりの読み込み上限を割り、集めた後で `memory_id` で重複を
        落とす。ここで絞らないと、ランキングへ渡る前に予算を使い切る。
        """
        keys = [item for item in dict.fromkeys(keys or ()) if item]
        if not keys:
            return (self._existing(
                "", query_text=query_text, include_archived=include_archived,
            ), 0)
        if len(keys) == 1:
            return (self._existing(
                keys[0], query_text=query_text, include_archived=include_archived,
            ), 0)
        share = max(8, self._scan_limit // len(keys))
        seen: dict[int, EpisodicMemory] = {}
        duplicates = 0
        for key in keys:
            with contextlib.suppress(Exception):
                if query_text and hasattr(self._store, "search_episodes"):
                    rows = self._store.search_episodes(
                        query_text,
                        limit=share,
                        speaker_key=key,
                        include_archived=include_archived,
                        persona_id=self.persona_id,
                        allow_legacy_unscoped=self.allow_legacy_unscoped,
                    )
                else:
                    rows = self._store.episodes(limit=share, speaker_key=key)
                for row in rows:
                    memory = _row_to_memory(row)
                    if memory.memory_id in seen:
                        duplicates += 1
                        continue
                    seen[memory.memory_id] = memory
        return (list(seen.values())[:self._scan_limit], duplicates)

    def commit(self, candidates: list[MemoryCandidate]) -> list[WriteVerdict]:
        """候補をゲートへ通して、通ったものだけ書く。

        **入口はここだけ。** 規則から来た候補も、内省LLMから来た候補も、
        同じゲートを通る。別経路を作ると重複と訂正の保証が消える。
        """
        if not candidates or not self.episodic_enabled:
            # Trace is per turn.  Never carry a previous turn's verdict into
            # a no-candidate or disabled turn.
            self._last_candidates = list(candidates)
            self._last_verdicts = []
            return []
        if not self.write_enabled:
            # Do not expose stale decisions from an earlier turn as this
            # turn's outcome, and do not enter any Store mutation path.
            self._last_candidates = list(candidates)
            self._last_verdicts = []
            return []
        # **既存の読み出しは1回だけ。** 候補ごとに読み直すと、1ターンの
        # 書き込みでDBを何度も走査することになる。同じ発話から出た候補どうしの
        # 重複も、書いたものをこの一覧へ足していけば見える。
        speaker = ""
        for candidate in candidates:
            if candidate.participant_ids:
                speaker = candidate.participant_ids[0]
                break
        existing = self._existing(speaker)
        stored_count = len(existing)
        verdicts: list[WriteVerdict] = []
        for candidate in candidates:
            try:
                verdict = self._gate.evaluate(
                    candidate, existing, stored_count=stored_count,
                )
                new_id = self._apply(candidate, verdict)
                if verdict.writes:
                    written = to_memory(candidate, verdict)
                    written.memory_id = int(new_id)
                    existing.append(written)
            except Exception:
                logger.exception("エピソードの保存判断でエラー（今回は覚えない）")
                continue
            verdicts.append(verdict)
            # **REINFORCE も1回の観測として数える。** 以前は新しい行が
            # できた時しか数えておらず、同じ失敗を繰り返すという
            # **いちばんよくある形**が Reflection の閾値へ永久に届かなかった。
            if verdict.decision is not WriteDecision.SKIP:
                self._since_reflection += 1
                if str(candidate.event_type).startswith("self_failure:"):
                    self._failures_since_reflection += 1
                elif str(candidate.event_type) == "correction":
                    self._corrections_since_reflection += 1
        self._last_verdicts = verdicts
        self._last_candidates = list(candidates)
        return verdicts

    def _apply(self, candidate: MemoryCandidate, verdict: WriteVerdict) -> int:
        """判断どおりにDBを触る。**物理削除はしない。**"""
        decision = verdict.decision
        if decision is WriteDecision.SKIP:
            return 0
        if decision is WriteDecision.REINFORCE:
            self._store.reinforce_episode(
                verdict.target_memory_id, confidence=verdict.confidence,
            )
            return verdict.target_memory_id
        if decision is WriteDecision.UPDATE:
            self._store.upgrade_episode(
                verdict.target_memory_id,
                information_type=str(candidate.information_type),
                confidence=verdict.confidence,
            )
            return verdict.target_memory_id
        memory = to_memory(candidate, verdict)
        new_id = self._write(memory, candidate)
        if decision in {WriteDecision.SUPERSEDE, WriteDecision.CONTRADICT} and verdict.target_memory_id:
            # **古い方を消さない。** 印を付けて、新しい方へ繋ぐ。
            self._store.mark_episode(
                verdict.target_memory_id,
                MemoryStatus.SUPERSEDED if decision is WriteDecision.SUPERSEDE
                else MemoryStatus.CONTRADICTED,
                superseded_by=new_id, count_contradiction=True,
            )
        return new_id

    def _write(self, memory: EpisodicMemory, candidate: MemoryCandidate) -> int:
        # **持ち主を書き込みの時点で付ける**（Phase 7D ⑤）。
        # Phase 7C は読み側だけだったので、ここが空のまま溜まっていた。
        # 空のまま溜めると、分離を有効にした瞬間に全部隠れる。
        from neuro_voice.cognition.persona_scope import PersonaScope

        return int(self._store.add_episode(
            memory.summary,
            persona_id=self.persona_id,
            scope=str(PersonaScope.PERSONA_PRIVATE),
            kind="preference" if candidate.event_type == "preference" else "episode",
            # 旧レコードと同じ 1〜5 の尺度へ戻して入れる（既存の読み手のため）。
            importance=max(1, min(5, round(float(memory.importance) * 5))),
            information_type=str(memory.information_type),
            event_type=str(memory.event_type),
            confidence=float(memory.confidence),
            status=str(memory.status),
            speaker_key=memory.participant_ids[0] if memory.participant_ids else "",
            occurred_at=memory.occurred_at,
            meta={
                "session_id": memory.session_id,
                "participant_ids": list(memory.participant_ids),
                "topic_ids": list(memory.topic_ids),
                "source_event_ids": list(memory.source_event_ids),
                "origin_turn_id": str(candidate.provenance.get("origin_turn_id", "")),
                "source_role": str(candidate.provenance.get("source_role", "")),
                "source_person_id": str(candidate.provenance.get("source_person_id", "")),
                "persona_id": str(candidate.provenance.get("persona_id", "")),
                "persona_epoch": int(candidate.provenance.get("persona_epoch", 0) or 0),
                "importance": round(float(memory.importance), 3),
                "emotional_salience": round(float(memory.emotional_salience), 3),
                "relationship_salience": round(float(memory.relationship_salience), 3),
                "goal_relevance": round(float(memory.goal_relevance), 3),
            },
        ))

    def observe_turn(
        self, user_text: str, reply: str = "", *, speaker_key: str = "",
        session_id: str = "", event_ids: tuple[str, ...] = (),
        privacy_level: str = "normal", input_confidence: float = 1.0,
        provenance: dict[str, Any] | None = None,
    ) -> list[WriteVerdict]:
        """1ターンから候補を作り、ゲートへ通す。**何も残らないのが普通。**"""
        if not self.episodic_enabled:
            return []
        candidates = propose_candidates(
            user_text, reply, speaker_key=speaker_key, session_id=session_id,
            event_ids=event_ids, privacy_level=privacy_level,
            input_confidence=input_confidence, provenance=provenance,
        )
        if not candidates:
            return []
        return self.commit(candidates)

    def observe_self_failure(
        self, pattern: str, *, detail: str = "", speaker_key: str = "",
        session_id: str = "",
    ) -> list[WriteVerdict]:
        """**自分の失敗を覚える。** これが無いと同じ失敗を繰り返す。"""
        if not self.episodic_enabled or not pattern:
            return []
        return self.commit([self_failure_candidate(
            pattern, detail=detail, speaker_key=speaker_key, session_id=session_id,
        )])

    # ------------------------------------------------------------------
    # 思い出す
    # ------------------------------------------------------------------

    def retrieve(
        self, user_text: str, state: Any = None, *, action: Any = None,
        speaker_key: str = "", scope=None, scope_mode: str = "legacy",
    ) -> RetrievalResult:
        """**探す理由がある時だけ探す。** 挨拶で長期記憶を引かない。

        `scope` を渡すと、その人物に紐づく `speaker_id` をまたいで探す
        （Phase 6F）。**過去のレコードは書き換えない**——検索側で束ねるだけ。

        `scope_mode`:

        * `legacy` — 今の `speaker_id` だけ（従来どおり）
        * `shadow` — 正式な結果は legacy。人物単位との**差だけ測る**
        * `person` — 人物単位を正式採用。解決できなければ legacy へ落ちる
        """
        if not self.retrieval_enabled:
            return RetrievalResult()
        started = time.perf_counter()
        obligation_gate = obligation_retrieval_gate(user_text, state, action=action)
        trigger = retrieval_trigger(user_text, state, action=action)
        trigger_ms = (time.perf_counter() - started) * 1000
        if not trigger:
            return RetrievalResult(
                obligation_gate=obligation_gate,
                latency_ms={"memory_trigger_check_ms": round(trigger_ms, 2)},
            )
        try:
            patterns = _failure_patterns_for(trigger, state)
            query = build_query(
                user_text, state, trigger=trigger, participant=speaker_key,
                failure_patterns=patterns,
            )
            mark = time.perf_counter()
            include_archived = query.trigger in {
                "past_reference", "labelled_recall", "action_retrieve_memory",
            }
            keys = tuple(getattr(scope, "search_keys", ()) or ())
            usable_scope = bool(scope is not None and getattr(scope, "resolved", False)
                                and len(keys) > 1)
            duplicates = 0
            person_count = 0
            if usable_scope and scope_mode == "person":
                # **legacy を別に読まない。** 人物単位の結果に含まれている。
                # 二重に読むと、束ねた意味が無いうえに予算を倍使う。
                memories, duplicates = self._across(
                    keys, query_text=query.text, include_archived=include_archived,
                )
                person_count = len(memories)
                legacy_memories = [item for item in memories
                                   if speaker_key in item.participant_ids]
                mode = "person"
            elif usable_scope and scope_mode == "shadow":
                legacy_memories = self._existing(
                    speaker_key, query_text=query.text, include_archived=include_archived,
                )
                widened, duplicates = self._across(
                    keys, query_text=query.text, include_archived=include_archived,
                )
                person_count = len(widened)
                # **測るだけ。** 正式な結果は legacy のまま。
                memories, mode = legacy_memories, "shadow"
            else:
                legacy_memories = self._existing(
                    speaker_key, query_text=query.text, include_archived=include_archived,
                )
                memories, mode = legacy_memories, "legacy"
            fetch_ms = (time.perf_counter() - mark) * 1000

            mark = time.perf_counter()
            # **同じ話を2回渡さない。** レコードは消さず、ここで絞るだけ。
            #
            # 人物単位で引くと、Discord で覚えたことと Local で覚えたことが
            # 両方返る。中身が同じでも `memory_id` が違えば別の記憶なので、
            # そのまま渡すと同じ話を2回聞かせることになる。
            from neuro_voice.cognition.dedup import deduplicate

            unique = deduplicate(memories)
            # **人数が増えても返す件数は増やさない。** 予算は同じ。
            scored = rank(unique.memories, query, limit=self._max_retrieved)
            rank_ms = (time.perf_counter() - mark) * 1000

            reflections = tuple(self.active_reflections()) if self.reflection_enabled else ()
            influence = influence_from(scored, reflections)
            if scored and self.write_enabled:
                with contextlib.suppress(Exception):
                    self._store.touch([item.memory.memory_id for item in scored])
            return RetrievalResult(
                trigger=trigger, query=query, scored=tuple(scored),
                influence=influence, reflections=reflections,
                latency_ms={
                    "memory_trigger_check_ms": round(trigger_ms, 2),
                    "memory_retrieval_ms": round(fetch_ms, 2),
                    "memory_ranking_ms": round(rank_ms, 2),
                },
                retrieval_mode=mode,
                scope_speaker_ids=(keys if usable_scope else
                                   ((speaker_key,) if speaker_key else ())),
                legacy_result_count=len(legacy_memories),
                person_result_count=person_count,
                deduplicated_count=duplicates,
                excluded_revoked_ids=tuple(
                    getattr(scope, "excluded_revoked_ids", ()) or ()),
                scope_difference=(person_count - len(legacy_memories)
                                  if usable_scope else 0),
                dedup=unique,
                obligation_gate=obligation_gate,
            )
        except Exception:
            # 想起で落ちても会話は続ける（第17条）。記憶が無いのと同じ動きになる。
            logger.exception("記憶の想起でエラー（今回は使わない）")
            return RetrievalResult(trigger=trigger, obligation_gate=obligation_gate)

    # ------------------------------------------------------------------
    # 仮説
    # ------------------------------------------------------------------

    def active_reflections(self) -> list[Reflection]:
        if not self.reflection_enabled:
            return []
        with contextlib.suppress(Exception):
            return [
                _reflection_from_row(row)
                for row in self._store.reflections(
                    target_type=str(ReflectionTarget.CONVERSATION_STRATEGY),
                )
            ]
        return []

    def reflection_due(
        self, *, session_ended: bool = False, idle_seconds: float = .0,
        explicit_request: bool = False,
    ) -> str:
        if not self.reflection_enabled:
            return ""
        return should_reflect(
            important_episodes=self._since_reflection,
            repeated_failures=self._failures_since_reflection,
            explicit_corrections=self._corrections_since_reflection,
            session_ended=session_ended, idle_seconds=idle_seconds,
            explicit_request=explicit_request,
            min_episodes=self._reflection_min_episodes,
        )

    def consolidate(self) -> Reflection | None:
        """複数の経験から `CONVERSATION_STRATEGY` を1つ作る。

        **リアルタイム応答の待ち時間へ入れない。** 呼ぶのは会話が
        止まっている間だけ（`Mind._reflect` と同じ扱い）。ここで作った
        仮説は**次のターン以降**にしか効かない。
        """
        if not self.reflection_enabled:
            return None
        try:
            episodes = self._existing()
            reflection = derive_conversation_strategy(episodes)
            self._since_reflection = 0
            self._failures_since_reflection = 0
            self._corrections_since_reflection = 0
            if reflection is None:
                return None
            self._store.save_reflection(_reflection_row(reflection))
            logger.info("会話戦略の仮説: %s", reflection.snapshot())
            return reflection
        except Exception:
            logger.exception("Reflection の生成でエラー")
            return None

    def challenge_reflections(self, statement: str) -> list[Reflection]:
        """明示的な発言で仮説を検算する。**本人の発言が常に強い。**

        反証されたら確信を下げ、下がりきったら使うのをやめる。
        レコードは残す——「一度そう考えて外した」ことも記録として意味がある。
        """
        if not self.reflection_enabled or not str(statement or "").strip():
            return []
        changed: list[Reflection] = []
        for reflection in self.active_reflections():
            before = float(reflection.confidence)
            revise(reflection, statement, is_user_statement=True)
            if float(reflection.confidence) != before:
                with contextlib.suppress(Exception):
                    self._store.save_reflection(_reflection_row(reflection))
                changed.append(reflection)
        return changed

    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """状態表示用。**本文は入れない**（第12条）。"""
        return {
            "episodic_enabled": self.episodic_enabled,
            "write_enabled": self.write_enabled,
            "retrieval_enabled": self.retrieval_enabled,
            "reflection_enabled": self.reflection_enabled,
            "episodes_since_reflection": self._since_reflection,
            "failures_since_reflection": self._failures_since_reflection,
            "last_write_decisions": [v.snapshot() for v in self._last_verdicts[-4:]],
            "reflections": [r.snapshot() for r in self.active_reflections()],
        }


def _reflection_row(reflection: Reflection) -> dict[str, Any]:
    return {
        "reflection_id": reflection.reflection_id,
        "target_type": str(reflection.target_type),
        "statement": reflection.statement,
        "source_memory_ids": list(reflection.source_memory_ids),
        "support_count": reflection.support_count,
        "contradiction_count": reflection.contradiction_count,
        "confidence": reflection.confidence,
        "action_deltas": dict(reflection.action_deltas),
        "status": str(reflection.status),
        "created_at": reflection.created_at,
    }


def _failure_patterns_for(trigger: str, state: Any) -> tuple[str, ...]:
    """いまの状況に似た過去の失敗を、パターン名で引く。

    文字列の似方では引けない——「もういいよ」と
    「終了の合図の後に説明を続けた」は1文字も重ならない。

    **引き金の名前ではなく状態から決める。** 以前は `trigger` だけを見ていた
    ため、未完了の義務が残っているだけで引き金が `unresolved_obligation` に
    なり、終了の合図が出ていても過去の失敗を1件も引けなかった
    （Phase 4 の preflight で発覚）。引き金は「なぜ探すか」、パターンは
    「何を探すか」で、別のもの。
    """
    patterns: list[str] = []
    if state is not None:
        if float(getattr(state, "end_signal", 0.0) or 0.0) > .5:
            patterns += ["kept_talking_after_end_signal", "repeated_the_same_explanation"]
        if float(getattr(state, "user_frustration", 0.0) or 0.0) > .5:
            patterns += ["asked_a_question_already_answered", "joked_at_a_bad_moment"]
        if float(getattr(state, "input_confidence", 1.0) or 1.0) < .55:
            patterns.append("answered_on_low_confidence")
    if not patterns and trigger == "similar_to_past_failure":
        patterns += ["kept_talking_after_end_signal", "repeated_the_same_explanation"]
    if not patterns and trigger == "relationship_event":
        patterns += ["asked_a_question_already_answered", "joked_at_a_bad_moment"]
    return tuple(dict.fromkeys(patterns))
