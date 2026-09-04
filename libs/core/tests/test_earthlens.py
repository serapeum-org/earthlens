from __future__ import annotations

import glob
import os
import pathlib
import shutil
from collections.abc import Mapping
from typing import List
from unittest.mock import MagicMock

import cdsapi
import numpy as np
import pandas as pd
import pytest

from earthlens._backends import AmbiguousDataSourceError, discover_backends
from earthlens.aggregate import AggregationConfig
from earthlens.chc import CHIRPS
from earthlens.earthlens import EarthLens, _LazyRegistry, _source_dirname
from earthlens.ecmwf import ECMWF
from earthlens.s3 import S3


class _SentinelClient:
    """Stand-in for :class:`cdsapi.Client` used in facade tests."""


@pytest.mark.chc
class TestChirpsBackend:
    @pytest.fixture(scope="module")
    def test_chirps_data_source_instantiate_object(
        self,
        chirps_data_source: str,
        dates: list,
        daily_temporal_resolution: str,
        chirps_variables: list[str],
        lat_bounds: list,
        lon_bounds: list,
        chirps_data_source_output_dir: str,
    ):
        earthlens = EarthLens(
            data_source=chirps_data_source,
            start=dates[0],
            end=dates[1],
            variables=chirps_variables,
            lat_lim=lat_bounds,
            lon_lim=lon_bounds,
            temporal_resolution=daily_temporal_resolution,
            path=chirps_data_source_output_dir,
        )
        assert isinstance(earthlens.DataSources, Mapping)
        assert isinstance(earthlens.datasource, CHIRPS)
        # Legacy list-shape `variables` is normalized to the catalog
        # dict shape (mirroring ECMWF). The dataset key is derived
        # from `temporal_resolution`: "daily" → "global-daily".
        assert earthlens.datasource.vars == {"global-daily": chirps_variables}
        return earthlens

    @pytest.mark.e2e
    def test_download_chirps_backend(
        self,
        test_chirps_data_source_instantiate_object: CHIRPS,
        chirps_data_source_output_dir: str,
        number_downloaded_files: int,
    ):
        test_chirps_data_source_instantiate_object.download()
        # Filename scheme is `<dataset-key>_<variable>_<date>.tif`.
        filelist = glob.glob(
            os.path.join(
                f"{chirps_data_source_output_dir}",
                "global-daily_precipitation_*.tif",
            )
        )
        assert len(filelist) == number_downloaded_files
        # delete the files
        try:
            shutil.rmtree(f"{chirps_data_source_output_dir}")
        except PermissionError:
            print("the downloaded files could not be deleted")


@pytest.mark.s3
class TestS3Backend:
    @pytest.fixture(scope="module")
    def test_s3_data_source_instantiate_object(
        self,
        s3_data_source: str,
        monthly_dates: list,
        monthly_temporal_resolution: str,
        s3_era5_variables: list[str],
        lat_bounds: list,
        lon_bounds: list,
        s3_era5_data_source_output_dir: str,
    ):
        earthlens = EarthLens(
            data_source=s3_data_source,
            start=monthly_dates[0],
            end=monthly_dates[1],
            variables=s3_era5_variables,
            lat_lim=lat_bounds,
            lon_lim=lon_bounds,
            temporal_resolution=monthly_temporal_resolution,
            path=s3_era5_data_source_output_dir,
        )
        assert isinstance(earthlens.DataSources, Mapping)
        assert isinstance(earthlens.datasource, S3)
        assert earthlens.datasource.vars == s3_era5_variables
        return earthlens

    @pytest.mark.e2e
    def test_download_s3_backend(
        self,
        test_s3_data_source_instantiate_object: S3,
        s3_era5_data_source_output_dir: str,
        number_downloaded_files: int,
    ):
        test_s3_data_source_instantiate_object.download()
        # ERA5 (`format: netcdf` in the s3 catalog) is rewritten to a WGS84
        # GeoTIFF on the way out — see s3/backend.py's `_localise`.
        filelist = glob.glob(os.path.join(f"{s3_era5_data_source_output_dir}", "*.tif"))
        assert len(filelist) == number_downloaded_files
        # delete the files
        try:
            shutil.rmtree(f"{s3_era5_data_source_output_dir}")
        except PermissionError:
            print("the downloaded files could not be deleted")


@pytest.mark.unit
class TestCheckSourceResolution:
    """`EarthLens._check_source` resolves keys and guards reserved topics (C1)."""

    @staticmethod
    def _synthetic_registry(*keys):
        """A `_LazyRegistry` over `keys` with throwaway specs (never resolved)."""
        return _LazyRegistry({key: ("m", "C", "", {}) for key in keys})

    def test_qualified_topic_key_resolves(self, monkeypatch):
        """A registered `source:topic` key passes validation."""
        monkeypatch.setattr(
            EarthLens, "DataSources", self._synthetic_registry("dem", "dem:elevation")
        )
        EarthLens._check_source("dem:elevation")

    def test_bare_reserved_word_raises_listing_claimants(self, monkeypatch):
        """A bare reserved word raises, naming every qualified key that serves it."""
        monkeypatch.setattr(
            EarthLens,
            "DataSources",
            self._synthetic_registry("dem:elevation", "bathymetry:elevation"),
        )
        with pytest.raises(AmbiguousDataSourceError, match="reserved topic") as exc:
            EarthLens._check_source("elevation")
        assert "dem:elevation" in str(exc.value)
        assert "bathymetry:elevation" in str(exc.value)

    def test_unclaimed_reserved_word_falls_through_to_did_you_mean(self, monkeypatch):
        """A reserved word no source qualifies is an ordinary unknown key."""
        monkeypatch.setattr(EarthLens, "DataSources", self._synthetic_registry("chc"))
        with pytest.raises(ValueError, match="is not a supported data source") as exc:
            EarthLens._check_source("precipitation")
        assert not isinstance(exc.value, AmbiguousDataSourceError)

    def test_unknown_word_still_lists_valid_keys(self, monkeypatch):
        """A non-reserved unknown key raises the enumerating ValueError."""
        monkeypatch.setattr(
            EarthLens, "DataSources", self._synthetic_registry("chc", "cmems")
        )
        with pytest.raises(ValueError, match="is not a supported data source"):
            EarthLens._check_source("bogus")

    def test_ambiguous_error_is_a_valueerror(self):
        """`AmbiguousDataSourceError` subclasses `ValueError` for existing catchers."""
        assert issubclass(AmbiguousDataSourceError, ValueError)


