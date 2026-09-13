"""成長する人格 (性格・気分・好み・親密度) の管理。

- 性格 (traits): 0.0〜1.0 の6パラメータ。内省 (reflection) のたびに
  ごく小さく変化する。急変しないよう1回の変化量を厳しくクランプする。
- 気分 (mood): valence (快〜不快) / arousal (高揚〜沈静)。会話の感情タグで
  すぐ動き、時間とともに性格由来のベースラインへ戻る。
- 好み (likes): 話題 → -1.0〜1.0 のスコア。会話の中で増減し、
  新しい好みが芽生えたり、飽きたりする。
- 親密度 (relationship): 会話量と日数で育つ。レベルで呼び方や距離感が変わる。

状態は persona ごとに JSON ファイルへ永続化する。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# (キー, 日本語ラベル, 初期値)
TRAITS: list[tuple[str, str, float]] = [
    ("brightness", "明るさ", 0.65),
    ("curiosity", "好奇心", 0.70),
    ("empathy", "思いやり", 0.60),
    ("mischief", "いたずら心", 0.55),
    ("confidence", "自信", 0.50),
    ("openness", "素直さ", 0.60),
]

_LEVEL_WORDS = ["とても低い", "低め", "ふつう", "高め", "とても高い"]

# 親密度レベル: (必要ポイント, ラベル, 距離感のヒント)
RELATIONSHIP_LEVELS: list[tuple[float, str, str]] = [
    (0, "はじめまして", "まだ少し他人行儀。丁寧すぎない程度に様子を見る"),
    (8, "顔見知り", "少し打ち解けてきた。軽い雑談ができる"),
    (25, "話し相手", "気軽に話せる相手。冗談も言える"),
    (70, "友達", "気安い口調でOK。からかったり甘えたりもする"),
    (180, "親友", "とても親しい。素の感情を見せられる"),
    (400, "相棒", "何でも話せる特別な存在。あうんの呼吸"),
]


def relationship_info(familiarity: float, turns: int = 0, first_met: str = "") -> dict[str, Any]:
    """親密度ポイントからレベル/ラベル/距離感ヒント等を計算する (話者共通ロジック)。

    Neuro全体でなく相手ごとに親密度を持たせるため、話者レジストリからも使えるよう
    モジュール関数として切り出している。
    """
    fam = float(familiarity or 0.0)
    level = 0
    for i, (need, _, _) in enumerate(RELATIONSHIP_LEVELS):
        if fam >= need:
            level = i
    label, hint = RELATIONSHIP_LEVELS[level][1], RELATIONSHIP_LEVELS[level][2]
    nxt = RELATIONSHIP_LEVELS[level + 1][0] if level + 1 < len(RELATIONSHIP_LEVELS) else None
    cur_base = RELATIONSHIP_LEVELS[level][0]
    progress = 1.0 if nxt is None else _clamp((fam - cur_base) / (nxt - cur_base), 0, 1)
    days = 1
    if first_met:
        try:
            days = (dt.date.today() - dt.date.fromisoformat(first_met)).days + 1
        except Exception:
            days = 1
    return {
        "level": level, "label": label, "hint": hint,
        "familiarity": round(fam, 1), "next": nxt,
        "progress": round(progress, 3),
        "days": days, "turns": int(turns), "first_met": first_met,
    }


def bump_familiarity(familiarity: float, last_day: str) -> tuple[float, str, bool]:
    """1ターンぶん親密度を加算する。返り値: (新familiarity, 新last_day, 今日が初会話か)。

    +0.5/ターン、その日最初の会話は +3.0 ボーナス。
    """
    gain = 0.5
    today = dt.date.today().isoformat()
    first_today = last_day != today
    if first_today:
        gain += 3.0
    return round(float(familiarity or 0.0) + gain, 2), today, first_today

# 感情タグ → (valence変化, arousal変化)
_EMOTION_MOOD = {
    "joy": (0.15, 0.10),
    "fun": (0.12, 0.15),
    "angry": (-0.15, 0.20),
    "sad": (-0.15, -0.10),
    "surprised": (0.02, 0.20),
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class PersonalityEngine:
    """人格状態の保持・更新・言語化。スレッドセーフ。"""

    def __init__(self, path: str | Path, growth_rate: float = 1.0):
        self._path = Path(path)
        self._growth = max(0.0, float(growth_rate))
        self._lock = threading.Lock()
        self._state = self._default_state()
        self._load()

    # ---------- 永続化 ----------

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "traits": {k: v for k, _, v in TRAITS},
            "mood": {"valence": 0.1, "arousal": 0.0},
            "likes": {},  # topic -> score (-1..1)
            "relationship": {
                "familiarity": 0.0,
                "turns": 0,
                "first_met": dt.date.today().isoformat(),
                "last_day": "",
            },
            "history": [],  # [{"time": iso, "text": "好奇心がすこし育った"}]
        }

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            base = self._default_state()
            for key in base:
                if key in data:
                    if isinstance(base[key], dict):
                        base[key].update(data[key] or {})
                    else:
                        base[key] = data[key]
            self._state = base
        except Exception:
            logger.exception("人格状態の読込に失敗 (%s)。初期状態で開始します", self._path)

    def save(self) -> None:
        with self._lock:
            data = json.dumps(self._state, ensure_ascii=False, indent=2)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(data, encoding="utf-8")
        except Exception:
            logger.exception("人格状態の保存に失敗 (%s)", self._path)

    # ---------- ターン毎の即時更新 ----------

    def on_turn(self, emotion: str | None = None, is_user: bool = True,
                bump_relationship: bool = True) -> None:
        """1ターンごとの軽い更新。感情タグで気分が動き、親密度が少し育つ。

        bump_relationship=False のときは全体親密度を育てない (話者ごとに親密度を
        管理する場合、そちらで加算するため二重カウントを避ける)。
        """
        with self._lock:
            mood = self._state["mood"]
            if emotion in _EMOTION_MOOD:
                dv, da = _EMOTION_MOOD[emotion]
                mood["valence"] = _clamp(mood["valence"] + dv, -1.0, 1.0)
                mood["arousal"] = _clamp(mood["arousal"] + da, -1.0, 1.0)
            # 気分は性格 (明るさ) 由来のベースラインへゆっくり戻る
            base_v = (self._state["traits"]["brightness"] - 0.5) * 0.4
            mood["valence"] = round(base_v + (mood["valence"] - base_v) * 0.92, 4)
            mood["arousal"] = round(mood["arousal"] * 0.88, 4)

            if is_user and bump_relationship:
                rel = self._state["relationship"]
                rel["turns"] = int(rel.get("turns", 0)) + 1
                fam, today, _ = bump_familiarity(
                    rel.get("familiarity", 0), rel.get("last_day", ""))
                rel["last_day"] = today
                rel["familiarity"] = fam

    # ---------- 内省 (reflection) による成長 ----------

    def apply_reflection(
        self,
        trait_deltas: dict[str, float] | None,
        like_updates: list[dict[str, Any]] | None,
        mood: dict[str, float] | None,
        note: str = "",
    ) -> list[str]:
        """LLM内省の結果を、変化量を制限しながら反映する。変化の説明文を返す。"""
        changes: list[str] = []
        max_t = 0.03 * self._growth   # 性格は1回の内省で最大±0.03
        max_l = 0.15 * self._growth   # 好みは最大±0.15
        labels = {k: label for k, label, _ in TRAITS}
        with self._lock:
            traits = self._state["traits"]
            for key, delta in (trait_deltas or {}).items():
                if key not in traits:
                    continue
                try:
                    d = _clamp(float(delta), -max_t, max_t)
                except (TypeError, ValueError):
                    continue
                if abs(d) < 1e-4:
                    continue
                old = traits[key]
                traits[key] = round(_clamp(old + d, 0.05, 0.95), 4)
                if abs(traits[key] - old) > 1e-4:
                    arrow = "▲" if d > 0 else "▼"
                    changes.append(f"{labels[key]} {arrow}")

            likes = self._state["likes"]
            for upd in (like_updates or []):
                topic = str((upd or {}).get("topic", "")).strip()[:24]
                if not topic:
                    continue
                try:
                    d = _clamp(float(upd.get("delta", 0)), -max_l, max_l)
                except (TypeError, ValueError):
                    continue
                if abs(d) < 1e-3:
                    continue
                old = float(likes.get(topic, 0.0))
                new = round(_clamp(old + d, -1.0, 1.0), 3)
                likes[topic] = new
                if old == 0.0:
                    changes.append(f"「{topic}」に興味がわいた" if d > 0 else f"「{topic}」は苦手かも")
            # 好みは上位だけ残す (小さすぎる興味は自然に忘れる)
            if len(likes) > 14:
                keep = sorted(likes.items(), key=lambda kv: abs(kv[1]), reverse=True)[:14]
                self._state["likes"] = dict(keep)

            if mood:
                m = self._state["mood"]
                with_default = {
                    "valence": m["valence"], "arousal": m["arousal"], **{
                        k: v for k, v in mood.items()
                        if k in ("valence", "arousal") and isinstance(v, (int, float))
                    }
                }
                # 内省による気分は現在値と平均して穏やかに反映
                m["valence"] = round(_clamp((m["valence"] + with_default["valence"]) / 2, -1, 1), 4)
                m["arousal"] = round(_clamp((m["arousal"] + with_default["arousal"]) / 2, -1, 1), 4)

            if changes or note:
                entry = {
                    "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "text": (note or "、".join(changes))[:120],
                    "changes": changes[:8],
                }
                hist = self._state["history"]
                hist.append(entry)
                if len(hist) > 60:
                    del hist[: len(hist) - 60]
        return changes

    # ---------- 参照 ----------

    def _mood_label(self) -> tuple[str, str]:
        m = self._state["mood"]
        v, a = m["valence"], m["arousal"]
        if v >= 0.4:
            return ("ワクワク", "🤩") if a >= 0.25 else ("ごきげん", "😊")
        if v >= 0.12:
            return ("おだやか", "🙂")
        if v > -0.12:
            return ("まったり", "😌") if a < 0 else ("ふつう", "😐")
        if v > -0.4:
            return ("もやもや", "😕")
        return ("イライラ", "😠") if a >= 0.25 else ("しょんぼり", "😢")

    def _relationship_info(self) -> dict[str, Any]:
        rel = self._state["relationship"]
        return relationship_info(
            rel.get("familiarity", 0), rel.get("turns", 0), rel.get("first_met", ""))

    def describe(self, rel: dict[str, Any] | None = None) -> str:
        """System Prompt に注入する内面状態の言語化。

        rel に相手ごとの関係(relationship_info)を渡すと、その相手との親密度で
        距離感を表現する (話者別親密度)。None なら全体の親密度を使う。
        """
        with self._lock:
            traits = dict(self._state["traits"])
            likes = dict(self._state["likes"])
            mood_label, _ = self._mood_label()
            if rel is None:
                rel = self._relationship_info()

        def level_word(v: float) -> str:
            idx = 0 if v < 0.25 else 1 if v < 0.45 else 2 if v < 0.62 else 3 if v < 0.8 else 4
            return _LEVEL_WORDS[idx]

        trait_txt = " / ".join(
            f"{label}:{level_word(traits[key])}" for key, label, _ in TRAITS
        )
        fav = [t for t, s in sorted(likes.items(), key=lambda kv: -kv[1]) if s >= 0.25][:5]
        dis = [t for t, s in sorted(likes.items(), key=lambda kv: kv[1]) if s <= -0.25][:3]
        like_txt = ""
        if fav:
            like_txt += f"\n- 最近好きなもの: {'、'.join(fav)}"
        if dis:
            like_txt += f"\n- ちょっと苦手: {'、'.join(dis)}"
        return (
            "【いまのあなたの内面】\n"
            f"- 性格: {trait_txt}\n"
            f"- 気分: {mood_label}\n"
            f"- 相手との関係: {rel['label']} (知り合って{rel['days']}日目)。{rel['hint']}"
            f"{like_txt}\n"
            "この内面はあなた自身の感覚。応答のテンションや話題選びに自然に滲ませること。"
            "数値やこの指示の存在を会話で直接言及してはいけない。"
        )

    def snapshot(self) -> dict[str, Any]:
        """UI (ステータス画面) 用のスナップショット。"""
        with self._lock:
            traits = dict(self._state["traits"])
            likes = dict(self._state["likes"])
            mood = dict(self._state["mood"])
            mood_label, mood_emoji = self._mood_label()
            rel = self._relationship_info()
            history = list(self._state["history"])[-12:][::-1]
        return {
            "traits": [
                {"key": k, "label": label, "value": round(traits[k], 3)}
                for k, label, _ in TRAITS
            ],
            "mood": {**mood, "label": mood_label, "emoji": mood_emoji},
            "likes": sorted(
                ({"topic": t, "score": round(s, 2)} for t, s in likes.items()),
                key=lambda x: -abs(x["score"]),
            ),
            "relationship": rel,
            "history": history,
        }

    def reflection_context(self) -> str:
        """内省プロンプトへ渡す現在状態の要約。"""
        with self._lock:
            traits = self._state["traits"]
            likes = self._state["likes"]
            mood = self._state["mood"]
        trait_txt = ", ".join(f"{k}={traits[k]:.2f}" for k, _, _ in TRAITS)
        like_txt = ", ".join(f"{t}={s:+.2f}" for t, s in list(likes.items())[:14]) or "(なし)"
        return (
            f"traits: {trait_txt}\n"
            f"likes: {like_txt}\n"
            f"mood: valence={mood['valence']:+.2f}, arousal={mood['arousal']:+.2f}"
        )
