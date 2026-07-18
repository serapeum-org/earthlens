"""Unit + integration tests for the NWM backend (faked S3, no network)."""

from __future__ import annotations

import earthlens.nwm

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

from earthlens.nwm import BUCKET, NWM
from earthlens.nwm.backend import (
    _is_int,
    _is_missing_key,
    build_key,
    enumerate_cycles,
)
from .conftest import FakeS3

pytestmark = [pytest.mark.nwm]

_SRC = Path(earthlens.nwm.__file__).parent


# -- enumerate_cycles -------------------------------------------------------


def test_enumerate_cycles_single_day():
    """Cycles for one day are the sorted run hours on that date."""
    day = dt.datetime(2026, 1, 1)
    assert [c.hour for c in enumerate_cycles(day, day, [12, 0])] == [0, 12]


def test_enumerate_cycles_multi_day():
    """A two-day window yields run hours per day, ascending."""
    cycles = enumerate_cycles(dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2), [0, 6])
    assert len(cycles) == 4
    assert cycles[0].day == 1 and cycles[-1].day == 2


def test_enumerate_cycles_inverted_window():
    """An inverted window is rejected."""
    with pytest.raises(ValueError, match="is after"):
        enumerate_cycles(dt.datetime(2026, 1, 2), dt.datetime(2026, 1, 1), [0])


def test_enumerate_cycles_bad_hour():
    """A run hour outside [0, 23] is rejected."""
    day = dt.datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="outside"):
        enumerate_cycles(day, day, [24])


# -- build_key --------------------------------------------------------------


def test_build_key_forecast(catalog):
    """A short_range forecast key uses the fNNN step token and conus suffix."""
    key = build_key(
        catalog.get_config("short_range"),
        catalog.get_product("chrtout"),
        dt.datetime(2026, 5, 26, 0),
        1,
        1,
    )
    assert (
        key == "nwm.20260526/short_range/nwm.t00z.short_range.channel_rt.f001.conus.nc"
    )


def test_build_key_analysis(catalog):
    """An analysis_assim key uses the tmNN look-back token."""
    key = build_key(
        catalog.get_config("analysis_assim"),
        catalog.get_product("ldasout"),
        dt.datetime(2026, 5, 26, 6),
        2,
        1,
    )
    assert key.endswith("nwm.t06z.analysis_assim.land.tm02.conus.nc")


def test_build_key_ensemble_member_on_token(catalog):
    """An ensemble key rides the member on the directory and product token."""
    key = build_key(
        catalog.get_config("medium_range"),
        catalog.get_product("chrtout"),
        dt.datetime(2026, 5, 26, 0),
        240,
        3,
    )
    assert "medium_range_mem3/" in key
    assert "medium_range.channel_rt_3.f240.conus.nc" in key


# -- construction + OUTPUT_KIND --------------------------------------------


def test_output_kind_tabular(make_nwm):
    """A chrtout request is tabular."""
    assert make_nwm(variables={"chrtout": ["streamflow"]}).OUTPUT_KIND == "tabular"


def test_output_kind_raster(make_nwm):
    """A ldasout request is raster."""
    assert make_nwm(variables={"ldasout": ["SOIL_M"]}).OUTPUT_KIND == "raster"


def test_mixed_kind_rejected(make_nwm):
    """A request mixing tabular and raster products is rejected."""
    with pytest.raises(ValueError, match="share one output_kind"):
        make_nwm(variables={"chrtout": ["streamflow"], "ldasout": ["SOIL_M"]})


def test_empty_variables_rejected(make_nwm):
    """An empty variables mapping is rejected."""
    with pytest.raises(ValueError, match="non-empty"):
        make_nwm(variables={})


def test_unknown_product_rejected(make_nwm):
    """An unknown product key is rejected."""
    with pytest.raises(ValueError, match="not in the NWM catalog"):
        make_nwm(variables={"nope": ["x"]})


def test_unknown_variable_rejected(make_nwm):
    """An unknown variable name within a product is rejected."""
    with pytest.raises(ValueError, match="are not in product"):
        make_nwm(variables={"chrtout": ["nope"]})


def test_empty_variable_list_selects_all(make_nwm):
    """An empty variable list is allowed (whole-file download)."""
    nwm = make_nwm(variables={"chrtout": []})
    assert nwm.OUTPUT_KIND == "tabular"


