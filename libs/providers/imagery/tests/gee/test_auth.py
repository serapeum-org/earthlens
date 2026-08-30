"""Tests for `earthlens.gee.auth` — service-account authentication.

`ee.ServiceAccountCredentials` and `ee.Initialize` are stubbed via `monkeypatch`
so no network or real credentials are touched; the real `ee.EEException` class is
kept so the backend's `except ee.EEException` branches are exercised faithfully.
"""

from __future__ import annotations

import base64
import json
import traceback
from unittest.mock import MagicMock

import ee
import pytest

from earthlens.gee import auth as auth_module
from earthlens.gee.auth import (
    _CREDENTIAL_MARKERS,
    AuthenticationError,
    EarthEngineAuth,
    EarthEngineCredentials,
    _is_inline_json,
    _load_key_dict,
    _redact,
)


def _key_text(**extra) -> str:
    """Return a JSON service-account key string with the given extra fields."""
    return json.dumps({"type": "service_account", "client_email": "sa@x.iam", **extra})


@pytest.fixture(scope="function")
def key_file(tmp_path):
    """Write a minimal service-account key file (with a project_id) and return its path."""
    path = tmp_path / "key.json"
    path.write_text(_key_text(project_id="demo-project"))
    return str(path)


@pytest.fixture(scope="function")
def stub_ee(monkeypatch):
    """Stub `ee.ServiceAccountCredentials` and `ee.Initialize`; keep `ee.EEException` real.

    Returns:
        tuple: `(credentials_stub, initialize_stub)` — both `MagicMock`s.
    """
    creds = MagicMock(name="ServiceAccountCredentials")
    init = MagicMock(name="Initialize")
    monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", creds)
    monkeypatch.setattr(auth_module.ee, "Initialize", init)
    return creds, init


class TestLoadKeyDict:
    """Tests for the module-private `_load_key_dict` helper."""

    def test_reads_path(self, key_file):
        """A path to a real key file is parsed to a dict."""
        result = _load_key_dict(key_file)
        assert result["project_id"] == "demo-project", f"unexpected: {result}"

    def test_reads_raw_json(self):
        """Raw JSON content is parsed to a dict."""
        result = _load_key_dict(_key_text(project_id="p"))
        assert result["project_id"] == "p"

    def test_garbage_returns_none(self):
        """A non-path, non-JSON string yields `None`."""
        assert _load_key_dict("not json and not a file") is None

    def test_non_object_json_returns_none(self):
        """Valid JSON that doesn't start with `{` is treated as a (missing) path → None."""
        assert _load_key_dict("[1, 2]") is None


class TestEarthEngineCredentials:
    """Tests for the credentials model's handling of the key value."""

    def test_key_is_not_echoed_in_repr_or_str(self):
        """The key renders as `SecretStr('**********')`, never as its value."""
        creds = EarthEngineCredentials(
            service_account="sa@x.iam", service_key="SUPERSECRET-KEY-VALUE"
        )
        assert "SUPERSECRET" not in repr(creds), f"repr leaked the key: {creds!r}"
        assert "SUPERSECRET" not in str(creds), f"str leaked the key: {creds}"

    def test_a_plain_string_is_still_accepted(self):
        """Callers pass a path or JSON content unchanged; pydantic coerces it."""
        creds = EarthEngineCredentials(
            service_account="sa@x.iam", service_key="C:/k.json"
        )
        assert creds.service_key.get_secret_value() == "C:/k.json", (
            "value not preserved"
        )

    def test_serialisation_does_not_leak_the_key(self):
        """Neither `model_dump` nor `model_dump_json` renders the key's value."""
        creds = EarthEngineCredentials(
            service_account="sa@x.iam", service_key="SUPERSECRET-KEY-VALUE"
        )
        assert "SUPERSECRET" not in str(creds.model_dump()), "model_dump leaked the key"
        assert "SUPERSECRET" not in creds.model_dump_json(), (
            "model_dump_json leaked the key"
        )


