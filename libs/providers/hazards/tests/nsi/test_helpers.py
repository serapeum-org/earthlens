"""Unit tests for the NSI OpenFEMA query/parse helpers."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.nsi._helpers import (
    odata_filter,
    paginate_arcgis,
    paginate_nfip,
    records_to_frame,
)

from .conftest import _FakeSession, make_client, make_nfip_records

pytestmark = pytest.mark.nsi

ENDPOINT = "https://www.fema.gov/api/open/v3/NfipClaims"
FIELD_MAP = {
    "claim_id": "id",
    "paid": "amountPaidOnBuildingClaim",
    "zone": "floodZoneCurrent",
}


@pytest.mark.unit
class TestOdataFilter:
    """`odata_filter` clause building."""

    def test_none_when_no_selector(self) -> None:
        """No selector yields no filter."""
        assert odata_filter() is None

    def test_string_and_numeric_clauses(self) -> None:
        """Strings are quoted; the year is bare; clauses join with `and`."""
        assert (
            odata_filter(state="LA", county="22071", year=2005)
            == "state eq 'LA' and countyCode eq '22071' and yearOfLoss eq 2005"
        )

    def test_flood_event_quote_escaped(self) -> None:
        """An embedded single quote is doubled per OData."""
        assert odata_filter(flood_event="O'Brien") == "floodEvent eq 'O''Brien'"


@pytest.mark.unit
class TestPagination:
    """`paginate_nfip` paging and capping."""

    def test_collects_all_pages_with_total(self) -> None:
        """Paging walks `$skip` until a short page ends it; total from page 1."""
        session = _FakeSession(nfip_records=make_nfip_records(25), nfip_total=25)
        rows, total = paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str="x eq 1",
            page_size=10,
        )
        assert len(rows) == 25
        assert total == 25
        skips = [c["params"]["$skip"] for c in session.calls]
        assert skips == [0, 10, 20]
        # Only the first request carries the count probe.
        assert session.calls[0]["params"]["$inlinecount"] == "allpages"
        assert "$inlinecount" not in session.calls[1]["params"]

    def test_max_records_caps_total_but_reports_full_count(self) -> None:
        """`max_records` caps the fetch and last `$top`, total stays the real count."""
        session = _FakeSession(nfip_records=make_nfip_records(100), nfip_total=100)
        rows, total = paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str=None,
            page_size=10,
            max_records=15,
        )
        assert len(rows) == 15
        assert total == 100
        assert session.calls[-1]["params"]["$top"] == 5

    def test_max_records_on_exact_page_boundary(self) -> None:
        """`max_records` on an exact page boundary stops without an extra page."""
        session = _FakeSession(nfip_records=make_nfip_records(50), nfip_total=50)
        rows, total = paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str=None,
            page_size=10,
            max_records=20,
        )
        assert len(rows) == 20
        skips = [c["params"]["$skip"] for c in session.calls]
        assert skips == [0, 10]

    def test_select_forwarded(self) -> None:
        """A `$select` projection reaches the query params."""
        session = _FakeSession(nfip_records=make_nfip_records(3))
        paginate_nfip(
            make_client(session),
            ENDPOINT,
            "NfipClaims",
            filter_str=None,
            page_size=10,
            select="id,yearOfLoss",
        )
        assert session.calls[0]["params"]["$select"] == "id,yearOfLoss"

    def test_stable_orderby_sent_on_every_page(self) -> None:
        """Every NFIP page carries a stable `$orderby` so paging can't drift (M2)."""
        session = _FakeSession(nfip_records=make_nfip_records(25), nfip_total=25)
        paginate_nfip(
            make_client(session), ENDPOINT, "NfipClaims", filter_str=None, page_size=10
        )
        assert all(c["params"].get("$orderby") == "id" for c in session.calls)

    def test_total_is_none_when_count_omitted(self) -> None:
        """A server that omits metadata.count yields total None, not 0."""
        session = _FakeSession(nfip_records=make_nfip_records(3), nfip_omit_count=True)
        rows, total = paginate_nfip(
            make_client(session), ENDPOINT, "NfipClaims", filter_str=None, page_size=10
        )
        assert len(rows) == 3
        assert total is None

    def test_null_metadata_degrades_to_none(self) -> None:
        """A `"metadata": null` envelope degrades to total None, not a crash (L4)."""
        session = _FakeSession(
            nfip_records=make_nfip_records(3), nfip_null_metadata=True
        )
        rows, total = paginate_nfip(
            make_client(session), ENDPOINT, "NfipClaims", filter_str=None, page_size=10
        )
        assert len(rows) == 3
        assert total is None


