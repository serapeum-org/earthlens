"""Authentication placeholder for the NWP backend.

Every NWP source the MVP targets is an **open, unauthenticated**
bucket or HTTPS endpoint: NOAA NODD (anonymous S3 / GCS / Azure),
ECMWF Open Data (no key), DWD Open Data (plain HTTPS), Météo-France
(`s3://mf-nwp-models`, unsigned). So unlike the ECMWF/CDS, GEE, and
Earthdata backends, `NWP` performs no login — :meth:`NWP._initialize`
returns `None` and no credentials are ever read.

This module exists so the package mirrors the layout of the other
backends (each has an `auth.py`) and so a future authenticated centre
has an obvious home. :func:`requires_auth` documents the current
state for callers that introspect it.
"""

from __future__ import annotations


def requires_auth() -> bool:
    """Return whether any MVP NWP centre needs credentials.

    Returns:
        bool: Always `False` — every supported centre is an open
            endpoint. Kept as a function (not a constant) so a future
            authenticated centre can make it model-dependent without
            changing the call sites.
    """
    return False
