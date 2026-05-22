"""Unit tests for `earthlens.openaq.backend` (search / fetch / download)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from earthlens.openaq import OpenAQ
from earthlens.openaq.backend import (
    _SCHEMA,
    _empty_frame,
    _measurement_datetime,
    _measurement_row,
)
from earthlens.base import RemoteProduct

from .conftest import _FakeOpenaq, _location, _measurement, _sensor

_BBOX_LAT = [34.0, 34.3]
_BBOX_LON = [-118.5, -118.1]


def _backend(tmp_path: Path, **overrides: Any) -> OpenAQ:
    """Build an OpenAQ backend bound to a temp output dir with a test key."""
    params: dict[str, Any] = dict(
        start="2024-01-01",
        end="2024-01-07",
        variables=["pm25"],
        lat_lim=_BBOX_LAT,
        lon_lim=_BBOX_LON,
        path=str(tmp_path),
        api_key="k",
    )
    params.update(overrides)
    return OpenAQ(**params)


@pytest.mark.openaq
class TestConstruction:
    """Constructor behaviour and the abstract-hook wiring."""

    def test_output_kind_is_tabular(self):
        """The backend declares the tabular output kind."""
        assert OpenAQ.OUTPUT_KIND == "tabular"

    def test_variables_dict_rejected(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """A mapping for variables is rejected with a clear TypeError."""
        with pytest.raises(TypeError, match="list of pollutant names"):
            _backend(tmp_path, variables={"pm25": []})

    def test_empty_variables_defaults_to_pm25(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """An empty variables list defaults to ['pm25']."""
        backend = _backend(tmp_path, variables=[])
        assert backend.vars == ["pm25"]

    def test_invalid_resolution_raises(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """An unknown temporal_resolution raises ValueError."""
        with pytest.raises(ValueError, match="temporal_resolution must be"):
            _backend(tmp_path, temporal_resolution="weekly")

    @pytest.mark.parametrize(
        "resolution, rollup",
        [
            ("hourly", "hours"),
            ("daily", "days"),
            ("monthly", "months"),
            ("yearly", "years"),
            ("all", None),
            ("raw", None),
        ],
    )
    def test_rollup_mapping(
        self,
        tmp_path: Path,
        fake_openaq: _FakeOpenaq,
        resolution: str,
        rollup: str | None,
    ):
        """Each temporal_resolution maps to its rollup endpoint segment."""
        backend = _backend(tmp_path, temporal_resolution=resolution)
        assert backend._rollup == rollup

    def test_bbox_string(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """The bbox renders as west,south,east,north."""
        backend = _backend(tmp_path)
        assert backend._bbox() == "-118.5,34.0,-118.1,34.3"


@pytest.mark.openaq
class TestSearch:
    """_search enumerates products from locations + sensors."""

    def test_one_product_per_matching_sensor(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """Only sensors whose parameter was requested become products."""
        fake_openaq.locations = {
            "results": [
                _location(
                    sensors=[
                        _sensor(sensor_id=10, param_id=2, name="pm25"),
                        _sensor(sensor_id=11, param_id=999, name="other"),
                    ]
                )
            ]
        }
        products = _backend(tmp_path)._search()
        assert [p.id for p in products] == ["10"]

    def test_product_metadata(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """A product carries station/parameter/units/lat/lon/provider."""
        product = _backend(tmp_path)._search()[0]
        assert product.metadata == {
            "station_id": 1,
            "parameter": "pm25",
            "units": "µg/m³",
            "lat": 34.1,
            "lon": -118.2,
            "provider": "AirNow",
        }

    def test_provider_defaults_when_absent(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """A location with no provider falls back to 'openaq'."""
        fake_openaq.locations = {"results": [_location(provider=None)]}
        product = _backend(tmp_path)._search()[0]
        assert product.metadata["provider"] == "openaq"

    def test_parameters_id_forwarded(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """variables=['pm25','no2'] forwards parameters_id=[2,15] to locations."""
        _backend(tmp_path, variables=["pm25", "no2"])._search()
        assert fake_openaq.location_calls()[0]["parameters_id"] == [2, 15]

    def test_max_sensors_per_location_caps(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """max_sensors_per_location truncates the per-location sensor list."""
        fake_openaq.locations = {
            "results": [
                _location(
                    sensors=[
                        _sensor(sensor_id=10, param_id=2, name="pm25"),
                        _sensor(sensor_id=12, param_id=2, name="pm25"),
                    ]
                )
            ]
        }
        products = _backend(tmp_path, max_sensors_per_location=1)._search()
        assert [p.id for p in products] == ["10"]

    def test_truncation_warns(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq, log_messages: list[str]
    ):
        """Hitting max_locations logs a partial-result warning."""
        fake_openaq.locations = {"results": [_location(station_id=1), _location(station_id=2)]}
        _backend(tmp_path, max_locations=2)._search()
        assert any("max_locations" in message for message in log_messages)


@pytest.mark.openaq
class TestFetch:
    """_fetch / _fetch_one shape measurements into the schema."""

    def test_schema_and_dtypes(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """A fetched frame has the schema columns and tz-aware UTC datetime."""
        backend = _backend(tmp_path)
        product = backend._search()[0]
        frame = backend._fetch_one(product)
        assert list(frame.columns) == list(_SCHEMA)
        assert str(frame["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert frame["value"].iloc[0] == 12.3

    def test_date_window_forwarded_daily(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """The default daily rollup carries a calendar date_from/date_to window."""
        backend = _backend(tmp_path)  # temporal_resolution defaults to "daily" -> /days
        backend._fetch_one(backend._search()[0])
        _url, params = fake_openaq.measurement_calls()[0]
        assert params["date_from"] == "2024-01-01"
        assert params["date_to"] == "2024-01-07"

    def test_date_window_forwarded_raw(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """Raw measurements carry a full datetime_from/datetime_to window."""
        backend = _backend(tmp_path, temporal_resolution="all")
        backend._fetch_one(backend._search()[0])
        _url, params = fake_openaq.measurement_calls()[0]
        assert params["datetime_from"] == "2024-01-01T00:00:00"
        assert params["datetime_to"] == "2024-01-07T00:00:00"

    def test_rollup_endpoint_used(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """A daily request fetches from the /days rollup endpoint."""
        backend = _backend(tmp_path, temporal_resolution="daily")
        backend._fetch_one(backend._search()[0])
        url, _params = fake_openaq.measurement_calls()[0]
        assert url.endswith("/sensors/10/days")

    def test_empty_sensor_returns_schema_frame(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """A sensor with no measurements yields a schema-only frame."""
        fake_openaq.measurements = {"results": []}
        backend = _backend(tmp_path)
        frame = backend._fetch_one(backend._search()[0])
        assert frame.empty
        assert list(frame.columns) == list(_SCHEMA)

    def test_fetch_list(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """_fetch returns one frame per product."""
        backend = _backend(tmp_path)
        products = backend._search()
        frames = backend._fetch(products)
        assert len(frames) == len(products)

    def test_429_then_success(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """A single 429 on the measurement call is retried, then succeeds."""
        fake_openaq.n_429 = 1
        backend = _backend(tmp_path)
        frame = backend._fetch_one(backend._search()[0])
        assert len(frame) == 1


@pytest.mark.openaq
class TestDownload:
    """download concatenates frames, writes the file, returns the frame."""

    def test_happy_path_returns_and_writes(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """A normal download returns rows and writes a CSV that round-trips."""
        backend = _backend(tmp_path)
        df = backend.download(progress_bar=False)
        assert len(df) == 1
        written = list(tmp_path.glob("*.csv"))
        assert len(written) == 1
        round_trip = pd.read_csv(written[0])
        assert list(round_trip.columns) == list(_SCHEMA)

    def test_empty_writes_schema_only(
        self, tmp_path: Path, fake_openaq: _FakeOpenaq
    ):
        """No locations -> empty schema-only frame, still written to disk."""
        fake_openaq.locations = {"results": []}
        backend = _backend(tmp_path)
        df = backend.download(progress_bar=False)
        assert df.empty
        assert list(df.columns) == list(_SCHEMA)
        assert len(list(tmp_path.glob("*.csv"))) == 1

    def test_aggregate_rejected(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """A non-None aggregate is rejected for the tabular backend."""
        backend = _backend(tmp_path)
        with pytest.raises(NotImplementedError, match="tabular"):
            backend.download(aggregate=object())

    def test_parquet_output(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """file_format='parquet' writes a .parquet file."""
        pytest.importorskip("pyarrow")
        backend = _backend(tmp_path, file_format="parquet")
        backend.download(progress_bar=False)
        assert len(list(tmp_path.glob("*.parquet"))) == 1

    def test_api_composition(self, tmp_path: Path, fake_openaq: _FakeOpenaq):
        """_api composes search+fetch and returns one frame per product."""
        backend = _backend(tmp_path)
        frames = backend._api()
        assert all(isinstance(frame, pd.DataFrame) for frame in frames)


@pytest.mark.openaq
class TestHelpers:
    """Module-level schema and row helpers."""

    def test_empty_frame_schema(self):
        """_empty_frame has exactly the schema columns and dtypes."""
        frame = _empty_frame()
        assert list(frame.columns) == list(_SCHEMA)
        assert frame.empty

    @pytest.mark.parametrize(
        "measurement, expected",
        [
            ({"period": {"datetimeFrom": {"utc": "2024-01-01T00:00:00Z"}}}, "2024-01-01T00:00:00Z"),
            ({"datetime": {"utc": "2024-02-02T00:00:00Z"}}, "2024-02-02T00:00:00Z"),
            ({"date": {"utc": "2024-03-03T00:00:00Z"}}, "2024-03-03T00:00:00Z"),
            ({}, None),
        ],
    )
    def test_measurement_datetime_fallbacks(
        self, measurement: dict[str, Any], expected: str | None
    ):
        """The timestamp is read from period/datetime/date, else None."""
        assert _measurement_datetime(measurement) == expected

    def test_measurement_row_uses_product_metadata(self):
        """A row pulls station fields from metadata and value from the reading."""
        product = RemoteProduct(
            id="10",
            metadata={
                "station_id": 7,
                "parameter": "pm25",
                "units": "µg/m³",
                "lat": 1.0,
                "lon": 2.0,
                "provider": "p",
            },
        )
        row = _measurement_row(product, _measurement(value=5.5))
        assert row["station_id"] == 7
        assert row["value"] == 5.5
        assert row["provider"] == "p"
