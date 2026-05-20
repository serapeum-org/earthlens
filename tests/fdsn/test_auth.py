"""Unit tests for `earthlens.fdsn.auth` (EarthScope token resolution)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.fdsn import auth
from earthlens.fdsn.auth import resolve_earthscope_token


@pytest.mark.fdsn
class TestResolveEarthscopeToken:
    """`resolve_earthscope_token` precedence: arg > env > file > None."""

    def test_explicit_argument_wins(self, monkeypatch: pytest.MonkeyPatch):
        """An explicit token is returned even when env/file are set."""
        monkeypatch.setenv("EARTHSCOPE_TOKEN", "from-env")
        assert resolve_earthscope_token("explicit") == "explicit"

    def test_env_var_used(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """The env var is used when no argument is given."""
        monkeypatch.setenv("EARTHSCOPE_TOKEN", "from-env")
        monkeypatch.setattr(auth, "EARTHSCOPE_TOKEN_FILE", tmp_path / "absent")
        assert resolve_earthscope_token() == "from-env"

    def test_file_used_when_no_arg_or_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """The token file is read when neither argument nor env is set."""
        monkeypatch.delenv("EARTHSCOPE_TOKEN", raising=False)
        token_file = tmp_path / ".earthscope_token"
        token_file.write_text("\nfile-token\n", encoding="utf-8")
        monkeypatch.setattr(auth, "EARTHSCOPE_TOKEN_FILE", token_file)
        assert resolve_earthscope_token() == "file-token"

    def test_none_when_no_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        """With no argument, env, or file the result is None."""
        monkeypatch.delenv("EARTHSCOPE_TOKEN", raising=False)
        monkeypatch.setattr(auth, "EARTHSCOPE_TOKEN_FILE", tmp_path / "absent")
        assert resolve_earthscope_token() is None

    def test_empty_argument_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """An empty-string argument is treated as 'no token supplied'."""
        monkeypatch.setenv("EARTHSCOPE_TOKEN", "from-env")
        assert resolve_earthscope_token("") == "from-env"
