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
    """Minimal Dataset stand-in tracking convert_longitude behaviour."""

    def __init__(self, global_360: bool):
        self.global_360 = global_360
        self.converted = False

    def convert_longitude(self):
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

    def fetch_one(self, model, cycle, step, params, mirror):
        """Record the call and return a fabricated GRIB path."""
        self.calls.append((cycle, step, tuple(params), mirror))
        return str(self.save_dir / f"{model.model_family}_{cycle:%Y%m%d%H}_f{step:03d}.grib2")


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

    def test_horizon_expands_hourly(self, mini_catalog, tmp_path):
        """horizon= expands to every integer hour up to the horizon."""
        b = _make(mini_catalog, tmp_path, horizon=3)
        assert b._steps_for(b._requests[0][1]) == [0, 1, 2, 3]

    def test_step_beyond_horizon_raises(self, mini_catalog, tmp_path):
        """A step past the model horizon raises ValueError."""
        b = _make(mini_catalog, tmp_path, steps=[100])
        with pytest.raises(ValueError, match="exceed"):
            b._steps_for(b._requests[0][1])


class TestSearch:
    """Tests for the cycle-grid walk."""

    def test_expands_cycles_and_steps(self, mini_catalog, tmp_path):
        """_search yields one product per (cycle, step) across the range."""
        b = _make(mini_catalog, tmp_path, start="2024-06-01", end="2024-06-02", steps=[0, 6])
        products = b._search()
        assert len(products) == 2 * 2 * 2
        assert products[0].id == "gfs.2024060100.f000"
        assert products[0].metadata["model_key"] == "gfs"
        assert products[0].metadata["step"] == 0

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


class TestFetch:
    """Tests for the GRIB2 -> cropped COG pipeline."""

    def test_fetch_writes_one_cog_per_product(self, mini_catalog, tmp_path, fake_pyramids):
        """_fetch opens, crops, and writes one COG per (cycle, step)."""
        b = _make(mini_catalog, tmp_path, steps=[0, 6])
        b._centres["herbie"] = _CountingCentre(tmp_path)
        paths = b._fetch(b._search())
        assert len(paths) == 4
        assert len(fake_pyramids["written"]) == 4
        assert paths[0].name == "gfs_2024060100_f000.tif"
        assert fake_pyramids["opened"][0].cropped == ((30.0, 10.0, 40.0, 20.0), 4326)

    def test_download_passthrough(self, mini_catalog, tmp_path, fake_pyramids):
        """download() without aggregate returns the per-product COGs."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        paths = b.download(progress_bar=False)
        assert [p.name for p in paths] == ["gfs_2024060100_f000.tif", "gfs_2024060112_f000.tif"]


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


class TestAggregateStub:
    """Tests for the aggregate stub (implemented in C6)."""

    def test_aggregate_not_yet_implemented(self, mini_catalog, tmp_path, fake_pyramids):
        """download(aggregate=...) raises until C6 lands the reducer."""
        b = _make(mini_catalog, tmp_path)
        b._centres["herbie"] = _CountingCentre(tmp_path)
        with pytest.raises(NotImplementedError, match="C6"):
            b.download(aggregate=object())
