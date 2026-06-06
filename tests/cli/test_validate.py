"""Unit tests for `earthlens.cli.validate`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from earthlens.cli.adapter import list_backends
from earthlens.cli.validate import (
    ValidateResult,
    _validate_nwp,
    _validate_overture,
    _validate_radar,
    supported_providers,
    validate_one,
)

pytestmark = pytest.mark.cli


def _info(provider):
    """Return the BackendInfo for a provider id."""
    return next(b for b in list_backends() if b.provider == provider)


_CURATED_ENUM = (
    "nwp",
    "s3",
    "ghsl",
    "overture",
    "fdsn",
    "firms",
    "radar",
    "tropycal",
    "gdacs",
    "chc",
)


class TestSupportedProviders:
    """Tests for supported_providers."""

    def test_all_curated_enumeration_providers_supported(self):
        """Every curated-enumeration provider has a validator wired up."""
        assert set(_CURATED_ENUM) <= set(supported_providers())


class TestBundledCatalogsLintClean:
    """The shipped catalogs should pass their own structural lint."""

    @pytest.mark.parametrize("provider", _CURATED_ENUM)
    def test_provider_validates_clean(self, provider):
        """Each bundled curated-enumeration catalog has no validation issues.

        Args:
            provider: The curated-enumeration provider to validate.
        """
        result = validate_one(_info(provider))
        assert result.status == "ok", f"{provider} validator errored: {result.detail}"
        assert result.issues == [], f"{provider} issues: {result.issues}"
        assert result.checked > 0, f"{provider} checked nothing"


class TestValidateNwp:
    """Tests for the nwp structural lint."""

    def test_clean_catalog_has_no_issues(self):
        """The bundled nwp catalog passes its own structural lint."""
        checked, issues = _validate_nwp(load_nwp())
        assert checked > 0 and issues == [], f"unexpected nwp issues: {issues}"

    def test_flags_missing_url_template(self):
        """A direct-https model with no url_template is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="direct-https",
                    url_template="",
                    bands={"t": 1},
                    cycles_utc=[0],
                    model_family="x",
                )
            }
        )
        checked, issues = _validate_nwp(catalog)
        assert checked == 1 and any("url_template" in i for i in issues)

    def test_flags_empty_bands_and_bad_cycle(self):
        """An empty band map and an out-of-range cycle hour are flagged."""
        catalog = SimpleNamespace(
            datasets={
                "bad": SimpleNamespace(
                    backend="herbie",
                    model_family="m",
                    url_template=None,
                    bands={},
                    cycles_utc=[0, 99],
                )
            }
        )
        _checked, issues = _validate_nwp(catalog)
        assert any("empty band map" in i for i in issues), "empty bands flagged"
        assert any("out of range" in i for i in issues), "bad cycle flagged"


class TestStructuralLints:
    """Negative cases for the structural validators."""

    def test_overture_default_type_must_be_in_types(self):
        """A default_type not among types is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "x": SimpleNamespace(types=["a", "b"], default_type="c"),
            }
        )
        _checked, issues = _validate_overture(catalog)
        assert any("default_type" in i for i in issues), "mismatch flagged"

    def test_radar_out_of_range_coords_flagged(self):
        """A station with an impossible latitude is flagged."""
        catalog = SimpleNamespace(
            datasets={
                "KXXX": SimpleNamespace(name="X", latitude=999.0, longitude=0.0),
            }
        )
        _checked, issues = _validate_radar(catalog)
        assert any("latitude" in i for i in issues), "bad latitude flagged"


class TestValidateOne:
    """Tests for validate_one."""

    def test_unsupported_provider(self):
        """A catalog-backed provider (uses refresh/audit) has no validator."""
        assert validate_one(_info("cmems")).status == "unsupported"

    def test_nwp_ok(self):
        """nwp validates clean end-to-end."""
        result = validate_one(_info("nwp"))
        assert result.status == "ok" and result.issues == []
        assert result.checked > 0, "models were checked"


class TestValidateResult:
    """Tests for ValidateResult."""

    def test_to_dict(self):
        """to_dict carries checked + issues."""
        data = ValidateResult("nwp", "ok", checked=3, issues=["x"]).to_dict()
        assert data["checked"] == 3 and data["issues"] == ["x"]


def load_nwp():
    """Load the real nwp catalog (helper for the lint test)."""
    from earthlens.cli.adapter import load_catalog

    return load_catalog(_info("nwp"))
