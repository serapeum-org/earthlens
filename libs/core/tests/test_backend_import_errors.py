"""Every backend either imports or fails with an actionable `ImportError`.

The same test in two environments, which is the point of it. CI installs
`extras: all` everywhere, so a backend whose SDK is missing is a case the
matrix structurally cannot reach — and three backends are never installed
there at all (`argo` by the argopy/openeo xarray conflict, `osm-pbf` by
cykhash's sdist-only distribution, `eedai` by choice). The no-extras lane in
tests.yml runs this module with the providers installed and no SDK extras, so
the failure mode a user meets after `pip install earthlens` is the one asserted
here rather than the one nobody runs.

What it refuses to accept is a raw `ModuleNotFoundError` reaching the caller:
`No module named 'ee'` does not tell anyone to `pip install earthlens[gee]`.
"""

from __future__ import annotations

import pytest

from earthlens.earthlens import EarthLens

#: `(key, module, extra)` per registered backend, de-duplicated by module so an
#: alias (`chirps` for `chc`, `google-earth-engine` for `gee`) is not asserted
#: twice. Ids name the key, which is what a user types.
_ENTRIES = sorted(
    {
        module: (key, module, extras)
        for key, module, extras in EarthLens.DataSources.entries()
    }.values()
)
_IDS = [key for key, _module, _extras in _ENTRIES]


@pytest.mark.unit
@pytest.mark.parametrize("key,module,extras", _ENTRIES, ids=_IDS)
def test_backend_resolves_or_names_its_extra(
    key: str, module: str, extras: str
) -> None:
    """A backend imports, or says which extra installs it."""
    try:
        backend = EarthLens.DataSources[key]
    except ImportError as exc:
        message = str(exc)
        assert key in message, (
            f"the ImportError for {key!r} does not name the data source: {message!r}"
        )
        if extras:
            assert f"earthlens[{extras}]" in message, (
                f"the ImportError for {key!r} does not name the pip extra that "
                f"fixes it (expected `earthlens[{extras}]`): {message!r}"
            )
        assert exc.__cause__ is not None, (
            f"the original import failure for {key!r} must stay chained, so the "
            "traceback still names the SDK that is missing"
        )
        return
    assert isinstance(backend, type), f"{key!r} resolved to {backend!r}, not a class"
    assert backend.__module__.startswith(module.rsplit(".", 1)[0]), (
        f"{key!r} resolved to {backend.__module__}, which is not under {module}"
    )
