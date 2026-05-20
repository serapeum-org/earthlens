"""Optional EarthScope token resolution for the FDSN backend.

The six FDSN-event networks earthlens ships with (USGS, EMSC, INGV,
EarthScope, ISC, GeoNet) all expose **public** event web services, so
the common path needs no credentials at all. This module is intentionally thin:
it only resolves an optional EarthScope access token from, in
priority order, an explicit argument, the `EARTHSCOPE_TOKEN`
environment variable, or a `~/.earthscope_token` file. The backend
consults it only when a provider row declares `needs_token: true`,
which none of the bundled public networks do.

There is deliberately no credentialed-login class here (unlike
:class:`earthlens.cmems.CmemsAuth` / `earthlens.gee.EarthEngineAuth`)
— event queries do not authenticate.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Default location of a saved EarthScope token file.
EARTHSCOPE_TOKEN_FILE: Path = Path.home() / ".earthscope_token"


def resolve_earthscope_token(token: str | None = None) -> str | None:
    """Resolve an optional EarthScope access token.

    Resolution order:

    1. The explicit `token` argument, if non-empty.
    2. The `EARTHSCOPE_TOKEN` environment variable.
    3. A `~/.earthscope_token` file (first non-empty line).

    Args:
        token: An explicit token passed by the caller, or `None`.

    Returns:
        The resolved token string, or `None` when no source supplies
            one (the normal case for the public event services).

    Examples:
        - An explicit token wins and is returned unchanged:
            ```python
            >>> from earthlens.fdsn.auth import resolve_earthscope_token
            >>> resolve_earthscope_token("abc123")
            'abc123'

            ```
        - With no argument, env var, or file, the result is `None`:
            ```python
            >>> import os
            >>> from earthlens.fdsn.auth import resolve_earthscope_token
            >>> os.environ.pop("EARTHSCOPE_TOKEN", None)  # doctest: +SKIP
            >>> resolve_earthscope_token()  # doctest: +SKIP

            ```
    """
    if token:
        return token
    env_token = os.environ.get("EARTHSCOPE_TOKEN")
    if env_token:
        return env_token
    if EARTHSCOPE_TOKEN_FILE.is_file():
        for line in EARTHSCOPE_TOKEN_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return None
