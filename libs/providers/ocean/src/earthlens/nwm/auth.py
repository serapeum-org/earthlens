"""Authentication placeholder for the National Water Model backend.

The `noaa-nwm-pds` bucket is **anonymous** (unsigned S3 GET + LIST), so
:class:`~earthlens.nwm.NWM` performs no login — :meth:`NWM._initialize`
returns `None` and no credentials are read. This module exists so the
package mirrors the layout of the credentialed backends and gives a
future authenticated hydrologic source an obvious home.
"""

from __future__ import annotations


def requires_auth() -> bool:
    """Return whether the NWM source needs credentials.

    Returns:
        bool: Always `False` — the NWM bucket is anonymous.

    Examples:
        - The NWM bucket is public:
            ```python
            >>> from earthlens.nwm.auth import requires_auth
            >>> requires_auth()
            False

            ```
    """
    return False
