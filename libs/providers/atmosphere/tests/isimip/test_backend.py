"""Unit tests for the ISIMIP backend (injected client; no network)."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from earthlens.base import RemoteProduct
from earthlens.biodiversity import LicenseWarning
from earthlens.isimip import ISIMIP
from earthlens.isimip import backend as backend_mod

from .conftest import FakeClient, make_dataset

pytestmark = [pytest.mark.isimip, pytest.mark.unit]


class TestConstruction:
    """Tests for ISIMIP construction and facet validation."""

    def test_valid_build(self, make_backend):
        """A fully-specified request builds a raster backend."""
        b = make_backend()
        assert b.OUTPUT_KIND == "raster"
        assert b._round == "ISIMIP3b"
        assert b._scenario == "ssp585"

    def test_gcm_casing_normalised(self, make_backend):
        """A CMIP6-cased GCM is lowercased to the API spelling."""
        assert make_backend(gcm="GFDL-ESM4")._gcm == "gfdl-esm4"

    @pytest.mark.parametrize(
        "overrides, match",
        [
            ({"variables": []}, "non-empty variables"),
            ({"variables": None}, "non-empty variables"),
            ({"scenario": None}, "requires a scenario"),
            ({"gcm": None}, "requires a gcm"),
            ({"start": ""}, "start and end"),
            ({"end": ""}, "start and end"),
        ],
    )
    def test_missing_required_raises(self, make_backend, overrides, match):
        """Omitting a required argument raises a friendly ValueError."""
        with pytest.raises(ValueError, match=match):
            make_backend(**overrides)

    @pytest.mark.parametrize(
        "overrides, match",
        [
            ({"dataset": "ISIMIP9z"}, "not a curated ISIMIP round"),
            ({"scenario": "ssp999"}, "not a curated ISIMIP scenario"),
            ({"gcm": "no-such-model"}, "not a curated ISIMIP forcing"),
            ({"variables": ["nope"]}, "not in the ISIMIP catalog"),
            ({"product": "Nope"}, "not an ISIMIP product"),
            ({"temporal_resolution": "hourly"}, "not an ISIMIP time_step"),
        ],
    )
    def test_bad_facet_raises(self, make_backend, overrides, match):
        """An out-of-vocabulary facet raises with a did-you-mean/known-values hint."""
        with pytest.raises(ValueError, match=match):
            make_backend(**overrides)

    def test_requires_bbox_without_whole_globe(self, make_backend):
        """Omitting the bbox without whole_globe is rejected."""
        with pytest.raises(ValueError, match="requires a bbox"):
            make_backend(lat_lim=None, lon_lim=None)

    def test_whole_globe_allows_no_bbox(self, make_backend):
        """whole_globe=True permits a whole-Earth request."""
        b = make_backend(lat_lim=None, lon_lim=None, whole_globe=True)
        assert b._bbox() is None


class TestBboxHelpers:
    """Tests for the spatial-subset helpers."""

    def test_bbox_order_is_west_east_south_north(self, make_backend):
        """`_bbox` returns the client's (west, east, south, north) order."""
        b = make_backend(lat_lim=[51.0, 53.0], lon_lim=[6.0, 8.0])
        assert b._bbox() == (6.0, 8.0, 51.0, 53.0), b._bbox()

    def test_whole_earth_is_no_subset(self, make_backend):
        """A whole-Earth box counts as no spatial subset."""
        b = make_backend(
            lat_lim=[-90.0, 90.0], lon_lim=[-180.0, 180.0], whole_globe=True
        )
        assert b._wants_spatial_subset() is False
        assert b._bbox() is None


class TestFileWindow:
    """Tests for the per-file date-window filter."""

    def test_overlapping_file_kept(self, make_backend):
        """A decade file overlapping the window is kept."""
        assert make_backend()._file_in_window("x_pr_global_daily_2015_2020.nc") is True

    def test_disjoint_file_dropped(self, make_backend):
        """A decade file outside the window is dropped."""
        assert make_backend()._file_in_window("x_pr_global_daily_2091_2100.nc") is False

    def test_rangeless_file_kept(self, make_backend):
        """A file whose name carries no decade range is kept to be safe."""
        assert make_backend()._file_in_window("x_pr_global_monthly.nc") is True


