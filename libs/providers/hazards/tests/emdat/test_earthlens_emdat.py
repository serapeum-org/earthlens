"""Facade-routing tests for the EM-DAT backend (`EarthLens` -> `EMDAT`).

Both registered keys resolve to the same class, and the per-instance
`OUTPUT_KIND` has to survive the trip through the facade — it is what the
facade reads to decide the return shape and to refuse `aggregate=`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import earthlens.emdat
from earthlens.earthlens import EarthLens


def _make_facade(tmp_path: Path, **overrides) -> EarthLens:
    """Construct an EarthLens facade bound to the EM-DAT backend."""
    params: dict[str, object] = dict(
        variables=["emdat:events"],
        data_source="emdat",
        path=str(tmp_path),
    )
    params.update(overrides)
    return EarthLens(**params)


@pytest.mark.emdat
class TestFacadeRouting:
    """The `emdat` / `gdis` keys resolve to and construct the EMDAT backend."""

    @pytest.mark.parametrize("key", ["emdat", "gdis"])
    def test_keys_present(self, key: str) -> None:
        """Both registered keys are among the data sources."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", ["emdat", "gdis"])
    def test_keys_resolve_to_emdat_class(self, key: str) -> None:
        """Both keys resolve to `earthlens.emdat.EMDAT`."""
        assert EarthLens.DataSources[key] is earthlens.emdat.EMDAT

    def test_facade_builds_emdat_backend(self, tmp_path: Path) -> None:
        """The facade binds an EMDAT instance as its datasource."""
        facade = _make_facade(tmp_path)
        assert isinstance(facade.datasource, earthlens.emdat.EMDAT)

    def test_backend_kwargs_forwarded(self, tmp_path: Path) -> None:
        """The hazard and country kwargs ride through `**backend_kwargs`."""
        facade = _make_facade(tmp_path, hazard="Flood", country="bgd")
        assert facade.datasource._hazards == ["flood"]
        assert facade.datasource._country == "bgd"


@pytest.mark.emdat
class TestOutputKindThroughFacade:
    """The per-instance `OUTPUT_KIND` reaches the facade intact."""

    @pytest.mark.parametrize(
        ("dataset_id", "expected"),
        [
            ("emdat:events", "tabular"),
            ("gdis:points", "vector"),
            ("gdis:polygons", "vector"),
        ],
    )
    def test_output_kind_per_dataset(
        self, tmp_path: Path, dataset_id: str, expected: str
    ) -> None:
        """Each dataset id sets its own output kind on the bound backend."""
        facade = _make_facade(tmp_path, variables=[dataset_id])
        assert facade.datasource.OUTPUT_KIND == expected

    @pytest.mark.parametrize("dataset_id", ["emdat:events", "gdis:points"])
    def test_aggregate_rejected(self, tmp_path: Path, dataset_id: str) -> None:
        """`aggregate=` is refused for every dataset — none of them is gridded."""
        facade = _make_facade(tmp_path, variables=[dataset_id])
        with pytest.raises(NotImplementedError, match="aggregate="):
            facade.download(aggregate=object())
