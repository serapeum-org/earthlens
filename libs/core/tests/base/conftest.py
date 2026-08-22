"""Shared configuration for the base tests, including the Hypothesis profile.

Registers and loads a deterministic Hypothesis profile so a property-based
failure reproduces from the CI log alone: `derandomize=True` seeds each test
from its own identity (no run-to-run randomness), and `deadline=None` avoids
per-example timing flakes on a loaded CI runner. The profile is loaded
unconditionally so local and CI runs behave identically.

The import is guarded because `hypothesis` is a dev-only dependency: without it
the property modules cannot be collected anyway, but the rest of the base suite
should still run.
"""

from __future__ import annotations

try:
    from hypothesis import settings
except ImportError:  # pragma: no cover - hypothesis is a declared dev dependency
    pass
else:
    settings.register_profile("deterministic", derandomize=True, deadline=None)
    settings.load_profile("deterministic")
