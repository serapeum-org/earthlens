"""Backend tests for `earthlens.drought.Drought` (all three routes).

Covers the USDM GeoJSON, Copernicus EDO/GDO `GetCoverage`, and SPEIbase
NetCDF transports. The EDO/GDO route is exercised end-to-end at the HTTP
boundary — `GetCoverage` URL rendering, a mocked fetch writing one TIFF per
period, the single `map=DO_WCS` request, and Copernicus error surfacing.

All transports are mocked at the HTTP / pyramids boundary so the suite is
network-free.
"""

from __future__ import annotations

import datetime as dt
import re
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


def test_drought_non_midnight_window_keeps_trailing_day():
    """A non-midnight start must not drop the trailing calendar day."""
    backend = Drought(
        start=dt.datetime(2026, 6, 1, 23, 59),
        end=dt.datetime(2026, 7, 1, 0, 1),
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path="drought_out",
    )
    # Snap to month-start; the range must cover both June and July.
    snapped = sorted(d.date() for d in backend.time.dates)
    assert snapped == [dt.date(2026, 6, 1), dt.date(2026, 7, 1)]


def test_drought_accepts_datetime_and_date_inputs():
    """start/end coerce through to_datetime so non-string inputs snap correctly.

    Pin the actual snapped value (not just 'both paths agree') — a
    regression that routes BOTH non-string inputs through a buggy coercer
    (silently truncating, returning None / NaT, etc.) would still pass an
    equality-only check because two identical wrong values compare equal.
    """
    backend_dt = Drought(
        start=dt.datetime(2026, 6, 23),
        end=dt.datetime(2026, 6, 23),
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        today=dt.date(2026, 6, 23),
    )
    backend_date = Drought(
        start=dt.date(2026, 6, 23),
        end=dt.date(2026, 6, 23),
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        today=dt.date(2026, 6, 23),
    )
    # 2026-06-23 queried on the same Tuesday walks back to 06-16 (G5).
    expected = [dt.date(2026, 6, 16)]
    assert [d.date() for d in backend_dt.time.dates] == expected
    assert [d.date() for d in backend_date.time.dates] == expected


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


def test_drought_output_kind_is_per_instance(tmp_path):
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
        path=str(tmp_path),
    )
    assert usdm.OUTPUT_KIND == "vector"
    assert spei.OUTPUT_KIND == "raster"


@pytest.mark.parametrize(
    "empty_path",
    [None, "", Path(""), Path("."), Path()],
    ids=["None", "empty-str", "Path-empty-str", "Path-dot", "Path-default"],
)
def test_drought_raster_rejects_every_empty_path_form(empty_path):
    """Every empty-path form (None, '', Path(''), Path('.'), Path()) raises.

    `bool(Path(''))` and `bool(Path())` are both True (pathlib defines no
    __bool__/__len__), so a `not path` check would silently route the
    parent class to `Path('.').absolute()` — the user's CWD.
    """
    kwargs = dict(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
    )
    if empty_path is not None:
        kwargs["path"] = empty_path
    with pytest.raises(ValueError, match="needs path="):
        Drought(**kwargs)


def test_drought_search_emits_one_product_per_snapped_period():
    """A two-week window collapses to one Tuesday release per week for USDM."""
    backend = Drought(
        start="2026-06-10",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        # Today pinned to Tue 2026-06-23: this week's 06-23 composite has
        # not been released yet (the Thursday release is 06-25), so the
        # walk-back fires for 06-23 itself; 06-16's release Thursday was
        # 06-18 and is past, so 06-16 lands on itself.
        today=dt.date(2026, 6, 23),
    )
    products = backend._search()
    # Window 06-10 (Wed) → 06-23 (Tue): 06-10 snaps to 06-09 (released).
    # 06-23 walks back to 06-16 (06-23's release Thursday is still future).
    assert [p.id for p in products] == [
        "usdm@2026-06-09",
        "usdm@2026-06-16",
    ]
    assert {p.metadata["dataset"] for p in products} == {"usdm"}


