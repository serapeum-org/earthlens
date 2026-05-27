"""Unit + integration tests for `earthlens.sentinel_hub.backend` (construction + dispatch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.sentinel_hub.backend import SentinelHub

pytestmark = pytest.mark.sentinel_hub


def _make_backend(variables, output_dir, **kwargs) -> SentinelHub:
    """Build a SentinelHub backend over a small bbox + window (no network)."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables=variables,
        lat_lim=[40.0, 40.1],
        lon_lim=[14.0, 14.1],
        path=output_dir,
        **kwargs,
    )


class TestConstruction:
    """Constructor validation and request resolution."""

    def test_output_kind_is_mixed(self, output_dir: Path):
        """The backend declares mixed output (fixed class attribute)."""
        backend = _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir)
        assert backend.OUTPUT_KIND == "mixed"
        assert SentinelHub.OUTPUT_KIND == "mixed"

    def test_resolves_keys(self, output_dir: Path):
        """Each requested key is resolved against the catalog."""
        backend = _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir)
        assert list(backend._resolved) == ["sentinel-2-l2a-ndvi"]

    def test_unknown_api_rejected(self, output_dir: Path):
        """An unknown `api=` is rejected at construction."""
        with pytest.raises(ValueError, match="unknown api"):
            _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir, api="render")

    def test_bad_mosaicking_order_rejected(self, output_dir: Path):
        """An invalid `mosaicking_order` is rejected."""
        with pytest.raises(ValueError, match="mosaicking_order"):
            _make_backend(
                {"sentinel-2-l2a-ndvi": []}, output_dir, mosaicking_order="newest"
            )

    def test_empty_variables_rejected(self, output_dir: Path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="at least one"):
            _make_backend({}, output_dir)

    def test_non_dict_variables_rejected(self, output_dir: Path):
        """A non-mapping variables value is rejected."""
        with pytest.raises(TypeError, match="mapping"):
            _make_backend(["sentinel-2-l2a-ndvi"], output_dir)

    def test_unknown_key_did_you_mean(self, output_dir: Path):
        """An unknown collection/recipe key raises with a hint."""
        with pytest.raises(ValueError, match="not a known"):
            _make_backend({"sentinel-2-l2a-ndv": []}, output_dir)


class TestSearch:
    """`_search` is a cheap dry-run."""

    def test_one_product_per_key(self, output_dir: Path):
        """`_search` returns one product per requested key, no network."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": [], "sentinel-2-l2a-ndwi": []}, output_dir
        )
        products = backend._search()
        assert sorted(p.id for p in products) == [
            "sentinel-2-l2a-ndvi",
            "sentinel-2-l2a-ndwi",
        ]
        assert products[0].metadata["resolved"].sh_collection == "SENTINEL2_L2A"


class TestPlaneRouting:
    """`_resolve_plane` auto-routes by size + geometry."""

    def test_small_bbox_routes_to_process(self, fake_sh, output_dir: Path):
        """A tiny bbox auto-routes to the Process plane."""
        backend = _make_backend({"sentinel-2-l2a-ndvi": []}, output_dir, resolution=10)
        assert backend._resolve_plane() == "process"

    def test_geometry_routes_to_statistical(self, fake_sh, output_dir: Path):
        """A `geometry=` request auto-routes to the Statistical plane."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi-stats": []},
            output_dir,
            geometry={"type": "Polygon", "coordinates": []},
        )
        assert backend._resolve_plane() == "statistical"

    def test_oversized_bbox_routes_off_process(self, fake_sh, output_dir: Path):
        """A coarse resolution over a wide bbox routes beyond Process."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi": []},
            lat_lim=[10.0, 40.0],
            lon_lim=[0.0, 30.0],
            path=output_dir,
            resolution=10,
        )
        assert backend._resolve_plane() in {"async", "batch"}

    def test_explicit_api_honoured(self, fake_sh, output_dir: Path):
        """An explicit `api=` overrides size-based routing."""
        backend = _make_backend(
            {"sentinel-2-l2a-ndvi": []}, output_dir, api="statistical"
        )
        assert backend._resolve_plane() == "statistical"
