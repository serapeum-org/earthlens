"""Unit tests for `earthlens.cli.validate`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.cli import validate as validate_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.validate import (
    ValidateResult,
    supported_providers,
    validate_one,
)

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


_CURATED_ENUM = (
    "nwp",
    "nwm",
    "mswep",
    "s3",
    "ghsl",
    "overture",
    "osm",
    "fdsn",
    "firms",
    "asf",
    "radar",
    "radklim",
    "goes",
    "tropycal",
    "gdacs",
    "hanze",
    "flopros",
    "catrare",
    "flodis",
    "drought",
    "argo",
    "chc",
    "erddap",
    "gbif",
    "obis",
    "wdpa",
    "iucn",
    "bathymetry",
    "fabdem",
    "jrc",
    "pvgis",
    "glaciers",
    "soilgrids",
)


class TestSupportedProviders:
    """Tests for supported_providers."""

    def test_all_curated_enumeration_providers_supported(self):
        """Every curated-enumeration provider has a validator wired up."""
        assert set(_CURATED_ENUM) <= set(supported_providers())


class TestBundledCatalogsLintClean:
    """The shipped catalogs should pass their own structural lint."""

    @pytest.mark.parametrize("provider", _CURATED_ENUM)
    def test_provider_validates_clean(self, provider):
        """Each bundled curated-enumeration catalog has no validation issues.

        Args:
            provider: The curated-enumeration provider to validate.
        """
        result = validate_one(_info(provider))
        assert result.status == "ok", f"{provider} validator errored: {result.detail}"
        assert result.issues == [], f"{provider} issues: {result.issues}"
        assert result.checked > 0, f"{provider} checked nothing"


class TestValidateOne:
    """Tests for validate_one."""

    def test_unsupported_provider(self):
        """A catalog-backed provider (uses refresh/audit) has no validator."""
        assert validate_one(_info("cmems")).status == "unsupported"


class TestLiveValidators:
    """Tests for the `--live` reachability validators (network mocked)."""

    def test_supported_providers_live_adds_openeo(self):
        """openeo only appears in the supported set under live."""
        assert "openeo" not in supported_providers()
        assert "openeo" in supported_providers(live=True)

    def test_supported_providers_live_adds_ecmwf_and_nwp(self):
        """ecmwf gains a live-only validator; nwp gains a live one on top."""
        assert "ecmwf" in supported_providers(live=True)
        assert "nwp" in supported_providers(live=True)


class TestValidateResult:
    """Tests for ValidateResult."""

    def test_to_dict(self):
        """to_dict carries checked + issues."""
        data = ValidateResult("nwp", "ok", checked=3, issues=["x"]).to_dict()
        assert data["checked"] == 3 and data["issues"] == ["x"]


class TestLivePrimitives:
    """Cover the thin live-reachability primitive helpers (SDK mocked)."""

    def test_http_head_returns_status(self, monkeypatch):
        """_http_head returns the HEAD response status code."""
        from earthlens.cli.validate import _http_head

        monkeypatch.setattr(
            validate_mod.requests,
            "head",
            lambda url, timeout=None, allow_redirects=None: SimpleNamespace(
                status_code=204
            ),
        )
        assert _http_head("https://x") == 204, "status code returned"


class TestLiveValidatorBranches:
    """Branch coverage for the live validators using fake catalogs."""

    def test_live_ghsl_reports_url_error(self, monkeypatch):
        """A ghsl_url failure is reported as an issue rather than raised."""
        import earthlens.ghsl._helpers as helpers

        def boom(*a, **kw):
            raise RuntimeError("bad url")

        monkeypatch.setattr(helpers, "ghsl_url", boom)
        result = validate_one(_info("ghsl"), live=True)
        assert result.status == "ok", "errors captured, not raised"
