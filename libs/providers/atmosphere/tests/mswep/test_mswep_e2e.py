"""Live end-to-end tests against a real approved GloH2O Drive share.

Selected with `-m "e2e and mswep"`; the default run deselects them.
They need access the maintainers cannot provision automatically: a
GloH2O request form must be approved (one form per product,
`gloh2o.org/mswep` and `gloh2o.org/mswx`), and the resulting share id
plus a credential must be configured — see
`docs/reference/mswep/authentication.md`.

The tests `skip` rather than fail when that configuration is absent, so
a contributor without access still gets a green run. That is a
deliberate exception to the repo's marker-not-environment rule: the
marker decides whether the tests are *selected*, and the skip only
reports that the selected tests cannot run on this machine.

Configure with `$MSWEP_DRIVE_FOLDER` (the shared folder id — which is
the version root) plus any credential: `$MSWEP_TOKEN_FILE`,
`$MSWEP_RCLONE_REMOTE`, or Application Default Credentials (`gcloud auth
application-default login`). `$MSWEP_E2E_VERSION` names the version the
folder holds (default `3.16`).
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
    try_application_default,
)

pytestmark = [pytest.mark.e2e, pytest.mark.mswep]


def _has_credential() -> bool:
    """Return whether any Drive credential is available (incl. ADC)."""
    if os.getenv(TOKEN_FILE_ENV) or os.getenv(RCLONE_REMOTE_ENV):
        return True
    return try_application_default() is not None


def _configured() -> bool:
    """Return whether this machine has an approved share configured."""
    return bool(os.getenv(FOLDER_ID_ENV)) and _has_credential()


requires_share = pytest.mark.skipif(
    not _configured(),
    reason=(
        f"no approved GloH2O share configured: set ${FOLDER_ID_ENV} plus a "
        f"credential (${TOKEN_FILE_ENV}, ${RCLONE_REMOTE_ENV}, or ADC via "
        "`gcloud auth application-default login`). Access is granted per "
        "person via the GloH2O request form."
    ),
)

VERSION = os.getenv("MSWEP_E2E_VERSION", "3.16")


@requires_share
def test_downloads_two_daily_granules(tmp_path):
    """Two real MSWEP daily granules land on disk as NetCDF files."""
    source = MSWEP(
        start="2020-04-25",
        end="2020-04-26",
        variables=["precipitation"],
        temporal_resolution="daily",
        variant="Past",
        version=VERSION,
        credentials=MswepCredentials(),
        path=tmp_path,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        paths = source.download(progress_bar=False)

    assert [p.name for p in paths] == ["2020116.nc", "2020117.nc"]
    for granule in paths:
        assert granule.stat().st_size > 0
        # netCDF-4 is HDF5-framed; classic netCDF starts "CDF". Accept either.
        assert granule.read_bytes()[:4] in (b"\x89HDF", b"CDF\x01", b"CDF\x02")
    # Every request carries the CC-BY-NC obligation.
    assert any(issubclass(w.category, LicenseWarning) for w in caught)


@requires_share
def test_output_mirrors_the_version_root(tmp_path):
    """Granules land under `<version-root>/Past/Daily/`, as the share holds them."""
    source = MSWEP(
        start="2020-04-25",
        end="2020-04-25",
        temporal_resolution="daily",
        variant="Past",
        version=VERSION,
        credentials=MswepCredentials(),
        path=tmp_path,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LicenseWarning)
        paths = source.download(progress_bar=False)

    relative = paths[0].relative_to(tmp_path).as_posix()
    assert relative.endswith("/Past/Daily/2020116.nc")


@requires_share
@pytest.mark.skipif(
    not os.getenv("MSWX_DRIVE_FOLDER"),
    reason="set $MSWX_DRIVE_FOLDER to an approved MSWX share to test forecasts",
)
def test_downloads_a_forecast_ensemble(tmp_path):
    """A real MSWX `Mid` forecast fetches one granule per member per valid day."""
    source = MSWEP(
        product="mswx",
        variant="Mid",
        init=os.getenv("MSWX_E2E_INIT", "2026-08-01"),
        members=[1, 2],
        variables=["Temp"],
        start=os.getenv("MSWX_E2E_INIT", "2026-08-01"),
        end=os.getenv("MSWX_E2E_END", "2026-08-02"),
        temporal_resolution="daily",
        credentials=MswepCredentials(folder_id=os.environ["MSWX_DRIVE_FOLDER"]),
        path=tmp_path,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LicenseWarning)
        paths = source.download(progress_bar=False)

    assert paths, "the forecast returned no granules"
    members = {p.parent.parent.name for p in paths}
    assert members == {"01", "02"}
    assert paths[0].read_bytes()[:4] in (b"\x89HDF", b"CDF\x01", b"CDF\x02")


@requires_share
def test_root_is_the_shared_folder(tmp_path):
    """The shared folder id resolves to a version-stamped root."""
    source = MSWEP(
        start="2020-04-25",
        end="2020-04-25",
        temporal_resolution="daily",
        version=VERSION,
        credentials=MswepCredentials(),
        path=tmp_path,
    )
    # Auth is lazy: reading `resolver` opens the Drive client on first use.
    root = source.resolver.root()
    assert root.name.startswith("MSWEP_V"), root.name
