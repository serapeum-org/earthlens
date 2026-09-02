"""Unit tests for the NWP backend (construction, search, fetch)."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.base import SpatialExtent
from earthlens.nwp import NWP
from earthlens.nwp import backend as backend_mod

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


def _make(mini_catalog, tmp_path, **kwargs):
    """Build an NWP bound to the in-memory mini catalog and tmp output dir."""
    params = dict(
        start="2024-06-01",
        end="2024-06-01",
        variables={"gfs": ["temperature_2m"]},
        lat_lim=[10, 20],
        lon_lim=[30, 40],
        path=str(tmp_path),
        catalog=mini_catalog,
    )
    params.update(kwargs)
    return NWP(**params)


class _LonDS:
    """Minimal Dataset stand-in tracking wrap_longitude behaviour."""

    def __init__(self, global_360: bool):
        self.global_360 = global_360
        self.converted = False

    def wrap_longitude(self):
        """Convert only when global; mirror pyramids' ValueError otherwise."""
        if not self.global_360:
            raise ValueError("The raster should cover the whole globe")
        self.converted = True
        return self


class _CountingCentre:
    """Centre stand-in counting fetch_one calls and recording args."""

    def __init__(self, save_dir):
        self.save_dir = save_dir
        self.calls = []

    def fetch_one(
        self, model, cycle, step, params, mirror, member=None, *, whole=False
    ):
        """Record the call (incl. `whole`) and return a fabricated GRIB path."""
        self.calls.append((cycle, step, tuple(params), mirror, member, whole))
        suffix = f"_m{member}" if member is not None else ""
        return str(
            self.save_dir
            / f"{model.model_family}_{cycle:%Y%m%d%H}_f{step:03d}{suffix}.grib2"
        )


class _FlakyCentre(_CountingCentre):
    """Centre stand-in that raises for one specific forecast step."""

    def __init__(self, save_dir, fail_step):
        super().__init__(save_dir)
        self.fail_step = fail_step

    def fetch_one(
        self, model, cycle, step, params, mirror, member=None, *, whole=False
    ):
        """Raise for `fail_step`; otherwise behave like the counting centre."""
        if step == self.fail_step:
            raise RuntimeError(f"f{step:03d} not published")
        return super().fetch_one(
            model, cycle, step, params, mirror, member, whole=whole
        )


class TestConstruction:
    """Tests for NWP.__init__ and _resolve_models."""

    def test_fixed_output_kind(self, mini_catalog, tmp_path):
        """The backend always declares raster output."""
        assert _make(mini_catalog, tmp_path).OUTPUT_KIND == "raster"

    def test_empty_variables_raises(self, mini_catalog, tmp_path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="non-empty"):
            _make(mini_catalog, tmp_path, variables={})

    def test_unknown_model_key_did_you_mean(self, mini_catalog, tmp_path):
        """An unknown model key surfaces the catalog did-you-mean hint."""
        with pytest.raises(ValueError, match="Did you mean 'gfs'"):
            _make(mini_catalog, tmp_path, variables={"gfsx": ["temperature_2m"]})

    def test_unknown_param_raises(self, mini_catalog, tmp_path):
        """A param absent from the model's band map is rejected."""
        with pytest.raises(ValueError, match="no band"):
            _make(mini_catalog, tmp_path, variables={"gfs": ["nope"]})

    def test_requests_preserve_order(self, mini_catalog, tmp_path):
        """_requests holds one (key, model, params) triple per request key."""
        b = _make(
            mini_catalog,
            tmp_path,
            variables={"gfs": ["temperature_2m"], "icon-global": ["temperature_2m"]},
        )
        assert [key for key, _, _ in b._requests] == ["gfs", "icon-global"]


