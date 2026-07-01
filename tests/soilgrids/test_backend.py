"""Unit tests for the SoilGrids backend (faked pyramids Dataset.from_wcs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.soilgrids import SoilGrids
from earthlens.soilgrids._helpers import IGH_PROJ4
from earthlens.soilgrids.backend import _close_dataset

from .conftest import FakeDataset

pytestmark = pytest.mark.soilgrids

LAT = [51.0, 52.0]
LON = [5.0, 6.0]
STD_DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]

#: A sentinel polygon mask — the faked crop only records it, never reads it.
_GEOMETRY = object()


class _BoomDataset:
    """A fake dataset whose close() raises, to exercise the best-effort guard."""

    def close(self) -> None:
        raise RuntimeError("gdal handle already gone")


def _backend(tmp_path: Path, variables: list[str], **kwargs) -> SoilGrids:
    """Build a SoilGrids over the standard AOI writing into tmp_path."""
    return SoilGrids(
        variables=variables, lat_lim=LAT, lon_lim=LON, path=str(tmp_path), **kwargs
    )


def test_default_request_fetches_every_depth_at_mean(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """variables=['clay'] fetches the six standard depths at the mean layer."""
    paths = _backend(tmp_path, ["clay"]).download()
    assert paths == [tmp_path / f"clay_{depth}_mean.tif" for depth in STD_DEPTHS]
    assert len(fake_from_wcs.recorder) == 6


def test_from_wcs_receives_the_pinned_arguments(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """Each from_wcs call gets the coverage id, bbox, endpoint, and CRS shim."""
    _backend(tmp_path, ["clay"]).download()
    call = fake_from_wcs.recorder[0]
    assert call["coverage"] == "clay_0-5cm_mean"
    assert call["endpoint"] == "https://maps.isric.org/mapserv?map=/map/clay.map"
    assert call["bbox"] == (5.0, 51.0, 6.0, 52.0)
    assert call["crs"] == "EPSG:4326"
    assert call["coverage_crs"] == IGH_PROJ4
    assert call["output_crs"] == "EPSG:4326"
    assert call["resolution"] is None
    assert call["output"].endswith("clay_0-5cm_mean.tif")


def test_quantiles_double_the_fetched_cells(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """Two quantiles fetch twice as many coverages as the single-mean default."""
    paths = _backend(tmp_path, ["clay"], quantiles=["Q0.05", "Q0.95"]).download()
    assert len(paths) == len(STD_DEPTHS) * 2
    covers = {c["coverage"] for c in fake_from_wcs.recorder}
    assert "clay_0-5cm_Q0.05" in covers
    assert "clay_0-5cm_Q0.95" in covers


def test_explicit_depths_restrict_the_fetch(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """Explicit depths limit the fetch to just those cells."""
    paths = _backend(tmp_path, ["phh2o"], depths=["0-5cm"]).download()
    assert paths == [tmp_path / "phh2o_0-5cm_mean.tif"]


def test_output_crs_and_resolution_are_forwarded(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """A native-CRS request forwards output_crs=None and a scalar resolution."""
    _backend(
        tmp_path, ["clay"], depths=["0-5cm"], output_crs=None, resolution=250.0
    ).download()
    call = fake_from_wcs.recorder[0]
    assert call["output_crs"] is None
    assert call["resolution"] == 250.0


def test_coverage_crs_override_is_used(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """A custom coverage_crs overrides the default IGH shim."""
    _backend(tmp_path, ["clay"], depths=["0-5cm"], coverage_crs="EPSG:4326").download()
    assert fake_from_wcs.recorder[0]["coverage_crs"] == "EPSG:4326"


def test_custom_output_crs_is_forwarded(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """A reprojecting output_crs is threaded through to from_wcs."""
    _backend(tmp_path, ["clay"], depths=["0-5cm"], output_crs="EPSG:3857").download()
    assert fake_from_wcs.recorder[0]["output_crs"] == "EPSG:3857"


def test_multiple_properties_fetch_every_cell(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """Two properties at their defaults fetch all 12 (2 x 6 depths) coverages."""
    paths = _backend(tmp_path, ["clay", "phh2o"]).download()
    assert len(paths) == len(STD_DEPTHS) * 2
    covers = {c["coverage"] for c in fake_from_wcs.recorder}
    assert "clay_0-5cm_mean" in covers
    assert "phh2o_100-200cm_mean" in covers


def test_polygon_aoi_masks_the_result(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """A polygon aoi= masks the fetched coverage instead of writing bbox-only."""
    backend = _backend(tmp_path, ["clay"], depths=["0-5cm"])
    backend._attach_clip_geometry(_GEOMETRY)
    paths = backend.download()
    assert paths == [tmp_path / "clay_0-5cm_mean.tif"]
    assert fake_from_wcs.recorder[0]["output"] is None
    assert fake_from_wcs.masks == [_GEOMETRY]
    assert fake_from_wcs.written == [str(tmp_path / "clay_0-5cm_mean.tif")]


def test_bbox_only_path_skips_the_mask(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """With no polygon aoi= the coverage is written straight from from_wcs."""
    _backend(tmp_path, ["clay"], depths=["0-5cm"]).download()
    assert fake_from_wcs.recorder[0]["output"].endswith("clay_0-5cm_mean.tif")
    assert fake_from_wcs.masks == []


def test_progress_flag_is_wired(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """download(progress_bar=...) is recorded and drives the per-coverage bar."""
    backend = _backend(tmp_path, ["clay"], depths=["0-5cm"])
    backend.download(progress_bar=False)
    assert backend._show_progress is False


def test_one_failed_coverage_is_skipped_not_fatal(
    fake_from_wcs: type[FakeDataset], info_log: list[str], tmp_path: Path
) -> None:
    """A single failed coverage is logged and skipped; the rest still land."""
    fake_from_wcs.fail_coverages = {"clay_5-15cm_mean"}
    paths = _backend(tmp_path, ["clay"]).download()
    assert len(paths) == len(STD_DEPTHS) - 1
    assert tmp_path / "clay_5-15cm_mean.tif" not in paths
    assert any("clay_5-15cm_mean failed" in m for m in info_log)


def test_all_coverages_failing_raises(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """When every coverage fails, download raises rather than returning []."""
    fake_from_wcs.fail_coverages = {f"clay_{d}_mean" for d in STD_DEPTHS}
    with pytest.raises(RuntimeError, match="all 6 requested coverage"):
        _backend(tmp_path, ["clay"]).download()


def test_partial_write_is_cleaned_up(
    fake_from_wcs: type[FakeDataset], tmp_path: Path
) -> None:
    """A coverage that fails after writing leaves no partial .tif behind."""
    fake_from_wcs.fail_after_write = {"clay_5-15cm_mean"}
    paths = _backend(tmp_path, ["clay"]).download()
    assert len(paths) == len(STD_DEPTHS) - 1
    assert not (tmp_path / "clay_5-15cm_mean.tif").exists()


def test_empty_plan_returns_no_paths(
    fake_from_wcs: type[FakeDataset], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty search plan short-circuits to no fetches and no paths."""
    backend = _backend(tmp_path, ["clay"], depths=["0-5cm"])
    monkeypatch.setattr(backend, "_search", list)
    assert backend.download() == []
    assert fake_from_wcs.recorder == []


