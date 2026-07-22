"""Live end-to-end test for the NWM federated CLI verbs.

Exercises the real NWM bucket-walk in `earthlens.cli.refresh` /
`earthlens.cli.validate` against the public, unsigned `noaa-nwm-pds`
bucket — the boto bodies the unit tests mock out — so a regression in the
real walk is caught. Gated on the `e2e` marker and network reachability;
a default `pytest` run skips it.

Run with:

    pixi run -e dev pytest -m "e2e and nwm" tests/cli
"""

from __future__ import annotations

import pytest
from earthlens.cli.adapter import list_backends
from earthlens.cli.refresh import audit_one, refresh_one
from earthlens.cli.validate import validate_one

pytestmark = [pytest.mark.e2e, pytest.mark.cli, pytest.mark.nwm]


def _network_available() -> bool:
    """Return whether the public NWM bucket is reachable (unsigned)."""
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config

        client = boto3.client(
            "s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED)
        )
        client.list_objects_v2(Bucket="noaa-nwm-pds", MaxKeys=1, Delimiter="/")
        return True
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(not _network_available(), reason="NWM bucket unreachable")
)


def _nwm_info():
    """Return the BackendInfo for the nwm backend."""
    return next(b for b in list_backends() if b.provider == "nwm")


def test_refresh_nwm_lists_live_configurations():
    """`refresh nwm` walks the live bucket and diffs against the curated index."""
    outcome = refresh_one(_nwm_info())
    assert outcome.status == "ok", outcome.detail
    assert outcome.live_count >= outcome.bundled_count, "every curated config is live"
    assert "usgs_timeslices" in outcome.new_ids, "the uncurated assimilation dir shows"


def test_audit_nwm_has_no_drift():
    """`audit nwm` finds no broken curated configuration on the live bucket."""
    outcome = audit_one(_nwm_info())
    assert outcome.status == "ok", outcome.detail
    assert outcome.broken == [], f"unexpected drift: {outcome.broken}"
    assert "usgs_timeslices" in outcome.untracked, "uncurated dir is untracked"


def test_validate_nwm_live_token_check_passes():
    """`validate nwm --live` finds every product's s3_token under a live carrier."""
    result = validate_one(_nwm_info(), live=True)
    assert result.status == "ok", result.detail
    assert result.issues == [], f"unexpected live issues: {result.issues}"
    assert result.checked > 0, "products were checked"