@pytest.mark.ecmwf
class TestECMWFBackend:
    """Tests for the C1+L3 fix that registers ECMWF in the facade.

    Pre-C1, `EarthLens(data_source="ecmwf", ...)` raised
    `ValueError: ecmwf not supported` because the `DataSources`
    mapping omitted ECMWF. These tests pin the registration so
    regressions show up immediately.
    """

    def test_ecmwf_is_registered_in_data_sources(self):
        """`EarthLens.DataSources` maps `"ecmwf"` to :class:`ECMWF`."""
        assert "ecmwf" in EarthLens.DataSources, (
            f"'ecmwf' missing from DataSources keys: {sorted(EarthLens.DataSources)}"
        )
        assert EarthLens.DataSources["ecmwf"] is ECMWF, (
            f"DataSources['ecmwf'] should be the ECMWF class; got "
            f"{EarthLens.DataSources['ecmwf']!r}"
        )

    def test_facade_accepts_ecmwf_data_source(self, tmp_path, monkeypatch):
        """`EarthLens(data_source="ecmwf", ...)` no longer raises."""
        monkeypatch.setattr(cdsapi, "Client", lambda: _SentinelClient())

        earthlens = EarthLens(
            data_source="ecmwf",
            temporal_resolution="daily",
            start="2022-01-01",
            end="2022-01-01",
            variables={
                "reanalysis-era5-single-levels": ["2m-temperature"],
            },
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path=str(tmp_path),
        )

        assert isinstance(earthlens.datasource, ECMWF), (
            f"datasource should be an ECMWF instance; got "
            f"{type(earthlens.datasource).__name__}"
        )

    def test_unknown_data_source_still_raises(self, tmp_path):
        """Unknown `data_source` values still raise `ValueError`."""
        with pytest.raises(ValueError, match="is not a supported data source"):
            EarthLens(
                data_source="not-a-real-source",
                start="2022-01-01",
                end="2022-01-01",
                variables=["2m-temperature"],
                lat_lim=[4.0, 5.0],
                lon_lim=[-75.0, -74.0],
                path=str(tmp_path),
            )

    def test_ecmwf_facade_propagates_constructor_arguments(self, tmp_path, monkeypatch):
        """The facade threads its constructor args into ECMWF unchanged."""
        monkeypatch.setattr(cdsapi, "Client", lambda: _SentinelClient())

        earthlens = EarthLens(
            data_source="ecmwf",
            temporal_resolution="monthly",
            start="2022-01-01",
            end="2022-02-01",
            variables={
                "reanalysis-era5-single-levels": [
                    "2m-temperature",
                    "total-precipitation",
                ],
            },
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path=str(tmp_path),
        )

        ecmwf = earthlens.datasource
        assert ecmwf.vars == {
            "reanalysis-era5-single-levels": [
                "2m-temperature",
                "total-precipitation",
            ],
        }, f"variables should be threaded through; got {ecmwf.vars!r}"
        assert ecmwf.temporal_resolution == "monthly", (
            f"temporal_resolution should be 'monthly'; got "
            f"{ecmwf.temporal_resolution!r}"
        )
        assert ecmwf.root_dir == tmp_path.resolve(), (
            f"root_dir should be the tmp path; got {ecmwf.root_dir}"
        )

    def test_dataset_arg_composes_variables_dict(self, tmp_path, monkeypatch):
        """`dataset=` + a list yields the same vars as the nested-dict form."""
        monkeypatch.setattr(cdsapi, "Client", lambda: _SentinelClient())

        earthlens = EarthLens(
            data_source="ecmwf",
            temporal_resolution="monthly",
            start="2022-01-01",
            end="2022-02-01",
            dataset="reanalysis-era5-single-levels",
            variables=["2m-temperature", "total-precipitation"],
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path=str(tmp_path),
        )

        assert earthlens.datasource.vars == {
            "reanalysis-era5-single-levels": [
                "2m-temperature",
                "total-precipitation",
            ],
        }, f"dataset= should compose the keyed dict; got {earthlens.datasource.vars!r}"

    def test_dataset_arg_with_dict_variables_raises(self, tmp_path, monkeypatch):
        """`dataset=` together with a dict `variables` is rejected."""
        monkeypatch.setattr(cdsapi, "Client", lambda: _SentinelClient())

        with pytest.raises(ValueError, match="pass variables= as a list"):
            EarthLens(
                data_source="ecmwf",
                start="2022-01-01",
                end="2022-02-01",
                dataset="reanalysis-era5-single-levels",
                variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
                lat_lim=[4.0, 5.0],
                lon_lim=[-75.0, -74.0],
                path=str(tmp_path),
            )

    def test_full_download_through_facade_routes_to_cdsapi(self, tmp_path, monkeypatch):
        """End-to-end: `EarthLens(...).download()` reaches CDS.

        * Two cdsapi.Client.retrieve calls — one per variable
        * Each retrieve receives the right dataset name and
          `variable=[cds_variable]` from the catalog

        Per-date GeoTIFF post-processing is intentionally not
        part of the package; see
        `examples/post_process_ecmwf_netcdf.py`.
        """
        retrieved = []

        class FakeClient:
            def retrieve(self, dataset, request, target):
                retrieved.append((dataset, request, target))
                # cdsapi always writes the file it is handed; the backend
                # treats a retrieve that wrote nothing as a failed download.
                pathlib.Path(target).write_bytes(b"")

        monkeypatch.setattr(cdsapi, "Client", FakeClient)

        earthlens = EarthLens(
            data_source="ecmwf",
            temporal_resolution="daily",
            start="2022-01-01",
            end="2022-01-01",
            variables={
                "reanalysis-era5-single-levels": [
                    "2m-temperature",
                    "total-precipitation",
                ],
            },
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path=str(tmp_path),
        )
        earthlens.download(progress_bar=False)

        assert len(retrieved) == 2, (
            f"Expected 2 retrieve calls (one per variable); got {len(retrieved)}"
        )
        datasets = [args[0] for args in retrieved]
        variables = [args[1]["variable"] for args in retrieved]
        assert datasets == [
            "reanalysis-era5-single-levels",
            "reanalysis-era5-single-levels",
        ], f"datasets: {datasets!r}"
        assert variables == [
            ["2m_temperature"],
            ["total_precipitation"],
        ], f"variables: {variables!r}"


