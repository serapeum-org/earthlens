"""Tests for the `EEA_AQ` backend request shaping and output."""

from __future__ import annotations

import builtins
import shutil
from pathlib import Path

import pandas as pd
import pytest
from loguru import logger

from earthlens.eea_aq import EEA_AQ

from .conftest import _FakeAirbaseClient


def _hourly_frame() -> pd.DataFrame:
    """Two MT pm25 rows an hour apart (11:00 and 12:00) on 2023-06-15."""
    return pd.DataFrame(
        {
            "Samplingpoint": ["MT/SPO-1", "MT/SPO-1"],
            "Pollutant": [6001, 6001],
            "Start": pd.to_datetime(["2023-06-15T11:00", "2023-06-15T12:00"]),
            "End": pd.to_datetime(["2023-06-15T12:00", "2023-06-15T13:00"]),
            "Value": ["5.0", "6.0"],
            "Unit": ["ug.m-3"] * 2,
            "AggType": ["hour"] * 2,
            "Validity": [1, 1],
            "Verification": [3, 3],
        }
    )


def _era_frame(verification: int) -> pd.DataFrame:
    """One MT pm25 row dated in-window, tagged with a per-era Verification."""
    return pd.DataFrame(
        {
            "Samplingpoint": ["MT/SPO-1"],
            "Pollutant": [6001],
            "Start": pd.to_datetime(["2023-06-15T00:00"]),
            "End": pd.to_datetime(["2023-06-15T01:00"]),
            "Value": ["5.0"],
            "Unit": ["ug.m-3"],
            "AggType": ["hour"],
            "Validity": [1],
            "Verification": [verification],
        }
    )


def _row_frame(start: str, verification: int = 1) -> pd.DataFrame:
    """One MT pm25 row at ISO `start`, tagged with `verification`."""
    starts = pd.to_datetime([start])
    return pd.DataFrame(
        {
            "Samplingpoint": ["MT/SPO-1"],
            "Pollutant": [6001],
            "Start": starts,
            # `End` is unused by `shape_frame`; keep it equal to `Start` to avoid
            # timedelta arithmetic (the value is irrelevant to the reshape).
            "End": starts,
            "Value": ["5.0"],
            "Unit": ["ug.m-3"],
            "AggType": ["hour"],
            "Validity": [1],
            "Verification": [verification],
        }
    )


class _EraRequest:
    """Copies a per-era fixture Parquet into the download dir."""

    def __init__(self, parquet: str) -> None:
        self._parquet = parquet

    def download(
        self, dir: str, skip_existing: bool = True, raise_for_status: bool = True
    ) -> None:
        shutil.copy(self._parquet, Path(dir) / "d.parquet")


class _EraClient:
    """Serves different Parquet for the Verified vs Unverified era."""

    countries = frozenset({"MT"})

    def __init__(self, verified: str, unverified: str) -> None:
        self._verified = verified
        self._unverified = unverified

    def request(self, source, *countries, poll=None, verbose=True):
        return _EraRequest(self._verified if source == "Verified" else self._unverified)


class _NoFilesRequest:
    """An airbase request whose download writes no Parquet (an empty era)."""

    def download(
        self, dir: str, skip_existing: bool = True, raise_for_status: bool = True
    ) -> None:
        return None


class _NoFilesClient:
    """A recording client whose every era returns zero Parquet files."""

    countries = frozenset({"MT"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], object]] = []

    def request(self, source, *countries, poll=None, verbose=True):
        self.calls.append((source, countries, poll))
        return _NoFilesRequest()


class _MaybeEraRequest:
    """Copies a fixture Parquet, or writes nothing when the era has no files."""

    def __init__(self, parquet: str | None) -> None:
        self._parquet = parquet

    def download(
        self, dir: str, skip_existing: bool = True, raise_for_status: bool = True
    ) -> None:
        if self._parquet is not None:
            shutil.copy(self._parquet, Path(dir) / "d.parquet")


