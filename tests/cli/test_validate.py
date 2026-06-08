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
    "nwm",
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

    def test_nwm_token_present_matches_bare_and_ensemble(self):
        """A bare token matches; an ensemble `{token}_{member}` form matches too."""
        from earthlens.cli.validate import _nwm_token_present

        assert _nwm_token_present("channel_rt", {"channel_rt"}), "bare token"
        assert _nwm_token_present("channel_rt", {"channel_rt_1"}), "ensemble member"
        assert not _nwm_token_present("channel_rt", {"land", "reservoir"}), "absent"

    def test_nwm_live_flags_token_absent_from_bucket(self, monkeypatch):
        """A product whose s3_token shows on no live carrier config is flagged."""
        from earthlens.cli import refresh as refresh_mod

        monkeypatch.setattr(refresh_mod, "_nwm_unsigned_client", lambda: object())
        monkeypatch.setattr(refresh_mod, "_nwm_latest_complete_day", lambda c: "nwm.0")
        monkeypatch.setattr(
            validate_mod, "_nwm_sample_tokens", lambda c, d, dir_: set()
        )
        result = validate_one(_info("nwm"), live=True)
        assert result.status == "ok", "nwm live validator ran"
        assert result.issues, "an empty bucket flags every product token"
        assert all("s3_token" in issue for issue in result.issues), "token messages"

    def test_nwm_live_clean_when_tokens_present(self, monkeypatch):
        """Every product's token appearing under a carrier config clears live."""
        from earthlens.cli import refresh as refresh_mod
        from earthlens.cli.adapter import load_catalog

        all_tokens = {
            product.s3_token for product in load_catalog(_info("nwm")).datasets.values()
        }
        monkeypatch.setattr(refresh_mod, "_nwm_unsigned_client", lambda: object())
        monkeypatch.setattr(refresh_mod, "_nwm_latest_complete_day", lambda c: "nwm.0")
        monkeypatch.setattr(
            validate_mod, "_nwm_sample_tokens", lambda c, d, dir_: set(all_tokens)
        )
        result = validate_one(_info("nwm"), live=True)
        assert result.issues == [], "every token present under a carrier -> clean"

    def test_supported_providers_live_adds_ecmwf_and_nwp(self):
        """ecmwf gains a live-only validator; nwp gains a live one on top."""
        assert "ecmwf" in supported_providers(live=True)
        assert "nwp" in supported_providers(live=True)


class _FakeSampleClient:
    """A minimal S3 stand-in serving canned `Contents` for token sampling."""

    def __init__(self, contents):
        self._contents = contents

    def list_objects_v2(self, **kwargs):
        """Return the canned object `Contents` regardless of the prefix."""
        return {"Contents": self._contents}


def _key(directory, output):
    """Build a NWM object key with the given `{output}` token."""
    return f"nwm.20260602/{directory}/nwm.t00z.short_range.{output}.f001.conus.nc"


