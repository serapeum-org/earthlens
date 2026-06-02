"""Unit tests for the HDX resource-probe tool (faked SDK)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from ..conftest import FakeHdx, FakeResource

pytestmark = pytest.mark.hdx

_TOOLS = Path(__file__).resolve().parents[3] / "tools" / "hdx"


def _load_probe():
    """Import tools/hdx/probe_hdx_resource.py by path."""
    sys.path.insert(0, str(_TOOLS))
    spec = importlib.util.spec_from_file_location(
        "probe_hdx_resource", _TOOLS / "probe_hdx_resource.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_tool = _load_probe()


class TestProbe:
    """Tests for probe (read a dataset, summarise its resources)."""

    def test_summarises_resources(self, fake_hdx: FakeHdx):
        """The sidecar records distinct formats and inferred output kinds."""
        fake_hdx.add_dataset(
            "d", [FakeResource("a.gpkg", "Geopackage"), FakeResource("b.csv", "CSV")]
        )
        record = probe_tool.probe("d")
        assert record["resource_count"] == 2
        assert record["formats"] == ["CSV", "Geopackage"]
        assert record["output_kinds"] == ["tabular", "vector"]
        assert record["resources"][0]["name"] == "a.gpkg"

    def test_missing_dataset_raises(self, fake_hdx: FakeHdx):
        """An unknown id raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            probe_tool.probe("ghost")

    def test_download_writes_file(self, fake_hdx: FakeHdx, tmp_path):
        """--download fetches the first resource and records its path."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        record = probe_tool.probe("d", download_to=str(tmp_path))
        assert Path(record["downloaded"]).exists()

    def test_main_prints_json(self, fake_hdx: FakeHdx, capsys):
        """The CLI prints a JSON sidecar for a dataset."""
        fake_hdx.add_dataset("d", [FakeResource("a.csv", "CSV")])
        assert probe_tool.main(["d"]) == 0
        out = capsys.readouterr().out
        assert '"hdx_id": "d"' in out
