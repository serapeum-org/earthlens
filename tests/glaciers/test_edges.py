"""Edge-path coverage for the glaciers helpers, catalog, and backend."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
import requests
from pyramids.feature.collection import FeatureCollection
from shapely.geometry import box

from earthlens.glaciers import _helpers
from earthlens.glaciers.backend import Glaciers
from earthlens.glaciers.catalog import Catalog, clear_catalog_cache

pytestmark = pytest.mark.glaciers

DATA = Path(__file__).parent / "data"


class _StreamResp:
    """A streaming response stand-in over fixed bytes."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int = 1 << 20):
        yield self._payload


class _FlakySession:
    """A session whose `get` raises `fails` times before streaming bytes."""

    def __init__(self, payload: bytes, fails: int) -> None:
        self._payload = payload
        self._fails = fails
        self.attempts = 0

    def get(self, url, stream=False, timeout=None):
        self.attempts += 1
        if self.attempts <= self._fails:
            raise requests.ConnectionError("boom")
        return _StreamResp(self._payload)


def test_stream_download_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`download_zip` retries a transient failure, then writes the file."""
    monkeypatch.setattr(_helpers.time, "sleep", lambda *_: None)
    session = _FlakySession(b"payload", fails=2)
    out = _helpers.download_zip(
        "https://x/f.zip", tmp_path, session=session, backoff=0.0
    )
    assert out.read_bytes() == b"payload"
    assert session.attempts == 3


def test_stream_download_raises_after_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`download_zip` raises `HTTPError` once every attempt fails."""
    monkeypatch.setattr(_helpers.time, "sleep", lambda *_: None)
    session = _FlakySession(b"payload", fails=99)
    with pytest.raises(requests.HTTPError, match="failed after"):
        _helpers.download_zip(
            "https://x/f.zip", tmp_path, session=session, retries=2, backoff=0.0
        )


def test_inner_shapefile_rejects_zip_without_shp(tmp_path: Path):
    """A zip with no `.shp` member raises a clear error."""
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "no shapefile here")
    with pytest.raises(ValueError, match="exactly one .shp"):
        _helpers._inner_shapefile(bad)


