"""Tests for the Argo parameter-family catalog."""

from __future__ import annotations

import pytest

from earthlens.argo import Catalog, Family

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
