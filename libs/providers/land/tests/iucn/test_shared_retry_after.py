"""The IUCN and WDPA REST shims re-export core's `parse_retry_after`.

Both land backends promise to reuse the shared helper rather than reimplement
RFC 9110 §10.2.3 parsing. The promise is theirs, so the test is theirs: it used
to live in `libs/core/tests/biodiversity/`, reaching into `earthlens.iucn._rest`
and `earthlens.wdpa._rest` from the distribution that must not depend on any
provider.

Both are checked here, in one place, because the assertion is precisely that
the two shims resolve to the *same* object — splitting it per backend would
lose that.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_both_shims_reach_the_same_function() -> None:
    """The IUCN and WDPA shims re-export the shared helper as the same object."""
    from earthlens.biodiversity import parse_retry_after
    from earthlens.iucn._rest import _parse_retry_after as iucn_alias
    from earthlens.wdpa._rest import _parse_retry_after as wdpa_alias

    assert iucn_alias is parse_retry_after, (
        "iucn._rest must re-export the shared parse_retry_after"
    )
    assert wdpa_alias is parse_retry_after, (
        "wdpa._rest must re-export the shared parse_retry_after"
    )