class _SelectiveEraClient:
    """Serves a Parquet for some eras and zero files for others (by era name)."""

    countries = frozenset({"MT"})

    def __init__(self, **era_parquets: str | None) -> None:
        self._map = era_parquets
        self.calls: list[tuple[str, tuple[str, ...], object]] = []

    def request(self, source, *countries, poll=None, verbose=True):
        self.calls.append((source, countries, poll))
        return _MaybeEraRequest(self._map.get(source))


def _capture(level: str) -> tuple[list[str], int]:
    """Add a loguru sink collecting messages at `level`; return (messages, id)."""
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level=level)
    return messages, sink


def _backend(client, tmp_path: Path, **overrides) -> EEA_AQ:
    """Build an EEA backend wired to the fake airbase client."""
    params = dict(
        start="2023-06-01",
        end="2023-06-30",
        variables=["pm25"],
        lat_lim=[35.7, 36.1],
        lon_lim=[14.1, 14.6],
        country="MT",
        client=client,
        path=str(tmp_path),
    )
    params.update(overrides)
    return EEA_AQ(**params)


@pytest.mark.eea
class TestCountryResolution:
    """Resolving the request to reporting countries."""

    def test_explicit_country_upper(self, tmp_path, fake_client):
        """An explicit lower-case country is upper-cased."""
        backend = _backend(fake_client, tmp_path, country=["mt", "de"])
        assert backend._resolve_countries() == ["MT", "DE"]

    def test_bbox_derived(self, tmp_path, fake_client):
        """Without `country=`, countries come from the bbox."""
        backend = _backend(fake_client, tmp_path, country=None)
        assert backend._resolve_countries() == ["MT"]


@pytest.mark.eea
class TestApi:
    """The download / reshape / window pipeline."""

    def test_returns_requested_pollutant_only(self, tmp_path, fake_client):
        """Only the requested pm25 rows come back (pm10/o3 dropped)."""
        df = _backend(fake_client, tmp_path).download(progress_bar=False)
        assert set(df["parameter"]) == {"pm25"}

    def test_windows_out_of_range_rows(self, tmp_path, fake_client):
        """The 2024 fixture row is dropped by the 2023 window."""
        df = _backend(fake_client, tmp_path).download(progress_bar=False)
        assert (df["datetime_utc"].dt.year == 2023).all()

    def test_request_uses_both_eras_for_recent_year(self, tmp_path, fake_client):
        """A 2023 window queries both Verified and Unverified for MT / PM2.5."""
        _backend(fake_client, tmp_path).download(progress_bar=False)
        assert [call[0] for call in fake_client.calls] == ["Verified", "Unverified"]
        assert all(
            call[1] == ("MT",) and call[2] == ["PM2.5"] for call in fake_client.calls
        )

    def test_dedup_across_eras(self, tmp_path, fake_client):
        """A row served by both eras (recent year) is de-duplicated."""
        df = _backend(fake_client, tmp_path).download(progress_bar=False)
        # The fixture's single in-window 2023 MT pm25 row is downloaded for both
        # Verified and Unverified but must appear once.
        assert len(df) == 1

    def test_dedup_keeps_verified_copy(self, tmp_path):
        """When a row differs across eras, the Verified copy wins the dedup."""
        verified = tmp_path / "v.parquet"
        unverified = tmp_path / "u.parquet"
        _era_frame(verification=3).to_parquet(verified)  # Verified: flag 3
        _era_frame(verification=1).to_parquet(unverified)  # Unverified: flag 1
        client = _EraClient(str(verified), str(unverified))
        df = _backend(client, tmp_path).download(progress_bar=False)
        assert len(df) == 1
        assert df.loc[0, "verification"] == 3

    def test_multi_dataset_range(self, tmp_path, fake_client):
        """A range straddling the boundary requests two datasets."""
        _backend(fake_client, tmp_path, start="2021-06-01", end="2024-06-30").download(
            progress_bar=False
        )
        sources = [call[0] for call in fake_client.calls]
        assert sources == ["Verified", "Unverified"]

    def test_hour_aware_end_is_half_open(self, tmp_path):
        """A non-midnight (hour-aware fmt) end drops a reading exactly at `end`."""
        parquet = tmp_path / "hourly.parquet"
        _hourly_frame().to_parquet(parquet)
        client = _FakeAirbaseClient(str(parquet))
        df = _backend(
            client,
            tmp_path,
            start="2023-06-15T00",
            end="2023-06-15T12",
            fmt="%Y-%m-%dT%H",
        ).download(progress_bar=False)
        assert len(df) == 1
        assert df.iloc[0]["datetime_utc"].hour == 11

    def test_no_country_returns_empty(self, tmp_path, fake_client):
        """A bbox intersecting no country returns a schema-only frame."""
        backend = _backend(
            fake_client,
            tmp_path,
            country=None,
            lat_lim=[10.0, 11.0],
            lon_lim=[-40.0, -39.0],
        )
        df = backend.download(progress_bar=False)
        assert df.empty
        assert "country" in df.columns
        assert fake_client.calls == []

    def test_unsupported_country_dropped(self, tmp_path, fake_client):
        """A country airbase does not serve is dropped, not crashed on."""
        fake_client.countries = frozenset({"MT"})  # DE not served
        _backend(fake_client, tmp_path, country=["MT", "DE"]).download(
            progress_bar=False
        )
        assert all(set(call[1]) <= {"MT"} for call in fake_client.calls)

    def test_all_countries_unsupported_returns_empty(self, tmp_path, fake_client):
        """When no requested country is served, an empty frame comes back."""
        fake_client.countries = frozenset({"FR"})
        df = _backend(fake_client, tmp_path, country="MT").download(progress_bar=False)
        assert df.empty
        assert fake_client.calls == []

    def test_no_parquet_returns_empty(self, tmp_path):
        """A dataset that yields no Parquet returns a schema-only frame."""

        class _EmptyReq:
            def download(self, dir, skip_existing=True, raise_for_status=True):
                return None

        class _EmptyClient:
            calls: list = []

            def request(self, source, *countries, poll=None, verbose=True):
                return _EmptyReq()

        df = _backend(_EmptyClient(), tmp_path).download(progress_bar=False)
        assert df.empty


