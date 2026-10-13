"""Recorded provider exchanges, replayed with no socket.

The tier between a hand-built `FakeResponse` and a live `e2e` test. A cassette
is a real exchange with the real provider, recorded once and replayed offline
for ever after — so the test asserts against what the service actually sent,
including the header casing, the pagination envelope and the error body, none
of which a hand-written double reproduces faithfully.

Two properties make this safe to keep in the repository:

* **The default record mode is `none`.** A `@pytest.mark.vcr` test cannot
  silently re-record against the live service; replaying an exchange the
  cassette does not contain is an error, not a quiet network call. Recording
  is a deliberate act (`--record-mode=once`).
* **Credentials never reach the YAML.** :func:`vcr_config` filters the headers
  and query parameters the keyed backends authenticate with, replacing each
  with a placeholder. earthlens backends put keys in both places — wdpa and
  iucn in a header, firms / airnow / nrel in the query string — so both are
  filtered.

Import from `earthlens.testing`, not from here.
"""

from __future__ import annotations

from typing import Any

import pytest

#: Request headers scrubbed before a cassette is written. Lower-case: vcrpy
#: matches these case-insensitively.
_SECRET_HEADERS: tuple[str, ...] = (
    "authorization",
    "x-api-key",
    "x-auth-token",
    "token",
    "cookie",
    "set-cookie",
    "proxy-authorization",
)

#: Query parameters scrubbed before a cassette is written. The keyed earthlens
#: backends that sign in the query string: firms (`MAP_KEY`), airnow / nrel
#: (`api_key`), wdpa / iucn (`token`), plus the AWS presign parameters.
_SECRET_QUERY: tuple[str, ...] = (
    "api_key",
    "apikey",
    "key",
    "MAP_KEY",
    "map_key",
    "token",
    "access_token",
    "password",
    "X-Amz-Credential",
    "X-Amz-Signature",
    "X-Amz-Security-Token",
)

#: What a scrubbed value is replaced with, so a diff of the cassette shows the
#: filtering happened rather than an empty string that could pass for a bug.
_PLACEHOLDER = "REDACTED"


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, Any]:
    """Configure `pytest-recording` for every `@pytest.mark.vcr` test.

    Returns:
        The vcrpy options: `none` record mode, credential filtering on both
        headers and query parameters, and matching that ignores the parts of
        a request a replay cannot reproduce byte-for-byte.
    """
    return {
        # A marked test replays or fails; it never reaches the network.
        "record_mode": "none",
        "filter_headers": [(name, _PLACEHOLDER) for name in _SECRET_HEADERS],
        "filter_query_parameters": [(name, _PLACEHOLDER) for name in _SECRET_QUERY],
        # Scrub the response's own credentials too — a token-minting endpoint
        # answers with one.
        "filter_post_data_parameters": [(name, _PLACEHOLDER) for name in _SECRET_QUERY],
        # Match on method + scheme + host + path + query. Deliberately not on
        # the body: a backend that serialises a dict into the body cannot be
        # relied on for key order across Python versions.
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "decode_compressed_response": True,
    }


def assert_cassette_is_scrubbed(path: Any) -> None:
    """Assert a recorded cassette carries no obvious credential.

    A belt-and-braces check for the committed YAML: `filter_headers` only
    scrubs what it was told about, so a backend that invents a new header name
    would leak until someone noticed. Point this at the cassette directory in
    a test and a leak fails the suite instead of reaching the repository.

    Args:
        path: A cassette file, or a directory of them.

    Raises:
        AssertionError: When a cassette contains an unscrubbed secret-looking
            header or query parameter.
    """
    import pathlib

    root = pathlib.Path(path)
    files = sorted(root.rglob("*.yaml")) if root.is_dir() else [root]
    offenders: list[str] = []
    for cassette in files:
        text = cassette.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for name in (*_SECRET_HEADERS, *_SECRET_QUERY):
            marker = name.lower()
            if marker not in lowered:
                continue
            # Present is fine as long as every occurrence was redacted.
            for line in text.splitlines():
                if marker in line.lower() and _PLACEHOLDER not in line:
                    offenders.append(f"{cassette.name}: {line.strip()[:100]}")
    assert not offenders, (
        "cassette(s) carry an unscrubbed credential — add the name to "
        "_SECRET_HEADERS / _SECRET_QUERY and re-record:\n" + "\n".join(offenders)
    )
