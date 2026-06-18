"""Unit tests for the IUCN Red List assessment backend."""

from __future__ import annotations

import pandas as pd
import pytest

from earthlens.biodiversity import LicenseWarning
from earthlens.iucn import IUCN, AuthenticationError
from earthlens.iucn._rest import IUCN_COLUMNS


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
            {"assessments": [{"assessment_id": 7, "taxon": {"scientific_name": "Acinonyx jubatus"}, "red_list_category_code": "VU"}]},
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
        """A country query also fires the LicenseWarning."""
        fake_iucn.state.route("countries/KE", {"assessments": []})
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
        assert isinstance(_backend(tmp_path, variables=["country:KE"])._api(), pd.DataFrame)

    def test_aggregate_rejected(self, tmp_path, fake_iucn):
        """A non-None aggregate raises NotImplementedError mentioning tabular."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _backend(tmp_path).download(aggregate=object())
