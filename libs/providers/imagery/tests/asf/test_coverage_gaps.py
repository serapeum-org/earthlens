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
def test_stack_opts_passes_empty_options_regardless_of_perp_window(
    fake_asf_search, tmp_path: Path
) -> None:
    """The SDK opts are always empty; perpendicular filtering happens client-side."""
    from .conftest import _FakeProduct

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
    assert opts.kwargs == {}


@pytest.mark.asf
@pytest.mark.unit
def test_stack_opts_passes_empty_options_regardless_of_temporal_window(
    fake_asf_search, tmp_path: Path
) -> None:
    """The SDK opts are always empty; temporal filtering happens client-side."""
    from .conftest import _FakeProduct

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
    assert opts.kwargs == {}


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


@pytest.mark.asf
@pytest.mark.unit
def test_download_in_stack_mode_returns_path_list(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """Stack-mode `download()` returns paths and respects idempotency, like search mode."""
    from .conftest import _FakeProduct

    stacked = [
        _FakeProduct(
            sceneName="S1A_REF",
            perpendicularBaseline=0.0,
            temporalBaseline=0,
        ),
        _FakeProduct(
            sceneName="S1A_SEC",
            perpendicularBaseline=12.0,
            temporalBaseline=6,
        ),
    ]
    reference = _FakeProduct(sceneName="S1A_REF", stack_return=stacked)
    fake_asf_search.granule_results = [reference]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="S1A_REF",
        perpendicular_baseline=(-100.0, 100.0),
        temporal_baseline=(0, 30),
        path=tmp_path,
    )
    paths = backend.download()
    assert {p.name for p in paths} == {"S1A_REF.zip", "S1A_SEC.zip"}
    assert all(p.exists() for p in paths)


@pytest.mark.asf
@pytest.mark.unit
def test_download_accepts_progress_bar_flag(
    fake_asf_search, fake_earthdata_auth, tmp_path: Path
) -> None:
    """The `progress_bar=` flag is documented as accepted; both values must not crash."""
    from .conftest import _FakeProduct

    fake_asf_search.search_results = [_FakeProduct(sceneName="S1A_X")]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    paths = backend.download(progress_bar=False)
    assert len(paths) == 1
    # Second call with the opposite flag is also fine (idempotent skip).
    paths2 = backend.download(progress_bar=True)
    assert paths == paths2


@pytest.mark.asf
@pytest.mark.unit
def test_catalog_load_with_none_path_uses_default(reset_catalog_cache) -> None:
    """Catalog.load(None) falls back to the bundled CATALOG_PATH."""
    cat = Catalog.load(catalog_path=None)
    assert "sentinel-1-slc" in cat.datasets
    assert len(cat.datasets) >= 1


@pytest.mark.asf
@pytest.mark.unit
def test_in_window_helper_directly() -> None:
    """`_in_window` is the bound check apply_baseline_windows delegates to."""
    from earthlens.asf._helpers import _in_window

    # No window → wildcard
    assert _in_window(42.0, None) is True
    assert _in_window(None, None) is True
    # In-window
    assert _in_window(0.0, (-100.0, 100.0)) is True
    assert _in_window(-100.0, (-100.0, 100.0)) is True  # inclusive low
    assert _in_window(100.0, (-100.0, 100.0)) is True  # inclusive high
    # Out-of-window
    assert _in_window(-200.0, (-100.0, 100.0)) is False
    assert _in_window(200.0, (-100.0, 100.0)) is False
    # Missing value with non-None window fails
    assert _in_window(None, (-100.0, 100.0)) is False


@pytest.mark.asf
@pytest.mark.unit
def test_stack_mode_metadata_carries_baseline_keys(
    fake_asf_search, tmp_path: Path
) -> None:
    """Stack-mode RemoteProduct.metadata includes the two baseline keys (search-mode omits)."""
    from .conftest import _FakeProduct

    stacked = [
        _FakeProduct(
            sceneName="S1A_REF",
            perpendicularBaseline=0.0,
            temporalBaseline=0,
        ),
        _FakeProduct(
            sceneName="S1A_SEC",
            perpendicularBaseline=42.0,
            temporalBaseline=12,
        ),
    ]
    reference = _FakeProduct(sceneName="S1A_REF", stack_return=stacked)
    fake_asf_search.granule_results = [reference]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="S1A_REF",
        path=tmp_path,
    )
    products = backend._search()
    assert len(products) == 2
    for remote in products:
        assert "perpendicularBaseline" in remote.metadata
        assert "temporalBaseline" in remote.metadata
    assert products[1].metadata["perpendicularBaseline"] == 42.0
    assert products[1].metadata["temporalBaseline"] == 12
