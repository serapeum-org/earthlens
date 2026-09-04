"""Shared fixtures for the JRC tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _forbid_network(request, monkeypatch):
    """Fail any non-e2e JRC test that reaches the network.

    The helpers take an injectable `http_text`; binding the real fetcher as a
    default argument once made the fakes inert and sent the "offline" suite to
    the live server. This turns that class of regression into an immediate,
    obvious failure instead of a slow, flaky pass.
    """
    if request.node.get_closest_marker("e2e"):
        return

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "a non-e2e JRC test attempted to reach the network; inject the "
            "`http_text` / `http_bytes` seam, or stub "
            "`pyramids.netcdf.NetCDF.read_file` for a cube read."
        )

    import requests

    monkeypatch.setattr(requests.Session, "request", _blocked)
    monkeypatch.setattr(requests, "get", _blocked)

    # The backend's raster I/O goes through pyramids, not requests, so block
    # that route too: otherwise a test can still reach the live cube over
    # /vsicurl.
    if not request.node.get_closest_marker("real_band_names"):
        monkeypatch.setattr("pyramids.netcdf.NetCDF.read_file", _blocked)
