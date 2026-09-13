"""Mind: 記憶と人格を束ねるファサード。

3層記憶:
  短期 … ConversationManager の生履歴 (既存・そのまま)
  中期 … セッション要約。内省のたびにLLMが更新し、毎ターン注入される
  長期 … SQLite + embedding。事実・好み・エピソードを意味検索で想起

人格:
  PersonalityEngine が性格・気分・好み・親密度を保持。
  会話ログをためて数ターンごとに1回だけLLMで「内省」し、
  記憶の抽出と内面の微小な変化を同時に行う (会話が忙しい間は待つ)。
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import hashlib
import logging
import math
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from neuro_voice.mind.backup import BackupManager
from neuro_voice.mind.embedder import LocalEmbedder, embedder_available
from neuro_voice.mind.personality import PersonalityEngine
from neuro_voice.mind.relationship import RelationshipStore
from neuro_voice.mind.prompts import build_reflection_messages, parse_reflection_json
from neuro_voice.mind.speakers import SpeakerRegistry
from neuro_voice.mind.episodes import EpisodicMemoryService, RetrievalResult
from neuro_voice.mind.internal import InternalStateService
from neuro_voice.mind.store import MemoryStore, RetentionPolicy
from neuro_voice.mind.voiceprint import VoiceprintEncoder, voiceprint_available
from neuro_voice.dialogue import ConversationKernel, DialogueIntelligence, MemoryRecallPolicy
from neuro_voice.dialogue.conversation_contract import recall_requested as is_recall_request
from neuro_voice.dialogue.memory_policy import MemoryRejection
from neuro_voice.dialogue.temporal import TimeAwareness
from neuro_voice.dialogue.working_memory import WorkingMemory
from neuro_voice.mind.temporal_self import TemporalSelf
from neuro_voice.activity import ActivityStateManager, ActivityOutcome
from neuro_voice.games import GameProfileSessionManager
from neuro_voice.privacy import PrivacyManager
from neuro_voice.research import AutonomousResearchService
from neuro_voice.research.store import ResearchStore
from neuro_voice.utils.textseg import strip_think

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


def self_failure_pattern(decision, outcome) -> str:
    """認知決定と実行結果から読み取れる、自分の失敗。

    **Local と Discord で同じ判定を使う。** ここが2箇所にあると、
    片方だけ新しいパターンを覚えて食い違う。自由文にせず短い識別子で持つ。
    """
    if decision is None or outcome is None:
        return ""
    action = str(getattr(decision, "selected_action", ""))
    status = str(getattr(outcome, "status", ""))
    if status == "interrupted" and action in {"continue_previous_topic", "answer",
                                              "comment", "react"}:
        # 言い終える前に割り込まれた。話しすぎている可能性。
        return "kept_talking_after_end_signal"
    if float(getattr(decision, "confidence", 1.0) or 1.0) < .55 and action == "answer":
        return "answered_on_low_confidence"
    return ""


def _allowed_next(mode):
    """いまの段階から進める先。**UIのボタンはこれを見る。**"""
    from neuro_voice.mind.migration import ALLOWED_TRANSITIONS

    return ALLOWED_TRANSITIONS.get(mode, frozenset())


def _display_resolution(verdict, resolver, speaker_id: int) -> str:
    """画面に出す解決状態。**表示のために状態を変えない。**

    `Legacy Speaker` / `Resolved Person` / `Unknown` / `Conflicted` /
    `Revoked` を区別する。全部「話者」に見えていると、
    誰が人物として繋がっているのか分からない。
    """
    from neuro_voice.mind.migration import _speaker_key

    status = str(getattr(verdict, "resolution_status", "unknown"))
    if status == "conflicted":
        return "conflicted"
    if getattr(verdict, "known", False):
        return "resolved_person"
    value = _speaker_key(f"voice:{speaker_id}").replace("speaker:", "voice:")
    history = resolver.history_for(value) if resolver is not None else []
    if history and all(str(item.status) in {"revoked", "superseded"}
                       for item in history):
        return "revoked"
    if status == "probable":
        return "unknown"
    return "legacy_speaker"


def _safe_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "default"


# 「俺の名前はちびだよ」「ちびって呼んで」等の名乗りを即時検出するパターン
_NAME_CHARS = r"一-龠々ぁ-んァ-ヶーa-zA-Z0-9"
_NAME_PATTERNS = [
    re.compile(
        r"(?:私|わたし|俺|おれ|僕|ぼく|自分|うち)の(?:名前|名)[はが]\s*[、,]?\s*"
        rf"([{_NAME_CHARS}]{{1,12}}?)(?:って(?:いう|言う|呼んで)|と(?:いい|言い|申し)ます|だよ|です|だ|$|[、。!?!?\s])"
    ),
    re.compile(rf"([{_NAME_CHARS}]{{1,12}}?)(?:って|と)呼んで"),
    re.compile(  # 「名前はコアラ」等。「猫の名前は〜」のような他者の名前は除外
        rf"(?<!の)名前は\s*[、,]?\s*([{_NAME_CHARS}]{{1,12}}?)(?:って(?:いう|言う)|だよ|です|だ|$|[、。!?!?\s])"
    ),
]
_NOT_NAMES = {"秘密", "内緒", "誰", "だれ", "何", "ない", "いい", "普通", "自由"}


def detect_self_name(text: str) -> str | None:
    """発話から「名乗り」を検出する。見つからなければ None。"""
    text = text.split("\n")[0]  # トーン注釈などは見ない
    for pat in _NAME_PATTERNS:
        m = pat.search(text)
        if m:
            name = m.group(1).strip()
            if not name or name in _NOT_NAMES or name.startswith("ゲスト"):
                continue
            # 「名前はなんだっけ?」「名前はなんでしょう」等の疑問詞を
            # 名前として誤学習しない (実例: 「なん」で登録される事故)
            if name.startswith(("なん", "なに", "何")):
                continue
            return name
    return None


def _json_safe(obj):
    """JSON(ひいてはpywebview)へ確実に渡せる形へ再帰的に変換する。

    numpy の数値型・set・tuple・bytes・その他の非対応型が混ざっていても
    こころステータスの取得が失敗しないようにするための保険。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "replace")
    item = getattr(obj, "item", None)  # numpy スカラー等 → Python数値
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:
            pass
    try:
        return float(obj)
    except (TypeError, ValueError):
        return str(obj)


# 「昨日話したこと」のような日付つきの想起質問 → 会話ログの検索窓 (日数)
_RECALL_INTENT_RE = re.compile(r"話|はなし|言って|いって|覚え|おぼえ|何した|なにした|記憶|思い出")
_TEMPORAL_WINDOWS = (
    (re.compile(r"一昨日|おととい"), 2, 2),
    (re.compile(r"昨日|きのう|昨夜|ゆうべ"), 1, 1),
    (re.compile(r"先週"), 3, 9),
    (re.compile(r"この前|このまえ|この間|こないだ|前回"), 1, 7),
)
_ASSISTANT_TRANSCRIPT_RECALL_RE = re.compile(
    r"(?:"
    r"(?:何|なに)(?:て|って|を)?(?:言った|いった|話した|はなした|勧めた|すすめた)|"
    r"(?:おすすめ|オススメ|勧め)(?:した|てた).{0,30}(?:何|なに|どれ)|"
    r"(?:あなた|きみ|君|ポッポ|ぽっぽ|AI).{0,20}"
    r"(?:何|なに)(?:て|って|を)?(?:言った|いった|話した|はなした|勧めた|すすめた)"
    r")"
)

_RECALL_FACT_LABELS = ("合言葉", "計画名", "番号", "機器名", "機材名", "装置名")
_LABELED_RECALL_FACTS = tuple(
    (label, re.compile(
        rf"(?:{re.escape(label)})(?:は|[:：])\s*[「『\"]?([^」』\"、。！!?？\s]+)"
    ))
    for label in _RECALL_FACT_LABELS
)


def _recall_label(text: str) -> str:
    value = str(text or "")
    return next((label for label in _RECALL_FACT_LABELS if label in value), "")


def _explicit_recall_request(text: str) -> bool:
    """Recall intent with a narrow ASR-tolerant passphrase form.

    The generic contract recognizer is intentionally conservative.  The
    controlled passphrase smoke sentence can lose its leading time reference
    in ASR, so preserve the fact label plus an evidence-seeking verb here.
    """
    value = " ".join(str(text or "").split())
    return bool(
        is_recall_request(value)
        or (_recall_label(value) and re.search(r"(?:教|覚|前|以前|計測|名前)", value))
    )


def temporal_query_window(text: str, *, now: float | None = None) -> tuple[float, float] | None:
    """発話が「昨日/この前 何を話した?」型なら (開始ts, 終了ts) を返す。"""
    text = str(text or "")
    if not _RECALL_INTENT_RE.search(text):
        return None
    for pattern, newest_days, oldest_days in _TEMPORAL_WINDOWS:
        if pattern.search(text):
            today = _dt.date.fromtimestamp(now if now is not None else time.time())
            start = _dt.datetime.combine(
                today - _dt.timedelta(days=oldest_days), _dt.time.min,
            ).timestamp()
            end = _dt.datetime.combine(
                today - _dt.timedelta(days=newest_days - 1), _dt.time.min,
            ).timestamp()
            return start, end
    return None


def assistant_transcript_wording_required(text: str) -> bool:
    """Whether answering requires the assistant's earlier surface wording."""
    return bool(_ASSISTANT_TRANSCRIPT_RECALL_RE.search(str(text or "")))


def dedupe_transcript_rows(rows: list[dict], *, limit: int | None = None) -> list[dict]:
    """Keep one high-priority row per repeated user utterance.

    Semantic recall can return the same test phrase or acknowledgement from
    several sessions.  Repeating those rows in the prompt makes frequency look
    like evidence and crowds out the current conversation.  Input order is
    priority order, so the first occurrence wins.
    """
    unique: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        surface = str(row.get("user_text") or "")
        key = re.sub(r"[\s\u3000、。,.!！?？〜～…・「」『』（）()]", "", surface).lower()
        key = key or f"id:{row.get('id', '')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if limit is not None and len(unique) >= limit:
            break
    return unique


def transcript_context_line(
    row: dict,
    *,
    when: str,
    include_assistant_wording: bool,
) -> str:
    """Format recall evidence without turning old replies into style examples."""
    who = str(row.get("speaker") or "ユーザー")
    emo = str(row.get("emotion") or "")
    emo_note = f" (この時の私は{emo}な気分だった)" if emo and emo != "neutral" else ""
    user_line = str(row.get("user_text", "")).split("\n")[0][:70]
    if include_assistant_wording:
        reply_line = str(row.get("assistant_text", ""))[:70]
        return f"- [{when}] {who}「{user_line}」→ 私「{reply_line}」{emo_note}"
    return f"- [{when}] {who}が「{user_line}」について話した{emo_note}"


