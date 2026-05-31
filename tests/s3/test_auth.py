"""Unit tests for `earthlens.s3.auth`."""

from __future__ import annotations

import builtins

import pytest

from earthlens.base import AbstractAuth
from earthlens.s3.auth import S3Auth, S3Credentials

pytestmark = [pytest.mark.s3]


def test_is_a_subclass_of_abstract_auth():
    """S3Auth honours the shared auth contract."""
    assert issubclass(S3Auth, AbstractAuth)


def test_client_is_lazy_then_built():
    """The client is absent until configure/client runs, then present."""
    auth = S3Auth(S3Credentials())
    assert auth.is_authenticated() is False
    client = auth.client()
    assert client is not None and auth.is_authenticated() is True


def test_configure_is_idempotent():
    """A second configure does not rebuild the client."""
    auth = S3Auth(S3Credentials())
    auth.configure()
    first = auth.client()
    auth.configure()
    assert auth.client() is first


def test_close_drops_the_client():
    """close resets the auth back to unauthenticated."""
    auth = S3Auth(S3Credentials())
    auth.client()
    auth.close()
    assert auth.is_authenticated() is False


def test_unsigned_is_the_default():
    """The default credentials select unsigned public access."""
    assert S3Credentials().aws_profile is None


def test_aws_profile_builds_a_session_client(monkeypatch):
    """A profile credential routes through a boto3 Session."""
    import boto3

    calls = {}

    class _Session:
        def __init__(self, profile_name=None):
            calls["profile"] = profile_name

        def client(self, name, region_name=None):
            calls["service"] = name
            return object()

    monkeypatch.setattr(boto3, "Session", _Session)
    auth = S3Auth(S3Credentials(aws_profile="myprofile"))
    auth.configure()
    assert calls == {"profile": "myprofile", "service": "s3"}


def test_signed_credentials_build_a_signed_client():
    """signed=True builds a signed client (not the UNSIGNED public one)."""
    from botocore import UNSIGNED

    auth = S3Auth(S3Credentials(signed=True, region="us-west-2"))
    client = auth.client()
    assert client.meta.config.signature_version is not UNSIGNED
    assert client.meta.region_name == "us-west-2"


def test_unsigned_is_used_for_public_buckets():
    """The default (unsigned) client uses the UNSIGNED signer."""
    from botocore import UNSIGNED

    assert S3Auth(S3Credentials()).client().meta.config.signature_version is UNSIGNED


def test_missing_boto3_raises_friendly_importerror(monkeypatch):
    """A missing boto3 surfaces an ImportError naming the extra."""
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.startswith("boto3") or name.startswith("botocore"):
            raise ImportError("no boto3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(ImportError, match=r"earthlens\[s3\]"):
        S3Auth(S3Credentials()).configure()
