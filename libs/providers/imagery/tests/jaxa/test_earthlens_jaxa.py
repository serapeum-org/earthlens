"""Facade-level routing tests for the JAXA backend."""

from __future__ import annotations

import pytest

from earthlens.earthlens import EarthLens


@pytest.mark.jaxa
@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    ["jaxa", "jaxa-earth", "g-portal", "ptree", "himawari"],
)
def test_jaxa_keys_registered(key) -> None:
    """Every registered JAXA facade key resolves through `EarthLens.DataSources`."""
    assert key in EarthLens.DataSources


@pytest.mark.jaxa
@pytest.mark.unit
def test_facade_constructs_jaxa(tmp_path) -> None:
    """Constructing the facade with `data_source='jaxa'` resolves the backend."""
    lens = EarthLens(
        data_source="jaxa",
        variables=["elevation"],
        start="2020-01-01",
        end="2020-12-31",
        lat_lim=[35.0, 36.0],
        lon_lim=[138.0, 139.0],
        path=tmp_path,
    )
    from earthlens.jaxa import JAXA

    assert isinstance(lens.datasource, JAXA)
    assert lens.datasource.protocol == "jaxa-earth"
