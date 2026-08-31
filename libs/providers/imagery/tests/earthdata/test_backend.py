"""Unit + integration tests for the EarthData backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.earthdata import EarthData

pytestmark = [pytest.mark.earthdata, pytest.mark.integration]


def _make(tmp_path, variables, **kwargs):
    """Construct an EarthData instance over a tmp output dir."""
    return EarthData(
        start="2020-06-01",
        end="2020-06-02",
        variables=variables,
        lat_lim=[10.0, 20.0],
        lon_lim=[30.0, 40.0],
        path=tmp_path,
        **kwargs,
    )


class _FakeReduced:
    """Reduced NetCDF stand-in that records the files it writes."""

    written: list[str] = []

    def to_file(self, target):
        """Write a placeholder file and record its path."""
        Path(target).write_text("x")
        _FakeReduced.written.append(str(target))


class _FakeNetCDFHandle:
    """NetCDF handle stand-in (with a time axis) whose reduce() returns a _FakeReduced."""

    dimension_names = ("time", "lat", "lon")

    def reduce(self, dim, how="mean"):
        """Return a fake reduced handle."""
        return _FakeReduced()


class _NoTimeNetCDFHandle:
    """NetCDF handle stand-in lacking a time dimension."""

    dimension_names = ("lat", "lon")

    def reduce(self, dim, how="mean"):
        """Should never be called when there is no time axis."""
        raise AssertionError("reduce() called on a granule with no time axis")


class _FakeNetCDF:
    """Fake pyramids NetCDF with a reduce attribute and read_file()."""

    reduce = True

    @staticmethod
    def read_file(path):
        """Return a fake handle (with a time axis) ignoring the path."""
        return _FakeNetCDFHandle()


class _NoTimeNetCDF:
    """Fake pyramids NetCDF whose granule has no time dimension."""

    reduce = True

    @staticmethod
    def read_file(path):
        """Return a fake handle without a time axis."""
        return _NoTimeNetCDFHandle()


class _NetCDFNoReduce:
    """Fake pyramids NetCDF lacking the reduce method."""


class _FakeGrouped:
    """Grouped COG collection stand-in whose to_file returns window paths."""

    def __init__(self, root):
        self._root = root

    def to_file(self, out_dir):
        """Return one fabricated per-window raster path."""
        return [str(Path(out_dir) / "window_2020.tif")]


class _FakeCollection:
    """Fake DatasetCollection whose groupby returns a _FakeGrouped."""

    def __init__(self, paths):
        self._paths = paths

    def groupby(self, freq):
        """Return a fake grouped collection."""
        return _FakeGrouped(self._paths)


class _FakeDatasetCollection:
    """Fake pyramids DatasetCollection with from_files + groupby."""

    groupby = True

    @staticmethod
    def from_files(paths):
        """Return a fake collection over the given paths."""
        return _FakeCollection(paths)


class _DatasetCollectionNoGroupby:
    """Fake pyramids DatasetCollection lacking the groupby method."""


class TestConstruction:
    """Catalog resolution + per-instance OUTPUT_KIND."""

    def test_raster_output_kind(self, fake_earthaccess, edl_env, tmp_path):
        """A raster dataset sets OUTPUT_KIND='raster'."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        assert obj.OUTPUT_KIND == "raster"

    def test_vector_output_kind(self, fake_earthaccess, edl_env, tmp_path):
        """A vector dataset sets OUTPUT_KIND='vector'."""
        obj = _make(tmp_path, {"ATL08_006": ["h_canopy"]})
        assert obj.OUTPUT_KIND == "vector"

    def test_mixed_kind_rejected(self, fake_earthaccess, edl_env, tmp_path):
        """Mixing raster and vector datasets is rejected."""
        with pytest.raises(ValueError, match="mixed kinds"):
            _make(
                tmp_path,
                {"GPM_3IMERGHHL_07": ["precipitation"], "ATL08_006": ["h_canopy"]},
            )

    def test_empty_variables_rejected(self, fake_earthaccess, edl_env, tmp_path):
        """An empty variables mapping is rejected."""
        with pytest.raises(ValueError, match="non-empty"):
            _make(tmp_path, {})

    def test_daac_with_multiple_datasets_rejected(
        self, fake_earthaccess, edl_env, tmp_path
    ):
        """daac= combined with several datasets is rejected up front."""
        with pytest.raises(ValueError, match="daac= only applies to a single-dataset"):
            _make(
                tmp_path,
                {
                    "GPM_3IMERGHHL_07": ["precipitation"],
                    "GPM_3IMERGM_07": ["precipitation"],
                },
                daac="GES_DISC",
            )

    def test_daac_with_single_dataset_ok(self, fake_earthaccess, edl_env, tmp_path):
        """daac= with one dataset resolves normally."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]}, daac="GES DISC")
        assert obj._datasets[0].provider == "GES_DISC"

    def test_unknown_key_rejected(self, fake_earthaccess, edl_env, tmp_path):
        """An unknown dataset key is rejected with did-you-mean."""
        with pytest.raises(ValueError, match="Did you mean"):
            _make(tmp_path, {"GPM_3IMERGHHL": ["precipitation"]})

    def test_login_deferred_until_fetch(self, fake_earthaccess, edl_env, tmp_path):
        """Construction does not authenticate; login is deferred to first fetch."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        assert len(fake_earthaccess.login_calls) == 0, "construction must not log in"
        obj._auth.configure()
        assert len(fake_earthaccess.login_calls) == 1, "configure() authenticates once"