def test_product_not_in_configuration_rejected(tmp_path):
    """A product the configuration does not publish is rejected."""
    # `coastal` exists as a product but `short_range` (conus) does not carry it.
    with pytest.raises(ValueError, match="not published under"):
        NWM(
            start="2026-05-26",
            end="2026-05-26",
            variables={"coastal": ["elevation"]},
            lat_lim=[-90, 90],
            lon_lim=[-180, 180],
            configuration="short_range",
            path=str(tmp_path),
        )


def test_build_key_subhourly_width(catalog):
    """The Hawaii short-range domain uses a 5-digit forecast step token."""
    key = build_key(
        catalog.get_config("short_range_hawaii"),
        catalog.get_product("chrtout"),
        dt.datetime(2026, 5, 26, 0),
        15,
        1,
    )
    assert key.endswith("nwm.t00z.short_range.channel_rt.f00015.hawaii.nc")


def test_coastal_product_is_tabular(make_nwm):
    """A coastal total_water request resolves to a tabular instance."""
    nwm = make_nwm(
        variables={"coastal": ["elevation"]},
        configuration="short_range_coastal_pacific",
    )
    assert nwm.OUTPUT_KIND == "tabular"


def test_forcing_product_is_raster(make_nwm):
    """A forcing request resolves to a raster instance."""
    nwm = make_nwm(variables={"forcing": ["T2D"]}, configuration="forcing_short_range")
    assert nwm.OUTPUT_KIND == "raster"


def test_bad_member_rejected(make_nwm):
    """A member out of the ensemble range is rejected."""
    with pytest.raises(ValueError, match="out of range"):
        make_nwm(configuration="medium_range", member=9)


# -- cycle / step resolution -----------------------------------------------


def test_unknown_cycle_rejected(make_nwm):
    """Requesting a run hour the configuration does not run is rejected."""
    nwm = make_nwm(configuration="medium_range", cycles=[3])
    with pytest.raises(ValueError, match="not run by configuration"):
        nwm._search()


def test_step_over_horizon_rejected(make_nwm):
    """A step beyond the configuration horizon is rejected."""
    nwm = make_nwm(steps=[99])
    with pytest.raises(ValueError, match="exceed the 18 h horizon"):
        nwm._search()


def test_horizon_expands_steps(make_nwm):
    """horizon= expands steps from first_step on the cadence."""
    nwm = make_nwm(cycles=[0], horizon=3)
    keys = [rp.metadata["step"] for rp in nwm._search()]
    assert keys == [1, 2, 3]


def test_default_step_is_first_step(make_nwm):
    """With no steps/horizon the single first_step is fetched."""
    nwm = make_nwm(cycles=[0])
    assert [rp.metadata["step"] for rp in nwm._search()] == [1]


# -- search enumeration -----------------------------------------------------


def test_search_crosses_cycles_steps_products(make_nwm):
    """_search enumerates one product per (cycle, step, product)."""
    nwm = make_nwm(cycles=[0, 12], steps=[1, 2])
    products = nwm._search()
    assert len(products) == 4
    assert all(rp.metadata["mode"] == "operational" for rp in products)


def test_search_keys_match_layout(make_nwm):
    """Enumerated hrefs are valid noaa-nwm-pds keys."""
    nwm = make_nwm(cycles=[0], steps=[1])
    href = nwm._search()[0].href
    assert (
        href == "nwm.20260526/short_range/nwm.t00z.short_range.channel_rt.f001.conus.nc"
    )


# -- mode resolution --------------------------------------------------------


def test_mode_recent_window_is_operational(make_nwm):
    """A recent window auto-routes to operational."""
    assert make_nwm()._mode == "operational"


def test_mode_old_window_is_retrospective(make_nwm):
    """A window far in the past auto-routes to retrospective."""
    nwm = make_nwm(start="1995-01-01", end="1995-01-02")
    assert nwm._mode == "retrospective"


def test_explicit_mode_wins(make_nwm):
    """An explicit mode= overrides the date heuristic."""
    assert make_nwm(mode="retrospective")._mode == "retrospective"


def test_invalid_mode_rejected(make_nwm):
    """An unrecognised mode= is rejected."""
    with pytest.raises(ValueError, match="must be 'operational'"):
        make_nwm(mode="archive")


# -- subset detection -------------------------------------------------------


def test_whole_earth_is_not_a_subset(make_nwm):
    """A whole-Earth bbox with no sites is not a subset."""
    assert make_nwm()._wants_subset() is False


def test_narrow_bbox_is_a_subset(make_nwm):
    """A narrower bbox is a subset request."""
    assert make_nwm(lat_lim=[30, 40], lon_lim=[-100, -90])._wants_subset() is True


