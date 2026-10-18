"""Tests for `earthlens.gee` — the package's public surface."""

from __future__ import annotations

import importlib
from pathlib import Path

import earthlens.gee as gee_pkg
from earthlens.gee import (
    CATALOG_PATH,
    GEE,
    AuthenticationError,
    Band,
    Cadence,
    Catalog,
    CloudMask,
    CollectionFilter,
    Dataset,
    EarthEngineAuth,
    create_feature,
    create_geometry,
)

_EXPECTED_EXPORTS = {
    # Epic 2 - the client-side raster analysis subpackage. Re-exported here
    # per the epic's per-task DoD; the operations themselves live in
    # `earthlens.gee.analysis`, which keeps its own complete `__all__`.
    "RasterOperation",
    "OperationChain",
    "RasterAnalysis",
    "NormalizedDifference",
    "Ndvi",
    "Ndwi",
    "Ndbi",
    "BandExpression",
    "ALLOWED_FUNCTIONS",
    "Slope",
    "Aspect",
    "Hillshade",
    "FocalMean",
    "FocalStd",
    "FocalMedian",
    "FocalApply",
    "Texture",
    "MEASURES",
    "Proximity",
    "Reclassify",
    "ConvertUnits",
    "ConnectedComponents",
    "Sieve",
    "Cluster",
    "Classify",
    "TrainingExtractor",
    "TrainingSet",
    "UNIT_CONVERSIONS",
    "supported_conversions",
    "PanSharpen",
    "MatchHistogram",
    "TasseledCap",
    # Epic 1 Task 1.1 - the reducer strategy objects and their registry.
    "Reducer",
    "Mean",
    "Median",
    "Min",
    "Max",
    "Mode",
    "Mosaic",
    "Sum",
    "Percentile",
    "REDUCERS",
    "resolve_reducer",
    # Epic 1 Task 1.6 - the promoted routing verdict.
    "EnginePlan",
    "EedaiPlan",
    # Epic 1 Task 1.12 - the output grid value object.
    "OutputGrid",
    # Epic 1 Task 1.9 - the strategy configs.
    "ExportConfig",
    "EngineConfig",
    # Epic 1 Task 1.3 - the engine strategy objects.
    "Engine",
    "EeEngine",
    "EedaiEngine",
    "AutoEngine",
    "ENGINES",
    "resolve_engine",
    # Epic 1 Task 1.4 - the destination strategy objects.
    "Sink",
    "UrlSink",
    "DriveSink",
    "GcsSink",
    "AssetSink",
    "SINKS",
    "resolve_sink",
    # Epic 1 Task 1.5 - the EEDAI reader adapter.
    "EedaiReader",
    # Epic 1 Tasks 1.7/1.8 - the payload and return types.
    "WorkUnit",
    "RasterWorkUnit",
    "FeatureWorkUnit",
    "Artifact",
    "DownloadResult",
    # Epic 1 Task 1.13 - the computePixels engine.
    "PixelsEngine",
    "HIGH_VOLUME_ENDPOINT",
    "GEE",
    "AuthenticationError",
    "CloudMask",
    "CollectionFilter",
    "Catalog",
    "Dataset",
    "Band",
    "Cadence",
    "Extent",
    "Provider",
    "CATALOG_PATH",
    "PROVIDERS_PATH",
    "EarthEngineAuth",
    "create_geometry",
    "create_feature",
    "GeometryConverter",
    "FeatureDownloader",
    "sample_points",
    "Sampler",
    "PointSampler",
    "RegionSampler",
    "RandomSampler",
    "StratifiedSampler",
    "ZonalStrategy",
    "zonal_stats",
    "feature_collection_to_dataframe",
    "feature_collection_to_gdf",
    "feature_collections_to_dataframe",
    "sample_points_to_gdf",
    "TaskInfo",
    "list_recent_tasks",
    "get_task_status",
    "cancel_task",
    "wait_for_task_id",
    "resolve_destination",
}


class TestPublicSurface:
    """Tests for the names re-exported from `earthlens.gee`."""

    def test_all_lists_the_expected_names(self):
        """`__all__` is exactly the documented public surface."""
        assert set(gee_pkg.__all__) == _EXPECTED_EXPORTS, (
            f"__all__ mismatch: {set(gee_pkg.__all__) ^ _EXPECTED_EXPORTS}"
        )

    def test_classes_resolve_to_their_modules(self):
        """The re-exported classes are the canonical ones from their submodules."""
        assert GEE.__module__ == "earthlens.gee.backend"
        assert AuthenticationError.__module__ == "earthlens.gee.auth"
        assert EarthEngineAuth.__module__ == "earthlens.gee.auth"
        for cls in (Catalog, Dataset, Band, Cadence):
            assert cls.__module__ == "earthlens.gee.catalog", cls

    def test_type_aliases_are_reexported_from_backend(self):
        """`CloudMask` / `CollectionFilter` are the backend's callable aliases."""
        from earthlens.gee import backend

        assert CloudMask is backend.CloudMask
        assert CollectionFilter is backend.CollectionFilter

    def test_catalog_path_points_at_the_bundled_catalog_dir(self):
        """`CATALOG_PATH` is the bundled `catalog/` directory and exists."""
        assert isinstance(CATALOG_PATH, Path)
        assert CATALOG_PATH.name == "catalog"
        assert CATALOG_PATH.is_dir()
        assert (CATALOG_PATH / "_index.yaml").is_file()

    def test_feature_helpers_are_callable(self):
        """`create_geometry` / `create_feature` are importable callables."""
        assert (
            callable(create_geometry) and create_geometry.__name__ == "create_geometry"
        )
        assert callable(create_feature) and create_feature.__name__ == "create_feature"

    def test_catalog_usable_from_package_root(self):
        """`Catalog` works when imported from `earthlens.gee` directly."""
        assert (
            Catalog().get_dataset("USGS/SRTMGL1_003").title
            == "NASA SRTM Digital Elevation 30m"
        )

    def test_module_has_a_docstring(self):
        """The package has a module docstring describing the backend."""
        doc = importlib.import_module("earthlens.gee").__doc__ or ""
        assert "Earth Engine" in doc and len(doc) > 100

    def test_every_public_name_is_documented(self):
        """Each `__all__` entry is named in the module docstring.

        Epic 1 added nine modules' worth of public symbols to `__all__` while
        the docstring's surface list went unchanged, so the whole object API
        was importable but absent from the package's own front door. Nothing
        caught that, which is why this holds the two together.
        """
        package = importlib.import_module("earthlens.gee")
        doc = package.__doc__ or ""
        undocumented = [name for name in package.__all__ if f"`{name}`" not in doc]
        assert undocumented == [], (
            f"in __all__ but not in the docstring: {undocumented}"
        )
