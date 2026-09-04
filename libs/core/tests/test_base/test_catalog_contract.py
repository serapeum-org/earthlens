"""Cross-backend AbstractCatalog contract checks.

Every backend catalog must chain to `super().model_post_init()` so the
base `catalog` field is populated from `get_catalog()`. This guards
against a backend overriding `model_post_init` and silently leaving
`catalog` empty (the H2 regression in
the catalog-consistency alignment).
"""

from __future__ import annotations

import importlib

import pytest

from earthlens._backends import discover_backends
from earthlens.base import AbstractCatalog

#: The class names a backend's `catalog` module may expose, in preference order.
CATALOG_CLASS_NAMES = ("Catalog", "StationCatalog")


def _discover_catalogs() -> list[tuple[str, str]]:
    """Find `(module, class-name)` for every backend that ships a catalog.

    Discovered from the backend registry rather than listed by hand. A
    hardcoded list only covers the backends someone remembered to add, and the
    default `get_catalog()` fails *open* — a catalog that kept its rows
    somewhere other than `datasets` would return an empty mapping rather than
    raise, so the contract has to be checked on all of them, not a sample.

    Returns:
        list[tuple[str, str]]: Sorted `(module name, class name)` pairs.
    """
    pairs = []
    for package in {entry[0] for entry in discover_backends().values()}:
        module_name = f"{package}.catalog"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for name in CATALOG_CLASS_NAMES:
            if hasattr(module, name):
                pairs.append((module_name, name))
                break
    return sorted(pairs)


#: (module, catalog-class-name) for every backend that ships a catalog.
CATALOG_BACKENDS = _discover_catalogs()


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
def test_catalog_mirrors_get_catalog(module_name: str, class_name: str):
    """`catalog` reads back exactly what `get_catalog()` returns."""
    cat = _build(module_name, class_name)
    assert dict(cat.catalog) == dict(cat.get_catalog())


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_is_a_read_only_view(module_name: str, class_name: str):
    """Writing through `catalog` fails instead of rewriting `datasets`."""
    cat = _build(module_name, class_name)
    key = next(iter(cat.datasets))
    with pytest.raises(TypeError):
        cat.catalog[key] = None
    assert cat.datasets[key] is not None, "datasets must be untouched"


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_dict_surface_matches_datasets(module_name: str, class_name: str):
    """The inherited len/in/iter surface is backed by the populated datasets."""
    cat = _build(module_name, class_name)
    assert len(cat) == len(cat.datasets)
    assert set(cat) == set(cat.datasets)
    first = next(iter(cat.datasets))
    assert first in cat


def test_every_backend_ships_a_discoverable_catalog():
    """Discovery must find one per backend package, not a subset.

    If this drops, a backend's catalog stopped being importable or renamed its
    class, and every contract check below silently stopped covering it.
    """
    packages = {entry[0] for entry in discover_backends().values()}
    assert len(CATALOG_BACKENDS) == len(packages), (
        f"{len(packages)} backend packages but {len(CATALOG_BACKENDS)} catalogs "
        f"discovered"
    )


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_get_catalog_is_not_silently_empty(module_name: str, class_name: str):
    """The inherited default returns `datasets`, which must actually hold rows.

    The base implementation cannot tell "this catalog has no rows" from "this
    catalog keeps its rows elsewhere", so it answers `{}` for both. Asserting
    non-empty across every backend is what turns that fail-open into a failure.
    """
    cat = _build(module_name, class_name)
    assert len(cat.get_catalog()) > 0, (
        f"{module_name}.{class_name}.get_catalog() is empty; if the rows live "
        f"somewhere other than `datasets`, override get_catalog()"
    )


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


#: Backends with no resolve step inherit the base stub (raises).
NO_RESOLVE_BACKENDS = [
    "earthlens.firms.catalog",
    "earthlens.gdacs.catalog",
    "earthlens.cmems.catalog",
]
#: Backends that override resolve() (signatures vary by backend need).
RESOLVE_BACKENDS = [
    "earthlens.ghsl.catalog",
    "earthlens.nwp.catalog",
    "earthlens.usgs_water.catalog",
    "earthlens.stac.catalog",
    "earthlens.openeo.catalog",
    "earthlens.sentinel_hub.catalog",
    "earthlens.earthdata.catalog",
    "earthlens.eumetsat.catalog",
]


@pytest.mark.parametrize("module_name", NO_RESOLVE_BACKENDS)
def test_resolve_default_raises(module_name: str):
    """A backend with no resolve step inherits the raising base stub."""
    cat = _build(module_name, "Catalog")
    with pytest.raises(NotImplementedError):
        cat.resolve("anything")


