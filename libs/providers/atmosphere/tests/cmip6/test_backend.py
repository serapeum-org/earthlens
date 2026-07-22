"""Unit tests for the CMIP6 backend (accessor stubbed; no network / GDAL)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent
from earthlens.cmip6 import CMIP6
from earthlens.cmip6 import accessor as accessor_mod

pytestmark = [pytest.mark.cmip6, pytest.mark.unit]


@pytest.fixture
def stub_accessor(monkeypatch):
    """Replace the pyramids accessor with recording stubs (no store I/O)."""
    calls = {"write": [], "window": []}

    def _write(zstore, variable, *, bbox, time, out_path, crs=4326):
        calls["write"].append(
            {"zstore": zstore, "variable": variable, "bbox": bbox, "time": time}
        )
        Path(out_path).write_bytes(b"nc")
        return Path(out_path)

    def _window(zstore, variable, start=None, end=None, *, time_dim="time"):
        calls["window"].append(zstore)
        return (0, 6)

    monkeypatch.setattr(accessor_mod, "write_subset", _write)
    monkeypatch.setattr(accessor_mod, "resolve_time_window", _window)
    return calls


@pytest.fixture
def backend(resolver, tmp_path):
    """A CMIP6 backend over the fixture resolver, writing to tmp_path."""
    return CMIP6(
        "2050-01-01",
        "2050-12-31",
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        member_id="r1i1p1f1",
        grid_label="gn",
        lat_lim=[35, 60],
        lon_lim=[-10, 30],
        path=tmp_path,
        resolver=resolver,
    )


def _make(resolver, tmp_path, **kwargs):
    """Build a CMIP6 backend with defaults filled in for a facet request."""
    params = dict(
        source_id="CanESM5",
        experiment_id="ssp585",
        variable_id="tas",
        table_id="Amon",
        grid_label="gn",
        lat_lim=[35, 60],
        lon_lim=[-10, 30],
        path=tmp_path,
        resolver=resolver,
    )
    params.update(kwargs)
    return CMIP6("2050-01-01", "2050-12-31", **params)


@pytest.mark.parametrize(
    "missing", ["source_id", "experiment_id", "variable_id", "table_id"]
)
def test_required_facets(resolver, tmp_path, missing):
    """An empty required facet raises a clear ValueError."""
    with pytest.raises(ValueError, match=missing):
        _make(resolver, tmp_path, **{missing: ""})


@pytest.mark.parametrize("start, end", [(None, "2050-12-31"), ("2050-01-01", None)])
def test_missing_dates_raise_friendly(resolver, tmp_path, start, end):
    """Omitting start or end raises a clear ValueError, not a strptime error."""
    with pytest.raises(ValueError, match="requires a start and end date"):
        CMIP6(
            start,
            end,
            source_id="CanESM5",
            experiment_id="ssp585",
            variable_id="tas",
            table_id="Amon",
            lat_lim=[35, 60],
            lon_lim=[-10, 30],
            path=tmp_path,
            resolver=resolver,
        )


def test_output_kind_is_raster(backend):
    """The backend declares raster output."""
    assert backend.OUTPUT_KIND == "raster"


def test_member_id_defaults_from_catalog(resolver, tmp_path):
    """Omitting member_id falls back to the catalog default."""
    b = _make(resolver, tmp_path, member_id=None)
    assert b._member_id == "r1i1p1f1"


def test_create_grid_and_dates(backend):
    """The backend captures the bbox and window as frozen value objects."""
    assert isinstance(backend.space, SpatialExtent)
    assert isinstance(backend.time, TemporalExtent)
    assert backend.space.latitude_min == 35.0


def test_bbox_narrow(backend):
    """A narrower box is returned as a (west, south, east, north) tuple."""
    assert backend._bbox() == (-10.0, 35.0, 30.0, 60.0)
    assert backend._wants_spatial_subset() is True


def test_bbox_whole_earth_is_none(resolver, tmp_path):
    """A whole-Earth request yields no bbox (whole-grid read)."""
    b = _make(resolver, tmp_path, lat_lim=[-90, 90], lon_lim=[-180, 180])
    assert b._bbox() is None
    assert b._wants_spatial_subset() is False


def test_search_resolves_stores(backend):
    """_search returns one product per resolved store, carrying the store."""
    products = backend._search()
    assert len(products) == 1
    assert isinstance(products[0], RemoteProduct)
    assert products[0].href.endswith("v20190429/")
    assert products[0].metadata["store"].member_id == "r1i1p1f1"


def test_search_fans_out(resolver, tmp_path):
    """Leaving grid_label unset fans out to one product per grid."""
    b = _make(resolver, tmp_path, grid_label=None)
    grids = sorted(p.metadata["store"].grid_label for p in b._search())
    assert grids == ["gn", "gr"]


def test_time_selector_date_window(backend, stub_accessor):
    """The default time selector resolves the date window to an index range."""
    store = backend._search()[0].metadata["store"]
    assert backend._time_selector(store) == (0, 6)
    assert stub_accessor["window"]


def test_time_selector_whole_time(resolver, tmp_path):
    """whole_time selects the full series with a slice(None)."""
    b = _make(resolver, tmp_path, whole_time=True)
    store = b._search()[0].metadata["store"]
    assert b._time_selector(store) == slice(None)


def test_fetch_writes_one_netcdf_per_store(backend, stub_accessor):
    """_fetch writes one NetCDF per resolved store and returns the paths."""
    paths = backend.download(progress_bar=False)
    assert len(paths) == 1
    assert paths[0].exists()
    assert stub_accessor["write"][0]["variable"] == "tas"
    assert stub_accessor["write"][0]["bbox"] == (-10.0, 35.0, 30.0, 60.0)
    assert stub_accessor["write"][0]["time"] == (0, 6)


def test_fetch_whole_earth_passes_no_bbox(resolver, tmp_path, stub_accessor):
    """A whole-Earth request passes bbox=None to write the whole native grid."""
    b = _make(resolver, tmp_path, lat_lim=[-90, 90], lon_lim=[-180, 180])
    b.download(progress_bar=False)
    assert stub_accessor["write"][0]["bbox"] is None


def test_download_rejects_aggregate(backend):
    """download(aggregate=...) is unsupported and raises NotImplementedError."""
    with pytest.raises(NotImplementedError, match="aggregate"):
        backend.download(aggregate=object())


def test_download_raises_on_facet_miss(resolver, tmp_path, stub_accessor):
    """A facet tuple that resolves to no store surfaces as a resolver error."""
    b = _make(resolver, tmp_path, experiment_id="ssp585", table_id="Amon")
    b._experiment_id = "ssp999"
    with pytest.raises(ValueError, match="ssp999"):
        b.download(progress_bar=False)


def test_terms_note(backend):
    """The backend surfaces the requested model's attribution note."""
    assert "CanESM5" in backend.terms_note()


def test_api_composes_search_fetch(backend, stub_accessor):
    """_api runs the search/fetch composition and returns written paths."""
    paths = backend._api()
    assert len(paths) == 1
    assert paths[0].exists()
