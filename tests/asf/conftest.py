"""Shared fakes and fixtures for the ASF backend tests.

The whole suite runs without `asf_search` installed or any network:
:class:`_FakeAsfSearch` is injected into `sys.modules` so the lazy
`import asf_search` inside `earthlens.asf` resolves to the fake.

The fake records every search/granule/stack/download call so tests
can assert on the exact kwargs the backend passes the SDK without
actually contacting ASF.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


class _FakeProduct:
    """Stand-in for an `asf_search.ASFProduct`.

    Carries a `.properties` dict mimicking the real product (with
    `sceneName`, `url`, `fileName`, baseline keys) and a `.stack()`
    method the test can override via `stack_return`.
    """

    def __init__(
        self,
        *,
        sceneName: str,
        fileName: str | None = None,
        url: str | None = None,
        perpendicularBaseline: float | None = None,
        temporalBaseline: int | None = None,
        stack_return: list[Any] | None = None,
    ) -> None:
        self.properties: dict[str, Any] = {
            "sceneName": sceneName,
            "fileName": fileName or f"{sceneName}.zip",
            "url": url or f"https://datapool.asf.alaska.edu/SLC/SA/{sceneName}.zip",
            "perpendicularBaseline": perpendicularBaseline,
            "temporalBaseline": temporalBaseline,
        }
        self.stack_calls: list[dict[str, Any]] = []
        self._stack_return = stack_return

    def stack(self, opts: Any = None, useSubclass: Any = None) -> Any:
        """Record the call and return the configured stack."""
        self.stack_calls.append({"opts": opts, "useSubclass": useSubclass})
        return _FakeResults(self._stack_return or [])


class _FakeResults(list):
    """Stand-in for `asf_search.ASFSearchResults` — a list with `download`."""

    def __init__(self, items: list[Any]):
        super().__init__(items)
        self.download_calls: list[dict[str, Any]] = []

    def download(
        self,
        path: str,
        session: Any = None,
        processes: int = 1,
        fileType: Any = None,
    ) -> None:
        """Record the call and create a tiny file per product."""
        self.download_calls.append(
            {
                "path": path,
                "session": session,
                "processes": processes,
                "fileType": fileType,
            }
        )
        out = Path(path)
        out.mkdir(parents=True, exist_ok=True)
        for product in self:
            (out / product.properties["fileName"]).write_bytes(b"asf-data")


class _FakeSession:
    """Stand-in for `asf_search.ASFSession`."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.username: str | None = None
        self.password: str | None = None

    def auth_with_token(self, token: str) -> _FakeSession:
        self.token = token
        return self

    def auth_with_creds(self, username: str, password: str) -> _FakeSession:
        self.username = username
        self.password = password
        return self


