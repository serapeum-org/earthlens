"""Unit tests for the WDPA protected-area backend."""

from __future__ import annotations

import pytest
from geopandas import GeoDataFrame
from shapely.geometry import MultiPolygon, Polygon

from earthlens.biodiversity import LicenseWarning
from earthlens.wdpa import WDPA, AuthenticationError


def _backend(tmp_path, variables=None, token="test-token", **kwargs):
    """Build a WDPA backend over a whole-Earth bbox with a test token."""
    return WDPA(
        start="2024-01-01",
        end="2024-12-31",
        variables=variables if variables is not None else ["KEN"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
        token=token,
        **kwargs,
    )


@pytest.mark.wdpa
class TestConstruction:
    """Construction validates inputs and requires a token."""

    def test_output_kind_is_vector(self, tmp_path):
        """The backend declares vector output."""
        assert _backend(tmp_path).OUTPUT_KIND == "vector"

    def test_missing_token_raises(self, tmp_path, monkeypatch):
        """Constructing without a token raises naming WDPA_TOKEN."""
        monkeypatch.delenv("WDPA_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="WDPA_TOKEN"):
            _backend(tmp_path, token=None)

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping `variables` raises a clear TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _backend(tmp_path, variables={"KEN": 1})

    def test_empty_variables_rejected(self, tmp_path):
        """An empty `variables` raises a ValueError."""
        with pytest.raises(ValueError, match="at least one country"):
            _backend(tmp_path, variables=[])

    def test_unknown_file_format_rejected(self, tmp_path):
        """An unknown file_format raises a ValueError."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            _backend(tmp_path, file_format="shp")


@pytest.mark.wdpa
class TestFetchAndDownload:
    """`download` fetches protected-area polygons over the v4 REST API."""

    def test_country_request_returns_polygons(self, tmp_path, fake_wdpa):
        """A country request authenticates, fetches, and returns polygons."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        fc = _backend(tmp_path).download()
        assert isinstance(fc, GeoDataFrame)
        assert len(fc) == 1
        assert fc.crs.to_epsg() == 4326
        assert isinstance(fc.geometry.iloc[0], (Polygon, MultiPolygon))

    def test_request_uses_query_token_and_geometry(self, tmp_path, fake_wdpa):
        """The request carries the token as a query param and with_geometry."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        _backend(tmp_path).download()
        call = fake_wdpa.state.calls[0]
        assert call["url"].startswith(
            "https://api.protectedplanet.net/v4/protected_areas/search"
        )
        assert call["params"]["token"] == "test-token"
        assert call["params"]["with_geometry"] == "true"
        assert call["params"]["country"] == "KEN"

    def test_pagination_fetches_second_page(self, tmp_path, fake_wdpa):
        """A full first page (50) triggers a second page fetch."""
        full_page = [fake_wdpa.area(wdpa_id=str(i)) for i in range(50)]
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.response({"protected_areas": full_page}),
                fake_wdpa.response({"protected_areas": [fake_wdpa.area(wdpa_id="x")]}),
            ]
        )
        fc = _backend(tmp_path).download()
        assert len(fake_wdpa.state.calls) == 2
        assert len(fc) == 51

    def test_point_only_area_dropped(self, tmp_path, fake_wdpa):
        """A point-only protected area is dropped from the polygon result."""
        point = {"type": "Point", "coordinates": [0, 0]}
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area(geometry=point)]})]
        )
        assert len(_backend(tmp_path).download()) == 0

    def test_by_id_request(self, tmp_path, fake_wdpa):
        """A numeric selector fetches a single protected area by id."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_area": fake_wdpa.area(wdpa_id="555")})]
        )
        fc = _backend(tmp_path, variables=["555"]).download()
        assert fake_wdpa.state.calls[0]["url"].endswith("/protected_areas/555")
        assert len(fc) == 1

    def test_license_warning_fires(self, tmp_path, fake_wdpa):
        """Every fetch raises the UNEP-WCMC LicenseWarning."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        with pytest.warns(LicenseWarning):
            _backend(tmp_path).download()

    def test_http_401_raises_auth_error(self, tmp_path, fake_wdpa):
        """An HTTP 401 surfaces as an AuthenticationError."""
        fake_wdpa.state.set_responses([fake_wdpa.response({}, status_code=401)])
        with pytest.raises(AuthenticationError, match="401"):
            _backend(tmp_path).download()

    def test_non_401_error_does_not_leak_token(self, tmp_path, fake_wdpa):
        """A non-recoverable 4xx surfaces as RuntimeError WITHOUT the token in the message."""
        # Burn through the 5 retry attempts on 500 + 1 final response so the
        # shim raises (the URL the fake echoes carries SECRET-TOKEN-HERE).
        leaky = "SECRET-TOKEN-HERE"
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({}, status_code=400)]  # 400 is non-retried
        )
        with pytest.raises(RuntimeError) as exc:
            _backend(tmp_path).download()
        assert leaky not in str(exc.value), (
            "WDPA token must never appear in a bubbled-up error message"
        )
        assert "redacted" in str(exc.value).lower()

    def test_retries_on_502_then_succeeds(self, tmp_path, fake_wdpa):
        """A 502 is retried; once a 200 arrives the download completes."""
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.response({}, status_code=502),
                fake_wdpa.response({}, status_code=502),
                fake_wdpa.response({"protected_areas": [fake_wdpa.area()]}),
            ]
        )
        fc = _backend(tmp_path).download()
        assert len(fc) == 1
        assert len(fake_wdpa.state.calls) == 3
        assert len(fake_wdpa.sleeps) == 2  # one sleep per 502 retry

    def test_retries_on_429_honours_retry_after(self, tmp_path, fake_wdpa):
        """A 429 with `Retry-After` sleeps for that many seconds, then retries."""
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.response({}, status_code=429, headers={"Retry-After": "7"}),
                fake_wdpa.response({"protected_areas": []}),
            ]
        )
        _backend(tmp_path).download()
        assert fake_wdpa.sleeps == [7.0]

    def test_retries_on_connection_error(self, tmp_path, fake_wdpa):
        """A transport-layer ConnectionError is retried and recovers on success."""
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.ConnectionError("network glitch"),
                fake_wdpa.response({"protected_areas": [fake_wdpa.area()]}),
            ]
        )
        fc = _backend(tmp_path).download()
        assert len(fc) == 1
        assert len(fake_wdpa.sleeps) == 1

    def test_rest_does_not_mutate_input_rows(self, fake_wdpa):
        """`_to_gdf` builds a GeoDataFrame without consuming the geometry from the input."""
        from earthlens.wdpa._rest import _row, _to_gdf

        row = _row(fake_wdpa.area())
        assert row is not None
        gdf = _to_gdf([row])
        assert len(gdf) == 1
        # The original row must still carry its geometry — _to_gdf used to .pop it.
        assert "geometry" in row, "row was mutated by _to_gdf (geometry popped)"

    def test_empty_path_opts_out_of_writing(self, tmp_path, fake_wdpa):
        """`path=""` returns the in-memory FC but writes no file."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        backend = WDPA(
            start="2024-01-01",
            end="2024-12-31",
            variables=["KEN"],
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            path="",
            token="test-token",
        )
        fc = backend.download()
        assert len(fc) == 1
        assert not any(tmp_path.glob("*.parquet"))

    def test_warn_license_suppressed_on_empty(self, tmp_path, fake_wdpa, recwarn):
        """An empty country result does not emit the LicenseWarning."""
        fake_wdpa.state.set_responses([fake_wdpa.response({"protected_areas": []})])
        _backend(tmp_path).download()
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]

    def test_persistent_5xx_exhausts_retries_then_raises(self, tmp_path, fake_wdpa):
        """When every retry returns 5xx, _get raises a token-free RuntimeError."""
        fake_wdpa.state.set_responses([fake_wdpa.response({}, status_code=503)] * 10)
        with pytest.raises(RuntimeError, match="503") as exc:
            _backend(tmp_path).download()
        assert "SECRET-TOKEN-HERE" not in str(exc.value)
        assert "redacted" in str(exc.value).lower()

    def test_persistent_connection_error_exhausts_retries(self, tmp_path, fake_wdpa):
        """Persistent transport errors raise a token-free RuntimeError after retries."""
        fake_wdpa.state.set_responses([fake_wdpa.ConnectionError("down")] * 10)
        with pytest.raises(RuntimeError, match="redacted") as exc:
            _backend(tmp_path).download()
        assert "SECRET-TOKEN-HERE" not in str(exc.value)

    def test_retry_after_malformed_falls_back_to_backoff(self, tmp_path, fake_wdpa):
        """A 429 with an unparseable Retry-After uses the exponential back-off."""
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.response(
                    {}, status_code=429, headers={"Retry-After": "soon"}
                ),
                fake_wdpa.response({"protected_areas": []}),
            ]
        )
        _backend(tmp_path).download()
        # No usable Retry-After -> sleep = BACKOFF_FACTOR * 2**0 = 1.0
        assert fake_wdpa.sleeps == [1.0]

    def test_explicit_session_is_passed_through(self):
        """`_session(session)` returns the caller's session verbatim, no new one made."""
        from earthlens.wdpa._rest import _session

        sentinel = object()
        assert _session(sentinel) is sentinel, "the explicit session must pass through"

    def test_fetch_country_max_pages_zero_short_circuits(self):
        """`fetch_country(..., max_pages=0)` does not call the HTTP session at all."""
        from earthlens.wdpa._rest import fetch_country

        class _BoomSession:
            def get(self, *args, **kwargs):
                raise AssertionError("HTTP must not be called when max_pages=0")

        result = fetch_country("token", "KEN", session=_BoomSession(), max_pages=0)
        assert len(result) == 0, "no pages fetched -> empty result"

    def test_retry_after_http_date_form(self, tmp_path, fake_wdpa):
        """A 429 carrying an HTTP-date Retry-After parses to a positive wait.

        RFC 9110 §10.2.3 allows the header in HTTP-date form. Today neither
        upstream uses it, but we honour it if they ever do — a future
        server-side change should not silently fall back to back-off.
        """
        fake_wdpa.state.set_responses(
            [
                fake_wdpa.response(
                    {},
                    status_code=429,
                    headers={"Retry-After": "Fri, 31 Dec 2099 23:59:59 GMT"},
                ),
                fake_wdpa.response({"protected_areas": []}),
            ]
        )
        _backend(tmp_path).download()
        # The wait is the delta until 2099 — many years of seconds.
        assert len(fake_wdpa.sleeps) == 1
        assert fake_wdpa.sleeps[0] > 365 * 24 * 3600

    def test_download_writes_geoparquet(self, tmp_path, fake_wdpa):
        """A non-empty result is written to a GeoParquet file under path."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        _backend(tmp_path).download()
        assert (tmp_path / "wdpa_protected_areas.parquet").exists()

    def test_country_prefix_selector(self, tmp_path, fake_wdpa):
        """A `country:` prefixed selector fetches the country's areas."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        _backend(tmp_path, variables=["country:KEN"]).download()
        assert fake_wdpa.state.calls[0]["params"]["country"] == "KEN"

    def test_empty_result_keeps_schema(self, tmp_path, fake_wdpa):
        """A country with no areas yields an empty FeatureCollection."""
        fc = _backend(tmp_path).download()
        assert len(fc) == 0
        assert "geometry" in fc.columns

    def test_geojson_write(self, tmp_path, fake_wdpa):
        """A non-parquet file_format writes via the OGR driver."""
        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        _backend(tmp_path, file_format="geojson").download()
        assert (tmp_path / "wdpa_protected_areas.geojson").exists()

    def test_api_returns_collection(self, tmp_path, fake_wdpa):
        """`_api` returns the protected-area FeatureCollection."""
        from geopandas import GeoDataFrame

        fake_wdpa.state.set_responses(
            [fake_wdpa.response({"protected_areas": [fake_wdpa.area()]})]
        )
        assert isinstance(_backend(tmp_path)._api(), GeoDataFrame)

    def test_aggregate_rejected(self, tmp_path, fake_wdpa):
        """A non-None aggregate raises NotImplementedError mentioning vector."""
        with pytest.raises(NotImplementedError, match="vector"):
            _backend(tmp_path).download(aggregate=object())


