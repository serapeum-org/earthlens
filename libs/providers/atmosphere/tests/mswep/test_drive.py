"""Unit tests for `earthlens.mswep.drive`."""

from __future__ import annotations

import pytest

from earthlens.mswep.catalog import Catalog, ProvisionalValueError
from earthlens.mswep.drive import (
    NAME_QUERY_CHUNK,
    DriveEntry,
    RootResolver,
    escape_query_value,
    find_children_by_name,
    find_folder,
    list_folders,
)

pytestmark = [pytest.mark.mswep, pytest.mark.unit]


class TestEscaping:
    """Drive `q` string-literal escaping."""

    def test_apostrophe_is_escaped(self):
        """A single quote would otherwise terminate the literal."""
        assert escape_query_value("O'Brien") == "O\\'Brien"

    def test_backslash_is_escaped_first(self):
        """A backslash is doubled before quotes are escaped."""
        assert escape_query_value("a\\b") == "a\\\\b"

    def test_plain_name_is_unchanged(self):
        """A granule name needs no escaping."""
        assert escape_query_value("2020116.18.nc") == "2020116.18.nc"


class TestListFolders:
    """Structural folder listing."""

    def test_lists_only_child_folders(self, share):
        """Listing a folder returns its sub-folders, not its files."""
        names = {entry.name for entry in list_folders(share, "SHARE")}
        assert names == {"MSWEP_V280", "MSWEP_V315", "MSWX_V100", "Gauge_metadata"}

    def test_files_are_excluded(self, share):
        """A folder holding granules lists no children."""
        daily = share.path_id("MSWEP_V315/Past/Daily")
        assert list_folders(share, daily) == []

    def test_entries_report_folder_kind(self, share):
        """Every listed entry is flagged as a folder."""
        assert all(entry.is_folder for entry in list_folders(share, "SHARE"))

    def test_shared_drive_flags_are_sent(self, share):
        """A share inside a Shared Drive resolves only with both flags set."""
        list_folders(share, "SHARE")
        kwargs = share.list_calls[-1]["kwargs"]
        assert kwargs["supportsAllDrives"] is True
        assert kwargs["includeItemsFromAllDrives"] is True

    def test_trashed_items_are_excluded(self, share):
        """The query filters out trashed objects."""
        list_folders(share, "SHARE")
        assert "trashed = false" in share.list_calls[-1]["q"]

    def test_paging_is_followed(self, drive):
        """A multi-page listing returns every page's entries."""
        drive.page_size = 2
        for index in range(5):
            drive.add_folder(f"f{index}", "SHARE")
        assert len(list_folders(drive, "SHARE")) == 5
        assert len(drive.list_calls) == 3


class TestFindFolder:
    """Single named-folder resolution."""

    def test_resolves_a_child(self, share):
        """A present folder resolves to an entry."""
        assert find_folder(share, "SHARE", "MSWEP_V315").name == "MSWEP_V315"

    def test_resolves_a_nested_child(self, share):
        """Resolution walks one level at a time, by parent id."""
        root = share.path_id("MSWEP_V315")
        assert find_folder(share, root, "Past").name == "Past"

    def test_absent_child_is_none(self, share):
        """A missing folder yields `None` rather than raising."""
        assert find_folder(share, "SHARE", "MSWEP_V999") is None

    def test_does_not_match_across_parents(self, share):
        """A folder under a different parent is not returned."""
        mswx = share.path_id("MSWX_V100")
        assert find_folder(share, mswx, "MSWEP_V315") is None