@pytest.mark.unit
class TestEarthLensDownloadAggregate:
    """Tests for the M3 `aggregate` pass-through on `EarthLens.download`."""

    @pytest.fixture
    def stub_facade(self, tmp_path, monkeypatch):
        """Build an `EarthLens` whose `.datasource` is a MagicMock.

        The facade is instantiated normally (with cdsapi.Client
        mocked) so its constructor logic runs unchanged; then
        `.datasource` is replaced with a MagicMock so we can inspect
        what `download()` forwards into the backend without
        exercising the real `ECMWF.download` body.

        Returns:
            EarthLens: Facade ready for `download(...)` calls; its
            `datasource.download` is a `MagicMock` exposing
            `call_args`.
        """
        monkeypatch.setattr(cdsapi, "Client", lambda: _SentinelClient())

        earthlens = EarthLens(
            data_source="ecmwf",
            temporal_resolution="daily",
            start="2022-01-01",
            end="2022-01-01",
            variables={"reanalysis-era5-single-levels": ["2m-temperature"]},
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path=str(tmp_path),
        )
        stub = MagicMock(name="stub_backend")
        # Match the C1 contract: ECMWF is a raster backend, so the
        # aggregate-guard in EarthLens.download should accept
        # `aggregate=` and forward it. Without this, MagicMock would
        # synthesise a child mock for `.OUTPUT_KIND` and the guard
        # would (correctly) reject it as not in {raster, mixed}.
        stub.OUTPUT_KIND = "raster"
        earthlens.datasource = stub
        return earthlens

    def test_aggregate_none_does_not_reach_backend(self, stub_facade):
        """`aggregate=None` (default) leaves the backend kwargs untouched."""
        stub_facade.download(progress_bar=False)
        _, kwargs = stub_facade.datasource.download.call_args
        assert "aggregate" not in kwargs, (
            f"`aggregate` should not appear in backend kwargs when None; "
            f"got kwargs={kwargs!r}"
        )

    def test_aggregate_config_forwarded_to_backend(self, stub_facade):
        """`aggregate=cfg` reaches the backend's `download` as a kwarg."""
        cfg = AggregationConfig(freq="1MS", op="sum")
        stub_facade.download(progress_bar=False, aggregate=cfg)
        _, kwargs = stub_facade.datasource.download.call_args
        assert kwargs.get("aggregate") is cfg, (
            f"Expected backend to receive the same config instance; "
            f"got kwargs={kwargs!r}"
        )

    def test_progress_bar_still_forwarded_alongside_aggregate(self, stub_facade):
        """Adding `aggregate` does not displace `progress_bar` in the kwargs."""
        cfg = AggregationConfig(freq="1D")
        stub_facade.download(progress_bar=False, aggregate=cfg)
        _, kwargs = stub_facade.datasource.download.call_args
        assert kwargs.get("progress_bar") is False, (
            f"`progress_bar` should still be forwarded; got kwargs={kwargs!r}"
        )
        assert kwargs.get("aggregate") is cfg, (
            f"`aggregate` should be forwarded alongside; got kwargs={kwargs!r}"
        )

    def test_extra_kwargs_pass_through_unchanged(self, stub_facade):
        """Backend-specific kwargs (e.g. CHIRPS `cores=`) still pass through."""
        stub_facade.download(progress_bar=False, cores=4)
        _, kwargs = stub_facade.datasource.download.call_args
        assert kwargs.get("cores") == 4, (
            f"Passed-through kwargs should reach the backend verbatim; "
            f"got kwargs={kwargs!r}"
        )


@pytest.mark.unit
class TestTopLevelReExports:
    """Pin the `earthlens.core` public surface (L2)."""

    def test_earthlens_facade_importable_from_package_root(self):
        """`from earthlens.core import EarthLens` resolves to the facade class."""
        import earthlens.core

        assert earthlens.core.EarthLens is EarthLens, (
            f"Top-level re-export should be the facade class; got "
            f"{earthlens.core.EarthLens!r}"
        )

    def test_aggregate_symbols_importable_from_package_root(self):
        """`AggregationConfig` and `aggregate_netcdf` resolve at top level."""
        import earthlens.core

        assert earthlens.core.AggregationConfig is AggregationConfig, (
            f"Top-level AggregationConfig drift: {earthlens.core.AggregationConfig!r}"
        )
        assert callable(earthlens.core.aggregate_netcdf), (
            f"Top-level aggregate_netcdf must be callable; got "
            f"{earthlens.core.aggregate_netcdf!r}"
        )

    def test_all_lists_only_sdk_free_symbols(self):
        """`__all__` excludes the per-backend classes (each needs an extra)."""
        import earthlens.core

        assert sorted(earthlens.core.__all__) == [
            "AggregatedWindow",
            "AggregationConfig",
            "AmbiguousDataSourceError",
            "EarthLens",
            "PolygonAoiWarning",
            "aggregate_netcdf",
            "cache_dir",
            "download",
            "find",
            "iter_aggregate_netcdf",
            "output_dir",
            "search",
            "set_cache_dir",
            "set_output_dir",
            "sources",
        ], f"Unexpected top-level __all__: {earthlens.core.__all__!r}"


class TestFunctionalDownload:
    """The one-shot earthlens.core.download() entry point."""

    def test_download_is_exported(self):
        """earthlens.core.download is a callable on the package surface."""
        import earthlens.core

        assert callable(earthlens.core.download), "download should be callable"

    def test_download_delegates_to_facade(self, monkeypatch):
        """download() builds an EarthLens and forwards the run-time args."""
        import earthlens.core
        from earthlens import earthlens as facade_module

        captured = {}

        class _FakeFacade:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def download(self, **kwargs):
                captured["download"] = kwargs
                return ["written.tif"]

        monkeypatch.setattr(facade_module, "EarthLens", _FakeFacade)
        result = earthlens.core.download(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path="out",
            progress_bar=False,
        )
        assert result == ["written.tif"], "should return the facade result"
        assert captured["init"]["data_source"] == "chc"
        assert captured["init"]["variables"] == ["precipitation"]
        assert captured["download"] == {"progress_bar": False, "aggregate": None}

    def test_facade_download_forwards_backend_return(self, tmp_path, monkeypatch):
        """EarthLens(...).download() returns the backend's value, never None (H2)."""
        facade = EarthLens(
            "chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
        )
        sentinel = [tmp_path / "a.tif", tmp_path / "b.tif"]
        monkeypatch.setattr(facade.datasource, "download", lambda *a, **k: sentinel)
        result = facade.download(progress_bar=False)
        assert result is sentinel, (
            f"facade must forward the backend paths; got {result}"
        )


def _write_ones_tif(path):
    """Write a tiny all-ones GeoTIFF to `path` for load() tests."""
    import numpy as np
    from pyramids.dataset import Dataset, GeoReference

    Dataset.from_array(
        np.ones((4, 4), "float32"),
        no_data_value=-9999.0,
        geo_ref=GeoReference(geo=(0.0, 1.0, 0.0, 4.0, 0.0, -1.0), epsg=4326),
    ).to_file(str(path))


