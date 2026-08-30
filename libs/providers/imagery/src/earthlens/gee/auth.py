"""Service-account authentication for the Google Earth Engine backend.

Hosts :class:`EarthEngineAuth`, a thin wrapper over the
`earthengine-api` (`ee`) authentication entry points. The Earth Engine
backend authenticates with a Google Cloud *service account* plus a JSON
key file (no interactive browser login on the machine that runs the
download); :class:`EarthEngineAuth.initialize` performs the one-time
`ee.Initialize` against a *registered* Cloud project.

The Cloud project the calls are scoped/billed to is mandatory on
current `earthengine-api` releases: it is taken from the explicit
`project` argument when given, else from the key file's `project_id`
field. A project that has never been registered for Earth Engine, or
that the service account lacks permission on, surfaces as an
:class:`AuthenticationError` with a pointer at the registration /
permissions docs rather than a raw `ee` exception.

See:
    - Service accounts: <https://developers.google.com/earth-engine/guides/service_account>
    - Registering a project: <https://code.earthengine.google.com/register>
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, cast

import ee
from pydantic import BaseModel, ConfigDict, SecretStr

from earthlens.base.auth import AbstractAuth
from earthlens.base.auth import AuthenticationError as _BaseAuthenticationError

_REGISTER_URL = "https://code.earthengine.google.com/register"
_SERVICE_ACCOUNT_DOCS = (
    "https://developers.google.com/earth-engine/guides/service_account"
)


class AuthenticationError(_BaseAuthenticationError):
    """Raised when the Earth Engine connection cannot be established.

    Wraps the underlying `ee` / Google credential errors with an
    actionable message — most commonly a missing or malformed service
    key, an unregistered Cloud project, or a service account that lacks
    an Earth Engine IAM role on the target project.

    A subclass of the cross-backend
    :class:`earthlens.base.AuthenticationError` so callers can catch
    every backend's auth failure with one `except` clause; backward
    compatible with existing `except earthlens.gee.AuthenticationError`
    consumers.
    """


class EarthEngineCredentials(BaseModel):
    """Frozen value object holding the Earth Engine service-account creds.

    Used internally by `EarthEngineAuth` to satisfy the
    `earthlens.base.AbstractAuth` generic-type bound. The public
    `EarthEngineAuth` constructor still accepts the three positional
    kwargs (`service_account`, `service_key`, `project`) for
    backward compatibility — the credentials object is built
    internally and stored on `self._creds`.

    Attributes:
        service_account: Service-account email, e.g.
            `my-sa@my-project.iam.gserviceaccount.com`.
        service_key: Path to the JSON key file, or the JSON content
            as a string, held as a `SecretStr` so it is never echoed in
            `repr` or logs. `EarthEngineAuth.initialize` distinguishes
            the two by leading character.
        project: Cloud project id; if `None`, falls back to the key
            file's `project_id` field at `configure()` time.

    Examples:
        - Build a credentials object from a file path:
            ```python
            >>> from earthlens.gee.auth import EarthEngineCredentials
            >>> creds = EarthEngineCredentials(
            ...     service_account="sa@my-project.iam.gserviceaccount.com",
            ...     service_key="/path/to/key.json",
            ...     project="my-project",
            ... )
            >>> creds.service_account
            'sa@my-project.iam.gserviceaccount.com'
            >>> creds.project
            'my-project'

            ```
        - `project` is optional — `None` defers resolution to `configure()`:
            ```python
            >>> from earthlens.gee.auth import EarthEngineCredentials
            >>> creds = EarthEngineCredentials(
            ...     service_account="sa@p.iam",
            ...     service_key='{"type": "service_account"}',
            ... )
            >>> creds.project is None
            True

            ```
        - The key is stored redacted; the value comes back only on request:
            ```python
            >>> from earthlens.gee.auth import EarthEngineCredentials
            >>> creds = EarthEngineCredentials(
            ...     service_account="sa@p.iam",
            ...     service_key="/path/to/key.json",
            ... )
            >>> creds.service_key
            SecretStr('**********')
            >>> creds.service_key.get_secret_value()
            '/path/to/key.json'

            ```
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    service_account: str
    #: Held as a `SecretStr` so `repr()` and `str()` render `**********`
    #: instead of the key. A plain `str` is coerced on construction, so
    #: callers pass a path or JSON content unchanged; the one consumer
    #: unwraps it with `get_secret_value()`.
    service_key: SecretStr
    project: str | None = None


def _is_inline_json(service_key: str) -> bool:
    """Whether `service_key` carries the key's JSON rather than its path.

    The same leading-character rule `_load_key_dict` applies, named once so
    the credential call and the parse cannot disagree about which shape they
    were handed.

    Args:
        service_key: Path to the service-account JSON file, or the JSON
            content as a string.

    Returns:
        bool: True when the value is inline JSON.
    """
    return isinstance(service_key, str) and service_key.lstrip().startswith("{")


