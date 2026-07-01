"""Unit tests for per-endpoint CADS client routing (`earthlens.ecmwf.endpoints`).

Covers URL/key resolution for cds / ads / ewds, the shared-PAT fallback, the
missing-credential error, and the backend's per-endpoint client cache. No
network: `cdsapi.Client` is always faked.
"""

from __future__ import annotations

from pathlib import Path

import cdsapi
import pytest

from earthlens.ecmwf import ECMWF, AuthenticationError
from earthlens.ecmwf import endpoints as ep

pytestmark = [pytest.mark.unit]

_ENV_VARS = ("CDSAPI_URL", "CDSAPI_KEY", "EWDS_URL", "EWDS_KEY", "ADS_URL", "ADS_KEY")


class _RecordingClient:
    """Fake `cdsapi.Client` that records the url/key it was built with."""

    def __init__(self, url=None, key=None):
        self.url = url
        self.key = key


def _clear_cads_env(monkeypatch):
    """Remove every CADS env var so a test starts from a known-empty state."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _home_without_dotfile(monkeypatch, tmp_path):
    """Point `Path.home()` at an empty dir so `~/.cdsapirc` is absent."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _write_cdsapirc(tmp_path, key):
    """Create a `~/.cdsapirc` under a faked home carrying `key`."""
    (tmp_path / ".cdsapirc").write_text(
        f"url: https://cds.climate.copernicus.eu/api\nkey: {key}\n"
    )


class TestOpenClient:
    """Tests for `endpoints.open_client`."""

    def test_cds_returns_bare_client(self, monkeypatch):
        """CDS builds a bare `cdsapi.Client()` (cdsapi reads its own config)."""
        _clear_cads_env(monkeypatch)
        sentinel = object()
        monkeypatch.setattr(cdsapi, "Client", lambda *a, **k: sentinel)
        assert ep.open_client("cds") is sentinel

    def test_default_endpoint_is_cds(self, monkeypatch):
        """No argument routes to CDS."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setattr(cdsapi, "Client", lambda *a, **k: "cds-client")
        assert ep.open_client() == "cds-client"

    def test_ewds_uses_endpoint_url_and_key(self, monkeypatch):
        """EWDS builds `Client(url=ewds, key=EWDS_KEY)` from its own env key."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("EWDS_KEY", "ewds-token")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        client = ep.open_client("ewds")
        assert client.url == "https://ewds.climate.copernicus.eu/api"
        assert client.key == "ewds-token"

    def test_ewds_falls_back_to_cdsapi_key_env(self, monkeypatch):
        """With no EWDS_KEY, EWDS reuses the shared `CDSAPI_KEY`."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("CDSAPI_KEY", "shared-pat")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        assert ep.open_client("ewds").key == "shared-pat"

    def test_ewds_falls_back_to_cdsapirc_key(self, monkeypatch, tmp_path):
        """With no env keys, EWDS reads the shared token from `~/.cdsapirc`."""
        _clear_cads_env(monkeypatch)
        _write_cdsapirc(tmp_path, "dotfile-pat")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        assert ep.open_client("ewds").key == "dotfile-pat"

    def test_ewds_url_env_override(self, monkeypatch):
        """`EWDS_URL` overrides the default endpoint URL."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("EWDS_KEY", "t")
        monkeypatch.setenv("EWDS_URL", "https://staging.ewds.invalid/api")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        assert ep.open_client("ewds").url == "https://staging.ewds.invalid/api"

    def test_missing_key_raises_authentication_error(self, monkeypatch, tmp_path):
        """No endpoint key and no shared PAT raises `AuthenticationError`."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        with pytest.raises(AuthenticationError) as excinfo:
            ep.open_client("ewds")
        message = str(excinfo.value)
        assert "EWDS_KEY" in message
        assert "ewds.climate.copernicus.eu/profile" in message

    def test_unknown_endpoint_raises_value_error(self, monkeypatch):
        """An unknown endpoint slug raises `ValueError` naming the valid ones."""
        _clear_cads_env(monkeypatch)
        with pytest.raises(ValueError, match="unknown ECMWF endpoint"):
            ep.open_client("mars")

    def test_endpoints_registry_has_the_three_cads_instances(self):
        """The endpoint registry enumerates cds, ads, and ewds."""
        assert set(ep.ENDPOINTS) == {"cds", "ads", "ewds"}

    def test_read_cdsapirc_key_none_when_no_key_line(self, monkeypatch, tmp_path):
        """A `~/.cdsapirc` without a `key:` line resolves to no token."""
        (tmp_path / ".cdsapirc").write_text("url: https://cds.climate.copernicus.eu/api\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert ep._read_cdsapirc_key() is None


class TestClientFor:
    """Tests for `ECMWF._client_for` (per-endpoint cache + injection)."""

    def test_injected_client_wins_for_every_endpoint(self):
        """An injected client is returned regardless of the requested endpoint."""
        ecmwf = ECMWF.__new__(ECMWF)
        ecmwf._clients = {}
        injected = object()
        ecmwf.client = injected
        assert ecmwf._client_for("cds") is injected
        assert ecmwf._client_for("ewds") is injected

    def test_open_client_propagates_endpoint_auth_error(self, monkeypatch, tmp_path):
        """`_open_client` re-raises an EWDS AuthenticationError unwrapped."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        ecmwf = ECMWF.__new__(ECMWF)
        with pytest.raises(AuthenticationError, match="EWDS_KEY"):
            ecmwf._open_client("ewds")

    def test_caches_one_client_per_endpoint(self, monkeypatch):
        """A client is built once per endpoint and reused on repeat calls."""
        ecmwf = ECMWF.__new__(ECMWF)
        ecmwf._clients = {}
        built: list[str] = []
        monkeypatch.setattr(
            ECMWF, "_open_client", lambda self, endpoint: built.append(endpoint) or object()
        )
        first = ecmwf._client_for("ewds")
        second = ecmwf._client_for("ewds")
        third = ecmwf._client_for("cds")
        assert first is second
        assert third is not first
        assert built == ["ewds", "cds"]
