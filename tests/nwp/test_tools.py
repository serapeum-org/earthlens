"""Unit tests for the NWP catalog tooling (offline)."""

from __future__ import annotations

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

from earthlens.nwp.catalog import NWPModel

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools" / "nwp"
sys.path.insert(0, str(_TOOLS_DIR))

import probe_nwp_model as probe  # noqa: E402

pytestmark = [pytest.mark.nwp, pytest.mark.unit]


class TestProbeTool:
    """Tests for tools/nwp/probe_nwp_model.py dispatch (no network)."""

    def test_direct_https_probe_builds_url(self, monkeypatch):
        """The direct-HTTPS probe HEADs the first band's URL."""
        calls = {}

        class _Resp:
            status_code = 200

        def fake_head(url, timeout=None, allow_redirects=None):
            calls["url"] = url
            return _Resp()

        module = types.ModuleType("requests")
        module.head = fake_head
        monkeypatch.setitem(sys.modules, "requests", module)
        model = NWPModel(
            provider="dwd-opendata",
            backend="direct-https",
            cycles_utc=[0],
            url_template="https://x/{var_lc}/f{step:03d}_{var}.bz2",
            bands={"temperature_2m": "T_2M"},
        )
        result = probe._probe_direct_https(model, dt.datetime(2024, 6, 1, 0), 0)
        assert "HTTP 200" in result
        assert calls["url"] == "https://x/t_2m/f000_T_2M.bz2"

    def test_herbie_probe_reports_unavailable(self, monkeypatch):
        """When herbie can't import, the probe says so rather than raising."""
        monkeypatch.setitem(sys.modules, "herbie", None)
        model = NWPModel(provider="noaa-nodd", model_family="gfs", backend="herbie")
        result = probe._probe_herbie(model, dt.datetime(2024, 6, 1, 0), 0)
        assert "herbie unavailable" in result