class TestFacadeLoad:
    """EarthLens.load() returns native pyramids objects in memory (H3)."""

    def _facade(self, tmp_path):
        return EarthLens(
            "chc",
            variables=["precipitation"],
            start="2020-01-01",
            end="2020-01-02",
            path=str(tmp_path),
        )

    def test_load_reads_rasters_into_pyramids(self, tmp_path, monkeypatch):
        """A list of written raster paths is read into pyramids Dataset objects."""
        from pyramids.dataset import Dataset

        tif = tmp_path / "ones.tif"
        _write_ones_tif(tif)
        facade = self._facade(tmp_path)
        monkeypatch.setattr(facade.datasource, "download", lambda *a, **k: [tif])
        loaded = facade.load(progress_bar=False)
        assert isinstance(loaded[0], Dataset), f"raster not loaded: {loaded!r}"
        assert loaded[0].read_array().shape == (4, 4), "loaded array shape wrong"

    def test_load_passes_through_in_memory_result(self, tmp_path, monkeypatch):
        """A non-list (GeoDataFrame / DataFrame) download result passes through."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        gdf = gpd.GeoDataFrame(geometry=[shapely.geometry.box(0, 0, 1, 1)], crs=4326)
        facade = self._facade(tmp_path)
        monkeypatch.setattr(facade.datasource, "download", lambda *a, **k: gdf)
        out = facade.load(progress_bar=False)
        assert out is gdf, "in-memory result not passed through"

    def test_load_leaves_non_raster_paths_alone(self, tmp_path):
        """A mixed result reads rasters but leaves a .csv table as a Path."""
        from pyramids.dataset import Dataset

        from earthlens.earthlens import _load_result

        tif = tmp_path / "ones.tif"
        _write_ones_tif(tif)
        csv = tmp_path / "table.csv"
        csv.write_text("a,b\n1,2\n")
        out = _load_result([tif, csv])
        assert isinstance(out[0], Dataset), "raster should be loaded"
        assert out[1] == csv, "a .csv table should stay a Path"

    def test_load_reads_netcdf_into_netcdf(self, tmp_path):
        """A written .nc path is read into a pyramids NetCDF, not a Dataset."""
        import numpy as np
        from pyramids.dataset import Dataset, GeoReference
        from pyramids.netcdf import NetCDF

        from earthlens.earthlens import _load_result

        nc = tmp_path / "cube.nc"
        Dataset.from_array(
            np.ones((4, 4), "float32"),
            geo_ref=GeoReference(geo=(0.0, 1.0, 0.0, 4.0, 0.0, -1.0), epsg=4326),
        ).to_file(str(nc))
        out = _load_result([nc])
        assert isinstance(out[0], NetCDF), (
            f"a .nc should read as NetCDF; got {out[0]!r}"
        )

    def test_module_download_load_true_calls_load(self, monkeypatch):
        """earthlens.core.download(load=True) routes to EarthLens.load()."""
        import earthlens.core
        from earthlens import earthlens as facade_module

        calls = {}

        class _FakeFacade:
            def __init__(self, **kwargs):
                calls["init"] = kwargs

            def download(self, **kwargs):
                calls["download"] = True
                return ["x.tif"]

            def load(self, **kwargs):
                calls["load"] = True
                return ["loaded"]

        monkeypatch.setattr(facade_module, "EarthLens", _FakeFacade)
        result = earthlens.core.download(
            data_source="chc", variables=["precipitation"], load=True
        )
        assert result == ["loaded"], "download(load=True) should return load()"
        assert calls.get("load") and "download" not in calls, "should call load()"


class TestTopLevelDiscovery:
    """Module-level sources() / search() / find() conveniences (M1)."""

    def test_sources_lists_registered_keys(self):
        """sources() returns the sorted registered data_source keys."""
        import earthlens.core

        keys = earthlens.core.sources()
        assert keys == sorted(keys), "sources() should be sorted"
        assert "chc" in keys and "gee" in keys, f"missing core keys: {keys[:5]}"

    def test_sources_collapses_aliases_to_canonical(self):
        """sources() lists one canonical key per backend, not the aliases."""
        import earthlens.core

        keys = earthlens.core.sources()
        assert "chirps" not in keys, "the chirps alias should collapse to chc"
        assert "google-earth-engine" not in keys, "the gee alias should collapse"
        assert "planetary-computer" not in keys, "STAC endpoint keys collapse to stac"
        assert "stac" in keys, "the canonical stac key should be present"
        assert len(keys) == len(set(keys)), "no duplicate keys"

    def test_sources_is_exported(self):
        """sources / search / find are on the package surface."""
        import earthlens.core

        assert all(
            callable(getattr(earthlens.core, name))
            for name in ("sources", "search", "find")
        ), "sources/search/find must be callable package attributes"

    def test_search_delegates_to_facade(self, monkeypatch):
        """search() builds an EarthLens and returns its .search()."""
        import earthlens.core
        from earthlens import earthlens as facade_module

        captured = {}

        class _FakeFacade:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def search(self):
                captured["search"] = True
                return ["product"]

        monkeypatch.setattr(facade_module, "EarthLens", _FakeFacade)
        result = earthlens.core.search(data_source="stac", variables=["red"])
        assert result == ["product"], "search() should return facade.search()"
        assert captured["init"]["data_source"] == "stac"
        assert captured.get("search"), "facade.search() should be called"

    def test_find_aggregates_guess_dataset(self, monkeypatch):
        """find() collects guess_dataset hits and skips sources that raise."""
        import earthlens.core
        from earthlens import earthlens as facade_module

        monkeypatch.setattr(facade_module, "sources", lambda: ["chc", "gee", "broken"])

        def _fake_guess(cls, source, text):
            if source == "broken":
                raise ModuleNotFoundError("No module named 'ee'", name="ee")
            return [f"{source}-ds"] if source == "chc" else []

        monkeypatch.setattr(
            facade_module.EarthLens,
            "guess_dataset",
            classmethod(_fake_guess),
        )
        result = earthlens.core.find("precip")
        assert result == {"chc": ["chc-ds"]}, f"unexpected find() result: {result}"


@pytest.mark.chc
class TestFacadeCadence:
    """The cadence= alias for temporal_resolution."""

    def _temporal_resolution(self, tmp_path, **kwargs):
        return EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
            **kwargs,
        ).datasource.temporal_resolution

    def test_cadence_overrides_temporal_resolution(self, tmp_path):
        """cadence= takes precedence over temporal_resolution."""
        resolved = self._temporal_resolution(
            tmp_path, temporal_resolution="daily", cadence="monthly"
        )
        assert resolved == "monthly", f"cadence should win; got {resolved}"

    def test_temporal_resolution_used_when_no_cadence(self, tmp_path):
        """temporal_resolution still applies when cadence is omitted."""
        resolved = self._temporal_resolution(tmp_path, temporal_resolution="monthly")
        assert resolved == "monthly", f"got {resolved}"


class TestFacadeConstructorOrder:
    """data_source is the first positional; the legacy order is shimmed."""

    def _base(self, tmp_path):
        return dict(start="2009-01-01", end="2009-01-02", path=str(tmp_path))

    def test_data_source_first_positional(self, tmp_path):
        """EarthLens('chc', variables=[...]) puts data_source first."""
        el = EarthLens("chc", variables=["precipitation"], **self._base(tmp_path))
        assert type(el.datasource).__name__ == "CHIRPS", "data_source-first failed"

    def test_keyword_order_still_works(self, tmp_path):
        """The all-keyword call (variables=, data_source=) is unaffected."""
        el = EarthLens(
            variables=["precipitation"], data_source="chc", **self._base(tmp_path)
        )
        assert type(el.datasource).__name__ == "CHIRPS", "keyword call failed"

    def test_legacy_positional_order_warns_and_swaps(self, tmp_path):
        """The legacy EarthLens(variables, data_source) order warns and swaps."""
        with pytest.warns(DeprecationWarning, match="data_source first"):
            el = EarthLens(["precipitation"], "chc", **self._base(tmp_path))
        assert el.datasource.vars == {"global-daily": ["precipitation"]}, "swap failed"

    def test_legacy_single_positional_list_defaults_chc(self, tmp_path):
        """A lone EarthLens([...]) variables list defaults the source to chc."""
        with pytest.warns(DeprecationWarning):
            el = EarthLens(["precipitation"], **self._base(tmp_path))
        assert type(el.datasource).__name__ == "CHIRPS", "default source failed"

    def test_missing_variables_raises(self, tmp_path):
        """A source with no variables= raises a clear error, not a backend TypeError."""
        with pytest.raises(ValueError, match="variables= is required"):
            EarthLens("chc", **self._base(tmp_path))


class TestFacadeTimeRange:
    """The single time= range splits into start/end (L1)."""

    def _window(self, tmp_path, **kwargs):
        time = EarthLens(
            "chc", variables=["precipitation"], path=str(tmp_path), **kwargs
        ).datasource.time
        return (
            time.start_date.date().isoformat(),
            time.end_date.date().isoformat(),
        )

    def test_interval_string(self, tmp_path):
        """time='a/b' sets start and end."""
        assert self._window(tmp_path, time="2020-01-01/2020-01-31") == (
            "2020-01-01",
            "2020-01-31",
        )

    def test_tuple(self, tmp_path):
        """time=(a, b) sets start and end."""
        assert self._window(tmp_path, time=("2020-01-01", "2020-02-01")) == (
            "2020-01-01",
            "2020-02-01",
        )

    def test_slice(self, tmp_path):
        """time=slice(a, b) sets start and end."""
        assert self._window(tmp_path, time=slice("2020-01-01", "2020-03-01")) == (
            "2020-01-01",
            "2020-03-01",
        )

    def test_time_with_start_end_raises(self, tmp_path):
        """Passing both time= and start=/end= is rejected."""
        with pytest.raises(ValueError, match="either time= or start="):
            EarthLens(
                "chc",
                variables=["precipitation"],
                path=str(tmp_path),
                time="2020-01-01/2020-02-01",
                start="2020-01-01",
            )

    @pytest.mark.parametrize(
        "time",
        ["2020-01-01/", "/2020-01-31", ("2020-01-01", None), slice("2020-01-01", None)],
    )
    def test_open_ended_time_raises(self, tmp_path, time):
        """An open-ended time= (a None bound) is rejected, not expanded to today."""
        with pytest.raises(ValueError, match="needs both bounds"):
            EarthLens(
                "chc",
                variables=["precipitation"],
                path=str(tmp_path),
                time=time,
            )


@pytest.mark.chc
class TestFacadePath:
    """The facade's output-path defaulting."""

    def test_omitted_path_download_persists_to_named_subdir(
        self, tmp_path, monkeypatch
    ):
        """download() with an omitted path persists under <output_dir()>/<source>/."""
        from pathlib import Path

        from earthlens.config import set_output_dir

        monkeypatch.chdir(tmp_path)
        set_output_dir(tmp_path / "configured")
        facade = EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
        )
        expected = Path(tmp_path / "configured").resolve() / "chc"
        assert facade.datasource.root_dir == expected, (
            f"got {facade.datasource.root_dir}"
        )
        assert not expected.exists(), "construction must not create the directory"

        # The stub replaces the backend's own download, which is what the
        # base class wrapped, so drive the directory hook the wrapper runs.
        monkeypatch.setattr(facade.datasource, "download", lambda *a, **k: [])
        facade.datasource._ensure_root_dir()
        facade.download(progress_bar=False)
        assert expected.is_dir(), "download() should create the default directory"

    def test_omitted_path_load_uses_tempdir(self, tmp_path, monkeypatch):
        """load() redirects to a temp dir and removes the empty default."""
        from pathlib import Path

        from earthlens.config import set_output_dir

        monkeypatch.chdir(tmp_path)
        set_output_dir(tmp_path / "configured")
        facade = EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
        )
        monkeypatch.setattr(facade.datasource, "download", lambda *a, **k: [])
        temp_dir = None

        def _capture(*a, **k):
            nonlocal temp_dir
            temp_dir = facade.datasource.path
            return []

        monkeypatch.setattr(facade.datasource, "download", _capture)
        facade.load(progress_bar=False)
        default = Path(tmp_path / "configured").resolve() / "chc"
        assert facade.datasource.root_dir != default, (
            "load() should redirect off the default"
        )
        assert not default.exists(), "load() should remove the empty default dir"
        # An empty result holds no handle into the temp dir, so it is gone at once.
        assert not Path(temp_dir).exists(), "load() leaked its temp dir"

    def test_blank_path_still_means_the_working_directory(self, tmp_path, monkeypatch):
        """An explicit path="" opts into the cwd even when an output dir is configured."""
        from pathlib import Path

        from earthlens.config import set_output_dir

        monkeypatch.chdir(tmp_path)
        set_output_dir(tmp_path / "configured")
        backend = EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path="",
        ).datasource
        set_output_dir(None)
        assert backend.root_dir == Path.cwd(), f"got {backend.root_dir}"


