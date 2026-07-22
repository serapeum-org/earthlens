"""Unit tests for the Sentinel Hub Statistical plane → tabular zonal stats (C7)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from earthlens.aggregate import AggregationConfig
from earthlens.sentinel_hub.backend import (
    SentinelHub,
    _flatten_statistics,
    _iter_geometries,
    _stats_frame,
)

pytestmark = pytest.mark.sentinel_hub

_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [[14.0, 40.0], [14.1, 40.0], [14.1, 40.1], [14.0, 40.1], [14.0, 40.0]]
    ],
}


def _stats_backend(
    output_dir, variables=None, geometry=_POLYGON, **kwargs
) -> SentinelHub:
    """A Statistical backend over a small polygon."""
    return SentinelHub(
        start="2020-06-01",
        end="2020-06-02",
        variables=variables or {"sentinel-2-l2a-ndvi-stats": []},
        lat_lim=[40.0, 40.1],
        lon_lim=[14.0, 14.1],
        path=output_dir,
        api="statistical",
        geometry=geometry,
        client_id="a",
        client_secret="b",
        **kwargs,
    )


class TestIterGeometries:
    """`_iter_geometries` normalises the supported geometry shapes."""

    def test_bare_geometry(self):
        """A bare GeoJSON geometry yields one pair keyed 0."""
        assert _iter_geometries(_POLYGON) == [(0, _POLYGON)]

    def test_feature_collection(self):
        """A FeatureCollection yields one pair per feature with its id."""
        fc = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "a", "geometry": _POLYGON},
                {"type": "Feature", "properties": {"id": "b"}, "geometry": _POLYGON},
            ],
        }
        assert [fid for fid, _ in _iter_geometries(fc)] == ["a", "b"]


class TestFlatten:
    """`_flatten_statistics` flattens the nested stats tree."""

    def test_skips_datamask_and_keeps_percentiles(self):
        """The dataMask output is dropped; stats + percentiles are kept."""
        payload = {
            "data": [
                {
                    "interval": {"from": "f", "to": "t"},
                    "outputs": {
                        "ndvi": {
                            "bands": {
                                "B0": {
                                    "stats": {"mean": 0.5},
                                    "percentiles": {"50": 0.5},
                                }
                            }
                        },
                        "dataMask": {"bands": {"B0": {"stats": {"mean": 1.0}}}},
                    },
                }
            ]
        }
        rows = _flatten_statistics(payload, feature_id="x")
        assert len(rows) == 1
        assert rows[0]["output"] == "ndvi"
        assert rows[0]["p50"] == 0.5


class TestEmptyStats:
    """An empty stats result writes a valid header-only CSV (not unparseable)."""

    def test_empty_frame_has_header(self):
        """`_stats_frame([])` is empty but carries the standard columns."""
        frame = _stats_frame([])
        assert frame.empty
        assert "mean" in frame.columns and "feature_id" in frame.columns

    def test_empty_frame_roundtrips(self, tmp_path):
        """A header-only CSV re-reads without error (0 rows)."""
        import pandas as pd

        target = tmp_path / "empty.csv"
        _stats_frame([]).to_csv(target, index=False)
        assert len(pd.read_csv(target)) == 0


class TestStatisticalFetch:
    """The Statistical plane writes a tidy table."""

    def test_requires_geometry(self, fake_sh, output_dir: Path):
        """api='statistical' without geometry= raises a clear error."""
        backend = SentinelHub(
            start="2020-06-01",
            end="2020-06-02",
            variables={"sentinel-2-l2a-ndvi-stats": []},
            lat_lim=[40.0, 40.1],
            lon_lim=[14.0, 14.1],
            path=output_dir,
            api="statistical",
            client_id="a",
            client_secret="b",
        )
        with pytest.raises(ValueError, match="needs geometry"):
            backend.download()

    def test_writes_table(self, fake_sh, output_dir: Path):
        """A statistical download writes one CSV with the flattened rows."""
        paths = _stats_backend(output_dir).download()
        assert len(paths) == 1 and paths[0].suffix == ".csv"
        df = pd.read_csv(paths[0])
        assert {"interval_from", "band", "mean", "p50"} <= set(df.columns)
        assert df.iloc[0]["mean"] == 0.45
        assert "dataMask" not in set(df["output"])

    def test_render_recipe_without_datamask_errors(self, fake_sh, output_dir: Path):
        """A render recipe (no dataMask) on the Statistical plane is rejected."""
        backend = _stats_backend(output_dir, variables={"sentinel-2-l2a-ndvi": []})
        with pytest.raises(ValueError, match="dataMask"):
            backend.download()

    def test_feature_collection_carries_ids(self, fake_sh, output_dir: Path):
        """A FeatureCollection produces one request per feature, ids carried."""
        fc = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": "farm-1", "geometry": _POLYGON},
                {"type": "Feature", "id": "farm-2", "geometry": _POLYGON},
            ],
        }
        backend = _stats_backend(output_dir, geometry=fc)
        paths = backend.download()
        df = pd.read_csv(paths[0])
        assert set(df["feature_id"]) == {"farm-1", "farm-2"}
        assert len(fake_sh.SentinelHubStatistical.instances) == 2

    def test_aggregate_maps_to_interval(self, fake_sh, output_dir: Path):
        """`aggregate=` maps freq → the Statistical aggregation_interval."""
        backend = _stats_backend(output_dir)
        backend.download(aggregate=AggregationConfig(freq="1MS", op="mean"))
        req = fake_sh.SentinelHubStatistical.instances[-1]
        assert req.aggregation["aggregation_interval"] == "P1M"
