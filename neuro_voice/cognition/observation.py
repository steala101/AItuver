"""実イベントを世界状態の観測へ変換する、**薄い**層。

Phase 6 で部品は作ったが、**呼ぶ側が無かった**。ここがその呼ぶ側。

Adapter がやるのは型変換だけ。意味の判断も、記憶への保存も、行動の生成も
入れない。**入れた瞬間に、映像の解析結果から発話が生まれる経路ができる。**

高頻度のイベント（映像は毎秒、ゲームは数百ms）がそのままDBへ行かないよう、
ここで落とす:

* 同じ内容の重複（`deduplication_key`）
* 確信の下限
* 変化していない観測
* 単位時間あたりの本数（rate limit）

**同期でDBへ書かない。** 書き込みは呼び出し側が後でまとめてやる
（`InitiativeRuntime.flush_world`）。映像1フレームごとにSQLiteを叩くと、
音声応答が止まる。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from neuro_voice.cognition.world import EntityType

# ---------------------------------------------------------------------------
# 観測の型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityObservation:
    """「これがある／いる」1件。"""

    canonical_name: str
    entity_type: EntityType | str = EntityType.UNKNOWN
    source_type: str = ""
    source_event_id: str = ""
    external_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = .6
    observed_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    dedup_key: str = ""

    def key(self) -> str:
        return self.dedup_key or f"entity:{self.source_type}:{self.canonical_name}"


@dataclass(frozen=True, slots=True)
class FactObservation:
    """「いまこうなっている」1件。"""

    subject_id: str
    predicate: str
    value: Any = True
    source_type: str = ""
    source_event_id: str = ""
    confidence: float = .6
    observed_at: float = field(default_factory=time.monotonic)
    expires_at: float | None = None
    ttl: float | None = None
    dedup_key: str = ""
    #: この出どころが正本か（第5条）。**推測ではないものに印を付ける。**
    #:
    #: Discordの接続状態やゲームのセッション状態は、確信の大小で
    #: 比べるものではない。持ち主が「もう居ない」と言えば居ない。
    #: 印が無いと矛盾の余裕に阻まれて、**退出が反映されない**。
    authoritative: bool = False

    def key(self) -> str:
        return self.dedup_key or f"fact:{self.subject_id}:{self.predicate}:{self.value}"


# ---------------------------------------------------------------------------
# 高頻度への備え
# ---------------------------------------------------------------------------

#: 観測として受け付ける確信の下限。
MIN_OBSERVATION_CONFIDENCE = .55
#: 同じ観測を再び受け付けるまでの秒数。
OBSERVATION_DEDUP_WINDOW = 3.0
#: 1秒あたりに通してよい観測の本数。**映像は毎秒来る。**
OBSERVATION_RATE_LIMIT = 8


class ObservationIntake:
    """観測の入口。**落とす理由を明示しておく。**

    落とす側を書いておかないと、「重要そうだから」で全部通り、
    世界状態が画面のゴミで埋まる。
    """

    def __init__(
        self, *, min_confidence: float = MIN_OBSERVATION_CONFIDENCE,
        dedup_window: float = OBSERVATION_DEDUP_WINDOW,
        rate_limit: int = OBSERVATION_RATE_LIMIT,
    ) -> None:
        self.min_confidence = float(min_confidence)
        self.dedup_window = float(dedup_window)
        self.rate_limit = int(rate_limit)
        self._seen: dict[str, float] = {}
        self._applied_events: set[str] = set()
        self._recent: list[float] = []
        self.dropped: dict[str, int] = {}

    def _drop(self, reason: str) -> bool:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1
        return False

    def accept(self, observation, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        if float(observation.confidence) < self.min_confidence:
            return self._drop("below_confidence")
        # **同じイベントIDを2回適用しない。**
        event_id = str(getattr(observation, "source_event_id", "") or "")
        if event_id:
            marker = f"{event_id}:{observation.key()}"
            if marker in self._applied_events:
                return self._drop("duplicate_event_id")
        key = observation.key()
        last = self._seen.get(key)
        if last is not None and moment - last < self.dedup_window:
            return self._drop("unchanged")
        self._recent = [item for item in self._recent if moment - item <= 1.0]
        if len(self._recent) >= self.rate_limit:
            return self._drop("rate_limited")
        self._seen[key] = moment
        self._recent.append(moment)
        if event_id:
            self._applied_events.add(f"{event_id}:{key}")
            if len(self._applied_events) > 512:
                self._applied_events = set(list(self._applied_events)[-256:])
        if len(self._seen) > 256:
            for old in sorted(self._seen, key=self._seen.get)[:128]:
                self._seen.pop(old, None)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"dropped": dict(self.dropped), "keys": len(self._seen)}


# ---------------------------------------------------------------------------
# 入力元ごとの変換
#
# **既存のイベント型を書き換えない。** ここで読むだけ。
# ---------------------------------------------------------------------------

#: いま誰が話しているか。**短命**——喋り終われば意味を失う。
CURRENT_SPEAKER_TTL = 8.0
#: 会話に居ること。話者確認から数分は有効。
PRESENCE_TTL = 300.0
#: セッション中の話題。
SESSION_SUBJECT = "session"


def from_speaker(
    *, speaker_id: int, name: str, confirmed: bool, event_id: str = "",
    now: float | None = None,
) -> tuple[list[EntityObservation], list[FactObservation]]:
    """話者確認 → 参加者と、いま話している人。

    **声紋が確認できた人だけを PARTICIPANT にする。** 確認できていない声を
    既知の人物として扱うと、別人の関係値が動く。
    """
    moment = time.monotonic() if now is None else now
    label = str(name or "").strip() or "不明な話者"
    if not confirmed or int(speaker_id) < 0:
        # **不明話者は UNKNOWN のまま持つ。** 既知へ寄せない。
        return ([EntityObservation(
            canonical_name=label, entity_type=EntityType.UNKNOWN,
            source_type="speaker", source_event_id=event_id,
            confidence=.4, observed_at=moment,
            dedup_key="entity:speaker:unknown")], [])
    external = f"speaker:{int(speaker_id)}"
    entity = EntityObservation(
        canonical_name=label, entity_type=EntityType.PARTICIPANT,
        source_type="speaker", source_event_id=event_id, external_id=external,
        confidence=.9, observed_at=moment,
        dedup_key=f"entity:{external}")
    facts = [
        FactObservation(
            subject_id=external, predicate="present_in_conversation", value=True,
            source_type="speaker", source_event_id=event_id, confidence=.9,
            observed_at=moment, ttl=PRESENCE_TTL,
            dedup_key=f"fact:{external}:present"),
        FactObservation(
            subject_id=SESSION_SUBJECT, predicate="speaking", value=external,
            source_type="speaker", source_event_id=event_id, confidence=.9,
            observed_at=moment, ttl=CURRENT_SPEAKER_TTL,
            dedup_key=f"fact:session:speaking:{external}"),
    ]
    return [entity], facts


#: 映像から拾うのは「いまどの画面か」だけ。物体や人は取り込まない。
#:
#: 全フレームの物体を取り込むと、世界状態がすぐゴミで埋まる。
#: 安定して当たるものから始める。
VISION_SCENE_TTL = 90.0
#: 場面として受け付ける確信の下限。**低い認識を現在の事実にしない。**
VISION_CONFIDENCE_FLOOR = .6
#: 場面が入る先。**画面は1つしかないので、Factも1つに収束させる。**
ACTIVE_SCREEN = "active_screen"

#: 実際に出力されうる場面。**`vision/service.py` のプロンプトが正本。**
#:
#: プロンプトは `minecraft_gameplay|menu|loading|error|unknown` を要求する。
#: ここに架空の分類を足さない——足しても映像側が一度も返さないので、
#: テストだけが緑になって実機では死んだままになる。
#: プロンプトを変えたら**ここも変える**（食い違いは診断で赤くなる）。
SCENE_TYPES: tuple[str, ...] = (
    "gameplay", "menu", "loading", "error", "unknown",
)
#: 実出力の揺れ → 上の分類。ゲーム名付きの `*_gameplay` は全部 `gameplay`。
SCENE_ALIASES: dict[str, str] = {
    "minecraft_gameplay": "gameplay", "game": "gameplay", "in_game": "gameplay",
    "playing": "gameplay",
    "title": "menu", "title_screen": "menu", "pause": "menu", "pause_menu": "menu",
    "inventory": "menu", "settings": "menu",
    "load": "loading", "loading_screen": "loading",
    "result": "error", "": "unknown", "none": "unknown", "other": "unknown",
}


def normalise_scene(value) -> str:
    """自由記述の `scene_type` を、実際に出うる分類へ落とす。

    **知らない言い方は `unknown`。** 勝手に近そうな分類へ寄せると、
    メニューを開いただけで「まだ戦ってる」と言う状態になる。
    """
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in SCENE_TYPES:
        return text
    if text in SCENE_ALIASES:
        return SCENE_ALIASES[text]
    # `minecraft_gameplay` のようにゲーム名が前に付く形。
    for known in SCENE_TYPES:
        if known != "unknown" and text.endswith(f"_{known}"):
            return known
    return "unknown"


#: 体力の見立てを受け付ける言い方。**プロンプトが `health_estimate` を
#: 自由記述で返す**ので、そのまま事実にせず、この3つへ落とす。
#:
#: 増やす前に実機のログを見ること。**架空の言い方を先回りで足さない。**
HEALTH_LEVELS: dict[str, str] = {
    "low": "low", "critical": "low", "danger": "low", "hurt": "low",
    "少ない": "low", "危険": "low", "瀕死": "low", "ピンチ": "low",
    "half": "medium", "medium": "medium", "半分": "medium", "中": "medium",
    "full": "high", "high": "high", "healthy": "high", "満": "high",
    "満タン": "high", "多い": "high", "元気": "high",
}
#: 体力は**すぐ変わる**。世界状態側の既定（`FACT_TTL["hp"]`）に合わせる。
VISION_HP_TTL = 8.0
#: 体力として受け付ける確信の下限。**画面のHPは読み違えやすい。**
#: 場面より高くしてあるのは、外した時に「まだ大丈夫」と言ってしまうから。
VISION_HP_FLOOR = .7


def normalise_health(value) -> str:
    """自由記述の体力 → `low` / `medium` / `high`。**知らない言い方は空。**"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in HEALTH_LEVELS:
        return HEALTH_LEVELS[text]
    for needle, level in HEALTH_LEVELS.items():
        if needle in text:
            return level
    return ""


