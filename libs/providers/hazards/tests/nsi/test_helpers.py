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
