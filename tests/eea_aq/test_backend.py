"""Tests for the `EEA_AQ` backend request shaping and output."""

from __future__ import annotations

import builtins
from pathlib import Path

import pandas as pd
import pytest

from earthlens.eea_aq import EEA_AQ
from tests.eea_aq.conftest import _FakeAirbaseClient


def _backend(client, tmp_path: Path, **overrides) -> EEA_AQ:
    """Build an EEA backend wired to the fake airbase client."""
    params = dict(
        start="2023-06-01",
        end="2023-06-30",
        variables=["pm25"],
        lat_lim=[35.7, 36.1],
        lon_lim=[14.1, 14.6],
        country="MT",
        client=client,
        path=str(tmp_path),
    )
    params.update(overrides)
    return EEA_AQ(**params)


@pytest.mark.eea
class TestCountryResolution:
    """Resolving the request to reporting countries."""

    def test_explicit_country_upper(self, tmp_path, fake_client):
        """An explicit lower-case country is upper-cased."""
        backend = _backend(fake_client, tmp_path, country=["mt", "de"])
        assert backend._resolve_countries() == ["MT", "DE"]

    def test_bbox_derived(self, tmp_path, fake_client):
        """Without `country=`, countries come from the bbox."""
        backend = _backend(fake_client, tmp_path, country=None)
        assert backend._resolve_countries() == ["MT"]


@pytest.mark.eea
class TestApi:
    """The download / reshape / window pipeline."""

    def test_returns_requested_pollutant_only(self, tmp_path, fake_client):
        """Only the requested pm25 rows come back (pm10/o3 dropped)."""
        df = _backend(fake_client, tmp_path).download(progress_bar=False)
        assert set(df["parameter"]) == {"pm25"}

    def test_windows_out_of_range_rows(self, tmp_path, fake_client):
        """The 2024 fixture row is dropped by the 2023 window."""
        df = _backend(fake_client, tmp_path).download(progress_bar=False)
        assert (df["datetime_utc"].dt.year == 2023).all()

    def test_request_uses_unverified_dataset(self, tmp_path, fake_client):
        """A 2023 window requests the Unverified dataset for MT / PM2.5."""
        _backend(fake_client, tmp_path).download(progress_bar=False)
        assert fake_client.calls == [("Unverified", ("MT",), ["PM2.5"])]

    def test_multi_dataset_range(self, tmp_path, fake_client):
        """A range straddling the boundary requests two datasets."""
        _backend(
            fake_client, tmp_path, start="2021-06-01", end="2024-06-30"
        ).download(progress_bar=False)
        sources = [call[0] for call in fake_client.calls]
        assert sources == ["Verified", "Unverified"]

    def test_no_country_returns_empty(self, tmp_path, fake_client):
        """A bbox intersecting no country returns a schema-only frame."""
        backend = _backend(
            fake_client, tmp_path, country=None, lat_lim=[10.0, 11.0], lon_lim=[-40.0, -39.0]
        )
        df = backend.download(progress_bar=False)
        assert df.empty and "country" in df.columns
        assert fake_client.calls == []

    def test_no_parquet_returns_empty(self, tmp_path):
        """A dataset that yields no Parquet returns a schema-only frame."""

        class _EmptyReq:
            def download(self, dir, skip_existing=True, raise_for_status=True):
                return None

        class _EmptyClient:
            calls: list = []

            def request(self, source, *countries, poll=None, verbose=True):
                return _EmptyReq()

        df = _backend(_EmptyClient(), tmp_path).download(progress_bar=False)
        assert df.empty


@pytest.mark.eea
class TestGuards:
    """Constructor + download guards."""

    def test_variables_mapping_rejected(self, tmp_path, fake_client):
        """A mapping `variables` is a `TypeError`."""
        with pytest.raises(TypeError):
            _backend(fake_client, tmp_path, variables={"pm25": 1})

    def test_bad_resolution_rejected(self, tmp_path, fake_client):
        """An unaccepted `temporal_resolution` is a `ValueError`."""
        with pytest.raises(ValueError, match="temporal_resolution"):
            _backend(fake_client, tmp_path, temporal_resolution="weekly")

    def test_aggregate_rejected(self, tmp_path, fake_client):
        """`download(aggregate=...)` raises `NotImplementedError`."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _backend(fake_client, tmp_path).download(aggregate=object())

    def test_writes_parquet(self, tmp_path, fake_client):
        """`file_format='parquet'` writes a Parquet file."""
        _backend(fake_client, tmp_path, file_format="parquet").download(
            progress_bar=False
        )
        assert list(tmp_path.glob("eea_aq_*.parquet"))


@pytest.mark.eea
def test_missing_airbase_raises(tmp_path, monkeypatch):
    """With no client and airbase absent, `_airbase_client` raises ImportError."""
    backend = EEA_AQ(
        start="2023-06-01",
        end="2023-06-30",
        variables=["pm25"],
        lat_lim=[35.7, 36.1],
        lon_lim=[14.1, 14.6],
        country="MT",
        path=str(tmp_path),
    )
    real_import = builtins.__import__

    def _no_airbase(name, *args, **kwargs):
        if name == "airbase":
            raise ImportError("no airbase")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_airbase)
    with pytest.raises(ImportError, match="eea_aq"):
        backend._airbase_client()
