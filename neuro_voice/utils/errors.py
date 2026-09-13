"""Turning an exception into something safe to show a person.

A failed HTTP call carries the whole request URL in its message.  For a TTS
backend that puts the utterance in the query string, that means the person's
own sentence comes back percent-encoded and fills the chat window with
``%E3%81%99%E3%81%8E...`` instead of telling them what went wrong.
"""
from __future__ import annotations

import re

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2}){3,}")
_WHITESPACE = re.compile(r"\s+")


def safe_error_text(error: BaseException | str, *, limit: int = 160) -> str:
    """A short, URL-free description suitable for the UI.

    The full detail still reaches the log; this is only what the person sees.
    """
    if isinstance(error, BaseException):
        text = str(error) or error.__class__.__name__
        label = error.__class__.__name__
    else:
        text, label = str(error or ""), ""
    text = _URL.sub("<URL>", text)
    text = _PERCENT_RUN.sub("…", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text or text == "<URL>":
        return label or "不明なエラー"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def safe_exception_summary(error: BaseException) -> str:
    """Return only exception type and an attached HTTP status, never payloads."""
    label = type(error).__name__
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    if 100 <= code <= 599:
        return f"{label} (HTTP {code})"
    return label
