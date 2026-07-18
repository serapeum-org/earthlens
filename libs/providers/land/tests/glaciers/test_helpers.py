"""Unit tests for `earthlens.glaciers._helpers` (region map, reads, parse)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import box

from earthlens.base import SpatialExtent
from earthlens.glaciers import _helpers as H
from earthlens.glaciers.catalog import Catalog

pytestmark = pytest.mark.glaciers

# A small bbox over the French Alps that the RGI sample covers, as [w, s, e, n].
ALPS = [5.9, 44.9, 6.1, 45.0]


def test_shapely_bbox_matches_corners():
    """`shapely_bbox` builds a box from the extent's four edges."""
    space = SpatialExtent.from_pairs(lat_lim=[44.0, 47.0], lon_lim=[-6.0, 12.0])
    assert H.shapely_bbox(space).equals(box(-6.0, 44.0, 12.0, 47.0))


def test_regions_for_bbox_alps_is_central_europe():
    """An Alpine bbox maps to GTN-G region 11 (Central Europe)."""
    assert H.regions_for_bbox(ALPS, Catalog().regions) == ["11"]


def test_regions_for_bbox_antimeridian_region_ten():
    """An East-Siberian bbox maps to region 10 via its antimeridian sub-box."""
    assert H.regions_for_bbox([150.0, 60.0, 160.0, 65.0], Catalog().regions) == ["10"]


def test_regions_for_bbox_open_ocean_is_empty():
    """A bbox over open ocean (outside every region box) overlaps no region."""
    # South Atlantic at ~37 S — south of Low Latitudes, north of Subantarctic.
    assert H.regions_for_bbox([-20.0, -40.0, -15.0, -35.0], Catalog().regions) == []


def test_read_outlines_clips_and_is_epsg4326(rgi_sample_zip: Path):
    """`read_outlines` returns a clipped EPSG:4326 FeatureCollection."""
    fc = H.read_outlines(rgi_sample_zip, ALPS)
    assert isinstance(fc, FeatureCollection)
    assert str(fc.crs).upper() == "EPSG:4326"
    assert len(fc) >= 1
    assert fc.total_bounds[0] >= ALPS[0] - 1.0


def test_read_outlines_uses_pyramids_read_file(
    rgi_sample_zip: Path, monkeypatch: pytest.MonkeyPatch
):
    """Vector reads route through pyramids `FeatureCollection.read_file` (G3)."""
    calls: list[str] = []
    original = FeatureCollection.read_file.__func__

    def _spy(cls, path, *args, **kwargs):
        calls.append(path)
        return original(cls, path, *args, **kwargs)

    monkeypatch.setattr(FeatureCollection, "read_file", classmethod(_spy))
    fc = H.read_outlines(rgi_sample_zip, ALPS)
    assert len(fc) >= 1
    assert calls and calls[0].startswith("/vsizip/")


def test_subpackage_has_no_bare_gpd_read_file_or_xarray():
    """No file in the subpackage names `gpd.read_file` / `xarray` / `xr.` (A2/G3/G8)."""
    import earthlens.glaciers as pkg

    pkg_dir = Path(pkg.__file__).parent
    forbidden = ("gpd.read_file", "xarray", "xr.")
    offenders = []
    for path in pkg_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        offenders += [(path.name, token) for token in forbidden if token in text]
    for path in pkg_dir.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        offenders += [(path.name, token) for token in forbidden if token in text]
    assert offenders == []


def test_inner_shapefile_resolves_single_shp(rgi_sample_zip: Path):
    """The inner `.shp` member is resolved from the zip namelist."""
    assert H._inner_shapefile(rgi_sample_zip).lower().endswith(".shp")


def test_glims_wfs_url_uses_urn_lat_lon_axis():
    """The WFS bbox is `south,west,north,east` with the URN CRS (axis landmine)."""
    url, params = H.glims_wfs_url(
        "https://wfs", "GLIMS:GLIMS_Glacier_Outlines", ALPS, 50
    )
    assert url == "https://wfs"
    assert params["bbox"] == "44.9,5.9,45.0,6.1,urn:ogc:def:crs:EPSG::4326"
    assert params["count"] == "50"


def test_fetch_glims_reads_and_clips(tmp_path: Path, fake_http):
    """`fetch_glims` queries the WFS, saves GeoJSON, and returns a clipped FC."""
    fc = H.fetch_glims(
        "https://www.glims.org/geoserver/ows",
        "GLIMS:GLIMS_Glacier_Outlines",
        [7.9, 46.3, 8.1, 46.5],
        tmp_path / "glims.geojson",
        session=_Session(fake_http),
    )
    assert isinstance(fc, FeatureCollection)
    assert str(fc.crs).upper() == "EPSG:4326"
    assert (tmp_path / "glims.geojson").exists()


def test_download_zip_is_idempotent(tmp_path: Path, fake_http):
    """`download_zip` streams once and reuses the cached file on a second call."""
    url = "https://example.org/downloads/wgms_sample.zip"
    first = H.download_zip(url, tmp_path, session=_Session(fake_http))
    assert first.exists() and first.stat().st_size > 0
    second = H.download_zip(url, tmp_path, session=_Session(fake_http))
    assert second == first
    # one streamed GET only; the second call short-circuits on the cached file
    assert sum(c["stream"] for c in fake_http.calls) == 1


def test_parse_wgms_csv_reads_table(wgms_sample_zip: Path):
    """`parse_wgms_csv` reads a FoG table as a long DataFrame."""
    df = H.parse_wgms_csv(wgms_sample_zip, "mass_balance")
    assert isinstance(df, pd.DataFrame)
    assert {"glacier_id", "year", "annual_balance"} <= set(df.columns)
    assert len(df) >= 1


def test_parse_wgms_csv_unknown_table_raises(wgms_sample_zip: Path):
    """An unknown table name raises `KeyError` naming the missing member."""
    with pytest.raises(KeyError, match="change_band"):
        H.parse_wgms_csv(wgms_sample_zip, "change_band")


def test_wgms_glacier_table_has_join_columns(wgms_sample_zip: Path):
    """The `glacier` join table carries id / coordinates / region."""
    df = H.wgms_glacier_table(wgms_sample_zip)
    assert {"id", "latitude", "longitude", "gtng_region"} <= set(df.columns)


def test_empty_canonical_is_schema_only():
    """`empty_canonical` builds a zero-row frame with the given columns."""
    df = H.empty_canonical(["a", "b"])
    assert list(df.columns) == ["a", "b"]
    assert len(df) == 0


def test_concat_outlines_merges_fragments(rgi_sample_zip: Path):
    """`concat_outlines` merges per-region fragments into one collection."""
    one = H.read_outlines(rgi_sample_zip, ALPS)
    merged = H.concat_outlines([one, one])
    assert isinstance(merged, FeatureCollection)
    assert len(merged) == 2 * len(one)


def test_concat_outlines_empty_raises():
    """`concat_outlines` needs at least one fragment."""
    with pytest.raises(ValueError, match="at least one fragment"):
        H.concat_outlines([])


class _Session:
    """A `requests.Session` stand-in delegating `.get` to a FakeHttp recorder."""

    def __init__(self, fake_http) -> None:
        self.get = fake_http.get