class TestMode:
    """Tests for the mode={subset,whole} download override."""

    def test_default_is_subset(self, mini_catalog, tmp_path):
        """mode defaults to subset."""
        assert _make(mini_catalog, tmp_path)._mode == "subset"

    def test_whole_accepted(self, mini_catalog, tmp_path):
        """mode='whole' is accepted and stashed."""
        assert _make(mini_catalog, tmp_path, mode="whole")._mode == "whole"

    def test_zarr_rejected_with_reason(self, mini_catalog, tmp_path):
        """mode='zarr' raises ValueError naming the no-zarr_url reason."""
        with pytest.raises(ValueError, match="zarr_url"):
            _make(mini_catalog, tmp_path, mode="zarr")

    def test_unknown_mode_rejected(self, mini_catalog, tmp_path):
        """An unrecognised mode raises ValueError listing the valid options."""
        with pytest.raises(ValueError, match="subset.*whole|whole.*subset"):
            _make(mini_catalog, tmp_path, mode="nope")

    def test_whole_threads_into_fetch_one(self, mini_catalog, tmp_path, fake_pyramids):
        """mode='whole' passes whole=True into every centre.fetch_one call."""
        b = _make(mini_catalog, tmp_path, mode="whole")
        b._centres["herbie"] = _CountingCentre(tmp_path)
        b._fetch(b._search())
        assert b._centres["herbie"].calls, "centre was never called"
        assert all(call[-1] is True for call in b._centres["herbie"].calls)

    def test_subset_threads_false(self, mini_catalog, tmp_path, fake_pyramids):
        """The default mode passes whole=False into every centre.fetch_one call."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        b._fetch(b._search())
        assert all(call[-1] is False for call in b._centres["herbie"].calls)


def test_no_xarray_or_cfgrib_imports_in_nwp():
    """No module under src/earthlens/nwp imports the banned xarray / cfgrib."""
    import pathlib

    root = pathlib.Path(backend_mod.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("import xarray", "import cfgrib", "from xarray", "from cfgrib"):
            if marker in text:
                offenders.append((path.name, marker))
    assert offenders == [], f"banned imports found: {offenders}"


class TestHooks:
    """Tests for _initialize, _create_grid, and _check_input_dates."""

    def test_initialize_returns_none(self, mini_catalog, tmp_path):
        """No auth: _initialize returns None and binds no client."""
        b = _make(mini_catalog, tmp_path)
        assert b._initialize() is None and not hasattr(b, "client")

    def test_create_grid(self, mini_catalog, tmp_path):
        """_create_grid wraps the bbox into a SpatialExtent."""
        space = _make(mini_catalog, tmp_path).space
        assert isinstance(space, SpatialExtent)
        assert (space.west, space.south, space.east, space.north) == (30, 10, 40, 20)

    def test_inverted_dates_raise(self, mini_catalog, tmp_path):
        """A start later than end is rejected by the TemporalExtent."""
        with pytest.raises(ValueError, match="inverted"):
            _make(mini_catalog, tmp_path, start="2024-06-02", end="2024-06-01")


class TestStepsFor:
    """Tests for the forecast-step resolution."""

    def test_default_is_analysis_step(self, mini_catalog, tmp_path):
        """With no steps/horizon the only step is the analysis (0)."""
        b = _make(mini_catalog, tmp_path)
        assert b._steps_for(b._requests[0][1]) == [0]

    def test_explicit_steps_sorted_unique(self, mini_catalog, tmp_path):
        """An explicit steps= list is de-duplicated and sorted."""
        b = _make(mini_catalog, tmp_path, steps=[12, 0, 12])
        assert b._steps_for(b._requests[0][1]) == [0, 12]

    def test_horizon_expands_hourly_by_default(self, mini_catalog, tmp_path):
        """horizon= expands hourly when the model has the default cadence (1)."""
        b = _make(mini_catalog, tmp_path, horizon=3)
        assert b._steps_for(b._requests[0][1]) == [0, 1, 2, 3]

    def test_horizon_expands_on_step_cadence(self, tmp_path):
        """horizon= steps on the model's step_cadence_h (M2), not hourly."""
        from earthlens.nwp import Catalog, NWPModel

        cat = Catalog(
            datasets={
                "gfs": NWPModel(
                    provider="noaa-nodd",
                    model_family="gfs",
                    cycles_utc=[0],
                    horizon_h=24,
                    step_cadence_h=3,
                    backend="herbie",
                    bands={"temperature_2m": ":TMP:2 m above ground:"},
                )
            }
        )
        b = _make(cat, tmp_path, horizon=12)
        assert b._steps_for(b._requests[0][1]) == [0, 3, 6, 9, 12]

    def test_step_beyond_horizon_raises(self, mini_catalog, tmp_path):
        """A step past the model horizon raises ValueError."""
        b = _make(mini_catalog, tmp_path, steps=[100])
        with pytest.raises(ValueError, match="exceed"):
            b._steps_for(b._requests[0][1])


