"""話者レジストリ: 声紋で「誰が話しているか」を覚える。

- 発話ごとに声紋ベクトルを既知の話者と照合し、一致すればその人と判定
  (声紋の重心を更新して精度が上がっていく)。一致しなければ新しい人として
  「ゲストn」を自動登録する。
- 名前は会話から学習する (内省が名乗りを検出) ほか、UIから変更できる。
- 保存先はペルソナごとの data/speakers_<persona>.json。名前・声紋・相手ごとの
  親密度をキャラクターごとに独立して育てる。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class SpeakerRegistry:
    """声紋プロファイルの照合・登録・永続化。スレッドセーフ。"""

    def __init__(
        self,
        path: str | Path,
        threshold: float = 0.70,
        new_threshold: float = 0.50,
        max_speakers: int = 32,
        model_tag: str = "",
        sticky_window_s: float = 90.0,
        sticky_margin: float = 0.08,
    ):
        self._path = Path(path)
        self._threshold = float(threshold)          # これ以上 = 確実に同一人物 (声紋を学習)
        self._new_threshold = float(new_threshold)  # これ未満 = 明らかに別人 (新規登録)
        self._max = int(max_speakers)
        self._model_tag = str(model_tag)
        # 直近に話した人を「継続中の話者」として優先する時間と、乗り換えに必要な差。
        # 声紋スコアはターン毎に数%揺れるため、これが無いと会話中に名前が入れ替わる。
        self._sticky_window_s = max(0.0, float(sticky_window_s))
        self._sticky_margin = max(0.0, float(sticky_margin))
        self._lock = threading.Lock()
        self._profiles: list[dict[str, Any]] = []
        # ``last_seen`` is a wall clock, and on Windows ``time.time()`` only
        # advances about every 15ms.  Two utterances inside one tick therefore
        # carry an identical timestamp, and "who spoke last" would fall back to
        # list order.  This counter keeps that ordering exact (第13条).
        self._seen_seq = 0
        self._seen_order: dict[int, int] = {}
        self._load()

    @staticmethod
    def _score_for(profile: dict[str, Any], vec: np.ndarray) -> float:
        centroid = np.asarray(profile["vec"], dtype=np.float32)
        return float(vec @ centroid / (np.linalg.norm(centroid) + 1e-9))

    def _mark_seen(self, profile: dict[str, Any], now: float) -> None:
        """Record that this profile just spoke, in both clock and order."""
        profile["last_seen"] = now
        self._seen_seq += 1
        self._seen_order[int(profile["id"])] = self._seen_seq

    def _sticky_profile(self, now: float, exclude: set[str]) -> dict[str, Any] | None:
        """The person who was just speaking, if the conversation is continuous."""
        if self._sticky_window_s <= 0:
            return None
        recent = [
            item for item in self._profiles
            if item.get("name") not in exclude
            and now - float(item.get("last_seen", 0.0) or 0.0) <= self._sticky_window_s
        ]
        if not recent:
            return None
        return max(recent, key=lambda item: (
            float(item.get("last_seen", 0.0) or 0.0),
            self._seen_order.get(int(item["id"]), 0),
        ))

    # ---------- 永続化 ----------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            saved_tag = str(data.get("model_tag", ""))
            if self._model_tag and saved_tag != self._model_tag:
                # 声紋モデルが変わるとベクトルの互換性がないため作り直す
                backup = self._path.with_suffix(".bak.json")
                self._path.rename(backup)
                logger.warning(
                    "声紋モデルが変更されたため話者プロファイルをリセットしました "
                    "(旧: %s → 新: %s、バックアップ: %s)",
                    saved_tag or "不明", self._model_tag, backup.name,
                )
                return
            self._profiles = data.get("profiles", [])
        except Exception:
            logger.exception("話者プロファイルの読込に失敗 (%s)", self._path)

    def save(self) -> None:
        with self._lock:
            data = json.dumps(
                {"model_tag": self._model_tag, "profiles": self._profiles},
                ensure_ascii=False,
            )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(data, encoding="utf-8")
        except Exception:
            logger.exception("話者プロファイルの保存に失敗 (%s)", self._path)

    # ---------- 照合・登録 ----------

    def identify(self, vec: np.ndarray, allow_new: bool = True,
                 exclude_names: set[str] | None = None,
                 min_match: float | None = None) -> dict[str, Any] | None:
        """声紋を照合し、話者プロファイルを返す (必要なら新規登録)。

        exclude_names: 照合候補から除外する名前 (例: 通話ループバックには本人=チビは
        いないので、チビを除外して他人だけと照合し、本人への誤判定を防ぐ)。
        min_match: 「同一人物とみなす」下限スコア (既定=new_threshold)。これを下げると
        一度覚えた人が声のブレで外れにくくなる (Discord圧縮音声など声紋が不安定な用途)。

        返り値: {"id", "name", "auto_name", "is_new", "score", "turns", ...}
        """
        now = time.time()
        exclude = {str(x) for x in (exclude_names or ())}
        match_th = self._new_threshold if min_match is None else float(min_match)
        with self._lock:
            best = None
            best_score = -1.0
            for p in self._profiles:
                if p.get("name") in exclude:
                    continue  # 本人(チビ)など除外指定の話者は候補にしない
                c = np.asarray(p["vec"], dtype=np.float32)
                score = float(vec @ c / (np.linalg.norm(c) + 1e-9))
                if score > best_score:
                    best, best_score = p, score

            # Within one continuous conversation the speaker rarely changes,
            # but voiceprint scores wobble by a few hundredths between turns.
            # Without hysteresis the displayed name flips back and forth
            # mid-conversation, which reads as two different people talking.
            sticky = self._sticky_profile(now, exclude)
            sticky_score = -1.0 if sticky is None else self._score_for(sticky, vec)
            if (
                sticky is not None
                and best is not None
                and best is not sticky
                # Never stick to someone whose voice does not match at all;
                # that would suppress a genuine new speaker.
                and sticky_score >= match_th
                # Hysteresis exists to absorb the few-percent wobble between
                # turns.  It must never overrule a match that is already good
                # enough to count as the same person: doing so kept calling
                # ジーレン by the previous speaker's name right after their
                # profile was created.
                and best_score < self._threshold
                and best_score < self._sticky_margin + sticky_score
            ):
                logger.info(
                    "話者ヒステリシス: %s(%.2f) より僅差のため %s(%.2f) を維持",
                    best["name"], best_score, sticky["name"], sticky_score,
                )
                best, best_score = sticky, sticky_score

            if best is not None:
                logger.info("話者照合: %s に類似度 %.2f (確定≥%.2f / 同一≥%.2f)",
                            best["name"], best_score, self._threshold, match_th)

            if best is not None and best_score >= self._threshold:
                # 確実に同一人物 → 声紋の重心を更新 (最大50発話で頭打ち)
                n = min(int(best.get("n", 1)), 50)
                c = np.asarray(best["vec"], dtype=np.float32)
                c = (c * n + vec) / (n + 1)
                best["vec"] = (c / (np.linalg.norm(c) + 1e-9)).tolist()
                best["n"] = n + 1
                best["turns"] = int(best.get("turns", 0)) + 1
                self._mark_seen(best, now)
                return {**self._public(best), "is_new": False, "score": round(best_score, 3)}

            if best is not None and best_score >= match_th:
                # 一致(グレーゾーン含む) → 一番近い人として扱うが、声紋は学習しない
                # (マイク条件や体調で声は揺れる。安易に新規ゲストを作らない)
                best["turns"] = int(best.get("turns", 0)) + 1
                self._mark_seen(best, now)
                return {**self._public(best), "is_new": False, "score": round(best_score, 3)}

            if not allow_new or len(self._profiles) >= self._max:
                return None
            # 新しい話者として登録
            pid = 1 + max((int(p["id"]) for p in self._profiles), default=0)
            profile = {
                "id": pid,
                "name": f"ゲスト{pid}",
                "auto_name": True,   # 会話から本名を学習したら False に
                "vec": vec.tolist(),
                "n": 1,
                "turns": 1,
                "first_met": dt.date.today().isoformat(),
                "last_seen": now,
                "familiarity": 0.0,  # この人との親密度(相手ごと)。record_turnで育つ
                "last_day": "",      # 親密度の「その日ボーナス」判定用
            }
            self._profiles.append(profile)
            self._mark_seen(profile, now)
            logger.info("新しい話者を登録: %s (類似度最高 %.2f < %.2f)",
                        profile["name"], best_score, self._threshold)
            return {**self._public(profile), "is_new": True, "score": round(best_score, 3)}

    def set_name(self, speaker_id: int, name: str, learned: bool = False) -> bool:
        """話者に名前を付ける。learned=True は会話からの自動学習。

        自動学習した名前は、後の名乗りで上書き訂正できる (誤学習の自己修復)。
        UIから手動で付けた名前 (name_source="manual") だけは自動学習で
        上書きしない。
        """
        name = str(name).strip()[:20]
        if not name:
            return False
        with self._lock:
            for p in self._profiles:
                if int(p["id"]) == int(speaker_id):
                    if learned and p.get("name_source") == "manual":
                        return False  # 手動で付けた名前は自動学習で上書きしない
                    p["name"] = name
                    p["auto_name"] = False
                    p["name_source"] = "learned" if learned else "manual"
                    logger.info("話者%d の名前を「%s」に設定 (%s)",
                                speaker_id, name, "自動学習" if learned else "手動")
                    return True
        return False

    def set_alias(self, speaker_id: int, source: str, name: str) -> bool:
        """Record what this person is called on one surface.

        The same person is 「チビ」 at the local microphone and 「ジーレン」 in
        Discord.  Storing both against one profile lets the assistant address
        them the way that room expects without believing they are two people.
        """
        source = str(source or "").strip().lower()[:20]
        name = str(name or "").strip()[:20]
        if not source or not name:
            return False
        with self._lock:
            for profile in self._profiles:
                if int(profile["id"]) == int(speaker_id):
                    aliases = dict(profile.get("aliases") or {})
                    if aliases.get(source) == name:
                        return False
                    aliases[source] = name
                    profile["aliases"] = aliases
                    logger.info("話者%d の別名: %s では「%s」", speaker_id, source, name)
                    return True
        return False

    def display_name(self, speaker_id: int, source: str = "") -> str:
        """The name to use on this surface, falling back to the canonical one."""
        source = str(source or "").strip().lower()
        with self._lock:
            for profile in self._profiles:
                if int(profile["id"]) == int(speaker_id):
                    aliases = profile.get("aliases") or {}
                    return str(aliases.get(source) or profile.get("name") or "")
        return ""

    def merge(self, source_id: int, target_id: int, *,
              source_surface: str = "", target_surface: str = "") -> dict[str, Any] | None:
        """Fold ``source_id`` into ``target_id`` as one person.

        Two profiles for one person is not just a cosmetic problem: every
        relationship, dialogue state and working memory is keyed by speaker id,
        so the assistant genuinely knows them as two strangers.  Merging keeps
        the longer history on every axis rather than picking a winner.
        """
        source_id, target_id = int(source_id), int(target_id)
        if source_id == target_id:
            return None
        with self._lock:
            source = next((p for p in self._profiles if int(p["id"]) == source_id), None)
            target = next((p for p in self._profiles if int(p["id"]) == target_id), None)
            if source is None or target is None:
                return None

            # Voiceprint: weight each centroid by how many samples formed it,
            # so a well-established profile is not pulled apart by a new one.
            source_n = max(1, int(source.get("n", 1)))
            target_n = max(1, int(target.get("n", 1)))
            source_vec = np.asarray(source["vec"], dtype=np.float32)
            target_vec = np.asarray(target["vec"], dtype=np.float32)
            merged = (target_vec * target_n + source_vec * source_n) / (target_n + source_n)
            target["vec"] = (merged / (np.linalg.norm(merged) + 1e-9)).tolist()
            target["n"] = min(50, target_n + source_n)

            target["turns"] = int(target.get("turns", 0)) + int(source.get("turns", 0))
            target["familiarity"] = max(
                float(target.get("familiarity", 0.0)), float(source.get("familiarity", 0.0)),
            )
            target["last_seen"] = max(
                float(target.get("last_seen", 0.0) or 0.0),
                float(source.get("last_seen", 0.0) or 0.0),
            )
            self._seen_order[target_id] = max(
                self._seen_order.get(target_id, 0), self._seen_order.pop(source_id, 0),
            )
            first_met = [
                value for value in (target.get("first_met"), source.get("first_met")) if value
            ]
            if first_met:
                target["first_met"] = min(first_met)

            aliases = dict(target.get("aliases") or {})
            aliases.update(source.get("aliases") or {})
            if source_surface and source.get("name"):
                aliases.setdefault(str(source_surface).lower(), str(source["name"]))
            if target_surface and target.get("name"):
                aliases.setdefault(str(target_surface).lower(), str(target["name"]))
            target["aliases"] = aliases
            # A manually confirmed name survives an auto-generated one.
            if target.get("auto_name", True) and not source.get("auto_name", True):
                target["name"] = source.get("name", target.get("name"))
                target["auto_name"] = False
                target["name_source"] = source.get("name_source", "manual")

            merged_names = {
                "source_name": str(source.get("name", "")),
                "target_name": str(target.get("name", "")),
            }
            self._profiles = [p for p in self._profiles if int(p["id"]) != source_id]
            logger.info(
                "話者を統合: %s(id=%d) → %s(id=%d) / aliases=%s",
                merged_names["source_name"], source_id,
                merged_names["target_name"], target_id, aliases,
            )
        self.save()
        return {
            **self._public(target), **merged_names,
            "source_id": source_id, "target_id": target_id,
        }

    def merge_candidates(self, *, minimum: float = 0.82) -> list[dict[str, Any]]:
        """Pairs whose voiceprints are close enough to be the same person.

        Offered as a suggestion, never applied automatically: two siblings on
        one microphone would otherwise be silently fused.
        """
        with self._lock:
            profiles = [dict(item) for item in self._profiles]
        pairs: list[dict[str, Any]] = []
        for index, left in enumerate(profiles):
            left_vec = np.asarray(left["vec"], dtype=np.float32)
            for right in profiles[index + 1:]:
                right_vec = np.asarray(right["vec"], dtype=np.float32)
                score = float(
                    left_vec @ right_vec
                    / ((np.linalg.norm(left_vec) * np.linalg.norm(right_vec)) + 1e-9)
                )
                if score < minimum:
                    continue
                # Keep the profile with more history as the merge target.  On a
                # tie, prefer more voiceprint evidence and then the older id,
                # so the suggestion does not depend on list order.
                def rank(item: dict[str, Any]) -> tuple[int, int, int]:
                    return (
                        int(item.get("turns", 0)),
                        int(item.get("n", 0)),
                        -int(item["id"]),
                    )

                target, source = (
                    (left, right) if rank(left) >= rank(right) else (right, left)
                )
                pairs.append({
                    "score": round(score, 3),
                    "source_id": int(source["id"]), "source_name": str(source["name"]),
                    "target_id": int(target["id"]), "target_name": str(target["name"]),
                })
        pairs.sort(key=lambda item: item["score"], reverse=True)
        return pairs[:5]

    def forget(self, speaker_id: int) -> bool:
        with self._lock:
            before = len(self._profiles)
            self._profiles = [p for p in self._profiles if int(p["id"]) != int(speaker_id)]
            self._seen_order.pop(int(speaker_id), None)
            return len(self._profiles) < before

    # ---------- 参照 ----------

    @staticmethod
    def _public(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(p["id"]),
            "name": str(p["name"]),
            "auto_name": bool(p.get("auto_name", True)),
            "turns": int(p.get("turns", 0)),
            "first_met": p.get("first_met", ""),
            "last_seen": p.get("last_seen"),
            "familiarity": float(p.get("familiarity", 0.0)),
            "aliases": dict(p.get("aliases") or {}),
        }

    # ---------- 親密度 (相手ごと) ----------

    def on_turn(self, speaker_id: int) -> None:
        """指定話者との親密度を1ターンぶん育てる (相手ごとに独立)。"""
        from neuro_voice.mind.personality import bump_familiarity

        with self._lock:
            for p in self._profiles:
                if int(p["id"]) == int(speaker_id):
                    fam, today, _ = bump_familiarity(
                        p.get("familiarity", 0.0), p.get("last_day", ""))
                    p["familiarity"] = fam
                    p["last_day"] = today
                    return

    def relationship(self, speaker_id: int) -> dict[str, Any] | None:
        """指定話者との関係情報 (レベル/ラベル/距離感ヒント等) を返す。"""
        from neuro_voice.mind.personality import relationship_info

        with self._lock:
            for p in self._profiles:
                if int(p["id"]) == int(speaker_id):
                    return relationship_info(
                        p.get("familiarity", 0.0), p.get("turns", 0),
                        p.get("first_met", ""))
        return None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            out = [self._public(p) for p in self._profiles]
        out.sort(key=lambda p: -(p["last_seen"] or 0))
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._profiles)