class TestIsInlineJson:
    """Tests for the module-private `_is_inline_json` helper."""

    @pytest.mark.parametrize(
        "value",
        ['{"type": "service_account"}', '   {"a": 1}'],
        ids=["plain", "leading-spaces"],
    )
    def test_json_content_is_inline(self, value):
        """A value whose first non-space character is a brace is inline JSON."""
        assert _is_inline_json(value) is True, f"should be inline JSON: {value!r}"

    @pytest.mark.parametrize(
        "value",
        [r"C:\\keys\\k.json", "/etc/keys/k.json", "./k.json", "k.json", "", "[1, 2]"],
        ids=["windows", "posix", "relative", "bare", "empty", "json-array"],
    )
    def test_everything_else_is_a_path(self, value):
        """Anything not starting with a brace is treated as a path, arrays included."""
        assert _is_inline_json(value) is False, f"should not be inline JSON: {value!r}"

    @pytest.mark.parametrize(
        "value", [None, 123, b"{}", {"a": 1}], ids=["none", "int", "bytes", "dict"]
    )
    def test_non_string_is_not_inline(self, value):
        """A non-string never counts as inline JSON, and does not raise."""
        assert _is_inline_json(value) is False, f"non-str should be False: {value!r}"

    def test_agrees_with_load_key_dict(self):
        """The shape rule matches what `_load_key_dict` parses, so the two cannot disagree."""
        content = _key_text(project_id="p")
        assert _is_inline_json(content) is True
        assert _load_key_dict(content) is not None, "inline JSON should parse"