@pytest.mark.parametrize("module_name", RESOLVE_BACKENDS)
def test_resolve_is_overridden(module_name: str):
    """Backends with a resolve step override the base stub."""
    cat = _build(module_name, "Catalog")
    assert type(cat).resolve is not AbstractCatalog.resolve


#: Two-level backends expose the shared 2-arg get_variable(dataset, leaf).
LEAF_BACKENDS = [
    "earthlens.chc.catalog",
    "earthlens.ecmwf.catalog",
    "earthlens.cmems.catalog",
    "earthlens.gee.catalog",
    "earthlens.firms.catalog",
    "earthlens.tropycal.catalog",
]
#: Single-level backends (one row is the leaf) inherit the raising base stub.
NO_LEAF_BACKENDS = [
    "earthlens.fdsn.catalog",
    "earthlens.gdacs.catalog",
    "earthlens.overture.catalog",
]


@pytest.mark.parametrize("module_name", LEAF_BACKENDS)
def test_get_variable_is_two_arg_override(module_name: str):
    """Two-level catalogs override get_variable with the 2-arg contract."""
    cat = _build(module_name, "Catalog")
    assert type(cat).get_variable is not AbstractCatalog.get_variable


@pytest.mark.parametrize("module_name", NO_LEAF_BACKENDS)
def test_get_variable_default_raises(module_name: str):
    """Single-level catalogs inherit the raising base get_variable stub."""
    cat = _build(module_name, "Catalog")
    with pytest.raises(NotImplementedError):
        cat.get_variable("dataset", "leaf")


def test_firms_get_variable_aliases_get_column():
    """firms.get_variable returns the same leaf as get_column."""
    from earthlens.firms import Catalog

    cat = Catalog()
    assert cat.get_variable("MODIS_NRT", "confidence") is cat.get_column(
        "MODIS_NRT", "confidence"
    )


def test_tropycal_get_variable_aliases_get_field():
    """tropycal.get_variable returns the same leaf as get_field."""
    from earthlens.tropycal import Catalog

    cat = Catalog()
    assert cat.get_variable("north_atlantic", "mslp") is cat.get_field(
        "north_atlantic", "mslp"
    )


def test_earthdata_providers_is_a_copy_of_daacs():
    """earthdata providers mirrors daacs by content but is a distinct dict."""
    from earthlens.earthdata import Catalog

    cat = Catalog()
    assert cat.providers == cat.daacs
    assert cat.providers is not cat.daacs


def test_stac_providers_is_a_copy_of_endpoints():
    """stac providers mirrors endpoints by content but is a distinct dict."""
    from earthlens.stac import Catalog

    cat = Catalog()
    assert cat.providers == cat.endpoints
    assert cat.providers is not cat.endpoints


#: Each catalog's expected did-you-mean entry noun. Domain catalogs name
#: their entries (parameters/basins/sensors/...); ecmwf keeps the default.
ENTRY_NOUNS = [
    ("earthlens.openaq.catalog", "Catalog", "parameters"),
    ("earthlens.usgs_water.catalog", "Catalog", "parameters"),
    ("earthlens.tropycal.catalog", "Catalog", "basins"),
    ("earthlens.firms.catalog", "Catalog", "sensors"),
    ("earthlens.gdacs.catalog", "Catalog", "hazard types"),
    ("earthlens.radar.catalog", "StationCatalog", "stations"),
    ("earthlens.fdsn.catalog", "Catalog", "networks"),
    ("earthlens.overture.catalog", "Catalog", "themes"),
    ("earthlens.ecmwf.catalog", "Catalog", "datasets"),
]


@pytest.mark.parametrize("module_name, class_name, noun", ENTRY_NOUNS)
def test_did_you_mean_uses_entry_noun(module_name: str, class_name: str, noun: str):
    """The unknown-key error names the catalog's domain entry noun."""
    cat = _build(module_name, class_name)
    with pytest.raises(ValueError, match=f"Known {noun}:"):
        cat.get_dataset("definitely-not-a-key")


@pytest.mark.parametrize("module_name, class_name", CATALOG_BACKENDS)
def test_catalog_rows_are_frozen(module_name: str, class_name: str):
    """Every row model rejects attribute assignment.

    The parse cache hands the *same* row objects to every `load()`, so a row
    that allowed assignment would let one caller rewrite the catalog for the
    whole process. `frozen=True` is what makes sharing them safe.
    """
    cat = _build(module_name, class_name)
    if not cat.datasets:
        pytest.skip(f"{module_name} exposes no rows to check")
    row = cat.datasets[next(iter(cat.datasets))]
    if not hasattr(row, "model_config"):
        pytest.skip(f"{module_name} rows are not pydantic models")
    assert row.model_config.get("frozen") is True, (
        f"{module_name}.{type(row).__name__} is not frozen; the shared parse "
        "cache would let one caller mutate every other caller's catalog"
    )
