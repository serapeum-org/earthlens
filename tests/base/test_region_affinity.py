"""Unit tests for `earthlens.base.region` (affinity, probe, egress warning)."""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from earthlens.base import region as region_mod
from earthlens.base.region import (
    _normalize_gcp_zone,
    region_affinity,
    warn_if_egress,
)


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """Reset the per-process probe cache around every test."""
    region_mod.clear_region_cache()
    yield
    region_mod.clear_region_cache()


@pytest.fixture
def no_aws_env(monkeypatch: pytest.MonkeyPatch):
    """Remove ambient AWS region env vars so the probe path is exercised."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


@pytest.fixture
def warnings_sink():
    """Capture WARNING-level loguru messages into a list."""
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink_id)


def _fake_metadata(mapping: dict[str, str | None]):
    """Build a _metadata_request stand-in over a url -> body mapping."""

    def _request(url: str, **kwargs: Any) -> str | None:
        return mapping.get(url)

    return _request


@pytest.mark.unit
class TestEnvResolution:
    """Caller-region resolution from explicit arg and environment."""

    def test_aws_region_env_is_in_region(self, monkeypatch: pytest.MonkeyPatch):
        """AWS_REGION supplies the caller region for an in-region match."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert region_affinity("us-west-2", probe=False) == "in-region"

    def test_aws_default_region_env_is_used(self, monkeypatch: pytest.MonkeyPatch):
        """AWS_DEFAULT_REGION is the fallback when AWS_REGION is unset."""
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        assert region_affinity("eu-west-1", probe=False) == "in-region"

    def test_env_mismatch_is_egress(self, monkeypatch: pytest.MonkeyPatch):
        """A known caller region differing from the bucket is egress."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert region_affinity("eu-west-1", probe=False) == "egress"

    def test_explicit_caller_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        """An explicit caller_region takes precedence over the environment."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert region_affinity("eu-west-1", caller_region="eu-west-1") == "in-region"

    def test_empty_bucket_is_unknown(self):
        """An empty bucket_region yields unknown regardless of caller."""
        assert region_affinity("", caller_region="us-west-2") == "unknown"

    def test_no_signal_is_unknown(self, no_aws_env):
        """No explicit caller, no env, and no probe yields unknown."""
        assert region_affinity("us-west-2", probe=False) == "unknown"


@pytest.mark.unit
class TestProbe:
    """The instance-metadata probe fallback and its cache."""

    def test_aws_probe_hit(self, monkeypatch: pytest.MonkeyPatch, no_aws_env):
        """The AWS IMDSv2 token+region probe resolves the caller region."""
        monkeypatch.setattr(
            region_mod,
            "_metadata_request",
            _fake_metadata(
                {
                    region_mod._AWS_TOKEN_URL: "token",
                    region_mod._AWS_REGION_URL: "us-west-2",
                }
            ),
        )
        assert region_affinity("us-west-2") == "in-region"

    def test_gcp_probe_hit_normalises_zone(
        self, monkeypatch: pytest.MonkeyPatch, no_aws_env
    ):
        """A GCP zone is normalised to its region before comparison."""
        monkeypatch.setattr(
            region_mod,
            "_metadata_request",
            _fake_metadata(
                {region_mod._GCP_ZONE_URL: "projects/9/zones/europe-west4-b"}
            ),
        )
        assert region_affinity("europe-west4") == "in-region"

    def test_azure_probe_hit(self, monkeypatch: pytest.MonkeyPatch, no_aws_env):
        """The Azure IMDS location probe resolves the caller region."""
        monkeypatch.setattr(
            region_mod,
            "_metadata_request",
            _fake_metadata({region_mod._AZURE_LOCATION_URL: "westus2"}),
        )
        assert region_affinity("westus2") == "in-region"

    def test_probe_all_fail_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, no_aws_env
    ):
        """When no metadata endpoint answers, the caller stays unknown."""
        monkeypatch.setattr(region_mod, "_metadata_request", _fake_metadata({}))
        assert region_affinity("us-west-2") == "unknown"

    def test_probe_false_skips_network(
        self, monkeypatch: pytest.MonkeyPatch, no_aws_env
    ):
        """probe=False never touches the metadata seam."""

        def _boom(*args: Any, **kwargs: Any) -> str | None:
            raise AssertionError("probe must not run when probe=False")

        monkeypatch.setattr(region_mod, "_metadata_request", _boom)
        assert region_affinity("us-west-2", probe=False) == "unknown"

    def test_probe_result_is_cached(self, monkeypatch: pytest.MonkeyPatch, no_aws_env):
        """The probe runs once; a second call reuses the cached region."""
        calls: list[str] = []

        def _counting(url: str, **kwargs: Any) -> str | None:
            calls.append(url)
            return "westus2" if url == region_mod._AZURE_LOCATION_URL else None

        monkeypatch.setattr(region_mod, "_metadata_request", _counting)
        assert region_affinity("westus2") == "in-region"
        first = len(calls)
        assert region_affinity("westus2") == "in-region"
        assert len(calls) == first


