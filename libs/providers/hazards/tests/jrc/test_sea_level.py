"""Unit tests for the JRC sea-level (TWL) forecast paths — no network."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from loguru import logger as _backend_logger
from pyramids.dataset import Dataset as PyramidsDataset

from earthlens.jrc import JRC, Catalog, _helpers

pytestmark = pytest.mark.jrc


@pytest.fixture(autouse=True)
def _isolated_http_client():
    """Drop the cached `HttpClient` so one test's stub cannot leak into the next."""
    _clear_client_cache()
    yield
    _clear_client_cache()


def _clear_client_cache() -> None:
    """Drop the cached client if it is still the real (cached) helper."""
    clear = getattr(_helpers._client, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _offline_band_names(monkeypatch):
    """Keep band naming offline: it would otherwise open the real cube."""
    monkeypatch.setattr(
        _helpers,
        "band_valid_times",
        lambda url, steps: [f"step_{index + 1}" for index in range(steps)],
    )


_COASTAL_CSV = "GID_0,NAME_0,summary_TWL_1_10\nABW,Aruba,2\nNLD,Netherlands,9\n"


class _FakeHttp:
    """Fake jeodpp autoindex + CSV fetcher for one product's newest cycle."""

    def __init__(self, base_url, product, cycle, files, *, incomplete_hour=None):
        year, month, day, hour = cycle
        root = f"{base_url.rstrip('/')}/{product}"
        hours = [f"{hour}/"]
        if incomplete_hour is not None:
            hours.append(f"{incomplete_hour}/")
        self.tree = {
            f"{root}/": [f"{year}/"],
            f"{root}/{year}/": [f"{month}/"],
            f"{root}/{year}/{month}/": [f"{day}/"],
            f"{root}/{year}/{month}/{day}/": sorted(hours, reverse=True),
            f"{root}/{year}/{month}/{day}/{hour}/": ["endFls", *files],
        }
        if incomplete_hour is not None:
            self.tree[f"{root}/{year}/{month}/{day}/{incomplete_hour}/"] = list(files)

    def __call__(self, url):
        if url.endswith(".csv"):
            return _COASTAL_CSV
        key = url if url.endswith("/") else url + "/"
        return "".join(f'<a href="{n}">{n}</a>' for n in self.tree[key])


class _FakeVariable:
    """A gridded TWL variable whose window read encodes the pixel origin."""

    def __init__(self, columns=1440, rows=720, bands=16):
        self.columns = columns
        self.rows = rows
        self._bands = bands

    def read_array(self, window, masked=False):
        col_off, row_off, width, height = window
        array = np.full(
            (self._bands, height, width),
            float(col_off * 1000 + row_off),
            dtype="float32",
        )
        return np.ma.masked_array(array, mask=False) if masked else array


class _FakeTimeVariable:
    """The cube's CF `time` coordinate, in days since 1950-01-01."""

    def __init__(self, steps=16):
        self._steps = steps

    def read_array(self, *args, **kwargs):
        return np.arange(27996.0, 27996.0 + self._steps)


class _FakeContainer:
    """A NetCDF container yielding a fake data variable plus a `time` axis."""

    def __init__(self, variable):
        self._variable = variable

    def get_variable(self, name):
        if name == "time":
            return _FakeTimeVariable()
        return self._variable


def _fake_read_file(_url, variable=None):
    """Stand-in for `NetCDF.read_file` returning a fake container."""
    return _FakeContainer(variable if variable is not None else _FakeVariable())


class _FakeMaskedVariable(_FakeVariable):
    """A variable whose window read masks one cell with a numeric fill value."""

    def read_array(self, window, masked=False):
        _, _, width, height = window
        data = np.full((self._bands, height, width), 1.5, dtype="float32")
        mask = np.zeros_like(data, dtype=bool)
        mask[:, 0, 0] = True
        return np.ma.masked_array(data, mask=mask)


def _fake_read_file_masked(_url):
    """Stand-in for `NetCDF.read_file` returning a masked-cell variable."""
    return _FakeContainer(_FakeMaskedVariable())


class _ExplodingDataset:
    """A cropped dataset whose write fails, to exercise the staging rollback."""

    def to_file(self, path):
        Path(path).write_bytes(b"partial")
        raise OSError("disk full")

    def close(self):
        """Accept the backend's `close_quietly` call."""


# --------------------------------------------------------------------------- #
# Dataset resolution
# --------------------------------------------------------------------------- #
class TestDatasetResolution:
    """The dataset / product / representation selectors resolve correctly."""

    @pytest.mark.parametrize(
        ("kwargs", "expected", "output_kind"),
        [
            (
                dict(dataset="sea_level", product="medium_term"),
                "sea_level_medium_term",
                "raster",
            ),
            (
                dict(dataset="sea_level", product="subseasonal"),
                "sea_level_subseasonal",
                "raster",
            ),
            (
                dict(
                    dataset="sea_level", product="subseasonal", representation="coastal"
                ),
                "sea_level_subseasonal_coastal",
                "tabular",
            ),
            (dict(dataset="sea_level_medium_term"), "sea_level_medium_term", "raster"),
        ],
    )
    def test_resolves(self, kwargs, expected, output_kind):
        """Each selector combination resolves to its catalog id + OUTPUT_KIND."""
        backend = JRC(lat_lim=[51.0, 53.0], lon_lim=[3.0, 5.0], **kwargs)
        assert backend._dataset.id == expected
        assert backend.OUTPUT_KIND == output_kind

    def test_coastal_medium_term_rejected(self):
        """The coastal representation is only offered for the subseasonal product."""
        with pytest.raises(ValueError, match="coastal"):
            JRC(dataset="sea_level", product="medium_term", representation="coastal")

    def test_unknown_product_rejected(self):
        """An unknown product is rejected with a clear message."""
        with pytest.raises(ValueError, match="product"):
            JRC(
                dataset="sea_level",
                product="seasonal",
                lat_lim=[51.0, 53.0],
                lon_lim=[3.0, 5.0],
            )

    def test_unknown_representation_rejected(self):
        """An unknown representation is rejected."""
        with pytest.raises(ValueError, match="representation"):
            JRC(
                dataset="sea_level",
                product="subseasonal",
                representation="bogus",
                lat_lim=[51.0, 53.0],
                lon_lim=[3.0, 5.0],
            )

    def test_gridded_requires_bbox(self):
        """A gridded request without a bounding box raises."""
        with pytest.raises(ValueError, match="bounding box"):
            JRC(dataset="sea_level", product="medium_term")


# --------------------------------------------------------------------------- #
# Cycle resolution (autoindex walk + endFls gate)
# --------------------------------------------------------------------------- #
class TestCycleResolution:
    """`resolve_cycle` / `find_cycle_file` against a fake autoindex."""

    def _http(self, **kw):
        return _FakeHttp(
            "https://x/root",
            "medium_term_forecasts",
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_202608261200-202609101200.nc"],
            **kw,
        )

    def test_latest_picks_newest_complete(self):
        """'latest' descends to the newest cycle carrying the endFls sentinel."""
        url, cycle_id = _helpers.resolve_cycle(
            "https://x/root",
            "medium_term_forecasts",
            "%Y/%m/%d/%H",
            "latest",
            "endFls",
            http_text=self._http(),
        )
        assert url.endswith("/2026/08/26/12/")
        assert cycle_id == "20260826T12"

    def test_latest_backtracks_past_incomplete(self):
        """A newer hour without endFls is skipped for the older complete one."""
        url, cycle_id = _helpers.resolve_cycle(
            "https://x/root",
            "medium_term_forecasts",
            "%Y/%m/%d/%H",
            "latest",
            "endFls",
            http_text=self._http(incomplete_hour="18"),
        )
        assert url.endswith("/2026/08/26/12/")

    def test_explicit_reference_time(self):
        """An explicit cycle resolves to its folder when complete."""
        url, cycle_id = _helpers.resolve_cycle(
            "https://x/root",
            "medium_term_forecasts",
            "%Y/%m/%d/%H",
            "2026-08-26T12",
            "endFls",
            http_text=self._http(),
        )
        assert cycle_id == "20260826T12"

    def test_incomplete_cycle_raises(self):
        """An explicit cycle without endFls raises rather than returning it."""
        http = _FakeHttp(
            "https://x/root",
            "medium_term_forecasts",
            ("2026", "08", "26", "00"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        # Overwrite the leaf so it has no endFls.
        http.tree["https://x/root/medium_term_forecasts/2026/08/26/00/"] = [
            "mediumTermTWLforecastGridded_x.nc"
        ]
        with pytest.raises(ValueError, match="not complete"):
            _helpers.resolve_cycle(
                "https://x/root",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "2026-08-26T00",
                "endFls",
                http_text=http,
            )

    def test_find_cycle_file_matches_glob(self):
        """The real data filename is read from the listing, not reconstructed."""
        name = _helpers.find_cycle_file(
            "https://x/root/medium_term_forecasts/2026/08/26/12/",
            "*TWLforecastGridded_*.nc",
            http_text=self._http(),
        )
        assert name == "mediumTermTWLforecastGridded_202608261200-202609101200.nc"


# --------------------------------------------------------------------------- #
# Affine reconstruction helpers
# --------------------------------------------------------------------------- #
class TestAffineHelpers:
    """`grid_geotransform` / `pixel_window` / `window_origin`."""

    def test_grid_geotransform_is_global_north_up(self):
        """The reconstructed affine is the global 0.25 deg north-up transform."""
        assert _helpers.grid_geotransform(1440, 720) == (
            -180.0,
            0.25,
            0.0,
            90.0,
            0.0,
            -0.25,
        )

    def test_affine_maps_corners_to_real_world_degrees(self):
        """The affine maps pixel corners to the true global lon/lat bounds."""
        cols, rows = 1440, 720
        x0, dx, _, y0, _, dy = _helpers.grid_geotransform(cols, rows)
        assert (x0, y0) == (-180.0, 90.0), "grid must start at the NW corner"
        assert x0 + cols * dx == pytest.approx(180.0), "east edge must reach +180"
        assert y0 + rows * dy == pytest.approx(-90.0), "south edge must reach -90"
        assert dy < 0, "row order must be north-up (a S/N flip inverts dy)"

    @pytest.mark.parametrize(
        ("bbox", "expected_origin"),
        [
            ((3.0, 51.0, 5.0, 53.0), (3.0, 53.0)),
            ((-180.0, 88.0, -178.0, 90.0), (-180.0, 90.0)),
            ((178.0, -90.0, 180.0, -88.0), (178.0, -88.0)),
        ],
    )
    def test_window_origin_is_the_bbox_nw_corner(self, bbox, expected_origin):
        """A window's origin is the NW corner of the requested box, in degrees."""
        geo = _helpers.grid_geotransform(1440, 720)
        col_off, row_off, _, _ = _helpers.pixel_window(geo, bbox, 1440, 720)
        origin = _helpers.window_origin(geo, col_off, row_off)
        assert (origin[0], origin[3]) == pytest.approx(expected_origin), (
            f"window origin {origin[:4]} should be the NW corner {expected_origin}"
        )

    def test_pixel_window_maps_bbox(self):
        """A bbox maps to the expected clamped pixel window."""
        geo = _helpers.grid_geotransform(1440, 720)
        assert _helpers.pixel_window(geo, (3.0, 51.0, 5.0, 53.0), 1440, 720) == (
            732,
            148,
            8,
            8,
        )

    def test_pixel_window_none_when_degenerate(self):
        """A zero-area bbox yields no window."""
        geo = _helpers.grid_geotransform(1440, 720)
        assert _helpers.pixel_window(geo, (3.0, 51.0, 3.0, 51.0), 1440, 720) is None

    def test_window_origin_shifts_to_corner(self):
        """The window origin is the bbox top-left in degrees, not index space."""
        geo = _helpers.grid_geotransform(1440, 720)
        assert _helpers.window_origin(geo, 732, 148) == (
            3.0,
            0.25,
            0.0,
            53.0,
            0.0,
            -0.25,
        )


# --------------------------------------------------------------------------- #
# Gridded + coastal fetch (fakes → real pyramids write)
# --------------------------------------------------------------------------- #
class TestGriddedFetch:
    """The gridded path writes a georeferenced multi-band GeoTIFF."""

    def test_writes_affine_correct_geotiff(self, tmp_path: Path, monkeypatch):
        """The crop is georeferenced in degrees (guards the index-space regression)."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_202608261200-202609101200.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _fake_read_file)

        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            reference_time="latest",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        paths = backend.download()
        assert len(paths) == 1, f"expected one output, got {paths}"
        assert paths[0].exists(), f"{paths[0]} was not written"

        written = PyramidsDataset.read_file(str(paths[0]))
        assert written.band_count == 16
        assert written.epsg == 4326
        origin_x, cell, _, origin_y, _, _ = written.geotransform
        # degrees, not index space
        assert cell == pytest.approx(0.25), f"cell size should be 0.25 deg, got {cell}"
        assert cell != 1.0, "an index-space affine leaked into the output"
        assert origin_x == pytest.approx(3.0, abs=0.25)
        assert origin_y == pytest.approx(53.0, abs=0.25)
        # the window origin (col 732, row 148) is encoded into the data
        assert float(732 * 1000 + 148) in set(
            np.asarray(written.read_array())[0].ravel()
        )


class TestCoastalFetch:
    """The coastal path returns the parsed per-country DataFrame."""

    def test_returns_dataframe(self, monkeypatch):
        """`download()` returns a `DataFrame` for the coastal representation."""
        row = Catalog().get("sea_level_subseasonal_coastal")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "24", "00"),
            ["subSeasonalCoastalForecast_202608240000-202610090000.csv"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr(
            _helpers, "http_bytes", lambda url: http(url).encode("utf-8")
        )

        backend = JRC(
            dataset="sea_level",
            product="subseasonal",
            representation="coastal",
            reference_time="latest",
        )
        result = backend.download()
        assert isinstance(result, pd.DataFrame)
        assert list(result["GID_0"]) == ["ABW", "NLD"]
        assert backend.OUTPUT_KIND == "tabular"


# --------------------------------------------------------------------------- #
# Cross-cutting guards
# --------------------------------------------------------------------------- #
class TestGridVerification:
    """`verify_grid_against_coordinates` guards the reconstructed affine (M3)."""

    def test_mismatched_coordinate_size_raises(self, monkeypatch):
        """Coordinates that disagree with the variable's shape are rejected."""
        monkeypatch.setattr(
            _helpers, "_read_grid_coordinates", lambda url: (np.zeros(10), np.zeros(5))
        )
        with pytest.raises(ValueError, match="do not match the variable"):
            _helpers.verify_grid_against_coordinates(
                "u", _helpers.grid_geotransform(1440, 720), 1440, 720
            )

    def test_non_global_extent_raises(self, monkeypatch):
        """A cube that is not the assumed global grid is rejected, not cropped."""
        lon = np.linspace(0.125, 359.875, 1440)  # a 0..360 grid, not -180..180
        lat = np.linspace(-89.875, 89.875, 720)
        monkeypatch.setattr(_helpers, "_read_grid_coordinates", lambda url: (lon, lat))
        with pytest.raises(ValueError, match="contradicts the assumed global grid"):
            _helpers.verify_grid_against_coordinates(
                "u", _helpers.grid_geotransform(1440, 720), 1440, 720
            )

    def test_matching_global_grid_passes(self, monkeypatch):
        """The real global 0.25 deg coordinates satisfy the reconstruction."""
        lon = np.linspace(-179.875, 179.875, 1440)
        lat = np.linspace(-89.875, 89.875, 720)
        monkeypatch.setattr(_helpers, "_read_grid_coordinates", lambda url: (lon, lat))
        _helpers.verify_grid_against_coordinates(
            "u", _helpers.grid_geotransform(1440, 720), 1440, 720
        )

    def test_unreadable_coordinates_are_tolerated(self, monkeypatch):
        """A cube without readable coordinates skips the check rather than failing."""
        monkeypatch.setattr(_helpers, "_read_grid_coordinates", lambda url: None)
        _helpers.verify_grid_against_coordinates(
            "u", _helpers.grid_geotransform(1440, 720), 1440, 720
        )


class TestNetworkSeams:
    """The byte fetch, the non-404 re-raise and the budget entry guard."""

    def test_http_bytes_returns_raw_body(self, monkeypatch):
        """`http_bytes` hands back undecoded bytes for the CSV parser."""

        class _Resp:
            content = ("GID_0\nAland\n").encode("utf-8")

        class _Client:
            def get(self, url, **kwargs):
                return _Resp()

        monkeypatch.setattr(_helpers, "_client", lambda: _Client())
        body = _helpers.http_bytes("https://x/f.csv").decode("utf-8")
        assert body == "GID_0\nAland\n"

    def test_non_404_error_is_reraised(self):
        """A 5xx on a pinned cycle is a server fault, not an aged-out cycle."""
        import requests

        def _boom(url):
            response = requests.Response()
            response.status_code = 503
            raise requests.HTTPError("503 Server Error", response=response)

        with pytest.raises(requests.HTTPError):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "2026-08-26T12",
                "endFls",
                http_text=_boom,
            )

    def test_exhausted_budget_stops_immediately(self):
        """A walk entered with no budget left issues no request at all."""
        calls = {"n": 0}

        def _counting(url):
            calls["n"] += 1
            return ""

        assert (
            _helpers._descend_newest("https://x/", 4, "endFls", _counting, [0]) is None
        )
        assert calls["n"] == 0, "an exhausted budget must not issue a request"


class TestWindowGuard:
    """The gridded read refuses an AOI that would materialise too much (M9)."""

    def test_oversized_window_is_refused(self, tmp_path: Path, monkeypatch):
        """A window above the cell guard raises instead of allocating."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _fake_read_file)
        monkeypatch.setattr(
            _helpers, "verify_grid_against_coordinates", lambda *a, **k: None
        )
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            path=tmp_path,
        )
        monkeypatch.setattr(type(backend), "MAX_WINDOW_CELLS", 100)
        with pytest.raises(ValueError, match="cell guard"):
            backend.download()


class TestFacadeKeys:
    """The sea-level facade keys resolve to the right dataset and shape (M7)."""

    @pytest.mark.parametrize(
        ("key", "extra", "dataset_id", "output_kind", "polygon"),
        [
            (
                "sea-level-forecast",
                {"product": "medium_term"},
                "sea_level_medium_term",
                "raster",
                True,
            ),
            (
                "sea-level-forecast",
                {"product": "subseasonal"},
                "sea_level_subseasonal",
                "raster",
                True,
            ),
            ("jrc-sea-level", {}, "sea_level_medium_term", "raster", True),
            ("twl-forecast", {}, "sea_level_medium_term", "raster", True),
            ("coastal-forecast", {}, "sea_level_subseasonal_coastal", "tabular", False),
            ("efhm", {"return_periods": [100]}, "efhm", "raster", True),
        ],
    )
    def test_key_routes_to_dataset(self, key, extra, dataset_id, output_kind, polygon):
        """Each facade key builds the right JRC dataset with the right output shape."""
        from earthlens.core import EarthLens

        kwargs = {"data_source": key, **extra}
        if output_kind == "raster":
            kwargs |= {"lat_lim": [51.0, 53.0], "lon_lim": [3.0, 5.0]}
        backend = EarthLens(**kwargs).datasource
        assert backend._dataset.id == dataset_id, (
            f"{key} routed to {backend._dataset.id}"
        )
        assert backend.OUTPUT_KIND == output_kind
        assert backend.SUPPORTS_POLYGON_AOI is polygon, (
            f"{key} polygon support should be {polygon}"
        )


class TestGuards:
    """Aggregate rejection, licence, and the no-xarray rule."""

    def test_aggregate_rejected(self):
        """A non-None aggregate= is refused (no reducible time axis)."""
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
        )
        with pytest.raises(NotImplementedError, match="aggregate="):
            backend.download(aggregate="mean")

    def test_licence_is_permissive(self):
        """The catalog records the shared CC-BY-4.0 licence."""
        assert Catalog().license_id == "CC-BY-4.0"

    def test_sea_level_rows_carry_attribution(self):
        """Each sea-level row states its (pending-citation) attribution."""
        catalog = Catalog()
        for key in (
            "sea_level_medium_term",
            "sea_level_subseasonal",
            "sea_level_subseasonal_coastal",
        ):
            assert "JRC" in catalog.get(key).attribution

    def test_no_xarray_in_src(self):
        """The JRC backend never imports xarray (raster read/crop is pyramids)."""
        import earthlens.jrc as package

        for module in Path(package.__file__).parent.glob("*.py"):
            assert "import xarray" not in module.read_text(encoding="utf-8"), (
                module.name
            )


class TestHelperEdges:
    """Edge cases in the sea-level helpers (raises + fallbacks)."""

    def test_http_text_reads_through_the_shared_client(self, monkeypatch):
        """`_http_text` fetches through the shared `HttpClient`, not bare requests."""
        seen = {}

        class _Resp:
            text = '<a href="12/">12/</a>'

        class _Client:
            def __init__(self, **kwargs):
                seen["timeout"] = kwargs.get("timeout")

            def get(self, url, **kwargs):
                seen["url"] = url
                return _Resp()

        monkeypatch.setattr(_helpers, "_client", lambda: _Client())
        assert "12/" in _helpers._http_text("https://x/")
        assert seen["url"] == "https://x/", f"unexpected URL fetched: {seen}"

    def test_shared_client_is_reused_and_has_a_timeout(self):
        """One cached client serves every request, and it carries a timeout."""
        _helpers._client.cache_clear()
        client = _helpers._client()
        assert _helpers._client() is client, "the client must be reused, not rebuilt"
        assert getattr(client, "timeout", None), "the client needs a timeout"

    def test_list_directory_normalizes_and_filters(self):
        """A missing trailing slash is added and parent / sort links are dropped."""
        html = '<a href="?C=N">s</a><a href="../">up</a><a href="12/">12/</a><a href="f.nc">f</a>'
        names = _helpers.list_directory("https://x/dir", http_text=lambda url: html)
        assert names == ["12/", "f.nc"]

    def test_parse_reference_time_datetime_passthrough(self):
        """A `datetime` reference-time is returned unchanged."""
        from datetime import datetime

        moment = datetime(2026, 8, 26, 12)
        assert _helpers._parse_reference_time(moment) is moment

    def test_parse_reference_time_bad_raises(self):
        """An unparseable reference-time raises."""
        with pytest.raises(ValueError, match="reference_time"):
            _helpers._parse_reference_time("not-a-date")

    def test_latest_without_complete_cycle_raises(self):
        """'latest' raises when no cycle carries the endFls sentinel."""
        http = _FakeHttp(
            "https://x/r", "medium_term_forecasts", ("2026", "08", "26", "12"), ["f.nc"]
        )
        http.tree["https://x/r/medium_term_forecasts/2026/08/26/12/"] = ["f.nc"]
        with pytest.raises(ValueError, match="no complete cycle"):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "latest",
                "endFls",
                http_text=http,
            )

    def test_find_cycle_file_no_match_raises(self):
        """A cycle folder without the expected file raises."""
        with pytest.raises(ValueError, match="no file matching"):
            _helpers.find_cycle_file(
                "https://x/c/",
                "*Gridded_*.nc",
                http_text=lambda url: '<a href="other.txt">o</a>',
            )

    def test_non_gridded_field_rejected(self, tmp_path: Path, monkeypatch):
        """A field that is not on the lat/lon grid is refused with a clear message."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr(
            "pyramids.netcdf.NetCDF.read_file",
            lambda _url: _FakeContainer(object()),  # no columns/rows -> not gridded
        )
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            field="summaryTWLcoast_01_15",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="not a gridded field"):
            backend.download()

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            (dict(dataset="efhm", field="TWL75"), "field"),
            (
                dict(dataset="sea_level", product="medium_term", return_periods=[100]),
                "return_periods",
            ),
            (
                dict(
                    dataset="sea_level",
                    product="subseasonal",
                    representation="coastal",
                    field="TWL75",
                ),
                "field",
            ),
        ],
    )
    def test_cross_kind_argument_warns(self, kwargs, expected, caplog):
        """A selector belonging to another kind is reported, not silently dropped."""
        import logging

        from _pytest.logging import LogCaptureHandler

        handler = LogCaptureHandler()
        logger_id = _backend_logger.add(handler, level="WARNING", format="{message}")
        try:
            JRC(lat_lim=[51.0, 53.0], lon_lim=[3.0, 5.0], **kwargs)
        finally:
            _backend_logger.remove(logger_id)
        messages = " ".join(r.getMessage() for r in handler.records)
        assert expected in messages, (
            f"expected a warning naming {expected!r}: {messages}"
        )

    def test_search_rejects_an_unhandled_kind(self):
        """`_search` refuses a bad kind before doing any network work."""
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
        )
        backend._dataset = backend._dataset.model_copy(update={"kind": "mystery"})
        with pytest.raises(ValueError, match="unhandled JRC dataset kind"):
            backend._search()

    def test_fetch_rejects_an_unhandled_kind(self):
        """`_fetch` refuses a kind that slipped past construction."""
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
        )
        backend._dataset = backend._dataset.model_copy(update={"kind": "mystery"})
        with pytest.raises(ValueError, match="unhandled JRC dataset kind"):
            backend._fetch([])

    def test_unhandled_kind_rejected(self):
        """A catalog row with an unhandled kind is refused at construction."""
        catalog = Catalog()
        row = catalog.get("efhm").model_copy(update={"kind": "not_a_kind"})
        catalog = catalog.model_copy(update={"datasets": {"efhm": row}})
        with pytest.raises(ValueError, match="unhandled kind"):
            JRC(catalog=catalog, lat_lim=[51.0, 53.0], lon_lim=[3.0, 5.0])

    def test_unknown_dataset_rejected(self):
        """An unknown dataset selector raises."""
        with pytest.raises(ValueError, match="unknown JRC dataset"):
            JRC(dataset="not-a-dataset", lat_lim=[51.0, 53.0], lon_lim=[3.0, 5.0])

    def test_aged_out_cycle_raises_value_error(self):
        """A 404 on a pinned cycle surfaces as the documented ValueError."""
        import requests

        def _gone(url):
            raise requests.HTTPError(f"404 Client Error: Not Found for url: {url}")

        with pytest.raises(ValueError, match="not published"):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "2026-01-01T12",
                "endFls",
                http_text=_gone,
            )

    def test_latest_crawl_is_bounded(self, monkeypatch):
        """The 'latest' walk stops after the probe budget instead of crawling on."""
        probes = {"listings": 0}

        def _endless(url):
            # Count EVERY listing, not just leaves: budgeting leaves alone still
            # allows thousands of requests across the year/month/day levels.
            probes["listings"] += 1
            depth = len([p for p in url.strip("/").split("/") if p.isdigit()])
            if depth >= 4:
                return ""  # a complete-looking leaf that never carries endFls
            return "".join(f'<a href="{n:02d}/">{n:02d}/</a>' for n in range(1, 13))

        monkeypatch.setattr(_helpers, "MAX_CYCLE_PROBES", 5)
        with pytest.raises(ValueError, match="no complete cycle"):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "latest",
                "endFls",
                http_text=_endless,
            )
        assert probes["listings"] <= 5, (
            f"every listing must be budgeted, issued {probes['listings']} requests"
        )

    def test_band_valid_times_falls_back_when_time_is_unreadable(self, monkeypatch):
        """An unreadable time axis degrades to positional band names, never raises."""
        monkeypatch.undo()  # exercise the real helper, not the offline stand-in
        assert _helpers.band_valid_times("/vsicurl/not-a-real-cube.nc", 3) == [
            "step_1",
            "step_2",
            "step_3",
        ]

    def test_cycle_id_rejects_a_short_path(self):
        """A URL without the four numeric segments cannot yield a cycle id."""
        with pytest.raises(ValueError, match="cycle id"):
            _helpers._cycle_id("https://x/r/2026/08/")

    def test_find_cycle_file_is_case_sensitive(self):
        """Glob matching is case-sensitive, so it behaves the same on all platforms."""
        with pytest.raises(ValueError, match="no file matching"):
            _helpers.find_cycle_file(
                "https://x/c/",
                "*TWLforecastGridded_*.nc",
                http_text=lambda url: '<a href="mediumTWLFORECASTGRIDDED_x.nc">o</a>',
            )


