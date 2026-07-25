"""CLI surface wiring for jaxa (validate / probe / curate stanza)."""

from __future__ import annotations

import sys
import types

import pytest

from earthlens.cli.curate import _PROBERS
from earthlens.cli.stanza import _EMITTERS
from earthlens.cli.validate import _VALIDATORS
from earthlens.jaxa.catalog import Catalog

pytestmark = [pytest.mark.jaxa, pytest.mark.unit]


@pytest.fixture
def catalog() -> Catalog:
    """The bundled jaxa catalog (no network)."""
    return Catalog()


def test_validators_registry_has_jaxa() -> None:
    """`earthlens datasets validate jaxa` is wired."""
    assert "jaxa" in _VALIDATORS


def test_probers_registry_has_jaxa() -> None:
    """`earthlens datasets probe jaxa <id>` is wired."""
    assert "jaxa" in _PROBERS


def test_emitters_registry_has_jaxa() -> None:
    """`earthlens datasets curate jaxa <id>` is wired."""
    assert "jaxa" in _EMITTERS


def test_validate_clean_catalog_has_no_issues(catalog: Catalog) -> None:
    """The bundled catalog validates cleanly (no protocol drift)."""
    checked, issues = _VALIDATORS["jaxa"](catalog)
    assert checked == len(catalog.datasets)
    assert issues == [], issues


def test_probe_jaxa_earth_returns_band_roles(
    catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A jaxa-earth row probe returns one entry per band, role='band'."""
    key, row = next(
        (k, r) for k, r in catalog.datasets.items() if r.protocol == "jaxa-earth"
    )

    class _FakeList:
        def filter_name(self):  # noqa: D401
            return [row.collection], [["B1", "B2"]]

    fake_je = types.SimpleNamespace(ImageCollectionList=lambda: _FakeList())
    fake_earth = types.SimpleNamespace(je=fake_je)
    monkeypatch.setitem(sys.modules, "jaxa", types.SimpleNamespace(earth=fake_earth))
    monkeypatch.setitem(sys.modules, "jaxa.earth", fake_earth)

    schema = _PROBERS["jaxa"](catalog, key)
    assert schema == {"B1": {"role": "band"}, "B2": {"role": "band"}}


def test_emit_jaxa_earth_seeds_default_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """A STAC name seeds protocol=jaxa-earth with the first band as default."""

    class _FakeList:
        def filter_name(self):  # noqa: D401
            return ["JAXA.AW3D30.v3.2"], [["DSM", "MSK"]]

    fake_je = types.SimpleNamespace(ImageCollectionList=lambda: _FakeList())
    fake_earth = types.SimpleNamespace(je=fake_je)
    monkeypatch.setitem(sys.modules, "jaxa", types.SimpleNamespace(earth=fake_earth))
    monkeypatch.setitem(sys.modules, "jaxa.earth", fake_earth)

    row = _EMITTERS["jaxa"](None, "JAXA.AW3D30.v3.2")
    assert row == {
        "protocol": "jaxa-earth",
        "collection": "JAXA.AW3D30.v3.2",
        "default_band": "DSM",
    }


def test_emit_gportal_numeric_id_seeds_short_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 7–9 digit id seeds protocol=gportal with mission/level in the description."""
    fake_gportal = types.SimpleNamespace(
        datasets=lambda: {"GCOM-W/AMSR2": {"LEVEL1": ["11001002"]}},
    )
    monkeypatch.setitem(sys.modules, "gportal", fake_gportal)

    row = _EMITTERS["jaxa"](None, "11001002")
    assert row == {
        "protocol": "gportal",
        "short_name": "11001002",
        "description": "GCOM-W/AMSR2 / LEVEL1",
    }
