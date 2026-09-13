"""LLMトークン列の文分割と <think> ブロック除去。"""
from __future__ import annotations

import re
from typing import AsyncIterator

_HARD_ENDERS = "。！？!?…"
_SOFT_ENDERS = "、"
_TRAILERS = "」』）)\"'"


class SentenceSegmenter:
    """ストリーミングテキストを文単位に区切り、TTSへ渡せる形にする。"""

    def __init__(self, max_chars: int = 120, min_chars: int = 1,
                 hard_max_chars: int | None = None,
                 comma_min_chars: int = 20):
        self._buf = ""
        self._max = max(1, max_chars)
        self._min = max(1, min(min_chars, self._max))
        # A Japanese comma is a useful low-latency split point only after a
        # phrase has enough context.  Treating every comma as a sentence end
        # made SBV2 close its intonation on fragments such as
        # 「爆弾処理のゲームって、」 and restart prosody on the next fragment.
        # Full stops remain immediate boundaries; commas are deliberately
        # softer so interruption tracking stays granular without choppy TTS.
        self._comma_min = max(self._min, min(int(comma_min_chars), self._max))
        # ``max_chars`` is a preferred TTS size, not permission to cut a
        # Japanese phrase in the middle.  Wait for punctuation up to this
        # hard ceiling before using a fallback split.
        self._hard_max = max(self._max, int(hard_max_chars or self._max * 2))

    def feed(self, text: str) -> list[str]:
        """トークンを追加し、確定した文のリストを返す。"""
        self._buf += text
        out: list[str] = []
        while True:
            idx = self._find_boundary()
            if idx is None:
                if len(self._buf) >= self._hard_max:
                    cut = self._natural_cut()
                    out.append(self._buf[:cut].strip())
                    self._buf = self._buf[cut:]
                break
            sent = self._buf[: idx + 1]
            rest = self._buf[idx + 1 :]
            while rest and rest[0] in _TRAILERS:
                sent += rest[0]
                rest = rest[1:]
            self._buf = rest
            sent = sent.strip()
            if sent:
                out.append(sent)
        return out

    def flush(self) -> str | None:
        """残りのバッファを文として返す。"""
        sent = self._buf.strip()
        self._buf = ""
        return sent or None

    def _find_boundary(self) -> int | None:
        for i, ch in enumerate(self._buf):
            length = i + 1
            if (ch in _HARD_ENDERS or ch == "\n") and length >= self._min:
                return i
            if ch in _SOFT_ENDERS and length >= self._comma_min:
                return i
        return None

    def _natural_cut(self) -> int:
        """Choose a Japanese connective before falling back to a hard cut."""
        limit = self._hard_max
        candidates = []
        for connective in ("けれど", "けど", "から", "ので", "のに", "そして", "でも", "ただ"):
            pos = self._buf.rfind(connective, self._min, limit)
            if pos >= self._min:
                candidates.append(pos + len(connective))
        return max(candidates, default=limit)


_META_REASONING_RE = re.compile(
    r"(?:the\s+user\s+(?:is\s+asking|asked)|core\s+points?\s+to\s+address|"
    r"drafting\s+response|refined\s+response|final\s+(?:answer|response|version|polish)|"
    r"the\s+prompt\s+says|one\s+more\s+check|i\s+need\s+to|"
    r"let['’]s\s+(?:go|keep|make)|wait\s*[,.:]|english\s+(?:translation|version)\s*:)",
    re.IGNORECASE,
)
_FINAL_MARKER_RE = re.compile(
    r"(?:final\s+(?:answer|response|version|polish(?:\s*\(japanese\))?)|"
    r"refined\s+response|drafting\s+response)\s*[:：]",
    re.IGNORECASE,
)
_JAPANESE_RE = re.compile(r"[ぁ-んァ-ヶ一-龠々ー]")
_META_REASONING_PREFIXES = (
    "the user is asking", "the user asked", "core point", "drafting response",
    "refined response", "final answer", "final response", "final version",
    "final polish", "the prompt says", "one more check", "i need to",
    "let's go", "let's keep", "let's make", "wait,", "wait:", "wait.",
    "english translation:", "english version:",
)
_MIXED_ENGLISH_REPLACEMENTS = {
    "actually": "実際",
    "basically": "要するに",
    "definitely": "確かに",
    "exactly": "まさに",
    "honestly": "正直",
    "literally": "文字どおり",
    "maybe": "たぶん",
    "probably": "おそらく",
    "really": "本当に",
}
_MIXED_ENGLISH_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(
        re.escape(word) for word in sorted(
            _MIXED_ENGLISH_REPLACEMENTS, key=len, reverse=True,
        )
    ) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_TRAILING_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*$")


