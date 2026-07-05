"""Unit tests for the CMIP6 pyramids accessor (stubbed readers, no GDAL)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from earthlens.cmip6 import accessor
from earthlens.cmip6.accessor import (
    GS_NO_SIGN_ENV,
    anonymous_gcs,
    resolve_time_window,
    store_output_stem,
    variable_names,
    write_subset,
    zstore_to_vsi,
)
from earthlens.cmip6.resolver import ResolvedStore
from tests.cmip6.conftest import FakeContainer, FakeLabeled

pytestmark = [pytest.mark.cmip6, pytest.mark.unit]

_SRC_MODULES = ["backend", "catalog", "resolver", "accessor", "__init__"]
_FORBIDDEN = ["xarray", "zarr", "gcsfs", "fsspec", "intake_esm", "intake-esm"]


@pytest.mark.parametrize(
    "store, expected",
    [
        ("gs://cmip6/a/b/", 'ZARR:"/vsigs/cmip6/a/b/"'),
        ("/vsigs/cmip6/a/b/", 'ZARR:"/vsigs/cmip6/a/b/"'),
        ('ZARR:"/vsigs/cmip6/a/b/"', 'ZARR:"/vsigs/cmip6/a/b/"'),
    ],
)
def test_zstore_to_vsi(store, expected):
    """A gs:// / vsigs / ZARR store URI maps to the GDAL multidim path."""
    assert zstore_to_vsi(store) == expected


def test_zstore_to_vsi_rejects_other_schemes():
    """A non-gs store URI raises a clear ValueError."""
    with pytest.raises(ValueError, match="/vsigs/"):
        zstore_to_vsi("s3://bucket/a/b/")


def test_anonymous_gcs_sets_and_restores_absent(monkeypatch):
    """The context sets the anon flag and removes it when it was unset."""
    monkeypatch.delenv(GS_NO_SIGN_ENV, raising=False)
    with anonymous_gcs():
        assert accessor.os.environ[GS_NO_SIGN_ENV] == "YES"
    assert GS_NO_SIGN_ENV not in accessor.os.environ


def test_anonymous_gcs_restores_prior_value(monkeypatch):
    """A pre-existing flag value is restored after the block."""
    monkeypatch.setenv(GS_NO_SIGN_ENV, "NO")
    with anonymous_gcs():
        assert accessor.os.environ[GS_NO_SIGN_ENV] == "YES"
    assert accessor.os.environ[GS_NO_SIGN_ENV] == "NO"


def test_resolve_time_window_none_when_no_bounds():
    """With neither bound given, no time window is resolved."""
    assert resolve_time_window("gs://cmip6/x/", "tas", None, None) is None


def test_reader_factories_return_pyramids_classes():
    """The lazy reader factories return the real pyramids reader classes."""
    from pyramids.netcdf import LabeledDataset, NetCDF

    assert accessor._netcdf_reader() is NetCDF
    assert accessor._labeled_reader() is LabeledDataset


def test_resolve_time_window_empty_store_is_none(monkeypatch):
    """A store with a zero-length time axis resolves to no window."""

    class _EmptyReader:
        @staticmethod
        def read_file(store, *, variables=None, anon=False):
            return FakeLabeled(kept=[])

    monkeypatch.setattr(accessor, "_labeled_reader", lambda: _EmptyReader)
    assert resolve_time_window("gs://cmip6/x/", "tas", "2015-01-01", "2015-06-30") is None


def test_resolve_time_window_computes_index_range(monkeypatch):
    """A date window maps to the half-open integer index range."""
    monkeypatch.setattr(accessor, "_labeled_reader", lambda: _FakeLabeledReader)
    window = resolve_time_window("gs://cmip6/x/", "tas", "2015-01-01", "2015-06-30")
    assert window == (0, 6)


def test_resolve_time_window_empty_raises(monkeypatch):
    """A window that selects no steps raises a clear ValueError."""
    monkeypatch.setattr(accessor, "_labeled_reader", lambda: _FakeLabeledReader)
    with pytest.raises(ValueError, match="no CMIP6 timesteps"):
        resolve_time_window("gs://cmip6/x/", "tas", "2099-01-01", "2099-06-30")


def test_resolve_time_window_wraps_select_error(monkeypatch):
    """A select_time ValueError is wrapped with the store context."""
    monkeypatch.setattr(accessor, "_labeled_reader", lambda: _RaisingLabeledReader)
    with pytest.raises(ValueError, match="no CMIP6 timesteps"):
        resolve_time_window("gs://cmip6/x/", "tas", "2015-01-01", "2015-06-30")