class _FakeRaster:
    """A stand-in for a pyramids raster: `copy()` detaches, `close()` releases."""

    def __init__(self, detached=False):
        self.detached = detached
        self.closed = False

    def copy(self):
        return _FakeRaster(detached=True)

    def close(self):
        self.closed = True


class TestLoadTempdirCleanup:
    """load() detaches its result from the temp dir and removes it at once."""

    def _tempdir(self, tmp_path):
        d = tmp_path / "earthlens-load-x"
        d.mkdir()
        (d / "raster.tif").write_bytes(b"data")
        return d

    def test_inmemory_result_removed_immediately(self, tmp_path):
        """A passthrough (non-list) result holds no handle, so the dir goes at once."""
        from earthlens.earthlens import _detach_and_cleanup

        d = self._tempdir(tmp_path)
        out = _detach_and_cleanup(d, {"in": "memory"})
        assert out == {"in": "memory"}, "passthrough result should be returned as-is"
        assert not d.exists(), "in-memory result should free the temp dir immediately"

    def test_empty_list_removed_immediately(self, tmp_path):
        """An empty list has nothing reading from the dir, so it goes at once."""
        from earthlens.earthlens import _detach_and_cleanup

        d = self._tempdir(tmp_path)
        _detach_and_cleanup(d, [])
        assert not d.exists(), "empty result should free the temp dir immediately"

    def test_rasters_detached_and_dir_removed(self, tmp_path):
        """Each raster is replaced by its in-memory copy, closed, and the dir removed."""
        from earthlens.earthlens import _detach_and_cleanup

        d = self._tempdir(tmp_path)
        original = _FakeRaster()
        out = _detach_and_cleanup(d, [original])
        assert original.closed, "the file-backed raster should be closed"
        assert out[0] is not original, "result should be the detached in-memory copy"
        assert out[0].detached, "returned raster should be the copy()"
        assert not d.exists(), "temp dir should be removed once rasters are detached"

    def test_raw_path_result_deferred_to_exit_sweep(self, tmp_path):
        """A raw Path in the result defers cleanup to the process-exit sweep."""
        from earthlens.earthlens import (
            _LOAD_TEMP_DIRS,
            _detach_and_cleanup,
            _sweep_load_tempdirs,
        )

        d = self._tempdir(tmp_path)
        _detach_and_cleanup(d, [d / "table.csv"])
        assert d.exists(), "a caller-owned path must not be removed immediately"
        assert str(d) in _LOAD_TEMP_DIRS, "dir should be registered for the exit sweep"
        _sweep_load_tempdirs()
        assert not d.exists(), "exit sweep should remove the deferred dir"


