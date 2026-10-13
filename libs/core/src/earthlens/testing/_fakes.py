"""One `requests.Response` double, instead of one per backend.

The suite had grown 51 module-level response doubles across the five providers,
most of them the same six methods over a fixed payload — `land/wdpa/conftest.py`
and `land/iucn/conftest.py` were the same class with one extra attribute. Each
copy also drifted: some `raise_for_status` unconditionally (so a test asserting
a 200 path cannot call it), some omit `iter_content` (so a streaming backend
cannot be faked with them at all), some return `{}` for a missing JSON body
where the real `requests` raises.

This is the superset, with the real library's behaviour where the copies
disagreed. It pairs with :func:`fake_http`, which routes URLs to responses on
the `requests` module seam the autouse transport fixture guarantees.

Import from `earthlens.testing`, not from here.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from typing import Any

import pytest


class FakeResponse:
    """A stand-in for `requests.Response` over a fixed payload.

    Keyword-only on purpose: the doubles it replaces disagreed on what the
    first positional argument meant (a JSON payload in some, the body text in
    others), so a positional call would have silently changed meaning during
    the migration.

    Args:
        status_code: The HTTP status.
        json: The decoded JSON body. `None` means "no JSON body", and
            :meth:`json` then raises the way `requests` does.
        text: The body as text. Defaults to the decoded `content`, or to the
            serialised `json` payload, or `""`.
        content: The body as bytes. Defaults to the encoded `text`.
        headers: Response headers.
        url: The URL the response came from.
        reason: The HTTP reason phrase.
        chunks: What :meth:`iter_content` should yield, for a streaming body
            whose chunk boundaries matter. Defaults to slicing `content`.

    Examples:
        - A JSON body behaves like the real thing:
            ```python
            >>> from earthlens.testing import FakeResponse
            >>> response = FakeResponse(json={"value": 1})
            >>> response.status_code, response.json()
            (200, {'value': 1})

            ```
        - A missing JSON body raises, rather than returning `{}`:
            ```python
            >>> from earthlens.testing import FakeResponse
            >>> FakeResponse(text="not json").json()
            Traceback (most recent call last):
            ValueError: no JSON body

            ```
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        json: Any = None,
        text: str | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        url: str = "https://example.invalid/",
        reason: str = "",
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = json
        self.headers = dict(headers or {})
        self.url = url
        self.reason = reason
        self._chunks = chunks
        if content is None and text is None and json is not None:
            text = _json.dumps(json)
        if content is None:
            content = (text or "").encode("utf-8")
        if text is None:
            text = content.decode("utf-8", errors="replace")
        self.content = content
        self.text = text
        self.closed = False

    @property
    def ok(self) -> bool:
        """Whether the status is below 400, as `requests.Response.ok` is."""
        return self.status_code < 400

    def json(self, **_kwargs: Any) -> Any:
        """Return the decoded JSON body.

        Returns:
            The payload handed to the constructor.

        Raises:
            ValueError: When there is no JSON body — what `requests` does, and
                what several of the replaced doubles did not.
        """
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        """Raise `requests.HTTPError` for a 4xx/5xx, and nothing for a 2xx.

        Raises:
            HTTPError: When the status is 400 or above.
        """
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(
                f"{self.status_code} Error: {self.reason or 'error'} for url: "
                f"{self.url}",
                response=self,  # type: ignore[arg-type]
            )

    def iter_content(self, chunk_size: int = 1, **_kwargs: Any) -> Iterator[bytes]:
        """Yield the body in chunks, as a streaming download reads it.

        Args:
            chunk_size: Bytes per chunk when the chunks were not pinned.

        Yields:
            Successive slices of `content`, or the pinned `chunks`.
        """
        if self._chunks is not None:
            yield from self._chunks
            return
        for start in range(0, len(self.content), max(chunk_size, 1)):
            yield self.content[start : start + max(chunk_size, 1)]

    def close(self) -> None:
        """Release the response, so a retry path can close what it opened."""
        self.closed = True

    def __enter__(self) -> FakeResponse:
        """Support `with requests.get(...) as response:`."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close on the way out, as `requests.Response` does."""
        self.close()


class FakeHttp:
    """A URL-to-response routing table installed on the `requests` seam.

    The autouse `unpooled_http_transport` fixture already points `HttpClient`
    at the `requests` module, so patching `requests.get` / `.head` / `.post`
    here reaches both the module-level callers and the pooled client.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install the router over `requests.get` / `.head` / `.post`.

        Args:
            monkeypatch: pytest's patcher, so the seam is undone per test.
        """
        import requests

        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._routes: list[tuple[str, Any]] = []
        self.default: Any = None
        for method in ("get", "head", "post"):
            monkeypatch.setattr(requests, method, self._handler(method))

    def route(self, fragment: str, response: Any) -> FakeHttp:
        """Answer any URL containing `fragment` with `response`.

        Args:
            fragment: A substring matched against the requested URL. Routes
                are tried in registration order, so register the specific
                ones first.
            response: A :class:`FakeResponse`, an exception instance to raise,
                or a callable taking `(url, **kwargs)`.

        Returns:
            This router, so routes can be chained.
        """
        self._routes.append((fragment, response))
        return self

    def always(self, response: Any) -> FakeHttp:
        """Answer every otherwise-unrouted URL with `response`.

        Args:
            response: As :meth:`route`.

        Returns:
            This router, so it can be chained.
        """
        self.default = response
        return self

    def _handler(self, method: str):
        """Build the replacement for one `requests` function."""

        def _call(url: str, **kwargs: Any) -> Any:
            self.calls.append((method, url, kwargs))
            for fragment, response in self._routes:
                if fragment in url:
                    return _resolve(response, url, **kwargs)
            if self.default is not None:
                return _resolve(self.default, url, **kwargs)
            raise AssertionError(
                f"unrouted {method.upper()} {url} — add a `fake_http.route(...)` "
                "for it, or `fake_http.always(...)` for a catch-all"
            )

        return _call


def _resolve(response: Any, url: str, **kwargs: Any) -> Any:
    """Turn a route's value into a response, raising it when it is an error."""
    if isinstance(response, BaseException):
        raise response
    if callable(response) and not isinstance(response, FakeResponse):
        return response(url, **kwargs)
    return response


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    """Give a test a URL-routed `requests` double instead of a bespoke class.

    Args:
        monkeypatch: pytest's patcher, so the seam is undone per test.

    Returns:
        The router, to hang routes off.

    Examples:
        ```python
        def test_backend_reads_the_payload(fake_http):
            fake_http.route("/api/v3/", FakeResponse(json={"results": []}))
            ...
        ```
    """
    return FakeHttp(monkeypatch)