def test_drought_search_does_not_overshoot_historical_tuesday():
    """A historical Tuesday queried much later snaps to itself, not the prior week."""
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        # Today is well past the 06-25 release Thursday for 2026-06-23, so
        # the walk-back must NOT trigger (Round 2 G5 regression).
        today=dt.date(2027, 1, 15),
    )
    products = backend._search()
    assert [p.id for p in products] == ["usdm@2026-06-23"]


def test_usdm_render_url_uses_tuesday_valid_date():
    """The `{ymd}` placeholder takes the Tuesday valid date verbatim."""
    rendered = Drought._render_usdm_url(
        "https://example.com/usdm_{ymd}.json", dt.date(2026, 6, 23)
    )
    assert rendered == "https://example.com/usdm_20260623.json"


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
        # Pin today to the Tuesday itself — the 06-23 composite's release
        # Thursday (06-25) is still in the future, so the walk-back fires.
        today=dt.date(2026, 6, 23),
    )
    fc = backend.download(progress_bar=False)
    assert fc.crs.to_epsg() == 4326
    assert len(fc) == 2
    assert set(fc["DM"]) == {1, 3}
    assert fc["release_date"].iloc[0] == "2026-06-16"


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


def test_usdm_honours_payload_crs_member(monkeypatch, tmp_path):
    """A GeoJSON `crs` member is respected (defensive reproject reachable)."""
    # Build a payload with USA Contiguous Albers Equal-Area metre coords +
    # the corresponding RFC 7946-style crs member (USDM historical shape).
    albers_payload = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "EPSG:5070"},
        },
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [
                                [-500_000.0, 1_700_000.0],
                                [500_000.0, 1_700_000.0],
                                [500_000.0, 2_300_000.0],
                                [-500_000.0, 2_300_000.0],
                                [-500_000.0, 1_700_000.0],
                            ]
                        ]
                    ],
                },
                "properties": {
                    "OBJECTID": 1,
                    "DM": 2,
                    "Shape_Length": 4.0,
                    "Shape_Area": 1.2,
                },
            }
        ],
    }
    monkeypatch.setattr(
        backend_module, "_http_get_json", lambda url: albers_payload
    )
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        dataset="usdm",
    )
    fc = backend.download(progress_bar=False)
    assert fc.crs.to_epsg() == 4326
    assert len(fc) == 1
    # The Albers metre coords map to ~ Texas/Oklahoma — well inside the
    # CONUS bbox, so the polygon survives the clip (proves the reproject ran).


def test_crs_from_geojson_handles_variants():
    """Each accepted `crs` member shape returns an EPSG:NNNN string."""
    f = backend_module._crs_from_geojson
    assert f({"crs": {"type": "name", "properties": {"name": "EPSG:5070"}}}) == "EPSG:5070"
    assert f({"crs": {"type": "EPSG", "properties": {"code": 4326}}}) == "EPSG:4326"
    assert (
        f({"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5070"}}})
        == "EPSG:5070"
    )
    # Missing crs member → RFC 7946 default.
    assert f({"type": "FeatureCollection", "features": []}) == "EPSG:4326"
    # Malformed crs blocks (non-dict properties / non-dict crs / missing
    # name+code) must all degrade to the RFC 7946 default, not raise.
    assert f({"crs": "EPSG:4326"}) == "EPSG:4326"  # crs is a str
    assert f({"crs": {"type": "name", "properties": "EPSG:4326"}}) == "EPSG:4326"
    assert f({"crs": {"type": "name", "properties": ["EPSG:4326"]}}) == "EPSG:4326"
    assert f({"crs": {"type": "name", "properties": {}}}) == "EPSG:4326"
    assert f({"crs": {"type": "name", "properties": {"name": None}}}) == "EPSG:4326"


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


def test_edo_render_wcs_url_carries_custom_params():
    """The EDO/GDO GetCoverage URL carries TIME + SELECTED_TIMESCALE + bbox."""
    url = Drought._render_wcs_url(
        "https://drought.emergency.copernicus.eu/api/wcs?map=DO_WCS",
        coverage="spaST",
        timescale="03",
        period=dt.date(2025, 12, 21),
        bbox=(5.0, 40.0, 15.0, 50.0),
    )
    assert "coverageID=spaST" in url
    assert "TIME=2025-12-21" in url
    assert "SELECTED_TIMESCALE=03" in url
    assert "SUBSET=Long(5.0,15.0)" in url
    assert "SUBSET=Lat(40.0,50.0)" in url
    assert "format=GEOTIFF" in url
    # The endpoint already has a query string, so the join uses `&`.
    assert "map=DO_WCS&SERVICE=WCS" in url


