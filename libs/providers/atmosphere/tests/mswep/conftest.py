"""Shared fixtures for the MSWEP / MSWX tests: a fake Drive v3 service.

The fake stands in for `googleapiclient`'s discovery client so the whole
suite runs with no network, no credentials and no Google SDK installed.
It models Drive the way Drive actually behaves — a flat store of objects
each carrying a parent id, addressed only by id, never by path — and
implements just the slice the backend uses: `files().list()` with a `q`
expression, and `files().get_media()`.
"""

from __future__ import annotations

import re

import pytest

from earthlens.mswep.drive import FOLDER_MIME

#: Matches the `'<id>' in parents` clause the backend always includes.
_PARENT_RE = re.compile(r"'([^']+)' in parents")

#: Matches every `name = '<value>'` clause in a query.
_NAME_RE = re.compile(r"name = '((?:[^'\\]|\\.)*)'")


def _unescape(value: str) -> str:
    """Reverse `escape_query_value` for the fake's own matching."""
    return value.replace("\\'", "'").replace("\\\\", "\\")


class FakeRequest:
    """A prepared call whose `execute()` returns a canned payload."""

    def __init__(self, payload):
        """Store the payload this request will return."""
        self._payload = payload

    def execute(self):
        """Return the canned payload."""
        return self._payload


class FakeResponse(dict):
    """An httplib2-shaped response: a header dict carrying a `.status`."""

    def __init__(self, status, headers=None):
        """Build a response with `status` and optional headers."""
        super().__init__(headers or {})
        self.status = status


class FakeHttp:
    """The httplib2-like transport `MediaIoBaseDownload` drives."""

    def __init__(self, data, error=None):
        """Serve `data` in range slices, or raise `error` on first call."""
        self._data = data
        self._error = error

    def request(self, uri, method="GET", headers=None, **kwargs):
        """Answer one ranged GET, honouring the `range` header."""
        if self._error is not None:
            raise self._error
        span = (headers or {}).get("range", "")
        start, end = 0, len(self._data) - 1
        if span.startswith("bytes="):
            raw_start, _, raw_end = span[len("bytes=") :].partition("-")
            start = int(raw_start)
            end = min(int(raw_end), len(self._data) - 1)
        chunk = self._data[start : end + 1]
        return (
            FakeResponse(
                206 if start else 200,
                {"content-range": f"bytes {start}-{end}/{len(self._data)}"},
            ),
            chunk,
        )


class FakeMediaRequest:
    """A `get_media` request with the attributes the downloader reads."""

    def __init__(self, data, error=None):
        """Bind the payload (and optional transport error) to serve."""
        self.uri = "https://fake.drive/get_media"
        self.headers = {}
        self.http = FakeHttp(data, error=error)


class FakeFiles:
    """The `files()` collection of the fake Drive service."""

    def __init__(self, store):
        """Bind to the owning store."""
        self._store = store

    def list(self, *, q, fields=None, pageSize=None, pageToken=None, **kwargs):  # noqa: N803
        """Answer a `files.list` query against the in-memory store.

        Records the call on the store so tests can assert on paging and
        on the `supportsAllDrives` flags, then filters the store by the
        parent, MIME-type and name clauses the backend emits.
        """
        self._store.list_calls.append({"q": q, "kwargs": kwargs})

        parent_match = _PARENT_RE.search(q)
        parent = parent_match.group(1) if parent_match else None
        wants_folder = FOLDER_MIME in q
        names = {_unescape(m) for m in _NAME_RE.findall(q)}

        matched = []
        for obj in self._store.objects:
            if parent is not None and obj["parent"] != parent:
                continue
            if wants_folder and obj["mimeType"] != FOLDER_MIME:
                continue
            if names and obj["name"] not in names:
                continue
            matched.append(
                {"id": obj["id"], "name": obj["name"], "mimeType": obj["mimeType"]}
            )

        page = self._store.page_size
        start = int(pageToken) if pageToken else 0
        chunk = matched[start : start + page]
        payload = {"files": chunk}
        if start + page < len(matched):
            payload["nextPageToken"] = str(start + page)
        return FakeRequest(payload)

    def get_media(self, *, fileId, **kwargs):  # noqa: N803
        """Return a media request the real `MediaIoBaseDownload` can drive.

        Deliberately not a canned payload: production code uses
        googleapiclient's own chunked downloader, so the fake models the
        httplib2-shaped transport underneath it instead. That keeps the
        real chunk loop, range headers and completion logic under test.
        """
        self._store.media_calls.append(fileId)
        error = self._store.media_errors.get(fileId)
        return FakeMediaRequest(self._store.contents.get(fileId, b""), error=error)


