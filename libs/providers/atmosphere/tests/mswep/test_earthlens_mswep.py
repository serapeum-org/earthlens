"""Facade wiring for the MSWEP / MSWX backend."""

from __future__ import annotations

import pytest

from earthlens.core import EarthLens

pytestmark = [pytest.mark.mswep, pytest.mark.unit]


class TestKeysPresent:
    """The backend is reachable through the facade's data-source table."""

    @pytest.mark.parametrize("key", ["mswep", "mswx", "gloh2o"])
    def test_keys_present(self, key: str) -> None:
        """Each registered alias resolves through the lazy registry."""
        assert key in EarthLens.DataSources

    @pytest.mark.parametrize("key", ["mswep", "mswx", "gloh2o"])
    def test_keys_resolve_to_the_backend(self, key: str) -> None:
        """Every alias imports the same backend class."""
        assert EarthLens.DataSources[key].__name__ == "MSWEP"

    def test_mswx_alias_defaults_to_the_mswx_product(self) -> None:
        """The `mswx` key carries the product default, so no kwarg is needed."""
        from earthlens._atmosphere import BACKENDS

        assert BACKENDS["mswx"][3] == {"product": "mswx"}

    def test_extras_hint_names_the_extra(self) -> None:
        """A missing SDK points the user at `earthlens[mswep]`."""
        from earthlens._atmosphere import BACKENDS

        assert BACKENDS["mswep"][2] == "mswep"


class TestFacadeConstruction:
    """`EarthLens(...)` forwards the backend's own keyword arguments."""

    def test_builds_through_the_facade(self, share, tmp_path) -> None:
        """Backend kwargs (`service`, `folder_id`) reach the constructor."""
        lens = EarthLens(
            "mswep",
            start="2020-04-25",
            end="2020-04-26",
            variables=["precipitation"],
            temporal_resolution="daily",
            path=tmp_path,
            folder_id="SHARE",
            service=share,
        )
        assert lens.datasource.OUTPUT_KIND == "raster"

    def test_mswx_key_selects_the_mswx_product(self, share, tmp_path) -> None:
        """The alias's default kwarg picks MSWX without the caller saying so."""
        lens = EarthLens(
            "mswx",
            start="2007-05-13",
            end="2007-05-13",
            variables=["Temp"],
            temporal_resolution="daily",
            path=tmp_path,
            folder_id="SHARE",
            service=share,
        )
        assert lens.datasource._product_key == "mswx"
