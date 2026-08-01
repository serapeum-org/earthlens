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
        """Return a request carrying the stored bytes for `fileId`."""
        self._store.media_calls.append(fileId)
        return FakeRequest(self._store.contents.get(fileId, b""))


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
            },
            "MSWX_V100": {"Past": {"Temp": {"Daily": ["2007133.nc"]}}},
            "Gauge_metadata": {},
        },
    )
    return drive