@pytest.mark.unit
class TestPaginateArcgis:
    """`paginate_arcgis` walks `exceededTransferLimit`."""

    def test_merges_pages_until_not_exceeded(self) -> None:
        """Features from every page are concatenated until the limit clears."""
        feats = [
            {"type": "Feature", "geometry": None, "properties": {"i": i}}
            for i in range(5)
        ]
        session = _FakeSession(nfhl={"features": feats})
        merged = paginate_arcgis(
            make_client(session),
            "https://x/MapServer/28/query",
            {"f": "geojson"},
            page_size=2,
        )
        assert len(merged["features"]) == 5
        offsets = [c["params"]["resultOffset"] for c in session.calls]
        assert offsets == [0, 2, 4]

    def test_single_page_when_not_exceeded(self) -> None:
        """A small result is one request with no follow-up page."""
        feats = [{"type": "Feature", "geometry": None, "properties": {}}]
        session = _FakeSession(nfhl={"features": feats})
        merged = paginate_arcgis(
            make_client(session), "https://x/MapServer/28/query", {"f": "geojson"}
        )
        assert len(merged["features"]) == 1
        assert len(session.calls) == 1

    def test_short_but_exceeded_pages_are_not_truncated(self) -> None:
        """A layer whose maxRecordCount is below page_size still pages fully (M1)."""
        feats = [
            {"type": "Feature", "geometry": None, "properties": {"i": i}}
            for i in range(5)
        ]
        # Server caps at 2/page though we request 1000 — short pages flagged exceeded.
        session = _FakeSession(nfhl={"features": feats}, nfhl_server_cap=2)
        merged = paginate_arcgis(
            make_client(session),
            "https://x/MapServer/28/query",
            {"f": "geojson"},
            page_size=1000,
        )
        assert len(merged["features"]) == 5
        offsets = [c["params"]["resultOffset"] for c in session.calls]
        assert offsets == [0, 2, 4]

    def test_full_page_continues_without_exceeded_flag(self) -> None:
        """A full page keeps paging even when the server omits the flag (M2)."""
        feats = [
            {"type": "Feature", "geometry": None, "properties": {"i": i}}
            for i in range(5)
        ]
        session = _FakeSession(nfhl={"features": feats}, nfhl_omit_exceeded=True)
        merged = paginate_arcgis(
            make_client(session),
            "https://x/MapServer/28/query",
            {"f": "geojson"},
            page_size=2,
        )
        assert len(merged["features"]) == 5
        offsets = [c["params"]["resultOffset"] for c in session.calls]
        assert offsets == [0, 2, 4]

    def test_identical_page_stops_a_server_that_ignores_paging(self) -> None:
        """A server that re-sends the same page stops early, no duplicates (L3)."""
        feats = [
            {"type": "Feature", "geometry": None, "properties": {"i": i}}
            for i in range(2)
        ]
        session = _FakeSession(nfhl={"features": feats}, nfhl_ignore_paging=True)
        merged = paginate_arcgis(
            make_client(session),
            "https://x/MapServer/28/query",
            {"f": "geojson"},
            page_size=2,
        )
        assert len(merged["features"]) == 2
        assert len(session.calls) == 2


@pytest.mark.unit
class TestRecordsToFrame:
    """`records_to_frame` mapping and empty handling."""

    def test_no_map_returns_raw_frame(self) -> None:
        """Without a field map every provider column survives."""
        frame = records_to_frame(make_nfip_records(2))
        assert isinstance(frame, pd.DataFrame)
        assert "amountPaidOnBuildingClaim" in frame.columns

    def test_map_renames_and_orders(self) -> None:
        """With a field map the friendly names appear in map order."""
        frame = records_to_frame(make_nfip_records(2), FIELD_MAP)
        assert list(frame.columns) == ["claim_id", "paid", "zone"]
        assert len(frame) == 2

    def test_empty_records_yield_schema_only_frame(self) -> None:
        """No records yields an empty frame with the friendly columns."""
        frame = records_to_frame([], FIELD_MAP)
        assert list(frame.columns) == ["claim_id", "paid", "zone"]
        assert frame.empty

    def test_schema_drift_returns_raw_frame_not_empty(self) -> None:
        """Records with no mapped columns are returned raw, not silently dropped."""
        records = [{"totally": "different", "shape": 1}, {"totally": "x", "shape": 2}]
        frame = records_to_frame(records, FIELD_MAP)
        assert len(frame) == 2
        assert "totally" in frame.columns
