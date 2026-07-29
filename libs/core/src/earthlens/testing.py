"""Shared pytest fixtures for testing earthlens and its backends.

Imported by each member distribution's `tests/conftest.py`. It lives in the
installed package rather than in a test directory because pytest's rootdir
becomes the *member* directory when a member is run on its own — which is how CI
runs them — so a repo-root `conftest.py` silently would not load and a module
under `libs/core/tests/` would not be importable. An installed module is
reachable from any rootdir.

The `numpy.testing` / `pandas.testing` precedent: shipping test support beside
the code it tests, so downstream code testing its own backend gets the same
seams rather than reinventing them.

Requires pytest, which is why it is behind the `test` extra
(`pip install earthlens-core[test]`) — importing this module without it raises
`ImportError`. Nothing in the runtime package imports it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def unpooled_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `HttpClient`'s default transport at the `requests`-module adapter.

    Production pools: `HttpClient()` builds a `requests.Session`, so a backend
    pulling many small files from one host pays one TCP+TLS handshake instead of
    one per file. The suite, though, drives HTTP by patching `requests.get` /
    `requests.head` on the module object, and `session.get` never consults
    `requests.get` — 161 tests would fall through to the real network.

    So for the duration of a test the default becomes
    :class:`~earthlens.base.http.RequestsGet`, which re-resolves `requests` per
    call. Every module-level fake keeps driving the transport while production
    keeps its pooled session. A test that needs the shipped default asks for
    :func:`real_pooled_session`, which switches it back.

    Args:
        monkeypatch: pytest's patcher, so the seam is undone per test.
    """
    from earthlens.base import http

    monkeypatch.setattr(http, "new_session", http.RequestsGet)
    # The per-thread cache outlives a test, so a session built against the
    # previous test's transport would otherwise be handed to this one.
    http.reset_thread_local_sessions()


@pytest.fixture
def real_pooled_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the production default so a test can observe real pooling.

    Args:
        monkeypatch: pytest's patcher, so the seam is undone per test.
    """
    import requests

    from earthlens.base import http

    monkeypatch.setattr(http, "new_session", requests.Session)
    http.reset_thread_local_sessions()
