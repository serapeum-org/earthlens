"""Public toolkit for provider CLI-tooling modules.

The stable surface a provider's `earthlens.<backend>.cli` imports so its
`refresh` / `curate` / `validate` / `stanza` handlers can live in the provider
distribution while the shared machinery stays in core. Providers may not reach
into core's underscore internals (see `TestNoPrivateCoreImports` in
`test_distribution_boundaries`), so every helper a handler needs is re-exported
here under a public name.

The handlers themselves are registered by each distribution through the
`earthlens.cli` entry-point group (see `earthlens._cli_tooling`) and dispatched
by the catalog-tooling commands; this module is only the toolbox they are
built from.
"""

from __future__ import annotations

from earthlens.cli.adapter import BackendInfo
from earthlens.cli.refresh import (
    _curated_attr_ids as curated_attr_ids,
)
from earthlens.cli.refresh import (
    _flatten as flatten,
)
from earthlens.cli.refresh import (
    _get_json as get_json,
)
from earthlens.cli.refresh import (
    _index_writer as index_writer,
)
from earthlens.cli.validate import (
    _lint as lint,
)
from earthlens.cli.validate import (
    _require as require,
)

__all__ = [
    "BackendInfo",
    "curated_attr_ids",
    "flatten",
    "get_json",
    "index_writer",
    "lint",
    "require",
]
