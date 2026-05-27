"""Unit + integration tests for the NWM backend (faked S3, no network)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.nwm import BUCKET, NWM
from earthlens.nwm.backend import (
    _is_missing_key,
    build_key,
    enumerate_cycles,
)
from tests.nwm.conftest import FakeS3

pytestmark = [pytest.mark.nwm]

_SRC = Path(__file__).resolve().parents[2] / "src" / "earthlens" / "nwm"


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


def test_download_bbox_subset_defers_to_pyg(make_nwm):
    """A bbox subset raises NotImplementedError naming PY-G."""
    nwm = make_nwm(lat_lim=[30, 40], lon_lim=[-100, -90])
    with pytest.raises(NotImplementedError, match="PY-G"):
        nwm.download(progress_bar=False)


def test_download_sites_defers_to_pyg(make_nwm):
    """A sites= subset raises NotImplementedError naming PY-G."""
    with pytest.raises(NotImplementedError, match="PY-G"):
        make_nwm(sites=[101]).download(progress_bar=False)


def test_download_retrospective_defers_to_pyg(make_nwm):
    """A retrospective request raises NotImplementedError naming PY-G."""
    nwm = make_nwm(mode="retrospective")
    with pytest.raises(NotImplementedError, match="PY-G"):
        nwm.download(progress_bar=False)


def test_retrospective_search_carries_zarr_uri(make_nwm):
    """Retrospective _search points at the product's Zarr store."""
    nwm = make_nwm(mode="retrospective")
    href = nwm._search()[0].href
    assert href.startswith("s3://noaa-nwm-retrospective-3-0-pds")


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
