"""Coverage for `earthlens.cli.refresh` network/SDK helpers (all mocked)."""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest
import yaml

from earthlens.cli import refresh as refresh_mod
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import coverage_one

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


class TestGetText:
    """Tests for the shared `_get_text` HTTP helper."""

    def test_get_text_returns_body(self, monkeypatch):
        """_get_text returns the response body text."""
        monkeypatch.setattr(
            refresh_mod.requests,
            "get",
            lambda url, timeout=None: SimpleNamespace(
                text="BODY", raise_for_status=lambda: None
            ),
        )
        assert refresh_mod._get_text("https://x") == "BODY"
