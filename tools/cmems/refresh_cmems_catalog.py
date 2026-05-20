"""Maintain the bundled CMEMS data catalog.

A single `argparse` subcommand CLI that mirrors the role of
`tools/gee/refresh_gee_catalog.py` and
`tools/ecmwf/refresh_available_datasets.py` for the Copernicus Marine
backend. Run with no args to see the subcommand list:

    pixi run -e dev python tools/cmems/refresh_cmems_catalog.py --help

Subcommands:

* `refresh` — call `copernicusmarine.describe()`, walk the returned
  `CopernicusMarineCatalogue.products`, and rewrite the
  `available_products:` block in `src/earthlens/cmems/
  cmems_data_catalog.yaml` in place. The `datasets:` curated map below
  is left untouched. Optional `--with-datasets <product_id> ...`
  prints a ready-to-paste `datasets:` stanza for every dataset under
  each listed product (walks the nested `versions[].parts[].
  services[].variables[]` to seed variable rows).
* `add-ids <dataset_id> ...` — fetch one `--with-datasets`-style
  stanza per id, run it through `compact`, append to the YAML's
  `datasets:` block, and reload via `Catalog.load()` so a malformed
  stanza fails the run loudly. Skips already-curated ids.
* `compact` — read raw `refresh --with-datasets` output on stdin and
  write the catalog's terser style on stdout (drops scratch markers,
  collapses blank lines, normalises line endings).

Exits 0 on success, 1 on any toolbox / parse / I/O error.
Not part of the installed package.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (  # noqa: E402
    CATALOG_DIR,
    INDEX_PATH,
    append_stanzas_to_datasets_block,
    catalog_file_for,
    compact_text,
    emit_dataset_stanza,
    render_available_datasets_block,
    splice_available_datasets,
)


def _cmd_refresh(args: argparse.Namespace) -> int:
    """Walk `cm.describe()`, rewrite `available_datasets:`, optionally emit stanzas.

    Implements the `refresh` subcommand. Calls
    `copernicusmarine.describe(disable_progress_bar=True)` to get the
    full live catalogue, collects every dataset id across every
    product, and replaces the `available_datasets:` block in
    `catalog/_index.yaml` in place. When `args.with_datasets` is
    non-empty, also writes one ready-to-paste `datasets:` stanza per
    dataset under each listed product to stdout.

    Args:
        args: Parsed argparse namespace. Honours `args.index`
            (`Path` to `catalog/_index.yaml`), `args.dry_run`
            (`bool` — when `True` the new block is printed instead
            of written), and `args.with_datasets`
            (`list[str] | None` of product ids).

    Returns:
        `0` on success, `1` on any toolbox- or splice-level failure.
        Per-product errors when emitting stanzas are logged to stderr
        and skipped without changing the return code.

    Examples:
        - Typical invocation as a subcommand:

            `pixi run -e dev python tools/cmems/refresh_cmems_catalog.py refresh`

          rewrites `available_datasets:` in `_index.yaml` in place.

        - Dry-run with stanza emission:

            `... refresh --dry-run --with-datasets GLOBAL_MULTIYEAR_PHY_001_030`

          prints the block + the per-dataset stanzas to stdout.
    """
    import copernicusmarine as cm

    try:
        catalogue = cm.describe(disable_progress_bar=True)
    except Exception as exc:  # noqa: BLE001 - tool: surface and exit non-zero
        print(f"error fetching CMEMS catalogue: {exc}", file=sys.stderr)
        return 1

    dataset_ids = sorted(
        {d.dataset_id for p in catalogue.products for d in p.datasets}
    )
    print(f"collected {len(dataset_ids)} dataset ids from describe()")

    block = render_available_datasets_block(dataset_ids)
    if args.dry_run:
        sys.stdout.write(block)
    else:
        text = args.index.read_text(encoding="utf-8")
        try:
            new_text = splice_available_datasets(text, block)
        except ValueError as exc:
            print(f"error rewriting {args.index}: {exc}", file=sys.stderr)
            return 1
        if new_text == text:
            print(f"No changes — {args.index} already up to date.")
        else:
            args.index.write_text(new_text, encoding="utf-8")
            print(f"updated {args.index}")

    if args.with_datasets:
        for pid in args.with_datasets:
            try:
                resp = cm.describe(product_id=pid, disable_progress_bar=True)
            except Exception as exc:  # noqa: BLE001
                print(f"# error fetching product {pid!r}: {exc}", file=sys.stderr)
                continue
            if not resp.products:
                print(f"# describe(product_id={pid!r}) returned no products", file=sys.stderr)
                continue
            product = resp.products[0]
            sys.stdout.write(f"\n# ---- paste under `datasets:` ----\n")
            sys.stdout.write(f"# product: {pid} ({len(product.datasets)} datasets)\n")
            for ds in product.datasets:
                sys.stdout.write(emit_dataset_stanza(product, ds))
    return 0


def _cmd_add_ids(args: argparse.Namespace) -> int:
    """Append one stanza per dataset_id to the YAML, then re-parse to verify.

    Implements the `add-ids` subcommand. For each id in
    `args.dataset_ids` that is not already curated, calls
    `copernicusmarine.describe(dataset_id=...)`, emits the canonical
    stanza, runs it through :func:`compact_text`, and appends it to
    the per-domain catalog file chosen by :func:`catalog_file_for`
    (creating the file with a `datasets:` header if it does not yet
    exist). The dataset id is also added to `_index.yaml`'s
    `available_datasets:` so the `curated ⊆ index` invariant holds.
    Finally reloads via `Catalog.load()` so any malformed stanza
    fails loud rather than silently corrupting the catalog.

    Args:
        args: Parsed argparse namespace. Honours `args.catalog_dir`
            (`Path` to the `catalog/` directory) and
            `args.dataset_ids` (`list[str]` of dataset ids to add).

    Returns:
        `0` on a clean append (or when every id is already curated),
        `1` when every requested id failed `describe()`.

    Examples:
        - Add one new dataset:

            `... add-ids cmems_mod_glo_phy_anfc_0.083deg_P1D-m`

          appends a freshly-emitted stanza to `catalog/global-physics.yaml`,
          adds the id to `_index.yaml`, and re-parses.
        - Add several at once:

            `... add-ids ds_a ds_b ds_c`

          fetches and appends each to its per-domain file; already-
          curated ids are skipped with a note on stderr.
    """
    import copernicusmarine as cm

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from earthlens.cmems import Catalog
    from earthlens.cmems.catalog import clear_catalog_cache

    cat = Catalog.load(args.catalog_dir)
    existing = set(cat.datasets)
    fresh = [d for d in args.dataset_ids if d not in existing]
    skipped = sorted(set(args.dataset_ids) - set(fresh))
    if skipped:
        print(f"skipping already-curated: {skipped}")
    if not fresh:
        print("nothing to add — all ids already curated")
        return 0

    # Group emitted stanzas by their target per-domain file.
    per_file: dict[str, list[str]] = {}
    appended = 0
    for ds_id in fresh:
        try:
            resp = cm.describe(dataset_id=ds_id, disable_progress_bar=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {ds_id}: describe() failed: {exc}", file=sys.stderr)
            continue
        product = next(iter(resp.products), None)
        if product is None:
            print(f"  ! {ds_id}: describe() returned no product", file=sys.stderr)
            continue
        target = next(
            (d for d in product.datasets if d.dataset_id == ds_id),
            None,
        )
        if target is None:
            print(f"  ! {ds_id}: not in describe() response", file=sys.stderr)
            continue
        stem = catalog_file_for(product.product_id)
        per_file.setdefault(stem, []).append(emit_dataset_stanza(product, target))
        appended += 1

    if appended == 0:
        print("nothing to append — every id failed describe()")
        return 1

    # Add the fresh ids to _index.yaml FIRST, so the curated-subset
    # invariant the loader enforces still holds when we re-parse below.
    index_path = args.catalog_dir / "_index.yaml"
    if index_path.exists():
        index_text = index_path.read_text(encoding="utf-8")
        current_index = set(yaml.safe_load(index_text).get("available_datasets") or [])
        merged_ids = sorted(current_index | set(fresh))
        index_path.write_text(
            splice_available_datasets(
                index_text, render_available_datasets_block(merged_ids)
            ),
            encoding="utf-8",
        )

    for stem, stanzas in per_file.items():
        target_file = args.catalog_dir / f"{stem}.yaml"
        cleaned = compact_text("".join(stanzas))
        if target_file.exists():
            new_text = append_stanzas_to_datasets_block(
                target_file.read_text(encoding="utf-8"), cleaned
            )
        else:
            new_text = f"datasets:\n\n{cleaned.rstrip(chr(10))}\n"
        target_file.write_text(new_text, encoding="utf-8")
        print(f"  appended {len(stanzas)} -> {target_file.name}")

    clear_catalog_cache()
    cat2 = Catalog.load(args.catalog_dir)
    print(f"appended {appended} stanzas — total curated: {len(cat2.datasets)}")
    return 0


def _cmd_compact(args: argparse.Namespace) -> int:
    """stdin -> stdout normalisation pass over raw `refresh --with-datasets` output.

    Implements the `compact` subcommand. Reads the entire stdin
    buffer, runs it through :func:`compact_text` (CRLF -> LF, blank-
    run collapse, scratch-marker strip, trailing-whitespace trim),
    and writes the result to stdout. Acts as a Unix-style filter:

        ... refresh --with-datasets X | ... compact > stanzas.yaml

    Args:
        args: Parsed argparse namespace (unused; reserved for future
            options).

    Returns:
        `0` always — the pass is purely textual and cannot fail.
    """
    sys.stdout.write(compact_text(sys.stdin.read()))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI dispatch -- route to refresh / add-ids / compact handlers.

    Builds the `argparse` tree, parses `argv`, and delegates to the
    selected subcommand handler (`_cmd_refresh`, `_cmd_add_ids`, or
    `_cmd_compact`). Mirrors the entry-point shape used by every
    other `tools/{backend}/refresh_*.py` script in the repo, so a
    maintainer who knows the GEE or ECMWF tooling sees the same CLI.

    Args:
        argv: Argument vector to parse, in the form
            `argparse.ArgumentParser.parse_args` accepts. `None`
            falls back to `sys.argv[1:]`.

    Returns:
        Exit code from the selected subcommand: `0` on success,
            `1` on any toolbox / parse / I/O error.

    Examples:
        - From a script entry point:

            ```python
            raise SystemExit(main())
            ```

        - Programmatic dispatch in tests:

            ```python
            assert main(["refresh", "--dry-run", "--index", "path/to/_index.yaml"]) == 0
            ```
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser(
        "refresh",
        help="walk describe() + rewrite available_datasets: (+ optional --with-datasets stanzas)",
    )
    p_refresh.add_argument(
        "--index",
        type=Path,
        default=INDEX_PATH,
        help="path to catalog/_index.yaml",
    )
    p_refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="print the new available_datasets: block instead of writing",
    )
    p_refresh.add_argument(
        "--with-datasets",
        nargs="+",
        metavar="PRODUCT_ID",
        help="also print ready-to-paste datasets: stanzas for every dataset under each listed product",
    )
    p_refresh.set_defaults(func=_cmd_refresh)

    p_add = sub.add_parser(
        "add-ids",
        help="fetch + compact + append stanzas for the given dataset ids",
    )
    p_add.add_argument(
        "dataset_ids",
        nargs="+",
        metavar="DATASET_ID",
    )
    p_add.add_argument(
        "--catalog-dir",
        type=Path,
        default=CATALOG_DIR,
        help="path to the catalog/ directory",
    )
    p_add.set_defaults(func=_cmd_add_ids)

    p_cpt = sub.add_parser(
        "compact",
        help="stdin -> stdout post-processor for raw `refresh --with-datasets` output",
    )
    p_cpt.set_defaults(func=_cmd_compact)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
