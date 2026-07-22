"""Unit tests for the IUCN Red List assessment backend."""

from __future__ import annotations

import pandas as pd
import pytest
from earthlens.iucn._rest import IUCN_COLUMNS

from earthlens.biodiversity import LicenseWarning
from earthlens.iucn import IUCN, AuthenticationError


def _backend(tmp_path, variables=None, token="test-token", **kwargs):
    """Build an IUCN backend over a whole-Earth bbox with a test token."""
    return IUCN(
        start="2024-01-01",
        end="2024-12-31",
        variables=variables if variables is not None else ["species:Panthera leo"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        path=str(tmp_path),
        token=token,
        **kwargs,
    )


def _species_summary():
    """A taxa/scientific_name response with one latest assessment."""
    return {
        "taxon": {"scientific_name": "Panthera leo"},
        "assessments": [
            {
                "assessment_id": 12345,
                "red_list_category_code": "VU",
                "year_published": "2016",
                "latest": True,
                "possibly_extinct": False,
                "url": "https://example.org/12345",
            }
        ],
    }


def _assessment_detail():
    """An assessment/{id} detail body with the nested category shape."""
    return {
        "assessment_id": 12345,
        "red_list_category": {"code": "VU"},
        "criteria": "A2abcd",
        "population_trend": "Decreasing",
    }


@pytest.mark.iucn
class TestConstruction:
    """Construction validates inputs and requires a token."""

    def test_output_kind_is_tabular(self, tmp_path):
        """The backend declares tabular output."""
        assert _backend(tmp_path).OUTPUT_KIND == "tabular"

    def test_missing_token_raises(self, tmp_path, monkeypatch):
        """Constructing without a token raises naming IUCN_TOKEN."""
        monkeypatch.delenv("IUCN_TOKEN", raising=False)
        with pytest.raises(AuthenticationError, match="IUCN_TOKEN"):
            _backend(tmp_path, token=None)

    def test_dict_variables_rejected(self, tmp_path):
        """A mapping `variables` raises a clear TypeError."""
        with pytest.raises(TypeError, match="not a mapping"):
            _backend(tmp_path, variables={"x": 1})

    def test_empty_variables_rejected(self, tmp_path):
        """An empty `variables` raises a ValueError."""
        with pytest.raises(ValueError, match="at least one species"):
            _backend(tmp_path, variables=[])

    def test_unknown_file_format_rejected(self, tmp_path):
        """An unknown file_format raises a ValueError."""
        with pytest.raises(ValueError, match="file_format must be"):
            _backend(tmp_path, file_format="xlsx")


@pytest.mark.iucn
class TestFetchAndDownload:
    """`download` fetches assessments over the v4 API into a DataFrame."""

    def test_non_latest_assessment_skips_detail_enrichment(self, tmp_path, fake_iucn):
        """`fetch_species` only enriches the latest assessment, not older ones."""
        fake_iucn.state.route(
            "taxa/scientific_name",
            {
                "taxon": {"scientific_name": "Panthera leo"},
                "assessments": [
                    {
                        "assessment_id": 1,
                        "red_list_category_code": "VU",
                        "year_published": "2010",
                        "latest": False,
                    }
                ],
            },
        )
        _backend(tmp_path).download()
        assessment_calls = [
            c for c in fake_iucn.state.calls if "assessment/" in c["url"]
        ]
        assert assessment_calls == [], (
            "a non-latest assessment must not trigger the detail fetch; "
            f"got calls: {assessment_calls}"
        )

    def test_latest_without_assessment_id_skips_detail_enrichment(
        self, tmp_path, fake_iucn
    ):
        """A latest assessment lacking an id is also skipped (no `assessment/None` GET)."""
        fake_iucn.state.route(
            "taxa/scientific_name",
            {
                "taxon": {"scientific_name": "Panthera leo"},
                "assessments": [
                    {
                        "assessment_id": None,
                        "red_list_category_code": "VU",
                        "latest": True,
                    }
                ],
            },
        )
        _backend(tmp_path).download()
        assert not [c for c in fake_iucn.state.calls if "assessment/" in c["url"]]

    def test_species_two_step_and_bearer(self, tmp_path, fake_iucn):
        """A species query does the binomial split + two-step with a Bearer header."""
        fake_iucn.state.route("taxa/scientific_name", _species_summary())
        fake_iucn.state.route("assessment/12345", _assessment_detail())
        frame = _backend(tmp_path).download()
        first = fake_iucn.state.calls[0]
        assert first["url"] == (
            "https://api.iucnredlist.org/api/v4/taxa/scientific_name"
        )
        assert first["params"] == {"genus_name": "Panthera", "species_name": "leo"}
        assert first["headers"]["Authorization"] == "Bearer test-token"
        assert any("assessment/12345" in c["url"] for c in fake_iucn.state.calls)
        assert isinstance(frame, pd.DataFrame)
        assert frame.loc[0, "category"] == "VU"
        assert frame.loc[0, "criteria"] == "A2abcd"

    def test_country_endpoint(self, tmp_path, fake_iucn):
        """A country query hits countries/{code} with the ISO alpha-2 code."""
        fake_iucn.state.route(
            "countries/KE",
            {
                "assessments": [
                    {
                        "assessment_id": 7,
                        "taxon": {"scientific_name": "Acinonyx jubatus"},
                        "red_list_category_code": "VU",
                    }
                ]
            },
        )
        frame = _backend(tmp_path, variables=["country:KE"]).download()
        assert fake_iucn.state.calls[0]["url"].endswith("/countries/KE")
        assert frame.loc[0, "scientific_name"] == "Acinonyx jubatus"

    def test_always_warns_license(self, tmp_path, fake_iucn, recwarn):
        """Every successful fetch fires exactly one LicenseWarning."""
        fake_iucn.state.route("taxa/scientific_name", _species_summary())
        fake_iucn.state.route("assessment/12345", _assessment_detail())
        _backend(tmp_path).download()
        warnings = [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]
        assert len(warnings) == 1

    def test_country_query_also_warns(self, tmp_path, fake_iucn):
        """A non-empty country query also fires the LicenseWarning."""
        fake_iucn.state.route(
            "countries/KE",
            {
                "assessments": [
                    {
                        "assessment_id": 1,
                        "red_list_category_code": "LC",
                        "taxon": {"scientific_name": "Acinonyx jubatus"},
                    }
                ]
            },
        )
        with pytest.warns(LicenseWarning):
            _backend(tmp_path, variables=["country:KE"]).download()

    def test_unknown_selector_rejected(self, tmp_path, fake_iucn):
        """A selector without a species:/country: prefix raises a ValueError."""
        with pytest.raises(ValueError, match="must start with"):
            _backend(tmp_path, variables=["Panthera leo"]).download()

    def test_empty_result_keeps_schema(self, tmp_path, fake_iucn):
        """An empty country list yields an empty DataFrame with the columns."""
        fake_iucn.state.route("countries/KE", {"assessments": []})
        frame = _backend(tmp_path, variables=["country:KE"]).download()
        assert len(frame) == 0
        assert list(frame.columns) == list(IUCN_COLUMNS)

    def test_http_401_raises_auth_error(self, tmp_path, fake_iucn):
        """An HTTP 401 surfaces as an AuthenticationError."""
        fake_iucn.state.route("taxa/scientific_name", {}, status_code=401)
        with pytest.raises(AuthenticationError, match="401"):
            _backend(tmp_path).download()

    def test_download_writes_csv(self, tmp_path, fake_iucn):
        """A non-empty result is written to a CSV file under path."""
        fake_iucn.state.route("taxa/scientific_name", _species_summary())
        fake_iucn.state.route("assessment/12345", _assessment_detail())
        _backend(tmp_path).download()
        assert (tmp_path / "iucn_assessments.csv").exists()

    def test_download_writes_parquet(self, tmp_path, fake_iucn):
        """The parquet file_format writes a parquet file."""
        fake_iucn.state.route("taxa/scientific_name", _species_summary())
        fake_iucn.state.route("assessment/12345", _assessment_detail())
        _backend(tmp_path, file_format="parquet").download()
        assert (tmp_path / "iucn_assessments.parquet").exists()

    def test_api_returns_dataframe(self, tmp_path, fake_iucn):
        """`_api` returns the assessment DataFrame."""
        fake_iucn.state.route("countries/KE", {"assessments": []})
        assert isinstance(
            _backend(tmp_path, variables=["country:KE"])._api(), pd.DataFrame
        )

    def test_aggregate_rejected(self, tmp_path, fake_iucn):
        """A non-None aggregate raises NotImplementedError mentioning tabular."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _backend(tmp_path).download(aggregate=object())

    def test_empty_path_opts_out_of_writing(self, tmp_path, fake_iucn):
        """`path=""` returns the in-memory DataFrame but writes no file."""
        fake_iucn.state.route("taxa/scientific_name", _species_summary())
        fake_iucn.state.route("assessment/12345", _assessment_detail())
        backend = IUCN(
            start="2024-01-01",
            end="2024-12-31",
            variables=["species:Panthera leo"],
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            path="",
            token="test-token",
        )
        frame = backend.download()
        assert len(frame) >= 1
        assert not any(tmp_path.glob("iucn_*"))

    def test_warn_license_suppressed_on_empty(self, tmp_path, fake_iucn, recwarn):
        """An empty country result does not emit the LicenseWarning."""
        fake_iucn.state.route("countries/KE", {"assessments": []})
        _backend(tmp_path, variables=["country:KE"]).download()
        assert not [w for w in recwarn.list if issubclass(w.category, LicenseWarning)]


@pytest.mark.iucn
class TestRestRetry:
    """The REST shim retries on transient failures and exposes a clean ValueError on 404."""

    def test_throttle_first_call_does_not_sleep(self, tmp_path, fake_iucn):
        """The first request in a session does not pay the inter-call delay."""
        fake_iucn.state.route("countries/KE", {"assessments": []})
        _backend(tmp_path, variables=["country:KE"]).download()
        # The throttle never sleeps on the first call; the only sleeps allowed
        # here are retry back-offs (none for a clean 200).
        assert fake_iucn.sleeps == []

    def test_retries_on_502_then_succeeds(self, tmp_path, fake_iucn):
        """A 502 is retried; once a 200 arrives the download completes.

        Three HTTP calls (502, 502, 200) and three sleeps: two retry back-offs
        (1.0, 2.0) and one inter-call throttle (the first back-off only advanced
        the simulated clock by 1.0 s, so the next call still owes the remaining
        1.0 s of the 2 s throttle window).
        """
        fake_iucn.state.route_queue(
            "countries/KE",
            [
                fake_iucn.response({}, status_code=502),
                fake_iucn.response({}, status_code=502),
                fake_iucn.response({"assessments": []}),
            ],
        )
        _backend(tmp_path, variables=["country:KE"]).download()
        assert (
            len([c for c in fake_iucn.state.calls if "countries/KE" in c["url"]]) == 3
        )
        assert fake_iucn.sleeps == [1.0, 1.0, 2.0]

    def test_retries_on_429_honours_retry_after(self, tmp_path, fake_iucn):
        """A 429 carrying `Retry-After` sleeps for that many seconds, then retries."""
        fake_iucn.state.route_queue(
            "countries/KE",
            [
                fake_iucn.response({}, status_code=429, headers={"Retry-After": "11"}),
                fake_iucn.response({"assessments": []}),
            ],
        )
        _backend(tmp_path, variables=["country:KE"]).download()
        assert fake_iucn.sleeps == [11.0]

    def test_404_surfaces_as_value_error(self, tmp_path, fake_iucn):
        """A 404 (unknown species / country) reads as a clean ValueError, not HTTPError."""
        fake_iucn.state.route("countries/XQ", {}, status_code=404)
        with pytest.raises(ValueError, match="404"):
            _backend(tmp_path, variables=["country:XQ"]).download()

    def test_connection_error_retries_then_recovers(self, tmp_path, fake_iucn):
        """A transport-layer ConnectionError is retried and recovers on success.

        One retry back-off (1.0 s) plus one inter-call throttle (1.0 s — the
        back-off advanced the simulated clock 1.0 s but the throttle window is
        2 s).
        """
        fake_iucn.state.route_queue(
            "countries/KE",
            [
                fake_iucn.ConnectionError("network glitch"),
                fake_iucn.response({"assessments": []}),
            ],
        )
        _backend(tmp_path, variables=["country:KE"]).download()
        assert fake_iucn.sleeps == [1.0, 1.0]

    def test_persistent_5xx_exhausts_retries_then_raises(self, tmp_path, fake_iucn):
        """When every retry returns 5xx, _get raises a clear RuntimeError."""
        fake_iucn.state.route_queue(
            "countries/KE",
            [fake_iucn.response({}, status_code=503)] * 10,
        )
        with pytest.raises(RuntimeError, match="503"):
            _backend(tmp_path, variables=["country:KE"]).download()

    def test_persistent_connection_error_exhausts_retries(self, tmp_path, fake_iucn):
        """Persistent transport errors raise a clean RuntimeError after retries."""
        fake_iucn.state.route_queue(
            "countries/KE",
            [fake_iucn.ConnectionError("down")] * 10,
        )
        with pytest.raises(RuntimeError, match="transport error"):
            _backend(tmp_path, variables=["country:KE"]).download()

    def test_throttle_sleeps_between_back_to_back_calls(self, tmp_path, fake_iucn):
        """A species query (two HTTP calls) throttles the second call."""
        fake_iucn.state.route("taxa/scientific_name", _species_summary())
        fake_iucn.state.route("assessment/12345", _assessment_detail())
        _backend(tmp_path).download()
        # 1st call: no throttle. 2nd call (assessment detail): throttle ≈ 2s.
        # Allow any sleep ≤ THROTTLE_SECONDS to appear in the sleeps list.
        from earthlens.iucn._rest import THROTTLE_SECONDS

        assert any(0 < s <= THROTTLE_SECONDS for s in fake_iucn.sleeps), (
            f"expected an inter-call throttle sleep, got {fake_iucn.sleeps!r}"
        )
