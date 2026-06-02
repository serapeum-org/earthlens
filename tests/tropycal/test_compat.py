"""Tests for the tropycal pkg_resources compatibility shim."""

from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from earthlens.tropycal._compat import ensure_pkg_resources

pytestmark = pytest.mark.tropycal


def test_noop_when_pkg_resources_present(monkeypatch):
    """A pre-existing pkg_resources is left untouched."""
    sentinel = types.ModuleType("pkg_resources")
    sentinel.marker = object()
    monkeypatch.setitem(sys.modules, "pkg_resources", sentinel)
    ensure_pkg_resources()
    assert sys.modules["pkg_resources"] is sentinel


def test_shim_installed_when_absent(monkeypatch):
    """When pkg_resources is missing, a stand-in with get_distribution is installed."""
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None
        if name == "pkg_resources"
        else real_find_spec(name, *a, **k),
    )
    monkeypatch.delitem(sys.modules, "pkg_resources", raising=False)
    try:
        ensure_pkg_resources()
        shim = sys.modules["pkg_resources"]
        assert getattr(shim, "__earthlens_shim__", False) is True
        assert isinstance(shim.get_distribution("numpy").version, str)
    finally:
        if getattr(sys.modules.get("pkg_resources"), "__earthlens_shim__", False):
            del sys.modules["pkg_resources"]


def test_shim_unknown_package_falls_back(monkeypatch):
    """The shim returns a placeholder version for an uninstalled package."""
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None
        if name == "pkg_resources"
        else real_find_spec(name, *a, **k),
    )
    monkeypatch.delitem(sys.modules, "pkg_resources", raising=False)
    try:
        ensure_pkg_resources()
        shim = sys.modules["pkg_resources"]
        assert shim.get_distribution("no-such-distribution-xyz").version == "0+unknown"
    finally:
        if getattr(sys.modules.get("pkg_resources"), "__earthlens_shim__", False):
            del sys.modules["pkg_resources"]


def test_importing_package_makes_pkg_resources_importable():
    """Importing earthlens.tropycal leaves pkg_resources importable."""
    import earthlens.tropycal  # noqa: F401

    import pkg_resources

    assert isinstance(pkg_resources.get_distribution("numpy").version, str)