class TestSearch:
    """_search request shape."""

    def test_search_request_shape(self, fake_earthaccess, edl_env, tmp_path):
        """search_data is called with short_name/version/provider/bbox/temporal/count."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        obj._search()
        call = fake_earthaccess.search_calls[-1]
        assert call["short_name"] == "GPM_3IMERGHHL"
        assert call["version"] == "07"
        assert call["provider"] == "GES_DISC"
        assert call["bounding_box"] == (30.0, 10.0, 40.0, 20.0)
        assert call["temporal"] == ("2020-06-01T00:00:00", "2020-06-02T23:59:59.999999")
        assert call["count"] == -1

    def test_temporal_spans_full_end_day(self, fake_earthaccess, edl_env, tmp_path):
        """A single-day request covers the whole day, not a midnight instant."""
        obj = EarthData(
            start="2020-06-01",
            end="2020-06-01",
            variables={"GPM_3IMERGHHL_07": ["precipitation"]},
            lat_lim=[10.0, 20.0],
            lon_lim=[30.0, 40.0],
            path=tmp_path,
        )
        obj._search()
        start, end = fake_earthaccess.search_calls[-1]["temporal"]
        assert start == "2020-06-01T00:00:00"
        assert end == "2020-06-01T23:59:59.999999"

    def test_temporal_keeps_an_end_that_names_a_time(
        self, fake_earthaccess, edl_env, tmp_path
    ):
        """An end naming a time of day means that instant, not the whole day."""
        obj = EarthData(
            start="2020-06-01 09:00",
            end="2020-06-01 09:30",
            fmt="%Y-%m-%d %H:%M",
            variables={"GPM_3IMERGHHL_07": ["precipitation"]},
            lat_lim=[10.0, 20.0],
            lon_lim=[30.0, 40.0],
            path=tmp_path,
        )
        obj._search()
        start, end = fake_earthaccess.search_calls[-1]["temporal"]
        assert start == "2020-06-01T09:00:00"
        assert end == "2020-06-01T09:30:00", (
            f"an explicit time must not be widened to end of day, got {end}"
        )

    def test_search_one_product_per_granule(self, fake_earthaccess, edl_env, tmp_path):
        """Each returned granule becomes one RemoteProduct."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        products = obj._search()
        assert len(products) == len(fake_earthaccess.granules)
        assert products[0].metadata["dataset"].short_name == "GPM_3IMERGHHL"


class TestFetch:
    """_fetch open-vs-download branching (G4)."""

    def test_offcloud_auto_uses_download(self, fake_earthaccess, edl_env, tmp_path):
        """Off-cloud + auto downloads over HTTPS."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        paths = obj.download()
        assert fake_earthaccess.download_calls and not fake_earthaccess.open_calls
        assert all(isinstance(p, Path) for p in paths)

    def test_direct_s3_always_uses_open(self, fake_earthaccess, edl_env, tmp_path):
        """direct_s3='always' streams from S3 regardless of region."""
        obj = _make(
            tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]}, direct_s3="always"
        )
        obj.download()
        assert fake_earthaccess.open_calls and not fake_earthaccess.download_calls

    def test_auto_in_region_uses_open(self, fake_earthaccess, edl_env, tmp_path):
        """auto + in-region + cloud-hosted streams from S3."""
        obj = _make(
            tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]}, region="us-west-2"
        )
        obj.download()
        assert fake_earthaccess.open_calls and not fake_earthaccess.download_calls

    def test_never_uses_download_even_in_region(
        self, fake_earthaccess, edl_env, tmp_path
    ):
        """direct_s3='never' downloads even when in-region."""
        obj = _make(
            tmp_path,
            {"GPM_3IMERGHHL_07": ["precipitation"]},
            region="us-west-2",
            direct_s3="never",
        )
        obj.download()
        assert fake_earthaccess.download_calls and not fake_earthaccess.open_calls

    def test_in_region_env_var(self, fake_earthaccess, monkeypatch, edl_env, tmp_path):
        """AWS_REGION supplies the caller region when region= is unset."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        assert obj._in_region("us-west-2") is True

    def test_progress_bar_forwarded_to_download(
        self, fake_earthaccess, edl_env, tmp_path
    ):
        """progress_bar=False is forwarded to earthaccess as show_progress."""
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        obj.download(progress_bar=False)
        assert fake_earthaccess.download_calls[-1]["show_progress"] is False

    def test_progress_bar_forwarded_to_open(self, fake_earthaccess, edl_env, tmp_path):
        """progress_bar is forwarded to earthaccess.open on the S3 path."""
        obj = _make(
            tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]}, direct_s3="always"
        )
        obj.download(progress_bar=False)
        assert fake_earthaccess.open_calls[-1]["show_progress"] is False