@pytest.mark.wdpa
class TestLimitStopsTheWork:
    """A `limit=` must stop querying selectors, not trim the merged frame.

    Each selector is a paged REST sweep over a country's protected areas, so a
    cap that only sliced the concatenated result would still pay for every
    country. Counting the per-selector calls is what distinguishes the two.
    """

    def test_selectors_past_the_cap_are_never_queried(self, tmp_path, monkeypatch):
        """The second country is not fetched once the first fills the cap."""
        backend = _backend(tmp_path, variables=["KEN", "TZA"])
        queried: list[str] = []
        square = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])

        def fake_fetch_selector(token, selector):
            queried.append(selector)
            return GeoDataFrame(
                {"name": ["a", "b", "c"]},
                geometry=[square, square, square],
                crs="EPSG:4326",
            )

        monkeypatch.setattr(backend, "_fetch_selector", fake_fetch_selector)
        backend._limit = 2
        merged = backend._fetch_all()

        assert queried == ["KEN"], (
            "the second selector was queried even though the first already "
            "filled the cap; the cap is trimming, not stopping the work"
        )
        assert len(merged) == 2

    def test_no_limit_queries_every_selector(self, tmp_path, monkeypatch):
        """Without a cap every requested country is still swept."""
        backend = _backend(tmp_path, variables=["KEN", "TZA"])
        queried: list[str] = []
        square = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])

        def fake_fetch_selector(token, selector):
            queried.append(selector)
            return GeoDataFrame({"name": ["a"]}, geometry=[square], crs="EPSG:4326")

        monkeypatch.setattr(backend, "_fetch_selector", fake_fetch_selector)
        backend._limit = None
        merged = backend._fetch_all()

        assert queried == ["KEN", "TZA"]
        assert len(merged) == 2

    def test_a_cap_that_matches_nothing_returns_an_empty_frame(
        self, tmp_path, monkeypatch
    ):
        """Every selector empty yields the schema-correct empty frame, not a crash."""
        backend = _backend(tmp_path, variables=["KEN"])
        monkeypatch.setattr(
            backend,
            "_fetch_selector",
            lambda token, selector: GeoDataFrame(
                {"name": []}, geometry=[], crs="EPSG:4326"
            ),
        )
        backend._limit = 5
        merged = backend._fetch_all()
        assert len(merged) == 0


@pytest.mark.wdpa
class TestPublicDownloadHonoursTheCap:
    """`download(limit=)` must reach the selector sweep, not just validate."""

    def test_the_keyword_bounds_the_result(self, tmp_path, monkeypatch):
        """A cap passed to `download` stops the second country being queried."""
        backend = _backend(tmp_path, variables=["KEN", "TZA"])
        queried: list[str] = []
        square = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])

        def fake_fetch_selector(token, selector):
            queried.append(selector)
            return GeoDataFrame(
                {"name": ["a", "b", "c"]},
                geometry=[square] * 3,
                crs="EPSG:4326",
            )

        monkeypatch.setattr(backend, "_fetch_selector", fake_fetch_selector)
        monkeypatch.setattr(backend, "_initialize", lambda: None)
        backend._auth = type("_Auth", (), {"token": "t"})()

        with pytest.warns(LicenseWarning):
            result = backend.download(progress_bar=False, limit=2)

        assert len(result) == 2
        assert queried == ["KEN"]
