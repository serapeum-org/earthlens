"""Configuration catalog for the National Water Model backend.

Hosts :class:`NWMCatalog`, the pydantic-backed reader for the bundled
`nwm_data_catalog.yaml`. NWM publishes many configurations (`short_range`,
`medium_range_mem1`, regional/coastal variants, analyses) whose file
names are **not** uniform — the forecast member can ride on the product
token (`channel_rt_1`), regional domains use sub-hourly 5-digit steps,
and analyses use `tmNN` instead of `fNNN`. So each :class:`NWMConfig`
row carries a full `key_template` (a `str.format` pattern over
`date` / `cycle` / `step` / `product`) that pins its exact S3 key,
rather than the backend guessing.

A configuration key (`"short_range"`) resolves to an
:class:`NWMConfig` via :meth:`NWMCatalog.get_config` (did-you-mean on a
miss). The path to the bundled YAML lives at :data:`CATALOG_PATH`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from earthlens.base import AbstractCatalog
from earthlens.base.yaml_loader import load_yaml_strict

CATALOG_PATH: Path = Path(__file__).parent / "nwm_data_catalog.yaml"

_CATALOG_CACHE: dict[Any, dict[str, "NWMConfig"]] = {}


def clear_catalog_cache() -> None:
    """Empty the module-level NWM-config parse cache."""
    _CATALOG_CACHE.clear()


def _load_configs(path: Path) -> dict[str, "NWMConfig"]:
    """Parse, validate, and cache the NWM config catalog at `path`.

    Args:
        path: Path to `nwm_data_catalog.yaml` (or a test override).

    Returns:
        dict[str, NWMConfig]: The `configurations:` map keyed by config key.

    Raises:
        ValueError: If the file has no `configurations:` block or a row
            fails validation.
    """
    resolved = str(path.resolve())
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = 0
    key = (resolved, mtime)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached

    data = load_yaml_strict(path) or {}
    rows = data.get("configurations") or {}
    if not rows:
        raise ValueError(f"{path} is missing or has an empty 'configurations:' block.")
    configs: dict[str, NWMConfig] = {}
    for cfg_key, body in rows.items():
        try:
            configs[cfg_key] = NWMConfig(**(body or {}))
        except ValidationError as exc:
            raise ValueError(
                f"{path} configuration {cfg_key!r} failed validation:\n{exc}"
            ) from exc
    _CATALOG_CACHE[key] = configs
    return configs


class NWMConfig(BaseModel):
    """One National Water Model configuration.

    The config key (`"short_range"`) is the parent key in
    :attr:`NWMCatalog.datasets` and is not stored on the row.

    Attributes:
        description: Human-readable summary.
        domain: Spatial domain (`"conus"`, `"hawaii"`, …) — informational.
        cycles_utc: The configuration's daily run hours, in `[0, 23]`.
        first_step: First forecast step published (NWM forecasts start at
            `f001`); the default step fetched when none is requested.
        horizon_h: Maximum forecast step.
        step_cadence_h: Spacing between published steps, for `horizon=`
            expansion.
        products: The product tokens available (`"channel_rt"`,
            `"land"`, …; ensemble members append the member, e.g.
            `"channel_rt_1"`).
        key_template: `str.format` pattern for the S3 key, over `date` /
            `cycle` (datetimes) / `step` (int) / `product` (str), e.g.
            `"nwm.{date:%Y%m%d}/short_range/nwm.t{cycle:%H}z.short_range.{product}.f{step:03d}.conus.nc"`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = ""
    domain: str = "conus"
    cycles_utc: list[int] = Field(default_factory=list)
    first_step: int = 1
    horizon_h: int = 0
    step_cadence_h: int = 1
    products: list[str] = Field(default_factory=list)
    key_template: str = ""


class NWMCatalog(AbstractCatalog):
    """Catalog of National Water Model configurations.

    Reads the bundled `nwm_data_catalog.yaml` and exposes its
    `configurations:` block as a typed `dict[str, NWMConfig]`.
    Instantiate with no arguments (`NWMCatalog()`).

    Examples:
        - Resolve a configuration and read its products:
            ```python
            >>> from earthlens.nwm import NWMCatalog
            >>> sr = NWMCatalog().get_config("short_range")
            >>> "channel_rt" in sr.products and sr.horizon_h
            18

            ```
    """

    _catalog_kind: str = "NWM catalog"

    datasets: dict[str, NWMConfig] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        """Auto-load the bundled `nwm_data_catalog.yaml` when no rows were supplied."""
        if self.datasets:
            return
        self.datasets = _load_configs(CATALOG_PATH)

    def get_catalog(self) -> dict[str, NWMConfig]:
        """Return the structural per-config map (satisfies the base contract)."""
        return self.datasets

    def get_config(self, key: str) -> NWMConfig:
        """Resolve a configuration key to its :class:`NWMConfig`.

        Args:
            key: A configuration key (e.g. `"short_range"`).

        Returns:
            NWMConfig: The resolved row.

        Raises:
            ValueError: When `key` is unknown (with a did-you-mean hint).
        """
        return self.get_dataset(key)
