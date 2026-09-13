"""Structured Gemma/Ollama visual analysis and short-term perception memory."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from neuro_voice.media import InferencePriority, InferencePriorityGate, MediaInput
from neuro_voice.vision.media import hash_distance
from neuro_voice.vision.models import PerceptionMemory, VisualObservation

logger = logging.getLogger(__name__)

_IMAGE_PROMPT = """この画像を、リアルタイム会話AIが利用するために解析してください。
前回の観察との差分を優先し、会話に有用な情報だけを返してください。
JSONのみを返してください:
{"summary":"短い状況説明","people":[],"objects":[],"actions":[],"visible_text":[],"scene_changes":[],"conversation_relevance":"none|low|medium|high","confidence":0.0}
断定できない内容は推測しない。顔から個人名、機微な属性、見えない動機を推測しない。"""

_VIDEO_PROMPT = """以下の画像は同じカメラ映像から時系列順に取得したフレームです。最初が最も古く最後が最新です。
静止物の列挙ではなく時間とともに何が変化したかを優先し、JSONのみを返してください:
{"summary":"現在の状況","people":[],"objects":[],"actions":[],"visible_text":[],"scene_changes":[],"conversation_relevance":"none|low|medium|high","confidence":0.0}
フレームから確認できない動機や意図は推測しない。"""


_MINECRAFT_PROMPT = """You are observing a Minecraft game screen for a real-time companion.
Describe only what is visibly supported by the frames. Identify the current player situation,
important changes (danger, combat, low health/hunger, nightfall, discovery, inventory/crafting
decision, death screen, objective progress, or a newly started/completed player action), and a
short helpful suggestion when appropriate. Populate scene_changes only with a change from the
previous structured observation, never with a static description of the current screen.
Put the player's concrete current action in actions (for example swimming, fleeing, eating,
mining, aiming, crafting, or standing still); do not repeat the scene summary there.
When inventory or crafting UI is open, put every confidently readable item name and visible
stack count in objects. Do not guess an unclear icon or count.
When a useful next move is visibly justified, put exactly one immediately actionable step in
suggested_help. Leave it empty when the frame does not support safe advice.
Do not invent coordinates, items, enemies, or facts that are not visible. Return JSON only:
{"scene_type":"minecraft_gameplay|menu|loading|error|unknown","summary":"short current situation","game":"minecraft|unknown","people":[],"objects":[],"actions":[],"visible_text":[],"scene_changes":[],"player_state":{"location_estimate":"","health_estimate":"","hunger_estimate":"","inventory_open":false,"menu_open":false,"combat_state":"","nearby_entities":[],"held_item":""},"notable_events":[],"suggested_help":"","commentary_worthy":false,"conversation_relevance":"none|low|medium|high","importance":0.0,"confidence":0.0}
Set commentary_worthy true for a clear new action or situation change a co-player would naturally
react to. Keep it false for unchanged scenes, continuous ordinary walking, title/menu/pause/loading
screens, cursor movement, or inventory browsing with no decision or result.
Use the English enum values shown in the schema, but write summary, objects, actions, visible_text,
scene_changes, player_state values, notable_events, and suggested_help in concise Japanese."""


def video_instruction(profile: str | None) -> str | None:
    """Return a focused analysis contract for a supported game profile."""
    from neuro_voice.games.profiles import GameProfileRegistry

    selected = GameProfileRegistry().get(profile)
    if selected is not None:
        if not selected.allows_vision:
            return None
        if selected.id == "minecraft":
            return _MINECRAFT_PROMPT
        return selected.analysis_instruction or None
    return _MINECRAFT_PROMPT if str(profile or "").strip().lower() == "minecraft" else None


def is_explicit_vision_query(text: str) -> bool:
    normalized = (text or "").replace(" ", "")
    terms = (
        "何が見える", "なにが見える", "見えてる", "映ってる", "持ってる",
        "画面見て", "画面を見て", "画像見て", "画像を見て", "写真見て", "写真を見て",
        "カメラ見て", "カメラを見て", "スクリーンショット", "これ何", "これなに",
        "画面見てる", "画面を見てる", "画面見えてる", "画面を見えてる",
        "今見てる", "いま見てる",
        "文字を読ん", "文字読ん", "何て書いて", "なんて書いて", "書いてある",
        "読める", "もう一回見て", "もう一度見て", "見直して", "撮り直して",
        "前の画面", "古い画面",
    )
    return any(term in normalized for term in terms)


def is_direct_vision_turn(
    text: str, *, mode: str = "on_demand", auto_prompt: str = "",
) -> bool:
    """Whether this dialogue turn is allowed to carry a raw image.

    ``vision.enabled`` expresses capability, not consent to add a costly image
    prefill to every turn. Raw media is attached only for an explicit visual
    request or for the exact internal prompt used by auto commentary.
    """
    value = (text or "").strip()
    if is_explicit_vision_query(value):
        return True
    return bool(
        str(mode).strip().lower() == "auto"
        and auto_prompt.strip()
        and value == auto_prompt.strip()
    )


def is_realtime_game_query(text: str) -> bool:
    """Questions/statements that should use the cached live game state."""
    normalized = (text or "").replace(" ", "")
    if is_explicit_vision_query(normalized):
        return True
    terms = (
        "今どう", "今何", "今どこ", "今の状況", "どうしたら", "どうすれば",
        "どうしよう", "助けて", "危ない",
        "襲われ", "囲まれ", "追われ", "敵", "ゾンビ", "クリーパー", "スケルトン",
        "体力", "空腹", "高いところ", "落ちそう", "死にそう", "画面", "状況判断",
        "さっき", "何が起き", "何で死ん", "どうして死ん", "見つからない", "採掘",
    )
    return any(term in normalized for term in terms)


def compact_reactive_visual_messages(messages: list[dict]) -> list[dict]:
    """Retain persona, live state, game facts and only the last exchange."""
    if not messages:
        return messages
    systems = [item for item in messages if item.get("role") == "system"]
    dialogue = [item for item in messages if item.get("role") != "system"]
    persona = str((systems[0] if systems else {}).get("content", ""))[:1200]
    keywords = (
        "OBS", "ゲーム映像", "Minecraft", "攻略", "現在状態", "継続認識",
        "直近の流れ", "会話方針",
    )
    relevant_parts: list[str] = []
    remaining = 1500
    for item in systems[1:]:
        content = str(item.get("content", ""))
        if not any(word in content for word in keywords):
            continue
        piece = content[:remaining]
        if piece:
            relevant_parts.append(piece)
            remaining -= len(piece)
        if remaining <= 0:
            break
    compact: list[dict] = [{
        "role": "system",
        "content": (
            persona
            + "\n\n【リアルタイム画像応答】添付画像は今のOBSゲーム画面。"
              "画像を根拠に、会話中の人格を保って自然に答える。"
              "固定の攻略文を読み上げず、見える状況に必要な部分だけ言い換える。"
              "返答は原則1〜3文。"
        ),
    }]
    if relevant_parts:
        compact.append({"role": "system", "content": "\n\n".join(relevant_parts)})
    for item in dialogue[-3:]:
        role = str(item.get("role", "user"))
        limit = 700 if role == "user" and item is dialogue[-1] else 350
        compact.append({"role": role, "content": str(item.get("content", ""))[-limit:]})
    logger.info(
        "Reactive visual prompt compacted: messages=%d chars=%d",
        len(compact), sum(len(str(item.get("content", ""))) for item in compact),
    )
    return compact


class VisualAnalyzer:
    def __init__(
        self, llm, *, timeout_s: float = 45.0,
        max_output_tokens: int = 160, context_size: int = 4096,
    ):
        self._llm = llm
        self._timeout_s = max(1.0, float(timeout_s))
        self._max_output_tokens = max(64, min(512, int(max_output_tokens)))
        self._context_size = max(2048, int(context_size))

    async def analyze(
        self, frames: list[MediaInput], *, request_id: str, temporal: bool = False,
        instruction: str | None = None,
    ) -> VisualObservation:
        if not frames:
            raise ValueError("at least one visual frame is required")
        prompt = instruction or (_VIDEO_PROMPT if temporal or len(frames) > 1 else _IMAGE_PROMPT)
        started = time.perf_counter()
        request_started = time.perf_counter()
        first_token_at: float | None = None
        chunks: list[str] = []

        async def _collect() -> None:
            if hasattr(self._llm, "chat"):
                try:
                    stream = self._llm.chat(
                        [{"role": "user", "content": prompt}], frames,
                        stream=True, request_id=request_id,
                        options={
                            "num_predict": self._max_output_tokens,
                            "num_ctx": self._context_size,
                            "temperature": 0.1,
                        },
                        think=False,
                    )
                except TypeError:
                    # Non-Ollama test/custom adapters may expose the older
                    # minimal signature.
                    stream = self._llm.chat(
                        [{"role": "user", "content": prompt}], frames,
                        stream=True, request_id=request_id,
                    )
            else:
                stream = self._llm.generate_with_media(
                    [{"role": "user", "content": prompt}], frames, request_id=request_id,
                )
            async for token in stream:
                nonlocal first_token_at
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                chunks.append(token)

        await asyncio.wait_for(_collect(), timeout=self._timeout_s)
        parse_started = time.perf_counter()
        observation = VisualObservation.from_model_text(
            "".join(chunks), captured_at=frames[-1].timestamp,
        )
        finished = time.perf_counter()
        observation.source_name = str(
            frames[-1].metadata.get("obs_source_name")
            or frames[-1].metadata.get("source") or "unknown"
        )
        observation.frame_hash = str(frames[-1].metadata.get("fingerprint", ""))
        observation.processing_time_ms = round((finished - started) * 1000)
        observation.analyzed_at = time.time()
        observation_metrics = {
            "vision_request_ms": round((finished - request_started) * 1000),
            "vision_inference_ms": round(
                ((finished - first_token_at) if first_token_at is not None else (finished - request_started)) * 1000
            ),
            "vision_first_token_ms": (
                None if first_token_at is None else round((first_token_at - request_started) * 1000)
            ),
            "vision_parse_ms": round((finished - parse_started) * 1000),
        }
        observation.metrics = observation_metrics
        logger.info(
            "[Vision] request=%dms first_token=%s inference=%dms parse=%dms total=%dms summary=%s",
            observation_metrics["vision_request_ms"],
            observation_metrics["vision_first_token_ms"],
            observation_metrics["vision_inference_ms"],
            observation_metrics["vision_parse_ms"],
            observation.processing_time_ms,
            observation.summary[:80],
        )
        return observation

    async def close(self) -> None:
        unload = getattr(self._llm, "unload", None)
        if callable(unload):
            await unload()
            return
        close = getattr(self._llm, "aclose", None)
        if callable(close):
            await close()


class PerceptionService:
    """Keeps visual data out of chat history and injects only useful context."""

    def __init__(
        self, analyzer: VisualAnalyzer, *, relevance_floor: str = "medium",
        memory_seconds: float = 30.0,
    ):
        self._analyzer = analyzer
        self.memory = PerceptionMemory(memory_seconds=max(5.0, float(memory_seconds)))
        self._relevance_floor = relevance_floor
        self._active_task: asyncio.Task | None = None
        self._last_fingerprint: str | None = None
        self._gate = InferencePriorityGate()
        self.last_metrics: dict[str, float | int | None] = {}
        self.last_was_cache_hit = False

    def cancel_background(self) -> None:
        self._gate.cancel_background()
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()

    async def analyze(
        self, frames: list[MediaInput], *, request_id: str, temporal: bool = False,
        background: bool = False, instruction: str | None = None,
    ) -> VisualObservation:
        fingerprint = str(frames[-1].metadata.get("fingerprint", "")) if frames else ""
        if (
            background and self.memory.current_scene is not None
            and hash_distance(self._last_fingerprint, fingerprint) < .02
        ):
            self.last_was_cache_hit = True
            self.last_metrics = {"cache_hit": 1, "visual_memory_update_ms": 0}
            return self.memory.current_scene
        self.last_was_cache_hit = False
        if self._active_task is not None and not self._active_task.done():
            # Only one visual inference may occupy the model. A newer frame is
            # more useful than stale background work; explicit queries also
            # preempt background work rather than queue behind it.
            self._active_task.cancel()
        priority = InferencePriority.BACKGROUND_VISION if background else InferencePriority.EXPLICIT_VISION_QUERY
        effective_instruction = instruction
        previous = self.memory.current_scene
        if background and previous is not None and instruction:
            previous_age = max(0.0, time.time() - float(previous.captured_at or time.time()))
            effective_instruction = (
                instruction
                + "\n\nPrevious structured observation (reference only; it may now be stale):\n"
                + previous.prompt_context()
                + f"\nIt came from a frame {previous_age:.1f} seconds before this request. "
                  "Compare it with the new image to populate scene_changes/notable_events. "
                  "Do not assume continuity when the new image does not support it."
            )
        task = asyncio.create_task(
            self._gate.run(priority, lambda: self._analyzer.analyze(
                frames, request_id=request_id, temporal=temporal, instruction=effective_instruction,
            ))
        )
        self._active_task = task
        try:
            observation = await task
            memory_started = time.perf_counter()
            self.memory.record(observation)
            memory_ms = round((time.perf_counter() - memory_started) * 1000)
            self.last_metrics = {
                **getattr(observation, "metrics", {}),
                "visual_memory_update_ms": memory_ms,
            }
            logger.info("[Vision] memory_update=%dms", memory_ms)
            self._last_fingerprint = fingerprint or self._last_fingerprint
            return observation
        finally:
            if self._active_task is task:
                self._active_task = None

    def context_for(self, user_text: str) -> str | None:
        observation = self.memory.current_scene
        if observation is None:
            return None
        explicit = is_explicit_vision_query(user_text)
        relevant = observation.conversation_relevance in {"medium", "high"}
        changed = bool(observation.scene_changes)
        if not (explicit or relevant or changed):
            return None
        if not explicit and self.memory.last_injected_at and time.time() - self.memory.last_injected_at < 20:
            return None
        self.memory.last_injected_at = time.time()
        return "現在の視覚情報:\n" + observation.prompt_context() + "\n必要な場合だけ会話へ利用し、毎回言及しない。"

    async def close(self) -> None:
        self.cancel_background()
        await self._analyzer.close()
