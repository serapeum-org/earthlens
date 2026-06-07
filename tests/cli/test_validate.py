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
    _validate_overture,
    _validate_radar,
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
    "s3",
    "ghsl",
    "overture",
    "fdsn",
    "firms",
    "radar",
    "tropycal",
    "gdacs",
    "chc",
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

    def test_overture_default_type_must_be_in_types(self):
        """A default_type not among types is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "x": SimpleNamespace(types=["a", "b"], default_type="c"),
            }
        )
        _checked, issues = _validate_overture(catalog)
        assert any("default_type" in i for i in issues), "mismatch flagged"

    def test_radar_out_of_range_coords_flagged(self):
        """A station with an impossible latitude is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "KXXX": SimpleNamespace(name="X", latitude=999.0, longitude=0.0),
            }
        )
        _checked, issues = _validate_radar(catalog)
        assert any("latitude" in i for i in issues), "bad latitude flagged"

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


class TestOfflineExtensions:
    """Tests for the usgs_water + sentinel_hub offline validators."""

    def test_usgs_water_validates_clean(self):
        """Every curated USGS parameter's services are known service names."""
        result = validate_one(_info("usgs_water"))
        assert result.status == "ok" and result.issues == []
        assert result.checked > 0, "parameters were checked"

    def test_sentinel_hub_validates_clean(self):
        """Every curated Sentinel Hub recipe's evalscript is well-formed."""
        result = validate_one(_info("sentinel_hub"))
        assert result.status == "ok" and result.issues == []
        assert result.checked > 0, "recipes were checked"

    def test_worldpop_validates_clean(self):
        """The worldpop structural lint (via Catalog.health) passes the bundle."""
        result = validate_one(_info("worldpop"))
        assert result.status == "ok" and result.issues == []
        assert result.checked > 0, "products were checked"


class TestLiveValidators:
    """Tests for the `--live` reachability validators (network mocked)."""

    def test_s3_live_flags_empty_bucket(self, monkeypatch):
        """An S3 dataset whose bucket serves no object is flagged live."""
        monkeypatch.setattr(validate_mod, "_s3_live_keys", lambda b, p, r: [])
        result = validate_one(_info("s3"), live=True)
        assert result.status == "ok" and result.issues, "empty bucket -> issue"

    def test_s3_live_clean_when_objects_present(self, monkeypatch):
        """A reachable object clears the s3 live check."""
        monkeypatch.setattr(validate_mod, "_s3_live_keys", lambda b, p, r: ["k"])
        result = validate_one(_info("s3"), live=True)
        assert result.issues == [], "objects present -> clean"

    def test_overture_live_flags_missing_sources(self, monkeypatch):
        """An Overture type without a sources column is flagged live."""
        monkeypatch.setattr(validate_mod, "_overture_live_sample", lambda t: (0, False))
        result = validate_one(_info("overture"), live=True)
        assert any("sources" in i for i in result.issues), "missing sources flagged"

    def test_ghsl_live_flags_non_200(self, monkeypatch):
        """A GHSL artefact that does not HEAD 200 is flagged live."""
        monkeypatch.setattr(validate_mod, "_http_head", lambda url: 404)
        result = validate_one(_info("ghsl"), live=True)
        assert result.status == "ok" and result.issues, "404 -> issue"

    def test_ghsl_live_clean_at_200(self, monkeypatch):
        """All artefacts HEADing 200 clear the ghsl live check."""
        monkeypatch.setattr(validate_mod, "_http_head", lambda url: 200)
        result = validate_one(_info("ghsl"), live=True)
        assert result.issues == [], "all 200 -> clean"

    def test_openeo_is_live_only(self, monkeypatch):
        """openeo has no offline validator; --live checks recipes vs live."""
        assert validate_one(_info("openeo")).status == "unsupported"
        monkeypatch.setattr(validate_mod, "_openeo_live_lists", lambda: (set(), set()))
        result = validate_one(_info("openeo"), live=True)
        assert result.status == "ok", "live openeo validator ran"

    def test_supported_providers_live_adds_openeo(self):
        """openeo only appears in the supported set under live."""
        assert "openeo" not in supported_providers()
        assert "openeo" in supported_providers(live=True)

    def test_radar_live_flags_empty_feed(self, monkeypatch):
        """An unreachable / empty NEXRAD chunk feed is flagged live."""
        monkeypatch.setattr(validate_mod, "_radar_feed_stations", lambda: set())
        result = validate_one(_info("radar"), live=True)
        assert result.status == "ok" and result.issues, "empty feed -> issue"

    def test_radar_live_clean_when_streaming(self, monkeypatch):
        """A feed containing a catalogued station clears the radar live check."""
        catalog = next(b for b in list_backends() if b.provider == "radar")
        from earthlens.cli.adapter import load_catalog

        station = next(iter(load_catalog(catalog).datasets))
        monkeypatch.setattr(
            validate_mod, "_radar_feed_stations", lambda: {station, "KZZZ"}
        )
        result = validate_one(_info("radar"), live=True)
        assert result.issues == [], "streaming station -> clean"

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
