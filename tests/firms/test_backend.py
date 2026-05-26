"""Unit + integration tests for `earthlens.firms.backend` (mocked HTTP)."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from geopandas import GeoDataFrame

from earthlens.base import SpatialExtent, TemporalExtent
from earthlens.firms import FIRMS, AuthenticationError

from .conftest import EMPTY_CSV, VIIRS_CSV, _FakeFirms, _FakeResponse


def _make_backend(tmp_path: Path, **overrides) -> FIRMS:
    """Construct a FIRMS backend with test defaults and a no-op sleep."""
    params: dict[str, object] = dict(
        start="2024-08-01",
        end="2024-08-01",
        variables=["VIIRS_SNPP_NRT"],
        lat_lim=[33.0, 35.0],
        lon_lim=[-119.0, -117.0],
        path=str(tmp_path),
        map_key="k",
    )
    params.update(overrides)
    backend = FIRMS(**params)
    backend._sleep = lambda _seconds: None
    return backend


@pytest.mark.firms
class TestConstruction:
    """`__init__` wiring."""

    def test_output_kind_is_vector(self):
        """FIRMS declares vector output."""
        assert FIRMS.OUTPUT_KIND == "vector"

    def test_space_and_time_captured(self, tmp_path: Path):
        """The bbox and the 'all' temporal sentinel are captured."""
        backend = _make_backend(tmp_path)
        assert isinstance(backend.space, SpatialExtent)
        assert isinstance(backend.time, TemporalExtent)
        assert backend.time.resolution == "all"


@pytest.mark.firms
class TestSearchAndUrl:
    """`_search` chunking and URL shaping."""

    def test_url_carries_bbox_in_wsen_order(self, tmp_path: Path, fake_firms: _FakeFirms):
        """The bbox path segment is W,S,E,N and the sensor + key appear."""
        backend = _make_backend(tmp_path)
        backend.download(progress_bar=False)
        url = fake_firms.calls[0]
        assert "/VIIRS_SNPP_NRT/" in url
        assert "/k/" in url
        assert "-119.0,33.0,-117.0,35.0" in url

    def test_25_day_two_sensor_request_issues_ten_gets(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """ceil(25/5)=5 chunks x 2 sensors == 10 CSV GETs (5-day cap)."""
        backend = _make_backend(
            tmp_path,
            start="2024-08-01",
            end="2024-08-25",
            variables=["VIIRS_SNPP_NRT", "MODIS_NRT"],
        )
        backend.download(progress_bar=False)
        assert len(fake_firms.calls) == 10

    def test_unknown_sensor_raises_did_you_mean(self, tmp_path: Path):
        """A bad sensor code raises before any network call."""
        backend = _make_backend(tmp_path, variables=["VIIRS_SNPP_NR"])
        with pytest.raises(ValueError, match="Did you mean"):
            backend.download(progress_bar=False)


@pytest.mark.firms
class TestFetch:
    """`_fetch` parsing, throttling, and error-body handling."""

    def test_download_returns_featurecollection(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """A CSV body becomes a FeatureCollection written to path."""
        backend = _make_backend(tmp_path)
        result = backend.download(progress_bar=False)
        assert isinstance(result, GeoDataFrame)
        assert result.crs.to_epsg() == 4326
        assert len(result) == 1
        assert list(tmp_path.glob("*.gpkg"))

    def test_quota_429_retries_then_succeeds(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """A 429 once backs off then returns the CSV (no exception)."""
        fake_firms.responses = [
            _FakeResponse("rate limit reached", 429),
            _FakeResponse(VIIRS_CSV),
        ]
        backend = _make_backend(tmp_path)
        result = backend.download(progress_bar=False)
        assert len(result) == 1

    def test_empty_csv_returns_empty_fc(self, tmp_path: Path, fake_firms: _FakeFirms):
        """A header-only CSV yields an empty FeatureCollection, nothing written."""
        fake_firms.text = EMPTY_CSV
        backend = _make_backend(tmp_path)
        result = backend.download(progress_bar=False)
        assert len(result) == 0
        assert not list(tmp_path.glob("*.gpkg"))

    def test_http_500_propagates(self, tmp_path: Path, fake_firms: _FakeFirms):
        """A 5xx status propagates as HTTPError."""
        fake_firms.responses = [_FakeResponse("server error", 500)]
        backend = _make_backend(tmp_path)
        with pytest.raises(requests.HTTPError):
            backend.download(progress_bar=False)

    def test_http_error_does_not_leak_map_key(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """An HTTP error must not echo the MAP_KEY (it rides in the URL)."""
        fake_firms.responses = [_FakeResponse("bad request", 400)]
        backend = _make_backend(tmp_path, map_key="SUPERSECRETKEY123")
        with pytest.raises(requests.HTTPError) as exc:
            backend.download(progress_bar=False)
        assert "SUPERSECRETKEY123" not in str(exc.value)
        assert "MAP_KEY" in str(exc.value)

    def test_invalid_map_key_body_raises_auth(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """An Invalid MAP_KEY 200-body raises AuthenticationError, not a parse error."""
        fake_firms.text = "Invalid MAP_KEY."
        backend = _make_backend(tmp_path)
        with pytest.raises(AuthenticationError, match="MAP_KEY"):
            backend.download(progress_bar=False)

    def test_persistent_quota_body_raises_runtime(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """A persistent quota body raises RuntimeError after the back-off cap."""
        fake_firms.text = "You have exceeded your transaction limit"
        backend = _make_backend(tmp_path)
        backend._max_retries = 2
        with pytest.raises(RuntimeError, match="quota"):
            backend.download(progress_bar=False)

    def test_other_error_body_raises_runtime(
        self, tmp_path: Path, fake_firms: _FakeFirms
    ):
        """A generic non-CSV error body raises RuntimeError quoting the body."""
        fake_firms.text = "Invalid coordinates given"
        backend = _make_backend(tmp_path)
        with pytest.raises(RuntimeError, match="non-CSV"):
            backend.download(progress_bar=False)


@pytest.mark.firms
class TestCoverageWarning:
    """`G5` out-of-coverage warning (warn, do not auto-swap)."""

    def test_old_window_warns_naming_sp_variant(
        self, tmp_path: Path, fake_firms: _FakeFirms, warnings_log: list[str]
    ):
        """A 2019 window on an NRT sensor warns and names the _SP variant."""
        backend = _make_backend(
            tmp_path, start="2019-01-01", end="2019-01-01"
        )
        backend.download(progress_bar=False)
        assert any("VIIRS_SNPP_SP" in msg for msg in warnings_log)


@pytest.mark.firms
class TestAggregateGuard:
    """The vector backend rejects aggregate= directly too."""

    def test_aggregate_rejected(self, tmp_path: Path):
        """download(aggregate=...) raises NotImplementedError."""
        backend = _make_backend(tmp_path)
        with pytest.raises(NotImplementedError, match="not supported"):
            backend.download(aggregate=object())


@pytest.mark.firms
class TestConstructionGuards:
    """`__init__` argument validation."""

    def test_variables_as_dict_raises_type_error(self, tmp_path: Path):
        """A dict `variables` (sensors are a list) raises TypeError."""
        with pytest.raises(TypeError, match="list of sensor codes"):
            FIRMS(
                start="2024-08-01",
                end="2024-08-01",
                variables={"VIIRS_SNPP_NRT": []},
                lat_lim=[33.0, 35.0],
                lon_lim=[-119.0, -117.0],
                path=str(tmp_path),
                map_key="k",
            )

    def test_invalid_file_format_raises_value_error(self, tmp_path: Path):
        """An unsupported output format is rejected."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _make_backend(tmp_path, file_format="shp")