class _FakeOptions:
    """Stand-in for `asf_search.ASFSearchOptions` — stores kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeConstants(types.SimpleNamespace):
    """Stand-in for the `PLATFORM` / `DATASET` / `PRODUCT_TYPE` modules."""


class _FakeAsfSearch(types.ModuleType):
    """Fake `asf_search` module recording every call the backend makes."""

    def __init__(self) -> None:
        super().__init__("asf_search")
        self.geo_search_calls: list[dict[str, Any]] = []
        self.granule_search_calls: list[list[str]] = []
        self.search_results: list[_FakeProduct] = []
        self.granule_results: list[_FakeProduct] = []
        self.ASFSession = _FakeSession
        self.ASFSearchOptions = _FakeOptions
        self.ASFSearchResults = _FakeResults
        # Constant modules — populated with the real (member name ->
        # SDK value) mapping for every constant the curated catalog
        # references. Lets the constant-name guard test pass and
        # surfaces the same `getattr(asf.PLATFORM, name)` value the
        # real SDK returns at search time.
        self.PLATFORM = _FakeConstants(
            AIRSAR="AIRSAR",
            ALOS="ALOS",
            ERS="ERS",
            ERS1="ERS-1",
            ERS2="ERS-2",
            JERS="JERS-1",
            NISAR="NISAR",
            RADARSAT="RADARSAT-1",
            SEASAT="SEASAT 1",
            SENTINEL1="SENTINEL-1",
            SENTINEL1A="Sentinel-1A",
            SENTINEL1B="Sentinel-1B",
            SENTINEL1C="Sentinel-1C",
            SENTINEL1D="Sentinel-1D",
            SIRC="SIR-C",
            SMAP="SMAP",
            UAVSAR="UAVSAR",
        )
        self.DATASET = _FakeConstants(
            ALOS_2="ALOS-2",
            ARIA_S1_GUNW="ARIA S1 GUNW",
            OPERA_S1="OPERA-S1",
            OPERA_S1_CALVAL="OPERA-S1-CALVAL",
            SLC_BURST="SLC-BURST",
            SENTINEL1="SENTINEL-1",
            TROPO="TROPO",
        )
        self.PRODUCT_TYPE = _FakeConstants(
            AMPLITUDE="AMPLITUDE",
            SLC="SLC",
            BURST="BURST",
            GRD_HD="GRD_HD",
            GRD_MD="GRD_MD",
            RTC="RTC",
            RTC_STATIC="RTC-STATIC",
            CSLC="CSLC",
            CSLC_STATIC="CSLC-STATIC",
            DIST_ALERT_S1="DIST-ALERT-S1",
            ECMWF_TROPO="ECMWF_TROPO",
            GCOV="GCOV",
            GOFF="GOFF",
            GSLC="GSLC",
            GUNW="GUNW",
            GUNW_STD="GUNW_STD",
            L0="L0",
            L0B="L0B",
            L1="L1",
            L1A_RADAR_HDF5="L1A_Radar_HDF5",
            L1_1="L1.1",
            LRCLK_UTC="LRCLK_UTC",
            OCN="OCN",
            RAW="RAW",
            RIFG="RIFG",
            ROFF="ROFF",
            RSLC="RSLC",
            RUNW="RUNW",
            TROPO_ZENITH="TROPO-ZENITH",
        )

    def geo_search(self, **kwargs: Any) -> _FakeResults:
        self.geo_search_calls.append(kwargs)
        return _FakeResults(list(self.search_results))

    def granule_search(self, granule_list: list[str], opts: Any = None) -> _FakeResults:
        self.granule_search_calls.append(list(granule_list))
        return _FakeResults(list(self.granule_results))


@pytest.fixture
def fake_asf_search(monkeypatch: pytest.MonkeyPatch) -> _FakeAsfSearch:
    """Inject a fake `asf_search` module under `sys.modules`.

    Lets the backend's lazy `import asf_search` resolve to the
    fake. The fake records every call and yields the configured
    products.
    """
    fake = _FakeAsfSearch()
    monkeypatch.setitem(sys.modules, "asf_search", fake)
    return fake


class _FakeEarthdataHandle:
    """Stand-in for the `earthaccess.Auth` handle behind `EarthdataAuth`."""

    def __init__(self, token: dict[str, Any] | None = None) -> None:
        self.token = token or {"access_token": "EDL.FAKE.TOKEN"}


class _FakeEarthdataAuth:
    """Stand-in for `EarthdataAuth` used by the ASFAuth tests."""

    instances: list[_FakeEarthdataAuth] = []

    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials
        self.configured = False
        self._auth = _FakeEarthdataHandle()
        _FakeEarthdataAuth.instances.append(self)

    def configure(self) -> None:
        self.configured = True


@pytest.fixture
def fake_earthdata_auth(monkeypatch: pytest.MonkeyPatch) -> type[_FakeEarthdataAuth]:
    """Swap `earthlens.asf.auth.EarthdataAuth` for the recording fake."""
    _FakeEarthdataAuth.instances = []
    monkeypatch.setattr(
        "earthlens.asf.auth.EarthdataAuth",
        _FakeEarthdataAuth,
    )
    # The fake EarthdataCredentials accepts the same fields as the
    # real one; the real class is fine — we only need to short-circuit
    # the auth object itself.
    return _FakeEarthdataAuth


@pytest.fixture
def reset_catalog_cache():
    """Clear the catalog parse cache before and after a test."""
    from earthlens.asf import catalog as catalog_module

    catalog_module.clear_catalog_cache()
    yield
    catalog_module.clear_catalog_cache()