@pytest.mark.unit
class TestMetadataRequest:
    """The single network seam's fail-safe branches."""

    def test_returns_body_on_200(self, monkeypatch: pytest.MonkeyPatch):
        """A 200 response returns its stripped body."""
        monkeypatch.setattr(
            region_mod.urllib.request, "urlopen", _fake_urlopen(200, b"us-west-2 ")
        )
        assert region_mod._metadata_request("http://x", headers={}) == "us-west-2"

    def test_none_on_non_200(self, monkeypatch: pytest.MonkeyPatch):
        """A non-200 status yields None."""
        monkeypatch.setattr(
            region_mod.urllib.request, "urlopen", _fake_urlopen(404, b"nope")
        )
        assert region_mod._metadata_request("http://x", headers={}) is None

    def test_none_on_empty_body(self, monkeypatch: pytest.MonkeyPatch):
        """An empty body yields None."""
        monkeypatch.setattr(
            region_mod.urllib.request, "urlopen", _fake_urlopen(200, b"  ")
        )
        assert region_mod._metadata_request("http://x", headers={}) is None

    def test_none_on_oserror(self, monkeypatch: pytest.MonkeyPatch):
        """A transport error (OSError/timeout) yields None, not a raise."""

        def _raise(*args: Any, **kwargs: Any):
            raise OSError("no route")

        monkeypatch.setattr(region_mod.urllib.request, "urlopen", _raise)
        assert region_mod._metadata_request("http://x", headers={}) is None


@pytest.mark.unit
class TestNormalizeGcpZone:
    """GCP zone-string to region normalisation."""

    @pytest.mark.parametrize(
        "zone, expected",
        [
            ("projects/9/zones/us-west1-a", "us-west1"),
            ("us-west1-a", "us-west1"),
            ("westus2", "westus2"),
            ("", None),
        ],
    )
    def test_normalise(self, zone: str, expected: str | None):
        """Path and trailing zone letter are stripped to the region."""
        assert _normalize_gcp_zone(zone) == expected


@pytest.mark.unit
class TestWarnIfEgress:
    """The egress warning helper."""

    def test_warns_above_threshold(self, warnings_sink):
        """A large cross-region pull emits one egress warning."""
        hint = warn_if_egress(
            "us-west-2", size_bytes=2 << 30, caller_region="eu-central-1"
        )
        assert hint == "egress"
        assert any("egress" in m.lower() for m in warnings_sink)

    def test_no_warn_below_threshold(self, warnings_sink):
        """A small egress transfer does not warn."""
        hint = warn_if_egress(
            "us-west-2", size_bytes=1024, caller_region="eu-central-1"
        )
        assert hint == "egress"
        assert warnings_sink == []

    def test_no_warn_when_in_region(self, warnings_sink):
        """An in-region transfer never warns, regardless of size."""
        hint = warn_if_egress(
            "us-west-2", size_bytes=5 << 30, caller_region="us-west-2"
        )
        assert hint == "in-region"
        assert warnings_sink == []

    def test_custom_threshold_fires(self, warnings_sink):
        """A custom threshold below the size triggers the warning."""
        hint = warn_if_egress(
            "us-west-2",
            size_bytes=200,
            threshold_bytes=100,
            caller_region="eu-central-1",
        )
        assert hint == "egress"
        assert len(warnings_sink) == 1

    def test_returns_unknown_without_signal(self, warnings_sink, no_aws_env):
        """No caller signal yields unknown and no warning."""
        hint = warn_if_egress("us-west-2", size_bytes=5 << 30, probe=False)
        assert hint == "unknown"
        assert warnings_sink == []


class _FakeResp:
    """Context-manager stand-in for a urlopen response."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _fake_urlopen(status: int, body: bytes):
    """Build a urlopen stand-in returning a canned response."""

    def _open(request: Any, timeout: float | None = None) -> _FakeResp:
        return _FakeResp(status, body)

    return _open
