"""Dependency checking must not import heavy packages to answer a question.

``find_spec("discord.ext.voice_recv")`` imports ``discord`` and ``discord.ext``
just to locate a submodule, and that import costs seconds before the window is
drawn.  Installed-distribution metadata answers the same question without it.
"""
from __future__ import annotations

import sys

import pytest

from neuro_voice import bootstrap


def test_the_heavy_submodules_are_answered_from_metadata():
    assert "discord" in bootstrap._DISTRIBUTION_FOR
    assert "discord.ext.voice_recv" in bootstrap._DISTRIBUTION_FOR


def test_checking_discord_does_not_import_it(monkeypatch):
    """The whole point: answering must leave the module unimported."""
    for name in [item for item in sys.modules if item.startswith("discord")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    bootstrap._distribution_installed("discord.py")
    bootstrap.discord_py_version()

    assert not [item for item in sys.modules if item.startswith("discord")]


def test_a_missing_distribution_reads_as_not_installed(monkeypatch):
    import importlib.metadata as metadata

    def _missing(_name):
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", _missing)
    assert bootstrap._distribution_installed("definitely-not-installed") is False
    assert bootstrap.discord_py_version() == ""


def test_an_installed_distribution_reads_as_present(monkeypatch):
    import importlib.metadata as metadata

    monkeypatch.setattr(metadata, "version", lambda _name: "2.7.1")
    assert bootstrap._distribution_installed("anything") is True
    assert bootstrap.discord_py_version() == "2.7.1"


def test_a_broken_environment_does_not_block_startup(monkeypatch):
    """A warning is not worth refusing to start over."""
    import importlib.metadata as metadata

    def _explode(_name):
        raise OSError("metadata unreadable")

    monkeypatch.setattr(metadata, "version", _explode)
    assert bootstrap._distribution_installed("anything") is True
    assert bootstrap.discord_py_version() == ""


@pytest.mark.parametrize("version,warns", [
    ("2.7.1", False), ("2.8.0", False), ("2.6.4", True), ("", False),
])
def test_only_an_incompatible_version_warns(monkeypatch, capsys, version, warns):
    monkeypatch.setattr(bootstrap, "discord_py_version", lambda: version)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    bootstrap._check_discord_compat()
    printed = capsys.readouterr().out
    assert ("DAVE" in printed) is warns