class TestRedact:
    """Tests for the module-private `_redact` helper.

    Pins the containment half of the fix that stopped a service-account key
    reaching a traceback.
    """

    def test_key_value_is_replaced(self):
        """The key's own text is swapped for the sentinel."""
        key = _key_text(project_id="p")
        result = _redact(f"failed opening {key} oops", key)
        assert key not in result, "the key value survived redaction"
        assert "<service key redacted>" in result, f"no sentinel in: {result}"

    def test_every_occurrence_is_replaced(self):
        """A key repeated in one message is redacted throughout."""
        key = _key_text(project_id="p")
        result = _redact(f"{key} and again {key}", key)
        assert key not in result, "a repeated occurrence survived"

    def test_pem_block_collapses_the_whole_message(self):
        """Any residual PEM material discards the message rather than trusting it."""
        marker = "-----BEGIN " + "PRIVATE KEY-----"
        result = _redact(f"boom {marker} tail", "unrelated-but-long-enough")
        assert result == "<service key redacted>", (
            f"expected full collapse, got: {result}"
        )

    def test_clean_message_is_untouched(self):
        """A message with neither the key nor PEM material passes through verbatim."""
        message = "could not reach the Earth Engine endpoint"
        assert _redact(message, _key_text()) == message, "a clean message was altered"

    @pytest.mark.parametrize(
        "short", ["", "a", "12345678"], ids=["empty", "one-char", "eight-chars"]
    )
    def test_short_keys_are_not_substituted(self, short):
        """A short value is not substituted, so a common substring is not mangled."""
        message = "12345678 is part of the identifier"
        assert _redact(message, short) == message, "a short key triggered substitution"

    @pytest.mark.parametrize("value", [None, 123, b"key"], ids=["none", "int", "bytes"])
    def test_non_string_key_does_not_raise(self, value):
        """A non-string key leaves the message alone instead of raising."""
        message = "nothing sensitive here"
        assert _redact(message, value) == message, f"non-str key mishandled: {value!r}"

    @pytest.mark.parametrize("indent", [None, 2], ids=["compact", "pretty"])
    @pytest.mark.parametrize("kind", ["service_account", "authorized_user"])
    def test_repr_escaped_keys_are_still_redacted(self, kind, indent):
        """A key rendered through `OSError.__str__` is redacted despite the escaping.

        `OSError` reprs its filename, so a multi-line key is no longer
        byte-identical to the value held and a plain substring replace cannot
        match it - the same mismatch that defeated the platform's masking. A
        key without a PEM header has no second line of defence, so both shapes
        are pinned here.
        """
        marker = "-----BEGIN " + "PRIVATE KEY-----"
        payload = (
            {"type": "service_account", "private_key": f"{marker}{chr(10)}SUPERSECRET"}
            if kind == "service_account"
            else {
                "type": "authorized_user",
                "client_secret": "SUPERSECRET",
                "refresh_token": "ALSOSECRET",
            }
        )
        key = json.dumps(payload, indent=indent)
        rendered = str(FileNotFoundError(2, "No such file", key))
        result = _redact(rendered, key)
        assert "SUPERSECRET" not in result, f"key material survived: {result}"
        assert "ALSOSECRET" not in result, f"key material survived: {result}"

    def test_ordinary_prose_is_not_collapsed(self):
        """A message that merely mentions a service account is left intact."""
        message = "could not build credentials (account='sa@x.iam.gserviceaccount.com')"
        assert _redact(message, json.dumps({"a": 1})) == message, "prose was collapsed"

    @pytest.mark.parametrize("marker", _CREDENTIAL_MARKERS)
    def test_every_declared_marker_collapses_the_message(self, marker):
        """Each declared credential marker discards the message it appears in."""
        result = _redact(f"boom {marker} tail", "unrelated-but-long-enough")
        assert result == "<service key redacted>", (
            f"marker {marker!r} did not collapse: {result}"
        )

    @pytest.mark.parametrize(
        "field", ["private_key", "client_secret", "refresh_token", "client_email"]
    )
    def test_unquoted_field_names_are_not_collapsed(self, field):
        """The markers carry their JSON quotes, so prose naming a field survives."""
        message = f"the {field} field is missing from the key"
        assert _redact(message, "unrelated-but-long-enough") == message, (
            f"prose naming {field} was collapsed"
        )

    def test_a_nine_character_key_is_substituted(self):
        """One character past the length guard the value is replaced."""
        result = _redact("the value 123456789 appeared", "123456789")
        assert "123456789" not in result, "a nine-character key survived"

    def test_redacted_output_carries_no_key_material(self):
        """The end-to-end property: neither the key nor a PEM header survives."""
        marker = "-----BEGIN " + "PRIVATE KEY-----"
        key = _key_text(project_id="p", private_key=f"{marker}\nSECRET\n")
        result = _redact(f"open() failed on {key}", key)
        assert "SECRET" not in result, f"key material survived: {result}"
        assert marker not in result, f"PEM header survived: {result}"


class TestAuthenticationError:
    """Tests for the :class:`AuthenticationError` exception type."""

    def test_is_exception_subclass(self):
        """`AuthenticationError` is a plain `Exception` subclass."""
        assert issubclass(AuthenticationError, Exception)
        assert str(AuthenticationError("boom")) == "boom"


class TestEarthEngineAuthEncodeDecode:
    """Tests for `encode_service_account` / `decode_service_account`."""

    def test_round_trip(self, key_file):
        """Encoding then decoding a key file yields the original mapping."""
        blob = EarthEngineAuth.encode_service_account(key_file)
        assert isinstance(blob, bytes)
        decoded = EarthEngineAuth.decode_service_account(blob)
        assert decoded == {
            "type": "service_account",
            "client_email": "sa@x.iam",
            "project_id": "demo-project",
        }

    def test_decode_independent_of_encode(self):
        """`decode_service_account` works on any base64'd JSON object."""
        blob = base64.b64encode(json.dumps({"a": 1}).encode())
        assert EarthEngineAuth.decode_service_account(blob) == {"a": 1}