@pytest.mark.eea
class TestEmptyResultSignals:
    """The two empty results are logged distinctly (see issue #1046).

    A request where every era returns zero files (the shape of an upstream EEA
    export outage) raises a single aggregate WARNING; a window that excludes
    every downloaded row (the era holds data, the dates are simply empty) is an
    INFO, so a caller facing an empty frame can tell the two apart.
    """

    def test_all_eras_empty_warns_once_about_upstream(self, tmp_path):
        """When every era returns zero files, one aggregate upstream WARNING fires."""
        client = _NoFilesClient()
        infos, isink = _capture("INFO")
        warnings, wsink = _capture("WARNING")
        df = _backend(client, tmp_path).download(progress_bar=False)
        logger.remove(wsink)
        logger.remove(isink)

        assert df.empty
        assert "station_id" in df.columns
        assert [call[0] for call in client.calls] == ["Verified", "Unverified"]
        # A single aggregate outage WARNING, naming both eras, not one per era.
        outage = [m for m in warnings if "no era returned any usable observations" in m]
        assert len(outage) == 1
        assert "upstream" in outage[0]
        assert "Verified" in outage[0]
        assert "Unverified" in outage[0]
        # The per-era emptiness is downgraded to a diagnostic INFO (not a WARNING),
        # and is positively emitted per era with the new wording.
        assert not any("returned no Parquet files" in m for m in warnings)
        assert any("era 'Verified' returned no Parquet files" in m for m in infos)
        assert any("era 'Unverified' returned no Parquet files" in m for m in infos)

    def test_out_of_window_download_logs_info_not_outage(self, tmp_path):
        """Files that all fall outside the window log an INFO, not the era WARNING."""
        parquet = tmp_path / "june.parquet"
        _era_frame(verification=1).to_parquet(parquet)  # a single 2023-06-15 row
        client = _FakeAirbaseClient(parquet)
        messages, sink = _capture("INFO")
        df = _backend(client, tmp_path, start="2023-07-01", end="2023-07-31").download(
            progress_bar=False
        )
        logger.remove(sink)

        assert df.empty
        assert "station_id" in df.columns
        assert any("none fell within" in m for m in messages)
        # Distinct from the empty-era path: this is not reported as missing files.
        assert not any("no Parquet files" in m for m in messages)


