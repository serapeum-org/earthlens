"""Backend tests for `earthlens.drought.Drought` (USDM + SPEIbase routes).

The EDO/GDO `edo-wcs` route waits on the cross-repo `PY-A` pyramids
temporal `read_wcs` extension and is exercised here only for the
`NotImplementedError` boundary. Once `PY-A` ships, a real EDO test lands
alongside the USDM/SPEIbase ones.

All transports are mocked at the HTTP / pyramids boundary so the suite is
network-free.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from earthlens.drought import Drought
from earthlens.drought import backend as backend_module
from earthlens.drought.backend import SPEIBASE_EPOCH_YEAR


_USDM_PAYLOAD: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [
                        [
                            [-90.0, 32.0],
                            [-86.0, 32.0],
                            [-86.0, 36.0],
                            [-90.0, 36.0],
                            [-90.0, 32.0],
                        ]
                    ]
                ],
            },
            "properties": {
                "OBJECTID": 1,
                "DM": 1,
                "Shape_Length": 16.0,
                "Shape_Area": 16.0,
            },
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    [
                        [
                            [-93.0, 33.0],
                            [-91.0, 33.0],
                            [-91.0, 35.0],
                            [-93.0, 35.0],
                            [-93.0, 33.0],
                        ]
                    ]
                ],
            },
            "properties": {
                "OBJECTID": 2,
                "DM": 3,
                "Shape_Length": 8.0,
                "Shape_Area": 4.0,
            },
        },
    ],
}


def test_drought_rejects_missing_dataset():
    """A request without `dataset=` is a clear ValueError."""
    with pytest.raises(ValueError, match="needs dataset="):
        Drought(
            start="2026-06-01",
            end="2026-06-01",
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="",
        )


def test_drought_rejects_missing_bbox():
    """A request without lat/lon limits is a clear ValueError."""
    with pytest.raises(ValueError, match="bbox is required"):
        Drought(
            start="2026-06-01",
            end="2026-06-01",
            lat_lim=[],
            lon_lim=[],
            dataset="usdm",
        )


def test_drought_rejects_unknown_dataset_with_did_you_mean():
    """An unknown dataset id surfaces the catalog did-you-mean."""
    with pytest.raises(ValueError, match="Did you mean 'usdm'"):
        Drought(
            start="2026-06-01",
            end="2026-06-01",
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="usdmm",
        )


def test_drought_rejects_inverted_window():
    """end < start surfaces a clear ValueError."""
    with pytest.raises(ValueError, match="before start"):
        Drought(
            start="2026-06-30",
            end="2026-06-01",
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="usdm",
        )


def test_drought_rejects_conflicting_variables_kwarg():
    """A non-trivial `variables=` argument is rejected."""
    with pytest.raises(ValueError, match="one-dataset-per-instance"):
        Drought(
            start="2026-06-01",
            end="2026-06-01",
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="usdm",
            variables=["edo-spaST"],
        )


def test_drought_output_kind_is_per_instance():
    """OUTPUT_KIND tracks the resolved catalog row."""
    usdm = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
    )
    spei = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
    )
    assert usdm.OUTPUT_KIND == "vector"
    assert spei.OUTPUT_KIND == "raster"


def test_drought_search_emits_one_product_per_snapped_period():
    """A two-week window collapses to two Thursday releases for USDM."""
    backend = Drought(
        start="2026-06-10",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
    )
    products = backend._search()
    assert [p.id for p in products] == [
        "usdm@2026-06-04",
        "usdm@2026-06-11",
        "usdm@2026-06-18",
    ]
    assert {p.metadata["dataset"] for p in products} == {"usdm"}


def test_usdm_render_url_uses_release_date():
    """The `{ymd}` placeholder takes the Thursday release date verbatim."""
    rendered = Drought._render_usdm_url(
        "https://example.com/usdm_{ymd}.json", dt.date(2026, 6, 18)
    )
    assert rendered == "https://example.com/usdm_20260618.json"


def test_usdm_fetch_builds_feature_collection_in_4326(monkeypatch, tmp_path):
    """Mock the HTTP layer; verify the FeatureCollection schema + CRS."""
    monkeypatch.setattr(
        backend_module, "_http_get_json", lambda url: _USDM_PAYLOAD
    )
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        path=str(tmp_path),
    )
    fc = backend.download(progress_bar=False)
    assert fc.crs.to_epsg() == 4326
    assert len(fc) == 2
    assert set(fc["DM"]) == {1, 3}
    assert fc["release_date"].iloc[0] == "2026-06-18"


def test_usdm_fetch_clips_to_bbox(monkeypatch, tmp_path):
    """Polygons outside the bbox are dropped; the schema is preserved."""
    monkeypatch.setattr(
        backend_module, "_http_get_json", lambda url: _USDM_PAYLOAD
    )
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[-80.0, -75.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        path=str(tmp_path),
    )
    fc = backend.download(progress_bar=False)
    assert len(fc) == 0
    assert {"OBJECTID", "DM", "release_date"}.issubset(set(fc.columns))


def test_usdm_aggregate_rejected(tmp_path):
    """`aggregate=` is a NotImplementedError on the vector USDM route."""
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        path=str(tmp_path),
    )
    with pytest.raises(NotImplementedError, match="vector"):
        backend.download(aggregate=object())


def test_edo_route_raises_pending_pyramids(tmp_path):
    """EDO/GDO is wired but raises pending PY-A."""
    backend = Drought(
        start="2026-06-21",
        end="2026-06-21",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="edo-spaST",
        path=str(tmp_path),
    )
    with pytest.raises(NotImplementedError, match="PY-A"):
        backend.download(progress_bar=False)


def test_raster_aggregate_rejected_until_reducer_lands(tmp_path):
    """The raster `aggregate=` path raises a clear NotImplementedError."""
    backend = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    with pytest.raises(NotImplementedError, match="not wired"):
        backend.download(aggregate=object())


class _FakeSubset:
    """Stand-in for `pyramids.dataset.Dataset` returned by `nc.subset`."""

    def __init__(self, target: Path):
        self._target = target

    def to_file(self, path: str) -> None:
        Path(path).write_bytes(b"FAKE-GTIFF-" + str(self._target).encode())


class _FakeNetCDF:
    """Stand-in for `pyramids.netcdf.NetCDF` — records `subset()` calls."""

    calls: list[dict[str, Any]] = []

    def __init__(self, path: str):
        self.path = path

    def subset(
        self,
        variable: str,
        *,
        time: int,
        bbox: tuple[float, float, float, float],
        crs: int,
    ) -> _FakeSubset:
        _FakeNetCDF.calls.append(
            {"variable": variable, "time": time, "bbox": bbox, "crs": crs}
        )
        return _FakeSubset(self.path)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_netcdf(monkeypatch):
    """Replace `pyramids.netcdf.NetCDF.read_file` with a `_FakeNetCDF`."""
    import pyramids.netcdf as netcdf_module

    monkeypatch.setattr(
        netcdf_module.NetCDF,
        "read_file",
        classmethod(lambda cls, path: _FakeNetCDF(str(path))),
    )
    _FakeNetCDF.calls.clear()
    yield _FakeNetCDF
    _FakeNetCDF.calls.clear()


def test_speibase_fetch_writes_one_tif_per_month(monkeypatch, tmp_path, fake_netcdf):
    """The SPEIbase route slices the NetCDF and writes one TIFF per month."""
    monkeypatch.setattr(
        backend_module,
        "_http_download",
        lambda url, target: target.write_bytes(b"fake-nc"),
    )
    backend = Drought(
        start="2026-05-15",
        end="2026-06-20",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    paths = backend.download(progress_bar=False)
    assert len(paths) == 2
    assert all(p.suffix == ".tif" for p in paths)
    assert paths[0].name == "speibase-12_202605.tif"
    assert paths[1].name == "speibase-12_202606.tif"
    calls = fake_netcdf.calls
    assert len(calls) == 2
    expected_may = (2026 - SPEIBASE_EPOCH_YEAR) * 12 + (5 - 1)
    expected_jun = (2026 - SPEIBASE_EPOCH_YEAR) * 12 + (6 - 1)
    assert calls[0]["time"] == expected_may
    assert calls[1]["time"] == expected_jun
    assert calls[0]["variable"] == "spei"
    assert calls[0]["bbox"] == (-95.0, 30.0, -85.0, 40.0)
    assert calls[0]["crs"] == 4326


def test_speibase_reuses_cached_nc(monkeypatch, tmp_path, fake_netcdf):
    """A second download for the same dataset id skips the re-download."""
    calls: list[str] = []

    def _record_download(url: str, target: Path) -> None:
        calls.append(url)
        target.write_bytes(b"fake-nc")

    monkeypatch.setattr(backend_module, "_http_download", _record_download)
    backend = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    backend.download(progress_bar=False)
    backend.download(progress_bar=False)
    assert len(calls) == 1


def test_speibase_rejects_period_before_epoch(monkeypatch, tmp_path, fake_netcdf):
    """A pre-epoch period raises rather than silently writing junk."""
    monkeypatch.setattr(
        backend_module,
        "_http_download",
        lambda url, target: target.write_bytes(b"fake-nc"),
    )
    backend = Drought(
        start="1850-01-01",
        end="1850-01-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="before the dataset epoch"):
        backend.download(progress_bar=False)


def test_drought_src_does_not_import_owslib_or_xarray():
    """earthlens drought src must never import owslib or xarray (`G7`)."""
    import re

    src = Path(__file__).resolve().parents[2] / "src" / "earthlens" / "drought"
    pattern = re.compile(r"\b(?:owslib|xarray)\b")
    offenders = []
    for py_file in src.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if any(
            line.startswith("import ") or line.startswith("from ")
            for line in text.splitlines()
            if pattern.search(line)
        ):
            offenders.append(py_file)
    assert offenders == [], f"owslib/xarray import leak in: {offenders}"
