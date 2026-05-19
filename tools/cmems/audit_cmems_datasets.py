"""Audit the bundled CMEMS catalog against the live toolbox catalogue.

For every dataset id under `datasets:` in
`src/earthlens/cmems/cmems_data_catalog.yaml`, call
`copernicusmarine.describe(dataset_id=...)` and classify the result:

* `covered` — id resolves; every curated variable is present in the
  toolbox response.
* `partial` — id resolves but at least one curated variable is missing
  from the toolbox's variable list (typo or deprecated short name).
* `missing` — `DatasetNotFound` raised (the id is stale or never
  existed).
* `renamed` — id resolves to a redirect; the toolbox's
  `CopernicusMarineDataset.dataset_id` differs from the requested id.
  The script emits a one-line patch suggestion.

`describe()` does not require credentials — this audit runs anonymous
and is suitable for CI. Pass `--strict` to exit 1 when any row is
partial / missing / renamed, suitable as a catalog-drift gate.

Usage:

    pixi run -e dev python tools/cmems/audit_cmems_datasets.py
    pixi run -e dev python tools/cmems/audit_cmems_datasets.py --strict
    pixi run -e dev python tools/cmems/audit_cmems_datasets.py --format=json

Not part of the installed package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import walk_variables  # noqa: E402

# Reach earthlens.cmems without an editable install when run from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from earthlens.cmems import Catalog  # noqa: E402


_STATUS_ORDER = ("covered", "partial", "renamed", "missing", "error")


def _describe(dataset_id: str) -> tuple[Any | None, str | None]:
    """Call `cm.describe(dataset_id=...)`, returning `(response, error)`.

    Wraps the toolbox call to (a) capture `DatasetNotFound` as a
    typed return rather than a raised exception so the audit can keep
    classifying remaining ids, and (b) pin the import inside the
    function — `copernicusmarine` is an optional dependency and the
    `--help` path should not require it.

    Args:
        dataset_id: CMEMS dataset id to resolve.

    Returns:
        `(response, None)` on success, `(None, error_message)` when
            the toolbox raised. `error_message` is `"<ExceptionType>:
            <message>"`.
    """
    import copernicusmarine as cm

    try:
        return (
            cm.describe(dataset_id=dataset_id, disable_progress_bar=True),
            None,
        )
    except cm.DatasetNotFound as exc:
        return None, f"DatasetNotFound: {exc}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _live_dataset(response: Any, requested_id: str) -> Any | None:
    """Find the CopernicusMarineDataset matching `requested_id` in the response.

    `describe(dataset_id=...)` returns one product per request. The
    toolbox may transparently redirect a deprecated id to its current
    canonical form, in which case the returned dataset's id differs
    from `requested_id`. Both cases are returned (the caller compares
    ids to detect the rename).

    Args:
        response: `CopernicusMarineCatalogue` from `_describe`.
        requested_id: The dataset id that was passed to `describe()`.

    Returns:
        The first `CopernicusMarineDataset` whose `dataset_id` matches
            `requested_id`; failing that, the first dataset in the
            response (the redirect target); or `None` if the response
            has no products / datasets.
    """
    if not response.products:
        return None
    product = response.products[0]
    for ds in product.datasets:
        if ds.dataset_id == requested_id:
            return ds
    return next(iter(product.datasets), None)


def classify(curated: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Classify every curated dataset id against `cm.describe()`.

    Args:
        curated: Mapping from CMEMS dataset id to its curated variable
            short names.

    Returns:
        List of row dicts, one per curated dataset, with keys
            `dataset_id`, `status`, `live_id`, `missing_variables`,
            `error`. Order matches `curated` iteration order; sort by
            `status` then `dataset_id` if you want deterministic
            output.
    """
    rows: list[dict[str, Any]] = []
    for ds_id, curated_vars in curated.items():
        response, error = _describe(ds_id)
        if response is None:
            rows.append(
                {
                    "dataset_id": ds_id,
                    "status": "missing" if error and "DatasetNotFound" in error else "error",
                    "live_id": None,
                    "missing_variables": [],
                    "error": error,
                }
            )
            continue
        live = _live_dataset(response, ds_id)
        if live is None:
            rows.append(
                {
                    "dataset_id": ds_id,
                    "status": "error",
                    "live_id": None,
                    "missing_variables": [],
                    "error": "describe() returned no datasets",
                }
            )
            continue
        if live.dataset_id != ds_id:
            rows.append(
                {
                    "dataset_id": ds_id,
                    "status": "renamed",
                    "live_id": live.dataset_id,
                    "missing_variables": [],
                    "error": None,
                }
            )
            continue
        live_vars = {v.short_name for v in walk_variables(live)}
        missing = sorted(set(curated_vars) - live_vars)
        rows.append(
            {
                "dataset_id": ds_id,
                "status": "covered" if not missing else "partial",
                "live_id": live.dataset_id,
                "missing_variables": missing,
                "error": None,
            }
        )
    return rows


def emit_markdown(rows: list[dict[str, Any]]) -> str:
    """Render `rows` as a Markdown coverage table.

    The table is ordered by `(status, dataset_id)` with `covered`
    rows first so a maintainer scanning the output sees drift at the
    bottom rather than buried in the green rows.

    Args:
        rows: Output of :func:`classify`.

    Returns:
        Markdown text, ending in a newline.
    """
    order = {name: i for i, name in enumerate(_STATUS_ORDER)}
    sorted_rows = sorted(
        rows, key=lambda r: (order.get(r["status"], 99), r["dataset_id"])
    )
    out = [
        "| Dataset ID | Status | Detail |",
        "|---|---|---|",
    ]
    for r in sorted_rows:
        if r["status"] == "covered":
            detail = "OK"
        elif r["status"] == "partial":
            detail = f"missing variables: {', '.join(r['missing_variables'])}"
        elif r["status"] == "renamed":
            detail = f"renamed to `{r['live_id']}`"
        elif r["status"] == "missing":
            detail = f"`DatasetNotFound`"
        else:
            detail = r.get("error") or "unknown error"
        out.append(f"| `{r['dataset_id']}` | {r['status']} | {detail} |")
    out.append("")
    return "\n".join(out)


def emit_json(rows: list[dict[str, Any]]) -> str:
    """Render `rows` as JSON (sorted by status then dataset_id)."""
    order = {name: i for i, name in enumerate(_STATUS_ORDER)}
    sorted_rows = sorted(
        rows, key=lambda r: (order.get(r["status"], 99), r["dataset_id"])
    )
    return json.dumps(sorted_rows, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry — return 0 on full coverage, 1 on drift (with `--strict`)."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 if any row is partial / missing / renamed / errored",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="output format (default: markdown)",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="path to cmems_data_catalog.yaml (default: bundled)",
    )
    args = parser.parse_args(argv)

    try:
        cat = Catalog.load(args.catalog) if args.catalog else Catalog()
    except (FileNotFoundError, ValueError) as exc:
        print(f"could not load the CMEMS catalog: {exc}", file=sys.stderr)
        return 1

    curated = {
        ds_id: sorted(ds.variables.keys())
        for ds_id, ds in cat.datasets.items()
    }
    if not curated:
        print("catalog has no datasets — nothing to audit", file=sys.stderr)
        return 1

    rows = classify(curated)
    if args.format == "markdown":
        sys.stdout.write(emit_markdown(rows))
    else:
        sys.stdout.write(emit_json(rows))

    if args.strict:
        bad = [r for r in rows if r["status"] != "covered"]
        if bad:
            print(
                f"\n--strict: {len(bad)} row(s) failed coverage",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
