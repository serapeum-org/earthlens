"""CLI-tooling table for the `earthlens-hazards` provider distribution.

Published to core's catalog-tooling commands through the `earthlens.cli`
entry-point group and merged by `earthlens._cli_tooling.discover_cli_tooling`.

Import-light by contract (the sibling of `earthlens._hazards`): this module
names each provider's handlers as `"module:attr"` targets, never importing the
handler modules or their SDKs, so resolving the entry point costs nothing until
a command actually dispatches to a provider.
"""

from __future__ import annotations

__all__ = ["CLI_TOOLING"]

#: `id -> {role -> target}`. A callable role's target is a `"module:attr"`
#: string core imports lazily on dispatch; a config role (`index_attr`) carries
#: a literal value.
CLI_TOOLING: dict[str, dict[str, str]] = {
    "overture": {
        "refresher": "earthlens.overture.cli:refresher",
        "writer": "earthlens.overture.cli:writer",
        "curated_ids": "earthlens.overture.cli:curated_ids",
        "index_attr": "available_releases",
        "prober": "earthlens.overture.cli:prober",
        "validator": "earthlens.overture.cli:validator",
        "live_validator": "earthlens.overture.cli:live_validator",
    },
    "aqueduct": {"validator": "earthlens.aqueduct.cli:validator"},
    "emdat": {"validator": "earthlens.emdat.cli:validator"},
    "fdsn": {
        "refresher": "earthlens.fdsn.cli:refresher",
        "curated_ids": "earthlens.fdsn.cli:curated_ids",
        "validator": "earthlens.fdsn.cli:validator",
    },
    "firms": {
        "refresher": "earthlens.firms.cli:refresher",
        "prober": "earthlens.firms.cli:prober",
        "validator": "earthlens.firms.cli:validator",
    },
    "flodis": {"validator": "earthlens.flodis.cli:validator"},
    "flopros": {"validator": "earthlens.flopros.cli:validator"},
    "gdacs": {"validator": "earthlens.gdacs.cli:validator"},
    "hdx": {
        "refresher": "earthlens.hdx.cli:refresher",
        "writer": "earthlens.hdx.cli:writer",
        "curated_ids": "earthlens.hdx.cli:curated_ids",
        "prober": "earthlens.hdx.cli:prober",
        "emitter": "earthlens.hdx.cli:emitter",
    },
    "hanze": {"validator": "earthlens.hanze.cli:validator"},
    "jrc": {"validator": "earthlens.jrc.cli:validator"},
    "nsi": {"validator": "earthlens.nsi.cli:validator"},
    "osm": {"validator": "earthlens.osm.cli:validator"},
}
