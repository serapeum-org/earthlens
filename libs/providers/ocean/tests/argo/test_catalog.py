"""Tests for the Argo parameter-family catalog."""

from __future__ import annotations

import pytest

from earthlens.argo import Catalog, Family, clear_catalog_cache

pytestmark = pytest.mark.argo


def test_families_present():
    """The bundled catalog ships the phy and bgc families."""
    cat = Catalog()
    assert sorted(cat.datasets) == ["bgc", "phy"]
    assert isinstance(cat.get_family("phy"), Family)


def test_parameters_for_phy():
    """The phy family carries the core physical parameters."""
    params = Catalog().parameters_for("phy")
    assert {"TEMP", "PSAL", "PRES"} <= params


def test_parameters_for_bgc_nonempty():
    """The bgc family carries the biogeochemical parameters."""
    params = Catalog().parameters_for("bgc")
    assert "DOXY" in params
    assert "CHLA" in params


def test_validate_parameters_ok():
    """Known parameters validate silently."""
    Catalog().validate_parameters(["TEMP", "PSAL"], "phy")


def test_validate_parameters_did_you_mean():
    """An unknown but close parameter raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'TEMP'"):
        Catalog().validate_parameters(["TEMPP"], "phy")


def test_unknown_family_raises():
    """An unknown family name raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'phy'"):
        Catalog().parameters_for("phys")


def test_load_uses_then_clears_cache():
    """A second load hits the parse cache; clearing it forces a re-parse."""
    clear_catalog_cache()
    first = Catalog.load()
    cached = Catalog.load()
    assert sorted(first.datasets) == sorted(cached.datasets)
    clear_catalog_cache()
    reloaded = Catalog.load()
    assert sorted(reloaded.datasets) == sorted(first.datasets)


def test_missing_families_block_raises(tmp_path):
    """A YAML without a `families:` block raises a clear error."""
    bad = tmp_path / "argo_data_catalog.yaml"
    bad.write_text("regions: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="families"):
        Catalog.load(bad)


def test_malformed_family_row_raises(tmp_path):
    """A family row with an unexpected key fails validation with a clear error."""
    bad = tmp_path / "argo_data_catalog.yaml"
    bad.write_text("families:\n  phy:\n    bogus: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="failed validation"):
        Catalog.load(bad)
