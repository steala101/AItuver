"""System/source/error chips remain usable when they contain long diagnostics."""
from __future__ import annotations

import re
from pathlib import Path


def _ui_html() -> str:
    return (
        Path(__file__).parents[1] / "neuro_voice" / "ui" / "assets" / "index.html"
    ).read_text(encoding="utf-8")


def _css_rule(selector: str) -> str:
    match = re.search(
        rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}",
        _ui_html(), flags=re.DOTALL,
    )
    assert match is not None, f"missing CSS rule for {selector}"
    return match.group("body")


def test_system_messages_are_selectable_and_wrap_inside_chat_width():
    rule = _css_rule(".sys")

    assert re.search(r"user-select\s*:\s*text\b", rule)
    assert re.search(r"white-space\s*:\s*pre-wrap\b", rule)
    assert re.search(r"overflow-wrap\s*:\s*anywhere\b", rule)
    assert re.search(r"max-width\s*:\s*(?:min\([^;]+\)|\d+%)", rule)


def test_system_messages_keep_textcontent_for_source_and_error_text():
    html = _ui_html()
    add_sys = re.search(
        r"function addSys\(text, isError\) \{(?P<body>.*?)^\}",
        html, flags=re.MULTILINE | re.DOTALL,
    )
    assert add_sys is not None
    assert "el.textContent = text" in add_sys.group("body")
    assert "innerHTML" not in add_sys.group("body")
    assert 'addSys("🔎 " + r.title + "\\n" + r.url)' in html
