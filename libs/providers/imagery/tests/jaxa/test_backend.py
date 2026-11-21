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


class _StubFetch:
    """A `_fetch_dataset` stand-in: return a dummy path, raise `exc` from `from_call`.

    Holds its own call counter; `from_call=2` fails the second dataset (a
    partial failure), `from_call=1` fails every dataset (a whole-service
    failure).
    """

    def __init__(self, exc, from_call=2):
        self._exc = exc
        self._from_call = from_call
        self._calls = 0

    def __call__(self, dataset):
        """Return one fabricated path per dataset until `from_call`, then raise."""
        from pathlib import Path

        self._calls += 1
        if self._calls >= self._from_call:
            raise self._exc
        return [Path(f"{dataset.key}.tif")]


@pytest.mark.jaxa
@pytest.mark.unit
class TestErrorsPolicy:
    """`errors=` partial-failure policy across the independent datasets."""

    _TWO = ["alos2-fnf", "amsr2-seaice-north-daily"]

    def _two_dataset_backend(self, base_kwargs, monkeypatch, exc, from_call=2):
        """A jaxa-earth backend over two datasets, raising `exc` from `from_call`."""
        backend = JAXA(variables=list(self._TWO), **base_kwargs)
        assert len(backend._resolved) == 2
        monkeypatch.setattr(backend, "_fetch_dataset", _StubFetch(exc, from_call))
        return backend

    def test_raise_propagates_first_failure(self, base_kwargs, monkeypatch) -> None:
        """errors="raise" surfaces a per-dataset exception, not the batch summary."""
        backend = self._two_dataset_backend(
            base_kwargs, monkeypatch, ValueError("dataset boom")
        )
        with pytest.raises(ValueError, match="dataset boom"):
            backend.download(errors="raise")

    def test_warn_returns_the_surviving_dataset(self, base_kwargs, monkeypatch) -> None:
        """errors="warn" drops a non-fatal per-dataset failure and returns survivors."""
        backend = self._two_dataset_backend(
            base_kwargs, monkeypatch, ValueError("dataset boom")
        )
        written = backend.download(errors="warn")
        assert [p.name for p in written] == ["alos2-fnf.tif"], (
            f"expected the surviving dataset's path, got {written}"
        )

    def test_ignore_drops_failures_and_returns_survivor(
        self, base_kwargs, monkeypatch
    ) -> None:
        """errors="ignore" drops a non-fatal per-dataset failure silently."""
        backend = self._two_dataset_backend(
            base_kwargs, monkeypatch, ValueError("dataset boom")
        )
        written = backend.download(errors="ignore")
        assert [p.name for p in written] == ["alos2-fnf.tif"]

    def test_default_policy_is_warn(self, base_kwargs, monkeypatch) -> None:
        """Omitting errors= defaults to warn — a non-fatal dataset failure survives."""
        backend = self._two_dataset_backend(
            base_kwargs, monkeypatch, ValueError("dataset boom")
        )
        written = backend.download()
        assert [p.name for p in written] == ["alos2-fnf.tif"]

    @pytest.mark.parametrize("policy", ["warn", "ignore"])
    def test_auth_error_propagates_under_any_policy(
        self, base_kwargs, monkeypatch, policy
    ) -> None:
        """A whole-service AuthenticationError propagates even under warn/ignore."""
        from earthlens.jaxa import AuthenticationError

        backend = self._two_dataset_backend(
            base_kwargs, monkeypatch, AuthenticationError("creds revoked"), from_call=1
        )
        with pytest.raises(AuthenticationError, match="creds revoked"):
            backend.download(errors=policy)

    @pytest.mark.parametrize("policy", ["warn", "ignore"])
    def test_every_dataset_failing_raises_non_fatal(
        self, base_kwargs, monkeypatch, policy
    ) -> None:
        """When every dataset fails, the first error is raised, never empty."""
        backend = self._two_dataset_backend(
            base_kwargs, monkeypatch, ValueError("all datasets down"), from_call=1
        )
        with pytest.raises(ValueError, match="all datasets down"):
            backend.download(errors=policy)

    def test_invalid_policy_rejected(self, base_kwargs) -> None:
        """An unrecognised errors= value is refused before any fetch."""
        backend = JAXA(variables=["elevation"], **base_kwargs)
        with pytest.raises(ValueError, match="errors"):
            backend.download(errors="explode")
