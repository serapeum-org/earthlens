"""The non-e2e lanes block sockets, and say what to do when one is opened."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CONFTEST = (
    "from earthlens.testing import (  # noqa: F401\n"
    "    pytest_collection_modifyitems,\n"
    "    pytest_configure,\n"
    "    pytest_runtest_call,\n"
    "    pytest_sessionfinish,\n"
    "    require_marked_hosts,\n"
    ")\n"
)

BLOCK_FLAGS = ["--disable-socket", "--allow-unix-socket", "--allow-hosts=127.0.0.1,::1"]


def _run(tmp_path: Path, body: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a throwaway lane wired to the shared hooks, returning the result."""
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_lane.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-q",
            *args,
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.slow
def test_a_blocked_socket_explains_the_choice(tmp_path: Path) -> None:
    """The failure names the two ways out, not just `SocketBlockedError`."""
    result = _run(
        tmp_path,
        """
        import socket

        def test_reaches_out():
            socket.create_connection(("example.com", 443), timeout=1)
        """,
        *BLOCK_FLAGS,
    )
    assert result.returncode != 0, result.stdout
    assert "must not touch the network" in result.stdout, result.stdout
    assert "@pytest.mark.e2e" in result.stdout, result.stdout
    assert "requires_host" in result.stdout, result.stdout


@pytest.mark.slow
def test_loopback_stays_open_for_an_emulator(tmp_path: Path) -> None:
    """`--allow-hosts=127.0.0.1` keeps a local moto/fake-gcs usable."""
    result = _run(
        tmp_path,
        """
        import socket

        def test_loopback_allowed():
            # Nothing is listening, so a refused connection is the success
            # case here: the *block* did not fire, the OS did.
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            try:
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
            finally:
                server.close()
        """,
        *BLOCK_FLAGS,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.slow
def test_the_block_does_not_see_collection_time_sockets(tmp_path: Path) -> None:
    """Measured, and the reason the probe migration had to come first.

    pytest-socket installs its block per test, so a socket opened while the
    module is being imported is not blocked at all. `--disable-socket` is the
    regression net behind `requires_host`, not a substitute for it — which is
    why the AST rule in test_distribution_boundaries.py exists too.
    """
    result = _run(
        tmp_path,
        """
        import socket

        try:
            socket.create_connection(("127.0.0.1", 9), timeout=0.2).close()
            REACHED = "connected"
        except OSError as exc:
            REACHED = type(exc).__name__

        def test_reports_what_happened_at_import():
            assert REACHED != "SocketBlockedError", (
                "pytest-socket now blocks collection-time sockets; the AST rule "
                "that catches them can be relaxed"
            )
        """,
        *BLOCK_FLAGS,
    )
    assert result.returncode == 0, result.stdout
