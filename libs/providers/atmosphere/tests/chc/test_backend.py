from __future__ import annotations

import glob
import os
import shutil
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pyramids.dataset import Dataset, GeoReference

from earthlens.aggregate import AggregationConfig
from earthlens.chc import CHIRPS

pytestmark = [pytest.mark.chc]


def test_chirps_declares_raster_output_kind():
    """CHIRPS declares OUTPUT_KIND='raster' so the facade forwards aggregate=."""
    assert CHIRPS.OUTPUT_KIND == "raster"


def test_chirps_supports_aggregate():
    """CHIRPS now wires the raster reducer, so the gate accepts aggregate=."""
    assert CHIRPS.SUPPORTS_AGGREGATE is True
    # The class no longer overrides the base's (empty) refusal reason.
    assert "AGGREGATE_REFUSAL_REASON" not in CHIRPS.__dict__


def _raise_ftp_down(*_args, **_kwargs):
    """Stand in for `_download_dataset` and always fail.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError("ftp down")


def _build_daily_chirps(tmp_path, start="2020-01-01", end="2020-01-03"):
    """A daily CHIRPS backend over a short window and a small AOI."""
    return CHIRPS(
        start=start,
        end=end,
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )


def _write_per_date_rasters(chirps, ds_key, values):
    """Write one 2x2 GeoTIFF per overlapping date, each filled with `values[i]`.

    Stands in for what the FTP path would leave on disk — canonical per-date
    filenames and the -9999 nodata CHIRPS normalises to — so the aggregation
    path can be exercised with no network.
    """
    dataset = chirps._catalog.datasets[ds_key]
    var = dataset.variables["precipitation"]
    dates = chirps._overlapping_dates(ds_key, dataset)
    geo = (-75.0, 0.5, 0.0, 5.0, 0.0, -0.5)
    paths = []
    for value, date in zip(values, dates):
        target = chirps.root_dir / chirps._output_filename(ds_key, dataset, var, date)
        Dataset.from_array(
            np.full((2, 2), float(value)),
            no_data_value=-9999.0,
            geo_ref=GeoReference(geo=geo, epsg=4326),
        ).to_file(str(target))
        paths.append(target)
    return dataset, var, paths


class TestAggregate:
    """CHIRPS re-aggregates its per-date GeoTIFFs when aggregate= is passed."""

    def test_sums_daily_flux_to_monthly(self, tmp_path):
        """auto op on a flux variable sums the daily stack into one window."""
        chirps = _build_daily_chirps(tmp_path)
        dataset, var, paths = _write_per_date_rasters(
            chirps, "global-daily", [1.0, 2.0, 3.0]
        )
        out = chirps._aggregate_pair(
            "global-daily",
            dataset,
            var,
            paths,
            AggregationConfig(freq="1MS", op="auto"),
        )
        assert len(out) == 1
        assert np.allclose(Dataset.read_file(str(out[0])).read_array(), 6.0)

    def test_download_writes_window_rasters_to_aggregated_dir(
        self, tmp_path, monkeypatch
    ):
        """download(aggregate=) lands the reduction under <root_dir>/aggregated/."""
        chirps = _build_daily_chirps(tmp_path)
        _, _, paths = _write_per_date_rasters(
            chirps, "global-daily", [10.0, 20.0, 30.0]
        )
        monkeypatch.setattr(chirps, "_download_dataset", lambda *a, **k: paths)

        out = chirps.download(
            progress_bar=False, aggregate=AggregationConfig(freq="1MS", op="mean")
        )

        assert len(out) == 1
        assert out[0].parent == chirps.root_dir / "aggregated"
        assert out[0].name == "global-daily_precipitation_mean_1MS_20200101.tif"
        assert np.allclose(Dataset.read_file(str(out[0])).read_array(), 20.0)

    def test_explicit_op_overrides_auto(self, tmp_path):
        """An explicit op is used verbatim, not the flux default."""
        chirps = _build_daily_chirps(tmp_path)
        dataset, var, paths = _write_per_date_rasters(
            chirps, "global-daily", [1.0, 5.0, 3.0]
        )
        out = chirps._aggregate_pair(
            "global-daily", dataset, var, paths, AggregationConfig(freq="1MS", op="max")
        )
        assert np.allclose(Dataset.read_file(str(out[0])).read_array(), 5.0)

    def test_honours_explicit_out_dir(self, tmp_path):
        """config.out_dir wins over the aggregated/ default."""
        chirps = _build_daily_chirps(tmp_path)
        dataset, var, paths = _write_per_date_rasters(
            chirps, "global-daily", [2.0, 2.0, 2.0]
        )
        target_dir = tmp_path / "monthly"
        out = chirps._aggregate_pair(
            "global-daily",
            dataset,
            var,
            paths,
            AggregationConfig(freq="1MS", op="mean", out_dir=target_dir),
        )
        assert out[0].parent == target_dir

    def test_rejects_discrete_dataset(self, tmp_path):
        """A discrete multi-year archive dataset has no per-date stack to reduce."""
        chirps = CHIRPS(
            variables={"chpclim-v2-monthly": ["precipitation"]},
            lat_lim=[4.0, 5.0],
            lon_lim=[-75.0, -74.0],
            path=str(tmp_path),
        )
        dataset = chirps._catalog.datasets["chpclim-v2-monthly"]
        var = dataset.variables["precipitation"]
        with pytest.raises(ValueError, match="fixed multi-year archive files"):
            chirps._aggregate_pair(
                "chpclim-v2-monthly", dataset, var, [], AggregationConfig(freq="1MS")
            )

    def test_rejects_mismatched_grids(self, tmp_path):
        """Per-date rasters that differ in shape cannot be stacked."""
        chirps = _build_daily_chirps(tmp_path, end="2020-01-02")
        dataset = chirps._catalog.datasets["global-daily"]
        var = dataset.variables["precipitation"]
        dates = chirps._overlapping_dates("global-daily", dataset)
        geo = (-75.0, 0.5, 0.0, 5.0, 0.0, -0.5)
        paths = []
        for shape, date in zip([(2, 2), (3, 3)], dates):
            target = chirps.root_dir / chirps._output_filename(
                "global-daily", dataset, var, date
            )
            Dataset.from_array(
                np.ones(shape),
                no_data_value=-9999.0,
                geo_ref=GeoReference(geo=geo, epsg=4326),
            ).to_file(str(target))
            paths.append(target)
        with pytest.raises(ValueError, match="differ in shape"):
            chirps._aggregate_pair(
                "global-daily", dataset, var, paths, AggregationConfig(freq="1MS")
            )

    def test_empty_paths_yields_nothing_and_creates_no_dir(self, tmp_path):
        """No written per-date rasters means no window output and no aggregated dir."""
        chirps = _build_daily_chirps(tmp_path)
        dataset = chirps._catalog.datasets["global-daily"]
        var = dataset.variables["precipitation"]
        out_dir = tmp_path / "aggregated"
        out = chirps._aggregate_pair(
            "global-daily",
            dataset,
            var,
            [],
            AggregationConfig(freq="1MS", out_dir=out_dir),
        )
        assert out == []
        assert not out_dir.exists()

    def test_resolve_op_routes_by_variable_kind(self):
        """auto sums flux and averages state; an explicit op passes through."""
        flux = SimpleNamespace(is_flux=True)
        state = SimpleNamespace(is_flux=False)
        assert CHIRPS._resolve_aggregate_op("auto", flux) == "sum"
        assert CHIRPS._resolve_aggregate_op("auto", state) == "mean"
        assert CHIRPS._resolve_aggregate_op("min", flux) == "min"

    def test_daily_freq_writes_one_window_per_day(self, tmp_path):
        """A 1D freq streams one reduced raster per day without stacking all."""
        chirps = _build_daily_chirps(tmp_path)
        dataset, var, paths = _write_per_date_rasters(
            chirps, "global-daily", [1.0, 2.0, 3.0]
        )
        out = chirps._aggregate_pair(
            "global-daily", dataset, var, paths, AggregationConfig(freq="1D", op="mean")
        )
        assert len(out) == 3
        values = [
            float(Dataset.read_file(str(path)).read_array()[0, 0]) for path in out
        ]
        assert values == [1.0, 2.0, 3.0]

    def test_all_nodata_sum_with_min_count_stays_nodata(self, tmp_path):
        """min_count keeps an all-missing flux window as nodata, not 0.0."""
        chirps = _build_daily_chirps(tmp_path)
        dataset, var, paths = _write_per_date_rasters(
            chirps, "global-daily", [-9999.0, -9999.0, -9999.0]
        )
        out = chirps._aggregate_pair(
            "global-daily",
            dataset,
            var,
            paths,
            AggregationConfig(freq="1MS", op="sum", min_count=1),
        )
        assert np.allclose(Dataset.read_file(str(out[0])).read_array(), -9999.0)

    def test_window_filename_stamps_time_only_for_subdaily_freq(self):
        """A sub-daily freq widens the stamp to HHMM; daily-or-coarser does not."""
        var = SimpleNamespace(name="precipitation")
        label = pd.Timestamp("2020-01-01 06:00")
        subdaily = CHIRPS._window_filename(
            "global-daily", var, "mean", AggregationConfig(freq="6h"), label
        )
        monthly = CHIRPS._window_filename(
            "global-daily", var, "sum", AggregationConfig(freq="1MS"), label
        )
        assert subdaily == "global-daily_precipitation_mean_6h_202001010600.tif"
        assert monthly == "global-daily_precipitation_sum_1MS_20200101.tif"


def test_chirps_catalog_property_aliases_private(tmp_path):
    """The public `catalog` attribute is a back-compat alias of `_catalog`."""
    chirps = CHIRPS(
        start="2020-01-01",
        end="2020-01-02",
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )
    assert chirps.catalog is chirps._catalog


def test_chirps_download_returns_written_paths(tmp_path, monkeypatch):
    """download() returns the GeoTIFF paths collected from _download_dataset."""
    chirps = CHIRPS(
        start="2020-01-01",
        end="2020-01-02",
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )
    fake = [tmp_path / "a.tif", tmp_path / "b.tif"]
    monkeypatch.setattr(chirps, "_download_dataset", lambda *a, **k: fake)

    result = chirps.download(progress_bar=False)

    assert result == fake


def test_chirps_download_errors_raise_propagates(tmp_path, monkeypatch):
    """errors="raise" surfaces the per-variable failure instead of logging it."""
    chirps = CHIRPS(
        start="2020-01-01",
        end="2020-01-02",
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )
    monkeypatch.setattr(chirps, "_download_dataset", _raise_ftp_down)

    with pytest.raises(RuntimeError, match="ftp down"):
        chirps.download(progress_bar=False, errors="raise")


def test_chirps_download_errors_warn_keeps_going(tmp_path, monkeypatch):
    """The default policy drops the failed variable and returns the rest."""
    chirps = CHIRPS(
        start="2020-01-01",
        end="2020-01-02",
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )
    monkeypatch.setattr(chirps, "_download_dataset", _raise_ftp_down)

    assert chirps.download(progress_bar=False) == []


def test_chirps_download_rejects_an_unknown_errors_policy(tmp_path):
    """An unrecognised errors= value is refused before any request."""
    chirps = CHIRPS(
        start="2020-01-01",
        end="2020-01-02",
        variables=["precipitation"],
        temporal_resolution="daily",
        lat_lim=[4.0, 5.0],
        lon_lim=[-75.0, -74.0],
        path=str(tmp_path),
    )
    with pytest.raises(ValueError, match="errors"):
        chirps.download(progress_bar=False, errors="explode")


@pytest.fixture(scope="module")
def test_create_chirps_object(
    dates: list,
    daily_temporal_resolution: str,
    chirps_variables: list[str],
    lat_bounds: list,
    lon_bounds: list,
    chirps_base_dir: str,
):
    coello = CHIRPS(
        start=dates[0],
        end=dates[1],
        lat_lim=lat_bounds,
        lon_lim=lon_bounds,
        variables=chirps_variables,
        temporal_resolution=daily_temporal_resolution,
        path=chirps_base_dir,
    )
    assert coello.api_url == "data.chc.ucsb.edu"
    # Legacy list-shape `variables` is normalized to the catalog dict shape.
    assert coello.vars == {"global-daily": ["precipitation"]}
    # `self.time` carries the outer window; per-dataset frequencies live
    # in the catalog (`Dataset.pandas_freq`) and are resolved per call.
    assert str(coello.time.start_date.date()) == dates[0]
    assert str(coello.time.end_date.date()) == dates[1]

    return coello


@pytest.mark.e2e
def test_download(
    test_create_chirps_object: CHIRPS,
    chirps_base_dir: str,
    number_downloaded_files: int,
):
    test_create_chirps_object.download()

    # New filename scheme is `<dataset-key>_<variable>_<date>.tif`.
    filelist = glob.glob(
        os.path.join(f"{chirps_base_dir}", "global-daily_precipitation_*.tif")
    )
    assert len(filelist) == number_downloaded_files
    # delete the files
    try:
        shutil.rmtree(f"{chirps_base_dir}")
    except PermissionError:
        print("the downloaded files could not be deleted")