def test_empty_depths_list_rejected(tmp_path: Path) -> None:
    """An explicitly-empty depths=[] is rejected (it selects no coverages)."""
    with pytest.raises(ValueError, match=r"depths=\[\] selects no coverages"):
        _backend(tmp_path, ["clay"], depths=[])


def test_empty_quantiles_list_rejected(tmp_path: Path) -> None:
    """An explicitly-empty quantiles=[] is rejected (it selects no coverages)."""
    with pytest.raises(ValueError, match=r"quantiles=\[\] selects no coverages"):
        _backend(tmp_path, ["clay"], quantiles=[])


def test_search_plans_without_network(tmp_path: Path) -> None:
    """_search returns one product per coverage triple with no network call."""
    plan = _backend(tmp_path, ["ocs"])._search()
    assert [p.id for p in plan] == ["ocs_0-30cm_mean"]
    assert plan[0].href.endswith("ocs.map")
    assert plan[0].metadata["property"] == "ocs"


def test_aggregate_is_rejected(tmp_path: Path) -> None:
    """download(aggregate=...) raises NotImplementedError naming the cause."""
    with pytest.raises(NotImplementedError, match="static"):
        _backend(tmp_path, ["clay"]).download(aggregate=object())


def test_attribution_logged_once(
    fake_from_wcs: type[FakeDataset], info_log: list[str], tmp_path: Path
) -> None:
    """The CC-BY attribution is logged exactly once per download."""
    _backend(tmp_path, ["clay", "sand"]).download()
    attribution = [m for m in info_log if m.startswith("soilgrids attribution:")]
    assert len(attribution) == 1


def test_missing_bbox_raises(tmp_path: Path) -> None:
    """A request without a bounding box raises ValueError."""
    with pytest.raises(ValueError, match="bounding box"):
        SoilGrids(variables=["clay"], path=str(tmp_path))


def test_empty_variables_raises(tmp_path: Path) -> None:
    """An empty variables list raises ValueError."""
    with pytest.raises(ValueError, match="property id"):
        _backend(tmp_path, [])


def test_mapping_variables_raises_type_error(tmp_path: Path) -> None:
    """A mapping variables= raises TypeError (properties are named by id)."""
    with pytest.raises(TypeError, match="must be a list"):
        SoilGrids(variables={"clay": []}, lat_lim=LAT, lon_lim=LON, path=str(tmp_path))


def test_unknown_property_raises_at_construction(tmp_path: Path) -> None:
    """An unknown property id fails at construction with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'clay'"):
        _backend(tmp_path, ["clayy"])


def test_output_kind_is_raster(tmp_path: Path) -> None:
    """The backend declares OUTPUT_KIND raster (so the facade gates aggregate)."""
    assert _backend(tmp_path, ["clay"]).OUTPUT_KIND == "raster"


def test_close_dataset_ignores_missing_close() -> None:
    """_close_dataset is a no-op when the object exposes no close method."""
    _close_dataset(object())


def test_close_dataset_swallows_close_errors() -> None:
    """_close_dataset swallows an exception raised by the underlying close."""
    _close_dataset(_BoomDataset())


def test_src_has_no_owslib_or_xarray_import() -> None:
    """The soilgrids src imports neither the OGC WCS SDK nor an array library."""
    import earthlens.soilgrids as pkg

    src_dir = Path(pkg.__file__).parent
    for py_file in src_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "owslib" not in stripped, f"{py_file.name}: {stripped}"
                assert "xarray" not in stripped, f"{py_file.name}: {stripped}"
