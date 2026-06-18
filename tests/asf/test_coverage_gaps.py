"""Round out coverage on the small unhit branches in `earthlens.asf`."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.asf import ASF, Catalog, Product
from earthlens.asf.catalog import _load_catalog_data


@pytest.mark.asf
@pytest.mark.unit
def test_empty_variables_list_rejected(tmp_path: Path) -> None:
    """`variables=[]` raises with the explicit empty-list message."""
    with pytest.raises(ValueError, match="empty list"):
        ASF(
            start="2024-01-01",
            end="2024-01-31",
            variables=[],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=tmp_path,
        )


@pytest.mark.asf
@pytest.mark.unit
def test_stack_opts_with_only_perpendicular_baseline(
    fake_asf_search, tmp_path: Path
) -> None:
    """A perpendicular window with no temporal window leaves the temporal key off."""
    from tests.asf.conftest import _FakeProduct

    reference = _FakeProduct(sceneName="REF", stack_return=[])
    fake_asf_search.granule_results = [reference]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="REF",
        perpendicular_baseline=(-50.0, 50.0),
        path=tmp_path,
    )
    backend._search()
    opts = reference.stack_calls[0]["opts"]
    assert opts.kwargs == {
        "minBaselinePerp": -50.0,
        "maxBaselinePerp": 50.0,
    }


@pytest.mark.asf
@pytest.mark.unit
def test_stack_opts_with_only_temporal_baseline(
    fake_asf_search, tmp_path: Path
) -> None:
    """A temporal window with no perpendicular window leaves the perp keys off."""
    from tests.asf.conftest import _FakeProduct

    reference = _FakeProduct(sceneName="REF", stack_return=[])
    fake_asf_search.granule_results = [reference]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="REF",
        temporal_baseline=(0, 60),
        path=tmp_path,
    )
    backend._search()
    opts = reference.stack_calls[0]["opts"]
    assert opts.kwargs == {"temporalBaselineDays": "0,60"}


@pytest.mark.asf
@pytest.mark.unit
def test_load_catalog_data_with_missing_file_uses_zero_mtime(
    tmp_path: Path, reset_catalog_cache
) -> None:
    """A missing path falls through to the load_yaml_strict error."""
    missing = tmp_path / "absent.yaml"
    # `load_yaml_strict` raises on a missing file; the cache key uses
    # mtime=0 for the fallback branch and the error propagates from
    # the YAML loader.
    with pytest.raises(Exception):
        _load_catalog_data(missing)


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_malformed_row_is_rejected_with_helpful_message(
    tmp_path: Path, reset_catalog_cache
) -> None:
    """A row that fails `Product` validation surfaces a clear ValueError."""
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text(
        "products:\n"
        "  bad-row:\n"
        "    aliases: []\n"
        "    platform: SENTINEL1\n"
        "    dataset: OPERA_S1\n"  # both set → exactly-one validator fires
        "    product_type: SLC\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bad-row"):
        _load_catalog_data(yaml_path)


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_accepts_products_kwarg_alias() -> None:
    """The model-validator alias accepts the domain-named kwarg."""
    cat = Catalog(
        products={
            "test-slc": Product(
                platform="SENTINEL1", product_type="SLC", stackable=True
            )
        }
    )
    assert "test-slc" in cat.datasets
    assert cat.products is cat.datasets
