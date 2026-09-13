"""No module may reference a name that does not exist.

`python -m compileall` was the only static check in use, and it does not catch
this: undefined names are a runtime error, so a line inside a rarely-taken
branch can sit broken for days.

Two happened within one session, both invisible until someone exercised the
path by hand:

* `_respond_from_text` passed `transcript=transcript`, a name that exists only
  in the *spoken* path.  Every message typed into the chat box raised
  `NameError` and produced no reply at all.
* `keep_alive` was added to the request body as `self._keep_alive`, an
  attribute that was only ever stored under another name.  Every reply failed.

Both were written into code that no test executed.  Pyflakes reads the code
rather than running it, so it finds them without a server, a microphone or an
event loop.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "neuro_voice"


def pyflakes_lines() -> list[str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(PACKAGE)],
            capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError:                       # pragma: no cover
        pytest.skip("pyflakes が入っていません")
    if "No module named" in (result.stderr or ""):
        pytest.skip("pyflakes が入っていません")
    return [line for line in (result.stdout or "").splitlines() if line.strip()]


def test_no_undefined_names():
    """The check that would have caught both of this session's outages."""
    offences = [line for line in pyflakes_lines() if "undefined name" in line]
    assert not offences, "存在しない名前を参照しています:\n" + "\n".join(offences)


def test_no_redefinition_of_unused_names():
    """A second definition means the first one never ran — usually a mistake."""
    offences = [
        line for line in pyflakes_lines()
        if "redefinition of unused" in line
    ]
    assert not offences, "同じ名前を二重に定義しています:\n" + "\n".join(offences)


def test_no_syntax_errors_in_any_module():
    import compileall

    assert compileall.compile_dir(
        str(PACKAGE), quiet=2, force=True, legacy=True,
    ), "構文エラーがあります"