class TestSearch:
    """Tests for `_search` (facet query build + file resolution)."""

    def test_query_facets(self, make_backend):
        """The datasets query carries every requested facet, GCM lowercased."""
        b = make_backend(gcm="GFDL-ESM4")
        b._search()
        call = b._client.datasets_calls[0]
        assert call == {
            "simulation_round": "ISIMIP3b",
            "product": "InputData",
            "climate_forcing": "gfdl-esm4",
            "climate_scenario": "ssp585",
            "climate_variable": "pr",
            "time_step": "daily",
        }, call

    def test_window_filters_paths(self, make_backend):
        """Only the in-window decade granule paths survive into the product."""
        b = make_backend()
        products = b._search()
        assert len(products) == 1
        assert len(products[0].metadata["paths"]) == 1
        assert "2015_2020" in products[0].metadata["paths"][0]

    def test_multiple_variables_fan_out(self, make_backend):
        """Each requested variable resolves to its own product."""
        b = make_backend(variables=["pr", "tas"])
        products = b._search()
        assert {p.metadata["name"].split("_")[-3] for p in products} == {"pr", "tas"}

    def test_missing_variable_raises(self, make_backend):
        """A variable that matches no dataset raises, never silently skipped."""
        b = make_backend(client=FakeClient(empty_vars=("pr",)))
        with pytest.raises(ValueError, match="no dataset for climate_variable"):
            b._search()

    def test_all_files_out_of_window_raises(self, make_backend):
        """A dataset whose files are all out of window yields no product and raises."""
        only_future = [
            make_dataset(
                "pr",
                files=[
                    {
                        "name": "x_pr_global_daily_2091_2100.nc",
                        "path": "p/2091_2100.nc",
                        "file_url": "u",
                    }
                ],
            )
        ]
        b = make_backend(client=FakeClient(datasets_by_var={"pr": only_future}))
        with pytest.raises(ValueError, match="no granule overlaps"):
            b._search()

    def test_one_variable_out_of_window_raises_even_if_another_succeeds(
        self, make_backend
    ):
        """A requested variable with no in-window granule raises, not silently dropped."""
        future = [
            make_dataset(
                "tas",
                files=[
                    {
                        "name": "x_tas_global_daily_2091_2100.nc",
                        "path": "p",
                        "file_url": "u",
                    }
                ],
            )
        ]
        client = FakeClient(datasets_by_var={"pr": [make_dataset("pr")], "tas": future})
        b = make_backend(client=client, variables=["pr", "tas"])
        with pytest.raises(ValueError, match="climate_variable='tas'"):
            b._search()


