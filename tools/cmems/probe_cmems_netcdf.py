"""Probe CMEMS variables to discover their NetCDF short name and units.

Submits a single `copernicusmarine.subset()` per `(dataset, variables)`
batch on a tiny 1deg x 1deg x 1day window, opens the returned NetCDF
via `pyramids.netcdf.NetCDF`, and writes a JSON sidecar mapping
`(dataset_id, variable)` -> `{long_name, units, nc_short_name}`.

The C2 refresh tool can already emit a humanised guess at `long_name`
from `describe()`'s `standard_name`, but those guesses do not always
match what `pyramids.netcdf.NetCDF` will actually read off the file
(some CMEMS NetCDFs carry richer `long_name` strings, some carry
none at all). The aggregator (`earthlens.aggregate.aggregate_netcdf`,
once `PY-L` lands) keys off the on-disk `long_name` / `unit`
attributes, so mismatches between the curated YAML and the real file
surface as silent `KeyError`s deep inside the loop. This probe is the
only way to know which curated rows would survive the live read.

Usage:

    pixi run -e dev python tools/cmems/probe_cmems_netcdf.py \\
        --dataset cmems_mod_glo_phy_my_0.083deg_P1D-m \\
        --variables thetao,so,uo,vo,zos \\
        --out C:/tmp/cmems_probe/glo_phy.json

    # Walk every curated (dataset, variable) pair in the bundled catalog
    # and write one sidecar per dataset under --out-dir:
    pixi run -e dev python tools/cmems/probe_cmems_netcdf.py \\
        --all-curated --out-dir C:/tmp/cmems_probe/

Cached NetCDFs land under `C:/tmp/cmems_probe/<safe_dataset_id>.nc`
so re-runs avoid re-queuing the toolbox. Failures on individual
`(dataset, variable)` pairs are captured into the JSON sidecar
(`error: <ExceptionType>: <message>`) rather than aborting the batch
- the goal is a complete coverage report, not a fast-fail.

Requires Copernicus Marine credentials (env vars
`COPERNICUSMARINE_SERVICE_USERNAME` / `COPERNICUSMARINE_SERVICE_PASSWORD`,
or a saved configuration directory from a previous
`copernicusmarine login`).

Not part of the installed package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import CATALOG_PATH  # noqa: E402

DEFAULT_CACHE_DIR = Path("C:/tmp/cmems_probe")
DEFAULT_PROBE_DATE = "2020-01-01"
DEFAULT_BBOX = (0.0, 1.0, 0.0, 1.0)
DEFAULT_DEPTHS = (0.0, 5.0)

_COORD_VAR_NAMES = frozenset(
    {
        "latitude",
        "lat",
        "longitude",
        "lon",
        "time",
        "valid_time",
        "depth",
        "elevation",
        "x",
        "y",
        "crs",
        "spatial_ref",
        "bnds",
        "climatology_bounds",
        "time_bnds",
        "depth_bnds",
        "lat_bnds",
        "lon_bnds",
    }
)


def safe_filename(dataset_id: str) -> str:
    """Return a Windows-safe variant of `dataset_id` for use as a filename.

    CMEMS dataset ids embed `.` and sometimes `/` segments that are
    legal on POSIX but confusing in `glob` patterns and illegal on
    Windows. Replace the offending characters with `_`.

    Args:
        dataset_id: The raw CMEMS dataset id.

    Returns:
        A filename-safe variant of `dataset_id`.
    """
    safe = dataset_id
    for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|", "."):
        safe = safe.replace(bad, "_")
    return safe


def fetch_one(
    dataset_id: str,
    variables: list[str],
    target: Path,
    *,
    probe_date: str = DEFAULT_PROBE_DATE,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    depth_range: tuple[float, float] | None = DEFAULT_DEPTHS,
    credentials_file: Path | None = None,
) -> Path:
    """Submit one tiny `copernicusmarine.subset()` and return the file path.

    The probe window is 1deg x 1deg x 1day so the returned NetCDF is
    tens of kilobytes — small enough to fetch all 30-odd curated
    variables in seconds. Depth-bearing datasets (physics,
    biogeochem) get `minimum_depth` / `maximum_depth` clipped to the
    surface 0-5m by default; pass `depth_range=None` to drop the
    depth selectors entirely for surface-only datasets (SST,
    altimetry).

    Args:
        dataset_id: CMEMS dataset id.
        variables: Variable short names to request.
        target: Output path for the cached NetCDF.
        probe_date: ISO date used for both `start_datetime` and
            `end_datetime`. Defaults to `"2020-01-01"` because every
            curated dataset covers that day.
        bbox: `(west, east, south, north)` in degrees. Defaults to
            a 1deg x 1deg window at the equator / prime meridian; safe
            for global datasets, may need adjusting per-region.
        depth_range: `(minimum_depth, maximum_depth)` in metres, or
            `None` to omit. Defaults to `(0.0, 5.0)`.
        credentials_file: Optional path to a saved
            `.copernicusmarine-credentials` file. `None` falls back
            to env vars / the toolbox config directory.

    Returns:
        `target` if the file already existed (cached), or after a
            successful `subset()` call.

    Raises:
        copernicusmarine.DatasetNotFound: Dataset id is invalid.
        copernicusmarine.VariableDoesNotExistInTheDataset: One of the
            variable names is not in the dataset.
    """
    if target.exists():
        return target
    import copernicusmarine as cm

    target.parent.mkdir(parents=True, exist_ok=True)
    west, east, south, north = bbox
    kwargs: dict[str, object] = dict(
        dataset_id=dataset_id,
        variables=list(variables),
        minimum_longitude=west,
        maximum_longitude=east,
        minimum_latitude=south,
        maximum_latitude=north,
        start_datetime=probe_date,
        end_datetime=probe_date,
        output_filename=target.name,
        output_directory=str(target.parent),
        disable_progress_bar=True,
        overwrite=False,
        file_format="netcdf",
    )
    if depth_range is not None:
        kwargs["minimum_depth"] = depth_range[0]
        kwargs["maximum_depth"] = depth_range[1]
    if credentials_file is not None:
        kwargs["credentials_file"] = str(credentials_file)
    cm.subset(**kwargs)
    return target


def collect_metadata(nc_path: Path) -> dict[str, dict[str, str]]:
    """Walk `nc_path` and collect `long_name` + `unit` per data variable.

    Coordinate variables (time / lat / lon / depth, and their bounds)
    are skipped — the catalog only carries data-variable rows. The
    `nc_short_name` field echoes the key so users grepping the
    sidecar can confirm the variable was actually present in the
    file (a row missing from the sidecar means the toolbox dropped
    the request or the variable was renamed mid-download).

    Args:
        nc_path: Path to a NetCDF file written by
            `copernicusmarine.subset()`.

    Returns:
        Mapping from variable short name to a `{long_name, units,
            nc_short_name}` dict.
    """
    from pyramids.netcdf import NetCDF

    out: dict[str, dict[str, str]] = {}
    with NetCDF.read_file(str(nc_path), read_only=True) as fh:
        for name, var in fh.meta_data.variables.items():
            if name in _COORD_VAR_NAMES:
                continue
            long_name = getattr(var, "long_name", "") or ""
            units = getattr(var, "unit", "") or ""
            out[name] = {
                "long_name": long_name,
                "units": units,
                "nc_short_name": name,
            }
    return out


def probe_one_dataset(
    dataset_id: str,
    variables: list[str],
    cache_dir: Path,
    *,
    probe_date: str = DEFAULT_PROBE_DATE,
    bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
    depth_range: tuple[float, float] | None = DEFAULT_DEPTHS,
    credentials_file: Path | None = None,
) -> dict[str, dict[str, str]]:
    """Probe one `(dataset, variables)` pair and return the metadata sidecar.

    Wraps `fetch_one` + `collect_metadata` with structured error
    capture: any toolbox-side exception (`DatasetNotFound`,
    `VariableDoesNotExistInTheDataset`,
    `CoordinatesOutOfDatasetBounds`, network) is recorded as
    `{error: "<ExceptionType>: <message>"}` against the dataset id
    rather than aborting the run.

    Args:
        dataset_id: CMEMS dataset id.
        variables: Variable short names to request.
        cache_dir: Directory under which the cached NetCDF lives.
        probe_date: Forwarded to :func:`fetch_one`.
        bbox: Forwarded to :func:`fetch_one`.
        depth_range: Forwarded to :func:`fetch_one`.
        credentials_file: Forwarded to :func:`fetch_one`.

    Returns:
        Either the per-variable metadata dict from
            :func:`collect_metadata`, or a single-entry dict
            `{"__error__": {"error": "<ExceptionType>: <message>"}}`
            when the probe failed.
    """
    nc_target = cache_dir / f"{safe_filename(dataset_id)}.nc"
    try:
        fetch_one(
            dataset_id,
            variables,
            nc_target,
            probe_date=probe_date,
            bbox=bbox,
            depth_range=depth_range,
            credentials_file=credentials_file,
        )
    except Exception as exc:  # noqa: BLE001 - tool: capture into sidecar
        return {"__error__": {"error": f"{type(exc).__name__}: {exc}"}}
    try:
        return collect_metadata(nc_target)
    except Exception as exc:  # noqa: BLE001 - tool: capture into sidecar
        return {"__error__": {"error": f"{type(exc).__name__}: {exc}"}}


def _curated_dataset_variables() -> dict[str, list[str]]:
    """Return `{dataset_id: [variable_short_name, ...]}` from the bundled YAML.

    Mirrors `Catalog().datasets` without round-tripping through the
    pydantic models — this script is read-only against the YAML and
    needs only the variable short names per dataset.

    Returns:
        Mapping from every curated dataset id to its list of variable
            short names.
    """
    import yaml

    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    datasets = raw.get("datasets") or {}
    return {
        ds_id: sorted((ds_body or {}).get("variables", {}).keys())
        for ds_id, ds_body in datasets.items()
    }


def _parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [float(p.strip()) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"--bbox must be 'west,east,south,north' (4 floats); got {text!r}"
        )
    return parts[0], parts[1], parts[2], parts[3]


def _parse_depth(text: str) -> tuple[float, float] | None:
    if text.lower() in {"none", "null", "skip"}:
        return None
    parts = [float(p.strip()) for p in text.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"--depth must be 'min,max' or 'none'; got {text!r}"
        )
    return parts[0], parts[1]


def main(argv: list[str] | None = None) -> int:
    """CLI entry: return 0 on full success, 1 if any probe failed."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dataset",
        help="probe a single CMEMS dataset_id (pair with --variables)",
    )
    mode.add_argument(
        "--all-curated",
        action="store_true",
        help="probe every (dataset, variable) pair in cmems_data_catalog.yaml",
    )
    parser.add_argument(
        "--variables",
        help="comma-separated variable short names (single-dataset mode)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="output JSON path (single-dataset mode)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="output directory for per-dataset JSON sidecars (--all-curated mode)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"directory for cached probe NetCDFs (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--probe-date",
        default=DEFAULT_PROBE_DATE,
        help=f"ISO date for the 1-day probe (default: {DEFAULT_PROBE_DATE})",
    )
    parser.add_argument(
        "--bbox",
        type=_parse_bbox,
        default=DEFAULT_BBOX,
        help="'west,east,south,north' in degrees (default: 0,1,0,1)",
    )
    parser.add_argument(
        "--depth",
        type=_parse_depth,
        default=DEFAULT_DEPTHS,
        help="'min,max' in metres or 'none' to omit (default: 0,5)",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        help="explicit path to a saved .copernicusmarine-credentials file",
    )
    args = parser.parse_args(argv)

    if args.dataset is not None:
        if not args.variables:
            parser.error("--dataset requires --variables")
        if args.out is None:
            parser.error("--dataset requires --out")
        variables = [v.strip() for v in args.variables.split(",") if v.strip()]
        sidecar = probe_one_dataset(
            args.dataset,
            variables,
            args.cache_dir,
            probe_date=args.probe_date,
            bbox=args.bbox,
            depth_range=args.depth,
            credentials_file=args.credentials_file,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        ok = "__error__" not in sidecar
        print(
            f"Wrote {len(sidecar)} entries to {args.out}"
            f"{' (FAILED)' if not ok else ''}"
        )
        return 0 if ok else 1

    if args.out_dir is None:
        parser.error("--all-curated requires --out-dir")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    curated = _curated_dataset_variables()
    failures: list[str] = []
    for ds_id, vars_ in curated.items():
        if not vars_:
            print(f"  skip {ds_id}: no curated variables")
            continue
        sidecar = probe_one_dataset(
            ds_id,
            vars_,
            args.cache_dir,
            probe_date=args.probe_date,
            bbox=args.bbox,
            depth_range=args.depth,
            credentials_file=args.credentials_file,
        )
        out_path = args.out_dir / f"{safe_filename(ds_id)}.json"
        out_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        if "__error__" in sidecar:
            failures.append(ds_id)
            print(f"  FAIL {ds_id}: {sidecar['__error__']['error']}")
        else:
            print(f"  ok   {ds_id}: {len(sidecar)} variables")
    if failures:
        print(f"\n{len(failures)} dataset probe(s) failed: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
