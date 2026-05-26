"""Unit tests for the National Water Model backend (no network)."""

from __future__ import annotations

import datetime as dt

import pytest

from earthlens.nwm import NWM
from earthlens.nwm.backend import enumerate_cycles

pytestmark = [pytest.mark.nwm, pytest.mark.unit]


def _make(tmp_path, **kwargs):
    """Build an NWM over short_range/channel_rt with a single-day window."""
    params = dict(
        start="2026-05-25",
        end="2026-05-25",
        variables={"short_range": ["channel_rt"]},
        lat_lim=[25, 50],
        lon_lim=[-125, -66],
        path=str(tmp_path),
        cycles=[0],
        steps=[1],
    )
    params.update(kwargs)
    return NWM(**params)


class TestEnumerateCycles:
    """Tests for the cycle-grid helper."""

    def test_single_day_two_cycles(self):
        """Two run hours on one day yield two ascending datetimes."""
        day = dt.datetime(2026, 1, 1)
        out = enumerate_cycles(day, day, [12, 0])
        assert [c.hour for c in out] == [0, 12]

    def test_start_after_end_raises(self):
        """A reversed window raises ValueError."""
        with pytest.raises(ValueError, match="after end"):
            enumerate_cycles(dt.datetime(2026, 1, 2), dt.datetime(2026, 1, 1), [0])

    def test_bad_hour_raises(self):
        """A run hour outside [0, 23] raises ValueError."""
        with pytest.raises(ValueError, match="outside"):
            enumerate_cycles(dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 1), [24])


class TestInit:
    """Tests for construction and request validation."""

    def test_empty_variables_raises(self, tmp_path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="non-empty"):
            _make(tmp_path, variables={})

    def test_unknown_config_raises(self, tmp_path):
        """An unknown configuration key raises (did-you-mean)."""
        with pytest.raises(ValueError, match="NWM catalog"):
            _make(tmp_path, variables={"nope": ["channel_rt"]})

    def test_unknown_product_raises(self, tmp_path):
        """A product not in the configuration is rejected."""
        with pytest.raises(ValueError, match="not in configuration"):
            _make(tmp_path, variables={"short_range": ["streamflow"]})

    def test_empty_product_list_selects_all(self, tmp_path):
        """An empty product list expands to all of the config's products."""
        nwm = _make(tmp_path, variables={"short_range": []})
        _, _, prods = nwm._requests[0]
        assert prods == ["channel_rt", "land", "reservoir", "terrain_rt"]

    def test_output_kind_is_tabular(self, tmp_path):
        """The backend declares a tabular output kind."""
        assert _make(tmp_path).OUTPUT_KIND == "tabular"


class TestStepsAndCycles:
    """Tests for step / cycle resolution."""

    def test_explicit_steps_win(self, tmp_path):
        """An explicit steps= list is sorted and used verbatim."""
        nwm = _make(tmp_path, steps=[3, 1, 2])
        cfg = nwm._catalog.get_config("short_range")
        assert nwm._steps_for(cfg) == [1, 2, 3]

    def test_horizon_expands_from_first_step(self, tmp_path):
        """horizon= expands from first_step on the config cadence."""
        nwm = _make(tmp_path, steps=None, horizon=4)
        cfg = nwm._catalog.get_config("short_range")
        assert nwm._steps_for(cfg) == [1, 2, 3, 4]

    def test_default_is_first_step(self, tmp_path):
        """With neither steps nor horizon, only the first step is fetched."""
        nwm = _make(tmp_path, steps=None, horizon=None)
        cfg = nwm._catalog.get_config("short_range")
        assert nwm._steps_for(cfg) == [1]

    def test_step_beyond_horizon_raises(self, tmp_path):
        """A step past the horizon raises ValueError."""
        nwm = _make(tmp_path, steps=[999])
        cfg = nwm._catalog.get_config("short_range")
        with pytest.raises(ValueError, match="exceed"):
            nwm._steps_for(cfg)

    def test_cycles_default_to_all(self, tmp_path):
        """cycles=None resolves to every cycle the config runs."""
        nwm = _make(tmp_path, cycles=None)
        cfg = nwm._catalog.get_config("short_range")
        assert nwm._cycles_for(cfg) == list(range(24))

    def test_unknown_cycle_raises(self, tmp_path):
        """A cycle hour the config never runs raises ValueError."""
        nwm = _make(
            tmp_path, variables={"medium_range_mem1": ["channel_rt_1"]}, cycles=[3]
        )
        cfg = nwm._catalog.get_config("medium_range_mem1")
        with pytest.raises(ValueError, match="not run"):
            nwm._cycles_for(cfg)

    def test_explicit_cycle_subset_sorted_unique(self, tmp_path):
        """An explicit cycles= subset is validated, de-duplicated, and sorted."""
        nwm = _make(tmp_path, cycles=[12, 0, 12])
        cfg = nwm._catalog.get_config("short_range")
        assert nwm._cycles_for(cfg) == [0, 12]


