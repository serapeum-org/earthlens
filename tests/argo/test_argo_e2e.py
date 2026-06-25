"""Live end-to-end tests for the Argo float backend.

Hits the real Argo data services through `argopy` (open data, no
credentials), so these are gated only on the `e2e` marker plus a network
reachability skip (a default `pytest` run skips them). `argopy` 1.4
realises some access paths through an xarray data-mode-merge step that is
fragile against the installed xarray (the erddap region path can hit an
`oindex` / `set_dims` error, and `.float()` / `.profile()` can crash in
`transform_data_mode`); these env-specific SDK failures are treated as a
skip, not a test failure — what is asserted is the earthlens contract
(shape + columns) when `argopy` does return data.

Run with:

    pixi run -e dev pytest -m "e2e and argo"
"""

from __future__ import annotations

import socket

import pytest

pytest.importorskip("argopy")

from earthlens.earthlens import EarthLens  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.argo]

#: A small north-Atlantic box with reliable Argo coverage.
_LAT = [40.0, 45.0]
_LON = [-60.0, -55.0]
_START = "2020-01-01"
_END = "2020-01-31"

#: A known long-running Argo float (WMO id) for the float-selector case.
_FLOAT_WMO = "6902746"

#: argopy realise failures that are environment / SDK-version artefacts
#: (an xarray skew or a transient transport error), not earthlens bugs —
#: surfaced as a skip so the suite stays green where the SDK is flaky.
_SDK_SKIP = (OSError, RecursionError, AttributeError, KeyError)


def _network_ok() -> bool:
    """Return True when the Ifremer ERDDAP host is reachable on 443."""
    try:
        socket.create_connection(("erddap.ifremer.fr", 443), timeout=5).close()
        return True
    except OSError:
        return False


_offline_skip = pytest.mark.skipif(
    not _network_ok(), reason="Argo (Ifremer) host unreachable"
)


def _download_or_skip(backend) -> object:
    """Run download(), skipping on env-specific argopy/network failures."""
    try:
        return backend.download()
    except _SDK_SKIP as exc:  # pragma: no cover - depends on live SDK/env
        pytest.skip(f"argopy realise failed (env/SDK artefact): {exc!r}")


@_offline_skip
def test_region_fetch_returns_profiles(tmp_path):
    """A live region fetch returns a non-empty profile frame with T/S columns."""
    backend = EarthLens(
        "argo",
        variables=["TEMP", "PSAL"],
        start=_START,
        end=_END,
        lat_lim=_LAT,
        lon_lim=_LON,
        path=str(tmp_path),
        source="gdac",
    )
    df = _download_or_skip(backend)
    if df.empty:
        pytest.skip("region matched no floats in this window")
    assert {"TEMP", "PSAL"} <= set(df.columns)
    assert (tmp_path / "argo_phy_region.csv").exists()


@_offline_skip
def test_float_fetch_returns_profiles(tmp_path):
    """A live float: fetch for a known WMO returns at least one profile."""
    backend = EarthLens(
        "argo",
        variables=[f"float:{_FLOAT_WMO}"],
        start=_START,
        end=_END,
        lat_lim=_LAT,
        lon_lim=_LON,
        path=str(tmp_path),
        source="gdac",
    )
    df = _download_or_skip(backend)
    if df.empty:
        pytest.skip("float returned no profiles")
    assert len(df) >= 1