def test_write_subset_reads_and_writes(monkeypatch, tmp_path):
    """write_subset opens the store, applies the window, and writes NetCDF."""
    container = FakeContainer()
    monkeypatch.setattr(accessor, "_netcdf_reader", lambda: _reader_returning(container))
    out = tmp_path / "tas.nc"
    result = write_subset(
        "gs://cmip6/x/", "tas", bbox=(-10, 40, 10, 55), time=(0, 6), out_path=out
    )
    assert result == out
    assert container.calls[0]["variable"] == "tas"
    assert container.calls[0]["time"] == (0, 6)
    assert container.calls[0]["bbox"] == (-10, 40, 10, 55)
    assert container.last_subset.written == str(out)
    assert container.closed is True


def test_write_subset_anonymous_during_read(monkeypatch, tmp_path):
    """The anon flag is live while the store is being read."""
    seen = {}

    class _Recorder(FakeContainer):
        def subset(self, variable, **kwargs):
            seen["flag"] = accessor.os.environ.get(GS_NO_SIGN_ENV)
            return super().subset(variable, **kwargs)

    monkeypatch.delenv(GS_NO_SIGN_ENV, raising=False)
    monkeypatch.setattr(accessor, "_netcdf_reader", lambda: _reader_returning(_Recorder()))
    write_subset("gs://cmip6/x/", "tas", bbox=None, time=0, out_path=tmp_path / "o.nc")
    assert seen["flag"] == "YES"


def test_variable_names(monkeypatch):
    """variable_names lists the store's data variables anonymously."""
    monkeypatch.setattr(
        accessor, "_netcdf_reader", lambda: _reader_returning(FakeContainer(["tas", "pr"]))
    )
    assert variable_names("gs://cmip6/x/") == ["tas", "pr"]


def test_store_output_stem_with_dates():
    """The output stem appends the window's start/end dates to the slug."""
    store = ResolvedStore(
        zstore="gs://cmip6/x/", source_id="CanESM5", experiment_id="ssp585",
        variable_id="tas", table_id="Amon", member_id="r1i1p1f1", grid_label="gn",
        version="1",
    )
    stem = store_output_stem(store, dt.datetime(2050, 1, 1), dt.datetime(2050, 12, 31))
    assert stem == "CanESM5_ssp585_tas_Amon_r1i1p1f1_gn_20500101_20501231"


def test_store_output_stem_without_dates():
    """A window with no date objects yields the bare facet slug."""
    store = ResolvedStore(
        zstore="gs://cmip6/x/", source_id="M", experiment_id="e", variable_id="v",
        table_id="Amon", member_id="r1i1p1f1", grid_label="gn", version="1",
    )
    assert store_output_stem(store, None, None) == store.slug


def test_close_quietly_swallows_errors():
    """A handle whose close() raises is closed without propagating."""

    class _Bad:
        def close(self):
            raise RuntimeError("boom")

    accessor._close_quietly(_Bad())


def test_no_forbidden_imports():
    """No cmip6 source module imports xarray/zarr/gcsfs/fsspec/intake-esm."""
    root = Path(accessor.__file__).parent
    for module in _SRC_MODULES:
        text = (root / f"{module}.py").read_text(encoding="utf-8")
        for banned in _FORBIDDEN:
            assert f"import {banned}" not in text, f"{module}.py imports {banned}"
            assert f"from {banned}" not in text, f"{module}.py imports from {banned}"


class _FakeLabeledReader:
    """Reader whose read_file returns a fixed-axis FakeLabeled."""

    @staticmethod
    def read_file(store, *, variables=None, anon=False):
        """Return a fake labelled dataset (ignoring the store)."""
        return FakeLabeled()


class _RaisingLabeledReader:
    """Reader whose select_time raises to exercise the wrap branch."""

    @staticmethod
    def read_file(store, *, variables=None, anon=False):
        """Return a fake dataset whose select_time always raises."""
        return _RaisingLabeled()


class _RaisingLabeled(FakeLabeled):
    """A labelled dataset whose select_time raises ValueError."""

    def select_time(self, start=None, end=None, *, time_dim="time"):
        """Always raise, mimicking an empty-window select_time."""
        raise ValueError("no timesteps in window")


def _reader_returning(container):
    """Build a fake NetCDF reader class whose read_file returns `container`."""

    class _Reader:
        @staticmethod
        def read_file(path):
            return container

    return _Reader
