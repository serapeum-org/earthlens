"""Unit tests for `earthlens.overture.releases`."""

from __future__ import annotations

import pytest

from earthlens.overture.releases import (
    STAC_TIMEOUT,
    ReleaseLookupError,
    child_release_ids,
    is_release_id,
    latest_release,
    stac_catalog,
)


class _FakeClient:
    """Stand-in transport that returns or raises a canned STAC response."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.urls: list[str] = []

    def get_json(self, url: str):
        """Record the URL and return the canned payload, or raise."""
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.mark.overture
class TestIsReleaseId:
    """Shape check for an Overture release id."""

    @pytest.mark.parametrize(
        "value", ["2026-07-22.0", "2026-06-17.1", "2026-07-22.10", "9999-99-99.0"]
    )
    def test_accepts_a_release_shaped_id(self, value):
        """Anything of the form yyyy-mm-dd.<ordinal> is release-shaped."""
        assert is_release_id(value)

    @pytest.mark.parametrize(
        "value",
        ["https:", "2026-7-22.0", "2026-07-22", "", "latest", "2026-07-22.0 "],
    )
    def test_rejects_anything_else(self, value):
        """Fragments, typos, and ordinal-less ids are not release ids."""
        assert not is_release_id(value)

    def test_rejects_a_trailing_newline(self):
        """A trailing newline does not sneak past the anchors."""
        assert not is_release_id("2026-07-22.0\n")

    @pytest.mark.parametrize("value", [None, 20260722, ["2026-07-22.0"]])
    def test_rejects_a_non_string(self, value):
        """A non-string is answered, not raised at."""
        assert not is_release_id(value)


@pytest.mark.overture
class TestStacCatalog:
    """Fetching Overture's STAC root document."""

    def test_returns_the_decoded_document(self):
        """A JSON object comes back as-is."""
        client = _FakeClient({"latest": "2026-07-22.0"})
        assert stac_catalog(client) == {"latest": "2026-07-22.0"}
        assert client.urls == ["https://stac.overturemaps.org/catalog.json"]

    def test_wraps_a_transport_failure(self):
        """A 5xx, timeout, or decode error surfaces as one typed error."""
        client = _FakeClient(error=OSError("connection reset"))
        with pytest.raises(ReleaseLookupError, match=r"connection reset"):
            stac_catalog(client)

    def test_rejects_a_document_that_is_not_an_object(self):
        """A JSON array is not a catalog."""
        client = _FakeClient(["2026-07-22.0"])
        with pytest.raises(ReleaseLookupError, match=r"not a JSON object"):
            stac_catalog(client)

    def test_default_client_is_bounded(self):
        """The default transport carries the module's timeout."""
        assert STAC_TIMEOUT == (5.0, 15.0)


@pytest.mark.overture
class TestLatestRelease:
    """Reading the published release id."""

    def test_returns_the_latest_key(self):
        """The catalog's own `latest` field is the answer."""
        assert latest_release(_FakeClient({"latest": "2026-07-22.0"})) == "2026-07-22.0"

    @pytest.mark.parametrize("latest", [None, "", "https:", "2026-7-22.0"])
    def test_rejects_a_latest_that_is_not_a_release(self, latest):
        """A missing or malformed `latest` is a lookup failure, not a release."""
        client = _FakeClient({"latest": latest})
        with pytest.raises(ReleaseLookupError, match=r"not a release id"):
            latest_release(client)

    def test_propagates_a_catalog_failure(self):
        """An unreadable catalog fails here too."""
        client = _FakeClient(error=OSError("no route"))
        with pytest.raises(ReleaseLookupError):
            latest_release(client)


@pytest.mark.overture
class TestChildReleaseIds:
    """Reading the release ids out of the catalog's child links."""

    def test_reads_the_segment_before_catalog_json(self):
        """The release is the second-to-last path segment of each child href."""
        client = _FakeClient(
            {
                "links": [
                    {"rel": "root", "href": "https://stac.example/catalog.json"},
                    {
                        "rel": "child",
                        "href": "https://stac.example/2026-07-22.0/catalog.json",
                    },
                    {
                        "rel": "child",
                        "href": "https://stac.example/2026-06-17.0/catalog.json",
                    },
                    {"rel": "self", "href": "https://stac.example/catalog.json"},
                ]
            }
        )
        assert child_release_ids(client) == ["2026-07-22.0", "2026-06-17.0"]

    def test_reads_a_relative_href(self):
        """The SDK's documented relative form parses too."""
        client = _FakeClient(
            {"links": [{"rel": "child", "href": "./2026-07-22.0/catalog.json"}]}
        )
        assert child_release_ids(client) == ["2026-07-22.0"]

    def test_skips_an_href_with_no_release_segment(self):
        """A child link too short to carry a release contributes nothing."""
        client = _FakeClient(
            {
                "links": [
                    {"rel": "child", "href": "https://stac.example/catalog.json"},
                    {"rel": "child", "href": ""},
                ]
            }
        )
        assert child_release_ids(client) == []

    @pytest.mark.parametrize("links", [None, "not-a-list", []])
    def test_tolerates_a_catalog_without_usable_links(self, links):
        """A missing or malformed `links` block yields no ids rather than raising."""
        assert child_release_ids(_FakeClient({"links": links})) == []

    def test_skips_a_link_that_is_not_an_object(self):
        """A non-object entry in `links` is ignored."""
        client = _FakeClient({"links": ["https://stac.example/2026-07-22.0/"]})
        assert child_release_ids(client) == []
