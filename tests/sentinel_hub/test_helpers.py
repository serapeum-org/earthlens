"""Unit tests for the Sentinel Hub stateless helpers + plane dispatch."""

from __future__ import annotations

import pytest

from earthlens.sentinel_hub._dispatch import (
    auto_select_api,
    resolve_api,
    validate_api,
)
from earthlens.sentinel_hub._helpers import (
    ASYNC_MAX_DIMENSION,
    SH_MAX_DIMENSION,
    VALID_APIS,
    cdse_collection,
    interval_for,
    resolve_endpoint,
)

pytestmark = pytest.mark.sentinel_hub


class TestResolveEndpoint:
    """`resolve_endpoint` alias / URL / default handling."""

    def test_default_is_cdse(self):
        """A `None` endpoint resolves to CDSE-free."""
        base, token = resolve_endpoint(None)
        assert base == "https://sh.dataspace.copernicus.eu"
        assert "dataspace" in token

    def test_commercial_alias(self):
        """The `commercial` alias resolves to the commercial host."""
        base, _ = resolve_endpoint("commercial")
        assert base == "https://services.sentinel-hub.com"

    def test_full_cdse_url_keeps_cdse_token(self):
        """A full CDSE base URL pairs with the CDSE Keycloak token URL."""
        _, token = resolve_endpoint("https://sh.dataspace.copernicus.eu")
        assert "dataspace" in token

    def test_full_commercial_url_keeps_commercial_token(self):
        """A non-CDSE URL pairs with the commercial token URL."""
        _, token = resolve_endpoint("https://sh.example.com")
        assert "sentinel-hub.com" in token

    def test_unknown_alias_raises(self):
        """An unknown non-URL string is rejected."""
        with pytest.raises(ValueError, match="unknown Sentinel Hub endpoint"):
            resolve_endpoint("nope")


class TestIntervalFor:
    """`interval_for` pandas-freq → ISO-8601 mapping."""

    @pytest.mark.parametrize(
        ("freq", "expected"),
        [("D", "P1D"), ("1MS", "P1M"), ("7D", "P7D"), ("10D", "P10D"), ("YS", "P1Y")],
    )
    def test_known_frequencies(self, freq, expected):
        """Known aliases map to their ISO duration."""
        assert interval_for(freq) == expected

    def test_unknown_frequency_raises(self):
        """An unmapped frequency raises NotImplementedError."""
        with pytest.raises(NotImplementedError):
            interval_for("B")


class TestValidateApi:
    """`validate_api` accepts `None` + known planes, rejects others."""

    def test_none_ok(self):
        """`None` (auto) is accepted."""
        validate_api(None)

    @pytest.mark.parametrize("api", VALID_APIS)
    def test_known_ok(self, api):
        """Every valid plane is accepted."""
        validate_api(api)

    def test_unknown_raises(self):
        """An unknown plane is rejected with the valid list."""
        with pytest.raises(ValueError, match="unknown api"):
            validate_api("render")


class TestAutoSelectApi:
    """`auto_select_api` size + geometry routing."""

    def test_geometry_routes_to_statistical(self):
        """A geometry request always goes to the Statistical plane."""
        assert auto_select_api(50, has_geometry=True) == "statistical"

    def test_small_raster_is_process(self):
        """A bbox within the Process cap routes to Process."""
        assert auto_select_api(SH_MAX_DIMENSION, has_geometry=False) == "process"

    def test_medium_raster_without_s3_is_tiling(self):
        """A medium bbox with no S3 bucket falls back to local tiling."""
        assert auto_select_api(SH_MAX_DIMENSION + 1, has_geometry=False) == "tiling"

    def test_medium_raster_with_s3_is_async(self):
        """A medium bbox with an S3 bucket routes to Async."""
        assert (
            auto_select_api(SH_MAX_DIMENSION + 1, has_geometry=False, has_s3=True)
            == "async"
        )

    def test_huge_raster_with_s3_is_batch(self):
        """A bbox above the Async ceiling (with S3) routes to Batch."""
        assert (
            auto_select_api(ASYNC_MAX_DIMENSION + 1, has_geometry=False, has_s3=True)
            == "batch"
        )

    def test_huge_raster_without_s3_is_tiling(self):
        """A huge bbox with no S3 bucket still falls back to tiling."""
        assert (
            auto_select_api(ASYNC_MAX_DIMENSION + 1, has_geometry=False) == "tiling"
        )


class TestResolveApi:
    """`resolve_api` honours an explicit plane, else auto-selects."""

    def test_explicit_wins(self):
        """An explicit plane is returned verbatim regardless of size."""
        assert resolve_api("process", 99999, has_geometry=False) == "process"

    def test_auto_when_none(self):
        """A `None` api auto-selects by size."""
        assert resolve_api(None, 100, has_geometry=False) == "process"

    def test_invalid_explicit_raises(self):
        """An invalid explicit plane is rejected."""
        with pytest.raises(ValueError):
            resolve_api("bogus", 100, has_geometry=False)


class TestCdseCollection:
    """`cdse_collection` rebinds on CDSE, returns stock off-CDSE."""

    def test_rebinds_on_cdse(self, fake_sh):
        """A CDSE base URL rebinds the collection via `define_from`."""
        coll = cdse_collection("SENTINEL2_L2A", "https://sh.dataspace.copernicus.eu")
        assert coll.service_url == "https://sh.dataspace.copernicus.eu"
        assert coll.name == "sentinel2_l2a_cdse"

    def test_unchanged_off_cdse(self, fake_sh):
        """A commercial base URL returns the stock member unchanged."""
        coll = cdse_collection("SENTINEL2_L2A", "https://services.sentinel-hub.com")
        assert coll.name == "SENTINEL2_L2A"
        assert coll.service_url is None

    def test_unknown_collection_raises(self, fake_sh):
        """An unknown DataCollection name raises a clear error."""
        with pytest.raises(ValueError, match="not a known sentinelhub DataCollection"):
            cdse_collection("NOPE", "https://services.sentinel-hub.com")
