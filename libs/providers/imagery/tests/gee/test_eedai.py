"""Tests for `earthlens.gee._eedai` — the guarded pyramids-eo reader loader."""

from __future__ import annotations

import pytest

from earthlens.gee import _eedai

_READER_EXPORTS = (
    "from_earthengine",
    "collection_from_earthengine",
    "EarthEngineCredentials",
)


def _raise_import_error(name):
    """Stand in for `importlib.import_module`, always failing as if uninstalled."""
    raise ImportError(f"No module named {name!r}")


class TestImportEarthengineReader:
    """Tests for `import_earthengine_reader`."""

    def test_returns_reader_module_when_installed(self):
        """With the `[eedai]` extra installed, the pyramids-eo reader is returned."""
        module = _eedai.import_earthengine_reader()
        assert module.__name__ == "pyramids_eo.earthengine"
        for name in _READER_EXPORTS:
            assert hasattr(module, name), name

    def test_missing_extra_raises_friendly_import_error(self, monkeypatch):
        """A missing pyramids-eo surfaces as an ImportError naming the extra."""
        monkeypatch.setattr(_eedai.importlib, "import_module", _raise_import_error)
        with pytest.raises(ImportError, match=r"pip install earthlens\[eedai\]"):
            _eedai.import_earthengine_reader()


class TestEedaiAvailable:
    """Tests for `eedai_available`."""

    def test_true_when_installed(self):
        """Returns True when the reader imports."""
        assert _eedai.eedai_available() is True

    def test_false_when_missing(self, monkeypatch):
        """Returns False, without raising, when the reader import fails."""
        monkeypatch.setattr(_eedai.importlib, "import_module", _raise_import_error)
        assert _eedai.eedai_available() is False
