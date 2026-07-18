"""Filesystem-safe filename helpers shared across the backends.

Several raster backends turn a provider dataset / product id into an output
file stem and independently re-spelled the same sanitisation — s3's
`_safe_name`, cmems's `_safe_filename`, and stac's ad-hoc `.replace("/", "_")`.
:func:`safe_filename` is the single implementation they now delegate to: a
whitelist collapse that keeps only `A-Z a-z 0-9 . _ -`, so every path separator
(`/`, `\\`) and Windows-illegal character (`: * ? " < > |`) — plus any other
punctuation or whitespace — becomes a single `_`.
"""

from __future__ import annotations

import re

#: Any run of characters outside the filesystem-safe whitelist
#: (`A-Z a-z 0-9 . _ -`). Matched greedily so a run collapses to one `_`.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(value: str) -> str:
    r"""Sanitise an id into a filesystem-safe file stem.

    Replaces every maximal run of characters outside the whitelist
    (`A-Z a-z 0-9 . _ -`) with a single `_`, then strips any leading /
    trailing `_`. Dots are kept, so a dataset id like
    `cmems_mod_glo_phy_my_0.083deg_P1D-m` is returned unchanged while a
    path-bearing key like `planetary-computer/sentinel-2-l2a` flattens to
    `planetary-computer_sentinel-2-l2a`.

    Args:
        value: The raw provider id / key.

    Returns:
        A filesystem-safe stem: only `A-Z a-z 0-9 . _ -`, no leading /
        trailing `_`.

    Examples:
        - Path separators and Windows-illegal characters collapse to `_`,
          while dots and hyphens survive:
            ```python
            >>> from earthlens.base.naming import safe_filename
            >>> safe_filename("a/b\\c:d")
            'a_b_c_d'
            >>> safe_filename('a*b?c"d<e>f|g')
            'a_b_c_d_e_f_g'
            >>> safe_filename("cmems_mod_glo_phy_my_0.083deg_P1D-m")
            'cmems_mod_glo_phy_my_0.083deg_P1D-m'

            ```
    """
    return _UNSAFE.sub("_", value).strip("_")
