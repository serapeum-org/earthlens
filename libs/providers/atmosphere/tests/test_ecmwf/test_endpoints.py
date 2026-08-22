"""Unit tests for per-endpoint CADS client routing (`earthlens.ecmwf.endpoints`).

Covers URL/key resolution for cds / ads / ewds / ecds / xds, the shared-PAT
fallback, the missing-credential error, and the backend's per-endpoint client
cache. No network: `cdsapi.Client` is always faked.
"""

from __future__ import annotations

from pathlib import Path

import cdsapi
import pytest

from earthlens.ecmwf import ECMWF, AuthenticationError, Variable
from earthlens.ecmwf import endpoints as ep

pytestmark = [pytest.mark.unit]

_ENV_VARS = (
    "CDSAPI_URL",
    "CDSAPI_KEY",
    "EWDS_URL",
    "EWDS_KEY",
    "ADS_URL",
    "ADS_KEY",
    "ECDS_URL",
    "ECDS_KEY",
    "XDS_URL",
    "XDS_KEY",
)


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

    def test_ads_uses_endpoint_url_and_key(self, monkeypatch):
        """ADS builds `Client(url=ads, key=ADS_KEY)` from its own env key."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("ADS_KEY", "ads-token")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        client = ep.open_client("ads")
        assert client.url == "https://ads.atmosphere.copernicus.eu/api"
        assert client.key == "ads-token"

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

    def test_endpoints_registry_has_the_five_cads_instances(self):
        """The endpoint registry enumerates cds, ads, ewds, ecds and xds."""
        assert set(ep.ENDPOINTS) == {"cds", "ads", "ewds", "ecds", "xds"}

    def test_ecds_uses_endpoint_url_and_key(self, monkeypatch):
        """ECDS builds `Client(url=ecds, key=ECDS_KEY)` from its own env key."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("ECDS_KEY", "ecds-token")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        client = ep.open_client("ecds")
        assert client.url == "https://ecds.ecmwf.int/api"
        assert client.key == "ecds-token"

    def test_xds_uses_endpoint_url_and_key(self, monkeypatch):
        """XDS builds `Client(url=xds, key=XDS_KEY)` from its own env key."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("XDS_KEY", "xds-token")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        client = ep.open_client("xds")
        assert client.url == "https://xds.ecmwf.int/api"
        assert client.key == "xds-token"

    @pytest.mark.parametrize("endpoint", ["ecds", "xds"])
    def test_ecmwf_stores_fall_back_to_shared_pat(self, monkeypatch, endpoint):
        """With no store-specific key, the shared CDS token authenticates."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("CDSAPI_KEY", "shared-pat")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        assert ep.open_client(endpoint).key == "shared-pat"

    @pytest.mark.parametrize("endpoint", ["ecds", "xds"])
    def test_ecmwf_stores_fall_back_to_cdsapirc_key(
        self, monkeypatch, tmp_path, endpoint
    ):
        """With no env keys, the store reads the shared token from `~/.cdsapirc`."""
        _clear_cads_env(monkeypatch)
        _write_cdsapirc(tmp_path, "dotfile-pat")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        assert ep.open_client(endpoint).key == "dotfile-pat"

    def test_ecds_url_env_override(self, monkeypatch):
        """`ECDS_URL` overrides the built-in ECDS URL."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("ECDS_KEY", "ecds-token")
        monkeypatch.setenv("ECDS_URL", "https://staging.ecds.invalid/api")
        monkeypatch.setattr(cdsapi, "Client", _RecordingClient)
        assert ep.open_client("ecds").url == "https://staging.ecds.invalid/api"

    @pytest.mark.parametrize(
        "endpoint, host",
        [("ecds", "ecds.ecmwf.int"), ("xds", "xds.ecmwf.int")],
    )
    def test_missing_key_error_names_the_store_profile(
        self, monkeypatch, tmp_path, endpoint, host
    ):
        """The missing-credential error points at the right store's profile page."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        with pytest.raises(AuthenticationError) as excinfo:
            ep.open_client(endpoint)
        assert f"{host}/profile" in str(excinfo.value)

    def test_read_cdsapirc_key_none_when_no_key_line(self, monkeypatch, tmp_path):
        """A `~/.cdsapirc` without a `key:` line resolves to no token."""
        (tmp_path / ".cdsapirc").write_text(
            "url: https://cds.climate.copernicus.eu/api\n"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert ep._read_cdsapirc_key() is None


class TestConstraintsHost:
    """Constraints/URL resolution per endpoint."""

    @pytest.mark.parametrize(
        "endpoint, expected",
        [
            ("ecds", "https://ecds.ecmwf.int/api"),
            ("xds", "https://xds.ecmwf.int/api"),
        ],
    )
    def test_constraints_base_url_is_the_store_host(
        self, monkeypatch, endpoint, expected
    ):
        """A non-CDS store validates against its own constraints host."""
        _clear_cads_env(monkeypatch)
        assert ep.constraints_base_url(endpoint) == expected

    def test_cds_constraints_base_url_stays_none(self, monkeypatch):
        """CDS keeps its historic `None` so the default template is used."""
        _clear_cads_env(monkeypatch)
        assert ep.constraints_base_url("cds") is None

    def test_endpoint_url_rejects_an_unknown_slug(self, monkeypatch):
        """`endpoint_url` names the valid slugs when given an unknown one."""
        _clear_cads_env(monkeypatch)
        with pytest.raises(ValueError, match="unknown ECMWF endpoint") as excinfo:
            ep.endpoint_url("mars")
        assert "ecds" in str(excinfo.value)

    def test_constraints_host_follows_url_override(self, monkeypatch):
        """Pointing `XDS_URL` at a staging host moves the constraints host too."""
        _clear_cads_env(monkeypatch)
        monkeypatch.setenv("XDS_URL", "https://staging.xds.invalid/api")
        assert ep.constraints_base_url("xds") == "https://staging.xds.invalid/api"

    @pytest.mark.parametrize(
        "endpoint, dataset, cds_variable",
        [
            ("ecds", "tigge-forecasts", "2_m_temperature"),
            ("xds", "derived-fire-fuel-biomass", "live_fuel_moisture_content_group"),
        ],
    )
    def test_catalog_accepts_the_new_endpoint_slugs(
        self, endpoint, dataset, cds_variable
    ):
        """A catalog row may declare the new stores without a validation error."""
        variable = Variable(
            cds_dataset=dataset,
            cds_variable=cds_variable,
            nc_variable="t2m",
            units="K",
            endpoint=endpoint,
        )
        assert variable.endpoint == endpoint


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

    def test_reading_client_does_not_poison_endpoint_routing(self, monkeypatch):
        """Reading `self.client` (cds) must not make `_client_for('ewds')` return it."""
        ecmwf = ECMWF.__new__(ECMWF)
        ecmwf._clients = {}
        ecmwf._injected_client = None
        monkeypatch.setattr(
            ECMWF, "_open_client", lambda self, endpoint: f"client-{endpoint}"
        )
        assert ecmwf.client == "client-cds"  # lazily builds + caches the CDS client
        assert ecmwf._client_for("ewds") == "client-ewds"  # not the CDS client

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
            ECMWF,
            "_open_client",
            lambda self, endpoint: built.append(endpoint) or object(),
        )
        first = ecmwf._client_for("ewds")
        second = ecmwf._client_for("ewds")
        third = ecmwf._client_for("cds")
        assert first is second
        assert third is not first
        assert built == ["ewds", "cds"]


