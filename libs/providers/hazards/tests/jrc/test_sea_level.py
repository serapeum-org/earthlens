"""Unit tests for the JRC sea-level (TWL) forecast paths — no network."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pyramids.dataset import Dataset as PyramidsDataset

from earthlens.jrc import JRC, Catalog, _helpers

pytestmark = pytest.mark.jrc

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

    def read_array(self, window):
        col_off, row_off, width, height = window
        array = np.full(
            (self._bands, height, width),
            float(col_off * 1000 + row_off),
            dtype="float32",
        )
        return array


class _FakeContainer:
    """A NetCDF container yielding a single fake variable."""

    def __init__(self, variable):
        self._variable = variable

    def get_variable(self, name):
        return self._variable


def _fake_read_file(_url, variable=None):
    """Stand-in for `NetCDF.read_file` returning a fake container."""
    return _FakeContainer(variable if variable is not None else _FakeVariable())


class _FakeMaskedVariable(_FakeVariable):
    """A variable whose window read masks one cell with a numeric fill value."""

    def read_array(self, window):
        _, _, width, height = window
        data = np.full((self._bands, height, width), 1.5, dtype="float32")
        mask = np.zeros_like(data, dtype=bool)
        mask[:, 0, 0] = True
        return np.ma.masked_array(data, mask=mask)


def _fake_read_file_masked(_url):
    """Stand-in for `NetCDF.read_file` returning a masked-cell variable."""
    return _FakeContainer(_FakeMaskedVariable())


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
        assert len(paths) == 1 and paths[0].exists()

        written = PyramidsDataset.read_file(str(paths[0]))
        assert written.band_count == 16
        assert written.epsg == 4326
        origin_x, cell, _, origin_y, _, _ = written.geotransform
        assert cell == pytest.approx(0.25) and cell != 1.0  # degrees, not index space
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
        with pytest.raises(Exception, match="aggregate="):
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

    def test_http_text_uses_requests(self, monkeypatch):
        """`_http_text` GETs the URL and raises on a bad status."""
        seen = {}

        class _Resp:
            text = '<a href="12/">12/</a>'

            def raise_for_status(self):
                seen["ok"] = True

        monkeypatch.setattr(_helpers.requests, "get", lambda url, timeout: _Resp())
        assert "12/" in _helpers._http_text("https://x/")
        assert seen["ok"]

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

    def test_unknown_dataset_rejected(self):
        """An unknown dataset selector raises."""
        with pytest.raises(ValueError, match="unknown JRC dataset"):
            JRC(dataset="not-a-dataset", lat_lim=[51.0, 53.0], lon_lim=[3.0, 5.0])

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
        assert first == again and first.exists()

    def test_out_of_grid_guard(self, tmp_path: Path, monkeypatch):
        """An AOI that maps to no pixels raises a clear coverage error."""
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
        assert len(paths) == 1 and paths[0].exists()

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
