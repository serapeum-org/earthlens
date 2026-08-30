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
def _offline_band_names(request, monkeypatch):
    """Keep band naming offline: it would otherwise open the real cube.

    Tests marked `real_band_names` opt out so the valid-time labels themselves
    are asserted somewhere rather than being stubbed everywhere.
    """
    if request.node.get_closest_marker("real_band_names"):
        return
    monkeypatch.setattr(
        _helpers,
        "band_valid_times",
        lambda url, steps: [f"step_{index + 1}" for index in range(steps)],
    )


#: The cubes' real geotransform, as pyramids derives it from their CF coords.
_GLOBAL_GEO = (-180.0, 0.25, 0.0, 90.0, 0.0, -0.25)


_COASTAL_CSV = "GID_0,NAME_0,summary_TWL_1_10\nABW,Aruba,2\nNLD,Netherlands,9\n"


class _FakeMDArray:
    """A CF `time` coordinate returning days since a stated epoch."""

    def __init__(self, values, units="days since 1950-01-01"):
        self._values = values
        self._units = units

    def ReadAsArray(self):  # noqa: N802 - mirrors GDAL's method name
        return np.asarray(self._values)

    def GetUnit(self):  # noqa: N802 - mirrors GDAL's method name
        return self._units


class _FakeRootGroup:
    """A root group exposing one named MDArray."""

    def __init__(self, values):
        self._values = values

    def OpenMDArray(self, name):  # noqa: N802 - mirrors GDAL's method name
        return _FakeMDArray(self._values)


class _FakeMDDataset:
    """A multidim dataset wrapping a fake root group."""

    def __init__(self, values):
        self._values = values

    def GetRootGroup(self):  # noqa: N802 - mirrors GDAL's method name
        return _FakeRootGroup(self._values)


class _FakeGdal:
    """The slice of `osgeo.gdal` that `band_valid_times` touches."""

    OF_MULTIDIM_RASTER = 0

    def __init__(self, values):
        self._values = values

    def OpenEx(self, url, flags):  # noqa: N802 - mirrors GDAL's method name
        return _FakeMDDataset(self._values)


def _raise_503(url):
    """Stand-in that fails the way a struggling server would."""
    import requests

    response = requests.Response()
    response.status_code = 503
    raise requests.HTTPError("503 Server Error", response=response)


def _raise_runtime(*args, **kwargs):
    """Stand-in that fails the way an unreachable cube would."""
    raise RuntimeError("cannot open")


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
        # A real gridded Variable reports both of these.
        self.band_count = bands
        # pyramids >= 0.58.1 derives this from the cube's CF coordinates.
        self.geotransform = _GLOBAL_GEO

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


class _FakeIntVariable(_FakeVariable):
    """A categorical field stored as integers, as the severity flags are."""

    def read_array(self, window, masked=False):
        _, _, width, height = window
        data = np.full((self._bands, height, width), 3, dtype="int16")
        mask = np.zeros_like(data, dtype=bool)
        mask[:, 0, 0] = True
        return np.ma.masked_array(data, mask=mask)


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
    """`pixel_window` / `window_origin` against the cubes' real affine."""

    def test_affine_maps_corners_to_real_world_degrees(self):
        """A 0.25 deg global grid's affine spans exactly the lon/lat domain."""
        cols, rows = 1440, 720
        # Derived from the grid definition, not copied from the code under test.
        expected = (-180.0, 360.0 / cols, 90.0, -180.0 / rows)
        x0, dx, y0, dy = _GLOBAL_GEO[0], _GLOBAL_GEO[1], _GLOBAL_GEO[3], _GLOBAL_GEO[5]
        assert (x0, dx, y0, dy) == expected, (
            "the shipped constant no longer matches a 0.25 deg global grid"
        )
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
        col_off, row_off, _, _ = _helpers.pixel_window(_GLOBAL_GEO, bbox, 1440, 720)
        origin = _helpers.window_origin(_GLOBAL_GEO, col_off, row_off)
        assert (origin[0], origin[3]) == pytest.approx(expected_origin), (
            f"window origin {origin[:4]} should be the NW corner {expected_origin}"
        )

    def test_pixel_window_maps_bbox(self):
        """A bbox maps to the expected clamped pixel window."""
        assert _helpers.pixel_window(
            _GLOBAL_GEO, (3.0, 51.0, 5.0, 53.0), 1440, 720
        ) == (
            732,
            148,
            8,
            8,
        )

    def test_pixel_window_none_when_degenerate(self):
        """A zero-area bbox yields no window."""
        assert (
            _helpers.pixel_window(_GLOBAL_GEO, (3.0, 51.0, 3.0, 51.0), 1440, 720)
            is None
        )

    def test_window_origin_shifts_to_corner(self):
        """The window origin is the bbox top-left in degrees, not index space."""
        assert _helpers.window_origin(_GLOBAL_GEO, 732, 148) == (
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
class TestNetworkSeams:
    """The byte fetch, the non-404 re-raise and the budget entry guard."""

    def test_http_bytes_returns_raw_body(self, monkeypatch):
        """`http_bytes` hands back undecoded bytes for the CSV parser."""

        class _Resp:
            content = ("GID_0" + chr(10) + "Aland" + chr(10)).encode("utf-8")

        class _Client:
            def get(self, url, **kwargs):
                return _Resp()

        monkeypatch.setattr(_helpers, "_client", lambda: _Client())
        body = _helpers.http_bytes("https://x/f.csv").decode("utf-8")
        assert body.endswith("Aland" + chr(10)), f"unexpected body: {body!r}"

    def test_non_404_error_is_reraised(self):
        """A 5xx on a pinned cycle is a server fault, not an aged-out cycle."""
        import requests

        with pytest.raises(requests.HTTPError):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "2026-08-26T12",
                "endFls",
                http_text=_raise_503,
            )

    def test_leaf_probe_respects_an_exhausted_budget(self):
        """A leaf probe with no budget left returns without listing anything."""
        calls = {"n": 0}

        def _counting(url):
            calls["n"] += 1
            return "endFls"

        assert (
            _helpers._probe_leaf("https://x/2026/08/26/12/", "endFls", _counting, [0])
            is None
        )
        assert calls["n"] == 0, "an exhausted budget must not issue a request"

    def test_exhausted_budget_stops_immediately(self):
        """A walk entered with no budget left issues no request at all."""
        calls = {"n": 0}

        def _counting(url):
            calls["n"] += 1
            return ""

        found = _helpers._descend_newest("https://x/", 4, "endFls", _counting, [0])
        assert found is None, "an exhausted budget must find nothing"
        assert calls["n"] == 0, "an exhausted budget must not issue a request"


