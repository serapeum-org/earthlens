"""Live gated e2e for the DEM backend — anonymous Copernicus DEM download."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.dem import DEM

pytestmark = [pytest.mark.dem, pytest.mark.e2e]


def test_download_one_glo30_tile(tmp_path: Path) -> None:
    """Fetch the Nile Delta GLO-30 tile anonymously from `copernicus-dem-30m`."""
    src = DEM(
        variables=[],
        lat_lim=[30.2, 30.8],
        lon_lim=[31.2, 31.8],
        path=tmp_path,
    )
    written = src.download(progress_bar=False)
    assert len(written) == 1
    tile = written[0]
    assert tile.exists()
    # A live tile is many megabytes; the fake stubs used elsewhere write 15 bytes.
    assert tile.stat().st_size > 100_000
    assert tile.name.endswith("_N30_00_E031_00_DEM.tif")