class _RecordingModernClient:
    """Fake `ecmwf.datastores.Client` that records its url/key."""

    def __init__(self, url=None, key=None):
        self.url = url
        self.key = key


def _install_fake_datastores(monkeypatch):
    """Inject a fake `ecmwf.datastores` module exposing `Client`."""
    import sys
    import types

    module = types.ModuleType("ecmwf.datastores")
    module.Client = _RecordingModernClient
    monkeypatch.setitem(sys.modules, "ecmwf.datastores", module)


class TestModernClient:
    """Tests for the opt-in `ecmwf-datastores-client` path."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("", False),
            ("0", False),
            ("no", False),
        ],
    )
    def test_use_modern_flag(self, monkeypatch, value, expected):
        """`EARTHLENS_ECMWF_MODERN` truthiness is parsed case-insensitively."""
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", value)
        assert ep._use_modern_client() is expected

    def test_open_modern_client_resolves_url_and_key(self, monkeypatch, tmp_path):
        """`open_modern_client` builds the modern client with the endpoint url + token."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        monkeypatch.setenv("EWDS_KEY", "tok-123")
        _install_fake_datastores(monkeypatch)
        client = ep.open_modern_client("ewds")
        assert isinstance(client, _RecordingModernClient)
        assert client.url == "https://ewds.climate.copernicus.eu/api"
        assert client.key == "tok-123"

    def test_open_client_delegates_when_flag_set(self, monkeypatch, tmp_path):
        """With the flag set, `open_client` returns the modern client."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        monkeypatch.setenv("CDSAPI_KEY", "pat")
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", "1")
        _install_fake_datastores(monkeypatch)
        assert isinstance(ep.open_client("ads"), _RecordingModernClient)

    def test_missing_extra_raises_importerror(self, monkeypatch, tmp_path):
        """A missing `ecmwf-datastores-client` raises a clear ImportError."""
        import sys

        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        monkeypatch.setenv("EWDS_KEY", "tok")
        monkeypatch.setitem(sys.modules, "ecmwf.datastores", None)
        with pytest.raises(ImportError, match="ecmwf-modern"):
            ep.open_modern_client("ewds")

    def test_default_path_unchanged_without_flag(self, monkeypatch):
        """Without the flag, `open_client('cds')` is the historic bare client."""
        _clear_cads_env(monkeypatch)
        monkeypatch.delenv("EARTHLENS_ECMWF_MODERN", raising=False)
        sentinel = object()
        monkeypatch.setattr(cdsapi, "Client", lambda *a, **k: sentinel)
        assert ep.open_client("cds") is sentinel

    def test_open_modern_client_missing_token_raises_auth_error(
        self, monkeypatch, tmp_path
    ):
        """No resolvable token raises `AuthenticationError` (modern path)."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        _install_fake_datastores(monkeypatch)
        with pytest.raises(AuthenticationError):
            ep.open_modern_client("ewds")

    def test_open_modern_client_unknown_endpoint_raises_value_error(self, monkeypatch):
        """An unknown endpoint slug raises `ValueError` before any import."""
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", "1")
        with pytest.raises(ValueError, match="unknown ECMWF endpoint"):
            ep.open_modern_client("mars")

    def test_open_client_cds_flag_set_no_token_raises(self, monkeypatch, tmp_path):
        """With the flag set, `open_client('cds')` resolves eagerly and raises."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", "1")
        _install_fake_datastores(monkeypatch)
        with pytest.raises(AuthenticationError):
            ep.open_client("cds")

    def test_open_client_delegation_threads_url_and_key(self, monkeypatch, tmp_path):
        """`open_client` with the flag set threads the endpoint url+key to the modern client."""
        _clear_cads_env(monkeypatch)
        _home_without_dotfile(monkeypatch, tmp_path)
        monkeypatch.setenv("ADS_KEY", "ads-tok")
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", "on")
        _install_fake_datastores(monkeypatch)
        client = ep.open_client("ads")
        assert isinstance(client, _RecordingModernClient)
        assert client.url == "https://ads.atmosphere.copernicus.eu/api"
        assert client.key == "ads-tok"

    def test_use_modern_flag_strips_whitespace(self, monkeypatch):
        """Surrounding whitespace around the flag value is ignored."""
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", " 1 ")
        assert ep._use_modern_client() is True

    def test_open_client_unknown_endpoint_with_flag_raises_value_error(
        self, monkeypatch
    ):
        """An unknown endpoint raises `ValueError` through `open_client` on the modern path."""
        monkeypatch.setenv("EARTHLENS_ECMWF_MODERN", "1")
        with pytest.raises(ValueError, match="unknown ECMWF endpoint"):
            ep.open_client("mars")
