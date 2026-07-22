"""Unit tests for `earthlens.glaciers.backend.Glaciers` (routing + outputs)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from earthlens.glaciers.backend import Glaciers
from pyramids.feature.collection import FeatureCollection

from earthlens.glaciers import _helpers

pytestmark = pytest.mark.glaciers

DATA = Path(__file__).parent / "data"
ALPS_LAT = [44.9, 45.0]
ALPS_LON = [5.9, 6.1]


def _route_download(url: str, dest_dir, **kwargs) -> Path:
    """Stand-in for `download_zip` returning the local fixture for a URL."""
    if "wgms" in url.lower() or "fog" in url.lower():
        return DATA / "wgms_sample.zip"
    return DATA / "rgi_sample.zip"


@pytest.fixture
def patch_download(monkeypatch: pytest.MonkeyPatch):
    """Patch `_helpers.download_zip` to serve the local RGI / WGMS fixtures."""
    monkeypatch.setattr(_helpers, "download_zip", _route_download)


def test_rgi_returns_clipped_feature_collection(tmp_path: Path, patch_download):
    """An RGI bbox over the Alps yields a clipped EPSG:4326 FeatureCollection."""
    backend = Glaciers(
        variables=["rgi:outlines"],
        lat_lim=ALPS_LAT,
        lon_lim=ALPS_LON,
        path=tmp_path,
    )
    assert backend.OUTPUT_KIND == "vector"
    fc = backend.download()
    assert isinstance(fc, FeatureCollection)
    assert str(fc.crs).upper() == "EPSG:4326"
    assert len(fc) >= 1


def test_rgi_search_maps_bbox_to_region(tmp_path: Path):
    """`_search` yields one product per overlapping GTN-G region."""
    backend = Glaciers(
        variables=["rgi:outlines"],
        lat_lim=ALPS_LAT,
        lon_lim=ALPS_LON,
        path=tmp_path,
    )
    products = backend._search()
    assert [p.metadata["region_id"] for p in products] == ["11"]
    assert products[0].href.endswith("rgi2000-v7.0-g-11_central_europe.zip")


def test_rgi_region_override_merges(tmp_path: Path, patch_download):
    """A multi-region `region=` override merges the per-region fragments."""
    one = Glaciers(variables=["rgi:outlines"], region="11", path=tmp_path).download()
    merged = Glaciers(
        variables=["rgi:outlines"], region=["11", "12"], path=tmp_path
    ).download()
    assert len(merged) == 2 * len(one)


def test_rgi_empty_when_bbox_hits_no_region(tmp_path: Path, patch_download):
    """A bbox over open ocean returns an empty FeatureCollection."""
    backend = Glaciers(
        variables=["rgi:outlines"],
        lat_lim=[-40.0, -35.0],
        lon_lim=[-20.0, -15.0],
        path=tmp_path,
    )
    fc = backend.download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 0


def test_glims_returns_feature_collection(tmp_path: Path, fake_http):
    """A GLIMS bbox query returns a clipped FeatureCollection."""
    backend = Glaciers(
        variables=["glims:outlines"],
        lat_lim=[46.3, 46.5],
        lon_lim=[7.9, 8.1],
        path=tmp_path,
    )
    assert backend.OUTPUT_KIND == "vector"
    fc = backend.download()
    assert isinstance(fc, FeatureCollection)
    assert str(fc.crs).upper() == "EPSG:4326"


def test_wgms_returns_dataframe_and_writes(tmp_path: Path, patch_download):
    """A WGMS dataset returns a DataFrame and writes it to root_dir."""
    backend = Glaciers(
        variables=["wgms:mass_balance"],
        path=tmp_path,
    )
    assert backend.OUTPUT_KIND == "tabular"
    df = backend.download()
    assert isinstance(df, pd.DataFrame)
    assert {"glacier_id", "year"} <= set(df.columns)
    assert (tmp_path / "wgms_mass_balance.csv").exists()


def test_wgms_glacier_id_filter(tmp_path: Path, patch_download):
    """A `glacier_id=` filter narrows the WGMS frame to that glacier."""
    full = Glaciers(variables=["wgms:mass_balance"], path=tmp_path).download()
    one_id = int(full["glacier_id"].iloc[0])
    filtered = Glaciers(
        variables=["wgms:mass_balance"], glacier_id=one_id, path=tmp_path
    ).download()
    assert set(filtered["glacier_id"]) == {one_id}
    assert 0 < len(filtered) <= len(full)


def test_download_rejects_aggregate_vector(tmp_path: Path, patch_download):
    """`aggregate=` is rejected for a vector dataset (G8)."""
    backend = Glaciers(variables=["rgi:outlines"], region="11", path=tmp_path)
    with pytest.raises(NotImplementedError, match="aggregate"):
        backend.download(aggregate=object())


def test_download_rejects_aggregate_tabular(tmp_path: Path, patch_download):
    """`aggregate=` is rejected for a tabular (WGMS) dataset too (G8)."""
    backend = Glaciers(variables=["wgms:mass_balance"], path=tmp_path)
    with pytest.raises(NotImplementedError, match="aggregate"):
        backend.download(aggregate=object())


def test_rgi_requires_bbox_or_region(tmp_path: Path):
    """RGI without a bbox or a region override is refused."""
    with pytest.raises(ValueError, match="needs a bbox"):
        Glaciers(variables=["rgi:outlines"], path=tmp_path)


def test_glims_requires_bbox(tmp_path: Path):
    """GLIMS without a bbox is refused (a global WFS query is too large)."""
    with pytest.raises(ValueError, match="needs a bbox"):
        Glaciers(variables=["glims:outlines"], path=tmp_path)


def test_variables_must_be_single_id(tmp_path: Path):
    """Exactly one dataset id is required (OUTPUT_KIND is per instance)."""
    with pytest.raises(ValueError, match="exactly one dataset id"):
        Glaciers(
            variables=["rgi:outlines", "wgms:mass_balance"],
            region="11",
            path=tmp_path,
        )


def test_variables_mapping_is_rejected(tmp_path: Path):
    """A mapping `variables=` is a TypeError (pass a list of one id)."""
    with pytest.raises(TypeError, match="one-element list"):
        Glaciers(variables={"rgi:outlines": []}, region="11", path=tmp_path)


def test_bad_output_format_rejected(tmp_path: Path):
    """An unrecognised `output_format` is rejected."""
    with pytest.raises(ValueError, match="output_format"):
        Glaciers(
            variables=["rgi:outlines"],
            region="11",
            output_format="geojson",
            path=tmp_path,
        )


def test_no_xarray_import_in_backend():
    """The backend source never imports an array/NetCDF stack (G8)."""
    import earthlens.glaciers.backend as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "import xarray" not in src
    assert "xr." not in src
