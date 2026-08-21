"""Tests for `earthlens.gee._eedai` — the guarded pyramids-eo reader loader."""

from __future__ import annotations

from types import SimpleNamespace

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


class _RecordingCredentials:
    """Records which `EarthEngineCredentials` constructor the adapter picked."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def application_default(self):
        self.calls.append(("application_default", None))
        return "adc"

    def from_service_account_info(self, info):
        self.calls.append(("from_service_account_info", info))
        return "inline"

    def from_service_account(self, path):
        self.calls.append(("from_service_account", path))
        return "path"


@pytest.fixture
def recording_credentials(monkeypatch):
    """Swap the reader module for one whose credentials class records calls."""
    credentials = _RecordingCredentials()
    module = SimpleNamespace(EarthEngineCredentials=credentials)
    monkeypatch.setattr(_eedai, "import_earthengine_reader", lambda: module)
    return credentials


class TestCredentialsFor:
    """Tests for `credentials_for`."""

    def test_none_falls_back_to_application_default(self, recording_credentials):
        """No key at all resolves through Application Default Credentials."""
        assert _eedai.credentials_for(None) == "adc"
        assert recording_credentials.calls == [("application_default", None)]

    def test_inline_json_uses_service_account_info(self, recording_credentials):
        """A JSON payload is passed to `from_service_account_info` verbatim."""
        payload = '  {"type": "service_account"}'
        assert _eedai.credentials_for(payload) == "inline"
        assert recording_credentials.calls == [("from_service_account_info", payload)]

    def test_path_uses_service_account_file(self, recording_credentials):
        """Anything else is treated as a path to the key file."""
        assert _eedai.credentials_for("/keys/sa.json") == "path"
        assert recording_credentials.calls == [
            ("from_service_account", "/keys/sa.json")
        ]
