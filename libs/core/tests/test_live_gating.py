"""Tests for the shared live-test gates: reachability, quarantine and re-raise."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from earthlens.testing import (
    _LIVE_SKIP_PREFIX,
    _live,
    require_reachable,
    reset_reachability_cache,
    skip_on_upstream,
)


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    """Keep one test's reachability verdicts out of the next one."""
    reset_reachability_cache()
    yield
    reset_reachability_cache()


def test_require_reachable_passes_a_reachable_target() -> None:
    """A cached "reachable" verdict lets the test proceed."""
    reset_reachability_cache({"example.org": None})
    require_reachable("example.org")


def test_require_reachable_skips_with_the_guard_prefix() -> None:
    """An unreachable target skips with the prefix both masked-lane guards read."""
    reset_reachability_cache({"example.org": "example.org unreachable (OSError)"})
    with pytest.raises(pytest.skip.Exception) as excinfo:
        require_reachable("example.org")
    assert str(excinfo.value).startswith(_LIVE_SKIP_PREFIX), str(excinfo.value)
    assert "example.org unreachable" in str(excinfo.value)


def test_require_reachable_probes_each_target_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N tests against one host cost one probe, not N."""
    calls: list[str] = []

    def _count(target: str, timeout: float) -> str | None:
        calls.append(target)
        return None

    monkeypatch.setattr(_live, "_probe", _count)
    for _ in range(5):
        require_reachable("example.org")
    require_reachable("other.example.org")
    assert calls == ["example.org", "other.example.org"], calls


def test_probe_uses_a_tcp_connect_for_a_bare_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare host is probed on 443, and `host:port` overrides the port."""
    seen: list[tuple[str, int]] = []

    def _fake_connect(address, timeout):
        seen.append(address)
        return type("_Sock", (), {"close": lambda self: None})()

    monkeypatch.setattr(_live.socket, "create_connection", _fake_connect)
    assert _live._probe("example.org", 1.0) is None
    assert _live._probe("example.org:8080", 1.0) is None
    assert seen == [("example.org", 443), ("example.org", 8080)], seen


def test_probe_uses_head_for_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL target is probed with `HEAD`, following redirects."""
    import requests

    seen: dict[str, object] = {}

    def _fake_head(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return object()

    monkeypatch.setattr(requests, "head", _fake_head)
    assert _live._probe("https://example.org/api", 3.0) is None
    assert seen["url"] == "https://example.org/api"
    assert seen["allow_redirects"] is True
    assert seen["timeout"] == 3.0


def test_probe_reports_the_failure_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection becomes a reason naming the exception type."""

    def _refuse(address, timeout):
        raise OSError("refused")

    monkeypatch.setattr(_live.socket, "create_connection", _refuse)
    assert _live._probe("example.org", 1.0) == "example.org unreachable (OSError)"


def test_probe_reraises_a_blocked_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pytest-socket block is a lane error, not an upstream outage."""

    class SocketConnectBlockedError(RuntimeError):
        """Named to match pytest-socket's, which is what the probe keys on."""

    def _blocked(address, timeout):
        raise SocketConnectBlockedError("no sockets in this lane")

    monkeypatch.setattr(_live.socket, "create_connection", _blocked)
    with pytest.raises(SocketConnectBlockedError):
        _live._probe("example.org", 1.0)


def test_skip_on_upstream_skips_an_availability_failure() -> None:
    """A 503 becomes a prefixed skip rather than a lane failure."""
    import requests

    with pytest.raises(pytest.skip.Exception) as excinfo:
        skip_on_upstream(requests.ConnectionError("boom"))
    assert str(excinfo.value).startswith(_LIVE_SKIP_PREFIX), str(excinfo.value)


def test_skip_on_upstream_reraises_a_real_failure() -> None:
    """A request-shaping error stays the failure it is."""
    with pytest.raises(ValueError, match="bad bbox"):
        skip_on_upstream(ValueError("bad bbox"))


def _run_lane(
    tmp_path: Path, body: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run a throwaway lane wired to the shared hooks in a subprocess."""
    (tmp_path / "conftest.py").write_text(
        "from earthlens.testing import (  # noqa: F401\n"
        "    pytest_collection_modifyitems,\n"
        "    pytest_runtest_call,\n"
        "    pytest_sessionfinish,\n"
        "    require_marked_hosts,\n"
        ")\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n"
        "    e2e: live end-to-end test\n"
        "    quarantined: held back\n"
        "    requires_host: host gate\n",
        encoding="utf-8",
    )
    (tmp_path / "test_lane.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-rs",
            "-q",
            *args,
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.slow
def test_requires_host_marker_skips_at_run_time(tmp_path: Path) -> None:
    """The marker gate probes when the test runs, and the skip carries the prefix."""
    result = _run_lane(
        tmp_path,
        """
        import pytest
        from earthlens.testing import reset_reachability_cache

        reset_reachability_cache({"down.invalid": "down.invalid unreachable"})

        @pytest.mark.e2e
        @pytest.mark.requires_host("down.invalid")
        def test_gated():
            raise AssertionError("must not run")
        """,
    )
    # One availability skip and no pass is a wholly masked lane, which the
    # session guard fails on purpose — that it fired proves the prefix landed.
    assert "live e2e skipped" in result.stdout, result.stdout
    assert "wholly masked" in result.stdout, result.stdout


@pytest.mark.slow
def test_quarantined_is_held_back_by_default(tmp_path: Path) -> None:
    """A quarantined test does not run, and says why, without reddening the lane."""
    result = _run_lane(
        tmp_path,
        """
        import pytest

        @pytest.mark.e2e
        @pytest.mark.quarantined
        def test_flaky():
            raise AssertionError("must not run")

        @pytest.mark.e2e
        def test_healthy():
            assert True
        """,
    )
    assert result.returncode == 0, result.stdout
    assert "quarantined" in result.stdout, result.stdout


@pytest.mark.slow
def test_quarantined_runs_when_selected(tmp_path: Path) -> None:
    """Naming the marker in `-m` runs the held-back tests."""
    result = _run_lane(
        tmp_path,
        """
        import pytest

        @pytest.mark.e2e
        @pytest.mark.quarantined
        def test_flaky():
            raise AssertionError("ran as asked")
        """,
        "-m",
        "quarantined",
    )
    assert result.returncode != 0, result.stdout
    assert "ran as asked" in result.stdout, result.stdout
