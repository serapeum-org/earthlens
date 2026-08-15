"""Unit tests for the lazy ISIMIP client factory."""

from __future__ import annotations

import builtins

import pytest

from earthlens.isimip._client import build_client

pytestmark = [pytest.mark.isimip, pytest.mark.unit]


class TestBuildClient:
    """Tests for `build_client`."""

    def test_builds_real_client_with_expected_methods(self):
        """With the SDK installed, the built client exposes the three methods."""
        client = build_client(
            "https://data.isimip.org/api/v1", "https://files.isimip.org/api/v2"
        )
        for method in ("datasets", "cutout_bbox", "download"):
            assert callable(getattr(client, method)), method

    def test_passes_urls_to_sdk(self):
        """The data / files-API URLs reach the constructed SDK client."""
        client = build_client("http://data.example", "http://files.example")
        assert client.base_url == "http://data.example", client.base_url
        assert client.files_api_url == "http://files.example", client.files_api_url

    def test_missing_sdk_raises_helpful_error(self, monkeypatch):
        """A missing `isimip-client` raises a ModuleNotFoundError naming the extra."""
        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name.startswith("isimip_client"):
                raise ModuleNotFoundError("No module named 'isimip_client'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        with pytest.raises(
            ModuleNotFoundError, match=r"\[isimip\] extra|isimip. extra|isimip"
        ):
            build_client("http://d", "http://f")
