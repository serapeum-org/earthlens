"""Unit tests for the HDX read-only configuration guard."""

from __future__ import annotations

import sys

import pytest

from earthlens.hdx import HDX

from .conftest import FakeConfiguration, FakeHdx

pytestmark = pytest.mark.hdx


class TestInitializeConfig:
    """Tests for HDX._initialize (Configuration singleton handling)."""

    def test_create_called_once(self, fake_hdx: FakeHdx, tmp_path):
        """The first instance creates the read-only configuration once."""
        HDX(variables={"kontur-population": []}, path=tmp_path)
        assert len(FakeConfiguration.create_calls) == 1
        kwargs = FakeConfiguration.create_calls[0]
        assert kwargs["hdx_read_only"] is True
        assert kwargs["user_agent"] == "earthlens"
        assert kwargs["hdx_site"] == "prod"

    def test_second_instance_does_not_recreate(self, fake_hdx: FakeHdx, tmp_path):
        """A second instance reuses the existing configuration (no re-create)."""
        HDX(variables={"kontur-population": []}, path=tmp_path)
        HDX(variables={"kontur-population": []}, path=tmp_path)
        assert len(FakeConfiguration.create_calls) == 1

    def test_custom_site_and_user_agent(self, fake_hdx: FakeHdx, tmp_path):
        """hdx_site / user_agent kwargs flow into Configuration.create."""
        HDX(
            variables={"kontur-population": []},
            path=tmp_path,
            hdx_site="stage",
            user_agent="custom-agent",
        )
        kwargs = FakeConfiguration.create_calls[0]
        assert kwargs["hdx_site"] == "stage"
        assert kwargs["user_agent"] == "custom-agent"

    def test_missing_extra_raises_friendly_import_error(self, monkeypatch, tmp_path):
        """A missing `hdx` SDK surfaces a friendly ImportError naming the extra."""
        for name in (
            "hdx",
            "hdx.api",
            "hdx.api.configuration",
            "hdx.data",
            "hdx.data.dataset",
        ):
            monkeypatch.setitem(sys.modules, name, None)
        with pytest.raises(ImportError, match=r"earthlens\[hdx\]"):
            HDX(variables={"kontur-population": []}, path=tmp_path)
