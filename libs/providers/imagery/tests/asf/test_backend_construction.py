"""Construction-time invariants for `earthlens.asf.ASF`.

These tests cover the C1 acceptance: `ASF(...)` constructs in both
search and stack mode, rejects mode-incompatible inputs with clear
errors, and exposes `OUTPUT_KIND = "raster"`.

`_search` / `_fetch` round-trips live in `test_backend.py` (C6).
"""

from __future__ import annotations

import pytest

from earthlens.asf import ASF


@pytest.mark.asf
@pytest.mark.unit
def test_output_kind_is_raster() -> None:
    """The class-level `OUTPUT_KIND` must be `'raster'`."""
    assert ASF.OUTPUT_KIND == "raster"


@pytest.mark.asf
@pytest.mark.unit
def test_search_mode_constructs(tmp_path) -> None:
    """A search-mode instance with bbox + dates constructs cleanly."""
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[40.0, 41.0],
        lon_lim=[-100.0, -99.0],
        path=tmp_path,
    )
    assert backend._mode == "search"
    assert backend._product_key == "sentinel-1-slc"


@pytest.mark.asf
@pytest.mark.unit
def test_stack_mode_constructs_without_bbox(tmp_path) -> None:
    """Stack mode does not require a bbox — the reference defines it."""
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="S1A_FAKE_SLC",
        path=tmp_path,
    )
    assert backend._mode == "stack"


@pytest.mark.asf
@pytest.mark.unit
def test_stack_mode_rejects_non_stackable_product(tmp_path) -> None:
    """Stack mode with a non-stackable product fails fast."""
    with pytest.raises(ValueError, match="not InSAR-stackable"):
        ASF(
            start="2024-01-01",
            end="2024-01-31",
            variables=["opera-rtc-s1"],
            reference="OPERA_FAKE",
            path=tmp_path,
        )


@pytest.mark.asf
@pytest.mark.unit
def test_search_mode_requires_bbox(tmp_path) -> None:
    """Search mode without lat_lim / lon_lim is rejected."""
    with pytest.raises(ValueError, match="search mode requires lat_lim"):
        ASF(
            start="2024-01-01",
            end="2024-01-31",
            variables=["sentinel-1-slc"],
            path=tmp_path,
        )


@pytest.mark.asf
@pytest.mark.unit
def test_variables_must_be_exactly_one_product(tmp_path) -> None:
    """Passing two products is rejected — one product per call."""
    with pytest.raises(ValueError, match="exactly one product"):
        ASF(
            start="2024-01-01",
            end="2024-01-31",
            variables=["sentinel-1-slc", "sentinel-1-burst"],
            lat_lim=[0.0, 1.0],
            lon_lim=[0.0, 1.0],
            path=tmp_path,
        )


@pytest.mark.asf
@pytest.mark.unit
def test_aliased_product_key_resolves(tmp_path) -> None:
    """A friendly alias resolves to the curated key."""
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["s1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    assert backend._product_key == "sentinel-1-slc"