def test_sites_is_a_subset(make_nwm):
    """An explicit sites= list is a subset request."""
    assert make_nwm(sites=[101])._wants_subset() is True


# -- download (faked S3) ----------------------------------------------------


def test_download_writes_whole_files(make_nwm, patch_client):
    """A no-subset operational download writes one NetCDF per key."""
    nwm = make_nwm(cycles=[0, 12], steps=[1])
    fake = patch_client(nwm, FakeS3(available=None))
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 2
    assert all(p.exists() and p.read_bytes().startswith(b"netcdf:") for p in paths)
    assert all(bucket == BUCKET for bucket, _ in fake.requested)


def test_download_multi_day_writes_distinct_files(make_nwm, patch_client):
    """A multi-day window with the same cycle/step writes one file per day.

    The NWM basename omits the date (it lives in the S3 key prefix), so the
    output name must flatten the full key to stay unique — otherwise day 2
    would overwrite day 1.
    """
    nwm = make_nwm(
        start="2026-05-26",
        end="2026-05-28",
        configuration="analysis_assim",
        cycles=[0],
        steps=[0],
    )
    patch_client(nwm, FakeS3(available=None))
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 3
    # distinct on-disk files (no overwrite), each carrying its day's bytes
    assert len({str(p) for p in paths}) == 3
    assert {p.read_bytes() for p in paths} == {
        b"netcdf:" + key.encode() for key in (rp.href for rp in nwm._search())
    }


def test_download_skips_unpublished_keys(make_nwm, patch_client):
    """A key that is not published is skipped, not fatal."""
    nwm = make_nwm(cycles=[0, 12], steps=[1])
    published = {nwm._search()[0].href}
    patch_client(nwm, FakeS3(available=published))
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 1


def test_download_aggregate_rejected(make_nwm):
    """download(aggregate=...) is rejected for NWM."""
    with pytest.raises(NotImplementedError, match="aggregate"):
        make_nwm().download(aggregate=object())


def test_retrospective_tabular_writes_parquet(make_nwm, fake_reader):
    """A retrospective chrtout request reads the Zarr and writes a Parquet table."""
    nwm = make_nwm(mode="retrospective", sites=[101, 179])
    reader = fake_reader(nwm)
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 1 and paths[0].suffix == ".parquet" and paths[0].exists()
    methods = [c[0] for c in reader.calls]
    assert methods == ["read_file", "select", "select_time", "to_parquet"]
    # opened the product's retro Zarr anonymously
    assert reader.calls[0][1]["anon"] is True
    assert reader.calls[0][1]["path"].endswith("chrtout.zarr")
    # selected the requested feature_ids
    assert reader.calls[1][1] == {"feature_id": [101, 179]}


def test_retrospective_gage_ids_join(make_nwm, fake_reader):
    """USGS gage_id strings route to select_by_coord, not feature_id select."""
    nwm = make_nwm(mode="retrospective", sites=["01010000", "01010500"])
    reader = fake_reader(nwm)
    nwm.download(progress_bar=False)
    methods = [c[0] for c in reader.calls]
    assert "select_by_coord" in methods
    by_coord = next(c for c in reader.calls if c[0] == "select_by_coord")
    assert by_coord[1] == ("gage_id", ["01010000", "01010500"])


def test_retrospective_bbox_selection(make_nwm, fake_reader):
    """A bbox (no sites=) routes to select_bbox with (W, S, E, N)."""
    nwm = make_nwm(mode="retrospective", lat_lim=[39.0, 40.0], lon_lim=[-77.0, -76.0])
    reader = fake_reader(nwm)
    nwm.download(progress_bar=False)
    bbox = next(c for c in reader.calls if c[0] == "select_bbox")
    assert bbox[1] == (-77.0, -76.0, 40.0, 39.0) or bbox[1] == (
        -77.0,
        39.0,
        -76.0,
        40.0,
    )


def test_retrospective_gridded_rejected(make_nwm):
    """A retrospective request for a gridded product is rejected."""
    nwm = make_nwm(
        variables={"ldasout": ["SOIL_M"]},
        configuration="analysis_assim",
        mode="retrospective",
        sites=[101],
    )
    with pytest.raises(NotImplementedError, match="gridded"):
        nwm.download(progress_bar=False)