class TestMembersFor:
    """Tests for the ensemble-member axis resolution."""

    def _ens_catalog(self):
        """A catalog with one ensemble model (members) + one deterministic."""
        from earthlens.nwp import Catalog, NWPModel

        return Catalog(
            datasets={
                "gefs": NWPModel(
                    provider="noaa-nodd",
                    model_family="gefs",
                    cycles_utc=[0],
                    horizon_h=240,
                    backend="herbie",
                    bands={"temperature_2m": ":TMP:2 m above ground:"},
                    members=["mean", "0", "1", "2"],
                ),
            }
        )

    def test_deterministic_model_has_none_axis(self, mini_catalog, tmp_path):
        """A model with no members yields a single [None] member axis."""
        b = _make(mini_catalog, tmp_path)
        assert b._members_for(b._requests[0][1]) == [None]

    def test_default_is_first_member(self, tmp_path):
        """An ensemble request with no members= fetches the first listed member."""
        cat = self._ens_catalog()
        b = _make(cat, tmp_path, variables={"gefs": ["temperature_2m"]})
        assert b._members_for(b._requests[0][1]) == ["mean"]

    def test_explicit_members(self, tmp_path):
        """members= selects the requested members."""
        cat = self._ens_catalog()
        b = _make(
            cat, tmp_path, variables={"gefs": ["temperature_2m"]}, members=["1", "2"]
        )
        assert b._members_for(b._requests[0][1]) == ["1", "2"]

    def test_unknown_member_raises(self, tmp_path):
        """A member not in the model's list is rejected."""
        cat = self._ens_catalog()
        b = _make(cat, tmp_path, variables={"gefs": ["temperature_2m"]}, members=["99"])
        with pytest.raises(ValueError, match="not in the model's members"):
            b._members_for(b._requests[0][1])


class TestSearch:
    """Tests for the cycle-grid walk."""

    def test_expands_cycles_and_steps(self, mini_catalog, tmp_path):
        """_search yields one product per (cycle, step) across the range."""
        b = _make(
            mini_catalog, tmp_path, start="2024-06-01", end="2024-06-02", steps=[0, 6]
        )
        products = b._search()
        assert len(products) == 2 * 2 * 2
        assert products[0].id == "gfs.2024060100.f000"
        assert products[0].metadata["model_key"] == "gfs"
        assert products[0].metadata["step"] == 0

    def test_expands_members(self, tmp_path):
        """_search crosses cycle×step×member for an ensemble model."""
        from earthlens.nwp import Catalog, NWPModel

        cat = Catalog(
            datasets={
                "gefs": NWPModel(
                    provider="noaa-nodd",
                    model_family="gefs",
                    cycles_utc=[0],
                    horizon_h=240,
                    backend="herbie",
                    bands={"temperature_2m": ":TMP:2 m above ground:"},
                    members=["mean", "1", "2"],
                )
            }
        )
        b = _make(
            cat,
            tmp_path,
            variables={"gefs": ["temperature_2m"]},
            steps=[0, 6],
            members=["1", "2"],
        )
        products = b._search()
        assert len(products) == 1 * 2 * 2  # 1 cycle × 2 steps × 2 members
        assert products[0].id == "gefs.2024060100.f000.m1"
        assert products[0].metadata["member"] == "1"

    def test_multi_model_search(self, mini_catalog, tmp_path):
        """Each requested model contributes its own cycle×step products."""
        b = _make(
            mini_catalog,
            tmp_path,
            variables={"gfs": ["temperature_2m"], "icon-global": ["temperature_2m"]},
        )
        keys = {p.metadata["model_key"] for p in b._search()}
        assert keys == {"gfs", "icon-global"}


