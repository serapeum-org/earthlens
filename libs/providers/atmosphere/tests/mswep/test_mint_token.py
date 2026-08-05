"""Unit tests for the operator script `tools/mswep/mint_token.py`.

The script is not part of the shipped `earthlens.mswep` package (it lives under
`tools/`), so it is loaded here by path. Only its pure logic is exercised — path
validation, client-secret shape checks, and the file-writing half of the mint
flow with the browser step mocked out.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.mswep, pytest.mark.unit]


def _load_mint_token() -> ModuleType:
    """Import the standalone `tools/mswep/mint_token.py` script by file path.

    Returns:
        ModuleType: The loaded module.

    Raises:
        FileNotFoundError: When the script cannot be found above this file.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tools" / "mswep" / "mint_token.py"
        if candidate.exists():
            spec = importlib.util.spec_from_file_location("mswep_mint_token", candidate)
            module = importlib.util.module_from_spec(spec)
            assert spec is not None and spec.loader is not None
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError("tools/mswep/mint_token.py not found above the test file")


mint_token = _load_mint_token()


def _write(path: Path, payload) -> Path:
    """Write a JSON dict (or raw string) to `path` and return it."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


class TestValidatedPath:
    """Tests for `validated_path`."""

    def test_resolves_existing_json(self, tmp_path):
        """An existing .json regular file resolves to its canonical path."""
        path = _write(tmp_path / "secret.json", {"installed": {}})
        result = mint_token.validated_path(path, must_exist=True)
        assert result == path.resolve(), f"expected {path.resolve()}, got {result}"

    def test_output_need_not_exist(self, tmp_path):
        """A non-existent .json is accepted when must_exist is False."""
        path = tmp_path / "token.json"
        assert mint_token.validated_path(path, must_exist=False) == path.resolve()

    def test_wrong_suffix_is_rejected(self, tmp_path):
        """A non-.json path exits with a message naming the required suffix."""
        with pytest.raises(SystemExit, match="must be a .json file"):
            mint_token.validated_path(tmp_path / "secret.txt", must_exist=False)

    def test_missing_required_file_is_rejected(self, tmp_path):
        """A must_exist path that is absent exits with 'does not exist'."""
        with pytest.raises(SystemExit, match="does not exist"):
            mint_token.validated_path(tmp_path / "nope.json", must_exist=True)

    def test_directory_is_not_a_regular_file(self, tmp_path):
        """A directory named like a .json file is rejected as not a regular file."""
        directory = tmp_path / "dir.json"
        directory.mkdir()
        with pytest.raises(SystemExit, match="not a regular file"):
            mint_token.validated_path(directory, must_exist=False)


class TestCheckNotServiceAccount:
    """Tests for `check_not_service_account`."""

    def test_service_account_key_is_rejected(self, tmp_path):
        """A service-account key exits, pointing the user at MSWEP_TOKEN_FILE."""
        path = _write(
            tmp_path / "sa.json",
            {"type": "service_account", "client_email": "bot@proj.iam"},
        )
        with pytest.raises(SystemExit, match="service-account key"):
            mint_token.check_not_service_account(path)

    def test_non_oauth_shape_is_rejected(self, tmp_path):
        """A JSON with neither 'installed' nor 'web' exits."""
        path = _write(tmp_path / "weird.json", {"nope": 1})
        with pytest.raises(SystemExit, match="does not look like an OAuth client"):
            mint_token.check_not_service_account(path)

    def test_unreadable_json_is_reported(self, tmp_path):
        """Malformed JSON exits with a read error rather than a traceback."""
        path = _write(tmp_path / "bad.json", "{ not json")
        with pytest.raises(SystemExit, match="could not be read as JSON"):
            mint_token.check_not_service_account(path)

    def test_installed_oauth_client_passes(self, tmp_path):
        """A Desktop-app ('installed') client ID is accepted silently."""
        path = _write(tmp_path / "ok.json", {"installed": {"client_id": "x"}})
        assert mint_token.check_not_service_account(path) is None


class _FakeCredentials:
    """A stand-in for the google credential returned by the consent flow."""

    def to_json(self):
        """Return a minimal authorized-user JSON carrying a refresh token."""
        return json.dumps({"refresh_token": "rt"})


class _FakeFlow:
    """A stand-in for `InstalledAppFlow` that never opens a browser."""

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        """Return a flow instance, ignoring the (unused-in-test) inputs."""
        return cls()

    def run_local_server(self, **kwargs):
        """Return a fake credential instead of running a local server."""
        return _FakeCredentials()


class TestMint:
    """Tests for `mint` (browser step mocked)."""

    def test_missing_oauthlib_exits(self, tmp_path, monkeypatch):
        """Without google-auth-oauthlib, mint exits telling the user to install it."""
        monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", None)
        with pytest.raises(SystemExit, match="google-auth-oauthlib"):
            mint_token.mint(tmp_path / "s.json", tmp_path / "token.json")

    def test_writes_credentials_json(self, tmp_path, monkeypatch):
        """A completed flow writes the credential JSON, creating parent dirs."""
        flow_mod = ModuleType("google_auth_oauthlib.flow")
        flow_mod.InstalledAppFlow = _FakeFlow
        monkeypatch.setitem(
            sys.modules, "google_auth_oauthlib", ModuleType("google_auth_oauthlib")
        )
        monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_mod)
        out = tmp_path / "sub" / "token.json"
        written = mint_token.mint(tmp_path / "s.json", out)
        assert written == out, f"expected {out}, got {written}"
        assert json.loads(out.read_text(encoding="utf-8"))["refresh_token"] == "rt"


class TestMain:
    """Tests for `main`."""

    def test_happy_path_writes_and_reports(self, tmp_path, monkeypatch, capsys):
        """main validates, mints, verifies the refresh token, and reports success."""
        secret = _write(tmp_path / "client.json", {"installed": {"client_id": "x"}})
        out = _write(tmp_path / "token.json", {"refresh_token": "rt"})
        monkeypatch.setattr(mint_token, "mint", lambda cs, o: out)
        code = mint_token.main([str(secret), "-o", str(out)])
        assert code == 0, f"expected exit 0, got {code}"
        assert "wrote" in capsys.readouterr().out

    def test_missing_refresh_token_exits(self, tmp_path, monkeypatch):
        """A minted file with no refresh token exits with remediation guidance."""
        secret = _write(tmp_path / "client.json", {"installed": {"client_id": "x"}})
        out = _write(tmp_path / "token.json", {"token": "at"})
        monkeypatch.setattr(mint_token, "mint", lambda cs, o: out)
        with pytest.raises(SystemExit, match="no refresh token"):
            mint_token.main([str(secret), "-o", str(out)])
