"""Per-test isolation of the output and cache directories.

Split out of the former single-module `earthlens.testing`; import from
`earthlens.testing`, not from here.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from earthlens.config import set_cache_dir, set_output_dir


@pytest.fixture(scope="session", autouse=True)
def _earthlens_dirs_scratch(tmp_path_factory):
    """Point the output and cache directories at a scratch root for the session.

    Session-scoped and autouse so the redirect is in place before *any* other
    fixture runs. A function-scoped fixture would be too late: pytest builds
    session-, package-, module- and class-scoped fixtures first, so a
    module-scoped fixture that constructs a backend without `path=` would
    resolve the developer's real `~/.earthlens/data` instead.
    """
    root = tmp_path_factory.mktemp("earthlens-dirs")
    set_output_dir(root / "data")
    set_cache_dir(root / "cache")
    yield root
    set_output_dir(None)
    set_cache_dir(None)


@pytest.fixture(autouse=True)
def isolate_earthlens_dirs(_earthlens_dirs_scratch, request):
    """Give each test its own slot beneath the session scratch root.

    A backend built without `path=` resolves to the configured output directory,
    and each backend hangs its intermediates cache off the configured cache
    directory. Left alone those are the developer's own `~/.earthlens/data` and
    per-platform user cache, so any test that downloads would write there.

    Per test, so a cache one test populates is not visible to the next — several
    tests assert on an empty or a pre-seeded cache. The slot is keyed on a hash
    of the whole node id: truncating the id would drop the discriminating path
    prefix, and long ids that end alike would share a directory. A readable
    prefix is kept so a leftover directory can still be traced back to its test.
    Resolving a directory never creates it, so an unused slot costs nothing.

    Autouse, so a member's tests get the isolation by importing this module's
    fixtures the same way they already import the HTTP transport seam.
    """
    nodeid = request.node.nodeid
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", nodeid)[-60:]
    digest = hashlib.blake2b(nodeid.encode("utf-8"), digest_size=8).hexdigest()
    root = _earthlens_dirs_scratch / f"{label}-{digest}"
    set_output_dir(root / "data")
    set_cache_dir(root / "cache")
    yield
    # Back to the session root rather than None, so anything still resolving
    # after this test — a wider-scoped teardown — stays isolated too.
    set_output_dir(_earthlens_dirs_scratch / "data")
    set_cache_dir(_earthlens_dirs_scratch / "cache")
