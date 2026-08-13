"""CLI-tooling table for the `earthlens-land` provider distribution.

Published to core's catalog-tooling commands through the `earthlens.cli`
entry-point group and merged by `earthlens._cli_tooling.discover_cli_tooling`.

Import-light by contract (the sibling of `earthlens._land`): this module names
each provider's handlers as `"module:attr"` targets, never importing the handler
modules or their SDKs.
"""

from __future__ import annotations

__all__ = ["CLI_TOOLING"]

#: `id -> {role -> target}`. A callable role's target is a `"module:attr"`
#: string core imports lazily on dispatch; a config role (`stanza_block`)
#: carries a literal value.
CLI_TOOLING: dict[str, dict[str, str]] = {
    "bathymetry": {"validator": "earthlens.bathymetry.cli:validator"},
    "fabdem": {"validator": "earthlens.fabdem.cli:validator"},
    "glaciers": {"validator": "earthlens.glaciers.cli:validator"},
    "soilgrids": {"validator": "earthlens.soilgrids.cli:validator"},
    "gbif": {
        "refresher": "earthlens.gbif.cli:refresher",
        "curated_ids": "earthlens.gbif.cli:curated_ids",
        "prober": "earthlens.gbif.cli:prober",
        "validator": "earthlens.gbif.cli:validator",
        "emitter": "earthlens.gbif.cli:emitter",
        "stanza_block": "taxa",
    },
    "wdpa": {
        "refresher": "earthlens.wdpa.cli:refresher",
        "curated_ids": "earthlens.wdpa.cli:curated_ids",
        "prober": "earthlens.wdpa.cli:prober",
        "validator": "earthlens.wdpa.cli:validator",
        "emitter": "earthlens.wdpa.cli:emitter",
        "stanza_block": "countries",
    },
    "iucn": {
        "refresher": "earthlens.iucn.cli:refresher",
        "curated_ids": "earthlens.iucn.cli:curated_ids",
        "prober": "earthlens.iucn.cli:prober",
        "validator": "earthlens.iucn.cli:validator",
        "emitter": "earthlens.iucn.cli:emitter",
        "stanza_block": "countries",
    },
}