class TestCentreFor:
    """Tests for the per-backend centre cache."""

    def test_caches_one_instance_per_backend(self, mini_catalog, tmp_path, monkeypatch):
        """_centre_for builds the centre once and reuses it."""
        built = []

        def fake_resolve(backend, save_dir):
            centre = _CountingCentre(save_dir)
            built.append(backend)
            return centre

        monkeypatch.setattr(backend_mod, "resolve_centre", fake_resolve)
        b = _make(mini_catalog, tmp_path)
        first = b._centre_for("herbie")
        assert b._centre_for("herbie") is first
        assert built == ["herbie"]

    def test_threads_show_progress_onto_centre(
        self, mini_catalog, tmp_path, fake_pyramids
    ):
        """download(progress_bar=False) sets show_progress on the centre (L4)."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        b.download(progress_bar=False)
        assert b._centres["herbie"].show_progress is False


class TestFetch:
    """Tests for the GRIB2 -> cropped COG pipeline."""

    def test_fetch_writes_one_cog_per_product(
        self, mini_catalog, tmp_path, fake_pyramids
    ):
        """_fetch opens, crops, and writes one COG per (cycle, step)."""
        b = _make(mini_catalog, tmp_path, steps=[0, 6])
        b._centres["herbie"] = _CountingCentre(tmp_path)
        paths = b._fetch(b._search())
        assert len(paths) == 4
        assert len(fake_pyramids["written"]) == 4
        assert paths[0].name == "gfs_2024060100_f000.tif"
        assert fake_pyramids["opened"][0].cropped == ((30.0, 10.0, 40.0, 20.0), 4326)
        # touch=False crops to the bbox extent (touch=True masks the full grid).
        assert fake_pyramids["opened"][0].touch is False

    def test_download_passthrough(self, mini_catalog, tmp_path, fake_pyramids):
        """download() without aggregate returns the per-product COGs."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        paths = b.download(progress_bar=False)
        assert [p.name for p in paths] == [
            "gfs_2024060100_f000.tif",
            "gfs_2024060112_f000.tif",
        ]

    def test_api_composes_search_fetch(self, mini_catalog, tmp_path, fake_pyramids):
        """_api() composes _search + _fetch into the COG list."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        assert [p.name for p in b._api()] == [
            "gfs_2024060100_f000.tif",
            "gfs_2024060112_f000.tif",
        ]


class TestFetchErrors:
    """Tests for the per-product `errors` policy (M1)."""

    def test_warn_returns_partial(self, mini_catalog, tmp_path, fake_pyramids):
        """errors='warn' skips the failed step and returns the rest."""
        b = _make(mini_catalog, tmp_path, steps=[0, 6])
        b._centres["herbie"] = _FlakyCentre(tmp_path, fail_step=6)
        paths = b.download(progress_bar=False, errors="warn")
        assert [p.name for p in paths] == [
            "gfs_2024060100_f000.tif",
            "gfs_2024060112_f000.tif",
        ]

    def test_skip_returns_partial(self, mini_catalog, tmp_path, fake_pyramids):
        """errors='skip' also drops the failed step (silently)."""
        b = _make(mini_catalog, tmp_path, steps=[0, 6])
        b._centres["herbie"] = _FlakyCentre(tmp_path, fail_step=6)
        assert len(b.download(progress_bar=False, errors="skip")) == 2

    def test_raise_propagates(self, mini_catalog, tmp_path, fake_pyramids):
        """errors='raise' aborts the whole download on the first miss."""
        b = _make(mini_catalog, tmp_path, steps=[0, 6])
        b._centres["herbie"] = _FlakyCentre(tmp_path, fail_step=6)
        with pytest.raises(RuntimeError, match="not published"):
            b.download(progress_bar=False, errors="raise")

    def test_invalid_errors_rejected(self, mini_catalog, tmp_path):
        """An unknown errors policy is rejected up front."""
        b = _make(mini_catalog, tmp_path)
        with pytest.raises(ValueError, match="errors must be"):
            b.download(errors="explode")


class TestNormaliseLongitude:
    """Tests for the 0–360 longitude handling."""

    def test_positive_bbox_is_noop(self, mini_catalog, tmp_path):
        """An eastern-hemisphere bbox leaves the dataset untouched."""
        b = _make(mini_catalog, tmp_path, lon_lim=[30, 40])
        ds = _LonDS(global_360=True)
        assert b._normalise_longitude(ds) is ds and ds.converted is False

    def test_negative_bbox_converts_global_grid(self, mini_catalog, tmp_path):
        """A bbox reaching negative longitudes shifts a 0–360 global grid."""
        b = _make(mini_catalog, tmp_path, lon_lim=[-100, -50])
        ds = _LonDS(global_360=True)
        assert b._normalise_longitude(ds).converted is True

    def test_negative_bbox_regional_grid_swallows_error(self, mini_catalog, tmp_path):
        """A non-global grid that cannot be converted is returned as-is."""
        b = _make(mini_catalog, tmp_path, lon_lim=[-100, -50])
        ds = _LonDS(global_360=False)
        assert b._normalise_longitude(ds) is ds


def _real_dataset(*, global_360: bool):
    """Build a real pyramids EPSG:4326 Dataset, whole-globe 0–360 or a small window."""
    import numpy as np
    from pyramids.dataset import Dataset, GeoReference

    if global_360:
        arr = np.arange(32, dtype="float32").reshape(1, 4, 8)
        geo = (0.0, 45.0, 0.0, 90.0, 0.0, -45.0)
    else:
        arr = np.arange(100, dtype="float32").reshape(1, 10, 10)
        geo = (10.0, 1.0, 0.0, 20.0, 0.0, -1.0)
    return Dataset.from_array(arr=arr, geo_ref=GeoReference(geo=geo, epsg=4326))


class TestNormaliseLongitudeRealPyramids:
    """Real-pyramids coverage of `_normalise_longitude` so the fakes are not the sole guard."""

    def test_negative_bbox_shifts_real_global_grid(self, mini_catalog, tmp_path):
        """A real 0–360 global grid is shifted to start at −180 when the bbox goes negative."""
        b = _make(mini_catalog, tmp_path, lon_lim=[-100, -50])
        out = b._normalise_longitude(_real_dataset(global_360=True))
        assert out.geotransform[0] == -180.0

    def test_positive_bbox_real_global_is_noop(self, mini_catalog, tmp_path):
        """An eastern-hemisphere bbox returns the same real Dataset untouched."""
        b = _make(mini_catalog, tmp_path, lon_lim=[30, 40])
        ds = _real_dataset(global_360=True)
        out = b._normalise_longitude(ds)
        assert out is ds and out.geotransform[0] == 0.0

    def test_negative_bbox_real_regional_swallows_valueerror(
        self, mini_catalog, tmp_path
    ):
        """A real non-global grid raises inside pyramids and is returned as-is."""
        b = _make(mini_catalog, tmp_path, lon_lim=[-100, -50])
        ds = _real_dataset(global_360=False)
        assert b._normalise_longitude(ds) is ds


class TestAggregate:
    """Tests for the (cycle, step) COG-stack reducer."""

    def _config(self, **kwargs):
        """Build an AggregationConfig with test-friendly defaults."""
        from earthlens.aggregate import AggregationConfig

        params = dict(freq="1D", op="auto")
        params.update(kwargs)
        return AggregationConfig(**params)

    def test_empty_paths_returns_empty(self, mini_catalog, tmp_path):
        """Aggregating an empty stack is a no-op."""
        assert _make(mini_catalog, tmp_path)._aggregate([], self._config()) == []

    def test_reduces_stack_to_per_window_cogs(
        self, mini_catalog, tmp_path, fake_aggregate
    ):
        """Two cycles in one daily window collapse to a single window COG."""
        b = _make(mini_catalog, tmp_path)
        paths = [
            tmp_path / "gfs_2024060100_f000.tif",
            tmp_path / "gfs_2024060112_f000.tif",
        ]
        out = b._aggregate(paths, self._config(freq="1D", op="mean"))
        assert len(out) == 1
        assert out[0].name == "gfs_mean_1D_2024060100.tif"
        assert len(fake_aggregate["written"]) == 1

    def test_accumulated_field_warns(
        self, mini_catalog, tmp_path, fake_aggregate, monkeypatch
    ):
        """Aggregating an accumulated (*_acc) band logs a warning (M3)."""
        warnings = []
        monkeypatch.setattr(
            backend_mod.logger, "warning", lambda msg: warnings.append(msg)
        )
        b = _make(
            mini_catalog, tmp_path, variables={"icon-global": ["precipitation_acc"]}
        )
        b._aggregate([tmp_path / "icon-global_2024060100_f000.tif"], self._config())
        assert any("accumulated" in m for m in warnings), warnings

    def test_instantaneous_field_no_warn(
        self, mini_catalog, tmp_path, fake_aggregate, monkeypatch
    ):
        """A non-accumulated band aggregates without the accumulation warning."""
        warnings = []
        monkeypatch.setattr(
            backend_mod.logger, "warning", lambda msg: warnings.append(msg)
        )
        b = _make(mini_catalog, tmp_path, variables={"gfs": ["temperature_2m"]})
        b._aggregate([tmp_path / "gfs_2024060100_f000.tif"], self._config())
        assert not any("accumulated" in m for m in warnings), warnings

    def test_multi_model_request_rejected(self, mini_catalog, tmp_path, fake_aggregate):
        """Aggregation across models with different grids is rejected."""
        b = _make(
            mini_catalog,
            tmp_path,
            variables={"gfs": ["temperature_2m"], "icon-global": ["temperature_2m"]},
        )
        with pytest.raises(ValueError, match="single model"):
            b._aggregate([tmp_path / "gfs_2024060100_f000.tif"], self._config())

    def test_icosahedral_model_rejected(self, tmp_path):
        """An icosahedral-grid model (declared by `grid_kind`) refuses aggregation."""
        from earthlens.nwp import NWP, Catalog, NWPModel

        cat = Catalog(
            datasets={
                "icon-icos": NWPModel(
                    provider="dwd-opendata",
                    backend="direct-https",
                    cycles_utc=[0, 12],
                    horizon_h=48,
                    idx=False,
                    mirrors=["origin"],
                    url_template="https://example.test/{var}.grib2",
                    bands={"temperature_2m": "T_2M"},
                    grid_kind="icosahedral",
                ),
            }
        )
        b = NWP(
            start="2024-06-01",
            end="2024-06-01",
            variables={"icon-icos": ["temperature_2m"]},
            lat_lim=[40, 45],
            lon_lim=[-80, -75],
            path=str(tmp_path),
            catalog=cat,
        )
        with pytest.raises(NotImplementedError, match="icosahedral"):
            b._aggregate([tmp_path / "icon-icos_2024060100_f000.tif"], self._config())
        # The error must not name `icon-d2` as an alternative — `icon-d2`'s
        # own catalog row carries `grid_kind: icosahedral` and would re-fail.
        with pytest.raises(NotImplementedError) as excinfo:
            b._aggregate([tmp_path / "icon-icos_2024060100_f000.tif"], self._config())
        assert "ICON-D2" not in str(excinfo.value)

    def test_url_template_substring_alone_no_longer_rejects(self, tmp_path):
        """A regular-grid row whose URL happens to contain the word `icosahedral`
        is no longer rejected — the guard uses `grid_kind`, not URL substring."""
        from earthlens.nwp import NWP, Catalog, NWPModel

        cat = Catalog(
            datasets={
                "ok-grid": NWPModel(
                    provider="dwd-opendata",
                    backend="direct-https",
                    cycles_utc=[0, 12],
                    horizon_h=48,
                    idx=False,
                    mirrors=["origin"],
                    # URL string contains the word, but the row declares
                    # itself regular — the guard trusts the declaration.
                    url_template="https://example.test/icosahedral_paper_{var}.grib2",
                    bands={"temperature_2m": "T_2M"},
                ),
            }
        )
        b = NWP(
            start="2024-06-01",
            end="2024-06-01",
            variables={"ok-grid": ["temperature_2m"]},
            lat_lim=[40, 45],
            lon_lim=[-80, -75],
            path=str(tmp_path),
            catalog=cat,
        )
        # No NotImplementedError; the guard passes through to pyramids
        # (which we intercept with the fake_aggregate fixture in the other
        # tests). Here we exercise the *check*, not the reducer body, so a
        # bare _aggregate call with an empty path list returns [].
        assert b._aggregate([], self._config()) == []

    def test_bundled_icosahedral_rows_have_grid_kind(self):
        """Every DWD ICON icosahedral row in the bundled catalog carries the flag."""
        from earthlens.nwp import Catalog

        cat = Catalog()
        expected = {"icon-global", "icon-d2", "icon-eps", "icon-eu-eps", "icon-d2-eps"}
        for key in expected:
            assert cat.datasets[key].grid_kind == "icosahedral", key
        # icon-eu (regular lat-lon, croppable) and gfs default to regular.
        assert cat.datasets["icon-eu"].grid_kind == "regular-latlon"
        assert cat.datasets["gfs"].grid_kind == "regular-latlon"

    def test_download_with_aggregate(
        self, mini_catalog, tmp_path, fake_pyramids, fake_aggregate
    ):
        """download(aggregate=...) fetches then reduces the stack end to end."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        out = b.download(
            progress_bar=False, aggregate=self._config(freq="1D", op="mean")
        )
        assert len(out) == 1 and out[0].name.startswith("gfs_mean_1D_")


