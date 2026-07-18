"""Authentication placeholder for the NEXRAD radar backend.

The `unidata-nexrad-level2-chunks` bucket is **anonymous** (unsigned S3
GET + LIST), so `Radar` performs no login — :meth:`Radar._initialize`
returns `None` and no credentials are read. This module exists so the
package mirrors the layout of the other backends and gives a future
authenticated radar source an obvious home.
"""

from __future__ import annotations


def requires_auth() -> bool:
    """Return whether the radar source needs credentials.

    Returns:
        bool: Always `False` — the chunk bucket is anonymous.
    """
    return False
