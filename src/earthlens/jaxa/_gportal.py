"""`gportal` protocol branch — search + SFTP download via the `gportal` SDK.

Imports `gportal` lazily so the wider `earthlens.jaxa` surface stays
importable without the `[jaxa]` extra. The branch translates the
`SpatialExtent` + `TemporalExtent` into a `gportal.search(...)` call,
materialises the resulting `Search.products()` iterator, and downloads
each `Product` over SFTP via `gportal.download(...)`.

`gportal.download` is sequential upstream (a list comprehension in
`gportal/sftp.py`); the function has no `processes=` argument and adding
concurrency at this layer is a follow-on. Auth is module-level —
`JaxaAuth.configure("gportal")` sets `gportal.username` and
`gportal.password` before this branch runs.
"""

from __future__ import annotations

from pathlib import Path

from earthlens.base.abstractdatasource import SpatialExtent, TemporalExtent
from earthlens.jaxa.catalog import Dataset


def fetch_gportal(
    *,
    dataset: Dataset,
    space: SpatialExtent,
    time: TemporalExtent,
    out_dir: Path,
) -> list[Path]:
    """Search G-Portal for a dataset and SFTP-download the matching products.

    Args:
        dataset: The resolved catalog row (its `short_name` is the
            G-Portal numeric dataset id passed to `gportal.search`).
        space: The validated WGS84 bbox.
        time: The validated date window.
        out_dir: Output directory (created if missing).

    Returns:
        list[Path]: One written path per product the search returned.
            Empty list when the search matches no products (treated as a
            normal empty AOI/time response, not an error).

    Raises:
        ImportError: If the `gportal` SDK is not installed.
        ValueError: If `dataset.short_name` is missing — bad catalog row.
    """
    try:
        import gportal  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "the 'gportal' SDK is required for the gportal protocol. "
            "Install it via the [jaxa] extra: pip install 'earthlens[jaxa]'."
        ) from exc

    if not dataset.short_name:
        raise ValueError(
            f"dataset {dataset.key!r} has no short_name — bad catalog row."
        )

    search = gportal.search(
        dataset_ids=[dataset.short_name],
        start_time=time.start_date.isoformat(),
        end_time=time.end_date.isoformat(),
        bbox=[space.west, space.south, space.east, space.north],
    )
    matched = search.matched() or 0
    if matched == 0:
        return []

    products = list(search.products())
    if not products:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = gportal.download(products, local_dir=str(out_dir))
    if isinstance(paths, str):
        paths = [paths]
    return [Path(p) for p in paths]