def test_unknown_backend_raises(tmp_path):
    """A catalog model with an unrecognised backend is rejected at construction."""
    from earthlens.nwp import Catalog
    from earthlens.nwp.catalog import NWPModel

    # backend is a pydantic Literal, so a normal NWPModel cannot hold an unknown
    # value — model_construct bypasses validation to exercise the runtime guard.
    bad = NWPModel.model_construct(
        provider="x",
        backend="bogus",
        cycles_utc=[0],
        horizon_h=0,
        bands={"temperature_2m": ":TMP:2 m above ground:"},
    )
    cat = Catalog(datasets={"bad": bad})
    with pytest.raises(ValueError, match="unknown backend"):
        NWP(
            start="2024-06-01",
            end="2024-06-01",
            variables={"bad": ["temperature_2m"]},
            lat_lim=[40, 45],
            lon_lim=[-80, -75],
            path=str(tmp_path),
            catalog=cat,
        )


class TestRetentionWarning:
    """C3 — RetentionWarning fires when the request precedes a model's window."""

    @staticmethod
    def _short_window_catalog():
        """Two-model catalog: one with `retention_days=1`, one archival."""
        from earthlens.nwp import Catalog, NWPModel

        return Catalog(
            datasets={
                "icon": NWPModel(
                    provider="dwd-opendata",
                    backend="direct-https",
                    cycles_utc=[0, 12],
                    horizon_h=48,
                    idx=False,
                    mirrors=["origin"],
                    url_template="https://example.test/{cycle:%H}/{var}.bz2",
                    bands={"t2m": "T_2M"},
                    retention_days=1,
                ),
                "gfs": NWPModel(
                    provider="noaa-nodd",
                    backend="herbie",
                    cycles_utc=[0, 12],
                    horizon_h=48,
                    mirrors=["aws"],
                    bands={"t2m": ":TMP:2 m above ground:"},
                ),
            }
        )

    def _build(self, catalog, model_key, tmp_path, start):
        from earthlens.nwp import NWP

        return NWP(
            start=start,
            end=start,
            variables={model_key: ["t2m"]},
            lat_lim=[40, 45],
            lon_lim=[-80, -75],
            path=str(tmp_path),
            catalog=catalog,
        )

    def test_out_of_window_warns(self, tmp_path):
        """A start older than `now - retention_days` warns with the cutoff date."""
        from earthlens.nwp import RetentionWarning

        cat = self._short_window_catalog()
        old = (
            dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=30)
        ).strftime("%Y-%m-%d")
        with pytest.warns(RetentionWarning, match="retains"):
            self._build(cat, "icon", tmp_path, old)

    def test_in_window_is_silent(self, tmp_path, recwarn):
        """An in-window start (today) does not emit RetentionWarning."""
        from earthlens.nwp import RetentionWarning

        cat = self._short_window_catalog()
        today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        self._build(cat, "icon", tmp_path, today)
        assert not [w for w in recwarn if issubclass(w.category, RetentionWarning)]

    def test_archival_model_is_silent(self, tmp_path, recwarn):
        """A `retention_days=None` row never warns, even for an ancient start."""
        from earthlens.nwp import RetentionWarning

        cat = self._short_window_catalog()
        self._build(cat, "gfs", tmp_path, "2000-01-01")
        assert not [w for w in recwarn if issubclass(w.category, RetentionWarning)]

    def test_retention_warning_is_importable_from_package(self):
        """`RetentionWarning` is on the public `earthlens.nwp` surface."""
        from earthlens.nwp import RetentionWarning

        assert issubclass(RetentionWarning, UserWarning)
