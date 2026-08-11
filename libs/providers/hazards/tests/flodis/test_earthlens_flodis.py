"""Facade-routing tests for the FLODIS backend (`EarthLens` -> `FLODIS`).

The `flodis` key resolves to the backend, the `dataset=` table selector rides
the facade's native-`dataset` path to the constructor, and the tabular
`OUTPUT_KIND` survives the trip so the facade refuses `aggregate=`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.flodis
from earthlens.earthlens import EarthLens


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the FLODIS backend."""
    params: dict[str, object] = dict(
        data_source="flodis",
        dataset="damages",
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.flodis
class TestFacadeRouting:
    """The `flodis` key resolves to and constructs the FLODIS backend."""

    def test_key_present(self) -> None:
        """The registered key is among the data sources."""
        assert "flodis" in EarthLens.DataSources

    def test_key_resolves_to_flodis_class(self) -> None:
        """The key resolves to `earthlens.flodis.FLODIS`."""
        assert EarthLens.DataSources["flodis"] is earthlens.flodis.FLODIS

    def test_facade_builds_flodis_backend(self, tmp_path: Path) -> None:
        """The facade binds a FLODIS instance as its datasource."""
        facade = _make_facade(tmp_path)
        assert isinstance(facade.datasource, earthlens.flodis.FLODIS)

    @pytest.mark.parametrize("table", ["damages", "displacement"])
    def test_dataset_selects_table(self, tmp_path: Path, table: str) -> None:
        """`dataset=` reaches the backend and selects the table."""
        facade = _make_facade(tmp_path, dataset=table)
        assert facade.datasource._dataset_name == table

    def test_backend_kwargs_forwarded(self, tmp_path: Path) -> None:
        """The country kwarg rides through `**backend_kwargs`."""
        facade = _make_facade(tmp_path, country="moz")
        assert facade.datasource._country == {"MOZ"}

    def test_requires_dataset_or_variables(self, tmp_path: Path) -> None:
        """Omitting both `dataset=` and `variables=` is rejected by the facade."""
        with pytest.raises(ValueError, match="variables= is required"):
            EarthLens(data_source="flodis", path=str(tmp_path))


@pytest.mark.flodis
class TestOutputKindThroughFacade:
    """The tabular `OUTPUT_KIND` reaches the facade intact."""

    @pytest.mark.parametrize("table", ["damages", "displacement"])
    def test_output_kind_tabular(self, tmp_path: Path, table: str) -> None:
        """Both tables are tabular on the bound backend."""
        facade = _make_facade(tmp_path, dataset=table)
        assert facade.datasource.OUTPUT_KIND == "tabular"

    @pytest.mark.parametrize("table", ["damages", "displacement"])
    def test_aggregate_rejected(self, tmp_path: Path, table: str) -> None:
        """`aggregate=` is refused — neither table is gridded."""
        facade = _make_facade(tmp_path, dataset=table)
        with pytest.raises(NotImplementedError, match="aggregate="):
            facade.download(aggregate=object())
