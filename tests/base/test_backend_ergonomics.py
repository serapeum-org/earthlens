from __future__ import annotations

import inspect

import pytest

from earthlens.chc import CHIRPS


class TestBackendDirectErgonomics:
    """The aoi= / cadence= / dataset= sugar works on backend classes directly."""

    def _chc(self, tmp_path, **kwargs):
        return CHIRPS(
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
            **kwargs,
        )

    def test_aoi_on_backend_sets_extent(self, tmp_path):
        """A direct CHIRPS(aoi=...) reduces to the right SpatialExtent."""
        space = self._chc(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64]).space
        assert (space.south, space.north, space.west, space.east) == (
            4.19,
            4.64,
            -75.65,
            -74.73,
        )

    def test_aoi_matches_lat_lon_pairs(self, tmp_path):
        """aoi= and the lat_lim/lon_lim pair yield the same extent."""
        via_aoi = self._chc(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64]).space
        via_pairs = self._chc(
            tmp_path, lat_lim=[4.19, 4.64], lon_lim=[-75.65, -74.73]
        ).space
        assert via_aoi == via_pairs

    def test_point_aoi_with_buffer(self, tmp_path):
        """A point aoi with buffer builds a square extent on the backend."""
        space = self._chc(tmp_path, aoi=(-75.0, 4.0), buffer=0.25).space
        assert (space.south, space.north, space.west, space.east) == (
            3.75,
            4.25,
            -75.25,
            -74.75,
        )

    def test_cadence_on_backend(self, tmp_path):
        """cadence= overrides temporal_resolution on the backend."""
        backend = self._chc(tmp_path, cadence="monthly")
        assert backend.temporal_resolution == "monthly"

    def test_aoi_with_lat_lim_raises(self, tmp_path):
        """Both aoi= and lat_lim= on the backend is rejected."""
        with pytest.raises(ValueError, match="either aoi= or lat_lim"):
            self._chc(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64], lat_lim=[4, 5])

    def test_buffer_without_aoi_raises(self, tmp_path):
        """buffer= without a point aoi= on the backend is rejected."""
        with pytest.raises(ValueError, match="buffer= only applies"):
            self._chc(tmp_path, buffer=0.5)

    def test_signature_introspection_preserved(self):
        """The wrapper keeps the backend's real signature for introspection."""
        params = inspect.signature(CHIRPS.__init__).parameters
        assert "variables" in params and "lat_lim" in params
        assert not any(
            p.kind == p.VAR_KEYWORD for p in params.values()
        ), "wrapped __init__ must still expose the real (no **kwargs) signature"


@pytest.mark.ecmwf
class TestBackendDirectDatasetSplit:
    """dataset= splits into the keyed variables dict on a dataset-keyed backend."""

    def test_dataset_composes_variables_dict(self, tmp_path):
        """ECMWF(dataset=, variables=[...]) composes the {dataset: [...]} dict."""
        from earthlens.ecmwf import ECMWF

        backend = ECMWF(
            dataset="reanalysis-era5-single-levels",
            variables=["2m-temperature"],
            cadence="monthly",
            start="2022-01-01",
            end="2022-02-01",
            aoi=[-75.0, 4.0, -74.0, 5.0],
            path=str(tmp_path),
        )
        assert backend.vars == {"reanalysis-era5-single-levels": ["2m-temperature"]}
        assert backend.temporal_resolution == "monthly"
        assert (backend.space.west, backend.space.east) == (-75.0, -74.0)

    def test_dataset_with_dict_variables_raises(self, tmp_path):
        """dataset= with a dict variables on the backend is rejected."""
        from earthlens.ecmwf import ECMWF

        with pytest.raises(ValueError, match="pass variables= as a list"):
            ECMWF(
                dataset="reanalysis-era5-single-levels",
                variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
                start="2022-01-01",
                end="2022-02-01",
                lat_lim=[4.0, 5.0],
                lon_lim=[-75.0, -74.0],
                path=str(tmp_path),
            )


class TestNativeAoiBackendUnaffected:
    """A backend with its own aoi= (WorldPop) keeps interpreting it."""

    def test_worldpop_native_aoi(self, tmp_path):
        """WorldPop(aoi='COM') passes the ISO3 through, not normalize_aoi."""
        pytest.importorskip("earthlens.worldpop")
        from earthlens.worldpop import WorldPop

        backend = WorldPop(
            variables=["pop"],
            start="2020",
            end="2020",
            fmt="%Y",
            aoi="COM",
            lat_lim=[-90, 90],
            lon_lim=[-180, 180],
            path=str(tmp_path),
        )
        assert backend is not None