class TestFacadeOptions:
    """The facade's backend-option discovery and early kwarg validation."""

    @pytest.mark.gee
    def test_options_for_lists_backend_extras(self):
        """options_for() surfaces the backend-specific constructor knobs."""
        options = EarthLens.options_for("gee")
        assert "scale" in options and "crs" in options, f"missing knobs: {options}"

    @pytest.mark.gee
    def test_options_for_excludes_facade_params(self):
        """options_for() omits the parameters the facade owns."""
        options = EarthLens.options_for("gee")
        assert not ({"start", "lat_lim", "variables"} & set(options)), options

    def test_options_for_unknown_source_raises(self):
        """options_for() rejects an unknown data_source."""
        with pytest.raises(ValueError, match="is not a supported data source"):
            EarthLens.options_for("nope")

    @pytest.mark.chc
    def test_unexpected_kwarg_raises_typeerror(self, tmp_path):
        """An unknown backend kwarg is rejected up front with a TypeError."""
        with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
            EarthLens(
                data_source="chc",
                variables=["precipitation"],
                start="2009-01-01",
                end="2009-01-02",
                path=str(tmp_path),
                foo="bar",
            )

    @pytest.mark.gee
    def test_unexpected_kwarg_suggests_closest(self):
        """A near-miss kwarg name suggests the closest backend option."""
        with pytest.raises(TypeError, match="scale"):
            EarthLens(
                data_source="gee",
                variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
                start="2022-01-01",
                end="2022-01-02",
                scal=90,
            )


@pytest.mark.chc
class TestFacadeDelegation:
    """The facade delegates unknown attributes to the bound backend."""

    def _facade(self, tmp_path):
        return EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
        )

    def test_delegates_backend_attribute(self, tmp_path):
        """A backend attribute is reachable through the facade."""
        facade = self._facade(tmp_path)
        # `vars` lives on the backend, not the facade.
        assert facade.vars == facade.datasource.vars

    def test_delegates_backend_method(self, tmp_path, monkeypatch):
        """A backend method is forwarded and called on the backend."""
        facade = self._facade(tmp_path)
        monkeypatch.setattr(
            facade.datasource, "_demo_helper", lambda: "from-backend", raising=False
        )
        assert facade._demo_helper() == "from-backend"

    def test_facade_own_attribute_takes_precedence(self, tmp_path):
        """The facade's own attributes win over delegation."""
        facade = self._facade(tmp_path)
        # `download` is defined on the facade itself.
        assert facade.download.__qualname__.startswith("EarthLens")

    def test_unknown_attribute_raises(self, tmp_path):
        """An attribute on neither facade nor backend raises AttributeError."""
        facade = self._facade(tmp_path)
        with pytest.raises(AttributeError):
            facade.totally_not_a_real_attribute

    def test_dunder_not_delegated(self, tmp_path):
        """Dunder lookups are not delegated (avoids proxying magic methods)."""
        facade = self._facade(tmp_path)
        with pytest.raises(AttributeError):
            facade.__nonexistent_dunder__

    def test_attribute_before_backend_bound_raises(self):
        """Accessing an attribute before `datasource` is set raises AttributeError."""
        facade = EarthLens.__new__(EarthLens)
        with pytest.raises(AttributeError, match="has no attribute"):
            facade.some_attribute

    def test_dir_includes_backend_attributes(self, tmp_path):
        """dir() surfaces the bound backend's attributes for tab-completion."""
        facade = self._facade(tmp_path)
        names = dir(facade)
        assert "vars" in names and "download" in names, "dir() should merge both"

    def test_authenticate_returns_facade_and_delegates(self, tmp_path, monkeypatch):
        """authenticate() forwards to the backend and returns the facade."""
        facade = self._facade(tmp_path)
        called = []
        monkeypatch.setattr(
            facade.datasource, "authenticate", lambda: called.append(True)
        )
        result = facade.authenticate()
        assert result is facade, "authenticate() should return the facade for chaining"
        assert called == [True], "should delegate to the backend's authenticate()"


