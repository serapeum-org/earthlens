"""Tests for the `EarthLens` facade entries routing to the NSI backend."""

from __future__ import annotations

import pytest

import earthlens.nsi
from earthlens.earthlens import EarthLens

pytestmark = pytest.mark.nsi

KEYS = ["nsi", "nfip", "nfhl"]


@pytest.mark.unit
class TestRegistry:
    """Tests for the NSI entries in `EarthLens.DataSources`."""

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_present(self, key: str) -> None:
        """Every NSI key is registered in `EarthLens.DataSources`."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", KEYS)
    def test_keys_resolve_to_nsi_class(self, key: str) -> None:
        """All NSI keys resolve to `earthlens.nsi.NSI`."""
        assert EarthLens.DataSources[key] is earthlens.nsi.NSI

    def test_source_pinning_aliases_carry_default_kwargs(self) -> None:
        """`nfip` / `nfhl` pin their source so they cannot fall back to structures."""
        assert EarthLens.DataSources.default_kwargs("nfip") == {"source": "nfip"}
        assert EarthLens.DataSources.default_kwargs("nfhl") == {"source": "nfhl"}
        assert EarthLens.DataSources.default_kwargs("nsi") == {}


@pytest.mark.unit
class TestFacadeConstruction:
    """Tests for `EarthLens("nsi", ...)` and the source-pinning aliases."""

    def test_nsi_defaults_to_structures_vector(self, tmp_path) -> None:
        """`EarthLens('nsi', ...)` builds a vector structures instance."""
        el = EarthLens("nsi", source="structures", fips="22071012700", path=tmp_path)
        assert isinstance(el.datasource, earthlens.nsi.NSI)
        assert el.datasource._source.id == "structures"
        assert el.datasource.OUTPUT_KIND == "vector"

    def test_nfip_alias_pins_tabular_source(self, tmp_path) -> None:
        """`EarthLens('nfip', ...)` resolves the nfip source (tabular), not structures."""
        el = EarthLens("nfip", filters={"county": "22071"}, path=tmp_path)
        assert el.datasource._source.id == "nfip"
        assert el.datasource.OUTPUT_KIND == "tabular"

    def test_nfhl_alias_pins_vector_source(self, tmp_path) -> None:
        """`EarthLens('nfhl', ...)` resolves the nfhl source."""
        el = EarthLens(
            "nfhl", lat_lim=[29.9, 30.0], lon_lim=[-90.1, -90.0], path=tmp_path
        )
        assert el.datasource._source.id == "nfhl"
        assert el.datasource.OUTPUT_KIND == "vector"

    def test_facade_rejects_aggregate(self, tmp_path) -> None:
        """`aggregate=` is refused for the record-shaped NSI backend."""
        el = EarthLens("nsi", source="structures", fips="22071012700", path=tmp_path)
        with pytest.raises(NotImplementedError):
            el.download(aggregate=object())
