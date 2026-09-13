"""Track generated speech against the PCM that was actually rendered."""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


def response_is_active(response_id: str, active_response_id: str | None,
                       cancelled_response_ids: set[str]) -> bool:
    """Single cancellation gate shared by LLM/TTS/playback stages."""
    return response_id == active_response_id and response_id not in cancelled_response_ids


def response_can_play(response_id: str, cancelled_response_ids: set[str]) -> bool:
    """Queued PCM survives generation completion; only cancellation rejects it."""
    return response_id not in cancelled_response_ids


@dataclass
class SpeechChunk:
    """A cancellable unit of synthesized speech."""

    chunk_id: str
    text: str
    segment_index: int = 0
    audio: object | None = None
    sample_rate: int = 0
    played_frames: int = 0
    total_frames: int = 0
    completed: bool = False

    @property
    def played_chars(self) -> int:
        if self.total_frames <= 0:
            return len(self.text) if self.completed else 0
        ratio = max(0.0, min(1.0, self.played_frames / self.total_frames))
        return round(len(self.text) * ratio)


@dataclass
class PlayedTextTracker:
    """Keeps response state separate from conversation history.

    ``generated_text`` may be ahead of TTS.  Chunk identities let a late TTS
    completion be rejected by the owning response before it reaches playback.
    """

    response_id: str = field(default_factory=lambda: uuid4().hex)
    #: One response owns one logical playback session, even when TTS streams
    #: it as several PCM chunks.
    playback_session_id: str = field(default_factory=lambda: uuid4().hex)
    generated_text: str = ""
    synthesized_text: str = ""
    played_text: str = ""
    interrupted_at_character: int | None = None
    interruption_reason: str | None = None
    _chunks: dict[str, SpeechChunk] = field(default_factory=dict, repr=False)
    _chunk_order: list[str] = field(default_factory=list, repr=False)
    _playing_id: str | None = field(default=None, repr=False)
    _recorded_chars: dict[str, int] = field(default_factory=dict, repr=False)

    def generated(self, text: str) -> None:
        self.generated_text += text

    def synthesized(self, text: str) -> None:
        """Compatibility hook for older callers without explicit chunks."""
        self.synthesized_text += text

    def queue_chunk(
        self, text: str, *, audio: object | None = None, sample_rate: int = 0,
        total_frames: int = 0, chunk_id: str | None = None,
        segment_index: int | None = None,
    ) -> SpeechChunk:
        chunk = SpeechChunk(
            chunk_id=chunk_id or uuid4().hex,
            text=text,
            segment_index=(len(self._chunk_order) if segment_index is None else int(segment_index)),
            audio=audio,
            sample_rate=sample_rate,
            total_frames=max(0, int(total_frames)),
        )
        self._chunks[chunk.chunk_id] = chunk
        self._chunk_order.append(chunk.chunk_id)
        self.synthesized_text += text
        return chunk

    def started(self, chunk_id: str) -> None:
        if chunk_id in self._chunks:
            self._playing_id = chunk_id

    def completed(self, chunk_id: str, played_frames: int, total_frames: int,
                  completed: bool) -> None:
        chunk = self._chunks.get(chunk_id)
        if chunk is None:
            return
        chunk.total_frames = max(chunk.total_frames, int(total_frames))
        chunk.played_frames = max(chunk.played_frames, min(int(played_frames), chunk.total_frames))
        chunk.completed = bool(completed and chunk.played_frames >= chunk.total_frames)
        self._record_chunk_progress(chunk)
        if self._playing_id == chunk_id:
            self._playing_id = None

    def played(self, text: str, ratio: float = 1.0) -> None:
        """Compatibility hook used by existing tests and non-chunk callers."""
        chars = max(0, min(len(text), round(len(text) * max(0.0, min(1.0, ratio)))))
        self.played_text += text[:chars]
        if chars < len(text):
            self.interrupted_at_character = len(self.played_text)

    def interrupted(self, reason: str) -> None:
        self.interruption_reason = reason
        if self.interrupted_at_character is None:
            self.interrupted_at_character = len(self.played_text)

    @property
    def queued_text(self) -> str:
        return "".join(
            self._chunks[chunk_id].text
            for chunk_id in self._chunk_order
            if chunk_id != self._playing_id and not self._chunks[chunk_id].completed
        )

    @property
    def playing_text(self) -> str:
        chunk = self._chunks.get(self._playing_id or "")
        return chunk.text if chunk is not None else ""

    @property
    def unplayed_text(self) -> str:
        # Generation order and playback order are the same.  This also keeps
        # an LLM's not-yet-segmented tail available after a cancellation.
        return self.generated_text[len(self.played_text):]

    def snapshot(self) -> dict[str, str | int | None]:
        return {
            "response_id": self.response_id,
            "generated_text": self.generated_text,
            "synthesized_text": self.synthesized_text,
            "queued_text": self.queued_text,
            "playing_text": self.playing_text,
            "played_text": self.played_text,
            "unplayed_text": self.unplayed_text,
            "interrupted_at_character": self.interrupted_at_character,
            "interruption_reason": self.interruption_reason,
        }

    def _record_chunk_progress(self, chunk: SpeechChunk) -> None:
        chars = chunk.played_chars
        previous = self._recorded_chars.get(chunk.chunk_id, 0)
        if chars > previous:
            self.played_text += chunk.text[previous:chars]
            self._recorded_chars[chunk.chunk_id] = chars
        if chars < len(chunk.text):
            self.interrupted_at_character = len(self.played_text)
