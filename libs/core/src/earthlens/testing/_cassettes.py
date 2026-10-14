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
    # Identity, not a secret, but just as unwelcome in a committed file: NREL
    # authenticates with `api_key` *and* `email`, so a recording carries the
    # maintainer's address into git unless it is scrubbed too.
    "email",
    "username",
    "user",
)

#: What a scrubbed value is replaced with, so a diff of the cassette shows the
#: filtering happened rather than an empty string that could pass for a bug.
_PLACEHOLDER = "REDACTED"


@pytest.fixture(scope="module")
def vcr_config(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Configure `pytest-recording` for every `@pytest.mark.vcr` test.

    Args:
        request: The pytest request, read for the `--record-mode` option.

    Returns:
        The vcrpy options: the record mode, credential filtering on headers,
        query parameters and form bodies, and matching that ignores the parts
        of a request a replay cannot reproduce byte-for-byte.
    """
    return {
        # Default `none`: a marked test replays or fails, never reaching the
        # network. Passed through from `--record-mode` rather than hardcoded,
        # because this fixture *overrides* the command line — `use_cassette`
        # ends with `if "record_mode" in merged_config: record_mode =
        # merged_config["record_mode"]`, so pinning it here would make
        # `--record-mode=once` silently do nothing and leave no way to
        # record a cassette in the first place.
        # `or "none"`: the option is always registered, so `getoption` returns
        # its `None` default rather than falling back to ours.
        "record_mode": request.config.getoption("--record-mode", default=None)
        or "none",
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
    """Assert a recorded cassette carries no unscrubbed credential.

    A belt-and-braces check for the committed YAML: the vcrpy filters only
    scrub what they were told about, so a backend that invents a new header
    name would leak until someone noticed. Point this at the cassette
    directory in a test and a leak fails the suite instead of reaching the
    repository.

    Only the *request* side is inspected — its headers and the query string of
    its URI. A response body is not searched, because the secret names are
    ordinary English words (`key`, `user`, `email`) that occur constantly in
    real payloads; searching bodies for them reports every cassette and so
    reports nothing.

    Args:
        path: A cassette file, or a directory of them.

    Raises:
        AssertionError: When a request carries a secret-looking header or
            query parameter whose value was not replaced.

    Examples:
        ```python
        >>> from earthlens.testing import assert_cassette_is_scrubbed
        >>> assert_cassette_is_scrubbed.__name__
        'assert_cassette_is_scrubbed'

        ```
    """
    import pathlib
    import urllib.parse

    import yaml

    root = pathlib.Path(path)
    files = sorted(root.rglob("*.yaml")) if root.is_dir() else [root]
    secret_headers = {name.lower() for name in _SECRET_HEADERS}
    secret_query = {name.lower() for name in _SECRET_QUERY}
    offenders: list[str] = []

    for cassette in files:
        loaded = yaml.safe_load(cassette.read_text(encoding="utf-8")) or {}
        for interaction in loaded.get("interactions") or []:
            request = interaction.get("request") or {}
            for name, value in (request.get("headers") or {}).items():
                if name.lower() not in secret_headers:
                    continue
                for item in value if isinstance(value, list) else [value]:
                    if _PLACEHOLDER not in str(item):
                        offenders.append(f"{cassette.name}: header {name}")
            query = urllib.parse.urlparse(request.get("uri") or "").query
            for name, values in urllib.parse.parse_qs(query).items():
                if name.lower() not in secret_query:
                    continue
                for item in values:
                    if _PLACEHOLDER not in item:
                        offenders.append(f"{cassette.name}: query {name}")

    assert not offenders, (
        "cassette(s) carry an unscrubbed credential - add the name to "
        "_SECRET_HEADERS / _SECRET_QUERY and re-record:"
        + chr(10)
        + chr(10).join(offenders)
    )
