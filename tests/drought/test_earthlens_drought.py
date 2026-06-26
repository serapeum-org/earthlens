"""Facade-registration tests for the drought backend."""

from __future__ import annotations

import pytest

from earthlens.drought import Drought
from earthlens.earthlens import EarthLens


@pytest.mark.parametrize("key", ["drought", "usdm", "edo", "gdo"])
def test_drought_keys_resolve(key):
    """Every drought key + alias resolves to the `Drought` class."""
    assert EarthLens.DataSources[key] is Drought


def test_drought_key_listed_in_registered_sources():
    """The registered key list carries the four drought entries."""
    keys = set(EarthLens.DataSources)
    assert {"drought", "usdm", "edo", "gdo"}.issubset(keys)


def test_facade_routes_usdm_through_drought_with_explicit_dataset():
    """`EarthLens("usdm", dataset="usdm", ...)` resolves to Drought."""
    facade = EarthLens(
        data_source="usdm",
        start="2026-06-23",
        end="2026-06-23",
        variables=[],
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="usdm",
    )
    assert isinstance(facade.datasource, Drought)
    assert facade.datasource._dataset.id == "usdm"
    assert facade.datasource.OUTPUT_KIND == "vector"


@pytest.mark.parametrize("key", ["drought", "usdm", "edo", "gdo"])
def test_facade_aliases_require_explicit_dataset(key):
    """Every drought alias requires an explicit dataset= kwarg (no pre-bind)."""
    with pytest.raises(ValueError, match="needs dataset="):
        EarthLens(
            data_source=key,
            start="2026-06-23",
            end="2026-06-23",
            variables=[],
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
        )


def test_facade_routes_drought_with_explicit_dataset_kwarg(tmp_path):
    """The bare `"drought"` key takes the `dataset=` kwarg verbatim."""
    facade = EarthLens(
        data_source="drought",
        start="2026-06-01",
        end="2026-06-01",
        variables=[],
        lat_lim=[30.0, 40.0],
        lon_lim=[-95.0, -85.0],
        dataset="speibase-12",
        path=str(tmp_path),
    )
    assert isinstance(facade.datasource, Drought)
    assert facade.datasource._dataset.id == "speibase-12"
    assert facade.datasource.OUTPUT_KIND == "raster"


def test_facade_rejects_unknown_dataset_through_drought():
    """An unknown `dataset=` value surfaces the catalog did-you-mean."""
    with pytest.raises(ValueError, match="Did you mean 'usdm'"):
        EarthLens(
            data_source="drought",
            start="2026-06-01",
            end="2026-06-01",
            variables=[],
            lat_lim=[30.0, 40.0],
            lon_lim=[-95.0, -85.0],
            dataset="usdmm",
        )
