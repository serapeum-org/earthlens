"""Unit + integration tests for `earthlens.fdsn.backend`."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from geopandas import GeoDataFrame
from obspy.clients.fdsn.header import FDSNNoDataException

from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent
from earthlens.fdsn import FDSN, Catalog, Provider

from .conftest import _FakeFdsn


def _make_backend(tmp_path: Path, **overrides) -> FDSN:
    """Construct an FDSN backend with sensible defaults for tests."""
    params: dict[str, object] = dict(
        start="2024-01-01",
        end="2024-01-31",
        variables=["USGS"],
        lat_lim=[30.0, 45.0],
        lon_lim=[130.0, 145.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return FDSN(**params)


@pytest.mark.fdsn
class TestFDSNConstruction:
    """`__init__` wiring: OUTPUT_KIND, space, time, defaults."""

    def test_output_kind_is_vector(self):
        """FDSN declares vector output (point features, not gridded)."""
        assert FDSN.OUTPUT_KIND == "vector", (
            f"FDSN.OUTPUT_KIND must be 'vector', got {FDSN.OUTPUT_KIND!r}"
        )

    def test_space_captured(self, tmp_path: Path):
        """The bbox lands on `self.space` as a SpatialExtent."""
        backend = _make_backend(tmp_path)
        assert isinstance(backend.space, SpatialExtent)
        assert backend.space.south == 30.0
        assert backend.space.east == 145.0

    def test_time_resolution_sentinel(self, tmp_path: Path):
        """The temporal resolution is the FDSN 'all' sentinel."""
        backend = _make_backend(tmp_path)
        assert isinstance(backend.time, TemporalExtent)
        assert backend.time.resolution == "all", (
            f"expected 'all' sentinel, got {backend.time.resolution!r}"
        )

    def test_empty_variables_defaults_to_usgs(self, tmp_path: Path):
        """An empty `variables` list defaults to ['USGS']."""
        backend = _make_backend(tmp_path, variables=[])
        assert backend.vars == ["USGS"], f"expected ['USGS'], got {backend.vars}"

    def test_invalid_file_format_rejected(self, tmp_path: Path):
        """An unsupported file_format raises at construction."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _make_backend(tmp_path, file_format="shp")

    def test_mapping_variables_rejected(self, tmp_path: Path):
        """A dict `variables` (backend-style mapping) raises a clear TypeError."""
        with pytest.raises(TypeError, match="must be a list of network keys"):
            _make_backend(tmp_path, variables={"USGS": ["events"]})


@pytest.mark.fdsn
class TestFDSNSearch:
    """`_search` builds one product per requested network."""

    def test_one_product_per_provider(self, tmp_path: Path):
        """Two networks yield two products, in request order."""
        backend = _make_backend(tmp_path, variables=["USGS", "EMSC"])
        products = backend._search()
        assert [p.id for p in products] == ["USGS", "EMSC"]
        assert all(isinstance(p, RemoteProduct) for p in products)

    def test_product_carries_fdsn_id(self, tmp_path: Path):
        """The resolved obspy URL_MAPPINGS key rides on product metadata."""
        product = _make_backend(tmp_path, variables=["USGS"])._search()[0]
        assert product.metadata["fdsn_id"] == "USGS"

    def test_product_carries_default_min_magnitude(self, tmp_path: Path):
        """The provider's catalog magnitude floor rides on product metadata."""
        product = _make_backend(tmp_path, variables=["INGV"])._search()[0]
        assert product.metadata["default_min_magnitude"] == 2.0

    def test_unknown_provider_raises(self, tmp_path: Path):
        """A network key absent from the catalog raises with a hint."""
        backend = _make_backend(tmp_path, variables=["USG"])
        with pytest.raises(ValueError, match="Did you mean 'USGS'"):
            backend._search()