class TestFindChildrenByName:
    """The granule-enumeration primitive."""

    def test_resolves_present_names(self, share):
        """Requested names that exist come back keyed by name."""
        parent = share.path_id("MSWEP_V315/Past/Daily")
        found = find_children_by_name(share, parent, ["2020116.nc", "2020117.nc"])
        assert set(found) == {"2020116.nc", "2020117.nc"}

    def test_missing_names_are_simply_absent(self, share):
        """A missing granule is reported by absence, not an exception."""
        parent = share.path_id("MSWEP_V315/Past/Daily")
        found = find_children_by_name(share, parent, ["2020116.nc", "1999001.nc"])
        assert set(found) == {"2020116.nc"}

    def test_empty_request_makes_no_call(self, share):
        """Asking for nothing costs no API call."""
        before = len(share.list_calls)
        assert find_children_by_name(share, "SHARE", []) == {}
        assert len(share.list_calls) == before

    def test_names_are_chunked_not_paged(self, drive):
        """Many names go out as chunked queries, never a folder enumeration."""
        for index in range(250):
            drive.add_file(f"g{index}.nc", "FOLDER")
        names = [f"g{index}.nc" for index in range(250)]
        found = find_children_by_name(drive, "FOLDER", names)
        assert len(found) == 250
        expected_chunks = -(-250 // NAME_QUERY_CHUNK)
        assert len(drive.list_calls) == expected_chunks

    def test_query_names_the_parent_and_the_files(self, share):
        """The query constrains by parent and by explicit name clauses."""
        find_children_by_name(share, "PARENT", ["a.nc", "b.nc"])
        query = share.list_calls[-1]["q"]
        assert "'PARENT' in parents" in query
        assert "name = 'a.nc' or name = 'b.nc'" in query


class TestRootResolver:
    """The shared `folder_id` is the version root itself (`C8` / `G8`)."""

    def test_returns_the_shared_folder_as_the_root(self, share):
        """`resolve` returns the folder pointed at, read from Drive."""
        root_id = share.path_id("MSWEP_V315")
        resolver = RootResolver(share, root_id, Catalog())
        entry = resolver.resolve("mswep")
        assert entry.id == root_id
        assert entry.name == "MSWEP_V315"

    def test_the_folder_decides_the_version_not_the_arg(self, share):
        """Pointing at the v2.80 folder yields it regardless of the version arg."""
        root_id = share.path_id("MSWEP_V280")
        resolver = RootResolver(share, root_id, Catalog())
        assert resolver.resolve("mswep", "2.80").name == "MSWEP_V280"

    def test_resolves_the_other_product(self, share):
        """MSWX's shared folder resolves to its own root."""
        root_id = share.path_id("MSWX_V100")
        resolver = RootResolver(share, root_id, Catalog())
        assert resolver.resolve("mswx").name == "MSWX_V100"

    def test_unknown_version_lists_the_known_ones(self, share):
        """A version the catalog never heard of names the valid keys."""
        resolver = RootResolver(share, share.path_id("MSWEP_V315"), Catalog())
        with pytest.raises(ValueError, match=r"known mswep version"):
            resolver.resolve("mswep", "9.99")

    def test_unknown_product_raises(self, share):
        """An unknown product key is rejected by the catalog."""
        resolver = RootResolver(share, share.path_id("MSWEP_V315"), Catalog())
        with pytest.raises(ValueError, match="not in the MSWEP catalog"):
            resolver.resolve("nope")

    def test_root_metadata_is_fetched_once(self, share):
        """The folder's metadata is read once per resolver, then cached."""
        resolver = RootResolver(share, share.path_id("MSWEP_V315"), Catalog())
        first = resolver.resolve("mswep")
        again = resolver.resolve("mswep")
        # The root DriveEntry is cached, not re-fetched per call.
        assert first is again is resolver.root()


class TestDriveEntry:
    """The entry value object."""

    def test_non_folder_is_not_a_folder(self):
        """A granule entry is not flagged as a folder."""
        assert not DriveEntry("1", "x.nc", "application/x-netcdf").is_folder

    def test_entry_is_frozen(self):
        """Entries are immutable value objects."""
        entry = DriveEntry("1", "x.nc")
        with pytest.raises(Exception):
            entry.id = "2"
