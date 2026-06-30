"""Unit tests for `earthlens.glaciers.catalog` (datasets + GTN-G regions)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from earthlens.glaciers.catalog import Catalog, Dataset, Region, clear_catalog_cache

pytestmark = pytest.mark.glaciers


def test_catalog_loads_five_datasets():
    """The bundled catalog exposes the five curated dataset ids."""
    cat = Catalog()
    assert cat.available() == [
        "glims:outlines",
        "rgi:outlines",
        "wgms:front_variation",
        "wgms:mass_balance",
        "wgms:state",
    ]


def test_output_kind_per_source():
    """rgi/glims rows are vector; wgms rows are tabular."""
    cat = Catalog()
    assert cat.get("rgi:outlines").output_kind == "vector"
    assert cat.get("glims:outlines").output_kind == "vector"
    assert cat.get("wgms:mass_balance").output_kind == "tabular"


def test_wgms_rows_carry_table_and_archive():
    """Each WGMS row names its CSV table and FoG archive URL."""
    row = Catalog().get("wgms:front_variation")
    assert row.source == "wgms"
    assert row.table == "front_variation"
    assert row.archive_url.endswith(".zip")


def test_glims_row_carries_wfs_fields():
    """The GLIMS row carries the WFS endpoint and feature-type name."""
    row = Catalog().get("glims:outlines")
    assert row.wfs_url.startswith("https://www.glims.org/geoserver")
    assert row.wfs_typename == "GLIMS:GLIMS_Glacier_Outlines"


def test_regions_table_has_nineteen_regions():
    """The GTN-G region table covers the 19 RGI glacier regions."""
    regions = Catalog().regions
    assert len(regions) == 19
    assert regions["11"].name == "Central Europe"
    assert regions["11"].bboxes[0] == [-6.0, 40.0, 26.0, 50.0]
    assert regions["11"].url.endswith("rgi2000-v7.0-g-11_central_europe.zip")


def test_region_ten_is_antimeridian_split():
    """Region 10 (North Asia) carries two bboxes across the antimeridian."""
    region = Catalog().regions["10"]
    assert len(region.bboxes) == 2


def test_unknown_id_raises_did_you_mean():
    """An unknown but close id raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'rgi:outlines'"):
        Catalog().get("rgi:outline")


def test_every_curated_id_is_in_the_index():
    """Each curated dataset id appears in `_index.yaml`'s available list."""
    cat = Catalog()
    assert set(cat.available()) == set(cat.available_datasets)


def test_dataset_rejects_source_output_kind_mismatch():
    """An rgi/glims row must be vector; a wgms row must be tabular."""
    with pytest.raises(ValueError, match="must be output_kind 'vector'"):
        Dataset(id="rgi:bad", source="rgi", output_kind="tabular")
    with pytest.raises(ValueError, match="must be output_kind 'tabular'"):
        Dataset(id="wgms:bad", source="wgms", output_kind="vector", table="x")


def test_wgms_row_requires_table_and_archive():
    """A tabular WGMS row without table/archive_url fails validation."""
    with pytest.raises(ValueError, match="needs table and archive_url"):
        Dataset(id="wgms:bad", source="wgms", output_kind="tabular")


def test_glims_row_requires_wfs_fields():
    """A GLIMS row without wfs_url/wfs_typename fails validation."""
    with pytest.raises(ValueError, match="needs wfs_url and wfs_typename"):
        Dataset(id="glims:bad", source="glims", output_kind="vector")


def test_region_rejects_bad_bbox():
    """A region bbox that is not a four-tuple is rejected."""
    with pytest.raises(ValueError, match="must be"):
        Region(id="99", name="bad", bboxes=[[1.0, 2.0, 3.0]], url="x")


def test_loader_rejects_duplicate_dataset(tmp_path: Path):
    """A dataset id declared in two catalog files is an error."""
    clear_catalog_cache()
    (tmp_path / "a.yaml").write_text(
        textwrap.dedent(
            """
            datasets:
              rgi:outlines:
                source: rgi
                output_kind: vector
            """
        )
    )
    (tmp_path / "b.yaml").write_text(
        textwrap.dedent(
            """
            datasets:
              rgi:outlines:
                source: rgi
                output_kind: vector
            """
        )
    )
    with pytest.raises(ValueError, match="declared in two catalog files"):
        Catalog.load(tmp_path)
    clear_catalog_cache()


def test_single_file_catalog_path(tmp_path: Path):
    """A single-YAML catalog path is accepted (test monkey-patch shape)."""
    clear_catalog_cache()
    path = tmp_path / "one.yaml"
    path.write_text(
        textwrap.dedent(
            """
            datasets:
              wgms:mass_balance:
                source: wgms
                output_kind: tabular
                table: mass_balance
                archive_url: https://example.org/fog.zip
            """
        )
    )
    cat = Catalog.load(path)
    assert cat.get("wgms:mass_balance").table == "mass_balance"
    clear_catalog_cache()