class TestFetchAndDownload:
    """Tests for `_fetch` / `download` over the cutout job flow."""

    def test_download_returns_cut_paths(self, make_backend):
        """A bbox download returns the extracted NetCDF path(s)."""
        b = make_backend()
        out = b.download(progress_bar=False)
        assert out, out
        assert all(p.suffix == ".nc" for p in out), out

    def test_cutout_called_with_bbox(self, make_backend):
        """The cutout job gets the window-filtered paths and the bbox order."""
        b = make_backend()
        b.download(progress_bar=False)
        call = b._client.cutout_calls[0]
        assert len(call["paths"]) == 1
        assert (call["west"], call["east"], call["south"], call["north"]) == (
            6.0,
            8.0,
            51.0,
            53.0,
        )
        assert call["poll"] == b._poll

    def test_download_extracts_with_flag(self, make_backend):
        """The cut zip is downloaded with `extract=True`."""
        b = make_backend()
        b.download(progress_bar=False)
        assert b._client.download_calls[0]["extract"] is True

    def test_unfinished_job_raises(self, make_backend):
        """A cutout job that does not finish raises RuntimeError."""
        b = make_backend(client=FakeClient(job_status="failed"))
        with pytest.raises(RuntimeError, match="did not finish"):
            b.download(progress_bar=False)

    def test_none_job_raises(self, make_backend):
        """A null cutout job response raises RuntimeError."""
        b = make_backend(client=FakeClient(job={}))
        with pytest.raises(RuntimeError, match="did not finish"):
            b.download(progress_bar=False)

    def test_finished_job_with_no_netcdf_raises(self, make_backend):
        """A finished cutout that yields no NetCDF raises rather than returning []."""
        b = make_backend(client=FakeClient(writes_output=False))
        with pytest.raises(RuntimeError, match="produced no NetCDF granule"):
            b.download(progress_bar=False)

    def test_whole_globe_downloads_raw(self, make_backend):
        """whole_globe downloads raw granules and never runs a cutout."""
        b = make_backend(lat_lim=None, lon_lim=None, whole_globe=True)
        out = b.download(progress_bar=False)
        assert out
        assert b._client.cutout_calls == []
        assert b._client.download_calls, "expected a raw download"

    def test_whole_globe_all_urls_missing_raises(self, make_backend):
        """A whole-globe request whose granules carry no file_url raises."""
        ds = [
            make_dataset(
                "pr",
                files=[
                    {
                        "name": "x_pr_global_daily_2015_2020.nc",
                        "path": "p",
                        "file_url": None,
                    },
                ],
            )
        ]
        b = make_backend(
            client=FakeClient(datasets_by_var={"pr": ds}),
            lat_lim=None,
            lon_lim=None,
            whole_globe=True,
        )
        with pytest.raises(RuntimeError, match="no.*downloadable URL"):
            b.download(progress_bar=False)

    def test_products_write_to_isolated_dirs(self, make_backend):
        """Two variables' cutouts land in separate per-dataset subdirectories."""
        out = make_backend(variables=["pr", "tas"]).download(progress_bar=False)
        assert len(out) == 2
        assert len({p.parent for p in out}) == 2, "products must not share a dir"

    def test_product_dir_keyed_by_window(self, make_backend):
        """Different date windows resolve to different output subdirectories."""
        product = RemoteProduct(id="ds_pr", metadata={})
        wide = make_backend(start="2015-01-01", end="2100-12-31")._product_dir(product)
        narrow = make_backend(start="2030-01-01", end="2031-12-31")._product_dir(
            product
        )
        assert wide != narrow, "a narrower window must not reuse the wider run's dir"
        assert "2015_2100" in wide.name
        assert "2030_2031" in narrow.name


class TestLicense:
    """Tests for the per-dataset licence warning."""

    def test_open_dataset_no_warning(self, make_backend):
        """A CC0 dataset emits no LicenseWarning."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", LicenseWarning)
            make_backend().download(progress_bar=False)

    def test_restricted_dataset_warns(self, make_backend):
        """A restricted dataset emits a LicenseWarning."""
        b = make_backend(client=FakeClient(restricted=True))
        with pytest.warns(LicenseWarning, match="non-open terms"):
            b.download(progress_bar=False)

    def test_non_open_rights_warns(self, make_backend):
        """A non-open rights label (not CC0/CC-BY) emits a LicenseWarning."""
        b = make_backend(client=FakeClient(rights="ISIMIP terms of use"))
        with pytest.warns(LicenseWarning):
            b.download(progress_bar=False)

    def test_unknown_rights_warns(self, make_backend):
        """A dataset with empty/unknown rights is warned, not assumed open."""
        b = make_backend(client=FakeClient(rights=""))
        with pytest.warns(LicenseWarning, match="unknown"):
            b.download(progress_bar=False)


class TestMisc:
    """Tests for the remaining public surface."""

    def test_terms_note(self, make_backend):
        """`terms_note` returns the round's documentation licence."""
        assert "CC0" in make_backend().terms_note()

    def test_aggregate_is_refused(self, make_backend):
        """Passing aggregate= is refused (the backend writes raw NetCDF)."""
        b = make_backend()
        with pytest.raises(Exception, match="reduce|aggregate|separately"):
            b.download(aggregate={"reducer": "mean"})

    def test_client_or_build_uses_factory(self, make_backend, monkeypatch):
        """With no injected client, `_client_or_build` builds via the factory."""
        sentinel = FakeClient()
        monkeypatch.setattr(backend_mod, "build_client", lambda *a, **k: sentinel)
        b = make_backend()
        b._client = None
        assert b._client_or_build() is sentinel


class TestNoXarray:
    """Conformance: the package never imports a NetCDF decoder."""

    def test_no_decoder_imports(self):
        """No source file imports xarray / netCDF4 (decode is pyramids')."""
        pkg = Path(backend_mod.__file__).parent
        for py in pkg.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            assert "import xarray" not in text, py.name
            assert "import netCDF4" not in text, py.name
