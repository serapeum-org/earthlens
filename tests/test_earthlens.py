from __future__ import annotations

import glob
import os
import shutil
from collections.abc import Mapping
from typing import List
from unittest.mock import MagicMock

import cdsapi
import numpy as np
import pandas as pd
import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.chc import CHIRPS
from earthlens.earthlens import EarthLens
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
        filelist = glob.glob(os.path.join(f"{s3_era5_data_source_output_dir}", f"*.nc"))
        assert len(filelist) == number_downloaded_files
        # delete the files
        try:
            shutil.rmtree(f"{s3_era5_data_source_output_dir}")
        except PermissionError:
            print("the downloaded files could not be deleted")


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
            f"'ecmwf' missing from DataSources keys: " f"{sorted(EarthLens.DataSources)}"
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
        assert (
            ecmwf.root_dir == tmp_path.resolve()
        ), f"root_dir should be the tmp path; got {ecmwf.root_dir}"

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
            f"Expected 2 retrieve calls (one per variable); " f"got {len(retrieved)}"
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
    """Pin the top-level `earthlens` package surface (L2)."""

    def test_earthlens_facade_importable_from_package_root(self):
        """`from earthlens import EarthLens` resolves to the facade class."""
        import earthlens

        assert earthlens.EarthLens is EarthLens, (
            f"Top-level re-export should be the facade class; got "
            f"{earthlens.EarthLens!r}"
        )

    def test_aggregate_symbols_importable_from_package_root(self):
        """`AggregationConfig` and `aggregate_netcdf` resolve at top level."""
        import earthlens

        assert earthlens.AggregationConfig is AggregationConfig, (
            f"Top-level AggregationConfig drift: {earthlens.AggregationConfig!r}"
        )
        assert callable(earthlens.aggregate_netcdf), (
            f"Top-level aggregate_netcdf must be callable; got "
            f"{earthlens.aggregate_netcdf!r}"
        )

    def test_all_lists_only_sdk_free_symbols(self):
        """`__all__` excludes the per-backend classes (each needs an extra)."""
        import earthlens

        assert sorted(earthlens.__all__) == [
            "AggregationConfig",
            "EarthLens",
            "aggregate_netcdf",
            "download",
        ], f"Unexpected top-level __all__: {earthlens.__all__!r}"


class TestFunctionalDownload:
    """The one-shot earthlens.download() entry point."""

    def test_download_is_exported(self):
        """earthlens.download is a callable on the package surface."""
        import earthlens

        assert callable(earthlens.download), "download should be callable"

    def test_download_delegates_to_facade(self, monkeypatch):
        """download() builds an EarthLens and forwards the run-time args."""
        import earthlens
        from earthlens import earthlens as facade_module

        captured = {}

        class _FakeFacade:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def download(self, **kwargs):
                captured["download"] = kwargs
                return ["written.tif"]

        monkeypatch.setattr(facade_module, "EarthLens", _FakeFacade)
        result = earthlens.download(
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


@pytest.mark.chc
class TestFacadePath:
    """The facade's output-path defaulting."""

    def test_omitted_path_defaults_to_named_subdir(self, tmp_path, monkeypatch):
        """An omitted path writes under ./earthlens-data/<source>/."""
        from pathlib import Path

        monkeypatch.chdir(tmp_path)
        backend = EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
        ).datasource
        expected = Path.cwd() / "earthlens-data" / "chc"
        assert backend.root_dir == expected, f"got {backend.root_dir}"
        assert backend.root_dir.is_dir(), "the default directory should be created"

    def test_empty_path_still_uses_cwd(self, tmp_path, monkeypatch):
        """An explicit path='' opts into the current working directory."""
        from pathlib import Path

        monkeypatch.chdir(tmp_path)
        backend = EarthLens(
            data_source="chc",
            variables=["precipitation"],
            start="2009-01-01",
            end="2009-01-02",
            path="",
        ).datasource
        assert backend.root_dir == Path.cwd(), f"got {backend.root_dir}"


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
        with pytest.raises(TypeError, match="service_account"):
            EarthLens(
                data_source="gee",
                variables={"UCSB-CHG/CHIRPS/DAILY": ["precipitation"]},
                start="2022-01-01",
                end="2022-01-02",
                servce_account="x",
            )


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
            facade.datasource, "_search", lambda: [RemoteProduct(id=str(i)) for i in range(3)]
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
            raise ImportError("no SDK")

        monkeypatch.setattr(facade_module.importlib, "import_module", _boom)
        with pytest.raises(ImportError, match="catalog is unavailable"):
            EarthLens.catalog("gee")


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