@pytest.mark.chc
class TestFacadeSearch:
    """The facade's search() / count() / preview() dry-run surface."""

    def _facade(self, tmp_path):
        return EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
        )

    def test_search_on_legacy_backend_raises(self, tmp_path):
        """A legacy _api-only backend rejects search() with guidance."""
        with pytest.raises(NotImplementedError, match="search\\(\\)/preview"):
            self._facade(tmp_path).search()

    def test_count_on_legacy_backend_raises(self, tmp_path):
        """A legacy backend rejects count() with the same guidance."""
        with pytest.raises(NotImplementedError, match="call download"):
            self._facade(tmp_path).count()

    def test_search_delegates_to_backend(self, tmp_path, monkeypatch):
        """search() returns the backend's _search products."""
        from earthlens.base import RemoteProduct

        facade = self._facade(tmp_path)
        products = [RemoteProduct(id="a"), RemoteProduct(id="b")]
        monkeypatch.setattr(facade.datasource, "_search", lambda: products)
        assert facade.search() == products, "search() should return the products"

    def test_count_uses_search_length(self, tmp_path, monkeypatch):
        """count() falls back to the length of _search."""
        from earthlens.base import RemoteProduct

        facade = self._facade(tmp_path)
        monkeypatch.setattr(
            facade.datasource,
            "_search",
            lambda: [RemoteProduct(id=str(i)) for i in range(3)],
        )
        assert facade.count() == 3, "count() should match the product count"

    def test_preview_flattens_products(self, tmp_path, monkeypatch):
        """preview() flattens id / href / metadata and caps at n."""
        from earthlens.base import RemoteProduct

        facade = self._facade(tmp_path)
        products = [
            RemoteProduct(id="a", href="h1", metadata={"cloud": 5}),
            RemoteProduct(id="b", href="h2"),
        ]
        monkeypatch.setattr(facade.datasource, "_search", lambda: products)
        assert facade.preview(1) == [{"id": "a", "href": "h1", "cloud": 5}]


@pytest.mark.chc
class TestFacadeDiscovery:
    """The facade's catalog-discovery classmethods."""

    def test_catalog_returns_loaded_catalog(self):
        """catalog() returns the backend's loaded catalog."""
        catalog = EarthLens.catalog("chc")
        assert len(catalog) > 0, "the CHC catalog should expose datasets"

    def test_list_datasets_includes_known_key(self):
        """list_datasets() returns the curated dataset keys."""
        keys = EarthLens.list_datasets("chc")
        assert "africa-monthly" in keys, f"missing africa-monthly in {keys[:5]}..."

    def test_describe_dataset_returns_record(self):
        """describe_dataset() returns a record carrying variables."""
        dataset = EarthLens.describe_dataset("chc", "africa-monthly")
        assert dataset.variables, "the dataset record should declare variables"

    def test_describe_unknown_dataset_raises(self):
        """describe_dataset() suggests the closest key on a miss."""
        with pytest.raises(ValueError, match="africa-monthly"):
            EarthLens.describe_dataset("chc", "africa-month")

    def test_guess_dataset_substring(self):
        """guess_dataset() finds datasets by case-insensitive substring."""
        hits = EarthLens.guess_dataset("chc", "MONTHLY")
        assert "africa-monthly" in hits, f"substring search missed it: {hits}"

    def test_guess_dataset_fuzzy_fallback(self):
        """guess_dataset() falls back to fuzzy matches when no substring hits."""
        hits = EarthLens.guess_dataset("chc", "africa-dialy")
        assert any("africa-daily" == h for h in hits), f"no fuzzy match in {hits}"

    def test_discovery_unknown_source_raises(self):
        """An unknown data_source is rejected with a did-you-mean hint."""
        with pytest.raises(ValueError, match="is not a supported data source"):
            EarthLens.list_datasets("chrips")

    def test_catalog_missing_raises_not_implemented(self, monkeypatch):
        """A backend whose module ships no Catalog raises NotImplementedError."""
        import earthlens.chc as chc_module

        monkeypatch.delattr(chc_module, "Catalog", raising=False)
        with pytest.raises(NotImplementedError, match="no catalog"):
            EarthLens.catalog("chc")

    def test_catalog_missing_sdk_raises_importerror(self, monkeypatch):
        """A backend whose module fails to import surfaces a friendly ImportError."""
        from earthlens import earthlens as facade_module

        def _boom(name):
            raise ModuleNotFoundError("No module named 'ee'", name="ee")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError, match="Backend catalog for"):
            EarthLens.catalog("gee")


class TestImportBackendModule:
    """The shared on-demand backend import used by the registry and catalog."""

    def test_returns_the_imported_module(self):
        """An installed backend is imported and handed back."""
        from earthlens.earthlens import _import_backend_module

        module = _import_backend_module("earthlens.chc", "chc", "")
        assert module.__name__ == "earthlens.chc", f"got {module.__name__}"

    def test_missing_sdk_names_the_key_and_extra(self, monkeypatch):
        """A failed import reports the key plus the pip extra that fixes it."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        def _boom(name):
            raise ModuleNotFoundError("No module named 'ee'", name="ee")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError) as exc:
            _import_backend_module("earthlens.gee", "gee", "gee")
        message = str(exc.value)
        assert "Backend 'gee' is unavailable" in message, f"got {message}"
        assert "pip install earthlens[gee]" in message, f"hint missing from {message}"

    def test_sdk_free_backend_gets_no_install_hint(self, monkeypatch):
        """With no extra there is nothing to install, so no hint is offered."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        def _boom(name):
            raise ModuleNotFoundError("No module named 'ftplib_x'", name="ftplib_x")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError) as exc:
            _import_backend_module("earthlens.chc", "chc", "")
        assert "pip install" not in str(exc.value), f"unexpected hint: {exc.value}"

    def test_subject_customises_the_opening(self, monkeypatch):
        """The catalog path opens the message with its own subject."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        def _boom(name):
            raise ModuleNotFoundError("No module named 'ee'", name="ee")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError, match="Backend catalog for 'gee'"):
            _import_backend_module(
                "earthlens.gee", "gee", "gee", subject="Backend catalog for"
            )

    def test_original_error_is_chained(self, monkeypatch):
        """The underlying ImportError is preserved as __cause__ for debugging."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        original = ModuleNotFoundError("No module named 'ee'", name="ee")

        def _boom(name):
            raise original

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError) as exc:
            _import_backend_module("earthlens.gee", "gee", "gee")
        assert exc.value.__cause__ is original, "the SDK error must stay reachable"

    def test_internal_import_error_is_passed_through(self, monkeypatch):
        """A bug inside the backend must not be reported as a missing extra."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        def _boom(name):
            raise ImportError(
                "cannot import name 'helper' from 'earthlens.gee._helpers'",
                name="earthlens.gee._helpers",
            )

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError) as exc:
            _import_backend_module("earthlens.gee", "gee", "gee")
        message = str(exc.value)
        assert "pip install" not in message, f"misleading install hint: {message}"
        assert "earthlens.gee._helpers" in message, f"real cause lost: {message}"

    def test_hand_rolled_import_error_is_passed_through(self, monkeypatch):
        """An ImportError with no module name keeps its own message."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        def _boom(name):
            raise ImportError("the backend rejected this configuration")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError, match="rejected this configuration"):
            _import_backend_module("earthlens.gee", "gee", "gee")

    def test_missing_sdk_still_gets_the_hint(self, monkeypatch):
        """A genuinely absent third-party SDK keeps the extras hint."""
        from earthlens import earthlens as facade_module
        from earthlens.earthlens import _import_backend_module

        def _boom(name):
            raise ModuleNotFoundError("No module named 'ee'", name="ee")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError, match=r"pip install earthlens\[gee\]"):
            _import_backend_module("earthlens.gee", "gee", "gee")

    def test_registry_and_catalog_share_the_wording(self, monkeypatch):
        """Both entry points produce the same body, differing only in subject."""
        from earthlens import earthlens as facade_module

        def _boom(name):
            raise ModuleNotFoundError("No module named 'ee'", name="ee")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError) as registry_exc:
            EarthLens.DataSources["gee"]
        with pytest.raises(ImportError) as catalog_exc:
            EarthLens.catalog("gee")
        tail = "its runtime dependency is not installed. Install with `pip install earthlens[gee]`."
        assert str(registry_exc.value).endswith(tail), f"got {registry_exc.value}"
        assert str(catalog_exc.value).endswith(tail), f"got {catalog_exc.value}"


