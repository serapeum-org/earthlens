"""Integration tests for the CMEMS backend through `EarthLens`."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from earthlens.aggregate import AggregationConfig
from earthlens.cmems import CMEMS
from earthlens.earthlens import EarthLens


class _FakeCmems(types.ModuleType):
    def __init__(self):
        super().__init__("copernicusmarine")
        self.login_calls: list[dict[str, Any]] = []
        self.subset_calls: list[dict[str, Any]] = []
        self.subset_response: Any = None
        self.InvalidUsernameOrPassword = type(
            "InvalidUsernameOrPassword", (Exception,), {}
        )
        self.CouldNotConnectToAuthenticationSystem = type(
            "CouldNotConnectToAuthenticationSystem", (Exception,), {}
        )
        self.CredentialsCannotBeNone = type(
            "CredentialsCannotBeNone", (Exception,), {}
        )

    def login(self, **kwargs: Any) -> bool:
        self.login_calls.append(dict(kwargs))
        return True

    def subset(self, **kwargs: Any) -> Any:
        self.subset_calls.append(dict(kwargs))
        return self.subset_response


@pytest.fixture
def fake_cmems(monkeypatch: pytest.MonkeyPatch) -> _FakeCmems:
    fake = _FakeCmems()
    monkeypatch.setitem(sys.modules, "copernicusmarine", fake)
    return fake


@pytest.mark.cmems
@pytest.mark.integration
class TestEarthLensCmemsRouting:
    """The facade resolves `data_source='cmems'` to the new backend."""

    def test_facade_registry_contains_cmems(self):
        """`cmems` is a registered key."""
        assert "cmems" in EarthLens.DataSources, (
            f"expected 'cmems' in DataSources, got {sorted(EarthLens.DataSources)!r}"
        )

    def test_facade_resolves_cmems_class(self):
        """The key resolves to the `CMEMS` class."""
        assert EarthLens.DataSources["cmems"] is CMEMS, (
            "DataSources['cmems'] should resolve to earthlens.cmems.CMEMS"
        )

    def test_facade_constructs_cmems(
        self, fake_cmems: _FakeCmems, tmp_path: Path
    ):
        """`EarthLens(data_source='cmems', ...)` instantiates the backend."""
        el = EarthLens(
            data_source="cmems",
            start="2024-01-01",
            end="2024-01-02",
            variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
            lat_lim=[40.0, 42.0],
            lon_lim=[-10.0, -8.0],
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        assert isinstance(el.datasource, CMEMS), (
            f"el.datasource must be a CMEMS instance, got {type(el.datasource).__name__}"
        )
        assert el.datasource.OUTPUT_KIND == "raster"


@pytest.mark.cmems
@pytest.mark.integration
class TestEarthLensCmemsAggregateGuard:
    """The facade forwards aggregate to CMEMS, which reduces via pyramids."""

    def test_facade_forwards_aggregate_to_reduce_path(
        self, fake_cmems: _FakeCmems, tmp_path: Path
    ):
        """For OUTPUT_KIND='raster', the facade forwards aggregate.

        The CMEMS backend downloads the subset, then routes into the
        `pyramids.netcdf.NetCDF.reduce`-backed aggregate path. When the
        installed pyramids has no `NetCDF.reduce` (a release that
        predates pyramids PR #339), the backend raises a clear
        NotImplementedError naming `NetCDF.reduce` — proving the facade
        guard is *not* the one stopping CMEMS (CMEMS is `"raster"`, so
        the kwarg is forwarded) and that the requirement is the pyramids
        reducer, not a staged earthlens shim.
        """
        subset = tmp_path / "cmems_mod_glo_phy_my_0.083deg_P1D-m.nc"
        subset.write_bytes(b"")
        fake_cmems.subset_response = types.SimpleNamespace(
            file_path=str(subset), status="ok"
        )
        el = EarthLens(
            data_source="cmems",
            start="2024-01-01",
            end="2024-01-02",
            variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
            lat_lim=[40.0, 42.0],
            lon_lim=[-10.0, -8.0],
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        with pytest.raises(NotImplementedError, match="NetCDF.reduce"):
            el.download(
                progress_bar=False,
                aggregate=AggregationConfig(freq="1MS", op="mean"),
            )

    def test_facade_rejects_aggregate_for_vector_only(
        self, fake_cmems: _FakeCmems, tmp_path: Path
    ):
        """Sanity: the C1 guard still rejects aggregate when OUTPUT_KIND='vector'.

        Patches the instance-level OUTPUT_KIND to "vector" and confirms the
        facade — not the backend — raises the NotImplementedError. Proves the
        guard distinguishes by OUTPUT_KIND and that CMEMS' "raster" declaration
        is what unlocks the kwarg in the previous test.
        """
        el = EarthLens(
            data_source="cmems",
            start="2024-01-01",
            end="2024-01-02",
            variables={"cmems_mod_glo_phy_my_0.083deg_P1D-m": ["thetao"]},
            lat_lim=[40.0, 42.0],
            lon_lim=[-10.0, -8.0],
            path=str(tmp_path),
            service_username="u",
            service_password="p",
        )
        el.datasource.OUTPUT_KIND = "vector"  # type: ignore[assignment]
        with pytest.raises(NotImplementedError, match="aggregate= is not supported"):
            el.download(
                progress_bar=False,
                aggregate=AggregationConfig(freq="1MS", op="mean"),
            )
