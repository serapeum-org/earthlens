"""Bulk-hydrate empty-band GEE catalog rows from live Earth Engine.

Ported from the retired `tools/gee/refresh_gee_catalog.py` `hydrate-live`
subcommand. Powers `curate gee --fill-empty --write`: for every curated GEE
dataset whose `bands:` map is still an empty placeholder, query Earth Engine
for the asset's real type / title / date window / band names and splice them
into the existing stanza **in place** — the comments and ordering of the
surrounding rows are preserved, only the placeholder fields are rewritten.

Credentialed: the Earth Engine read sits behind :func:`_fetch_asset_payload`
(monkeypatch-able), so the stanza-rewriting core (:func:`_rewrite_stanza`)
stays pure and fully testable offline.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: Earth Engine asset `type` -> the lowercased `ee_type` the catalog row uses.
_EE_TYPE_MAP = {
    "IMAGE_COLLECTION": "image_collection",
    "IMAGE": "image",
    "TABLE": "table",
    "TABLE_COLLECTION": "table_collection",
    "FEATURE_VIEW": "table",
}

#: Band names that must be quoted in YAML (they parse as booleans / ints).
_BOOL_BAND_NAMES = {
    "y",
    "Y",
    "yes",
    "Yes",
    "YES",
    "n",
    "N",
    "no",
    "No",
    "NO",
    "true",
    "True",
    "TRUE",
    "false",
    "False",
    "FALSE",
    "on",
    "On",
    "ON",
    "off",
    "Off",
    "OFF",
}


def _strip_html(text: str) -> str:
    """Strip HTML tags + collapse whitespace; safe for use in a YAML title."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_placeholder_title(title: str | None) -> bool:
    """Return True when a curated title is still an empty / placeholder value."""
    if not title:
        return True
    return "(community-published catalog reference)" in title or title.strip() == ""


