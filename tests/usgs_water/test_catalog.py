"""Tests for the USGS Water parameter-code catalog."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earthlens.usgs_water import Catalog, Parameter
from earthlens.usgs_water import catalog as catalog_module
from earthlens.usgs_water.backend import SERVICES

pytestmark = pytest.mark.usgs_water


def test_resolve_friendly_name_to_code():
    """A friendly name resolves to its 5-digit code."""
    assert Catalog().resolve("discharge") == "00060"


def test_resolve_raw_code_passthrough():
    """A raw 5-digit code passes through unmapped."""
    assert Catalog().resolve("00060") == "00060"
    assert Catalog().resolve("99999") == "99999"


def test_resolve_unknown_name_raises_with_hint():
    """An unknown but close name raises with a did-you-mean hint."""
    with pytest.raises(ValueError, match="Did you mean 'discharge'"):
        Catalog().resolve("dischrge")


def test_get_parameter_returns_row():
    """get_parameter returns the full row with units and group."""
    param = Catalog().get_parameter("gage_height")
    assert param.code == "00065"
    assert param.units == "ft"
    assert param.group == "Physical"


def test_available_parameters_sorted():
    """available_parameters lists every curated key, sorted."""
    available = Catalog().available_parameters
    assert "discharge" in available
    assert available == sorted(available)


def test_curated_services_are_valid_service_names():
    """Every catalog `services` entry is a known service= value."""
    catalog = Catalog()
    for param in catalog.parameters.values():
        for service in param.services:
            assert service in SERVICES


def test_parameter_extra_forbidden():
    """Unknown fields on a Parameter row are rejected."""
    with pytest.raises(ValidationError):
        Parameter(code="00060", bogus="x")


def test_parameter_code_must_be_five_digits():
    """A non-5-digit code fails validation."""
    with pytest.raises(ValidationError):
        Parameter(code="600")


def test_load_rejects_empty_block(tmp_path):
    """Loading a YAML without a parameters block raises ValueError."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("other: {}\n")
    with pytest.raises(ValueError, match="parameters"):
        Catalog.load(empty)


def test_load_rejects_bad_row(tmp_path):
    """A row with an invalid code fails validation with the param name."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("parameters:\n  flow: {code: '6'}\n")
    with pytest.raises(ValueError, match="'flow' failed validation"):
        Catalog.load(bad)


def test_get_catalog_returns_parameters():
    """get_catalog returns the same parameter map."""
    catalog = Catalog()
    assert catalog.get_catalog() is catalog.parameters


def test_parse_cache_reuses_rows_until_cleared():
    """Parsing is cached on (path, mtime); clearing forces a re-parse."""
    catalog_module.clear_catalog_cache()
    Catalog.load()
    assert catalog_module._CATALOG_CACHE, "load() should populate the cache"
    catalog_module.clear_catalog_cache()
    assert not catalog_module._CATALOG_CACHE, "clear_catalog_cache() empties it"