class TestBandValidTimes:
    """`band_valid_times` reads the CF time axis through the gdal seam."""

    @pytest.mark.real_band_names
    def test_time_axis_becomes_band_labels(self, monkeypatch):
        """A field whose bands are the time axis gets real valid times."""
        monkeypatch.setattr(
            _helpers, "gdal_module", lambda: _FakeGdal([27996.0, 27997.0, 27998.0])
        )
        assert _helpers.band_valid_times("irrelevant", 3) == [
            "2026-08-26T00:00",
            "2026-08-27T00:00",
            "2026-08-28T00:00",
        ]

    @pytest.mark.real_band_names
    def test_aggregate_field_keeps_positional_names(self, monkeypatch):
        """A 2-D aggregate (1 band, 16-step axis) is never mislabelled."""
        monkeypatch.setattr(
            _helpers, "gdal_module", lambda: _FakeGdal(list(range(27996, 28012)))
        )
        assert _helpers.band_valid_times("irrelevant", 1) == ["step_1"]

    @pytest.mark.real_band_names
    def test_gdal_module_returns_the_vendored_gdal(self):
        """The seam hands back the real GDAL module."""
        assert hasattr(_helpers.gdal_module(), "OpenEx")


class TestGeographicAffineGuard:
    """`require_geographic_affine` rejects an affine that is not a lon/lat grid."""

    def test_cf_affine_is_accepted(self):
        """The cubes' real CF affine passes."""
        _helpers.require_geographic_affine(_GLOBAL_GEO, 1440, 720, "x")

    def test_small_index_space_variable_is_refused(self):
        """A coastal-point field's index affine sits inside the domain but is refused."""
        # The live TWLcoast field: 50x16 with (0,1,0,16,0,-1), which passes an
        # extent check because 0..50 / 0..16 is inside +-180/+-90.
        with pytest.raises(ValueError, match="index-space geotransform"):
            _helpers.require_geographic_affine(
                (0.0, 1.0, 0.0, 16.0, 0.0, -1.0), 50, 16, "TWLcoast"
            )

    def test_legitimate_one_degree_grid_is_accepted(self):
        """A real 1-degree global grid is not mistaken for index space."""
        _helpers.require_geographic_affine(
            (-180.0, 1.0, 0.0, 90.0, 0.0, -1.0), 360, 180, "x"
        )

    @pytest.mark.parametrize(
        ("name", "geo"),
        [
            ("flipped index space", (0.0, 1.0, 0.0, 720.0, 0.0, -1.0)),
            ("gdal identity", (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)),
            ("south-up", (-180.0, 0.25, 0.0, -90.0, 0.0, 0.25)),
            ("projected metres", (2_000_000.0, 1000.0, 0.0, 5_000_000.0, 0.0, -1000.0)),
        ],
    )
    def test_non_geographic_affine_is_refused(self, name, geo):
        """Index-space, south-up and projected affines are all rejected."""
        with pytest.raises(ValueError):
            _helpers.require_geographic_affine(geo, 1440, 720, "x")