def from_vision(observation, *, now: float | None = None) -> tuple[
        list[EntityObservation], list[FactObservation]]:
    """映像の解析結果 → いまの画面。

    `people` や `objects` を全部取り込まないのは、確信が安定しないため。
    そこは実機で当たり方を見てから増やす。

    **低い確信でも `unknown` としては通す。** 「分からなくなった」ことは
    現在の状態であって、黙って前の画面を保つ方が嘘に近い。
    ただし前の画面を**上書きするかどうかは `WorldStateGate` が決める**——
    低確信の観測は僅差では既存の事実を覆せない。
    """
    moment = time.monotonic() if now is None else now
    raw = str(getattr(observation, "scene_type", "") or "").strip()
    confidence = getattr(observation, "confidence", None)
    confidence = .6 if confidence is None else float(confidence)
    if not raw:
        return ([], [])
    scene = normalise_scene(raw)
    confident = confidence >= VISION_CONFIDENCE_FLOOR
    if not confident:
        # **断定しない。** 低確信のまま具体的な画面名を名乗らせない。
        scene, confidence = "unknown", min(confidence, VISION_CONFIDENCE_FLOOR - .05)
    event_id = str(getattr(observation, "observation_id", "") or "")
    facts: list[FactObservation] = []
    # **体力の見立て**（Phase 6D の1種類）。既存プロンプトの
    # `player_state.health_estimate` が正本で、新しい検出器は作らない。
    #
    # ゲーム中の画面でしか意味を持たない。メニューを開いている間の
    # 「HPが少ない」は、いまの状況ではない。
    details = getattr(observation, "player_state_details", None)
    if isinstance(details, dict) and scene == "gameplay" and confident:
        health = normalise_health(details.get("health_estimate"))
        if health and confidence >= VISION_HP_FLOOR:
            facts.append(FactObservation(
                subject_id="player", predicate="hp", value=health,
                source_type="vision", source_event_id=event_id,
                confidence=confidence, observed_at=moment, ttl=VISION_HP_TTL,
                # **画面が正本ではない。** 読み違えるので、確信で比べさせる。
                authoritative=False,
                dedup_key=f"fact:player:hp:{health}"))
    return ([], [FactObservation(
        subject_id=ACTIVE_SCREEN, predicate="scene", value=scene,
        source_type="vision", source_event_id=event_id, confidence=confidence,
        observed_at=moment, ttl=VISION_SCENE_TTL,
        # 画面は**移り変わるもの**で、2人の観測者が別のことを言っている
        # わけではない。はっきり見えているなら、それが今の画面。
        # ——ただし**自信が無い時は正本にしない**。曖昧な認識が
        # 今の画面を塗り替えると、メニューを開いただけで話が変わる。
        authoritative=confident,
        # **同じ画面が続く間は同じ鍵**——毎フレーム新しいFactを作らない。
        dedup_key=f"fact:{ACTIVE_SCREEN}:scene:{scene}"), *facts])


