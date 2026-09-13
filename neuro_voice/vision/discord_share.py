"""Screen-share vision session backed by an OBS source.

Discord's DAVE audio receiver does not currently expose decoded Go Live video.
This module therefore keeps the conversational session independent from the
frame transport and uses OBS as the first transport implementation.  The same
session semantics are used by Discord-direct audio and the local microphone.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from neuro_voice.vision.service import (
    PerceptionService,
    VisualAnalyzer,
    is_explicit_vision_query,
    is_realtime_game_query,
)
from neuro_voice.vision.video import VideoObservationService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScreenShareControl:
    action: str
    matched: bool
    source_kind: str = "discord_screen_share"


_SHARE_SUBJECT = r"(?:Discord(?:の)?|ディスコード(?:の)?)?(?:画面共有|共有画面|配信画面)"
_STOP_RE = re.compile(
    rf"{_SHARE_SUBJECT}.{{0,12}}(?:見るの|見てるの|認識するの)?"
    r"(?:を)?(?:やめ|止め|とめ|見なく|見ない|閉じ)"
)
_START_RE = re.compile(
    rf"{_SHARE_SUBJECT}.{{0,12}}(?:を)?"
    r"(?:見て|見といて|見てて|見ながら|認識して|映像認識して)"
)
_MINECRAFT_SOURCE_RE = re.compile(
    r"(?:(?:OBS|ＯＢＳ|オービーエス)(?:の)?)?"
    r"(?:マイクラ|マインクラフト|Minecraft)"
    r"(?:(?:の)?(?:ゲーム)?画面)?"
    r".{0,10}(?:を)?(?:見て|見せて|映して|認識して)"
)


def classify_screen_share_control(text: str) -> ScreenShareControl:
    """Recognize explicit start/stop requests; stop always has precedence."""
    normalized = re.sub(r"\s+", "", str(text or ""))
    if re.search(
        rf"{_SHARE_SUBJECT}.{{0,12}}見て(?:って|と).{{0,12}}"
        r"(?:言|頼|お願い|伝え)",
        normalized,
    ):
        return ScreenShareControl("none", False)
    if _STOP_RE.search(normalized):
        return ScreenShareControl("stop", True)
    if _MINECRAFT_SOURCE_RE.search(normalized):
        return ScreenShareControl("start", True, "minecraft_obs")
    if _START_RE.search(normalized):
        return ScreenShareControl("start", True, "discord_screen_share")
    return ScreenShareControl("none", False)


class _ConfigOverlay:
    """Read-through config with session-local overrides.

    VideoObservationService writes diagnostic runtime values through ``set``.
    Keeping them in this overlay prevents a Discord share from silently
    replacing the local camera/game source selected by the user.
    """

    def __init__(self, base, overrides: dict[str, Any]):
        self._base = base
        self._overrides = dict(overrides)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overrides:
            return self._overrides[key]
        return self._base.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._overrides[key] = value

    def section(self, key: str) -> dict[str, Any]:
        base = dict(self._base.section(key))
        prefix = key + "."
        for dotted, value in self._overrides.items():
            if dotted.startswith(prefix) and "." not in dotted[len(prefix):]:
                base[dotted[len(prefix):]] = value
        return base


class DiscordScreenShareSession:
    """Owns one screen-share observation session.

    The conversation layer only depends on this session API. A future decoded
    DAVE video source can replace ``backend=obs`` without changing turn
    planning or prompt injection.
    """

    def __init__(
        self,
        cfg,
        llm,
        *,
        is_busy: Callable[[], bool] = lambda: False,
        on_frame: Callable[[Any], Any] | None = None,
        on_observation: Callable[[Any], Any] | None = None,
        service_factory=None,
        perception_factory=None,
    ):
        self._cfg = cfg
        self._llm = llm
        self._is_busy = is_busy
        self._on_frame = on_frame
        self._on_observation = on_observation
        self._service_factory = service_factory
        self._perception_factory = perception_factory
        self._service: VideoObservationService | None = None
        self._perception: PerceptionService | None = None
        self._runtime_cfg: _ConfigOverlay | None = None
        self._active = False
        self._requester = ""
        self._built_source = ""
        self._source_kind = "discord_screen_share"
        self._game_profile = ""
        self._capture_max_width = 1280
        self._prime_task: asyncio.Task | None = None
        self._preview_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._active and self._service is not None and self._service.is_running

    @property
    def source_name(self) -> str:
        return self._configured_source_name(self._source_kind)

    def _configured_source_name(self, source_kind: str) -> str:
        if source_kind == "minecraft_obs":
            configured = self._cfg.get(
                "discord.screen_share.minecraft_source_name", "",
            )
            return str(
                configured or self._cfg.get("video.obs.source_name", "") or ""
            ).strip()
        configured = self._cfg.get("discord.screen_share.obs_source_name", "")
        # Never fall back to the normal game source for a Discord share.
        # Doing so can make a successful capture falsely claim that it is
        # looking at the shared Discord window.
        return str(configured or "").strip()

    @property
    def source_kind(self) -> str:
        return self._source_kind

    @property
    def requester(self) -> str:
        return self._requester

    @property
    def latest_frame(self):
        """Latest frame from the selected OBS source, for UI preview only."""
        return self._service.latest_frame if self._service is not None else None

    def _build_perception(self) -> PerceptionService:
        if self._perception_factory is not None:
            return self._perception_factory()
        vision_backend = self._llm
        if getattr(self._llm, "name", "") == "ollama":
            from neuro_voice.llm.ollama_multimodal import OllamaMultimodalClient

            vision_backend = OllamaMultimodalClient(
                str(self._cfg.get("llm.backends.ollama.base_url", "http://localhost:11434/v1")),
                str(self._cfg.get("vision.model", "gemma4-12b-qat")),
                timeout_s=float(self._cfg.get("vision.request_timeout_sec", 45)),
                keep_alive=self._cfg.get("llm.keep_alive"),
                retries=1,
            )
        return PerceptionService(
            VisualAnalyzer(
                vision_backend,
                timeout_s=float(self._cfg.get("vision.request_timeout_sec", 45)),
                max_output_tokens=int(
                    self._cfg.get(
                        (
                            "discord.screen_share.minecraft_max_output_tokens"
                            if self._source_kind == "minecraft_obs"
                            else "discord.screen_share.max_output_tokens"
                        ),
                        self._cfg.get("vision.realtime.max_output_tokens", 160),
                    )
                ),
                context_size=int(self._cfg.get("vision.realtime.context_size", 4096)),
            ),
            memory_seconds=float(self._cfg.get("vision.visual_memory_seconds", 30.0)),
        )

    def _ensure_service(self) -> VideoObservationService:
        if self._service is not None:
            return self._service
        source = self.source_name
        self._runtime_cfg = _ConfigOverlay(
            self._cfg,
            {
                "video.input": "obs",
                "video.obs.source_name": source,
                # Discord shares are generic content. Inheriting the global
                # Minecraft profile made Granblue and desktop shares get
                # force-classified as Minecraft.
                "video.game_profile": self._game_profile,
                "video.capture_max_width": self._capture_max_width,
                "vision.capture_fps": float(
                    self._cfg.get("discord.screen_share.capture_fps", 1.0)
                ),
                "video.max_frames_per_analysis": int(
                    self._cfg.get("discord.screen_share.max_frames_per_analysis", 1)
                ),
            },
        )
        self._perception = self._build_perception()
        self._built_source = source
        if self._service_factory is not None:
            self._service = self._service_factory(
                self._runtime_cfg, self._perception, self._is_busy
            )
        else:
            self._service = VideoObservationService(
                self._runtime_cfg,
                self._perception,
                is_busy=self._is_busy,
                on_observation=self._on_observation,
            )
        return self._service

    def _select_source(self, source_kind: str) -> None:
        self._source_kind = (
            "minecraft_obs" if source_kind == "minecraft_obs"
            else "discord_screen_share"
        )
        if self._source_kind == "minecraft_obs":
            self._game_profile = str(
                self._cfg.get("video.game_profile", "minecraft") or "minecraft"
            ).strip().lower()
            self._capture_max_width = max(
                640,
                int(self._cfg.get(
                    "discord.screen_share.minecraft_capture_max_width", 768,
                )),
            )
        else:
            self._game_profile = ""
            self._capture_max_width = max(
                960,
                int(self._cfg.get("discord.screen_share.capture_max_width", 1280)),
            )

    async def start(
        self, *, requester: str = "", source_kind: str = "discord_screen_share",
    ) -> tuple[bool, str]:
        if not bool(self._cfg.get("discord.screen_share.enabled", True)):
            return False, "Discordの画面共有認識は設定で無効になってるよ。"
        backend = str(self._cfg.get("discord.screen_share.backend", "obs")).strip().lower()
        if backend != "obs":
            return False, "今使える画面共有の受信方式はOBSだけだよ。"
        selected_kind = (
            "minecraft_obs" if source_kind == "minecraft_obs"
            else "discord_screen_share"
        )
        target_source = self._configured_source_name(selected_kind)
        if self.active and (
            selected_kind != self._source_kind
            or self._built_source != target_source
        ):
            await self.close()
        self._select_source(selected_kind)
        source = target_source
        if not source:
            label = "マイクラ" if selected_kind == "minecraft_obs" else "画面共有"
            return False, f"OBSの{label}ソース名が未設定だよ。設定から選んでね。"
        if self.active:
            self._requester = requester or self._requester
            return True, f"もう見てるよ。OBSの「{source}」を追いかけてる。"
        if self._service is not None and self._built_source != source:
            await self.close()
            self._select_source(selected_kind)
        service = self._ensure_service()
        ok = await service.start()
        if not ok:
            detail = service.last_capture_error
            suffix = f"（{detail}）" if detail else ""
            return False, f"OBSの「{source}」を開けなかったよ{suffix}"
        self._active = True
        self._requester = requester
        # Cold vision startup can take ten seconds or more. Start it alongside
        # the short acknowledgement, then keep it alive if a dialogue turn
        # times out instead of repeatedly cancelling and restarting it.
        self._prime_task = asyncio.create_task(
            service.ensure_current_analysis(
                request_id=f"screen-share-prime-{uuid4().hex}",
                timeout_s=None,
            ),
            name="screen-share-initial-analysis",
        )
        self._preview_task = asyncio.create_task(
            self._publish_first_frame(service),
            name="screen-share-initial-preview",
        )
        logger.info(
            "OBS conversation vision started: kind=%s source=%s width=%d profile=%s",
            self._source_kind, source, self._capture_max_width,
            self._game_profile or "generic",
        )
        label = "マイクラ画面" if self._source_kind == "minecraft_obs" else "Discord画面共有"
        return True, (
            f"うん、{label}としてOBSの「{source}」を見始めたよ。"
            "画面についてそのまま聞いて。"
        )

    async def _publish_first_frame(self, service: VideoObservationService) -> None:
        try:
            frame = await service.wait_for_first_frame(timeout_s=3.0)
            if frame is None or self._on_frame is None:
                return
            result = self._on_frame(frame)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Unable to publish screen-share preview", exc_info=True)

    async def stop(self, *, reason: str = "user") -> tuple[bool, str]:
        if self._service is None or not self._active:
            return True, "今は画面共有を見てないよ。"
        await self._service.stop()
        self._active = False
        self._requester = ""
        logger.info("Discord screen-share vision stopped: reason=%s", reason)
        return True, "画面共有を見るのをやめたよ。"

    async def handle_control(self, text: str, *, requester: str = "") -> tuple[bool, str | None]:
        control = classify_screen_share_control(text)
        if not control.matched:
            return False, None
        if control.action == "stop":
            _, reply = await self.stop(reason="voice_command")
            return True, reply
        _, reply = await self.start(
            requester=requester, source_kind=control.source_kind,
        )
        return True, reply

    @staticmethod
    def _format_fresh_observation(text: str, observation) -> str:
        """Turn one freshly analyzed frame into a short spoken answer.

        This intentionally avoids a second LLM pass. With the current 12B
        model, structured vision plus ordinary response generation took
        25–30 seconds and made a question-bound frame sound like old memory.
        """
        normalized = re.sub(r"\s+", "", str(text or ""))
        summary = str(getattr(observation, "summary", "") or "").strip()
        if summary.startswith("{"):
            # A streamed JSON object can be truncated at the output limit.
            # Never expose its schema or read internal field names aloud.
            match = re.search(
                r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)',
                summary,
            )
            summary = (
                match.group(1).replace(r"\"", '"').replace(r"\n", " ").strip()
                if match else ""
            )
        visible_text = [
            str(item).strip() for item in
            (getattr(observation, "visible_text", None) or [])
            if str(item).strip()
        ]
        state_labels = {
            "location_estimate": "場所",
            "health_estimate": "体力",
            "hunger_estimate": "空腹",
            "combat_state": "戦闘状況",
            "nearby_entities": "近くにいるもの",
            "held_item": "手に持っているもの",
        }
        player_state: list[str] = []
        state_details = getattr(observation, "player_state_details", None) or {}
        if isinstance(state_details, dict):
            for key, label in state_labels.items():
                value = state_details.get(key)
                if value in (None, "", [], {}, False):
                    continue
                if isinstance(value, (list, tuple)):
                    value = "、".join(str(item) for item in value if str(item).strip())
                if value:
                    player_state.append(f"{label}は{value}")
        if not player_state:
            for item in (getattr(observation, "player_state", None) or []):
                value = str(item).strip()
                if not value:
                    continue
                match = re.fullmatch(r"([a-z_]+)\s*:\s*(.+)", value)
                if match:
                    key, raw_value = match.groups()
                    # UI-open booleans are analyzer internals, not spoken facts.
                    if key in {"inventory_open", "menu_open"}:
                        continue
                    label = state_labels.get(key)
                    if label:
                        player_state.append(f"{label}は{raw_value}")
                    continue
                if not re.search(r"[a-z_]+\s*:", value, re.IGNORECASE):
                    player_state.append(value)
        suggested_help = str(
            getattr(observation, "suggested_help", "") or ""
        ).strip()
        actions = [
            str(item).strip() for item in
            (getattr(observation, "actions", None) or [])
            if str(item).strip()
        ]
        notable_events = [
            str(item).strip() for item in
            (getattr(observation, "notable_events", None) or [])
            if str(item).strip()
        ]

        def add_unique(parts: list[str], value: str) -> None:
            value = re.sub(r"\s+", " ", str(value or "")).strip().rstrip("。")
            if not value:
                return
            compact = re.sub(r"[\s、。！？!?]", "", value)
            for current in parts:
                current_compact = re.sub(r"[\s、。！？!?]", "", current)
                if compact == current_compact or (
                    len(compact) >= 8 and (
                        compact in current_compact or current_compact in compact
                    )
                ):
                    return
            parts.append(value)

        if any(term in normalized for term in (
            "文字", "読ん", "読め", "書いて", "なんて", "何て",
        )):
            if visible_text:
                quoted = "、".join(f"「{item}」" for item in visible_text[:6])
                return f"{quoted}って読めるよ。"
            if summary:
                return (
                    f"{summary.rstrip('。')}。"
                    "ただ、文字ははっきり判別できなかったよ。"
                )
            return "今のフレームでは、読める文字をはっきり判別できなかったよ。"

        parts: list[str] = []
        wants_advice = any(term in normalized for term in (
            "どうしたら", "どうすれば", "どうしよう", "助けて",
            "次は", "次に", "何すれば", "なにすれば",
        ))
        wants_state = any(term in normalized for term in (
            "どこ", "状態", "体力", "持って", "装備",
        ))
        # A request for help should hear the useful action first. Other visual
        # questions start with the grounded situation, then mention what the
        # player is doing and one useful next step when the analyzer supplied it.
        if wants_advice and suggested_help:
            add_unique(parts, suggested_help)
        add_unique(parts, summary)
        if actions:
            add_unique(parts, actions[0])
        if wants_state and player_state:
            add_unique(parts, "状態は" + "、".join(player_state[:5]))
        elif notable_events:
            add_unique(parts, notable_events[0])
        if suggested_help and not wants_advice and len(parts) < 3:
            add_unique(parts, suggested_help)
        if not parts:
            return (
                "今の画面から、確実に言える状況までは読み取れなかったよ。"
            )
        return "。".join(parts[:3]) + "。"

    async def answer_current_visual_question(
        self, text: str, *, response_id: str = "",
    ) -> str | None:
        """Answer an explicit current-screen turn from a newly captured frame.

        Old background/cold-start observations are never used by this path.
        The structured vision result is spoken directly so the same 12B model
        is not invoked a second time merely to paraphrase it.
        """
        if not self.active or self._service is None:
            return None
        if not self.wants_current_visual_answer(text):
            return None
        direct_answer = self.wants_direct_visual_answer(text)
        timeout_key = (
            "discord.screen_share.direct_answer_timeout_sec"
            if direct_answer
            else "discord.screen_share.reasoning_refresh_timeout_sec"
        )
        timeout_default = 18.0 if direct_answer else 6.5
        timeout = max(
            1.0,
            float(self._cfg.get(
                timeout_key, timeout_default,
            )),
        )
        request_id = response_id or f"current-screen-{uuid4().hex}"
        try:
            observation, pending = await self._service.ensure_current_analysis(
                request_id=request_id,
                timeout_s=timeout,
                force_fresh=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Fresh current-screen analysis failed", exc_info=True)
            return (
                "今の画面を撮り直したけど、解析に失敗したよ。"
                "前の景色を今の景色として答えるのはやめておくね。"
            )
        frame = self._service.last_explicit_frame
        frame_metadata = (
            (getattr(frame, "metadata", {}) or {}) if frame is not None else {}
        )
        frame_matches_request = bool(
            frame is not None
            and str(frame_metadata.get("explicit_request_id", "")) == request_id
        )
        if frame_matches_request and self._on_frame is not None:
            result = self._on_frame(frame)
            if inspect.isawaitable(result):
                await result
        elif frame is not None:
            logger.warning(
                "Stale explicit preview suppressed: request=%s frame_request=%s "
                "seq=%s",
                request_id,
                frame_metadata.get("explicit_request_id", ""),
                frame_metadata.get("capture_sequence", "-"),
            )
        if pending or observation is None:
            logger.info(
                "Fresh current-screen answer timed out: request=%s timeout=%.1fs",
                request_id, timeout,
            )
            if not direct_answer:
                # The normal conversation path can still answer from the
                # timestamped last observation and official game knowledge.
                # Do not replace the user's question with a waiting sentence.
                self._fresh_context_response_id = request_id
                self._fresh_context_at = time.monotonic()
                return None
            return (
                f"今のフレームは撮り直せたけど、{timeout:.0f}秒以内に解析が"
                "終わらなかったよ。前の景色を今の景色として答えるのはやめておくね。"
            )
        logger.info(
            "Fresh current-screen answer ready: request=%s captured_age=%.1fs "
            "source=%s",
            request_id,
            max(0.0, time.time() - float(observation.captured_at or time.time())),
            self.source_name,
        )
        if not direct_answer:
            # This turn needs the frame as evidence for reasoning (for example
            # "can I craft a campfire with what I have?").  Let the ordinary
            # conversation planner combine it with official knowledge instead
            # of reading the vision model's scene summary as the final answer.
            self._fresh_context_response_id = request_id
            self._fresh_context_at = time.monotonic()
            return None
        return self._format_fresh_observation(text, observation)

    def wants_current_visual_answer(self, text: str) -> bool:
        """Whether this turn requires a newly captured screen frame.

        A stable Minecraft how-to such as "how do I cook this pork?" must use
        official recipe data, not spend several seconds asking the vision
        model to describe whatever happens to be on screen. An explicit
        "look at this screen" request still wins.
        """
        if is_explicit_vision_query(text):
            return True
        if self.needs_visual_reasoning(text):
            return True
        if self._source_kind == "minecraft_obs":
            from neuro_voice.games import is_minecraft_knowledge_query

            if is_minecraft_knowledge_query(text):
                return False
        return is_realtime_game_query(text)

    @staticmethod
    def needs_visual_reasoning(text: str) -> bool:
        """Whether the answer depends on both the live frame and reasoning."""
        compact = re.sub(r"\s+", "", str(text or "")).casefold()
        if not compact:
            return False
        grounded = any(term in compact for term in (
            "今", "いま", "現在", "持って", "手持ち", "インベントリ",
            "この画面", "画面の", "見えて", "ここから", "この状態",
        ))
        judgment = any(term in compact for term in (
            "作れる", "つくれる", "できる", "出来る", "足りる", "使える",
            "焼ける", "焼けば", "焼く", "調理でき", "クラフトでき",
            "どうやって",
            "どうしたら", "どうすれば", "どうしよう", "次は", "次に",
            "何すれば", "なにすれば", "何を作", "何が作", "どれを",
            "おすすめ", "方がいい", "助けて",
        ))
        return grounded and judgment

    def wants_direct_visual_answer(self, text: str) -> bool:
        """Whether structured vision alone can safely answer the question."""
        if not self.wants_current_visual_answer(text):
            return False
        compact = re.sub(r"\s+", "", str(text or "")).casefold()
        if self.needs_visual_reasoning(compact):
            return False
        if any(term in compact for term in (
            "どうしたら", "どうすれば", "どうしよう", "次は", "次に",
            "どうやって", "何すれば", "なにすれば", "作れる", "つくれる", "できる",
            "出来る", "足りる", "使える", "おすすめ", "方がいい", "助けて",
            "焼ける", "焼けば", "焼く", "作り", "つくり", "クラフト",
            "攻略", "方法", "材料", "レシピ",
        )):
            return False
        # Direct output is intentionally a whitelist.  An unrecognised
        # question is safer through the conversation planner than as a raw
        # scene-summary narration.
        return any(term in compact for term in (
            "何が見える", "なにが見える", "何見える", "なに見える",
            "どんな画面", "どんな景色", "見えてる景色", "景色は",
            "今の画面", "画面どう", "画面見てる", "画面を見てる",
            "画面見えてる", "今見てる", "いま見てる",
            "画面を見て", "画像を見て", "今どこ", "どこにいる",
            "何してる", "なにしてる", "何をしてる", "どうなって",
            "状態は", "体力", "空腹", "装備は", "何持ってる",
            "持ち物を見", "文字", "読ん", "読め", "何て書", "なんて書",
        ))

    async def context_for_turn(self, text: str, *, response_id: str = "") -> str | None:
        if not self.active or self._service is None:
            return None
        explicit = self.wants_current_visual_answer(text)
        timed_out = False
        already_refreshed = bool(
            response_id
            and response_id == getattr(self, "_fresh_context_response_id", "")
            and time.monotonic() - float(
                getattr(self, "_fresh_context_at", float("-inf"))
            ) < 15.0
        )
        if (
            explicit
            and not already_refreshed
            and bool(self._cfg.get("discord.screen_share.fresh_on_explicit_query", True))
        ):
            timeout = max(
                0.2,
                float(self._cfg.get("discord.screen_share.explicit_wait_timeout_sec", 2.5)),
            )
            try:
                _, timed_out = await self._service.ensure_current_analysis(
                    request_id=response_id or f"discord-share-{uuid4().hex}",
                    timeout_s=timeout,
                )
                if timed_out:
                    logger.info(
                        "Discord screen-share analysis still running after %.1fs; "
                        "preserving it for the next turn",
                        timeout,
                    )
            except asyncio.TimeoutError:
                # Defensive only; ensure_current_analysis reports pending
                # without raising or cancelling the underlying task.
                timed_out = True
                logger.info(
                    "Discord screen-share fresh analysis exceeded %.1fs; using timestamped state",
                    timeout,
                )
            except Exception:
                logger.warning("Discord screen-share explicit analysis failed", exc_info=True)
                self._service.request_priority_analysis()
        context = self._service.state_context(text)
        status = self._service.debug_status()
        if not context:
            capture_count = status.get("capture_count", 0)
            frame_age = status.get("latest_frame_age_sec")
            worker = status.get("worker_state", "capturing")
            label = (
                "OBSマイクラ画面" if self._source_kind == "minecraft_obs"
                else "Discord画面共有"
            )
            return (
                f"【{label}・受信確認済み】"
                f"対象ソース={self.source_name}; 取得フレーム数={capture_count}; "
                f"最新フレーム経過秒={frame_age}; worker={worker}。"
                "OBS映像の受信には成功しているが、意味解析の初回結果はまだない。"
                "画面が見えていないとは答えず、受信済みで意味解析中だと一度だけ短く伝えること。"
                "同じ待機文を何度も繰り返さないこと。"
            )
        freshness = status.get("latest_observation_age_sec")
        warning = (
            " 最新フレームの解析は応答待ち時間内に完了しなかった。"
            "下記は時刻付きの直近状態であり、現在も同じだと断定しないこと。"
            if timed_out
            else ""
        )
        if self._source_kind == "minecraft_obs":
            source_contract = (
                "【OBSマイクラ画面・継続認識】"
                f"対象ソース={self.source_name}; 解析状態の経過秒={freshness}。"
                "これはOBSのMinecraftゲームキャプチャである。"
            )
        else:
            source_contract = (
                "【Discord画面共有・OBS継続認識】"
                f"対象ソース={self.source_name}; 解析状態の経過秒={freshness}。"
                "これはDiscord通話参加者が共有している画面の観察である。"
            )
        return (
            source_contract
            + "会話履歴やAI自身の経験ではない。質問に関係する時だけ使うこと。"
              "まずユーザーが実際に尋ねたことへ答えること。視覚要約を言い換えるだけの"
              "ナレーションは禁止。判断や攻略の質問では、画面上の根拠と検証済みゲーム知識を"
              "組み合わせ、できること・不足しているもの・次の一手を具体的に答えること。"
              "inventory_open、menu_openのような内部キーやTrue/Falseは発話しないこと。"
              "画面内の文字を聞かれた場合はvisible_textにある読めた文字を直接答え、"
              "ゲーム名を聞き返したり曖昧な感想へ逃げたりしないこと。"
              "文字を判別できない場合だけ、判別できない領域を具体的に伝えること。"
            + f"{warning}\n{context}"
        )

    def debug_status(self) -> dict[str, Any]:
        status = self._service.debug_status() if self._service is not None else {}
        return {
            "active": self.active,
            "backend": str(self._cfg.get("discord.screen_share.backend", "obs")),
            "source_name": self.source_name,
            "source_kind": self._source_kind,
            "capture_max_width": self._capture_max_width,
            "game_profile": self._game_profile,
            "requester": self._requester,
            **status,
        }

    async def close(self) -> None:
        for task in (self._preview_task, self._prime_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._preview_task, self._prime_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._preview_task = None
        self._prime_task = None
        if self._service is not None and self._active:
            await self._service.stop()
        self._active = False
        if self._perception is not None:
            await self._perception.close()
        self._service = None
        self._perception = None
        self._runtime_cfg = None
        self._built_source = ""