class TestNwmValidateInternals:
    """Direct tests for the NWM validate helpers (network mocked)."""

    def test_sample_tokens_parses_output_and_skips_short_names(self):
        """The `{output}` token is parsed; names with too few parts are skipped."""
        client = _FakeSampleClient(
            [
                {"Key": _key("short_range", "channel_rt")},
                {"Key": "nwm.20260602/short_range/nwm.t00z.land.nc"},
            ]
        )
        tokens = validate_mod._nwm_sample_tokens(client, "nwm.20260602", "short_range")
        assert tokens == {"channel_rt"}, f"only the well-formed token parsed: {tokens}"

    def test_sample_tokens_empty_listing_returns_empty_set(self):
        """A directory with no objects samples an empty token set."""
        tokens = validate_mod._nwm_sample_tokens(_FakeSampleClient([]), "d", "x")
        assert tokens == set(), f"expected empty set, got {tokens}"

    def test_config_directory_appends_mem1_for_ensembles(self):
        """An ensemble config maps to its `_mem1` directory; deterministic stays bare."""
        ensemble = SimpleNamespace(members=6)
        deterministic = SimpleNamespace(members=0)
        assert validate_mod._nwm_config_directory(ensemble, "medium_range") == (
            "medium_range_mem1"
        ), "ensemble appends _mem1"
        assert (
            validate_mod._nwm_config_directory(deterministic, "short_range")
            == "short_range"
        ), "deterministic stays bare"

    @pytest.mark.parametrize(
        "tokens, expected",
        [
            ({"channel_rt"}, True),
            ({"channel_rt_1"}, True),
            ({"channel_rt_12"}, True),
            ({"land", "reservoir"}, False),
            (set(), False),
        ],
    )
    def test_token_present_bare_and_ensemble_forms(self, tokens, expected):
        """A bare token or its `{token}_{member}` ensemble form counts as present.

        Args:
            tokens: The sampled file tokens for one configuration directory.
            expected: Whether `channel_rt` should be reported present.
        """
        assert (
            validate_mod._nwm_token_present("channel_rt", tokens) is expected
        ), f"_nwm_token_present('channel_rt', {tokens}) should be {expected}"

    def test_validate_nwm_flags_empty_variables(self):
        """A product with an empty `variables` map is flagged by the offline lint."""
        catalog = SimpleNamespace(
            datasets={"bad": SimpleNamespace(s3_token="x", variables={})},
            configurations={},
        )
        checked, issues = validate_mod._validate_nwm(catalog)
        assert checked == 1, "one product inspected"
        assert any("variables" in issue for issue in issues), "empty variables flagged"

    def test_validate_nwm_flags_unknown_product_in_configuration(self):
        """A configuration referencing an uncurated product key is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "chrtout": SimpleNamespace(
                    s3_token="channel_rt", variables={"streamflow": object()}
                )
            },
            configurations={
                "short_range": SimpleNamespace(products=["chrtout", "ghost"])
            },
        )
        _checked, issues = validate_mod._validate_nwm(catalog)
        assert any(
            "ghost" in issue and "unknown product" in issue for issue in issues
        ), f"dangling product reference flagged: {issues}"

    def test_validate_nwm_clean_minimal_catalog(self):
        """A coherent minimal catalog produces no offline issues."""
        catalog = SimpleNamespace(
            datasets={
                "chrtout": SimpleNamespace(
                    s3_token="channel_rt", variables={"streamflow": object()}
                )
            },
            configurations={"short_range": SimpleNamespace(products=["chrtout"])},
        )
        checked, issues = validate_mod._validate_nwm(catalog)
        assert checked == 1 and issues == [], f"coherent catalog is clean: {issues}"

    def test_live_nwm_flags_only_the_absent_product(self, monkeypatch):
        """`_live_nwm` flags exactly the product whose token no carrier serves."""
        from earthlens.cli import refresh as refresh_mod

        catalog = SimpleNamespace(
            datasets={
                "a": SimpleNamespace(s3_token="ta"),
                "b": SimpleNamespace(s3_token="tb"),
            },
            configurations={"cfg": SimpleNamespace(members=0, products=["a", "b"])},
        )
        monkeypatch.setattr(refresh_mod, "_nwm_unsigned_client", lambda: object())
        monkeypatch.setattr(refresh_mod, "_nwm_latest_complete_day", lambda c: "d")
        monkeypatch.setattr(
            validate_mod, "_nwm_sample_tokens", lambda c, d, dir_: {"ta"}
        )
        checked, issues = validate_mod._live_nwm(catalog)
        assert checked == 2, "both products inspected"
        assert len(issues) == 1 and "b" in issues[0], f"only 'b' flagged: {issues}"

    def test_live_nwm_ensemble_carrier_matches_member_token(self, monkeypatch):
        """An ensemble-only carrier's `{token}_{member}` file satisfies the check."""
        from earthlens.cli import refresh as refresh_mod

        catalog = SimpleNamespace(
            datasets={"a": SimpleNamespace(s3_token="channel_rt")},
            configurations={"ens": SimpleNamespace(members=6, products=["a"])},
        )
        captured = {}

        def _sample(client, day, directory):
            """Record the sampled directory and return the member-suffixed token."""
            captured["dir"] = directory
            return {"channel_rt_1"}

        monkeypatch.setattr(refresh_mod, "_nwm_unsigned_client", lambda: object())
        monkeypatch.setattr(refresh_mod, "_nwm_latest_complete_day", lambda c: "d")
        monkeypatch.setattr(validate_mod, "_nwm_sample_tokens", _sample)
        checked, issues = validate_mod._live_nwm(catalog)
        assert issues == [], "the ensemble member token satisfies the bare token"
        assert captured["dir"] == "ens_mem1", "the ensemble member-1 directory sampled"


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

    def test_usgs_water_unknown_service_flagged(self):
        """A parameter declaring an unknown service is flagged."""
        from earthlens.cli.validate import _validate_usgs_water

        catalog = SimpleNamespace(
            datasets={"q": SimpleNamespace(services=["daily", "not-a-service"])}
        )
        _checked, issues = _validate_usgs_water(catalog)
        assert any("not-a-service" in i for i in issues), "unknown service flagged"

    def test_sentinel_hub_bad_evalscript_flagged(self, monkeypatch):
        """A recipe whose evalscript lacks //VERSION=3 + dataMask is flagged."""
        from earthlens.cli import validate as vm
        from earthlens.cli.validate import _validate_sentinel_hub

        monkeypatch.setattr(
            "earthlens.sentinel_hub.read_evalscript",
            lambda name: "// not versioned\nreturn x;",
        )
        catalog = SimpleNamespace(
            recipes={
                "r": SimpleNamespace(evalscript="r.js", kind="stats"),
                "blank": SimpleNamespace(evalscript=None, kind="render"),
            }
        )
        _checked, issues = _validate_sentinel_hub(catalog)
        assert any("//VERSION=3" in i for i in issues), "version header flagged"
        assert any("dataMask" in i for i in issues), "stats dataMask flagged"
        assert vm is not None


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

    def test_openeo_live_lists_unions_ids(self, monkeypatch):
        """_openeo_live_lists collects collection + process ids from the API."""
        from earthlens.cli.validate import _openeo_live_lists

        def fake_get(url):
            if "processes" in url:
                return {"processes": [{"id": "ndvi"}, {"no": "id"}]}
            return {"collections": [{"id": "S2"}]}

        monkeypatch.setattr(validate_mod, "_get_json", fake_get)
        collections, processes = _openeo_live_lists()
        assert collections == {"S2"} and processes == {"ndvi"}, "ids unioned"

    def test_s3_live_keys_lists_one(self, monkeypatch):
        """_s3_live_keys returns the object keys from an unsigned client."""
        import earthlens.s3.auth as s3_auth
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

    def test_overture_live_sample_reports_sources(self, monkeypatch):
        """_overture_live_sample returns (row_count, has_sources_column)."""
        import overturemaps.core as core

        from earthlens.cli.validate import _overture_live_sample

        class FakeFrame:
            columns = ["id", "sources"]

            def __len__(self):
                return 2

        monkeypatch.setattr(core, "geodataframe", lambda t, bbox: FakeFrame())
        rows, has_sources = _overture_live_sample("building")
        assert rows == 2 and has_sources is True, "rows + sources column reported"


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

    def test_sentinel_hub_missing_evalscript_file(self, monkeypatch):
        """A recipe whose evalscript file is missing is flagged."""
        from earthlens.cli.validate import _validate_sentinel_hub

        def missing(name):
            raise FileNotFoundError(f"{name} not found")

        monkeypatch.setattr("earthlens.sentinel_hub.read_evalscript", missing)
        catalog = SimpleNamespace(
            recipes={"r": SimpleNamespace(evalscript="gone.js", kind="render")}
        )
        _checked, issues = _validate_sentinel_hub(catalog)
        assert any("gone.js" in i for i in issues), "missing file flagged"

    def test_live_s3_reports_bucket_error(self, monkeypatch):
        """A bucket whose listing raises is reported as drift, not raised."""

        def boom(b, p, r):
            raise RuntimeError("403")

        monkeypatch.setattr(validate_mod, "_s3_live_keys", boom)
        result = validate_one(_info("s3"), live=True)
        assert any("bucket error" in i for i in result.issues), "error captured"

    def test_live_overture_reports_fetch_failure(self, monkeypatch):
        """An Overture type whose fetch raises is reported, not raised."""

        def boom(t):
            raise RuntimeError("network")

        monkeypatch.setattr(validate_mod, "_overture_live_sample", boom)
        result = validate_one(_info("overture"), live=True)
        assert any("fetch failed" in i for i in result.issues), "fetch failure reported"

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
