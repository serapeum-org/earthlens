"""Facade-routing tests for the IUCN backend (`EarthLens` -> `IUCN`)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import earthlens.iucn
from earthlens.earthlens import EarthLens


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the IUCN backend."""
    params: dict[str, object] = dict(
        variables=["country:KE"],
        data_source="iucn",
        start="2024-01-01",
        end="2024-12-31",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
        token="test-token",
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.iucn
class TestFacadeRouting:
    """The `iucn` key (and `redlist` alias) resolves to the backend."""

    def test_key_registered(self):
        """`iucn` is among the registered data sources."""
        assert "iucn" in EarthLens.DataSources

    def test_key_resolves_to_iucn_class(self):
        """The `iucn` key resolves to `earthlens.iucn.IUCN`."""
        assert EarthLens.DataSources["iucn"] is earthlens.iucn.IUCN

    def test_alias_resolves(self):
        """The `redlist` alias resolves to the same class."""
        assert EarthLens.DataSources["redlist"] is earthlens.iucn.IUCN

    def test_facade_builds_iucn_backend(self, tmp_path: Path):
        """The facade binds an IUCN instance as its datasource."""
        assert isinstance(_make_facade(tmp_path).datasource, earthlens.iucn.IUCN)


@pytest.mark.iucn
class TestFacadeDownload:
    """The facade rejects `aggregate=` and returns the DataFrame."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path):
        """`download(aggregate=...)` on the tabular backend raises."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _make_facade(tmp_path).download(aggregate=object())

    def test_download_returns_dataframe(self, tmp_path: Path, fake_iucn):
        """A facade download returns the assessment DataFrame."""
        fake_iucn.state.route("countries/KE", {"assessments": []})
        frame = _make_facade(tmp_path).download()
        assert isinstance(frame, pd.DataFrame)
