"""Shared pytest fixtures, hooks and markers for earthlens and its backends.

Imported by each member distribution's `tests/conftest.py`. It lives in the
installed package rather than in a test directory because pytest's rootdir
becomes the *member* directory when a member is run on its own — which is how CI
runs them — so a repo-root `conftest.py` silently would not load and a module
under `libs/core/tests/` would not be importable. An installed module is
reachable from any rootdir.

The `numpy.testing` / `pandas.testing` precedent: shipping test support beside
the code it tests, so downstream code testing its own backend gets the same
seams rather than reinventing them.

What lives here, by submodule:

| Submodule | Surface                                                    |
|-----------|------------------------------------------------------------|
| `_http`   | the transport seam (`unpooled_http_transport`)             |
| `_live`   | the availability classifier, the skip helpers, both guards |
| `_dirs`   | per-test output/cache isolation                            |
| `_markers`| the one marker registry, registered via `pytest_configure` |
| `_tagging`| the directory-derived backend marker for every test       |
| `_fakes`  | the shared `requests.Response` double and the URL router  |

Requires pytest, which is why it is behind the `test` extra
(`pip install earthlens-core[test]`) — importing this module without it raises
`ImportError`. Nothing in the runtime package imports it.
"""

from __future__ import annotations

from earthlens.testing._dirs import _earthlens_dirs_scratch, isolate_earthlens_dirs
from earthlens.testing._fakes import FakeHttp, FakeResponse, fake_http
from earthlens.testing._http import real_pooled_session, unpooled_http_transport
from earthlens.testing._live import (
    _LIVE_SKIP_PREFIX,
    is_upstream_unavailable,
    pytest_collection_modifyitems,
    pytest_runtest_call,
    pytest_sessionfinish,
    require_marked_hosts,
    require_reachable,
    reset_reachability_cache,
    skip_live_unavailable,
    skip_on_upstream,
)
from earthlens.testing._markers import MARKERS, pytest_configure
from earthlens.testing._tagging import marker_for_path, tag_items_by_directory

__all__ = [
    "MARKERS",
    "FakeHttp",
    "FakeResponse",
    "_LIVE_SKIP_PREFIX",
    "_earthlens_dirs_scratch",
    "fake_http",
    "is_upstream_unavailable",
    "isolate_earthlens_dirs",
    "marker_for_path",
    "pytest_collection_modifyitems",
    "pytest_configure",
    "pytest_runtest_call",
    "pytest_sessionfinish",
    "real_pooled_session",
    "require_marked_hosts",
    "require_reachable",
    "reset_reachability_cache",
    "skip_live_unavailable",
    "skip_on_upstream",
    "tag_items_by_directory",
    "unpooled_http_transport",
]
