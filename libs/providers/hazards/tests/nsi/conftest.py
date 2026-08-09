"""Offline fixtures for the NSI backend tests.

Every fixture is offline: a :class:`_FakeSession` records requests and returns
canned NSI / NFHL GeoJSON and OpenFEMA NFIP JSON. It is injected into the backend
via `session=` (the repo's HTTP-test idiom), so the real
:class:`~earthlens.base.http.HttpClient` runs on top of it. No network, no real
GDAL beyond pyramids' GeoJSON decode.
"""

from __future__ import annotations

from typing import Any

import pytest

#: A two-feature canned NSI structures GeoJSON (trimmed from a live capture).
STRUCTURES_GEOJSON: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-90.067, 29.950]},
            "properties": {
                "fd_id": 9837305,
                "occtype": "RES1-1SNB",
                "st_damcat": "RES",
                "val_struct": 225638.48,
                "val_cont": 112819.24,
                "found_type": "S",
                "found_ht": 0.75,
                "num_story": 1,
                "sqft": 1201,
                "med_yr_blt": 1938,
                "firmzone": "X",
                "ground_elv": 1.62,
                "cbfips": "220710127003006",
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-90.066, 29.951]},
            "properties": {
                "fd_id": 9837306,
                "occtype": "COM1",
                "st_damcat": "COM",
                "val_struct": 500000.0,
                "val_cont": 250000.0,
                "found_type": "S",
                "found_ht": 1.0,
                "num_story": 2,
                "sqft": 4000,
                "med_yr_blt": 1975,
                "firmzone": "AE",
                "ground_elv": 1.4,
                "cbfips": "220710127003007",
            },
        },
    ],
}

#: An empty NSI response (a non-US polygon or an empty tract).
EMPTY_GEOJSON: dict[str, Any] = {"type": "FeatureCollection", "features": []}

#: A canned FEMA NFHL flood-zone GeoJSON (hand-built; `hazards.fema.gov` is
#: network-blocked from the build env — see the A1 findings).
NFHL_GEOJSON: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-90.07, 29.95],
                        [-90.06, 29.95],
                        [-90.06, 29.96],
                        [-90.07, 29.96],
                        [-90.07, 29.95],
                    ]
                ],
            },
            "properties": {
                "FLD_ZONE": "AE",
                "SFHA_TF": "T",
                "ZONE_SUBTY": "",
                "STATIC_BFE": 12.0,
            },
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-90.06, 29.95],
                        [-90.05, 29.95],
                        [-90.05, 29.96],
                        [-90.06, 29.96],
                        [-90.06, 29.95],
                    ]
                ],
            },
            "properties": {
                "FLD_ZONE": "X",
                "SFHA_TF": "F",
                "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD",
                "STATIC_BFE": -9999.0,
            },
        },
    ],
}


def make_nfip_records(count: int) -> list[dict[str, Any]]:
    """Build `count` canned NFIP claim records with distinct ids.

    Args:
        count: Number of records to synthesise.

    Returns:
        list[dict]: Records carrying the provider field names the backend maps.
    """
    return [
        {
            "id": 2724000 + i,
            "dateOfLoss": "2005-09-22T00:00:00.000Z",
            "yearOfLoss": 2005,
            "ratedFloodZone": "A03",
            "floodZoneCurrent": "AE",
            "amountPaidOnBuildingClaim": 1000.0 + i,
            "amountPaidOnContentsClaim": 200.0 + i,
            "amountPaidOnIncreasedCostOfComplianceClaim": 0.0,
            "buildingDamageAmount": 5000.0 + i,
            "buildingPropertyValue": 150000.0,
            "occupancyType": 2,
            "causeOfDamage": "1",
            "floodEvent": None,
            "waterDepth": 0,
            "state": "LA",
            "countyCode": "22071",
            "censusGeoid": "22071012700",
            "latitude": 30.0,
            "longitude": -90.1,
        }
        for i in range(count)
    ]


class _FakeResponse:
    """A minimal `requests.Response` stand-in wrapping a JSON payload.

    Carries `status_code` and `headers` so the real
    :class:`~earthlens.base.http.HttpClient` retry/JSON path runs unchanged.
    """

    def __init__(
        self,
        payload: Any,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        """No-op: canned responses are always 200."""

    def json(self) -> Any:
        """Return the wrapped payload."""
        return self._payload


class _FakeSession:
    """A `requests.Session` stand-in routing each verb to a canned payload.

    :class:`~earthlens.base.http.HttpClient` dispatches to `session.get` /
    `session.post`, so implementing those two is enough to drive the backend
    through the real client offline.

    Attributes:
        calls: Every recorded `(method, url, params, json)` request.
        structures: The GeoJSON returned for an NSI structures request.
        nfhl: The GeoJSON returned for an NFHL query.
        nfip_total: The `metadata.count` returned for a `$inlinecount` request.
        nfip_records: The full record list paged over by `$top`/`$skip`.
    """

    def __init__(
        self,
        structures: dict | None = None,
        nfhl: dict | None = None,
        nfip_records: list[dict] | None = None,
        nfip_total: int | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.structures = structures if structures is not None else STRUCTURES_GEOJSON
        self.nfhl = nfhl if nfhl is not None else NFHL_GEOJSON
        self.nfip_records = nfip_records if nfip_records is not None else []
        self.nfip_total = (
            nfip_total if nfip_total is not None else len(self.nfip_records)
        )

    def get(self, url: str, params: dict | None = None, **kwargs: Any) -> _FakeResponse:
        """Route a GET to its canned payload by URL and query params."""
        params = params or {}
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "nsiapi/structures" in url:
            return _FakeResponse(self.structures)
        if "/query" in url:
            feats = self.nfhl.get("features", [])
            offset = int(params.get("resultOffset", 0))
            count = int(params.get("resultRecordCount", len(feats)))
            page = feats[offset : offset + count]
            payload = {
                "type": "FeatureCollection",
                "features": page,
                "exceededTransferLimit": offset + count < len(feats),
            }
            return _FakeResponse(payload)
        if "NfipClaims" in url:
            skip = int(params.get("$skip", 0))
            top = int(params.get("$top", len(self.nfip_records)))
            payload = {"NfipClaims": self.nfip_records[skip : skip + top]}
            if "$inlinecount" in params:
                payload["metadata"] = {"count": self.nfip_total}
            return _FakeResponse(payload)
        raise AssertionError(f"no fixture routed for URL {url!r}")

    def post(self, url: str, json: dict | None = None, **kwargs: Any) -> _FakeResponse:
        """Route a POST (the NSI polygon body) to the structures payload."""
        self.calls.append({"method": "POST", "url": url, "json": json})
        return _FakeResponse(self.structures)


def make_client(session: _FakeSession):
    """Wrap a fake session in a real `HttpClient` for the helper tests."""
    from earthlens.base.http import HttpClient

    return HttpClient(session=session)


@pytest.fixture
def fake_session() -> _FakeSession:
    """A default recording session with the two-feature structures fixture."""
    return _FakeSession()