def test_operational_subset_downloads_then_reads(make_nwm, fake_reader, patch_client):
    """An operational sites= subset downloads the file then reads + writes Parquet."""
    nwm = make_nwm(cycles=[0], steps=[1], sites=[101])
    patch_client(nwm, FakeS3(available=None))
    reader = fake_reader(nwm)
    paths = nwm.download(progress_bar=False)
    assert len(paths) == 1 and paths[0].suffix == ".parquet"
    methods = [c[0] for c in reader.calls]
    # operational subset selects labels but does NOT time-slice a single step
    assert "read_file" in methods and "select" in methods
    assert "select_time" not in methods
    assert reader.calls[0][1]["anon"] is False  # local downloaded file


def test_operational_gridded_bbox_writes_geotiff(make_nwm, fake_netcdf, patch_client):
    """An operational gridded bbox subset reads + crops each variable to GeoTIFF."""
    nwm = make_nwm(
        variables={"ldasout": ["SNEQV", "SNOWH"]},
        configuration="short_range",
        cycles=[0],
        steps=[1],
        lat_lim=[39, 40],
        lon_lim=[-78, -75],
    )
    patch_client(nwm, FakeS3(available=None))
    netcdf = fake_netcdf(nwm)
    paths = nwm.download(progress_bar=False)
    # one GeoTIFF per requested variable
    assert len(paths) == 2 and all(p.suffix == ".tif" for p in paths)
    subsets = [c for c in netcdf.calls if c[0] == "subset"]
    assert [c[1]["variable"] for c in subsets] == ["SNEQV", "SNOWH"]
    # bbox forwarded as (W, S, E, N), time=0 (single operational timestep)
    assert subsets[0][1]["time"] == 0
    assert subsets[0][1]["bbox"] == (-78.0, 39.0, -75.0, 40.0)


def test_operational_gridded_sites_rejected(make_nwm, patch_client):
    """sites= does not apply to a gridded product (raises ValueError)."""
    nwm = make_nwm(
        variables={"ldasout": ["SNEQV"]},
        configuration="short_range",
        cycles=[0],
        steps=[1],
        sites=[101],
    )
    patch_client(nwm, FakeS3(available=None))
    with pytest.raises(ValueError, match="does not apply to the gridded"):
        nwm.download(progress_bar=False)


def test_operational_gridded_interleaved_layer_var_rejected(
    make_nwm, monkeypatch, patch_client
):
    """A multi-layer (interleaved) variable raises a clear NotImplementedError."""
    nwm = make_nwm(
        variables={"ldasout": ["SOIL_M"]},
        configuration="short_range",
        cycles=[0],
        steps=[1],
        lat_lim=[39, 40],
        lon_lim=[-78, -75],
    )
    patch_client(nwm, FakeS3(available=None))

    class _Interleaved:
        dataset = None

        @classmethod
        def read_file(cls, path, **kw):
            return cls()

        def subset(self, *a, **k):
            raise ValueError(
                "the y dimension 'soil_layers_stag' has no 1-D coordinate variable"
            )

        def close(self):
            pass

    monkeypatch.setattr(nwm, "_netcdf_reader", lambda: _Interleaved)
    with pytest.raises(NotImplementedError, match="layer dimension interleaved"):
        nwm.download(progress_bar=False)


def test_retrospective_search_carries_zarr_uri(make_nwm):
    """Retrospective _search points at the product's Zarr store."""
    nwm = make_nwm(mode="retrospective")
    href = nwm._search()[0].href
    assert href.startswith("s3://noaa-nwm-retrospective-3-0-pds")


def test_feature_ids_and_gage_ids_split(make_nwm):
    """sites= splits into integer feature_ids and string gage_ids."""
    nwm = make_nwm(sites=[101, "01010000", 179])
    assert nwm._feature_ids() == [101, 179]
    assert nwm._gage_ids() == ["01010000"]
    assert make_nwm()._feature_ids() is None and make_nwm()._gage_ids() is None


def test_operational_subset_skips_unpublished(make_nwm, fake_reader, patch_client):
    """An unpublished key in an operational subset is skipped, not read."""
    nwm = make_nwm(cycles=[0], steps=[1], sites=[101])
    patch_client(nwm, FakeS3(available=set()))  # nothing published
    reader = fake_reader(nwm)
    paths = nwm.download(progress_bar=False)
    assert paths == []
    assert reader.calls == []  # nothing downloaded -> reader never invoked


def test_close_quietly_swallows_errors():
    """_close_quietly never raises, even when the store's close() fails."""
    from earthlens.nwm.backend import _close_quietly

    class _Bad:
        @property
        def dataset(self):
            raise RuntimeError("boom")

    _close_quietly(_Bad())  # must not raise