def test_edo_render_wcs_url_omits_timescale_when_none():
    """A None timescale leaves SELECTED_TIMESCALE off the URL entirely."""
    url = Drought._render_wcs_url(
        "https://example.com/wcs?map=DO_WCS",
        coverage="twsan",
        timescale=None,
        period=dt.date(2024, 6, 21),
        bbox=(0.0, 0.0, 1.0, 1.0),
    )
    assert "SELECTED_TIMESCALE" not in url


def test_edo_render_wcs_url_rejects_missing_coverage():
    """An edo-wcs row without a coverage id is a clear ValueError."""
    with pytest.raises(ValueError, match="must carry a `coverage`"):
        Drought._render_wcs_url(
            "https://example.com/wcs",
            coverage=None,
            timescale="01",
            period=dt.date(2024, 6, 21),
            bbox=(0.0, 0.0, 1.0, 1.0),
        )


def test_edo_fetch_writes_one_tif_per_period(monkeypatch, tmp_path):
    """The EDO route streams a GeoTIFF per period and validates via read_file."""
    fetched: list[tuple[str, str]] = []

    def _fake_download(url, target, *, label):
        fetched.append((url, label))
        Path(target).write_bytes(b"MM\x00*FAKE-GEOTIFF")

    read_calls: list[str] = []

    class _FakeDataset:
        @classmethod
        def read_file(cls, path):
            read_calls.append(path)
            return _FakeDataset()

        def close(self):
            pass

    import pyramids.dataset as dataset_mod

    monkeypatch.setattr(backend_module, "_http_download_raster", _fake_download)
    monkeypatch.setattr(dataset_mod.Dataset, "read_file", _FakeDataset.read_file)

    backend = Drought(
        start="2025-12-01",
        end="2025-12-31",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="edo-spaST",
        path=str(tmp_path),
    )
    paths = backend.download(progress_bar=False)
    assert backend.OUTPUT_KIND == "raster"
    assert all(p.suffix == ".tif" for p in paths)
    assert all(p.name.startswith("edo-spaST_") for p in paths)
    # Every fetched URL is the EDO map endpoint with the SPI timescale.
    assert fetched, "at least one period fetched"
    for url, label in fetched:
        assert label == "edo-spaST"
        assert "map=DO_WCS" in url and "SELECTED_TIMESCALE=01" in url
    # read_file was called once per written tif (the raster validation).
    assert len(read_calls) == len(paths)


def test_gdo_fetch_uses_the_single_do_wcs_map(monkeypatch, tmp_path):
    """A GDO row routes through the same `map=DO_WCS` map as EDO."""
    seen: list[str] = []

    def _fake_download(url, target, *, label):
        seen.append(url)
        Path(target).write_bytes(b"MM\x00*FAKE")

    class _FakeDataset:
        @classmethod
        def read_file(cls, path):
            return _FakeDataset()

        def close(self):
            pass

    import pyramids.dataset as dataset_mod

    monkeypatch.setattr(backend_module, "_http_download_raster", _fake_download)
    monkeypatch.setattr(dataset_mod.Dataset, "read_file", _FakeDataset.read_file)

    backend = Drought(
        start="2024-06-21",
        end="2024-06-21",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="gdo-smand",
        path=str(tmp_path),
    )
    backend.download(progress_bar=False)
    assert seen and all("map=DO_WCS" in u for u in seen)
    assert all("GDO_WCS" not in u for u in seen)


