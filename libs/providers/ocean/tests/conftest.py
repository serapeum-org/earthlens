"""Shared fixtures for the ocean distribution's tests.

Holds the HTTP transport seam every member root needs; see
`_unpooled_http_transport`.
"""

from __future__ import annotations

import pytest

# The HTTP transport seam lives in the installed package so every member
# root can reach it; see earthlens.testing for why it cannot live here.
from earthlens.testing import (  # noqa: F401 - fixtures used by name
    real_pooled_session,
    unpooled_http_transport,
)
