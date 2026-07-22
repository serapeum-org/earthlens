"""Unit tests for the CLI's `_jaxa_grouped` refresh walker."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from earthlens.cli.refresh import _jaxa_grouped

pytestmark = [pytest.mark.jaxa, pytest.mark.unit]


def _install_fake_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `jaxa.earth.je` + `gportal` for the refresh walker.

    The walker enumerates jaxa-earth via `je.ImageCollectionList()`
    and gportal via `gportal.datasets()`; both are replaced with tiny
    fakes so no network is touched.
    """
    fake_je = SimpleNamespace(
        ImageCollectionList=lambda: SimpleNamespace(
            filter_name=lambda: (["JAXA.EORC.TEST", "JAXA.EORC.OTHER"], [[], []]),
        ),
    )
    fake_earth = SimpleNamespace(je=fake_je)
    monkeypatch.setitem(sys.modules, "jaxa", types.SimpleNamespace(earth=fake_earth))
    monkeypatch.setitem(sys.modules, "jaxa.earth", fake_earth)
    fake_gportal = SimpleNamespace(
        datasets=lambda: {"GCOM-W/AMSR2": {"LEVEL1": ["11001002", "11001003"]}},
    )
    monkeypatch.setitem(sys.modules, "gportal", fake_gportal)


def test_ptree_group_derives_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `ptree` group reflects the catalog's `protocol: ptree` rows."""
    _install_fake_sdks(monkeypatch)
    row = SimpleNamespace(protocol="ptree", short_name="himawari-ahi-fldk")
    other = SimpleNamespace(protocol="gportal", short_name="11001002")
    catalog = SimpleNamespace(datasets={"h": row, "o": other})

    grouped = _jaxa_grouped(catalog)
    assert grouped["ptree"] == ["himawari-ahi-fldk"]


def test_ptree_group_picks_up_new_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A curator adding a second `ptree` row lands in the refresh group automatically."""
    _install_fake_sdks(monkeypatch)
    rows = {
        "h1": SimpleNamespace(protocol="ptree", short_name="himawari-ahi-fldk"),
        "h2": SimpleNamespace(protocol="ptree", short_name="himawari-ahi-target-area"),
    }
    catalog = SimpleNamespace(datasets=rows)

    grouped = _jaxa_grouped(catalog)
    assert grouped["ptree"] == [
        "himawari-ahi-fldk",
        "himawari-ahi-target-area",
    ]


def test_ptree_group_empty_when_no_ptree_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog without `ptree` rows yields an empty `ptree` group, not a KeyError."""
    _install_fake_sdks(monkeypatch)
    rows = {"o": SimpleNamespace(protocol="gportal", short_name="11001002")}
    catalog = SimpleNamespace(datasets=rows)

    grouped = _jaxa_grouped(catalog)
    assert grouped["ptree"] == []
