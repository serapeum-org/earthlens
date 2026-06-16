"""Tests for the Windows ecCodes-binary shim."""

from __future__ import annotations

import os
import sys
import types

import pytest

from earthlens.nwp import _eccodes

pytestmark = pytest.mark.nwp


@pytest.fixture(autouse=True)
def _reset_once(monkeypatch):
    """Clear the once-guard so each test exercises the shim body."""
    monkeypatch.setattr(_eccodes, "_done", False)
    monkeypatch.delenv("ECCODES_PYTHON_USE_FINDLIBS", raising=False)


def test_noop_off_windows(monkeypatch):
    """Off Windows the shim does nothing (Linux/macOS get the binary from eccodeslib)."""
    monkeypatch.setattr(os, "name", "posix")
    _eccodes.ensure_eccodes()
    assert "ECCODES_PYTHON_USE_FINDLIBS" not in os.environ


def test_noop_when_library_already_resolvable(monkeypatch):
    """When findlibs already locates ecCodes (conda/system), the shim leaves env untouched."""
    monkeypatch.setattr(os, "name", "nt")
    fake_findlibs = types.SimpleNamespace(find=lambda name: r"C:\conda\lib\eccodes.dll")
    monkeypatch.setitem(sys.modules, "findlibs", fake_findlibs)
    _eccodes.ensure_eccodes()
    assert "ECCODES_PYTHON_USE_FINDLIBS" not in os.environ


def test_noop_when_ecmwflibs_absent(monkeypatch):
    """On Windows with no resolvable library and no ecmwflibs, the shim no-ops cleanly."""
    monkeypatch.setattr(os, "name", "nt")
    fake_findlibs = types.SimpleNamespace(find=lambda name: None)
    monkeypatch.setitem(sys.modules, "findlibs", fake_findlibs)
    monkeypatch.setitem(sys.modules, "ecmwflibs", None)  # import yields None -> treated as absent
    monkeypatch.delitem(sys.modules, "ecmwflibs", raising=False)
    monkeypatch.setattr(_eccodes, "_done", False)
    # Force `import ecmwflibs` to raise ImportError.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _no_ecmwflibs(name, *args, **kwargs):
        if name == "ecmwflibs":
            raise ImportError("no ecmwflibs")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _no_ecmwflibs)
    _eccodes.ensure_eccodes()
    assert "ECCODES_PYTHON_USE_FINDLIBS" not in os.environ


def test_once_guard(monkeypatch):
    """A second call short-circuits even when the environment looks fresh."""
    monkeypatch.setattr(_eccodes, "_done", True)
    monkeypatch.setattr(os, "name", "nt")
    _eccodes.ensure_eccodes()
    assert "ECCODES_PYTHON_USE_FINDLIBS" not in os.environ
