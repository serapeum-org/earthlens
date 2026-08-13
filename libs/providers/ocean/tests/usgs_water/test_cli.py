"""Tests for the USGS Water catalog-tooling handlers (`earthlens.usgs_water.cli`).

Moved out of core's CLI test suite when the USGS Water refresh/write/emit/validate
handlers moved into this distribution (issue #863).
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
import yaml

import earthlens.usgs_water.cli as usgs_cli
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import audit_one, refresh_one
from earthlens.cli.stanza import emit_stanza
from earthlens.cli.validate import validate_one
from earthlens.usgs_water import catalog as usgs_catalog

pytestmark = pytest.mark.cli


def _info():
    """Return the BackendInfo for the usgs_water backend."""
    return next(b for b in list_backends() if b.provider == "usgs_water")


class TestRefresher:
    """Tests for the USGS Water (dataretrieval) lister."""

    def test_lists_parameter_codes(self, monkeypatch):
        """usgs_water refresh reads the reference-table parameter codes."""
        monkeypatch.setattr(
            usgs_cli, "_parameter_codes", lambda: ["00060", "00065", "00060"]
        )
        outcome = refresh_one(_info())
        assert outcome.status == "ok", "usgs_water refresh ran"
        assert outcome.live_count == 2, "deduped codes"

    def test_audit_curated_codes_not_broken(self, monkeypatch):
        """Curated codes present live are not flagged broken."""
        monkeypatch.setattr(
            usgs_cli, "_parameter_codes", lambda: ["00060", "00065", "00010"]
        )
        outcome = audit_one(_info())
        assert "00060" not in outcome.broken, "a live curated code is not broken"


class TestWriter:
    """Tests for the sibling parameter-table writer."""

    def test_writes_parameter_table_sibling(self, tmp_path, monkeypatch):
        """usgs_water --write persists the full reference table to a sibling."""
        dst = tmp_path / usgs_catalog.CATALOG_PATH.name
        shutil.copy(usgs_catalog.CATALOG_PATH, dst)
        monkeypatch.setattr(usgs_catalog, "CATALOG_PATH", dst)
        monkeypatch.setattr(
            usgs_cli,
            "_parameter_rows",
            lambda: {"00060": {"name": "Discharge", "group": "PHY", "unit": "ft3/s"}},
        )
        path = usgs_cli.writer(_info(), {"usgs_water": ["00060"]})
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        assert data["available_parameters"]["00060"]["unit"] == "ft3/s", "table written"


class TestParameterTable:
    """Tests for the USGS reference-table parsers (dataretrieval mocked)."""

    def _patch_frame(self, monkeypatch, rows):
        """Patch dataretrieval to return a tiny pandas frame of `rows`."""
        import pandas as pd
        from dataretrieval import waterdata

        monkeypatch.setattr(
            waterdata, "get_reference_table", lambda collection=None: pd.DataFrame(rows)
        )

    def test_codes_listed(self, monkeypatch):
        """_parameter_codes returns every parameter_code as a string."""
        self._patch_frame(monkeypatch, [{"parameter_code": 60}, {"parameter_code": 10}])
        assert usgs_cli._parameter_codes() == ["60", "10"], "codes stringified"

    def test_rows_keyed_by_code(self, monkeypatch):
        """_parameter_rows keys name/group/unit by the parameter code."""
        self._patch_frame(
            monkeypatch,
            [
                {
                    "parameter_code": "00060",
                    "parameter_name": "Discharge",
                    "parameter_group_code": "PHY",
                    "unit_of_measure": "ft3/s",
                }
            ],
        )
        rows = usgs_cli._parameter_rows()
        assert rows["00060"]["name"] == "Discharge", "name parsed"
        assert rows["00060"]["unit"] == "ft3/s", "unit parsed"

    def test_blank_codes_skipped(self, monkeypatch):
        """A row with no usable code is dropped."""
        self._patch_frame(
            monkeypatch, [{"parameter_code": ""}, {"parameter_code": "1"}]
        )
        assert list(usgs_cli._parameter_rows()) == ["1"], "blank code dropped"


class TestValidator:
    """Tests for the USGS Water service-name validator."""

    def test_validates_clean(self):
        """Every curated USGS parameter's services are known service names."""
        result = validate_one(_info())
        assert result.status == "ok"
        assert result.issues == []
        assert result.checked > 0, "parameters were checked"

    def test_unknown_service_flagged(self):
        """A parameter declaring an unknown service name is flagged."""
        catalog = SimpleNamespace(
            datasets={"x": SimpleNamespace(services=["daily", "bogus"])}
        )
        _checked, issues = usgs_cli.validator(catalog)
        assert any("bogus" in issue for issue in issues), "unknown service flagged"


class TestEmitter:
    """Tests for the USGS Water emitter (pure args, no network)."""

    def test_seeds_row_from_args(self):
        """The row is built from the code + options without any fetch."""
        result = emit_stanza(
            _info(),
            "00060",
            key="discharge",
            name="Discharge",
            units="ft3/s",
            services=["daily"],
        )
        assert result.status == "ok", "usgs emitter ran"
        assert result.row == {
            "code": "00060",
            "name": "Discharge",
            "units": "ft3/s",
            "group": "Physical",
            "services": ["daily"],
        }

    def test_name_defaults_from_key(self):
        """An omitted name is titled from the key."""
        result = emit_stanza(_info(), "00010", key="water_temp")
        assert result.row["name"] == "Water Temp", "key titled into a name"