@pytest.mark.eea
class TestAdjacentEraFallback:
    """When the primary era is empty, the adjacent live era is retried (#1046)."""

    def test_empty_primary_era_falls_back_to_adjacent(self, tmp_path):
        """A June-2022 boundary-year request with an empty Verified era resolves via Unverified."""
        unverified = tmp_path / "u.parquet"
        _row_frame("2022-06-15T00:00").to_parquet(unverified)
        client = _SelectiveEraClient(Verified=None, Unverified=str(unverified))
        infos, isink = _capture("INFO")
        warnings, wsink = _capture("WARNING")
        df = _backend(client, tmp_path, start="2022-06-01", end="2022-06-30").download(
            progress_bar=False
        )
        logger.remove(isink)
        logger.remove(wsink)

        assert len(df) == 1
        assert df.loc[0, "dataset"] == "Unverified"
        assert df.loc[0, "datetime_utc"].year == 2022
        # Verified swept first (empty), then Unverified as the adjacent fallback.
        assert [call[0] for call in client.calls] == ["Verified", "Unverified"]
        # The retry is an INFO, and a recovered download raises no outage WARNING.
        assert any("retrying the adjacent era(s)" in m for m in infos)
        assert not any("no era returned any usable observations" in m for m in warnings)

    def test_no_fallback_when_both_live_eras_already_swept(self, tmp_path):
        """A 2023+ window already spans both live eras, so no fallback fires."""
        client = _NoFilesClient()  # both eras empty
        messages, sink = _capture("WARNING")
        df = _backend(client, tmp_path).download(progress_bar=False)  # default 2023
        logger.remove(sink)

        assert df.empty
        # Exactly the two live eras, with no extra adjacent retry appended.
        assert [call[0] for call in client.calls] == ["Verified", "Unverified"]
        assert not any("retrying the adjacent era(s)" in m for m in messages)

    def test_fallback_era_also_empty_returns_schema_only(self, tmp_path):
        """When both the primary and the adjacent era are empty, an empty frame."""
        client = _SelectiveEraClient(Verified=None, Unverified=None)
        df = _backend(client, tmp_path, start="2022-06-01", end="2022-06-30").download(
            progress_bar=False
        )

        assert df.empty
        assert "station_id" in df.columns
        assert [call[0] for call in client.calls] == ["Verified", "Unverified"]

    def test_out_of_range_request_does_not_fall_back(self, tmp_path):
        """A 2015 empty-Verified request never bulk-downloads the Unverified era."""
        unverified = tmp_path / "u.parquet"
        _row_frame("2023-06-15T00:00").to_parquet(unverified)
        client = _SelectiveEraClient(Verified=None, Unverified=str(unverified))
        df = _backend(client, tmp_path, start="2015-06-01", end="2015-06-30").download(
            progress_bar=False
        )

        assert df.empty
        # Unverified (2023+) can never satisfy 2015, so it is never requested.
        assert [call[0] for call in client.calls] == ["Verified"]


@pytest.mark.eea
class TestGuards:
    """Constructor + download guards."""

    def test_variables_mapping_rejected(self, tmp_path, fake_client):
        """A mapping `variables` is a `TypeError`."""
        with pytest.raises(TypeError):
            _backend(fake_client, tmp_path, variables={"pm25": 1})

    def test_bad_resolution_rejected(self, tmp_path, fake_client):
        """An unaccepted `temporal_resolution` is a `ValueError`."""
        with pytest.raises(ValueError, match="temporal_resolution"):
            _backend(fake_client, tmp_path, temporal_resolution="weekly")

    def test_aggregate_rejected(self, tmp_path, fake_client):
        """`download(aggregate=...)` raises `NotImplementedError`."""
        with pytest.raises(NotImplementedError, match="tabular"):
            _backend(fake_client, tmp_path).download(aggregate=object())

    def test_writes_parquet(self, tmp_path, fake_client):
        """`file_format='parquet'` writes a Parquet file."""
        _backend(fake_client, tmp_path, file_format="parquet").download(
            progress_bar=False
        )
        assert list(tmp_path.glob("eea_aq_*.parquet"))