class TestAggregate:
    """download(aggregate=) routing — axis-driven, not format-driven (G6)."""

    def test_granule_stack_routes_to_groupby(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A multi-granule fetch (the common case) windows the stack via groupby."""
        import pyramids.dataset as dsmod

        from earthlens.aggregate import AggregationConfig

        monkeypatch.setattr(dsmod, "DatasetCollection", _FakeDatasetCollection)
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        out = obj.download(aggregate=AggregationConfig(freq="1MS"))
        assert [p.name for p in out] == ["window_2020.tif"]

    def test_single_netcdf_reduces_internal_axis(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A single NetCDF cube collapses its internal time axis via NetCDF.reduce."""
        import pyramids.netcdf as ncmod

        from earthlens.aggregate import AggregationConfig

        fake_earthaccess.granules = [{"meta": {"concept-id": "G1"}}]
        _FakeReduced.written = []
        monkeypatch.setattr(ncmod, "NetCDF", _FakeNetCDF)
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        out = obj.download(aggregate=AggregationConfig(freq="1MS"))
        assert [str(p).endswith("_1MS_agg.nc") for p in out] == [True]
        assert len(_FakeReduced.written) == 1

    def test_single_netcdf_without_time_falls_back_to_stack(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A single NetCDF with no time axis falls back to the stack/groupby path."""
        import pyramids.dataset as dsmod
        import pyramids.netcdf as ncmod

        from earthlens.aggregate import AggregationConfig

        fake_earthaccess.granules = [{"meta": {"concept-id": "G1"}}]
        monkeypatch.setattr(ncmod, "NetCDF", _NoTimeNetCDF)
        monkeypatch.setattr(dsmod, "DatasetCollection", _FakeDatasetCollection)
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        out = obj.download(aggregate=AggregationConfig(freq="1MS"))
        assert [p.name for p in out] == ["window_2020.tif"]

    def test_single_cog_uses_stack_path(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A lone COG (no internal axis) falls through to the stack path."""
        import pyramids.dataset as dsmod

        from earthlens.aggregate import AggregationConfig

        monkeypatch.setattr(dsmod, "DatasetCollection", _FakeDatasetCollection)
        obj = _make(tmp_path, {"OPERA_L2_RTC-S1_V1": ["VV"]}, direct_s3="never")
        out = obj._aggregate([Path("only.tif")], AggregationConfig(freq="YS"))
        assert [p.name for p in out] == ["window_2020.tif"]

    def test_missing_reduce_raises(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A single NetCDF without NetCDF.reduce raises NotImplementedError."""
        import pyramids.netcdf as ncmod

        from earthlens.aggregate import AggregationConfig

        fake_earthaccess.granules = [{"meta": {"concept-id": "G1"}}]
        monkeypatch.setattr(ncmod, "NetCDF", _NetCDFNoReduce)
        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        with pytest.raises(NotImplementedError, match="NetCDF.reduce"):
            obj.download(aggregate=AggregationConfig(freq="1MS"))

    def test_missing_groupby_raises(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """A stack without DatasetCollection.groupby raises NotImplementedError."""
        import pyramids.dataset as dsmod

        from earthlens.aggregate import AggregationConfig

        monkeypatch.setattr(dsmod, "DatasetCollection", _DatasetCollectionNoGroupby)
        obj = _make(tmp_path, {"OPERA_L2_RTC-S1_V1": ["VV"]})
        with pytest.raises(NotImplementedError, match="groupby"):
            obj._aggregate([Path("a.tif"), Path("b.tif")], AggregationConfig(freq="YS"))

    def test_search_missing_earthaccess_raises(
        self, fake_earthaccess, edl_env, tmp_path, monkeypatch
    ):
        """_search surfaces a friendly ImportError when earthaccess is gone."""
        from .test_auth import _block_earthaccess

        obj = _make(tmp_path, {"GPM_3IMERGHHL_07": ["precipitation"]})
        monkeypatch.delitem(__import__("sys").modules, "earthaccess", raising=False)
        monkeypatch.setattr("builtins.__import__", _block_earthaccess)
        with pytest.raises(ImportError, match=r"earthlens\[earthdata\]"):
            obj._search()