# ---------------------------------------------------------------------------
# Discord の参加状態 (Phase 6C)
#
# **接続上の人物と、声紋で推定した人物は別物。**
# Discord ID は確実だが「誰の声か」は言わない。声紋は「誰の声か」を言うが
# 確実ではない。片方をもう片方の根拠にすると、取り違えが恒久化する。
# ---------------------------------------------------------------------------

#: 参加していることの寿命。**再起動を越えて現在の事実にしない。**
#: `present_in_conversation` は `SESSION_SCOPED_PREDICATES` に入っている。
DISCORD_PRESENCE_TTL = 3600.0
#: Discord の参加者を指す接頭辞。声紋側の `speaker:` と混ざらないようにする。
DISCORD_PREFIX = "discord"


def _discord_external_id(user_id: int) -> str:
    return f"{DISCORD_PREFIX}:{int(user_id)}"


def from_discord_presence(
    *, user_id: int, display_name: str, present: bool, channel: str = "",
    is_bot: bool = False, event_id: str = "", session_id: str = "",
    now: float | None = None,
) -> tuple[list[EntityObservation], list[FactObservation]]:
    """Discord の入退室 → 参加者と、いま居るかどうか。

    **Bot 自身を人間の参加者として登録しない。** 自分の声に反応し、
    自分を数に入れて「みんな居るね」と言う状態になる。

    表示名は変わる。`external_id` に Discord の user ID を入れて、
    **名前ではなく安定IDで同じEntityを引く**。退出しても Entity は消さない
    ——消すと再入室のたびに別人になり、関係が積み上がらない。
    """
    if is_bot or int(user_id) < 0:
        return ([], [])
    moment = time.monotonic() if now is None else now
    external = _discord_external_id(user_id)
    label = str(display_name or "").strip() or f"Discord利用者{int(user_id)}"
    entity = EntityObservation(
        canonical_name=label, entity_type=EntityType.PARTICIPANT,
        source_type=DISCORD_PREFIX, source_event_id=event_id, external_id=external,
        # **声紋で確認した人物ではない。** 接続としては確実だが、
        # 誰の声かは別問題なので、話者側より低く置く。
        confidence=.8, observed_at=moment,
        attributes={"transport": DISCORD_PREFIX, "session_id": str(session_id or "")},
        dedup_key=f"entity:{external}")
    facts = [FactObservation(
        subject_id=external, predicate="present_in_conversation", value=bool(present),
        source_type=DISCORD_PREFIX, source_event_id=event_id, confidence=.9,
        observed_at=moment, ttl=DISCORD_PRESENCE_TTL, authoritative=True,
        dedup_key=f"fact:{external}:present:{bool(present)}")]
    if present and channel:
        facts.append(FactObservation(
            subject_id=external, predicate="voice_channel", value=str(channel)[:60],
            source_type=DISCORD_PREFIX, source_event_id=event_id, confidence=.9,
            observed_at=moment, ttl=DISCORD_PRESENCE_TTL, authoritative=True,
            dedup_key=f"fact:{external}:channel:{str(channel)[:60]}"))
    elif not present:
        # **チャンネル情報は退出で意味を失う。** 偽で上書きして古くする。
        facts.append(FactObservation(
            subject_id=external, predicate="voice_channel", value="",
            source_type=DISCORD_PREFIX, source_event_id=event_id, confidence=.9,
            observed_at=moment, ttl=1.0, authoritative=True,
            dedup_key=f"fact:{external}:channel:left"))
    return ([entity], facts)


