"""Derive each test's backend marker from the directory it lives in.

Three backends (`ecmwf`, `drought`, `gee`) already auto-tagged their own
subtree from a local `conftest.py`, and it is strictly the better mechanism: it
cannot drift, and it is why `-m "e2e and ecmwf"` keeps working even though none
of the eight `test_ecmwf/*_e2e.py` modules names the marker. The other 59
backends tagged by hand with a module-level `pytestmark`, and the hand form had
drifted in one systematic way — every backend-scoped `test_cli.py` carries only
`pytest.mark.cli`, so `-m <backend>` missed that backend's own CLI tests for 33
of 62 backends.

This promotes the three local hooks into one, so the documented "run just this
backend" selector is complete and no new backend has to remember the marker.

Import from `earthlens.testing`, not from here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from earthlens.testing._markers import MARKERS

#: Directory names that do not spell their marker. `test_ecmwf` is handled by
#: the generic `test_` strip below; `eea_aq` is the one true rename (the extra
#: and the package are `eea_aq`, the marker is `eea`).
_DIRECTORY_ALIASES: dict[str, str] = {"eea_aq": "eea"}


def marker_for_path(path: Path) -> str | None:
    """Return the backend marker a test file's location implies, or `None`.

    The backend is the first directory under the member's `tests/` root, so
    `libs/providers/land/tests/wdpa/test_cli.py` is `wdpa`. A directory that
    names no declared marker (core's `tests/base/`, a backend's nested
    `tests/<backend>/catalog/`) yields `None` rather than inventing one.

    Args:
        path: The test module's path.

    Returns:
        The marker name, or `None` when the location implies none.

    Examples:
        ```python
        >>> from pathlib import Path
        >>> from earthlens.testing import marker_for_path
        >>> marker_for_path(Path("libs/providers/land/tests/wdpa/test_cli.py"))
        'wdpa'
        >>> marker_for_path(Path("libs/core/tests/base/test_dates.py")) is None
        True

        ```
    """
    parts = path.parts
    if "tests" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("tests")
    if index + 2 > len(parts) - 1:  # the file sits directly in tests/
        return None
    name = parts[index + 1]
    candidate = _DIRECTORY_ALIASES.get(name, name)
    if candidate not in MARKERS and candidate.startswith("test_"):
        candidate = candidate[len("test_") :]
    return candidate if candidate in MARKERS else None


def tag_items_by_directory(items: list[pytest.Item]) -> None:
    """Apply the directory-implied backend marker to every collected item.

    Args:
        items: The collected items, marked in place.
    """
    for item in items:
        path = getattr(item, "path", None)
        if path is None:  # pragma: no cover - every modern item has one
            continue
        marker = marker_for_path(Path(path))
        if marker is not None and item.get_closest_marker(marker) is None:
            item.add_marker(getattr(pytest.mark, marker))
