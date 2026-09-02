"""Unit + integration tests for `earthlens.cmems.backend`."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.base import RemoteProduct, SpatialExtent, TemporalExtent, safe_filename
from earthlens.cmems import CMEMS, AuthenticationError, CmemsAuth
from earthlens.cmems.backend import _unique_output_names


class _FakeResponse:
    """Stand-in for `copernicusmarine.ResponseSubset`."""

    def __init__(self, file_path: Path):
        self.file_path = str(file_path)
        self.status = "ok"


class _FakeCmems(types.ModuleType):
    """Stub `copernicusmarine` module supporting login + subset."""

    def __init__(self):
        super().__init__("copernicusmarine")
        self.login_calls: list[dict[str, Any]] = []
        self.subset_calls: list[dict[str, Any]] = []
        self.subset_response: _FakeResponse | None = None
        self.subset_raises: BaseException | None = None
        # Per-dataset failures keyed by dataset_id, for partial-failure
        # tests. Checked before the blanket `subset_raises`.
        self.subset_raises_for: dict[str, BaseException] = {}
        self.login_result: bool | BaseException = True
        self.InvalidUsernameOrPassword = type(
            "InvalidUsernameOrPassword", (Exception,), {}
        )
        self.CouldNotConnectToAuthenticationSystem = type(
            "CouldNotConnectToAuthenticationSystem", (Exception,), {}
        )
        self.CredentialsCannotBeNone = type("CredentialsCannotBeNone", (Exception,), {})

    def login(self, **kwargs: Any) -> bool:
        self.login_calls.append(dict(kwargs))
        if isinstance(self.login_result, BaseException):
            raise self.login_result
        return bool(self.login_result)

    def subset(self, **kwargs: Any) -> _FakeResponse:
        self.subset_calls.append(dict(kwargs))
        per_ds = self.subset_raises_for.get(kwargs.get("dataset_id"))
        if per_ds is not None:
            raise per_ds
        if self.subset_raises is not None:
            raise self.subset_raises
        assert self.subset_response is not None, (
            "test forgot to set `subset_response` on the fake module"
        )
        return self.subset_response


@pytest.fixture
def fake_cmems(monkeypatch: pytest.MonkeyPatch) -> _FakeCmems:
    fake = _FakeCmems()
    monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
    return fake


# Records (dim, how[, groupby_distinct]) for each NetCDF.reduce call so the
# aggregate wiring test can assert depth-then-time reduction.
_FAKE_REDUCE_CALLS: list[dict[str, Any]] = []


class _FakeVar:
    """Stand-in for a reduced pyramids variable subset."""

    def __init__(self, n_windows: int):
        self._n = n_windows
        self.geotransform = (0.0, 1.0, 0.0, 0.0, 0.0, -1.0)
        self.epsg = 4326

    def read_array(self) -> np.ndarray:
        # A single window means `reduce("time", ...)` squeezed the time axis away,
        # so pyramids hands back a 2-D (lat, lon) array; >1 window stays 3-D
        # (time, lat, lon). This is the only case that exercises the 2-D->3-D
        # reshape branch in `_aggregate_one`.
        if self._n == 1:
            return np.zeros((2, 2), dtype="float64")
        return np.zeros((self._n, 2, 2), dtype="float64")


class _FakeReducedNetCDF:
    """Stand-in for the multi-window NetCDF that `reduce("time", ...)` returns."""

    def __init__(self, n_windows: int):
        self.variable_names = ["thetao"]
        self._n = n_windows

    def get_variable(self, name: str) -> _FakeVar:
        return _FakeVar(self._n)


class _FakeNetCDF:
    """Minimal pyramids `NetCDF` stub exercising the aggregate path."""

    dimension_names = ("depth", "latitude", "longitude", "time")
    # CF-decoded time axis (pyramids `NetCDF.get_time_variable`) — 40 daily steps
    # -> Jan (31) + Feb (9) 2020 -> two monthly windows.
    _times = [d.isoformat() for d in pd.date_range("2020-01-01", periods=40, freq="D")]

    @classmethod
    def read_file(cls, path: str) -> _FakeNetCDF:
        return cls()

    def get_time_variable(self, var_name="time", time_format="%Y-%m-%d %H:%M:%S"):
        return self._times

    def reduce(self, dim, how="mean", *, groupby=None, skipna=True):
        call: dict[str, Any] = {"dim": dim, "how": how}
        if groupby is not None:
            call["groupby_distinct"] = len(set(groupby))
        _FAKE_REDUCE_CALLS.append(call)
        if dim == "depth":
            return self  # collapsed container, still reducible by time
        return _FakeReducedNetCDF(len(set(groupby)) if groupby else 1)

    def sel(self, **kwargs):
        return self


class _FakeDatasetWriter:
    def __init__(self, target_seen: list):
        self._seen = target_seen

    def to_file(self, path: str) -> None:
        Path(path).write_bytes(b"GTIFF")


class _FakeDataset:
    @staticmethod
    def from_array(arr, geo_ref=None, **kwargs) -> _FakeDatasetWriter:
        return _FakeDatasetWriter([])


def _install_fake_pyramids_reduce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire fake pyramids `NetCDF`/`Dataset` for the aggregate path (no xarray)."""
    _FAKE_REDUCE_CALLS.clear()

    netcdf_mod = types.ModuleType("pyramids.netcdf")
    netcdf_mod.NetCDF = _FakeNetCDF
    monkeypatch.setitem(sys.modules, "pyramids.netcdf", netcdf_mod)

    dataset_mod = types.ModuleType("pyramids.dataset")
    dataset_mod.Dataset = _FakeDataset
    # The real value object: pyramids.base.georeference is not faked, and the
    # aggregate path imports GeoReference from pyramids.dataset.
    from pyramids.base.georeference import GeoReference

    dataset_mod.GeoReference = GeoReference
    monkeypatch.setitem(sys.modules, "pyramids.dataset", dataset_mod)


