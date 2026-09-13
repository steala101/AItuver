"""Folder-backed game profile registry.

Profiles are declarative data.  They may select an explicitly registered
runtime, but they can never name arbitrary Python objects to import.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def default_profile_root() -> Path:
    return Path(__file__).resolve().parents[2] / "game_profiles"


@dataclass(frozen=True, slots=True)
class GameProfile:
    id: str
    display_name: str
    display_name_ja: str = ""
    aliases: tuple[str, ...] = ()
    assistant_role: str = ""
    interaction_mode: str = "conversation"
    vision_policy: str = "none"
    knowledge_enabled_default: bool = True
    commentary_enabled_default: bool = False
    analysis_instruction: str = ""
    conversation_instruction: str = ""
    #: 参照するマニュアルの版と検証コード。`profile.json` の `manual` から読む。
    #: 以前は同じ値が `session.py` にも直接書かれていて、`profile.json` を
    #: 直しても発話は古い版を言い続ける状態だった。
    manual_version: str = ""
    manual_verification_code: str = ""
    folder: Path = field(default_factory=Path)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def manual_reference(self) -> str:
        """人へ言える形にした版と検証コード。無ければ空。"""
        parts = []
        if self.manual_version:
            parts.append(f"版は{self.manual_version}")
        if self.manual_verification_code:
            parts.append(f"検証コードは{self.manual_verification_code}")
        return "参照マニュアルの" + "、".join(parts) + "。" if parts else ""

    @property
    def label(self) -> str:
        if self.display_name_ja and self.display_name_ja != self.display_name:
            return f"{self.display_name_ja} / {self.display_name}"
        return self.display_name

    @property
    def allows_vision(self) -> bool:
        return self.vision_policy == "observe_gameplay"

    def public_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "assistant_role": self.assistant_role,
            "interaction_mode": self.interaction_mode,
            "vision_policy": self.vision_policy,
            "knowledge_enabled_default": self.knowledge_enabled_default,
            "commentary_enabled_default": self.commentary_enabled_default,
        }


class GameProfileRegistry:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_profile_root()
        self._profiles: dict[str, GameProfile] = {}
        self._aliases: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        profiles: dict[str, GameProfile] = {}
        aliases: dict[str, str] = {}
        if not self.root.exists():
            self._profiles, self._aliases = profiles, aliases
            return
        for profile_path in sorted(self.root.glob("*/profile.json")):
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            profile_id = str(data.get("id", "")).strip().lower()
            if not _PROFILE_ID.fullmatch(profile_id):
                raise ValueError(f"Invalid game profile id: {profile_id!r}")
            if profile_path.parent.name != profile_id:
                raise ValueError(
                    f"Profile folder must match id: {profile_path.parent.name!r} != {profile_id!r}"
                )
            if profile_id in profiles:
                raise ValueError(f"Duplicate game profile id: {profile_id}")
            display_name = str(data.get("display_name", "")).strip()
            if not display_name:
                raise ValueError(f"Game profile {profile_id!r} has no display_name")
            manual = data.get("manual")
            manual = manual if isinstance(manual, dict) else {}
            profile = GameProfile(
                id=profile_id,
                display_name=display_name,
                display_name_ja=str(data.get("display_name_ja", "")).strip(),
                aliases=tuple(str(x).strip() for x in data.get("aliases", []) if str(x).strip()),
                assistant_role=str(data.get("assistant_role", "")).strip(),
                interaction_mode=str(data.get("interaction_mode", "conversation")).strip(),
                vision_policy=str(data.get("vision_policy", "none")).strip(),
                knowledge_enabled_default=bool(data.get("knowledge_enabled_default", True)),
                commentary_enabled_default=bool(data.get("commentary_enabled_default", False)),
                analysis_instruction=str(data.get("analysis_instruction", "")).strip(),
                conversation_instruction=str(data.get("conversation_instruction", "")).strip(),
                manual_version=str(manual.get("version", "")).strip(),
                manual_verification_code=str(manual.get("verification_code", "")).strip(),
                folder=profile_path.parent.resolve(),
                raw=data,
            )
            profiles[profile_id] = profile
            for value in (profile_id, profile.display_name, profile.display_name_ja, *profile.aliases):
                key = value.strip().casefold()
                if key:
                    aliases.setdefault(key, profile_id)
        self._profiles, self._aliases = profiles, aliases

    def all(self) -> list[GameProfile]:
        return list(self._profiles.values())

    def get(self, profile_id: str | None) -> GameProfile | None:
        key = str(profile_id or "").strip().casefold()
        resolved = self._aliases.get(key, key)
        return self._profiles.get(resolved)

    def alias_items(self) -> list[tuple[str, str]]:
        """(照合キー, profile_id) の一覧。長いものが先。

        会話から「どのゲームの話か」を取り出すために使う。長い別名を先に
        見ることで、「keep talking and nobody explodes」が「keep talking」で
        途中一致して終わることがない。
        """
        return sorted(self._aliases.items(), key=lambda item: -len(item[0]))

    def labels(self) -> list[str]:
        """人へ読み上げる用の一覧。「今使えるのは〜」に使う。"""
        return [profile.label for profile in self.all()]

    def public_summaries(self) -> list[dict[str, Any]]:
        return [profile.public_summary() for profile in self.all()]


def profile_from_config(cfg) -> GameProfile | None:
    root = cfg.get("game_profiles.root", "") if cfg is not None else ""
    registry = GameProfileRegistry(root or None)
    return registry.get(cfg.get("video.game_profile", "") if cfg is not None else "")


def profile_allows_vision(cfg) -> bool:
    profile = profile_from_config(cfg)
    return profile is None or profile.allows_vision
