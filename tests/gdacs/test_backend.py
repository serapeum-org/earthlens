"""Unit + integration tests for `earthlens.gdacs.backend`."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from geopandas import GeoDataFrame

from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent
from earthlens.gdacs import GDACS
from earthlens.gdacs.backend import SEARCH_URL

from .conftest import _FakeGdacs


def _make_backend(tmp_path: Path, **overrides) -> GDACS:
    """Construct a GDACS backend with sensible defaults for tests."""
    params: dict[str, object] = dict(
        start="2026-05-01",
        end="2026-05-21",
        variables=["EQ"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
    )
    params.update(overrides)
    return GDACS(**params)


@pytest.mark.gdacs
class TestGDACSConstruction:
    """`__init__` wiring: OUTPUT_KIND, space, time, defaults."""

    def test_output_kind_is_vector(self):
        """GDACS declares vector output (alert features, not gridded)."""
        assert (
            GDACS.OUTPUT_KIND == "vector"
        ), f"GDACS.OUTPUT_KIND must be 'vector', got {GDACS.OUTPUT_KIND!r}"

    def test_space_captured(self, tmp_path: Path):
        """The bbox lands on `self.space` as a SpatialExtent."""
        backend = _make_backend(tmp_path, lat_lim=[30.0, 45.0], lon_lim=[10.0, 20.0])
        assert isinstance(backend.space, SpatialExtent)
        assert backend.space.south == 30.0
        assert backend.space.east == 20.0

    def test_time_resolution_sentinel(self, tmp_path: Path):
        """The temporal resolution is the GDACS 'all' sentinel."""
        backend = _make_backend(tmp_path)
        assert isinstance(backend.time, TemporalExtent)
        assert (
            backend.time.resolution == "all"
        ), f"expected 'all' sentinel, got {backend.time.resolution!r}"

    def test_empty_variables_defaults_to_all_types(self, tmp_path: Path):
        """An empty `variables` list defaults to all six hazard types."""
        backend = _make_backend(tmp_path, variables=[])
        assert backend.vars == ["EQ", "TC", "FL", "VO", "WF", "DR"]

    def test_default_alert_levels(self, tmp_path: Path):
        """`alert_level=None` defaults to all three levels."""
        backend = _make_backend(tmp_path)
        assert backend._alert_levels == ["Green", "Orange", "Red"]

    def test_custom_alert_levels(self, tmp_path: Path):
        """An explicit alert_level list is preserved."""
        backend = _make_backend(tmp_path, alert_level=["Red"])
        assert backend._alert_levels == ["Red"]

    def test_invalid_file_format_rejected(self, tmp_path: Path):
        """An unsupported file_format raises at construction."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _make_backend(tmp_path, file_format="shp")

    def test_mapping_variables_rejected(self, tmp_path: Path):
        """A dict `variables` (backend-style mapping) raises a clear TypeError."""
        with pytest.raises(TypeError, match="must be a list of hazard-type codes"):
            _make_backend(tmp_path, variables={"EQ": ["magnitude"]})

    def test_no_network_on_construction(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """Constructing the backend issues no HTTP request."""
        _make_backend(tmp_path)
        assert fake_gdacs.calls == [], "construction must not hit the network"


@pytest.mark.gdacs
class TestGDACSSearch:
    """`_search` builds one product carrying the combined query."""

    def test_single_product(self, tmp_path: Path):
        """All hazard types ride one product (GDACS returns them in one GET)."""
        backend = _make_backend(tmp_path, variables=["EQ", "TC"])
        products = backend._search()
        assert len(products) == 1
        assert isinstance(products[0], RemoteProduct)

    def test_product_carries_query(self, tmp_path: Path):
        """The product metadata carries types, levels, and the date window."""
        backend = _make_backend(tmp_path, variables=["EQ", "TC"], alert_level=["Red"])
        meta = backend._search()[0].metadata
        assert meta["event_types"] == ["EQ", "TC"]
        assert meta["alert_levels"] == ["Red"]
        assert "from" in meta and "to" in meta

    def test_unknown_hazard_raises(self, tmp_path: Path):
        """A hazard code absent from the catalog raises with a hint."""
        backend = _make_backend(tmp_path, variables=["EQK"])
        with pytest.raises(ValueError, match="Did you mean 'EQ'"):
            backend._search()


@pytest.mark.gdacs
class TestGDACSFetch:
    """`_fetch` issues the combined GET and maps the GeoJSON."""

    def test_returns_feature_collection(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """`_fetch` returns a single-element list of FeatureCollections."""
        backend = _make_backend(tmp_path)
        results = backend._fetch(backend._search())
        assert len(results) == 1
        assert isinstance(results[0], GeoDataFrame)

    def test_hits_search_url(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """The request targets the GDACS SEARCH endpoint."""
        backend = _make_backend(tmp_path)
        backend._fetch(backend._search())
        assert fake_gdacs.calls[0]["url"] == SEARCH_URL

    def test_forwards_params(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """Date window, hazard list, and alert levels reach the query params."""
        backend = _make_backend(
            tmp_path, variables=["EQ", "TC"], alert_level=["Green", "Red"]
        )
        backend._fetch(backend._search())
        params = fake_gdacs.calls[0]["params"]
        assert params["fromDate"] == "2026-05-01"
        assert params["toDate"] == "2026-05-21"
        assert params["eventlist"] == "EQ,TC"
        assert params["alertlevel"] == "Green;Red"

    def test_forwards_timeout(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """The configured timeout reaches `requests.get`."""
        backend = _make_backend(tmp_path, timeout=12.0)
        backend._fetch(backend._search())
        assert fake_gdacs.calls[0]["timeout"] == 12.0

    def test_empty_feed_yields_empty_fc(
        self, tmp_path: Path, fake_gdacs: _FakeGdacs, make_payload
    ):
        """An empty feed maps to an empty FeatureCollection, not an error."""
        fake_gdacs.set_payload(make_payload(features=[]))
        backend = _make_backend(tmp_path)
        results = backend._fetch(backend._search())
        assert len(results[0]) == 0, "empty feed should yield an empty FC"

    def test_http_error_propagates(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """A non-2xx status propagates rather than being swallowed."""
        fake_gdacs.set_status_error(requests.HTTPError("500 Server Error"))
        backend = _make_backend(tmp_path)
        with pytest.raises(requests.HTTPError, match="500"):
            backend._fetch(backend._search())

    def test_cap_logs_truncation_warning(
        self,
        tmp_path: Path,
        fake_gdacs: _FakeGdacs,
        make_feature,
        make_payload,
        warnings_log,
    ):
        """A response at the 100-event cap logs a truncation warning."""
        from earthlens.gdacs.backend import MAX_EVENTS_PER_RESPONSE

        fake_gdacs.set_payload(
            make_payload(
                features=[
                    make_feature(eventid=i) for i in range(MAX_EVENTS_PER_RESPONSE)
                ]
            )
        )
        backend = _make_backend(tmp_path)
        backend._fetch(backend._search())
        assert any(
            "truncated" in msg for msg in warnings_log
        ), f"expected a truncation warning, got {warnings_log}"

    def test_below_cap_no_warning(
        self,
        tmp_path: Path,
        fake_gdacs: _FakeGdacs,
        make_feature,
        make_payload,
        warnings_log,
    ):
        """A response below the cap logs no truncation warning."""
        fake_gdacs.set_payload(
            make_payload(features=[make_feature(eventid=i) for i in range(5)])
        )
        backend = _make_backend(tmp_path)
        backend._fetch(backend._search())
        assert not any(
            "truncated" in msg for msg in warnings_log
        ), f"unexpected truncation warning, got {warnings_log}"

    def test_bbox_post_filter(
        self, tmp_path: Path, fake_gdacs: _FakeGdacs, make_feature, make_payload
    ):
        """Alerts outside lat_lim/lon_lim are dropped client-side."""
        fake_gdacs.set_payload(
            make_payload(
                features=[
                    make_feature(eventid=1, lon=12.5, lat=10.0),
                    make_feature(eventid=2, lon=100.0, lat=80.0),
                ]
            )
        )
        backend = _make_backend(tmp_path, lat_lim=[0.0, 20.0], lon_lim=[0.0, 20.0])
        results = backend._fetch(backend._search())
        assert len(results[0]) == 1, "out-of-box alert should be dropped"
        assert results[0]["event_id"].iloc[0] == "1"


@pytest.mark.gdacs
class TestGDACSDownload:
    """`download` writes one file and returns the alert FeatureCollection."""

    def test_returns_and_writes(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """A download returns the FC and writes one GeoPackage."""
        backend = _make_backend(tmp_path)
        fc = backend.download(progress_bar=False)
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1, f"expected 1 alert, got {len(fc)}"
        assert (tmp_path / "gdacs_alerts.gpkg").is_file()

    def test_geojson_format(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """`file_format='geojson'` writes a .geojson file."""
        backend = _make_backend(tmp_path, file_format="geojson")
        backend.download(progress_bar=False)
        assert (tmp_path / "gdacs_alerts.geojson").is_file()

    def test_empty_writes_nothing(
        self, tmp_path: Path, fake_gdacs: _FakeGdacs, make_payload
    ):
        """An empty result returns an empty FC and writes no file."""
        fake_gdacs.set_payload(make_payload(features=[]))
        backend = _make_backend(tmp_path)
        fc = backend.download(progress_bar=False)
        assert len(fc) == 0
        assert list(tmp_path.glob("*.gpkg")) == [], "nothing should be written"

    def test_aggregate_rejected(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """A non-None aggregate is rejected (vector output)."""
        backend = _make_backend(tmp_path)
        with pytest.raises(NotImplementedError, match="vector"):
            backend.download(aggregate=object())

    def test_api_via_search_fetch(self, tmp_path: Path, fake_gdacs: _FakeGdacs):
        """`_api` composes search+fetch and returns FeatureCollections."""
        backend = _make_backend(tmp_path)
        results = backend._api()
        assert len(results) == 1
        assert isinstance(results[0], GeoDataFrame)