class Mind:
    """記憶 + 人格エンジン。パイプラインから毎ターン参照される。"""

    def __init__(
        self,
        cfg,
        llm,
        persona_key: str,
        persona_name: str,
        is_busy: Callable[[], bool] | None = None,
        on_event: EventCallback | None = None,
    ):
        self._cfg = cfg
        self._llm = llm
        self._is_busy = is_busy or (lambda: False)
        self._on_event = on_event
        self._data_dir = Path(str(cfg.get("mind.data_dir", "data")))
        self._top_k = int(cfg.get("mind.recall.top_k", 5))
        self._min_score = float(cfg.get("mind.recall.min_score", 0.72))
        self._recall_timeout = float(cfg.get("mind.recall.timeout_s", 4.0))
        self._recall_mention_score = float(cfg.get("mind.recall.mention_score", 0.88))
        self._every_turns = max(2, int(cfg.get("mind.reflection.every_turns", 6)))
        self._min_idle = float(cfg.get("mind.reflection.min_idle_s", 5.0))
        self._llm_timeout = float(cfg.get("mind.reflection.timeout_s", 120.0))
        self._cancel_reflection_on_activity = bool(
            cfg.get("mind.reflection.cancel_on_user_activity", True)
        )
        self._max_items = int(cfg.get("mind.max_memories", 2000))
        # 会話ログ記憶: 要約でなく実際の発言+その時の感情を保存し想起する
        self._transcript_enabled = bool(cfg.get("mind.transcript.enabled", True))
        self._transcript_top_k = max(0, int(cfg.get("mind.transcript.top_k", 3)))
        self._transcript_temporal_max = max(1, int(cfg.get("mind.transcript.temporal_max", 6)))
        self._transcript_retention_days = float(cfg.get("mind.transcript.retention_days", 45))
        self._transcript_max_items = int(cfg.get("mind.transcript.max_items", 8000))
        growth = self._growth_for(persona_key)

        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mind")
        # 記憶の想起 (embedding検索) 専用の実行スレッド。声紋エンコード等と
        # 同じスレッドを共有すると、毎ターンの話者識別の後ろに想起が並んで
        # しまい、短いタイムアウト内にほぼ確実に間に合わなくなる
        # (「過去の話を全く思い出せない」の主原因)。
        self._recall_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mind-recall")
        self._embedder: LocalEmbedder | None = None
        if str(cfg.get("mind.embedding.backend", "local")) == "local" and embedder_available():
            self._embedder = LocalEmbedder(
                model=str(cfg.get("mind.embedding.model", "intfloat/multilingual-e5-small")),
                device=str(cfg.get("mind.embedding.device", "cpu")),
            )
        elif str(cfg.get("mind.embedding.backend", "local")) == "local":
            logger.warning(
                "sentence-transformers が未導入のため、記憶の想起はキーワード検索になります "
                "(pip install sentence-transformers で意味検索が有効になります)"
            )

        self._persona_key = _safe_key(persona_key)
        self._persona_name = persona_name
        self._store = self._open_persona_store(self._persona_key)
        self._episodes = EpisodicMemoryService(self._store, cfg)
        self._bind_persona_scope()
        self._personality = PersonalityEngine(
            self._data_dir / f"personality_{self._persona_key}.json", growth_rate=growth
        )
        # Persona-scoped dialogue control.  This is intentionally separate
        # from memories: user traits and conversation state must not pollute
        # the recallable fact store.
        self._dialogue = DialogueIntelligence(
            cfg, self._data_dir / f"dialogue_{self._persona_key}.json",
            persona_key=self._persona_key,
        )
        #: 直近に選んだ行動。同じ行動の連投を点数で抑えるために持つ。
        self._recent_actions: list[str] = []
        self._kernel = ConversationKernel(
            enabled=bool(cfg.get("conversation_kernel.enabled", True)),
            max_frames=int(cfg.get("conversation_kernel.max_frames", 24)),
        )
        self._last_kernel_source = "local"
        # True once the person has spoken in *this* session.
        self._had_user_turn = False
        # One continuation controller per persona, shared by local voice and
        # Discord.  Sharing it is what keeps the two surfaces from reaching
        # different conclusions about the same request.
        from neuro_voice.dialogue.continuation import ContinuationController

        self._continuation = ContinuationController(cfg, emit=self._emit)
        self._continuation.cancel_all_for_restart()
        self._relationships = RelationshipStore(
            self._data_dir / f"relationships_{self._persona_key}.json", cfg,
            persona_id=self._persona_key,
        )
        self._temporal = TimeAwareness(self._data_dir / f"temporal_{self._persona_key}.json", cfg)
        self._temporal_self = TemporalSelf(
            self._data_dir / f"temporal_self_{self._persona_key}.json", cfg,
            persona_key=self._persona_key,
        )
        # 持続する内面。**短期感情はここ（RAM）が持ち、長期の関係と好みは
        # `RelationshipStore` と記憶のまま**（第5条）。
        self._internal = InternalStateService(
            cfg, relationships=self._relationships,
            temporal_self=self._temporal_self, episodes=self._episodes,
        )
        # Activity facts are persona-scoped just like relationship/memory data.
        # The manager is intentionally independent of transcript history.
        self._activities = ActivityStateManager(
            self._data_dir / f"activities_{self._persona_key}.json", cfg,
        )
        self._game_session = GameProfileSessionManager(
            cfg, self._data_dir / f"game_session_{self._persona_key}.json",
        )
        #: 人格を切り替えても付け直すために覚えておく。
        self._game_observers: list = []
        #: 関係値の移行 (Phase 6E)。**繋ぐまでは従来どおり。**
        self._identity_runtime: Any = None
        self._relationship_resolver: Any = None
        self._identity_migration: Any = None
        self._migration_jobs: Any = None
        # Autonomous research is persona-scoped and kept out of ordinary
        # memory.  Its store never contains the raw conversation that led to
        # a question; only a privacy-sanitized research record is persisted.
        self._research_store = ResearchStore(self._data_dir / f"research_{self._persona_key}.db")
        self._research = AutonomousResearchService(
            cfg, self._research_store,
            on_event=lambda event_type, data: self._emit(event_type, **data),
            is_busy=self._is_busy,
        )
        self._recall_policy = MemoryRecallPolicy(self._recall_mention_score)
        self._last_recall: list[dict[str, Any]] = []
        self._last_recall_records: list[dict[str, Any]] = []
        self._last_recall_evidence_memory_ids: list[int] = []
        self._last_retrieval_trace: dict[str, Any] = {}
        self._last_recall_mode = ""
        self._privacy = PrivacyManager(cfg)
        self._privacy_context: dict[str, object] = {"group": False, "audience": set()}
        self._emb_cache: tuple[list[int], list[dict], np.ndarray] | None = None
        self._transcript_cache: tuple[list[int], list[dict], np.ndarray] | None = None
        if self._transcript_enabled:
            with contextlib.suppress(Exception):
                self._store.prune_transcripts(
                    self._transcript_retention_days, max_items=self._transcript_max_items,
                )

        self._session_summary = ""
        self._pending: list[dict[str, str]] = []
        self._pending_emotion: str | None = None
        self._reflect_task: asyncio.Task | None = None
        self._reflection_generating = False
        self._lock = asyncio.Lock()

        # 話者識別 (声で相手を覚える)
        self._voiceprint: VoiceprintEncoder | None = None
        self._speakers: SpeakerRegistry | None = None
        self._current_speaker: dict[str, Any] | None = None
        self._ask_name = bool(cfg.get("speaker.ask_name", True))
        self._speaker_timeout = float(cfg.get("speaker.timeout_s", 4.0))
        if bool(cfg.get("speaker.enabled", True)):
            if voiceprint_available():
                quality = str(cfg.get("speaker.model", "full"))
                self._voiceprint = VoiceprintEncoder(
                    device=str(cfg.get("speaker.device", "cpu")),
                    quality=quality,
                )
                self._speaker_quality = quality
                self._speakers = self._make_speaker_registry(
                    self._persona_key, migrate_legacy=True,
                )
            else:
                logger.warning(
                    "onnxruntime が未導入のため話者識別は無効です "
                    "(pip install onnxruntime で有効になります)"
                )

        # 起動時にembedding/声紋モデルを裏でロードしておく
        # (会話中の初回ロード/ダウンロードで応答が止まるのを防ぐ)
        if self._embedder is not None:
            self._recall_ex.submit(self._warmup_embedder)
        if self._voiceprint is not None:
            self._ex.submit(self._warmup_voiceprint)

        # 人格・記憶データの自動バックアップ (エラーは会話へ波及させない)
        self._backup: BackupManager | None = None
        if bool(cfg.get("mind.backup.enabled", True)):
            try:
                self._backup = BackupManager.from_config(
                    cfg, self._data_dir, save_hook=self._save_all_states
                )
                self._backup.start()
            except Exception:
                logger.exception("バックアップ機能の初期化に失敗 (無効のまま続行)")
                self._backup = None

    def _growth_for(self, persona_key: str) -> float:
        """成長率。ペルソナ別 growth_rate があれば全体設定より優先する。"""
        base = float(self._cfg.get("mind.growth_rate", 1.0))
        override = self._cfg.get(f"persona.presets.{persona_key}.growth_rate")
        try:
            return float(override) if override is not None else base
        except (TypeError, ValueError):
            return base

    @property
    def game_session(self):
        """いま選ばれているゲームのセッション。**正本はここ1つ**（第5条）。"""
        return self._game_session

    def attach_game_observer(self, callback) -> None:
        """ゲームの出来事を受け取る。

        人格を切り替えるとセッション管理は作り直されるので、
        **登録した相手を覚えておいて付け直す**——付け直しを忘れると、
        人格を変えた瞬間に配線だけ静かに切れる。
        """
        if not callable(callback):
            return
        if callback not in self._game_observers:
            self._game_observers.append(callback)
        self._game_session.add_observer(callback)

    def _reattach_game_observers(self) -> None:
        for callback in self._game_observers:
            with contextlib.suppress(Exception):
                self._game_session.add_observer(callback)

    def _save_all_states(self) -> None:
        """バックアップ前に全エンジンの状態をディスクへ書き出す。"""
        self._personality.save()
        self._temporal_self.heartbeat()
        with contextlib.suppress(Exception):
            self._dialogue.save()
        self._relationships.save()
        self._temporal.save()
        self._activities.save()
        self._game_session.save()
        if self._speakers is not None:
            self._speakers.save()

    # ---------- バックアップ (UI用) ----------

    @property
    def backup(self) -> BackupManager | None:
        return self._backup

    def backup_now(self) -> dict[str, Any]:
        if self._backup is None:
            return {"ok": False, "message": "バックアップ機能が無効です (mind.backup.enabled)"}
        try:
            return self._backup.create_backup(reason="manual")
        except Exception:
            logger.exception("手動バックアップに失敗")
            return {"ok": False, "message": "バックアップ作成中にエラーが発生しました (ログ参照)"}

    def _warmup_voiceprint(self) -> None:
        try:
            rng = np.random.default_rng(0)
            self._voiceprint.encode(rng.standard_normal(16000).astype(np.float32) * 0.01)
        except Exception:
            logger.exception("声紋モデルのウォームアップに失敗 (話者識別は無効)")
            self._voiceprint = None
            self._speakers = None

    def _warmup_embedder(self) -> None:
        try:
            self._embedder.encode(["ウォームアップ"])
        except Exception:
            logger.exception("embeddingモデルのウォームアップに失敗 (キーワード検索で継続)")
            self._embedder = None
        # Persistent vectors stay in the bounded derived index.  Preloading
        # every retained memory/transcript here would make startup RAM grow
        # linearly with source retention and bypass the Stage M candidate cap.

    def _speaker_path(self, persona_key: str) -> Path:
        """話者名・声紋・なかよし度をペルソナ別に保存するパス。"""
        return self._data_dir / f"speakers_{_safe_key(persona_key)}.json"

    def _make_speaker_registry(self, persona_key: str, *, migrate_legacy: bool = False) -> SpeakerRegistry:
        path = self._speaker_path(persona_key)
        # 初回だけ現在のペルソナへ旧共通データを引き継ぐ。別ペルソナへはコピーしない。
        legacy = self._data_dir / "speakers.json"
        if (migrate_legacy and bool(self._cfg.get("speaker.migrate_legacy_shared_once", True))
                and not path.exists() and legacy.exists()):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, path)
                logger.info("旧共通話者データを %s へ一度だけ移行", path.name)
            except Exception:
                logger.exception("旧共通話者データの移行に失敗")
        quality = getattr(self, "_speaker_quality", str(self._cfg.get("speaker.model", "full")))
        return SpeakerRegistry(
            path,
            threshold=float(self._cfg.get("speaker.threshold", 0.70)),
            new_threshold=float(self._cfg.get("speaker.new_threshold", 0.50)),
            sticky_window_s=float(self._cfg.get("speaker.sticky_window_s", 90.0)),
            sticky_margin=float(self._cfg.get("speaker.sticky_margin", 0.08)),
            model_tag=f"wavlm-sv-{quality}",
        )

    # ---------- 外部から状態を差し替え ----------

    def set_llm(self, llm) -> None:
        self._llm = llm

    def persona_trace_fields(self, memory_ids=()) -> dict[str, Any]:
        """分離が効いているかを読むための数字（Phase 7D 観測点）。

        **本文は返さない。** 持ち主・スコープ・件数だけ。件数の集計は
        1クエリで済ませる——観測のために毎ターン全記憶を読み出すと、
        測るために遅くなる。
        """
        key = str(getattr(self, "_persona_key", "") or "")
        context = self.persona_context
        switch = self.persona_switch()
        owners = {}
        with contextlib.suppress(Exception):
            owners = self._store.owners_of(memory_ids)
        rejected: dict[str, int] = {}
        if bool(self._cfg.get("turn_integrity.persona_scope_enabled", False)):
            with contextlib.suppress(Exception):
                rejected = self._store.scope_rejections(key)
        legacy = int(rejected.pop("", 0))
        return {
            "active_persona_id": key,
            "active_persona_version": str(
                getattr(context, "persona_version", "") or ""),
            "persona_epoch": int(getattr(context, "persona_epoch", 0) or 0),
            "retrieved_memory_persona_ids": [
                owner for owner, _ in owners.values()],
            "retrieved_memory_scopes": [scope for _, scope in owners.values()],
            "cross_persona_rejection_count": sum(rejected.values()),
            "cross_persona_rejection_persona_ids": sorted(rejected),
            "legacy_unscoped_rejection_count": legacy,
            "persona_switch_state": str(getattr(switch, "state", "") or ""),
            "persona_switch_from": str(getattr(
                getattr(switch, "history", [None])[-1] if getattr(switch, "history", None)
                else None, "from_persona", "") or ""),
            "persona_switch_to": key,
            "persona_switch_epoch": int(
                getattr(context, "persona_epoch", 0) or 0),
        }

    def _bind_persona_scope(self) -> None:
        """記憶の読み書きに、いまのペルソナを結びつける（Phase 7D ⑤）。

        **ここが呼ばれていなかった。** `EpisodicMemoryService.bind_persona()`
        も `MemoryStore.bind_persona()` も定義はあるのに呼び出し側が無く、
        読み側の絞り込みも書き込み側の持ち主も空のままだった。
        Store を作り直す所（初期化とペルソナ切替）の両方で結ぶ。
        """
        key = str(getattr(self, "_persona_key", "") or "")
        scoped = bool(self._cfg.get("turn_integrity.persona_scope_enabled", False))
        legacy = bool(self._cfg.get(
            "turn_integrity.allow_legacy_unscoped_memory", False))
        with contextlib.suppress(Exception):
            # **書き込みは常に持ち主を付ける。** フラグに関係なく。
            # 空のまま溜めると、あとで分離を有効にした瞬間に全部隠れる。
            # 拒否（strict）だけを分離が有効な時に限る——無効の間は
            # 持ち主が空でも読めるので、拒否すると読める記憶を落とす。
            self._store.bind_persona(key, strict=scoped)
        with contextlib.suppress(Exception):
            self._episodes.bind_persona(
                key if scoped else "", allow_legacy_unscoped=legacy)

    def persona_switch(self):
        """ペルソナ切替の状態機械 (Phase 7C)。**世代を持つ。**"""
        switch = getattr(self, "_persona_switch", None)
        if switch is None:
            from neuro_voice.cognition.persona_scope import PersonaSwitch

            switch = PersonaSwitch(self._persona_key)
            self._persona_switch = switch
        return switch

    @property
    def persona_context(self):
        """いま誰か。**`persona_id` だけでなく世代まで。**"""
        return self.persona_switch().context

    def _open_persona_store(self, persona_key: str) -> MemoryStore:
        """Open the shared Local/Discord memory source with capacity telemetry."""
        configured_backup = str(self._cfg.get("mind.backup.dir", "") or "").strip()
        backup_dir = (
            Path(configured_backup) if configured_backup
            else self._data_dir / "backups"
        )
        return MemoryStore(
            self._data_dir / f"mind_{_safe_key(persona_key)}.db",
            retention_policy=RetentionPolicy.from_config(self._cfg),
            capacity_warning_free_percent=self._cfg.get(
                "mind.storage.warning_free_percent", 20.0,
            ),
            capacity_probe_paths={
                "data": self._data_dir,
                "backup": backup_dir,
            },
        )

    def switch_persona(self, persona_key: str, persona_name: str) -> None:
        """ペルソナ切替。人格・記憶・話し相手・なかよし度をすべて分離する。

        Phase 7C: **設定値の書き換えではなく状態遷移**として扱う。
        世代（`persona_epoch`）を進め、切替前に始まった非同期の結果を
        受け付けなくする。旧ペルソナの遅い返事を新しい声で読み上げる
        のが、いちばんまずい形。
        """
        key = _safe_key(persona_key)
        if key == self._persona_key:
            self._persona_name = persona_name
            return
        # **新規処理を止めてから世代を進める。**
        self.persona_switch().switch(key, drain=self._drain_persona_work)
        self._personality.save()
        self._dialogue.close()
        self._relationships.save()
        self._temporal.save()
        self._store.close()
        # A persona owns its research queue and database.  Do not let a
        # queued result from the previous persona write into the new one.
        self._research.cancel_active()
        self._research_store.close()
        if self._speakers is not None:
            self._speakers.save()
        self._game_session.save()
        self._persona_key = key
        self._persona_name = persona_name
        growth = self._growth_for(persona_key)
        self._store = self._open_persona_store(key)
        self._episodes = EpisodicMemoryService(self._store, self._cfg)
        self._bind_persona_scope()
        self._personality = PersonalityEngine(
            self._data_dir / f"personality_{key}.json", growth_rate=growth
        )
        self._dialogue = DialogueIntelligence(
            self._cfg, self._data_dir / f"dialogue_{key}.json", persona_key=key,
        )
        self._relationships = RelationshipStore(
            self._data_dir / f"relationships_{key}.json", self._cfg,
            persona_id=key,
        )
        self._temporal = TimeAwareness(self._data_dir / f"temporal_{key}.json", self._cfg)
        self._temporal_self = TemporalSelf(
            self._data_dir / f"temporal_self_{key}.json", self._cfg, persona_key=key,
        )
        # ペルソナが変われば短期感情は引き継がない（長期の関係は各ファイルが持つ）。
        self._internal = InternalStateService(
            self._cfg, relationships=self._relationships,
            temporal_self=self._temporal_self, episodes=self._episodes,
        )
        self._activities.save()
        self._activities = ActivityStateManager(
            self._data_dir / f"activities_{key}.json", self._cfg,
        )
        self._game_session = GameProfileSessionManager(
            self._cfg, self._data_dir / f"game_session_{key}.json",
        )
        self._reattach_game_observers()
        # 人格ごとに関係値のファイルが変わる。**作り直す。**
        self._relationship_resolver = None
        self._identity_migration = None
        self._migration_jobs = None
        self._research_store = ResearchStore(self._data_dir / f"research_{key}.db")
        self._research = AutonomousResearchService(
            self._cfg, self._research_store,
            on_event=lambda event_type, data: self._emit(event_type, **data),
            is_busy=self._is_busy,
        )
        if self._voiceprint is not None:
            self._speakers = self._make_speaker_registry(key, migrate_legacy=False)
        self._current_speaker = None
        self._emb_cache = None
        self._transcript_cache = None
        self._session_summary = ""
        self._pending.clear()
        self._last_recall = []
        self._last_recall_records = []
        self._last_recall_evidence_memory_ids = []
        self._last_recall_mode = ""
        # 道具の一式も人格ごと (Phase 7B/7C)。
        # **確認待ちと未報告を持ち越さない。** 前の子が聞いた「やっていい?」
        # に、次の子が答えられてしまう。
        self._drop_tool_runtime()
        logger.info("Mind: ペルソナを %s に切替", persona_name)

    def _drain_persona_work(self) -> int:
        """切替の前に、走っているものを終わらせる／失効させる。

        戻り値は落とした件数。**0 でも構わないが、数えておく**——
        後から「切替の瞬間に何が動いていたか」を聞かれた時に、
        記録が無いと答えようがない。
        """
        dropped = 0
        with contextlib.suppress(Exception):
            self._research.cancel_active()
            dropped += 1
        runtime = getattr(self, "_tool_runtime", None)
        if runtime is not None:
            with contextlib.suppress(Exception):
                dropped += int(runtime.close_turn("persona_switch") or 0)
        return dropped

    def _drop_tool_runtime(self) -> None:
        runtime = getattr(self, "_tool_runtime", None)
        if runtime is None:
            return
        with contextlib.suppress(Exception):
            runtime.shutdown()
        self._tool_runtime = None

    def accepts_persona_result(self, stamped) -> bool:
        """切替前に始まった結果を受け入れてよいか (Phase 7C)。

        **一致しなければ、状態更新も記憶保存も Planner 投入も発話も
        しない。** 「もう届いてしまったから使う」をやると、
        前の子の考えが次の子の口から出る。
        """
        return self.persona_switch().accepts(stamped)

    def reset_session(self) -> None:
        """会話リセット時: セッション要約と未処理ログを破棄 (長期記憶と人格は残る)。"""
        self._session_summary = ""
        self._pending.clear()

    # ---------- 自律研究 ----------

    @property
    def research(self) -> AutonomousResearchService:
        return self._research

    def research_heartbeat(self) -> None:
        """Advance queued research and offer grounded persona interests."""
        self._research.heartbeat()
        self._research.maybe_seed_curiosity(self._research_interest_candidates())

    def _research_interest_candidates(self) -> list[tuple[str, str]]:
        """Public, persona-authored interests safe to offer to the research gate."""
        try:
            from neuro_voice.memory.persona import get_active_persona

            _key, persona = get_active_persona(self._cfg)
            raw = str((persona or {}).get("interests") or "")
        except Exception:
            logger.debug("自律研究の興味候補を取得できませんでした", exc_info=True)
            return []
        topics = [
            " ".join(value.split())[:48]
            for value in re.split(r"[、,，/／\n]+", raw)
            if 2 <= len(" ".join(value.split())) <= 48
        ]
        return [
            (topic, f"{topic} 最近の重要な変化や新しい動向")
            for topic in dict.fromkeys(topics)
        ][:8]

    def maybe_queue_knowledge_gap(self, user_text: str, reply: str) -> dict[str, Any] | None:
        """Queue a direct-conversation factual gap after an honest non-answer."""
        if bool(self._privacy_context.get("group", False)):
            return None
        text = str(user_text or "").split("\n", 1)[0].strip()
        answer = str(reply or "").strip()
        if not text or not answer:
            return None
        if not re.search(r"[?？]|(?:何|なに|どうやって|教えて|知ってる)", text):
            return None
        if not re.search(
            r"(?:知らない|分からない|わからない|確認できない|情報がない|特定できない)",
            answer,
        ):
            return None
        if re.search(
            r"(?:私のこと|どう思う|どう感じる|好き|恥ずかし|顔|声|聞こえる|見える|覚えてる)",
            text,
        ):
            return None
        question = self._research.create_question(
            title=text[:100],
            question=text[:220],
            reason_code="KNOWLEDGE_GAP",
            creator="CONVERSATION_GAP",
            priority=.78,
            expected_value=.78,
            report_policy="REPORT_WHEN_RELEVANT",
        )
        return question.as_dict()

    def maybe_queue_user_research(self, user_text: str) -> dict[str, Any] | None:
        """Recognise an explicit deferred-research request without storing raw text.

        Immediate "調べて" remains the normal response-time search path.  Only
        "調べておいて/あとで" becomes a background commitment.
        """
        text = str(user_text or "").strip()
        if not re.search(r"(?:あとで|後で|ついでに|自動で)?(?:調べて|検索して|学習して)(?:おいて|おいてね|ほしい|欲しい)", text):
            return None
        # Strip the request phrase before handing the topic to the privacy
        # boundary.  The service will block rather than persist personal data.
        topic = re.sub(r"(?:これ|それ|この話題|この件)?(?:について)?(?:あとで|後で|ついでに|自動で)?(?:調べて|検索して|学習して)(?:おいて|おいてね|ほしい|欲しい)[。!！?？]*", "", text).strip(" 、。")
        if len(topic) < 2:
            topic = "ユーザーが指定した話題"
        question = self._research.create_question(
            title=topic[:100], question=topic[:220], reason_code="USER_REQUEST",
            creator="USER", priority=.9, expected_value=.9, report_policy="REPORT_NOW",
        )
        return question.as_dict()

    def research_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._research.history(limit)

    def research_detail(self, question_id: str) -> dict[str, Any] | None:
        return self._research.detail(question_id)

    def research_disable_topic(self, topic: str, disabled: bool = True) -> None:
        self._research.disable_topic(topic, disabled)

    def reset_relationship(self, speaker_key: str | None = None, *, temporary_only: bool = False) -> None:
        """UI用。現在の相手または指定相手の関係性を安全に初期化する。"""
        self._relationships.reset(speaker_key or self._relationship_speaker_key(), temporary_only)

    def relationship_status(self, speaker_key: str | None = None) -> dict[str, Any]:
        return self._relationships.snapshot(speaker_key or self._relationship_speaker_key())

    # ---------- Realtime dialogue intelligence ----------

    def _dialogue_user_key(self, source: str = "local") -> str:
        cur = self._current_speaker or {}
        sid = int(cur.get("id", -1))
        if sid >= 0:
            return f"speaker:{sid}"
        name = str(cur.get("name", "")).strip()
        if name:
            return f"hint:{_safe_key(name)}"
        return f"{source}:user"

    def _directive_key_snapshot(self) -> str:
        return self._dialogue_user_key(self._last_kernel_source or "local")

    def _follow_speaker_key(self, previous_key: str) -> None:
        """Move a running directive when the speaker's identity resolves.

        The first turn creates a directive before voiceprint matching has
        finished, so it is filed under ``local:user``.  A few hundred
        milliseconds later the key becomes ``speaker:1`` and every later turn
        looks there — finding nothing, and starting a second directive while
        the first one is still holding the story.  Following the key keeps one
        session alive across that transition, and across a speaker merge.
        """
        new_key = self._directive_key_snapshot()
        if not previous_key or new_key == previous_key:
            return
        with contextlib.suppress(Exception):
            self._continuation.rekey(previous_key, new_key)

    def _relationship_speaker_key(self, source: str = "local") -> str:
        """Never merge a relationship when the identity is uncertain."""
        cur = self._current_speaker or {}
        sid = int(cur.get("id", -1)) if cur else -1
        if sid >= 0:
            return f"speaker:{sid}"
        name = str(cur.get("name", "") or "").strip()
        if name:
            return f"hint:{_safe_key(name)}"
        return f"{source}:unknown"

    # ---------- speaker_id から person_id への移行 (Phase 6E) ----------
    #
    # **鍵を作る場所は `_relationship_speaker_key` の1つだけ**なので、
    # そこへ Resolver を挟めば、20箇所以上ある呼び出し側を書き換えずに
    # 段階的に切り替えられる。書き換えなかった場所が古い鍵のまま
    # 動き続ける、という壊れ方を避けたい。

    def participant_ref(self, source: str = "local"):
        """いま喋っている人を、**legacy と person の両方**で指す。

        `person_id` が決まらなくても `speaker_id` を失わない。
        落とすと、解決できなかった相手の関係値が「不明な誰か」へ混ざる。
        """
        from neuro_voice.mind.migration import participant_ref

        speaker_key = self._relationship_speaker_key(source)
        runtime = getattr(self, "_identity_runtime", None)
        if runtime is None:
            return participant_ref(speaker_id=speaker_key)
        try:
            from neuro_voice.cognition.identity import VoiceIdentity

            cur = self._current_speaker or {}
            sid = int(cur.get("id", -1)) if cur else -1
            if sid < 0:
                return participant_ref(speaker_id=speaker_key)
            resolution = runtime.resolve_identity(
                voice=VoiceIdentity(voiceprint_id=str(sid),
                                    confidence=float(cur.get("score", 0.0) or 0.0)),
                event_id=f"turn:{speaker_key}")
            return participant_ref(speaker_id=speaker_key, resolution=resolution)
        except Exception:  # noqa: BLE001 — 解決できなくても会話は続く（第17条）
            logger.debug("参加者の解決に失敗（legacyで続行）", exc_info=True)
            return participant_ref(speaker_id=speaker_key)

    def relationship_resolver(self):
        """関係値の読み書きを集める1箇所。**無ければ作る。**"""
        if getattr(self, "_relationship_resolver", None) is None:
            from neuro_voice.mind.migration import (
                IdentityMigration, RelationshipResolver, resolve_mode,
            )
            from neuro_voice.mind.relationship import RelationshipStore

            mode = resolve_mode(self._cfg)
            person = None
            if mode is not None and str(mode) != "disabled":
                # **既存ファイルとは別に持つ。** 同じ形式・同じ軸・同じ減衰。
                person = RelationshipStore(
                    self._data_dir / f"person_relationships_{self._persona_key}.json",
                    self._cfg, persona_id=self._persona_key)
            runtime = getattr(self, "_identity_runtime", None)
            self._relationship_resolver = RelationshipResolver(
                self._relationships, person,
                resolver=runtime.resolver() if runtime is not None else None,
                mode=mode)
            self._identity_migration = IdentityMigration(
                self._relationship_resolver, journal=self._store)
            with contextlib.suppress(Exception):
                self._identity_migration.hydrate()
        return self._relationship_resolver

    def identity_migration(self):
        self.relationship_resolver()
        return self._identity_migration

    def attach_identity_runtime(self, runtime) -> None:
        """`InitiativeRuntime` を繋ぐ。**人物の解決はあちらが正本。**"""
        self._identity_runtime = runtime
        self._relationship_resolver = None
        self._identity_migration = None
        self._migration_jobs = None

    def tool_runtime(self):
        """道具の一式 (Phase 7)。**既定は全部止まっている。**

        `Mind` が持つのは、実行の記録を残す先（`mind.db`）と設定が
        ここに揃っているから。**新しい保存先を作らない**（第35項）。
        """
        runtime = getattr(self, "_tool_runtime", None)
        if runtime is None:
            from neuro_voice.cognition.tool_runtime import ToolRuntime

            runtime = ToolRuntime(self._cfg, journal=self._store)
            self._tool_runtime = runtime
        return runtime

    def _ensure_person_store(self):
        resolver = self.relationship_resolver()
        if resolver.person is None:
            from neuro_voice.mind.relationship import RelationshipStore

            resolver.person = RelationshipStore(
                self._data_dir / f"person_relationships_{self._persona_key}.json",
                self._cfg, persona_id=self._persona_key)
        return resolver

    def set_migration_mode(self, mode) -> str:
        """段階を1つ進める／戻す。**一度に1つだけ。**

        検証なしの直接指定。UI からは `request_migration()` を使うこと
        ——あちらは遷移の可否と昇格条件を見る。
        """
        return str(self._ensure_person_store().set_mode(mode))

    def migration_speaker_keys(self) -> list[str]:
        """移行の対象になりうる `speaker_id` の一覧。"""
        keys = list(self._relationships.all_snapshots())
        return [key for key in keys if str(key).startswith("speaker:")]

    def migration_job_runner(self):
        if getattr(self, "_migration_jobs", None) is None:
            from neuro_voice.mind.migration import MigrationJobRunner

            self._migration_jobs = MigrationJobRunner(
                self.identity_migration(), store=self._store)
            # **起動時に前回落ちた作業を拾う。** 勝手に再開はしない。
            with contextlib.suppress(Exception):
                self._migration_jobs.recover()
        return self._migration_jobs

    def migration_dry_run(self) -> dict[str, Any]:
        """**何も変えずに**、移せるかどうかだけ見る。"""
        self._ensure_person_store()
        return self.identity_migration().dry_run(
            self.migration_speaker_keys()).snapshot()

    def request_migration(self, operation: str, *, confirmed: bool = False,
                          ) -> dict[str, Any]:
        """UI からの操作を1箇所で受ける。

        **UIのボタンを隠すだけでは足りない。** 押せなくしても別の経路から
        呼べば通るので、遷移の可否も昇格条件もここで見直す。
        """
        from neuro_voice.mind.migration import MigrationMode, coerce_mode

        self._ensure_person_store()
        migration = self.identity_migration()
        runner = self.migration_job_runner()
        action = str(operation or "").strip().lower()
        if action == "dry_run":
            return {"ok": True, "operation": action,
                    "result": self.migration_dry_run()}
        if action == "resume":
            if not confirmed:
                return self._needs_confirmation(action, migration)
            job = runner.resume(self.migration_speaker_keys())
            if job is None:
                return {"ok": False, "operation": action, "reason": "nothing_to_resume"}
            return {"ok": True, "operation": action, "job": job.snapshot()}
        blocked = runner.blocked
        if blocked is not None:
            # **走っている間も、中断中も、新しい作業を受けない。**
            # 中断を無視して始めると、どこまで進んだのか分からなくなる。
            if blocked.resumable and action not in {"resume", "legacy_rollback"}:
                return {"ok": False, "operation": action,
                        "reason": "interrupted_job_pending",
                        "job": blocked.snapshot()}
            if blocked.running:
                return {"ok": False, "operation": action, "reason": "job_running",
                        "job": blocked.snapshot()}
        if action == "resync":
            if not confirmed:
                return self._needs_confirmation(action, migration)
            return {"ok": True, "operation": action, "result": migration.resync()}
        if action == "migrate":
            if not confirmed:
                return self._needs_confirmation(action, migration)
            job = runner.start("migrate", self.migration_speaker_keys())
            return {"ok": True, "operation": action, "job": job.snapshot()}
        target = coerce_mode(action)
        if str(target) != action:
            return {"ok": False, "operation": action, "reason": "unknown_operation"}
        verdict = migration.can_transition(target)
        if not verdict.allowed:
            return {"ok": False, "operation": action, "reason": verdict.reason,
                    "blockers": list(verdict.blockers)}
        risky = target in {MigrationMode.DUAL_WRITE, MigrationMode.PERSON_PRIMARY,
                           MigrationMode.LEGACY_ROLLBACK}
        if risky and not confirmed:
            return self._needs_confirmation(action, migration, target=target)
        if target is MigrationMode.LEGACY_ROLLBACK:
            # **戻す時は、中断していた作業を諦める。** 残しておくと
            # 「途中で止まっている」と言い続けて、次に進めなくなる。
            with contextlib.suppress(Exception):
                runner.cancel_recovered(reason="legacy_rollback")
        return {"ok": True, "operation": action,
                "mode": self.set_migration_mode(target)}

    def _needs_confirmation(self, action: str, migration, target=None) -> dict[str, Any]:
        """確認画面に出す材料。**戻し方も一緒に返す。**"""
        resolver = self.relationship_resolver()
        status = migration.status()
        return {
            "ok": False, "operation": action, "reason": "needs_confirmation",
            "confirm": {
                "from": str(resolver.mode),
                "to": str(target) if target else action,
                "targets": len(self.migration_speaker_keys()),
                "conflicts": status["by_status"].get("conflict", 0),
                "partial_failures": resolver.counters["write_person_failed"],
                "how_to_undo": "legacy_rollback",
            },
        }

    def relationship_migration_status(self) -> dict[str, Any]:
        """UI と診断用。**関係値そのものは出さない**（第12条）。"""
        resolver = self.relationship_resolver()
        migration = self._identity_migration
        identity = getattr(self._identity_runtime, "_resolver", None)
        links = getattr(identity, "links", {}) if identity else {}
        confirmed = sum(1 for item in links.values() if item.usable)
        conflicted = sum(1 for item in links.values()
                         if str(item.status) == "conflicted")
        speakers = self.migration_speaker_keys()
        return {
            "mode": str(resolver.mode),
            "speakers": len(speakers),
            "confirmed_links": confirmed,
            "unresolved": max(0, len(speakers) - confirmed),
            "conflicts": conflicted,
            "shadow_difference": len(resolver.last_difference),
            "dual_write_mismatch": resolver.counters["write_person_failed"],
            "partial_failures": resolver.counters["write_person_failed"],
            "resolver": resolver.status(),
            "migration": migration.status(),
            "job": self.migration_job_runner().status(),
            "allowed_next": [
                str(item) for item in sorted(
                    _allowed_next(resolver.mode), key=str)],
        }

    def identity_scope_for(self, ref=None):
        """その人物に紐づく `speaker_id` の範囲。**過去は移さない。**"""
        from neuro_voice.mind.migration import identity_scope

        reference = ref if ref is not None else self.participant_ref()
        identity = getattr(self._identity_runtime, "_resolver", None)
        return identity_scope(reference, identity)

    def _speaker_relationship_snapshot(self, speaker_id: int) -> dict[str, Any]:
        """画面へ出す1人分の関係値。**Resolver を通す。**

        移行の段階に応じて legacy か person かが変わる。直接
        `RelationshipStore` を読むと、`person_primary` の時に
        画面だけ古い値を出し続ける。
        """
        from neuro_voice.mind.migration import ParticipantIdentityRef

        legacy_key = f"speaker:{int(speaker_id)}"
        try:
            resolver = self.relationship_resolver()
        except Exception:  # noqa: BLE001 — 表示で会話を止めない（第17条）
            return self._relationships.snapshot(legacy_key)
        reference = ParticipantIdentityRef(speaker_id=legacy_key)
        identity = getattr(self._identity_runtime, "_resolver", None)
        if identity is not None:
            with contextlib.suppress(Exception):
                from neuro_voice.cognition.identity import VoiceIdentity
                from neuro_voice.mind.migration import participant_ref

                reference = participant_ref(
                    speaker_id=legacy_key,
                    resolution=identity.resolve(voice=VoiceIdentity(
                        voiceprint_id=str(speaker_id), confidence=.9)))
        with contextlib.suppress(Exception):
            return resolver.snapshot(reference)
        return self._relationships.snapshot(legacy_key)

    def _resolved_speaker_rows(self) -> list[dict[str, Any]]:
        with contextlib.suppress(Exception):
            return self.speaker_status_rows()
        return []

    def speaker_status_rows(self) -> list[dict[str, Any]]:
        """UI 用の話者一覧。**Identity の解決状態を一緒に見せる。**

        表示のためだけに関係値も Identity も更新しない。
        """
        from neuro_voice.cognition.identity import VoiceIdentity

        identity = getattr(self._identity_runtime, "_resolver", None)
        rows: list[dict[str, Any]] = []
        for profile in (self._speakers.list() if self._speakers else []):
            speaker_id = int(profile.get("id", -1))
            entry = {
                "speaker_id": speaker_id,
                "name": str(profile.get("name", "") or ""),
                "auto_name": bool(profile.get("auto_name", True)),
                "person_id": "", "resolution": "legacy_speaker",
            }
            if identity is not None and speaker_id >= 0:
                with contextlib.suppress(Exception):
                    verdict = identity.resolve(
                        voice=VoiceIdentity(voiceprint_id=str(speaker_id),
                                            confidence=.9))
                    entry["person_id"] = verdict.person_id
                    entry["resolution"] = _display_resolution(verdict, identity,
                                                              speaker_id)
            rows.append(entry)
        return rows

    def observe_dialogue_turn(
        self,
        user_text: str,
        emotion: str | None = None,
        emotion_confidence: float = 0.0,
        source: str = "local",
    ) -> dict[str, Any]:
        """Record only compact interaction signals before the LLM responds."""
        # Reflection uses the same local LLM as the realtime reply.  Abort an
        # in-flight reflection as soon as a new turn is ready, otherwise Ollama
        # may serialize the reply behind a background "growth" request.
        self.prioritize_realtime_turn()
        group = bool(
            self._privacy_context.get("group", False)
            if source == "discord" else False
        )
        privacy = self._privacy.classify(user_text, group=group)
        return self._dialogue.observe_turn(
            self._dialogue_user_key(source), user_text,
            emotion=emotion, emotion_confidence=emotion_confidence,
            source=source, group=group,
            allow_adaptive_store=bool(privacy.should_store),
        )

    def prioritize_realtime_turn(self) -> bool:
        """Yield an in-flight reflection LLM request to a live conversation.

        Only the cancellable LLM-generation phase is interrupted.  Once a
        reflection has started applying its parsed result, it is allowed to
        finish its small persistence work so personality state cannot be left
        half-written.
        """
        task = self._reflect_task
        if (
            not self._cancel_reflection_on_activity
            or not self._reflection_generating
            or task is None
            or task.done()
        ):
            return False
        task.cancel()
        logger.info("内省LLMを中断し、リアルタイム応答を優先します")
        return True

    def dialogue_allows_tool(
        self, tool: str, user_text: str, *, source: str = "local",
    ) -> tuple[bool, str]:
        if tool == "search" and self._game_session.active:
            return False, "active_game_profile_uses_local_manual"
        kernel = self.conversation_tool_policy(user_text, source=source)
        if tool == "search" and not bool(kernel.get("allow_search", True)):
            return False, str(kernel.get("reason") or "conversation_kernel_denied")
        return self._dialogue.should_use_tool(tool, user_text)

    def conversation_voice_direction(self, source: str = "local") -> dict[str, Any]:
        """Planner-selected voice direction shared by local and Discord TTS."""
        return self._dialogue.voice_direction(self._dialogue_user_key(source))

    def conversation_expression_plan(self, source: str = "local") -> dict[str, Any]:
        """Observable expression intent; avatar execution is deliberately absent."""
        return self._dialogue.expression_plan(
            self._dialogue_user_key(source), source=source,
        )

    def temporal_self_context(self) -> str:
        return self._temporal_self.prompt_context()

    def temporal_self_snapshot(self) -> dict[str, Any]:
        return self._temporal_self.snapshot()

    # ---------- Activity fact grounding ----------

    def activity_handle_final_input(
        self, text: str, *, actor_id: str, source: str, utterance_id: str | None = None,
    ) -> ActivityOutcome:
        """Submit a speaker-resolved STT final to the activity ledger.

        Callers must never pass STT partials here; this keeps partial recognition out
        of canonical facts and makes the same contract available locally and Discord.
        """
        return self._activities.handle_final_input(
            text, actor_id, source=source, utterance_id=utterance_id, is_final=True,
        )

    def activity_grounded_context(self) -> str | None:
        return self._activities.grounded_context()

    def activity_snapshot(self) -> dict[str, Any]:
        return self._activities.snapshot()

    def activity_active(self) -> bool:
        """Return whether canonical activity input must take precedence."""
        return bool(self._activities.active)

    def activity_validate_assistant_text(
        self, text: str, *, generation_id: str | None = None,
    ) -> tuple[str, str | None]:
        result, reason = self._activities.validate_and_commit_assistant_text(
            text, generation_id=generation_id,
        )
        return result.value, reason

    def activity_retry_context(self) -> str | None:
        return self._activities.retry_context()

    # ---------- Selected game profile ----------

    def game_profile_handle_final_input(
        self, text: str, *, actor_id: str, source: str, utterance_id: str | None = None,
    ) -> ActivityOutcome:
        """Apply the same game-session rules on local microphone and Discord."""
        return self._game_session.handle_final_input(
            text, actor_id=actor_id, source=source, utterance_id=utterance_id,
        )

    # ---------- Cognitive kernel ----------

    #: つらさの手がかり。`ResponseDirector._SUPPORT` と同じ語を使い、
    #: 判定を2箇所で別々に持たない。
    _DISTRESS = re.compile(
        r"(?:つら|辛い|疲れ|しんど|困っ|不安|悲し|むかつ|最悪|失敗|"
        r"できなかった|自信がな|間違ってる気が|怖い)"
    )

    def cognitive_state(
        self, user_text: str = "", *, source: str = "local",
        input_confidence: float = 1.0, danger: float = 0.0,
        interrupted_response: str = "", response_required: bool = False,
        response_requirement_source: str = "",
    ):
        """行動選択のための統合ビュー。**新しい保管場所は作らない。**

        関係性・感情・作業記憶・義務は今までどおり各モジュールが持ち主で、
        ここは読み取って1つの形へ並べるだけ（第5条）。
        """
        from neuro_voice.cognition import build_state

        profile = self._dialogue._user(self._dialogue_user_key(source))
        working = dict(WorkingMemory.snapshot(profile) or {})
        # ConversationKernel owns addressability/response intent. Preserve its
        # compact result for the Action Selector; never derive this again from
        # a second, text-only heuristic.
        working["response_required"] = bool(response_required)
        working["response_requirement_source"] = str(response_requirement_source or "")[:80]
        # Only durable, prior open questions may become recall obligations.
        # ConversationKernel duties (``respond``/``answer``) are created for
        # the current input and therefore must never trigger memory lookup.
        now = time.time()
        obligation_contexts: list[dict[str, Any]] = []
        for index, item in enumerate(list(working.get("open_questions", []))[:3]):
            if not isinstance(item, dict) or bool(item.get("resolved", False)):
                continue
            due_at = float(item.get("expiry", 0.0) or 0.0)
            topic = str(item.get("topic", "") or "")
            question = str(item.get("question", "") or "")
            if not (topic or question):
                continue
            obligation_contexts.append({
                "obligation_id": f"working:{source}:{item.get('created_turn', index)}",
                "topic_ids": (topic,) if topic else (),
                "goal": question[:60], "status": "open", "priority": 0.5,
                "created_at": float(item.get("created_at", 0.0) or 0.0),
                "due_at": due_at, "persona_id": self._persona_key,
                "source_event_ids": tuple(item.get("source_event_ids", ()) or ()),
            })
        obligations = [str(item["goal"]) for item in obligation_contexts]
        # 「どう思う？」だけで対象が決まらず、話題候補が複数あるなら聞き返す側へ。
        if self._VAGUE.search(str(user_text or "").strip()):
            working["ambiguity"] = min(
                1.0, .4 + .3 * len(list(working.get("open_questions", []))[:2]),
            )
        return build_state(
            working_memory=working,
            relationship=self._relationships.snapshot(
                self._relationship_speaker_key(source),
            ),
            # **感情は行動選択が読める形で渡す。**
            #
            # 以前はここへ `TemporalSelf.snapshot()` を丸ごと渡していた。
            # 感情はその中の `"affect"` の下に入っていて、しかも
            # `AffectState` に `irritation` という軸は無い（`frustration`）。
            # そのため `CognitiveState.irritation` は常に 0.0 で、
            # 苛立ち時の抑制も冗談の罰も一度も効いていなかった。
            affect=self._cognition_affect(),
            obligations=tuple(obligations),
            obligation_contexts=tuple(obligation_contexts),
            active_persona_id=str(self._persona_key),
            goal=str(working.get("conversation_goal") or ""),
            user_state=self._user_affect(user_text),
            world_state={
                "danger": float(danger),
                "group": bool(self._privacy_context.get("group", False)),
                "interrupted_response": str(interrupted_response or ""),
            },
            uncertainty={"input": float(input_confidence)},
            recent_actions=tuple(self._recent_actions),
            self_state=self._persona_traits_for_cognition(),
        )

    #: 苛立ち。つらさとは別に扱う——寄り添う相手と、待たせている相手は違う。
    _FRUSTRATION = re.compile(
        r"(?:イライラ|いらいら|むかつ|うざ|しつこい|さっきから|何度も言|"
        r"だから[、。]|そうじゃなく|違うって)"
    )
    #: 打ち切りの合図。**これが出たら話を広げない。**
    _END_SIGNAL = re.compile(
        r"^(?:もう)?(?:いい|いいよ|大丈夫|わかった|オッケー|ok)[。！!、\s]*$|"
        r"もういいよ|もういい[。！!\s]*$|やめとく|また今度|終わりにしよう"
    )
    #: 「もういいよ、それで」型。**打ち切りではなく同意**なので、無視しない。
    _AFFIRMATIVE_CLOSURE = re.compile(
        r"(?:それで|そっちで|そのまま|うん)[、。\s]*(?:いい|OK|オッケー)|"
        r"(?:もう)?いいよ[、。\s]*それで|それでいい"
    )
    #: 指示語だけで対象が決まらない発話。候補が複数あれば聞き返す側へ寄せる。
    _VAGUE = re.compile(r"^(?:それ|これ|あれ|どう思う|どうかな)[はがを、。？?\s]*$|どう思う[？?]?$")

    def _user_affect(self, user_text: str) -> dict[str, Any]:
        """発話から読み取れる相手の状態。**すべて推定であって申告ではない。**"""
        value = str(user_text or "").strip()
        ending = bool(self._END_SIGNAL.search(value))
        return {
            "distress": .7 if self._DISTRESS.search(value) else .0,
            "frustration": .6 if self._FRUSTRATION.search(value) else .0,
            # 打ち切りの合図が出たら、続けたい度合いは低いとみなす。
            "engagement": .1 if ending else .5,
            "end_signal": .9 if ending else .0,
            "closure_is_affirmative": bool(
                self._AFFIRMATIVE_CLOSURE.search(value)),
        }

    def _cognition_affect(self) -> dict[str, Any]:
        """行動選択が読む感情。**2つの持ち主を1つの形へ揃える。**

        `TemporalSelf`（声の表情づけ）と `InternalStateService`（行動選択用の
        短期感情）は別の持ち主のまま。ここは読み方を1箇所へ寄せるだけで、
        どちらかへ書き戻すことはしない。
        """
        merged: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            merged.update((self._temporal_self.snapshot() or {}).get("affect") or {})
        if self._internal.enabled and self._internal.affect_enabled:
            with contextlib.suppress(Exception):
                merged.update(self._internal.snapshot().affect)
        return merged

    def _persona_traits_for_cognition(self) -> dict[str, Any]:
        """好奇心などの人格軸。無ければ空でよい（既定値で埋めない）。"""
        with contextlib.suppress(Exception):
            return dict(self._dialogue._persona_traits())
        return {}

    def apply_cognitive_decision(
        self, decision, *, source: str = "local", response_id: str = "",
    ) -> bool:
        """選ばれた行動を TurnFrame へ反映する。

        書き換え口は `guarded_transition` のみ。`response_goal` /
        `response_shape` は保護フィールドなので、理由を必ず添える。
        """
        try:
            from neuro_voice.cognition.bridge import frame_changes

            changes = frame_changes(decision)
            if changes is None:
                return False
            return self._kernel.guarded_transition(
                changes, source=source, response_id=response_id or None,
                transition_reason=f"cognitive_kernel:{decision.decision_reason}"[:120],
            )
        except Exception:
            logger.exception("認知決定のTurnFrame反映でエラー")
            return False

    # -- エピソード記憶 (Phase 3) ---------------------------------------
    #
    # 保存先は既存の `MemoryStore`。判断は `cognition/episodic.py`。
    # ここは呼び出し口を1つに揃えるだけ——`Mind` の外から
    # `EpisodicMemoryService` を直接触ると、privacy と話者キーの扱いが分かれる。

    def recall_for_action(
        self, user_text: str = "", state=None, *, action=None, source: str = "local",
    ) -> RetrievalResult:
        """行動を選ぶ前の想起。**探す理由がある時だけ探す。**

        結果は行動候補の点数へ効く（`CognitiveKernel(memory_influence=...)`）。
        プロンプトへ足すだけでは、記憶が選択を変えたことにならない。
        """
        # 人物単位で引くかどうか (Phase 6F)。**記憶は書き換えない**——
        # 「この人物はどの speaker_id か」をその場で作って束ねるだけ。
        scope, scope_mode = None, "legacy"
        with contextlib.suppress(Exception):
            scope_mode = self._memory_scope_mode()
            if scope_mode != "legacy":
                scope = self.identity_scope_for(self.participant_ref(source))
        return self._episodes.retrieve(
            user_text, state, action=action,
            speaker_key=self._relationship_speaker_key(source),
            scope=scope, scope_mode=scope_mode,
        )

    def _memory_scope_mode(self) -> str:
        """記憶を人物単位で引くか。**移行の段階と揃える。**

        関係値が legacy を読んでいるのに記憶だけ人物単位、という
        食い違いを作らない。片方だけ先に進むと、どちらの結果を
        見ているのか分からなくなる。
        """
        from neuro_voice.mind.migration import MigrationMode

        mode = self.relationship_resolver().mode
        if mode in {MigrationMode.DISABLED, MigrationMode.LEGACY_ROLLBACK}:
            return "legacy"
        if mode is MigrationMode.PERSON_PRIMARY and bool(
                self._cfg.get("memory.identity_scope_retrieval_enabled", False)):
            return "person"
        if bool(self._cfg.get("memory.identity_scope_shadow_read_enabled", False)):
            return "shadow"
        return "legacy"

    def observe_episodes(
        self, user_text: str, reply: str = "", *, source: str = "local",
        input_confidence: float = 1.0, privacy_level: str = "normal",
        event_ids: tuple[str, ...] = (), provenance: dict[str, Any] | None = None,
    ) -> list:
        """ターンから保存候補を作り、ゲートへ通す。**大半は何も残らない。**"""
        verdicts = self._episodes.observe_turn(
            user_text, reply,
            speaker_key=self._relationship_speaker_key(source),
            session_id=self._directive_key_snapshot(),
            event_ids=event_ids, privacy_level=privacy_level,
            provenance=provenance,
            input_confidence=input_confidence,
        )
        # 明示的な発言は、こちらの仮説を検算する材料でもある。
        # **本人の発言が仮説より常に強い。**
        with contextlib.suppress(Exception):
            self._episodes.challenge_reflections(user_text)
        return verdicts

    def record_self_failure(self, pattern: str, *, detail: str = "", source: str = "local") -> list:
        """**自分の失敗を覚える。** 同じ状況で同じ行動を選びにくくなる。"""
        return self._episodes.observe_self_failure(
            pattern, detail=detail,
            speaker_key=self._relationship_speaker_key(source),
            session_id=self._directive_key_snapshot(),
        )

    def consolidate_memory(self, *, session_ended: bool = False,
                           idle_seconds: float = .0, explicit: bool = False):
        """溜まった経験から会話戦略の仮説を1つ作る。

        **応答の待ち時間へ入れない。** 会話が止まっている間だけ呼ぶ。
        できた仮説は次のターン以降にしか効かない。
        """
        reason = self._episodes.reflection_due(
            session_ended=session_ended, idle_seconds=idle_seconds,
            explicit_request=explicit,
        )
        if not reason:
            return None
        reflection = self._episodes.consolidate()
        if reflection is not None:
            self._emit("memory_reflection", reason=reason, **reflection.snapshot())
        return reflection

    def memory_snapshot(self) -> dict[str, Any]:
        return self._episodes.snapshot()

    # -- 持続する内面 (Phase 4) -----------------------------------------
    #
    # 短期感情は `InternalStateService`（RAM）、長期の関係は
    # `RelationshipStore`、好みは記憶から導く。**新しい感情モデルは作らない。**

    def internal_state(self, source: str = "local"):
        """いまの内面。**取る前に、経過時間ぶん基準値へ戻る。**"""
        return self._internal.snapshot(self._relationship_speaker_key(source))

    def internal_action_bias(self, state=None, *, source: str = "local"):
        """内面から行動候補への小さな補正。`WARN` と沈黙には触らない。"""
        key = self._relationship_speaker_key(source)
        return self._internal.action_bias(
            state if state is not None else self._internal.snapshot(key),
            participant_id=key,
        )

    def internal_planner_view(
        self, state=None, *, source: str = "local", end_signal: float = .0,
    ) -> dict[str, Any]:
        """Planner へ渡す `internal_state`。**粗いラベルだけ。**"""
        key = self._relationship_speaker_key(source)
        return self._internal.planner_view(
            state if state is not None else self._internal.snapshot(key),
            participant_id=key, end_signal=end_signal,
        )

    def observe_internal_outcome(
        self, *, decision=None, outcome=None, cognitive_state=None,
        source: str = "local", event_id: str = "", correction: bool = False,
    ) -> list:
        """**結果が出てから**内面を動かす。予測で先に動かさない。"""
        key = self._relationship_speaker_key(source)
        # 話者が特定できていない時は、特定の人の長期値へ触らせない。
        known = bool(self._current_speaker and int(self._current_speaker.get("id", -1)) >= 0)
        deltas = self._internal.observe_outcome(
            decision=decision, outcome=outcome, cognitive_state=cognitive_state,
            participant_id=key if known else "", event_id=event_id,
            correction=correction,
        )
        return deltas

    def observe_internal_preference(self, subject: str, valence: float, *, memory_id: int = 0):
        return self._internal.observe_preference(subject, valence, memory_id=memory_id)

    def internal_state_status(self) -> dict[str, Any]:
        return self._internal.status()

    # -- Local と Discord の共通意味論 (第19条) --------------------------
    #
    # **片側だけ実装して完了としない。** ターンが終わった後にやることを
    # ここへ集めておけば、呼び出し側が1行呼ぶだけで両側の意味が揃う。
    # 経路ごとに手順を書くと、必ず片方だけ直されて食い違う。

    def close_turn(
        self, user_text: str, reply: str, *, source: str = "local",
        decision=None, outcome=None, cognitive_state=None,
        emotion: str | None = None, topic: str = "",
        input_confidence: float = 1.0, event_id: str = "",
    ) -> None:
        """1ターンの後始末。**Local と Discord で同じことをする。**

        * 会話ログ・人格・関係性の更新（`record_turn`）
        * 経験の保存（Phase 3）— `record_turn` の中で走る
        * 内面の更新（Phase 4）— 結果が確定してから
        * 自分の失敗の記録（Phase 3）

        どれも機能フラグで止まっていれば何もしない。
        """
        with contextlib.suppress(Exception):
            self.record_turn(
                user_text, reply, emotion, topic=topic, source=source,
                event_id=event_id,
            )
        if decision is None and outcome is None:
            return
        with contextlib.suppress(Exception):
            self.observe_internal_outcome(
                decision=decision, outcome=outcome,
                cognitive_state=cognitive_state, source=source,
                event_id=event_id,
            )
        pattern = self_failure_pattern(decision, outcome)
        if pattern:
            with contextlib.suppress(Exception):
                self.record_self_failure(
                    pattern, source=source,
                    detail=f"{getattr(decision, 'selected_action', '')}:"
                           f"{getattr(outcome, 'status', '')}",
                )

    def record_cognitive_action(self, action: str) -> None:
        """直近の行動履歴。同じ行動の連投を点数で抑えるために要る。"""
        self._recent_actions.append(str(action))
        del self._recent_actions[:-8]

    def game_profile_check_pending(self) -> bool:
        """このターン、爆弾解除の答えを検算する必要があるか。

        Discordは既定では文単位で流してしまうので、これが真の時だけ
        全文を保持する経路へ入れる（Localは元から保持する。第19条）。
        """
        return self._game_session.check_pending()

    def game_profile_verify_reply(self, reply: str) -> tuple[str, str]:
        """爆弾解除でポッポが出した答えを、規則の側から検算する。

        (判定, 差し替える文) を返す。差し替える文が空ならそのまま話してよい。
        """
        return self._game_session.verify_reply(reply)

    def game_profile_grounded_context(self) -> str | None:
        return self._game_session.grounded_context()

    def game_profile_active(self) -> bool:
        return self._game_session.active

    def game_profile_snapshot(self) -> dict[str, Any]:
        return self._game_session.snapshot()

    def conversation_tool_policy(
        self, text: str, *, source: str = "local",
    ) -> dict[str, Any]:
        """Fast pre-tool gate using the same priorities as response generation."""
        current = self._kernel.current(source)
        if current is not None and current.normalized_input == str(text or "").strip():
            return current.search_decision.legacy()
        return self._kernel.tool_policy_for(
            text, activity_active=self._activities.active or self._game_session.active,
        )

    def conversation_kernel_snapshot(
        self, source: str = "local", *, public: bool = False,
    ) -> dict[str, Any]:
        return self._kernel.snapshot(source, public=public)

    def begin_conversation_turn(
        self,
        user_text: str,
        *,
        source: str,
        response_id: str,
        utterance_id: str,
        conversation_topic: str = "",
    ) -> dict[str, Any]:
        """Create the one authoritative frame before any tool can execute."""
        source = str(source or "local")
        self._had_user_turn = True
        group_context = (
            bool(self._privacy_context.get("group", False))
            if source == "discord" else False
        )
        relationship = self._relationships.snapshot(
            self._relationship_speaker_key(source)
        )
        dialogue = self._dialogue.snapshot(self._dialogue_user_key(source))
        reference_candidates: list[dict[str, Any]] = []
        if self._kernel.is_context_reference(user_text) and self._transcript_enabled:
            with contextlib.suppress(Exception):
                rows = self._store.recent_transcripts(
                    limit=int(self._cfg.get(
                        "conversation_kernel.recent_transcripts", 6,
                    ))
                )
                # MemoryStore returns chronological order.  A demonstrative
                # normally points at the immediately preceding exchange, so
                # make recency explicit instead of treating every row as an
                # equally plausible referent.
                count = max(1, len(rows))
                rows = [
                    {
                        **dict(row),
                        "score": max(
                            0.30,
                            0.98 - 0.14 * (count - index - 1),
                        ),
                    }
                    for index, row in enumerate(rows)
                ]
                if group_context:
                    audience = set(self._privacy_context.get("audience", set()))
                    rows = [
                        row for row in rows
                        if self._privacy.allow_context(
                            row, audience, group=True,
                        )
                    ]
                reference_candidates = rows
        frame = self._kernel.build(
            user_text,
            source=source,
            speaker_id=self._relationship_speaker_key(source),
            active_topic=conversation_topic,
            activity=self._activities.snapshot(),
            working_memory=dict(dialogue.get("working_memory") or {}),
            reference_candidates=reference_candidates,
            relationship=relationship,
            momentum=float(dialogue.get("momentum", .35)),
            group=group_context,
            active_directive=self._continuation.active(
                self._dialogue_user_key(source)
            ),
            utterance_id=utterance_id,
            conversation_id=self._dialogue_user_key(source),
            response_id=response_id,
        )
        self._last_kernel_source = source
        self._emit("conversation_kernel", frame=frame.snapshot(public=True))
        return frame.snapshot()

    def mark_kernel_search_execution(
        self,
        *,
        source: str,
        response_id: str,
        query: str,
        found: bool | None = None,
        error_code: str = "",
    ) -> bool:
        return self._kernel.record_search_execution(
            source=source, response_id=response_id, query=query,
            found=found, error_code=error_code,
        )

    def mark_kernel_generation(
        self, *, source: str, response_id: str, generation_id: str,
    ) -> bool:
        return self._kernel.record_generation(
            source=source, response_id=response_id,
            generation_id=generation_id,
        )

    def finalize_kernel_output(
        self, text: str, *, source: str, response_id: str,
    ) -> bool:
        return self._kernel.finalize_output(
            text, source=source, response_id=response_id,
        )

    def _conversation_kernel_context(
        self,
        user_text: str,
        *,
        relationship: dict[str, Any],
        conversation_topic: str,
        memories: list[dict[str, Any] | str] | None = None,
        rejected_memories: list[dict[str, Any] | str] | None = None,
        reference_candidates: list[dict[str, Any] | str] | None = None,
        source: str = "local",
        response_id: str = "",
        utterance_id: str = "",
    ) -> str:
        dialogue = self._dialogue.snapshot(self._dialogue_user_key(source))
        working = dict(dialogue.get("working_memory") or {})
        source = str(source or "local")
        self._last_kernel_source = source
        speaker_id = self._relationship_speaker_key(source)
        frame = self._kernel.find(
            response_id=response_id or None, source=source,
        )
        if frame is None or frame.normalized_input != str(user_text or "").strip():
            frame = self._kernel.build(
                user_text,
                source=source,
                speaker_id=speaker_id,
                active_topic=conversation_topic,
                activity=self._activities.snapshot(),
                working_memory=working,
                memories=memories or [],
                rejected_memories=rejected_memories or [],
                reference_candidates=reference_candidates or [],
                relationship=relationship,
                momentum=float(dialogue.get("momentum", .35)),
                group=(
                    bool(self._privacy_context.get("group", False))
                    if source == "discord" else False
                ),
                response_id=response_id or None,
                utterance_id=utterance_id or None,
                conversation_id=self._dialogue_user_key(source),
            )
        else:
            self._kernel.record_memory_influences(
                memories or [],
                rejected_memories=rejected_memories or [],
                source=source,
                response_id=response_id or None,
            )
        self._emit("conversation_kernel", frame=frame.snapshot(public=True))
        return self._kernel.prompt(frame)

    def observe_affect(self, label: str | None, *, intensity: float = 1.0, reason: str = "conversation") -> None:
        if bool(self._cfg.get("affect.enabled", True)):
            self._temporal_self.observe_affect(label, intensity=intensity, reason=reason)

    def smooth_voice_style(self, style, *, source: str, generation_id: str | None = None):
        if not bool(self._cfg.get("voice_affect.enabled", True)):
            return style
        return self._temporal_self.smooth_style(style, source=source, generation_id=generation_id)

    def temporal_heartbeat(self) -> None:
        self._temporal_self.heartbeat()

    def conversation_plan_snapshot(self, source: str = "local") -> dict[str, Any]:
        return self._dialogue.current_plan(self._dialogue_user_key(source))

    def enforce_conversation_sentence(
        self, text: str, *, source: str, response_id: str,
    ) -> str:
        """Apply the current response contract immediately before TTS."""
        return self._dialogue.enforce_output_sentence(
            self._dialogue_user_key(source), text,
            source=source, response_id=response_id,
        )

    def enforce_conversation_reply(
        self, text: str, *, source: str, response_id: str,
    ) -> str:
        """Canonical final reply for display, transcript and memory."""
        return self._dialogue.enforce_output_reply(
            self._dialogue_user_key(source), text,
            source=source, response_id=response_id,
        )

    def flush_conversation_sentence(
        self, *, source: str, response_id: str,
    ) -> str:
        """Flush a grounding-sensitive fragment held across TTS chunks."""
        return self._dialogue.flush_output_sentence(
            self._dialogue_user_key(source),
            source=source, response_id=response_id,
        )

    def autonomy_followups(self, source: str = "local") -> list[dict[str, str]]:
        """Return only grounded, unresolved follow-ups for the autonomy layer.

        These originate from explicit unfinished/project language in working
        memory.  No new curiosity or private inference is invented here.
        """
        try:
            snapshot = self._dialogue.snapshot(self._dialogue_user_key(source))
            questions = (snapshot.get("working_memory") or {}).get("open_questions") or []
            current_turn = int((snapshot.get("user") or {}).get("turns", 0) or 0)
            return [
                {
                    "topic": str(item.get("topic") or "")[:120],
                    "question": str(item.get("question") or "")[:220],
                    "source_excerpt": str(item.get("source_excerpt") or "")[:180],
                }
                for item in questions
                if (
                    isinstance(item, dict)
                    and item.get("source") == "unfinished_user_topic"
                    and item.get("topic")
                    and item.get("question")
                    and item.get("source_excerpt")
                    and current_turn - int(item.get("created_turn", current_turn) or current_turn) >= 4
                    and item.get("last_mentioned_turn") is None
                )
            ][-1:]
        except Exception:
            logger.exception("自発発話用の保留話題取得に失敗")
            return []

    def autonomy_context(self, source: str = "local") -> dict[str, Any]:
        """Return bounded, observable seeds for varied autonomous speech.

        This is not a transcript dump and contains no hidden reasoning.  It
        gives the autonomy policy alternatives to repeatedly asking about an
        unresolved past item: current focus, explicit interests and time.
        """
        try:
            snapshot = self._dialogue.snapshot(self._dialogue_user_key(source))
            working = snapshot.get("working_memory") or {}
            user = snapshot.get("user") or {}
            facts = user.get("facts") or {}
            interests = [
                str(item).strip()[:80]
                for item in (
                    list(facts.get("interests") or [])
                    + list(facts.get("current_projects") or [])
                )
                if str(item).strip()
            ][-5:]
            # Before the person has said anything this session there is no
            # current topic.  Offering last session's leftovers as "今の話題"
            # is how the assistant opened three sessions in a row with the same
            # remark about a microphone.
            fresh = bool(self._had_user_turn)
            hour = _dt.datetime.now().hour
            period = "朝" if 5 <= hour < 11 else "昼" if 11 <= hour < 17 else "夜" if 17 <= hour < 24 else "深夜"
            # Mood and the latest reflection give an idle move something of its
            # own to react to, instead of only echoing the user's last topic.
            mood_text = ""
            with contextlib.suppress(Exception):
                mood = self._personality.snapshot().get("mood") or {}
                valence = float(mood.get("valence", 0.0))
                arousal = float(mood.get("arousal", 0.0))
                tone = "上向き" if valence > .2 else "沈み気味" if valence < -.2 else "普通"
                energy = "元気" if arousal > .2 else "落ち着き" if arousal < -.2 else "平常"
                mood_text = f"{tone}・{energy}"
            reflection = ""
            with contextlib.suppress(Exception):
                recent = self._research.history(1)
                if recent:
                    reflection = str(
                        (recent[0] or {}).get("answer_summary")
                        or (recent[0] or {}).get("question")
                        or (recent[0] or {}).get("topic") or ""
                    )[:180]
            return {
                "active_theme": str(working.get("active_theme") or "")[:100] if fresh else "",
                "continuing_thought": (
                    str(working.get("continuing_thought") or "")[:180] if fresh else ""
                ),
                "recent_topics": (
                    [str(item)[:80] for item in (user.get("topics") or [])[-4:]]
                    if fresh else []
                ),
                "interests": interests,
                "time_context": f"{period}・{hour}時台",
                "mood": mood_text,
                "recent_reflection": reflection,
            }
        except Exception:
            logger.exception("自発発話用コンテキスト取得に失敗")
            return {}

    # ------------------------------------------------------------------
    # Behavior directives (continuing requests)
    # ------------------------------------------------------------------
    @property
    def continuation(self):
        """The shared ContinuationController.  Local and Discord use the same one."""
        return self._continuation

    def directive_conversation_id(self, source: str = "local") -> str:
        return self._dialogue_user_key(source)

    def active_directive(self, source: str = "local"):
        return self._continuation.active(self._dialogue_user_key(source))

    def _recent_assistant_text(self, turns: int = 2) -> str:
        """The last thing said, so hesitating every single turn can be damped."""
        with contextlib.suppress(Exception):
            rows = self._store.recent(max(1, int(turns))) or []
            return " ".join(str(row.get("assistant_text") or "") for row in rows)
        return ""

    def hesitation_permission(
        self, source: str = "local", *, transcript=None, memory_hits: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        """What is honestly unclear this turn, phrased as a bounded permission.

        Hesitating convincingly requires knowing *where* the difficulty is, and
        only the model writing the sentence knows that.  What the code can
        establish is whether there is any difficulty at all — from signals it
        already computed.  Certainty gets an explicit "do not hesitate", so a
        previous turn's permission does not linger.

        Returns ``(prompt_block, snapshot)``.  The snapshot is for the log.
        """
        from neuro_voice.dialogue.hesitation import assess, permission_block
        from neuro_voice.tts.delivery import count_gestures

        if not bool(self._cfg.get("conversation.hesitation.enabled", True)):
            return "", {}

        confidence, doubtful = 1.0, []
        if transcript is not None:
            with contextlib.suppress(Exception):
                uncertain = list(getattr(transcript, "uncertain_words", None) or ())
                doubtful = [str(word.text) for word in uncertain][:2]
                words = list(getattr(transcript, "words", None) or ())
                if words:
                    confidence = sum(
                        float(getattr(word, "probability", 1.0)) for word in words
                    ) / len(words)

        frame = None
        with contextlib.suppress(Exception):
            frame = self._kernel.current(source)
        reference_unresolved, reference_subject = False, ""
        factual, grounded, opinion = False, True, False
        if frame is not None:
            with contextlib.suppress(Exception):
                reference = frame.reference_resolution
                reference_unresolved = bool(reference.requires_clarification)
                reference_subject = str(
                    reference.ambiguity_reason or (reference.candidate_values or [""])[0] or ""
                )
            with contextlib.suppress(Exception):
                factual = bool(frame.search_decision.allowed)
                grounded = not bool(frame.search_decision.blocked_by_context)
            with contextlib.suppress(Exception):
                opinion = "どう思う" in str(frame.normalized_input or "")

        assessment = assess(
            transcript_confidence=confidence,
            doubtful_words=doubtful,
            reference_unresolved=reference_unresolved,
            reference_subject=reference_subject,
            memory_requested=bool(memory_hits) or reference_unresolved,
            memory_hits=int(memory_hits),
            factual_question=factual,
            has_grounds=grounded,
            opinion_requested=opinion,
            settled_view=not opinion,
            recent_gestures=count_gestures(self._recent_assistant_text()),
            max_allowance=int(self._cfg.get("conversation.hesitation.max_gestures", 2)),
        )
        return permission_block(assessment), assessment.snapshot()

    @property
    def last_plan_metrics(self) -> dict[str, Any]:
        """Whether the last turn's plan reached the reply.  Measurement only."""
        return dict(getattr(self._dialogue, "last_plan_metrics", {}) or {})

    def directive_waiting_for_user(self, source: str = "local") -> bool:
        """Whether a turn-based directive has handed the floor to the person.

        The conversation layer needs this to avoid answering a game master's
        question with silence.
        """
        # Imported here for the same reason ContinuationController is: the
        # dialogue package imports Mind back (R-025).
        from neuro_voice.dialogue.directive import DirectiveStatus, InteractionMode

        directive = self._continuation.active(self._dialogue_user_key(source))
        return bool(
            directive is not None
            and directive.status is DirectiveStatus.WAITING_FOR_USER
            and bool(getattr(directive, "needs_user_input", False))
            and getattr(directive, "interaction_mode", None) in {
                InteractionMode.NARRATION,
                InteractionMode.CO_THINKING,
            }
        )

    def directive_plan_context(self, source: str = "local") -> dict[str, Any]:
        """Bounded material for interpreting a request as a plan."""
        context = self.autonomy_context(source)
        activity = self._activities.snapshot() or {}
        mood = {}
        with contextlib.suppress(Exception):
            mood = self._personality.snapshot().get("mood") or {}
        return {
            "interests": list(context.get("interests") or []),
            "activity_summary": (
                f"{activity.get('activity_name')} / {activity.get('status')}"
                if activity.get("activity_name") else ""
            ),
            "affect_summary": (
                f"valence={float(mood.get('valence', 0)):.2f} "
                f"arousal={float(mood.get('arousal', 0)):.2f}"
                if mood else ""
            ),
            "active_theme": str(context.get("active_theme") or ""),
        }

    def directive_segment_context(self, source: str = "local") -> dict[str, Any]:
        """Material a segment may use, already filtered for privacy."""
        plan_context = self.directive_plan_context(source)
        notes: list[str] = []
        with contextlib.suppress(Exception):
            for row in self._store.recent(4):
                text = str((row or {}).get("text") or "").strip()
                if text:
                    notes.append(text[:140])
        return {
            "interests": plan_context["interests"],
            "affect_summary": plan_context["affect_summary"],
            "memory_notes": notes,
        }

    # ------------------------------------------------------------------
    # Speaker identity (one person, several surfaces)
    # ------------------------------------------------------------------
    def merge_speakers(
        self, source_id: int, target_id: int, *,
        source_surface: str = "", target_surface: str = "",
    ) -> dict[str, Any]:
        """Declare two speaker profiles to be the same person.

        Identity is not cosmetic here: relationships, dialogue profiles and
        adaptive learning are all keyed by ``speaker:{id}``.  Two profiles mean
        the assistant genuinely knows one person as two strangers, with half a
        history each.  Merging moves every keyed store, not just the name.
        """
        if self._speakers is None:
            return {"ok": False, "message": "話者識別が無効です"}
        source_id, target_id = int(source_id), int(target_id)
        merged = self._speakers.merge(
            source_id, target_id,
            source_surface=source_surface, target_surface=target_surface,
        )
        if merged is None:
            return {"ok": False, "message": "統合できる話者が見つかりません"}
        moved: dict[str, bool] = {}
        with contextlib.suppress(Exception):
            moved["relationship"] = self._relationships.merge(
                f"speaker:{source_id}", f"speaker:{target_id}",
            )
        with contextlib.suppress(Exception):
            moved["dialogue"] = self._dialogue.merge_users(
                f"speaker:{source_id}", f"speaker:{target_id}",
            )
        with contextlib.suppress(Exception):
            # A directive filed under the profile that just disappeared has to
            # move too, or the session becomes unreachable.
            moved["directive"] = self._continuation.rekey(
                f"speaker:{source_id}", f"speaker:{target_id}",
            ) is not None
        self._speakers.save()
        if int((self._current_speaker or {}).get("id", -1)) == source_id:
            self._current_speaker = {**merged}
        self._emit("speakers_merged", **merged, moved=moved)
        logger.info(
            "話者統合完了: %s → %s (移行=%s)",
            merged.get("source_name"), merged.get("target_name"), moved,
        )
        return {"ok": True, "merged": merged, "moved": moved}

    def speaker_merge_candidates(self) -> list[dict[str, Any]]:
        """Profile pairs that look like the same person.  Suggestion only."""
        if self._speakers is None:
            return []
        minimum = float(self._cfg.get("speaker.merge_suggestion_threshold", 0.82))
        with contextlib.suppress(Exception):
            return self._speakers.merge_candidates(minimum=minimum)
        return []

    def note_speaker_alias(self, speaker_id: int, source: str, name: str) -> bool:
        """Remember what one surface calls this person (Discord nickname, etc.)."""
        if self._speakers is None:
            return False
        changed = self._speakers.set_alias(int(speaker_id), source, name)
        if changed:
            self._speakers.save()
        return changed

    def speaker_display_name(self, source: str = "local") -> str:
        """The name to use for the current speaker on this surface."""
        current = self._current_speaker or {}
        speaker_id = int(current.get("id", -1))
        if self._speakers is not None and speaker_id >= 0:
            name = self._speakers.display_name(speaker_id, source)
            if name:
                return name
        return str(current.get("name", "") or "")

    def speech_recognition_context(self, source: str = "local") -> dict[str, Any]:
        """Words currently in play, for biasing the recognizer toward them.

        Correcting what the recognizer hears beats correcting its output, and
        the words it most often gets wrong are exactly the ones this
        conversation has already established: names, the current game, the
        topic being discussed.
        """
        result: dict[str, list[str]] = {
            "recent_turns": [], "topics": [], "proper_nouns": [],
            "memory_terms": [], "activity_terms": [], "interests": [],
        }
        try:
            context = self.autonomy_context(source)
            theme = str(context.get("active_theme") or "").strip()
            if theme:
                result["topics"].append(theme[:40])
            result["topics"].extend(
                str(item)[:40] for item in (context.get("recent_topics") or [])
            )
            result["interests"] = [
                str(item)[:40] for item in (context.get("interests") or [])
            ]
        except Exception:
            logger.debug("STT文脈: 会話文脈の取得に失敗", exc_info=True)
        from neuro_voice.memory.persona import get_active_persona

        with contextlib.suppress(Exception):
            _key, persona = get_active_persona(self._cfg)
            persona_name = str((persona or {}).get("name") or "").strip()
            if persona_name:
                result["proper_nouns"].append(persona_name[:24])
        # Never feed every registered speaker to Whisper.  A list such as
        # "ポッポ チビ ゲスト4" gets hallucinated verbatim at the end of an
        # otherwise correct utterance.  Only the person speaking now is useful.
        with contextlib.suppress(Exception):
            name = self.speaker_display_name(source).strip()
            if name:
                result["proper_nouns"].append(name[:24])
        with contextlib.suppress(Exception):
            activity = self._activities.snapshot() or {}
            if bool(getattr(self._activities, "active", False)):
                for key in ("activity_name", "game_name", "title"):
                    value = str(activity.get(key) or "").strip()
                    if value:
                        result["activity_terms"].append(value[:40])
        with contextlib.suppress(Exception):
            result["recent_turns"] = [
                str((row or {}).get("user_text") or "")[:120]
                for row in self._store.recent_transcripts(limit=4)
            ]
        with contextlib.suppress(Exception):
            result["memory_terms"] = [
                str((row or {}).get("text") or "")[:120] for row in self._store.recent(4)
            ]
        return result

    def configure_conversation_features(self) -> None:
        self._dialogue.configure_features()

    def conversation_developer_status(self, source: str = "local") -> dict[str, Any]:
        return self._dialogue.developer_snapshot(self._dialogue_user_key(source))

    def mark_dialogue_tool_use(self, tool: str, user_text: str) -> None:
        self._dialogue.mark_tool_use(tool, user_text)

    # ---------- 話者識別 ----------

    async def identify_speaker(
        self, audio: np.ndarray, allow_new: bool = True,
        exclude_names: set[str] | None = None,
        min_match: float | None = None,
    ) -> dict[str, Any] | None:
        """発話音声から話者を識別する。失敗/短い発話は前回の相手を継続。

        allow_new=False のときは未登録の声を新規登録せず、
        {"unknown": True} を返す (動画音声などを弾くフィルタ用)。
        exclude_names: 照合候補から外す名前 (通話ループバックで本人を除外する等)。
        """
        if self._voiceprint is None or self._speakers is None:
            return None
        try:
            loop = asyncio.get_running_loop()
            vec = await asyncio.wait_for(
                loop.run_in_executor(self._ex, self._voiceprint.encode, audio),
                timeout=self._speaker_timeout,
            )
        except asyncio.TimeoutError:
            logger.info("話者識別がタイムアウト。前回の相手として続行")
            return self._current_speaker
        except Exception:
            logger.exception("話者識別でエラー")
            return self._current_speaker
        if vec is None:
            return self._current_speaker  # 1秒未満の発話などは判定しない
        profile = self._speakers.identify(
            vec, allow_new=allow_new, exclude_names=exclude_names, min_match=min_match)
        if profile is None and not allow_new:
            return {"unknown": True}  # 未登録の声 (登録はしない)
        if profile is not None:
            previous_key = self._directive_key_snapshot()
            self._current_speaker = profile
            self._follow_speaker_key(previous_key)
            self._speakers.save()
        return profile

    def set_speaker_hint(self, name: str) -> None:
        """外部ソース (Discord等) で話者が確定している場合に直接指定する。"""
        name = str(name).strip()[:24]
        if not name:
            return
        cur = self._current_speaker
        if cur is not None and cur.get("name") == name:
            cur["turns"] = int(cur.get("turns", 0)) + 1
            return
        previous_key = self._directive_key_snapshot()
        self._current_speaker = {
            "id": -1, "name": name, "auto_name": False,
            "turns": 1, "first_met": "", "hint": True,
        }
        self._follow_speaker_key(previous_key)

    def clear_speaker_context(self) -> None:
        """Clear a transient speaker context without changing saved profiles.

        A Discord account identifies the transport source, not necessarily the
        physical person.  Unknown direct audio must not inherit an old profile
        or create an account-name based memory association.
        """
        self._current_speaker = None

    def rename_speaker(self, speaker_id: int, name: str) -> bool:
        if self._speakers is None:
            return False
        ok = self._speakers.set_name(speaker_id, name, learned=False)
        if ok:
            self._speakers.save()
            cur = self._current_speaker
            if cur and int(cur.get("id", -1)) == int(speaker_id):
                self._current_speaker = {**cur, "name": name, "auto_name": False}
        return ok

    def forget_speaker(self, speaker_id: int) -> bool:
        if self._speakers is None:
            return False
        ok = self._speakers.forget(speaker_id)
        if ok:
            self._speakers.save()
            cur = self._current_speaker
            if cur and int(cur.get("id", -1)) == int(speaker_id):
                self._current_speaker = None
        return ok

    # ---------- 毎ターン: コンテキスト注入 ----------

    @property
    def last_recall(self) -> list[dict[str, Any]]:
        """Sanitized diagnostics for the most recent recall attempt."""
        return [dict(item) for item in self._last_recall]

    @property
    def last_retrieval_trace(self) -> dict[str, Any]:
        """Privacy-safe stage counters for the latest context retrieval."""
        return dict(self._last_retrieval_trace)

    def recall_response_contract(self, user_text: str) -> dict[str, str]:
        """Deterministic surface contract for explicit questions about memory.

        A missing recalled record is not evidence for an invented answer.  For
        the labelled passphrase form, return the stored fact directly rather
        than letting a streaming model substitute a similarly-shaped fiction.
        """
        if not _explicit_recall_request(user_text):
            return {}
        self._last_recall_evidence_memory_ids = []
        if not self._last_recall_records:
            self._last_retrieval_trace["recall_status"] = "NOT_FOUND"
            self._last_retrieval_trace["recall_llm_call_count"] = 0
            self._last_retrieval_trace["recall_evidence_memory_ids"] = []
            return {
                "status": "NOT_FOUND",
                "reply": f"その{_recall_label(user_text)}は覚えていないよ。" if _recall_label(user_text)
                else "そのことは覚えていないよ。",
            }
        for record in self._last_recall_records:
            for label, pattern in _LABELED_RECALL_FACTS:
                match = pattern.search(str(record.get("text") or ""))
                if not match:
                    continue
                fact = match.group(1)
                memory_id = int(record.get("id") or 0)
                self._last_recall_evidence_memory_ids = [memory_id] if memory_id > 0 else []
                self._last_retrieval_trace["recall_status"] = "FOUND"
                self._last_retrieval_trace["recall_llm_call_count"] = 0
                self._last_retrieval_trace["recall_evidence_memory_ids"] = list(
                    self._last_recall_evidence_memory_ids)
                return {
                    "status": "FOUND",
                    "required_fact": fact,
                    "reply": f"{label}は{fact}だよ。",
                }
        self._last_retrieval_trace["recall_status"] = "FOUND"
        self._last_retrieval_trace["recall_llm_call_count"] = 0
        self._last_retrieval_trace["recall_evidence_memory_ids"] = []
        return {
            "status": "FOUND",
            "reply": "その記憶は見つかったけれど、具体的な内容までは確かめられないよ。",
        }

    def set_privacy_context(self, *, group: bool, audience_user_ids=None) -> None:
        self._privacy_context = {"group": bool(group), "audience": {str(x) for x in (audience_user_ids or [])}}

    def participant_count(self) -> int:
        """いま場に何人いるか。**2値の近似をやめる。**

        `is_group` だけだと3人と5人の差が出ず、人数が増えるほど割り込みが
        高くつくという扱いができない。聞き手の一覧が分かるなら数える。
        """
        audience = self._privacy_context.get("audience") or set()
        if audience:
            # 自分は数に入れない。相手が1人なら1対1。
            return max(1, len(audience))
        return 2 if bool(self._privacy_context.get("group", False)) else 1

    def autonomy_privacy_scope(self, text: str) -> str:
        """Return the privacy scope that an autonomous event is allowed to use.

        The autonomy layer must receive the same boundary decision as memory
        retrieval.  In particular, a private/deletion-requested utterance must
        never become an open thread that can be brought up later in a group.
        """
        decision = self._privacy.classify(
            text, group=bool(self._privacy_context.get("group", False)),
        )
        return str(decision.visibility)

    def privacy_safe_output(self, text: str) -> str:
        return self._privacy.safe_output(text)

    async def build_context(
        self, user_text: str, *, include_recall: bool = True,
        retrieval_trigger_reason: str = "",
        conversation_topic: str = "", recall_timeout_s: float | None = None,
        source: str = "local",
        response_id: str = "",
        utterance_id: str = "",
    ) -> str | None:
        """人格状態 + 中期要約 + 想起した長期記憶をまとめた system 追記文を作る。"""
        sp = self._current_speaker
        # 相手ごとの親密度を内面描写に反映する (話者が特定できている場合)
        rel = None
        if sp is not None and self._speakers is not None:
            sid = int(sp.get("id", -1))
            if sid >= 0:
                rel = self._speakers.relationship(sid)
        parts = [self._personality.describe(rel=rel)]
        if bool(self._cfg.get("time_awareness.enabled", True)):
            parts.append(self._temporal.prompt_context(conversation_topic or user_text))
        if bool(self._cfg.get("temporal_self.enabled", True)):
            parts.append(self._temporal_self.prompt_context())
        game_context = self._game_session.grounded_context()
        if game_context:
            parts.append(game_context)
        if sp is not None:
            line = f"【いま話している相手】{sp['name']} (会話{sp.get('turns', 1)}回目"
            if sp.get("first_met"):
                line += f"、初対面は{sp['first_met']}"
            line += ")"
            if sp.get("auto_name") and self._ask_name and sp.get("turns", 0) <= 5:
                line += (
                    "\nまだ名前を知らない相手。会話の自然な流れで名前を聞いてみて。"
                    "名乗ってくれたらその名前で呼ぶ。"
                )
            parts.append(line)
        if bool(self._cfg.get("mind.relationship.enabled", True)):
            parts.append(self._relationships.prompt(self._relationship_speaker_key()))
        if self._session_summary:
            parts.append(f"【この会話のこれまで】\n{self._session_summary}")
        # Research is evidence, not a conversational fact.  It is supplied
        # only when explicitly asked for or when the current wording matches a
        # stored topic, and always keeps provisional/verified status visible.
        research_query = user_text.strip()
        history_request = bool(re.search(r"(?:最近|何を|その)?(?:調べた|検索結果|研究|知識|学習)", research_query))
        research_items = self._research.history(12)
        matched_research = [
            item for item in research_items
            if history_request or (item.get("title") and str(item.get("title")) in research_query)
        ][:4]
        if matched_research:
            lines = []
            for item in matched_research:
                confidence = item.get("confidence")
                confidence_note = f" confidence={confidence}" if confidence is not None else ""
                lines.append(f"- {item.get('title', '調査')}: status={item.get('status', 'UNKNOWN')}{confidence_note}")
            parts.append(
                "【自律研究の履歴・根拠の強さを保って利用】\n" + "\n".join(lines) +
                "\nVERIFIED/LEARNED以外は確定事実として言わず、調べた範囲・暫定情報だと明示する。"
            )
        if not include_recall:
            self._last_recall = []
            self._last_retrieval_trace = {
                "memory_trigger_result": "NOT_APPLICABLE",
                "memory_trigger_reason": retrieval_trigger_reason or "not_applicable",
                "retrieval_candidate_count": 0,
                "retrieval_after_scope_count": 0,
                "retrieval_after_lexical_count": 0,
                "retrieval_after_semantic_count": 0,
                "retrieval_ranked_count": 0,
                "retrieval_selected_count": 0,
                "recall_status": "NOT_APPLICABLE",
                "recall_llm_call_count": 0,
                "recall_evidence_memory_ids": [],
            }
            relationship_state = self._relationships.snapshot(self._relationship_speaker_key())
            parts.insert(0, self._conversation_kernel_context(
                user_text,
                relationship=relationship_state,
                conversation_topic=conversation_topic,
                source=source,
                response_id=response_id,
                utterance_id=utterance_id,
            ))
            dialogue_context = self._dialogue.prompt_context(
                self._dialogue_user_key(source), user_text,
                relationship=relationship_state,
                allow_recent_reply_context=not bool(
                    self._privacy_context.get("group", False),
                ),
                response_id=response_id,
                source=source,
                recall_requested=is_recall_request(user_text),
                recall_grounded=False,
                group=bool(
                    self._privacy_context.get("group", False)
                    if source == "discord" else False
                ),
                # **持続している内面を口調へ届ける。** Phase 4 から
                # 繋がっていなかった配線。フラグが off なら空 dict が渡る。
                internal_state=self.internal_planner_view(source=source),
            )
            if dialogue_context:
                parts.append(dialogue_context)
            return "\n\n".join(parts)
        recall_query = "\n".join(x for x in (conversation_topic.strip(), user_text.strip()) if x)
        self._last_recall = []
        self._last_recall_records = []
        self._last_recall_evidence_memory_ids = []
        context = self.persona_context
        active_persona = str(getattr(context, "persona_id", "") or "")
        bound_persona = str(getattr(self._store, "bound_persona_id", "") or "")
        self._last_retrieval_trace = {
            "memory_trigger_result": "QUERY_BUILT" if recall_query else "QUERY_NOT_BUILT",
            "memory_trigger_reason": "" if recall_query else "empty_query",
            "retrieval_query_feature_hash": hashlib.sha256(
                recall_query.encode("utf-8")).hexdigest()[:16] if recall_query else "",
            "memory_store_bound_persona_id": bound_persona,
            "memory_store_path_hash": hashlib.sha256(
                str(self._store._path).encode("utf-8")).hexdigest()[:16],
            "retrieval_candidate_count": 0,
            "retrieval_after_scope_count": 0,
            "retrieval_after_lexical_count": 0,
            "retrieval_after_semantic_count": 0,
            "retrieval_ranked_count": 0,
            "retrieval_selected_count": 0,
            "retrieval_rejection_reasons": [],
            "retrieval_representation_status_counts": {},
            "recall_status": "NOT_APPLICABLE",
            "recall_llm_call_count": 0,
            "recall_evidence_memory_ids": [],
        }
        if recall_query and (not active_persona or active_persona != bound_persona):
            self._last_retrieval_trace.update({
                "memory_trigger_result": "PERSONA_MISMATCH",
                "memory_trigger_reason": "context_store_mismatch",
                "retrieval_rejection_reasons": ["PERSONA_MISMATCH"],
            })
            recall_query = ""
        timeout_s = self._recall_timeout if recall_timeout_s is None else recall_timeout_s
        transcripts: list[dict] = []
        reference_transcripts: list[dict] = []
        try:
            # 想起は必ず時間制限付き。遅い場合 (モデルロード中など) は諦めて応答を優先する
            recalled, transcripts = await asyncio.wait_for(
                self._recall(recall_query), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.info("記憶の想起がタイムアウト (%.2fs)。今回はスキップ", timeout_s)
            recalled = []
        except Exception:
            logger.exception("記憶の想起でエラー")
            recalled = []
        # 「昨日/この前 何を話した?」型の質問は、日付で会話ログを直接引く
        # (意味検索は質問文に内容語が無いと当たらないため)。
        if self._transcript_enabled:
            window = temporal_query_window(user_text)
            if window is not None:
                with contextlib.suppress(Exception):
                    rows = self._store.transcripts_between(window[0], window[1], limit=200)
                    if len(rows) > self._transcript_temporal_max:
                        # 期間全体の流れが分かるよう、均等に間引く
                        step = len(rows) / self._transcript_temporal_max
                        rows = [rows[int(i * step)] for i in range(self._transcript_temporal_max)]
                    known = {int(t["id"]) for t in transcripts}
                    transcripts = [r for r in rows if int(r["id"]) not in known] + transcripts
            # Pronouns and elliptical follow-ups contain too few content words
            # for semantic/keyword recall. Resolve them against a small recent
            # window before considering any external search.
            if self._kernel.is_context_reference(user_text):
                with contextlib.suppress(Exception):
                    limit = int(self._cfg.get(
                        "conversation_kernel.recent_transcripts", 6,
                    ))
                    rows = self._store.recent_transcripts(limit=limit)
                    reference_transcripts = rows
                    known = {int(t["id"]) for t in transcripts}
                    transcripts = [
                        row for row in rows if int(row["id"]) not in known
                    ] + transcripts
        recalled = list(recalled or [])
        self._last_retrieval_trace["retrieval_candidate_count"] = len(recalled)
        self._last_retrieval_trace["retrieval_after_scope_count"] = len(recalled)
        if self._last_recall_mode.startswith("lexical"):
            self._last_retrieval_trace["retrieval_after_lexical_count"] = len(recalled)
            self._last_retrieval_trace["retrieval_representation_status_counts"] = {
                "NOT_REQUIRED" if self._embedder is None else "MISSING": len(recalled),
            }
        else:
            self._last_retrieval_trace["retrieval_after_semantic_count"] = len(recalled)
            self._last_retrieval_trace["retrieval_representation_status_counts"] = {
                "READY": len(recalled),
            }
        ranked, rejected_memory_uses = self._recall_policy.evaluate(
            recalled, current_text=user_text,
        )
        if bool(self._privacy_context.get("group", False)):
            audience = set(self._privacy_context.get("audience", set()))
            privacy_rejected = [
                item for item in ranked
                if not self._privacy.allow_context(
                    item.record, audience, group=True,
                )
            ]
            ranked = [
                item for item in ranked
                if self._privacy.allow_context(
                    item.record, audience, group=True,
                )
            ]
            rejected_memory_uses.extend([
                MemoryRejection(item.record, "PRIVACY_BOUNDARY")
                for item in privacy_rejected
            ])
            transcripts = [row for row in transcripts if self._privacy.allow_context(row, audience, group=True)]
            reference_transcripts = [
                row for row in reference_transcripts
                if self._privacy.allow_context(row, audience, group=True)
            ]
        self._last_recall = [
            {"id": int(item.record["id"]), "score": item.score, "mention_allowed": item.mention_allowed}
            for item in ranked
        ]
        self._last_recall_records = [dict(item.record) for item in ranked]
        self._last_retrieval_trace["retrieval_ranked_count"] = len(ranked)
        self._last_retrieval_trace["retrieval_selected_count"] = len(self._last_recall)
        if self._last_retrieval_trace.get("memory_trigger_result") == "QUERY_BUILT":
            self._last_retrieval_trace["memory_trigger_result"] = (
                "RETRIEVED" if self._last_recall else "RANK_BELOW_THRESHOLD"
            )
            if not self._last_recall:
                self._last_retrieval_trace["retrieval_rejection_reasons"] = [
                    "RANK_BELOW_THRESHOLD"
                ]
        speakable_memories: list[str] = []
        if ranked:
            def _memory_line(item) -> str:
                created = item.record.get("created_at")
                when = self._temporal.relative_at(float(created)) if created else "記録時期不明"
                return f"- [{when}に記録] {item.record['text']}"

            internal = "\n".join(_memory_line(item) for item in ranked)
            speakable = [_memory_line(item) for item in ranked if item.mention_allowed]
            speakable_memories = [
                str(item.record.get("text", ""))
                for item in ranked if item.mention_allowed and item.record.get("text")
            ]
            parts.append(
                "【関連する過去の記憶・内部用】\n" + internal +
                "\nこれらは会話の整合性を保つ内部情報。ユーザーが明示的に尋ねた場合、"
                "または自然さを確実に高める場合以外は、記憶の存在や内容を口に出さない。"
            )
            if speakable:
                parts.append("【自然に言及してよい高関連記憶】\n" + "\n".join(f"- {text}" for text in speakable))
        explicit_transcript_recall = (
            temporal_query_window(user_text) is not None
            or assistant_transcript_wording_required(user_text)
        )
        transcripts = dedupe_transcript_rows(
            transcripts,
            limit=self._transcript_temporal_max if explicit_transcript_recall else self._transcript_top_k,
        )
        reference_transcripts = dedupe_transcript_rows(reference_transcripts)
        if transcripts:
            def _transcript_line(row: dict) -> str:
                when = self._temporal.relative_at(float(row.get("created_at") or 0)) or "以前"
                return transcript_context_line(
                    row,
                    when=when,
                    include_assistant_wording=explicit_transcript_recall,
                )

            parts.append(
                "【過去の実際の会話の記録・内部用】\n"
                + "\n".join(_transcript_line(row) for row in transcripts)
                + (
                    "\nこれは過去回答の言い回しを再利用する例ではなく、現在の考えに使う話題の根拠。"
                    if not explicit_transcript_recall else
                    "\n過去に何を話したか聞かれたら、この記録を根拠に具体的に答えてよい。"
                )
                + "記録に無い部分は曖昧にごまかさず「そこまでは思い出せない」と言う。"
            )
        relationship_state = self._relationships.snapshot(self._relationship_speaker_key())
        kernel_memories = [
            {
                "text": str(item.record.get("text", "")),
                "score": float(item.score),
                "mention_allowed": bool(item.mention_allowed),
                "memory_id": str(item.record.get("id") or ""),
                "relevance": float(item.relevance),
                "freshness": float(item.freshness),
                "privacy_allowed": bool(item.privacy_allowed),
                "contradiction_status": str(item.contradiction_status),
                "influence_type": str(item.influence_type),
            }
            for item in ranked[:3]
            if item.record.get("text")
        ]
        if self._kernel.is_context_reference(user_text):
            # Make recent dialogue an explicit cause of this turn's reasoning,
            # not merely an unrelated prompt appendix.
            transcript_influences = [
                {
                    "text": (
                        f"{row.get('speaker') or 'ユーザー'}: "
                        f"{str(row.get('user_text') or '')[:80]} / "
                        f"私: {str(row.get('assistant_text') or '')[:100]}"
                    ),
                    "score": float(row.get("score", .90)),
                    "mention_allowed": True,
                    "memory_id": f"transcript:{row.get('id', '')}",
                    "influence_type": "conversation_reference",
                }
                for row in reference_transcripts[-3:]
            ]
            kernel_memories = transcript_influences + kernel_memories
        parts.insert(0, self._conversation_kernel_context(
            user_text,
            relationship=relationship_state,
            conversation_topic=conversation_topic,
            memories=kernel_memories,
            rejected_memories=[
                {
                    "memory_id": str(item.record.get("id") or ""),
                    "rejection_reason": str(item.reason),
                }
                for item in rejected_memory_uses
                if item.record.get("id") is not None
            ],
            reference_candidates=reference_transcripts,
            source=source,
            response_id=response_id,
            utterance_id=utterance_id,
        ))
        dialogue_context = self._dialogue.prompt_context(
            self._dialogue_user_key(source), user_text, relationship=relationship_state,
            memory_snippets=speakable_memories,
            allow_recent_reply_context=not bool(
                self._privacy_context.get("group", False),
            ),
            response_id=response_id,
            source=source,
            recall_requested=is_recall_request(user_text),
            recall_grounded=bool(ranked or transcripts or reference_transcripts),
            group=bool(
                self._privacy_context.get("group", False)
                if source == "discord" else False
            ),
            internal_state=self.internal_planner_view(source=source),
        )
        if dialogue_context:
            parts.append(dialogue_context)
        return "\n\n".join(parts)

    async def _recall(self, query: str) -> tuple[list[dict], list[dict]]:
        """(記憶, 会話ログ) のタプルを返す。"""
        query = (query or "").strip()
        if not query or query.startswith("("):
            return [], []  # 自発発話プロンプト等は想起しない
        loop = asyncio.get_running_loop()
        if self._embedder is not None:
            self._last_recall_mode = "semantic"
            return await loop.run_in_executor(self._recall_ex, self._recall_semantic, query)
        self._last_recall_mode = "lexical_no_embedder"
        return await loop.run_in_executor(self._recall_ex, self._recall_keyword, query)

    def _emb_matrix(
        self, query: str = "", query_embedding: np.ndarray | None = None,
    ) -> tuple[list[int], list[dict], np.ndarray]:
        if query and query_embedding is not None and hasattr(
            self._store, "search_episode_embeddings"
        ):
            rows = self._store.search_episode_embeddings(
                query_embedding.astype(np.float32, copy=False).tobytes(),
                dim=int(query_embedding.shape[0]),
                limit=max(40, self._top_k * 16),
                include_archived=True,
            )
            rows = [row for row in rows if row.get("embedding") is not None]
            ids = [r["id"] for r in rows]
            mat = (
                np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                if rows else np.zeros((0, 1), dtype=np.float32)
            )
            return ids, rows, mat
        if query:
            # Compatibility for alternate stores without the Stage M sidecar.
            rows = self._store.search_episodes(
                query, limit=max(40, self._top_k * 16), include_archived=True,
            )
            rows = [row for row in rows if row.get("embedding") is not None]
            ids = [r["id"] for r in rows]
            mat = (
                np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                if rows else np.zeros((0, 1), dtype=np.float32)
            )
            return ids, rows, mat
        if self._emb_cache is None:
            rows = self._store.all_embeddings()
            ids = [r["id"] for r in rows]
            mat = (
                np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                if rows else np.zeros((0, 1), dtype=np.float32)
            )
            self._emb_cache = (ids, rows, mat)
        return self._emb_cache

    def _transcript_matrix(
        self, query: str = "", query_embedding: np.ndarray | None = None,
    ) -> tuple[list[int], list[dict], np.ndarray]:
        if (query and query_embedding is not None and self._transcript_enabled
                and hasattr(self._store, "search_transcript_embeddings")):
            rows = self._store.search_transcript_embeddings(
                query_embedding.astype(np.float32, copy=False).tobytes(),
                dim=int(query_embedding.shape[0]),
                limit=max(24, self._transcript_top_k * 8),
            )
            rows = [row for row in rows if row.get("embedding") is not None]
            ids = [r["id"] for r in rows]
            mat = (
                np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                if rows else np.zeros((0, 1), dtype=np.float32)
            )
            return ids, rows, mat
        if query and self._transcript_enabled:
            rows = self._store.search_transcripts(
                query, limit=max(24, self._transcript_top_k * 8),
            )
            rows = [row for row in rows if row.get("embedding") is not None]
            ids = [r["id"] for r in rows]
            mat = (
                np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                if rows else np.zeros((0, 1), dtype=np.float32)
            )
            return ids, rows, mat
        if self._transcript_cache is None:
            rows = self._store.transcript_embeddings() if self._transcript_enabled else []
            ids = [r["id"] for r in rows]
            mat = (
                np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                if rows else np.zeros((0, 1), dtype=np.float32)
            )
            self._transcript_cache = (ids, rows, mat)
        return self._transcript_cache

    def _store_transcript_sync(self, user_text: str, reply: str, speaker: str,
                               emotion: str, topic: str) -> None:
        """会話ログ1件を embedding 付きで保存する (_recall_ex スレッドで実行)。"""
        try:
            emb_bytes, dim = None, None
            if self._embedder is not None:
                vec = self._embedder.encode([f"{user_text}\n{reply}"])[0]
                emb_bytes, dim = vec.tobytes(), int(vec.shape[0])
            self._store.add_transcript(
                user_text, reply, speaker=speaker, emotion=emotion, topic=topic,
                embedding=emb_bytes, dim=dim,
            )
            self._transcript_cache = None  # 次の想起で再構築
        except Exception:
            logger.exception("会話ログの保存に失敗")

    def _search_transcripts(self, q: np.ndarray, query: str = "") -> list[dict]:
        """クエリベクトルで会話ログを検索。古い記録ほど思い出しにくくする。"""
        if not self._transcript_enabled or self._transcript_top_k <= 0:
            return []
        ids, rows, mat = self._transcript_matrix(query, q)
        if not ids:
            return []
        sims = mat @ q
        order = np.argsort(-sims)[: self._transcript_top_k * 3]
        now = time.time()
        out: list[dict] = []
        for i in order:
            sim = float(sims[i])
            if sim < 0.74:  # 会話ログは量が多くノイズになりやすいので高めの閾値
                break
            age_days = max(0.0, now - float(rows[i]["created_at"])) / 86400.0
            if sim * math.exp(-age_days / 21.0) < 0.45:
                continue  # 人間らしく、古い会話ほど薄れる
            out.append({**rows[i], "score": sim})
            if len(out) >= self._transcript_top_k:
                break
        return out

    def _recall_semantic(self, query: str) -> tuple[list[dict], list[dict]]:
        q = self._embedder.encode([query], is_query=True)[0]
        transcripts = self._search_transcripts(q, query)
        ids, rows, mat = self._emb_matrix(query, q)
        if not ids:
            # Older persona DBs may have no vectors yet.  An enabled embedder
            # must not make their scoped text memories invisible.
            self._last_recall_mode = "lexical_fallback_no_embeddings"
            recalled, _ = self._recall_keyword(query)
            return recalled, transcripts
        sims = mat @ q
        order = np.argsort(-sims)[: self._top_k * 2]
        out = []
        for i in order:
            if sims[i] < self._min_score:
                break
            if is_recall_request(str(rows[i].get("text") or "")):
                continue  # Stored questions are prompts, never recall evidence.
            out.append({**rows[i], "score": float(sims[i])})
            if len(out) >= self._top_k:
                break
        if self._episodes.write_enabled:
            self._store.touch([r["id"] for r in out])
        return out, transcripts

    def _recall_keyword(self, query: str) -> tuple[list[dict], list[dict]]:
        """embedding なしのフォールバック: 文字バイグラムの重なりで検索。"""
        def bigrams(s: str) -> set[str]:
            s = re.sub(r"[\s、。!?!?・()()「」]+", "", s)
            return {s[i : i + 2] for i in range(len(s) - 1)}

        qb = bigrams(query)
        if not qb:
            return [], []
        scored = []
        # Stage M: load only bounded, ACL-filtered candidates from the
        # rebuildable index.  Retained source volume must not become prompt or
        # process-memory volume, while explicit old recalls remain reachable.
        for r in self._store.search_episodes(
            query,
            limit=max(20, self._top_k * 8),
            include_archived=True,
        ):
            if is_recall_request(str(r.get("text") or "")):
                continue  # A past question must not answer itself.
            tb = bigrams(r["text"])
            if not tb:
                continue
            # クエリ側のバイグラムがどれだけ記憶に含まれるか (短い記憶を不利にしない)
            score = len(qb & tb) / min(len(qb), len(tb))
            if score >= 0.12:
                scored.append({**r, "score": score})
        scored.sort(key=lambda r: -r["score"])
        out = scored[: self._top_k]
        if self._episodes.write_enabled:
            self._store.touch([r["id"] for r in out])
        transcripts: list[dict] = []
        if self._transcript_enabled and self._transcript_top_k > 0:
            t_scored = []
            for r in self._store.search_transcripts(
                query, limit=max(24, self._transcript_top_k * 8),
            ):
                tb = bigrams(f"{r['user_text']} {r['assistant_text']}")
                if not tb:
                    continue
                score = len(qb & tb) / min(len(qb), len(tb))
                if score >= 0.2:
                    t_scored.append({**r, "score": score})
            t_scored.sort(key=lambda r: -r["score"])
            transcripts = t_scored[: self._transcript_top_k]
        return out, transcripts

    # ---------- 毎ターン: 記録と内省のトリガ ----------

    def record_turn(
        self, user_text: str, reply: str, emotion: str | None = None, *,
        topic: str = "", source: str = "local", event_id: str = "",
    ) -> None:
        """応答完了後に呼ぶ。ログをため、条件が揃えば裏で内省を走らせる。"""
        is_user = not user_text.startswith("(")
        privacy = self._privacy.classify(
            user_text, group=bool(self._privacy_context.get("group", False)),
        ) if is_user else None
        cur = self._current_speaker
        # 親密度は相手ごとに育てる。話者が特定できている(声紋登録済み=id>=0)なら
        # その人の親密度を加算し、全体親密度は育てない(二重カウント回避)。
        # 話者不明(id<0など)のときだけ従来通り全体親密度を育てる。
        sid = int(cur.get("id", -1)) if cur else -1
        per_speaker = is_user and self._speakers is not None and sid >= 0
        self._personality.on_turn(emotion=emotion, is_user=is_user,
                                  bump_relationship=not per_speaker)
        if per_speaker:
            self._speakers.on_turn(sid)
            self._speakers.save()
        # 名乗りの即時検出 (内省を待たず、その場で名前を覚える)。
        # auto_name に限定しない: 誤学習した名前も「俺の名前は〇〇」で
        # その場で訂正できる (手動設定名だけは set_name 側で保護される)。
        if is_user and self._speakers is not None and cur is not None:
            name = detect_self_name(user_text)
            if name and name != cur.get("name") and self._speakers.set_name(cur["id"], name, learned=True):
                self._current_speaker = {**cur, "name": name, "auto_name": False}
                self._speakers.save()
                logger.info("名乗りを検出: %s → %s", cur["name"], name)
                self._emit("user_speaker", name=name, is_new=False)
                self._emit("mind_update", note=f"相手の名前が「{name}」だと覚えた", changes=[])
        speaker = (self._current_speaker or {}).get("name", "") if is_user else ""
        if privacy is not None and not privacy.should_store:
            # Explicit "do not remember" and restricted credentials never
            # enter the reflection queue, transcript table, or embeddings.
            if is_user:
                self._dialogue.record_response(self._dialogue_user_key(), reply)
            return
        if is_user and reply.strip():
            self.maybe_queue_knowledge_gap(user_text, reply)
        # **全ターンを長期保存しない。** 明示された好み・訂正・約束・強い感情
        # だけがゲートを通る。挨拶と相づちはここで落ちる。
        if is_user:
            with contextlib.suppress(Exception):
                context = self.persona_context
                provenance = {
                    "origin_turn_id": str(event_id or ""),
                    "source_role": "user",
                    "source_person_id": str(
                        self._relationship_speaker_key(source) or f"{source}:mic"),
                    "persona_id": str(getattr(context, "persona_id", "") or ""),
                    "persona_epoch": int(getattr(context, "persona_epoch", 0) or 0),
                }
                self.observe_episodes(
                    user_text, reply, source=source,
                    privacy_level=str(privacy.sensitivity) if privacy is not None else "normal",
                    event_ids=((str(event_id),) if event_id else ()),
                    provenance=provenance,
                )
        self._pending.append({
            "user": user_text[:400], "assistant": reply[:400], "speaker": speaker,
            "speaker_key": self._relationship_speaker_key(),
        })
        # 会話ログ記憶: 実際の発言とその時の自分の感情をそのまま長期保存する
        # (要約と別建て。「昨日何を話した?」に具体的に答えるための記録)。
        if (self._transcript_enabled and self._episodes.write_enabled
                and is_user and reply.strip()):
            self._recall_ex.submit(
                self._store_transcript_sync,
                user_text[:400], reply[:400], speaker, emotion or "", (topic or "")[:96],
            )
        if emotion and emotion != "neutral":
            self._pending_emotion = emotion
        self._temporal_self.observe_activity(source="discord" if bool(self._privacy_context.get("group", False)) else "local",
                                             discord=bool(self._privacy_context.get("group", False)))
        if emotion:
            self.observe_affect(emotion, reason="turn_complete")
        if is_user:
            if bool(self._cfg.get("time_awareness.enabled", True)):
                self._temporal.record_turn(topic or user_text)
            self._dialogue.record_response(self._dialogue_user_key(), reply)
            if bool(self._cfg.get("mind.relationship.enabled", True)):
                self._relationships.observe(self._relationship_speaker_key())
        if len(self._pending) >= self._every_turns:
            self._schedule_reflection()

    def _schedule_reflection(self) -> None:
        if self._reflect_task is not None and not self._reflect_task.done():
            return
        with contextlib.suppress(RuntimeError):
            self._reflect_task = asyncio.get_running_loop().create_task(self._reflect())

    async def _reflect(self, wait_idle_s: float = 180.0, min_idle_s: float | None = None) -> None:
        """会話が十分な時間途切れるのを待ってから1回のLLM呼び出しで内省する。

        ローカルLLM (Ollama) は同時リクエストが弱いので、min_idle_s 秒
        連続で会話が止まっている時だけ実行する (応答生成との衝突を避ける)。
        """
        min_idle = self._min_idle if min_idle_s is None else min_idle_s
        async with self._lock:
            deadline = time.monotonic() + wait_idle_s
            idle_since: float | None = None
            while True:
                if self._is_busy():
                    idle_since = None
                    if time.monotonic() > deadline:
                        return  # 忙しすぎるので今回は見送り (ログは残っている)
                    await asyncio.sleep(1.0)
                    continue
                now = time.monotonic()
                if idle_since is None:
                    idle_since = now
                if now - idle_since >= min_idle:
                    break
                if now > deadline:
                    return
                await asyncio.sleep(0.5)
            turns = list(self._pending)
            if not turns:
                return
            try:
                messages = build_reflection_messages(
                    self._persona_name,
                    self._personality.reflection_context(),
                    self._session_summary,
                    turns,
                )
                self._reflection_generating = True
                try:
                    text = await asyncio.wait_for(
                        self._complete(messages), timeout=self._llm_timeout
                    )
                finally:
                    self._reflection_generating = False
                data = parse_reflection_json(text)
            except asyncio.TimeoutError:
                logger.warning("内省LLM呼び出しがタイムアウト (%.0fs)。次の機会に再試行",
                               self._llm_timeout)
                return
            except Exception:
                logger.exception("内省LLM呼び出しでエラー")
                return
            if data is None:
                # パース失敗が続いても溜まりすぎないよう古い分は破棄
                del self._pending[: max(1, len(turns) // 2)]
                return
            self._pending = self._pending[len(turns):]
            await self._apply_reflection(data)
        # 会話が止まっている間に、溜まった経験から会話戦略の仮説を作る。
        # **ロックの外**——ここで応答を待たせない（第17条）。
        # できた仮説は次のターン以降にしか効かない。
        with contextlib.suppress(Exception):
            self.consolidate_memory(idle_seconds=min_idle)

    async def _apply_reflection(self, data: dict[str, Any]) -> None:
        summary = str(data.get("summary", "") or "").strip()
        if summary:
            self._session_summary = summary[:400]

        memories = data.get("memories") or []
        stored = 0
        for m in memories[:4]:
            text = str((m or {}).get("text", "")).strip()
            if not text:
                continue
            kind = str(m.get("kind", "fact"))
            if kind not in ("fact", "preference", "episode"):
                kind = "fact"
            importance = m.get("importance", 3)
            scored_importance = self._dialogue.memory_importance(text, kind)
            if scored_importance < self._dialogue.min_memory_importance:
                continue
            try:
                importance = max(int(importance), self._dialogue.importance_to_legacy(scored_importance))
            except (TypeError, ValueError):
                importance = self._dialogue.importance_to_legacy(scored_importance)
            await self._store_memory(text, kind, importance)
            stored += 1

        # 会話から相手の名前を学習 (「俺は〇〇」等の名乗りを内省が検出)
        learned_name = str(data.get("speaker_name", "") or "").strip()
        cur = self._current_speaker
        if (learned_name and self._speakers is not None and cur is not None
                and cur.get("auto_name")):
            if self._speakers.set_name(cur["id"], learned_name, learned=True):
                self._current_speaker = {**cur, "name": learned_name, "auto_name": False}
                self._speakers.save()
                self._emit("mind_update",
                           note=f"相手の名前が「{learned_name}」だと覚えた", changes=[])

        changes = self._personality.apply_reflection(
            data.get("trait_deltas"),
            data.get("like_updates"),
            data.get("mood"),
            note=str(data.get("note", "") or ""),
        )
        relationship_changes = self._relationships.apply_events(data.get("relationship_events") or [])
        self._personality.save()
        self._dialogue.save()
        self._store.prune(self._max_items)
        if changes or relationship_changes:
            note = str(data.get("note", "") or "")
            self._emit("mind_update", note=note, changes=[*changes, *relationship_changes])
        logger.info(
            "内省完了: 記憶+%d件, 変化=%s, 関係=%s, 要約=%.40s",
            stored, changes or "なし", relationship_changes or "なし", summary
        )

    async def _store_memory(self, text: str, kind: str, importance) -> None:
        emb_bytes = None
        dim = None
        if self._embedder is not None:
            loop = asyncio.get_running_loop()
            try:
                vec = await loop.run_in_executor(
                    self._recall_ex, lambda: self._embedder.encode([text])[0]
                )
                emb_bytes, dim = vec.tobytes(), int(vec.shape[0])
            except Exception:
                logger.exception("embedding生成に失敗 (テキストのみ保存)")
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            importance = 3
        self._store.add(text, kind=kind, importance=importance,
                        embedding=emb_bytes, dim=dim)
        self._emb_cache = None  # キャッシュ再構築

    async def _complete(self, messages: list[dict]) -> str:
        out: list[str] = []
        async for token in strip_think(self._llm.generate(messages)):
            out.append(token)
        return "".join(out)

    # ---------- おまかせ選曲 ----------

    async def suggest_song(self, extra_hint: str = "") -> str | None:
        """今の気分・好みからAI自身に「流したい曲」を1つ選ばせ、
        YouTube検索用のクエリ(曲名+アーティスト等)を返す。

        曲の指定があるときはこの関数を通さない (指定曲をそのまま検索する)。
        LLM未設定や失敗時は None を返し、呼び出し側が既定クエリへフォールバックする。
        """
        if self._llm is None:
            return None
        try:
            inner = self._personality.describe()
        except Exception:
            inner = ""
        sys = (
            "あなたはローカルAIキャラクターです。今の気分や好みに合わせて、"
            "いま自分が流したい曲を1曲だけ選びます。"
            "実在してYouTubeで見つかりそうな有名な曲 (J-POP・ボカロ・アニソン・"
            "ゲーム音楽・洋楽など) から選ぶこと。作らない・存在しない曲は選ばない。"
            "出力はYouTubeで検索する語句だけを1行で: 「曲名 アーティスト名」。"
            "説明・前置き・記号・カギ括弧・複数候補・番号は一切書かない。"
        )
        if inner:
            sys += "\n" + inner
        user = "今の気分にぴったりな曲を1つだけ選んで、その検索語だけを書いて。"
        if extra_hint:
            user += f" ヒント: {extra_hint}"
        msgs = [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ]
        try:
            text = await asyncio.wait_for(self._complete(msgs), timeout=25.0)
        except Exception:
            logger.warning("おまかせ選曲のLLM呼び出しに失敗", exc_info=True)
            return None
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            return None
        line = lines[0].strip(" 　「」『』\"'“”・:：-—ー*#0123456789.)")
        # 「〇〇を流します/再生します」等の説明が混ざったら曲名部分だけ残す
        line = re.sub(r"\s*(?:を)?\s*(?:流し|再生|かけ|聴|聞).*$", "", line).strip()
        line = line.strip(" 　「」『』\"'・:：")
        if not line or len(line) > 60:
            return None
        return line

    # ---------- セッション終了 ----------

    async def on_session_end(self) -> None:
        """終了時: 残りのログで最後の内省をし、セッションを思い出として保存。"""
        try:
            if self._pending:
                await asyncio.wait_for(
                    self._reflect(wait_idle_s=5.0, min_idle_s=0.1), timeout=30.0
                )
        except Exception:
            logger.warning("終了時の内省をスキップしました")
        if self._session_summary:
            import datetime as dt

            stamp = dt.date.today().strftime("%Y-%m-%d")
            await self._store_memory(
                f"[{stamp}の会話] {self._session_summary}", "episode", 3
            )
        self._personality.save()
        self._dialogue.save()
        self._relationships.save()
        self._temporal.save()

    async def close(self) -> None:
        """Release persistence and embedding/voiceprint workers at app exit."""
        task = self._reflect_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._personality.save()
        self._dialogue.close()
        self._relationships.save()
        self._temporal.save()
        self._temporal_self.close("CLEAN")
        self._activities.save()
        self._game_session.save()
        with contextlib.suppress(Exception):
            await self._research.stop()
            self._research_store.close()
        with contextlib.suppress(Exception):
            self._store.close()
        # 終了時バックアップ (全状態が保存された後に取る)
        if self._backup is not None:
            with contextlib.suppress(Exception):
                self._backup.stop()
                if bool(self._cfg.get("mind.backup.on_exit", True)):
                    self._backup.create_backup(reason="exit")
        with contextlib.suppress(Exception):
            self._ex.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(Exception):
            self._recall_ex.shutdown(wait=False, cancel_futures=True)

    # ---------- UI用 ----------

    def status(self) -> dict[str, Any]:
        snap = self._personality.snapshot()
        counts = self._store.counts()
        # Optional research history must never make the entire existing
        # "こころ" panel unavailable (for example when an old process still
        # holds a just-migrated SQLite file).  The panel can safely render an
        # unavailable research card and normal Mind information remains useful.
        try:
            research_status = self._research.snapshot()
            recent_research = self._research.history(5)
        except Exception as exc:
            logger.exception("自律研究ステータスの取得に失敗（こころ表示は継続）")
            research_status = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
            recent_research = []
        speakers = self._speakers.list() if self._speakers is not None else []
        # 各話者に「その相手との親密度(レベル/ラベル/進捗)」を付ける (相手別表示用)
        if self._speakers is not None:
            # **Resolver 経由で読む。** 直接 legacy を読むと、person_primary
            # の時に画面と会話が違う値を見ることになる——**同じ相手なのに
            # 表示だけ古い**、という気づきにくい食い違いになる。
            resolution = {row["speaker_id"]: row
                          for row in self._resolved_speaker_rows()}
            for sp in speakers:
                r = self._speakers.relationship(sp["id"])
                if r:
                    sp["relationship"] = {
                        "level": r["level"], "label": r["label"],
                        "familiarity": r["familiarity"], "next": r["next"],
                        "progress": r["progress"], "days": r["days"],
                    }
                sp["relationship_state"] = self._speaker_relationship_snapshot(
                    int(sp["id"]))
                found = resolution.get(int(sp["id"]))
                if found:
                    # **表示のために状態を変えない。** 読むだけ。
                    sp["identity"] = {"person_id": found.get("person_id", ""),
                                      "resolution": found.get("resolution", "")}
        # メインの「なかよし度」表示も、今話している相手がいればその人の親密度にする
        cur = self._current_speaker
        cur_rel = None
        if (cur is not None and self._speakers is not None
                and int(cur.get("id", -1)) >= 0):
            cur_rel = self._speakers.relationship(int(cur["id"]))
        result = {
            "enabled": True,
            "name": self._persona_name,
            **snap,
            "relationship": cur_rel or snap.get("relationship"),
            "memory": {
                "total": counts.get("total", 0),
                "facts": counts.get("fact", 0),
                "preferences": counts.get("preference", 0),
                "episodes": counts.get("episode", 0),
                "session_summary": self._session_summary,
                "semantic": self._embedder is not None,
                "storage": self._store.storage_health(),
            },
            "recent_memories": self._store.recent(8),
            "speakers": speakers,
            # Suggestion only.  Two people on one microphone must never be
            # fused without the user saying so.
            "speaker_merge_candidates": self.speaker_merge_candidates(),
            "speaker_id_enabled": self._speakers is not None,
            "dialogue": self._dialogue.snapshot(self._dialogue_user_key()),
            "relationship_state": self.relationship_status(),
            "relationships": self._relationships.all_snapshots(),
            "relationship_enabled": bool(self._cfg.get("mind.relationship.enabled", True)),
            "time_awareness": self._temporal.snapshot(),
            "temporal_self": self._temporal_self.snapshot(),
            "activity": self._activities.snapshot(),
            "conversation_kernel": self._kernel.snapshot(
                self._last_kernel_source, public=True,
            ),
            "behavior_directive": self._continuation.snapshot(
                self._dialogue_user_key(self._last_kernel_source)
            ),
            "research": research_status,
            # This is already sanitized by ResearchStore; raw conversation or
            # credentials are never returned to the GUI.
            "recent_research": recent_research,
        }
        if bool(self._cfg.get("conversation_features.developer_ui", False)):
            # 開発者デバッグの生成失敗が、こころステータス全体を落とさないようにする
            # (以前は例外/シリアライズ不能でパネルが開けなくなっていた)。
            try:
                result["conversation_debug"] = self.conversation_developer_status()
            except Exception:
                logger.exception("会話エンジンのデバッグ情報生成に失敗 (こころは通常表示)")
                result["conversation_debug"] = {"error": "デバッグ情報を取得できませんでした"}
        # pywebview/JSON へ確実に渡せるよう、数値型やsetなどをJSON安全化する
        return _json_safe(result)

    def _emit(self, event_type: str, **data: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event_type, data)
        except Exception:
            logger.exception("Mindイベント通知でエラー")
