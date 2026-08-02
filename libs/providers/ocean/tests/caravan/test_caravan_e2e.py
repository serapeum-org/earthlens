"""Live end-to-end tests for the Caravan backend (`-m "e2e and caravan"`).

These hit the real Zenodo records pinned in the catalog. They are marker-gated,
never environment-gated, and need no credentials — Caravan is anonymous static
HTTP throughout.

The point of the transfer assertions is not tidiness: they are the guard on the
design claim. If a change ever makes the backend download an archive instead of
range-reading it, these tests fail loudly rather than merely getting slower.
"""

from __future__ import annotations

import pytest

from earthlens.caravan import Caravan, Catalog

pytestmark = [pytest.mark.e2e, pytest.mark.caravan]

#: A generous ceiling on what one catchment may cost. Measured is ~2.9 MB
#: against an 8.84 GB archive; anything approaching the archive size means the
#: range path broke and the whole file is being pulled.
MAX_TRANSFER_MB = 25.0


def test_one_grdc_catchment_costs_megabytes_not_gigabytes(tmp_path):
    """A catchment-year out of the 8.84 GB GRDC archive transfers a few MB."""
    source = Caravan(
        start="2000-01-01",
        end="2000-12-31",
        variables=["streamflow", "total_precipitation", "temperature_2m_mean"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        dataset="grdc",
        gauge_ids=["GRDC_1159100"],
        path=str(tmp_path),
    )

    frame = source.download()

    assert not frame.empty
    assert list(frame.columns) == [
        "gauge_id",
        "date",
        "streamflow",
        "total_precipitation_sum",
        "temperature_2m_mean",
    ]
    assert frame["gauge_id"].unique().tolist() == ["GRDC_1159100"]
    assert len(frame) == 366

    archive = source._open_archive()
    requests_made, megabytes = archive.transfer_stats
    assert megabytes < MAX_TRANSFER_MB, (
        f"one catchment transferred {megabytes:.1f} MB from a "
        f"{source.archive_file.size / 1e9:.1f} GB archive - the range path is broken"
    )
    assert requests_made < 20


def test_the_grdc_archive_index_is_cheap(tmp_path):
    """Indexing 5,356 catchments must read the directory, not the archive."""
    source = Caravan(
        start="2000-01-01",
        end="2000-01-02",
        variables=["streamflow"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        dataset="grdc",
        gauge_ids=["GRDC_1159100"],
        path=str(tmp_path),
    )

    archive = source._open_archive()

    assert len(archive.gauge_ids("grdc")) == 5356
    requests_made, megabytes = archive.transfer_stats
    assert megabytes < 5.0
    assert requests_made <= 8


def test_a_bounding_box_resolves_against_the_real_centroids(tmp_path):
    """A bbox request is resolved from the archive's own attribute table."""
    frame = Caravan(
        start="2019-01-01",
        end="2019-01-03",
        variables=["streamflow"],
        lat_lim=[55.0, 57.5],
        lon_lim=[8.0, 11.0],
        dataset="denmark",
        path=str(tmp_path),
    ).download(limit=6)

    assert not frame.empty
    assert len(frame) == 6
    assert frame["gauge_id"].str.startswith("camelsdk_").all()


def test_the_pinned_grdc_record_still_matches_zenodo():
    """The catalog's pinned file must still exist with the recorded checksum."""
    from earthlens.caravan._helpers import resolve_record

    archive = Catalog().get_extension("grdc").resolve_version().file_for("csv")

    published = resolve_record(archive.record)

    assert archive.name in published, (
        f"record {archive.record} no longer publishes {archive.name}; "
        f"run `earthlens datasets refresh caravan`"
    )
    assert published[archive.name].md5 == archive.md5
    assert published[archive.name].size == archive.size


def test_base_at_its_default_version_refuses_without_the_opt_in(tmp_path):
    """The 29 GB row stays gated even against the live catalog."""
    with pytest.raises(ValueError, match="allow_full_download=True"):
        Caravan(
            start="2000-01-01",
            end="2000-01-02",
            variables=["streamflow"],
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            dataset="base",
            gauge_ids=["camels_01022500"],
            path=str(tmp_path),
        )


def test_base_1_2_is_readable_without_downloading_12_gb(tmp_path):
    """The escape hatch reaches CAMELS data through the range path."""
    source = Caravan(
        start="2000-01-01",
        end="2000-01-05",
        variables=["streamflow", "potential_evaporation"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        dataset="base",
        version="1.2",
        gauge_ids=["camels_01022500"],
        path=str(tmp_path),
    )

    frame = source.download()

    assert len(frame) == 5
    # base 1.2 predates the PET split, so the legacy column name is the one used.
    assert "potential_evaporation_sum" in frame.columns
    _, megabytes = source._open_archive().transfer_stats
    assert megabytes < MAX_TRANSFER_MB


def test_the_community_extensions_published_under_a_camels_name_fetch(tmp_path):
    """Extensions titled CAMELS-CZ / CAMELS-ES are still reachable and Caravan-shaped."""
    for dataset, gauge_id in (
        ("czechia", "camelscz_24042409"),
        ("spain", "camelses_1080"),
    ):
        frame = Caravan(
            start="2019-01-01",
            end="2019-01-05",
            variables=["streamflow", "total_precipitation"],
            lat_lim=[-90.0, 90.0],
            lon_lim=[-180.0, 180.0],
            dataset=dataset,
            gauge_ids=[gauge_id],
            path=str(tmp_path),
        ).download()

        assert len(frame) == 5, dataset
        assert frame["gauge_id"].unique().tolist() == [gauge_id]
        assert "total_precipitation_sum" in frame.columns


def test_the_spanish_efas_columns_come_back(tmp_path):
    """Caravan-ES carries four EFAS/EMO-1 fields no other extension has."""
    frame = Caravan(
        start="2019-01-01",
        end="2019-01-03",
        variables=["discharge_efas", "precipitation_emo1", "temperature_emo1"],
        lat_lim=[-90.0, 90.0],
        lon_lim=[-180.0, 180.0],
        dataset="spain",
        gauge_ids=["camelses_1080"],
        path=str(tmp_path),
    ).download()

    assert list(frame.columns) == [
        "gauge_id",
        "date",
        "dis_efas5",
        "pr_emo1",
        "ta_emo1",
    ]
    assert frame["dis_efas5"].notna().all()


def test_every_pinned_extension_still_matches_zenodo():
    """A stale pin anywhere in the catalog should fail here, not for a user."""
    from earthlens.caravan._helpers import resolve_record

    catalog = Catalog()
    for key in sorted(catalog.extensions):
        archive = catalog.get_extension(key).resolve_version().file_for("csv")
        published = resolve_record(archive.record)

        assert archive.name in published, f"{key}: {archive.name} is gone"
        assert published[archive.name].md5 == archive.md5, f"{key}: md5 drift"
        assert published[archive.name].size == archive.size, f"{key}: size drift"
