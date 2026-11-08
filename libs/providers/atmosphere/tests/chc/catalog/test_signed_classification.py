"""Completeness lock-in: every genuinely-signed CHC variable is marked `signed` (M1)."""

from __future__ import annotations

import pytest

from earthlens.chc import Catalog

pytestmark = [pytest.mark.chc]

#: Standardised indices are dimensionless and signed by construction.
_SIGNED_INDICES = {"spi", "spei"}


def _expected_signed(name: str, units: str) -> bool:
    """A CHC variable carries real negatives iff it is a degC temperature, a delta, an anomaly, or an index."""
    return (
        units == "degC"
        or name.endswith("_delta")
        or name.endswith("_anomaly")
        or name in _SIGNED_INDICES
    )


@pytest.fixture(scope="module")
def bundled_catalog() -> Catalog:
    """Bundled catalog, loaded once per module."""
    return Catalog()


class TestSignedClassification:
    """`signed` is set exactly for the variables that carry genuine negatives (M1)."""

    def test_signed_flag_matches_the_classification_rule(
        self, bundled_catalog: Catalog
    ):
        """signed is true for every degC / delta / anomaly / SPI-SPEI variable and no other."""
        wrong: list[str] = []
        for key, ds in bundled_catalog.datasets.items():
            for var in ds.variables.values():
                expected = _expected_signed(var.name, var.units)
                if var.signed != expected:
                    wrong.append(
                        f"{key}/{var.name} (units={var.units}): "
                        f"signed={var.signed}, expected={expected}"
                    )
        assert wrong == [], "signed classification drifted:\n" + "\n".join(
            sorted(set(wrong))
        )