class TestApi:
    """Tests for the `_api` search/fetch composition."""

    def test_api_returns_fetched_paths(self, tmp_path, fake_s3):
        """_api composes search + fetch and returns the written paths."""
        nwm = _make(tmp_path)
        paths = nwm._api()
        assert len(paths) == 1
        assert paths[0].name.endswith("channel_rt.f001.conus.nc")


class TestSearch:
    """Tests for the product-expansion search step."""

    def test_search_crosses_cycle_step_product(self, tmp_path):
        """_search yields one product per (cycle, step, product) with the S3 key."""
        nwm = _make(tmp_path, variables={"short_range": ["channel_rt", "land"]})
        products = nwm._search()
        assert len(products) == 2
        hrefs = sorted(p.href for p in products)
        assert hrefs[0].endswith("channel_rt.f001.conus.nc")
        assert hrefs[1].endswith("land.f001.conus.nc")

    def test_search_metadata_carries_axis(self, tmp_path):
        """Each product carries config / cycle / step / product / domain."""
        product = _make(tmp_path)._search()[0]
        assert product.metadata["config_key"] == "short_range"
        assert product.metadata["step"] == 1
        assert product.metadata["domain"] == "conus"


class TestFetchAndDownload:
    """Tests for fetching and the inventory result."""

    def test_download_fetches_and_inventories(self, tmp_path, fake_s3):
        """download writes the NetCDF and returns a one-row inventory."""
        nwm = _make(tmp_path)
        df = nwm.download(progress_bar=False)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["config"] == "short_range"
        assert row["product"] == "channel_rt"
        assert row["valid_time"] == dt.datetime(2026, 5, 25, 1)
        assert (tmp_path / "nwm.t00z.short_range.channel_rt.f001.conus.nc").exists()

    def test_download_multi_product_columns(self, tmp_path, fake_s3):
        """A two-product request yields two inventory rows with the right columns."""
        nwm = _make(tmp_path, variables={"short_range": ["channel_rt", "land"]})
        df = nwm.download(progress_bar=False)
        assert sorted(df["product"]) == ["channel_rt", "land"]
        assert list(df.columns) == [
            "config",
            "cycle",
            "step",
            "valid_time",
            "product",
            "domain",
            "path",
        ]

    def test_missing_object_is_skipped(self, tmp_path, fake_s3):
        """A (cycle, step) absent from the bucket is skipped, others kept."""
        nwm = _make(tmp_path, steps=[1, 5])  # f005 is not in the fake tree
        df = nwm.download(progress_bar=False)
        assert list(df["step"]) == [1]

    def test_all_missing_returns_empty_with_columns(self, tmp_path, fake_s3):
        """When nothing is available the inventory is empty but well-formed."""
        nwm = _make(tmp_path, steps=[7])
        df = nwm.download(progress_bar=False)
        assert df.empty
        assert "valid_time" in df.columns

    def test_no_partial_file_left_on_failure(self, tmp_path, fake_s3):
        """A failed fetch leaves no .part file behind."""
        nwm = _make(tmp_path, steps=[9])
        nwm.download(progress_bar=False)
        assert not list(tmp_path.glob("*.part"))

    def test_aggregate_rejected(self, tmp_path):
        """A non-None aggregate raises NotImplementedError."""
        nwm = _make(tmp_path)
        with pytest.raises(NotImplementedError, match="not supported"):
            nwm.download(aggregate=object())


class TestS3ClientImport:
    """Tests for the lazy boto3 import guard."""

    def test_missing_boto3_friendly_error(self, tmp_path, monkeypatch):
        """A missing boto3 raises an ImportError naming earthlens[nwm]."""
        import builtins

        from earthlens.nwm import backend

        real_import = builtins.__import__

        def _no_boto3(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("no boto3")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_boto3)
        with pytest.raises(ImportError, match=r"earthlens\[nwm\]"):
            backend._s3_client("us-east-1")