@pytest.fixture
def cmems_instance(fake_cmems: _FakeCmems, tmp_path: Path) -> CMEMS:
    """Construct a CMEMS instance against the stub toolbox."""
    return CMEMS(
        start="2024-01-01",
        end="2024-01-02",
        variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao", "so"]},
        lat_lim=[40.0, 42.0],
        lon_lim=[-10.0, -8.0],
        temporal_resolution="daily",
        path=str(tmp_path),
        service_username="alice",
        service_password="secret",
    )


@pytest.mark.cmems
class TestCMEMSConstruction:
    """`__init__` wires `space` / `time` / OUTPUT_KIND correctly."""

    def test_output_kind_is_raster(self):
        """CMEMS declares raster output (gridded NetCDF/Zarr)."""
        assert CMEMS.OUTPUT_KIND == "raster", (
            f"CMEMS.OUTPUT_KIND must be 'raster' (the on-disk artefact is "
            f"a gridded NetCDF/Zarr), got {CMEMS.OUTPUT_KIND!r}"
        )

    def test_init_captures_space(self, cmems_instance: CMEMS):
        """The user bbox is captured onto `self.space` via `SpatialExtent`."""
        assert isinstance(cmems_instance.space, SpatialExtent), (
            "self.space must be a SpatialExtent instance"
        )
        assert cmems_instance.space.south == 40.0
        assert cmems_instance.space.north == 42.0
        assert cmems_instance.space.west == -10.0
        assert cmems_instance.space.east == -8.0

    def test_init_captures_time(self, cmems_instance: CMEMS):
        """The parsed dates land on `self.time` as a `TemporalExtent`."""
        assert isinstance(cmems_instance.time, TemporalExtent)
        assert cmems_instance.time.resolution == "D", (
            f"daily cadence should map to pandas 'D' freq, "
            f"got {cmems_instance.time.resolution!r}"
        )

    def test_construction_defers_authentication(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """Construction stores the auth offline but does not log in."""
        assert len(fake_cmems.login_calls) == 0, (
            f"construction must not authenticate, got {len(fake_cmems.login_calls)}"
        )
        assert cmems_instance._auth is not None, (
            "CmemsAuth must be stored on the instance"
        )
        assert isinstance(cmems_instance._auth, CmemsAuth)
        cmems_instance._auth.configure()
        assert len(fake_cmems.login_calls) == 1, "first configure() authenticates once"
        cmems_instance._auth.configure()
        assert len(fake_cmems.login_calls) == 1, "configure() is idempotent"

    def test_authenticate_runs_configure(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """authenticate() eagerly runs the deferred toolbox login once."""
        assert len(fake_cmems.login_calls) == 0, "construction must not authenticate"
        assert cmems_instance.authenticate() is cmems_instance, "returns self"
        assert len(fake_cmems.login_calls) == 1, "authenticate() runs configure once"


@pytest.mark.cmems
class TestCMEMSSearch:
    """`_search` returns one `RemoteProduct` per `(dataset, variables)` group."""

    def test_search_produces_one_product_per_dataset(self, cmems_instance: CMEMS):
        """One dataset id in `self.vars` → one product."""
        products = cmems_instance._search()
        assert len(products) == 1, f"expected 1 product, got {len(products)}"
        assert isinstance(products[0], RemoteProduct)
        assert products[0].id == "cmems_mod_glo_phy_my_0.083deg_P1D-m"

    def test_search_carries_variables_in_metadata(self, cmems_instance: CMEMS):
        """The variable list rides on `product.metadata['variables']`."""
        products = cmems_instance._search()
        assert products[0].metadata["variables"] == [
            "thetao",
            "so",
        ], f"variables not carried in metadata; got {products[0].metadata!r}"

    def test_search_multi_dataset(self, fake_cmems: _FakeCmems, tmp_path: Path):
        """Multiple datasets in `self.vars` → multiple products."""
        cm = CMEMS(
            start="2024-01-01",
            end="2024-01-02",
            variables={
                "ds-a": ["v1"],
                "ds-b": ["v2", "v3"],
            },
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            temporal_resolution="daily",
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        products = cm._search()
        assert [p.id for p in products] == ["ds-a", "ds-b"]
        assert products[1].metadata["variables"] == ["v2", "v3"]


@pytest.mark.cmems
class TestCMEMSFetch:
    """`_fetch` forwards the request to `copernicusmarine.subset`."""

    def test_fetch_returns_written_paths(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """`_fetch` returns one `Path` per successful subset call."""
        target = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        products = cmems_instance._search()
        paths = cmems_instance._fetch(products)
        assert paths == [target], f"_fetch should return [{target}], got {paths!r}"
        assert len(fake_cmems.subset_calls) == 1

    def test_subset_call_forwards_bbox(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """The bbox + dates are forwarded to the toolbox verbatim."""
        target = tmp_path / "out.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        cmems_instance._fetch(cmems_instance._search())
        call = fake_cmems.subset_calls[0]
        assert call["dataset_id"] == "cmems_mod_glo_phy_my_0.083deg_P1D-m"
        assert call["variables"] == ["thetao", "so"]
        assert call["minimum_longitude"] == -10.0
        assert call["maximum_longitude"] == -8.0
        assert call["minimum_latitude"] == 40.0
        assert call["maximum_latitude"] == 42.0
        assert call["overwrite"] is True

    def test_subset_call_uses_safe_filename(
        self, fake_cmems: _FakeCmems, tmp_path: Path
    ):
        """Filename strips path separators and other illegal chars."""
        cm = CMEMS(
            start="2024-01-01",
            end="2024-01-02",
            variables={"weird/id:foo*bar": ["v"]},
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        target = tmp_path / "out.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        cm._fetch(cm._search())
        filename = fake_cmems.subset_calls[0]["output_filename"]
        for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
            assert bad not in filename, (
                f"unsafe character {bad!r} leaked into output_filename: {filename!r}"
            )

    def test_total_failure_raises(self, fake_cmems: _FakeCmems, cmems_instance: CMEMS):
        """When every subset fails, _fetch raises rather than returning []."""
        fake_cmems.subset_raises = RuntimeError("server upset")
        with pytest.raises(RuntimeError, match="all 1 CMEMS subset"):
            cmems_instance._fetch(cmems_instance._search())

    def test_partial_failure_returns_successes(
        self, fake_cmems: _FakeCmems, tmp_path: Path
    ):
        """One failing dataset is dropped; the surviving dataset's path is returned."""
        good = tmp_path / "good.nc"
        good.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(good)
        fake_cmems.subset_raises_for = {"bad_ds": RuntimeError("server upset")}
        cm = CMEMS(
            start="2024-01-01",
            end="2024-01-02",
            variables={
                "bad_ds": ["x"],
                "cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"],
            },
            lat_lim=[40.0, 42.0],
            lon_lim=[-10.0, -8.0],
            temporal_resolution="daily",
            path=str(tmp_path),
            service_username="alice",
            service_password="secret",
        )
        paths = cm._fetch(cm._search())
        assert paths == [good], (
            f"partial failure should return only the survivor; got {paths!r}"
        )

    def test_errors_raise_propagates_the_first_failure(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """errors="raise" surfaces the toolbox exception, not the batch summary."""
        fake_cmems.subset_raises = RuntimeError("server upset")
        with pytest.raises(RuntimeError, match="server upset"):
            cmems_instance.download(errors="raise")

    def test_errors_rejects_an_unknown_policy(self, cmems_instance: CMEMS):
        """An unrecognised errors= value is refused before any request."""
        with pytest.raises(ValueError, match="errors"):
            cmems_instance.download(errors="explode")

    def test_subset_missing_file_path_raises(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """A response without file_path surfaces as FileNotFoundError."""

        class _NoPath:
            file_path = None
            status = "weird"

        fake_cmems.subset_response = _NoPath()  # type: ignore[assignment]
        with pytest.raises(FileNotFoundError, match="no file_path"):
            cmems_instance._subset_one(cmems_instance._search()[0], progress_bar=False)

    def test_invalid_credentials_midcall_wrapped(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS
    ):
        """An `InvalidUsernameOrPassword` mid-subset becomes `AuthenticationError`."""
        fake_cmems.subset_raises = fake_cmems.InvalidUsernameOrPassword("expired")
        with pytest.raises(AuthenticationError, match="mid-request"):
            cmems_instance._subset_one(cmems_instance._search()[0], progress_bar=False)


@pytest.mark.cmems
class TestCMEMSDownload:
    """End-to-end `download()` flow against the stub toolbox."""

    def test_download_writes_per_dataset(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """`download()` returns the list of written paths."""
        target = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        paths = cmems_instance.download(progress_bar=False)
        assert paths == [target], f"download() should return [{target}], got {paths!r}"

    def test_download_aggregate_guarded_without_reduce(
        self,
        fake_cmems: _FakeCmems,
        cmems_instance: CMEMS,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """`aggregate=` raises NotImplementedError when pyramids lacks `reduce`."""
        target = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)

        fake_netcdf_mod = types.ModuleType("pyramids.netcdf")
        fake_netcdf_mod.NetCDF = type("NetCDF", (), {})  # no `reduce` attr
        monkeypatch.setitem(sys.modules, "pyramids.netcdf", fake_netcdf_mod)

        with pytest.raises(NotImplementedError, match="NetCDF.reduce"):
            cmems_instance.download(
                progress_bar=False,
                aggregate=AggregationConfig(freq="1MS", op="mean"),
            )

    def test_download_aggregate_via_reduce(
        self,
        fake_cmems: _FakeCmems,
        cmems_instance: CMEMS,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """`aggregate=` reduces each subset into per-(variable, window) GeoTIFFs."""
        subset = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        subset.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(subset)
        _install_fake_pyramids_reduce(monkeypatch)

        paths = cmems_instance.download(
            progress_bar=False,
            aggregate=AggregationConfig(freq="1MS", op="mean"),
        )
        # 40 daily steps span Jan + Feb 2020 -> 2 monthly windows, one var.
        names = sorted(p.name for p in paths)
        assert names == [
            "cmems_mod_glo_phy_my_0.083deg_P1D-m_thetao_1MS_20200101.tif",
            "cmems_mod_glo_phy_my_0.083deg_P1D-m_thetao_1MS_20200201.tif",
        ], f"unexpected aggregate outputs: {names}"
        assert all(p.exists() for p in paths), "GeoTIFFs should be written"
        # depth collapsed (no level) + time windowed -> two reduce calls.
        assert _FAKE_REDUCE_CALLS == [
            {"dim": "depth", "how": "mean"},
            {"dim": "time", "how": "mean", "groupby_distinct": 2},
        ], f"unexpected reduce calls: {_FAKE_REDUCE_CALLS}"

    @pytest.mark.parametrize("bad_times", [None, []])
    def test_window_labels_raises_when_time_axis_undecodable(self, bad_times):
        """`_window_labels` raises a clear ValueError when the decoded axis is empty/None."""
        nc = types.SimpleNamespace(get_time_variable=lambda *a, **k: bad_times)
        with pytest.raises(ValueError, match="time"):
            CMEMS._window_labels(nc, "1MS")

    def test_window_labels_skips_empty_buckets(self):
        """Sparse timesteps under a fine `freq` leave empty Grouper buckets unlabelled."""
        nc = types.SimpleNamespace(
            get_time_variable=lambda *a, **k: ["2020-01-01", "2020-03-01"]
        )
        labels = CMEMS._window_labels(nc, "D")
        assert labels == [
            "20200101",
            "20200301",
        ], f"one label per timestep expected, empty daily buckets skipped; got {labels}"

    def test_download_aggregate_at_depth_level(
        self,
        fake_cmems: _FakeCmems,
        cmems_instance: CMEMS,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """`level=` selects a single depth via `sel` instead of collapsing it."""
        subset = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        subset.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(subset)
        _install_fake_pyramids_reduce(monkeypatch)

        paths = cmems_instance.download(
            progress_bar=False,
            aggregate=AggregationConfig(freq="1MS", op="mean", level=0.5),
        )
        assert len(paths) == 2, f"expected two monthly windows, got {len(paths)}"
        assert _FAKE_REDUCE_CALLS == [
            {"dim": "time", "how": "mean", "groupby_distinct": 2},
        ], f"depth should be `sel`-ected (no depth reduce), got: {_FAKE_REDUCE_CALLS}"

    def test_download_aggregate_single_window_reshapes_2d(
        self,
        fake_cmems: _FakeCmems,
        cmems_instance: CMEMS,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A freq collapsing all steps to one window yields a 2-D array, reshaped to 3-D."""
        subset = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        subset.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(subset)
        _install_fake_pyramids_reduce(monkeypatch)

        paths = cmems_instance.download(
            progress_bar=False,
            aggregate=AggregationConfig(freq="YS", op="mean"),
        )
        names = sorted(p.name for p in paths)
        assert names == [
            "cmems_mod_glo_phy_my_0.083deg_P1D-m_thetao_YS_20200101.tif",
        ], f"40 daily steps in one calendar year -> one window; got {names}"
        assert paths[0].exists(), "single-window GeoTIFF should be written"

    def test_aggregate_one_raises_without_time_dimension(
        self,
        cmems_instance: CMEMS,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """`_aggregate_one` rejects a NetCDF lacking a `time` dimension."""
        _install_fake_pyramids_reduce(monkeypatch)
        monkeypatch.setattr(
            _FakeNetCDF, "dimension_names", ("depth", "latitude", "longitude")
        )
        nc_path = tmp_path / "no_time.nc"
        nc_path.write_bytes(b"")
        with pytest.raises(ValueError, match="no `time` dimension"):
            cmems_instance._aggregate_one(
                nc_path, AggregationConfig(freq="1MS", op="mean"), "mean"
            )

    def test_aggregate_one_without_depth_skips_depth_reduce(
        self,
        cmems_instance: CMEMS,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A 2-D (no `depth`) NetCDF goes straight to the time reduce."""
        _install_fake_pyramids_reduce(monkeypatch)
        monkeypatch.setattr(
            _FakeNetCDF, "dimension_names", ("latitude", "longitude", "time")
        )
        nc_path = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        nc_path.write_bytes(b"")
        written = cmems_instance._aggregate_one(
            nc_path,
            AggregationConfig(freq="1MS", op="mean", out_dir=str(tmp_path)),
            "mean",
        )
        assert len(written) == 2, f"expected two monthly windows, got {len(written)}"
        assert _FAKE_REDUCE_CALLS == [
            {"dim": "time", "how": "mean", "groupby_distinct": 2},
        ], f"no depth dim -> only the time reduce should run, got: {_FAKE_REDUCE_CALLS}"

    def test_download_disable_progress_forwards(
        self, fake_cmems: _FakeCmems, cmems_instance: CMEMS, tmp_path: Path
    ):
        """`progress_bar=False` flips `disable_progress_bar=True` on the SDK call."""
        target = tmp_path / "out.nc"
        target.write_bytes(b"")
        fake_cmems.subset_response = _FakeResponse(target)
        cmems_instance.download(progress_bar=False)
        assert fake_cmems.subset_calls[0]["disable_progress_bar"] is True


@pytest.mark.cmems
class TestSafeFilename:
    """`safe_filename` strips OS-illegal chars from CMEMS dataset ids."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (
                "cmems_mod_glo_phy_my_0.083deg_P1D-m",
                "cmems_mod_glo_phy_my_0.083deg_P1D-m",
            ),
            ("a/b\\c:d", "a_b_c_d"),
            ('a*b?c"d<e>f|g', "a_b_c_d_e_f_g"),
        ],
    )
    def test_safe_filename(self, raw: str, expected: str):
        """Illegal characters are uniformly replaced with `_`."""
        assert safe_filename(raw) == expected, (
            f"safe_filename({raw!r}) should be {expected!r}, got {safe_filename(raw)!r}"
        )


@pytest.mark.cmems
class TestUniqueOutputNames:
    """`_unique_output_names` maps dataset ids to collision-free filenames."""

    def test_no_collision_keeps_clean_stems(self):
        """Distinct ids that don't normalise-collide keep their plain stems."""
        names = _unique_output_names(
            ["cmems_mod_glo_phy_my_0.083deg_P1D-m", "med-cmcc-tem-rean-d"], "nc"
        )
        assert names == {
            "cmems_mod_glo_phy_my_0.083deg_P1D-m": "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc",
            "med-cmcc-tem-rean-d": "med-cmcc-tem-rean-d.nc",
        }, f"clean stems should be untouched, got {names!r}"

    def test_collision_is_disambiguated_and_unique(self):
        """Two ids normalising to the same stem get distinct hash-suffixed names."""
        names = _unique_output_names(["a/b", "a_b"], "nc")
        assert len(set(names.values())) == 2, (
            f"colliding ids must get unique filenames, got {names!r}"
        )
        for value in names.values():
            assert value.startswith("a_b_") and value.endswith(".nc"), (
                f"disambiguated name should keep the stem + suffix, got {value!r}"
            )

    def test_disambiguation_is_deterministic(self):
        """The hash suffix is stable across calls (same id -> same name)."""
        first = _unique_output_names(["a/b", "a_b"], "nc")
        second = _unique_output_names(["a/b", "a_b"], "nc")
        assert first == second, (
            f"output names must be deterministic: {first} != {second}"
        )

    @pytest.mark.parametrize("ext", ["nc", "zarr"])
    def test_extension_applied(self, ext: str):
        """The supplied extension is appended to every filename."""
        names = _unique_output_names(["ds-1"], ext)
        assert names["ds-1"] == f"ds-1.{ext}", (
            f"expected ds-1.{ext}, got {names['ds-1']!r}"
        )

    def test_empty_input(self):
        """No dataset ids yields an empty map."""
        assert _unique_output_names([], "nc") == {}, "empty input should map to {}"