#: ゲームから拾う1種類。**実際に出ている高確信のイベント**を選ぶ。
#:
#: `GameCompanionEvent.kind` は既存の分類器が付けている。架空の種類は足さない。
GAME_STATE_TTL = 45.0
GAME_CONFIDENCE_FLOOR = .6


def from_game_event(event, *, now: float | None = None) -> tuple[
        list[EntityObservation], list[FactObservation]]:
    """ゲームの出来事 → いまのプレイ状況。

    `priority` を確信として扱う。既存の `GameCompanionDirector` が
    付けている値で、**新しい分類器は作らない。**
    """
    moment = time.monotonic() if now is None else now
    kind = str(getattr(event, "kind", "") or "").strip()
    priority = float(getattr(event, "priority", 0.0) or 0.0)
    if not kind or priority < GAME_CONFIDENCE_FLOOR:
        return ([], [])
    signature = str(getattr(event, "signature", "") or kind)
    return ([], [FactObservation(
        subject_id="game", predicate="situation", value=kind,
        source_type="game", source_event_id=signature, confidence=priority,
        observed_at=moment, ttl=GAME_STATE_TTL,
        dedup_key=f"fact:game:situation:{kind}")])


# ---------------------------------------------------------------------------
# KTANE (Phase 6C)
#
# **KTANEに画面認識は無い。** 既存の実装は最初から最後まで会話駆動で、
# 爆弾の情報も、いま扱っているモジュールも、ミスの回数も、
# ユーザーの発話から `parse.py` が読み取っている。
#
# だから取り込むのも会話由来の信号だけにする。**画面から爆弾の状態を
# 読む経路を新しく作らない**——作ると、当たらない認識結果を根拠に
# 「切って」と言う経路ができる。爆弾では間違いが爆発になる。
# ---------------------------------------------------------------------------