def test_edo_fetch_surfaces_copernicus_error(monkeypatch, tmp_path):
    """A server rejection (out-of-range date) surfaces the Copernicus message."""
    import requests

    class _FakeResp:
        status_code = 422

        def json(self):
            return {"message": "Requested date is outside the available range."}

        @property
        def text(self):
            return "{...}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())
    backend = Drought(
        start="2026-06-21",
        end="2026-06-21",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="edo-cdinx",
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="outside the available range"):
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

    closed: list[Path] = []

    def __init__(self, target: Path):
        self._target = target

    def to_file(self, path: str) -> None:
        Path(path).write_bytes(b"FAKE-GTIFF-" + str(self._target).encode())

    def close(self) -> None:
        _FakeSubset.closed.append(self._target)


class _FakeNetCDF:
    """Stand-in for `pyramids.netcdf.NetCDF` — records `subset()` calls."""

    calls: list[dict[str, Any]] = []
    # Sized so the happy-path SPEIbase tests at 2026 dates (idx ≈ 1505)
    # land inside the axis, but the overflow-guard test at 2099-06
    # (idx 2381) does not. Any value in (1506, 2381) works; 2000 is a
    # round number comfortably past 2026 without reaching 2099.
    n_time: int = 2000

    def __init__(self, path: str):
        self.path = path

    @property
    def dimension_sizes(self) -> dict[str, int]:
        return {"time": _FakeNetCDF.n_time}

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
    """Replace `pyramids.netcdf.NetCDF.read_file` with a `_FakeNetCDF`.

    Snapshots and restores `_FakeNetCDF.n_time` alongside `.calls` so a
    test that monkeys with the time axis length (the G3 missing-axis +
    capitalised-axis tests do this through `monkeypatch.setattr`) cannot
    leak its mutation into the rest of the file — `_FakeNetCDF.n_time` is
    a class-level int and `dimension_sizes` reads `_FakeNetCDF.n_time`,
    not `self.n_time`, so a stale value would silently break every
    subsequent SPEIbase test.
    """
    import pyramids.netcdf as netcdf_module

    monkeypatch.setattr(
        netcdf_module.NetCDF,
        "read_file",
        classmethod(lambda cls, path: _FakeNetCDF(str(path))),
    )
    saved_n_time = _FakeNetCDF.n_time
    _FakeNetCDF.calls.clear()
    _FakeSubset.closed.clear()
    yield _FakeNetCDF
    _FakeNetCDF.calls.clear()
    _FakeSubset.closed.clear()
    _FakeNetCDF.n_time = saved_n_time


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
    # Each per-period subset Dataset is closed after it is written.
    assert len(_FakeSubset.closed) == 2


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


def test_speibase_rejects_missing_time_axis(monkeypatch, tmp_path, fake_netcdf):
    """An empty / time-less dimension_sizes raises instead of silently bypassing."""
    monkeypatch.setattr(
        backend_module,
        "_http_download",
        lambda url, target: target.write_bytes(b"fake-nc"),
    )
    # `fake_netcdf` IS the `_FakeNetCDF` class object — patch the property
    # on the class itself, and restore it in `finally` so the rest of the
    # suite sees the fixture's default `n_time` axis again.
    monkeypatch.setattr(
        fake_netcdf, "dimension_sizes", property(lambda self: {})
    )
    backend = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="no discoverable time axis"):
        backend.download(progress_bar=False)


def test_speibase_handles_capitalised_time_dim(monkeypatch, tmp_path, fake_netcdf):
    """`Time` / `T` / `t` axis names resolve through the case-insensitive lookup."""
    monkeypatch.setattr(
        backend_module,
        "_http_download",
        lambda url, target: target.write_bytes(b"fake-nc"),
    )
    monkeypatch.setattr(
        fake_netcdf, "dimension_sizes", property(lambda self: {"Time": 2000})
    )
    backend = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    # Should NOT raise — the case-insensitive lookup finds "Time".
    backend.download(progress_bar=False)


