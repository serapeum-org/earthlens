"""Unit tests for the Sentinel Hub catalog tooling (`tools/sentinel_hub/`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "sentinel_hub"
sys.path.insert(0, str(_TOOLS_DIR))

import probe_sh_collection as probe  # noqa: E402

pytestmark = pytest.mark.sentinel_hub


class TestProbe:
    """`probe` reports curated + enum membership (against the real enum)."""

    def test_probe_curated_key(self, capsys):
        """Probing a curated key prints its binding + bands."""
        assert probe.main(["sentinel-2-l2a"]) == 0
        assert "SENTINEL2_L2A" in capsys.readouterr().out

    def test_probe_yaml_stanza(self, capsys):
        """`--yaml` emits a collections.yaml stanza."""
        assert probe.main(["sentinel-2-l2a", "--yaml"]) == 0
        assert "collections:" in capsys.readouterr().out

    def test_probe_uncurated_enum_member(self, capsys):
        """An enum member that is not curated is reported, exit 0."""
        assert probe.main(["LANDSAT_OT_L2"]) == 0
        assert "not yet curated" in capsys.readouterr().out

    def test_probe_unknown_name(self):
        """A name that is neither curated nor an enum member exits 1."""
        assert probe.main(["totally-bogus"]) == 1