class TestIndexSpaceGuard:
    """An un-georeferenced variable is refused, naming the pyramids requirement."""

    def test_index_space_geotransform_is_refused(self, tmp_path: Path, monkeypatch):
        """A variable still in index space (pre-0.58.1 pyramids) raises."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        stale = _FakeVariable()
        stale.geotransform = (0.0, 1.0, 0, 720.0, 0, -1.0)  # the pre-fix affine
        monkeypatch.setattr(
            "pyramids.netcdf.NetCDF.read_file", lambda _url: _FakeContainer(stale)
        )
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="north-up|lon/lat"):
            backend.download()


class TestBandNamesReachTheOutput:
    """The written raster carries the cube's valid times, not just step_N."""

    @pytest.mark.real_band_names
    def test_written_bands_are_labelled_with_valid_times(
        self, tmp_path: Path, monkeypatch
    ):
        """A gridded fetch stamps the CF valid times onto the output bands."""
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
            _helpers, "gdal_module", lambda: _FakeGdal([27996.0 + n for n in range(16)])
        )
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        written = PyramidsDataset.read_file(str(backend.download()[0]))
        assert written.band_names[0] == "2026-08-26T00:00", (
            f"expected a CF valid time, got {written.band_names[0]!r}"
        )


class TestBandCountGuard:
    """A variable reporting no bands is not a gridded field (M1)."""

    def test_zero_band_variable_is_refused(self, tmp_path: Path, monkeypatch):
        """band_count 0 must not be treated as a single step."""
        row = Catalog().get("sea_level_medium_term")
        http = _FakeHttp(
            row.base_url,
            row.product,
            ("2026", "08", "26", "12"),
            ["mediumTermTWLforecastGridded_x.nc"],
        )
        monkeypatch.setattr(_helpers, "_http_text", http)
        bandless = _FakeVariable()
        bandless.band_count = 0
        monkeypatch.setattr(
            "pyramids.netcdf.NetCDF.read_file", lambda _url: _FakeContainer(bandless)
        )
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="not a gridded forecast field"):
            backend.download()


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

    def test_explicit_dataset_id_warns_about_family_selectors(self):
        """Naming a dataset directly reports that product= cannot also apply."""
        messages = []
        logger_id = _backend_logger.add(
            lambda m: messages.append(str(m)), level="WARNING"
        )
        try:
            JRC(
                dataset="sea_level_medium_term",
                product="subseasonal",
                lat_lim=[51.0, 53.0],
                lon_lim=[3.0, 5.0],
            )
        finally:
            _backend_logger.remove(logger_id)
        assert any("product" in m for m in messages), f"expected a warning: {messages}"

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
        """A real 404 on a pinned cycle surfaces as the documented ValueError."""
        import requests

        def _gone(url):
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("404 Client Error: Not Found", response=response)

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

    @pytest.mark.real_band_names
    def test_band_valid_times_falls_back_when_time_is_unreadable(self, monkeypatch):
        """An unreadable time axis degrades to positional band names, never raises."""
        monkeypatch.setattr(_helpers, "gdal_module", _raise_runtime)
        assert _helpers.band_valid_times("irrelevant", 3) == [
            "step_1",
            "step_2",
            "step_3",
        ]

    def test_cycle_id_rejects_a_short_path(self):
        """A URL without the four numeric segments cannot yield a cycle id."""
        with pytest.raises(ValueError, match="cycle id"):
            _helpers._cycle_id("https://x/r/2026/08/")

    def test_http_bytes_uses_the_injected_fetch(self):
        """The `fetch=` seam bypasses the shared client entirely."""
        body = _helpers.http_bytes("https://x/f.csv", fetch=lambda url: b"GID_0")
        assert body == b"GID_0", f"the injected fetch was not used: {body!r}"

    def test_pruned_directory_is_skipped_mid_walk(self):
        """A 404 from a directory pruned during the walk is skipped, not fatal."""
        import requests

        def _pruned(url):
            depth = len([p for p in url.strip("/").split("/") if p.isdigit()])
            if depth == 0:
                return '<a href="2026/">2026/</a>'
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("404", response=response)

        with pytest.raises(ValueError, match="no complete cycle"):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "latest",
                "endFls",
                http_text=_pruned,
            )

    def test_non_404_during_the_walk_is_reraised(self):
        """A 5xx mid-walk is a server fault and must not be swallowed."""
        import requests

        def _boom(url):
            depth = len([p for p in url.strip("/").split("/") if p.isdigit()])
            if depth == 0:
                return '<a href="2026/">2026/</a>'
            response = requests.Response()
            response.status_code = 503
            raise requests.HTTPError("503", response=response)

        with pytest.raises(requests.HTTPError):
            _helpers.resolve_cycle(
                "https://x/r",
                "medium_term_forecasts",
                "%Y/%m/%d/%H",
                "latest",
                "endFls",
                http_text=_boom,
            )

    def test_unparseable_time_units_fall_back_to_the_default_epoch(self):
        """An unrecognised date in the units falls back rather than raising."""
        assert _helpers._parse_cf_epoch("days since not-a-date").year == 1950

    def test_ambiguous_glob_match_raises(self):
        """Two files matching one glob is a layout change, not a pick-the-first."""
        html = (
            '<a href="mediumTermTWLforecastGridded_a.nc">a</a>'
            '<a href="mediumTermTWLforecastGridded_b.nc">b</a>'
        )
        with pytest.raises(ValueError, match="expected exactly one"):
            _helpers.find_cycle_file(
                "https://x/c/", "*TWLforecastGridded_*.nc", http_text=lambda url: html
            )

    def test_out_of_domain_extent_is_refused(self):
        """An affine whose far corner leaves the lon/lat domain is rejected."""
        # A valid negative origin whose southern edge runs off the globe.
        with pytest.raises(ValueError, match="outside the lon/lat domain"):
            _helpers.require_geographic_affine(
                (-180.0, 0.25, 0.0, 0.0, 0.0, -0.25), 1440, 720, "x"
            )

    def test_field_name_is_sanitised_for_the_filename(self):
        """A path-traversing field name cannot escape the output directory."""
        assert _helpers._safe_name("../../etc/passwd") == ".._.._etc_passwd"
        assert _helpers._safe_name("!!!") == "___"

    def test_origin_outside_the_lonlat_domain_is_refused(self):
        """An origin that is not a lon/lat coordinate at all is rejected."""
        with pytest.raises(ValueError, match="origin is not a"):
            _helpers.require_geographic_affine(
                (-500.0, 0.25, 0.0, 90.0, 0.0, -0.25), 10, 10, "x"
            )

    def test_zero_to_360_longitude_is_named(self):
        """A 0..360 cube gets a message about the convention, not a vague error."""
        with pytest.raises(ValueError, match="0..360 longitude convention"):
            _helpers.require_geographic_affine(
                (0.125, 0.25, 0.0, 90.0, 0.0, -0.25), 1440, 720, "x"
            )

    def test_rotated_affine_is_refused(self):
        """A rotated grid cannot be windowed by bbox, so it is refused."""
        with pytest.raises(ValueError, match="rotated geotransform"):
            _helpers.require_geographic_affine(
                (-180.0, 0.25, 0.1, 90.0, 0.1, -0.25), 1440, 720, "x"
            )

    def test_cf_epoch_is_read_from_the_units(self):
        """The epoch comes from the file's units, not a hardcoded constant."""
        assert _helpers._parse_cf_epoch("days since 2000-01-01").year == 2000
        assert _helpers._parse_cf_epoch(None).year == 1950
        assert _helpers._parse_cf_epoch("hours since 2000-01-01").year == 1950

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
        """A second download of the same AOI skips the read and reuses the file."""
        backend = self._backend(tmp_path, monkeypatch)
        first = backend.download()[0]
        stamp = first.stat().st_mtime_ns
        reads = {"n": 0}

        def _counting_read(_url):
            reads["n"] += 1
            return _FakeContainer(_FakeVariable())

        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _counting_read)
        again = backend.download()[0]
        assert first == again, f"the cached path changed: {first} -> {again}"
        assert reads["n"] == 0, "a cache hit must not re-open the cube"
        assert again.stat().st_mtime_ns == stamp, "the cached file was rewritten"

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

    def test_integer_field_is_cast_before_filling(self, tmp_path: Path, monkeypatch):
        """An integer-stored field is cast to float before the NaN fill (H3)."""
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
            lambda _url: _FakeContainer(_FakeIntVariable()),
        )
        backend = JRC(
            dataset="sea_level",
            product="medium_term",
            lat_lim=[51.0, 53.0],
            lon_lim=[3.0, 5.0],
            path=tmp_path,
        )
        written = PyramidsDataset.read_file(str(backend.download()[0]))
        band0 = np.asarray(written.read_array())[0]
        assert np.isnan(band0).any(), "the masked cell must become NaN"
        assert (band0[np.isfinite(band0)] == 3).all(), "values must survive the cast"

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
