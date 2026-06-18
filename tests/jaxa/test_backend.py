"""Unit tests for the JAXA backend's construction + protocol dispatch."""

from __future__ import annotations

import pytest

from earthlens.jaxa import JAXA


@pytest.fixture
def base_kwargs(tmp_path) -> dict:
    """Common JAXA(...) kwargs for the unit tests."""
    return {
        "start": "2020-01-01",
        "end": "2020-12-31",
        "lat_lim": [35.0, 36.0],
        "lon_lim": [138.0, 139.0],
        "path": tmp_path,
    }


@pytest.mark.jaxa
@pytest.mark.unit
def test_empty_variables_rejected(base_kwargs) -> None:
    """An empty variables list raises ValueError with a helpful hint."""
    with pytest.raises(ValueError, match="non-empty `variables`"):
        JAXA(variables=[], **base_kwargs)


@pytest.mark.jaxa
@pytest.mark.unit
def test_unknown_variable_rejected(base_kwargs) -> None:
    """An unknown key raises a did-you-mean ValueError."""
    with pytest.raises(ValueError, match="not in the JAXA catalog"):
        JAXA(variables=["not-a-thing"], **base_kwargs)


@pytest.mark.jaxa
@pytest.mark.unit
def test_jaxa_earth_request_resolves_to_protocol(base_kwargs) -> None:
    """A jaxa-earth alias resolves and pins backend.protocol."""
    backend = JAXA(variables=["elevation"], **base_kwargs)
    assert backend.protocol == "jaxa-earth"
    assert backend.OUTPUT_KIND == "raster"


@pytest.mark.jaxa
@pytest.mark.unit
def test_gportal_request_resolves_to_protocol(base_kwargs) -> None:
    """A gportal key resolves and pins backend.protocol."""
    backend = JAXA(variables=["sgli-l380"], **base_kwargs)
    assert backend.protocol == "gportal"


@pytest.mark.jaxa
@pytest.mark.unit
def test_mixed_protocol_request_rejected(base_kwargs) -> None:
    """A request mixing the two protocols is rejected."""
    with pytest.raises(ValueError, match="single protocol"):
        JAXA(variables=["aw3d30", "sgli-l380"], **base_kwargs)


@pytest.mark.jaxa
@pytest.mark.unit
def test_aggregate_argument_not_supported(base_kwargs) -> None:
    """`download(aggregate=...)` raises NotImplementedError."""
    backend = JAXA(variables=["elevation"], **base_kwargs)
    with pytest.raises(NotImplementedError, match="aggregate"):
        backend.download(aggregate=object())
