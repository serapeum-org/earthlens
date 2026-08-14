"""Diagnose Earthdata Login (EDL) reachability from a CI runner.

All three EDL-backed e2e lanes — `earthdata`, `asf`, and `emdat`'s GDIS half —
authenticate against the single host `urs.earthdata.nasa.gov`, and all three
fail intermittently on GitHub runners with `[Errno 101] Network is unreachable`
(`ENETUNREACH`). The host publishes both an A and a AAAA record, but GitHub
runners have no IPv6 egress: a resolved AAAA connects into a dead route while
the A record is reachable. See issue #926.

This probe is a non-gating diagnostic. It never fails the job (it always exits
`0`); it only prints evidence so the next failing run can confirm or kill the
IPv6 hypothesis, and — by dialling the same host once with the default dual
stack and once with IPv6 forced off — proves in-place whether forcing IPv4
would fix the lane before that change is shipped to production.

Run it standalone::

    python tools/edl/reachability_probe.py

It reads no credentials and writes nothing (no `~/.netrc`, no token cache), so
it is safe to run before the real e2e step.
"""

from __future__ import annotations

import socket
import sys

import requests
import urllib3.util.connection

#: The single Earthdata Login identity host every EDL lane authenticates against.
EDL_HOST = "urs.earthdata.nasa.gov"

#: Seconds to wait on each connect before giving up. A dead IPv6 route surfaces
#: `ENETUNREACH` immediately, but a black-holed A record would otherwise hang.
CONNECT_TIMEOUT = 30


def _resolve(host: str) -> list[tuple[int, str]]:
    """Resolve `host` to its (address-family, ip) pairs for TCP port 443.

    Args:
        host: The hostname to resolve.

    Returns:
        One `(family, ip)` pair per record `getaddrinfo` returns, in the order
        the resolver offered them — the order `socket.create_connection` would
        try. Empty when resolution fails.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        print(f"getaddrinfo({host}) failed: {exc}")
        return []
    return [(family, sockaddr[0]) for family, _, _, _, sockaddr in infos]


def _family_name(family: int) -> str:
    """Return `'IPv6'` / `'IPv4'` / `'AF_?'` for a socket address family."""
    if family == socket.AF_INET6:
        return "IPv6"
    if family == socket.AF_INET:
        return "IPv4"
    return f"AF_{family}"


def _dial(family: int, ip: str) -> None:
    """Open a TCP connection to `ip`:443 in `family` and print the outcome.

    The connect result is the whole point of the probe: an `ENETUNREACH`
    (`errno` 101) on the IPv6 address while the IPv4 address connects is the
    signature of the no-IPv6-egress fault.

    Args:
        family: The socket address family (`AF_INET` / `AF_INET6`).
        ip: The literal address to dial.
    """
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect((ip, 443))
    except OSError as exc:
        print(f"  {_family_name(family)} {ip}: FAIL errno={exc.errno} {exc}")
    else:
        print(f"  {_family_name(family)} {ip}: OK")
    finally:
        sock.close()


def _http_get(host: str, label: str) -> None:
    """GET `https://host/` through requests and print connect/HTTP outcome.

    This exercises the exact urllib3 stack `earthaccess.login` uses, so a
    failure here reproduces the lane's failure, and a success proves the
    transport is usable.

    Args:
        host: The host to GET (scheme is added).
        label: A short tag identifying the stack configuration in the log
            (e.g. `'default stack'` or `'IPv4 forced'`).
    """
    try:
        resp = requests.get(f"https://{host}/", timeout=CONNECT_TIMEOUT)
    except requests.RequestException as exc:
        print(f"  https GET ({label}): FAIL {type(exc).__name__}: {exc}")
    else:
        print(f"  https GET ({label}): HTTP {resp.status_code}")


def main() -> int:
    """Print the EDL reachability report and always succeed.

    Returns:
        Always `0` — the probe is diagnostic and must never fail the job.
    """
    print(f"== Earthdata Login reachability probe: {EDL_HOST} ==")

    pairs = _resolve(EDL_HOST)
    print(f"resolved {len(pairs)} record(s):")
    for family, ip in pairs:
        print(f"  {_family_name(family)} {ip}")

    print("raw per-family connect:")
    for family, ip in pairs:
        _dial(family, ip)

    # Dial the host through requests once as the runner is configured, then
    # again with IPv6 forced off. urllib3 asks getaddrinfo for AF_UNSPEC only
    # while HAS_IPV6 is true; setting it false restricts resolution to A
    # records, which is exactly what earthlens.base.prefer_ipv4() does in
    # production. The flip is duplicated inline here on purpose: the probe is a
    # standalone diagnostic that must run even if the earthlens package import
    # is broken, so it deliberately does not import prefer_ipv4().
    print("requests GET, default vs IPv4-forced:")
    _http_get(EDL_HOST, "default stack")
    urllib3.util.connection.HAS_IPV6 = False
    _http_get(EDL_HOST, "IPv4 forced")

    print("== probe complete (non-gating) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
