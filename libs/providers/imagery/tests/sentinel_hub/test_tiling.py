"""Unit tests for the Sentinel Hub local-tiling + mosaic plane (C6)."""

from __future__ import annotations

from pathlib import Path

import pyramids.dataset.merge as merge_mod
import pytest

from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub


def _tiling_backend(output_dir, **kwargs) -> SentinelHub:
    """A backend whose bbox renders to ~5000 px/side → a 2×2 tile grid."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables={"sentinel-2-l2a-ndvi": []},
        lat_lim=[40.0, 40.5],
        lon_lim=[14.0, 14.5],
        path=output_dir,
        resolution=10,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


@pytest.fixture
def recorded_merge(monkeypatch):
    """Record merge_rasters calls and write a placeholder destination."""
    calls = []

    def _fake_merge(src, dst, **kwargs):
        calls.append((list(src), str(dst), kwargs))
        Path(dst).write_bytes(b"II*\x00merged")

    monkeypatch.setattr(merge_mod, "merge_rasters", _fake_merge)
    return calls


class TestTiling:
    """Oversized AOIs without S3 render via tiling + mosaic."""

    def test_renders_grid_and_merges(self, fake_sh, recorded_merge, output_dir: Path):
        """A 2×2 grid renders four tiles and merges them into one GeoTIFF."""
        backend = _tiling_backend(output_dir)
        assert backend._resolve_plane() == "tiling"
        paths = backend.download()
        assert len(paths) == 1
        assert paths[0].name == "sentinel-2-l2a-ndvi.tif"
        assert paths[0].exists()
        # the rendered tiles' own no-data reaches merge_rasters, not its 0 default
        assert recorded_merge[-1][2]["no_data_value"] == -9999.0
        # one merge call, four tile sources
        assert len(recorded_merge) == 1
        srcs, dst, _kw = recorded_merge[0]
        assert len(srcs) == 4
        assert dst == str(paths[0])

    def test_tiles_cover_the_request_bbox(
        self, fake_sh, recorded_merge, output_dir: Path
    ):
        """Four Process requests are built, one per tile."""
        backend = _tiling_backend(output_dir)
        backend.download()
        assert len(fake_sh.SentinelHubRequest.instances) == 4

    def test_tile_temporaries_cleaned_up(
        self, fake_sh, recorded_merge, output_dir: Path
    ):
        """The per-product tile scratch directory is removed after the merge."""
        backend = _tiling_backend(output_dir)
        backend.download()
        assert not list(output_dir.glob("_tiles_*"))

    def test_explicit_tiling_api(self, fake_sh, recorded_merge, output_dir: Path):
        """A small AOI can be forced through the tiling plane (single tile)."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=output_dir,
            resolution=10,
            api="tiling",
            client_id="a",
            client_secret="b",
        )
        backend.download()
        assert len(fake_sh.SentinelHubRequest.instances) == 1
