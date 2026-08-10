"""Facade-routing tests for the HANZE backend (`EarthLens` -> `HANZE`).

The `hanze` key must resolve to and construct the backend, forward the
HANZE-specific selectors through `**backend_kwargs`, carry the per-instance
`OUTPUT_KIND` (`tabular` by default, `vector` with `with_geometry=True`), and
refuse `aggregate=` for both — the guard the facade reads `OUTPUT_KIND` for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.hanze
from earthlens.earthlens import EarthLens


def _make_facade(tmp_path: Path, **overrides: object) -> EarthLens:
    """Construct an EarthLens facade bound to the HANZE backend."""
    params: dict[str, object] = dict(
        data_source="hanze",
        start="1990",
        end="2020",
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.hanze
class TestFacadeRouting:
    """The `hanze` key resolves to and constructs the HANZE backend."""

    def test_key_registered(self) -> None:
        """`hanze` is among the registered data sources."""
        assert "hanze" in EarthLens.DataSources

    def test_key_resolves_to_hanze_class(self) -> None:
        """The `hanze` key resolves to `earthlens.hanze.HANZE`."""
        assert EarthLens.DataSources["hanze"] is earthlens.hanze.HANZE

    def test_facade_builds_hanze_backend(self, tmp_path: Path) -> None:
        """The facade binds a HANZE instance as its datasource."""
        facade = _make_facade(tmp_path)
        assert isinstance(facade.datasource, earthlens.hanze.HANZE)

    def test_selectors_forwarded(self, tmp_path: Path) -> None:
        """The country / flood_type selectors ride through `**backend_kwargs`."""
        facade = _make_facade(tmp_path, country=["DE", "NL"], flood_type="coastal")
        backend = facade.datasource
        assert backend._country == {"DE", "NL"}
        assert backend._flood_types == ["Coastal"]


@pytest.mark.hanze
class TestFacadeOutputKind:
    """The per-instance `OUTPUT_KIND` survives the trip through the facade."""

    def test_default_is_tabular(self, tmp_path: Path) -> None:
        """A plain request is `tabular`."""
        assert _make_facade(tmp_path).datasource.OUTPUT_KIND == "tabular"

    def test_with_geometry_is_vector(self, tmp_path: Path) -> None:
        """`with_geometry=True` makes the instance `vector`."""
        facade = _make_facade(tmp_path, with_geometry=True)
        assert facade.datasource.OUTPUT_KIND == "vector"


@pytest.mark.hanze
class TestFacadeAggregateRejection:
    """A tabular / vector backend refuses `aggregate=` at the facade."""

    def test_aggregate_raises_not_implemented(self, tmp_path: Path) -> None:
        """`download(aggregate=...)` raises before any network fetch."""
        facade = _make_facade(tmp_path)
        with pytest.raises(NotImplementedError) as exc:
            facade.download(aggregate=object())
        assert "aggregate=" in str(exc.value)

    def test_with_geometry_also_rejects_aggregate(self, tmp_path: Path) -> None:
        """The vector variant refuses `aggregate=` too."""
        facade = _make_facade(tmp_path, with_geometry=True)
        with pytest.raises(NotImplementedError):
            facade.download(aggregate=object())