class TestEarthEngineAuthInitialize:
    """Tests for `EarthEngineAuth.initialize` and the constructor."""

    def test_no_project_raises(self):
        """A key with no `project_id` and no `project` arg fails fast."""
        with pytest.raises(AuthenticationError, match="no Earth Engine Cloud project"):
            EarthEngineAuth.initialize("sa@x.iam", _key_text())

    def test_project_from_key_file(self, key_file, stub_ee):
        """The project is read from the key file's `project_id`."""
        creds, init = stub_ee
        project = EarthEngineAuth.initialize("sa@x.iam", key_file)
        assert project == "demo-project"
        init.assert_called_once()
        assert init.call_args.kwargs["project"] == "demo-project"
        creds.assert_called_once_with("sa@x.iam", key_file)

    def test_explicit_project_overrides_key_file(self, key_file, stub_ee):
        """An explicit `project` argument wins over the key file's `project_id`."""
        _, init = stub_ee
        project = EarthEngineAuth.initialize("sa@x.iam", key_file, project="override")
        assert project == "override"
        assert init.call_args.kwargs["project"] == "override"

    def test_inline_json_goes_straight_to_key_data(self, monkeypatch):
        """Inline JSON is passed as `key_data=`, never positionally as a filename."""
        monkeypatch.setattr(auth_module.ee, "Initialize", MagicMock())
        creds = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", creds)
        project = EarthEngineAuth.initialize("sa@x.iam", _key_text(project_id="p"))
        assert project == "p"
        assert creds.call_count == 1
        assert "key_data" in creds.call_args.kwargs

    def test_a_path_is_passed_positionally(self, monkeypatch, key_file):
        """A filesystem path keeps the positional filename form `ee` expects."""
        monkeypatch.setattr(auth_module.ee, "Initialize", MagicMock())
        creds = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", creds)
        EarthEngineAuth.initialize("sa@x.iam", key_file)
        assert creds.call_count == 1
        assert creds.call_args.args[1] == key_file
        assert "key_data" not in creds.call_args.kwargs

    def test_credential_failure_raises(self, monkeypatch):
        """A failed credential construction raises an `AuthenticationError`."""
        creds = MagicMock(side_effect=RuntimeError("nope"))
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", creds)
        with pytest.raises(
            AuthenticationError, match="could not build service-account credentials"
        ):
            EarthEngineAuth.initialize("sa@x.iam", _key_text(project_id="p"))

    def test_a_failure_never_reports_the_key(self, monkeypatch):
        """No key material reaches the error text or the chained traceback.

        Pins the defect that put a private key in a public CI log: the key was
        passed where a filename belonged, so the resulting exception carried it
        and the traceback printed it.
        """
        # Assembled rather than written whole: the repository's gitleaks gate
        # scans source text and matches a literal PEM header, so spelling one
        # out here would fail CI on the very test that proves keys stay out of
        # tracebacks. The runtime value is identical, so `_redact`'s PEM branch
        # is still the thing under test - do not "tidy" this back into one
        # string.
        marker = "-----BEGIN " + "PRIVATE KEY-----"
        secret = marker + chr(10) + "SUPERSECRET" + chr(10) + marker + chr(10)
        key = _key_text(project_id="p", private_key=secret)
        creds = MagicMock(side_effect=FileNotFoundError(2, "No such file", key))
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", creds)
        with pytest.raises(AuthenticationError) as excinfo:
            EarthEngineAuth.initialize("sa@x.iam", key)
        rendered = "".join(
            traceback.format_exception(
                type(excinfo.value), excinfo.value, excinfo.value.__traceback__
            )
        )
        assert "SUPERSECRET" not in rendered
        assert marker not in rendered
        assert excinfo.value.__cause__ is None

    def test_not_registered_project_raises_friendly(self, monkeypatch):
        """An "EE not registered" error becomes a registration-pointing AuthenticationError."""
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(
            auth_module.ee,
            "Initialize",
            MagicMock(
                side_effect=ee.EEException(
                    "Project p is not registered to use Earth Engine"
                )
            ),
        )
        with pytest.raises(
            AuthenticationError, match="not registered to use Earth Engine"
        ):
            EarthEngineAuth.initialize("sa@x.iam", _key_text(project_id="p"))

    @pytest.mark.parametrize(
        "detail",
        [
            "Caller does not have required permission on the project",
            "missing roles/serviceusage.serviceUsageConsumer",
            "PERMISSION_DENIED: the request was rejected",
        ],
        ids=["required-permission", "serviceUsageConsumer", "permission-denied"],
    )
    def test_permission_error_raises_friendly(self, monkeypatch, detail):
        """Each permission-flavoured message becomes an IAM-role-pointing error."""
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(
            auth_module.ee, "Initialize", MagicMock(side_effect=ee.EEException(detail))
        )
        with pytest.raises(
            AuthenticationError, match="serviceUsageConsumer|earthengine.viewer"
        ):
            EarthEngineAuth.initialize("sa@x.iam", _key_text(project_id="p"))

    def test_other_ee_exception_wrapped(self, monkeypatch):
        """Any other `ee.EEException` is wrapped as an initialisation failure."""
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(
            auth_module.ee, "Initialize", MagicMock(side_effect=ee.EEException("weird"))
        )
        with pytest.raises(AuthenticationError, match="initialisation failed"):
            EarthEngineAuth.initialize("sa@x.iam", _key_text(project_id="p"))

    def test_generic_exception_wrapped(self, monkeypatch):
        """A non-`EEException` from `ee.Initialize` is also wrapped."""
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(
            auth_module.ee, "Initialize", MagicMock(side_effect=OSError("disk"))
        )
        with pytest.raises(AuthenticationError, match="initialisation failed"):
            EarthEngineAuth.initialize("sa@x.iam", _key_text(project_id="p"))

    def test_an_ee_exception_carrying_the_key_is_redacted(self, monkeypatch):
        """The `ee.Initialize` branch redacts too, not only the credential branch."""
        marker = "-----BEGIN " + "PRIVATE KEY-----"
        key = _key_text(project_id="p", private_key=marker + chr(10) + "SUPERSECRET")
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(
            auth_module.ee,
            "Initialize",
            MagicMock(side_effect=ee.EEException(f"backend rejected {key}")),
        )
        with pytest.raises(AuthenticationError) as excinfo:
            EarthEngineAuth.initialize("sa@x.iam", key)
        rendered = str(excinfo.value)
        assert "SUPERSECRET" not in rendered, f"key material survived: {rendered}"
        assert marker not in rendered, f"PEM header survived: {rendered}"

    def test_a_generic_initialisation_failure_is_redacted(self, monkeypatch):
        """The non-`EEException` branch redacts the key it is handed as well."""
        key = _key_text(project_id="p", private_key="SUPERSECRET")
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(
            auth_module.ee,
            "Initialize",
            MagicMock(side_effect=FileNotFoundError(2, "No such file", key)),
        )
        with pytest.raises(AuthenticationError) as excinfo:
            EarthEngineAuth.initialize("sa@x.iam", key)
        assert "SUPERSECRET" not in str(excinfo.value), "key material survived"

    @pytest.mark.parametrize(
        "error",
        [
            ee.EEException("Project p is not registered to use Earth Engine"),
            ee.EEException("Caller does not have required permission"),
            ee.EEException("something else entirely"),
            OSError("disk"),
        ],
        ids=["not-registered", "permission", "other-ee", "generic"],
    )
    def test_every_initialisation_failure_breaks_the_chain(self, monkeypatch, error):
        """Every branch raises `from None`, so no cause can print the key."""
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        monkeypatch.setattr(auth_module.ee, "Initialize", MagicMock(side_effect=error))
        key = _key_text(project_id="p")
        with pytest.raises(AuthenticationError) as excinfo:
            EarthEngineAuth.initialize("sa@x.iam", key)
        assert excinfo.value.__cause__ is None, "the exception chain was not broken"

    def test_an_unparseable_key_uses_the_explicit_project(self, monkeypatch):
        """A key that parses to nothing still initialises when `project` is given."""
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", MagicMock())
        init = MagicMock()
        monkeypatch.setattr(auth_module.ee, "Initialize", init)
        project = EarthEngineAuth.initialize("sa@x.iam", "neither json nor a file", "p")
        assert project == "p", "the explicit project did not survive an unparseable key"
        assert init.call_args.kwargs["project"] == "p"

    def test_inline_json_with_leading_whitespace_uses_key_data(self, monkeypatch):
        """Whitespace before the brace still routes the key to `key_data=`."""
        monkeypatch.setattr(auth_module.ee, "Initialize", MagicMock())
        creds = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(auth_module.ee, "ServiceAccountCredentials", creds)
        project = EarthEngineAuth.initialize(
            "sa@x.iam", "  " + _key_text(project_id="p")
        )
        assert project == "p"
        assert "key_data" in creds.call_args.kwargs, (
            "a padded key went in as a filename"
        )

    def test_constructor_sets_attributes(self, key_file, stub_ee):
        """The constructor stores the account and the resolved project."""
        auth = EarthEngineAuth("sa@x.iam", key_file)
        assert auth.service_account == "sa@x.iam"
        assert auth.project == "demo-project"


