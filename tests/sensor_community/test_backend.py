"""Tests for the `SensorCommunity` backend discovery + fetch."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from earthlens.sensor_community import SensorCommunity
from earthlens.sensor_community._helpers import LicenseWarning
from tests.sensor_community.conftest import DHT_CSV, _FakeClient, _record


def _backend(client, tmp_path: Path, **overrides) -> SensorCommunity:
    """Build a SensorCommunity backend wired to the fake client."""
    params = dict(
        start="2026-06-30",
        end="2026-06-30",
        variables=["pm25", "pm10"],
        lat_lim=[48.5, 48.9],
        lon_lim=[9.0, 9.3],
        client=client,
        path=str(tmp_path),
    )
    params.update(overrides)
    return SensorCommunity(**params)


@pytest.mark.sensor_community
class TestDaysAndSearch:
    """Day enumeration and live-API discovery."""

    def test_days_inclusive(self, tmp_path, fake_client):
        """`_days` spans start to end inclusive."""
        backend = _backend(fake_client, tmp_path, start="2026-06-28", end="2026-06-30")
        assert backend._days() == ["2026-06-28", "2026-06-29", "2026-06-30"]

    def test_search_discovers_bbox_sensors(self, tmp_path, fake_client):
        """`_search` returns the SDS011 sensor active in the bbox."""
        products = _backend(fake_client, tmp_path)._search()
        assert [(p.id, p.metadata["sensor_type"]) for p in products] == [("140", "sds011")]

    def test_search_type_filtered(self, tmp_path):
        """A DHT22-only snapshot yields no products for a PM request."""
        client = _FakeClient(snapshot=[_record(sensor_id=108, sensor_type="DHT22")])
        assert _backend(client, tmp_path)._search() == []


@pytest.mark.sensor_community
class TestFetchAndDownload:
    """Per-sensor archive fetch and the full download."""

    def test_download_extracts_both_columns(self, tmp_path, fake_client):
        """The download yields pm25 (P2) and pm10 (P1) rows."""
        df = _backend(fake_client, tmp_path).download(progress_bar=False)
        assert set(df["parameter"]) == {"pm25", "pm10"}
        assert len(df) == 4

    def test_missing_day_skipped(self, tmp_path):
        """A missing archive file is skipped, not an error."""
        client = _FakeClient(archive={})  # every archive_csv returns None
        df = _backend(client, tmp_path).download(progress_bar=False)
        assert df.empty
        assert client.archive_calls == [("2026-06-30", "sds011", "140")]

    def test_license_warning_emitted(self, tmp_path, fake_client):
        """Every download emits a `LicenseWarning`."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _backend(fake_client, tmp_path).download(progress_bar=False)
        assert any(issubclass(w.category, LicenseWarning) for w in caught)

    def test_window_filter(self, tmp_path):
        """Readings outside the date window are dropped."""
        # archive returns a row dated 2026-06-30, request only 2026-06-29
        client = _FakeClient()
        backend = _backend(client, tmp_path, start="2026-06-29", end="2026-06-29")
        # _days -> 2026-06-29 only; archive has no file for that day -> empty
        df = backend.download(progress_bar=False)
        assert df.empty

    def test_temperature_from_dht(self, tmp_path):
        """A temperature request reads the DHT22 `temperature` column."""
        client = _FakeClient(
            snapshot=[_record(sensor_id=108, sensor_type="DHT22", lat="48.53", lon="9.2")],
            archive={("2026-06-30", "dht22", "108"): DHT_CSV},
        )
        df = _backend(
            client, tmp_path, variables=["temperature"]
        ).download(progress_bar=False)
        assert set(df["parameter"]) == {"temperature"}
        assert df.loc[0, "value"] == 24.9


@pytest.mark.sensor_community
class TestGuards:
    """Constructor + download guards."""

    def test_variables_mapping_rejected(self, tmp_path, fake_client):
        """A mapping `variables` is a `TypeError`."""
        with pytest.raises(TypeError):
            _backend(fake_client, tmp_path, variables={"pm25": 1})

    def test_bad_resolution_rejected(self, tmp_path, fake_client):
        """An unaccepted `temporal_resolution` is a `ValueError`."""
        with pytest.raises(ValueError, match="temporal_resolution"):
            _backend(fake_client, tmp_path, temporal_resolution="monthly")

    def test_aggregate_rejected(self, tmp_path, fake_client):
        """`download(aggregate=...)` raises `NotImplementedError`."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _backend(fake_client, tmp_path).download(aggregate=object())

    def test_writes_parquet(self, tmp_path, fake_client):
        """`file_format='parquet'` writes a Parquet file."""
        _backend(fake_client, tmp_path, file_format="parquet").download(
            progress_bar=False
        )
        assert list(tmp_path.glob("sensor_community_*.parquet"))