def test_speibase_rejects_period_past_time_axis(monkeypatch, tmp_path, fake_netcdf):
    """A period past the bundled NetCDF's time axis raises clearly."""
    monkeypatch.setattr(
        backend_module,
        "_http_download",
        lambda url, target: target.write_bytes(b"fake-nc"),
    )
    # 1500 months from 1901-01 → up to 2025-12; ask for 2099-06 to overshoot.
    backend = Drought(
        start="2099-06-01",
        end="2099-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="past the bundled time axis"):
        backend.download(progress_bar=False)


_LEAK_PATTERN = re.compile(r"\b(?:owslib|xarray)\b")


def _scan_for_leaky_imports(src_dir: Path) -> list[tuple[Path, str]]:
    """Walk every `.py` under `src_dir` and return any owslib/xarray import.

    Matches both module-level (`import owslib`) and indented / lazy
    (`    from owslib.wcs import ...`) imports by `lstrip`-ing each line
    before the `import `/`from ` prefix check — the drought backend uses
    indented lazy imports throughout (every pyramids import lives at
    column ≥4 inside a `_fetch_*` helper), so a future indented
    `from owslib...` must not slip past.
    """
    offenders: list[tuple[Path, str]] = []
    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.lstrip()
            if not (
                stripped.startswith("import ")
                or stripped.startswith("from ")
            ):
                continue
            if _LEAK_PATTERN.search(line):
                offenders.append((py_file, line.strip()))
                break
    return offenders


def test_owslib_xarray_guard_catches_indented_imports(tmp_path):
    """The shared scanner must catch indented (lazy) imports, not just col-0.

    Exercise the SAME helper the production guard uses, so any future
    weakening of `_scan_for_leaky_imports` (e.g. dropping the lstrip)
    breaks this meta-test loud and clear — rather than the production
    guard quietly accepting a leak while a private copy of its logic
    continues to claim it does not.
    """
    leak = tmp_path / "leak.py"
    leak.write_text(
        "def _fetch_wcs():\n"
        "    from owslib.wcs import WebCoverageService\n"
        "    return WebCoverageService\n",
        encoding="utf-8",
    )
    offenders = _scan_for_leaky_imports(tmp_path)
    assert offenders, "the indented-import guard must catch a lazy `from owslib...`"


def test_drought_src_does_not_import_owslib_or_xarray():
    """earthlens drought src must never import owslib or xarray (`G7`).

    Catches both module-level and indented (lazy) imports — the backend
    uses indented imports pervasively (`from pyramids.netcdf import
    NetCDF` inside `_fetch_speibase`), so a future indented
    `from owslib.wcs import WebCoverageService` inside `_fetch_wcs` (or
    `import xarray as xr` inside any helper) must not slip past this
    guard.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "earthlens" / "drought"
    offenders = _scan_for_leaky_imports(src)
    assert offenders == [], (
        f"owslib/xarray import leak — drought has no SDK extra: {offenders}"
    )


class _FakeResponse:
    """Minimal `requests.Response` stand-in for the HTTP helpers."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        json_body: Any | None = None,
    ):
        self.status_code = status
        self._body = body
        self._json = json_body
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size: int = 1024):
        view = memoryview(self._body)
        for offset in range(0, len(view), chunk_size):
            yield bytes(view[offset : offset + chunk_size])

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def test_http_get_json_decodes_payload(monkeypatch):
    """`_http_get_json` raises for status then returns the JSON body."""
    import requests

    payload = {"hello": "world"}

    def _fake_get(url, timeout, headers):
        assert "User-Agent" in headers
        return _FakeResponse(json_body=payload)

    monkeypatch.setattr(requests, "get", _fake_get)
    assert backend_module._http_get_json("https://example.com") == payload


def test_http_get_json_raises_on_http_error(monkeypatch):
    """A non-2xx response surfaces as `requests.HTTPError`."""
    import requests

    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout, headers: _FakeResponse(status=500, json_body=None),
    )
    with pytest.raises(requests.HTTPError):
        backend_module._http_get_json("https://example.com/oops")


def test_http_download_streams_to_target(monkeypatch, tmp_path):
    """`_http_download` writes the full body then atomically renames."""
    import requests

    body = b"x" * (1 << 17)  # two chunks at the 64 KiB stream size

    def _fake_get(url, timeout, stream, headers):
        assert stream is True
        return _FakeResponse(body=body)

    monkeypatch.setattr(requests, "get", _fake_get)
    target = tmp_path / "nested" / "file.nc"
    backend_module._http_download("https://example.com/file.nc", target)
    assert target.exists()
    assert target.read_bytes() == body
    assert not (target.with_suffix(target.suffix + ".partial").exists())


