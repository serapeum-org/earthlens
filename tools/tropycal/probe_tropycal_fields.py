"""Probe tropycal to discover the live `Storm.to_dataframe()` field schema.

Loads one `tropycal.tracks.TrackDataset(basin, source)`, samples a few
storms from a season, unions their `to_dataframe(attrs_as_columns=True)`
columns, and writes a JSON sidecar recording each field's dtype + an
example value plus whether tropycal exposes a Saffir-Simpson `category`
column (it does not in 1.4, so `earthlens.tropycal.events` derives it
from `vmax`). The sidecar seeds / verifies the catalog `fields:` maps and
the `events.py` schema.

This is the Tropycal analog of `tools/ecmwf/probe_cds_netcdf.py`. It is a
maintainer tool, not part of the installed package, and requires the
`[tropycal]` extra (`pip install earthlens[tropycal]`).

Usage:

    pixi run -e dev python tools/tropycal/probe_tropycal_fields.py \
        --basin north_atlantic --source hurdat --year 2005 \
        --out C:/tmp/tropycal_probe/north_atlantic.json

The first load downloads + parses the whole basin best-track file and can
be slow (seconds for HURDAT, longer for IBTrACS).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


def probe_fields(
    basin: str, source: str, year: int, num_storms: int
) -> dict[str, Any]:
    """Load a basin/season and summarise its `to_dataframe()` schema.

    Args:
        basin: tropycal basin code (`"north_atlantic"`, …).
        source: tropycal data source (`"ibtracs"`/`"hurdat"`).
        year: Season (calendar year) to sample.
        num_storms: Maximum number of storms to sample from the season.

    Returns:
        A summary dict: `basin`, `source`, `year`, `storms_sampled`,
        `has_category_column`, and `fields` (column -> {dtype, example}).

    Raises:
        ImportError: If the `[tropycal]` extra is not installed.
    """
    try:
        import tropycal.tracks as tracks
    except ImportError as exc:  # pragma: no cover - tool-only path
        raise ImportError(
            "probe_tropycal_fields needs the `tropycal` package. Install it "
            "with `pip install earthlens[tropycal]`."
        ) from exc

    track_dataset = tracks.TrackDataset(basin=basin, source=source)
    storm_ids = list(track_dataset.get_season(year).summary().get("id") or [])[:num_storms]

    fields: dict[str, dict[str, str]] = {}
    has_category = False
    for storm_id in storm_ids:
        frame = track_dataset.get_storm(storm_id).to_dataframe(attrs_as_columns=True)
        has_category = has_category or ("category" in frame.columns)
        for column in frame.columns:
            if column in fields:
                continue
            example = frame[column].dropna()
            fields[column] = {
                "dtype": str(frame[column].dtype),
                "example": _stringify(example.iloc[0]) if len(example) else None,
            }

    return {
        "basin": basin,
        "source": source,
        "year": year,
        "storms_sampled": len(storm_ids),
        "has_category_column": has_category,
        "fields": dict(sorted(fields.items())),
    }


def _stringify(value: object) -> str:
    """Render a sample cell value as a short string for the JSON sidecar."""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: probe one basin/season and write the JSON sidecar.

    Args:
        argv: Optional argument list (defaults to `sys.argv`).

    Returns:
        Process exit code (`0` on success).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--basin", default="north_atlantic", help="tropycal basin code")
    parser.add_argument(
        "--source", default="hurdat", choices=["ibtracs", "hurdat"], help="data source"
    )
    parser.add_argument("--year", type=int, default=2005, help="season (calendar year)")
    parser.add_argument(
        "--num-storms", type=int, default=5, help="storms to sample from the season"
    )
    parser.add_argument("--out", type=Path, default=None, help="JSON sidecar path")
    args = parser.parse_args(argv)

    summary = probe_fields(args.basin, args.source, args.year, args.num_storms)
    text = json.dumps(summary, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry
    raise SystemExit(main())