class SpokenOutputFilter:
    """Keep raw model self-talk out of UI, history and TTS.

    Normal Japanese starts streaming after a tiny prefix.  If a model begins
    narrating its drafting process, output is held until the stream finishes
    and only the last Japanese final-response candidate is released.
    """

    def __init__(self, max_final_chars: int = 420):
        self._initial = ""
        self._normal = ""
        self._reasoning = ""
        self._surface_tail = ""
        self._surface_has_japanese = False
        self._mode = "undecided"
        self._max_final = max(80, int(max_final_chars))

    def feed(self, text: str) -> str:
        if not text:
            return ""
        if self._mode == "reasoning":
            self._reasoning += text
            return ""
        if self._mode == "undecided":
            self._initial += text
            if _META_REASONING_RE.search(self._initial):
                self._mode = "reasoning"
                self._reasoning = self._initial
                self._initial = ""
                return ""
            japanese = len(_JAPANESE_RE.findall(self._initial))
            first_japanese = _JAPANESE_RE.search(self._initial)
            if (
                len(self._initial) >= 96
                and self._looks_like_english_self_talk(self._initial)
                and (first_japanese is None or first_japanese.start() >= 72)
            ):
                self._mode = "reasoning"
                self._reasoning = self._initial
                self._initial = ""
                return ""
            if japanese < 6:
                if len(self._initial) < 96 and "\n" not in self._initial:
                    return ""
                if self._looks_like_english_self_talk(self._initial):
                    self._mode = "reasoning"
                    self._reasoning = self._initial
                    self._initial = ""
                    return ""
            self._mode = "normal"
            self._normal = self._initial
            self._initial = ""
            return self._drain_normal()
        self._normal += text
        return self._drain_normal()

    def _drain_normal(self) -> str:
        marker = _META_REASONING_RE.search(self._normal)
        if marker is not None:
            spoken = self._normal[:marker.start()]
            self._reasoning = self._normal[marker.start():]
            self._normal = ""
            self._mode = "reasoning"
            return self._drain_surface(spoken)
        # Hold only a suffix that could be the start of a meta-reasoning
        # marker split across streamed tokens.  A fixed tail delayed every
        # Japanese response, while this keeps the normal path effectively
        # zero-copy and still catches e.g. ``Wa`` + ``it, ...``.
        hold = self._partial_marker_start(self._normal)
        if hold is None:
            out, self._normal = self._normal, ""
            return self._drain_surface(out)
        out = self._normal[:hold]
        self._normal = self._normal[hold:]
        return self._drain_surface(out)

    def _drain_surface(self, text: str, *, final: bool = False) -> str:
        """Translate accidental English fillers without delaying Japanese.

        Local models occasionally drop one English drafting word into an
        otherwise Japanese answer (for example ``definitely``).  Hold only a
        trailing Latin word across token boundaries, then normalize a small,
        explicit list. Product names and technical terms are untouched.
        """
        self._surface_tail += text
        if final:
            ready, self._surface_tail = self._surface_tail, ""
        else:
            trailing = _TRAILING_LATIN_WORD.search(self._surface_tail)
            if trailing is None:
                ready, self._surface_tail = self._surface_tail, ""
            else:
                ready = self._surface_tail[:trailing.start()]
                self._surface_tail = self._surface_tail[trailing.start():]
        has_japanese = bool(_JAPANESE_RE.search(ready))
        japanese_context = self._surface_has_japanese or has_japanese
        self._surface_has_japanese = japanese_context
        if not ready or not japanese_context:
            return ready
        return _MIXED_ENGLISH_RE.sub(
            lambda match: _MIXED_ENGLISH_REPLACEMENTS[match.group(1).lower()],
            ready,
        )

    @staticmethod
    def _partial_marker_start(text: str) -> int | None:
        lowered = text.lower().replace("’", "'")
        max_len = max(len(value) for value in _META_REASONING_PREFIXES)
        start = max(0, len(lowered) - max_len)
        for index in range(start, len(lowered)):
            if index and lowered[index - 1].isalnum():
                continue
            suffix = lowered[index:]
            if any(value.startswith(suffix) for value in _META_REASONING_PREFIXES):
                return index
        return None

    @staticmethod
    def _looks_like_english_self_talk(text: str) -> bool:
        """Treat a long English opening as internal drafting in Japanese mode."""
        latin = len(re.findall(r"[A-Za-z]", text))
        words = len(re.findall(r"\b[A-Za-z]{2,}\b", text))
        return latin >= 56 and words >= 10

    def flush(self) -> str:
        if self._mode == "undecided":
            text, self._initial = self._initial, ""
            if _META_REASONING_RE.search(text):
                text = self._extract_final(text)
            return self._drain_surface(text, final=True)
        if self._mode == "normal":
            text, self._normal = self._normal, ""
            marker = _META_REASONING_RE.search(text)
            text = text[:marker.start()] if marker else text
            return self._drain_surface(text, final=True)
        text, self._reasoning = self._reasoning, ""
        return self._drain_surface(self._extract_final(text), final=True)

    def _extract_final(self, raw: str) -> str:
        candidates: list[str] = []
        markers = list(_FINAL_MARKER_RE.finditer(raw))
        for marker in reversed(markers):
            candidates.append(raw[marker.end():])
        candidates.extend(reversed(re.split(_META_REASONING_RE, raw)))
        for candidate in candidates:
            # A later self-check (usually "Wait, ...") is not part of the
            # Japanese answer that preceded it.
            later_meta = _META_REASONING_RE.search(candidate)
            if later_meta is not None:
                candidate = candidate[:later_meta.start()]
            candidate = candidate.strip(" \t\r\n:-—")
            if len(_JAPANESE_RE.findall(candidate)) < 6:
                continue
            first_japanese = _JAPANESE_RE.search(candidate)
            if first_japanese is not None and first_japanese.start() > 0:
                prefix = candidate[:first_japanese.start()]
                if len(re.findall(r"[A-Za-z]", prefix)) >= 4:
                    candidate = candidate[first_japanese.start():]
            candidate = self._limit(candidate)
            if candidate:
                return candidate
        return ""

    def _limit(self, text: str) -> str:
        if len(text) <= self._max_final:
            return text
        window = text[:self._max_final]
        cut = max(window.rfind(mark) for mark in "。！？")
        return window[:cut + 1] if cut >= 40 else window


async def _strip_tagged_think(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Remove explicit <think> blocks while preserving split-tag safety."""
    in_think = False
    buf = ""
    async for token in stream:
        buf += token
        while True:
            if in_think:
                end = buf.find("</think>")
                if end < 0:
                    buf = buf[-8:]  # タグ分割対策に末尾のみ保持
                    break
                buf = buf[end + 8 :]
                in_think = False
                continue
            start = buf.find("<think>")
            if start >= 0:
                if buf[:start]:
                    yield buf[:start]
                buf = buf[start + 7 :]
                in_think = True
                continue
            keep = 6  # "<think" が分割されて届く場合に備える
            if len(buf) > keep:
                yield buf[:-keep]
                buf = buf[-keep:]
            break
    if not in_think and buf:
        yield buf


async def strip_think(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Remove tagged and untagged internal reasoning from a spoken stream."""
    spoken = SpokenOutputFilter()
    async for token in _strip_tagged_think(stream):
        output = spoken.feed(token)
        if output:
            yield output
    tail = spoken.flush()
    if tail:
        yield tail
