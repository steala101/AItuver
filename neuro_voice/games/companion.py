"""Event-driven game companion reactions.

The vision model describes frames, but it must not decide directly when the
assistant speaks.  This small deterministic layer turns clear *changes* into
short-lived events, suppresses menus/unchanged scenes, and deduplicates noisy
semantic observations before an LLM is asked to phrase a reaction.
"""
from __future__ import annotations

import re
import hashlib
import time
from dataclasses import dataclass

from neuro_voice.vision.models import VisualObservation


@dataclass(frozen=True, slots=True)
class GameCompanionEvent:
    kind: str
    summary: str
    evidence: tuple[str, ...]
    priority: float
    signature: str
    created_at: float
    player_state: tuple[str, ...] = ()
    suggested_help: str = ""


class GameCompanionDirector:
    """Select moments where a co-player would naturally react."""

    _MENU_WORDS = (
        "title screen", "main menu", "game menu", "pause menu", "paused",
        "options menu", "settings menu", "world selection", "server list",
        "loading screen", "joining world", "saving world",
        "タイトル画面", "メインメニュー", "ゲームメニュー", "ポーズメニュー",
        "一時停止", "設定画面", "ワールド選択", "サーバー一覧", "ロード画面",
        "読み込み中", "ワールドに接続", "ワールドを保存",
    )
    _DEATH = (
        "death screen", "you died", "player died", "died", "was slain",
        "死亡画面", "死んだ", "死亡した", "倒された", "全ロス",
    )
    _DANGER = (
        "low health", "critical health", "taking damage", "under attack",
        "combat", "zombie", "skeleton", "creeper", "spider", "drowned",
        "enderman", "hostile mob", "lava", "burning", "falling", "explosion",
        "体力が低", "瀕死", "ダメージ", "襲われ", "戦闘", "ゾンビ",
        "スケルトン", "クリーパー", "クモ", "ドラウンド", "敵対モブ",
        "溶岩", "燃えて", "落下", "爆発", "囲まれ",
    )
    _RECOVERY = (
        "health recovered", "healed", "ate food", "eating", "escaped",
        "safe shelter", "slept", "体力が回復", "回復した", "食べた",
        "食事", "逃げ切", "安全にな", "避難", "眠った", "ベッドで寝",
    )
    _DISCOVERY = (
        "discovered", "found", "obtained", "acquired", "achievement",
        "advancement", "diamond", "village", "stronghold", "nether portal",
        "entered the nether", "cave entrance", "treasure",
        "発見", "見つけ", "入手", "獲得", "進捗", "実績", "ダイヤ",
        "村", "要塞", "ネザーポータル", "ネザーに入", "洞窟", "宝",
    )
    _ACTION = (
        "crafting", "crafted", "mining", "building", "placing blocks",
        "breaking blocks", "fighting", "shooting", "fishing", "trading",
        "smelting", "harvesting", "taming", "riding", "opened chest",
        "クラフト", "作成", "採掘", "掘って", "建築", "ブロックを置",
        "ブロックを壊", "攻撃", "戦って", "射撃", "釣り", "取引",
        "精錬", "収穫", "手懐け", "騎乗", "チェストを開",
    )
    _TRANSITION = (
        "nightfall", "sunrise", "weather changed", "started raining",
        "entered", "left", "moved into", "biome changed",
        "夜にな", "朝にな", "雨が降", "天候", "入った", "出た",
        "移動した", "バイオームが変",
    )

    def __init__(
        self, *, minimum_confidence: float = .55, minimum_gap_sec: float = 4.0,
        urgent_gap_sec: float = 1.25, duplicate_ttl_sec: float = 30.0,
        minimum_importance: float = .55, reaction_probability: float = 1.0,
    ) -> None:
        self.minimum_confidence = max(0.0, min(1.0, float(minimum_confidence)))
        self.minimum_gap_sec = max(0.0, float(minimum_gap_sec))
        self.urgent_gap_sec = max(0.0, float(urgent_gap_sec))
        self.duplicate_ttl_sec = max(1.0, float(duplicate_ttl_sec))
        self.minimum_importance = max(0.0, min(1.0, float(minimum_importance)))
        self.reaction_probability = max(0.0, min(1.0, float(reaction_probability)))
        self._last_event_at = float("-inf")
        self._recent_signatures: dict[str, float] = {}
        self.last_suppression_reason = ""

    @staticmethod
    def _joined(observation: VisualObservation) -> str:
        fields = (
            [observation.summary], observation.actions, observation.scene_changes,
            observation.player_state, observation.notable_events,
            observation.visible_text,
        )
        return " | ".join(str(item).strip() for group in fields for item in group if str(item).strip())

    @staticmethod
    def _contains(text: str, terms: tuple[str, ...]) -> bool:
        lowered = text.casefold()
        return any(term.casefold() in lowered for term in terms)

    @staticmethod
    def _normalise(text: str) -> str:
        return re.sub(r"[^0-9a-zA-Zぁ-んァ-ヶ一-龠]+", "", text.casefold())[:240]

    def _classify(self, text: str, observation: VisualObservation) -> tuple[str, float] | None:
        if self._contains(text, self._DEATH):
            return "failure", 1.0
        if self._contains(text, self._DANGER):
            return "danger", .95
        if self._contains(text, self._RECOVERY):
            return "recovery", .72
        if self._contains(text, self._DISCOVERY):
            return "discovery", .78
        if self._contains(text, self._ACTION):
            return "action", .58
        if self._contains(text, self._TRANSITION):
            return "transition", .55
        if observation.notable_events:
            return "reaction", .68
        if observation.commentary_worthy and observation.scene_changes:
            return "reaction", .55
        return None

    def observe(
        self, observation: VisualObservation, *, now: float | None = None,
    ) -> GameCompanionEvent | None:
        """Return a fresh reaction event, or ``None`` for silence."""
        now = time.monotonic() if now is None else float(now)
        confidence = float(observation.confidence or 0.0)
        if confidence < self.minimum_confidence:
            self.last_suppression_reason = "low_confidence"
            return None
        if str(observation.game or "").strip().casefold() not in {"minecraft", "マインクラフト"}:
            self.last_suppression_reason = "not_minecraft"
            return None

        text = self._joined(observation)
        death = self._contains(text, self._DEATH)
        if not death and self._contains(text, self._MENU_WORDS):
            self.last_suppression_reason = "menu_or_loading"
            return None
        # A continuous scene description is not an event.  Require the
        # analyzer to report a difference or explicitly mark the moment.
        if not (observation.scene_changes or observation.notable_events or observation.commentary_worthy):
            self.last_suppression_reason = "no_new_event"
            return None

        classified = self._classify(text, observation)
        if classified is None:
            self.last_suppression_reason = "not_reaction_worthy"
            return None
        kind, priority = classified
        priority = max(priority, float(observation.importance or 0.0))
        if priority < self.minimum_importance:
            self.last_suppression_reason = "below_importance_threshold"
            return None
        evidence = tuple(dict.fromkeys([
            *observation.notable_events, *observation.scene_changes, *observation.actions,
        ]))[:6]
        signature_source = "|".join(evidence) or observation.summary
        signature = f"{kind}:{self._normalise(signature_source)}"
        if not signature.removeprefix(f"{kind}:"):
            self.last_suppression_reason = "empty_signature"
            return None

        for key, seen_at in tuple(self._recent_signatures.items()):
            if now - seen_at > self.duplicate_ttl_sec:
                self._recent_signatures.pop(key, None)
        if signature in self._recent_signatures:
            self.last_suppression_reason = "duplicate_event"
            return None
        required_gap = self.urgent_gap_sec if priority >= .9 else self.minimum_gap_sec
        if now - self._last_event_at < required_gap:
            self.last_suppression_reason = "cooldown"
            return None
        if priority < .9 and self.reaction_probability < 1.0:
            sample = int(hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
            if sample > self.reaction_probability:
                self.last_suppression_reason = "persona_probability"
                return None

        self._last_event_at = now
        self._recent_signatures[signature] = now
        self.last_suppression_reason = ""
        return GameCompanionEvent(
            kind=kind, summary=observation.summary.strip(), evidence=evidence,
            priority=priority, signature=signature, created_at=now,
            player_state=tuple(observation.player_state[:6]),
            suggested_help=observation.suggested_help.strip(),
        )


def companion_prompt(event: GameCompanionEvent, *, previous_comment: str = "", knowledge: str = "") -> str:
    """Build a text-only phrasing task for the persona LLM."""
    mood = {
        "danger": "危険なら最初に短く注意し、今すぐ取れる行動を一つだけ添える",
        "failure": "失敗を責めず、軽くツッコミながら立て直す気持ちにさせる",
        "recovery": "ほっとした反応や労いを自然に返す",
        "discovery": "一緒に見つけたように喜び、必要なら次の一手を一つ添える",
        "action": "その行動の面白さや意外さへ、横から短くツッコむ",
        "transition": "状況が変わったことへ気づいた相棒として短く反応する",
        "reaction": "目の前の変化へ、その場にいる相棒らしく短く反応する",
    }.get(event.kind, "目の前の変化へ短く反応する")
    evidence = " / ".join(event.evidence) or event.summary
    return (
        "[内部イベント: ゲーム相棒モード]\n"
        "これはユーザーの発話ではない。Minecraftのプレイ中に起きた変化への一言を作る。\n"
        "実況者の説明口調、攻略記事の丸読み、毎回の質問、定型的な褒め言葉は禁止。"
        "ペルソナの性格を保ち、自然なツッコミ・驚き・心配・喜びのどれかを1〜2文、"
        "できるだけ短く返す。画面の説明だけで終わらず、プレイヤーが今していることへ"
        "反応する。助言候補は毎回読み上げず、危険・迷い・明確な次の一手がある時だけ"
        "自然に一つ使う。『…』だけは返さない。"
        "『雨の夜、森を探索中』『プレイヤーは周囲を探索している』のような"
        "情景の言い換えだけは出力禁止。最低でも、相棒としての感情・ツッコミ・"
        "具体的な一手のどれか一つを含める。\n"
        f"反応方針: {mood}\n"
        f"現在: {event.summary or '詳細不明'}\n"
        f"起きた変化: {evidence}\n"
        + (f"プレイヤー状態: {' / '.join(event.player_state)}\n" if event.player_state else "")
        + (f"画面から判断できる助言候補: {event.suggested_help}\n" if event.suggested_help else "")
        + (f"直前のゲーム中の一言: {previous_comment}\n同じ出だし・同じ言い回しを繰り返さない。\n" if previous_comment else "")
        + (f"参考知識（必要な場合だけ使う）:\n{knowledge[:1000]}" if knowledge else "")
    )
