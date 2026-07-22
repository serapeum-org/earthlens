"""Facade routing + alias tests for the Argo backend."""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd
import pytest
from earthlens.core import EarthLens

from earthlens.argo import ARGO

from .conftest import FakeArgo

pytestmark = pytest.mark.argo


def test_argo_key_resolves():
    """The canonical key and both aliases resolve to the ARGO backend."""
    for key in ("argo", "argo-floats", "argopy"):
        assert key in EarthLens.DataSources
        assert EarthLens.DataSources[key] is ARGO


def test_aliases_collapse_to_canonical():
    """sources() lists the canonical 'argo' key, not its aliases."""
    import earthlens

    keys = earthlens.core.sources()
    assert "argo" in keys
    assert "argo-floats" not in keys
    assert "argopy" not in keys


def test_facade_routes_download(fake_argopy: FakeArgo, tmp_path):
    """EarthLens('argo', ...).download() routes through the backend to a DataFrame."""
    result = EarthLens(
        "argo",
        variables=["TEMP", "PSAL"],
        start="2020-01-01",
        end="2020-01-15",
        lat_lim=[40.0, 45.0],
        lon_lim=[-60.0, -55.0],
        path=str(tmp_path),
    ).download()
    assert isinstance(result, pd.DataFrame)
    assert len(result) == 3
    assert fake_argopy.ctor_kwargs == {"src": "erddap", "ds": "phy", "mode": "standard"}


def test_facade_forwards_backend_kwargs(fake_argopy: FakeArgo, tmp_path):
    """Backend knobs (dataset/source/mode) forward through the facade."""
    EarthLens(
        "argopy",
        variables=["DOXY"],
        start="2020-01-01",
        end="2020-01-15",
        lat_lim=[40.0, 45.0],
        lon_lim=[-60.0, -55.0],
        path=str(tmp_path),
        dataset="bgc",
        source="gdac",
    ).download()
    assert fake_argopy.ctor_kwargs["ds"] == "bgc"
    assert fake_argopy.ctor_kwargs["src"] == "gdac"
