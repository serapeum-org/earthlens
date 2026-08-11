"""Lazy factory + structural type for the ISIMIP cutout client.

The ISIMIP backend talks to two ISIMIP services — the repository REST API (facet
search) and the files API v2 (the async server-side cutout job). Both are
wrapped by the `isimip-client` SDK's `ISIMIPClient`, which this module builds
**lazily** so `isimip-client` stays a per-backend extra (`pip install
earthlens[isimip]`) that is never imported until an ISIMIP download actually
runs. :class:`IsimipClient` is the structural type the backend depends on, so a
test can inject a fake with the same three methods instead of the real SDK.
"""

from __future__ import annotations

from typing import Any, Protocol


class IsimipClient(Protocol):
    """The subset of `isimip_client.client.ISIMIPClient` the backend uses.

    A structural type (`Protocol`) so the backend accepts either the real SDK
    client or an injected fake with the same three methods.
    """

    def datasets(self, **facets: Any) -> list[dict[str, Any]]:
        """Return the datasets matching the given facet keyword arguments."""

    def cutout_bbox(
        self,
        paths: list[str],
        west: float,
        east: float,
        south: float,
        north: float,
        poll: float | None = None,
    ) -> dict[str, Any]:
        """Submit an async bbox cutout job and (with `poll`) block until done."""

    def download(self, url: str, path: str | None = None, extract: bool = False) -> Any:
        """Download `url` into `path`, unzipping the result when `extract`."""


def build_client(data_url: str, files_api_url: str) -> IsimipClient:
    """Build the real `isimip-client` client, importing the SDK lazily.

    Args:
        data_url: The ISIMIP repository REST API base (facet search).
        files_api_url: The ISIMIP files API v2 base (the cutout job endpoint).

    Returns:
        IsimipClient: A configured `isimip_client.client.ISIMIPClient`.

    Raises:
        ModuleNotFoundError: If `isimip-client` is not installed. The message
            names the `[isimip]` extra that provides it.
    """
    try:
        from isimip_client.client import ISIMIPClient
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via a stub
        raise ModuleNotFoundError(
            "The isimip backend needs the `isimip-client` SDK. Install the extra: "
            "`pip install earthlens[isimip]` (or `earthlens-atmosphere[isimip]`)."
        ) from exc
    client: IsimipClient = ISIMIPClient(data_url=data_url, files_api_url=files_api_url)
    return client