#: Credential field names, matched only when quoted as a mapping key, so both
#: JSON ("private_key":) and a Python repr ('private_key':) are caught while
#: ordinary prose naming a field is not.
_CREDENTIAL_KEY_RE = re.compile(
    r"""['"](?:private_key|client_secret|refresh_token|client_email)['"]\s*:"""
)
#: PEM armour, which carries no quoting at all.
_PEM_MARKER = "PRIVATE KEY"


def _redact(message: str, service_key: str) -> str:
    """Return `message` with any credential material removed.

    Defence in depth for the error paths. `ee.ServiceAccountCredentials` takes
    a *filename* positionally, so handing it inline JSON makes Python raise
    `FileNotFoundError` with the whole key as the "filename" - which a
    traceback then prints. Callers below break the exception chain, and this
    strips the value from anything they do report.

    Only an inline-JSON `service_key` is substituted. A path is not secret, and
    redacting it made the commonest failure of all - a key file that is not
    where it was said to be - report `No such file or directory: '<service key
    redacted>'`, naming nothing the reader can act on. Both workflows pass a
    path, so that was the usual case.

    Substring replacement alone is not enough either, and assuming it was is
    what let a key reach a log in the first place. `OSError.__str__` reprs the
    filename, so a multi-line key arrives with its newlines escaped and is no
    longer byte-identical to the value held - the same mismatch that defeated
    the platform's own secret masking. The escaped form is replaced too, and
    any residual credential marker collapses the message rather than trusting
    that the substitutions caught everything.

    Args:
        message: The text about to be surfaced.
        service_key: The key path or JSON content to strip.

    Returns:
        str: The message with credential material replaced, or
            `"<service key redacted>"` alone when a marker survived.
    """
    cleaned = message
    if _is_inline_json(service_key) and len(service_key) > 8:
        for form in (service_key, repr(service_key)[1:-1]):
            cleaned = cleaned.replace(form, "<service key redacted>")
    if _PEM_MARKER in cleaned or _CREDENTIAL_KEY_RE.search(cleaned):
        return "<service key redacted>"
    return cleaned


def _load_key_dict(service_key: str) -> dict[str, Any] | None:
    """Return the parsed service-account JSON, or `None` if not parseable.

    Accepts either a filesystem path to the key file or the raw JSON
    string itself; returns `None` when `service_key` is neither (so the
    caller can still proceed with whatever `ee` accepts and only error
    if `ee` itself rejects it). `_is_inline_json` decides between the two
    shapes — a leading `{` means inline JSON, anything else a path. That
    is steadier than `Path(...).is_file()`, which merely answers `False`
    for multi-line JSON content and so cannot tell inline content apart
    from a path that does not exist.

    Args:
        service_key: Path to the service-account JSON file, or the JSON
            content as a string.

    Returns:
        The decoded key mapping, or `None` if it could not be read or
        parsed.
    """
    if not isinstance(service_key, str):
        return None
    if _is_inline_json(service_key):
        try:
            return cast("dict[str, Any]", json.loads(service_key))
        except ValueError:
            return None
    try:
        return cast("dict[str, Any]", json.loads(Path(service_key).read_text()))
    except (OSError, ValueError):
        return None