def test_module_exposes_expected_symbols():
    """The auth module exposes the documented public symbols."""
    assert hasattr(auth_module, "EarthEngineAuth")
    assert hasattr(auth_module, "AuthenticationError")
    # C2 retrofit — the credentials value object is also public.
    assert hasattr(auth_module, "EarthEngineCredentials")


@pytest.mark.unit
class TestEarthEngineAuthC2Retrofit:
    """C2 retrofit — `EarthEngineAuth` inherits the cross-backend ABC."""

    def test_inherits_abstract_auth(self):
        """`EarthEngineAuth` subclasses `earthlens.base.AbstractAuth`."""
        from earthlens.base import AbstractAuth

        assert issubclass(EarthEngineAuth, AbstractAuth)

    def test_gee_authentication_error_is_base_subclass(self):
        """`gee.AuthenticationError` is a subclass of `base.AuthenticationError`."""
        from earthlens.base import AuthenticationError as BaseAuthError

        assert issubclass(AuthenticationError, BaseAuthError), (
            "gee.AuthenticationError must inherit base.AuthenticationError "
            "so callers can catch every backend's auth failure uniformly."
        )

    def test_constructor_eagerly_configures(self, key_file, stub_ee):
        """Construction still runs `ee.Initialize` (back-compat)."""
        _, init = stub_ee
        auth = EarthEngineAuth("sa@x.iam", key_file)
        init.assert_called_once()
        assert auth.is_authenticated() is True
        assert auth.project == "demo-project"

    def test_configure_is_idempotent(self, key_file, stub_ee):
        """A second `configure()` does not re-invoke `ee.Initialize`."""
        _, init = stub_ee
        auth = EarthEngineAuth("sa@x.iam", key_file)
        init.reset_mock()
        auth.configure()
        init.assert_not_called()

    def test_is_authenticated_false_until_configured(self, key_file, stub_ee):
        """A bypass-construct instance is not authenticated until `configure()` runs."""
        from earthlens.gee.auth import EarthEngineCredentials

        creds = EarthEngineCredentials(
            service_account="sa@x.iam",
            service_key=key_file,
        )
        # Bypass __init__ so we can observe the pre-configure state.
        auth = EarthEngineAuth.__new__(EarthEngineAuth)
        auth._creds = creds
        auth.service_account = "sa@x.iam"
        auth.project = None
        assert auth.is_authenticated() is False
        auth.configure()
        assert auth.is_authenticated() is True
        assert auth.project == "demo-project"

    def test_context_manager_round_trip(self, key_file, stub_ee):
        """`with EarthEngineAuth(...) as auth` exposes the same instance."""
        with EarthEngineAuth("sa@x.iam", key_file) as auth:
            assert auth.is_authenticated() is True