@pytest.mark.fdsn
class TestFDSNFetch:
    """`_fetch` queries each network and maps results to FeatureCollections."""

    def test_returns_feature_collections(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """`_fetch` returns one FeatureCollection per product."""
        backend = _make_backend(tmp_path)
        results = backend._fetch(backend._search())
        assert len(results) == 1
        assert isinstance(results[0], GeoDataFrame)

    def test_forwards_bbox_dates_magnitude(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """bbox, time window, and min_magnitude reach `get_events`."""
        backend = _make_backend(tmp_path, min_magnitude=5.0)
        backend._fetch(backend._search())
        _, kwargs = fake_fdsn.calls[0]
        assert kwargs["minlatitude"] == 30.0
        assert kwargs["maxlongitude"] == 145.0
        assert kwargs["minmagnitude"] == 5.0
        assert str(kwargs["starttime"]).startswith("2024-01-01")
        assert str(kwargs["endtime"]).startswith("2024-01-31")

    def test_nodata_yields_empty_not_error(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """FDSNNoDataException maps to an empty FeatureCollection."""
        fake_fdsn.set_result("USGS", FDSNNoDataException("204"))
        backend = _make_backend(tmp_path)
        results = backend._fetch(backend._search())
        assert len(results[0]) == 0, "no-data network should yield an empty FC"

    def test_total_failure_raises(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """When every network errors, `_fetch` raises RuntimeError naming the cause."""
        fake_fdsn.set_result("USGS", RuntimeError("boom"))
        backend = _make_backend(tmp_path)
        with pytest.raises(RuntimeError, match="boom"):
            backend._fetch(backend._search())

    def test_partial_failure_skips_and_continues(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """One failing network is skipped; the healthy network's events survive."""
        fake_fdsn.set_result("EMSC", RuntimeError("emsc down"))
        backend = _make_backend(tmp_path, variables=["USGS", "EMSC"])
        results = backend._fetch(backend._search())
        assert len(results) == 2, "result list stays aligned with products"
        assert len(results[0]) == 1, "healthy USGS network returns its event"
        assert len(results[1]) == 0, "failed EMSC network contributes an empty FC"

    def test_token_passed_only_when_needed(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """No eida_token is sent for a network that does not require one."""
        backend = _make_backend(tmp_path, earthscope_token="tok")
        backend._fetch(backend._search())
        _, ctor_kwargs = fake_fdsn.constructions[0]
        assert "eida_token" not in ctor_kwargs, (
            "public USGS network must not receive a token"
        )

    def test_token_passed_when_provider_needs_it(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """A needs_token provider with a token receives eida_token on the client."""
        backend = _make_backend(tmp_path, earthscope_token="tok")
        backend._catalog = Catalog(
            providers={"USGS": Provider(fdsn_id="USGS", needs_token=True)}
        )
        backend._fetch(backend._search())
        _, ctor_kwargs = fake_fdsn.constructions[0]
        assert ctor_kwargs.get("eida_token") == "tok", (
            "token-gated network should receive the resolved token"
        )

    def test_versioned_user_agent(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """The obspy client is built with a versioned earthlens user-agent."""
        backend = _make_backend(tmp_path)
        backend._fetch(backend._search())
        _, ctor_kwargs = fake_fdsn.constructions[0]
        assert ctor_kwargs["user_agent"].startswith("earthlens/"), (
            f"user_agent should be versioned, got {ctor_kwargs.get('user_agent')!r}"
        )

    @pytest.mark.parametrize(
        "provider, expected_floor",
        [("USGS", 4.5), ("INGV", 2.0), ("GEONET", 3.0)],
    )
    def test_min_magnitude_falls_back_per_provider(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn, provider: str, expected_floor: float
    ):
        """With min_magnitude=None, each network uses its catalog floor."""
        backend = _make_backend(tmp_path, variables=[provider], min_magnitude=None)
        backend._fetch(backend._search())
        _, query_kwargs = fake_fdsn.calls[0]
        assert query_kwargs["minmagnitude"] == expected_floor, (
            f"{provider} should fall back to {expected_floor}, got {query_kwargs['minmagnitude']}"
        )

    def test_explicit_min_magnitude_overrides_floor(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """An explicit min_magnitude overrides the per-provider floor."""
        backend = _make_backend(tmp_path, variables=["INGV"], min_magnitude=6.0)
        backend._fetch(backend._search())
        _, query_kwargs = fake_fdsn.calls[0]
        assert query_kwargs["minmagnitude"] == 6.0


@pytest.mark.fdsn
class TestFDSNDownload:
    """`download` writes per-network files and returns the union FC."""

    def test_returns_union_and_writes_files(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """Two networks write two files and return a 2-row union."""
        backend = _make_backend(tmp_path, variables=["USGS", "EMSC"])
        fc = backend.download()
        assert len(fc) == 2, f"expected 2 events, got {len(fc)}"
        written = sorted(p.name for p in tmp_path.glob("*.gpkg"))
        assert written == ["emsc.gpkg", "usgs.gpkg"], f"unexpected files {written}"

    def test_geojson_format(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """`file_format='geojson'` writes a .geojson file."""
        backend = _make_backend(tmp_path, file_format="geojson")
        backend.download()
        assert (tmp_path / "usgs.geojson").is_file()

    def test_all_empty_writes_nothing(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """An all-empty result returns an empty FC and writes no files."""
        fake_fdsn.set_result("USGS", FDSNNoDataException("204"))
        backend = _make_backend(tmp_path)
        fc = backend.download()
        assert len(fc) == 0
        assert list(tmp_path.glob("*.gpkg")) == [], "nothing should be written"

    def test_partial_failure_returns_healthy_network(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """A multi-network download survives one network failing."""
        fake_fdsn.set_result("EMSC", RuntimeError("emsc down"))
        backend = _make_backend(tmp_path, variables=["USGS", "EMSC"])
        fc = backend.download()
        assert len(fc) == 1, "the healthy USGS network's event is returned"
        written = sorted(p.name for p in tmp_path.glob("*.gpkg"))
        assert written == ["usgs.gpkg"], (
            f"only the healthy network is written: {written}"
        )

    def test_aggregate_rejected(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """A non-None aggregate is rejected (vector output)."""
        backend = _make_backend(tmp_path)
        with pytest.raises(NotImplementedError, match="vector"):
            backend.download(aggregate=object())

    def test_api_via_search_fetch(self, tmp_path: Path, fake_fdsn: _FakeFdsn):
        """`_api` composes search+fetch and returns FeatureCollections."""
        backend = _make_backend(tmp_path)
        results = backend._api()
        assert len(results) == 1
        assert isinstance(results[0], GeoDataFrame)

    def test_download_limit_overrides_constructor(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """A per-call limit replaces the constructor's for that query."""
        backend = _make_backend(tmp_path, limit=5)
        backend.download(limit=2)
        _base_url, kwargs = fake_fdsn.calls[-1]
        assert kwargs["limit"] == 2, f"expected the per-call cap, got {kwargs['limit']}"

    def test_download_limit_none_keeps_constructor(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """Omitting the per-call limit keeps whatever the constructor set."""
        backend = _make_backend(tmp_path, limit=5)
        backend.download()
        _base_url, kwargs = fake_fdsn.calls[-1]
        assert kwargs["limit"] == 5, (
            f"expected the constructor cap, got {kwargs['limit']}"
        )

    def test_download_rejects_non_positive_limit(
        self, tmp_path: Path, fake_fdsn: _FakeFdsn
    ):
        """A zero or negative per-call limit is refused."""
        backend = _make_backend(tmp_path)
        with pytest.raises(ValueError):
            backend.download(limit=0)
