"""Curation probes — extract a dataset's band/asset schema from a live sample.

The companion to :mod:`earthlens.cli.refresh`. Where `refresh` regenerates the
informational `available_*` index, `probe` produces the *seed* for the curated,
load-bearing rows: it fetches one sample record from a provider and records the
per-band / per-asset metadata (media type, common name, dtype, nodata) a
maintainer reviews before pasting into the catalog. This is the CLI port of the
`tools/*/probe_*.py` scripts.

Like `refresh`, only providers with a usable sample source have a prober
wired up; others report `unsupported`. Adding one is a single entry in
:data:`_PROBERS`. The heavier **credentialed** samplers (real NetCDF /
granule / CDS retrieval / full NWP availability) live in :data:`_DEEP_PROBERS`
and are reached with `probe --deep` (cmems, earthdata, ecmwf, nwp); `--deep`
falls back to the light prober for providers without a deep sampler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from earthlens._cli_tooling import dispatch_table
from earthlens.cli.adapter import BackendInfo, load_catalog


@dataclass
class ProbeResult:
    """The asset/band schema probed for one dataset.

    Attributes:
        provider: Canonical provider id.
        dataset: The dataset / collection probed.
        status: `"ok"`, `"unsupported"` (no prober), or `"error"`.
        detail: Failure reason for `"error"` / `"unsupported"`, else empty.
        assets: Mapping of asset key -> `{media_type, common_name, dtype,
            nodata}` (empty unless `status == "ok"`).
    """

    provider: str
    dataset: str
    status: str
    detail: str = ""
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Project the result to a JSON-friendly dict.

        Returns:
            A mapping of every field, suitable for `json.dumps`.

        Examples:
            - The probed asset schema is nested under `assets`:

                ```python
                >>> from earthlens.cli.curate import ProbeResult
                >>> result = ProbeResult(
                ...     "stac", "sentinel-2-l2a", "ok",
                ...     assets={"B04": {"common_name": "red"}},
                ... )
                >>> result.to_dict()["assets"]["B04"]["common_name"]
                'red'

                ```
        """
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "status": self.status,
            "detail": self.detail,
            "assets": self.assets,
        }


def _bands_from_summaries(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a STAC doc's `summaries.eo:bands` (or `gee:bands`) list."""
    summaries = body.get("summaries", {}) or {}
    return summaries.get("eo:bands") or summaries.get("gee:bands") or []


def _infer_dtype(value: str | None) -> str:
    """Infer a coarse dtype (`int` / `float` / `str`) from a sample value."""
    if value is None or value == "":
        return "str"
    try:
        int(value)
        return "int"
    except ValueError:
        pass
    try:
        float(value)
        return "float"
    except ValueError:
        return "str"


#: Provider id -> a credentialed deep sampler (the `--deep` half of probe).
_DEEP_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("deep_prober"),
}


#: Provider id -> a callable taking the loaded catalog and a dataset id and
#: returning its per-entry schema.
_PROBERS: dict[str, Callable[[Any, str], dict[str, dict[str, Any]]]] = {
    # Discovered handlers first; in-core literals are the migration remainder.
    **dispatch_table("prober"),
}


def supported_providers(deep: bool = False) -> list[str]:
    """Return the provider ids that have a curation prober wired up.

    Args:
        deep: When `True`, include providers that only have a credentialed
            `--deep` sampler (none currently — every deep provider also has
            a light prober).

    Returns:
        The sorted provider ids `probe` can sample.

    Examples:
        - STAC is wired up:

            ```python
            >>> from earthlens.cli.curate import supported_providers
            >>> "stac" in supported_providers()
            True

            ```
    """
    providers = set(_PROBERS)
    if deep:
        providers |= set(_DEEP_PROBERS)
    return sorted(providers)


def probe_dataset(info: BackendInfo, dataset: str, deep: bool = False) -> ProbeResult:
    """Probe one dataset's asset/band schema from a live sample record.

    With `deep=True` it uses the credentialed heavy sampler (real NetCDF /
    granule / CDS retrieval) when the provider has one, falling back to the
    light public prober otherwise. A provider with neither returns
    `"unsupported"`; any fetch / parse failure returns `"error"` — neither
    raises.

    Args:
        info: The backend the dataset belongs to.
        dataset: The dataset / collection id to probe.
        deep: Use the credentialed deep sampler when available.

    Returns:
        The :class:`ProbeResult`.
    """
    prober = (_DEEP_PROBERS.get(info.provider) if deep else None) or _PROBERS.get(
        info.provider
    )
    if prober is None:
        return ProbeResult(
            provider=info.provider,
            dataset=dataset,
            status="unsupported",
            detail="no sample endpoint wired up for probing",
        )
    try:
        catalog = load_catalog(info)
        assets = prober(catalog, dataset)
    except Exception as exc:  # noqa: BLE001 — network / parse failures are reported
        return ProbeResult(
            provider=info.provider, dataset=dataset, status="error", detail=str(exc)
        )
    return ProbeResult(
        provider=info.provider, dataset=dataset, status="ok", assets=assets
    )