class EarthEngineAuth(AbstractAuth[EarthEngineCredentials]):
    """Authenticate and initialise a connection to Google Earth Engine.

    Construct this with a service-account email and key (file path or
    raw JSON); construction performs the one-time `ee.Initialize`. The
    Cloud project is read from the `project` argument or, failing that,
    from the key file's `project_id`.

    Conforms to the cross-backend
    :class:`earthlens.base.AbstractAuth` contract (C2): construction
    still authenticates eagerly for backward compatibility, but the
    underlying work lives in :meth:`configure` and is idempotent —
    the second call after :meth:`is_authenticated` returns `True`
    short-circuits.

    Args:
        service_account: The service-account email, e.g.
            `my-sa@my-project.iam.gserviceaccount.com`.
        service_key: Path to the service-account JSON key file, or the
            JSON content as a string.
        project: Cloud project id to scope the Earth Engine calls to.
            If omitted, the key file's `project_id` is used.

    Raises:
        AuthenticationError: If the credentials are missing/invalid, no
            project can be determined, or the project is not registered
            for Earth Engine / not accessible to the service account.

    Examples:
        - Authenticate with a key file:

            ```python
            >>> auth = EarthEngineAuth(  # doctest: +SKIP
            ...     "my-sa@my-project.iam.gserviceaccount.com",
            ...     "/path/to/key.json",
            ... )
            ```
    """

    def __init__(
        self,
        service_account: str,
        service_key: str,
        project: str | None = None,
    ):
        """Authenticate and call `ee.Initialize`; see the class docstring.

        Args:
            service_account: The service-account email.
            service_key: Path to the service-account JSON key file, or
                the JSON content as a string.
            project: Cloud project id; if omitted, read from the key
                file's `project_id`.

        Raises:
            AuthenticationError: As described on :class:`EarthEngineAuth`.
        """
        creds = EarthEngineCredentials(
            service_account=service_account,
            # Wrapped explicitly rather than leaning on pydantic's coercion, so
            # the annotation and the call agree and mypy can check the field.
            service_key=SecretStr(service_key),
            project=project,
        )
        super().__init__(creds)
        # Backward-compat surface: existing callers reach for
        # `auth.service_account` and `auth.project` as plain attrs.
        self.service_account = service_account
        self.project: str | None = None
        self.configure()

    def configure(self) -> None:
        """Authenticate against Earth Engine; idempotent.

        Calls `initialize` on first invocation and caches the
        resolved Cloud project id on `self.project`. Subsequent
        calls short-circuit when `is_authenticated` returns `True`,
        so it is safe to call repeatedly from long-lived workers.

        Raises:
            AuthenticationError: As described on `EarthEngineAuth`
                — missing/invalid key, unresolved project,
                unregistered Earth Engine project, or insufficient
                IAM permissions on the service account.

        Examples:
            - Calling `configure` twice does the network work once
              (the second call short-circuits via
              `is_authenticated`):

                ```python
                >>> auth = EarthEngineAuth(  # doctest: +SKIP
                ...     "my-sa@my-project.iam.gserviceaccount.com",
                ...     "/path/to/key.json",
                ... )
                >>> auth.is_authenticated()  # doctest: +SKIP
                True
                >>> auth.configure()  # no-op  # doctest: +SKIP

                ```
        """
        if self.is_authenticated():
            return
        self.project = self.initialize(
            self._creds.service_account,
            self._creds.service_key.get_secret_value(),
            self._creds.project,
        )

    def is_authenticated(self) -> bool:
        """`True` once `ee.Initialize` has succeeded for this instance.

        Cheap predicate — does not call into the `ee` library or
        the network. Returns `True` exactly when `self.project` is
        set to a non-empty string (the success signal from
        `initialize`).

        Returns:
            bool: `True` after a successful `configure()` /
                construction, `False` otherwise.

        Examples:
            - A fresh, configured instance is authenticated:
                ```python
                >>> auth = EarthEngineAuth(  # doctest: +SKIP
                ...     "my-sa@my-project.iam.gserviceaccount.com",
                ...     "/path/to/key.json",
                ... )
                >>> auth.is_authenticated()  # doctest: +SKIP
                True
                >>> auth.project  # doctest: +SKIP
                'my-project'

                ```
        """
        return bool(self.project)

    @staticmethod
    def initialize(
        service_account: str,
        service_key: str,
        project: str | None = None,
    ) -> str:
        """Authenticate the service account and call `ee.Initialize`.

        The key is dispatched by its shape rather than by trial and error:
        `_is_inline_json` decides whether it reaches
        `ee.ServiceAccountCredentials` as `key_data=` (inline JSON) or
        positionally (a filename), so the call that would embed the key in
        an `open()` failure is never attempted.

        Args:
            service_account: The service-account email.
            service_key: Path to the service-account JSON key file, or
                the JSON content as a string, told apart by a leading `{`.
            project: Cloud project id to scope the calls to. If omitted,
                the key file's `project_id` is used.

        Returns:
            The Cloud project id the connection was initialised with.

        Raises:
            AuthenticationError: For every failure — no project could be
                resolved, the credentials could not be built from the key,
                the project is not registered for Earth Engine, the service
                account lacks permission on it, or `ee.Initialize` failed for
                any other reason. Nothing is chained (`raise ... from None`)
                and every interpolated detail passes through `_redact`,
                because an `ee` error can carry the key itself and a chained
                traceback would print it.

        Examples:
            - Initialise from a key file (requires network + a registered project):
                ```python
                >>> EarthEngineAuth.initialize(  # doctest: +SKIP
                ...     "my-sa@my-project.iam.gserviceaccount.com",
                ...     "/path/to/key.json",
                ... )
                'my-project'

                ```
            - A key with no `project_id` and no explicit `project` fails fast:
                ```python
                >>> import json
                >>> bad_key = json.dumps({"type": "service_account"})
                >>> EarthEngineAuth.initialize("sa@x.iam", bad_key)  # doctest: +IGNORE_EXCEPTION_DETAIL
                Traceback (most recent call last):
                    ...
                earthlens.gee.auth.AuthenticationError: no Earth Engine Cloud project

                ```
        """
        key_dict = _load_key_dict(service_key)
        resolved_project = project or (key_dict or {}).get("project_id")
        if not resolved_project:
            raise AuthenticationError(
                "no Earth Engine Cloud project: pass project=, or use a "
                "service-account key file that includes a 'project_id' "
                f"field. See {_SERVICE_ACCOUNT_DOCS}."
            )

        # `ee.ServiceAccountCredentials` takes a *filename* positionally, so
        # inline JSON must go to `key_data=`. Handing the content positionally
        # makes `open()` raise FileNotFoundError with the whole key as the
        # "filename", and the traceback then prints the private key - which is
        # how a key reached a public CI log once. Choose by shape instead of
        # letting the wrong call fail.
        try:
            if _is_inline_json(service_key):
                credentials = ee.ServiceAccountCredentials(
                    service_account, key_data=service_key
                )
            else:
                credentials = ee.ServiceAccountCredentials(service_account, service_key)
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            # `from None`, not `from exc`: the cause's message can embed the
            # key, and a chained traceback prints it. The detail is preserved
            # through _redact instead, which cannot echo key material.
            detail = _redact(str(exc), service_key)
            raise AuthenticationError(
                "could not build service-account credentials from the "
                f"supplied key (account={service_account!r}): {detail}. Check "
                f"that the key file/JSON is valid. See {_SERVICE_ACCOUNT_DOCS}."
            ) from None

        try:
            ee.Initialize(credentials=credentials, project=resolved_project)
        except ee.EEException as exc:
            # Classify on the raw text and report the redacted one. The needles
            # are fixed substrings that hold no key material, so classification
            # is safe on the raw message - whereas classifying on the redacted
            # text loses the actionable branches whenever a credential marker
            # collapsed it to the sentinel, which is the one case where naming
            # the unregistered project or the missing IAM role matters most.
            raw = str(exc)
            message = _redact(raw, service_key)
            if "not registered to use Earth Engine" in raw:
                raise AuthenticationError(
                    f"Cloud project {resolved_project!r} is not registered "
                    f"to use Earth Engine. Register it at {_REGISTER_URL} "
                    "(pick the noncommercial track if eligible), then retry."
                ) from None
            if (
                "does not have required permission" in raw
                or "serviceUsageConsumer" in raw
                or "PERMISSION_DENIED" in raw
            ):
                raise AuthenticationError(
                    f"service account {service_account!r} cannot use project "
                    f"{resolved_project!r}: grant it the "
                    "'roles/serviceusage.serviceUsageConsumer' and "
                    "'roles/earthengine.viewer' IAM roles on that project."
                ) from None
            raise AuthenticationError(
                f"Earth Engine initialisation failed for project "
                f"{resolved_project!r}: {message}"
            ) from None
        except Exception as exc:  # noqa: BLE001 - re-raised as AuthenticationError
            raise AuthenticationError(
                f"Earth Engine initialisation failed for project "
                f"{resolved_project!r}: {_redact(str(exc), service_key)}"
            ) from None

        return resolved_project

    @staticmethod
    def encode_service_account(service_key_path: str) -> bytes:
        """Base64-encode a service-account JSON key file.

        Useful for shipping a key through an environment variable or CI
        secret without newlines.

        Args:
            service_key_path: Path to the service-account JSON key file.

        Returns:
            The base64-encoded JSON content as a byte string.

        Examples:
            - Encode a tiny key file and inspect the result:
                ```python
                >>> import json, os, tempfile
                >>> p = os.path.join(tempfile.mkdtemp(), "key.json")
                >>> _ = open(p, "w").write(json.dumps({"type": "service_account", "project_id": "demo"}))
                >>> blob = EarthEngineAuth.encode_service_account(p)
                >>> EarthEngineAuth.decode_service_account(blob)
                {'type': 'service_account', 'project_id': 'demo'}

                ```

        See Also:
            decode_service_account: The inverse operation.
        """
        content = json.loads(Path(service_key_path).read_text())
        return base64.b64encode(json.dumps(content).encode())

    @staticmethod
    def decode_service_account(service_key_bytes: bytes) -> dict[str, Any]:
        """Decode a base64-encoded service-account key back to a mapping.

        Inverse of :meth:`encode_service_account`.

        Args:
            service_key_bytes: The base64-encoded JSON content.

        Returns:
            The decoded service-account key as a dictionary.

        Examples:
            - Round-trip a key dict through encode then decode:
                ```python
                >>> import base64, json
                >>> blob = base64.b64encode(json.dumps({"client_email": "sa@p.iam", "project_id": "p"}).encode())
                >>> decoded = EarthEngineAuth.decode_service_account(blob)
                >>> decoded["client_email"]
                'sa@p.iam'
                >>> decoded["project_id"]
                'p'

                ```

        See Also:
            encode_service_account: The inverse operation.
        """
        return cast(
            "dict[str, Any]", json.loads(base64.b64decode(service_key_bytes).decode())
        )
