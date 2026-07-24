"""Live end-to-end tests for the GHSL backend.

Hits the real, public, anonymous JRC GHSL HTTPS file tree
(`jeodpp.jrc.ec.europa.eu`), so these tests are gated only behind the `e2e`
pytest marker plus network availability — no credentials are needed (GHSL is
open, attribution-only). A default `pytest` invocation skips them.

Run with:

    pixi run -e dev pytest -m "e2e and ghsl" tests/ghsl
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.earthlens import EarthLens

#: A tiny Moroccan-coast AOI inside the verified R6_C18 tile — small enough to
#: fetch one 100 m tile in seconds and reliably over land.
_LAT_LIM = [30.5, 30.8]
_LON_LIM = [-9.0, -8.7]


def _nonempty_geotiff(path: Path) -> bool:
    """Return whether `path` reads back as a non-empty pyramids raster."""
    from pyramids.dataset import Dataset

    dataset = Dataset.read_file(str(path))
    return dataset.rows > 0 and dataset.columns > 0


@pytest.mark.e2e
@pytest.mark.ghsl
class TestGhslLiveFetch:
    """Live GHSL fetches (open HTTPS — no credentials needed)."""

    def test_population_100m_lands_cropped_geotiff(self, tmp_path: Path):
        """A small GHS-POP 2020 100 m pull lands one cropped EPSG:4326 GeoTIFF."""
        paths = EarthLens(
            data_source="ghsl",
            variables=["GHS_POP"],
            start="2020-01-01",
            end="2020-12-31",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        assert _nonempty_geotiff(paths[0]), "the population raster must be non-empty"

    # SMOD has no tiled resolution, so this pulls the whole-globe file
    # (~300 MB+); marked `slow` so a routine e2e run can drop it with
    # `-m "e2e and ghsl and not slow"`.
    @pytest.mark.slow
    def test_smod_categorical_writes_legend_sidecar(self, tmp_path: Path):
        """A GHS-SMOD 2020 pull lands a categorical raster + a legend sidecar."""
        paths = EarthLens(
            data_source="ghsl",
            variables=["GHS_SMOD"],
            start="2020-01-01",
            end="2020-12-31",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            resolution="30ss",
        ).download(progress_bar=False)

        assert len(paths) == 1, f"expected one GeoTIFF, got {paths}"
        sidecar = paths[0].with_suffix(".legend.json")
        assert sidecar.exists(), "categorical output must carry a legend sidecar"

    def test_two_epoch_aggregate_growth(self, tmp_path: Path):
        """A 2000+2020 POP request reduces to one across-epoch GeoTIFF."""
        out = EarthLens(
            data_source="ghsl",
            variables=["GHS_POP"],
            start="2000-01-01",
            end="2020-12-31",
            lat_lim=_LAT_LIM,
            lon_lim=_LON_LIM,
            path=str(tmp_path),
            epochs=[2000, 2020],
        ).download(
            progress_bar=False,
            aggregate=AggregationConfig(freq="100YS", op="max"),
        )

        assert len(out) == 1, f"expected one aggregated raster, got {out}"
        assert _nonempty_geotiff(out[0]), "the aggregated raster must be non-empty"
