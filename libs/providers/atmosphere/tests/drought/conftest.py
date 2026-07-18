"""Auto-tag every test under `tests/drought/` with the `drought` pytest marker."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_collection_modifyitems(items):
    """Tag every test in this subtree with `@pytest.mark.drought`.

    Lets the suite be filtered with `-m drought` to run only the
    drought backend's tests (e.g. within the atmosphere CI lane).

    Pytest delivers the FULL item list to every conftest hook, not
    just items from this subtree, so we filter by path.
    """
    here = Path(__file__).parent.resolve()
    for item in items:
        try:
            if Path(item.fspath).resolve().is_relative_to(here):
                item.add_marker(pytest.mark.drought)
        except (OSError, ValueError):
            continue