@pytest.mark.eea
def test_missing_airbase_raises(tmp_path, monkeypatch):
    """With no client and airbase absent, `_airbase_client` raises ImportError."""
    backend = EEA_AQ(
        start="2023-06-01",
        end="2023-06-30",
        variables=["pm25"],
        lat_lim=[35.7, 36.1],
        lon_lim=[14.1, 14.6],
        country="MT",
        path=str(tmp_path),
    )
    real_import = builtins.__import__

    def _no_airbase(name, *args, **kwargs):
        if name == "airbase":
            raise ImportError("no airbase")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_airbase)
    with pytest.raises(ImportError, match="eea_aq"):
        backend._airbase_client()


@pytest.mark.eea
def test_airbase_client_builds_and_caches_real_client(tmp_path, monkeypatch):
    """With no client injected, `_airbase_client` builds one via airbase, once."""
    import airbase

    built: list[object] = []

    def _factory():
        obj = object()
        built.append(obj)
        return obj

    monkeypatch.setattr(airbase, "AirbaseClient", _factory)
    backend = EEA_AQ(
        start="2023-06-01",
        end="2023-06-30",
        variables=["pm25"],
        lat_lim=[35.7, 36.1],
        lon_lim=[14.1, 14.6],
        country="MT",
        path=str(tmp_path),
    )
    first = backend._airbase_client()
    second = backend._airbase_client()

    assert first is built[0], "should return the airbase-built client"
    assert second is first, "should cache the client, not rebuild it"
    assert len(built) == 1, f"client built {len(built)} times, expected once"


@pytest.mark.eea
class TestLimitStopsTheWork:
    """A `limit=` must stop the era downloads, not trim the concatenated frame.

    This backend is the one where the distinction is most expensive: `airbase`
    has no date filter, so each era is a bulk download of every Parquet the
    service holds for the (era, country, pollutant) triple — hundreds of MB is
    normal. Trimming rows afterwards would leave every byte of that transferred.
    """

    def test_the_second_era_is_never_requested_once_the_cap_is_met(
        self, tmp_path, fake_client
    ):
        """A 2023 window spans Verified + Unverified; a met cap stops after one."""
        backend = _backend(fake_client, tmp_path)
        assert len(fake_client.calls) == 0

        backend._limit = 1
        backend._api()

        eras = [call[0] for call in fake_client.calls]
        assert eras == ["Verified"], (
            f"requested {eras}; the Unverified era was bulk-downloaded even "
            f"though the cap was already met by the Verified one"
        )

    def test_without_a_cap_both_eras_are_requested(self, tmp_path, fake_client):
        """The unbounded sweep is unchanged."""
        backend = _backend(fake_client, tmp_path)
        backend._limit = None
        backend._api()

        assert [call[0] for call in fake_client.calls] == ["Verified", "Unverified"]

    def test_a_zero_limit_is_refused_before_any_download(self, tmp_path, fake_client):
        """`limit=0` is caught before the first era is requested."""
        backend = _backend(fake_client, tmp_path)
        with pytest.raises(ValueError):
            backend.download(progress_bar=False, limit=0)
        assert fake_client.calls == []


@pytest.mark.eea
class TestPublicDownloadHonoursTheCap:
    """`download(limit=)` must reach the era loop, not just validate."""

    def test_the_keyword_stops_the_second_era(self, tmp_path, fake_client):
        """A cap passed to `download` leaves the Unverified era unrequested."""
        backend = _backend(fake_client, tmp_path)
        backend.download(progress_bar=False, limit=1)

        assert [call[0] for call in fake_client.calls] == ["Verified"]
