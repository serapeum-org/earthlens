"""Integration tests for the Earthdata backend through EarthLens."""

from __future__ import annotations

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.earthdata import EarthData
from earthlens.earthlens import EarthLens

pytestmark = [pytest.mark.earthdata, pytest.mark.integration]


def _facade(tmp_path, variables, **kwargs):
    """Build an EarthLens facade bound to the earthdata backend."""
    return EarthLens(
        data_source="earthdata",
        start="2020-06-01",
        end="2020-06-02",
        variables=variables,
        lat_lim=[10.0, 20.0],
        lon_lim=[30.0, 40.0],
        path=tmp_path,
        **kwargs,
    )


class TestRouting:
    """The facade resolves the earthdata key."""

    def test_key_registered(self):
        """`earthdata` is a registered DataSources key."""
        assert "earthdata" in EarthLens.DataSources

    def test_resolves_to_earthdata_class(self):
        """The key resolves to earthlens.earthdata.EarthData."""
        assert EarthLens.DataSources["earthdata"] is EarthData

    def test_facade_builds_backend(self, fake_earthaccess, edl_env, tmp_path):
        """The facade constructs an EarthData datasource."""
        fac = _facade(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        assert isinstance(fac.datasource, EarthData)


class TestPerInstanceAggregateGuard:
    """The facade gates aggregate= on the per-instance OUTPUT_KIND (G1/G6)."""

    def test_raster_forwards_aggregate(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A raster instance forwards aggregate= into the backend (stack path)."""
        import pyramids.dataset as dsmod

        from .test_backend import _FakeDatasetCollection

        monkeypatch.setattr(dsmod, "DatasetCollection", _FakeDatasetCollection)
        fac = _facade(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        out = fac.download(aggregate=AggregationConfig(freq="1MS"))
        assert out, "raster aggregate should produce reduced paths"

    def test_vector_rejects_aggregate(self, fake_earthaccess, edl_env, tmp_path):
        """A vector instance rejects aggregate= at the facade."""
        fac = _facade(tmp_path, {"ATL08_006": ["h_canopy"]})
        with pytest.raises(NotImplementedError, match="vector"):
            fac.download(aggregate=AggregationConfig(freq="1MS"))

    def test_vector_download_without_aggregate_ok(
        self, fake_earthaccess, edl_env, tmp_path
    ):
        """A vector instance downloads fine without aggregate=."""
        fac = _facade(tmp_path, {"ATL08_006": ["h_canopy"]})
        paths = fac.download()
        assert isinstance(paths, list)