@pytest.mark.firms
class TestWrite:
    """Output-format selection."""

    def test_geojson_format_writes_geojson(self, tmp_path: Path, fake_firms: _FakeFirms):
        """file_format='geojson' writes a .geojson file."""
        backend = _make_backend(tmp_path, file_format="geojson")
        backend.download(progress_bar=False)
        assert list(tmp_path.glob("*.geojson"))
        assert not list(tmp_path.glob("*.gpkg"))


@pytest.mark.firms
class TestCoverageWarningExtra:
    """Further `G5` coverage-warning branches."""

    def test_before_mission_start_warns(
        self, tmp_path: Path, fake_firms: _FakeFirms, warnings_log: list[str]
    ):
        """A window predating the sensor's mission start warns."""
        backend = _make_backend(tmp_path, start="2010-01-01", end="2010-01-01")
        backend.download(progress_bar=False)
        assert any("coverage begins" in msg for msg in warnings_log)

    def test_recent_window_no_coverage_warning(
        self, tmp_path: Path, fake_firms: _FakeFirms, warnings_log: list[str]
    ):
        """A window inside NRT retention emits no out-of-coverage warning."""
        import datetime as dt

        recent = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        backend = _make_backend(tmp_path, start=recent, end=recent)
        backend.download(progress_bar=False)
        assert not any(
            "covers only" in msg or "coverage begins" in msg for msg in warnings_log
        )

    def test_datetime_mission_start_is_normalised(
        self, tmp_path: Path, warnings_log: list[str]
    ):
        """A datetime mission_start is coerced to a date before comparison."""
        import datetime as dt

        from earthlens.firms.catalog import Sensor, Temporal

        backend = _make_backend(tmp_path)
        sensor = Sensor(
            code="VIIRS_SNPP_NRT",
            family="VIIRS",
            resolution_m=375,
            temporal=Temporal(start=dt.datetime(2012, 1, 20), quality="NRT"),
        )
        backend._warn_if_out_of_coverage(
            sensor, dt.date(2010, 1, 1), dt.date(2010, 1, 1)
        )
        assert any("coverage begins 2012-01-20" in msg for msg in warnings_log)

    def test_sp_sensor_skips_nrt_retention_warning(
        self, tmp_path: Path, fake_firms: _FakeFirms, warnings_log: list[str]
    ):
        """An archive (_SP) sensor never emits the NRT-retention warning."""
        backend = _make_backend(
            tmp_path, variables=["VIIRS_SNPP_SP"], start="2019-01-01", end="2019-01-01"
        )
        backend.download(progress_bar=False)
        assert not any("covers only" in msg for msg in warnings_log)

    def test_old_nrt_sensor_without_sp_variant_omits_hint(
        self, tmp_path: Path, fake_firms: _FakeFirms, warnings_log: list[str]
    ):
        """An NRT sensor with no _SP twin warns without naming a variant."""
        backend = _make_backend(
            tmp_path,
            variables=["VIIRS_NOAA21_NRT"],
            start="2019-01-01",
            end="2019-01-01",
        )
        backend.download(progress_bar=False)
        retention = [msg for msg in warnings_log if "covers only" in msg]
        assert retention and not any("_SP" in msg for msg in retention)


@pytest.mark.firms
class TestApiComposition:
    """The canonical `_api` / `_fetch` composition (non-progress path)."""

    def test_api_composes_search_and_fetch(self, tmp_path: Path, fake_firms: _FakeFirms):
        """_api() runs the search/fetch split and returns per-chunk collections."""
        backend = _make_backend(tmp_path)
        collections = backend._api()
        assert len(collections) == 1
        assert len(collections[0]) == 1
