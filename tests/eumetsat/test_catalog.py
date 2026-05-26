"""Unit tests for the EUMETSAT collection catalog loader."""

from __future__ import annotations

import pytest

from earthlens.eumetsat import Catalog
from earthlens.eumetsat.catalog import (
    DataStoreGroup,
    EumetsatCollection,
    clear_catalog_cache,
)

pytestmark = pytest.mark.eumetsat


@pytest.fixture
def catalog():
    """A freshly-loaded bundled catalog."""
    clear_catalog_cache()
    return Catalog()


def test_curated_collections_loaded(catalog):
    """The bundled catalog merges all per-group files (~30 collections)."""
    assert len(catalog.collections) >= 30


def test_get_collection_resolves_id_and_group(catalog):
    """A friendly key resolves to the real id and its group."""
    col = catalog.get_collection("msg-hrseviri")
    assert col.collection_id == "EO:EUM:DAT:MSG:HRSEVIRI"
    assert col.group is DataStoreGroup.MSG
    assert col.output_kind == "raster"


def test_available_index_covers_every_curated_id(catalog):
    """Every curated collection_id is a member of available_collections."""
    curated = {c.collection_id for c in catalog.collections.values()}
    assert curated <= set(catalog.available_collections)


def test_groups_span_multiple_missions(catalog):
    """The curated set spans several Data Store groups."""
    groups = {c.group for c in catalog.collections.values()}
    assert {
        DataStoreGroup.MSG,
        DataStoreGroup.METOP,
        DataStoreGroup.SENTINEL_3,
    } <= groups


def test_mtg_and_metop_sg_fully_curated(catalog):
    """MTG and Metop-SG are curated in bulk (the whole group, not a sample)."""
    mtg = [c for c in catalog.collections.values() if c.group is DataStoreGroup.MTG]
    sg = [c for c in catalog.collections.values() if c.group is DataStoreGroup.METOP_SG]
    assert len(mtg) >= 27, f"expected the full MTG group, got {len(mtg)}"
    assert len(sg) >= 3, f"expected the full Metop-SG group, got {len(sg)}"


def test_mtg_carries_both_raster_and_vector_kinds(catalog):
    """MTG mixes raster maps and vector products (AMV / LI events) per G1."""
    kinds = {
        c.output_kind
        for c in catalog.collections.values()
        if c.group is DataStoreGroup.MTG
    }
    assert {"raster", "vector"} <= kinds
    assert catalog.get_collection("mtg-amv").output_kind == "vector"


def test_get_collection_unknown_key_did_you_mean(catalog):
    """An unknown key raises ValueError with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'msg-hrseviri'"):
        catalog.get_collection("msg-hrsevir")


def test_resolve_group_match(catalog):
    """resolve() with a matching group returns the row."""
    col = catalog.resolve("s3-olci-l2-wfr", group="Sentinel-3")
    assert col.collection_id == "EO:EUM:DAT:0407"


def test_resolve_group_mismatch_raises(catalog):
    """resolve() with a wrong group raises ValueError."""
    with pytest.raises(ValueError, match="not the requested group"):
        catalog.resolve("msg-hrseviri", group="MTG")


def test_resolve_accepts_enum_group(catalog):
    """resolve() accepts a DataStoreGroup enum as well as a string."""
    col = catalog.resolve("msg-hrseviri", group=DataStoreGroup.MSG)
    assert col.group is DataStoreGroup.MSG


def test_contains_and_iter(catalog):
    """The catalog supports `in` and iteration over curated keys."""
    assert "msg-hrseviri" in catalog
    assert "msg-hrseviri" in list(catalog)


def test_collection_extra_forbidden():
    """An unexpected field on a collection row is rejected."""
    with pytest.raises(Exception):
        EumetsatCollection(collection_id="x", group="MSG", bogus=1)


def test_format_tags_distinguish_native_and_netcdf(catalog):
    """SEVIRI is tagged native; the Sentinel-3 mirror is tagged netcdf."""
    assert catalog.get_collection("msg-hrseviri").format == "native"
    assert catalog.get_collection("s3-olci-l2-wfr").format == "netcdf"


def test_sentinel5p_timeliness_recorded(catalog):
    """The S5P rows record NRT vs reprocessed timeliness; imagery leaves it None."""
    assert catalog.get_collection("s5p-l2-no2").timeliness == "nrt"
    assert catalog.get_collection("s5p-l2-co").timeliness == "nrt"
    assert catalog.get_collection("s5p-l2-o3").timeliness == "nrt"
    assert catalog.get_collection("s5p-l2-ch4").timeliness == "reprocessed"
    assert catalog.get_collection("msg-hrseviri").timeliness is None


def test_every_collection_id_matches_eumetsat_pattern(catalog):
    """Every curated collection_id is a well-formed `EO:EUM:DAT|CM:...` id.

    `DAT` is the Data Store stream; `CM` is the CM SAF climate-monitoring
    stream (e.g. the ASCAT Level 1 climate data records).
    """
    import re

    pattern = re.compile(r"^EO:EUM:(DAT|CM):[\w:.-]+$")
    bad = {
        key: col.collection_id
        for key, col in catalog.collections.items()
        if not pattern.match(col.collection_id)
    }
    assert not bad, f"malformed collection ids: {bad}"


def test_load_from_single_file(tmp_path):
    """Catalog.load() accepts a single YAML file as well as a directory."""
    single = tmp_path / "one.yaml"
    single.write_text(
        "collections:\n"
        "  demo:\n"
        "    collection_id: 'EO:EUM:DAT:DEMO'\n"
        "    group: MSG\n",
        encoding="utf-8",
    )
    clear_catalog_cache()
    cat = Catalog.load(catalog_path=single)
    assert cat.get_collection("demo").collection_id == "EO:EUM:DAT:DEMO"


def test_get_catalog_returns_datasets(catalog):
    """get_catalog() returns the same structural map as `datasets`."""
    assert catalog.get_catalog() is catalog.datasets


def test_nonexistent_catalog_path_raises(tmp_path):
    """Loading a path that is neither a dir nor a file raises ValueError."""
    clear_catalog_cache()
    with pytest.raises(ValueError, match="does not exist"):
        Catalog.load(catalog_path=tmp_path / "missing")


def test_empty_collections_block_rejected(tmp_path):
    """A catalog file with no collections raises ValueError."""
    (tmp_path / "empty.yaml").write_text("collections: {}\n", encoding="utf-8")
    clear_catalog_cache()
    with pytest.raises(ValueError, match="empty 'collections:' block"):
        Catalog.load(catalog_path=tmp_path)


def test_invalid_collection_row_reports_validation_error(tmp_path):
    """A row with a bad group enum surfaces a wrapped validation error."""
    (tmp_path / "bad.yaml").write_text(
        "collections:\n  x:\n    collection_id: 'EO:EUM:DAT:X'\n    group: NOPE\n",
        encoding="utf-8",
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(catalog_path=tmp_path)


def test_duplicate_key_across_files_rejected(tmp_path):
    """A collection key declared in two files raises ValueError."""
    (tmp_path / "a.yaml").write_text(
        "collections:\n  dup:\n    collection_id: 'EO:EUM:DAT:A'\n    group: MSG\n",
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        "collections:\n  dup:\n    collection_id: 'EO:EUM:DAT:B'\n    group: MTG\n",
        encoding="utf-8",
    )
    clear_catalog_cache()
    with pytest.raises(ValueError, match="declared in two catalog files"):
        Catalog.load(catalog_path=tmp_path)
