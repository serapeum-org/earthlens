"""Tests for routing the HDX backend through the EarthLens facade."""

from __future__ import annotations

import pytest

import earthlens.hdx
from earthlens.earthlens import EarthLens

from .conftest import FakeHdx, FakeResource

pytestmark = pytest.mark.hdx


class TestRegistry:
    """Tests for the `"hdx"` registry entry."""

    def test_key_present(self):
        """The hdx key is registered alongside the other backends."""
        assert "hdx" in EarthLens.DataSources

    def test_key_resolves_to_hdx_class(self):
        """The key resolves to earthlens.hdx.HDX."""
        assert EarthLens.DataSources["hdx"] is earthlens.hdx.HDX


class TestFacadeConstruction:
    """Tests for `EarthLens(data_source="hdx", ...)`."""

    def test_constructs_backend(self, fake_hdx: FakeHdx, tmp_path):
        """The facade builds the HDX backend with the standard arguments."""
        facade = EarthLens(
            data_source="hdx",
            variables={"kontur-population": []},
            path=tmp_path,
        )
        assert isinstance(facade.datasource, earthlens.hdx.HDX)
        assert facade.datasource.OUTPUT_KIND == "mixed"

    def test_forwards_escape_hatch_kwargs(self, fake_hdx: FakeHdx, tmp_path):
        """hdx_id= / resource= flow through the facade to the backend."""
        facade = EarthLens(
            data_source="hdx",
            variables={},
            hdx_id="arbitrary-id",
            resource="*.tif",
            path=tmp_path,
        )
        assert facade.datasource._targets == [("arbitrary-id", ["*.tif"])]

    def test_download_through_facade(self, fake_hdx: FakeHdx, tmp_path):
        """A facade download routes to the backend and returns file paths."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        facade = EarthLens(data_source="hdx", variables={}, hdx_id="d", path=tmp_path)
        paths = facade.download()
        assert [p.name for p in paths] == ["a.csv"]

    def test_facade_forwards_aggregate_backend_rejects(self, fake_hdx, tmp_path):
        """The facade forwards aggregate= for mixed; the backend rejects it (`G1`)."""
        facade = EarthLens(
            data_source="hdx",
            variables={"kontur-population": []},
            path=tmp_path,
        )
        with pytest.raises(NotImplementedError, match="aggregate="):
            facade.download(aggregate=object())