def test_http_download_raster_streams_on_success(monkeypatch, tmp_path):
    """`_http_download_raster` writes a 2xx body atomically to the target."""
    import requests

    body = b"MM\x00*" + b"y" * (1 << 17)
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse(status=200, body=body)
    )
    target = tmp_path / "edo.tif"
    backend_module._http_download_raster(
        "https://example.com/wcs", target, label="edo-spaST"
    )
    assert target.read_bytes() == body
    assert not target.with_suffix(target.suffix + ".partial").exists()


def test_http_download_raster_surfaces_json_message(monkeypatch, tmp_path):
    """A 4xx with a JSON `message` is re-raised as a clear ValueError."""
    import requests

    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResponse(
            status=422, json_body={"message": "date outside coverage range"}
        ),
    )
    with pytest.raises(ValueError, match="date outside coverage range"):
        backend_module._http_download_raster(
            "https://example.com/wcs", tmp_path / "x.tif", label="edo-cdinx"
        )


def test_http_download_raster_falls_back_to_raw_body(monkeypatch, tmp_path):
    """A 5xx with a non-JSON body still surfaces the raw text (no crash)."""
    import requests

    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResponse(status=500, body=b"upstream mapserver error"),
    )
    with pytest.raises(ValueError, match="upstream mapserver error"):
        backend_module._http_download_raster(
            "https://example.com/wcs", tmp_path / "x.tif", label="edo-fpanv"
        )


def test_http_download_raster_rejects_non_raster_200(monkeypatch, tmp_path):
    """A 200 carrying a non-TIFF body (MapServer error) is rejected, not written.

    The Copernicus WCS answers an invalid `map=`/coverage with a plain-text
    `ERROR: invalid map parameter` body under HTTP 200; without the magic-byte
    guard it would slip through and reach `Dataset.read_file` as an opaque
    GDAL failure.
    """
    import requests

    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResponse(status=200, body=b"ERROR: invalid map parameter"),
    )
    target = tmp_path / "x.tif"
    with pytest.raises(ValueError, match="non-raster body"):
        backend_module._http_download_raster(
            "https://example.com/wcs", target, label="gdo-smand"
        )
    assert not target.exists(), "no file written for a non-raster response"


def test_usdm_reprojects_non_4326_payload(monkeypatch, tmp_path):
    """A payload arriving in EPSG:3857 is reprojected to 4326."""
    # Inject a payload through a custom `_geojson_to_gdf` shim that hands
    # back an EPSG:3857 frame — exercises the defensive `to_crs("EPSG:4326")`
    # branch (the live USDM ships 4326 today, but the guard must hold).
    original = backend_module.Drought._geojson_to_gdf

    def _wrap(payload, period):
        gdf = original(payload, period)
        if not len(gdf):
            return gdf
        return gdf.set_crs("EPSG:4326", allow_override=True).to_crs("EPSG:3857")

    monkeypatch.setattr(
        backend_module.Drought,
        "_geojson_to_gdf",
        staticmethod(_wrap),
    )
    monkeypatch.setattr(
        backend_module, "_http_get_json", lambda url: _USDM_PAYLOAD
    )
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        dataset="usdm",
        path=str(tmp_path),
    )
    fc = backend.download(progress_bar=False)
    assert fc.crs.to_epsg() == 4326
    assert len(fc) == 2


def test_empty_period_returns_empty_fc(monkeypatch, tmp_path):
    """A USDM payload with zero features returns the empty-schema FC."""
    monkeypatch.setattr(
        backend_module,
        "_http_get_json",
        lambda url: {"type": "FeatureCollection", "features": []},
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
    assert len(fc) == 0
    assert {"OBJECTID", "DM", "release_date"}.issubset(set(fc.columns))


def test_unknown_transport_on_row_raises(monkeypatch, tmp_path):
    """A row whose transport drifts out of sync surfaces a clear ValueError."""
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        path=str(tmp_path),
    )
    rogue = backend._dataset.model_copy(
        update={"transport": "future-tx"}, deep=False
    )
    monkeypatch.setattr(backend, "_dataset", rogue)
    with pytest.raises(ValueError, match="unknown drought transport"):
        backend._fetch(backend._search())