@pytest.mark.chc
class TestFacadeAoi:
    """The facade's aoi= parameter routes to the backend's spatial extent."""

    def _build(self, tmp_path, **kwargs):
        return EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path=str(tmp_path),
            **kwargs,
        ).datasource.space

    def test_aoi_bbox_sets_spatial_extent(self, tmp_path):
        """A bbox aoi= populates the backend's SpatialExtent edges."""
        space = self._build(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64])
        assert (space.south, space.north, space.west, space.east) == (
            4.19,
            4.64,
            -75.65,
            -74.73,
        )

    def test_aoi_matches_legacy_lat_lon_pairs(self, tmp_path):
        """aoi= and the legacy lat_lim/lon_lim pair yield the same extent."""
        via_aoi = self._build(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64])
        via_pairs = self._build(
            tmp_path, lat_lim=[4.19, 4.64], lon_lim=[-75.65, -74.73]
        )
        assert via_aoi == via_pairs

    def test_aoi_point_with_buffer(self, tmp_path):
        """A point aoi= with buffer builds a square extent."""
        space = self._build(tmp_path, aoi=(-75.0, 4.0), buffer=0.25)
        assert (space.south, space.north, space.west, space.east) == (
            3.75,
            4.25,
            -75.25,
            -74.75,
        )

    def test_aoi_polygon_attaches_mask(self, tmp_path):
        """A polygon aoi= attaches a GeoDataFrame mask to the backend extent."""
        gpd = pytest.importorskip("geopandas")
        shapely = pytest.importorskip("shapely")
        poly = shapely.geometry.Polygon([(-75, 4), (-74, 4), (-74.5, 5)])
        gdf = gpd.GeoDataFrame(geometry=[poly], crs="EPSG:4326")
        space = self._build(tmp_path, aoi=gdf)
        assert space.geometry is not None, "polygon aoi should attach a mask"
        assert (space.west, space.east) == (-75.0, -74.0), "bbox envelope wrong"

    def test_aoi_bbox_attaches_no_mask(self, tmp_path):
        """A bbox aoi= leaves the extent's geometry as None."""
        space = self._build(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64])
        assert space.geometry is None, f"bbox should attach no mask: {space.geometry!r}"

    def test_aoi_with_lat_lim_raises(self, tmp_path):
        """Passing both aoi= and lat_lim= is rejected."""
        with pytest.raises(ValueError, match="either aoi= or lat_lim"):
            self._build(tmp_path, aoi=[-75.65, 4.19, -74.73, 4.64], lat_lim=[4, 5])

    def test_buffer_without_aoi_raises(self, tmp_path):
        """buffer= without a point aoi= is rejected."""
        with pytest.raises(ValueError, match="buffer= only applies"):
            self._build(tmp_path, buffer=0.5)

    def test_accepts_date_objects(self, tmp_path):
        """The facade threads date objects through to the backend window."""
        import datetime as dt

        space_time = EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start=dt.date(2009, 1, 1),
            end=dt.date(2009, 1, 2),
            path=str(tmp_path),
        ).datasource.time
        assert space_time.start_date == dt.datetime(2009, 1, 1)
        assert space_time.end_date == dt.datetime(2009, 1, 2)

    def test_native_aoi_backend_rejects_buffer(self):
        """A backend that owns aoi= (WorldPop) rejects buffer= up front."""
        pytest.importorskip("earthlens.worldpop")
        with pytest.raises(ValueError, match="buffer= is not supported"):
            EarthLens(
                data_source="worldpop",
                variables=["population"],
                start="2020-01-01",
                end="2020-12-31",
                aoi="USA",
                buffer=0.5,
            )


class TestQualifiedKeyDefaultDir:
    """The default output directory derived from a `source:topic` facade key."""

    def test_qualified_key_flattens_its_separator(self):
        """A source:topic key becomes one directory name, colon flattened."""
        assert _source_dirname("jrc:sea-level-forecast") == "jrc_sea-level-forecast"

    def test_bare_key_is_unchanged(self):
        """A bare source key already names a directory, so it passes through."""
        assert _source_dirname("chc") == "chc"

    def test_no_registered_key_keeps_a_colon(self):
        """No registered key may leave a colon in its directory name."""
        # A colon is legal in a POSIX filename, so Linux CI would happily accept
        # the unflattened name. Asserting on the derived string keeps this a real
        # gate on every platform rather than a Windows-only one.
        qualified = sorted(k for k in discover_backends() if ":" in k)
        assert qualified, "expected the registry to carry source:topic keys"
        offenders = [k for k in qualified if ":" in _source_dirname(k)]
        assert not offenders, f"colon survives into the directory name: {offenders}"

    def test_registered_keys_map_to_distinct_directories(self):
        """Flattening must not land two facade keys in one output directory."""
        # `load()` cleans up the default directory it created, so two keys
        # sharing one would let a download for either delete the other's output.
        seen: dict[str, str] = {}
        for key in sorted(discover_backends()):
            name = _source_dirname(key)
            clash = seen.get(name)
            assert clash is None, (
                f"{key!r} and {clash!r} both derive the directory {name!r}"
            )
            seen[name] = key

    @pytest.mark.jrc
    def test_facade_default_path_is_creatable(self, tmp_path):
        """The facade's derived default directory can actually be created."""
        from earthlens.config import set_output_dir

        set_output_dir(tmp_path)
        try:
            target = pathlib.Path(EarthLens(data_source="jrc:coastal-forecast").path)
            assert ":" not in target.name, f"unusable directory name: {target.name}"
            target.mkdir(parents=True, exist_ok=True)
            assert target.is_dir(), f"{target} was not created"
        finally:
            set_output_dir(None)