def test_api_composes_search_fetch(make_nwm, patch_client):
    """_api returns the fetched paths via the search/fetch composition."""
    nwm = make_nwm(cycles=[0], steps=[1])
    patch_client(nwm, FakeS3(available=None))
    paths = nwm._api()
    assert len(paths) == 1 and paths[0].exists()


def test_client_builds_unsigned_boto3(make_nwm):
    """_client returns a live unsigned boto3 S3 client."""
    client = make_nwm()._client()
    assert client.meta.service_model.service_name == "s3"


def test_fetch_reraises_non_missing_error(make_nwm, monkeypatch):
    """A non-404 S3 error is re-raised, not skipped."""
    from botocore.exceptions import ClientError

    class _Denying:
        def get_object(self, Bucket, Key):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    nwm = make_nwm(cycles=[0], steps=[1])
    monkeypatch.setattr(nwm, "_client", lambda: _Denying())
    with pytest.raises(ClientError):
        nwm.download(progress_bar=False)


# -- helpers ----------------------------------------------------------------


def test_is_missing_key_true():
    """A NoSuchKey client error is recognised as a missing key."""

    class _Exc(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    assert _is_missing_key(_Exc()) is True


def test_is_missing_key_false_on_other_error():
    """A non-404 error is not treated as a missing key."""

    class _Exc(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    assert _is_missing_key(_Exc()) is False


def test_is_missing_key_false_without_response():
    """An exception without a response dict is not a missing key."""
    assert _is_missing_key(RuntimeError("boom")) is False


def test_is_int_excludes_bool():
    """_is_int accepts ints but rejects bool (an int subclass)."""
    assert _is_int(101) is True
    assert _is_int(True) is False and _is_int("01010000") is False


# -- lazy-import error branches ---------------------------------------------


def test_reader_missing_dep_friendly_error(make_nwm, monkeypatch):
    """A missing LabeledDataset surfaces a friendly ImportError naming the extra."""
    monkeypatch.setitem(
        sys.modules, "pyramids.netcdf", types.ModuleType("pyramids.netcdf")
    )
    with pytest.raises(ImportError, match=r"earthlens\[nwm\]"):
        make_nwm()._reader()


def test_netcdf_reader_missing_dep_friendly_error(make_nwm, monkeypatch):
    """A missing NetCDF reader surfaces a friendly ImportError naming the extra."""
    monkeypatch.setitem(
        sys.modules, "pyramids.netcdf", types.ModuleType("pyramids.netcdf")
    )
    with pytest.raises(ImportError, match=r"earthlens\[nwm\]"):
        make_nwm()._netcdf_reader()


def test_client_missing_boto3_friendly_error(make_nwm, monkeypatch):
    """A missing boto3 surfaces a friendly ImportError naming the extra."""
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(ImportError, match=r"earthlens\[nwm\]"):
        make_nwm()._client()


def test_readers_resolve_pyramids_classes(make_nwm):
    """The lazy readers resolve the real pyramids classes when installed."""
    nwm = make_nwm()
    assert nwm._reader().__name__ == "LabeledDataset"
    assert nwm._netcdf_reader().__name__ == "NetCDF"


def test_gridded_other_value_error_propagates(make_nwm, monkeypatch, patch_client):
    """A non-interleaved ValueError from subset propagates unchanged (not wrapped)."""
    nwm = make_nwm(
        variables={"ldasout": ["SNEQV"]},
        configuration="short_range",
        cycles=[0],
        steps=[1],
        lat_lim=[39, 40],
        lon_lim=[-78, -75],
    )
    patch_client(nwm, FakeS3(available=None))

    class _Boom:
        dataset = None

        @classmethod
        def read_file(cls, path, **kw):
            return cls()

        def subset(self, *a, **k):
            raise ValueError("something else entirely")

        def close(self):
            pass

    monkeypatch.setattr(nwm, "_netcdf_reader", lambda: _Boom)
    with pytest.raises(ValueError, match="something else entirely"):
        nwm.download(progress_bar=False)


# -- no xarray / zarr in the source -----------------------------------------


@pytest.mark.parametrize("name", ["backend.py", "catalog.py", "__init__.py", "auth.py"])
def test_no_xarray_or_zarr_import(name):
    """The NWM source never imports xarray or zarr (PY-G owns the read)."""
    banned = ("import xarray", "import zarr", "from xarray", "from zarr")
    code_lines = [
        line.strip() for line in (_SRC / name).read_text(encoding="utf-8").splitlines()
    ]
    offenders = [line for line in code_lines if any(line.startswith(b) for b in banned)]
    assert offenders == []