class TestGriddedEdges:
    """Cache reuse + the out-of-grid guard on the gridded path."""

    def _backend(self, tmp_path, monkeypatch):
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_202608261200-202609101200.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _fake_read_file)
        return JRC(
            dataset="sea_level",
            product="medium_term",
            reference_time="latest",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )

    def test_cached_output_is_reused(self, tmp_path: Path, monkeypatch):
        """A second download of the same AOI reuses the cached crop."""
        backend = self._backend(tmp_path, monkeypatch)
        first = backend.download()[0]
        again = backend.download()[0]
        assert first == again, f"the cached path changed: {first} -> {again}"
        assert first.exists(), f"{first} was not written"

    def test_out_of_grid_guard(self, tmp_path: Path, monkeypatch):
        """An AOI that maps to no pixels raises a clear coverage error.

        The shipped grid is global, so no real AOI can miss it; the guard exists
        for a future non-global row (and for a degenerate window), which is why
        `pixel_window` is forced to `None` here rather than fed a real bbox.
        """
        backend = self._backend(tmp_path, monkeypatch)
        monkeypatch.setattr(_helpers, "pixel_window", lambda *args, **kwargs: None)
        with pytest.raises(ValueError, match="outside the sea-level grid"):
            backend.download()

    def test_point_aoi_is_widened(self, tmp_path: Path, monkeypatch):
        """A point AOI is widened to one pixel, not reported off-grid."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _fake_read_file)
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            reference_time="latest",
            lat_lim=[52.0, 52.0],
            lon_lim=[5.0, 5.0],
            path=tmp_path,
        )
        paths = backend.download()
        assert len(paths) == 1, f"expected one output, got {paths}"
        assert paths[0].exists(), f"{paths[0]} was not written"

    def test_failed_write_leaves_no_partial_file(self, tmp_path: Path, monkeypatch):
        """A write that fails mid-way removes the staged file and re-raises."""
        backend = self._backend(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "earthlens.jrc.backend.crop_to_aoi",
            lambda *args, **kwargs: _ExplodingDataset(),
        )
        with pytest.raises(OSError, match="disk full"):
            backend.download()
        assert list(tmp_path.glob("*.part.tif")) == [], (
            "a staged .part file was left behind"
        )
        assert list(tmp_path.glob("*.tif")) == [], "a partial output was left behind"

    def test_masked_fill_becomes_nan(self, tmp_path: Path, monkeypatch):
        """A masked source cell is written as NaN, not the numeric fill value."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _fake_read_file_masked)
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            reference_time="latest",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        written = PyramidsDataset.read_file(str(backend.download()[0]))
        band0 = np.asarray(written.read_array())[0]
        assert np.isnan(band0).any(), "the masked cell must be written as NaN"
        assert np.isfinite(band0).any(), "unmasked cells must be preserved"
