"""Unit tests for `earthlens.cli.validate`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.cli import validate as validate_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.validate import (
    ValidateResult,
    _live_ecmwf,
    _validate_nwp,
    _validate_tropycal,
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
    "jrc_flood",
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


class TestValidateNwp:
    """Tests for the nwp structural lint."""

    def test_clean_catalog_has_no_issues(self):
        """The bundled nwp catalog passes its own structural lint."""
        checked, issues = _validate_nwp(load_nwp())
        assert checked > 0 and issues == [], f"unexpected nwp issues: {issues}"

    def test_flags_missing_url_template(self):
        """A direct-https model with no url_template is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="direct-https",
                    url_template="",
                    bands={"t": 1},
                    cycles_utc=[0],
                    model_family="x",
                )
            }
        )
        checked, issues = _validate_nwp(catalog)
        assert checked == 1 and any("url_template" in i for i in issues)

    def test_flags_empty_bands_and_bad_cycle(self):
        """An empty band map and an out-of-range cycle hour are flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="herbie",
                    model_family="m",
                    url_template=None,
                    bands={},
                    cycles_utc=[0, 99],
                )
            }
        )
        _checked, issues = _validate_nwp(catalog)
        assert any("empty band map" in i for i in issues), "empty bands flagged"
        assert any("out of range" in i for i in issues), "bad cycle flagged"


class TestStructuralLints:
    """Negative cases for the structural validators."""

    def test_tropycal_unknown_basin_and_bad_source_flagged(self):
        """A non-SDK basin and an unsupported (basin, source) pair are flagged."""
        catalog = SimpleNamespace(
            datasets={
                "north_atlantic": SimpleNamespace(sources=["jtwc"]),
                "mars_basin": SimpleNamespace(sources=["ibtracs"]),
            }
        )
        _checked, issues = _validate_tropycal(catalog)
        assert any("mars_basin" in i and "not in" in i for i in issues), "bad basin"
        assert any("jtwc" in i for i in issues), "unsupported source flagged"


class TestValidateOne:
    """Tests for validate_one."""

    def test_unsupported_provider(self):
        """A catalog-backed provider (uses refresh/audit) has no validator."""
        assert validate_one(_info("cmems")).status == "unsupported"

    def test_nwp_ok(self):
        """nwp validates clean end-to-end."""
        result = validate_one(_info("nwp"))
        assert result.status == "ok" and result.issues == []
        assert result.checked > 0, "models were checked"


class TestLiveValidators:
    """Tests for the `--live` reachability validators (network mocked)."""

    def test_supported_providers_live_adds_openeo(self):
        """openeo only appears in the supported set under live."""
        assert "openeo" not in supported_providers()
        assert "openeo" in supported_providers(live=True)

    def test_nwp_live_flags_non_200_cycle(self, monkeypatch):
        """A direct-https model whose latest cycle does not HEAD 200 is flagged."""
        monkeypatch.setattr(validate_mod, "_http_head", lambda url: 404)
        result = validate_one(_info("nwp"), live=True)
        assert result.status == "ok" and result.issues, "404 cycle -> issue"

    def test_nwp_live_clean_at_200(self, monkeypatch):
        """All direct-https latest cycles HEADing 200 clear the nwp live check."""
        monkeypatch.setattr(validate_mod, "_http_head", lambda url: 200)
        result = validate_one(_info("nwp"), live=True)
        assert result.issues == [], "all 200 -> clean"

    def test_ecmwf_live_flags_invalid_request(self, monkeypatch):
        """An ECMWF dataset whose minimal request fails the validator is flagged."""
        import earthlens.ecmwf.constraints as constraints

        catalog = SimpleNamespace(
            datasets={"good": object(), "nocon": object(), "bad": object()},
            minimal_valid_request=lambda key: {
                "good": {"data_format": "netcdf", "variable": ["x"]},
                "nocon": {"data_format": "netcdf"},
                "bad": {"data_format": "netcdf", "variable": ["y"]},
            }[key],
        )

        class FakeValidator:
            def __init__(self, dataset, request):
                self.dataset = dataset

            def check(self):
                if self.dataset == "bad":
                    raise ValueError("missing required selector 'level'")

        monkeypatch.setattr(constraints, "RequestValidator", FakeValidator)
        checked, issues = _live_ecmwf(catalog)
        assert checked == 2, "the no-constraints dataset is skipped"
        assert any("bad" in i for i in issues), "invalid request flagged"

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


def load_nwp():
    """Load the real nwp catalog (helper for the lint test)."""
    from earthlens.cli.adapter import load_catalog

    return load_catalog(_info("nwp"))


class TestOfflineValidatorBranches:
    """Negative-path coverage for the offline structural validators."""

    def test_nwp_herbie_missing_model_family(self):
        """A herbie model with no model_family is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="herbie",
                    model_family="",
                    url_template=None,
                    bands={"t": 1},
                    cycles_utc=[0],
                )
            }
        )
        _checked, issues = _validate_nwp(catalog)
        assert any("model_family" in i for i in issues), "herbie family flagged"


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

    def test_live_nwp_skips_non_direct_https(self, monkeypatch):
        """Non-direct-https models are skipped by the nwp live check."""
        from earthlens.cli.validate import _live_nwp

        catalog = SimpleNamespace(
            datasets={"h": SimpleNamespace(backend="herbie", cycles_utc=[0])}
        )
        checked, issues = _live_nwp(catalog)
        assert checked == 0 and issues == [], "herbie model skipped"

    def test_live_ecmwf_reports_fetch_failure(self):
        """A dataset whose constraints fetch raises is reported, not raised."""
        from earthlens.cli.validate import _live_ecmwf

        def boom(key):
            raise RuntimeError("offline")

        catalog = SimpleNamespace(datasets={"d": object()}, minimal_valid_request=boom)
        checked, issues = _live_ecmwf(catalog)
        assert any("constraints fetch failed" in i for i in issues), "failure reported"

    def test_nwp_latest_cycle_none_without_cycles(self):
        """_nwp_latest_cycle returns None for a model with no cycle hours."""
        from earthlens.cli.validate import _nwp_latest_cycle

        assert _nwp_latest_cycle(SimpleNamespace(cycles_utc=[])) is None

    def test_live_nwp_skips_model_without_url(self):
        """A direct-https model with no url_template/bands is skipped, not flagged."""
        from earthlens.cli.validate import _live_nwp

        catalog = SimpleNamespace(
            datasets={
                "x": SimpleNamespace(
                    backend="direct-https", cycles_utc=[0], url_template="", bands={}
                )
            }
        )
        checked, issues = _live_nwp(catalog)
        assert checked == 0 and issues == [], "incomplete model skipped"
