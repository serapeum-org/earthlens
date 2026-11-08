"""Overture re-exports the shared `LicenseWarning`, rather than defining its own.

The promise is Overture's, so the test is Overture's. It used to live in
`libs/core/tests/biodiversity/`, where it reached into `earthlens.overture._helpers`
— a provider's private module — from the distribution that must not depend on
any provider. Core's guard enforces the forward direction (no provider may
import a private core symbol); this is the reverse, and it belongs here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.overture


def test_identity_across_import_paths() -> None:
    """The helper module and the public re-export are the one shared class."""
    from earthlens.biodiversity import LicenseWarning
    from earthlens.overture import LicenseWarning as overture_public
    from earthlens.overture._helpers import LicenseWarning as overture_helper

    assert overture_helper is LicenseWarning, (
        "overture._helpers must re-export the shared class, not define its own"
    )
    assert overture_public is LicenseWarning, (
        "the public overture surface must expose the same shared class"
    )
