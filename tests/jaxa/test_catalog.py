"""Unit tests for the JAXA catalog loader and Dataset model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.jaxa import Catalog, Dataset
from earthlens.jaxa.catalog import clear_catalog_cache


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """Drop the per-`(path, mtime)` cache so tests don't share state."""
    clear_catalog_cache()
    yield
    clear_catalog_cache()


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_row_requires_collection() -> None:
    """A jaxa-earth row without `collection` fails the model validator."""
    with pytest.raises(ValidationError, match="protocol='jaxa-earth' but no"):
        Dataset(key="aw3d30", protocol="jaxa-earth")


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_row_rejects_short_name() -> None:
    """A jaxa-earth row that also sets `short_name` raises."""
    with pytest.raises(ValidationError, match="belongs to the gportal protocol"):
        Dataset(
            key="aw3d30",
            protocol="jaxa-earth",
            collection="JAXA.foo",
            short_name="10003001",
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_row_requires_short_name() -> None:
    """A gportal row without `short_name` fails the model validator."""
    with pytest.raises(ValidationError, match="protocol='gportal' but no"):
        Dataset(key="sgli", protocol="gportal")


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_row_rejects_collection() -> None:
    """A gportal row that also sets `collection` raises."""
    with pytest.raises(ValidationError, match="belongs to the jaxa-earth protocol"):
        Dataset(
            key="sgli",
            protocol="gportal",
            collection="JAXA.foo",
            short_name="10003001",
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_dataset_extra_forbid() -> None:
    """Unknown fields are rejected (catches catalog typos)."""
    with pytest.raises(ValidationError):
        Dataset(
            key="x",
            protocol="jaxa-earth",
            collection="JAXA.foo",
            spel="something",  # type: ignore[call-arg]
        )


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_resolves_canonical_and_aliases() -> None:
    """`resolve` maps canonical and friendly keys to the same canonical key."""
    cat = Catalog()
    assert cat.resolve("aw3d30") == "aw3d30"
    assert cat.resolve("elevation") == "aw3d30"
    assert cat.resolve("dem") == "aw3d30"


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_did_you_mean() -> None:
    """Unknown keys raise a ValueError with a hint."""
    cat = Catalog()
    with pytest.raises(ValueError, match="Did you mean 'aw3d30'"):
        cat.resolve("aw3d3")


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_by_protocol() -> None:
    """`by_protocol` returns rows filtered by their `protocol` discriminator."""
    cat = Catalog()
    jaxa_earth = cat.by_protocol("jaxa-earth")
    gportal = cat.by_protocol("gportal")
    assert "aw3d30" in jaxa_earth
    assert "sgli-l3-nwlr" in gportal
    assert cat.resolve("sgli-l380") == "sgli-l3-nwlr"  # alias still resolves
    assert not (set(jaxa_earth) & set(gportal))


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_get_returns_dataset_row() -> None:
    """`get(alias)` resolves to a Dataset with the right protocol + identifier."""
    cat = Catalog()
    aw3d30 = cat.get("elevation")
    assert aw3d30.protocol == "jaxa-earth"
    assert aw3d30.collection == "JAXA.EORC_ALOS.PRISM_AW3D30.v3.2_global"
    sgli = cat.get("sgli-l380")
    assert sgli.protocol == "gportal"
    assert sgli.short_name == "10003001"


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_dict_surface() -> None:
    """`cat[key]` / `key in cat` / `len(cat)` work via AbstractCatalog."""
    cat = Catalog()
    assert "aw3d30" in cat
    assert cat["aw3d30"].protocol == "jaxa-earth"
    assert len(cat) >= 5


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_dict_surface_accepts_aliases() -> None:
    """`cat[alias]` / `alias in cat` work the same way as `cat.get(alias)`."""
    cat = Catalog()
    assert "elevation" in cat
    assert cat["elevation"].key == "aw3d30"


@pytest.mark.jaxa
@pytest.mark.unit
def test_catalog_getitem_unknown_key_raises_key_error() -> None:
    """`cat[missing]` raises `KeyError` (dict-style), not `ValueError`."""
    cat = Catalog()
    with pytest.raises(KeyError):
        _ = cat["not-a-real-key"]
