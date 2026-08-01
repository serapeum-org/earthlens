"""Google Drive v3 primitives for the MSWEP / MSWX backend.

Drive has no paths — only parent/child ids — so every lookup here walks
the tree by name from the shared-folder id GloH2O's approval email
carries. Two rules shape the module:

* **Always pass `supportsAllDrives` / `includeItemsFromAllDrives`.**
  GloH2O may place the share in a Shared Drive rather than a personal
  one, and the listing silently returns nothing without them.
* **Never enumerate a granule folder.** `Past/Hourly/` holds roughly
  400,000 files (46 years x 8760), so `files.list` paging to find 24 of
  them is absurd. :func:`find_children_by_name` resolves **by name** in
  chunked `or`-joined queries instead; folder listing is reserved for
  the handful of structural levels (the share root, the variant and
  temporal folders) where cardinality is tiny.

:class:`RootResolver` implements the version → root-folder lookup. Roots
are version-stamped and coexist in the share (`MSWEP_V280` beside
`MSWEP_V315`), and GloH2O's own examples disagree with the documented
version, so the resolver **confirms the folder exists** rather than
trusting the catalog constant — and, when it does not, raises listing
the roots that are actually present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from earthlens.mswep.catalog import Catalog

#: Drive's MIME type for a folder — the `q` filter that keeps a listing
#: to sub-directories.
FOLDER_MIME = "application/vnd.google-apps.folder"

#: Page size for `files.list`. Drive caps this at 1000.
PAGE_SIZE = 1000

#: How many file names to pack into one `or`-joined `files.list` query.
#: Drive rejects an over-long `q`, and 100 names keeps the request well
#: inside the limit while cutting a 24-granule day to a single call.
NAME_QUERY_CHUNK = 100


@dataclass(frozen=True)
class DriveEntry:
    """One Drive object: its id, name and MIME type.

    Attributes:
        id: The Drive file id, used for every subsequent call.
        name: The object's name within its parent folder.
        mime_type: Drive MIME type; equals :data:`FOLDER_MIME` for a
            folder.

    Examples:
        - A folder entry knows it is one:
            ```python
            >>> from earthlens.mswep.drive import DriveEntry, FOLDER_MIME
            >>> DriveEntry(id="1a", name="Past", mime_type=FOLDER_MIME).is_folder
            True

            ```
    """

    id: str
    name: str
    mime_type: str = ""

    @property
    def is_folder(self) -> bool:
        """Return whether this entry is a Drive folder."""
        return self.mime_type == FOLDER_MIME


def escape_query_value(value: str) -> str:
    """Escape a string for use inside a Drive `q` string literal.

    Drive's query grammar delimits literals with single quotes, so a
    backslash or apostrophe in a file name must be escaped or the query
    is malformed. Granule names never contain either, but folder names
    come from the share and are not ours to trust.

    Args:
        value: The raw value to embed.

    Returns:
        str: The escaped value, without surrounding quotes.

    Examples:
        - An apostrophe is escaped:
            ```python
            >>> from earthlens.mswep.drive import escape_query_value
            >>> escape_query_value("O'Brien")
            "O\\\\'Brien"

            ```
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _execute_list(service: Any, query: str, page_token: str | None) -> dict[str, Any]:
    """Run one `files.list` page against Drive.

    Args:
        service: The Drive v3 client.
        query: The assembled `q` expression.
        page_token: Continuation token, or `None` for the first page.

    Returns:
        dict[str, Any]: The raw Drive response.
    """
    return dict(
        service.files()
        .list(
            q=query,
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=PAGE_SIZE,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )


def _paged_entries(service: Any, query: str) -> list[DriveEntry]:
    """Collect every page of a `files.list` query into `DriveEntry` rows.

    Args:
        service: The Drive v3 client.
        query: The assembled `q` expression.

    Returns:
        list[DriveEntry]: Every matching entry, across all pages.
    """
    entries: list[DriveEntry] = []
    page_token: str | None = None
    while True:
        response = _execute_list(service, query, page_token)
        for row in response.get("files", []):
            entries.append(
                DriveEntry(
                    id=row["id"],
                    name=row.get("name", ""),
                    mime_type=row.get("mimeType", ""),
                )
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            return entries


def list_folders(service: Any, parent_id: str) -> list[DriveEntry]:
    """List the sub-folders of one Drive folder.

    Restricted to folders on purpose: every caller here walks structural
    levels (the share root, variant, variable, temporal), and a granule
    folder holds hundreds of thousands of files that must never be
    paged.

    Args:
        service: The Drive v3 client.
        parent_id: Drive id of the parent folder.

    Returns:
        list[DriveEntry]: The child folders, in Drive's own order.
    """
    query = (
        f"'{escape_query_value(parent_id)}' in parents "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    return _paged_entries(service, query)


def find_folder(service: Any, parent_id: str, name: str) -> DriveEntry | None:
    """Resolve one named sub-folder of a Drive folder.

    Args:
        service: The Drive v3 client.
        parent_id: Drive id of the parent folder.
        name: Exact child folder name.

    Returns:
        DriveEntry | None: The folder, or `None` when absent.
    """
    query = (
        f"'{escape_query_value(parent_id)}' in parents "
        f"and name = '{escape_query_value(name)}' "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    entries = _paged_entries(service, query)
    return entries[0] if entries else None


def find_children_by_name(
    service: Any, parent_id: str, names: list[str]
) -> dict[str, DriveEntry]:
    """Resolve many named children of one folder in chunked queries.

    The granule-enumeration primitive. Rather than page a folder holding
    ~400,000 files, this asks Drive for exactly the names wanted,
    `or`-joining up to :data:`NAME_QUERY_CHUNK` of them per request — so
    a 24-granule day costs one call, not 400 pages.

    Args:
        service: The Drive v3 client.
        parent_id: Drive id of the folder holding the granules.
        names: Exact file names to resolve.

    Returns:
        dict[str, DriveEntry]: Name to entry, containing only the names
            that exist. A caller detects a missing granule by its
            absence from this mapping.
    """
    found: dict[str, DriveEntry] = {}
    parent = escape_query_value(parent_id)
    for start in range(0, len(names), NAME_QUERY_CHUNK):
        chunk = names[start : start + NAME_QUERY_CHUNK]
        clause = " or ".join(f"name = '{escape_query_value(n)}'" for n in chunk)
        query = f"'{parent}' in parents and ({clause}) and trashed = false"
        for entry in _paged_entries(service, query):
            found[entry.name] = entry
    return found


class RootResolver:
    """Resolve a product + version onto its Drive root folder.

    Roots are version-stamped and several coexist in one share
    (`MSWEP_V280` beside `MSWEP_V315`), and GloH2O's published examples
    disagree with the documented version — the V3.16 documentation's own
    worked example still writes `MSWEP_V315`, and the website's rclone
    example writes `MSWEP_V315_test`. So the catalog constant is treated
    as a hypothesis to confirm against the share, never as truth.

    The share-root listing is cached for the resolver's lifetime: it is
    a handful of folders and every granule request needs it.

    Attributes:
        _service: The Drive v3 client.
        _folder_id: Drive id of the shared folder GloH2O granted.
        _catalog: The MSWEP catalog supplying the version → root map.
        _roots: Cached share-root listing, or `None` before first use.

    Examples:
        - Resolution is by folder name within the share:
            ```python
            >>> from earthlens.mswep.drive import DriveEntry, FOLDER_MIME
            >>> DriveEntry("1a", "MSWEP_V315", FOLDER_MIME).name
            'MSWEP_V315'

            ```
    """

    def __init__(self, service: Any, folder_id: str, catalog: Catalog) -> None:
        """Bind the resolver to a Drive client, share and catalog.

        Args:
            service: The Drive v3 client.
            folder_id: Drive id of the shared folder.
            catalog: The MSWEP catalog to read version → root from.
        """
        self._service = service
        self._folder_id = folder_id
        self._catalog = catalog
        self._roots: dict[str, DriveEntry] | None = None

    def share_roots(self) -> dict[str, DriveEntry]:
        """Return the share's top-level folders, keyed by name (cached).

        Returns:
            dict[str, DriveEntry]: Folder name to entry.
        """
        if self._roots is None:
            self._roots = {
                entry.name: entry
                for entry in list_folders(self._service, self._folder_id)
            }
        return self._roots

    def resolve(self, product_key: str, version: str | None = None) -> DriveEntry:
        """Resolve a product + version onto its root folder in the share.

        Args:
            product_key: Product key (`"mswep"`, `"mswx"`).
            version: Version key; defaults to the product's
                `default_version`.

        Returns:
            DriveEntry: The root folder.

        Raises:
            ValueError: When the version is unknown to the catalog, or
                the expected root folder is absent from the share — in
                which case the message lists the roots actually present.
            ProvisionalValueError: When the version's root name is an
                unverified placeholder.
        """
        product = self._catalog.get_product(product_key)
        key = version or product.default_version
        try:
            row = product.versions[key]
        except KeyError:
            raise ValueError(
                f"{key!r} is not a known {product_key} version. Known "
                f"versions: {sorted(product.versions)}."
            ) from None

        self._catalog.check_not_provisional(
            row, f"the {product_key} v{key} root folder name ({row.root!r})"
        )

        roots = self.share_roots()
        if row.root in roots:
            return roots[row.root]

        raise ValueError(
            f"the {product_key} v{key} root folder {row.root!r} is not in the "
            f"shared folder. Roots present: {sorted(roots) or '<none>'}. "
            "GloH2O stamps the version into the folder name and renames it "
            "between releases, so pick a version whose root is listed above, "
            "or correct the version -> root map in mswep_data_catalog.yaml."
        )
