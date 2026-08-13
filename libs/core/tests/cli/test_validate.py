"""Unit tests for `earthlens.cli.validate`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.cli import validate as validate_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.validate import (
    ValidateResult,
    _live_ecmwf,
    _validate_drought,
    _validate_goes,
    _validate_nrel,
    _validate_nwp,
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


class TestValidateDrought:
    """Tests for the drought structural lint."""

    def test_clean_rows_pass(self):
        """A well-formed edo-wcs raster row and a usdm vector row report nothing."""
        catalog = SimpleNamespace(
            datasets={
                "edo-spaST": SimpleNamespace(
                    source="EDO",
                    endpoint="https://x/wcs",
                    output_kind="raster",
                    cadence="10day",
                    native_crs="EPSG:4326",
                    transport="edo-wcs",
                    coverage="spaST",
                    timescale="01",
                ),
                "usdm": SimpleNamespace(
                    source="USDM",
                    endpoint="https://x/{ymd}.json",
                    output_kind="vector",
                    cadence="weekly",
                    native_crs="EPSG:4326",
                    transport="usdm-geojson",
                    coverage=None,
                    timescale=None,
                ),
            }
        )
        checked, issues = _validate_drought(catalog)
        assert checked == 2
        assert issues == []

    def test_flags_edo_wcs_row_missing_coverage_and_timescale(self):
        """An edo-wcs row without a coverage or timescale is flagged for each."""
        catalog = SimpleNamespace(
            datasets={
                "edo-bad": SimpleNamespace(
                    source="EDO",
                    endpoint="https://x/wcs",
                    output_kind="raster",
                    cadence="10day",
                    native_crs="EPSG:4326",
                    transport="edo-wcs",
                    coverage=None,
                    timescale=None,
                )
            }
        )
        checked, issues = _validate_drought(catalog)
        assert checked == 1
        assert any("missing coverage" in i for i in issues)
        assert any("missing timescale" in i for i in issues)

    def test_flags_transport_output_kind_mismatch(self):
        """A usdm-geojson row declared raster (or edo-wcs declared vector) is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "usdm": SimpleNamespace(
                    source="USDM",
                    endpoint="https://x",
                    output_kind="raster",
                    cadence="weekly",
                    native_crs="EPSG:4326",
                    transport="usdm-geojson",
                    coverage=None,
                    timescale=None,
                )
            }
        )
        _checked, issues = _validate_drought(catalog)
        assert any("must be output_kind=vector" in i for i in issues)


class TestValidateNrel:
    """Tests for the nrel structural lint."""

    def test_good_rows_pass(self):
        """A row with source, endpoint, and columns reports no issues."""
        catalog = SimpleNamespace(
            datasets={
                "nsrdb-psm3": SimpleNamespace(
                    source="nsrdb", endpoint="/api/x.csv", columns=["time", "GHI"]
                )
            }
        )
        checked, issues = _validate_nrel(catalog)
        assert checked == 1
        assert issues == []

    def test_flags_missing_source_and_columns(self):
        """A row missing its source and columns is flagged for each."""
        catalog = SimpleNamespace(
            datasets={"bad": SimpleNamespace(source="", endpoint="/x.csv", columns=[])}
        )
        _checked, issues = _validate_nrel(catalog)
        assert any("missing source" in i for i in issues)
        assert any("missing columns" in i for i in issues)


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

    def test_radar_out_of_range_coords_flagged(self):
        """A station with an impossible latitude is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "KXXX": SimpleNamespace(name="X", latitude=999.0, longitude=0.0),
            }
        )
        _checked, issues = _validate_radar(catalog)
        assert any("latitude" in i for i in issues), "bad latitude flagged"

    def test_goes_clean_catalog_passes(self):
        """A well-formed GOES product yields no issues."""
        catalog = SimpleNamespace(
            domains={"C": None, "F": None, "M1": None, "M2": None},
            datasets={
                "abi-l2-mcmip": SimpleNamespace(
                    product_group="ABI-L2-MCMIP",
                    domains=["C", "F"],
                    default_domain="C",
                    band_split=False,
                    bands=[],
                ),
            },
        )
        checked, issues = _validate_goes(catalog)
        assert (checked, issues) == (1, []), "a clean product lints clean"

    def test_goes_flags_missing_product_group(self):
        """A GOES product missing product_group / domains is flagged (_require branch)."""
        catalog = SimpleNamespace(
            domains={"C": None, "F": None},
            datasets={
                "bare": SimpleNamespace(
                    product_group="",
                    domains=[],
                    default_domain="C",
                    band_split=False,
                    bands=[],
                ),
            },
        )
        _checked, issues = _validate_goes(catalog)
        assert any("product_group" in i for i in issues), (
            "missing product_group flagged"
        )
        assert any("domains" in i for i in issues), "empty domains flagged"

    def test_goes_flags_unknown_domain_and_empty_bands(self):
        """An unknown domain, a stray default, and empty band-split bands are flagged."""
        catalog = SimpleNamespace(
            domains={"C": None, "F": None},
            datasets={
                "bad": SimpleNamespace(
                    product_group="ABI-L2-BAD",
                    domains=["C", "Z"],
                    default_domain="F",
                    band_split=True,
                    bands=[],
                ),
            },
        )
        _checked, issues = _validate_goes(catalog)
        assert any("unknown domain" in i for i in issues), "bad domain flagged"
        assert any("default_domain" in i for i in issues), "stray default flagged"
        assert any("bands" in i for i in issues), "empty band-split bands flagged"

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

    def test_s3_live_keys_lists_one(self, monkeypatch):
        """_s3_live_keys returns the object keys from an unsigned client."""
        import earthlens.base.s3 as s3_auth
        from earthlens.cli.validate import _s3_live_keys

        class FakeClient:
            def list_objects_v2(self, **kw):
                return {"Contents": [{"Key": "k"}]}

        class FakeAuth:
            def __init__(self, creds):
                pass

            def client(self):
                return FakeClient()

        monkeypatch.setattr(s3_auth, "S3Auth", FakeAuth)
        assert _s3_live_keys("b", "p", None) == ["k"], "object key returned"

    def test_radar_feed_stations_paginates(self, monkeypatch):
        """_radar_feed_stations follows the continuation token across pages."""
        import earthlens.radar.backend as radar_backend
        from earthlens.cli.validate import _radar_feed_stations

        pages = [
            {
                "CommonPrefixes": [{"Prefix": "KAAA/"}],
                "IsTruncated": True,
                "NextContinuationToken": "t",
            },
            {"CommonPrefixes": [{"Prefix": "KBBB/"}], "IsTruncated": False},
        ]

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def list_objects_v2(self, **kw):
                page = pages[self.calls]
                self.calls += 1
                return page

        monkeypatch.setattr(radar_backend, "_s3_client", lambda region: FakeClient())
        assert _radar_feed_stations() == {"KAAA", "KBBB"}, "both pages collected"


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

    def test_live_s3_reports_bucket_error(self, monkeypatch):
        """A bucket whose listing raises is reported as drift, not raised."""

        def boom(b, p, r):
            raise RuntimeError("403")

        monkeypatch.setattr(validate_mod, "_s3_live_keys", boom)
        result = validate_one(_info("s3"), live=True)
        assert any("bucket error" in i for i in result.issues), "error captured"

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
