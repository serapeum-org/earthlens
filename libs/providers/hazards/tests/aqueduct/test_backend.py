"""Unit tests for the Aqueduct backend (no network — canned shapefile zips)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pyramids.feature.collection import FeatureCollection

from earthlens.aqueduct import Aqueduct, Catalog

pytestmark = pytest.mark.aqueduct


def test_cache_miss_downloads_via_http_client(
    country_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    """On a cache miss the backend downloads the zip through HttpClient."""
    source = (country_cache / Catalog().get("country").zip).read_bytes()
    empty_cache = tmp_path / "empty_cache"

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url: str, dest, **kwargs) -> Path:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(source)
            return Path(dest)

    monkeypatch.setattr("earthlens.aqueduct.backend.HttpClient", _FakeClient)
    fc = Aqueduct(path=tmp_path, cache_dir=empty_cache, return_period=100).download()
    assert (empty_cache / Catalog().get("country").zip).exists()
    assert len(fc) == 3


def _backend(cache: Path, tmp_path: Path, **kwargs) -> Aqueduct:
    """Build a backend pointed at a canned cache dir (no network)."""
    return Aqueduct(path=tmp_path, cache_dir=cache, **kwargs)


def test_download_returns_feature_collection(
    country_cache: Path, tmp_path: Path
) -> None:
    """The default download returns a FeatureCollection of admin units."""
    fc = _backend(country_cache, tmp_path, return_period=100).download()
    assert isinstance(fc, FeatureCollection)
    assert list(fc.columns) == ["unit_id", "unit_name", "rp_100", "geometry"]
    assert fc.crs.to_epsg() == 4326
    assert len(fc) == 3


def test_multiple_return_periods_become_rp_columns(
    country_cache: Path, tmp_path: Path
) -> None:
    """Each requested return period becomes its own rp_<n> column."""
    fc = _backend(country_cache, tmp_path, return_period=[100, 500]).download()
    assert {"rp_100", "rp_500"}.issubset(fc.columns)


def test_default_selects_all_nine_return_periods(
    country_cache: Path, tmp_path: Path
) -> None:
    """Omitting return_period selects all nine flood magnitudes."""
    fc = _backend(country_cache, tmp_path).download()
    rp_cols = [c for c in fc.columns if c.startswith("rp_")]
    assert len(rp_cols) == 9


def test_geometry_false_returns_dataframe(country_cache: Path, tmp_path: Path) -> None:
    """geometry=False returns a geometry-dropped DataFrame."""
    df = _backend(country_cache, tmp_path, geometry=False, return_period=100).download()
    assert isinstance(df, pd.DataFrame)
    assert not isinstance(df, FeatureCollection)
    assert "geometry" not in df.columns


def test_output_kind_tracks_geometry_flag(country_cache: Path, tmp_path: Path) -> None:
    """OUTPUT_KIND is vector with geometry, tabular without."""
    assert _backend(country_cache, tmp_path).OUTPUT_KIND == "vector"
    assert _backend(country_cache, tmp_path, geometry=False).OUTPUT_KIND == "tabular"


def test_country_name_filters_units(country_cache: Path, tmp_path: Path) -> None:
    """A country name keeps only the matching unit, case-insensitively."""
    fc = _backend(
        country_cache, tmp_path, country="alpha", return_period=100
    ).download()
    assert list(fc["unit_name"]) == ["ALPHA"]


def test_bbox_filters_units(country_cache: Path, tmp_path: Path) -> None:
    """A bbox keeps only units intersecting it."""
    fc = _backend(
        country_cache, tmp_path, lat_lim=[-1, 4], lon_lim=[-1, 4], return_period=100
    ).download()
    assert set(fc["unit_name"]) == {"ALPHA", "BETA"}


def test_country_and_bbox_filters_compose(country_cache: Path, tmp_path: Path) -> None:
    """A country and a bbox filter compose (both must hold)."""
    fc = _backend(
        country_cache,
        tmp_path,
        country="ALPHA",
        lat_lim=[-1, 4],
        lon_lim=[-1, 4],
        return_period=100,
    ).download()
    assert list(fc["unit_name"]) == ["ALPHA"]


def test_geometry_false_empty_returns_empty_dataframe(
    country_cache: Path, tmp_path: Path
) -> None:
    """A no-match tabular request returns an empty geometry-less DataFrame."""
    df = _backend(
        country_cache,
        tmp_path,
        country="Nowhere At All",
        return_period=100,
        geometry=False,
    ).download()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0
    assert "geometry" not in df.columns


def test_2030_scenario_selects_future_columns(
    country_cache: Path, tmp_path: Path
) -> None:
    """A 2030 scenario resolves to its future exposure columns."""
    fc = _backend(
        country_cache,
        tmp_path,
        metric="gdp_affected",
        year=2030,
        scenario="ssp2-rcp8p5",
        return_period=250,
    ).download()
    assert "rp_250" in fc.columns
    assert len(fc) == 3


def test_state_level_extracts_nested_bundle(state_cache: Path, tmp_path: Path) -> None:
    """The state level reads its shapefile from the nested bundle zip."""
    fc = _backend(
        state_cache, tmp_path, admin_level="state", return_period=100
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert "rp_100" in fc.columns


def test_written_geopackage_lands_under_path(
    country_cache: Path, tmp_path: Path
) -> None:
    """A vector download writes one GeoPackage under the output path."""
    _backend(country_cache, tmp_path, return_period=100).download()
    written = list(tmp_path.glob("aqueduct_country_*.gpkg"))
    assert len(written) == 1


def test_distinct_return_periods_write_distinct_files(
    country_cache: Path, tmp_path: Path
) -> None:
    """Requests differing only by return period do not overwrite each other."""
    _backend(country_cache, tmp_path, return_period=100).download()
    _backend(country_cache, tmp_path, return_period=500).download()
    assert len(list(tmp_path.glob("aqueduct_country_*.gpkg"))) == 2


def test_download_logs_source_attribution(country_cache: Path, tmp_path: Path) -> None:
    """A download logs the CC-BY attribution + licence."""
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO")
    try:
        _backend(country_cache, tmp_path, return_period=100).download()
    finally:
        logger.remove(sink)
    assert any("World Resources Institute" in message for message in messages)


def test_cached_zip_is_not_redownloaded(country_cache: Path, tmp_path: Path) -> None:
    """A present cache zip is reused without any HTTP call."""
    backend = _backend(country_cache, tmp_path, return_period=100)

    def _boom(*args, **kwargs):
        raise AssertionError("download must not be called on a cache hit")

    from earthlens.base.http import HttpClient

    original = HttpClient.download
    HttpClient.download = _boom  # type: ignore[method-assign]
    try:
        backend.download()
    finally:
        HttpClient.download = original  # type: ignore[method-assign]


def test_empty_result_returns_empty_collection(
    country_cache: Path, tmp_path: Path
) -> None:
    """A filter matching no unit returns an empty FeatureCollection, no crash."""
    fc = _backend(
        country_cache, tmp_path, country="Nowhere At All", return_period=100
    ).download()
    assert isinstance(fc, FeatureCollection)
    assert len(fc) == 0
    assert "rp_100" in fc.columns


def test_empty_result_writes_no_file(country_cache: Path, tmp_path: Path) -> None:
    """An empty vector result writes no GeoPackage (nothing to persist)."""
    _backend(
        country_cache, tmp_path, country="Nowhere At All", return_period=100
    ).download()
    assert not list(tmp_path.glob("aqueduct_*.gpkg"))


def test_unmatched_country_warns(country_cache: Path, tmp_path: Path) -> None:
    """An unmatched country name emits a warning naming the value."""
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        _backend(
            country_cache, tmp_path, country="Sylvania", return_period=100
        ).download()
    finally:
        logger.remove(sink)
    assert any("Sylvania" in message for message in messages)


def test_corrupt_cached_zip_is_redownloaded(
    country_cache: Path, tmp_path: Path, monkeypatch
) -> None:
    """A non-zip file at the cache path is discarded and re-downloaded."""
    source = (country_cache / Catalog().get("country").zip).read_bytes()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / Catalog().get("country").zip).write_bytes(b"not a zip at all")

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def download(self, url: str, dest, **kwargs) -> Path:
            Path(dest).write_bytes(source)
            return Path(dest)

    monkeypatch.setattr("earthlens.aqueduct.backend.HttpClient", _FakeClient)
    fc = Aqueduct(path=tmp_path, cache_dir=cache, return_period=100).download()
    assert len(fc) == 3


def test_coastal_hazard_is_rejected(country_cache: Path, tmp_path: Path) -> None:
    """coastal is part of the locked 2020 product and is rejected."""
    with pytest.raises(ValueError, match="riverine"):
        _backend(country_cache, tmp_path, hazard="coastal")


def test_baseline_scenario_rejected_for_2030(
    country_cache: Path, tmp_path: Path
) -> None:
    """A 2030 scenario is invalid for year 2010."""
    with pytest.raises(ValueError, match="not defined for year"):
        _backend(country_cache, tmp_path, year=2010, scenario="ssp2-rcp8p5")


def test_unknown_metric_rejected(country_cache: Path, tmp_path: Path) -> None:
    """An unknown metric name is rejected up front."""
    with pytest.raises(ValueError, match="metric"):
        _backend(country_cache, tmp_path, metric="deaths")


def test_unknown_admin_level_rejected(country_cache: Path, tmp_path: Path) -> None:
    """An unknown admin level is rejected with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean"):
        _backend(country_cache, tmp_path, admin_level="countries")


def test_unknown_return_period_rejected(country_cache: Path, tmp_path: Path) -> None:
    """A return period outside the shipped nine is rejected."""
    with pytest.raises(ValueError, match="return_period"):
        _backend(country_cache, tmp_path, return_period=7)


def test_aqueduct_source_does_not_import_xarray() -> None:
    """No aqueduct module imports xarray — geometry read is delegated to pyramids."""
    import earthlens.aqueduct as pkg

    package_dir = Path(pkg.__file__).parent
    offenders = [
        path.name
        for path in package_dir.glob("*.py")
        if "import xarray" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