def _date_window(asset: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return an asset's `(start_date, end_date)` as `YYYY-MM-DD` (or None)."""
    start = asset.get("startTime") or asset.get("start_time")
    end = asset.get("endTime") or asset.get("end_time")
    if start:
        start = start[:10] if len(start) >= 10 else start
    if end:
        end = end[:10] if len(end) >= 10 else end
    return start, end


def _properties_text(asset: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty value among `keys` in the asset properties."""
    props = asset.get("properties") or {}
    for key in keys:
        value = props.get(key)
        if value:
            return str(value)
    return None


def _band_records(asset_id: str, asset: dict[str, Any], ee_mod: Any) -> list[dict]:
    """Return an asset's band records (from the asset doc, else live band names)."""
    bands = asset.get("bands") or []
    if bands:
        return list(bands)
    ee_type = asset.get("type", "").upper()
    try:
        if ee_type == "IMAGE":
            names = ee_mod.Image(asset_id).bandNames().getInfo()
        elif ee_type == "IMAGE_COLLECTION":
            names = ee_mod.ImageCollection(asset_id).first().bandNames().getInfo()
        else:
            return []
        return [{"id": name} for name in (names or [])]
    except Exception:  # noqa: BLE001 — an access-restricted asset has no bands
        return []


def _configure_ee() -> Any:
    """Authenticate the service account and return the `ee` module (creds)."""
    import os

    import ee
    from earthlens.gee.auth import EarthEngineAuth

    EarthEngineAuth.initialize(
        os.environ.get("GEE_SERVICE_ACCOUNT", ""),
        os.environ.get("GEE_SERVICE_KEY", ""),
        os.environ.get("GEE_PROJECT"),
    )
    return ee


def _fetch_asset_payload(asset_id: str, ee_mod: Any) -> dict[str, Any] | None:
    """Read one asset's type / title / dates / bands from Earth Engine.

    Args:
        asset_id: The Earth Engine asset id to hydrate.
        ee_mod: The authenticated `ee` module.

    Returns:
        The hydration payload (`ee_type` / `title` / `start_date` /
        `end_date` / `bands`), or None when the asset cannot be read.
    """
    try:
        asset = ee_mod.data.getAsset(asset_id)
    except Exception:  # noqa: BLE001 — an unreadable asset is skipped
        return None
    ee_type = _EE_TYPE_MAP.get(asset.get("type", "").upper(), "image_collection")
    bands = (
        _band_records(asset_id, asset, ee_mod)
        if ee_type in {"image", "image_collection"}
        else []
    )
    start, end = _date_window(asset)
    raw_title = _properties_text(asset, "title", "system:asset_title", "system:title")
    title = None
    if raw_title:
        cleaned = _strip_html(raw_title)
        title = (cleaned[:180].rstrip() if len(cleaned) > 180 else cleaned) or None
    return {
        "ee_type": ee_type,
        "title": title,
        "start_date": start,
        "end_date": end,
        "bands": bands,
    }


def _rewrite_stanza(
    text: str, asset_id: str, payload: dict[str, Any], existing_title: str | None
) -> str:
    """Splice hydrated `ee_type` / title / dates / bands into one stanza.

    Args:
        text: The full per-family catalog file text.
        asset_id: The asset id whose stanza to rewrite.
        payload: The hydration payload from :func:`_fetch_asset_payload`.
        existing_title: The curated title (only overwritten if a placeholder).

    Returns:
        The file text with `asset_id`'s placeholder fields filled in; the
        input is returned unchanged when the stanza is not found.
    """
    key = re.escape(asset_id)
    pattern = re.compile(rf"(?ms)^  {key}:\n(.*?)(?=^  [A-Za-z0-9/_.-]+:|\Z)")
    match = pattern.search(text)
    if not match:
        return text
    block = match.group(1)

    if payload["ee_type"]:
        block = re.sub(
            r"(    ee_type: )[^\n]+", rf"\1{payload['ee_type']}", block, count=1
        )
    if payload["title"] and _looks_like_placeholder_title(existing_title):
        title_yaml = payload["title"].replace("'", "''")
        block = re.sub(r"(    title: ).*", rf"\1'{title_yaml}'", block, count=1)
    if payload["start_date"]:
        block = re.sub(
            r'(      start_date: ")[^"]*(")',
            rf"\g<1>{payload['start_date']}\g<2>",
            block,
            count=1,
        )
    if payload["end_date"]:
        block = re.sub(
            r"(      end_date: )[^\n]+",
            rf'\1"{payload["end_date"]}"',
            block,
            count=1,
        )
    if payload["bands"]:
        block = _splice_bands(block, payload["bands"])

    return text[: match.start()] + f"  {asset_id}:\n" + block + text[match.end() :]


def _splice_bands(block: str, bands: list[dict[str, Any]]) -> str:
    """Replace a stanza's empty `bands:` map with the hydrated band rows."""
    lines = []
    for band in bands:
        band_id = band.get("id") or band.get("name") or ""
        if not band_id:
            continue
        quote = band_id in _BOOL_BAND_NAMES or band_id.isdigit()
        lines.append(f'      {chr(34) + band_id + chr(34) if quote else band_id}:')
        for key in ("wavelength_um", "center_wavelength"):
            value = band.get(key)
            if value is not None:
                lines.append(f"        wavelength: {value}")
                break
        for key in ("units", "unit"):
            value = band.get(key)
            if value:
                lines.append(f"        units: {value}")
                break
        scale = band.get("scale")
        if scale not in (None, 1.0, 1):
            lines.append(f"        scale: {scale}")
        offset = band.get("offset")
        if offset not in (None, 0.0, 0):
            lines.append(f"        offset: {offset}")
    bands_yaml = "    bands:\n" + "\n".join(lines) + "\n"
    replaced = re.sub(
        r"(?ms)^    bands:[ \t]*(\{\}\s*)?\n(?=^  [A-Za-z0-9/_.-]+:|\Z|\s*$)",
        bands_yaml,
        block,
        count=1,
    )
    if replaced == block:
        replaced = re.sub(
            r"(?ms)^    bands:[ \t]*(\{\}\s*)?\n", bands_yaml, block, count=1
        )
    return replaced


def _find_file_for_asset(catalog_dir: Path, asset_id: str) -> Path | None:
    """Return the per-family `catalog/*.yaml` file holding `asset_id`'s stanza."""
    head = re.compile(rf"(?m)^  {re.escape(asset_id)}:\s*$")
    for path in sorted(catalog_dir.glob("*.yaml")):
        if path.name == "_index.yaml":
            continue
        if head.search(path.read_text(encoding="utf-8")):
            return path
    return None


def bulk_hydrate_empty(limit: int | None = None) -> dict[str, Any]:
    """Fill every empty-band curated GEE row from live Earth Engine, in place.

    Loads the curated catalog, finds rows whose `bands:` is still an empty
    placeholder, reads each asset's real metadata from Earth Engine, and
    rewrites the stanza in its per-family file (preserving the rest).

    Args:
        limit: Only hydrate the first `limit` empty rows (alphabetical).

    Returns:
        A summary `{candidates, hydrated, skipped, filled}` mapping.
    """
    from earthlens.gee import Catalog
    from earthlens.gee.catalog import CATALOG_PATH, clear_catalog_cache

    catalog_dir = Path(CATALOG_PATH)
    ee_mod = _configure_ee()
    catalog = Catalog()
    empty = sorted(key for key, row in catalog.datasets.items() if not row.bands)
    if limit:
        empty = empty[:limit]
    existing_titles = {key: catalog.datasets[key].title for key in empty}

    file_text: dict[Path, str] = {}
    dirty: set[Path] = set()
    hydrated = 0
    skipped = 0
    filled: list[str] = []
    for asset_id in empty:
        payload = _fetch_asset_payload(asset_id, ee_mod)
        path = _find_file_for_asset(catalog_dir, asset_id) if payload else None
        if not payload or path is None:
            skipped += 1
            continue
        if path not in file_text:
            file_text[path] = path.read_text(encoding="utf-8")
        new_text = _rewrite_stanza(
            file_text[path], asset_id, payload, existing_titles.get(asset_id)
        )
        if new_text != file_text[path]:
            file_text[path] = new_text
            dirty.add(path)
            hydrated += 1
            filled.append(asset_id)
        else:
            skipped += 1

    for path in dirty:
        path.write_text(file_text[path], encoding="utf-8")
    clear_catalog_cache()
    return {
        "candidates": len(empty),
        "hydrated": hydrated,
        "skipped": skipped,
        "filled": filled,
    }
