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
    """Version to root-folder resolution (`C8` / `G8`)."""

    def test_resolves_the_default_version(self, share):
        """With no version named, the catalog default is used."""
        resolver = RootResolver(share, "SHARE", Catalog())
        assert resolver.resolve("mswep").name == "MSWEP_V315"

    def test_resolves_an_explicit_version(self, share):
        """A named version picks its own coexisting root."""
        resolver = RootResolver(share, "SHARE", Catalog())
        assert resolver.resolve("mswep", "2.80").name == "MSWEP_V280"

    def test_resolves_the_other_product(self, share):
        """MSWX resolves under its own root."""
        resolver = RootResolver(share, "SHARE", Catalog())
        assert resolver.resolve("mswx").name == "MSWX_V100"

    def test_unknown_version_lists_the_known_ones(self, share):
        """A version the catalog never heard of names the valid keys."""
        resolver = RootResolver(share, "SHARE", Catalog())
        with pytest.raises(ValueError, match=r"known mswep version"):
            resolver.resolve("mswep", "9.99")

    def test_provisional_root_is_refused(self, share):
        """The unverified V3.16 root name will not resolve silently."""
        resolver = RootResolver(share, "SHARE", Catalog())
        with pytest.raises(ProvisionalValueError, match="provisional"):
            resolver.resolve("mswep", "3.16")

    def test_absent_root_lists_what_is_present(self, drive):
        """A catalog root missing from the share names the roots that exist."""
        drive.add_folder("MSWEP_V999", "SHARE")
        resolver = RootResolver(drive, "SHARE", Catalog())
        with pytest.raises(ValueError, match="MSWEP_V999"):
            resolver.resolve("mswep")

    def test_absent_root_message_explains_renaming(self, drive):
        """The error says GloH2O renames the folder between releases."""
        drive.add_folder("MSWEP_V999", "SHARE")
        resolver = RootResolver(drive, "SHARE", Catalog())
        with pytest.raises(ValueError, match="stamps the version"):
            resolver.resolve("mswep")

    def test_empty_share_reports_none_present(self, drive):
        """An empty share still produces an actionable message."""
        resolver = RootResolver(drive, "SHARE", Catalog())
        with pytest.raises(ValueError, match="<none>"):
            resolver.resolve("mswep")

    def test_share_roots_are_cached(self, share):
        """The root listing is fetched once per resolver, not per request."""
        resolver = RootResolver(share, "SHARE", Catalog())
        resolver.resolve("mswep")
        after_first = len(share.list_calls)
        resolver.resolve("mswep", "2.80")
        resolver.resolve("mswx")
        assert len(share.list_calls) == after_first

    def test_unknown_product_raises(self, share):
        """An unknown product key is rejected by the catalog."""
        resolver = RootResolver(share, "SHARE", Catalog())
        with pytest.raises(ValueError, match="not in the MSWEP catalog"):
            resolver.resolve("nope")


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