def test_clip_to_bbox_reprojects_non_4326():
    """`_clip_to_bbox` reprojects a non-4326 collection before clipping."""
    gdf = gpd.GeoDataFrame(
        {"v": [1]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326"
    ).to_crs("EPSG:3857")
    fc = FeatureCollection(gdf)
    out = _helpers._clip_to_bbox(fc, [-1.0, -1.0, 2.0, 2.0])
    assert str(out.crs).upper() == "EPSG:4326"
    assert len(out) == 1


def test_fetch_glims_empty_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An empty WFS response yields an empty FeatureCollection."""
    empty = json.dumps({"type": "FeatureCollection", "features": [], "crs": None})

    class _Resp:
        text = empty

        def raise_for_status(self):
            return None

    monkeypatch.setattr(_helpers.requests, "get", lambda *a, **k: _Resp())
    fc = _helpers.fetch_glims(
        "https://wfs", "T", [0.0, 0.0, 1.0, 1.0], tmp_path / "e.geojson"
    )
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 0


def test_filter_wgms_region_and_bbox_and_name():
    """`filter_wgms` narrows by region prefix, bbox, and name substring."""
    df = pd.DataFrame(
        {
            "glacier_id": [1, 2, 3],
            "glacier_name": ["ALPHA", "BETA", "GAMMA"],
            "value": [10, 20, 30],
        }
    )
    glaciers = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "latitude": [46.0, 10.0, 47.0],
            "longitude": [8.0, 8.0, 9.0],
            "gtng_region": ["11_central_europe", "16_low_latitudes", "11_x"],
        }
    )
    by_region = _helpers.filter_wgms(df, glaciers, region="11")
    assert set(by_region["glacier_id"]) == {1, 3}
    by_bbox = _helpers.filter_wgms(df, glaciers, bbox=[7.5, 45.0, 8.5, 46.5])
    assert set(by_bbox["glacier_id"]) == {1}
    by_name = _helpers.filter_wgms(df, glaciers, glacier_name="bet")
    assert set(by_name["glacier_id"]) == {2}


def test_concat_outlines_all_empty_returns_empty(rgi_sample_zip: Path):
    """Merging only-empty fragments returns an empty collection."""
    empty = _helpers.empty_feature_collection()
    out = _helpers.concat_outlines([empty, empty])
    assert isinstance(out, FeatureCollection)
    assert len(out) == 0


def test_catalog_path_must_exist(tmp_path: Path):
    """A non-existent catalog path raises a clear error."""
    clear_catalog_cache()
    with pytest.raises(ValueError, match="does not exist"):
        Catalog.load(tmp_path / "missing")
    clear_catalog_cache()


def test_catalog_empty_datasets_block(tmp_path: Path):
    """A catalog with no `datasets:` block is rejected."""
    clear_catalog_cache()
    (tmp_path / "a.yaml").write_text("regions: {}\n")
    with pytest.raises(ValueError, match="empty 'datasets:' block"):
        Catalog.load(tmp_path)
    clear_catalog_cache()


def test_catalog_duplicate_region(tmp_path: Path):
    """A region declared twice across catalog files is an error."""
    clear_catalog_cache()
    body = (
        "datasets:\n  rgi:outlines:\n    source: rgi\n    output_kind: vector\n"
        'regions:\n  "11":\n    name: x\n    bboxes: [[0,0,1,1]]\n    url: u\n'
    )
    (tmp_path / "a.yaml").write_text(body)
    (tmp_path / "b.yaml").write_text(
        'regions:\n  "11":\n    name: y\n    bboxes: [[0,0,1,1]]\n    url: v\n'
    )
    with pytest.raises(ValueError, match="declared twice"):
        Catalog.load(tmp_path)
    clear_catalog_cache()


def test_catalog_bad_dataset_row(tmp_path: Path):
    """A dataset row that fails validation is reported with its origin file."""
    clear_catalog_cache()
    (tmp_path / "a.yaml").write_text(
        "datasets:\n  rgi:outlines:\n    source: rgi\n    output_kind: tabular\n"
    )
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(tmp_path)
    clear_catalog_cache()


def test_catalog_curated_id_missing_from_index(tmp_path: Path):
    """A curated id absent from `available_datasets:` is rejected."""
    clear_catalog_cache()
    (tmp_path / "a.yaml").write_text(
        "available_datasets:\n  - other:id\n"
        "datasets:\n  rgi:outlines:\n    source: rgi\n    output_kind: vector\n"
    )
    with pytest.raises(ValueError, match="missing from 'available_datasets:'"):
        Catalog.load(tmp_path)
    clear_catalog_cache()


def test_catalog_bad_region_row(tmp_path: Path):
    """A region row missing a required field is reported."""
    clear_catalog_cache()
    (tmp_path / "a.yaml").write_text(
        "datasets:\n  rgi:outlines:\n    source: rgi\n    output_kind: vector\n"
        'regions:\n  "11":\n    name: x\n    bboxes: [[0,0,1,1]]\n'
    )
    with pytest.raises(ValueError, match="region '11' failed validation"):
        Catalog.load(tmp_path)
    clear_catalog_cache()


def test_region_rejects_empty_bboxes():
    """A region with no bboxes is rejected."""
    from earthlens.glaciers.catalog import Region

    with pytest.raises(ValueError, match="has no bboxes"):
        Region(id="11", name="x", bboxes=[], url="u")


def test_wgms_parquet_output(tmp_path: Path, monkeypatch):
    """A WGMS dataset can be written as parquet."""
    pytest.importorskip("pyarrow")
    monkeypatch.setattr(
        _helpers, "download_zip", lambda *a, **k: DATA / "wgms_sample.zip"
    )
    backend = Glaciers(
        variables=["wgms:mass_balance"],
        output_format="parquet",
        path=tmp_path,
    )
    backend.download()
    assert (tmp_path / "wgms_mass_balance.parquet").exists()


def test_wgms_empty_filter_writes_schema_only(tmp_path: Path, monkeypatch):
    """A WGMS filter matching nothing writes a schema-only table and warns."""
    monkeypatch.setattr(
        _helpers, "download_zip", lambda *a, **k: DATA / "wgms_sample.zip"
    )
    backend = Glaciers(
        variables=["wgms:mass_balance"],
        glacier_id=999999999,
        path=tmp_path,
    )
    df = backend.download()
    assert len(df) == 0
    assert (tmp_path / "wgms_mass_balance.csv").exists()
