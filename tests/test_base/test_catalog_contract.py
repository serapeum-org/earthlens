"""Cross-backend AbstractCatalog contract checks.

Every backend catalog must chain to `super().model_post_init()` so the
base `catalog` field is populated from `get_catalog()`. This guards
against a backend overriding `model_post_init` and silently leaving
`catalog` empty (the H2 regression in
`planning/align/catalog-consistency.md`).
"""

from __future__ import annotations

import importlib

import pytest

#: (module, catalog-class-name) for every backend that ships a catalog.
CATALOG_BACKENDS = [
    ("earthlens.chc.catalog", "Catalog"),
    ("earthlens.cmems.catalog", "Catalog"),
    ("earthlens.earthdata.catalog", "Catalog"),
    ("earthlens.ecmwf.catalog", "Catalog"),
    ("earthlens.eumetsat.catalog", "Catalog"),
    ("earthlens.fdsn.catalog", "Catalog"),
    ("earthlens.firms.catalog", "Catalog"),
    ("earthlens.gdacs.catalog", "Catalog"),
    ("earthlens.gee.catalog", "Catalog"),
    ("earthlens.nwp.catalog", "Catalog"),
    ("earthlens.openaq.catalog", "Catalog"),
    ("earthlens.openeo.catalog", "Catalog"),
    ("earthlens.overture.catalog", "Catalog"),
    ("earthlens.radar.catalog", "StationCatalog"),
    ("earthlens.sentinel_hub.catalog", "Catalog"),
    ("earthlens.stac.catalog", "Catalog"),
    ("earthlens.tropycal.catalog", "Catalog"),
    ("earthlens.usgs_water.catalog", "Catalog"),
]


def _build(module_name: str, class_name: str):
    """Import and instantiate a backend's catalog from the bundled YAML."""
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_field_is_populated(module_name: str, class_name: str):
    """The base `catalog` field is non-empty after construction."""
    cat = _build(module_name, class_name)
    assert len(cat.catalog) > 0


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_field_mirrors_get_catalog(module_name: str, class_name: str):
    """`super().model_post_init` set `catalog` to the `get_catalog()` result."""
    cat = _build(module_name, class_name)
    assert cat.catalog is cat.get_catalog()


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_dict_surface_matches_datasets(module_name: str, class_name: str):
    """The inherited len/in/iter surface is backed by the populated datasets."""
    cat = _build(module_name, class_name)
    assert len(cat) == len(cat.datasets)
    assert set(cat) == set(cat.datasets)
    first = next(iter(cat.datasets))
    assert first in cat


#: Collection backends that keep a domain `available_collections` index and
#: mirror it into the base `available_datasets` field. `"list"` backends carry
#: a flat list; `"dict"` (stac) carries a per-endpoint dict that is flattened.
COLLECTION_INDEX_BACKENDS = [
    ("earthlens.openeo.catalog", "list"),
    ("earthlens.sentinel_hub.catalog", "list"),
    ("earthlens.stac.catalog", "dict"),
]


@pytest.mark.parametrize("module_name, shape", COLLECTION_INDEX_BACKENDS)
def test_available_datasets_mirrors_collection_index(module_name: str, shape: str):
    """The base available_datasets field mirrors the domain collection index."""
    cat = _build(module_name, "Catalog")
    assert cat.available_datasets
    if shape == "list":
        assert set(cat.available_datasets) == set(cat.available_collections)
    else:
        flat = {cid for ids in cat.available_collections.values() for cid in ids}
        assert set(cat.available_datasets) == flat


#: Backends that populate the base `providers` field (some via a domain alias:
#: earthdata mirrors `daacs`, stac mirrors `endpoints`, fdsn mirrors `datasets`).
PROVIDER_BACKENDS = [
    "earthlens.ecmwf.catalog",
    "earthlens.gee.catalog",
    "earthlens.earthdata.catalog",
    "earthlens.stac.catalog",
    "earthlens.fdsn.catalog",
]


@pytest.mark.parametrize("module_name", PROVIDER_BACKENDS)
def test_providers_populated_and_resolvable(module_name: str):
    """The base providers field is non-empty and get_provider() resolves a slug."""
    cat = _build(module_name, "Catalog")
    assert cat.providers
    slug = next(iter(cat.providers))
    assert cat.get_provider(slug) is cat.providers[slug]
