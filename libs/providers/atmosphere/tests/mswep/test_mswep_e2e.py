"""Live end-to-end tests against a real approved GloH2O Drive share.

Selected with `-m "e2e and mswep"`; the default run deselects them.
They need access the maintainers cannot provision automatically: a
GloH2O request form must be approved for **your own** Google account
(one form per product, `gloh2o.org/mswep` and `gloh2o.org/mswx`), and
the resulting share id plus a **user** OAuth credential must be
configured — see `docs/reference/mswep/authentication.md`.

The tests `skip` rather than fail when that configuration is absent, so
a contributor without access still gets a green run. That is a
deliberate exception to the repo's marker-not-environment rule: the
marker decides whether the tests are *selected*, and the skip only
reports that the selected tests cannot run on this machine.
"""

from __future__ import annotations

import os
import warnings

import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.mswep import MSWEP, MswepCredentials
from earthlens.mswep.auth import (
    FOLDER_ID_ENV,
    RCLONE_REMOTE_ENV,
    TOKEN_FILE_ENV,
)

pytestmark = [pytest.mark.e2e, pytest.mark.mswep]


def _configured() -> bool:
    """Return whether this machine has an approved share configured."""
    has_folder = bool(os.getenv(FOLDER_ID_ENV))
    has_credential = bool(os.getenv(TOKEN_FILE_ENV) or os.getenv(RCLONE_REMOTE_ENV))
    return has_folder and has_credential


requires_share = pytest.mark.skipif(
    not _configured(),
    reason=(
        f"no approved GloH2O share configured: set ${FOLDER_ID_ENV} plus "
        f"${TOKEN_FILE_ENV} or ${RCLONE_REMOTE_ENV}. Access is granted per "
        "person via the GloH2O request form."
    ),
)


@requires_share
def test_downloads_one_daily_granule(tmp_path):
    """One real MSWEP daily granule lands on disk as a NetCDF file."""
    source = MSWEP(
        start="2020-04-25",
        end="2020-04-25",
        variables=["precipitation"],
        temporal_resolution="daily",
        variant="Past",
        credentials=MswepCredentials(),
        path=tmp_path,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LicenseWarning)
        paths = source.download(progress_bar=False)

    assert len(paths) == 1
    granule = paths[0]
    assert granule.name == "2020116.nc"
    assert granule.stat().st_size > 0
    # netCDF-4 is HDF5-framed; classic netCDF starts "CDF". Accept either
    # rather than pinning the on-disk format GloH2O happens to ship today.
    assert granule.read_bytes()[:4] in (b"\x89HDF", b"CDF\x01", b"CDF\x02")


@requires_share
def test_share_lists_the_expected_roots(tmp_path):
    """The share exposes a version-stamped root the catalog knows about."""
    source = MSWEP(
        start="2020-04-25",
        end="2020-04-25",
        temporal_resolution="daily",
        credentials=MswepCredentials(),
        path=tmp_path,
    )
    source._initialize()
    roots = source.resolver.share_roots()
    assert roots, "the approved share listed no folders"
    assert any(name.startswith("MSWEP_V") for name in roots), sorted(roots)
