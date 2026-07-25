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
    """A request mixing two protocols is rejected."""
    with pytest.raises(ValueError, match="single protocol"):
        JAXA(variables=["aw3d30", "sgli-l380"], **base_kwargs)


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_key_resolves_to_ptree_protocol(base_kwargs) -> None:
    """The `himawari-ahi-fldk` key pins the ptree protocol."""
    backend = JAXA(variables=["himawari-ahi-fldk"], **base_kwargs)
    assert backend.protocol == "ptree"


@pytest.mark.jaxa
@pytest.mark.unit
def test_mixing_ptree_with_jaxa_earth_rejected(base_kwargs) -> None:
    """A request mixing ptree with jaxa-earth is rejected with both keys named."""
    with pytest.raises(ValueError, match="single protocol"):
        JAXA(variables=["aw3d30", "himawari-ahi-fldk"], **base_kwargs)


@pytest.mark.jaxa
@pytest.mark.unit
def test_mixing_ptree_with_gportal_rejected(base_kwargs) -> None:
    """A request mixing ptree with gportal is rejected."""
    with pytest.raises(ValueError, match="single protocol"):
        JAXA(variables=["sgli-l380", "himawari-ahi-fldk"], **base_kwargs)


@pytest.mark.jaxa
@pytest.mark.unit
def test_ptree_credentials_reach_the_auth_object(monkeypatch, base_kwargs) -> None:
    """`ptree_username=` / `ptree_password=` are threaded into JaxaAuth."""
    monkeypatch.delenv("JAXA_PTREE_USERNAME", raising=False)
    monkeypatch.delenv("JAXA_PTREE_PASSWORD", raising=False)
    backend = JAXA(
        variables=["himawari-ahi-fldk"],
        ptree_username="alice@example.org",
        ptree_password="pytest-fixture-not-a-real-pw",
        **base_kwargs,
    )
    backend.auth.configure()
    assert backend.auth.username == "alice@example.org"
    assert backend.auth.password is not None
    assert backend.auth.password.get_secret_value() == "pytest-fixture-not-a-real-pw"


@pytest.mark.jaxa
@pytest.mark.unit
def test_aggregate_argument_not_supported(base_kwargs) -> None:
    """`download(aggregate=...)` raises NotImplementedError."""
    backend = JAXA(variables=["elevation"], **base_kwargs)
    with pytest.raises(NotImplementedError, match="aggregate"):
        backend.download(aggregate=object())


@pytest.mark.jaxa
@pytest.mark.unit
def test_authenticate_fails_fast_for_missing_gportal_credentials(
    monkeypatch, base_kwargs
) -> None:
    """`lens.datasource.authenticate()` raises eagerly on a gportal request without creds."""
    from earthlens.jaxa import AuthenticationError

    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    backend = JAXA(variables=["sgli-l380"], **base_kwargs)
    assert backend.protocol == "gportal"
    with pytest.raises(AuthenticationError, match="GPORTAL_USERNAME"):
        backend.authenticate()


@pytest.mark.jaxa
@pytest.mark.unit
def test_authenticate_no_op_for_jaxa_earth(monkeypatch, base_kwargs) -> None:
    """`authenticate()` is a no-op for jaxa-earth even without env vars."""
    monkeypatch.delenv("GPORTAL_USERNAME", raising=False)
    monkeypatch.delenv("GPORTAL_PASSWORD", raising=False)
    backend = JAXA(variables=["elevation"], **base_kwargs)
    backend.authenticate()  # must not raise
    assert backend.auth.is_authenticated()


@pytest.mark.jaxa
@pytest.mark.unit
@pytest.mark.parametrize(
    "cadence,expected_freq",
    [
        ("daily", "D"),
        ("hourly", "h"),
        ("monthly", "MS"),
        ("yearly", "YS"),
        ("raw", "D"),
    ],
)
def test_temporal_resolution_maps_to_frequency_alias(
    base_kwargs, cadence, expected_freq
) -> None:
    """`temporal_resolution` resolves to the documented pandas frequency alias."""
    backend = JAXA(
        variables=["elevation"],
        temporal_resolution=cadence,
        **{k: v for k, v in base_kwargs.items() if k != "temporal_resolution"},
    )
    assert backend.time.resolution == expected_freq


@pytest.mark.jaxa
@pytest.mark.unit
def test_unknown_temporal_resolution_rejected(base_kwargs) -> None:
    """An unsupported cadence raises instead of silently becoming month-start."""
    kwargs = {k: v for k, v in base_kwargs.items() if k != "temporal_resolution"}
    with pytest.raises(ValueError, match="is not supported by JAXA"):
        JAXA(variables=["elevation"], temporal_resolution="nonsense", **kwargs)
