"""Tests for `ASF._search` (search mode + stack mode + baselines)."""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.asf import ASF
from earthlens.asf._helpers import (
    apply_baseline_windows,
    wkt_from_extent,
)
from earthlens.base import SpatialExtent


@pytest.mark.asf
@pytest.mark.unit
def test_wkt_from_extent_round_trips_a_bbox() -> None:
    """`wkt_from_extent` returns a POLYGON over the same bbox."""
    space = SpatialExtent(
        latitude_min=0.0,
        latitude_max=1.5,
        longitude_min=-100.0,
        longitude_max=-99.0,
    )
    wkt = wkt_from_extent(space)
    assert wkt.startswith("POLYGON")
    # shapely strips trailing zeros, so check on the integer-rounded forms.
    for coord in ("0", "1.5", "-100", "-99"):
        assert coord in wkt


@pytest.mark.asf
@pytest.mark.unit
def test_apply_baseline_windows_drops_out_of_window_products() -> None:
    """A defensive client-side filter keeps the baseline windows honest."""

    class _P:
        def __init__(self, perp, temp):
            self.properties = {
                "perpendicularBaseline": perp,
                "temporalBaseline": temp,
            }

    inside = _P(50.0, 12)
    too_high = _P(200.0, 12)
    too_late = _P(50.0, 999)
    missing = _P(None, 12)
    filtered = apply_baseline_windows(
        [inside, too_high, too_late, missing],
        perpendicular_baseline=(-100.0, 100.0),
        temporal_baseline=(0, 60),
    )
    assert filtered == [inside]


@pytest.mark.asf
@pytest.mark.unit
def test_apply_baseline_windows_wildcard_keeps_all() -> None:
    """`None` windows disable the filter entirely."""

    class _P:
        def __init__(self, perp, temp):
            self.properties = {
                "perpendicularBaseline": perp,
                "temporalBaseline": temp,
            }

    products = [_P(0.0, 0), _P(500.0, 500), _P(None, None)]
    assert (
        apply_baseline_windows(products, None, None) == products
    )


@pytest.mark.asf
@pytest.mark.unit
def test_search_mode_calls_geo_search_with_resolved_args(
    fake_asf_search, tmp_path: Path
) -> None:
    """Search mode dispatches `geo_search` with WKT + resolved constants."""
    from tests.asf.conftest import _FakeProduct

    fake_asf_search.search_results = [
        _FakeProduct(sceneName="S1A_FOO_SLC"),
        _FakeProduct(sceneName="S1A_BAR_SLC"),
    ]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[40.0, 41.0],
        lon_lim=[-100.0, -99.0],
        path=tmp_path,
        flight_direction="ASCENDING",
        beam_mode="IW",
        polarization="VV",
        max_results=42,
    )
    products = backend._search()
    assert len(products) == 2
    assert fake_asf_search.geo_search_calls, "geo_search was not called"
    call = fake_asf_search.geo_search_calls[0]
    assert call["platform"] == "SENTINEL1"
    assert call["processingLevel"] == "SLC"
    assert call["flightDirection"] == "ASCENDING"
    assert call["beamMode"] == "IW"
    assert call["polarization"] == "VV"
    assert call["maxResults"] == 42
    assert call["start"].startswith("2024-01-01")
    assert call["end"].startswith("2024-01-31")
    assert call["intersectsWith"].startswith("POLYGON")


