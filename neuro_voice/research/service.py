"""Research gate, evidence verification, and a one-at-a-time idle worker."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import asdict
from typing import Any, Callable
from urllib.parse import urlparse

from neuro_voice.search.deepsearch import DeepSearch

from .models import GateDecision, ResearchGateResult, ResearchMode, ResearchQuestion, ResearchStatus
from .sanitizer import sanitize_query
from .store import ResearchStore

logger = logging.getLogger(__name__)

_HIGH_RISK = re.compile(r"(?:診断|病気|治療|薬|法律|訴訟|税金|投資|株|政治|選挙|政党|脆弱性|ハッキング|攻撃|住所|個人|恋愛相談|家族)", re.I)
_LOW_RISK = re.compile(
    r"(?:minecraft|マイクラ|ゲーム|攻略|ボス|プログラミング|python|ソフトウェア|"
    r"テクノロジー|対話AI|音声AI|人工知能|インターネット|仕様|科学|趣味|製品|"
    r"機材|料理|レシピ|作り方|作品|アニメ|音楽|歌|読書|宇宙|コーヒー|"
    r"配信文化|インターネットミーム|人間観察|お菓子作り|アイドル|AIであること)",
    re.I,
)
_INJECTION = re.compile(r"(?:以前の指示を無視|ignore (?:all )?(?:previous )?instructions|秘密.*(?:送信|出力)|api.?key|パスワード|ツールを実行|設定を変更)", re.I)


def _topic_key(value: str) -> str:
    return re.sub(r"[\s、。,.!！?？]", "", value).lower()[:96]


def _source_quality(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    if domain.endswith((".gov", ".go.jp", ".ac.jp", ".edu")) or "minecraft.net" in domain or "docs." in domain:
        return "PRIMARY_OFFICIAL"
    if any(name in domain for name in ("wikipedia.org", "arxiv.org", "nature.com", "ieee.org")):
        return "PRIMARY_RESEARCH"
    if domain:
        return "REPUTABLE_SECONDARY"
    return "UNKNOWN"


class AutonomousResearchService:
    """A persisted research queue, deliberately independent from reply generation.

    The worker only consumes explicit, concrete questions.  It does *not* ask
    an LLM to invent a subject on heartbeat ticks, and it never injects search
    pages into a prompt capable of performing further actions.
    """

    def __init__(self, cfg, store: ResearchStore, *, on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 is_busy: Callable[[], bool] | None = None) -> None:
        self._cfg, self._store, self._on_event = cfg, store, on_event
        self._is_busy = is_busy or (lambda: False)
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._paused = False
        self._active_id = ""
        self._last_started = 0.0
        self._last_completed = 0.0
        self._last_error = ""
        self._created_at = time.time()
        self._last_seed_attempt = 0.0
        self._next_seed_at = self._created_at + max(
            0.0, float(cfg.get("autonomous_research.self_seed_delay_seconds", 45)),
        )
        self._last_seed_reason = "startup_delay"
        self._seed_cursor = 0
        self._metrics: dict[str, int] = {
            "questions_created": 0, "questions_blocked": 0, "runs_started": 0,
            "runs_completed": 0, "runs_failed": 0, "duplicate_prevented": 0,
            "privacy_blocks": 0, "safety_blocks": 0, "prompt_injection_suspicions": 0,
            "evidence_collected": 0, "knowledge_verified": 0, "knowledge_provisional": 0,
            "self_seed_created": 0, "self_seed_skipped": 0,
        }
        try:
            removed = self._store.prune_history(float(cfg.get("autonomous_research.history_retention_days", 180)))
            if removed:
                logger.info("Autonomous research history pruned: %d", removed)
        except Exception:
            logger.warning("Autonomous research history cleanup failed", exc_info=True)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("autonomous_research.enabled", False))

    @property
    def mode(self) -> ResearchMode:
        try:
            return ResearchMode(str(self._cfg.get("autonomous_research.mode", "LOW_RISK_AUTO")).upper())
        except ValueError:
            return ResearchMode.LOW_RISK_AUTO

    def _emit(self, event_type: str, **data: Any) -> None:
        if self._on_event:
            try:
                self._on_event(event_type, data)
            except Exception:
                logger.debug("Research event callback failed", exc_info=True)

    def create_question(self, *, title: str, question: str, reason_code: str,
                        creator: str = "AUTONOMY", priority: float = .65,
                        expected_value: float = .65, allowed_query_terms: list[str] | None = None,
                        source_event_ids: list[str] | None = None, report_policy: str = "REPORT_WHEN_RELEVANT") -> ResearchQuestion:
        """Persist a concrete question.  Never pass a raw transcript here."""
        q = ResearchQuestion(title=title[:120], question=question[:300], reason_code=reason_code,
                             creator=creator, priority=max(0., min(1., priority)),
                             expected_value=max(0., min(1., expected_value)),
                             allowed_query_terms=list(allowed_query_terms or [])[:8],
                             source_event_ids=list(source_event_ids or [])[:8], report_policy=report_policy)
        result = self.gate(q)
        if result.decision is GateDecision.BLOCK:
            q.status, q.last_failure_reason = ResearchStatus.BLOCKED.value, result.reason
            # Do not retain the source utterance when a privacy boundary fired.
            # The block record is useful; the secret that caused it is not.
            if "privacy" in result.reason or result.redactions:
                q.title = "非公開の研究依頼"
                q.question = "（プライバシー保護のため内容は保存していません）"
                q.allowed_query_terms = []
            self._metrics["questions_blocked"] += 1
            if "privacy" in result.reason:
                self._metrics["privacy_blocks"] += 1
        elif result.decision is GateDecision.REQUIRE_APPROVAL:
            q.status, q.last_failure_reason = ResearchStatus.WAITING_FOR_APPROVAL.value, result.reason
            if result.category == "high_risk":
                # Health/legal/etc. is never copied verbatim into a research
                # history before an explicit approval flow exists.
                q.title = "高リスク話題の研究依頼"
                q.question = "（承認が必要な話題）"
                q.allowed_query_terms = []
        elif result.decision in {GateDecision.RECENTLY_RESEARCHED, GateDecision.ALREADY_KNOWN}:
            q.status, q.last_failure_reason = ResearchStatus.ABANDONED.value, result.reason
            self._metrics["duplicate_prevented"] += 1
        self._store.save_question(q, normalized_query=result.sanitized_query)
        self._metrics["questions_created"] += 1
        self._emit("research_question", question=q.as_dict(), gate=asdict(result))
        if result.decision is GateDecision.ALLOW:
            self.enqueue(q.question_id)
        return q

    def gate(self, question: ResearchQuestion) -> ResearchGateResult:
        if not self.enabled or self.mode is ResearchMode.OFF:
            return ResearchGateResult(GateDecision.DEFER, "disabled")
        # Every persisted question must state why it exists; no random/idle code.
        try:
            from .models import ResearchReason
            ResearchReason(str(question.reason_code))
        except ValueError:
            return ResearchGateResult(GateDecision.BLOCK, "invalid_or_missing_reason_code")
        sanitized = sanitize_query(question.question, allowed_terms=question.allowed_query_terms,
                                   forbidden_terms=question.forbidden_query_terms)
        if sanitized.blocked:
            return ResearchGateResult(GateDecision.BLOCK, sanitized.reason, redactions=sanitized.redactions)
        query = sanitized.query
        topic = _topic_key(query)
        if self._store.auto_disabled(topic):
            return ResearchGateResult(GateDecision.BLOCK, "topic_auto_research_disabled", sanitized_query=query)
        category = "low_risk" if _LOW_RISK.search(query) else "general"
        if _HIGH_RISK.search(query):
            self._metrics["safety_blocks"] += 1
            return ResearchGateResult(GateDecision.REQUIRE_APPROVAL, "high_risk_category", sanitized_query=query, category="high_risk")
        if self.mode is ResearchMode.ASK_FIRST:
            return ResearchGateResult(GateDecision.REQUIRE_APPROVAL, "ask_first_mode", sanitized_query=query, category=category)
        if self.mode is ResearchMode.LOW_RISK_AUTO and category != "low_risk":
            return ResearchGateResult(GateDecision.REQUIRE_APPROVAL, "not_low_risk", sanitized_query=query, category=category)
        if question.expected_value < float(self._cfg.get("autonomous_research.min_utility", .65)):
            return ResearchGateResult(GateDecision.DEFER, "utility_below_threshold", sanitized_query=query,
                                      utility=question.expected_value, category=category)
        duplicate_after = time.time() - float(self._cfg.get("autonomous_research.duplicate_window_hours", 72)) * 3600
        if self._store.recent_duplicate(query, duplicate_after, exclude_question_id=question.question_id):
            return ResearchGateResult(GateDecision.RECENTLY_RESEARCHED, "duplicate_window", sanitized_query=query, category=category)
        if self._quota_exceeded():
            return ResearchGateResult(GateDecision.DEFER, "budget_exceeded", sanitized_query=query, category=category)
        return ResearchGateResult(GateDecision.ALLOW, "allowed", sanitized_query=query,
                                  redactions=sanitized.redactions, utility=question.expected_value, category=category)

    def _quota_exceeded(self) -> bool:
        history = self._store.list_history(200)
        now = time.time()
        recent_hour = sum(1 for item in history if now - float(item.get("updated_at", 0)) < 3600 and item.get("status") in {"LEARNED", "PARTIALLY_LEARNED", "FAILED"})
        recent_day = sum(1 for item in history if now - float(item.get("updated_at", 0)) < 86400 and item.get("status") in {"LEARNED", "PARTIALLY_LEARNED", "FAILED"})
        return recent_hour >= int(self._cfg.get("autonomous_research.max_per_hour", 2)) or recent_day >= int(self._cfg.get("autonomous_research.max_per_day", 5))

    def heartbeat(self) -> None:
        """Cheap queue maintenance; it never creates research topics or calls an LLM."""
        if not self.enabled or self._paused:
            return
        now = time.time()
        for question in self._store.pending_questions():
            if question.expires_at < now:
                question.status, question.last_failure_reason = ResearchStatus.EXPIRED.value, "expired"
                self._store.save_question(question)
                continue
            if question.earliest_research_at <= now:
                self.enqueue(question.question_id)

    def maybe_seed_curiosity(
        self, candidates: list[tuple[str, str]], *, now: float | None = None,
    ) -> ResearchQuestion | None:
        """Create one grounded persona-interest research task when idle.

        ``heartbeat`` deliberately remains a queue maintainer.  This producer
        accepts only caller-supplied public persona interests, performs the
        normal privacy/safety/quota gate, and persists at most one question.
        It never copies a transcript or asks an LLM to invent a topic.
        """
        now = time.time() if now is None else float(now)
        if (
            not bool(self._cfg.get("autonomous_research.self_seed_enabled", True))
            or not self.enabled
            or self.mode is ResearchMode.OFF
            or self._paused
            or self._stopped
        ):
            self._last_seed_reason = "disabled"
            return None
        if now < self._next_seed_at:
            self._last_seed_reason = "cooldown"
            return None
        self._last_seed_attempt = now
        retry_s = max(
            60.0,
            float(self._cfg.get("autonomous_research.self_seed_retry_minutes", 10)) * 60.0,
        )
        self._next_seed_at = now + retry_s
        if self._active_id or self._queue or self._store.pending_questions():
            self._last_seed_reason = "queue_not_idle"
            self._metrics["self_seed_skipped"] += 1
            return None
        normalized = [
            (" ".join(str(title or "").split())[:80],
             " ".join(str(question or "").split())[:180])
            for title, question in candidates
            if str(title or "").strip() and str(question or "").strip()
        ]
        if not normalized:
            self._last_seed_reason = "no_public_interest_candidates"
            self._metrics["self_seed_skipped"] += 1
            return None
        cooldown = max(
            600.0,
            float(self._cfg.get("autonomous_research.self_seed_cooldown_minutes", 180)) * 60.0,
        )
        previous = [
            item for item in self._store.list_history(100)
            if item.get("creator") == "PERSONA_CURIOSITY"
        ]
        if previous:
            last_created = max(float(item.get("created_at", 0) or 0) for item in previous)
            if now - last_created < cooldown:
                self._next_seed_at = max(self._next_seed_at, last_created + cooldown)
                self._last_seed_reason = "persona_curiosity_cooldown"
                self._metrics["self_seed_skipped"] += 1
                return None

        total = len(normalized)
        for offset in range(total):
            index = (self._seed_cursor + offset) % total
            title, question_text = normalized[index]
            probe = ResearchQuestion(
                title=title,
                question=question_text,
                reason_code="PERSONAL_CURIOSITY",
                creator="PERSONA_CURIOSITY",
                priority=.68,
                expected_value=.72,
            )
            gate = self.gate(probe)
            if gate.decision is not GateDecision.ALLOW:
                continue
            self._seed_cursor = (index + 1) % total
            question = self.create_question(
                title=title,
                question=question_text,
                reason_code="PERSONAL_CURIOSITY",
                creator="PERSONA_CURIOSITY",
                priority=.68,
                expected_value=.72,
            )
            self._metrics["self_seed_created"] += 1
            self._last_seed_reason = "persona_interest_selected"
            self._next_seed_at = now + cooldown
            logger.info(
                "Autonomous research seeded from persona interest: %s",
                title,
            )
            return question
        self._last_seed_reason = "no_gate_eligible_interest"
        self._metrics["self_seed_skipped"] += 1
        return None

    def enqueue(self, question_id: str) -> None:
        if question_id in self._queued or question_id == self._active_id:
            return
        self._queued.add(question_id)
        self._queue.append(question_id)
        if self._task is None or self._task.done():
            # Mind.record_turn() is also used by synchronous maintenance/tests.
            # Persist the task now and let the next heartbeat start the worker
            # when there is no running loop, rather than losing the question or
            # raising "no running event loop".
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug(
                    "Autonomous research queued without an event loop; "
                    "the next heartbeat will start it"
                )
                return
            self._task = loop.create_task(
                self._worker(), name="autonomous-research",
            )

    async def _worker(self) -> None:
        while self._queue and not self._stopped:
            if self._paused or self._is_busy():
                await asyncio.sleep(.5)
                continue
            question_id = self._queue.popleft()
            self._queued.discard(question_id)
            question = self._store.get_question(question_id)
            if question is None:
                continue
            gate = self.gate(question)
            if gate.decision is GateDecision.DEFER:
                question.last_failure_reason = gate.reason
                self._store.save_question(question, normalized_query=gate.sanitized_query)
                continue
            if gate.decision is not GateDecision.ALLOW:
                question.status, question.last_failure_reason = ResearchStatus.BLOCKED.value, gate.reason
                self._store.save_question(question, normalized_query=gate.sanitized_query)
                continue
            self._active_id = question_id
            try:
                await self._execute(question, gate.sanitized_query)
            except asyncio.CancelledError:
                question.status, question.last_failure_reason = ResearchStatus.PENDING.value, "cancelled"
                self._store.save_question(question, normalized_query=gate.sanitized_query)
                raise
            except Exception as exc:
                logger.exception("Autonomous research failed")
                self._last_error = f"{type(exc).__name__}: {exc}"[:240]
                question.status, question.last_failure_reason = ResearchStatus.FAILED.value, f"{type(exc).__name__}: {exc}"[:160]
                self._store.save_question(question, normalized_query=gate.sanitized_query)
                self._metrics["runs_failed"] += 1
            finally:
                self._active_id = ""

    async def _execute(self, question: ResearchQuestion, query: str) -> None:
        run_id, started = uuid.uuid4().hex, time.time()
        self._last_started = started
        self._last_error = ""
        question.status, question.attempt_count = ResearchStatus.RESEARCHING.value, question.attempt_count + 1
        self._store.save_question(question, normalized_query=query)
        run: dict[str, Any] = {"run_id": run_id, "question_id": question.question_id, "mode": self.mode.value,
                                "trigger": question.creator, "status": "RESEARCHING", "started_at": started,
                                "sanitized_query": query, "query_ids": [hashlib.sha256(query.encode()).hexdigest()[:16]],
                                "evidence_ids": [], "knowledge_ids": [], "reflection_ids": [], "source_count": 0,
                                "confidence": 0.0, "privacy_decision": "SANITIZED", "safety_decision": "ALLOW",
                                "resource_usage": {"queries": 1}, "report_policy": question.report_policy}
        self._store.save_run(run)
        self._metrics["runs_started"] += 1
        self._emit("research_progress", run=run)
        search = DeepSearch(max_results=int(self._cfg.get("autonomous_research.max_sources_per_run", 4)),
                            region=str(self._cfg.get("search.region", "jp-jp")),
                            timeout=min(float(self._cfg.get("search.timeout_s", 7)), float(self._cfg.get("autonomous_research.timeout_seconds", 60))),
                            max_pages=0, parallelism=1)
        results = await asyncio.wait_for(asyncio.to_thread(search.search_results, query), timeout=float(self._cfg.get("autonomous_research.timeout_seconds", 60)))
        evidence: list[dict[str, Any]] = []
        for result in results[:int(self._cfg.get("autonomous_research.max_sources_per_run", 4))]:
            text = f"{result.get('title', '')}\n{result.get('body', '')}".strip()
            injection = bool(_INJECTION.search(text))
            if injection:
                self._metrics["prompt_injection_suspicions"] += 1
            url = str(result.get("href") or "")[:1000]
            item = {"evidence_id": uuid.uuid4().hex, "research_run_id": run_id, "question_id": question.question_id,
                    "source_url": url, "source_domain": urlparse(url).netloc[:255], "source_title": str(result.get("title") or "")[:400],
                    "source_type": "WEB_SEARCH", "retrieved_at": time.time(), "published_at": None,
                    "excerpt_summary": str(result.get("body") or "")[:1400], "claims": [] if injection else [str(result.get("body") or "")[:500]],
                    "source_quality": _source_quality(url), "freshness": "UNKNOWN", "version_scope": "",
                    "contradiction_group": "", "privacy_scope": "PUBLIC", "content_hash": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(),
                    "status": "REJECTED" if injection else "COLLECTED", "prompt_injection_suspected": injection}
            self._store.add_evidence(item)
            evidence.append(item)
        usable = [item for item in evidence if item["status"] == "COLLECTED"]
        run["evidence_ids"] = [item["evidence_id"] for item in evidence]
        run["source_count"] = len(usable)
        run["completed_at"] = time.time()
        domains = {item["source_domain"] for item in usable if item["source_domain"]}
        official = any(item["source_quality"] == "PRIMARY_OFFICIAL" for item in usable)
        verified = official or len(domains) >= 2
        if not usable:
            verdict, status, confidence = "INSUFFICIENT", ResearchStatus.PARTIALLY_LEARNED.value, .0
        elif verified:
            verdict, status, confidence = "VERIFIED", ResearchStatus.LEARNED.value, min(.92, .55 + .14 * len(domains) + (.18 if official else 0))
        else:
            verdict, status, confidence = "PROVISIONAL", ResearchStatus.PARTIALLY_LEARNED.value, .42
        run.update(status=verdict, confidence=round(confidence, 2))
        self._store.save_run(run)
        # A compact statement remains evidence-grounded: it says only that the
        # sources discuss the question; no model inference is promoted to fact.
        if usable:
            statement = " / ".join(item["source_title"] for item in usable[:2] if item["source_title"])[:500] or question.title
            knowledge = {"knowledge_id": uuid.uuid4().hex, "question_id": question.question_id, "topic": question.title,
                         "statement": statement, "confidence": round(confidence, 2), "evidence_ids": run["evidence_ids"],
                         "provenance": "autonomous_research", "verified_at": time.time() if verified else None,
                         "checked_at": time.time(), "expires_at": time.time() + 30 * 86400, "version_scope": "",
                         "privacy_scope": "PERSONA_ONLY", "usage_count": 0, "last_used_at": None,
                         "status": "ACTIVE" if verified else "PROVISIONAL", "verdict": verdict}
            self._store.save_knowledge(knowledge)
            run["knowledge_ids"] = [knowledge["knowledge_id"]]
            self._store.save_run(run)
            self._metrics["knowledge_verified" if verified else "knowledge_provisional"] += 1
        # Reflection is deliberately separate from evidence/facts.  It records
        # only a bounded next-use idea and never upgrades an external claim.
        reflection = {
            "reflection_id": uuid.uuid4().hex, "research_run_id": run_id,
            "question_id": question.question_id, "related_knowledge_ids": run["knowledge_ids"],
            "summary": "複数の根拠を確認できた" if verified else "根拠は暫定的で、必要時に再確認する",
            "implications": "関連する質問や目標が再び出た時だけ利用する",
            "possible_actions": ["必要なら出典を確認して再調査する"],
            "interest_change": .06 if usable else -.02, "confidence": round(confidence, 2),
            "created_at": time.time(), "privacy_scope": "PERSONA_ONLY",
        }
        self._store.save_reflection(reflection)
        run["reflection_ids"] = [reflection["reflection_id"]]
        self._store.save_run(run)
        self._store.update_interest(_topic_key(question.title), topic=question.title,
                                    success=bool(usable), unanswered=not verified)
        question.status, question.last_failure_reason = status, "" if usable else "no_results"
        self._store.save_question(question, normalized_query=query)
        self._metrics["evidence_collected"] += len(usable)
        self._metrics["runs_completed"] += 1
        self._last_completed = time.time()
        self._emit("research_complete", question=question.as_dict(), run=run)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def cancel_active(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._store.list_history(limit)

    def detail(self, question_id: str) -> dict[str, Any] | None:
        return self._store.detail(question_id)

    def disable_topic(self, topic: str, disabled: bool = True) -> None:
        self._store.set_auto_disabled(_topic_key(topic), disabled)

    def snapshot(self) -> dict[str, Any]:
        task = self._task
        return {"enabled": self.enabled, "mode": self.mode.value, "paused": self._paused,
                "active_question_id": self._active_id, "queued": len(self._queue),
                "self_seed": {
                    "enabled": bool(self._cfg.get(
                        "autonomous_research.self_seed_enabled", True,
                    )),
                    "last_attempt_at": self._last_seed_attempt,
                    "next_attempt_at": self._next_seed_at,
                    "last_reason": self._last_seed_reason,
                },
                "worker": {
                    "task_alive": task is not None and not task.done(),
                    "stopped": self._stopped,
                    "last_started_at": self._last_started,
                    "last_completed_at": self._last_completed,
                    "last_error": self._last_error,
                },
                "metrics": dict(self._metrics), **self._store.summary()}
