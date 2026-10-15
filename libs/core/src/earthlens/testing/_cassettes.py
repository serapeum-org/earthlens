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
* **Credentials never reach the YAML.** `vcr_config` filters the headers
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
    # The Copernicus Marine toolbox stamps the logged-in account onto *every*
    # request it makes, data chunks included, as `x-cop-user`. Scrubbing it
    # also makes the cassette replayable: the recorded URI is matched on, so
    # an unfiltered account name would only ever match the machine that
    # recorded it.
    "x-cop-user",
)

#: JSON response-body keys that are a secret wherever they appear. These are
#: names no data payload reuses, so they need no context to be safe to redact:
#: an OAuth token endpoint answers with live bearer credentials for the
#: account, and `filter_post_data_parameters` only ever sees a *request*.
_SECRET_JSON_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "client_secret",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "session_state",
    }
)

#: JSON keys that identify the signed-in account — but only inside an identity
#: document. Every one of these is an ordinary data field somewhere: IUCN's
#: taxonomy answers carry `name` on each synonym and `family_name` on each
#: taxon, so redacting them by name alone corrupts real payloads and reports
#: three clean cassettes as leaks. They are redacted only where
#: `_is_identity_document` holds.
_IDENTITY_JSON_KEYS: frozenset[str] = frozenset(
    {
        "email",
        "email_verified",
        "family_name",
        "given_name",
        "name",
        "preferred_username",
        "sub",
        "user",
        "username",
    }
)

#: What makes a body an identity document rather than data that happens to use
#: the same words: the OpenID Connect `sub` claim beside at least one profile
#: claim, both at the top level. That is the `/userinfo` shape, and it is the
#: leg that returned the maintainer's full name and login into a cassette.
_IDENTITY_MARKERS: frozenset[str] = frozenset(
    {"preferred_username", "email", "given_name", "name"}
)

#: What a scrubbed value is replaced with, so a diff of the cassette shows the
#: filtering happened rather than an empty string that could pass for a bug.
_PLACEHOLDER = "REDACTED"


def _is_identity_document(node: Any) -> bool:
    """Return whether a decoded JSON body identifies the signed-in account.

    Args:
        node: A decoded JSON document.

    Returns:
        True for a top-level object carrying `sub` and a profile claim.
    """
    return (
        isinstance(node, dict)
        and "sub" in node
        and bool(_IDENTITY_MARKERS & {str(key).lower() for key in node})
    )


def _redact_secrets(node: Any) -> Any:
    """Replace every secret-keyed value in a decoded JSON tree.

    Walks the whole tree for `_SECRET_JSON_KEYS`, and additionally applies
    `_IDENTITY_JSON_KEYS` to any node that is an identity document.

    Args:
        node: Any node of a decoded JSON document.

    Returns:
        The node with secret-keyed and identity-keyed values replaced.
    """
    if isinstance(node, dict):
        redact_identity = _is_identity_document(node)
        result = {}
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in _SECRET_JSON_KEYS or (
                redact_identity and lowered in _IDENTITY_JSON_KEYS
            ):
                result[key] = _PLACEHOLDER
            else:
                result[key] = _redact_secrets(value)
        return result
    if isinstance(node, list):
        return [_redact_secrets(item) for item in node]
    return node


def _scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """Redact the account identity out of a response body before recording.

    Only a well-formed JSON object body is touched, and only at the keys in
    `_IDENTITY_JSON_KEYS`. Anything that does not parse as JSON is left
    exactly as it was, so a raster, a NetCDF chunk or an HTML error page goes
    into the cassette untouched.

    Args:
        response: The vcrpy response dict, whose `body["string"]` holds the
            payload as `bytes` or `str`.

    Returns:
        The same response, with an identity-bearing JSON body rewritten.
    """
    import json

    body = (response.get("body") or {}).get("string")
    if not body:
        return response
    try:
        raw = body.decode("utf-8") if isinstance(body, bytes) else body
        loaded = json.loads(raw)
    except (AttributeError, UnicodeDecodeError, ValueError):
        # Not text, or not JSON: a GeoTIFF, a zarr chunk, an HTML error page.
        # The decode has to be inside the try — every binary payload this tier
        # records raises here, and a filter that raises fails the recording.
        return response
    if not isinstance(loaded, dict):
        return response
    redacted = _redact_secrets(loaded)
    if redacted == loaded:
        return response
    rewritten = json.dumps(redacted)
    response["body"]["string"] = (
        rewritten.encode("utf-8") if isinstance(body, bytes) else rewritten
    )
    return response


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
        # The request filters above cannot see a response, and an identity
        # endpoint answers with the account rather than with a credential.
        "before_record_response": _scrub_response,
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

    On the *request* side it reads the headers and the query string of the
    URI. On the *response* side it decodes JSON bodies and checks the keys in
    `_IDENTITY_JSON_KEYS`. A response body is never searched for bare
    substrings: the secret names are ordinary English words (`key`, `user`,
    `email`) that occur constantly in real payloads, so a substring search
    reports every cassette and therefore reports nothing. A structured key
    lookup has no such problem, and it is what catches an identity endpoint
    answering with the account behind the credential.

    Args:
        path: A cassette file, or a directory of them.

    Raises:
        AssertionError: When a request carries a secret-looking header or
            query parameter, or a JSON response an identity-keyed field,
            whose value was not replaced.

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
            body = (interaction.get("response") or {}).get("body") or {}
            for field in _unscrubbed_secret_fields(body.get("string")):
                offenders.append(f"{cassette.name}: response body {field}")

    assert not offenders, (
        "cassette(s) carry an unscrubbed credential - add the name to "
        "_SECRET_HEADERS / _SECRET_QUERY / _SECRET_JSON_KEYS and re-record:"
        + chr(10)
        + chr(10).join(offenders)
    )


def _unscrubbed_secret_fields(body: Any) -> list[str]:
    """Return the secret-keyed JSON fields a response body still exposes.

    Applies exactly the rule `_redact_secrets` records with, so the check and
    the filter cannot drift: `_SECRET_JSON_KEYS` anywhere, and
    `_IDENTITY_JSON_KEYS` only inside an identity document.

    Args:
        body: A recorded response body, as `bytes`, `str` or `None`.

    Returns:
        The dotted paths of every secret-keyed field whose value is neither
        the placeholder nor empty. Empty for a body that is not JSON.
    """
    import json

    if not body:
        return []
    raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    try:
        loaded = json.loads(raw)
    except ValueError:
        return []

    found: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            redact_identity = _is_identity_document(node)
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                lowered = str(key).lower()
                if lowered in _SECRET_JSON_KEYS or (
                    redact_identity and lowered in _IDENTITY_JSON_KEYS
                ):
                    if value not in (None, "", [], {}) and value != _PLACEHOLDER:
                        found.append(here)
                else:
                    _walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")

    _walk(loaded, "")
    return found