@pytest.mark.asf
@pytest.mark.unit
def test_search_mode_uses_dataset_for_processed_products(
    fake_asf_search, tmp_path: Path
) -> None:
    """Catalog rows keyed by `dataset` reach `geo_search(dataset=...)`."""
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["opera-rtc-s1"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    backend._search()
    call = fake_asf_search.geo_search_calls[0]
    assert "platform" not in call
    assert call["dataset"] == "OPERA_S1"
    assert call["processingLevel"] == "RTC"


@pytest.mark.asf
@pytest.mark.unit
def test_stack_mode_runs_granule_search_then_stack(
    fake_asf_search, tmp_path: Path
) -> None:
    """Stack mode calls `granule_search` and then `.stack()` on the result."""
    from tests.asf.conftest import _FakeProduct

    stacked = [
        _FakeProduct(sceneName="S1A_REF_SLC", perpendicularBaseline=0.0, temporalBaseline=0),
        _FakeProduct(sceneName="S1A_SEC_SLC", perpendicularBaseline=42.0, temporalBaseline=12),
    ]
    reference = _FakeProduct(sceneName="S1A_REF_SLC", stack_return=stacked)
    fake_asf_search.granule_results = [reference]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="S1A_REF_SLC",
        perpendicular_baseline=(-100.0, 100.0),
        temporal_baseline=(0, 90),
        path=tmp_path,
    )
    products = backend._search()
    assert fake_asf_search.granule_search_calls == [["S1A_REF_SLC"]]
    assert reference.stack_calls, "stack() was not called on the reference"
    opts = reference.stack_calls[0]["opts"]
    assert opts.kwargs["minBaselinePerp"] == -100.0
    assert opts.kwargs["maxBaselinePerp"] == 100.0
    assert opts.kwargs["temporalBaselineDays"] == "0,90"
    assert len(products) == 2
    assert products[1].metadata["perpendicularBaseline"] == 42.0


@pytest.mark.asf
@pytest.mark.unit
def test_stack_mode_post_filters_baseline_windows(
    fake_asf_search, tmp_path: Path
) -> None:
    """Stacked products outside the windows are dropped client-side."""
    from tests.asf.conftest import _FakeProduct

    stacked = [
        _FakeProduct(sceneName="S1_A", perpendicularBaseline=10.0, temporalBaseline=6),
        _FakeProduct(sceneName="S1_B", perpendicularBaseline=999.0, temporalBaseline=6),
        _FakeProduct(sceneName="S1_C", perpendicularBaseline=10.0, temporalBaseline=999),
    ]
    reference = _FakeProduct(sceneName="REF", stack_return=stacked)
    fake_asf_search.granule_results = [reference]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="REF",
        perpendicular_baseline=(-100.0, 100.0),
        temporal_baseline=(0, 60),
        path=tmp_path,
    )
    products = backend._search()
    assert [p.id for p in products] == ["S1_A"]


@pytest.mark.asf
@pytest.mark.unit
def test_stack_mode_unknown_reference_raises(
    fake_asf_search, tmp_path: Path
) -> None:
    """An empty `granule_search` result names the reference id in the error."""
    fake_asf_search.granule_results = []
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        reference="S1A_DOES_NOT_EXIST",
        path=tmp_path,
    )
    with pytest.raises(ValueError, match="S1A_DOES_NOT_EXIST"):
        backend._search()


@pytest.mark.asf
@pytest.mark.unit
def test_search_mode_remote_product_metadata_omits_baseline_keys(
    fake_asf_search, tmp_path: Path
) -> None:
    """Search-mode `RemoteProduct.metadata` omits baseline keys (stack-only)."""
    from tests.asf.conftest import _FakeProduct

    fake_asf_search.search_results = [
        _FakeProduct(
            sceneName="S1A_TEST_SLC",
            fileName="S1A_TEST_SLC.zip",
        ),
    ]
    backend = ASF(
        start="2024-01-01",
        end="2024-01-31",
        variables=["sentinel-1-slc"],
        lat_lim=[0.0, 1.0],
        lon_lim=[0.0, 1.0],
        path=tmp_path,
    )
    [remote] = backend._search()
    assert remote.id == "S1A_TEST_SLC"
    assert remote.metadata["fileName"] == "S1A_TEST_SLC.zip"
    assert "perpendicularBaseline" not in remote.metadata
    assert "temporalBaseline" not in remote.metadata
    assert remote.metadata["product"] is fake_asf_search.search_results[0]
