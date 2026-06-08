"""Shared fixtures for the CLI test suite."""

from __future__ import annotations

import pytest

from earthlens.cli.table import build_table, clear_table_cache


@pytest.fixture(autouse=True)
def _clear_cli_table_cache():
    """Drop the process-lifetime catalog-table cache around every CLI test.

    The table cache is keyed by provider selection and lives for the
    process, which would otherwise leak state between tests (e.g. a
    `refresh` assertion seeing a stale table). Clearing before and after
    keeps each test isolated.
    """
    clear_table_cache()
    yield
    clear_table_cache()


@pytest.fixture(scope="session")
def chc_table():
    """A provider-scoped table over the CHC backend only (fast to build).

    Returns:
        The cached :class:`~earthlens.cli.table.CatalogTable` for `chc`.
    """
    return build_table(providers=["chc"])
