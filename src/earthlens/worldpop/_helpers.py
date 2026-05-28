"""Shared helpers for the WorldPop backend.

Holds the small, network-free utilities the backend and REST layer reuse:
EPSG parsing, ISO3 normalisation, the bundled ISO3 → bbox table loader and
the bbox → intersecting-ISO3 resolution (`G5`), and the age/sex cohort
filename parser (`C6`). The download / mosaic / table-flatten orchestration
lives in `backend.py`; this module is pure functions so it is trivially
testable without the network.
"""

from __future__ import annotations

import re
from pathlib import Path

from earthlens.base.yaml_loader import load_yaml_strict

#: Bundled ISO3 → `[west, south, east, north]` bbox table (WGS84).
ISO3_BBOX_PATH: Path = Path(__file__).parent / "iso3_bbox.yaml"

#: Matches a WorldPop age/sex cohort filename: `{iso3}_{sex}_{agelow}_{year}.tif`
#: (e.g. `ken_f_0_2020.tif`, `com_m_80_2000.tif`).
_COHORT_RE = re.compile(r"^[a-z]{3}_([mf])_(\d+)_(\d{4})\.tif$", re.IGNORECASE)


def epsg_int(crs: str | int) -> int:
    """Parse an EPSG string / code to its integer code.

    Args:
        crs: An EPSG code as `4326`, `"4326"`, or `"EPSG:4326"`
            (case-insensitive).

    Returns:
        int: The numeric EPSG code.

    Raises:
        ValueError: If `crs` is not a recognised EPSG code.

    Examples:
        - Accepts the common spellings:
            ```python
            >>> from earthlens.worldpop._helpers import epsg_int
            >>> epsg_int("EPSG:4326")
            4326
            >>> epsg_int(3857)
            3857

            ```
    """
    if isinstance(crs, int):
        return crs
    text = str(crs).strip().upper()
    if text.startswith("EPSG:"):
        text = text[len("EPSG:") :]
    try:
        return int(text)
    except ValueError:
        raise ValueError(
            f"could not parse {crs!r} as an EPSG code "
            "(expected e.g. 4326 or 'EPSG:4326')."
        ) from None


def normalise_iso3(code: str) -> str:
    """Return an upper-cased, stripped ISO3 country code.

    Args:
        code: A 3-letter ISO 3166-1 alpha-3 code in any case.

    Returns:
        str: The upper-cased code.

    Raises:
        ValueError: If `code` is not three ASCII letters.
    """
    text = code.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", text):
        raise ValueError(f"{code!r} is not a 3-letter ISO3 country code.")
    return text


def cohort_of(url_or_name: str) -> tuple[str, int] | None:
    """Parse the `(sex, age_low)` cohort from a WorldPop age/sex filename.

    Args:
        url_or_name: A URL or bare filename. Age/sex files look like
            `{iso3}_{sex}_{agelow}_{year}.tif` (`ken_f_0_2020.tif`); plain
            population files (`ken_ppp_2020.tif`) do not match.

    Returns:
        tuple[str, int] | None: `(sex, age_low)` with `sex` in `{"m", "f"}`
            and `age_low` the lower bound of the 5-year band, or `None` for
            a non-cohort filename.

    Examples:
        - An age/sex file parses; a plain population file does not:
            ```python
            >>> from earthlens.worldpop._helpers import cohort_of
            >>> cohort_of("ken_f_0_2020.tif")
            ('f', 0)
            >>> cohort_of("https://x/com_m_80_2000.tif")
            ('m', 80)
            >>> cohort_of("ken_ppp_2020.tif") is None
            True

            ```
    """
    name = url_or_name.rsplit("/", 1)[-1]
    match = _COHORT_RE.match(name)
    if match is None:
        return None
    return match.group(1).lower(), int(match.group(2))


def load_iso3_bbox(path: Path | None = None) -> dict[str, list[float]]:
    """Load the bundled ISO3 → `[w, s, e, n]` bbox table.

    Args:
        path: Path to the table YAML. Defaults to `ISO3_BBOX_PATH`.

    Returns:
        dict[str, list[float]]: `ISO3 -> [west, south, east, north]` (WGS84).

    Raises:
        ValueError: If the file is missing or has no `bboxes:` block.
    """
    path = path if path is not None else ISO3_BBOX_PATH
    data = load_yaml_strict(path) or {}
    bboxes = data.get("bboxes") or {}
    if not bboxes:
        raise ValueError(f"{path} is missing or has an empty 'bboxes:' block.")
    return {normalise_iso3(k): list(v) for k, v in bboxes.items()}


def _bbox_intersects(a: list[float], b: list[float]) -> bool:
    """Return whether two `[w, s, e, n]` bboxes overlap (touching counts)."""
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    return not (ae < bw or be < aw or an < bs or bn < as_)


def iso3_for_bbox(
    bbox_wgs84: list[float], table: dict[str, list[float]]
) -> list[str]:
    """Return the ISO3 codes whose country bbox intersects `bbox_wgs84`.

    Args:
        bbox_wgs84: The AOI as `[west, south, east, north]` in degrees.
        table: An `ISO3 -> [w, s, e, n]` table (from `load_iso3_bbox`).

    Returns:
        list[str]: The intersecting ISO3 codes, sorted.
    """
    return sorted(
        iso3 for iso3, cbbox in table.items() if _bbox_intersects(bbox_wgs84, cbbox)
    )
