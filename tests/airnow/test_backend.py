"""Tests for the `AirNow` backend request shaping and output."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from earthlens.airnow import AirNow
from earthlens.airnow.auth import AuthenticationError
from tests.airnow.conftest import _FakeAirnow, _FakeSession, _observation


def _backend(state: _FakeAirnow, tmp_path: Path, **overrides) -> AirNow:
    """Build an AirNow backend wired to the recording fake session."""
    params = dict(
        start="2026-01-01",
        end="2026-01-01",
        variables=["pm25", "o3"],
        lat_lim=[33.0, 35.0],
        lon_lim=[-119.0, -117.0],
        api_key="k",
        session=_FakeSession(state),
        path=str(tmp_path),
    )
    params.update(overrides)
    return AirNow(**params)


@pytest.mark.airnow
class TestRequestShaping:
    """The `/aq/data/` query arguments derived from the request."""

    def test_bbox_and_parameters(self, tmp_path, fake_airnow):
        """BBOX is `minLon,minLat,maxLon,maxLat`; parameters are the codes."""
        state = _FakeAirnow()
        _backend(state, tmp_path).download(progress_bar=False)
        params = state.calls[0]
        assert params["BBOX"] == "-119.0,33.0,-117.0,35.0"
        assert params["parameters"] == "PM25,OZONE"

    def test_date_bounds_extend_to_end_of_day(self, tmp_path, fake_airnow):
        """A date-granular window runs `T00` to `T23`."""
        state = _FakeAirnow()
        _backend(state, tmp_path).download(progress_bar=False)
        assert state.calls[0]["startDate"] == "2026-01-01T00"
        assert state.calls[0]["endDate"] == "2026-01-01T23"

    def test_data_type_and_monitor_type(self, tmp_path, fake_airnow):
        """`dataType` / `monitorType` reflect the constructor arguments."""
        state = _FakeAirnow()
        _backend(state, tmp_path, data_type="C", monitor_type="mobile").download(
            progress_bar=False
        )
        assert state.calls[0]["dataType"] == "C"
        assert state.calls[0]["monitorType"] == 1


@pytest.mark.airnow
class TestConstructorGuards:
    """Constructor validation paths."""

    def test_variables_mapping_rejected(self, tmp_path):
        """A mapping `variables` is a `TypeError`."""
        with pytest.raises(TypeError):
            AirNow(
                start="2026-01-01",
                end="2026-01-01",
                variables={"pm25": 1},
                lat_lim=[33, 35],
                lon_lim=[-119, -117],
                api_key="k",
                path=str(tmp_path),
            )

    def test_bad_monitor_type_rejected(self, tmp_path):
        """An unknown `monitor_type` is a `ValueError`."""
        with pytest.raises(ValueError, match="monitor_type"):
            AirNow(
                start="2026-01-01",
                end="2026-01-01",
                variables=["pm25"],
                lat_lim=[33, 35],
                lon_lim=[-119, -117],
                api_key="k",
                monitor_type="satellite",
                path=str(tmp_path),
            )

    def test_bad_resolution_rejected(self, tmp_path):
        """An unaccepted `temporal_resolution` is a `ValueError`."""
        with pytest.raises(ValueError, match="temporal_resolution"):
            AirNow(
                start="2026-01-01",
                end="2026-01-01",
                variables=["pm25"],
                lat_lim=[33, 35],
                lon_lim=[-119, -117],
                api_key="k",
                temporal_resolution="monthly",
                path=str(tmp_path),
            )

    def test_missing_key_raises(self, tmp_path, monkeypatch):
        """No key anywhere raises `AuthenticationError` at construction."""
        monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
        with pytest.raises(AuthenticationError):
            AirNow(
                start="2026-01-01",
                end="2026-01-01",
                variables=["pm25"],
                lat_lim=[33, 35],
                lon_lim=[-119, -117],
                path=str(tmp_path),
            )

    def test_empty_variables_defaults_pm25(self, tmp_path, fake_airnow):
        """An empty `variables` defaults to `["pm25"]`."""
        backend = _backend(_FakeAirnow(), tmp_path, variables=[])
        assert backend.vars == ["pm25"]


@pytest.mark.airnow
class TestOutputFrame:
    """The returned long-format DataFrame."""

    def test_schema_and_values(self, tmp_path, fake_airnow):
        """The frame carries the schema columns with tz-aware datetimes."""
        state = _FakeAirnow(rows=[_observation()])
        df = _backend(state, tmp_path).download(progress_bar=False)
        assert list(df.columns) == [
            "station_id", "parameter", "datetime_utc", "value", "raw_value",
            "units", "aqi", "category", "lat", "lon", "site_name", "provider",
        ]
        assert str(df["datetime_utc"].dtype) == "datetime64[ns, UTC]"
        assert df.loc[0, "value"] == 12.3

    def test_raw_value_from_raw_concentration(self, tmp_path, fake_airnow):
        """The `raw_value` column carries AirNow's `RawConcentration`."""
        row = _observation(concentration=12.3)
        row["RawConcentration"] = 11.0
        df = _backend(_FakeAirnow(rows=[row]), tmp_path).download(progress_bar=False)
        assert df.loc[0, "raw_value"] == 11.0

    def test_reads_value_field(self, tmp_path, fake_airnow):
        """The concentration is read from AirNow's `Value` field."""
        state = _FakeAirnow(rows=[_observation(concentration=9.9)])
        df = _backend(state, tmp_path).download(progress_bar=False)
        assert df.loc[0, "value"] == 9.9

    def test_concentration_field_fallback(self, tmp_path, fake_airnow):
        """A row spelling the value `Concentration` is still read."""
        row = _observation(concentration=7.5)
        del row["Value"]
        row["Concentration"] = 7.5
        df = _backend(_FakeAirnow(rows=[row]), tmp_path).download(progress_bar=False)
        assert df.loc[0, "value"] == 7.5

    def test_missing_sentinel_scrubbed(self, tmp_path, fake_airnow):
        """AirNow's -999 sentinel becomes NaN for value/aqi/category."""
        state = _FakeAirnow(
            rows=[_observation(concentration=-999, aqi=-999, category=-999)]
        )
        df = _backend(state, tmp_path).download(progress_bar=False)
        assert pd.isna(df.loc[0, "value"])
        assert pd.isna(df.loc[0, "aqi"])

    def test_empty_result_is_schema_only(self, tmp_path, fake_airnow):
        """No rows returns a zero-row frame with the full schema."""
        df = _backend(_FakeAirnow(rows=[]), tmp_path).download(progress_bar=False)
        assert df.empty and "station_id" in df.columns

    def test_writes_csv(self, tmp_path, fake_airnow):
        """The default download writes a CSV under the output dir."""
        _backend(_FakeAirnow(), tmp_path).download(progress_bar=False)
        assert list(tmp_path.glob("airnow_*.csv"))

    def test_writes_parquet(self, tmp_path, fake_airnow):
        """`file_format='parquet'` writes a Parquet file."""
        _backend(_FakeAirnow(), tmp_path, file_format="parquet").download(
            progress_bar=False
        )
        assert list(tmp_path.glob("airnow_*.parquet"))


@pytest.mark.airnow
class TestAggregateRejection:
    """A tabular backend rejects `aggregate=`."""

    def test_aggregate_raises(self, tmp_path, fake_airnow):
        """`download(aggregate=...)` raises `NotImplementedError`."""
        backend = _backend(_FakeAirnow(), tmp_path)
        with pytest.raises(NotImplementedError, match="tabular"):
            backend.download(aggregate=object())