#: 爆弾1つ。セッションが終われば意味を失うが、途中では消えない。
KTANE_BOMB = "ktane_bomb"
#: いま扱っているモジュールは**言い直すまで有効**。TTLは長めでよい。
KTANE_MODULE_TTL = 600.0
#: 爆弾が動いていること自体。セッション中ずっと。
KTANE_SESSION_TTL = 7200.0
#: 実際に取り込むイベント。**既存経路が出しているものだけ。**
KTANE_EVENTS: tuple[str, ...] = (
    "bomb_started", "module_detected", "strike_recorded", "bomb_ended",
)


def from_ktane_event(
    *, event: str, session_id: str = "", module: str = "", strikes: int = 0,
    outcome: str = "", confidence: float = .9, event_id: str = "",
    now: float | None = None,
) -> tuple[list[EntityObservation], list[FactObservation]]:
    """KTANE の出来事 → 爆弾の状態。

    `event` は `KTANE_EVENTS` のどれか。**知らない種類は何も作らない**
    ——「たぶんこれだろう」で爆弾の状態を作らない。
    """
    kind = str(event or "").strip().lower()
    if kind not in KTANE_EVENTS:
        return ([], [])
    moment = time.monotonic() if now is None else now
    marker = event_id or f"ktane:{kind}:{session_id}"
    entity = EntityObservation(
        canonical_name="爆弾", entity_type=EntityType.TASK_TARGET,
        source_type="ktane", source_event_id=marker, external_id=KTANE_BOMB,
        confidence=confidence, observed_at=moment,
        attributes={"session_id": str(session_id or ""), "game": "ktane"},
        dedup_key=f"entity:{KTANE_BOMB}")
    facts: list[FactObservation] = []

    def add(predicate: str, value, *, ttl: float, key: str) -> None:
        facts.append(FactObservation(
            subject_id=KTANE_BOMB, predicate=predicate, value=value,
            source_type="ktane", source_event_id=marker, confidence=confidence,
            observed_at=moment, ttl=ttl, authoritative=True,
            dedup_key=f"fact:{KTANE_BOMB}:{key}"))

    if kind == "bomb_started":
        add("current_game", "ktane", ttl=KTANE_SESSION_TTL, key="game:ktane")
        add("bomb_active", True, ttl=KTANE_SESSION_TTL, key=f"active:{session_id}")
    elif kind == "module_detected":
        if not str(module or "").strip():
            return ([], [])
        add("current_module", str(module)[:40], ttl=KTANE_MODULE_TTL,
            key=f"module:{str(module)[:40]}")
    elif kind == "strike_recorded":
        # **ミスの回数は解法そのものを変える**（サイモン等）。短命にしない。
        add("strike_count", int(strikes), ttl=KTANE_SESSION_TTL,
            key=f"strikes:{int(strikes)}")
    elif kind == "bomb_ended":
        add("bomb_active", False, ttl=KTANE_SESSION_TTL, key=f"ended:{session_id}")
        # **結果が分からないなら分からないと書く。** 終わった=解除できた
        # ではない。ユーザーが言わなかっただけかもしれない。
        add("session_result", str(outcome or "unknown")[:20],
            ttl=KTANE_SESSION_TTL, key=f"result:{session_id}")
    return ([entity], facts)


