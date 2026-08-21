"""Shared fixtures for the land distribution's tests.

Re-exports the HTTP transport seam every member root needs from
`earthlens.testing`, which is where it now lives so that a member's tests can
find it when that member is run on its own.
"""

from __future__ import annotations

# The HTTP transport seam lives in the installed package so every member
# root can reach it; see earthlens.testing for why it cannot live here.
from earthlens.testing import (  # noqa: F401 - fixtures + hooks used by name
    _earthlens_dirs_scratch,
    isolate_earthlens_dirs,
    pytest_runtest_call,
    pytest_sessionfinish,
    real_pooled_session,
    unpooled_http_transport,
)