class FakeDrive:
    """An in-memory Drive v3 stand-in.

    Holds a flat list of objects, each with an `id`, `name`, `mimeType`
    and `parent` — exactly Drive's own model, where a "path" exists only
    as a chain of parent ids.

    Attributes:
        objects: Every stored object.
        contents: File id to its bytes.
        list_calls: Every `files.list` call, for paging assertions.
        media_calls: Every downloaded file id.
        page_size: Page size the fake enforces, so paging is testable
            without creating a thousand objects.
    """

    def __init__(self, page_size=1000):
        """Create an empty store with the given page size."""
        self.objects = []
        self.contents = {}
        self.list_calls = []
        self.media_calls = []
        self.media_errors = {}
        self.page_size = page_size
        self._next = 0

    def files(self):
        """Return the `files()` collection."""
        return FakeFiles(self)

    def _add(self, name, parent, mime, data=None):
        """Insert an object and return its generated id."""
        self._next += 1
        obj_id = f"id{self._next}"
        self.objects.append(
            {"id": obj_id, "name": name, "mimeType": mime, "parent": parent}
        )
        if data is not None:
            self.contents[obj_id] = data
        return obj_id

    def add_folder(self, name, parent):
        """Add a folder under `parent` and return its id."""
        return self._add(name, parent, FOLDER_MIME)

    def add_file(self, name, parent, data=b"CDF fake"):
        """Add a file under `parent` and return its id."""
        return self._add(name, parent, "application/x-netcdf", data)

    def path_id(self, path, root="SHARE"):
        """Walk a `/`-joined folder path from `root` and return the leaf id.

        Drive itself has no paths; this exists only so tests can name a
        location readably instead of threading generated ids around.
        """
        current = root
        for segment in path.split("/"):
            matches = [
                obj
                for obj in self.objects
                if obj["parent"] == current and obj["name"] == segment
            ]
            if not matches:
                raise KeyError(f"{segment!r} not found under {current!r}")
            current = matches[0]["id"]
        return current

    def add_tree(self, parent, spec):
        """Create a nested folder tree from a dict, returning the id map.

        A dict value is a sub-folder; a list value is a set of file names
        inside that folder.
        """
        ids = {}
        for name, children in spec.items():
            folder_id = self.add_folder(name, parent)
            ids[name] = folder_id
            if isinstance(children, dict):
                ids.update(self.add_tree(folder_id, children))
            elif isinstance(children, list):
                for filename in children:
                    self.add_file(filename, folder_id)
        return ids


@pytest.fixture
def loguru_messages():
    """Collect WARNING+ loguru messages into a list (loguru bypasses caplog)."""
    from loguru import logger

    messages = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def drive():
    """An empty fake Drive service."""
    return FakeDrive()


@pytest.fixture
def share(drive):
    """A fake share holding an MSWEP and an MSWX root, with a few granules."""
    drive.add_tree(
        "SHARE",
        {
            "MSWEP_V280": {"Past": {"Daily": []}},
            "MSWEP_V315": {
                "Past": {
                    "Daily": ["2020116.nc", "2020117.nc"],
                    "Hourly": ["2020116.18.nc"],
                    "Monthly": ["202004.nc"],
                },
                "NRT": {"Daily": ["2025001.nc"]},
                "Gauge_metadata": [
                    "daily_station_locations.csv",
                    "monthly_station_locations.csv",
                    "daily_station_date_ranges.csv",
                    "monthly_station_date_ranges.csv",
                    "daily_station_reporting_times.csv",
                ],
            },
            "MSWX_V100": {"Past": {"Temp": {"Daily": ["2007133.nc"]}}},
            "Gauge_metadata": {},
        },
    )
    return drive
