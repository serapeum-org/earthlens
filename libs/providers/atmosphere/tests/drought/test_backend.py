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

import numpy as np
import pytest

import earthlens.drought
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


_RASTER_KWARGS = dict(
    start="2026-06-01",
    end="2026-06-01",
    lat_lim=[30.0, 40.0],
    lon_lim=[-95.0, -85.0],
    dataset="speibase-12",
)


@pytest.mark.parametrize(
    "cwd_path",
    ["", "   ", Path(""), Path("."), Path()],
    ids=["empty-str", "whitespace", "Path-empty-str", "Path-dot", "Path-default"],
)
def test_drought_raster_rejects_an_explicit_cwd_path(cwd_path):
    """Asking for the working directory explicitly is refused for raster.

    `bool(Path(''))` and `bool(Path())` are both True (pathlib defines no
    __bool__/__len__), so a `not path` check would silently route the parent
    class to `Path('.').absolute()` — the user's CWD.
    """
    with pytest.raises(ValueError, match="needs a real path="):
        Drought(**_RASTER_KWARGS, path=cwd_path)


def test_drought_raster_without_path_uses_the_configured_output_dir(tmp_path):
    """An omitted path is no longer an error: it resolves to the configured dir."""
    from earthlens.config import set_output_dir

    set_output_dir(tmp_path / "configured")
    backend = Drought(**_RASTER_KWARGS)
    assert backend.root_dir == (tmp_path / "configured").resolve(), (
        f"got {backend.root_dir}"
    )


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
    monkeypatch.setattr(backend_module, "_http_get_json", lambda url: _USDM_PAYLOAD)
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
    monkeypatch.setattr(backend_module, "_http_get_json", lambda url: _USDM_PAYLOAD)
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
    # Build a payload with projected metre coords + a matching RFC 7946-style
    # crs member (the non-4326 shape USDM historical files carry). Web Mercator
    # (EPSG:3857) is used deliberately: its transform to EPSG:4326 is a closed
    # form needing no PROJ datum grids, so the reproject behaves identically on
    # minimal PROJ builds (e.g. the conda-forge GDAL the wheel-test job installs)
    # as on the source env — a datum-shift CRS like EPSG:5070 drops the feature
    # when the grid is absent.
    mercator_payload = {
        "type": "FeatureCollection",
        "crs": {
            "type": "name",
            "properties": {"name": "EPSG:3857"},
        },
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [
                            [
                                [-10_000_000.0, 4_000_000.0],
                                [-9_000_000.0, 4_000_000.0],
                                [-9_000_000.0, 5_000_000.0],
                                [-10_000_000.0, 5_000_000.0],
                                [-10_000_000.0, 4_000_000.0],
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
    monkeypatch.setattr(backend_module, "_http_get_json", lambda url: mercator_payload)
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
    # The Web Mercator metre coords map to ~ the south-eastern US (lon -90..-81,
    # lat 34..41) — well inside the world bbox, so the polygon survives the clip
    # (proves the reproject ran).


def test_crs_from_geojson_handles_variants():
    """Each accepted `crs` member shape returns an EPSG:NNNN string."""
    f = backend_module._crs_from_geojson
    assert (
        f({"crs": {"type": "name", "properties": {"name": "EPSG:5070"}}}) == "EPSG:5070"
    )
    assert f({"crs": {"type": "EPSG", "properties": {"code": 4326}}}) == "EPSG:4326"
    assert (
        f(
            {
                "crs": {
                    "type": "name",
                    "properties": {"name": "urn:ogc:def:crs:EPSG::5070"},
                }
            }
        )
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
    """`aggregate=` is refused on the vector USDM route, by the shared gate."""
    backend = Drought(
        start="2026-06-23",
        end="2026-06-23",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
        path=str(tmp_path),
    )
    with pytest.raises(NotImplementedError, match="no gridded reduction"):
        backend.download(aggregate=object())


def test_edo_fetch_writes_one_tif_per_period(monkeypatch, tmp_path):
    """The EDO route fetches one coverage per period over WCS and crops it."""
    fetched: list[tuple[str, dict]] = []

    class _FakeDataset:
        # A full-width -180..180 strip, latitude-cropped like the real
        # Copernicus server returns — so the backend's `_clip_wcs_raster`
        # windows it down to the requested lon_lim.
        bbox = [-180.0, 40.0, 180.0, 50.0]
        no_data_value = (None,)

        @classmethod
        def from_wcs(cls, endpoint, **kwargs):
            fetched.append((endpoint, kwargs))
            return _FakeDataset()

        def read_array(self):
            return np.zeros((10, 360), dtype="uint8")

        def close(self):
            pass

    import pyramids.dataset as dataset_mod

    # `_clip_wcs_raster` writes the cropped output with a real, unpatched
    # `pyramids.dataset.Dataset` (its own internal import), so the file on
    # disk after `download()` is genuine — `from_wcs` is the only patched
    # entry point, so `read_file` below re-opens it for real.
    monkeypatch.setattr(dataset_mod.Dataset, "from_wcs", _FakeDataset.from_wcs)

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
    # Every fetch hits the EDO map endpoint and carries the SPI timescale.
    assert fetched, "at least one period fetched"
    for endpoint, kwargs in fetched:
        assert "map=DO_WCS" in endpoint
        params = kwargs["extra_params"]
        assert params["SELECTED_TIMESCALE"] == "01"
        assert params["coverageID"] == "spaST"
        # The shim needs the WCS-1.x CRS= and a format=, or it 500s.
        assert params["CRS"] == "EPSG:4326"
        assert kwargs["wcs_format"] == "GEOTIFF"
        assert kwargs["direct"] is True
    # The final on-disk file is the real, cropped GeoTIFF -- not just the
    # -180..180 strip `_FakeDataset` reports -- so re-open it and confirm the
    # crop actually landed.
    written = dataset_mod.Dataset.read_file(str(paths[0]))
    assert written.bbox == pytest.approx([5.0, 40.0, 15.0, 50.0])
    written.close()


def test_gdo_fetch_uses_the_single_do_wcs_map(monkeypatch, tmp_path):
    """A GDO row routes through the same `map=DO_WCS` map as EDO."""
    seen: list[str] = []

    class _FakeDataset:
        bbox = [-180.0, 40.0, 180.0, 50.0]
        no_data_value = (None,)

        @classmethod
        def from_wcs(cls, endpoint, **kwargs):
            seen.append(endpoint)
            return _FakeDataset()

        def read_array(self):
            return np.zeros((10, 360), dtype="uint8")

        def close(self):
            pass

    import pyramids.dataset as dataset_mod

    monkeypatch.setattr(dataset_mod.Dataset, "from_wcs", _FakeDataset.from_wcs)

    backend = Drought(
        start="2024-06-21",
        end="2024-06-21",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="gdo-smand",
        path=str(tmp_path),
    )
    backend.download(progress_bar=False)
    assert seen, "no GetCoverage URL was captured"
    assert all("map=DO_WCS" in u for u in seen), seen
    assert all("GDO_WCS" not in u for u in seen)


def test_clip_wcs_raster_trims_full_width_strip_to_bbox():
    """`_clip_wcs_raster` windows a global-width WCS strip down to the bbox.

    Regression: the Copernicus EDO/GDO server honours the `Lat` subset but
    ignores `Long`, returning a full -180..180 strip with no embedded SRS, so
    the backend must clip longitude locally rather than trust the server.
    """
    from pyramids.dataset import Dataset

    # 64 rows over lat 36..52, 1440 cols over lon -180..180 (0.25 deg cells).
    arr = np.arange(64 * 1440, dtype="float32").reshape(64, 1440)
    geo = (-180.0, 0.25, 0.0, 52.0, 0.0, -0.25)
    strip = Dataset.create_from_array(arr=arr, geo=geo, epsg=4326, no_data_value=None)

    clipped = Drought._clip_wcs_raster(strip, (-10.0, 36.0, 12.0, 52.0))

    assert clipped is not None
    assert [round(b, 2) for b in clipped.bbox] == [-10.0, 36.0, 12.0, 52.0]
    assert clipped.columns < strip.columns  # longitude trimmed
    assert clipped.rows == strip.rows  # latitude already full-height


def test_clip_wcs_raster_returns_none_when_bbox_outside_coverage():
    """A bbox entirely outside the raster yields `None` (original untouched)."""
    from pyramids.dataset import Dataset

    arr = np.zeros((4, 8), dtype="uint8")  # covers lon 0..8, lat 0..4
    geo = (0.0, 1.0, 0.0, 4.0, 0.0, -1.0)
    ds = Dataset.create_from_array(arr=arr, geo=geo, epsg=4326, no_data_value=None)

    assert Drought._clip_wcs_raster(ds, (20.0, 20.0, 30.0, 30.0)) is None


def test_fetch_wcs_warns_when_bbox_misses_downloaded_raster(monkeypatch, tmp_path):
    """`_fetch_wcs` logs a warning and keeps the unclipped file when the crop is a no-op."""
    from loguru import logger

    class _FakeDataset:
        # Covers lon 0..8, lat 0..4 -- nowhere near the requested lon_lim/lat_lim.
        bbox = [0.0, 0.0, 8.0, 4.0]
        no_data_value = (None,)

        @classmethod
        def from_wcs(cls, endpoint, **kwargs):
            return _FakeDataset()

        def read_array(self):
            return np.zeros((4, 8), dtype="uint8")

        def to_file(self, path):
            # The crop is a no-op, so the backend writes the server's own
            # coverage out unclipped rather than a cropped copy.
            Path(path).write_bytes(b"MM\x00*FAKE-GEOTIFF")

        def close(self):
            pass

    import pyramids.dataset as dataset_mod

    monkeypatch.setattr(dataset_mod.Dataset, "from_wcs", _FakeDataset.from_wcs)

    backend = Drought(
        start="2025-12-01",
        end="2025-12-31",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="edo-spaST",
        path=str(tmp_path),
    )
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        paths = backend.download(progress_bar=False)
    finally:
        logger.remove(sink_id)
    warnings = [m for m in messages if "unclipped" in m]
    assert len(warnings) == len(paths)
    assert "edo-spaST" in warnings[0]
    assert str(paths[0]) in warnings[0]
    # The original, un-cropped download is left in place rather than dropped.
    assert paths[0].exists()


def test_edo_fetch_rejects_a_row_without_a_coverage_id(monkeypatch, tmp_path):
    """An edo-wcs row carrying no `coverage` raises before any request goes out.

    The guard moved from the deleted `_render_wcs_url` into
    `_fetch_wcs_coverage`; this keeps it covered. `coverage` is `None` for the
    non-WCS transports, so a mis-catalogued row would otherwise send
    `coverageID=None` to Copernicus and get an opaque server error back.
    """
    import pyramids.dataset as dataset_mod

    def _unreachable(cls, endpoint, **kwargs):
        raise AssertionError("from_wcs must not be called without a coverage id")

    monkeypatch.setattr(dataset_mod.Dataset, "from_wcs", classmethod(_unreachable))
    backend = Drought(
        start="2026-06-21",
        end="2026-06-21",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="edo-cdiad",
        path=str(tmp_path),
    )
    # The catalog row is a frozen pydantic model, so swap in a copy rather
    # than assigning through it.
    monkeypatch.setattr(
        backend, "_dataset", backend._dataset.model_copy(update={"coverage": None})
    )
    with pytest.raises(ValueError, match="must carry a `coverage` id"):
        backend.download(progress_bar=False)


def test_edo_fetch_surfaces_copernicus_error(monkeypatch, tmp_path):
    """A server rejection (out-of-range date) surfaces the Copernicus message.

    The backend translates pyramids' `WCSError` — which is not a `ValueError` —
    into one, so the documented contract survives the WCS transport living in
    pyramids. The faked message mirrors the real shape: since pyramids 0.46.0
    the response body is embedded in the error text
    (serapeum-org/pyramids#744), which is what carries the Copernicus text.
    """
    import pyramids.dataset as dataset_mod
    from pyramids.errors import WCSError

    def _reject(cls, endpoint, **kwargs):
        raise WCSError(
            f"WCS GetCoverage request failed for {endpoint!r}: "
            "HTTP Error 422: Unprocessable Entity: "
            '{"message":"Requested date is outside the available range.",'
            '"code":"DATE_OUT_OF_RANGE"}'
        )

    monkeypatch.setattr(dataset_mod.Dataset, "from_wcs", classmethod(_reject))
    backend = Drought(
        start="2026-06-21",
        end="2026-06-21",
        lat_lim=[40.0, 50.0],
        lon_lim=[5.0, 15.0],
        dataset="edo-cdiad",
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="outside the available range"):
        backend.download(progress_bar=False)


def test_raster_aggregate_rejected_until_reducer_lands(tmp_path):
    """The raster `aggregate=` path is refused too, with the reducer named."""
    backend = Drought(
        start="2026-06-01",
        end="2026-06-01",
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    with pytest.raises(NotImplementedError, match="not wired yet"):
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
    monkeypatch.setattr(fake_netcdf, "dimension_sizes", property(lambda self: {}))
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
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
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
    src = Path(earthlens.drought.__file__).parent
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

    def close(self) -> None:
        """No-op — the fake holds no socket (HttpClient closes the stream)."""

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
    monkeypatch.setattr(backend_module, "_http_get_json", lambda url: _USDM_PAYLOAD)
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
    rogue = backend._dataset.model_copy(update={"transport": "future-tx"}, deep=False)
    monkeypatch.setattr(backend, "_dataset", rogue)
    with pytest.raises(ValueError, match="unknown drought transport"):
        backend._fetch(backend._search())


class TestLimitStopsTheWork:
    """A `limit=` caps the USDM polygons, and is refused where it cannot apply."""

    def _usdm(self, weeks: int = 3) -> Drought:
        """Build a USDM backend spanning `weeks` weekly periods."""
        end = dt.date(2026, 6, 1) + dt.timedelta(days=7 * (weeks - 1))
        return Drought(
            start="2026-06-01",
            end=end.strftime("%Y-%m-%d"),
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="usdm",
        )

    def _fake_period(self, backend, monkeypatch, fetched, rows: int = 3):
        """Record each period fetched and serve `rows` in-bbox polygons for it."""
        import geopandas as gpd
        from shapely.geometry import Polygon

        square = Polygon([(-94, 31), (-94, 32), (-93, 32), (-93, 31)])

        def fake_fetch(product, bbox):
            fetched.append(product.metadata["period"])
            return gpd.GeoDataFrame(
                {"dm": list(range(rows))},
                geometry=[square] * rows,
                crs="EPSG:4326",
            )

        monkeypatch.setattr(backend, "_fetch_usdm_period", fake_fetch)

    def test_weeks_past_the_cap_are_never_downloaded(self, monkeypatch):
        """The later weeks' GeoJSON is not requested once the cap is met."""
        backend = self._usdm(weeks=3)
        fetched: list[dt.date] = []
        self._fake_period(backend, monkeypatch, fetched)

        backend._limit = 4
        result = backend._fetch(backend._search())

        assert len(fetched) == 2, (
            f"downloaded {len(fetched)} weeks for a cap met by 2; the cap is "
            f"trimming, not stopping the work"
        )
        assert len(result) == 4

    def test_no_limit_downloads_every_week(self, monkeypatch):
        """Without a cap the whole window is swept."""
        backend = self._usdm(weeks=3)
        fetched: list[dt.date] = []
        self._fake_period(backend, monkeypatch, fetched)

        backend._limit = None
        result = backend._fetch(backend._search())

        assert len(fetched) == 3
        assert len(result) == 9

    def test_the_cap_counts_only_rows_inside_the_bbox(self, monkeypatch):
        """USDM publishes nationally, so the clip must precede the cap.

        With the cap applied to the raw national frame, `limit=3` filled up on
        polygons outside the request bbox and the final clip removed all of
        them — returning nothing while the weeks that did intersect were never
        fetched. Every polygon in the other fixtures sits inside the bbox, so
        only a frame mixing the two can see this.
        """
        import geopandas as gpd
        from shapely.geometry import Polygon

        backend = self._usdm(weeks=3)
        inside = Polygon([(-94, 31), (-94, 32), (-93, 32), (-93, 31)])
        outside = Polygon([(10, 50), (10, 51), (11, 51), (11, 50)])

        def fake_fetch(product, bbox):
            frame = gpd.GeoDataFrame(
                {"dm": [0, 1, 2, 3]},
                geometry=[outside, outside, outside, inside],
                crs="EPSG:4326",
            )
            return frame.cx[bbox[0] : bbox[2], bbox[1] : bbox[3]]

        monkeypatch.setattr(backend, "_fetch_usdm_period", fake_fetch)
        backend._limit = 3
        result = backend._fetch(backend._search())

        assert len(result) == 3, (
            f"got {len(result)} row(s) for limit=3; the cap counted polygons "
            f"outside the request bbox that the clip then discarded"
        )

    def test_a_cap_on_a_raster_transport_is_refused(self, tmp_path):
        """Raster transports write files, so a row cap is rejected, not ignored.

        Silently accepting it would be the worst outcome: the caller believes
        the request is bounded while every period is downloaded in full.
        """
        spei = Drought(
            start="2026-06-01",
            end="2026-06-01",
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="speibase-12",
            path=str(tmp_path),
        )
        with pytest.raises(ValueError, match="USDM .vector. transport only"):
            spei.download(progress_bar=False, limit=5)
