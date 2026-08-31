"""Parse-cache contract for the single-file catalog backends.

firms, gdacs, openaq, and overture gained the `(path, mtime_ns)` parse
cache + `clear_catalog_cache()` helper in M3 of
the catalog-consistency alignment, matching the fdsn / nwp / radar
loaders. This checks the cache populates on load and clears on demand.
"""

from __future__ import annotations

import importlib

import pytest

CACHED_BACKENDS = [
    "earthlens.firms.catalog",
    "earthlens.gdacs.catalog",
    "earthlens.openaq.catalog",
    "earthlens.overture.catalog",
]


@pytest.mark.parametrize("module_name", CACHED_BACKENDS)
def test_load_populates_then_clears_cache(module_name: str):
    """Catalog.load() fills the module cache; clear_catalog_cache() empties it."""
    module = importlib.import_module(module_name)
    module.clear_catalog_cache()
    assert not module._CATALOG_CACHE
    module.Catalog.load()
    assert module._CATALOG_CACHE, "load() should populate the parse cache"
    module.clear_catalog_cache()
    assert not module._CATALOG_CACHE, "clear_catalog_cache() should empty it"


@pytest.mark.parametrize("module_name", CACHED_BACKENDS)
def test_cached_load_returns_equal_catalog(module_name: str):
    """A second load() (cache hit) yields the same dataset keys."""
    module = importlib.import_module(module_name)
    module.clear_catalog_cache()
    first = module.Catalog.load()
    second = module.Catalog.load()
    assert set(first.datasets) == set(second.datasets)


@pytest.mark.parametrize("module_name", CACHED_BACKENDS)
def test_load_missing_path_raises(module_name: str, tmp_path):
    """load() on a non-existent path raises (exercises the mtime guard)."""
    module = importlib.import_module(module_name)
    module.clear_catalog_cache()
    with pytest.raises((FileNotFoundError, ValueError)):
        module.Catalog.load(tmp_path / "does_not_exist.yaml")


@pytest.mark.parametrize("module_name", CACHED_BACKENDS)
def test_first_load_does_not_alias_cache(module_name: str):
    """Mutating the first-loaded catalog's datasets must not leak into the cache."""
    module = importlib.import_module(module_name)
    module.clear_catalog_cache()
    first = module.Catalog.load()
    first.datasets["__injected__"] = next(iter(first.datasets.values()))
    second = module.Catalog.load()
    assert "__injected__" not in second.datasets