# ---------------------------------------------------------------------------
# まとめ役
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ObservationResult:
    """1回の観測で**実際に何が呼ばれたか**。

    件数だけを返していると、「0件」が「入力が無かった」なのか
    「呼んだが全部落ちた」なのか「そもそも呼んでいない」なのか
    区別できない。Trace はここを写す。
    """

    source: str = ""
    adapter: str = "WorldObservationAdapter"
    source_event_id: str = ""
    entity_called: bool = False
    fact_called: bool = False
    applied: int = 0
    delta_id: str = ""
    dropped: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)
    #: 入力元ごとの、一言だけの但し書き。**本文は入れない**（第12条）。
    detail: dict[str, Any] = field(default_factory=dict)

    def __int__(self) -> int:
        return self.applied

    def __bool__(self) -> bool:
        return self.applied > 0


class WorldObservationAdapter:
    """既存イベント → 観測 → `observe_entity` / `observe_fact`。

    **入力元ごとの差を吸収するだけ。** 意味の判断はここに置かない。
    """

    def __init__(self, runtime, *, intake: ObservationIntake | None = None) -> None:
        self._runtime = runtime
        self.intake = intake or ObservationIntake()
        self.counters: dict[str, int] = {
            "speaker": 0, "vision": 0, "game": 0, "discord": 0, "ktane": 0,
            "accepted": 0, "dropped": 0, "scene_transitions": 0,
        }
        self.last_latency_ms: dict[str, float] = {}
        self.last_result: ObservationResult | None = None
        #: いま画面は何か。**遷移を出すためだけに持つ。**
        self.current_scene: str = ""
        #: Discord に居ると分かっている人。**接続が真実の持ち主**（第5条）。
        self.discord_present: dict[int, str] = {}

    # -- 入口 -----------------------------------------------------------

    def observe_speaker(
        self, *, speaker_id: int, name: str, confirmed: bool,
        event_id: str = "", now: float | None = None,
    ) -> int:
        self.counters["speaker"] += 1
        return self._apply(*from_speaker(
            speaker_id=speaker_id, name=name, confirmed=confirmed,
            event_id=event_id or uuid.uuid4().hex[:12], now=now),
            source="speaker", now=now)

    def observe_vision(self, observation, *, now: float | None = None) -> int:
        self.counters["vision"] += 1
        entities, facts = from_vision(observation, now=now)
        if not getattr(self._runtime, "extended_vision_fact_enabled", False):
            # 画面の種類だけ。**体力の見立ては別のフラグで上げる。**
            facts = [item for item in facts if item.predicate == "scene"]
        scene = next((str(item.value) for item in facts
                      if item.predicate == "scene"), "")
        before = self.current_scene
        # **遷移かどうかはここでしか分からない。** 世界状態は「いま」しか
        # 持たないので、変わった瞬間を捕まえるのはこの層の仕事。
        transition = ""
        if scene and scene != before:
            transition = f"{before or 'unknown'}->{scene}"
            self.counters["scene_transitions"] += 1
        applied = self._apply(entities, facts, source="vision", now=now)
        if scene and applied:
            self.current_scene = scene
        if self.last_result is not None:
            self.last_result.detail = {
                "scene": scene, "previous_scene": before, "transition": transition}
        return applied

    def observe_game_event(self, event, *, now: float | None = None) -> int:
        self.counters["game"] += 1
        return self._apply(*from_game_event(event, now=now),
                           source="game", now=now)

    def observe_discord_presence(
        self, *, user_id: int, display_name: str, present: bool, channel: str = "",
        is_bot: bool = False, event_id: str = "", session_id: str = "",
        now: float | None = None,
    ) -> int:
        """1人ぶんの入退室。**Bot は数に入れない。**"""
        self.counters["discord"] += 1
        applied = self._apply(*from_discord_presence(
            user_id=user_id, display_name=display_name, present=present,
            channel=channel, is_bot=is_bot, event_id=event_id,
            session_id=session_id, now=now), source="discord", now=now)
        if not is_bot and int(user_id) >= 0:
            if present:
                self.discord_present[int(user_id)] = str(display_name or "")
            else:
                self.discord_present.pop(int(user_id), None)
        if self.last_result is not None:
            self.last_result.detail = {
                "participant_id": _discord_external_id(user_id) if not is_bot else "",
                "present": bool(present), "bot": bool(is_bot),
                "present_count": len(self.discord_present)}
        return applied

    def reconcile_discord(
        self, members, *, channel: str = "", session_id: str = "",
        event_id: str = "", now: float | None = None,
    ) -> dict[str, Any]:
        """**いま実際に居る人の一覧**で作り直す。

        起動直後や再接続の後は、前回の参加状態を信じてはいけない。
        Discord が返す現在のメンバー一覧が唯一の根拠（第5条）。
        入退室イベントを1件ずつ追うより、毎回この一覧で合わせる方が
        取りこぼしが無い——切断中に起きた出入りはイベントが来ない。
        """
        seen: dict[int, str] = {}
        for member in members or ():
            if isinstance(member, dict):
                user_id, name = member.get("id", -1), member.get("name", "")
                is_bot = bool(member.get("bot", False))
            else:
                user_id = getattr(member, "id", -1)
                name = getattr(member, "display_name", None) or getattr(member, "name", "")
                is_bot = bool(getattr(member, "bot", False))
            if is_bot or int(user_id) < 0:
                continue
            seen[int(user_id)] = str(name or "")
        joined = [key for key in seen if key not in self.discord_present]
        left = [key for key in self.discord_present if key not in seen]
        for user_id in joined:
            self.observe_discord_presence(
                user_id=user_id, display_name=seen[user_id], present=True,
                channel=channel, session_id=session_id, now=now,
                event_id=f"{event_id or 'reconcile'}:join:{user_id}")
        for user_id in left:
            self.observe_discord_presence(
                user_id=user_id, display_name=self.discord_present.get(user_id, ""),
                present=False, channel=channel, session_id=session_id, now=now,
                event_id=f"{event_id or 'reconcile'}:leave:{user_id}")
        return {"joined": joined, "left": left, "present": len(self.discord_present)}

    def observe_ktane_event(self, *, event: str, now: float | None = None,
                            **kwargs) -> int:
        self.counters["ktane"] += 1
        applied = self._apply(*from_ktane_event(event=event, now=now, **kwargs),
                              source="ktane", now=now)
        if self.last_result is not None:
            self.last_result.detail = {"ktane_event": str(event or "")}
        return applied

    # -- 適用 -----------------------------------------------------------

    def _apply(self, entities, facts, *, source: str = "",
               now: float | None = None) -> int:
        started = time.perf_counter()
        before = dict(self.intake.dropped)
        result = ObservationResult(source=source)
        result.source_event_id = next(
            (str(item.source_event_id) for item in (*entities, *facts)
             if getattr(item, "source_event_id", "")), "")
        applied = 0
        entity_started = time.perf_counter()
        for observation in entities:
            if not self.intake.accept(observation, now=now):
                self.counters["dropped"] += 1
                continue
            result.entity_called = True
            entity, delta = self._runtime.observe_entity(
                observation.canonical_name,
                entity_type=observation.entity_type,
                confidence=observation.confidence,
                event_ids=(observation.source_event_id,),
                attributes={**observation.attributes,
                            **({"external_id": observation.external_id}
                               if observation.external_id else {})},
                now=now)
            if entity is not None:
                applied += 1
                self.counters["accepted"] += 1
                result.delta_id = str(getattr(delta, "delta_id", "")
                                      or result.delta_id)
        result.latency_ms["observe_entity_ms"] = round(
            (time.perf_counter() - entity_started) * 1000, 3)
        fact_started = time.perf_counter()
        for observation in facts:
            if not self.intake.accept(observation, now=now):
                self.counters["dropped"] += 1
                continue
            result.fact_called = True
            delta = self._runtime.observe_fact(
                observation.subject_id, observation.predicate, observation.value,
                confidence=observation.confidence,
                event_ids=(observation.source_event_id,),
                ttl=observation.ttl, authoritative=observation.authoritative,
                now=now)
            if delta is not None:
                applied += 1
                self.counters["accepted"] += 1
                result.delta_id = str(getattr(delta, "delta_id", "")
                                      or result.delta_id)
        result.latency_ms["observe_fact_ms"] = round(
            (time.perf_counter() - fact_started) * 1000, 3)
        result.latency_ms["observation_adapter_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)
        result.applied = applied
        result.dropped = {key: value - before.get(key, 0)
                          for key, value in self.intake.dropped.items()
                          if value - before.get(key, 0) > 0}
        self.last_latency_ms = dict(result.latency_ms)
        self.last_result = result
        return applied

    def status(self) -> dict[str, Any]:
        """診断用。**画面の中身も音声も入れない**（第12条）。"""
        last = self.last_result
        return {
            "counters": dict(self.counters),
            "intake": self.intake.snapshot(),
            "latency_ms": dict(self.last_latency_ms),
            "last": {
                "source": last.source if last else "",
                "source_event_id": last.source_event_id if last else "",
                "entity_called": bool(last.entity_called) if last else False,
                "fact_called": bool(last.fact_called) if last else False,
                "applied": last.applied if last else 0,
                "delta_id": last.delta_id if last else "",
                "dropped": dict(last.dropped) if last else {},
                "detail": dict(last.detail) if last else {},
            },
            "scene": self.current_scene,
            "discord_present": len(self.discord_present),
        }


#: 再起動後も現在の事実として扱ってはいけない述語。
#:
#: **一時状態を保存する場合も、起動時は必ず古いものとして扱う。**
#: 再起動しただけでゲーム途中の状態を「いまこう」と言うのは嘘になる。
SESSION_SCOPED_PREDICATES = frozenset({
    "speaking", "scene", "situation", "hp", "position", "visible",
    "present_in_conversation",
    # Phase 6C。**参加中も画面も爆弾も、再起動を越えて現在の事実にしない。**
    # 特に `present_in_conversation` は、実際の参加者一覧で確かめ直すまで
    # 「居る」と言ってはいけない（`reconcile_discord`）。
    "voice_channel", "bomb_active", "current_module", "strike_count",
    "current_game",
})


__all__ = [
    "ACTIVE_SCREEN", "CURRENT_SPEAKER_TTL", "DISCORD_PRESENCE_TTL",
    "DISCORD_PREFIX", "GAME_STATE_TTL", "KTANE_BOMB", "KTANE_EVENTS",
    "KTANE_MODULE_TTL", "KTANE_SESSION_TTL", "MIN_OBSERVATION_CONFIDENCE",
    "OBSERVATION_DEDUP_WINDOW", "OBSERVATION_RATE_LIMIT", "PRESENCE_TTL",
    "HEALTH_LEVELS", "SCENE_ALIASES", "SCENE_TYPES", "SESSION_SCOPED_PREDICATES",
    "SESSION_SUBJECT", "VISION_CONFIDENCE_FLOOR", "VISION_HP_FLOOR",
    "VISION_HP_TTL", "VISION_SCENE_TTL", "normalise_health",
    "EntityObservation", "FactObservation", "ObservationIntake",
    "ObservationResult", "WorldObservationAdapter",
    "from_discord_presence", "from_game_event", "from_ktane_event",
    "from_speaker", "from_vision", "normalise_scene",
]
